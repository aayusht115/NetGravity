# Phase 8.1 — Final Report

**Agent Contract + Capability Registry**
Date: 2026-08-25 · Work performed locally · **No Git/GitHub operations**

---

## Summary

| | |
|---|---|
| Regression before | 2,116 passed · 4 skipped · 0 failed |
| Regression after | **2,203 passed · 4 skipped · 0 failed** |
| Tests added | 87 (one new module) |
| Tests deleted / skipped / weakened / inverted | **0** |
| Files modified | 7 |
| Files created | 5 source/test + 2 docs |
| Lines | +612 / −3 in modified files |
| Frozen areas touched | **none** |
| Frontend changes | **none** |
| LLM planner / Agents SDK / retry logic | **not introduced** |

The audit changed the shape of this phase. The brief reads as though the
capability layer needed building; most of it already existed, and was better than
a parallel design would have been. So the work became: **adopt what exists, close
the four specific gaps that stopped it being plannable, and prove nothing
changed.**

---

## 1. Audit findings

### 1.1 What already existed

| Brief asks for | Already present | Decision |
|---|---|---|
| `AgentResult` contract | `ToolResult` — capability, success, output, error_code, failure_class, duration, attempts, execution_mode, metadata | **Extend** (one optional field) |
| Capability model | `Capability` — name, handler, execution_mode, dependencies, timeout, retry_policy, optional, required_roles | **Extend** (one optional field) |
| Capability registry | `CapabilityRegistry` — register / get / has / names / describe / validate_plan_capabilities, duplicate prevention | **Extend**, not replace |
| Execution state | `ExecutionContext` — 40+ typed fields, state machine, step bookkeeping, unavailable evidence, typed authoritative results | **Extend**, not replace |
| Dependencies | `Capability.dependencies`, `PlanStep.depends_on` + `soft_depends_on`, `DependencyType.HARD/SOFT`, `classify_dependencies`, `execution_layers` | **Reuse unchanged** |
| Status distinctions | `FailureClass` (RETRYABLE / NON_RETRYABLE / REQUIRES_HUMAN), `ErrorCode` (15), `EvidenceStatus` (4) | **Reuse as the derivation basis** |
| Run storage | `ExecutionStateStore` (by execution_id, by request_id, idempotent) | **Reuse — no new store** |

Thirteen capabilities were registered with handlers, in a dotted namespace. The
executor already enforced timeouts, bounded retries, error classification, and
HARD/SOFT dependency criticality with explicit `unavailable_evidence`.

### 1.2 The four gaps

**Gap 1 — outcome was a boolean.** `ToolResult.success` could not distinguish
proven infeasibility, a timeout, a validation rejection, and absent inputs. All
four arrived as `success=False`. The information existed but was spread across
three vocabularies, and no consumer could branch on it uniformly.

**Gap 2 — no recorded mapping to the typed result.** `ToolResult.output` is a
flattened dict for transport. The authoritative typed results lived in dedicated
context fields (`rei_registry`, `forecast_result`, `network_states`,
`risk_results`, `reasoning`, `governance_result`) — correct and load-bearing, but
a caller had to *know* which field went with which capability, and nothing wrote
that down.

**Gap 3 — `Capability.input_schema` / `output_schema` were dead.** Both declared,
neither populated anywhere in the codebase. Verified by grep across `netgravity/`.

**Gap 4 — three real capabilities were invisible.** Extraction, the Digital Twin
projection and forecast signal routing all have real providers and real typed
results. None appeared in the registry, because none is a plan step.

### 1.3 Which concepts to extend rather than duplicate

- `ToolResult` stays the **executor's** contract. `AgentResult` is the
  **orchestration-facing** view over it. Converting handlers would have changed
  execution behaviour in a phase required not to.
- `EvidenceStatus` / `UnavailableEvidence` already expressed "insufficient
  evidence"; `FailureClass` already expressed retryability. `AgentStatus`
  **unifies** these rather than inventing a third vocabulary.
- `ExecutionStateStore` already holds runs by id — `get_execution_state` delegates
  to it.

### 1.4 Conversation/NLU — excluded, with reason

§3 says include it "only if it is actually an executable capability". It is not.
`ChatService` runs `ConversationalNLU` to determine *which* capabilities a turn
needs, before any execution exists. It is the **caller** of the capability layer.
Registering it would make understanding a request a step inside executing one.

A test asserts no declared capability mentions NLU or conversation, so the
omission stays a decision.

---

## 2. Existing components reused

