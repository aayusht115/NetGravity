/**
 * NetGravity — KPI Screen View Controller
 * =======================================
 * WHICH population the KPI screen is reporting on, and how far into it the
 * reader has gone.
 *
 * THE PROBLEM THIS SOLVES
 * -----------------------
 * The KPI screen used to be one continuous scroll: the whole facility network,
 * then every network chart, then every site in a table, and only then the one
 * site the top bar had selected. Selecting Atlanta at the top left the reader
 * scrolling past four network charts and a nine-row table to reach Atlanta's
 * own numbers, and nothing on the way down said which of those figures were
 * about Atlanta and which were about the network.
 *
 * THREE TIERS, ONE STATE
 * ----------------------
 *   Tier 0  The scorecard. The whole network, always, captioned as that. It
 *           is the same authoritative figure set the Overview states, so a
 *           reader arriving from that screen lands on numbers that agree with
 *           the ones they just left.
 *   Tier 1  The lens — storage sites, production sites, or the corridors
 *           between them. Switching lens returns to that lens's whole
 *           population; a selected site is never carried across, because the
 *           same name on another lens is a different site or no site at all.
 *   Tier 2  Region and Status narrow the population; Facility drills into one
 *           site and swaps the roll-up for that site's own detail IN PLACE.
 *
 * ONE VIEW STATE, ONE RENDER
 * --------------------------
 * Every control writes to `view` below and then calls `applyView()`. Nothing
 * renders itself from its own reading of the controls. That is what stops the
 * half-filtered screen — a donut still drawn over nine sites beside a table
 * showing three — which is the failure that makes a reader stop trusting every
 * other number on the page.
 *
 * IT COMPUTES NO KPI
 * ------------------
 * Every facility figure here is the backend's own record, read through
 * `warehouse.js`. This file decides only WHICH records are on screen and says
 * so in words. The counts it does make are counts OF those records, printed
 * under a label naming what was counted.
 */

import { LANES, getFacilityById, formatNumber, formatCurrencyExact,
         formatCurrency, perPeriodLabel } from './data.js';
import { setWarehouseFilter, warehouseFacets, warehouseRow,
         warehouseViewLabel, warehouseAttributedHolding } from './warehouse.js';
import { initKpiExplain, closeKpiExplainPanels,
         startKpiExplainShimmer } from './kpi-explain.js';
import { kpiService } from './integration/services/kpi-service.js';

/** The whole screen's state. Read by `applyView()` and by the export. */
const view = {
  /** 'network' | 'dc' | 'plant' | 'lane' */
  domain: 'network',
  /** A facility id, or null for the roll-up. Only ever set on a facility lens. */
  entityId: null,
  /** Lane lens only — the corridors are not facilities and carry no band.
   *
   *  There is no "one corridor" selection and deliberately so: origin and
   *  destination together already narrow to one, and they degrade gracefully
   *  on the way. A single-lane selection would leave "highest-cost corridors"
   *  ranking one row, a mode split at 100% of one mode and a table of one —
   *  three panels saying less than the table row the reader came from, with
   *  no detail view behind it to make the trip worth taking.
   *
   *  There is no period filter either: a flow row carries `flow_units` and
   *  `flow_units_per_period` and NO per-period breakdown, so the control
   *  would show the same figures whichever period was chosen. */
  mode: 'all',
  origin: 'all',
  destination: 'all',
  /** FIND within the corridor table, not a filter on the lens. The selects
   *  above scope every card; this narrows only the table's rows, so a reader
   *  looking for one corridor does not change what the cards are reporting. */
  laneSearch: '',
};

/** Set by `initKpiView()`. Kept as hooks rather than imports so this module
 *  and `app.js` do not import each other. */
let hooks = { selectEntity: null, renderEntity: null,
              renderNetworkScorecard: null, networkInventoryCost: null,
              populatePeriods: null, selectPeriod: null };

let wired = false;


const DOMAIN_LABEL = { network: 'Network', dc: 'Distribution centres',
                       plant: 'Plants', lane: 'Corridors' };

function el(id) { return document.getElementById(id); }

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** A reading the solve did not produce, IN WORDS — never as 0, which would
 *  state a measurement nobody made, and never as a bare dash, which a reader
 *  has no way to tell apart from zero. The corridor table has six columns and
 *  the room to say it. */
const ABSENT = '<span class="wh-absent wh-absent-label">Not reported</span>';

function num(value, suffix = '') {
  return (value === null || value === undefined || Number.isNaN(Number(value)))
    ? ABSENT : `${formatNumber(value)}${suffix}`;
}

// ─── The corridor lens ──────────────────────────────────────

/** Every corridor the lane filters leave on screen. */
function visibleLanes() {
  return LANES.filter((l) => (view.mode === 'all' || String(l.mode || '') === view.mode)
    && (view.origin === 'all' || String(l.from || '') === view.origin)
    && (view.destination === 'all' || String(l.to || '') === view.destination));
}

/** The name a facility id is known by, or the id when the network has no
 *  facility for it — a market on the far end of a corridor, typically. */
function endpointName(id) {
  const fac = getFacilityById(id);
  return fac ? fac.name : String(id || '');
}

