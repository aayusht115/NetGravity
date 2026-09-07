/**
 * NetGravity — Scenario Planning Workspace
 * ========================================
 * Executive decision workspace for scenario exploration, MILP evaluation,
 * trade-off comparison, robustness stress-testing, and AI recommendation.
 * 
 * Matches approved visual designs:
 * - Single Scenario Deep-Dive (My Scenarios)
 * - Multi-Scenario Trade-off Analysis (Scenario Comparison)
 */

import { SCENARIOS, DCS, PLANTS, MARKETS, NETWORK_REGIONS, PRODUCT_CATEGORIES,
         formatNumber, formatCurrency,
         SOLVE_HORIZON, currencyLabel, withCurrency } from './data.js';
import { countriesContaining, loadAdmin1 } from './world-basemap.js';
import { initMap, renderScenarioDigitalTwin, invalidateMapSize,
         revealMap } from './map.js';
import { scenarioService } from './integration/services/scenario-service.js';
import {
  startRun, stepStart, stepDone, stepFail, finishRun, note,
} from './agent-activity.js';
import { mountAgentLoading, dismissAgentLoading, offerBackgroundExit,
         withdrawBackgroundExit } from './agent-loading.js';
import { startBackgroundTask, finishBackgroundTask,
         failBackgroundTask } from './background-tasks.js';
import {
  mapScenarioRecord, baselineFromScenarioRecord, BASELINE_SCENARIO_ID,
} from './integration/mappers/scenario-mapper.js';

// ─── State ──────────────────────────────────────────────────
// Which metric rows the comparison table shows — user-controlled via
// "Customize metrics" (see renderCustomizeMenu). Defaults to the 4 "key" rows.
let multiVisibleKeys = ['totalCost', 'costChange', 'changeEffect', 'fillRate',
                        'capacityRisk'];
// Up to 3 scenarios shown alongside baseline — the single source of truth
// for both the comparison table and the Digital Twin map's toggle group.
//
// This started as `['SCN_REBALANCE', 'SCN_USER_1']`: two prototype ids that no
// backend has ever issued. They matched nothing, so they rendered as nothing —
// and they permanently occupied two of the three comparison slots. The first
// scenario a user created was pushed into the third; the second and third
// OVERWROTE it, because `multiSelectedIds.length` was already 3. That is why
// only ever one scenario appeared, however many were solved.
//
// It is now derived from the scenarios that actually exist, in `syncSelection`.
let multiSelectedIds = [];
// Which of the selected scenarios the Digital Twin map is currently showing.
let mapActiveId = BASELINE_SCENARIO_ID;

/** Up to three scenarios to compare, alongside the baseline. */
const MAX_COMPARED = 3;

function baselineScenario() {
  return SCENARIOS.find((s) => s.id === BASELINE_SCENARIO_ID) || null;
}

function userScenarios() {
  return SCENARIOS.filter((s) => s.id !== BASELINE_SCENARIO_ID);
}

/**
 * Reconcile the comparison selection with the scenarios that exist.
 *
 * Drops ids that no longer resolve (deleted scenarios, a project switch) and
 * fills empty slots with the most recently solved scenarios, so opening the
 * page always shows something to compare rather than an empty table beside a
 * populated scenario list.
 */
function syncSelection() {
  const known = new Set(SCENARIOS.map((s) => s.id));
  multiSelectedIds = multiSelectedIds.filter(
    (id) => known.has(id) && id !== BASELINE_SCENARIO_ID);

  if (multiSelectedIds.length < MAX_COMPARED) {
    const newestFirst = userScenarios().slice().reverse();
    for (const scenario of newestFirst) {
      if (multiSelectedIds.length >= MAX_COMPARED) break;
      if (!multiSelectedIds.includes(scenario.id)) {
        multiSelectedIds.push(scenario.id);
      }
    }
  }
  multiSelectedIds = multiSelectedIds.slice(0, MAX_COMPARED);

  if (!SCENARIOS.some((s) => s.id === mapActiveId)) {
    mapActiveId = baselineScenario() ? BASELINE_SCENARIO_ID
      : (multiSelectedIds[0] || BASELINE_SCENARIO_ID);
  }
}

// Metric definitions with formatters, provenance, and drilldown data
const ALL_METRIC_DEFS = {
  totalCost: {
    key: 'totalCost',
    label: 'Total Cost ({ccy})',
    fmt: (v) => formatCurrency(v),
    provenance: 'MODEL FACT',
    category: 'financial',
  },
  costChange: {
    key: 'costChange',
    label: 'Cost Change',
    fmt: (v) => (v === 0 ? '—' : `${v < 0 ? '↓ ' : '↑ '}${Math.abs(v)}%`),
    cellClass: (v) => (v < 0 ? 'cell-pos' : v > 0 ? 'cell-neg' : ''),
    category: 'financial',
  },
  sla: {
    key: 'sla',
    label: 'SLA (On-time)',
    fmt: (v) => `${v}%`,
    cellClass: (v) => (v >= 95 ? 'cell-pos' : 'cell-neg'),
    provenance: 'MODEL FACT',
    category: 'operations',
  },
  capacityRisk: {
    key: 'capacityRisk',
    label: 'Capacity Risk (Dec)',
    fmt: (v) => v,
    cellClass: (v) => {
      if (v === 'High') return 'cell-neg';
      if (v === 'Medium') return 'cell-warn';
      return 'cell-pos';
    },
    provenance: 'FORECAST',
    category: 'operations',
  },
  avgUtil: {
    key: 'avgUtil',
    label: 'Network Avg Utilisation',
    fmt: (v) => `${v}%`,
    cellClass: (v) => (v > 70 ? 'cell-neg' : 'cell-pos'),
    provenance: 'MODEL FACT',
    category: 'operations',
  },
  transportCost: {
    key: 'transportCost',
    label: 'Transport Costs',
    fmt: (v) => formatCurrency(v),
    provenance: 'MODEL FACT',
    category: 'financial',
  },
  inventoryDays: {
    key: 'inventoryDays',
    label: 'Inventory Days',
    fmt: (v) => `${v}`,
    provenance: 'MODEL FACT',
    category: 'financial',
  },
  carbonKg: {
    key: 'carbonKg',
    label: 'Scope 3 Carbon (kg CO2)',
    fmt: (v) => formatNumber(v),
    provenance: 'FORECAST',
    category: 'sustainability',
  },
  implementationTime: {
    key: 'implementationTime',
    label: 'Implementation Time',
    fmt: (v) => v,
    provenance: 'MODEL FACT',
    category: 'sustainability',
  },
  fixedCost: {
    key: 'fixedCost',
    label: 'Fixed Facility Cost',
    fmt: (v) => formatCurrency(v),
    provenance: 'MODEL FACT',
    category: 'financial',
  },
  inventoryCost: {
    key: 'inventoryCost',
    label: 'Inventory Holding Cost',
    fmt: (v) => formatCurrency(v),
    provenance: 'MODEL FACT',
    category: 'financial',
  },
  delhiUtil: {
    key: 'delhiUtil',
    label: 'Utilisation – Delhi NCR',
    fmt: (v) => `${v}%`,
    cellClass: (v) => (v > 92 ? 'cell-neg' : 'cell-pos'),
    provenance: 'MODEL FACT',
    category: 'operations',
  },
};

// ─── Deep-Dive / Comparison table rows ───────────────────────
// The default-visible rows in the multi-scenario table (Dump/Scenario
// comparison deepdive updated.png, Dump/Multiple scenario comparison
// updated.png), picked per the global KPI priority order (Cost > Savings >
// Service/SLA > Capacity/Risk) rather than the original mockup's set, since
// S8's spec now requires 9 comparison dimensions and not all of them can be
// on by default without crowding. "View detailed comparison" / the
// customize-metrics picker exposes every row in DETAIL_EXTRA_ROWS.
const DEEPDIVE_ROWS = [
  { key: 'totalCost', label: 'Total Network Cost', sub: '({ccy} per period)', icon: '💰', unit: 'currency', kind: 'lowerBetter' },
  // "Cost change", not "Savings".
  //
  // The value is `costChange`: negative when the scenario costs less. Labelled
  // "Savings %" it read exactly backwards — a +10% demand scenario that pushed
  // cost UP 3.6% was shown as "Savings ↑ 3.6%", and a capacity scenario that
  // cut cost 16.9% as "Savings ↓ 16.9%". An executive ranking options by that
  // column picks the one that costs more.
  //
  // Renamed rather than negated: this is the number the engine computes, and
  // naming it correctly removes the sign question entirely instead of adding a
  // derived field that can drift. The arrow and colour already follow
  // `lowerBetter`, which was right all along — only the word was wrong.
  { key: 'costChange', label: 'Cost change %', sub: '(vs baseline — down is cheaper)', icon: '💹', unit: 'percent', kind: 'lowerBetter',
    fmt: (v) => (v === 0 ? 'No change' : `${v < 0 ? '↓' : '↑'} ${Math.abs(v).toFixed(1)}%`) },
  // Fill rate rather than SLA as a default row: it is the figure that moves on
  // every scenario this engine solves, and the one an infeasible network is
  // conditioned on. SLA is a row away in the picker.
  { key: 'fillRate', label: 'Demand Fill Rate', sub: '(% of demand served)', icon: '✅', unit: 'percent', kind: 'higherBetter' },
  { key: 'capacityRisk', label: 'Capacity Risk', sub: '(Overall network)', icon: '⚠️', unit: 'categorical', kind: 'categorical' },
  // The row that keeps "Savings %" honest.
  //
  // The baseline column is the network AS RUN — every facility open, because
  // that is what the client operates. A scenario is solved with the freedom to
  // close sites. So "Savings %" is the value of adopting the scenario, and on
  // this network most of that value is simply re-optimising the existing
  // footprint: three unrelated scenarios all reported about −47%. This row
  // separates them, so a change that does nothing shows as doing nothing.
  { key: 'changeEffect', label: "This change's own effect", sub: '(vs the same network re-optimised)', icon: '🔬', unit: 'currency', kind: 'lowerBetter',
    fmt: (v) => (v === null || v === undefined ? 'Unavailable'
      : Math.abs(v) < 1 ? 'No effect'
      : `${v < 0 ? '↓ ' : '↑ '}${formatCurrency(Math.abs(v))}`) },
];

// Available via the customize-metrics picker (not shown by default) —
// Fill Rate and Risk Factor are P0-required dimensions for S8 but have no
// backing data anywhere in this build (confirmed during the S9 pass: no
// RF/REI data exists, and no scenario carries a fill-rate field), so their
// rows render "Not available" rather than a fabricated number. Keeping them
// out of the default view avoids every user seeing a "Not available" row
// unasked; putting them in the picker keeps the gap honest and discoverable.
const DETAIL_EXTRA_ROWS = [
  { key: 'sla', label: 'Service Level', sub: '(% of demand inside SLA)', icon: '🛡️', unit: 'percent', kind: 'higherBetter' },
  { key: 'referenceCost', label: 'Re-optimised, no change', sub: '(your footprint, solver free to close sites)', icon: '♻️', unit: 'currency', kind: 'lowerBetter' },
  { key: 'avgUtil', label: 'Avg Utilization', sub: '(Network avg.)', icon: '📈', unit: 'percent', kind: 'lowerBetter' },
  { key: 'maxUtil', label: 'Max Utilization', sub: '(Peak facility)', icon: '📊', unit: 'percent', kind: 'lowerBetter' },
  // These four are components of Total Network Cost and were rendering an em
  // dash on every row. The payload carried all of them the whole time — the
  // mapper read six KPIs out of a response that carries twenty, so there was
  // no field on the record for the table to find.
  { key: 'transportCost', label: 'Transport Cost', sub: '({ccy} per period)', icon: '🚛', unit: 'currency', kind: 'lowerBetter' },
  { key: 'fixedCost', label: 'Fixed Facility Cost', sub: '({ccy} per period)', icon: '🏭', unit: 'currency', kind: 'lowerBetter' },
  { key: 'handlingCost', label: 'Handling Cost', sub: '({ccy} per period)', icon: '📥', unit: 'currency', kind: 'lowerBetter' },
  { key: 'inventoryCost', label: 'Inventory Cost', sub: '({ccy} per period)', icon: '📦', unit: 'currency', kind: 'lowerBetter' },
  { key: 'unservedDemand', label: 'Unserved Demand', sub: '(units the plan strands)', icon: '🚫', unit: 'number', kind: 'lowerBetter' },
  { key: 'facilitiesOpen', label: 'Facilities Open', sub: '(sites the plan uses)', icon: '🏢', unit: 'number', kind: 'lowerBetter' },
  { key: 'carbonKg', label: 'Scope 3 Carbon', sub: '(kg CO2 per period)', icon: '🌱', unit: 'number', kind: 'lowerBetter' },
  // No engine in this build computes days-on-hand or a governed Risk Factor.
  // They stay in the picker, marked, rather than being quietly dropped.
  { key: 'inventoryDays', label: 'Inventory Days', sub: '(not computed by this engine)', icon: '📅', unit: 'number', kind: 'lowerBetter', unavailable: true },
  { key: 'riskFactor', label: 'Risk Factor (RF)', sub: '(chain explained in Scenario evidence)', icon: '🧭', unit: 'number', kind: 'higherBetter', unavailable: true },
];

const ALL_TABLE_ROWS = [...DEEPDIVE_ROWS, ...DETAIL_EXTRA_ROWS];

//: Rows that only exist relative to the baseline, so the baseline's own cell
//: reads "Reference" rather than an absent value.
const REFERENCE_ONLY_ROWS = new Set(['costChange', 'changeEffect']);

function fmtRowValue(row, v) {
  if (row.unavailable) return 'Not available';
  // A figure the engine could not produce is absent, and says so. It is never
  // rendered as zero, which on a cost row reads as a free network.
  if (v === undefined || v === null) return 'Unavailable';
  if (row.fmt) return row.fmt(v);
  if (row.unit === 'currency') return formatCurrency(v);
  if (row.unit === 'percent') return `${Number(v).toFixed(1)}%`;
  if (row.unit === 'number') return formatNumber(v);
  return v;
}

function riskRank(label) {
  return { 'Very Low': 0, Low: 1, Medium: 2, High: 3, 'Very High': 4 }[label] ?? 2;
}

// Plain "Scenario N" naming everywhere in this UI — no "(AI Rec.)" /
// "(My Scen 1)" suffixes from the underlying data's cardTitle field.
function scenarioDisplayName(s) {
  if (s.id === 'SCN_ACTUAL') return 'Baseline';
  // Every scenario shows the name the user gave it.
  //
  // This used to key on an `SCN_CUSTOM_` id prefix, which the backend has
  // never produced — it issues `SCN_<8 hex>` — so the branch never fired and
  // every scenario appeared as the generic "Scenario 1", "Scenario 2" in
  // dropdowns, table headers and the drawer. The prefix existed to tell a
  // user's scenario apart from the built-in presets; those presets are gone,
  // so anything in this list is one the user solved.
  return s.name || s.cardTitle || `Scenario ${s.num}`;
}

// Compares a scenario's value against baseline for one row. Returns
// { text, good } where good is true/false, or null for "no material
// change". Categorical rows (Capacity Risk) resolve via riskRank rather
// than always showing "—" — a scenario that improves risk should say so.
function computeRowDelta(row, baseVal, scnVal) {
  if (!row || row.unavailable) return { text: '—', good: null };
  // No delta exists unless both sides do. `undefined - undefined` is NaN, and
  // NaN formatted through toFixed reads as "NaN%" on screen.
  if (row.kind !== 'categorical'
      && (typeof baseVal !== 'number' || typeof scnVal !== 'number')) {
    return { text: '—', good: null };
  }
  if (row.kind === 'categorical') {
    const baseRank = riskRank(baseVal);
    const scnRank = riskRank(scnVal);
    if (baseRank === scnRank) return { text: 'Unchanged', good: null };
    return scnRank < baseRank ? { text: 'Improved', good: true } : { text: 'Worsened', good: false };
  }
  const diff = scnVal - baseVal;
  if (Math.abs(diff) < 0.001) return { text: '—', good: null };
  const text = row.unit === 'percent'
    ? `${diff > 0 ? '↑' : '↓'} ${Math.abs(diff).toFixed(1)} pts`
    : `${diff > 0 ? '↑' : '↓'} ${Math.abs(baseVal ? (diff / baseVal) * 100 : 0).toFixed(1)}%`;
  const good = row.kind === 'higherBetter' ? diff > 0 : diff < 0;
  return { text, good };
}

// Shared deltas used by both the KPI cards and the "NetGravity's take"
// checklist, so the two always agree with each other and with the table.
// Looks rows up by key (not position) — DEEPDIVE_ROWS' order isn't stable
// now that Savings % sits between Total Cost and Service Level.
function computeScenarioDeltas(baseline, scn) {
  const rowByKey = (key) => ALL_TABLE_ROWS.find((r) => r.key === key);
  // A project with no solved scenarios has no baseline to compare against —
  // `SCENARIOS.find(s => s.id === 'SCN_ACTUAL')` is then undefined, and every
  // caller here dereferenced it, throwing on `baseline.totalCost`. There is no
  // delta without two sides, so it reports none.
  const b = baseline || {};
  const s = scn || {};
  return {
    cost: computeRowDelta(rowByKey('totalCost'), b.totalCost, s.totalCost),
    sla: computeRowDelta(rowByKey('sla'), b.sla, s.sla),
    util: computeRowDelta(rowByKey('avgUtil'), b.avgUtil, s.avgUtil),
    riskGood: (b.capacityRisk == null || s.capacityRisk == null)
      ? null : riskRank(s.capacityRisk) < riskRank(b.capacityRisk),
  };
}

