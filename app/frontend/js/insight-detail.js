/**
 * Netgravity — Insight Deep Dive
 * ===============================
 * Replaces the old drawer-based insight/recommendation detail panels.
 * Clicking a card in Home's attention feed navigates here instead —
 * a full page, same app shell (sidebar/topbar) as every other tab.
 *
 *   Home attention feed → DEEP DIVE (this page)
 *                          └→ "Approve recommendation" → REVIEW MODAL
 *                                                         └→ action taken
 *                          └→ "Run scenario" → Scenario Planning tab
 *
 * Every insight/action is assigned one of four execution types, per the
 * product spec: 'auto' (the model can execute it — full review-and-send
 * modal, then an action-taken state), 'scenario' (the real next step is
 * simulating it in Scenario Planning), 'human' (suggest only — a person
 * has to act), 'none' (informational, nothing to do).
 *
 * References: Dump/Insights deep dive.jpeg, Dump/action to be taken.png,
 * Dump/insight action taken.png.
 *
 * STATUS: PROTOTYPE / MOCKED — deep-dive content (trend charts, before/
 * after figures, email drafts) is generated deterministically from the
 * existing HOME_INSIGHTS/HOME_ACTION_ITEMS records, not real telemetry.
 */

import { HOME_INSIGHTS, HOME_ACTION_ITEMS, getFacilityById, getOptimizedBaseCase, getUtilLabel, GOVERNANCE_TIERS } from './data.js';

/* ─── Execution type per item ──────────────────────────────────
   Explicit rather than inferred from free-text `nextAction`, so the
   mix of auto/scenario/human/none is deliberate and easy to audit. */
const EXECUTION_TYPE = {
  INS_CAP_RISK: 'auto',
  INS_INVESTIGATE_DELHI: 'scenario',
  INS_EXPLORE_UNDERUTIL: 'auto',
  INS_UNDERUTIL_CAP: 'auto',
  INS_KOLKATA_SPARE: 'auto',
  INS_MUMBAI_SLA: 'human',
  INS_MUMBAI_COST: 'none',
  INS_SPARE_CAP: 'scenario',
  INS_RECOMMENDATION: 'auto',
  INS_PERF_STABLE: 'none',
  ACT_REBALANCE_BADDI: 'auto',
  ACT_INVESTIGATE_DELHI: 'human',
  ACT_EXPLORE_UNDERUTIL: 'scenario',
};

const KNOWN_FACILITIES = [
  'Delhi NCR DC', 'Mumbai DC', 'Bengaluru DC', 'Kolkata DC', 'Guwahati DC',
  'Baddi Plant', 'Pune Plant',
];

/* ─── Icons ──────────────────────────────────────────────────── */
const ICON = {
  arrowLeft: `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 15l-5-5 5-5"/></svg>`,
  arrowRight: `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 10h10M11 6l4 4-4 4"/></svg>`,
  chevronRight: `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="7 4 13 10 7 16"/></svg>`,
  chevronDown: `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="5 8 10 13 15 8"/></svg>`,
  rupee: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="4" x2="18" y2="4"/><line x1="6" y1="8" x2="18" y2="8"/><path d="M6 8c5 0 8 1.5 8 4.5S11 17 6 17"/><line x1="6" y1="17" x2="15" y2="21"/></svg>`,
  trendUp: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 17 9 11 13 15 21 6"/><polyline points="14 6 21 6 21 13"/></svg>`,
  shield: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>`,
  bulb: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 22h4M12 2a6 6 0 0 0-4 10.5c.6.6 1 1.4 1 2.5h6c0-1.1.4-1.9 1-2.5A6 6 0 0 0 12 2z"/></svg>`,
  sparkle: `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l1.8 5.6L19.4 9.4l-5.6 1.8L12 17l-1.8-5.8L4.6 9.4l5.6-1.8L12 2z"/></svg>`,
  check: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`,
  close: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`,
  play: `<svg viewBox="0 0 24 24" fill="currentColor"><polygon points="6 3 20 12 6 21 6 3"/></svg>`,
  cube: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.7l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.7l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.3 7 12 12 20.7 7"/><line x1="12" y1="22" x2="12" y2="12"/></svg>`,
  info: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.5"/><line x1="12" y1="11" x2="12" y2="16.5"/><circle cx="12" cy="7.8" r="0.6" fill="currentColor" stroke="none"/></svg>`,
  lock: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="9" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>`,
  refresh: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>`,
  mail: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg>`,
  bell: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>`,
  calendar: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>`,
  edit: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>`,
  gear: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`,
  listCheck: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="m3 8 2 2 4-4"/><path d="M13 6h8"/><path d="m3 16 2 2 4-4"/><path d="M13 18h8"/></svg>`,
};

