/**
 * NetGravity — Main Application Controller
 * ==========================================
 * Tab routing, Home cockpit state, facility/period selectors,
 * KPI rendering, insight list, insight drawer, and all sub-views.
 */

import { PLANTS, DCS, MARKETS, LANES, DATA_QUALITY, SCHEMA_MAPPING,
         CONTRACT_DEMO, SYSTEM_STATUS, GOVERNANCE_TIERS, EXTERNAL_SIGNALS,
         AGENT_STATE, SCENARIOS, RECOMMENDATION, FORECAST, DEMAND_HISTORY,
         PERIODS, FACILITY_KPIS, HOME_INSIGHTS,
         formatCurrency, formatNumber, getUtilColor, getUtilLabel,
         getFacilityById, getInsightsForFacility, getKpisForFacility } from './data.js';
import { initMap, setNetworkState } from './map.js';
import { initTwin3D, setTwin3DState, resizeTwin3D, resumeTwin3D } from './twin3d.js';
import { renderForecastChart,
         renderFacilityThroughputChart, renderFacilityCostBreakdownChart, renderFacilityLaneFlowsChart } from './charts.js';
import { initScenarios } from './scenarios.js';
import { initAgent } from './agent.js';

// ─── State ──────────────────────────────────────────────────
const state = {
  activeTab: 'home',
  networkState: 'actual',
  mapsInitialised: {},
  chartsInitialised: {},
  // Home cockpit state
  facilityType: 'DC',          // 'DC' | 'Plant'
  selectedFacility: 'DC_DELHI',
  selectedPeriod: 'AUG_2026',
};

// ─── Boot ───────────────────────────────────────────────────
function bootApp() {
  try { initTabs(); } catch (e) { console.error('initTabs error:', e); }
  try { initHomeSelectors(); } catch (e) { console.error('initHomeSelectors error:', e); }
  try { renderHome(); } catch (e) { console.error('renderHome error:', e); }
  try { renderTwinTables(); } catch (e) { console.error('renderTwinTables error:', e); }
  try { initScenarios(); } catch (e) { console.error('initScenarios error:', e); }
  try { initAgent(); } catch (e) { console.error('initAgent error:', e); }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootApp);
} else {
  bootApp();
}

// ─── Tab Routing ────────────────────────────────────────────
function initTabs() {
  document.querySelectorAll('.nav-item[data-tab]').forEach(item => {
    item.addEventListener('click', () => {
      const tab = item.dataset.tab;
      if (tab === state.activeTab) return;

      // Update nav
      document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
      item.classList.add('active');

      // Update panels
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      const panel = document.getElementById('tab-' + tab);
      if (panel) panel.classList.add('active');

      state.activeTab = tab;

      // Lazy-init maps, 3D twin and charts
      if (tab === 'twin') {
        setTimeout(() => {
          try {
            if (!state.mapsInitialised['twin-3d']) {
              initTwin3D('twin3d-canvas');
              state.mapsInitialised['twin-3d'] = true;
            } else {
              resumeTwin3D();
              resizeTwin3D();
            }
          } catch (err) {
            console.error('Twin 3D initialization warning:', err);
          }
        }, 50);
      }
      if (tab === 'forecast' && !state.chartsInitialised['forecast']) {
        setTimeout(() => {
          renderForecastChart('chart-forecast');
          renderDataIntelligence();
          state.chartsInitialised['forecast'] = true;
        }, 50);
      }
      if (tab === 'scenarios') {
        setTimeout(() => {
          initScenarios();
          state.chartsInitialised['scenarios'] = true;
        }, 50);
      }
      if (tab === 'recommend') {
        renderRecommendation();
      }

      // Invalidate map/3D canvas sizes on tab switch
      if (tab === 'twin') {
        setTimeout(() => {
          window.dispatchEvent(new Event('resize'));
          resizeTwin3D();
        }, 100);
      }
    });
  });

  // 2D / 3D View Toggle (Digital Twin)
  const viewToggle = document.getElementById('twin-view-toggle');
  if (viewToggle) {
    viewToggle.querySelectorAll('.toggle-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        viewToggle.querySelectorAll('.toggle-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const view = btn.dataset.view;

        const panel2d = document.getElementById('twin-2d-panel');
        const panel3d = document.getElementById('twin-3d-panel');

        if (view === '2d') {
          if (panel2d) panel2d.style.display = 'block';
          if (panel3d) panel3d.style.display = 'none';
          if (!state.mapsInitialised['map-twin']) {
            initMap('map-twin');
            state.mapsInitialised['map-twin'] = true;
          }
          setTimeout(() => window.dispatchEvent(new Event('resize')), 50);
        } else {
          if (panel2d) panel2d.style.display = 'none';
          if (panel3d) panel3d.style.display = 'block';
          if (!state.mapsInitialised['twin-3d']) {
            initTwin3D('twin3d-canvas');
            state.mapsInitialised['twin-3d'] = true;
          } else {
            resumeTwin3D();
            resizeTwin3D();
          }
        }
      });
    });
  }

  // Network state toggles (Digital Twin)
  const mapToggle = document.getElementById('map-toggle-twin');
  if (mapToggle) {
    mapToggle.querySelectorAll('.toggle-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        mapToggle.querySelectorAll('.toggle-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const newState = btn.dataset.state;
        state.networkState = newState;

        // Update 2D map
        setNetworkState(newState);

        // Update 3D twin
        setTwin3DState(newState);

        // Update 3D stats overlay
        const stateLabel = document.getElementById('twin3d-state-label');
        if (stateLabel) {
          const names = { actual: 'Actual', optimised: 'Optimised Base', recommended: 'Recommended' };
          stateLabel.textContent = names[newState] || newState;
        }
      });
    });
  }

  // Window resize handler for 3D canvas
  window.addEventListener('resize', () => {
    resizeTwin3D();
  });

  // Facility panel close
  document.getElementById('fp-close')?.addEventListener('click', closeFacilityPanel);

  // Insight drawer close
  document.getElementById('insight-drawer-close')?.addEventListener('click', closeInsightDrawer);
  document.getElementById('insight-drawer-overlay')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeInsightDrawer();
  });
}

