/**
 * NetGravity — Insight Deep Dive
 * ===============================
 * The full-page view of one finding. Reached by clicking a card in Home's
 * attention feed.
 *
 *   Home attention feed → DEEP DIVE (this page)
 *                          └→ "Run a scenario" → Scenario Planning tab
 *                          └→ "Open in Digital Twin" → Twin tab, on that site
 *
 * What this page shows, and only this
 * -----------------------------------
 * The finding as the engine stated it — headline, full narrative, theme,
 * severity — the evidence it cites with each metric's authoritative value and
 * source, and the recommendation the engine derived from the whole network.
 * Then two ways to act: test a change as a scenario, or look at the site in the
 * twin.
 *
 * What it used to show
 * --------------------
 * The previous version of this file was 836 lines and was annotated at the top
 * as "PROTOTYPE / MOCKED". It presented, as ordinary page content:
 *
 *   * a seven-month "Actual / Projected" utilisation trend chart, generated
 *     from a hash of the insight's id — no such series exists anywhere in this
 *     system, and none is computed;
 *   * a before/after cost table whose delta came from
 *     `synth(id, 'cost', 6, 18)`, i.e. between ₹6L and ₹18L chosen by hashing a
 *     string;
 *   * a service level of "94.6%" as a hardcoded literal;
 *   * an "amount at risk" of ₹8–32L, likewise hashed;
 *   * an editable "shift 8–18% of volume" slider recomputing all of the above;
 *   * a drafted e-mail to "Priya Mehta (Regional Planning Analyst)", a person
 *     who does not exist, cc'd to "West Region Operations";
 *   * Approve / Reject buttons which changed their own label and nothing else,
 *     and an "action taken" state asserting that something had happened.
 *
 * None of it was reachable, because nothing populated the insight feed — so it
 * had never been seen. Wiring the feed up made every line of it live, and a
 * hashed rupee figure beside a real one is indistinguishable to a reader. The
 * whole apparatus is gone rather than relabelled: a caveat under a fabricated
 * chart does not make the chart true, and this application's central promise is
 * that a number on screen came from the solver.
 *
 * The scenario button is the honest version of the shift slider. A planner who
 * wants to know what moving 12% of volume would cost can have that answered by
 * the MILP, which is what Scenario Planning is for.
 *
 * What this phase added back — and where each figure comes from
 * ------------------------------------------------------------
 * The page now carries the prototype's LAYOUT: a headline stat banner, a
 * two-column split with a chart on the left and the recommendation on the
 * right, a metric row, a "why this works" block, an action bar and a
 * progressive "why" reveal. Every one of those slots is filled from data the
 * backend actually computed, or is omitted:
 *
 *   * the chart plots either the facilities/lanes the finding was computed over
 *     (`record.entities`, ranked, sent by `/api/insights`), or — for a
 *     utilisation or capacity finding on a network whose upload carried a
 *     capacity history — the client's OWN recorded utilisation per period
 *     (`OBSERVED_UTILISATION`), which is a measurement they supplied, not a
 *     solver output, and is labelled as such on the axis title;
 *   * the threshold line is `NETWORK_RECOMMENDATION.thresholds`, imported from
 *     the module that owns `UTILIZATION_THRESHOLDS`, never a literal 90;
 *   * the metric tiles are the finding's own evidence rows, showing the value
 *     the engine computed and naming the engine that computed it;
 *   * the headline banner restates the finding's first evidence figure. It is
 *     NOT an "amount at risk": no engine in this system produces one, so that
 *     banner shows a measured figure or does not appear.
 *
 * There is deliberately no "Actual vs Projected" pair on the trend chart. The
 * observed series is history; projecting it onto future utilisation would need
 * the forecast run through the network model per future period, which nothing
 * does. One real line is drawn instead of two, one of which would be invented.
 */

import {
  HOME_INSIGHTS, NETWORK_INSIGHTS, NETWORK_RECOMMENDATION, OBSERVED_UTILISATION,
  getFacilityById, PLANTS, DCS,
} from './data.js';

