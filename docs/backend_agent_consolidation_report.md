# Backend Agent Consolidation — Phase 7A

Extraction Agent and Reasoning Agent work from a teammate branch, audited and
selectively integrated into the local NetGravity baseline.

| | |
|---|---|
| **Branch audited** | `feature/extraction-ingestion-integration` — `github.com/aayusht115/NetGravity` |
| **Commit analysed** | `0503b1fdc9699db5ff0d0c8caf5f090e5bbf9426` — *"feat: add executive reasoning agent"*, Aayush Thakur, 24 Aug 2026 |
| **Local baseline** | `a15661bd691b3fca91e1a24e706f1405a7d2e6c9` (authoritative) |
| **Divergence point** | `d3a46a6` — *"feat: implement 2D/3D Digital Twin builder…"*, present in **both** histories |
| **Clone location** | scratchpad, disposable; left **unmodified** (`git status` clean, HEAD still `0503b1f`) |
| **Git operations performed** | none — no commit, no push, no branch, no PR, no merge |
| **Tests before → after** | **2,021 passed / 3 skipped → 2,103 passed / 4 skipped, 0 failed** |

---

## 1. Branch ancestry — what was actually new

The branch is not a fork of an unrelated codebase; it shares history with ours
up to `d3a46a6`, after which the two diverged. Our side did Phases 5, 6, 6.1 and
6.2 (Digital Twin, Forecasting, validation harness, structural-break
adaptation); their side did four commits.

Verified by commit presence rather than by reading diffs: `b99d0d8`, `9317d85`
and `d3a46a6` are **present in our local history**; the four below are not.

| Commit | Subject | Insertions |
|---|---|---|
| `7d83892` | feat: add external signal ingestion | 2,969 |
| `47c8f0c` | feat: add AI column recommendations and clarification flow | 1,835 |
| `a5163f6` | docs: document ingestion and column clarification | 65 |
| `0503b1f` | feat: add executive reasoning agent | 1,200 |

**6,068 insertions across 69 files** was the candidate surface.

### A three-way analysis, after a false start

My first pass compared each of the teammate's changed files against our working
tree with `git show d3a46a6:<file> | diff -`. That reported almost every file as
changed on both sides — including all of `netgravity/ingestion/`, which we had
frozen since Phase 4A. The cause was line endings: `git show` emits LF while the
working tree is CRLF, so `diff` saw every line as different.

Redone with `git diff` (which honours `autocrlf`), the real picture is far
narrower — **only four files were touched by both sides**:

| Both changed — genuine merge required |
|---|
| `netgravity/orchestrator/core/execution_context.py` |
| `netgravity/orchestrator/core/planner.py` |
| `netgravity/orchestrator/registry.py` |
| `netgravity/orchestrator/schemas/requests.py` |

Everything else the teammate changed, we had not touched. That is what made a
file-level integration safe rather than reckless, and it is worth recording
because the wrong answer would have led to hand-merging sixty files that needed
no merge at all.

---

## 2. Integration gap analysis

### ALREADY EXISTS IN MAIN — not copied

| Component | Where it already lives |
|---|---|
| Ingestion core (zones, pipeline, classification, mapping) | `netgravity/ingestion/` — Phase 4A |
| Structured external-signal ingestion | `ingestion/adapters/signals.py` |
| Guardrail relevance policy | `ingestion/guardrails/relevance.py` |
| `MarketIntelligenceSignal` / `ExternalSignal` split | `ingestion/schemas/signal.py`, `orchestrator/schemas/requests.py` |
| Orchestrator routing of market signals to forecasting | `orchestrator/routing/signal_router.py` — correction phase |
| Execution context, audit, provenance | `orchestrator/core/`, `orchestrator/audit/` |
| Numeric grounding | `orchestrator/validation/numeric_grounding.py` |
| Governance | `orchestrator/governance/` |
| Forecasting agent + structural-break adaptation | `netgravity/forecasting/` — Phases 6 / 6.2 |
| Resilience / REI | `netgravity/resilience/` |
| Digital Twin | `orchestrator/twin/` — Phase 5 |
| Reasoning agent (template + gateway paths) | `orchestrator/agents/reasoning_agent.py` |

