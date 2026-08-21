# Phase 2 — Orchestrator ↔ Deterministic Core Integration

Companion to [`phase2_integration_gap_report.md`](phase2_integration_gap_report.md),
which was written before implementation. This records what the integrated
system does.

Phase 2 added no algorithms, no agents, no risk scores and no optimization
objectives. It connected what existed and proved the connections hold.

---

## 1. Integrated architecture

```
                         ORCHESTRATOR  (control plane — coordinates, never computes)
                              │
             ┌────────────────┼─────────────────┐
             ▼                ▼                 ▼
        Intent Agent    Scenario Planner   External Signal
        (proposal only) (override only)    (evidence only → P)
             │                │                 │
             └────────┬───────┘                 │
                      ▼                         │
                 MILP (authoritative)           │
                      │                         │
              ┌───────┴────────┐                │
              ▼                ▼                │
          Baseline           REI ───────────────┘
              │                │
              ▼                ▼
        Network State     REI Registry (cached by material fingerprint)
                               │
                               ▼
                        RF Assessment      RF = P + REI − P·REI
                               │
                               ▼
                       Reasoning Agent     (explains; never computes)
                               │
                               ▼
                      Numeric Grounding    (strips ungrounded figures)
                               │
                               ▼
                      Action Governance
                        /      |      \
                     AUTO  APPROVAL  HUMAN_ONLY
```

## 2. Interfaces connected

| Seam | Interface | Change |
|---|---|---|
| Orchestrator → MILP | `OptimizationClient` → `optimization.milp.solve` | Result now stamped `result_kind`, `baseline_snapshot_id`, `model_version`, `execution_id`. |
| Orchestrator → REI | `REIClient.assess_registry` → `REIService.get_or_compute` | **New**: returns the TYPED registry, stored on `ExecutionContext.rei_registry`. |
| REI → RF | `ctx.rei_registry` → `assess_event_risk` | **Changed**: was a lossy dict round-trip; now the authoritative object. |
| RF → Reasoning | `ctx.risk_results` → `ReasoningAgent.reason` | Unchanged (already correct). |
| Reasoning → Grounding | `ground_narrative` | Unchanged. Runs on the template path too. |
| Grounding → Governance | `grounding_failed` → `ActionClassifier` | Unchanged. |
| All → Audit | `ExecutionTrace.record` | **Changed**: correlation-stamped; 11 new canonical events. |

## 3. Workflows

```
A  EXTERNAL RISK   load → interpret_signal ─┐
                              rei ──────────┴→ risk → reason → govern
B  WHAT-IF         load → baseline ─┐
                   create → validate ┴→ solve_scenario → kpi ─┐
                                             rei ─────────────┴→ reason → govern
C  EXPLANATION     load → rei → risk → reason → govern      (no optimization step)
```

Workflow C is new. It has no `optimization.solve` step by construction, so an
explanation cannot trigger a fresh optimization — and its REI step is served
from cache when evidence exists (measured: 0 solver invocations).

## 4. Execution traces

**A — external risk (measured on the `PHASE2_DELHI` fixture)**

```
C0 = 1,200                              baseline MILP
PI(DELHI) = 400, max EI = 500           REI batch, 6 solves
REI(DELHI) = 400/500 = 0.80             registry
P = 0.70                                external signal (stated, not inferred)
RF = 0.7 + 0.8 − 0.56 = 0.94            RF calculator
governance: HUMAN_ONLY (R6_RISK_FACTOR_HUMAN, RF ≥ 0.8)
```

**B — what-if** `DC_DELHI` 5,000 → 50 units: 1,200 → 1,400 (+16.67%). Baseline
byte-for-byte unchanged; the change exists only in the scenario overlay.

**C — explanation** with valid cached evidence: **0 MILP solves**, REI 0.80
served from cache, RF refused with `NO_EVENT_PROBABILITY` when no signal is
attached.

## 5. Failure matrix

| # | Failure | Step | RF | Downstream | Final |
|---|---|---|---|---|---|
| 1 | MILP success | COMPLETED | — | — | COMPLETED |
| 2 | MILP infeasible | FAILED | — | halted | INFEASIBLE |
| 3 | MILP timeout | FAILED | — | KPI BLOCKED | FAILED |
| 4 | MILP exception | FAILED | — | KPI BLOCKED | FAILED |
| 5 | REI success | COMPLETED | COMPUTED | — | per governance |
| 6 | REI timeout | FAILED | `NO_REI` | reason COMPLETED (soft) | COMPLETED |
| 7 | REI exception | FAILED | `NO_REI` | reason COMPLETED | COMPLETED |
| 8 | REI stale | COMPLETED | `STALE_REI` | reason COMPLETED | COMPLETED |
| 9 | RF success | COMPLETED | COMPUTED | — | per governance |
| 10 | No P | COMPLETED | `NO_EVENT_PROBABILITY` | reason COMPLETED | COMPLETED |
| 11 | No REI + no P | COMPLETED | `NO_INPUTS` | reason COMPLETED | COMPLETED |
| 12 | Node unmappable | COMPLETED | `NODE_MAPPING_UNAVAILABLE` | reason COMPLETED | COMPLETED |
| 13 | Reasoning success | COMPLETED | — | — | per governance |
| 14 | Reasoning failure | FAILED (optional) | unaffected | govern COMPLETED | not FAILED |
| 15 | Grounding failure | COMPLETED | unaffected | APPROVAL_REQUIRED | REQUIRES_APPROVAL |
| 16 | Governance | always produces a verdict | — | — | — |
| 17 | Invalid scenario | validate FAILED | — | solve BLOCKED | FAILED |
| 18 | Stale snapshot | not started | — | — | STALE |

`P = 0` is **not** a failure: it computes (RF = 0 + 0.8 = 0.8). A stated zero
and a missing value are different facts, and the system keeps them different.

## 6. Defects found and fixed

1. **Fabricated node status.** The flattened REI projection omitted
   `calculation_status` / `failure_reason`, and `_registry_from_rei_output`
   rebuilt a typed registry defaulting every node to `OK`. An INFEASIBLE node
   was recorded as healthy-with-no-REI. Fixed by passing the typed registry
   through and deleting the rebuild.
2. **Cached batches over-reported solve cost.** `n_milp_solves` kept the
   originating batch's count on a cache hit, contradicting both the field's own
   contract and the service docstring. Now 0, as documented.
3. **A rename permanently broke RF.** Snapshot ids derive from `data_version`
   (which hashes names); the REI cache keys on the material fingerprint (which
   does not). Renaming a facility minted a new snapshot id, hit the cache, and
   returned a batch stamped with the old id — which the staleness check
   correctly refused, forever. Fixed by re-stamping a cache-served batch with
   the serving snapshot, retaining the original in `computed_for_snapshot_id`.
   The check itself was not relaxed.

## 7. Known limitations

1. ~~**Analytical-action carve-out in governance.**~~ **Resolved.** R7 no longer
   short-circuits R7B — see [`r7_governance_precedence.md`](r7_governance_precedence.md).
2. **No in-flight cache deduplication.** N simultaneous cold REI requests each
   compute a batch; the last wins the entry. Redundant work, not divergence —
   the batches agree exactly.
3. **Idempotency returns a point-in-time view.** A duplicate arriving while the
   original is still running gets a partial response rather than blocking.
   Sequential retry is fully correct.
4. **Location→node mapping is string matching.** Adequate when ids encode
   location; a real deployment needs a geographic table.
5. **No authentication on the orchestrator API.** Capability-level
   authorization exists and works, but the actor is caller-asserted.
