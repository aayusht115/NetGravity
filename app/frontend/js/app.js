/**
 * NetGravity — Main Application Controller
 * ==========================================
 * Tab routing, Home cockpit state, facility/period selectors,
 * KPI rendering, insight list, insight drawer, and all sub-views.
 */

import { PLANTS, DCS, MARKETS, LANES, EXTERNAL_SIGNALS,
         RECOMMENDATION, PERIODS, HOME_ACTION_ITEMS, FACILITY_KPIS,
         formatCurrency, formatNumber, getUtilColor, getUtilLabel,
         getFacilityById, getInsightsForFacility, getKpisForFacility } from './data.js';
import { initMap, setNetworkState, invalidateMapSize } from './map.js';
import { initTwin3D, setTwin3DState, resizeTwin3D, resumeTwin3D } from './twin3d.js';
import { renderForecastChart,
         renderFacilityThroughputChart, renderFacilityCostBreakdownChart, renderFacilityLaneFlowsChart } from './charts.js';
import { initScenarios } from './scenarios.js';
import { initAgent } from './agent.js';
import { initLandingPage } from './landing.js';
import { initInsightDetail } from './insight-detail.js';
import { initAuth } from './auth.js';
import { initProjects } from './projects.js';
import { initIngestion } from './ingestion.js';
import { initChatbot } from './chatbot.js';
import { triggerAgentReasoning } from './agent-reasoning.js';

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
  window.closeActionDrawer = closeActionDrawer;
  window.renderHome = renderHome;
}

// ─── Boot ───────────────────────────────────────────────────
function bootApp() {
  try { initProjects(); } catch (e) { console.error('initProjects error:', e); }
  try { initIngestion(); } catch (e) { console.error('initIngestion error:', e); }
  try { initAuth();
  initChatbot(); } catch (e) { console.error('initAuth error:', e); }
  try { initLandingPage(); } catch (e) { console.error('initLandingPage error:', e); }
  try { initTabs(); } catch (e) { console.error('initTabs error:', e); }
  try { initSidebarCollapse(); } catch (e) { console.error('initSidebarCollapse error:', e); }
  try { initHomeSelectors(); } catch (e) { console.error('initHomeSelectors error:', e); }
  try { renderHome(); } catch (e) { console.error('renderHome error:', e); }
  try { renderTwinTables(); } catch (e) { console.error('renderTwinTables error:', e); }
  try { initScenarios(); } catch (e) { console.error('initScenarios error:', e); }
  try { initAgent(); } catch (e) { console.error('initAgent error:', e); }
  try { initInsightDetail(); } catch (e) { console.error('initInsightDetail error:', e); }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootApp);
} else {
  bootApp();
}

