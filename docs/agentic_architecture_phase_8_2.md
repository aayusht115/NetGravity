# Phase 8.2 — Standardized Capability Executor

**Status:** complete. No planner, no retries added, no agent framework.
**Regression:** recorded in `validation/agentic_phase_8_2_report.md`.
**Seam overhead:** ~49 µs per invocation, measured. No deep copies.

---

## 1. Existing execution paths

The audit found execution logic spread across **three** places, with no single
point any caller could use.

| Concern | Lived in | Notes |
|---|---|---|
| Invoke handler, enforce timeout, classify errors, apply retry policy | `CapabilityTool.execute` (`tools/base.py`) | Solid. Reused untouched. |
| Capability-level authorization, upstream assembly | `Orchestrator._run_step` | Only reachable from a plan |
| Dependency gating, blocking, degradation, recording, tracing, infeasibility | `Orchestrator._execute_plan` | Only reachable from a plan |

Per-capability audit as found:

| Capability | Entry point | Caller | Result type | Validation | State recording |
|---|---|---|---|---|---|
| `network.load_snapshot` | `SnapshotManager.assert_fresh` | plan step | dict | freshness | `record_step` |
| `scenario.create` / `.validate` | `ScenarioBuilder` / `ScenarioValidator` | plan step | dict | in handler | `record_step` |
| `optimization.solve` / `.solve_scenario` | `OptimizationClient` | plan step | dict + typed `network_states` | `validate_optimization` (warnings) | `record_step` |
| `kpi.summarise` | `KPIClient` | plan step | dict | — | `record_step` |
| `resilience.assess` | `REIClient` | plan step | dict + typed `rei_registry` | `validate_rei` (warnings) | `record_step` |
| `risk.compute_rf` | `assess_network_risk` | plan step | dict + typed `risk_results` | in handler | `record_step` |
| `external.interpret_signal` | `ExternalSignalAgent` | plan step | dict | in handler | `record_step` |
| `market.score_signal` | relevance guardrail | plan step | dict | guardrail | `record_step` |
| `forecast.demand` | `ForecastingService` | plan step | dict + typed `forecast_result` | in service | `record_step` |
| `reasoning.synthesise` | `ReasoningAgent` | plan step | dict + typed `reasoning` | `numeric_grounding` | `record_step` |
| `governance.classify` | `GovernancePolicy` | plan step | dict + typed `governance_result` | in classifier | `record_step` |
| **`extraction.parse`** | `ExtractionParsingAgent.extract` | **ingestion API — bypassed all of the above** | `ExtractionResult` | row rules | **none** |
| **`twin.publish`** | `Orchestrator._project_twin` | **called directly in `run()`** | `TwinStateRef` | — | `twin_refs` only |
| **`signal.route_for_forecast`** | `ExternalSignalRouter.route_for_forecast` | **inline inside the forecast handler** | `SignalRoutingDecision` | — | `signal_routing` only |

The last three rows are the finding. Each is a real capability with a real
result, and each reached its specialist by a path of its own — so none of the
checks, none of the normalisation and none of the recording applied to it.

**No second executor was created.** Invocation is still `CapabilityTool.execute`.
`CapabilityExecutor` wraps it with the checks and the normalisation that
previously had no home.

---

## 2. Why the seam is required

Two reasons, and the second is the one that mattered.

**Uniformity.** Running one capability meant reproducing three layers, or
reaching past them. Three callers reached past them.

**A refusal has to be indistinguishable from nothing, and distinguishable from a
result.** The dangerous shape is not a crash. It is asking for KPIs before
anything has solved and receiving `{}` — which reads as a network that costs
nothing. Before this phase there was no place that could refuse. The handler
would run and do its best with whatever the context held.

Now:

```
executor.execute("kpi.summarise", context)   # nothing has solved yet
→ AgentStatus.INSUFFICIENT_EVIDENCE
→ output = None
→ unavailable = {"optimization.solve": "has not run"}
```