/**
 * What origin and destination can offer, given each other.
 *
 * Dependent, on the same rule as Region and Status: a control must not offer
 * a value that leads to an empty screen. Picking an origin narrows the
 * destinations to the ones that origin actually serves, so the pair can only
 * ever describe corridors this network really has.
 */
function laneFacets() {
  const byMode = LANES.filter((l) => view.mode === 'all'
    || String(l.mode || '') === view.mode);
  const uniq = (rows, key) => [...new Map(rows
    .map((l) => [String(l[key] || ''), endpointName(l[key])])
    .filter(([id]) => id)).entries()]
    .map(([id, name]) => ({ id, name }))
    .sort((a, b) => a.name.localeCompare(b.name));
  return {
    origins: uniq(byMode.filter((l) => view.destination === 'all'
      || String(l.to || '') === view.destination), 'from'),
    destinations: uniq(byMode.filter((l) => view.origin === 'all'
      || String(l.from || '') === view.origin), 'to'),
    modes: [...new Set(LANES.map((l) => String(l.mode || '')).filter(Boolean))].sort(),
  };
}

/**
 * A number, or absence — and `null` is ABSENCE.
 *
 * `Number(null)` is 0, not NaN, so `Number.isFinite(Number(lane.flow))` was
 * TRUE for a corridor the solve never routed anything down. Every missing
 * volume became a zero volume: the spend card counted those corridors as
 * "calculated" and multiplied a real rate by a flow nobody reported to get
 * ₹0, and the volume card averaged them in. A corridor with no reported flow
 * does not cost nothing — it is not known to cost anything.
 */
function figure(value) {
  if (value === null || value === undefined || value === '') return null;
  const out = Number(value);
  return Number.isFinite(out) ? out : null;
}

/**
 * What the solve says this corridor costs, over the horizon.
 *
 * THE SOLVER'S OWN FIGURE, not one multiplied together here. This returned
 * `rate × lane.flow`, and `lane.flow` is a PER-PERIOD volume — so the card
 * above it reported a per-period amount on a screen whose every other cost
 * is a horizon total. On a twelve-period network that read $248.6K beside a
 * $293.71M network cost and $290.70M of facility spend: a gap of about $3M
 * that no line on the page accounted for, because the missing part was the
 * same corridor spend times twelve.
 *
 * `transport_cost` is what the engine charged this corridor across the
 * horizon, carried on every flow row and unused until now. Reading it makes
 * facility spend plus corridor spend reconcile with the network cost above
 * them, and takes the last piece of business arithmetic out of the browser —
 * §9: the frontend calculates no authoritative KPI.
 *
 * Absent, not zero, where the solve routed nothing down this corridor: a
 * corridor with no solved flow is not one that costs nothing.
 */
function laneSpend(lane) {
  return figure(lane.transportCost);
}

/**
 * A transport mode as a reader reads it.
 *
 * The upload states them in capitals — ROAD, RAIL, INTERMODAL — and this
 * screen showed all three renderings at once: "mostly by road" in the volume
 * tile, "ROAD" in the mode split beside it, and "ROAD" again in the filter
 * and the table's tag. One value, three voices, on one lens.
 *
 * DISPLAY ONLY. The raw string stays on the lane and in every comparison the
 * filters make, so nothing here can stop a mode matching itself.
 */
function modeLabel(mode) {
  const raw = String(mode || '').trim();
  if (!raw) return '';
  return raw.replace(/[^\s-]+/g, (word) => (
    word.length <= 2 ? word.toUpperCase()
      : word.charAt(0).toUpperCase() + word.slice(1).toLowerCase()));
}

function laneName(lane) {
  const from = getFacilityById(lane.from);
  const to = getFacilityById(lane.to);
  return `${from ? from.name : lane.from} → ${to ? to.name : lane.to}`;
}

/**
 * The corridor lens's own scorecard.
 *
 * This lens had none: the facility grid was left on screen carrying whatever
 * the previous lens had rendered, so switching from Plants to Freight showed
 * "Production sites 2 of 2" above a corridor table with the caption blanked.
 * Facility counts describing a population that is not on the screen is the
 * exact defect the rest of this screen was rebuilt to remove.
 *
 * Three figures, each counted over the corridors actually shown, and each
 * stated as absent rather than as zero where the solve produced nothing.
 */
