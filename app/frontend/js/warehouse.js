/**
 * NetGravity — Warehouse Deep Dive
 * =================================
 * Peak against average, ranked, and sized against a stated growth rate.
 *
 * WHERE IT LIVES
 * --------------
 * The upper half of the KPI dashboard — one screen with the selected-facility
 * detail that screen already had, not a band above it. There is one facility
 * population here and one report of it, and the questions come in this order:
 *
 *   * Which of my twenty sites should I look at first?
 *   * Is the site that looks comfortable comfortable in its worst month?
 *   * — and only then — what is happening at that one site?
 *
 * ONE EXPORT
 * ----------
 * This file writes no file. The screen's export is a PDF of the view as it
 * is drawn — the print stylesheet in `style.css` — so there is no
 * second serialisation here to drift from what is on screen.
 *
 * ONE VOCABULARY
 * --------------
 * A DC and a warehouse are the same site. This band is the standard facility
 * view of the network, not a report about a separate kind of building, so it
 * uses the words the rest of the product already uses: "Distribution Centre"
 * for the role, "facility" for the collective. Two words for one thing sends
 * the reader hunting for a distinction there is none of.
 *
 * The identifiers below still read `wh-` and the endpoint is still
 * `/api/kpis/warehouse`. Those are not shown to anyone, and renaming a stored
 * document's keys to change a caption would invalidate every cached analysis
 * for no reader's benefit.
 *
 * WHAT IT DOES NOT DO
 * -------------------
 * It computes nothing. Every figure is read from `/api/kpis/warehouse`, which
 * reads the solved state through the authoritative KPI registry. The one thing
 * this file decides is ORDER — which row to put at the top — and it takes the
 * band that decides that from the backend too.
 *
 * NEVER PUT A BACKTICK IN AN HTML COMMENT IN THIS FILE
 * -----------------------------------------------------
 * The markup below is written in template literals, so a backtick inside an
 * HTML comment ENDS the template and the rest of the function is parsed as
 * code. It happened once, in a comment explaining a units label: this module
 * stopped parsing, `app.js` imports it statically so the whole module graph
 * went with it, and the visible symptom was three screens away — the landing
 * page lost its world map, because `initLandingPage()` never ran. The full
 * test suite passed throughout, because every frontend test here reads these
 * files as text and no JS engine exists in the test environment to parse them.
 *
 * ABSENCE IS RENDERED, NOT SKIPPED
 * --------------------------------
 * Three sections here can be empty because an input is missing rather than
 * because there is nothing to report: the growth sizing (no rate stated), the
 * before-and-after (no second solve run), and per-site stock (a model that
 * carries none). Each arrives with a status and a reason, and each is rendered
 * as that reason. A blank table would read as "nothing to report", which is
 * the one thing none of them means.
 */

import { formatCurrency, formatNumber, fmtNum,
         perPeriodLabel, SOLVE_HORIZON, horizonLabel } from './data.js';
import { kpiService } from './integration/services/kpi-service.js';
import { getActiveProjectId } from './integration/project-context.js';
import { renderWarehouseUtilisationChart, renderWarehouseSpendChart,
         renderWarehouseStatusMixChart, renderWarehouseHeadroomChart,
         renderWarehouseStockChart,
         // One definition of the status colours, shared with every chart that
         // speaks them. A second copy here is how a state comes to be drawn in
         // two colours on one screen.
         BAND_COLOUR } from './charts.js';

/** Utilisation at or above which a period is a bottleneck. Mirrors the
 *  backend's `config/defaults.py`; every band the screen shows is computed
 *  server-side, and this is used only to caption the threshold line. */
const OVER_PCT = 90;

const state = {
  /** The last report the backend returned, so a re-render costs no request. */
  report: null,
  /** Which project it describes, so a project switch cannot show one
   *  network's sites under another's name. */
  projectId: null,
  loading: false,
  error: null,
};

