# NetGravity — Production Readiness

**Verdict: NOT production-ready. Substantially closer than at the start of
Phase 10.0, and honest about the distance remaining.**

This document exists because the brief asks that readiness be justified by
evidence rather than asserted from a green test run. The suite is green. That
is necessary and not sufficient.

---

## 1. What changed the risk profile most

The Phase 10.0 audit found a class of defect more dangerous than an outage: the
application presented **fabricated numbers with an explicit claim that a solver
produced them**. An outage is visible; this was not.

| Defect | Evidence | State |
|---|---|---|
| Client-side scenario fabrication labelled "MILP verified" | `scenarios.js:1081-1140` pushed `{totalCost: 1220000, sla: 96.5, carbonKg: 101200}` with `robustnessTests:[PASS]` and `'Solver Execution: Branch-and-Cut (Exact)'`, making **no request at all** | **FIXED** — solves through the engine |
| Scenario API discarded its own real solve | `api/scenarios.py` ran the orchestrator, obtained real deltas, then returned literals | **FIXED** — returns authoritative KPIs + deltas |
| Second independent MILP on every upload | `network_extractor.py::solve_extracted_network` — invented freight rates, straight-line "distance", hardcoded `fillRate: 100.0`, `slaAdherence: 96.5` | **REMOVED** |
| Forecast fabricated a cone on any exception | `api/forecast.py` `except Exception: pass` → hardcoded P10/P50/P90, indistinguishable from a real forecast | **FIXED** — explicit status, never a cone |
| Chatbot invented a business briefing on failure | `chatbot.js` emitted "96.7% On-time SLA … all 19 India facilities" | **FIXED** — generator and FAQ deleted |
| Fabricated data-quality claim | `validPct: 98.0` asserted on real customer files | **FIXED** — measured |
| Analysis ran on synthetic data regardless of upload | One global orchestrator from `build_case16_network()` | **FIXED** — project→snapshot binding |
| Authentication was decorative | Any password accepted; unknown emails auto-provisioned; `/me` returned a default user | **FIXED** — PBKDF2, no fallback, route guards |
| No project isolation | All state process-global, projects had no owner | **FIXED** — ownership enforced, 403 on cross-owner |

---

## 2. Acceptance criteria (§36)

| # | Criterion | Baseline | Now | Evidence |
|---|---|:--:|:--:|---|
| 1 | Architecture mapped | ✅ | ✅ | `architecture_inventory.md`, `module_dependency_map.md` |
| 2 | Existing modules reused | ⚠️ | ✅ | Duplicate MILP removed; no new engine |
| 3 | No duplicate orchestration | ✅ | ✅ | Single orchestrator, 16 capabilities |
| 4 | Ingestion works end-to-end | ⚠️ | ⚠️ | Pipeline + preview real; **finalize→bind not wired** |
| 5 | Project lifecycle | ❌ | ✅ | E2E-05/06/08/09 |
| 6 | Canonical network | ⚠️ | ✅ | `bind_network` implemented and tested |
| 7 | Dashboard uses backend data | ⚠️ | ✅ | Production console: 18/18 authoritative |
| 8 | KPIs authoritative | ✅ | ✅ | `kpi_validation.json` — 0 fabricated values |
| 9 | Forecast uses real engine | ⚠️ | ✅ | Routed via `forecast.demand`; honest status |
| 10 | Scenarios use real solver | ❌ | ✅ | E2E-12/13, UI-09/10 |
| 11 | Resilience works | ✅ | ✅ | REI computed in scenario runs |
| 12 | Risk isolation | ✅ | ✅ | `risk_factor` delta present |
| 13 | Digital twin works | ❌ | ⚠️ | API real; **prototype UI still mock** |
| 14 | Chat / NLU | ⚠️ | ✅ | Honest failure state |
| 15 | LLM planner when available | ✅ | ✅ | `planner/llm_planner.py` |
| 16 | Deterministic fallback | ✅ | ✅ | Verified with gateway absent |
| 17 | PlanValidator authoritative | ✅ | ✅ | unchanged |
| 18 | FailureManager authoritative | ✅ | ✅ | unchanged |
| 19 | Adaptive execution | ✅ | ✅ | unchanged |
| 20 | Reasoning evidence-grounded | ✅ | ✅ | `_FACT_SPEC` unchanged |
| 21 | Governance not bypassable | ⚠️ | ⚠️ | Engine sound; **no UI enforcement path** |
| 22 | Audit / provenance | ⚠️ | ✅ | Every KPI carries provenance; traces persisted to `execution_traces` and read back on a buffer miss (10.9) |
| 23 | No frontend KPI calculation | ❌ | ✅ | Structural tests on literals |
| 24 | Project isolation | ❌ | ✅ | E2E-08/09 |
| 25 | Security review | ✅ | ✅ | `security_findings.json` |
| 26 | Existing tests intact | ✅ | ✅ | No test deleted or weakened |
| 27 | New integration tests | ⚠️ | ✅ | +38 tests |
| 28 | Full E2E passes | ❌ | ⚠️ | 20/20 API + 13/13 UI; **upload→bind step absent** |
| 29 | Gaps documented | ✅ | ✅ | this document |

