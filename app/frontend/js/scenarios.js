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

import {
  SCENARIOS,
  formatCurrency,
  formatNumber,
  SCENARIO_COMPARISON_INSIGHTS,
  SCENARIO_COMPARISON_ACTIONS,
} from './data.js';
import { initMap, renderScenarioDigitalTwin, invalidateMapSize } from './map.js';

// ─── State ──────────────────────────────────────────────────
let activeView = 'my-scenarios'; // 'my-scenarios' | 'comparison'
let selectedScenarioId = 'SCN_REBALANCE';
let mapMode = 'scenario'; // 'baseline' | 'scenario'
let activeMetricView = 'key';

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

const METRIC_VIEWS = {
  key: ['totalCost', 'costChange', 'sla', 'capacityRisk', 'avgUtil'],
  financial: ['totalCost', 'costChange', 'transportCost', 'fixedCost', 'inventoryCost', 'inventoryDays'],
  operations: ['sla', 'capacityRisk', 'avgUtil', 'delhiUtil', 'implementationTime'],
  all: ['totalCost', 'costChange', 'sla', 'capacityRisk', 'avgUtil', 'transportCost', 'inventoryDays', 'carbonKg', 'implementationTime', 'fixedCost', 'inventoryCost', 'delhiUtil'],
};

let activeSingleMetricKeys = ['totalCost', 'costChange', 'sla', 'capacityRisk', 'avgUtil'];
let activeMultiMetricKeys = ['totalCost', 'costChange', 'sla', 'capacityRisk', 'avgUtil', 'transportCost', 'inventoryDays', 'carbonKg', 'implementationTime'];

// ─── Init ───────────────────────────────────────────────────
export function initScenarios() {
  renderScenarioStrip();
  renderSingleScenarioTable();
  renderSingleScenarioInsights();
  renderSingleScenarioActions();
  renderMultiScenarioTable();
  renderComparisonInsights();
  renderMultiScenarioActions();
  wireScenarioEvents();

  // Initialize visual context 2D digital twin map
  setTimeout(() => {
    initMap('scenario-leaflet-map', {
      zoom: 4.2,
      center: [22.5, 79.5],
      isCompact: true,
      initialScenario: selectedScenarioId,
      mode: mapMode,
    });
    renderScenarioDigitalTwin('scenario-leaflet-map', selectedScenarioId, mapMode);
    invalidateMapSize('scenario-leaflet-map');
  }, 60);
}

// ─── Switch Sub-Nav View ────────────────────────────────────
export function switchScenarioView(viewName) {
  activeView = viewName;

  const btnMy = document.getElementById('tab-btn-my-scenarios');
  const btnComp = document.getElementById('tab-btn-comparison');
  const paneMy = document.getElementById('view-my-scenarios');
  const paneComp = document.getElementById('view-comparison');

  if (viewName === 'my-scenarios') {
    if (btnMy) btnMy.classList.add('active');
    if (btnComp) btnComp.classList.remove('active');
    if (paneMy) paneMy.classList.remove('hidden');
    if (paneComp) paneComp.classList.add('hidden');
    invalidateMapSize('scenario-leaflet-map');
    renderScenarioDigitalTwin('scenario-leaflet-map', selectedScenarioId, mapMode);
  } else {
    if (btnMy) btnMy.classList.remove('active');
    if (btnComp) btnComp.classList.add('active');
    if (paneMy) paneMy.classList.add('hidden');
    if (paneComp) paneComp.classList.remove('hidden');
    renderMultiScenarioTable();
  }
}

