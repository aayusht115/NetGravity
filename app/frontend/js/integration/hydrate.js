/**
 * NetGravity — Authoritative Hydration
 * ====================================
 * Fills the prototype's own data structures (`js/data.js`) with real, solved
 * values from the backend, so every existing screen renders authoritative data
 * without a single change to its markup or layout.
 *
 * Why this shape
 * --------------
 * Eight frontend modules import from `data.js`. Reassigning its exports is not
 * possible (they are `const`), but mutating the arrays and objects in place is
 * — and ES module bindings mean every consumer sees the mutation immediately.
 * `data.js` already used that technique in `loadNetworkData()`. This module
 * does the same thing with authoritative figures instead of invented ones.
 *
 * What it refuses to do
 * ---------------------
 * `loadNetworkData()` fabricated where the upload was silent: SLA fixed at
 * 96.5, on-time at 97.2, storage cost at 45,000, and — worst — it derived an
 * "optimised" case by multiplying cost by 0.94 and labelled the result
 * `source: "DETERMINISTIC_ENGINE"`. Nothing here invents a value. A metric the
 * backend reports as non-VALID is left absent, and the screens that read it
 * show their own empty state.
 */

import { apiClient } from './api-client.js';
import { kpiService } from './services/kpi-service.js';
import { scenarioService } from './services/scenario-service.js';
import {
  mapScenarioRecord, baselineFromNetworkKPIs,
} from './mappers/scenario-mapper.js';
import { forecastService } from './services/forecast-service.js';
import { insightService } from './services/insight-service.js';
import { getActiveProjectId, setActiveSnapshotId } from './project-context.js';
import {
  DCS, PLANTS, LANES, FACILITY_KPIS, SCENARIOS, EXTERNAL_SIGNALS,
  SOLVED_STATE_KEY, setNetworkPeriods, OBSERVED_UTILISATION,
  setAuthoritativeBaseline, clearDemoNarrative, loadNetworkData,
  setForecastSeries, getOptimizedBaseCase, applyInsightResponse,
  applyHorizon, SOLVE_HORIZON, setActiveCurrency, setNetworkGeography,
  applySystemStatus, MARKETS, setForecastCatalogue,
} from '../data.js';

/** Read a KPIResult; a non-VALID status yields null, never 0. */
function val(kpi) {
  if (!kpi || kpi.status !== 'VALID') return null;
  return kpi.value;
}

/**
 * Hand the forecast's own metadata to the screens.
 *
 * Set before `setForecastSeries()`, because that dispatches the event the
 * summary card re-renders on and the card reads this.
 */
function publishForecastMeta(meta) {
  if (typeof window !== 'undefined') window.__ngForecastMeta = meta;
}

/**
 * Pick the series the forecast screen shows, and reshape it for the chart.
 *
 * The engine forecasts every market-product pair. The screen has one chart, so
 * it shows the largest series by latest observed demand — the one that matters
 * most — and names it, rather than silently plotting an arbitrary pair or
 * summing pairs the engine deliberately kept separate.
 *
 * Returns null when nothing is forecastable, so the caller can render an
 * explicit empty state instead of a cone.
 */
/** One engine series, reshaped for the chart. */
function reshapeForecastSeries(series, names) {
  const hist = series.history || series.observed || [];
  const pts = series.points || [];
  const marketName = names.markets[series.market_id];
  const productName = names.products[series.product_id];
  return {
    key: `${series.market_id}/${series.product_id}`,
    // Names where the upload supplies them, ids as the secondary detail. The
    // screen showed "M002/P001" and nothing else, which is an internal key a
    // planner has no reason to recognise.
    label: [marketName || series.market_id, productName || series.product_id]
      .join(' · '),
    marketId: series.market_id,
    productId: series.product_id,
    latestObserved: hist.length
      ? Number(hist[hist.length - 1].quantity ?? hist[hist.length - 1]) || 0 : 0,
    seriesLabel: `${series.market_id}/${series.product_id}`,
    accuracy: series.accuracy || null,
    engine: series.engine || '',
    history: {
      labels: hist.map((p, i) => p.timestamp || p.period || `T${i + 1}`),
      values: hist.map((p) => Number(p.quantity ?? p)),
    },
    forecast: {
      labels: pts.map((p) => `+${p.period}`),
      values: pts.map((p) => Math.round(p.mean)),
      // p90/p10 are the engine's own quantiles. Absent when it produced none —
      // never widened or invented to make a nicer-looking band.
      upper: pts.every((p) => p.p90 != null) ? pts.map((p) => Math.round(p.p90)) : [],
      lower: pts.every((p) => p.p10 != null) ? pts.map((p) => Math.round(p.p10)) : [],
    },
  };
}