The handler was never called. There is no value to misread.

---

## 3. Executor contract

`netgravity/orchestrator/core/executor.py`

```python
async def execute(
    capability_id: str,
    context: ExecutionContext,
    *,
    params:      Optional[Dict[str, Any]] = None,
    upstream:    Optional[Dict[str, Any]] = None,
    unavailable: Optional[Dict[str, UnavailableEvidence]] = None,
    step_id:     Optional[str] = None,
    record:      bool = True,
) -> AgentResult
```

Nine steps, in order:

1. resolve the capability from `CapabilityRegistry`
2. refuse if it is not registered
3. validate declared inputs
4. verify required dependencies are satisfied
5. invoke the registered implementation via `CapabilityTool`
6. normalise the outcome into `AgentResult`
7. validate the output against the declared contract
8. record on the `ExecutionContext`, exactly once
9. return

Steps 1–4 are preflight: a refusal there means the handler was never called.

**Never raises for a capability-level failure.** The failure *is* the return
value, which is what lets a caller inspect it rather than catch it.

### What stayed outside

**Authorization** remains in `Orchestrator._run_step`. It is the only part of the
decision that depends on the **actor** rather than the capability — a role that
may not invoke a capability is stopped there, and raised rather than returned so
it can never be mistaken for a data-shaped outcome.

**Dependency scheduling** remains in `_execute_plan`: which layer runs, what is
blocked, what degrades. The executor answers *"are this capability's inputs
satisfied?"*, never *"what should run next?"*.

---

## 4. Input validation

Driven by `CapabilityContract.required_inputs`. A **present-but-None** value
counts as absent — passing None through as though it were supplied is precisely
how a missing input becomes a default deep inside a handler.

```
missing declared input → INSUFFICIENT_EVIDENCE, handler never invoked
```

Recorded on the context as missing evidence, because "we did not run this, and
here is why" is exactly the kind of absence a caller must not have to infer.

---

## 5. Dependency validation

Criticality was previously recorded **only inside plans** (`soft_depends_on`,
`optional`). A caller invoking a capability outside a plan had no way to know
which inputs were genuinely required. Phase 8.2 adds one contract field:

```python
optional_dependencies: Tuple[str, ...] = ()      # subset of `dependencies`
required_dependencies -> Tuple[str, ...]         # derived: the rest
```

Only `required_dependencies` block. Getting this wrong is harmful in both
directions, so it is declared per capability rather than inferred:

| Capability | dependencies | required | why |
|---|---|---|---|
| `optimization.solve` | `load_snapshot` | `load_snapshot` | cannot solve an unpinned network |
| `kpi.summarise` | `optimization.solve` | `optimization.solve` | nothing to project from |
| `scenario.validate` | `scenario.create` | `scenario.create` | HARD by design |
| **`risk.compute_rf`** | REI, interpret_signal | **none** | see below |
| **`twin.publish`** | optimize, REI, RF | **none** | publishes in every condition |

**`risk.compute_rf` is the important one.** Refusing to run RF when only one of
P and REI is present would replace an explicit `NOT_COMPUTABLE` row — which names
exactly what was missing — with a capability that simply did not run. The
handler's own report is strictly more informative than a refusal, so both inputs
are declared optional.

**Plans win where they are more specific.** When the execution belongs to a plan
step, the plan's HARD/SOFT classification is consulted too, and the two are
combined by intersection: an edge must be required by **both** the contract and
the plan to block. Anything else would let this check contradict a decision the
plan already made.

No scheduling. Nothing is queued, ordered or run to satisfy a gap.

---

## 6. Result normalization

Statuses come from evidence the capability itself produced — `success`, its
`error_code`, its `failure_class`, the declared contract, and the context's
record of missing evidence. **No status is inferred from the mere presence of an
exception.**