// ─── Render Scenario Selection Strip ────────────────────────
function renderScenarioStrip() {
  const container = document.getElementById('scn-strip-container');
  if (!container) return;

  const visibleScenarios = SCENARIOS.filter((s) => s.id !== 'SCN_ACTUAL');
  const stripScenarios = visibleScenarios.slice(0, 4);

  const cardsHtml = stripScenarios
    .map((s) => {
      const isSelected = s.id === selectedScenarioId;
      const isRec = s.type === 'RECOMMENDED' || s.id === 'SCN_REBALANCE';
      const cardTitle = s.cardTitle || s.name;

      // Extract specification / what changed snippet
      let specSnippet = s.highlight || '';
      if (!specSnippet && s.changes && s.changes.length > 0) {
        specSnippet = `${s.changes[0].item}: ${s.changes[0].change}`;
      }
      if (!specSnippet) {
        specSnippet = s.description || 'Optimized network parameters.';
      }

      const costText = s.costChange !== undefined ? `${s.costChange < 0 ? '↓ ' : '↑ '}${Math.abs(s.costChange)}%` : '—';
      const riskClass = s.capacityRisk === 'Low' || s.capacityRisk === 'Very Low' ? 'risk-low' : 'risk-high';

      return `
        <div class="scn-strip-card ${isSelected ? 'selected' : ''}" data-scn-id="${s.id}">
          <div class="scn-strip-top">
            <div class="scn-strip-left">
              <div class="scn-strip-checkbox">
                ${isSelected ? '✓' : ''}
              </div>
              <div class="scn-strip-name">${cardTitle}</div>
            </div>
            ${!isRec ? `<span class="scn-strip-close" data-del-id="${s.id}" title="Remove scenario">✕</span>` : '<span class="tag tag-success" style="font-size:9.5px;padding:1px 6px">Rec.</span>'}
          </div>

          <div class="scn-strip-spec">${specSnippet}</div>

          <div class="scn-strip-metrics-row">
            <span class="scn-strip-metric-pill cost">${costText} Cost</span>
            <span class="scn-strip-metric-pill sla">${s.sla}% SLA</span>
            <span class="scn-strip-metric-pill ${riskClass}">${s.capacityRisk}</span>
          </div>

          <div class="flex items-center justify-between" style="border-top:1px solid var(--border-light);padding-top:6px">
            <a href="javascript:void(0)" class="scn-strip-inspect-link" data-inspect-id="${s.id}">Click to Inspect →</a>
          </div>
        </div>
      `;
    })
    .join('');

  container.innerHTML = `
    ${cardsHtml}
    <div class="scn-strip-all-card" id="btn-view-all-scenarios" title="Open All Scenarios Workspace">
      <div style="font-size:20px;font-weight:800;color:var(--primary);line-height:1">➔</div>
      <div style="font-weight:700;font-size:12.5px;color:var(--text-1);margin-top:2px">All Scenarios</div>
      <div class="text-xs text-muted">View all (${visibleScenarios.length})</div>
    </div>
  `;

  // Wire card events (entire card is clickable to select and open details)
  container.querySelectorAll('.scn-strip-card').forEach((card) => {
    card.addEventListener('click', (e) => {
      if (e.target.classList.contains('scn-strip-close')) {
        e.stopPropagation();
        const delId = e.target.dataset.delId;
        removeScenario(delId);
        return;
      }
      const scnId = card.dataset.scnId;
      selectScenario(scnId);
      openScenarioDrawer(scnId);
    });
  });

  document.getElementById('btn-view-all-scenarios')?.addEventListener('click', () => {
    openAllScenariosDrawer();
  });
}

