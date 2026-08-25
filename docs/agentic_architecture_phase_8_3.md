# Phase 8.3 — Deterministic Orchestrator Planner / Execution Graph

**Status:** complete. No LLM planner, no retries, no rerouting, no concurrency.
**Planner overhead:** 380 µs to derive-and-validate a 7-capability plan; 223 µs on the production template path.
**Plan size:** 4.3 KB for 7 capabilities — 7× smaller than the network snapshot it plans against, and it names no facility or lane.

---

## 1. Existing planning architecture

The audit found a planner already doing part of this job, so nothing was
replaced.

`WorkflowPlanner.plan(resolution)` in `core/planner.py`:

- looks up one of ten hand-written `WORKFLOW_TEMPLATES`, keyed by `Intent`
- calls its `build(resolution)` to produce `List[PlanStep]`
- calls `plan.validate_dag()` — duplicate ids, unknown dependencies, cycles
- calls `registry.validate_plan_capabilities()` — every capability exists
- drops `optional` steps whose capability is unregistered, then re-validates
- returns a typed `ExecutionPlan`

`ExecutionPlan` and `PlanStep` were already typed Pydantic models with
`validate_dag()`, `execution_layers()`, `classify_dependencies()` and
HARD/SOFT criticality. Phase 8.1 added the capability contracts and
`dependency_map()`; Phase 8.2 added `optional_dependencies` and the executor.

### What was missing

**1. Dependency information was duplicated.** Templates hard-code step-level
`depends_on` ids. Contracts separately declare capability-level `dependencies`.
Nothing checked that the two agreed, so they could drift silently.

**2. No plannable enforcement.** Nothing structurally prevented a plan naming a
SERVICE or EMBEDDED capability. Only a test stood between the planner and
scheduling the twin projection.

**3. No context-awareness.** `plan()` never received the `ExecutionContext`, so
it could not know what a run had already produced.

**4. No input-satisfiability check.** Contract `required_inputs` was never
consulted at plan time — a plan could be built that the executor would refuse.

**5. Only intent-keyed planning.** An intent with no template raised. There was
no way to plan for a set of capabilities.

**6. Thin plan metadata.** No `request_id`/`execution_id`, no record of where the
structure came from, no validation status, no rationale.

**7. One planning failure code.** `PlanningFailureError` could not distinguish
"unsupported request" from "cycle" from "missing dependency".

### What was retained, and why

The ten templates encode deliberate **exclusions**, and the reasoning behind them
is domain judgement no dependency graph contains:

- a forecast question runs **no solver** — optimising against an estimate is a
  separate act with its own entry point (`build_forecast_scenario`)
- a market-intelligence message runs **no forecast** — a signal reaching the
  forecaster by virtue of having been mentioned is precisely the routing decision
  the orchestrator exists to make
- an explanation runs **no optimization** — it must not launch a fresh solve
  nobody asked for
- a status query runs **no solver** — a count of facilities does not need an
  optimum

A graph closure knows what a capability **needs**. It cannot know what a question
should be **refused**. So the templates stay authoritative for the intents they
cover, and derivation is goal-driven.

---

## 2. Planner responsibilities

```
                 ┌─ plan(resolution)  → template, for a known intent
WorkflowPlanner ─┤
                 └─ derive(goals)     → CapabilityGraphPlanner, from contracts

both  →  PlanValidator.assert_valid()  →  typed, validated ExecutionPlan
```

One class, two entry points, one contract out, and **both validated**. The graph
planner is reached *through* `WorkflowPlanner`, so a caller cannot bypass the
validation it guarantees.

The planner determines **what** should execute and **in what order**. Nothing
else. It computes no forecast, no REI, no RF, no cost and no verdict — asserted
structurally in §9.

---

## 3. Planner contract

`ExecutionPlan` was extended, not replaced. Every field added has a default, so
all ten templates construct unchanged.

