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
  renderMyScenariosGrid();
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
      openScenarioDrawer(id);
    });
  });
}

// ─── Render My Scenarios Grid ───────────────────────────────
function renderMyScenariosGrid() {
  const container = document.getElementById('my-scenarios-grid');
  if (!container) return;

  const userScenarios = SCENARIOS.filter((s) => s.source === 'user');

  if (userScenarios.length === 0) {
    container.innerHTML = `
      <div class="card" style="padding:16px;text-align:center;color:var(--text-3);font-size:12.5px;grid-column:1 / -1;background:#faf8fd;border:1px dashed var(--border)">
        No custom what-if scenarios created yet. Use the top <strong>+ Create New Scenario</strong> button to run custom what-if evaluations.
      </div>
    `;
    return;
  }

  container.innerHTML = userScenarios
    .map(
      (s) => `
      <div class="my-scenario-card" data-scn-id="${s.id}" style="cursor:pointer" title="Click to inspect scenario details">
        <div>
          <div class="flex items-center justify-between mb-xs">
            <span class="tag tag-primary" style="font-size:10px">${s.badge || 'User Created'}</span>
            <span class="text-xs text-muted" style="font-weight:600">MILP Solved</span>
          </div>
          <div style="font-size:14px;font-weight:700;color:var(--text-1);margin-bottom:4px">${s.cardTitle || s.name}</div>
          <div style="font-size:12px;color:var(--text-2);margin-bottom:8px;line-height:1.4">${s.description}</div>
        </div>
        <div class="flex items-center justify-between pt-sm" style="border-top:1px solid var(--border-light)">
          <div class="text-xs">
            <strong>Cost:</strong> <span style="color:var(--green);font-weight:700">${s.costChange ? (s.costChange < 0 ? '↓ ' : '↑ ') + Math.abs(s.costChange) + '%' : '—'}</span> · 
            <strong>SLA:</strong> ${s.sla}%
          </div>
          <span class="text-xs" style="color:var(--primary);font-weight:700">Click to Inspect →</span>
        </div>
      </div>
    `
    )
    .join('');

  // Wire card clicks
  container.querySelectorAll('.my-scenario-card[data-scn-id]').forEach((card) => {
    card.addEventListener('click', () => {
      const id = card.dataset.scnId;
      selectedScenarioId = id;
      openScenarioDrawer(id);
    });
  });
}

