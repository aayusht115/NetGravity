/**
 * NetGravity — Chart.js Visualizations
 * ======================================
 * Forecast chart, scenario cost comparison, performance radar.
 */

/* global Chart */

import { DEMAND_HISTORY, FORECAST, SCENARIOS, perPeriodLabel } from './data.js';

const chartInstances = {};

// ─── Forecast Chart ─────────────────────────────────────────
export function renderForecastChart(canvasId) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();

  const ctx = document.getElementById(canvasId);
  if (!ctx) return;

  const isCompact = canvasId === 'chart-forecast-home';
  const histLabels = DEMAND_HISTORY.months;
  const foreLabels = FORECAST.months;

  // With no history there is nothing to draw. The padding below is built with
  // `new Array(histLabels.length - 1)`, which is `new Array(-1)` for an empty
  // series and throws `RangeError: Invalid array length` — so an empty
  // forecast took the whole screen down instead of showing an empty state.
  if (!histLabels.length) {
    const host = ctx.parentElement;
    if (host && !host.querySelector('.ng-forecast-empty')) {
      const note = document.createElement('div');
      note.className = 'ng-forecast-empty text-xs text-muted';
      note.style.cssText = 'display:flex;align-items:center;justify-content:center;'
        + 'height:100%;text-align:center;padding:24px';
      note.textContent = 'No demand history has been ingested for this network, '
        + 'so no forecast can be produced.';
      host.appendChild(note);
    }
    ctx.style.display = 'none';
    return;
  }
  ctx.style.display = '';
  const stale = ctx.parentElement && ctx.parentElement.querySelector('.ng-forecast-empty');
  if (stale) stale.remove();

  const allLabels = [...histLabels, ...foreLabels];

  // Historical data + nulls for forecast period
  const histData = [...DEMAND_HISTORY.northIndia, ...new Array(foreLabels.length).fill(null)];

  // Forecast data: nulls for historical + forecast values (overlap last historical point)
  const foreData = [...new Array(histLabels.length - 1).fill(null), DEMAND_HISTORY.northIndia[histLabels.length - 1], ...FORECAST.northIndia];
  const upperData = [...new Array(histLabels.length - 1).fill(null), DEMAND_HISTORY.northIndia[histLabels.length - 1], ...FORECAST.upper];
  const lowerData = [...new Array(histLabels.length - 1).fill(null), DEMAND_HISTORY.northIndia[histLabels.length - 1], ...FORECAST.lower];

  // Capacity line — drawn only when a threshold is actually known for this
  // series. All-null keeps the dataset present (so the legend is stable) but
  // plots nothing, instead of drawing another network's capacity across the
  // chart and dragging the y-axis away from the real demand range.
  const capValue = DEMAND_HISTORY.baddiCapacity;
  const hasCap = typeof capValue === 'number' && Number.isFinite(capValue);
  const capData = allLabels.map(() => (hasCap ? capValue : null));

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'line',
    data: {
      labels: allLabels,
      datasets: [
        {
          label: 'Historical Demand',
          data: histData,
          borderColor: '#6B2FA0',
          backgroundColor: 'rgba(107,47,160,.08)',
          borderWidth: isCompact ? 2 : 2.5,
          pointRadius: isCompact ? 1.5 : 2,
          pointHoverRadius: 4,
          fill: false,
          tension: 0.3,
        },
        {
          label: 'Forecast',
          data: foreData,
          borderColor: '#6B2FA0',
          borderWidth: isCompact ? 2 : 2.5,
          borderDash: [5, 3],
          pointRadius: isCompact ? 2 : 3,
          pointHoverRadius: 5,
          fill: false,
          tension: 0.3,
        },
        {
          label: 'Upper Bound',
          data: upperData,
          borderColor: 'rgba(107,47,160,.2)',
          backgroundColor: 'rgba(107,47,160,.06)',
          borderWidth: 1,
          pointRadius: 0,
          fill: '+1',
          tension: 0.3,
        },
        {
          label: 'Lower Bound',
          data: lowerData,
          borderColor: 'rgba(107,47,160,.2)',
          borderWidth: 1,
          pointRadius: 0,
          fill: false,
          tension: 0.3,
        },
        {
          label: 'Capacity',
          data: capData,
          borderColor: '#dc2626',
          borderWidth: 1.8,
          borderDash: [8, 4],
          pointRadius: 0,
          fill: false,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          position: 'top',
          labels: {
            usePointStyle: true,
            boxWidth: 6,
            font: { family: 'Inter', size: isCompact ? 9.5 : 11 },
            padding: isCompact ? 8 : 16,
          },
        },
        tooltip: {
          backgroundColor: '#1a1a2e',
          titleFont: { family: 'Inter', size: 12 },
          bodyFont: { family: 'Inter', size: 11 },
          padding: 10,
          cornerRadius: 8,
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            font: { family: 'Inter', size: isCompact ? 8.5 : 10 },
            maxRotation: 45,
            maxTicksLimit: isCompact ? 8 : 16,
          },
        },
        y: {
          beginAtZero: false,
          // The floor was pinned at 6,000 — the prototype's own demand range.
          // Any network whose demand sits below that had its entire history
          // clipped off the bottom of the chart: the line simply was not
          // there, and only the forecast tail showed. Chart.js fits the axis
          // to the data instead.
          grid: { color: '#f0f0f5' },
          ticks: {
            font: { family: 'Inter', size: isCompact ? 9 : 11 },
            callback: v => (v / 1000).toFixed(0) + 'K',
          },
          title: {
            display: !isCompact,
            text: `Demand (${perPeriodLabel()})`,
            font: { family: 'Inter', size: 11, weight: '600' },
          },
        },
      },
    },
  });
}