// ─── Header & Topbar Visibility Controller ──────────────────
function updateTopBarLayout(tab) {
  const isHomeOverview = (tab === 'home' || tab === 'overview');

  // Upload Data button: ONLY on Home Overview page (unchanged from
  // before the redesign). Launches the same ingestion flow used during
  // onboarding — see initTabs' btn-topbar-upload handler.
  const btnUpload = document.getElementById('btn-topbar-upload');
  if (btnUpload) {
    btnUpload.style.display = isHomeOverview ? 'flex' : 'none';
  }

  // Facility/Period controls: live in the global topbar's left area only
  // on Home Overview; every other tab keeps its copy in the sub-topbar row.
  const homeTopControls = document.getElementById('home-top-controls');
  if (homeTopControls) {
    homeTopControls.style.display = isHomeOverview ? 'flex' : 'none';
  }

  // Home's redesign (Dump/Home Overview-updated.png) has no generic
  // greeting/selector row — its own headers (attention feed, Digital
  // Twin card) carry that context instead, and the Digital Twin card has
  // its own Facility/Period controls. Every other page keeps this row.
  const subTopbar = document.getElementById('app-sub-topbar');
  if (subTopbar) {
    subTopbar.style.display = isHomeOverview ? 'none' : 'flex';
  }
  if (isHomeOverview) return;

  // Sub-topbar Page Title in parallel with selectors across other pages
  const mainTitle = document.getElementById('sub-topbar-main-title');
  const subTitle = document.getElementById('sub-topbar-sub-title');

  if (mainTitle && subTitle) {
    if (tab === 'insights') {
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

  // Facility & Period selectors: STAYS visible across all non-Home pages
  const controls = document.getElementById('topbar-controls');
  if (controls) {
    controls.style.display = 'flex';
  }
}

// ─── Tab Routing & Sub-Navigation ───────────────────────────
export function navigateToTab(tab) {
  updateTopBarLayout(tab);

  // Sidebar is flat (Home, KPIs, Digital Twin, Forecast, Scenario
  // Planning). Insights/Recommendations pages are gone — an insight card
  // on Home opens a full-page deep dive instead (see insight-detail.js),
  // which is not itself a sidebar destination and manages its own nav
  // highlighting/panel display independent of this function.
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const navKey = (tab === 'overview') ? 'home' : tab;
  document.querySelector(`.nav-item[data-tab="${navKey}"]`)?.classList.add('active');

  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));

  // 1. Home / Overview
  if (tab === 'home' || tab === 'overview') {
    document.getElementById('tab-home')?.classList.add('active');
    state.activeTab = 'home';
    setTimeout(() => {
      renderHome();
      window.dispatchEvent(new Event('resize'));
    }, 50);
    window.scrollTo({ top: 0, behavior: 'smooth' });
    return;
  }

  // 2. Facility KPI Dashboard
  if (tab === 'facility-dashboard') {
    document.getElementById('tab-facility-dashboard')?.classList.add('active');
    state.activeTab = 'facility-dashboard';
    renderFacilityDashboard();
    window.scrollTo({ top: 0, behavior: 'smooth' });
    return;
  }

  // 3. Other Top Tabs (Digital Twin, Scenario Planning, Forecasting)
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

// ─── Sidebar Collapse (icon rail <-> full labels) ────────────
// Defaults to collapsed (see the .sidebar.collapsed class already on the
// element in index.html), matching Dump/Home Overview-updated.png. The
// choice is remembered per-browser so a reload doesn't reset it.
function initSidebarCollapse() {
  const sidebar = document.getElementById('sidebar');
  const toggleBtn = document.getElementById('sidebar-toggle-btn');
  if (!sidebar || !toggleBtn) return;

  let collapsed = true;
  try {
    const saved = window.localStorage.getItem('ng_sidebar_collapsed');
    if (saved !== null) collapsed = saved === '1';
  } catch (e) { /* localStorage unavailable (private mode, etc.) — keep default */ }

  function apply() {
    sidebar.classList.toggle('collapsed', collapsed);
    // Width is also set explicitly here, not left to .sidebar.collapsed's
    // CSS alone: #sidebar is a non-shrinking flex item with position:sticky,
    // and that combination has proven unreliable to re-measure after a
    // pure class-based width change (observed via headless verification —
    // same class of engine quirk as the rAF/CSS-transition issues on the
    // ingestion loading pop-up). Setting it directly here is deterministic
    // regardless of that.
    sidebar.style.width = collapsed ? '76px' : 'var(--sidebar-w)';
    void sidebar.offsetWidth; // force a synchronous layout flush
    toggleBtn.title = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
  }
  apply();

  toggleBtn.addEventListener('click', () => {
    collapsed = !collapsed;
    apply();
    try { window.localStorage.setItem('ng_sidebar_collapsed', collapsed ? '1' : '0'); } catch (e) { /* ignore */ }
  });
}

function initTabs() {
  // Primary nav items (flat: Home, KPIs, Digital Twin, Forecast, Scenario
  // Planning — see navigateToTab for how Insights/Recommendations, which
  // no longer have their own sidebar entry, still route correctly).
  document.querySelectorAll('.nav-item[data-tab]').forEach(item => {
    item.addEventListener('click', () => {
      const tab = item.dataset.tab;
      navigateToTab(tab);
    });
  });

  // Sidebar logo lockup: returns to Home, not the landing page — this is
  // only ever visible once the user is signed in.
  document.getElementById('sidebar-brand-lockup')?.addEventListener('click', () => {
    navigateToTab('home');
  });

  // Topbar Global Action Handlers

  // Upload Data: same ingestion flow used during onboarding
  // (upload -> AI loading -> Excel/PDF mapping -> network loading -> Home).
  // See js/ingestion.js and js/projects.js (getCurrentProject).
  document.getElementById('btn-topbar-upload')?.addEventListener('click', () => {
    if (typeof window.showUploadData === 'function') {
      const project = typeof window.getCurrentProject === 'function' ? window.getCurrentProject() : null;
      window.showUploadData(project);
    }
  });

  // Home attention feed: manual refresh (re-renders Home in place)
  document.getElementById('home2-refresh-btn')?.addEventListener('click', (e) => {
    const btn = e.currentTarget;
    btn.classList.add('spinning');
    setTimeout(() => btn.classList.remove('spinning'), 650);
    const t = document.getElementById('home2-refresh-time');
    if (t) t.textContent = 'Just now';
    renderHome();
  });

  document.getElementById('btn-topbar-notifications')?.addEventListener('click', () => {
    alert('Notifications & System Alerts\n\n• Critical: Delhi NCR DC capacity at 94%\n• Opportunity: Kolkata DC spare volume absorption\n• Notice: Winter fog lead-time adjustments active');
  });

  document.getElementById('btn-topbar-help')?.addEventListener('click', () => {
    alert('Netgravity Help & Documentation\n\nAI Decision Intelligence for Logistics Networks.\n• Overview: Network telemetry & KPIs\n• Insights: AI-generated observations & diagnosis\n• Recommendations: Prescriptive optimization actions\n• Digital Twin: 2D/3D network topology\n• Scenarios: What-if decision planning');
  });

  // Profile menu: Profile (stub) / Sign out (back to landing sign-in)
  document.getElementById('btn-topbar-profile')?.addEventListener('click', (e) => {
    e.stopPropagation();
    document.getElementById('profile-dropdown-menu')?.classList.toggle('open');
  });
  document.addEventListener('click', () => {
    document.getElementById('profile-dropdown-menu')?.classList.remove('open');
  });
  document.getElementById('profile-menu-profile')?.addEventListener('click', () => {
    document.getElementById('profile-dropdown-menu')?.classList.remove('open');
    alert('User Profile\n\nLogged in as: Amit Kumar\nRole: Lead Supply Chain Architect (Admin)\nOrganization: Kearney Decision Systems');
  });
  document.getElementById('profile-menu-signout')?.addEventListener('click', () => {
    document.getElementById('profile-dropdown-menu')?.classList.remove('open');
    if (typeof window.returnToLanding === 'function') window.returnToLanding();
  });

  // "Current Project" pill: opens Select Project so the user can switch
  // or create a project mid-session.
  document.getElementById('project-select-btn')?.addEventListener('click', () => {
    if (typeof window.showSelectProject === 'function') window.showSelectProject();
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

  // Action drawer close (still used by scenarios.js's own detail drawer)
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
      if (homeTopFacility) homeTopFacility.value = state.selectedFacility;
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
      if (homeTopPeriod) homeTopPeriod.value = state.selectedPeriod;
      renderHome();
    });
  }

  // Home Overview's own Facility/Period controls, relocated into the
  // global topbar's left area (see updateTopBarLayout — the generic
  // sub-topbar row is hidden on this tab). Kept in sync with the same
  // global state as every other tab's selectors.
  const homeTopFacility = document.getElementById('home-top-facility');
  if (homeTopFacility) {
    homeTopFacility.value = state.selectedFacility || 'DC_DELHI';
    homeTopFacility.addEventListener('change', () => {
      state.selectedFacility = homeTopFacility.value;
      if (selFacility) selFacility.value = state.selectedFacility;
      if (selFacilityPicker) selFacilityPicker.value = state.selectedFacility;
      renderHome();
    });
  }

  const homeTopPeriod = document.getElementById('home-top-period');
  if (homeTopPeriod) {
    homeTopPeriod.innerHTML = (PERIODS || []).map(p =>
      `<option value="${p.id}">${p.label}</option>`
    ).join('');
    homeTopPeriod.value = state.selectedPeriod;

    homeTopPeriod.addEventListener('change', () => {
      state.selectedPeriod = homeTopPeriod.value;
      if (selPeriod) selPeriod.value = state.selectedPeriod;
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
  renderHomeAttentionFeed();
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


// ─── Network-wide KPI aggregation (Home Overview only) ───────
// Home's redesign (Dump/Home Overview-updated.png) shows a whole-network
// pulse, not a single selected facility — that per-facility breakdown is
// what the KPIs tab is for. This averages/sums across every DC's
// FACILITY_KPIS entry for the chosen period.
function getNetworkKpis(periodId) {
  let rows = DCS
    .map(d => (FACILITY_KPIS[d.id] || {})[periodId])
    .filter(Boolean);
  // Not every period in PERIODS has mock data for every facility yet —
  // fall back the same way getKpisForFacility does.
  if (!rows.length) {
    rows = DCS.map(d => (FACILITY_KPIS[d.id] || {}).AUG_2026).filter(Boolean);
  }
  if (!rows.length) return null;

  const avg = sel => rows.reduce((s, r) => s + sel(r), 0) / rows.length;
  const sum = sel => rows.reduce((s, r) => s + sel(r), 0);

  return {
    utilisation: { value: +avg(r => r.utilisation.value).toFixed(1), prev: +avg(r => r.utilisation.prev).toFixed(1) },
    sla: { value: +avg(r => r.sla.value).toFixed(1), prev: +avg(r => r.sla.prev).toFixed(1), target: rows[0].sla.target },
    totalCost: { value: sum(r => r.totalCost.value), prev: sum(r => r.totalCost.prev) },
    inventoryDays: { value: +avg(r => r.inventoryDays.value).toFixed(1), prev: +avg(r => r.inventoryDays.prev).toFixed(1) },
  };
}

function pctDelta(value, prev) {
  if (!prev) return 0;
  return ((value - prev) / prev) * 100;
}

// Lower-is-better metrics (cost, inventory days): a drop is "Good", a
// rise is flagged by how large it is. Tone names match the existing
// tag-success/tag-warning/tag-danger palette used across the app.
function deltaStatus(pct) {
  if (pct <= 0) return { tone: Math.abs(pct) < 1 ? 'gray' : 'green', label: Math.abs(pct) < 1 ? 'Stable' : 'Good' };
  if (pct > 6) return { tone: 'red', label: 'High' };
  return { tone: 'amber', label: 'Medium' };
}

function slaStatus(value, target) {
  if (value >= target) return { tone: 'green', label: 'Good' };
  if (value >= target - 2) return { tone: 'amber', label: 'Watch' };
  return { tone: 'red', label: 'Below Target' };
}

function utilTone(label) {
  return label === 'Critical' ? 'red' : label === 'Moderate' ? 'amber' : 'green';
}

function fmtDelta(pct) {
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%`;
}

function kpiCardHtml({ icon, iconBg, iconColor, name, value, sub, tone, pillLabel, barPct }) {
  return `
    <div class="home2-kpi-card">
      <div class="home2-kpi-card-head">
        <span class="home2-kpi-icon-badge" style="background:${iconBg};color:${iconColor}">${icon}</span>
        <span class="home2-kpi-name">${name}</span>
      </div>
      <div class="home2-kpi-val-row">
        <span class="home2-kpi-val">${value}</span>
        <span class="home2-kpi-pill tone-${tone}">${pillLabel}</span>
      </div>
      <div class="home2-kpi-sub">${sub}</div>
      <div class="home2-kpi-progress-track"><div class="home2-kpi-progress-fill tone-${tone}" style="width:${Math.max(4, Math.min(100, barPct))}%"></div></div>
    </div>`;
}

// ─── Home KPI Row (Network Health — compact, network-wide) ──
function renderHomeKPIs() {
  const kpis = getNetworkKpis(state.selectedPeriod);
  const grid = document.getElementById('home-kpi-grid');
  if (!kpis || !grid) return;

  const utilLabel = getUtilLabel(kpis.utilisation.value);
  const sla = slaStatus(kpis.sla.value, kpis.sla.target);
  const costDeltaPct = pctDelta(kpis.totalCost.value, kpis.totalCost.prev);
  const costStatus = deltaStatus(costDeltaPct);
  const invDeltaPct = pctDelta(kpis.inventoryDays.value, kpis.inventoryDays.prev);
  const invStatus = deltaStatus(invDeltaPct);

  grid.innerHTML = [
    kpiCardHtml({
      icon: '📊', iconBg: '#f5f0fa', iconColor: 'var(--primary)',
      name: 'Capacity Utilization',
      value: `${kpis.utilisation.value}%`,
      sub: 'of total capacity',
      tone: utilTone(utilLabel), pillLabel: utilLabel,
      barPct: kpis.utilisation.value,
    }),
    kpiCardHtml({
      icon: '✅', iconBg: '#f0fdf4', iconColor: 'var(--green)',
      name: 'On-time Service (SLA)',
      value: `${kpis.sla.value}%`,
      sub: `Target: ≥${kpis.sla.target}%`,
      tone: sla.tone, pillLabel: sla.label,
      barPct: kpis.sla.value,
    }),
    kpiCardHtml({
      icon: '₹', iconBg: '#f5f0fa', iconColor: 'var(--primary)',
      name: 'Total Cost',
      value: formatCurrency(kpis.totalCost.value),
      sub: `vs last period: ${fmtDelta(costDeltaPct)}`,
      tone: costStatus.tone, pillLabel: costStatus.label,
      barPct: 50 + costDeltaPct * 3,
    }),
    kpiCardHtml({
      icon: '📦', iconBg: '#fffbeb', iconColor: 'var(--amber)',
      name: 'Inventory Days',
      value: `${kpis.inventoryDays.value} days`,
      sub: `vs last period: ${fmtDelta(invDeltaPct)}`,
      tone: invStatus.tone, pillLabel: invStatus.label,
      barPct: 50 + invDeltaPct * 3,
    }),
  ].join('');
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
// ─── Attention feed categorisation ───────────────────────────
// Buckets an insight's `impact` (or an action's `tag` — the two use
// overlapping wording) into the small taxonomy shown in
// Dump/Home Overview-updated.png. Order matters: check the more
// specific phrase ("high value", "high impact") before the generic one.
const ATTENTION_CATEGORY_META = {
  'Recommendation':      { icon: '✨', bg: '#f5f0fa', color: '#6B2FA0', link: 'Review' },
  'Capacity Risk':       { icon: '⚠️', bg: '#fef2f2', color: '#dc2626', link: 'Investigate' },
  'Service Risk':        { icon: '🛡️', bg: '#fffbeb', color: '#b45309', link: 'View details' },
  'Network Opportunity': { icon: '📈', bg: '#f0fdf4', color: '#16a34a', link: 'Review' },
  'Performance Update':  { icon: '✅', bg: '#f0fdf4', color: '#16a34a', link: 'View details' },
  'Status':              { icon: 'ℹ️', bg: '#eff6ff', color: '#2563eb', link: 'View details' },
};

function categorizeAttentionLabel(text) {
  const t = (text || '').toLowerCase();
  if (t.includes('high value')) return 'Recommendation';
  if (t.includes('high impact')) return 'Capacity Risk';
  if (t.includes('medium impact')) return 'Service Risk';
  if (t.includes('opportunity') || t.includes('optimization')) return 'Network Opportunity';
  if (t.includes('positive') || t.includes('normal')) return 'Performance Update';
  return 'Status';
}

function attentionCardHtml(kind, id, category, title, subtitle) {
  const meta = ATTENTION_CATEGORY_META[category];
  const link = kind === 'action' ? 'Run scenario' : meta.link;
  return `
    <div class="home2-attn-item" data-kind="${kind}" data-id="${id}" title="Click for details">
      <span class="home2-attn-icon" style="background:${meta.bg};color:${meta.color}">${meta.icon}</span>
      <div class="home2-attn-body">
        <div class="home2-attn-kicker">${category}</div>
        <div class="home2-attn-item-title">${title}</div>
        <div class="home2-attn-item-sub">${subtitle}</div>
        <span class="home2-attn-link">${link}
          <svg width="13" height="13" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M5 10h10M11 6l4 4-4 4"/></svg>
        </span>
      </div>
    </div>`;
}

// Insights actioned via the deep-dive page (insight-detail.js) are
// dropped from Home's feed on the next render — see
// window.markAttentionItemResolved below.
const resolvedInsightIds = new Set();

// ─── Home Attention Feed (merged Insights + Recommendations) ─
// Replaces the two separate "Here is what I found" / "Here are my
// recommendations" preview cards with one scrollable feed, per
// Dump/Home Overview-updated.png. Clicking a card navigates to the
// full-page insight deep dive — see insight-detail.js.
function renderHomeAttentionFeed() {
  const list = document.getElementById('home-attention-list');
  if (!list) return;

  const insights = getInsightsForFacility(state.selectedFacility)
    .filter(ins => !resolvedInsightIds.has(ins.id));

  const insightCards = insights.map(ins =>
    attentionCardHtml('insight', ins.id, categorizeAttentionLabel(ins.impact), ins.title, ins.subtitle));

  const actionCards = HOME_ACTION_ITEMS
    .filter(act => !resolvedInsightIds.has(act.id))
    .map(act => {
      const impact = act.expectedImpact || {};
      const subtitle = [impact.cost, impact.sla ? `SLA ${impact.sla}` : null].filter(Boolean).join(' · ');
      return attentionCardHtml('action', act.id, categorizeAttentionLabel(act.tag), act.title, subtitle);
    });

  const cards = [...insightCards, ...actionCards];
  list.innerHTML = cards.length
    ? cards.join('')
    : `<div class="home2-attn-empty">No open items right now — network is performing within target.</div>`;

  list.querySelectorAll('.home2-attn-item').forEach(el => {
    el.addEventListener('click', () => {
      const { kind, id } = el.dataset;
      if (typeof window.showInsightDetail === 'function') {
        window.showInsightDetail(kind, id);
      }
    });
  });
}

if (typeof window !== 'undefined') {
  window.markAttentionItemResolved = id => resolvedInsightIds.add(id);
}

// ─── Scenario Comparison Action Drawer (scenarios.js) ────────
// Home's attention feed no longer uses this drawer (it navigates to a
// full-page insight deep dive instead — see insight-detail.js), but
// scenarios.js's own "Scenario Comparison Actions" list still opens
// detail into these same #action-drawer-* elements, so closeActionDrawer
// stays here as the shared close handler.
export function closeActionDrawer() {
  const overlay = document.getElementById('action-drawer-overlay');
  if (overlay) {
    overlay.classList.remove('active');
    overlay.classList.remove('visible');
    overlay.style.display = 'none';
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

// ─── KPI Export Report ───────────────────────────────────────
export function exportFacilityReport() {
  const facId = state.selectedFacility || 'DC_DELHI';
  const fac = getFacilityById(facId) || DCS[0];
  const kpis = getKpisForFacility(facId);
  const insights = getInsightsForFacility(facId);

  const lines = [
    '=== NetGravity Executive Facility Performance & Analytics Report ===',
    'Generated Date,' + new Date().toLocaleDateString(),
    'Facility Name,' + fac.name,
    'Facility Type,' + (fac.id.startsWith('PLT_') ? 'Manufacturing Plant' : 'Distribution Centre'),
    'Location,"' + fac.city + ', ' + fac.state + ' (' + fac.region + ' Region)"',
    'Active Period,' + (state.selectedPeriod || 'August 2026'),
    '',
    '=== Operational Telemetry & Capacity Horizon ===',
    'Capacity (Units/Day),' + fac.capacity,
    'Current Throughput (Units/Day),' + fac.throughput,
    'Utilisation Rate,' + fac.utilPct + '%',
    'Projected Peak Utilisation (Dec 2026),' + (fac.id === 'DC_DELHI' ? '108% (Breach Risk)' : '76%'),
    '',
    '=== Core Performance KPIs ===',
    'Metric,Value,Target Benchmark,Status',
    'On-Time Service SLA,' + (kpis.sla || '96.7%') + ',>=95.0%,Target Met',
    'Monthly Operating Cost,' + (kpis.cost || '₹11.8L') + ',Budget Aligned,Healthy',
    'Inventory Days of Supply,' + (kpis.invDays || '11.2 Days') + ',10-14 Days,Optimal',
    'Order Fill Rate,' + (kpis.fillRate || '99.1%') + ',>=98.0%,Optimal',
    '',
    '=== AI Prescriptive Diagnosis & Risk Telemetry ===',
    'Insight ID,Severity,Diagnosis Summary'
  ];

  if (insights && insights.length > 0) {
    insights.forEach(function(ins) {
      const desc = ins.title || ins.desc || '';
      lines.push('"' + ins.id + '","' + (ins.impact || 'Critical') + '","' + desc.split('"').join('""') + '"');
    });
  } else {
    lines.push('"INS_CAP_RISK","Critical","Delhi NCR DC is approaching 94% utilization threshold with Q4 peak breach risk."');
    lines.push('"INS_COST_SAVING","Opportunity","Baddi manufacturing volume rebalancing to Kolkata DC captures ₹2.4L/mo savings."');
  }

  const csvContent = 'data:text/csv;charset=utf-8,' + encodeURIComponent(lines.join('\r\n'));
  const link = document.createElement('a');
  link.setAttribute('href', csvContent);
  link.setAttribute('download', 'NetGravity_KPI_Report_' + fac.id + '.csv');
  document.body.appendChild(link);
  link.click();
  link.remove();

  showNotification('✓ Exported KPI report for ' + fac.name + ' (' + fac.id + '.csv)');
}

// Expose export on window
if (typeof window !== 'undefined') {
  window.exportFacilityReport = exportFacilityReport;
  window.triggerAgentReasoning = triggerAgentReasoning;
}
