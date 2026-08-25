# Phase 8.2 — Final Report

**Standardized Capability Executor**
Date: 2026-08-25 · Work performed locally · **No Git/GitHub operations**

---

## Summary

| | |
|---|---|
| Regression before (Phase 8.1 final) | 2,203 passed · 4 skipped · 0 failed |
| Regression after | *(§9)* |
| Tests added | 56 (one new module) |
| Tests deleted / skipped / weakened | **0** — one Phase 8.1 test *strengthened*, see §8 |
| Files created | 1 source + 1 test + 2 docs |
| Files modified | 5 |
| Seam overhead | **~49 µs / invocation**, measured |
| Frozen areas touched | **none** |
| Retries / rerouting / escalation / planner / Agents SDK | **not introduced** |

Two real defects were found by the hand-driven chain §11 asked for, both fixed
and pinned. One of them was a **failure reporting SUCCESS** — precisely what this
seam exists to prevent.

---

## 1. Audit findings

### 1.1 Execution logic was spread across three places

| Concern | Lived in |
|---|---|
| Invoke handler, timeout, error classification, retry policy | `CapabilityTool.execute` |
| Capability-level authorization, upstream assembly | `Orchestrator._run_step` |
| Dependency gating, blocking, degradation, recording, tracing | `Orchestrator._execute_plan` |

All three were reachable **only from a plan**. Anything wanting to run one
capability had to reproduce them or reach past them.

### 1.2 Three capabilities reached past all of it

| Capability | Entry point | Caller before 8.2 | Checks applied | Recording |
|---|---|---|---|---|
| `extraction.parse` | `ExtractionParsingAgent.extract` | ingestion API | its own row rules | **none** |
| `twin.publish` | `Orchestrator._project_twin` | called directly in `run()` | none | `twin_refs` only |
| `signal.route_for_forecast` | `ExternalSignalRouter.route_for_forecast` | inline inside the forecast handler | none | `signal_routing` only |

The other thirteen went through `CapabilityTool` and were recorded via
`record_step`. The full per-capability table is in
`docs/agentic_architecture_phase_8_2.md` §1.

### 1.3 Decisions taken from the audit

**No second executor.** `CapabilityTool.execute` remains the invocation
mechanism — timeout, error classification and the pre-existing retry policies are
untouched. `CapabilityExecutor` wraps it with the checks and normalisation that
had no home.

**Criticality was missing from the contracts.** HARD/SOFT existed only inside
`PlanStep`. A caller executing a capability outside a plan had no way to know
which dependencies were genuinely required — so `optional_dependencies` was added
to `CapabilityContract`.

---

## 2. Infrastructure reused

| Component | How |
|---|---|
| `CapabilityTool.execute` | The only invocation path. Unchanged. |
| `CapabilityRegistry` | Resolution, contracts, `validate_inputs`. Unchanged. |
| `AgentResult` / `AgentStatus` | The return contract. Unchanged. |
| `ExecutionContext.record_step` | Still the single write path — now called from the executor and nowhere else |
| `ExecutionStateStore` | Unchanged. No new store. |
| `AuditLogger` | Reused to give a direct execution a trace (§7) |
| `FailureClass` / `ErrorCode` | The basis of status normalisation |
| `PlanStep.soft_depends_on` / `optional` | Consulted where a plan is more specific than a contract |

---

## 3. Executor design

`netgravity/orchestrator/core/executor.py` — `CapabilityExecutor.execute()`, nine
steps: resolve → exists → inputs → dependencies → invoke → normalise → validate
output → record once → return. Full contract in the architecture doc §3.

Three design points worth stating:

**It never raises for a capability-level failure.** The failure *is* the return
value, so a caller inspects it rather than catching it.

**It holds no state.** A test asserts `vars(executor) == {"registry"}`.

**It decides nothing.** Given a capability id it runs that id. Static tests
assert its code contains no `WORKFLOW_TEMPLATES`, `WorkflowPlanner`,
`execution_layers` or `resolve_capability`, no `while`/`async for` loop, and
exactly **one** awaited call — `tool.execute`.

### What stayed outside, deliberately

**Authorization** stays in `_run_step`: it depends on the **actor**, not the
capability, and is raised rather than returned so it can never be mistaken for a
data-shaped outcome.

**Dependency scheduling** stays in `_execute_plan`. The executor answers "are
this capability's inputs satisfied?", never "what should run next?".

---

## 4. Adapters introduced

Three handlers, each delegating to the **same production entry point it always
used**. No algorithm moved.