// ─── Render Comparison Table ────────────────────────────────
function renderComparisonTable() {
  const container = document.getElementById('scenario-comparison-table-wrap');
  if (!container) return;

  const displayScenarios = SCENARIOS;

  const html = `
    <table class="scenario-comparison-table">
      <thead>
        <tr>
          <th>Metrics</th>
          ${displayScenarios
      .map((s) => {
        const isRec = s.id === 'SCN_REBALANCE';
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
      ${scenario.changes && scenario.changes.length > 0
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
      ${scenario.assumptions && scenario.assumptions.length > 0
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
      ${scenario.robustnessTests && scenario.robustnessTests.length > 0
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
  // Floating Menu Scroll Actions
  document.querySelectorAll('.scn-floating-btn[data-target]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      document.querySelectorAll('.scn-floating-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      const targetId = btn.dataset.target;
      const targetEl = document.getElementById(targetId);
      if (targetEl) {
        targetEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
  });

  // Wire inspect optimised base card
  document.getElementById('card-opt-base')?.addEventListener('click', () => {
    openScenarioDrawer('SCN_OPTIMISED_BASE');
  });

  // Setup Scroll Spy for Floating Menu
  setupScenarioScrollSpy();

  // Top toggle create scenario button
  document.getElementById('btn-toggle-create-scenario')?.addEventListener('click', () => {
    openCreateToolbox();
  });

  // Close toolbox buttons
  document.getElementById('btn-close-toolbox')?.addEventListener('click', closeCreateToolbox);
  document.getElementById('btn-cancel-toolbox')?.addEventListener('click', closeCreateToolbox);
  document.getElementById('modal-create-toolbox')?.addEventListener('click', (e) => {
    if (e.target.id === 'modal-create-toolbox') closeCreateToolbox();
  });

  // Toolbox Step 1 type selector tiles
  document.querySelectorAll('.scn-type-card').forEach((card) => {
    card.addEventListener('click', () => {
      document.querySelectorAll('.scn-type-card').forEach((c) => c.classList.remove('active'));
      card.classList.add('active');
      const type = card.dataset.type;
      renderToolboxStep2(type);
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

function setupScenarioScrollSpy() {
  const sectionIds = ['scn-section-recommended', 'scn-section-opt-base', 'scn-section-my-scenarios', 'scn-section-comparison'];
  const sections = sectionIds.map((id) => document.getElementById(id)).filter(Boolean);

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          const id = entry.target.id;
          document.querySelectorAll('.scn-floating-btn').forEach((btn) => {
            if (btn.dataset.target === id) {
              btn.classList.add('active');
            } else {
              btn.classList.remove('active');
            }
          });
        }
      });
    },
    { rootMargin: '-10% 0px -70% 0px', threshold: 0 }
  );

  sections.forEach((s) => observer.observe(s));
}

// ─── Handle Scenario Execution (Agentic Calibration & Solver) ─
let currentToolboxType = 'CHANGE_CAPACITY';

const SCENARIO_TYPE_META = {
  CHANGE_CAPACITY: {
    badge: 'Change Capacity',
    desc: 'Adjust capacity at existing distribution centers to resolve bottlenecks or reduce unused overhead.',
  },
  OPEN_FACILITY: {
    badge: 'Open Facility',
    desc: 'Evaluate adding a new distribution center or cross-dock hub to the network topology.',
  },
  CLOSE_FACILITY: {
    badge: 'Close Facility',
    desc: 'Decommission a sub-scale node and reassign regional customer demand across surviving DCs.',
  },
  CHANGE_DEMAND: {
    badge: 'Change Demand',
    desc: 'Stress test network resilience under regional or national demand surges and SKU mix shifts.',
  },
  CHANGE_TRANSPORT_COST: {
    badge: 'Change Transport Cost',
    desc: 'Simulate diesel fuel shocks, highway toll surcharges, or negotiated freight rate discounts.',
  },
  CHANGE_SLA: {
    badge: 'Change SLA Target',
    desc: 'Evaluate cost and routing implications of tighter lead-time and on-time service level targets.',
  },
};

function renderToolboxStep2(type) {
  currentToolboxType = type;
  const container = document.getElementById('toolbox-dynamic-fields');
  const badgeEl = document.getElementById('toolbox-active-type-badge');
  const descEl = document.getElementById('toolbox-active-type-desc');

  if (badgeEl) badgeEl.textContent = SCENARIO_TYPE_META[type]?.badge || type;
  if (descEl) descEl.textContent = SCENARIO_TYPE_META[type]?.desc || '';
  if (!container) return;

  if (type === 'CHANGE_CAPACITY') {
    container.innerHTML = `
      <div class="form-group mb-sm">
        <label class="form-label">Select Target Facility</label>
        <select class="form-select" id="tb-cap-facility">
          <option value="DC_DELHI">Delhi NCR DC (Current: 12,000 u/d · 108% Peak Load)</option>
          <option value="DC_MUMBAI">Mumbai DC (Current: 10,000 u/d · 85% Load)</option>
          <option value="DC_BENGALURU">Bengaluru DC (Current: 8,000 u/d · 82% Load)</option>
          <option value="DC_KOLKATA">Kolkata DC (Current: 6,000 u/d · 68% Load)</option>
          <option value="DC_GUWAHATI">Guwahati DC (Current: 3,000 u/d · 45% Load)</option>
        </select>
      </div>

      <div class="form-group mb-sm">
        <label class="form-label">Capacity Adjustment Direction & Volume</label>
        <div class="flex gap-sm">
          <select class="form-select" id="tb-cap-dir" style="width:130px">
            <option value="INCREASE">Increase (+)</option>
            <option value="DECREASE">Decrease (-)</option>
          </select>
          <div style="position:relative;flex:1">
            <input type="number" class="form-input" id="tb-cap-amount" value="2500" step="500" min="500">
            <span style="position:absolute;right:10px;top:9px;font-size:11px;color:var(--text-3)">units/day</span>
          </div>
        </div>
      </div>

      <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">CapEx / Setup Cost (₹)</label>
          <input type="text" class="form-input" id="tb-cap-cost" value="₹2,50,000">
        </div>
        <div class="form-group">
          <label class="form-label">Monthly Fixed Opex Delta</label>
          <input type="text" class="form-input" id="tb-cap-opex" value="+₹35,000/mo">
        </div>
      </div>

      <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Start Horizon</label>
          <input type="text" class="form-input" id="tb-cap-start" value="1 Oct 2026">
        </div>
        <div class="form-group">
          <label class="form-label">End Horizon <span class="text-muted">(Optional)</span></label>
          <input type="text" class="form-input" id="tb-cap-end" value="31 Dec 2026">
        </div>
      </div>

      <div class="form-group">
        <label class="form-label">Operational Notes</label>
        <input type="text" class="form-input" id="tb-cap-notes" value="Expand peak storage buffer to absorb December surge without bottlenecking">
      </div>
    `;
  } else if (type === 'CLOSE_FACILITY') {
    container.innerHTML = `
      <div class="form-group mb-sm">
        <label class="form-label">Select Facility to Decommission / Close</label>
        <select class="form-select" id="tb-close-facility">
          <option value="DC_GUWAHATI">Guwahati DC (3,000 u/d · Sub-scale 45% utilisation)</option>
          <option value="DC_KOLKATA">Kolkata DC (6,000 u/d · Eastern Hub)</option>
          <option value="DC_BENGALURU">Bengaluru DC (8,000 u/d · Southern Hub)</option>
          <option value="DC_MUMBAI">Mumbai DC (10,000 u/d · Western Hub)</option>
          <option value="DC_DELHI">Delhi NCR DC (12,000 u/d · Northern Hub)</option>
        </select>
      </div>

      <div class="form-group mb-sm">
        <label class="form-label">Volume Reassignment Strategy</label>
        <select class="form-select" id="tb-close-reassign">
          <option value="MILP_GLOBAL">Re-optimize all network flows via PuLP MILP (Recommended)</option>
          <option value="NEAREST_DC">Transfer 100% volume to nearest surviving regional DC</option>
          <option value="DIRECT_PLANT">Direct dispatch from Pune / Baddi plants to demand markets</option>
        </select>
      </div>

      <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Exit / Lease Severance (₹)</label>
          <input type="text" class="form-input" id="tb-close-exit" value="₹4,50,000">
        </div>
        <div class="form-group">
          <label class="form-label">Monthly Fixed Cost Savings</label>
          <input type="text" class="form-input" id="tb-close-saving" value="₹1,20,000/mo">
        </div>
      </div>

      <div class="form-group mb-sm">
        <label class="form-label">Effective Closure Date</label>
        <input type="text" class="form-input" id="tb-close-date" value="1 Nov 2026">
      </div>

      <div class="form-group">
        <label class="form-label">Strategic Rationale</label>
        <input type="text" class="form-input" id="tb-close-notes" value="Consolidate low-volume sub-scale node into regional hub to reduce fixed lease overhead">
      </div>
    `;
  } else if (type === 'OPEN_FACILITY') {
    container.innerHTML = `
      <div class="form-group mb-sm">
        <label class="form-label">Candidate DC Location</label>
        <select class="form-select" id="tb-open-location">
          <option value="DC_HYDERABAD">Hyderabad DC (South-Central Corridor)</option>
          <option value="DC_AHMEDABAD">Ahmedabad DC (Gujarat & Western Corridor)</option>
          <option value="DC_JAIPUR">Jaipur DC (North-West Regional Hub)</option>
          <option value="DC_CHENNAI">Chennai DC (Southern Coastal Corridor)</option>
          <option value="DC_LUCKNOW">Lucknow DC (Central Uttar Pradesh Hub)</option>
          <option value="DC_INDORE">Indore DC (Central India Transit Hub)</option>
        </select>
      </div>

      <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Planned Capacity</label>
          <div style="position:relative">
            <input type="number" class="form-input" id="tb-open-capacity" value="6000" step="1000">
            <span style="position:absolute;right:10px;top:9px;font-size:11px;color:var(--text-3)">units/day</span>
          </div>
        </div>
        <div class="form-group">
          <label class="form-label">Facility Tier</label>
          <select class="form-select" id="tb-open-tier">
            <option value="REGIONAL">Regional Distribution Hub</option>
            <option value="CROSSDOCK">Urban Fast Cross-Dock</option>
            <option value="SATELLITE">Satellite Micro-Fulfillment</option>
          </select>
        </div>
      </div>

      <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Initial Fitout CapEx (₹)</label>
          <input type="text" class="form-input" id="tb-open-capex" value="₹22,00,000">
        </div>
        <div class="form-group">
          <label class="form-label">Fixed Monthly Opex (₹)</label>
          <input type="text" class="form-input" id="tb-open-opex" value="₹2,80,000/mo">
        </div>
      </div>

      <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Target Go-Live Date</label>
          <input type="text" class="form-input" id="tb-open-date" value="1 Dec 2026">
        </div>
        <div class="form-group">
          <label class="form-label">Primary Inbound Source</label>
          <select class="form-select" id="tb-open-source">
            <option value="PUNE">Pune Plant (High Capacity)</option>
            <option value="BADDI">Baddi Plant (Northern Inbound)</option>
            <option value="DUAL">Dual-Source Allocation</option>
          </select>
        </div>
      </div>

      <div class="form-group">
        <label class="form-label">Expansion Objective</label>
        <input type="text" class="form-input" id="tb-open-notes" value="Offload regional demand and shorten last-mile delivery times">
      </div>
    `;
  } else if (type === 'CHANGE_DEMAND') {
    container.innerHTML = `
      <div class="form-group mb-sm">
        <label class="form-label">Target Market / Region</label>
        <select class="form-select" id="tb-dem-region">
          <option value="ALL">All Markets (National Demand Surge · +14.2% Base)</option>
          <option value="NORTH">North Region (Delhi NCR, Lucknow, Jaipur)</option>
          <option value="WEST">West Region (Mumbai, Pune, Ahmedabad)</option>
          <option value="EAST">East Region (Kolkata, Patna, Guwahati)</option>
          <option value="SOUTH">South Region (Bengaluru, Chennai, Hyderabad)</option>
        </select>
      </div>

      <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Demand Surge Magnitude (%)</label>
          <input type="number" class="form-input" id="tb-dem-pct" value="15.0" step="2.5" min="1" max="50">
        </div>
        <div class="form-group">
          <label class="form-label">SKU Category Focus</label>
          <select class="form-select" id="tb-dem-sku">
            <option value="ALL">All SKU Categories (Uniform Surge)</option>
            <option value="FAST">High-Velocity SKUs</option>
            <option value="SEASONAL">Seasonal & Promotional Lines</option>
          </select>
        </div>
      </div>

      <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Forecast Horizon</label>
          <select class="form-select" id="tb-dem-horizon">
            <option value="DEC_PEAK">December Peak Surge (+14.2%)</option>
            <option value="Q4_SUSTAINED">Q4 Full Quarter Sustained</option>
            <option value="POST_FESTIVAL">Post-Festival High Baseline</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Demand Confidence Interval</label>
          <select class="form-select" id="tb-dem-ci">
            <option value="P90">P90 Upper Bound (Stress Test)</option>
            <option value="P50">P50 Mean Expected</option>
          </select>
        </div>
      </div>

      <div class="form-group">
        <label class="form-label">Simulation Notes</label>
        <input type="text" class="form-input" id="tb-dem-notes" value="Evaluate supply chain resilience against extreme peak season surge">
      </div>
    `;
  } else if (type === 'CHANGE_TRANSPORT_COST') {
    container.innerHTML = `
      <div class="form-group mb-sm">
        <label class="form-label">Target Corridor / Echelon</label>
        <select class="form-select" id="tb-freight-lane">
          <option value="ALL_LANES">All Active Transportation Lanes (National Fuel Shock)</option>
          <option value="PRIMARY">Primary Inbound (Plant → DC Long-Haul Corridors)</option>
          <option value="SECONDARY">Secondary Outbound (DC → Customer Demand Markets)</option>
          <option value="EASTERN">Eastern Corridor (Baddi → Kolkata / Guwahati)</option>
          <option value="NORTHERN">Northern Trunk (Baddi → Delhi NCR / Lucknow)</option>
        </select>
      </div>

      <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Rate Adjustment Direction & %</label>
          <div class="flex gap-sm">
            <select class="form-select" id="tb-freight-dir" style="width:130px">
              <option value="INCREASE">Rate Hike (+)</option>
              <option value="DECREASE">Discount (-)</option>
            </select>
            <input type="number" class="form-input" id="tb-freight-pct" value="12.5" step="1.0" min="0">
          </div>
        </div>
        <div class="form-group">
          <label class="form-label">Primary Cost Driver</label>
          <select class="form-select" id="tb-freight-driver">
            <option value="DIESEL">Diesel Price Increase (+8.5%)</option>
            <option value="TOLL">Expressway Toll & FASTag Tariff Hike</option>
            <option value="CONTRACT">Annual Carrier Contract Renegotiation</option>
            <option value="GREEN">Green Logistics Rail-Shift Incentive</option>
          </select>
        </div>
      </div>

      <div class="form-group">
        <label class="form-label">Corridor Cost Notes</label>
        <input type="text" class="form-input" id="tb-freight-notes" value="Test modal-shift viability under volatile diesel fuel prices">
      </div>
    `;
  } else if (type === 'CHANGE_SLA') {
    container.innerHTML = `
      <div class="form-group mb-sm">
        <label class="form-label">Target Customer Tier</label>
        <select class="form-select" id="tb-sla-tier">
          <option value="ALL">Network-Wide Standard (All Customer Demand)</option>
          <option value="METRO">Tier 1 Metro Customers (Delhi, Mumbai, Bengaluru)</option>
          <option value="REGIONAL">Tier 2 & 3 Regional Markets</option>
        </select>
      </div>

      <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Target On-Time SLA (%)</label>
          <input type="number" class="form-input" id="tb-sla-target" value="98.0" step="0.5" min="85" max="99.9">
        </div>
        <div class="form-group">
          <label class="form-label">Maximum Delivery Window</label>
          <select class="form-select" id="tb-sla-window">
            <option value="24">24 Hours (Next Day Service)</option>
            <option value="48">48 Hours (Standard Regional)</option>
            <option value="72">72 Hours (Extended Zone)</option>
          </select>
        </div>
      </div>

      <div class="grid-2 mb-sm" style="gap:var(--space-sm)">
        <div class="form-group">
          <label class="form-label">Non-Delivery Penalty (₹/unit)</label>
          <input type="number" class="form-input" id="tb-sla-penalty" value="75" step="10">
        </div>
        <div class="form-group">
          <label class="form-label">Routing Priority Constraint</label>
          <select class="form-select" id="tb-sla-strict">
            <option value="HARD">Strict Feasibility (Zero SLA Breaches Allowed)</option>
            <option value="SOFT">Soft Penalty Optimization (Cost-Service Trade-off)</option>
          </select>
        </div>
      </div>

      <div class="form-group">
        <label class="form-label">SLA Strategy Notes</label>
        <input type="text" class="form-input" id="tb-sla-notes" value="Elevate premium service commitment to gain retail market share">
      </div>
    `;
  }
}

function openCreateToolbox() {
  document.getElementById('modal-create-toolbox')?.classList.add('visible');
  document.getElementById('toolbox-form-body')?.classList.remove('hidden');
  document.getElementById('agent-execution-view')?.classList.add('hidden');

  // Reset to default type
  document.querySelectorAll('.scn-type-card').forEach((c, idx) => {
    if (idx === 0) c.classList.add('active');
    else c.classList.remove('active');
  });

  renderToolboxStep2('CHANGE_CAPACITY');
}

function closeCreateToolbox() {
  document.getElementById('modal-create-toolbox')?.classList.remove('visible');
}

// ─── Run Scenario (Solver Pipeline Simulation) ──────────────
function handleRunScenario() {
  const formBody = document.getElementById('toolbox-form-body');
  const agentView = document.getElementById('agent-execution-view');
  const progressBar = document.getElementById('agent-progress-bar-fill');
  const telemetryBox = document.getElementById('agent-telemetry-box');
  const statusText = document.getElementById('agent-live-status-text');

  if (!agentView || !telemetryBox) return;

  // Build Scenario Attributes Based on Specific Type
  let scnName = '';
  let scnCardTitle = '';
  let scnDesc = '';
  let scnHighlight = '';
  let totalCost = 1205000;
  let costChange = -6.2;
  let sla = 97.2;
  let delhiUtil = 78.0;
  let avgUtil = 62.0;
  let capacityRisk = 'Low';
  let capacityRiskClass = 'green';
  let capEx = 250000;
  let implementationTime = '3–4 weeks';
  let changesList = [];
  let whyBullets = [];
  let rejectionReason = '';

  if (currentToolboxType === 'CHANGE_CAPACITY') {
    const facSelect = document.getElementById('tb-cap-facility');
    const facName = facSelect?.options[facSelect.selectedIndex]?.text.split('(')[0].trim() || 'Delhi NCR DC';
    const dir = document.getElementById('tb-cap-dir')?.value || 'INCREASE';
    const amount = parseInt(document.getElementById('tb-cap-amount')?.value || '2500', 10);
    const notes = document.getElementById('tb-cap-notes')?.value || 'Seasonal capacity adjustment';

    scnName = `User: ${dir === 'INCREASE' ? 'Expand' : 'Reduce'} ${facName} (+${formatNumber(amount)} u/d)`;
    scnCardTitle = `${dir === 'INCREASE' ? 'Expand' : 'Reduce'} ${facName}`;
    scnDesc = `${dir === 'INCREASE' ? 'Added' : 'Reduced'} capacity by ${formatNumber(amount)} units/day at ${facName}.`;
    scnHighlight = notes;
    totalCost = dir === 'INCREASE' ? 1205000 : 1248000;
    costChange = dir === 'INCREASE' ? -6.2 : -2.9;
    sla = dir === 'INCREASE' ? 97.4 : 95.1;
    delhiUtil = facName.includes('Delhi') ? (dir === 'INCREASE' ? 84.0 : 112.0) : 91.0;
    capEx = 250000;
    implementationTime = '3–4 weeks';
    changesList = [{ item: `${facName} Capacity`, change: `${dir === 'INCREASE' ? '+' : '-'}${formatNumber(amount)} units/day`, note: notes }];
    whyBullets = [
      `Resolves ${facName} throughput constraints during peak December surge.`,
      `Maintains healthy 97.4% on-time delivery across northern retail hubs.`,
      `Total recurring logistics cost drops to ₹12.05L (↓6.2% vs baseline).`,
    ];
    rejectionReason = 'Requires minor Capex for temporary facility racking, unlike pure routing optimization.';
  } else if (currentToolboxType === 'CLOSE_FACILITY') {
    const facSelect = document.getElementById('tb-close-facility');
    const facName = facSelect?.options[facSelect.selectedIndex]?.text.split('(')[0].trim() || 'Guwahati DC';
    const strat = document.getElementById('tb-close-reassign')?.options[document.getElementById('tb-close-reassign')?.selectedIndex]?.text || 'Re-optimize via MILP';
    const notes = document.getElementById('tb-close-notes')?.value || 'Decommission sub-scale node';

    scnName = `User: Decommission ${facName}`;
    scnCardTitle = `Decommission ${facName}`;
    scnDesc = `Closed ${facName} node; redistributed regional flow using strategy: ${strat}.`;
    scnHighlight = notes;
    totalCost = 1260000;
    costChange = -1.9;
    sla = 94.8;
    delhiUtil = 93.0;
    capacityRisk = 'Medium';
    capacityRiskClass = 'amber';
    capEx = 450000;
    implementationTime = '6–8 weeks';
    changesList = [
      { item: `${facName} Status`, change: 'Decommissioned (0 units/day)', note: notes },
      { item: 'Flow Reassignment', change: strat, note: 'Absorbed by adjacent regional network' },
    ];
    whyBullets = [
      `Eliminates ₹1.20L/month in fixed facility lease and warehouse overhead.`,
      `Slightly increases secondary outbound transit distances to peripheral markets.`,
      `Net network cost is ₹12.60L with 94.8% SLA.`,
    ];
    rejectionReason = 'Higher freight miles to remote tier-3 markets slightly degrades SLA compared to keeping regional presence.';
  } else if (currentToolboxType === 'OPEN_FACILITY') {
    const locSelect = document.getElementById('tb-open-location');
    const locName = locSelect?.options[locSelect.selectedIndex]?.text.split('(')[0].trim() || 'Hyderabad DC';
    const cap = parseInt(document.getElementById('tb-open-capacity')?.value || '6000', 10);
    const notes = document.getElementById('tb-open-notes')?.value || 'Commission candidate DC';

    scnName = `User: Open ${locName} (${formatNumber(cap)} u/d)`;
    scnCardTitle = `Open ${locName}`;
    scnDesc = `Commissioned new ${locName} with ${formatNumber(cap)} units/day capacity.`;
    scnHighlight = notes;
    totalCost = 1195000;
    costChange = -7.0;
    sla = 98.1;
    delhiUtil = 88.0;
    capacityRisk = 'Low';
    capacityRiskClass = 'green';
    capEx = 2200000;
    implementationTime = '8–12 weeks';
    changesList = [
      { item: `New Node: ${locName}`, change: `+${formatNumber(cap)} units/day capacity`, note: notes },
      { item: 'Network Footprint', change: 'Expanded from 5 to 6 DCs', note: 'New South-Central Hub' },
    ];
    whyBullets = [
      `Significantly reduces secondary delivery lead times in regional corridor.`,
      `Achieves best-in-class 98.1% on-time SLA.`,
      `Unlocks ₹11.95L total monthly cost but requires ₹22L initial setup CapEx.`,
    ];
    rejectionReason = 'Requires substantial upfront capital expenditure (₹22L) and 2–3 months construction timeline.';
  } else if (currentToolboxType === 'CHANGE_DEMAND') {
    const regSelect = document.getElementById('tb-dem-region');
    const regName = regSelect?.options[regSelect.selectedIndex]?.text.split('(')[0].trim() || 'All Markets';
    const pct = parseFloat(document.getElementById('tb-dem-pct')?.value || '15.0');
    const notes = document.getElementById('tb-dem-notes')?.value || 'Demand surge resilience test';

    scnName = `User: ${regName} Demand Surge (+${pct}%)`;
    scnCardTitle = `Surge (+${pct}%): ${regName}`;
    scnDesc = `Simulated +${pct}% demand increase across ${regName}.`;
    scnHighlight = notes;
    totalCost = 1380000;
    costChange = +7.4;
    sla = 95.6;
    delhiUtil = 96.5;
    capacityRisk = 'Medium';
    capacityRiskClass = 'amber';
    capEx = 0;
    implementationTime = 'Immediate (Routing)';
    changesList = [{ item: `${regName} Demand`, change: `+${pct}% Surge`, note: notes }];
    whyBullets = [
      `Stress-tests network feasibility under severe peak surge volumes.`,
      `Dynamic flow reallocations prevent catastrophic bottlenecking.`,
      `Network delivers 95.6% SLA even under elevated throughput pressure.`,
    ];
    rejectionReason = 'Stress scenario representing higher total volume expenditure.';
  } else if (currentToolboxType === 'CHANGE_TRANSPORT_COST') {
    const laneSelect = document.getElementById('tb-freight-lane');
    const laneName = laneSelect?.options[laneSelect.selectedIndex]?.text.split('(')[0].trim() || 'All Active Lanes';
    const dir = document.getElementById('tb-freight-dir')?.value || 'INCREASE';
    const pct = parseFloat(document.getElementById('tb-freight-pct')?.value || '12.5');
    const notes = document.getElementById('tb-freight-notes')?.value || 'Freight rate shift';

    scnName = `User: Freight ${dir === 'INCREASE' ? 'Hike' : 'Discount'} (${dir === 'INCREASE' ? '+' : '-'}${pct}%)`;
    scnCardTitle = `Freight ${dir === 'INCREASE' ? 'Hike' : 'Drop'} (${pct}%)`;
    scnDesc = `Adjusted transportation tariffs by ${dir === 'INCREASE' ? '+' : '-'}${pct}% across ${laneName}.`;
    scnHighlight = notes;
    totalCost = dir === 'INCREASE' ? 1345000 : 1152000;
    costChange = dir === 'INCREASE' ? +4.7 : -10.3;
    sla = 96.5;
    delhiUtil = 89.0;
    capEx = 0;
    implementationTime = 'Immediate';
    changesList = [{ item: `${laneName} Freight Rate`, change: `${dir === 'INCREASE' ? '+' : '-'}${pct}%`, note: notes }];
    whyBullets = [
      `Re-evaluates lane selection and modal trade-offs under modified tariff curves.`,
      `MILP solver re-routes marginal volume to minimize overall cost impact.`,
      `Maintains stable 96.5% on-time SLA.`,
    ];
    rejectionReason = 'Parametric cost sensitivity test.';
  } else if (currentToolboxType === 'CHANGE_SLA') {
    const targetSla = parseFloat(document.getElementById('tb-sla-target')?.value || '98.0');
    const win = document.getElementById('tb-sla-window')?.value || '24';
    const notes = document.getElementById('tb-sla-notes')?.value || 'Tighter SLA target';

    scnName = `User: Strict SLA Target (${targetSla}% / ${win}h)`;
    scnCardTitle = `Strict SLA (${targetSla}%)`;
    scnDesc = `Enforced hard ${targetSla}% SLA target with maximum ${win}-hour delivery window.`;
    scnHighlight = notes;
    totalCost = 1295000;
    costChange = +0.8;
    sla = targetSla;
    delhiUtil = 91.5;
    capEx = 0;
    implementationTime = '1–2 weeks';
    changesList = [
      { item: 'Minimum SLA Target', change: `${targetSla}%`, note: notes },
      { item: 'Lead Time Window', change: `Max ${win} hours`, note: 'Fast-lane prioritization' },
    ];
    whyBullets = [
      `Prioritizes direct line-haul connections to fulfill tight ${win}-hour commitments.`,
      `Guarantees ${targetSla}% on-time customer fulfillment.`,
      `Marginal freight cost increase of +0.8% to satisfy expedited dispatch requirements.`,
    ];
    rejectionReason = 'Tighter lead times increase premium dedicated vehicle runs.';
  }

  // Transition to Agentic Execution Screen
  if (formBody) formBody.classList.add('hidden');
  agentView.classList.remove('hidden');
  if (progressBar) progressBar.style.width = '15%';
  if (statusText) statusText.textContent = 'Agent initializing mathematical graph and parameter bounds...';

  telemetryBox.innerHTML = `
    <div class="telemetry-row">
      <span class="telemetry-spinner"></span>
      <span class="telemetry-tag agent">AGENT</span>
      <span>Ingesting scenario specifications: <strong>${scnCardTitle}</strong>...</span>
    </div>
  `;

  // Step 2 (700ms): Echelon calibration
  setTimeout(() => {
    if (progressBar) progressBar.style.width = '40%';
    if (statusText) statusText.textContent = 'Calibrating multi-echelon network balance & candidate arcs...';
    telemetryBox.innerHTML += `
      <div class="telemetry-row">
        <span class="telemetry-spinner"></span>
        <span class="telemetry-tag agent">AGENT</span>
        <span>Calibrating 380 active transportation corridors & facility mass-balance...</span>
      </div>
    `;
    telemetryBox.scrollTop = telemetryBox.scrollHeight;
  }, 700);

  // Step 3 (1500ms): Solving MILP
  setTimeout(() => {
    if (progressBar) progressBar.style.width = '68%';
    if (statusText) statusText.textContent = 'Executing Mixed-Integer Linear Program in PuLP (HiGHS Backend)...';
    telemetryBox.innerHTML += `
      <div class="telemetry-row">
        <span class="telemetry-spinner"></span>
        <span class="telemetry-tag milp">MILP</span>
        <span>Formulating dual simplex & branch-and-cut optimization model...</span>
      </div>
    `;
    telemetryBox.scrollTop = telemetryBox.scrollHeight;
  }, 1500);

  // Step 4 (2300ms): Optimal Solution & Stress Testing
  setTimeout(() => {
    if (progressBar) progressBar.style.width = '88%';
    if (statusText) statusText.textContent = 'Running resilience stress tests (+15% surge & lane disruptions)...';
    telemetryBox.innerHTML += `
      <div class="telemetry-row">
        <span class="telemetry-tag milp" style="background:#10b981">OPTIMAL</span>
        <span>Deterministic solution found in 0.36s (Cost: ₹${(totalCost / 100000).toFixed(2)}L, SLA: ${sla}%) ✓</span>
      </div>
      <div class="telemetry-row">
        <span class="telemetry-spinner"></span>
        <span class="telemetry-tag stress">CHALLENGER</span>
        <span>Simulating resilience stress test... PASS (SLA maintained ≥95.0%) ✓</span>
      </div>
    `;
    telemetryBox.scrollTop = telemetryBox.scrollHeight;
  }, 2300);

  // Step 5 (3100ms): AI Recommendation Synthesis
  setTimeout(() => {
    if (progressBar) progressBar.style.width = '100%';
    if (statusText) statusText.textContent = 'AI synthesis complete. Formatting executive scorecard...';
    telemetryBox.innerHTML += `
      <div class="telemetry-row">
        <span class="telemetry-tag done">SYNTHESIS</span>
        <span>Synthesized trade-off provenance: Evaluated and ready for executive inspection.</span>
      </div>
    `;
    telemetryBox.scrollTop = telemetryBox.scrollHeight;
  }, 3100);

  // Step 6 (3800ms): Finish, append scenario and open drawer
  setTimeout(() => {
    const userScnCount = SCENARIOS.filter((s) => s.source === 'user').length + 1;
    const newScnId = `SCN_CUSTOM_${Date.now()}`;
    const newScenario = {
      id: newScnId,
      num: `My ${userScnCount}`,
      name: scnName,
      cardTitle: scnCardTitle,
      shortName: `My Scen ${userScnCount}`,
      type: 'USER_CREATED',
      source: 'user',
      badge: 'User Created',
      badgeClass: 'tag-primary',
      status: 'Evaluated',
      description: scnDesc,
      highlight: scnHighlight,
      totalCost: totalCost,
      costChange: costChange,
      transportCost: Math.round(totalCost * 0.68),
      fixedCost: Math.round(totalCost * 0.27),
      inventoryCost: Math.round(totalCost * 0.05),
      sla: sla,
      avgUtil: avgUtil,
      maxUtil: delhiUtil,
      delhiUtil: delhiUtil,
      capacityRisk: capacityRisk,
      capacityRiskClass: capacityRiskClass,
      carbonKg: Math.round(totalCost * 0.11),
      implementationCost: capEx,
      implementationTime: implementationTime,
      confidence: 'High Confidence',
      stars: 4,
      robustness: 'High',
      feasible: true,
      objective: {
        goal: scnDesc,
        primaryMetric: 'Total Cost & SLA',
        constraint: 'SLA ≥ 95%',
      },
      changes: changesList,
      assumptions: [
        { label: 'Demand Horizon', value: 'December Peak Surge (+14.2%)', type: 'FORECAST' },
        { label: 'Footprint Status', value: 'Custom What-If Parameterization', type: 'MODEL FACT' },
      ],
      optimisation: {
        objective: 'Minimise Total Network Logistics Cost',
        lockedDecisions: 'Specified intervention constraints',
        allowedDecisions: 'Dynamic lane reallocations',
        slaConstraint: '≥95.0%',
      },
      robustnessTests: [
        { test: '+15% Demand Surge', status: 'PASS', detail: 'Absorbs peak surge without catastrophic SLA failure' },
        { test: 'Corridor Resilience', status: 'PASS', detail: 'Alternative transport arcs active' },
      ],
      aiAssessment: {
        recommendation: `Evaluated what-if scenario: ${scnCardTitle}. Yields ₹${(totalCost / 100000).toFixed(2)}L total monthly cost with ${sla}% on-time SLA.`,
        why: whyBullets,
        whatIRejected: rejectionReason,
      },
    };

    SCENARIOS.push(newScenario);
    selectedScenarioId = newScnId;

    // Update UI badge & tables
    const badge = document.getElementById('scn-count-badge');
    if (badge) badge.textContent = SCENARIOS.length - 1;

    renderMyScenariosGrid();
    renderComparisonTable();
    closeCreateToolbox();

    // Open Scenario Details Drawer
    openScenarioDrawer(newScnId);
  }, 3800);
}

