/**
 * NetGravity — Main Application Controller
 * ==========================================
 * Tab routing, Home cockpit state, facility/period selectors,
 * KPI rendering, insight list, insight drawer, and all sub-views.
 */

import { PLANTS, DCS, MARKETS, LANES, DATA_QUALITY, SCHEMA_MAPPING,
         CONTRACT_DEMO, SYSTEM_STATUS, GOVERNANCE_TIERS, EXTERNAL_SIGNALS,
         AGENT_STATE, SCENARIOS, RECOMMENDATION, FORECAST, DEMAND_HISTORY,
         PERIODS, FACILITY_KPIS, HOME_INSIGHTS, HOME_ACTION_ITEMS,
         formatCurrency, formatNumber, getUtilColor, getUtilLabel,
         getFacilityById, getInsightsForFacility, getKpisForFacility } from './data.js';
import { initMap, setNetworkState, invalidateMapSize } from './map.js';
import { initTwin3D, setTwin3DState, resizeTwin3D, resumeTwin3D } from './twin3d.js';
import { renderForecastChart,
         renderFacilityThroughputChart, renderFacilityCostBreakdownChart, renderFacilityLaneFlowsChart } from './charts.js';
import { initScenarios } from './scenarios.js';
import { initAgent } from './agent.js';
import { initLandingPage } from './landing.js';
import { initInsightsPage } from './insights.js';
import { initAuth } from './auth.js';

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

// Expose globally on window
if (typeof window !== 'undefined') {
  window.navigateToTab = navigateToTab;
  window.openActionDrawer = openActionDrawer;
  window.closeActionDrawer = closeActionDrawer;
  window.renderHome = renderHome;
}

// ─── Boot ───────────────────────────────────────────────────
function bootApp() {
  try { initAuth(); } catch (e) { console.error('initAuth error:', e); }
  try { initLandingPage(); } catch (e) { console.error('initLandingPage error:', e); }
  try { initTabs(); } catch (e) { console.error('initTabs error:', e); }
  try { initHomeSelectors(); } catch (e) { console.error('initHomeSelectors error:', e); }
  try { renderHome(); } catch (e) { console.error('renderHome error:', e); }
  try { renderTwinTables(); } catch (e) { console.error('renderTwinTables error:', e); }
  try { initScenarios(); } catch (e) { console.error('initScenarios error:', e); }
  try { initAgent(); } catch (e) { console.error('initAgent error:', e); }
  try { initInsightsPage(); } catch (e) { console.error('initInsightsPage error:', e); }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootApp);
} else {
  bootApp();
}

// ─── Header & Topbar Visibility Controller ──────────────────
function updateTopBarLayout(tab) {
  const isHomeOverview = (tab === 'home' || tab === 'overview');

  // Upload Data button: ONLY on Home Overview page
  const btnUpload = document.getElementById('btn-topbar-upload');
  if (btnUpload) {
    btnUpload.style.display = isHomeOverview ? 'flex' : 'none';
  }

  // Sub-topbar Page Title in parallel with selectors across ALL pages
  const mainTitle = document.getElementById('sub-topbar-main-title');
  const subTitle = document.getElementById('sub-topbar-sub-title');

  if (mainTitle && subTitle) {
    if (tab === 'home' || tab === 'overview') {
      mainTitle.innerHTML = 'Hello, <strong id="logged-in-user-name">Amit Kumar</strong>';
      subTitle.textContent = '· Network Decision Command Center';
    } else if (tab === 'insights') {
      mainTitle.innerHTML = 'Insights';
      subTitle.textContent = '· AI-generated observations from your network';
    } else if (tab === 'facility-dashboard') {
      mainTitle.innerHTML = 'Facility KPIs & Analytics';
      subTitle.textContent = '· Telemetry & cost breakdown';
    } else if (tab === 'forecast') {
      mainTitle.innerHTML = 'Demand Forecast';
      subTitle.textContent = '· AI predictive projections';
    } else if (tab === 'twin') {
      mainTitle.innerHTML = 'Digital Twin';
      subTitle.textContent = '· India network topology';
    } else if (tab === 'scenarios') {
      mainTitle.innerHTML = 'Scenario Planning';
      subTitle.textContent = '· Multi-echelon network optimization';
    } else if (tab === 'recommendations' || tab === 'recommend') {
      mainTitle.innerHTML = 'Recommendations';
      subTitle.textContent = '· Prescriptive AI actions';
    }
  }

  // Facility & Period selectors: STAYS visible across all pages
  const controls = document.getElementById('topbar-controls');
  if (controls) {
    controls.style.display = 'flex';
  }
}

