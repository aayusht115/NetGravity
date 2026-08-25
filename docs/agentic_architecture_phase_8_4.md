# NetGravity Agentic Architecture — Phase 8.4: Failure Management & Recovery Policy

## 1. Overview & Architectural Hierarchy

Phase 8.4 establishes the **Failure Management and Recovery Policy** layer directly above the single-shot `CapabilityExecutor`.

```
    User Request
         ↓
    Orchestrator
         ↓
    Deterministic WorkflowPlanner
         ↓
    ExecutionPlan
         ↓
    FAILURE MANAGER (FailureManager)  ← Phase 8.4
         ↓
    CapabilityExecutor (Single-shot seam)
         ↓
    Specialist Capability
         ↓
    AgentResult
         ↓
    ExecutionContext
         ↓
    Orchestrator
```

### Key Architectural Invariants
1. **Single-Shot Seam**: `CapabilityExecutor` remains strictly single-shot and executes without internal retry loops or fallback branching.
2. **Deterministic Governance**: `FailureManager` sits above `CapabilityExecutor` and makes bounded policy decisions (`CONTINUE`, `RETRY`, `REROUTE`, `BLOCK`, `ESCALATE`).
3. **No Domain Calculation**: `FailureManager` never computes mathematical formulas (MILP, REI, RF, or forecasting) and never invents un-planned arbitrary capability steps.
4. **Observable History**: Every attempt (attempt 1, attempt 2, etc.) is preserved chronologically in `ExecutionContext.step_attempts`. Attempt 1 is never overwritten or erased by attempt 2.
5. **Absence is Never Zero**: A failed or uncomputed calculation (e.g. failed REI or missing external signal probability) is recorded as `UNAVAILABLE` / `NOT_RUN` with explicit `UnavailableEvidence`. It is never defaulted to `0.0`.

---

## 2. Policy Actions & Failure Classification

### Recovery Actions (`RecoveryAction`)
- **`CONTINUE`**: Capability outcome is usable (`SUCCESS` or `PARTIAL`). Proceed to downstream dependents.
- **`RETRY`**: Transient, retryable error within attempt budget (`attempt < max_attempts`). Schedule next attempt with exponential backoff.
- **`REROUTE`**: Primary capability failed and a valid registered alternative exists in `CapabilityRegistry`. Execute the alternative and record full provenance.
- **`BLOCK`**: A HARD dependency failed. Downstream dependent steps cannot safely execute and are marked `BLOCKED` with `EvidenceStatus.NOT_RUN`.
- **`ESCALATE`**: Autonomous recovery is not possible (retries exhausted, non-retryable critical failure, safety violation). Produce an `EscalationOutcome` for operator intervention.

### Failure Classification
| Failure Category | Classification | Policy Behavior |
| :--- | :--- | :--- |
| **Transient Error** (Timeout, 429, 500, 502, network drop) | `RETRYABLE_FAILURE` | Retry up to `max_attempts` with backoff; fast-fail if circuit breaker trips. |
| **Solver Infeasibility** | `NON_RETRYABLE_FAILURE` | Mathematical finding; never retried; halts plan execution cleanly. |
| **Output Schema Violation** | `INVALID_OUTPUT` | Validator rejected payload; output set to `None`; never retried. |
| **Missing Input / Dependency** | `INSUFFICIENT_EVIDENCE` | Preflight refusal; never executed; downstream HARD steps blocked. |
| **Authorization Rejection** | `AUTHORIZATION_FAILURE` | Actor role lacks permissions; immediately raised. |

---

## 3. Circuit Breaker for External Dependencies

To prevent cascading failures and quota exhaustion on shared dependencies (e.g., text-generation LLM Gateway), NetGravity implements a thread-safe **`CircuitBreaker`**.

### Circuit Breaker States
- **`CLOSED`**: All requests allowed through. Consecutive failure count tracked.
- **`OPEN`**: Tripped when consecutive transient/infrastructure failures exceed threshold (e.g. 3). Requests fast-fail immediately with `CIRCUIT_BREAKER_OPEN` without hitting the external network.
- **`HALF_OPEN`**: Entered automatically when `recovery_timeout_seconds` (cooldown) elapses. Permits a single probe call. If probe succeeds, circuit resets to `CLOSED`. If probe fails, circuit trips back to `OPEN`.

---

## 4. Observable Provenance & Escalation Structure

### Attempt Record (`StepAttemptRecord`)
```json
{
  "step_id": "s1",
  "capability": "solver.cloud",
  "attempt": 1,
  "status": "RETRYABLE_FAILURE",
  "error_code": "ENGINE_TIMEOUT",
  "error_message": "Capability exceeded timeout",
  "is_reroute": false,
  "timestamp": "2026-08-25T21:00:00Z"
}
```

### Escalation Record (`EscalationOutcome`)
```json
{
  "capability": "network.optimize",
  "execution_id": "exec_849204",
  "reason": "Autonomous recovery exhausted: Solver infeasible",
  "failed_attempts": 1,
  "blocked_downstream_capabilities": ["resilience.assess", "risk.compute_rf"],
  "available_evidence": {"network.state": "SUCCESS"},
  "recommended_human_action": "Relax facility capacity constraints or add lane connections in scenario configuration.",
  "timestamp": "2026-08-25T21:00:05Z"
}
```

---

## 5. Security & Authority Invariants

1. **`event_probability` vs `confidence`**: `event_probability` describes likelihood of a discrete hazard event and feeds `RF = P + REI - P*REI`. `confidence` describes the qualitative certainty of the intelligence assessment and never feeds RF.
2. **`failed RF != (RF = 0)`**: An uncomputed RF calculation raises or reports `NOT_COMPUTABLE` via `UnavailableEvidence`. It is never falsified as zero risk.
3. **Plannable vs Executable**: Non-plannable capabilities (such as Digital Twin projection or file parsing) cannot be scheduled as autonomous plan steps.
