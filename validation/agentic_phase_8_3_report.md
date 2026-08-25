# Phase 8.3 — Final Report

**Deterministic Orchestrator Planner / Execution Graph**
Date: 2026-08-25 · Work performed locally · **No Git/GitHub operations**

---

## Summary

| | |
|---|---|
| Regression before (Phase 8.2 final) | 2,260 passed · 4 skipped · 0 failed |
| Regression after | **2,316 passed · 4 skipped · 0 failed** |
| Tests added | 56 (one new module) |
| Tests deleted / skipped / weakened | **0** — one test made *stricter*, see §9 |
| Files created | 2 source + 1 test + 2 docs |
| Files modified | 5 source + 1 test |
| Planner overhead | **380 µs** derive+validate (7 capabilities); **223 µs** production template path |
| Plan size | 4.3 KB — **7× smaller** than the network it plans against |
| Frozen areas touched | **none** |
| LLM / retries / rerouting / escalation / concurrency | **not introduced** |

The headline result: **all ten existing workflow templates pass the new
validator unchanged**, and deriving a plan from a template's own capability set
reproduces that template's ordering for **9 of 10**. The contracts and the
hand-written graphs agree — which is exactly the drift the audit went looking
for. The tenth disagreement is real, is explained in §6, and is pinned by a test.

---

## 1. Audit findings

### 1.1 A planner already existed

`WorkflowPlanner.plan(resolution)` looked up one of ten hand-written
`WORKFLOW_TEMPLATES`, built `List[PlanStep]`, ran `validate_dag()` and
`validate_plan_capabilities()`, dropped unregistered optional steps, and returned
a typed `ExecutionPlan`. `ExecutionPlan`/`PlanStep` already carried HARD/SOFT
criticality, cycle detection and layering.

**So nothing was replaced.** Per §1, the existing planner was extended.

### 1.2 The seven gaps

| # | Gap | Consequence |
|---|---|---|
| 1 | Dependency info **duplicated** — templates hard-code step `depends_on`, contracts declare capability `dependencies`, nothing compared them | silent drift |
| 2 | No **plannable** enforcement | only a test stopped the planner scheduling the twin projection |
| 3 | Planner never saw `ExecutionContext` | could not know what was already done |
| 4 | `required_inputs` never checked at plan time | a plan could be built the executor would refuse |
| 5 | Only intent-keyed planning | no way to plan for a capability set |
| 6 | Thin plan metadata | no identity, origin, validation status or rationale |
| 7 | One planning failure code | "unsupported request" indistinguishable from "cycle" |

### 1.3 What must be retained

The templates encode deliberate **exclusions** — a forecast question runs no
solver, a market-intelligence message runs no forecast, an explanation launches
no optimisation, a status query runs no solver. A dependency graph knows what a
capability *needs*; it cannot know what a question should be *refused*.

**Decision:** templates stay authoritative for the intents they cover; derivation
is goal-driven; both paths are validated. Where the final plan enters the
executor is unchanged — `CapabilityExecutor` remains the sole seam.

---

## 2. Components reused

| Component | How |
|---|---|
| `WorkflowPlanner` | **Extended in place** — kept template lookup, DAG validation, optional-step dropping |
| `ExecutionPlan` / `PlanStep` | Extended with defaulted fields; all ten templates construct unchanged |
| `plan.validate_dag()` | Reused by the validator for cycles and unknown dependencies, not reimplemented |
| `registry.validate_plan_capabilities()` | Left exactly where it was, so its errors keep their current type and message |
| `CapabilityContract` dependencies / `optional_dependencies` | The graph planner's only source of ordering |
| `CapabilityRegistry` | Resolution and contracts. Unchanged. |
| `ExecutionContext.capability_status` | Read for context-awareness. Never written. |
| `PlanningFailureError` | `PlanRefused` subclasses it, so existing handlers still catch |
| `CapabilityExecutor` | Untouched. Still the only execution seam. |

---

## 3. New and modified components

**Created**

| File | What |
|---|---|
| `orchestrator/schemas/plan_validation.py` | `PlanOrigin`, `PlanFailureReason` (10 values), `PlanViolation`, `PlanValidation` |
| `orchestrator/core/plan_graph.py` | `CapabilityGraphPlanner`, `PlanValidator`, `PlanRefused` |
| `tests/test_plan_graph.py` | 56 tests |
| `docs/agentic_architecture_phase_8_3.md` | Architecture document |
| `validation/agentic_phase_8_3_report.md` | This report |

