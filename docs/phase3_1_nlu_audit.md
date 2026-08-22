# Phase 3.1 — LLM/NLU boundary audit

Written **before** any change, as the phase requires. It records what Phase 3
actually built, so the hardening that follows can be judged against evidence
rather than against an impression.

Companion documents: [`phase3_conversational_audit.md`](phase3_conversational_audit.md),
[`phase3_conversational_layer.md`](phase3_conversational_layer.md),
[`phase3_1_llm_evaluation.md`](phase3_1_llm_evaluation.md) (results).

---

## 1. What is configured

| Component | File | State at audit |
|---|---|---|
| Gateway client | [llm_gateway.py](../netgravity/orchestrator/agents/llm_gateway.py) | Implemented, **no credential configured** |
| Client contract | `LLMClient` Protocol | 3 members: `available`, `generate`, `stats` |
| Intent tier 1 | [intent_agent.py](../netgravity/orchestrator/agents/intent_agent.py) `_rule_based` | Deterministic keyword parser |
| Intent tier 2 | `intent_agent._llm_based` | One inlined prompt, JSON out |
| Conversational NLU | [nlu.py](../netgravity/orchestrator/conversation/nlu.py) | Wraps tier 1+2, adds ambiguity |
| Entity resolution | [entity_resolver.py](../netgravity/orchestrator/conversation/entity_resolver.py) | Token matching against master data |
| Validation | [conversation.py](../netgravity/orchestrator/schemas/conversation.py) | Pydantic, `extra="forbid"` |
| Handoff | [chat_service.py](../netgravity/orchestrator/conversation/chat_service.py) | Builds `OrchestratorRequest` |

`TEXT_API_TOKEN` was unset at audit time, so `LLMGateway.available` returned
`False` and **all 1,126 existing tests exercised the rule tier only**. The
gateway host answered `GET /health` with 200, so the endpoint is live; the
absence was credential, not service.

## 2. The gateway shapes the design

`POST /v1/generate` accepts **exactly one field**, `prompt`. No system role, no
temperature, no `response_format`, no tools, no model selection, no streaming.
Three consequences run through everything below:

1. **Every instruction is inlined** in the prompt string. There is no
   privileged channel, so prompt text and user text arrive at the model in the
   same buffer — the reason the boundary cannot rest on the model obeying it.
2. **There is no structured-output mode.** Responses are parsed defensively by
   `extract_json`, and any parse failure must fall back rather than fail.
3. **There is no tool channel.** This is the load-bearing fact for §4 of the
   phase brief: a model reached through this interface has no mechanism by
   which to invoke MILP, REI, RF or governance, whatever a prompt tells it.

Limits are **shared across all consumers**: cumulative USD budget that does not
reset, 100 requests/day, 20/rolling-minute, 100k prompt chars, 2,000 output
tokens, 60s server timeout. This is why the evaluation batches.

## 3. Finding — the model is consulted far less often than it appears

`IntentAgent.resolve` calls the model only when rule confidence `< 0.75`.
Rule confidences:

| Rule | Confidence | Model consulted? |
|---|---:|---|
| Comparison, capacity change, closure, explanation, resilience, optimization, state | 0.80–0.90 | **No** |
| External event with no named facility | 0.60 | Yes |
| "what if" with no facility matched | 0.40 | Yes |
| No rule matched | 0.00 | Yes |

`ConversationalNLU._classify` short-circuits earlier still: `FORECAST`,
`STATUS_QUERY` and metric-bearing `NETWORK_STATE_QUERY` are decided before
`IntentAgent` is called at all, so the model never sees them.

**Implication for the evaluation.** Measuring the model through the system
would measure mostly the rules. The evaluation therefore runs two modes —
`SYSTEM` (what a user experiences) and `LLM_TIER` (`_llm_based` forced on every
case) — because they answer different questions and only the second is a
measurement of the model.

**Implication for risk.** Every phrase the rules recognise is a request the
model never sees. Widening rule coverage is simultaneously a cost reduction and
an attack-surface reduction, which is why several Phase 3.1 fixes take that
form.

## 4. The prompt

`_llm_based` builds one prompt: role line, the `Intent` enum, the
`ScenarioActionType` enum, the real facility list (capped at 60), a JSON schema
sketch, six rules, then `User request: {text}`.

Weaknesses visible on inspection, before any measurement:

* **`STATUS_QUERY` and `FORECAST` are listed but never explained.** They were
  added in Phase 3 and the prompt enumerates the enum, so the names appear with
  no definition of what distinguishes them from `NETWORK_STATE_QUERY`.
* **No instruction about probability.** Probability extraction is delegated to
  `ExternalSignalAgent`, so the intent prompt says nothing — but a model asked
  for scenario numbers may still volunteer one.
* **No instruction to refuse.** Nothing tells the model that some inputs are
  adversarial and should classify as `UNKNOWN`.
* **User text is last**, immediately after the rules, which is the position
  from which an injected "ignore the above" is most effective.

## 5. Where the defences actually are

Not in the prompt. Four structural mechanisms, each of which holds whatever the
model returns:

| # | Mechanism | Where |
|---|---|---|
| 1 | The intent schema has **no field** able to hold a cost, REI, RF, SLA or governance verdict; `parameters` rejects ~20 result-value key names | `ConversationalIntent` |
| 2 | Facility ids are **filtered against master data** — `entities = [f for f in parsed[...] if f in allowed]` | `_llm_based` |
| 3 | Numeric claims in narrative are **grounded and stripped** if unverifiable | `numeric_grounding` |
| 4 | Governance runs on deterministic evidence, never on model text | `action_classifier` |

## 6. Gaps identified at audit, before measurement

| # | Gap | Severity |
|---|---|---|
| A1 | `LLMResponse.model_name` is never populated; `nlu._model_name()` reads a `config.model_name` that does not exist and always yields `"gateway"`. **Model provenance is not actually recorded.** | Medium — audit records cannot be re-evaluated when the model changes |
| A2 | No metric counts unparseable model output; failures fall back silently | Medium |
| A3 | `NLUResult` is defined and never used | Trivial |
| A4 | The model's intent is accepted verbatim. It is the **only** model output that is neither range-checked nor cross-checked against master data — and it selects the workflow | **High**, confirmed under test in §7 of the results |
| A5 | No live-model evaluation exists at all | High — this phase |

A4 deserves its statement plainly: entity ids are filtered, numbers are
grounded, governance is deterministic — but nothing downstream re-derives the
*intent*. It is the one model output with no second opinion.

## 7. What this phase does and does not touch

**In scope:** the evaluation dataset and harness, the NLU prompt and vocabulary,
entity resolution, ambiguity adjudication, validation, probability extraction,
model provenance.

**Untouched, by instruction and in fact:** MILP, REI, RF, Reasoning, numeric
grounding, governance rules and thresholds, the action taxonomy, the workflow
planner's routing table, the orchestrator control plane. No forecasting agent is
built. Nothing is pushed.

The one governance-adjacent change is in the NLU, not in governance: the model
may no longer relabel an explicit closure request as something milder. The rule
that a structural action is `HUMAN_ONLY` is unchanged — what changed is that a
closure is still recognised as a closure.
