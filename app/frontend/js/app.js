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
  getActiveCurrency, FORECAST_CATALOGUE, selectForecastSeries, withCurrency,
  FORECAST_BRIEFING, recommendedNetworkChanges, DEMAND_SHORTFALL
} from './data.js';
// The twin's legend and the encoding it describes, shared with both views.
import { twinLegendHtml, facilityLabel, NODE_STYLE } from './twin-legend.js';
import { initMap, invalidateMapSize, refreshAllMaps,
         revealMap, renderMapLegendCounts, refreshTwinMapLegend } from './map.js';
import { initTwin3D, resizeTwin3D } from './twin3d.js';
import { networkCountryLabel } from './world-basemap.js';
import {
  renderForecastChart,
  renderFacilityThroughputChart, renderFacilityCostBreakdownChart, renderFacilityLaneFlowsChart
} from './charts.js';
import { initScenarios } from './scenarios.js';
import { renderWarehouseDashboard, clearWarehouseState,
         warehouseRow, recordWarehouseDrawn } from './warehouse.js';
import { mountAgentLoading } from './agent-loading.js';
import {
  beginAnalysisLoading, endAnalysisLoading, reportAnalysisStage,
} from './analysis-loading.js';
import { initAgent } from './agent.js';
import { initLandingPage } from './landing.js';
import { initInsightDetail } from './insight-detail.js';
// The two presentation decisions the tiles here and the deep-dive page
// both make about a finding. Shared so they cannot disagree — see the
// module header.
import { insightCta, insightDescription } from './insight-presentation.js';
import { initAuth } from './auth.js';
import { initProjects, loadProjects, openProjectById } from './projects.js';
import { initIngestion } from './ingestion.js';
import { initChatbot } from './chatbot.js';
import { apiClient } from './integration/api-client.js';
import { initActions } from './actions.js';
import { showInfoPanel, signOut } from './workspace-chrome.js';
import { getActiveProjectId, setActiveProject } from './integration/project-context.js';
import { loadIdentity, getCurrentUser } from './identity.js';
import { kpiService } from './integration/services/kpi-service.js';
import { twinService } from './integration/services/twin-service.js';
import { mapNetworkKPIsToCards } from './integration/mappers/kpi-mapper.js';
import { initKpiView, renderKpiView, currentKpiView } from './kpi-view.js';
import { clearKpiExplainCache } from './kpi-explain.js';
import { mapTwinStateToFrontend } from './integration/mappers/twin-mapper.js';

// ─── State ──────────────────────────────────────────────────
const state = {
  activeTab: 'home',
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
  // Which band of findings the Insights page is showing — 'all', a severity,
  // or 'action'. Held here rather than read back off the DOM so a re-render
  // triggered by a late briefing keeps the reader's selection.
  insightsFilter: 'all',
};

// Insights actioned via the deep-dive page (insight-detail.js) are dropped
// from the Overview's tiles and the Insights page on the next render — see
// window.markAttentionItemResolved.
//
// Declared here, above the window exports, because those exports make
// renderHome() callable immediately: hydrate.js invokes it as soon as the
// authoritative data lands, which can be before module evaluation reaches the
// bottom of this file. With the declaration further down, that call hit the
// temporal dead zone and threw "Cannot access 'resolvedInsightIds' before
// initialization", leaving Home's findings blank.
const resolvedInsightIds = new Set();

