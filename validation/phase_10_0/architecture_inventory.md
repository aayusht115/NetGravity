# Phase 10.0 — Architecture Inventory

**Method.** Every row below was derived by reading implementation source, not
filenames and not prior documentation. Where a previous phase report asserted
something this audit could not reproduce, the discrepancy is recorded in
§7 rather than silently corrected.

**Baseline.** Branch `Backend-wiring`, working tree dirty (9 modified, 5
untracked paths). No git operation was performed by this phase.

---

## 1. Scale

| Layer | Files | Lines |
|---|---:|---:|
| `netgravity/orchestrator/` | 80 | 26,296 |
| `netgravity/ingestion/` | 58 | 11,180 |
| `netgravity/forecasting/` | 17 | 4,067 |
| `netgravity/resilience/` | 7 | 2,630 |
| `netgravity/schemas/` | 6 | 2,494 |
| `netgravity/optimization/` | 5 | 1,759 |
| `netgravity/metrics/` | 3 | 719 |
| Other engine packages | ~30 | ~3,700 |
| **Backend engine total** | **~206** | **~52,845** |
| `netgravity/tests/` | 98 | 39,894 |
| `app/backend/` | 12 | ~1,300 |
| `app/frontend/js/` | 33 | 12,408 |

The engine is mature and heavily tested. The **application layer
(`app/`) is the immature surface**, and it is where every P0 finding sits.

---

## 2. Orchestration core — VERIFIED PRESENT

All components the brief (§5) designated authoritative exist in code:

| Component | Location | Status |
|---|---|---|
| `CapabilityRegistry` | `orchestrator/routing/capability_registry.py` | Real |
| `CapabilityExecutor` | `orchestrator/core/executor.py` | Real (Phase 8.2) |
| `FailureManager` | `orchestrator/core/failure_manager.py` | Real (Phase 8.4) |
| `CircuitBreaker` | `orchestrator/core/circuit_breaker.py` | Real |
| `ExecutionContext` | `orchestrator/core/execution_context.py` | Real |
| `ExecutionStateStore` | `orchestrator/state/stores.py` | Real, **in-memory** |
| `AgentResult` | `orchestrator/schemas/agent_result.py` | Real (Phase 8.1) |
| `KPIResult` / `KPIStatus` | `orchestrator/schemas/kpi.py` | Real (Phase 9.1) |
| `KPIRegistry` | `orchestrator/metrics/registry.py` | Real (Phase 9.1) |
| `AuthoritativeEvidencePackage` | `orchestrator/schemas/kpi.py` | Real, **not wired to API** |
| Deterministic planner | `orchestrator/core/plan_graph.py` | Real (Phase 8.3) |
| `LLMPlanner` | `orchestrator/planner/llm_planner.py` | Real |
| `PlanValidator` | `orchestrator/core/plan_graph.py` | Real |
| `ResultObserver` | `orchestrator/core/result_observer.py` | Real (Phase 8.5) |
| `AdaptiveDecisionPolicy` | `orchestrator/core/adaptive_policy.py` | Real |
| Governance | `orchestrator/governance/` | Real |
| Reasoning | `orchestrator/reasoning/` | Real |
| Digital Twin | `orchestrator/twin/service.py` | Real |
| `SnapshotManager` | `orchestrator/state/stores.py` | Real, **multi-snapshot capable** |

**Registered capabilities: 16** (enumerated live, not from docs):

```
external.interpret_signal   extraction.parse            forecast.demand
governance.classify         kpi.summarise               market.score_signal
network.load_snapshot       optimization.solve          optimization.solve_scenario
reasoning.synthesise        resilience.assess           risk.compute_rf
scenario.create             scenario.validate           signal.route_for_forecast
twin.publish
```

**Conclusion: there is no need to build orchestration in this phase, and no
justification for a second one.** The brief's §5 prohibition is satisfiable by
integration alone.

---

## 3. Module inventory (responsibility → I/O → consumers → status)

### 3.1 Engine layer (production-grade)

| Module | Responsibility | Input | Output | Current consumers | Status |
|---|---|---|---|---|---|
| `optimization/milp.py` | Exact MILP network solve | `CanonicalNetwork` | `NetworkStateResult` | `OptimizationClient` → orchestrator | **PRODUCTION** |
| `forecasting/service.py` | ETS / intermittent / quantile forecast + sup-F break test | `ForecastRequest` | `ForecastResult` | orchestrator `forecast.demand`; **also called directly by `api/forecast.py`** | **PRODUCTION (bypassed once)** |
| `resilience/rei.py` | REI via disruption re-solve | network + facility | `FacilityResilienceResult` | `REIClient` → orchestrator | **PRODUCTION** |
| `orchestrator/risk/risk_factor.py` | `RF = P + REI − P·REI` | P, REI | `RiskAssessment` | orchestrator `risk.compute_rf` | **PRODUCTION** |
| `metrics/kpis.py` | Network KPI computation | solve result | `NetworkKPIs` | `KPIClient` | **PRODUCTION** |
| `orchestrator/metrics/registry.py` | Authoritative KPI wrapping + status | `ExecutionContext` | `KPIResult[T]` | `api/kpis.py` | **PRODUCTION, CONNECTED** |
| `ingestion/` (58 files) | Upload → parse → map → validate → canonicalize | files | `CanonicalNetwork` + `RowIssue[]` | `ingestion/api.py` | **PRODUCTION, ORPHANED OUTPUT** |
| `orchestrator/twin/service.py` | Twin state + `MetricDelta` comparison | `ExecutionContext` | twin state | `/orchestrator/twin/*` | **PRODUCTION, UNUSED BY UI** |

### 3.2 Application layer (`app/backend/api/`)

