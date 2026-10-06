# docs/03_test_spec.md — FIN-C2-072 Test Specification

## Template Metadata

| Field | Value |
|---|---|
| Template ID | FIN-C2-072 |
| Category | Cat 2 |
| Test suite | tests/unit/test_agent.py + tests/proof_of_boundary/ |
| Coverage target | Domain-node behavior + framework compliance |

## Test Strategy

- **Unit tests** (`tests/unit/test_agent.py`): per-node `execute()` behavior
- **Proof-of-Boundary** (`tests/proof_of_boundary/`): framework compliance (import isolation, state safety, invoke order)
- Audit emits patched at node-module level (never stubbed in `sys.modules`)
- All tests green under the installed SDK (`agenticstar-agentcore==1.0.1`)

## Framework Compliance Tests (PB)

| TC-ID | Test | Expected Result |
|---|---|---|
| PB-2 | State safety: no credential field names in `State` | 0 violations |
| PB-4 | Import isolation: no platform-internal (`agenticstar`) import in `src/` | 0 violations |
| PB-5 | State safety: no Pydantic `BaseModel` in State annotations | 0 violations |
| PB-6a | Per-node `__call__` invoke order: trust-gate→node_start→input-gate→execute→output-gate→node_complete | All 8 domain nodes pass |
| PB-6b | Full graph `.invoke()` backbone order: initialize→pre_process→main→post_process→finalize | node_history order correct |
| PB-7 | HITL interrupt propagation (not enabled for this template) | SKIP (documented stub) |

## Unit Tests

### Input Validation (ValidateInputNode)

| TC-ID | Test | Expected Result |
|---|---|---|
| TC-IN-01 | Valid treasury forecast request | SUCCESS + validated_input set |
| TC-IN-02 | Empty user_input | ERROR + "empty" in error_log |
| TC-IN-03 | Whitespace-only input | ERROR |
| TC-IN-04 | Prompt-injection pattern (`ignore all previous instructions`) | ERROR; request refused before domain nodes |
| TC-IN-05 | Leading/trailing whitespace stripped | validated_input stripped |
| TC-IN-06 | Trust level = VERIFIED_EXTERNAL | Confirmed |

### Historical Data Loading (LoadHistoricalDataNode)

| TC-ID | Test | Expected Result |
|---|---|---|
| TC-LD-01 | Default input_context | SUCCESS + historical_data_json populated |
| TC-LD-02 | subsidiary_id + currency from input_context | Data reflects context values |
| TC-LD-03 | Trust level = INTERNAL | Confirmed |
| TC-LD-04 | Audit emit: payload contains subsidiary_id (args[1]) | emit called; payload[subsidiary_id] correct |

### Cash Position Forecasting (ForecastCashPositionsNode)

| TC-ID | Test | Expected Result |
|---|---|---|
| TC-FC-01 | Valid historical_data_json | SUCCESS + forecast_json with 7d/30d/90d keys |
| TC-FC-02 | Missing historical_data_json | ERROR |
| TC-FC-03 | Trust level = INTERNAL | Confirmed |

### Liquidity Risk Identification (IdentifyLiquidityRiskNode)

| TC-ID | Test | Expected Result |
|---|---|---|
| TC-LR-01 | Balance above threshold | liquidity_risks_json = [] |
| TC-LR-02 | Balance below threshold | risk_windows populated; shortfall correct |
| TC-LR-03 | Missing forecast_json | ERROR |

### Threshold Breach Classification (CheckThresholdBreachesNode)

| TC-ID | Test | Expected Result |
|---|---|---|
| TC-TB-01 | No risk windows | breach_detected=False; threshold_breaches_json=[] |
| TC-TB-02 | Small shortfall (< critical_ratio × threshold) | severity=warning; breach_detected=True |
| TC-TB-03 | Large shortfall (> critical_ratio × threshold) | severity=critical |
| TC-TB-04 | Missing liquidity_risks_json | ERROR |

### Scenario Analysis (GenerateScenarioAnalysisNode)

| TC-ID | Test | Expected Result |
|---|---|---|
| TC-SA-01 | Valid forecast_json | SUCCESS + fx_30d, fx_90d, ir_sensitivity in output |
| TC-SA-02 | Stress scenario balance < base | Confirmed (fx_multiplier < 1.0) |
| TC-SA-03 | Missing forecast_json | ERROR |

