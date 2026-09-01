/**
 * NetGravity — Main Application Controller
 * ==========================================
 * Tab routing, Home cockpit state, facility/period selectors,
 * KPI rendering, insight list, insight drawer, and all sub-views.
 */

import {
  PLANTS, DCS, MARKETS, LANES, EXTERNAL_SIGNALS, SCENARIOS,
  DEMAND_HISTORY, FORECAST,
  RECOMMENDATION, PERIODS, HOME_ACTION_ITEMS, FACILITY_KPIS,
  GOVERNANCE_TIERS, SYSTEM_STATUS,
  formatCurrency, formatNumber, fmtNum, getUtilColor, getUtilLabel, getUtilTagClass,
  getFacilityById, getInsightsForFacility, getKpisForFacility, getOptimizedBaseCase,
  getNetworkInsights, NETWORK_RECOMMENDATION,
  isDCFacility, isPlantFacility, facilityRole, clearNetworkModel,
  perPeriodLabel
} from './data.js';
import { initMap, setNetworkState, invalidateMapSize, refreshAllMaps } from './map.js';
import { initTwin3D, setTwin3DState, resizeTwin3D } from './twin3d.js';
import {
  renderForecastChart,
  renderFacilityThroughputChart, renderFacilityCostBreakdownChart, renderFacilityLaneFlowsChart
} from './charts.js';
import { initScenarios } from './scenarios.js';
import { initAgent } from './agent.js';
import { initLandingPage } from './landing.js';
import { initInsightDetail } from './insight-detail.js';
import { initAuth } from './auth.js';
import { initProjects, loadProjects, openProjectById } from './projects.js';
import { initIngestion } from './ingestion.js';
import { initChatbot } from './chatbot.js';
import { triggerAgentReasoning } from './agent-reasoning.js';
import { apiClient } from './integration/api-client.js';
import { initActions } from './actions.js';
import { authService } from './integration/services/auth-service.js';
import { getActiveProjectId, setActiveProject } from './integration/project-context.js';
import { loadIdentity, getCurrentUser, clearCurrentUser } from './identity.js';
import { kpiService } from './integration/services/kpi-service.js';
import { twinService } from './integration/services/twin-service.js';
import { mapNetworkKPIsToCards } from './integration/mappers/kpi-mapper.js';
import { mapTwinStateToFrontend } from './integration/mappers/twin-mapper.js';

// ─── State ──────────────────────────────────────────────────
const state = {
  activeTab: 'home',
  networkState: 'actual',
  mapsInitialised: {},
  chartsInitialised: {},
  // Home cockpit state
  facilityType: 'DC',          // 'DC' | 'Plant'
  // No default facility id. It was 'DC_DELHI' — a prototype facility — and
  // every screen keyed off it showed nothing until the user changed the
  // selector. `initHomeSelectors()` sets this to the first facility in the
  // network that is actually loaded.
  selectedFacility: null,
  // Set from the bound network's own demand periods; there is no default
  // quarter, because no upload has ever stated one.
  selectedPeriod: null,
};

// Insights actioned via the deep-dive page (insight-detail.js) are dropped
// from Home's feed on the next render — see window.markAttentionItemResolved.
//
// Declared here, above the window exports, because those exports make
// renderHome() callable immediately: hydrate.js invokes it as soon as the
// authoritative data lands, which can be before module evaluation reaches the
// bottom of this file. With the declaration further down, that call hit the
// temporal dead zone and threw "Cannot access 'resolvedInsightIds' before
// initialization", leaving Home's attention feed blank.
const resolvedInsightIds = new Set();

// Expose globally on window
if (typeof window !== 'undefined') {
  window.navigateToTab = navigateToTab;
  window.closeActionDrawer = closeActionDrawer;
  window.renderHome = renderHome;
  // Exposed so the authoritative hydration can refresh the twin once solved
  // figures arrive. Without this its re-render call was a silent no-op, and
  // the Digital Twin kept showing pre-solve utilisation.
  window.renderTwinTables = renderTwinTables;
  window.initHomeSelectors = initHomeSelectors;
  // Read-only: lets a validation run assert that a facility's role comes from
  // the loaded network rather than from the spelling of its id.
  window.__ngFacilityRole = facilityRole;
}

// ─── Boot ───────────────────────────────────────────────────
function bootApp() {
  try { initProjects(); } catch (e) { console.error('initProjects error:', e); }
  try { initIngestion(); } catch (e) { console.error('initIngestion error:', e); }
  try {
    initAuth();
    initChatbot();
  } catch (e) { console.error('initAuth error:', e); }
  try { initLandingPage(); } catch (e) { console.error('initLandingPage error:', e); }
  try { initTabs(); } catch (e) { console.error('initTabs error:', e); }
  try { initSidebarCollapse(); } catch (e) { console.error('initSidebarCollapse error:', e); }
  try { initHomeSelectors(); } catch (e) { console.error('initHomeSelectors error:', e); }
  try { renderHome(); } catch (e) { console.error('renderHome error:', e); }
  try { renderTwinTables(); } catch (e) { console.error('renderTwinTables error:', e); }
  try { initScenarios(); } catch (e) { console.error('initScenarios error:', e); }
  try { initAgent(); } catch (e) { console.error('initAgent error:', e); }
  try { initInsightDetail(); } catch (e) { console.error('initInsightDetail error:', e); }
  // One delegated listener for every `data-action` in the markup. The
  // inline `onclick` attributes it replaces were script, and a CSP that
  // allows those has to allow all inline script.
  try { initActions(); } catch (e) { console.error('initActions error:', e); }
  loadServerStatus();
  // A reset link opens the page with `?reset_token=…`. Handled before session
  // restore, so arriving with a link does not drop into a stale session
  // instead of the reset form.
  import('./auth.js').then((m) => m.handleResetLink()).catch(() => null);
  document.getElementById('form-panel-reset')?.addEventListener('submit', (e) => {
    e.preventDefault();
    if (typeof window.requestPasswordReset === 'function') window.requestPasswordReset();
  });
  restoreSession();
}

/**
 * What this build actually is, from the server rather than from a literal.
 *
 * Version strings were hardcoded in three places and none of them matched the
 * application's own — the chat header announced "NetGravity AI v2.4" while the
 * server reported 2.0.0. `/api/status` is public and carries no customer data.
 */
function loadServerStatus() {
  fetch('/api/status')
    .then((r) => (r.ok ? r.json() : null))
    .then((s) => { if (s) window.__ngServerStatus = s; })
    .catch(() => { /* the label falls back to a version-free name */ });
}

/**
 * Put the user back where they were after a page refresh.
 *
 * The auth token and the active project id are both already persisted in
 * `localStorage` — and nothing read them on boot. Refreshing the page dropped
 * a signed-in user back onto the marketing landing page with a valid session in
 * their browser, and every solved scenario, KPI and map apparently gone. They
 * were not gone; the app had simply forgotten it had a session.
 *
 * The token is verified against the server before anything is restored, so an
 * expired or revoked one lands on sign-in rather than into a shell that will
 * fail on its first request.
 */
async function restoreSession() {
  const landing = document.getElementById('landing-page');
  if (!landing || landing.classList.contains('hidden')) return;
  // The session is an httpOnly cookie now, so `apiClient.token` is empty in
  // the browser and cannot be the test for 'signed in'.
  if (!apiClient.hasSession) return;

  // Verifying the token and loading the identity are the same call.
  const user = await loadIdentity();
  if (!user) {
    // Not signed in any more. Clear the stale token so the next request does
    // not carry it, and leave the landing page up.
    apiClient.setToken(null);
    return;
  }

  try {
    await loadProjects();
  } catch (e) {
    // Signed in, but the project list is unavailable. Sign-in is still the
    // honest landing place.
    return;
  }

  const activeId = getActiveProjectId();
  if (activeId && openProjectById(activeId)) return;
  if (typeof window.showSelectProject === 'function') window.showSelectProject();
}

// `bootApp()` must not run during this module's own evaluation.
//
// As a deferred module script this file is usually evaluated after the DOM is
// ready, so the `else` branch used to call bootApp() right here — on line ~90,
// while the module body below was still executing. Everything bootApp reaches
// that depends on a `const` declared further down (ATTENTION_CATEGORY_META,
// resolvedInsightIds, …) was then in the temporal dead zone, and Home's
// attention feed died with "Cannot access '…' before initialization".
//
// A microtask runs after the current synchronous execution completes, which
// includes the rest of this module — so every declaration exists by the time
// bootApp starts.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootApp);
} else {
  queueMicrotask(bootApp);
}

// The forecast arrives asynchronously, after the screens have already
// rendered. Redraw whichever forecast canvas is mounted so the real series
// replaces the empty state without the user having to navigate away and back.
window.addEventListener('forecastSeriesLoaded', () => {
  try { renderForecastChart('chart-forecast'); } catch (err) { }
  try { renderForecastChart('chart-forecast-home'); } catch (err) { }
  try { renderForecastSummary(); } catch (err) { }
  // Home's forecast sentence is derived from the same series, so it has to be
  // rewritten when the series arrives — otherwise it keeps whatever it said
  // before the engine answered.
  try { renderHomeForecast(); } catch (err) { }
});

