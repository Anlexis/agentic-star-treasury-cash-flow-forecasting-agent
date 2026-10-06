"""AgentCore Platform v1.0"""

# Inner domain node — ForecastCashPositionsNode.
# Time-series forecasting for 7d/30d/90d cash positions.
# TrustLevel.ANONYMOUS — inner graph node; trust gate is at the outer
# backbone pre_process (ValidateInputNode, VERIFIED_EXTERNAL).

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import _from_json, _to_json


class ForecastCashPositionsNode(FunctionNode):
    """Forecast 7-day, 30-day, and 90-day cash positions.

    Deterministic flow projection: each horizon's position is the opening
    balance plus all validated inflows minus all validated outflows whose
    day_offset falls within the horizon. With no flow data the projection
    degrades to a flat carry-forward of the opening balance. Production
    deployments may replace this with an ARIMA/statistical model configured
    via agent.yaml behind the same output shape.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    _HORIZONS: ClassVar[list[int]] = [7, 30, 90]

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        historical_data = _from_json(state.get("historical_data_json"))
        if not historical_data:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ForecastCashPositionsNode: historical_data_json is missing"],
            }

        opening_balance = historical_data.get("opening_balance", 0)
        currency = historical_data.get("currency", "JPY")
        inflows = historical_data.get("cash_inflows") or []
        outflows = historical_data.get("cash_outflows") or []

        # Deterministic flow projection: for each horizon H,
        #   projected = opening_balance + Σ inflows(day_offset ≤ H) − Σ outflows(day_offset ≤ H)
        # Entries arrive validated by LoadHistoricalDataNode; malformed entries
        # (possible in direct unit-level use) contribute zero rather than raising.
        def _net_through(day_limit: int) -> float:
            def _sum(entries: list[Any]) -> float:
                total = 0.0
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    day = e.get("day_offset")
                    amount = e.get("amount")
                    if isinstance(day, (int, float)) and isinstance(amount, (int, float)) and day <= day_limit:
                        total += float(amount)
                return total

            return _sum(inflows) - _sum(outflows)

        model = "deterministic_flow_projection" if (inflows or outflows) else "flat_carry_forward"
        forecasts = {}
        for horizon in self._HORIZONS:
            projected = float(opening_balance) + _net_through(horizon)
            forecasts[f"{horizon}d"] = {
                "horizon_days": horizon,
                "currency": currency,
                "projected_balance": projected,
                "confidence_interval_low": projected * 0.9,
                "confidence_interval_high": projected * 1.1,
                "model": model,
            }

        # Audit: record that cash-position forecasts were produced.
        emit_trace_event(
            "forecast_cash_positions_complete",
            {"horizon_count": len(self._HORIZONS)},
            state,
        )

        return {
            "forecast_json": _to_json(forecasts),
            "status": AgentStatus.SUCCESS.value,
        }
