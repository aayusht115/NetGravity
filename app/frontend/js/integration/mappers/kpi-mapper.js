/**
 * NetGravity — Authoritative KPI Mapper
 * =====================================
 * Pure transformation layer from backend `KPIResult` models to frontend card models.
 * Strictly adheres to Phase 9.1 Authoritative KPI rules:
 * - Never calculates authoritative KPIs on the client.
 * - Never defaults missing or invalid metrics to zero.
 * - Preserves provenance and evidence status.
 */

import { formatCurrency } from '../../data.js';

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

/**
 * Money, at the precision and in the currency the rest of the product uses.
 *
 * THIS USED TO BE ITS OWN OPINION ABOUT BOTH, and it was wrong on both. It
 * hardcoded the rupee and the lakh — so a network priced in dollars printed
 * "₹12.45L" — and it printed two decimal places on a figure in the millions,
 * which is the thing a leadership audience reads as noise rather than
 * precision. It also decided whether a number was already in lakhs by testing
 * `val > 10000`, so a genuine ₹9,000 cost was rendered "₹9000.00L".
 *
 * `formatCurrency` in `data.js` is the one definition of all of that: it reads
 * the project's own currency, and it picks the scale and the precision from
 * the magnitude. Delegating means a figure formatted here and the same figure
 * formatted anywhere else cannot disagree.
 */
export function formatCurrencyLakhs(val) {
  if (typeof val !== 'number') return '—';
  return formatCurrency(val);
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