// ─── Scenario Cost Comparison ───────────────────────────────
// ─── Scenario Cost Impact (vs Baseline) ──────────────────────
export function renderScenarioCostImpactChart(canvasId, scenarioList) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();

  const ctx = document.getElementById(canvasId);
  if (!ctx) return;

  const list = scenarioList || SCENARIOS;
  const labels = list.map(s => s.shortName || s.name);
  const dataLakhs = list.map(s => +(s.totalCost / 100000).toFixed(2));

  // Color mapping: Baseline=Gray, Opt Base=Purple, Rec=Green, Others=Purple Accent
  const backgroundColors = list.map(s => {
    if (s.id === 'SCN_ACTUAL') return '#94a3b8';
    if (s.id === 'SCN_REBALANCE') return '#16a34a';
    if (s.id === 'SCN_OPTIMISED_BASE') return '#6B2FA0';
    return '#a855f7';
  });

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'Total Cost (₹ in Lakhs)',
          data: dataLakhs,
          backgroundColor: backgroundColors,
          borderRadius: 6,
          barThickness: 24,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => ` Total Cost: ₹${ctx.raw}L (${list[ctx.dataIndex].costChange ? list[ctx.dataIndex].costChange + '%' : 'Baseline'})`,
          },
        },
      },
      scales: {
        y: {
          min: 10.0,
          max: 14.5,
          grid: { color: '#f0f0f5' },
          ticks: { font: { family: 'Inter', size: 10 }, stepSize: 1.0 },
        },
        x: {
          grid: { display: false },
          ticks: { font: { family: 'Inter', size: 10, weight: '500' } },
        },
      },
    },
  });
}

// ─── Scenario Capacity Risk (December) ──────────────────────
export function renderScenarioCapacityRiskChart(canvasId, scenarioList) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();

  const ctx = document.getElementById(canvasId);
  if (!ctx) return;

  const list = scenarioList || SCENARIOS;
  const labels = list.map(s => s.shortName || s.name);

  // Map risk level: High=4, Medium=3, Low=2, Very Low=1
  const riskValues = list.map(s => {
    if (s.capacityRisk === 'High') return 4;
    if (s.capacityRisk === 'Medium') return 3;
    if (s.capacityRisk === 'Low') return 2;
    return 1; // Very Low
  });

  const colors = list.map(s => {
    if (s.capacityRisk === 'High') return '#dc2626';
    if (s.capacityRisk === 'Medium') return '#f59e0b';
    if (s.capacityRisk === 'Low') return '#22c55e';
    return '#16a34a'; // Very Low
  });

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'Capacity Risk Level',
          data: riskValues,
          backgroundColor: colors,
          borderRadius: 6,
          barThickness: 24,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => ` Capacity Risk: ${list[ctx.dataIndex].capacityRisk} (Delhi NCR: ${list[ctx.dataIndex].delhiUtil || list[ctx.dataIndex].maxUtil}%)`,
          },
        },
      },
      scales: {
        y: {
          min: 0,
          max: 4.5,
          grid: { color: '#f0f0f5' },
          ticks: {
            stepSize: 1,
            callback: (val) => {
              if (val === 4) return 'High';
              if (val === 3) return 'Medium';
              if (val === 2) return 'Low';
              if (val === 1) return 'Very Low';
              return '';
            },
            font: { family: 'Inter', size: 10 },
          },
        },
        x: {
          grid: { display: false },
          ticks: { font: { family: 'Inter', size: 10, weight: '500' } },
        },
      },
    },
  });
}

