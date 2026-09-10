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
 * THE SCREEN'S COLOUR VOCABULARY, in one place.
 *
 * TWO LANGUAGES, and they must not be confused with each other:
 *
 *   STATUS   red, amber, green, blue, grey. Each names a health band and
 *            nothing else. The tags in `style.css`, the attention rows, the
 *            health table and the status doughnut all already spoke it.
 *   IDENTITY purples and teals, for telling one FACILITY from another. They
 *            carry no meaning beyond "not the same site as the last one".
 *
 * They were mixed. The spend doughnut ran through the status hues to colour
 * facilities, so a site could be drawn in the exact red the doughnut beside it
 * used for "over capacity" — and the utilisation chart drew a healthy site in
 * purple while the status doughnut drew it green, so one state had two
 * colours. A reader who has learned that red means trouble cannot be shown a
 * red that means "the third facility".
 */
export const BAND_COLOUR = {
  CRITICAL: '#dc2626',
  TIGHT: '#d97706',
  UNDERUSED: '#2563eb',
  HEALTHY: '#16a34a',
  NOT_OPERATING: '#c9c9d4',
};

/** Identity, deliberately clear of every status hue. Ordered so neighbouring
 *  slices stay distinguishable, including for the most common colour-vision
 *  deficiencies, where red-vs-green is exactly the pair that fails. */
export const IDENTITY_PALETTE = [
  // ALTERNATING FAMILIES, so neighbours never sit in the same hue. Ordered
  // purple / teal / slate rather than four purples then four teals: the ring
  // draws them in order, and four consecutive purples read as one wedge.
  // Lightness also steps, so the sequence survives a greyscale print and the
  // common colour-vision deficiencies.
  '#6B2FA0', '#0891b2', '#334155', '#b893d6',
  '#0e7490', '#7c3aad', '#64748b', '#22d3ee',
];

/** The remainder is not an identity, so it does not take an identity colour —
 *  a ninth slice wrapped the palette and came back the same purple as the
 *  first, making the largest facility and "everything else" look like one
 *  thing. */
export const OTHER_SLICE_COLOUR = '#c9c9d4';



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

/**
 * The share, written on the slice.
 *
 * A doughnut answers "how is this split", and a legend of names makes the
 * reader match colours back and forth to find out. The percentage goes where
 * the slice is.
 *
 * Only where it FITS. A label crossing its own arc is worse than none, so a
 * slice under `minPct` is left to the legend — which carries every value in
 * full. The "Other" slice this chart builds absorbs the long tail, so what is
 * left is nearly always big enough to label.
 */
const doughnutSliceShare = {
  id: 'ngDoughnutShare',
  afterDatasetsDraw(chart, _args, opts) {
    // OPT-IN. A registered plugin runs on every chart in the app, and this
    // one would have started writing percentages onto doughnuts nobody asked
    // to change — the scenario radar, the facility cost breakdown.
    if (!opts || opts.enabled !== true) return;
    const minPct = opts.minPct || 6;
    const meta = chart.getDatasetMeta(0);
    if (!meta || !meta.data) return;
    const values = chart.data.datasets[0]?.data || [];
    const total = values.reduce((sum, v) => sum + (Number(v) || 0), 0);
    if (total <= 0) return;

    const { ctx } = chart;
    ctx.save();
    ctx.font = '700 11px Inter, system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    meta.data.forEach((arc, i) => {
      const pct = ((Number(values[i]) || 0) / total) * 100;
      if (pct < minPct) return;
      const { startAngle, endAngle, innerRadius, outerRadius } = arc.getProps(
        ['startAngle', 'endAngle', 'innerRadius', 'outerRadius'], true);
      const angle = (startAngle + endAngle) / 2;
      const radius = innerRadius + (outerRadius - innerRadius) / 2;
      const x = arc.x + Math.cos(angle) * radius;
      const y = arc.y + Math.sin(angle) * radius;
      // ONE PRECISION for the whole chart. It switched at ten per cent, so a
      // single donut carried "18%" beside "8.3%" and "7.8%" — three numbers
      // that look like three different kinds of measurement.
      const text = `${Math.round(pct)}%`;
      // A label wider than the arc it names crosses into its neighbours. The
      // legend carries every value in full, so the crowded ones are simply
      // not drawn.
      const arcLength = Math.abs(endAngle - startAngle) * radius;
      if (ctx.measureText(text).width + 6 > arcLength) return;
      // Outlined, so it stays legible on every slice colour without needing
      // one text colour per palette entry.
      ctx.lineWidth = 3;
      ctx.strokeStyle = 'rgba(0,0,0,.30)';
      ctx.strokeText(text, x, y);
      ctx.fillStyle = '#ffffff';
      ctx.fillText(text, x, y);
    });
    ctx.restore();
  },
};

