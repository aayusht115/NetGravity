/**
 * NetGravity — Scenario Planning Workspace
 * ========================================
 * Executive decision workspace for scenario exploration, MILP evaluation,
 * trade-off comparison, robustness stress-testing, and AI recommendation.
 */

import { SCENARIOS, formatCurrency, formatNumber } from './data.js';
import {
  renderScenarioCostImpactChart,
  renderScenarioCapacityRiskChart,
  renderScenarioSlaChart,
  renderScenarioFlowMap,
} from './charts.js';

// ─── State ──────────────────────────────────────────────────
let activeScnTab = 'recommended';
let selectedScenarioId = 'SCN_REBALANCE';
let activeMetricView = 'key';

// Metric definitions with formatters, groupings, and labels
const ALL_METRIC_DEFS = {
  totalCost: {
    label: 'Total Cost (₹)',
    fmt: (v) => `₹${(v / 100000).toFixed(2)}L`,
    provenance: 'MODEL FACT',
  },
  costChange: {
    label: 'Cost Change',
    fmt: (v) => (v === 0 ? '—' : `${v < 0 ? '↓ ' : '↑ '}${Math.abs(v)}%`),
    style: (v) => (v < 0 ? 'color:var(--green);font-weight:700' : ''),
  },
  sla: {
    label: 'SLA (On-time)',
    fmt: (v) => `${v}%`,
    style: (v) => (v >= 95 ? 'color:var(--green);font-weight:700' : 'color:var(--red);font-weight:700'),
    provenance: 'MODEL FACT',
  },
  capacityRisk: {
    label: 'Capacity Risk (Dec)',
    fmt: (v) => v,
    style: (v) => {
      if (v === 'High') return 'color:var(--red);font-weight:700';
      if (v === 'Medium') return 'color:var(--amber);font-weight:700';
      return 'color:var(--green);font-weight:700';
    },
    provenance: 'FORECAST',
  },
  delhiUtil: {
    label: 'Utilisation – Delhi NCR',
    fmt: (v) => `${v}%`,
    style: (v) => (v > 90 ? 'color:var(--amber);font-weight:700' : 'color:var(--text-1)'),
    provenance: 'MODEL FACT',
  },
  transportCost: {
    label: 'Transport Freight Cost',
    fmt: (v) => formatCurrency(v),
    provenance: 'MODEL FACT',
  },
  fixedCost: {
    label: 'Fixed Facility Cost',
    fmt: (v) => formatCurrency(v),
    provenance: 'MODEL FACT',
  },
  inventoryCost: {
    label: 'Inventory Holding Cost',
    fmt: (v) => formatCurrency(v),
    provenance: 'MODEL FACT',
  },
  avgUtil: {
    label: 'Network Avg Utilisation',
    fmt: (v) => `${v}%`,
    provenance: 'MODEL FACT',
  },
  carbonKg: {
    label: 'Scope 3 Carbon (kg CO₂)',
    fmt: (v) => formatNumber(v) + ' kg',
    provenance: 'MODEL FACT',
  },
  implementationTime: {
    label: 'Implementation Time',
    fmt: (v) => v,
    provenance: 'MODEL FACT',
  },
  confidence: {
    label: 'Robustness / Confidence',
    fmt: (s) => {
      if (s.stars === 5) return '★★★★★';
      if (s.stars === 3) return '★★★☆☆';
      return s.confidence || '—';
    },
    style: () => 'color:var(--primary);letter-spacing:1px',
    provenance: 'AI ASSESSMENT',
  },
};

let activeMetricKeys = ['totalCost', 'costChange', 'sla', 'capacityRisk', 'delhiUtil', 'implementationTime', 'confidence'];

// ─── Init ───────────────────────────────────────────────────
export function initScenarios() {
  renderScenarioCardsRow();
  renderComparisonTable();
  renderImpactCharts();
  wireScenarioEvents();
}