// ─── Init ───────────────────────────────────────────────────
export function initScenarios() {
  syncSelection();
  renderScenarioSelector();
  renderMultiScenarioTable();
  renderMultiScenarioTakeCard();
  renderScenarioMapToggle();
  wireScenarioEvents();

  // Initialize visual context 2D digital twin map.
  //
  // `zoom` / `center` are only the view before anything is known about the
  // network; `initMap` re-frames onto the actual nodes as soon as it has
  // drawn them. `initScenarios()` also runs at app boot, when this panel is
  // `display: none` and the container is 0x0 — a map framed at that size
  // resolves to the minimum zoom — so `updateScenarioMap()` re-measures and
  // re-frames once the page is actually on screen.
  setTimeout(() => {
    initMap('scenario-leaflet-map', {
      zoom: 4.2,
      center: [22.5, 79.5],
      isCompact: true,
      // The same zoom the Digital Twin page has, which is the source of
      // truth for how a map in this product behaves: wheel, +/-, fit,
      // double-click, box-zoom, keyboard — all live from the start.
      //
      // `isCompact` used to turn the wheel off here, and the click-to-arm
      // step that replaced it was a guard against this card swallowing the
      // page's scroll. Measured: the Scenario Planning page's scroll height
      // equals its client height — the page does not scroll — so the guard
      // was protecting against something that does not happen, at the cost
      // of behaving unlike every other map.
      scrollWheelZoom: true,
      initialScenario: mapActiveId,
      mode: mapActiveId === BASELINE_SCENARIO_ID ? 'baseline' : 'scenario',
    });
    updateScenarioMap();
  }, 60);
}

// The page is rebuilt whenever a network finishes loading, so a project opened
// from a fresh session shows its solved scenarios without the user having to
// navigate away and back. `hydrateFromBackend` fires this after it has filled
// SCENARIOS.
if (typeof window !== 'undefined') {
  window.addEventListener('authoritativeDataLoaded', () => {
    if (!document.getElementById('multi-scenario-table-wrap')) return;
    syncSelection();
    invalidateComparison();
    renderScenarioSelector();
    renderMultiScenarioTable();
    renderMultiScenarioTakeCard();
    renderScenarioMapToggle();
    updateScenarioMap();
  });
}

// ─── Scenario Selector (selected chips + "Add Scenario" menu) ───
// Only currently-selected scenarios (up to 3) show as chips; every other
// scenario lives behind "Add Scenario" instead of being listed inline, so
// the bar stays compact regardless of how many scenarios exist.
function renderScenarioSelector() {
  const chipsEl = document.getElementById('scn-selected-chips');
  const menu = document.getElementById('scn-add-scenario-menu');
  if (!chipsEl || !menu) return;

  const selected = multiSelectedIds.map((id) => SCENARIOS.find((s) => s.id === id)).filter(Boolean);
  const unselected = userScenarios().filter((s) => !multiSelectedIds.includes(s.id));

  chipsEl.innerHTML = selected.length
    ? selected
      .map((s) => `
      <div class="scn-selected-chip" data-scn-id="${s.id}">
        <span>${scenarioDisplayName(s)}</span>
        <button type="button" class="scn-scenario-dropdown-del" data-remove-id="${s.id}" title="Remove from comparison">✕</button>
      </div>`)
      .join('')
    : '<span class="text-xs text-muted">No scenario yet — create one to compare against your network.</span>';

  chipsEl.querySelectorAll('[data-remove-id]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.removeId;
      multiSelectedIds = multiSelectedIds.filter((sid) => sid !== id);
      if (mapActiveId === id) mapActiveId = BASELINE_SCENARIO_ID;
      invalidateComparison();
      // Without this, dropping a chip immediately re-added the same scenario:
      // `syncSelection` fills empty slots from the newest scenarios, and the
      // one just removed is usually the newest. Removing a chip means "stop
      // comparing this", so it stays out until the user adds it back.
      renderScenarioSelector();
      renderMultiScenarioTable();
      renderMultiScenarioTakeCard();
      renderScenarioMapToggle();
      updateScenarioMap();
    });
  });

  const addBtn = document.getElementById('scn-add-scenario-btn');
  if (addBtn) addBtn.classList.toggle('disabled', multiSelectedIds.length >= MAX_COMPARED);

  // The menu ALWAYS lists the scenarios that are not being compared.
  //
  // It used to replace the whole list with "remove one to add another" as soon
  // as three were selected — and the delete button lives on those list items,
  // so with three scenarios compared there was no way to delete a fourth at
  // all. Adding is what the limit governs; deleting is not.
  const atLimit = multiSelectedIds.length >= MAX_COMPARED;
  if (unselected.length === 0) {
    menu.innerHTML = '<div class="scn-add-scenario-empty">Every scenario is already being compared.</div>';
  } else {
    menu.innerHTML = (atLimit
      ? `<div class="scn-add-scenario-empty">Comparing the maximum of ${MAX_COMPARED} — remove one to add another. You can still delete from here.</div>`
      : '')
      + unselected
        .map((s) => `
        <div class="scn-scenario-dropdown-item${atLimit ? ' disabled' : ''}"${atLimit ? '' : ` data-add-id="${s.id}"`}>
          <span${atLimit ? ' style="opacity:.55"' : ''}>${scenarioDisplayName(s)}</span>
          <button type="button" class="scn-scenario-dropdown-del" data-del-id="${s.id}" title="Delete scenario">✕</button>
        </div>`)
        .join('');
  }

  menu.querySelectorAll('[data-add-id]').forEach((item) => {
    item.addEventListener('click', (e) => {
      if (e.target.closest('.scn-scenario-dropdown-del')) return;
      if (multiSelectedIds.length >= MAX_COMPARED) return;
      multiSelectedIds.push(item.dataset.addId);
      menu.classList.remove('open');
      onSelectionChanged();
    });
  });

  menu.querySelectorAll('.scn-scenario-dropdown-del[data-del-id]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      removeScenario(btn.dataset.delId);
    });
  });
}

// Re-renders everything that depends on which scenarios are selected.
function onSelectionChanged() {
  syncSelection();
  invalidateComparison();
  renderScenarioSelector();
  renderMultiScenarioTable();
  renderMultiScenarioTakeCard();
  renderScenarioMapToggle();
  updateScenarioMap();
}

/**
 * Delete a scenario for good.
 *
 * This used to splice the local array only, so a scenario the user deleted
 * came back on the next page load — `hydrateFromBackend` re-lists them from the
 * server, which had never been told. It also refused to delete the last one,
 * for a reason that no longer holds: the baseline is its own row now, so a
 * project with no scenarios still has a network to show.
 */
async function removeScenario(scenarioId) {
  try {
    await scenarioService.deleteScenario(scenarioId);
  } catch (err) {
    // The scenario stays on screen rather than disappearing from a view that
    // the server would repopulate on the next load.
    console.warn('Scenario delete failed:', err);
    window.alert(`This scenario could not be deleted: ${err?.message || err}`);
    return;
  }

  const idx = SCENARIOS.findIndex((s) => s.id === scenarioId);
  if (idx !== -1) SCENARIOS.splice(idx, 1);

  multiSelectedIds = multiSelectedIds.filter((id) => id !== scenarioId);
  if (mapActiveId === scenarioId) mapActiveId = BASELINE_SCENARIO_ID;

  onSelectionChanged();
}

// ─── Digital Twin Map Toggle (one button per selected scenario) ───
function renderScenarioMapToggle() {
  const group = document.getElementById('scn-map-toggle-group');
  if (!group) return;

  const baseline = baselineScenario();
  const selected = multiSelectedIds.map((id) => SCENARIOS.find((s) => s.id === id)).filter(Boolean);
  const options = baseline ? [baseline, ...selected] : selected;
  if (!options.length) {
    group.innerHTML = '<span class="text-xs text-muted">No solved network to draw yet.</span>';
    return;
  }
  if (!options.some((s) => s.id === mapActiveId)) mapActiveId = options[0]?.id;

  group.innerHTML = options
    .map((s) => `<button type="button" class="toggle-btn${s.id === mapActiveId ? ' active' : ''}" data-map-scn-id="${s.id}">${scenarioDisplayName(s)}</button>`)
    .join('');

  group.querySelectorAll('[data-map-scn-id]').forEach((btn) => {
    btn.addEventListener('click', () => {
      mapActiveId = btn.dataset.mapScnId;
      group.querySelectorAll('.toggle-btn').forEach((b) => b.classList.toggle('active', b === btn));
      updateScenarioMap();
    });
  });
}

// ─── Shared row rendering for both comparison tables ─────────

/**
 * The sub-label for a comparison row, corrected for the horizon actually
 * solved.
 *
 * Every cost and carbon figure in this table is the solver's total across the
 * modelled periods. These rows were written when a solve was always one period
 * and say "per period" literally — so on a twelve-month horizon they label a
 * twelve-month total as a monthly one, which overstates it twelvefold in the
 * one place a user compares options and picks one. The row keeps its own text
 * for a single-period solve, where it is exactly right.
 */
function rowSubLabel(row) {
  // `{ccy}` is resolved here rather than in the row table, because that table
  // is a module constant evaluated before hydration has read the network's
  // currency. Substituting at render time is what lets one definition serve a
  // rupee network and a dollar one.
  const sub = withCurrency(row.sub);
  const n = SOLVE_HORIZON.periodsModelled;
  if (!n || n <= 1 || !/per period/.test(sub || '')) return sub;
  const span = (SOLVE_HORIZON.firstPeriod && SOLVE_HORIZON.lastPeriod)
    ? `${SOLVE_HORIZON.firstPeriod}–${SOLVE_HORIZON.lastPeriod}`
    : `${n} periods`;
  return sub.replace('per period', `total, ${span}`);
}

function scenarioRowMetricCellHtml(row) {
  return `<span class="scn-row2-icon">${row.icon}</span>
    <div><div class="scn-row2-label">${row.label}</div><div class="scn-row2-sub">${rowSubLabel(row)}</div></div>`;
}

function scenarioDeltaPillHtml(delta) {
  const tone = delta.good === null ? 'neutral' : (delta.good ? 'good' : 'bad');
  return `<span class="scn-delta-pill ${tone}">${delta.text}</span>`;
}

// ─── Leaflet Visual Context (2D Digital Twin) ───────────────
function updateScenarioMap() {
  const mode = mapActiveId === BASELINE_SCENARIO_ID ? 'baseline' : 'scenario';
  renderScenarioDigitalTwin('scenario-leaflet-map', mapActiveId, mode);
  renderScenarioMapCaption();
  invalidateMapSize('scenario-leaflet-map');
  // Re-frame on the scenario just drawn. A scenario can open a greenfield
  // site outside the baseline's own extent, and a map held at the baseline's
  // frame would draw that site off the edge — the reader would see lanes
  // leaving the picture towards a DC that appears not to exist. The delay
  // lets invalidateSize() land first, so the fit is against a real viewport.
  setTimeout(() => revealMap('scenario-leaflet-map'), 60);
}

/**
 * Say, in words, what the map is currently showing and what moved.
 *
 * The map redraws correctly, but a change of two percentage points on one ring
 * out of eight is easy to miss — which is exactly the complaint that this
 * "feels like a mock-up, no real change is visible". The caption states the
 * difference the drawing is showing.
 */
function renderScenarioMapCaption() {
  const wrap = document.getElementById('scenario-map-wrap');
  if (!wrap) return;
  let caption = document.getElementById('scn-map-caption');
  if (!caption) {
    caption = document.createElement('div');
    caption.id = 'scn-map-caption';
    caption.className = 'text-xs text-muted';
    caption.style.cssText = 'padding:8px 2px 0;line-height:1.5';
    wrap.parentElement?.appendChild(caption);
  }

  const scn = SCENARIOS.find((s) => s.id === mapActiveId);
  if (!scn) { caption.textContent = ''; return; }
  if (scn.id === BASELINE_SCENARIO_ID) {
    caption.innerHTML = '<strong>Current network.</strong> Ring colour is the '
      + 'solved utilisation of each distribution centre; line weight is solved '
      + 'volume on each corridor.';
    return;
  }

  const changes = describeScenarioChanges(scn);
  caption.innerHTML = `<strong>${scenarioDisplayName(scn)}.</strong> `
    + (changes.length
      ? changes.map((c) => c.text).join(' · ')
      : 'The solver reached the same plan as the baseline — this change moved nothing.');
}

/**
 * What one scenario actually did to the network, read from the two solved
 * states rather than from a stored narrative.
 *
 * Returns [] when nothing moved, which is itself an answer worth showing.
 */
function describeScenarioChanges(scn) {
  const out = [];
  const before = scn.baselineFacilities || {};
  const after = scn.scenarioFacilities || {};

  (scn.newSites || []).forEach((site) => {
    const state = after[site.id];
    const opened = state && state.isOpen === true && (state.throughput || 0) > 0;
    out.push({
      kind: opened ? 'opened' : 'declined',
      text: opened
        ? `New site ${site.name} opens, moving ${formatNumber(Math.round(state.throughput))} units`
        : `New site ${site.name} was offered to the solver and left closed — on these costs it does not pay`,
    });
  });

  const closed = Object.keys(after).filter(
    (id) => after[id]?.isOpen === false && before[id]?.isOpen === true);
  if (closed.length) {
    out.push({ kind: 'closed', text: `${closed.length} facility${closed.length === 1 ? '' : ' sites'} closes (${closed.join(', ')})` });
  }
  const opened = Object.keys(after).filter(
    (id) => after[id]?.isOpen === true && before[id]?.isOpen === false);
  if (opened.length) {
    out.push({ kind: 'opened', text: `${opened.join(', ')} re-opens` });
  }

  const moved = laneMovements(scn);
  if (moved.length) {
    out.push({ kind: 'flow', text: `${moved.length} corridor${moved.length === 1 ? '' : 's'} carry different volume` });
  }
  return out;
}

/** Lanes whose solved volume differs between the two states. */
function laneMovements(scn) {
  const key = (f) => `${f.origin_id}->${f.destination_id}`;
  const before = new Map((scn.baselineFlows || []).map((f) => [key(f), f.flow_units]));
  const after = new Map((scn.scenarioFlows || []).map((f) => [key(f), f.flow_units]));
  const lanes = new Set([...before.keys(), ...after.keys()]);
  const moved = [];
  lanes.forEach((k) => {
    const b = before.get(k) || 0;
    const a = after.get(k) || 0;
    if (Math.abs(a - b) >= 1) moved.push({ lane: k, before: b, after: a, shift: a - b });
  });
  return moved.sort((x, y) => Math.abs(y.shift) - Math.abs(x.shift));
}

// ─── Render Multi-Scenario Comparison Table (up to 3 scenarios) ──
function renderMultiScenarioTable() {
  const container = document.getElementById('multi-scenario-table-wrap');
  if (!container) return;

  // The baseline is its own row now, so this is genuinely the network as
  // solved. It used to fall back to `SCENARIOS[0]` — the first user scenario —
  // which meant the column headed "Current Baseline" was a scenario, the first
  // scenario was compared against itself, and the second against the first.
  const baseline = baselineScenario();
  const selected = multiSelectedIds.map((id) => SCENARIOS.find((s) => s.id === id)).filter(Boolean);

  if (!baseline) {
    container.innerHTML = '<div class="scn-empty-note">'
      + 'This network has not been solved, so there is no baseline to compare '
      + 'scenarios against.</div>';
    return;
  }
  if (!selected.length) {
    container.innerHTML = '<div class="scn-empty-note">'
      + 'No scenario selected. Create one with <strong>Create New Scenario</strong>, '
      + 'or add an existing one from <strong>+ Add scenario to compare</strong>.'
      + '</div>';
    return;
  }

  const rows = ALL_TABLE_ROWS.filter((r) => multiVisibleKeys.includes(r.key));

  // Every column carries its OWN review action.
  //
  // The only route to a scenario's detail was the "Review proposed changes"
  // button on the recommendation card, which opens the RECOMMENDED scenario —
  // correctly, since that card is about that scenario. But with two scenarios
  // compared side by side and the second one selected, that was the only
  // review button on the screen, so it read as "review the selected one" and
  // opened the other. A user can approve the wrong intervention that way.
  //
  // One button per column, under the name of the scenario it opens, removes
  // the ambiguity rather than trying to guess which one "selected" means.
  const theadCols = selected
    .map((s, i) => `<th class="${i === 0 ? 'scn-th-rec2' : ''}" style="text-align:center">
        <div>${scenarioDisplayName(s)}${i === 0 ? ' <span class="scn-sparkle-inline">✦</span>' : ''}</div>
        <button type="button" class="scn-col-review" data-review-scenario="${s.id}"
                title="Open the detailed audit for ${scenarioDisplayName(s)}">Review →</button>
      </th>`)
    .join('');

  const rowsHtml = rows
    .map((row) => {
      const baseVal = baseline[row.key];
      const baseCls = row.kind === 'categorical' ? `scn-risk-${baseline.capacityRiskClass}` : '';

      const cellsHtml = selected
        .map((s, i) => {
          const scnVal = s[row.key];
          const delta = computeRowDelta(row, baseVal, scnVal);
          const valCls = row.kind === 'categorical' ? `scn-risk-${s.capacityRiskClass}` : '';
          return `<td class="${i === 0 ? 'scn-td-rec2' : ''}">
              <div class="${valCls}" style="font-weight:700">${fmtRowValue(row, scnVal)}</div>
              ${scenarioDeltaPillHtml(delta)}
            </td>`;
        })
        .join('');

      // S9: Capacity Risk is the one row wired to the metric drilldown
      // (evidence trail for the risk chain). Other rows deliberately stay
      // non-clickable this pass — e.g. totalCost's drilldown branch would
      // duplicate S6's Total Network Cost ownership, which is out of scope
      // here and gets fixed when S6 is wired in, not by exposing it via S9.
      const isRiskRow = row.key === 'capacityRisk';
      // Two rows are defined only AGAINST the baseline, so the baseline's own
      // cell is the reference rather than a missing value. It rendered
      // "Unavailable", which reads as a gap in the data.
      const baseText = REFERENCE_ONLY_ROWS.has(row.key)
        ? '<span class="text-xs text-muted">Reference</span>'
        : fmtRowValue(row, baseVal);
      return `
        <tr${isRiskRow ? ` class="scn-risk-row-clickable" data-metric-key="capacityRisk" data-scenario-id="${selected[0]?.id || ''}"` : ''}>
          <td class="scn-row2-metric">${scenarioRowMetricCellHtml(row)}</td>
          <td class="${baseCls}">${baseText}</td>
          ${cellsHtml}
        </tr>`;
    })
    .join('');

  container.innerHTML = `
    <table class="scn-data-table2 scn-multi-table2">
      <thead>
        <tr>
          <th>Metric</th>
          <th style="text-align:center">Current Baseline</th>
          ${theadCols}
        </tr>
      </thead>
      <tbody>${rowsHtml}</tbody>
    </table>
  `;

  container.querySelectorAll('tr[data-metric-key="capacityRisk"]').forEach((tr) => {
    tr.style.cursor = 'pointer';
    tr.title = 'View risk evidence';
    tr.addEventListener('click', () => openMetricDrilldown('capacityRisk', tr.dataset.scenarioId));
  });

  // Each column's own review action opens that column's scenario, by id.
  container.querySelectorAll('[data-review-scenario]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      openScenarioDrawer(btn.dataset.reviewScenario);
    });
  });
}