if (typeof Chart !== 'undefined' && Chart.register) {
  Chart.register(forecastAnnotations);
  Chart.register(doughnutSliceShare);
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
  // An uploaded forecast can exist for a market whose history this upload
  // does not carry. Bailing out on empty history alone told the reader "no
  // forecast can be produced" while a forecast sat in the response — the exact
  // opposite of the truth. There is nothing to draw only when BOTH halves are
  // empty.
  if (!histLabels.length && !foreLabels.length) {
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
  //
  // The pad ends on the last OBSERVED point so the forecast line joins the
  // history rather than starting a pixel to its right. With no history there is
  // no point to repeat and no join to make, and the forecast starts at index 0
  // — `new Array(-1)` is a RangeError, which is why this used to be unreachable
  // behind a guard that refused to draw a forecast without history at all.
  const joinPad = histLabels.length
    ? [...new Array(histLabels.length - 1).fill(null),
       DEMAND_HISTORY.northIndia[histLabels.length - 1]]
    : [];
  const foreData = [...joinPad, ...FORECAST.northIndia];
  const upperData = [...joinPad, ...FORECAST.upper];
  const lowerData = [...joinPad, ...FORECAST.lower];

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
          // Suppressed on the compact card, where the axis is 8.5px tall and
          // a title would take a third of the plot to say what the tick
          // labels already show.
          title: { display: !isCompact, text: 'Period',
                   font: { family: 'Inter', size: 10, weight: '600' } },
          ticks: {
            font: { family: 'Inter', size: isCompact ? 8.5 : 10 },
            // 45 DEGREES IS A LAST RESORT, NOT A DEFAULT.
            //
            // Angled tick labels are the single most common reason a chart
            // reads as unfinished, and on a period axis they are never
            // necessary: "2026-03" is short, and `maxTicksLimit` already
            // drops enough of them to fit. Left horizontal, with the limit
            // doing the work.
            maxRotation: 0,
            autoSkip: true,
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
/* ─── FOUR SCENARIO CHARTS REMOVED ──────────────────────────────────────
   `renderScenarioCostImpactChart`, `renderScenarioCapacityRiskChart`,
   `renderScenarioSlaChart` and `renderScenarioRadar` were exported here and
   called from nowhere — no module imported them and no canvas in index.html
   carried an id they draw into.

   They were not merely unused, they were WRONG, and would have been the
   moment anything wired them up. The cost chart divided every figure by
   100,000 and pinned its axis to `min: 10.0, max: 14.5` — a lakh range fitted
   to the demo network it was written against. A network costing ₹150,627 is
   1.5 on that scale and plots below the floor: an empty chart, no error.
   The risk chart mapped `capacityRisk` through the strings 'High'/'Medium'/
   'Low' when the backend has reported `HIGH`/`MEDIUM`/`LOW` since the KPI
   layer became authoritative, so every scenario scored 1, "Very Low".

   Deleted rather than fixed. Nothing asks for these; the scenario screen
   answers the same three questions with the comparison table and the cards,
   against the solved figures. Keeping a broken renderer alive because it
   might be wanted is how a demo-era hardcode survives into a client build.

   The removed source is in the session scratchpad if any of it is wanted
   back — but what would be wanted is the intent, not the code. */

export function renderFacilityThroughputChart(canvasId, row) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();
  const ctx = document.getElementById(canvasId);
  if (!ctx) return false;

  const host = ctx.parentElement;
  const stale = host && host.querySelector('.ng-series-absent');
  if (stale) stale.remove();

  const series = (row && row.throughput_by_period) || {};
  const periods = Object.keys(series);
  const capacity = Number(row && row.rated_capacity_per_period) || 0;

  if (periods.length < 2) {
    // One period is not a horizon, and two points invented between them is
    // the defect this replaced.
    ctx.style.display = 'none';
    if (host) {
      const note = document.createElement('div');
      note.className = 'ng-series-absent wh-status-note';
      note.textContent = periods.length === 1
        ? 'This solve modelled a single period, so there is no history to plot '
          + 'for this site — its one reading is on the cards above.'
        : 'This solve reported no per-period throughput for this site.';
      host.appendChild(note);
    }
    return false;
  }
  ctx.style.display = '';

  const values = periods.map((key) => Number(series[key]) || 0);

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'line',
    data: {
      labels: periods,
      datasets: [
        ...(capacity > 0 ? [{
          label: 'Capacity',
          data: periods.map(() => capacity),
          borderColor: '#dc2626',
          borderWidth: 2,
          borderDash: [5, 5],
          pointRadius: 0,
          fill: false,
        }] : []),
        {
          label: 'Throughput',
          data: values,
          borderColor: '#6B2FA0',
          backgroundColor: 'rgba(107,47,160,.10)',
          borderWidth: 2.5,
          pointRadius: 2.5,
          pointHoverRadius: 5,
          fill: true,
          tension: 0.3,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          position: 'top', align: 'end',
          labels: { usePointStyle: true, boxWidth: 8,
                    font: { family: 'Inter', size: 11 }, padding: 12 },
        },
        tooltip: {
          callbacks: {
            label: (c) => ` ${c.dataset.label}: ${formatNumber(c.raw)} ${perPeriodLabel()}`,
          },
        },
      },
      scales: {
        x: { grid: { display: false },
             // TIME STAYS ON X. This is the one axis arrangement that does
             // not become a bar chart: periods run left to right because that
             // is how a horizon is read.
             title: { display: true, text: 'Period',
                      font: { family: 'Inter', size: 10, weight: '600' } },
             // The only axis in the product without these. Chart.js angles
             // tick labels on its own once they stop fitting, so a horizon of
             // twenty-four periods produced the rotated axis every other
             // chart here was fixed to avoid — silently, and only on the
             // networks with enough periods to trigger it.
             ticks: { font: { family: 'Inter', size: 10 },
                      maxRotation: 0, autoSkip: true, maxTicksLimit: 16 } },
        y: {
          beginAtZero: true,
          grid: { color: 'rgba(0,0,0,.05)' },
          ticks: {
            font: { family: 'Inter', size: 10 },
            callback: (v) => formatNumber(v),
          },
          // WHAT is being counted, then the unit. The axis read
          // "units/month" alone, which names a rate and leaves "a rate of
          // what" to the reader — on a chart plotting throughput against
          // rated capacity, that is the whole question.
          // `perPeriodLabel()` already carries the unit, so it is appended
          // rather than prefixed with a second "units".
          title: { display: true, text: `Throughput (${perPeriodLabel()})`,
                   font: { family: 'Inter', size: 10, weight: '600' } },
        },
      },
    },
  });
  return true;
}