### Alert Generation (AlertIfRequiredNode)

| TC-ID | Test | Expected Result |
|---|---|---|
| TC-AL-01 | breach_detected=False | alert_payload_json=None |
| TC-AL-02 | breach_detected=True, severity=warning | alert_payload_json set; alert_level=WARNING |
| TC-AL-03 | breach_detected=True, any severity=critical | alert_level=CRITICAL |

### Output Gate (SecurityGateOutputNode)

| TC-ID | Test | Expected Result |
|---|---|---|
| TC-SG-01 | Clean forecast output | SUCCESS; formatted_output preserved |
| TC-SG-02 | Output contains raw historical_data_json | Output gate redacts blocked content (`[REDACTED]`) |
| TC-SG-03 | Empty result | SUCCESS; empty formatted_output |
| TC-SG-04 | Audit: payload (args[1]) contains template_id=FIN-C2-072 | emit_trace_event called; payload verified |
| TC-SG-05 | Trust level = ANONYMOUS | Confirmed |

### Precision Gate — token boundaries (`tests/unit/test_gate_precision_identifiers.py`)

| TC-ID | Test | Expected Result |
|---|---|---|
| TC-PG-01 | Non-monetary decimals through the gate (`8.512345`, `9999.99999%`, `ratio 0.123456`, `fx_multiplier 0.856789`) | Byte-identical — a fraction is not a standalone value |
| TC-PG-02 | Off-grid decimal amount in currency context (`JPY 1234.56`, `1234.56 JPY`, `¥1234.56`) | Snaps as ONE number (`JPY 1,000`); no dangling fraction |
| TC-PG-03 | Decimal followed by a letter (`JPY 1234.56m`) | Byte-identical — the absorption must not backtrack |
| TC-PG-04 | Grid controls (`JPY 1,000` / `JPY 1,234` / `USD 1,234,567`) | Byte-identical / `JPY 1,000` / `USD 1,235,000` |
| TC-PG-05 | Domain identifiers (FX scenario names `jpy_1234567_stress`, `scenario_90210`; `FIN-C2-072`; `FIN-SUB-20260831-001`; `90d`; UUID trace id) | Byte-identical |
| TC-PG-06 | Fail-safe currency context preserved — separated non-listed code (`XBT 9999`, `XBT 5432109`) | Still snaps onto the grid |
| TC-PG-07 | Attached `<3 letters>-<digits>` — named currency vs record reference (`JPY-9999` vs `SKF-6205`) | `-10,000` / byte-identical |
| TC-PG-08 | Blank line between a currency-code line and a numbered heading | Heading is NOT renumbered; `USD\n+9999` (one newline) still snaps |
| TC-PG-10 | CJK statutory forms through the gate (`（3000万円+600万円×法定相続人の数）`, `第90条 600万円`, `予測 1234万円×3人`) | Byte-identical — a marker must not bind across a CJK word character |
| TC-PG-11 | CJK currency context (`9999円`, `￥9999`, `残高は 5432109 円`) | Still snaps onto the grid |
| TC-PG-12 | Grouped value followed by an identifier character (`JPY 1,000-1`, `JPY 1,000.0f`, `1,000600`) | Byte-identical — never mangled into its first digit group |
| TC-PG-09 | Full rendered report carrying caller scenario names, through `_security_gate_output` | Byte-identical; an off-grid figure added to the same report still snaps |

## Security Test Cases

| TC-ID | Security Gate | Test | Expected Result |
|---|---|---|---|
| TC-SEC-01 | Input | Prompt-injection blocked at pre_process | ERROR before domain nodes run |
| TC-SEC-02 | Output | Raw treasury data blocked in output | `[REDACTED]` in output |
| TC-SEC-03 | Audit | Two audit events: data_access + output_gate | emit_trace_event called in both nodes |
| TC-SEC-04 | Trust | ValidateInputNode requires VERIFIED_EXTERNAL | trust gate enforced at backbone |

## State Serialisation Compliance

All dict/list state fields use `Optional[str]` (JSON-encoded). The `_to_json` / `_from_json` helpers in `src/schemas/state.py` are used by all domain nodes. Tests verify these via json.loads() on node outputs.