function renderLaneSummary() {
  const grid = el('kpi-lane-summary');
  if (!grid) return;

  const lanes = visibleLanes();
  const spends = lanes.map(laneSpend).filter((v) => v !== null);
  const modes = [...new Set(lanes.map((l) => String(l.mode || '')).filter(Boolean))];
  const flows = lanes.map((l) => figure(l.flow)).filter((v) => v !== null);

  // The mode carrying the most corridors, named rather than counted: "Road"
  // says more than "3" to a reader deciding where to look.
  const dominant = modes
    .map((m) => ({ mode: m, n: lanes.filter((l) => String(l.mode || '') === m).length }))
    .sort((a, b) => b.n - a.n)[0];

  grid.innerHTML = `
    <div class="dash-metric-card">
      <div class="dash-metric-title">Corridors</div>
      <div class="dash-metric-val">${lanes.length} <span style="font-size:15px;color:var(--text-3);font-weight:600">of ${LANES.length}</span></div>
      <div class="dash-metric-sub"><span>${lanes.length === LANES.length
        ? 'every corridor in this plan' : 'shown by the filters above'}</span></div>
    </div>

    <div class="dash-metric-card">
      <div class="dash-metric-title">Corridor spend</div>
      <div class="dash-metric-val">${spends.length
        ? formatCurrency(spends.reduce((a, b) => a + b, 0))
        : '<span class="wh-absent wh-absent-label">Not solved</span>'}</div>
      <div class="dash-metric-sub"><span>${spends.length
        ? `solved transport cost on ${spends.length} of ${lanes.length} corridor${lanes.length === 1 ? '' : 's'}`
          + (lanes.length - spends.length > 0
              ? ` · ${lanes.length - spends.length} report none`
              : '')
        : 'no corridor carries a solved transport cost'}</span></div>
    </div>

    <div class="dash-metric-card">
      <div class="dash-metric-title">Volume moved</div>
      <div class="dash-metric-val">${flows.length
        ? formatNumber(flows.reduce((a, b) => a + b, 0))
        : '<span class="wh-absent wh-absent-label">Not solved</span>'}</div>
      <div class="dash-metric-sub"><span>${flows.length
        ? (dominant ? `mostly by ${dominant.mode.toLowerCase()}` : 'across the corridors shown')
          + (lanes.length - flows.length > 0
              ? ` · ${lanes.length - flows.length} corridor${
                  lanes.length - flows.length === 1 ? '' : 's'} report no volume`
              : '')
        : 'no corridor carries a solved volume'}</span></div>
    </div>`;
}

function renderLaneView() {
  renderLaneSummary();
  const lanes = visibleLanes();

  // Ranked by what each corridor actually costs the plan — the solve's own
  // transport cost for it, over the horizon. Where no corridor carries one
  // there is nothing to rank, so the card ranks by the rate instead AND says
  // that is what it did; a list headed "highest-cost" ranked on a rate the
  // volume could reverse is worse than no list.
  const withSpend = lanes.filter((l) => laneSpend(l) !== null);
  const bySpend = withSpend.length > 0;
  const ranked = [...(bySpend ? withSpend : lanes)]
    .sort((a, b) => (bySpend
      ? laneSpend(b) - laneSpend(a)
      : (figure(b.cost) || 0) - (figure(a.cost) || 0)))
    .slice(0, 8);

  const costTag = el('kpi-lane-cost-tag');
  if (costTag) {
    costTag.textContent = bySpend
      ? 'by solved transport cost' : 'by rate — no solved transport cost';
  }

  const top = el('kpi-lane-top');
  if (top) {
    top.innerHTML = ranked.length === 0
      ? '<div class="wh-status-note">No corridor matches the current filters.</div>'
      : ranked.map((l) => {
        const spend = laneSpend(l);
        return `<div class="kpi-lane-row">
          <div class="kpi-lane-row-name">${esc(laneName(l))}</div>
          <div class="kpi-lane-row-fig">${spend === null
            ? (l.cost == null ? ABSENT : `${formatCurrencyExact(l.cost)}<small>per unit</small>`)
            : `${formatCurrency(spend)}<small>${l.cost == null ? ''
                : `${formatCurrencyExact(l.cost)}/unit · `}${num(l.flow)} ${perPeriodLabel()}</small>`}</div>
        </div>`;
      }).join('');
  }

  // The mix of modes, counted — and the volume on each where the solve
  // produced one. A corridor count and a volume share are different findings
  // and the card states which is which rather than blending them.
  const modes = [...new Set(lanes.map((l) => String(l.mode || '')).filter(Boolean))];
  const modeBox = el('kpi-lane-modes');
  if (modeBox) {
    modeBox.innerHTML = modes.length === 0
      ? '<div class="wh-status-note">No corridor in this network states a transport mode.</div>'
      : modes.map((mode) => {
        const rows = lanes.filter((l) => String(l.mode || '') === mode);
        const flows = rows.map((l) => figure(l.flow)).filter((v) => v !== null);
        const share = lanes.length ? (rows.length / lanes.length) * 100 : 0;
        return `<div class="kpi-lane-row">
          <div class="kpi-lane-row-name">
            <span class="kpi-mode-bar" style="width:${share.toFixed(1)}%"></span>
            <span class="kpi-mode-label">${esc(modeLabel(mode))}</span>
          </div>
          <div class="kpi-lane-row-fig">${rows.length}<small>${share.toFixed(1)}% of corridors${
            flows.length ? ` · ${formatNumber(flows.reduce((a, b) => a + b, 0))} units` : ''}</small></div>
        </div>`;
      }).join('');
  }
  const modeTag = el('kpi-lane-mode-tag');
  if (modeTag) {
    modeTag.textContent = modes.length
      ? `${modes.length} mode${modes.length === 1 ? '' : 's'}`
      : 'not stated';
  }

  const countTag = el('kpi-lane-count');
  if (countTag) {
    countTag.textContent = `${lanes.length} corridor${lanes.length === 1 ? '' : 's'}`;
  }

  // The table alone narrows to the search; every card above it still reports
  // the filtered view, because a find is not a filter.
  const term = view.laneSearch.trim().toLowerCase();
  const found = term
    ? lanes.filter((l) => `${laneName(l)} ${l.mode || ''}`.toLowerCase().includes(term))
    : lanes;

  const tableSub = el('kpi-lane-table-sub');
  if (tableSub) {
    tableSub.textContent = term
      ? `${found.length} of ${lanes.length} corridor${lanes.length === 1 ? '' : 's'} match "${view.laneSearch.trim()}"`
      : 'Every corridor in the current view';
  }

  const body = document.querySelector('#table-kpi-lanes tbody');
  if (body) {
    body.innerHTML = found.length === 0
      ? `<tr><td colspan="6">${term
          ? 'No corridor matches that search.'
          : 'No corridor matches the current filters.'}</td></tr>`
      : found.map((l) => `<tr>
          <td><strong>${esc(laneName(l))}</strong></td>
          <td class="num">${num(l.flow)}</td>
          <td class="num">${num(l.distance, ' km')}</td>
          <td class="num font-bold">${l.cost == null ? ABSENT : formatCurrencyExact(l.cost)}</td>
          <td class="num">${l.leadTime == null ? ABSENT : `${l.leadTime} days`}</td>
          <td>${l.mode ? `<span class="tag tag-muted">${esc(modeLabel(l.mode))}</span>` : ABSENT}</td>
        </tr>`).join('');
  }
}

