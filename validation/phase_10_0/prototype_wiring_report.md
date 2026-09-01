# Phase 10.0 (continued) — Wiring the Approved Prototype

**Correction to the earlier Phase 10.0 report.** That pass built a separate
production console (`production.html`) alongside the approved prototype. That
was the wrong reading of the brief: §19 asks for the prototype to be *wired*,
not replaced. The separate console has been **deleted**, and `index.html` — its
layout, navigation, screen hierarchy and user flow unchanged — is now the
production application.

---

## 1. What "wired" means here

Eight prototype modules import from `js/data.js`. Its exports are `const`, so
they cannot be reassigned — but they can be mutated in place, and ES module
bindings mean every consumer sees the change. `data.js` already used that
technique in `loadNetworkData()`.

`js/integration/hydrate.js` does the same thing with authoritative figures:

```
project selected / upload committed
   → GET /api/kpis/network        → setAuthoritativeBaseline(...)
   → GET /api/kpis/facilities     → FACILITY_KPIS[...] , node.utilPct , node.throughput
   → GET /api/scenarios           → SCENARIOS
   → clearDemoNarrative(...)      → drop insights about facilities you do not operate
   → renderHome() · renderTwinTables() · initHomeSelectors()
```

No markup was restructured. The only `index.html` edits were: two `id`
attributes added to stat spans that had none, and the agent-modal copy
(see §3).

---

## 2. Upload → analysis, end to end