```python
class ExecutionPlan(BaseModel):
    plan_id: str
    workflow_id: str
    intent: str
    steps: List[PlanStep]
    description: str
    request_id: str = ""                       # NEW
    execution_id: str = ""                     # NEW
    origin: PlanOrigin = TEMPLATE              # NEW  TEMPLATE | CAPABILITY_GRAPH
    validation: PlanValidation                 # NEW
    rationale: List[str] = []                  # NEW
```

`origin` matters because it says how much the plan's *absences* can be trusted: a
template author decided what to leave out; a derived plan knows only what was
asked for.

`validation.checked` is False until a validator has run, so an **unvalidated plan
is distinguishable from a valid one** rather than assumed fine.

`PlanStep` gained the declared contract, copied at plan time so a plan is
self-contained and inspectable without cross-referencing the registry:

```python
required_inputs: List[str]      optional_inputs: List[str]
expected_output: str            domain: str
timeout_seconds: Optional[float]  execution_mode: Optional[ExecutionMode]
```

---

## 4. Executable vs plannable

Phase 8.2 discovered the distinction; Phase 8.3 makes it **explicit metadata**.

```python
planner_selectable: bool = True    # may a planner choose this as a GOAL?
```

- **Executable** — the executor can invoke it. All **16** capabilities are.
- **Plannable** — a planner may select it independently. **13** are.

`is_plan_schedulable` now requires *both* `invocation == ORCHESTRATED` **and**
`planner_selectable`. A model validator refuses the inconsistent combination, so
a SERVICE capability cannot claim to be selectable.

| Capability | Executable | Plannable | Reached through |
|---|---|---|---|
| `extraction.parse` | ✅ | ❌ | the ingestion API, before a run exists |
| `signal.route_for_forecast` | ✅ | ❌ | `forecast.demand` |
| `twin.publish` | ✅ | ❌ | the orchestrator, after the plan settles |
| the other 13 | ✅ | ✅ | a plan step |

The field is separate from `invocation` rather than derived from it, so an
ORCHESTRATED capability could later be withheld from independent selection
without changing how it is invoked. **No capability is marked so speculatively.**

### The Digital Twin — a deliberate deviation from the brief

§4's second example lists DIGITAL_TWIN as a plan step. It is not one here, and
that is intentional.

`Orchestrator._project_twin` already publishes after every run and is the **only**
path into the twin. Scheduling `twin.publish` as well would publish twice, and
§10 requires preserving existing behaviour. The test for §11.11 therefore asserts
the real property: after a completed authoritative run, a twin state exists and
the plan never mentioned it.

---

## 5. Dependency graph

`CapabilityGraphPlanner.derive(goals)`:

1. **check goals** — each must exist and be plannable, else refuse
2. **close** over `required_dependencies`, transitively, breadth-first over a
   *sorted* frontier. `optional_dependencies` are **not** added — adding them
   would schedule work the provider is explicitly built to do without
3. **drop** capabilities the context already satisfies (§7)
4. **refuse** if a needed capability already failed (§7, §15)
5. **order** by Kahn's algorithm, taking the alphabetically first ready
   capability at each step
6. **build steps**, copying each contract onto its step
7. **validate**

### Terminal rank — a gap derivation exposed

Reasoning and governance declare **no** hard dependencies, deliberately: a
missing input must not suppress the narrative, and governance must always return
a verdict. So the dependency graph has no edge placing them at the end — and
ordering by dependencies alone put them **first**, with governance ruling on
evidence that did not exist yet.

"Runs last" and "depends on everything" are different claims, and only the first
is true. Hence:

```python
terminal_rank: int = 0    # 0 = not terminal; higher runs later
```

`reasoning.synthesise` is 1, `governance.classify` is 2. Terminal steps take
**SOFT** edges to everything before them — exactly what `_reason_and_govern` does
in the templates, derived rather than repeated.

### Deterministic ordering, no concurrency

`ordered_step_ids()` flattens `execution_layers()`, which is already sorted within
each layer. Two independent capabilities always come out in the same order.
Phase 8.3 runs strictly sequentially; the layering that permits concurrency is
preserved on the plan but not acted on.

### Example plans