// ─── Render Recommended Scenario Cards ──────────────────────
function renderScenarioCardsRow() {
  const container = document.getElementById('scenario-cards-row');
  if (!container) return;

  // Filter candidates for top cards
  const candidateScenarios = SCENARIOS.filter(
    (s) => s.id === 'SCN_REBALANCE' || s.id === 'SCN_EXPAND_DELHI' || s.id === 'SCN_KOLKATA'
  );

  container.innerHTML = candidateScenarios
    .map((s) => {
      const isSelected = s.id === selectedScenarioId;
      const isRec = s.id === 'SCN_REBALANCE';
      const badgeCls = isRec ? 'tag-success' : 'tag-warning';
      const badgeText = isRec ? 'Recommended' : 'Viable';

      return `
        <div class="scn-rec-card ${isRec ? 'recommended' : ''} ${isSelected ? 'selected' : ''}" data-scn-id="${s.id}">
          <div>
            <div class="scn-rec-card-header">
              <div class="flex items-center gap-xs">
                <span class="scn-num-badge">${s.num}</span>
                <span class="tag ${badgeCls}" style="font-size:10px;padding:2px 8px">${badgeText}</span>
              </div>
              <span class="text-xs text-muted" style="font-weight:600">MILP Verified</span>
            </div>

            <div class="scn-card-body">
              <div class="scn-card-left">
                <div class="scn-card-title">${s.cardTitle || s.name}</div>
                <div class="scn-card-desc">${s.description}</div>
                <div class="scn-card-highlight">${s.highlight || ''}</div>
              </div>

              <div class="scn-card-right">
                <div class="scn-stat-unit">
                  <span class="scn-stat-label">Total Cost</span>
                  <span class="scn-stat-val positive">${s.costChange ? (s.costChange < 0 ? '↓ ' : '↑ ') + Math.abs(s.costChange) + '%' : '—'}</span>
                </div>
                <div class="scn-stat-unit">
                  <span class="scn-stat-label">SLA</span>
                  <span class="scn-stat-val">${s.sla}%</span>
                </div>
                <div class="scn-stat-unit">
                  <span class="scn-stat-label">Capacity Risk</span>
                  <span class="scn-stat-val" style="color:${s.capacityRisk === 'Low' || s.capacityRisk === 'Very Low' ? 'var(--green)' : 'var(--amber)'}">${s.capacityRisk}</span>
                </div>
              </div>
            </div>
          </div>

          <div class="scn-card-footer">
            <span class="scn-conf-badge ${isRec ? 'high' : 'medium'}">${s.confidence}</span>
            <span class="text-xs" style="color:var(--primary);font-weight:600">Inspect Details →</span>
          </div>
        </div>
      `;
    })
    .join('');

  // Wire card clicks to open detail drawer
  container.querySelectorAll('.scn-rec-card').forEach((card) => {
    card.addEventListener('click', () => {
      const id = card.dataset.scnId;
      selectedScenarioId = id;
      renderScenarioCardsRow();
      renderComparisonTable();
      openScenarioDrawer(id);
    });
  });
}

