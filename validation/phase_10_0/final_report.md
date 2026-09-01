# PHASE 10.0 — Production Application Build: Final Report

**Date:** 2026-08-30 · **Branch:** `Backend-wiring` (local only; no git operation performed)

---

## 1. Executive summary

NetGravity's **engine** was already production-grade: ~53,000 lines of
orchestration, optimization, forecasting, resilience, risk and ingestion code
behind 2,460 passing tests, built across Phases 8.1–9.1. The **application layer
bolted on top of it was not**, and the gap between the two was not merely
"unfinished wiring".

The forensic audit found that in five places the application presented
**fabricated numbers to a decision-maker while claiming a solver had produced
them**:

- The prototype's "Create scenario" button animated six fake progress steps and
  then pushed a literal object — `totalCost: 1220000`, `sla: 96.5`,
  `carbonKg: 101200`, `robustnessTests: [{status: 'PASS'}]`,
  `'Solver Execution: Branch-and-Cut (Exact)'`, `note: 'MILP verified'` — into
  the comparison table. **No request was ever made.**
- `/api/scenarios/simulate` ran a real orchestrator solve, obtained real
  `ScenarioMetricDelta` objects, and then returned hardcoded values instead.
- Every file upload was solved by a **second, independent MILP** with invented
  freight rates and straight-line "distances", which returned
  `fillRate: 100.0` and `slaAdherence: 96.5` as literals.
- The forecast endpoint caught every exception with `pass` and returned a
  hardcoded P10/P50/P90 cone byte-indistinguishable from a real one.
- The chatbot answered engine failures with a confident briefing citing
  "96.7% On-time SLA" and "all 19 India facilities".

Underneath, authentication accepted any password, auto-created accounts for
unknown emails, and returned a default authenticated user for tokenless
requests; all state was process-global, so project isolation was structurally
impossible; and the whole system analysed one synthetic fixture regardless of
what a customer uploaded.

All of the above is fixed. The system now refuses rather than invents. It is
**not yet production-ready** — three blockers remain, listed in §23 — but the
class of defect that made it dangerous is gone.

---

## 2. Existing architecture (what was already there)

Verified by reading source, not documentation:

- **Orchestration spine, complete and correct:** `CapabilityRegistry`,
  `CapabilityExecutor`, `FailureManager` + `CircuitBreaker`, `ExecutionContext`,
  `ExecutionStateStore`, deterministic `plan_graph` planner, `LLMPlanner`,
  `PlanValidator`, `ResultObserver`, `AdaptiveDecisionPolicy`, Governance,
  Reasoning, Digital Twin. **16 registered capabilities.**
- **Authoritative KPI layer (Phase 9.1):** `KPIResult[T]` with a constructor
  invariant that a non-VALID status cannot carry a value; `KPIRegistry`;
  a 17-entry threshold catalogue; `AuthoritativeEvidencePackage`.
- **Engines:** PuLP/HiGHS MILP; ETS/intermittent/quantile forecasting with
  sup-F structural-break detection; REI; `RF = P + REI − P·REI`; carbon.
- **A 58-file ingestion pipeline** that produces a real `CanonicalNetwork` and
  refuses to finalize while blocking mapping questions remain.
- **`SnapshotManager`** — content-addressed, thread-safe, deep-copying, and
  already multi-snapshot capable, with `OrchestratorRequest.network_snapshot_id`
  documented to always win over the current snapshot.

No orchestration, planner, executor, state store, KPI engine or solver was
built in this phase. None needed to be.

---

## 3. Production architecture

`docs/production_architecture.md`. In brief: a thin **application layer**
(`app/backend/api` + `app/backend/services`) owns identity, project ownership
and snapshot binding, and delegates every calculation to the orchestrator.
A **production console** (`app/frontend/production.html`) renders only
authoritative results, each with its status.

---

## 4. What already existed → 5. What was connected → 6. What was built

**Reused unchanged:** the entire orchestration spine, all engines, the KPI
registry, the threshold catalogue, `SnapshotManager`, the ingestion pipeline,
`api-client.js`, `kpi-mapper.js`.

**Connected (existed but had no consumer):**

