# Phase 10.2 — Why the KPIs were empty, and what now fills them

Source: `Dump/NetGravity_Test_Data_Clean.xlsx`, the same workbook as Phase 10.1.

Two separate problems, and they compounded. Columns that *were* in the file
never reached the model, and the model that used what did reach it proved
infeasible — at which point the KPI layer correctly reported thirteen metrics as
`INFEASIBLE` with no value. Correct, and useless: the screens went blank.

---

## 1. Data points in the file that reached nothing

| Column | In the workbook | What the model used | Consequence |
|---|---|---|---|
| `Fixed_Cost` on the three plants | 3,852,000 / 3,624,500 / 3,211,800 per month | **0** | Fixed cost understated by ₹10.7M/month — more than the ₹5.2M the network was reporting in total. The extractor read the column for DCs only. |
| `Status` (all `ACTIVE`) | 8 live facilities | every facility `CANDIDATE` | The optimiser was being asked to *design* a network from eight candidate sites, not evaluate the client's. It answered by closing three of them, including two of the three plants. |
| `Unit_Weight_Kg` | 0.42 and 1.05 | engine default **1.0** | Carbon is computed in tonne-kilometres, so P001's emissions were overstated by 2.4×. |
| `Unit_Cost` | 78.50 and 118.00 | default **0.0** | `unit_value` is what holding cost is a percentage *of*. At zero, the entire inventory term evaluated to ₹0 on a network carrying real goods. |
| `Demand_History` variability | 36 monthly observations per pair | `std_dev` = **0.0** | Safety stock is sized from σ. At zero there is none, so inventory cost was cycle stock alone. |
| `Available_/Used_Capacity_Units` | 288 rows | parsed, stored nowhere | The client's own recorded utilisation existed and was discarded. |

Each is now read. Where a figure genuinely is not in the file — handling cost,
production cost — it stays `None` and the assumption is stated, not defaulted.

Two smaller ones, from the same class of defect:

* **Market priority** was assigned by a hardcoded rule: `"High" if demand >
  2500 else "Medium"`. That threshold appears in no upload and means nothing to
  a network measured in pallets or tonnes. Priority is now read from the file
  or left unset; this workbook has no priority column, so the column reads "—".
* **Freight-rate currency** was ignored. A rates table quoting two currencies
  would have been summed as though they were one. It is now reported. (This
  workbook is entirely INR, so nothing changes for it.)

### What changed in the answer

With the economics corrected, the optimiser's own decision changes:

| | Before | After |
|---|---|---|
| Facilities open | 7 of 8 | 8 of 8 (as-is baseline) |
| Facility fixed cost | ₹5,200,000 | ₹17,201,200 |
| Inventory cost | ₹0 | ₹3,531 |
| Total carbon | 1,415.80 kg | 756.47 kg |

---

## 2. The baseline was a redesign, not the client's network

`OptimizationConfig` defaults to `BROWNFIELD_SCENARIO_OPTIMIZATION`, which
treats every facility as an open/close decision. So the panel labelled "current
network" was costing a network with three of the client's eight sites shut:
₹9.6M/month against the ₹18.1M/month they actually run.

A network assembled from an upload is now solved in
`ACTUAL_AS_IS_EVALUATION`, which pins the existing footprint open and marks the
result `is_hypothetical=False`. That mode's own documented caveat is carried
into the assumptions rather than hidden: the upload contains no observed
shipment volumes, so flow *across* that fixed footprint is the cost-minimal
allocation, not a replay of what shipped.

The redesign is a real and valuable finding — closing three sites saves
₹8.5M/month on this data — but it is a scenario, not a baseline.

---

## 3. The KPI blackout

The strict model is genuinely infeasible on this workbook: three markets cannot
be reached within their own service levels (Phase 10.1 §2). `SolverInfeasibleError`
propagated, no `NetworkStateResult` existed, and every KPI came back
`INFEASIBLE` with `value: null`.

That answer is true and it is not enough. A planner facing an unservable
network still needs to know what the servable part costs, which sites carry it,
and exactly how much demand is stranded.

### The relaxation

`OptimizationConfig.relax_to_shortage_when_infeasible` (default **False**) lets
the engine re-solve once with `allow_shortage=True` when — and only when — the
strict model has proved infeasible. Same costs, same capacities, **same service
levels**; one variable per demand record for the units that cannot be served,
priced so the solver has to choose which demand to strand.

It is not a fabrication and it is not a fallback to a plausible number:

* it does **not** relax the client's SLAs, invent capacity, or fill a gap;
* the stranded volume comes back as `unserved_demand` (8,733) and depresses
  `demand_fill_rate` to 0.7639;
* `NetworkStateResult.solve_relaxation` records that the strict solve was
  infeasible, and every KPI built from that state carries the same note in its
  `metadata`, so nothing downstream can mistake it for a fully-served plan;
* it is off by default, so a caller asking for a fully-served plan still gets
  the infeasible answer. It is switched on by the assembler, for uploaded
  networks only.

The banner on Home says so in words: *"Analysis complete, but your network
cannot serve all of its demand within its own service levels. The figures below
are the best achievable plan; 8,733 units are left unserved."*

### The shortage penalty is not a cost

At ₹1,000,000/unit the penalty reaches ₹8.73bn — it is the solver's device for
ranking which demand to strand, not a price anyone pays. It is excluded from
`business_network_cost` (which is why that reads ₹18.07M, not ₹8.75bn), reported
separately as `shortage_penalty_cost`, carries a `notional` warning in its
metadata, and has been removed from the frontend's cost breakdown, where it was
being written into the `unmetPenalty` line.

