# NetGravity — Production Architecture

**Phase 10.0.** Describes what is implemented, not what is intended. Anything
not built is in `validation/phase_10_0/integration_gap_analysis.md`.

---

## 1. Shape of the system

```
BROWSER
  production.html  ──────────────────────────────────┐
  index.html (approved prototype, partially wired)   │
                                                     ▼
  js/integration/  api-client · project-context · services · mappers
                                                     │  bearer token
                                                     │  X-Request-ID
                                                     ▼
APPLICATION LAYER            app/backend/
  api/        auth · projects · kpis · scenarios · forecast · ingestion_dynamic
  services/   security (PBKDF2 + sessions) · project_registry · errors
                                                     │
                     project_id ──► snapshot_id ─────┤
                                                     ▼
ORCHESTRATOR                 netgravity/orchestrator/
  NLU → Planner (deterministic | LLM) → PlanValidator → ExecutionGraph
      → FailureManager (+ CircuitBreaker) → CapabilityExecutor
      → ResultObserver → AdaptiveDecisionPolicy → Reasoning → Governance
                                                     │
                                                     ▼
SPECIALIST ENGINES
  optimization (PuLP/HiGHS MILP) · forecasting (ETS/intermittent/quantile,
  sup-F breaks) · resilience (REI) · risk (RF) · carbon · twin · ingestion
                                                     │
                                                     ▼
AUTHORITATIVE KPI LAYER      orchestrator/metrics/registry.py
  KPIResult[T] (VALID | INSUFFICIENT_EVIDENCE | NOT_COMPUTABLE |
                INFEASIBLE | INVALID_INPUT)
  AuthoritativeEvidencePackage  ──►  /api/kpis/evidence
```

The orchestration spine and the engines were built in Phases 8.1–9.1 and are
unchanged by this phase. Phase 10.0 built the **application layer** above them
and corrected the frontend paths that bypassed them.

---

## 2. The binding that makes it a product

A project is analysable only when a network snapshot is bound to it:

```
upload → parse → detect → map → review → confirm      (netgravity/ingestion/)
      → CanonicalNetwork                              (ingestion/builder.py)
      → Orchestrator.register_network()               (state/stores.py)
      → snapshot_id                                   content-addressed
      → ProjectRecord.snapshot_id                     (services/project_registry.py)
      → OrchestratorRequest(network_snapshot_id=…)    pinned; always wins
```

Before this phase the chain was severed at step three: one process-global
orchestrator was built at import from a synthetic fixture, and every project
pointed at it. The primitives for the fix already existed and were already
tested; what was missing was the edge between them.

**Consequences that follow from the binding, not from extra machinery:**

- A project with no ingested data has `snapshot_id = None` and every analytical
  endpoint answers `409 NO_NETWORK_BOUND`. It never borrows another network.
- Two projects cannot share state, because every request pins its own snapshot.
- The bundled synthetic network still exists, but as an explicitly labelled
  demo workspace (`is_demo`, "Case-16 Demo Network (synthetic)"), not as the
  implicit default a real project inherits.

---

## 3. Authority rules the code enforces

| Question | Authority | Enforced by |
|---|---|---|
| What does the network cost? | `optimization/milp.py` | Only capability that writes `NetworkStateResult` |
| What will demand be? | `forecasting/` | `forecast.demand` capability; refuses without history |
| How exposed is a facility? | `resilience/rei.py` | `resilience.assess` |
| What is the risk factor? | `risk/risk_factor.py` (`RF = P + REI − P·REI`) | `risk.compute_rf` |
| Is a number trustworthy? | `KPIResult.status` | Constructor invariant: a non-VALID status may not carry a value |
| What may the LLM assert? | `validation/numeric_grounding.py` | `_FACT_SPEC` whitelist |
| May an action execute? | `governance/` | `governance.classify` |

The LLM never calculates, never executes a tool, and never overrides a solver
result. Verified live with the gateway both configured and unavailable.

---