// ═══════════════════════════════════════════════════════════
// HOME — Decision Cockpit
// ═══════════════════════════════════════════════════════════

// ─── Home Selectors ─────────────────────────────────────────
function initHomeSelectors() {
  const selType = document.getElementById('sel-type');
  const selFacility = document.getElementById('sel-facility');
  const selPeriod = document.getElementById('sel-period');

  // Populate period selector
  if (selPeriod) {
    selPeriod.innerHTML = (PERIODS || []).map(p =>
      `<option value="${p.id}">${p.label}</option>`
    ).join('');
    selPeriod.value = state.selectedPeriod;

    selPeriod.addEventListener('change', () => {
      state.selectedPeriod = selPeriod.value;
      renderHome();
    });
  }

  // Type selector changes facility list
  if (selType) {
    selType.addEventListener('change', () => {
      state.facilityType = selType.value;
      populateFacilitySelector();
      renderHome();
    });
  }

  // Facility selector
  if (selFacility) {
    selFacility.addEventListener('change', () => {
      state.selectedFacility = selFacility.value;
      renderHome();
    });
  }

  populateFacilitySelector();

  // View more KPIs → Navigate to full Facility Analytics Dashboard
  document.getElementById('btn-view-more-kpis')?.addEventListener('click', () => {
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.getElementById('tab-facility-dashboard')?.classList.add('active');
    state.activeTab = 'facility-dashboard';
    renderFacilityDashboard();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });

  // Back to Decision Cockpit from Dashboard
  document.getElementById('btn-back-to-home')?.addEventListener('click', () => {
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.getElementById('tab-home')?.classList.add('active');
    state.activeTab = 'home';
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });

  // Ask NetGravity
  document.getElementById('btn-ask-ng')?.addEventListener('click', () => {
    showNotification('NetGravity AI investigation active. Navigate to Scenario Planning to explore alternative interventions.');
  });
}

function populateFacilitySelector() {
  const selFacility = document.getElementById('sel-facility');
  if (!selFacility) return;

  const facilities = state.facilityType === 'DC' ? DCS : PLANTS;

  selFacility.innerHTML = facilities.map(f =>
    `<option value="${f.id}">${f.name}</option>`
  ).join('');

  if (!state.selectedFacility || !facilities.some(f => f.id === state.selectedFacility)) {
    state.selectedFacility = facilities[0].id;
  }
  selFacility.value = state.selectedFacility;
}

// ─── Render Full Home ───────────────────────────────────────
function renderHome() {
  const fac = getFacilityById(state.selectedFacility) || DCS[0];
  if (!fac) return;

  // Update header
  const nameEl = document.getElementById('home-facility-name');
  if (nameEl) nameEl.textContent = fac.name;

  // Update facility dot colour based on utilisation
  const dot = document.getElementById('facility-dot');
  const kpis = getKpisForFacility(state.selectedFacility, state.selectedPeriod);
  if (kpis && dot) {
    const util = kpis.utilisation.value;
    dot.style.background = util >= 90 ? 'var(--red)' : util >= 75 ? 'var(--amber)' : 'var(--green)';
  }

  renderKPIs();
  renderHomeInsights();
}