---

## 4. KPIs that were computed and never exposed

| Added | Why it was empty |
|---|---|
| `facility_cost`, `transport_cost`, `handling_cost`, `inventory_cost`, `carbon_cost`, `opening_cost`, `closure_cost` | Every solve produced these on `CostBreakdown`; the registry exposed only the total. The dashboard's cost breakdown had nothing to read and rendered blank beneath a populated total. They sum to `business_network_cost` exactly. |
| `capacity_units` per facility | A caller could read a utilisation percentage but not the capacity it was a percentage of. |
| `GET /api/kpis/flows` | The MILP decides how much moves down every lane and what it costs, and `NetworkStateResult.flows` has carried both since the contract was written — with no HTTP surface. Every corridor showed a null volume beside a freight rate the upload supplied. 15 of 36 lanes carry flow in the optimal plan; their transport costs sum to the network's `transport_cost` exactly. |
| Recorded utilisation on `/api/network/structure` | 288 rows of the client's own capacity history reached no consumer. Now served with the structure and shown as the "previous" figure on each facility row, labelled with the period it was recorded in. |

Network KPIs go from **13, all null** to **25, all valued**.

---

## 5. A finding the recorded utilisation exposes

Now that both numbers are visible, they disagree:

| | F001 | F004 | F006 | F007 |
|---|---|---|---|---|
| Recorded (2026-08, client's file) | 76.7% | 71.6% | 98.5% | 80.5% |
| Modelled (this solve) | 23.2% | 11.4% | 19.7% | 43.1% |

The uploaded demand totals 36,982 units against 235,910 units of facility
capacity, while the capacity sheet records 185,000-odd units in use. The two
products in `Demand_History` therefore do not account for most of the volume
these sites actually handle. Nothing in this build can reconcile that, and it
is not treated as a defect in either number — it is reported, because a
utilisation of 11% on a site the client records at 72% is the kind of gap a
planner needs to see rather than an average of.

---

## 6. Frontend

| Defect | Fix |
|---|---|
| The cost breakdown showed the ₹8.73bn notional penalty as an `unmetPenalty` line item, beneath a total that excluded it | Line removed; the shortfall is reported as a quantity of demand, which is what it is |
| "Analysis complete" was shown for a plan that strands 23.6% of demand | A third banner state, in amber, naming the units unserved and the markets that cause it |
| The Home tile labelled **Fill Rate** was averaging the per-facility `sla` rows, which hydration writes from `pct_demand_in_sla` | Reads `demand_fill_rate`. The two coincide only while every servable unit is also inside its service level |
| `LANES[].flow` still held the prototype's own corridor volumes | Written from `/api/kpis/flows`; explicitly null, not zero, on a lane the solve does not use |
| Facility lane tables showed a null volume and cost beside a real rate | Filled from the same source |
| The "vs previous" column on every facility read "—" | Compares against the client's own recorded utilisation, labelled `recorded 2026-08` |
| Home's forecast sentence was the literal string *"I forecast North India demand to increase 14% over the next 3 months."* | Derived from the series the engine returned. (The element it writes to is absent from the current markup — the string was dead, but it was still in the shipped bundle) |

---

## 7. A bug this work introduced and fixed

Stamping the relaxation onto `NetworkStateResult.metadata` replaced a typed
`ModelMetadata` with a plain dict, and Digital Twin construction reads that
object attribute by attribute — `AttributeError: 'dict' object has no attribute
'run_id'`, caught by driving the real UI. The marker now lives in a declared
`solve_relaxation` field of its own.

---

## 8. Validation

| Suite | Result |
|---|---|
| `validation/phase_10_1/run_client_data_e2e.py` | **27 / 27** (was 20; 6 new checks, 1 rewritten) |
| `netgravity/tests/test_infeasible_relaxation.py` | **13 / 13** — new |
| `netgravity/tests/test_normalised_upload.py` | **28 / 28** (was 20) |
| Backend regression | **2,542 passed · 4 skipped** (was 2,521) |
| `validation/phase_10_0/run_ui_parity.py` | **828 / 16**, unchanged from Phase 10.1 |
| Browser page errors on the client journey | **0** |

`C-17c` previously asserted that facility KPIs stayed **empty**, because the
strict solve produced no flows and inventing a utilisation would have been a
fabrication. That premise no longer holds — the relaxed plan has real flows — so
the check was not dropped but tightened: every facility must report a
utilisation, and it must equal its throughput over the capacity the upload
stated.

`test_no_engine_imports_the_twin` failed on a comment of mine that used the word
"twin" inside `orchestrator/engines/`. The architectural guard is crude but
deliberate, so the comment was reworded rather than the test relaxed.

---

## 9. Still not wired

Unchanged from Phase 10.1 except where noted:

* **Uploaded signals do not influence the forecast.** Parsed, stored, displayed,
  and the card says so.
* **Capacity history now reaches the screen** as the recorded prior, but still
  feeds no engine — it does not constrain the solve or the forecast.
* **No scenario is generated automatically**, so the Scenario screens stay empty
  for a fresh upload. The as-is/optimised comparison in §2 is a scenario the app
  can now solve but does not create on its own.
* **Per-facility total cost and inventory days remain null.** Attributing
  network cost to a facility needs an allocation policy (how is inbound freight
  split?) that no engine here owns, and inventing one would be the same class of
  defect as the constants this phase removed.
* **Persistence is in-process** — a restart loses accounts, projects, uploaded
  history, signals and capacity history.
* `/orchestrator/*` remains unauthenticated.
