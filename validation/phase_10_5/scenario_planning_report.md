# Phase 10.5 — Scenario planning

Six reported problems. Chasing them down found a seventh in the solver that was
producing most of the visible symptom.

---

## 1. Only one scenario could ever exist

```js
let multiSelectedIds = ['SCN_REBALANCE', 'SCN_USER_1'];
```

Two prototype ids that no backend has ever issued. They resolved to nothing, so
they were invisible — and they permanently occupied two of the three comparison
slots. The creation path then read:

```js
if (multiSelectedIds.length < 3) multiSelectedIds.push(mapped.id);
else multiSelectedIds[multiSelectedIds.length - 1] = mapped.id;
```

The first scenario filled the third slot. **Every scenario after that overwrote
it.** However many were solved, one was on screen.

The selection is now derived from the scenarios that actually exist
(`syncSelection`), and making room drops the oldest rather than overwriting the
newest.

---

## 2. There was no baseline

`SCENARIOS` held only solved scenarios. The comparison table did:

```js
const baseline = SCENARIOS.find((s) => s.id === 'SCN_ACTUAL') || SCENARIOS[0];
```

`SCN_ACTUAL` was never created by anything, so the column headed **"Current
Baseline" was the first user scenario**. Scenario 1 was compared against itself
— every delta an em dash — and scenario 2 against scenario 1 rather than against
the network. Every figure in the table was real; what they were being compared
to was not what the header said. The map's Baseline toggle never rendered for
the same reason.

`baselineFromScenarioRecord` / `baselineFromNetworkKPIs` build a real
`SCN_ACTUAL` row from the snapshot solve every scenario is already measured
against. It is installed by hydration, and by the first scenario of a session so
the comparison has a reference immediately.

---

## 3. Three of the six scenario types could not run at all

| Type | What happened |
|---|---|
| Change Capacity | worked |
| Close Facility | worked |
| Open Facility | worked, but could only pin open a site the client already runs |
| **Change Demand** | reached the API, then rejected: both the API and the orchestrator's validator demanded a `facility_id` for a change that applies to every demand row — and the modal rendered no facility field for it, so it was unreachable |
| **Change Transport Cost** | not in `_ACTION_MAP`; HTTP 400 before any solver saw it |
| **Change SLA** | not in `_ACTION_MAP`; HTTP 400 |

All six now run:

* `ScenarioActionType` gains `ADD_FACILITY`, `CHANGE_TRANSPORT_COST`,
  `CHANGE_SLA`, and a `NETWORK_WIDE_ACTIONS` set for the three that describe the
  whole network rather than named facilities.
* `CHANGE_TRANSPORT_COST` scales `rate_per_unit` on the lanes in scope —
  network-wide, or narrowed to the lanes touching one facility, because "our
  Pune carrier raised rates" is a different question from a market-wide move.
* `CHANGE_SLA` shifts `sla_days` on the demand rows. It **refuses** on a network
  that states no SLA, rather than applying the change to nothing and returning a
  "scenario" identical to the baseline — which would have the user read an
  unchanged cost as evidence that tightening service is free.

Measured on `Dump/NetGravity_Test_Data_Clean.xlsx`:

| Scenario | Result |
|---|---|
| Freight +25% | transport cost ₹863,063 → **₹1,431,590** |
| Demand +20% | total demand 36,982 → **44,378** |
| Relax SLA by 1 day | fill rate 0.7639 → **1.0**, unserved 8,733 → **0** |

That last row is a finding in its own right: the client's network can serve all
its demand. What it cannot do is serve it inside the delivery promise as stated.

---

## 4. "Open a facility" could not open a facility

The Target Location control was a dropdown of the client's own DCs and plants.
Every one of them was already open, and the solver was already free to keep them
open, so choosing one asked a question with a known answer. **"Where should we
put a new DC?" was unanswerable through this application.**

`ADD_FACILITY` was mapped to `OPEN_FACILITY` in the API's action table, which is
what made the two indistinguishable. They are now separate actions, and the
form offers:

* a jump-to list of 32 Indian cities, and
* **editable latitude and longitude** — the authority, so a site can go anywhere;
* capacity, site type, fixed cost and handling cost.

The new site is added as a `CANDIDATE`, not pinned open: if opening it does not
pay, the solver leaves it shut and the page says so.

Freight to and from it is derived by `ScenarioEngine._auto_connect_facility` —
already in the codebase and never reachable from the orchestrator — from the
haversine distance to every plant and market, priced at **the client's own
average rate per kilometre across their existing road lanes**, not at a
constant. That is what makes the answer theirs.

> `ADD_FACILITY NEW_NAGPUR_DC 'Nagpur DC' (21.1458, 79.0882) capacity 6,000 units/period, 10 lanes derived`

---

## 5. The solver's optimality tolerance was measured against a notional penalty

