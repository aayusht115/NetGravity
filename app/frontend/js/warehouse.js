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
 * This file writes no file of its own. `warehouseHealthCsvLines()` hands its
 * rows to `exportFacilityReport()` in `app.js`, so the screen's one button
 * produces one report covering both halves. Two buttons made the reader work
 * out which half each file covered before they could trust either.
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
         renderWarehouseStockChart } from './charts.js';

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

/** The same colours the band tags carry in `style.css`, so one state reads as
 *  one colour in the tag, in the attention row, in the table and in the mix
 *  chart. Two colour codes for one vocabulary is worse than no chart. */
const BAND_COLOUR = {
  CRITICAL: '#dc2626',
  TIGHT: '#d97706',
  UNDERUSED: '#2563eb',
  HEALTHY: '#16a34a',
  NOT_OPERATING: '#c9c9d4',
};

function bandTag(band) {
  const key = String(band || 'HEALTHY');
  return `<span class="tag tag-band-${key.toLowerCase()}">${esc(BAND_LABEL[key] || key)}</span>`;
}

/** A figure the engine did not produce, shown as absent with its reason on
 *  hover — never as 0, which would state a measurement nobody made. */
function absent(reason) {
  return `<span class="wh-absent" title="${esc(reason || 'Not reported by this solve')}">&mdash;</span>`;
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

function renderSummary() {
  const grid = el('wh-summary-grid');
  if (!grid) return;
  const r = state.report;
  if (!r) { grid.innerHTML = ''; return; }

  const gapColour = r.n_bottlenecks > 0 ? 'var(--amber)' : 'var(--green)';
  const peak = r.avg_peak_utilization_pct;

  grid.innerHTML = `
    <div class="dash-metric-card">
      <div class="dash-metric-title">Distribution facilities</div>
      <div class="dash-metric-val">${r.n_warehouses_open} <span style="font-size:15px;color:var(--text-3);font-weight:600">of ${r.n_warehouses}</span></div>
      <div class="dash-metric-sub">
        <!-- Storage sites against storage sites, not against every open
             facility: plants and suppliers are open too, and counting them
             here would put a larger number under a smaller population. The
             rest are named as sites rather than left unexplained. -->
        <span>open in this plan${r.n_open > r.n_warehouses_open
          ? ` · ${r.n_open - r.n_warehouses_open} other facilit${
              r.n_open - r.n_warehouses_open === 1 ? 'y' : 'ies'} open` : ''}</span>
      </div>
    </div>

    <div class="dash-metric-card">
      <div class="dash-metric-title">At or above ${OVER_PCT}%</div>
      <div class="dash-metric-val" style="color:${gapColour}">${r.n_bottlenecks}</div>
      <div class="dash-metric-sub">
        <span>${multiPeriod()
          ? 'in at least one modelled period'
          : 'in the period modelled'}</span>
      </div>
    </div>

    <div class="dash-metric-card">
      <div class="dash-metric-title">Under-used</div>
      <div class="dash-metric-val" style="color:${r.n_underused > 0 ? 'var(--blue)' : 'var(--text-1)'}">${r.n_underused}</div>
      <div class="dash-metric-sub">
        <span>open, carrying full fixed cost</span>
      </div>
    </div>

    <div class="dash-metric-card">
      <div class="dash-metric-title">${multiPeriod() ? 'Average peak utilisation' : 'Average utilisation'}</div>
      <div class="dash-metric-val">${peak === null || peak === undefined ? '—' : `${fmtNum(peak, 1)}%`}</div>
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
  const rows = (state.report?.health_kpis || [])
    .filter((k) => k.is_open && (k.health_band === 'CRITICAL' || k.health_band === 'TIGHT'
                                || k.health_band === 'UNDERUSED'))
    .sort((a, b) => BAND_ORDER.indexOf(a.health_band) - BAND_ORDER.indexOf(b.health_band)
                    || b.peak_utilization_pct - a.peak_utilization_pct)
    .slice(0, 5);

  if (rows.length === 0) {
    // A statement, not an empty div. "Nothing needs attention" is a finding
    // and the reader should be able to see that it was checked.
    node.innerHTML = `<div class="wh-status-note">
      <strong>No site reaches the ${OVER_PCT}% threshold${multiPeriod()
        ? ' in any modelled period' : ''}, and none is under-used.</strong>
      Capacity is not what limits this plan.
    </div>`;
    return;
  }

  node.innerHTML = rows.map((k) => {
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
  }).join('');
}

function renderCharts() {
  const r = state.report;
  if (!r) return;

  const open = (r.health_kpis || [])
    .filter((k) => k.is_open)
    .sort((a, b) => b.peak_utilization_pct - a.peak_utilization_pct)
    .slice(0, 12);
  renderWarehouseUtilisationChart('chart-wh-utilisation', open, OVER_PCT, multiPeriod());

  const tag = el('wh-util-tag');
  if (tag) {
    tag.textContent = r.n_bottlenecks > 0
      ? `${r.n_bottlenecks} at or above ${OVER_PCT}%`
      : `none above ${OVER_PCT}%`;
    tag.className = `tag ${r.n_bottlenecks > 0 ? 'tag-band-tight' : 'tag-band-healthy'}`;
  }

  renderWarehouseSpendChart('chart-wh-spend', r.top_facilities_driving_cost || []);
  const spendTag = el('wh-spend-tag');
  if (spendTag) spendTag.textContent = `${formatCurrency(r.total_facility_spend)} total`;

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
  const rows = state.report?.health_kpis || [];
  renderWarehouseStatusMixChart('chart-wh-mix', BAND_ORDER.map((band) => ({
    label: BAND_LABEL[band] || band,
    value: rows.filter((k) => k.health_band === band).length,
    color: BAND_COLOUR[band],
  })));
  const tag = el('wh-mix-tag');
  if (tag) tag.textContent = `${rows.length} facilit${rows.length === 1 ? 'y' : 'ies'}`;
}

/**
 * Capacity and what the busiest period puts in it, in units.
 *
 * Open sites only, ranked by capacity. A site this plan does not open has no
 * headroom the plan can use: drawing its whole capacity as spare room would
 * point a planner at a building nobody is running.
 */
function renderHeadroom() {
  const open = (state.report?.health_kpis || [])
    .filter((k) => k.is_open && Number(k.rated_capacity_per_period) > 0)
    .sort((a, b) => (b.rated_capacity_per_period || 0) - (a.rated_capacity_per_period || 0))
    .slice(0, 12);
  renderWarehouseHeadroomChart('chart-wh-headroom', open, multiPeriod());
  const tag = el('wh-headroom-tag');
  if (tag) {
    tag.textContent = open.length
      ? `${open.length} open site${open.length === 1 ? '' : 's'}`
      : 'no open site states a capacity';
  }
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
  const rows = state.report?.health_kpis || [];
  // BOTH readings, or the chart draws a zero bar for the half a site never
  // reported and the gap between the two bars — the whole finding — becomes
  // an artefact of a missing figure.
  const held = rows
    .filter((k) => k.avg_inventory_units !== null && k.avg_inventory_units !== undefined
                && k.peak_inventory_units !== null && k.peak_inventory_units !== undefined)
    .sort((a, b) => (b.peak_inventory_units || 0) - (a.peak_inventory_units || 0))
    .slice(0, 12);

  const wrap = el('wh-stock-wrap');
  const note = el('wh-stock-absent');
  const tag = el('wh-stock-tag');

  if (held.length) {
    if (wrap) wrap.style.display = '';
    if (note) { note.style.display = 'none'; note.innerHTML = ''; }
    if (tag) {
      tag.textContent = `${held.length} of ${rows.length} site${
        rows.length === 1 ? '' : 's'} hold stock`;
    }
    renderWarehouseStockChart('chart-wh-stock', held);
    return;
  }

  if (wrap) wrap.style.display = 'none';
  if (tag) tag.textContent = 'not reported';
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
  const rows = [...(state.report?.health_kpis || [])].sort((a, b) =>
    BAND_ORDER.indexOf(a.health_band) - BAND_ORDER.indexOf(b.health_band)
    || b.peak_utilization_pct - a.peak_utilization_pct);

  const count = el('wh-health-count');
  if (count) count.textContent = `${rows.length} facilit${rows.length === 1 ? 'y' : 'ies'}`;

  if (rows.length === 0) {
    body.innerHTML = '<tr><td colspan="11">No facility has been solved for this network.</td></tr>';
    return;
  }

  body.innerHTML = rows.map((k) => {
    const stockReason = k.inventory_status?.reason || '';
    const util = (v, band) => `<span style="font-weight:700;color:${
      band === 'CRITICAL' ? 'var(--red)' : band === 'TIGHT' ? 'var(--amber)'
      : band === 'UNDERUSED' ? 'var(--blue)' : 'var(--text-1)'}">${fmtNum(v, 1)}%</span>`;
    return `<tr>
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
  if (state.report && state.projectId === projectId) { renderAll(); return; }

  const node = el('wh-attention');
  if (node) {
    node.innerHTML = `<div class="wh-status-note">Reading the solved footprint…</div>`;
  }
  await loadWarehouseReport();
  if (!state.report) { renderUnavailable(state.error || 'No solved network state.'); return; }
  renderAll();
}

/**
 * Every site in the network, as lines for the screen's one export.
 *
 * The rows the health table shows, in the order it shows them. This file no
 * longer writes a file of its own — `exportFacilityReport()` in `app.js` owns
 * the screen's single button and appends this section to it, so one press
 * produces one report covering both halves of the screen.
 *
 * An empty analysis returns a line SAYING it is empty rather than nothing at
 * all: a report whose network section is silently missing reads as a network
 * with no sites in it.
 */
export function warehouseHealthCsvLines() {
  const rows = [...(state.report?.health_kpis || [])].sort((a, b) =>
    BAND_ORDER.indexOf(a.health_band) - BAND_ORDER.indexOf(b.health_band)
    || b.peak_utilization_pct - a.peak_utilization_pct);

  const header = ['Facility ID', 'Facility', 'Role', 'Region', 'Status', 'Open',
    `Capacity ${perPeriodLabel()}`, 'Avg throughput', 'Peak throughput',
    'Peak period', 'Avg utilisation %', 'Peak utilisation %',
    'Tight periods', 'Periods observed', 'Avg stock', 'Peak stock',
    'Fixed cost', 'Handling cost', 'Holding cost', 'Opening cost', 'Facility cost'];

  // An absent stock reading exports as empty, not as 0 — the distinction the
  // screen makes has to survive the file, or a spreadsheet averages a zero
  // nobody measured.
  const cell = (v) => (v === null || v === undefined ? ''
    : (typeof v === 'string' && /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v));

  const body = rows.map((k) => [
    k.facility_id, k.facility_name, roleLabel(k.role), k.region, BAND_LABEL[k.health_band] || k.health_band,
    k.is_open ? 'yes' : 'no', k.rated_capacity_per_period, k.avg_throughput_units,
    k.peak_throughput_units, k.peak_period, k.avg_utilization_pct, k.peak_utilization_pct,
    k.is_open ? k.bottleneck_periods_count : null, k.is_open ? k.periods_observed : null,
    k.avg_inventory_units, k.peak_inventory_units,
    k.fixed_cost, k.handling_cost, k.holding_cost, k.opening_cost, k.total_facility_cost,
  ].map(cell).join(','));

  if (!rows.length) {
    return ['=== Facility Network ===',
            cell('No facility network analysis has been read for this project.')];
  }
  return [`=== Facility Network \u2014 every site (${
    state.report?.network_id || 'network'}) ===`, header.join(','), ...body];
}

/** Drop everything cached — called when the open project changes. */
export function clearWarehouseState() {
  state.report = null;
  state.projectId = null;
  state.error = null;
}