// ─── Scenario SLA Comparison (On-time Service) ──────────────
export function renderScenarioSlaChart(canvasId, scenarioList) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();

  const ctx = document.getElementById(canvasId);
  if (!ctx) return;

  const list = scenarioList || SCENARIOS;
  const labels = list.map(s => s.shortName || s.name);
  const slaValues = list.map(s => s.sla);

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'On-time SLA (%)',
          data: slaValues,
          borderColor: '#6B2FA0',
          backgroundColor: '#6B2FA0',
          borderWidth: 2,
          pointRadius: 5,
          pointHoverRadius: 7,
          pointBackgroundColor: '#6B2FA0',
          tension: 0.2,
          fill: false,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => ` SLA (On-time): ${ctx.raw}%`,
          },
        },
      },
      scales: {
        y: {
          min: 90,
          max: 100,
          grid: { color: '#f0f0f5' },
          ticks: {
            stepSize: 2,
            callback: (v) => v + '%',
            font: { family: 'Inter', size: 10 },
          },
        },
        x: {
          grid: { display: false },
          ticks: { font: { family: 'Inter', size: 10, weight: '500' } },
        },
      },
    },
  });
}

// ─── Scenario Flow Map Diagram ──────────────────────────────
// REMOVED — `renderScenarioFlowMap()`
//
// A hand-drawn SVG of five fixed nodes (Baddi, Delhi NCR, Mumbai, Kolkata,
// Chennai) with three fixed arcs colour-coded "Increase / Decrease / No
// Change". It took a `containerId` and an `activeScenarioId` and used neither:
// the same picture rendered for every scenario of every network, and the
// colours asserted flow changes no engine had computed.
//
// Deleted rather than left exported: nothing imported it, so it drew for
// nobody, but an exported function that fabricates a network diagram is one
// call away from doing so. The real corridor view is `map.js`
// (`renderScenarioDigitalTwin`), which reads the loaded network and the
// solver's own per-lane flows.

// ─── Performance Radar ──────────────────────────────────────
export function renderScenarioRadar(canvasId) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();

  const ctx = document.getElementById(canvasId);
  if (!ctx) return;

  const scenarios = SCENARIOS.filter(s => s.type === 'SCENARIO');

  // Normalise metrics to 0-100 scale for radar
  function normalise(arr, invert = false) {
    const min = Math.min(...arr);
    const max = Math.max(...arr);
    const range = max - min || 1;
    return arr.map(v => {
      const norm = ((v - min) / range) * 100;
      return invert ? 100 - norm : norm;
    });
  }

  const costs = normalise(scenarios.map(s => s.totalCost), true);         // lower is better
  const slas = normalise(scenarios.map(s => s.sla));                       // higher is better
  const utils = normalise(scenarios.map(s => s.maxUtil), true);            // lower is better
  const carbons = normalise(scenarios.map(s => s.carbonKg), true);        // lower is better
  const implCosts = normalise(scenarios.map(s => s.implementationCost), true); // lower is better

  const radarColors = ['#6B2FA0', '#f59e0b', '#dc2626', '#2563eb'];

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'radar',
    data: {
      labels: ['Cost Efficiency', 'SLA Performance', 'Utilisation Balance', 'Carbon Footprint', 'Implementation Cost'],
      datasets: scenarios.map((s, i) => ({
        label: s.name,
        data: [costs[i], slas[i], utils[i], carbons[i], implCosts[i]],
        borderColor: radarColors[i % radarColors.length],
        backgroundColor: radarColors[i % radarColors.length] + '15',
        borderWidth: 2,
        pointRadius: 3,
        pointHoverRadius: 6,
      })),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'top',
          labels: { usePointStyle: true, font: { family: 'Inter', size: 11 } },
        },
      },
      scales: {
        r: {
          beginAtZero: true,
          max: 100,
          grid: { color: '#e8e8ef' },
          pointLabels: { font: { family: 'Inter', size: 11 } },
          ticks: { display: false },
        },
      },
    },
  });
}

