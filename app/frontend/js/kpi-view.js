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

/** The whole screen's state. Read by `applyView()` and by the export. */
const view = {
  /** 'network' | 'dc' | 'plant' | 'lane' */
  domain: 'network',
  /** A facility id, or null for the roll-up. Only ever set on a facility lens. */
  entityId: null,
  region: 'all',
  status: 'all',
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

/**
 * WHAT THE PANEL IS CURRENTLY SHOWING, before Apply.
 *
 * Every control used to write straight to `view` and redraw, so narrowing by
 * region and then by status rebuilt the screen twice and the page moved under
 * a decision the reader had not finished making. Choices land here; Apply
 * copies them across and renders once.
 */
const staged = { entityId: null, region: 'all', status: 'all',
                 mode: 'all', origin: 'all', destination: 'all' };

function stageFromView() {
  staged.entityId = view.entityId;
  staged.region = view.region;
  staged.status = view.status;
  staged.mode = view.mode;
  staged.origin = view.origin;
  staged.destination = view.destination;
}

/** How many narrowings are active, for the closed trigger. */
function activeFilterCount() {
  if (view.domain === 'lane') {
    return [view.origin, view.destination, view.mode]
      .filter((v) => v !== 'all').length;
  }
  return [view.region, view.status].filter((v) => v !== 'all').length
    + (view.entityId ? 1 : 0);
}

/** The trigger says what is active, so a filtered screen says so with the
 *  panel shut. */
function renderFilterTrigger() {
  const label = el('kpi-filters-label');
  const count = el('kpi-filters-count');
  const n = activeFilterCount();
  if (label) {
    const parts = view.domain === 'lane'
      ? [view.origin !== 'all' ? endpointName(view.origin) : null,
         view.destination !== 'all' ? endpointName(view.destination) : null,
         view.mode !== 'all' ? view.mode : null]
      : [view.entityId ? (warehouseRow(view.entityId)?.facility_name || view.entityId) : null,
         view.region !== 'all' ? view.region : null,
         view.status !== 'all' ? (BAND_TEXT[view.status] || view.status) : null];
    const named = parts.filter(Boolean);
    label.textContent = named.length ? named.join(' · ') : 'Filters';
  }
  if (count) {
    count.hidden = n === 0;
    count.textContent = String(n);
  }
  const trigger = el('kpi-filters-trigger');
  if (trigger) trigger.classList.toggle('is-active', n > 0);
}

/** Set by `initKpiView()`. Kept as hooks rather than imports so this module
 *  and `app.js` do not import each other. */
let hooks = { selectEntity: null, renderEntity: null,
              renderNetworkScorecard: null, networkInventoryCost: null };

let wired = false;

const BAND_TEXT = {
  CRITICAL: 'Over capacity', TIGHT: 'Tight', HEALTHY: 'Healthy',
  UNDERUSED: 'Under-used', NOT_OPERATING: 'Not in this plan',
};

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
  // health band and a facility has no origin, so the other three are hidden
  // rather than left on screen as controls that narrow nothing.
  const show = (id, on) => {
    const wrap = el(id);
    if (wrap) wrap.style.display = on ? '' : 'none';
  };

  if (isLane) {
    const facets = laneFacets();
    show('kpi-filter-entity-wrap', false);
    show('kpi-filter-region-wrap', false);
    show('kpi-filter-status-wrap', false);
    show('kpi-filter-origin-wrap', true);
    show('kpi-filter-dest-wrap', true);
    // A network whose corridors state no mode has nothing to filter by.
    show('kpi-filter-mode-wrap', facets.modes.length > 0);

    const origin = el('kpi-filter-origin');
    if (origin) {
      origin.innerHTML = option('all', 'All origins', staged.origin === 'all')
        + facets.origins.map((o) => option(o.id, o.name, o.id === staged.origin)).join('');
    }
    const dest = el('kpi-filter-dest');
    if (dest) {
      dest.innerHTML = option('all', 'All destinations', staged.destination === 'all')
        + facets.destinations.map((d) => option(d.id, d.name, d.id === staged.destination)).join('');
    }
    const mode = el('kpi-filter-mode');
    if (mode) {
      mode.innerHTML = option('all', 'All modes', staged.mode === 'all')
        + facets.modes.map((m) => option(m, modeLabel(m), staged.mode === m)).join('');
    }
    return;
  }

  const facets = warehouseFacets();
  show('kpi-filter-entity-wrap', true);
  show('kpi-filter-status-wrap', true);
  show('kpi-filter-origin-wrap', false);
  show('kpi-filter-dest-wrap', false);
  show('kpi-filter-mode-wrap', false);
  // A network whose sites carry no region has nothing to filter by.
  show('kpi-filter-region-wrap', facets.regions.length > 0);

  const entity = el('kpi-filter-entity');
  if (entity) {
    entity.innerHTML = option('all',
      `All ${view.domain === 'plant' ? 'plants'
        : view.domain === 'network' ? 'facilities' : 'distribution centres'}`,
      staged.entityId === null)
      + facets.entities.map((e) => option(e.id, e.name, e.id === staged.entityId)).join('');
  }

  const region = el('kpi-filter-region');
  if (region) {
    region.innerHTML = option('all', 'All regions', staged.region === 'all')
      + facets.regions.map((r) => option(r, r, r === staged.region)).join('');
  }

  const status = el('kpi-filter-status');
  if (status) {
    status.innerHTML = option('all', 'All states', staged.status === 'all')
      + facets.statuses.map((s) => option(s.value, s.label, s.value === staged.status)).join('');
  }
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
    setWarehouseFilter({ domain: view.domain, region: view.region, status: view.status });
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
    note.textContent = onEntity
      ? `${lens} network summary — not this facility's own figures, which are below.`
      : isNetwork
        ? 'Every facility in this plan — the same figures the Overview reports.'
        : isLane ? 'These figures describe the corridors shown below.'
        : `These figures describe ${warehouseViewLabel().toLowerCase()}.`;
  }

  stageFromView();
  renderControls();
  renderFilterTrigger();
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
  // A lens change always returns to that lens's whole population. Region and
  // Status describe a facility and mean nothing to a corridor; carrying a
  // selected site across is worse still, since the same name on another lens
  // is a different site or no site at all.
  view.entityId = null;
  view.region = 'all';
  view.status = 'all';
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