**Modified**

| File | Δ | Change |
|---|---|---|
| `schemas/plans.py` | +68 | plan identity, origin, validation, rationale; step contract fields; `capabilities`, `is_validated`, `ordered_step_ids()` |
| `schemas/capability.py` | +58 | `planner_selectable`, `terminal_rank`, `is_plannable`, consistency validator |
| `core/planner.py` | +99 | validator wired into the production path; `context` parameter; `derive()`; `capability_goals_for()` |
| `routing/capability_contracts.py` | +9 | three marked non-selectable; two terminal ranks |
| `core/orchestrator.py` | +4/−2 | planner receives the context |
| `tests/test_agent_contract.py` | +6/−3 | one test made stricter (§9) |

---

## 4. Capability / dependency model

Derivation closes over `required_dependencies` only. `optional_dependencies` are
deliberately **not** added — adding them would schedule work the provider is
explicitly built to do without, and RF reporting `NOT_COMPUTABLE` is strictly
more informative than a plan that quietly ran an interpretation nobody asked for.

A dependency counts as satisfied three ways: same capability in the plan, **same
domain** in the plan, or already usable in the context. The domain rule is what
lets `kpi.summarise` — whose contract can name only `optimization.solve` — be
satisfied by `optimization.solve_scenario`.

### Terminal rank — a gap derivation exposed

Reasoning and governance declare **no** hard dependencies, correctly. So the graph
had no edge placing them last, and ordering by dependencies alone put them
**first** — with governance ruling on evidence that did not exist yet.

"Runs last" and "depends on everything" are different claims, and only the first
is true of these two. `terminal_rank` (reasoning 1, governance 2) captures it, and
terminal steps take **SOFT** edges to everything before them — exactly what
`_reason_and_govern` does in the templates, derived rather than repeated.

---

## 5. Executable vs plannable

Made explicit metadata rather than implied by `invocation`:

```python
planner_selectable: bool = True
is_plan_schedulable = (invocation == ORCHESTRATED) and planner_selectable
```

**16 executable · 13 plannable.** A model validator refuses a SERVICE capability
that claims to be selectable, so the two facts cannot disagree.

The field is separate from `invocation` so an ORCHESTRATED capability could later
be withheld from independent selection without changing how it is invoked. **None
is marked so speculatively.**

### Deviation from the brief: the Digital Twin

§4's second example lists DIGITAL_TWIN as a plan step. It is **not** one here.
`Orchestrator._project_twin` already publishes after every run and is the only
path in; scheduling `twin.publish` as well would publish twice, and §10 requires
preserving existing behaviour. The §11.11 test asserts the real property instead:
after a completed authoritative run a twin state exists, and the plan never
mentioned it.

---

## 6. Test scenarios — all 18 from §11

| # | Scenario | Result |
|---|---|---|
| 1 | Network-state query | `load → optimize → kpi → reasoning → governance` |
| 2 | Forecast-only | `load → forecast → reasoning → governance` — **no solver, no REI** |
| 3 | Resilience | `load → rei → risk → reasoning → governance` — no signal interpretation |
| 4 | Optimization | `load → optimize → kpi → …` — **no forecast** |
| 5 | Forecast + disruption scenario | scenario chain ordered; `validate` before `solve_scenario` |
| 6 | External signal + forecast | scoring scheduled; **no RF, no interpretation** |
| 7 | Forecast + optimization | `load → forecast → optimize → kpi` |
| 8 | Full impact analysis | RF after both inputs though both are SOFT |
| 9 | Reasoning-only | valid — reasoning explains whatever exists |
| 10 | Governance-required | valid alone — every response leaves with a verdict |
| 11 | Twin after an authoritative run | state published, **never planned** |
| 12 | Missing capability | `UNKNOWN_CAPABILITY` |
| 13 | Missing HARD dependency | `MISSING_HARD_DEPENDENCY`, names `optimization.solve` |
| 14 | Cyclic dependency | `DEPENDENCY_CYCLE` |
| 15 | Non-plannable requested | `NOT_PLANNABLE` for all three, as goal *and* as step |
| 16 | Already completed | omitted, recorded in `rationale`; `skip_satisfied=False` forces a re-run |
| 17 | Failed prerequisite | `BLOCKED_BY_FAILURE` — **not retried** |
| 18 | Unsupported request | `No workflow is registered` — nothing invented |