// ─── Open All Scenarios Drawer (Right Slide-over Window) ─────
export function openAllScenariosDrawer() {
  const overlay = document.getElementById('all-scenarios-drawer-overlay');
  const listEl = document.getElementById('all-scenarios-drawer-list');
  const countBadge = document.getElementById('all-scenarios-count-badge');
  if (!overlay || !listEl) return;

  const visibleScenarios = SCENARIOS.filter((s) => s.id !== 'SCN_ACTUAL');
  if (countBadge) countBadge.textContent = `${visibleScenarios.length} Active`;

  listEl.innerHTML = visibleScenarios
    .map((s) => {
      const isSelected = s.id === selectedScenarioId;
      const isRec = s.type === 'RECOMMENDED' || s.id === 'SCN_REBALANCE';
      const badgeCls = isRec ? 'tag-success' : 'tag-primary';
      const badgeText = isRec ? 'Recommended' : (s.type === 'AI_RECOMMENDED' ? 'AI Recommended' : 'User Created');

      let specSnippet = s.highlight || '';
      if (!specSnippet && s.changes && s.changes.length > 0) {
        specSnippet = `${s.changes[0].item}: ${s.changes[0].change}`;
      }
      if (!specSnippet) specSnippet = s.description || '';

      const costText = s.costChange !== undefined ? `${s.costChange < 0 ? '↓ ' : '↑ '}${Math.abs(s.costChange)}%` : '—';
      const riskColor = s.capacityRisk === 'Low' || s.capacityRisk === 'Very Low' ? 'var(--green)' : 'var(--red)';

      return `
        <div class="scn-section-box" style="padding:14px;border-left:3px solid ${isSelected ? 'var(--primary)' : 'var(--border)'};background:${isSelected ? '#fcf9ff' : 'var(--bg-card)'}">
          <div class="flex items-center justify-between mb-xs">
            <div class="flex items-center gap-xs">
              <span class="tag ${badgeCls}" style="font-size:10px;padding:2px 7px">${badgeText}</span>
              <strong style="font-size:14px;color:var(--text-1)">${s.cardTitle || s.name}</strong>
            </div>
            ${!isRec ? `<button class="btn btn-ghost btn-sm text-danger" data-drawer-del="${s.id}" title="Delete scenario" style="padding:2px 6px;font-size:12px">🗑️</button>` : ''}
          </div>

          <p style="font-size:12px;color:var(--text-2);line-height:1.45;margin-bottom:10px">${specSnippet}</p>

          <div class="grid-3 mb-sm" style="gap:6px;background:var(--bg-subtle);padding:8px 10px;border-radius:var(--r-sm);font-size:11.5px">
            <div><span class="text-muted">Total Cost:</span> <strong>₹${(s.totalCost / 100000).toFixed(2)}L</strong> <span style="color:var(--green)">(${costText})</span></div>
            <div><span class="text-muted">SLA:</span> <strong style="color:${s.sla >= 95 ? 'var(--green)' : 'var(--red)'}">${s.sla}%</strong></div>
            <div><span class="text-muted">Risk:</span> <strong style="color:${riskColor}">${s.capacityRisk}</strong></div>
          </div>

          <div class="flex items-center justify-between">
            <button class="btn btn-secondary btn-sm" data-drawer-select="${s.id}">
              ${isSelected ? '✓ Currently Selected' : 'Select Scenario'}
            </button>
            <a href="javascript:void(0)" class="scn-strip-inspect-link" data-drawer-inspect="${s.id}">Inspect Details →</a>
          </div>
        </div>
      `;
    })
    .join('');

  // Wire drawer item actions
  listEl.querySelectorAll('[data-drawer-select]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const scnId = btn.dataset.drawerSelect;
      selectScenario(scnId);
      openAllScenariosDrawer();
    });
  });

  listEl.querySelectorAll('[data-drawer-inspect]').forEach((link) => {
    link.addEventListener('click', () => {
      const scnId = link.dataset.drawerInspect;
      overlay.classList.remove('visible');
      selectScenario(scnId);
      openScenarioDrawer(scnId);
    });
  });

  listEl.querySelectorAll('[data-drawer-del]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const scnId = btn.dataset.drawerDel;
      removeScenario(scnId);
      openAllScenariosDrawer();
    });
  });

  overlay.classList.add('visible');
}

// ─── Select Scenario ────────────────────────────────────────
export function selectScenario(scenarioId) {
  selectedScenarioId = scenarioId;

  // Update strip highlight
  document.querySelectorAll('.scn-strip-card').forEach((card) => {
    const isThis = card.dataset.scnId === scenarioId;
    card.classList.toggle('selected', isThis);
    const cb = card.querySelector('.scn-strip-checkbox');
    if (cb) cb.innerHTML = isThis ? '✓' : '';
  });

  // Re-render single scenario table
  renderSingleScenarioTable();

  // Update map button text & visual context 2D digital twin
  const btnScenario = document.getElementById('btn-map-scenario');
  const scnObj = SCENARIOS.find((s) => s.id === scenarioId);
  if (btnScenario && scnObj) {
    btnScenario.textContent = scnObj.cardTitle || scnObj.name;
  }
  updateScenarioMapLayers();

  // Re-render right rail insights and actions
  renderSingleScenarioInsights();
  renderSingleScenarioActions();
}

// ─── Remove Scenario ────────────────────────────────────────
function removeScenario(scenarioId) {
  const idx = SCENARIOS.findIndex((s) => s.id === scenarioId);
  if (idx !== -1) {
    SCENARIOS.splice(idx, 1);
    if (selectedScenarioId === scenarioId) {
      selectedScenarioId = 'SCN_REBALANCE';
    }
    renderScenarioStrip();
    renderSingleScenarioTable();
    renderMultiScenarioTable();
  }
}