// ─── Render Comparison Table ────────────────────────────────
function renderComparisonTable() {
  const container = document.getElementById('scenario-comparison-table-wrap');
  if (!container) return;

  // Active scenarios to display in table columns
  let displayScenarios = SCENARIOS;
  if (activeScnTab === 'opt_base') {
    displayScenarios = SCENARIOS.filter((s) => s.id === 'SCN_ACTUAL' || s.id === 'SCN_OPTIMISED_BASE');
  } else if (activeScnTab === 'my_scenarios') {
    displayScenarios = SCENARIOS.filter((s) => s.id === 'SCN_ACTUAL' || s.source === 'user' || s.id === 'SCN_REBALANCE');
  }

  const html = `
    <table class="scenario-comparison-table">
      <thead>
        <tr>
          <th>Metrics</th>
          ${displayScenarios
            .map((s) => {
              const isRec = s.id === 'SCN_REBALANCE';
              const isSelected = s.id === selectedScenarioId;
              let colHeaderClass = isRec ? 'highlight-col' : '';
              return `
                <th class="${colHeaderClass}" style="cursor:pointer" data-scn-id="${s.id}" title="Click to view scenario details">
                  <div style="font-size:11px;color:var(--text-3);font-weight:500">${s.badge || s.type}</div>
                  <div style="font-size:13px;font-weight:700">${s.shortName || s.name}</div>
                  ${isRec ? '<span class="tag tag-success" style="font-size:9px;margin-top:3px">Recommended</span>' : ''}
                </th>
              `;
            })
            .join('')}
        </tr>
      </thead>
      <tbody>
        ${activeMetricKeys
          .map((key) => {
            const def = ALL_METRIC_DEFS[key];
            if (!def) return '';

            return `
              <tr>
                <td>
                  <div style="display:flex;align-items:center;gap:6px">
                    <span>${def.label}</span>
                    ${def.provenance ? `<span class="provenance-badge ${def.provenance.toLowerCase().replace(/\s+/g, '-')}">${def.provenance}</span>` : ''}
                  </div>
                </td>
                ${displayScenarios
                  .map((s) => {
                    const val = s[key];
                    const isRec = s.id === 'SCN_REBALANCE';
                    const customStyle = def.style ? def.style(val) : '';
                    let formattedVal = def.fmt ? (typeof def.fmt === 'function' ? (key === 'confidence' ? def.fmt(s) : def.fmt(val)) : val) : val;

                    return `
                      <td class="${isRec ? 'highlight-col' : ''}" style="${customStyle};cursor:pointer" data-scn-id="${s.id}">
                        ${formattedVal !== undefined && formattedVal !== null ? formattedVal : '—'}
                      </td>
                    `;
                  })
                  .join('')}
              </tr>
            `;
          })
          .join('')}
      </tbody>
    </table>
  `;

  container.innerHTML = html;

  // Wire column / cell clicks to open drawer
  container.querySelectorAll('[data-scn-id]').forEach((el) => {
    el.addEventListener('click', () => {
      const id = el.dataset.scnId;
      selectedScenarioId = id;
      renderScenarioCardsRow();
      renderComparisonTable();
      openScenarioDrawer(id);
    });
  });
}

// ─── Render Impact Charts ───────────────────────────────────
function renderImpactCharts() {
  renderScenarioCostImpactChart('chart-scn-cost', SCENARIOS);
  renderScenarioCapacityRiskChart('chart-scn-risk', SCENARIOS);
  renderScenarioSlaChart('chart-scn-sla', SCENARIOS);
  renderScenarioFlowMap('container-scn-flow-map', selectedScenarioId);
}

