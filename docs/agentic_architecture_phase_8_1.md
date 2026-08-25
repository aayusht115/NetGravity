# Phase 8.1 — Agent Contract and Capability Registry

**Status:** complete. No planner, no agent framework, no retry logic.
**Regression:** 2,116 → 2,203 passed, 4 skipped, 0 failed (+87 new, none changed).
**Behaviour change:** none. Every addition is a view over state the system already kept.

---

## 1. The architecture this phase found

The audit came first, and it changed the plan. The brief was written as though the
capability layer had to be built; most of it already existed and was better than a
fresh design would have been.

What was already there:

| Concern | Existing component | Verdict |
|---|---|---|
| Execution result | `ToolResult` (`schemas/plans.py`) | Reused, extended by one optional field |
| Capability declaration | `Capability` (`tools/base.py`) | Reused, extended by one optional field |
| Registry | `CapabilityRegistry` (`routing/`) | **Extended**, not replaced |
| Execution state | `ExecutionContext` (`core/`) | **Extended**, not replaced |
| State machine | `ExecutionState` + `LEGAL_TRANSITIONS` | Untouched |
| Dependencies | `Capability.dependencies`, `PlanStep.depends_on`, `DependencyType.HARD/SOFT` | Untouched |
| Missing evidence | `EvidenceStatus`, `UnavailableEvidence` | Reused as the basis of two statuses |
| Retry classification | `FailureClass`, `RetryPolicy` | Reused as the basis of two statuses |
| Idempotency / run storage | `ExecutionStateStore` | Reused, no new store |

Thirteen capabilities were registered with handlers, named in a dotted namespace
(`optimization.solve`, `resilience.assess`), and the executor already enforced
timeouts, retries, error classification and HARD/SOFT dependency criticality.

So this phase did not introduce a contract layer. It closed the specific gaps
that stopped the existing one from being plannable.

### The four real gaps

**1. Outcome was a boolean.** `ToolResult.success` cannot express the difference
between "the solver proved infeasibility", "the engine timed out", "we produced
something the validators rejected" and "nobody could compute this because the
inputs were absent". All four arrived as `success=False`. The information existed
— in `error_code`, `failure_class`, `EvidenceStatus` — but it was scattered
across three vocabularies, and no consumer could branch on it uniformly.

**2. No typed output on the envelope.** `ToolResult.output` is
`Dict[str, Any]` — a flattened projection for transport. The authoritative typed
results lived in dedicated `ExecutionContext` fields (`rei_registry`,
`forecast_result`, `network_states`, `risk_results`). Correct, and load-bearing:
rebuilding a `FacilityResilienceRegistry` from the flattened dict would default a
FAILED node's status to OK. But a caller had to know which field to read for
which capability, and nothing recorded that mapping.

**3. `Capability.input_schema` and `output_schema` were dead.** Both fields
existed. Neither was populated anywhere in the codebase. A planner asking "what
does this produce?" had no answer.

**4. Three real capabilities were invisible.** Extraction, the Digital Twin
projection and forecast signal routing all have real providers and real results,
and none appeared in the registry — because none is a plan step. "What can this
system do?" had no single answer.

---

## 2. Why a common contract is required

A planner has to decide what to do next from an outcome it did not produce. That
requires two things the previous shape could not give it.

**It must be able to branch without knowing the specialist.** "Did this produce
usable evidence?" has to be answerable the same way for a MILP solve, an REI
sweep and a narrative. Otherwise every planner branch grows a per-capability
special case, and the registry's whole point — that adding an agent needs no core
change — is lost on the planning side.

**It must never be able to read a failure as a value.** This is the sharper
requirement. The dangerous shape is not a crash; it is
`result.output.get("total_cost", 0)` on a run that never solved. A zero cost is a
plausible number. It flows into a KPI, into a comparison, into a narrative, and
nothing downstream can tell it apart from a measurement.

`AgentResult` makes that shape unconstructible rather than discouraged.

---

## 3. `AgentResult[T]`

`netgravity/orchestrator/schemas/agent_result.py`

```python
class AgentResult(BaseModel, Generic[T]):
    capability: str
    status: AgentStatus
    output: Optional[T] = None          # the authoritative domain object
    agent: str = ""
    execution_id: str = ""
    errors: List[AgentError] = []
    warnings: List[str] = []
    unavailable: Dict[str, UnavailableEvidence] = {}
    provenance: ResultProvenance
    metadata: Dict[str, Any] = {}
```

