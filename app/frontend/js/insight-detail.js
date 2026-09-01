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
 */

import {
  HOME_INSIGHTS, NETWORK_INSIGHTS, NETWORK_RECOMMENDATION,
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
};

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
   Sections
   ═══════════════════════════════════════════════════════════════ */

function findingCardHtml(record) {
  return `
    <div class="insd-card">
      <div class="insd-why-title">${ICON.info}<span>What I found</span></div>
      <p class="insd-why-text">${insdEsc(record.narrative || record.subtitle)}</p>
    </div>`;
}

/**
 * The metrics the finding cites, with their authoritative values.
 *
 * This is what a deep dive is for. Each row names the metric, the value the
 * deterministic layer computed, and which engine computed it — so a reader can
 * check the sentence above against the figures it was written from.
 *
 * When a finding cites nothing, the section says so. It does not fill the space
 * with a chart.
 */
function evidenceCardHtml(record) {
  const rows = (record.evidence || []).map(e => `
    <tr>
      <td class="insd-detail-label">${insdEsc(e.label)}</td>
      <td class="insd-detail-val">${insdEsc(e.display_value)}</td>
      <td class="insd-detail-label">${insdEsc(e.source)}</td>
    </tr>`).join('');

  const body = rows
    ? `<table class="insd-table">
         <thead><tr><th>Metric</th><th>Value</th><th>Computed by</th></tr></thead>
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

/**
 * The engine's recommendation for the network.
 *
 * Network-wide rather than per-insight, and labelled as such, because that is
 * what the engine produces: one recommendation chosen by the whole body of
 * evidence. Presenting a per-finding recommendation would mean inventing one.
 */
function recommendationCardHtml() {
  const rec = NETWORK_RECOMMENDATION;
  if (!rec.text) {
    return `
      <div class="insd-card">
        <div class="insd-rec-head">What I recommend</div>
        <p class="insd-why-text">No recommendation has been generated for this
          network yet.</p>
      </div>`;
  }
  const drivers = (rec.keyDrivers || []).length
    ? `<ul class="insd-details-body">${rec.keyDrivers
        .map(d => `<li>${insdEsc(d)}</li>`).join('')}</ul>`
    : '';
  const limitation = rec.limitation
    ? `<p class="insd-chart-note">Limitation: ${insdEsc(rec.limitation)}</p>`
    : '';
  return `
    <div class="insd-card">
      <div class="insd-rec-head">What I recommend</div>
      <p class="insd-rec-sentence">${insdEsc(rec.text)}</p>
      ${drivers}${limitation}
      <p class="insd-chart-note">This is a network-level recommendation drawn
        from every finding, not from this one alone.</p>
    </div>`;
}

/**
 * The two things a reader can actually do.
 *
 * Both navigate. Neither claims to have changed anything: the previous version
 * offered Approve / Reject buttons that only changed their own label, and an
 * "action taken" state that asserted an action had been taken when none had.
 */
function actionBarHtml(facilityId) {
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

      <div class="insd-main-split">
        <div>
          ${findingCardHtml(record)}
          ${evidenceCardHtml(record)}
        </div>
        ${recommendationCardHtml()}
      </div>

      ${actionBarHtml(insdFlow.facilityId)}
      ${footerNoteHtml(record)}
    </div>`;

  bindDeepDive();
}

function bindDeepDive() {
  document.getElementById('insd-back-btn')?.addEventListener('click', backToHome);

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
  if (typeof window.navigateToTab === 'function') window.navigateToTab('home');
}

export function initInsightDetail() {
  if (typeof window !== 'undefined') {
    window.showInsightDetail = showInsightDetail;
  }
}
