# Phase 10.3b — The prototype network was the default state

The four screen defects in `ui_defects_report.md` were all fixed and verified,
and the issues were reported again. They were reported against a project with
**nothing uploaded** — a state every harness so far had skipped, because each
one uploaded the workbook before it looked at anything.

Driving that state directly (`run_empty_project_check.py`) showed why.

---

## 1. What a user with no data was shown

| Screen | Rendered |
|---|---|
| Home KPI strip | **₹12.8L** total cost, "Source: Optimized Base Case (Actual)", **89.5%** fill rate, **7.8%** savings opportunity |
| Facility picker | Delhi NCR DC, Mumbai DC, Bengaluru DC, Kolkata DC, Guwahati DC |
| Digital Twin 2D | 19 markers, 26 corridors |
| Twin tables | Baddi Plant, Pune Plant, Hyderabad Plant, Kolkata Plant; **Delhi NCR DC at 94% "Stress"** |
| KPI screen | **94% (9,400/10,000 u/d)**, SLA 96.7%, cost ₹11.8L, inventory 11.2 days |
| Scenarios | Baseline ₹12.85L vs Scenario 1 **↓7.9%**, Scenario 2 — a full comparison table |
| Assistant | "business network cost of 150,627.70 per period" |

None of it was theirs. Above it sat a banner reading *"This project has no
network yet. Upload your data to run the analysis."*

That combination is worse than either half alone: the banner is one line of
small text above a costed, confident dashboard, and the dashboard is what a
reader believes.

## 2. Why

`data.js` shipped **populated**. It declares the arrays every screen reads —
`PLANTS`, `DCS`, `MARKETS`, `LANES`, `FACILITY_KPIS`, `SCENARIOS`,
`HOME_INSIGHTS`, `RECOMMENDATION`, `AGENT_STATE` — and each held the
prototype's own network as its initial value. Its header said so: *"Central
Mock Data Layer… STATUS: PROTOTYPE / MOCKED"*.

Every fix so far ran on the **success** path: `hydrateFromBackend()` overwrites
those arrays when a solve returns. Nothing emptied them when there was nothing
to overwrite them with. `NO_NETWORK_BOUND` showed a banner and left the demo
network in place.

Two smaller versions of the same thing sat inside the accessors:

* `getOptimizedBaseCase()` returned a hand-authored base case — ₹12.85L, 89.5%
  fill rate, and a 7.8% "savings opportunity" against an optimised counterpart
  no solver produced — whenever no authoritative one had been installed. That
  is the Home strip above.
* `getInsightsForFacility()` and `getKpisForFacility()` fell back to `DC_DELHI`,
  so asking about a facility that is not in the loaded network returned another
  network's insights rather than nothing.

## 3. The change

`data.js` goes from 1,627 lines to 618. Sixteen constants now start empty; the
20 functions are unchanged. Where a record's shape matters it is described in a
comment rather than demonstrated with a fake row.

* `clearNetworkModel()` empties topology, solved metrics, scenarios, signals,
  forecast, narrative and the base case in one call.
* `openProject()` calls it **before** hydrating, so leaving an analysed project
  for an unanalysed one cannot leave the first one's figures under the second
  one's name.
* The `NO_NETWORK_BOUND` branch calls it and re-renders, so the banner now sits
  above empty states instead of a dashboard.
* `getOptimizedBaseCase()` returns an all-null base case. Every consumer
  already renders a dash for a null field.
* The accessors return nothing for a facility that is not loaded.
* The assistant refuses before calling: `/orchestrator/chat` falls back to the
  orchestrator's own boot network when given no snapshot, and answered in full.
  With no snapshot bound it now says there is nothing to report on.

Result, on the same journey:

| Screen | Now |
|---|---|
| Home KPI strip | "—", "No solved result yet", "Not available" |
| Facility picker | "No facility in this network" |
| Digital Twin 2D | 0 markers |
| KPI screen | "No facility to report on — upload a dataset to populate these KPIs" |
| Forecast | "—" |
| Scenarios | "No solved scenarios yet for this network" |
| Assistant | "This project has no analysed network yet, so I have nothing to report on." |

## 4. Also fixed

The assistant's request ran against the API client's 30-second default. A chat
question runs a solve **and** a reasoning pass, so it aborted mid-analysis and
told the user the engine was unreachable while it was still working. Raised to
180s for that one call; the typing indicator shows throughout.

That failure had been passing its own check: `U-16` only excluded the phrase
"did not return an answer", so a timeout satisfied it. The check now fails on a
timeout, an unreachable engine, or a refusal.

---

## 5. Validation

| Suite | Result |
|---|---|
| `run_empty_project_check.py` (new) | **10 / 10** |
| `run_ui_flow_check.py` | **25 / 25** |
| `run_client_data_e2e.py` | **27 / 27** |
| Backend regression | **2,542 passed · 4 skipped** |
| Uncaught page errors | **0** on both journeys |

The new harness checks each screen against a list of prototype markers —
`Baddi`, `Guwahati`, `Delhi NCR DC`, `12.8L`, `89.5%`, `DC_CENTRAL` — so any
future path that reintroduces demo content fails a named check rather than
looking plausible.

## 6. UI parity moved, and this is why

`run_ui_parity.py` goes from **828 PASS / 16 FAIL** to **775 / 69**.

The harness loads the production app **with nothing uploaded** and compares it
to the standalone, which ships with the prototype network populated. It is now
comparing an empty app to a populated one, so the comparison's premise no
longer holds for content.

All 69 failures are content-volume, not design:

* **30** are element counts (`.btn` 4 vs 8, `.card-title` 11 vs 13) — buttons
  and titles that live inside table rows that no longer exist;
* **34** are `height`/`width` (`.card-table-scroll` 271px vs 40px, `.app-shell`
  5,732px vs 5,138px) — a page with no rows is shorter;
* **5** touch `display`/`padding`/`margin`, and each traces to the same cause:
  `#home-twin-callout` is `display:none` because it reports the *selected
  facility's* utilisation and there is no facility; `#topbar-controls` differs
  by 15px because the facility `<select>` reads "No facility in this network"
  instead of "Delhi NCR DC"; `.text-muted`/`.text-xs` match a different first
  element, now inside an empty-state card.

The controlled comparison is clean: between the 828 run and the 775 run the
only change to anything the harness measures was emptying the seed data. No CSS
rule and no markup on the app shell changed.

**This is a real tension and it should be a decision, not a silent trade.** The
standing instruction was that production should look exactly like the approved
standalone. The standalone's content is the prototype's fabricated network. The
two can be reconciled for the design system — and are — but not for content,
because production's content is now real or absent. If exact content parity
matters more than the empty state, say so and I will put the demo network back
behind an explicit "demo project" switch instead of making it the default.

---

## 7. Still not done

* **`/orchestrator/*` is unauthenticated.** Unchanged and still the largest
  gap. The frontend now refuses to ask about an unbound network, but the
  endpoint itself will still answer anyone who asks it directly.
* **The reasoning LLM emits nothing on every run.** Now correctly diagnosed:
  the model spends its entire 2,000-token output allowance on internal
  reasoning (`output_tokens=1984`) and returns an empty body. The gateway
  accepts exactly one field — `{"prompt": "..."}` — with no model, no
  temperature and no reasoning-effort control, so there is no knob to turn, and
  tuning by trial costs live calls against a shared budget. The reasoning layer
  degrades to deterministic text, which is what the assistant's answers are.
* **Persistence is in-process** — a restart loses accounts, projects and
  uploads.
* **No contract parser**; the PDF path stores the file and says so.
* **Uploaded signals do not influence the forecast**; capacity history reaches
  no engine.
* **No scenario is generated automatically.**
