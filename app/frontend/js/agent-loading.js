/**
 * NetGravity — the agent loading screen
 * =====================================
 * The approved circular visualisation (`Dump/loading page.png`): an
 * orchestrator at the centre with five specialist layers around it, the
 * signal travelling between whichever two are actually talking, and a
 * progress bar counting real dispatches.
 *
 * This file draws. It decides nothing.
 *
 * Every state it renders comes from `agent-activity.js`, which records what
 * the application genuinely dispatched and folds in the orchestrator's own
 * execution trace. There is no sequence in here, no timer that advances a
 * stage, and no flow named after a screen. Point it at a run that engaged two
 * layers and it draws two lit layers and three dark ones.
 *
 * The parts that ARE decoration — the concentric rings, the drifting
 * particles — carry no information and are stated as such: they are marked
 * `aria-hidden` and they never change with state.
 */

import {
  LAYERS, HUB, subscribe, getRun, progress, layersEngaged, clearRun,
} from './agent-activity.js';

const OVERLAY_ID = 'agent-loading-overlay';

/* ─── the five layers, as the design names them ──────────────────────── */

/**
 * Position is an angle on the ring, in degrees clockwise from twelve o'clock,
 * matching the mockup: Intent at the top, then Scenario Planner, Extraction,
 * Forecasting, Reasoning.
 */
const LAYER_META = {
  intent: {
    name: 'Intent Layer', angle: 0,
    desc: 'Understanding your request',
    icon: '<path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-3.2-.6L3 21l1.8-5.2A8.4 8.4 0 0 1 12 3.1a8.4 8.4 0 0 1 9 8.4z"/>',
  },
  scenario: {
    name: 'Scenario Planner', angle: 72,
    desc: 'Designing & evaluating scenarios',
    icon: '<circle cx="18" cy="5" r="2.6"/><circle cx="6" cy="12" r="2.6"/><circle cx="18" cy="19" r="2.6"/><line x1="8.4" y1="10.7" x2="15.6" y2="6.3"/><line x1="8.4" y1="13.3" x2="15.6" y2="17.7"/>',
  },
  extraction: {
    name: 'Extraction Layer', angle: 144,
    // 'uploaded' dropped from the mockup's wording: at the dialog's scale
    // the line clamps to two, and the full sentence was cut mid-word.
    desc: 'Extracting & structuring data',
    icon: '<ellipse cx="12" cy="5.6" rx="7.4" ry="2.9"/><path d="M4.6 5.6v5.6c0 1.6 3.3 2.9 7.4 2.9s7.4-1.3 7.4-2.9V5.6"/><path d="M4.6 11.2v5.6c0 1.6 3.3 2.9 7.4 2.9s7.4-1.3 7.4-2.9v-5.6"/>',
  },
  forecasting: {
    name: 'Forecasting Layer', angle: 216,
    desc: 'Predicting future demand & trends',
    icon: '<polyline points="3 17 9 11 13 15 21 6.5"/><polyline points="15.4 6.5 21 6.5 21 12"/>',
  },
  reasoning: {
    name: 'Reasoning Layer', angle: 288,
    desc: 'Deriving insights & recommendations',
    icon: '<path d="M9.5 3.2A3.2 3.2 0 0 0 6.3 6.4a3 3 0 0 0-2 5.2 3 3 0 0 0 .6 4.6 3.1 3.1 0 0 0 4.6 3.5V3.2z"/><path d="M14.5 3.2a3.2 3.2 0 0 1 3.2 3.2 3 3 0 0 1 2 5.2 3 3 0 0 1-.6 4.6 3.1 3.1 0 0 1-4.6 3.5V3.2z"/>',
  },
};

/**
 * What a state is called, in the words the design uses.
 *
 * `idle` and `blocked` look similar and mean opposite things, so they are
 * never worded alike: idle is "nobody asked this layer to do anything", and
 * blocked is "this layer was part of the request and something stopped it".
 * The first is not a problem and the second is.
 */
