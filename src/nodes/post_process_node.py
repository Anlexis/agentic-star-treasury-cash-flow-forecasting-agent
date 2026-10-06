"""AgentCore Platform v1.0"""

# post_process slot (outer backbone) — output gate + audit.
# Module-level _security_gate_output() enforces the output gate:
#   (1) no bulk raw treasury re-emission (verbatim blocked-field embedding), and
#   (2) the approved external-output schema's precision invariant — every large
#       monetary figure in the external report must sit on the rounding grid
#       (multiples of _EXTERNAL_ROUND_UNIT); off-grid values are values DERIVED
#       from protected treasury fields leaking at full precision, and are
#       redacted to the grid. This closes the gap where derived numbers
#       (projected balances, shortfalls) never match a blocked field verbatim.
# emit_trace_event() provides audit logging (J-SOX).
# The gate is a module-level function rather than an _extra_security_gate_*
# instance method, keeping the hook stateless.

import re
from typing import Any, ClassVar
from uuid import uuid4

from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

# Fields that must NEVER be included in external responses
_S3_BLOCKED_FIELDS = frozenset(
    {
        "historical_data_json",
        "threshold_breaches_json",
        "validated_input",
        "audit_trace_id",
    }
)

# Approved external precision (must match _EXTERNAL_ROUND_UNIT in
# src/graph/domain_workflow_graph.py — the report RENDERS on this grid, the
# gate ENFORCES it).
_EXTERNAL_ROUND_UNIT = 1000
# EXPLICIT output schema — monetary values are identified by FORM and by
# CURRENCY CONTEXT, never by magnitude:
#   form:    comma-grouped numbers (9,999 / 1,234,567 — the renderer formats
#            every monetary value with "{:,.0f}") and unformatted runs of 5+
#            digits (a rendering-regression leak);
#   context: any bare 1-4 digit number associated with a currency marker is
#            monetary even though short — SYMMETRICALLY: a 3-letter uppercase
#            code or a currency symbol (incl. fullwidth ￥ and 円/₩), before or
#            after the value, attached or separated by horizontal whitespace or
#            a single newline, signed or unsigned. The 3 letters must be a
#            STANDALONE word, so embedded acronyms stay structural ("STAR 2026"
#            and the "WAR" inside "[WARNING]" do not match). Any standalone
#            3-letter uppercase word counts as a code when it is SEPARATED from
#            the value: the caller's validated currency can be any ISO-FORMAT
#            code (LoadHistoricalDataNode accepts ^[A-Z]{3}$, not a fixed list),
#            and a false snap fails SAFE while a missed leak does not.
# Structural tokens stay untouched: horizons ("90d" — lowercase suffix),
# bare counts ("3 breach(es)"), years without currency adjacency
# ("year 2026", "in 2026"), and the identifiers this report renders (see the
# guard note below).
# ALL matched tokens, at ANY magnitude, must sit on the rounding grid;
# off-grid = a full-precision value derived from protected treasury fields,
# snapped + audited.
# Grammar (group-based; the delimiter is explicit rather than a lookbehind, so
# it can be a whitespace run). Every value accepts an optional explicit +/- sign.
# Branch order matters: currency-context branches first, then form-based.
_SYMBOL = r"[¥￥$€£円₩]"
# A standalone 3-letter uppercase word. Both sides are constrained, and the two
# mechanisms are NOT interchangeable:
#   - "\b" is the word-boundary test, and Python's \w covers CJK, so it is the
#     only thing that keeps a marker from binding across 万 / 円 / 条 / 日 / 人.
#   - "(?![A-Za-z])" is what makes the code standalone on the RIGHT while still
#     allowing a DIGIT to follow (the attached "JPY9999" leak form). A plain
#     trailing "\b" would reject that, so this is an addition, not a swap.
# Without the right-hand test, "WAR" inside "[WARNING]" and "STA" inside "STAR"
# read as currency markers.
_ANY_CODE = r"\b[A-Z]{3}(?![A-Za-z])"
# Marker AFTER the value: here a trailing "\b" IS correct — nothing legitimately
# follows a code in that position — and it is stricter than "(?![A-Za-z])",
# because it also refuses to bind across a CJK character.
_ANY_CODE_POST = r"[A-Z]{3}\b"