/**
 * Forecast Summary card, from the forecasting engine's own output.
 *
 * Every field here was static markup describing the prototype: model "Enhanced
 * Demand Forecast", growth "+14.2%", breach facility "Delhi NCR DC", breach
 * month "December 2026", projected utilisation "108%". None of it came from a
 * forecast run, and all of it stayed on screen for any uploaded network. The
 * breach fields are gone entirely — nothing in this build projects a
 * capacity-breach month — and are replaced by facts the engine does report:
 * which series is plotted, how much history it saw, and how it scored.
 */
function renderForecastSummary() {
  const meta = window.__ngForecastMeta || null;
  const set = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };

  if (!meta || !meta.series) {
    ['fc-model', 'fc-horizon', 'fc-accuracy', 'fc-series', 'fc-periods',
     'fc-series-count'].forEach(id => set(id, '—'));
    set('fc-chart-tag', 'No forecast');
    set('fc-chart-subtitle', meta && meta.reason
      ? meta.reason
      : 'No demand history has been ingested for this network.');
    return;
  }

  const mase = meta.accuracy && typeof meta.accuracy.mase === 'number'
    ? meta.accuracy.mase : null;
  set('fc-model', meta.engine || 'Not reported');
  set('fc-horizon', `${FORECAST.months.length} periods`);
  // MASE < 1 means the model beats a naive seasonal forecast; the comparison
  // is stated because the bare number means nothing to most readers.
  set('fc-accuracy', mase === null ? 'Not reported'
    : `${mase.toFixed(2)} (${mase < 1 ? 'better' : 'worse'} than naive)`);
  set('fc-series', meta.shown || '—');
  set('fc-periods', `${DEMAND_HISTORY.months.length} periods`);
  set('fc-series-count', `${meta.series} market-product pair(s)`);
  set('fc-chart-title', `Demand — ${meta.shown || 'Historical & Forecast'}`);
  set('fc-chart-tag', meta.status === 'OK' ? 'Observed + forecast' : meta.status);
  set('fc-chart-subtitle',
    `${DEMAND_HISTORY.months.length} observed periods + `
    + `${FORECAST.months.length}-period forecast · p10–p90 band`);
}

window.addEventListener('networkDataLoaded', (e) => {
  const net = e.detail;
  if (net && net.dcs && net.dcs.length > 0) {
    state.selectedFacility = net.dcs[0].id;
  }
  try { initHomeSelectors(); } catch (err) { }
  try { renderHome(); } catch (err) { }
  try { renderTwinTables(); } catch (err) { }
  try {
    // Redraw every mounted map from the arrays as they now stand. This used to
    // call initMap('home-map') and initMap('twin-map') — neither id exists in
    // the markup (the 2D container is 'map-twin'), so a map created before the
    // upload kept the demo network for the rest of the session.
    refreshAllMaps();
  } catch (err) { }
});

// ─── Header & Topbar Visibility Controller ──────────────────
function updateTopBarLayout(tab) {
  const isHomeOverview = (tab === 'home' || tab === 'overview');

  // .main-content caps at 1280px so other tabs' content doesn't stretch
  // too wide, but that same cap was leaving a plain white/gray gap to the
  // right of Home's purple gradient background on wide viewports (the
  // gradient only fills #tab-home's own box, which bleeds out to
  // .main-content's edges — not past them). Home spans the full width
  // instead so the gradient reaches the actual right edge of the page.
  const mainContent = document.querySelector('.main-content');
  if (mainContent) {
    mainContent.classList.toggle('home-full-bleed', isHomeOverview);
  }

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
        // Always call initTwin3D rather than gating on a "did we ever
        // init" flag: the 3D scene/canvas is a singleton shared with
        // Home's preview (see twin3d.js), and Home re-parents it into its
        // own container every time it renders — including after this tab
        // was already visited once. initTwin3D already branches
        // internally between a cold init and a cheap reparent+resume, so
        // calling it unconditionally is what keeps the canvas following
        // whichever container is actually asking for it.
        initTwin3D('twin3d-canvas');
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
      renderForecastSummary();
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

  // Home attention feed: manual refresh.
  //
  // It used to spin the icon, write "Just now" into the timestamp and re-render
  // the same cached figures — a refresh button that refreshed nothing and then
  // said it had. It now re-runs hydration, and the timestamp reports when the
  // analysis behind the figures was actually computed.
  document.getElementById('home2-refresh-btn')?.addEventListener('click', (e) => {
    const btn = e.currentTarget;
    btn.classList.add('spinning');
    const project = typeof window.getCurrentProject === 'function'
      ? window.getCurrentProject() : null;
    if (!project) {
      btn.classList.remove('spinning');
      renderHome();
      return;
    }
    import('./integration/hydrate.js')
      .then((m) => m.hydrateFromBackend(project.id))
      .catch(() => null)
      .then(() => {
        btn.classList.remove('spinning');
        renderHome();
        renderAnalysisTimestamp();
      });
  });

  document.getElementById('btn-topbar-notifications')?.addEventListener('click', () => {
    // This listed three fixed alerts about the prototype's demo footprint as
    // though they were live findings for whatever network was loaded.
    // Real threshold breaches for this network are reported on Home, sourced
    // from the KPI layer's triggered thresholds.
    alert('Notifications & System Alerts\n\nNo active alert for this network.\n'
      + 'Threshold breaches appear on the Home cockpit when the engine reports them.');
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
    // The signed-in account, not a fixed one. This read
    // "Logged in as: Amit Kumar / Role: Lead Supply Chain Architect (Admin) /
    // Organization: Kearney Decision Systems" for every user of the
    // application, including the role — which is a security-relevant claim the
    // server had never made about them.
    const user = getCurrentUser();
    if (!user) {
      alert('User profile\n\nNo signed-in session was found.');
      return;
    }
    // A real screen, not an alert(): it is where a password is changed, a
    // second factor is enrolled and live sessions are revoked. Those are all
    // API endpoints now, and an endpoint nobody can reach is a feature on
    // paper. The alert is kept as the fallback if the module fails to load.
    import('./account-security.js')
      .then((m) => m.openAccountSecurity())
      .catch(() => alert('User profile: ' + (user.name || '-')
        + ' | ' + (user.email || '-')
        + ' | role ' + (user.role || '-')
        + ' | ' + (user.organization || '-')));
  });
  document.getElementById('profile-menu-signout')?.addEventListener('click', async () => {
    document.getElementById('profile-dropdown-menu')?.classList.remove('open');
    // Sign-out now REVOKES the session and clears the model.
    //
    // It used to call `returnToLanding()` only: the bearer token stayed in
    // localStorage and the previous user's network stayed in memory, so the
    // next person to use the browser was signed in as them — and with session
    // restore on boot, a refresh would have put them straight back into that
    // account's projects.
    try {
      await authService.logout();
    } catch (e) {
      // The local session is cleared regardless; a server that cannot be
      // reached must not leave the user apparently signed in.
      apiClient.setToken(null);
    }
    clearCurrentUser();
    clearNetworkModel();
    setActiveProject(null);
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
          // See the same call in navigateToTab: always re-run initTwin3D
          // rather than trusting a one-time "initialised" flag, since
          // Home's preview may have re-parented the shared canvas away
          // since the last time this tab was shown.
          initTwin3D('twin3d-canvas');
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

        renderTwinStats();
      });
    });

    // Populate the overlays for the default ('actual') state on load, rather
    // than leaving the placeholders that are baked into the markup.
    renderTwinStats();
  }

  // Window resize handler for 3D canvas and maps
  window.addEventListener('resize', () => {
    resizeTwin3D();
  });

  // Facility panel close
  document.getElementById('fp-close')?.addEventListener('click', closeFacilityPanel);

  // S12: Signal Guardrails & Admin — replaces the old alert() stub
  document.getElementById('nav-item-settings')?.addEventListener('click', () => {
    renderAdminSettingsModal();
    document.getElementById('modal-admin-settings')?.classList.add('visible');
  });
  document.getElementById('btn-close-admin-settings')?.addEventListener('click', () => {
    document.getElementById('modal-admin-settings')?.classList.remove('visible');
  });
  document.getElementById('btn-close-admin-settings-bottom')?.addEventListener('click', () => {
    document.getElementById('modal-admin-settings')?.classList.remove('visible');
  });

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
/**
 * Report when the analysis on screen was actually computed.
 *
 * The markup shipped the literal string "5 min ago", which was true only by
 * coincidence and never updated. Reads the timestamp the KPI endpoint returns
 * with the figures themselves.
 */
function renderAnalysisTimestamp() {
  const el = document.getElementById('home2-refresh-time');
  if (!el) return;
  const at = (typeof window !== 'undefined') ? window.__ngAnalysisComputedAt : null;
  if (!at) {
    el.textContent = 'not yet analysed';
    return;
  }
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - at));
  if (seconds < 60) el.textContent = 'just now';
  else if (seconds < 3600) el.textContent = `${Math.round(seconds / 60)} min ago`;
  else if (seconds < 86400) el.textContent = `${Math.round(seconds / 3600)} h ago`;
  else el.textContent = new Date(at * 1000).toLocaleString();
}

