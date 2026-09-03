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
  GOVERNANCE_TIERS, GOVERNANCE_TIERS_CURRENCY, SYSTEM_STATUS,
  formatCurrency, formatNumber, fmtNum, getUtilColor, getUtilLabel, getUtilTagClass,
  getFacilityById, getInsightsForFacility, getKpisForFacility, getOptimizedBaseCase,
  getNetworkInsights, NETWORK_RECOMMENDATION, OBSERVED_UTILISATION,
  isDCFacility, isPlantFacility, facilityRole, clearNetworkModel,
  perPeriodLabel, SOLVE_HORIZON, horizonLabel,
  formatCurrencyExact, currencySymbol, currencyLabel, NETWORK_GEOGRAPHY,
  getActiveCurrency, FORECAST_CATALOGUE, selectForecastSeries
} from './data.js';
import { initMap, setNetworkState, invalidateMapSize, refreshAllMaps,
         revealMap, renderMapLegendCounts } from './map.js';
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
import { showInfoPanel, signOut } from './workspace-chrome.js';
import { getActiveProjectId, setActiveProject } from './integration/project-context.js';
import { loadIdentity, getCurrentUser } from './identity.js';
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
  // Called by ingestion.js the moment an analysis finishes, which can be
  // before Home has ever rendered.
  window.renderOverviewAlert = renderOverviewAlert;
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
  // The series ACTUALLY plotted, which changes when the picker changes.
  // `meta.shown` is written once at hydration, so the title kept naming the
  // default series after the user selected a different one.
  const shownKey = FORECAST.seriesLabel || meta.shown || '—';
  const shownName = FORECAST.seriesName
    ? `${FORECAST.seriesName} (${shownKey})` : shownKey;
  set('fc-series', shownName);
  set('fc-periods', `${DEMAND_HISTORY.months.length} periods`);
  set('fc-series-count', `${meta.series} market-product pair(s)`);
  // The chart's title IS the series picker (see #fc-series-select), so there
  // is no separate title string to write; the picker names the series.
  set('fc-chart-tag', meta.status === 'OK' ? 'Observed + forecast' : meta.status);
  set('fc-chart-subtitle',
    `${DEMAND_HISTORY.months.length} observed periods + `
    + `${FORECAST.months.length}-period forecast · p10–p90 band`);
  set('fc-method-prov',
    'Produced by netgravity.forecasting, routed through the orchestrator '
    + 'capability "forecast.demand". No language model is involved in the '
    + 'figures on this chart.');
  renderForecastSeriesSelect();
  renderForecastAxisNote();
  renderForecastCapacityKey();
}

/* ═══════════════════════════════════════════════════════════════
   FORECAST PAGE — Dump/Demand forecast.png
   ═══════════════════════════════════════════════════════════════ */

/**
 * Draw the whole Forecast screen.
 *
 * The left column is the Overview's alert and attention card, rendered by the
 * SAME two functions into this page's containers — `renderOverviewAlert` and
 * `renderHomeAttentionFeed` both take the element they draw into. Nothing on
 * this page computes a finding, a figure or a recommendation of its own.
 *
 * On the recommendation: the reasoning agent has no FORECAST scope
 * (netgravity/orchestrator/schemas/reasoning.py lists NETWORK, FACILITY,
 * LANE, SCENARIO, COMPARISON, RESILIENCE, INGESTION) and `/api/forecast` is
 * deterministic — its own provenance block reports `llm_used: false`. So
 * there is no forecast-specific recommended action to integrate, and what
 * this card shows is the agent's NETWORK recommendation, labelled as one.
 * Inventing a forecast-shaped sentence to fill the space would be writing a
 * recommendation the engine never made.
 */
function renderForecastPage() {
  renderOverviewAlert('fc-alert');
  renderHomeAttentionFeed('fc-attn-body');
  renderHomeSignals('fc-signals-row');
  renderAnalysisTimestamp();
  renderForecastSummary();
  renderDataIntelligence();
  wireForecastPage();
  requestAnimationFrame(() => sizePageToWindow('.fc-main', '--fc-main-top'));
}

/**
 * Name the two halves of the x-axis under the plot.
 *
 * The two spans are sized in proportion to how many periods each covers, so
 * the words sit under the part of the axis they describe rather than at the
 * midpoints of two equal halves.
 */
function renderForecastAxisNote() {
  const note = document.getElementById('fc-axis-note');
  if (!note) return;
  const hist = DEMAND_HISTORY.months.length;
  const fore = FORECAST.months.length;
  const total = hist + fore;
  const histEl = document.getElementById('fc-axis-hist');
  const foreEl = document.getElementById('fc-axis-fore');
  if (!total || !hist || !fore) {
    note.hidden = true;
    return;
  }
  note.hidden = false;
  if (histEl) histEl.style.flexGrow = String(hist);
  if (foreEl) foreEl.style.flexGrow = String(fore);
}

/**
 * The capacity entry in the key is only shown when a capacity line is drawn.
 *
 * A key that lists a series the chart does not plot tells the reader the
 * capacity is somewhere on the picture and leaves them looking for it.
 */
function renderForecastCapacityKey() {
  const el = document.getElementById('fc-legend-capacity');
  if (!el) return;
  const cap = DEMAND_HISTORY.baddiCapacity;
  el.hidden = !(typeof cap === 'number' && Number.isFinite(cap));
}