// ─── Tab Routing & Sub-Navigation ───────────────────────────
export function navigateToTab(tab) {
  updateTopBarLayout(tab);

  // 1. Insights Page (nested under Home)
  if (tab === 'insights') {
    const homeGroup = document.getElementById('nav-group-home');
    if (homeGroup) homeGroup.classList.add('expanded');

    document.querySelectorAll('.nav-item, .nav-item-expandable').forEach(n => n.classList.remove('active'));
    document.getElementById('nav-item-home')?.classList.add('active');

    document.querySelectorAll('.nav-sub-pill').forEach(p => p.classList.remove('active'));
    document.getElementById('nav-sub-insights')?.classList.add('active');

    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    const panel = document.getElementById('tab-insights');
    if (panel) panel.classList.add('active');

    state.activeTab = 'insights';
    try { initInsightsPage(); } catch (e) { console.error(e); }
    window.scrollTo({ top: 0, behavior: 'smooth' });
    return;
  }

  // 2. Recommendations Page (nested under Home)
  if (tab === 'recommendations' || tab === 'recommend') {
    const homeGroup = document.getElementById('nav-group-home');
    if (homeGroup) homeGroup.classList.add('expanded');

    document.querySelectorAll('.nav-item, .nav-item-expandable').forEach(n => n.classList.remove('active'));
    document.getElementById('nav-item-home')?.classList.add('active');

    document.querySelectorAll('.nav-sub-pill').forEach(p => p.classList.remove('active'));
    document.getElementById('nav-sub-recommendations')?.classList.add('active');

    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    const panel = document.getElementById('tab-recommendations') || document.getElementById('tab-recommend');
    if (panel) panel.classList.add('active');

    state.activeTab = 'recommendations';
    window.scrollTo({ top: 0, behavior: 'smooth' });
    return;
  }

  // 3. Home / Overview (nested under Home)
  if (tab === 'home' || tab === 'overview') {
    const homeGroup = document.getElementById('nav-group-home');
    if (homeGroup) homeGroup.classList.add('expanded');

    document.querySelectorAll('.nav-item, .nav-item-expandable').forEach(n => n.classList.remove('active'));
    document.getElementById('nav-item-home')?.classList.add('active');

    document.querySelectorAll('.nav-sub-pill').forEach(p => p.classList.remove('active'));
    document.getElementById('nav-sub-overview')?.classList.add('active');

    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.getElementById('tab-home')?.classList.add('active');

    state.activeTab = 'home';
    setTimeout(() => {
      renderHome();
      window.dispatchEvent(new Event('resize'));
    }, 50);
    window.scrollTo({ top: 0, behavior: 'smooth' });
    return;
  }

  // 4. Facility KPI Dashboard
  if (tab === 'facility-dashboard') {
    document.querySelectorAll('.nav-item, .nav-item-expandable').forEach(n => n.classList.remove('active'));
    document.getElementById('nav-item-kpis')?.classList.add('active');
    document.querySelectorAll('.nav-sub-pill').forEach(p => p.classList.remove('active'));

    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.getElementById('tab-facility-dashboard')?.classList.add('active');
    state.activeTab = 'facility-dashboard';
    renderFacilityDashboard();
    window.scrollTo({ top: 0, behavior: 'smooth' });
    return;
  }

  // 5. Other Top Tabs (Digital Twin, Scenario Planning, Forecasting)
  document.querySelectorAll('.nav-item, .nav-item-expandable').forEach(n => n.classList.remove('active'));
  document.querySelector(`.nav-item[data-tab="${tab}"]`)?.classList.add('active');
  document.querySelectorAll('.nav-sub-pill').forEach(p => p.classList.remove('active'));

  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  const panel = document.getElementById('tab-' + tab);
  if (panel) panel.classList.add('active');

  state.activeTab = tab;

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
        if (!state.mapsInitialised['map-twin']) {
          initMap('map-twin');
          state.mapsInitialised['map-twin'] = true;
        }
      } catch (err) {
        console.error('Twin initialization warning:', err);
      }
      window.dispatchEvent(new Event('resize'));
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
      invalidateMapSize('scenario-leaflet-map');
    }, 50);
  }
}