/**
 * Reshape EVERY forecastable series, largest first.
 *
 * The engine forecasts every market-product pair — 60 of them on the US
 * workbook — and this returned exactly one, the largest, with no way to reach
 * the other 59. "59 series forecast" was reported beside a chart locked to
 * M002/P001. The work was done and 98% of it was unreachable.
 *
 * Sorted by latest observed demand so the default selection is still the
 * series that matters most.
 */
function reshapeAllForecastSeries(fc, names) {
  const all = (fc && fc.series) || [];
  const usable = all.filter((s) => (s.points || []).length && s.ok !== false);
  return usable
    .map((s) => reshapeForecastSeries(s, names))
    .sort((a, b) => b.latestObserved - a.latestObserved);
}

/**
 * Load the bound network's TOPOLOGY from the server.
 *
 * Structure is input data — it exists as soon as a network is bound, and it is
 * true whether or not the solver finds a feasible answer. The twin used to
 * take its node list from `/api/kpis/facilities`, which is (correctly) empty
 * when no solve succeeded, so an infeasible network rendered as an empty map
 * with no facilities and no markets even though the snapshot held all of them.
 *
 * It also means a project opened in a fresh session shows its network without
 * needing the upload preview that populated it the first time.
 */
async function loadStructure(projectId) {
  const res = await apiClient.get('/api/network/structure', { project_id: projectId });
  if (!res || !(res.plants || res.dcs || res.markets)) return null;

  // External signals from the upload replace the prototype's own. Mapped to
  // the shape the signals card already renders; fields the workbook does not
  // carry are marked "Not available" rather than filled with demo copy.
  EXTERNAL_SIGNALS.length = 0;
  (res.signals || []).forEach((s) => {
    EXTERNAL_SIGNALS.push({
      id: s.id,
      title: s.description || s.type || s.id,
      source: 'Uploaded external signals',
      publishedDate: s.date || 'Not available',
      effectiveDate: s.date || 'Not available',
      geography: s.marketId || 'Not available',
      direction: 'Not available',
      magnitude: s.probability != null
        ? `Event probability ${(s.probability * 100).toFixed(0)}%`
        : 'Not available',
      confidence: s.relevance || 'Not available',
      rationale: s.description || '',
      // Nothing in this build routes an uploaded signal into a forecast, so
      // the card must not claim it did.
      intendedUse: 'Recorded from your upload — not yet routed into a forecast',
      type: (s.type || 'signal').toLowerCase(),
      icon: '📡',
      color: '#6B2FA0',
    });
  });

  // The money unit and the geographic extent this network states about itself.
  //
  // Set FIRST, before anything renders a figure or draws a map: every
  // `formatCurrency` call downstream reads the active currency, and the map
  // fits the bounds. Both used to be hardcoded — "₹" with lakh grouping, and
  // an India basemap — so a US network was priced in rupees and drawn over
  // the Deccan.
  setActiveCurrency(res.currency || null);
  setNetworkGeography(res.geography || null);

  // The periods the upload actually states, and the calendar length the
  // optimiser prices one at. Set BEFORE `loadNetworkData`, because the seed it
  // writes is keyed by the solved-state key and the screens read the period
  // list as soon as they render.
  setNetworkPeriods(res.periods || [], res.costPeriod || null);

  // The client's own recorded utilisation, per period. This is a MEASUREMENT
  // the upload carried, not a solver output, and it is the only genuine time
  // series the network has — the demand history is collapsed to its latest
  // period before the model ever sees it. The period selector and the insight
  // deep dive's trend chart both read it, and both label it as observed.
  const observed = res.observedUtilisation || {};
  OBSERVED_UTILISATION.periods = res.observedPeriods || observed.periods || [];
  OBSERVED_UTILISATION.points = observed.points || [];
  OBSERVED_UTILISATION.byFacility = {};

  loadNetworkData({
    plants: res.plants || [],
    dcs: res.dcs || [],
    markets: res.markets || [],
    // Carried for their `category`, which is what demand growth is scoped by.
    products: res.products || [],
    lanes: (res.lanes || []).map((l) => ({
      from: l.from,
      to: l.to,
      cost: l.ratePerUnit,
      distance: l.distanceKm,
      leadTime: l.leadTimeDays,
      capacity: l.capacity,
      mode: l.mode,
      // Flow is a solver output and stays absent until one exists.
      flow: null,
    })),
  });
  return res;
}