Generic in the payload, so the type survives the trip:
`AgentResult[ForecastResult]`, never `AgentResult` around a dict. The domain
result stays authoritative and is carried by reference — a test asserts
`result.output is the_registry_object`.

### The six statuses

| Status | Means | Derived from |
|---|---|---|
| `SUCCESS` | Complete, valid, all declared inputs present | `success=True`, nothing else |
| `PARTIAL` | Usable, knowingly incomplete | a degraded SOFT dependency, or a handler saying so |
| `RETRYABLE_FAILURE` | Transient; re-running could work | `FailureClass.RETRYABLE` |
| `NON_RETRYABLE_FAILURE` | Deterministic; re-running gives the same answer | `FailureClass.NON_RETRYABLE` |
| `INVALID_OUTPUT` | It ran and produced something validation refused | `VALIDATION_FAILURE`, or validator warnings |
| `INSUFFICIENT_EVIDENCE` | Never attempted, or not computable, for want of inputs | `MISSING_DATA`, `DEPENDENCY_FAILURE`, `EvidenceStatus.NOT_RUN` |

Every one of these is derived from evidence the codebase already produced. The
classification is a pure function, `AgentResult.classify`, so it is identical
everywhere and testable on its own.

Three distinctions in that table are worth stating plainly:

**Infeasibility is `NON_RETRYABLE_FAILURE`, not an error.** The solver *proved*
there is no feasible solution. That is a finding. Retrying spends solver time to
obtain the same answer.

**A rejected result is `INVALID_OUTPUT`, not a failure.** Nothing malfunctioned —
the engine ran and the validators refused what it produced. Worse than a missing
result, because it looks usable. The rejected payload is preserved in
`metadata["rejected_output"]` for diagnosis and kept out of `output`.

**Absent inputs are `INSUFFICIENT_EVIDENCE`, not a failure.** "Nobody could
compute this" and "this broke" call for different responses. Collapsing them
points an operator at the wrong component.

### The invariants

A `model_validator` refuses three shapes at construction:

```python
status in NO_OUTPUT_STATUSES and output is not None   → ValueError
status in USABLE_STATUSES     and output is None      → ValueError
status in NO_OUTPUT_STATUSES and no errors and no unavailable → ValueError
```

The first is the one that matters. A result that cannot be trusted **cannot hold
a value**, so `result.output or 0` has nothing to find. The third refuses an
unexplained failure — a failure nobody can act on is not a report.

`require()` exists as the alternative to `or <default>`: it returns the output or
raises with the reason.

### What was NOT done to `ToolResult`

`ToolResult` keeps `success: bool` as the executor's own contract, and every
existing caller keeps reading it. One optional field was added:

```python
status: Optional[AgentStatus] = None
```

Left `None` by default, in which case the status is derived. No handler was
obliged to change, and **none did**. The field only lets a future handler say
something the boolean cannot — that a batch was partial, for instance.

---

## 4. Capability model

`netgravity/orchestrator/schemas/capability.py`

```python
class CapabilityContract(BaseModel):        # frozen
    capability_id: str
    domain: CapabilityDomain
    provider: str                    # "ForecastingService"
    input_type: str                  # "ForecastRequest"
    output_type: str                 # "ForecastResult"
    authoritative_field: str         # ExecutionContext attribute holding it
    dependencies: Tuple[str, ...]
    required_inputs: Tuple[str, ...]
    validations: Tuple[str, ...]
    execution_mode: ExecutionMode
    invocation: InvocationMode
    host_capability: Optional[str]
    llm_backed: bool
```

Frozen: a contract describes the system as built. A mutable one would let the
planner and the executor disagree about what a capability does, and the failure
would surface far from the change.

`authoritative_field` is the fix for gap 2 — it records, in one place, which
typed `ExecutionContext` field holds a capability's real result. A test checks
every declared field name against the actual dataclass fields.

### Domains

Thirteen, covering the eight the brief requires plus the five the existing
capabilities need:

