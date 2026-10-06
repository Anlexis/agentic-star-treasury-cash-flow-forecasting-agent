"""AgentCore Platform v1.0"""

# Inner domain node — LoadHistoricalDataNode.
# Loads, validates, and normalises AP/AR schedules + historical cash flow data.
# Emits audit trace on data access (J-SOX).
# TrustLevel.ANONYMOUS — inner graph node; trust gate is at the outer
# backbone pre_process (ValidateInputNode, VERIFIED_EXTERNAL).

import re
from typing import Any, ClassVar

from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import INPUT_REJECTED

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.schemas.state import _finite_in_range, _to_json

# ── Caller-data validation bounds ────────────────────────────────────────────
# input_context["historical_data"] is caller-supplied and untrusted: every field
# is bounds-checked before it can influence the forecast. Violations return a
# status=ERROR with the offending FIELD named — never the offending VALUE
# (treasury figures must not round-trip into error logs).
_MAX_FLOW_ENTRIES = 500
_MAX_ABS_AMOUNT = 1_000_000_000_000_000  # 1e15 — beyond any real cash position
_MAX_DAY_OFFSET = 365
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _validate_flows(raw: Any, field: str) -> tuple[list[dict[str, float]], str | None]:
    """Validate and normalise a caller-supplied cash-flow list.

    Accepted entry shape: {"day_offset": int 0..365, "amount": number 0..1e15}.
    Unknown keys are dropped. Returns (normalised_entries, error_message);
    error messages name the field/index only — never echo values.
    """
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return [], f"{field} must be a list"
    if len(raw) > _MAX_FLOW_ENTRIES:
        return [], f"{field} exceeds the maximum of {_MAX_FLOW_ENTRIES} entries"
    normalised: list[dict[str, float]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            return [], f"{field}[{i}] must be an object"
        day = entry.get("day_offset")
        amount = entry.get("amount")
        if not isinstance(day, int) or isinstance(day, bool) or not 0 <= day <= _MAX_DAY_OFFSET:
            return [], f"{field}[{i}].day_offset must be an integer between 0 and {_MAX_DAY_OFFSET}"
        parsed_amount = None if isinstance(amount, str) else _finite_in_range(amount, 0, _MAX_ABS_AMOUNT)
        if parsed_amount is None:
            return [], f"{field}[{i}].amount must be a finite non-negative number within bounds"
        normalised.append({"day_offset": float(day), "amount": parsed_amount})
    return normalised, None


class LoadHistoricalDataNode(FunctionNode):
    """Load, validate, and normalise AP/AR schedules and historical cash flow data.

    Supported input contract (input_context["historical_data"]):
        {
          "opening_balance": number,                      # may be negative (overdraft)
          "cash_inflows":  [{"day_offset": int, "amount": number}, ...],  # AR receipts
          "cash_outflows": [{"day_offset": int, "amount": number}, ...],  # AP payments
        }
    All fields are validated against explicit bounds before use. When the caller
    supplies no historical_data, the node degrades to an empty zero-balance
    baseline (data_quality: "stub") so the pipeline stays runnable for smoke
    invocations. Production deployments may replace this node with an
    authorized TMS/ERP adapter behind the same output shape.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        input_context = state.get("input_context", {}) or {}

        # Data specification (subsidiary, date range, currency) — validated.
        subsidiary_id = str(input_context.get("subsidiary_id", "default"))[:64]
        currency = input_context.get("currency", "JPY")
        if not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["LoadHistoricalDataNode: currency must be a 3-letter uppercase ISO code"],
            }
        raw_range = input_context.get("date_range_days", 90)
        if not isinstance(raw_range, int) or isinstance(raw_range, bool) or not 1 <= raw_range <= _MAX_DAY_OFFSET:
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"LoadHistoricalDataNode: date_range_days must be an integer 1..{_MAX_DAY_OFFSET}"],
            }
        date_range_days = raw_range

        # Caller-provided historical data (validated) or the stub baseline.
        supplied = input_context.get("historical_data")
        if supplied is None:
            opening_balance: float = 0.0
            inflows: list[dict[str, float]] = []
            outflows: list[dict[str, float]] = []
            data_source, data_quality = "input_context", "stub"
        else:
            if not isinstance(supplied, dict):
                emit_progress(INPUT_REJECTED)
                return {
                    "status": AgentStatus.SUCCESS.value,
                    "error_code": "INVALID_REQUEST",
                    "error_log": ["LoadHistoricalDataNode: historical_data must be an object"],
                }
            raw_balance = supplied.get("opening_balance", 0)
            parsed_balance = (
                None
                if isinstance(raw_balance, str)
                else _finite_in_range(raw_balance, -_MAX_ABS_AMOUNT, _MAX_ABS_AMOUNT)
            )
            if parsed_balance is None:
                emit_progress(INPUT_REJECTED)
                return {
                    "status": AgentStatus.SUCCESS.value,
                    "error_code": "INVALID_REQUEST",
                    "error_log": [
                        "LoadHistoricalDataNode: historical_data.opening_balance must be a finite number within bounds"
                    ],
                }
            opening_balance = parsed_balance
            inflows, err = _validate_flows(supplied.get("cash_inflows"), "historical_data.cash_inflows")
            if err:
                emit_progress(INPUT_REJECTED)
                return {
                    "status": AgentStatus.SUCCESS.value,
                    "error_code": "INVALID_REQUEST",
                    "error_log": [f"LoadHistoricalDataNode: {err}"],
                }
            outflows, err = _validate_flows(supplied.get("cash_outflows"), "historical_data.cash_outflows")
            if err:
                emit_progress(INPUT_REJECTED)
                return {
                    "status": AgentStatus.SUCCESS.value,
                    "error_code": "INVALID_REQUEST",
                    "error_log": [f"LoadHistoricalDataNode: {err}"],
                }
            data_source, data_quality = "caller_provided", "validated"

        historical_data = {
            "subsidiary_id": subsidiary_id,
            "currency": currency,
            "date_range_days": date_range_days,
            "cash_inflows": inflows,
            "cash_outflows": outflows,
            "opening_balance": opening_balance,
            "data_source": data_source,
            "data_quality": data_quality,
        }

        # Audit data access event
        emit_trace_event(
            "fin_c2_072.data_access",
            {
                "subsidiary_id": subsidiary_id,
                "currency": currency,
                "date_range_days": date_range_days,
                "data_quality": data_quality,
                "inflow_count": len(inflows),
                "outflow_count": len(outflows),
                "session_id": state.get("session_id", ""),
            },
            state,
        )

        return {
            "historical_data_json": _to_json(historical_data),
            "status": AgentStatus.SUCCESS.value,
        }
