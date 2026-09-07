/**
 * NetGravity — what the agents are actually doing, right now
 * ==========================================================
 *
 * A recorder, not a script.
 *
 * The loading visualisation shows five specialist layers around an
 * orchestrator, and it has to show the ones that are genuinely working on the
 * request in front of the user. That is a hard requirement rather than a
 * stylistic one: this repository has now had to delete TWO animated "agent
 * pipelines" that advanced on timers and named a solver the product does not
 * use. The second, `agent-reasoning.js`, ran four stages on 450ms timers,
 * filled a progress bar to 100% and printed lines into a fake terminal — in
 * front of a tab change, with no work happening behind it at all. A picture of
 * agents talking to each other is a claim about the system, and a claim has to
 * be true.
 *
 * So this module holds no sequence of its own. It offers three things:
 *
 *   1. A caller declares the work it is ABOUT to do — `startRun({ plan })`.
 *      The plan is the caller's own list of real dispatches, in the order it
 *      will make them. `hydrateFromBackend` knows it will load the network,
 *      solve it, ask for a briefing, read the scenarios and build the
 *      forecast, because that is what its next five `await`s are.
 *
 *   2. The caller reports each dispatch as it genuinely starts, finishes,
 *      fails or is retried — `stepStart` / `stepDone` / `stepFail`.
 *
 *   3. When a response carries an `execution_id`, the recorder fetches that
 *      execution's trace from `/orchestrator/executions/<id>/trace` and folds
 *      the SERVER's account of the run in on top: which capabilities actually
 *      ran, how many attempts each took, which failed, and what intent the
 *      orchestrator resolved. That is the authoritative record, and it is what
 *      turns "the client asked for a scenario" into "the scenario planner ran
 *      `optimization.solve_scenario` twice and succeeded on the second".
 *
 * Nothing here decides what runs, reorders anything, or retries anything. It
 * observes. If a layer is dark, no one asked it to do anything.
 *
 * Parallel work is first-class: `startRun` takes steps with an optional
 * `group`, and every step in a group is dispatched together and shown active
 * together, because that is how `Promise.all` behaves in the caller.
 */

/* ─── the layers ─────────────────────────────────────────────────────── */

/**
 * The five specialist layers and the hub, by id.
 *
 * These are the names in the approved design. What each one MEANS here is the
 * set of orchestrator capabilities that belong to it — see `layerForCapability`
 * — so a layer lighting up always corresponds to a capability the registry
 * actually holds.
 */
export const LAYERS = ['intent', 'reasoning', 'scenario', 'forecasting', 'extraction'];
export const HUB = 'orchestrator';

/**
 * Which layer owns a capability, by the registry's own naming.
 *
 * The live registry (`GET /orchestrator/capabilities`) holds sixteen:
 * extraction.parse, network.load_snapshot, forecast.demand,
 * signal.route_for_forecast, market.score_signal, external.interpret_signal,
 * scenario.create, scenario.validate, optimization.solve_scenario,
 * optimization.solve, kpi.summarise, twin.publish, risk.compute_rf,
 * resilience.assess, reasoning.synthesise, governance.classify.
 *
 * Mapped by prefix rather than by an enumerated list so a capability added to
 * the registry lands somewhere sensible instead of vanishing from the picture.
 * Anything unrecognised is the orchestrator's own work, which is the truthful
 * default: the orchestrator is what ran it.
 */
export function layerForCapability(name) {
  const id = String(name || '').toLowerCase();
  if (id.startsWith('extraction.') || id.startsWith('network.')) return 'extraction';
  if (id.startsWith('forecast.') || id.startsWith('signal.')
      || id.startsWith('market.') || id.startsWith('external.')) return 'forecasting';
  if (id.startsWith('scenario.') || id === 'optimization.solve_scenario') return 'scenario';
  if (id.startsWith('reasoning.') || id.startsWith('governance.')) return 'reasoning';
  return HUB;
}

/* ─── state ──────────────────────────────────────────────────────────── */

const listeners = new Set();

/** The run in progress, or null. Never partially built: replaced wholesale. */
let run = null;