// ─── Facility Full Analytics Dashboard ──────────────────────
export function renderFacilityDashboard() {
  const fac = getFacilityById(state.selectedFacility);
  if (!fac) return;

  const kpis = getKpisForFacility(state.selectedFacility, state.selectedPeriod);
  const period = PERIODS.find(p => p.id === state.selectedPeriod);
  const isDC = state.selectedFacility.startsWith('DC_');
  const utilPct = isDC ? fac.utilPct : ((fac.throughput / fac.capacity) * 100).toFixed(1);
  const utilColor = getUtilColor(utilPct);

  // Update Top Bar & Header
  document.getElementById('dash-facility-name').textContent = fac.name;
  document.getElementById('dash-facility-type').textContent = isDC ? 'Distribution Centre' : 'Manufacturing Plant';
  document.getElementById('dash-period-label').textContent = period ? period.label : 'August 2026';
  
  const dashDot = document.getElementById('dash-facility-dot');
  if (dashDot) {
    dashDot.style.background = utilPct >= 90 ? 'var(--red)' : utilPct >= 75 ? 'var(--amber)' : 'var(--green)';
  }

  document.getElementById('dash-title').textContent = `${fac.name} — Performance & Analytics`;
  document.getElementById('dash-subtitle').textContent = `${fac.city}, ${fac.state} · ${fac.region} Region · Full operational telemetry and cost breakdown for ${period ? period.short : 'Aug 2026'}.`;

  // 6 Executive Metric Cards
  const metricsGrid = document.getElementById('dash-metrics-grid');
  if (metricsGrid) {
    metricsGrid.innerHTML = `
      <div class="dash-metric-card">
        <div class="dash-metric-title">Capacity & Daily Throughput</div>
        <div class="dash-metric-val" style="color:${utilColor}">${utilPct}% <span style="font-size:14px;color:var(--text-3);font-weight:600">(${formatNumber(fac.throughput)}/${formatNumber(fac.capacity)} u/d)</span></div>
        <div class="dash-metric-sub">
          <span>Spare Capacity: <strong>${formatNumber(fac.capacity - fac.throughput)} u/d</strong></span>
          <span class="tag ${utilPct >= 90 ? 'tag-danger' : utilPct >= 75 ? 'tag-warning' : 'tag-success'}">${getUtilLabel(utilPct)}</span>
        </div>
      </div>

      <div class="dash-metric-card">
        <div class="dash-metric-title">On-Time SLA Performance</div>
        <div class="dash-metric-val" style="color:var(--green)">${kpis ? kpis.sla.value : 96.7}%</div>
        <div class="dash-metric-sub">
          <span>Target: <strong>≥95.0%</strong></span>
          <span style="color:var(--green);font-weight:600">↑ 1.8% vs last period</span>
        </div>
      </div>

      <div class="dash-metric-card">
        <div class="dash-metric-title">Total Operating Cost</div>
        <div class="dash-metric-val" style="color:var(--primary)">${kpis ? formatCurrency(kpis.totalCost.value) : '₹11.8L'}</div>
        <div class="dash-metric-sub">
          <span>Handling: <strong>₹${fac.handlingCost || 4.2}/unit</strong></span>
          <span style="color:var(--green);font-weight:600">↓ 3.2% vs budget</span>
        </div>
      </div>

      <div class="dash-metric-card">
        <div class="dash-metric-title">Inventory Supply Coverage</div>
        <div class="dash-metric-val">${kpis ? kpis.inventoryDays.value : 11.2} <span style="font-size:16px;color:var(--text-3);font-weight:600">days</span></div>
        <div class="dash-metric-sub">
          <span>Holding Value: <strong>₹3.4L avg</strong></span>
          <span>Safety buffer: <strong>140%</strong></span>
        </div>
      </div>

      <div class="dash-metric-card">
        <div class="dash-metric-title">Average Transit Lead Time</div>
        <div class="dash-metric-val">1.2 <span style="font-size:16px;color:var(--text-3);font-weight:600">days</span></div>
        <div class="dash-metric-sub">
          <span>Fastest: <strong>0.3d</strong> · Slowest: <strong>3.5d</strong></span>
          <span class="tag tag-success">On Schedule</span>
        </div>
      </div>

      <div class="dash-metric-card">
        <div class="dash-metric-title">Carbon Footprint Intensity</div>
        <div class="dash-metric-val">0.42 <span style="font-size:16px;color:var(--text-3);font-weight:600">kg CO₂e/u</span></div>
        <div class="dash-metric-sub">
          <span>Total: <strong>14.8t CO₂e/mo</strong></span>
          <span style="color:var(--green);font-weight:600">↓ 2.1% YoY</span>
        </div>
      </div>
    `;
  }

  // Tags on Charts
  const utilTag = document.getElementById('dash-util-tag');
  if (utilTag) utilTag.textContent = `${utilPct}% Utilisation`;
  const costTag = document.getElementById('dash-total-cost-tag');
  if (costTag) costTag.textContent = kpis ? `${formatCurrency(kpis.totalCost.value)} / period` : '₹11.8L / period';

  // Connected Lanes calculation
  const connectedLanes = LANES.filter(l => l.from === state.selectedFacility || l.to === state.selectedFacility).map(l => {
    const isOutbound = l.from === state.selectedFacility;
    const peer = isOutbound ? getFacilityById(l.to) : getFacilityById(l.from);
    return {
      ...l,
      direction: isOutbound ? 'Outbound' : 'Inbound',
      peerName: peer ? peer.name : (l.to || l.from),
      label: isOutbound ? `→ ${peer ? peer.name : l.to}` : `← ${peer ? peer.name : l.from}`
    };
  });

  const laneCountTag = document.getElementById('dash-lane-count-tag');
  if (laneCountTag) laneCountTag.textContent = `${connectedLanes.length} Active Corridors`;

  // Render the 3 Charts
  setTimeout(() => {
    renderFacilityThroughputChart('chart-dash-throughput', fac);
    renderFacilityCostBreakdownChart('chart-dash-costs', fac);
    renderFacilityLaneFlowsChart('chart-dash-lanes', connectedLanes, state.selectedFacility);
  }, 60);

  // Corridor Summary Narrative
  const totalFlow = connectedLanes.reduce((sum, l) => sum + (l.flow || 0), 0);
  const avgCost = connectedLanes.length > 0 ? (connectedLanes.reduce((sum, l) => sum + (l.cost || 0), 0) / connectedLanes.length).toFixed(1) : 0;
  const summaryEl = document.getElementById('dash-corridor-summary');
  if (summaryEl) {
    summaryEl.innerHTML = `
      <div style="font-weight:700;color:var(--text-1);margin-bottom:6px">Corridor Network Health</div>
      <div>• <strong>${connectedLanes.length} active transportation corridors</strong> handle a collective flow of <strong>${formatNumber(totalFlow)} units/day</strong>.</div>
      <div class="mt-xs">• Weighted average transportation rate across all active arcs is <strong>₹${avgCost} / unit</strong>.</div>
      <div class="mt-xs">• Primary inbound supply is linked to manufacturing capacity with an on-time transit confidence of <strong>98.2%</strong>.</div>
    `;
  }

  // Corridor Table
  const tableBody = document.querySelector('#table-dash-lanes tbody');
  if (tableBody) {
    tableBody.innerHTML = connectedLanes.map(l => `
      <tr>
        <td><strong>${l.peerName}</strong></td>
        <td><span class="tag ${l.direction === 'Inbound' ? 'tag-primary' : 'tag-muted'}">${l.direction}</span></td>
        <td class="num">${formatNumber(l.flow)} u/d</td>
        <td class="num">${formatNumber(l.distance)} km</td>
        <td class="num font-bold">₹${l.cost.toFixed(1)}</td>
        <td class="num">${l.leadTime} days</td>
        <td><span class="tag tag-success">${l.mode}</span></td>
      </tr>
    `).join('');
  }

  // Telemetry Grid
  const telGrid = document.getElementById('dash-telemetry-grid');
  if (telGrid) {
    telGrid.innerHTML = `
      <div style="padding:12px;background:var(--bg-elevated);border-radius:var(--r-md);text-align:center">
        <div class="text-xs text-muted" style="text-transform:uppercase;font-weight:600">Connected ERP</div>
        <div style="font-size:16px;font-weight:800;margin:4px 0">SAP S/4HANA</div>
        <div class="text-xs" style="color:var(--green)">✓ Synced 8m ago</div>
      </div>
      <div style="padding:12px;background:var(--bg-elevated);border-radius:var(--r-md);text-align:center">
        <div class="text-xs text-muted" style="text-transform:uppercase;font-weight:600">Warehouse WMS</div>
        <div style="font-size:16px;font-weight:800;margin:4px 0">Manhattan Scale</div>
        <div class="text-xs" style="color:var(--green)">✓ Real-time feed</div>
      </div>
      <div style="padding:12px;background:var(--bg-elevated);border-radius:var(--r-md);text-align:center">
        <div class="text-xs text-muted" style="text-transform:uppercase;font-weight:600">Transport TMS</div>
        <div style="font-size:16px;font-weight:800;margin:4px 0">Oracle OTM Cloud</div>
        <div class="text-xs" style="color:var(--green)">✓ 384 GPS pings/hr</div>
      </div>
      <div style="padding:12px;background:var(--bg-elevated);border-radius:var(--r-md);text-align:center">
        <div class="text-xs text-muted" style="text-transform:uppercase;font-weight:600">Data Integrity</div>
        <div style="font-size:16px;font-weight:800;margin:4px 0;color:var(--green)">99.2%</div>
        <div class="text-xs text-muted">0 schema mismatches</div>
      </div>
    `;
  }
}


