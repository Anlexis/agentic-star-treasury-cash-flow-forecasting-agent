"""AgentCore Platform v1.0"""

# Inner domain node — IdentifyLiquidityRiskNode.
# Detects windows where the forecast cash position falls below the
# configured minimum threshold.
# TrustLevel.ANONYMOUS — inner graph node; trust gate is at the outer
# backbone pre_process (ValidateInputNode, VERIFIED_EXTERNAL).

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import INPUT_REJECTED

from src.schemas.state import _finite_in_range, _from_json, _to_json

# Default minimum cash threshold (JPY).  Override via input_context.
_DEFAULT_MIN_THRESHOLD = 0
# Caller-supplied thresholds must be FINITE and within magnitude bounds:
# float("NaN") parses fine, but NaN comparisons are always False — a NaN
# threshold silently suppresses every liquidity alert (fail-open).
_MAX_ABS_THRESHOLD = 1_000_000_000_000_000  # 1e15


class IdentifyLiquidityRiskNode(FunctionNode):
    """Identify forecast windows where cash position falls below threshold.

    Risk window: any horizon where projected_balance < min_cash_threshold
    (configured per subsidiary / currency in agent.yaml or input_context).
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
                "error_log": ["IdentifyLiquidityRiskNode: forecast_json is missing"],
            }

        input_context = state.get("input_context", {})
        raw_threshold = input_context.get("min_cash_threshold", _DEFAULT_MIN_THRESHOLD)
        parsed = _finite_in_range(raw_threshold, -_MAX_ABS_THRESHOLD, _MAX_ABS_THRESHOLD)
        if parsed is None:
            # Fail CLOSED: a rejected threshold produces an error, never a
            # silent no-alert run (NaN/Infinity would otherwise fail open).
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["IdentifyLiquidityRiskNode: min_cash_threshold must be a finite number within bounds"],
            }
        min_threshold = parsed

        risk_windows = []
        for horizon_key, forecast in forecasts.items():
            projected = forecast.get("projected_balance", 0)
            if projected < min_threshold:
                risk_windows.append(
                    {
                        "horizon": horizon_key,
                        "projected_balance": projected,
                        "min_threshold": min_threshold,
                        "shortfall": min_threshold - projected,
                        "currency": forecast.get("currency", "JPY"),
                    }
                )

        # Audit: record the liquidity-risk scan outcome.
        emit_trace_event(
            "identify_liquidity_risk_complete",
            {"risk_window_count": len(risk_windows)},
            state,
        )

        return {
            "liquidity_risks_json": _to_json(risk_windows),
            "status": AgentStatus.SUCCESS.value,
        }
