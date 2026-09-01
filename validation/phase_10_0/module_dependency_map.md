# Phase 10.0 — Module Dependency Map

Derived from import graph and call-site reading. Arrows are *actual* runtime
dependencies, not intended ones.

---

## 1. Target architecture vs. reality

The brief's §4 conceptual chain, annotated with what the repository actually
does. `✔` = present and wired. `⚠` = present but bypassed. `✘` = absent.

```
USER
 ↓  ✔
FRONTEND (app/frontend)
 ↓  ⚠  dual path: 8 modules read mock data.js; 6 also call services
API / APPLICATION LAYER (app/backend/api + orchestrator/api.py)
 ↓  ⚠  /orchestrator/* is real; /api/scenarios + /api/projects + /api/auth are mocked
CHAT / NLU (orchestrator/conversation)                    ✔ real, deterministic fallback verified
 ↓
ORCHESTRATOR (core/orchestrator.py)                       ✔ real
 ↓
PLANNER (core/planner.py, core/plan_graph.py,
         planner/llm_planner.py)                          ✔ real, LLM + deterministic
 ↓
PLAN VALIDATOR (core/plan_graph.py::PlanValidator)        ✔ real
 ↓
EXECUTION GRAPH (core/plan_graph.py)                      ✔ real
 ↓
FAILURE MANAGER (core/failure_manager.py + circuit_breaker) ✔ real
 ↓
CAPABILITY EXECUTOR (core/executor.py)                    ✔ real
 ↓
SPECIALIST CAPABILITIES (16 registered)                   ✔ real
 ├── INGESTION      ⚠  runs, but its CanonicalNetwork is never registered
 ├── OPTIMIZATION   ✔
 ├── FORECASTING    ⚠  no history_provider passed → capability starved;
 │                     api/forecast.py calls the engine directly instead
 ├── SCENARIO       ✔  engine real; API discards its output
 ├── RESILIENCE     ✔
 ├── RISK           ✔
 ├── SIGNAL ROUTING ⚠  no signal_provider passed
 ├── KPI REGISTRY   ✔  connected through /api/kpis
 └── DIGITAL TWIN   ⚠  real service, zero UI consumers
 ↓
AGENT RESULT / AUTHORITATIVE EVIDENCE                     ✔ real
 ↓  ✘  AuthoritativeEvidencePackage has NO HTTP surface
RESULT OBSERVER (core/result_observer.py)                 ✔ real
 ↓
ADAPTIVE DECISION POLICY (core/adaptive_policy.py)        ✔ real
 ↓
REPLAN IF REQUIRED                                        ✔ real
 ↓
REASONING (orchestrator/reasoning/)                       ✔ real, numerically grounded
 ↓
GOVERNANCE (orchestrator/governance/)                     ✔ real
 ↓
FINAL RESPONSE → FRONTEND                                 ⚠  reaches chat only
 ↓
AUDIT / TRACE / DIGITAL TWIN (orchestrator/audit)         ✔ real, no UI surface
```

**Reading:** the vertical spine from Orchestrator down to Governance is intact
and production-grade. Every `⚠`/`✘` is at an application-layer boundary.

---

## 2. Critical dependency chains

### 2.1 The severed chain (P0)

```
ingestion/pipeline.py  →  ingestion/builder.py:69  →  CanonicalNetwork
                                                          │
                                                          ✘  NO EDGE
                                                          ▼
                                    SnapshotManager.register()   [stores.py:82]
                                                          │
                                                          ▼
                              OrchestratorRequest(network_snapshot_id=…)
                                                          │
                                    ┌─────────────────────┼─────────────────────┐
                                    ▼                     ▼                     ▼
                            optimization.solve      forecast.demand        twin.publish
                                    │                     │                     │
                                    ▼                     ▼                     ▼
                                        KPIRegistry → KPIResult → /api/kpis
```

The only missing edge is `CanonicalNetwork → SnapshotManager.register()`.
Everything downstream of it already works and is tested.

### 2.2 The authoritative KPI chain (working)

```
optimization/milp.py → NetworkStateResult → ExecutionContext
   → KPIRegistry.network_kpis()  [orchestrator/metrics/registry.py]
   → KPIResult[T] (status-typed)
   → api/kpis.py :64  model_dump(mode="json")
   → kpi-service.js → kpi-mapper.js (honours status, never zero-fills)
   → 2 of 4 Home cockpit cards
```

Correct end-to-end. Defects are *coverage* (2 of 4 cards) and *staleness*
(`_cached_context` never invalidated), not authority.

### 2.3 The fabrication chains (P0)

```
scenarios.js:1081  setInterval(fake progress)
   → literal object { totalCost: 1220000, sla: 96.5, carbonKg: 101200,
                      robustnessTests: [PASS], changes:[note:"MILP verified"] }
   → SCENARIOS.push()                        ← NO BACKEND CALL AT ALL
```

```
api/scenarios.py:219  orchestrator.run_sync(req)      ← real solve happens
   :222  registry.scenario_comparison(ctx)            ← real deltas obtained
   :229-259  return { totalCost: 1205000, sla: 95.5,  ← real deltas DISCARDED
                      avgUtil: 68.2, carbonKg: 102400 }
```

```
api/forecast.py:134  except Exception: pass
   :138  return hardcoded _FORECAST_NORTH_INDIA cone
        ← indistinguishable from a real HiGHS quantile forecast; no status field
```

```
chatbot.js:204  catch (err) → generateAIResponse(query)
   → "96.7% On-time SLA … Delhi NCR DC 94% utilization … all 19 India facilities"
        ← confident fabricated business narrative on LLM/orchestrator failure
```

### 2.4 Duplicate ingestion architecture (P1)

```
POST /api/ingestions            → ingestion/api.py       → real 58-file pipeline
POST /api/ingestions/upload-and-parse → ingestion_dynamic.py → own parser,
                                          own classifier, own network builder
```

Both blueprints mount on the **same `/api/ingestions` prefix**. The dynamic one
duplicates column classification and network assembly that
`ingestion/ai/field_mapper.py` and `ingestion/builder.py` already do with
guardrails, provenance and row-level issue reporting.

---

## 3. Import-graph facts worth recording

- `app/backend/api/kpis.py` imports `KPIRegistry` and `Orchestrator` directly —
  correct, no duplication.
- `app/backend/api/forecast.py` imports `ForecastingService` directly, bypassing
  the orchestrator's `forecast.demand` capability, its planner, its validator and
  its failure manager.
- `app/frontend/js/integration/mappers/twin-mapper.js` — imported once into
  `app.js`, referenced zero times. Dead.
- `app/frontend/js/data.js` is imported by **8** frontend modules; the
  integration services by **6**. Five modules (`charts`, `map`, `twin3d`,
  `insight-detail`, `agent`) import mock data and nothing else.
- No frontend module imports a solver, REI, or RF routine — the *engine*
  boundary is clean. The violation is fabricated **literals**, not computation.

---

## 4. What must not be built

Confirmed present, therefore forbidden to duplicate per §5:
orchestration framework, planner, plan validator, executor, failure manager,
state store, KPI engine, twin store, forecasting engine, MILP engine,
LLM gateway, ingestion pipeline.

Two duplicates **already exist** and are recorded as debt to retire, not to
extend: `api/ingestion_dynamic.py` + `api/network_extractor.py` (duplicate
ingestion), and `js/data.js` `SCENARIOS` + `api/scenarios.py` `_scenarios_store`
(duplicate scenario stores, mutually inconsistent).
