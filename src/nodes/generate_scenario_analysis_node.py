"""AgentCore Platform v1.0"""

# Inner domain node — GenerateScenarioAnalysisNode.
# FX sensitivity and interest rate scenario modelling.
# TrustLevel.ANONYMOUS — inner graph node; trust gate is at the outer
# backbone pre_process (ValidateInputNode, VERIFIED_EXTERNAL).

import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import INPUT_REJECTED

from src.schemas.state import _finite_in_range, _from_json, _to_json

# Default FX scenario multipliers (base / stress / optimistic)
_FX_SCENARIOS: dict[str, float] = {
    "base": 1.00,
    "stress": 0.85,  # -15% FX adverse move
    "optimistic": 1.10,  # +10% FX favourable move
}

# Default interest rate scenario basis-point shifts
_IR_SCENARIOS: dict[str, float] = {
    "base": 0,
    "rate_hike_25bps": 25,
    "rate_cut_25bps": -25,
}

# Caller-supplied scenario tables are untrusted: names are rendered into the
# external report (so they must be inert identifiers, never free text) and
# multipliers/shifts feed arithmetic (so they must be finite and bounded —
# the same rule as every caller-controlled number).
_MAX_SCENARIOS = 8
_SCENARIO_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_FX_MULTIPLIER_RANGE = (0.0, 10.0)
_IR_BPS_RANGE = (-10_000.0, 10_000.0)


def _validate_scenarios(
    raw: Any, defaults: dict[str, float], value_range: tuple[float, float], field: str
) -> tuple[dict[str, float], str | None]:
    """Validate a caller-supplied scenario table; (table, error) — errors never echo values."""
    if raw is None:
        return dict(defaults), None
    if not isinstance(raw, dict):
        return {}, f"{field} must be an object"
    if not raw or len(raw) > _MAX_SCENARIOS:
        return {}, f"{field} must contain 1..{_MAX_SCENARIOS} scenarios"
    validated: dict[str, float] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _SCENARIO_NAME_RE.match(name):
            return {}, f"{field} scenario names must be lowercase identifiers (a-z, 0-9, _; max 32 chars)"
        parsed = _finite_in_range(value, *value_range)
        if parsed is None:
            return {}, f"{field}[{name}] must be a finite number within bounds"
        validated[name] = parsed
    return validated, None


class GenerateScenarioAnalysisNode(FunctionNode):
    """Generate FX sensitivity and interest rate scenario analysis.

    Applies configured FX rate and interest rate scenarios to the 30d/90d
    forecasts.  Scenario parameters are configurable per subsidiary via
    agent.yaml or input_context.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        forecasts = _from_json(state.get("forecast_json"))
        if not forecasts:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["GenerateScenarioAnalysisNode: forecast_json is missing"],
            }

        input_context = state.get("input_context", {})
        fx_scenarios, err = _validate_scenarios(
            input_context.get("fx_scenarios"), _FX_SCENARIOS, _FX_MULTIPLIER_RANGE, "fx_scenarios"
        )
        if err:
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"GenerateScenarioAnalysisNode: {err}"],
            }
        ir_scenarios, err = _validate_scenarios(
            input_context.get("ir_scenarios"), _IR_SCENARIOS, _IR_BPS_RANGE, "ir_scenarios"
        )
        if err:
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"GenerateScenarioAnalysisNode: {err}"],
            }

        scenario_results = {}

        # FX sensitivity on 30d and 90d horizons
        for horizon_key in ("30d", "90d"):
            forecast = forecasts.get(horizon_key, {})
            base_balance = forecast.get("projected_balance", 0)
            currency = forecast.get("currency", "JPY")

            fx_analysis = {}
            for scenario_name, multiplier in fx_scenarios.items():
                fx_analysis[scenario_name] = {
                    "projected_balance": base_balance * multiplier,
                    "fx_multiplier": multiplier,
                    "currency": currency,
                }
            scenario_results[f"fx_{horizon_key}"] = fx_analysis

        # Interest rate scenario impact (simplified: affects financing costs)
        ir_analysis = {}
        for scenario_name, bps_shift in ir_scenarios.items():
            ir_analysis[scenario_name] = {
                "rate_shift_bps": bps_shift,
                "impact": "see_full_model",
            }
        scenario_results["ir_sensitivity"] = ir_analysis

        # Audit: record that scenario analysis was generated.
        emit_trace_event(
            "generate_scenario_analysis_complete",
            {"scenario_group_count": len(scenario_results)},
            state,
        )

        return {
            "scenario_analysis_json": _to_json(scenario_results),
            "status": AgentStatus.SUCCESS.value,
        }
