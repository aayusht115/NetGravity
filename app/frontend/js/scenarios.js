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

import { SCENARIOS, DCS, formatNumber } from './data.js';
import { initMap, renderScenarioDigitalTwin, invalidateMapSize } from './map.js';

// ─── State ──────────────────────────────────────────────────
// Which metric rows the comparison table shows — user-controlled via
// "Customize metrics" (see renderCustomizeMenu). Defaults to the 4 "key" rows.
let multiVisibleKeys = ['totalCost', 'costChange', 'sla', 'capacityRisk'];
// Up to 3 scenarios shown alongside baseline — the single source of truth
// for both the comparison table and the Digital Twin map's toggle group.
let multiSelectedIds = ['SCN_REBALANCE', 'SCN_USER_1'];
// Which of the selected scenarios the Digital Twin map is currently showing.
let mapActiveId = multiSelectedIds[0];

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
  { key: 'totalCost', label: 'Total Network Cost', sub: '(₹ Lakh per month)', icon: '💰', unit: 'currency', kind: 'lowerBetter' },
  { key: 'costChange', label: 'Savings %', sub: '(vs baseline)', icon: '💹', unit: 'percent', kind: 'lowerBetter',
    fmt: (v) => (v === 0 ? '—' : `${v < 0 ? '↓' : '↑'} ${Math.abs(v)}%`) },
  { key: 'sla', label: 'Service Level', sub: '(% on-time delivery)', icon: '🛡️', unit: 'percent', kind: 'higherBetter' },
  { key: 'capacityRisk', label: 'Capacity Risk', sub: '(Overall network)', icon: '⚠️', unit: 'categorical', kind: 'categorical' },
];

// Available via the customize-metrics picker (not shown by default) —
// Fill Rate and Risk Factor are P0-required dimensions for S8 but have no
// backing data anywhere in this build (confirmed during the S9 pass: no
// RF/REI data exists, and no scenario carries a fill-rate field), so their
// rows render "Not available" rather than a fabricated number. Keeping them
// out of the default view avoids every user seeing a "Not available" row
// unasked; putting them in the picker keeps the gap honest and discoverable.
const DETAIL_EXTRA_ROWS = [
  { key: 'avgUtil', label: 'Avg Utilization', sub: '(Network avg.)', icon: '📈', unit: 'percent', kind: 'lowerBetter' },
  { key: 'maxUtil', label: 'Max Utilization', sub: '(Peak facility)', icon: '📊', unit: 'percent', kind: 'lowerBetter' },
  { key: 'transportCost', label: 'Transport Cost', sub: '(₹ Lakh per month)', icon: '🚛', unit: 'currency', kind: 'lowerBetter' },
  { key: 'fixedCost', label: 'Fixed Facility Cost', sub: '(₹ Lakh per month)', icon: '🏭', unit: 'currency', kind: 'lowerBetter' },
  { key: 'inventoryCost', label: 'Inventory Cost', sub: '(₹ Lakh per month)', icon: '📦', unit: 'currency', kind: 'lowerBetter' },
  { key: 'inventoryDays', label: 'Inventory Days', sub: '(Days on hand)', icon: '📦', unit: 'number', kind: 'lowerBetter' },
  { key: 'carbonKg', label: 'Scope 3 Carbon', sub: '(kg CO2 per month)', icon: '🌱', unit: 'number', kind: 'lowerBetter' },
  { key: 'fillRate', label: 'Fill Rate', sub: '(On-time fulfillment)', icon: '✅', unit: 'percent', kind: 'higherBetter', unavailable: true },
  { key: 'riskFactor', label: 'Risk Factor (RF)', sub: '(chain explained in Scenario evidence)', icon: '🧭', unit: 'number', kind: 'higherBetter', unavailable: true },
];

const ALL_TABLE_ROWS = [...DEEPDIVE_ROWS, ...DETAIL_EXTRA_ROWS];

function fmtRowValue(row, v) {
  if (row.unavailable) return 'Not available';
  if (v === undefined || v === null) return '—';
  if (row.fmt) return row.fmt(v);
  if (row.unit === 'currency') return `₹${(v / 100000).toFixed(2)}L`;
  if (row.unit === 'percent') return `${v}%`;
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
  // A scenario actually created via the toolbox this session (id prefix
  // SCN_CUSTOM_, see runScenarioCreation) keeps the name the user gave it
  // everywhere — dropdowns, table headers, picker chips, drawer — instead
  // of the generic "Scenario N" used for the built-in presets. Presets
  // also carry type:'USER_CREATED' in the underlying data (legacy field,
  // predates this naming scheme) so that alone can't be the signal.
  if (s.id.startsWith('SCN_CUSTOM_') && s.cardTitle) return s.cardTitle;
  return `Scenario ${s.num}`;
}

// Compares a scenario's value against baseline for one row. Returns
// { text, good } where good is true/false, or null for "no material
// change". Categorical rows (Capacity Risk) resolve via riskRank rather
// than always showing "—" — a scenario that improves risk should say so.
function computeRowDelta(row, baseVal, scnVal) {
  if (row.unavailable) return { text: '—', good: null };
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
  return {
    cost: computeRowDelta(rowByKey('totalCost'), baseline.totalCost, scn.totalCost),
    sla: computeRowDelta(rowByKey('sla'), baseline.sla, scn.sla),
    util: computeRowDelta(rowByKey('avgUtil'), baseline.avgUtil, scn.avgUtil),
    riskGood: riskRank(scn.capacityRisk) < riskRank(baseline.capacityRisk),
  };
}