const STATE_LABEL = {
  idle: 'Not involved in this request',
  waiting: 'Waiting for inputs…',
  active: 'ACTIVE',
  retrying: 'RETRYING',
  blocked: 'Blocked',
  done: 'Complete',
  failed: 'Failed',
};

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* ─── geometry ───────────────────────────────────────────────────────── */

/* One radius, in percentages of a SQUARE stage.

   It used to be two — 30% of the width and 33% of the height — because the
   stage was a wide rectangle filling the page, and two radii were what it
   took to stop a wide box from flattening the ring into a lens. That was a
   correction for the container's shape rather than a description of the
   figure, and it had to be re-tuned every time the container changed.
   The stage is now square (`aspect-ratio: 1/1`), so one radius draws a true
   circle at any size and nothing here has to know how big it is. */
const RADIUS = 40;    // % of the square stage

/* Clearance a callout keeps from the node it belongs to. `12.5%` is exactly
   half a node — they are sized at 25% of the same square — so this stays
   correct when the stage resizes, which a px value could not. */
const CALLOUT_GAP = 'calc(12.5% + 14px)';

function pointFor(angleDeg) {
  const rad = ((angleDeg - 90) * Math.PI) / 180;
  return { x: 50 + RADIUS * Math.cos(rad), y: 50 + RADIUS * Math.sin(rad) };
}

/* ─── markup ─────────────────────────────────────────────────────────── */

function badgeFor(state) {
  if (state === 'done') {
    return `<span class="agl-badge done" title="Complete"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="5 12.5 10 17.5 19 7"/></svg></span>`;
  }
  if (state === 'failed') {
    return `<span class="agl-badge failed" title="Failed"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><line x1="7" y1="7" x2="17" y2="17"/><line x1="17" y1="7" x2="7" y2="17"/></svg></span>`;
  }
  if (state === 'blocked') {
    return `<span class="agl-badge blocked" title="Blocked"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><line x1="9" y1="7" x2="9" y2="17"/><line x1="15" y1="7" x2="15" y2="17"/></svg></span>`;
  }
  if (state === 'active' || state === 'retrying') {
    return `<span class="agl-badge working" title="Working"><span class="agl-spinner"></span></span>`;
  }
  return '';
}

/**
 * The callout beside a layer.
 *
 * Only for layers with something to say. A layer nobody asked to do anything
 * has no card, because an empty card beside a dark circle reads as a card
 * that failed to load.
 */
function calloutHtml(id, agent) {
  if (agent.state === 'idle') return '';
  const meta = LAYER_META[id];
  const p = pointFor(meta.angle);
  // Outside its own node, on whichever side of the ring the node is on. A
  // node at dead centre-top counts as the right-hand side, which is where the
  // mockup puts the Intent Layer's card.
  const side = p.x >= 50 ? 'right' : 'left';
  const edge = side === 'right'
    ? `left:calc(${p.x}% + ${CALLOUT_GAP})`
    : `right:calc(${(100 - p.x).toFixed(2)}% + ${CALLOUT_GAP})`;
  const head = agent.state === 'active' || agent.state === 'retrying'
    ? `${STATE_LABEL[agent.state]}: ${escapeHtml(agent.headline || meta.desc)}`
    : escapeHtml(agent.headline || STATE_LABEL[agent.state]);
  const lines = (agent.lines || []).filter(Boolean).slice(0, 3);
  return `
    <div class="agl-callout side-${side} state-${agent.state}" data-callout="${id}"
         style="top:${p.y}%;${edge}">
      <div class="agl-callout-head">${head}</div>
      ${lines.map((l) => `<div class="agl-callout-line">${escapeHtml(l)}</div>`).join('')}
    </div>`;
}

/**
 * The signal in flight, as an SVG line with a travelling dot.
 *
 * Drawn only while there IS one. `from`/`to` are the two nodes actually
 * exchanging something, so the line is never between a pair that is not
 * talking.
 */