# Currency codes recognised when the marker is ATTACHED to the value with no
# whitespace between them. This is a CURRENCY list, not a list of identifiers to
# exclude: attached "<3 uppercase letters>-<digits>" is the shape this domain's
# own record references take (the template id FIN-C2-072, a subsidiary code
# "FIN-9999"), and by form alone it is indistinguishable from an attached
# negative amount ("JPY-9999"). Naming the currencies resolves the ambiguity in
# the direction that keeps record references intact, and a currency list is
# closed and stable where an identifier list never could be.
# SEPARATED markers are NOT restricted: "XBT 5432109" still snaps, so the
# caller's validated ^[A-Z]{3}$ currency — which the report always renders
# SEPARATED, as "{value} {currency}" — keeps the full fail-safe behaviour.
_ISO_CODE = (
    r"\b(?:JPY|USD|EUR|GBP|CNY|CNH|KRW|AUD|CAD|CHF|HKD|SGD|TWD|THB|INR|IDR|MYR|PHP|VND"
    r"|BRL|MXN|SEK|NOK|DKK|PLN|CZK|TRY|NZD|RUB|ZAR|SAR|AED|ILS)(?![A-Za-z])"
)
_ISO_CODE_POST = (
    r"(?:JPY|USD|EUR|GBP|CNY|CNH|KRW|AUD|CAD|CHF|HKD|SGD|TWD|THB|INR|IDR|MYR|PHP|VND"
    r"|BRL|MXN|SEK|NOK|DKK|PLN|CZK|TRY|NZD|RUB|ZAR|SAR|AED|ILS)\b"
)

# Delimiter between a currency marker and its value: horizontal whitespace and at
# most ONE newline — never a paragraph break. A plain `\s*` spans blank lines, so a
# 3-letter uppercase word ending a line would bind to the number that opens the next
# block and rewrite it ("  90d: 3,000,000 JPY\n\n2. Liquidity Alerts" -> "0. Liquidity
# Alerts" — measured on the pre-fix grammar, which is still what `main` carries).
# Every enumerated leak form (spaces, tabs, single newline, signed, symmetric,
# comma-grouped) still matches.
_GATE_DELIM = r"[ \t]*(?:\n[ \t]*)?"
# The same delimiter with at least one whitespace character present.
_GATE_DELIM_WS = r"(?:[ \t]+|[ \t]*\n[ \t]*)"

# Marker BEFORE the value: any standalone 3-letter uppercase word or a symbol
# when SEPARATED; only a named currency code or a symbol when ATTACHED.
_CURRENCY_MARKER = rf"(?:(?:{_ANY_CODE}|{_SYMBOL}){_GATE_DELIM_WS}|(?:{_ISO_CODE}|{_SYMBOL}){_GATE_DELIM})"
# Marker AFTER the value, same rule mirrored.
_CURRENCY_MARKER_POST = (
    rf"(?:{_GATE_DELIM_WS}(?:{_ANY_CODE_POST}|{_SYMBOL})|{_GATE_DELIM}(?:{_ISO_CODE_POST}|{_SYMBOL}))"
)

# A monetary amount may carry a DECIMAL part, and every value alternative absorbs
# it into the SAME token. Two separate things go wrong without that:
#   - the fraction of "9999.99999" is a standalone 5+-digit run in its own
#     right, so the form-based branch latches onto it and rewrites a percentage
#     into "9999.100,000" — a number the document never contained;
#   - in currency context the integer part snaps while the fraction dangles
#     ("JPY 1234.56" -> "JPY 1,000.56"), producing a value that is neither the
#     true figure nor on the grid.
# The fix is NOT to exempt decimals from the grid. An off-grid amount in
# explicit currency context still snaps — as ONE number ("JPY 1234.56" ->
# "JPY 1,000") — while a ratio, percentage, coverage figure or version
# reference, which was never a monetary token, is left byte-identical.
#
# The `(?!\.\d)` arm is what makes absorption stick. A plain `(?:\.\d+)?` lets
# the engine backtrack out of the fraction and re-match the integer part alone
# whenever the text right after the fraction fails the token's trailing guard —
# "JPY 1234.56m" would go back to matching "JPY 1234" and the dangling-fraction
# bug returns. Either the fraction is taken whole, or there is none there.
_VAL_FRACTION = r"(?:\.\d+|(?!\.\d))"