/**
 * Fill one period `<select>` from the periods the bound network states.
 *
 * With no network, or with a single period, there is nothing to choose: the
 * control is disabled and reads what the figures actually cover. The previous
 * version listed four fixed quarters that appeared in no upload and filtered
 * nothing.
 */
function populatePeriodSelect(select) {
  const periods = PERIODS || [];
  if (!periods.length) {
    select.innerHTML = '<option value="">No period stated in this data</option>';
    select.disabled = true;
    select.title = 'The uploaded data states no period for its demand rows.';
    return;
  }
  select.innerHTML = periods
    .map((p) => `<option value="${p.id}">${p.label}</option>`).join('');
  if (!state.selectedPeriod
      || !periods.some((p) => p.id === state.selectedPeriod)) {
    state.selectedPeriod = periods[0].id;
  }
  select.value = state.selectedPeriod;
  // One period is the ordinary case: the MILP aggregates every demand row into
  // a single solved state, so offering a choice would imply a filter that does
  // not exist.
  select.disabled = periods.length < 2;
  select.title = periods.length < 2
    ? 'Your data states one demand period, and the analysis covers all of it.'
    : '';
}

function initHomeSelectors() {
  const selPeriod = document.getElementById('sel-period');
  const selFacility = document.getElementById('sel-facility');
  const selKpiType = document.getElementById('home-kpi-type-select');
  const selForecastType = document.getElementById('home-forecast-type-select');
  const selFacilityPicker = document.getElementById('home-facility-picker');

  // Facility selectors are populated from the network that is actually
  // loaded. The markup ships with the prototype's five demo DCs baked in as
  // static <option> elements, so after a user loaded their own network the
  // pickers still offered facilities that were not in their data — and
  // selecting one showed an empty dashboard. Options only; no layout changes.
  const facilityOptions = (extraAll) => {
    const opts = DCS.map(d => `<option value="${d.id}">${d.name || d.id}</option>`);
    if (!opts.length) return '<option value="ALL">No facility in this network</option>';
    return extraAll
      ? opts.join('') + `<option value="ALL">${extraAll}</option>`
      : `<option value="ALL">All Facilities</option>` + opts.join('');
  };

  const homeTopFacilitySel = document.getElementById('home-top-facility');
  if (homeTopFacilitySel) {
    homeTopFacilitySel.innerHTML = facilityOptions(null);
  }

  // Topbar facility selector
  if (selFacility) {
    selFacility.innerHTML = facilityOptions('All DCs (Network Overview)');
    // Fall back to the first facility that exists rather than a demo id.
    if (!DCS.some(d => d.id === state.selectedFacility)) {
      state.selectedFacility = DCS.length ? DCS[0].id : 'ALL';
    }
    selFacility.value = state.selectedFacility;
    selFacility.addEventListener('change', () => {
      state.selectedFacility = selFacility.value;
      if (selFacilityPicker) selFacilityPicker.value = state.selectedFacility;
      if (homeTopFacility) homeTopFacility.value = state.selectedFacility;
      renderHome();
    });
  }

  // (see populatePeriodSelect below)
  // Populate global period selector in topbar
  //
  // From the bound network's own demand periods. It used to offer four fixed
  // quarters — Q3 2026 down to Q4 2025 — for every network, and every one of
  // them showed identical figures, because the solve produces a single state
  // and the "periods" were four labels over one set of numbers. When the data
  // states one period there is no choice to offer, so the control says what
  // the figures cover instead of pretending to filter them.
  if (selPeriod) {
    populatePeriodSelect(selPeriod);
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
    homeTopFacility.value = state.selectedFacility || 'ALL';
    homeTopFacility.addEventListener('change', () => {
      state.selectedFacility = homeTopFacility.value;
      if (selFacility) selFacility.value = state.selectedFacility;
      if (selFacilityPicker) selFacilityPicker.value = state.selectedFacility;
      renderHome();
    });
  }

  const homeTopPeriod = document.getElementById('home-top-period');
  if (homeTopPeriod) {
    populatePeriodSelect(homeTopPeriod);
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

  // Note: the Digital Twin map/card itself is no longer a click-to-navigate
  // target (it needs click-drag for 3D orbit controls) — only the "Open
  // Digital Twin" link in its header navigates, via its own onclick.

  // Chatbot Send Button & Enter Key
}

function populateFacilitySelector() {
  const selFacilityPicker = document.getElementById('home-facility-picker');
  const facilities = state.facilityType === 'DC' ? DCS : PLANTS;

  // An empty network is the ordinary state at boot, and `facilities[0].id`
  // threw on it — `TypeError: Cannot read properties of undefined (reading
  // 'id')`, which aborted `initHomeSelectors()` and left every Home selector
  // unwired until a network happened to load first.
  if (!facilities.length) {
    state.selectedFacility = null;
    if (selFacilityPicker) {
      selFacilityPicker.innerHTML =
        '<option value="">No facility in this network</option>';
    }
    return;
  }

  if (!state.selectedFacility
      || !facilities.some(f => f.id === state.selectedFacility)) {
    state.selectedFacility = facilities[0].id;
  }
  if (selFacilityPicker) {
    selFacilityPicker.innerHTML = facilities.map(f =>
      `<option value="${f.id}">${f.name}</option>`
    ).join('');
    selFacilityPicker.value = state.selectedFacility;
  }
}

// ─── Render Full Home ───────────────────────────────────────
function renderHome() {
  renderHomeKPIs();
  renderHomeForecast();
  renderHomeDigitalTwin();
  renderHomeAttentionFeed();
  renderNetworkRecommendation();
  renderAnalysisTimestamp();
}

// ─── Facility Full Analytics Dashboard ──────────────────────
/** Shown when no facility exists to report on, instead of a blank screen. */
function renderFacilityDashboardEmpty() {
  const grid = document.getElementById('dash-metrics-grid');
  if (grid) {
    grid.innerHTML = `<div class="card" style="grid-column:1/-1">
        <div class="card-title">No facility to report on</div>
        <div class="card-subtitle">This project has no distribution centre or
          plant loaded yet. Upload a dataset to populate these KPIs.</div>
      </div>`;
  }
  const name = document.getElementById('dash-facility-name');
  if (name) name.textContent = '—';
  const tbody = document.querySelector('#table-dash-lanes tbody');
  if (tbody) tbody.innerHTML = '<tr><td colspan="7">No corridors are loaded.</td></tr>';
}

export function renderFacilityDashboard() {
  // Fall back to a facility that EXISTS before giving up. `state.selectedFacility`
  // starts as the prototype's 'DC_DELHI', and a bare `if (!fac) return` left the
  // whole KPI screen blank — no error, no empty state — for any network that
  // does not contain a facility by that name, which is every real one.
  let fac = getFacilityById(state.selectedFacility);
  if (!fac) {
    const fallback = DCS[0] || PLANTS[0];
    if (!fallback) { renderFacilityDashboardEmpty(); return; }
    state.selectedFacility = fallback.id;
    fac = fallback;
    const sel = document.getElementById('sel-facility');
    if (sel && [...sel.options].some(o => o.value === fac.id)) sel.value = fac.id;
  }

  const kpis = getKpisForFacility(state.selectedFacility, state.selectedPeriod);
  const period = PERIODS.find(p => p.id === state.selectedPeriod);
  // Role comes from the loaded network, not from the id's spelling.
  const isDC = isDCFacility(state.selectedFacility);
  const utilPct = isDC ? fac.utilPct
    : (fac.throughput != null && fac.capacity
        ? ((fac.throughput / fac.capacity) * 100).toFixed(1) : null);
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
    dashDot.style.background = utilColor;
  }

  const elDashTitle = document.getElementById('dash-title');
  if (elDashTitle) elDashTitle.textContent = `${fac.name} — Performance & Analytics`;
  const elDashSubtitle = document.getElementById('dash-subtitle');
  if (elDashSubtitle) elDashSubtitle.textContent = `${fac.city}, ${fac.state} · ${fac.region} Region · Full operational telemetry and cost breakdown for ${period ? period.short : 'Aug 2026'}.`;

  // Corridors attached to this facility, computed BEFORE the cards because
  // three of them report on it. Lead time, distance and rate are uploaded
  // inputs; flow and carbon are solver outputs and stay absent until a solve
  // produces them.
  const laneRows = LANES.filter(
    l => l.from === state.selectedFacility || l.to === state.selectedFacility);
  const leadTimes = laneRows.map(l => l.leadTime).filter(v => typeof v === 'number');
  const laneCarbon = laneRows.map(l => l.carbonKg).filter(v => typeof v === 'number');
  const laneFlow = laneRows.map(l => l.flow).filter(v => typeof v === 'number');
  const avgLead = leadTimes.length
    ? (leadTimes.reduce((a, b) => a + b, 0) / leadTimes.length) : null;
  const carbonTotal = laneCarbon.length
    ? laneCarbon.reduce((a, b) => a + b, 0) : null;
  const carbonUnits = laneFlow.length ? laneFlow.reduce((a, b) => a + b, 0) : 0;
  const carbonPerUnit = (carbonTotal !== null && carbonUnits > 0)
    ? carbonTotal / carbonUnits : null;
  const dash = (v) => (v === null || v === undefined || Number.isNaN(v)) ? '—' : v;

  // 6 Executive Metric Cards
  const metricsGrid = document.getElementById('dash-metrics-grid');
  if (metricsGrid) {
    metricsGrid.innerHTML = `
      <div class="dash-metric-card">
        <div class="dash-metric-title">Capacity & Daily Throughput</div>
        <div class="dash-metric-val" style="color:${utilColor}">${utilPct}% <span style="font-size:14px;color:var(--text-3);font-weight:600">(${formatNumber(fac.throughput)}/${formatNumber(fac.capacity)} u/d)</span></div>
        <div class="dash-metric-sub">
          <span>Spare Capacity: <strong>${formatNumber(fac.capacity - fac.throughput)} u/d</strong></span>
          <span class="tag ${getUtilTagClass(utilPct)}">${getUtilLabel(utilPct)}</span>
        </div>
      </div>

      <div class="dash-metric-card">
        <div class="dash-metric-title">Demand Served Within SLA</div>
        <div class="dash-metric-val" style="color:var(--green)">${dash(kpis?.sla?.value)}%</div>
        <div class="dash-metric-sub">
          <span>Target: <strong>≥95.0%</strong></span>
          <!-- Was "↑ 1.8% vs last period". There is no last period: a solve is
               one point in time and this build stores no prior run. -->
          <span class="text-muted">Network-wide · no prior solve to compare</span>
        </div>
      </div>

      <div class="dash-metric-card">
        <div class="dash-metric-title">Total Operating Cost</div>
        <div class="dash-metric-val" style="color:var(--primary)">${formatCurrency(kpis?.totalCost?.value ?? null)}</div>
        <div class="dash-metric-sub">
          <span>Handling: <strong>${fac.handlingCost == null ? '—' : '₹' + fac.handlingCost + '/unit'}</strong></span>
          <!-- Was "↓ 3.2% vs budget". No budget is loaded anywhere in this build. -->
          <span class="text-muted">Not attributed per facility</span>
        </div>
      </div>

      <div class="dash-metric-card">
        <div class="dash-metric-title">Inventory Supply Coverage</div>
        <div class="dash-metric-val">${dash(kpis?.inventoryDays?.value)} <span style="font-size:16px;color:var(--text-3);font-weight:600">days</span></div>
        <div class="dash-metric-sub">
          <!-- Were "₹3.4L avg" and "140%", neither computed anywhere. -->
          <span>Holding value: <strong>—</strong></span>
          <span>Safety buffer: <strong>—</strong></span>
        </div>
      </div>

      <div class="dash-metric-card">
        <div class="dash-metric-title">Average Transit Lead Time</div>
        <div class="dash-metric-val">${avgLead === null ? '—' : avgLead.toFixed(1)} <span style="font-size:16px;color:var(--text-3);font-weight:600">days</span></div>
        <div class="dash-metric-sub">
          <!-- From the transit times on this facility's own lanes. The card
               read a fixed "1.2 days · Fastest 0.3d · Slowest 3.5d" for every
               facility of every network. -->
          <span>${leadTimes.length
            ? `Fastest: <strong>${Math.min(...leadTimes)}d</strong> · Slowest: <strong>${Math.max(...leadTimes)}d</strong>`
            : 'No connected lane carries a transit time'}</span>
          <span class="text-muted">${leadTimes.length} lane(s)</span>
        </div>
      </div>

      <div class="dash-metric-card">
        <div class="dash-metric-title">Carbon on Connected Corridors</div>
        <div class="dash-metric-val">${carbonPerUnit === null ? '—' : carbonPerUnit.toFixed(2)} <span style="font-size:16px;color:var(--text-3);font-weight:600">kg CO₂e/u</span></div>
        <div class="dash-metric-sub">
          <!-- Summed from the solver's per-lane carbon for the lanes attached
               to this facility, and labelled as that rather than as the
               facility's own footprint. Was a fixed "0.42 kg CO2e/u,
               14.8t CO2e/mo, down 2.1% YoY". -->
          <span>Total: <strong>${carbonTotal === null ? '—' : formatNumber(Math.round(carbonTotal)) + ' kg'}</strong></span>
          <span class="text-muted">${carbonTotal === null ? 'no solved flow' : 'inbound + outbound lanes'}</span>
        </div>
      </div>
    `;
  }

  // Tags on Charts
  const utilTag = document.getElementById('dash-util-tag');
  if (utilTag) utilTag.textContent = `${utilPct}% Utilisation`;
  const costTag = document.getElementById('dash-total-cost-tag');
  if (costTag) costTag.textContent = kpis ? `${formatCurrency(kpis.totalCost.value)} / period` : '— / period';

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
      <div>• <strong>${connectedLanes.length} active transportation corridors</strong> handle a collective flow of <strong>${formatNumber(totalFlow)} ${perPeriodLabel()}</strong>.</div>
      <div class="mt-xs">• Weighted average transportation rate across all active arcs is <strong>₹${avgCost} / unit</strong>.</div>
      <!-- Was "on-time transit confidence of 98.2%", a figure nothing in this
           build measures. Replaced with a fact the corridor set does carry. -->
      <div class="mt-xs">• ${leadTimes.length
        ? `Transit times across these corridors run <strong>${Math.min(...leadTimes)}–${Math.max(...leadTimes)} days</strong>.`
        : 'No connected corridor carries a transit time.'}</div>
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
        <td class="num font-bold">₹${fmtNum(l.cost, 1)}</td>
        <td class="num">${l.leadTime} days</td>
        <td><span class="tag tag-success">${l.mode}</span></td>
      </tr>
    `).join('');
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
    rows = DCS.map(d => getKpisForFacility(d.id, periodId)).filter(Boolean);
  }
  if (!rows.length) return null;

  // Aggregate only over rows that actually carry the metric. A solved network
  // reports utilisation per facility but has no per-facility SLA or cost, so
  // those arrive absent rather than as invented numbers — and averaging over a
  // null would produce NaN, or throw, depending on the field.
  const pick = sel => rows.map(sel).filter(v => v !== null && v !== undefined && !Number.isNaN(v));
  const avg = sel => {
    const vals = pick(sel);
    return vals.length ? +(vals.reduce((a, b) => a + b, 0) / vals.length).toFixed(1) : null;
  };
  const sum = sel => {
    const vals = pick(sel);
    return vals.length ? vals.reduce((a, b) => a + b, 0) : null;
  };

  return {
    utilisation: { value: avg(r => r.utilisation?.value), prev: avg(r => r.utilisation?.prev) },
    // `?? 95` was a second copy of the invented service target. A target the
    // data does not state stays null, and the tile that reads it says so.
    sla: { value: avg(r => r.sla?.value), prev: avg(r => r.sla?.prev),
           target: (typeof rows[0].sla?.target === 'number') ? rows[0].sla.target : null },
    totalCost: { value: sum(r => r.totalCost?.value), prev: sum(r => r.totalCost?.prev) },
    inventoryDays: { value: avg(r => r.inventoryDays?.value), prev: avg(r => r.inventoryDays?.prev) },
  };
}

function pctDelta(value, prev) {
  if (!prev) return 0;
  return ((value - prev) / prev) * 100;
}

function fmtDelta(pct) {
  if (pct === null || pct === undefined || Number.isNaN(pct)) return '—';
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%`;
}