/* ─── the signal queue ───────────────────────────────────────────────
   Hand-offs are recorded the instant they happen and can happen faster than
   a person can see. Hydration awaits its five stages back to back, so the
   answer coming BACK from a layer was overwritten by the next request going
   out in the same tick: the return leg existed for microseconds and was never
   drawn once.

   So signals queue, and each is shown for a minimum dwell before the next
   replaces it. Every signal displayed is one that genuinely happened, with
   its own message — nothing is invented and nothing is reordered. Only the
   speed is slowed, and only for the picture: the work never waits for it.

   The queue is bounded. If more hand-offs pile up than can be shown at
   reading speed, the oldest are dropped rather than falling further behind
   what the system is doing — a signal shown thirty seconds late would be a
   lie about the present. */
/* 1500ms, paired with the dot's 1.9s crossing: one hand-off is roughly one
   trip across the diagram, which is the pace at which a person can read WHO
   is passing WHAT to WHOM. At 620ms the dot crossed twice per message and the
   ring read as traffic rather than as a conversation.
   The queue is shorter to match: three messages at this dwell is 4.5 seconds,
   and anything further behind that would be describing a past the rest of the
   screen has already moved on from. */
const SIGNAL_DWELL_MS = 1500;
const SIGNAL_QUEUE_MAX = 2;
let signalQueue = [];
let signalTimer = null;

function pumpSignals() {
  if (!run) { signalQueue = []; signalTimer = null; return; }
  if (!signalQueue.length) { signalTimer = null; return; }
  run.signal = signalQueue.shift();
  emit();
  signalTimer = setTimeout(pumpSignals, SIGNAL_DWELL_MS);
}

function pushSignal(signal) {
  if (!run) return;
  // The orchestrator's OWN work — the MILP solve is the case — is a step
  // whose layer is the hub. There is no hand-off in that: the line would run
  // from the centre of the hub to the centre of the hub, which draws as a dot
  // and reads as a glitch. What is being done is said on the hub itself.
  if (signal.from === signal.to) return;
  // Every hand-off that genuinely happened, whether or not it was on screen
  // long enough to be seen. The queue below shows them at reading speed and
  // drops the overflow rather than falling behind the present; this keeps the
  // record of what actually passed between agents.
  run.signalLog.push({ from: signal.from, to: signal.to,
                       message: signal.message, at: signal.at,
                       tone: signal.tone || null });
  if (run.signalLog.length > 40) run.signalLog.shift();
  if (signalTimer) {
    signalQueue.push(signal);
    if (signalQueue.length > SIGNAL_QUEUE_MAX) {
      signalQueue = signalQueue.slice(-SIGNAL_QUEUE_MAX);
    }
    return;
  }
  run.signal = signal;
  signalTimer = setTimeout(pumpSignals, SIGNAL_DWELL_MS);
}

function stopSignals() {
  if (signalTimer) clearTimeout(signalTimer);
  signalTimer = null;
  signalQueue = [];
}

function blankAgents() {
  const agents = {};
  LAYERS.forEach((id) => {
    agents[id] = { state: 'idle', headline: '', lines: [], attempts: 0 };
  });
  agents[HUB] = { state: 'idle', headline: '', lines: [], attempts: 0 };
  return agents;
}

function emit() {
  const snapshot = getRun();
  listeners.forEach((fn) => {
    try { fn(snapshot); } catch (e) { /* a listener must never break a run */ }
  });
}

/** Subscribe to every change. Returns an unsubscribe function. */
export function subscribe(fn) {
  listeners.add(fn);
  try { fn(getRun()); } catch (e) { /* ignore */ }
  return () => listeners.delete(fn);
}

/** A read-only copy of the current run, or null when nothing is running. */
export function getRun() {
  if (!run) return null;
  return {
    title: run.title,
    verb: run.verb,
    subtitle: run.subtitle,
    startedAt: run.startedAt,
    endedAt: run.endedAt,
    failure: run.failure,
    steps: run.steps.map((s) => ({ ...s })),
    agents: JSON.parse(JSON.stringify(run.agents)),
    signal: run.signal ? { ...run.signal } : null,
    signalLog: run.signalLog.map((x) => ({ ...x })),
    executionIds: [...run.executionIds],
    traceNotes: [...run.traceNotes],
  };
}

export function isRunning() {
  return run !== null && !run.endedAt;
}

/* ─── the run ────────────────────────────────────────────────────────── */

