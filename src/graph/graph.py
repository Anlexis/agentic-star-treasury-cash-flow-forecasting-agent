"""AgentCore Platform v1.0"""

# src/graph/graph.py — FIN-C2-072 outer graph (Cat 2 nested).
#
# Architecture:
#   Outer AgentBaseGraph: fixed 5-node backbone.
#     pre_process  = ValidateInputNode  (VERIFIED_EXTERNAL)
#     main         = CashFlowForecastingGraphNode (GraphNode wrapping DomainWorkflowGraph)
#     post_process = SecurityGateOutputNode (ANONYMOUS)
#
#   Inner DomainWorkflowGraph (BaseGraph, src/graph/domain_workflow_graph.py):
#     6 domain nodes — all TrustLevel.INTERNAL.
#
# Class-name invariants:
#   CorporateTreasuryCashFlowForecastingAgent must match:
#     - config/agent.yaml  `class:`
#     - src/api/server.py  import + instantiation
#
# Do NOT override add_edges() on the outer graph.
# Inner graph nodes instantiated with NO ctor args in register_nodes().

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus

from src.graph.context_bridge import set_caller_input_context
from src.nodes.post_process_node import SecurityGateOutputNode
from src.nodes.pre_process_node import ValidateInputNode
from src.schemas.state import State

if TYPE_CHECKING:
    from src.graph.domain_workflow_graph import DomainWorkflowGraph

# Runtime-parameter file: src/graph/graph.py -> parents[2] is the repo root.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _runtime_config() -> dict[str, Any]:
    """Read the runtime parameters (max_retry, timeout_s) from config/config.yaml.

    Returns an empty dict — never raises — when the file is absent, unreadable,
    not valid YAML, or not a mapping. The inner graph validates the key
    `timeout_seconds`, so `timeout_s` is mapped on the way through.
    """
    try:
        import yaml

        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    out: dict[str, Any] = {}
    if "max_retry" in loaded:
        out["max_retry"] = loaded["max_retry"]
    if "timeout_s" in loaded:
        out["timeout_seconds"] = loaded["timeout_s"]
    return out


class CashFlowForecastingGraphNode(GraphNode):
    """GraphNode wrapper for the inner DomainWorkflowGraph.

    Assigned to the `main` slot in CorporateTreasuryCashFlowForecastingAgent.
    """

    # Re-raise inner graph errors as SubgraphError (fail fast)
    error_strategy: ClassVar[str] = "propagate"

    # HITL interrupts stay inside the inner graph
    propagate_hitl: ClassVar[bool] = False

    def _parent_config(self) -> dict[str, Any]:
        """Forward the runtime parameters to the inner graph.

        Everything declared in config/config.yaml (max_retry, timeout_s) is
        handed to DomainWorkflowGraph under config["configurable"], where its
        _validate_config() reads it at compile time. Without this the inner
        graph is constructed with an empty config and the declared values are
        dead configuration text.

        Degrades to {} — never raises — when the file is missing or malformed.
        """
        config = _runtime_config()
        if not config:
            return {}
        return {"configurable": config}

    def get_subgraph(self) -> "DomainWorkflowGraph":
        """Instantiate the inner DomainWorkflowGraph with the runtime config.

        The config travels through the BaseGraph constructor; the domain NODES
        still take no constructor arguments and keep the
        execute(self, state) -> dict signature.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request declined by pre_process has no validated input to act on, so
        running the inner graph would only produce a second, vaguer reason for
        the same rejection - and overwrite the specific one already settled.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        """Pass validated_input (set by pre_process) to the inner graph.

        Also bridges the caller's input_context to the inner graph:
        GraphNode.execute() does not forward input_context on subgraph.invoke()
        (SDK 1.0.1), and extract_input is the last our-code hook that sees the
        outer state before the inner invoke — see src/graph/context_bridge.py.
        """
        set_caller_input_context(state.get("input_context") or {})
        return cast(str, state.get("validated_input", state.get("user_input", "")))

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        """Map inner graph sub_result fields into outer state.

        GraphNode.execute() raises SubgraphError before calling merge_output
        when the inner graph returns status=ERROR.  So merge_output is only
        reached on a successful inner run — defaulting status to SUCCESS is
        always correct here (belt-and-suspenders, handles None sub_result status).

        Returns ONLY changed keys (never the full state).
        """
        return {
            # Outer reason wins: a reason settled before the inner run is the real
            # one, and a plain sub_result.get() would erase it.
            "error_code": state.get("error_code") or sub_result.get("error_code", ""),
            "result": sub_result.get("output") or sub_result.get("result", ""),
            "breach_detected": sub_result.get("breach_detected", False),
            "alert_payload_json": sub_result.get("alert_payload_json"),
            "scenario_analysis_json": sub_result.get("scenario_analysis_json"),
            # Default to SUCCESS — merge_output is unreachable on error (SubgraphError raised first)
            "status": sub_result.get("status") or AgentStatus.SUCCESS.value,
        }


class CorporateTreasuryCashFlowForecastingAgent(AgentBaseGraph):
    """FIN-C2-072 Corporate Treasury Cash Flow Forecasting & Liquidity Alert Agent.

    Cat 2 nested architecture:
      outer backbone: initialize -> pre_process -> main (domain workflow)
                     -> post_process -> finalize
      inner workflow: 6-node linear domain pipeline (DomainWorkflowGraph)

    Class name matches config/agent.yaml `class:` and src/api/server.py import.
    """

    @property
    def name(self) -> str:
        return "CorporateTreasuryCashFlowForecastingAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = ValidateInputNode()
        self._nodes["main"] = CashFlowForecastingGraphNode()
        self._nodes["post_process"] = SecurityGateOutputNode()

    # add_edges() is NOT overridden — backbone wiring is the framework's concern.