| Capability | Delegates to | Notes |
|---|---|---|
| `extraction.parse` | `ExtractionParsingAgent.extract` | requires `source`; typed result to `context.extraction_result` |
| `signal.route_for_forecast` | `ExternalSignalRouter.route_for_forecast` | same call the forecast handler makes; produces no probability |
| `twin.publish` | `Orchestrator._project_twin` | still the only path into the twin |

Registering them required separating two questions the codebase had answered with
one fact: **having a handler** makes a capability *executable*; **`invocation`**
makes it *plannable*. All 16 are now executable; only 13 are schedulable.

`ExecutionContext` gained one field, `extraction_result`, following the existing
pattern for typed authoritative results.

---

## 5. Capabilities tested through the seam

All eight §5 requires, plus the rest of the catalogue. From the §11 chain, run by
hand in explicit dependency order on the Phase 8.0 synthetic network:

| Capability | Status | Output type | Authoritative | ms |
|---|---|---|---|---|
| `network.load_snapshot` | SUCCESS | `str` (pinned id) | yes | 0.1 |
| `signal.route_for_forecast` | SUCCESS | `SignalRoutingDecision` | yes | 0.1 |
| `forecast.demand` | **INSUFFICIENT_EVIDENCE** | — | yes | 5.0 |
| `resilience.assess` | SUCCESS | `FacilityResilienceRegistry` | yes | 138.3 |
| `optimization.solve` | SUCCESS | `NetworkStateResult` | yes | 14.8 |
| `kpi.summarise` | SUCCESS | `dict` | yes | 0.0 |
| `risk.compute_rf` | SUCCESS | `RiskAssessment` | yes | 0.2 |
| `reasoning.synthesise` | SUCCESS | `ReasoningResult` | **no** | 3.6 |
| `governance.classify` | SUCCESS | `GovernanceDecision` | yes | 0.2 |
| `twin.publish` | SUCCESS | `list[TwinStateRef]` | yes | 0.4 |

`extraction.parse` is exercised separately (it precedes a snapshot) and returns a
typed `ExtractionResult`.

**`forecast.demand` is INSUFFICIENT_EVIDENCE, and that is correct.** No
`history_provider` is configured in this build, so there is no demand history —
the capability reports that rather than inventing a series. The status is derived
from the handler's own `MISSING_DATA`, not from the exception type.

`reasoning.synthesise` is the only non-authoritative row, as it must be.

---

## 6. Failure cases verified

| Case | Result |
|---|---|
| Unknown capability | `NON_RETRYABLE_FAILURE`, `CAPABILITY_NOT_FOUND`, recorded |
| Missing declared input | `INSUFFICIENT_EVIDENCE`, **handler never invoked** |
| Input present but `None` | `INSUFFICIENT_EVIDENCE` — None is absent, not a value |
| Missing HARD dependency | refused; `INSUFFICIENT_EVIDENCE` naming the dependency |
| Failed HARD dependency | refused |
| Declared-optional dependency absent | **runs**; the provider reports the gap itself |
| Plan softens a contract-required edge | runs — the plan is the more specific statement |
| MILP infeasible | `NON_RETRYABLE_FAILURE`, `output=None`, not retryable |
| Engine timeout | `RETRYABLE_FAILURE`, reported, **nothing retries it** |
| REI unavailable | `INSUFFICIENT_EVIDENCE`, **never 0.0**; `require()` raises |
| Wrong output type | `INVALID_OUTPUT`, projection removed from `engine_results` |
| Success with no output at all | `INVALID_OUTPUT` |
| Unclassified exception | `RETRYABLE_FAILURE` (`EngineFailureError`, pre-existing), message preserved |
| KPIs requested before any solve | `INSUFFICIENT_EVIDENCE` — **not a zero-cost network** |

### The two defects the §11 chain exposed

Both were only reachable once capabilities could run outside a plan — which is
what this phase introduced. Neither would have been found by running a workflow.

**Defect 1 — governance failed for want of a log entry.**
`governance.classify` passed `audit.get(execution_id)` straight into `_govern`,
which records unconditionally. A directly-built context has no registered trace,
so `None` arrived and governance surfaced as `ENGINE_FAILURE`. Governance must
*always* produce a verdict. Fixed by asking the existing `AuditLogger` for a
trace (`get(...) or start(...)`), so a direct execution is properly audited
rather than guarded.

