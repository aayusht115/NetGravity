/**
 * NetGravity — Explain this chart
 * ===============================
 * A short briefing about ONE visual, on request.
 *
 * WHICH CHARTS GET A BUTTON
 * -------------------------
 * Four, and deliberately not fourteen. A button on every card teaches a
 * reader that the button means nothing, so it goes only where a visual takes
 * more than a glance to read:
 *
 *   * peak against average   the finding is the PAIR of bars;
 *   * capacity carried       the finding is that ranking by units and ranking
 *                            by per cent disagree;
 *   * stock held             the finding is the GAP between the two bars;
 *   * throughput horizon     the finding is the trend against the ceiling.
 *
 * The ranked lists and the share-of-total donuts have no button: a sorted
 * list with its values printed on it already says what it says, and an
 * explanation of it would restate the labels.
 *
 * NOTHING IS GENERATED UNTIL SOMEONE ASKS
 * ---------------------------------------
 * No request is made on render, on navigation, or on a filter change. The
 * backend keeps one record per chart per analysis so re-opening costs nothing
 * there; this module keeps its own copy so re-opening costs nothing HERE
 * either — not even a round trip.
 *
 * A FILTER CHANGE CLOSES IT
 * -------------------------
 * An explanation is about the rows that were on screen when it was asked for.
 * Leaving one open while the filter beneath it narrows to three sites would
 * put a confident paragraph about nine sites over a chart of three, which is
 * the exact failure the whole screen was rebuilt to prevent. It closes on
 * every view change; the cache means re-opening the previous view is instant.
 *
 * AN OVERLAY, NOT A PANEL
 * -----------------------
 * It is moved into the chart card and drawn OVER it. An overlay is out of
 * flow, so the card's height and its neighbour's position are identical
 * whether or not anything has been explained — where the expanding panel this
 * replaced made a chart move in order to explain itself.
 *
 * THE NUMBERS ARE CHECKED, NOT ABSENT
 * -----------------------------------
 * A briefing read beside a chart says less than the picture if it carries no
 * quantities, so here the model may cite figures — but only ones registered
 * as citable facts in `numeric_grounding._FACT_SPEC`, all of them produced by
 * `reasoning/kpi_chart_evidence.py` from the backend's own solved rows.
 * Anything else it writes is removed before it arrives.
 */

import { kpiService } from './integration/services/kpi-service.js';
import { warehouseDrawnIds } from './warehouse.js';
import { currentKpiView } from './kpi-view.js';

/** chart id -> the canvas it explains, and what identifies its subject. */
const EXPLAINABLE = [
  { chart: 'peak_vs_average', canvas: 'chart-wh-utilisation', scope: 'network',
    title: 'Peak vs Average Utilisation' },
  { chart: 'capacity_carried', canvas: 'chart-wh-headroom', scope: 'network',
    title: 'Capacity and What It Carries' },
  { chart: 'stock_held', canvas: 'chart-wh-stock', scope: 'network',
    title: 'Stock Held' },
  { chart: 'throughput_horizon', canvas: 'chart-dash-throughput', scope: 'facility',
    title: 'Throughput vs Capacity Horizon' },
];

/** Which chart the one open card is currently about, so pressing the same
 *  button again closes it rather than re-opening what is already there. */
let openChart = null;

/** Answers already received, keyed by chart + the exact subject asked about.
 *  Cleared when the project changes; a solve of the same project produces a
 *  new fingerprint server-side, which is caught on the next request. */
const answers = new Map();

let wired = false;

function el(id) { return document.getElementById(id); }

