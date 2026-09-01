# NetGravity — Production API Map

Every route registered by the application, its authority, and its scoping.
Generated against the running app; the machine-readable form is
`validation/phase_10_0/api_validation.json` (47 rules total, including the
orchestrator control plane and the canonical ingestion pipeline).

**Auth** — `require` means a valid `Authorization: Bearer <token>` is mandatory
and the route returns `401` without one. **Scope** — `project` means the route
takes `project_id`, verifies ownership, and resolves the project's snapshot.

---

## Application API — `app/backend/api/`

| Method | Route | Auth | Scope | Authoritative source | Notes |
|---|---|:--:|:--:|---|---|
| POST | `/api/auth/signup` | public | — | `services/security.py` | PBKDF2; ≥8-char password required |
| POST | `/api/auth/login` | public | — | `services/security.py` | 401 on wrong password *or* unknown email, equal timing |
| GET | `/api/auth/me` | require | — | session store | No anonymous fallback |
| POST | `/api/auth/logout` | public | — | session store | Idempotent |
| GET | `/api/projects` | require | owner | `ProjectRegistry` | Own projects + shared demo only |
| POST | `/api/projects` | require | owner | `ProjectRegistry` | Starts with `snapshot_id: null` |
| GET | `/api/projects/<id>` | require | project | `ProjectRegistry` | `403` cross-owner, `404` absent |
| PUT/PATCH | `/api/projects/<id>` | require | project | `ProjectRegistry` | `snapshot_id` is not client-writable |
| GET | `/api/projects/<id>/snapshot` | require | project | `SnapshotManager` | `409 NO_NETWORK_BOUND` when unbound |
| GET | `/api/kpis/network` | require | project | `KPIRegistry.network_kpis` | 18 KPIs, each with `KPIStatus` |
| GET | `/api/kpis/facilities` | require | project | `KPIRegistry.facility_kpis` | Per-facility utilisation/throughput |
| GET | `/api/kpis/facilities/<id>` | require | project | + resilience + risk | `404` if absent from the network |
| GET | `/api/kpis/evidence` | require | project | `AuthoritativeEvidencePackage` | New in 10.0 — closes gap P2-2 |
| GET | `/api/kpis/thresholds` | require | — | `build_threshold_catalogue()` | Platform policy; carries no customer data |
| GET | `/api/scenarios` | require | project | per-project store | Empty until a scenario is solved |
| GET | `/api/scenarios/baseline` | require | project | `optimization.solve` | Recomputed from the snapshot; immutable |
| POST | `/api/scenarios/simulate` | require | project | MILP + `scenario_comparison` | Real solve; `502 CAPABILITY_FAILURE` on error |
| GET | `/api/scenarios/<id>` | require | project | per-project store | `404` outside the project |
| GET | `/api/forecast` | require | project | `forecast.demand` capability | `OK` or `FORECAST_UNAVAILABLE`; never a fabricated cone |
| GET | `/api/signals` | require | — | configured `signal_provider` | `NO_SIGNAL_SOURCE_CONFIGURED` when none |
| POST | `/api/ingestions/preview/upload-and-parse` | require | project | pandas + `classify_column_name` | Preview only; **no optimisation** |
| GET | `/api/ingestions/preview/active` | require | project | per-project preview | `NO_PREVIEW` when nothing uploaded |
| GET | `/api/status` | public | — | mount status | Health only; no customer data |

## Canonical ingestion pipeline — `netgravity/ingestion/api.py`

| Method | Route | Notes |
|---|---|---|
| POST | `/api/ingestions` | Start a run |
| GET | `/api/ingestions/<run_id>` | Run state |
| GET | `/api/ingestions/<run_id>/draft` | Draft mapping |
| GET/POST | `/api/ingestions/<run_id>/reviews` | Clarification items |
| POST | `/api/ingestions/<run_id>/reviews/.../analyse` | AI-assisted mapping |
| POST | `/api/ingestions/<run_id>/finalize` | Refuses while blocking questions remain, and refuses if no `CanonicalNetwork` could be assembled |

> **Known gap.** `finalize` produces a real `CanonicalNetwork`, and
> `ProjectRegistry.bind_network()` is implemented and tested, but the two are
> not yet joined in the HTTP flow. Binding a customer network to a project is
> therefore not reachable from the UI in this phase. Tracked as the first item
> of remaining work.

## Orchestrator control plane — `netgravity/orchestrator/api.py`

| Method | Route | Purpose |
|---|---|---|
| POST | `/orchestrator/run` | Execute a request through the full agentic pipeline |
| POST | `/orchestrator/chat` | Conversational entry point |
| GET | `/orchestrator/chat/<id>/history` | Conversation history |
| GET | `/orchestrator/executions/<id>` | Execution state |
| GET | `/orchestrator/executions/<id>/trace` | Full audit trace |
| POST | `/orchestrator/approvals/<id>` | Approve/reject a governed action |
| GET | `/orchestrator/twin/states`, `/states/<id>`, `/snapshots/<id>`, `/compare` | Digital twin |
| POST | `/orchestrator/insights` | Evidence-grounded insight generation |
| GET | `/orchestrator/capabilities`, `/workflows`, `/health` | Introspection |

> **Known gap.** The control plane is not yet behind `@require_auth` or project
> scoping; it predates the application layer's auth. It must not be exposed
> publicly in its current state. Tracked as P1 in the gap analysis.

---

## Error envelope

Every application error serializes identically:

```json
{ "error": { "code": "NO_NETWORK_BOUND",
             "message": "This project has no network bound yet. …",
             "context": { "project_id": "pr-…" } } }
```

`code` is from `AppErrorCode` (application layer) or `ErrorCode`
(`orchestrator/exceptions.py`). The frontend switches on `code`, never on
message text.

---

## Verified anonymous-access behaviour

Measured, not asserted — see `api_validation.json`:

| Route | Anonymous | Expected |
|---|:--:|:--:|
| `/api/status` | 200 | 200 |
| `/api/projects` | 401 | 401 |
| `/api/auth/me` | 401 | 401 |
| `/api/kpis/network` | 401 | 401 |
| `/api/kpis/thresholds` | 401 | 401 |
| `/api/scenarios` | 401 | 401 |
| `/api/forecast` | 401 | 401 |
| `/api/signals` | 401 | 401 |

All eight probes match expectation.