/**
 * What one facility's cost is made of — from the solve, not from constants.
 *
 * THIS CHART USED TO BE INVENTED. It read:
 *
 *     const transportCost = isDC ? 580000 : 720000;
 *     const holdingCost   = 140000;
 *     const surchargeCost = 45000;
 *     const fixedCost     = (facility.fixedCost || 100) * 100000 / 12;
 *
 * — five hard-coded figures and two magic multipliers, drawn as a doughnut
 * labelled with the site's name and rendered in the same currency as every
 * real number on the screen. A reader had no way to tell it apart from the
 * solved values beside it. It also decided the site's role from the spelling
 * of its id (`startsWith('DC_')`), which this product settled long ago.
 *
 * It now takes the components the engine actually reports for that facility —
 * fixed, opening, handling and holding — which are the same four that sum to
 * the `total_facility_cost` on the card above it, so the two agree by
 * construction. Transport is NOT among them and is not implied: the model
 * does not attribute it to a site.
 *
 * `breakdown` is null when the solve reported no cost for this facility, and
 * the chart then draws nothing rather than a ring of zeroes.
 */
export function renderFacilityCostBreakdownChart(canvasId, breakdown) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;

  const parts = [
    { label: 'Fixed', value: Number(breakdown?.fixed_cost) || 0 },
    { label: 'Opening', value: Number(breakdown?.opening_cost) || 0 },
    { label: 'Handling', value: Number(breakdown?.handling_cost) || 0 },
    { label: 'Holding', value: Number(breakdown?.holding_cost) || 0 },
  ].filter((part) => part.value > 0);

  // WHAT THIS CHART ACTUALLY SHOWS, said on the card beside it.
  //
  // Zero-value components are dropped below, because a slice of nothing is not
  // a cost. So a fixed caption listing four components describes a ring that
  // usually has three: inventory is decided for the NETWORK on this model, not
  // per site, so `holding_cost` is zero on most sites and "and holding"
  // promises a slice that is not there.
  //
  // Written from the parts that survive the filter instead — the caption and
  // the picture cannot disagree because one is derived from the other.
  const subtitle = document.getElementById('dash-costs-subtitle');
  if (subtitle) {
    const names = parts.map((p) => p.label.toLowerCase());
    subtitle.textContent = names.length
      ? `${names.slice(0, -1).join(', ')}${names.length > 1 ? ' and ' : ''}`
        + `${names[names.length - 1]} — the components this solve attributed `
        + 'to this site. Transport is not attributed to a site.'
      : 'No facility cost was attributed to this site.';
    // Sentence case: the list is built from labels that are capitalised for
    // the legend.
    subtitle.textContent = subtitle.textContent.charAt(0).toUpperCase()
      + subtitle.textContent.slice(1);
  }

  const host = ctx.parentElement;
  const stale = host && host.querySelector('.ng-cost-absent');
  if (stale) stale.remove();
  if (parts.length === 0) {
    // A ring of zeroes reads as "this site costs nothing".
    ctx.style.display = 'none';
    if (host) {
      const note = document.createElement('div');
      note.className = 'ng-cost-absent wh-status-note';
      note.textContent = 'This solve reported no facility cost for this site, '
        + 'so there is nothing to break down.';
      host.appendChild(note);
    }
    return;
  }
  ctx.style.display = '';

  const total = parts.reduce((sum, part) => sum + part.value, 0);

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: parts.map((part) => part.label),
      datasets: [{
        data: parts.map((part) => part.value),
        // Identity, not status: a cost component is not a health band.
        backgroundColor: parts.map((_, i) => IDENTITY_PALETTE[i % IDENTITY_PALETTE.length]),
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
          labels: {
            usePointStyle: true, boxWidth: 10,
            font: { family: 'Inter', size: 11 }, padding: 12,
            generateLabels: (chart) => {
              const ds = chart.data.datasets[0] || {};
              return chart.data.labels.map((label, i) => ({
                text: `${label} — ${formatCurrency(Number(ds.data[i]) || 0)}`,
                fillStyle: ds.backgroundColor[i],
                strokeStyle: ds.backgroundColor[i],
                pointStyle: 'circle',
                index: i,
              }));
            },
          },
        },
        tooltip: {
          callbacks: {
            label: (c) => ` ${c.label}: ${formatCurrency(c.raw)}`
              + (total > 0 ? ` (${((c.raw / total) * 100).toFixed(1)}%)` : ''),
          },
        },
        ngDoughnutShare: { enabled: true },
      },
      cutout: '68%',
    },
  });
}

