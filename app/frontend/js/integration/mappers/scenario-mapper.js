/**
 * NetGravity — Scenario Mapper
 * ============================
 * Pure transformation from the backend's authoritative scenario payload to the
 * Scenario Planning cockpit's card model.
 *
 * Phase 10.0 rewrite. The previous mapper defaulted every absent authoritative
 * value to zero — `raw.totalCost || 0`, `raw.sla || 0`, `raw.carbonKg || 0` —
 * and defaulted unknown capacity risk to `'Low'`. Both are forbidden: a missing
 * cost is not a cost of zero, and an unknown risk is not a low risk. Absent
 * evidence now maps to `null` plus a status the UI renders as "Unavailable".
 */

/** Unwrap a `KPIResult`; a non-VALID status yields a null value, never 0. */
export function readKPI(kpiResult) {
  if (!kpiResult) {
    return { value: null, status: 'NOT_COMPUTABLE', unit: '', isValid: false };
  }
  const status = kpiResult.status || 'NOT_COMPUTABLE';
  if (status !== 'VALID' || kpiResult.value === null || kpiResult.value === undefined) {
    return {
      value: null,
      status,
      unit: kpiResult.unit || '',
      isValid: false,
      reason: (kpiResult.metadata && kpiResult.metadata.reason) || '',
    };
  }
  return {
    value: kpiResult.value,
    status: 'VALID',
    unit: kpiResult.unit || '',
    isValid: true,
  };
}

/** Unwrap a `ScenarioMetricDelta`. `NOT_COMPARABLE` never becomes 0. */
export function readDelta(delta) {
  if (!delta || delta.direction === 'NOT_COMPARABLE') {
    return {
      absolute: null,
      percentage: null,
      direction: 'NOT_COMPARABLE',
      isComparable: false,
      reason: (delta && delta.reason) || 'One side of the comparison is unavailable.',
    };
  }
  return {
    absolute: delta.abs_delta,
    percentage: delta.pct_delta,
    direction: delta.direction,
    isComparable: true,
    baselineValue: delta.baseline_value,
    comparisonValue: delta.comparison_value,
  };
}

/**
 * Capacity-risk band from peak utilisation.
 *
 * Returns 'Unknown' when utilisation is unavailable. The previous mapper
 * returned 'Low' in that case, which reported an unmeasured network as safe.
 */
export function capacityRiskFrom(peakUtil) {
  if (peakUtil === null || peakUtil === undefined) {
    return { label: 'Unknown', className: 'grey' };
  }
  if (peakUtil >= 90) return { label: 'High', className: 'red' };
  if (peakUtil >= 75) return { label: 'Medium', className: 'amber' };
  return { label: 'Low', className: 'green' };
}

/**
 * Map one authoritative scenario response to the cockpit card model.
 *
 * Fields the engine does not produce (implementation cost, star rating,
 * narrative assessment) are surfaced as null/empty rather than invented. The
 * prototype populated them with fabricated constants.
 */
/**
 * Every network figure the comparison table can show, unwrapped from one side
 * of the response.
 *
 * The mapper used to read six KPIs. The comparison table offers thirteen rows,
 * so seven of them — transport cost, fixed facility cost, inventory cost,
 * handling cost, carbon, fill rate, inventory days — had no field to read and
 * rendered an em dash for every scenario, on a payload that carried all but the
 * last of them. `network_kpis()` returns the full set on both sides; this
 * unwraps the full set.
 */