// ─── "NetGravity's take" card ────────────────────────────────
//
// It used to RANK. `rankScenarios()` sorted the compared scenarios on cost,
// picked a winner, and `renderMultiScenarioTakeCard()` wrote the sentence
// recommending it — all in the browser, from figures the mapper had already
// unwrapped. A decision made here is invisible to the audit trail, cannot be
// asserted by the backend suite, and is free to disagree with the same numbers
// in the table beside it.
//
// It now READS a decision: `POST /api/scenarios/compare` ranks the set from the
// authoritative KPI values and their statuses, names the scenario the numbers
// favour, states the caveats, and returns the set's own grounded briefing. The
// scenario's own SCENARIO-scoped explanation comes back on `/simulate`.
//
// Nothing on this card is calculated here. `formatCurrency` renders an amount
// the backend supplied, in the currency the upload stated.

//: The backend's verdict for the currently compared set, keyed on that set —
//: so switching the map toggle, or re-rendering for any other reason, does not
//: ask again. Cleared whenever the selection changes.
let comparisonState = { key: '', status: 'idle', data: null, error: '' };

function comparisonKey(ids) {
  return [...ids].sort().join('|');
}

/** A different set of scenarios is a different analysis. */
function invalidateComparison() {
  comparisonState = { key: '', status: 'idle', data: null, error: '' };
}

/**
 * Ask the backend to rank the compared set, once per set.
 *
 * Never throws: the comparison table beside this card is perfectly good
 * without a verdict, and the card says what it is missing rather than
 * disappearing.
 */
async function loadComparison(ids) {
  const key = comparisonKey(ids);
  if (comparisonState.key === key && comparisonState.status !== 'idle') return;
  comparisonState = { key, status: 'loading', data: null, error: '' };
  renderMultiScenarioTakeCard();
  try {
    const data = await scenarioService.compareScenarios(ids);
    if (comparisonKey(ids) !== comparisonState.key) return;   // selection moved on
    comparisonState = { key, status: 'ready', data, error: '' };
  } catch (err) {
    if (comparisonKey(ids) !== comparisonState.key) return;
    // A TIMEOUT here is a statement about how long this page waited, not
    // about the ranking. Nothing is retried: the one model request the
    // explanation may spend comes from a shared budget, and re-sending would
    // spend a second answering a question already asked.
    comparisonState = {
      key, status: 'failed', data: null,
      error: (err && err.message) || 'the comparison could not be run',
    };
  }
  renderMultiScenarioTakeCard();
}

function takeCheckItem(good, text) {
  return `
    <div class="scn-take-check-item">
      <span class="scn-take-check-icon ${good === null ? 'warn' : (good ? 'good' : 'warn')}">${good ? '✓' : '!'}</span>
      <span>${text}</span>
    </div>`;
}

/** The card head, with where the words came from. Visibility of system status. */
function takeHeadHtml(source, cached) {
  const badge = !source ? ''
    : source === 'llm'
      ? `<span class="scn-take-source" title="${cached
          ? 'Written once by the model for this analysis and saved against it — reopening it spends nothing.'
          : 'Written by the model from the deterministic results. Every figure on this card is supplied by the engine, not by the model.'}">AI${cached ? ' · saved' : ''}</span>`
      : '<span class="scn-take-source muted" title="Written by the deterministic template. No model was reached.">Rule-based</span>';
  return `
    <div class="scn-take-head">
      <span class="scn-take-icon">✨</span>
      <span class="scn-take-title">NetGravity's Recommendation</span>
      ${badge}
    </div>`;
}

/** Up to three supporting numbers, exactly as the backend supplied them. */
function takeFiguresHtml(figures) {
  const shown = (figures || []).slice(0, 3);
  if (!shown.length) return '';
  return `
    <div class="scn-take-figures">
      ${shown.map((f) => {
        const value = f.format === 'currency'
          ? (typeof f.amount === 'number' ? formatCurrency(f.amount) : 'Not available')
          : (f.value || 'Not available');
        return `
        <div class="scn-take-figure">
          <div class="scn-take-figure-label">${f.label}</div>
          <div class="scn-take-figure-value">${value}</div>
        </div>`;
      }).join('')}
    </div>`;
}

/**
 * What a reader can actually DO about this scenario, given what it found.
 *
 * Every item maps to something this application performs. None of them
 * approves anything: opening or closing a site is classified HUMAN_ONLY by
 * governance whatever the economics say, and the backend says so on the
 * comparison — so the actions offered are ways to examine, test and ask,
 * which is what a what-if screen supports.
 *
 * The list is context-specific in the strict sense: each entry is gated on a
 * FINDING in this scenario's own solved result, so a scenario that stranded no
 * demand is never offered "test more capacity", and a scenario that changed no
 * topology is never labelled a structural decision.
 */
function recommendedActions(scn, comparison) {
  const actions = [];
  if (!scn) return actions;

  // The governance verdict is NOT here. Opening or closing a site is a human
  // decision whatever the economics say, and that is a statement about the
  // change rather than something a reader can press — see `governanceHtml`.
  const row = ((comparison && comparison.ranked) || [])
    .find((r) => r.scenario_id === scn.id) || {};
  const action = (scn.request && scn.request.action) || '';

  // Always first: read what the solver actually did before acting on it.
  actions.push({
    label: 'Review the proposed changes',
    detail: 'Sites, corridors and cost, component by component, from this solve.',
    primary: true,
    run: () => openScenarioDrawer(scn.id),
  });

  // What the change asks of the network, and what can be done about it.
  //
  // These used to be one generic entry — "Test more capacity at the busiest
  // site", which named no site and opened an empty capacity form. The backend
  // now says WHICH sites the plan fills, how much extra each carries, what
  // capacity was left closed and which regions have nothing left, so each
  // action can name its target and open the builder already pointed at it.
  const cap = scn.capacityResponse || null;
  const unserved = readKpiValue(scn.scenarioKpis, 'unserved_demand');
  const full = (cap && cap.at_ceiling) || [];
  const idle = (cap && cap.idle) || [];
  const regions = (cap && cap.regions_without_room) || [];

  // Reopening beats building, so it is offered first when both are available.
  if (idle.length && (typeof unserved !== 'number' || unserved > 0)) {
    const site = idle[0];
    actions.push({
      label: `Test reopening ${site.name}`,
      detail: `This plan left ${formatNumber(Math.round(site.capacity || 0))} units `
        + `of capacity closed at ${site.name}`
        + `${site.region ? ` in ${site.region}` : ''}. Opens a scenario that holds `
        + 'it open, to see what the network does with it.',
      run: () => openCreateToolboxWith('OPEN_FACILITY',
        { facilityId: site.id, openMode: 'EXISTING',
          name: `Reopen ${site.name}` }),
    });
  }

  if (full.length) {
    const site = full[0];
    const at = typeof site.util_pct === 'number' ? `${site.util_pct.toFixed(0)}%` : 'its ceiling';
    actions.push({
      label: `Add capacity at ${site.name}`,
      detail: `${site.name} runs at ${at} in this plan`
        + `${typeof site.added_units === 'number' && site.added_units > 0
            ? `, carrying ${formatNumber(Math.round(site.added_units))} units more `
              + 'than it does today' : ''}`
        + '. Opens a capacity scenario on that site — it tests the idea, it does '
        + 'not apply it.',
      run: () => openCreateToolboxWith('CHANGE_CAPACITY', { facilityId: site.id }),
    });
  } else if (typeof unserved === 'number' && unserved > 0) {
    actions.push({
      label: 'Test more capacity at the busiest site',
      detail: `This plan leaves ${formatNumber(Math.round(unserved))} units unserved. `
        + 'Opens a capacity scenario — it tests the idea, it does not apply it.',
      run: () => openCreateToolboxWith('CHANGE_CAPACITY'),
    });
  } else if (String(scn.capacityRisk || '').toLowerCase() === 'high') {
    actions.push({
      label: 'Test more capacity at the busiest site',
      detail: 'Capacity risk stays high in this plan. Opens a capacity scenario '
        + 'to see what relieving it costs.',
      run: () => openCreateToolboxWith('CHANGE_CAPACITY'),
    });
  }

  // A new site is proposed only where the backend found a region with sites at
  // their ceiling, nothing closed to reopen, and no headroom on anything open.
  // Anything weaker recommends building where a reopening would have done.
  if (regions.length) {
    const region = regions[0].region;
    actions.push({
      label: `Site a new facility in ${region}`,
      detail: `Every site in ${region} is at its ceiling in this plan and none `
        + 'is closed, so more demand there has nowhere to go. Opens the '
        + 'greenfield form — you supply the location, capacity and costs.',
      run: () => openCreateToolboxWith('OPEN_FACILITY',
        { openMode: 'NEW', name: `New site in ${region}` }),
    });
  }

  // Growth stated for the whole network, on an upload that names regions.
  // Loading every warehouse with growth happening in one region overstates the
  // case for expanding the ones that are not.
  const scoped = scn.request
    && (scn.request.demand_region || scn.request.demand_product_category);
  if (action === 'CHANGE_DEMAND' && !scoped
      && (NETWORK_REGIONS.length || PRODUCT_CATEGORIES.length)) {
    actions.push({
      label: 'Re-run this growth for one region or category',
      detail: 'This scenario grew every demand row. Your upload states '
        + `${NETWORK_REGIONS.length ? `${NETWORK_REGIONS.length} regions` : ''}`
        + `${NETWORK_REGIONS.length && PRODUCT_CATEGORIES.length ? ' and ' : ''}`
        + `${PRODUCT_CATEGORIES.length ? `${PRODUCT_CATEGORIES.length} product categories` : ''}`
        + ', so the growth can be scoped to where it is actually happening.',
      run: () => openCreateToolboxWith('CHANGE_DEMAND'),
    });
  }

  // The change moved nothing: the solver reaches the same plan without it.
  if (typeof row.change_effect === 'number' && Math.abs(row.change_effect) < 1
      && typeof row.reoptimisation_effect === 'number'
      && Math.abs(row.reoptimisation_effect) >= 1) {
    actions.push({
      label: 'Look at the redesign on its own',
      detail: 'The whole difference here comes from re-optimising the footprint '
        + 'you already have — this change adds nothing to it.',
      run: () => openScenarioDrawer(scn.id),
    });
  }

  // Evidence the briefing itself says it did not have.
  const missing = (scn.explanation && scn.explanation.missing_information) || [];
  if (missing.length) {
    actions.push({
      label: 'Request the missing data',
      detail: `${missing.length} input this scenario needed was not in the upload. `
        + 'Opens the action list, where a request can be sent to whoever owns it.',
      run: () => {
        if (typeof window.navigateToTab === 'function') window.navigateToTab('home');
      },
    });
  }

  // Always last: ask about it in words.
  //
  // This used to call `askChatbotPrompt` alone, which renders into an overlay
  // it does not open: the button did nothing visible and sent a question to
  // the orchestrator anyway. And the question was unanswerable as asked — the
  // chat layer knows nothing about a saved scenario, so it would have analysed
  // the baseline network and answered about that, minutes later.
  actions.push({
    label: 'Chat with Netgravity about this scenario',
    detail: 'Opens the assistant on this scenario\'s own briefing — its cost, '
      + 'what it leaves unserved, and what it asks of each site — and you can '
      + 'ask from there.',
    run: () => openChatAboutScenario(scn, comparison),
  });

  return actions;
}

/**
 * Open the assistant on this scenario, with what is already known about it.
 *
 * Every line below is a value the BACKEND returned: the scenario's own
 * SCENARIO-scoped briefing from `/simulate`, the ranking's verdict from
 * `/compare`, and the capacity account derived from the authoritative
 * per-facility KPIs. Nothing is computed here, nothing is asked of a model,
 * and a field the backend did not supply is left out rather than filled in.
 *
 * The scenario is named in the box as a follow-up question so the reader's
 * next message carries the subject with it — the chat endpoint answers about
 * the network, and a question that does not say which scenario it means would
 * get an answer about a different one.
 */
function openChatAboutScenario(scn, comparison) {
  if (typeof window.openChatbotWithBriefing !== 'function') return;

  const esc = (t) => String(t == null ? '' : t)
    .replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;',
                                   '"': '&quot;', "'": '&#39;' }[c]));

  const name = scenarioDisplayName(scn);
  const card = (scn.explanation && scn.explanation.card) || null;
  const cap = scn.capacityResponse || null;
  const parts = [];

  parts.push(`<p><strong>${esc(name)}</strong></p>`);

  // The ranking's verdict, when this is the scenario it is about.
  if (comparison && comparison.verdict
      && comparison.recommended_scenario_id === scn.id) {
    parts.push(`<p>${esc(comparison.verdict)}</p>`);
  }
  if (card && card.headline) parts.push(`<p>${esc(card.headline)}</p>`);
  if (card && card.meaning) parts.push(`<p>${esc(card.meaning)}</p>`);

  // The three figures the card shows, in the same words and the same currency.
  const figures = (card && card.figures) || [];
  if (figures.length) {
    parts.push(`<p>${figures.map((f) => {
      const value = f.format === 'currency'
        ? (typeof f.amount === 'number' ? formatCurrency(f.amount) : 'not available')
        : (f.value || 'not available');
      return `${esc(f.label)}: <strong>${esc(value)}</strong>`;
    }).join(' · ')}</p>`);
  }

  if (cap && cap.verdict) parts.push(`<p>${esc(cap.verdict)}</p>`);
  const full = (cap && cap.at_ceiling) || [];
  if (full.length) {
    parts.push(`<p>At their ceiling: ${full.map((r) => {
      const at = typeof r.util_pct === 'number' ? `${r.util_pct.toFixed(0)}%` : '—';
      return `${esc(r.name)} (${at}${r.region ? `, ${esc(r.region)}` : ''})`;
    }).join(', ')}.</p>`);
  }
  if (cap && (cap.idle || []).length) {
    parts.push(`<p>Left closed: ${cap.idle.map((r) => esc(r.name)).join(', ')} — `
      + `${formatNumber(Math.round(cap.idle_capacity_units || 0))} units of `
      + 'capacity this plan does not use.</p>');
  }

  if (card && card.warning) parts.push(`<p>${esc(card.warning)}</p>`);
  if (card && card.next_step) parts.push(`<p>${esc(card.next_step)}</p>`);

  // Where the words came from. The card says it above the fold and the
  // assistant must say it too — a briefing that reads as the model's when the
  // template wrote it is the one thing this surface must not do.
  parts.push('<p class="text-xs text-muted">'
    + (card && card.source === 'llm'
        ? 'Written by the model from this scenario\'s solved results; every '
          + 'figure is the engine\'s.'
        : 'Written by the deterministic template — no model was reached for '
          + 'this scenario.')
    + ' Ask a follow-up below and I will answer it from your network.</p>');

  window.openChatbotWithBriefing({
    topic: 'SCENARIO BRIEFING',
    html: parts.join(''),
    question: `In the scenario "${name}", what would it take to serve the `
      + 'demand this plan leaves unserved?',
  });
}

/** One authoritative KPI value, or null when its status refuses it. */
function readKpiValue(kpis, metricId) {
  const result = (kpis || {})[metricId];
  if (!result || result.status !== 'VALID') return null;
  return typeof result.value === 'number' ? result.value : null;
}

/**
 * Open the builder pre-set to one scenario type, so the action lands somewhere.
 *
 * `options` points it at the specific thing the action named. An action that
 * says "add capacity at Nagpur DC" and then opens an empty form with the first
 * site in the list selected has told the reader to do the work again, and has
 * invited them to run it against the wrong site.
 *
 * Only fields the chosen type actually renders are set; the rest are ignored,
 * and every value stays editable. Nothing is submitted here.
 */
function openCreateToolboxWith(type, options = {}) {
  openCreateToolbox();
  document.querySelectorAll('.scn-type-card').forEach((card) => {
    card.classList.toggle('active', card.dataset.type === type);
  });
  renderToolboxDynamicFields(type);

  if (options.name) {
    const nameInput = document.getElementById('toolbox-scenario-name');
    if (nameInput) nameInput.value = options.name;
  }
  // OPEN_FACILITY renders "keep an existing site open" and "propose a new one"
  // as two panels behind a mode select, so the mode is set before the facility.
  if (options.openMode) {
    const mode = document.getElementById('toolbox-open-mode');
    if (mode) {
      mode.value = options.openMode;
      mode.dispatchEvent(new Event('change'));
    }
  }
  if (options.facilityId) {
    const select = document.getElementById('toolbox-facility');
    // Only if the option is really there. Assigning an unknown value to a
    // <select> silently leaves it on whatever was selected before, which is
    // how an action naming one site would run against another.
    if (select && [...select.options].some((o) => o.value === options.facilityId)) {
      select.value = options.facilityId;
    }
  }
  if (typeof options.amount === 'number' && Number.isFinite(options.amount)) {
    const amount = document.getElementById('toolbox-amount');
    if (amount) amount.value = String(options.amount);
  }
  if (options.productCategory) {
    const category = document.getElementById('toolbox-demand-category');
    // Same rule as the facility above: an unknown value would leave the
    // select on "every category" while the label promised one.
    if (category && [...category.options]
        .some((o) => o.value === options.productCategory)) {
      category.value = options.productCategory;
    }
  }
  if (options.region) {
    const region = document.getElementById('toolbox-demand-region');
    if (region && [...region.options].some((o) => o.value === options.region)) {
      region.value = options.region;
    }
  }
}

/* The builder, reachable from the screens that have a reason to open it.
   The Forecast page recommends testing the network at the rate its own
   projection implies, and that recommendation is worth nothing if acting on
   it means navigating here and re-typing a number this application already
   computed. Nothing is submitted — the form opens filled in and a person
   presses Run. */
