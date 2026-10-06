"""Precision gate — decimal absorption and domain-identifier integrity.

Four defects were measured in the grammar this template shipped with, and all
four are pinned here:

1. DECIMAL FRACTION CORRUPTION — the fraction of a decimal is a standalone
   5+-digit run in its own right, so the form-based branch rewrote it
   ("8.512345" -> "8.512,000", "9999.99999%" -> "9999.100,000%"), and in
   currency context the integer part snapped while the fraction dangled
   ("JPY 1234.56" -> "JPY 1,000.56" — neither the true figure nor a grid value).
2. BACKTRACKING — writing the absorption as an optional "(?:\\.\\d+)?" lets the
   engine back out of the fraction and re-match the integer whenever the text
   right after the fraction fails the trailing guard, so "JPY 1234.56m" came
   back out as "JPY 1,000.56m". The absorption is written "(?:\\.\\d+|(?!\\.\\d))"
   — take the fraction whole, or assert there is not one — which cannot
   backtrack.
3. DELIMITER — a plain "\\s*" between marker and value spans a paragraph break,
   so a 3-letter uppercase word ending a line binds to the number opening the
   next block: "  90d: 3,000,000 JPY\\n\\n2. Liquidity Alerts" came back as
   "0. Liquidity Alerts". The gate rewrote the document's own structure.
4. WORD BOUNDARIES vs GUARD CLASSES — an ASCII guard class is NOT a substitute
   for "\\b". Python's \\w covers CJK, so 万 / 円 / 条 / 日 / 人 are word
   characters that "(?![A-Za-z0-9_-])" admits and "\\b" rejects. Swapping one for the other
   made the statutory form "（3000万円+600万円×…）" match as a 円-marker plus "+600"
   and render "+1,000万円" — the gate rewriting a quoted statute, on a line no
   caller input touches. The grammar carries BOTH.

5. IDENTIFIER COLLISION — the grammar read any digit run as a value regardless
   of what it was embedded in. This one was LIVE on this template: FX scenario
   names are caller-supplied, validated only as ^[a-z0-9_]{1,32}$, and rendered
   verbatim, so POST /invoke returned "jpy_1,235,000_stress" for a caller's
   "jpy_1234567_stress" and "scenario_90,000" for "scenario_90210".

The grid itself is NOT relaxed: an off-grid amount in explicit currency context
still snaps — as ONE number.
"""

import pytest

from src.graph.domain_workflow_graph import _build_forecast_report
from src.nodes.post_process_node import _enforce_precision, _security_gate_output
from src.schemas.state import _to_json


def _gate(text: str) -> str:
    out, _ = _enforce_precision(text)
    return out


class TestDecimalsSurviveByteIdentical:
    """Non-monetary decimals are not monetary tokens and must not be rewritten."""

    @pytest.mark.parametrize(
        "text",
        [
            "8.512345",
            "9999.99999%",
            "ratio 0.123456",
            "coverage ratio 1.23456",
            "fx_multiplier 0.856789",
            "critical_ratio 0.20000",
            "confidence interval 0.900001 to 1.100001",
            "horizon 90d, 7.512345 days elapsed",
        ],
    )
    def test_decimal_is_byte_identical(self, text: str) -> None:
        assert _gate(text) == text


class TestOffGridDecimalAmountsSnapAsOneNumber:
    """The grid is not relaxed for decimals — the WHOLE amount snaps."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("JPY 1234.56", "JPY 1,000"),
            ("JPY -1234.56", "JPY -1,000"),
            ("1234.56 JPY", "1,000 JPY"),
            ("¥1234.56", "¥1,000"),
            # An amount that ENDS A SENTENCE must not escape the grid — this is
            # why "." sits in the leading guard only.
            ("The book totals JPY 9999.", "The book totals JPY 10,000."),
        ],
    )
    def test_off_grid_decimal_amount_snaps_whole(self, text: str, expected: str) -> None:
        assert _gate(text) == expected

    def test_no_dangling_fraction_is_ever_left_behind(self) -> None:
        """The pre-fix grammar produced "JPY 1,000.56" — on-grid integer, live
        fraction. Assert the shape, not just the value."""
        out = _gate("JPY 1234.56")
        assert ".56" not in out, out
        assert out == "JPY 1,000"


class TestAbsorptionDoesNotBacktrack:
    """A decimal followed by a letter is the case an optional fraction misses."""

    @pytest.mark.parametrize("text", ["JPY 1234.56m", "JPY 1234.56bn", "1234.56m JPY", "¥1234.56x"])
    def test_decimal_with_trailing_suffix_is_byte_identical(self, text: str) -> None:
        assert _gate(text) == text


class TestGridControls:
    """The on-grid / off-grid contract, unchanged."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("JPY 1,000", "JPY 1,000"),  # byte-identical
            ("JPY 1,234", "JPY 1,000"),
            ("USD 1,234,567", "USD 1,235,000"),
            ("1,234 JPY", "1,000 JPY"),
            ("Forecast: 9999 JPY", "Forecast: 10,000 JPY"),
        ],
    )
    def test_grid_contract(self, text: str, expected: str) -> None:
        assert _gate(text) == expected


