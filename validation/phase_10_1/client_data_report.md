# Phase 10.1 — Ingesting Real Client Data

Source: `Dump/NetGravity_Test_Data_Clean.xlsx` — a single workbook of 8 sheets:
Facilities (8), Markets (7), Lanes (36), Products (2), Demand_History (504
rows over 36 months), Capacity (288 rows), Transportation_Rates (72), External
Signals (10).

The dataset is **normalised**, which is what a real client sends and what broke
the application:

* the Markets sheet carries **no demand column** — demand is a monthly history
  keyed by market and product;
* the Lanes sheet carries **no rate column** — freight is a separate table
  keyed by lane *and product*;
* capacity appears both as a static facility figure and as a monthly series.

---

## 1. Why the KPIs were blank

Three separate causes, in order of severity.

### 1a. The extractor invented the economics it could not parse

Every unmatched column fell through to a literal:

| Field | Column in the workbook | What was used instead |
|---|---|---|
| Facility capacity | `Capacity_Units` (42,150 / 38,480 / …) | **10,000** for every facility |
| Freight rate | `Rate_Per_Unit` in a separate table (11.16–38.72) | **₹10.00/unit** for every lane |
| Transit time | `Transit_Time_Days` (0.5–2.9) | **1.0 day** for every lane |
| Handling cost | not present | **₹4.00/unit** for every DC |
| Distance | `Distance_Km` | 300 km when unmatched |
| Coordinates | `Latitude`/`Longitude` | a hash of the row's id |
| Market name | `Market_Name` | the id, e.g. "M005" |

That is worse than a blank screen. A network built from those values solves
cleanly and reports a confident cost for economics the client never supplied.

Every one of these now reads the file, and **nothing is substituted** — a field
the workbook does not carry is `None`, and the reason is returned in
`structure.notes`.

### 1b. One sheet was read as several tables

`classify_sheet()` now assigns each sheet exactly one role. Previously every
sheet was tested against every branch, so the `Capacity` sheet (which shares
`Facility_ID` with `Facilities`) re-registered all eight facilities — and
because the duplicate check looked only at the DC set, the three plants were
added to the DC list as well. The network showed **3 plants and 8 DCs for a
workbook containing 3 plants and 5 DCs.**

### 1c. Demand was never found, so nothing could be assembled

With no demand column on the Markets sheet, `assemble_network_from_structure`
raised *"None of the recognised markets carried a demand quantity"*, no network
was bound, and every KPI endpoint answered `409 NO_NETWORK_BOUND`. The frontend
rendered that as blanks.

Current demand is now taken from the **latest period on record** in
`Demand_History` (2026-08), kept split by product, and the period is named in
the assumptions.

---

## 2. What the analysis actually found

With the real data parsed, the MILP returns **INFEASIBLE** — and that is a
correct answer, not a defect.

| Market | Demand (Aug 2026) | Only SLA-eligible lane | Lane capacity | Short |
|---|---|---|---|---|
| M001 Delhi | 9,433 | F004→M001, 0.5d ≤ 2d SLA | 3,230 | **6,203** |
| M005 Pune | 5,141 | F007→M005, 0.5d ≤ 1d SLA | 3,122 | **2,019** |
| M002 Mumbai | 5,569 | F007→M002, 0.6d ≤ 2d SLA | 5,058 | **511** |

Every other lane into those markets is too slow for the stated service level
(2.4–2.7 days against a 1–2 day SLA). Total unservable demand 8,733 units,
which matches the solver's own shortage quantity exactly when shortages are
permitted.

`diagnose_servability()` reports this **before** the solve runs, in words, per
market. It is arithmetic on the user's own numbers — demand versus the capacity
of the lanes that meet each market's SLA — not a second optimiser.

---

## 3. Fixes

### Backend

