# Phase 10.0 — Production Readiness Matrix

Assessed against the brief's §36 acceptance criteria. Status recorded as of the
**forensic baseline** (Phases A–C complete, before any Phase 10.0 code change).

Legend: ✅ met · ⚠️ partial · ❌ not met

---

## §36 acceptance criteria — baseline assessment

| # | Criterion | Baseline | Evidence |
|---|---|:--:|---|
| 1 | Existing architecture fully mapped | ✅ | `architecture_inventory.md`, `module_dependency_map.md` |
| 2 | Existing modules reused | ⚠️ | Engine reused; but 2 duplicates exist (`ingestion_dynamic`, frontend `SCENARIOS`) |
| 3 | No duplicate orchestration architecture | ✅ | Single orchestrator; no second planner/executor/state store |
| 4 | Data ingestion works end-to-end | ⚠️ | Pipeline works; **output never reaches the engine** (P0-1) |
| 5 | Project lifecycle works | ❌ | Projects are a hardcoded list; no snapshot binding (P0-2) |
| 6 | Canonical network works | ⚠️ | `builder.py` produces it; nothing consumes it |
| 7 | Dashboard uses backend data | ⚠️ | 2 of 4 KPI cards; mocks retained on failure (P1-6) |
| 8 | KPIs use authoritative layer | ✅ | `/api/kpis` → `KPIRegistry` → `KPIResult`, verified |
| 9 | Forecast uses real forecasting engine | ⚠️ | Real engine, **fabricated history + fabricated fallback** (P1-1, P1-2) |
| 10 | Scenarios use real solver | ❌ | UI fabricates client-side; API discards its own solve (P0-3, P0-4) |
| 11 | Resilience works | ✅ | `resilience/rei.py` real, tested, reachable via capability |
| 12 | Risk isolation works | ✅ | `RF = P + REI − P·REI`; `NOT_COMPUTABLE` never defaults to 0 |
| 13 | Digital Twin works | ❌ | Service real; **UI is 100% mock, `twinService` dead code** (P1-3) |
| 14 | Chat / NLU works | ⚠️ | Real; fabricated narrative on failure (P1-4) |
| 15 | LLM planner works when available | ✅ | `planner/llm_planner.py` present |
| 16 | Deterministic fallback works | ✅ | Verified live at `llm.available: False` |
| 17 | PlanValidator remains authoritative | ✅ | `core/plan_graph.py` |
| 18 | FailureManager remains authoritative | ✅ | `core/failure_manager.py` + circuit breaker |
| 19 | Adaptive execution works | ✅ | `core/adaptive_policy.py`, `result_observer.py` |
| 20 | Reasoning is evidence-grounded | ✅ | `validation/numeric_grounding.py` `_FACT_SPEC` whitelist |
| 21 | Governance cannot be bypassed | ⚠️ | Engine-side sound; **no UI enforcement path** (P2-6) |
| 22 | Audit / provenance exists | ⚠️ | Trace API real; no consumer, nothing persisted (P2-3, P2-4) |
| 23 | Frontend has no authoritative KPI calculations | ❌ | `scenarios.js:1081-1140` fabricates cost/SLA/carbon (P0-3) |
| 24 | Project isolation works | ❌ | All state process-global and unkeyed (P0-2) |
| 25 | Security review completed | ✅ | This document + `integration_gap_analysis.md` §P0-5, §P3 |
| 26 | Existing tests remain intact | ✅ | 2,460 passed · 4 skipped · 0 failed · 128.84s |
| 27 | New integration tests pass | ⚠️ | 14 exist (Phase 9.2); contract-shape only, not authenticity |
| 28 | Full E2E workflow passes | ❌ | Steps 4–10 of §29 (upload → canonical → snapshot) are severed |
| 29 | Production gaps documented | ✅ | This artifact set |

**Baseline score: 12 ✅ · 10 ⚠️ · 7 ❌ of 29.**

---

## Verdict at baseline

**NOT PRODUCTION READY.**

This verdict is not driven by test failures — the suite is green and the engine
is genuinely strong. It is driven by a **provenance failure**: in three places
the application presents fabricated numbers to a decision-maker with an explicit
or implied claim that a solver produced them.

- `scenarios.js:1136` labels invented figures `"MILP verified"` and
  `"Branch-and-Cut (Exact)"`.
- `api/scenarios.py:229` discards a real solve and returns literals.
- `api/forecast.py:138` returns a fabricated cone that is byte-indistinguishable
  from a real quantile forecast.

For a decision-support product, that is a more serious defect than an outage:
an outage is visible, and this is not.

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| A user acts on a fabricated scenario believing it solver-verified | **High** (default UI path) | **Severe** | P0-3, P0-4 |
| Analysis presented for a network the customer never uploaded | **Certain** today | **Severe** | P0-1 |
| Cross-project data exposure in multi-user use | High | Severe | P0-2 |
| Unauthenticated access to all analysis endpoints | **Certain** today | High | P0-5 |
| Forecast fallback mistaken for a real forecast | Medium | High | P1-1 |
| Total state loss on restart | **Certain** | Medium | P2-3 |

---

## What "production ready" would require

Beyond P0/P1 closure, evidence — not assertion — on:

1. A durable store for projects, snapshots, scenarios, decisions and traces.
2. Authenticated, authorized, project-scoped access on every route.
3. An E2E test that ingests a file and asserts the dashboard's numbers derive
   from *that* file's snapshot — the §29 journey, executed.
4. Observability per §23 (request/project/plan/step/capability IDs correlated).
5. Load characterisation of MILP and forecast paths under concurrency.

Items 1, 4 and 5 are genuinely out of reach within this phase and are stated as
remaining gaps rather than quietly deferred.