| Situation | Status | Reasoning |
|---|---|---|
| MILP infeasible | `NON_RETRYABLE_FAILURE` | the solver *proved* it; `output=None`, so it cannot masquerade as a solved network |
| Engine timeout | `RETRYABLE_FAILURE` | transient. Reported; nothing retries it |
| REI unavailable (no baseline) | `INSUFFICIENT_EVIDENCE` | **never zero** — zero exposure reads as a perfectly safe facility |
| Reasoning output malformed | `INVALID_OUTPUT` | ran, produced something unusable |
| Wrong output type | `INVALID_OUTPUT` | contract non-conformance |
| Valid forecast | `SUCCESS` | |
| Ran with a tolerated dependency absent | `PARTIAL` | real and usable, computed on less than the full picture |
| Unclassified exception | `RETRYABLE_FAILURE` | `EngineFailureError` is pre-existing RETRYABLE; unchanged here |

The typed authoritative object is attached from the context field the contract
names, so a consumer receives `ForecastResult` or `FacilityResilienceRegistry`
itself, not the flattened projection that would have lost per-series and per-node
status.

### Output validation is deliberately narrow

It checks **contract conformance** — did the capability produce the type it
promised, and did it produce anything at all — and nothing else.

It does **not** escalate the warnings `ResultValidator` already produces inside
the optimization and REI handlers. Those are advisory by design: a KPI slightly
outside an expected band is worth flagging and is not grounds for discarding a
solved network. Escalating them would change behaviour the existing tests
correctly pin, and would suppress results that are fine.

**The new check immediately found three inaccuracies in the Phase 8.1
catalogue** — declarations of mine that pointed a domain-object type at a field
holding an identifier or a different type. The metadata was corrected; the check
was not loosened. One contract flag, `authoritative_is_reference`, names the
cases where the field legitimately holds an id (a pinned snapshot, a scenario),
because comparing a `str` to `"NetworkSnapshot"` would reject a correct result.

---

## 7. Execution state

No new store. `record_step` remains the single write path and is now called
**inside the executor and nowhere else** — `_execute_plan` no longer records. One
write path means one place that can be wrong about it, and the step lists, the
capability status, the engine-results projection and the missing-evidence map
cannot end up disagreeing about the same execution.

Recorded per invocation: `execution_id`, `capability_id`, status, duration,
attempts, output reference, errors, warnings, provenance (snapshot + scenario),
and dependency state. No transcripts.

The normalised status is written onto the `ToolResult` before recording, so what
the context holds and what the caller received cannot diverge. In particular a
result the executor rejected as `INVALID_OUTPUT` is recorded as invalid — its
projection is removed from `engine_results` and an explicit
`EvidenceStatus.INVALID` is recorded — rather than as the success the handler
believed it was.

The executor itself holds no state. A test asserts `vars(executor) == {"registry"}`.

---

## 8. Failure boundary

No retry, no reroute, no fallback, no escalation, no circuit breaker.

```
capability → AgentResult(status=…) → ExecutionContext → caller
```

`is_retryable` is **reported and acted on nowhere.** Tests assert the executor
contains no `while`/`async for` loop and no reference to `should_retry`,
`delay_for`, `fallback`, `escalate` or `circuit` in its code (docstrings stripped,
since they discuss these terms precisely to say they are absent).

**One honest note:** several capabilities carry a pre-existing `RetryPolicy`
(`optimization.solve` allows 2 attempts, `external.interpret_signal` 3). Those
policies live in `CapabilityTool` and are **unchanged** — this phase neither adds
nor removes retry. The attempt count is surfaced through
`AgentResult.provenance.attempts` so a caller can see it happened.

---

## 9. Authority boundaries

Verified structurally.

