# FIN-C2-072 — Unit tests: caller-data input contract + approved external
# output schema.
#
# Covers two hardening behaviours:
#   1) LoadHistoricalDataNode validates input_context["historical_data"]
#      field-by-field (bounds, shapes) and never echoes rejected values.
#   2) ForecastCashPositionsNode computes real per-horizon flow projections.
#   3) The output gate enforces the external precision grid on DERIVED values
#      (not just verbatim blocked-field embedding), and the report renders
#      only rounded aggregate figures.

import json

import pytest

from framework.schemas.agent_status import AgentStatus

from src.graph.domain_workflow_graph import _build_forecast_report
from src.nodes.forecast_cash_positions_node import ForecastCashPositionsNode
from src.nodes.load_historical_data_node import LoadHistoricalDataNode
from src.schemas.state import _to_json


@pytest.fixture(autouse=True)
def silence_audit(monkeypatch):
    for mod_path in (
        "src.nodes.load_historical_data_node",
        "src.nodes.forecast_cash_positions_node",
        "src.nodes.identify_liquidity_risk_node",
        "src.nodes.check_threshold_breaches_node",
        "src.nodes.post_process_node",
    ):
        monkeypatch.setattr(mod_path + ".emit_trace_event", lambda *a, **k: None)


_VALID_DATA = {
    "opening_balance": 5_000_000,
    "cash_inflows": [{"day_offset": 5, "amount": 2_000_000}],
    "cash_outflows": [
        {"day_offset": 3, "amount": 1_000_000},
        {"day_offset": 20, "amount": 3_000_000},
    ],
}


def _load(input_context):
    node = LoadHistoricalDataNode()
    return node.execute({"input_context": input_context, "node_history": [], "session_id": "t"})


class TestLoadCallerDataValidation:
    def test_valid_caller_data_is_normalised(self):
        result = _load({"historical_data": _VALID_DATA})
        assert result["status"] == AgentStatus.SUCCESS
        data = json.loads(result["historical_data_json"])
        assert data["opening_balance"] == 5_000_000
        assert data["data_source"] == "caller_provided"
        assert data["data_quality"] == "validated"
        assert [e["amount"] for e in data["cash_inflows"]] == [2_000_000]
        assert len(data["cash_outflows"]) == 2

    def test_absent_caller_data_degrades_to_stub(self):
        result = _load({})
        assert result["status"] == AgentStatus.SUCCESS
        data = json.loads(result["historical_data_json"])
        assert data["opening_balance"] == 0
        assert data["data_quality"] == "stub"

    def test_non_object_historical_data_rejected(self):
        result = _load({"historical_data": [1, 2, 3]})
        assert result["status"] == AgentStatus.SUCCESS

    def test_non_numeric_opening_balance_rejected_without_echo(self):
        secret_marker = "SUPER-SECRET-BALANCE-STRING"
        result = _load({"historical_data": {"opening_balance": secret_marker}})
        assert result["status"] == AgentStatus.SUCCESS
        assert secret_marker not in " ".join(result["error_log"])

    def test_oversized_flow_list_rejected(self):
        flows = [{"day_offset": 1, "amount": 1} for _ in range(501)]
        result = _load({"historical_data": {"opening_balance": 0, "cash_inflows": flows}})
        assert result["status"] == AgentStatus.SUCCESS

    def test_negative_amount_rejected(self):
        result = _load({"historical_data": {"opening_balance": 0, "cash_inflows": [{"day_offset": 1, "amount": -5}]}})
        assert result["status"] == AgentStatus.SUCCESS

    def test_out_of_range_day_offset_rejected(self):
        result = _load({"historical_data": {"opening_balance": 0, "cash_outflows": [{"day_offset": 999, "amount": 5}]}})
        assert result["status"] == AgentStatus.SUCCESS

    def test_invalid_currency_rejected(self):
        result = _load({"currency": "YENS"})
        assert result["status"] == AgentStatus.SUCCESS


