# NetGravity Architecture — Phase 8.6: Adaptive Agentic Execution

## 1. Executive Summary & Objective

Phase 8.6 upgrades NetGravity from a static batch pipeline into a **controlled closed-loop orchestration model**:

```
User Request
     ↓
LLM / Template Planner  ──► PlanProposal
     ↓
PlanValidator  ──────────► Validated ExecutionPlan
     ↓
[ Adaptive Execution Loop ]
     ┌────────────────────────────────────────────────────────┐
     │ 1. Ready Step Selection (DAG topological order)       │
     │ 2. FailureManager & CapabilityExecutor execution       │
     │ 3. AgentResult generation                              │
     │ 4. ResultObserver (Deterministic Domain Interpretation)│
     │ 5. AdaptiveDecisionPolicy (Next Action Determination)  │
     │    ├── CONTINUE: proceed to next ready step            │
     │    ├── REPLAN: invoke planner + validate + branch      │
     │    ├── RETRY: retry transient step                     │
     │    ├── REROUTE: fallback to alternative capability     │
     │    ├── BLOCK: halt unsatisfied downstream dependencies │
     │    ├── ESCALATE: escalate to human operator            │
     │    └── TERMINATE: finalize execution early             │
     └────────────────────────────────────────────────────────┘
     ↓
Final Synthesis (Reasoning Agent)
     ↓
Governance Classifier (Classification + Gatekeeping)
     ↓
Digital Twin Publication (Orchestrator-controlled)
     ↓
FinalResponse
```

---

## 2. Architecture & Control Plane Contracts

### 2.1 The Single Execution Seam
- **Single-shot Execution**: `CapabilityExecutor` executes exactly one capability at a time and returns a typed `AgentResult`. It contains **no loops, retries, or replanning logic**.
- **Seam Uniformity**: All capabilities (deterministic MILP/REI/KPI, probabilistic Forecasting/Signals, and Reasoning) execute exclusively through `CapabilityExecutor`.

### 2.2 Failure Management & Recovery Authority
- **Central Recovery**: `FailureManager` is the single authority for execution error classification, retry backoff, capability rerouting, and circuit breaking.
- **`execute_step` Contract**: Exposes atomic step execution to the adaptive loop, maintaining step status (`COMPLETED`, `FAILED`, `BLOCKED`, `ESCALATED`) and recording `events.STEP_FAILED` and `events.EVIDENCE_UNAVAILABLE` into the audit trace.

### 2.3 ResultObserver
- **Deterministic Domain Interpretation**: Transforms raw `AgentResult` outcomes into structured `ResultObservation` records without LLM calls.
- **Materiality Evaluation**: Detects whether demand shifts exceed `material_forecast_threshold` ($\ge 15\%$) relative to baseline snapshot demand.
- **Mathematical Infeasibility**: Detects solver infeasibilities (`INFEASIBLE_OPTIMIZATION`) directly from optimization results and flags for human escalation.
- **Evidence Gap Preservation**: Missing prerequisites produce `INSUFFICIENT_EVIDENCE` and record typed `UnavailableEvidence` in the context; numerical defaults (such as `0.0` or fake costs) are strictly forbidden.

### 2.4 AdaptiveDecisionPolicy
- **Deterministic Decision Logic**:
  - `domain_outcome == "MATERIAL_FORECAST_INCREASE"`: If demand shift $\ge 15\%$ and downstream scenario analysis is absent, returns `REPLAN`.
  - `status == AgentStatus.RETRYABLE_FAILURE`: Returns `RETRY` if attempt count $< \text{max\_attempts}$.
  - `status == AgentStatus.INSUFFICIENT_EVIDENCE`: Evaluates dependency optionality; returns `BLOCK` for downstream dependents if required, or `CONTINUE` if optional.
  - `status == AgentStatus.INFEASIBLE` / `domain_outcome == "INFEASIBLE_OPTIMIZATION"`: Returns `ESCALATE` with mathematical explanations.
  - Repeated failures exceeding retry budget on mandatory steps: Returns `ESCALATE`.
  - Failures on optional steps: Returns `CONTINUE` with degraded evidence.

### 2.5 ReplanGuard & Infinite Loop Guardrails
To prevent infinite loops, oscillatory behavior, and budget exhaustion, `ReplanGuard` enforces:
1. **Max Execution Steps Budget**: Halts if total executed steps reach `max_execution_steps` (default 25).
2. **Max Replans Limit**: Halts if replan count reaches `max_replans` (default 3).
3. **Repeated-Plan Cycle Detection**: Computes a canonical signature of the proposed DAG (`step_id:capability -> ...`). If the signature matches any previously executed plan in the session, the replan is refused with a cycle alert and escalated.

---

## 3. Strict Boundary Invariants

| Boundary | Rule / Invariant | Verification Mechanism |
| :--- | :--- | :--- |
| **Planner Boundary** | The LLM Planner produces `PlanProposal` objects only. It has **no execution authority**, no access to `CapabilityExecutor`, and cannot call engines. | Structural AST & inspect test |
| **Specialist Boundary** | Specialists (Forecasting, MILP, REI, RF) **cannot invoke each other** or call the planner. All data exchange is orchestrated via `ExecutionContext`. | Structural AST test |
| **Risk Signal Isolation** | Signals carrying `event_probability` or probability fields are classified as `REFUSED_RISK_SIGNAL` and **strictly blocked from Forecasting**. | Routing contract & unit tests |
| **Publishing Authority** | Digital Twin publication is exclusively managed by `Orchestrator`. No specialist or planner can publish directly. | Code analysis & test suite |
| **Governance Net** | All workflows terminate through deterministic governance classification and safety checks. | Lifecycle integration tests |