// ─── KPI Cards ──────────────────────────────────────────────
function renderKPIs() {
  const kpis = getKpisForFacility(state.selectedFacility, state.selectedPeriod);
  const grid = document.getElementById('kpi-grid');
  if (!kpis || !grid) return;

  const period = PERIODS.find(p => p.id === state.selectedPeriod);
  const prevLabel = kpis.prevLabel || 'previous period';

  const utilDelta = kpis.utilisation.value - kpis.utilisation.prev;
  const slaDelta = kpis.sla.value - kpis.sla.prev;
  const costDelta = ((kpis.totalCost.value - kpis.totalCost.prev) / kpis.totalCost.prev * 100);
  const invDelta = kpis.inventoryDays.value - kpis.inventoryDays.prev;

  grid.innerHTML = `
    <!-- Utilisation -->
    <div class="kpi-card status-${kpis.utilisation.status}">
      <div class="kpi-header">
        <div class="kpi-label">Capacity Utilisation</div>
        <div class="kpi-icon ${kpis.utilisation.status === 'critical' ? 'red' : kpis.utilisation.status === 'warning' ? 'amber' : 'purple'}">📊</div>
      </div>
      <div class="kpi-value">${kpis.utilisation.value}<span class="kpi-unit">%</span></div>
      <div class="kpi-desc">of ${formatNumber(kpis.utilisation.capacity)} ${kpis.utilisation.unit}</div>
      <div class="kpi-compare">
        <span class="${utilDelta > 0 ? 'up' : 'down'}">↑ ${Math.abs(utilDelta).toFixed(0)}%</span>
        <span class="kpi-compare-label"> vs ${prevLabel}</span>
      </div>
    </div>

    <!-- SLA -->
    <div class="kpi-card status-${kpis.sla.status}">
      <div class="kpi-header">
        <div class="kpi-label">On-time Service (SLA)</div>
        <div class="kpi-icon ${kpis.sla.value >= kpis.sla.target ? 'green' : 'red'}">✅</div>
      </div>
      <div class="kpi-value">${kpis.sla.value}<span class="kpi-unit">%</span></div>
      <div class="kpi-desc">Target: ≥${kpis.sla.target}%</div>
      <div class="kpi-compare">
        <span class="${slaDelta >= 0 ? 'down' : 'up'}">↑ ${Math.abs(slaDelta).toFixed(1)}%</span>
        <span class="kpi-compare-label"> vs ${prevLabel}</span>
      </div>
    </div>

    <!-- Total Cost -->
    <div class="kpi-card status-${kpis.totalCost.status}">
      <div class="kpi-header">
        <div class="kpi-label">Total Cost</div>
        <div class="kpi-icon purple">₹</div>
      </div>
      <div class="kpi-value">${formatCurrency(kpis.totalCost.value)}</div>
      <div class="kpi-desc">Total cost for period</div>
      <div class="kpi-compare">
        <span class="${costDelta <= 0 ? 'down' : 'up'}">${costDelta <= 0 ? '↓' : '↑'} ${Math.abs(costDelta).toFixed(1)}%</span>
        <span class="kpi-compare-label"> vs ${prevLabel}</span>
      </div>
    </div>

    <!-- Inventory Days -->
    <div class="kpi-card status-${kpis.inventoryDays.value > 15 ? 'warning' : 'normal'}">
      <div class="kpi-header">
        <div class="kpi-label">Inventory Days</div>
        <div class="kpi-icon purple">📦</div>
      </div>
      <div class="kpi-value">${kpis.inventoryDays.value}<span class="kpi-unit"> days</span></div>
      <div class="kpi-desc">of supply</div>
      <div class="kpi-compare">
        <span class="${invDelta <= 0 ? 'down' : 'up'}">${invDelta <= 0 ? '↓' : '↑'} ${Math.abs(invDelta).toFixed(1)} days</span>
        <span class="kpi-compare-label"> vs ${prevLabel}</span>
      </div>
    </div>
  `;
}