function buttonOf(entry) {
  return el(`kpi-explain-btn-${entry.chart}`);
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/**
 * WHAT this explanation would be about, right now.
 *
 * The sites THIS CHART DREW — not every site the filters left on screen.
 * Each chart applies its own selection on top of the filters: the utilisation
 * and headroom charts take open sites only, stock takes the sites that report
 * a level. Briefing over the visible rows instead described a proposed site
 * the headroom chart had deliberately left out, which is a confident
 * paragraph about a bar that is not there.
 *
 * Returns null when the chart has no subject, so nothing is requested for a
 * chart with nothing on it.
 */
function subjectOf(entry) {
  const ids = warehouseDrawnIds(entry.chart);
  if (entry.scope === 'facility') {
    // THE SAME TEST AS EVERY OTHER CHART, and it used to be a different one:
    // a selected facility was taken to mean a drawn chart. It does not. The
    // throughput chart draws nothing when the solve produced no per-period
    // series — a single-period solve, by design — and the button beside that
    // empty card still returned a confident paragraph about a busiest period,
    // written from the peak and average on the cards above. Correct figures,
    // about a picture that is not there.
    const view = currentKpiView();
    return (view.entityId && ids.includes(view.entityId))
      ? { facilityId: view.entityId } : null;
  }
  return ids.length ? { facilityIds: ids } : null;
}

function cacheKey(entry, subject) {
  return `${entry.chart}::${subject.facilityId || (subject.facilityIds || []).join(',')}`;
}

// ─── Rendering one card ─────────────────────────────────────

function cardMarkup(card) {
  // A HEADLINE AND THE PROSE, and nothing else.
  //
  // The evidence list, the "why it matters" paragraph and the "how was this
  // calculated?" note are all gone. The chart below already presents the
  // calculated result; this is here to say what the pattern IS, and a card
  // that also teaches the formula and lists its inputs stops being a
  // sentence a reader can take in at a glance.
  //
  // The figures now live inside the sentences — checked against the
  // deterministic results before they arrive (see numeric_grounding), so
  // dropping the separate list loses no verification, only clutter.
  return `
    ${card.headline ? `<div class="kpi-ai-headline">${esc(card.headline)}</div>` : ''}
    ${card.meaning ? `<p class="kpi-ai-text">${esc(card.meaning)}</p>` : ''}`;
}

/** Working, nothing to say, or a failure — never an empty overlay, which
 *  reads as a briefing that said nothing. */
function noteMarkup(text) {
  return `<div class="kpi-ai-text">${esc(text)}</div>`;
}

// ─── The one overlay ────────────────────────────────────────

/**
 * Move the overlay into the chart card it describes.
 *
 * The card is given `position: relative` so the overlay can sit over its
 * plot area. Because an overlay is out of flow, the chart card's height is
 * the same whether or not it has been explained — which is the whole reason
 * this is not an expanding panel.
 */
function placeOverlay(entry) {
  const overlay = el('kpi-explain-card');
  const canvas = document.getElementById(entry.canvas);
  const chartCard = canvas?.closest('.card');
  if (!overlay || !chartCard) return false;
  chartCard.classList.add('has-ai-overlay');
  if (overlay.parentElement !== chartCard) chartCard.appendChild(overlay);
  return true;
}

/** Every button back to rest, and the overlay hidden. */
function resetButtons(except = null) {
  EXPLAINABLE.forEach((entry) => {
    const button = buttonOf(entry);
    if (!button || entry.chart === except) return;
    button.classList.remove('is-open');
    button.setAttribute('aria-expanded', 'false');
    // ONE LABEL, ALWAYS. It read "✓ Explained" after a response landed,
    // which is a state the overlay beside it already shows — and it left the
    // button saying something other than what pressing it does.
    button.textContent = '\u2728 AI Explain';
  });
}

function setOverlayOpen(entry, open) {
  const overlay = el('kpi-explain-card');
  if (overlay) overlay.hidden = !open;
  openChart = open ? entry.chart : null;
  resetButtons(open ? entry.chart : null);
  const button = buttonOf(entry);
  if (button && open) {
    button.classList.add('is-open');
    button.setAttribute('aria-expanded', 'true');
  }
}

async function onExplain(entry) {
  const overlay = el('kpi-explain-card');
  const body = el('kpi-explain-body');
  const button = buttonOf(entry);
  if (!overlay || !body || !button) return;

  // The shimmer is a one-off invitation to try the feature. Once it has been
  // tried it has done its job, and a control that keeps advertising itself
  // after use is noise.
  stopShimmer();

  // NO TOGGLE. The overlay closes on its own × and nothing else: a card that
  // also vanishes when the button behind it is pressed again gives the reader
  // two ways out, one of which is invisible from where they are looking.
  // Pressing another chart's button moves the overlay there instead.

  if (!placeOverlay(entry)) return;

  const subject = subjectOf(entry);
  if (!subject) {
    body.innerHTML = noteMarkup('There is nothing on this chart to explain.');
    setOverlayOpen(entry, true);
    return;
  }

  const key = cacheKey(entry, subject);
  if (answers.has(key)) {
    body.innerHTML = answers.get(key);
    setOverlayOpen(entry, true);
    return;
  }

  body.innerHTML = noteMarkup('Reading the analysis\u2026');
  setOverlayOpen(entry, true);
  button.disabled = true;
  button.textContent = '\u2728 Analysing\u2026';

  try {
    const response = await kpiService.explainChart(entry.chart, subject);
    const result = response?.card || {};
    const markup = (result.headline || result.meaning)
      ? cardMarkup(result)
      : noteMarkup(response?.reason
          || 'No explanation is available for this chart yet.');
    // Only a real briefing is kept. Caching "nothing to explain" would keep
    // the answer to a question the next solve may well answer differently.
    if (result.headline || result.meaning) answers.set(key, markup);
    body.innerHTML = markup;
  } catch (err) {
    // The chart is perfectly readable without this. Say what happened and
    // leave the reader the button.
    body.innerHTML = noteMarkup(
      (err && err.message) || 'The explanation could not be read.');
  } finally {
    button.disabled = false;
    button.textContent = '\u2728 AI Explain';
  }
}

// ─── Discoverability ────────────────────────────────────────

/**
 * A single reflection across the buttons when the screen first appears.
 *
 * The feature is invisible until someone notices a small button in a chart
 * header, and a screen of eleven cards gives them little reason to look. One
 * pass says "this is here"; a control that pulses forever says "look at me"
 * over and over, which is the thing that makes a dashboard tiring.
 *
 * Stops for good on the first click — it has been found — and never starts at
 * all for a reader who has asked for reduced motion.
 */
let shimmerDone = false;
let shimmerTimer = null;

/** How often the invitation is repeated.
 *
 *  TWO SECONDS IS DELIBERATE and was asked for directly: at fourteen the
 *  sweep was missed by anyone who happened to be reading the scorecard, and
 *  the feature stayed undiscovered. It is not a pulse, because it is not
 *  forever — `stopShimmer()` ends it permanently on the first click and it
 *  never runs at all under `prefers-reduced-motion`, so it advertises the
 *  button only to a reader who has not yet used it.
 *
 *  The comment here used to say "fourteen seconds" and did not match the
 *  constant beside it; a later reader took the constant for leftover test
 *  code and nearly reverted it. */
const SHIMMER_INTERVAL_MS = 2000;

function prefersReducedMotion() {
  return typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function sweepOnce() {
  EXPLAINABLE.map(buttonOf).filter(Boolean).forEach((b) => {
    b.classList.remove('is-shimmering');
    // Reflow, so re-adding the class restarts the animation rather than
    // being coalesced into no change at all.
    void b.offsetWidth;
    b.classList.add('is-shimmering');
  });
}

export function startKpiExplainShimmer() {
  if (shimmerDone || prefersReducedMotion()) return;
  if (!EXPLAINABLE.map(buttonOf).some(Boolean)) return;
  sweepOnce();
  // REPEATED, not looped. A single sweep on arrival is missed by anyone who
  // was reading the scorecard at the time, and a control that animates
  // continuously is the thing that makes a dashboard tiring. So it glints
  // again every fourteen seconds until the feature is used.
  if (shimmerTimer) clearInterval(shimmerTimer);
  shimmerTimer = setInterval(() => {
    if (shimmerDone || document.hidden) return;
    sweepOnce();
  }, SHIMMER_INTERVAL_MS);
}

function stopShimmer() {
  shimmerDone = true;
  if (shimmerTimer) { clearInterval(shimmerTimer); shimmerTimer = null; }
  EXPLAINABLE.forEach((entry) => {
    buttonOf(entry)?.classList.remove('is-shimmering');
  });
}

/** A new project or a new solve is a new thing to explain, so the invitation
 *  is offered once more. */
export function resetKpiExplainShimmer() {
  shimmerDone = false;
}

/**
 * Put a button in each explainable card's header and a panel under its chart.
 *
 * Mounted from here rather than written into the markup four times: the
 * pairing of a button with the panel it opens is stated once, so the two
 * cannot drift apart, and a chart that stops being explainable is removed by
 * deleting one line from `EXPLAINABLE`.
 */
export function initKpiExplain() {
  // The close button belongs to the card, not to any chart, so it is wired
  // once and independently of whether the charts have been drawn yet.
  const closer = el('kpi-explain-close');
  if (closer && !closer.dataset.wired) {
    closer.dataset.wired = '1';
    closer.addEventListener('click', () => {
      const overlay = el('kpi-explain-card');
      if (overlay) overlay.hidden = true;
      openChart = null;
      resetButtons();
    });
  }

  EXPLAINABLE.forEach((entry) => {
    const canvas = document.getElementById(entry.canvas);
    const chartCard = canvas?.closest('.card');
    if (!chartCard || chartCard.querySelector('.kpi-explain-btn')) return;

    // In the header, beside the status tag — never inside the card body. The
    // card's height must not depend on whether it has been explained.
    const header = chartCard.querySelector('.card-header');
    if (!header) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'kpi-explain-btn';
    button.id = `kpi-explain-btn-${entry.chart}`;
    button.setAttribute('aria-expanded', 'false');
    button.textContent = '\u2728 AI Explain';
    button.addEventListener('click', () => onExplain(entry));
    header.appendChild(button);
  });

  wired = EXPLAINABLE.some((entry) => buttonOf(entry));
}

/**
 * Close the card, without discarding what was already fetched.
 *
 * Called on every view change. An explanation describes the rows that were on
 * screen when it was asked for, so leaving it open while the filter beneath it
 * narrows would put a paragraph about nine sites over a chart of three. The
 * cache survives, so returning to that view reopens instantly.
 */
export function closeKpiExplainPanels() {
  const overlay = el('kpi-explain-card');
  if (overlay) overlay.hidden = true;
  openChart = null;
  resetButtons();
}

/** Drop everything — called when the open project changes, because a saved
 *  briefing is about one project's network and no other. */
export function clearKpiExplainCache() {
  answers.clear();
  // A new network is a new thing to explain, so the one-off invitation is
  // offered again.
  resetKpiExplainShimmer();
  closeKpiExplainPanels();
}