function initTabs() {
  // Primary nav items
  document.querySelectorAll('.nav-item[data-tab]').forEach(item => {
    item.addEventListener('click', () => {
      const tab = item.dataset.tab;
      navigateToTab(tab);
    });
  });

  // Expandable Home Menu Click
  const navItemHome = document.getElementById('nav-item-home');
  if (navItemHome) {
    navItemHome.addEventListener('click', (e) => {
      const homeGroup = document.getElementById('nav-group-home');
      if (homeGroup) {
        // Toggle expansion or navigate
        if (state.activeTab !== 'home' && state.activeTab !== 'insights' && state.activeTab !== 'recommendations') {
          navigateToTab('home');
        } else {
          homeGroup.classList.toggle('expanded');
        }
      }
    });
  }

  // Nested sub-pills under Home
  document.querySelectorAll('.nav-sub-pill[data-tab]').forEach(pill => {
    pill.addEventListener('click', (e) => {
      e.stopPropagation();
      const tab = pill.dataset.tab;
      navigateToTab(tab);
    });
  });

  // Home Page: Click on Insights Card or Items -> Navigate to Insights Page
  document.getElementById('home-insights-card')?.addEventListener('click', () => {
    navigateToTab('insights');
  });
  document.getElementById('home-insights-header')?.addEventListener('click', () => {
    navigateToTab('insights');
  });

  // Topbar Global Action Handlers
  document.getElementById('btn-topbar-upload')?.addEventListener('click', () => {
    alert('Upload Network Data\n\nSAP S/4HANA, Manhattan WMS, and Oracle OTM data pipelines are connected.\nSelect CSV/Excel freight matrix or ERP dump to sync.');
  });

  document.getElementById('btn-topbar-notifications')?.addEventListener('click', () => {
    alert('Notifications & System Alerts\n\n• Critical: Delhi NCR DC capacity at 94%\n• Opportunity: Kolkata DC spare volume absorption\n• Notice: Winter fog lead-time adjustments active');
  });

  document.getElementById('btn-topbar-help')?.addEventListener('click', () => {
    alert('Netgravity Help & Documentation\n\nAI Decision Intelligence for Logistics Networks.\n• Overview: Network telemetry & KPIs\n• Insights: AI-generated observations & diagnosis\n• Recommendations: Prescriptive optimization actions\n• Digital Twin: 2D/3D network topology\n• Scenarios: What-if decision planning');
  });

  document.getElementById('btn-topbar-profile')?.addEventListener('click', () => {
    alert('User Profile\n\nLogged in as: Amit Kumar\nRole: Lead Supply Chain Architect (Admin)\nOrganization: Kearney Decision Systems');
  });

  // 2D / 3D View Toggle (Digital Twin Tab)
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

  // Network state toggles (Digital Twin Tab)
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

  // Window resize handler for 3D canvas and maps
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

  // Action drawer close
  document.getElementById('action-drawer-close')?.addEventListener('click', closeActionDrawer);
  document.getElementById('action-drawer-overlay')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeActionDrawer();
  });

  // Back to Home from Facility Dashboard
  document.getElementById('btn-back-to-home')?.addEventListener('click', () => {
    navigateToTab('home');
  });
}

