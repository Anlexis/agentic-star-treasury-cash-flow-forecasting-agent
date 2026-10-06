"""AgentCore Platform v1.0"""

# pre_process slot (outer backbone) — input validation.
# This is the EXTERNAL-FACING gate; VERIFIED_EXTERNAL trust level is correct here.
# All inner domain nodes (DomainWorkflowGraph) use TrustLevel.INTERNAL.

import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT


class ValidateInputNode(FunctionNode):
    """Input validation for treasury cash-flow forecast invocations.

    Enforces:
      - Non-empty user_input
      - Rejects prompt-injection patterns
      - Trims and normalises whitespace
    """

    # Backbone pre_process gate — VERIFIED_EXTERNAL is correct (external-facing).
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    # Lightweight prompt-injection rejection patterns
    _INJECTION_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"(ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions?|"
        r"system\s*:\s*you\s+are|"
        r"<\s*/?(?:script|iframe|object)\s*>)",
        re.I,
    )

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        user_input = state.get("user_input", "")

        if not user_input or not user_input.strip():
            emit_progress(EMPTY_INPUT)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["ValidateInputNode: user_input is empty or missing"],
            }

        stripped = user_input.strip()

        # Reject prompt-injection patterns
        if self._INJECTION_RE.search(stripped):
            # Not a value the caller can correct: the content itself is refused,
            # so the run terminates rather than inviting a resend.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: input contains disallowed pattern"],
            }

        # Audit: record that a validated invocation was accepted.
        emit_trace_event(
            "pre_process_complete",
            {"input_chars": len(stripped)},
            state,
        )

        return {
            "validated_input": stripped,
            "status": AgentStatus.SUCCESS.value,
        }