// ─── The controls ───────────────────────────────────────────

function option(value, label, selected) {
  return `<option value="${esc(value)}"${selected ? ' selected' : ''}>${esc(label)}</option>`;
}

/**
 * Rebuild the three selects for the current lens.
 *
 * Options come from the data that is actually loaded, so no control can offer
 * a region, a state or a mode the network does not contain — a filter that
 * leads only to an empty screen is a control that lies about what is there.
 */
function renderControls() {
  const isLane = view.domain === 'lane';

  // Each lens shows only the dimensions that describe it. A corridor has no
  // facility and a facility has no origin, so the others are hidden rather
  // than left on the bar as controls that narrow nothing.
  const show = (id, on) => {
    const wrap = el(id);
    if (wrap) wrap.style.display = on ? '' : 'none';
  };

  show('kpi-filter-entity-wrap', !isLane);
  show('kpi-filter-period-wrap', !isLane);
  show('kpi-filter-origin-wrap', isLane);
  show('kpi-filter-dest-wrap', isLane);

  if (isLane) {
    const facets = laneFacets();
    // A network whose corridors state no mode has nothing to filter by.
    show('kpi-filter-mode-wrap', facets.modes.length > 0);

    // DEPENDENT BOTH WAYS. `laneFacets` narrows each list by the OTHER
    // selection, so picking Bengaluru as an origin leaves only the
    // destinations something actually runs to from there — and the same in
    // reverse. It reads `view`, which these controls now write directly, so
    // the lists are correct the moment either changes.
    const origin = el('kpi-filter-origin');
    if (origin) {
      origin.innerHTML = option('all', 'All origins', view.origin === 'all')
        + facets.origins.map((o) => option(o.id, o.name, o.id === view.origin)).join('');
    }
    const dest = el('kpi-filter-dest');
    if (dest) {
      dest.innerHTML = option('all', 'All destinations', view.destination === 'all')
        + facets.destinations.map((d) => option(d.id, d.name, d.id === view.destination)).join('');
    }
    const mode = el('kpi-filter-mode');
    if (mode) {
      mode.innerHTML = option('all', 'All modes', view.mode === 'all')
        + facets.modes.map((m) => option(m, modeLabel(m), view.mode === m)).join('');
    }
    return;
  }

  show('kpi-filter-mode-wrap', false);
  const facets = warehouseFacets();

  // THE CONTROL IS NAMED FOR ITS BUCKET. "Facility" over a list of plants is
  // the generic word for the thing the tab above has already narrowed.
  const label = el('kpi-filter-entity-label');
  if (label) {
    label.textContent = view.domain === 'plant' ? 'Plant'
      : view.domain === 'dc' ? 'Distribution centre' : 'Facility';
  }

  const entity = el('kpi-filter-entity');
  if (entity) {
    entity.innerHTML = option('all',
      `All ${view.domain === 'plant' ? 'plants'
        : view.domain === 'network' ? 'facilities' : 'distribution centres'}`,
      view.entityId === null)
      + facets.entities.map((e) => option(e.id, e.name, e.id === view.entityId)).join('');
  }

  // THE APPLICATION'S OWN PERIOD CONTROL, moved here from the top bar.
  //
  // Not a second list and not a new mechanism: `hooks.populatePeriods` is the
  // same `populatePeriodSelect` every other screen's period control is filled
  // by, reading the client's recorded capacity history, and it selects the
  // same `state.selectedPeriod`. The control was in the global top bar and is
  // on the screen it describes instead.
  const period = el('kpi-filter-period');
  if (period && hooks.populatePeriods) {
    const had = period.value;
    hooks.populatePeriods(period);
    if (had && [...period.options].some((o) => o.value === had)) period.value = had;
  }
  show('kpi-filter-period-wrap', Boolean(period) && period.options.length > 1);
}

/**
 * The part of the network's cost that belongs to no facility.
 *
 * Shown on the Network lens only, and only when there IS a gap. It is not an
 * error and not missing money: the solve decides inventory for the network
 * rather than per warehouse, so the figure is real and simply has no building
 * to sit in. Stated plainly, because the alternative is a reader adding up the
 * Facility Cost column, coming up short, and concluding the screen is wrong.
 */