if (typeof window !== 'undefined') {
  window.openScenarioBuilderWith = openCreateToolboxWith;
}

function takeActionsHtml(actions) {
  if (!actions.length) return '';
  // Actions now name the sites they are about — "Add capacity at Toronto DC" —
  // and those names came out of an uploaded spreadsheet. Nothing here has ever
  // carried markup deliberately, so escaping costs nothing and closes the way
  // in.
  const esc = (t) => String(t == null ? '' : t)
    .replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;',
                                   '"': '&quot;', "'": '&#39;' }[c]));
  return `
    <div class="scn-take-section-title" style="margin-top:14px">Recommended actions</div>
    <div class="scn-take-actions">
      ${actions.map((a, i) => `
        <button type="button" class="scn-take-action${a.primary ? ' primary' : ''}"
                data-action-index="${i}">
          <span class="scn-take-action-label">${esc(a.label)}</span>
          <span class="scn-take-action-detail">${esc(a.detail)}</span>
        </button>`).join('')}
    </div>`;
}

/**
 * The whole recommendation in four lines, before any prose.
 *
 * A reader deciding whether to act needs four things: what was changed, what
 * it costs against today, what it does to service, and whether the network
 * can physically carry it. Those were spread across a verdict sentence, three
 * paragraphs of narration, a figure strip and a capacity section — all true,
 * none of it answering the question in one place.
 *
 * EVERY VALUE HERE IS THE BACKEND'S. The cost and the deltas come from the
 * ranking (`/compare`), the service figures from the scenario's own
 * authoritative KPIs, the capacity counts from `capacity_response`. This
 * formats and lays out; it does not decide, and it does not compute a
 * business value. A figure the backend did not supply is left out — never
 * shown as a dash that reads like a zero.
 */
function atAGlanceHtml(scn, comparison) {
  const row = ((comparison && comparison.ranked) || [])
    .find((r) => r.scenario_id === scn.id) || {};
  const cap = scn.capacityResponse || null;
  const rows = [];

  // 1. WHAT YOU CHANGED — from the request the user actually submitted, so it
  //    is their own words back, not an interpretation of them.
  const asked = requestSummary(scn);
  if (asked) rows.push(['You changed', asked]);

  // 2. WHAT IT COSTS — the figure and its distance from today, together. They
  //    were a tile and a paragraph, and a reader had to hold one while
  //    reading the other.
  const cost = readKpiValue(scn.scenarioKpis, 'business_network_cost');
  if (typeof cost === 'number') {
    const delta = row.cost_delta;
    const vsToday = typeof delta === 'number'
      ? ` <span class="scn-glance-delta ${delta < 0 ? 'good' : 'bad'}">${
          delta < 0 ? '↓' : '↑'} ${formatCurrency(Math.abs(delta))} vs today</span>`
      : '';
    rows.push(['Cost', `<strong>${formatCurrency(cost)}</strong>${vsToday}`]);
  }

  // 3. HOW MUCH OF THAT IS THE CHANGE. The single most misread thing on this
  //    screen: the baseline pins today's footprint open and a scenario may
  //    close sites, so most of the gap is usually the redesign — which means
  //    "demand +50%" can read as a saving. Said in one line, in the currency.
  const attribution = (comparison && comparison.attribution) || null;
  if (attribution && typeof attribution.reoptimisation_amount === 'number') {
    const reopt = formatCurrency(Math.abs(attribution.reoptimisation_amount));
    if (attribution.change_direction === 'none') {
      rows.push(['Of which', `all ${reopt} is re-optimising today's footprint —
        <strong>the change itself moves nothing</strong>`]);
    } else if (typeof attribution.change_amount === 'number') {
      const change = formatCurrency(Math.abs(attribution.change_amount));
      rows.push(['Of which', `${reopt} is re-optimising today's footprint;
        <strong>the change itself ${
          attribution.change_direction === 'adds' ? 'adds' : 'saves'} ${change}</strong>`]);
    }
  }

  // 4. SERVICE. A cost ranking's blind spot, so it is never optional here.
  const fill = readKpiValue(scn.scenarioKpis, 'demand_fill_rate');
  const unserved = readKpiValue(scn.scenarioKpis, 'unserved_demand');
  if (typeof fill === 'number' || typeof unserved === 'number') {
    const served = typeof fill === 'number'
      ? `<strong>${(fill * 100).toFixed(1)}% of demand served</strong>` : '';
    const missed = typeof unserved === 'number' && unserved > 0
      ? `${served ? ' · ' : ''}<span class="scn-glance-delta bad">${
          formatNumber(Math.round(unserved))} units nobody can reach</span>`
      : (served ? ' · all of it reachable' : '');
    rows.push(['Service', `${served}${missed}`]);
  }

  // 5. CAPACITY. Whether the network can physically carry it, and what is
  //    left to carry it with.
  if (cap) {
    const bits = [];
    if (cap.at_ceiling_count) {
      bits.push(`<strong>${cap.at_ceiling_count} site${
        cap.at_ceiling_count === 1 ? '' : 's'} at their ceiling</strong>`);
    }
    if (cap.idle_count && cap.idle_capacity_units) {
      bits.push(`${formatNumber(Math.round(cap.idle_capacity_units))} units left closed`);
    }
    if (!bits.length && typeof cap.open_headroom_units === 'number') {
      bits.push(`${formatNumber(Math.round(cap.open_headroom_units))} units of room to spare`);
    }
    if (bits.length) rows.push(['Capacity', bits.join(' · ')]);
  }

  if (!rows.length) return '';
  return `
    <dl class="scn-glance">
      ${rows.map(([label, value]) => `
        <dt>${label}</dt>
        <dd>${value}</dd>`).join('')}
    </dl>`;
}

/**
 * The change the user asked for, in their own terms.
 *
 * Read from `scn.request` — the body this screen submitted — rather than from
 * the solved result, because "what did I ask for" and "what did the solver do
 * with it" are two different questions and the card answers the second one
 * everywhere else.
 */
function requestSummary(scn) {
  const req = scn.request || {};
  const esc = (t) => String(t == null ? '' : t)
    .replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;',
                                   '"': '&quot;', "'": '&#39;' }[c]));
  const scope = [req.demand_region, req.demand_product_category]
    .filter(Boolean).map(esc).join(', ');

  if (typeof req.demand_multiplier === 'number') {
    const pct = (req.demand_multiplier - 1) * 100;
    return `Demand ${pct >= 0 ? '+' : ''}${pct.toFixed(0)}%`
      + (scope ? ` on ${scope}` : ' across the whole network');
  }
  if (typeof req.transport_cost_multiplier === 'number') {
    const pct = (req.transport_cost_multiplier - 1) * 100;
    return `Freight rates ${pct >= 0 ? '+' : ''}${pct.toFixed(0)}%`;
  }
  if (typeof req.capacity_delta_units === 'number' && req.capacity_delta_units) {
    return `Capacity ${req.capacity_delta_units > 0 ? '+' : '−'}`
      + `${formatNumber(Math.abs(req.capacity_delta_units))} units at `
      + `${esc((req.facility_ids || []).join(', ')) || 'the named site'}`;
  }
  if (typeof req.sla_days_delta === 'number' && req.sla_days_delta) {
    return `Delivery promise ${req.sla_days_delta > 0 ? '+' : ''}`
      + `${req.sla_days_delta} day${Math.abs(req.sla_days_delta) === 1 ? '' : 's'}`;
  }
  if (req.new_facility && req.new_facility.name) {
    return `A new site at ${esc(req.new_facility.name)}`;
  }
  if (req.action === 'CLOSE_FACILITY') {
    return `Close ${esc((req.facility_ids || []).join(', ')) || 'the named site'}`;
  }
  if (req.action === 'OPEN_FACILITY') {
    return `Hold ${esc((req.facility_ids || []).join(', ')) || 'the named site'} open`;
  }
  return '';
}

/**
 * What the change asks of the sites that have to absorb it.
 *
 * The question a demand scenario is actually asking, and the one the card did
 * not answer: raising demand by half produced the same cost narration as
 * raising it by five percent. Which sites the plan fills, how hard the rest
 * have to run, what capacity is sitting closed, and where the network has
 * nothing left — those are the parts that differ.
 *
 * Every figure and the verdict sentence are the backend's, from
 * `capacity_response`. This formats them; the only arithmetic here is
 * `formatNumber` on a unit count and `formatCurrency` on nothing at all.
 */
function capacityResponseHtml(scn, { title = true, rows = 5 } = {}) {
  const cr = scn && scn.capacityResponse;
  if (!cr) return '';

  // The card shows fewer than the drawer does. It already carries the
  // verdict, the figures, the trade-off band and the actions, and a
  // recommendation whose primary action has been pushed below the fold by its
  // own supporting detail is a recommendation nobody acts on. The count that
  // is NOT shown is still stated, so a truncated list never reads as a
  // complete one.
  const full = (cr.at_ceiling || []).slice(0, rows);
  const harder = (cr.working_harder || []).slice(0, rows);
  const idle = (cr.idle || []).slice(0, rows);
  const regions = cr.regions_without_room || [];
  if (!cr.verdict && !full.length && !harder.length && !idle.length) return '';

  const units = (n) => (typeof n === 'number' ? formatNumber(Math.round(n)) : '—');
  const pct = (n) => (typeof n === 'number' ? `${n.toFixed(1)}%` : '—');
  // Site and region names came out of a spreadsheet somebody uploaded, and
  // this block is written with innerHTML.
  const esc = (t) => String(t == null ? '' : t)
    .replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;',
                                   '"': '&quot;', "'": '&#39;' }[c]));

  // What kind of site, and where. Whether the ceiling is a PLANT's or a
  // warehouse's is the difference between "we cannot make enough" and "we
  // cannot move enough", and they are not the same problem.
  const where = (row) => {
    const bits = [row.role, row.region].filter(Boolean).map(esc);
    return bits.length
      ? `<span class="scn-cap-region">${bits.join(' · ')}</span>` : '';
  };

  // One site's line: what it must run at, and how much of that is new.
  const load = (row, tag) => `
    <div class="scn-cap-row">
      <span class="scn-cap-site">
        ${esc(row.name)}${where(row)}
      </span>
      <span class="scn-cap-util ${tag}">${pct(row.util_pct)}</span>
      <span class="scn-cap-delta">${
        typeof row.baseline_util_pct === 'number'
          ? `from ${pct(row.baseline_util_pct)}` : ''}${
        typeof row.added_units === 'number' && row.added_units > 0
          ? ` · +${units(row.added_units)} units` : ''}</span>
    </div>`;

  const more = (shown, total) => (total > shown.length
    ? `<div class="scn-cap-more">and ${total - shown.length} more</div>` : '');

  return `
    ${title ? `<div class="scn-take-section-title" style="margin-top:14px">
      What this asks of the network
    </div>` : ''}
    ${cr.verdict ? `<p class="scn-take-para">${cr.verdict}</p>` : ''}
    ${full.length ? `
      <div class="scn-cap-group-label">Run to their ceiling — no room left</div>
      ${full.map((r) => load(r, 'full')).join('')}
      ${more(full, cr.at_ceiling_count || full.length)}` : ''}
    ${harder.length ? `
      <div class="scn-cap-group-label">Working harder, still with room</div>
      ${harder.map((r) => load(r, 'hot')).join('')}
      ${more(harder, cr.working_harder_count || harder.length)}` : ''}
    ${idle.length ? `
      <div class="scn-cap-group-label">
        Left closed — ${units(cr.idle_capacity_units)} units of capacity unused
      </div>
      ${idle.map((r) => `
        <div class="scn-cap-row">
          <span class="scn-cap-site">
            ${esc(r.name)}${where(r)}
          </span>
          <span class="scn-cap-util idle">closed</span>
          <span class="scn-cap-delta">${units(r.capacity)} units of capacity</span>
        </div>`).join('')}
      ${more(idle, cr.idle_count || idle.length)}` : ''}
    ${regions.length ? `
      <p class="scn-take-para" style="margin-top:8px">
        <strong>${regions.map((r) => esc(r.region)).join(', ')}</strong>
        ${regions.length === 1 ? 'has' : 'have'} no room left at all — every site
        there is at its ceiling and none is closed, so serving more demand from
        within ${regions.length === 1 ? 'that region' : 'those regions'} means a
        new site.
      </p>` : ''}
    ${!cr.regions_known && full.length ? `
      <p class="scn-take-para text-muted" style="margin-top:8px">
        Your upload does not state a region for its sites, so this cannot say
        which region a new facility belongs in. Add a region column and it will.
      </p>` : ''}`;
}

/* The attribution paragraph that stood here is gone, not the attribution.
   It said the same thing as the glance block's "Of which" row directly above
   it AND as the backend's own sentence in the collapsed technical detail
   (`_scenario_explanation` passes `attribution["text"]` into `card.details`).
   Three copies of the one point that stops a demand INCREASE reading as a
   saving: the row carries it above the fold in the project's currency, the
   backend's sentence spells it out for whoever opens the detail. */

/**
 * The one thing not to miss, in one band.
 *
 * Two sources reach here and they say different things: the RANKING's warning
 * is about the trade-off it just made (cheapest, but service or capacity is
 * not acceptable), and the BRIEFING's is the risk the reasoning found. Both
 * matter, so neither is dropped — but two stacked amber blocks read as an
 * alarm rather than as a caveat, so they share one band.
 */
function warningBandHtml(rankingWarning, briefingWarning) {
  const lines = [rankingWarning, briefingWarning]
    .map((t) => (t || '').trim())
    .filter((t, i, all) => t && all.indexOf(t) === i);
  if (!lines.length) return '';
  return `
    <div class="scn-take-tradeoff">
      <span class="scn-take-tradeoff-icon">⚠️</span>
      <span>${lines.map((t) => `<span class="scn-take-tradeoff-line">${t}</span>`).join('')}</span>
    </div>`;
}

/**
 * Whether two sentences state the same finding.
 *
 * The card leads with the ENGINE's verdict and then the model's headline about
 * the same ranking, and on a live run those were: "Freight +15% costs less than
 * the network you run today, and less than the 1 other option compared" and
 * "Freight +15% costs less than the network the business runs today and less
 * than Demand +30%". One message printed twice, in two voices — and the second
 * copy pushed the recommended actions down behind the chat button.
 *
 * Compared on the first six significant words, which is where a sentence makes
 * its claim. An exact-string check caught none of this.
 */
function saysTheSameThing(a, b) {
  const lead = (text) => String(text || '')
    .toLowerCase()
    .replace(/[^a-z0-9%+\-\s]/g, ' ')
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 6)
    .join(' ');
  const left = lead(a);
  const right = lead(b);
  return Boolean(left) && left === right;
}

/**
 * The governance verdict, where the other statements about this recommendation
 * are — not in the action list.
 *
 * It was a disabled button at the head of "Recommended actions": a control
 * that does nothing, above the controls that do. Opening or closing a site is
 * classified HUMAN_ONLY by the backend whatever the economics say, and that is
 * a fact about the change, not something a reader can press.
 */
function governanceHtml(comparison, focus) {
  const governance = comparison && comparison.governance;
  if (!governance || governance.classification !== 'HUMAN_ONLY') return '';
  if (comparison.recommended_scenario_id !== focus.id) return '';
  return `
    <div class="scn-take-governance">
      <span class="scn-take-governance-tag">Human decision</span>
      <span>${governance.note}</span>
    </div>`;
}

/**
 * The words the reasoning wrote, under one heading, below the answer.
 *
 * These were the second and third things on the card and they are the fourth
 * and fifth thing a reader needs: the verdict and the figures say what
 * happened, this says why it happened and what it means. Keeping it above the
 * actions meant the primary control sat under three paragraphs.
 *
 * It is NOT collapsed. The AI briefing is the thing the reader asked for, and
 * a recommendation folded behind a disclosure triangle is one nobody reads —
 * this is a reordering, not a demotion.
 */
function narrativeHtml(card, verdict, aboutTheSet, comparedCount, focus) {
  let headline = card && card.headline
    && !saysTheSameThing(card.headline, verdict) ? card.headline : '';
  const meaning = (card && card.meaning) || '';

  // ONE SENTENCE, ONCE. The headline is capped at 140 characters and the
  // model writes longer ones, so a single finding arrived as a truncated
  // headline followed by the whole of itself:
  //
  //   "...while maintaining a high demand fill rate…"
  //   "...while maintaining a high demand fill rate and service within SLA."
  //
  // `card_from_briefing` skips its own duplication guard when the headline is
  // truncated — correctly, since dropping the BODY would lose the rest of the
  // sentence. Dropping the truncated copy loses nothing at all.
  if (headline && meaning) {
    const stem = headline.replace(/[\u2026.]+$/, '').trim().toLowerCase();
    if (stem && meaning.trim().toLowerCase().startsWith(stem)) headline = '';
  }
  if (!headline && !meaning) return '';
  return `
    <div class="scn-take-section-title" style="margin-top:16px">
      ${card && card.source === 'llm' ? 'What this means' : 'What the figures say'}
    </div>
    ${headline ? `<p class="scn-take-para" style="margin-bottom:8px"><strong>${
      aboutTheSet ? `Across the ${comparedCount} compared`
                  : scenarioDisplayName(focus)}:</strong> ${headline}</p>` : ''}
    ${meaning ? `<p class="scn-take-para">${meaning}</p>` : ''}`;
}

/** The technical account, collapsed. One conclusion is said once above it. */
function takeDetailsHtml(lines) {
  const items = (lines || []).filter(Boolean);
  if (!items.length) return '';
  return `
    <details class="scn-take-details">
      <summary>How this was decided</summary>
      <ul>${items.map((d) => `<li>${d}</li>`).join('')}</ul>
    </details>`;
}