/**
 * WHICH SITES THE SECTIONS BELOW THE SCORECARD ARE ABOUT.
 *
 * The four scorecard cards are NOT filtered by this — they are the whole
 * network's authoritative figures, the same ones the Overview states, and they
 * carry a caption saying so. Everything under the domain tabs is: the
 * attention list, the four charts and the health table all read `visibleRows()`
 * so a filter can never leave one card network-wide while its neighbours
 * narrow, which is the state that makes a reader mistrust the whole screen.
 *
 * Set by `kpi-view.js`, which owns the controls. Nothing here computes a KPI:
 * every row is the backend's own record and every count is a count OF those
 * records, made under a label saying what was counted.
 */
const view = {
  /** 'network' — every facility; 'dc' — storage sites; 'plant' — the rest. */
  domain: 'network',
  region: 'all',
  status: 'all',
};

/** DC and WAREHOUSE are the same site — this mirrors `STORAGE_ROLES` in
 *  `netgravity/orchestrator/metrics/warehouse_deep_dive.py`. A role the set
 *  does not name is a production/supply site and belongs to the Plants tab, so
 *  the two tabs together always show every facility the solve reported. */
const STORAGE_ROLES = new Set(['WAREHOUSE', 'DC', 'DEPOT', 'DARKSTORE', 'CROSS_DOCK']);

function isStorageRole(role) {
  return STORAGE_ROLES.has(String(role || '').toUpperCase());
}

/** Every row the current domain and filters leave on screen. */
function visibleRows() {
  const rows = state.report?.health_kpis || [];
  return rows.filter((k) => {
    const storage = isStorageRole(k.role);
    // 'network' narrows by nothing: every facility the solve reported, which
    // is what makes it the scope the Overview's figures describe.
    if (view.domain === 'dc' && !storage) return false;
    if (view.domain === 'plant' && storage) return false;
    if (view.region !== 'all' && String(k.region || '') !== view.region) return false;
    if (view.status !== 'all' && String(k.health_band) !== view.status) return false;
    return true;
  });
}

/** Narrow the screen, then redraw it. Called by `kpi-view.js` on every
 *  tab switch and filter change. */
export function setWarehouseFilter(next) {
  Object.assign(view, next || {});
  if (state.report) renderAll();
}

/**
 * What the filter controls can offer, and what the current selection leaves.
 *
 * The options come from the report rather than from a fixed list, so a network
 * with no cross-docks never offers "Cross-dock" as a status nobody can select
 * their way into an empty screen with.
 */
export function warehouseFacets() {
  const rows = state.report?.health_kpis || [];
  const inDomain = rows.filter((k) => (view.domain === 'network' ? true
    : view.domain === 'dc' ? isStorageRole(k.role) : !isStorageRole(k.role)));
  const shown = visibleRows();
  return {
    ready: Boolean(state.report),
    regions: [...new Set(inDomain.map((k) => String(k.region || '')).filter(Boolean))].sort(),
    statuses: BAND_ORDER.filter((band) => inDomain.some((k) => k.health_band === band))
      .map((band) => ({ value: band, label: BAND_LABEL[band] || band })),
    entities: shown
      .map((k) => ({ id: k.facility_id, name: k.facility_name || k.facility_id }))
      .sort((a, b) => a.name.localeCompare(b.name)),
    shownCount: shown.length,
    openCount: shown.filter((k) => k.is_open).length,
    domainCount: inDomain.length,
  };
}

/** One row, by id — `kpi-view.js` reads the selected site's band and role
 *  from the same record the table below it drew. */
export function warehouseRow(facilityId) {
  return (state.report?.health_kpis || [])
    .find((k) => k.facility_id === facilityId) || null;
}

/**
 * WHICH SITES EACH CHART ACTUALLY DREW, recorded as it drew them.
 *
 * Not the same list for every chart, and not the same as `visibleRows()`.
 * Each chart applies its own selection on top of the filters — the
 * utilisation chart takes the twelve busiest OPEN sites, headroom takes open
 * sites that state a capacity, stock takes the sites that report one — so a
 * site can be in the table and absent from a chart above it.
 *
 * This exists because "Explain this chart" was briefing over every visible
 * row, and so described a proposed site the headroom chart had deliberately
 * left out: a confident paragraph about a bar that is not there. Recording
 * the ids at the moment of drawing keeps the explanation and the picture the
 * same set by construction, rather than by two copies of a filter that would
 * drift the first time either was changed.
 */