Plus: unsatisfiable input, duplicate step id, empty plan, all-violations-reported,
and no-partial-pruning.

### Template agreement

All ten templates pass the new validator. Deriving from a template's own
capability set reproduces its ordering for **9 of 10**.

**The tenth — `wf_scenario_analysis`.** Same capability set, different order.
`kpi.summarise` declares a dependency on `optimization.solve`; in the scenario
workflow the KPI step is meant to summarise the **scenario** solve, and
`summarise_kpis` reads whatever its `depends_on` produced:

```python
source = next((p for p in req.upstream.values()
               if isinstance(p, dict) and p.get("business_network_cost") is not None), {})
```

So a derived scenario plan would report **baseline KPIs for a scenario question**.
The contract cannot currently express "whichever optimization ran". The template
stays authoritative for scenario workflows, and a test pins the difference so it
fails if it changes in either direction.

---

## 7. Failure cases and semantics

```
Planner → PlanRefused(validation) → Orchestrator
```

`PlanRefused` subclasses `PlanningFailureError` and carries a typed
`PlanValidation`, so Phase 8.4 can branch on `reasons` rather than parse prose.
Every violation is collected, not just the first — fixing three problems one
round-trip at a time is how a caller ends up guessing.

**No step is ever dropped to make a plan valid.** §6 requires this and a test
enforces it: silently pruning would execute something nobody designed.

---

## 8. Executor integration (§14)

One controlled end-to-end run on the Phase 8.0 synthetic network, in
`TestPlanToExecutorIntegration`:

1. **planner generated the plan** — `origin=CAPABILITY_GRAPH`, from goals
2. **plan was validated** — `checked=True`, zero violations, before anything ran
3. **executor received only valid steps** — `set(results) == plan.capabilities`
4. **outputs were recorded** — `set(context.step_results) == set(plan.ordered_step_ids())`
5. **dependencies were respected** — zero `INSUFFICIENT_EVIDENCE`, which is what
   an out-of-order run would produce
6. **authoritative results intact** — `NetworkStateResult` and
   `FacilityResilienceRegistry` arrive typed; reasoning is non-authoritative

The template path end-to-end is separately asserted unchanged, now behind the
validator.

---

## 9. Architectural boundary validation

| Rule | Result |
|---|---|
| Planner calls no model | ✅ AST imports + call targets |
| Planner is deterministic | ✅ no `random`/`time`/`datetime`/`uuid`/`secrets` import; no `shuffle`/`now`/`uuid4` |
| Same inputs → same plan | ✅ byte-identical; goal order irrelevant; **3 fresh registries agree** |
| Planner executes nothing | ✅ no `await`, no `async def`, no executor import |
| Planner computes no domain value | ✅ no `risk_factor`, `assess_network_risk`, `solve`, `total_cost` |
| Planner writes no authoritative result | ✅ no `rei_registry =`, `forecast_result =`, `record_step`, `engine_results[` |
| Planner imports no engine | ✅ AST |
| No specialist reaches the planner | ✅ AST over `agents/*.py` |
| Executor remains the sole seam | ✅ untouched |
| Signal Router keeps forecast authority | ✅ planner may schedule scoring; never interprets, never converts confidence to probability, never reaches RF |
| One planning surface | ✅ graph planner reachable only through `WorkflowPlanner` |

### The one test that changed

`test_a_service_capability_is_never_offered_as_schedulable` constructed a SERVICE
contract with the default `planner_selectable=True`. Phase 8.3's new consistency
validator correctly **refuses** that combination, so the test was updated to
declare `planner_selectable=False`.

That is a test made **stricter** by a new invariant, not weakened: it had been
constructing a contract the model now considers incoherent. No assertion was
loosened, and no test was skipped or deleted.

---

## 10. Regression results

Import/startup validation — all six affected modules import cleanly:

```
health    : {'status': 'ok', 'capabilities': 16, 'workflows': 10}
planner   : WorkflowPlanner | validator: PlanValidator | graph: CapabilityGraphPlanner
plannable : 13 of 16
```

Full suite, after all Phase 8.3 changes:

```
2316 passed, 4 skipped, 578298 warnings in 299.88s (0:04:59)
```

**2,260 → 2,316 is exactly +56**, the new module's count. No pre-existing test
changed status.

Phase 8.0 capability harness, re-run on the final code
(`validation/phase_8_3/capability_harness_rerun.txt`):

```
14 of 15 sections PASS · checks: 219/222 · live model calls: 0/20 (blocked 3)
```

Identical to Phases 8.0, 8.1 and 8.2. The one NOT_TESTED section is
`extraction_llm`, refused by the shared gateway with `daily_limit_exceeded` —
**0 API calls charged**, unrelated to this phase. Phase 8.0's artifacts were
backed up before the run and restored after; `git status` on
`validation/capability_validation/` is empty, confirming a byte-identical
restore.

---

## 11. Performance observations (§16)

No stress testing, per §16. Measured over 2,000 iterations each:

```
derive 7-goal plan (incl. validation) :  379.7 µs
validate an existing plan             :   64.5 µs
template plan + validation (prod path):  223.1 µs
```

Against a 14.8 ms MILP solve or a 138 ms REI sweep, planning is roughly 0.2–2.5 %
of one capability's cost, and a plan is built once per request.

**No large object is copied.** A plan holds capability ids and step ids:

```
plan JSON (7 capabilities) :   4,292 bytes
network snapshot JSON      :  29,246 bytes     (7× larger)
plan mentions 'facilities' : False
plan mentions 'lanes'      : False
```

A test asserts every `params` value is a scalar, so a plan cannot start carrying
a network.

---

## 12. IMPLEMENTED / VERIFIED / DEFERRED

### IMPLEMENTED

- `CapabilityGraphPlanner` — goal-driven, dependency-closing, deterministically ordered
- `PlanValidator` — 10 typed failure reasons, all violations collected
- `PlanRefused` carrying typed `PlanValidation`; subclasses the existing error
- `ExecutionPlan` extended: identity, `origin`, `validation`, `rationale`,
  `capabilities`, `is_validated`, `ordered_step_ids()`
- `PlanStep` extended with its declared contract, so a plan is self-contained
- `planner_selectable` — executable vs plannable, explicit and validated
- `terminal_rank` — ordering for capabilities that must run last without
  depending on everything
- Validation wired into the **production** planner path
- Context-aware planning: derived plans skip satisfied capabilities; templates
  are never pruned
- `WorkflowPlanner.derive()` and `capability_goals_for()`
- 56 tests, architecture document, this report

### VERIFIED

- All ten existing templates pass the new validator **unchanged**
- Derivation reproduces template ordering for **9 of 10** workflows
- All 18 §11 scenarios behave as specified, each with an explicit expected plan
- Determinism: byte-identical plans; goal order irrelevant; three fresh
  registries agree
- Planner calls no model, imports no engine, executes nothing, computes no
  domain value, writes no authoritative result
- Plan→executor integration: generated, validated, executed in order, recorded,
  authoritative results typed and intact
- No specialist agent can reach the planner
- Frozen areas byte-identical: `optimization/`, `resilience/`,
  `orchestrator/risk/`, `orchestrator/governance/`, `forecasting/`,
  `ingestion/`, `orchestrator/twin/`, `orchestrator/agents/`,
  `orchestrator/conversation/`, `app/`
- No retries, rerouting, escalation, circuit breakers, concurrency, Agents SDK
  or Agno

### DEFERRED

| Item | Why |
|---|---|
| Retry / reroute / escalate on `BLOCKED_BY_FAILURE` | Phase 8.4 |
| Parallel execution | `execution_layers()` exists; ordering stays sequential |
| Autonomous replanning | §14 forbids |
| LLM planner | §8 forbids; design sketched in the architecture doc §13 |
| Domain-level dependencies | would close the `wf_scenario_analysis` gap |
| Typed context fields for `kpi.summarise`, `scenario.validate` | carried from Phase 8.2 |
| Replacing templates with derived plans | §10 — the exclusions are not recoverable |
| Entity-name grounding | open since Phase 8.0.1; prerequisite for an LLM planner |