class TestForecastFlowProjection:
    def test_projection_arithmetic_per_horizon(self):
        node = ForecastCashPositionsNode()
        historical = dict(_VALID_DATA, currency="JPY")
        result = node.execute({"historical_data_json": _to_json(historical), "node_history": []})
        assert result["status"] == AgentStatus.SUCCESS
        forecast = json.loads(result["forecast_json"])
        # 7d: 5,000,000 + 2,000,000 − 1,000,000 (day 3 only) = 6,000,000
        assert forecast["7d"]["projected_balance"] == 6_000_000
        # 30d/90d: additionally − 3,000,000 (day 20) = 3,000,000
        assert forecast["30d"]["projected_balance"] == 3_000_000
        assert forecast["90d"]["projected_balance"] == 3_000_000
        assert forecast["7d"]["model"] == "deterministic_flow_projection"

    def test_no_flow_data_carries_opening_balance_flat(self):
        node = ForecastCashPositionsNode()
        historical = {"opening_balance": 1_000_000, "currency": "JPY", "cash_inflows": [], "cash_outflows": []}
        result = node.execute({"historical_data_json": _to_json(historical), "node_history": []})
        forecast = json.loads(result["forecast_json"])
        for horizon in ("7d", "30d", "90d"):
            assert forecast[horizon]["projected_balance"] == 1_000_000
            assert forecast[horizon]["model"] == "flat_carry_forward"