// ─── Expanded KPIs ──────────────────────────────────────────
function renderExpandedKpis() {
  const panel = document.getElementById('expanded-kpis');
  if (!panel) return;

  if (!state.expandedKpis) {
    panel.classList.add('hidden');
    return;
  }

  const kpis = getKpisForFacility(state.selectedFacility, state.selectedPeriod);
  const fac = getFacilityById(state.selectedFacility);
  if (!kpis || !fac) return;

  const isDC = state.selectedFacility.startsWith('DC_');

  panel.classList.remove('hidden');
  panel.innerHTML = `
    <div class="card-title mb-md">Extended KPIs — ${fac.name}</div>
    <div class="grid-4" style="gap:var(--space-md)">
      <div style="padding:12px;background:var(--bg);border-radius:var(--r-md);border:1px solid var(--border-light)">
        <div class="text-xs text-muted" style="text-transform:uppercase;font-weight:600">Throughput</div>
        <div style="font-size:18px;font-weight:800;margin:4px 0">${formatNumber(fac.throughput)}</div>
        <div class="text-xs text-muted">units/day</div>
      </div>
      <div style="padding:12px;background:var(--bg);border-radius:var(--r-md);border:1px solid var(--border-light)">
        <div class="text-xs text-muted" style="text-transform:uppercase;font-weight:600">Capacity</div>
        <div style="font-size:18px;font-weight:800;margin:4px 0">${formatNumber(fac.capacity)}</div>
        <div class="text-xs text-muted">units/day</div>
      </div>
      <div style="padding:12px;background:var(--bg);border-radius:var(--r-md);border:1px solid var(--border-light)">
        <div class="text-xs text-muted" style="text-transform:uppercase;font-weight:600">Spare Capacity</div>
        <div style="font-size:18px;font-weight:800;margin:4px 0;color:var(--green)">${formatNumber(fac.capacity - fac.throughput)}</div>
        <div class="text-xs text-muted">units/day</div>
      </div>
      ${isDC ? `
      <div style="padding:12px;background:var(--bg);border-radius:var(--r-md);border:1px solid var(--border-light)">
        <div class="text-xs text-muted" style="text-transform:uppercase;font-weight:600">Handling Cost</div>
        <div style="font-size:18px;font-weight:800;margin:4px 0">₹${fac.handlingCost}</div>
        <div class="text-xs text-muted">per unit</div>
      </div>
      ` : `
      <div style="padding:12px;background:var(--bg);border-radius:var(--r-md);border:1px solid var(--border-light)">
        <div class="text-xs text-muted" style="text-transform:uppercase;font-weight:600">Region</div>
        <div style="font-size:18px;font-weight:800;margin:4px 0">${fac.region}</div>
        <div class="text-xs text-muted">${fac.state}</div>
      </div>
      `}
    </div>
  `;
}