// How many corridors this chart draws before it stops.
//:
// The others cap at twelve upstream; this one drew every corridor attached to
// the site into a 220px box. Measured at nine corridors the ticks were 18px
// apart against 13px labels — five pixels of clearance — and a hub with
// twenty would have overlapped outright. Twelve is what fits at a readable
// row height, and the telemetry table directly below the chart carries all of
// them, so nothing is hidden by capping it.
const LANE_CHART_LIMIT = 12;

export function renderFacilityLaneFlowsChart(canvasId, connectedLanes, facilityId) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();

  const ctx = document.getElementById(canvasId);
  if (!ctx || !connectedLanes || connectedLanes.length === 0) return;

  // THE BUSIEST FIRST, so the ones that fall off the end are the ones that
  // matter least. Corridors with no solved volume sort last rather than being
  // dropped: a lane the plan did not use is a finding, and it is one the
  // reader should see if there is room for it.
  const ordered = [...connectedLanes].sort((a, b) => {
    const av = Number.isFinite(Number(a.flow)) ? Number(a.flow) : -1;
    const bv = Number.isFinite(Number(b.flow)) ? Number(b.flow) : -1;
    return bv - av;
  });
  const shown = ordered.slice(0, LANE_CHART_LIMIT);
  sizeBarChartHost(canvasId, shown.length);

  const labels = shown.map(l => l.label);
  const flows = shown.map(l => l.flow);
  const costs = shown.map(l => l.cost);

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
          beginAtZero: true,
          grid: { color: '#f0f0f5' },
          // The bars are a VOLUME and the axis said so nowhere: the only
          // place the unit appeared was the dataset label, and the legend is
          // switched off on this chart.
          title: { display: true, text: `Volume moved (${perPeriodLabel()})`,
                   font: { family: 'Inter', size: 11, weight: '600' } },
          ticks: { font: { family: 'Inter', size: 10 },
                   callback: (v) => formatNumber(v) },
        },
        y: {
          grid: { display: false },
          title: { display: true, text: 'Corridor',
                   font: { family: 'Inter', size: 11, weight: '600' } },
          ticks: { font: { family: 'Inter', size: 11, weight: '500' },
                   autoSkip: false,
                   // A corridor label is "Plant A -> Bengaluru Distribution
                   // Centre": two site names and an arrow. At full length it
                   // took a third of the card and pushed the plot into a
                   // strip. Trimmed with the ellipsis that says it was cut;
                   // the tooltip carries the whole thing.
                   callback(value) {
                     const text = this.getLabelForValue(value);
                     return text.length > 30 ? `${text.slice(0, 29)}…` : text;
                   } },
        },
      },
    },
  });

  // What the caller needs to caption it honestly: how many corridors this site
  // has, and how many of them are on the chart.
  return { shown: shown.length, total: connectedLanes.length };
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
    if (!chartArea || !scales || typeof pct !== 'number') return;

    // THE LINE FOLLOWS THE VALUE AXIS, WHICHEVER ONE THAT IS.
    //
    // This drew a horizontal rule at `scales.y.getPixelForValue(pct)`, which
    // was right while the chart was columns of facilities. The chart is bars
    // now, so the percentages run along X — and a horizontal rule on it marks
    // a POSITION IN THE LIST OF SITES rather than a utilisation. It would
    // have gone on drawing, in the right colour, in the wrong place, which is
    // the worst way for a threshold marker to fail.
    const horizontal = chart.options && chart.options.indexAxis === 'y';
    const valueScale = horizontal ? scales.x : scales.y;
    if (!valueScale) return;
    const at = valueScale.getPixelForValue(pct);
    if (!Number.isFinite(at)) return;
    if (horizontal ? (at < chartArea.left || at > chartArea.right)
                   : (at < chartArea.top || at > chartArea.bottom)) return;

    ctx.save();
    ctx.setLineDash([5, 4]);
    ctx.lineWidth = 1.4;
    ctx.strokeStyle = '#dc2626';
    ctx.beginPath();
    if (horizontal) {
      ctx.moveTo(at, chartArea.top);
      ctx.lineTo(at, chartArea.bottom);
    } else {
      ctx.moveTo(chartArea.left, at);
      ctx.lineTo(chartArea.right, at);
    }
    ctx.stroke();
    ctx.setLineDash([]);

    const label = `${pct}% threshold`;
    ctx.font = '600 10.5px Inter, system-ui, sans-serif';
    const w = ctx.measureText(label).width + 12;
    // The chip sits beside the line it names: at the top of a vertical rule,
    // above the right-hand end of a horizontal one. Clamped so it never
    // leaves the plot area on a narrow card.
    const bx = horizontal
      ? Math.min(Math.max(chartArea.left, at - w / 2), chartArea.right - w)
      : chartArea.right - w;
    const by = horizontal
      ? chartArea.top + 2
      : Math.max(chartArea.top, at - 18);
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
/**
 * A legend in the DOM, beside the canvas rather than inside it.
 *
 * A canvas legend is drawn text: it cannot ellipsise to the space it has, it
 * cannot carry a title attribute, and it clips whatever does not fit — which
 * is how "Newark Distributi… — $22" reached the screen, losing the digits
 * that were the point. Truncating the NAME harder only moved the problem,
 * because the amount is what got cut.
 *
 * As DOM: the name flexes and ellipsises to whatever room is left, the value
 * is reserved and never truncates, and hovering a row shows the full name
 * through the browser's own tooltip — no re-implementation.
 */