function kpiStripArrowSvg(dir) {
  return dir === 'up'
    ? `<svg width="11" height="11" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M10 15V5M5 9l5-5 5 5"/></svg>`
    : `<svg width="11" height="11" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M10 5v10M5 11l5 5 5-5"/></svg>`;
}

function kpiStripItemHtml({ icon, value, label, deltaPrefix, deltaText, deltaDir, deltaGood, deltaNeutral }) {
  // deltaNeutral: a plain source/state label rather than a trend arrow —
  // used where no authoritative prior-period figure exists to compare
  // against (see the Total cost tile, sourced from S6's single snapshot).
  const deltaInner = deltaNeutral
    ? `<strong style="color:var(--text-3);font-weight:600">${deltaText}</strong>`
    : `<strong class="tone-${deltaGood ? 'good' : 'bad'}">${deltaText}${kpiStripArrowSvg(deltaDir)}</strong>`;
  return `
    <div class="home2-kpi-strip-item">
      <span class="home2-kpi-strip-icon">${icon}</span>
      <div class="home2-kpi-strip-text">
        <div class="home2-kpi-strip-value">${value}</div>
        <div class="home2-kpi-strip-name">${label}</div>
        <div class="home2-kpi-strip-delta">${deltaPrefix}
          ${deltaInner}
        </div>
      </div>
    </div>`;
}

