# Phase 10.3 — The four screens, and what they were showing

Phases 10.1 and 10.2 fixed the parsing and the KPI layer, and verified both
over HTTP. Every defect in this phase was invisible to that: the API was
returning the right answer and the screen was showing something else.

Driven end to end with `Dump/NetGravity_Test_Data_Clean.xlsx` in a real browser
(`validation/phase_10_3/run_ui_flow_check.py`, 25 checks, all against rendered
DOM).

---

## 1. The upload / mapping-review screen

### 1a. The mapping table was a fixture

`ingestion.js` said so at the top: *"All extraction content is mocked — there is
no real parsing."* `baseMappingRows()` returned nine invented rows —
`customer_id`, `origin_dc`, `destination_market`, `amt_rs`, `misc_ref` — with
invented samples (`C1021, C2044, C3198`), under invented stats
(`48 detected, 42 auto-mapped, 4 need review, 2 ignored`), identical for every
file anyone had ever uploaded.

The parser had been returning the real 51 columns since Phase 10.0. The screen
just did not use them for anything except a fallback that never fired.

### 1b. The column classifier described a different program

`classify_column_name()` was a separate vocabulary mapping to nine prototype
schema fields. On the sample workbook it reported **42 of 51 columns as "No
match found"** — including `Latitude`, `Capacity_Units`, `Fixed_Cost`,
`Rate_Per_Unit`, `Demand_Units` and `Service_SLA_Days`, every one of which the
extractor reads correctly. The nine it did match were mostly mislabelled
(`Facility_Type` → "Distribution Centre", `City` → "Region / Zone").

Two causes:

* **It was a parallel schema.** The extractor decides what a column is from the
  alias tuples `FACILITY_ID_COLS`, `CAPACITY_COLS`, `RATE_COLS` and the rest.
  The review screen asked a different function, which disagreed.
* **Its regexes could not match snake_case.** `\bcost\b` cannot match inside
  `unit_cost`, because `_` is a word character — so `Unit_Cost`, `Rate_Per_Unit`
  and `Total_Cost` all fell through to "No match found".

`classify_column_name(col, sheet_role)` is now derived from those same alias
tuples and takes the sheet's role from `classify_sheet()`, so the answer is the
one the extractor will act on:

| | Before | After |
|---|---|---|
| Columns recognised | 9 / 51 (7 mislabelled) | **50 / 51** |
| `Capacity_Units` | "No match found" | "Facility capacity" on Facilities, **"Lane capacity"** on Lanes |
| `Rate_Per_Unit` | "No match found" | "Freight rate" |
| `Unit_Cost` | "No match found" | "Unit value" |
| Genuinely unused | — | `Product_Category`, and it says so |

The dropdown's options are now served with the mapping (`schemaFields`) from
the same table that produced the suggestion. A `<select>` whose value is not
among its options falls back silently to the **first** one, so every row of a
real workbook had been rendering as "Customer ID" regardless of what the server
said.

Columns are grouped by sheet, with what the parser decided each sheet *is*
("Read as: freight rates", "5/5 columns used"), so a misread sheet can be
caught here rather than after the solve.

### 1c. Text ran off the screen

`.ing-map-sample` set `max-width` on a `<span>` left at `display: inline` — and
an inline box ignores `max-width`. The signals sheet joins three ~90-character
descriptions into one sample cell, which stretched the table past the viewport
and carried the mapping dropdowns and the Confirm button off the right edge
with no way to reach them.

Now `display: block`, wrapping (`overflow-wrap: anywhere`), a fixed table
layout with declared column widths, and `min-width: 0` on the grid item — a
grid child defaults to `min-width: auto`, which is what let its content force
the column wider than `1fr` in the first place.

Verified rather than eyeballed: the check asserts `documentElement.scrollWidth
<= clientWidth` and that no element's right edge exceeds the viewport.

### 1d. The file size read "0.0 MB"

`(bytes / 1024 / 1024).toFixed(1)` on a 33 KB workbook. Now **32.9 KB**, with
the unit chosen to suit the size.

### 1e. Other fabrications on that screen