function signalHtml(signal) {
  if (!signal) return '';
  const a = signal.from === HUB ? { x: 50, y: 50 } : pointFor(LAYER_META[signal.from].angle);
  const b = signal.to === HUB ? { x: 50, y: 50 } : pointFor(LAYER_META[signal.to].angle);
  const tone = signal.tone === 'error' ? 'error' : 'ok';
  // The line is SVG stretched to the stage (`preserveAspectRatio: none`), which
  // is right for a line and wrong for a dot: a circle in that viewBox comes out
  // as an ellipse as wide as the stage's aspect ratio. So the dot is an element
  // of its own, positioned in the same percentages and animated between them,
  // which keeps it round at any stage shape.
  return `
    <svg class="agl-signal tone-${tone}" viewBox="0 0 100 100"
         preserveAspectRatio="none" aria-hidden="true">
      <line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" class="agl-signal-line"/>
    </svg>
    <span class="agl-signal-dot tone-${tone}" aria-hidden="true"
          style="--x1:${a.x}%;--y1:${a.y}%;--x2:${b.x}%;--y2:${b.y}%"></span>`;
}

function elapsedText(run) {
  const end = run.endedAt || Date.now();
  const total = Math.max(0, Math.round((end - run.startedAt) / 1000));
  const mm = String(Math.floor(total / 60)).padStart(2, '0');
  const ss = String(total % 60).padStart(2, '0');
  return `00:${mm}:${ss}`;
}

/* ─── the skeleton ───────────────────────────────────────────────────── */

/**
 * Everything that does NOT depend on the run, built once.
 *
 * The dialog used to be re-created from a template string on every state
 * change. That is the obvious way to write a view and it is wrong here for
 * one reason: replacing an element restarts every CSS animation inside it.
 * The dialog's entrance played again on every emit — and with the live poll
 * and the signal queue both firing, it never got past the opening frames of
 * its own fade, so the screen showed a blurred workspace and no dialog at
 * all. The travelling signal had the same problem in a form nobody could miss
 * once it was pointed out: a 1.9-second crossing restarted from the beginning
 * whenever anything changed, so the dot never once arrived.
 *
 * So the structure is built once and `update()` writes text and class names
 * into it. Nothing that animates is replaced while it is animating.
 */
