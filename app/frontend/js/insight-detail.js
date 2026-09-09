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
 * What the page reads as now, top to bottom
 * -----------------------------------------
 *   the headline          the conclusion, as the engine stated it
 *   the lede              its own prose explaining that conclusion
 *   the banner            the one figure the finding leads on
 *   the visual            a chart, or a scale against the threshold
 *   how this was reached  the figures in the order they were used
 *   recommended action    the step, and the button that goes there
 *
 * The order is the argument. Before this, the prose sat under the chart in a
 * footnote slot, the only thing between the figures and the recommendation
 * was a table, and the one control that promised to explain the finding
 * ("Why this finding?") was a disclosure toggle at the foot revealing the
 * narrative already printed at the top.
 *
 * Where each figure comes from
 * ----------------------------
 * Every slot is filled from data the backend actually computed, or omitted:
 *
 *   * the chart plots either the facilities/lanes the finding was computed over
 *     (`record.entities`, ranked, sent by `/api/insights`), or — for a
 *     utilisation or capacity finding on a network whose upload carried a
 *     capacity history — the client's OWN recorded utilisation per period
 *     (`OBSERVED_UTILISATION`), which is a measurement they supplied, not a
 *     solver output, and is labelled as such on the axis title;
 *   * the threshold line is `NETWORK_RECOMMENDATION.thresholds`, imported from
 *     the module that owns `UTILIZATION_THRESHOLDS`, never a literal 90;
 *   * the reasoning chain is the finding's own evidence rows, each with the
 *     value the engine computed, the role it played in reaching the finding,
 *     and the engine that computed it;
 *   * the headline banner restates the finding's first evidence figure. It is
 *     NOT an "amount at risk": no engine in this system produces one, so that
 *     banner shows a measured figure or does not appear;
 *   * the threshold scale draws a percentage against the configured
 *     utilisation threshold, and only for the themes that threshold judges —
 *     and only where no chart already carries the same comparison;
 *   * the recommended action is the finding's own (`recommendedAction`), and
 *     the button beside it resolves through `insightCta` — the same function
 *     the Overview tile uses, so the tile and the page it opens cannot
 *     disagree about what to do next.
 *
 * There is deliberately no "Actual vs Projected" pair on the trend chart. The
 * observed series is history; projecting it onto future utilisation would need
 * the forecast run through the network model per future period, which nothing
 * does. One real line is drawn instead of two, one of which would be invented.
 */

import {
  HOME_INSIGHTS, NETWORK_INSIGHTS, NETWORK_RECOMMENDATION, OBSERVED_UTILISATION,
  getFacilityById, PLANTS, DCS,
  HOME_ACTION_ITEMS, NOTIFICATION_RECIPIENTS, EMAIL_DELIVERY,
} from './data.js';
// The same two decisions the Overview's tile made about this finding: what
// its description says once the headline is taken out, and which screen its
// recommended action is asking the reader to open. Shared, because the tile
// and this page disagreeing about either is the defect.
import { insightCta, insightDescription } from './insight-presentation.js';
// The node identities the Digital Twin's legend and both of its maps use.
import { NODE_STYLE } from './twin-legend.js';