/**
 * The period key every facility screen reads. The prototype's selector offers
 * several periods of mock history; a solved network is a single point in time,
 * so authoritative values are written to the default period and the others are
 * left untouched rather than back-filled with copies pretending to be history.
 */
// The one key a facility's solved metrics live under. It was the literal
// 'Q3_2026' — a quarter no uploaded workbook has ever contained.
const DEFAULT_PERIOD = SOLVED_STATE_KEY;

/**
 * Pull authoritative KPIs for a project and write them into `data.js`.
 *
 * Returns a small report so callers can tell the user what actually happened
 * rather than assuming success.
 */
/**
 * The stages hydration actually goes through, in order.
 *
 * Exported so the loading screen can list them before they run and mark them
 * off as they finish, rather than animating a bar to a fixed schedule. Each
 * one corresponds to a real await below.
 */
export const HYDRATION_STAGES = [
  ['structure', 'Reading your network'],
  ['solve', 'Solving the network'],
  ['insights', 'Interpreting the result'],
  ['scenarios', 'Loading solved scenarios'],
  ['forecast', 'Building the demand forecast'],
];

/**
 * Insights for one facility, fetched on demand and cached in `HOME_INSIGHTS`.
 *
 * Lazily rather than during hydration: a scoped briefing is a reasoning pass
 * per site, and running one for every facility on a dashboard load would pay
 * for findings nobody has asked to see. Returns the number of findings written,
 * so a caller can decide whether to re-render.
 */
export async function loadFacilityInsights(facilityId, projectId = null) {
  const pid = projectId || getActiveProjectId();
  if (!pid || !facilityId) return 0;
  const response = await insightService.getFacilityInsights(facilityId, pid);
  return response ? applyInsightResponse(response) : 0;
}

