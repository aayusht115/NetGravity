# NetGravity — Production Integration Map

Screen → component → API → DTO → backend source → authoritative owner, as
required by brief §19. Status is what the code does today.

Legend: **CONNECTED** (live authoritative data) · **PARTIAL** (some elements
live, some mock) · **MOCKED** (renders `js/data.js`) · **N/A**.

---

## A. Production console — `production.html`

Imports no mock data at all. Every element below is live.

| Screen | Component | API | DTO | Backend source | Authoritative owner | Status |
|---|---|---|---|---|---|---|
| Auth gate | `#form-signin` / `#form-signup` | `POST /api/auth/login`, `/signup` | `{token, user, status}` | `services/security.py` | PBKDF2 credential store | CONNECTED |
| Workspace gate | `#project-list` | `GET /api/projects` | `ProjectRecord[]` | `ProjectRegistry` | Project ownership | CONNECTED |
| Workspace gate | `#form-create-project` | `POST /api/projects` | `ProjectRecord` | `ProjectRegistry` | — | CONNECTED |
| Overview | `#kpi-grid` (18 cards) | `GET /api/kpis/network` | `{kpis: Record<string, KPIResult>}` | `KPIRegistry.network_kpis` | `optimization.milp`, `metrics.kpis` | CONNECTED |
| Overview | `#kpi-provenance` | same | `snapshot_id`, `execution_id`, `computed_at` | `ExecutionContext` | — | CONNECTED |
| Overview | `#threshold-list` | same | `TriggeredThreshold[]` | `evaluate_thresholds` | `metrics/thresholds.py` | CONNECTED |
| Facilities | `#facility-table` | `GET /api/kpis/facilities` | `Record<facility, Record<metric, KPIResult>>` | `KPIRegistry.facility_kpis` | `optimization.milp` | CONNECTED |
| Data | `#upload-input`, `#quality-summary`, `#mapping-table` | `POST /api/ingestions/preview/upload-and-parse` | `{files, mapping, mapStats, dataQuality, structure}` | pandas + `classify_column_name` | measured, not asserted | CONNECTED |
| Forecast | `#forecast-output` | `GET /api/forecast` | `{status, series[{status, engine, pattern, points[p10,p50,p90]}]}` | `forecast.demand` capability | `netgravity.forecasting` | CONNECTED |
| Scenarios | `#form-scenario` → `#scenario-results` | `POST /api/scenarios/simulate` | `{baseline_kpis, scenario_kpis, deltas, provenance}` | MILP + `scenario_comparison` | `optimization.milp` | CONNECTED |
| Evidence | `#evidence-output` | `GET /api/kpis/evidence` | `AuthoritativeEvidencePackage` | `KPIRegistry.evidence_package` | all engines | CONNECTED |
| Assistant | `#chat-log` | `POST /orchestrator/chat` | `{response, intent, conversation_id}` | orchestrator ChatService | reasoning (grounded) | CONNECTED |

**Empty and error states.** Selecting a project with no bound network renders
`NO_NETWORK_BOUND` as an explicit message in `#kpi-grid`; no previous project's
numbers survive the switch. A failed scenario solve renders the error and adds
no row. A failed assistant call renders unavailability, not a narrative.

---

## B. Approved prototype — `index.html`

Corrected where it fabricated; still partly mock elsewhere. Stated plainly.

| Screen | Component | API | Backend source | Status | Note |
|---|---|---|---|---|---|
| O1/O2 Landing & auth | `auth.js` | `/api/auth/*` | `services/security.py` | CONNECTED | Real credential verification |
| O3/O4 Projects | `projects.js` | `/api/projects` | `ProjectRegistry` | CONNECTED | Owned + isolated |
| O5/O6 Ingestion console | `ingestion.js` | `/api/ingestions*` | ingestion pipeline | PARTIAL | Preview live; finalize→bind not wired |
| S1 Home cockpit | `app.js` | `/api/kpis/network` | `KPIRegistry` | PARTIAL | 2 of 4 cards overwritten; mocks remain on failure (P1-6) |
| S2 Digital twin 2D/3D | `map.js`, `twin3d.js` | — | — | MOCKED | `/orchestrator/twin/*` is real and unused (P1-3) |
| S3 Facility dashboard | `app.js` | — | mock `FACILITY_KPIS` | MOCKED | — |
| S4 Forecast chart | `charts.js` | — | mock `FORECAST` | MOCKED | Real forecast lives on the production console |
| S5–S8 Scenario planning | `scenarios.js` | `POST /api/scenarios/simulate` | MILP | **PARTIAL — corrected** | Creation now solves for real; comparison table still reads the mock `SCENARIOS` seed |
| S9 Insight deep-dive | `insight-detail.js` | — | mock `HOME_INSIGHTS` | MOCKED | `/orchestrator/insights` real and unused (P2-7) |
| S10 Assistant | `chatbot.js` | `POST /orchestrator/chat` | orchestrator | **CONNECTED — corrected** | Fabricated fallback removed entirely |
| S11/S12 Governance & traces | — | — | mock `GOVERNANCE_TIERS` | MOCKED | Trace API real and unused (P2-4) |

### What changed in the prototype this phase

| File | Was | Now |
|---|---|---|
| `scenarios.js` | `setInterval` fake progress → literal `{totalCost: 1220000, sla: 96.5, carbonKg: 101200, robustnessTests:[PASS], note:'MILP verified'}` pushed to `SCENARIOS`, **no request made** | `await scenarioService.simulateScenario()`; renders authoritative KPIs; explicit failure state |
| `chatbot.js` | On any failure, `generateAIResponse()` emitted "96.7% On-time SLA… all 19 India facilities" | Both `generateAIResponse` and `FAQ_KNOWLEDGE_BASE` deleted; failure is reported |
| `mappers/scenario-mapper.js` | `raw.totalCost \|\| 0`, `raw.sla \|\| 0`, `capacityRisk \|\| 'Low'` | Status-preserving; null for absent; risk `'Unknown'` when utilisation is unknown |
| `services/*` | Unscoped | Project-scoped via `project-context.js` |

---

## C. Integration layer — shared

| Module | Responsibility | Status |
|---|---|---|
| `integration/api-client.js` | HTTP transport, bearer token, `X-Request-ID`, timeout, error normalization | PRODUCTION (pre-existing, sound) |
| `integration/project-context.js` | Active project id + change subscription | NEW in 10.0 |
| `integration/errors.js` | `ApplicationError` / `ErrorCode` | PRODUCTION |
| `mappers/kpi-mapper.js` | `KPIResult` → card; never zero-fills | PRODUCTION (pre-existing, sound) |
| `mappers/scenario-mapper.js` | Scenario payload → card; status-preserving | REWRITTEN in 10.0 |
| `mappers/twin-mapper.js` | Twin state → frontend | **DEAD** — imported once, never called |
| `services/*` | Typed endpoint wrappers | Project-scoped in 10.0 |

---

## D. Rule the map enforces

No frontend module imports a solver, a KPI formula, REI, or RF. The engine
boundary was already clean; the violation this phase removed was fabrication by
**literal**, which no import-graph check could have detected. The structural
tests in `netgravity/tests/integration/test_production_application.py`
therefore assert on *values* — the specific fabricated constants — rather than
on imports.