// ─── Home KPI Strip (Network Today — 3 headline, network-wide KPIs;
//     the full facility-by-facility breakdown lives behind "View all
//     KPIs" on the Facility Dashboard tab) ───────────────────────────
// S1 P0 coverage: Total Network Cost (tile 1), Fill Rate (tile 2 — see
// note below on why this isn't split into a second SLA-Adherence figure),
// Savings % (tile 3, replacing the lower-priority Utilization-detail tile
// that used to be here — Capacity Risk itself is already surfaced via the
// attention feed's risk-categorized cards, not duplicated here).
//
// Total cost is sourced from S6's getOptimizedBaseCase().baseline — not
// from getNetworkKpis()'s FACILITY_KPIS sum, which is a different, much
// larger figure (per-facility operating cost on a different basis, ~3x
// this network total) that was being shown here as if it were the same
// "Total Network Cost" every other screen displays. getNetworkKpis() is
// still used below for Fill Rate, which has no other single owner.
function renderHomeKPIs() {
  const kpis = getNetworkKpis(state.selectedPeriod);
  const grid = document.getElementById('home-kpi-grid');
  if (!grid) return;

  // Every tile here reports a solved figure, so each falls back to "—" rather
  // than to a number. The fallbacks used to be literals — cost kept whatever
  // was last in the base case (the prototype's own ₹12.8L, shown under a
  // "Source: S6 Optimized Base Case" label for a network it did not describe)
  // and fill rate defaulted to 94.3%, which rendered as the literal text
  // "null%" once the KPI was honestly absent, with a delta computed against it.
  const baseCase = getOptimizedBaseCase();
  const baselineCost = baseCase?.baseline?.totalCost ?? null;
  // "Optimized Base Case (Actual)" is the DEMO base case's own name. Once a
  // real network is bound the figure is an as-is evaluation of what the user
  // uploaded — nothing has been optimised — so the label says which it is.
  const costSource = baselineCost === null
    ? 'No solved result yet'
    : (baseCase?.source === 'AUTHORITATIVE_KPI_LAYER'
       ? 'Your network, as uploaded'
       : 'Optimized Base Case (Actual)');
  // The tile is labelled "Fill Rate", so it reports the fill rate. It was
  // averaging the per-facility `sla` rows, which hydration writes from
  // `pct_demand_in_sla` — the two coincide only while every servable unit is
  // also inside its service level.
  const baseFill = getOptimizedBaseCase()?.baseline?.fillRate;
  const fillRate = (typeof baseFill === 'number' && Number.isFinite(baseFill))
    ? baseFill
    : ((typeof kpis?.sla?.value === 'number' && Number.isFinite(kpis.sla.value))
       ? kpis.sla.value : null);
  // A target is only real if the data states one. It was `?? 95.0`, so the
  // tile printed "vs target: +5.0%" against a 95% benchmark nobody had ever
  // given the product — a fabricated business comparison next to a solved
  // figure, in the same typeface. With no target, the tile says what the fill
  // rate is made of instead: served demand out of total demand, both solved.
  const fillRateTarget = (typeof kpis?.sla?.target === 'number')
    ? kpis.sla.target : null;
  const fillRateVsTarget = (fillRate === null || fillRateTarget === null)
    ? null : fillRate - fillRateTarget;
  const baseline = getOptimizedBaseCase()?.baseline || {};
  const fillBasis = (typeof baseline.servedDemand === 'number'
                     && typeof baseline.totalDemand === 'number')
    ? `${formatNumber(baseline.servedDemand)} of ${formatNumber(baseline.totalDemand)} units served`
    : null;
  // The best solved scenario for THIS network, not a named prototype one.
  //
  // This read `SCENARIOS.find(s => s.id === 'SCN_REBALANCE')` — an id the
  // backend has never issued — so the tile said "Not available" however many
  // scenarios the user had solved, including ones that saved money.
  const improvements = SCENARIOS
    .filter(s => s.id !== 'SCN_ACTUAL'
      && typeof s.costChange === 'number' && s.costChange < 0);
  const best = improvements.length
    ? improvements.reduce((a, b) => (a.costChange <= b.costChange ? a : b))
    : null;
  const savingsPct = best ? +Math.abs(best.costChange).toFixed(1) : null;

  grid.innerHTML = [
    kpiStripItemHtml({
      icon: '₹', value: formatCurrency(baselineCost), label: 'Total cost',
      deltaPrefix: 'Source:',
      deltaText: costSource,
      deltaNeutral: true,
    }),
    kpiStripItemHtml({
      icon: '✅', value: fillRate === null ? '—' : `${fillRate}%`, label: 'Fill Rate',
      deltaPrefix: fillRateVsTarget !== null ? 'vs target:' : '',
      deltaText: fillRateVsTarget !== null
        ? fmtDelta(fillRateVsTarget)
        : (fillBasis || 'no service target in your data'),
      deltaDir: (fillRateVsTarget ?? 0) < 0 ? 'down' : 'up',
      deltaGood: fillRateVsTarget === null ? null : fillRateVsTarget >= 0,
      deltaNeutral: fillRateVsTarget === null,
    }),
    kpiStripItemHtml({
      icon: '🎯', value: savingsPct !== null ? `${savingsPct}%` : 'Not available', label: 'Savings opportunity',
      deltaPrefix: best ? 'best scenario:' : '',
      deltaText: best ? best.name : 'No scenario beats your network yet',
      deltaDir: 'down', deltaGood: savingsPct !== null,
      deltaNeutral: savingsPct === null,
    }),
  ].join('');

  // Asynchronously re-read the authoritative KPI layer, but only when there is
  // something to read it for.
  //
  // This fired unconditionally on every render — including the very first one,
  // on the landing page, before anyone had signed in. With no token and no
  // project it produced a 401 in the console on every page load, and the
  // browser's own network panel showed the application failing to authenticate
  // against itself. The figures it writes are already on screen from
  // hydration; this only refreshes them.
  const activeProject = (typeof window.getCurrentProject === 'function')
    ? window.getCurrentProject() : null;
  if (!activeProject || !apiClient.hasSession) return;

  kpiService.getNetworkKPIs(activeProject.id).then(res => {
    if (res && res.kpis) {
      const mapped = mapNetworkKPIsToCards(res.kpis);
      if (mapped.totalCost && mapped.totalCost.isValid) {
        // Authoritative cost from Phase 9.1 KPIRegistry
        const costEl = grid.querySelector('.home2-kpi-strip-item:nth-child(1) .home2-kpi-strip-value');
        if (costEl) costEl.textContent = mapped.totalCost.display;
      }
      // Second tile is Fill Rate, so it takes the fill rate; `mapped.sla` is
      // SLA compliance and belongs to a different tile.
      if (mapped.fillRate && mapped.fillRate.isValid) {
        const fillEl = grid.querySelector('.home2-kpi-strip-item:nth-child(2) .home2-kpi-strip-value');
        if (fillEl) fillEl.textContent = mapped.fillRate.display;
      }
    }
  }).catch(e => console.warn('Authoritative KPI fetch note:', e));
}

// ─── Home Forecast Section ──────────────────────────────────

/**
 * One sentence describing the forecast on screen, from the forecast on screen.
 *
 * Reads the series the engine returned (`FORECAST`) and the history it was
 * fitted to (`DEMAND_HISTORY`), so the percentage quoted is the one the chart
 * below the banner draws. Returns an explicit "no forecast" rather than a
 * number when the engine produced nothing.
 */
function homeForecastSentence() {
  const meta = window.__ngForecastMeta || null;
  // `northIndia` is the prototype's name for the plotted series; it holds
  // whichever market-product pair the engine returned for THIS network.
  const mean = (FORECAST && FORECAST.northIndia) || [];
  const observed = (DEMAND_HISTORY && DEMAND_HISTORY.northIndia) || [];
  const lastObserved = [...observed].reverse().find((v) => typeof v === 'number');
  const horizon = mean.filter((v) => typeof v === 'number');

  if (!meta || !meta.series || !horizon.length || !lastObserved) {
    return 'No demand forecast is available for this network yet.';
  }
  const end = horizon[horizon.length - 1];
  const changePct = ((end - lastObserved) / lastObserved) * 100;
  const direction = changePct >= 0 ? 'increase' : 'decrease';
  const label = meta.shown || 'demand';
  return `I forecast ${label} to ${direction} ${Math.abs(changePct).toFixed(1)}% `
    + `over the next ${horizon.length} periods.`;
}
function renderHomeForecast() {
  const banner = document.getElementById('home-forecast-banner');
  if (banner) {
    // Was the literal sentence "I forecast North India demand to increase 14%
    // over the next 3 months." — a claim about the prototype's own region and
    // a growth rate no engine produced, shown on Home for every network that
    // was ever loaded. It now states the series the engine actually forecast
    // and the change it actually projects, or says there is no forecast.
    banner.textContent = homeForecastSentence();
  }

  // Render compact forecast chart
  setTimeout(() => {
    renderForecastChart('chart-forecast-home');
  }, 40);
}