/**
 * Begin recording a run.
 *
 * `plan` is the caller's own list of the dispatches it is about to make:
 *
 *   [{ id, layer, label, message, group }]
 *
 * `layer` is which of the five (or the hub) the work is going to. `message` is
 * what is being passed, in the user's words — it is shown on the signal
 * travelling between the hub and that layer. `group` marks steps that are
 * dispatched together; steps sharing a group are shown active at the same
 * time, because they are.
 *
 * A caller that cannot say in advance passes fewer steps and adds them with
 * `addStep`; progress then reports the fraction of what is known, which is
 * what a fraction of an unknown total honestly is.
 */
export function startRun({ title, verb, subtitle, plan }) {
  run = {
    title: title || 'Working',
    verb: verb || 'working on',
    subtitle: subtitle || '',
    startedAt: Date.now(),
    endedAt: null,
    failure: null,
    steps: (plan || []).map((s) => ({
      id: s.id,
      layer: s.layer,
      label: s.label || '',
      message: s.message || '',
      group: s.group || null,
      status: 'pending',
      detail: '',
      attempts: 0,
      startedAt: null,
      endedAt: null,
    })),
    agents: blankAgents(),
    signal: null,
    signalLog: [],
    executionIds: [],
    traceNotes: [],
  };

  // Every layer the plan will reach is "waiting for inputs" from the first
  // frame, and every layer it will not is dark. That distinction is the whole
  // point of the subdued state: dark means "not part of this request".
  run.steps.forEach((s) => {
    const a = run.agents[s.layer];
    if (a && a.state === 'idle') a.state = 'waiting';
  });

  // The intent layer is the one thing that is genuinely already working when a
  // run begins: the caller has just turned a user action into a request. It is
  // marked done by the first dispatch, which is the moment that resolution
  // produced something.
  run.agents.intent.state = 'active';
  run.agents.intent.headline = 'Understanding the request';
  // Replaced by the orchestrator's OWN resolved intent as soon as the first
  // trace arrives — see `absorbTrace`. Until then it says what is being done,
  // not what the page's subtitle already says.
  run.agents.intent.lines = ['Turning this request into a plan the '
                             + 'orchestrator can run'];
  run.agents[HUB].state = 'active';
  run.agents[HUB].headline = 'Coordinating';
  ensureRequestObserver();
  emit();
  return run;
}

/** Add a dispatch that was not known when the run started. */
export function addStep(step) {
  if (!run) return;
  run.steps.push({
    id: step.id,
    layer: step.layer,
    label: step.label || '',
    message: step.message || '',
    group: step.group || null,
    status: 'pending',
    detail: '',
    attempts: 0,
    startedAt: null,
    endedAt: null,
  });
  const a = run.agents[step.layer];
  if (a && a.state === 'idle') a.state = 'waiting';
  emit();
}

function findStep(id) {
  return run ? run.steps.find((s) => s.id === id) : null;
}

/** One dispatch has genuinely left the client. */
export function stepStart(id, { message, lines } = {}) {
  const step = findStep(id);
  if (!step) return;
  step.status = 'active';
  step.startedAt = Date.now();
  if (message) step.message = message;

  // Intent resolution produced a plan the moment the first request went out.
  if (run.agents.intent.state === 'active') {
    run.agents.intent.state = 'done';
    run.agents.intent.headline = 'Request understood';
  }

  const agent = run.agents[step.layer];
  if (agent) {
    agent.state = 'active';
    agent.headline = step.label || 'Working';
    // What it was asked for, as its own second line. Without this an active
    // layer showed a heading and nothing under it, and the one sentence
    // describing the work was only ever visible on the signal in flight —
    // which lasts a second and is gone.
    agent.lines = lines ? [].concat(lines) : [step.message].filter(Boolean);
  }
  run.agents[HUB].state = 'active';
  // "Coordinating" is what the hub does while a SPECIALIST works. When the
  // step's own layer IS the hub — the solve runs through the capability
  // executor and the design names no optimisation satellite — the hub is the
  // one working, and saying "Coordinating" would hide the only thing
  // happening on screen behind a word for supervising someone else.
  if (step.layer !== HUB) {
    run.agents[HUB].headline = 'Coordinating';
  }

  // The signal travels FROM the hub TO the layer: the orchestrator is what
  // dispatched it.
  pushSignal({ from: HUB, to: step.layer, message: step.message, at: Date.now() });
  emit();
}