# A short (1-4 digit) value may never be the PREFIX of a comma-grouped one.
# The alternation lists the grouped form first, but "first" is not "only": when
# the grouped form matches and then the TRAILING GUARD rejects the token, the
# engine backtracks into the short form and matches the leading digit group
# alone — "JPY 1,000-1" comes out "JPY 0,000-1", which is the very mangling the
# ordering exists to prevent. a peer template has the same latent hole via its "\b"
# ("JPY 1,000.0f" -> "JPY 0,000.0f"); a trailing guard just makes it easier to
# reach. Refusing the prefix outright removes the fallback path entirely.
_SHORT_VAL = r"[+-]?\d{1,4}(?!,\d)"

# Identifier guards, widened to THIS template's render alphabet. The generic
# guard class is [A-Za-z0-9-]; that is not enough here, because the identifier
# this report actually renders is joined by `_`:
#
#   fx_scenarios names   caller-supplied, validated only as ^[a-z0-9_]{1,32}$
#                        (GenerateScenarioAnalysisNode) and rendered VERBATIM as
#                        the label of every FX scenario line. Without `_` in the
#                        class, "jpy_1234567_stress" comes back from POST /invoke
#                        as "jpy_1,235,000_stress" and "scenario_90210" as
#                        "scenario_90,000" — measured, and the reason this gate
#                        needed fixing at all.
#   FIN-C2-072           the report header's template id, and the "-"-joined
#                        subsidiary / record shapes of this domain.
#   7d / 30d / 90d       horizon tokens: the trailing guard is what keeps a
#                        horizon from being read as a bare value.
#
# `.` and `,` are in the LEADING guard only — they stop a match from ENTERING a
# number part-way through ("0.123456" can be entered neither at "123456" nor at
# "23456"; "1,000600" cannot be entered at "000600" and re-emitted as
# "1,1,000"). In the trailing guard "." would let an amount that ends a sentence
# escape the grid ("The book totals JPY 9999.").
# `:` and `=` are deliberately NOT in either class: this report separates every
# label from its value with whitespace, so admitting them would only create a
# way for a future "Shortfall:9999JPY" rendering regression to bypass the grid.
#
# ⚠️ These guards are an ADDITION to the word-boundary tests, never a
# replacement for them. An ASCII character class cannot do \b's job: Python's
# \w covers CJK, so 万 / 円 / 条 / 日 / 人 are word characters that "(?![A-Za-z0-9_-])"
# happily admits. Dropping \b from the marker-and-value token made the
# statutory form "（3000万円+600万円×…）" match as a 円-marker plus "+600" and
# render "+1,000万円" — the gate rewriting a quoted statute. Measured on
# a peer template and reproduced here before this line was written.
_LEAD_GUARD = r"(?<![A-Za-z0-9_.,\-])"
_TRAIL_GUARD = r"(?![A-Za-z0-9_\-])"

_NUM_TOKEN_RE = re.compile(
    _LEAD_GUARD
    # marker THEN value: "JPY 9999", "JPY  -9999", "JPY\t9999", "¥9999", "USD\n+9999".
    # The value alternatives accept a comma-grouped form FIRST: the regex is
    # leftmost-first, so without it "JPY 1,234" would match as marker + "1"
    # (mangling the number on the snap into "JPY 0,234" — what `main` still
    # does) instead of as the whole grouped value. An on-grid "JPY 1,000" must
    # stay byte-identical, and an off-grid "JPY 1,234" must snap as 1234, not 1.
    + rf"(?:(?P<pre>{_CURRENCY_MARKER})"
    rf"(?P<val_after>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|{_SHORT_VAL}{_VAL_FRACTION})\b"
    # value THEN marker: "9999 JPY", "-9999\tJPY", "9999円", "+9999  $"
    rf"|(?P<val_before>{_SHORT_VAL}{_VAL_FRACTION})(?P<post>{_CURRENCY_MARKER_POST})"
    # form-based, standalone at any magnitude: comma-grouped or 5+-digit runs
    rf"|(?P<val_form>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{5,}}{_VAL_FRACTION}))" + _TRAIL_GUARD
)