function renderMultiScenarioTakeCard() {
  const container = document.getElementById('scn-multi-take-card');
  if (!container) return;

  const baseline = baselineScenario();
  const selected = multiSelectedIds
    .map((id) => SCENARIOS.find((s) => s.id === id))
    .filter((s) => s && s.id !== BASELINE_SCENARIO_ID);

  if (!baseline || !selected.length) {
    container.innerHTML = takeHeadHtml('', false) + `
      <div class="scn-empty-note">
        ${baseline
          ? 'No scenario is selected, so there is nothing to recommend. Create '
            + 'one and it will be ranked against your solved network.'
          : 'This network has not been solved, so there is no baseline to judge '
            + 'a scenario against.'}
      </div>`;
    return;
  }

  const ids = selected.map((s) => s.id);
  const key = comparisonKey(ids);
  if (comparisonState.key !== key) {
    // Fired without awaiting: it re-renders this card when it lands.
    loadComparison(ids);
  }
  const state = comparisonState.key === key ? comparisonState : { status: 'loading' };

  if (state.status === 'loading' || state.status === 'idle') {
    container.innerHTML = takeHeadHtml('', false) + `
      <div class="scn-empty-note">
        Ranking ${selected.length === 1 ? 'this scenario' : `these ${selected.length} scenarios`}
        against your solved network…
      </div>`;
    return;
  }

  if (state.status === 'failed') {
    // Say what is missing and what still stands. The table beside this card
    // is unaffected, and saying so is the difference between a degraded panel
    // and a screen the reader stops trusting.
    container.innerHTML = takeHeadHtml('', false) + `
      <div class="scn-empty-note">
        The ranking could not be produced — ${state.error}. The comparison table
        beside this is unaffected: those figures come from each scenario's own
        solve. Reopen this tab to try again.
      </div>`;
    return;
  }

  const comparison = state.data || {};
  const ranked = comparison.ranked || [];
  const recommendedId = comparison.recommended_scenario_id;

  // WHICH scenario the actions are about: the one the map toggle is showing,
  // falling back to the one the backend recommends. Taking "whichever is
  // first" instead meant switching the map to another scenario left the
  // actions describing the previous one.
  const focus = selected.find((s) => s.id === mapActiveId)
    || selected.find((s) => s.id === recommendedId)
    || selected[0];

  // WHICH briefing explains what is on screen.
  //
  // Comparing several, the question is "why this one rather than those" — and
  // that is the COMPARISON's own briefing, about the set. Comparing one, there
  // is nothing to weigh it against, and the scenario's own SCENARIO-scoped
  // briefing is the answer; the backend produces no comparison briefing for a
  // set of one, so there is nothing to fall back from.
  //
  // Rendering the scenario's card under a set of three was the failure this
  // avoids: an explanation of one scenario beneath a verdict about three.
  const comparisonCard = (comparison.explanation && comparison.explanation.card) || null;
  const scenarioCard = (focus.explanation && focus.explanation.card) || null;
  const card = (selected.length > 1 && comparisonCard) ? comparisonCard : scenarioCard;
  const aboutTheSet = card === comparisonCard && Boolean(comparisonCard);
  // The briefing the card came from, so the grounding notes below belong to
  // the words above them rather than to the other one.
  const briefing = aboutTheSet ? comparison.explanation : focus.explanation;
  const cached = Boolean(card && card.cached);
  const source = (card && card.source) || '';

  // The verdict is the backend's sentence, not one composed here.
  const verdict = comparison.verdict || '';
  const caveats = comparison.caveats || [];
  const warning = comparison.warning || '';

  const rest = ranked
    .filter((r) => r.scenario_id !== focus.id)
    .map((r) => `
    <div class="flex items-center justify-between text-xs" style="padding:4px 0;border-bottom:1px solid var(--border-light)">
      <span>${r.name || r.scenario_id}${r.scenario_id === recommendedId ? ' <span class="tag tag-success" style="font-size:9px">ranked first</span>' : ''}</span>
      <span style="font-weight:700;color:${(r.cost_delta ?? 0) < 0 ? 'var(--green)' : 'var(--red)'}">
        ${r.cost_delta === null || r.cost_delta === undefined
          ? '—' : `${r.cost_delta < 0 ? '↓' : '↑'} ${formatCurrency(Math.abs(r.cost_delta))}`}
      </span>
    </div>`).join('');

  const actions = recommendedActions(focus, comparison);

  // THE ORDER A READER NEEDS, not the order the pieces were built in.
  //
  // Verdict → the four facts at a glance → the risk → what to do → the
  // reasoning behind it. Previously the prose came second and third, the
  // figures fourth, the capacity account fifth and the actions ninth, so the
  // question this screen exists to answer was below the fold under three
  // paragraphs that restated the figures in a different format.
  //
  // Nothing is dropped. The narration moves under "Why this, in full", which
  // is open by default when a model wrote it — that IS the AI recommendation
  // and hiding it would be answering a different complaint.
  container.innerHTML = takeHeadHtml(source, cached)
    + `<div class="scn-take-headline">${verdict}</div>`
    + atAGlanceHtml(focus, comparison)
    + warningBandHtml(warning, card && card.warning)
    + governanceHtml(comparison, focus)
    + (card && card.next_step
        ? `<div class="scn-take-section-title">Next step</div>
           <div class="scn-take-checklist">${takeCheckItem(null, card.next_step)}</div>` : '')
    + takeActionsHtml(actions)
    + narrativeHtml(card, verdict, aboutTheSet, selected.length, focus)
    + capacityResponseHtml(focus, { rows: 3 })
    + takeFiguresHtml(card && card.figures)
    + (rest ? `
      <div class="scn-take-section-title" style="margin-top:14px">Also compared</div>
      ${rest}` : '')
    + takeDetailsHtml([
        ...caveats,
        ...((card && card.details) || []),
        // Why these words are the deterministic template's rather than the
        // model's, when they are. The fallback is meant to be seamless, and
        // that is exactly what made it invisible: the reasoning degraded for
        // the whole life of the server and nothing said so.
        ...((briefing && briefing.grounding && briefing.grounding.warnings) || []),
      ])
    + `<div class="text-xs text-muted" style="margin-top:12px;line-height:1.5">
        Ranked by the engine on solved network cost from the MILP. No figure on
        this card is estimated.
      </div>`;

  container.querySelectorAll('[data-action-index]').forEach((btn) => {
    const item = actions[Number(btn.dataset.actionIndex)];
    if (!item || typeof item.run !== 'function') return;
    btn.addEventListener('click', item.run);
  });
}

// ─── Open Scenario Detail Drawer ────────────────────────────
export function openScenarioDrawer(scenarioId) {
  const scn = SCENARIOS.find((s) => s.id === scenarioId);
  const baseline = baselineScenario();
  const overlay = document.getElementById('scenario-drawer-overlay');
  const content = document.getElementById('scenario-drawer-content');
  if (!overlay || !content) return;

  if (!scn) {
    content.innerHTML = '<div class="text-xs text-muted">That scenario is no '
      + 'longer available.</div>';
    overlay.classList.add('visible');
    return;
  }

  // Everything below is read from the two solved states and from the request
  // the user actually submitted.
  //
  // This drawer used to read `scn.changes`, `scn.assumptions`,
  // `scn.robustnessTests`, `scn.objective` and `scn.aiAssessment` — five fields
  // the mapper leaves null on purpose, because no engine produces them. The
  // "Resilience Stress Testing (+15% Demand Surge)" section rendered nothing
  // under a heading that claimed a test had run, and the header showed
  // `undefined` for the scenario's status and description.

  const changes = describeScenarioChanges(scn);
  const moved = laneMovements(scn).slice(0, 8);

  const requestRows = [];
  const req = scn.request || {};
  if (req.action) requestRows.push(['Change requested', String(req.action).replace(/_/g, ' ')]);
  if ((req.facility_ids || []).length) {
    requestRows.push(['Facilities named', req.facility_ids.join(', ')]);
  }
  if (req.capacity_delta_units != null) {
    requestRows.push(['Capacity adjustment',
      `${req.capacity_delta_units > 0 ? '+' : ''}${formatNumber(req.capacity_delta_units)} units/period`]);
  }
  if (req.demand_multiplier != null) {
    requestRows.push(['Demand', `x${req.demand_multiplier} on every demand row`]);
  }
  if (req.transport_cost_multiplier != null) {
    requestRows.push(['Freight rates', `x${req.transport_cost_multiplier}`]);
  }
  if (req.sla_days_delta != null) {
    requestRows.push(['Delivery promise',
      `${req.sla_days_delta > 0 ? '+' : ''}${req.sla_days_delta} days`]);
  }
  if (req.new_facility) {
    requestRows.push(['New site',
      `${req.new_facility.name} at ${Number(req.new_facility.latitude).toFixed(4)}, `
      + `${Number(req.new_facility.longitude).toFixed(4)}`]);
    requestRows.push(['Stated capacity',
      `${formatNumber(req.new_facility.capacity_units_per_period)} units/period`]);
  }

  const costRow = (label, key) => {
    const before = baseline ? baseline[key] : null;
    const after = scn[key];
    if (after === null || after === undefined) return '';
    const delta = (typeof before === 'number' && typeof after === 'number')
      ? after - before : null;
    return `
      <tr>
        <td>${label}</td>
        <td style="text-align:right">${before === null || before === undefined ? '—' : formatCurrency(before)}</td>
        <td style="text-align:right;font-weight:700">${formatCurrency(after)}</td>
        <td style="text-align:right;color:${(delta ?? 0) <= 0 ? 'var(--green)' : 'var(--red)'}">
          ${delta === null ? '—' : `${delta < 0 ? '↓' : '↑'} ${formatCurrency(Math.abs(delta))}`}
        </td>
      </tr>`;
  };

  content.innerHTML = `
    <div style="margin-bottom:18px">
      <div class="flex items-center gap-xs mb-xs">
        <span class="tag ${scn.feasible ? 'tag-success' : 'tag-danger'}" style="font-size:10px;padding:3px 8px">${scn.feasible ? 'SOLVED' : 'NOT FEASIBLE'}</span>
        <span class="provenance-badge model-fact">MILP EVALUATED</span>
      </div>
      <h3 style="font-size:20px;font-weight:800;color:var(--text-1)">${scenarioDisplayName(scn)}</h3>
      <p style="font-size:12.5px;color:var(--text-2);margin-top:4px">
        Solved against snapshot <code>${scn.snapshotId || '—'}</code> by execution
        <code>${scn.executionId || '—'}</code>.
      </p>
    </div>

    <div class="grid-2 mb-md" style="gap:var(--space-sm)">
      <div style="background:var(--bg-subtle);padding:10px 14px;border-radius:var(--r-md);border:1px solid var(--border-light)">
        <span class="text-xs text-muted">Total network cost</span>
        <div style="font-size:16px;font-weight:800;color:var(--text-1)">
          ${formatCurrency(scn.totalCost)}
          ${typeof scn.costChange === 'number' && scn.costChange !== 0
            ? `<span class="text-xs" style="color:${scn.costChange < 0 ? 'var(--green)' : 'var(--red)'}">(${scn.costChange < 0 ? '↓ ' : '↑ '}${Math.abs(scn.costChange).toFixed(1)}%)</span>`
            : ''}
        </div>
      </div>
      <div style="background:var(--bg-subtle);padding:10px 14px;border-radius:var(--r-md);border:1px solid var(--border-light)">
        <span class="text-xs text-muted">Demand fill rate</span>
        <div style="font-size:16px;font-weight:800;color:${(scn.fillRate ?? 0) >= 95 ? 'var(--green)' : 'var(--red)'}">
          ${scn.fillRate === null || scn.fillRate === undefined ? 'Unavailable' : `${scn.fillRate.toFixed(1)}%`}
        </div>
      </div>
    </div>

    <!-- What was asked for -->
    <div class="scn-section-box">
      <h4 style="font-size:13px;font-weight:700;color:var(--text-1);margin-bottom:8px">What you asked for</h4>
      ${requestRows.length ? requestRows.map(([label, value]) => `
        <div class="flex items-center justify-between text-xs py-xs" style="border-bottom:1px solid var(--border-light)">
          <span style="color:var(--text-2)">${label}</span>
          <span style="font-weight:600;text-align:right">${value}</span>
        </div>`).join('')
        : '<div class="text-xs text-muted">No request parameters were recorded.</div>'}
      ${(scn.overrides || []).length ? `
        <div class="text-xs text-muted" style="margin-top:8px;line-height:1.5">
          Applied to the network as:<br>
          ${scn.overrides.map((o) => `<code style="font-size:11px">${o}</code>`).join('<br>')}
        </div>` : ''}
    </div>

    <!-- What the solver did with it -->
    <div class="scn-section-box">
      <h4 style="font-size:13px;font-weight:700;color:var(--text-1);margin-bottom:8px">What the solver changed</h4>
      ${changes.length ? changes.map((c) => `
        <div class="scn-change-row">
          <div><strong>${c.text}</strong></div>
        </div>`).join('')
        : '<div class="text-xs text-muted">The solver reached the same plan as '
          + 'the baseline. This change moved nothing — which is itself the '
          + 'answer.</div>'}
      ${moved.length ? `
        <table class="scn-data-table" style="font-size:12px;margin-top:10px;width:100%">
          <thead><tr><th>Corridor</th><th style="text-align:right">Baseline</th><th style="text-align:right">Scenario</th><th style="text-align:right">Shift</th></tr></thead>
          <tbody>
            ${moved.map((m) => `
              <tr>
                <td>${m.lane.replace('->', ' → ')}</td>
                <td style="text-align:right">${formatNumber(Math.round(m.before))}</td>
                <td style="text-align:right;font-weight:700">${formatNumber(Math.round(m.after))}</td>
                <td style="text-align:right;color:${m.shift > 0 ? 'var(--primary)' : 'var(--text-2)'}">
                  ${m.shift > 0 ? '↑' : '↓'} ${formatNumber(Math.round(Math.abs(m.shift)))}
                </td>
              </tr>`).join('')}
          </tbody>
        </table>` : ''}
    </div>

    <!-- What the change asks of the sites that absorb it.

         The section above says which corridors moved volume, which is the
         audit of what happened and the wrong grain for "we raised demand by
         half": a planner is asking which sites have to carry it and what
         happens where they cannot. Same figures as the recommendation card,
         from the same backend block, so the two cannot disagree. -->
    ${scn.capacityResponse ? `
    <div class="scn-section-box">
      <h4 style="font-size:13px;font-weight:700;color:var(--text-1);margin-bottom:8px">What this asks of the network</h4>
      ${capacityResponseHtml(scn, { title: false })}
    </div>` : ''}

    <!-- Cost decomposition, both sides -->
    <div class="scn-section-box">
      <h4 style="font-size:13px;font-weight:700;color:var(--text-1);margin-bottom:8px">Cost, component by component</h4>
      <table class="scn-data-table" style="font-size:12px;width:100%">
        <thead><tr><th>Component</th><th style="text-align:right">Baseline</th><th style="text-align:right">Scenario</th><th style="text-align:right">Change</th></tr></thead>
        <tbody>
          ${costRow('Transport', 'transportCost')}
          ${costRow('Fixed facility', 'fixedCost')}
          ${costRow('Handling', 'handlingCost')}
          ${costRow('Inventory', 'inventoryCost')}
          ${costRow('Opening', 'openingCost')}
          ${costRow('Closure', 'closureCost')}
          <tr style="font-weight:800;background:var(--bg-subtle)">
            <td>Total network cost</td>
            <td style="text-align:right">${formatCurrency(baseline ? baseline.totalCost : null)}</td>
            <td style="text-align:right">${formatCurrency(scn.totalCost)}</td>
            <td style="text-align:right">${typeof scn.costChange === 'number' ? `${scn.costChange < 0 ? '↓' : '↑'} ${Math.abs(scn.costChange).toFixed(1)}%` : '—'}</td>
          </tr>
        </tbody>
      </table>
      <div class="text-xs text-muted" style="margin-top:8px;line-height:1.5">
        The shortage penalty the solver uses to decide which demand to strand is
        excluded — nobody pays it. Demand the plan does not serve is reported as
        a quantity below, not as money.
      </div>
    </div>

    <!-- Service -->
    <div class="scn-section-box">
      <h4 style="font-size:13px;font-weight:700;color:var(--text-1);margin-bottom:8px">Demand and service</h4>
      <div class="flex items-center justify-between text-xs py-xs" style="border-bottom:1px solid var(--border-light)">
        <span style="color:var(--text-2)">Total demand</span>
        <span style="font-weight:600">${formatNumber(scn.totalDemand)} units</span>
      </div>
      <div class="flex items-center justify-between text-xs py-xs" style="border-bottom:1px solid var(--border-light)">
        <span style="color:var(--text-2)">Served</span>
        <span style="font-weight:600">${formatNumber(scn.servedDemand)} units</span>
      </div>
      <div class="flex items-center justify-between text-xs py-xs" style="border-bottom:1px solid var(--border-light)">
        <span style="color:var(--text-2)">Unserved</span>
        <span style="font-weight:600;color:${(scn.unservedDemand || 0) > 0 ? 'var(--red)' : 'var(--green)'}">
          ${formatNumber(scn.unservedDemand)} units
          ${baseline && typeof baseline.unservedDemand === 'number' && typeof scn.unservedDemand === 'number'
            ? ` (baseline ${formatNumber(baseline.unservedDemand)})` : ''}
        </span>
      </div>
      <div class="flex items-center justify-between text-xs py-xs">
        <span style="color:var(--text-2)">Facilities open</span>
        <span style="font-weight:600">${formatNumber(scn.facilitiesOpen)}${baseline && baseline.facilitiesOpen != null ? ` (baseline ${formatNumber(baseline.facilitiesOpen)})` : ''}</span>
      </div>
    </div>

    <div class="scn-section-box">
      <span class="provenance-badge model-fact">MODEL FACT</span>
      <div class="text-xs text-muted" style="margin-top:8px;line-height:1.5">
        Every figure here is the MILP's own output for this scenario, read
        through the authoritative KPI layer. Nothing on this panel is an
        estimate, an average, or a narrative.
        ${scn.provenance && scn.provenance.engine ? `<br>Engine: ${scn.provenance.engine}.` : ''}
      </div>
    </div>

    <div class="flex gap-sm mt-lg">
      <button class="btn btn-secondary" id="btn-close-scenario-drawer" style="flex:1">Close</button>
    </div>
  `;

  overlay.classList.add('visible');

  document.getElementById('scenario-drawer-close')?.addEventListener('click', () => {
    overlay.classList.remove('visible');
  });
  document.getElementById('btn-close-scenario-drawer')?.addEventListener('click', () => {
    overlay.classList.remove('visible');
  });
}