/** One dispatch came back. `executionId` folds the server's own record in. */
export function stepDone(id, { detail, executionId, lines } = {}) {
  const step = findStep(id);
  if (!step) return;
  step.status = 'done';
  step.endedAt = Date.now();
  if (detail) step.detail = detail;

  const agent = run.agents[step.layer];
  if (agent) {
    // Still active while another step of this run is using the same layer.
    const busy = run.steps.some((s) => s.layer === step.layer && s.status === 'active');
    agent.state = busy ? 'active' : 'done';
    if (detail) agent.lines = [detail];
    if (lines) agent.lines = [].concat(lines);
    agent.headline = busy ? agent.headline : (step.label || 'Complete');
  }
  // The answer travels back to the hub.
  pushSignal({
    from: step.layer, to: HUB,
    message: detail || `${step.label} complete`, at: Date.now(),
  });
  emit();
  if (executionId) absorbTrace(executionId);
}

/** One dispatch failed. Recorded as a failure; nothing is filled in for it. */
export function stepFail(id, { error, executionId } = {}) {
  const step = findStep(id);
  if (!step) return;
  step.status = 'failed';
  step.endedAt = Date.now();
  step.detail = error || 'failed';
  const agent = run.agents[step.layer];
  if (agent) {
    agent.state = 'failed';
    agent.headline = 'Could not complete';
    agent.lines = [error || 'No reason was reported.'];
  }
  // The orchestrator is what receives the failure, so it is what has to be
  // seen reacting to it. What it says depends on whether there is anything
  // left to run: a failed step with four dispatches still to come is a run
  // continuing without that evidence, and a failed step with none left is a
  // run that is over. Neither is a claim that recovery is under way — this
  // client does not retry, and saying it was would be a lie about the system.
  const remaining = run.steps.filter((x) => x.status === 'pending').length;
  run.agents[HUB].state = 'active';
  run.agents[HUB].headline = 'Handling a failed step';
  run.agents[HUB].lines = remaining
    ? [`${step.label || step.id} failed`,
       `Continuing with ${remaining} remaining step${remaining === 1 ? '' : 's'}, `
       + 'without its evidence']
    : [`${step.label || step.id} failed`, 'No steps remain to run'];

  // A failure jumps the queue: it is the thing the reader needs to see.
  stopSignals();
  run.signal = { from: step.layer, to: HUB, message: error || 'failed',
                 at: Date.now(), tone: 'error' };
  emit();
  if (executionId) absorbTrace(executionId);
}

/**
 * A dispatch is being attempted again.
 *
 * Only called where a retry genuinely happens — either the client retried, or
 * a trace reported `attempts > 1` for a capability.
 */
export function stepRetry(id, attempt) {
  const step = findStep(id);
  if (!step) return;
  step.attempts = attempt;
  step.status = 'active';
  const agent = run.agents[step.layer];
  if (agent) {
    agent.state = 'retrying';
    agent.attempts = attempt;
    agent.headline = `Retrying (attempt ${attempt})`;
  }
  emit();
}

/** Attach a line of real detail to a layer without changing its state. */
export function note(layer, lines) {
  if (!run || !run.agents[layer]) return;
  run.agents[layer].lines = [].concat(lines);
  emit();
}

/** The run is over. `error` is shown as the reason; nothing is invented. */
export function finishRun({ error } = {}) {
  if (!run) return;
  stopSignals();
  stopAllWatching();
  run.endedAt = Date.now();
  run.failure = error || null;
  run.agents[HUB].state = error ? 'failed' : 'done';
  run.agents[HUB].headline = error ? 'Stopped' : 'Complete';
  // A layer with a planned dispatch that never left is BLOCKED, not idle and
  // not complete: it was part of this request and something upstream stopped
  // it. Idle would say it was never involved, which is the opposite.
  const strandedLayers = new Set(
    run.steps.filter((x) => x.status === 'pending').map((x) => x.layer));
  LAYERS.forEach((id) => {
    const a = run.agents[id];
    // A layer that was never dispatched to stays dark; one still marked active
    // with the run over is reported as it ended, not as complete.
    if (a.state === 'active') a.state = error ? 'failed' : 'done';
    if (strandedLayers.has(id) && a.state !== 'failed' && a.state !== 'done') {
      a.state = 'blocked';
      a.headline = 'Did not run';
      if (!a.lines.length) {
        a.lines = [error ? 'The run stopped before this step was reached.'
                         : 'The run finished before this step was reached.'];
      }
    } else if (a.state === 'waiting') {
      a.state = error ? 'blocked' : 'idle';
    }
  });
  run.signal = null;
  emit();
}