### REQUIRED — INTEGRATED

| Component | Files | Why it was required |
|---|---|---|
| **Reasoning evidence layer** | `orchestrator/reasoning/evidence.py` | Indexes a deterministic payload into referenceable metrics **without deriving or altering a value**. Reads our Phase 5 `DigitalTwinState`/`TwinComparison` — already aligned. Nothing equivalent existed. |
| **Reasoning contract validation** | `orchestrator/reasoning/validation.py` | Structural checks on model output — unknown evidence refs, scope/entity mismatch, insight caps. Callers fail closed to the template. |
| **Read-only reasoning tools** | `orchestrator/reasoning/tools.py` | `evidence_manifest`, `lookup_evidence`, `list_missing_evidence` — closures over one immutable pack, no write capability. Unknown refs return `NOT_FOUND`. |
| **Pluggable reasoning runtime** | `orchestrator/reasoning/runtime.py` | `ReasoningRuntime` Protocol, `StubReasoningRuntime` (no I/O), `OpenAIAgentsReasoningRuntime` (inert unless explicitly selected **and** keyed **and** installed). |
| **Versioned reasoning prompt** | `orchestrator/reasoning/prompts.py` | `reasoning-v2.0`, with the evidence rules stated in the prompt as well as enforced in code. |
| **Reasoning schemas** | `orchestrator/schemas/reasoning.py` | `ReasoningDraft`, `ExecutiveBriefing`, `KPIInsight`, `EvidenceMetric`, `ReasoningEvidencePack`, `ReasoningScope`, `InsightRequest`. |
| **Executive briefing on the result** | `orchestrator/schemas/risk.py` | `ReasoningResult.briefing` — additive; legacy `summary`/`recommendation` still populated so existing consumers are untouched. |
| **Grounding fact-spec extension** | `orchestrator/validation/numeric_grounding.py` | Facility- and lane-level twin metrics could not previously be grounded. See §9 — this touches a frozen area. |
| **Insights endpoint** | `orchestrator/api.py` | `POST /orchestrator/insights` — explains an already-published twin state. Read-only; cannot optimise, mutate the twin, or classify an action. |
| **Prose market-intelligence adapter** | `ingestion/adapters/market_intelligence.py`, `ai/signal_reader.py`, `document_text.py` | Reads a news article / circular / notice (PDF, text, markdown) into `MarketIntelligenceSignal`. **Complements** `adapters/signals.py` rather than duplicating it — see §9. |
| **Shared gateway contract** | `netgravity/llm/` | The gateway's *facts* — endpoints, limits, retryable statuses — in one module imported by both clients. Removes a real drift hazard: the gateway budget is shared and cumulative, so a limit corrected in one client and not the other spends someone else's capacity. |
| **Column clarification flow** | `ingestion/api.py`, `service.py`, `session.py`, `draft.py`, `profiling.py`, `ai/clarification.py`, `memory/field_catalog.py`, `schemas/field_mapping.py`, `review.py` | Human-in-the-loop resolution of unrecognised client columns: profile → provisional draft → AI recommendation → clarification → finalise. Backend only. |
| **`MARKET_INTELLIGENCE` intent + workflow** | `schemas/requests.py`, `core/planner.py` | A stated market change ("diesel is up 6%") is not a hazard with a probability and must not be routed as one. |
| **`market.score_signal` capability** | `orchestrator/registry.py` | Scores reported signals against the guardrail policy at execution time, against the pinned network. |
| **Credential isolation hardening** | `conftest.py` | Imports `ingestion.config` *before* deleting credential env vars, so a lazy `.env` load cannot repopulate them mid-test and turn an offline test into a live, budget-consuming call. A real security fix. |
| **Tests** | 6 files (see §15) | The teammate's own coverage for everything above. |

### ADAPTED

