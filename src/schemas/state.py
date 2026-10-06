"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  dict/list fields are stored as Optional[str]
# (JSON-encoded).  Use _to_json() / _from_json() helpers.

from __future__ import annotations

import json as _json
import math
from typing import Any, Optional

from framework.schemas.agent_state import AgentState


def _finite_in_range(value: Any, lo: float, hi: float) -> Optional[float]:
    """Parse a caller-controlled numeric: FINITE float within [lo, hi], else None.

    Rejects bools, non-numerics, and — critically — non-finite values: float()
    happily parses "NaN"/"Infinity" (and Python's json accepts bare NaN in
    request bodies), and IEEE NaN comparisons are always False, which turns a
    threshold check into silent FAIL-OPEN. Every caller-supplied number must
    come through here (or an equivalent explicit finite check).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or not lo <= parsed <= hi:
        return None
    return parsed


def _to_json(obj: Any) -> Optional[str]:
    """Serialize a dict or list to a JSON string (None-safe)."""
    if obj is None:
        return None
    return _json.dumps(obj, ensure_ascii=False, default=str)


def _from_json(s: Optional[str]) -> Any:
    """Deserialize a JSON string to a dict or list (None-safe)."""
    if not s:
        return None
    return _json.loads(s)


class State(AgentState):
    """FIN-C2-072 agent state.

    All dict/list fields are stored as Optional[str] (JSON-encoded).
    Use _to_json() / _from_json() to serialize / deserialize before storing.

    Field naming: suffix *_json denotes a JSON-encoded dict/list.
    """

    # ── Pre-process ──────────────────────────────────────────────────────────
    validated_input: Optional[str]

    # ── Domain pipeline (all JSON-serialized) ────────────────────
    # LoadHistoricalData — normalized AP/AR + cash flow data
    historical_data_json: Optional[str]

    # ForecastCashPositions — 7d/30d/90d position forecasts
    forecast_json: Optional[str]

    # IdentifyLiquidityRisk — risk windows below minimum threshold
    liquidity_risks_json: Optional[str]

    # CheckThresholdBreaches — breach severity per horizon (warning / critical)
    threshold_breaches_json: Optional[str]

    # GenerateScenarioAnalysis — FX/IR sensitivity scenarios
    scenario_analysis_json: Optional[str]

    # AlertIfRequired — alert payload (populated only when breach_detected=True)
    alert_payload_json: Optional[str]

    # ── Scalars ──────────────────────────────────────────────────────────────
    breach_detected: Optional[bool]  # True when any threshold breach found
    audit_trace_id: Optional[str]  # Audit correlation ID

    # ── Post-process ─────────────────────────────────────────────────────────
    formatted_output: Optional[str]
    error_code: Optional[str]