// ─── Home Digital Twin Map Preview (3D — same engine as the Digital
//     Twin tab, re-parented into Home's preview container) ──────────
function renderHomeDigitalTwin() {
  setTimeout(() => {
    try {
      // initTwin3D re-parents/resumes the existing scene when already
      // initialised, so it's safe to call every time Home renders.
      initTwin3D('home-map-twin');
      window.dispatchEvent(new Event('resize'));
    } catch (e) {
      console.warn('Home 3D twin init:', e);
    }
  }, 50);
  renderHomeTwinCallout();
}

// Floating "key info" card on the Digital Twin preview — whichever
// facility is selected in the topbar (Facility selector) surfaces its
// utilisation snapshot directly on the map, matching
// Dump/Updated Home Page.png's "Insight context" callout.
function renderHomeTwinCallout() {
  const el = document.getElementById('home-twin-callout');
  if (!el) return;

  const fac = state.selectedFacility && state.selectedFacility !== 'ALL'
    ? getFacilityById(state.selectedFacility) : null;

  if (!fac) {
    el.innerHTML = '';
    el.classList.remove('visible');
    return;
  }

  // Utilisation is a solver output. Until a solve has produced one it is
  // absent, and absent must read as "—" — it used to interpolate straight
  // into the template and render the literal text "undefined%".
  const hasUtil = typeof fac.utilPct === 'number' && Number.isFinite(fac.utilPct);
  const utilLabel = hasUtil ? getUtilLabel(fac.utilPct) : null;
  const tone = utilLabel === 'Critical' ? 'red' : utilLabel === 'Stress' ? 'amber'
             : utilLabel ? 'green' : 'muted';

  el.innerHTML = `
    <div class="home-twin-callout-head">
      <span class="home-twin-callout-icon">✨</span>
      <span>Facility snapshot</span>
    </div>
    <div class="home-twin-callout-name">${fac.name} utilization</div>
    <div class="home-twin-callout-value tone-${tone}">${hasUtil ? fac.utilPct + '%' : '—'}</div>
    <div class="home-twin-callout-sub">${formatNumber(fac.throughput)} / ${formatNumber(fac.capacity)} ${perPeriodLabel()} capacity</div>
  `;
  el.classList.add('visible');
}

// ─── Home Numbered Insights (Right Rail) ─────────────────────
// ─── Attention feed categorisation ───────────────────────────
// Buckets an insight's `impact` (or an action's `tag` — the two use
// overlapping wording) into the small taxonomy shown in
// Dump/Home Overview-updated.png. Order matters: check the more
// specific phrase ("high value", "high impact") before the generic one.
const ATTENTION_CATEGORY_META = {
  'Recommendation': { icon: '✨', bg: '#f5f0fa', color: '#6B2FA0', link: 'Review' },
  'Capacity Risk': { icon: '⚠️', bg: '#fef2f2', color: '#dc2626', link: 'Investigate' },
  'Service Risk': { icon: '🛡️', bg: '#fffbeb', color: '#b45309', link: 'View details' },
  'Network Opportunity': { icon: '📈', bg: '#f0fdf4', color: '#16a34a', link: 'Review' },
  'Performance Update': { icon: '✅', bg: '#f0fdf4', color: '#16a34a', link: 'View details' },
  'Status': { icon: 'ℹ️', bg: '#eff6ff', color: '#2563eb', link: 'View details' },
};

// Retained for the records that genuinely only carry prose — the scenario
// comparison actions in `scenarios.js`, whose `tag` field is the only signal
// they have. An insight from `/api/insights` carries an explicit `category`
// derived from the engine's own severity, and never reaches this function.
function categorizeAttentionLabel(text) {
  const t = (text || '').toLowerCase();
  if (t.includes('high value')) return 'Recommendation';
  if (t.includes('high impact')) return 'Capacity Risk';
  if (t.includes('medium impact')) return 'Service Risk';
  if (t.includes('opportunity') || t.includes('optimization')) return 'Network Opportunity';
  if (t.includes('positive') || t.includes('normal')) return 'Performance Update';
  return 'Status';
}

function attentionCardHtml(kind, id, category, title, subtitle, index) {
  const meta = ATTENTION_CATEGORY_META[category] || ATTENTION_CATEGORY_META['Status'];
  const link = kind === 'action' ? 'Run scenario' : meta.link;
  const featured = index === 0;
  // There was a "₹NNL/month at risk" line here, hashed from the insight's id
  // between ₹8L and ₹32L. It was a made-up currency figure in the most
  // prominent position on the page, and it was labelled "purely cosmetic
  // emphasis" — but nothing on screen said so, and a reader has no way to tell
  // a cosmetic rupee figure from a computed one. The engine does not produce an
  // amount-at-risk, so none is shown.
  return `
    <div class="home2-attn-item${featured ? ' featured' : ''}" data-kind="${kind}" data-id="${id}" title="Click for details">
      <span class="home2-attn-badge-num">${index + 1}</span>
      <span class="home2-attn-icon" style="background:${meta.bg};color:${meta.color}">${meta.icon}</span>
      <div class="home2-attn-body">
        <div class="home2-attn-kicker" style="color:${meta.color}">${category}</div>
        <div class="home2-attn-item-title">${title}</div>
        <div class="home2-attn-item-sub">${subtitle}</div>
        <span class="home2-attn-link">${link}
          <svg width="13" height="13" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M5 10h10M11 6l4 4-4 4"/></svg>
        </span>
      </div>
    </div>`;
}

// ─── Home Attention Feed (merged Insights + Recommendations) ─
// Replaces the two separate "Here is what I found" / "Here are my
// recommendations" preview cards with one scrollable feed, per
// Dump/Home Overview-updated.png. Clicking a card navigates to the
// full-page insight deep dive — see insight-detail.js.
// Facilities whose scoped briefing has already been asked for, so a re-render
// never re-requests one. Without this, fetching inside a render function is a
// loop: the response fires `insightsLoaded`, which re-renders, which fetches.
const requestedFacilityInsights = new Set();