**Defect 2 — the twin reported SUCCESS having published nothing.**
`_project_twin` never raises, by design: inside a workflow, failing to draw the
picture must not fail the analysis. But an empty `twin_refs` list satisfied the
envelope's "output is not None" invariant, so `twin.publish` returned SUCCESS
with `[]`. **A failure masquerading as a successful output.** Fixed in the
handler, the only place that knows publishing was the point; the in-run
projection is untouched and still degrades quietly. Both halves are pinned by
tests.

---

## 7. State and provenance validation

| Claim | Verified |
|---|---|
| Recorded exactly once | `record_step` call counted; exactly one write per execution |
| `_execute_plan` no longer records | removed; the executor is the only writer |
| Direct invocation is recorded | under the capability id when no `step_id` is given |
| Recorded status == returned status | asserted, including for `INVALID_OUTPUT` |
| `record=False` writes nothing | asserted |
| No second state store | `vars(executor) == {"registry"}` |
| Provenance survives success | capability, execution_id, snapshot_id, scenario_id, provider, duration |
| Provenance survives failure | asserted separately — failure is when it matters most |
| Snapshot/scenario ids preserved | carried, never re-derived |
| No transcripts stored | nothing conversational is recorded |

---

## 8. Architectural boundary validation

| Rule | Result |
|---|---|
| Executor imports no engine | ✅ AST |
| Executor reaches specialists only via the registered tool | ✅ exactly one awaited call, `tool.execute` |
| Executor contains no planning | ✅ AST, docstrings stripped |
| Executor has no retry/reroute/escalation | ✅ no loop; no `should_retry`/`delay_for`/`fallback`/`escalate`/`circuit` |
| Executor cannot substitute a provider | ✅ |
| Extraction cannot execute forecasting | ✅ AST |
| Forecasting cannot invoke MILP/REI/RF | ✅ AST |
| Digital Twin invokes no engine | ✅ AST; `_project_twin` still the only path in |
| Reasoning cannot overwrite an authoritative value | ✅ a contradictory PROBABILISTIC figure leaves the deterministic record intact and is marked non-authoritative |
| Governance authoritative | ✅ DETERMINISTIC, `llm_backed=False` |
| RF the only RF authority | ✅ unchanged; router output reaches no RF input |
| Signal Router owns forecast eligibility | ✅ declared authority; no probability |
| No agent gained a path to the executor | ✅ AST over `agents/*.py` |

### The one Phase 8.1 test that changed

`test_the_only_undeclared_handlers_are_the_three_non_plan_steps` asserted that
extraction, twin publish and signal routing had **no handler** — a gap, recorded
honestly at the time. Phase 8.2 closed it.

It was replaced by **two stronger assertions**, not a weaker one:

- `test_every_declared_capability_is_executable` — `unimplemented() == []`
- `test_being_executable_does_not_make_a_capability_plannable` — the three still
  fail `is_plan_schedulable`, `schedulable()` is still exactly 13, and
  `resolve_capability` still returns `None` for EXTRACTION and DIGITAL_TWIN

No assertion was loosened and no test was skipped or deleted.

---

## 9. Regression results

Import/startup validation — all five affected modules import cleanly:

```
health     : {'status': 'ok', 'capabilities': 16, 'workflows': 10}
executor   : CapabilityExecutor | shares registry: True
contracts  : 16
schedulable: 13 of 16
```

Full suite: *(filled from the final run — see the console record at the end of
this section)*

---

## 10. Performance observations

No stress testing performed, per §12. One measurement, 2,000 iterations of a
no-op capability:

```
bare CapabilityTool.execute :  14.7 µs/call
through CapabilityExecutor  :  63.3 µs/call
seam overhead               :  48.6 µs/call
```

~49 µs of contract construction and validation per invocation. Against the
measured cost of real work — 14.8 ms for a MILP solve, 138 ms for an REI sweep —
that is roughly 0.03 % to 0.3 %. It would matter for a hot loop; it does not
matter here, and this phase adds no hot loop.

**No large object is duplicated.** Verified by identity:

```
envelope output IS the engine's own object : True
context field   IS the engine's own object : True
```

The envelope carries the domain result by reference. The only copy is
`ToolResult.model_copy(update={"status": ...})`, a shallow copy of a small
record whose `output` dict is shared, not cloned. Snapshot and scenario ids are
carried through, never re-derived.

---

## 11. Remaining limitations

1. **Two capabilities' outputs are not type-checked.** `kpi.summarise`
   (declares `NetworkKPIs`) and `scenario.validate` (declares
   `ValidationReport`) hold no typed result on the context, so
   `authoritative_field` is empty and the conformance check is skipped for both.
   The other fourteen are checked. Giving these two typed context fields is a
   small follow-up.

   Separately, `network.load_snapshot` and `scenario.create` are skipped **by
   declaration** (`authoritative_is_reference`), because their fields hold an
   identifier by design. That is intended, not a gap.