// ─── Home Insights ──────────────────────────────────────────
function renderHomeInsights() {
  const list = document.getElementById('home-insight-list');
  if (!list) return;

  const insights = getInsightsForFacility(state.selectedFacility);

  list.innerHTML = insights.map(ins => {
    const severityClass = ins.impactColor === '#dc2626' ? 'severity-high'
      : ins.impactColor === '#d97706' ? 'severity-medium'
      : ins.impactColor === '#6B2FA0' ? 'severity-ai'
      : 'severity-low';

    return `
      <div class="home-insight-card ${severityClass}" data-insight-id="${ins.id}">
        <div class="insight-icon-wrap" style="background:${ins.iconBg}">${ins.icon}</div>
        <div class="insight-main">
          <div class="insight-main-title">${ins.title}</div>
          <div class="insight-main-sub">${ins.subtitle}</div>
          <span class="insight-impact-tag" style="background:${ins.impactColor}">${ins.impact}</span>
        </div>
        <div class="insight-why">
          <div class="insight-why-label">${ins.id === 'INS_RECOMMENDATION' ? 'Why I recommend this' : 'Why I found this'}</div>
          <div class="insight-why-text">${ins.why}</div>
        </div>
        <a class="insight-action-link" href="javascript:void(0)">${ins.action} →</a>
      </div>
    `;
  }).join('');

  // Wire click handlers
  list.querySelectorAll('.home-insight-card').forEach(card => {
    card.addEventListener('click', () => {
      const insightId = card.dataset.insightId;
      const insight = insights.find(i => i.id === insightId);
      if (!insight) return;

      if (insightId === 'INS_RECOMMENDATION') {
        // Navigate to recommendation tab
        document.querySelector('.nav-item[data-tab="recommend"]')?.click();
        renderRecommendation();
      } else if (insight.detail) {
        openInsightDrawer(insight);
      }
    });
  });
}

// ─── Insight Drawer ─────────────────────────────────────────
function openInsightDrawer(insight) {
  const overlay = document.getElementById('insight-drawer-overlay');
  const content = document.getElementById('insight-drawer-content');
  if (!overlay || !content) return;

  const d = insight.detail;

  content.innerHTML = `
    <h2 style="font-size:20px;font-weight:800;margin-bottom:4px">${insight.title}</h2>
    <span class="insight-impact-tag" style="background:${insight.impactColor}">${insight.impact}</span>

    <div class="drawer-section-title">What I Found</div>
    <p class="drawer-text">${d.whatIFound}</p>

    <div class="drawer-section-title">Why It Matters</div>
    <p class="drawer-text">${d.whyItMatters}</p>

    <div class="drawer-section-title">Evidence</div>
    ${d.evidence.map(e => `
      <div class="evidence-row">
        <span class="evidence-label">${e.label}</span>
        <span>
          <span class="evidence-value">${e.value}</span>
          <span class="provenance-badge ${e.provenance.toLowerCase().replace(/ /g, '-')}">${e.provenance}</span>
        </span>
      </div>
    `).join('')}

    ${d.whatITested.length > 0 ? `
      <div class="drawer-section-title">What I Tested</div>
      <ul style="font-size:13px;color:var(--text-2);padding-left:20px;line-height:2">
        ${d.whatITested.map(t => `<li>${t}</li>`).join('')}
      </ul>
    ` : ''}

    <div class="drawer-section-title">Recommendation</div>
    <p class="drawer-text" style="font-weight:600;color:var(--primary)">${d.recommendation}</p>

    <div class="drawer-section-title">Next Action</div>
    <button class="btn btn-primary mt-sm" id="drawer-next-action">${d.nextAction} →</button>
  `;

  overlay.classList.add('visible');

  // Wire next action button
  document.getElementById('drawer-next-action')?.addEventListener('click', () => {
    closeInsightDrawer();
    // Navigate to scenarios or recommendation
    const tab = d.nextAction.toLowerCase().includes('scenario') ? 'scenarios' : 'recommend';
    document.querySelector(`.nav-item[data-tab="${tab}"]`)?.click();
    if (tab === 'recommend') renderRecommendation();
  });
}

function closeInsightDrawer() {
  document.getElementById('insight-drawer-overlay')?.classList.remove('visible');
}

// ═══════════════════════════════════════════════════════════
// EXISTING TAB VIEWS (preserved from original prototype)
// ═══════════════════════════════════════════════════════════

// ─── Digital Twin Tables ────────────────────────────────────
function renderTwinTables() {
  // Plants
  const plantBody = document.querySelector('#table-plants tbody');
  if (plantBody) {
    plantBody.innerHTML = PLANTS.map(p => `
      <tr class="clickable-row" data-id="${p.id}">
        <td>${p.name}</td>
        <td class="num">${formatNumber(p.capacity)}</td>
        <td class="num">${formatNumber(p.throughput)}</td>
        <td><span class="tag tag-success">Active</span></td>
      </tr>
    `).join('');
  }

  // DCs
  const dcBody = document.querySelector('#table-dcs tbody');
  if (dcBody) {
    dcBody.innerHTML = DCS.map(d => {
      const color = getUtilColor(d.utilPct);
      const label = getUtilLabel(d.utilPct);
      const tagClass = d.utilPct >= 90 ? 'tag-danger' : d.utilPct >= 75 ? 'tag-warning' : 'tag-success';
      return `
        <tr class="clickable-row" data-id="${d.id}">
          <td>${d.name}</td>
          <td class="num">${formatNumber(d.capacity)}</td>
          <td class="num"><span style="color:${color};font-weight:700">${d.utilPct}%</span></td>
          <td><span class="tag ${tagClass}">${label}</span></td>
        </tr>
      `;
    }).join('');
  }

  // Markets
  const mktBody = document.querySelector('#table-markets tbody');
  if (mktBody) {
    mktBody.innerHTML = MARKETS.map(m => `
      <tr>
        <td>${m.name}</td>
        <td class="num">${formatNumber(m.demand)}</td>
        <td>${m.slaDays}d</td>
        <td><span class="tag ${m.priority === 'High' ? 'tag-danger' : m.priority === 'Medium' ? 'tag-warning' : 'tag-muted'}">${m.priority}</span></td>
      </tr>
    `).join('');
  }

  // Clickable rows to open facility panel
  document.querySelectorAll('.clickable-row').forEach(row => {
    row.style.cursor = 'pointer';
    row.addEventListener('click', () => openFacilityPanel(row.dataset.id));
  });
}