function renderHomeAttentionFeed() {
  const list = document.getElementById('home-attention-list');
  if (!list) return;

  // A scoped briefing costs a reasoning pass, so it is fetched for the facility
  // actually being looked at rather than for all of them at load time. The
  // response re-renders this feed through the `insightsLoaded` listener below.
  const selected = state.selectedFacility;
  if (selected && selected !== 'ALL' && !requestedFacilityInsights.has(selected)) {
    requestedFacilityInsights.add(selected);
    // Dynamically imported, matching how this file already reaches hydrate.js:
    // a static import would pull the whole integration layer into the initial
    // bundle for a feature that only fires once a facility is chosen.
    import('./integration/hydrate.js')
      .then((m) => m.loadFacilityInsights(selected))
      .catch(() => { /* the feed renders without the facility's own findings */ });
  }

  // Network findings first, then the selected facility's own.
  //
  // The feed used to show ONLY `getInsightsForFacility(selectedFacility)`,
  // which meant a finding about the network — an unserved-demand shortfall, a
  // cost structure, a footprint the plan does not use — had nowhere to appear
  // at all. Nothing wrote either store, so in practice the list was always
  // empty; now that both are written, network-level findings need somewhere to
  // land, and this is the screen they belong on.
  //
  // De-duplicated by HEADLINE, not by id.
  //
  // A facility briefing restates the network-level themes — cost, service —
  // because those figures are network-wide whatever scope you ask about. Their
  // generated ids differ by scope, so an id-based filter let them through and
  // the feed showed "I see the current cost position clearly" twice and "I see
  // all stated demand served" twice, out of nine cards. The same sentence twice
  // is noise, not information.
  //
  // The facility's own findings are added second, so the network's copy of a
  // shared theme wins and keeps its network scope — which is what its wording
  // describes.
  const seen = new Set();
  const insights = [
    ...getNetworkInsights(),
    ...getInsightsForFacility(state.selectedFacility),
  ].filter((ins) => {
    if (!ins || resolvedInsightIds.has(ins.id)) return false;
    const key = (ins.title || ins.id || '').trim().toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  // RISK before OPPORTUNITY before INFORMATION, and the engine's own ranking
  // within each band. A numbered feed implies an order of importance, and
  // insertion order is not one.
  const SEVERITY_ORDER = { RISK: 0, OPPORTUNITY: 1, INFORMATION: 2 };
  insights.sort((a, b) => {
    const s = (SEVERITY_ORDER[a.severity] ?? 2) - (SEVERITY_ORDER[b.severity] ?? 2);
    return s !== 0 ? s : (a.rank || 0) - (b.rank || 0);
  });

  const insightItems = insights.map(ins => ({
    kind: 'insight',
    id: ins.id,
    // `category` is set from the engine's severity when the record came from
    // `/api/insights`; the keyword fallback covers a record that predates it.
    category: ins.category || categorizeAttentionLabel(ins.impact),
    title: ins.title,
    subtitle: ins.subtitle,
  }));

  const actionItems = HOME_ACTION_ITEMS
    .filter(act => !resolvedInsightIds.has(act.id))
    .map(act => {
      const impact = act.expectedImpact || {};
      const subtitle = [impact.cost, impact.sla ? `SLA ${impact.sla}` : null].filter(Boolean).join(' · ');
      return { kind: 'action', id: act.id, category: categorizeAttentionLabel(act.tag), title: act.title, subtitle };
    });

  // Numbered 1..N across the whole merged feed (not restarted per source),
  // matching Dump/Updated Home Page.png — with the very first item getting
  // extra emphasis (see attentionCardHtml's "featured" treatment).
  const cards = [...insightItems, ...actionItems].map((item, i) =>
    attentionCardHtml(item.kind, item.id, item.category, item.title, item.subtitle, i));

  // An empty feed means no insight has been generated for this network — it
  // does NOT mean the network is healthy. The old copy ("network is
  // performing within target") asserted a clean bill of health from the
  // absence of evidence, which is the one conclusion absence cannot support.
  list.innerHTML = cards.length
    ? cards.join('')
    : `<div class="home2-attn-empty">No insights have been generated for this network yet.</div>`;

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

  // A briefing that arrives after the first paint — the network one during
  // hydration, or a facility one fetched on selection — redraws the feed and
  // the recommendation. Without this the findings were fetched and stored and
  // simply never shown until the next unrelated re-render.
  window.addEventListener('insightsLoaded', () => {
    try {
      renderHomeAttentionFeed();
      renderNetworkRecommendation();
    } catch (e) { /* a redraw must never break the page */ }
  });

  // Switching project invalidates every cached briefing: the ids are scoped to
  // a network, and a facility id can legitimately repeat across two of them.
  window.addEventListener('networkModelCleared', () => {
    requestedFacilityInsights.clear();
  });
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

/**
 * Update the Digital Twin's stat overlays from the network that is loaded.
 *
 * The node count was hardcoded to 19 in the markup with no id, and the corridor
 * count to 20 with an id nothing wrote to — so both kept describing the
 * prototype's demo footprint no matter whose network was on screen.
 *
 * The third tile is "Overall Risk", as in the approved design. Risk is only
 * ever computed per facility (`risk_factor`, owned by
 * netgravity.orchestrator.risk.risk_factor) — there is no network-level risk
 * metric with an authoritative owner, and aggregating the facility figures
 * here would make this screen a second KPI engine. So it stays an em dash
 * until the backend owns that metric, rather than showing an invented band.
 */
function renderTwinStats() {
  const nodeCount = PLANTS.length + DCS.length + MARKETS.length;
  const laneCount = LANES.length;

  [['map2d-node-count', nodeCount], ['twin3d-node-count', nodeCount],
   ['map2d-flow-count', laneCount], ['twin3d-flow-count', laneCount]]
    .forEach(([id, value]) => {
      const el = document.getElementById(id);
      if (el) el.textContent = String(value);
    });

  ['map2d-risk-label', 'twin3d-risk-label'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = '—';
    el.title = 'No network-level risk metric has an authoritative owner yet; '
             + 'risk is computed per facility.';
  });
}

/** The solver's open/closed decision for a facility, or "not solved". */
function openStatusTag(node) {
  if (node.isOpen === true) return '<span class="tag tag-success">Open</span>';
  if (node.isOpen === false) return '<span class="tag tag-muted">Closed by solver</span>';
  return '<span class="tag tag-muted">Not solved</span>';
}

function renderTwinTables() {
  renderTwinStats();

  // Plants
  const plantBody = document.querySelector('#table-plants tbody');
  if (plantBody) {
    // Status is the SOLVER's open/closed decision, not a fixed green tag. This
    // printed "Active" on every plant unconditionally — including the two the
    // optimiser had closed in the very solve the throughput column beside it
    // came from.
    plantBody.innerHTML = PLANTS.map(p => `
      <tr class="clickable-row" data-id="${p.id}">
        <td>${p.name}</td>
        <td class="num">${formatNumber(p.capacity)}</td>
        <td class="num">${formatNumber(p.throughput)}</td>
        <td>${openStatusTag(p)}</td>
      </tr>
    `).join('');
  }

  // DCs
  const dcBody = document.querySelector('#table-dcs tbody');
  if (dcBody) {
    // Utilisation is a solver output. Until a solve produces one it is absent,
    // and absent must render as "—" — interpolating it straight into the
    // template printed the literal text "undefined%" for every DC whenever the
    // network had no feasible solution.
    dcBody.innerHTML = DCS.map(d => {
      const hasUtil = typeof d.utilPct === 'number' && Number.isFinite(d.utilPct);
      const color = hasUtil ? getUtilColor(d.utilPct) : 'var(--text-3)';
      const label = hasUtil ? getUtilLabel(d.utilPct) : 'Not solved';
      const tagClass = hasUtil ? getUtilTagClass(d.utilPct) : 'tag-muted';
      return `
        <tr class="clickable-row" data-id="${d.id}">
          <td>${d.name}</td>
          <td class="num">${formatNumber(d.capacity)}</td>
          <td class="num"><span style="color:${color};font-weight:700">${hasUtil ? d.utilPct + '%' : '—'}</span></td>
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
        <td>${m.slaDays == null ? '—' : m.slaDays + 'd'}</td>
        <td><span class="tag ${m.priority === 'High' ? 'tag-danger' : m.priority === 'Medium' ? 'tag-warning' : 'tag-muted'}">${m.priority || '—'}</span></td>
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
window.openFacilityPanel = function (facilityId) {
  const fac = [...PLANTS, ...DCS].find(f => f.id === facilityId);
  if (!fac) return;

  const isPlant = isPlantFacility(fac.id);
  const isDC = isDCFacility(fac.id);
  const utilPct = isDC ? fac.utilPct
    : (fac.throughput != null && fac.capacity
        ? ((fac.throughput / fac.capacity) * 100).toFixed(1) : null);
  const utilColor = getUtilColor(utilPct);
  const utilLabel = getUtilLabel(utilPct);

  // S2 P0 #5: every facility needs a risk/bottleneck status, not just
  // Delhi — derived from the same utilLabel already computed above (no new
  // calculation), so the band can never drift from what the map/DC table
  // already show for this facility.
  const riskStatus = utilLabel === 'Critical' ? 'HIGH — Capacity Breach'
    : utilLabel === 'Stress' ? 'MEDIUM — Approaching Capacity'
      : 'LOW — Healthy Headroom';

  // Was `facilityId === 'DC_DELHI'`, which pinned a hardcoded "Forecast Dec
  // 2026 — 10,800 units/day" panel to one prototype facility and showed it for
  // any network that happened to contain that id. Nothing in this build
  // forecasts a per-facility capacity breach, so the panel is off until
  // something does.
  const isBaddi = false;
  const forecastSection = isBaddi ? `
    <div class="fp-stat" style="border-bottom:2px solid var(--red)">
      <span class="fp-stat-label">Forecast Dec 2026</span>
      <span class="fp-stat-value" style="color:var(--red)">10,800 units/day</span>
    </div>
  ` : '';

  document.getElementById('fp-content').innerHTML = `
    <div class="fp-title">${fac.name}</div>
    <div class="fp-subtitle">${fac.city}, ${fac.state} · ${fac.region} Region</div>
    <div class="fp-stat"><span class="fp-stat-label">Type</span><span class="fp-stat-value">${isPlant ? 'Manufacturing Plant' : 'Distribution Centre'}</span></div>
    <div class="fp-stat"><span class="fp-stat-label">Status</span><span class="fp-stat-value">${openStatusTag(fac)}</span></div>
    <div class="fp-stat"><span class="fp-stat-label">Capacity</span><span class="fp-stat-value">${formatNumber(fac.capacity)} ${perPeriodLabel()}</span></div>
    <div class="fp-stat"><span class="fp-stat-label">Current Throughput</span><span class="fp-stat-value">${formatNumber(fac.throughput)} ${perPeriodLabel()}</span></div>
    <div class="fp-stat"><span class="fp-stat-label">Utilisation</span><span class="fp-stat-value" style="color:${utilColor}">${utilPct}% <span class="tag ${getUtilTagClass(utilPct)}">${utilLabel}</span></span></div>
    ${isDC && fac.fixedCostPerYear != null ? `<div class="fp-stat"><span class="fp-stat-label">Fixed Cost</span><span class="fp-stat-value">${formatCurrency(fac.fixedCostPerYear)}/year</span></div>` : ''}
    ${isDC && fac.handlingCost != null ? `<div class="fp-stat"><span class="fp-stat-label">Handling Cost</span><span class="fp-stat-value">₹${fac.handlingCost}/unit</span></div>` : ''}
    ${forecastSection}
    <div class="fp-stat"><span class="fp-stat-label">Risk / Bottleneck</span><span class="fp-stat-value"><span class="tag ${getUtilTagClass(utilPct)}">${riskStatus}</span></span></div>
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

// --- Recommendation -----------------------------------------
// `renderRecommendation()` used to live here: 108 lines rendering a
// `#recommendation-panel` element that does not exist in index.html, from a
// `RECOMMENDATION` object that `clearDemoNarrative()` empties for every real
// network. It was called from nowhere. Wired up as it stood, it would have
// printed a NaN percentage for cost, SLA, utilisation and carbon, an empty
// analyst email, and Approve/Reject buttons that only changed their own label.
//
// Deleted rather than repaired: the recommendation this engine actually
// produces is one sentence chosen by the evidence, with the drivers behind it,
// and that is what `renderNetworkRecommendation()` shows.

/** Escape text for interpolation into innerHTML. */
function escapeInsightText(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function renderNetworkRecommendation() {
  const el = document.getElementById('home-recommendation');
  if (!el) return;
  const rec = NETWORK_RECOMMENDATION;

  if (!rec.text) {
    // Absence of a recommendation is not a clean bill of health, and the copy
    // says only what is true: nothing has been produced yet.
    el.innerHTML = '<div class="home2-rec-empty">No recommendation has been '
      + 'generated for this network yet.</div>';
    return;
  }

  const drivers = (rec.keyDrivers || []).length
    ? '<ul class="home2-rec-drivers">'
      + rec.keyDrivers.map((d) => `<li>${escapeInsightText(d)}</li>`).join('')
      + '</ul>'
    : '';
  const limitation = rec.limitation
    ? `<div class="home2-rec-limitation">Limitation: ${escapeInsightText(rec.limitation)}</div>`
    : '';
  // Some analyses did not run. Their values are unknown, not zero, and a
  // recommendation formed without them says so.
  const partial = (rec.evidenceCompleteness && rec.evidenceCompleteness !== 'COMPLETE')
    ? `<div class="home2-rec-warning">Evidence is ${escapeInsightText(rec.evidenceCompleteness)}`
      + ' - some analyses did not run, so their values are unknown rather than zero.</div>'
    : '';
  // Whether the figures in the prose were checked against the deterministic
  // results. Shown only when the check did NOT pass: anyone acting on a
  // recommendation is entitled to know its numbers were not verified.
  const grounding = (rec.groundingStatus
                     && rec.groundingStatus !== 'GROUNDED'
                     && rec.groundingStatus !== 'NO_CLAIMS')
    ? `<div class="home2-rec-warning">Numeric grounding: ${escapeInsightText(rec.groundingStatus)}`
      + ' - treat the figures above as unverified.</div>'
    : '';

  el.innerHTML = `
    <div class="home2-rec-label">What I recommend</div>
    <div class="home2-rec-text">${escapeInsightText(rec.text)}</div>
    ${drivers}${limitation}${partial}${grounding}`;
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
          <span class="tag tag-muted">Magnitude: ${sig.magnitude}</span>
          <span class="tag ${sig.confidence === 'HIGH' ? 'tag-success' : 'tag-warning'}">Conf: ${sig.confidence}</span>
          <span class="tag tag-muted" title="No Signal_Type field exists in the current schema — every signal shares the same generic 'signal' type today">Category: Not available</span>
          <span class="tag tag-muted" title="No severity/materiality threshold field exists in the current schema to compute a guardrail bucket">Guardrail status: Not available</span>
        </div>
        <div class="text-xs mt-sm" style="color:var(--primary);font-weight:600">→ ${sig.intendedUse}</div>
      </div>
    `).join('');
  }
}

// ─── S12: Signal Guardrails & Admin ──────────────────────────
// Sourced from GOVERNANCE_TIERS and SYSTEM_STATUS (data.js) — both fully
// authored but never wired into any screen before this. Trigger
// keywords/buckets and Product Master have no backing data anywhere in
// this build, so they show "Not available" rather than invented content.
function renderAdminSettingsModal() {
  const body = document.getElementById('admin-settings-body');
  if (!body) return;

  const tiersHtml = GOVERNANCE_TIERS.map(t => `
    <div style="padding:10px 0">
      <span class="tag" style="background:${t.color}22;color:${t.color};font-weight:700">Tier ${t.tier} — ${t.label}</span>
      <div class="text-sm mt-xs" style="color:var(--text-1)">${t.description}</div>
      <div class="text-xs text-muted mt-xs">Materiality threshold: ${t.criteria}</div>
    </div>
  `).join('<hr style="border:none;border-top:1px solid var(--border-light);margin:0">');

  const opt = SYSTEM_STATUS.optimisation;
  const fc = SYSTEM_STATUS.forecast;
  const d = SYSTEM_STATUS.data;
  const ai = SYSTEM_STATUS.ai;

  body.innerHTML = `
    <div class="card-title" style="font-size:13px;margin:10px 0 6px">Guardrail Configuration &amp; Materiality Thresholds</div>
    <div style="border:1px solid var(--border-light);border-radius:var(--r-sm);padding:0 12px">${tiersHtml}</div>

    <div class="card-title" style="font-size:13px;margin:18px 0 6px">Optimization Configuration</div>
    <div class="grid-2" style="gap:10px;font-size:12.5px">
      <div><span class="text-muted">Solver</span><br><strong>${opt.solver}</strong></div>
      <div><span class="text-muted">Status</span><br><strong>${opt.status}</strong></div>
      <div><span class="text-muted">Last run</span><br><strong>${new Date(opt.lastRun).toLocaleString('en-IN')}</strong></div>
      <div><span class="text-muted">Forecast model</span><br><strong>${fc.model}</strong></div>
      <div><span class="text-muted">Forecast horizon</span><br><strong>${fc.horizon}</strong></div>
      <div><span class="text-muted">Data quality</span><br><strong>${d.qualityPct}% (${d.facilities} facilities, ${d.lanes} lanes)</strong></div>
      <div><span class="text-muted">AI agent</span><br><strong>${ai.agentStatus} — ${ai.model}</strong></div>
    </div>

    <div class="card-title" style="font-size:13px;margin:18px 0 6px">Trigger Keywords / Buckets</div>
    <div class="text-xs" style="color:var(--text-2);font-style:italic">Not available — no keyword/bucket configuration exists in this build yet.</div>

    <div class="card-title" style="font-size:13px;margin:18px 0 6px">Product Master / Configuration</div>
    <div class="text-xs" style="color:var(--text-2);font-style:italic">Not available — no Product Master data exists in this build yet.</div>
  `;
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
  const facId = state.selectedFacility;
  const fac = getFacilityById(facId) || DCS[0] || PLANTS[0];
  if (!fac) { showNotification('No facility is loaded to export.'); return; }
  const kpis = getKpisForFacility(facId);
  const insights = getInsightsForFacility(facId);

  const lines = [
    '=== NetGravity Executive Facility Performance & Analytics Report ===',
    'Generated Date,' + new Date().toLocaleDateString(),
    'Facility Name,' + fac.name,
    'Facility Type,' + (isPlantFacility(fac.id) ? 'Manufacturing Plant' : 'Distribution Centre'),
    'Location,"' + fac.city + ', ' + fac.state + ' (' + fac.region + ' Region)"',
    'Active Period,' + (state.selectedPeriod || 'as uploaded'),
    '',
    '=== Operational Telemetry & Capacity Horizon ===',
    // An exported figure is evidence a reader may act on, so a metric the
    // engine did not produce is exported as "Not available" — never as a
    // plausible-looking number, and never with a status ("Target Met",
    // "Healthy") asserted over a value that does not exist. Peak utilisation
    // has no forecast behind it at all, so it is always reported as absent.
    'Capacity (Units/Day),' + (fac.capacity ?? 'Not available'),
    'Current Throughput (Units/Day),' + (fac.throughput ?? 'Not available'),
    'Utilisation Rate,' + (fac.utilPct == null ? 'Not available' : fac.utilPct + '%'),
    'Projected Peak Utilisation,Not available (no demand forecast for this facility)',
    '',
    '=== Core Performance KPIs ===',
    'Metric,Value,Target Benchmark,Status',
    'On-Time Service SLA,' + (kpis.sla || 'Not available') + ',>=95.0%,' + (kpis.sla ? 'Reported' : 'Not available'),
    'Monthly Operating Cost,' + (kpis.cost || 'Not available') + ',Budget Aligned,' + (kpis.cost ? 'Reported' : 'Not available'),
    'Inventory Days of Supply,' + (kpis.invDays || 'Not available') + ',10-14 Days,' + (kpis.invDays ? 'Reported' : 'Not available'),
    'Order Fill Rate,' + (kpis.fillRate || 'Not available') + ',>=98.0%,' + (kpis.fillRate ? 'Reported' : 'Not available'),
    '',
    '=== AI Prescriptive Diagnosis & Risk Telemetry ===',
    'Insight ID,Severity,Diagnosis Summary'
  ];

  if (insights && insights.length > 0) {
    insights.forEach(function (ins) {
      const desc = ins.title || ins.desc || '';
      lines.push('"' + ins.id + '","' + (ins.impact || 'Critical') + '","' + desc.split('"').join('""') + '"');
    });
  } else {
    // No insight has been generated for this network. Exporting two invented
    // ones about the prototype's demo footprint would put fabricated findings
    // into a file the user may circulate as analysis.
    lines.push('"","No insight","No insight has been generated for this network yet."');
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