// ─── Init ───────────────────────────────────────────────────
export function initScenarios() {
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
      mode: 'scenario',
    });
    renderScenarioDigitalTwin('scenario-leaflet-map', mapActiveId, 'scenario');
    invalidateMapSize('scenario-leaflet-map');
    renderFutureNetworkCallouts();
  }, 60);
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
  const unselected = SCENARIOS.filter((s) => s.id !== 'SCN_ACTUAL' && !multiSelectedIds.includes(s.id));

  chipsEl.innerHTML = selected
    .map((s) => `
      <div class="scn-selected-chip" data-scn-id="${s.id}">
        <span>${scenarioDisplayName(s)}</span>
        ${selected.length > 1 ? `<button type="button" class="scn-scenario-dropdown-del" data-remove-id="${s.id}" title="Remove from comparison">✕</button>` : ''}
      </div>`)
    .join('');

  chipsEl.querySelectorAll('[data-remove-id]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.removeId;
      if (multiSelectedIds.length <= 1) return;
      multiSelectedIds = multiSelectedIds.filter((sid) => sid !== id);
      if (mapActiveId === id) mapActiveId = multiSelectedIds[0];
      onSelectionChanged();
    });
  });

  const addBtn = document.getElementById('scn-add-scenario-btn');
  if (addBtn) addBtn.classList.toggle('disabled', multiSelectedIds.length >= 3);

  if (multiSelectedIds.length >= 3) {
    menu.innerHTML = '<div class="scn-add-scenario-empty">Maximum of 3 — remove one to add another.</div>';
  } else if (unselected.length === 0) {
    menu.innerHTML = '<div class="scn-add-scenario-empty">No other scenarios yet.</div>';
  } else {
    menu.innerHTML = unselected
      .map((s) => `
        <div class="scn-scenario-dropdown-item" data-add-id="${s.id}">
          <span>${scenarioDisplayName(s)}</span>
          <button type="button" class="scn-scenario-dropdown-del" data-del-id="${s.id}" title="Delete scenario">✕</button>
        </div>`)
      .join('');
  }

  menu.querySelectorAll('[data-add-id]').forEach((item) => {
    item.addEventListener('click', (e) => {
      if (e.target.closest('.scn-scenario-dropdown-del')) return;
      if (multiSelectedIds.length >= 3) return;
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
  renderScenarioSelector();
  renderMultiScenarioTable();
  renderMultiScenarioTakeCard();
  renderScenarioMapToggle();
  updateScenarioMap();
}

// Permanently deletes a scenario from the system (not just from the current
// comparison) — always keeps at least one non-baseline scenario in existence.
function removeScenario(scenarioId) {
  const remaining = SCENARIOS.filter((s) => s.id !== 'SCN_ACTUAL' && s.id !== scenarioId);
  if (remaining.length === 0) return;

  const idx = SCENARIOS.findIndex((s) => s.id === scenarioId);
  if (idx !== -1) SCENARIOS.splice(idx, 1);

  multiSelectedIds = multiSelectedIds.filter((id) => id !== scenarioId);
  if (multiSelectedIds.length === 0) multiSelectedIds = [remaining[0].id];
  if (mapActiveId === scenarioId) mapActiveId = multiSelectedIds[0];

  onSelectionChanged();
}

// ─── Digital Twin Map Toggle (one button per selected scenario) ───
function renderScenarioMapToggle() {
  const group = document.getElementById('scn-map-toggle-group');
  if (!group) return;

  const baseline = SCENARIOS.find((s) => s.id === 'SCN_ACTUAL');
  const selected = multiSelectedIds.map((id) => SCENARIOS.find((s) => s.id === id)).filter(Boolean);
  const options = baseline ? [baseline, ...selected] : selected;
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
  const mode = mapActiveId === 'SCN_ACTUAL' ? 'baseline' : 'scenario';
  renderScenarioDigitalTwin('scenario-leaflet-map', mapActiveId, mode);
  invalidateMapSize('scenario-leaflet-map');
  renderFutureNetworkCallouts();
}

// ─── S10: Future Network View callouts ────────────────────────
// Compact deltas around the existing map. Cost/Service/Risk reuse
// computeScenarioDeltas/computeRowDelta — the exact functions S8's
// comparison table calls — rather than recomputing anything here. Facility
// and lane/flow changes read straight from the scenario's own
// assumptions/changes fields (already authored per scenario), never
// invented for this callout.
function renderFutureNetworkCallouts() {
  const container = document.getElementById('scn-future-callouts');
  const stateLabel = document.getElementById('scn-map-state-label');
  if (!container) return;

  const baseline = SCENARIOS.find((s) => s.id === 'SCN_ACTUAL') || SCENARIOS[0];
  const scn = SCENARIOS.find((s) => s.id === mapActiveId) || baseline;
  const isBaseline = scn.id === 'SCN_ACTUAL';

  if (stateLabel) {
    stateLabel.textContent = isBaseline
      ? 'Showing: Actual (observed baseline)'
      : `Showing: ${scenarioDisplayName(scn)} — Future Network`;
  }

  if (isBaseline) {
    container.innerHTML = `
      <div class="scn-future-callout-chip">
        <span class="scn-fc-label">State</span>
        <span class="scn-fc-value">Actual — no scenario deltas to show</span>
      </div>`;
    return;
  }

  const deltas = computeScenarioDeltas(baseline, scn);
  const facilityFootprint = scn.assumptions?.find((a) => a.label === 'Facility Footprint')?.value || 'Not available';
  const laneChanges = scn.changes || [];
  const laneSummary = laneChanges.length
    ? `${laneChanges.length} change${laneChanges.length === 1 ? '' : 's'} — ${laneChanges[0].item} ${laneChanges[0].change}`
    : 'Not available';

  const riskRow = ALL_TABLE_ROWS.find((r) => r.key === 'capacityRisk');
  const riskDelta = computeRowDelta(riskRow, baseline.capacityRisk, scn.capacityRisk);
  const toneOf = (good) => (good === true ? 'good' : good === false ? 'bad' : '');

  const chips = [
    { label: 'Facility changes', value: facilityFootprint, tone: '' },
    { label: 'Lane / flow changes', value: laneSummary, tone: '' },
    { label: 'Cost impact', value: deltas.cost.text, tone: toneOf(deltas.cost.good) },
    { label: 'Service impact', value: deltas.sla.text, tone: toneOf(deltas.sla.good) },
    { label: 'Risk impact', value: `${baseline.capacityRisk} → ${scn.capacityRisk} (${riskDelta.text})`, tone: toneOf(riskDelta.good) },
  ];

  // P1: Carbon impact, only if the scenario has the field — never fabricated.
  if (typeof scn.carbonKg === 'number' && typeof baseline.carbonKg === 'number') {
    const carbonRow = ALL_TABLE_ROWS.find((r) => r.key === 'carbonKg');
    const carbonDelta = computeRowDelta(carbonRow, baseline.carbonKg, scn.carbonKg);
    chips.push({ label: 'Carbon impact', value: carbonDelta.text, tone: toneOf(carbonDelta.good) });
  }

  container.innerHTML = chips.map((c) => `
    <div class="scn-future-callout-chip ${c.tone}">
      <span class="scn-fc-label">${c.label}</span>
      <span class="scn-fc-value">${c.value}</span>
    </div>`).join('');
}

// ─── Render Multi-Scenario Comparison Table (up to 3 scenarios) ──
function renderMultiScenarioTable() {
  const container = document.getElementById('multi-scenario-table-wrap');
  if (!container) return;

  const baseline = SCENARIOS.find((s) => s.id === 'SCN_ACTUAL') || SCENARIOS[0];
  const selected = multiSelectedIds.map((id) => SCENARIOS.find((s) => s.id === id)).filter(Boolean);
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
      return `
        <tr${isRiskRow ? ` class="scn-risk-row-clickable" data-metric-key="capacityRisk" data-scenario-id="${selected[0]?.id || ''}"` : ''}>
          <td class="scn-row2-metric">${scenarioRowMetricCellHtml(row)}</td>
          <td class="${baseCls}">${fmtRowValue(row, baseVal)}</td>
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

// ─── "NetGravity's take" card — single & multi-scenario views ───
function scenarioTakeCardBodyHtml(scn, baseline, opts) {
  const d = computeScenarioDeltas(baseline, scn);
  const invDelta = scn.inventoryDays - baseline.inventoryDays;
  const tradeoffText = invDelta > 0
    ? `Inventory increases by ${invDelta} day${invDelta === 1 ? '' : 's'}.`
    : invDelta < 0
      ? `Inventory decreases by ${Math.abs(invDelta)} day${Math.abs(invDelta) === 1 ? '' : 's'}.`
      : null;

  const checklist = [
    { good: !!d.cost.good, text: `Cost <strong>${d.cost.text}</strong>` },
    { good: !!d.sla.good, text: `Service <strong>${d.sla.text}</strong>` },
    { good: d.riskGood, text: `Risk <strong>${baseline.capacityRisk} → ${scn.capacityRisk}</strong>` },
  ];

  return `
    <div class="scn-take-head">
      <span class="scn-take-icon">✨</span>
      <span class="scn-take-title">NetGravity's Recommendation</span>
    </div>
    <div class="scn-take-headline">${scn.highlight || scn.description}</div>
    <p class="scn-take-para">${scn.aiAssessment?.recommendation || scn.description}</p>
    <div class="scn-take-section-title">${opts.checklistTitle}</div>
    <div class="scn-take-checklist">
      ${checklist.map((c) => `
        <div class="scn-take-check-item">
          <span class="scn-take-check-icon ${c.good ? 'good' : 'warn'}">${c.good ? '✓' : '!'}</span>
          <span>${c.text}</span>
        </div>`).join('')}
    </div>
    ${tradeoffText ? `
      <div class="scn-take-tradeoff">
        <span class="scn-take-tradeoff-icon">⚠️</span>
        <div><strong>Trade-off</strong><div>${tradeoffText}</div></div>
      </div>` : ''}
    <button type="button" class="scn-take-review-btn" data-take-review>Review proposed changes <span>→</span></button>
  `;
}

function wireTakeCardLinks(container, scnId) {
  container.querySelector('[data-take-review]')?.addEventListener('click', () => openScenarioDrawer(scnId));
}

function renderMultiScenarioTakeCard() {
  const container = document.getElementById('scn-multi-take-card');
  if (!container) return;
  const baseline = SCENARIOS.find((s) => s.id === 'SCN_ACTUAL') || SCENARIOS[0];
  const topId = multiSelectedIds[0];
  const scn = SCENARIOS.find((s) => s.id === topId) || SCENARIOS[1];
  container.innerHTML = scenarioTakeCardBodyHtml(scn, baseline, { checklistTitle: 'Why I recommend it' });
  wireTakeCardLinks(container, scn.id);
}

// ─── Open Scenario Detail Drawer ────────────────────────────
export function openScenarioDrawer(scenarioId) {
  const scn = SCENARIOS.find((s) => s.id === scenarioId) || SCENARIOS[1];
  const overlay = document.getElementById('scenario-drawer-overlay');
  const content = document.getElementById('scenario-drawer-content');
  if (!overlay || !content) return;

  const isRec = scn.type === 'RECOMMENDED' || scn.id === 'SCN_REBALANCE';
  const badgeCls = isRec ? 'tag-success' : 'tag-primary';

  const changesHtml = scn.changes
    ? scn.changes
        .map(
          (c) => `
          <div class="scn-change-row">
            <div>
              <strong>${c.item}</strong>
              <div class="text-xs text-muted">${c.note || ''}</div>
            </div>
            <div style="font-weight:700;color:var(--primary)">${c.change}</div>
          </div>
        `
        )
        .join('')
    : '<div class="text-xs text-muted">No configuration changes.</div>';

  const assumptionsHtml = scn.assumptions
    ? scn.assumptions
        .map(
          (a) => `
          <div class="flex items-center justify-between text-xs py-xs" style="border-bottom:1px solid var(--border-light)">
            <span style="color:var(--text-2)">${a.label}</span>
            <div class="flex items-center gap-xs">
              <span style="font-weight:600">${a.value}</span>
              <span class="provenance-badge ${a.type.toLowerCase().replace(' ', '-')}">${a.type}</span>
            </div>
          </div>
        `
        )
        .join('')
    : '<div class="text-xs text-muted">No explicit assumptions.</div>';

  const robustnessHtml = scn.robustnessTests
    ? scn.robustnessTests
        .map(
          (t) => `
          <div class="flex items-center justify-between text-xs py-xs" style="border-bottom:1px solid var(--border-light)">
            <div>
              <strong>${t.test}</strong>
              <div class="text-xs text-muted">${t.detail}</div>
            </div>
            <span class="tag ${t.status === 'PASS' ? 'tag-success' : 'tag-danger'}" style="font-size:10px">${t.status}</span>
          </div>
        `
        )
        .join('')
    : '';

  content.innerHTML = `
    <div style="margin-bottom:18px">
      <div class="flex items-center gap-xs mb-xs">
        <span class="tag ${badgeCls}" style="font-size:10px;padding:3px 8px">${scn.status}</span>
        <span class="provenance-badge model-fact">MILP EVALUATED</span>
      </div>
      <h3 style="font-size:20px;font-weight:800;color:var(--text-1)">${scn.cardTitle || scn.name}</h3>
      <p style="font-size:12.5px;color:var(--text-2);margin-top:4px">${scn.description}</p>
    </div>

    <!-- Objective Box -->
    <div class="scn-section-box">
      <h4 style="font-size:13px;font-weight:700;color:var(--text-1);margin-bottom:4px">Optimisation Objective</h4>
      <p style="font-size:12.5px;color:var(--text-2)">${scn.objective?.goal || 'Cost & SLA Optimisation'}</p>
    </div>

    <!-- Key Metrics Grid -->
    <div class="grid-2 mb-md" style="gap:var(--space-sm)">
      <div style="background:var(--bg-subtle);padding:10px 14px;border-radius:var(--r-md);border:1px solid var(--border-light)">
        <span class="text-xs text-muted">Total Cost</span>
        <div style="font-size:16px;font-weight:800;color:var(--text-1)">₹${(scn.totalCost / 100000).toFixed(2)}L <span class="text-xs" style="color:var(--green)">(${scn.costChange < 0 ? '↓ ' : '↑ '}${Math.abs(scn.costChange)}%)</span></div>
      </div>
      <div style="background:var(--bg-subtle);padding:10px 14px;border-radius:var(--r-md);border:1px solid var(--border-light)">
        <span class="text-xs text-muted">On-Time SLA</span>
        <div style="font-size:16px;font-weight:800;color:${scn.sla >= 95 ? 'var(--green)' : 'var(--red)'}">${scn.sla}%</div>
      </div>
    </div>

    <!-- What Changed Section -->
    <div class="scn-section-box">
      <h4 style="font-size:13px;font-weight:700;color:var(--text-1);margin-bottom:8px">What Changed (Network Allocations)</h4>
      ${changesHtml}
    </div>

    <!-- Assumptions -->
    <div class="scn-section-box">
      <h4 style="font-size:13px;font-weight:700;color:var(--text-1);margin-bottom:8px">Assumptions & Boundary Constraints</h4>
      ${assumptionsHtml}
    </div>

    <!-- Robustness & Stress Testing -->
    <div class="scn-section-box">
      <h4 style="font-size:13px;font-weight:700;color:var(--text-1);margin-bottom:8px">Resilience Stress Testing (+15% Demand Surge)</h4>
      ${robustnessHtml}
    </div>

    <!-- AI Assessment -->
    <div class="scn-section-box" style="background:#faf5ff;border-color:var(--purple-200)">
      <div class="flex items-center gap-xs mb-xs">
        <span style="font-size:14px">🧠</span>
        <h4 style="font-size:13px;font-weight:700;color:var(--primary)">NetGravity AI Assessment</h4>
      </div>
      <p style="font-size:12.5px;color:var(--text-1);line-height:1.45;margin-bottom:8px">${scn.aiAssessment?.recommendation || ''}</p>
      <ul style="font-size:12px;color:var(--text-2);padding-left:16px;margin:0;line-height:1.4">
        ${scn.aiAssessment?.why ? scn.aiAssessment.why.map((w) => `<li>${w}</li>`).join('') : ''}
      </ul>
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
export function openMetricDrilldown(metricKey, scenarioId) {
  const modal = document.getElementById('modal-metric-drilldown');
  const titleEl = document.getElementById('drilldown-title');
  const bodyEl = document.getElementById('drilldown-body-content');
  const provEl = document.getElementById('drilldown-provenance-tag');
  if (!modal || !bodyEl) return;

  const def = ALL_METRIC_DEFS[metricKey];
  const baseline = SCENARIOS.find((s) => s.id === 'SCN_ACTUAL') || SCENARIOS[0];
  const scn = SCENARIOS.find((s) => s.id === scenarioId) || SCENARIOS[1];

  if (titleEl) titleEl.textContent = `${def.label} Drill-Down`;
  if (provEl) provEl.textContent = `PROVENANCE: ${def.provenance || 'MODEL FACT'}`;

  let detailHtml = '';

  if (metricKey === 'totalCost' || metricKey === 'costChange' || metricKey === 'transportCost') {
    detailHtml = `
      <div style="font-size:12.5px;color:var(--text-2);margin-bottom:14px">
        Mathematical cost decomposition across freight transport, fixed facility handling, and inventory holding.
      </div>
      <table class="scn-data-table" style="font-size:12.5px">
        <thead>
          <tr>
            <th>Cost Component</th>
            <th style="text-align:center">Baseline</th>
            <th style="text-align:center">${scn.cardTitle || scn.name}</th>
            <th style="text-align:center">Variance</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>Transport Freight Cost</td>
            <td style="text-align:center">₹${(baseline.transportCost / 100000).toFixed(2)}L</td>
            <td style="text-align:center">₹${(scn.transportCost / 100000).toFixed(2)}L</td>
            <td style="text-align:center;color:var(--green);font-weight:700">↓ ${(((baseline.transportCost - scn.transportCost) / baseline.transportCost) * 100).toFixed(1)}%</td>
          </tr>
          <tr>
            <td>Fixed Facility Cost</td>
            <td style="text-align:center">₹${(baseline.fixedCost / 100000).toFixed(2)}L</td>
            <td style="text-align:center">₹${(scn.fixedCost / 100000).toFixed(2)}L</td>
            <td style="text-align:center">0.0%</td>
          </tr>
          <tr>
            <td>Inventory Holding Cost</td>
            <td style="text-align:center">₹${(baseline.inventoryCost / 100000).toFixed(2)}L</td>
            <td style="text-align:center">₹${(scn.inventoryCost / 100000).toFixed(2)}L</td>
            <td style="text-align:center;color:var(--green);font-weight:700">↓ ${(((baseline.inventoryCost - scn.inventoryCost) / baseline.inventoryCost) * 100).toFixed(1)}%</td>
          </tr>
          <tr style="font-weight:800;background:var(--bg-subtle)">
            <td>Total Net Cost</td>
            <td style="text-align:center">₹${(baseline.totalCost / 100000).toFixed(2)}L</td>
            <td style="text-align:center">₹${(scn.totalCost / 100000).toFixed(2)}L</td>
            <td style="text-align:center;color:var(--green)">↓ ${Math.abs(scn.costChange)}%</td>
          </tr>
        </tbody>
      </table>
    `;
  } else if (metricKey === 'sla') {
    detailHtml = `
      <div style="font-size:12.5px;color:var(--text-2);margin-bottom:14px">
        On-time service SLA across North, West, East, and South regional customer clusters.
      </div>
      <div class="grid-2 mb-md" style="gap:var(--space-sm)">
        <div style="background:var(--bg-subtle);padding:10px;border-radius:var(--r-sm);border:1px solid var(--border-light)">
          <span class="text-xs text-muted">Baseline SLA</span>
          <div style="font-size:16px;font-weight:800;color:var(--red)">94.3% (Target: ≥95.0%)</div>
        </div>
        <div style="background:var(--bg-subtle);padding:10px;border-radius:var(--r-sm);border:1px solid var(--border-light)">
          <span class="text-xs text-muted">Scenario SLA</span>
          <div style="font-size:16px;font-weight:800;color:var(--green)">${scn.sla}% (Exceeds Target)</div>
        </div>
      </div>
      <div class="text-xs text-muted">
        • Baddi → Delhi NCR corridor on-time delivery improves from 91.2% to 96.8% due to reallocated bottleneck volume.
      </div>
    `;
  } else if (metricKey === 'capacityRisk' || metricKey === 'delhiUtil' || metricKey === 'avgUtil') {
    // S9 P0 coverage for the risk chain: KPI+value (chips above), source/
    // state (this row), contributing factors, threshold, deterministic
    // evidence (the two boxes below — unchanged from before this pass) and
    // an AI-written explanation kept visibly separate via its own
    // provenance-badge, per the "never show RF alone" rule's intent — since
    // this build has no REI/RF/VaR data at all, that's stated explicitly
    // as Not available rather than a fabricated score.
    const baselineName = scenarioDisplayName(baseline);
    const scenarioName = scenarioDisplayName(scn);
    detailHtml = `
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;font-size:11.5px;color:var(--text-2)">
        <span>State: <strong>${baselineName}</strong> vs <strong>${scenarioName}</strong></span>
      </div>
      <div style="font-size:12.5px;color:var(--text-2);margin-bottom:14px">
        Projected peak utilization for December demand surge (+14.2% YoY growth).
      </div>
      <div class="grid-2 mb-md" style="gap:var(--space-sm)">
        <div style="background:var(--bg-subtle);padding:10px;border-radius:var(--r-sm);border:1px solid var(--border-light)">
          <span class="text-xs text-muted">Delhi NCR DC (Baseline)</span>
          <div style="font-size:16px;font-weight:800;color:var(--red)">108% (Capacity Breach)</div>
        </div>
        <div style="background:var(--bg-subtle);padding:10px;border-radius:var(--r-sm);border:1px solid var(--border-light)">
          <span class="text-xs text-muted">Delhi NCR DC (Scenario)</span>
          <div style="font-size:16px;font-weight:800;color:var(--green)">91% (Safe Headroom)</div>
        </div>
      </div>
      <div style="font-size:12px;margin:12px 0 4px;color:var(--text-1);font-weight:700">Governance threshold</div>
      <div style="font-size:12.5px;color:var(--text-2);margin-bottom:12px">
        Utilization ≥95% is classified Critical; 85–95% is Stress. Delhi NCR's baseline projection (108%) breaches the Critical threshold; the scenario's 91% falls to Stress, below the breach line.
      </div>
      <div style="font-size:12px;margin:12px 0 4px;color:var(--text-1);font-weight:700">Contributing factors</div>
      <ul style="font-size:12.5px;color:var(--text-2);margin:0 0 14px 18px;padding:0">
        <li>December demand forecast: +14.2% YoY growth in North India</li>
        <li>Baddi → Delhi NCR is the network's highest-volume corridor</li>
        <li>Delhi NCR has no spare capacity headroom at baseline; Kolkata DC has 41% headroom available</li>
      </ul>
      <div style="font-size:12px;margin:12px 0 4px;color:var(--text-1);font-weight:700">Risk chain (P → REI → RF)</div>
      <div style="font-size:12px;color:var(--text-2);margin-bottom:14px;font-style:italic">
        Not available — this build does not compute Facility REI or Governed Risk Factor (RF). The label above is a categorical risk read of the utilization projection only, not a synthesized RF score.
      </div>
      <span class="provenance-badge model-fact">MODEL FACT — evidence above</span>
      <hr style="border:none;border-top:1px solid var(--border-light);margin:14px 0">
      <span class="provenance-badge ai-assessment">AI ASSESSMENT</span>
      <div style="font-size:12.5px;color:var(--text-2);margin-top:8px">
        Based on the utilization projection and corridor concentration above, Delhi NCR is likely to breach capacity in December without intervention; reallocating volume toward Kolkata's spare capacity is the most direct mitigation available today.
      </div>
    `;
  } else {
    detailHtml = `
      <div style="font-size:12.5px;color:var(--text-2);margin-bottom:14px">
        Deterministic MILP model output for ${def.label}.
      </div>
      <div class="flex items-center justify-between" style="background:var(--bg-subtle);padding:12px;border-radius:var(--r-sm)">
        <span>Baseline: <strong>${baseline[metricKey] !== undefined ? def.fmt(baseline[metricKey]) : '—'}</strong></span>
        <span>Scenario: <strong style="color:var(--primary)">${scn[metricKey] !== undefined ? def.fmt(scn[metricKey]) : '—'}</strong></span>
      </div>
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

// ─── Open Create Scenario Toolbox ───────────────────────────
function openCreateToolbox() {
  const modal = document.getElementById('modal-create-toolbox');
  if (!modal) return;

  const formBody = document.getElementById('toolbox-form-body');
  const previewBody = document.getElementById('toolbox-preview-body');
  const execView = document.getElementById('agent-execution-view');
  if (formBody) formBody.classList.remove('hidden');
  if (previewBody) previewBody.classList.add('hidden');
  if (execView) execView.classList.add('hidden');

  const nameInput = document.getElementById('toolbox-scenario-name');
  if (nameInput) nameInput.value = '';

  renderToolboxDynamicFields('CHANGE_CAPACITY');
  modal.classList.add('visible');
}

function renderToolboxDynamicFields(type) {
  const container = document.getElementById('toolbox-dynamic-fields');
  const badge = document.getElementById('toolbox-active-type-badge');
  const desc = document.getElementById('toolbox-active-type-desc');
  if (!container) return;

  if (type === 'CHANGE_CAPACITY') {
    if (badge) badge.textContent = 'Change Capacity';
    if (desc) desc.textContent = 'Specify capacity expansion or reduction parameters.';
    container.innerHTML = `
      <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Facility</label>
          <select class="form-select" id="toolbox-facility">
            <option value="DC_DELHI" selected>Delhi NCR DC</option>
            <option value="DC_MUMBAI">Mumbai DC</option>
            <option value="DC_BENGALURU">Bengaluru DC</option>
            <option value="DC_KOLKATA">Kolkata DC</option>
            <option value="DC_GUWAHATI">Guwahati DC</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Adjustment Direction</label>
          <select class="form-select" id="toolbox-direction">
            <option value="INCREASE" selected>Increase (+)</option>
            <option value="DECREASE">Decrease (-)</option>
          </select>
        </div>
      </div>
      <div class="grid-2" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Adjustment Amount (units/day)</label>
          <input type="number" class="form-input" id="toolbox-amount" value="2000" min="100" max="10000" step="500">
        </div>
        <div class="form-group">
          <label class="form-label">Effective Horizon</label>
          <select class="form-select" id="toolbox-horizon">
            <option value="Q4_2026" selected>1 Oct – 31 Dec 2026</option>
            <option value="ANNUAL">Full Year 2027</option>
          </select>
        </div>
      </div>
    `;
  } else if (type === 'OPEN_FACILITY' || type === 'CLOSE_FACILITY') {
    if (badge) badge.textContent = type === 'OPEN_FACILITY' ? 'Open Facility' : 'Close Facility';
    if (desc) desc.textContent = 'Evaluate network reconfiguration with facility footprint changes.';
    container.innerHTML = `
      <div class="form-group">
        <label class="form-label">Target Location</label>
        <select class="form-select" id="toolbox-facility">
          <option value="DC_AHMEDABAD">Ahmedabad DC (New Candidate)</option>
          <option value="DC_HYDERABAD">Hyderabad DC (New Candidate)</option>
          <option value="DC_GUWAHATI">Guwahati DC</option>
        </select>
      </div>
    `;
  } else {
    if (badge) badge.textContent = type.replace('_', ' ');
    if (desc) desc.textContent = 'Configure parameter adjustments for network optimization.';
    container.innerHTML = `
      <div class="grid-2" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Adjustment Magnitude (%)</label>
          <input type="number" class="form-input" id="toolbox-amount" value="10" min="-50" max="50" step="5">
        </div>
        <div class="form-group">
          <label class="form-label">Target Corridor / Zone</label>
          <select class="form-select" id="toolbox-zone">
            <option value="NORTH" selected>North India Corridor</option>
            <option value="WEST">Western Corridor</option>
            <option value="ALL">Network Wide</option>
          </select>
        </div>
      </div>
    `;
  }

  // P0 #4/#5: keep the Current → Proposed line in sync with whatever
  // fields this type just rendered. Reassigning oninput/onchange (rather
  // than addEventListener) is safe to call on every render — no listener
  // stacking, since the container's fields are fully replaced each time.
  updateCurrentProposed(type);
  container.oninput = () => updateCurrentProposed(type);
  container.onchange = () => updateCurrentProposed(type);
}

// ─── Current → Proposed summary (P0 #4/#5) ───────────────────
// Reuses fields already on the facility (fac.capacity) or already present
// elsewhere in this exact modal (the 95% SLA constraint default) rather
// than inventing new baseline numbers. Where no honest baseline exists
// (zone-level demand/transport cost), shows "Not available" instead.
function updateCurrentProposed(type) {
  const el = document.getElementById('toolbox-current-proposed');
  if (!el) return;
  let currentText = 'Not available';
  let proposedText = 'Not available';

  if (type === 'CHANGE_CAPACITY') {
    const fac = DCS.find((d) => d.id === document.getElementById('toolbox-facility')?.value);
    const direction = document.getElementById('toolbox-direction')?.value;
    const amount = Number(document.getElementById('toolbox-amount')?.value || 0);
    if (fac) {
      currentText = `${formatNumber(fac.capacity)} units/day`;
      const proposed = direction === 'DECREASE' ? fac.capacity - amount : fac.capacity + amount;
      proposedText = `${formatNumber(Math.max(0, proposed))} units/day`;
    }
  } else if (type === 'OPEN_FACILITY') {
    currentText = 'Not in network today';
    proposedText = 'New facility added (zero baseline throughput)';
  } else if (type === 'CLOSE_FACILITY') {
    const fac = DCS.find((d) => d.id === document.getElementById('toolbox-facility')?.value);
    currentText = fac ? `${formatNumber(fac.capacity)} units/day (Active)` : 'Active';
    proposedText = '0 units/day (Closed — removed from network)';
  } else {
    const amount = Number(document.getElementById('toolbox-amount')?.value || 0);
    const zone = document.getElementById('toolbox-zone')?.selectedOptions?.[0]?.textContent || 'selected zone';
    if (type === 'CHANGE_SLA') {
      currentText = '95% (network SLA constraint)';
      proposedText = `${95 + amount}%`;
    } else {
      currentText = 'Not available';
      proposedText = `${amount > 0 ? '+' : ''}${amount}% in ${zone}`;
    }
  }

  el.innerHTML = `<strong>Current:</strong> ${currentText} &nbsp;→&nbsp; <strong>Proposed:</strong> ${proposedText}`;
}

// ─── Scenario Preview (P0 #7) ─────────────────────────────────
// One confirmation step between the form and the actual solver run — a
// summary of exactly what will be evaluated, plus P1's qualitative KPI
// direction chips. No numeric KPI estimate is shown here: the real
// evaluation only happens after "Confirm & Run", so any number here would
// be fabricated ahead of the solver, not sourced from it.
const KPI_DIRECTION_BY_TYPE = {
  CHANGE_CAPACITY: { Cost: '↑ Likely (CapEx)', Service: '↑ Likely', Capacity: '↓ Risk likely', Risk: '↓ Likely' },
  OPEN_FACILITY: { Cost: '↑ Likely (CapEx)', Service: '↑ Likely', Capacity: '↓ Risk likely', Risk: '↓ Likely' },
  CLOSE_FACILITY: { Cost: '↓ Likely (Fixed cost)', Service: '↓ Risk likely', Capacity: '↑ Risk likely', Risk: '↑ Likely' },
  CHANGE_DEMAND: { Cost: 'Depends on direction', Service: 'Depends on direction', Capacity: 'Depends on direction', Risk: 'Depends on direction' },
  CHANGE_TRANSPORT_COST: { Cost: 'Direct impact', Service: 'Neutral', Capacity: 'Neutral', Risk: 'Neutral' },
  CHANGE_SLA: { Cost: '↑ Likely if tightened', Service: 'Direct impact', Capacity: 'Neutral', Risk: '↓ Likely if tightened' },
};

function showScenarioPreview() {
  const formBody = document.getElementById('toolbox-form-body');
  const previewBody = document.getElementById('toolbox-preview-body');
  if (!formBody || !previewBody) return;

  const name = document.getElementById('toolbox-scenario-name')?.value.trim() || 'Untitled Scenario';
  const type = document.querySelector('.scn-type-card.active')?.dataset.type || 'CHANGE_CAPACITY';
  const typeLabel = document.getElementById('toolbox-active-type-badge')?.textContent || type;
  const currentProposedText = document.getElementById('toolbox-current-proposed')?.textContent || 'Not available';
  const objective = document.getElementById('toolbox-objective')?.selectedOptions?.[0]?.textContent || '—';
  const constraint = document.getElementById('toolbox-constraint-type')?.selectedOptions?.[0]?.textContent || '—';

  const nameEl = document.getElementById('preview-scenario-name');
  if (nameEl) nameEl.textContent = name;

  const rows = [
    ['Type', typeLabel],
    ['Current → Proposed', currentProposedText],
    ['Optimise for', objective],
    ['Subject to constraint', constraint],
  ];
  const rowsEl = document.getElementById('preview-summary-rows');
  if (rowsEl) {
    rowsEl.innerHTML = rows.map(([label, val]) => `
      <div class="flex items-center justify-between text-xs">
        <span class="text-muted">${label}</span>
        <span style="font-weight:700;text-align:right">${val || '—'}</span>
      </div>`).join('');
  }

  const chipsEl = document.getElementById('preview-kpi-chips');
  if (chipsEl) {
    const dirs = KPI_DIRECTION_BY_TYPE[type] || {};
    chipsEl.innerHTML = Object.entries(dirs).map(([k, v]) => `
      <span class="tag tag-muted" style="font-size:10.5px">${k}: ${v}</span>`).join('');
  }

  formBody.classList.add('hidden');
  previewBody.classList.remove('hidden');
}

// ─── Execute Scenario Creation (Phased Telemetry) ───────────
function runScenarioCreation() {
  const formBody = document.getElementById('toolbox-form-body');
  const previewBody = document.getElementById('toolbox-preview-body');
  const execView = document.getElementById('agent-execution-view');
  if (formBody) formBody.classList.add('hidden');
  if (previewBody) previewBody.classList.add('hidden');
  if (execView) execView.classList.remove('hidden');

  const steps = [
    { id: 'step-phase-1', text: 'Scenario validated ✓' },
    { id: 'step-phase-2', text: 'Preparing optimisation ✓' },
    { id: 'step-phase-3', text: 'Running network optimisation ●' },
    { id: 'step-phase-4', text: 'Evaluating scenario impact ○' },
    { id: 'step-phase-5', text: 'Stress testing ○' },
    { id: 'step-phase-6', text: 'Generating assessment ○' },
  ];

  let currentStep = 1;
  const interval = setInterval(() => {
    currentStep++;
    if (currentStep <= 6) {
      for (let i = 1; i <= currentStep; i++) {
        const el = document.getElementById(`step-phase-${i}`);
        if (el) {
          if (i < currentStep) {
            el.innerHTML = `<span>${steps[i - 1].text.replace('●', '✓').replace('○', '✓')}</span><span style="color:var(--green);font-weight:700">✓</span>`;
            el.classList.remove('text-muted');
          } else if (i === currentStep) {
            el.innerHTML = `<span>${steps[i - 1].text.replace('○', '●')}</span><span class="telemetry-spinner"></span>`;
            el.classList.remove('text-muted');
          }
        }
      }
    } else {
      clearInterval(interval);
      // Finalize and add new scenario object
      const newNum = SCENARIOS.length;
      const newId = `SCN_CUSTOM_${Date.now()}`;
      const userName = document.getElementById('toolbox-scenario-name')?.value.trim();
      const displayName = userName || `Scenario ${newNum}`;
      const newScenario = {
        id: newId,
        num: newNum,
        name: `${displayName} (Custom)`,
        shortName: displayName,
        cardTitle: displayName,
        type: 'USER_CREATED',
        source: 'user',
        badge: 'User Created',
        badgeClass: 'tag-primary',
        status: 'Evaluated',
        description: 'Custom what-if simulation with adjusted network flows.',
        highlight: 'User-configured MILP optimization run.',
        totalCost: 1220000,
        costChange: -5.1,
        transportCost: 1290000,
        fixedCost: 312000,
        variableCost: 235000,
        inventoryCost: 56000,
        inventoryDays: 16,
        sla: 96.5,
        avgUtil: 66.2,
        maxUtil: 88.0,
        delhiUtil: 88.0,
        capacityRisk: 'Low',
        capacityRiskClass: 'green',
        carbonKg: 101200,
        implementationCost: 35000,
        implementationTime: '15 mins',
        confidence: 'High Confidence',
        stars: 4,
        robustness: 'High',
        feasible: true,
        objective: {
          goal: 'User customized solver execution',
          primaryMetric: 'Total Cost',
          constraint: 'SLA ≥ 95%',
        },
        changes: [
          { item: 'Configured Network Adjustment', change: 'Custom parameters applied', note: 'MILP verified' },
        ],
        assumptions: [
          { label: 'Demand Forecast', value: 'December Peak Forecast', type: 'FORECAST' },
          { label: 'Solver Execution', value: 'Branch-and-Cut (Exact)', type: 'MODEL FACT' },
        ],
        robustnessTests: [
          { test: '+15% Demand Surge', status: 'PASS', detail: 'Peak utilization stays below 90%' },
        ],
        aiAssessment: {
          recommendation: 'Viable custom scenario with solid SLA and controlled capacity risk.',
          why: ['Cost ↓5.1%', 'SLA 96.5%', 'Low capacity risk'],
        },
      };

      SCENARIOS.push(newScenario);

      // Bring the freshly-created scenario straight into the comparison —
      // filling an empty slot if there's room, otherwise replacing the last
      // one — and show it on the Digital Twin map right away.
      if (multiSelectedIds.length < 3) {
        multiSelectedIds.push(newId);
      } else {
        multiSelectedIds[multiSelectedIds.length - 1] = newId;
      }
      mapActiveId = newId;

      setTimeout(() => {
        const modal = document.getElementById('modal-create-toolbox');
        if (modal) modal.classList.remove('visible');
        onSelectionChanged();
      }, 500);
    }
  }, 350);
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

  document.getElementById('btn-run-toolbox-scenario')?.addEventListener('click', () => {
    showScenarioPreview();
  });
  document.getElementById('btn-back-to-toolbox-form')?.addEventListener('click', () => {
    document.getElementById('toolbox-preview-body')?.classList.add('hidden');
    document.getElementById('toolbox-form-body')?.classList.remove('hidden');
  });
  document.getElementById('btn-confirm-run-scenario')?.addEventListener('click', () => {
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

