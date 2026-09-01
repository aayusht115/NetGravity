# Phase 10.0 — Integration Gap Analysis

Classification per brief §30: BUILT / CONNECTED / PARTIALLY CONNECTED / MOCKED /
MISSING / BROKEN / NOT PRODUCTION READY.
Priority per §31.

**Regression baseline (pre-change): 2,460 passed · 4 skipped · 0 failed · 128.84s.**

---

## P0 — System cannot operate as a production application

### P0-1 · Ingested customer data never reaches the engine
- **Component:** `app/backend/app.py:83`
- **Expected:** A project's uploaded, validated, canonicalized network is the
  network the solver, KPI layer and twin operate on.
- **Current:** One process-global orchestrator is built at import from
  `build_case16_network()` — a fixture the same file annotates
  `FABRICATED demonstration data`. Ingestion's real `CanonicalNetwork`
  (`ingestion/builder.py:69`) is discarded.
- **Dependency:** `SnapshotManager.register()` (`state/stores.py:82`) — already
  exists, content-addressed, thread-safe, multi-snapshot.
- **Severity:** P0. Every downstream number in the product describes a fictional
  network. No amount of UI wiring can fix this.
- **Fix:** Register finalized ingestion networks as snapshots; resolve
  `project → snapshot_id`; pass `network_snapshot_id` on every request.

### P0-2 · Project isolation is structurally impossible
- **Component:** `api/projects.py`, `api/scenarios.py:34`,
  `api/ingestion_dynamic.py:23`, `app.py:83`
- **Expected:** Project A can never read Project B's data (§8).
- **Current:** All state is process-global and unkeyed: one `_orchestrator`,
  one `_scenarios_store` list, one `_ACTIVE_PARSED_NETWORK` dict, one
  `_PROJECTS` list with no owner field. All five seeded projects carry the
  identical `snapshot_id: "snap_case16_synthetic"`.
- **Severity:** P0 — a multi-tenant correctness and confidentiality defect.
- **Fix:** Key every store by `project_id`; derive `snapshot_id` from the
  project; reject cross-project reads.

### P0-3 · Scenario results are fabricated client-side and labelled "MILP verified"
- **Component:** `app/frontend/js/scenarios.js:1081-1140`
- **Expected:** Scenario KPIs come from a real MILP re-solve compared against an
  immutable baseline.
- **Current:** A `setInterval` renders fake progress, then a literal object is
  pushed into `SCENARIOS` with `totalCost: 1220000`, `costChange: -5.1`,
  `sla: 96.5`, `carbonKg: 101200`, `robustnessTests: [{status: 'PASS'}]`,
  `assumptions: [{label:'Solver Execution', value:'Branch-and-Cut (Exact)'}]`
  and `changes: [{note: 'MILP verified'}]`. **No backend call occurs.**
- **Severity:** P0. This is the most serious finding in the audit: invented
  numbers presented to a decision-maker with an explicit, false provenance claim.
- **Fix:** Route creation through `POST /api/scenarios/simulate`; render only
  returned authoritative values; remove the literal.

### P0-4 · The scenario API discards the real solve it just performed
- **Component:** `app/backend/api/scenarios.py:210-273`
- **Expected:** Return the orchestrator's authoritative deltas.
- **Current:** `orchestrator.run_sync()` runs, `registry.scenario_comparison(ctx)`
  returns real `ScenarioMetricDelta` objects — then the response hardcodes
  `totalCost: 1205000`, `sla_val = 95.5`, `avgUtil: 68.2`, `maxUtil: 88.0`,
  `carbonKg: 102400`. Only `costChange` uses a real delta, and it falls back to
  a fabricated `-6.5` when absent.
- **Severity:** P0 — violates §9 (frontend/API must never fabricate KPIs) and
  §24 (never convert failures into plausible business values).
- **Fix:** Serialize the real `KPIResult`/`ScenarioMetricDelta` payload with
  status; delete the literals; surface `INFEASIBLE`/`NOT_COMPUTABLE` honestly.