function readNetworkFigures(kpis) {
  const of = (id) => readKPI(kpis[id]);
  const cost = of('business_network_cost');
  const sla = of('pct_demand_in_sla');
  const fillRate = of('demand_fill_rate');
  const avgUtil = of('avg_utilization_pct');
  const maxUtil = of('max_utilization_pct');
  const carbon = of('total_carbon_kg');
  const risk = capacityRiskFrom(maxUtil.value);

  return {
    totalCost: cost.value,
    totalCostStatus: cost.status,
    sla: sla.value,
    slaStatus: sla.status,
    // Reported as a percentage, because that is how the table's row is
    // labelled. The engine reports a fraction.
    fillRate: fillRate.value === null ? null : +(fillRate.value * 100).toFixed(2),
    fillRateStatus: fillRate.status,
    avgUtil: avgUtil.value,
    avgUtilStatus: avgUtil.status,
    maxUtil: maxUtil.value,
    maxUtilStatus: maxUtil.status,
    carbonKg: carbon.value,
    carbonStatus: carbon.status,

    // The components that add up to the total. Each is the solver's own
    // figure, wrapped, never recomputed here.
    transportCost: of('transport_cost').value,
    fixedCost: of('facility_cost').value,
    inventoryCost: of('inventory_cost').value,
    handlingCost: of('handling_cost').value,
    openingCost: of('opening_cost').value,
    closureCost: of('closure_cost').value,

    totalDemand: of('total_demand').value,
    servedDemand: of('served_demand').value,
    unservedDemand: of('unserved_demand').value,
    facilitiesOpen: of('n_facilities_open').value,
    facilitiesClosed: of('n_facilities_closed').value,

    capacityRisk: risk.label,
    capacityRiskClass: risk.className,

    // Inventory days is not a figure this engine produces. Named here as null
    // so the row renders "Not available" rather than reading undefined off a
    // record that never had the field.
    inventoryDays: null,
  };
}

export function mapScenarioRecord(raw) {
  if (!raw) return null;

  const kpis = raw.scenario_kpis || {};
  const deltas = raw.deltas || {};
  const costDelta = readDelta(deltas.business_network_cost);

  // The network unchanged, but re-solved with the same freedom a scenario has.
  //
  // Without it every scenario looks like it saves the same ~47%, because the
  // project baseline is an as-is evaluation with the footprint pinned open
  // while a scenario is free to close sites. Separating the two is the
  // difference between "closing Kolkata saves ₹85L" and the truth.
  const referenceCost = readKPI(
    (raw.reference_kpis || {}).business_network_cost).value;
  const scenarioCost = readKPI(kpis.business_network_cost).value;

  return {
    id: raw.id,
    name: raw.name,
    shortName: raw.name,
    cardTitle: raw.name,
    type: raw.type || 'USER_CREATED',
    source: raw.source || 'user',
    projectId: raw.project_id,
    snapshotId: raw.snapshot_id,
    executionId: raw.execution_id,
    feasible: raw.feasible !== false,

    // Authoritative measurements. `value` is null whenever `status` is not
    // VALID; the UI must render `status`, not coerce the null.
    ...readNetworkFigures(kpis),

    costChange: costDelta.percentage === null
      ? null : +costDelta.percentage.toFixed(2),
    costChangeAbsolute: costDelta.absolute,
    costChangeComparable: costDelta.isComparable,

    // What re-optimising the footprint is worth, and what THIS CHANGE is worth
    // on top of it. Null when the reference solve was unavailable — never
    // approximated from the numbers that are present.
    referenceCost,
    reoptimisationEffect: null,   // filled by the caller, which has the baseline
    changeEffect: (typeof referenceCost === 'number' && typeof scenarioCost === 'number')
      ? +(scenarioCost - referenceCost).toFixed(2) : null,
    referenceNote: raw.reference_note || '',

    // THIS scenario's own grounded briefing, from the reasoning step its
    // workflow already ran. Null when the run produced none — the pane then
    // says it has nothing to explain, rather than showing the network's
    // briefing beside a scenario's numbers.
    explanation: raw.explanation && Object.keys(raw.explanation).length
      ? raw.explanation : null,

    deltas,
    baselineKpis: raw.baseline_kpis || {},
    referenceKpis: raw.reference_kpis || {},
    scenarioKpis: kpis,

    // The solved topology on both sides, so the Digital Twin can draw what
    // this scenario changed rather than falling back to a fixed table.
    baselineFacilities: raw.baseline_facilities || {},
    scenarioFacilities: raw.scenario_facilities || {},
    baselineFlows: raw.baseline_flows || [],
    scenarioFlows: raw.scenario_flows || [],
    // Sites this scenario introduces that exist in no uploaded network. They
    // carry their own coordinates, because the KPI layer's per-facility record
    // is a solver outcome and has none.
    newSites: raw.new_sites || [],
    // What the builder actually did, in its own words.
    overrides: raw.overrides || [],
    triggeredThresholds: raw.triggered_thresholds || [],
    provenance: raw.provenance || {},
    request: raw.request || {},

    // Not produced by any engine — left empty rather than fabricated.
    implementationCost: null,
    implementationTime: null,
    confidence: null,
    stars: null,
    robustnessTests: [],
    aiAssessment: null,
  };
}