/* ─── Deterministic pseudo-random helpers (mocked content only) ────── */
function insdHashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}
function synth(id, salt, min, max) {
  return min + (insdHashStr(id + ':' + salt) % (max - min + 1));
}
function insdEsc(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
function firstPctIn(text) {
  const m = /(\d+(?:\.\d+)?)\s?%/.exec(text || '');
  return m ? parseFloat(m[1]) : null;
}
function extractFacilities(text) {
  const found = [];
  KNOWN_FACILITIES.forEach(name => {
    if (text.includes(name) && !found.includes(name)) found.push(name);
  });
  return found;
}

/* ─── Flow state ─────────────────────────────────────────────── */
const insdFlow = {
  vm: null,          // current view model
  shiftPct: null,     // editable in the modal / sentence
  actionState: 'pending', // 'pending' | 'taken'
  messageSent: false,
};

/* ═══════════════════════════════════════════════════════════════
   View-model construction
   ═══════════════════════════════════════════════════════════════ */
function findRaw(kind, id) {
  if (kind === 'action') {
    return HOME_ACTION_ITEMS.find(a => a.id === id) || null;
  }
  for (const fac in HOME_INSIGHTS) {
    const found = HOME_INSIGHTS[fac].find(i => i.id === id);
    if (found) return found;
  }
  return null;
}

function categorize(text) {
  const t = (text || '').toLowerCase();
  if (t.includes('high value')) return 'Recommendation';
  if (t.includes('high impact')) return 'Capacity Risk';
  if (t.includes('medium impact')) return 'Service Risk';
  if (t.includes('opportunity') || t.includes('optimization')) return 'Network Opportunity';
  if (t.includes('positive') || t.includes('normal')) return 'Performance Update';
  return 'Status';
}

function priorityBadge(category) {
  if (category === 'Capacity Risk' || category === 'Recommendation') return { label: 'High priority', tone: 'red' };
  if (category === 'Service Risk') return { label: 'Medium priority', tone: 'amber' };
  if (category === 'Network Opportunity') return { label: 'Opportunity', tone: 'green' };
  if (category === 'Performance Update') return { label: 'Stable', tone: 'green' };
  return { label: 'Informational', tone: 'gray' };
}

function buildViewModel(kind, id) {
  const raw = findRaw(kind, id);
  if (!raw) return null;

  const executionType = EXECUTION_TYPE[id] || (kind === 'action' ? 'scenario' : 'auto');
  const category = categorize(kind === 'action' ? raw.tag : raw.impact);
  const badge = priorityBadge(category);

  const title = raw.title;
  const subtitle = kind === 'action'
    ? (raw.why || '').split(/(?<=[.!?])\s/)[0]
    : (raw.subtitle || '');
  const whatIFound = kind === 'action' ? raw.why : (raw.detail?.whatIFound || raw.subtitle || '');
  const whyItMatters = kind === 'action' ? raw.why : (raw.detail?.whyItMatters || '');
  const recommendationSentence = kind === 'action'
    ? raw.title + '.'
    : (raw.detail?.recommendation || raw.title + '.');

  // Facilities mentioned, for the trend chart label + before/after rows.
  const corpus = [title, subtitle, whatIFound, recommendationSentence].join(' ');
  const facilities = extractFacilities(corpus);
  const facA = facilities[0] || (getFacilityById('DC_DELHI') || {}).name || 'Primary facility';
  const facB = facilities[1] || (getFacilityById('DC_KOLKATA') || {}).name || 'Target facility';

  // Current metric (%) driving the trend chart — parsed from real text
  // where possible, synthesized (deterministically, per id) otherwise.
  const currentPct = firstPctIn(whatIFound) || firstPctIn(subtitle) || synth(id, 'cur', 62, 94);
  const threshold = category === 'Capacity Risk' || category === 'Network Opportunity' ? 90
    : category === 'Service Risk' ? 95 : 85;

  const shiftPct = synth(id, 'shift', 8, 18);

  // Cost / service / risk chips — real figures where the underlying data
  // has them (HOME_ACTION_ITEMS.expectedImpact), synthesized otherwise.
  const costDeltaLakh = raw.expectedImpact
    ? (parseFloat((raw.expectedImpact.cost || '').replace(/[^\d.]/g, '')) || synth(id, 'cost', 6, 18))
    : synth(id, 'cost', 6, 18);
  const serviceGainPts = raw.expectedImpact
    ? (synth(id, 'svc', 8, 20) / 10)
    : (synth(id, 'svc', 8, 20) / 10);
  // Risk level: derived from the same utilization risk band (getUtilLabel)
  // S2/S6/S8/S9/S-Decision-1 all already use — not an independently
  // invented High/Medium/Low keyed off the priority badge's color, which
  // had no connection to any actual utilization or capacity figure.
  const riskFrom = getUtilLabel(currentPct);
  const facAAfterDefault = Math.max(40, Math.round(currentPct - (currentPct - threshold + 4)));
  const riskTo = getUtilLabel(facAAfterDefault);

  const riskAmountLakh = synth(id, 'risk', Math.max(10, Math.round(costDeltaLakh * 1.3)), Math.max(14, Math.round(costDeltaLakh * 1.8)));

  const whyThisWorks = kind === 'action'
    ? (raw.rootCause && raw.rootCause[0] ? `${raw.rootCause[0].label}: ${raw.rootCause[0].value}.` : whyItMatters)
    : (raw.detail?.evidence && raw.detail.evidence[0]
      ? (typeof raw.detail.evidence[0] === 'string' ? raw.detail.evidence[0] : `${raw.detail.evidence[0].label}: ${raw.detail.evidence[0].value}.`)
      : whyItMatters);

  const whyStatValue = synth(id, 'spare', 18, 34);

  return {
    kind, id, raw, executionType, category, badge,
    title, subtitle, whatIFound, whyItMatters, recommendationSentence,
    facA, facB, currentPct, threshold, shiftPct,
    costDeltaLakh, serviceGainPts, riskFrom, riskTo, riskAmountLakh,
    whyThisWorks, whyStatValue,
    email: {
      to: 'Priya Mehta (Regional Planning Analyst)',
      cc: 'West Region Operations',
      subject: `Proposed volume shift: ${facA} → ${facB}`,
    },
  };
}

/* Recomputed as the shift % is edited (sentence input or modal table). */
// S-Decision-1: Before/After Cost and Risk must come from S6's and S9's
// authoritative outputs, never recomputed independently here.
function computeAfterValues(vm, shiftPct) {
  const ratio = shiftPct / vm.shiftPct;
  const facAAfter = Math.max(40, Math.round(vm.currentPct - (vm.currentPct - vm.threshold + 4) * ratio));
  const facBBase = Math.max(40, vm.currentPct - synth(vm.id, 'facb', 12, 22));
  const facBAfter = Math.min(96, Math.round(facBBase + (vm.currentPct - facAAfter) * 0.9));

  // Cost: "Before" is S6's authoritative Actual/baseline Total Network Cost
  // (getOptimizedBaseCase().baseline) — not an independently invented
  // figure. "After" applies this insight's own claimed savings on top of
  // that authoritative base, scaled to how much of the recommended shift
  // is actually being applied (the editable % in the modal).
  const baselineCost = getOptimizedBaseCase().baseline.totalCost / 100000;
  const costBeforeLakh = baselineCost.toFixed(2);
  const costAfterLakh = (baselineCost - vm.costDeltaLakh * ratio).toFixed(2);

  const serviceBefore = (94.6).toFixed(1);
  const serviceAfter = (94.6 + vm.serviceGainPts * ratio).toFixed(1);

  // Risk: derived from the same utilization risk band (getUtilLabel) that
  // S2/S6/S8/S9 all already use — not an independently invented label.
  const riskFrom = getUtilLabel(vm.currentPct);
  const riskTo = getUtilLabel(facAAfter);

  return {
    facAAfter, facBBefore: Math.round(facBBase), facBAfter,
    costBeforeLakh, costAfterLakh, serviceBefore, serviceAfter,
    riskFrom, riskTo,
  };
}

/* ═══════════════════════════════════════════════════════════════
   Chart
   ═══════════════════════════════════════════════════════════════ */
const insdChartInstances = {};
function renderTrendChart(canvasId, vm) {
  if (insdChartInstances[canvasId]) insdChartInstances[canvasId].destroy();
  const ctx = document.getElementById(canvasId);
  if (!ctx || typeof Chart === 'undefined') return;

  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul'];
  const start = Math.max(48, vm.currentPct - 22);
  const actual = [0, 1, 2, 3, 4].map(i => Math.round(start + (vm.currentPct - start) * (i / 4)));
  const h = insdHashStr(vm.id);
  const projected = [actual[4], Math.round(vm.currentPct + 3 - (h % 7)), Math.round(vm.currentPct + (h % 5) - 3)];

  const actualData = [...actual, null, null];
  const projectedData = [null, null, null, null, ...projected];
  const thresholdData = months.map(() => vm.threshold);

  insdChartInstances[canvasId] = new Chart(ctx, {
    type: 'line',
    data: {
      labels: months,
      datasets: [
        {
          label: 'Actual',
          data: actualData,
          borderColor: '#6B2FA0',
          backgroundColor: 'rgba(107,47,160,.06)',
          borderWidth: 2.5,
          pointRadius: 3,
          pointBackgroundColor: '#6B2FA0',
          tension: 0.25,
        },
        {
          label: 'Projected',
          data: projectedData,
          borderColor: '#9218EA',
          borderDash: [6, 4],
          borderWidth: 2.5,
          pointRadius: 3,
          pointBackgroundColor: '#9218EA',
          tension: 0.25,
        },
        {
          label: `Preferred threshold ${vm.threshold}%`,
          data: thresholdData,
          borderColor: '#ef4444',
          borderDash: [3, 3],
          borderWidth: 1.5,
          pointRadius: 0,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
      scales: {
        y: { min: 0, max: 100, ticks: { callback: v => v + '%' }, grid: { color: '#f3f4f6' } },
        x: { grid: { display: false } },
      },
    },
  });
}

/* ═══════════════════════════════════════════════════════════════
   Deep-dive page
   ═══════════════════════════════════════════════════════════════ */
function metricHtml(iconSvg, tone, label, val, sub) {
  return `
    <div class="insd-metric">
      <div class="insd-metric-head"><span class="insd-metric-icon tone-${tone}">${iconSvg}</span>${label}</div>
      <div class="insd-metric-val tone-${tone}">${val}</div>
      <div class="insd-metric-sub">${sub}</div>
    </div>`;
}

// riskFrom/riskTo are now Healthy/Stress/Critical (getUtilLabel), not the
// old High/Medium/Low — this maps the chip's color/caption off the real
// band instead of a literal 'Medium' string match that would otherwise
// never match again, and never claims "Improved" when the band is
// actually unchanged.
function riskToneFor(riskTo) {
  return riskTo === 'Critical' ? 'red' : riskTo === 'Stress' ? 'amber' : 'green';
}
function riskCaptionFor(riskFrom, riskTo) {
  return INSD_RISK_RANK[riskTo] < INSD_RISK_RANK[riskFrom] ? 'Improved' : 'Unchanged';
}

function recommendationCardHtml(vm) {
  // Only 'auto' items are genuinely reallocation-style — the editable
  // "Shift X%..." sentence and the target-facility capacity stat both
  // presuppose that framing. 'scenario'/'human'/'none' items use their
  // own real recommendation text instead (e.g. "Monitor closely and
  // prepare carrier contingency"), which isn't about moving volume.
  const isReallocation = vm.executionType === 'auto';
  const after = computeAfterValues(vm, vm.shiftPct);

  const sentence = isReallocation
    ? `Shift <input type="number" class="insd-edit-pct" id="insd-sentence-pct" min="1" max="60" value="${vm.shiftPct}">% of volume from ${insdEsc(vm.facA)} to ${insdEsc(vm.facB)}.`
    : insdEsc(vm.recommendationSentence);

  const metricsRow = vm.executionType === 'none' ? '' : `
      <div class="insd-metrics-row">
        ${metricHtml(ICON.rupee, 'green', 'Cost impact', `↓ ₹${vm.costDeltaLakh}L / month`, `(${(vm.costDeltaLakh / 12).toFixed(1)}% improvement)`)}
        ${metricHtml(ICON.trendUp, 'green', 'Service level', `↑ ${vm.serviceGainPts.toFixed(1)} pts`, `(to ${after.serviceAfter}%)`)}
        ${metricHtml(ICON.shield, riskToneFor(vm.riskTo), 'Risk level', `${vm.riskFrom} → ${vm.riskTo}`, riskCaptionFor(vm.riskFrom, vm.riskTo))}
      </div>`;

  const whyStat = isReallocation ? `
          <div class="insd-why-stat">
            <span class="insd-why-stat-label">${insdEsc(vm.facB)} available capacity<span class="insd-why-stat-sub">~${(vm.whyStatValue * 128).toLocaleString('en-IN')} MT / month</span></span>
            <span class="insd-why-stat-value">${vm.whyStatValue}%</span>
          </div>` : '';

  return `
    <div class="insd-card">
      <div class="insd-rec-head">${ICON.sparkle}Recommended action</div>
      <div class="insd-rec-sentence" id="insd-rec-sentence">${sentence}</div>
      ${metricsRow}
      <div class="insd-why-row">
        <span class="insd-why-icon">${ICON.bulb}</span>
        <div style="flex:1;min-width:0">
          <div class="insd-why-title">Why this works</div>
          <div class="insd-why-text">${insdEsc(vm.whyThisWorks)}</div>
          ${whyStat}
        </div>
      </div>
    </div>`;
}

function actionTakenCardHtml(vm) {
  const after = computeAfterValues(vm, vm.shiftPct);
  const now = new Date();
  const timeStr = now.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' }) + ', '
    + now.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });

  const detailsRows = `
    <div class="insd-detail-row">${ICON.gear}<div><div class="insd-detail-label">What I changed</div><div class="insd-detail-val">Reallocated ${vm.shiftPct}% of volume from ${insdEsc(vm.facA)} to ${insdEsc(vm.facB)}.</div></div></div>
    <div class="insd-detail-row">${ICON.calendar}<div><div class="insd-detail-label">Executed</div><div class="insd-detail-val">${timeStr}</div></div></div>
    <div class="insd-detail-row">${ICON.trendUp}<div><div class="insd-detail-label">Operational changes</div><div class="insd-detail-val">${insdEsc(vm.facA)} utilisation: ${vm.currentPct}% → ${after.facAAfter}%  |  ${insdEsc(vm.facB)}: ${after.facBBefore}% → ${after.facBAfter}%<br>3 lanes updated</div></div></div>
    <div class="insd-detail-row">${ICON.bell}<div><div class="insd-detail-label">Follow-up</div><div class="insd-detail-val">${vm.messageSent ? "I've notified the regional planning analyst<br>" : ''}Recheck scheduled in 7 days</div></div></div>
  `;

  return `
    <div class="insd-card insd-taken-card">
      <div class="insd-taken-head">
        <span class="insd-taken-check">${ICON.check}</span>
        <span class="insd-taken-title">I've taken action</span>
        <span class="insd-taken-pill">Completed</span>
      </div>
      <div class="insd-taken-time">${timeStr}</div>
      <div class="insd-taken-summary">I shifted <b>${vm.shiftPct}%</b> of volume from ${insdEsc(vm.facA)} to ${insdEsc(vm.facB)} to reduce the capacity risk.</div>
      <div class="insd-metrics-row">
        ${metricHtml(ICON.rupee, 'green', 'Cost impact', `↓ ₹${vm.costDeltaLakh}L / month`, `(${(vm.costDeltaLakh / 12).toFixed(1)}% improvement)`)}
        ${metricHtml(ICON.trendUp, 'green', 'Service level', `↑ ${vm.serviceGainPts.toFixed(1)} pts`, `(to ${after.serviceAfter}%)`)}
        ${metricHtml(ICON.shield, riskToneFor(vm.riskTo), 'Risk level', `${vm.riskFrom} → ${vm.riskTo}`, riskCaptionFor(vm.riskFrom, vm.riskTo))}
      </div>
      <button type="button" class="insd-details-toggle" id="insd-details-toggle">${ICON.chevronDown}<span>Action details</span></button>
      <div class="insd-details-body" id="insd-details-body">
        ${detailsRows}
        <button type="button" class="insd-action-link" id="insd-taken-why-btn">${ICON.info}<span>Why this was recommended</span></button>
        <div class="insd-why-reveal" id="insd-taken-why-reveal">${insdEsc(vm.whyThisWorks)}</div>
        ${vm.messageSent ? `
          <button type="button" class="insd-view-message-link" id="insd-view-message">${ICON.mail}View message sent${ICON.chevronRight}</button>
          <div class="insd-sent-message-box hidden" id="insd-sent-message-box">${insdEsc(emailBody(vm, after))}</div>
        ` : ''}
      </div>
    </div>`;
}

function riskBannerHtml(vm, taken) {
  if (vm.executionType === 'none') return '';
  return `
    <div class="insd-risk-banner${taken ? ' tone-taken' : ''}">
      <span class="insd-risk-icon">${ICON.rupee}</span>
      <div class="insd-risk-body">
        <div class="insd-risk-amount">₹${vm.riskAmountLakh}L / month ${taken ? 'was at risk' : 'at risk'}</div>
        <div class="insd-risk-note">If no action ${taken ? 'was' : 'is'} taken</div>
      </div>
      ${taken ? `<button type="button" class="insd-risk-link" id="insd-view-analysis">View analysis details${ICON.chevronRight}</button>` : ''}
    </div>`;
}

function actionBarHtml(vm) {
  const taken = insdFlow.actionState === 'taken';
  const parts = [];

  if (vm.executionType === 'auto') {
    if (!taken) {
      parts.push(`<button type="button" class="insd-btn-primary" id="insd-approve-btn">${ICON.check}<span>Approve recommendation</span></button>`);
    }
    parts.push(`<button type="button" class="insd-btn-secondary" id="insd-run-scenario-btn">${ICON.play}<span>Run scenario</span></button>`);
  } else if (vm.executionType === 'scenario') {
    parts.push(`<button type="button" class="insd-btn-primary" id="insd-run-scenario-btn">${ICON.play}<span>Run scenario</span></button>`);
  } else if (vm.executionType === 'human') {
    if (!taken) {
      parts.push(`<button type="button" class="insd-btn-secondary" id="insd-acknowledge-btn">${ICON.check}<span>Acknowledge</span></button>`);
    } else {
      parts.push(`<span class="insd-note-pill">${ICON.info}Flagged for manual follow-up</span>`);
    }
  }

  if (vm.executionType !== 'none') {
    parts.push(`<button type="button" class="insd-action-link" id="insd-why-btn">${ICON.info}<span>Why this recommendation?</span></button>`);
  }
  parts.push(`<button type="button" class="insd-action-link" id="insd-twin-btn">${ICON.cube}<span>View in Digital Twin</span></button>`);
  parts.push(`<div class="insd-why-reveal" id="insd-why-reveal">${insdEsc(vm.whyItMatters || vm.whatIFound)}</div>`);

  return `<div class="insd-action-bar">${parts.join('')}</div>`;
}

function footerNoteHtml(vm) {
  const taken = insdFlow.actionState === 'taken';
  const text = taken
    ? "I'll continue monitoring this risk and update you if conditions change."
    : 'AI-generated insight. Please review before taking action.';
  return `<div class="insd-footer-note">${ICON.lock}<span>${text}</span></div>`;
}

// 'none'-type items are "all is well" stories (e.g. a cost reduction
// already achieved, or a facility performing within range) — forcing a
// synthesized percentage trend chart onto them reads as a mismatch, since
// their underlying data isn't naturally a "value approaching a threshold"
// story the way a capacity/SLA risk is. They get a plain confirmation
// panel instead of a chart.
function leftCardHtml(vm) {
  if (vm.executionType === 'none') {
    return `
      <div class="insd-card">
        <div class="insd-chart-head">
          <div class="insd-chart-title">Status</div>
        </div>
        <div class="insd-why-row">
          <span class="insd-taken-check" style="width:34px;height:34px">${ICON.check}</span>
          <div style="flex:1;min-width:0">
            <div class="insd-why-title">No action needed</div>
            <div class="insd-why-text">${insdEsc(vm.whatIFound)}</div>
          </div>
        </div>
      </div>`;
  }

  const chartTitle = vm.category === 'Network Opportunity' || vm.category === 'Capacity Risk'
    ? 'Utilization trend (%)'
    : vm.category === 'Service Risk' ? 'Service level trend (%)' : 'Trend (%)';

  return `
    <div class="insd-card">
      <div class="insd-chart-head">
        <div class="insd-chart-title">${chartTitle}</div>
        <div class="insd-chart-legend">
          <span class="insd-legend-item"><span class="insd-legend-swatch"></span>Actual</span>
          <span class="insd-legend-item"><span class="insd-legend-swatch dashed"></span>Projected</span>
        </div>
      </div>
      <div class="insd-chart-canvas-wrap"><canvas id="insd-trend-chart"></canvas></div>
      <div class="insd-chart-note">${ICON.trendUp}<span>${insdEsc(vm.whatIFound)}</span></div>
    </div>`;
}

function renderDeepDive() {
  const page = document.getElementById('tab-insight-detail');
  const vm = insdFlow.vm;
  if (!page || !vm) return;

  const taken = insdFlow.actionState === 'taken';
  const rightCard = vm.executionType === 'auto' && taken ? actionTakenCardHtml(vm) : recommendationCardHtml(vm);

  page.innerHTML = `
    <div class="insd-page">
      <button type="button" class="insd-back-link" id="insd-back-btn">${ICON.arrowLeft}<span>Back to Home</span></button>

      <div class="insd-header-row">
        <h1 class="insd-title">${insdEsc(vm.title)}</h1>
        <span class="insd-badge tone-${vm.badge.tone}">${vm.badge.label}</span>
      </div>
      <p class="insd-subtitle">${insdEsc(vm.subtitle)}</p>

      ${riskBannerHtml(vm, taken && vm.executionType === 'auto')}

      <div class="insd-main-split">
        ${leftCardHtml(vm)}
        ${rightCard}
      </div>

      ${actionBarHtml(vm)}
      ${footerNoteHtml(vm)}
    </div>`;

  if (vm.executionType !== 'none') {
    setTimeout(() => renderTrendChart('insd-trend-chart', vm), 30);
  }
  bindDeepDive();
}

function bindDeepDive() {
  const vm = insdFlow.vm;

  document.getElementById('insd-back-btn')?.addEventListener('click', backToHome);

  document.getElementById('insd-sentence-pct')?.addEventListener('change', e => {
    const val = Math.max(1, Math.min(60, Number(e.target.value) || vm.shiftPct));
    insdFlow.shiftPct = val;
    renderDeepDive();
  });

  document.getElementById('insd-approve-btn')?.addEventListener('click', () => openActionReviewModal());

  document.getElementById('insd-run-scenario-btn')?.addEventListener('click', () => {
    if (typeof window.navigateToTab === 'function') window.navigateToTab('scenarios');
  });

  document.getElementById('insd-acknowledge-btn')?.addEventListener('click', () => {
    insdFlow.actionState = 'taken';
    renderDeepDive();
  });

  document.getElementById('insd-why-btn')?.addEventListener('click', () => {
    document.getElementById('insd-why-reveal')?.classList.toggle('open');
  });

  // S11 P1: link to original reasoning from the post-approval receipt —
  // reuses vm.whyThisWorks (already computed for the pre-approval card),
  // not a new explanation.
  document.getElementById('insd-taken-why-btn')?.addEventListener('click', () => {
    document.getElementById('insd-taken-why-reveal')?.classList.toggle('open');
  });

  document.getElementById('insd-twin-btn')?.addEventListener('click', () => {
    if (typeof window.navigateToTab === 'function') window.navigateToTab('twin');
  });

  document.getElementById('insd-view-analysis')?.addEventListener('click', () => {
    document.getElementById('insd-why-reveal')?.classList.toggle('open');
    document.getElementById('insd-why-reveal')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  });

  const toggle = document.getElementById('insd-details-toggle');
  const body = document.getElementById('insd-details-body');
  if (toggle && body) {
    toggle.addEventListener('click', () => {
      const collapsed = body.classList.toggle('hidden');
      toggle.classList.toggle('collapsed', collapsed);
    });
  }

  document.getElementById('insd-view-message')?.addEventListener('click', () => {
    document.getElementById('insd-sent-message-box')?.classList.toggle('hidden');
  });
}

/* ═══════════════════════════════════════════════════════════════
   "Review proposed action" modal
   ═══════════════════════════════════════════════════════════════ */
function emailBody(vm, after) {
  return `Hi ${vm.email.to.split(' (')[0]},

I identified a ${vm.category.toLowerCase()} at ${vm.facA}, with utilization expected to reach ${vm.currentPct}% next month.

I recommend shifting ${vm.shiftPct}% of volume from ${vm.facA} to ${vm.facB}. Based on the current network model, this is expected to reduce ${vm.facA} utilization to ${after.facAAfter}%, lower monthly network cost by approximately ₹${vm.costDeltaLakh}L, and improve service level by ${vm.serviceGainPts.toFixed(1)} percentage points.

Please review the proposed allocation change and confirm implementation readiness.

Regards,
Netgravity`;
}

const INSD_RISK_RANK = { Healthy: 0, Stress: 1, Critical: 2 };

function modalTableRows(vm, after) {
  // vm.costDeltaLakh is an insight-specific savings figure authored
  // independently of S6's real network total — for some insights it can
  // exceed the authoritative baseline (a holdover from when this modal
  // showed an unrelated, much larger fictional cost). Rather than display
  // an impossible negative "after" cost, show "Not available" — an honest
  // gap, not a fabricated number.
  const costAfterValid = Number(after.costAfterLakh) > 0;
  const costAfterDisplay = costAfterValid ? `₹${after.costAfterLakh}L` : 'Not available';
  const costGood = costAfterValid && Number(after.costAfterLakh) < Number(after.costBeforeLakh);

  const rows = [
    [`${vm.facA} utilization`, `${vm.currentPct}%`, `${after.facAAfter}%`, after.facAAfter < vm.currentPct],
    [`${vm.facB} utilization`, `${after.facBBefore}%`, `${after.facBAfter}%`, true],
    ['Monthly network cost', `₹${after.costBeforeLakh}L`, costAfterDisplay, costGood],
    ['Service level', `${after.serviceBefore}%`, `${after.serviceAfter}%`, true],
    ['Risk', after.riskFrom, after.riskTo, INSD_RISK_RANK[after.riskTo] <= INSD_RISK_RANK[after.riskFrom]],
  ];
  return rows.map(([label, before, afterVal, good]) => `
    <tr>
      <td>${insdEsc(label)}</td>
      <td>${insdEsc(before)}</td>
      <td class="insd-after-val${good ? '' : ' worse'}">${insdEsc(afterVal)}</td>
    </tr>`).join('');
}

// P0 #6: approval requirement, derived from GOVERNANCE_TIERS using this
// insight's own real cost-at-stake — not a new classification scheme, and
// not hardcoded copy duplicated from the tier definitions.
function approvalTierFor(costDeltaLakh) {
  const tier = costDeltaLakh < 5 ? GOVERNANCE_TIERS[0]
    : costDeltaLakh <= 50 ? GOVERNANCE_TIERS[1]
    : GOVERNANCE_TIERS[2];
  return tier;
}

function modalHtml(vm) {
  const after = computeAfterValues(vm, insdFlow.shiftPct ?? vm.shiftPct);
  const shiftPct = insdFlow.shiftPct ?? vm.shiftPct;
  const tier = approvalTierFor(vm.costDeltaLakh);

  return `
    <div class="insd-modal-card">
      <div class="insd-modal-head">
        <div class="insd-modal-title-row">${ICON.sparkle}<span class="insd-modal-title">Review proposed action</span></div>
        <button type="button" class="insd-modal-close" id="insd-modal-close">${ICON.close}</button>
      </div>
      <p class="insd-modal-sentence">I recommend shifting <input type="number" class="insd-shift-input" id="insd-modal-shift-pct" min="1" max="60" value="${shiftPct}">% of volume from ${insdEsc(vm.facA)} to ${insdEsc(vm.facB)}.</p>
      <p class="insd-modal-sub">This reduces the projected capacity risk while improving service with a lower monthly network cost.</p>
      <div class="text-xs" style="margin:-4px 0 12px;padding:6px 10px;background:${tier.color}14;border:1px solid ${tier.color}44;border-radius:var(--r-sm);color:${tier.color};font-weight:600">
        Tier ${tier.tier} — ${tier.label}: ${insdEsc(tier.description)}
      </div>

      <div class="insd-modal-split">
        <div>
          <div class="insd-modal-col-title">Before vs Proposed After</div>
          <table class="insd-table" id="insd-modal-table">
            <thead><tr><th>Metric</th><th>Before (Current)</th><th>Proposed After</th></tr></thead>
            <tbody>${modalTableRows(vm, after)}</tbody>
          </table>
        </div>
        <div>
          <div class="insd-modal-col-title">What I'll do if you approve</div>
          <div class="insd-steps-list">
            <div class="insd-step-item">
              <span class="insd-step-icon">${ICON.trendUp}</span>
              <div><div class="insd-step-title">Update the recommended volume allocation</div><div class="insd-step-desc">I'll update the model with the new allocation.</div></div>
            </div>
            <div class="insd-step-item" id="insd-step-notify">
              <span class="insd-step-icon">${ICON.mail}</span>
              <div><div class="insd-step-title">Notify the regional planning analyst</div><div class="insd-step-desc">I'll send an email with the details and impact.</div></div>
            </div>
            <div class="insd-step-item">
              <span class="insd-step-icon">${ICON.calendar}</span>
              <div><div class="insd-step-title">Schedule a follow-up check in 7 days</div><div class="insd-step-desc">I'll recheck utilization and impact in 7 days.</div></div>
            </div>
          </div>
        </div>
      </div>

      <div class="insd-modal-email-section">
        <div class="insd-modal-email-head">${ICON.mail}Message I'll send</div>
        <div class="insd-email-meta-row">
          <span><b>To:</b>${insdEsc(vm.email.to)}</span>
          <span><b>CC:</b>${insdEsc(vm.email.cc)}</span>
          <span><b>Subject:</b>${insdEsc(vm.email.subject)}</span>
        </div>
        <div class="insd-email-body" id="insd-email-body">${insdEsc(emailBody(vm, after))}</div>
        <div class="insd-email-edit-links">
          <button type="button" class="insd-view-message-link" id="insd-edit-recipients">${ICON.edit}Edit recipients</button>
          <button type="button" class="insd-view-message-link" id="insd-edit-message">${ICON.edit}Edit message</button>
        </div>
      </div>

      <div class="insd-modal-footer">
        <span class="insd-modal-footer-note">${ICON.lock}No action will be taken until you approve.</span>
        <button type="button" class="insd-btn-reject" id="insd-reject-btn">Reject</button>
        <button type="button" class="insd-btn-outline" id="insd-edit-action-btn">Edit action</button>
        <button type="button" class="insd-btn-outline-purple" id="insd-approve-only-btn">Approve action only</button>
        <button type="button" class="insd-btn-approve-send" id="insd-approve-send-btn">Approve action &amp; send</button>
      </div>
    </div>`;
}

function openActionReviewModal() {
  const overlay = document.getElementById('action-review-modal-overlay');
  if (!overlay) return;
  insdFlow.shiftPct = insdFlow.vm.shiftPct;
  overlay.innerHTML = modalHtml(insdFlow.vm);
  overlay.classList.add('active');
  bindModal();
}

function closeActionReviewModal() {
  const overlay = document.getElementById('action-review-modal-overlay');
  if (overlay) { overlay.classList.remove('active'); overlay.innerHTML = ''; }
}

function refreshModalTable() {
  const vm = insdFlow.vm;
  const after = computeAfterValues(vm, insdFlow.shiftPct);
  const tbody = document.querySelector('#insd-modal-table tbody');
  if (tbody) tbody.innerHTML = modalTableRows(vm, after);
  const emailBox = document.getElementById('insd-email-body');
  if (emailBox) emailBox.textContent = emailBody(vm, after);
}

function bindModal() {
  const vm = insdFlow.vm;

  document.getElementById('insd-modal-close')?.addEventListener('click', closeActionReviewModal);
  document.getElementById('insd-reject-btn')?.addEventListener('click', closeActionReviewModal);
  document.getElementById('action-review-modal-overlay')?.addEventListener('click', e => {
    if (e.target.id === 'action-review-modal-overlay') closeActionReviewModal();
  });

  document.getElementById('insd-modal-shift-pct')?.addEventListener('change', e => {
    insdFlow.shiftPct = Math.max(1, Math.min(60, Number(e.target.value) || vm.shiftPct));
    refreshModalTable();
  });

  let notifySkipped = false;
  document.getElementById('insd-edit-action-btn')?.addEventListener('click', () => {
    document.getElementById('insd-modal-shift-pct')?.focus();
  });

  document.getElementById('insd-edit-recipients')?.addEventListener('click', () => {
    document.getElementById('insd-step-notify')?.classList.toggle('skipped');
    notifySkipped = document.getElementById('insd-step-notify')?.classList.contains('skipped') || false;
  });

  document.getElementById('insd-edit-message')?.addEventListener('click', () => {
    const box = document.getElementById('insd-email-body');
    box?.classList.toggle('hidden');
  });

  function approve(sendEmail) {
    vm.shiftPct = insdFlow.shiftPct;
    insdFlow.actionState = 'taken';
    // actionTakenCardHtml reads vm.messageSent (vm === insdFlow.vm), not
    // insdFlow.messageSent directly — keep both in sync.
    vm.messageSent = insdFlow.messageSent = sendEmail && !notifySkipped;
    closeActionReviewModal();
    renderDeepDive();
    if (vm.raw) vm.raw.__resolved = true;
    if (typeof window.markAttentionItemResolved === 'function') window.markAttentionItemResolved(vm.id);
  }

  document.getElementById('insd-approve-only-btn')?.addEventListener('click', () => approve(false));
  document.getElementById('insd-approve-send-btn')?.addEventListener('click', () => approve(true));
}

/* ═══════════════════════════════════════════════════════════════
   Entry points / navigation
   ═══════════════════════════════════════════════════════════════ */
export function showInsightDetail(kind, id) {
  const vm = buildViewModel(kind, id);
  if (!vm) return;

  insdFlow.vm = vm;
  insdFlow.shiftPct = vm.shiftPct;
  insdFlow.actionState = 'pending';
  insdFlow.messageSent = false;

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
  closeActionReviewModal();
  if (typeof window.navigateToTab === 'function') window.navigateToTab('home');
}

export function initInsightDetail() {
  if (typeof window !== 'undefined') {
    window.showInsightDetail = showInsightDetail;
  }
}
