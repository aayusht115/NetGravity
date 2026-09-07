/**
 * NetGravity — Chart.js Visualizations
 * ======================================
 * Forecast chart, scenario cost comparison, performance radar.
 */

/* global Chart */

import { DEMAND_HISTORY, FORECAST, SCENARIOS, perPeriodLabel,
         formatCurrency, formatCurrencyExact, formatNumber,
         currencyLabel } from './data.js';

const chartInstances = {};

/**
 * Two annotations the mockup asks for, drawn straight onto the canvas.
 *
 * A Chart.js plugin rather than a second charting library: the annotation
 * plugin is a separate bundle, and these are two lines and two labels.
 *
 *   * A dashed vertical rule at the last OBSERVED period, captioned
 *     "Forecast starts". Without it the reader has to work out where
 *     measurement stops and projection begins from the line's dash pattern
 *     alone, and the two are drawn in the same colour.
 *   * The capacity line's own value, at the right-hand end of it, so the red
 *     rule is a number rather than a decoration.
 *
 * Both are skipped when the thing they describe is not on the chart.
 */
const forecastAnnotations = {
  id: 'ngForecastAnnotations',
  afterDatasetsDraw(chart, _args, opts) {
    const { ctx, chartArea, scales } = chart;
    if (!chartArea || !scales || !scales.x || !scales.y) return;
    const compact = opts && opts.compact;

    // ── the forecast boundary ──
    const idx = opts ? opts.splitIndex : -1;
    if (typeof idx === 'number' && idx >= 0) {
      const x = scales.x.getPixelForValue(idx);
      if (Number.isFinite(x)) {
        ctx.save();
        ctx.setLineDash([5, 4]);
        ctx.lineWidth = 1.4;
        ctx.strokeStyle = '#8b7bb8';
        ctx.beginPath();
        ctx.moveTo(x, chartArea.top + (compact ? 2 : 20));
        ctx.lineTo(x, chartArea.bottom);
        ctx.stroke();
        ctx.restore();

        if (!compact) {
          const label = 'Forecast starts';
          ctx.save();
          ctx.font = '600 11px Inter, system-ui, sans-serif';
          const w = ctx.measureText(label).width + 16;
          const bx = Math.min(Math.max(x - w / 2, chartArea.left),
                              chartArea.right - w);
          const by = chartArea.top + 2;
          ctx.fillStyle = '#ffffff';
          ctx.strokeStyle = '#dfd7ee';
          ctx.lineWidth = 1;
          if (ctx.roundRect) {
            ctx.beginPath();
            ctx.roundRect(bx, by, w, 20, 6);
            ctx.fill();
            ctx.stroke();
          } else {
            ctx.fillRect(bx, by, w, 20);
            ctx.strokeRect(bx, by, w, 20);
          }
          ctx.fillStyle = '#4b3a6b';
          ctx.textBaseline = 'middle';
          ctx.textAlign = 'center';
          ctx.fillText(label, bx + w / 2, by + 10);
          ctx.restore();
        }
      }
    }

    // ── what the red rule is worth ──
    const cap = opts ? opts.capacity : null;
    if (!compact && typeof cap === 'number' && Number.isFinite(cap)) {
      const y = scales.y.getPixelForValue(cap);
      if (Number.isFinite(y) && y > chartArea.top && y < chartArea.bottom) {
        const label = `Capacity ${opts.capacityLabel || ''}`.trim();
        ctx.save();
        ctx.font = '700 11px Inter, system-ui, sans-serif';
        ctx.fillStyle = '#dc2626';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'top';
        ctx.fillText(label, chartArea.right - 4, y + 5);
        ctx.restore();
      }
    }
  },
};

