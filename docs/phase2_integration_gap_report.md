# Phase 2 — Integration Gap Report

Produced **before** implementation, per Phase 2 §2. Traces the real interfaces
between Orchestrator, Planner, Execution Context, MILP, REI Service, REI
Registry, RF Risk Assessment, Reasoning Agent, Numeric Grounding and Action
Governance.

Scope note: this is an audit of what exists, not a design document. Components
that already work correctly are named as such and left alone.

---

## A. Existing compatible interfaces (no change needed)

| Seam | Interface | Status |
|---|---|---|
| Orchestrator → MILP | `OptimizationClient.solve/solve_scenario` → `optimization.milp.solve` | Real. No second cost model; `build_network_state_result` supplies the contract. Infeasible → `SolverInfeasibleError` (NON_RETRYABLE). |
| Orchestrator → REI | `REIClient.assess` → `REIService.get_or_compute` → `assess_network_resilience` | Real, and routed through the service so caching/invalidation are inherited rather than bypassed. |
| Orchestrator → RF | `compute_rf` handler → `risk_assessment.assess_event_risk` → `risk_factor.compute_risk_factor` | Real. RF arithmetic lives in one place. |
| Orchestrator → Reasoning | `synthesise` handler → `ReasoningAgent.reason` | Real, with a deterministic template fallback. |
| Reasoning → Grounding | `ReasoningAgent._ground` → `ground_narrative` | Real, and unconditional — the template path is checked too. |
| Orchestrator → Governance | `Orchestrator._govern` → `ActionClassifier.classify` | Real. Structural actions are HUMAN_ONLY before any REI/RF threshold is read. |
| Dependency model | `ExecutionPlan.classify_dependencies` HARD/SOFT | Correct. Settle-precedence (required-failure outranks governance verdict) is in place. |
| Scenario isolation | `SnapshotManager` / `ScenarioStore` | Correct. Deep copies both ways, no `promote_to_observed`, all stores lock-guarded. |

## B. Duplicate interfaces

**B1 — `_registry_from_rei_output` (registry.py) is a second construction path
for `FacilityResilienceRegistry`.** The REI step produces the typed registry,
flattens it to a transport dict, and the RF step then rebuilds a *different*
typed registry from that dict. Two paths to the same type is one too many.

## C. Missing adapters

**C1 — No typed REI registry on the execution context.** The only way RF can
reach per-node calculation status and snapshot provenance is the round-trip in
B1. The context carries `risk_results` and `reasoning` but not the REI batch.

**C2 — No absolute capacity override.** `ScenarioIntentSpec` offers
`capacity_multiplier` only. The Phase 2 what-if example ("capacity reduced by
2,000 units/day") is an absolute delta, and the intent agent cannot convert one
to a multiplier because it does not know the facility's capacity.

**C3 — No explanation workflow.** "Why is Delhi DC high risk?" matches no rule
in `IntentAgent._rule_based` (`_RESILIENCE_WORDS` has "riskiest", not "risk"),
resolves to `Intent.UNKNOWN`, and the run terminates REQUIRES_HUMAN before any
evidence is consulted.

## D. Incorrect assumptions

**D1 — `_registry_from_rei_output` fabricates two fields.** The flattened rows
emitted by `REIClient.assess` carry no `calculation_status`, no
`failure_reason` and no `facility_role`, so the rebuild defaults every node to
`CalculationStatus.OK`. A node whose disruption solve genuinely FAILED is
reconstructed as OK-with-no-REI, and `lookup_rei` then writes
`calculation_status=OK` into the audit trail for a node that failed. The
registry-level `baseline_solver_status` is likewise hardcoded to `OPTIMAL`
regardless of what the baseline did. Both are fabricated values reaching a
provenance record — exactly what §27 prohibits.

**D2 — `compute_rf` produces a silent empty assessment when REI is missing.**
It appends a warning, but the returned `RiskAssessment` has empty `results`
*and* empty `not_computable`. §6/§19 require the response to state that REI is
unavailable; a reader of the risk block alone sees nothing at all. (The absence
is *not* read as zero anywhere, so this is an incompleteness, not a
correctness failure.)

**D3 — Scenario results carry incomplete provenance.** `solve_scenario` passes
`baseline_state=None` into `build_scenario_result`, so the emitted
`baseline_snapshot_id` is `None`; `model_version`, `execution_id` and
`scenario_version` are absent entirely. §11 requires all of them, plus an
explicit marker that the result is hypothetical rather than current state.
(`is_hypothetical=True` is present; the identifying fields are not.)

## E. Mock-only paths

None in the deterministic chain. The MILP, REI, RF, grounding and governance
paths are all real today. The LLM gateway is real HTTP and reports unavailable
without a token, degrading to rule-based intent and template reasoning — that
is a designed fallback, not a mock.

## F. Places where production services are bypassed

None found. `REIClient` routes through `REIService` (cache respected);
`OptimizationClient` calls `milp.solve` directly with no shadow cost model;
`KPIClient` selects from the MILP result rather than recomputing.

## G. Places where deterministic calculations may accidentally be performed by the LLM

None found, and three defences hold:

1. `ReasoningAgent` receives an already-computed payload and cannot write back.
2. Every numeric claim is adjudicated by `ground_narrative`; CONTRADICTED and
   UNSUPPORTED figures are stripped from the text, not merely warned about.
3. `IntentAgent` output is a *proposal*: facility ids are filtered against the
   real network, and `ScenarioValidator` rejects the rest.

One residual risk worth naming: the grounding tolerance means a model figure
within 0.5% of an authoritative one passes. That is a rounding allowance, not a
computation path.

## H. Observability gaps (§26)

Present: `execution_started`, `intent_resolved`, `plan_built`,
`snapshot_validated`, `step_blocked`, `step_degraded`, `step_exception`,
`step_failed`, `solver_infeasible`, `governance_applied`,
`execution_completed`.

Missing: `workflow_started`, `step_started`, `step_completed`,
`evidence_unavailable`, `rei_lookup`, `rf_calculated`, `rf_not_computable`,
`reasoning_completed`, `grounding_completed`, `governance_decision`,
`workflow_completed`. Events also carry no correlation stamp, so a single event
cannot be tied to its execution/workflow/snapshot without reading its parent
trace.

---

## Planned changes

Ordered by risk, smallest blast radius first. Nothing in section A is touched.

1. **Remove the round-trip (B1/C1/D1).** `REIClient` gains `assess_registry()`
   returning the typed batch; `assess()` keeps its signature and flattens it.
   The REI handler stores the typed registry on `ExecutionContext.rei_registry`
   and RF consumes that. `_registry_from_rei_output` is deleted rather than
   patched — the fabrication cannot recur if the path does not exist.
2. **Explicit NOT_COMPUTABLE rows (D2).**
3. **Scenario provenance fields (D3).**
4. **Absolute capacity delta (C2)** — schema, validator, builder, intent rule.
5. **Explanation intent and workflow (C3)** — one new `WORKFLOW_TEMPLATES`
   entry, which is the planner's designed extension seam. No new agent.
6. **Canonical observability events (H)** — added alongside the existing ones,
   which keeps existing traces and tests valid.
7. **`tests/integration/` suite** covering §5–§25.