// ─── Facility Panel ─────────────────────────────────────────
window.openFacilityPanel = function(facilityId) {
  const fac = [...PLANTS, ...DCS].find(f => f.id === facilityId);
  if (!fac) return;

  const isPlant = fac.id.startsWith('PLT_');
  const isDC = fac.id.startsWith('DC_');
  const utilPct = isDC ? fac.utilPct : ((fac.throughput / fac.capacity) * 100).toFixed(1);
  const utilColor = getUtilColor(utilPct);
  const utilLabel = getUtilLabel(utilPct);

  const isBaddi = facilityId === 'DC_DELHI';
  const forecastSection = isBaddi ? `
    <div class="fp-stat" style="border-bottom:2px solid var(--red)">
      <span class="fp-stat-label">Forecast Dec'26</span>
      <span class="fp-stat-value" style="color:var(--red)">10,800 units/day</span>
    </div>
    <div class="fp-stat">
      <span class="fp-stat-label">Risk</span>
      <span class="fp-stat-value"><span class="tag tag-danger">HIGH — Capacity Breach</span></span>
    </div>
  ` : '';

  document.getElementById('fp-content').innerHTML = `
    <div class="fp-title">${fac.name}</div>
    <div class="fp-subtitle">${fac.city}, ${fac.state} · ${fac.region} Region</div>
    <div class="fp-stat"><span class="fp-stat-label">Type</span><span class="fp-stat-value">${isPlant ? 'Manufacturing Plant' : 'Distribution Centre'}</span></div>
    <div class="fp-stat"><span class="fp-stat-label">Status</span><span class="fp-stat-value"><span class="tag tag-success">Active</span></span></div>
    <div class="fp-stat"><span class="fp-stat-label">Capacity</span><span class="fp-stat-value">${formatNumber(fac.capacity)} units/day</span></div>
    <div class="fp-stat"><span class="fp-stat-label">Current Throughput</span><span class="fp-stat-value">${formatNumber(fac.throughput)} units/day</span></div>
    <div class="fp-stat"><span class="fp-stat-label">Utilisation</span><span class="fp-stat-value" style="color:${utilColor}">${utilPct}% <span class="tag ${utilPct >= 90 ? 'tag-danger' : utilPct >= 75 ? 'tag-warning' : 'tag-success'}">${utilLabel}</span></span></div>
    ${isDC ? `<div class="fp-stat"><span class="fp-stat-label">Fixed Cost</span><span class="fp-stat-value">₹${fac.fixedCost}L/year</span></div>` : ''}
    ${isDC ? `<div class="fp-stat"><span class="fp-stat-label">Handling Cost</span><span class="fp-stat-value">₹${fac.handlingCost}/unit</span></div>` : ''}
    ${forecastSection}
    <div style="margin-top:var(--space-lg)">
      <div class="card-title mb-md">Connected Lanes</div>
      ${LANES.filter(l => l.from === facilityId || l.to === facilityId).map(l => `
        <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border-light);font-size:12px">
          <span>${l.from === facilityId ? '→ ' + getFacilityById(l.to)?.name : '← ' + getFacilityById(l.from)?.name}</span>
          <span style="color:var(--text-3)">${formatNumber(l.flow)} u/d · ₹${l.cost}/u</span>
        </div>
      `).join('')}
    </div>
  `;

  document.getElementById('facility-panel').classList.add('open');
};

function closeFacilityPanel() {
  document.getElementById('facility-panel').classList.remove('open');
}