/** Drop the run entirely (the overlay has closed). */
export function clearRun() {
  stopSignals();
  stopAllWatching();
  run = null;
  emit();
}

/* ─── the event vocabulary ───────────────────────────────────────────── */

/**
 * The events this recorder understands.
 *
 * A named vocabulary so a producer does not have to know which function to
 * call: anything that learns something about a run — the dispatch sites in
 * this application today, a server-sent stream tomorrow — can describe what
 * happened and hand it over.
 *
 * `agent_blocked` is here alongside the eight obvious ones because Blocked is
 * a state the orchestrator genuinely reports (`REQUIRES_APPROVAL`,
 * `REQUIRES_HUMAN`, and its own `blocked_steps` list) and a run that is
 * waiting on a person is not a run that is working.
 */
export const AGENT_EVENTS = [
  'agent_started',
  'signal_sent',
  'agent_progress',
  'agent_completed',
  'agent_waiting',
  'agent_blocked',
  'agent_failed',
  'retry_started',
  'orchestration_updated',
  'workflow_completed',
];

/**
 * Apply one event.
 *
 * A façade over the functions above and NOT a second state machine: every
 * branch calls the same recorder the direct API calls, so there is one store,
 * one set of transitions, and no way for the two entry points to disagree.
 *
 * Unknown event types are ignored rather than guessed at. An event that names
 * a step the run does not have adds it, because a producer that knows about a
 * dispatch this client did not plan is telling us something true.
 */
export function applyAgentEvent(event) {
  if (!run || !event || typeof event !== 'object') return;
  const { type } = event;
  const stepId = event.step || event.id;

  switch (type) {
    case 'agent_started':
      if (stepId && !findStep(stepId)) {
        addStep({ id: stepId, layer: event.layer || HUB,
                  label: event.label || '', message: event.message || '' });
      }
      if (stepId) stepStart(stepId, { message: event.message, lines: event.lines });
      break;

    case 'signal_sent':
      pushSignal({ from: event.from || HUB, to: event.to || HUB,
                   message: event.message || '', at: Date.now(),
                   tone: event.tone || null });
      emit();
      break;

    case 'agent_progress':
      if (event.layer && event.lines) note(event.layer, event.lines);
      break;

    case 'agent_completed':
      if (stepId) {
        stepDone(stepId, { detail: event.detail, lines: event.lines,
                           executionId: event.executionId });
      }
      break;

    case 'agent_waiting':
      if (event.layer && run.agents[event.layer]) {
        run.agents[event.layer].state = 'waiting';
        if (event.message) run.agents[event.layer].lines = [event.message];
        emit();
      }
      break;

    case 'agent_blocked':
      if (event.layer && run.agents[event.layer]) {
        run.agents[event.layer].state = 'blocked';
        run.agents[event.layer].headline = event.label || 'Blocked';
        if (event.message) run.agents[event.layer].lines = [event.message];
        emit();
      }
      break;

    case 'agent_failed':
      if (stepId) stepFail(stepId, { error: event.error, executionId: event.executionId });
      break;

    case 'retry_started':
      if (stepId) stepRetry(stepId, event.attempt || 2);
      break;

    case 'orchestration_updated':
      if (event.executionId) absorbTrace(event.executionId);
      if (event.live) absorbLive(event.live);
      break;

    case 'workflow_completed':
      finishRun({ error: event.error || null });
      break;

    default:
      break;   // an event this build does not know is not an error
  }
}

/* ─── the server's own account ───────────────────────────────────────── */

/**
 * Fold one execution's trace into the picture.
 *
 * `/orchestrator/executions/<id>/trace` is the authoritative record of what
 * ran: `tool_invocations` carries the capability, whether it succeeded, how
 * long it took and — the reason this is worth a round trip — how many
 * ATTEMPTS it needed. A capability that failed twice and succeeded on the
 * third is a retry the user should see, and the client has no other way to
 * know it happened.
 *
 * Best-effort by design. A trace that is missing, unauthorised or slow changes
 * nothing on screen: the client's own account of its dispatches is already
 * true, this only adds detail to it.
 */