// ─── Render Single Scenario Comparison Table ────────────────
function renderSingleScenarioTable() {
  const container = document.getElementById('single-scenario-table-wrap');
  if (!container) return;

  const baseline = SCENARIOS.find((s) => s.id === 'SCN_ACTUAL') || SCENARIOS[0];
  const selected = SCENARIOS.find((s) => s.id === selectedScenarioId) || SCENARIOS[1];

  const rowsHtml = activeSingleMetricKeys
    .map((key) => {
      const def = ALL_METRIC_DEFS[key];
      if (!def) return '';

      const baseVal = baseline[key] !== undefined ? def.fmt(baseline[key]) : '—';
      const scnVal = selected[key] !== undefined ? def.fmt(selected[key]) : '—';
      const baseClass = def.cellClass ? def.cellClass(baseline[key]) : '';
      const scnClass = def.cellClass ? def.cellClass(selected[key]) : '';

      const provBadge = def.provenance
        ? `<span class="provenance-badge ${def.provenance.toLowerCase().replace(' ', '-')}">${def.provenance}</span>`
        : '';

      return `
        <tr class="scn-metric-row" data-metric-key="${key}">
          <td style="font-weight:600;display:flex;align-items:center;justify-content:space-between">
            <div style="display:flex;align-items:center">
              <span>${def.label}</span>
              ${provBadge}
            </div>
          </td>
          <td class="${baseClass}" style="text-align:center">${baseVal}</td>
          <td class="scn-td-rec ${scnClass}" style="text-align:center;font-weight:700">
            <span>${scnVal}</span>
            <span class="scn-info-btn" data-drill-metric="${key}" title="Click for mathematical drilldown">ⓘ</span>
          </td>
        </tr>
      `;
    })
    .join('');

  container.innerHTML = `
    <table class="scn-data-table">
      <thead>
        <tr>
          <th style="width:40%">Metrics</th>
          <th style="width:30%;text-align:center">Baseline</th>
          <th class="scn-th-rec" style="width:30%;text-align:center">${selected.cardTitle || selected.name}</th>
        </tr>
      </thead>
      <tbody>
        ${rowsHtml}
      </tbody>
    </table>
  `;

  // Wire metric drilldown clicks
  container.querySelectorAll('.scn-info-btn').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const metricKey = btn.dataset.drillMetric;
      openMetricDrilldown(metricKey, selectedScenarioId);
    });
  });

  container.querySelectorAll('.scn-metric-row').forEach((row) => {
    row.addEventListener('click', () => {
      const metricKey = row.dataset.metricKey;
      openMetricDrilldown(metricKey, selectedScenarioId);
    });
  });
}

// ─── Render Single Scenario Insights ────────────────────────
function renderSingleScenarioInsights() {
  const container = document.getElementById('scn-single-insights-list');
  if (!container) return;

  const scn = SCENARIOS.find((s) => s.id === selectedScenarioId) || SCENARIOS[1];

  let bullets = [
    'Utilisation projected to exceed 95%',
    'Cost reduction focused on 2 lanes',
  ];

  if (scn.id === 'SCN_USER_1') {
    bullets = [
      'Transport cost ↓7.7% on western lanes',
      'Delhi NCR remains at 94% utilisation',
    ];
  } else if (scn.id === 'SCN_USER_2') {
    bullets = [
      '10-min automated dispatch rollout',
      'Scope 3 Carbon reduced to 97,133 kg',
    ];
  } else if (scn.id === 'SCN_AI_REC_4') {
    bullets = [
      'Intermodal rail transport absorbs demand',
      'SLA maintained at 96.7% with Low risk',
    ];
  }

  container.innerHTML = bullets
    .map((b) => `<div class="scn-rail-bullet-item">• ${b}</div>`)
    .join('');

  container.querySelectorAll('.scn-rail-bullet-item').forEach((el) => {
    el.addEventListener('click', () => {
      openScenarioDrawer(selectedScenarioId);
    });
  });
}

// ─── Render Single Scenario Action Items ────────────────────
function renderSingleScenarioActions() {
  const container = document.getElementById('scn-single-actions-list');
  if (!container) return;

  const scn = SCENARIOS.find((s) => s.id === selectedScenarioId) || SCENARIOS[1];

  let items = [
    '1. Rebalance Baddi to Delhi NCR',
    '2. Review recommended network state',
  ];

  if (scn.id === 'SCN_USER_1') {
    items = [
      '1. Validate line-haul freight rates with carrier',
      '2. Stress test western corridor headroom',
    ];
  } else if (scn.id === 'SCN_USER_2') {
    items = [
      '1. Verify Kolkata DC cross-dock throughput',
      '2. Configure automated dispatch rules in TMS',
    ];
  } else if (scn.id === 'SCN_AI_REC_4') {
    items = [
      '1. Review rail freight schedules and SLA impact',
      '2. Approve intermodal corridor allocation',
    ];
  }

  container.innerHTML = items
    .map((item) => `<div class="scn-rail-action-row">${item}</div>`)
    .join('');

  container.querySelectorAll('.scn-rail-action-row').forEach((el) => {
    el.addEventListener('click', () => {
      openScenarioDrawer(selectedScenarioId);
    });
  });
}

