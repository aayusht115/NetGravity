# Phase 10.7 — PostgreSQL, greenfield siting, the loading screen, and values from nowhere

What was asked:

> Move to postgresql, do as required, we need to preserve the uploaded data and
> the analysis for a project.
> Investigate scenario planner. Issue — Unable to create a scenario where a new
> facility is opened. Inspect and fix.
> Delay the loading screen, it should load until the all the KPIs have been
> calculated.
> Do not keep any hardcoded values, it should be dynamic and should reflect only
> what has been fed to the system by the user.
> Investigate any other issue and fix.

---

## 1. "Unable to create a scenario where a new facility is opened"

The scenario was created every time. The API returned 201, the site appeared in
the solve with its stated capacity, the map drew it with a NEW badge — and it
carried **zero units**, was never opened, and the scenario's cost came back
identical to a no-change reference, to four decimal places.

Two independent causes, either of which alone was sufficient.

### 1a. Every greenfield DC was pinned to zero throughput

`ScenarioBuilder._add_facility` set

```python
production_capacity_units_per_period=(capacity if role is NodeRole.PLANT else 0.0)
```

reading "a DC produces nothing". But the field does not mean that.
`FacilityRecord` documents it as a **PLANT/SUPPLIER supply-side limit**, and the
schema's own default for it is `1e12` — every ingested DC carries `1e12`. The
MILP's capacity constraint took the smaller of the two limits:

```python
eff_cap = min(fac.capacity_units_per_period,
              fac.production_capacity_units_per_period)   #  min(100000, 0) = 0
prob += outbound_sum <= eff_cap * y[fac.id]
```

`min(100000, 0)` is 0. The new DC could ship nothing, and `unpin_zero_thru`
then forced its binary to 0. Nothing anywhere reported this: the facility still
appeared in every KPI with its full stated capacity.

The decisive test: a **free** DC with 100,000 units of capacity placed exactly
on top of an unserved market. 8,733 units of that network's demand were going
unserved at a shortage penalty of ₹1,000,000 each — ₹8.7bn in the objective —
and the solver left the free DC shut and carried ₹0 of flow across it. That is
not an economic answer; it is a facility the model could not use.

Two fixes:

* the builder no longer overrides the field for a non-plant;
* the MILP now uses `fac.effective_supply_capacity`, the accessor that already
  encodes "this limit applies to plants and suppliers". A DC whose production
  capacity is zero — which is the literal truth about a DC, and what
  `modes.py` and `rei.py` both set deliberately — is no longer silently
  converted into a facility that may ship nothing. The same correction was
  applied to `diagnostics/infeasibility.py`, which was blaming capacity for
  infeasibilities that had another cause.

### 1b. Freight to a new site was priced 4–6× the client's own tariff

`_auto_connect_facility` priced every lane to and from a new site at a single
rate per kilometre, estimated as the **mean of each lane's rate ÷ distance**:

```python
base_rate_per_km = sum(l.rate_per_unit / l.distance_km for l in road_lanes) / len(road_lanes)
```

That estimator is dominated by the shortest lanes in the network. On the client
workbook a 9 km last-mile leg at ₹9.04 implies ₹0.96/km while a 2,214 km trunk
leg at ₹39.92 implies ₹0.018/km — a 53× spread over the *same* tariff.
Averaging the ratios lands near the short end and then multiplies it by a long
distance.

Measured against the client's own 36 road lanes:

| estimator | median error reproducing their own lanes | worst |
|---|---|---|
| mean of ratios (shipped) | **419%** | 730% |
| ratio of totals | 27% | 97% |
| affine fit (now) | **8.2%** | 33% |

A 1,575 km haul the client prices at **₹34.17** was quoted at **₹216.86**.

Transit had the same problem in a smaller way: `lead_time_days = distance / 500`
— a constant with no relation to the network. Their own lanes imply 19–671
km/day.

