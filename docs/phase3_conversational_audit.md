# Phase 3 — Pre-Implementation Audit

Produced before any code change, per Phase 3 §1.

## Headline finding

**There is no existing chatbot, conversation, NLP-session or forecasting code.**
`grep` for `chat|conversation|session_id|Chatbot|Forecast` across `netgravity/`
returns nothing. So Phase 3 adds a genuinely new layer rather than replacing one,
and there is no duplicate intent-routing to reconcile.

The frontend `app/frontend/js/agent.js` renders a *simulated* agent trace from
static data (`AGENT_STATE` in `data.js`). It calls no backend and contains no
intent logic. It is a mock-up, not an implementation.

**Second finding, load-bearing:** the structured-intent path Phase 3 asks for
**already exists** and is already used by every Phase 2 test —
`OrchestratorRequest.explicit_intent` + `explicit_scenarios` + `external_signal`.
That is precisely "structured intent in, orchestrator decides". Phase 3's NLP
layer should therefore *produce* those fields, not invent a new entry point.
This is the single most important integration decision in the audit: it means
the LLM cannot select a workflow even by accident, because workflow selection
happens in `WorkflowPlanner` from the `Intent` enum, downstream of everything the
LLM touches.

---

## Audit table

| Existing capability | Current implementation | Phase 3 gap | Proposed integration point |
|---|---|---|---|
| Orchestrator control plane | `core/orchestrator.py` — full lifecycle, dependency-aware layer execution | None. Reused unchanged. | Chat layer calls `orchestrator.run()` with a translated request |
| Workflow planner | `core/planner.py`, `WORKFLOW_TEMPLATES` keyed by `Intent` | Needs entries for new intents only | Add templates; core untouched |
| Intent schema | `schemas/requests.py::Intent`, `IntentResolution`, `ScenarioIntentSpec` | No conversation id, no ambiguity state, no entity-resolution result, no schema/prompt version | New `schemas/conversation.py`; extend `Intent` enum only |
| Intent agent (NLP) | `agents/intent_agent.py` — rules tier + LLM tier, output filtered against real facility ids | No ambiguity detection, no clarification, no multi-match entity resolution, no conversation context | Wrap, don't replace. New `ConversationalNLU` delegates to it |
| Entity resolution | `IntentAgent._match_facilities` — token match against real ids, cannot invent | Returns a flat list; no "2 matches → ambiguous" signal, no name/alias matching | New `EntityResolver` reading the live snapshot |
| Scenario planner | `engines/scenario_builder.py` + `ScenarioValidator` | None. Already handles absolute + relative capacity, closure, open, shift | Reused verbatim |
| Extraction / parsing | `IntentAgent._parse_capacity_change`, `_PROBABILITY_PATTERNS` in signal agent | None for scenarios. Probability extraction is deterministic and correct | Reused verbatim |
| MILP | `optimization/milp.py` via `OptimizationClient` | None | Reused verbatim |
| REI service | `resilience/service.py` — cache keyed on material fingerprint | None | Reused verbatim; cache is what makes §24 achievable |
| RF | `orchestrator/risk/` | None | Reused verbatim |
| Reasoning agent | `agents/reasoning_agent.py` | None | Reused verbatim |
| Numeric grounding | `validation/numeric_grounding.py` | None. **Do not build a second one.** | Reused verbatim |
| Governance | `governance/action_classifier.py` (R7 finalised) | None | Reused verbatim; chat never bypasses it |
| LLM gateway | `agents/llm_gateway.py` — HTTP, retry, backoff, budget cap, `available` degradation | Not provider-abstract; no structured-output helper; no model/prompt version metadata | Add a narrow `LLMClient` Protocol that the existing gateway satisfies. Do not rewrite it |
| Execution context | `core/execution_context.py` | No conversation id | Add one field |
| Audit | `audit/audit_logger.py`, `audit/events.py` | Missing the 8 chat events | Add constants + emit. Do not build a second trail |
| Digital twin | `SnapshotManager` (observed) + `ScenarioStore` (hypothetical) | None | Read-only source for entity resolution and status answers |
| Forecasting agent | **Does not exist** | §5.F asks the orchestrator to route to it | See below |

---

## Two honest gaps

### 1. There is no Forecasting Agent

§5.F says "Orchestrator routes to the Forecasting Agent". No such component
exists — no demand model, no projection engine, nothing. Building one means
inventing a forecasting methodology, which is a substantial new *deterministic*
capability, not a conversational layer, and §25 explicitly rules out expanding
scope.

**Decision:** recognise `FORECAST` as an intent, and have the orchestrator
answer honestly that no forecasting capability is registered. That is the
correct behaviour under the existing capability-registry design, and it means a
future phase can register a forecasting capability without touching the NLP
layer at all. Fabricating numbers would violate the phase's own central rule.

### 2. `INFORMATION` queries and the "no unnecessary MILP" requirement

§5.A wants "How many warehouses do we have?" answered from the digital twin
without a solve. Every current workflow that reports network state runs
`optimization.solve`, because cost and utilisation *require* it.

**Decision:** a new `wf_information` template that reads the snapshot only — no
solver step, structurally. Counts and inventory-of-facilities questions need no
optimization; questions that genuinely need cost route to `NETWORK_STATE_QUERY`
instead. The NLU distinguishes them.

---

## Duplicate-logic risks identified, and how each is avoided

| Risk | Avoidance |
|---|---|
| A second router inside the LLM layer | NLU emits an `Intent` enum value; `WorkflowPlanner` alone maps intent → workflow. Asserted by test |
| A second grounding system | Chat renders `ReasoningResult` that has *already* been grounded. No new numeric checking |
| A second intent parser | `ConversationalNLU` delegates to the existing `IntentAgent` for the rules+LLM tiers, then adds ambiguity/entity resolution around it |
| A second audit trail | Chat events go into the existing `ExecutionTrace` via `events.py` constants |
| A second scenario path | Chat produces `ScenarioIntentSpec`; the existing validator and builder do the rest |

---

## Planned changes

New (5 modules): `schemas/conversation.py`, `conversation/entity_resolver.py`,
`conversation/nlu.py`, `conversation/chat_service.py`, `conversation/store.py`.

Modified (6, all additive): `schemas/requests.py` (2 intents),
`core/planner.py` (2 templates), `core/execution_context.py` (`conversation_id`),
`audit/events.py` (8 constants), `agents/llm_gateway.py` (`LLMClient` Protocol +
version metadata), `core/orchestrator.py` (thread `conversation_id`).

Nothing in MILP, REI, RF, grounding or governance is touched.
