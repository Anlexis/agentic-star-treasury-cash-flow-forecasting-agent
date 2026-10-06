"""AgentCore Platform v1.0"""

# Inner domain node — AlertIfRequiredNode.
# Generates alert payload when a threshold breach is detected.
# No alert generated if breach_detected is False or missing.
# TrustLevel.ANONYMOUS — inner graph node; trust gate is at the outer
# backbone pre_process (ValidateInputNode, VERIFIED_EXTERNAL).

from datetime import datetime, timezone
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import _from_json, _to_json


class AlertIfRequiredNode(FunctionNode):
    """Generate treasury alert payload if a threshold breach was detected.

    Alert channels (email, Slack, TMS webhook) are configurable via
    agent.yaml or input_context.  This node produces the alert payload;
    actual dispatch is handled by the integration layer (not in scope for
    this template's core logic).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        breach_detected = state.get("breach_detected", False)

        if not breach_detected:
            # No breach — no alert required
            return {
                "alert_payload_json": None,
                "status": AgentStatus.SUCCESS.value,
            }

        breaches = _from_json(state.get("threshold_breaches_json")) or []
        input_context = state.get("input_context", {})

        critical_breaches = [b for b in breaches if b.get("severity") == "critical"]
        alert_level = "CRITICAL" if critical_breaches else "WARNING"

        alert_payload = {
            "alert_level": alert_level,
            "template_id": "FIN-C2-072",
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "subsidiary_id": input_context.get("subsidiary_id", "default"),
            "breach_count": len(breaches),
            "critical_count": len(critical_breaches),
            "alert_channels": input_context.get("alert_channels", ["email"]),
            "breaches": [
                {
                    "horizon": b.get("horizon"),
                    "severity": b.get("severity"),
                    "shortfall": b.get("shortfall"),
                    "currency": b.get("currency"),
                }
                for b in breaches
            ],
            "message": (
                f"Treasury {alert_level} — {len(breaches)} liquidity risk " f"window(s) detected in cash flow forecast."
            ),
        }

        # Audit: record that a treasury alert payload was generated.
        emit_trace_event(
            "alert_if_required_complete",
            {"alert_level": alert_level, "breach_count": len(breaches)},
            state,
        )

        return {
            "alert_payload_json": _to_json(alert_payload),
            "status": AgentStatus.SUCCESS.value,
        }