```
EXTRACTION   SIGNAL_INTERPRETATION   SIGNAL_ROUTING   FORECAST
RESILIENCE   OPTIMIZATION            NETWORK_STATE    SCENARIO
KPI          RISK                    REASONING        GOVERNANCE
DIGITAL_TWIN
```

Resolution is **by domain, not by name** — "who can forecast?" — so a provider
can be added or replaced without a planner change.

`SIGNAL_INTERPRETATION` and `SIGNAL_ROUTING` are deliberately separate, and the
separation is load-bearing. Interpretation derives the likelihood that feeds
`RF = P + REI − P·REI`. Routing decides whether a reported signal is relevant
enough to reach a forecast, and its confidence score never touches RF. A single
"signals" domain would invite a planner to substitute one for the other — exactly
the conflation the risk chain is built to prevent. A test asserts the two domains
share no provider and that RF depends on the first and never the second.

### Invocation modes — the honest part

```
ORCHESTRATED   registered with a handler; a planner may schedule it       (13)
SERVICE        invoked outside the plan graph                             (2)
EMBEDDED       a gated stage inside another capability's handler          (1)
```

This is how gap 4 is closed without lying. All three previously-invisible
capabilities are now declared:

- **`extraction.parse`** — SERVICE. Runs *before* an execution exists; it
  produces the network a run is later pinned to, so it cannot be a step inside
  that run.
- **`twin.publish`** — SERVICE. Runs *after* the plan settles, composing results
  that are already authoritative. It computes nothing.
- **`signal.route_for_forecast`** — EMBEDDED in `forecast.demand`. A gate inside
  that handler with no independent entry point.

They are declared so "what can this system do?" has one answer. They are marked
so a planner that tried to schedule one would be wrong — and
`resolve_capability()` defaults to `schedulable_only=True`, so it cannot be
handed one by accident.

### Conversation/NLU is deliberately absent

§3 says to include it "only if it is actually an executable capability". It is
not. `ChatService` runs `ConversationalNLU` to work out *which* capabilities a
turn needs, before any execution exists. It is the **caller** of the capability
layer, not a member of it. Registering it would create a cycle in which
understanding a request became a step inside executing one.

A test asserts no declared capability mentions NLU or conversation, so the
omission stays a decision rather than decaying into an oversight.

---

## 5. Capability registry

`CapabilityRegistry` was **extended**, not duplicated. Contracts live in a
separate store from handlers *inside the same registry object*:

```python
self._capabilities: Dict[str, Capability]          # executable
self._tools:        Dict[str, CapabilityTool]      # executable
self._contracts:    Dict[str, CapabilityContract]  # metadata
```

The separation is the mechanism, not a comment: a contract can exist with no
entry in `_tools`, so a metadata lookup has **no handler to call even by
accident**. A test registers a capability whose handler raises on invocation and
then calls every metadata method.

New surface, all read-only:

| Method | Answers |
|---|---|
| `register_contract` / `register_contracts` | declaration, with duplicate prevention |
| `contract(id)` | the declaration, or `CapabilityNotFoundError` |
| `resolve(domain)` | every capability serving a domain |
| `resolve_capability(domain)` | one schedulable provider, or `None` |
| `validate_inputs(id, available)` | which declared inputs are absent |
| `dependency_map()` | `id → dependencies` |
| `authoritative()` / `schedulable()` | filtered views |
| `undeclared()` / `unimplemented()` | the gaps, made visible |

`undeclared()` and `unimplemented()` are the self-audit. Current state:

```
registered handlers : 13
declared contracts  : 16
undeclared          : []                                        ← no handler lacks metadata
unimplemented       : [extraction.parse, signal.route_for_forecast, twin.publish]
```

`unimplemented()` returning exactly the SERVICE and EMBEDDED three is asserted by
a test. An ORCHESTRATED capability appearing there would be a real defect — a
plan could name it and fail at execution time.

Contracts are attached to their capabilities at wiring time by name lookup, so a
handler and its declaration are registered in one act:

```python
for capability in capabilities:
    capability.contract = CONTRACTS_BY_ID.get(capability.name)
registry.register_all(capabilities)
registry.register_contracts(CAPABILITY_CONTRACTS, replace=True)
```

---

## 6. `ExecutionContext` changes

Extended, with **no new store**. One field was added:

```python
capability_status: Dict[str, AgentStatus]
```