class TestOutputPrecisionEnforcement:
    def test_off_grid_derived_value_is_snapped(self):
        from src.nodes.post_process_node import _security_gate_output

        report = "7d position: 5,432,109 JPY"
        sanitised = _security_gate_output(report, {"session_id": "t"})
        assert "5,432,109" not in sanitised
        assert "5,432,000" in sanitised

    def test_structural_tokens_and_grid_values_pass(self):
        from src.nodes.post_process_node import _security_gate_output

        # Counts (bare 1-3 digits), horizons (90d), years (2026) and on-grid
        # monetary values (6,000,000 / the schema note's 1,000) are untouched.
        report = "7d: 6,000,000 JPY — 3 breach(es), horizon 90d, year 2026, rounded to the nearest 1,000"
        assert _security_gate_output(report, {"session_id": "t"}) == report

    def test_small_off_grid_monetary_is_snapped(self):
        """Monetary-by-FORM enforcement has no magnitude floor."""
        from src.nodes.post_process_node import _security_gate_output

        sanitised = _security_gate_output("Forecast balance: 9,999 JPY", {"session_id": "t"})
        assert "9,999" not in sanitised
        assert "10,000" in sanitised

    def test_unformatted_currency_adjacent_value_is_snapped(self):
        """A bare 4-digit value next to a currency marker is
        monetary by CONTEXT — an unformatted rendering regression must not
        bypass the gate."""
        from src.nodes.post_process_node import _security_gate_output

        sanitised = _security_gate_output("Forecast: 9999 JPY", {"session_id": "t"})
        assert "9999" not in sanitised
        assert "10,000 JPY" in sanitised

    def test_currency_symbol_adjacent_value_is_snapped(self):
        from src.nodes.post_process_node import _security_gate_output

        sanitised = _security_gate_output("balance ¥9999 today", {"session_id": "t"})
        assert "¥9999" not in sanitised
        assert "10,000" in sanitised

    def test_structural_year_without_currency_context_unchanged(self):
        from src.nodes.post_process_node import _security_gate_output

        report = "Report generated in 2026, year 2026, horizon 90d, 3 breach(es)"
        assert _security_gate_output(report, {"session_id": "t"}) == report

    @pytest.mark.parametrize(
        "leak, exact",
        [
            ("Forecast: JPY 9999", "9999"),  # code BEFORE value, spaced
            ("Forecast: USD 9999", "9999"),  # second currency code
            ("Forecast: 9999$", "9999"),  # symbol AFTER value
            ("Forecast: JPY9999", "9999"),  # code attached before value
            ("Forecast: ¥ 9999", "9999"),  # spaced symbol before value
            ("Forecast: ￥9999", "9999"),  # fullwidth yen before value
            ("Forecast: 9999円", "9999"),  # yen kanji after value
        ],
        ids=[
            "code-before",
            "code-before-usd",
            "symbol-after",
            "code-attached",
            "symbol-spaced",
            "fullwidth-yen",
            "yen-kanji-suffix",
        ],
    )
    def test_symmetric_currency_adjacency_is_snapped(self, leak, exact):
        """Currency-marker adjacency is symmetric — code or
        symbol, before or after, spaced or attached."""
        from src.nodes.post_process_node import _security_gate_output

        sanitised = _security_gate_output(leak, {"session_id": "t"})
        assert exact not in sanitised, sanitised
        assert "10,000" in sanitised, sanitised

    @pytest.mark.parametrize(
        "leak, snapped",
        [
            ("JPY-9999", "-10,000"),  # attached code, negative
            ("USD-9999", "-10,000"),  # second currency code, negative
            ("JPY +9999", "+10,000"),  # spaced code, positive
            ("JPY+9999", "+10,000"),  # attached code, positive
            ("+9999 JPY", "+10,000"),  # positive counterpart, code after
            ("¥-9999", "-10,000"),  # signed after symbol (regression guard)
        ],
        ids=[
            "code-neg-attached",
            "usd-neg-attached",
            "code-pos-spaced",
            "code-pos-attached",
            "pos-before-code",
            "symbol-neg",
        ],
    )
    def test_signed_currency_adjacent_values_snap_with_sign(self, leak, snapped):
        """Explicit +/- signs are part of the monetary token in
        every adjacency form, and the sign survives the snap."""
        from src.nodes.post_process_node import _security_gate_output

        sanitised = _security_gate_output(leak, {"session_id": "t"})
        assert "9999" not in sanitised, sanitised
        assert snapped in sanitised, sanitised

    @pytest.mark.parametrize("sep", ["  ", "\t", " \t ", "\n"], ids=["2sp", "tab", "mixed", "nl"])
    @pytest.mark.parametrize("sign, snapped", [("", "10,000"), ("+", "+10,000"), ("-", "-10,000")])
    @pytest.mark.parametrize("direction", ["marker-first", "value-first"], ids=["mk-val", "val-mk"])
    def test_multi_whitespace_delimiter_matrix(self, sep, sign, snapped, direction):
        """Separator runs ≥2 and/or non-space
        whitespace between marker and value, both directions, all signs."""
        from src.nodes.post_process_node import _security_gate_output

        leak = f"JPY{sep}{sign}9999" if direction == "marker-first" else f"{sign}9999{sep}JPY"
        sanitised = _security_gate_output(leak, {"session_id": "t"})
        assert "9999" not in sanitised, repr(sanitised)
        assert snapped in sanitised, repr(sanitised)
        assert sep in sanitised, repr(sanitised)  # original delimiter preserved

    def test_multi_whitespace_symbol_delimiter(self):
        from src.nodes.post_process_node import _security_gate_output

        sanitised = _security_gate_output("balance ￥\t-9999 today", {"session_id": "t"})
        assert "9999" not in sanitised, repr(sanitised)
        assert "-10,000" in sanitised, repr(sanitised)

    def test_embedded_acronym_year_stays_structural(self):
        """Word-boundary guard: the 3 letters before a number must be a
        standalone word — 'STAR 2026' must not read as currency-adjacent."""
        from src.nodes.post_process_node import _security_gate_output

        report = "the AGENTIC STAR 2026 rollout"
        assert _security_gate_output(report, {"session_id": "t"}) == report

    def test_verbatim_blocked_field_still_redacted(self):
        from src.nodes.post_process_node import _security_gate_output

        blocked = _to_json({"opening_balance": 123, "rows": ["sensitive"] * 3})
        report = f"debug dump: {blocked}"
        sanitised = _security_gate_output(report, {"historical_data_json": blocked, "session_id": "t"})
        assert blocked not in sanitised
        assert "[REDACTED]" in sanitised

    def test_report_renders_rounded_aggregates_only(self):
        state = {
            "forecast_json": _to_json({"7d": {"projected_balance": 1_234_567, "currency": "JPY"}}),
            "threshold_breaches_json": _to_json(
                [{"horizon": "7d", "severity": "warning", "shortfall": 765_433, "currency": "JPY"}]
            ),
            "breach_detected": True,
            "scenario_analysis_json": None,
            "alert_payload_json": None,
        }
        report = _build_forecast_report(state)
        assert "1,235,000" in report  # rounded, not exact
        assert "1,234,567" not in report
        assert "765,000" in report
        assert "765,433" not in report
        assert "approved external output schema" in report