function renderCostAttribution() {
  const node = el('kpi-cost-attribution');
  if (!node) return;

  const inventory = hooks.networkInventoryCost ? hooks.networkInventoryCost() : null;
  if (view.domain !== 'network' || typeof inventory !== 'number'
      || !Number.isFinite(inventory) || inventory <= 0) {
    node.hidden = true;
    return;
  }

  const attributed = warehouseAttributedHolding();
  const unattributed = inventory - attributed;
  // Rounding noise is not a finding.
  if (unattributed <= Math.max(1, inventory * 0.005)) { node.hidden = true; return; }

  // ON THE SPEND CARD, not floating above the filter bar.
  //
  // This is a caption about ONE chart's numbers — the facility spend donut,
  // whose slices do not sum to the network's cost because inventory is decided
  // for the network rather than per site. Sat between the scorecard and the
  // controls it read as a general announcement; moved onto the card it
  // explains, it is where a reader who has just tried to add the slices up
  // will actually look. Relocated rather than deleted: without it a reader
  // does that sum, comes up short, and concludes the page is wrong.
  node.hidden = false;
  node.textContent = attributed > 0
    ? `Inventory holding — ${formatCurrency(unattributed)} of `
      + `${formatCurrency(inventory)} is held at network level and is not `
      + `attributed to any site, so the facility figures do not sum to the total cost.`
    : `Inventory holding — ${formatCurrency(inventory)} — is held at network `
      + `level and is not attributed to any site, so the facility figures do `
      + `not sum to the total cost.`;
}

/** What is on screen, in words, under the controls that put it there. */
function renderShowing() {
  const node = el('kpi-showing');
  if (!node) return;

  if (view.domain === 'lane') {
    const shown = visibleLanes().length;
    const parts = [];
    if (view.origin !== 'all') parts.push(`from ${endpointName(view.origin)}`);
    if (view.destination !== 'all') parts.push(`to ${endpointName(view.destination)}`);
    if (view.mode !== 'all') parts.push(`by ${modeLabel(view.mode).toLowerCase()}`);
    node.textContent = `Showing: ${parts.length ? parts.join(' \u00b7 ') : 'all corridors'}`
      + ` \u00b7 ${shown} of ${LANES.length} corridor${LANES.length === 1 ? '' : 's'}`;
    return;
  }

  if (view.entityId) {
    const row = warehouseRow(view.entityId);
    const fac = getFacilityById(view.entityId);
    node.textContent = `Showing: ${row?.facility_name || fac?.name || view.entityId}`
      + ' — one facility';
    return;
  }

  const facets = warehouseFacets();
  if (!facets.ready) { node.textContent = 'Reading the solved footprint…'; return; }
  const scope = facets.shownCount === facets.domainCount
    ? `${facets.shownCount} site${facets.shownCount === 1 ? '' : 's'}`
    : `${facets.shownCount} of ${facets.domainCount} sites`;
  node.textContent = `Showing: ${warehouseViewLabel()} · ${scope} · ${facets.openCount} open`;
}

function renderBreadcrumb() {
  const bar = el('kpi-breadcrumb');
  const trail = el('kpi-crumb-trail');
  const back = el('kpi-crumb-back');
  if (!bar) return;

  if (!view.entityId) { bar.style.display = 'none'; return; }
  bar.style.display = '';

  const row = warehouseRow(view.entityId);
  const fac = getFacilityById(view.entityId);
  const name = row?.facility_name || fac?.name || view.entityId;
  if (trail) {
    trail.innerHTML = `<span class="kpi-crumb-root">${esc(DOMAIN_LABEL[view.domain])}</span>`
      + `<span class="kpi-crumb-sep">›</span><strong>${esc(name)}</strong>`;
  }
  if (back) {
    back.textContent = `← Back to all ${view.domain === 'plant' ? 'plants'
      : view.domain === 'network' ? 'facilities' : 'distribution centres'}`;
  }
}

// ─── Applying the state ─────────────────────────────────────

/**
 * Put the screen into the state `view` describes.
 *
 * Order matters: the facility filter is pushed down BEFORE anything is drawn,
 * so the roll-up sections and the controls above them are built from the same
 * population in the same pass.
 */