`netgravity/scenarios/tariff.py` now fits the affine structure real freight has,
from the network's own lanes, per mode:

```
rate_per_unit  = fixed_leg_cost + rate_per_km    * distance_km
lead_time_days = terminal_time  + distance_km    / speed_km_per_day
```

On the client network: `₹11.36/unit + ₹0.01290/unit-km` (8.2% median error) and
`0.56 d + 713 km/day` (9.5% median error). Each derived lane carries its
`rate_per_km`, `fixed_leg_cost`, `speed_km_per_day` and `terminal_time_days`, so
a later relocation re-prices from the same structure rather than from the ratio
of one derived pair.

Where the network cannot support a fit — fewer than three comparable lanes, or
no spread of distances — the scenario is **refused with the reason**. The
previous code fell back to a hardcoded `0.025` per km and to `100.0` km for an
unmeasurable haul: made-up numbers in the client's own currency.

### What it does now

| | before | after |
|---|---|---|
| Free 100k DC on the unserved market | throughput 0, closed | throughput **27,549**, open |
| Realistic Nagpur DC (20k units, ₹25m/yr) | closed, no effect | open; fill rate **76.39% → 94.54%** |
| Through the Create Scenario modal | `changeEffect` exactly **0** | **−₹2,363,161.93** against the re-optimised reference |
| A site priced at ₹5bn/yr | closed | still closed — the solver decides |

The last row matters: the fix must not make every proposed site open.

---

## 2. PostgreSQL

`NETGRAVITY_DATABASE_URL` (or `DATABASE_URL`) selects PostgreSQL. Without it the
application falls back to SQLite and **says so** on `/api/status`
(`"engine": "sqlite"`), because "is my work being kept, and where" should not
have to be inferred from a log line. A configured URL that cannot be reached is
a **hard failure**, not a silent fallback — a process that runs, looks healthy
and writes a user's work to an unbacked local file is the worst of the three
outcomes.

Documents are stored as `TEXT`, not `JSONB`, deliberately. JSONB is a normalised
representation: it reorders keys and re-renders numbers through `numeric`. This
store's contract is that an uploaded network comes back exactly as it went in,
and the check below asserts it byte-for-byte. The columns that are actually
queried are real columns with real indexes.

### Preserving the uploaded data *and the analysis*

Uploads were already persisted. The **analysis** was not: it was recomputed from
scratch on every request that touched it. Five KPI endpoints each called the
orchestrator independently, and the only thing between them and a fresh MILP
solve was a 120-second in-process cache written *after* the solve returned — so
opening a project fired five concurrent solves of the same network, and two
minutes later fired five more.

Nothing about that computation is time-varying. A snapshot is immutable by
construction, the solver is deterministic, and the KPI layer is a pure function
of the execution it reads. Worse, where a MILP has ties, recomputing can produce
a *different* optimum of the same cost — so a user paging between screens could
watch facility assignments change underneath them.

`analyses` now holds the complete authoritative reading of one snapshot — network
KPIs, per-facility metrics, resilience, risk, lane flows, triggered thresholds
and the evidence package — keyed by `(snapshot_id, data_version)`. There is no
TTL, because time is not what makes it stale; a different network is. Concurrent
first requests share one solve.

### Migration

`scripts/migrate_to_postgres.py` is idempotent (every insert is an upsert),
non-destructive (the SQLite file is opened read-only), and **verifies**: after
copying it re-reads every document from Postgres and compares it byte-for-byte
against the source. A migration that reports success without reading the data
back has only established that the writes did not raise.

The existing store on this machine — 54 accounts, 64 sessions, 17 projects, 2
network snapshots, 26 scenario networks, 18 solved scenarios, 18 history/signal
records, **199 rows** — was migrated and verified.

---

## 3. The loading screen