// ─── Open Metric Drilldown Modal ────────────────────────────
//
// Rebuilt from the two solved states.
//
// The capacity-risk branch of this modal used to state that "Delhi NCR DC
// (Baseline)" runs at 108% and the scenario brings it to 91%, list "Baddi →
// Delhi NCR" as the network's highest-volume corridor, and report that Kolkata
// DC has 41% headroom — for whatever network was loaded. None of those
// facilities exist in a client upload, and none of those numbers came from a
// solve. It now reports the facilities in THIS network, at their solved
// utilisation, on both sides.
export function openMetricDrilldown(metricKey, scenarioId) {
  const modal = document.getElementById('modal-metric-drilldown');
  const titleEl = document.getElementById('drilldown-title');
  const bodyEl = document.getElementById('drilldown-body-content');
  const provEl = document.getElementById('drilldown-provenance-tag');
  if (!modal || !bodyEl) return;

  const def = ALL_METRIC_DEFS[metricKey] || { label: metricKey };
  const baseline = baselineScenario();
  const scn = SCENARIOS.find((s) => s.id === scenarioId)
    || SCENARIOS.find((s) => s.id === multiSelectedIds[0]);

  if (titleEl) titleEl.textContent = `${def.label} Drill-Down`;
  if (provEl) provEl.textContent = `PROVENANCE: ${def.provenance || 'MODEL FACT'}`;

  if (!baseline || !scn) {
    bodyEl.innerHTML = '<div class="text-xs text-muted">No solved scenario is '
      + 'available for this network, so there is nothing to compare.</div>';
    modal.classList.add('visible');
    return;
  }

  const facilityName = (id) => {
    const node = [...PLANTS, ...DCS, ...MARKETS].find((n) => n.id === id);
    if (node) return node.name || id;
    const site = (scn.newSites || []).find((s) => s.id === id);
    return site ? `${site.name} (new)` : id;
  };

  let detailHtml = '';

  if (metricKey === 'capacityRisk' || metricKey === 'avgUtil'
      || metricKey === 'maxUtil') {
    const before = baseline.scenarioFacilities || {};
    const after = scn.scenarioFacilities || {};
    const ids = [...new Set([...Object.keys(before), ...Object.keys(after)])]
      .sort((a, b) => ((after[b]?.utilPct ?? 0) - (after[a]?.utilPct ?? 0)));

    const rows = ids.map((id) => {
      const b = before[id] || {};
      const a = after[id] || {};
      const shift = (typeof a.utilPct === 'number' && typeof b.utilPct === 'number')
        ? a.utilPct - b.utilPct : null;
      const closed = a.isOpen === false;
      return `
        <tr${closed ? ' style="opacity:.65"' : ''}>
          <td>${facilityName(id)}${closed ? ' <span class="tag tag-danger" style="font-size:9px">closed</span>' : ''}</td>
          <td style="text-align:right">${b.utilPct == null ? '—' : `${b.utilPct.toFixed(1)}%`}</td>
          <td style="text-align:right;font-weight:700;color:${utilColour(a.utilPct)}">${a.utilPct == null ? '—' : `${a.utilPct.toFixed(1)}%`}</td>
          <td style="text-align:right">${shift === null ? '—' : `${shift > 0 ? '↑' : '↓'} ${Math.abs(shift).toFixed(1)} pts`}</td>
        </tr>`;
    }).join('');

    detailHtml = `
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;font-size:11.5px;color:var(--text-2)">
        <span>Comparing <strong>${scenarioDisplayName(baseline)}</strong> against <strong>${scenarioDisplayName(scn)}</strong></span>
      </div>
      <div class="grid-2 mb-md" style="gap:var(--space-sm)">
        <div style="background:var(--bg-subtle);padding:10px;border-radius:var(--r-sm);border:1px solid var(--border-light)">
          <span class="text-xs text-muted">Peak facility utilisation — baseline</span>
          <div style="font-size:16px;font-weight:800;color:${utilColour(baseline.maxUtil)}">
            ${baseline.maxUtil == null ? 'Unavailable' : `${baseline.maxUtil.toFixed(1)}%`}
            <span class="text-xs" style="font-weight:600">${baseline.capacityRisk}</span>
          </div>
        </div>
        <div style="background:var(--bg-subtle);padding:10px;border-radius:var(--r-sm);border:1px solid var(--border-light)">
          <span class="text-xs text-muted">Peak facility utilisation — scenario</span>
          <div style="font-size:16px;font-weight:800;color:${utilColour(scn.maxUtil)}">
            ${scn.maxUtil == null ? 'Unavailable' : `${scn.maxUtil.toFixed(1)}%`}
            <span class="text-xs" style="font-weight:600">${scn.capacityRisk}</span>
          </div>
        </div>
      </div>
      <div style="font-size:12px;margin:12px 0 4px;color:var(--text-1);font-weight:700">Per facility, as solved</div>
      <table class="scn-data-table" style="font-size:12px;width:100%">
        <thead><tr><th>Facility</th><th style="text-align:right">Baseline</th><th style="text-align:right">Scenario</th><th style="text-align:right">Shift</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="4" class="text-xs text-muted">No per-facility state was reported for either solve.</td></tr>'}</tbody>
      </table>
      <div style="font-size:12px;margin:14px 0 4px;color:var(--text-1);font-weight:700">Governance threshold</div>
      <div style="font-size:12.5px;color:var(--text-2);margin-bottom:12px">
        Utilisation at or above 95% is Critical; 85–95% is Stress; below 85% is
        Healthy. The band above is read from the peak facility in each solve.
      </div>
      <div style="font-size:12px;margin:12px 0 4px;color:var(--text-1);font-weight:700">Risk chain (P → REI → RF)</div>
      <div style="font-size:12px;color:var(--text-2);margin-bottom:14px;font-style:italic">
        Not available — this build does not surface a Facility REI or a governed
        Risk Factor on this screen. The band above is a categorical read of the
        solved utilisation only, not a synthesised RF score.
      </div>
      <span class="provenance-badge model-fact">MODEL FACT — every percentage above is a solver output</span>
    `;
  } else if (metricKey === 'totalCost' || metricKey === 'costChange'
             || metricKey === 'transportCost') {
    const row = (label, key) => {
      const b = baseline[key];
      const a = scn[key];
      const delta = (typeof b === 'number' && typeof a === 'number' && b !== 0)
        ? ((a - b) / Math.abs(b)) * 100 : null;
      return `
        <tr>
          <td>${label}</td>
          <td style="text-align:center">${formatCurrency(b)}</td>
          <td style="text-align:center">${formatCurrency(a)}</td>
          <td style="text-align:center;font-weight:700;color:${(delta ?? 0) <= 0 ? 'var(--green)' : 'var(--red)'}">
            ${delta === null ? '—' : `${delta < 0 ? '↓' : '↑'} ${Math.abs(delta).toFixed(1)}%`}
          </td>
        </tr>`;
    };
    detailHtml = `
      <div style="font-size:12.5px;color:var(--text-2);margin-bottom:14px">
        Cost decomposition from the solver, component by component. These are
        the components of Total Network Cost, not a re-derivation of it.
      </div>
      <table class="scn-data-table" style="font-size:12.5px;width:100%">
        <thead>
          <tr>
            <th>Cost component</th>
            <th style="text-align:center">Baseline</th>
            <th style="text-align:center">${scenarioDisplayName(scn)}</th>
            <th style="text-align:center">Variance</th>
          </tr>
        </thead>
        <tbody>
          ${row('Transport', 'transportCost')}
          ${row('Fixed facility', 'fixedCost')}
          ${row('Handling', 'handlingCost')}
          ${row('Inventory', 'inventoryCost')}
          ${row('Opening', 'openingCost')}
          ${row('Closure', 'closureCost')}
          <tr style="font-weight:800;background:var(--bg-subtle)">
            ${row('Total network cost', 'totalCost').replace('<tr>', '').replace('</tr>', '')}
          </tr>
        </tbody>
      </table>
      <div class="text-xs text-muted" style="margin-top:10px;line-height:1.5">
        The shortage penalty is excluded: it is the solver's device for choosing
        which demand to strand, not money anyone pays.
      </div>
      <span class="provenance-badge model-fact">MODEL FACT</span>
    `;
  } else {
    const b = baseline[metricKey];
    const a = scn[metricKey];
    detailHtml = `
      <div style="font-size:12.5px;color:var(--text-2);margin-bottom:14px">
        Deterministic MILP output for ${def.label}.
      </div>
      <div class="flex items-center justify-between" style="background:var(--bg-subtle);padding:12px;border-radius:var(--r-sm)">
        <span>Baseline: <strong>${b === undefined || b === null ? 'Unavailable' : (def.fmt ? def.fmt(b) : b)}</strong></span>
        <span>${scenarioDisplayName(scn)}: <strong style="color:var(--primary)">${a === undefined || a === null ? 'Unavailable' : (def.fmt ? def.fmt(a) : a)}</strong></span>
      </div>
      <span class="provenance-badge model-fact" style="margin-top:10px;display:inline-block">MODEL FACT</span>
    `;
  }

  bodyEl.innerHTML = detailHtml;
  modal.classList.add('visible');

  document.getElementById('modal-close-drilldown')?.addEventListener('click', () => {
    modal.classList.remove('visible');
  });
  document.getElementById('btn-close-drilldown-bottom')?.addEventListener('click', () => {
    modal.classList.remove('visible');
  });
}

/** The shared utilisation risk palette — Healthy / Stress / Critical. */
function utilColour(pct) {
  if (pct === null || pct === undefined) return 'var(--text-2)';
  if (pct >= 95) return '#dc2626';
  if (pct >= 85) return '#f59e0b';
  return '#22c55e';
}

// ─── Open Create Scenario Toolbox ───────────────────────────
function openCreateToolbox() {
  const modal = document.getElementById('modal-create-toolbox');
  if (!modal) return;

  document.getElementById('toolbox-form-body')?.classList.remove('hidden');
  document.getElementById('scn-creation-error')?.remove();

  const nameInput = document.getElementById('toolbox-scenario-name');
  if (nameInput) nameInput.value = '';

  // Reset the SELECTED TYPE, not just the fields below it.
  //
  // `renderToolboxDynamicFields('CHANGE_CAPACITY')` redrew the capacity fields
  // while the type card kept whatever the user picked last time — and
  // `readScenarioForm` reads the action from the card. So re-opening the modal
  // after running "Close Facility" showed capacity inputs, said "Change
  // Capacity" on the badge, and submitted CLOSE_FACILITY with whatever facility
  // the capacity dropdown happened to have selected. That is most of why
  // scenario results "seemed random".
  document.querySelectorAll('.scn-type-card').forEach((card) => {
    card.classList.toggle('active', card.dataset.type === 'CHANGE_CAPACITY');
  });

  renderToolboxDynamicFields('CHANGE_CAPACITY');
  modal.classList.add('visible');
}


/**
 * Facility <option> list for the scenario builder, derived from the network
 * actually loaded.
 *
 * These lists were hardcoded to the prototype's five demo DCs, so after a user
 * uploaded their own network the builder still offered "Bengaluru DC" and
 * "Guwahati DC" — facilities that do not exist in their data, and which the
 * solver would reject.
 */
function facilityOptionsHtml({ includePlants = false, includeAll = false } = {}) {
  const source = includePlants ? [...DCS, ...PLANTS] : DCS;
  if (!source.length) {
    return '<option value="">No facility in this network</option>';
  }
  const all = includeAll
    ? '<option value="" selected>Every lane in the network</option>' : '';
  // A candidate is a site the client gave us as a PROPOSAL. It belongs in this
  // list — "pin it open" is exactly how you ask the model to commit to one, and
  // it is the only way to test a site the client has actually screened rather
  // than a point invented on the map. But it must be labelled: unlabelled, a
  // proposed DC sat in a dropdown headed "one of my existing sites" and read as
  // somewhere the client already operates.
  return all + source
    .map((f, i) => {
      const isCandidate = String(f.status || '').toUpperCase() === 'CANDIDATE';
      const label = `${f.name || f.id}${isCandidate ? ' — proposed site' : ''}`;
      return `<option value="${f.id}"${(i === 0 && !includeAll) ? ' selected' : ''}>`
        + `${label}</option>`;
    })
    .join('');
}

/**
 * Places to put a new site, as a shortcut for typing coordinates.
 *
 * This was a list of thirty-two Indian cities, hardcoded. On a US network the
 * "Jump to a city" control offered Coimbatore and Guwahati — and picking one
 * wrote those coordinates into the latitude and longitude fields, so the
 * shortcut's only effect was to propose a site nine thousand kilometres from
 * the network it was being added to.
 *
 * It is now built from two things the application actually knows:
 *
 *   1. the places in the uploaded network — its markets, then its facilities,
 *      which are named locations with real coordinates; and
 *   2. the administrative subdivisions of the countries that network sits in,
 *      from the same Natural Earth data both maps draw, so a site can be
 *      proposed somewhere the network does not yet reach.
 *
 * The two coordinate inputs beside this remain the authority and are editable:
 * a new facility can go anywhere, and this is a convenience, not a
 * restriction. Nothing here is pre-selected.
 */