| Component | Adaptation | Reason |
|---|---|---|
| `OrchestratorRequest.market_signal` (singular) | **Dropped**; folded onto our existing `market_signals` (plural) | §6 — duplicate concept. See §10. |
| `ExecutionContext.market_signal` | Same | Same |
| `ChatService` request build | Contributes `[signal]` to `market_signals` | Same |
| `market.score_signal` handler | Scores the **list**; returns `{signals, n_scored, n_passed}` | Same |
| `ReasoningAgent._template` market block | Iterates N signals; still accepts a bare dict | Same |
| 3 tests in `test_market_intelligence_route.py` | Read `scored["signals"][0]` | I changed the capability's contract; the tests follow it |
| `core/planner.py` | Took their `MARKET_INTELLIGENCE` workflow; **kept our** `FORECAST` workflow | Their branch predates our forecaster and still says "no forecasting capability is registered" |
| `pyproject.toml` / `requirements.txt` | `openai-agents` added as an **optional** `llm` extra | §7 |

### DUPLICATE — DO NOT COPY

| Item | Why |
|---|---|
| Their Digital Twin builder/service/store (`d3a46a6`) | Already in our history — it is the shared divergence point, not new work. |
| Their `orchestrator/routing/` | Untouched by them; **our** `ExternalSignalRouter` from the correction phase is authoritative. |
| Their structured `adapters/signals.py` | Identical to ours. |

### NOT REQUIRED

| Item | Why |
|---|---|
| `.gitignore` (`_patches/`) | Their machine-transfer scratch directory; not part of the feature. |
| `.env.example` | Ours already documents the gateway. The three new reasoning variables are recorded in §11 rather than by overwriting a file whose content is a local-setup concern. |
| `README.md` ingestion section | Documentation of the clarification flow already integrated as `docs/ingestion_clarification_flow.md`; leaving our README avoids a merge whose only content is prose. |
| All frontend / prototype work | Out of scope by instruction. |

### OBSOLETE