| Module | Responsibility | Real engine call? | Status |
|---|---|---|---|
| `kpis.py` | Network/facility KPI DTOs | **Yes** — `KPIRegistry` + orchestrator | **CONNECTED** (stale-cache defect) |
| `scenarios.py` | Scenario catalogue + simulate | Partially — calls orchestrator then **discards results** | **BROKEN / FABRICATED** |
| `forecast.py` | Forecast + signals | Partially — real engine on hardcoded history, silent fabricated fallback | **PARTIALLY CONNECTED** |
| `projects.py` | Project CRUD | No | **MOCKED** |
| `auth.py` | Login / session | No | **MOCKED + INSECURE** |
| `ingestion_dynamic.py` | Excel/CSV parse | Own parser, **duplicates `netgravity/ingestion/`** | **DUPLICATE ARCHITECTURE** |
| `network_extractor.py` | Column classify + network build | Own heuristics | **DUPLICATE ARCHITECTURE** |

### 3.3 Frontend (`app/frontend/js/`)

| Module | Backend calls | Data source | Status |
|---|---|---|---|
| `integration/api-client.js` | — | — | **PRODUCTION** (timeout, correlation ID, error normalization, bearer token) |
| `integration/mappers/kpi-mapper.js` | — | — | **PRODUCTION** (honours `KPIStatus`, never zero-fills) |
| `auth.js` | `authService` | backend | CONNECTED |
| `projects.js` | `projectService` | backend | CONNECTED (to mocked backend) |
| `ingestion.js` | `ingestionService` | backend | PARTIALLY CONNECTED |
| `app.js` | `kpiService` | **mock first, 2 of 4 cards overwritten** | PARTIALLY CONNECTED |
| `chatbot.js` | `chatService` | backend, **fabricated fallback** | PARTIALLY CONNECTED |
| `scenarios.js` | `scenarioService` imported | **mock `SCENARIOS`; creation fabricated client-side** | **MOCKED / FABRICATING** |
| `charts.js` | none | mock `FORECAST` | **MOCKED** |
| `map.js` | none | mock | **MOCKED** |
| `twin3d.js` | none | mock | **MOCKED** |
| `insight-detail.js` | none | mock `HOME_INSIGHTS` | **MOCKED** |
| `agent.js` | none | mock `AGENT_STATE` | **MOCKED** |
| `data.js` | — | 1,475-line hardcoded store, self-labelled `PROTOTYPE / MOCKED` | **MOCK LAYER** |

---

## 4. The single decisive architectural gap

The ingestion pipeline produces a real `CanonicalNetwork`
(`ingestion/builder.py:69`). The orchestrator consumes networks through
`SnapshotManager.register()` (`state/stores.py:82`), which is content-addressed,
thread-safe, deep-copying and already supports **many concurrent snapshots**
selected per-request via `OrchestratorRequest.network_snapshot_id`.

**These two halves are never connected.** `app/backend/app.py:83` instead does:

```python
_orchestrator = build_orchestrator(network=build_case16_network())
```

— once, at import, from a fixture the file itself labels
`FABRICATED demonstration data`.

Consequences, all traceable to this one line:

1. Uploaded customer data never reaches the solver, the KPI layer, or the twin.
2. Every project shares one global orchestrator → **project isolation is
   structurally impossible**, not merely unenforced.
3. All five seeded projects carry `snapshot_id: "snap_case16_synthetic"`.
4. `build_orchestrator` is called with **no `history_provider`**, so the
   orchestrator's own `forecast.demand` capability has no history — which is why
   `api/forecast.py` bypasses the orchestrator entirely.

The fix is integration, not construction: the required primitive already exists
and is already tested.

---

## 5. Persistence

`SnapshotManager`, `ScenarioStore`, `ExecutionStateStore`, `_USERS`,
`_SESSIONS`, `_PROJECTS`, `_scenarios_store`, `_ACTIVE_PARSED_NETWORK` are **all
in-memory dictionaries or module-level lists**. Everything is lost on restart.

`ingestion/snapshot.py` does provide durable `save_snapshot` / `load_snapshot`
to a curated zone keyed by `data_version` — the only real persistence in the
system, and it is not used by the application layer.

Per brief §25, no database is introduced by this phase; the gap is documented
and the existing curated-zone mechanism is the recommended integration point.

---

## 6. LLM integration

Single gateway (`netgravity/llm/`), env-configured, currently
`available: False, token_configured: False`. Verified live: the system runs
deterministically end-to-end with no token, exactly as §15 requires. No second
provider exists. No credential appears in `app/` source; `.env` is gitignored.

---

## 7. Discrepancies against the Phase 9.2 report

The prior report is contradicted by source in four places. Recorded, not
silently fixed:

| 9.2 claim | Verified reality |
|---|---|
| "S2 Digital Twin — Real Data: **Yes** — PASS" | `twin3d.js` and `map.js` make **zero** backend calls. `twinService` is imported into `app.js` and **never called**; `mapTwinStateToFrontend` is dead code. Twin is 100% mock. |
| "S5–S8 Scenario Planning — Real Data: **Yes** — PASS" | `scenarios.js` renders the mock `SCENARIOS` array; scenario creation fabricates cost/SLA/carbon client-side and labels it "MILP verified". |
| "O3/O4 Projects — Real Data: **Yes**" | `projects.py` serves a hardcoded 5-project list, all pointing at the same synthetic snapshot. |
| "Frontend Business Logic Audit — client-side KPI fabrication: **0 instances**" | The audit searched for solver symbols (`solve_network`, `LpProblem`, `compute_rei`). It could not detect fabrication-by-literal, which is the actual pattern present (`scenarios.js:1096-1140`). |

The 9.2 report's *API-contract* and *KPI-reconciliation* sections are
reproducible and stand; its *screen-level* "real data" column does not.