const drawn = {
  peak_vs_average: [],
  capacity_carried: [],
  stock_held: [],
  //: The facility chart, recorded from the drill-down rather than from here.
  //: It is the one explainable chart this module does not draw, and it was
  //: the one chart whose explanation could not be checked against what was
  //: on screen — so `subjectOf` handed over a facility id whatever the chart
  //: had done, and a site whose solve carries no per-period series got a
  //: confident paragraph about a horizon the chart had drawn nothing of.
  throughput_horizon: [],
};

export function warehouseDrawnIds(chart) {
  return [...(drawn[chart] || [])];
}

/**
 * Record what a chart drew, for a chart drawn elsewhere.
 *
 * The three above record themselves on the line that draws them. The facility
 * throughput chart is drawn by `renderFacilityDashboard` in `app.js`, which
 * is the only place that knows WHICH site is on screen — so it records
 * through here, on the same line, rather than this module keeping a second
 * copy of a selection it does not own.
 */
export function recordWarehouseDrawn(chart, ids) {
  if (!(chart in drawn)) return;
  drawn[chart] = Array.isArray(ids) ? [...ids] : [];
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function el(id) { return document.getElementById(id); }

const BAND_LABEL = {
  CRITICAL: 'Over capacity',
  TIGHT: 'Tight',
  HEALTHY: 'Healthy',
  UNDERUSED: 'Under-used',
  NOT_OPERATING: 'Not in this plan',
};

/** The order the screen reads in: what is broken, then what is at its limit,
 *  then what is wasteful, then what is fine, then what is not running. */
const BAND_ORDER = ['CRITICAL', 'TIGHT', 'UNDERUSED', 'HEALTHY', 'NOT_OPERATING'];


function bandTag(band) {
  const key = String(band || 'HEALTHY');
  return `<span class="tag tag-band-${key.toLowerCase()}">${esc(BAND_LABEL[key] || key)}</span>`;
}

/** A figure the engine did not produce, shown as absent with its reason on
 *  hover — never as 0, which would state a measurement nobody made.
 *
 *  For a TABLE CELL, where a column of words would be unreadable and the
 *  column heading already says what is missing. Anywhere a figure stands on
 *  its own, use `absentLabel()`. */
function absent(reason) {
  return `<span class="wh-absent" title="${esc(reason || 'Not reported by this solve')}">&mdash;</span>`;
}

/**
 * The same absence, in WORDS.
 *
 * A dash carrying its reason on hover is not enough on a card: most readers
 * never hover, and an em dash where a number should be reads as "nothing" or,
 * worse, as zero. A card states the absence; only a dense table gets a dash.
 */
function absentLabel(reason = 'Unavailable') {
  return `<span class="wh-absent wh-absent-label">${esc(reason)}</span>`;
}

/**
 * One word for a role.
 *
 * The engine keeps DC and WAREHOUSE apart because an upload may say either.
 * They are the same site, so both resolve to the one name the rest of the
 * product uses — the map legend, the 3D twin and the scenario toolbox all say
 * "Distribution Centre" — rather than to a compound that shows the reader the
 * seam between two spellings.
 */
function roleLabel(role) {
  const key = String(role || '').toUpperCase();
  return ({
    DC: 'Distribution Centre',
    WAREHOUSE: 'Distribution Centre',
    DEPOT: 'Depot',
    DARKSTORE: 'Dark store',
    CROSS_DOCK: 'Cross-dock',
    PLANT: 'Plant',
    SUPPLIER: 'Supplier',
  })[key] || key;
}

function multiPeriod() {
  return (state.report?.periods_modelled || SOLVE_HORIZON.periodsModelled || 1) > 1;
}

// ─── Loading and fetching ───────────────────────────────────

export async function loadWarehouseReport() {
  const projectId = getActiveProjectId();
  if (!projectId) {
    state.error = 'No project is open, so there is no network to read.';
    state.report = null;
    return null;
  }
  state.projectId = projectId;
  state.loading = true;
  state.error = null;
  try {
    const response = await kpiService.getWarehouseDeepDive(projectId);
    state.report = response.warehouse || null;
    return state.report;
  } catch (err) {
    state.error = (err && err.message) || 'The facility analysis could not be read.';
    return null;
  } finally {
    state.loading = false;
  }
}

// ─── Sections ───────────────────────────────────────────────

function renderBasis() {
  const node = el('wh-basis');
  if (!node) return;
  const report = state.report;
  if (!report) { node.textContent = '—'; return; }
  const span = horizonLabel();
  // What the figures are OF. A peak means nothing without the horizon it is
  // the peak of, and on a one-period solve there is no peak to speak of —
  // saying so is what stops the screen implying a seasonal reading.
  node.textContent = multiPeriod()
    ? `${span} · peak figures are the busiest single period of the horizon`
    : 'One period modelled · peak and average are the same figure';
}

/**
 * The scorecard for the population currently selected.
 *
 * NOT the network's fixed totals. It read `report.n_warehouses_open` and the
 * other whole-network counters whatever the tabs and filters said, so the
 * first card announced "Distribution facilities 8 of 9" above a screen showing
 * three plants — a contradiction between the top of the screen and the rest of
 * it, which is the exact failure this screen was rebuilt to remove.
 *
 * Every figure here is now COUNTED over `visibleRows()`. That is counting, not
 * computing: each row's `health_band`, `is_open` and per-site utilisation are
 * the backend's own, and the one mean is a mean of those authoritative
 * per-site values — the same one the report makes for the whole network.
 * §9 still holds: nothing here calculates a KPI the engine did not report.
 *
 * The Network lens does not use this at all. It shows the four figures the
 * Overview states, drawn by Overview's own renderer.
 */
function renderSummary() {
  const grid = el('wh-summary-grid');
  if (!grid) return;
  if (!state.report) { grid.innerHTML = ''; return; }

  const rows = visibleRows();
  const open = rows.filter((k) => k.is_open);
  const tight = open.filter((k) => k.health_band === 'CRITICAL'
                                || k.health_band === 'TIGHT');
  const underused = open.filter((k) => k.health_band === 'UNDERUSED');
  const peaks = open.map((k) => Number(k.peak_utilization_pct))
    .filter((v) => Number.isFinite(v));
  const meanPeak = peaks.length
    ? peaks.reduce((a, b) => a + b, 0) / peaks.length : null;

  const noun = view.domain === 'plant' ? 'Production sites'
    : view.domain === 'dc' ? 'Distribution facilities' : 'Facilities';
  const gapColour = tight.length > 0 ? 'var(--amber)' : 'var(--green)';
  const closed = rows.length - open.length;

  grid.innerHTML = `
    <div class="dash-metric-card">
      <div class="dash-metric-title">${noun}</div>
      <div class="dash-metric-val">${open.length} <span style="font-size:15px;color:var(--text-3);font-weight:600">of ${rows.length}</span></div>
      <div class="dash-metric-sub">
        <span>open in this plan${closed > 0
          ? ` · ${closed} not in it` : ''}</span>
      </div>
    </div>

    <div class="dash-metric-card">
      <div class="dash-metric-title">At or above ${OVER_PCT}%</div>
      <div class="dash-metric-val" style="color:${gapColour}">${tight.length}</div>
      <div class="dash-metric-sub">
        <span>${multiPeriod()
          ? 'in at least one modelled period'
          : 'in the period modelled'}</span>
      </div>
    </div>

    <div class="dash-metric-card">
      <div class="dash-metric-title">Under-used</div>
      <div class="dash-metric-val" style="color:${underused.length > 0 ? 'var(--blue)' : 'var(--text-1)'}">${underused.length}</div>
      <div class="dash-metric-sub">
        <span>open, carrying full fixed cost</span>
      </div>
    </div>

    <div class="dash-metric-card">
      <div class="dash-metric-title">${multiPeriod() ? 'Average peak utilisation' : 'Average utilisation'}</div>
      <div class="dash-metric-val">${meanPeak === null ? absentLabel('Not reported') : `${fmtNum(meanPeak, 1)}%`}</div>
      <div class="dash-metric-sub">
        <!-- Named precisely. This is the mean of each site's own worst
             period, which is not the network's peak and must not be read as
             one. -->
        <span>${multiPeriod() ? 'mean of each open site’s worst period' : 'across open sites'}</span>
      </div>
    </div>`;
}

function renderAttention() {
  const node = el('wh-attention');
  if (!node) return;
  const rows = visibleRows()
    .filter((k) => k.is_open && (k.health_band === 'CRITICAL' || k.health_band === 'TIGHT'
                                || k.health_band === 'UNDERUSED'))
    .sort((a, b) => BAND_ORDER.indexOf(a.health_band) - BAND_ORDER.indexOf(b.health_band)
                    || b.peak_utilization_pct - a.peak_utilization_pct);

  if (rows.length === 0) {
    // Two different empty states. "Nothing needs attention" is a finding and
    // the reader should see that it was checked; "your filters match nothing"
    // is not a finding at all, and printing the first when the second is true
    // would report a clean bill of health for sites nobody is looking at.
    node.innerHTML = visibleRows().length === 0
      ? `<div class="wh-status-note"><strong>No facility matches the current
          filters.</strong> Clear one of them to see the sites this plan
          carries.</div>`
      : `<div class="wh-status-note">
          <strong>No site here reaches the ${OVER_PCT}% threshold${multiPeriod()
            ? ' in any modelled period' : ''}, and none is under-used.</strong>
          Capacity is not what limits this part of the plan.
        </div>`;
    return;
  }

  // TWO, and the rest behind a count.
  //
  // Five exception cards stacked above the charts pushed every visual below
  // the fold, so the screen led with a list rather than with the picture. Two
  // is enough to say what is worst; a reader who wants the others is one click
  // from them, and the table below carries all of them anyway.
  const SHOWN = 2;
  const card = (k) => {
    const band = String(k.health_band).toLowerCase();
    const name = esc(k.facility_name || k.facility_id);
    let why;
    if (k.health_band === 'UNDERUSED') {
      why = `Runs at ${fmtNum(k.peak_utilization_pct, 1)}% even in its busiest `
          + `period while carrying its full fixed cost of `
          + `${formatCurrency(k.total_facility_cost)}. Consolidation is worth `
          + `testing as a scenario — no scenario has been run, so no saving is stated.`;
    } else if (multiPeriod() && k.avg_utilization_pct < OVER_PCT) {
      // The finding this screen exists for, stated in full.
      why = `Averages ${fmtNum(k.avg_utilization_pct, 1)}% across the horizon and `
          + `reaches ${fmtNum(k.peak_utilization_pct, 1)}%`
          + (k.peak_period ? ` in period ${esc(k.peak_period)}` : '')
          + `. The average is below the ${OVER_PCT}% threshold and the peak is not, `
          + `so it has no room in the period that decides whether it works.`;
    } else {
      why = `At ${fmtNum(k.peak_utilization_pct, 1)}% of stated capacity`
          + (multiPeriod()
              ? ` in ${k.bottleneck_periods_count} of ${k.periods_observed} periods`
              : '')
          + `. There is no headroom here for a surge or for absorbing volume `
          + `from elsewhere.`;
    }
    return `<div class="wh-attention-row band-${band}">
      <div style="min-width:0">
        <div class="wh-attention-name">${name} ${bandTag(k.health_band)}</div>
        <div class="wh-attention-why">${why}</div>
      </div>
      <div class="wh-attention-fig" style="color:${
        k.health_band === 'CRITICAL' ? 'var(--red)'
        : k.health_band === 'TIGHT' ? 'var(--amber)' : 'var(--blue)'}">
        ${fmtNum(k.peak_utilization_pct, 1)}%
        <small>${multiPeriod() ? 'peak period' : 'utilisation'}</small>
      </div>
    </div>`;
  };

  const more = rows.length - SHOWN;
  node.innerHTML = rows.slice(0, SHOWN).map(card).join('')
    + (more > 0
        ? `<div id="wh-attention-rest" hidden>${rows.slice(SHOWN).map(card).join('')}</div>`
          + `<div class="wh-attention-more">`
          + `<button type="button" class="wh-attention-more-btn" `
          + `id="wh-attention-more-btn">View ${more} more</button></div>`
        : '');

  el('wh-attention-more-btn')?.addEventListener('click', (e) => {
    const rest = el('wh-attention-rest');
    if (!rest) return;
    rest.hidden = !rest.hidden;
    e.target.textContent = rest.hidden ? `View ${more} more` : 'Show fewer';
  });
}

/**
 * What this lens calls its population, in the words on the cards.
 *
 * "Network Status Mix" over a chart of two plants describes a population the
 * chart is not about — the same defect the scorecard had. One noun, chosen
 * once, used by every caption that needs it.
 */
function lensNoun(plural = true) {
  if (view.domain === 'plant') return plural ? 'plants' : 'plant';
  if (view.domain === 'dc') return plural ? 'distribution centres' : 'distribution centre';
  return plural ? 'facilities' : 'facility';
}

/** Titles and subtitles that name the population currently on screen. */
function renderLensLabels() {
  const mix = el('wh-mix-title');
  if (mix) {
    mix.textContent = view.domain === 'plant' ? 'Plant Status Mix'
      : view.domain === 'dc' ? 'Distribution Centre Status Mix'
      : 'Network Status Mix';
  }
  const util = el('wh-util-subtitle');
  if (util) {
    util.textContent = `Every open ${lensNoun(false)}, against the ${OVER_PCT}% threshold`;
  }
}

function renderCharts() {
  const r = state.report;
  if (!r) return;

  const shown = visibleRows();
  const open = shown
    .filter((k) => k.is_open)
    .sort((a, b) => b.peak_utilization_pct - a.peak_utilization_pct)
    .slice(0, 12);
  drawn.peak_vs_average = open.map((k) => k.facility_id);
  renderWarehouseUtilisationChart('chart-wh-utilisation', open, OVER_PCT, multiPeriod());

  // EVERY VISIBLE SITE, not the backend's ranked top ten.
  //
  // `top_facilities_driving_cost` is capped at TOP_N, so on a network of more
  // than ten facilities the donut drew ten slices while any total beside it
  // summed all of them — a chart whose parts could not add up to its own
  // whole. Built from the rows on screen instead, each of which carries its
  // own `total_facility_cost`, so the slices ARE the population. The chart
  // folds the small tail into "Other" rather than dropping it.
  const drivers = shown
    .map((k) => ({
      facility_id: k.facility_id,
      facility_name: k.facility_name || k.facility_id,
      total_facility_cost: Number(k.total_facility_cost) || 0,
    }))
    .filter((d) => d.total_facility_cost > 0)
    .sort((a, b) => b.total_facility_cost - a.total_facility_cost);
  renderWarehouseSpendChart('chart-wh-spend', drivers);

  renderMix();
  renderHeadroom();
  renderStock();
}

/**
 * The mix of states, counted.
 *
 * Every site in the report, including the ones this plan does not open — a
 * network where four of eleven sites are not running is a finding, and a mix
 * that quietly dropped them would draw a smaller, healthier network than the
 * one the client has.
 */
function renderMix() {
  const rows = visibleRows();
  renderWarehouseStatusMixChart('chart-wh-mix', BAND_ORDER.map((band) => ({
    label: BAND_LABEL[band] || band,
    value: rows.filter((k) => k.health_band === band).length,
    color: BAND_COLOUR[band],
  })));
}

/**
 * Capacity and what the busiest period puts in it, in units.
 *
 * Open sites only, ranked by capacity. A site this plan does not open has no
 * headroom the plan can use: drawing its whole capacity as spare room would
 * point a planner at a building nobody is running.
 */
function renderHeadroom() {
  const open = visibleRows()
    .filter((k) => k.is_open && Number(k.rated_capacity_per_period) > 0)
    .sort((a, b) => (b.rated_capacity_per_period || 0) - (a.rated_capacity_per_period || 0))
    .slice(0, 12);
  drawn.capacity_carried = open.map((k) => k.facility_id);
  renderWarehouseHeadroomChart('chart-wh-headroom', open, multiPeriod());
}

/**
 * Average against peak stock — or the reason there is none.
 *
 * A model that writes no inventory decisions has no stock to draw, and that is
 * not the same statement as "these sites hold nothing". The canvas is REMOVED
 * in that case and the engine's own reason takes its place; an empty chart
 * frame with axes on it reads as a measurement of zero.
 */
function renderStock() {
  const rows = visibleRows();
  // BOTH readings, or the chart draws a zero bar for the half a site never
  // reported and the gap between the two bars — the whole finding — becomes
  // an artefact of a missing figure.
  const held = rows
    .filter((k) => k.avg_inventory_units !== null && k.avg_inventory_units !== undefined
                && k.peak_inventory_units !== null && k.peak_inventory_units !== undefined)
    .sort((a, b) => (b.peak_inventory_units || 0) - (a.peak_inventory_units || 0))
    .slice(0, 12);

  drawn.stock_held = held.map((k) => k.facility_id);

  const wrap = el('wh-stock-wrap');
  const note = el('wh-stock-absent');

  // A REPORTED ZERO IS NOT A MISSING READING, and the backend keeps them
  // apart: a site with no inventory decisions comes back with
  // INSUFFICIENT_EVIDENCE and a reason saying so, while 0.0 is a measured
  // level. The caption used to say "12 of 12 sites hold stock" when what it
  // had counted was sites that REPORTED — including any holding nothing —
  // so a network carrying no stock at all read as one where every site did.
  const holding = held.filter((k) => Number(k.peak_inventory_units) > 0).length;
  const subtitle = el('wh-stock-subtitle');
  if (subtitle) {
    subtitle.textContent = held.length === 0
      ? `No stock level reported for ${rows.length === 1 ? 'this site' : 'these sites'}`
      : `${holding} of ${held.length} site${held.length === 1 ? '' : 's'} hold stock`
        + (held.length < rows.length
            ? ` · ${rows.length - held.length} report none`
            : ' · every site reports a level');
  }

  if (held.length) {
    if (wrap) wrap.style.display = '';
    if (note) { note.style.display = 'none'; note.innerHTML = ''; }
    renderWarehouseStockChart('chart-wh-stock', held);
    return;
  }

  if (wrap) wrap.style.display = 'none';
  if (!note) return;
  note.style.display = '';

  // The engine's reason is written about ONE site. Printed alone on a card
  // about every site it reads as a statement about some unnamed one, so the
  // network-level fact is stated first and the engine's words are attributed
  // rather than paraphrased — and counted, so "one reason for all of them" is
  // something checked rather than assumed.
  const reasons = [...new Set(rows
    .map((k) => k.inventory_status?.reason)
    .filter(Boolean))];
  note.innerHTML = `<div class="wh-status-note">
    <strong>No site in this plan reports a stock level.</strong>
    ${reasons.length === 1
      ? `The engine gives one reason for all of them: ${esc(reasons[0])}`
      : reasons.length
        ? `The engine's reasons: ${reasons.map(esc).join(' ')}`
        : 'This solve wrote no inventory decisions at all, so there is no '
          + 'stock level to draw. That is a model which does not carry stock, '
          + 'not a network holding none.'}</div>`;
}

function renderHealthTable() {
  const body = document.querySelector('#table-wh-health tbody');
  if (!body) return;
  const rows = [...visibleRows()].sort((a, b) =>
    BAND_ORDER.indexOf(a.health_band) - BAND_ORDER.indexOf(b.health_band)
    || b.peak_utilization_pct - a.peak_utilization_pct);

  if (rows.length === 0) {
    body.innerHTML = `<tr><td colspan="11">${(state.report?.health_kpis || []).length
      ? 'No facility matches the current filters.'
      : 'No facility has been solved for this network.'}</td></tr>`;
    return;
  }

  // Every row opens that site's detail — the screen's drill-down. The id
  // travels on the row rather than in a closure so one delegated listener in
  // `kpi-view.js` covers a table that is rebuilt on every filter change.
  body.innerHTML = rows.map((k) => {
    const stockReason = k.inventory_status?.reason || '';
    const util = (v, band) => `<span style="font-weight:700;color:${
      band === 'CRITICAL' ? 'var(--red)' : band === 'TIGHT' ? 'var(--amber)'
      : band === 'UNDERUSED' ? 'var(--blue)' : 'var(--text-1)'}">${fmtNum(v, 1)}%</span>`;
    return `<tr class="wh-health-row" data-facility-id="${esc(k.facility_id)}"
                title="Open this facility's detail">
      <td>
        <div style="font-weight:600">${esc(k.facility_name || k.facility_id)}</div>
        <div class="text-xs" style="color:var(--text-3)">${esc(roleLabel(k.role))}${
          k.region ? ` · ${esc(k.region)}` : ''}</div>
      </td>
      <td>${bandTag(k.health_band)}</td>
      <td class="num">${formatNumber(k.rated_capacity_per_period)}</td>
      <td class="num">${formatNumber(k.avg_throughput_units)}</td>
      <td class="num">${formatNumber(k.peak_throughput_units)}${
        k.peak_period ? `<div class="text-xs" style="color:var(--text-3)">period ${esc(k.peak_period)}</div>` : ''}</td>
      <td class="num">${util(k.avg_utilization_pct, null)}</td>
      <td class="num">${util(k.peak_utilization_pct, k.health_band)}</td>
      <td class="num">${k.is_open
        ? `${k.bottleneck_periods_count} / ${k.periods_observed}`
        : absent('This site is not open in this plan, so it has no periods to be tight in.')}</td>
      <td class="num">${k.avg_inventory_units === null || k.avg_inventory_units === undefined
        ? absent(stockReason) : formatNumber(k.avg_inventory_units)}</td>
      <td class="num">${k.peak_inventory_units === null || k.peak_inventory_units === undefined
        ? absent(stockReason) : formatNumber(k.peak_inventory_units)}</td>
      <td class="num">${formatCurrency(k.total_facility_cost)}</td>
    </tr>`;
  }).join('');
}

