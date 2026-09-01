# Phase 10.4 — The agentic flow, and scenario planning

Two reported problems. Both turned out to be a working engine with a broken
path to it.

---

## 1. The reasoning LLM emitted nothing, on every call

`TEXT_API_TOKEN` was set, the gateway was reachable, and every reasoning call
came back with **`output_tokens=1984` and zero characters of text**. The
orchestrator degraded to deterministic templates each time, so the agentic
layer had never once produced an LLM-authored word.

### Measured, not guessed

Two live calls established the shape of it:

| Prompt | Output tokens | Characters | Parses |
|---|---|---|---|
| `Reply with exactly this JSON: {"ok": true}` | 107 | 12 | yes |
| The reasoning prompt as shipped | **1,984** | **0** | — |

The gateway caps output at 2,000 tokens and the backing model bills its
internal reasoning to that same allowance. The prompt asked for enough
deliberation to consume the entire budget before a single visible character
was written.

### What bought the text back

Three changes, tested one at a time:

* **Dropped the `claims` array.** It required restating every figure with its
  exact value "so your figures can be verified" — a cross-checking task that
  dominated the reasoning. It was also redundant: `ground_narrative()` takes
  `structured_claims=None` and falls back to `extract_numeric_claims()`, which
  reads the numbers straight out of the visible text. The code says as much —
  that fallback exists for "the case where it returns prose anyway, which a
  prompt-only gateway cannot prevent".