The existing lists are keyed by *step id*, which is right for executing a plan —
the same capability can legitimately be two steps. But a planner asks
capability-shaped questions: "do we have resilience evidence yet?"

`capability_status` is written **only** by `record_step` and the `record_*`
methods, so it cannot disagree with the step lists it summarises. A test asserts
that correspondence directly.

Everything else is derived, computed on demand:

```python
completed_capabilities()    failed_capabilities()    pending_capabilities()
capability_outcome(cap)     typed_output(cap, field)
agent_result(cap, ...)      capability_provenance()
```

`pending_capabilities()` is computed from the plan each call rather than stored —
a stored copy is one more thing that can fall out of step.

Two details worth recording:

**Disagreeing steps fold to PARTIAL.** A comparison run solves two scenarios
through one capability. If one succeeds and one fails, SUCCESS hides the failure
and failure discards the good solve. PARTIAL is the accurate answer.

**`record_unavailable` uses `setdefault`, not assignment.** `record_step`
classifies a failure and *then* records the missing evidence. Assignment would
overwrite `RETRYABLE_FAILURE` with `INSUFFICIENT_EVIDENCE` and destroy the
retryable distinction on every engine fault. Only a capability with no outcome at
all is INSUFFICIENT.

`agent_result()` is a **view**, built on demand. It reads the typed field named by
the contract — never `engine_results`. A capability that never ran returns
`INSUFFICIENT_EVIDENCE`, never an empty success. A test builds envelopes for every
recorded capability and asserts `step_results` is byte-identical afterwards.

---

## 7. Dependency model

Dependencies are stated as **facts about a capability**, not as an order:

```
network.load_snapshot      → ()
optimization.solve         → (network.load_snapshot,)
resilience.assess          → (network.load_snapshot,)
risk.compute_rf            → (resilience.assess, external.interpret_signal)
scenario.validate          → (scenario.create,)
optimization.solve_scenario→ (scenario.validate,)
twin.publish               → (optimization.solve, resilience.assess, risk.compute_rf)
reasoning.synthesise       → ()      ← intentionally empty
governance.classify        → ()      ← intentionally empty
```

The two empty entries are deliberate and for different reasons.

**Reasoning** explains whatever evidence exists, and every edge into it is SOFT in
the plan. Declaring hard dependencies would suggest a missing input should
suppress the narrative, when the requirement is the opposite: say what is missing.

**Governance** must *always* return a verdict. Missing evidence makes it more
conservative, never absent, so nothing it reads may be a precondition that blocks
it.

### No universal workflow

This is the point of §6, and it is demonstrated rather than asserted. A state
query resolves to five capabilities:

```
"what does the network look like now?"
  → load → optimize → kpi → reason → govern
```

Forecast, REI and RF are **not** in that plan, and each reports
`INSUFFICIENT_EVIDENCE` rather than zero. A test asserts exactly this. A scenario
question resolves to a different, longer graph. The registry supplies the raw
material; the planner decides.

```
User Request
     ↓
Orchestrator ─────────────→ Capability Registry     (metadata: who, what, needs)
     │                            │
     │  resolve_capability(domain)│
     │  validate_inputs(id, ...)  │
     ↓                            ↓
Specialist Agent  ←──── Capability (handler, timeout, retry)
     ↓
Domain Result  (ForecastResult, FacilityResilienceRegistry, NetworkStateResult…)
     ↓
AgentResult[T]   status + typed output + provenance
     ↓
ExecutionContext   capability_status, typed authoritative fields
     ↓
Orchestrator
```

---

## 8. Architectural boundaries

Verified structurally by AST import-graph tests, not by convention.

| Rule | Verified | How |
|---|---|---|
| Registry does not execute business logic | ✅ | handler raises if called; every metadata method invoked |
| Registry modules import no engine | ✅ | AST: no `optimization`, `resilience`, `forecasting`, `pulp` |
| Forecasting cannot invoke MILP / REI / RF | ✅ | AST over `forecasting/**` |
| Digital Twin invokes no engine | ✅ | AST over `orchestrator/twin/**` |
| Reasoning cannot modify authoritative results | ✅ | `is_authoritative` False for PROBABILISTIC; `numeric_grounding` declared |
| Governance remains authoritative | ✅ | DETERMINISTIC, `llm_backed=False` |
| RF probability separate from signal confidence | ✅ | disjoint domains; RF depends on interpretation, not routing |
| Planner never schedules SERVICE/EMBEDDED | ✅ | AST over every `PlanStep(...)` in the planner |
| Every workflow references a registered capability | ✅ | all 10 templates built and checked |
| Specialists do not reach an engine or the control plane | ✅ | AST over `orchestrator/agents/*.py` |
| Agents do not invoke other specialist agents | ⚠️ | **one documented exception — see below** |