// ─── Leaflet Visual Context (2D Digital Twin) ───────────────
function updateScenarioMapLayers() {
  renderScenarioDigitalTwin('scenario-leaflet-map', selectedScenarioId, mapMode);
  invalidateMapSize('scenario-leaflet-map');
}

// ─── Render Multi-Scenario Trade-off Analysis Table ─────────
function renderMultiScenarioTable() {
  const container = document.getElementById('multi-scenario-table-wrap');
  if (!container) return;

  const baseline = SCENARIOS.find((s) => s.id === 'SCN_ACTUAL') || SCENARIOS[0];
  const compareScenarios = SCENARIOS.filter((s) => s.id !== 'SCN_ACTUAL');

  const theadCols = compareScenarios
    .map((s) => {
      const isRec = s.type === 'RECOMMENDED' || s.id === 'SCN_REBALANCE';
      const thClass = isRec ? 'scn-th-rec' : '';
      const colTitle = s.shortName || s.name;
      return `<th class="${thClass}" style="text-align:center">${colTitle}</th>`;
    })
    .join('');

  const rowsHtml = activeMultiMetricKeys
    .map((key) => {
      const def = ALL_METRIC_DEFS[key];
      if (!def) return '';

      const baseVal = baseline[key] !== undefined ? def.fmt(baseline[key]) : '—';
      const baseClass = def.cellClass ? def.cellClass(baseline[key]) : '';

      const provBadge = def.provenance
        ? `<span class="provenance-badge ${def.provenance.toLowerCase().replace(' ', '-')}">${def.provenance}</span>`
        : '';

      const cellsHtml = compareScenarios
        .map((s) => {
          const isRec = s.type === 'RECOMMENDED' || s.id === 'SCN_REBALANCE';
          const val = s[key] !== undefined ? def.fmt(s[key]) : '—';
          const cellClass = def.cellClass ? def.cellClass(s[key]) : '';
          const recColClass = isRec ? 'scn-td-rec' : '';

          return `<td class="${recColClass} ${cellClass}" style="text-align:center">${val}</td>`;
        })
        .join('');

      return `
        <tr class="scn-multi-row" data-metric-key="${key}">
          <td style="font-weight:600;display:flex;align-items:center;justify-content:space-between">
            <div style="display:flex;align-items:center">
              <span>${def.label}</span>
              ${provBadge}
            </div>
          </td>
          <td class="${baseClass}" style="text-align:center">${baseVal}</td>
          ${cellsHtml}
          <td style="width:24px;text-align:center">
            <span class="scn-info-btn" data-drill-metric="${key}" title="Click for metric breakdown">ⓘ</span>
          </td>
        </tr>
      `;
    })
    .join('');

  container.innerHTML = `
    <table class="scn-multi-table">
      <thead>
        <tr>
          <th style="width:24%;text-align:left">Metrics</th>
          <th style="width:15%;text-align:center">Current Baseline</th>
          ${theadCols}
          <th style="width:24px"></th>
        </tr>
      </thead>
      <tbody>
        ${rowsHtml}
      </tbody>
    </table>
  `;

  // Wire metric drilldowns
  container.querySelectorAll('.scn-info-btn').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const metricKey = btn.dataset.drillMetric;
      openMetricDrilldown(metricKey, selectedScenarioId);
    });
  });

  container.querySelectorAll('.scn-multi-row').forEach((row) => {
    row.addEventListener('click', () => {
      const metricKey = row.dataset.metricKey;
      openMetricDrilldown(metricKey, selectedScenarioId);
    });
  });
}

// ─── Render Comparison Insights (View 2) ────────────────────
function renderComparisonInsights() {
  const container = document.getElementById('multi-comparison-insights-list');
  if (!container) return;

  container.innerHTML = SCENARIO_COMPARISON_INSIGHTS.map(
    (ins) => `
      <div class="scn-insight-item" data-insight-id="${ins.id}">
        (${ins.num}) ${ins.text}
      </div>
    `
  ).join('');

  container.querySelectorAll('.scn-insight-item').forEach((el) => {
    el.addEventListener('click', () => {
      const insId = el.dataset.insightId;
      const ins = SCENARIO_COMPARISON_INSIGHTS.find((item) => item.id === insId);
      if (ins) {
        openScenarioDrawer(ins.scenarioId);
      }
    });
  });
}