* **Stopped asking it to verify.** The old rules ("every number you write is
  checked", "any figure that does not match will be REMOVED") invited exactly
  the deliberation that ate the budget. Grounding still runs — in code,
  afterwards.
* **Capped each string in the schema itself** rather than in prose, and ordered
  the fields by importance so a long reply loses the least valuable first.

| | Before | After |
|---|---|---|
| Output tokens | 1,984 | 1,429 |
| Characters of text | 0 | 420 |
| Valid JSON | no | **yes** |
| `ReasoningResult.source` | `template` | **`llm`** |

Live, on the client network, the assistant now answers:

> *"I find the network serves 28,249 of 36,982 units (76.3858% fill), leaving
> 8,733 units unserved. My eight open facilities show low average
> utilisation…"*

### The safety net is unchanged

`extract_json` still refuses to repair a truncated object — its docstring is
explicit that "a half-read model answer is more dangerous than no answer at
all", and that is right, so nothing was relaxed there. Grounding was tested
directly: a hallucinated `999,999.00` against a payload that says
`18,067,793.96` comes back `CONTRADICTED` → `GROUNDING_FAILED`, and
`strip_ungrounded_claims()` removes it from the visible text.

Two tests pinned properties of the old prompt — that it contains
`DETERMINISTIC RESULTS:` and the word "authoritative". Both are things the
prompt *should* say, so the new prompt says them rather than the tests being
changed.

---

## 2. Scenario planning

The backend was solving scenarios correctly the entire time. Closing F006 on
the client network returns, and always returned:

| | Baseline | Scenario |
|---|---|---|
| Business network cost | ₹18,067,794 | **₹9,612,573** |
| Demand fill rate | 0.7639 | **0.6684** |
| Unserved demand | 8,733 | **12,262** |
| Facilities open | 8 | **5** |
| Max utilisation | 43.12% | **69.22%** |

Three separate defects stood between that and the screen.

### 2a. The Digital Twin had nothing to redraw from

The simulate response carried network **totals** only — no per-facility state,
no per-lane flow. The map cannot show what a scenario changed without them.

`KPIRegistry.facility_kpis()` and `.flow_kpis()` both already accept the `key`
that selects a scenario's own solved state, so nothing new is computed:
`baseline_facilities`, `scenario_facilities`, `baseline_flows` and
`scenario_flows` are now returned alongside the KPIs.

### 2b. The scenario map was a hardcoded table

`getScenarioNetworkData()` was a switch over five prototype scenario ids
(`SCN_REBALANCE`, `SCN_USER_1`, `SCN_AI_REC_4`, …). Each branch assigned literal
utilisation and throughput to `DC_DELHI`, `DC_MUMBAI`, `DC_BENGALURU`,
`DC_KOLKATA` and `DC_GUWAHATI`, with hand-written flow overrides on
`PLT_BADDI → DC_DELHI` carrying a `deltaText` describing a rebalancing no
solver had performed.

A user-created scenario matched none of those branches. It fell into an `else`
that invented `DC_DELHI` at `delhiUtil || 88.0` plus four more facilities —
none of which exist in an uploaded network, so the override map addressed
nothing and **every scenario rendered the baseline**.

It now reads the scenario's own solved state, and marks a corridor "changed" by
comparing its scenario volume against the same lane in the baseline. On
"Close Kolkata DC": F006 closed (the optimiser also closes F001 and F002),
**6 corridors move**, and utilisation shifts per facility — F003 to 69.22%,
F007 to 54.01%.

`getFlowsForState()` had the same disease for the Digital Twin's own
Actual/Optimised/Recommended toggle: "optimised" and "recommended" were
manufactured by scaling three named prototype lanes by 0.88, 1.25, 0.82, 1.45.
On a real network none matched, so all three toggles already drew identical
corridors while claiming three different plans. Removed; a genuine alternative
plan for the current network is a scenario.

### 2c. Listed scenarios were never mapped

`hydrateFromBackend()` pushed **raw backend records** into `SCENARIOS`, while
the create flow pushed records through `mapScenarioRecord()`. So a scenario
created in-session had `totalCost`; the same scenario after a page reload had
only `scenario_kpis`, and the comparison table — which reads `totalCost` —
rendered it blank. Both paths now use the mapper.

### 2d. Every scenario was called "Scenario 1"

`scenarioDisplayName()` returned the user's own name only when the id started
`SCN_CUSTOM_`. The backend has never issued that prefix — it generates
`SCN_<8 hex>` — so the branch never fired and every scenario appeared as
"Scenario N" in dropdowns, table headers and the drawer. The prefix existed to
distinguish user scenarios from built-in presets; the presets are gone, so
anything in the list is one the user solved. The table now reads
**"Expand Pune DC"**.

---

## 3. Resilience assessment failed on every run

Visible in the logs as `orchestrator.tool.failed capability=resilience.assess
code=ENGINE_FAILURE`, three times per scenario.

`compute_baseline()` in `rei.py` solves its own baseline with the **strict**
config and raises `BaselineSolveError` if it is infeasible — bypassing the
Phase 10.2 shortage relaxation, which lives in the optimizer's engine adapter.
The client's network is strictly infeasible, so resilience was unavailable for
it and for any network like it.

The inconsistency is the giveaway: `resilience/engine.py` already runs every
*disruption* solve with `allow_shortage=True`. Only the baseline refused — it
was held to a stricter standard than the disruptions it exists to be compared
against. It now honours the same `relax_to_shortage_when_infeasible` flag the
caller set, and logs when it does.

`assess_network_resilience()` now completes on the client network, reporting
`max_performance_impact = 3,734,320`.

---

## 4. Validation

| Suite | Result |
|---|---|
| `validation/phase_10_4/run_scenario_check.py` (new) | **11 / 11** |
| `validation/phase_10_3/run_ui_flow_check.py` | **25 / 25** |
| `validation/phase_10_3/run_empty_project_check.py` | **10 / 10** |
| `validation/phase_10_1/run_client_data_e2e.py` | **27 / 27** |
| Backend regression | **2,542 passed · 4 skipped** |
| Uncaught page errors | **0** on every journey |

The scenario harness drives the real toolbox — open the builder, pick a
facility, set an amount, click Run — so the whole path is exercised: form →
simulate → mapper → `SCENARIOS` → comparison table → map.

---

## 5. Still not done

This is closer to production-ready than it was, and it is not there.

* **`/orchestrator/*` is unauthenticated.** Unchanged, and now more material:
  the reasoning layer it fronts actually produces content. It remains the
  largest gap.
* **Persistence is in-process.** A restart loses accounts, projects, uploads
  and every solved scenario. There is no database. For a demo this is a
  nuisance; for production it is disqualifying on its own.
* **The LLM call budget is 4 per gateway instance and the daily allowance is
  shared.** Fine for a demo, not sized for concurrent users.
* **Facility-level REI reports zero facilities assessed** on this network even
  though the network-level figure computes, and one facility (F007) produces a
  negative performance impact — closing it lowers *business* cost while
  stranding more demand, because the shortage penalty is excluded from that
  measure by design. The engine logs it; nothing on screen explains it.
* **No contract parser**; the PDF path stores the file and says so.
* **Uploaded signals do not influence the forecast**; capacity history reaches
  no engine.
* **No scenario is generated automatically** — the user must create the first
  one.