async function absorbTrace(executionId) {
  if (!run || !executionId || run.executionIds.includes(executionId)) return;
  run.executionIds.push(executionId);

  let trace = null;
  try {
    const mod = await import('./integration/api-client.js');
    const client = mod.apiClient || mod.default;
    if (!client || typeof client.get !== 'function') return;
    trace = await client.get(`/orchestrator/executions/${encodeURIComponent(executionId)}/trace`);
  } catch (e) {
    return;   // no trace is not an error; it is simply less detail
  }
  if (!run || !trace || typeof trace !== 'object') return;

  // What the orchestrator resolved the request to be, and how.
  const intent = trace.intent || {};
  if (intent.interpreted) {
    const conf = (typeof intent.confidence === 'number')
      ? ` · confidence ${(intent.confidence * 100).toFixed(0)}%` : '';
    run.agents.intent.state = 'done';
    run.agents.intent.headline = 'Request understood';
    run.agents.intent.lines = [
      `Intent: ${String(intent.interpreted).replace(/_/g, ' ').toLowerCase()}`,
      `Resolved by ${String(intent.source || 'the planner').toLowerCase()}${conf}`,
    ];
  }

  // Which capabilities actually ran, and what they cost.
  const invocations = Array.isArray(trace.tool_invocations) ? trace.tool_invocations : [];
  const perLayer = {};
  invocations.forEach((inv) => {
    const layer = layerForCapability(inv.capability);
    const bucket = perLayer[layer] || (perLayer[layer] = { ran: [], retried: [], failed: [] });
    bucket.ran.push(inv.capability);
    if ((inv.attempts || 1) > 1) bucket.retried.push(`${inv.capability} ×${inv.attempts}`);
    if (inv.success === false) bucket.failed.push(inv.capability);
  });

  Object.entries(perLayer).forEach(([layer, b]) => {
    const agent = run.agents[layer];
    if (!agent) return;
    if (agent.state === 'idle') agent.state = 'done';   // it ran server-side
    const lines = [];
    lines.push(`${b.ran.length} capabilit${b.ran.length === 1 ? 'y' : 'ies'}: `
               + b.ran.slice(0, 3).join(', ')
               + (b.ran.length > 3 ? `, +${b.ran.length - 3} more` : ''));
    if (b.retried.length) {
      lines.push(`Retried: ${b.retried.join(', ')}`);
      agent.state = agent.state === 'failed' ? 'failed' : 'retrying';
    }
    if (b.failed.length) {
      lines.push(`Failed: ${b.failed.join(', ')}`);
      agent.state = 'failed';
    }
    agent.lines = lines;
  });

  // Errors the orchestrator recorded, verbatim.
  const errors = Array.isArray(trace.errors) ? trace.errors : [];
  errors.slice(0, 2).forEach((e) => {
    const text = e && (e.message || e.error || e.code);
    if (text) run.traceNotes.push(String(text));
  });

  emit();
}

/* ─── the run as the server sees it, WHILE it runs ───────────────────── */

/**
 * Everything above happens at the edges of an HTTP call: a dispatch leaves,
 * a dispatch returns, and the trace is read once it has. That is true and it
 * is silent for exactly the part that matters — a cold network solve takes
 * twenty to forty seconds, and for all of it the client knows only that it is
 * still waiting.
 *
 * The orchestrator knows more. Its `ExecutionContext` carries the state
 * machine's position, the outcome of every capability so far, and the
 * completed, failed and blocked step lists, and it mutates them as the run
 * proceeds. `GET /orchestrator/executions/live?correlation_id=…` reads them,
 * keyed on the id the api client puts on the request itself — so while a
 * request is in flight this polls the execution that request started and the
 * layers advance during the wait instead of all at once at the end.
 *
 * Strictly additive. Every failure mode here — no route, no execution yet, a
 * poll that errors — leaves the picture exactly as the client's own dispatch
 * record already drew it.
 */
const LIVE_POLL_MS = 900;

/** State machine position → what the orchestrator is doing, in its words. */
const LIVE_STATE = {
  RECEIVED: { state: 'active', headline: 'Request received' },
  UNDERSTANDING: { state: 'active', headline: 'Interpreting the request' },
  PLANNED: { state: 'active', headline: 'Plan built' },
  VALIDATING: { state: 'active', headline: 'Validating the plan' },
  RUNNING: { state: 'active', headline: 'Running the plan' },
  WAITING: { state: 'waiting', headline: 'Waiting' },
  COMPLETED: { state: 'done', headline: 'Complete' },
  FAILED: { state: 'failed', headline: 'Stopped' },
  INFEASIBLE: { state: 'failed', headline: 'No feasible solution' },
  REQUIRES_APPROVAL: { state: 'blocked', headline: 'Waiting for an approval' },
  REQUIRES_HUMAN: { state: 'blocked', headline: 'Waiting for a person' },
  CANCELLED: { state: 'failed', headline: 'Cancelled' },
  STALE: { state: 'blocked', headline: 'Superseded by a newer state' },
};

