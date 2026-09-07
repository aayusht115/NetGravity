/**
 * NetGravity — Background Work Tray
 * =================================
 * One long request, left running, with the workspace given back.
 *
 * WHY THIS EXISTS. A scenario solve is a MILP over the whole network, and a
 * measured one — demand raised across every region for a single product
 * category — took over 700 seconds. The loading dialog held the entire
 * workspace behind a blurred scrim for all of it: the Digital Twin, the
 * assistant and every other screen out of reach, with no exit and nothing
 * saying how long the wait would be. That is not a slow screen; it is a
 * screen a reader concludes has hung.
 *
 * WHAT IT IS NOT. Not a scheduler, not a queue, not a second orchestration
 * layer and not a store of anything the business depends on. The work is
 * already running on the server and already survives this page — aborting a
 * fetch does not abort an optimiser, which is why `findScenarioCreatedSince`
 * exists. This module holds a TITLE, a START TIME and a STATUS for a promise
 * the caller is still awaiting, draws them, and gets out of the way. It reads
 * no KPI, computes nothing, and stores no business value.
 *
 * Nielsen, explicitly:
 *   #1 visibility of system status — the pill stays up, counting, for the
 *      whole run, on every screen the reader moves to.
 *   #3 user control and freedom — the run is left, not cancelled; the dialog
 *      it was left from said which of those it was doing.
 *   #4 consistency — the pill uses the same surface, radius and type scale as
 *      the rest of the workspace chrome.
 *  #10 recognition over recall — the completion notice names the scenario and
 *      offers the one action that follows from it, so the reader does not
 *      have to remember what they started or where to find it.
 */

const TRAY_ID = 'ng-bg-tray';

/**
 * Live tasks, keyed by id.
 *
 * Deliberately module-local and deliberately not persisted. A task is a
 * promise this page is awaiting; a page that reloads is no longer awaiting
 * anything, and a pill restored from storage for a promise nobody holds would
 * count up forever against work whose result can never arrive here. What
 * survives a reload is the SCENARIO, on the server, listed on the next visit
 * to Scenario Planning — which is what the dialog says when it is left.
 */
const tasks = new Map();