| Component | Reused how |
|---|---|
| `ToolResult` | Kept as-is; `AgentResult.from_tool_result` is a view over it |
| `Capability` | Kept as-is; gained one optional `contract` field |
| `CapabilityRegistry` | Extended in place with a second, separate store |
| `ExecutionContext` | Extended with one field; everything else derived |
| `ExecutionStateStore` | `get_execution_state` delegates — no new store |
| `FailureClass`, `ErrorCode` | The basis of RETRYABLE / NON_RETRYABLE / INVALID / INSUFFICIENT |
| `EvidenceStatus`, `UnavailableEvidence` | Carried through the envelope unchanged |
| `DependencyType.HARD/SOFT` | Untouched; PARTIAL derives from a degraded SOFT edge |
| `ExecutionState` state machine | Untouched |

---

## 3. New components

| File | Lines | What |
|---|---|---|
| `orchestrator/schemas/agent_result.py` | ~380 | `AgentResult[T]`, `AgentError`, `ResultProvenance`, `classify` |
| `orchestrator/schemas/capability.py` | ~230 | `CapabilityContract`, `CapabilityDomain`, `InvocationMode`, `CapabilitySummary` |
| `orchestrator/routing/capability_contracts.py` | ~330 | The 16-capability catalogue + import-time coherence check |
| `orchestrator/tools/adapters.py` | ~260 | Thin adapters for the three non-plan-step capabilities |
| `tests/test_agent_contract.py` | ~880 | 87 tests |
| `docs/agentic_architecture_phase_8_1.md` | — | Architecture document (§13) |
| `validation/phase_8_1/capability_harness_rerun.txt` | — | Phase 8.0 harness re-run evidence |

### Files modified

| File | Δ | Change |
|---|---|---|
| `schemas/plans.py` | +54 | `AgentStatus` enum; optional `ToolResult.status` |
| `core/execution_context.py` | +226 | `capability_status` field; 7 derived accessors |
| `routing/capability_registry.py` | +188 | Contract store + 14 metadata methods |
| `core/orchestrator.py` | +91/−1 | 7 read-only control-plane primitives |
| `core/planner.py` | +18 | 3 capability-name constants (not scheduled) |
| `registry.py` | +25/−2 | Contract wiring at build time |
| `tools/base.py` | +13 | Optional `Capability.contract` |

**The 3 deletions are the exact lines replaced** — one import line, and the two
brackets of `register_all([...])` turned into a named list. Nothing was removed.

---

## 4. Architectural decisions

**1. `AgentResult` wraps; it does not replace.** Handlers still return
`ToolResult`. `AgentResult.from_tool_result` derives the status from evidence
already recorded. This is why introducing the contract changed no behaviour — a
test asserts building envelopes for every capability leaves `step_results`
byte-identical.

**2. A failing status cannot hold output.** Enforced by a `model_validator`, not
by convention. `result.output or 0` on an unusable result has nothing to find. An
unexplained failure — no errors, no missing evidence — is also refused.

**3. Contracts and handlers live in separate stores inside one registry.** A
contract can exist with no entry in `_tools`, so a metadata lookup has no handler
to call *even by accident*. That is the mechanism behind "the registry must not
execute capabilities". A test registers a handler that raises and then calls every
metadata method.

**4. Invocation mode records the system as built.** Three capabilities are real
and are not plan steps. Marking them SERVICE / EMBEDDED — and defaulting
`resolve_capability` to `schedulable_only=True` — means a planner can see they
exist and cannot be handed one to schedule.

**5. `SIGNAL_INTERPRETATION` and `SIGNAL_ROUTING` are separate domains.** One
derives the probability feeding `RF = P + REI − P·REI`; the other decides forecast
relevance and never touches RF. A single "signals" domain would invite exactly the
substitution the risk chain prevents. Tests assert the domains are disjoint and
that RF depends on the first and never the second.

**6. `authoritative_field` is declared once, in the contract.**
`ExecutionContext.typed_output` takes the field name as an argument rather than
keeping its own capability→field map. A second copy is how the two come to
disagree.

**7. `record_unavailable` uses `setdefault`.** `record_step` classifies a failure
and *then* records missing evidence. Assignment would overwrite
`RETRYABLE_FAILURE` with `INSUFFICIENT_EVIDENCE` on every engine fault and destroy
the distinction the record exists to preserve.

**8. Disagreeing steps of one capability fold to PARTIAL.** A comparison run
solves two scenarios through one capability. SUCCESS would hide a failed solve;
failure would discard a good one.

**9. Declined: populating `input_schema` / `output_schema`.** The contract's
`input_type` / `output_type` supersede them. Removing the two dead fields is a
behaviour-neutral cleanup, but it changes the `Capability` API and belongs to a
phase that is allowed to.

---