// ═══════════════════════════════════════════════════════════
// HOME — Decision Cockpit (2/3 + 1/3 Layout)
// ═══════════════════════════════════════════════════════════

// ─── Home Selectors ─────────────────────────────────────────
function initHomeSelectors() {
  const selPeriod = document.getElementById('sel-period');
  const selFacility = document.getElementById('sel-facility');
  const selKpiType = document.getElementById('home-kpi-type-select');
  const selForecastType = document.getElementById('home-forecast-type-select');
  const selFacilityPicker = document.getElementById('home-facility-picker');

  // Topbar facility selector
  if (selFacility) {
    selFacility.value = state.selectedFacility || 'DC_DELHI';
    selFacility.addEventListener('change', () => {
      state.selectedFacility = selFacility.value;
      if (selFacilityPicker) selFacilityPicker.value = state.selectedFacility;
      renderHome();
    });
  }

  // Populate global period selector in topbar
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

  // KPI Section "View by" (DC / Plant)
  if (selKpiType) {
    selKpiType.value = state.facilityType;
    selKpiType.addEventListener('change', () => {
      state.facilityType = selKpiType.value;
      if (selForecastType) selForecastType.value = state.facilityType;
      populateFacilitySelector();
      renderHome();
    });
  }

  // Forecast Section "View by" (DC / Plant)
  if (selForecastType) {
    selForecastType.value = state.facilityType;
    selForecastType.addEventListener('change', () => {
      state.facilityType = selForecastType.value;
      if (selKpiType) selKpiType.value = state.facilityType;
      populateFacilitySelector();
      renderHome();
    });
  }

  // Facility Picker (if visible/available)
  if (selFacilityPicker) {
    selFacilityPicker.addEventListener('change', () => {
      state.selectedFacility = selFacilityPicker.value;
      renderHome();
    });
  }

  populateFacilitySelector();

  // Click entire KPI Block or "View more KPIs →"
  document.getElementById('home-kpi-block')?.addEventListener('click', () => {
    navigateToTab('facility-dashboard');
  });
  document.getElementById('btn-view-more-kpis')?.addEventListener('click', (e) => {
    e.stopPropagation();
    navigateToTab('facility-dashboard');
  });

  // Click entire Forecast Block or "View more details (open Forecasting) →"
  document.getElementById('home-forecast-block')?.addEventListener('click', () => {
    navigateToTab('forecast');
  });
  document.getElementById('btn-view-forecast-details')?.addEventListener('click', (e) => {
    e.stopPropagation();
    navigateToTab('forecast');
  });

  // Click entire Digital Twin map block → Navigate to Digital Twin
  document.getElementById('home-twin-block')?.addEventListener('click', () => {
    navigateToTab('twin');
  });

  // Chatbot Send Button & Enter Key
  document.getElementById('home-chat-send')?.addEventListener('click', handleHomeChat);
  document.getElementById('home-chat-input')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') handleHomeChat();
  });
}

function populateFacilitySelector() {
  const selFacilityPicker = document.getElementById('home-facility-picker');
  const facilities = state.facilityType === 'DC' ? DCS : PLANTS;

  if (selFacilityPicker) {
    selFacilityPicker.innerHTML = facilities.map(f =>
      `<option value="${f.id}">${f.name}</option>`
    ).join('');
    if (!state.selectedFacility || !facilities.some(f => f.id === state.selectedFacility)) {
      state.selectedFacility = facilities[0].id;
    }
    selFacilityPicker.value = state.selectedFacility;
  } else {
    if (!state.selectedFacility || !facilities.some(f => f.id === state.selectedFacility)) {
      state.selectedFacility = facilities[0].id;
    }
  }
}

