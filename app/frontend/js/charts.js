/**
 * NetGravity — Chart.js Visualizations
 * ======================================
 * Forecast chart, scenario cost comparison, performance radar.
 */

/* global Chart */

import { DEMAND_HISTORY, FORECAST, SCENARIOS, formatCurrency } from './data.js';

const chartInstances = {};

// ─── Forecast Chart ─────────────────────────────────────────
export function renderForecastChart(canvasId) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();

  const ctx = document.getElementById(canvasId);
  if (!ctx) return;

  const histLabels = DEMAND_HISTORY.months;
  const foreLabels = FORECAST.months;
  const allLabels = [...histLabels, ...foreLabels];

  // Historical data + nulls for forecast period
  const histData = [...DEMAND_HISTORY.northIndia, ...new Array(foreLabels.length).fill(null)];

  // Forecast data: nulls for historical + forecast values (overlap last historical point)
  const foreData = [...new Array(histLabels.length - 1).fill(null), DEMAND_HISTORY.northIndia[histLabels.length - 1], ...FORECAST.northIndia];
  const upperData = [...new Array(histLabels.length - 1).fill(null), DEMAND_HISTORY.northIndia[histLabels.length - 1], ...FORECAST.upper];
  const lowerData = [...new Array(histLabels.length - 1).fill(null), DEMAND_HISTORY.northIndia[histLabels.length - 1], ...FORECAST.lower];

  // Capacity line
  const capData = allLabels.map(() => DEMAND_HISTORY.baddiCapacity);

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
          borderWidth: 2.5,
          pointRadius: 2,
          pointHoverRadius: 5,
          fill: false,
          tension: 0.3,
        },
        {
          label: 'Forecast',
          data: foreData,
          borderColor: '#6B2FA0',
          borderWidth: 2.5,
          borderDash: [6, 4],
          pointRadius: 3,
          pointHoverRadius: 6,
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
          label: 'Delhi NCR DC Capacity',
          data: capData,
          borderColor: '#dc2626',
          borderWidth: 2,
          borderDash: [10, 5],
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
          labels: { usePointStyle: true, font: { family: 'Inter', size: 11 }, padding: 16 },
        },
        tooltip: {
          backgroundColor: '#1a1a2e',
          titleFont: { family: 'Inter', size: 12 },
          bodyFont: { family: 'Inter', size: 11 },
          padding: 12,
          cornerRadius: 8,
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { font: { family: 'Inter', size: 10 }, maxRotation: 45 },
        },
        y: {
          beginAtZero: false,
          min: 6000,
          grid: { color: '#f0f0f5' },
          ticks: {
            font: { family: 'Inter', size: 11 },
            callback: v => (v / 1000).toFixed(0) + 'K',
          },
          title: { display: true, text: 'Demand (units/day)', font: { family: 'Inter', size: 11, weight: '600' } },
        },
      },
    },
  });
}

// ─── Scenario Cost Comparison ───────────────────────────────
export function renderScenarioCostChart(canvasId) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();

  const ctx = document.getElementById(canvasId);
  if (!ctx) return;

  // Only scenarios (not actual baseline)
  const scenarios = SCENARIOS.filter(s => s.id !== 'SCN_ACTUAL');

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: scenarios.map(s => s.name),
      datasets: [
        {
          label: 'Transport Cost',
          data: scenarios.map(s => s.transportCost),
          backgroundColor: '#6B2FA0',
          borderRadius: 4,
        },
        {
          label: 'Fixed Cost',
          data: scenarios.map(s => s.fixedCost),
          backgroundColor: '#b893d6',
          borderRadius: 4,
        },
        {
          label: 'Inventory Cost',
          data: scenarios.map(s => s.inventoryCost),
          backgroundColor: '#e8ddf2',
          borderRadius: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'top',
          labels: { usePointStyle: true, font: { family: 'Inter', size: 11 } },
        },
        tooltip: {
          backgroundColor: '#1a1a2e',
          callbacks: {
            label: ctx => `${ctx.dataset.label}: ${formatCurrency(ctx.raw)}`,
          },
        },
      },
      scales: {
        x: {
          stacked: true,
          grid: { display: false },
          ticks: { font: { family: 'Inter', size: 11 } },
        },
        y: {
          stacked: true,
          grid: { color: '#f0f0f5' },
          ticks: {
            font: { family: 'Inter', size: 11 },
            callback: v => formatCurrency(v),
          },
        },
      },
    },
  });
}

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
          label: 'Daily Flow (units/day)',
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
            label: (ctx) => `Flow: ${ctx.raw.toLocaleString('en-IN')} units/day · Cost: ₹${costs[ctx.dataIndex]}/unit`,
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