## 5. Tests added — 87

| Group | N | Covers |
|---|---|---|
| `TestAgentResultStatuses` | 8 | one construction per status; infeasibility non-retryable; MISSING_DATA → insufficient |
| `TestAgentResultInvariants` | 11 | failing status cannot hold output (×4); usable cannot be empty (×2); unexplained failure refused; `require()` raises; reasoning never authoritative; provenance; explicit status wins; `classify` table; generic type preserved |
| `TestCapabilityRegistry` | 14 | registration, lookup, duplicate prevention, idempotent re-declaration, unknown capability, domain resolution, unserved domain → None, SERVICE never schedulable, `validate_inputs`, contract invariants (×3), handler+contract co-registration |
| `TestLiveRegistry` | 9 | no undeclared; unimplemented is exactly the 3; all 8 required domains present; NLU absent; **every declared type name resolves to a real class**; **every authoritative field exists on the context**; declared vs registered dependencies agree; execution modes agree |
| `TestExecutionContextCapabilityState` | 11 | record success/failure, failure class preserved, blocked → insufficient, two-step PARTIAL fold, pending from plan, never-ran → insufficient, typed field not projection, projection fallback, provenance, derived-state consistency |
| `TestSpecialistsAreRepresentable` | 17 | 7 required capabilities declared; **state query needs no forecast/REI/RF**; typed authoritative optimization output; reasoning advisory; extraction via adapter; rejected extraction → INVALID_OUTPUT; review ≠ rejection; twin representable; failed twin offers no numbers; routing yields no probability; **representation mutates nothing** |
| `TestArchitecturalBoundaries` | 12 | registry cannot execute; registry imports no engine; planner never schedules SERVICE/EMBEDDED; all 10 workflows valid; agents reach no engine; **the one agent-to-agent edge pinned**; forecasting isolated; twin isolated; reasoning advisory / governance authoritative; RF vs routing separate; primitives neither plan nor retry; no LLM planner or SDK |
| `TestCatalogueIntegrity` | 5 | declared once; dependencies declared; no cycle; immutable |

**Four of these were my own bugs, found and fixed:** wrong constructor arguments
for `ForecastResult`, `FacilityResilienceRegistry`, `ExtractionResult`
(HUMAN_REVIEW_REQUIRED is rejected by the schema unless it names something to
review — a good constraint I had not honoured) and `TwinStateType.OBSERVED`
(the real members are BASELINE / OPTIMIZED / SCENARIO).

---

## 6. Boundaries verified

| Rule | Result |
|---|---|
| Registry does not execute business logic | ✅ handler raises if called; all metadata methods invoked |
| Registry / contract / schema modules import no engine | ✅ AST |
| Forecasting cannot invoke MILP / REI / RF / orchestrator | ✅ AST over `forecasting/**` |
| Digital Twin invokes no engine independently | ✅ AST over `twin/**` |
| Reasoning cannot modify authoritative results | ✅ PROBABILISTIC ⇒ `is_authoritative` False |
| Governance remains authoritative | ✅ DETERMINISTIC, `llm_backed=False` |
| RF probability separate from signal confidence | ✅ disjoint domains, RF dependencies checked |
| Planner never schedules SERVICE / EMBEDDED | ✅ AST over every `PlanStep(...)` |
| Specialists do not reach an engine or the control plane | ✅ AST over `agents/*.py` |
| Agents do not directly invoke other specialist agents | ⚠️ **one pre-existing exception** |

### The exception, reported not hidden

`ExtractionParsingAgent` calls `ExternalSignalAgent(None).interpret(...)`. A
genuine agent-to-agent call and a real departure from §9. **Pre-existing** — it
predates this phase — and constrained: parsing only, model tier explicitly
disabled (`allow_llm=False`), and it stops at the signal without touching REI or
RF.

I did not weaken the test to pass. It **pins** the exception:

```python
assert edges == {"extraction_agent.py": ["...agents.external_signal_agent"]}
```

A second such edge fails the suite, and the test additionally asserts
`extraction_agent.py` contains neither `assess_network_risk` nor `risk_factor`, so
that edge cannot become a route into the risk chain.

**Deferred fix:** extract the shared text-to-signal parsing into a helper both
agents call. Behaviour-neutral, but it refactors a specialist's internals, which
§7 forbids here.

---

## 7. Validation results

### Import / startup

All 10 affected modules import cleanly. `build_orchestrator(enable_llm=False)`
starts and reports healthy:

```
health     : {'status': 'ok', 'capabilities': 13, 'workflows': 10}
contracts  : 16
undeclared : []
unimplemented : ['extraction.parse', 'signal.route_for_forecast', 'twin.publish']
domains    : 13
```