`enterApp()` revealed the dashboard immediately and started hydration
*afterwards*. Opening a project showed a complete, fully drawn screen of dashes
and zeroes until the figures arrived, then snapped into numbers. Nothing said
the screen in front of the user was not yet the answer.

The ingestion flow did have a loading pop-up, but it was a `setInterval` that
advanced a percentage forty times and called `onDone()` regardless of what the
backend was doing. Its "% complete" was a count of its own ticks.

`js/analysis-loading.js` lists the stages hydration genuinely performs, marks
each done as its promise resolves, and shows completed-over-total — a real
fraction of a real list — with a real elapsed clock. It closes when hydration
settles, and only then. It reports what it found:

> ✓ Reading your network — 8 facilities, 36 lanes · Done
> ✓ Solving the network — 25 KPIs computed · Done
> ✓ Loading solved scenarios — 0 scenario(s) · Done
> • Building the demand forecast · Working

`/api/kpis/readiness` answers "has this been solved before" **without starting a
solve**, so the wait is described accurately rather than guessed at.

### A related defect the loading screen exposed

`REQUEST_TIMEOUT_MS` was 30 seconds for every call, including the ones that run
a MILP. Aborting the fetch does not stop the solve, so a user could be shown
"Analysis unavailable: request timed out" for work that had succeeded, while the
server paid for it anyway. KPI, baseline, scenario and forecast calls now use
`SOLVE_TIMEOUT_MS`.

---

## 4. Values that came from nowhere

Each row is a figure that was on screen and did not come from the user's data.

| what was shown | where it came from | now |
|---|---|---|
| `vs target: +5.0%` beside the fill rate | a literal `95.0` service target in **three** files | no target unless the data states one; the tile shows `28,249 of 36,982 units served` |
| Period selector: `Q3 2026 / Q2 2026 / Q1 2026 / Q4 2025` | a hardcoded list; no upload has ever contained them, and all four showed identical figures | the periods the demand rows state (`Period 1`), and the control is disabled when there is no real choice |
| `units/day` on every capacity, throughput and flow | a label; the model's cost period is MONTH | `units/month`, from the engine's own `cost_period` |
| `Last refreshed: 5 min ago` | static markup; the refresh button wrote "Just now" without re-fetching | the timestamp the KPI layer returns; the button now actually re-hydrates |
| `Active` on every facility row | a fixed green tag | the solver's own open/closed decision |
| Owner `You` on every workspace | `p.owner_id ? 'You' : 'You'` — both branches | the signed-in account's id; the shared demo reads `Sample` |
| Sign-in password pre-filled `••••••••••••` | markup `value=` | empty |
| `NetGravity AI v2.4` in the assistant header | a version that appears nowhere in this codebase (the app reports 2.0.0) | `/api/status`, and whether a model is reachable |
| New-site form opening on `Nagpur DC` at 21.1458/79.0882, capacity 5,000 | literals | the centroid of the loaded network and the median of its own DC economics; the name opens empty |
| `₹undefinedL/year` in the facility profile | reading `fac.fixedCost`, a field the structure API does not return | `fac.fixedCostPerYear`, formatted, omitted when absent |
| A `GET /api/kpis/network` with no project and no token on every page load | an unconditional call in `renderHomeKPIs` | guarded; the console is clean |

One thing was deliberately made **stricter**: an empty numeric field in the
new-site form is now missing rather than zero. `Number('')` is `0`, so a blank
fixed cost used to propose a site that costs nothing to run — the single most
favourable assumption available, made silently on the user's behalf.

`app/frontend/js/graph.js` still contains a hardcoded 18-node prototype network.
It is **not referenced by anything** — no import, no script tag — so nothing it
contains reaches a screen. It is left in place rather than deleted, and flagged
here.

---

## 5. Other issues found and fixed

