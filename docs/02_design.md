# docs/02_design.md — FIN-C2-072 Design Specification

## 1. Template Metadata

| Field | Value |
|---|---|
| Template ID | FIN-C2-072 |
| Category | Cat 2 |
| Industry | FIN (Finance) |
| Architecture | AgentBaseGraph (outer) + BaseGraph (inner DomainWorkflowGraph) |
| L1 Base (framework base class) | AgentBaseGraph — direct framework inheritance |
| Pattern | DocGen — multi-step domain workflow |
| Design Date | 2026-07-02 |

## 2. Architecture Overview

FIN-C2-072 uses the **Cat 2 nested** architecture: an outer `AgentBaseGraph` backbone wraps a `GraphNode` in the `main` slot that delegates to an inner `DomainWorkflowGraph` (BaseGraph). Domain complexity is encapsulated in the inner graph; the outer backbone provides the fixed pipeline and security gates.

### 2.1 Outer Backbone Pipeline

```
START → initialize → pre_process → main (DomainWorkflow) → post_process → finalize → END
                                               ↓ (RETRY, max 3)
                                          pre_process
```

| Slot | Class | Responsibility | Trust Level |
|---|---|---|---|
| initialize | InitializeNode (default) | schema_version, session_id, trust_level | — |
| pre_process | ValidateInputNode | Input sanitisation + prompt-injection rejection | VERIFIED_EXTERNAL |
| main | CashFlowForecastingGraphNode | GraphNode wrapper — delegates to DomainWorkflowGraph | — |
| post_process | SecurityGateOutputNode | Output gate + audit log | ANONYMOUS |
| finalize | FinalizeNode (default) | response_metadata, total_time_ms | — |

### 2.2 Inner Domain Workflow Pipeline

```
START → load_historical_data → forecast_cash_positions → identify_liquidity_risk
      → check_threshold_breaches → generate_scenario_analysis → alert_if_required → END
```

| Node | Class | Responsibility | Trust Level |
|---|---|---|---|
| load_historical_data | LoadHistoricalDataNode | Load/normalise AP/AR + cash flow data; audit | INTERNAL |
| forecast_cash_positions | ForecastCashPositionsNode | 7d/30d/90d position forecasting | INTERNAL |
| identify_liquidity_risk | IdentifyLiquidityRiskNode | Detect windows below min_cash_threshold | INTERNAL |
| check_threshold_breaches | CheckThresholdBreachesNode | Classify severity: warning / critical | INTERNAL |
| generate_scenario_analysis | GenerateScenarioAnalysisNode | FX/IR sensitivity scenarios | INTERNAL |
| alert_if_required | AlertIfRequiredNode | Generate alert payload if breach detected | INTERNAL |

## 3. State Schema

State inherits `AgentState` as a flat TypedDict. All dict/list fields are stored as `Optional[str]` (JSON-encoded) to avoid msgpack round-trip corruption. `_to_json()` / `_from_json()` helpers in `src/schemas/state.py` are used for serialisation.

| Field | Type | Source Node | Description |
|---|---|---|---|
| validated_input | Optional[str] | ValidateInputNode | Trimmed, injection-free user input |
| historical_data_json | Optional[str] | LoadHistoricalDataNode | JSON: AP/AR + historical cash flow |
| forecast_json | Optional[str] | ForecastCashPositionsNode | JSON: 7d/30d/90d forecasts |
| liquidity_risks_json | Optional[str] | IdentifyLiquidityRiskNode | JSON: risk windows |
| threshold_breaches_json | Optional[str] | CheckThresholdBreachesNode | JSON: breach severity list |
| scenario_analysis_json | Optional[str] | GenerateScenarioAnalysisNode | JSON: FX/IR scenarios |
| alert_payload_json | Optional[str] | AlertIfRequiredNode | JSON: alert payload (when breach) |
| breach_detected | Optional[bool] | CheckThresholdBreachesNode | True when ≥1 breach found |
| audit_trace_id | Optional[str] | SecurityGateOutputNode | Audit correlation ID |
| formatted_output | Optional[str] | SecurityGateOutputNode | Final sanitised output |

## 4. Security Design

### Input validation
`ValidateInputNode` (pre_process, VERIFIED_EXTERNAL):
- Rejects empty / whitespace-only inputs
- Regex-based prompt-injection rejection (system-instruction override patterns)
- Returns `AgentStatus.ERROR` on violation; execution does not reach domain nodes

