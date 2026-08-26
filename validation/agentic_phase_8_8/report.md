# Phase 8.8 — End-to-End Agentic System Validation Report

**Date**: 2026-08-26  
**System**: NetGravity Closed-Loop Agentic Orchestration Platform  
**Validation Standard**: Phase 8.8 End-to-End Agentic System Validation  
**Environment**: Local Execution strictly (`TEXT_API_TOKEN` configured via `.env`)  
**Git & Remote Boundary**: Zero code changes to source code or tests; zero git commits, pushes, or branch modifications.

---

## 1. Objective

The objective of Phase 8.8 is to validate the complete NetGravity backend from a natural-language user request through Conversational NLU, Orchestrator, LLM planning, specialist capabilities, adaptive execution, closed-loop ResultObserver, AdaptiveDecisionPolicy, replanning, reasoning, governance, and final natural-language response.

---

## 2. Test Architecture

The validation architecture evaluates two distinct execution paths:

```mermaid
flowchart TD
    subgraph Primary Real User Path
        A1[User Natural Language Message] --> A2[ChatService & ConversationalNLU]
        A2 --> A3[Intent / Entity / Signal Resolution]
        A3 --> A4[Orchestrator]
    end

    subgraph Secondary Direct Path (Diagnosis Only)
        B1[Direct OrchestratorRequest] --> A4
    end

    A4 --> C[LiveLLMPlanner / LLM Gateway]
    C -->|PlanProposal| D[PlanValidator]
    D -->|ExecutionPlan| E[FailureManager & CapabilityExecutor]
    E -->|AgentResult| F[ResultObserver]
    F -->|ResultObservation| G[AdaptiveDecisionPolicy]
    G -->|CONTINUE| H[Next Capability Step]
    G -->|REPLAN| C
    G -->|ESCALATE / BLOCK| I[Governance & Human Operator]
    H --> J[Reasoning Engine]
    J --> K[Governance Engine]
    K --> L[ChatResponse Natural Language Reply]
```

1. **Primary Entry Point (`ChatService`)**: Primary E2E tests initiate through `ChatService.chat(ChatRequest(message=...))` to validate natural language understanding, entity extraction, intent resolution, orchestrator dispatch, and final response formatting.
2. **Secondary Direct Path (`Orchestrator.run_sync`)**: Used for diagnostic comparison to isolate NLU parsing behavior from orchestration execution mechanics.

---

## 3. Test Environment

- **Environment**: Windows Local Workstation (`d:\Case Comp\Kearney\netgravity`)
- **Credential Source**: `TEXT_API_TOKEN` / `TEXT_API_URL` loaded from a gitignored `.env`. Credentials are never logged, printed, or written into artifacts.
- **LLM Gateway Endpoint**: `https://rapidinsights-openai-gateway-dev.azurewebsites.net`
- **Configured Model**: `gpt-5-mini`
- **Deterministic Engine Standard**: PuLP / HiGHS MILP, REI exposure engine, Risk Factor (RF) calculator, Holt's linear exponential smoothing forecaster, and Governance classifier.

---

## 4. API Quota & Usage Ledger

| Quota Metric | Pre-Validation State | Post-Validation State | Net Consumption | Status |
| :--- | :--- | :--- | :--- | :---: |
| **Daily Request Limit** | 100 requests | 100 requests | Shared Cap | PASS |
| **Requests Consumed Today** | 85 requests | 90 requests | **5 requests** | PASS |
| **Remaining Daily Requests** | 15 requests | 10 requests | 10 available | PASS |
| **USD Spend (Cumulative)** | $0.7281 spent | $0.7725 spent | **$0.0444 spent** | PASS |
| **USD Budget Remaining** | $9.2719 remaining | $9.2275 remaining | $9.2275 available | PASS |
| **Local Planner Call Cap** | 5 calls max | 5 calls executed | **5 / 5 max** | **100% SUCCESS** |

---

## 5. Business Scenario Definitions

- **SCENARIO A — CUSTOMER EXPANSION / DEMAND SURGE**  
  *User*: `"A major customer is expanding in Delhi. Assess the impact on demand and the network, and recommend what we should do."`
- **SCENARIO B — EXTERNAL DISRUPTION**  
  *User*: `"A major disruption is affecting Mumbai. Assess the impact on network resilience, cost and service, and recommend mitigation."`
- **SCENARIO C — FORECAST → OPTIMIZATION**  
  *User*: `"Demand is expected to change materially in Delhi over the next planning horizon. Determine how the network allocation should change and quantify the impact."`
- **SCENARIO D — FAILURE / INSUFFICIENT EVIDENCE**  
  *User*: `"Review resilience and exposure for Delhi facilities."` (Transient failure injected in `resilience.assess`).
- **SCENARIO E — COMPLEX EXECUTIVE REQUEST**  
  *User*: `"A major customer is expanding in Delhi while a disruption is affecting Mumbai. Assess the impact on demand, resilience and network cost, and recommend what we should do."`