// ─── Open Scenario Detail Drawer ────────────────────────────
export function openScenarioDrawer(scenarioId) {
  const scenario = SCENARIOS.find((s) => s.id === scenarioId) || SCENARIOS[2];
  const overlay = document.getElementById('scenario-drawer-overlay');
  const content = document.getElementById('scenario-drawer-content');
  if (!overlay || !content) return;

  const isRec = scenario.id === 'SCN_REBALANCE';

  content.innerHTML = `
    <!-- Header -->
    <div style="margin-bottom:20px">
      <span class="scn-drawer-tag ${isRec ? 'tag-success' : 'tag-primary'}">${scenario.badge || 'Scenario Specification'}</span>
      <h2 style="font-size:20px;font-weight:800;color:var(--text-1);margin-bottom:6px">${scenario.name}</h2>
      <p style="font-size:13px;color:var(--text-2);line-height:1.5">${scenario.description}</p>
    </div>

    <!-- Section A: Scenario Objective -->
    <div class="scn-section-box">
      <div class="drawer-section-title" style="margin-top:0">A. Scenario Objective</div>
      <div style="font-size:13px;font-weight:600;color:var(--text-1);margin-bottom:6px">
        🎯 ${scenario.objective?.goal || 'Optimize network flows to balance cost and SLA'}
      </div>
      <div class="flex items-center gap-md text-xs text-muted">
        <div><strong>Primary Metric:</strong> ${scenario.objective?.primaryMetric || 'Total Cost'}</div>
        <div><strong>Constraint:</strong> ${scenario.objective?.constraint || 'SLA ≥ 95%'}</div>
      </div>
    </div>

    <!-- Section B: What Changed -->
    <div class="scn-section-box">
      <div class="drawer-section-title" style="margin-top:0">B. What Changed (Exact Flow & Facility Modifiers)</div>
      ${
        scenario.changes && scenario.changes.length > 0
          ? scenario.changes
              .map(
                (ch) => `
            <div class="scn-change-row">
              <div>
                <div style="font-weight:600;color:var(--text-1)">${ch.item}</div>
                <div class="text-xs text-muted">${ch.note || ''}</div>
              </div>
              <span class="tag tag-purple" style="font-weight:700">${ch.change}</span>
            </div>
          `
              )
              .join('')
          : '<div class="text-xs text-muted">No structural changes from baseline observed state.</div>'
      }
    </div>

    <!-- Section C: Assumptions & External Signals -->
    <div class="scn-section-box">
      <div class="drawer-section-title" style="margin-top:0">C. Assumptions & Input Provenance</div>
      ${
        scenario.assumptions && scenario.assumptions.length > 0
          ? scenario.assumptions
              .map(
                (asm) => `
            <div class="evidence-row">
              <span class="evidence-label">${asm.label}</span>
              <div class="flex items-center">
                <span class="evidence-value">${asm.value}</span>
                <span class="provenance-badge ${asm.type.toLowerCase().replace(/\s+/g, '-')}">${asm.type}</span>
              </div>
            </div>
          `
              )
              .join('')
          : ''
      }
    </div>

    <!-- Section D: Mathematical Optimisation Settings -->
    <div class="scn-section-box">
      <div class="drawer-section-title" style="margin-top:0">D. Optimisation Settings (Deterministic MILP)</div>
      <div class="evidence-row">
        <span class="evidence-label">Objective Function</span>
        <span class="evidence-value">${scenario.optimisation?.objective || 'Minimise Total Logistics Cost'}</span>
      </div>
      <div class="evidence-row">
        <span class="evidence-label">Locked Decisions</span>
        <span class="evidence-value">${scenario.optimisation?.lockedDecisions || 'Facility footprint'}</span>
      </div>
      <div class="evidence-row">
        <span class="evidence-label">Allowed Variables</span>
        <span class="evidence-value">${scenario.optimisation?.allowedDecisions || 'Multi-echelon flow volumes'}</span>
      </div>
      <div class="evidence-row">
        <span class="evidence-label">Service Constraint</span>
        <span class="evidence-value">${scenario.optimisation?.slaConstraint || 'SLA ≥ 95%'}</span>
      </div>
      <div style="margin-top:8px;font-size:11px;color:var(--text-3)">
        Solver: <strong>PuLP / HiGHS Core</strong> · 100% Deterministic Math Source of Truth
      </div>
    </div>

    <!-- Section E: Scenario Outcome Scorecard -->
    <div class="scn-section-box">
      <div class="drawer-section-title" style="margin-top:0">
        E. Scenario Outcome <span class="provenance-badge model-fact">MODEL FACT</span>
      </div>
      <div class="scn-outcome-grid">
        <div class="scn-outcome-card">
          <div class="text-xs text-muted">Total Cost</div>
          <div style="font-size:15px;font-weight:800;color:var(--primary)">₹${(scenario.totalCost / 100000).toFixed(2)}L</div>
          <div class="text-xs" style="color:var(--green)">${scenario.costChange ? (scenario.costChange < 0 ? '↓ ' : '↑ ') + Math.abs(scenario.costChange) + '%' : '—'}</div>
        </div>
        <div class="scn-outcome-card">
          <div class="text-xs text-muted">On-Time SLA</div>
          <div style="font-size:15px;font-weight:800;color:var(--text-1)">${scenario.sla}%</div>
          <div class="text-xs" style="color:var(--green)">Target: ≥95%</div>
        </div>
        <div class="scn-outcome-card">
          <div class="text-xs text-muted">Delhi NCR Util</div>
          <div style="font-size:15px;font-weight:800;color:${scenario.delhiUtil > 90 ? 'var(--amber)' : 'var(--green)'}">${scenario.delhiUtil || scenario.maxUtil}%</div>
          <div class="text-xs text-muted">Ceiling: 100%</div>
        </div>
        <div class="scn-outcome-card">
          <div class="text-xs text-muted">Capacity Risk</div>
          <div style="font-size:13px;font-weight:700;color:${scenario.capacityRisk === 'Low' || scenario.capacityRisk === 'Very Low' ? 'var(--green)' : 'var(--red)'}">${scenario.capacityRisk}</div>
        </div>
        <div class="scn-outcome-card">
          <div class="text-xs text-muted">Implementation Lead Time</div>
          <div style="font-size:13px;font-weight:700;color:var(--text-1)">${scenario.implementationTime || '2–3 weeks'}</div>
        </div>
        <div class="scn-outcome-card">
          <div class="text-xs text-muted">Scope 3 Carbon</div>
          <div style="font-size:13px;font-weight:700;color:var(--text-1)">${formatNumber(scenario.carbonKg)} kg</div>
        </div>
      </div>
    </div>

    <!-- Section F: Robustness & Stress Testing -->
    <div class="scn-section-box">
      <div class="drawer-section-title" style="margin-top:0">
        F. Robustness & Stress Tests <span class="provenance-badge model-fact">MODEL FACT</span>
      </div>
      ${
        scenario.robustnessTests && scenario.robustnessTests.length > 0
          ? scenario.robustnessTests
              .map(
                (t) => `
            <div class="scn-robust-row">
              <div>
                <div style="font-weight:600;color:var(--text-1)">${t.status === 'PASS' ? '✓' : '⚠'} ${t.test}</div>
                <div class="text-xs text-muted">${t.detail}</div>
              </div>
              <span class="tag ${t.status === 'PASS' ? 'tag-success' : 'tag-danger'}">${t.status}</span>
            </div>
          `
              )
              .join('')
          : '<div class="text-xs text-muted">Standard robustness stress testing passed.</div>'
      }
    </div>

    <!-- Section G: NetGravity AI Assessment -->
    <div class="scn-ai-box">
      <div class="drawer-section-title" style="margin-top:0;color:var(--primary);border-color:var(--purple-100)">
        G. NetGravity AI Assessment <span class="provenance-badge ai-assessment">AI ASSESSMENT</span>
      </div>
      <p style="font-size:13px;font-weight:600;color:var(--text-1);margin-bottom:10px;line-height:1.5">
        "${scenario.aiAssessment?.recommendation || 'Evaluated for optimal trade-off balance.'}"
      </p>

      <div style="margin-bottom:10px">
        <div style="font-size:11px;font-weight:700;color:var(--text-2);text-transform:uppercase;margin-bottom:4px">Why I Recommend / Evaluated This:</div>
        <ul style="padding-left:18px;font-size:12.5px;color:var(--text-2);line-height:1.6">
          ${(scenario.aiAssessment?.why || ['Provides mathematically optimal cost reduction', 'Meets all operational SLA constraints']).map((w) => `<li>${w}</li>`).join('')}
        </ul>
      </div>

      <div>
        <div style="font-size:11px;font-weight:700;color:var(--text-2);text-transform:uppercase;margin-bottom:4px">What I Rejected & Trade-Offs:</div>
        <div style="font-size:12px;color:var(--text-3);line-height:1.5;background:rgba(255,255,255,0.7);padding:8px;border-radius:6px">
          ${scenario.aiAssessment?.whatIRejected || 'No critical disqualifying trade-offs.'}
        </div>
      </div>
    </div>

    <!-- Section H: Actions -->
    <div class="flex flex-col gap-sm">
      ${isRec ? '<button class="btn btn-primary btn-block" id="drawer-btn-review-rec" style="padding:12px">Review Formal Recommendation →</button>' : ''}
      <div class="flex gap-sm">
        <button class="btn btn-secondary btn-block" id="drawer-btn-run-again">Run Scenario Again</button>
        <button class="btn btn-ghost btn-block" id="drawer-btn-close">Close Drawer</button>
      </div>
      <div style="text-align:center;font-size:11px;color:var(--text-3);margin-top:4px">
        🔒 Human governance tier: Approval required before production dispatch changes.
      </div>
    </div>
  `;

  overlay.classList.add('visible');

  // Wire drawer buttons
  document.getElementById('scenario-drawer-close')?.addEventListener('click', closeScenarioDrawer);
  document.getElementById('drawer-btn-close')?.addEventListener('click', closeScenarioDrawer);
  document.getElementById('drawer-btn-run-again')?.addEventListener('click', () => {
    closeScenarioDrawer();
    openCreateToolbox();
  });
  document.getElementById('drawer-btn-review-rec')?.addEventListener('click', () => {
    closeScenarioDrawer();
    // Switch to digital twin or recommendations if available
    window.location.hash = '#tab-digital-twin';
    document.querySelector('.nav-item[data-tab="tab-digital-twin"]')?.click();
  });
}