This is the one that produced "it seems random", and it took measuring two
almost-identical models to see it.

Adding capacity to a facility **raised** the reported cost:

| Model | Reported | Status | Facilities open |
|---|---|---|---|
| The network, unchanged | ₹9,561,047 | OPTIMAL | 5 |
| The same network, +6,000 units capacity at F004 | **₹14,512,146** | OPTIMAL | 7 |

Relaxing a constraint cannot make a minimum worse. The 5-facility plan is still
feasible in the second model, so the second answer is one the first proves is
beatable by ₹5M — and both came back OPTIMAL.

The cause is in `OptimizationConfig`:

```python
mip_gap = 0.001          # 0.1% optimality gap
shortage_penalty = 1e6   # currency / unit unmet demand
```

When the strict model is infeasible the engine re-solves with `allow_shortage`,
and the objective becomes `business_cost + shortage_penalty × unserved`. On this
network that second term is **₹8.73 billion** against a business cost of ~₹1.8e7.
A 0.1% *relative* gap is then a tolerance of **₹8.7 million of real money**: the
solver stops the moment it has the right unserved quantity and stops caring
about the spend entirely.

The fix is an absolute tolerance. `OptimizationConfig.mip_gap_abs` is passed
through to all four solver backends (`gapAbs` on HiGHS/CBC/CPLEX, `mipGapAbs` on
Gurobi), with the relative gap tightened alongside it so it can actually govern
— a solver stops when *either* tolerance is met, so setting one without the
other changes nothing.

It is set wherever shortage is permitted, sized to the network's own spend
(0.1% of the total fixed cost of running every non-market facility for one
period, floored at ₹1,000) rather than as a constant, so it stays reachable on a
network an order of magnitude larger:

* `DeterministicEngineClient._relaxed_shortage_result` — every scenario and
  baseline solve;
* `resilience/engine.py::shortage_config` — every disruption solve, now shared
  by `rei.py`. Every REI figure is a *difference* of two shortage-permitted
  costs, so a ₹5M tolerance on each side could swamp the difference entirely.

After the fix, on the same three models:

| Scenario | Before | After |
|---|---|---|
| No change | ₹9,561,047 | ₹9,561,047 |
| +6,000 capacity at F004 | ₹14,512,146 | **₹9,561,047** |
| Demand +20% | ₹14,649,002 | **₹9,744,000** |

Solve time went **down**, to 0.1–0.5s.

---

## 6. Every scenario appeared to save the same 47%

Even with the solver fixed, three unrelated scenarios reported −47.1%, −46.8%
and −47.1%. That is not a bug in either number; it is a comparison between two
different questions.

The project baseline is deliberately an `ACTUAL_AS_IS_EVALUATION` — the client's
footprint pinned open, because that is the network they run and the figure they
recognise. A scenario is solved as a `BROWNFIELD_SCENARIO_OPTIMIZATION`, free to
close sites. So the gap between the columns is the change **plus the entire
value of redesigning the footprint**, and on this network the redesign dominates
it by a factor of 165.

Every scenario now carries a third reference: `reference_kpis`, the same network
**unchanged** but solved the way scenarios are. It is a zero-delta capacity
change run through the identical code path — so what it isolates is guaranteed
comparable rather than approximately so — and it is cached per snapshot.

| | Baseline (as run) | Re-optimised, no change | Close F004 |
|---|---|---|---|
| Business network cost | ₹18,067,794 | ₹9,561,047 | ₹7,988,958 |
| Unserved demand | 8,733 | 8,733 | 11,963 |

* Re-optimising the existing footprint: **−₹8,506,746**.
* Closing F004 on top of that: **−₹1,572,089**, at the price of stranding 3,230
  more units.

The comparison table gains a **"This change's own effect"** row, on by default,
and the recommendation panel now says:

> *"Capacity boost is the cheapest of the 3 compared, at ₹85.1L below your
> current network. **The change itself moves nothing.** All ₹85.1L of the
> difference comes from re-optimising the footprint you already have — the
> solver reaches the same plan with or without this change."*

A scenario that does nothing now reads as doing nothing.

---

## 7. Everything downstream of the scenario

**The recommendation panel** read five fields the mapper leaves null by design —
`scn.highlight`, `scn.description`, `scn.aiAssessment.recommendation` — so its
headline and paragraph rendered the string `undefined`, and its trade-off line
computed `undefined - undefined`. It also always described `multiSelectedIds[0]`
— the phantom `SCN_REBALANCE`, falling through to `SCENARIOS[1]` — so it never
changed when the user selected a different scenario or created a new one. It now
ranks the scenarios on screen, names the best, attributes the saving, and states
the service cost even when that is bad.

