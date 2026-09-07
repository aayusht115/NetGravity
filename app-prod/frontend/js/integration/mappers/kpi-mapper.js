/**
 * NetGravity — Authoritative KPI Mapper
 * =====================================
 * Pure transformation layer from backend `KPIResult` models to frontend card models.
 * Strictly adheres to Phase 9.1 Authoritative KPI rules:
 * - Never calculates authoritative KPIs on the client.
 * - Never defaults missing or invalid metrics to zero.
 * - Preserves provenance and evidence status.
 */

export function mapKPIValue(kpiResult, formatter = (v) => String(v)) {
  if (!kpiResult) {
    return { display: '—', status: 'UNAVAILABLE', isValid: false };
  }
  if (!kpiResult.is_valid || kpiResult.value === null || kpiResult.value === undefined) {
    const status = kpiResult.status || 'UNAVAILABLE';
    const reason = (kpiResult.metadata && kpiResult.metadata.reason) || 'Insufficient evidence';
    return {
      display: status === 'INFEASIBLE' ? 'Infeasible' : 'Unavailable',
      status,
      reason,
      isValid: false,
    };
  }
  return {
    display: formatter(kpiResult.value),
    status: 'VALID',
    value: kpiResult.value,
    unit: kpiResult.unit,
    isValid: true,
  };
}

export function formatCurrencyLakhs(val) {
  if (typeof val !== 'number') return '—';
  // If value is in raw rupees, convert to Lakhs
  const inLakhs = val > 10000 ? val / 100000 : val;
  return `₹${inLakhs.toFixed(2)}L`;
}

export function formatPct(val) {
  if (typeof val !== 'number') return '—';
  return `${val.toFixed(1)}%`;
}

export function formatNumberWithCommas(val) {
  if (typeof val !== 'number') return '—';
  return Math.round(val).toLocaleString();
}

export function mapNetworkKPIsToCards(rawKpis) {
  if (!rawKpis || typeof rawKpis !== 'object') {
    return {
      totalCost: { display: '—', status: 'UNAVAILABLE' },
      sla: { display: '—', status: 'UNAVAILABLE' },
      fillRate: { display: '—', status: 'UNAVAILABLE' },
      peakUtil: { display: '—', status: 'UNAVAILABLE' },
      carbon: { display: '—', status: 'UNAVAILABLE' },
    };
  }

  const costRes = rawKpis.business_network_cost || rawKpis.total_cost;
  const slaRes = rawKpis.pct_demand_in_sla;
  // Fill rate and SLA compliance are different metrics that coincide only when
  // every servable unit is also within its service level. The Home tile is
  // labelled "Fill Rate" and was reading the SLA percentage.
  const fillRes = rawKpis.demand_fill_rate;
  const peakUtilRes = rawKpis.max_utilization_pct;
  const carbonRes = rawKpis.total_carbon_kg;

  return {
    totalCost: mapKPIValue(costRes, formatCurrencyLakhs),
    sla: mapKPIValue(slaRes, formatPct),
    // The engine reports fill rate as a fraction; the card shows a percentage.
    fillRate: mapKPIValue(fillRes, (v) => formatPct(v * 100)),
    peakUtil: mapKPIValue(peakUtilRes, formatPct),
    carbon: mapKPIValue(carbonRes, (v) => `${formatNumberWithCommas(v)} kg`),
  };
}