if (typeof Chart !== 'undefined' && Chart.register) {
  Chart.register(forecastAnnotations);
}

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
          label: 'Historical',
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
          label: 'Forecast range (p90)',
          data: upperData,
          borderColor: 'rgba(107,47,160,.2)',
          backgroundColor: 'rgba(107,47,160,.06)',
          borderWidth: 1,
          pointRadius: 0,
          fill: '+1',
          tension: 0.3,
        },
        {
          label: 'Forecast range (p10)',
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
      // Head room for the "Forecast starts" chip, which is drawn inside the
      // canvas and would otherwise sit on the topmost gridline.
      layout: { padding: { top: isCompact ? 0 : 12, right: isCompact ? 0 : 8 } },
      plugins: {
        ngForecastAnnotations: {
          compact: isCompact,
          // The last OBSERVED period: the forecast series starts by repeating
          // it, so this index is the join rather than the first projection.
          splitIndex: histLabels.length - 1,
          capacity: hasCap ? capValue : null,
          capacityLabel: hasCap
            ? `~ ${capValue >= 1000 ? (capValue / 1000).toFixed(1) + 'K'
                                    : formatNumber(capValue)}` : '',
        },
        legend: {
          // Off on the full-size chart: the card draws its own key above the
          // plot (`.fc-legend`), which does not re-wrap every time the series
          // changes and does not name the two bounds datasets that only exist
          // to shade the band between them.
          display: isCompact,
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
            // One decimal below 10K.
            //
            // `toFixed(0)` on a 500-unit step printed "8K" for both 8,000 and
            // 8,500 — an axis with two identical labels a gridline apart, and
            // a reader who reads a value off it gets the wrong number. The
            // decimal is dropped again when it is a zero, and above 10K the
            // step is never fine enough to need it.
            callback: (v) => {
              const k = v / 1000;
              if (Math.abs(k) >= 10) return k.toFixed(0) + 'K';
              const t = k.toFixed(1);
              return (t.endsWith('.0') ? t.slice(0, -2) : t) + 'K';
            },
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
          label: `Total Cost (${currencyLabel()})`,
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
            label: (ctx) => ` Total Cost: ${formatCurrency(list[ctx.dataIndex].totalCost)} (${list[ctx.dataIndex].costChange ? list[ctx.dataIndex].costChange + '%' : 'Baseline'})`,
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
            label: (ctx) => ctx.raw ? `${ctx.dataset.label}: ${formatNumber(ctx.raw)} ${perPeriodLabel()}` : '',
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
            label: (ctx) => ` ${ctx.label}: ${formatCurrency(ctx.raw)} (${((ctx.raw / (transportCost + handlingCost + fixedCost + holdingCost + surchargeCost)) * 100).toFixed(1)}%)`,
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
            label: (ctx) => `Flow: ${formatNumber(ctx.raw)} ${perPeriodLabel()} · Cost: ${formatCurrencyExact(costs[ctx.dataIndex])}/unit`,
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

/**
 * A dashed horizontal rule at the over-utilisation threshold, with its value.
 *
 * The bars mean "tight" or "fine" only against the line, and a legend entry
 * for a threshold is easy to miss; drawn on the plot it is unmissable and
 * costs no library.
 */
const utilisationThreshold = {
  id: 'ngUtilisationThreshold',
  afterDatasetsDraw(chart, _args, opts) {
    const { ctx, chartArea, scales } = chart;
    const pct = opts && opts.pct;
    if (!chartArea || !scales || !scales.y || typeof pct !== 'number') return;
    const y = scales.y.getPixelForValue(pct);
    if (!Number.isFinite(y) || y < chartArea.top || y > chartArea.bottom) return;

    ctx.save();
    ctx.setLineDash([5, 4]);
    ctx.lineWidth = 1.4;
    ctx.strokeStyle = '#dc2626';
    ctx.beginPath();
    ctx.moveTo(chartArea.left, y);
    ctx.lineTo(chartArea.right, y);
    ctx.stroke();
    ctx.setLineDash([]);

    const label = `${pct}% threshold`;
    ctx.font = '600 10.5px Inter, system-ui, sans-serif';
    const w = ctx.measureText(label).width + 12;
    const bx = chartArea.right - w;
    const by = Math.max(chartArea.top, y - 18);
    ctx.fillStyle = '#fef2f2';
    ctx.strokeStyle = '#fecaca';
    ctx.lineWidth = 1;
    if (ctx.roundRect) {
      ctx.beginPath();
      ctx.roundRect(bx, by, w, 16, 5);
      ctx.fill();
      ctx.stroke();
    } else {
      ctx.fillRect(bx, by, w, 16);
      ctx.strokeRect(bx, by, w, 16);
    }
    ctx.fillStyle = '#dc2626';
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'center';
    ctx.fillText(label, bx + w / 2, by + 8);
    ctx.restore();
  },
};

/**
 * Peak against average utilisation, per site.
 *
 * The chart this screen exists for. `utilization_pct` over a horizon is a
 * MEAN, and a site at 50% for the year and 95% in one month is drawn by that
 * mean as comfortable. Both bars, side by side, against the threshold.
 *
 * On a single-period solve the two bars are identical by definition, so only
 * one is drawn and the caption says why — two identical bars would imply a
 * seasonal reading the data does not carry.
 */
export function renderWarehouseUtilisationChart(canvasId, rows, thresholdPct, multiPeriod) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();
  const ctx = document.getElementById(canvasId);
  if (!ctx || !rows || rows.length === 0) return;

  const labels = rows.map((r) => r.facility_name || r.facility_id);
  const peak = rows.map((r) => Number(r.peak_utilization_pct) || 0);
  const avg = rows.map((r) => Number(r.avg_utilization_pct) || 0);

  // Colour by where the bar lands, not by series: the reader is looking for
  // "which of these is a problem", and a uniform palette makes them read the
  // axis to find out.
  const peakColour = peak.map((v) => (v >= 100 ? '#dc2626'
    : v >= thresholdPct ? '#d97706' : '#6B2FA0'));

  const datasets = [{
    label: multiPeriod ? 'Peak period' : 'Utilisation',
    data: peak,
    backgroundColor: peakColour,
    borderRadius: 4,
    barPercentage: 0.72,
    categoryPercentage: 0.72,
  }];
  if (multiPeriod) {
    datasets.push({
      label: 'Horizon average',
      data: avg,
      backgroundColor: '#d4bfe8',
      borderRadius: 4,
      barPercentage: 0.72,
      categoryPercentage: 0.72,
    });
  }

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { grid: { display: false }, ticks: { font: { family: 'Inter', size: 10.5 } } },
        y: {
          beginAtZero: true,
          title: { display: true, text: '% of stated capacity',
                   font: { family: 'Inter', size: 11 } },
          ticks: { font: { family: 'Inter', size: 10.5 }, callback: (v) => `${v}%` },
        },
      },
      plugins: {
        legend: {
          display: multiPeriod, position: 'bottom',
          labels: { usePointStyle: true, boxWidth: 8,
                    font: { family: 'Inter', size: 11 }, padding: 12 },
        },
        tooltip: {
          callbacks: {
            label: (c) => ` ${c.dataset.label}: ${Number(c.raw).toFixed(1)}%`,
            afterBody: (items) => {
              const row = rows[items[0].dataIndex];
              if (!row) return '';
              const lines = [];
              if (row.peak_period) lines.push(`Busiest period: ${row.peak_period}`);
              if (multiPeriod) {
                lines.push(`At or above ${thresholdPct}% in `
                  + `${row.bottleneck_periods_count} of ${row.periods_observed} periods`);
              }
              return lines;
            },
          },
        },
        ngUtilisationThreshold: { pct: thresholdPct },
      },
    },
    plugins: [utilisationThreshold],
  });
}