**The comparison table** offered thirteen metric rows and the mapper read six
KPIs out of a response carrying twenty. Transport cost, fixed facility cost,
handling cost, inventory cost, carbon and fill rate had no field to read and
rendered an em dash on every column. All are now mapped; unserved demand and
facilities-open are added. `inventoryDays` and `riskFactor` stay in the picker
marked "Not available", because no engine here computes them.

**The scenario drawer** read `scn.changes`, `scn.assumptions`,
`scn.robustnessTests` and `scn.objective` — four more null fields — and rendered
an empty section under the heading "Resilience Stress Testing (+15% Demand
Surge)", claiming a test that had not run. It now shows what was asked for, the
builder's own override strings, the corridors that moved with their volumes,
the cost decomposition on both sides, and the demand actually served.

**The capacity-risk drilldown** stated that "Delhi NCR DC (Baseline)" runs at
108% and the scenario brings it to 91%, that "Baddi → Delhi NCR" is the
network's highest-volume corridor, and that Kolkata DC has 41% headroom — for
whatever network was loaded. None of those facilities exist in a client upload
and none of those numbers came from a solve. It now reports this network's
facilities at their solved utilisation on both sides.

**Deleting a scenario** spliced the local array only, so a deleted scenario came
back on the next page load. There is now a `DELETE /api/scenarios/<id>`. The
delete button also lived only on the add-menu's list items, and that list was
replaced by "remove one to add another" as soon as three were compared — so
with three selected, nothing could be deleted at all. The list is always shown;
only *adding* is gated.

**The Digital Twin** drew every lane at its baseline weight, so a scenario that
empties a corridor looked identical to one that does not. A scenario map now
draws only the lanes the plan uses, marks a closed facility with a dashed grey
ring and a ⛔, marks a greenfield site with a NEW badge, draws the corridors into
a new site that exist in no uploaded lane list, and carries a caption saying
what changed:

> *"Close a DC. 3 facility sites closes (F001, F002, F006) · 12 corridors carry
> different volume"*

**A refused scenario was stored as a result.** `run_sync` never raises — it
captures every failure and returns it, which is right for a control plane and
wrong to treat as success. A scenario the builder refused came back HTTP 201
with a stored record whose every figure was null. It is now HTTP 422 with the
engine's own reason.

**A page refresh dropped a signed-in user onto the marketing page** with a valid
token and an active project id sitting in `localStorage` — nothing read them on
boot. Every scenario, KPI and map appeared to be gone. `restoreSession()`
verifies the token against `/api/auth/me` and re-opens the project.

**Home's "Savings opportunity" tile** keyed on `SCENARIOS.find(s => s.id ===
'SCN_REBALANCE')`, so it read "Not available" however many money-saving
scenarios existed. It now names the best solved one.

---

## 8. Validation

| Suite | Result |
|---|---|
| `validation/phase_10_5/run_scenario_ui_check.py` (new, browser) | **29 / 29** |
| `validation/phase_10_5/run_scenario_types_check.py` (new, API) | **15 / 15** |
| `validation/phase_10_4/run_scenario_check.py` | **11 / 11** |
| `validation/phase_10_3/run_ui_flow_check.py` | **25 / 25** |
| `validation/phase_10_3/run_empty_project_check.py` | **10 / 10** |
| `validation/phase_10_1/run_client_data_e2e.py` | **27 / 27** |
| Backend regression | **2,542 passed · 4 skipped** |
| Uncaught page errors | **0** |

The browser harness drives the page's own controls — open the modal, click a
type card, fill what it renders, press Run Scenario — once per scenario type, so
the whole path is exercised: form → service → API → MILP → mapper → `SCENARIOS`
→ table → recommendation → map. Six scenarios are created in one session and
three compared.

One existing check was rewritten rather than satisfied: `E-08` asserted the
literal string `"No solved scenarios"`, which was the single empty-state
sentence the page had. The page now distinguishes three empty states and says
which applies. The check follows the intent and additionally requires that no
currency figure appears — which it did not test before.

---

## 9. Still not done

Unchanged from Phase 10.4, and still disqualifying:

* **`/orchestrator/*` is unauthenticated.**
* **Persistence is in-process.** A restart loses accounts, projects, uploads and
  every solved scenario. There is no database.
* **The LLM call budget is 4 per gateway instance**, against a shared daily
  allowance.
* **No contract parser**; uploaded signals do not influence the forecast;
  capacity history reaches no engine.

New, and now visible rather than hidden:

* **Facility-level REI still reports a negative performance impact** for F004
  (−₹1,572,089) and F007 (−₹130,411). The absolute-gap fix did not change these,
  which settles the question: they are not a solver artefact. Closing those
  facilities genuinely lowers *business* cost while stranding more demand,
  because the shortage penalty is excluded from that measure by design. The
  engine logs it; nothing on screen explains it.
* **Scenario `reference_kpis` costs one extra solve per snapshot.** Cached, and
  ~0.4s on this network, but it is a real cost on a much larger one.