/** The current view, as a PDF, through the browser's print dialog. */
export function exportKpiViewAsPdf() {
  renderPrintHeader();
  // Any open explanation is a floating overlay ON TOP of a chart, so printing
  // with it open would hide the very visual it describes.
  closeKpiExplainPanels();

  // NOTHING TOUCHES THE DOM AFTER THIS POINT.
  //
  // There was a body class here, added before printing and removed on a
  // 1500ms timer. It was styled by nothing — the print rules are all in a
  // media query and never needed it — and the timer fired while Chrome was
  // still generating the preview, mutating the document underneath it. The
  // preview sat on "Loading preview…" and never resolved.
  //
  // The layout the printer gets is the layout that is already on screen, so
  // the correct amount of work to do here is none.
  window.print();
}

// ─── Wiring ─────────────────────────────────────────────────

/**
 * Called once at start-up. `hooks` carries the two things this module cannot
 * do itself: set the application's selected facility, and draw that facility's
 * detail — both of which live in `app.js`, which imports this file.
 */
export function initKpiView(nextHooks) {
  hooks = { ...hooks, ...(nextHooks || {}) };
  if (wired) return;
  wired = true;

  el('kpi-domain-bar')?.addEventListener('click', (e) => {
    const tab = e.target.closest('.kpi-domain-tab');
    if (tab) setDomain(tab.dataset.domain);
  });

  // ── The filter panel ──
  //
  // Every control STAGES. Nothing below the panel moves until Apply, so the
  // reader can set three things and see the consequence once.
  const panel = el('kpi-filters-panel');
  const trigger = el('kpi-filters-trigger');

  const openPanel = (open) => {
    if (!panel || !trigger) return;
    panel.hidden = !open;
    trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    // Opening always starts from what is actually on screen, so an abandoned
    // edit cannot leak into the next one.
    if (open) { stageFromView(); renderControls(); }
  };

  trigger?.addEventListener('click', (e) => {
    e.stopPropagation();
    openPanel(panel?.hidden !== false);
  });

  el('kpi-filter-entity')?.addEventListener('change', (e) => {
    staged.entityId = e.target.value === 'all' ? null : e.target.value;
  });
  el('kpi-filter-region')?.addEventListener('change', (e) => {
    staged.region = e.target.value;
  });
  el('kpi-filter-status')?.addEventListener('change', (e) => {
    staged.status = e.target.value;
  });
  el('kpi-filter-origin')?.addEventListener('change', (e) => {
    staged.origin = e.target.value;
    // Dependent: an origin narrows which destinations exist, and the panel
    // has to offer only those before the reader picks one.
    if (staged.destination !== 'all') {
      const was = view.destination; view.destination = staged.destination;
      const wasO = view.origin; view.origin = staged.origin;
      if (!laneFacets().destinations.some((d) => d.id === staged.destination)) {
        staged.destination = 'all';
      }
      view.destination = was; view.origin = wasO;
    }
    renderControls();
  });
  el('kpi-filter-dest')?.addEventListener('change', (e) => {
    staged.destination = e.target.value;
    renderControls();
  });
  el('kpi-filter-mode')?.addEventListener('change', (e) => {
    staged.mode = e.target.value;
    renderControls();
  });

  // Quiet, beside Apply: it undoes rather than does, so it must not compete
  // with the button that changes the screen. Clears the staged edit AND the
  // committed view, so there is no half-reset state to be surprised by.
  el('kpi-filter-reset')?.addEventListener('click', () => {
    view.entityId = null;
    view.region = 'all';
    view.status = 'all';
    view.mode = 'all';
    view.origin = 'all';
    view.destination = 'all';
    view.laneSearch = '';
    stageFromView();
    const box = el('kpi-lane-search');
    if (box) box.value = '';
    openPanel(false);
    applyView();
  });

  el('kpi-filters-apply')?.addEventListener('click', () => {
    const changedEntity = staged.entityId !== view.entityId;
    view.region = staged.region;
    view.status = staged.status;
    view.mode = staged.mode;
    view.origin = staged.origin;
    view.destination = staged.destination;
    view.entityId = staged.entityId;
    // A narrowing can remove the very site being viewed.
    if (view.entityId && view.domain !== 'lane') {
      setWarehouseFilter({ domain: view.domain, region: view.region, status: view.status });
      if (!warehouseFacets().entities.some((x) => x.id === view.entityId)) {
        view.entityId = null;
      }
    }
    if (view.entityId && changedEntity && hooks.selectEntity) {
      hooks.selectEntity(view.entityId);
    }
    openPanel(false);
    applyView();
    if (changedEntity) scrollViewToTop();
  });

  // Clicking away abandons the edit rather than half-applying it.
  document.addEventListener('click', (e) => {
    if (!panel || panel.hidden) return;
    if (!el('kpi-filters')?.contains(e.target)) openPanel(false);
  });

  el('kpi-lane-search')?.addEventListener('input', (e) => {
    view.laneSearch = e.target.value || '';
    // Only the table is redrawn: re-running applyView would rebuild the
    // controls and take the cursor out of the box being typed in.
    renderLaneView();
  });

  el('btn-export-pdf')?.addEventListener('click', () => exportKpiViewAsPdf());

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