### P0-5 · Authentication is bypassable and passwords are never verified
- **Component:** `app/backend/api/auth.py`
- **Expected:** Verified credentials; unauthenticated requests rejected (§22).
- **Current:**
  - `login()` reads `password` and **never checks it** — any password succeeds.
  - Unknown emails are **auto-provisioned** as valid users on login (`:47-57`).
  - `/me` returns a **default authenticated planner** when the token is missing
    or invalid (`:105-108`) — authentication is optional in practice.
  - No `@require_auth` guard exists on any endpoint; `/api/kpis/*`,
    `/api/scenarios/*`, `/api/projects/*` and `/orchestrator/*` are fully open.
  - Sessions are an in-memory dict; no hashing, no expiry, no rotation.
- **Severity:** P0.
- **Fix:** Hash+verify credentials, remove auto-provisioning and the anonymous
  fallback, add an auth decorator applied to every non-public route.

---

## P1 — Core business workflow broken

### P1-1 · Forecast silently substitutes a fabricated cone on engine failure
- **Component:** `app/backend/api/forecast.py:134-154`
- **Current:** `except Exception: pass` → returns hardcoded
  `_FORECAST_NORTH_INDIA` / `_FORECAST_UPPER` / `_FORECAST_LOWER` with no status
  field, no logging, and the same hardcoded `growthRate: 14.2`,
  `breachMonth: "Dec'26"`, `breachProjectedUtil: 108` present in both branches.
  A client cannot distinguish a real HiGHS quantile forecast from the fallback.
- **Severity:** P1 (§11, §24). **Fix:** return `FORECAST_FAILURE` with a status;
  never emit a fabricated cone; log the exception.

### P1-2 · Forecast runs on hardcoded history, not project data
- **Component:** `api/forecast.py:22-27` (`_NORTH_INDIA_HISTORY`), and
  `app.py:83` passing **no `history_provider`** to `build_orchestrator`.
- **Current:** Even the real engine call forecasts a fabricated 24-point series.
  The orchestrator's own `forecast.demand` capability is starved of history,
  which is *why* the API bypasses it.
- **Severity:** P1. **Fix:** wire a project-scoped `history_provider`.

### P1-3 · Digital Twin is 100% mock; the real twin service has no consumer
- **Component:** `js/twin3d.js`, `js/map.js`; `twinService` /
  `mapTwinStateToFrontend` imported into `app.js` and **never called** (dead).
- **Current:** 2D and 3D twins render `PLANTS`/`DCS`/`MARKETS`/`LANES` from
  `data.js`. `/orchestrator/twin/*` is real and unused.