// Expose globally on window
if (typeof window !== 'undefined') {
  window.navigateToTab = navigateToTab;
  window.closeActionDrawer = closeActionDrawer;
  window.renderHome = renderHome;
  // Called by ingestion.js the moment an analysis finishes, which can be
  // before Home has ever rendered.
  window.renderOverviewAlert = renderOverviewAlert;
  // Every screen that reports the state of the solve, in one call. The
  // notice reaches TWO elements now — the Overview's error-only banner and
  // the Forecast page's full alert — and ingestion.js has no business
  // knowing either id.
  window.refreshNetworkNotice = () => {
    renderOverviewAlert('ov-notice', { errorsOnly: true });
    renderOverviewAlert('fc-alert');
  };
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
  // The agent loading overlay is a singleton on <body>; mounting it at boot
  // means every entry point that raises a loading state finds it there.
  try { mountAgentLoading(); } catch (e) { console.error('agent loading mount:', e); }
  try { initHomeSelectors(); } catch (e) { console.error('initHomeSelectors error:', e); }
  // The KPI screen's tabs, filters and drill-down. The two hooks are the
  // things it cannot do itself: set the application's selected facility, and
  // draw that facility's detail — both of which live here.
  try {
    initKpiView({
      selectEntity: (facilityId) => {
        state.selectedFacility = facilityId;
        const sel = document.getElementById('sel-facility');
        if (sel && [...sel.options].some((o) => o.value === facilityId)) {
          sel.value = facilityId;
        }
      },
      renderEntity: () => renderFacilityDashboard(),
      // The period control, filled and read exactly as the top bar's was.
      // One list, one selected period, one `renderForSelection()` — the KPI
      // screen did not get a period of its own, it got the one that already
      // existed, on the screen that describes it.
      populatePeriods: (select) => populatePeriodSelect(select),
      selectPeriod: (value) => {
        state.selectedPeriod = value;
        const sel = document.getElementById('sel-period');
        if (sel && [...sel.options].some((o) => o.value === value)) sel.value = value;
        renderForSelection();
      },
      // The KPI screen's Network lens shows the Overview's four figures, and
      // shows them by calling the Overview's OWN renderer. One source, so the
      // two screens cannot report different numbers for one network — a
      // second copy of this arithmetic here is exactly how they would.
      renderNetworkScorecard: (rowId) => renderHomeKpiStrip(rowId),
      // The network's inventory cost, from the same authoritative baseline the
      // scorecard above it reads — so the gap the KPI screen names and the
      // total it is measured against come from one source.
      networkInventoryCost: () => {
        const base = getOptimizedBaseCase() || {};
        const v = (base.baseline || {}).inventoryCost;
        return (typeof v === 'number' && Number.isFinite(v)) ? v : null;
      },
    });
  } catch (e) { console.error('initKpiView error:', e); }
  try { initTwinLegendDock(); } catch (e) { console.error('twin legend dock:', e); }
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
  // A forecast that arrived with the upload was not produced here, and every
  // field on this card that describes a model has to say so. Reporting an
  // engine name, a horizon "modelled" and an accuracy for numbers nothing was
  // fitted to would attribute the upload's own projection to this build.
  const supplied = meta.source === 'uploaded';
  set('fc-model', supplied
    ? 'Supplied with this upload — not recalculated'
    : (meta.engine || 'Not reported'));
  set('fc-horizon', `${FORECAST.months.length} periods`);
  // MASE < 1 means the model beats a naive seasonal forecast; the comparison
  // is stated because the bare number means nothing to most readers.
  //
  // "Not applicable" rather than "Not reported" when nothing was fitted: a
  // measurement that could not exist is a different fact from one that was not
  // taken, and this row is read as a quality claim either way.
  set('fc-accuracy', supplied ? 'Not applicable — no model was fitted'
    : (mase === null ? 'Not reported'
       : `${mase.toFixed(2)} (${mase < 1 ? 'better' : 'worse'} than naive)`));
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
  set('fc-chart-tag', supplied ? 'Supplied forecast'
    : (meta.status === 'OK' ? 'Observed + forecast' : meta.status));
  // The band is named only when there is one to name. A supplied forecast
  // without stated bounds is drawn as a line, and promising a p10–p90 band
  // beside it would describe a shape that is not on the chart.
  const banded = FORECAST.upper.length && FORECAST.lower.length;
  set('fc-chart-subtitle',
    `${DEMAND_HISTORY.months.length} observed periods + `
    + `${FORECAST.months.length}-period forecast`
    + (banded ? ' · p10–p90 band' : ' · no confidence band was supplied')
    + (meta.uncovered
      ? ` · ${meta.uncovered} market-product pair(s) in this network are not in `
        + 'the upload and have no forecast' : ''));
  set('fc-method-prov', supplied
    ? 'Supplied with this upload and shown exactly as received. No forecasting '
      + 'engine ran against these figures: no ETS or quantile model, no '
      + 'intermittent-demand model, no structural-break detection, no '
      + 'rolling-origin backtest, and no external signal was applied. The '
      + 'periods on the axis are the ones the upload states.'
    : 'Produced by netgravity.forecasting, routed through the orchestrator '
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
 * The left column is the Overview's alert — `renderOverviewAlert` takes the
 * element it draws into — above this page's OWN attention card. Nothing here
 * computes a finding, a figure or a recommendation of its own.
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
  // This page's own card, about the forecast. The Overview's network-scoped
  // feed used to be drawn here as well — the same finding, twice, on two
  // screens, one of them asking a different question. That feed is gone
  // entirely now; this screen has its own grounded answer.
  renderForecastAttention('fc-attn-body');
  renderHomeSignals('fc-signals-row');
  renderAnalysisTimestamp();
  renderForecastSummary();
  renderDataIntelligence();
  wireForecastPage();
  requestAnimationFrame(() => sizePageToWindow('.fc-main', '--fc-main-top'));
}

/**
 * "How did it reach that number?", as a file.
 *
 * ONLY WHERE THERE IS A DERIVATION TO GIVE. A forecast supplied with the
 * upload was not calculated here, so there is no calculation to explain and
 * this build cannot account for how the supplier arrived at it. Offering the
 * button anyway would promise a derivation that cannot exist — the same
 * failure as the twin's hover card, which described a control it did not have
 * (Nielsen #1: the system's state is what the screen shows, and a control is a
 * statement that something can be done).
 *
 * The uploaded case is not left silent: the provenance line under this card
 * already says the figures were supplied and not recalculated, which is the
 * answer to the question the button would have been pressed to ask.
 */
function forecastDownloadHtml() {
  const meta = window.__ngForecastMeta || {};
  if (meta.source === 'uploaded') return '';
  if (!FORECAST.months.length) return '';
  return `
    <button type="button" class="insd-download fc-attn-download"
            id="fc-download-doc">
      <svg viewBox="0 0 20 20" fill="none" stroke="currentColor"
           stroke-width="1.9" aria-hidden="true">
        <path d="M10 3v9m0 0 3.5-3.5M10 12 6.5 8.5M4 15.5h12"/>
      </svg>
      <span>Download how this forecast was calculated</span>
      <span class="insd-download-ext">DOCX</span>
    </button>`;
}

/**
 * Fetch the forecast's derivation and hand it to the browser to save.
 *
 * The same contract as the deep dive's download, deliberately: the button
 * says what it is doing throughout, and a failure says so ON the button
 * rather than in a console nobody has open.
 */
async function downloadForecastDerivation(button) {
  if (!button || button.disabled) return;
  const label = button.querySelector('span');
  const original = label ? label.textContent : '';
  button.disabled = true;
  if (label) label.textContent = 'Preparing\u2026';
  // A document takes a solve and, where the gateway is configured, a
  // text-generation call — which the gateway allows itself a minute for. A
  // button that says the same thing for that long reads as a hung one, so
  // the wait names its slow half rather than growing silent.
  const stage = setTimeout(() => {
    if (label && button.disabled) label.textContent = 'Writing the explanation\u2026';
  }, 5000);

  try {
    const mod = await import('./integration/services/forecast-service.js');
    const { blob, filename } = await mod.forecastService.downloadDerivation({
      // Off `FORECAST`, which the series picker rewrites — not off the
      // hydration-time meta, which names the series the screen opened on.
      marketId: FORECAST.marketId || null,
      productId: FORECAST.productId || null,
      horizon: FORECAST.months.length || null,
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename || 'forecast-derivation.docx';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    if (label) label.textContent = original;
  } catch (err) {
    if (label) label.textContent = 'Could not build the document';
    button.classList.add('is-failed');
    button.title = err && err.message ? err.message : '';
    setTimeout(() => {
      if (label) label.textContent = original;
      button.classList.remove('is-failed');
    }, 4000);
  } finally {
    clearTimeout(stage);
    button.disabled = false;
  }
}

/**
 * What this forecast means, and what to do about it.
 *
 * Every figure and every sentence is the BACKEND's: the briefing comes from
 * the reasoning step the forecast workflow runs and has passed numeric
 * grounding; the outlook is summed from the forecaster's own points by
 * `_forecast_outlook`. This selects, formats and wires buttons. It computes no
 * quantity and writes no finding.
 *
 * The actions are the point. A forecast briefing that ends "monitor demand"
 * has told a planner nothing they can act on; this application can test the
 * network against the demand the forecast projects, at the rate it projects,
 * and that is one press.
 */
function renderForecastAttention(listId = 'fc-attn-body') {
  const list = document.getElementById(listId);
  if (!list) return;

  const briefing = FORECAST_BRIEFING.explanation;
  const card = (briefing && briefing.card) || null;
  const outlook = FORECAST_BRIEFING.outlook || null;

  if (!card && !outlook) {
    list.innerHTML = `<div class="ov-attn-empty">No forecast has been produced
      for this network yet, so there is nothing to read here. A forecast needs
      observed demand history in the upload.</div>`;
    return;
  }

  const rows = [];
  if (outlook && typeof outlook.growth_pct === 'number') {
    const up = outlook.growth_pct >= 0;
    rows.push(['Demand ahead',
      `<strong>${formatNumber(Math.round(outlook.total_forecast_units || 0))} units</strong>
       over ${outlook.horizon} periods
       <span class="fc-attn-delta ${up ? 'up' : 'down'}">${up ? '↑' : '↓'}
       ${Math.abs(outlook.growth_pct).toFixed(1)}% vs the periods just observed</span>`]);
  } else if (outlook && typeof outlook.total_forecast_units === 'number') {
    rows.push(['Demand ahead',
      `<strong>${formatNumber(Math.round(outlook.total_forecast_units))} units</strong>
       over ${outlook.horizon} periods. No comparable observed window, so no
       growth rate is stated.`]);
  }

  const top = ((outlook && outlook.fastest_growing) || [])
    .filter((r) => typeof r.growth_pct === 'number' && r.growth_pct > 0);
  if (top.length) {
    rows.push(['Growing fastest', top.slice(0, 3).map((r) =>
      `${escapeInsightText(r.market_id)} · ${escapeInsightText(r.product_id)}
       <span class="fc-attn-delta up">+${r.growth_pct.toFixed(0)}%</span>`).join('<br>')]);
  }

  if (outlook && outlook.n_structural_breaks) {
    rows.push(['History that changed',
      `${outlook.n_structural_breaks} series changed level partway through, so the
       forecast for ${outlook.n_structural_breaks === 1 ? 'it is' : 'those are'}
       built from the period after the break rather than the whole history.`]);
  }

  const signals = FORECAST_BRIEFING.signals;
  if (signals && typeof signals.attached === 'number' && signals.attached) {
    rows.push(['External signals',
      signals.series_adjusted
        ? `${signals.attached} supplied · <strong>${signals.series_adjusted}
           series moved</strong> by them`
        : `${signals.attached} supplied · none changed a forecast — the router
           applies a signal only where it names something this network contains`]);
  }

  const actions = forecastActions(outlook);

  // WHOSE FORECAST THIS IS, BEFORE ANY OF ITS FIGURES.
  //
  // It was one muted sentence at the foot of the card, under the numbers,
  // the actions and the download. A reader who takes a growth rate off this
  // card and repeats it in a meeting has to know whether this application
  // produced it or simply added up a column somebody handed it — and that is
  // the first thing they need to know, not the last.
  const uploaded = (window.__ngForecastMeta || {}).source === 'uploaded';

  list.innerHTML = `
    ${uploaded ? `
      <div class="fc-attn-provenance">
        <span class="fc-attn-provenance-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"
               stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 9l5-5 5 5M12 4v12"/>
          </svg>
        </span>
        <span><strong>This forecast was uploaded with your data.</strong>
          NetGravity has not modelled, adjusted or recalculated any of it —
          the figures below are read from the sheet you supplied.</span>
      </div>` : ''}
    ${card && card.headline
      ? `<div class="ov-attn-lead"><div class="ov-attn-section">
           <div class="ov-attn-section-label tone-why">${uploaded
             ? 'What the supplied forecast says' : 'What the projection says'}</div>
           <div class="ov-attn-section-text">${escapeInsightText(card.headline)}</div>
         </div></div>` : ''}
    ${rows.length ? `<dl class="fc-attn-facts">
      ${rows.map(([label, value]) => `<dt>${label}</dt><dd>${value}</dd>`).join('')}
    </dl>` : ''}
    ${card && card.meaning
      ? `<div class="ov-attn-section"><div class="ov-attn-section-label tone-impact">
           What it means</div>
         <div class="ov-attn-section-text">${escapeInsightText(card.meaning)}</div>
       </div>` : ''}
    ${card && card.warning
      ? `<div class="fc-attn-warning">${escapeInsightText(card.warning)}</div>` : ''}
    ${card && card.next_step ? `
      <div class="ov-attn-next">
        <span class="ov-attn-next-icon">${OV_ICONS.chart}</span>
        <div class="ov-attn-next-text">
          <div class="ov-attn-section-label tone-next">Recommended next step</div>
          <div class="ov-attn-next-sub">${escapeInsightText(card.next_step)}</div>
        </div>
      </div>` : ''}
    ${actions.length ? `
      <div class="scn-take-section-title" style="margin-top:14px">Recommended actions</div>
      <div class="scn-take-actions">
        ${actions.map((a, i) => `
          <button type="button" class="scn-take-action${a.primary ? ' primary' : ''}"
                  data-fc-action="${i}">
            <span class="scn-take-action-label">${escapeInsightText(a.label)}</span>
            <span class="scn-take-action-detail">${escapeInsightText(a.detail)}</span>
            <span class="scn-take-action-go">Set this up →</span>
          </button>`).join('')}
      </div>` : ''}
    ${forecastDownloadHtml()}
    <div class="text-xs text-muted" style="margin-top:10px;line-height:1.5">
      ${uploaded
        ? 'Every figure here is summed from that sheet, and from nothing else.'
        : `${card && card.source === 'llm'
            ? 'Written by the model from the forecaster\'s own output.'
            : 'Written from the forecaster\'s own output without a model.'}
           Every figure here is the forecasting engine's.`}
    </div>`;

  document.getElementById('fc-download-doc')?.addEventListener('click',
    (e) => downloadForecastDerivation(e.currentTarget));

  list.querySelectorAll('[data-fc-action]').forEach((btn) => {
    const item = actions[Number(btn.dataset.fcAction)];
    if (item && typeof item.run === 'function') {
      btn.addEventListener('click', item.run);
    }
  });
}

/**
 * What a reader can DO about a forecast, from what the forecast found.
 *
 * Gated on the outlook's own figures, so a network whose demand is projected
 * flat is never offered a growth scenario and one with no concentration is
 * never told to scope it. Nothing here applies anything: each opens the
 * scenario builder, filled in, for a person to run.
 */
function forecastActions(outlook) {
  const actions = [];
  if (!outlook) return actions;

  const growth = outlook.growth_pct;
  if (typeof growth === 'number' && Math.abs(growth) >= 1) {
    const pct = growth.toFixed(0);
    actions.push({
      label: `Test the network at ${growth > 0 ? '+' : ''}${pct}% demand`,
      detail: 'Opens the scenario builder with this forecast\'s own growth rate '
        + 'filled in. The forecast says what is coming; only a solve says '
        + 'whether the current footprint carries it.',
      primary: true,
      run: () => openScenarioFromForecast({ pct: Number(pct) }),
    });
  }

  // Scoped growth, but only where the builder can actually scope it.
  //
  // The builder narrows a demand change by REGION or by product category. The
  // outlook names a market and a product ID, and a product ID is neither — so
  // passing it produced a button reading "Test M001 alone" that ran a
  // network-wide scenario, the select having correctly ignored a value it did
  // not offer. Markets carry a region, and region is one of the two, so that
  // is what this scopes by. Where the upload states no region for the market,
  // the action is not offered rather than offered and silently wrong.
  const top = (outlook.fastest_growing || [])
    .filter((r) => typeof r.growth_pct === 'number' && r.growth_pct > 0);
  if (top.length) {
    const row = top[0];
    const market = MARKETS.find((m) => m.id === row.market_id) || null;
    const region = (market && market.region) || '';
    const where = (market && market.name) || row.market_id;
    if (region) {
      actions.push({
        label: `Test ${region} alone, at +${row.growth_pct.toFixed(0)}%`,
        detail: `${where} grows fastest in this projection, at `
          + `+${row.growth_pct.toFixed(0)}%. Growth stated for the whole network `
          + `loads every site; scoping it to ${region} loads the ones that will `
          + 'actually feel it.',
        run: () => openScenarioFromForecast({
          pct: Number(row.growth_pct.toFixed(0)), region }),
      });
    }
  }
  return actions;
}

/**
 * Open the scenario builder on the Scenario Planning tab, pre-filled.
 *
 * Navigates first: the builder is a modal on that page, and opening it over
 * the Forecast screen would put a scenario form on a page with no scenarios.
 * Nothing is submitted — every field stays editable, and the person presses
 * Run.
 */
function openScenarioFromForecast({ pct, region = '' }) {
  if (typeof window.navigateToTab === 'function') window.navigateToTab('scenarios');
  setTimeout(() => {
    if (typeof window.openScenarioBuilderWith !== 'function') return;
    window.openScenarioBuilderWith('CHANGE_DEMAND', {
      amount: pct,
      region,
      name: region
        ? `Forecast demand ${pct > 0 ? '+' : ''}${pct}% in ${region}`
        : `Forecast demand ${pct > 0 ? '+' : ''}${pct}%`,
    });
  }, 260);
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
  if (foreEl) {
    foreEl.style.flexGrow = String(fore);
    // Name the right-hand half for what it actually is. "Forecast" alone reads
    // as this build's forecast on a chart where it is not.
    const label = foreEl.lastChild;
    if (label && label.nodeType === 3) {
      label.textContent = (window.__ngForecastMeta || {}).source === 'uploaded'
        ? 'Forecast (supplied)' : 'Forecast';
    }
  }
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
  // The warehouse report describes the network that produced it. Re-uploading
  // into the SAME project keeps the project id and changes the data, so a
  // report cached against the id alone would survive its own network.
  try {
    clearWarehouseState();
    // The saved chart briefings go with it. They are about the network that
    // produced them, and a paragraph about the previous upload sitting under
    // a chart of the new one is worse than having none.
    clearKpiExplainCache();
    if (state.activeTab === 'facility-dashboard') renderWarehouseDashboard();
  } catch (err) { }
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
  // Two screens are exceptions, both because the pair would be dead there.
  //
  // Scenario Planning: a scenario is solved over the whole network for the
  // horizon it was built with, so neither control changes anything.
  //
  // Forecast: a demand forecast is per market-product SERIES, and the screen
  // has its own picker for that (`#fc-series-select`) — a facility does not
  // narrow it, because a forecast is about demand and demand belongs to
  // markets. Nor does the period: the horizon is the forecast's own and is
  // stated on the card. Two controls that look like scope and move nothing
  // are worse than none: a reader who sets a facility and sees the chart
  // unchanged has to work out whether the control is broken or the network
  // is. Hiding a dead control is more honest than showing it.
  // THE PAIR IS GONE FROM THE TOP BAR, on every screen.
  //
  // It was hidden one screen at a time as each was found not to use it —
  // Scenario Planning, then Forecast, then the KPI screen — and the remaining
  // three were no better. The Overview reports the whole network by
  // definition; the Digital Twin is a map of every site, narrowed by clicking
  // one; Insights are findings about the network, each naming its own
  // facility. On all three the control moved nothing a reader could see,
  // which teaches them that scope on this product does not work.
  //
  // Scope now lives on the screen that HAS one, in the words of that screen:
  // the KPI page's own lens dropdowns, the Forecast's series picker, the
  // twin's node selection. `#sel-facility` / `#sel-period` in the hidden
  // sub-topbar row remain the application's source of truth and are
  // untouched — this removes a control, not the state behind it.
  const topScope = document.getElementById('home-top-controls');
  if (topScope) {
    topScope.style.display = 'none';
  }

  // The KPI screen owns its own Facility control, inside the filter bar that
  // states what the whole screen is showing. The global picker cannot be left
  // beside it: it lists distribution centres only, so drilling into a PLANT
  // left it naming a different site than the one on screen — a reader seeing
  // "Delhi" above Bengaluru's numbers with nothing to say which was wrong,
  // which is the exact failure the breadcrumb exists to prevent.
  //
  // AND NEITHER CAN PERIOD STAY, for the reason Scenario Planning hides both.
  //
  // This screen reports the whole solved horizon: "12 periods, 2025-09 to
  // 2026-08", with peak figures taken from the busiest single period of it.
  // Nothing on it is scoped to one month — not the scorecard, not the four
  // charts, not the health table, and not the facility drill-down either. The
  // control was there and moving it changed nothing, including when a period
  // OUTSIDE the modelled horizon was picked; the label it used to feed
  // (`dash-period-label`) is not in the markup any more.
  //
  // So it goes, on the rule stated above: hiding a dead control is more
  // honest than showing one. It is untouched on the Digital Twin, the
  // Forecast and the Overview, which do scope by period. If the KPI cards are
  // ever given per-period figures, deleting this branch brings it back.

  // Home carries its own page head ("Overview · Your network health…"), so it
  // does not need the generic title row. Every other page does.
  // The 2D/3D pair describes how the twin is drawn and means nothing
  // anywhere else, so it appears on the title row of exactly one screen.
  const twinActions = document.getElementById('sub-topbar-actions');
  if (twinActions) {
    twinActions.style.display = (tab === 'twin') ? 'flex' : 'none';
  }

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
      // "Facility KPIs" named one of four lenses. The screen opens on the
      // whole network and carries corridors as well as sites, so a title
      // naming only facilities described a quarter of it.
      mainTitle.innerHTML = 'Network KPIs &amp; Analytics';
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
  // After the bar has been laid out for this tab: its height changes with
  // what it carries, and anything pinned below it has to know.
  requestAnimationFrame(publishTopBarHeight);

  // Overview, then Baseline (Digital Twin / KPIs / Insights), then Forecast
  // and Scenarios. The full-page insight deep dive is NOT a sidebar
  // destination — it is opened from a tile, a row or a card, and manages its
  // own nav highlighting and panel display independently of this function.
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const navKey = (tab === 'overview') ? 'home' : tab;
  document.querySelector(`.nav-item[data-tab="${navKey}"]`)?.classList.add('active');
  // A selected page inside a collapsed group would be invisible.
  syncNavGroups();

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

  // 2. KPI Dashboard — the warehouse network, then the selected facility.
  if (tab === 'facility-dashboard') {
    document.getElementById('tab-facility-dashboard')?.classList.add('active');
    state.activeTab = 'facility-dashboard';
    // Always the whole network first. A reader arriving from the Overview
    // arrives with a network-level question, so the screen opens on the
    // network rather than on whichever site a previous visit left selected —
    // `renderKpiView()` clears the drill-down and draws the roll-up.
    renderKpiView();
    // Not awaited, and deliberately so: the facility band is already hydrated
    // and renders immediately, while the warehouse band fetches a report the
    // backend has already computed and cached per network version. Blocking
    // the whole screen on that request would make an instant render wait for a
    // round trip it does not need. The band shows its own "reading the solved
    // footprint" line in the meantime.
    renderWarehouseDashboard();
    scrollPageToTop();
    return;
  }

  // 3. Insights — every finding about the baseline, not just the three
  //    that lead on Home.
  if (tab === 'insights') {
    document.getElementById('tab-insights')?.classList.add('active');
    state.activeTab = 'insights';
    renderInsightsPage();
    scrollPageToTop();
    return;
  }

  // 4. Other Top Tabs (Digital Twin, Scenario Planning, Forecasting)
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
        // A scenario solved since the last render changes what is recommended,
        // and this is the screen that states it.
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

/**
 * The Baseline group opens and closes, and opens itself when it has to.
 *
 * The head navigates nowhere: there is no baseline screen, only the two views
 * inside it, and a header that jumped to one of its children would make the
 * other unreachable by one click. It is a disclosure, and it is keyboard
 * operable because it is not a `<button>`.
 *
 * `syncNavGroups` is called after every tab change: a group holding the
 * active page opens itself, so the sidebar can never show a selected item
 * inside a collapsed group.
 */
function bindNavGroups() {
  document.querySelectorAll('.nav-group-head').forEach((head) => {
    const group = head.closest('.nav-group');
    if (!group) return;

    const setOpen = (open) => {
      group.dataset.open = open ? 'true' : 'false';
      head.setAttribute('aria-expanded', open ? 'true' : 'false');
    };

    // The caret collapses the group, and it is the only thing that does.
    // `stopPropagation` is what keeps it from also navigating: it sits
    // inside the head, whose own click now opens a page.
    group.querySelector('.nav-caret-btn')?.addEventListener('click', (e) => {
      e.stopPropagation();
      setOpen(group.dataset.open === 'false');
    });

    // The head opens the view it stands for. A group head that only toggled
    // looked identical to the four rows that navigate and did nothing when
    // clicked; `data-group-tab` is the destination, and the group opens with
    // it so the row that is now active is visible.
    const open = () => {
      setOpen(true);
      const tab = head.dataset.groupTab;
      if (tab) navigateToTab(tab);
    };
    head.addEventListener('click', open);
    head.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
  });
  syncNavGroups();
}

function syncNavGroups() {
  document.querySelectorAll('.nav-group').forEach((group) => {
    if (group.querySelector('.nav-item.active')) {
      group.dataset.open = 'true';
      group.querySelector('.nav-group-head')?.setAttribute('aria-expanded', 'true');
    }
  });
}

function initTabs() {
  // Every sidebar entry that names a tab: Overview, Baseline's three
  // (Digital Twin, KPIs, Insights), Forecast and Scenarios. The Baseline
  // group HEAD is deliberately not one of them — it carries `data-group-tab`
  // instead, so it opens its first child without competing with that child
  // for the active mark.
  document.querySelectorAll('.nav-item[data-tab]').forEach(item => {
    item.addEventListener('click', () => {
      const tab = item.dataset.tab;
      navigateToTab(tab);
    });
  });

  bindNavGroups();

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
    // The SAME loading screen as opening a project, because it is the same
    // work: `hydrateFromBackend` reloads the network, re-solves it, asks for a
    // briefing, reads the scenarios and rebuilds the forecast. A spinning icon
    // on a 24px button was the only sign of forty seconds of that, and the
    // dashboard behind it went on showing the previous figures as though
    // nothing were happening.
    beginAnalysisLoading(project.name || 'this project', true);
    import('./integration/hydrate.js')
      .then((m) => m.hydrateFromBackend(project.id, reportAnalysisStage))
      .then(() => { endAnalysisLoading(); })
      .catch((err) => {
        endAnalysisLoading(`The analysis could not be refreshed: `
          + `${(err && err.message) || 'unknown error'}`);
      })
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

  // The twin's overlays and the recommended-change note, for the one state
  // this screen has. There is no toggle to hang them off any more.
  renderTwinStats();

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
 * The Forecast page's `.fc-main` is sized to fill the rest of the FIRST
 * screen, so the row below it — the signals — begins below the fold. It needs
 * the distance from the top of the window down to its own top edge: the global
 * top bar, the page-title row, the page's padding and the head row. Those vary
 * with the viewport and with which page is showing, and none of them can be
 * expressed in CSS from inside the grid, so the distance is measured once per
 * render and written back as a custom property.
 *
 * The Overview had a `.ov-main` sized the same way. It does not any more: with
 * the twin gone that page is four rows that flow, and a page that flows needs
 * no measurement.
 *
 * Reading the top of the element whose height we are about to set is not
 * circular: its top is fixed by what comes BEFORE it, and nothing before it
 * depends on its height.
 */
/**
 * The height of the in-page top bar, published for anything that has to sit
 * clear of it.
 *
 * `.app-global-topbar` is `position: sticky; top: 0` inside `.main-content`,
 * which is the scroll container. Anything else sticky in that container pins
 * to the same scrollport — so the scenario recommendation card, at
 * `top: 16px`, pinned SIXTEEN PIXELS FROM THE TOP OF THE SCROLLPORT, which is
 * behind the bar. Its heading and the first line of the verdict scrolled
 * underneath and stayed there.
 *
 * Measured rather than written as a constant: the bar's height is its own
 * padding plus a row of controls whose size follows the type scale, and it
 * changes with the project-name button's line count on a narrow window.
 */
/**
 * The key's dock: the glyphs it shows shut, and opening it.
 *
 * The peek is built from `NODE_STYLE`, which is the same source the markers,
 * the 3D badges and the key's own rows are drawn from — so the three symbols
 * on the handle are the three symbols on the map, by construction rather than
 * by a second list that would drift.
 */
function initTwinLegendDock() {
  const toggle = document.getElementById('twin-legend-toggle');
  const panel = document.getElementById('twin3d-legend');
  const peek = document.getElementById('twin-legend-peek');
  if (!toggle || !panel) return;

  if (peek && !peek.textContent) {
    peek.textContent = ['plant', 'dc', 'market']
      .map((k) => (NODE_STYLE[k] || {}).glyph || '').join('');
  }

  toggle.addEventListener('click', () => {
    const open = panel.hidden;
    panel.hidden = !open;
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  });

  // A click on the map is a click on the thing the key describes, so the key
  // gets out of the way rather than staying open over it.
  document.getElementById('twin-stage')?.addEventListener('click', (e) => {
    if (panel.hidden) return;
    if (document.getElementById('twin-legend-dock')?.contains(e.target)) return;
    panel.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
  });
}

function publishTopBarHeight() {
  const bar = document.querySelector('.app-global-topbar');
  const shell = document.querySelector('.main-content');
  if (!bar || !shell) return;
  const h = Math.round(bar.getBoundingClientRect().height);
  if (Number.isFinite(h) && h > 0) {
    shell.style.setProperty('--global-topbar-h', `${h}px`);
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('resize', publishTopBarHeight);
}

function sizePageToWindow(selector, varName) {
  const el = document.querySelector(selector);
  if (!el || el.offsetParent === null) return;
  const top = Math.round(el.getBoundingClientRect().top);
  if (!Number.isFinite(top) || top <= 0 || top > window.innerHeight) return;
  const shell = document.querySelector('.main-content');
  if (shell) shell.style.setProperty(varName, top + 'px');
}

/**
 * Keep Home's bottom rows clear of the fixed "Ask Netgravity" button.
 *
 * SUPERSEDED, and deliberately kept as one function rather than deleted: this
 * used to size a fixed-height body grid (`.ov-main`) to the first screen, and
 * to measure the KPI strip so four figures landed above the fold. Neither is
 * needed any more — the grid is gone with the twin, and the strip is the FIRST
 * row of the page. Both variables are cleared here so a cached stylesheet
 * cannot go on subtracting a height nobody is writing.
 *
 * What is left is the one measurement this page still needs. "Ask Netgravity"
 * is `position: fixed` at the bottom right of the VIEWPORT, and Home's last
 * two rows — the data requests and the "last analysed" line — are what end up
 * underneath it. `--ov-fab-reserve` is the width to keep clear on their right.
 *
 * Measured rather than reserved as a constant: the button's width is its
 * label's width, which follows the type scale, and it is hidden outright until
 * a user is signed in (`display: none`, so there is no rect at all).
 *
 * Visibility is read from the rect, not from `offsetParent`: that property is
 * null for EVERY `position: fixed` element, visible or not, so the obvious
 * test reports the button hidden and reserves nothing. A hidden button has a
 * zero-width rect, which is the thing actually being asked.
 */
function sizeOverviewToWindow() {
  const shell = document.querySelector('.main-content');
  if (shell) {
    shell.style.removeProperty('--ov-strip-h');
    shell.style.removeProperty('--ov-main-top');
  }

  const page = document.querySelector('#tab-home.active');
  if (!page) return;
  const box = page.getBoundingClientRect();

  const fab = document.getElementById('floating-chatbot-fab');
  const fabBox = fab ? fab.getBoundingClientRect() : null;
  const reserve = (fabBox && fabBox.width > 0)
    ? Math.max(0, Math.round(box.right - fabBox.left) + 16) : 0;
  page.style.setProperty('--ov-fab-reserve', reserve + 'px');
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
// Four rows, in the order the page is read: the figures, the findings, the
// data the analysis did not have, and when it last ran.
//
// `renderOverviewAlert` is NOT called here for the whole state of the solve —
// only for the one state a tile cannot express, an infeasible network. The
// attention feed that used to sit beside it is gone: the three insight tiles
// say the same things with the recommendation on screen rather than behind an
// internal scroller.
//
// Nor is `renderHomeDigitalTwin`. The twin preview is gone from this page; it
// is one click away under Baseline, and it is the same scene.
function renderHome() {
  renderSidebarMeta();
  renderOverviewAlert('ov-notice', { errorsOnly: true });
  renderHomeKpiStrip();
  renderHomeInsightTiles();
  renderHomeDataStrip();
  renderHomeForecast();
  renderAnalysisTimestamp();
  // After the rows have their final content, so the measurement is of the
  // page that is actually on screen.
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
// Default is the FORECAST page's element. Home carried this card until the
// insight tiles replaced it; `#ov-alert` is not in the markup any more, so a
// default naming it would make every bare call a silent no-op — including
// ingestion.js's, which is how the shortfall notice reaches a screen at all.
function renderOverviewAlert(elId = 'fc-alert', { errorsOnly = false } = {}) {
  const el = document.getElementById(elId);
  if (!el) return;
  const notice = window.__ngNetworkNotice || null;

  // ERRORS ONLY, for the Overview's notice slot. That page states an unserved
  // shortfall as a finding — the first insight tile — so repeating it in a
  // banner above the figures would be the same conclusion twice. What a tile
  // cannot state is that there is no plan at all: an infeasible network
  // produces no briefing, so the tiles would render "no findings have been
  // generated yet" over a genuine engine failure. That case, and only that
  // case, needs a banner.
  if (errorsOnly && !(notice && notice.tone === 'error')) {
    el.hidden = true;
    el.innerHTML = '';
    return;
  }
  el.hidden = false;
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
    el.querySelector('.ov-alert-link')?.addEventListener('click',
      openDemandShortfallDetail);
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

/**
 * The demand this plan cannot serve, market by market, and why.
 *
 * "View affected demand" used to change the page — it opened the Digital Twin,
 * which draws the network and says nothing about a shortfall. The reader
 * pressed a link naming a thing and got a different screen that did not
 * contain it.
 *
 * Everything below is read from the relaxation note the engine attaches to the
 * run these figures come from: the same plan, its own flows, its own reason.
 * Nothing is recomputed here and nothing is apportioned — where the engine
 * gave no market breakdown, this says so instead of spreading the total.
 */
function openDemandShortfallDetail() {
  const overlay = document.getElementById('action-drawer-overlay');
  const body = document.getElementById('action-drawer-content');
  if (!overlay || !body) return;

  const base = getOptimizedBaseCase()?.baseline || {};
  const unserved = DEMAND_SHORTFALL.unservedDemand ?? base.unservedDemand ?? null;
  const total = DEMAND_SHORTFALL.totalDemand ?? base.totalDemand ?? null;
  const rows = DEMAND_SHORTFALL.shortMarkets || [];
  const opened = DEMAND_SHORTFALL.wouldOpenCandidates || [];

  const marketName = (id) => {
    const m = MARKETS.find((x) => x.id === id);
    return m ? m.name : id;
  };
  const facilityName = (id) => {
    const f = getFacilityById(id);
    return f ? f.name : id;
  };
  const share = (v) => (total ? `${((v / total) * 100).toFixed(1)}%` : '—');

  const marketRows = rows.map((r) => `
    <tr>
      <td>
        <div style="font-weight:600">${escapeInsightText(marketName(r.market_id))}</div>
        <div class="text-xs" style="color:var(--text-3)">${escapeInsightText(r.market_id)}</div>
      </td>
      <td class="num">${formatNumber(r.demand)}</td>
      <td class="num" style="font-weight:700;color:var(--red)">${formatNumber(r.unserved)}</td>
      <td class="num">${r.demand ? `${((r.unserved / r.demand) * 100).toFixed(1)}%` : '—'}</td>
    </tr>`).join('');

  body.innerHTML = `
    <div class="card-title" style="font-size:17px;margin-bottom:2px">Demand this plan cannot serve</div>
    <div class="card-subtitle" style="margin-bottom:16px">Read from the solved
      plan these figures come from &mdash; its own flows, market by market.</div>

    <div class="grid-2" style="margin-bottom:16px">
      <div class="dash-metric-card">
        <div class="dash-metric-title">Unserved</div>
        <div class="dash-metric-val" style="color:var(--red)">${
          unserved === null ? '—' : formatNumber(unserved)}</div>
        <div class="dash-metric-sub"><span>${
          unserved === null ? 'not reported' : `${share(unserved)} of stated demand`}</span></div>
      </div>
      <div class="dash-metric-card">
        <div class="dash-metric-title">Stated demand</div>
        <div class="dash-metric-val">${total === null ? '—' : formatNumber(total)}</div>
        <div class="dash-metric-sub"><span>across every market in this upload</span></div>
      </div>
    </div>

    <div class="card-title mb-md" style="font-size:13px">Why</div>
    <div class="wh-status-note" style="margin-bottom:18px">${
      DEMAND_SHORTFALL.reason
        ? escapeInsightText(DEMAND_SHORTFALL.reason)
        : 'This run carried no relaxation note, so the engine has not stated a '
          + 'reason for the shortfall here. The figure above is the one it '
          + 'reported with the plan.'}</div>

    <div class="card-title mb-md" style="font-size:13px">Where it falls</div>
    ${rows.length ? `
    <div class="table-wrap" style="margin-bottom:18px">
      <table class="ng-table">
        <thead><tr>
          <th>Market</th>
          <th class="num">Demand</th>
          <th class="num">Unserved</th>
          <th class="num">Short by</th>
        </tr></thead>
        <tbody>${marketRows}</tbody>
      </table>
    </div>` : `
    <div class="wh-status-note" style="margin-bottom:18px">
      <strong>No market breakdown travelled with this run.</strong>
      The engine reports the shortfall per market only when it has relaxed an
      infeasible model; this total came from a plan it did not. Nothing here
      will apportion it across markets &mdash; a split nobody computed would
      read exactly like one that was.
    </div>`}

    ${opened.length ? `
    <div class="card-title mb-md" style="font-size:13px">What the plan already opened to get this far</div>
    <div class="wh-status-note" style="margin-bottom:18px">
      ${opened.map((id) => escapeInsightText(facilityName(id))).join(', ')}
      &mdash; proposed sites this plan took up. The shortfall above is what
      remains after them.
    </div>` : ''}

    <div class="text-xs" style="color:var(--text-2);line-height:1.6">
      Serving this demand is a capacity question, and testing an answer to it
      is what the Scenario Planner is for: add capacity at a short market's
      serving site, or open a proposed one, and re-solve.
    </div>`;

  overlay.classList.add('active');
  overlay.classList.add('visible');
  overlay.style.display = 'flex';
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
  // ONE DECIMAL, both branches. A DC's stored `utilPct` carried the solver's
  // full precision and printed "34.32%" beside a plant's "82.7%" — two
  // precisions for one metric on one screen, which reads as two different
  // kinds of measurement rather than one rounded two ways.
  const utilRaw = isDC ? fac.utilPct
    : (fac.throughput != null && fac.capacity
        ? (fac.throughput / fac.capacity) * 100 : null);
  const utilPct = (utilRaw === null || utilRaw === undefined
                   || Number.isNaN(Number(utilRaw)))
    ? null : Number(utilRaw).toFixed(1);
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
  // A FIGURE THE SOLVE DID NOT PRODUCE, IN WORDS.
  //
  // An em dash where a number should be reads as "nothing" or, worse, as
  // zero, and the reader has no way to tell which. These cards each carry one
  // figure, so there is room to say it — the dense health table is the one
  // place a dash still earns its keep.
  const absent = (reason = 'Not reported') =>
    `<span class="wh-absent wh-absent-label">${reason}</span>`;
  const dash = (v) => (v === null || v === undefined || Number.isNaN(v)) ? absent() : v;

  // The authoritative facility cost, from the same warehouse report row the
  // health table and the spend donut read.
  const whRow = warehouseRow(state.selectedFacility);
  const rawFacilityCost = whRow ? Number(whRow.total_facility_cost) : NaN;
  const facilityCost = Number.isFinite(rawFacilityCost) ? rawFacilityCost : null;

  // THE STOCK THIS SITE HOLDS, from that same row.
  //
  // `null` and `0` are different answers and stay different: the engine
  // reports no inventory decisions as absence WITH a reason, and a solved
  // zero as zero. `Number(null)` is 0, so each is tested before it is read.
  const stockFigure = (value) => {
    if (value === null || value === undefined || value === '') return null;
    const out = Number(value);
    return Number.isFinite(out) ? out : null;
  };
  const stockAvg = whRow ? stockFigure(whRow.avg_inventory_units) : null;
  const stockPeak = whRow ? stockFigure(whRow.peak_inventory_units) : null;
  // The engine's own words for why there is nothing, rather than a second,
  // vaguer copy of them written here.
  const stockReason = (whRow && whRow.inventory_status && whRow.inventory_status.reason)
    || 'Not reported by this solve';

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

      <!-- THE SERVICE-LEVEL CARD IS GONE FROM HERE, DELIBERATELY.

           It showed the NETWORK's demand-served figure on a card headed by a
           facility's name, in a grid where every other card is about that one
           site. Captioning it "Network-wide" did not fix that: a reader
           scanning six cards about Atlanta reads the fifth as Atlanta's too.

           Service level is a property of demand, not of a building, and this
           model does not decompose it per site — so this is not a figure
           waiting to be built, it is one that does not exist at this level.
           It belongs to the Network lens's scorecard at the top of the
           screen, where it describes the thing it is actually about. -->

      <div class="dash-metric-card">
        <div class="dash-metric-title">Facility Cost</div>
        <!-- ONE FACILITY-COST METRIC, and this is it.
             This card read kpis.totalCost, which the per-facility endpoint
             never produces — it returns utilisation, throughput, capacity and
             is_open, and no cost at all — so it was blank on every real
             network while the table beside it showed a figure. Not two
             definitions disagreeing: one real metric, and one card reading a
             field nobody fills.
             It now reads the SAME total_facility_cost the health table and
             the spend donut read, so the three cannot disagree, and it names
             what that figure covers rather than leaving the reader to assume
             it is everything.
             (No backticks in this comment: it sits inside a template literal,
             and one would end the template. See warehouse.js's header.) -->
        <div class="dash-metric-val" style="color:var(--primary)">${
          facilityCost === null ? '<span class="wh-absent wh-absent-label">Not reported</span>'
                                : formatCurrency(facilityCost)}</div>
        <div class="dash-metric-sub">
          <span>Handling: <strong>${fac.handlingCost == null
            ? 'Not reported' : formatCurrencyExact(fac.handlingCost) + '/unit'}</strong></span>
          <span class="text-muted">Fixed, opening, handling and holding — transport excluded</span>
        </div>
      </div>

      <div class="dash-metric-card">
        <div class="dash-metric-title">Stock Held on Site</div>
        <!-- IT READS THE ROW THE TABLE READS.
             This card was headed "Inventory Supply Coverage" and showed
             kpis.inventoryDays — a field FACILITY_KPIS seeds as null and
             nothing ever fills — over two hardcoded "Not reported" strings.
             So Columbus Regional DC read "Not reported" three times while the
             health table two panels away reported 415 average and 2,117 peak
             units for that same site: one screen, two answers to "does this
             site hold stock".
             Days of coverage is NOT computed here and is not implied. It
             needs a demand rate and a period length in days, and the solve
             states neither — so the card reports the quantity the solve did
             produce, and the coverage line says what is missing rather than
             printing a number nobody measured.
             A measured 0 stays 0: a site holding none is not a site nobody
             measured, and the engine's own inventory_status keeps them
             apart. -->
        <div class="dash-metric-val">${stockPeak === null ? absent('Not reported')
          : `${formatNumber(stockPeak)} <span style="font-size:16px;color:var(--text-3);font-weight:600">units</span>`}</div>
        <div class="dash-metric-sub">
          <span>Average held: <strong>${stockAvg === null
            ? absent('Not reported') : formatNumber(stockAvg)}</strong></span>
          <span class="text-muted">${stockPeak === null
            ? escAttr(stockReason)
            : 'Peak across the horizon · days of coverage not reported by this solve'}</span>
        </div>
      </div>

      <div class="dash-metric-card">
        <div class="dash-metric-title">Average Transit Lead Time</div>
        <div class="dash-metric-val">${avgLead === null ? absent('Not reported') : `${avgLead.toFixed(1)} <span style="font-size:16px;color:var(--text-3);font-weight:600">days</span>`}</div>
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
        <div class="dash-metric-val">${carbonPerUnit === null ? absent('Not reported') : `${carbonPerUnit.toFixed(2)} <span style="font-size:16px;color:var(--text-3);font-weight:600">kg CO₂e/u</span>`}</div>
        <div class="dash-metric-sub">
          <!-- Summed from the solver's per-lane carbon for the lanes attached
               to this facility, and labelled as that rather than as the
               facility's own footprint. Was a fixed "0.42 kg CO2e/u,
               14.8t CO2e/mo, down 2.1% YoY". -->
          <span>Total: <strong>${carbonTotal === null ? absent('Not reported') : formatNumber(Math.round(carbonTotal)) + ' kg'}</strong></span>
          <span class="text-muted">${carbonTotal === null ? 'no solved flow' : 'inbound + outbound lanes'}</span>
        </div>
      </div>
    `;
  }


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


  // Render the 3 Charts
  setTimeout(() => {
    // The solved per-period series for THIS site — the same record the
    // explanation beside it reads, so the picture and the sentence cannot
    // state different throughputs.
    //
    // RECORDED ON THE LINE THAT DRAWS IT, like the three network charts. The
    // chart returns whether it had a series to plot, and "explain this chart"
    // reads that — so a site the solve gave no per-period figures for is a
    // chart with nothing on it to explain, rather than a selected facility
    // that gets explained anyway.
    recordWarehouseDrawn('throughput_horizon',
      renderFacilityThroughputChart('chart-dash-throughput', whRow)
        ? [state.selectedFacility] : []);
    // The engine's own components for THIS site — the same four that sum to
    // the Facility Cost card above. It used to be handed the facility and
    // invent five figures from constants.
    renderFacilityCostBreakdownChart('chart-dash-costs', whRow);
    renderFacilityLaneFlowsChart('chart-dash-lanes', connectedLanes, state.selectedFacility);
  }, 60);

  // Corridor Summary Narrative
  // Only corridors that reported a volume. `l.flow || 0` counted an absent
  // reading as zero and folded it into a total presented as the site's whole
  // throughput.
  const reportedFlows = connectedLanes
    .map((l) => (l.flow === null || l.flow === undefined ? null : Number(l.flow)))
    .filter((v) => v !== null && Number.isFinite(v));
  const totalFlow = reportedFlows.reduce((sum, v) => sum + v, 0);
  const avgCost = connectedLanes.length > 0 ? (connectedLanes.reduce((sum, l) => sum + (l.cost || 0), 0) / connectedLanes.length).toFixed(1) : 0;
  const summaryEl = document.getElementById('dash-corridor-summary');
  if (summaryEl) {
    summaryEl.innerHTML = `
      <div style="font-weight:700;color:var(--text-1);margin-bottom:6px">Corridor Network Health</div>
      <div>• <strong>${connectedLanes.length} active transportation corridors</strong>${
        reportedFlows.length
          ? ` handle a collective flow of <strong>${formatNumber(totalFlow)} ${perPeriodLabel()}</strong>`
            + (reportedFlows.length < connectedLanes.length
                ? `, across the ${reportedFlows.length} that report a volume`
                : '')
          : ', none of which reports a solved volume'}.</div>
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
        <td class="num">${l.flow === null || l.flow === undefined
          ? absent('Not reported')
          : `${formatNumber(l.flow)} ${perPeriodLabel()}`}</td>
        <td class="num">${l.distance == null
          ? absent('Not reported') : `${formatNumber(l.distance)} km`}</td>
        <td class="num font-bold">${l.cost == null
          ? absent('Not reported') : formatCurrencyExact(l.cost)}</td>
        <td class="num">${l.leadTime == null
          ? absent('Not reported') : `${l.leadTime} days`}</td>
        <td>${l.mode ? `<span class="tag tag-muted">${l.mode}</span>`
          : absent('Not reported')}</td>
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

// The Home twin preview and its facility callout are GONE, renderers and all.
//
// They drew the 3D scene into `#home-map-twin` and a utilisation snapshot into
// `#home-twin-callout`, neither of which is in the markup any more. Leaving
// the two functions behind would be a second, unreachable copy of the Digital
// Twin's own view — which is how a screen ends up with two versions of one
// scene that disagree. The scene itself is untouched: `initTwin3D` is called
// by the Digital Twin tab (see navigateToTab), which is where it belongs, and
// the facility snapshot is on that tab and on the KPI page.

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

/**
 * Ask for the selected facility's own briefing, once.
 *
 * A scoped briefing costs a reasoning pass, so it is fetched for the facility
 * actually being looked at rather than for all of them at load time. The
 * response re-renders every consumer through the `insightsLoaded` listener
 * below.
 *
 * Shared by the Overview's insight tiles, the Insights page and the Forecast
 * page's attention card, so that whichever of them a reader lands on first is
 * what triggers the request — and the other two get it for nothing.
 */
function ensureFacilityInsights() {
  const selected = state.selectedFacility;
  if (!selected || selected === 'ALL') return;
  if (requestedFacilityInsights.has(selected)) return;
  requestedFacilityInsights.add(selected);
  // Dynamically imported, matching how this file already reaches hydrate.js:
  // a static import would pull the whole integration layer into the initial
  // bundle for a feature that only fires once a facility is chosen.
  import('./integration/hydrate.js')
    .then((m) => m.loadFacilityInsights(selected))
    .catch(() => { /* the screens render without the facility's own findings */ });
}

/**
 * Every finding about what is on screen, most serious first.
 *
 * ONE ranking, read by three screens — the Overview's tiles, the Insights
 * page and the Forecast page's attention card. It was inlined in the feed,
 * so the tiles would have had to re-derive "which finding leads" and the two
 * answers would have drifted the first time either changed.
 */
function rankedAttentionInsights() {
  ensureFacilityInsights();

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

  return insights.map(ins => ({
    kind: 'insight',
    id: ins.id,
    // The record itself, so a consumer that needs more than the feed's five
    // fields — the evidence rows, the theme, the recommended action — reads
    // them off the finding rather than being handed a copy that can go stale.
    record: ins,
    theme: ins.theme || '',
    severity: ins.severity || 'INFORMATION',
    // The chip's words. An insight's chip has always shown its severity;
    // actions need their own vocabulary in the same slot, so both carry it
    // explicitly rather than one of them being inferred at render time.
    label: ins.category || categorizeAttentionLabel(ins.impact),
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
}

/**
 * What the completeness gate needs a PERSON to supply, most urgent first.
 *
 * Action items are not findings. Nothing was solved to produce them — the
 * completeness gate read the upload and reported a column that is not there
 * — so they carry their own category and their own label rather than
 * borrowing the severity vocabulary the Reasoning Agent's findings use. A
 * missing column presented as a RISK the engine identified would be this
 * application claiming an analysis it did not run.
 *
 * The record shape changed with the store: `expectedImpact` was a prototype
 * field describing a cost and an SLA delta for a demo action, and no engine
 * produces either for a missing column. The server sends a title and a
 * sentence built from the gap itself; both are used as sent.
 */
function attentionActionItems() {
  const actionItems = HOME_ACTION_ITEMS
    .filter(act => !resolvedInsightIds.has(act.id))
    .map(act => ({
      kind: 'action',
      id: act.id,
      record: act,
      required: act.severity === 'REQUIRED',
      category: act.severity === 'REQUIRED' ? 'RISK' : 'OPPORTUNITY',
      label: act.severity === 'REQUIRED' ? 'DATA NEEDED' : 'OPTIONAL DATA',
      title: act.title,
      subtitle: act.subtitle,
      headline: '',
      isAction: true,
    }));

  // Required data first: an analysis running without a field it needs is a
  // more urgent thing to read than one that could have gone further. Both
  // sit under the engine's own findings, because a finding is a conclusion
  // about the network and an action is a request to a person — and the feed
  // is read top-down.
  actionItems.sort((a, b) => (a.category === 'RISK' ? 0 : 1) - (b.category === 'RISK' ? 0 : 1));
  return actionItems;
}

// The Home attention feed is GONE, renderer and all.
//
// It drew one scrolling card — the lead finding with a heading, a "why it
// matters" block, an impact block, a recommended change, a recommended next
// step and a collapsed list of everything else — into `#ov-attn-body`. That
// element is not in the markup any anymore: the Overview shows three insight
// tiles instead, and every finding it used to hide behind "3 further
// findings" is on the Insights page.
//
// It had no second caller. The Forecast page looks like it shares this card
// and does not: `renderForecastAttention` draws the FORECAST's own briefing
// into `#fc-attn-body`, and pointing this renderer at that element would put
// the network's finding under the forecast's question — the exact defect
// that function was written to fix. Keeping a dead renderer aimed at a live
// container is how that comes back.

if (typeof window !== 'undefined') {
  window.markAttentionItemResolved = id => resolvedInsightIds.add(id);
  // The unserved-demand breakdown, reachable from the deep-dive page as
  // well as from the tile. The drawer lives here because the markets and
  // the relaxation note it reads are this module's stores; exposing the
  // opener is how insight-detail.js reaches it without app.js and it
  // importing each other.
  window.openDemandShortfallDetail = openDemandShortfallDetail;

  // A briefing that arrives after the first paint — the network one during
  // hydration, or a facility one fetched on selection — redraws the feed and
  // the recommendation. Without this the findings were fetched and stored and
  // simply never shown until the next unrelated re-render.
  window.addEventListener('insightsLoaded', () => {
    try {
      // Everything that draws a finding. Home's tiles and the Insights page
      // read the same ranking the Forecast page's attention card does, so a
      // briefing that lands after first paint reaches all three or none.
      renderHomeInsightTiles();
      renderHomeDataStrip();
      renderInsightsPage();
      renderOverviewAlert('ov-notice', { errorsOnly: true });
      renderOverviewAlert('fc-alert');
    } catch (e) { /* a redraw must never break the page */ }
  });

  // The completeness gate's own answer, which arrives on its own request.
  // Without this the data strip rendered once, before the actions existed,
  // and stayed empty on a network that had four outstanding requests.
  window.addEventListener('actionsLoaded', () => {
    try {
      renderHomeDataStrip();
      renderInsightsPage();
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
/**
 * Facility and scenario names come from an uploaded file, so they are escaped
 * before they reach innerHTML.
 */
function escAttr(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/**
 * What the algorithm recommends changing about this network, in one sentence.
 *
 * The single reader behind both places this has to appear: the Digital Twin,
 * and Home's "Needs your attention" card. One function, so the two screens
 * cannot come to disagree about what was recommended.
 *
 * Three answers, because there are three situations and collapsing them lies:
 * a network nobody has run a plan against has not been told there is nothing
 * to change, and saying so would be a conclusion drawn from absence.
 *
 * Returns null when there is nothing worth putting in front of the reader on
 * a screen that did not ask — Home takes this branch, the twin does not.
 */
function recommendedChangeSummary({ quietWhenNothing = false,
                                    onTwin = true } = {}) {
  const change = recommendedNetworkChanges();

  if (change.status === 'CHANGES') {
    const plans = change.plans.map(escAttr).join(', ');
    // The closing sentence is about WHERE THE READER IS. "The twin
    // below" is true on the Digital Twin and false anywhere else, and
    // it was rendered verbatim on Home for as long as that page carried
    // this line — pointing at a drawing that was not below it.
    const where = onTwin
      ? 'The twin below is the network as it runs today \u2014 test the '
        + 'change as a scenario to see it drawn.'
      : 'This is a plan that was solved, not a change that has been '
        + 'made \u2014 open the Digital Twin to see the network it would '
        + 'replace.';
    return {
      quiet: false,
      html: `<div><strong>The recommended plan would</strong>
        <span class="twin-change-what">${escAttr(change.summary)}</span>.
        From ${plans}. ${where}</div>`,
    };
  }
  if (quietWhenNothing) return null;

  return {
    quiet: true,
    html: `<div><strong>${change.status === 'NO_CHANGE'
      ? 'No change is recommended.'
      : 'No change has been recommended yet.'}</strong>
      ${escAttr(change.reason).replace(/^No change is recommended\.\s*/, '')}</div>`,
  };
}


function renderTwinStats() {
  const nodeCount = PLANTS.length + DCS.length + MARKETS.length;
  const laneCount = LANES.length;

  // Which country the reader is looking at, in words.
  //
  // The map says it already — the countries with sites in them are painted
  // white against the grey of everywhere else — but only to a reader who
  // recognises the coastline, and a supply chain in Vietnam or Poland does
  // not read at a glance the way one in India does. `networkCountryLabel`
  // runs the same point-in-polygon pass that chooses the white land and the
  // frame, so the caption is a label on the geometry rather than a second
  // opinion about it. Empty (and hidden) when the sites fall in no outline,
  // because there is then no country to name.
  const countryLabel = networkCountryLabel([...PLANTS, ...DCS, ...MARKETS]);
  [['twin3d-country', 'twin3d-country-name'],
   ['map2d-country', 'map2d-country-name']].forEach(([wrapId, nameId]) => {
    const wrap = document.getElementById(wrapId);
    const name = document.getElementById(nameId);
    if (!wrap || !name) return;
    name.textContent = countryLabel;
    wrap.hidden = !countryLabel;
    wrap.title = countryLabel
      ? `The network's sites fall inside ${countryLabel}. The whole of it is `
        + 'in frame, and its land is drawn lighter than its neighbours.'
      : '';
  });


  // BOTH LEGENDS, from one function, on every refresh.
  //
  // The 3D legend was written out in the markup and the 2D one in map.js, so
  // there were two hand-maintained copies of the same key. They had drifted:
  // the 2D one keyed the DC ring in #dc2626/#f59e0b/#22c55e and the 3D one
  // keyed the identical bands in #b91c1c/#b45309/#047857, on a page whose
  // two views the reader switches between with one button.
  //
  // The redraw is not decorative either. The flow bands are derived from the
  // loaded lanes, and both legends are first built before any network exists
  // — so without this they would carry the "no corridor states a volume"
  // copy for the rest of the session.
  const legend3d = document.getElementById('twin3d-legend');
  if (legend3d) legend3d.innerHTML = twinLegendHtml(perPeriodLabel());
  refreshTwinMapLegend();
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
  // A site the client PROPOSED is not a site the solver CLOSED.
  //
  // Both arrive here with `isOpen === false` and, until this branch existed,
  // both printed "Closed by solver" — which reads, for a candidate, as though
  // the optimiser had shut down a working warehouse the client never built.
  // The wording for an existing facility is unchanged, byte for byte.
  const isCandidate = String(node.status || '').toUpperCase() === 'CANDIDATE';
  if (node.isOpen === true) {
    return isCandidate
      ? '<span class="tag tag-success">Open</span> <span class="tag tag-info">newly opened</span>'
      : '<span class="tag tag-success">Open</span>';
  }
  if (node.isOpen === false) {
    return isCandidate
      ? '<span class="tag tag-muted">Proposed — not opened</span>'
      : '<span class="tag tag-muted">Closed by solver</span>';
  }
  return isCandidate
    ? '<span class="tag tag-info">Proposed site</span>'
    : '<span class="tag tag-muted">Not solved</span>';
}

/* ═══════════════════════════════════════════════════════════════
   THE TWIN'S OWN FIGURES, for the facility and period selected
   ═══════════════════════════════════════════════════════════════
   The page carried three tables — Plants, Distribution Centres, Demand
   Markets — listing every node in the network with four columns each. They
   ignored the Facility and Period controls at the top of the screen
   completely: a reader who selected one DC got the same three full tables,
   and nothing acknowledged the selection.

   This answers the question those controls ask. Every figure is read from
   `getKpisForFacility(id, period)` — the same accessor the KPI screen uses,
   so the two screens cannot report different numbers for the same site — or
   from the authoritative base case for the whole network. Nothing is
   computed here (§9), and a figure the solve did not produce renders as a
   dash with the solver's own reason on it, never as a zero.
   ═══════════════════════════════════════════════════════════════ */




/**
 * Everything on the Digital Twin that is not the scene itself.
 *
 * Still called `renderTwinTables` because `window.renderTwinTables` is the
 * name projects.js and the hydration path already call, and renaming it
 * would be a change to four other files for no reader's benefit. What it
 * draws is the stats overlay, the legend and the metrics band; the three
 * asset tables it used to fill are gone.
 */
function renderTwinTables() {
  renderTwinStats();
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

  // What the CLIENT said this site is — not what the solver decided to do with
  // it. `openStatusTag` answers only the second question, so a proposed site
  // the optimiser declined and a real DC it shut both read "Closed by solver",
  // which is right for one of them and badly wrong for the other.
  const isCandidate = String(fac.status || '').toUpperCase() === 'CANDIDATE';

  // Utilisation in the busiest single period of the solved horizon, set by
  // hydration from the solver's own `peak_utilization_pct`. Not a second
  // calculation of the same thing: the average shown beside it and this peak
  // are two readings the MILP already published together.
  const peakPct = (fac.peakUtilPct === null || fac.peakUtilPct === undefined)
    ? null : fac.peakUtilPct;

  // A single-period solve has no busiest month to report — peak and average
  // are arithmetically the same number. Printing it twice would imply a
  // seasonal profile the upload never stated, so the panel says what is
  // actually known instead (see the peak row below).
  const solvedAvg = (fac.utilPct === null || fac.utilPct === undefined)
    ? null : fac.utilPct;
  const hasSeasonalProfile = peakPct !== null && solvedAvg !== null
    && Math.abs(peakPct - solvedAvg) > 0.05;

  // S2 P0 #5: every facility needs a risk/bottleneck status, not just Delhi.
  //
  // The band now reads from the PEAK where the solve reported one, not from
  // the horizon average. Capacity is sized for the busiest period, so a site
  // reporting "Healthy Headroom" on a yearly mean while breaching in one month
  // is exactly the failure this row exists to prevent. Where no peak was
  // reported the average still drives it, so a single-period solve behaves
  // precisely as it did before.
  //
  // Note this is deliberately allowed to differ from the map and DC-table
  // colouring, which remain on the average: those surfaces answer "how loaded
  // is this network typically", this row answers "will this site hold".
  const riskBasisPct = hasSeasonalProfile ? peakPct : utilPct;
  const riskLabel = getUtilLabel(riskBasisPct);

  // Whether there is a load to assess at all.
  //
  // This ternary used to have no such branch: anything that was not 'Critical'
  // or 'Stress' fell through to "LOW — Healthy Headroom", and `getUtilLabel`
  // returns 'Not solved' for a null utilisation. So a site the solver never
  // ran, and a proposed site it declined to open, both announced healthy spare
  // capacity — the most reassuring message on the panel, on the one facility
  // that had produced no evidence whatsoever.
  //
  // AND WHETHER THE SITE IS OPERATING AT ALL. This tested only for a null
  // utilisation, and hydration writes 0 for a site the solve did not use — so
  // a proposed DC the optimiser declined reported "0% Healthy", "Headroom
  // 21,000 units/month" and "LOW — Healthy Headroom". Every band read zero as
  // comfortable. `getUtilLabel` already takes `isOpen` and answers "Not
  // selected"; the DC table passes it and this panel did not.
  const notOperating = fac.isOpen === false;
  const riskKnown = !notOperating && riskBasisPct !== null
    && riskBasisPct !== undefined && !Number.isNaN(Number(riskBasisPct));
  const riskStatus = !riskKnown
    ? (notOperating
        ? (isCandidate ? 'Not opened — no load to assess'
                       : 'Closed in this solve — no load to assess')
        : (isCandidate ? 'Not opened — no load to assess' : 'Not solved'))
    : riskLabel === 'Critical' ? 'HIGH — Capacity Breach'
      : riskLabel === 'Stress' ? 'MEDIUM — Approaching Capacity'
        : 'LOW — Healthy Headroom';

  // ---- Headroom, and the growth that would consume it ------------------
  //
  // Not "how full is this site" but "what happens to it when demand grows".
  // Both figures below are arithmetic on numbers already on the panel —
  // capacity, and the solved utilisation at the busiest period — so they need
  // no scenario run, no forecast and no model of their own.
  //
  // The growth figure is stated against THIS SITE'S OWN volume, not against
  // network demand, and the wording says so. They are not the same number: a
  // 10% national uplift does not land as 10% here, and the solver may re-route
  // around this site entirely. Claiming otherwise would be inventing an
  // allocation the model never produced.
  const CRITICAL_PCT = 95;
  const headroomBasis = riskKnown ? Number(riskBasisPct) : null;
  const capacityUnits = Number(fac.capacity);
  const headroomUnits = (headroomBasis !== null && Number.isFinite(capacityUnits)
    && capacityUnits > 0)
    ? capacityUnits * (1 - headroomBasis / 100)
    : null;
  const growthToBreachPct = (headroomBasis !== null && headroomBasis > 0)
    ? (CRITICAL_PCT / headroomBasis - 1) * 100
    : null;

  const headroomRow = headroomUnits === null ? '' : `
    <div class="fp-stat"><span class="fp-stat-label">Headroom${hasSeasonalProfile ? ' at peak' : ''}</span><span class="fp-stat-value">${formatNumber(Math.max(0, Math.round(headroomUnits)))} ${perPeriodLabel()}</span></div>`;

  const growthRow = growthToBreachPct === null ? '' : (growthToBreachPct <= 0
    ? `
    <div class="fp-stat"><span class="fp-stat-label">Growth before breach</span><span class="fp-stat-value" style="color:var(--red)">already at or past ${CRITICAL_PCT}% — no room for growth</span></div>`
    : `
    <div class="fp-stat"><span class="fp-stat-label">Growth before breach</span><span class="fp-stat-value">+${growthToBreachPct.toFixed(0)}% <span style="color:var(--text-3);font-weight:400">on this site's own volume${hasSeasonalProfile ? ', at peak' : ''}</span></span></div>`);

  // The peak row: the solved busiest-period reading where one exists, and an
  // explicit statement of its absence where it does not. Never a repeat of the
  // average dressed up as a second finding.
  // Nothing about the busiest period of a site that was not used in this
  // solve. The row below reports on the DATA, and printing it here reads as a
  // statement about a warehouse that has no solve to have a peak in.
  const peakRow = notOperating ? '' : hasSeasonalProfile
    ? `<div class="fp-stat"><span class="fp-stat-label">Utilisation (peak)</span><span class="fp-stat-value" style="color:${getUtilColor(peakPct)}">${peakPct}% <span class="tag ${getUtilTagClass(peakPct)}">${getUtilLabel(peakPct)}</span></span></div>`
    : (peakPct !== null
      ? `<div class="fp-stat"><span class="fp-stat-label">Utilisation (peak)</span><span class="fp-stat-value" style="color:var(--text-3);font-weight:400">single period modelled — no seasonal profile in this data</span></div>`
      : '');

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
    ${(utilPct === null || utilPct === undefined || notOperating)
      ? `<div class="fp-stat"><span class="fp-stat-label">Utilisation (avg)</span><span class="fp-stat-value" style="color:var(--text-3);font-weight:400">${
          notOperating
            ? (isCandidate ? 'not opened in this solve'
                           : 'closed by the solver in this solve')
            : (isCandidate ? 'not opened in this solve' : 'not solved')}</span></div>`
      : `<div class="fp-stat"><span class="fp-stat-label">Utilisation (avg)</span><span class="fp-stat-value" style="color:${utilColor}">${utilPct}% <span class="tag ${getUtilTagClass(utilPct, fac.isOpen)}">${getUtilLabel(utilPct, fac.isOpen)}</span></span></div>`}
    ${peakRow}
    ${headroomRow}
    ${growthRow}
    ${isCandidate ? (fac.openingCost != null
      ? `<div class="fp-stat"><span class="fp-stat-label">Cost to open</span><span class="fp-stat-value">${formatCurrency(fac.openingCost)} <span style="color:var(--text-3);font-weight:400">one-time</span></span></div>`
      : `<div class="fp-stat"><span class="fp-stat-label">Cost to open</span><span class="fp-stat-value" style="color:var(--text-3);font-weight:400">not priced in the upload — the optimiser is treating this site as free to build</span></div>`) : ''}
    ${isDC && fac.fixedCostPerYear != null ? `<div class="fp-stat"><span class="fp-stat-label">Fixed Cost</span><span class="fp-stat-value">${formatCurrency(fac.fixedCostPerYear)}/year</span></div>` : ''}
    ${isDC && fac.handlingCost != null ? `<div class="fp-stat"><span class="fp-stat-label">Handling Cost</span><span class="fp-stat-value">${formatCurrencyExact(fac.handlingCost)}/unit</span></div>` : ''}
    ${forecastSection}
    <div class="fp-stat"><span class="fp-stat-label">Risk / Bottleneck${hasSeasonalProfile ? ' <span style="color:var(--text-3);font-weight:400">at peak</span>' : ''}</span><span class="fp-stat-value"><span class="tag ${riskKnown ? getUtilTagClass(riskBasisPct) : 'tag-muted'}">${riskStatus}</span></span></div>
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

  // ...and SHOW it. Adding `.open` to the panel stopped being enough when the
  // unified slide-in drawers landed: `#facility-panel` is a child of
  // `#facility-panel-overlay`, which carries `display: none` until it is given
  // `.active` / `.visible`, and the later `.facility-panel` block in style.css
  // makes the panel `position: relative` with `transform: translateX(100%)`,
  // so the older `.facility-panel.open { right: 0 }` rule has nothing left to
  // move. Clicking a node on the Digital Twin therefore built the whole panel
  // — 1,536 characters of that site's real figures — inside a container the
  // reader could not see, which is why "Click node to inspect full
  // diagnostics" appeared to do nothing.
  //
  // `actions.js`'s `closeFacilityPanel` already reached for the overlay; only
  // the opening half was left behind.
  const overlay = document.getElementById('facility-panel-overlay');
  if (overlay) {
    overlay.classList.add('active');
    overlay.classList.add('visible');
    // The element carries `style="display:none"` inline, which beats the
    // stylesheet's `display: flex !important` on `.active`.
    overlay.style.display = 'flex';
  }
};

function closeFacilityPanel() {
  document.getElementById('facility-panel')?.classList.remove('open');
  // The half that actually hides it — the same three lines
  // `closeActionDrawer` uses, so the two drawers behave identically.
  const overlay = document.getElementById('facility-panel-overlay');
  if (overlay) {
    overlay.classList.remove('active');
    overlay.classList.remove('visible');
    overlay.style.display = 'none';
  }
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
   OVERVIEW — row 1: the network's headline figures
   ═══════════════════════════════════════════════════════════════ */

/**
 * The figures that describe this network, and nothing derived.
 *
 * Every value is read from `getOptimizedBaseCase().baseline`, which hydration
 * writes straight from the authoritative KPI layer. This function does no
 * arithmetic on a business value: it formats what is there and renders a dash
 * for what is not. §9 — the frontend never calculates an authoritative KPI,
 * and a figure the solver did not report has no fallback.
 *
 * There is no delta. The strip's stylesheet has a `.home2-kpi-strip-delta`
 * rule with good/bad tones, from the band that used to sit at the top of this
 * page, and it stays unused: this build computes no previous-period baseline,
 * so any arrow rendered here would be a comparison against a number that does
 * not exist. The link to the dashboard is what the rest of the detail is for.
 */
const HOME_KPI_TILES = [
  {
    key: 'totalCost',
    name: 'Total network cost',
    format: (v) => withCurrency(formatCurrency(v)),
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2.8v18.4"/><path d="M16.6 6.6H9.9a2.9 2.9 0 0 0 0 5.8h4.2a2.9 2.9 0 0 1 0 5.8H7"/></svg>`,
  },
  {
    key: 'avgUtilization',
    name: 'Average utilisation',
    format: (v) => `${v.toFixed(1)}%`,
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3.6 16.8a9 9 0 1 1 16.8 0"/><path d="M12 16.4 16.2 10"/><circle cx="12" cy="16.8" r="1.3" fill="currentColor"/></svg>`,
  },
  {
    // The fifth figure, and the one the strip was missing.
    //
    // Cost, utilisation, service level and carbon say what the plan costs,
    // how hard it works, how much of it lands in time and what it emits.
    // None of them says how much of the demand is served AT ALL — a network
    // can hit 100% of its service level on the demand it chooses to serve
    // and strand the rest, and on this build's own test network it does
    // exactly that. `demand_fill_rate` is the KPI engine's own answer, and
    // it is the figure the first insight tile is about.
    key: 'fillRate',
    name: 'Demand fill rate',
    format: (v) => `${v.toFixed(1)}%`,
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3.6 7.4 12 3.2l8.4 4.2v9.2L12 20.8 3.6 16.6z"/><path d="M3.6 7.4 12 11.6l8.4-4.2M12 11.6v9.2"/></svg>`,
  },
  {
    key: 'sla',
    name: 'Demand within service level',
    format: (v) => `${v.toFixed(1)}%`,
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.2"/><polyline points="8.2 12.3 10.9 15 15.9 9.4"/></svg>`,
  },
  {
    // The fourth lens. Cost, utilisation and service are all operational;
    // this is the one headline figure the solve reports that none of them
    // covers, and `total_carbon_kg` is computed by the KPI engine for every
    // solved network (netgravity/metrics/kpis.py).
    //
    // The kilograms the engine reports, shown in tonnes. That is a unit on
    // one number, not a second carbon figure: nothing is summed, scaled by
    // an assumption, or combined with anything else here — it is the value
    // the engine returned, written the way a network-scale total is read.
    key: 'carbonKgCo2e',
    name: 'CO\u2082e from solved flow',
    format: (v) => `${formatNumber(Math.round(v / 1000))} t`,
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4.6 19.4c-2-4.6.3-10.2 5-12.2 2.6-1.1 6.6-1.3 9.8-.9.5 3.2.3 7.2-.8 9.8-2 4.7-7.6 7-12.2 5"/><path d="M4.2 19.8C7.4 16.6 11 13.9 15.4 12"/></svg>`,
  },
];

function renderHomeKpiStrip(rowId = 'ov-kpi-strip-row') {
  const row = document.getElementById(rowId);
  if (!row) return;

  const base = getOptimizedBaseCase() || {};
  const figures = base.baseline || {};
  // The solver's own reason, when there is one. Shown on the tile rather than
  // as a bare dash, so "no figure" is never mistaken for "zero".
  const reason = base.unavailableReason || '';

  row.innerHTML = HOME_KPI_TILES.map((tile) => {
    const raw = figures[tile.key];
    const has = typeof raw === 'number' && Number.isFinite(raw);
    const value = has ? tile.format(raw) : '—';
    const title = has ? '' : ` title="${escapeInsightText(reason
      || 'This figure was not reported by the solve for this network.')}"`;
    return `
      <div class="home2-kpi-strip-item"${title}>
        <span class="home2-kpi-strip-icon">${tile.icon}</span>
        <div>
          <div class="home2-kpi-strip-value">${escapeInsightText(value)}</div>
          <div class="home2-kpi-strip-name">${escapeInsightText(tile.name)}</div>
        </div>
      </div>`;
  }).join('');
}


/* ═══════════════════════════════════════════════════════════════
   OVERVIEW — row 2: the three findings that need a decision
   ═══════════════════════════════════════════════════════════════
   Three tiles, side by side, each saying the same four things in the same
   order (Nielsen #4 — consistency):

     1. the conclusion, in bold, with the figure it turns on;
     2. why it matters, in figures the engine reported;
     3. what to do about it, with a button that goes there;
     4. the way into the full finding.

   This replaces a single scrolling "Needs your attention" card. That card
   gave the lead finding a heading, a "why it matters" block, an "impact"
   block, a recommended change, a recommended next step and a collapsed list
   of everything else — six sections in a 500px column with an internal
   scroller, so on a 1050px window 204px of it, including the recommendation,
   sat below its own bottom edge. Three tiles put three findings on one screen
   with nothing hidden, and the rest are on the Insights page.

   COLOUR IS CARRIED BY ONE TILE. Only a RISK is tinted. A palette applied to
   every tile distinguishes nothing, and a green "opportunity" beside a red
   "unserved demand" invites a reader to weigh them against each other as two
   sides of one choice — which they are not.
   ═══════════════════════════════════════════════════════════════ */

/**
 * How a finding of each severity reads on a tile.
 *
 * `tone` is a class, not a colour: the values live in home-overview.css with
 * the rest of the palette, so a change of brand does not mean a change of
 * JavaScript.
 */
const OV_TILE_SEVERITY = {
  RISK: {
    tone: 'tone-risk',
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13.5"/><line x1="12" y1="17" x2="12" y2="17.01"/></svg>`,
  },
  OPPORTUNITY: {
    tone: 'tone-opportunity',
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 16.5 9 9.6 13 13.2 21 4.6"/><polyline points="16.4 4.6 21 4.6 21 9.2"/></svg>`,
  },
  INFORMATION: {
    tone: 'tone-information',
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.2"/><line x1="12" y1="11" x2="12" y2="16.4"/><line x1="12" y1="7.6" x2="12" y2="7.61"/></svg>`,
  },
};

/**
 * The finding's own figures, as label/value pairs a reader can check.
 *
 * "Why it matters, numerically" is not a sentence this file writes. It is the
 * evidence the engine attached to the finding — the metric, and whatever it
 * was compared against — printed with the engine's own formatting.
 * `display_value` is authoritative for anything a person reads, so the figure
 * on the tile and the figure in the sentence beside it cannot disagree.
 *
 * `skipRef` drops the row already shown as the tile's headline figure, so a
 * tile never prints the same number twice.
 */
function tileEvidenceRows(record, skipRef, limit = 2) {
  return (record.evidence || [])
    .filter((e) => e && e.ref !== skipRef && e.display_value
                   && e.display_value !== 'Not available')
    .slice(0, limit);
}

/**
 * One insight tile.
 *
 * Everything on it is read off the record. Nothing is computed here: §9 — a
 * screen that derives a business figure is a second, unverified KPI engine,
 * and a screen that writes its own recommendation is a second, unverified
 * reasoning agent.
 */
function insightTileHtml(item, isLead = false) {
  const rec = item.record || {};
  const sev = OV_TILE_SEVERITY[item.severity] || OV_TILE_SEVERITY.INFORMATION;
  const cta = insightCta(item.theme, item.severity);

  // The headline figure: the first metric the finding cites, with the engine's
  // own label under it. A finding that cites none simply has no figure line —
  // never a zero, and never a dash dressed up as a reading.
  const lead = (rec.evidence || []).find((e) => e && e.display_value
                                                && e.display_value !== 'Not available');
  const figureHtml = lead ? `
      <div class="ov-tile-figure">
        <span class="ov-tile-figure-value">${escapeInsightText(lead.display_value)}</span>
        <span class="ov-tile-figure-label">${escapeInsightText(lead.label || '')}</span>
      </div>` : '';

  // WHAT THE FINDING SAYS, in the engine's own prose and before any figure
  // is asked to speak for it. The headline alone is a conclusion with no
  // working; a reader who has not seen this network before cannot act on
  // "The plan leaves 2 candidate sites unused" without the sentence under it.
  const description = insightDescription(rec, item.title);
  const descriptionHtml = description ? `
      <p class="ov-tile-description">${escapeInsightText(description)}</p>` : '';

  // WHY IT MATTERS — the figures the finding rests on, each with the engine's
  // own label and formatting, so the number in the prose above and the number
  // in this list cannot drift apart.
  const rows = tileEvidenceRows(rec, lead ? lead.ref : null);
  const rowsHtml = rows.length ? `
        <dl class="ov-tile-evidence">
          ${rows.map((e) => `
            <div class="ov-tile-evidence-row">
              <dt>${escapeInsightText(e.label || e.ref || '')}</dt>
              <dd>${escapeInsightText(e.display_value)}</dd>
            </div>`).join('')}
        </dl>` : '';
  const whyHtml = rowsHtml ? `
      <div class="ov-tile-section">
        <div class="ov-tile-section-label">Why it matters</div>
        ${rowsHtml}
      </div>` : '';

  // RECOMMENDED ACTION. Never composed in the browser: `recommendedAction` is
  // what `/api/insights` sent, which is the Reasoning Agent's line when it
  // wrote one and the theme's own default when it did not. An empty one drops
  // the whole block rather than printing an empty heading.
  const action = (rec.recommendedAction || '').trim();
  const actionHtml = action ? `
      <div class="ov-tile-section ov-tile-action">
        <div class="ov-tile-section-label">Recommended action</div>
        <p class="ov-tile-section-text">${escapeInsightText(action)}</p>
        <button type="button" class="ov-tile-cta" data-cta-tab="${escapeInsightText(cta.tab)}">
          <span>${escapeInsightText(cta.label)}</span>
          <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M5 10h10M11 6l4 4-4 4"/></svg>
        </button>
      </div>` : '';

  // THE TINT GOES ON ONE TILE, and only when that tile is a risk.
  //
  // Not on every risk. A network with two of them would put two red cards
  // beside one white one, and a reader scanning three cards for the one to
  // start with would be given two answers — which is the same as none. The
  // findings are already ranked; the leftmost is the lead, and the tint says
  // so. Every other tile still states its own severity, in its icon and in
  // the eyebrow above the figure, so nothing is hidden by not being red.
  const tinted = isLead && item.severity === 'RISK' ? ' is-lead' : '';

  // A finding that cites no figure, carries no narrative and has no step is
  // rare — `/api/insights` supplies an action for every theme — but it is
  // reachable from a record whose evidence refs the pack could not resolve.
  // The tile stretches to its neighbours either way, so the choice is between
  // a stated absence and 200px of nothing. The absence is stated.
  const bodyHtml = (whyHtml || actionHtml)
    ? `${whyHtml}${actionHtml}`
    : `<p class="ov-tile-bare">This finding cites no figure of its own, and no
        step has been recorded against it.</p>`;
  // The description sits between the headline and the figures, so it is
  // outside the body's own `flex: 1` — the body stretches to keep the three
  // tiles' calls to action level, and prose that stretched with it would
  // leave a gap under a short sentence.

  return `
    <article class="ov-tile ${sev.tone}${tinted}" data-kind="insight" data-id="${escapeInsightText(item.id)}">
      <div class="ov-tile-head">
        <span class="ov-tile-icon" aria-hidden="true">${sev.icon}</span>
        <span class="ov-tile-eyebrow">${escapeInsightText(item.category || item.theme || '')}</span>
      </div>
      ${figureHtml}
      <!-- THE HEADING is the conclusion, not the category above it. The
           eyebrow is a label — "Capacity Risk" — and a screen reader
           tabbing the headings of this page would otherwise be read three
           category names and none of the three findings. -->
      <h3 class="ov-tile-highlight">${escapeInsightText(item.title || '')}</h3>
      ${descriptionHtml}
      <div class="ov-tile-body">
        ${bodyHtml}
      </div>
      <button type="button" class="ov-tile-detail" data-open-detail>
        <span>View detailed finding</span>
        <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M5 10h10M11 6l4 4-4 4"/></svg>
      </button>
    </article>`;
}

/**
 * The three findings that lead, as tiles.
 *
 * RISK first, because `rankedAttentionInsights()` ranks them that way — so
 * the leftmost tile is the tinted one whenever anything is wrong, and is not
 * tinted when nothing is. The tint follows the finding; it is not a slot.
 */
function renderHomeInsightTiles(containerId = 'ov-tiles') {
  const el = document.getElementById(containerId);
  if (!el) return;

  const items = rankedAttentionInsights();

  // An empty list means no insight has been generated for this network — it
  // does NOT mean the network is healthy, and this copy has never said so.
  if (!items.length) {
    el.innerHTML = `
      <div class="ov-tiles-empty">
        <p>No findings have been generated for this network yet.</p>
        <p class="ov-tiles-empty-sub">Upload a network dataset, or re-run the
          analysis, and the Reasoning Agent's conclusions appear here.</p>
      </div>`;
    return;
  }

  el.innerHTML = items.slice(0, 3).map((it, i) => insightTileHtml(it, i === 0)).join('');

  el.querySelectorAll('.ov-tile').forEach((tile) => {
    const id = tile.dataset.id;
    const open = () => {
      if (typeof window.showInsightDetail === 'function') {
        window.showInsightDetail('insight', id);
      }
    };
    tile.querySelector('[data-open-detail]')?.addEventListener('click', open);
    tile.querySelector('.ov-tile-cta')?.addEventListener('click', (ev) => {
      // The tile is not itself one big button. The two controls on it go to
      // two different places, and one clickable region with two destinations
      // is how a reader ends up somewhere they did not choose.
      ev.stopPropagation();
      const tab = ev.currentTarget.dataset.ctaTab;
      // An empty `tab` is the shortfall drawer, not a missing destination —
      // see the override in insightTileHtml().
      if (!tab) { openDemandShortfallDetail(); return; }
      if (typeof window.navigateToTab === 'function') window.navigateToTab(tab);
    });
  });
}

/* ═══════════════════════════════════════════════════════════════
   OVERVIEW — row 3: what the analysis did not have
   ═══════════════════════════════════════════════════════════════
   The completeness gate reads the upload and reports the fields that are not
   in it. Two kinds, and they must never read as one:

     REQUIRED  a field the analysis needs. Its absence is why some figure on
               this page is a dash.
     OPTIONAL  a field that would sharpen the analysis. Its absence costs
               precision, not the result.

   Both used to be buried inside the attention card's collapsed "further
   findings" list, seventh to tenth on a real upload — below the fold, inside
   a card that scrolled. A request nobody scrolls to has not been raised.

   EVERY REQUEST, AND THE FULL WIDTH.

   This showed two per group with an "N more" link into the Insights page.
   Two is the wrong number for a list whose whole purpose is to be actioned:
   a reader cannot tell whether the third one matters without opening another
   screen, and the count that replaced them ("3 more optional fields") is a
   statistic rather than something anyone can act on. All of them are here.

   And each group is one band across the page rather than a column beside its
   neighbour. Two side-by-side cards are only ever the same height by
   accident — with two required fields and three optional ones, the shorter
   one ends in a block of empty tint. A full-width band ends where its last
   request ends.
   ═══════════════════════════════════════════════════════════════ */

const OV_DATA_GROUPS = [
  {
    id: 'required',
    tone: 'tone-required',
    title: 'Critical missing data',
    blurb: 'The analysis is running without these. Its totals are computed from the records that do state them.',
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13.5"/><line x1="12" y1="17" x2="12" y2="17.01"/></svg>`,
  },
  {
    id: 'optional',
    tone: 'tone-optional',
    title: 'Optional data to enhance',
    blurb: 'Not needed for a result. Supplying them sharpens the one you have.',
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.2 17.6h5.6M10 20.6h4"/><path d="M12 3.2a5.6 5.6 0 0 0-3.3 10.1v1.4h6.6v-1.4A5.6 5.6 0 0 0 12 3.2z"/></svg>`,
  },
];

/**
 * Whether this request has already gone out, and when.
 *
 * `lastSent` is the endpoint's own dispatch record for this action —
 * `{sent_at, recipients, result}` — and the band never showed it. So a list
 * of four requests looked identical whether one of them had been emailed an
 * hour ago or never, and the mistake it invited is asking the same person
 * for the same column twice.
 *
 * `stubbed` is reported as saved rather than sent, because no message left
 * the machine: this build ships with no outbound credential, and a stub
 * described as a send is the one outcome that makes the feature worse than
 * not having it.
 *
 * Returns null when nothing has been sent, so the row carries no chip at all
 * rather than one saying "not yet" — the absence of the chip is the state.
 */
function dataSentChip(item) {
  const sent = (item.record || {}).lastSent;
  if (!sent || !sent.sent_at) return null;
  const at = new Date(sent.sent_at);
  if (Number.isNaN(at.getTime())) return null;
  const when = at.toDateString() === new Date().toDateString()
    ? 'today'
    : at.toLocaleDateString([], { day: 'numeric', month: 'short' });
  if (sent.result === 'failed') return { label: `Send failed ${when}`, tone: 'is-failed' };
  if (sent.result === 'stubbed') return { label: `Saved ${when}`, tone: '' };
  if (sent.result === 'partial') return { label: `Partly sent ${when}`, tone: 'is-failed' };
  return { label: `Asked ${when}`, tone: 'is-sent' };
}

function dataStripCardHtml(group, items) {
  // The count belongs in the heading, not in a "+N more" link at the bottom:
  // a reader deciding whether to deal with this now needs to know it is four
  // fields before they start reading, and all four are below it.
  const n = items.length;
  return `
    <section class="ov-data-card ${group.tone}">
      <div class="ov-data-card-head">
        <span class="ov-data-card-icon" aria-hidden="true">${group.icon}</span>
        <div class="ov-data-card-headtext">
          <h3 class="ov-data-card-title">${escapeInsightText(group.title)}
            <span class="ov-data-card-count">${n} ${n === 1 ? 'field' : 'fields'}</span>
          </h3>
          <p class="ov-data-card-blurb">${escapeInsightText(group.blurb)}</p>
        </div>
      </div>
      <ul class="ov-data-list">
        ${items.map((it) => {
          const sent = dataSentChip(it);
          return `
          <li class="ov-data-item">
            <div class="ov-data-item-text">
              <span class="ov-data-item-title">${escapeInsightText(it.title)}</span>
              ${sent ? `<span class="ov-data-item-sent ${sent.tone}">${escapeInsightText(sent.label)}</span>` : ''}
              ${it.subtitle ? `<span class="ov-data-item-sub">${escapeInsightText(it.subtitle)}</span>` : ''}
            </div>
            <!-- BOTH WAYS TO ANSWER, on the row.
                 There was one button, labelled "Request this data", and it
                 opened a page offering two things: send the request, or
                 upload the file yourself. Most of the time the person
                 reading this HAS the workbook — they uploaded the last one
                 — so the single most likely action was behind a button
                 promising an email, and its label described only the other
                 one. Nielsen #4: a control says what it does. -->
            <div class="ov-data-item-actions">
              <button type="button" class="ov-data-item-cta is-quiet"
                      data-upload-for="${escapeInsightText(it.id)}">
                <span>Upload it</span>
              </button>
              <button type="button" class="ov-data-item-cta" data-action-id="${escapeInsightText(it.id)}">
                <span>${sent ? 'Ask again' : 'Ask for it'}</span>
                <svg width="13" height="13" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M5 10h10M11 6l4 4-4 4"/></svg>
              </button>
            </div>
          </li>`; }).join('')}
      </ul>
    </section>`;
}

function renderHomeDataStrip(containerId = 'ov-data-strip') {
  const el = document.getElementById(containerId);
  if (!el) return;

  const actions = attentionActionItems();
  const required = actions.filter((a) => a.required);
  const optional = actions.filter((a) => !a.required);

  if (!actions.length) {
    // "Nothing is missing" is a claim, and it is only true once the gate has
    // actually run. It runs with the briefing, so a network that has findings
    // has been checked and one that has none has not — and the second says
    // nothing at all rather than issuing a clean bill of health it cannot
    // support.
    const analysed = rankedAttentionInsights().length > 0;
    el.classList.toggle('is-empty', !analysed);
    el.innerHTML = analysed ? `
      <div class="ov-data-clear">
        <span class="ov-data-clear-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.4"/><polyline points="8.4 12.2 11 14.8 15.8 9.6"/></svg>
        </span>
        <span>Your upload carried every field this analysis asked for. No data
          requests are outstanding.</span>
      </div>` : '';
    return;
  }

  el.classList.remove('is-empty');
  el.innerHTML = [
    required.length ? dataStripCardHtml(OV_DATA_GROUPS[0], required) : '',
    optional.length ? dataStripCardHtml(OV_DATA_GROUPS[1], optional) : '',
  ].filter(Boolean).join('');

  el.querySelectorAll('[data-action-id]').forEach((btn) => {
    btn.addEventListener('click', () => {
      // The same page the feed opened: one destination for one request, with
      // the recipients, the draft the server composed, and the send.
      if (typeof window.showInsightDetail === 'function') {
        window.showInsightDetail('action', btn.dataset.actionId);
      }
    });
  });

  // The workbook, straight from here. The same call the detail page's
  // "Upload the data instead" makes — a reader who already has the column
  // should not have to walk through a page about emailing someone for it.
  el.querySelectorAll('[data-upload-for]').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (typeof window.showUploadData !== 'function') return;
      const project = typeof window.getCurrentProject === 'function'
        ? window.getCurrentProject() : null;
      window.showUploadData(project);
    });
  });
}

/* ═══════════════════════════════════════════════════════════════
   INSIGHTS PAGE — every finding, not just the three that lead
   ═══════════════════════════════════════════════════════════════
   Home shows three tiles. This is what "View more insights" opens, and what
   the sidebar's Baseline > Insights entry points at: the same records, all of
   them, filterable, each row opening the same deep dive a tile does.

   Nielsen #6 — the filters carry their own counts, so a reader can see there
   are two risks without first selecting "Risks" and counting the rows.
   ═══════════════════════════════════════════════════════════════ */

const INSIGHTS_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'RISK', label: 'Risks' },
  { id: 'OPPORTUNITY', label: 'Opportunities' },
  { id: 'INFORMATION', label: 'For information' },
  { id: 'action', label: 'Data needed' },
];

function insightRowHtml(item) {
  const rec = item.record || {};
  const sev = OV_TILE_SEVERITY[item.severity] || OV_TILE_SEVERITY.INFORMATION;
  const isAction = Boolean(item.isAction);
  const tone = isAction
    ? (item.required ? 'tone-risk' : 'tone-opportunity') : sev.tone;
  const icon = isAction ? OV_DATA_GROUPS[item.required ? 0 : 1].icon : sev.icon;

  const lead = isAction ? null
    : (rec.evidence || []).find((e) => e && e.display_value
                                       && e.display_value !== 'Not available');

  // A data request's "action" is the request itself, and it is stated rather
  // than borrowed from the reasoning vocabulary: nothing was solved to
  // produce it, so it must never read as something the engine concluded.
  const action = isAction
    ? (item.required
        ? 'Request this field from whoever owns it. Until it arrives the analysis runs without it.'
        : 'Request this field when you can. The result stands without it.')
    : (rec.recommendedAction || '').trim();

  // The engine's own prose, minus whatever the headline already said. The row
  // used to print `subtitle` — the narrative's FIRST SENTENCE — which on a
  // finding whose headline is that sentence was the heading again, in grey.
  const description = isAction
    ? String(item.subtitle || '')
    : insightDescription(rec, item.title);

  // ONE figure, and the finding's own lead. A second one was tried here and
  // taken out: the engine's description already carries it — "Average
  // utilisation is 56.23% and the busiest site at 77.14%" — so the chip
  // beside it said nothing new, and two right-aligned figures moved the
  // primary one left on the rows that had two. The figure a reader scans the
  // page for has to be in the same place on every row.
  const figureHtml = lead ? `
        <div class="insp-row-figure">
          <span class="insp-row-figure-value">${escapeInsightText(lead.display_value)}</span>
          <span class="insp-row-figure-label">${escapeInsightText(lead.label || '')}</span>
        </div>` : '';

  return `
    <article class="insp-row ${tone}" data-kind="${item.kind}" data-id="${escapeInsightText(item.id)}"
             role="button" tabindex="0">
      <span class="insp-row-icon" aria-hidden="true">${icon}</span>

      <div class="insp-row-main">
        <div class="insp-row-eyebrow">${escapeInsightText(item.label || item.category || '')}</div>
        <h3 class="insp-row-title">${escapeInsightText(item.title || '')}</h3>
        ${description ? `<p class="insp-row-sub">${escapeInsightText(description)}</p>` : ''}
      </div>

      <!-- BESIDE the finding, not under it. Stacked, every row was 157px of
           which about a third was empty tint to the right of a one-line
           sentence in a box the width of the page. -->
      ${action ? `
      <div class="insp-row-action">
        <div class="insp-row-action-label">Recommended action</div>
        <p class="insp-row-action-text">${escapeInsightText(action)}</p>
      </div>` : '<div class="insp-row-action is-empty"></div>'}

      <div class="insp-row-right">
        ${figureHtml}
        <span class="insp-row-link">
          <span>${isAction ? 'Open this request' : 'View detailed finding'}</span>
          <svg width="13" height="13" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M5 10h10M11 6l4 4-4 4"/></svg>
        </span>
      </div>
    </article>`;
}

function renderInsightsPage() {
  const filterEl = document.getElementById('insp-filters');
  const listEl = document.getElementById('insp-list');
  if (!listEl) return;

  const insights = rankedAttentionInsights();
  const actions = attentionActionItems();
  const all = [...insights, ...actions];

  // The one recommendation that ranks the findings rather than following from
  // any single one of them. `getNetworkRecommendation()` is the engine's own,
  // with its grounding caveat attached; it is stated once, at the head of the
  // list, because five rows each carrying a step leave a reader working out
  // which of them is the overall answer.
  const recEl = document.getElementById('insp-rec');
  if (recEl) {
    const rec = getNetworkRecommendation();
    // What the algorithm recommends CHANGING, as distinct from what it
    // recommends doing next: close this, open that, read off the plans that
    // were actually solved. It was on Home, inside the attention card; this
    // page is where it belongs now, beside the recommendation it makes
    // concrete. Quiet when nothing has been solved — a reader who has run no
    // plan is not owed a line saying so, and the Digital Twin states it for
    // the reader who goes looking.
    const change = recommendedChangeSummary({ quietWhenNothing: true,
                                              onTwin: false });
    recEl.hidden = !rec;
    recEl.innerHTML = rec ? `
      <div class="insp-rec-head">
        <span class="insp-rec-icon" aria-hidden="true">${OV_ICONS.chart}</span>
        <div>
          <div class="insp-rec-label">What I recommend for this network</div>
          <p class="insp-rec-headline">${escapeInsightText(rec.headline)}</p>
        </div>
      </div>
      <p class="insp-rec-text">${escapeInsightText(rec.text)}</p>
      ${change ? `<div class="insp-rec-change">${change.html}</div>` : ''}
      ${rec.limitation
        ? `<p class="insp-rec-limit">${escapeInsightText(rec.limitation)}</p>` : ''}
      <button type="button" class="insp-rec-cta" data-action="navigateToTab"
              data-arg="scenarios">
        <span>${escapeInsightText(rec.cta)}</span>
        <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M5 10h10M11 6l4 4-4 4"/></svg>
      </button>` : '';
    // No click handler: `data-action` is dispatched by the delegated listener
    // in actions.js, and a second one here would navigate twice.
  }

  const countFor = (id) => id === 'all' ? all.length
    : id === 'action' ? actions.length
    : insights.filter((i) => i.severity === id).length;

  const active = INSIGHTS_FILTERS.some((f) => f.id === state.insightsFilter)
    ? state.insightsFilter : 'all';
  state.insightsFilter = active;

  if (filterEl) {
    // A filter that would empty the list is disabled rather than removed: a
    // control that appears and disappears between renders is a control a
    // reader cannot learn (Nielsen #4).
    filterEl.innerHTML = INSIGHTS_FILTERS.map((f) => {
      const n = countFor(f.id);
      return `
        <button type="button" role="tab" class="insp-filter${f.id === active ? ' is-active' : ''}"
                data-filter="${f.id}" aria-selected="${f.id === active}"
                ${n === 0 && f.id !== 'all' ? 'disabled' : ''}>
          <span>${escapeInsightText(f.label)}</span>
          <span class="insp-filter-count">${n}</span>
        </button>`;
    }).join('');
    filterEl.querySelectorAll('.insp-filter').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.insightsFilter = btn.dataset.filter;
        renderInsightsPage();
      });
    });
  }

  const rows = active === 'all' ? all
    : active === 'action' ? actions
    : insights.filter((i) => i.severity === active);

  if (!rows.length) {
    listEl.innerHTML = `<div class="insp-empty">${all.length
      ? 'Nothing in this network matches that filter.'
      : 'No findings have been generated for this network yet. Upload a '
        + 'network dataset, or re-run the analysis, and the Reasoning '
        + 'Agent’s conclusions appear here.'}</div>`;
    return;
  }

  listEl.innerHTML = rows.map(insightRowHtml).join('');
  const open = (row) => {
    if (typeof window.showInsightDetail === 'function') {
      window.showInsightDetail(row.dataset.kind, row.dataset.id);
    }
  };
  listEl.querySelectorAll('.insp-row').forEach((row) => {
    row.addEventListener('click', () => open(row));
    // A row is a button, so it answers to a keyboard like one.
    row.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        open(row);
      }
    });
  });
}

/* ═══════════════════════════════════════════════════════════════
   FORECAST — the signals influencing this network
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
// Named for the Home card it was written for; that card is gone and the
// Forecast tab is the only caller now. The default follows the caller rather
// than pointing at an element that no longer exists.
function renderHomeSignals(rowId = 'fc-signals-row') {
  const row = document.getElementById(rowId);
  if (!row) return;

  if (!EXTERNAL_SIGNALS.length) {
    row.innerHTML = `<div class="ov-signals-empty">
      No external signals were found in your upload. A signals sheet &mdash;
      events, their dates and their markets &mdash; appears here when one is
      included.</div>`;
    return;
  }

  // WHAT THE ROUTER ACTUALLY DID with this signal.
  //
  // This used to regex-match `sig.intendedUse`, which `loadStructure` sets to
  // the constant "Recorded from your upload — not yet routed into a forecast"
  // for every signal — so "Not yet applied" was the only outcome the chip
  // could ever show, whatever the forecast had done with it. Signals ARE
  // routed (`_uploaded_signals_for` -> `signal_router.route_for_forecast`);
  // the chip had no way to know.
  //
  // `applied_signal_ids` is the routing's own answer, derived from the
  // per-series `signal_adjustments` the enricher recorded.
  const signalState = FORECAST_BRIEFING.signals || null;
  const appliedIds = new Set((signalState && signalState.applied_signal_ids) || []);
  const forecastRan = Boolean(signalState);

  // Three on the row, matching the mockup; the rest are behind "View all
  // signals", which is why that link is there rather than decorative.
  row.innerHTML = EXTERNAL_SIGNALS.slice(0, 3).map((sig) => {
    const applied = appliedIds.has(sig.id);
    // Not "context only" — that was a guess dressed as a category. Either the
    // forecast has not run, or it ran and this signal did not move it.
    const tone = applied ? 'ok' : forecastRan ? 'info' : 'pending';
    const label = applied ? 'Used in forecast'
      : forecastRan ? 'Did not change the forecast' : 'No forecast run yet';
    return `
      <div class="ov-signal">
        <span class="ov-signal-icon">${signalIconSvg(sig.type)}</span>
        <div class="ov-signal-text">
          <div class="ov-signal-title" title="${escapeInsightText(sig.title)}">${escapeInsightText(sig.title)}</div>
          <div class="ov-signal-meta">Source: ${escapeInsightText(sig.source)}
            &nbsp;&middot;&nbsp; ${escapeInsightText(sig.publishedDate)}</div>
          <span class="ov-signal-chip tone-${tone}" title="${escapeInsightText(
            applied
              ? 'The forecaster applied an adjustment from this signal. The rule that fired is recorded against it.'
              : forecastRan
                ? 'This signal reached the router and did not adjust any series — usually because it names no market or facility in this network, or its confidence is below the guardrail.'
                : 'No forecast has been produced for this network yet, so nothing has been routed.')}"
                >${OV_SIGNAL_CHIP[tone]}${label}</span>
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