/**
 * Which sites the facility spend sits in.
 *
 * A doughnut because the question is "what share", and the share is the whole
 * finding — a total says how much, this says where to look. Transport is
 * excluded and the caption says so: it is not attributed to a site by the
 * model, and a pie that silently omitted the largest cost line would be read
 * as the network's cost.
 */
export function renderWarehouseSpendChart(canvasId, drivers) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();
  const ctx = document.getElementById(canvasId);
  if (!ctx || !drivers || drivers.length === 0) return;

  const palette = ['#6B2FA0', '#2563eb', '#16a34a', '#d97706', '#dc2626',
                   '#9a64c1', '#0891b2', '#65a30d', '#c2410c', '#7c3aad'];
  const total = drivers.reduce((sum, d) => sum + (Number(d.total_facility_cost) || 0), 0);

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: drivers.map((d) => d.facility_name || d.facility_id),
      datasets: [{
        data: drivers.map((d) => Number(d.total_facility_cost) || 0),
        backgroundColor: drivers.map((_, i) => palette[i % palette.length]),
        borderWidth: 2,
        borderColor: '#ffffff',
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'right',
          labels: { usePointStyle: true, boxWidth: 10,
                    font: { family: 'Inter', size: 11 }, padding: 10 },
        },
        tooltip: {
          callbacks: {
            label: (c) => ` ${c.label}: ${formatCurrency(c.raw)}`
              + (total > 0 ? ` (${((c.raw / total) * 100).toFixed(1)}%)` : ''),
          },
        },
      },
      cutout: '62%',
    },
  });
}