export function applyView() {
  document.querySelectorAll('#kpi-domain-bar .kpi-domain-tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.domain === view.domain);
  });

  const isLane = view.domain === 'lane';
  const isNetwork = view.domain === 'network';
  const onEntity = Boolean(view.entityId) && !isLane;

  const rollup = el('kpi-rollup');
  const entity = el('kpi-entity');
  const lanes = el('kpi-lanes');
  if (rollup) rollup.style.display = (isLane || onEntity) ? 'none' : '';
  if (entity) entity.style.display = onEntity ? '' : 'none';
  if (lanes) lanes.style.display = isLane ? '' : 'none';

  // THE FILTER GOES DOWN FIRST. Everything below reads the narrowed rows
  // through `warehouse.js`, including the caption that names them — computing
  // that caption before this line labelled the Plants tab "every facility",
  // because it was still reading the lens the reader had just left.
  if (!isLane) {
    // The lens IS the narrowing now. Region and Status were the other two and
    // both are gone — region duplicated what the facility list already shows,
    // and hiding tight sites is the one thing a reader of a capacity screen
    // never wants — so they are pinned open rather than left as stale state
    // some other caller could set.
    setWarehouseFilter({ domain: view.domain, region: 'all', status: 'all' });
  }

  // TIER 0 FOLLOWS THE SCOPE. The Network lens shows the four figures the
  // Overview states, drawn by Overview's OWN renderer against the
  // authoritative KPI layer — one source, so the two screens cannot report
  // different numbers for one network. Every other lens shows that
  // population counted, and re-counted on each filter.
  // EXACTLY ONE of the three is on screen. The facility grid used to be left
  // visible on the corridor lens carrying whatever the previous lens had
  // rendered — plant counts above a corridor table — so each is now tied to
  // the lens it belongs to rather than to "not the network one".
  const strip = el('kpi-network-strip');
  const cards = el('wh-summary-grid');
  const laneCards = el('kpi-lane-summary');
  if (strip) strip.style.display = isNetwork ? '' : 'none';
  if (cards) cards.style.display = (!isNetwork && !isLane) ? '' : 'none';
  if (laneCards) laneCards.style.display = isLane ? '' : 'none';
  if (isNetwork && hooks.renderNetworkScorecard) {
    hooks.renderNetworkScorecard('kpi-network-strip-row');
  }
  const note = el('kpi-scorecard-note');
  if (note) {
    // INSIDE A DRILL-DOWN THE SCORECARD IS NOT ABOUT THIS SITE.
    //
    // It is the lens's summary, and it does not narrow to one facility — so
    // above a screen headed "Bengaluru — one facility" it read "4 of 5 open"
    // and "49.0%", which a reader takes for Bengaluru's own. Naming the
    // population and then naming what it is NOT is what separates them.
    const lens = view.domain === 'plant' ? 'Plant'
      : view.domain === 'dc' ? 'Distribution-centre' : 'Network';
    // ONLY WHERE THERE IS SOMETHING TO SAY.
    //
    // "These figures describe plants." restated the tab already highlighted
    // above it and the "Showing:" line already beside it — a third label for
    // a population nobody had asked twice about. The two lines kept are the
    // two that state something neither of those does: that a drill-down's
    // scorecard is NOT about the selected site, and that the Network lens
    // agrees with the Overview.
    // ONLY THE DRILL-DOWN LINE SURVIVES.
    //
    // The network branch read "Every facility in this plan — the same figures
    // the Overview reports." That is a third label for a population the
    // highlighted tab already names and the "Showing:" line beside it already
    // counts, floating between the scorecard and the filters. It is exactly
    // the loose sentence between cards that keeps being asked about.
    //
    // The drill-down line stays because it is the one that states something
    // nothing else on screen does: above a page headed "Bengaluru — one
    // facility", this row of figures is NOT Bengaluru's, and a reader who
    // assumes otherwise reads a wrong number off it.
    note.textContent = onEntity
      ? `${lens} network summary — not this facility's own figures, which are below.`
      : '';
    note.hidden = !note.textContent;
  }

  renderControls();
  renderCostAttribution();
  renderShowing();
  renderBreadcrumb();

  // The buttons mount once the cards exist; the panels close on every view
  // change, because a briefing describes the rows that were on screen when it
  // was asked for and this call is the moment those rows change.
  initKpiExplain();
  closeKpiExplainPanels();
  // One reflection across the buttons, so a feature that is otherwise a small
  // control in a chart header gets noticed once. It stops for good on the
  // first click and never runs under reduced-motion.
  startKpiExplainShimmer();

  if (isLane) { renderLaneView(); return; }
  if (onEntity && hooks.renderEntity) hooks.renderEntity();
}

/** Drill into one site, or back out of it. */
/**
 * Put the reader at the top of what they just opened.
 *
 * INSTANT, and twice. A smooth scroll animates over several hundred
 * milliseconds while `renderFacilityDashboard()` is still drawing its charts
 * on a 60ms timer; each chart that lands changes the page height under the
 * running animation, and the scroll finished wherever the shifting content
 * left it — which is how clicking a facility landed the reader halfway down
 * its charts with the breadcrumb and its first cards off-screen.
 *
 * So: jump immediately, then jump again once the charts have laid out. The
 * second call is a no-op when nothing moved.
 */
function scrollViewToTop() {
  const main = document.querySelector('.main-content');
  if (!main) return;
  main.scrollTop = 0;
  // After the chart timers in `renderFacilityDashboard` (60ms) have run and
  // the browser has laid the result out.
  setTimeout(() => { main.scrollTop = 0; }, 120);
}

function selectEntity(facilityId) {
  view.entityId = facilityId || null;
  if (view.entityId && hooks.selectEntity) hooks.selectEntity(view.entityId);
  applyView();
  scrollViewToTop();
}

function setDomain(domain) {
  if (view.domain === domain) return;
  view.domain = domain;
  // A lens change always returns to that lens's whole population. Carrying a
  // selected site across would be worse than useless: the same name on
  // another lens is a different site or no site at all.
  view.entityId = null;
  view.mode = 'all';
  view.origin = 'all';
  view.destination = 'all';
  view.laneSearch = '';
  const box = el('kpi-lane-search');
  if (box) box.value = '';
  applyView();
}