### Caller data contract (input_context)
`POST /invoke` accepts an optional `input_context` object (size-capped at the adapter). Supported keys: `historical_data` (`opening_balance`, `cash_inflows`, `cash_outflows` — each flow entry `{day_offset, amount}`), `min_cash_threshold`, `critical_ratio`, `subsidiary_id`, `currency`, `date_range_days`. `LoadHistoricalDataNode` validates every field against explicit bounds (entry-count cap, day-offset 0–365, magnitude caps, ISO currency) before use. **Every caller-controlled number must be FINITE** (`NaN`/`Infinity` are rejected — `_finite_in_range` in `src/schemas/state.py`; a NaN threshold would otherwise fail OPEN, silently suppressing alerts), applied consistently to `min_cash_threshold`, `critical_ratio`, and the `fx_scenarios`/`ir_scenarios` tables (whose scenario names must also be inert `[a-z0-9_]{1,32}` identifiers, since they render into the report). Violations return `AgentStatus.ERROR` naming the field only — rejected values are never echoed. With no `historical_data`, the pipeline degrades to a zero-balance stub baseline. Because the framework GraphNode does not forward `input_context` to a subgraph, `src/graph/context_bridge.py` carries it across the outer→inner boundary via the sanctioned `extract_input` / `_extra_initial_state` hooks.

### Output gate — approved external output schema
`SecurityGateOutputNode.execute()` calls the module-level `_security_gate_output(result, state)` helper, which enforces two independent layers:
- **Verbatim embedding**: raw treasury fields (`historical_data_json`, `threshold_breaches_json`, `validated_input`, `audit_trace_id`) are never re-emitted verbatim; blocked content is replaced with `[REDACTED]`
- **Derived-value precision**: the external report renders only aggregate figures rounded to the nearest 1,000 (the approved external output schema; raw AP/AR line items are never rendered at all). The gate independently enforces the invariant with an explicit FORM-based schema (no magnitude exemption): monetary values are identified by form — comma-grouped numbers or 5+-digit runs (the renderer formats every monetary value with `{:,.0f}`) — **and by currency context, symmetrically**: any bare short number immediately associated with a currency marker — a 3-letter uppercase code before or after the value (spaced or attached), or a currency symbol before or after — is monetary regardless of digit count, so an unformatted rendering regression cannot bypass the gate. Structural tokens (horizons, counts, years without currency adjacency) never match. Any matched token off the 1,000 grid, at any magnitude, is snapped onto it with a `fin_c2_072.precision_redaction` audit event
- **Precision-gate token boundaries** — the grammar is deliberately narrow about where a monetary token starts and ends, because the report also renders identifiers made of digits. Four rules, each pinned by `tests/unit/test_gate_precision_identifiers.py`:
  - **Decimal absorption.** Every value alternative takes its fraction into the SAME token, written `(?:\.\d+|(?!\.\d))`. Without it the fraction of `9999.99999` is a standalone 5+-digit run in its own right and gets rewritten (`9999.100,000`), and a decimal amount snaps its integer part while the fraction dangles (`JPY 1234.56` → `JPY 1,000.56` — neither the true figure nor a grid value). The optional form `(?:\.\d+)?` is NOT equivalent: the engine backtracks out of the fraction and re-matches the integer whenever the following text fails the trailing guard, so `JPY 1234.56m` regresses. The grid is not relaxed for decimals — an off-grid amount in currency context still snaps, as ONE number (`JPY 1234.56` → `JPY 1,000`).
  - **Delimiter.** Marker-to-value separation is `[ \t]*(?:\n[ \t]*)?` — horizontal whitespace and at most one newline, never a paragraph break. A plain `\s*` lets a 3-letter uppercase word ending a line bind to the number opening the next block, so the gate rewrites the document's own structure (`  90d: 3,000,000 JPY\n\n2. Liquidity Alerts` → `0. Liquidity Alerts`).
  - **Identifier guards** widened to this template's render alphabet, `[A-Za-z0-9_-]` (plus `.` on the leading side only, so an amount ending a sentence still snaps). The `_` is load-bearing: FX scenario names are caller-supplied, validated only as `^[a-z0-9_]{1,32}$`, and rendered verbatim as the label of every FX scenario line — without the guard, `POST /invoke` returned `jpy_1,235,000_stress` for a caller's `jpy_1234567_stress`. `:` and `=` are deliberately excluded, since every label in this report is separated from its value by whitespace.
  - **Word boundaries are kept ALONGSIDE the guard classes, never replaced by them.** Python's `\w` covers CJK, so `万` `円` `条` `日` `人` are word characters that an ASCII class such as `(?![A-Za-z0-9_-])` admits and `\b` rejects. Swapping one for the other makes a statutory form like `（3000万円+600万円×法定相続人の数）` match as a `円`-marker plus `+600` and render `+1,000万円` — the gate rewriting a quoted statute on a line no caller input touches. The grammar carries both tests; `(?![A-Za-z])` is used on the marker only where a digit must still be allowed to follow (the attached `JPY9999` leak form).
  - **A short (1–4 digit) value may never be the prefix of a comma-grouped one** (`_SHORT_VAL`). The alternation lists the grouped form first, but "first" is not "only": when the grouped form matches and the trailing guard then rejects the token, the engine backtracks into the short form and matches the leading digit group alone — `JPY 1,000-1` becomes `JPY 0,000-1`, the exact mangling the ordering exists to prevent and the one `main` still has unconditionally.
  - **Attached `<3 letters>-<digits>`** is restricted to named currency codes. That shape is how this domain writes record references (`FIN-C2-072`, subsidiary codes) and by form alone it is indistinguishable from an attached negative amount (`JPY-9999`); a currency list is closed and stable where an identifier list could never be. SEPARATED markers stay unrestricted, so the caller's validated `^[A-Z]{3}$` currency — which the report always renders separated, as `{value} {currency}` — keeps the full fail-safe behaviour (`XBT 9999` → `XBT 10,000`).