// ─── Render Multi-Scenario Action Items (View 2) ────────────
function renderMultiScenarioActions() {
  const container = document.getElementById('multi-comparison-actions-list');
  if (!container) return;

  container.innerHTML = SCENARIO_COMPARISON_ACTIONS.map(
    (act) => `
      <div class="scn-action-item-row" data-action-id="${act.id}">
        <input type="checkbox" style="margin-top:2px;cursor:pointer">
        <span>${act.title}</span>
      </div>
    `
  ).join('');

  container.querySelectorAll('.scn-action-item-row').forEach((el) => {
    el.addEventListener('click', (e) => {
      if (e.target.tagName === 'INPUT') return;
      const actId = el.dataset.actionId;
      openActionDetailDrawer(actId);
    });
  });
}

// ─── Open Action Detail Drawer ──────────────────────────────
export function openActionDetailDrawer(actionId) {
  const act = SCENARIO_COMPARISON_ACTIONS.find((a) => a.id === actionId);
  if (!act) return;

  const overlay = document.getElementById('action-drawer-overlay');
  const content = document.getElementById('action-drawer-content');
  if (!overlay || !content) return;

  const scn = SCENARIOS.find((s) => s.id === act.scenarioId) || SCENARIOS[1];

  content.innerHTML = `
    <div style="margin-bottom:16px">
      <span class="tag tag-primary" style="font-size:10px;padding:3px 8px">Action Item</span>
      <h3 style="font-size:18px;font-weight:800;color:var(--text-1);margin-top:6px">${act.title}</h3>
      <div class="text-xs text-muted">Related Scenario: <strong>${scn.cardTitle || scn.name}</strong></div>
    </div>

    <div class="scn-section-box">
      <h4 style="font-size:13px;font-weight:700;color:var(--text-1);margin-bottom:6px">Why this action exists</h4>
      <p style="font-size:12.5px;color:var(--text-2);line-height:1.45">${act.why}</p>
    </div>

    <div class="scn-section-box">
      <h4 style="font-size:13px;font-weight:700;color:var(--text-1);margin-bottom:6px">Mathematical Evidence</h4>
      <div style="font-size:12px;color:var(--text-2);background:#fff;padding:10px;border-radius:var(--r-sm);border:1px solid var(--border-light)">
        ${act.evidence}
      </div>
    </div>

    <div class="scn-section-box">
      <h4 style="font-size:13px;font-weight:700;color:var(--text-1);margin-bottom:6px">Suggested Next Step</h4>
      <p style="font-size:12.5px;color:var(--text-1);font-weight:600">${act.nextStep}</p>
    </div>

    <div class="flex gap-sm mt-lg">
      <button class="btn btn-primary" id="btn-action-goto-scenario">Inspect Scenario Details →</button>
      <button class="btn btn-secondary" id="btn-action-dismiss">Close</button>
    </div>
  `;

  overlay.classList.add('visible');

  document.getElementById('action-drawer-close')?.addEventListener('click', () => {
    overlay.classList.remove('visible');
  });
  document.getElementById('btn-action-dismiss')?.addEventListener('click', () => {
    overlay.classList.remove('visible');
  });
  document.getElementById('btn-action-goto-scenario')?.addEventListener('click', () => {
    overlay.classList.remove('visible');
    selectScenario(act.scenarioId);
    openScenarioDrawer(act.scenarioId);
  });
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
      <button class="btn btn-primary" id="btn-apply-scenario-drawer" style="flex:1">Proceed with this Scenario</button>
      <button class="btn btn-secondary" id="btn-close-scenario-drawer">Close</button>
    </div>
  `;

  overlay.classList.add('visible');

  document.getElementById('scenario-drawer-close')?.addEventListener('click', () => {
    overlay.classList.remove('visible');
  });
  document.getElementById('btn-close-scenario-drawer')?.addEventListener('click', () => {
    overlay.classList.remove('visible');
  });
  document.getElementById('btn-apply-scenario-drawer')?.addEventListener('click', () => {
    overlay.classList.remove('visible');
    selectScenario(scenarioId);
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
    detailHtml = `
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
  const execView = document.getElementById('agent-execution-view');
  if (formBody) formBody.classList.remove('hidden');
  if (execView) execView.classList.add('hidden');

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
}