The blocker named in the previous report ("a customer cannot yet bind their own
data") is closed:

```
upload files      POST /api/ingestions/preview/upload-and-parse
   → parse, classify columns, MEASURE quality
mapping review    the prototype's own confirm screens
   → confirm
commit            POST /api/ingestions/preview/commit
   → assemble_network_from_structure()   →  CanonicalNetwork
   → Orchestrator.register_network()     →  snapshot_id
   → ProjectRegistry.bind_network()      →  bound to this project
analysis          every KPI / scenario / forecast now runs on YOUR network
```

`app/backend/services/network_assembler.py` performs the assembly and returns
an explicit `assumptions` list — every default it had to apply, in words
(annualised fixed costs, uncapacitated facilities, single aggregate product,
markets excluded for want of demand). Nothing is silently substituted.

---

## 3. Fabrications found and removed in this pass

Each was found by driving the real UI, not by reading code.

| # | Defect | Evidence | Fix |
|---|---|---|---|
| 1 | **Extractor invented throughput** | `network_extractor.py` set `throughput = capacity * 0.85` (plants) and `* 0.78` (DCs) | Removed — flow is a solver output |
| 2 | **Extractor invented utilisation** | literal `"utilPct": 78.0` on every DC, rendered on the Digital Twin as measured | Removed |
| 3 | **Extractor invented an entire network** | with no recognisable facilities it returned a "Primary Manufacturing Plant" at Baddi, two DCs, three markets *with demand* — presented as parsed from the user's file | Removed; the assembler now reports exactly what was missing |
| 4 | **Market demand was a placeholder** | markets were derived from lane destinations with a hardcoded `2000`; the real `demand` column was not in the alias list, and lane-derived markets blocked the real ones | Aliases widened; explicit markets table now overrides; no default |
| 5 | **Agent modal named the wrong solver** | "Google OR-Tools Simplex engine … converged in 320ms", plus a "Multi-Agent Adversarial Debate" that does not exist and a fabricated verdict | Replaced with the real pipeline; names PuLP/HiGHS |
| 6 | **`loadNetworkData` fabricated an optimised case** | derived `optimized.totalCost = baseline * 0.94`, `sla: 98.2`, `savings: 6.0%`, labelled `source: "DETERMINISTIC_ENGINE"` | Removed; solved figures arrive via `setAuthoritativeBaseline()` |
| 7 | **Twin stat overlay was hardcoded** | "19 Network Nodes" had no `id` at all; "20 Active Corridors" had an id nothing wrote to | `renderTwinStats()` counts the loaded network |
| 8 | **3D scene never rebuilt** | `initTwin3D` returns early once initialised, so a 5-node upload still rendered 19 demo nodes beside correct tables | `rebuildTwin3D()` on network change, with explicit geometry/material disposal |
| 9 | **Demo narrative survived a network switch** | insights, recommendations, action items and the agent trace still described Guwahati/Baddi | `clearDemoNarrative()` drops anything about facilities not in the loaded network |
| 10 | **Auth read non-existent field ids** | `panel-signin-email` does not exist; it fell back to a hardcoded address, and `.catch(() => null)` entered the app on failure | Real fields; failures stop the flow and show the reason |
| 11 | **Five fabricated projects** | listed for every visitor, all pointing at the same synthetic snapshot | Removed; projects come from the server, owned and isolated |
| 12 | **Hardcoded facility dropdowns** | scenario builder and topbar pickers offered demo DCs not in the user's network | Derived from the loaded network |
| 13 | **CSV export invented insights** | exported two fabricated findings when none existed | Exports an explicit "no insight" row |

### A backend defect this surfaced

`SnapshotManager.assert_fresh()` compared every snapshot against **one global
"current"** pointer, so with more than one project loaded every project but the
most recently ingested was rejected as `STALE_SNAPSHOT`. Freshness is now scoped
per `network_id`: a snapshot is stale only when a newer version of *the same
network* exists — which is precisely what the guard was written to catch. With a
single network the behaviour is unchanged, and all 2,501 tests still pass.

### Null-safety

`formatCurrency`, `formatNumber`, `fmtDelta` and `getNetworkKpis` threw on
`null`. That mattered once the app started telling the truth: a metric the
engine cannot produce is absent, and absence must render as "—", not crash the
screen or print `₹0`.

---

## 4. Validation

`validation/phase_10_0/prototype_e2e_validation.json` — **15 / 15 passing**,
driven through the real prototype UI in Chromium:

| Check | Result |
|---|---|
| Landing → sign-up → create project → upload → mapping → confirm → app shell | PASS |
| Home states the true analysis state | "Analysis complete. Every figure below is computed from your uploaded data." |
| Twin tables list the uploaded facilities | `PLT_PUNE 20,000 / 7,500` · `DC_MUM 12,000 / 33.33%` · `DC_DEL 10,000 / 35%` |
| Twin utilisation matches the API exactly | twin 33.33% · API 33.33% |
| Twin stat overlay counts this network | 5 nodes · 5 corridors · ₹4.6L |
| 3D geometry matches the network | 5 nodes rendered, 5 in network |
| Market demand matches the uploaded file | 4,000 / 3,500 — the values in `markets.csv` |
| No prototype demo facility remains | none |
| Home KPI strip derived from my network | ₹4.6L |
| No blocking JavaScript error | 0 |

Full backend regression: **2,501 passed · 4 skipped · 0 failed.**

---

## 4b. UI parity with the approved standalone

`app/standalone/netgravity_standalone.html` is the design authority. It is a
build artifact: its `<style>` block is the ten stylesheets concatenated, and
its module script is the JS files concatenated. Comparing it against the
served app found one dominant defect and several smaller ones.

**`index.html` linked one of the ten stylesheets.** Only `css/style.css`
(2,954 lines) was referenced; `landing`, `auth`, `home-overview`, `insights`,
`insight-detail`, `agent-reasoning`, `chatbot`, `projects` and `ingestion` —
5,792 further lines, 66% of the design system — were never loaded by any
route, and no JS injected them. Every screen except the core shell rendered
partly unstyled. All ten are now linked in the standalone's exact cascade
order, which is load-bearing: later sheets deliberately override earlier ones.

Verified by reconstructing the standalone's `<style>` block from the ten files
on disk: 99.66% line-identical, the only real difference being 33 lines of S10
callout CSS added after the standalone was built (since removed with the
feature).

Other parity fixes:

| Defect | Fix |
|---|---|
| Digital Twin's third stat tile read "Network Cost", not the approved "Overall Risk" | Restored. Risk is only computed per facility (`risk_factor`); no network-level risk metric has an authoritative owner, so the tile reads "—" rather than an aggregate invented in the browser. Cost keeps its own home on the Home KPI strip. |
| Facility snapshot rendered the literal text `undefined%` | Absent utilisation now renders "—" |
| Attention feed's empty state claimed "network is performing within target" | Now "No insights have been generated for this network yet" — absence of evidence cannot support a clean bill of health |
| Facility dashboard fell back to `₹11.8L` and `₹4.2/unit` when a KPI was unavailable | Both render "—" |
| CSV export fabricated a full KPI set on missing data (`96.7%`, `₹11.8L`, `11.2 Days`, `99.1%`), plus a hardcoded "108% (Breach Risk)" peak utilisation, each stamped "Target Met"/"Healthy"/"Optimal" | Exports "Not available", and no status is asserted over a value that does not exist |

Three features added after the standalone was built changed the approved
layout or flow, and were removed at the user's direction:

- **S10 Future Network callouts** and the map state label — made Scenario
  Planning 69px taller than approved.
- **S9 scenario Preview step** — "Preview Scenario →" then "Confirm & Run"
  replaced the approved single "▶ Run Scenario", and the Current → Proposed
  summary added 24px to the Create Scenario modal.

The Metric Drilldown and Admin Settings modals were **kept**: both are
`display:none` until opened, so they cost zero pixels, and the settings modal
replaced an `alert()` stub.

### Parity result

`validation/phase_10_0/ui_parity_validation.json` — **831 passing, 13 failing**
across 434 selectors harvested from the standalone's own markup (every `id`,
and every class carrying a CSS rule), comparing 27 computed properties plus
bounding-box geometry at 1600×1000 with animations frozen.

- All ten stylesheets load, in the right cascade order.
- No element present in the standalone is missing from production.
- No blocking JavaScript error.
- Zero dimension differences on unique elements.

The 13 remaining failures are: 12 element-count differences caused entirely by
the two kept hidden modals, and one width difference on the Home KPI strip,
where the standalone prints "vs last period: -2.8%". No prior-period value
exists anywhere in the API, the mapper, or the KPI registry, so that delta
cannot be reproduced without inventing it; production prints the metric's
provenance instead, which is 100px wider.

---

## 5. What is still not wired

Stated plainly:

- **Forecast** returns `FORECAST_UNAVAILABLE` for an uploaded network, because
  no demand *history* has been ingested — only a current demand figure. The
  forecast screen will stay empty until transactional history reaches the
  staging zone.
- **Insights, recommendations and the agent trace** are cleared rather than
  regenerated. `/orchestrator/insights` is real and unconsumed; wiring it is the
  next task.
- **Governance** has no UI enforcement path.
- **Persistence** remains in-process; a restart loses accounts and projects.
- `/orchestrator/*` is still unauthenticated.