function skeletonHtml() {
  return `
    <div class="agl-shell" role="status" aria-live="polite">
      <h1 class="agl-title">AI agents are
        <span class="agl-title-verb"></span></h1>
      <p class="agl-sub"></p>

      <div class="agl-stage">
        <div class="agl-rings" aria-hidden="true">
          <span class="agl-ring r1"></span>
          <span class="agl-ring r2"></span>
          <span class="agl-ring r3"></span>
          <span class="agl-particles">
            ${Array.from({ length: 14 }, (_, i) => `<i style="--i:${i}"></i>`).join('')}
          </span>
        </div>
        <svg class="agl-links" viewBox="0 0 100 100" preserveAspectRatio="none"
             aria-hidden="true">
          ${LAYERS.map((id) => {
            const pt = pointFor(LAYER_META[id].angle);
            return `<line x1="50" y1="50" x2="${pt.x}" y2="${pt.y}"
                          class="agl-link" data-link="${id}"/>`;
          }).join('')}
        </svg>
        <div class="agl-signal-host"></div>

        <div class="agl-hub">
          <span class="agl-hub-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
                 stroke-linecap="round" stroke-linejoin="round">
              <circle cx="6" cy="5.5" r="2.2"/><circle cx="18" cy="5.5" r="2.2"/>
              <circle cx="6" cy="18.5" r="2.2"/><circle cx="18" cy="18.5" r="2.2"/>
              <path d="M6 7.7v8.6M18 7.7v8.6M8.2 5.5c3 3.2 6.6 9.8 9.8 13"/>
            </svg>
          </span>
          <span class="agl-hub-name">Orchestrator Agent</span>
          <span class="agl-hub-state"></span>
          <span class="agl-hub-line"></span>
        </div>

        ${LAYERS.map((id) => {
          const meta = LAYER_META[id];
          const pt = pointFor(meta.angle);
          return `
            <div class="agl-node" data-layer="${id}" style="left:${pt.x}%;top:${pt.y}%">
              <span class="agl-badge-host"></span>
              <span class="agl-node-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     stroke-width="1.7" stroke-linecap="round"
                     stroke-linejoin="round">${meta.icon}</svg>
              </span>
              <span class="agl-node-name">${escapeHtml(meta.name)}</span>
              <span class="agl-node-desc">${escapeHtml(meta.desc)}</span>
            </div>`;
        }).join('')}

        <div class="agl-callout-host"></div>
      </div>

      <div class="agl-progress-card">
        <span class="agl-progress-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
               stroke-linecap="round" stroke-linejoin="round"><path d="M12 2.6 14 9l6.4 2-6.4 2-2 6.4-2-6.4L3.6 11 10 9z"/></svg>
        </span>
        <div class="agl-progress-text">
          <div class="agl-progress-title"></div>
          <div class="agl-progress-msg"></div>
          <div class="agl-progress-bar"><span></span></div>
        </div>
        <div class="agl-progress-figs">
          <div class="agl-fig"><span class="agl-fig-value" data-fig="pct"></span></div>
          <div class="agl-fig">
            <span class="agl-fig-value" data-fig="engaged"></span>
            <span class="agl-fig-label">Layers engaged</span>
          </div>
          <div class="agl-fig">
            <span class="agl-fig-value" id="agl-elapsed"></span>
            <span class="agl-fig-label">Elapsed time</span>
          </div>
        </div>
      </div>

      <p class="agl-trace-note" hidden></p>

      <p class="agl-foot">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4.5" y="10.5" width="15" height="10" rx="2.2"/><path d="M8 10.5V7.6a4 4 0 0 1 8 0v2.9"/></svg>
        Your data is secure and encrypted during processing.
      </p>
    </div>`;
}

/** Cached references into the skeleton, so `update` never queries the DOM. */
let els = null;

function buildSkeleton(overlay) {
  overlay.innerHTML = skeletonHtml();
  const q = (sel) => overlay.querySelector(sel);
  els = {
    shell: q('.agl-shell'),
    verb: q('.agl-title-verb'),
    sub: q('.agl-sub'),
    links: {},
    nodes: {},
    badges: {},
    signalHost: q('.agl-signal-host'),
    calloutHost: q('.agl-callout-host'),
    hub: q('.agl-hub'),
    hubState: q('.agl-hub-state'),
    hubLine: q('.agl-hub-line'),
    card: q('.agl-progress-card'),
    title: q('.agl-progress-title'),
    msg: q('.agl-progress-msg'),
    bar: q('.agl-progress-bar'),
    barFill: q('.agl-progress-bar > span'),
    pct: q('[data-fig="pct"]'),
    engaged: q('[data-fig="engaged"]'),
    elapsed: q('#agl-elapsed'),
    note: q('.agl-trace-note'),
  };
  LAYERS.forEach((id) => {
    els.links[id] = overlay.querySelector(`[data-link="${id}"]`);
    els.nodes[id] = overlay.querySelector(`[data-layer="${id}"]`);
    els.badges[id] = els.nodes[id].querySelector('.agl-badge-host');
  });
}

/* What was last written, so nothing is written twice. Setting an identical
   class or innerHTML is not free: it is what restarts an animation. */
let lastNodeState = {};
let lastHubState = '';
let lastSignalKey = '';
let lastCalloutKey = '';

function setText(el, text) {
  const value = text == null ? '' : String(text);
  if (el && el.textContent !== value) el.textContent = value;
}