class TestThresholdInputHardening:
    @staticmethod
    def _identify(threshold):
        from src.nodes.identify_liquidity_risk_node import IdentifyLiquidityRiskNode

        node = IdentifyLiquidityRiskNode()
        state = {
            "forecast_json": _to_json({"7d": {"projected_balance": 100, "currency": "JPY"}}),
            "input_context": {"min_cash_threshold": threshold},
            "node_history": [],
        }
        return node.execute(state)

    def test_non_numeric_min_threshold_errors_cleanly(self):
        assert self._identify("plenty")["status"] == AgentStatus.SUCCESS

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", float("nan"), float("inf"), float("-inf"), 1e16])
    def test_non_finite_or_unbounded_threshold_fails_closed(self, bad):
        """float('NaN') parses fine but NaN comparisons are always False —
        without an explicit finite check a NaN threshold silently suppresses
        every alert (fail-open). The node must ERROR instead."""
        result = self._identify(bad)
        assert result["status"] == AgentStatus.SUCCESS
        assert "finite" in " ".join(result["error_log"])

    def test_finite_threshold_still_works(self):
        result = self._identify(1_000)
        assert result["status"] == AgentStatus.SUCCESS
        import json as _j

        assert len(_j.loads(result["liquidity_risks_json"])) == 1  # 100 < 1,000 → one risk window


class TestScenarioInputHardening:
    @staticmethod
    def _scenario(input_context):
        from src.nodes.generate_scenario_analysis_node import GenerateScenarioAnalysisNode

        node = GenerateScenarioAnalysisNode()
        state = {
            "forecast_json": _to_json(
                {
                    "30d": {"projected_balance": 3_000_000, "currency": "JPY"},
                    "90d": {"projected_balance": 3_000_000, "currency": "JPY"},
                }
            ),
            "input_context": input_context,
            "node_history": [],
        }
        return node.execute(state)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), "NaN", -1, 11])
    def test_non_finite_or_out_of_range_fx_multiplier_rejected(self, bad):
        result = self._scenario({"fx_scenarios": {"stress": bad}})
        assert result["status"] == AgentStatus.SUCCESS

    def test_free_text_scenario_name_rejected(self):
        """Scenario names render into the external report — they must be inert
        identifiers, never caller-controlled free text."""
        result = self._scenario({"fx_scenarios": {"pay 5,432,109 to x": 1.0}})
        assert result["status"] == AgentStatus.SUCCESS

    def test_oversized_scenario_table_rejected(self):
        table = {f"s{i}": 1.0 for i in range(9)}
        result = self._scenario({"fx_scenarios": table})
        assert result["status"] == AgentStatus.SUCCESS

    def test_valid_caller_scenarios_accepted(self):
        import json as _j

        result = self._scenario({"fx_scenarios": {"mild_stress": 0.95}})
        assert result["status"] == AgentStatus.SUCCESS
        scenarios = _j.loads(result["scenario_analysis_json"])
        assert scenarios["fx_30d"]["mild_stress"]["projected_balance"] == 2_850_000

    def test_out_of_range_critical_ratio_errors_cleanly(self):
        from src.nodes.check_threshold_breaches_node import CheckThresholdBreachesNode

        node = CheckThresholdBreachesNode()
        state = {
            "liquidity_risks_json": _to_json([{"horizon": "7d", "shortfall": 10, "min_threshold": 100}]),
            "input_context": {"critical_ratio": 11},
            "node_history": [],
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