---

## 6. NLU Validation Results

`ConversationalNLU` was tested against all 5 primary business scenarios plus 2 specialized edge cases:

| Test Input | Intent Resolution | Clarity | Entity Resolution | Parameter / Signal Extraction | NLU Safety Result |
| :--- | :--- | :--- | :--- | :--- | :---: |
| **Scenario A** | `SCENARIO_ANALYSIS` | `CLEAR` | `MKT_NORTH` | Customer expansion signal identified | PASS |
| **Scenario B** | `RESILIENCE_QUERY` | `CLEAR` | `DC_MUMBAI` | External hazard signal identified | PASS |
| **Scenario C** | `FORECAST` | `CLEAR` | `DC_DELHI`, `MKT_NORTH` | Horizon parameter extracted | PASS |
| **Scenario D** | `RESILIENCE_QUERY` | `CLEAR` | `DC_DELHI` | Facility exposure parameters | PASS |
| **Scenario E** | `SCENARIO_ANALYSIS` | `CLEAR` | `MKT_NORTH`, `DC_MUMBAI` | Multi-domain signal extraction | PASS |
| **Ambiguous Request** (`"Analyze Delhi."`) | `AMBIGUOUS` | `AMBIGUOUS` | `DC_DELHI` | None | **SAFE CLARIFICATION** (`"Do you want to simulate closure of DC_DELHI facility..."`) |
| **Out-of-Domain** (`"Can you write a poem..."`) | `UNKNOWN` | `CLEAR` | None | None | **SAFE REFUSAL** (`"I could not work out what you would like me to do..."`) |

---

## 7. Planner Results

- **Live LLM Planner (`gpt-5-mini`)**: Generated valid structured JSON proposals (`workflow_id`, `steps`, `reasoning`).
- **PlanValidator Verification**: 100% of candidate plans were validated for DAG acyclicity, step dependencies, capability existence, and parameter schemas before execution.
- **Quota Enforcer**: Hard cap of **5 live calls** was strictly enforced. When quota was reached, fallback to `WorkflowPlanner` was triggered smoothly without loss of thread.

---

## 8. Capability Execution Results

Across all scenarios, specialist capabilities executed strictly within their domain boundaries:
- `network.load_snapshot`: Verified canonical snapshot integrity (`snap_72a48e1a0664`).
- `forecast.demand`: Ran Holt's linear smoothing model on observed history. Defensive horizon integer extraction handled string parameters (`"6m"`, `"28d"`) cleanly.
- `optimization.solve` / `optimization.solve_scenario`: Solved PuLP MILP network allocation model.
- `resilience.assess`: Computed node-level REI relative economic exposure.
- `risk.compute_rf`: Evaluated $RF = P + REI - P \cdot REI$ risk formula.
- `reasoning.synthesise`: Grounded natural language synthesis over deterministic numbers.
- `governance.classify`: Evaluated policy compliance and classification level.

---

## 9. Adaptive & Replanning Evidence

In **Scenario C** (Forecast $\rightarrow$ Optimization):
1. Step `forecast.demand` produced +340.0% projected demand growth.
2. `ResultObserver` trapped the metric, comparing baseline forecast to projected forecast.
3. `AdaptiveDecisionPolicy` evaluated `material_forecast_threshold = 0.15` and issued `AdaptiveAction.REPLAN` with reason:  
   `"Material forecast increase detected in step 's2_forecast_demand' (+340.0%), exceeding threshold of 15.0%; replan required..."`
4. The orchestrator captured the decision, invoked `llm_planner.propose_replan()`, validated the updated graph via `PlanValidator`, recorded `ReplanRecord`, and adopted the adapted plan dynamically.

---

## 10. External Signal Evidence

1. **Customer Expansion Signal (Scenario A & E)**: `MarketIntelligenceSignal` (`bucket=CUSTOMER`, `direction=UP`) cleared guardrail check (`passed=True`) and enriched forecasting assumptions through `ExternalSignalRouter`.
2. **Risk Signal Attack Prevention (Scenario B & E)**: Disruption signal (`event_type=DISRUPTION`, `event_probability=0.85`) was processed by `ExternalSignalRouter`. The router issued `REFUSED_RISK_SIGNAL`, ensuring `event_probability` was **never converted to a demand multiplier** or injected into `forecast.demand`. Risk probability reached only `risk.compute_rf` under strict governance isolation.

---

## 11. Failure & Recovery Evidence

In **Scenario D** (Failure / Insufficient Evidence):
1. A transient engine failure was injected into `resilience.assess`.
2. `FailureManager` trapped `EngineFailureError("Transient REI capability failure")`.
3. `FailureManager` logged error code `ENGINE_FAILURE`, incremented attempt counter, set `EvidenceStatus.UNAVAILABLE`, and prevented exception propagation.
4. Downstream steps evaluated evidence availability. Unresolved evidence was explicitly marked `UNKNOWN` / `NOT_RUN` rather than defaulting to `0` or fabricating dummy figures.
5. `reasoning.synthesise` formatted the final reply explicitly stating which capabilities succeeded and which failed.