// ─── The screen ─────────────────────────────────────────────

function renderAll() {
  renderBasis();
  renderLensLabels();
  renderSummary();
  renderAttention();
  renderCharts();
  renderHealthTable();
}

function renderUnavailable(message) {
  const grid = el('wh-summary-grid');
  if (grid) grid.innerHTML = '';
  const node = el('wh-attention');
  if (node) {
    node.innerHTML = `<div class="wh-status-note"><strong>The facility
      analysis is not available.</strong> ${esc(message)}</div>`;
  }
}

/** Called by `navigateToTab`. Fetches once per project and re-renders after. */
export async function renderWarehouseDashboard() {
  const projectId = getActiveProjectId();
  // Cached: the analysis behind this is computed once per network version
  // server-side, and re-requesting it on every tab visit would spend a round
  // trip to be told the same thing.
  if (state.report && state.projectId === projectId) {
    renderAll();
    window.dispatchEvent(new CustomEvent('warehouse-report-ready'));
    return;
  }

  const node = el('wh-attention');
  if (node) {
    node.innerHTML = `<div class="wh-status-note">Reading the solved footprint…</div>`;
  }
  await loadWarehouseReport();
  if (!state.report) { renderUnavailable(state.error || 'No solved network state.'); return; }
  renderAll();
  // The filter controls are built from this report — which roles, which
  // regions, which states actually occur — so they cannot be populated until
  // it lands. `kpi-view.js` listens rather than polls.
  window.dispatchEvent(new CustomEvent('warehouse-report-ready'));
}