/**
 * How many sites sit in each state.
 *
 * A doughnut because the question is COMPOSITION — the reader wants the shape
 * of the network before reading any site's name, and "three of eleven are
 * tight" is a different sentence from a list of three names.
 *
 * The slice colours are the band colours the tags, the attention rows and the
 * table already use, so one state is one colour everywhere on this screen. A
 * band with nothing in it is left out rather than drawn as a zero slice: a
 * legend entry with nothing behind it is a label the reader has to dismiss.
 */
export function renderWarehouseStatusMixChart(canvasId, slices) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();
  const ctx = document.getElementById(canvasId);
  const rows = (slices || []).filter((s) => Number(s.value) > 0);
  if (!ctx || rows.length === 0) return;

  const total = rows.reduce((sum, s) => sum + Number(s.value), 0);

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: rows.map((s) => s.label),
      datasets: [{
        data: rows.map((s) => Number(s.value)),
        backgroundColor: rows.map((s) => s.color),
        borderWidth: 2,
        borderColor: '#ffffff',
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'bottom',
          labels: { usePointStyle: true, boxWidth: 9,
                    font: { family: 'Inter', size: 11 }, padding: 9 },
        },
        tooltip: {
          callbacks: {
            label: (c) => ` ${c.label}: ${c.raw} site${c.raw === 1 ? '' : 's'}`
              + (total > 0 ? ` of ${total}` : ''),
          },
        },
      },
      cutout: '58%',
    },
  });
}

/**
 * Rated capacity, and how much of it the busiest period uses — in UNITS.
 *
 * The utilisation chart answers "how full"; this one answers "how much room",
 * and they rank a network differently. A site at 95% of 200 units has ten
 * spare and a site at 80% of 40,000 has eight thousand: a planner deciding
 * where the next volume can go needs the second reading, and the per-cent
 * chart puts the wrong site at the top for that question.
 *
 * Stacked to the rated capacity so the BAR LENGTH is the capacity — the solid
 * part is what the peak period carries, the pale part is what is left. Nothing
 * new is measured here: both numbers are read from the report, and the
 * difference between them is drawn rather than computed as a KPI. The tooltip
 * names both source figures so the split can be checked against them.
 *
 * A peak ABOVE stated capacity gets its own red segment. The model can exceed
 * a stated capacity, and a chart that clipped the bar at 100% would hide the
 * single worst thing it could have to show.
 */