/** Distinct named places in the loaded network, markets first. */
function networkPlacePresets() {
  const seen = new Set();
  const out = [];
  const push = (node, kind) => {
    if (!Number.isFinite(node.lat) || !Number.isFinite(node.lng)) return;
    const label = node.city || node.name || node.id;
    if (!label) return;
    // Deduped on the place, not the node: several facilities can share a city,
    // and the list is a list of places.
    const key = `${String(label).toLowerCase()}|${node.lat.toFixed(2)},${node.lng.toFixed(2)}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ label, kind, lat: +node.lat.toFixed(4), lng: +node.lng.toFixed(4) });
  };
  MARKETS.forEach((m) => push(m, 'market'));
  DCS.forEach((f) => push(f, 'facility'));
  PLANTS.forEach((f) => push(f, 'facility'));
  return out;
}

/**
 * The centroid of a ring, by the shoelace formula.
 *
 * Not the centre of its bounding box, which for a crescent-shaped region — a
 * Chile, a West Virginia — lands outside the region itself. This stays inside
 * for any convex shape and for most concave ones. It is a starting point the
 * user edits, and it is labelled as the region rather than as a town.
 */
function ringCentroid(ring) {
  let twiceArea = 0;
  let x = 0;
  let y = 0;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
    const cross = (ring[j][0] * ring[i][1]) - (ring[i][0] * ring[j][1]);
    twiceArea += cross;
    x += (ring[j][0] + ring[i][0]) * cross;
    y += (ring[j][1] + ring[i][1]) * cross;
  }
  if (!twiceArea) return null;
  return { lng: x / (3 * twiceArea), lat: y / (3 * twiceArea) };
}

/** The largest polygon of a feature, which is the one worth centring on. */
function largestRing(feature) {
  let best = null;
  let bestSpan = -1;
  for (const poly of feature.geometry.coordinates) {
    const ring = poly[0];
    if (!ring || ring.length < 4) continue;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const [lng, lat] of ring) {
      if (lng < minX) minX = lng;
      if (lng > maxX) maxX = lng;
      if (lat < minY) minY = lat;
      if (lat > maxY) maxY = lat;
    }
    const span = (maxX - minX) * (maxY - minY);
    if (span > bestSpan) { bestSpan = span; best = ring; }
  }
  return best;
}

function escapeAttr(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function presetOptionHtml({ label, lat, lng }) {
  return `<option value="${lat},${lng}">${escapeAttr(label)}</option>`;
}

/**
 * The synchronous part: a prompt, and the network's own places.
 *
 * The regions arrive separately — the subdivision data is 1.6 MB and is loaded
 * on demand, and a form must not wait on it.
 */
function sitePresetOptionsHtml() {
  const places = networkPlacePresets();
  const prompt = places.length
    ? '<option value="" selected>Jump to a place, or type coordinates below</option>'
    : '<option value="" selected>Type coordinates below</option>';
  if (!places.length) return prompt;
  return prompt
    + '<optgroup label="In your network">'
    + places.map(presetOptionHtml).join('')
    + '</optgroup>';
}

/**
 * Add the regions of the network's own countries, once they have loaded.
 *
 * Best effort: if the subdivision data is unavailable the control still offers
 * the network's own places and the coordinate inputs, which is the whole of
 * what it needs to work.
 */
async function hydrateSitePresetRegions() {
  const select = document.getElementById('toolbox-site-city');
  if (!select || select.querySelector('optgroup[data-regions]')) return;

  const nodes = [...DCS, ...PLANTS, ...MARKETS];
  const countries = countriesContaining(nodes).map((c) => c.name);
  if (!countries.length) return;

  let admin1 = null;
  try {
    admin1 = await loadAdmin1();
  } catch (e) {
    return;
  }
  if (!admin1 || !admin1.collection) return;
  // The form may have been closed, or re-rendered for another scenario type,
  // while the data was in flight.
  const target = document.getElementById('toolbox-site-city');
  if (!target || target !== select || target.querySelector('optgroup[data-regions]')) return;

  const wanted = new Set(countries);
  const byCountry = new Map();
  for (const feature of admin1.collection.features) {
    const country = feature.properties && feature.properties.admin;
    if (!wanted.has(country)) continue;
    const ring = largestRing(feature);
    if (!ring) continue;
    const centre = ringCentroid(ring);
    if (!centre || !Number.isFinite(centre.lat) || !Number.isFinite(centre.lng)) continue;
    const list = byCountry.get(country) || byCountry.set(country, []).get(country);
    list.push({
      label: feature.properties.name,
      lat: +centre.lat.toFixed(4),
      lng: +centre.lng.toFixed(4),
    });
  }
  if (!byCountry.size) return;

  const html = [...byCountry.entries()].map(([country, regions]) => {
    regions.sort((a, b) => a.label.localeCompare(b.label));
    return `<optgroup data-regions="1" label="${escapeAttr(country)} — regions">`
      + regions.map(presetOptionHtml).join('')
      + '</optgroup>';
  }).join('');
  target.insertAdjacentHTML('beforeend', html);
}

/**
 * The middle of the loaded network, as a starting point for a new site.
 *
 * The latitude and longitude inputs opened at 21.1458 / 79.0882 — Nagpur —
 * for every network in every country. The centroid of the user's own
 * facilities is at least a point on their map; it is a starting position for a
 * control they then edit, not a recommendation.
 */
function networkCentroid() {
  const nodes = [...DCS, ...PLANTS].filter(
    (f) => Number.isFinite(f.lat) && Number.isFinite(f.lng));
  if (!nodes.length) return null;
  return {
    lat: +(nodes.reduce((s, f) => s + f.lat, 0) / nodes.length).toFixed(4),
    lng: +(nodes.reduce((s, f) => s + f.lng, 0) / nodes.length).toFixed(4),
  };
}

/**
 * A median handling cost and a median fixed cost from the network as loaded.
 *
 * Used only as a STARTING VALUE in the new-site form, which the user edits.
 * Both inputs are required and are sent as typed — nothing is defaulted behind
 * the user's back, because the economics of a new site are the whole question
 * this scenario asks.
 */
function medianOf(values) {
  // `v > 0` excluded zero, and zero is a real value here: the client's own DCs
  // state a handling cost of 0.00, so the handling field opened empty and the
  // form then refused to run for want of a number the network had already
  // supplied. Only absent and negative values are unusable.
  const usable = values.filter((v) => typeof v === 'number' && Number.isFinite(v) && v >= 0);
  if (!usable.length) return null;
  usable.sort((a, b) => a - b);
  return usable[Math.floor(usable.length / 2)];
}

function renderToolboxDynamicFields(type) {
  const container = document.getElementById('toolbox-dynamic-fields');
  const badge = document.getElementById('toolbox-active-type-badge');
  const desc = document.getElementById('toolbox-active-type-desc');
  if (!container) return;

  const setHeader = (label, text) => {
    if (badge) badge.textContent = label;
    if (desc) desc.textContent = text;
  };

  if (type === 'CHANGE_CAPACITY') {
    setHeader('Change Capacity',
      'Add or remove capacity at one facility. The solver re-allocates the '
      + 'whole network around the new limit.');
    container.innerHTML = `
      <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Facility</label>
          <select class="form-select" id="toolbox-facility">
            ${facilityOptionsHtml({ includePlants: true })}
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Adjustment Direction</label>
          <select class="form-select" id="toolbox-direction">
            <option value="INCREASE" selected>Increase (+)</option>
            <option value="DECREASE">Decrease (−)</option>
          </select>
        </div>
      </div>
      <div class="form-group">
        <label class="form-label">Adjustment amount (units per period)</label>
        <input type="number" class="form-input" id="toolbox-amount" value="2000" min="1" step="500">
        <div class="text-xs text-muted" style="margin-top:4px">
          A decrease larger than the facility's current capacity is refused
          rather than clamped — to remove a site entirely, use Close Facility.
        </div>
      </div>
    `;
  } else if (type === 'CLOSE_FACILITY') {
    setHeader('Close Facility',
      'Force a site shut and let the solver re-route everything it was carrying.');
    container.innerHTML = `
      <div class="form-group">
        <label class="form-label">Facility to close</label>
        <select class="form-select" id="toolbox-facility">
          ${facilityOptionsHtml({ includePlants: true })}
        </select>
        <div class="text-xs text-muted" style="margin-top:4px">
          Closure cost is charged where the network states one. Demand this
          leaves unservable is reported as unserved, not hidden.
        </div>
      </div>
    `;
  } else if (type === 'OPEN_FACILITY') {
    // Two genuinely different questions, and they used to be the same one.
    //
    // "Open Facility" offered a dropdown of the client's OWN distribution
    // centres and plants — every one of which the solver was already free to
    // keep open — so choosing one asked a question with a known answer. There
    // was no way to ask about a site that does not exist yet.
    setHeader('Open a Facility',
      'Commit to a site already in your data — including one you supplied as a '
      + 'proposal — or put a new one anywhere on the map.');
    const handling = medianOf(DCS.map((d) => d.handlingCost));
    const fixed = medianOf(DCS.map((d) => d.fixedCostPerYear));
    const capacity = medianOf(DCS.map((d) => d.capacity));
    // Every starting value below comes from the loaded network. Where it
    // cannot — the network has no DC, or states no cost — the field opens
    // EMPTY and the form refuses to submit until the user fills it, rather
    // than defaulting to a figure the product made up.
    const centre = networkCentroid();
    container.innerHTML = `
      <div class="form-group mb-sm">
        <label class="form-label">What kind of site?</label>
        <select class="form-select" id="toolbox-open-mode">
          <option value="NEW" selected>A new site — anywhere on the map</option>
          <option value="EXISTING">One of the sites already in my data</option>
        </select>
      </div>

      <div id="toolbox-open-existing" class="hidden">
        <div class="form-group">
          <label class="form-label">Site to keep open</label>
          <select class="form-select" id="toolbox-facility">
            ${facilityOptionsHtml({ includePlants: true })}
          </select>
          <div class="text-xs text-muted" style="margin-top:4px">
            Pins this site open for the whole solve. Useful when a contract or a
            commitment means it cannot be closed, even if closing it would be
            cheaper — and the way to test a site marked "proposed site", which
            the client supplied as a candidate but the optimiser has not taken.
          </div>
        </div>
      </div>

      <div id="toolbox-open-new">
        <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
          <div class="form-group">
            <label class="form-label">Site name</label>
            <input type="text" class="form-input" id="toolbox-site-name" placeholder="Name this site" maxlength="48" value="New DC">
          </div>
          <div class="form-group">
            <label class="form-label">Jump to a place</label>
            <select class="form-select" id="toolbox-site-city">
              ${sitePresetOptionsHtml()}
            </select>
          </div>
        </div>
        <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
          <div class="form-group">
            <label class="form-label">Latitude</label>
            <!-- The whole globe. These read min="6" max="38" and the
                 longitude min="67" max="98" — India's bounding box — so on a
                 US network the form opened at its own centroid (-98) with the
                 field already out of range, and the browser refused to submit
                 a scenario the solver would have accepted. -->
            <input type="number" class="form-input" id="toolbox-site-lat" value="${centre ? centre.lat : ''}" placeholder="Latitude" step="0.0001" min="-85" max="85">
          </div>
          <div class="form-group">
            <label class="form-label">Longitude</label>
            <input type="number" class="form-input" id="toolbox-site-lng" value="${centre ? centre.lng : ''}" placeholder="Longitude" step="0.0001" min="-180" max="180">
          </div>
        </div>
        <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
          <div class="form-group">
            <label class="form-label">Capacity (units per period)</label>
            <input type="number" class="form-input" id="toolbox-site-capacity" value="${capacity ? Math.round(capacity) : ''}" placeholder="Units per period" min="1" step="500">
          </div>
          <div class="form-group">
            <label class="form-label">Site type</label>
            <select class="form-select" id="toolbox-site-role">
              <option value="DC" selected>Distribution centre</option>
              <option value="PLANT">Plant</option>
            </select>
          </div>
        </div>
        <div class="grid-2" style="gap:var(--space-sm)">
          <div class="form-group">
            <label class="form-label">Fixed cost (${currencyLabel()} per year)</label>
            <input type="number" class="form-input" id="toolbox-site-fixed" value="${fixed != null ? Math.round(fixed) : ''}" placeholder="Per year" min="0" step="100000">
          </div>
          <div class="form-group">
            <label class="form-label">Handling cost (${currencyLabel()} per unit)</label>
            <input type="number" class="form-input" id="toolbox-site-handling" value="${handling != null ? Number(handling).toFixed(2) : ''}" placeholder="Per unit" min="0" step="0.5">
          </div>
        </div>
        <div class="text-xs text-muted" style="margin-top:8px;line-height:1.5">
          Freight to and from the new site is derived from the distance to each
          of your plants and markets, priced at your own network's average rate
          per kilometre — not at a constant. The solver may leave the site
          closed: if opening it does not pay, that is the answer.
          ${DCS.length ? '' : '<br><strong>No network is loaded, so a new site has nothing to connect to.</strong>'}
        </div>
      </div>
    `;

    const modeSelect = document.getElementById('toolbox-open-mode');
    const newBlock = document.getElementById('toolbox-open-new');
    const existingBlock = document.getElementById('toolbox-open-existing');
    modeSelect?.addEventListener('change', () => {
      const isNew = modeSelect.value === 'NEW';
      newBlock?.classList.toggle('hidden', !isNew);
      existingBlock?.classList.toggle('hidden', isNew);
    });

    const city = document.getElementById('toolbox-site-city');
    // The regions of the network's own countries, added when the subdivision
    // data has loaded. The control is usable before then: it already carries
    // every place in the uploaded network.
    hydrateSitePresetRegions();
    city?.addEventListener('change', () => {
      if (!city.value) return;
      const [lat, lng] = city.value.split(',');
      const latEl = document.getElementById('toolbox-site-lat');
      const lngEl = document.getElementById('toolbox-site-lng');
      if (latEl) latEl.value = lat;
      if (lngEl) lngEl.value = lng;
      const nameEl = document.getElementById('toolbox-site-name');
      const label = city.options[city.selectedIndex]?.text || '';
      if (nameEl && label) nameEl.value = `${label} DC`;
    });
  } else if (type === 'CHANGE_DEMAND') {
    // Applies to every demand row, so it names no facility — which is why the
    // form must not ask for one. It used to fall into a generic branch that
    // rendered no facility field at all, while the submit path refused to run
    // without one. The scenario was unreachable from this modal.
    // Growth arrives from a client as a figure for a part of their business —
    // "chilled is up 20% in the South" — and the scope field here used to be a
    // disabled box reading "Every market and product". A single network-wide
    // multiplier is the wrong question: it loads every warehouse in the country
    // with growth that is happening in one region, which overstates the case
    // for expanding the ones that are not.
    //
    // The two selects offer only labels THIS upload states. Where it states
    // none the select is not rendered at all, and the note says why, rather
    // than offering a list of regions the data cannot be filtered by.
    setHeader('Change Demand',
      'Scale demand up or down and re-solve — for the whole network, or for one '
      + 'region or product category the client says is growing.');
    const regionOpts = NETWORK_REGIONS
      .map((r) => `<option value="${r}">${r}</option>`).join('');
    const categoryOpts = PRODUCT_CATEGORIES
      .map((c) => `<option value="${c}">${c}</option>`).join('');
    const scopeNotes = [];
    if (!NETWORK_REGIONS.length) {
      scopeNotes.push('This upload states no region for its markets, so growth '
        + 'cannot be scoped by region.');
    }
    if (!PRODUCT_CATEGORIES.length) {
      scopeNotes.push('This upload states no product category, so growth cannot '
        + 'be scoped by category.');
    }
    container.innerHTML = `
      <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Demand change (%)</label>
          <input type="number" class="form-input" id="toolbox-amount" value="15" min="-90" max="200" step="5">
        </div>
        ${NETWORK_REGIONS.length ? `
        <div class="form-group">
          <label class="form-label">Region</label>
          <select class="form-select" id="toolbox-demand-region">
            <option value="" selected>Every region</option>
            ${regionOpts}
          </select>
        </div>` : ''}
      </div>
      ${PRODUCT_CATEGORIES.length ? `
      <div class="form-group">
        <label class="form-label">Product category</label>
        <select class="form-select" id="toolbox-demand-category">
          <option value="" selected>Every product</option>
          ${categoryOpts}
        </select>
      </div>` : ''}
      <div class="text-xs text-muted" style="margin-top:6px">
        Rows outside the chosen scope are left exactly as they are, not scaled by
        zero. Naming both a region and a category narrows to their overlap.
        ${scopeNotes.join(' ')}
      </div>
    `;
  } else if (type === 'CHANGE_TRANSPORT_COST') {
    setHeader('Change Transport Cost',
      'Move freight rates and let the solver re-route around the new economics.');
    container.innerHTML = `
      <div class="grid-2" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Rate change (%)</label>
          <input type="number" class="form-input" id="toolbox-amount" value="10" min="-90" max="300" step="5">
        </div>
        <div class="form-group">
          <label class="form-label">Which lanes</label>
          <select class="form-select" id="toolbox-facility">
            ${facilityOptionsHtml({ includePlants: true, includeAll: true })}
          </select>
        </div>
      </div>
      <div class="text-xs text-muted" style="margin-top:6px">
        Choosing a facility narrows the change to the lanes touching it — a
        carrier renegotiation at one site, rather than a market-wide move.
      </div>
    `;
  } else if (type === 'CHANGE_SLA') {
    setHeader('Change SLA',
      'Tighten or relax the delivery promise, in days, and see what it costs.');
    container.innerHTML = `
      <div class="grid-2" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Change to promised delivery (days)</label>
          <input type="number" class="form-input" id="toolbox-amount" value="-1" min="-10" max="10" step="0.5">
        </div>
        <div class="form-group">
          <label class="form-label">Scope</label>
          <input type="text" class="form-input" value="Every demand row with a stated SLA" disabled>
        </div>
      </div>
      <div class="text-xs text-muted" style="margin-top:6px">
        Negative tightens the promise, positive relaxes it. If no demand row in
        your upload states an SLA, this scenario is refused rather than run
        against nothing.
      </div>
    `;
  } else {
    setHeader(String(type).replace(/_/g, ' '),
      'This change is not supported by the analysis engine.');
    container.innerHTML = `
      <div class="text-xs text-muted" style="padding:10px 0;line-height:1.5">
        The engine has no capability for <strong>${type}</strong>, so this
        scenario cannot be run. Nothing would be solved, and a result shown here
        would not be one.
      </div>
    `;
  }
}

// ─── Execute Scenario Creation (real solve) ──────────────────
//
// Phase 10.0 rewrite. This function previously ran a `setInterval` that
// animated six fake progress steps and then pushed a literal object into
// SCENARIOS:
//
//     totalCost: 1220000, costChange: -5.1, sla: 96.5, carbonKg: 101200,
//     robustnessTests: [{ test: '+15% Demand Surge', status: 'PASS', ... }],
//     assumptions: [{ label: 'Solver Execution', value: 'Branch-and-Cut (Exact)' }],
//     changes:     [{ note: 'MILP verified' }]
//
// No request was ever made. Those numbers were invented in the browser and
// labelled as exact solver output — the most serious defect the Phase 10.0
// audit found, because a planner had no way to tell them from a real solve.
//
// The scenario is now solved by the MILP engine through the orchestrator, and
// every figure rendered comes back from the authoritative KPI layer with its
// own status.

/**
 * The change this scenario makes, in the words the form used.
 *
 * Read off the request body that is about to be sent, so the message on the
 * signal describes the thing actually travelling to the planner.
 */
function scenarioActionLabel(body) {
  const action = String((body && (body.action || body.action_type)) || '')
    .replace(/_/g, ' ').toLowerCase();
  return action || 'this change';
}

/* The step list that used to live here is gone.
   It drew six phases with a spinner inside the scenario modal while the agent
   dialog was already on screen, in front of it, reporting the same wait — two
   loading states for one request, disagreeing about how far along it was.
   The dialog is the loading screen; this panel now carries only the reason a
   run stopped. */

/**
 * Show why a run stopped, and put the user back on the field that stopped it.
 *
 * `fieldId` matters more than it looks. Every field in the new-site form
 * pre-fills from the loaded network except the site NAME, which opened blank —
 * so the common first experience of "Open a facility" was: fill nothing, press
 * Run, and land on an execution view carrying a one-line refusal, with the
 * offending input hidden behind it on a form the user had already left. No
 * request was ever sent, which is why nothing appeared in any server log.
 * Reported as "unable to create a scenario when I chose to open a new facility".
 *
 * The name now defaults, so the form is runnable as it opens; this returns the
 * user to the exact input for every OTHER refusal rather than making them hunt.
 */
function showCreationError(message, fieldId = null, diagnosis = null) {
  const formBody = document.getElementById('toolbox-form-body');
  if (!formBody) return;
  // A refusal belongs beside the form that has to change, with the user's
  // own inputs still in it. It used to be written into a separate panel that
  // replaced the form — so the message and the field it was about could not
  // be on screen at the same time, and a "Back to the form" button existed
  // to undo a swap that never needed to happen.
  formBody.classList.remove('hidden');
  document.getElementById('modal-create-toolbox')?.classList.add('visible');

  if (fieldId) {
    const field = document.getElementById(fieldId);
    if (field) {
      field.focus();
      field.scrollIntoView({ behavior: 'smooth', block: 'center' });
      // Cleared on the next edit, so the mark tracks the current value rather
      // than staying red on a field the user has already fixed.
      field.style.borderColor = 'var(--red, #dc2626)';
      field.addEventListener('input', function clear() {
        field.style.borderColor = '';
        field.removeEventListener('input', clear);
      });
    }
  }
  let banner = document.getElementById('scn-creation-error');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'scn-creation-error';
    banner.className = 'alert alert-error';
    banner.style.cssText = 'margin-top:12px;padding:10px 12px;border-radius:8px;'
      + 'background:rgba(220,38,38,.08);color:var(--red);font-size:12px;line-height:1.5';
    formBody.appendChild(banner);
  }
  banner.textContent = message;

  // WHY, in figures, when the reason was that the network cannot serve what
  // this scenario asks of it.
  //
  // A refused scenario used to be one line of red text saying no feasible
  // solution exists — true, and nothing a planner can do anything with. The
  // solver now diagnoses an infeasible solve and the numbers travel to here;
  // this states the shortfall, names the markets, and names any site the
  // client has proposed that the optimiser would build.
  banner.appendChild(infeasibilityHtml(diagnosis));
}

/**
 * The shortfall behind a refused scenario, as an element.
 *
 * Every figure is the backend's, from `OptimizationResult.infeasibility` — a
 * diagnostic solve that was allowed to leave demand unserved. It is NOT a
 * plan and carries no cost, which the backend's own summary says and this
 * does not repeat.
 *
 * Returns an empty fragment when there is nothing to add, so the caller does
 * not have to branch.
 */
function infeasibilityHtml(diagnosis) {
  const frag = document.createDocumentFragment();
  if (!diagnosis || !diagnosis.diagnosed) return frag;

  const esc = (t) => String(t == null ? '' : t)
    .replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;',
                                   '"': '&quot;', "'": '&#39;' }[c]));
  const rows = [];
  if (typeof diagnosis.unserved_demand === 'number') {
    rows.push(['Cannot be served',
      `<strong>${formatNumber(Math.round(diagnosis.unserved_demand))} units</strong>`
      + (typeof diagnosis.unserved_rate === 'number'
          ? ` · ${(diagnosis.unserved_rate * 100).toFixed(1)}% of the demand asked for`
          : '')]);
  }
  const short = diagnosis.short_markets || [];
  if (short.length) {
    rows.push(['Short in', short.map((m) =>
      `${esc(m.market_id)} (${formatNumber(Math.round(m.unserved))} units)`)
      .join(', ')]);
  }
  const build = diagnosis.would_open_candidates || [];
  if (build.length) {
    rows.push(['Would build', `${build.map(esc).join(', ')} — `
      + `${build.length === 1 ? 'a site you have' : 'sites you have'} proposed `
      + 'but not built']);
  }
  if (!rows.length) return frag;

  const box = document.createElement('div');
  box.className = 'scn-infeasible-detail';
  box.innerHTML = rows.map(([label, value]) =>
    `<div class="scn-infeasible-row"><span>${label}</span><span>${value}</span></div>`)
    .join('');
  frag.appendChild(box);
  return frag;
}

/**
 * Translate the modal's form state into the scenario API's request body.
 *
 * Returns `{ body }` or `{ error }` — a form that cannot produce a runnable
 * request says why, rather than submitting something the API will reject with a
 * message written for a developer.
 */
function readScenarioForm() {
  const name = document.getElementById('toolbox-scenario-name')?.value.trim()
    || 'Untitled scenario';
  const type = document.querySelector('.scn-type-card.active')?.dataset.type
    || 'CHANGE_CAPACITY';
  const facilityId = document.getElementById('toolbox-facility')?.value || '';
  const direction = document.getElementById('toolbox-direction')?.value || 'INCREASE';
  const amountEl = document.getElementById('toolbox-amount');
  const amount = amountEl ? Number(amountEl.value) : NaN;

  const needsAmount = ['CHANGE_CAPACITY', 'CHANGE_DEMAND',
                       'CHANGE_TRANSPORT_COST', 'CHANGE_SLA'].includes(type);
  if (needsAmount && !Number.isFinite(amount)) {
    return { error: 'Enter a number for the amount before running this scenario.' };
  }

  const body = { name, action: type, facility_ids: [] };

  if (type === 'CHANGE_CAPACITY') {
    if (!facilityId) return { error: 'Choose a facility to change capacity at.' };
    if (amount <= 0) {
      return { error: 'The adjustment amount must be above zero. Use the direction control to reduce capacity.' };
    }
    body.facility_ids = [facilityId];
    body.capacity_delta_units = direction === 'DECREASE' ? -amount : amount;

  } else if (type === 'CLOSE_FACILITY') {
    if (!facilityId) return { error: 'Choose a facility to close.' };
    body.facility_ids = [facilityId];

  } else if (type === 'OPEN_FACILITY') {
    const mode = document.getElementById('toolbox-open-mode')?.value || 'NEW';
    if (mode === 'EXISTING') {
      if (!facilityId) return { error: 'Choose a site to keep open.' };
      body.facility_ids = [facilityId];
    } else {
      const siteName = document.getElementById('toolbox-site-name')?.value.trim();
      // An EMPTY numeric input is missing, not zero. `Number('')` is 0, so a
      // blank fixed cost used to pass a `>= 0` check and propose a site that
      // costs nothing to run — the single most favourable assumption available,
      // made silently on the user's behalf.
      const num = (id) => {
        const raw = document.getElementById(id)?.value;
        if (raw === undefined || raw === null || String(raw).trim() === '') return null;
        const n = Number(raw);
        return Number.isFinite(n) ? n : null;
      };
      const lat = num('toolbox-site-lat');
      const lng = num('toolbox-site-lng');
      const capacity = num('toolbox-site-capacity');
      const fixed = num('toolbox-site-fixed');
      const handling = num('toolbox-site-handling');
      const role = document.getElementById('toolbox-site-role')?.value || 'DC';

      if (!siteName) return { error: 'Give the new site a name.', field: 'toolbox-site-name' };
      if (lat === null || lng === null) {
        return { error: 'Enter a latitude and longitude for the new site, or pick a city.',
                 field: lat === null ? 'toolbox-site-lat' : 'toolbox-site-lng' };
      }
      if (capacity === null || capacity <= 0) {
        return { error: 'A new site needs a capacity above zero — a site with no capacity cannot serve anything.',
                 field: 'toolbox-site-capacity' };
      }
      if (fixed === null || handling === null) {
        return { error: 'Enter the fixed cost per year and the handling cost per unit. '
                      + 'They decide whether opening this site pays, so they are not assumed.',
                 field: fixed === null ? 'toolbox-site-fixed' : 'toolbox-site-handling' };
      }
      if (fixed < 0 || handling < 0) {
        return { error: 'Fixed and handling costs cannot be negative.',
                 field: fixed < 0 ? 'toolbox-site-fixed' : 'toolbox-site-handling' };
      }
      body.action = 'ADD_FACILITY';
      body.new_facility = {
        name: siteName, latitude: lat, longitude: lng,
        capacity_units_per_period: capacity,
        fixed_cost_per_year: fixed, handling_cost_per_unit: handling,
        role,
      };
    }

  } else if (type === 'CHANGE_DEMAND') {
    if (amount <= -100) {
      return { error: 'Demand cannot fall by 100% or more — that removes the demand rather than changing it.' };
    }
    // The form captures a percentage; the engine takes a multiplier.
    body.demand_multiplier = 1 + (amount / 100);
    // Empty means "every one of them", which is what this form always did.
    // Sent only when set, so an unscoped run is byte-identical to before.
    const demandRegion = (document.getElementById('toolbox-demand-region')?.value || '').trim();
    const demandCategory = (document.getElementById('toolbox-demand-category')?.value || '').trim();
    if (demandRegion) body.demand_region = demandRegion;
    if (demandCategory) body.demand_product_category = demandCategory;

  } else if (type === 'CHANGE_TRANSPORT_COST') {
    if (amount <= -100) {
      return { error: 'Freight rates cannot fall by 100% or more — that removes transport cost from the model rather than changing it.' };
    }
    body.transport_cost_multiplier = 1 + (amount / 100);
    // Empty means network-wide, which the select's first option says.
    if (facilityId) body.facility_ids = [facilityId];

  } else if (type === 'CHANGE_SLA') {
    if (amount === 0) {
      return { error: 'A change of zero days is not a scenario. Tighten or relax the promise.' };
    }
    body.sla_days_delta = amount;

  } else {
    return { error: `The analysis engine has no capability for ${type}, so this scenario cannot be run.` };
  }

  return { body };
}

async function runScenarioCreation() {
  // The modal is not swapped to a view of its own here.
  //
  // It used to hide its form and reveal `#agent-execution-view` — the panel
  // that held this page's private six-step run display until that was
  // deleted for reporting the same request the agent dialog reports. The
  // panel stayed, empty, and was still being revealed: every scenario run
  // opened a blank white card, in front of the loading screen it had just
  // raised. The loading dialog is what reports a run, so the modal closes
  // and lets it.
  document.getElementById('scn-creation-error')?.remove();

  // The form validates itself now and says which field is wrong. It used to
  // check only `facility_ids.length` and print "Select a facility before
  // running this scenario" — which was the wrong advice for the three scenario
  // types that name no facility, and the only advice available for a form that
  // rendered no facility field for them in the first place.
  const { body, error, field } = readScenarioForm();
  if (error) {
    showCreationError(error, field);
    return;
  }
  document.getElementById('modal-create-toolbox')?.classList.remove('visible');


  // The agent view of this run.
  //
  // TWO dispatches, because that is what this function makes: one request to
  // /api/scenarios/simulate, and the local read of the solved result against
  // the baseline. So the ring lights the Scenario Planner and the hub, and
  // leaves Extraction, Forecasting and Reasoning dark — none of them is asked
  // to do anything by a scenario run, and a lit layer that did no work is the
  // one thing this visualisation must never show.
  //
  // If the orchestrator's plan for SCENARIO_ANALYSIS does reach further, the
  // execution trace passed to `stepDone` says so and those layers light from
  // the server's own record rather than from a guess made here.
  mountAgentLoading();
  startRun({
    title: 'Running your scenario',
    verb: 'running your scenario',
    subtitle: 'The orchestrator hands the change to the scenario planner, '
      + 'which re-solves the network and measures it against your baseline.',
    plan: [
      { id: 'simulate', layer: 'scenario',
        label: 'Solving the scenario',
        message: `Passing "${scenarioActionLabel(body)}" to the scenario planner` },
      { id: 'compare', layer: 'orchestrator',
        label: 'Comparing against the baseline',
        message: 'Reading the solved KPIs back against the baseline solve' },
    ],
  });

  // The way out of a wait that can be a quarter of an hour.
  //
  // A measured run — demand raised across every region for one product
  // category — took over 700 seconds behind a scrim with no exit. Nothing
  // below needs the dialog to be on screen: this function is awaiting one
  // promise, the solve is on the server, and the server finishes and stores
  // the scenario whether or not anybody is listening (which is what
  // `findScenarioCreatedSince` was written for). So the dialog can go and the
  // run cannot notice.
  //
  // `backgrounded` decides only WHERE each branch below reports: the dialog
  // if the reader stayed, the tray if they left. Neither changes the work.
  const taskId = `scn_${Date.now().toString(36)}`;
  let backgrounded = false;
  const scenarioLabel = body.name || 'your scenario';
  offerBackgroundExit(() => {
    backgrounded = true;
    dismissAgentLoading(0);
    // The title names the scenario, not what is being done to it: this row
    // outlives the solve, and "Solving ..." under a green tick is a card
    // arguing with itself.
    startBackgroundTask({ id: taskId, title: scenarioLabel });
  });

  // Everything from here reports to one of two places, and `reportFailure`
  // picks — so a branch cannot be written for the dialog and then forget that
  // the dialog may no longer be there. `step` is which dispatch stopped, which
  // the trace still records when the reader stayed to watch it.
  const reportFailure = (step, message, diagnosis = null) => {
    withdrawBackgroundExit();
    if (backgrounded) {
      failBackgroundTask(taskId, message);
      return;
    }
    stepFail(step, { error: message });
    finishRun({ error: message });
    dismissAgentLoading(2600);
    showCreationError(message, null, diagnosis);
  };

  let solved;
  const dispatchedAt = Date.now();
  try {
    stepStart('simulate');
    solved = await scenarioService.simulateScenario(body);
    stepDone('simulate', {
      detail: 'Scenario solved',
      executionId: solved && solved.execution_id,
    });
  } catch (err) {
    // A TIMEOUT is a statement about how long this client waited. It is not a
    // statement about the solve, which is still running: aborting a fetch does
    // not abort the optimiser. Measured on a 20-facility network, the first
    // scenario took 5m19s against a 5m limit — the server finished, stored the
    // scenario and answered 201 into a connection nobody was listening on,
    // while the screen said "The scenario could not be solved".
    //
    // So look for it before saying anything.
    if (err && err.code === 'TIMEOUT') {
      note('scenario', [
        'Still solving — this client stopped waiting, the solver did not',
        'Watching for the finished scenario',
      ]);
      solved = await scenarioService.findScenarioCreatedSince(
        body.name, dispatchedAt, { projectId: body.project_id });
    }

    if (!solved) {
      // Now it can be reported, and only as much as is known: this client
      // never saw a result. Whether the run failed or is simply still going is
      // exactly what a timeout cannot tell us, so it is not claimed either way.
      const message = (err && err.code === 'TIMEOUT')
        ? 'This scenario is taking longer than this page will wait. The solve '
          + 'is still running on the server — reopen Scenario Planning in a few '
          + 'minutes and it will be listed if it completed.'
        : (err && err.message
            ? `The scenario could not be solved: ${err.message}`
            : 'The scenario could not be solved.');
      // The solver's own diagnosis, when the refusal was an infeasible solve.
      // `ApplicationError.details` now carries the error's `context`.
      reportFailure('simulate', message,
                    (err && err.details && err.details.infeasibility) || null);
      return;
    }

    stepDone('simulate', {
      detail: 'Solved on the server, collected after this page stopped waiting',
      executionId: solved.execution_id,
    });
  }

  stepStart('compare');
  const mapped = mapScenarioRecord(solved);
  if (!mapped) {
    reportFailure('compare',
                  'The solver returned no usable result for this scenario.');
    return;
  }
  withdrawBackgroundExit();
  if (!backgrounded) {
    stepDone('compare', { detail: 'Measured against the baseline' });
    finishRun({});
    dismissAgentLoading(500);
  }

  // Install the baseline if this is the first scenario of the session.
  //
  // Every scenario response carries the same `baseline_kpis` — the snapshot
  // solve it was measured against — so the comparison has a real reference
  // column from the very first scenario, without waiting for a page reload.
  if (!baselineScenario()) {
    const baselineRow = baselineFromScenarioRecord(solved);
    if (baselineRow) SCENARIOS.unshift(baselineRow);
  }

  mapped.num = SCENARIOS.length;
  SCENARIOS.push(mapped);

  // Make room by dropping the OLDEST compared scenario, not by overwriting the
  // newest. The previous line replaced `multiSelectedIds[length - 1]`, which —
  // with two phantom prototype ids permanently occupying the first two slots —
  // meant every scenario after the first overwrote the one before it. Only one
  // user scenario was ever on screen, however many had been solved.
  if (multiSelectedIds.length >= MAX_COMPARED) multiSelectedIds.shift();
  multiSelectedIds.push(mapped.id);
  mapActiveId = mapped.id;

  const modal = document.getElementById('modal-create-toolbox');
  if (modal) modal.classList.remove('visible');

  renderScenarioSelector();
  renderMultiScenarioTable();
  renderMultiScenarioTakeCard();
  renderScenarioMapToggle();
  updateScenarioMap();

  // Tell whoever walked away, wherever they walked to.
  //
  // Not a modal. The reader left this run precisely so they could work
  // somewhere else, and taking that screen away from them the moment the
  // solve lands would undo the thing they asked for. The card names the
  // scenario and carries the one action that follows from it; it stays until
  // it is read.
  if (backgrounded) {
    finishBackgroundTask(taskId, {
      message: 'Solved and measured against your baseline.',
      openLabel: 'View results',
      onOpen: () => {
        mapActiveId = mapped.id;
        if (typeof window.navigateToTab === 'function') {
          window.navigateToTab('scenarios');
        }
        renderScenarioSelector();
        renderMultiScenarioTable();
        renderMultiScenarioTakeCard();
        renderScenarioMapToggle();
        updateScenarioMap();
      },
    });
  }
}

// ─── Customize Metrics Popover ───────────────────────────────
// Lets the user pick which rows show in the comparison table.
function customizeMenuHtml(visibleKeys) {
  return ALL_TABLE_ROWS.map((row) => `
    <label class="scn-customize-item">
      <input type="checkbox" data-metric-key="${row.key}" ${visibleKeys.includes(row.key) ? 'checked' : ''}>
      <span>${row.icon} ${row.label}</span>
    </label>
  `).join('');
}

function wireCustomizeMenu(btnId, menuId, getKeys, rerender) {
  const btn = document.getElementById(btnId);
  const menu = document.getElementById(menuId);
  if (!btn || !menu) return;

  menu.innerHTML = customizeMenuHtml(getKeys());
  menu.addEventListener('click', (e) => e.stopPropagation());

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    menu.classList.toggle('open');
  });

  menu.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener('change', () => {
      const key = cb.dataset.metricKey;
      const keys = getKeys();
      if (cb.checked) {
        if (!keys.includes(key)) keys.push(key);
      } else {
        const idx = keys.indexOf(key);
        if (idx !== -1) {
          if (keys.length <= 1) { cb.checked = true; return; }
          keys.splice(idx, 1);
        }
      }
      rerender();
    });
  });
}