class TestDomainIdentifiersSurvive:
    """Every identifier THIS template renders, driven through the gate.

    These are the repo's own render alphabet, not a generic list:
      - FX scenario names   caller-supplied ^[a-z0-9_]{1,32}$, rendered verbatim
      - FIN-C2-072          the report header's template id
      - FIN-…-…             the "-"-joined subsidiary / record shapes
      - 7d / 30d / 90d      horizon tokens
    """

    @pytest.mark.parametrize(
        "text",
        [
            "  jpy_1234567_stress: 7,000,000",
            "  scenario_90210: 9,000,000",
            "  plan_2026: 3,030,000",
            "  fx_100000_downside: 1,000,000",
            "=== Corporate Treasury Cash Flow Forecast — FIN-C2-072 ===",
            "FIN-C2-07234 build",
            "subsidiary FIN-SUB-20260831-001",
            "SKF-6205",
            "horizon 90d, 3 breach(es), year 2026",
            "the AGENTIC STAR 2026 rollout",
            "trace 89c5a489-eab0-486a-9af0-18aa969e4c0c",
        ],
    )
    def test_identifier_is_byte_identical(self, text: str) -> None:
        assert _gate(text) == text


class TestFailSafeCurrencyContextPreserved:
    """Restricting the ATTACHED form must not weaken the SEPARATED one.

    LoadHistoricalDataNode accepts any ^[A-Z]{3}$ currency, and the report always
    renders it SEPARATED ("{value} {currency}"), so an exotic-but-valid code must
    still put its neighbour on the grid. Only the attached "<3 letters>-<digits>"
    shape — which is how this domain writes record references — is restricted to
    named currencies.
    """

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("XBT 9999", "XBT 10,000"),  # separated, non-ISO code — still snaps
            ("9999 XBT", "10,000 XBT"),
            ("XBT 5432109", "XBT 5,432,000"),
            ("5,432,109 XBT", "5,432,000 XBT"),
            ("JPY-9999", "-10,000"),  # attached NAMED currency — still snaps
            ("USD-9999", "-10,000"),
            ("JPY+9999", "+10,000"),
            ("JPY9999", "10,000"),
        ],
    )
    def test_currency_context_still_snaps(self, text: str, expected: str) -> None:
        assert expected in _gate(text), _gate(text)

    @pytest.mark.parametrize("text", ["SKF-6205", "FIN-6205", "ABC-1234"])
    def test_attached_non_currency_code_is_left_alone(self, text: str) -> None:
        assert _gate(text) == text