/* ─── Icons ──────────────────────────────────────────────────── */
const ICON = {
  arrowLeft: `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 15l-5-5 5-5"/></svg>`,
  arrowRight: `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 10h10M11 6l4 4-4 4"/></svg>`,
  play: `<svg viewBox="0 0 24 24" fill="currentColor"><polygon points="6 3 20 12 6 21 6 3"/></svg>`,
  cube: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.7l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.7l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.3 7 12 12 20.7 7"/><line x1="12" y1="22" x2="12" y2="12"/></svg>`,
  info: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.5"/><line x1="12" y1="11" x2="12" y2="16.5"/><circle cx="12" cy="7.8" r="0.6" fill="currentColor" stroke="none"/></svg>`,
  bulb: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 22h4M12 2a6 6 0 0 0-4 10.5c.6.6 1 1.4 1 2.5h6c0-1.1.4-1.9 1-2.5A6 6 0 0 0 12 2z"/></svg>`,
  trendUp: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>`,
  sparkle: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3z"/></svg>`,
  gauge: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20a8 8 0 1 1 8-8"/><line x1="12" y1="12" x2="16.5" y2="8.5"/></svg>`,
  lock: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="10.5" width="16" height="10" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/></svg>`,
};

/** Brand purple, and the tones the metric tiles use. Matches insight-detail.css. */
const INSD_PURPLE = '#6B2FA0';
const INSD_PURPLE_BRIGHT = '#9218EA';
const INSD_RED = '#ef4444';

function insdEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* ─── Presentation of the engine's own severity ───────────────
   The badge is driven by `InsightSeverity`, which the engine sets when it makes
   the finding. It was previously derived by searching the prose for the strings
   "high impact" / "opportunity" / "positive", so an identical finding phrased
   differently was badged differently. */
const SEVERITY_BADGE = {
  RISK: { label: 'Needs attention', tone: 'red' },
  OPPORTUNITY: { label: 'Opportunity', tone: 'green' },
  INFORMATION: { label: 'Informational', tone: 'gray' },
};

/* ─── Flow state ─────────────────────────────────────────────── */
const insdFlow = {
  record: null,
  /** The facility this finding is about, when it is about one. */
  facilityId: null,
};

/* ═══════════════════════════════════════════════════════════════
   Lookup
   ═══════════════════════════════════════════════════════════════ */

/**
 * Find one insight by id, and the facility it belongs to if any.
 *
 * Searches the network findings first, then each facility's. An id encodes its
 * own scope and entity (`INS_FACILITY_DC_WEST_CAPACITY`), but the record is
 * looked up rather than parsed out of the string: the id's shape is the API's
 * business, not this page's.
 */
function findRecord(id) {
  const network = NETWORK_INSIGHTS.find(i => i.id === id);
  if (network) return { record: network, facilityId: null };
  for (const facilityId of Object.keys(HOME_INSIGHTS)) {
    const found = (HOME_INSIGHTS[facilityId] || []).find(i => i.id === id);
    if (found) return { record: found, facilityId };
  }
  return null;
}

/** True when this facility is in the loaded network — never a fixed demo list. */
function facilityExists(id) {
  return [...PLANTS, ...DCS].some(f => f.id === id);
}

/* ═══════════════════════════════════════════════════════════════
   Chart
   ───────────────────────────────────────────────────────────────
   One chart, chosen by what the finding actually has behind it. There is no
   default case: a finding with nothing plottable gets no canvas, because an
   empty axis reads as "measured zero" rather than "not measured".
   ═══════════════════════════════════════════════════════════════ */

const insdCharts = {};

/** The configured utilisation threshold, from the engine. Null when absent. */
function utilisationThreshold() {
  const t = NETWORK_RECOMMENDATION.thresholds || {};
  const over = Number(t.utilization_over_pct);
  return Number.isFinite(over) ? over : null;
}

function labelForMetric(metric) {
  return String(metric || '')
    .replace(/_/g, ' ')
    .replace(/\bpct\b/, '%')
    .replace(/^./, (c) => c.toUpperCase());
}

/**
 * Decide what this finding can honestly be drawn as.
 *
 * Returns a descriptor, or null when nothing plottable exists. Order matters:
 * the entities the finding was computed OVER are the most direct evidence for
 * it, so they outrank the network's recorded history.
 */
function chartPlanFor(record) {
  const entities = (record.entities || []).filter(
    (e) => typeof e.value === 'number' && Number.isFinite(e.value));

  if (entities.length >= 2) {
    const isPct = Boolean(entities[0].metric && entities[0].metric.endsWith('_pct'));
    return {
      kind: 'entities',
      title: isPct
        ? 'Utilisation by site (%)'
        : labelForMetric(entities[0].metric)
          + (entities[0].kind === 'LANE' ? ' by lane' : ' by site'),
      entities,
      threshold: isPct ? utilisationThreshold() : null,
      unitSuffix: isPct ? '%' : '',
      note: 'Every site this finding was computed over, ranked. Figures are the '
          + 'optimiser output for this solve.',
    };
  }

  // The client's own recorded utilisation. Offered ONLY for findings that are
  // about utilisation or capacity — attaching a utilisation history to, say, a
  // carbon finding would be decoration rather than evidence.
  const points = (OBSERVED_UTILISATION.points || []).filter(
    (pt) => typeof pt.utilisationPct === 'number');
  if (['Capacity', 'Utilisation'].includes(record.theme) && points.length >= 2) {
    return {
      kind: 'observed',
      title: 'Recorded utilisation by period (%)',
      points,
      threshold: utilisationThreshold(),
      unitSuffix: '%',
      note: 'Your own recorded available and used capacity, period by period. '
          + 'This is measurement from your upload, not an output of the solve.',
    };
  }

  const components = (NETWORK_RECOMMENDATION.series || {}).cost_components || [];
  if (['Cost', 'Cost structure'].includes(record.theme) && components.length >= 2) {
    return {
      kind: 'components',
      title: 'Cost by component',
      components,
      note: 'Every cost component the solve priced, largest first.',
    };
  }

  // Last resort: the finding's own cited figures, side by side.
  //
  // Only when at least two of them share a unit — comparing a percentage
  // against a rupee total on one axis would be a chart that means nothing.
  // Nothing is derived: these are the exact values in the evidence table, so
  // the bar and the row cannot disagree.
  const cited = (record.evidence || []).filter(
    (e) => typeof e.value === 'number' && Number.isFinite(e.value));
  if (cited.length >= 2) {
    const byUnit = {};
    cited.forEach((e) => {
      const u = e.unit || '';
      (byUnit[u] = byUnit[u] || []).push(e);
    });
    const group = Object.values(byUnit)
      .sort((a, b) => b.length - a.length)[0];
    if (group && group.length >= 2) {
      const pct = (group[0].unit || '') === 'percent';
      return {
        kind: 'evidence',
        title: 'Figures this finding cites',
        cited: group,
        threshold: pct ? utilisationThreshold() : null,
        unitSuffix: pct ? '%' : '',
        note: 'The values in the evidence table below, drawn to scale. '
            + 'Nothing here is derived from them.',
      };
    }
  }
  return null;
}

/** Draw the planned chart. A no-op when Chart.js is absent. */
function renderInsightChart(canvasId, plan) {
  if (insdCharts[canvasId]) {
    insdCharts[canvasId].destroy();
    delete insdCharts[canvasId];
  }
  const canvas = document.getElementById(canvasId);
  if (!canvas || !plan || typeof Chart === 'undefined') return;

  const grid = '#f3f4f6';
  let config = null;

  if (plan.kind === 'evidence') {
    const labels = plan.cited.map((e) => e.label);
    const datasets = [{
      label: 'Value',
      data: plan.cited.map((e) => e.value),
      backgroundColor: plan.cited.map((_, i) => (i === 0 ? INSD_PURPLE : '#b893d6')),
      borderRadius: 4,
      maxBarThickness: 46,
    }];
    if (plan.threshold != null) {
      datasets.push({
        label: 'Threshold ' + plan.threshold + '%',
        data: labels.map(() => plan.threshold),
        type: 'line',
        borderColor: INSD_RED,
        borderDash: [4, 4],
        borderWidth: 1.5,
        pointRadius: 0,
      });
    }
    config = {
      type: 'bar',
      data: { labels, datasets },
      options: {
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              // The engine's own formatting, so the tooltip and the evidence
              // table read identically.
              label: (c) => plan.cited[c.dataIndex]
                ? plan.cited[c.dataIndex].display_value
                : String(c.parsed.x),
            },
          },
        },
        scales: {
          x: { beginAtZero: true,
               ticks: { callback: (v) => v + plan.unitSuffix },
               grid: { color: grid } },
          y: { grid: { display: false } },
        },
      },
    };
  } else if (plan.kind === 'entities') {
    const labels = plan.entities.map((e) => e.label || e.entity_id);
    const datasets = [{
      label: labelForMetric(plan.entities[0].metric),
      data: plan.entities.map((e) => e.value),
      // A closed site is drawn in grey: the same bar height means a different
      // thing for a site the plan does not use, and colouring both alike
      // invites a comparison that is not meaningful.
      backgroundColor: plan.entities.map((e) => (
        e.is_open === false
          ? '#cbd5e1'
          : (plan.threshold != null && e.value > plan.threshold)
            ? INSD_RED
            : INSD_PURPLE)),
      borderRadius: 4,
      maxBarThickness: 26,
    }];
    if (plan.threshold != null) {
      datasets.push({
        label: 'Threshold ' + plan.threshold + '%',
        data: labels.map(() => plan.threshold),
        type: 'line',
        borderColor: INSD_RED,
        borderDash: [4, 4],
        borderWidth: 1.5,
        pointRadius: 0,
      });
    }
    config = {
      type: 'bar',
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (c) => c.dataset.label + ': ' + c.parsed.y + plan.unitSuffix,
            },
          },
        },
        scales: {
          y: {
            beginAtZero: true,
            ticks: { callback: (v) => v + plan.unitSuffix },
            grid: { color: grid },
          },
          x: { grid: { display: false }, ticks: { autoSkip: false, maxRotation: 42 } },
        },
      },
    };
  } else if (plan.kind === 'observed') {
    const labels = plan.points.map((pt) => pt.period);
    const datasets = [{
      label: 'Recorded utilisation',
      data: plan.points.map((pt) => pt.utilisationPct),
      borderColor: INSD_PURPLE,
      backgroundColor: 'rgba(107,47,160,.06)',
      borderWidth: 2.5,
      pointRadius: plan.points.length > 18 ? 0 : 3,
      pointBackgroundColor: INSD_PURPLE,
      tension: 0.25,
      fill: true,
      // A period whose figures cannot form a ratio breaks the line rather than
      // being joined through as though it had been measured.
      spanGaps: false,
    }];
    if (plan.threshold != null) {
      datasets.push({
        label: 'Threshold ' + plan.threshold + '%',
        data: labels.map(() => plan.threshold),
        borderColor: INSD_RED,
        borderDash: [4, 4],
        borderWidth: 1.5,
        pointRadius: 0,
      });
    }
    config = {
      type: 'line',
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { mode: 'index', intersect: false },
        },
        scales: {
          y: { min: 0, max: 100, ticks: { callback: (v) => v + '%' },
               grid: { color: grid } },
          x: { grid: { display: false },
               ticks: { autoSkip: true, maxTicksLimit: 12, maxRotation: 42 } },
        },
      },
    };
  } else if (plan.kind === 'components') {
    const palette = ['#6B2FA0', '#9218EA', '#b893d6', '#d4bfe8', '#7c3aad',
                     '#4a206e', '#a63bf2', '#e8ddf2'];
    config = {
      type: 'doughnut',
      data: {
        labels: plan.components.map((c) => c.label),
        datasets: [{
          data: plan.components.map((c) => c.value),
          backgroundColor: plan.components.map((_, i) => palette[i % palette.length]),
          borderWidth: 0,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '58%',
        plugins: {
          legend: { position: 'right', labels: { boxWidth: 12, font: { size: 11 } } },
        },
      },
    };
  }

  if (config) insdCharts[canvasId] = new Chart(canvas, config);
}

/* ═══════════════════════════════════════════════════════════════
   Sections
   ═══════════════════════════════════════════════════════════════ */

/**
 * The finding's own headline figure, in the banner slot.
 *
 * The prototype put an "amount at risk" here — a rupee figure produced by
 * `8 + hash(id) % 24`. No engine in this system computes an amount at risk, so
 * this shows the first figure the finding actually cites, or renders nothing.
 * A banner is a strong visual claim and it must carry a measured number.
 */
function headlineBannerHtml(record) {
  const first = (record.evidence || [])[0];
  if (!first || !first.display_value) return '';
  const tone = record.severity === 'RISK' ? '' : ' tone-taken';
  return `
    <div class="insd-risk-banner${tone}">
      <span class="insd-risk-icon">${ICON.gauge}</span>
      <div class="insd-risk-body">
        <div class="insd-risk-amount">${insdEsc(first.display_value)}</div>
        <div class="insd-risk-note">${insdEsc(first.label)} — computed by
          ${insdEsc(first.source)}</div>
      </div>
    </div>`;
}

/**
 * The left column: the chart where one can be drawn, the narrative always.
 *
 * When `chartPlanFor` returns nothing the card keeps its title and prose and
 * simply has no canvas. The prototype's equivalent branch drew a synthesised
 * seven-month trend for every finding regardless of whether one existed.
 */
function findingCardHtml(record, plan) {
  const chart = plan
    ? `
      <div class="insd-chart-head">
        <div class="insd-chart-title">${insdEsc(plan.title)}</div>
        <div class="insd-chart-legend">
          <span class="insd-legend-item"><span class="insd-legend-swatch"></span>${
            plan.kind === 'observed' ? 'Recorded'
              : plan.kind === 'evidence' ? 'Cited figure' : 'Solved plan'}</span>
          ${plan.threshold != null
            ? `<span class="insd-legend-item"><span class="insd-legend-swatch dashed"></span>Threshold ${plan.threshold}%</span>`
            : ''}
        </div>
      </div>
      <div class="insd-chart-canvas-wrap"><canvas id="insd-trend-chart"></canvas></div>`
    : `<div class="insd-chart-head"><div class="insd-chart-title">What I found</div></div>`;

  const provenance = plan
    ? `<div class="insd-chart-note">${ICON.info}<span>${insdEsc(plan.note)}</span></div>`
    : '';

  return `
    <div class="insd-card">
      ${chart}
      <div class="insd-chart-note">${ICON.trendUp}<span>${
        insdEsc(record.narrative || record.subtitle)}</span></div>
      ${provenance}
    </div>`;
}

/**
 * The metrics the finding cites, with their authoritative values.
 *
 * This is what a deep dive is for. Each row names the metric, the value the
 * deterministic layer computed, which engine computed it, and — new this phase
 * — the role it played: the measurement, what it was compared against, or the
 * driver behind it.
 *
 * Until this phase the table had never rendered a row in production. The API
 * has always resolved evidence; `toInsightRecord` dropped it, so every finding
 * fell through to the "cites no single figure" copy below — which is a false
 * statement about thirteen of the fourteen insight themes.
 */
function evidenceCardHtml(record) {
  const ROLE = { metric: 'Measured', comparison: 'Compared against', driver: 'Driver' };
  const rows = (record.evidence || []).map(e => `
    <tr>
      <td class="insd-detail-label">${insdEsc(e.label)}</td>
      <td class="insd-detail-val">${insdEsc(e.display_value)}</td>
      <td class="insd-detail-label">${insdEsc(ROLE[e.role] || 'Measured')}</td>
      <td class="insd-detail-label">${insdEsc(e.source)}</td>
    </tr>`).join('');

  const body = rows
    ? `<table class="insd-table">
         <thead><tr><th>Metric</th><th>Value</th><th>Role</th><th>Computed by</th></tr></thead>
         <tbody>${rows}</tbody>
       </table>`
    : `<p class="insd-why-text">This finding is a statement about the plan's
         structure rather than about one metric, so it cites no single figure.
         The network KPIs it follows from are on the Home dashboard.</p>`;

  return `
    <div class="insd-card">
      <div class="insd-why-title">${ICON.bulb}<span>Evidence</span></div>
      ${body}
    </div>`;
}

/** One metric tile. Mirrors the prototype's `.insd-metric`. */
function metricTileHtml(icon, tone, label, value, sub) {
  return `
    <div class="insd-metric">
      <div class="insd-metric-head"><span class="insd-metric-icon tone-${tone}">${icon}</span>${insdEsc(label)}</div>
      <div class="insd-metric-val tone-${tone}">${insdEsc(value)}</div>
      <div class="insd-metric-sub">${insdEsc(sub)}</div>
    </div>`;
}

/**
 * The right column: the engine's recommendation, and the figures behind it.
 *
 * The metric row is the prototype's three-tile strip, but each tile carries an
 * evidence row this finding actually cites rather than a hashed cost delta, a
 * hashed service gain and a risk band derived from them. Where the finding
 * cites fewer than three figures, fewer than three tiles are drawn — the row
 * is not padded to fill the grid.
 */
function recommendationCardHtml(record) {
  const rec = NETWORK_RECOMMENDATION;

  const tones = ['purple', 'green', 'amber'];
  const tiles = (record.evidence || []).slice(0, 3).map((e, i) =>
    metricTileHtml(ICON.gauge, tones[i] || 'purple', e.label, e.display_value,
                   `via ${e.source}`)).join('');
  const metricsRow = tiles ? `<div class="insd-metrics-row">${tiles}</div>` : '';

  if (!rec.text) {
    return `
      <div class="insd-card">
        <div class="insd-rec-head">${ICON.sparkle}Recommended action</div>
        <p class="insd-why-text">No recommendation has been generated for this
          network yet.</p>
        ${metricsRow}
      </div>`;
  }

  const drivers = (rec.keyDrivers || []).length
    ? `<div class="insd-why-stat" style="display:block">
         <span class="insd-why-stat-label">Key drivers</span>
         <ul class="insd-details-body" style="margin-top:6px">${rec.keyDrivers
           .map(d => `<li>${insdEsc(d)}</li>`).join('')}</ul>
       </div>`
    : '';
  const limitation = rec.limitation
    ? `<p class="insd-chart-note">${ICON.info}<span>Limitation: ${insdEsc(rec.limitation)}</span></p>`
    : '';

  return `
    <div class="insd-card">
      <div class="insd-rec-head">${ICON.sparkle}Recommended action</div>
      <p class="insd-rec-sentence">${insdEsc(rec.text)}</p>
      ${metricsRow}
      <div class="insd-why-row">
        <span class="insd-why-icon">${ICON.bulb}</span>
        <div style="flex:1;min-width:0">
          <div class="insd-why-title">Why this works</div>
          <div class="insd-why-text">This is a network-level recommendation drawn
            from every finding, not from this one alone.</div>
          ${drivers}
        </div>
      </div>
      ${limitation}
    </div>`;
}

/**
 * The two things a reader can actually do.
 *
 * Both navigate. Neither claims to have changed anything: the previous version
 * offered Approve / Reject buttons that only changed their own label, and an
 * "action taken" state that asserted an action had been taken when none had.
 */
function actionBarHtml(facilityId, whyText) {
  const canOpenTwin = facilityId && facilityExists(facilityId);
  const twinButton = canOpenTwin
    ? `<button type="button" class="insd-btn-outline-purple" id="insd-open-twin">
         ${ICON.cube}<span>Open in Digital Twin</span></button>`
    : '';
  return `
    <div class="insd-action-bar">
      <button type="button" class="insd-btn-primary" id="insd-run-scenario">
        ${ICON.play}<span>Test a change as a scenario</span></button>
      ${twinButton}
      <button type="button" class="insd-action-link" id="insd-why-btn">
        ${ICON.info}<span>Why this finding?</span></button>
      <div class="insd-why-reveal" id="insd-why-reveal">${insdEsc(whyText)}</div>
    </div>`;
}

/**
 * Where the figures came from, and whether they were checked.
 *
 * The grounding status is shown whenever it is not clean, because a reader
 * acting on prose is entitled to know its numbers were not verified against
 * the deterministic results.
 */
function footerNoteHtml(record) {
  const rec = NETWORK_RECOMMENDATION;
  const parts = [
    `Source: NetGravity reasoning over the solved network state`
    + (rec.stateId ? ` (${insdEsc(rec.stateId)})` : '') + '.',
  ];
  if (rec.groundingStatus && rec.groundingStatus !== 'GROUNDED'
      && rec.groundingStatus !== 'NO_CLAIMS') {
    parts.push(`Numeric grounding: ${insdEsc(rec.groundingStatus)} — the figures
      in this text were not all verified against the deterministic results.`);
  }
  if (rec.evidenceCompleteness && rec.evidenceCompleteness !== 'COMPLETE') {
    parts.push(`Evidence is ${insdEsc(rec.evidenceCompleteness)}: some analyses
      did not run, so their values are unknown rather than zero.`);
  }
  return `<p class="insd-footer-note">${parts.join(' ')}</p>`;
}

/* ═══════════════════════════════════════════════════════════════
   Render
   ═══════════════════════════════════════════════════════════════ */

function renderDeepDive() {
  const page = document.getElementById('tab-insight-detail');
  const record = insdFlow.record;
  if (!page || !record) return;

  const badge = SEVERITY_BADGE[record.severity] || SEVERITY_BADGE.INFORMATION;
  const plan = chartPlanFor(record);
  const facility = insdFlow.facilityId
    ? getFacilityById(insdFlow.facilityId)
    : null;
  const scopeLine = facility
    ? `${insdEsc(record.theme)} · ${insdEsc(facility.name || facility.id)}`
    : `${insdEsc(record.theme)} · whole network`;

  page.innerHTML = `
    <div class="insd-page">
      <button type="button" class="insd-back-link" id="insd-back-btn">${ICON.arrowLeft}<span>Back to Home</span></button>

      <div class="insd-header-row">
        <h1 class="insd-title">${insdEsc(record.title)}</h1>
        <span class="insd-badge tone-${badge.tone}">${badge.label}</span>
      </div>
      <p class="insd-subtitle">${scopeLine}</p>

      ${headlineBannerHtml(record)}

      <div class="insd-main-split">
        <div>
          ${findingCardHtml(record, plan)}
          ${evidenceCardHtml(record)}
        </div>
        ${recommendationCardHtml(record)}
      </div>

      ${actionBarHtml(insdFlow.facilityId, record.narrative || record.subtitle)}
      ${footerNoteHtml(record)}
    </div>`;

  // After innerHTML, so the canvas exists. A frame of delay lets the layout
  // settle first: Chart.js sizes to the wrapper, and measuring it mid-paint
  // produced a chart one frame wide on a cold render.
  if (plan) requestAnimationFrame(() => renderInsightChart('insd-trend-chart', plan));
  bindDeepDive();
}

function bindDeepDive() {
  document.getElementById('insd-back-btn')?.addEventListener('click', backToHome);

  document.getElementById('insd-why-btn')?.addEventListener('click', () => {
    document.getElementById('insd-why-reveal')?.classList.toggle('open');
  });

  document.getElementById('insd-run-scenario')?.addEventListener('click', () => {
    if (typeof window.navigateToTab === 'function') window.navigateToTab('scenarios');
  });

  document.getElementById('insd-open-twin')?.addEventListener('click', () => {
    const facilityId = insdFlow.facilityId;
    if (typeof window.exploreInTwin === 'function' && facilityId) {
      window.exploreInTwin(facilityId);
    } else if (typeof window.navigateToTab === 'function') {
      window.navigateToTab('twin');
    }
  });
}

/* ═══════════════════════════════════════════════════════════════
   Entry points / navigation
   ═══════════════════════════════════════════════════════════════ */

/**
 * Open the deep dive for one insight.
 *
 * `kind` is accepted and ignored for the moment: the attention feed passes
 * 'insight' or 'action', and `HOME_ACTION_ITEMS` is currently empty because no
 * engine produces a discrete action record. When one does, this is where that
 * branch belongs; until then an unfound id opens nothing rather than opening a
 * page about the wrong thing.
 */
export function showInsightDetail(kind, id) {
  const hit = findRecord(id);
  if (!hit) return;

  insdFlow.record = hit.record;
  insdFlow.facilityId = hit.facilityId;

  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  const page = document.getElementById('tab-insight-detail');
  if (page) page.classList.add('active');

  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('nav-item-home')?.classList.add('active');

  const subTopbar = document.getElementById('app-sub-topbar');
  if (subTopbar) subTopbar.style.display = 'none';
  const btnUpload = document.getElementById('btn-topbar-upload');
  if (btnUpload) btnUpload.style.display = 'none';

  renderDeepDive();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function backToHome() {
  // Chart.js keeps a live instance bound to a canvas this page is about to
  // discard. Destroying it here keeps one instance per canvas at most.
  Object.keys(insdCharts).forEach((id) => {
    insdCharts[id].destroy();
    delete insdCharts[id];
  });
  if (typeof window.navigateToTab === 'function') window.navigateToTab('home');
}

export function initInsightDetail() {
  if (typeof window !== 'undefined') {
    window.showInsightDetail = showInsightDetail;
  }
}