def _enforce_precision(result: str) -> tuple[str, int]:
    """Snap every monetary-form token onto the approved external grid.

    Returns (sanitised_result, redaction_count). A redaction means a value
    derived from protected treasury data reached the external surface at full
    precision — the gate rounds it onto the approved grid. The currency
    marker, the original delimiter whitespace, and the explicit sign of the
    original token are all preserved on the snapped replacement.
    """
    redactions = 0

    def _snap(match: re.Match[str]) -> str:
        nonlocal redactions
        pre = match.group("pre") or ""
        post = match.group("post") or ""
        token = match.group("val_after") or match.group("val_before") or match.group("val_form")
        # float(), not int(): the token may carry a decimal fraction, and the
        # WHOLE amount — not just its integer part — is what sits on the grid.
        value = float(token.replace(",", ""))  # float() understands leading +/-
        if value % _EXTERNAL_ROUND_UNIT == 0:
            return match.group(0)
        redactions += 1
        snapped = round(value / _EXTERNAL_ROUND_UNIT) * _EXTERNAL_ROUND_UNIT
        plus = "+" if token.startswith("+") and snapped >= 0 else ""
        return f"{pre}{plus}{snapped:,d}{post}"

    return _NUM_TOKEN_RE.sub(_snap, result), redactions


def _security_gate_output(result: str, state: dict[str, Any]) -> str:
    """Enforce the external-output boundary on the formatted report.

    Module-level function (NOT an instance method). Two independent layers:
    verbatim blocked-field embedding is replaced with [REDACTED]; then every
    large numeric token is snapped onto the approved precision grid
    (_enforce_precision). Returns the sanitised result.
    """
    blocked_snippets = []
    for field in _S3_BLOCKED_FIELDS:
        val = state.get(field)
        if val and isinstance(val, str) and len(val) > 10 and val in result:
            blocked_snippets.append(field)

    sanitised = result
    if blocked_snippets:
        for field in blocked_snippets:
            val = state.get(field, "")
            if val and isinstance(val, str):
                sanitised = sanitised.replace(val, "[REDACTED]")

    sanitised, precision_redactions = _enforce_precision(sanitised)
    if precision_redactions:
        emit_trace_event(
            "fin_c2_072.precision_redaction",
            {
                "template_id": "FIN-C2-072",
                "redaction_count": precision_redactions,
                "session_id": state.get("session_id", ""),
            },
            state,
        )

    return sanitised


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class SecurityGateOutputNode(FunctionNode):
    """Output gate + audit.

    Applies _security_gate_output() and emits an audit trace
    before forwarding the formatted forecast report to the caller.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "result": message,
                "formatted_output": message,
            }
        result = state.get("result", "")
        if not isinstance(result, str):
            result = str(result) if result else ""

        # Gate output — no raw treasury data re-emission
        sanitised = _security_gate_output(result, state)

        # Audit log — financial forecast access record (J-SOX)
        trace_id = state.get("audit_trace_id") or str(uuid4())
        audit_payload = {
            "template_id": "FIN-C2-072",
            "event": "forecast_output_gate",
            "breach_detected": state.get("breach_detected", False),
            "alert_generated": bool(state.get("alert_payload_json")),
            "session_id": state.get("session_id", ""),
            "trace_id": trace_id,
        }
        emit_trace_event("fin_c2_072.output_gate", audit_payload, state)

        return {
            "formatted_output": sanitised,
            "audit_trace_id": trace_id,
            "status": AgentStatus.SUCCESS.value,
        }