```
network state    load → optimization.solve → kpi → reasoning → governance
forecast only    load → forecast.demand → reasoning → governance          (no solver)
resilience       load → resilience.assess → risk.compute_rf → reasoning → governance
signal+forecast  load → forecast.demand → market.score_signal → reasoning → governance
forecast+optim   load → forecast.demand → optimization.solve → kpi
impact analysis  load → forecast → interpret_signal → rei → optimization → kpi → risk → reasoning → governance
```

---

## 6. Plan validation

`PlanValidator.validate(plan, context)` runs on **every** plan — template or
derived — and collects **every** violation rather than stopping at the first.

| Check | Reason on failure |
|---|---|
| every capability registered | `UNKNOWN_CAPABILITY` |
| every capability plannable | `NOT_PLANNABLE` |
| required inputs supplied | `UNSATISFIABLE_INPUT` |
| required dependencies met | `MISSING_HARD_DEPENDENCY` |
| graph acyclic | `DEPENDENCY_CYCLE` |
| dependencies resolvable | `INVALID_ORDERING` |
| step ids unique | `DUPLICATE_STEP` |
| plan non-empty | `EMPTY_PLAN` |
| no failed prerequisite | `BLOCKED_BY_FAILURE` |
| intent supported | `NO_WORKFLOW_FOR_INTENT` |

A dependency is satisfied three ways, any one sufficient:

1. another step in this plan provides the capability
2. another step provides a capability in the **same domain** — this is what lets
   `kpi.summarise`, whose contract can name only `optimization.solve`, be
   satisfied by `optimization.solve_scenario` in a scenario workflow
3. the context already holds a usable result

**An invalid plan never reaches the executor.** `assert_valid` raises
`PlanRefused` — a subclass of the existing `PlanningFailureError`, so every
current handler still catches it — carrying the typed `PlanValidation`. **No step
is dropped and no partial plan is returned**: silently pruning an invalid plan
would execute something nobody designed.

**All ten existing templates pass unchanged.** That is the strongest single
result of this phase: the contracts and the hand-written graphs agree about every
dependency.

---

## 7. Context-aware planning

The planner reads the context; it does not manage failure.

**Derived plans** omit a capability the context already satisfies, and record
why:

```
'forecast.demand' not scheduled: this execution already holds a usable result (SUCCESS)
```

`skip_satisfied=False` forces a fresh run — which is how a caller says
"recompute", rather than the planner guessing.

**Template plans are never pruned.** A template's shape is a deliberate design;
removing a step because an earlier run produced something similar would execute a
workflow nobody wrote. The overlap is noted in `rationale` instead.

**A failed prerequisite blocks.** The planner reports `BLOCKED_BY_FAILURE` and
stops. It does not retry — that decision belongs to Phase 8.4, and quietly
re-running here would take it away.

---

## 8. Failure boundary

```
Planner → PlanRefused(validation) → Orchestrator
```

No retry, no alternative provider, no guessed input, no silently dropped step.
`PlanRefused.reasons` gives Phase 8.4 a typed place to branch.

---

## 9. Authority boundaries

Verified structurally by AST checks on the planner's own source.

| Rule | How |
|---|---|
| Planner calls no model | imports checked: no `openai`, `anthropic`; no `generate`/`chat`/`complete` call |
| Planner is deterministic | no `random`, `time`, `datetime`, `uuid`, `secrets` import; no `shuffle`/`now`/`uuid4` call |
| Planner executes nothing | no `await`, no `async def`, no executor import |
| Planner computes no domain value | no `risk_factor`, `assess_network_risk`, `solve`, `total_cost` |
| Planner writes no authoritative result | no `rei_registry =`, `forecast_result =`, `record_step`, `engine_results[` |
| Planner imports no engine | no `optimization`, `resilience`, `forecasting`, `ingestion`, `agents`, `pulp` |
| No specialist can reach the planner | AST over `agents/*.py` |
| One planning surface | the graph planner is reached only through `WorkflowPlanner` |
| Signal Router keeps forecast authority | planner may schedule scoring; never interprets, never converts confidence to probability, never reaches RF |

Determinism is also asserted behaviourally: the same goals produce a
byte-identical plan, goal order does not affect the result, and **three fresh
registries produce the same ordering** — which is what catches dict or set
iteration order leaking into output.