function render(run) {
  const overlay = document.getElementById(OVERLAY_ID);
  if (!overlay) return;
  if (!run) {
    overlay.classList.remove('active');
    lastNodeState = {};
    lastHubState = '';
    lastSignalKey = '';
    lastCalloutKey = '';
    if (els) {
      els.signalHost.innerHTML = '';
      els.calloutHost.innerHTML = '';
    }
    return;
  }
  if (!els || !els.shell || !els.shell.isConnected) buildSkeleton(overlay);
  overlay.classList.add('active');
  update(run);
}

function update(run) {
  const prog = progress(run);
  const engaged = layersEngaged(run);
  const hub = run.agents[HUB];
  const active = run.steps.find((s) => s.status === 'active');
  const message = run.failure
    || (active ? (active.message || active.label) : '')
    || (run.endedAt ? 'Finished.' : run.subtitle);

  els.shell.setAttribute('aria-label', run.title);
  setText(els.verb, run.verb);
  setText(els.sub, run.subtitle);

  // The hub.
  if (hub.state !== lastHubState) {
    els.hub.className = `agl-hub state-${hub.state}`;
    lastHubState = hub.state;
  }
  setText(els.hubState, hub.headline || 'Coordinating');
  setText(els.hubLine, (hub.lines || []).length ? hub.lines[hub.lines.length - 1] : '');

  // The layers, and the links to them.
  LAYERS.forEach((id) => {
    const agent = run.agents[id];
    if (lastNodeState[id] === agent.state) return;
    lastNodeState[id] = agent.state;
    els.nodes[id].className = `agl-node state-${agent.state}`;
    els.badges[id].innerHTML = badgeFor(agent.state);
    els.links[id].setAttribute(
      'class', `agl-link${agent.state === 'idle' ? '' : ' on'}`);
  });

  // The signal. Replaced ONLY when a different hand-off is being shown, so a
  // crossing that is under way is allowed to finish.
  const sig = run.signal;
  const key = sig ? `${sig.from}>${sig.to}@${sig.at}` : '';
  if (key !== lastSignalKey) {
    lastSignalKey = key;
    els.signalHost.innerHTML = signalHtml(sig);
  }

  // The callouts, likewise: rewritten only when what they say changes.
  const calloutMarkup = LAYERS.map((id) => calloutHtml(id, run.agents[id])).join('');
  if (calloutMarkup !== lastCalloutKey) {
    lastCalloutKey = calloutMarkup;
    els.calloutHost.innerHTML = calloutMarkup;
  }

  // The progress card.
  els.card.classList.toggle('failed', Boolean(run.failure));
  setText(els.title, run.title);
  setText(els.msg, message);
  els.bar.classList.toggle('indeterminate', !prog);
  els.barFill.style.width = `${prog ? prog.pct : 100}%`;
  setText(els.pct, prog ? `${prog.pct}%` : '\u2014');
  setText(els.engaged, `${engaged.engaged} of ${engaged.total}`);
  setText(els.elapsed, elapsedText(run));

  const note = run.traceNotes.length ? run.traceNotes[run.traceNotes.length - 1] : '';
  setText(els.note, note);
  els.note.hidden = !note;
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
  subscribe((run) => {
    render(run);
    if (run && !run.endedAt) {
      if (!clockTimer) clockTimer = setInterval(tick, 1000);
    } else if (clockTimer) {
      clearInterval(clockTimer);
      clockTimer = null;
    }
  });
}

/**
 * Take the overlay down.
 *
 * `holdMs` keeps a failed run on screen long enough to be read — the reason a
 * run stopped is the one thing a loading screen must not flash past.
 */
export function dismissAgentLoading(holdMs = 0) {
  const done = () => {
    clearRun();
    const overlay = document.getElementById(OVERLAY_ID);
    if (overlay) overlay.classList.remove('active');
  };
  if (holdMs > 0) setTimeout(done, holdMs);
  else done();
}

if (typeof window !== 'undefined') {
  window.mountAgentLoading = mountAgentLoading;
}
