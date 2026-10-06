# PB-6: Invoke Execution Order Verification
# Verifies BaseNode.__call__() enforces: trust gate -> node_start ->
# _security_gate_input() -> execute() -> _security_gate_output() ->
# node_complete, for every concrete node under src/nodes/.
#
# PB-6b: Full-graph backbone slot order
# CorporateTreasuryCashFlowForecastingAgent.invoke() must visit:
#   initialize -> pre_process -> main -> post_process -> finalize
# (by class name in node_history) and return AgentStatus.SUCCESS.

import importlib
import inspect
import pkgutil

import pytest

# ---------------------------------------------------------------------------
# Constants used by PB-6b (graph-level test)
# ---------------------------------------------------------------------------

# The GraphNode subclass placed in the outer graph's "main" backbone slot.
# Must match the class name in src/graph/graph.py.
_MAIN_SLOT_NODE = "CashFlowForecastingGraphNode"

# A valid treasury forecast request that:
#   - passes ValidateInputNode input validation (non-empty, no injection patterns)
#   - produces a SUCCESS-yielding domain pipeline run
_VALID_PAYLOAD = (
    "Forecast 7d/30d/90d cash flow for subsidiary CORP-001, currency JPY, "
    "and generate liquidity alerts if thresholds are breached."
)


# ---------------------------------------------------------------------------
# Autouse fixture: silence domain-level emit_trace_event calls
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _silence_domain_audit_emits(monkeypatch):
    """Patch emit_trace_event at the node-module level (NOT sys.modules).

    LoadHistoricalDataNode and SecurityGateOutputNode import emit_trace_event
    from shared.utils.audit_logger at module level.  Patching the module
    attribute silences the call without touching the shared package (never
    stub shared.* in sys.modules).
    """
    monkeypatch.setattr(
        "src.nodes.load_historical_data_node.emit_trace_event",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "src.nodes.post_process_node.emit_trace_event",
        lambda *a, **k: None,
    )


# ---------------------------------------------------------------------------
# Node discovery (PB-6a)
# ---------------------------------------------------------------------------


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ and collect concrete BaseNode subclasses."""
    from framework.nodes.base_node import BaseNode

    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError:
        return []

    seen: set[type] = set()
    discovered: list[type] = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
                and attr not in seen
            ):
                seen.add(attr)
                discovered.append(attr)
    return discovered


# ---------------------------------------------------------------------------
# PB-6a: per-node __call__ invoke order
# ---------------------------------------------------------------------------


class TestInvokeOrder:
    """PB-6a: __call__ must run trust-gate -> node_start -> input-gate -> execute() -> output-gate -> node_complete."""

    def test_call_order_for_every_node(self, monkeypatch):
        node_classes = _discover_node_classes()
        if not node_classes:
            pytest.skip("no concrete BaseNode subclasses found under src/nodes/")

        import framework.nodes.base_node as base_node_module

        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, _payload, _state, _o=order: _o.append(f"event:{event_type}"),
            )

            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _o=order, _label=label, _orig=original):
                    _o.append(_label)
                    return _orig(self, arg)

                monkeypatch.setattr(node_cls, method_name, spy)

            instance = node_cls()
            state = {
                "caller_trust_level": node_cls.required_trust_level.value,
                "correlation_id": "pb6-invoke-order-test",
            }
            instance(state)

            expected = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
                "event:node_complete",
            ]
            if order != expected:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\n" f"expected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)


# ---------------------------------------------------------------------------
# PB-6b: Full graph invoke backbone order
# ---------------------------------------------------------------------------


class TestGraphInvokeOrder:
    """PB-6b: Full graph invoke must visit every backbone slot in order and return SUCCESS.

    Verifies:
      - Graph compiles without error.
      - agent.invoke(_VALID_PAYLOAD) visits:
            InitializeNode -> ValidateInputNode -> CashFlowForecastingGraphNode
            -> SecurityGateOutputNode -> FinalizeNode
        (by class name in node_history).
      - Final status is AgentStatus.SUCCESS.
      - The main backbone slot is exactly _MAIN_SLOT_NODE.

    Trust level note:
      Uses TrustLevel.VERIFIED_EXTERNAL (the production external caller trust level).
      Inner domain nodes now declare TrustLevel.ANONYMOUS, so GraphNode.execute()
      propagating the VERIFIED_EXTERNAL ctx into the inner DomainWorkflowGraph
      does not trigger trust-gate denials on any domain node.
      The external-facing trust gate lives on ValidateInputNode (VERIFIED_EXTERNAL),
      which correctly enforces authentication for production callers.
    """

    def test_backbone_node_history(self, monkeypatch):
        """PB-6b: graph.invoke() must produce SUCCESS with full backbone node_history."""
        # Silence framework-level lifecycle emit_trace_event
        monkeypatch.setattr(
            "framework.nodes.base_node.emit_trace_event",
            lambda *a, **k: None,
        )

        from framework.schemas.agent_status import AgentStatus
        from framework.schemas.invocation_context import InvocationContext
        from framework.schemas.trust_level import TrustLevel
        from src.graph.graph import CorporateTreasuryCashFlowForecastingAgent

        agent = CorporateTreasuryCashFlowForecastingAgent()
        agent.compile()

        # VERIFIED_EXTERNAL: inner domain nodes now declare ANONYMOUS, so the
        # outer VERIFIED_EXTERNAL ctx passes all inner trust gates.
        ctx = InvocationContext(
            session_id="pb6b-graph-test",
            caller_id="pb6b-test",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        )
        result = agent.invoke(_VALID_PAYLOAD, ctx=ctx)

        node_history = result.get("node_history", [])

        # Every backbone class must appear — failure means a slot was skipped.
        expected_backbone = (
            "InitializeNode",
            "ValidateInputNode",  # pre_process slot
            _MAIN_SLOT_NODE,  # main slot = CashFlowForecastingGraphNode
            "SecurityGateOutputNode",  # post_process slot
            "FinalizeNode",
        )
        for cls_name in expected_backbone:
            assert cls_name in node_history, (
                f"Backbone class '{cls_name}' missing from node_history; " f"got: {node_history}"
            )

        # Order: each backbone node must appear after the previous one.
        indices = [node_history.index(n) for n in expected_backbone]
        assert indices == sorted(indices), f"Backbone nodes out of order in node_history; got: {node_history}"

        # Final status must be SUCCESS for a valid payload.
        status = result.get("status")
        assert (
            status == AgentStatus.SUCCESS or status == AgentStatus.SUCCESS.value
        ), f"Expected SUCCESS, got {status!r}; node_history: {node_history}"

    def test_main_slot_node_is_correct_type(self):
        """The `main` backbone slot must be an instance of _MAIN_SLOT_NODE."""
        from src.graph.graph import CashFlowForecastingGraphNode, CorporateTreasuryCashFlowForecastingAgent

        agent = CorporateTreasuryCashFlowForecastingAgent()
        agent.compile()
        main_node = agent._nodes.get("main")
        assert main_node is not None, "No node registered at `main` slot"
        assert isinstance(
            main_node, CashFlowForecastingGraphNode
        ), f"Expected {_MAIN_SLOT_NODE} at `main` slot, got {type(main_node).__name__}"