---

## 10. Existing workflows — the §10 decision

| Option | Decision |
|---|---|
| A. remain explicit deterministic workflows | **Chosen for all ten** |
| B. represented as planner templates | already are |
| C. replaced by planner-generated plans | **declined** |

Reason: the templates encode exclusions that derivation cannot recover, and §10
says not to rewrite workflows for architectural symmetry.

But the two are now **checked against each other**. Deriving from a template's
*own* capability set reproduces the template's ordering for **9 of 10**
workflows. This is the drift check the audit was looking for, and it uses the
template as the source rather than a second copy of it.

### The tenth — a real limitation, pinned

`wf_scenario_analysis` derives the same capability **set** in a different
**order**. `kpi.summarise` declares a dependency on `optimization.solve`; in the
scenario workflow the KPI step is meant to summarise the **scenario** solve, and
`summarise_kpis` reads whatever its `depends_on` produced:

```python
source = next((p for p in req.upstream.values()
               if isinstance(p, dict) and p.get("business_network_cost") is not None), {})
```

So a derived scenario plan would report **baseline** KPIs for a scenario
question. The contract cannot currently say "whichever optimization ran", so the
template stays authoritative for scenario workflows. A test pins this and fails
if it changes in either direction.

---

## 11. Why the planner is deterministic

Because a plan is the record of what the system decided to do, and a
non-reproducible plan cannot be audited, diffed or reasoned about after the fact.
If the same question produced different plans on different days, no trace would
be explainable and no regression would be attributable.

It also keeps the eventual LLM planner honest: a deterministic planner is the
thing a model's proposal gets **checked against**.

```
User Request
     ↓
Orchestrator
     ↓
Deterministic Planner ──── CapabilityRegistry (contracts, dependencies)
     ↓
Validated ExecutionPlan   ← PlanValidator: exists, plannable, inputs,
     ↓                        dependencies, acyclic, ordered, non-empty
CapabilityExecutor
     ↓
AgentResult
     ↓
ExecutionContext
     ↓
Orchestrator
```

---

## 12. Deliberately deferred to Phase 8.4+

- **Retry, reroute, escalation, circuit breakers** — `BLOCKED_BY_FAILURE` is
  reported; nothing acts on it
- **Parallel execution** — `execution_layers()` exists; ordering is sequential
- **Autonomous replanning** — a refused plan returns to the caller
- **LLM planning** — §13
- **Domain-level dependencies** — would let `kpi.summarise` bind to whichever
  optimization ran, closing the tenth-workflow gap
- **Typed context fields** for `kpi.summarise` and `scenario.validate`, so their
  output types become checkable (carried over from Phase 8.2)
- **Entity-name grounding** — still open from Phase 8.0.1, and a prerequisite for
  §13

---

## 13. How an LLM planner can propose without becoming authoritative

The seam is already shaped for it:

```
model proposes  →  goals: List[str]          (capability ids, nothing more)
                        ↓
registry validates →  exist? plannable?      (a hallucinated capability is refused here)
                        ↓
deterministic planner → closes dependencies, orders, builds the plan
                        ↓
PlanValidator         → the full §6 checklist
                        ↓
CapabilityExecutor    → the only execution seam
```

Three properties make this safe, and all three exist today:

1. **The model's output is a list of names, not a graph.** It cannot choose
   execution order, invent a dependency, or omit a prerequisite — the
   deterministic planner derives all of that. A wrong proposal produces a wrong
   *scope*, never a malformed run.
2. **Every name is checked against the registry.** A hallucinated capability is
   `UNKNOWN_CAPABILITY`; a service capability is `NOT_PLANNABLE`. Both refuse
   before anything executes.
3. **The model never touches a result.** It proposes before execution and has no
   path to the executor or to any authoritative field.

What must be added first: bounded proposal size, a deterministic fallback when
the model is unavailable (the templates already are one), and entity-name
grounding — because a planner acting on model-proposed capabilities makes a
fabricated facility name materially more consequential than it is today.
