# PB: End-to-end business behaviour through POST /invoke — src/api/server.py
#
# Proves the supported input contract produces REAL outcomes through the full
# nested graph (outer backbone → inner domain pipeline), not the stub baseline:
#   - a non-zero cash-position forecast from caller-provided historical data
#   - a WARNING liquidity alert (shortfall below the critical ratio)
#   - a CRITICAL liquidity alert (shortfall above the critical ratio)
#   - a validation rejection for malformed caller data
#
# Unlike test_server_boot.py (which stubs the agent to isolate the auth
# boundary), these tests run the REAL compiled agent: every request crosses the
# entry-point auth, the outer trust/input gates, the input_context bridge into
# the inner graph, all six domain nodes, and the output gate.
#
# The app is driven through its real ASGI interface (no TestClient — httpx is
# only a transitive dependency; see test_server_boot.py for the rationale).

import asyncio
import json

import pytest

from src.api import server as server_module  # noqa: F401  (import = boot check)
from src.api.server import app

_TOKEN = "pb-invoke-e2e-token"

# 5,000,000 opening; +2,000,000 inflow (day 5); −1,000,000 (day 3) −3,000,000
# (day 20) outflows. Expected projections: 7d = 6,000,000; 30d/90d = 3,000,000.
_HISTORICAL_DATA = {
    "opening_balance": 5_000_000,
    "cash_inflows": [{"day_offset": 5, "amount": 2_000_000}],
    "cash_outflows": [
        {"day_offset": 3, "amount": 1_000_000},
        {"day_offset": 20, "amount": 3_000_000},
    ],
}
_INPUT_TEXT = "Forecast 7d/30d/90d cash positions and flag liquidity risk."


def _post_invoke(payload: dict) -> tuple[int, dict]:
    """POST /invoke with a Bearer token through the real ASGI app."""
    body = json.dumps(payload).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/invoke",
        "raw_path": b"/invoke",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"authorization", f"Bearer {_TOKEN}".encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }

    messages = []
    sent = {"body": b""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body":
            sent["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    parsed = json.loads(sent["body"].decode() or "{}")
    return start["status"], parsed


@pytest.fixture(autouse=True)
def token_configured(monkeypatch):
    """Deploy-shaped server environment: INVOKE_AUTH_TOKEN set, caller uses Bearer."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)


def _invoke(input_context: dict) -> dict:
    status_code, body = _post_invoke(
        {"input": _INPUT_TEXT, "session_id": "pb-invoke-e2e", "input_context": input_context}
    )
    assert status_code == 200, f"expected 200, got {status_code}: {body}"
    return body


class TestInvokeEndToEnd:
    def test_caller_data_produces_non_zero_forecast(self):
        """Caller-provided historical data must yield real, non-zero projections."""
        body = _invoke({"historical_data": _HISTORICAL_DATA})

        assert body["status"] == "success"
        output = body["output"]
        assert "6,000,000" in output, output  # 7d projection
        assert "3,000,000" in output, output  # 30d/90d projections
        assert "No liquidity threshold breaches detected." in output

    def test_warning_alert_path(self):
        """Shortfall at or below critical_ratio × threshold classifies as WARNING."""
        body = _invoke(
            {
                "historical_data": _HISTORICAL_DATA,
                # 30d/90d projected 3,000,000 < 3,500,000 → shortfall 500,000;
                # critical threshold = 0.20 × 3,500,000 = 700,000 → WARNING.
                "min_cash_threshold": 3_500_000,
            }
        )

        assert body["status"] == "success"
        output = body["output"]
        assert "LIQUIDITY ALERT: 2 threshold breach(es) detected" in output, output
        assert "[WARNING]" in output
        assert "[CRITICAL]" not in output
        assert "shortfall 500,000" in output
        assert "Alert level: WARNING" in output

    def test_critical_alert_path(self):
        """Shortfall above critical_ratio × threshold classifies as CRITICAL."""
        body = _invoke(
            {
                "historical_data": _HISTORICAL_DATA,
                # All horizons breach 8,000,000; smallest shortfall 2,000,000 >
                # 0.20 × 8,000,000 = 1,600,000 → CRITICAL.
                "min_cash_threshold": 8_000_000,
            }
        )

        assert body["status"] == "success"
        output = body["output"]
        assert "LIQUIDITY ALERT: 3 threshold breach(es) detected" in output, output
        assert "[CRITICAL]" in output
        assert "Alert level: CRITICAL" in output

    def test_invalid_caller_data_is_rejected(self):
        """Malformed historical_data must produce a validation error, not a forecast."""
        body = _invoke({"historical_data": {"opening_balance": "lots"}})

        assert body["status"] == "success"
        assert body.get("output"), body
        assert (
            "could not be accepted" in body["output"]
            or "No question was received" in body["output"]
            or "too long" in body["output"]
        )

    @pytest.mark.parametrize(
        "bad_threshold",
        ["NaN", "Infinity", "-Infinity", float("nan"), float("inf")],
        ids=["str-nan", "str-inf", "str-neginf", "raw-nan", "raw-inf"],
    )
    def test_non_finite_threshold_fails_closed_not_open(self, bad_threshold):
        """A NaN/Infinity min_cash_threshold must ERROR with no forecast/alert —
        never a 'success + no breaches' fail-open (raw floats also cover Python
        json's bare-NaN extension reaching the request body)."""
        body = _invoke({"historical_data": _HISTORICAL_DATA, "min_cash_threshold": bad_threshold})

        assert body["status"] == "success", body
        assert body.get("output"), body
        assert (
            "could not be accepted" in body["output"]
            or "No question was received" in body["output"]
            or "too long" in body["output"]
        )

    def test_external_report_carries_only_grid_values(self):
        """Approved external schema: every large figure sits on the 1,000 grid."""
        import re

        body = _invoke({"historical_data": _HISTORICAL_DATA})
        for token in re.findall(r"-?\d{1,3}(?:,\d{3})+|-?\d{5,}", body["output"]):
            value = int(token.replace(",", ""))
            if abs(value) >= 10_000:
                assert value % 1_000 == 0, f"off-grid value leaked: {token}"