| Capability | Now reachable via |
|---|---|
| `SnapshotManager.register()` / `Orchestrator.register_network()` | `ProjectRegistry.bind_network()` |
| `AuthoritativeEvidencePackage` | `GET /api/kpis/evidence` |
| `forecast.demand` capability + `load_staging_history` | `GET /api/forecast` with a real `history_provider` |
| `KPIRegistry.scenario_comparison()` | `POST /api/scenarios/simulate` |
| `KPIRegistry.facility_kpis()` | `GET /api/kpis/facilities` |

**Built (new, ~1,000 lines application + ~1,200 frontend):**

| File | Purpose |
|---|---|
| `app/backend/services/security.py` | PBKDF2-HMAC-SHA256 credentials, sessions, `@require_auth` |
| `app/backend/services/project_registry.py` | Project ownership + snapshot binding |
| `app/backend/services/errors.py` | Typed application error taxonomy |
| `app/frontend/production.html` + `css/production.css` | Production console, prototype's design tokens |
| `app/frontend/js/production/main.js` | Console controller; imports no mock data |
| `app/frontend/js/integration/project-context.js` | Active-project scope |

**Rewritten:** `api/auth.py`, `api/projects.py`, `api/scenarios.py`,
`api/forecast.py`, `api/kpis.py`, `api/ingestion_dynamic.py`, `app.py`,
`mappers/scenario-mapper.js`, and the scenario/chat paths in the prototype.

**Removed:** `network_extractor.py::solve_extracted_network` (duplicate MILP);
`chatbot.js::generateAIResponse` + `FAQ_KNOWLEDGE_BASE` (fabrication generator).

---

## 7. What was not built

Stated plainly, not buried: durable persistence; the
`ingestion.finalize() → bind_network()` join; authentication on
`/orchestrator/*`; rate limiting; the prototype's twin/insight/governance
screens; load testing.

---

## 8–10. Ingestion · API · Frontend

**Ingestion.** Upload guardrails added (extension allowlist, 25 MB/file,
10 files/request, size measured from the stream, not Content-Length). Data
quality is now **measured** — duplicates, empty rows, null density, sparse
columns, each issue carrying its count — replacing a literal `validPct: 98.0`.
The preview no longer optimises. A latent defect was found and fixed while
removing the duplicate solver: `Path` was used without being imported inside a
bare `except`, so **every CSV upload silently parsed to zero tables**.

**API.** 47 routes. Eight anonymous-access probes verified: `/api/status` 200,
all seven scoped routes 401. Every application error serializes to one envelope.

**Frontend.** Two entry points. The production console is fully authoritative.
The approved prototype had its two fabrication paths corrected; its twin,
insight, facility and governance screens still read `js/data.js`, which
`docs/production_integration_map.md` records screen by screen.

---

## 11–18. Authority

| Domain | Owner | Verified |
|---|---|---|
| KPIs | `KPIRegistry` | 18/18 carry a status; **0 fabricate a value** |
| Optimization | `optimization/milp.py` | Duplicate solver removed; scenarios carry `execution_id` |
| Forecasting | `netgravity/forecasting` | Routed via capability; `FORECAST_UNAVAILABLE` when no history |
| Scenario | MILP + `scenario_comparison` | Baseline recomputed from snapshot, never mutated |
| Resilience / Risk | `rei.py`, `risk_factor.py` | `risk_factor` delta present in scenario output |
| Reasoning | evidence-grounded | `_FACT_SPEC` unchanged |
| Governance | `governance/` | Engine sound; UI enforcement path still missing |
| Twin | `twin/service.py` | API real; prototype UI still mock |

**GAP-01 closed.** Phase 9.1 documented five metrics computed by `compute_kpis()`
but dropped at the `metrics/contracts.py` bridge. They are now carried across as
`Optional[float] = None` — Optional, not `0.0`, so "not reported" stays
distinguishable from a measured zero. Network KPI coverage moved from
**13/18 VALID to 18/18**.

---

## 19. Security

Six P0/P1/P3 findings resolved (password verification, auto-provisioning,
anonymous fallback, authorization, upload validation, debug/CORS config).
Four remain open and are recorded in `security_findings.json`: non-durable
credential store, no rate limiting, no CSRF defence for cookie deployments,
prompt-injection surface untested.

**No security certification is claimed.**

---

## 20–21. Observability · Performance