/** Every control in the chart card's header, wired once. */
function wireForecastPage() {
  const card = document.querySelector('#tab-forecast .fc-chart-card');
  if (!card || card.dataset.wired === '1') return;
  card.dataset.wired = '1';

  const panel = document.getElementById('fc-method-panel');
  const methodBtn = document.getElementById('fc-methodology-btn');
  const menu = document.getElementById('fc-more-menu');
  const menuBtn = document.getElementById('fc-more-btn');

  const setMenu = (open) => {
    if (!menu || !menuBtn) return;
    menu.hidden = !open;
    menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  const setPanel = (open) => {
    if (!panel || !methodBtn) return;
    panel.hidden = !open;
    methodBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  };

  methodBtn?.addEventListener('click', () => {
    setMenu(false);
    setPanel(panel.hidden);
  });
  document.getElementById('fc-method-close')?.addEventListener('click', () => setPanel(false));

  menuBtn?.addEventListener('click', (e) => {
    e.stopPropagation();
    setMenu(menu.hidden);
  });
  document.getElementById('fc-menu-methodology')?.addEventListener('click', () => {
    setMenu(false);
    setPanel(true);
  });
  document.getElementById('fc-menu-download')?.addEventListener('click', () => {
    setMenu(false);
    downloadForecastSeriesCsv();
  });
  document.getElementById('fc-menu-signals')?.addEventListener('click', () => {
    setMenu(false);
    document.getElementById('fc-signals-card')
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  // Click-away and Escape, because a menu you cannot dismiss without picking
  // something from it is a trap (Nielsen #3).
  document.addEventListener('click', (e) => {
    if (menu && !menu.hidden && !e.target.closest('.fc-menu-wrap')) setMenu(false);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (menu && !menu.hidden) setMenu(false);
    else if (panel && !panel.hidden) setPanel(false);
  });

  // "View all signals" opens the full detail the upload carried, in place —
  // it used to be a link to this very page, which on this page is nowhere.
  const all = document.getElementById('fc-signals-all');
  const allBtn = document.getElementById('fc-view-all-signals');
  const allLabel = document.getElementById('fc-view-all-signals-label');
  allBtn?.addEventListener('click', () => {
    if (!all) return;
    const open = all.hidden;
    all.hidden = !open;
    allBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (allLabel) allLabel.textContent = open ? 'Hide signal detail' : 'View all signals';
  });

  // The same re-analysis the Overview's card offers, from the same handler.
  document.getElementById('fc-refresh-btn')?.addEventListener('click', () => {
    document.getElementById('home2-refresh-btn')?.click();
  });
}

/**
 * The plotted series as a CSV, exactly as the engine reported it.
 *
 * Period, observed quantity, forecast mean and the p10/p90 bounds — nothing
 * derived, nothing rounded. Blank where the engine has no value for that
 * period, never a zero.
 */
function downloadForecastSeriesCsv() {
  const hist = DEMAND_HISTORY.months || [];
  const fore = FORECAST.months || [];
  if (!hist.length && !fore.length) return;

  const rows = [['period', 'observed', 'forecast_mean', 'forecast_p10', 'forecast_p90']];
  const cell = (v) => (typeof v === 'number' && Number.isFinite(v)) ? String(v) : '';
  hist.forEach((period, i) => {
    rows.push([period, cell((DEMAND_HISTORY.northIndia || [])[i]), '', '', '']);
  });
  fore.forEach((period, i) => {
    rows.push([period, '', cell((FORECAST.northIndia || [])[i]),
               cell((FORECAST.lower || [])[i]), cell((FORECAST.upper || [])[i])]);
  });

  const csv = rows.map((r) => r.map((c) => {
    const t = String(c);
    return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
  }).join(',')).join('\n');

  const name = (FORECAST.seriesLabel || 'series').replace(/[^A-Za-z0-9_.-]+/g, '_');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `netgravity_forecast_${name}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/**
 * Make the picker exactly as wide as the option it is showing.
 *
 * A `<select>` reserves the width of its widest option. As the chart's title
 * that means the heading is as wide as the longest market name in a
 * fifty-nine-entry catalogue, with the selected one adrift at the left of it.
 * Measured against a mirror span in the same font, then capped.
 */
function sizeForecastSelect(select) {
  if (!select || !select.options.length) return;
  const text = (select.options[select.selectedIndex] || {}).textContent || '';
  let ruler = document.getElementById('fc-series-ruler');
  if (!ruler) {
    ruler = document.createElement('span');
    ruler.id = 'fc-series-ruler';
    ruler.setAttribute('aria-hidden', 'true');
    ruler.style.cssText = 'position:absolute;visibility:hidden;white-space:pre;'
      + 'top:-9999px;left:-9999px';
    document.body.appendChild(ruler);
  }
  const cs = getComputedStyle(select);
  ruler.style.font = cs.font;
  ruler.style.letterSpacing = cs.letterSpacing;
  ruler.textContent = text.trim();
  const w = Math.ceil(ruler.getBoundingClientRect().width) + 2;
  select.style.width = w > 0 ? `min(${w}px, 100%)` : '';
}

/**
 * The series picker for the forecast chart.
 *
 * Every market-product pair the engine forecast, by name, with the id as the
 * secondary detail. The screen used to plot one series — chosen for us, named
 * "M002/P001" — and report "59 series forecast" beside it with no way to see
 * any of the other 58.
 */
function renderForecastSeriesSelect() {
  const select = document.getElementById('fc-series-select');
  if (!select) return;

  if (!FORECAST_CATALOGUE.length) {
    select.innerHTML = '<option value="">No forecastable series</option>';
    select.disabled = true;
    return;
  }
  select.disabled = false;
  const current = FORECAST.seriesLabel;
  // Sized after the options are in, below.
  select.innerHTML = FORECAST_CATALOGUE.map((entry) => `
    <option value="${entry.key}"${entry.key === current ? ' selected' : ''}>
      ${entry.label} (${entry.key})
    </option>`).join('');

  sizeForecastSelect(select);

  if (!select.dataset.wired) {
    select.dataset.wired = '1';
    select.addEventListener('change', () => {
      if (!selectForecastSeries(select.value)) return;
      renderForecastChart('chart-forecast');
      renderForecastSummary();
    });
    // The title has to fit its own text: a select sized to the longest option
    // makes the heading as wide as the widest market name in the catalogue.
    select.addEventListener('change', () => sizeForecastSelect(select));
  }
}

// Populate the picker when the catalogue arrives, not only when the Forecast
// tab happens to render. `renderForecastSummary()` runs from `navigateToTab`
// behind a `chartsInitialised` guard, so on a session where Home had already
// drawn its forecast preview the picker was never filled and kept its
// placeholder "Loading…" option forever.
if (typeof window !== 'undefined') {
  window.addEventListener('forecastCatalogueLoaded',
    () => { try { renderForecastSeriesSelect(); } catch (e) { /* not mounted */ } });
}

window.addEventListener('networkDataLoaded', (e) => {
  const net = e.detail;
  if (net && net.dcs && net.dcs.length > 0) {
    state.selectedFacility = net.dcs[0].id;
  }
  try { initHomeSelectors(); } catch (err) { }
  try { renderHome(); } catch (err) { }
  try { renderTwinTables(); } catch (err) { }
  // The Forecast page shows the same alert, attention card and signals as
  // Home. Without this they stayed as they were at the moment the tab was
  // last opened, which for a network loaded afterwards is empty.
  try { if (document.getElementById('fc-alert')) renderForecastPage(); } catch (err) { }
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

  // Upload Data: on every page. It launches the same ingestion flow used
  // during onboarding (see initTabs' btn-topbar-upload handler), which is a
  // global action — it replaces the network the whole application is looking
  // at. Showing it only on Home meant the top bar changed shape between tabs
  // and that loading a file from the Digital Twin meant navigating away first.
  const btnUpload = document.getElementById('btn-topbar-upload');
  if (btnUpload) {
    btnUpload.style.display = 'flex';
  }

  // Scope lives in ONE place on every screen: the top bar's left area, in
  // order of how much each control narrows — project, then facility, then
  // period. It used to be here on Home and one row lower on every other tab,
  // which is Nielsen #4 twice over: the same three controls in two positions,
  // and a user who had just set a facility on Home looking for it in the
  // wrong row on the Digital Twin.
  //
  // Scenario Planning is the one exception, and by request: a scenario is
  // solved over the whole network for the horizon it was built with, so a
  // facility or period picker there would be a control that changes nothing.
  // Hiding it is more honest than showing a dead one.
  const scopeApplies = (tab !== 'scenarios');
  const topScope = document.getElementById('home-top-controls');
  if (topScope) {
    topScope.style.display = scopeApplies ? 'flex' : 'none';
  }

  // Home carries its own page head ("Overview · Your network health…"), so it
  // does not need the generic title row. Every other page does.
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
      subTitle.textContent = '· AI predictive projections to help you plan '
        + 'ahead with confidence';
    } else if (tab === 'twin') {
      mainTitle.innerHTML = 'Digital Twin';
      // Not 'India network topology'. The subtitle names the geography the
      // loaded network is actually in, or simply says what the screen is.
      subTitle.textContent = NETWORK_GEOGRAPHY.region
        ? `· ${NETWORK_GEOGRAPHY.region} network topology`
        : '· Network topology';
    } else if (tab === 'scenarios') {
      mainTitle.innerHTML = 'Scenario Planning';
      subTitle.textContent = '· Multi-echelon network optimization';
    } else if (tab === 'recommendations' || tab === 'recommend') {
      mainTitle.innerHTML = 'Recommendations';
      subTitle.textContent = '· Prescriptive AI actions';
    }
  }

  // The sub-topbar's Facility/Period pair is the SECOND copy of controls that
  // now live in the top bar on every page, so it is never shown. The elements
  // stay in the DOM deliberately: `#sel-facility` / `#sel-period` are the
  // application's source of truth for the current scope — every other screen
  // reads them and `initHomeSelectors()` keeps them in step with the visible
  // pair — so removing them would mean rewiring the scope of the whole app to
  // remove one duplicate row.
  const controls = document.getElementById('topbar-controls');
  if (controls) {
    controls.style.display = 'none';
  }
}

/**
 * Send the page area back to the top.
 *
 * `window.scrollTo` moved the window, which is the right call only while the
 * window is what scrolls. `.main-content` is the scroll container now (see
 * style.css), so scrolling the window is a no-op and arriving on a new tab
 * left the reader wherever the last one had been scrolled to.
 */
function scrollPageToTop() {
  const main = document.querySelector('.main-content');
  if (main && typeof main.scrollTo === 'function') {
    main.scrollTo({ top: 0, behavior: 'smooth' });
  } else if (main) {
    main.scrollTop = 0;
  }
}

if (typeof window !== 'undefined') window.scrollPageToTop = scrollPageToTop;

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
    scrollPageToTop();
    return;
  }

  // 2. Facility KPI Dashboard
  if (tab === 'facility-dashboard') {
    document.getElementById('tab-facility-dashboard')?.classList.add('active');
    state.activeTab = 'facility-dashboard';
    renderFacilityDashboard();
    scrollPageToTop();
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
        // The three tables below the map, and with them the legend's
        // facility counts. They were drawn on `networkDataLoaded` only, so
        // opening this tab after any later change showed the counts as of
        // the last load rather than as of now.
        renderTwinTables();
      } catch (err) {
        console.error('Twin initialization warning:', err);
      }
      window.dispatchEvent(new Event('resize'));
    }, 50);
  }
  if (tab === 'forecast') {
    // Every time, not once. The page carries the alert, the attention card
    // and the signals row, all of which are scoped by the current selection
    // and can change between two visits — the old `chartsInitialised` guard
    // meant a forecast opened before the first solve stayed empty for the
    // rest of the session.
    setTimeout(() => {
      try {
        renderForecastChart('chart-forecast');
        renderForecastPage();
      } catch (err) {
        console.error('Forecast render warning:', err);
      }
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

/**
 * Re-render whatever is on screen after the facility or period selection moved.
 *
 * There is one selection (`state.selectedFacility` / `state.selectedPeriod`)
 * and several screens that scope themselves by it, but every selector's change
 * handler called `renderHome()` and only `renderHome()`. `renderFacilityDashboard()`
 * was reachable from exactly one place — `navigateToTab` — so on the KPI screen
 * the dropdown moved, the state updated, and not one card, chart or corridor
 * row changed: Bangalore selected, Delhi's 11.36% and 3,230/28,430 still on
 * screen. A user reading a facility's numbers under another facility's name is
 * the worst failure mode this application has, because nothing about it looks
 * broken.
 *
 * Routing the refresh through the active tab means a new screen cannot
 * reintroduce the bug by forgetting to subscribe: it registers here once.
 */
/**
 * The sidebar's network identity, from the project that is open.
 *
 * Both values were literals in the markup — "India Network" and model version
 * "v7.0" — displayed for every project. The name is the project's own; the
 * region is what the data says, labelled as inferred when it was derived
 * rather than stated.
 */
function renderSidebarMeta() {
  const project = (typeof window.getCurrentProject === 'function')
    ? window.getCurrentProject() : null;
  const nameEl = document.getElementById('sidebar-network-name');
  if (nameEl) nameEl.textContent = project?.name || '—';
  const regionEl = document.getElementById('sidebar-network-region');
  if (regionEl) {
    const region = NETWORK_GEOGRAPHY.region || project?.region || '';
    regionEl.textContent = region || 'Not set';
    regionEl.title = NETWORK_GEOGRAPHY.basis || '';
  }
}

function renderForSelection() {
  // Home is always refreshed: its KPI strip and twin preview are scoped by the
  // same selection, and it is the screen a user returns to.
  renderHome();
  if (state.activeTab === 'facility-dashboard') {
    renderFacilityDashboard();
  } else if (state.activeTab === 'twin') {
    renderTwinTables();
  }
}

// ─── Sidebar Collapse (icon rail <-> full labels) ────────────
// Defaults to EXPANDED, with labels, matching Dump/home overview.png. An
// icon-only rail asks the reader to remember what a glyph means every time
// they look at it — Nielsen #6, recognition rather than recall — and this
// product's five destinations are not five universally-understood icons.
// The choice is still remembered per-browser, so anyone who prefers the rail
// keeps it.
function initSidebarCollapse() {
  const sidebar = document.getElementById('sidebar');
  const toggleBtn = document.getElementById('sidebar-toggle-btn');
  if (!sidebar || !toggleBtn) return;

  let collapsed = false;
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

  // Header actions open an in-product panel, not a browser alert().
  //
  // `alert()` blocks the whole page until dismissed, is not styleable, cannot
  // be closed by clicking away, and on a slow render leaves the app looking
  // stalled or blank behind the modal dialog. For two informational panels
  // that is a needless way to make a working product feel broken.
  document.getElementById('btn-topbar-notifications')?.addEventListener('click', () => {
    // This listed three fixed alerts about the prototype's demo footprint as
    // though they were live findings for whatever network was loaded.
    // Real threshold breaches for this network are reported on Home, sourced
    // from the KPI layer's triggered thresholds.
    showInfoPanel('Notifications', `
      <p>No active alert for this network.</p>
      <p class="text-sm" style="color:var(--text-2)">Threshold breaches appear on
      the Home cockpit when the engine reports them, naming the metric and the
      threshold that fired.</p>`);
  });

  document.getElementById('btn-topbar-help')?.addEventListener('click', () => {
    showInfoPanel('Help', `
      <p class="text-sm" style="color:var(--text-2)">AI decision intelligence for
      logistics networks.</p>
      <ul class="text-sm" style="margin:10px 0 0 18px;line-height:1.9">
        <li><strong>Overview</strong> &mdash; what needs attention, the headline
          KPIs, the twin and your external signals</li>
        <li><strong>KPIs</strong> &mdash; one facility at a time, with its corridors</li>
        <li><strong>Digital Twin</strong> &mdash; 2D and 3D network topology</li>
        <li><strong>Forecast</strong> &mdash; demand history and projection</li>
        <li><strong>Scenarios</strong> &mdash; what-if planning against your network</li>
      </ul>`);
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
      showInfoPanel('Profile', '<p>No signed-in session was found.</p>');
      return;
    }
    // A real screen, not an alert(): it is where a password is changed, a
    // second factor is enrolled and live sessions are revoked. Those are all
    // API endpoints now, and an endpoint nobody can reach is a feature on
    // paper. The alert is kept as the fallback if the module fails to load.
    import('./account-security.js')
      .then((m) => m.openAccountSecurity())
      .catch(() => showInfoPanel('Profile', 'User profile: ' + (user.name || '-')
        + ' | ' + (user.email || '-')
        + ' | role ' + (user.role || '-')
        + ' | ' + (user.organization || '-')));
  });
  document.getElementById('profile-menu-signout')?.addEventListener('click', () => {
    document.getElementById('profile-dropdown-menu')?.classList.remove('open');
    // One implementation, in js/workspace-chrome.js, shared with the account
    // menu on the project screens. Sign-out REVOKES the session and clears the
    // model: it used to call `returnToLanding()` only, so the bearer token
    // stayed in localStorage and the previous user's network stayed in memory
    // — the next person to use the browser was signed in as them, and a
    // refresh put them straight back into that account's projects. Two copies
    // of that would be two chances to get it wrong again.
    signOut();
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
          // Re-measure AND re-frame. The map was built while this panel was
          // hidden, so its idea of both its size and its zoom is from a 0x0
          // container. Two frames' grace for the panel to lay out.
          setTimeout(() => {
            window.dispatchEvent(new Event('resize'));
            revealMap('map-twin');
          }, 60);
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
  // Both cards that carry it: the Overview's and the Forecast page's.
  const targets = ['home2-refresh-time', 'fc-refresh-time']
    .map((id) => document.getElementById(id)).filter(Boolean);
  if (!targets.length) return;
  const at = (typeof window !== 'undefined') ? window.__ngAnalysisComputedAt : null;
  const seconds = at === null || at === undefined
    ? null : Math.max(0, Math.round(Date.now() / 1000 - at));

  let text;
  if (seconds === null) text = 'not yet analysed';
  else if (seconds < 60) text = 'just now';
  else if (seconds < 3600) text = `${Math.round(seconds / 60)} min ago`;
  else if (seconds < 86400) text = `${Math.round(seconds / 3600)} h ago`;
  else text = new Date(at * 1000).toLocaleString();

  targets.forEach((el) => { el.textContent = text; });
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
  // Two different period lists, and the control was reading the wrong one.
  //
  // `PERIODS` comes from `network.demands`, and the assembler keeps only the
  // LATEST period of an uploaded history — so for every real upload it is
  // `[1]`, the control had a single option, and `periods.length < 2` disabled
  // it. The dropdown was not broken; it was faithfully reporting a collapse
  // that happens two layers below it.
  //
  // `OBSERVED_UTILISATION.periods` is the client's own recorded capacity
  // history, which that collapse never touches: 36 months of stated available
  // and used capacity on the test workbook. That is a real list of real
  // periods, so it is what the user gets to explore.
  //
  // What it selects is the OBSERVED window — the recorded utilisation series
  // and the figures drawn from it. It does NOT re-solve the network: there is
  // one solved plan, built from the demand the model was given, and pretending
  // a dropdown re-optimises it would be a filter that does not exist. The
  // title says so, and `renderPeriodScopeNote()` says so on screen.
  const observed = (OBSERVED_UTILISATION.periods || []).map((id) => ({
    id: String(id), label: String(id),
  }));
  const periods = observed.length ? observed : (PERIODS || []);

  if (!periods.length) {
    select.innerHTML = '<option value="">No period stated in this data</option>';
    select.disabled = true;
    select.title = 'The uploaded data states no period for its demand rows.';
    return;
  }

  // Most recent first: a planner opening the control wants the latest month,
  // not the oldest one three years back.
  const ordered = observed.length ? [...periods].reverse() : periods;

  // Which of these the model actually solved.
  //
  // The recorded history runs longer than the horizon the MILP is given, so
  // some of these periods carry a solved reading of their own and the rest
  // fall back to the horizon average. Both are legitimate; showing them
  // identically is not, because two months displaying the same utilisation
  // would look like a finding rather than like one of them not being modelled.
  const solved = new Set(Object.values(SOLVE_HORIZON.periodLabels || {}));
  const someSolved = ordered.some((p) => solved.has(p.id));
  select.innerHTML = ordered
    .map((p) => {
      const isSolved = solved.has(p.id);
      const suffix = (someSolved && !isSolved) ? ' · recorded only' : '';
      return `<option value="${p.id}">${p.label}${suffix}</option>`;
    }).join('');
  if (!state.selectedPeriod
      || !ordered.some((p) => p.id === state.selectedPeriod)) {
    state.selectedPeriod = ordered[0].id;
  }
  select.value = state.selectedPeriod;
  select.disabled = ordered.length < 2;
  select.title = ordered.length < 2
    ? 'Your data states one demand period, and the analysis covers all of it.'
    : (someSolved
       ? `Selects the period the figures describe. The ${solved.size} period(s) `
         + 'the model solved show that period\'s own solved utilisation; the '
         + 'rest are outside the modelled horizon and show what your capacity '
         + 'history recorded, against the horizon average.'
       : 'Selects which recorded period the observed-utilisation figures cover. '
         + 'The optimised plan is one solve over the demand the model was given, '
         + 'and does not change with this control.');
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
      renderForSelection();
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
      renderForSelection();
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
      renderForSelection();
    });
  }

  const homeTopPeriod = document.getElementById('home-top-period');
  if (homeTopPeriod) {
    populatePeriodSelect(homeTopPeriod);
    homeTopPeriod.addEventListener('change', () => {
      state.selectedPeriod = homeTopPeriod.value;
      if (selPeriod) selPeriod.value = state.selectedPeriod;
      renderForSelection();
    });
  }

  // KPI Section "View by" (DC / Plant)
  if (selKpiType) {
    selKpiType.value = state.facilityType;
    selKpiType.addEventListener('change', () => {
      state.facilityType = selKpiType.value;
      if (selForecastType) selForecastType.value = state.facilityType;
      populateFacilitySelector();
      renderForSelection();
    });
  }

  // Forecast Section "View by" (DC / Plant)
  if (selForecastType) {
    selForecastType.value = state.facilityType;
    selForecastType.addEventListener('change', () => {
      state.facilityType = selForecastType.value;
      if (selKpiType) selKpiType.value = state.facilityType;
      populateFacilitySelector();
      renderForSelection();
    });
  }

  // Facility Picker (if visible/available)
  if (selFacilityPicker) {
    selFacilityPicker.addEventListener('change', () => {
      state.selectedFacility = selFacilityPicker.value;
      if (selFacility) selFacility.value = state.selectedFacility;
      renderForSelection();
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

/**
 * Tell a page's body grid how much window is left for it.
 *
 * The Overview's `.ov-main` and the Forecast page's `.fc-main` are both sized
 * to fill the rest of the FIRST screen, so the row below each of them — the
 * signals — begins below the fold. Each needs the distance from the top of
 * the window down to its own top edge: the global top bar, the page-title row
 * where there is one, the page's padding and the head row. Those vary with
 * the viewport and with which page is showing, and none of them can be
 * expressed in CSS from inside the grid, so the distance is measured once per
 * render and written back as a custom property.
 *
 * Reading the top of the element whose height we are about to set is not
 * circular: its top is fixed by what comes BEFORE it, and nothing before it
 * depends on its height.
 */
function sizePageToWindow(selector, varName) {
  const el = document.querySelector(selector);
  if (!el || el.offsetParent === null) return;
  const top = Math.round(el.getBoundingClientRect().top);
  if (!Number.isFinite(top) || top <= 0 || top > window.innerHeight) return;
  const shell = document.querySelector('.main-content');
  if (shell) shell.style.setProperty(varName, top + 'px');
}

function sizeOverviewToWindow() {
  sizePageToWindow('#tab-home.active .ov-main', '--ov-main-top');
}

if (typeof window !== 'undefined') {
  let sizingFrame = null;
  window.addEventListener('resize', () => {
    // Coalesced to one measurement per frame: a drag-resize fires this
    // continuously, and each call is a forced layout.
    if (sizingFrame) return;
    sizingFrame = requestAnimationFrame(() => {
      sizingFrame = null;
      try {
        sizeOverviewToWindow();
        sizePageToWindow('#tab-forecast.active .fc-main', '--fc-main-top');
      } catch (e) { /* not mounted */ }
    });
  });
}

// ─── Render Full Home ───────────────────────────────────────
function renderHome() {
  renderSidebarMeta();
  renderOverviewAlert();
  renderHomeForecast();
  renderHomeDigitalTwin();
  renderHomeAttentionFeed();
  renderHomeSignals();
  renderAnalysisTimestamp();
  // After the head row has its final text, so the measurement is of the head
  // that is actually on screen.
  requestAnimationFrame(sizeOverviewToWindow);
}

/* ═══════════════════════════════════════════════════════════════
   OVERVIEW — scope
   ═══════════════════════════════════════════════════════════════
   Facility and Period are `#home-top-facility` / `#home-top-period` in the
   global top bar, populated and bound by `initHomeSelectors()` against the
   same state every other tab reads, so there is no second copy of a value
   to fall out of step. This page owns no selector of its own.

   The page-local "View: network summary / selected facility" control is
   gone along with the three-KPI band it switched. Per-facility figures are
   not lost with it: the twin's own snapshot follows the Facility selector,
   and the facility-by-facility breakdown is the KPIs tab.
   ═══════════════════════════════════════════════════════════════ */

/* ═══════════════════════════════════════════════════════════════
   OVERVIEW — row 1a: the one thing that is wrong
   ═══════════════════════════════════════════════════════════════ */
/**
 * The headline state of the analysis, as a card.
 *
 * This replaces `#ng-network-notice` — a full-width amber banner whose text
 * was one paragraph containing every market's shortfall in prose. On the test
 * network that was six lines and about 900 characters of run-on sentence
 * above the fold, and the number a reader actually needed (452,610) was
 * buried in the middle of it.
 *
 * The card states the headline figure, one sentence of what it means, and a
 * link to the rows behind it. The per-market detail is not deleted — it is
 * what the linked view is for.
 */
function renderOverviewAlert(elId = 'ov-alert') {
  const el = document.getElementById(elId);
  if (!el) return;
  const notice = window.__ngNetworkNotice || null;
  const base = getOptimizedBaseCase()?.baseline || {};
  const unserved = (typeof base.unservedDemand === 'number') ? base.unservedDemand : null;
  const total = (typeof base.totalDemand === 'number') ? base.totalDemand : null;

  // Infeasible first: it outranks every other state, and none of the figures
  // below it describe a plan that exists.
  if (notice && notice.tone === 'error') {
    el.className = 'ov-alert tone-error';
    el.innerHTML = `
      ${OV_ICONS.alert}
      <div class="ov-alert-text">
        <div class="ov-alert-title">No feasible plan for this network</div>
        <div class="ov-alert-sub">${escapeInsightText(notice.detail || notice.message || '')}</div>
      </div>`;
    return;
  }

  if (unserved && unserved > 0) {
    el.className = 'ov-alert tone-warn';
    // The share goes in the sentence below, not on the figure line: appended
    // there it pushed "of demand)" onto a second line and shoved the whole
    // card down whenever the sidebar was expanded.
    const pct = total ? `${((unserved / total) * 100).toFixed(1)}% of demand. ` : '';
    el.innerHTML = `
      ${OV_ICONS.alert}
      <div class="ov-alert-text">
        <div class="ov-alert-title">Network capacity constraint detected</div>
        <div class="ov-alert-figure">${formatNumber(unserved)}
          <span>units of demand are unserved</span></div>
        <div class="ov-alert-sub">${pct}Unable to meet required service levels
          at multiple locations.</div>
        <button type="button" class="ov-alert-link">
          <span>View affected demand</span>
          <svg width="13" height="13" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M5 10h10M11 6l4 4-4 4"/></svg>
        </button>
      </div>`;
    el.querySelector('.ov-alert-link')?.addEventListener('click', () => {
      // The markets the shortfall is in, which is the detail the old banner
      // spelled out in prose.
      if (typeof window.navigateToTab === 'function') window.navigateToTab('twin');
    });
    return;
  }

  // Nothing wrong that the solve found. Said plainly, and never as a clean
  // bill of health the evidence does not support.
  const solved = total !== null;
  el.className = 'ov-alert tone-ok';
  el.innerHTML = `
    ${OV_ICONS.ok}
    <div class="ov-alert-text">
      <div class="ov-alert-title">${solved
        ? 'All stated demand is served'
        : 'No analysis has run yet'}</div>
      <div class="ov-alert-sub">${solved
        ? 'Every unit of demand in your upload is met within its service level.'
        : 'Upload a network dataset to populate this page.'}</div>
    </div>`;
}

const OV_ICONS = {
  alert: `<span class="ov-alert-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13.5"/><line x1="12" y1="17" x2="12" y2="17.01"/></svg></span>`,
  ok: `<span class="ov-alert-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.4"/><polyline points="8.4 12.2 11 14.8 15.8 9.6"/></svg></span>`,
  chart: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="20" x2="6" y2="13"/><line x1="12" y1="20" x2="12" y2="8"/><line x1="18" y1="20" x2="18" y2="4"/></svg>`,
};

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
  // Peak-period utilisation, from the solver. Null unless a multi-period solve
  // reported one that is genuinely above the average — on a single-period solve
  // the two are the same number, and printing both would imply a seasonal
  // reading the data cannot support.
  const peakRaw = kpis?.utilisation?.peak;
  const peakUtil = (typeof peakRaw === 'number' && Number.isFinite(peakRaw)
    && SOLVE_HORIZON.periodsModelled > 1 && peakRaw > (parseFloat(utilPct) || 0) + 0.05)
    ? peakRaw : null;

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
  if (elDashSubtitle) {
    // `fac.city`, `fac.state` and `fac.region` are not fields on a loaded
    // facility, so this read "undefined, undefined · undefined Region" for
    // every network. What the network does carry is the site's name, the
    // inferred geography and the horizon the figures cover.
    const where = NETWORK_GEOGRAPHY.region ? ` · ${NETWORK_GEOGRAPHY.region}` : '';
    const span = horizonLabel() || (period ? period.short : 'the period as uploaded');
    elDashSubtitle.textContent =
      `${fac.id}${where} · Operational telemetry and cost breakdown across ${span}.`;
  }

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
        <div class="dash-metric-title">Capacity & Throughput</div>
        <div class="dash-metric-val" style="color:${utilColor}">${utilPct}% <span style="font-size:14px;color:var(--text-3);font-weight:600">(${formatNumber(fac.throughput)}/${formatNumber(fac.capacity)} ${perPeriodLabel()})</span></div>
        <div class="dash-metric-sub">
          <span>Spare Capacity: <strong>${formatNumber(fac.capacity - fac.throughput)} ${perPeriodLabel()}</strong></span>
          <span class="tag ${getUtilTagClass(utilPct)}">${getUtilLabel(utilPct)}</span>
        </div>
        <!-- Utilisation in the busiest single period of the horizon. The
             figure above is the average across it, and a site with room on
             average can still be full in its peak month — which is the reading
             that decides whether it needs more space. Shown only when a
             horizon was actually modelled and the solver reported a peak. -->
        ${peakUtil === null ? '' : `
        <div class="dash-metric-sub">
          <span>Peak period: <strong style="color:${getUtilColor(peakUtil)}">${peakUtil.toFixed(1)}%</strong></span>
          <span class="text-muted">busiest of ${SOLVE_HORIZON.periodsModelled} modelled periods</span>
        </div>`}
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
          <span>Handling: <strong>${fac.handlingCost == null ? '—' : formatCurrencyExact(fac.handlingCost) + '/unit'}</strong></span>
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
      <div class="mt-xs">• Weighted average transportation rate across all active arcs is <strong>${formatCurrencyExact(avgCost)} / unit</strong>.</div>
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
        <td class="num">${formatNumber(l.flow)} ${perPeriodLabel()}</td>
        <td class="num">${formatNumber(l.distance)} km</td>
        <td class="num font-bold">${l.cost == null ? '—' : formatCurrencyExact(l.cost)}</td>
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
/**
 * Home's forecast preview.
 *
 * `#home-forecast-banner` and `#chart-forecast-home` were removed from the
 * Overview when it was rebuilt against the mockup, so both branches are
 * inert today. The function is kept, guarded, rather than deleted: it is the
 * one place that turns `homeForecastSentence()` into a rendered line, and
 * re-deriving that if a forecast preview returns to Home would be rewriting
 * it rather than restoring it. It costs nothing while the elements are absent.
 */
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
  if (!document.getElementById('chart-forecast-home')) return;
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

// ─── Home Attention Feed (merged Insights + Recommendations) ─
// Replaces the two separate "Here is what I found" / "Here are my
// recommendations" preview cards with one scrollable feed, per
// Dump/Home Overview-updated.png. Clicking a card navigates to the
// full-page insight deep dive — see insight-detail.js.
// Facilities whose scoped briefing has already been asked for, so a re-render
// never re-requests one. Without this, fetching inside a render function is a
// loop: the response fires `insightsLoaded`, which re-renders, which fetches.
const requestedFacilityInsights = new Set();

function renderHomeAttentionFeed(listId = 'ov-attn-body') {
  const list = document.getElementById(listId);
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
    // The first figure the finding cites, already formatted by the engine.
    // Undefined when it cites none, and the card then omits the line.
    headline: (ins.evidence && ins.evidence[0])
      ? ins.evidence[0].display_value : '',
  }));

  const actionItems = HOME_ACTION_ITEMS
    .filter(act => !resolvedInsightIds.has(act.id))
    .map(act => {
      const impact = act.expectedImpact || {};
      const subtitle = [impact.cost, impact.sla ? `SLA ${impact.sla}` : null].filter(Boolean).join(' · ');
      return { kind: 'action', id: act.id, category: categorizeAttentionLabel(act.tag), title: act.title, subtitle };
    });

  const items = [...insightItems, ...actionItems];

  // An empty feed means no insight has been generated for this network — it
  // does NOT mean the network is healthy. The old copy ("network is
  // performing within target") asserted a clean bill of health from the
  // absence of evidence, which is the one conclusion absence cannot support.
  if (!items.length) {
    list.innerHTML = `<div class="ov-attn-empty">No insights have been generated
      for this network yet.</div>`;
    return;
  }

  // The top-ranked finding, in full. The feed used to be a scrolling list of
  // every finding at equal weight, which asks the reader to trade off six
  // things before doing one — and the recommendation, rendered into a separate
  // block below it, overlapped the third card.
  //
  // The rest are not dropped: they are listed underneath, and every one still
  // opens its own deep dive.
  const lead = items[0];
  const rest = items.slice(1);
  const rec = getNetworkRecommendation();

  /* The finding's figure and its sentence, without saying the figure twice.
     An insight's `subtitle` usually restates its own headline evidence — "I
     see 452,610 units of 1,435,985 units of demand left unserved" already
     contains "452,610" — so printing the headline in front of it produced
     "452,610 units I see 452,610 units of ... left unserved". */
  function impactHtml(item) {
    const head = (item.headline || '').trim();
    const sub = (item.subtitle || '').trim();
    if (!head && !sub) return '';
    const restated = head && sub
      && sub.replace(/[\s,]/g, '').includes(head.replace(/[\s,]/g, '').replace(/units$/i, ''));
    const body = restated
      ? escapeInsightText(sub)
      : [head ? `<strong>${escapeInsightText(head)}</strong>` : '',
         escapeInsightText(sub)].filter(Boolean).join('<br>');
    return `
      <div class="ov-attn-section">
        <div class="ov-attn-section-label tone-impact">Impact</div>
        <div class="ov-attn-section-text">${body}</div>
      </div>`;
  }

  list.innerHTML = `
    <div class="ov-attn-lead" data-kind="${lead.kind}" data-id="${lead.id}">
      <div class="ov-attn-section">
        <div class="ov-attn-section-label tone-why">Why it matters</div>
        <div class="ov-attn-section-text">${escapeInsightText(lead.title)}</div>
      </div>
      ${impactHtml(lead)}
      <button type="button" class="ov-attn-more-link" data-open-lead>
        <span>View the full finding</span>
        <svg width="13" height="13" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M5 10h10M11 6l4 4-4 4"/></svg>
      </button>
    </div>

    ${rec ? `
    <div class="ov-attn-next">
      <span class="ov-attn-next-icon">${OV_ICONS.chart}</span>
      <div class="ov-attn-next-text">
        <div class="ov-attn-section-label tone-next">Recommended next step</div>
        <div class="ov-attn-next-title">${escapeInsightText(rec.headline)}</div>
        <div class="ov-attn-next-sub">${escapeInsightText(rec.text)}</div>
        ${rec.limitation ? `<div class="ov-attn-next-limit">${escapeInsightText(rec.limitation)}</div>` : ''}
      </div>
    </div>
    <button type="button" class="ov-attn-cta">
      <span>${escapeInsightText(rec.cta)}</span>
      <svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M5 10h10M11 6l4 4-4 4"/></svg>
    </button>` : ''}

    ${rest.length ? `
    <!-- Open. The card is as tall as the column beside the twin now, and a
         collapsed summary in a card with 200px of white space under it is
         asking the reader to click to find out whether there is anything
         there. It still closes. -->
    <details class="ov-attn-rest" open>
      <summary>${rest.length} further finding${rest.length === 1 ? '' : 's'}</summary>
      <div class="ov-attn-rest-list">
        ${rest.map((it) => `
          <button type="button" class="ov-attn-rest-item"
                  data-kind="${it.kind}" data-id="${it.id}">
            <span class="ov-attn-rest-cat cat-${(it.category || 'info').toLowerCase()}">${escapeInsightText(it.category || '')}</span>
            <span class="ov-attn-rest-title">${escapeInsightText(it.title)}</span>
          </button>`).join('')}
      </div>
    </details>` : ''}`;

  const open = (kind, id) => {
    if (typeof window.showInsightDetail === 'function') {
      window.showInsightDetail(kind, id);
    }
  };
  list.querySelector('[data-open-lead]')?.addEventListener('click', () => {
    open(lead.kind, lead.id);
  });
  list.querySelectorAll('.ov-attn-rest-item').forEach((el) => {
    el.addEventListener('click', () => open(el.dataset.kind, el.dataset.id));
  });
  list.querySelector('.ov-attn-cta')?.addEventListener('click', () => {
    if (typeof window.navigateToTab === 'function') window.navigateToTab('scenarios');
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
      // The recommendation is part of the attention card now, so redrawing
      // the feed redraws it too — on both pages that show that card.
      renderHomeAttentionFeed();
      renderOverviewAlert();
      renderHomeAttentionFeed('fc-attn-body');
      renderOverviewAlert('fc-alert');
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

  // "How many plants / DCs / markets" is answered on the legend itself, at
  // the bottom-right corner of the map, rather than only by the three tables
  // further down the page.
  renderMapLegendCounts();

  [['map2d-node-count', nodeCount], ['twin3d-node-count', nodeCount],
   ['map2d-flow-count', laneCount], ['twin3d-flow-count', laneCount]]
    .forEach(([id, value]) => {
      const el = document.getElementById(id);
      if (el) el.textContent = String(value);
    });

  // The em dash needs a VISIBLE reason, not only a tooltip.
  //
  // Refusing to invent a network-level risk band is right — no engine here
  // owns that metric, and aggregating the per-facility figures on this screen
  // would make it a second KPI engine. But an em dash beside "Overall Risk",
  // on a map whose own cards report three sites above 90% utilisation, reads
  // as "no risk found" rather than "not computed". A tooltip is not an answer:
  // it is invisible until hovered and absent entirely on touch.
  //
  // So the tile says what IS known — how many sites are over the threshold,
  // which the KPI layer does own — and names the metric that is missing.
  const overThreshold = DCS.concat(PLANTS).filter(
    (f) => typeof f.utilPct === 'number' && f.utilPct >= 90).length;
  const riskText = overThreshold
    ? `${overThreshold} site(s) ≥90%`
    : 'Not computed';
  ['map2d-risk-label', 'twin3d-risk-label'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = riskText;
    el.style.fontSize = '13px';
    el.title = overThreshold
      ? `${overThreshold} facility/facilities are at or above the 90% `
        + 'utilisation threshold. There is no network-level risk rating: risk '
        + 'is computed per facility, and aggregating it here would invent a '
        + 'metric no engine owns.'
      : 'No network-level risk metric has an authoritative owner; risk is '
        + 'computed per facility. This is not a statement that the network '
        + 'carries no risk.';
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
      // `d.isOpen === false` is the solver's decision not to use this site. It
      // runs at 0%, which the utilisation bands read as "Healthy" — a green
      // tag saying a facility performs well on a facility that is not
      // operating. Operating status and utilisation health are different
      // facts and get different answers.
      const label = hasUtil ? getUtilLabel(d.utilPct, d.isOpen) : 'Not solved';
      const tagClass = hasUtil ? getUtilTagClass(d.utilPct, d.isOpen) : 'tag-muted';
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
    <!-- city/state/region are not fields on a loaded facility; this rendered
         "undefined, undefined - undefined Region". The network does carry the
         site id and the inferred geography. -->
    <div class="fp-subtitle">${fac.id}${NETWORK_GEOGRAPHY.region ? " · " + NETWORK_GEOGRAPHY.region : ""}</div>
    <div class="fp-stat"><span class="fp-stat-label">Type</span><span class="fp-stat-value">${isPlant ? 'Manufacturing Plant' : 'Distribution Centre'}</span></div>
    <div class="fp-stat"><span class="fp-stat-label">Status</span><span class="fp-stat-value">${openStatusTag(fac)}</span></div>
    <div class="fp-stat"><span class="fp-stat-label">Capacity</span><span class="fp-stat-value">${formatNumber(fac.capacity)} ${perPeriodLabel()}</span></div>
    <div class="fp-stat"><span class="fp-stat-label">Current Throughput</span><span class="fp-stat-value">${formatNumber(fac.throughput)} ${perPeriodLabel()}</span></div>
    <div class="fp-stat"><span class="fp-stat-label">Utilisation</span><span class="fp-stat-value" style="color:${utilColor}">${utilPct}% <span class="tag ${getUtilTagClass(utilPct)}">${utilLabel}</span></span></div>
    ${isDC && fac.fixedCostPerYear != null ? `<div class="fp-stat"><span class="fp-stat-label">Fixed Cost</span><span class="fp-stat-value">${formatCurrency(fac.fixedCostPerYear)}/year</span></div>` : ''}
    ${isDC && fac.handlingCost != null ? `<div class="fp-stat"><span class="fp-stat-label">Handling Cost</span><span class="fp-stat-value">${formatCurrencyExact(fac.handlingCost)}/unit</span></div>` : ''}
    ${forecastSection}
    <div class="fp-stat"><span class="fp-stat-label">Risk / Bottleneck</span><span class="fp-stat-value"><span class="tag ${getUtilTagClass(utilPct)}">${riskStatus}</span></span></div>
    <div style="margin-top:var(--space-lg)">
      <div class="card-title mb-md">Connected Lanes</div>
      ${LANES.filter(l => l.from === facilityId || l.to === facilityId).map(l => `
        <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border-light);font-size:12px">
          <span>${l.from === facilityId ? '→ ' + getFacilityById(l.to)?.name : '← ' + getFacilityById(l.from)?.name}</span>
          <span style="color:var(--text-3)">${formatNumber(l.flow)} ${perPeriodLabel()} · ${formatCurrencyExact(l.cost)}/u</span>
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

/**
 * The engine's recommendation, shaped for the attention card.
 *
 * NOTHING HERE IS WRITTEN BY THE UI. The mockup shows a short action title
 * ("Test additional capacity") above a sentence of detail, but the engine
 * returns one block of prose and no title — so the title is its own FIRST
 * SENTENCE, which is where it states its conclusion, and the detail is the
 * rest. Splitting the engine's words is restructuring; composing a headline
 * for it would be putting a conclusion in its mouth.
 *
 * The button's label describes what the BUTTON does, not what the engine
 * concluded, for the same reason: this build can open the scenario planner,
 * and it cannot promise that a particular scenario is the right one.
 *
 * Returns null when no recommendation has been produced. Absence is not a
 * clean bill of health, and the caller renders nothing rather than reassurance.
 */
function getNetworkRecommendation() {
  const rec = NETWORK_RECOMMENDATION;
  if (!rec.text) return null;

  const text = String(rec.text).trim();
  // First sentence, on a real terminator followed by a space — not on every
  // full stop, or "1,435,985 units." and "e.g." would split it.
  const m = text.match(/^(.{20,180}?[.!?])(\s|$)/);
  const headline = m ? m[1].trim() : text;
  const body = m ? text.slice(m[1].length).trim() : '';

  // Every caveat the engine attached, in one line, because a recommendation
  // acted on without them is a recommendation misread.
  const caveats = [];
  if (rec.limitation) caveats.push(`Limitation: ${rec.limitation}`);
  if (rec.evidenceCompleteness && rec.evidenceCompleteness !== 'COMPLETE') {
    caveats.push(`Evidence is ${rec.evidenceCompleteness} — some analyses did `
      + 'not run, so their values are unknown rather than zero.');
  }
  if (rec.groundingStatus
      && rec.groundingStatus !== 'GROUNDED'
      && rec.groundingStatus !== 'NO_CLAIMS') {
    caveats.push(`Numeric grounding: ${rec.groundingStatus} — treat the figures `
      + 'above as unverified.');
  }

  return {
    headline,
    text: body || (rec.keyDrivers || []).join(' · '),
    limitation: caveats.join(' '),
    cta: 'Open scenario planner',
  };
}

/* ═══════════════════════════════════════════════════════════════
   OVERVIEW — row 3: the signals influencing this network
   ═══════════════════════════════════════════════════════════════ */
/**
 * The external signals the upload carried.
 *
 * Every one of these is a row from the user's own workbook — `EXTERNAL_SIGNALS`
 * is emptied and refilled by hydration, and this build ships no demo signals.
 *
 * The status chip is the honest part. Nothing in this build routes an uploaded
 * signal into a forecast, so a card must not imply that one moved a number.
 * `intendedUse` already records that per signal, and the chip renders it:
 * "Not yet applied" is the truthful state for every signal today, and the chip
 * will say "Used in forecast" on its own the moment that stops being true.
 */
function renderHomeSignals(rowId = 'ov-signals-row') {
  const row = document.getElementById(rowId);
  if (!row) return;

  if (!EXTERNAL_SIGNALS.length) {
    row.innerHTML = `<div class="ov-signals-empty">
      No external signals were found in your upload. A signals sheet &mdash;
      events, their dates and their markets &mdash; appears here when one is
      included.</div>`;
    return;
  }

  // Three on the row, matching the mockup; the rest are behind "View all
  // signals", which is why that link is there rather than decorative.
  row.innerHTML = EXTERNAL_SIGNALS.slice(0, 3).map((sig) => {
    const applied = /used in forecast/i.test(sig.intendedUse || '');
    const context = /context/i.test(sig.intendedUse || '');
    const tone = applied ? 'ok' : context ? 'info' : 'pending';
    const label = applied ? 'Used in forecast'
      : context ? 'Context only' : 'Not yet applied';
    return `
      <div class="ov-signal">
        <span class="ov-signal-icon">${signalIconSvg(sig.type)}</span>
        <div class="ov-signal-text">
          <div class="ov-signal-title" title="${escapeInsightText(sig.title)}">${escapeInsightText(sig.title)}</div>
          <div class="ov-signal-meta">Source: ${escapeInsightText(sig.source)}
            &nbsp;&middot;&nbsp; ${escapeInsightText(sig.publishedDate)}</div>
          <span class="ov-signal-chip tone-${tone}">${OV_SIGNAL_CHIP[tone]}${label}</span>
        </div>
      </div>`;
  }).join('');
}

const OV_SIGNAL_CHIP = {
  ok: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.4"/><polyline points="8.4 12.2 11 14.8 15.8 9.6"/></svg>`,
  info: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.4"/><line x1="12" y1="11" x2="12" y2="16.5"/><line x1="12" y1="7.6" x2="12" y2="7.61"/></svg>`,
  pending: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.4"/><polyline points="12 7 12 12 15.4 14"/></svg>`,
};

/** A glyph for what KIND of signal this is, from the type the upload stated. */
function signalIconSvg(type) {
  const t = String(type || '').toLowerCase();
  if (/weather|monsoon|flood|storm|rain/.test(t)) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17.5 15.5a4 4 0 0 0-1-7.87 6 6 0 0 0-11.6 1.5A3.5 3.5 0 0 0 5.5 16"/><line x1="8" y1="19" x2="7" y2="21.5"/><line x1="12" y1="19" x2="11" y2="21.5"/><line x1="16" y1="19" x2="15" y2="21.5"/></svg>`;
  }
  if (/survey|customer|distributor|consumer|panel/.test(t)) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.4"/><path d="M2.6 20a6.4 6.4 0 0 1 12.8 0"/><circle cx="17.5" cy="9" r="2.6"/><path d="M16 14.2a5.2 5.2 0 0 1 5.4 5"/></svg>`;
  }
  if (/retail|expansion|store|market_growth|growth/.test(t)) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 9.5 5 4h14l1.5 5.5"/><path d="M4 9.5V20h16V9.5"/><path d="M3.5 9.5a2.6 2.6 0 0 0 5.2 0 2.6 2.6 0 0 0 5.2 0 2.6 2.6 0 0 0 5.2 0"/><path d="M9.5 20v-5.5h5V20"/></svg>`;
  }
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4.2 4.2a11 11 0 0 1 15.6 15.6"/><path d="M7.8 7.8a6 6 0 0 1 8.4 8.4"/><circle cx="12" cy="12" r="1.6" fill="currentColor"/></svg>`;
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

  // Absence renders as absence. This block previously read from a hardcoded
  // literal — 42 facilities, 380 lanes, 98.4% quality, ARIMA, a run dated
  // 18/08/2026 — shown identically for every project. Model-governance
  // metadata that describes a run which never happened is worse than no
  // metadata: it is the surface a reviewer consults to decide whether to
  // trust the analysis.
  const or_ = (value, suffix = '') =>
    (value === null || value === undefined || value === '')
      ? '<span style="color:var(--text-3)">Not available</span>'
      : `${value}${suffix}`;

  const geographyLine = NETWORK_GEOGRAPHY.region
    ? `${NETWORK_GEOGRAPHY.region}${NETWORK_GEOGRAPHY.basis
        ? ` <span class="text-xs" style="color:var(--text-3)">(${NETWORK_GEOGRAPHY.basis})</span>` : ''}`
    : null;

  // Say when the approval bands are denominated in a currency other than the
  // one this network is priced in. They are fixed INR policy amounts; read
  // against a dollar network they understate the band by roughly eighty times.
  const bandNote = (getActiveCurrency() && getActiveCurrency() !== GOVERNANCE_TIERS_CURRENCY)
    ? `<div class="text-xs" style="color:var(--amber, #b45309);margin-top:6px">
         These approval bands are configured in ${GOVERNANCE_TIERS_CURRENCY}. This
         network is priced in ${getActiveCurrency()}, and no exchange rate is
         configured — compare them yourself before relying on a tier.
       </div>`
    : '';

  body.innerHTML = `
    <div class="card-title" style="font-size:13px;margin:10px 0 6px">Guardrail Configuration &amp; Materiality Thresholds</div>
    <div style="border:1px solid var(--border-light);border-radius:var(--r-sm);padding:0 12px">${tiersHtml}</div>
    ${bandNote}

    <div class="card-title" style="font-size:13px;margin:18px 0 6px">This project&rsquo;s network</div>
    <div class="grid-2" style="gap:10px;font-size:12.5px">
      <div><span class="text-muted">Facilities</span><br><strong>${or_(d.facilities)}</strong></div>
      <div><span class="text-muted">Markets</span><br><strong>${or_(d.markets)}</strong></div>
      <div><span class="text-muted">Lanes</span><br><strong>${or_(d.lanes)}</strong></div>
      <div><span class="text-muted">Geography</span><br><strong>${or_(geographyLine)}</strong></div>
      <div><span class="text-muted">Currency</span><br><strong>${or_(getActiveCurrency())}</strong></div>
      <div><span class="text-muted">Observed periods</span><br><strong>${or_(d.historicalPeriods)}</strong></div>
    </div>

    <div class="card-title" style="font-size:13px;margin:18px 0 6px">The run behind these figures</div>
    <div class="grid-2" style="gap:10px;font-size:12.5px">
      <div><span class="text-muted">Engine</span><br><strong>${or_(opt.solver)}</strong></div>
      <div><span class="text-muted">Solver status</span><br><strong>${or_(opt.status)}</strong></div>
      <div><span class="text-muted">Computed at</span><br><strong>${or_(opt.lastRun ? new Date(opt.lastRun).toLocaleString() : null)}</strong></div>
      <div><span class="text-muted">Solve time</span><br><strong>${or_(opt.computeSeconds, ' s')}</strong></div>
      <div><span class="text-muted">Planning horizon</span><br><strong>${or_(horizonLabel() || `${SOLVE_HORIZON.periodsModelled} period`)}</strong></div>
      <div><span class="text-muted">Forecast engine</span><br><strong>${or_(fc.model)}</strong></div>
      <div><span class="text-muted">Execution id</span><br><strong style="font-family:monospace;font-size:11px">${or_(opt.executionId)}</strong></div>
      <div><span class="text-muted">Data version</span><br><strong style="font-family:monospace;font-size:11px">${or_(opt.dataVersion)}</strong></div>
    </div>
    <div class="text-xs" style="color:var(--text-3);margin-top:8px">
      Every value above describes the analysis currently loaded. A field reads
      &ldquo;Not available&rdquo; when this run did not produce it.
    </div>

    <div class="card-title" style="font-size:13px;margin:18px 0 6px">Trigger Keywords / Buckets</div>
    <div class="text-xs" style="color:var(--text-2);font-style:italic">Not available — no keyword/bucket configuration exists in this build yet.</div>

    <div class="card-title" style="font-size:13px;margin:18px 0 6px">Product Master / Configuration</div>
    <div class="text-xs" style="color:var(--text-2);font-style:italic">Not available — no Product Master data exists in this build yet.</div>
  `;
}

// ─── In-product info panel ──────────────────────────────────
/* showInfoPanel lives in js/workspace-chrome.js and is imported above.
   There were two of these — one here for the app shell, one for the
   project screens — differing only in which card class they borrowed. */


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
/** One CSV field: quoted, with embedded quotes doubled, never "undefined". */
function csvCell(value) {
  if (value === null || value === undefined || value === '') return '"Not available"';
  return '"' + String(value).split('"').join('""') + '"';
}

/**
 * Read one exported figure out of a facility KPI record.
 *
 * The record's fields are OBJECTS — `{ value, unit, delta }` — and the export
 * interpolated them straight into a string, so "On-Time Service SLA" left the
 * building as the literal text `[object Object]`. Three other rows named keys
 * (`cost`, `invDays`, `fillRate`) that no record has ever carried, so they
 * exported "Not available" on a fully solved network.
 */
function kpiField(kpis, key) {
  const entry = kpis && kpis[key];
  if (entry === null || entry === undefined) return null;
  const value = (typeof entry === 'object') ? entry.value : entry;
  return (value === null || value === undefined || Number.isNaN(Number(value)))
    ? null : value;
}

export function exportFacilityReport() {
  const facId = state.selectedFacility;
  const fac = getFacilityById(facId) || DCS[0] || PLANTS[0];
  if (!fac) { showNotification('No facility is loaded to export.'); return; }
  const kpis = getKpisForFacility(facId, state.selectedPeriod) || {};
  const insights = getInsightsForFacility(facId);

  const util = kpiField(kpis, 'util');
  const sla = kpiField(kpis, 'sla');
  const cost = kpiField(kpis, 'totalCost');
  const invDays = kpiField(kpis, 'inventoryDays');
  const throughput = kpiField(kpis, 'throughput') ?? fac.throughput;
  const capacity = kpiField(kpis, 'capacity') ?? fac.capacity;
  const perPeriod = perPeriodLabel();
  const ccy = getActiveCurrency();

  // Location from what the network actually carries. `fac.city`, `fac.state`
  // and `fac.region` are not fields on a loaded facility, so this row exported
  // "undefined, undefined (undefined Region)" for every facility of every
  // project — including the ones whose coordinates were on screen beside it.
  const coords = (typeof fac.lat === 'number' && typeof fac.lng === 'number')
    ? `${fac.lat.toFixed(4)}, ${fac.lng.toFixed(4)}` : null;
  const location = [fac.name, NETWORK_GEOGRAPHY.region].filter(Boolean).join(' — ');

  const lines = [
    '=== NetGravity Facility Performance Report ===',
    'Generated,' + csvCell(new Date().toISOString()),
    'Facility,' + csvCell(fac.name),
    'Facility ID,' + csvCell(fac.id),
    'Facility Type,' + csvCell(isPlantFacility(fac.id) ? 'Manufacturing Plant' : 'Distribution Centre'),
    'Location,' + csvCell(location),
    'Coordinates,' + csvCell(coords),
    'Currency,' + csvCell(ccy),
    'Planning horizon,' + csvCell(horizonLabel() || `${SOLVE_HORIZON.periodsModelled} period`),
    'Period shown,' + csvCell(state.selectedPeriod || 'as uploaded'),
    '',
    '=== Operational Telemetry ===',
    // An exported figure is evidence a reader may act on, so a metric the
    // engine did not produce is exported as "Not available" — never as a
    // plausible-looking number, and never with a status ("Target Met",
    // "Healthy") asserted over a value that does not exist. Peak utilisation
    // has no forecast behind it at all, so it is always reported as absent.
    `Capacity (units/${perPeriod}),` + csvCell(capacity),
    `Throughput (units/${perPeriod}),` + csvCell(throughput),
    'Utilisation %,' + csvCell(util === null ? null : Number(util).toFixed(2)),
    'Projected Peak Utilisation,' + csvCell(null) + ',"no demand forecast for this facility"',
    '',
    '=== Core Performance KPIs ===',
    'Metric,Value,Unit,Status',
    'Demand served within SLA,' + csvCell(sla) + ',' + csvCell('%')
      + ',' + csvCell(sla === null ? null : 'Reported'),
    'Operating cost,' + csvCell(cost) + ',' + csvCell(ccy)
      + ',' + csvCell(cost === null ? null : 'Reported'),
    'Inventory days of supply,' + csvCell(invDays) + ',' + csvCell('days')
      + ',' + csvCell(invDays === null ? null : 'Reported'),
    '',
    '=== Findings ===',
    'Insight ID,Severity,Summary',
  ];

  if (insights && insights.length > 0) {
    insights.forEach(function (ins) {
      lines.push([
        csvCell(ins.id),
        // The insight's own severity, or absence. It was defaulted to
        // "Critical", which asserts a severity the engine never assigned.
        csvCell(ins.impact || ins.severity),
        csvCell(ins.title || ins.headline || ins.desc),
      ].join(','));
    });
  } else {
    // No insight has been generated for this network. Exporting two invented
    // ones about the prototype's demo footprint would put fabricated findings
    // into a file the user may circulate as analysis.
    lines.push(csvCell('') + ',' + csvCell('No insight')
      + ',' + csvCell('No insight has been generated for this network yet.'));
  }

  // A Blob with an explicit filename, not a `data:` URL. Chrome ignores the
  // `download` attribute's name on long data: URLs and saves the report under a
  // generated temporary name, which is how an export meant to be circulated
  // arrived as an unidentifiable file.
  const stamp = new Date().toISOString().slice(0, 10);
  const safeName = String(fac.name || fac.id).replace(/[^A-Za-z0-9_-]+/g, '_');
  const blob = new Blob(['﻿' + lines.join('\r\n')],
                        { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `NetGravity_KPI_${safeName}_${stamp}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);

  showNotification('Exported KPI report for ' + fac.name);
}

// Expose export on window
if (typeof window !== 'undefined') {
  window.exportFacilityReport = exportFacilityReport;
  window.triggerAgentReasoning = triggerAgentReasoning;
}