// ─── Facility Dashboard Charts ──────────────────────────────
export function renderFacilityThroughputChart(canvasId, facility) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();

  const ctx = document.getElementById(canvasId);
  if (!ctx || !facility) return;

  const months = ['Sep 25', 'Oct 25', 'Nov 25', 'Dec 25', 'Jan 26', 'Feb 26', 'Mar 26', 'Apr 26', 'May 26', 'Jun 26', 'Jul 26', 'Aug 26', 'Sep 26 (F)', 'Oct 26 (F)', 'Nov 26 (F)'];
  const baseTput = facility.throughput || 8000;
  const cap = facility.capacity || 10000;

  // Generate realistic monthly data based on facility throughput
  const historical = [
    Math.round(baseTput * 0.88), Math.round(baseTput * 0.90), Math.round(baseTput * 0.93), Math.round(baseTput * 0.95),
    Math.round(baseTput * 0.91), Math.round(baseTput * 0.92), Math.round(baseTput * 0.94), Math.round(baseTput * 0.96),
    Math.round(baseTput * 0.97), Math.round(baseTput * 0.98), Math.round(baseTput * 0.99), baseTput,
    null, null, null
  ];

  const isAtRisk = facility.id === 'DC_DELHI' || facility.id === 'PLT_BADDI';
  const growthMultiplier = isAtRisk ? 1.05 : 1.015;

  const forecast = [
    null, null, null, null, null, null, null, null, null, null, null, baseTput,
    Math.round(baseTput * growthMultiplier),
    Math.round(baseTput * Math.pow(growthMultiplier, 2)),
    Math.round(baseTput * Math.pow(growthMultiplier, 3))
  ];

  const capLine = months.map(() => cap);

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'line',
    data: {
      labels: months,
      datasets: [
        {
          label: 'Capacity Limit',
          data: capLine,
          borderColor: '#dc2626',
          borderWidth: 2,
          borderDash: [5, 5],
          pointRadius: 0,
          fill: false,
        },
        {
          label: 'Actual Throughput',
          data: historical,
          borderColor: '#6B2FA0',
          backgroundColor: 'rgba(107,47,160,0.1)',
          borderWidth: 2.5,
          pointRadius: 3,
          pointBackgroundColor: '#6B2FA0',
          tension: 0.3,
          fill: true,
        },
        {
          label: 'Forecast Trend',
          data: forecast,
          borderColor: '#d97706',
          borderWidth: 2.5,
          borderDash: [6, 4],
          pointRadius: 4,
          pointBackgroundColor: '#d97706',
          tension: 0.3,
          fill: false,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'top', labels: { usePointStyle: true, font: { family: 'Inter', size: 11 } } },
        tooltip: {
          callbacks: {
            label: (ctx) => ctx.raw ? `${ctx.dataset.label}: ${ctx.raw.toLocaleString('en-IN')} u/d` : '',
          },
        },
      },
      scales: {
        y: {
          beginAtZero: false,
          min: Math.round(cap * 0.4),
          max: Math.round(cap * 1.18),
          grid: { color: '#f0f0f5' },
          ticks: { font: { family: 'Inter', size: 11 } },
        },
        x: {
          grid: { display: false },
          ticks: { font: { family: 'Inter', size: 10 } },
        },
      },
    },
  });
}

export function renderFacilityCostBreakdownChart(canvasId, facility) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();

  const ctx = document.getElementById(canvasId);
  if (!ctx || !facility) return;

  const isDC = facility.id.startsWith('DC_');
  const transportCost = isDC ? 580000 : 720000;
  const handlingCost = (facility.throughput || 6000) * (facility.handlingCost || 4.0) * 30;
  const fixedCost = isDC ? (facility.fixedCost || 100) * 100000 / 12 : 350000;
  const holdingCost = 140000;
  const surchargeCost = 45000;

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Inbound/Outbound Transport', 'Facility Handling', 'Fixed Storage & Ops', 'Inventory Holding', 'Surcharges & Accessorials'],
      datasets: [
        {
          data: [transportCost, Math.round(handlingCost), Math.round(fixedCost), holdingCost, surchargeCost],
          backgroundColor: ['#6B2FA0', '#2563eb', '#16a34a', '#f59e0b', '#dc2626'],
          borderWidth: 2,
          borderColor: '#ffffff',
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'right',
          labels: { usePointStyle: true, boxWidth: 10, font: { family: 'Inter', size: 11 }, padding: 12 },
        },
        tooltip: {
          callbacks: {
            label: (ctx) => ` ${ctx.label}: ₹${(ctx.raw / 100000).toFixed(2)}L (${((ctx.raw / (transportCost + handlingCost + fixedCost + holdingCost + surchargeCost)) * 100).toFixed(1)}%)`,
          },
        },
      },
      cutout: '68%',
    },
  });
}

export function renderFacilityLaneFlowsChart(canvasId, connectedLanes, facilityId) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();

  const ctx = document.getElementById(canvasId);
  if (!ctx || !connectedLanes || connectedLanes.length === 0) return;

  const labels = connectedLanes.map(l => l.label);
  const flows = connectedLanes.map(l => l.flow);
  const costs = connectedLanes.map(l => l.cost);

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [
        {
          label: `Flow (${perPeriodLabel()})`,
          data: flows,
          backgroundColor: '#6B2FA0',
          borderRadius: 6,
          barThickness: 16,
        },
      ],
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => `Flow: ${ctx.raw.toLocaleString('en-IN')} ${perPeriodLabel()} · Cost: ₹${costs[ctx.dataIndex]}/unit`,
          },
        },
      },
      scales: {
        x: {
          grid: { color: '#f0f0f5' },
          ticks: { font: { family: 'Inter', size: 10 } },
        },
        y: {
          grid: { display: false },
          ticks: { font: { family: 'Inter', size: 11, weight: '500' } },
        },
      },
    },
  });
}