Their `Intent.FORECAST` workflow template description ("no forecasting
capability is registered") — true when written, false in our baseline since
Phase 6. **Not** integrated.

---

## 3. Extraction Agent components

The intended architecture holds:

```
Data / External Input → Ingestion → Extraction Agent → Structured Evidence
                                                              ↓
                                                        Orchestrator
                                                              ↓
                                                    specialist workflows
```

Three intake shapes now reach **one** schema through **one** guardrail:

| Route | Adapter | State |
|---|---|---|
| Structured JSON signal file | `adapters/signals.py` | already in main |
| Prose document (PDF / txt / md) | `adapters/market_intelligence.py` | **integrated** |
| Spreadsheet of signals | ordinary tabular pipeline, `ContentType.MARKET_SIGNAL` | **integrated** |
| Chat utterance | `ChatService` → `NLU._extract_market_signal` | **integrated** |

**Verified prohibitions.** By AST import analysis over every integrated
extraction-side module (`extraction_agent.py`, `market_intelligence.py`,
`signal_reader.py`, `ingestion/service.py`, `ingestion/api.py`):

| Extraction must not… | Result |
|---|---|
| invoke Forecasting | **CLEAN** — no import of `netgravity.forecasting` |
| invoke MILP | **CLEAN** — no import of `netgravity.optimization` |
| calculate REI | **CLEAN** — no import of `netgravity.resilience` |
| calculate RF | **CLEAN** — no import of `orchestrator.risk` |
| make governance decisions | **CLEAN** — no import of `orchestrator.governance` |
| decide the downstream workflow | **CLEAN** — the planner owns workflow selection |

The prose adapter also **will not fetch**: no HTTP call, feed reader or scraper.
Signals arrive because a person supplied them.

---

## 4. Reasoning Agent components

```
authoritative evidence → Orchestrator → Reasoning Agent → grounded reasoning
                                                                 ↓
                                                    validation / governance → output
```

Integrated as an **extension** of the existing agent, not a replacement. The
new `runtime` parameter defaults to `None`, and `scope`/`entity_id`/
`user_question` default to `NETWORK`/`None`/`""` — so every existing call site
behaves exactly as before.

Four fallback layers, each failing closed to the deterministic template:

1. runtime unavailable (not selected, no key, or SDK absent) → template
2. runtime raises → template + warning
3. output fails `validate_reasoning_draft` → template + the violations listed
4. output ungrounded → claims stripped, confidence downgraded, `grounding_status` set

**Verified prohibitions.** The Reasoning Agent must not become authoritative
for MILP cost, allocation, REI, RF, probability, facility existence,
optimisation or governance decisions:

| Check | Result |
|---|---|
| imports MILP / REI / RF / governance / forecasting | **CLEAN** — none, across all 6 reasoning modules + schemas + agent |
| reuses existing grounding | **YES** — `orchestrator.validation.numeric_grounding` |
| reasoning schemas expose a probability | **ABSENT** — no `event_probability`, `probability:`, `likelihood:`, `risk_factor:` field |
| mutates the payload it explains | **NO** — payload byte-identical after `reason()` (asserted live, §11) |
| tools offer any write capability | **NO** — three read-only functions; unknown refs return `NOT_FOUND` |

---

## 5. Orchestrator remains the control layer

Not redesigned. Both agents were integrated against existing interfaces:
capabilities registered in `CapabilityRegistry`, steps composed by `planner.py`,
evidence passed through `ExecutionContext`.

The three forbidden shortcuts, checked:

| Forbidden path | Status |
|---|---|
| Extraction → Forecasting | **Absent.** `wf_market_intelligence` has no forecast step, verified live: `['network.load_snapshot', 'market.score_signal', 'reasoning.synthesise', 'governance.classify']`. Signals reach forecasting only via `ExternalSignalRouter` on the forecast workflow. |
| Reasoning → MILP | **Absent.** No solver import; `ReasoningResult` cannot carry an objective. |
| Reasoning → Governance | **Absent.** `governance.classify` is a separate plan step the planner owns. |

`market.score_signal` is registered `DETERMINISTIC`, `optional=True`,
`NO_RETRY`, depending only on `CAP_LOAD_NETWORK` — so a run with no signal, or a
scoring failure, degrades the narrative rather than failing.

**Scoring is not routing.** Clearing the guardrail here admits a signal nowhere;
`ExternalSignalRouter` reads `passed_guardrail` as one of several conditions.

---

## 6. Schema consolidation

| Schema | Decision |
|---|---|
| `ExtractionResult` | Ours authoritative; teammate's field additions taken (`market_intelligence`) |
| `MarketIntelligenceSignal` | **Ours unchanged.** No probability field, by design |
| `ExternalSignal` | **Ours unchanged.** Retains `event_probability` |
| `ForecastRequest` / `ForecastResult` | **Ours unchanged.** Phase 6/6.2 versions authoritative |
| `ReasoningResult` | Extended additively with `briefing` |
| `ExecutionContext` | Merged; single `market_signals` field |
| `Snapshot` / `Network` | **Unchanged** |
| provenance / audit events | **Unchanged** |
| `ReasoningDraft`, `ExecutiveBriefing`, `EvidenceMetric`, … | New, no equivalent existed |

### The RF safety boundary is unchanged

The two signal types remain separate all the way through execution:

| | `MarketIntelligenceSignal` | `ExternalSignal` |
|---|---|---|
| carries a probability | **no, by design** | yes, `event_probability` |
| feeds `RF = P + REI − P·REI` | **never** | yes |
| may enrich a forecast | yes, if routed | **never** |
| request field | `market_signals` | `external_signal` |
| capability | `market.score_signal` | `external.interpret_signal` |

Verified live: a router given one of each returns
`{'ROUTED_TO_FORECASTING': 1, 'REFUSED_RISK_SIGNAL': 1}`. No integrated code
converts confidence, severity, materiality or direction into a probability.

---

## 7. Dependency changes

**One added**, as an optional extra:

```toml
llm = ["openai>=1.40.0", "anthropic>=0.40.0", "openai-agents>=0.2.0"]
```

Same placement the teammate chose. Nothing was removed, no version was changed,
and no existing dependency was replaced.

`openai-agents` is **not installed** in this environment, which is the point:
the SDK is imported only inside `OpenAIAgentsReasoningRuntime.run()` on the
explicitly enabled live path. The whole suite runs without it, taking the
deterministic template path — one test skips (§15).

No `.env` file, secret or token entered the working tree (`git status` verified).

---

## 8. Failure contracts — status for this phase

Per §8, the full retry/reroute/escalation framework was **not** implemented.
What was verified:

| Requirement | Status |
|---|---|
| explicit success/failure status | **Met.** `ReasoningResult.source` ∈ {llm, template}; `grounding_status`; `validation_warnings`; `score_signal` returns `n_scored`/`n_passed`; ingestion returns `FileResult` with `Severity` |
| null distinguished from valid zero | **Met.** Evidence renders `None` as `"Not available"`, never `0`; `list_missing_evidence` reports explicit unavailability; the prompt forbids calling an absent value zero |
| malformed output exposed to the orchestrator | **Met.** `validate_reasoning_draft` violations are appended verbatim to `validation_warnings` |
| failure cannot silently become success | **Met.** Four fallback layers, each recording why. A failed run degrades to the deterministic template with a stated reason |
| audit/provenance preserved | **Met.** `market.score_signal` appears in `trace.engine_results`; unchanged event vocabulary |

**DEFERRED:** retry policy for the reasoning runtime, rerouting to an alternate
runtime, escalation to a human on repeated failure, and a circuit breaker on the
shared gateway budget.

---

## 9. Frozen components — one deliberate change

§9 froze MILP, REI, RF, governance, forecasting algorithms, structural-break
adaptation, ingestion core, Digital Twin, conversation/NLU and frontend.
`git status` confirms **zero** changes under `netgravity/optimization/`,
`netgravity/resilience/`, `orchestrator/risk/`, `orchestrator/governance/`,
`orchestrator/twin/`, `orchestrator/routing/`, `netgravity/schemas/`,
`netgravity/costs/`, `netgravity/metrics/` and `netgravity/forecasting/`.

Following the §9 procedure for the one unavoidable case:

**1 — The exact incompatibility.** `orchestrator/validation/numeric_grounding.py`
is the grounding mechanism §12 requires the Reasoning Agent to reuse. Its
`_FACT_SPEC` table listed only network-level metric names. The new
facility- and lane-scoped briefings cite twin values —
`throughput_units`, `utilization_pct`, `units_delta`, `distance_km`,
`carbon_kg`, `closure_cost_charged` — none of which had an entry, so every one
would be reported ungrounded and stripped. The alternative was to *not* reuse
grounding for scoped insights, which §12 forbids.

**2 — The smallest possible change.** Additive only: eleven new entries in a
name→(kind, source) dict, plus one rule letting the twin's generic comparison
keys (`baseline_value`, `comparison_value`, `abs_delta`, `pct_delta`) inherit
the kind of the metric named beside them — so a cost delta cannot ground
against a unit delta. No existing entry altered, no function signature changed,
no grounding logic rewritten.

**3 — Documented here**, and the ingestion core was likewise extended rather
than redesigned: `adapters/market_intelligence.py` is a **new sibling** of
`adapters/signals.py`, and its module docstring states the non-duplication —
`signals.py` ingests already-structured JSON, the new adapter starts from prose,
and both end at the same schema and the same guardrail.

Conversation/NLU **was** modified (`nlu.py`, `chat_service.py`) — unavoidable,
because recognising a market-intelligence utterance is the entry point of the
integrated route. Our side had not touched either file, so these are the
teammate's changes taken whole, plus the one-line adaptation in §10.

---

## 10. The one design decision I made against the branch

The teammate added `OrchestratorRequest.market_signal` (singular) for a
chat-reported signal. Our correction phase had already added
`market_signals` (plural) for signals awaiting a routing decision.

Both are "a `MarketIntelligenceSignal` arriving at the orchestrator". §6 says
*do not create duplicate concepts*, and two fields naming one concept is the
duplication that makes every reader check both and eventually miss one. The
arrival route is not something the orchestrator needs to branch on.

**Consolidated onto `market_signals`.** The chat path contributes one element;
`market.score_signal` scores the list; the reasoning payload carries
`market_evidence` as a list. Blast radius was five source lines and three test
assertions.

It also *gains* something. With one transport, a chat-reported signal is
visible to `ExternalSignalRouter` if a forecast workflow runs — which is
precisely the `Extraction → Orchestrator → Signal Router → Forecasting` flow
§3 specifies. Two fields would have left the chat route unable to reach it.

---

## 11. Orchestrator integration points

| Integration | Where |
|---|---|
| `Intent.MARKET_INTELLIGENCE` | `schemas/requests.py` |
| `CAP_SCORE_MARKET = "market.score_signal"` | `core/planner.py` |
| `wf_market_intelligence` workflow | `core/planner.py` — load → score → reason → govern |
| `score_market_signal` handler + capability | `registry.py` |
| `market_evidence` in the reasoning payload | `registry.py` |
| `build_orchestrator(reasoning_runtime=…)` | `registry.py` |
| `POST /orchestrator/insights` | `api.py` |
| Ingestion blueprint at `/api/ingestions` | `app/backend/app.py` |

New environment variables (documented, not committed):
`NETGRAVITY_REASONING_RUNTIME`, `NETGRAVITY_REASONING_MODEL`, `OPENAI_API_KEY` —
all three required together for a live run; any missing → deterministic template.
`conftest.py` strips all three in every test.

---

## 12. Security / boundary validation

| Imported agent code must not be able to… | Result |
|---|---|
| fabricate facility IDs into authoritative state | **Blocked.** Evidence is indexed from the payload; `twin_reasoning_payload` raises for an unknown facility or lane |
| fabricate costs | **Blocked.** Grounding checks every number against `build_authoritative_facts`; unmatched claims stripped |
| fabricate REI | **Blocked.** Same, `source="rei_engine"` |
| fabricate RF | **Blocked.** Same, `source="risk_engine"` |
| fabricate probability | **Blocked.** No probability field exists in any reasoning schema |
| directly invoke MILP / REI / RF | **Blocked.** No import, verified by AST across 11 modules |
| bypass the orchestrator | **Blocked.** Both agents are invoked as registered capabilities |
| bypass grounding | **Blocked.** `_ground()` runs on every return path including all four fallbacks |
| convert signal confidence into RF probability | **Blocked.** Separate types, separate fields, separate capabilities; router returns `REFUSED_RISK_SIGNAL` |

All reuse existing mechanisms — the grounding module, the guardrail policy, the
capability registry — rather than introducing parallel ones.

---

## 13. Tests

### Before integration

| Suite | Result |
|---|---|
| Teammate branch, as cloned | **1,906 passed, 4 skipped** (`openai-agents` absent) |
| Local main, before any change | **2,021 passed, 3 skipped** |

### After integration

| Suite | Result |
|---|---|
| Reasoning Agent (`tests/reasoning/`) | pass (1 skip — SDK absent) |
| Gateway contract (`tests/test_gateway_contract.py`) | pass |
| Ingestion (`netgravity/tests/ingestion/`, incl. 3 new files) | pass |
| Market-intelligence route (`test_market_intelligence_route.py`) | **32 passed** |
| **Complete regression suite** | **2,103 passed, 4 skipped, 0 failed** (404 s) |

+82 net tests over our baseline. The fourth skip is
`test_reasoning_agent.py:112 — could not import 'agents'`, the optional live
path; the other three pre-date this phase.

No stress, performance or load testing was run, per §10.

### One failure I caused, and fixed

Consolidating onto the plural field made `market_evidence` a list while
`ReasoningAgent._template` still called `.get()` on it — five tests failed with
`'list' object has no attribute 'get'`. Fixed by iterating the list (still
accepting a bare dict, since several tests assemble a payload by hand). Three
further tests asserted the old single-signal return shape of
`market.score_signal`; since I changed that contract deliberately, the tests
were updated to read `scored["signals"][0]` rather than the contract being bent
back to fit them.

### Smoke test

Steps 1–5 pass, MILP core unchanged (**Case-16 = $150,627.70**). Step 6 fails on
the standalone HTML bundle's missing `<script type="module">` tag — the same
pre-existing frontend defect recorded in Phase 6.2, present in committed `HEAD`,
unrelated to this phase, which touched no frontend file.

---

## 14. Repository hygiene

| Check | Result |
|---|---|
| Only backend files changed | **Yes** — 21 new, 38 modified, all Python/docs/config |
| Frontend/prototype modified | **No.** The 5 frontend entries in `git status` are byte-identical to the uncommitted teammate frontend work recorded in Phase 6.2 |
| `.env` or secrets in tree | **None** |
| Clone/scratch files in project | **None** |
| Duplicate agent implementation left | **None.** Only remaining `market_signal` occurrences are the handler *name* and the docstring explaining the consolidation |
| Teammate clone modified | **No** — `git status` clean, HEAD still `0503b1f` |
| Committed / pushed / merged | **No.** Local working tree only |

---

## 15. Remaining gaps

**IMPLEMENTED NOW** is everything in §2 under *REQUIRED — INTEGRATED* and
*ADAPTED*. The following are **DEFERRED**:

1. **Failure-contract framework** — retry policy for the reasoning runtime,
   rerouting to an alternate runtime, escalation after repeated failure, and a
   circuit breaker on the shared cumulative gateway budget (§8).
2. **The live reasoning path is untested against a real model.** `openai-agents`
   is not installed and no key is configured, so `OpenAIAgentsReasoningRuntime`
   has only ever been exercised through `StubReasoningRuntime`. The contract
   validator and all four fallbacks are covered; the SDK adapter itself is not.
3. **`ExecutiveBriefing` has no consumer.** `ReasoningResult.briefing` is
   populated and grounded, but only `POST /orchestrator/insights` reads it. The
   chat surface still returns the legacy `summary`.
4. **`chat_service._forecast_response` still refuses forecasts** — it says
   "NetGravity has no forecasting capability registered", false since Phase 6.
   Carried forward from Phase 6 unfixed; a one-message change.
5. **Market signals do not flow from chat to forecasting end-to-end.** The
   transport now exists (§10) and the router accepts them, but no workflow
   composes `wf_market_intelligence` with `wf_forecast`. A user must ask for a
   forecast separately.
6. **Ingestion clarification flow has no UI**, by scope. `/api/ingestions` is
   mounted and tested; nothing consumes it.
7. **Minor ingestion robustness defect, still open.** `_parse_signal` accepts a
   string for `affected_entities` and shreds it into characters. Recorded in the
   correction phase; ingestion was frozen then and is frozen now.
8. **`.env.example` not reconciled.** Ours does not yet document the three
   reasoning variables (§11 does).

---

## 16. Recommended next phase

1. **Implement the failure-contract framework** (§8's deferred half) — it is the
   largest named gap and everything integrated here fails closed into it.
2. **Exercise the live reasoning path once, deliberately** — install
   `openai-agents`, configure a key, run one insight, and record what the
   contract validator rejects. The budget is shared and cumulative, so this
   should be a single deliberate run, not a test-suite fixture.
3. **Give `ExecutiveBriefing` a consumer** — route the chat surface through it
   so the structured briefing is what users actually see.
4. **Fix the two carried-forward one-liners** — the chatbot's forecast refusal
   and the `affected_entities` string-shredding.
5. **Reconcile `.env.example`** and the README ingestion section, both
   deliberately skipped here as prose-only merges.
6. **Then** stress/performance testing, which §10 excluded from this phase.
