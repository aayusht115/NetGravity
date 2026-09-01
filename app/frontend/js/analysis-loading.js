/**
 * NetGravity — The analysis loading screen
 * ========================================
 * Holds the application until a project's KPIs actually exist.
 *
 * What it replaces
 * ----------------
 * `enterApp()` revealed the dashboard immediately and started hydration
 * afterwards, so opening a project showed a fully drawn screen of dashes and
 * zeroes for as long as the MILP took — twenty to forty seconds on a real
 * network — and then snapped into figures. Nothing told the user that the
 * numbers in front of them were not yet the answer.
 *
 * The ingestion flow did have a loading pop-up, but it was a `setInterval` that
 * advanced a percentage forty times and called `onDone()` regardless of what
 * the backend was doing. Its "% complete" was a count of its own ticks.
 *
 * What this does instead
 * ----------------------
 * It lists the stages hydration genuinely performs, marks each one done as its
 * promise resolves, and shows a percentage that is completed stages over total
 * stages — a real fraction of a real list. The elapsed clock is real seconds.
 * It closes when hydration settles, and only then.
 *
 * It reuses the existing `.ing-loading-*` markup so it looks like the rest of
 * the product; what changed is where the numbers come from.
 */

import { HYDRATION_STAGES } from './integration/hydrate.js';

const OVERLAY_ID = 'loading-modal-overlay';

let state = null;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function stageRowsHtml() {
  return state.stages.map((s) => `
    <div class="ing-loading-row${s.status === 'running' ? ' highlight' : ''}"
         data-stage="${s.id}">
      <div class="ing-loading-row-left">
        <span class="ing-stage-mark" data-mark="${s.status}">${
          s.status === 'done' ? '&#10003;' : s.status === 'running' ? '&#8226;' : '&#9675;'
        }</span>
        <div>
          <div class="ing-loading-row-title${s.status === 'running' ? ' pulse' : ''}">${escapeHtml(s.label)}</div>
          <div class="ing-loading-row-sub" data-detail>${escapeHtml(s.detail || '')}</div>
        </div>
      </div>
      <div class="ing-loading-row-right">
        <div class="ing-loading-status" data-status>${
          s.status === 'done' ? 'Done' : s.status === 'running' ? 'Working' : 'Waiting'
        }</div>
      </div>
    </div>`).join('');
}

function render() {
  const overlay = document.getElementById(OVERLAY_ID);
  if (!overlay || !state) return;
  const done = state.stages.filter((s) => s.status === 'done').length;
  const pct = Math.round((done / state.stages.length) * 100);

  overlay.innerHTML = `
    <div class="ing-loading-card" role="status" aria-live="polite">
      <div class="ing-loading-head">
        <span class="ing-sparkle-icon">&#10022;</span>
        <div class="ing-loading-title">Analysing ${escapeHtml(state.projectName)}</div>
      </div>
      <div class="ing-loading-sub">${escapeHtml(state.subtitle)}</div>

      <div class="ing-loading-row">
        <div class="ing-loading-row-left">
          <div>
            <div class="ing-loading-row-title">Progress</div>
            <div class="ing-loading-row-sub" id="ng-analysis-elapsed">0s elapsed</div>
          </div>
        </div>
        <div class="ing-loading-row-right">
          <span class="ing-progress-pill">
            <span id="ing-loading-pct">${pct}%</span>
            &nbsp;&mdash;&nbsp;${done} of ${state.stages.length} steps complete
          </span>
        </div>
      </div>

      ${stageRowsHtml()}

      <div class="ing-loading-sub" style="margin-top:10px" id="ng-analysis-foot">
        ${escapeHtml(state.footnote)}
      </div>
    </div>`;
  overlay.classList.add('active');
}

function tick() {
  if (!state) return;
  const seconds = Math.round((Date.now() - state.startedAt) / 1000);
  const el = document.getElementById('ng-analysis-elapsed');
  if (el) el.textContent = `${seconds}s elapsed`;
}

/**
 * Put the loading screen up.
 *
 * `alreadyComputed` comes from `/api/kpis/readiness` — the backend saying
 * whether this network version has been solved before. It changes only what
 * the user is TOLD, never how long they wait: a first solve is a genuinely
 * different wait from reading a stored result, and saying so is the difference
 * between a slow screen and a broken one.
 */
export function beginAnalysisLoading(projectName, alreadyComputed = false) {
  state = {
    projectName: projectName || 'this project',
    stages: HYDRATION_STAGES.map(([id, label]) => ({
      id, label, status: 'pending', detail: '',
    })),
    startedAt: Date.now(),
    subtitle: alreadyComputed
      ? 'Reading the analysis already computed for this network.'
      : 'Running the optimisation over your network. Every figure on the '
        + 'dashboard comes from this solve, so it opens when the solve is done.',
    footnote: alreadyComputed
      ? 'This network has been solved before, so this should be quick.'
      : 'This is the first time this version of your data has been analysed. '
        + 'The result is kept, so opening the project again will not repeat it.',
  };
  render();
  state.timer = setInterval(tick, 1000);
}

/**
 * Revise the wording once it is known whether this network was solved before.
 *
 * Exists so the overlay can be raised BEFORE that is known. It used to go up
 * only after `getReadiness()` returned, which left the dashboard visible with
 * no figures behind it for one HTTP round trip — a short window, but the exact
 * thing the loading screen is for, and check L-02 in
 * `validation/phase_10_7/run_greenfield_and_hardcoded_check.py` sampled it.
 *
 * A no-op when no overlay is up, so a caller does not have to check.
 */
export function refineAnalysisLoading(alreadyComputed) {
  if (!state) return;
  state.subtitle = alreadyComputed
    ? 'Reading the analysis already computed for this network.'
    : 'Running the optimisation over your network. Every figure on the '
      + 'dashboard comes from this solve, so it opens when the solve is done.';
  state.footnote = alreadyComputed
    ? 'This network has been solved before, so this should be quick.'
    : 'This is the first time this version of your data has been analysed. '
      + 'The result is kept, so opening the project again will not repeat it.';
  render();
}

/** Mark one real stage started or finished. Called from hydration. */
export function reportAnalysisStage(id, status, detail = '') {
  if (!state) return;
  const stage = state.stages.find((s) => s.id === id);
  if (!stage) return;
  stage.status = status === 'done' ? 'done' : 'running';
  if (detail) stage.detail = detail;
  render();
  tick();
}

/**
 * Take the loading screen down.
 *
 * `failure` is shown for a moment before closing, so a project that could not
 * be analysed says why here rather than revealing an empty dashboard and
 * leaving the banner to explain it.
 */
export function endAnalysisLoading(failure = null) {
  const overlay = document.getElementById(OVERLAY_ID);
  if (state && state.timer) clearInterval(state.timer);

  const close = () => {
    if (overlay) {
      overlay.classList.remove('active');
      overlay.innerHTML = '';
    }
    state = null;
  };

  if (failure && overlay && state) {
    const foot = document.getElementById('ng-analysis-foot');
    if (foot) {
      foot.textContent = failure;
      foot.style.color = 'var(--red, #c0392b)';
      foot.style.fontWeight = '600';
    }
    setTimeout(close, 1800);
    return;
  }
  close();
}

export function isAnalysisLoading() {
  return state !== null;
}

if (typeof window !== 'undefined') {
  window.beginAnalysisLoading = beginAnalysisLoading;
  window.endAnalysisLoading = endAnalysisLoading;
}