// ─── Wire Scenario Events ───────────────────────────────────
// initScenarios() runs both at app boot and every time the user
// navigates to the Scenario Planning tab (see app.js), so this must be
// idempotent — otherwise listeners stack up on every visit, and a
// simple toggle (like "View detailed comparison") ends up firing an
// even number of times per click and appearing to do nothing at all.
let eventsWired = false;
function wireScenarioEvents() {
  if (eventsWired) return;
  eventsWired = true;

  // "Add Scenario" popover
  document.getElementById('scn-add-scenario-btn')?.addEventListener('click', (e) => {
    e.stopPropagation();
    if (multiSelectedIds.length >= 3) return;
    document.getElementById('scn-add-scenario-menu')?.classList.toggle('open');
  });

  // "Customize metrics" popover
  wireCustomizeMenu('scn-multi-customize-btn', 'scn-multi-customize-menu', () => multiVisibleKeys, renderMultiScenarioTable);

  // Click outside any open dropdown/popover closes it.
  document.addEventListener('click', () => {
    document.getElementById('scn-add-scenario-menu')?.classList.remove('open');
    document.getElementById('scn-multi-customize-menu')?.classList.remove('open');
  });

  // Toolbox Modal events (Create Scenario)
  document.getElementById('btn-close-toolbox')?.addEventListener('click', () => {
    document.getElementById('modal-create-toolbox')?.classList.remove('visible');
  });
  document.getElementById('btn-cancel-toolbox')?.addEventListener('click', () => {
    document.getElementById('modal-create-toolbox')?.classList.remove('visible');
  });

  document.querySelectorAll('.scn-type-card').forEach((card) => {
    card.addEventListener('click', () => {
      document.querySelectorAll('.scn-type-card').forEach((c) => c.classList.remove('active'));
      card.classList.add('active');
      const type = card.dataset.type;
      renderToolboxDynamicFields(type);
    });
  });

  // Single step, as in the approved design: "Run Scenario" executes.
  document.getElementById('btn-run-toolbox-scenario')?.addEventListener('click', () => {
    runScenarioCreation();
  });

  document.getElementById('btn-create-scenario-main')?.addEventListener('click', () => {
    openCreateToolbox();
  });

  // Scenario Detail Drawer ("Why this scenario?" / "Review proposed changes")
  document.getElementById('scenario-drawer-close')?.addEventListener('click', () => {
    document.getElementById('scenario-drawer-overlay')?.classList.remove('visible');
  });
}