export function renderWarehouseHeadroomChart(canvasId, rows, multiPeriod) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();
  const ctx = document.getElementById(canvasId);
  const sites = (rows || []).filter(
    (r) => Number.isFinite(Number(r.rated_capacity_per_period))
        && Number(r.rated_capacity_per_period) > 0);
  if (!ctx || sites.length === 0) return;

  const cap = sites.map((r) => Number(r.rated_capacity_per_period));
  const peak = sites.map((r) => Number(r.peak_throughput_units) || 0);
  const over = peak.map((v, i) => Math.max(0, v - cap[i]));

  const datasets = [
    { label: multiPeriod ? 'Used in the busiest period' : 'Used',
      data: peak.map((v, i) => Math.min(v, cap[i])),
      backgroundColor: '#6B2FA0', borderRadius: 3, barPercentage: 0.68 },
    { label: 'Headroom',
      data: cap.map((c, i) => Math.max(0, c - peak[i])),
      backgroundColor: '#e5dcf0', borderRadius: 3, barPercentage: 0.68 },
  ];
  // Only when something actually exceeds. An always-present legend entry for
  // a condition no site is in reads as a warning the reader has to rule out.
  if (over.some((v) => v > 0)) {
    datasets.push({ label: 'Over stated capacity', data: over,
                    backgroundColor: '#dc2626', borderRadius: 3, barPercentage: 0.68 });
  }

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'bar',
    data: { labels: sites.map((r) => r.facility_name || r.facility_id), datasets },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: {
          stacked: true, beginAtZero: true,
          title: { display: true, text: perPeriodLabel(),
                   font: { family: 'Inter', size: 11 } },
          ticks: { font: { family: 'Inter', size: 10.5 },
                   callback: (v) => formatNumber(v) },
        },
        y: { stacked: true, grid: { display: false },
             ticks: { font: { family: 'Inter', size: 10.5 } } },
      },
      plugins: {
        legend: {
          position: 'bottom',
          labels: { usePointStyle: true, boxWidth: 8,
                    font: { family: 'Inter', size: 10.5 }, padding: 9 },
        },
        tooltip: {
          callbacks: {
            label: (c) => ` ${c.dataset.label}: ${formatNumber(c.raw)}`,
            afterBody: (items) => {
              const i = items[0].dataIndex;
              return [`Rated capacity: ${formatNumber(cap[i])}`,
                      `${multiPeriod ? 'Busiest period carries' : 'Carries'}: `
                        + `${formatNumber(peak[i])}`];
            },
          },
        },
      },
    },
  });
}

/**
 * Average stock against peak stock, per site.
 *
 * Two bars because the GAP between them is the finding. A site whose peak is
 * twice its average is holding for a season; a site whose peak sits on its
 * average is holding a constant buffer, and the two need different answers.
 *
 * Only sites the model reports a stock level for are passed in. A site with no
 * inventory decision is not a site holding nothing, and a zero bar would state
 * a measurement nobody took — the card shows the reason instead.
 */
export function renderWarehouseStockChart(canvasId, rows) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();
  const ctx = document.getElementById(canvasId);
  if (!ctx || !rows || rows.length === 0) return;

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: rows.map((r) => r.facility_name || r.facility_id),
      datasets: [
        { label: 'Peak', data: rows.map((r) => Number(r.peak_inventory_units) || 0),
          backgroundColor: '#6B2FA0', borderRadius: 4,
          barPercentage: 0.72, categoryPercentage: 0.72 },
        { label: 'Average', data: rows.map((r) => Number(r.avg_inventory_units) || 0),
          backgroundColor: '#d4bfe8', borderRadius: 4,
          barPercentage: 0.72, categoryPercentage: 0.72 },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { grid: { display: false }, ticks: { font: { family: 'Inter', size: 10.5 } } },
        y: {
          beginAtZero: true,
          title: { display: true, text: 'Units held',
                   font: { family: 'Inter', size: 11 } },
          ticks: { font: { family: 'Inter', size: 10.5 },
                   callback: (v) => formatNumber(v) },
        },
      },
      plugins: {
        legend: {
          position: 'bottom',
          labels: { usePointStyle: true, boxWidth: 8,
                    font: { family: 'Inter', size: 11 }, padding: 10 },
        },
        tooltip: {
          callbacks: { label: (c) => ` ${c.dataset.label}: ${formatNumber(c.raw)} units` },
        },
      },
    },
  });
}