/* ─── Icons ──────────────────────────────────────────────────── */
const ICON = {
  mail: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="4.5" width="15" height="11" rx="1.6"/><path d="M2.9 5.4 10 10.6l7.1-5.2"/></svg>',
  upload: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13V3.4M6.4 6.8 10 3.2l3.6 3.6"/><path d="M3.4 12.6v2.8a1.4 1.4 0 0 0 1.4 1.4h10.4a1.4 1.4 0 0 0 1.4-1.4v-2.8"/></svg>',
  arrowLeft: `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 15l-5-5 5-5"/></svg>`,
  arrowRight: `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 10h10M11 6l4 4-4 4"/></svg>`,
  play: `<svg viewBox="0 0 24 24" fill="currentColor"><polygon points="6 3 20 12 6 21 6 3"/></svg>`,
  cube: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.7l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.7l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.3 7 12 12 20.7 7"/><line x1="12" y1="22" x2="12" y2="12"/></svg>`,
  info: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.5"/><line x1="12" y1="11" x2="12" y2="16.5"/><circle cx="12" cy="7.8" r="0.6" fill="currentColor" stroke="none"/></svg>`,
  bulb: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 22h4M12 2a6 6 0 0 0-4 10.5c.6.6 1 1.4 1 2.5h6c0-1.1.4-1.9 1-2.5A6 6 0 0 0 12 2z"/></svg>`,
  trendUp: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>`,
  sparkle: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3z"/></svg>`,
  gauge: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20a8 8 0 1 1 8-8"/><line x1="12" y1="12" x2="16.5" y2="8.5"/></svg>`,
  download: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M7.5 10.5 12 15l4.5-4.5"/><path d="M4 17v2.2A1.8 1.8 0 0 0 5.8 21h12.4a1.8 1.8 0 0 0 1.8-1.8V17"/></svg>`,
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

/** The configured under-utilisation threshold, from the engine. Null when absent. */
function underUtilisationThreshold() {
  const t = NETWORK_RECOMMENDATION.thresholds || {};
  const under = Number(t.utilization_under_pct);
  return Number.isFinite(under) ? under : null;
}

/**
 * What colour each bar is, and what that colour means.
 *
 * EVERY BAR WAS THE SAME PURPLE. The rule was: grey if the plan does not use
 * the site, red if it is over the threshold, purple otherwise — so on a
 * healthy network, which is most of them, all seven bars were identical and
 * the chart carried no more information than a list of numbers would. There
 * was no legend either, so the two colours that did exist were unexplained.
 *
 * The bands are the ENGINE'S OWN policy thresholds, read from
 * `NETWORK_RECOMMENDATION.thresholds` — the same two numbers the reasoning
 * agent judges utilisation by, and never a 90 or a 30 written into this file.
 * A chart that invented its own bands would be drawing a judgement nobody
 * made.
 *
 * Where the metric is not a percentage there is no band to apply, so the bars
 * are coloured by what the node IS — plant or distribution centre, in the
 * same two colours the Digital Twin's legend and maps use.
 *
 * Returns the per-bar colours and the classes actually present, so the legend
 * below the chart lists only what the reader can see. A key with four entries
 * for a chart showing two is a key that has to be read twice.
 */
function entityBarStyle(plan) {
  const CLOSED = { key: 'closed', color: '#cbd5e1',
                   label: 'Not used by this plan' };
  const over = plan.threshold;
  const under = underUtilisationThreshold();
  const isPct = plan.unitSuffix === '%';

  const classify = (e) => {
    if (e.is_open === false) return CLOSED;
    if (isPct && typeof e.value === 'number') {
      if (over != null && e.value >= over) {
        return { key: 'over', color: INSD_RED,
                 label: `At or above the ${over}% threshold` };
      }
      if (under != null && e.value <= under) {
        return { key: 'under', color: '#b45309',
                 label: `At or below ${under}% \u2014 under-used` };
      }
      return { key: 'mid', color: INSD_PURPLE,
               label: over != null && under != null
                 ? `Between ${under}% and ${over}%` : 'Within policy' };
    }
    const role = String(e.role || '').toUpperCase();
    if (role === 'PLANT' || role === 'SUPPLIER') {
      return { key: 'plant', color: NODE_STYLE.plant.color, label: 'Plant' };
    }
    if (e.kind === 'LANE') {
      return { key: 'lane', color: INSD_PURPLE, label: 'Lane' };
    }
    return { key: 'dc', color: NODE_STYLE.dc.color,
             label: NODE_STYLE.dc.label };
  };

  const classes = new Map();
  const colors = plan.entities.map((e) => {
    const cls = classify(e);
    const seen = classes.get(cls.key);
    if (seen) seen.count += 1;
    else classes.set(cls.key, { ...cls, count: 1 });
    return cls.color;
  });

  // In the order they read on the chart: worst first, then the rest, then the
  // sites the plan does not use.
  const ORDER = ['over', 'under', 'mid', 'plant', 'dc', 'lane', 'closed'];
  const legend = [...classes.values()]
    .sort((a, b) => ORDER.indexOf(a.key) - ORDER.indexOf(b.key));
  return { colors, legend };
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
      // Banded by the engine's own policy thresholds, or by what the node is
      // where the metric carries no band. See `entityBarStyle`.
      backgroundColor: entityBarStyle(plan).colors,
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
 * What the finding says, in the engine's own prose.
 *
 * THE LEDE, and it used to be a caption. The narrative was printed inside an
 * `.insd-chart-note` under the canvas, beside a small trend icon, in the slot
 * this file uses for provenance footnotes like "figures are the optimiser
 * output for this solve". So the one paragraph explaining the finding was
 * styled as a disclaimer about the picture above it, below the fold on a
 * short window, and a reader who came here from a tile to find out what the
 * headline MEANT had to read past a chart to reach it.
 *
 * `insightDescription` takes out whatever the headline already said, so the
 * page does not open by repeating its own title — the same rule the Overview
 * tile follows, from the same module.
 */
function descriptionHtml(record) {
  const text = insightDescription(record, record.title);
  if (!text) return '';
  return `<p class="insd-lede">${insdEsc(text)}</p>`;
}

/**
 * Where a percentage sits against the threshold it was judged by.
 *
 * A finding that says "97.2% against a 90% threshold" is two numbers a reader
 * has to hold in their head and subtract. Drawn to scale it is one glance,
 * and the thing being judged is visible as a distance rather than a
 * difference.
 *
 * NOTHING IS COMPUTED. §9 — the frontend never calculates an authoritative
 * figure. Both numbers are printed exactly as the engine formatted them
 * (`display_value`, and the threshold from `NETWORK_RECOMMENDATION.thresholds`
 * — never a 90 written into this file); the only arithmetic is turning a
 * percentage into a bar width, which is a drawing instruction, not a reading.
 * No derived number appears anywhere in the output.
 *
 * TWO GUARDS, and both are about not drawing a comparison the engine did not
 * make:
 *
 *   * the only threshold this page has is the UTILISATION one, so the scale
 *     is offered only to the themes it judges. A cost finding that happens to
 *     cite a percentage would otherwise be drawn against a 90% utilisation
 *     line, which is a comparison nobody made and every reader would read as
 *     one that had been;
 *   * a chart that already draws its own threshold line has made the
 *     comparison. Drawing it twice, at two granularities, gives a reader two
 *     places to look for one answer — so the scale fills the gap where there
 *     is no chart, or where the chart is not about a threshold, and stays out
 *     of the way otherwise.
 */
function thresholdScaleHtml(record, plan) {
  if (!['Capacity', 'Utilisation'].includes(record.theme)) return '';
  if (plan && plan.threshold != null) return '';

  const threshold = utilisationThreshold();
  if (threshold == null) return '';
  const row = (record.evidence || []).find(
    (e) => e && e.unit === 'percent' && typeof e.value === 'number'
           && Number.isFinite(e.value));
  if (!row) return '';

  const clamp = (n) => Math.max(0, Math.min(100, n));
  const over = row.value > threshold;
  return `
    <div class="insd-scale${threshold > 78 ? ' caption-left' : ''}">
      <div class="insd-scale-head">
        <span class="insd-scale-label">${insdEsc(row.label)}</span>
        <span class="insd-scale-value${over ? ' is-over' : ''}">${insdEsc(row.display_value)}</span>
      </div>
      <div class="insd-scale-track">
        <div class="insd-scale-fill${over ? ' is-over' : ''}"
             style="width:${clamp(row.value)}%"></div>
        <div class="insd-scale-mark" style="left:${clamp(threshold)}%"></div>
        <!-- Inside the track, on the same offset as the mark, so the caption
             stands under the position it names. -->
        <div class="insd-scale-caption" style="left:${clamp(threshold)}%">
          Threshold ${threshold}%</div>
      </div>
      <div class="insd-scale-foot">
        <span>0%</span>
        <span>100%</span>
      </div>
    </div>`;
}

/**
 * The left column's visual: the chart where one can be drawn, and the scale.
 *
 * When `chartPlanFor` returns nothing the card keeps its title and simply has
 * no canvas. The prototype's equivalent branch drew a synthesised seven-month
 * trend for every finding regardless of whether one existed. When there is
 * neither a chart nor a scale the card is omitted altogether, rather than
 * standing empty over the word "chart".
 */
function findingCardHtml(record, plan) {
  const scale = thresholdScaleHtml(record, plan);
  if (!plan && !scale) return '';

  // THE KEY LISTS WHAT IS ON THE CHART, with how many bars carry it.
  //
  // It used to say "Solved plan" beside one purple swatch, whatever the
  // chart was drawn in — so a chart with grey bars for the sites the plan
  // does not use, and red for the sites over the threshold, had a key naming
  // neither. `entityBarStyle` returns only the classes actually present, so
  // the key never lists a colour the reader cannot find.
  const bands = (plan && plan.kind === 'entities') ? entityBarStyle(plan).legend : [];
  const bandKeys = bands.length
    ? bands.map((b) => `
          <span class="insd-legend-item">
            <span class="insd-legend-swatch" style="background:${b.color}"></span>
            ${insdEsc(b.label)}
            <span class="insd-legend-count">${b.count}</span>
          </span>`).join('')
    : `<span class="insd-legend-item"><span class="insd-legend-swatch"></span>${
        plan && plan.kind === 'observed' ? 'Recorded'
          : plan && plan.kind === 'evidence' ? 'Cited figure' : 'Solved plan'}</span>`;

  const chart = plan
    ? `
      <div class="insd-chart-head">
        <div class="insd-chart-title">${insdEsc(plan.title)}</div>
      </div>
      <div class="insd-chart-canvas-wrap"><canvas id="insd-trend-chart"></canvas></div>
      <div class="insd-chart-legend">
        ${bandKeys}
        ${plan.threshold != null
          ? `<span class="insd-legend-item"><span class="insd-legend-swatch dashed"></span>Threshold ${plan.threshold}%</span>`
          : ''}
      </div>`
    : `<div class="insd-chart-head"><div class="insd-chart-title">Where this sits</div></div>`;

  const provenance = plan
    ? `<div class="insd-chart-note">${ICON.info}<span>${insdEsc(plan.note)}</span></div>`
    : '';

  return `
    <div class="insd-card">
      ${chart}
      ${scale}
      ${provenance}
    </div>`;
}

/**
 * How the engine reached this, in the order it reached it.
 *
 * THIS SECTION IS NEW, and it is the one a deep dive exists for. The page had
 * the finding, a chart and a table of figures, and nothing that said how the
 * second led to the first — a reader could see 97.2% and see the conclusion,
 * and had to take the step between them on trust. "Why this finding?" was a
 * disclosure toggle at the bottom that revealed the narrative they had
 * already read at the top.
 *
 * Nothing here is written by this file. The chain is the engine's own
 * evidence, which already carries the distinction that makes a chain: each
 * figure is tagged with the ROLE it played — the measurement, the thing it
 * was compared against, or the driver behind it. Those three lists were
 * concatenated into one flat array for the table and the ordering thrown
 * away. This reads them back out.
 *
 * A step with no figures is omitted rather than shown empty: a finding that
 * cites no comparison did not make one, and printing the heading over a blank
 * would suggest the engine had weighed something it did not.
 */
function reasoningCardHtml(record) {
  const ROLE_STEPS = [
    { role: 'metric', title: 'What was measured',
      blurb: 'The figures this finding reads, as the solve computed them.' },
    { role: 'comparison', title: 'What it was compared against',
      // Deliberately not "the threshold or baseline": `comparison_refs` also
      // carries a plain counterpart figure — 5 sites open against 2 left
      // closed — and calling that a threshold would describe the engine's
      // reasoning as something stricter than it was.
      blurb: 'What those figures were read against.' },
    { role: 'driver', title: 'What is behind it',
      blurb: 'The quantities moving the measurement above.' },
  ];

  // THIS IS ALSO THE EVIDENCE TABLE. There used to be one below it listing
  // the same rows flat, with a Role column naming what the step headings
  // here name and a "Computed by" column naming what `via …` names — and a
  // third copy in tiles beside the recommendation. On a finding citing one
  // figure, that figure appeared four times on the page counting the
  // banner. The role and the engine travel with each figure here instead.

  const evidence = (record.evidence || []).filter(
    (e) => e && e.display_value && e.display_value !== 'Not available');

  const steps = ROLE_STEPS
    .map((step) => ({ ...step, rows: evidence.filter((e) => (e.role || 'metric') === step.role) }))
    .filter((step) => step.rows.length);

  // The magnitude behind each figure, drawn only where the figures are
  // COMPARABLE — two or more sharing a unit — because a currency total and a
  // percentage on one scale is a picture of nothing.
  //
  // Deliberately measured across the WHOLE finding rather than within a
  // step: the comparison worth seeing is exactly the one that crosses them,
  // 56.23% average against the 77.14% busiest site, 5 sites open against 2
  // left closed. Bars drawn per step would put each of those alone on its
  // own scale, at full width, saying nothing.
  //
  // §9 — the width is a drawing instruction, not a reading. No number
  // derived from it is printed anywhere; `display_value` stays the only
  // thing on this page a reader reads.
  const peak = {};
  evidence.forEach((e) => {
    if (typeof e.value !== 'number' || !Number.isFinite(e.value)) return;
    const u = e.unit || '';
    peak[u] = peak[u] === undefined
      ? { max: Math.abs(e.value), n: 1 }
      : { max: Math.max(peak[u].max, Math.abs(e.value)), n: peak[u].n + 1 };
  });
  const barFor = (e) => {
    const group = peak[e.unit || ''];
    if (!group || group.n < 2 || !(group.max > 0)) return '';
    if (typeof e.value !== 'number' || !Number.isFinite(e.value)) return '';
    return `<span class="insd-bar" aria-hidden="true"><span class="insd-bar-fill"
      style="width:${(Math.abs(e.value) / group.max) * 100}%"></span></span>`;
  };

  // A finding with no figures at all. It is rare and it is real — some
  // findings are statements about the plan's STRUCTURE rather than about a
  // metric — and it is stated rather than left as a heading over nothing.
  if (!steps.length) {
    return `
      <div class="insd-card">
        <div class="insd-why-title">${ICON.gauge}<span>How this was reached</span></div>
        <p class="insd-why-text">This finding is a statement about the plan's
          structure rather than about one metric, so it cites no single figure.
          The network KPIs it follows from are on the Home dashboard.</p>
      </div>`;
  }

  const stepsHtml = steps.map((step, i) => `
    <li class="insd-reason-step">
      <span class="insd-reason-index" aria-hidden="true">${i + 1}</span>
      <div class="insd-reason-body">
        <div class="insd-reason-title">${insdEsc(step.title)}</div>
        <p class="insd-reason-blurb">${insdEsc(step.blurb)}</p>
        <dl class="insd-reason-figures">
          ${step.rows.map((e) => `
            <div class="insd-reason-figure">
              <dt>${insdEsc(e.label)}${barFor(e)}</dt>
              <dd>${insdEsc(e.display_value)}
                <span class="insd-reason-source">via ${insdEsc(e.source)}</span></dd>
            </div>`).join('')}
        </dl>
      </div>
    </li>`).join('');

  // The conclusion those steps arrive at, as the last link. This is the
  // headline verbatim — not a paraphrase, which would be this file writing a
  // finding of its own.
  const conclusion = `
    <li class="insd-reason-step is-conclusion">
      <span class="insd-reason-index" aria-hidden="true">${ICON.bulb}</span>
      <div class="insd-reason-body">
        <div class="insd-reason-title">And therefore</div>
        <p class="insd-reason-conclusion">${insdEsc(record.title)}</p>
      </div>
    </li>`;

  // What the ANALYSIS could not establish. Labelled as the analysis's, not
  // this finding's: `limitation` is written across the whole briefing, and
  // attaching it to one finding would claim the engine said something about
  // this finding that it did not.
  const rec = NETWORK_RECOMMENDATION;
  const limitation = rec.limitation ? `
    <p class="insd-chart-note">${ICON.info}<span>What this analysis does not
      establish: ${insdEsc(rec.limitation)}</span></p>` : '';

  // HOW, not just WHAT.
  //
  // The chain showed the steps and the figures and said nothing about where
  // either came from — which is what makes a correct derivation still read as
  // a black box. A reader can see 97.2% and see the conclusion and has no
  // account of the move between them, nor of whether a language model made
  // it. This is that account, and the distinction it draws is the one that
  // matters most to somebody deciding how much to trust the number: the
  // figures are deterministic, the sentence about them is not, and the
  // sentence is checked back against the figures before it is published.
  const method = `
    <p class="insd-method">The figures below are computed by the optimiser and
      the KPI engine from the solved plan &mdash; no model estimates any of
      them. The reasoning layer then reads those figures, compares them with
      the configured policy thresholds and states the conclusion, and every
      number it quotes is checked back against the computed results before the
      finding is published.</p>`;

  // The whole derivation, as a file. A finding that has to survive being
  // forwarded to somebody who will never open this application needs to leave
  // it, and the sections a screen cannot hold — every record the finding was
  // computed over, what the model was given, what it could not establish —
  // are what that conversation asks for first.
  const download = `
    <button type="button" class="insd-download" id="insd-download-doc">
      ${ICON.download}
      <span>Download the full derivation</span>
      <span class="insd-download-ext">DOCX</span>
    </button>`;

  return `
    <div class="insd-card">
      <div class="insd-why-title">${ICON.gauge}<span>How this was reached</span></div>
      ${method}
      <ol class="insd-reason-chain">${stepsHtml}${conclusion}</ol>
      ${limitation}
      ${download}
    </div>`;
}

/**
 * The right column: what to do about this finding, and where to do it.
 *
 * THE CTA IS THE FINDING'S OWN, and it was not. This card led with the right
 * sentence and offered no way to act on it — the only buttons on the page
 * were a generic "Test a change as a scenario" and "Open in Digital Twin" in
 * a bar at the bottom, identical on every finding. So a capacity risk whose
 * tile on the Overview said "Open KPIs", and an unserved-demand risk whose
 * tile said "View affected demand", both arrived here offering the scenario
 * planner. The tile and the page it opens now resolve the destination through
 * the same function (`insightCta`), so they cannot disagree.
 *
 * The metric row carries evidence this finding actually cites rather than a
 * hashed cost delta, a hashed service gain and a risk band derived from them.
 * Where it cites fewer than three figures, fewer than three tiles are drawn —
 * the row is not padded to fill the grid.
 */
function recommendationCardHtml(record, cta) {
  const rec = NETWORK_RECOMMENDATION;

  // THIS finding's own recommended action, when it has one.
  //
  // Every insight carries one now — written by the Reasoning Agent, or the
  // theme-appropriate default `/api/insights` supplies — and it is the same
  // sentence the Overview's tile for this finding printed. Showing the
  // network-level recommendation instead meant a reader who pressed "View
  // detailed finding" on a capacity risk was given the advice about the
  // network's biggest cost component, which is a different subject.
  //
  // The network recommendation is not dropped: where the finding has none it
  // is still what this card shows, under the caveat that says so.
  const own = String(record.recommendedAction || '').trim();

  if (!own && !rec.text) {
    return `
      <div class="insd-card">
        <div class="insd-rec-head">${ICON.sparkle}Recommended action</div>
        <p class="insd-why-text">No recommendation has been generated for this
          network yet.</p>
      </div>`;
  }

  const ctaHtml = `
      <button type="button" class="insd-btn-primary insd-rec-cta" id="insd-rec-cta">
        <span>${insdEsc(cta.label)}</span>
        ${ICON.arrowRight}
      </button>`;

  const drivers = (rec.keyDrivers || []).length
    ? `<div class="insd-why-stat" style="display:block">
         <span class="insd-why-stat-label">Key drivers</span>
         <ul class="insd-details-body" style="margin-top:6px">${rec.keyDrivers
           .map(d => `<li>${insdEsc(d)}</li>`).join('')}</ul>
       </div>`
    : '';

  // The wider advice, kept as context under the finding's own step. Where the
  // finding has no step of its own, the network's IS the recommendation and
  // leads the card — with the caveat that it was drawn from every finding.
  const wider = (own && rec.text) ? `
      <div class="insd-why-stat" style="display:block">
        <span class="insd-why-stat-label">For the network as a whole</span>
        <div class="insd-why-text" style="margin-top:6px">${insdEsc(rec.text)}</div>
      </div>` : '';

  return `
    <div class="insd-card">
      <div class="insd-rec-head">${ICON.sparkle}Recommended action</div>
      <p class="insd-rec-sentence">${insdEsc(own || rec.text)}</p>
      ${ctaHtml}
      <div class="insd-why-row">
        <span class="insd-why-icon">${ICON.bulb}</span>
        <div style="flex:1;min-width:0">
          <div class="insd-why-title">Why this works</div>
          <div class="insd-why-text">${own
            ? 'This step follows from this finding and the figures above it.'
            : 'This is a network-level recommendation drawn from every finding, '
              + 'not from this one alone.'}</div>
          ${wider}
          ${drivers}
        </div>
      </div>
    </div>`;
}

/**
 * The other ways to act, after the finding's own.
 *
 * SECONDARY NOW, because the recommendation card above carries the step this
 * finding is actually asking for. These are the two general things any
 * finding can be taken into, and each is dropped when it would duplicate the
 * button already on the page: sending a reader to the scenario planner twice,
 * from two buttons with different labels, is two answers to one question.
 *
 * Neither claims to have changed anything. The previous version of this page
 * offered Approve / Reject buttons that only changed their own label, and an
 * "action taken" state that asserted an action had been taken when none had.
 *
 * The "Why this finding?" disclosure is gone with them. It revealed the
 * narrative — which is now the lede at the top of the page — and the question
 * it asked is answered at length by "How this was reached", in the body,
 * where a reader does not have to know to press anything to see it.
 */
function actionBarHtml(facilityId, cta) {
  const buttons = [];

  if (cta.tab !== 'scenarios') {
    buttons.push(`
      <button type="button" class="insd-btn-secondary" id="insd-run-scenario">
        ${ICON.play}<span>Test a change as a scenario</span></button>`);
  }
  if (cta.tab !== 'twin' && facilityId && facilityExists(facilityId)) {
    buttons.push(`
      <button type="button" class="insd-btn-outline-purple" id="insd-open-twin">
        ${ICON.cube}<span>Open in Digital Twin</span></button>`);
  }
  if (!buttons.length) return '';

  return `
    <div class="insd-action-bar">
      <span class="insd-action-bar-label">Or take it further</span>
      ${buttons.join('')}
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
  const cta = insightCta(record.theme, record.severity);
  const facility = insdFlow.facilityId
    ? getFacilityById(insdFlow.facilityId)
    : null;
  const scopeLine = facility
    ? `${insdEsc(record.theme)} · ${insdEsc(facility.name || facility.id)}`
    : `${insdEsc(record.theme)} · whole network`;

  // THE ORDER IS THE ARGUMENT.
  //
  //   what it says      the headline, then the prose explaining it
  //   what it rests on  the finding's own lead figure
  //   how it was read   the chart and the scale
  //   how it follows    the reasoning chain, then the evidence it cites
  //   what to do        the recommendation and the button that goes there
  //
  // Previously the prose sat under the chart in a footnote slot and the only
  // thing between the figures and the recommendation was a table.
  page.innerHTML = `
    <div class="insd-page">
      <button type="button" class="insd-back-link" id="insd-back-btn">${ICON.arrowLeft}<span>${insdOrigin.label}</span></button>

      <div class="insd-header-row">
        <h1 class="insd-title">${insdEsc(record.title)}</h1>
        <span class="insd-badge tone-${badge.tone}">${badge.label}</span>
      </div>
      <p class="insd-subtitle">${scopeLine}</p>

      ${descriptionHtml(record)}
      ${headlineBannerHtml(record)}

      <div class="insd-main-split">
        <div>
          ${findingCardHtml(record, plan)}
          ${reasoningCardHtml(record)}
        </div>
        ${recommendationCardHtml(record, cta)}
      </div>

      ${actionBarHtml(insdFlow.facilityId, cta)}
      ${footerNoteHtml(record)}
    </div>`;

  // After innerHTML, so the canvas exists. A frame of delay lets the layout
  // settle first: Chart.js sizes to the wrapper, and measuring it mid-paint
  // produced a chart one frame wide on a cold render.
  if (plan) requestAnimationFrame(() => renderInsightChart('insd-trend-chart', plan));
  bindDeepDive(cta);
}

function bindDeepDive(cta) {
  document.getElementById('insd-back-btn')?.addEventListener('click', backToOrigin);

  // The finding's own call to action. `tab: ''` is the unserved-demand case:
  // the breakdown lives in a drawer app.js owns, not on a tab, and it is
  // reached through the opener that module exposes rather than by this file
  // importing it — app.js imports this one, so the dependency cannot run
  // both ways.
  document.getElementById('insd-rec-cta')?.addEventListener('click', () => {
    if (!cta.tab) {
      if (typeof window.openDemandShortfallDetail === 'function') {
        window.openDemandShortfallDetail();
      }
      return;
    }
    if (typeof window.navigateToTab === 'function') window.navigateToTab(cta.tab);
  });

  document.getElementById('insd-download-doc')?.addEventListener('click',
    (e) => downloadDerivation(e.currentTarget));

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

/**
 * Fetch the finding's derivation and hand it to the browser to save.
 *
 * The button says what it is doing throughout: a document that takes a second
 * to build behind a button that does not change is a button a reader presses
 * twice. A failure says so ON the button rather than in a console nobody has
 * open — this is a thing the reader just asked for, and silence is the worst
 * answer to a click.
 */
async function downloadDerivation(button) {
  if (!button || button.disabled) return;
  const record = insdFlow.record;
  if (!record) return;

  const label = button.querySelector('span');
  const original = label ? label.textContent : '';
  button.disabled = true;
  if (label) label.textContent = 'Preparing\u2026';
  // A document takes a solve and, where the gateway is configured, a
  // text-generation call — which the gateway allows itself a minute for. A
  // button that says the same thing for that long reads as a hung one, so
  // the wait names its slow half rather than growing silent.
  const stage = setTimeout(() => {
    if (label && button.disabled) label.textContent = 'Writing the explanation\u2026';
  }, 5000);

  try {
    const mod = await import('./integration/services/insight-service.js');
    // The record's own scope, so a facility finding asks for the facility
    // briefing rather than the network one it is not in.
    const { blob, filename } = await mod.insightService.downloadDerivation(
      record.id, {
        scope: record.scope || (insdFlow.facilityId ? 'FACILITY' : 'NETWORK'),
        entityId: record.entity_id || insdFlow.facilityId || null,
      });
    // An object URL and a synthetic click: the only way to name a file the
    // browser saves from a fetch. Revoked immediately after — the blob is
    // held in memory until it is.
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename || 'derivation.docx';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    if (label) label.textContent = original;
  } catch (err) {
    if (label) label.textContent = 'Could not build the document';
    button.classList.add('is-failed');
    button.title = err && err.message ? err.message : '';
    setTimeout(() => {
      if (label) label.textContent = original;
      button.classList.remove('is-failed');
    }, 4000);
  } finally {
    clearTimeout(stage);
    button.disabled = false;
  }
}

/* ═══════════════════════════════════════════════════════════════
   Entry points / navigation
   ═══════════════════════════════════════════════════════════════ */

/* ═══════════════════════════════════════════════════════════════
   ACTION VIEW — a request to a person, not a finding about a network
   ───────────────────────────────────────────────────────────────
   The same page, a different thing on it. An action item comes from the
   deterministic completeness gate: a column the upload does not carry, and
   the sites it is missing from. Nothing was solved to produce it, so this
   view has no chart, no evidence table and no recommendation — there is no
   figure to plot and none to cite. What it has instead is the request, and
   the means to send it.

   The panel is on screen from the moment the page opens rather than behind
   a "compose" button. It is the whole reason to be on this page; hiding it
   behind a click would be one step of ceremony protecting nothing, and the
   reader would have to remember what the page was for.
   ═══════════════════════════════════════════════════════════════ */

const insdAction = {
  /** The action being looked at. */
  item: null,
  /** Addresses currently ticked, as a Set of lowercased emails. */
  selected: new Set(),
  /** Result of the last send on this page, shown until it is left. */
  outcome: null,
  sending: false,
};

function findAction(id) {
  return HOME_ACTION_ITEMS.find((a) => a.id === id) || null;
}

function insdValidEmail(value) {
  return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(String(value || '').trim());
}

/** The sites the field is missing from. Long lists collapse. */
function affectedHtml(item) {
  const names = item.entities || [];
  if (!names.length) {
    return `<p class="insd-action-note">This applies to the upload as a whole —
      the gate found no column for it anywhere in the file.</p>`;
  }
  const LIMIT = 8;
  const shown = names.slice(0, LIMIT);
  const hidden = names.slice(LIMIT);
  return `
    <div class="insd-affected">
      <div class="insd-affected-head">
        ${insdEsc(item.entityType || 'Record')}${names.length === 1 ? '' : 's'} affected
        <span class="insd-affected-count">${names.length}</span>
      </div>
      <ul class="insd-affected-list">
        ${shown.map((n) => `<li>${insdEsc(n)}</li>`).join('')}
      </ul>
      ${hidden.length ? `
      <details class="insd-affected-more">
        <summary>${hidden.length} more</summary>
        <ul class="insd-affected-list">
          ${hidden.map((n) => `<li>${insdEsc(n)}</li>`).join('')}
        </ul>
      </details>` : ''}
    </div>`;
}

/** What this field is, and what its absence costs. */
function whatsMissingHtml(item) {
  const required = item.severity === 'REQUIRED';
  const consequence = required
    ? `The analysis is running without it. Every figure that depends on this
       field is computed from the sites that do state it, so the network's
       totals are incomplete rather than wrong.`
    : (item.whatItUnlocks
        ? `Providing it ${insdEsc(item.whatItUnlocks.replace(/^would /, 'would '))}.`
        : 'The analysis ran without it.');
  return `
    <div class="insd-card">
      <div class="insd-chart-head">
        <div class="insd-chart-title">What is missing</div>
        <span class="insd-badge tone-${required ? 'risk' : 'info'}">
          ${required ? 'REQUIRED' : 'OPTIONAL'}</span>
      </div>
      <div class="insd-missing-field">
        ${insdEsc(item.displayLabel)}
        ${item.unit ? `<span class="insd-missing-unit">${insdEsc(item.unit)}</span>` : ''}
      </div>
      <p class="insd-action-note">${consequence}</p>
      ${affectedHtml(item)}
    </div>`;
}

/** The standing recipient list as ticks, plus a field for anyone else. */
function recipientsHtml() {
  if (!NOTIFICATION_RECIPIENTS.length) {
    return `
      <p class="insd-action-note">No addresses are saved yet. Add the person who
        owns this data below — they will be offered next time.</p>`;
  }
  return `
    <div class="insd-recipients">
      ${NOTIFICATION_RECIPIENTS.map((r) => {
        const on = insdAction.selected.has(r.email.toLowerCase());
        return `
        <label class="insd-recipient${on ? ' is-on' : ''}">
          <input type="checkbox" data-recipient="${insdEsc(r.email)}" ${on ? 'checked' : ''}>
          <span class="insd-recipient-label">${insdEsc(r.label || r.email)}</span>
          <span class="insd-recipient-email">${insdEsc(r.email)}</span>
        </label>`;
      }).join('')}
    </div>`;
}

/**
 * What will happen when this button is pressed, in the reader's own terms.
 *
 * `stub` is the state this build ships in — no outbound credential is
 * configured, so `EmailSender` logs the message and returns a labelled stub.
 * Saying so is not a disclaimer; it is the difference between a feature and a
 * lie about one.
 *
 * What it does NOT do is name an environment variable. This screen is read by
 * whoever owns the network, not by whoever deploys it: they cannot set
 * `NETGRAVITY_SMTP_HOST`, should not have to know it exists, and telling them
 * to is an instruction addressed to somebody who is not in the room. The
 * person who CAN set it reads `/api/status`, where `outbound_email` names the
 * variable and the reason.
 */
function deliveryNoteHtml() {
  if (EMAIL_DELIVERY.mode !== 'stub') return '';
  return `
    <p class="insd-delivery-note">
      ${ICON.info}
      <span><strong>Email is not switched on for this workspace yet.</strong>
        Pressing send will save the request and record who it is for, so the
        wording and the audit trail are ready — but the message will not leave
        this system. Ask whoever administers your NetGravity deployment to
        connect a mail server, and you can copy the message below in the
        meantime.</span>
    </p>`;
}

/** A moment a person can place, from a timestamp only a machine can. */
function insdWhen(value) {
  const at = new Date(value);
  if (!value || Number.isNaN(at.getTime())) return '';
  const sameDay = new Date().toDateString() === at.toDateString();
  const time = at.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  return sameDay ? `today at ${time}`
    : `on ${at.toLocaleDateString([], { day: 'numeric', month: 'short' })} at ${time}`;
}

/** "a@b.com and c@d.com", "a@b.com, c@d.com and e@f.com" — never "a, b, c". */
function insdList(values) {
  const items = (values || []).filter(Boolean);
  if (items.length <= 1) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

/**
 * What happened just now, or what happened last time.
 *
 * Four outcomes, not three. `partial` is the one that was missing and the one
 * that matters most: `SMTP.send_message` raises only when it rejects EVERY
 * address, so a mistyped address in a list of four used to come back as a
 * clean send and that person was never asked. It names them.
 */
function outcomeHtml() {
  const item = insdAction.item;
  const outcome = insdAction.outcome;
  const previous = item && item.lastSent;

  if (outcome) {
    const delivered = insdList(outcome.delivered && outcome.delivered.length
      ? outcome.delivered : outcome.recipients);
    const refused = insdList(outcome.refused);
    const tone = outcome.delivery === 'sent' ? 'good'
      : outcome.delivery === 'stubbed' ? 'info' : 'bad';
    let said;
    if (outcome.delivery === 'sent') {
      said = `Sent to ${delivered}.`;
    } else if (outcome.delivery === 'partial') {
      said = `Sent to ${delivered}, but the mail server would not accept `
        + `${refused} — so they have not been asked. Check the address and `
        + `send again to them alone.`;
    } else if (outcome.delivery === 'stubbed') {
      said = `Saved for ${delivered}. Nothing was delivered — email is not `
        + 'switched on for this workspace yet.';
    } else {
      // The real reason, which the endpoint used to swallow: a configured
      // mail server that rejected the message was reported as "no mail server
      // is configured", and the SMTP error went nowhere.
      said = `Not sent. ${outcome.notes || 'The mail server rejected the message.'}`;
    }
    return `<div class="insd-outcome tone-${tone}">${insdEsc(said)}</div>`;
  }
  if (previous) {
    // TO, not from — the one word in this sentence that says which direction
    // the request went, and it was the wrong one.
    const when = insdWhen(previous.sent_at);
    const who = insdList(previous.recipients || []);
    // The VERB follows the outcome. "Already sent to X ... It was saved but
    // not delivered" contradicts itself in its own second clause, and a
    // reader who stops at the first full stop has been told something false.
    const said = previous.result === 'stubbed'
      ? `Already saved for ${who}${when ? ` ${when}` : ''} — not delivered, `
        + 'because email is not switched on for this workspace yet.'
      : previous.result === 'failed'
        ? `Last attempt to reach ${who}${when ? ` ${when}` : ''} did not get through.`
        : previous.result === 'partial'
          ? `Already sent to some of ${who}${when ? ` ${when}` : ''} — the mail `
            + 'server refused the rest.'
          : `Already sent to ${who}${when ? ` ${when}` : ''}.`;
    return `<div class="insd-outcome tone-info">${insdEsc(said)}</div>`;
  }
  return '';
}

function requestPanelHtml(item) {
  const draft = item.draft || { subject: '', body: '' };
  return `
    <div class="insd-card insd-request">
      <div class="insd-chart-head">
        <div class="insd-chart-title">Request this data</div>
      </div>

      <label class="insd-field-label" for="insd-subject">Subject</label>
      <input class="insd-input" id="insd-subject" type="text"
             value="${insdEsc(draft.subject)}">

      <label class="insd-field-label">Send to</label>
      ${recipientsHtml()}
      <div class="insd-add-row">
        <input class="insd-input" id="insd-new-recipient" type="email"
               placeholder="someone@company.com"
               aria-label="Add another email address">
        <button type="button" class="insd-btn-secondary insd-btn-compact"
                id="insd-add-recipient">Add</button>
      </div>
      <p class="insd-field-error" id="insd-recipient-error" hidden></p>

      <label class="insd-field-label" for="insd-message">Message</label>
      <textarea class="insd-textarea" id="insd-message" rows="10"
                spellcheck="false">${insdEsc(draft.body)}</textarea>
      <p class="insd-action-note insd-note-tight">Written from the gap itself —
        the field name and the sites it is missing from. Edit anything before
        sending.</p>

      ${deliveryNoteHtml()}
      ${outcomeHtml()}

      <button type="button" class="insd-btn-primary insd-send-btn" id="insd-send">
        ${ICON.mail}<span>${EMAIL_DELIVERY.mode === 'stub'
          ? 'Save request' : 'Send request'}</span>
      </button>
    </div>`;
}

function renderActionDetail() {
  const page = document.getElementById('tab-insight-detail');
  const item = insdAction.item;
  if (!page || !item) return;

  const required = item.severity === 'REQUIRED';
  page.innerHTML = `
    <div class="insd-page">
      <button type="button" class="insd-back-link" id="insd-back-btn">${ICON.arrowLeft}<span>${insdOrigin.label}</span></button>

      <div class="insd-header-row">
        <h1 class="insd-title">${insdEsc(item.title)}</h1>
        <span class="insd-badge tone-${required ? 'risk' : 'info'}">
          ${required ? 'DATA NEEDED' : 'OPTIONAL DATA'}</span>
      </div>
      <p class="insd-subtitle">Action \u00b7 found by the data completeness check on your upload</p>

      <div class="insd-main-split">
        <div>
          ${whatsMissingHtml(item)}
          <!-- Inside the left column, not under both. The request panel is
               much the taller of the two, and a full-width bar under it left
               about three hundred pixels of empty page beside the card it
               belongs to. Here it also reads as what it is: the alternative
               to the request on the right. -->
          <div class="insd-action-bar insd-action-bar-inline">
            <button type="button" class="insd-btn-secondary" id="insd-upload-instead">
              ${ICON.upload}<span>Upload the data instead</span></button>
            <button type="button" class="insd-action-link" id="insd-why-btn">
              ${ICON.info}<span>Why is this needed?</span></button>
            <div class="insd-why-reveal" id="insd-why-reveal">${insdEsc(
              required
                ? `This field is on the required list because the network model reads
                   it directly. It is checked against the columns your workbook
                   actually carried, before any default is applied — so this says the
                   column was absent or blank for these sites, not that its value was
                   zero.`
                : `This field is on the optional list: the analysis completes without
                   it. It is checked against the columns your workbook actually
                   carried, so this says the column was absent or blank, not that its
                   value was zero.`
            )}</div>
          </div>
        </div>
        ${requestPanelHtml(item)}
      </div>

      <p class="insd-footer-note">Source: the deterministic data-completeness
        check over the columns your upload carried. No model was called to
        produce this item, and no figure on this page was estimated.</p>
    </div>`;

  bindActionDetail();
}

function bindActionDetail() {
  document.getElementById('insd-back-btn')?.addEventListener('click', backToOrigin);

  document.getElementById('insd-why-btn')?.addEventListener('click', () => {
    document.getElementById('insd-why-reveal')?.classList.toggle('open');
  });

  document.getElementById('insd-upload-instead')?.addEventListener('click', () => {
    if (typeof window.showUploadData === 'function') {
      const project = typeof window.getCurrentProject === 'function'
        ? window.getCurrentProject() : null;
      window.showUploadData(project);
    }
  });

  document.querySelectorAll('[data-recipient]').forEach((box) => {
    box.addEventListener('change', () => {
      const email = box.getAttribute('data-recipient').toLowerCase();
      if (box.checked) insdAction.selected.add(email);
      else insdAction.selected.delete(email);
      box.closest('.insd-recipient')?.classList.toggle('is-on', box.checked);
      refreshSendState();
    });
  });

  const addBtn = document.getElementById('insd-add-recipient');
  const addInput = document.getElementById('insd-new-recipient');
  const addError = document.getElementById('insd-recipient-error');

  const addRecipient = async () => {
    const email = (addInput?.value || '').trim();
    if (!email) return;
    if (!insdValidEmail(email)) {
      // Caught here rather than at send: an address is easiest to fix while
      // the person is still looking at the field they typed it into.
      if (addError) {
        addError.textContent = `"${email}" does not look like an email address.`;
        addError.hidden = false;
      }
      return;
    }
    if (addError) addError.hidden = true;
    if (!NOTIFICATION_RECIPIENTS.some(r => r.email.toLowerCase() === email.toLowerCase())) {
      NOTIFICATION_RECIPIENTS.push({ label: email, email });
    }
    insdAction.selected.add(email.toLowerCase());
    addInput.value = '';
    // Saved server-side too, so it is offered on the next action and to the
    // pipeline's own triggers — not just for the rest of this page view.
    try {
      const mod = await import('./integration/services/action-service.js');
      await mod.actionService.addRecipient(email);
    } catch (err) {
      console.warn('[actions] recipient saved locally only:', err.message);
    }
    renderActionDetail();
  };

  addBtn?.addEventListener('click', addRecipient);
  addInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); addRecipient(); }
  });

  document.getElementById('insd-send')?.addEventListener('click', sendRequest);
  refreshSendState();
}

/** Disabled with a reason, rather than failing after the click. */
function refreshSendState() {
  const btn = document.getElementById('insd-send');
  if (!btn) return;
  const none = insdAction.selected.size === 0;
  btn.disabled = none || insdAction.sending;
  btn.title = none ? 'Tick at least one recipient first' : '';
}

async function sendRequest() {
  const item = insdAction.item;
  if (!item || insdAction.sending) return;
  const to = [...insdAction.selected];
  if (!to.length) return;

  const btn = document.getElementById('insd-send');
  insdAction.sending = true;
  refreshSendState();
  if (btn) {
    btn.querySelector('span').textContent =
      EMAIL_DELIVERY.mode === 'stub' ? 'Saving\u2026' : 'Sending\u2026';
  }

  const subject = document.getElementById('insd-subject')?.value || '';
  const body = document.getElementById('insd-message')?.value || '';

  try {
    const mod = await import('./integration/services/action-service.js');
    const res = await mod.actionService.dispatch(item.id, { to, subject, body });
    insdAction.outcome = {
      delivery: res.delivery,
      recipients: (res.dispatch && res.dispatch.recipients) || to,
      // Who actually took the message and who the server turned away. A list
      // of four with one address refused is one person still waiting to be
      // asked, and only naming them makes that something anyone can act on.
      delivered: res.delivered || [],
      refused: res.refused || [],
      notes: res.notes || '',
    };
    item.lastSent = res.dispatch || item.lastSent;
  } catch (err) {
    // A failed send says so. The one thing this must never do is go quiet
    // and leave the reader believing a request went out.
    insdAction.outcome = { delivery: 'failed', recipients: to,
                           delivered: [], refused: [], notes: err.message };
  } finally {
    insdAction.sending = false;
    renderActionDetail();
  }
}

/**
 * Open the deep dive for one insight, or the action view for one action.
 *
 * `kind` used to be accepted and ignored, with a note saying the branch
 * belonged here once something produced action records. The data-completeness
 * gate now does, so this is that branch. An id matching neither store opens
 * nothing, rather than opening a page about the wrong thing.
 */
/**
 * The page the reader was on when they opened this one.
 *
 * "Back to Home" was literal: every route out of this page called
 * `navigateToTab('home')`, so a reader who opened a finding from the Insights
 * list was returned to the Overview and had to find their way back to the
 * list, losing their filter on the way. Nielsen #3 — a way out that goes
 * somewhere the reader did not come from is not an exit.
 *
 * Read off the DOM rather than passed in, because every caller would
 * otherwise have to remember to pass it, and the one that forgot would be
 * the bug this fixes.
 */
const INSD_ORIGINS = {
  'tab-insights': { tab: 'insights', label: 'Back to Insights', nav: 'nav-item-insights' },
  'tab-forecast': { tab: 'forecast', label: 'Back to Forecast', nav: 'nav-item-forecast' },
};
const INSD_ORIGIN_HOME = { tab: 'home', label: 'Back to Home', nav: 'nav-item-home' };
let insdOrigin = INSD_ORIGIN_HOME;

export function showInsightDetail(kind, id) {
  const action = (kind === 'action') ? findAction(id) : null;
  const hit = action ? null : findRecord(id);
  if (!action && !hit) return;

  // Before any panel is switched, so it reads the page being left.
  const from = document.querySelector('.tab-panel.active');
  insdOrigin = (from && INSD_ORIGINS[from.id]) || INSD_ORIGIN_HOME;

  if (action) {
    insdAction.item = action;
    insdAction.outcome = null;
    insdAction.sending = false;
    // Everyone on the standing list is ticked to begin with: that list is
    // "who generally wants to see this kind of thing", so the default is the
    // list, and un-ticking is the exception rather than the ritual.
    insdAction.selected = new Set(
      NOTIFICATION_RECIPIENTS.map((r) => r.email.toLowerCase()));
  } else {
    insdFlow.record = hit.record;
    insdFlow.facilityId = hit.facilityId;
  }

  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  const page = document.getElementById('tab-insight-detail');
  if (page) page.classList.add('active');

  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById(insdOrigin.nav)?.classList.add('active');

  const subTopbar = document.getElementById('app-sub-topbar');
  if (subTopbar) subTopbar.style.display = 'none';
  const btnUpload = document.getElementById('btn-topbar-upload');
  if (btnUpload) btnUpload.style.display = 'none';

  if (action) renderActionDetail();
  else renderDeepDive();
  // The page area is the scroll container, not the window.
  if (typeof window.scrollPageToTop === 'function') window.scrollPageToTop();
  else window.scrollTo({ top: 0, behavior: 'smooth' });
}

function backToOrigin() {
  // Chart.js keeps a live instance bound to a canvas this page is about to
  // discard. Destroying it here keeps one instance per canvas at most.
  Object.keys(insdCharts).forEach((id) => {
    insdCharts[id].destroy();
    delete insdCharts[id];
  });
  if (typeof window.navigateToTab === 'function') {
    window.navigateToTab(insdOrigin.tab);
  }
}

export function initInsightDetail() {
  if (typeof window !== 'undefined') {
    window.showInsightDetail = showInsightDetail;
  }
}