// ─── Recommendation Panel ───────────────────────────────────
function renderRecommendation() {
  const rec = RECOMMENDATION;
  const panel = document.getElementById('recommendation-panel');
  if (!panel) return;

  panel.innerHTML = `
    <div class="card rec-panel mb-lg">
      <div class="rec-title">${rec.title}</div>
      <div class="flex items-center gap-sm mb-md">
        <span class="tag tag-warning">Tier ${rec.tier} — PROPOSE</span>
        <span class="tag tag-primary">Human Approval Required</span>
      </div>

      <div class="impact-grid">
        <div class="impact-item">
          <div class="impact-label">Cost Change</div>
          <div class="impact-value positive">↓ ${Math.abs(rec.impact.costChange)}%</div>
        </div>
        <div class="impact-item">
          <div class="impact-label">SLA</div>
          <div class="impact-value neutral">${rec.impact.sla}%</div>
        </div>
        <div class="impact-item">
          <div class="impact-label">Peak Utilisation</div>
          <div class="impact-value positive">↓ ${Math.abs(rec.impact.peakUtilChange)}pp</div>
        </div>
        <div class="impact-item">
          <div class="impact-label">Carbon</div>
          <div class="impact-value positive">↓ ${Math.abs(rec.impact.carbonChange)}%</div>
        </div>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <div class="rec-subtitle" style="margin-top:0">What I found</div>
        <p class="text-sm" style="color:var(--text-2);line-height:1.7">${rec.evidence.whatIFound}</p>

        <div class="rec-subtitle">Why it matters</div>
        <p class="text-sm" style="color:var(--text-2);line-height:1.7">${rec.evidence.whyItMatters}</p>

        <div class="rec-subtitle">What I tested</div>
        <ol style="font-size:13px;color:var(--text-2);padding-left:20px;line-height:1.8">
          ${rec.evidence.whatITested.map(t => `<li>${t}</li>`).join('')}
        </ol>
      </div>

      <div class="card">
        <div class="rec-subtitle" style="margin-top:0">What I rejected</div>
        ${rec.evidence.whatIRejected.map(r => `
          <div class="rejected-item">
            <div class="rejected-name">✕ ${r.scenario}</div>
            <div class="rejected-reason">${r.reason}</div>
          </div>
        `).join('')}

        <div class="rec-subtitle">What could change the recommendation</div>
        <ul style="font-size:13px;color:var(--text-2);padding-left:20px;line-height:1.8">
          ${rec.evidence.whatCouldChange.map(c => `<li>${c}</li>`).join('')}
        </ul>
      </div>
    </div>

    <div class="card mt-lg">
      <div class="rec-subtitle" style="margin-top:0">What I need from you</div>
      ${rec.nextSteps.map(s => `
        <div class="next-step">
          <div class="step-number">${s.step}</div>
          <div class="step-content">
            <div class="step-action">${s.action}</div>
            <div class="step-owner">Owner: ${s.owner}</div>
          </div>
        </div>
      `).join('')}

      <div class="flex gap-sm mt-lg" style="flex-wrap:wrap">
        <button class="btn btn-success" id="btn-approve">✓ Approve</button>
        <button class="btn btn-secondary" id="btn-modify">Modify</button>
        <button class="btn btn-danger" id="btn-reject">✕ Reject</button>
        <button class="btn btn-secondary" id="btn-more-tests">Run More Tests</button>
        <button class="btn btn-primary" id="btn-gen-email">📧 Generate Analyst Email</button>
      </div>

      <div class="email-preview mt-lg" id="email-preview">${rec.analystEmail}</div>
    </div>
  `;

  // Wire action buttons
  document.getElementById('btn-approve')?.addEventListener('click', () => {
    document.getElementById('btn-approve').textContent = '✓ Approved';
    document.getElementById('btn-approve').disabled = true;
    document.getElementById('btn-approve').style.opacity = '0.6';
  });
  document.getElementById('btn-reject')?.addEventListener('click', () => {
    document.getElementById('btn-reject').textContent = '✕ Rejected';
    document.getElementById('btn-reject').disabled = true;
    document.getElementById('btn-reject').style.opacity = '0.6';
  });
  document.getElementById('btn-gen-email')?.addEventListener('click', () => {
    document.getElementById('email-preview').classList.toggle('visible');
  });
  document.getElementById('btn-more-tests')?.addEventListener('click', () => {
    document.querySelector('.nav-item[data-tab="scenarios"]')?.click();
  });
  document.getElementById('btn-modify')?.addEventListener('click', () => {
    document.querySelector('.nav-item[data-tab="scenarios"]')?.click();
  });
}

// ─── Data Intelligence ──────────────────────────────────────
function renderDataIntelligence() {
  // External signals (on Forecast tab now)
  const sigContainer = document.getElementById('external-signals');
  if (sigContainer) {
    sigContainer.innerHTML = EXTERNAL_SIGNALS.map(sig => `
      <div class="signal-card">
        <div class="flex items-center justify-between mb-sm">
          <span style="font-size:14px">${sig.icon} <strong>${sig.title}</strong></span>
          <span class="signal-badge">External signal</span>
        </div>
        <div class="text-xs text-muted">Source: ${sig.source} · ${sig.publishedDate}</div>
        <div class="text-sm mt-sm" style="color:var(--text-2)">${sig.rationale}</div>
        <div class="flex gap-sm mt-sm" style="flex-wrap:wrap">
          <span class="tag tag-muted">Geography: ${sig.geography}</span>
          <span class="tag tag-muted">Direction: ${sig.direction}</span>
          <span class="tag ${sig.confidence === 'HIGH' ? 'tag-success' : 'tag-warning'}">Conf: ${sig.confidence}</span>
        </div>
        <div class="text-xs mt-sm" style="color:var(--primary);font-weight:600">→ ${sig.intendedUse}</div>
      </div>
    `).join('');
  }
}

// ─── Notification Toast ─────────────────────────────────────
function showNotification(message) {
  const notif = document.createElement('div');
  notif.style.cssText = `
    position: fixed; bottom: 24px; right: 24px;
    background: #1a1a2e; color: white; padding: 14px 24px;
    border-radius: 10px; font-size: 13px; font-family: Inter, sans-serif;
    box-shadow: 0 8px 24px rgba(0,0,0,.2); z-index: 500;
    animation: fadeIn .3s ease;
  `;
  notif.textContent = message;
  document.body.appendChild(notif);
  setTimeout(() => {
    notif.style.opacity = '0';
    notif.style.transition = 'opacity .3s';
    setTimeout(() => notif.remove(), 300);
  }, 4000);
}