/** `AgentStatus` → what it means for the layer that owns the capability. */
const LIVE_CAPABILITY = {
  SUCCESS: 'done',
  PARTIAL: 'done',
  RETRYABLE_FAILURE: 'retrying',
  NON_RETRYABLE_FAILURE: 'failed',
  INVALID_OUTPUT: 'failed',
  INSUFFICIENT_EVIDENCE: 'failed',
};

/** Fold one live execution view into the picture. */
function absorbLive(executions) {
  if (!run || !Array.isArray(executions) || !executions.length) return;
  let changed = false;

  executions.forEach((ex) => {
    if (!ex || typeof ex !== 'object') return;

    // Where the orchestrator's own state machine is.
    const position = LIVE_STATE[String(ex.state || '').toUpperCase()];
    if (position && !run.endedAt) {
      run.agents[HUB].state = position.state;
      run.agents[HUB].headline = position.headline;
      changed = true;
    }

    // What it resolved the request to be, before any response has come back.
    if (ex.intent && run.agents.intent.state !== 'failed') {
      run.agents.intent.state = 'done';
      run.agents.intent.headline = 'Request understood';
      run.agents.intent.lines = [
        `Intent: ${String(ex.intent).replace(/_/g, ' ').toLowerCase()}`,
      ];
      changed = true;
    }

    // Capabilities the plan WILL use are waiting, not dark. A dark layer means
    // "not part of this request", and one the plan names plainly is.
    (ex.planned_capabilities || []).forEach((capability) => {
      const agent = run.agents[layerForCapability(capability)];
      if (agent && agent.state === 'idle') { agent.state = 'waiting'; changed = true; }
    });

    // Capabilities that have SETTLED, with the outcome the orchestrator
    // recorded for each. Nothing is claimed about the one currently running:
    // the status map holds outcomes, and inferring "so the next one must be
    // running now" would be a guess dressed as a reading.
    const settled = {};
    Object.entries(ex.capability_status || {}).forEach(([capability, status]) => {
      const layer = layerForCapability(capability);
      const outcome = LIVE_CAPABILITY[String(status).toUpperCase()];
      if (!outcome) return;
      const bucket = settled[layer] || (settled[layer] = { done: [], bad: [], state: null });
      if (outcome === 'done') bucket.done.push(capability);
      else bucket.bad.push(`${capability} (${String(status).toLowerCase().replace(/_/g, ' ')})`);
      // A failure outranks a success on the same layer; a retry outranks both
      // only while nothing has failed outright.
      if (outcome === 'failed') bucket.state = 'failed';
      else if (outcome === 'retrying' && bucket.state !== 'failed') bucket.state = 'retrying';
      else if (!bucket.state) bucket.state = 'done';
    });

    Object.entries(settled).forEach(([layer, bucket]) => {
      const agent = run.agents[layer];
      if (!agent) return;
      // A layer this client is still waiting on stays active: the dispatch has
      // not come back, whatever the capabilities inside it have done.
      const busy = run.steps.some((x) => x.layer === layer && x.status === 'active');
      if (bucket.state === 'failed') agent.state = 'failed';
      else if (bucket.state === 'retrying') agent.state = 'retrying';
      else if (!busy) agent.state = 'done';
      const lines = [];
      if (bucket.done.length) {
        lines.push(`${bucket.done.length} capabilit${bucket.done.length === 1 ? 'y' : 'ies'} `
                   + `complete: ${bucket.done.slice(0, 2).join(', ')}`
                   + (bucket.done.length > 2 ? `, +${bucket.done.length - 2} more` : ''));
      }
      if (bucket.bad.length) lines.push(bucket.bad.slice(0, 2).join('; '));
      if (lines.length) agent.lines = lines;
      changed = true;
    });

    // Steps the orchestrator itself marked blocked — a prerequisite that never
    // arrived. This is the one place Blocked comes from a server statement
    // rather than from the shape of the client's own plan.
    if ((ex.blocked_steps || []).length && !run.endedAt) {
      run.agents[HUB].state = 'blocked';
      run.agents[HUB].headline = `${ex.blocked_steps.length} step(s) blocked`;
      changed = true;
    }

    (ex.errors || []).slice(-1).forEach((line) => {
      const text = String(line || '');
      if (text && !run.traceNotes.includes(text)) { run.traceNotes.push(text); changed = true; }
    });
  });

  if (changed) emit();
}