| Area | Change |
|---|---|
| Sheet classification | One role per sheet; time-series tables checked before the master tables they share an id column with |
| Value coercion | `_num`/`_text` return `None`, never a default |
| Coordinates | Explicit `Latitude`/`Longitude` win; the city lookup is a fallback and a hash-grid position is recorded as approximate |
| De-overlap fan | 0.6° (~65 km, enough to put the Mumbai plant offshore) → 0.12°, with `latSource`/`lngSource` preserving the uploaded value |
| Freight rates | Joined from `Transportation_Rates` by lane id, kept per product |
| Demand | From the latest period of `Demand_History`, split by product |
| Products | The `Products` sheet is honoured; the single-aggregate-product assumption applies only when the upload has no product dimension |
| Lane rates → solver | Collapsed to one demand-weighted rate per lane, and stated. The MILP keys arcs on `(origin, destination, mode, product)` and skips duplicates, so one lane per product would silently apply the first product's rate to all of them |
| Lane capacity | `Capacity_Units` on the Lanes sheet is carried into `LaneRecord.lane_capacity` — it was previously dropped, hiding the binding constraint |
| Fixed cost | Annualised ×12 with the interpretation stated (it was ×1000×12, twelve-thousand-fold, from an assumption about a normalisation the extractor no longer does) |
| Servability | New `diagnose_servability()`; findings surface in the commit response |
| Demand history | New `demand_history_store`; `app.py`'s `history_provider` consults it before the staging zone — the forecasting engine was complete and simply never received an uploaded network's history |
| Forecast API | Returns the observed history each series was built from |
| Structure API | New `GET /api/network/structure` — the bound network's nodes and lanes, which exist whether or not a solve succeeds |
| KPI registry | A solve that *proved* infeasibility now reports `INFEASIBLE` with the solver's reason, instead of `INSUFFICIENT_EVIDENCE`. "No solution exists" and "we could not find out" are different answers |

### Frontend

| Defect | Fix |
|---|---|
| `bootApp()` ran during module evaluation | Deferred to a microtask. As a deferred module script the DOM is usually already ready, so `bootApp()` was called on line ~90 while the module body was still executing — every `const` below it was in the temporal dead zone, and Home's attention feed died with `Cannot access 'ATTENTION_CATEGORY_META' before initialization` |
| Digital Twin read its node list from `/api/kpis/facilities` | Reads `/api/network/structure`. Facility KPIs are solver output and correctly empty when infeasible; the twin was rendering an empty map for a network whose 15 nodes were in the snapshot |
| `undefined%` on every DC row and in the 3D tooltip | Absent utilisation renders "—" / "Not solved" |
| 3D scene failed to render | DC radius was `1.3 + (null/100)*0.7` = NaN, which propagated into the cylinder geometry and broke `computeBoundingSphere` |
| Home KPI strip showed `₹12.8L` and `null%` | The prototype's demo base case survived when no solve succeeded; hydration now clears it explicitly. Fill rate renders "—" |
| Facility dashboard threw on `kpis.totalCost.value` | `data.js` and `hydrate.js` were writing two different shapes into `FACILITY_KPIS`; reconciled to one |
| Every facility got a fabricated performance profile at parse time | `loadNetworkData` seeded utilisation 75%, throughput 80% of capacity, cost/unit ₹4.2, SLA 96.5%, storage ₹45,000, transport ₹320,000, lane volume 1,500 at 97.2% on-time, and deltas of "+4.2%"/"+2.1%"/"-0.3%"/"+1.5%" — all visible before any solve, and all surviving an infeasible one. Now null until the engine produces them |
| Scenario screens threw on an empty `SCENARIOS` | Comparison table, recommendation card, metric drilldown and scenario map each report they have nothing to compare |
| Forecast chart threw `RangeError: Invalid array length` | `new Array(months.length - 1)` is `new Array(-1)` for an empty series; an empty state is rendered instead |
| Forecast chart y-axis pinned at `min: 6000` | The prototype's own demand range. The client's history (3,972–5,862) was clipped entirely off the bottom — only the forecast tail was visible |
| Forecast screen was never connected to the engine | Drew 24 months of prototype "North India" demand and a hardcoded cone for every network. Now the real series, its p10–p90 band, and its own accuracy |
| Forecast Summary card was static demo HTML | Model "Enhanced Demand Forecast", growth "+14.2%", breach facility "Delhi NCR DC", breach month "December 2026", projected utilisation "108%" — none from a forecast run. Replaced with what the engine reports; the breach fields are gone because nothing in this build projects one |
| "Capacity threshold" line | Was the prototype's Baddi DC capacity (10,000 u/d) drawn over every network. Omitted unless a threshold is known |
| External Signals card | Showed the prototype's own signals; now the 10 signals from the upload, with "Not available" for fields the workbook lacks and no claim that they influenced the forecast |
| Banner said "Analysis complete" after an infeasible solve | States that no feasible plan exists, and why, per market |
| Attention feed empty state | "network is performing within target" asserted a clean bill of health from an absence of insights |