let clock = null;

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/** Elapsed, in the units a reader of a four-minute wait actually reads. */
function elapsedText(startedAt) {
  const total = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
  if (total < 60) return `${total}s`;
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${mins}m ${String(secs).padStart(2, '0')}s`;
}

function tray() {
  let el = document.getElementById(TRAY_ID);
  if (!el) {
    el = document.createElement('div');
    el.id = TRAY_ID;
    el.className = 'ng-bg-tray';
    // Announced, not shouted: a reader who has moved to another screen should
    // be told the work landed without having the screen taken from them.
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    document.body.appendChild(el);
  }
  return el;
}

function render() {
  const el = tray();
  const rows = [...tasks.values()];
  if (!rows.length) {
    el.innerHTML = '';
    el.classList.remove('visible');
    return;
  }
  el.classList.add('visible');
  el.innerHTML = rows.map((task) => {
    if (task.status === 'running') {
      return `
        <div class="ng-bg-card running" data-task="${escapeHtml(task.id)}">
          <span class="ng-bg-spinner" aria-hidden="true"></span>
          <div class="ng-bg-text">
            <div class="ng-bg-title">${escapeHtml(task.title)}</div>
            <div class="ng-bg-sub">Still solving on the server ·
              <span class="ng-bg-elapsed">${elapsedText(task.startedAt)}</span>
              — keep working, you will be told when it lands</div>
          </div>
        </div>`;
    }
    if (task.status === 'failed') {
      return `
        <div class="ng-bg-card failed" data-task="${escapeHtml(task.id)}">
          <span class="ng-bg-mark" aria-hidden="true">!</span>
          <div class="ng-bg-text">
            <div class="ng-bg-title">${escapeHtml(task.title)}</div>
            <div class="ng-bg-sub">${escapeHtml(task.message || 'did not complete')}</div>
          </div>
          <button type="button" class="ng-bg-dismiss" data-dismiss="${escapeHtml(task.id)}"
                  aria-label="Dismiss">×</button>
        </div>`;
    }
    return `
      <div class="ng-bg-card done" data-task="${escapeHtml(task.id)}">
        <span class="ng-bg-mark ok" aria-hidden="true">✓</span>
        <div class="ng-bg-text">
          <div class="ng-bg-title">${escapeHtml(task.title)}</div>
          <div class="ng-bg-sub">${escapeHtml(task.message || 'Finished and ready to read.')}</div>
        </div>
        ${task.openLabel ? `
          <button type="button" class="ng-bg-open" data-open="${escapeHtml(task.id)}">
            ${escapeHtml(task.openLabel)}
          </button>` : ''}
        <button type="button" class="ng-bg-dismiss" data-dismiss="${escapeHtml(task.id)}"
                aria-label="Dismiss">×</button>
      </div>`;
  }).join('');

  el.querySelectorAll('[data-open]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const task = tasks.get(btn.dataset.open);
      dismiss(btn.dataset.open);
      if (task && typeof task.onOpen === 'function') task.onOpen();
    });
  });
  el.querySelectorAll('[data-dismiss]').forEach((btn) => {
    btn.addEventListener('click', () => dismiss(btn.dataset.dismiss));
  });
}

/* The elapsed figure is redrawn in place rather than by re-rendering the tray:
   replacing the markup once a second would take the focus off the button a
   reader is about to press. */
function tickElapsed() {
  const el = document.getElementById(TRAY_ID);
  if (!el) return;
  [...tasks.values()].forEach((task) => {
    if (task.status !== 'running') return;
    const node = el.querySelector(`[data-task="${CSS.escape(task.id)}"] .ng-bg-elapsed`);
    if (node) node.textContent = elapsedText(task.startedAt);
  });
}

function ensureClock() {
  const anyRunning = [...tasks.values()].some((t) => t.status === 'running');
  if (anyRunning && !clock) clock = setInterval(tickElapsed, 1000);
  if (!anyRunning && clock) { clearInterval(clock); clock = null; }
}

/**
 * Take a run over: it is no longer on screen, and it is still happening.
 *
 * `id` is the caller's own handle for the promise it is still awaiting. This
 * neither starts nor owns that promise.
 *
 * `title` names the WORK, not its state — measured on a live run, a title of
 * `Solving "Demand +50%"` was still saying "Solving" under a green tick when
 * the scenario had landed. The state is the row's own second line, which
 * changes; the title does not.
 */
export function startBackgroundTask({ id, title }) {
  if (!id) return;
  tasks.set(id, { id, title: title || 'Working', status: 'running',
                  startedAt: Date.now() });
  render();
  ensureClock();
}

/** It landed. The notice names it and offers the one action that follows. */
export function finishBackgroundTask(id, { message = '', openLabel = '', onOpen = null } = {}) {
  const task = tasks.get(id);
  if (!task) return;
  tasks.set(id, { ...task, status: 'done', message, openLabel, onOpen });
  render();
  ensureClock();
}

/**
 * It did not land.
 *
 * Left on screen until dismissed. A failure that clears itself after a few
 * seconds is a failure the reader who stepped away never learns about, and
 * this is precisely the flow where they stepped away.
 */
export function failBackgroundTask(id, message) {
  const task = tasks.get(id);
  if (!task) return;
  tasks.set(id, { ...task, status: 'failed', message: message || 'did not complete' });
  render();
  ensureClock();
}

/** Whether this page is still waiting on a given run. */
export function isBackgroundTask(id) {
  const task = tasks.get(id);
  return Boolean(task && task.status === 'running');
}

export function dismiss(id) {
  tasks.delete(id);
  render();
  ensureClock();
}

if (typeof window !== 'undefined') {
  window.startBackgroundTask = startBackgroundTask;
  window.finishBackgroundTask = finishBackgroundTask;
  window.failBackgroundTask = failBackgroundTask;
}