- Implementation is a **module-level function** (NOT an instance method)

### Audit logging
`emit_trace_event()` is called positionally in:
- `LoadHistoricalDataNode.execute()` — data access audit (`fin_c2_072.data_access`)
- `SecurityGateOutputNode.execute()` — output gate audit (`fin_c2_072.output_gate`), including breach_detected + alert_generated flags

### No `_extra_security_gate_*` Instance Methods
No `_extra_security_gate_input` / `_extra_security_gate_output` instance methods are defined on any FunctionNode subclass; the output gate is a module-level function, keeping the hook stateless.

## 5. Key Design Decisions

1. **Cat 2 nested**: domain complexity in inner `DomainWorkflowGraph`; outer backbone for security gates
2. **State serialisation**: all dict/list state fields serialised as JSON strings; `_to_json`/`_from_json` as module-level helpers
3. **Linear inner pipeline**: no conditional branching in the inner graph (sequential: load→forecast→risk→breach→scenario→alert)
4. **breach_detected bool**: stored as a plain `Optional[bool]` in state (scalars exempt from the JSON serialisation rule)
5. **Configurable thresholds**: `min_cash_threshold`, `critical_ratio`, FX/IR scenarios pulled from `input_context` at runtime; defaults in node module constants
6. **J-SOX audit trail**: `emit_trace_event` in `LoadHistoricalDataNode` (data access) + `SecurityGateOutputNode` (output gate) — two-event trail for financial data access auditing

## 6. File Layout

```
src/
  schemas/state.py                         ← State TypedDict + serialisation helpers
  nodes/
    pre_process_node.py                    ← ValidateInputNode (VERIFIED_EXTERNAL)
    post_process_node.py                   ← SecurityGateOutputNode (ANONYMOUS)
    load_historical_data_node.py           ← LoadHistoricalDataNode (INTERNAL)
    forecast_cash_positions_node.py        ← ForecastCashPositionsNode (INTERNAL)
    identify_liquidity_risk_node.py        ← IdentifyLiquidityRiskNode (INTERNAL)
    check_threshold_breaches_node.py       ← CheckThresholdBreachesNode (INTERNAL)
    generate_scenario_analysis_node.py     ← GenerateScenarioAnalysisNode (INTERNAL)
    alert_if_required_node.py              ← AlertIfRequiredNode (INTERNAL)
  graph/
    graph.py                               ← outer graph (CorporateTreasuryCashFlowForecastingAgent)
    domain_workflow_graph.py               ← inner graph (DomainWorkflowGraph)
  api/server.py                            ← FastAPI entry point
config/agent.yaml                          ← class: CorporateTreasuryCashFlowForecastingAgent
```

## 7. References

- src/examples/graph_cat2_sample.py
- src/examples/domain_workflow_graph_sample.py