### Phase 8.0 capability harness re-run (§12)

Evidence: `validation/phase_8_1/capability_harness_rerun.txt`

```
PASS  ingestion                    21/21     PASS  signal_enriched_forecasting  11/11
PASS  extraction                   14/14     PASS  digital_twin                 13/13
PASS  signal_routing               10/10     PASS  snapshot_scenario_isolation   9/9
PASS  milp                         11/11     PASS  provenance                   14/14
PASS  rei                          10/10     PASS  reasoning                     9/9
PASS  rf                           13/13     PASS  nlu_chatbot                  36/36
PASS  governance                    9/9      NOT_TESTED  extraction_llm          0/3
PASS  forecasting                  39/39

checks: 219/222 passed · live model calls: 0/20 (blocked 3)
shared gateway spend: 0.44984745 -> 0.44984745 (unchanged)
```

**14 of 15 sections PASS, identical to Phase 8.0.** The single NOT_TESTED section
is `extraction_llm`, refused by the shared gateway with `daily_limit_exceeded` —
**0 API calls charged, 0 spend**. That is the same external quota condition
recorded in Phase 8.0.1 and has nothing to do with this phase's changes. I am not
claiming the live extraction path was validated here.

The Phase 8.0 artifacts (`metrics/`, `traces/`, `plots/`, `report.md`) were backed
up before the re-run and restored afterwards, so Phase 8.0's evidence is intact.

### End-to-end behaviour check

A real network-state run through the wired orchestrator:

```
state             : COMPLETED
completed steps   : ['load', 'optimize', 'kpi', 'reason', 'govern']
capability_status : all 5 SUCCESS
pending           : []

optimization.solve   → SUCCESS   output=NetworkStateResult   authoritative=True
reasoning.synthesise → SUCCESS   output=ReasoningResult      authoritative=False
resilience.assess    → INSUFFICIENT_EVIDENCE   output=None
```

Three things this demonstrates at once: the typed authoritative object reaches the
envelope; the narrative is carried but marked non-authoritative; and a capability
the workflow correctly did not run reports **absence, not zero**.

### Regression

Baseline recorded before any change: **2,116 passed · 4 skipped · 0 failed**
(266.75s).

Final run after all Phase 8.1 changes:

```
2203 passed, 4 skipped, 576313 warnings in 251.32s (0:04:11)
```

**2,116 → 2,203 is exactly +87**, the count of the new module. No pre-existing
test changed status, and no test was deleted, skipped, weakened or inverted to
accommodate this phase.

---

## 8. Status classification

### IMPLEMENTED

- `AgentResult[T]` with 6 explicit statuses, typed generic output, provenance,
  errors, warnings, unavailable-evidence map
- Three construction invariants making the misread-a-failure shapes unconstructible
- `AgentStatus` on `ToolResult` as an optional override (default `None`, derived)
- `CapabilityContract` / `CapabilityDomain` (13) / `InvocationMode` (3)
- 16-capability catalogue with import-time coherence checking
- `CapabilityRegistry` extended: contract store, domain resolution,
  `validate_inputs`, `dependency_map`, `undeclared`/`unimplemented` self-audit
- `ExecutionContext.capability_status` + 7 derived capability-level accessors
- Thin adapters for extraction, twin publish and signal routing
- 7 read-only control-plane primitives on `Orchestrator`
- 87 tests, architecture document, this report

### VERIFIED

- Regression: no test deleted, skipped, weakened or inverted
- Phase 8.0 harness: 14/15 PASS, 219/222 checks, unchanged from Phase 8.0
- Import/startup clean across all 10 affected modules
- All 8 brief-required capability domains present and resolvable
- Every declared type name resolves to a real class; every `authoritative_field`
  exists on `ExecutionContext`
- Declared dependencies and execution modes agree with the registered ones
- Building envelopes mutates no recorded result
- A state query requires no forecast, REI or RF — no universal workflow
- Registry cannot execute; imports no engine
- Deterministic engines authoritative; reasoning advisory
- RF probability separate from signal-routing confidence
- Frozen areas byte-identical: `optimization/`, `resilience/`, `orchestrator/risk/`,
  `orchestrator/governance/`, `forecasting/`, `ingestion/`, `orchestrator/twin/`,
  `orchestrator/agents/`, `orchestrator/conversation/`, `orchestrator/validation/`, `app/`
- No LLM planner, no Agents SDK, no Agno, no retry/reroute/escalation
- No frontend changes, no Git operations

### DEFERRED