/**
 * How much inventory cost is actually pinned to a facility.
 *
 * The network's cost has four parts and the solve knows three of them per
 * site — fixed, handling and opening. Inventory it decides for the network as
 * a whole, so summing the sites leaves that part out, and a reader who adds
 * up the Facility Cost column and expects the headline figure comes up short.
 * The screen states the gap rather than hiding it or, worse, spreading it
 * across sites on an allocation nobody solved for.
 */
export function warehouseAttributedHolding() {
  return (state.report?.health_kpis || [])
    .reduce((sum, k) => sum + (Number(k.holding_cost) || 0), 0);
}

/** The current view in words, for the "Showing" line and the PDF header. */
export function warehouseViewLabel() {
  const parts = [view.domain === 'plant' ? 'Plants'
    : view.domain === 'network' ? 'Every facility' : 'Distribution centres'];
  if (view.region !== 'all') parts.push(`region ${view.region}`);
  if (view.status !== 'all') parts.push(BAND_LABEL[view.status] || view.status);
  return parts.join(' \u00b7 ');
}

/** Drop everything cached — called when the open project changes. */
export function clearWarehouseState() {
  state.report = null;
  state.projectId = null;
  state.error = null;
  // Back to the whole network. A region filter carried into a project that has
  // no such region would open the new network on an empty screen.
  view.domain = 'dc';
  view.region = 'all';
  view.status = 'all';
}