/* Requests being watched, by the correlation id the api client put on them. */
const watching = new Map();
let observerAttached = false;

async function apiClient() {
  const mod = await import('./integration/api-client.js');
  return mod.apiClient || mod.default || null;
}

async function pollLive(correlationId) {
  const entry = watching.get(correlationId);
  if (!entry || entry.stopped || !run || run.endedAt) return stopWatching(correlationId);
  let body = null;
  try {
    const client = await apiClient();
    if (!client) return stopWatching(correlationId);
    body = await client.get('/orchestrator/executions/live',
                            { correlation_id: correlationId });
  } catch (e) {
    // No live view is not an error; it is simply less detail. Stop asking
    // rather than hammering a route that is not answering.
    return stopWatching(correlationId);
  }
  if (!watching.has(correlationId)) return;
  absorbLive(body && body.executions);
  const still = watching.get(correlationId);
  if (still && !still.stopped) still.timer = setTimeout(() => pollLive(correlationId), LIVE_POLL_MS);
}

function startWatching(correlationId) {
  if (!correlationId || !run || run.endedAt || watching.has(correlationId)) return;
  // The first poll is one interval away on purpose: a request that answers
  // quickly is never polled at all, and the screen costs nothing for it.
  watching.set(correlationId, {
    stopped: false,
    timer: setTimeout(() => pollLive(correlationId), LIVE_POLL_MS),
  });
}

function stopWatching(correlationId) {
  const entry = watching.get(correlationId);
  if (!entry) return;
  entry.stopped = true;
  if (entry.timer) clearTimeout(entry.timer);
  watching.delete(correlationId);
}

function stopAllWatching() {
  [...watching.keys()].forEach(stopWatching);
}

/**
 * Watch every request the application makes while a run is on screen.
 *
 * Attached once, lazily, and never detached — the observer does nothing at all
 * unless a run is in progress, and re-registering on each run would leak one
 * observer per run.
 */
function ensureRequestObserver() {
  if (observerAttached) return;
  observerAttached = true;
  import('./integration/api-client.js').then((mod) => {
    if (typeof mod.observeRequests !== 'function') return;
    mod.observeRequests((ev) => {
      if (!run || run.endedAt || !ev || !ev.correlationId) return;
      // Never watch the watcher: polling the live route would announce itself
      // and start a poll of the poll.
      if (String(ev.endpoint || '').indexOf('/orchestrator/executions') === 0) return;
      if (ev.phase === 'start') startWatching(ev.correlationId);
      else stopWatching(ev.correlationId);
    });
  }).catch(() => { /* no client, no live view */ });
}

/* ─── derived, never invented ────────────────────────────────────────── */

/**
 * Progress as a real fraction: dispatches finished over dispatches planned.
 *
 * Returns null when there is nothing to divide, and the view shows an
 * indeterminate bar rather than a number. A percentage counted from a timer is
 * exactly the defect this module exists to avoid.
 */
export function progress(snapshot) {
  const r = snapshot || getRun();
  if (!r || !r.steps.length) return null;
  const settled = r.steps.filter((s) => s.status === 'done' || s.status === 'failed').length;
  return { done: settled, total: r.steps.length,
           pct: Math.round((settled / r.steps.length) * 100) };
}

/** How many of the five layers this request has genuinely engaged. */
export function layersEngaged(snapshot) {
  const r = snapshot || getRun();
  if (!r) return { engaged: 0, total: LAYERS.length };
  const engaged = LAYERS.filter((id) => {
    const st = r.agents[id].state;
    return st !== 'idle';
  }).length;
  return { engaged, total: LAYERS.length };
}

if (typeof window !== 'undefined') {
  // Read-only handles for diagnostics and for the browser harness. Nothing
  // here can start or alter a run.
  window.__ngAgentRun = getRun;
  window.__ngAgentProgress = () => progress();
  window.__ngAgentEvents = () => [...AGENT_EVENTS];
  window.__ngAgentWatching = () => [...watching.keys()];
}