class TestDelimiterDoesNotSpanAParagraphBreak:
    """The gate must not rewrite the document's own structure.

    Left-hand sides are this template's real line shapes: a forecast line ending
    in the currency code, and the alert-level line.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "  90d: 3,000,000 JPY\n\n2. Liquidity Alerts",
            "Currency: JPY\n\n3. Cash Position Forecasts",
            "  [WARNING] 90d — shortfall 500,000 JPY\n\n4. FX Scenario Analysis",
            "Alert level: CRITICAL\n\n2. Scenario Analysis",
        ],
    )
    def test_blank_line_is_not_a_delimiter(self, text: str) -> None:
        assert _gate(text) == text

    def test_single_newline_delimiter_still_catches_the_leak(self) -> None:
        """One newline stays inside the delimiter on purpose — a wrapped
        "USD\\n+9999" render is a real leak form."""
        assert _gate("USD\n+9999") == "USD\n+10,000"

    def test_report_skeleton_survives_its_own_line_adjacencies(self) -> None:
        """The shipped skeleton puts a horizon line right after a currency-code
        line; neither the horizon nor the next block may be renumbered."""
        text = "  7d: 6,000,000 JPY\n  30d: 3,000,000 JPY\n  90d: 3,000,000 JPY"
        assert _gate(text) == text


class TestRenderedReportPassesThroughUnchanged:
    """End-to-end: the real report, with caller-supplied scenario names, must
    leave the gate byte-identical — this is the defect that was LIVE."""

    @staticmethod
    def _state() -> dict:
        return {
            "forecast_json": _to_json(
                {
                    "7d": {"projected_balance": 6_000_000, "currency": "JPY"},
                    "30d": {"projected_balance": 3_000_000, "currency": "JPY"},
                    "90d": {"projected_balance": 3_000_000, "currency": "JPY"},
                }
            ),
            "threshold_breaches_json": _to_json(
                [{"horizon": "30d", "severity": "warning", "shortfall": 500_000, "currency": "JPY"}]
            ),
            "breach_detected": True,
            "alert_payload_json": _to_json({"alert_level": "WARNING"}),
            "scenario_analysis_json": _to_json(
                {
                    "fx_30d": {
                        "base": {"projected_balance": 3_000_000},
                        "jpy_1234567_stress": {"projected_balance": 2_550_000},
                        "scenario_90210": {"projected_balance": 3_150_000},
                    }
                }
            ),
            "session_id": "t",
        }

    def test_full_report_is_byte_identical_through_the_gate(self) -> None:
        state = self._state()
        report = _build_forecast_report(state)
        assert "jpy_1234567_stress" in report
        assert "scenario_90210" in report
        assert _security_gate_output(report, state) == report

    def test_off_grid_regression_in_the_same_report_still_snaps(self) -> None:
        """The identifier guards must not have disabled the gate itself."""
        state = self._state()
        report = _build_forecast_report(state) + "\n  raw 30d position: 5,432,109 JPY"
        sanitised = _security_gate_output(report, state)
        assert "5,432,109" not in sanitised
        assert "5,432,000" in sanitised
        assert "jpy_1234567_stress" in sanitised
        assert "scenario_90210" in sanitised


class TestWordBoundariesAreNotReplacedByGuardClasses:
    """CJK regression: 万 / 円 / 条 / 日 / 人 are \\w characters.

    An ASCII-only guard class admits every one of them, so dropping "\\b" in
    favour of "(?![A-Za-z0-9_-])" lets a currency marker bind across a CJK
    character. Measured on a peer template: the always-rendered statutory line
    "（3000万円+600万円×法定相続人の数）" came back as "+1,000万円" — the gate
    misquoting a statute on every invoke, on a line no caller input reaches.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "（3000万円+600万円×法定相続人の数）",
            "基礎控除 3000万円+600万円",
            "残高 600万円",
            "予測 1234万円×3人",
            "第90条 600万円",
            "3000万円-600万円",
            "3000万円 600万円",
        ],
    )
    def test_marker_does_not_bind_across_a_cjk_character(self, text: str) -> None:
        assert _gate(text) == text

    @pytest.mark.parametrize(
        "text, expected",
        [
            # The CJK symbols in the marker class must still work as markers.
            ("9999円", "10,000円"),
            ("￥9999", "￥10,000"),
            ("9999₩", "10,000₩"),
            ("手数料 9999円を差し引く", "手数料 10,000円を差し引く"),
            ("残高は 5432109 円", "残高は 5,432,000 円"),
            ("3000円", "3000円"),  # on grid — byte-identical
        ],
    )
    def test_cjk_currency_context_still_snaps(self, text: str, expected: str) -> None:
        assert _gate(text) == expected


class TestShortValueIsNeverThePrefixOfAGroupedOne:
    """The trailing guard must not push the engine into the short alternative.

    The alternation lists the comma-grouped form first, but "first" is not
    "only": when the grouped form matches and the TRAILING GUARD then rejects
    the token, a backtrack into the 1-4 digit form matches the leading group
    alone and the snap mangles the number — "JPY 1,000-1" -> "JPY 0,000-1",
    exactly the failure the ordering exists to prevent (and the one `main`
    still has unconditionally). `_SHORT_VAL` refuses the prefix outright.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "JPY 1,000-1",
            "JPY 1,000-001",
            "JPY 1,000.0f",
            "JPY1,000-1",
            "¥1,000-1",
            "￥1,000-001",
            "XBT 1,000-001",
            "JPY\n1,000-1",
            "1,000600",
            "901,0002026",
            "1,0009999",
        ],
    )
    def test_grouped_value_is_never_mangled_into_its_first_group(self, text: str) -> None:
        out = _gate(text)
        assert out == text, out
        assert "0,000" not in out or "0,000" in text