## 4. Failure vocabulary

Every layer fails explicitly (brief §24). No failure becomes a business value.

| Code | Meaning |
|---|---|
| `UNAUTHENTICATED` / `FORBIDDEN` | No valid session / not your project |
| `NO_NETWORK_BOUND` | Project exists, has no ingested network |
| `VALIDATION_ERROR` | Malformed request |
| `INGESTION_ERROR` | No uploaded file could be parsed |
| `FORECAST_FAILURE` / `FORECAST_UNAVAILABLE` | Engine failed / no observed history |
| `CAPABILITY_FAILURE` | A capability raised during execution |
| `ENGINE_UNAVAILABLE` | Orchestrator not mounted |
| `KPIStatus.INFEASIBLE` | The solve was infeasible — not a cheaper network |
| `KPIStatus.INSUFFICIENT_EVIDENCE` | Inputs absent — not zero |

---

## 5. Frontend

Two entry points, deliberately:

- **`production.html`** — the production console. Imports no mock data. Every
  figure is a `KPIResult` rendered with its status; a non-VALID metric renders
  "Unavailable"/"Infeasible" with a badge, never a number. Uses the prototype's
  stylesheet and design tokens, so the visual language is unchanged.
- **`index.html`** — the approved prototype. Its authoritative paths were
  corrected (scenario creation, chatbot failure) but several screens still read
  `js/data.js`. See the gap analysis; this is stated, not hidden.

`js/integration/` is shared by both: `api-client` (timeouts, correlation IDs,
normalized errors, bearer token), `project-context` (active project + change
notification), typed services, and mappers that preserve status.

---

## 6. Configuration

| Variable | Default | Purpose |
|---|---|---|
| `NETGRAVITY_ENV` | `development` | `production` disables debug and tightens CORS |
| `NETGRAVITY_CORS_ORIGINS` | *(empty)* | Comma-separated allowlist; empty ⇒ same-origin |
| `NETGRAVITY_HOST` / `_PORT` | `127.0.0.1` / `5050` | Bind address |
| `NETGRAVITY_DEBUG` | `0` | Opt-in; ignored in production |
| `NETGRAVITY_MAX_UPLOAD_BYTES` | 64 MiB | Request body cap |
| `NETGRAVITY_SEED_DEMO` | `1` | Seed the synthetic demo workspace |
| `TEXT_API_URL` / `TEXT_API_TOKEN` | — | LLM gateway; absent ⇒ deterministic |

No credential appears in source; `.env` is gitignored.

---

Deployment configuration, migrations, backups and the identity
controls are documented separately in **[operations.md](operations.md)**.

## 7. What this architecture does not yet provide

Stated here so the document cannot be read as a completeness claim:

- ~~**Durable persistence.**~~ Delivered. Accounts, sessions, projects,
  snapshots, uploaded history, the analysis computed from a network, and solved
  scenarios are stored in **PostgreSQL** (`NETGRAVITY_DATABASE_URL`), with a
  SQLite fallback for a machine with no database server. `/api/status` names
  which is in use. Execution traces remain deliberately unpersisted: an
  execution is one in-flight run, and its artefacts are stored individually, so
  a restart loses the workings and keeps the answers. Migrate an existing
  SQLite store with `scripts/migrate_to_postgres.py`, which verifies every row
  byte-for-byte after copying.
- **Database operations.** There is a schema and a migration script, but no
  backup, no point-in-time recovery, no enforced connection encryption and no
  migration versioning beyond `CREATE TABLE IF NOT EXISTS`.
- **Ingestion → project binding through the UI.** `ProjectRegistry.bind_network`
  exists and is tested, but the canonical ingestion console's finalize step is
  not yet wired to call it; the demo workspace is the only bound project.
- **Rate limiting** on login and on the (expensive) solve endpoints.
- **Twin, insight and governance screens in the prototype** still read mock data.

These are tracked with severities in
`validation/phase_10_0/integration_gap_analysis.md`.