| Rule | How |
|---|---|
| Executor imports no engine | AST: no `optimization`, `resilience`, `forecasting`, `ingestion`, `agents`, `twin`, `risk`, `pulp`, `openai` |
| Executor reaches specialists only through the registered tool | AST: exactly **one** awaited call in the module, and it is `tool.execute` |
| Executor contains no planning | AST: no `WORKFLOW_TEMPLATES`, `WorkflowPlanner`, `execution_layers`, `resolve_capability` |
| Executor cannot substitute a provider | given an id, it runs that id |
| Extraction cannot execute forecasting | AST over `agents/*.py` |
| Forecasting cannot invoke MILP/REI/RF | AST over `forecasting/**` |
| Digital Twin invokes no engine | AST over `twin/**`; `_project_twin` remains the only path in |
| Reasoning cannot modify authoritative results | a PROBABILISTIC capability writing a contradictory figure leaves the deterministic record untouched and is marked non-authoritative |
| Governance remains authoritative | DETERMINISTIC, `llm_backed=False` |
| RF is the only RF authority | `risk.compute_rf` unchanged; router output reaches no RF input |
| Signal Router owns forecast eligibility | `signal.route_for_forecast` is the declared authority; produces no probability |
| No agent gained a path to the executor | AST: no `agents/*.py` imports `core.executor` or `core.orchestrator` |

### Executable ≠ plannable

To put the three bypassing capabilities through the seam they needed handlers.
Registering them would previously have made them schedulable, which Phase 8.1
deliberately prevented.

The fix separates two questions the codebase had been answering with one fact:

- **Having a handler** makes a capability **executable**.
- **`invocation`** makes it **plannable**.

All 16 capabilities are now executable. Only 13 are schedulable.
`resolve_capability` still returns `None` for EXTRACTION and DIGITAL_TWIN, and no
workflow template names any of the three.

This required updating one Phase 8.1 test that asserted the gap. It was replaced
with a **stronger** pair of assertions — every declared capability is executable,
*and* executability does not confer plannability — not a weaker one.

---

## 10. What is deliberately NOT implemented

- **LLM planner** — nothing in the executor calls a model
- **OpenAI Agents SDK / Agno** — not imported, not installed
- **Dynamic planning / routing** — the executor runs the capability it is given
- **Retries** — none added; pre-existing policies unchanged
- **Rerouting, fallback provider selection, escalation, circuit breakers** — none
- **Parallel execution changes** — the existing layer executor is untouched
- **Agent-to-agent handoffs** — none; agents cannot reach the executor
- **Specialist redesign** — every handler delegates to the same production entry
  point it always used. No algorithm moved.
- **Escalating advisory validator warnings** — they remain warnings, by design

---

## 11. How this prepares the planner

```
User Request
     ↓
Orchestrator ──────────────→ CapabilityRegistry     (metadata: who, what, needs)
     │                              │
     │  resolve_capability(domain)  │
     ↓                              ↓
CapabilityExecutor  ←──────── Capability (handler, timeout, retry)
     │  1 resolve   2 exists   3 inputs   4 dependencies
     │  5 invoke    6 normalise 7 validate 8 record
     ↓
Specialist Agent / Engine
     ↓
Domain Result   (ForecastResult, FacilityResilienceRegistry, NetworkStateResult…)
     ↓
AgentResult[T]   status + typed output + provenance
     ↓
ExecutionContext   capability_status, typed authoritative fields
     ↓
Orchestrator
```

A planner arriving in Phase 8.3 gets three things it would otherwise have had to
build:

1. **One call for "run this."** `execute(capability_id, context)` — no plan
   required, no per-capability special case.
2. **A truthful answer when it asks for something premature.** The seam refuses
   and names what is missing, so a planner that gets the order wrong learns it
   instead of receiving a plausible zero.
3. **Declared criticality.** `required_dependencies` tells the planner which
   edges genuinely constrain order and which a provider handles itself — the
   information needed to build a graph rather than replay a template.

What Phase 8.3 must add is the decision: which capabilities a question needs, and
in what order. The executor will refuse to help with that, which is the point.
