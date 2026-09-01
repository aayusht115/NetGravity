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

import { SCENARIOS, DCS, PLANTS, MARKETS, formatNumber, formatCurrency } from './data.js';
import { initMap, renderScenarioDigitalTwin, invalidateMapSize } from './map.js';
import { scenarioService } from './integration/services/scenario-service.js';
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
    label: 'Total Cost (₹)',
    fmt: (v) => `₹${(v / 100000).toFixed(2)}L`,
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
    fmt: (v) => `₹${(v / 100000).toFixed(2)}L`,
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
    fmt: (v) => `₹${(v / 100000).toFixed(2)}L`,
    provenance: 'MODEL FACT',
    category: 'financial',
  },
  inventoryCost: {
    key: 'inventoryCost',
    label: 'Inventory Holding Cost',
    fmt: (v) => `₹${(v / 100000).toFixed(2)}L`,
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
  { key: 'totalCost', label: 'Total Network Cost', sub: '(₹ per period)', icon: '💰', unit: 'currency', kind: 'lowerBetter' },
  { key: 'costChange', label: 'Savings %', sub: '(vs baseline)', icon: '💹', unit: 'percent', kind: 'lowerBetter',
    fmt: (v) => (v === 0 ? '—' : `${v < 0 ? '↓' : '↑'} ${Math.abs(v).toFixed(1)}%`) },
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
  { key: 'transportCost', label: 'Transport Cost', sub: '(₹ per period)', icon: '🚛', unit: 'currency', kind: 'lowerBetter' },
  { key: 'fixedCost', label: 'Fixed Facility Cost', sub: '(₹ per period)', icon: '🏭', unit: 'currency', kind: 'lowerBetter' },
  { key: 'handlingCost', label: 'Handling Cost', sub: '(₹ per period)', icon: '📥', unit: 'currency', kind: 'lowerBetter' },
  { key: 'inventoryCost', label: 'Inventory Cost', sub: '(₹ per period)', icon: '📦', unit: 'currency', kind: 'lowerBetter' },
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

  // Initialize visual context 2D digital twin map
  setTimeout(() => {
    initMap('scenario-leaflet-map', {
      zoom: 4.2,
      center: [22.5, 79.5],
      isCompact: true,
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
function scenarioRowMetricCellHtml(row) {
  return `<span class="scn-row2-icon">${row.icon}</span>
    <div><div class="scn-row2-label">${row.label}</div><div class="scn-row2-sub">${row.sub}</div></div>`;
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
    container.innerHTML = '<div class="text-xs text-muted" style="padding:18px">'
      + 'This network has not been solved, so there is no baseline to compare '
      + 'scenarios against.</div>';
    return;
  }
  if (!selected.length) {
    container.innerHTML = '<div class="text-xs text-muted" style="padding:18px">'
      + 'No scenario selected. Create one with <strong>Create New Scenario</strong>, '
      + 'or add an existing one from <strong>+ Add scenario to compare</strong>.'
      + '</div>';
    return;
  }

  const rows = ALL_TABLE_ROWS.filter((r) => multiVisibleKeys.includes(r.key));

  const theadCols = selected
    .map((s, i) => `<th class="${i === 0 ? 'scn-th-rec2' : ''}" style="text-align:center">${scenarioDisplayName(s)}${i === 0 ? ' <span class="scn-sparkle-inline">✦</span>' : ''}</th>`)
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
}

// ─── "NetGravity's take" card ────────────────────────────────
//
// Rewritten from the scenarios that are actually on screen.
//
// Every line of this card used to read a field the mapper does not produce —
// `scn.highlight`, `scn.description`, `scn.aiAssessment.recommendation` — so
// the headline and paragraph rendered the string "undefined", and the trade-off
// line computed `undefined - undefined`. It also always described
// `multiSelectedIds[0]`, which was the phantom `SCN_REBALANCE`, falling through
// to `SCENARIOS[1]`: the card never changed when the user selected a different
// scenario, and never updated when a new one was created.
//
// It now RANKS the selected scenarios on their solved cost against the
// baseline, names the best one, and says what it costs in service — with no
// narrative that is not derived from two numbers.

/**
 * Rank the compared scenarios and pick one to recommend.
 *
 * Cheapest-first on business network cost, but a scenario that strands more
 * demand than the baseline is flagged rather than silently recommended: on this
 * engine the cheapest plan is frequently the one that serves least, and cost
 * alone would recommend closing the network.
 */
function rankScenarios(baseline, scenarios) {
  return scenarios
    .filter((s) => typeof s.totalCost === 'number')
    .map((s) => {
      const costDelta = typeof baseline.totalCost === 'number'
        ? s.totalCost - baseline.totalCost : null;
      const fillDelta = (typeof s.fillRate === 'number'
                         && typeof baseline.fillRate === 'number')
        ? s.fillRate - baseline.fillRate : null;
      return { scenario: s, costDelta, fillDelta };
    })
    .sort((a, b) => (a.costDelta ?? Infinity) - (b.costDelta ?? Infinity));
}

function takeCheckItem(good, text) {
  return `
    <div class="scn-take-check-item">
      <span class="scn-take-check-icon ${good === null ? 'warn' : (good ? 'good' : 'warn')}">${good ? '✓' : '!'}</span>
      <span>${text}</span>
    </div>`;
}

function renderMultiScenarioTakeCard() {
  const container = document.getElementById('scn-multi-take-card');
  if (!container) return;

  const baseline = baselineScenario();
  const selected = multiSelectedIds
    .map((id) => SCENARIOS.find((s) => s.id === id)).filter(Boolean);

  if (!baseline || !selected.length) {
    container.innerHTML = `
      <div class="scn-take-head">
        <span class="scn-take-icon">✨</span>
        <span class="scn-take-title">NetGravity's Recommendation</span>
      </div>
      <div class="text-xs text-muted" style="padding:10px 0;line-height:1.6">
        ${baseline
          ? 'No scenario is selected, so there is nothing to recommend. Create '
            + 'one and it will be compared against your solved network.'
          : 'This network has not been solved, so there is no baseline to judge '
            + 'a scenario against.'}
      </div>`;
    return;
  }

  const ranked = rankScenarios(baseline, selected);
  if (!ranked.length) {
    container.innerHTML = `
      <div class="scn-take-head">
        <span class="scn-take-icon">✨</span>
        <span class="scn-take-title">NetGravity's Recommendation</span>
      </div>
      <div class="text-xs text-muted" style="padding:10px 0;line-height:1.6">
        None of the selected scenarios produced a cost the engine could report,
        so there is nothing to rank. Open a scenario to see why.
      </div>`;
    return;
  }

  const best = ranked[0];
  const scn = best.scenario;
  const saves = best.costDelta !== null && best.costDelta < 0;
  const changes = describeScenarioChanges(scn);

  const headline = best.costDelta === null
    ? `${scenarioDisplayName(scn)} — no comparable baseline cost`
    : saves
      ? `${scenarioDisplayName(scn)} is the cheapest of the ${ranked.length} compared, `
        + `at ${formatCurrency(Math.abs(best.costDelta))} below your current network`
      : `None of the ${ranked.length} compared beats your current network on cost; `
        + `${scenarioDisplayName(scn)} is the closest, at `
        + `${formatCurrency(Math.abs(best.costDelta))} above it`;

  // Attribute the saving honestly.
  //
  // Most of the headline reduction on this kind of network is not the change at
  // all — it is the value of letting the solver re-optimise a footprint the
  // baseline holds fixed. Reporting the whole gap as the scenario's doing is
  // how three unrelated changes come to read as "-47%" apiece.
  const reoptEffect = (typeof scn.referenceCost === 'number'
                       && typeof baseline.totalCost === 'number')
    ? scn.referenceCost - baseline.totalCost : null;
  const attribution = (reoptEffect === null || scn.changeEffect === null)
    ? ''
    : Math.abs(scn.changeEffect) < 1
      ? `<p class="scn-take-para"><strong>The change itself moves nothing.</strong> `
        + `All ${formatCurrency(Math.abs(reoptEffect))} of the difference comes from `
        + `re-optimising the footprint you already have — the solver reaches the `
        + `same plan with or without this change.</p>`
      : `<p class="scn-take-para">Of that difference, `
        + `<strong>${formatCurrency(Math.abs(reoptEffect))}</strong> comes from `
        + `re-optimising the footprint you already have, and `
        + `<strong>${scn.changeEffect < 0 ? 'a further ' : ''}`
        + `${formatCurrency(Math.abs(scn.changeEffect))}</strong> from this change `
        + `${scn.changeEffect < 0 ? 'on top of it' : 'against it'}.</p>`;

  // The service trade-off is stated even when it is bad — especially when it is
  // bad. A cheaper plan that strands more demand is not a saving.
  const fillLine = best.fillDelta === null
    ? 'Fill rate is not comparable between these two solves.'
    : best.fillDelta < -0.05
      ? `It serves <strong>${Math.abs(best.fillDelta).toFixed(1)} points less</strong> of demand than today — the saving is partly a smaller promise.`
      : best.fillDelta > 0.05
        ? `It also serves <strong>${best.fillDelta.toFixed(1)} points more</strong> of demand.`
        : 'Demand served is essentially unchanged.';

  const rest = ranked.slice(1).map((r) => `
    <div class="flex items-center justify-between text-xs" style="padding:4px 0;border-bottom:1px solid var(--border-light)">
      <span>${scenarioDisplayName(r.scenario)}</span>
      <span style="font-weight:700;color:${(r.costDelta ?? 0) < 0 ? 'var(--green)' : 'var(--red)'}">
        ${r.costDelta === null ? '—' : `${r.costDelta < 0 ? '↓' : '↑'} ${formatCurrency(Math.abs(r.costDelta))}`}
      </span>
    </div>`).join('');

  container.innerHTML = `
    <div class="scn-take-head">
      <span class="scn-take-icon">✨</span>
      <span class="scn-take-title">NetGravity's Recommendation</span>
    </div>
    <div class="scn-take-headline">${headline}</div>
    ${attribution}
    <p class="scn-take-para">${fillLine}</p>
    <div class="scn-take-section-title">What it does</div>
    <div class="scn-take-checklist">
      ${changes.length
        ? changes.map((c) => takeCheckItem(c.kind !== 'declined', c.text)).join('')
        : takeCheckItem(null, 'The solver reached the same plan as the baseline — this change moved nothing.')}
      ${takeCheckItem(saves, `Total network cost <strong>${formatCurrency(scn.totalCost)}</strong> vs <strong>${formatCurrency(baseline.totalCost)}</strong> today`)}
      ${takeCheckItem(
        baseline.capacityRisk === scn.capacityRisk ? null
          : riskRank(scn.capacityRisk) < riskRank(baseline.capacityRisk),
        `Capacity risk <strong>${baseline.capacityRisk} → ${scn.capacityRisk}</strong>`)}
    </div>
    ${rest ? `
      <div class="scn-take-section-title" style="margin-top:12px">Also compared</div>
      ${rest}` : ''}
    <div class="text-xs text-muted" style="margin-top:12px;line-height:1.5">
      Ranked on solved network cost from the MILP. No figure on this card is
      estimated.
    </div>
    <button type="button" class="scn-take-review-btn" data-take-review>Review proposed changes <span>→</span></button>
  `;
  container.querySelector('[data-take-review]')
    ?.addEventListener('click', () => openScenarioDrawer(scn.id));
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

  const formBody = document.getElementById('toolbox-form-body');
  const execView = document.getElementById('agent-execution-view');
  if (formBody) formBody.classList.remove('hidden');
  if (execView) execView.classList.add('hidden');
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
  return all + source
    .map((f, i) => `<option value="${f.id}"${(i === 0 && !includeAll) ? ' selected' : ''}>`
      + `${f.name || f.id}</option>`)
    .join('');
}

/**
 * Cities to place a new site at, as a shortcut for typing coordinates.
 *
 * A NEW facility can go anywhere — the two coordinate inputs beside this list
 * are the authority, and they are editable. This is a convenience so the common
 * case does not require looking up a latitude, not a restriction on where a
 * site may be proposed. The coordinates are ordinary public geography, not a
 * claim about the client's network.
 */
const INDIA_SITE_PRESETS = [
  ['Ahmedabad', 23.0225, 72.5714], ['Bengaluru', 12.9716, 77.5946],
  ['Bhopal', 23.2599, 77.4126], ['Bhubaneswar', 20.2961, 85.8245],
  ['Chandigarh', 30.7333, 76.7794], ['Chennai', 13.0827, 80.2707],
  ['Coimbatore', 11.0168, 76.9558], ['Dehradun', 30.3165, 78.0322],
  ['Delhi NCR', 28.6139, 77.2090], ['Guwahati', 26.1445, 91.7362],
  ['Hyderabad', 17.3850, 78.4867], ['Indore', 22.7196, 75.8577],
  ['Jaipur', 26.9124, 75.7873], ['Kanpur', 26.4499, 80.3319],
  ['Kochi', 9.9312, 76.2673], ['Kolkata', 22.5726, 88.3639],
  ['Lucknow', 26.8467, 80.9462], ['Ludhiana', 30.9010, 75.8573],
  ['Madurai', 9.9252, 78.1198], ['Mumbai', 19.0760, 72.8777],
  ['Nagpur', 21.1458, 79.0882], ['Nashik', 19.9975, 73.7898],
  ['Patna', 25.5941, 85.1376], ['Pune', 18.5204, 73.8567],
  ['Raipur', 21.2514, 81.6296], ['Rajkot', 22.3039, 70.8022],
  ['Ranchi', 23.3441, 85.3096], ['Siliguri', 26.7271, 88.3953],
  ['Surat', 21.1702, 72.8311], ['Vadodara', 22.3072, 73.1812],
  ['Varanasi', 25.3176, 82.9739], ['Visakhapatnam', 17.6868, 83.2185],
];

function sitePresetOptionsHtml() {
  // Nothing is pre-selected. Index 20 (Nagpur) used to be, which meant the form
  // opened already proposing a specific Indian city — a suggestion the product
  // had no basis for, sitting in the same fields as the numbers derived from
  // the user's own network. The user picks, or types coordinates.
  return '<option value="" selected>Choose a city, or type coordinates below</option>'
    + INDIA_SITE_PRESETS
      .map(([name, lat, lng]) => `<option value="${lat},${lng}">${name}</option>`)
      .join('');
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
      'Commit to keeping an existing site open, or propose a new one anywhere '
      + 'in India.');
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
          <option value="NEW" selected>A new site — anywhere in India</option>
          <option value="EXISTING">One of my existing sites, pinned open</option>
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
            cheaper.
          </div>
        </div>
      </div>

      <div id="toolbox-open-new">
        <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
          <div class="form-group">
            <label class="form-label">Site name</label>
            <input type="text" class="form-input" id="toolbox-site-name" placeholder="Name this site" maxlength="48" value="">
          </div>
          <div class="form-group">
            <label class="form-label">Jump to a city</label>
            <select class="form-select" id="toolbox-site-city">
              ${sitePresetOptionsHtml()}
            </select>
          </div>
        </div>
        <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
          <div class="form-group">
            <label class="form-label">Latitude</label>
            <input type="number" class="form-input" id="toolbox-site-lat" value="${centre ? centre.lat : ''}" placeholder="Latitude" step="0.0001" min="6" max="38">
          </div>
          <div class="form-group">
            <label class="form-label">Longitude</label>
            <input type="number" class="form-input" id="toolbox-site-lng" value="${centre ? centre.lng : ''}" placeholder="Longitude" step="0.0001" min="67" max="98">
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
            <label class="form-label">Fixed cost (₹ per year)</label>
            <input type="number" class="form-input" id="toolbox-site-fixed" value="${fixed != null ? Math.round(fixed) : ''}" placeholder="Per year" min="0" step="100000">
          </div>
          <div class="form-group">
            <label class="form-label">Handling cost (₹ per unit)</label>
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
    setHeader('Change Demand',
      'Scale every demand row up or down and re-solve. Useful for a peak-season '
      + 'or a downturn case.');
    container.innerHTML = `
      <div class="grid-2" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Demand change (%)</label>
          <input type="number" class="form-input" id="toolbox-amount" value="15" min="-90" max="200" step="5">
        </div>
        <div class="form-group">
          <label class="form-label">Scope</label>
          <input type="text" class="form-input" value="Every market and product" disabled>
        </div>
      </div>
      <div class="text-xs text-muted" style="margin-top:6px">
        Applies to all demand rows in the network. Per-market demand changes are
        not exposed here.
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

const CREATION_STEPS = [
  'Validating scenario',
  'Submitting to orchestrator',
  'Running network optimisation',
  'Comparing against baseline',
  'Reading authoritative KPIs',
];

function renderCreationProgress(activeIndex, failed = false) {
  for (let i = 1; i <= CREATION_STEPS.length; i++) {
    const el = document.getElementById(`step-phase-${i}`);
    if (!el) continue;
    const label = CREATION_STEPS[i - 1];
    el.classList.remove('text-muted');
    if (failed && i === activeIndex) {
      el.innerHTML = `<span>${label}</span><span style="color:var(--red);font-weight:700">✕</span>`;
    } else if (i < activeIndex) {
      el.innerHTML = `<span>${label}</span><span style="color:var(--green);font-weight:700">✓</span>`;
    } else if (i === activeIndex) {
      el.innerHTML = `<span>${label}</span><span class="telemetry-spinner"></span>`;
    } else {
      el.innerHTML = `<span>${label}</span>`;
      el.classList.add('text-muted');
    }
  }
}

function showCreationError(message) {
  const execView = document.getElementById('agent-execution-view');
  if (!execView) return;
  let banner = document.getElementById('scn-creation-error');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'scn-creation-error';
    banner.className = 'alert alert-error';
    banner.style.cssText = 'margin-top:12px;padding:10px 12px;border-radius:8px;'
      + 'background:rgba(220,38,38,.08);color:var(--red);font-size:12px;line-height:1.5';
    execView.appendChild(banner);
  }
  banner.textContent = message;

  // A failed run must be recoverable without closing and re-opening the modal.
  if (!document.getElementById('scn-creation-back')) {
    const back = document.createElement('button');
    back.id = 'scn-creation-back';
    back.className = 'btn btn-secondary btn-sm';
    back.style.cssText = 'margin-top:10px';
    back.textContent = 'Back to the form';
    back.addEventListener('click', () => {
      document.getElementById('agent-execution-view')?.classList.add('hidden');
      document.getElementById('toolbox-form-body')?.classList.remove('hidden');
      document.getElementById('scn-creation-error')?.remove();
      back.remove();
    });
    execView.appendChild(back);
  }
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

      if (!siteName) return { error: 'Give the new site a name.' };
      if (lat === null || lng === null) {
        return { error: 'Enter a latitude and longitude for the new site, or pick a city.' };
      }
      if (capacity === null || capacity <= 0) {
        return { error: 'A new site needs a capacity above zero — a site with no capacity cannot serve anything.' };
      }
      if (fixed === null || handling === null) {
        return { error: 'Enter the fixed cost per year and the handling cost per unit. '
                      + 'They decide whether opening this site pays, so they are not assumed.' };
      }
      if (fixed < 0 || handling < 0) {
        return { error: 'Fixed and handling costs cannot be negative.' };
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
  const formBody = document.getElementById('toolbox-form-body');
  const execView = document.getElementById('agent-execution-view');
  if (formBody) formBody.classList.add('hidden');
  if (execView) execView.classList.remove('hidden');

  document.getElementById('scn-creation-error')?.remove();
  document.getElementById('scn-creation-back')?.remove();

  // The form validates itself now and says which field is wrong. It used to
  // check only `facility_ids.length` and print "Select a facility before
  // running this scenario" — which was the wrong advice for the three scenario
  // types that name no facility, and the only advice available for a form that
  // rendered no facility field for them in the first place.
  const { body, error } = readScenarioForm();
  if (error) {
    renderCreationProgress(1, true);
    showCreationError(error);
    return;
  }

  renderCreationProgress(2);

  let solved;
  try {
    solved = await scenarioService.simulateScenario(body);
  } catch (err) {
    // Explicit failure. No scenario is added, and nothing is fabricated to
    // fill the gap.
    renderCreationProgress(3, true);
    showCreationError(
      err && err.message
        ? `The scenario could not be solved: ${err.message}`
        : 'The scenario could not be solved.',
    );
    return;
  }

  renderCreationProgress(CREATION_STEPS.length + 1);

  const mapped = mapScenarioRecord(solved);
  if (!mapped) {
    showCreationError('The solver returned no usable result for this scenario.');
    return;
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