- **Severity:** P1 (§18, and falsifies the 9.2 report's S2 "real data" claim).

### P1-4 · Chatbot fabricates a business narrative on failure
- **Component:** `js/chatbot.js:204` → `generateAIResponse()`
- **Current:** On orchestrator/LLM failure the UI emits confident specifics —
  "96.7% On-time SLA", "Delhi NCR DC 94% utilization", "all 19 India facilities"
  — none of which came from the engine.
- **Severity:** P1 (§24, §16). **Fix:** show an explicit `LLM_FAILURE` state.

### P1-5 · Fabricated data-quality metrics on ingestion
- **Component:** `api/ingestion_dynamic.py:93-117`
- **Current:** `valid_rows = int(total_rows * 0.98)` and a literal
  `"validPct": 98.0` regardless of the actual data; `total_rows … or 100`
  invents 100 records for an empty upload. Violates §7's "every rejected record
  must have an explainable reason".
- **Severity:** P1.

### P1-6 · Home cockpit renders mocks first and keeps them on API failure
- **Component:** `js/app.js:859-872`
- **Current:** `renderHome()` paints mock KPIs; the authoritative fetch then
  overwrites **only 2 of the 4** mapped cards (cost, SLA — not peak utilisation,
  not carbon). `.catch(console.warn)` leaves fabricated values on screen with no
  error state. Violates §20 ("never show stale authoritative numbers as if they
  were current").
- **Severity:** P1.

---

## P2 — Important production capability missing

| ID | Component | Gap |
|---|---|---|
| P2-1 | `api/ingestion_dynamic.py` + `api/network_extractor.py` | Duplicate ingestion architecture competing with the 58-file real pipeline, on the **same `/api/ingestions` prefix**. Violates §5. |
| P2-2 | `AuthoritativeEvidencePackage` | Built in Phase 9.1, has **no HTTP surface**. §9's evidence→DTO→frontend chain terminates at the API boundary. |
| P2-3 | Persistence | Every store is in-memory; all projects, uploads, scenarios, decisions and traces vanish on restart. `ingestion/snapshot.py` is the only durable mechanism and is unused by `app/`. |
| P2-4 | Audit / trace UI | `/orchestrator/executions/<id>/trace` is real; no screen consumes it. |
| P2-5 | `api/kpis.py:27` | `_cached_context` is never invalidated — KPIs go stale silently after any state change. |
| P2-6 | Governance surface | Governance is authoritative in the engine, but the UI reads mock `GOVERNANCE_TIERS`; no UI path enforces `BLOCKED`. |
| P2-7 | Insight screen | `insight-detail.js` renders mock `HOME_INSIGHTS`; `/orchestrator/insights` is real and unused. |

---

## P3 — Non-critical integration / UX

| ID | Component | Gap |
|---|---|---|
| P3-1 | `kpi-mapper.js:37` | `val > 10000 ? val/100000 : val` — heuristic unit inference. A genuine ₹8,000 cost renders as "₹8000.00L". Unit must come from `KPIResult.unit`, not magnitude. |
| P3-2 | `api/ingestion_dynamic.py:99` | `file_mappings` loop-variable leak; correct only for single-file uploads. |
| P3-3 | `app.py:142` | `debug=True`, `host="0.0.0.0"` — dev configuration in the entrypoint. |
| P3-4 | `app.py:26` | `CORS(app)` — unrestricted origins. |
| P3-5 | No config separation | dev/test/prod not separated (§26). |

---

## P4 — Polish

`js/data.js` retains 1,475 lines of mock content that will be dead once P0–P2
land; `agent.js` mock `AGENT_STATE`; no loading skeletons on several panels.

---

## What is genuinely production-grade already

Recording this explicitly so the phase is not misread as "nothing works":

- The **entire orchestration spine** (planner → validator → graph → failure
  manager → executor → observer → adaptive policy → reasoning → governance),
  16 capabilities, built and tested across Phases 8.1–8.8.
- The **authoritative KPI layer** (Phase 9.1) and its `/api/kpis` surface —
  the one application endpoint that is fully correct.
- `kpi-mapper.js` — honours `KPIStatus`, never zero-fills, correct by design.
- `api-client.js` — timeouts, correlation IDs, normalized errors, bearer tokens.
- The **58-file ingestion pipeline** with blocking-question enforcement
  (`service.py:176-185` refuses finalization while blocking items remain).
- **LLM-optional operation**, verified live at `available: False`.
- **2,460 passing tests.**

The gap is not the engine. It is the ~1,300-line application layer bolted onto
it, and the frontend's parallel mock universe.

---

## Recommended execution order

1. **P0-1 + P0-2** — snapshot/project binding. Unblocks everything else; every
   other fix is cosmetic until real data flows.
2. **P0-4 → P0-3** — make the scenario API honest, then point the UI at it.
3. **P0-5** — authentication and route guards.
4. **P1-1, P1-5, P1-4** — remove every fabricated-fallback path.
5. **P1-2, P1-3, P1-6** — history provider, twin wiring, cockpit coverage.
6. **P2** — evidence-package DTO, retire duplicate ingestion, persistence.
