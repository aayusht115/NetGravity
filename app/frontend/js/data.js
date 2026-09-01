/**
 * NetGravity — Client-Side Model
 * ==============================
 * The structures every screen reads. They are DECLARATIONS, not data: each one
 * starts empty and is filled by `loadNetworkData()` and `hydrateFromBackend()`
 * from the network bound to the open project.
 *
 * They used to ship populated with the prototype's own network — Baddi Plant,
 * DC Delhi NCR, MKT_LUCKNOW, two solved scenarios, a costed recommendation and
 * a full per-facility performance profile. Because the screens read these
 * arrays directly, a user who had opened a project and uploaded nothing was
 * shown a complete, confident dashboard of a network they had never seen:
 * ₹12.8L total cost, 94% utilisation, 89.5% fill rate, and a 7.8% savings
 * opportunity. The banner above it said "no network yet".
 *
 * Nothing in this file may describe a network. Where the shape of a record
 * matters it is documented in a comment, not demonstrated with a fake row.
 */

// ─── FACILITIES ─────────────────────────────────────────────
// Filled by `loadNetworkData()` from the bound network. EMPTY until then:
// this shipped holding the prototype's own network, and every screen reads
// it directly, so a user who had not uploaded anything was shown a full
// dashboard of somebody else's facilities under the label "Actual".
export const PLANTS = [];

export const DCS = [];

export const MARKETS = [];

