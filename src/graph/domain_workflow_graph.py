"""AgentCore Platform v1.0"""

# src/graph/domain_workflow_graph.py — FIN-C2-072 inner domain workflow graph.
#
# Inherits BaseGraph (fully custom topology — no pre_process/main/post_process
# backbone inside the inner graph).  Called by CashFlowForecastingGraphNode
# in graph.py via get_subgraph().
#
# Pipeline (linear):
#   START → load_historical_data
#          → forecast_cash_positions
#          → identify_liquidity_risk
#          → check_threshold_breaches
#          → generate_scenario_analysis
#          → alert_if_required → END
#
# ⚠️ register_nodes(): NO super() call — BaseGraph.register_nodes() is abstract.
# ⚠️ All node ctors: NO arguments — SDK-v1 nodes have no __init__ params.
# ⚠️ Inner nodes: TrustLevel.INTERNAL (never VERIFIED_EXTERNAL on inner nodes).

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus

from src.graph.context_bridge import get_caller_input_context
from src.nodes.alert_if_required_node import AlertIfRequiredNode
from src.nodes.check_threshold_breaches_node import CheckThresholdBreachesNode
from src.nodes.forecast_cash_positions_node import ForecastCashPositionsNode
from src.nodes.generate_scenario_analysis_node import GenerateScenarioAnalysisNode
from src.nodes.identify_liquidity_risk_node import IdentifyLiquidityRiskNode
from src.nodes.load_historical_data_node import LoadHistoricalDataNode
from src.schemas.state import State, _from_json


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for FIN-C2-072.

    Orchestrates the 6-step treasury cash-flow forecasting and alert pipeline.
    Called by CashFlowForecastingGraphNode.get_subgraph() in graph.py.

    Pipeline:
        load_historical_data → forecast_cash_positions → identify_liquidity_risk
        → check_threshold_breaches → generate_scenario_analysis → alert_if_required
    """

    # ── Identity ─────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "CashFlowForecastingWorkflow"

    @property
    def state_schema(self) -> type:
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """Validate the runtime config forwarded by the outer GraphNode.

        CashFlowForecastingGraphNode._parent_config() forwards the
        config/config.yaml runtime parameters under config["configurable"].
        No key is mandatory — the domain nodes all carry literal defaults — but
        a value that is present must be usable, so a non-positive max_retry or
        timeout_seconds is rejected here rather than at run time.
        """
        configurable = (self.config or {}).get("configurable") or {}
        for key in ("max_retry", "timeout_seconds"):
            if key not in configurable:
                continue
            value = configurable[key]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                file_key = "timeout_s" if key == "timeout_seconds" else key
                raise ValueError(
                    f"DomainWorkflowGraph: config/config.yaml {file_key} " f"must be a positive integer, got {value!r}"
                )

    # ── Initial-state bridge ──────────────────────────────────────────────────

    def _extra_initial_state(self) -> dict[str, Any]:
        """Seed the inner graph's state with the caller's input_context.

        GraphNode.execute() does not forward input_context on subgraph.invoke()
        (SDK 1.0.1); CashFlowForecastingGraphNode.extract_input() stashes it via
        the context bridge immediately before the inner invoke, and this hook
        (called by BaseGraph.invoke while building initial state) reads it back.
        Inner domain nodes keep their plain state["input_context"] reads.
        """
        return {"input_context": get_caller_input_context()}

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all domain nodes with NO constructor arguments (SDK-v1).

        Do NOT call super() — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize / finalize (outer backbone concern).
        """
        self._nodes["load_historical_data"] = LoadHistoricalDataNode()
        self._nodes["forecast_cash_positions"] = ForecastCashPositionsNode()
        self._nodes["identify_liquidity_risk"] = IdentifyLiquidityRiskNode()
        self._nodes["check_threshold_breaches"] = CheckThresholdBreachesNode()
        self._nodes["generate_scenario_analysis"] = GenerateScenarioAnalysisNode()
        self._nodes["alert_if_required"] = AlertIfRequiredNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear domain pipeline."""
        self._sg.add_edge(START, "load_historical_data")
        self._sg.add_edge("load_historical_data", "forecast_cash_positions")
        self._sg.add_edge("forecast_cash_positions", "identify_liquidity_risk")
        self._sg.add_edge("identify_liquidity_risk", "check_threshold_breaches")
        self._sg.add_edge("check_threshold_breaches", "generate_scenario_analysis")
        self._sg.add_edge("generate_scenario_analysis", "alert_if_required")
        self._sg.add_edge("alert_if_required", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: dict[str, Any]) -> str:
        """Required by BaseGraph ABC; not called in a linear topology."""
        return END if state.get("status") == AgentStatus.ERROR.value else "alert_if_required"

    # ── Output shape ─────────────────────────────────────────────────────────

    def get_output(self, state: dict[str, Any]) -> dict[str, Any]:
        """Shape the sub_result dict returned to CashFlowForecastingGraphNode.merge_output().

        Designed together with outer merge_output() — field names must match.
        """
        # Assemble the human-readable forecast report
        forecast_report = _build_forecast_report(state)

        return {
            # the reason must leave the subgraph or the outer graph cannot report it
            "error_code": state.get("error_code"),
            "output": forecast_report,
            "status": state.get("status"),
            "breach_detected": state.get("breach_detected", False),
            "alert_payload_json": state.get("alert_payload_json"),
            "scenario_analysis_json": state.get("scenario_analysis_json"),
            "trace_id": state.get("trace_id"),
            "node_history": state.get("node_history", []),
        }


# ── Approved external-output schema ──────────────────────────────────────────
# The report is the template's ONLY external surface. It renders exclusively
# aggregate values derived from protected treasury fields, and every monetary
# value is rounded to _EXTERNAL_ROUND_UNIT before rendering — exact
# ledger-derived figures never leave the agent. Raw inputs (individual AP/AR
# line items, the raw historical series) are never rendered at all. The output
# gate (src/nodes/post_process_node.py) independently ENFORCES the rounding
# invariant on whatever string reaches it, so a future rendering change cannot
# silently reintroduce precise values.
_EXTERNAL_ROUND_UNIT = 1000


def _round_ext(value: float) -> float:
    """Round a monetary value to the approved external precision."""
    return float(round(float(value) / _EXTERNAL_ROUND_UNIT) * _EXTERNAL_ROUND_UNIT)


def _build_forecast_report(state: dict[str, Any]) -> str:
    """Assemble the external forecast report from domain state fields.

    Module-level function (not an instance method) for testability.
    Renders ONLY the approved external-output schema: aggregate figures,
    rounded to the nearest _EXTERNAL_ROUND_UNIT — see the schema note above.
    """
    forecast = _from_json(state.get("forecast_json")) or {}
    breaches = _from_json(state.get("threshold_breaches_json")) or []
    scenarios = _from_json(state.get("scenario_analysis_json")) or {}
    breach_detected = state.get("breach_detected", False)
    alert_payload = _from_json(state.get("alert_payload_json"))

    lines = [
        "=== Corporate Treasury Cash Flow Forecast — FIN-C2-072 ===",
        "(All monetary values rounded to the nearest 1,000 — approved external output schema.)",
        "",
        "Cash Position Forecasts:",
    ]

    for horizon in ("7d", "30d", "90d"):
        f = forecast.get(horizon, {})
        balance = f.get("projected_balance", "N/A")
        currency = f.get("currency", "JPY")
        lines.append(
            f"  {horizon}: {_round_ext(balance):,.0f} {currency}"
            if isinstance(balance, (int, float))
            else f"  {horizon}: {balance} {currency}"
        )

    if breach_detected:
        lines.append("")
        lines.append(f"LIQUIDITY ALERT: {len(breaches)} threshold breach(es) detected")
        for breach in breaches:
            lines.append(
                f"  [{breach.get('severity','?').upper()}] {breach.get('horizon')} — "
                f"shortfall {_round_ext(breach.get('shortfall')):,.0f} {breach.get('currency','')}"
                if isinstance(breach.get("shortfall"), (int, float))
                else f"  [{breach.get('severity','?').upper()}] {breach.get('horizon')}"
            )
        if alert_payload:
            lines.append(f"Alert level: {alert_payload.get('alert_level')}")
    else:
        lines.append("")
        lines.append("No liquidity threshold breaches detected.")

    if scenarios:
        lines.append("")
        lines.append("FX Scenario Analysis (30d):")
        fx_30d = scenarios.get("fx_30d", {})
        for scenario_name, data in fx_30d.items():
            bal = data.get("projected_balance", "N/A")
            lines.append(
                f"  {scenario_name}: {_round_ext(bal):,.0f}"
                if isinstance(bal, (int, float))
                else f"  {scenario_name}: {bal}"
            )

    return "\n".join(lines)