### The one exception, reported rather than hidden

`ExtractionParsingAgent` imports and calls `ExternalSignalAgent`:

```python
signal = ExternalSignalAgent(None).interpret(
    request.source, known_facility_ids=known, allow_llm=False,
)
```

This is a genuine agent-to-agent call and a real, narrow departure from §9's
rule. It is **pre-existing**, predating this phase, and it is constrained: it is
used for *parsing* only, with the model tier explicitly disabled
(`allow_llm=False`), and it stops at the signal — it never looks up REI and never
computes RF.

Rather than weaken the boundary test into something that would pass, the test
**pins** it:

```python
assert edges == {
    "extraction_agent.py": ["...agents.external_signal_agent"]
}, f"an undocumented agent-to-agent dependency appeared: {edges}"
```

A second such edge fails the suite. The test additionally asserts that
`extraction_agent.py` contains neither `assess_network_risk` nor `risk_factor`,
so that edge cannot become a back door into the risk chain.

**Recommendation, deferred:** invert it — move the shared text-to-signal parsing
into a helper both agents call, so neither depends on the other. Behaviour-neutral,
but it is a refactor of a specialist's internals, which §7 forbids in this phase.

---

## 9. What this phase intentionally does NOT implement

Per §11, all deliberately absent, and asserted absent by tests:

- **LLM planning** — no model call anywhere in the new code. A test checks the
  four new modules import no `openai`/`agents`/`agno`/`langchain`/`litellm` and
  contain no `LLMGateway` reference.
- **OpenAI Agents SDK / Agno** — not imported, not installed.
- **Retry / reroute / escalation / circuit breakers** — `is_retryable` is
  *reported* and acted on nowhere. A test parses the seven new control-plane
  methods and asserts none contains `Await`, `asyncio`, `sleep`, `retry`,
  `reroute`, `escalate` or `generate`.
- **Agent handoffs, autonomous tool selection, parallel execution changes** —
  the existing layer-based executor is untouched.
- **Dynamic routing** — `resolve_capability` returns metadata. It selects nothing.

Also not done, and worth naming:

- `Capability.input_schema` / `output_schema` remain unpopulated. The contract's
  `input_type` / `output_type` supersede them. Removing the dead fields is a
  behaviour-neutral cleanup left for a later phase.
- Handlers still return `ToolResult`, not `AgentResult`. That was the design
  choice: converting them would have changed execution behaviour in a phase
  required not to.

---

## 10. Proposed Phase 8.2

In the order that makes the next phase cheap:

1. **Adopt `AgentResult` at the executor seam.** Have `CapabilityTool.execute`
   produce it directly, with `ToolResult` retained as its serialised projection.
   One place changes; every handler keeps its signature.
2. **Let handlers declare PARTIAL.** Three already know they are partial —
   the REI batch (`REIBatchStatus.PARTIAL`), forecasting (per-series
   `ForecastStatus`), and extraction (`WARNING`). The field is there; the
   handlers do not use it yet.
3. **Deterministic capability-graph planner.** Build the plan from
   `dependency_map()` and the resolved domains instead of `WORKFLOW_TEMPLATES`.
   Still no model — prove domain resolution reproduces the ten existing
   workflows before anything decides anything.
4. **Retry and escalation policy**, reading `is_retryable` and `FailureClass`.
   The classification is already correct; only the actor is missing.
5. **Then, and only then, an LLM planner** — constrained to *proposing* a
   capability set, which the registry validates and the deterministic planner
   orders. The model must never select execution order or touch a result.

Prerequisite for step 5: entity-name grounding, still open from Phase 8.0.1.
`numeric_grounding` grounds numbers, not names, so a fabricated facility name can
survive as prose. A planner acting on model-proposed capabilities makes that gap
more consequential than it is today.