---

## 13. Limitations

1. **Derivation does not reproduce `wf_scenario_analysis`'s ordering.** Explained
   in §6 and pinned. Scenario workflows must keep using templates.

2. **Templates are not context-pruned.** A derived plan skips a satisfied
   capability; a template does not. Deliberate — a template's shape is a design —
   but it means the two paths treat the context differently, and only the derived
   path realises the §7 saving.

3. **Goal sets are the caller's responsibility.** For a known intent the template
   supplies them; for a derived plan the caller names them. Nothing yet maps a
   natural-language request to a goal set without a template — that is what an
   LLM planner would do, and it is out of scope.

4. **The validator checks structure, not suitability.** It will accept a valid
   plan that is a poor answer to the question. A validator that second-guessed
   the planner would become a second planner.

5. **`BLOCKED_BY_FAILURE` only fires on derived plans.** A template plan whose
   prerequisite failed in an *earlier* execution is not blocked at plan time; the
   executor refuses the step instead. Both are safe; they are not the same code.

6. **Two capabilities' outputs still cannot be type-checked** — `kpi.summarise`
   and `scenario.validate` hold no typed context field. Carried over from
   Phase 8.2.

7. **The twin deviates from §4's example** — see §5. Reported rather than
   silently reconciled.

---

## 14. Acceptance criteria

| Criterion | Status |
|---|---|
| Deterministic planner exists / existing extended | ✅ extended |
| Produces typed `ExecutionPlan` | ✅ |
| Uses existing `CapabilityRegistry` | ✅ |
| Uses existing dependency metadata | ✅ contracts only |
| Executable vs plannable explicit | ✅ `planner_selectable`, validated |
| Validates plans before execution | ✅ on **both** paths |
| Dependency cycles detected | ✅ |
| Missing HARD dependencies detected | ✅ |
| Already-completed capabilities recognized | ✅ derived plans |
| Planner does not execute capabilities | ✅ AST |
| Executor remains the sole seam | ✅ untouched |
| Planner calls no LLM | ✅ AST |
| Planner is deterministic | ✅ incl. across fresh registries |
| No retries / rerouting / escalation | ✅ |
| No parallel execution | ✅ single deterministic order |
| No Agents SDK / Agno | ✅ |
| No frontend changes | ✅ `app/` byte-identical |
| Deterministic authority intact | ✅ |
| Full regression passes | ✅ 2,316 passed · 4 skipped · 0 failed |
| Documentation produced | ✅ |
| Validation report produced | ✅ |
| No Git/GitHub operations | ✅ none performed |

---

## 15. Recommendation for Phase 8.4

Phase 8.4 is the failure-management layer, and this phase has left it a clean
place to sit.

1. **Read `PlanRefused.reasons` and `AgentResult.status`.** Both are typed and
   both are already correct. `BLOCKED_BY_FAILURE`, `RETRYABLE_FAILURE` and
   `INSUFFICIENT_EVIDENCE` call for three different responses, and nothing acts
   on any of them yet.

2. **Put the policy ABOVE the executor, not inside it.** The executor is a
   single-shot seam by design, and keeping it that way is what makes retry
   observable. A `FailurePolicy` that decides retry / reroute / escalate and then
   calls `executor.execute` again is testable in isolation; a retry loop inside
   the executor is not.

3. **Bound everything, and record the bound.** Max attempts per capability, max
   total attempts per run, and a circuit breaker keyed on capability. Every
   decision should land in `ExecutionContext` so a trace explains why something
   was attempted three times.

4. **Reroute means "another provider of the same domain."** `resolve(domain)`
   already returns them, and `OPTIMIZATION` already has two. Rerouting to a
   different *domain* would be answering a different question.

5. **Escalation is governance's existing job.** `HUMAN_ONLY` and
   `REQUIRES_APPROVAL` already exist; escalation should route into them rather
   than invent a parallel path.

Two items should be closed before an LLM planner arrives in 8.5+: **domain-level
dependencies** (closing the `wf_scenario_analysis` gap) and **entity-name
grounding**, still open from Phase 8.0.1.

Stopped here. Phase 8.4 not begun.