function closeScenarioDrawer() {
  document.getElementById('scenario-drawer-overlay')?.classList.remove('visible');
}

// ─── Wire Scenario Page Events ──────────────────────────────
function wireScenarioEvents() {
  // Secondary Nav Tabs
  document.querySelectorAll('.scn-tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.scn-tab-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      activeScnTab = btn.dataset.scnTab;
      renderComparisonTable();
    });
  });

  // Top toggle create scenario button
  document.getElementById('btn-toggle-create-scenario')?.addEventListener('click', () => {
    openCreateToolbox();
  });

  // Close toolbox button
  document.getElementById('btn-close-toolbox')?.addEventListener('click', () => {
    closeCreateToolbox();
  });

  // Toolbox Step 1 type selector tiles
  document.querySelectorAll('.scn-type-card').forEach((card) => {
    card.addEventListener('click', () => {
      document.querySelectorAll('.scn-type-card').forEach((c) => c.classList.remove('active'));
      card.classList.add('active');
    });
  });

  // Toolbox Step 3 Advanced Options accordion
  document.getElementById('toolbox-adv-toggle')?.addEventListener('click', () => {
    const content = document.getElementById('toolbox-adv-content');
    const chevron = document.getElementById('adv-chevron');
    if (content) {
      const isHidden = content.classList.contains('hidden');
      if (isHidden) {
        content.classList.remove('hidden');
        if (chevron) chevron.textContent = '▲';
      } else {
        content.classList.add('hidden');
        if (chevron) chevron.textContent = '▼';
      }
    }
  });

  // Toolbox "Run Scenario" button
  document.getElementById('btn-run-toolbox-scenario')?.addEventListener('click', handleRunScenario);

  // Metric View Select Dropdown
  document.getElementById('scn-metric-view-select')?.addEventListener('change', (e) => {
    const val = e.target.value;
    activeMetricView = val;
    if (val === 'key') {
      activeMetricKeys = ['totalCost', 'costChange', 'sla', 'capacityRisk', 'delhiUtil', 'implementationTime', 'confidence'];
    } else if (val === 'financial') {
      activeMetricKeys = ['totalCost', 'costChange', 'transportCost', 'fixedCost', 'inventoryCost'];
    } else if (val === 'operations') {
      activeMetricKeys = ['sla', 'capacityRisk', 'delhiUtil', 'avgUtil', 'implementationTime'];
    } else if (val === 'all') {
      activeMetricKeys = Object.keys(ALL_METRIC_DEFS);
    }
    renderComparisonTable();
  });

  // Customize Metrics Button & Modal
  document.getElementById('btn-customize-metrics')?.addEventListener('click', () => {
    document.getElementById('modal-metric-customizer')?.classList.add('visible');
  });

  document.getElementById('modal-close-metrics')?.addEventListener('click', () => {
    document.getElementById('modal-metric-customizer')?.classList.remove('visible');
  });

  document.getElementById('btn-cancel-metrics')?.addEventListener('click', () => {
    document.getElementById('modal-metric-customizer')?.classList.remove('visible');
  });

  document.getElementById('btn-save-metrics')?.addEventListener('click', () => {
    const checked = [];
    document.querySelectorAll('#modal-metric-customizer input[data-metric]:checked').forEach((cb) => {
      checked.push(cb.dataset.metric);
    });
    if (checked.length > 0) {
      activeMetricKeys = checked;
      renderComparisonTable();
    }
    document.getElementById('modal-metric-customizer')?.classList.remove('visible');
  });

  document.getElementById('btn-reset-metrics')?.addEventListener('click', () => {
    activeMetricKeys = ['totalCost', 'costChange', 'sla', 'capacityRisk', 'delhiUtil', 'implementationTime', 'confidence'];
    document.querySelectorAll('#modal-metric-customizer input[data-metric]').forEach((cb) => {
      cb.checked = activeMetricKeys.includes(cb.dataset.metric);
    });
    renderComparisonTable();
    document.getElementById('modal-metric-customizer')?.classList.remove('visible');
  });

  // Drawer Overlay Click
  document.getElementById('scenario-drawer-overlay')?.addEventListener('click', (e) => {
    if (e.target.id === 'scenario-drawer-overlay') closeScenarioDrawer();
  });

  // View Detailed Results Button
  document.getElementById('btn-view-detailed-results')?.addEventListener('click', () => {
    openScenarioDrawer(selectedScenarioId);
  });
}