**The assistant went permanently deterministic after four questions.**
`max_requests_per_execution: 4` was counted on the gateway *instance* and never
reset. One gateway is built per orchestrator, and the orchestrator lives as long
as the server — so the fifth question anyone asked, for the whole life of the
process, was refused with "budget exhausted" and answered from the deterministic
template instead. Nothing on screen said so: the same question produced a
specific answer in the morning and a generic briefing in the afternoon. It is now
per execution (reset by the orchestrator), with a separate cumulative cap of 500
as the actual runaway guard.

**A time-limited baseline was blamed on the configuration.**
`OptimizationResult.is_solved` accepts `SolverStatus.TIME_LIMIT`, so a solve that
ran out of time before finding any incumbent returns "solved" with every facility
closed. `only_baseline_open_facilities` then filtered out a perfectly assessable
network and reported *"No eligible facilities found under the configured
filters"* — sending the reader to check a configuration that was correct. This is
the intermittent `test_client_exposes_batch_provenance` failure seen under heavy
CPU contention. The two causes are now distinguished, and the message names the
solver status.

---

## Validation

Every number below is from a run against the real engines and, where the
database is involved, against a real PostgreSQL 16.4 server.

| suite | result |
|---|---|
| Backend regression, SQLite | **2,547 passed · 4 skipped** in 150.7s |
| Backend regression, **PostgreSQL** | **2,547 passed · 4 skipped** in 149.8s |
| `validation/phase_10_7/run_postgres_check.py` | **19/19** |
| `validation/phase_10_7/run_greenfield_and_hardcoded_check.py` | **26/26** |
| `validation/phase_10_6/run_persistence_check.py` | 14/14 |
| `validation/phase_10_6/run_identity_map_chat_check.py` | 21/21 |
| `validation/phase_10_5/run_scenario_types_check.py` | 15/15 |
| `validation/phase_10_5/run_scenario_ui_check.py` | 29/29 |
| `validation/phase_10_4/run_scenario_check.py` | 11/11 |
| `validation/phase_10_3/run_ui_flow_check.py` | 25/25 |
| `validation/phase_10_3/run_empty_project_check.py` | 10/10 |
| `validation/phase_10_1/run_client_data_e2e.py` | 27/27 |

The whole suite passing on **both** backends is the migration's real proof: the
same 2,547 assertions, the same code, one against a file and one against a
server on the far side of a socket.

The chat was exercised with ten consecutive questions on a single long-running
server — the case that used to collapse after four. All ten answered from the
network's own figures, declined the three that were out of scope, and asked for
clarification on the one that was ambiguous.

---

## Still not production-ready, and why

Unchanged from Phase 10.6 unless noted.

* **The account store is self-contained.** No password reset, no MFA, no
  lockout, no IdP. Front it with a real identity provider.
* **Bearer tokens live in `localStorage`**, which is reachable from any XSS.
* **PostgreSQL is now the store, but nothing operates it.** No connection
  encryption is enforced (`sslmode` is whatever the URL says), no backup, no
  point-in-time recovery, no migration versioning beyond `CREATE TABLE IF NOT
  EXISTS`. The schema has changed once already this phase; a second change to a
  table with data in it will need a real migration tool.
* **Execution traces are still not persisted** — deliberately. A restart keeps
  the answers and loses the workings.
* **The MILP aggregates every demand row into one period.** The period control
  now reports what the data states, but a multi-period network would be solved
  as a single aggregate, and the screen would not say so.
* **Facility REI still reports a negative performance impact for F004 and
  F007.** Real, explained by the excluded shortage penalty, and still
  unexplained on screen.
* **`/api/kpis/facilities/<id>` returns no resilience or risk block** for the
  baseline workflow, because `NETWORK_STATE_QUERY` does not run an REI
  assessment. Pre-existing; the endpoint reports what the execution produced.
* **Uploaded external signals are still not routed into the forecast**, and the
  card says so rather than implying they were.
* **There is no contract parser.**

Tests passing is not the same as production-ready (§39). What the numbers above
establish is that the five things asked for are done and verified, not that the
system is finished.
