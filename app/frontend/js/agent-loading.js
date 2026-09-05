/**
 * NetGravity — the agent loading screen
 * =====================================
 * The approved design (`Dump/avengersloading.html`): a small rectangular
 * dialog holding a vertical phase tree. Each phase is a node on a line — a
 * spinner while it runs, a green tick when it lands — with its title typed
 * out and its detail streaming underneath. Finished phases collapse away, so
 * what is on screen is what is happening now.
 *
 * This file draws. It decides nothing.
 *
 * The reference is a demo, and the difference matters. Its six phases are a
 * fixed list advanced by `setTimeout` every 1250ms, with invented logs —
 * "Recruiting Iron Man", "Simulating 14,000,605 outcomes". Nothing of that
 * kind is here. The phases ARE the dispatches the caller declared in
 * `startRun({ plan })`, they change state only when a request genuinely starts
 * and returns, and every log line is either the message the caller stated or a
 * fact read back from the orchestrator's own execution trace. A scenario run
 * shows two phases because it makes two requests; opening a project shows
 * five because hydration awaits five.
 *
 * The structure is built once and updated in place. Replacing an element
 * restarts every CSS animation inside it, and this screen is almost entirely
 * animation: rebuilding on each state change left the dialog stuck in the
 * opening frames of its own fade and re-typed every title from scratch.
 */

import {
  subscribe, getRun, progress, clearRun,
} from './agent-activity.js';

const OVERLAY_ID = 'agent-loading-overlay';

/* ─── pacing ─────────────────────────────────────────────────────────────
   The screen is deliberately behind the work.

   Events arrive at machine speed. A network that has been solved before
   reports five dispatches and twenty lines of detail inside two seconds, and
   a screen that draws each one the instant it arrives gives every line a
   twentieth of a second — which is the same as giving it none. Nielsen's
   first heuristic asks for visibility of system status, and a line nobody
   could read was never visible; the tenth asks that the system explain
   itself, and an explanation that flashes past explains nothing.

   So reveals are QUEUED, and released at reading speed. What is queued is
   only ever something that has already genuinely happened, released in the
   order it happened. The queue chooses WHEN a fact reaches the screen. It
   never chooses WHAT — that is `update`, reading the recorder, and nothing
   else in this file creates an item.

   Three consequences are handled openly rather than hidden:

     - The screen can fall behind. It speeds up as the backlog grows, to a
       floor, so it is never describing a past the system has left.
     - The dialog can outlast the work: `dismissAgentLoading` waits for the
       queue to drain, so the last thing that happened is read rather than
       flashed. That wait is bounded by `MAX_DRAIN_MS`.
     - Holding a reader has a cost, so there is a way out of it. "Skip to
       latest" and Escape release everything at once — Nielsen's third
       heuristic, and the reason pacing a person's wait is defensible at all.

   The elapsed clock is never paced: it counts real seconds from the real
   start, so the one number on the dialog that claims to be a duration always
   is one. Each phase states its own true duration for the same reason — a
   step held open for eight tenths of a second says, in its own words, that
   it took four hundredths. */

/** One revealed item holds the floor for this long when nothing is waiting. */
const BASE_STEP_MS = 900;
/** …and never for less than this, however far behind the screen falls. */
const FLOOR_STEP_MS = 380;
/** The screen aims to be no further behind than this, and speeds up to hold it. */
const BACKLOG_BUDGET_MS = 9000;
/** A completed phase stays open this long after its tick, then collapses. */
const PHASE_HOLD_MS = 1400;
/** The closing verdict gets longer than a log line: it is the answer. */
const FINALE_HOLD_MS = 1200;
/** The queue is never drained for longer than this once dismissal is asked. */
const MAX_DRAIN_MS = 20000;
/** How long one character of a typed title takes… */
const TYPE_MS = 30;
/** …but a whole title never takes longer than this.

    A title still typing itself while the lines underneath it are already
    arriving reads as two things happening at once when only one is. The
    budget keeps the heading ahead of its own detail on a long label without
    making a short one flick past. */