function openCreateToolbox() {
  const toolbox = document.getElementById('scenario-toolbox-pane');
  if (toolbox) {
    toolbox.classList.remove('hidden');
    toolbox.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function closeCreateToolbox() {
  document.getElementById('scenario-toolbox-pane')?.classList.add('hidden');
}

// ─── Handle Scenario Execution (MILP Solver Simulation) ─────
function handleRunScenario() {
  const facilitySelect = document.getElementById('toolbox-facility');
  const facilityName = facilitySelect?.options[facilitySelect.selectedIndex]?.text || 'Delhi NCR DC';
  const changeDir = document.getElementById('toolbox-change-dir')?.value || 'INCREASE';
  const amount = document.getElementById('toolbox-amount')?.value || '2000';
  const notes = document.getElementById('toolbox-notes')?.value || 'Custom planner intervention';

  const progressBox = document.getElementById('toolbox-progress');
  const runBtn = document.getElementById('btn-run-toolbox-scenario');
  if (!progressBox || !runBtn) return;

  // Show progress state
  progressBox.classList.remove('hidden');
  runBtn.disabled = true;
  runBtn.innerHTML = '<span>⏳ Evaluating with PuLP / HiGHS...</span>';

  // Step 1: Solving
  setTimeout(() => {
    const stepSolver = document.getElementById('progress-step-solver');
    if (stepSolver) {
      stepSolver.className = 'progress-step done';
      stepSolver.textContent = '✓ MILP Optimization Complete (0.42s)';
    }

    const stepResilience = document.getElementById('progress-step-resilience');
    if (stepResilience) {
      stepResilience.className = 'progress-step active';
      stepResilience.textContent = '● Resilience Stress Testing (+15% surge)...';
    }

    // Step 2: Stress testing & AI Assessment
    setTimeout(() => {
      if (stepResilience) {
        stepResilience.className = 'progress-step done';
        stepResilience.textContent = '✓ Robustness Verified (SLA: 96.8%)';
      }

      const stepAi = document.getElementById('progress-step-ai');
      if (stepAi) {
        stepAi.className = 'progress-step done';
        stepAi.textContent = '✓ AI Trade-off Assessment Generated';
      }

      // Step 3: Insert new evaluated scenario
      const newScnId = `SCN_CUSTOM_${Date.now()}`;
      const newScenario = {
        id: newScnId,
        num: `My ${SCENARIOS.filter((s) => s.source === 'user').length + 1}`,
        name: `My Scenario ${SCENARIOS.filter((s) => s.source === 'user').length + 1}: ${changeDir === 'INCREASE' ? 'Expand' : 'Reduce'} ${facilityName}`,
        cardTitle: `${changeDir === 'INCREASE' ? 'Expand' : 'Reduce'} ${facilityName}`,
        shortName: `My Scen ${SCENARIOS.filter((s) => s.source === 'user').length + 1}`,
        type: 'USER_CREATED',
        source: 'user',
        badge: 'User Created',
        badgeClass: 'tag-primary',
        status: 'Evaluated',
        description: `${changeDir === 'INCREASE' ? 'Increase' : 'Decrease'} capacity by ${formatNumber(amount)} units/day at ${facilityName}.`,
        highlight: notes,
        totalCost: 1208000,
        costChange: -6.0,
        transportCost: 820000,
        fixedCost: 336000,
        inventoryCost: 52000,
        sla: 96.9,
        avgUtil: 61.5,
        maxUtil: 76.0,
        delhiUtil: 76.0,
        capacityRisk: 'Very Low',
        capacityRiskClass: 'green',
        carbonKg: 129000,
        implementationCost: 70000,
        implementationTime: '4–6 weeks',
        confidence: 'High Confidence',
        stars: 4,
        robustness: 'High',
        feasible: true,
        objective: {
          goal: `Evaluate ${changeDir.toLowerCase()} of ${formatNumber(amount)} u/d at ${facilityName}`,
          primaryMetric: 'Total Cost & SLA',
          constraint: 'SLA ≥ 95%',
        },
        changes: [
          { item: `${facilityName} Capacity`, change: `${changeDir === 'INCREASE' ? '+' : '-'}${formatNumber(amount)} units/day`, note: notes },
        ],
        assumptions: [
          { label: 'Demand Horizon', value: 'December Peak Surge (+14.2%)', type: 'FORECAST' },
          { label: 'Footprint Status', value: 'Capacity Modification', type: 'MODEL FACT' },
        ],
        optimisation: {
          objective: 'Minimise Total Network Logistics Cost',
          lockedDecisions: 'Existing DC network topology',
          allowedDecisions: 'Dynamic lane reallocations',
          slaConstraint: '≥95.0%',
        },
        robustnessTests: [
          { test: '+15% Demand Surge', status: 'PASS', detail: 'Absorbs peak surge without SLA degradation' },
          { test: 'Corridor Resilience', status: 'PASS', detail: 'Maintains alternative routing' },
        ],
        aiAssessment: {
          recommendation: `Feasible and cost-effective intervention. Achieves 6.0% cost reduction and lowers peak capacity utilization to 76%.`,
          why: [
            `Total cost drops to ₹12.08L (↓6.0% vs baseline)`,
            `SLA increases to 96.9%`,
            `Eliminates capacity risk at ${facilityName}`,
          ],
          whatIRejected: 'Compared to NetGravity Recommended Scenario 1 (₹11.84L), this requires minor operational capacity adjustment costs.',
        },
      };

      SCENARIOS.push(newScenario);
      selectedScenarioId = newScnId;

      // Update UI
      const badge = document.getElementById('scn-count-badge');
      if (badge) badge.textContent = SCENARIOS.length - 1;

      renderComparisonTable();
      renderImpactCharts();

      // Reset button
      runBtn.disabled = false;
      runBtn.innerHTML = '<span>▶ Run Scenario</span>';
      progressBox.classList.add('hidden');
      closeCreateToolbox();

      // Open new scenario details
      openScenarioDrawer(newScnId);
    }, 1200);
  }, 1000);
}