export async function hydrateFromBackend(projectId = null, onStage = null) {
  const pid = projectId || getActiveProjectId();
  if (!pid) return { ok: false, reason: 'No active project.' };

  // Report a stage only when it has genuinely started or finished. A caller
  // that passes no callback pays nothing.
  //
  // `executionId` is the orchestrator's own id for the run that produced the
  // response, when the route reports one. It is passed on rather than used
  // here: the loading screen fetches that execution's trace and shows which
  // capabilities actually ran, how many attempts each took and which failed —
  // detail the client cannot know from the response body alone.
  const stage = (id, state, detail, executionId) => {
    if (typeof onStage === 'function') {
      try { onStage(id, state, detail, executionId); } catch (e) { /* never break hydration */ }
    }
  };

  // Topology first: every later step writes onto these arrays, and they must
  // describe the bound network even when the solve produces nothing.
  stage('structure', 'start');
  const structure = await loadStructure(pid).catch(() => null);
  stage('structure', 'done', structure
    ? `${(structure.plants || []).length + (structure.dcs || []).length} facilities, `
      + `${(structure.lanes || []).length} lanes`
    : 'not available');

  // The MILP runs here, on the server, the first time this network version is
  // asked for. It is the long step, and it is the reason the loading screen
  // exists — the dashboard used to be revealed before this resolved, so a user
  // read an empty screen for as long as their network took to solve.
  stage('solve', 'start');
  const [networkRes, facilityRes, flowRes] = await Promise.all([
    kpiService.getNetworkKPIs(pid),
    kpiService.getAllFacilityKPIs(pid).catch(() => null),
    kpiService.getFlowKPIs(pid).catch(() => null),
  ]);
  stage('solve', 'done', `${Object.values((networkRes && networkRes.kpis) || {})
    .filter((r) => r && r.status === 'VALID').length} KPIs computed`,
    networkRes && networkRes.execution_id);

  const k = (networkRes && networkRes.kpis) || {};

  // Record which snapshot these figures came from, so the assistant asks its
  // questions about THIS network rather than the one the orchestrator boots
  // with.
  setActiveSnapshotId(networkRes && networkRes.snapshot_id);

  // When the solve that produced these figures actually ran. The header said
  // "Last refreshed: 5 min ago" as static markup and the refresh button wrote
  // "Just now" without re-fetching anything, so the one thing on the page that
  // claimed to describe freshness was the one thing that never changed.
  if (typeof window !== 'undefined') {
    window.__ngAnalysisComputedAt = (networkRes && networkRes.computed_at) || null;
  }

  // An infeasible solve is a result, not a gap. Surface the solver's own
  // reason so the screens can say why there are no figures instead of simply
  // showing none.
  const infeasible = Object.values(k).some((r) => r && r.status === 'INFEASIBLE');
  const infeasibleReason = infeasible
    ? (Object.values(k).find((r) => r && r.status === 'INFEASIBLE')?.metadata?.reason || '')
    : '';

  // A third state, between "solved" and "no answer": the strict model proved
  // infeasible and the engine returned the best plan that serves as much as
  // the network physically can, with the remainder reported as unserved
  // demand. The figures below are real and every one of them is conditioned on
  // that shortfall, so it travels with them rather than being inferred from a
  // fill rate that happens to be below 100%.
  const relaxedMeta = Object.values(k)
    .find((r) => r && r.metadata && r.metadata.solve_relaxation)?.metadata || null;
  const relaxed = relaxedMeta ? {
    kind: relaxedMeta.solve_relaxation,
    reason: relaxedMeta.relaxation_reason || '',
    unservedDemand: relaxedMeta.unserved_demand ?? null,
    totalDemand: relaxedMeta.total_demand ?? null,
  } : null;

  // ---- Planning horizon ------------------------------------------------
  // What span of time every figure below covers. Applied BEFORE the baseline
  // is written, so nothing can render a cost before the label that says what
  // period it is on exists.
  applyHorizon(networkRes?.horizon);

  // ---- Network baseline ------------------------------------------------
  // Written straight from the solver's own cost breakdown. There is no
  // "optimised" counterpart, because the engine has not been asked to produce
  // one: the previous code invented it as baseline × 0.94.
  const totalCost = val(k.business_network_cost);
  if (totalCost === null) {
    // No solved cost for this network. The baseline must be CLEARED, not left
    // as-is: the prototype ships a demo base case (₹12.8L), and leaving it in
    // place meant a bound-but-unsolved network showed that figure on Home
    // under a "Source: Optimized Base Case (Actual)" label — a number from a
    // network the user had never seen, presented as their own.
    setAuthoritativeBaseline({
      source: 'AUTHORITATIVE_KPI_LAYER',
      snapshotId: networkRes?.snapshot_id,
      executionId: networkRes?.execution_id,
      footprintChange: null,
      baseline: {
        totalCost: null, transportCost: null, fixedCost: null, variableCost: null,
        inventoryCost: null, unmetPenalty: null, slaPenalty: null,
        sla: null, fillRate: null, avgUtilization: null, maxUtilization: null,
        carbonKgCo2e: null, avgDistanceKm: null, capacityHeadroomPct: null,
      },
      optimized: null,
      savings: null,
      unavailableReason: infeasibleReason
        || 'no solved result is available for this network',
    });
  } else {
    setAuthoritativeBaseline({
      source: 'AUTHORITATIVE_KPI_LAYER',
      snapshotId: networkRes.snapshot_id,
      executionId: networkRes.execution_id,
      footprintChange: 'None (as observed in the uploaded network)',
      baseline: {
        totalCost,
        transportCost: val(k.transport_cost),
        fixedCost: val(k.facility_cost),
        variableCost: null,
        inventoryCost: val(k.inventory_cost),
        handlingCost: val(k.handling_cost),
        // NOT `shortage_penalty_cost`. That figure is the solver's own device
        // for deciding which demand to strand when not all of it can be
        // served — at ₹1,000,000/unit it reaches ₹8.7bn on this network — and
        // nobody pays it. It is excluded from `business_network_cost` for the
        // same reason, so putting it in the cost breakdown would show a line
        // item that is not part of the total above it. The shortfall is
        // reported below as a quantity of demand, which is what it is.
        unmetPenalty: null,
        slaPenalty: null,
        totalDemand: val(k.total_demand),
        servedDemand: val(k.served_demand),
        unservedDemand: val(k.unserved_demand),
        sla: val(k.pct_demand_in_sla),
        fillRate: (val(k.demand_fill_rate) !== null
          ? +(val(k.demand_fill_rate) * 100).toFixed(1) : null),
        avgUtilization: val(k.avg_utilization_pct),
        maxUtilization: val(k.max_utilization_pct),
        carbonKgCo2e: val(k.total_carbon_kg),
        avgDistanceKm: val(k.weighted_avg_distance_km),
        capacityHeadroomPct: (val(k.max_utilization_pct) !== null
          ? +(100 - val(k.max_utilization_pct)).toFixed(1) : null),
      },
      // No optimised case and no savings figure: neither exists until an
      // optimisation scenario is actually run.
      optimized: null,
      savings: null,
    });
  }

  // ---- Per-facility ----------------------------------------------------
  const facilities = (facilityRes && facilityRes.facilities) || {};
  const byId = new Map([...PLANTS, ...DCS].map((f) => [f.id, f]));
  const observedByFacility = new Map(
    [...((structure && structure.plants) || []),
     ...((structure && structure.dcs) || [])]
      .filter((n) => n && n.observed)
      .map((n) => [n.id, n.observed]),
  );

  Object.entries(facilities).forEach(([facId, metrics]) => {
    const util = val(metrics.utilization_pct);
    // PER PERIOD, to match `node.capacity`, which comes from the upload's own
    // per-period capacity column. `throughput_units` is the horizon total, so
    // over twelve modelled periods it is twelve times this — and shown beside a
    // one-period capacity it would read as 277% on a site the solver puts at
    // 23%. The per-period figure divides into that capacity to give exactly
    // `utilization_pct`, so every number on the card agrees.
    const throughput = val(metrics.throughput_units_per_period)
      ?? val(metrics.throughput_units);
    const node = byId.get(facId);

    // Keep the topology arrays consistent with the solve, so the map, the 3D
    // twin and the facility tables all show the same utilisation.
    if (node) {
      if (util !== null) node.utilPct = util;
      // Utilisation in the busiest single period of the solved horizon, from
      // the same solve as the average above. `utilPct` has always carried the
      // horizon MEAN, and a mean is the one number that cannot answer what a
      // multi-period model was built to answer: a site at 43% for the year can
      // still be out of room in March. Left unset when the solve reported no
      // peak, so a consumer can tell "no peak available" from "peak equals
      // average" rather than inferring a seasonal profile the data never
      // stated.
      const peak = val(metrics.peak_utilization_pct);
      if (peak !== null) node.peakUtilPct = peak;
      if (throughput !== null) node.throughput = throughput;
      // Whether the solver kept this site open. The facility tables printed a
      // green "Active" tag on every row unconditionally, including sites the
      // optimiser had closed.
      const open = val(metrics.is_open);
      node.isOpen = (open === null) ? null : Boolean(open);
    }

    const existing = FACILITY_KPIS[facId] || {};
    const prior = existing[DEFAULT_PERIOD] || {};
    // The client's own recorded utilisation for this site, from the capacity
    // history in their upload. It is the genuine prior the "vs previous"
    // column was asking for — 288 rows of it were parsed and then reached
    // nothing, leaving that column showing a dash on every row.
    const obs = observedByFacility.get(facId) || null;
    FACILITY_KPIS[facId] = {
      ...existing,
      [DEFAULT_PERIOD]: {
        ...prior,
        utilisation: {
          value: util,
          capacity: node ? node.capacity : (prior.utilisation?.capacity ?? null),
          unit: 'units/period',
          // Utilisation in the busiest single period of the horizon, from the
          // solver. `value` above is the horizon AVERAGE, and a site at 43%
          // for the year can still be at 91% in March — which is the reading
          // that decides whether it needs more room. Equal to `value` on a
          // single-period solve, and null only when the solve reported none.
          peak: val(metrics.peak_utilization_pct),
          // The comparison is against what the client RECORDED, not against a
          // previous solve — there has been none. Still null when the upload
          // carried no capacity history for this facility.
          prev: obs ? obs.utilisationPct : null,
          status: util === null ? 'unknown'
            : util >= 90 ? 'critical' : util >= 75 ? 'warning' : 'normal',
        },
        // SLA and cost are network-level in this engine; there is no
        // per-facility figure to report, and inventing one per facility is
        // exactly what the previous hydration did.
        sla: {
          value: val(k.pct_demand_in_sla),
          // The third and last copy of the invented 95% service target. Home
          // subtracted the solved figure from it and printed the difference as
          // "vs target: -18.6%" — a shortfall against a benchmark the client
          // never set, next to a number that came from their own solve.
          //
          // The uploaded data DOES state a service level: `sla_days` per
          // demand row. `pct_demand_in_sla` is already measured against it, so
          // the figure is its own compliance statement and needs no second
          // target on top.
          target: null,
          prev: null,
          // 'normal' was asserted for every network, including one serving
          // 76% of its demand.
          status: (() => {
            const v = val(k.pct_demand_in_sla);
            if (v === null) return 'unknown';
            return v >= 99 ? 'normal' : v >= 90 ? 'warning' : 'critical';
          })(),
        },
        totalCost: { value: null, prev: null, status: 'unknown' },
        inventoryDays: { value: null, prev: null, status: 'unknown' },
        prevLabel: obs && obs.period
          ? `recorded ${obs.period}` : 'no prior solve',
      },
    };

    // A solved entry for each period the model actually covered, keyed by the
    // month the upload named it.
    //
    // Without these, selecting a period resolved to the same horizon-average
    // entry whichever month was chosen — the control moved and nothing behind
    // it did. Utilisation here is the solver's own per-period figure, not this
    // month's throughput divided by something in the browser.
    const series = (networkRes?.horizon?.by_facility || {})[facId];
    const labels = SOLVE_HORIZON.periodLabels || {};
    if (series && series.utilisation) {
      Object.entries(series.utilisation).forEach(([index, pct]) => {
        const label = labels[index];
        if (!label || typeof pct !== 'number' || !Number.isFinite(pct)) return;
        const base = FACILITY_KPIS[facId][SOLVED_STATE_KEY];
        FACILITY_KPIS[facId][label] = {
          ...base,
          utilisation: {
            ...base.utilisation,
            value: +pct.toFixed(1),
            // The horizon average, kept beside the period's own reading so the
            // two are distinguishable rather than one silently replacing the
            // other.
            horizonAverage: util,
            throughput: series.throughput ? series.throughput[index] ?? null : null,
            prev: null,
            status: pct >= 90 ? 'critical' : pct >= 75 ? 'warning' : 'normal',
          },
          prevLabel: `solved ${label}`,
        };
      });
    }
  });

  // ---- Lane flows ------------------------------------------------------
  // How much the solver actually moves down each lane, and what that costs.
  // `LANES[].flow` held the prototype's own corridor volumes until now, and the
  // per-facility lane tables showed a null volume beside a real freight rate.
  const flows = (flowRes && flowRes.flows) || [];
  const flowByLane = new Map(
    flows.map((f) => [`${f.origin_id}->${f.destination_id}`, f]),
  );
  LANES.forEach((lane) => {
    const f = flowByLane.get(`${lane.from}->${lane.to}`);
    // Explicitly null, not zero, when this lane carries no solved flow: an
    // unused lane and an unsolved one look identical at zero.
    //
    // PER PERIOD, because a corridor's volume is read beside its per-unit rate
    // and its stated lane capacity, both of which are per period. The horizon
    // total stays available as `flowHorizon` for anything that wants the whole
    // span rather than a period of it.
    lane.flow = f ? (f.flow_units_per_period ?? f.flow_units) : null;
    lane.flowHorizon = f ? f.flow_units : null;
    lane.transportCost = f ? f.transport_cost : null;
    lane.carbonKg = f ? f.carbon_kg : null;
  });

  Object.values(FACILITY_KPIS).forEach((periods) => {
    Object.values(periods || {}).forEach((period) => {
      (period && period.laneFlows || []).forEach((row) => {
        const [from, to] = String(row.lane).split(' → ');
        const f = flowByLane.get(`${from}->${to}`);
        if (f) {
          row.volume = f.flow_units_per_period ?? f.flow_units;
          row.cost = f.transport_cost;
        }
      });
    });
  });

  // ---- Demo narrative --------------------------------------------------
  // Anything written about the prototype's own footprint is dropped, so the
  // user is never shown an insight about a facility they do not operate.
  clearDemoNarrative(Object.keys(facilities));

  // ---- Insights --------------------------------------------------------
  // The Reasoning Agent's findings about THIS network, fetched after the solve
  // because they explain its result.
  //
  // This step did not exist. `HOME_INSIGHTS` was written by nothing, so the
  // Home attention feed rendered its empty state on every uploaded network
  // regardless of what the network said — while the engine had six grounded
  // findings ready and an endpoint to serve them.
  //
  // Network scope only, here. A briefing per facility would be one reasoning
  // pass per site on every dashboard load; the facility ones are fetched when a
  // facility is actually selected (see `loadFacilityInsights`).
  stage('insights', 'start');
  let insightCount = 0;
  const insightResponse = await insightService.getNetworkInsights(pid);
  if (insightResponse) {
    insightCount = applyInsightResponse(insightResponse);
  }
  stage('insights', 'done', insightCount
    ? `${insightCount} finding${insightCount === 1 ? '' : 's'}`
    : 'no findings',
    insightResponse && insightResponse.execution_id);

  // ---- Scenarios -------------------------------------------------------
  // Only scenarios actually solved for this project. The two fabricated
  // "canonical" scenarios the prototype shipped with are gone.
  stage('scenarios', 'start');
  try {
    const solved = await scenarioService.listScenarios(pid);
    SCENARIOS.length = 0;

    // The baseline goes in FIRST, as `SCN_ACTUAL`.
    //
    // There was no baseline row at all. The comparison table did
    // `SCENARIOS.find(s => s.id === 'SCN_ACTUAL') || SCENARIOS[0]`, so the
    // column headed "Current Baseline" showed the first user scenario — the
    // first scenario was compared against itself and the second against the
    // first. The map's Baseline toggle never rendered for the same reason.
    const baselineRow = baselineFromNetworkKPIs({
      kpis: k,
      facilities,
      flows,
      snapshotId: networkRes?.snapshot_id,
      executionId: networkRes?.execution_id,
      projectId: pid,
    });
    if (baselineRow) SCENARIOS.push(baselineRow);

    // Through the SAME mapper the create flow uses. These were pushed raw, so
    // a scenario that arrived by listing carried the backend's snake_case
    // shape (`scenario_kpis`, `baseline_kpis`) while one created in-session
    // carried the mapped shape (`totalCost`, `maxUtil`). The comparison table
    // reads the mapped names, so every scenario rendered blank after a reload.
    solved.forEach((s) => {
      const mapped = mapScenarioRecord(s);
      if (mapped) {
        mapped.num = SCENARIOS.length;
        SCENARIOS.push(mapped);
      }
    });
  } catch (e) {
    SCENARIOS.length = 0;
  }
  stage('scenarios', 'done', `${Math.max(0, SCENARIOS.length - 1)} scenario(s)`);

  // ---- Forecast --------------------------------------------------------
  // The forecast screen was never connected to the forecasting engine: it drew
  // 24 months of prototype "North India" demand and a hardcoded cone from
  // data.js whatever network was loaded. The engine, its quantile bands and
  // its rolling-origin accuracy metrics were all real and simply unread.
  let forecastReport = { status: 'NOT_REQUESTED', series: 0 };
  stage('forecast', 'start');
  try {
    const fc = await forecastService.getForecast(pid, 6);
    // Market and product NAMES, so a series reads as "New York Metro ·
    // Sparkling Water" rather than "M002/P001".
    const names = {
      markets: Object.fromEntries(
        ((structure && structure.markets) || []).map((m) => [m.id, m.name])),
      products: Object.fromEntries(
        ((structure && structure.products) || []).map((p2) => [p2.id, p2.name])),
    };
    const allSeries = reshapeAllForecastSeries(fc, names);
    const chosen = allSeries[0] || null;
    if (chosen) {
      forecastReport = {
        status: fc.status || 'OK',
        series: (fc.series || []).length,
        shown: chosen.seriesLabel,
        engine: chosen.engine,
        accuracy: chosen.accuracy || null,
        // The orchestrator's id for the run that produced this forecast, so
        // the loading screen can read its trace.
        executionId: fc.execution_id || null,
      };
      publishForecastMeta(forecastReport);
      // Every forecastable series is handed to the screens; the largest is
      // selected. A selector can now reach the rest.
      setForecastCatalogue(allSeries);
      setForecastSeries(chosen);
    } else {
      forecastReport = {
        status: fc.status || 'NO_SERIES', series: 0,
        reason: fc.message || '',
      };
      publishForecastMeta(forecastReport);
      setForecastCatalogue([]);
      setForecastSeries({});
    }
  } catch (e) {
    // An unavailable forecast is a real answer; the chart renders its own
    // empty state and says why rather than drawing a fabricated cone.
    forecastReport = { status: 'UNAVAILABLE', series: 0, reason: e?.message || '' };
    publishForecastMeta(forecastReport);
    setForecastCatalogue([]);
    setForecastSeries({});
  }
  stage('forecast', 'done', forecastReport.series
    ? `${forecastReport.series} series` : String(forecastReport.status).toLowerCase(),
    forecastReport.executionId);

  // ---- Model governance metadata --------------------------------------
  // What the Settings screen reports. Written from the run that produced the
  // figures above, replacing a hardcoded block that described 42 facilities,
  // 380 lanes and an ARIMA forecast for every project ever opened.
  //
  // Last, because it summarises everything the stages before it loaded.
  applySystemStatus({
    network: {
      observedPeriods: (structure && structure.observedPeriods) || [],
      qualityPct: null,
    },
    analysis: networkRes || null,
    forecast: {
      model: forecastReport.engine || null,
      horizon: forecastReport.series ? SOLVE_HORIZON.periodsModelled : null,
      generatedAt: (networkRes && networkRes.computed_at)
        ? new Date(networkRes.computed_at * 1000).toISOString() : null,
    },
  });

  if (typeof window !== 'undefined') {
    // A read-only snapshot of what hydration actually wrote, so a validation
    // run can tell an empty field on screen apart from an empty field in the
    // model behind it. Reads nothing the screens do not already read.
    window.__ngModelProbe = () => ({
      baselineCost: getOptimizedBaseCase()?.baseline?.totalCost ?? null,
      fixedCost: getOptimizedBaseCase()?.baseline?.fixedCost ?? null,
      transportCost: getOptimizedBaseCase()?.baseline?.transportCost ?? null,
      inventoryCost: getOptimizedBaseCase()?.baseline?.inventoryCost ?? null,
      fillRate: getOptimizedBaseCase()?.baseline?.fillRate ?? null,
      unservedDemand: getOptimizedBaseCase()?.baseline?.unservedDemand ?? null,
      lanesWithFlow: LANES.filter((l) => l.flow !== null && l.flow !== undefined).length,
      lanesTotal: LANES.length,
      relaxed: relaxed ? relaxed.kind : null,
    });
    window.dispatchEvent(new CustomEvent('authoritativeDataLoaded', {
      detail: { projectId: pid, snapshotId: networkRes?.snapshot_id },
    }));
    // Re-render whichever screens are mounted. The facility selectors are
    // rebuilt too, so they offer the facilities in THIS network.
    if (typeof window.initHomeSelectors === 'function') window.initHomeSelectors();
    if (typeof window.renderHome === 'function') window.renderHome();
    if (typeof window.renderTwinTables === 'function') window.renderTwinTables();
  }

  const validCount = Object.values(k).filter((r) => r.status === 'VALID').length;
  return {
    ok: true,
    projectId: pid,
    snapshotId: networkRes?.snapshot_id,
    kpisValid: validCount,
    kpisTotal: Object.keys(k).length,
    facilities: Object.keys(facilities).length,
    // Topology is reported separately from solved metrics, because a network
    // can be fully loaded and still have no feasible answer.
    nodes: structure
      ? (structure.plants || []).length + (structure.dcs || []).length
        + (structure.markets || []).length
      : 0,
    infeasible,
    infeasibleReason,
    relaxed,
  };
}