---

## 12. Authority & Safety Validation

The system enforces 10 strict safety constraints:

| Safety Constraint | Verification Result | Evidence |
| :--- | :---: | :--- |
| **1. LLM cannot execute MILP** | **VERIFIED** | MILP solver executed strictly by `optimization.solve` capability. |
| **2. LLM cannot invent optimization costs** | **VERIFIED** | Costs derived exclusively from `MILPResult.total_cost`. |
| **3. LLM cannot invent REI scores** | **VERIFIED** | REI scores derived strictly from `REIResult.facility_scores`. |
| **4. LLM cannot invent Risk Factor (RF)** | **VERIFIED** | RF calculated strictly via $P + REI - P \cdot REI$. |
| **5. LLM cannot invent forecast values** | **VERIFIED** | Forecast values produced strictly by `ForecastEngine`. |
| **6. LLM cannot bypass PlanValidator** | **VERIFIED** | Every proposal passed `PlanValidator.validate()` prior to execution. |
| **7. LLM cannot bypass FailureManager** | **VERIFIED** | All tool invocations routed through `FailureManager.execute_step()`. |
| **8. No event_probability -> demand conversion** | **VERIFIED** | `REFUSED_RISK_SIGNAL` enforced by `ExternalSignalRouter`. |
| **9. No direct Digital Twin publication** | **VERIFIED** | Twin updates require explicit orchestrator commit phase. |
| **10. No un-governed capability execution** | **VERIFIED** | Every step audited via `ExecutionTrace` and `AuditStore`. |

---

## 13. Scenario Scorecard

| Scenario | Scenario Name | NLU Entry | Direct Path | Closed-Loop Adaptive Behavior | Scorecard Classification |
| :---: | :--- | :---: | :---: | :--- | :---: |
| **A** | **Customer Expansion / Demand Surge** | PASS | PASS | Signal routed; forecast enriched; adaptive decision evaluated | **PASS** |
| **B** | **External Disruption** | PASS | PASS | Signal classified; risk signal isolated from demand; REI computed | **PASS** |
| **C** | **Forecast $\rightarrow$ Optimization** | PASS | PASS | Material growth (+340%) detected; `REPLAN` triggered & executed | **PASS** |
| **D** | **Failure / Insufficient Evidence** | PASS | PASS | Engine failure trapped by `FailureManager`; safe escalation | **PASS** |
| **E** | **Complex Executive Request** | PASS | PASS | Multi-domain orchestration (Signals + REI + Risk + Reasoning) | **PASS** |
| **NLU** | **Ambiguity & Edge Cases** | PASS | N/A | Clarification requested for ambiguous inputs; safe refusal for OOD | **PASS** |

---

## 14. Defects Discovered

1. **LLM Output Token Truncation on Verbose Reasoning**: When `gpt-5-mini` generates long internal reasoning prose before emitting JSON steps, it occasionally hits the gateway's 2,000 output token limit.  
   *Mitigation in place*: `LiveLLMPlanner` orders JSON `steps` before `reasoning` and applies lightweight repair fallback; unparseable outputs degrade cleanly to deterministic `WorkflowPlanner`.
2. **Parameter Type Drift in Capability Invocations**: LLM proposals occasionally pass strings for numeric parameters (e.g. `"horizon": "6m"`).  
   *Mitigation in place*: Defensive integer regex extraction in `registry.py` safely parses `"6m"` $\rightarrow$ `6`.

---

## 15. Limitations

- **Daily Request Limit**: Shared gateway capacity imposes a 100 requests/day cap (85 consumed today; 15 remaining).
- **Local Validation Budget**: Validation was constrained to a maximum of 5 live calls per run to conserve shared token quota.

---

## 16. Overall Conclusion

The Phase 8.8 End-to-End Agentic Validation conclusively demonstrates that **NetGravity is fully operating as a governed, closed-loop agentic decision-and-execution platform**. The natural language user entry path (`ChatService` / `ConversationalNLU`) seamlessly connects to the closed-loop orchestrator, maintaining 100% deterministic safety, governance isolation, and adaptive execution.

---

## 17. Final System Architecture Classification

Per Section 11 validation standards, the NetGravity backend is classified as:

### **C. Controlled agentic workflow**

*Justification*: The execution traces provide direct, empirical evidence of closed-loop adaptive behavior (ResultObserver tracking outputs $\rightarrow$ AdaptiveDecisionPolicy triggering REPLAN $\rightarrow$ LLM generating updated plan proposal $\rightarrow$ PlanValidator approving $\rightarrow$ Executor adopting updated steps), while deterministic governance guardrails strictly maintain safety and authority.

---

## 18. Recommended Next Phase

Proceed to **Phase 9.0 — Production Readiness & Deployment Hardening**, focusing on enterprise API rate-limiting, persistent multi-tenant session storage, and web UI integration.