**23 ✅ · 6 ⚠️ · 0 ❌** (baseline: 12 ✅ · 10 ⚠️ · 7 ❌).

---

## 3. Why the verdict is still "not ready"

The three blockers this section named through Phase 10.0 are closed. What
replaces them is a shorter, more specific list.

**Closed since:**

| Blocker | Closed by |
|---|---|
| Nothing persists | PostgreSQL, with a versioned migrated schema, verified backups, and a cold-restart check that rebuilds every module and recovers the account, project, network, analysis and scenarios (10.6, 10.7, 10.8) |
| A customer cannot bind their own data | `ingestion.finalize()` binds the network; the client workbook is analysed end to end in every harness since (10.1) |
| The control plane is unauthenticated | `/orchestrator/*` fails closed behind a blueprint-wide `before_request`; a bare request gets 401 (10.6) |
| No rate limiting | Credential and solve endpoints both limited, with `Retry-After` (10.8) |
| Session token in `localStorage` | httpOnly + SameSite cookie, double-submit CSRF, and a CSP that addresses the injection rather than only the prize (10.8) |
| No reset, MFA or lockout | All three, with the lockout in the database and TOTP verified against RFC 6238's own vectors (10.8) |

| No point-in-time recovery | `scripts/pitr.py` configures WAL archiving, takes the physical base backup a dump cannot substitute for, and **drills a recovery** — verified on PostgreSQL 16.4, keeping a transaction committed before the target and excluding one after it (10.9) |
| No identity provider integration | OpenID Connect, authorization code + PKCE, linked on `(issuer, subject)`; the ID token verifier is tested against `alg: none`, algorithm confusion, key smuggling, wrong audience, expiry and nonce replay (10.9) |
| No load characterisation | Two measured profiles: 8 users at 1 s think time, worst p99 **31 ms**; and a flood finding the ceiling at 397 completed requests/second with 0 server errors (10.9) |
| Rate-limit counters per process | One shared row per (bucket, client), atomic upsert; four threads consumed 25 of 40 against a limit of 25 (10.9) |
| The MILP is single-period | `FULL_HORIZON` models every period with stock carried between them; 24 periods is 23× the variables and 7× the time, solving in 0.21 s (10.9) |
| Execution traces not persisted | Written through to `execution_traces` and read back on a miss; `from_dict` is the pinned inverse of `to_dict` (10.9) |
| Uploaded signals not routed into the forecast | Attached to the forecast request and routed through the orchestrator's own rules; the response reports both signals attached and series actually adjusted (10.9) |
| No contract parser | Lease and minimum-term clauses become `FacilityCommitment` and set the fields constraint C5c enforces — which nothing had ever set, so the constraint was permanently inert (10.9) |
| The insight feed was always empty | Nothing wrote `HOME_INSIGHTS`, and the template emitted one theme. Nine now — service, capacity, utilisation, cost, cost structure, footprint, resilience, carbon, scenario impact — each with a severity the engine states, fetched by `/api/insights` and verified in a browser (10.9) |

**Still open, in the order they matter:**