// ─── Render Full Home ───────────────────────────────────────
function renderHome() {
  renderHomeKPIs();
  renderHomeForecast();
  renderHomeDigitalTwin();
  renderHomeInsights();
  renderHomeActions();
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
  const elFacName = document.getElementById('dash-facility-name');
  if (elFacName) elFacName.textContent = fac.name;
  const elFacType = document.getElementById('dash-facility-type');
  if (elFacType) elFacType.textContent = isDC ? 'Distribution Centre' : 'Manufacturing Plant';
  const elPeriodLabel = document.getElementById('dash-period-label');
  if (elPeriodLabel) elPeriodLabel.textContent = period ? period.label : 'August 2026';
  
  const dashDot = document.getElementById('dash-facility-dot');
  if (dashDot) {
    dashDot.style.background = utilPct >= 90 ? 'var(--red)' : utilPct >= 75 ? 'var(--amber)' : 'var(--green)';
  }

  const elDashTitle = document.getElementById('dash-title');
  if (elDashTitle) elDashTitle.textContent = `${fac.name} — Performance & Analytics`;
  const elDashSubtitle = document.getElementById('dash-subtitle');
  if (elDashSubtitle) elDashSubtitle.textContent = `${fac.city}, ${fac.state} · ${fac.region} Region · Full operational telemetry and cost breakdown for ${period ? period.short : 'Aug 2026'}.`;

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


// ─── Home KPI Cards (2x2 Compact Grid) ───────────────────────
function renderHomeKPIs() {
  const kpis = getKpisForFacility(state.selectedFacility, state.selectedPeriod);
  const grid = document.getElementById('home-kpi-grid');
  if (!kpis || !grid) return;

  grid.innerHTML = `
    <!-- Card 1: Capacity Utilisation -->
    <div class="home-kpi-item">
      <div class="home-kpi-item-header">
        <div class="home-kpi-name">Capacity Utilisation</div>
        <div class="home-kpi-icon-badge" style="background:#f5f0fa;color:var(--primary)">📊</div>
      </div>
      <div class="home-kpi-val">${kpis.utilisation.value}%</div>
      <div class="home-kpi-sub">of ${formatNumber(kpis.utilisation.capacity)} ${kpis.utilisation.unit}</div>
    </div>

    <!-- Card 2: On-time Service (SLA) -->
    <div class="home-kpi-item">
      <div class="home-kpi-item-header">
        <div class="home-kpi-name">On-time Service (SLA)</div>
        <div class="home-kpi-icon-badge" style="background:#f0fdf4;color:var(--green)">✅</div>
      </div>
      <div class="home-kpi-val">${kpis.sla.value}%</div>
      <div class="home-kpi-sub">Target: ≥${kpis.sla.target}%</div>
    </div>

    <!-- Card 3: Total Cost -->
    <div class="home-kpi-item">
      <div class="home-kpi-item-header">
        <div class="home-kpi-name">Total Cost</div>
        <div class="home-kpi-icon-badge" style="background:#f5f0fa;color:var(--primary);font-weight:700">₹</div>
      </div>
      <div class="home-kpi-val">${formatCurrency(kpis.totalCost.value)}</div>
      <div class="home-kpi-sub">Total cost for period</div>
    </div>

    <!-- Card 4: Inventory Days -->
    <div class="home-kpi-item">
      <div class="home-kpi-item-header">
        <div class="home-kpi-name">Inventory Days</div>
        <div class="home-kpi-icon-badge" style="background:#fffbeb;color:var(--amber)">📦</div>
      </div>
      <div class="home-kpi-val">${kpis.inventoryDays.value} days</div>
      <div class="home-kpi-sub">of supply</div>
    </div>
  `;
}

// ─── Home Forecast Section ──────────────────────────────────
function renderHomeForecast() {
  const banner = document.getElementById('home-forecast-banner');
  if (banner) {
    banner.textContent = 'I forecast North India demand to increase 14% over the next 3 months.';
  }

  // Render compact forecast chart
  setTimeout(() => {
    renderForecastChart('chart-forecast-home');
  }, 40);
}

// ─── Home Digital Twin Map Preview ──────────────────────────
function renderHomeDigitalTwin() {
  setTimeout(() => {
    try {
      if (!state.mapsInitialised['home-map-twin']) {
        initMap('home-map-twin');
        state.mapsInitialised['home-map-twin'] = true;
      } else {
        window.dispatchEvent(new Event('resize'));
      }
    } catch (e) {
      console.warn('Home map init:', e);
    }
  }, 50);
}

// ─── Home Numbered Insights (Right Rail) ─────────────────────
function renderHomeInsights() {
  const list = document.getElementById('home-insights-list');
  if (!list) return;

  const insights = getInsightsForFacility(state.selectedFacility);

  list.innerHTML = insights.map((ins, idx) => {
    const num = ins.num || (idx + 1);
    const actionText = ins.action || 'view details →';
    return `
      <div class="home-insight-row" data-insight-id="${ins.id}" onclick="window.openInsightDrawer && window.openInsightDrawer('${ins.id}')" style="cursor:pointer;" title="Click to view insight details">
        <div class="insight-row-left">
          <span class="insight-num-badge">${num}</span>
          <span>${ins.title}</span>
          <span class="insight-info-icon">ⓘ</span>
        </div>
        ${actionText ? `<span class="insight-overlay-hint">${actionText}</span>` : ''}
      </div>
    `;
  }).join('');

  // Wire View More Insights Button
  const btnViewMore = document.getElementById('btn-view-more-insights');
  if (btnViewMore) {
    btnViewMore.onclick = (e) => {
      e.stopPropagation();
      navigateToTab('insights');
    };
  }
}

// ─── Home Recommendations (Right Rail) ────────────────────────
function renderHomeActions() {
  const list = document.getElementById('home-actions-list') || document.getElementById('home-recommendations-list');
  if (!list) return;

  list.innerHTML = HOME_ACTION_ITEMS.map(act => `
    <div class="home-action-row" data-action-id="${act.id}" onclick="window.openActionDrawer && window.openActionDrawer('${act.id}')" style="cursor:pointer;" title="Click to view recommendation details">
      <div class="home-action-checkbox" style="color:#9218EA;font-weight:700">✦</div>
      <div class="home-action-text">
        ${act.title}
        <span class="home-action-tag" style="color:${act.tagColor}">(${act.tag})</span>
      </div>
    </div>
  `).join('');

  // Wire click handlers as well
  list.querySelectorAll('.home-action-row').forEach(row => {
    row.addEventListener('click', (e) => {
      e.stopPropagation();
      const actionId = row.dataset.actionId;
      if (typeof window.openActionDrawer === 'function') {
        window.openActionDrawer(actionId);
      }
    });
  });

  // Wire View More Recommendations Button
  const btnViewMoreRec = document.getElementById('btn-view-more-recommendations');
  if (btnViewMoreRec) {
    btnViewMoreRec.onclick = (e) => {
      e.stopPropagation();
      navigateToTab('recommendations');
    };
  }
}

// ─── Recommendation Detail Drawer ───────────────────────────
export function openActionDrawer(actionOrId) {
  const overlay = document.getElementById('action-drawer-overlay');
  const content = document.getElementById('action-drawer-content');
  if (!overlay || !content) return;

  let action = null;
  if (typeof actionOrId === 'object' && actionOrId !== null) {
    action = actionOrId;
  } else if (typeof actionOrId === 'string' && typeof HOME_ACTION_ITEMS !== 'undefined') {
    action = HOME_ACTION_ITEMS.find(a => a.id === actionOrId);
  }
  if (!action && typeof HOME_ACTION_ITEMS !== 'undefined' && HOME_ACTION_ITEMS.length > 0) {
    action = HOME_ACTION_ITEMS[0];
  }
  if (!action) return;

  content.innerHTML = `
    <div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:12px">
      <span class="provenance-badge ai-assessment" style="padding:4px 10px;border-radius:12px;font-size:11px;font-weight:700;background:#f5f3ff;color:#9218EA;">AI RECOMMENDATION</span>
      <button class="facility-panel-close" onclick="window.closeActionDrawer && window.closeActionDrawer()" style="background:none;border:none;font-size:22px;color:#9ca3af;cursor:pointer;padding:4px;line-height:1;">✕</button>
    </div>

    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
      <h2 style="font-size:20px;font-weight:800;margin:0;color:#111827;">${action.title}</h2>
      <span class="tag" style="background:#faf5ff;color:${action.tagColor};font-weight:700;padding:4px 8px;border-radius:6px;font-size:11px;">${action.tag}</span>
    </div>

    <div style="background:#fafafc;border:1px solid #f0f0f5;border-radius:12px;padding:14px 16px;margin-bottom:16px;">
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;color:#9218EA;margin-bottom:6px;letter-spacing:0.05em;">Why am I recommending this?</div>
      <p style="font-size:13px;line-height:1.6;color:#374151;margin:0;">${action.why}</p>
    </div>

    <div style="margin-bottom:16px;">
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;color:#6b7280;margin-bottom:8px;letter-spacing:0.05em;">Root Cause & Network Telemetry</div>
      <div style="display:flex;flex-direction:column;gap:8px;">
        ${action.rootCause ? action.rootCause.map(r => `
          <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:#ffffff;border:1px solid #eef0f3;border-radius:8px;">
            <span style="font-size:12.5px;color:#6b7280;">${r.label}</span>
            <div style="display:flex;align-items:center;gap:6px;">
              <span style="font-size:13px;font-weight:700;color:#111827;">${r.value}</span>
              <span class="provenance-badge ${r.provenance.toLowerCase().replace(/ /g, '-')}" style="font-size:10px;padding:2px 6px;border-radius:4px;background:#f3f4f6;color:#6b7280;">${r.provenance}</span>
            </div>
          </div>
        `).join('') : ''}
      </div>
    </div>

    ${action.expectedImpact ? `
      <div style="margin-bottom:16px;">
        <div style="font-size:11px;font-weight:700;text-transform:uppercase;color:#6b7280;margin-bottom:8px;letter-spacing:0.05em;">Expected Impact</div>
        <div style="display:grid;grid-template-columns:repeat(3, 1fr);gap:10px;">
          <div style="background:#f0fdf4;border:1px solid #dcfce7;border-radius:10px;padding:12px 10px;text-align:center">
            <div style="font-size:11px;color:#166534;font-weight:600;text-transform:uppercase">Cost</div>
            <div style="font-size:16px;font-weight:800;color:#15803d;margin-top:2px">${action.expectedImpact.cost}</div>
          </div>
          <div style="background:#f0fdf4;border:1px solid #dcfce7;border-radius:10px;padding:12px 10px;text-align:center">
            <div style="font-size:11px;color:#166534;font-weight:600;text-transform:uppercase">SLA</div>
            <div style="font-size:16px;font-weight:800;color:#15803d;margin-top:2px">${action.expectedImpact.sla}</div>
          </div>
          <div style="background:#f0fdf4;border:1px solid #dcfce7;border-radius:10px;padding:12px 10px;text-align:center">
            <div style="font-size:11px;color:#166534;font-weight:600;text-transform:uppercase">Capacity Risk</div>
            <div style="font-size:16px;font-weight:800;color:#15803d;margin-top:2px">${action.expectedImpact.risk}</div>
          </div>
        </div>
      </div>
    ` : ''}

    <div style="display:flex;flex-direction:column;gap:10px;margin-top:auto;padding-top:16px;">
      <button class="btn-primary" onclick="window.navigateToTab && window.navigateToTab('scenarios'); window.closeActionDrawer && window.closeActionDrawer();" style="width:100%;padding:12px;border-radius:8px;background:#9218EA;color:#fff;border:none;font-weight:600;font-size:13px;cursor:pointer;box-shadow:0 2px 8px rgba(146,24,234,0.25);">
        Simulate in Scenario Planner →
      </button>
      <button onclick="window.navigateToTab && window.navigateToTab('recommendations'); window.closeActionDrawer && window.closeActionDrawer();" style="width:100%;padding:9px;border-radius:8px;background:#f9fafb;color:#374151;border:1px solid #e5e7eb;font-weight:600;font-size:12.5px;cursor:pointer;">
        View All Recommendations →
      </button>
    </div>
  `;

  overlay.classList.add('active');
  overlay.classList.add('visible');
  overlay.style.display = 'flex';
}

export function closeActionDrawer() {
  const overlay = document.getElementById('action-drawer-overlay');
  if (overlay) {
    overlay.classList.remove('active');
    overlay.classList.remove('visible');
    overlay.style.display = 'none';
  }
}
// ─── Contextual Home Chatbot ────────────────────────────────
function handleHomeChat() {
  const input = document.getElementById('home-chat-input');
  const messages = document.getElementById('home-chat-messages');
  if (!input || !input.value.trim()) return;

  const query = input.value.trim();
  input.value = '';

  if (messages) {
    messages.style.display = 'block';
    messages.innerHTML += `<div class="home-chat-msg user">You: ${query}</div>`;

    // Contextual intelligent responses based on active facility and query
    let responseText = '';
    const qLower = query.toLowerCase();

    if (qLower.includes('delhi') || qLower.includes('risk') || qLower.includes('capacity')) {
      responseText = `Delhi NCR DC is operating at 94% utilisation. With demand projected to surge +14.2% by December (108% peak utilisation), NetGravity recommends flow rebalancing to Kolkata DC to avoid bottlenecks. You can explore this intervention under <strong>Scenario Planning</strong>.`;
    } else if (qLower.includes('kolkata') || qLower.includes('spare') || qLower.includes('underutilised')) {
      responseText = `Kolkata DC has 41% spare capacity (2,800 units/day) with the lowest handling cost in the network (₹3.5/unit). It can absorb 800–1,200 units/day from Baddi manufacturing to alleviate Northern corridor pressure.`;
    } else if (qLower.includes('forecast') || qLower.includes('demand') || qLower.includes('surge')) {
      responseText = `North India regional demand is forecast to grow 14% over the next 3 months, crossing the 10,000 units/day DC ceiling by October. View complete confidence bands in the <strong>Forecasting</strong> tab.`;
    } else if (qLower.includes('cost') || qLower.includes('save') || qLower.includes('rebalance')) {
      responseText = `The recommended flow rebalancing scenario reduces total operating cost by <strong>7.8% (₹8.4L/month)</strong> while preserving on-time delivery SLA at <strong>96.7%</strong>. Review the scenario comparison under <strong>Scenario Planning</strong>.`;
    } else {
      responseText = `Based on current network telemetry for ${state.selectedPeriod}, Delhi NCR is operating near capacity ceiling while Kolkata has significant headroom. Would you like to review the flow rebalancing scenario in <strong>Scenario Planning</strong>?`;
    }

    setTimeout(() => {
      messages.innerHTML += `<div class="home-chat-msg bot">🤖 NetGravity: ${responseText}</div>`;
      messages.scrollTop = messages.scrollHeight;
    }, 250);
  }
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
      <span class="fp-stat-label">Forecast Dec 2026</span>
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
