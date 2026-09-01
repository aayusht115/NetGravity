# NetGravity — Production Data Flow

How a number reaches a screen, and what stops an ungrounded one from doing so.

---

## 1. The authoritative path

```
CanonicalNetwork (project's bound snapshot)
   │
   ▼  optimization.solve            netgravity/optimization/milp.py  (PuLP/HiGHS)
OptimizationResult
   │
   ▼  compute_kpis()                netgravity/metrics/kpis.py
NetworkKPIs
   │
   ▼  build_network_state_result()  netgravity/metrics/contracts.py
NetworkStateResult ──────────────►  ExecutionContext.network_states
   │
   ▼  KPIRegistry.network_kpis()    orchestrator/metrics/registry.py
KPIResult[T]   { value, unit, scope, formula_id, source_capability,
                 authoritative_owner, status, snapshot_id, execution_id }
   │
   ├──►  AuthoritativeEvidencePackage  ──►  GET /api/kpis/evidence
   │                                   ──►  Reasoning (grounded)
   ▼
GET /api/kpis/network   →  kpi-service.js  →  kpi-mapper / scenario-mapper
                                            →  rendered with its status badge
```

Nothing on this path recomputes a value. Each stage either carries the number
forward or explains why it cannot.

---

## 2. Where a value can become unavailable

There are exactly four honest outcomes, and no fifth:

| Where | Outcome | Example |
|---|---|---|
| Solver infeasible | `KPIStatus.INFEASIBLE`, `value = None` | A capacity cut that makes demand unservable |
| Input absent | `KPIStatus.INSUFFICIENT_EVIDENCE` | A solve that did not report a distance figure |
| Formula undefined | `KPIStatus.NOT_COMPUTABLE` | `RF` when event probability is unknown |
| Input invalid | `KPIStatus.INVALID_INPUT` | Probability outside `[0, 1]` |

`KPIResult`'s `model_validator` refuses construction of a non-VALID result that
carries a value, and of a VALID result that carries none — so the two can never
disagree. The frontend mappers mirror this: `readKPI()` returns
`value: null` for any non-VALID status, and the renderer prints
"Unavailable"/"Infeasible" rather than a number.

**Zero is reserved for measurement.** `total_carbon_kg = 0` means a solved
network with no emissions. An unavailable carbon figure is
`INSUFFICIENT_EVIDENCE`, never `0`.

---

## 3. Ingestion

```
upload  ──►  file validation      extension allowlist, ≤25 MB, ≤10 files,
                                  size measured from the stream
        ──►  parse                pandas; failures reported per file, never swallowed
        ──►  field detection      classify_column_name → auto | review | ignored
        ──►  quality measurement  duplicates, empty rows, null density — counted
        ──►  mapping review       blocking questions must be answered
        ──►  validation           row-level issues with explainable reasons
        ──►  canonicalization     ingestion/builder.py → CanonicalNetwork
        ──►  snapshot             ProjectRegistry.bind_network()  ← not yet wired to finalize
        ──►  analysis ready
```

Two properties worth stating:

- **`finalize` refuses** while any blocking question is unanswered, and refuses
  if no `CanonicalNetwork` could be assembled. A partially-understood dataset
  cannot become an analysable network.
- **The preview endpoint does not optimise.** It previously ran a second,
  independent MILP with invented freight rates and returned hardcoded
  `fillRate: 100.0` / `slaAdherence: 96.5`. That solver was removed; the preview
  now returns structure and measured quality only.

---

## 4. Scenario flow

```
BASELINE                                  SCENARIO
  snapshot (immutable)                      snapshot + ScenarioIntentSpec
  → optimization.solve                      → optimization.solve_scenario
  → NetworkStateResult                      → NetworkStateResult  (ctx key "scenario:<id>")
        │                                          │
        └──────────► KPIRegistry.scenario_comparison() ◄────────┘
                                │
                     ScenarioMetricDelta
                     { abs_delta, pct_delta, direction, reason }
                     direction = NOT_COMPARABLE when either side is missing
                                │
                                ▼
                     POST /api/scenarios/simulate
                     { baseline_kpis, scenario_kpis, deltas, provenance }
```

The baseline is recomputed from the snapshot on demand and is never written to
by a scenario run, so §13's immutability requirement holds structurally rather
than by convention. A zero baseline yields `pct_delta = None` with a stated
reason — not a division result and not a zero.

---

## 5. Forecast flow

```
ingestion staging zone (data/standardized/**.json)
   │  load_staging_history()
   ▼
DemandTimeSeries[]  ──►  history_provider(snapshot)  ──►  forecast.demand capability
                                                              │
                              no matching history ────────────┤──► MissingDataError
                                                              │     ("no observed demand
                                                              │      history … reported
                                                              ▼      unforecastable")
                                          ForecastResult
                                          SeriesForecast { status, engine, pattern,
                                                           points[p10,p50,p90], accuracy }
                                                              │
                                                              ▼
                                      GET /api/forecast  { status: OK | FORECAST_UNAVAILABLE }
```

A network with no ingested transactional history genuinely cannot be forecast,
and the API says so. On the demo network this is the live behaviour today:
`FORECAST_UNAVAILABLE` with an empty `series`.

---

## 6. Reasoning boundary

```
AuthoritativeEvidencePackage ──► Reasoning ──► narrative
                                     │
                                     ▼
                          numeric_grounding._FACT_SPEC
                          every number the model asserts must
                          match an authoritative fact
```

The model receives evidence and returns prose. It cannot write a
`KPIResult`, cannot call a capability, and cannot change a solver output.
When the gateway is unavailable the system runs deterministically end to end —
verified live, with the assistant reporting unavailability rather than
answering from memory.