// ─── S2: de-overlap co-located nodes ──────────────────────────
// Several plants/DCs/markets share a city and were given the exact same
// lat/lng (e.g. Kolkata Plant, Kolkata DC and the Kolkata market all sit at
// 22.57,88.36), which made them render as fully stacked, indistinguishable
// icons on both the 2D Leaflet map and the 3D twin — neither has any
// de-collision logic of its own. Fixed once here, at the shared data
// source, so every consumer (map.js, twin3d.js, lane endpoints, FACILITIES)
// sees already-separated coordinates. This is a schematic nudge for
// legibility, not a claim about real inter-facility distance.
function deoverlapNodes(nodeArrays) {
  const groups = new Map();
  nodeArrays.flat().forEach((n) => {
    const key = `${n.lat.toFixed(4)},${n.lng.toFixed(4)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(n);
  });
  const FAN_RADIUS = 0.9; // degrees — visibly separates icons on the India-wide view
  groups.forEach((nodes) => {
    if (nodes.length < 2) return;
    nodes.forEach((n, i) => {
      const angle = (2 * Math.PI * i) / nodes.length;
      n.lat += FAN_RADIUS * Math.sin(angle);
      n.lng += FAN_RADIUS * Math.cos(angle);
    });
  });
}
deoverlapNodes([PLANTS, DCS, MARKETS]);

export const FACILITIES = [];

// ─── LANES (key corridors with cost, distance, lead time) ───
export const LANES = [];

// ─── DEMAND HISTORY (24 months) & FORECAST ──────────────────
// Observed demand, written by `setForecastSeries()` from the forecasting
// engine's own history. `northIndia` is the prototype's name for the
// plotted series; it holds whichever market-product pair the engine
// returned for THIS network.
export const DEMAND_HISTORY = {
  months: [],
  northIndia: [],
  baddiCapacity: null,
};

// Produced forecast, written by `setForecastSeries()`.
export const FORECAST = {
  months: [],
  northIndia: [],
  upper: [],
  lower: [],
  seriesLabel: '',
  growthRate: null,
  breachMonth: null,
  breachFacility: null,
  breachProjectedUtil: null,
};

// ─── EXTERNAL SIGNALS ───────────────────────────────────────
// Signals that arrived with the upload, mapped in by `loadStructure()`.
// Held the prototype's own ('North India GDP Growth Accelerating', RBI
// Quarterly Bulletin), shown for every network.
export const EXTERNAL_SIGNALS = [];

// ─── DATA QUALITY ───────────────────────────────────────────
// Data quality, measured by the parser on the uploaded file and written here
// by ingestion.js. It starts EMPTY on purpose: this object used to ship a demo
// dataset — 4,820 records, 98.4% valid, and eight issues naming the
// prototype's own DC_GUWAHATI, MKT_LUCKNOW and PLT_BADDI→DC_GUWAHATI — which
// the ingestion screen rendered under the heading "Can this data be trusted to
// run the model?" for whatever file the user had just uploaded. A file that
// fails to parse must show nothing here, not somebody else's clean bill of
// health.
export const DATA_QUALITY = {
  totalRecords: 0,
  validRecords: 0,
  validPct: null,
  nullCellPct: null,
  duplicateRows: null,
  emptyRows: null,
  issues: [],
};

// ─── CONTRACT EXTRACTION DEMO ───────────────────────────────
export const CONTRACT_DEMO = {
  vendorA: {
    name: "TransCorp Logistics",
    headlineRate: "₹10/kg",
    extractedTerms: [
      { field: "Base Rate", value: "₹10/kg", confidence: "HIGH" },
      { field: "Fuel Surcharge", value: "₹2/kg", confidence: "HIGH" },
      { field: "Non-Serviceable Surcharge", value: "₹5/kg for 12 pin codes in NE India", confidence: "MEDIUM" },
      { field: "Minimum Volume", value: "500 kg/shipment", confidence: "HIGH" },
      { field: "Penalty (Late Pickup)", value: "₹500/incident", confidence: "MEDIUM" },
      { field: "Effective Date", value: "2026-04-01", confidence: "HIGH" },
    ],
    effectiveCost: "₹12–17/kg (depending on route)",
    hiddenCostAlert: true,
  },
  vendorB: {
    name: "SpeedFreight India",
    headlineRate: "₹12/kg",
    extractedTerms: [
      { field: "Base Rate", value: "₹12/kg (all-inclusive)", confidence: "HIGH" },
      { field: "Fuel Surcharge", value: "Included", confidence: "HIGH" },
      { field: "Coverage", value: "All pin codes", confidence: "HIGH" },
      { field: "Minimum Volume", value: "200 kg/shipment", confidence: "HIGH" },
      { field: "Effective Date", value: "2026-04-01", confidence: "HIGH" },
    ],
    effectiveCost: "₹12/kg (flat)",
    hiddenCostAlert: false,
  },
};

// ─── SCHEMA MAPPING DEMO ────────────────────────────────────
export const SCHEMA_MAPPING = {
  distributors: [
    { name: "Distributor A", field: "Qty", mappedTo: "Demand_Units", confidence: 87 },
    { name: "Distributor B", field: "Volume", mappedTo: "Demand_Units", confidence: 91 },
    { name: "Distributor C", field: "Units Shipped", mappedTo: "Demand_Units", confidence: 95 },
    { name: "Distributor D", field: "Order_Qty", mappedTo: "Demand_Units", confidence: 98 },
  ],
  canonicalField: "Demand_Units",
  sourceTypes: ["ERP", "WMS", "TMS", "Facility Master", "Demand Data", "Lane Data"],
};

// ─── SCENARIO RESULTS (Canonical MILP outputs for Decision Cockpit) ──────
// Only scenarios actually solved for the open project, written by
// `hydrateFromBackend()`. The hand-authored ones that shipped here
// carried full cost/SLA/utilisation figures no solver produced.
export const SCENARIOS = [];

// ─── SCENARIO COMPARISON INSIGHTS (Deterministic AI Assessments) ─
export const SCENARIO_COMPARISON_INSIGHTS = [];

// ─── MULTI-SCENARIO ACTION ITEMS ────────────────────────────
export const SCENARIO_COMPARISON_ACTIONS = [];


// ─── AI AGENT STATE ─────────────────────────────────────────
// The orchestrator's own trace for this project. Shipped with a
// seven-step narrative about the prototype's footprint.
export const AGENT_STATE = {
  status: 'idle',
  currentObjective: 'No objective set for this network yet.',
  activityTrace: [],
  toolCalls: [],
};

// ─── RECOMMENDATION ─────────────────────────────────────────
// Produced by the reasoning layer for the open project. Shipped naming a
// specific intervention ('Rebalance 12% of Baddi volume...') with a
// -7.8% cost impact, offered as a recommendation for any network.
export const RECOMMENDATION = {
  title: 'No recommendation has been generated for this network yet.',
  scenarioId: null,
  tier: null,
  impact: {},
  evidence: {},
};

// ─── GOVERNANCE TIERS ───────────────────────────────────────
export const GOVERNANCE_TIERS = [
  { tier: 1, label: "INFORM", description: "Low-risk informational insight. No approval required.", criteria: "Value at stake < ₹5L, fully reversible, no SLA impact", color: "#22c55e" },
  { tier: 2, label: "PROPOSE", description: "AI recommends and prepares action. Human approval required.", criteria: "Value at stake ₹5L–₹50L, or SLA impact, or partial reversibility", color: "#f59e0b" },
  { tier: 3, label: "HUMAN DECISION", description: "High-impact structural decision. AI analyses, cannot execute.", criteria: "Close/open DC, major contract change, CapEx > ₹50L", color: "#dc2626" },
];

// ─── SYSTEM STATUS ──────────────────────────────────────────
export const SYSTEM_STATUS = {
  data: { facilities: 42, demandZones: 120, lanes: 380, historicalPeriods: 24, qualityPct: 98.4 },
  forecast: { model: "Enhanced Demand Forecast (ARIMA + external signals)", horizon: "6 months", lastUpdated: "2026-08-15" },
  optimisation: { solver: "PuLP / HiGHS (MILP)", status: "Optimal", lastRun: "2026-08-18T14:22:00" },
  ai: { agentStatus: "Active", model: "Configured LLM Provider", lastAction: "Stress-tested leading option" },
};


// ─── PERIODS ────────────────────────────────────────────────
// The periods THIS network's demand rows are stated for.
//
// Four fixed quarters were hardcoded here — "Q3 2026", "Q2 2026", "Q1 2026",
// "Q4 2025" — and offered in the period selector on Home and in the KPI
// screens for every network ever uploaded. No uploaded workbook has ever
// contained them: the demand rows in the client's own data are stated for
// period 1. Selecting "Q2 2026" therefore filtered on a period that did not
// exist and showed the same figures as "Q3 2026", because both were labels
// over one set of numbers.
//
// Filled by `setNetworkPeriods()` from `/api/network/structure`, which reports
// the distinct `period` values the upload actually carried. Empty until a
// network is bound, and the selector then says so instead of offering a
// choice that is not real.
export const PERIODS = [];

/**
 * The calendar length the optimiser prices one period at, as the engine
 * reports it — "MONTH" unless configured otherwise.
 *
 * Every capacity, throughput and flow figure on screen was labelled
 * "units/day". They are `*_units_per_period` values from a model whose cost
 * period is a month, so the label overstated the rate by roughly thirty times
 * on the client's own numbers.
 */
export const PERIOD_META = { costPeriod: null };

/** How to label a per-period quantity, in the engine's own terms. */
export function perPeriodLabel() {
  const p = PERIOD_META.costPeriod;
  if (p === 'DAY') return 'units/day';
  if (p === 'MONTH') return 'units/month';
  if (p === 'QUARTER') return 'units/quarter';
  if (p === 'YEAR') return 'units/year';
  return 'units/period';
}

/** Replace the period list with the ones the bound network states. */
export function setNetworkPeriods(periods, costPeriod) {
  PERIODS.length = 0;
  (periods || []).forEach((p, i) => {
    const id = String(p);
    PERIODS.push({
      id,
      label: `Period ${id}`,
      short: `P${id}`,
      // Comparison against a prior period is only offered where the data
      // actually has one.
      prevId: i > 0 ? String(periods[i - 1]) : null,
    });
  });
  PERIOD_META.costPeriod = costPeriod || null;
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('networkPeriodsChanged', {
      detail: { periods: PERIODS.map((p) => p.id), costPeriod },
    }));
  }
}

// ─── FACILITY KPIs BY PERIOD ────────────────────────────────
// Each facility + period combination has its own KPI snapshot.
// "prev" values are for the comparison period.
// Per-facility solved metrics, written by `hydrateFromBackend()` from
// `/api/kpis/facilities`. Shipped with a full performance profile for
// each of the prototype's DCs.
export const FACILITY_KPIS = {};

// ─── INSIGHTS ────────────────────────────────────────────────
// Per-facility insight feed, keyed by facility id, written by
// `hydrateFromBackend()` from `/api/insights?scope=FACILITY`.
//
// Nothing wrote this. It was initialised empty, read by the Home feed and the
// deep dive, and only ever CLEARED — so on any uploaded network the feed said
// "No insights have been generated for this network yet" permanently, on a
// network that had been fully solved and about which the Reasoning Agent had
// six grounded things to say. The endpoint existed, the service wrapper
// existed, and no line of code connected them.
export const HOME_INSIGHTS = {};

// Network-wide insights — the ones that are about the network rather than
// about one site. Held separately from HOME_INSIGHTS because they are not
// keyed by a facility, and folding them in under a pretend facility id would
// make `getInsightsForFacility()` answer with things that are not about that
// facility.
export const NETWORK_INSIGHTS = [];

// What the engine recommends doing next about this network: one string chosen
// by the evidence, with the reasoning that produced it.
export const NETWORK_RECOMMENDATION = {
  text: '',
  keyDrivers: [],
  limitation: '',
  suggestedQuestions: [],
  evidenceCompleteness: '',
  groundingStatus: '',
  stateId: '',
  computedAt: null,
  // The configured policy thresholds, from the module that owns them. A chart
  // draws its threshold line at `utilization_over_pct` rather than at a 90
  // written into the chart code — a second copy of a policy constant is a
  // second definition, and it drifts.
  thresholds: {},
  // Whole-network breakdowns the solve produced (currently `cost_components`,
  // ranked, zero components dropped). Empty when the solve produced none.
  series: {},
};

/**
 * Recorded utilisation per period, from the client's own capacity history.
 *
 * The one genuine time series an uploaded network carries, and NOT a solver
 * output: `used` and `available` are two columns the client supplied on the
 * same row. Kept separate from every solved figure for that reason — a chart
 * that mixed "what the plan does" with "what the sites did" would be plotting
 * two different quantities on one axis.
 *
 * `points` is `[{period, available, used, utilisationPct, facilities}]`, in
 * period order, with `utilisationPct: null` wherever the two figures cannot
 * form a ratio — a gap in the line, never a zero.
 */
export const OBSERVED_UTILISATION = { periods: [], points: [], byFacility: {} };

// ─── HOME ACTION ITEMS ───────────────────────────────────────
export const HOME_ACTION_ITEMS = [];


/**
 * Drop demo narrative content that describes a DIFFERENT network.
 *
 * The prototype ships with insights, recommendations, an agent trace and a
 * forecast written about its own demo footprint ("close Guwahati", "Delhi NCR
 * DC at 108%"). Once a user's own network is loaded, none of that is true of
 * their data — and a confident narrative about facilities they do not operate
 * is exactly the kind of fabrication this application must not produce.
 *
 * Insights keyed to a facility that still exists are kept; everything else is
 * cleared, and the screens fall back to their own empty states until the
 * reasoning layer produces real ones.
 */
/**
 * Empty every structure that describes a network.
 *
 * Called when a project has no bound network, and when switching projects.
 * The arrays now start empty, so this matters on the SECOND project a user
 * opens: without it, leaving an analysed project for an unanalysed one left
 * the first one's facilities, corridors and KPIs on screen under the second
 * one's name.
 *
 * Deliberately total — topology, solved metrics, scenarios, signals, forecast
 * and narrative. A partial reset is how a screen ends up mixing two networks.
 */
export function clearNetworkModel() {
  PLANTS.length = 0;
  DCS.length = 0;
  MARKETS.length = 0;
  FACILITIES.length = 0;
  LANES.length = 0;
  SCENARIOS.length = 0;
  EXTERNAL_SIGNALS.length = 0;

  Object.keys(FACILITY_KPIS).forEach((k) => delete FACILITY_KPIS[k]);

  // Narrative, agent trace, recommendation, forecast and history.
  clearDemoNarrative([]);

  // The base case reverts to the all-null one until a solve installs another.
  setAuthoritativeBaseline(null);

  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('networkModelCleared'));
  }
}

export function clearDemoNarrative(keepFacilityIds = []) {
  const keep = new Set(keepFacilityIds);
  Object.keys(HOME_INSIGHTS).forEach((facId) => {
    if (!keep.has(facId)) delete HOME_INSIGHTS[facId];
  });

  HOME_ACTION_ITEMS.length = 0;
  NETWORK_INSIGHTS.length = 0;
  Object.assign(NETWORK_RECOMMENDATION, {
    text: '', keyDrivers: [], limitation: '', suggestedQuestions: [],
    evidenceCompleteness: '', groundingStatus: '', stateId: '', computedAt: null,
  });
  SCENARIO_COMPARISON_INSIGHTS.length = 0;
  SCENARIO_COMPARISON_ACTIONS.length = 0;

  AGENT_STATE.activityTrace = [];
  AGENT_STATE.currentObjective = 'No objective set for this network yet.';
  AGENT_STATE.status = 'idle';

  // Reset every narrative field on RECOMMENDATION, not just the obvious ones —
  // the demo copy names specific facilities in its title, evidence, analyst
  // email and rejected-options list.
  Object.keys(RECOMMENDATION).forEach((key) => {
    const v = RECOMMENDATION[key];
    if (Array.isArray(v)) RECOMMENDATION[key] = [];
    else if (typeof v === 'string') RECOMMENDATION[key] = '';
    else if (v && typeof v === 'object') RECOMMENDATION[key] = {};
  });
  RECOMMENDATION.title = 'No recommendation has been generated for this network yet.';

  // Forecast and history come from the forecasting engine per project; the
  // demo series describes another network's demand.
  DEMAND_HISTORY.months = [];
  DEMAND_HISTORY.northIndia = [];
  FORECAST.months = [];
  FORECAST.northIndia = [];
  FORECAST.upper = [];
  FORECAST.lower = [];
}

/**
 * Write an observed history and a produced forecast into the structures the
 * forecast screen reads.
 *
 * The screen was never connected to the forecasting engine. It rendered
 * `DEMAND_HISTORY`/`FORECAST` — 24 months of prototype demand for "North
 * India" and a 6-month cone, both literals in this file — regardless of which
 * network was loaded. Once `clearDemoNarrative()` emptied them the chart threw
 * `RangeError: Invalid array length`, because it builds padding with
 * `new Array(months.length - 1)` and that is -1 for an empty series.
 *
 * `history` and `forecast` are `{ labels: string[], values: number[] }`;
 * `upper`/`lower` are the p90/p10 bands, absent when the engine reports none.
 */
export function setForecastSeries({ history, forecast, capacityLine = null,
                                    seriesLabel = '' } = {}) {
  DEMAND_HISTORY.months = (history && history.labels) || [];
  DEMAND_HISTORY.northIndia = (history && history.values) || [];
  // The dashed "capacity threshold" line was the prototype's own Baddi DC
  // capacity (10,000 u/d), drawn over every network. It is a real threshold
  // only when the caller supplies one for THIS series; otherwise the chart
  // omits it rather than drawing another network's limit.
  DEMAND_HISTORY.baddiCapacity = capacityLine;

  FORECAST.months = (forecast && forecast.labels) || [];
  FORECAST.northIndia = (forecast && forecast.values) || [];
  FORECAST.upper = (forecast && forecast.upper) || [];
  FORECAST.lower = (forecast && forecast.lower) || [];
  FORECAST.seriesLabel = seriesLabel;

  // Growth rate and the capacity-breach fields describe the demo network and
  // have no counterpart here unless the engine produced one.
  FORECAST.growthRate = null;
  FORECAST.breachMonth = null;
  FORECAST.breachFacility = null;
  FORECAST.breachProjectedUtil = null;

  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('forecastSeriesLoaded', {
      detail: { periods: DEMAND_HISTORY.months.length,
                horizon: FORECAST.months.length },
    }));
  }
}

export function hasForecastSeries() {
  return DEMAND_HISTORY.months.length > 1 && FORECAST.months.length > 0;
}

// Both accessors used to fall back to `DC_DELHI` — a prototype facility — so
// asking for a facility that is not in the loaded network returned another
// network's insights and KPIs rather than nothing. An absent facility now
// yields an absent answer, and the screens render their empty states.
export function getInsightsForFacility(facilityId) {
  return HOME_INSIGHTS[facilityId] || [];
}

/** Insights about the network as a whole, in the order the engine ranked them. */
export function getNetworkInsights() {
  return NETWORK_INSIGHTS.slice();
}

/**
 * The severity → attention-category map.
 *
 * The severity comes from the engine (`InsightSeverity`), so a card's colour,
 * icon and position follow what was actually found. The theme refines it
 * within a severity: a capacity risk and a service risk are both RISK but a
 * planner reads them differently.
 *
 * This replaces keyword-matching the narrative for the strings "high impact",
 * "opportunity" and "positive" — which meant an insight's rendering depended
 * on incidental wording, and any finding phrased another way was shown as a
 * neutral "Status" however serious it was.
 */
const _THEME_CATEGORY = {
  Capacity: 'Capacity Risk',
  Service: 'Service Risk',
  Resilience: 'Service Risk',
  Utilisation: 'Network Opportunity',
  Footprint: 'Network Opportunity',
  'Scenario impact': 'Recommendation',
};

export function insightCategory(insight) {
  if (!insight) return 'Status';
  const byTheme = _THEME_CATEGORY[insight.theme];
  if (insight.severity === 'RISK') return byTheme || 'Capacity Risk';
  if (insight.severity === 'OPPORTUNITY') return byTheme || 'Network Opportunity';
  // INFORMATION: a fact worth stating that asks for no decision. Cost, cost
  // structure and carbon land here, and a served-in-full service note does too
  // — which is a genuine "Performance Update", not a risk.
  if (insight.theme === 'Service') return 'Performance Update';
  return 'Status';
}

/**
 * Turn one `/api/insights` record into a feed record.
 *
 * The subtitle is the narrative's FIRST SENTENCE rather than a summary written
 * here: the full narrative is what the deep dive shows, and re-describing it
 * in the card would be a second, unverified account of the same finding.
 */
export function toInsightRecord(apiInsight) {
  const narrative = String(apiInsight.narrative || '');
  const firstSentence = (narrative.match(/^[^.!?]*[.!?]/) || [narrative])[0].trim();
  return {
    id: apiInsight.id,
    title: apiInsight.headline || '',
    subtitle: firstSentence,
    narrative,
    theme: apiInsight.theme || '',
    severity: apiInsight.severity || 'INFORMATION',
    category: insightCategory(apiInsight),
    metricRefs: apiInsight.metric_refs || [],
    // The figures the finding rests on, each with a label, a formatted value,
    // the engine that computed it and — since this phase — the raw number.
    //
    // This line is the whole reason the deep dive's Evidence table had never
    // rendered a single row in production. `/api/insights` has always resolved
    // evidence for thirteen of the fourteen insight themes, and dropping it
    // here left `record.evidence` undefined, so `evidenceCardHtml` fell to its
    // "this finding cites no single figure" copy on EVERY insight — a false
    // statement about findings that cite one.
    evidence: apiInsight.evidence || [],
    // The facilities or lanes the finding was computed over, ranked by the
    // metric its theme is about. This is what a chart plots; `evidence` is what
    // the table lists.
    entities: apiInsight.entities || [],
    rank: apiInsight.rank || 0,
  };
}

/**
 * Write a `/api/insights` response into the stores the screens read.
 *
 * `scope` decides where it lands: a NETWORK response replaces the network
 * insights and the recommendation; a FACILITY response replaces that
 * facility's entry. Replaces rather than merges, so a re-solve cannot leave a
 * finding on screen that the new solve no longer supports.
 */
export function applyInsightResponse(response) {
  if (!response || !Array.isArray(response.insights)) return 0;
  const records = response.insights.map(toInsightRecord);

  if (response.scope === 'FACILITY' && response.entity_id) {
    HOME_INSIGHTS[response.entity_id] = records;
  } else if (response.scope === 'NETWORK') {
    NETWORK_INSIGHTS.length = 0;
    records.forEach((r) => NETWORK_INSIGHTS.push(r));
    Object.assign(NETWORK_RECOMMENDATION, {
      text: response.recommendation || '',
      keyDrivers: response.key_drivers || [],
      limitation: response.limitation || '',
      suggestedQuestions: response.suggested_questions || [],
      evidenceCompleteness: response.evidence_completeness || '',
      groundingStatus: (response.grounding && response.grounding.status) || '',
      stateId: response.state_id || '',
      computedAt: response.computed_at || null,
      thresholds: response.thresholds || {},
      series: response.series || {},
    });
  }

  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('insightsLoaded', {
      detail: { scope: response.scope, entityId: response.entity_id || null,
                count: records.length },
    }));
  }
  return records.length;
}

/**
 * The one key facility KPIs are stored under.
 *
 * They used to be keyed by the literal string `'Q3_2026'` in three separate
 * files. There is one solved state per network — the MILP aggregates every
 * demand row into a single period — so a per-period store was always a store
 * of one entry under a label that came from nowhere.
 */
export const SOLVED_STATE_KEY = 'CURRENT';

export function getKpisForFacility(facilityId, periodId) {
  const facilityKpis = FACILITY_KPIS[facilityId];
  if (!facilityKpis) return null;
  return facilityKpis[SOLVED_STATE_KEY]
    || facilityKpis[periodId]
    || facilityKpis[Object.keys(facilityKpis)[0]]
    || null;
}

// ─── HELPER FUNCTIONS ───────────────────────────────────────
export function getFacilityById(id) {
  return [...PLANTS, ...DCS, ...MARKETS].find(f => f.id === id);
}

/**
 * What kind of node this is: 'PLANT' | 'DC' | 'MARKET' | null.
 *
 * Read from the arrays the network was loaded into, NOT from the id's spelling.
 * The screens tested `id.startsWith('DC_')` / `startsWith('PLT_')`, which are
 * the prototype's own naming convention. A client whose sites are F001…F008 —
 * the ordinary case — failed every one of those tests, so each of their five
 * distribution centres was labelled "Manufacturing Plant", took the plant
 * branch for utilisation, and skipped the DC-only cards entirely.
 */
export function facilityRole(id) {
  if (PLANTS.some(f => f.id === id)) return 'PLANT';
  if (DCS.some(f => f.id === id)) return 'DC';
  if (MARKETS.some(f => f.id === id)) return 'MARKET';
  return null;
}

export function isDCFacility(id) { return facilityRole(id) === 'DC'; }
export function isPlantFacility(id) { return facilityRole(id) === 'PLANT'; }

export function getScenarioById(id) {
  return SCENARIOS.find(s => s.id === id);
}

export function formatCurrency(value, decimals = 0) {
  // A cost the engine could not produce has no number. Rendering an em dash is
  // the honest answer; "₹0" would read as a free network, and the previous
  // version threw on null, taking the whole screen down with it.
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  if (value >= 100000) return "₹" + (value / 100000).toFixed(1) + "L";
  if (value >= 1000) return "₹" + (value / 1000).toFixed(1) + "K";
  return "₹" + Number(value).toFixed(decimals);
}

/**
 * Format a number that may legitimately be absent.
 *
 * Returns an em dash for null/undefined/NaN instead of throwing or printing
 * "NaN". Screens call this wherever a figure comes from the engine, because a
 * metric the solver could not produce has no number — and a crash or a zero
 * are both worse answers than saying so.
 */
export function fmtNum(value, digits = 1, suffix = '') {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return `${Number(value).toFixed(digits)}${suffix}`;
}

export function formatNumber(value) {
  // A quantity the engine did not produce has no number. An em dash is the
  // honest rendering; the previous version threw on null and took the screen
  // down with it.
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("en-IN");
}

// Single owner for the utilization risk bands used everywhere in the app
// (KPI tiles, map markers, facility panels, legends) — Healthy <85%,
// Stress 85-95%, Critical >=95%, matching the audited KPI Formula/Logic
// Verification's DC Capacity Utilization thresholds. Every other place
// that needs a color, label or tag class for a utilization number should
// call one of these three functions rather than re-testing the raw
// percentage, so the band can never drift out of sync between screens.
export function getUtilColor(pct) {
  if (pct >= 95) return "#dc2626";
  if (pct >= 85) return "#f59e0b";
  return "#22c55e";
}

export function getUtilLabel(pct) {
  if (pct >= 95) return "Critical";
  if (pct >= 85) return "Stress";
  return "Healthy";
}

export function getUtilTagClass(pct) {
  if (pct >= 95) return "tag-danger";
  if (pct >= 85) return "tag-warning";
  return "tag-success";
}

// ─── S6: Optimized Base Case (single owner) ──────────────────────────
// The ONE authoritative computation of "today's facility footprint held
// fixed, only routing/allocation re-optimized" — Z = ΣC_ij·x_ij (transport)
// + ΣF_j·y_j (fixed facility, unchanged since footprint is fixed) +
// ΣP_unmet·u_k (shortage penalty, ₹10,000/unit) + SLA-pen, per the KPI
// Formula/Logic Verification's Total Network Cost definition.
//
// This is NOT the same figure as SCENARIOS' SCN_REBALANCE ("Recommended"):
// that scenario allows volume reallocation across plants and is a candidate
// action a user opts into, whereas the Optimized Base Case is the routing-
// only floor achievable with zero footprint change. Do not conflate them —
// every screen that shows a "Baseline Cost" / "Optimized Cost" / "Savings"
// figure for S6 must call this function rather than reading SCENARIOS or
// getNetworkKpis() (both are separate, independently-owned figures).
//
// Provenance: DEMO. No live optimizer is wired into this prototype, so
// these numbers are hand-authored to be internally consistent (fixed cost
// unchanged, transport cost improved, shortage penalty cleared by better
// routing) rather than fabricated as if they were a solver result. Once a
// real deterministic engine is wired in, this function's body — not its
// call sites — is what gets replaced, and `source` becomes DETERMINISTIC_ENGINE.
/**
 * The network's cost/service base case, as computed by the KPI layer.
 *
 * Returns an all-null base case until `setAuthoritativeBaseline()` installs a
 * solved one. It used to return a hand-authored demo case instead —
 * ₹12.85L total cost, 89.5% fill rate, 78% utilisation, and a 7.8% "savings
 * opportunity" against an "optimised" counterpart that no solver produced.
 * Home read it straight onto its KPI strip under "Source: Optimized Base Case
 * (Actual)", so a user who had uploaded nothing was shown a complete costed
 * plan for a network that does not exist.
 *
 * Every consumer renders a dash for a null field, so an empty base case is a
 * visible absence rather than a wrong number.
 */
const EMPTY_BASELINE = {
  totalCost: null, transportCost: null, fixedCost: null, variableCost: null,
  inventoryCost: null, handlingCost: null, unmetPenalty: null, slaPenalty: null,
  sla: null, fillRate: null, avgUtilization: null, maxUtilization: null,
  carbonKgCo2e: null, avgDistanceKm: null, capacityHeadroomPct: null,
  totalDemand: null, servedDemand: null, unservedDemand: null,
};

export function getOptimizedBaseCase() {
  if (customOptimizedBaseCase) {
    return customOptimizedBaseCase;
  }
  return {
    source: 'NONE',
    footprintChange: null,
    baseline: { ...EMPTY_BASELINE },
    // No optimised counterpart and no savings figure: neither exists until an
    // optimisation scenario is actually run.
    optimized: null,
    savings: null,
    unavailableReason: 'no network has been analysed for this project yet',
  };
}

let customOptimizedBaseCase = null;

/**
 * Install a baseline computed by the authoritative KPI layer.
 *
 * `getOptimizedBaseCase()` returns this in preference to the demo figures
 * below, so every screen that reads the base case shows solved values. Fields
 * the engine could not report arrive as null and stay null — the screens render
 * a dash. Nothing here is derived, scaled or filled in.
 */
export function setAuthoritativeBaseline(baseCase) {
  customOptimizedBaseCase = baseCase || null;
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('baselineUpdated', { detail: baseCase }));
  }
}

export function hasAuthoritativeBaseline() {
  return Boolean(customOptimizedBaseCase
    && customOptimizedBaseCase.source === 'AUTHORITATIVE_KPI_LAYER');
}

export function loadNetworkData(networkData) {
  if (!networkData) return;

  if (networkData.plants && networkData.plants.length > 0) {
    PLANTS.length = 0;
    PLANTS.push(...networkData.plants);
  }

  if (networkData.dcs && networkData.dcs.length > 0) {
    DCS.length = 0;
    DCS.push(...networkData.dcs);
  }

  if (networkData.markets && networkData.markets.length > 0) {
    MARKETS.length = 0;
    MARKETS.push(...networkData.markets);
  }

  FACILITIES.length = 0;
  FACILITIES.push(
    ...PLANTS.map(p => ({ ...p, type: 'Plant' })),
    ...DCS.map(d => ({ ...d, type: 'DC' }))
  );

  if (networkData.lanes && networkData.lanes.length > 0) {
    LANES.length = 0;
    LANES.push(...networkData.lanes);
  }

  // De-overlap newly added nodes for clear map rendering
  deoverlapNodes([PLANTS, DCS, MARKETS]);

  // Seed a KPI shell per uploaded facility, carrying ONLY what the upload
  // itself states.
  //
  // This block used to manufacture a full performance profile for every
  // facility the moment a file was parsed: utilisation defaulted to 75%,
  // throughput to 80% of capacity, cost per unit to ₹4.2 (DC) or ₹3.5 (plant),
  // SLA to 96.5%, storage cost to ₹45,000, transport cost to ₹320,000, every
  // lane's volume to 1,500 units at 97.2% on-time, and each metric carried an
  // invented period-on-period delta ("+4.2%", "+2.1%", "-0.3%", "+1.5%").
  // None of it came from a solve. It was visible before any optimisation ran,
  // and it survived intact whenever the solve produced nothing — so an
  // INFEASIBLE network still showed a plausible, entirely fictional set of
  // facility KPIs.
  //
  // Throughput, utilisation and per-unit cost are solver outputs and stay null
  // until `hydrateFromBackend()` writes the authoritative values.
  // The shell carries EVERY key its consumers read, all null.
  //
  // Two different shapes were in play: this seed wrote
  // `throughput/util/costPerUnit/…` while `hydrateFromBackend()` wrote
  // `utilisation/sla/totalCost/inventoryDays`. renderFacilityDashboard reads
  // the second set, so whenever hydration did not run — which is exactly the
  // case when a solve is infeasible — it hit `kpis.totalCost.value` on an
  // object that had no `totalCost` and threw, taking the whole KPI screen
  // down. One shape now, so both paths agree.
  Object.keys(FACILITY_KPIS).forEach(k => delete FACILITY_KPIS[k]);
  [...PLANTS, ...DCS].forEach(f => {
    FACILITY_KPIS[f.id] = {
      [SOLVED_STATE_KEY]: {
        // Solver outputs — absent until a solve produces them.
        throughput: { value: null, unit: 'units/period', delta: null },
        util: { value: null, unit: '%', delta: null },
        utilisation: { value: null, capacity: f.capacity ?? null,
                       unit: 'units/period', prev: null, status: 'unknown' },
        // `target: 95.0` used to sit here, and the Home KPI strip subtracted
        // the solved fill rate from it and printed the difference as
        // "vs target: +5.0%". Nothing in any upload states a service target,
        // so that was a benchmark the product invented and then reported the
        // client's performance against. Null until something real supplies one.
        sla: { value: null, unit: '%', target: null, delta: null, prev: null,
               status: 'unknown' },
        totalCost: { value: null, prev: null, status: 'unknown' },
        inventoryDays: { value: null, prev: null, status: 'unknown' },
        // Stated in the upload.
        costPerUnit: { value: f.handlingCost ?? null, unit: '₹', delta: null },
        capacity: { value: f.capacity ?? null, unit: 'units/period' },
        costBreakdown: null,
        prevLabel: 'no prior solve',
        laneFlows: LANES
          .filter(l => l.from === f.id || l.to === f.id)
          .map(l => ({
            lane: `${l.from} → ${l.to}`,
            // Volume is a solver output; the rate is from the upload.
            volume: null,
            cost: null,
            ratePerUnit: l.cost ?? null,
            ontimePct: null,
          })),
      },
    };
  });

  // NOTE (Phase 10.0): this function used to derive a full base case here from
  // whatever `networkData.kpis` happened to contain — inventing an "optimised"
  // counterpart as `totalCost * 0.94`, an SLA of 98.2, savings of 6.0%, and
  // labelling the result `source: "DETERMINISTIC_ENGINE"`. None of it came from
  // a solver. Topology is loaded here; solved figures arrive through
  // `setAuthoritativeBaseline()` once the network has actually been optimised.

  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('networkDataLoaded', { detail: networkData }));
  }
}

if (typeof window !== 'undefined') {
  window.loadNetworkData = loadNetworkData;
}