export function renderHtmlLegend(hostId, items) {
  const host = document.getElementById(hostId);
  if (!host) return;
  host.innerHTML = items.map((item) => `<li class="ng-legend-row" title="${
    String(item.label).replace(/"/g, '&quot;')}">
      <span class="ng-legend-dot" style="background:${item.colour}"></span>
      <span class="ng-legend-name">${String(item.label)
        .replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]))}</span>
      <span class="ng-legend-value">${item.value}</span>
    </li>`).join('');
}

/**
 * A facility name short enough to sit under a bar.
 *
 * Drops the role words every site on the axis shares — the reader already
 * knows they are looking at distribution centres, and repeating it six times
 * spends the space that would have told them WHICH one. The full name stays
 * in the tooltip, so nothing is lost, only deferred.
 */
/**
 * A facility name cut to fit under a bar.
 *
 * A PLAIN CHARACTER CUT, deliberately. This used to strip the role words every
 * site shares — "Bengaluru Distribution Centre" became "Bengaluru" — which
 * read well but had two problems: it depended on a list of role spellings that
 * had to be kept in step with whatever an upload happened to say (it shipped
 * once knowing "Distribution Center" but not "Manufacturing", so a DC came out
 * as "Atlanta" and a plant as "Los Angeles Manuf…" on the same axis), and the
 * labels came out at wildly different lengths.
 *
 * Cutting at a fixed width has neither problem: it works on any name in any
 * language, and every label is the same size, which is what makes an axis
 * scannable. The full name is always one hover away — the bar's tooltip on the
 * canvas charts, the row's own title on the DOM legends — so nothing is lost.
 */
export function shortFacilityLabel(name, limit = 7) {
  const out = String(name || '').trim();
  // A real ellipsis, not three dots.
  return out.length > limit ? `${out.slice(0, limit).trimEnd()}\u2026` : out;
}

/** Characters per line, and how many lines an axis label may run to. */
const AXIS_LINE_CHARS = 13;
const AXIS_MAX_LINES = 2;

/** One name, wrapped on spaces into at most `AXIS_MAX_LINES` lines. */
function wrapLabel(name, perLine = AXIS_LINE_CHARS) {
  const words = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (!words.length) return [''];
  const lines = [];
  let line = '';
  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word;
    if (candidate.length <= perLine) { line = candidate; continue; }
    if (line) lines.push(line);
    if (lines.length === AXIS_MAX_LINES) {
      // Out of lines with words still to place. The ellipsis goes on the last
      // one so the reader knows the name continues.
      lines[AXIS_MAX_LINES - 1] = `${lines[AXIS_MAX_LINES - 1]}\u2026`;
      return lines;
    }
    // A single word longer than the line is the only case that gets cut
    // mid-word, because there is nowhere else to break it.
    line = word.length > perLine ? `${word.slice(0, perLine - 1)}\u2026` : word;
  }
  if (line) lines.push(line);
  return lines.slice(0, AXIS_MAX_LINES);
}

/**
 * A set of axis labels, wrapped rather than truncated.
 *
 * WHY NOT A FIXED CUT. Seven characters is the right width for "Central DC"
 * and the wrong one for a network whose sites are named for where they are
 * and what they do. On the Case-16 network the five drawn sites are three
 * "… Distribution Centre"s and two "… Manufacturing Plant"s, and a
 * seven-character cut rendered them "Western\u2026", "Norther\u2026",
 * "Central\u2026", "Eastern\u2026", "Souther\u2026" — technically distinct,
 * and hiding the one distinction a reader of a capacity chart needs, which is
 * which bars are plants. It also cut "Northern" and "Southern" one letter
 * short of a word they had room for, which reads as a misspelling rather than
 * an abbreviation.
 *
 * Wrapping on spaces keeps whole words: "Western" over "Distribution\u2026",
 * "Northern" over "Manufacturing\u2026". Chart.js draws an array label as one
 * line per element, so this costs a second row of axis and nothing else, and
 * the tooltip still carries the whole name for anyone who needs to be sure.
 *
 * The width then WIDENS if it has to. Two sites whose names agree for the
 * first two lines would draw two identical labels, so the line length grows
 * until every label on the chart is distinct — spending width only where it
 * buys a distinction.
 */