1. **No SCIM or directory sync.** SSO authenticates; it does not deprovision.
   Removing a leaver from the directory stops future sign-ins and does not end
   a session already issued here. Run "sign out everywhere", or shorten the
   absolute session deadline. No SAML either.
2. **The load measurement is of the development server.** Werkzeug, not
   gunicorn, because gunicorn does not run on this platform. The latencies are
   a floor for a production deployment, not a prediction of it.
3. **Solve scaling is measured on one fixture.** 24 periods in 0.21 s on 15
   facilities says the horizon model is affordable; it says nothing about a
   client network an order of magnitude larger. Measure before trusting the
   300 s worker timeout against one.
4. **PITR ships WAL to a local directory by default**, which protects against
   everything except losing the machine. `--archive-command` is the seam;
   choosing and crediting an object-storage target is a deployment decision.
5. **The horizon model has stated limits.** A plant cannot build ahead (its
   outbound flow IS its production), cross-docks hold no stock, and facility
   opening is one decision for the whole horizon rather than a phased build.
6. **The ID token verifier is hand-written**, because no JWT library is
   installed here. It is tested against the attacks that matter; a maintained
   library is still preferable if dependencies may be added.
7. **Contract extraction depends on a model reading a document.** Every clause
   carries its source excerpt and confidence for that reason. A commitment that
   pins a site open should be read by a person before a plan is built on it.

---

## 4. Evidence index

| Artifact | Contents |
|---|---|
| `validation/phase_10_0/e2e_validation.json` | 20 API-level checks, all passing |
| `validation/phase_10_0/production_ui_validation.json` | 13 browser checks, all passing |
| `validation/phase_10_0/kpi_validation.json` | 18 KPIs; 0 fabricate a value |
| `validation/phase_10_0/api_validation.json` | 47 routes; 8 anonymous probes correct |
| `validation/phase_10_0/ingestion_validation.json` | Measured quality; `.exe` rejected |
| `validation/phase_10_0/agentic_validation.json` | 16 capabilities; control-plane modules present |
| `validation/phase_10_0/security_findings.json` | 6 resolved, 4 open |
| `validation/phase_10_0/performance_observations.json` | Latency measurements |
| `validation/phase_10_0/screenshots/` | 9 production-console screenshots |

---

## 5. Route to production

**P0 — deployment decisions this repository cannot make for you**
1. Deploy behind TLS with `NETGRAVITY_ENV=production` set, so Secure cookies,
   HSTS, same-origin CORS and the refusal of an unencrypted database
   connection are all in force.
2. Configure a real password-reset channel (`smtp` or `webhook`).
   `/api/status` reports `reset_delivery.configured` and will say `false` until
   you do.
3. Point `pitr.py configure --archive-command` at object storage, not the local
   directory it defaults to, and run `pitr.py drill` on a schedule. The
   mechanism is verified; where the archive lives is yours.

**P1**
4. Load-characterise against a **production** WSGI server and a client-scale
   network. `scripts/load_test.py` is the instrument; the numbers in
   `validation/phase_10_9/` are from the development server and one fixture.
5. Configure OIDC if you have a provider (`docs/operations.md` §3), and decide
   `AUTO_PROVISION` deliberately — a provider usually authenticates a whole
   directory, and being authenticated is not being invited.
6. Have a leaver process. SSO does not deprovision, and there is no SCIM
   endpoint.

**P2**
7. Wire the prototype's remaining twin/governance screens to their real APIs,
   or retire them in favour of the production console.
8. Replace the hand-written ID token verifier with PyJWT or Authlib if
   dependencies may be added.
9. Read every extracted contract commitment before acting on a plan it pins
   open.

---

## 6. Where the evidence lives

`docs/operations.md` — production configuration, migrations, backups, PITR,
identity controls, rate limits, and what the model does and does not do.

`validation/phase_10_9/production_readiness_report.md` — the most recent pass:
the multi-period MILP, the insight generator, PITR verified by drill, shared
rate-limit counters, persisted traces, contractual site commitments, OIDC, and
the two measured load profiles. Each item carries the defect behind it and the
measurement that closed it.

`validation/phase_10_8/production_readiness_report.md` — the pass before, which
this one's opening list was taken from.