//: The id every screen uses for "the network as it stands".
export const BASELINE_SCENARIO_ID = 'SCN_ACTUAL';

/**
 * The baseline row the comparison table compares against.
 *
 * There was none. `SCENARIOS` held only solved scenarios, and the table did
 * `SCENARIOS.find(s => s.id === 'SCN_ACTUAL') || SCENARIOS[0]` — so the column
 * headed "Current Baseline" was in fact the FIRST USER SCENARIO. Scenario 1
 * compared against itself (every delta "—"), and scenario 2 was measured
 * against scenario 1 rather than against the network. Every number in the table
 * was real; what they were being compared to was not what the header said.
 *
 * Built from a solved scenario's own `baseline_kpis` — the same snapshot solve
 * every scenario is measured against, which is exactly the figure the header
 * claims.
 */
export function baselineFromScenarioRecord(raw) {
  if (!raw || !raw.baseline_kpis) return null;
  return {
    id: BASELINE_SCENARIO_ID,
    name: 'Current Baseline',
    shortName: 'Baseline',
    cardTitle: 'Current network',
    type: 'BASELINE',
    source: 'engine',
    num: 0,
    projectId: raw.project_id,
    snapshotId: raw.snapshot_id,
    executionId: raw.execution_id,
    feasible: true,
    ...readNetworkFigures(raw.baseline_kpis),
    // The baseline is the reference, so it has no change against itself.
    costChange: 0,
    costChangeAbsolute: 0,
    costChangeComparable: true,
    referenceCost: null,
    reoptimisationEffect: null,
    changeEffect: null,
    referenceNote: '',
    deltas: {},
    baselineKpis: raw.baseline_kpis,
    referenceKpis: {},
    scenarioKpis: raw.baseline_kpis,
    baselineFacilities: raw.baseline_facilities || {},
    scenarioFacilities: raw.baseline_facilities || {},
    baselineFlows: raw.baseline_flows || [],
    scenarioFlows: raw.baseline_flows || [],
    newSites: [],
    overrides: [],
    triggeredThresholds: [],
    provenance: raw.provenance || {},
    request: {},
    implementationCost: null,
    implementationTime: null,
    confidence: null,
    stars: null,
    robustnessTests: [],
    aiAssessment: null,
  };
}

/**
 * The same baseline row, built from `/api/kpis/*` when no scenario exists yet.
 *
 * A project that has been solved but has no scenario still has a baseline to
 * show, and the map's Baseline toggle needs one before the first scenario is
 * created.
 */
export function baselineFromNetworkKPIs({ kpis, facilities, flows, snapshotId,
                                          executionId, projectId } = {}) {
  if (!kpis || !Object.keys(kpis).length) return null;
  const facilityStates = {};
  Object.entries(facilities || {}).forEach(([id, metrics]) => {
    const value = (metricId) => {
      const r = metrics[metricId];
      return r && r.status === 'VALID' ? r.value : null;
    };
    facilityStates[id] = {
      utilPct: value('utilization_pct'),
      throughput: value('throughput_units'),
      capacity: value('capacity_units'),
      isOpen: value('is_open'),
    };
  });
  return baselineFromScenarioRecord({
    project_id: projectId,
    snapshot_id: snapshotId,
    execution_id: executionId,
    baseline_kpis: kpis,
    baseline_facilities: facilityStates,
    baseline_flows: flows || [],
    provenance: { authoritative_source: 'KPIRegistry', llm_used: false },
  });
}

/** Format an authoritative value for display, or an explicit unavailable mark. */
export function displayValue(value, status, formatter = (v) => String(v)) {
  if (value === null || value === undefined) {
    if (status === 'INFEASIBLE') return 'Infeasible';
    if (status === 'INSUFFICIENT_EVIDENCE') return 'No evidence';
    return 'Unavailable';
  }
  return formatter(value);
}