// Vertical room per bar. A tick label is 13px at the 10.5px font these
// charts use (12px on the corridor chart), so 26 leaves a clear half-row
// between one name and the next — enough that a label which wraps to two
// lines still does not touch its neighbour.
const BAR_ROW_PX = 26;
// What the plot needs on top of the bars: the value axis, its title, and the
// legend where there is one.
const BAR_CHROME_PX = 86;
// Never a strip, never taller than a screen.
const BAR_MIN_PX = 190;
const BAR_MAX_PX = 640;

/**
 * Give a horizontal bar chart the height its categories need.
 *
 * The container heights are literals in the markup — 300px, 260px, 220px —
 * chosen against a five-site demo. Measured on the running application: at
 * twelve sites the ticks are 20px apart with 13px labels, and at twenty they
 * are 12px apart, which is an overlap. A name past the single-line budget
 * wraps to two lines and overlaps at twelve.
 *
 * Columns get more cramped as categories are added; bars get longer. This is
 * the second half of turning these charts on their side, and without it the
 * fix trades rotated labels for colliding ones.
 *
 * Bounded at both ends: `BAR_MIN_PX` so a two-site chart is not a strip, and
 * `BAR_MAX_PX` so a large network does not produce a card whose bottom is off
 * the screen. Past that the renderers already cap how many rows they draw and
 * the table below carries the rest.
 */
export function sizeBarChartHost(canvasId, categories) {
  if (typeof document === 'undefined') return;
  const canvas = document.getElementById(canvasId);
  const host = canvas && canvas.parentElement;
  if (!host || !host.classList.contains('chart-wrap')) return;
  const wanted = Math.round(Number(categories) || 0) * BAR_ROW_PX + BAR_CHROME_PX;
  const height = Math.max(BAR_MIN_PX, Math.min(BAR_MAX_PX, wanted));
  host.style.height = `${height}px`;
}

export function axisFacilityLabels(names, horizontal = false) {
  const full = (names || []).map((n) => String(n || '').trim());
  const distinct = new Set(full).size;

  // A BAR CHART'S CATEGORY AXIS IS NOT A COLUMN CHART'S.
  //
  // The 13-character budget is what a COLUMN has: one column-width to write a
  // site name in, which is why these names were being cut to "Western
  // Distribution…" and stacked over two lines. Turned on its side the name
  // runs along the left of the plot with four or five times the room, and
  // wrapping it there throws that room away — so a horizontal axis takes the
  // whole name on one line, and only reaches for the wrap on a name long
  // enough to eat the plot area.
  if (horizontal) {
    const LONGEST = 34;
    return full.map((n) => (n.length <= LONGEST
      ? [n]
      : wrapLabel(n, Math.ceil(LONGEST / 2))));
  }

  for (let perLine = AXIS_LINE_CHARS; perLine <= 26; perLine += 3) {
    const wrapped = full.map((n) => wrapLabel(n, perLine));
    if (new Set(wrapped.map((w) => w.join(' '))).size === distinct) return wrapped;
  }
  return full.map((n) => wrapLabel(n, 26));
}