---

## 4. Is the forecast correct?

Yes, and it is now the forecast that reaches the screen.

For M001/P001 — 36 observed months, 3,972 → 5,862, linear trend +57.7/month:

| | |
|---|---|
| Engine | `QuantileRegression_HiGHS`, `llm_used: false` |
| Routed through | orchestrator capability `forecast.demand` |
| Horizon 1–3 | 5,946 · 5,990 · 6,224 |
| Naive linear extrapolation | ≈6,199 by horizon 6 — the forecast continues level and trend |
| MASE | **0.488** (below 1 — better than a naive seasonal forecast) |
| WAPE | 1.57% |
| Validation | `ROLLING_ORIGIN`, 3 folds, 36 observations |
| Bands | p10/p50/p90 from the engine's own quantiles |

Not every series scores well, and that is reported rather than smoothed:
M001/P002 has MASE **1.438**, worse than naive. 14 of 14 market-product pairs
produce a series.

---

## 5. Validation

| Suite | Result |
|---|---|
| `validation/phase_10_1/run_client_data_e2e.py` | **20 / 20** — real HTTP, real workbook |
| `netgravity/tests/test_normalised_upload.py` | **20 / 20** — new |
| `validation/phase_10_0/run_prototype_e2e.py` | **16 / 16** |
| Backend regression | **2,501 passed · 4 skipped** |
| Browser page errors on the client journey | **0** |

The remaining console entries are expected protocol responses — one `401` from
the pre-authentication session check and two `409 NO_NETWORK_BOUND` from KPI
calls issued before the commit — plus a software-WebGL shader warning from
headless Chromium.

---

## 6. UI parity note

`run_ui_parity.py` moves from 831/13 to **828/16** against the approved
standalone. All three new differences come from the Forecast screen, and each
is a deliberate removal of fabricated content:

* `.fp-stat-value` width — the first summary row reads "—" before the engine
  answers, where the standalone hardcodes "Enhanced Demand Forecast";
* `.tag-danger` / `.tag-muted` counts — the red "Breach Projected Dec'26" badge
  is now muted, because nothing in this build computes a capacity breach.

Layout, card structure and classes are unchanged; only values differ, and they
now come from the engine. `#home-kpi-grid` also narrows (778px → 760px) as the
cost tile's provenance label changes.

---

## 7. Still not wired

* **Uploaded signals do not influence the forecast.** They are parsed, stored
  and displayed, and the card says so. The orchestrator's signal-routing path
  exists and is unconnected to uploaded signals.
* **Capacity history is parsed but unused.** 288 rows of monthly available/used
  capacity are carried through `structure.capacityHistory` and reach no engine;
  utilisation still comes only from the solve.
* **No scenario is generated automatically**, so the Scenario screens are empty
  for a fresh upload until the user creates one.
* **Persistence is in-process** — a restart loses accounts, projects, uploaded
  history and signals.
* `/orchestrator/*` remains unauthenticated.