Every KPI carries `snapshot_id`, `execution_id`, `formula_id`,
`source_capability` and `authoritative_owner`. Requests carry `X-Request-ID`.
Traces exist at `/orchestrator/executions/<id>/trace` but are not persisted.

Measured (Case-16, 7 facilities): `/api/status` 0.3 ms · `/api/kpis/network`
(cached) 0.5 ms · `/api/kpis/evidence` 0.9 ms · full MILP scenario solve 43 ms.
The KPI context cache has a 120 s TTL keyed by snapshot, replacing a cache that
was never invalidated. Scenario solves are deliberately uncached. **No load
testing at production scale was performed.**

---

## 22. Test results

| Suite | Result |
|---|---|
| Full regression, baseline | **2,460 passed · 4 skipped · 0 failed** (128.84 s) |
| Full regression, final | **2,501 passed · 4 skipped · 0 failed** (140.81 s)  
| | *(+41 = 32 new production-application tests + 8 added to API wiring + 1 from splitting the GAP-01 test)*** |
| New integration tests | **+38** (30 production-application, 8 added to API wiring) |
| E2E API validation | **20 / 20** |
| Production UI validation (browser) | **13 / 13** |

No test was deleted. Two files were **updated** rather than weakened, because
they asserted behaviour that was itself the defect:

- `test_frontend_api_wiring.py` asserted `/api/auth/me` returns 200 **with no
  Authorization header** — the authentication bypass, encoded as a contract. It
  also asserted `>= 5` projects and `>= 2` scenarios, which held only because
  those were hardcoded fabrications. Every test was retargeted to the corrected
  contract and 8 tests were added covering the properties that replaced the
  defects.
- `test_kpi_authoritative_layer.py::test_documented_data_gap_is_reported_not_fabricated`
  asserted the GAP-01 metrics stay INSUFFICIENT_EVIDENCE. With the gap closed
  they are VALID. It was split into two tests: one proving they now arrive, one
  proving a solve that omits them still reports INSUFFICIENT_EVIDENCE rather
  than zero — preserving the original intent exactly.

---

## 23. Remaining gaps · 24. Production risks

**P0 — blocking**

1. **`ingestion.finalize()` does not call `bind_network()`.** Both halves exist
   and are tested; the join does not. A customer cannot yet analyse their own
   data through the UI. *Highest-value next task.*
2. **`/orchestrator/*` is unauthenticated.** It predates the application auth
   layer. Must not be publicly exposed as-is.
3. **Nothing persists.** All state is in-process; a restart loses every
   workspace, and a second worker shares nothing.

**P1** — no rate limiting on login or solve; prototype twin/insight/governance
screens still mock; traces not persisted.

**P2** — no load characterisation; prompt-injection untested; `data.js` retains
1,475 lines of mock content that will be dead once P1 lands.

**Risks if deployed today:** total state loss on restart; unauthenticated access
to the control plane; and — because only the labelled demo workspace is bound —
a user could mistake synthetic demonstration figures for their own network, the
one residual instance of the class of problem this phase set out to remove.

---

## 25. Production readiness assessment

**NOT PRODUCTION-READY.**

Acceptance criteria moved from **12 ✅ / 10 ⚠️ / 7 ❌** to **23 ✅ / 6 ⚠️ / 0 ❌**,
and the suite is green — but neither fact establishes readiness. The verdict
rests on §23's three blockers: without persistence, without customer-data
binding, and with an open control plane, this is a validated pilot system, not a
deployable product.

What *can* be said with evidence: **the application no longer fabricates
business figures, and no longer claims a solver produced numbers it did not.**
Every KPI on every screen is traceable to the engine that computed it, and
anything that cannot be computed says so.

---

## 26. Recommended next steps

1. Join `ingestion.finalize()` to `ProjectRegistry.bind_network()`; add an E2E
   test that uploads a file and asserts the dashboard's numbers derive from
   *that* file's snapshot.
2. Put `/orchestrator/*` behind `@require_auth` with project scoping.
3. Introduce a durable store (SQLite is sufficient) for accounts, projects,
   snapshot index, scenarios and traces.
4. Rate-limit login and solve endpoints.
5. Retire the prototype's mock screens in favour of the production console, or
   wire them to their existing real APIs.
6. Load-characterise MILP and forecast at realistic network scale.

Only after 1–3 is a production pilot defensible.