// ─── Execute Scenario Creation (Phased Telemetry) ───────────
function runScenarioCreation() {
  const formBody = document.getElementById('toolbox-form-body');
  const execView = document.getElementById('agent-execution-view');
  if (formBody) formBody.classList.add('hidden');
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
      const newScenario = {
        id: newId,
        num: newNum,
        name: `Scenario ${newNum} (Custom)`,
        shortName: `User Created ${newNum}`,
        cardTitle: `Scenario ${newNum}`,
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

      setTimeout(() => {
        const modal = document.getElementById('modal-create-toolbox');
        if (modal) modal.classList.remove('visible');
        renderScenarioStrip();
        selectScenario(newId);
        renderMultiScenarioTable();
      }, 500);
    }
  }, 350);
}

// ─── Wire Scenario Events ───────────────────────────────────
function wireScenarioEvents() {
  // Sub-Navigation Tabs
  document.getElementById('tab-btn-my-scenarios')?.addEventListener('click', () => {
    switchScenarioView('my-scenarios');
  });
  document.getElementById('tab-btn-comparison')?.addEventListener('click', () => {
    switchScenarioView('comparison');
  });

  // Add Scenario Top Button
  document.getElementById('btn-add-scenario-top')?.addEventListener('click', () => {
    openCreateToolbox();
  });

  // Map View Toggle (Baseline vs Scenario)
  const btnMapBase = document.getElementById('btn-map-baseline');
  const btnMapScn = document.getElementById('btn-map-scenario');

  if (btnMapBase && btnMapScn) {
    btnMapBase.addEventListener('click', () => {
      mapMode = 'baseline';
      btnMapBase.classList.add('active');
      btnMapScn.classList.remove('active');
      updateScenarioMapLayers();
    });

    btnMapScn.addEventListener('click', () => {
      mapMode = 'scenario';
      btnMapScn.classList.add('active');
      btnMapBase.classList.remove('active');
      updateScenarioMapLayers();
    });
  }

  // Metric View Dropdowns
  document.getElementById('scn-metric-view-select')?.addEventListener('change', (e) => {
    const view = e.target.value;
    activeSingleMetricKeys = METRIC_VIEWS[view] || METRIC_VIEWS.key;
    renderSingleScenarioTable();
  });

  document.getElementById('scn-multi-metric-view-select')?.addEventListener('change', (e) => {
    const view = e.target.value;
    activeMultiMetricKeys = METRIC_VIEWS[view] || METRIC_VIEWS.all;
    renderMultiScenarioTable();
  });

  // Customize Metrics Modal
  const btnCustSingle = document.getElementById('btn-customize-metrics');
  const btnCustMulti = document.getElementById('btn-multi-customize-metrics');
  const modalCust = document.getElementById('modal-metric-customizer');

  const openCustModal = () => {
    if (modalCust) modalCust.classList.add('visible');
  };

  if (btnCustSingle) btnCustSingle.addEventListener('click', openCustModal);
  if (btnCustMulti) btnCustMulti.addEventListener('click', openCustModal);

  document.getElementById('modal-close-metrics')?.addEventListener('click', () => {
    if (modalCust) modalCust.classList.remove('visible');
  });
  document.getElementById('btn-cancel-metrics')?.addEventListener('click', () => {
    if (modalCust) modalCust.classList.remove('visible');
  });

  document.getElementById('btn-save-metrics')?.addEventListener('click', () => {
    const checked = [];
    document.querySelectorAll('#modal-metric-customizer input[type="checkbox"]:checked').forEach((cb) => {
      if (cb.dataset.metric) checked.push(cb.dataset.metric);
    });
    if (checked.length > 0) {
      activeSingleMetricKeys = checked;
      activeMultiMetricKeys = checked;
      renderSingleScenarioTable();
      renderMultiScenarioTable();
    }
    if (modalCust) modalCust.classList.remove('visible');
  });

  document.getElementById('btn-reset-metrics')?.addEventListener('click', () => {
    activeSingleMetricKeys = METRIC_VIEWS.key;
    activeMultiMetricKeys = METRIC_VIEWS.all;
    renderSingleScenarioTable();
    renderMultiScenarioTable();
    if (modalCust) modalCust.classList.remove('visible');
  });

  // Toolbox Modal events
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
    runScenarioCreation();
  });

  // Create Scenario Button
  document.getElementById('btn-create-scenario-main')?.addEventListener('click', () => {
    openCreateToolbox();
  });

  // All Scenarios Drawer Close & Clear Actions
  document.getElementById('all-scenarios-drawer-close')?.addEventListener('click', () => {
    document.getElementById('all-scenarios-drawer-overlay')?.classList.remove('visible');
  });
  document.getElementById('btn-close-all-scenarios')?.addEventListener('click', () => {
    document.getElementById('all-scenarios-drawer-overlay')?.classList.remove('visible');
  });

  document.getElementById('btn-clear-user-scenarios')?.addEventListener('click', () => {
    // Keep baseline, recommended and canonical scenarios, clear user-created custom scenarios
    const canonicalIds = ['SCN_ACTUAL', 'SCN_REBALANCE', 'SCN_AI_REC_4'];
    const removedCount = SCENARIOS.length - SCENARIOS.filter(s => canonicalIds.includes(s.id)).length;
    
    // Filter in-place
    for (let i = SCENARIOS.length - 1; i >= 0; i--) {
      if (!canonicalIds.includes(SCENARIOS[i].id)) {
        SCENARIOS.splice(i, 1);
      }
    }

    selectedScenarioId = 'SCN_REBALANCE';
    renderScenarioStrip();
    renderSingleScenarioTable();
    renderSingleScenarioInsights();
    renderSingleScenarioActions();
    renderMultiScenarioTable();
    renderComparisonInsights();
    renderMultiScenarioActions();
    openAllScenariosDrawer();
  });

  // Scenario Chatbot Events
  document.getElementById('scn-chat-send')?.addEventListener('click', handleScenarioChat);
  document.getElementById('scn-chat-input')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') handleScenarioChat();
  });
}