// ─── Export ─────────────────────────────────────────────────

/**
 * Stamp the page with what produced it, then hand it to the browser's own
 * PDF engine.
 *
 * WHY window.print AND NOT A PDF LIBRARY. The requirement is "exactly the
 * visuals displayed by the selected KPI view" — and the thing that is exactly
 * those visuals is the page itself. A client-side renderer would re-draw every
 * chart into an image and could drift from what the reader is looking at,
 * which is the whole class of bug this screen has been spent removing. The
 * print stylesheet hides the application chrome and lets the real canvases
 * through.
 *
 * The header is written HERE rather than in markup because it has to name the
 * filters that were active at the moment of export.
 */
function renderPrintHeader() {
  const meta = el('kpi-print-meta');
  if (!meta) return;
  const project = document.getElementById('topbar-current-project-name');
  const basis = document.getElementById('wh-basis');
  const showing = el('kpi-showing');

  const rows = [
    ['Project', project ? project.textContent.trim() : ''],
    ['View', currentKpiView().label || DOMAIN_LABEL[view.domain] || ''],
    ['Horizon', basis ? basis.textContent.trim() : ''],
    ['Showing', showing ? showing.textContent.replace(/^Showing:\s*/, '').trim() : ''],
    ['Exported', new Date().toLocaleString()],
  ].filter(([, value]) => value);

  meta.innerHTML = rows.map(([label, value]) =>
    `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('');
}

/**
 * The current view, as a workbook.
 *
 * WHY EXCEL AND NOT THE PDF THIS REPLACES. The PDF was the screen as it
 * looks, which is the right artefact for circulating a conclusion and the
 * wrong one for the question this button is actually pressed to answer:
 * somebody wants these figures in a model of their own. A picture of a table
 * has to be retyped.
 *
 * The scope travels with it. The sites are the ones the screen is showing
 * after its lens and filters, so the workbook is about the population the
 * reader was looking at — and its cover says which population that was.
 */
export async function exportKpiViewToExcel(button) {
  const label = button && button.querySelector('span');
  const original = label ? label.textContent : '';
  if (button) button.disabled = true;
  if (label) label.textContent = 'Preparing\u2026';
  try {
    const { blob, filename } = await kpiService.downloadWorkbook({
      facilityIds: view.domain === 'lane'
        ? [] : warehouseFacets().entities.map((e) => e.id),
      lensLabel: currentKpiView().label || DOMAIN_LABEL[view.domain] || '',
      filters: activeFilterText(),
      horizon: (el('wh-basis') || {}).textContent || '',
    });
    // The service returns the bytes; handing them to the browser is the
    // caller's job, the same way every other download on this product works.
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename || 'netgravity-kpis.xlsx';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    if (label) label.textContent = original;
  } catch (err) {
    // ON the button, not in a console nobody has open.
    if (label) label.textContent = 'Could not export';
    setTimeout(() => { if (label) label.textContent = original; }, 3200);
  } finally {
    if (button) button.disabled = false;
  }
}

/**
 * How these figures are calculated, as a document.
 *
 * A document takes a solve and, where the gateway is configured, a
 * text-generation call — which the gateway allows itself a minute for. A
 * button that says the same thing for that long reads as a hung one, so it
 * says what it is doing as it goes.
 */
export async function exportKpiMethod(button) {
  const label = button && button.querySelector('span');
  const original = label ? label.textContent : '';
  if (button) button.disabled = true;
  if (label) label.textContent = 'Preparing\u2026';
  const staged = setTimeout(() => {
    if (label && button && button.disabled) {
      label.textContent = 'Writing the explanation\u2026';
    }
  }, 5000);
  try {
    const { blob, filename } = await kpiService.downloadMethod({
      facilityIds: view.domain === 'lane'
        ? [] : warehouseFacets().entities.map((e) => e.id),
      lensLabel: currentKpiView().label || DOMAIN_LABEL[view.domain] || '',
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename || 'netgravity-method.docx';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    if (label) label.textContent = original;
  } catch (err) {
    if (label) label.textContent = 'Could not build the document';
    if (button) button.title = (err && err.message) || '';
    setTimeout(() => { if (label) label.textContent = original; }, 3600);
  } finally {
    clearTimeout(staged);
    if (button) button.disabled = false;
  }
}

/** The narrowings in force, in words, for the workbook's cover. */
function activeFilterText() {
  const parts = view.domain === 'lane'
    ? [view.origin !== 'all' ? `from ${endpointName(view.origin)}` : null,
       view.destination !== 'all' ? `to ${endpointName(view.destination)}` : null,
       view.mode !== 'all' ? `by ${modeLabel(view.mode)}` : null]
    : [view.entityId
        ? (warehouseRow(view.entityId)?.facility_name || view.entityId) : null];
  const named = parts.filter(Boolean);
  return named.length ? named.join(' · ') : 'None';
}


// ─── Wiring ─────────────────────────────────────────────────

/**
 * Called once at start-up. `hooks` carries the two things this module cannot
 * do itself: set the application's selected facility, and draw that facility's
 * detail — both of which live in `app.js`, which imports this file.
 */
/**
 * Fill the print header whenever a print starts, from wherever it started.
 *
 * It used to be filled by the export button, which meant a reader pressing
 * Ctrl+P got the same page with an EMPTY header — a sheet of figures with no
 * statement of what they are of. The button is Excel now and prints no longer
 * pass through it at all, so this listens for the thing itself.
 */
if (typeof window !== 'undefined') {
  window.addEventListener('beforeprint', () => {
    try {
      renderPrintHeader();
      // An open explanation floats OVER a chart, so printing with it open
      // would hide the visual it describes.
      closeKpiExplainPanels();
    } catch (e) { /* a print must not be blocked by its own header */ }
  });
}

export function initKpiView(nextHooks) {
  hooks = { ...hooks, ...(nextHooks || {}) };
  if (wired) return;
  wired = true;

  el('kpi-domain-bar')?.addEventListener('click', (e) => {
    const tab = e.target.closest('.kpi-domain-tab');
    if (tab) setDomain(tab.dataset.domain);
  });

  // ── The filter row ──
  //
  // Each control writes to `view` and redraws. There is no staging: the bar
  // carries at most three controls, and Apply on three controls is a click
  // that buys the reader nothing they did not already have.

  el('kpi-filter-entity')?.addEventListener('change', (e) => {
    const next = e.target.value === 'all' ? null : e.target.value;
    const changed = next !== view.entityId;
    view.entityId = next;
    if (view.entityId && changed && hooks.selectEntity) {
      hooks.selectEntity(view.entityId);
    }
    applyView();
    if (changed) scrollViewToTop();
  });

  // The same period the rest of the application is scoped to.
  el('kpi-filter-period')?.addEventListener('change', (e) => {
    if (hooks.selectPeriod) hooks.selectPeriod(e.target.value);
    applyView();
  });

  el('kpi-filter-origin')?.addEventListener('change', (e) => {
    view.origin = e.target.value;
    // An origin narrows which destinations exist. If the one already chosen
    // is not among them, it is dropped rather than left naming a corridor
    // this pair has no route on.
    if (view.destination !== 'all'
        && !laneFacets().destinations.some((d) => d.id === view.destination)) {
      view.destination = 'all';
    }
    applyView();
  });
  el('kpi-filter-dest')?.addEventListener('change', (e) => {
    view.destination = e.target.value;
    if (view.origin !== 'all'
        && !laneFacets().origins.some((o) => o.id === view.origin)) {
      view.origin = 'all';
    }
    applyView();
  });
  el('kpi-filter-mode')?.addEventListener('change', (e) => {
    view.mode = e.target.value;
    // A mode can remove both ends of the pair that was chosen.
    const facets = laneFacets();
    if (view.origin !== 'all' && !facets.origins.some((o) => o.id === view.origin)) {
      view.origin = 'all';
    }
    if (view.destination !== 'all'
        && !facets.destinations.some((d) => d.id === view.destination)) {
      view.destination = 'all';
    }
    applyView();
  });

  el('kpi-filter-reset')?.addEventListener('click', () => {
    view.entityId = null;
    view.mode = 'all';
    view.origin = 'all';
    view.destination = 'all';
    view.laneSearch = '';
    const box = el('kpi-lane-search');
    if (box) box.value = '';
    applyView();
  });

  el('kpi-lane-search')?.addEventListener('input', (e) => {
    view.laneSearch = e.target.value || '';
    // Only the table is redrawn: re-running applyView would rebuild the
    // controls and take the cursor out of the box being typed in.
    renderLaneView();
  });

  el('btn-export-xlsx')?.addEventListener('click',
    (e) => exportKpiViewToExcel(e.currentTarget));
  el('btn-export-method')?.addEventListener('click',
    (e) => exportKpiMethod(e.currentTarget));

  el('kpi-crumb-back')?.addEventListener('click', () => selectEntity(null));

  // One delegated listener for a table that is rebuilt on every filter change.
  document.querySelector('#table-wh-health tbody')?.addEventListener('click', (e) => {
    const row = e.target.closest('.wh-health-row');
    if (row?.dataset.facilityId) selectEntity(row.dataset.facilityId);
  });

  // The controls are built from the report — which regions and which states
  // actually occur — so they are rebuilt when it lands rather than on a timer.
  window.addEventListener('warehouse-report-ready', () => {
    renderControls();
    renderShowing();
  });
}

/**
 * Land on the whole network.
 *
 * Called by `navigateToTab`. A reader arriving from the Overview is arriving
 * with a network-level question, so the screen always opens on the network —
 * never on whichever site a previous visit happened to leave selected.
 */
export function renderKpiView() {
  view.entityId = null;
  applyView();
}

/**
 * What is on screen right now, for the export.
 *
 * The button downloads the current view, so the file has to be able to say
 * which view that was. `label` goes in the file's header; the flags decide
 * which sections it carries.
 */
export function currentKpiView() {
  return {
    domain: view.domain,
    entityId: view.entityId,
    label: view.domain === 'lane'
      ? ['Corridors',
         view.origin === 'all' ? null : `from ${endpointName(view.origin)}`,
         view.destination === 'all' ? null : `to ${endpointName(view.destination)}`,
         view.mode === 'all' ? 'all modes' : view.mode,
        ].filter(Boolean).join(' · ')
      : warehouseViewLabel(),
    lanes: view.domain === 'lane' ? visibleLanes() : null,
  };
}
