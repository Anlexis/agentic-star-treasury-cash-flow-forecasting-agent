"""AgentCore Platform v1.0"""

# Inner domain node — CheckThresholdBreachesNode.
# Evaluates breach severity (warning / critical) for each risk window.
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

# Severity levels
_SEVERITY_WARNING = "warning"
_SEVERITY_CRITICAL = "critical"
# Critical if shortfall > 20% of threshold (configurable)
_CRITICAL_RATIO_DEFAULT = 0.20


class CheckThresholdBreachesNode(FunctionNode):
    """Classify liquidity risk windows as WARNING or CRITICAL.

    Critical: shortfall exceeds critical_ratio * min_threshold
    Warning:  shortfall > 0 but below critical threshold
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        risk_windows = _from_json(state.get("liquidity_risks_json"))
        if risk_windows is None:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CheckThresholdBreachesNode: liquidity_risks_json is missing"],
            }

        input_context = state.get("input_context", {})
        # Explicitly finite + bounded (NaN/Infinity must never reach the
        # severity comparison — see _finite_in_range).
        parsed_ratio = _finite_in_range(input_context.get("critical_ratio", _CRITICAL_RATIO_DEFAULT), 0, 10)
        if parsed_ratio is None:
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["CheckThresholdBreachesNode: critical_ratio must be a finite number between 0 and 10"],
            }
        critical_ratio = parsed_ratio

        breaches = []
        for window in risk_windows:
            shortfall = window.get("shortfall", 0)
            min_threshold = window.get("min_threshold", 0)
            critical_threshold = min_threshold * critical_ratio if min_threshold else 0

            severity = _SEVERITY_CRITICAL if shortfall > critical_threshold else _SEVERITY_WARNING
            breaches.append({**window, "severity": severity})

        breach_detected = len(breaches) > 0

        # Audit: record the breach-classification outcome.
        emit_trace_event(
            "check_threshold_breaches_complete",
            {"breach_count": len(breaches), "breach_detected": breach_detected},
            state,
        )

        return {
            "threshold_breaches_json": _to_json(breaches),
            "breach_detected": breach_detected,
            "status": AgentStatus.SUCCESS.value,
        }