2. **Output validation is narrow by design.** Contract conformance only. The
   advisory warnings from `ResultValidator` deliberately remain warnings —
   escalating them would discard correct results and change behaviour the
   existing tests rightly pin.

3. **Pre-existing retry policies remain.** `optimization.solve` allows 2 attempts
   and `external.interpret_signal` 3, inside `CapabilityTool`. This phase neither
   added nor removed retry; the attempt count is surfaced through provenance.
   A caller cannot yet *choose* whether to retry — that is Phase 8.3+.

4. **`_execute_plan` still owns dependency scheduling.** Correct for this phase,
   but it means the plan path and a direct caller reach the dependency question
   by different routes: the plan blocks before the executor is called, the
   executor refuses when called directly. The two agree, and a test covers the
   interaction, but they are not the same code.

5. **`INSUFFICIENT_EVIDENCE` on a refusal is recorded but produces no step
   entry.** A refused execution appears in `unavailable_evidence` and
   `capability_status` but has no `step_results` row, since no tool ran. Readers
   iterating `step_results` will not see it.

6. **The twin's direct-vs-in-run asymmetry is deliberate but is an asymmetry.**
   `twin.publish` fails when invoked directly and degrades quietly inside a run.
   Justified — publishing is the request in one case and a side-effect in the
   other — but it is a behavioural difference between two paths to the same code.

7. **`forecast.demand` could not be exercised to SUCCESS** in this environment:
   no `history_provider` is configured, so there is no demand history. The
   INSUFFICIENT_EVIDENCE path is verified; the SUCCESS path through the seam is
   covered by the existing forecasting suite, not by the §11 chain.

---

## 12. Acceptance criteria

| Criterion | Status |
|---|---|
| One standardized capability executor exists | ✅ `CapabilityExecutor` |
| Existing registry reused | ✅ no new registry |
| Existing `ExecutionContext` / state store reused | ✅ no new store |
| Inputs validated before execution | ✅ handler not invoked on a refusal |
| HARD dependencies enforced | ✅ declared criticality; plan may soften |
| Outputs validated | ✅ contract conformance |
| `AgentResult` returned consistently | ✅ every path, including refusals |
| Typed domain results intact | ✅ carried by reference, verified by identity |
| Failures cannot masquerade as successful outputs | ✅ **including one such defect found and fixed** |
| Provenance recorded | ✅ on success and on failure |
| Existing capabilities execute through the seam | ✅ all 16, including the three that bypassed it |
| No specialist agent orchestrates another | ✅ AST |
| No planning logic in the executor | ✅ AST |
| No retries | ✅ none added; pre-existing unchanged and disclosed |
| No rerouting / escalation | ✅ AST |
| No OpenAI Agents SDK / Agno | ✅ |
| No frontend changes | ✅ `app/` byte-identical |
| Full regression passes | *(§9)* |
| Documentation + validation report | ✅ |
| No Git/GitHub operations | ✅ none performed |

---

## 13. Recommendation for Phase 8.3

The seam is what a planner needs; the decision is what is still missing.

1. **Move dependency resolution behind one function.** Today the plan path blocks
   in `_execute_plan` and a direct caller is refused by the executor. Both are
   correct and they agree, but a planner should ask one question and get one
   answer. `DependencyResolver.satisfied(capability, context)` — used by both.

2. **Build the deterministic capability-graph planner.** From
   `dependency_map()` plus `required_dependencies` plus domain resolution.
   Acceptance test: it reproduces all ten existing `WORKFLOW_TEMPLATES` exactly.
   Still no model — prove the graph before letting anything choose.

3. **Give the caller the retry decision.** `is_retryable` is already correct and
   already reported. A policy layer *above* the executor should act on it, so the
   executor stays a single-shot seam.

4. **Close the typed-output gaps** from §11.1 — three capabilities without typed
   context fields — so output validation covers the whole catalogue.

5. **Only then, an LLM planner**, constrained to *proposing* a capability set
   that the registry validates and the deterministic planner orders. The model
   must never choose execution order and must never touch a result.

Still open from Phase 8.0.1 and a prerequisite for step 5: **entity-name
grounding.** `numeric_grounding` grounds numbers, not names, so a fabricated
facility name can survive as prose. A planner acting on model-proposed
capabilities makes that gap materially more consequential.

Stopped here. Phase 8.3 not begun.