| Was | Now |
|---|---|
| "Rows analyzed 12,655" — `6000 + hash(fileName) % 9000` | **927**, counted by the parser |
| "99% of fields are typically accepted as-is" | share of *this file's* columns the model reads |
| "Filters (1)" — a count with no filter | removed |
| Data Quality: 4,820 records, 98.4% valid, 8 issues naming `DC_GUWAHATI`, `MKT_LUCKNOW`, `PLT_BADDI→DC_GUWAHATI` | measured on the uploaded file; `DATA_QUALITY` now starts empty so an unparsed file cannot show somebody else's clean bill of health |
| A parse failure was swallowed (`console.warn`) and the fixture shown instead | named on screen, and Confirm is disabled — a file with no readable columns cannot be carried into the network build |

### 1f. The PDF path

There is no contract parser in this build. The screen nonetheless said *"AI is
understanding your document"* and *"What I've found so far"* over
`CONTRACT_DEMO.vendorA` — TransCorp Logistics, ₹10/kg base rate, ₹2/kg fuel
surcharge — presented as terms extracted from the user's file, with a page
count derived from the file name.

Rewritten to say what is true: the file is stored, not parsed; the terms are a
worked example of the output format; none of them reach the optimiser. Freight
rates come from the spreadsheet.

---

## 2. Digital Twin — the 2D map drew nothing

`initMap()` created the Leaflet map, added a legend, and stopped. The only
thing that plots nodes and corridors is `renderNetwork()`, reached solely
through `setNetworkState()` — which is bound to nothing but a click on the
network-state toggle. Opening **Digital Twin → 2D** therefore showed an empty
basemap for a fully loaded network until the user happened to click a toggle
they had no reason to touch.

The one refresh path that existed made it worse: on `networkDataLoaded` it
called `initMap('home-map')` and `initMap('twin-map')`. **Neither id exists** —
the 2D container is `map-twin` — so `initMap` returned null and a map built
before the upload kept the demo network for the rest of the session.

* `initMap` now draws the current state immediately and frames the view on the
  loaded network (`fitToNetwork`), instead of a fixed India-wide centre.
* New `refreshAllMaps()` redraws every mounted map when a network loads.
* Two hardcoded overrides removed: `renderNetwork` reassigned utilisation and
  throughput for `DC_DELHI` (91%, 8,200) and `DC_KOLKATA` (64%, 3,840) whenever
  the "recommended" state was selected — figures no engine produced, painted
  over whatever network was loaded.
* `renderScenarioFlowMap()` deleted: a hand-drawn SVG of Baddi / Delhi NCR /
  Mumbai / Kolkata / Chennai with arcs colour-coded "Increase / Decrease / No
  Change". It took `containerId` and `activeScenarioId` and used neither.
  Nothing imported it, so it drew for nobody — but an exported function that
  fabricates a network diagram is one call away from doing so.

**51 markers now render on entering the tab**, with no toggle click.

---

## 3. KPI screen — blank, then wrong, then invented

Three separate defects stacked.

**It returned early.** `renderFacilityDashboard()` opened with
`const fac = getFacilityById(state.selectedFacility); if (!fac) return;` and
`state.selectedFacility` defaults to `'DC_DELHI'`. For any network without a
facility by that name — every real one — the entire screen rendered nothing: no
error, no empty state. It now falls back to a facility that exists, and shows a
stated empty state only when the network genuinely has none.

**It read roles from the spelling of an id.** `state.selectedFacility.startsWith('DC_')`
appeared in five places. The client's DCs are `F004`–`F008`, so every one of
them failed the test: labelled "Manufacturing Plant", routed through the plant
branch for utilisation, and skipped the DC-only cards. `facilityRole(id)` now
reads the arrays the network was actually loaded into.

**Half its cards were literals.** These rendered identically for every facility
of every network:

| Card | Was | Now |
|---|---|---|
| SLA | "↑ 1.8% vs last period" | no prior solve exists, and it says so |
| Operating cost | "↓ 3.2% vs budget" | no budget is loaded anywhere in this build |
| Inventory | "Holding Value ₹3.4L avg", "Safety buffer 140%" | "—" |
| Transit lead time | "1.2 days · Fastest 0.3d · Slowest 3.5d" | computed from this facility's own lanes (F004: 0.5–2.7d across 4 lanes) |
| Carbon | "0.42 kg CO₂e/u · 14.8t CO₂e/mo · ↓2.1% YoY" | summed from the solver's per-lane carbon on connected corridors, labelled as that |
| Corridor summary | "on-time transit confidence of 98.2%" | the transit range those corridors actually carry |

Also removed: a "Forecast Dec 2026 — 10,800 units/day" panel pinned to
`facilityId === 'DC_DELHI'`.

---

## 4. Chatbot — reading a field the API does not return

Two bugs, either of which alone was fatal.

**Wrong field.** `/orchestrator/chat` returns its answer as `reply`. The client
read `res.response`, which the endpoint has never emitted — so every successful
answer fell through to the "did not return an answer" branch. The assistant
appeared to fetch nothing while the orchestrator was answering correctly.

**Wrong network.** No caller ever passed `network_snapshot_id`, so the
orchestrator answered from the network it boots with. Replies named
`DC_CENTRAL`, `DC_EAST`, `DC_NORTH_NEW` — facilities the user has never seen.
The snapshot is now recorded by hydration in `project-context` and sent by
default.

Also: `intent: "UNKNOWN"` was rendered under the badge "ORCHESTRATOR RESPONSE",
which made a refusal look like an answer; and replies are now HTML-escaped,
since they carry facility names and free text that came from an uploaded file
into an `innerHTML` sink.

The assistant now answers *"a business network cost of 18,067,793.96 per
period… 8,733 units unserved"* — the user's own figures.

---

## 5. Also fixed

`_describe_parse_failure` in the reasoning agent classified "empty body with
`output_tokens=1984`" as *"a gateway problem"*. The gateway's cap is 2,000: a
reasoning model bills its internal reasoning to the same allowance and can
consume the whole budget while emitting nothing visible. That is a
prompt-length problem, and the message now says so. (The reasoning layer
degrades to deterministic text either way, which is what the chat reply above
is.)

---

## 6. Validation

| Suite | Result |
|---|---|
| `validation/phase_10_3/run_ui_flow_check.py` | **25 / 25** — browser, rendered DOM |
| `validation/phase_10_1/run_client_data_e2e.py` | **27 / 27** |
| Backend regression | **2,542 passed · 4 skipped** |
| Uncaught page errors on the journey | **0** |

The flow check asserts on what is drawn, not on JSON: 51 mapping rows carrying
the workbook's own column names, `Capacity_Units` resolving differently on two
sheets, no element exceeding the viewport, 51 map markers, the facility
selector offering `F004`–`F008`, `F004` recognised as a DC, and the assistant's
answer containing the user's cost rather than the demo network's.

---

## 7. Not done — this is not yet production-ready

Tests passing is not the same claim. What still blocks it:

* **`/orchestrator/*` is unauthenticated.** The chat endpoint this phase wired
  up serves network figures to an unauthenticated caller. It is the largest
  remaining gap and was left alone here because closing it changes the contract
  for the orchestrator's own test suite; it needs its own change, not a rider
  on a UI fix.
* **Persistence is in-process.** A restart loses accounts, projects, uploaded
  history, signals and capacity history. There is no database.
* **No contract parser.** The PDF path stores the file and says so.
* **Uploaded signals do not influence the forecast**, and capacity history
  reaches no engine.
* **No scenario is generated automatically**, so the Scenario screens stay
  empty for a fresh upload until the user creates one. The as-is/optimised
  comparison worth ₹8.5M/month on this data is a scenario the app can solve but
  does not offer.
* **Per-facility cost and inventory days are unattributed** — splitting network
  cost across facilities needs an allocation policy no engine here owns.
* The demo network in `data.js` is still the seed state before an upload
  replaces it. It is the model's starting value, not a fallback the screens
  reach for, but it is the reason a stale render can still show Baddi.