// ─── Scenario Planning Contextual Chatbot ──────────────────
function handleScenarioChat() {
  const input = document.getElementById('scn-chat-input');
  const messages = document.getElementById('scn-chat-messages');
  if (!input || !input.value.trim()) return;

  const query = input.value.trim();
  input.value = '';

  if (messages) {
    messages.style.display = 'block';
    messages.innerHTML += `<div class="home-chat-msg user">You: ${query}</div>`;

    const scn = SCENARIOS.find((s) => s.id === selectedScenarioId) || SCENARIOS[1];
    const scnName = scn.cardTitle || scn.name;
    const costText = scn.costChange < 0 ? `saves ${Math.abs(scn.costChange)}% (₹${((1285000 - scn.totalCost) / 100000).toFixed(1)}L/mo)` : `increases cost by ${scn.costChange}%`;

    let responseText = '';
    const qLower = query.toLowerCase();

    if (qLower.includes('cost') || qLower.includes('save') || qLower.includes('budget') || qLower.includes('roi')) {
      responseText = `<strong>${scnName}</strong> ${costText} with total operating cost at ₹${(scn.totalCost / 100000).toFixed(2)}L/month. Transport cost is ₹${(scn.transportCost / 100000).toFixed(2)}L.`;
    } else if (qLower.includes('delhi') || qLower.includes('bottleneck') || qLower.includes('capacity') || qLower.includes('util')) {
      responseText = `Under <strong>${scnName}</strong>, network average utilisation is <strong>${scn.avgUtil}%</strong> and Delhi NCR DC capacity risk is mitigated to <strong>${scn.capacityRisk}</strong> (relieving the 108% December peak forecast).`;
    } else if (qLower.includes('sla') || qLower.includes('service') || qLower.includes('delivery') || qLower.includes('time')) {
      responseText = `On-time delivery SLA under <strong>${scnName}</strong> reaches <strong>${scn.sla}%</strong> with an average lead time of 2.1 days, meeting enterprise SLA targets (>95%).`;
    } else if (qLower.includes('kolkata') || qLower.includes('rebalance') || qLower.includes('lane') || qLower.includes('flow')) {
      responseText = `Key network reallocation: Baddi → Delhi volume is reduced by 1,200 units/day, while Baddi → Kolkata DC absorbs +800 units/day and Pune → Mumbai direct handling absorbs +400 units/day.`;
    } else if (qLower.includes('action') || qLower.includes('implement') || qLower.includes('execute') || qLower.includes('next')) {
      responseText = `Recommended next steps: [1] Execute line-haul freight rebalancing on Baddi–Delhi lane; [2] Confirm cross-dock capacity reservations with regional 3PL partners.`;
    } else {
      responseText = `<strong>${scnName}</strong> delivers ${costText}, maintains ${scn.sla}% SLA, and ${scn.highlight || scn.description}. Would you like to inspect parameter assumptions or run stress tests?`;
    }

    setTimeout(() => {
      messages.innerHTML += `<div class="home-chat-msg bot">🤖 NetGravity: ${responseText}</div>`;
      messages.scrollTop = messages.scrollHeight;
    }, 200);
  }
}