export function renderWarehouseUtilisationChart(canvasId, rows, thresholdPct, multiPeriod) {
  if (chartInstances[canvasId]) chartInstances[canvasId].destroy();
  const ctx = document.getElementById(canvasId);
  if (!ctx || !rows || rows.length === 0) return;

  // SHORT ON THE AXIS, FULL IN THE TOOLTIP.
  //
  // "Bengaluru Distribution Centre" and five siblings, set at an angle along
  // the foot of the chart, overlapped each other and pushed the plot area up.
  // Cut to a common width they stay level and readable, and the hover gives
  // back the whole name for anyone who needs to be sure which site they are on.
  const labels = axisFacilityLabels(
    rows.map((r) => r.facility_name || r.facility_id), true);
  // The box before the chart, so Chart.js lays out against the real height.
  sizeBarChartHost(canvasId, rows.length);
  const peak = rows.map((r) => Number(r.peak_utilization_pct) || 0);
  const avg = rows.map((r) => Number(r.avg_utilization_pct) || 0);

  // Colour by STATE, not by series: the reader is looking for "which of these
  // is a problem", and a uniform palette makes them read the axis to find out.
  //
  // From the row's own band, so this chart says the same thing in the same
  // colour as the tag beside it, the row in the table and the status
  // doughnut. It used to draw everything under the threshold in purple, which
  // made an under-used site (blue everywhere else) purple here and a healthy
  // one (green everywhere else) purple too — one state, three colours.
  const peakColour = rows.map((r) => BAND_COLOUR[r.health_band]
    || BAND_COLOUR.HEALTHY);

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
      // BARS, NOT COLUMNS — because the categories are NAMES.
      //
      // A site is called "Bengaluru Distribution Centre", and a column chart
      // has one column-width of horizontal room to write that in. Every such
      // chart in this product had the same three symptoms: labels wrapped to
      // three lines, or set at an angle, or trimmed to the point where two
      // sites read the same — and the plot area squeezed upward to make room.
      //
      // Turned on its side the name runs along the axis the way it is read,
      // at full length, and the chart grows DOWNWARD as sites are added
      // rather than getting more cramped. The rule applied across the
      // product: a category that is a name reads horizontally; a category
      // that is TIME stays a column chart, because time runs left to right
      // and nobody reads a calendar sideways.
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: {
          beginAtZero: true,
          title: { display: true, text: 'Share of stated capacity used (%)',
                   font: { family: 'Inter', size: 11, weight: '600' } },
          ticks: { font: { family: 'Inter', size: 10.5 }, callback: (v) => `${v}%` },
        },
        y: {
          grid: { display: false },
          title: { display: true, text: 'Facility',
                   font: { family: 'Inter', size: 11, weight: '600' } },
          ticks: { font: { family: 'Inter', size: 10.5 }, autoSkip: false },
        },
      },
      plugins: {
        legend: {
          display: multiPeriod, position: 'bottom',
          labels: {
            usePointStyle: true, boxWidth: 8,
            font: { family: 'Inter', size: 11 }, padding: 12,
            // A NEUTRAL SWATCH FOR THE PEAK SERIES, AND IT SAYS SO.
            //
            // Its bars are coloured by health band, so Chart.js took the
            // first bar's colour for the legend dot — and a red dot beside
            // "Peak period" says the series is red, when red means "over
            // capacity" on this screen and three of the bars are not.
            //
            // Neutral fixed that and left a second problem: a key naming a
            // colour that appears nowhere on the chart. A reader who has just
            // read the status doughnut beside this — where red, amber, blue
            // and green are labelled with the states they mean — finds those
            // same four colours here under a grey dot that explains none of
            // them. So the label states what the colour is doing; the dot
            // then reads as "this series", which is all it was ever claiming.
            generateLabels: (chart) => chart.data.datasets.map((ds, i) => ({
              text: i === 0 ? `${ds.label} — colour shows the site's state` : ds.label,
              fillStyle: i === 0 ? '#5a5a72' : ds.backgroundColor,
              strokeStyle: i === 0 ? '#5a5a72' : ds.backgroundColor,
              pointStyle: 'circle',
              datasetIndex: i,
            })),
          },
        },
        tooltip: {
          callbacks: {
            // The FULL name heads the tooltip: the axis carries the short one,
            // so this is where a reader confirms which site they are on.
            title: (items) => {
              const row = rows[items[0].dataIndex];
              return row ? (row.facility_name || row.facility_id) : '';
            },
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

  // Identity, not status: these slices are facilities, and a facility is not
  // a health band. See IDENTITY_PALETTE.
  const palette = IDENTITY_PALETTE;
  const total = drivers.reduce((sum, d) => sum + (Number(d.total_facility_cost) || 0), 0);

  // THE SLICES ADD UP TO THE WHOLE.
  //
  // Beyond about eight, slices are too thin to read and the legend is longer
  // than the chart — but dropping the tail leaves a doughnut whose parts do
  // not sum to the total anyone states beside it. So the tail is COMBINED,
  // named, and counted: "Other (6 sites)" is a slice a reader can reason
  // about, where six missing slices are not.
  const MAX_SLICES = 8;
  const ranked = [...drivers].sort(
    (a, b) => (Number(b.total_facility_cost) || 0) - (Number(a.total_facility_cost) || 0));
  const head = ranked.slice(0, MAX_SLICES);
  const tail = ranked.slice(MAX_SLICES);
  const slices = head.map((d) => ({
    label: d.facility_name || d.facility_id,
    value: Number(d.total_facility_cost) || 0,
  }));
  if (tail.length) {
    slices.push({
      label: `Other (${tail.length} site${tail.length === 1 ? '' : 's'})`,
      value: tail.reduce((sum, d) => sum + (Number(d.total_facility_cost) || 0), 0),
      isOther: true,
    });
  }

  // The legend lives in the DOM beside the canvas — see renderHtmlLegend.
  renderHtmlLegend(`legend-${canvasId.replace(/^chart-/, '')}`,
    slices.map((sl, i) => ({
      label: sl.label,
      colour: sl.isOther ? OTHER_SLICE_COLOUR : palette[i % palette.length],
      value: formatCurrency(sl.value),
    })));

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: slices.map((sl) => sl.label),
      datasets: [{
        data: slices.map((sl) => sl.value),
        backgroundColor: slices.map((sl, i) => (sl.isOther
          ? OTHER_SLICE_COLOUR : palette[i % palette.length])),
        borderWidth: 2,
        borderColor: '#ffffff',
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (c) => ` ${c.label}: ${formatCurrency(c.raw)}`
              + (total > 0 ? ` (${((c.raw / total) * 100).toFixed(1)}%)` : ''),
          },
        },
        ngDoughnutShare: { enabled: true },
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
          labels: {
            usePointStyle: true, boxWidth: 9,
            font: { family: 'Inter', size: 11 }, padding: 9,
            // The count and the share, on the legend. "Tight" alone tells the
            // reader a state exists in this network but not how much of it is
            // in that state, which is the whole question a composition chart
            // is drawn to answer.
            generateLabels: (chart) => {
              const ds = chart.data.datasets[0] || {};
              return chart.data.labels.map((label, i) => {
                const value = Number(ds.data[i]) || 0;
                const share = total > 0 ? (value / total) * 100 : null;
                return {
                  // COUNT ONLY. The share is drawn on the slice, and printing
                  // it twice made the legend the longest thing in the card.
                  text: `${label} — ${value}`,
                  fillStyle: ds.backgroundColor[i],
                  strokeStyle: ds.backgroundColor[i],
                  pointStyle: 'circle',
                  index: i,
                };
              });
            },
          },
        },
        tooltip: {
          callbacks: {
            label: (c) => ` ${c.label}: ${c.raw} site${c.raw === 1 ? '' : 's'}`
              + (total > 0 ? ` of ${total}` : ''),
          },
        },
        ngDoughnutShare: { enabled: true },
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
  sizeBarChartHost(canvasId, sites.length);

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
    // FULL ON THE AXIS where the axis has room for it. This chart is
    // horizontal, so a name runs along the left rather than into a
    // column-width — and `axisFacilityLabels(..., true)` gives it the whole
    // name on one line, reaching for a wrap only past 34 characters.
    data: {
      labels: axisFacilityLabels(
        sites.map((r) => r.facility_name || r.facility_id), true),
      datasets,
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: {
          stacked: true, beginAtZero: true,
          // WHAT the units are, not only how often. The axis read
          // "units/month", which names a rate and not a quantity — on a chart
          // whose two stacked segments are throughput and the room left above
          // it, "of what" is the question a reader has.
          title: { display: true,
                   text: `Rated capacity, used and spare (${perPeriodLabel()})`,
                   font: { family: 'Inter', size: 11, weight: '600' } },
          ticks: { font: { family: 'Inter', size: 10.5 },
                   callback: (v) => formatNumber(v) },
        },
        y: { stacked: true, grid: { display: false },
             title: { display: true, text: 'Facility',
                      font: { family: 'Inter', size: 11, weight: '600' } },
             ticks: { font: { family: 'Inter', size: 10.5 }, autoSkip: false } },
      },
      plugins: {
        legend: {
          position: 'bottom',
          labels: { usePointStyle: true, boxWidth: 8,
                    font: { family: 'Inter', size: 10.5 }, padding: 9 },
        },
        tooltip: {
          callbacks: {
            // The full name heads the tooltip, since the axis carries a
            // shortened one.
            title: (items) => {
              const r = sites[items[0].dataIndex];
              return r ? (r.facility_name || r.facility_id) : '';
            },
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
  sizeBarChartHost(canvasId, rows.length);

  // A NETWORK THAT REPORTS ZERO EVERYWHERE STILL NEEDS A READABLE AXIS.
  //
  // A reported zero is a measurement, so those sites are drawn — but with
  // every value 0 the axis had no range to build from. Chart.js fell back to
  // a 0–1 domain, divided it into eleven fractional ticks, and the tick
  // callback rounds to whole units: the axis read 1, 1, 1, 1, 1, 1, 0, 0, 0,
  // 0, 0 beside a card saying no site holds stock. Six labels claiming a
  // quantity nothing on the chart has.
  //
  // Integer ticks, and a ceiling of 1 where there is nothing to scale to, so
  // the axis reads 0 to 1 and every label is a number some bar could take.
  const peakValues = rows.map((r) => Number(r.peak_inventory_units) || 0);
  const avgValues = rows.map((r) => Number(r.avg_inventory_units) || 0);
  const highest = Math.max(0, ...peakValues, ...avgValues);

  chartInstances[canvasId] = new Chart(ctx, {
    type: 'bar',
    data: {
      // Horizontal since this chart stopped being columns of names, so the
      // axis takes the whole name; the tooltip still carries it in full for
      // anything past the limit.
      labels: axisFacilityLabels(
        rows.map((r) => r.facility_name || r.facility_id), true),
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
      // Bars, for the same reason as every other name-categorised chart on
      // this screen: the categories are site names, and a column has one
      // column-width to write one in.
      indexAxis: 'y',
      scales: {
        x: {
          beginAtZero: true,
          ...(highest > 0 ? {} : { suggestedMax: 1 }),
          title: { display: true, text: 'Units held',
                   font: { family: 'Inter', size: 11, weight: '600' } },
          ticks: { font: { family: 'Inter', size: 10.5 },
                   // Whole units, matching what the callback prints. Without
                   // it the ticks are fractional and the labels repeat.
                   precision: 0,
                   callback: (v) => formatNumber(v) },
        },
        y: {
          grid: { display: false },
          title: { display: true, text: 'Facility',
                   font: { family: 'Inter', size: 11, weight: '600' } },
          ticks: { font: { family: 'Inter', size: 10.5 }, autoSkip: false },
        },
      },
      plugins: {
        legend: {
          position: 'bottom',
          labels: { usePointStyle: true, boxWidth: 8,
                    font: { family: 'Inter', size: 11 }, padding: 10 },
        },
        tooltip: {
          callbacks: {
            // The FULL name heads the tooltip; the axis carries the short one.
            title: (items) => {
              const row = rows[items[0].dataIndex];
              return row ? (row.facility_name || row.facility_id) : '';
            },
            label: (c) => ` ${c.dataset.label}: ${formatNumber(c.raw)} units`,
          },
        },
      },
    },
  });
}