const TYPE_BUDGET_MS = 520;

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function prefersReducedMotion() {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (e) {
    return false;
  }
}

/* ─── what a phase says ──────────────────────────────────────────────── */

/**
 * The log lines for one phase, in the order they became true.
 *
 * Four sources, all of them real:
 *
 *   - `step.message`, the caller's own statement of what is being passed;
 *   - `step.detail`, what came back;
 *   - the layer's lines, which `absorbTrace` and `absorbLive` write from the
 *     orchestrator's execution record — capability names, retry counts,
 *     failures, the resolved intent;
 *   - the step's own measured duration, from the timestamps the recorder took
 *     when the request left and when it returned.
 *
 * Nothing is generated here. A phase with nothing to say shows nothing.
 */
function logsFor(run, step, index) {
  const out = [];
  // The orchestrator resolves the intent while serving the FIRST request, so
  // that is the phase it belongs to. No step carries the intent layer — the
  // tree is a list of dispatches, and interpreting the request is not one —
  // and without this the one thing the trace says about what the orchestrator
  // understood would never reach the screen.
  if (index === 0) {
    const intent = run.agents.intent;
    if (intent && intent.lines) {
      intent.lines.filter(Boolean).forEach((line) => out.push(splitLine(line)));
    }
  }
  if (step.message) out.push({ text: step.message });
  // The layer's lines, but only for the step that owns the layer right now.
  //
  // The recorder keeps one set of lines per layer, so two steps on the same
  // layer would both read whatever that layer last said and each of them
  // would print the other's detail. Ownership is the last dispatch to go out
  // on the layer, which is the one whose answer those lines describe.
  const agent = run.agents[step.layer];
  if (agent && agent.lines && ownsLayer(run, step)) {
    agent.lines.filter(Boolean).forEach((line) => out.push(splitLine(line)));
  }
  if (step.detail && step.status !== 'failed') out.push({ text: step.detail });
  if (step.status === 'failed') out.push({ text: step.detail || 'failed', tone: 'error' });
  // The measured wait, stated by the phase that waited it.
  //
  // The screen holds a phase open for long enough to be read, which for a
  // fast dispatch is longer than the dispatch took. That is a choice about
  // legibility, and the honest way to make it is to say so: the spinner is
  // paced, this figure is not.
  if (step.startedAt && step.endedAt && step.status !== 'active') {
    out.push({ text: 'Took', value: `${((step.endedAt - step.startedAt) / 1000).toFixed(2)}s` });
  }
  // Deduped: `stepDone` writes the same detail to the step and the layer.
  const seen = new Set();
  return out.filter((l) => {
    const key = `${l.text}|${l.value || ''}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/** Is this the step the layer's current lines belong to? */
function ownsLayer(run, step) {
  let owner = null;
  run.steps.forEach((s) => {
    if (s.layer !== step.layer || !s.startedAt) return;
    if (!owner || s.startedAt >= owner.startedAt) owner = s;
  });
  return !owner || owner.id === step.id;
}

/**
 * "Retried: optimization.solve ×2" reads better as a label and a value, which
 * is how the design sets a log line out. Presentation only — the string is
 * whatever the trace produced.
 */
function splitLine(line) {
  const at = String(line).indexOf(': ');
  if (at > 0 && at < 34) {
    return { text: line.slice(0, at), value: line.slice(at + 2) };
  }
  return { text: line };
}

/**
 * The phase's own state, from the step's.
 *
 * `blocked` is the one that is not a straight read of `step.status`. A step
 * that is still pending when the RUN has ended never ran, and it never will:
 * something upstream stopped it. Drawing that as "pending" would say it is
 * about to happen — the opposite of what is true — and drawing it as done
 * would be worse. The recorder marks the layer blocked for the same reason;
 * this is the same fact, on the phase.
 */
function phaseState(step, runEnded) {
  if (step.status === 'active') return 'active';
  if (step.status === 'done') return 'done';
  if (step.status === 'failed') return 'failed';
  return runEnded ? 'blocked' : 'pending';
}

/* ─── the skeleton ───────────────────────────────────────────────────── */

function skeletonHtml() {
  return `
    <div class="agl-shell" role="status" aria-live="polite">
      <div class="agl-head">
        <span class="agl-orb" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
               stroke-linecap="round" stroke-linejoin="round">
            <circle cx="6" cy="5.5" r="2.2"/><circle cx="18" cy="5.5" r="2.2"/>
            <circle cx="6" cy="18.5" r="2.2"/><circle cx="18" cy="18.5" r="2.2"/>
            <path d="M6 7.7v8.6M18 7.7v8.6M8.2 5.5c3 3.2 6.6 9.8 9.8 13"/>
          </svg>
        </span>
        <div class="agl-head-text">
          <h1 class="agl-title"></h1>
          <p class="agl-sub">Elapsed <span class="agl-elapsed" id="agl-elapsed">0.0s</span>
            <span class="agl-sub-note"></span></p>
        </div>
        <button type="button" class="agl-skip" hidden
                title="Show everything that has already happened, without waiting for it to be read out. This does not change the work.">
          Skip to latest
        </button>
      </div>

      <div class="agl-bar"><span></span></div>

      <div class="agl-tree"></div>

      <div class="agl-finale" hidden>
        <span class="agl-finale-mark" aria-hidden="true"></span>
        <div class="agl-finale-text">
          <div class="agl-finale-title"></div>
          <div class="agl-finale-sub"></div>
        </div>
      </div>
    </div>`;
}

function phaseHtml(step) {
  return `
    <div class="agl-phase pending" data-step="${escapeHtml(step.id)}">
      <span class="agl-node" aria-hidden="true">
        <span class="agl-node-dot"></span>
        <span class="agl-node-spin"></span>
        <span class="agl-node-tick">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2"
               stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4 4L19 7"/></svg>
        </span>
        <span class="agl-node-cross">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2"
               stroke-linecap="round"><path d="M7 7l10 10M17 7L7 17"/></svg>
        </span>
      </span>
      <div class="agl-phase-title"></div>
      <div class="agl-logs"></div>
    </div>`;
}

/* ─── mounted references ─────────────────────────────────────────────── */

let els = null;
let phases = null;          // step id -> { root, title, logs, label }
let lastPlanKey = '';
let lastState = {};         // what is DRAWN
let queuedState = {};       // what has been queued to be drawn
let typedFor = {};
let loggedFor = {};
let blockedNoted = {};
let typeTimer = null;

let queue = [];
let pumpTimer = null;
let collapseTimers = {};
let revealFloorUntil = 0;
let drainBy = 0;
let queuedFinale = false;
/** The reader asked not to be paced. Everything is released at once. */
let instant = false;

function buildSkeleton(overlay) {
  overlay.innerHTML = skeletonHtml();
  const q = (sel) => overlay.querySelector(sel);
  els = {
    shell: q('.agl-shell'),
    title: q('.agl-title'),
    elapsed: q('.agl-elapsed'),
    note: q('.agl-sub-note'),
    skip: q('.agl-skip'),
    bar: q('.agl-bar'),
    barFill: q('.agl-bar > span'),
    tree: q('.agl-tree'),
    finale: q('.agl-finale'),
    finaleTitle: q('.agl-finale-title'),
    finaleSub: q('.agl-finale-sub'),
  };
  els.skip.addEventListener('click', flush);
  phases = null;
  lastPlanKey = '';
}

/** The tree is rebuilt only when the RUN changes, never on a state change. */
function buildTree(run) {
  els.tree.innerHTML = run.steps.map(phaseHtml).join('');
  phases = {};
  run.steps.forEach((step) => {
    const root = els.tree.querySelector(`[data-step="${CSS.escape(step.id)}"]`);
    phases[step.id] = {
      root,
      title: root.querySelector('.agl-phase-title'),
      logs: root.querySelector('.agl-logs'),
      label: step.label || step.id,
    };
  });
  resetPacing();
  lastState = {};
  // Every phase is already drawn `pending`, so that state is never queued:
  // the queue exists to show things happening, and nothing has yet.
  queuedState = {};
  run.steps.forEach((step) => { queuedState[step.id] = 'pending'; });
  typedFor = {};
  loggedFor = {};
  blockedNoted = {};
}

function resetPacing() {
  if (pumpTimer) { clearTimeout(pumpTimer); pumpTimer = null; }
  Object.keys(collapseTimers).forEach((id) => clearTimeout(collapseTimers[id]));
  collapseTimers = {};
  queue = [];
  revealFloorUntil = 0;
  drainBy = 0;
  queuedFinale = false;
  instant = false;
}

/* ─── typing ─────────────────────────────────────────────────────────── */

/**
 * Type a title out, once, when its phase begins.
 *
 * Guarded on the phase rather than run on every update: this is called from a
 * reveal, and reveals happen throughout a run, so re-typing from the first
 * character each time is exactly the class of bug that made the previous
 * screen unreadable.
 */
function typeTitle(el, text) {
  if (prefersReducedMotion() || instant) {
    el.textContent = text;
    return;
  }
  el.textContent = '';
  const caret = document.createElement('span');
  caret.className = 'agl-caret';
  caret.textContent = '▍';
  const body = document.createTextNode('');
  el.appendChild(body);
  el.appendChild(caret);
  const per = Math.min(TYPE_MS, TYPE_BUDGET_MS / Math.max(1, text.length));
  let i = 0;
  const step = () => {
    if (i > text.length) return;
    body.textContent = text.slice(0, i);
    i += 1;
    typeTimer = setTimeout(step, per);
  };
  step();
}

/* ─── the queue ──────────────────────────────────────────────────────── */

/**
 * How long the item just revealed holds the floor.
 *
 * A lone item gets the full reading pace. As the backlog grows the pace
 * shortens, so that whatever is waiting is on screen within roughly
 * `BACKLOG_BUDGET_MS` — a screen thirty seconds behind the system would be
 * describing a past, which is a different lie from the one this avoids.
 * `FLOOR_STEP_MS` is where it stops: below that a line is a flicker again.
 */
function stepMs() {
  if (instant) return 0;
  // A failure still on the queue is the thing the reader needs to see, and it
  // is not made easier to read by three lines of preamble arriving first. The
  // queue keeps its order and loses nothing; it just stops waiting until the
  // failure is on screen.
  if (queue.some((item) => item.urgent)) return 0;
  const waiting = queue.length || 1;
  // What is LEFT of the budget over what is left to show. Dividing the whole
  // budget by the current backlog looks equivalent and is not: the backlog
  // shrinks as it drains, so each remaining item would be given more time
  // than the one before it and the total would run to several times the
  // budget. Measured that way, 777ms of work held the dialog for 21 seconds.
  const left = Math.max(0, drainBy - Date.now());
  return Math.max(FLOOR_STEP_MS, Math.min(BASE_STEP_MS, left / waiting));
}

function pump() {
  pumpTimer = null;
  if (!queue.length) { updateSkip(); return; }
  reveal(queue.shift());
  if (queue.length) {
    pumpTimer = setTimeout(pump, instant ? 0 : Math.max(0, revealFloorUntil - Date.now()));
  }
  updateSkip();
}

function startPump() {
  updateSkip();
  if (!queue.length) return;
  // A failure arriving while the next reveal is already scheduled has to
  // re-arm it. Marking the item urgent sets the PACE to nothing, but the
  // pump was timed off the previous reveal and would still sit out the rest
  // of that wait first — measured, the reason reached the screen 1.3 seconds
  // after it was recorded rather than immediately.
  const urgent = instant || queue.some((item) => item.urgent);
  if (pumpTimer) {
    if (!urgent) return;
    clearTimeout(pumpTimer);
    pumpTimer = null;
  }
  pumpTimer = setTimeout(pump, urgent ? 0 : Math.max(0, revealFloorUntil - Date.now()));
}

/** Put one already-true fact on the screen. */
function reveal(item) {
  if (item.kind === 'state') revealState(item);
  else if (item.kind === 'log') revealLog(item);
  else if (item.kind === 'finale') revealFinale(item);
  revealFloorUntil = Date.now()
    + (item.kind === 'finale' && !instant ? FINALE_HOLD_MS : stepMs());
  drawProgress();
  keepLatestInView();
}

function revealState(item) {
  const phase = phases && phases[item.id];
  if (!phase) return;
  lastState[item.id] = item.state;
  phase.root.className = `agl-phase ${item.state}`;
  // The title is typed when the phase first appears, and left alone after.
  // Blocked included: the reader has to be able to see which step it was that
  // never got to run.
  if (!typedFor[item.id]) {
    typedFor[item.id] = true;
    typeTitle(phase.title, phase.label);
  }
  if (item.state !== 'active') {
    const caret = phase.title.querySelector('.agl-caret');
    if (caret) caret.remove();
  }
  // A blocked phase says so in its own words, always.
  //
  // This used to be conditional on the phase having no lines of its own,
  // which meant it almost never appeared: a step whose layer is the hub
  // inherits the hub's lines, so a stranded step showed "Continuing with 1
  // remaining step" and never the fact that it was the step that did not run.
  if (item.state === 'blocked' && !blockedNoted[item.id]) {
    blockedNoted[item.id] = true;
    phase.logs.insertAdjacentHTML('beforeend', logHtml({
      text: 'Did not run — the run stopped before this step was reached',
      tone: 'error',
    }));
  }
  if (item.state === 'done') holdThenCollapse(item.id);
}

/**
 * A finished phase is read, and only then folded away.
 *
 * It used to collapse on the same frame its tick landed, which meant the
 * lines it had just written left with it — the detail was drawn and removed
 * inside one animation frame. The hold is the fix, and it is why the dialog
 * is allowed to outlive the work by a bounded amount.
 */
function holdThenCollapse(id) {
  clearTimeout(collapseTimers[id]);
  const fold = () => {
    const phase = phases && phases[id];
    if (phase && lastState[id] === 'done') phase.root.classList.add('collapsed');
  };
  if (instant) { fold(); return; }
  collapseTimers[id] = setTimeout(fold, PHASE_HOLD_MS);
}

function revealLog(item) {
  const phase = phases && phases[item.id];
  if (!phase) return;
  // Lines are APPENDED as they are released, never rewritten: each one has an
  // entrance animation, and rebuilding the list would replay all of them every
  // time a single new line arrived.
  phase.logs.insertAdjacentHTML('beforeend', logHtml(item.line));
}

function revealFinale(item) {
  els.finale.hidden = false;
  els.finale.classList.toggle('failed', item.failed);
  setText(els.finaleTitle, item.failed ? 'Stopped' : 'Done');
  setText(els.finaleSub, item.sub);
}

/**
 * Release the whole queue now.
 *
 * Nielsen's third heuristic. The pacing above holds a reader on purpose, and
 * a hold with no way out of it is a trap: someone who has seen this screen
 * fifty times should not have to watch it read itself out. Skipping changes
 * only the display — the work is wherever it was, and if it is still running
 * the dialog stays up until it is not.
 */
function flush() {
  instant = true;
  if (pumpTimer) { clearTimeout(pumpTimer); pumpTimer = null; }
  if (typeTimer) { clearTimeout(typeTimer); typeTimer = null; }
  let guard = 2000;
  while (queue.length && guard > 0) { reveal(queue.shift()); guard -= 1; }
  revealFloorUntil = 0;
  // Half-typed titles are completed rather than left mid-word.
  if (phases) {
    Object.keys(phases).forEach((id) => {
      if (typedFor[id]) setText(phases[id].title, phases[id].label);
      if (lastState[id] === 'done') holdThenCollapse(id);
    });
  }
  updateSkip();
  keepLatestInView();
}

/** Offered only when there is something waiting to be shown. */
function updateSkip() {
  if (!els || !els.skip) return;
  els.skip.hidden = instant || !queue.length;
}

/** The newest line is the one being read. It is never left below the fold. */
function keepLatestInView() {
  if (!els || !els.shell) return;
  if (els.shell.scrollHeight > els.shell.clientHeight + 1) {
    els.shell.scrollTop = els.shell.scrollHeight;
  }
}

/* ─── render ─────────────────────────────────────────────────────────── */

function render(run) {
  const overlay = document.getElementById(OVERLAY_ID);
  if (!overlay) return;
  if (!run) {
    overlay.classList.remove('active');
    if (typeTimer) { clearTimeout(typeTimer); typeTimer = null; }
    resetPacing();
    if (els) {
      els.tree.innerHTML = '';
      els.finale.hidden = true;
      els.skip.hidden = true;
    }
    phases = null;
    lastPlanKey = '';
    return;
  }
  if (!els || !els.shell || !els.shell.isConnected) buildSkeleton(overlay);
  overlay.classList.add('active');

  // A different run means a different set of phases.
  const planKey = run.steps.map((s) => s.id).join('|');
  if (planKey !== lastPlanKey) {
    lastPlanKey = planKey;
    buildTree(run);
  }
  update(run);
}

function setText(el, text) {
  const value = text == null ? '' : String(text);
  if (el && el.textContent !== value) el.textContent = value;
}

function elapsedText(run) {
  const end = run.endedAt || Date.now();
  return `${Math.max(0, (end - run.startedAt) / 1000).toFixed(1)}s`;
}

function logHtml(line) {
  return `<div class="agl-log${line.tone === 'error' ? ' error' : ''}">`
    + '<span class="agl-log-mark" aria-hidden="true">▸</span>'
    + `<span class="agl-log-text">${escapeHtml(line.text)}</span>`
    + (line.value ? `<span class="agl-log-value">${escapeHtml(line.value)}</span>` : '')
    + '</div>';
}

/**
 * The fraction, and the count beside it, are what the READER has been shown.
 *
 * `progress(run)` still supplies the denominator, because the number of
 * dispatches planned is the recorder's fact and not this file's. The
 * numerator is the settled phases already on screen: a bar reading 5 of 5
 * above a tree still showing the third phase is a dialog disagreeing with
 * itself, and the reader believes the half that is moving.
 */
function drawProgress() {
  const run = getRun();
  if (!run || !els) return;
  const prog = progress(run);
  els.bar.classList.toggle('indeterminate', !prog);
  if (!prog) {
    els.barFill.style.width = '100%';
    setText(els.note, '');
    return;
  }
  const shown = run.steps.filter((s) => {
    const drawn = lastState[s.id];
    return drawn === 'done' || drawn === 'failed';
  }).length;
  els.barFill.style.width = `${Math.round((shown / prog.total) * 100)}%`;
  setText(els.note, ` · ${shown} of ${prog.total} complete`);
}

/**
 * Read the recorder, and queue whatever has become true since the last read.
 *
 * This is the only place an item is created, and every field on one is copied
 * off a step or off the run. Nothing downstream invents; `reveal` moves what
 * is handed to it onto the screen and chooses nothing.
 */
function update(run) {
  setText(els.title, run.title);
  setText(els.elapsed, elapsedText(run));

  run.steps.forEach((step, index) => {
    if (!phases[step.id]) return;
    const state = phaseState(step, Boolean(run.endedAt));
    const moved = queuedState[step.id] !== state;
    if (state === 'pending') return;

    // A phase that is STARTING is announced before it says anything. A phase
    // that is finishing says its last words first, and the tick lands after
    // them — a step that reports "27 KPIs computed" has not finished until it
    // has reported it.
    if (moved && state === 'active') {
      queuedState[step.id] = state;
      queue.push({ kind: 'state', id: step.id, state });
    }
    const already = loggedFor[step.id] || (loggedFor[step.id] = []);
    logsFor(run, step, index).forEach((line) => {
      const key = `${line.text}|${line.value || ''}|${line.tone || ''}`;
      if (already.includes(key)) return;
      already.push(key);
      queue.push({ kind: 'log', id: step.id, line });
    });
    if (moved && state !== 'active') {
      queuedState[step.id] = state;
      queue.push({ kind: 'state', id: step.id, state, urgent: state === 'failed' });
    }
  });

  if (run.endedAt && !queuedFinale) {
    queuedFinale = true;
    const prog = progress(run);
    queue.push({
      kind: 'finale',
      failed: Boolean(run.failure),
      sub: run.failure || (prog ? `${prog.done} of ${prog.total} steps completed` : ''),
    });
  }
  // The deadline opens when a backlog first forms and is not moved while
  // it is being worked off, so a burst arriving in pieces still drains
  // inside one budget rather than restarting it with every arrival.
  if (queue.length && drainBy < Date.now()) {
    drainBy = Date.now() + BACKLOG_BUDGET_MS;
  }
  startPump();
}

/* ─── the clock ──────────────────────────────────────────────────────── */

let clockTimer = null;

function tick() {
  const run = getRun();
  if (run && els && els.elapsed) setText(els.elapsed, elapsedText(run));
}

/* ─── mounting ───────────────────────────────────────────────────────── */

let mounted = false;

/**
 * Start drawing whatever the recorder holds.
 *
 * Idempotent: the overlay is a singleton and this can be called from every
 * entry point that raises a loading state.
 */
export function mountAgentLoading() {
  if (mounted) return;
  mounted = true;
  let overlay = document.getElementById(OVERLAY_ID);
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = OVERLAY_ID;
    overlay.className = 'agl-overlay';
    document.body.appendChild(overlay);
  }
  buildSkeleton(overlay);
  // Escape is what a reader reaches for to get out of a modal. There is no
  // "out" while work is running — cancelling a solve is not something this
  // client can do, and pretending otherwise would be worse than not offering
  // it — so it does the thing that IS available: stops the pacing.
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const ov = document.getElementById(OVERLAY_ID);
    if (ov && ov.classList.contains('active') && queue.length) flush();
  });
  subscribe((run) => {
    render(run);
    if (run && !run.endedAt) {
      if (!clockTimer) clockTimer = setInterval(tick, 100);
    } else if (clockTimer) {
      clearInterval(clockTimer);
      clockTimer = null;
    }
  });
}

/**
 * Take the overlay down.
 *
 * It waits for the queue to drain first. Closing on the last event would undo
 * the pacing entirely for the phase that matters most — the final one, whose
 * lines would be drawn and removed in the same breath.
 *
 * `holdMs` is the caller's own hold on top of that, and a failed run gets a
 * long one: the reason a run stopped is the one thing this screen must not
 * flash past. `MAX_DRAIN_MS` bounds the whole thing, so a backlog that never
 * empties cannot hold a workspace shut.
 */
export function dismissAgentLoading(holdMs = 0) {
  const deadline = Date.now() + MAX_DRAIN_MS;
  const close = () => {
    clearRun();
    const overlay = document.getElementById(OVERLAY_ID);
    if (overlay) overlay.classList.remove('active');
  };
  const whenRead = () => {
    const waiting = queue.length || Date.now() < revealFloorUntil;
    if (waiting && Date.now() < deadline) {
      setTimeout(whenRead, 120);
      return;
    }
    if (holdMs > 0) setTimeout(close, holdMs);
    else close();
  };
  whenRead();
}

if (typeof window !== 'undefined') {
  window.mountAgentLoading = mountAgentLoading;
}