| Item | Why |
|---|---|
| Handlers producing `AgentResult` directly | Would change execution behaviour; §12 forbids |
| Handlers declaring PARTIAL (REI batch, per-series forecast, extraction WARNING) | The field exists; using it is Phase 8.2 |
| Removing dead `Capability.input_schema` / `output_schema` | Changes the `Capability` API |
| Inverting the extraction → signal-agent edge | Refactors a specialist's internals; §7 forbids |
| Capability-graph planner replacing `WORKFLOW_TEMPLATES` | §11 explicitly stops before planning |
| Retry / escalation acting on `is_retryable` | §11 |
| Entity-name grounding | Open from Phase 8.0.1; prerequisite for an LLM planner |
| Live `extraction_llm` validation | External shared quota exhausted; 0 calls charged |
| `openai-agents` SDK runtime | Still uninstalled and untested |

---

## 9. Limitations

1. **The status distinctions are derived, not yet declared.** No handler sets
   `ToolResult.status`, so PARTIAL currently arises only from a degraded SOFT
   dependency. Three capabilities already know they can be partial and do not say
   so. The mechanism is in place and unused.

2. **`AgentResult` is a view, not the execution path.** The executor still
   produces `ToolResult`. That was the point — but it means the contract is not
   yet *enforced* on handlers, only *derivable* from them.

3. **Three capabilities are declared but not executable through the registry.**
   Honest and marked, but a future planner must respect `invocation`. The default
   `schedulable_only=True` makes the safe path the easy one; it does not make the
   unsafe path impossible.

4. **`resolve_capability` returns the first match for a multi-provider domain.**
   OPTIMIZATION has two providers (observed and scenario). Ordering is
   deterministic (sorted by id) but arbitrary as a *choice* — the planner will
   need to select on scenario context, which is Phase 8.2's job.

5. **The extraction → signal-agent edge remains.** Pinned by a test, not removed.

6. **One capability harness section is NOT TESTED** because the external shared
   quota was exhausted. Not a defect in this phase, and not claimed as validated.

---

## 10. Recommended Phase 8.2

In the order that makes it cheapest:

1. **Adopt `AgentResult` at the executor seam** — `CapabilityTool.execute`
   returns it, with `ToolResult` as its serialised projection. One place changes;
   handler signatures do not.
2. **Let the three handlers that know they are partial say so** — REI
   (`REIBatchStatus.PARTIAL`), forecasting (per-series `ForecastStatus`),
   extraction (`WARNING`).
3. **Deterministic capability-graph planner** built from `dependency_map()` and
   domain resolution. Acceptance test: it reproduces all ten existing workflows
   exactly. Still no model.
4. **Retry and escalation** reading `is_retryable` and `FailureClass`. The
   classification is already correct; only the actor is missing.
5. **Then an LLM planner**, constrained to *proposing* a capability set that the
   registry validates and the deterministic planner orders. The model must never
   choose execution order and must never touch a result.

Before step 5, close entity-name grounding (open from Phase 8.0.1):
`numeric_grounding` grounds numbers, not names, so a fabricated facility name can
survive as prose. A planner acting on model-proposed capabilities makes that gap
materially more consequential than it is today.

---

## 11. Acceptance criteria

| Criterion | Status |
|---|---|
| Common `AgentResult` contract exists, or existing equivalent formally adopted | ✅ both — `ToolResult` adopted, `AgentResult[T]` added |
| Capability model exists | ✅ `CapabilityContract`, 13 domains |
| Capability registry exists or existing one formally extended | ✅ extended in place |
| `ExecutionContext` can track capability execution state | ✅ `capability_status` + 7 accessors |
| Dependencies can be represented | ✅ contract `dependencies` + `dependency_map()` |
| Existing specialists remain independently functional | ✅ harness 14/15 PASS, unchanged |
| No specialist agent directly orchestrates another | ⚠️ one pre-existing parsing edge, pinned and reported |
| Deterministic engines remain authoritative | ✅ verified structurally |
| No LLM planner introduced | ✅ asserted by test |
| No OpenAI Agents SDK introduced | ✅ asserted by test |
| No retry/reroute/escalation introduced | ✅ asserted by test |
| Full regression passes | ✅ 2,203 passed · 4 skipped · 0 failed |
| Architecture documented | ✅ `docs/agentic_architecture_phase_8_1.md` |
| No frontend changes | ✅ `app/` byte-identical |
| No Git/GitHub operations | ✅ none performed |

**One criterion is not a clean pass**, and it is marked accordingly rather than
claimed. The agent-to-agent rule has a single pre-existing exception, documented
in §6 with a test that prevents a second one and a concrete deferred fix.

Stopped here. Phase 8.2 not begun.
