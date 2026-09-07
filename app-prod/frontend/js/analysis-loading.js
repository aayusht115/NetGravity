/**
 * NetGravity — the analysis loading screen
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
 * What this does now
 * ------------------
 * It is an adapter. The screen itself is the approved agent visualisation
 * (`agent-loading.js`), and what it shows comes from `agent-activity.js` —
 * the recorder that holds what the application genuinely dispatched, plus the
 * orchestrator's own execution trace for each response.
 *
 * This file's job is to translate hydration's five stages into that
 * recorder's vocabulary. The stage list is not a script for the animation: it
 * is `HYDRATION_STAGES`, the literal list of `await`s in
 * `hydrateFromBackend`, and each one is reported as it genuinely starts and
 * finishes. The percentage is completed stages over total stages — a real
 * fraction of a real list — and the elapsed clock is real seconds.
 *
 * The exported API is unchanged, so `projects.js` and `ingestion.js` did not
 * have to learn anything about agents to get the new screen.
 */

import { HYDRATION_STAGES } from './integration/hydrate.js';
import {
  startRun, stepStart, stepDone, stepFail, finishRun, isRunning, note,
} from './agent-activity.js';
import { mountAgentLoading, dismissAgentLoading } from './agent-loading.js';

/**
 * Which layer each hydration stage genuinely talks to.
 *
 * Every one of these is a real HTTP route that reaches the orchestrator, and
 * the layer is the one that owns the capability behind it:
 *
 *   structure  →  /api/network-structure   →  network.load_snapshot
 *   solve      →  /api/kpis/*              →  optimization.solve, kpi.summarise
 *   insights   →  /api/insights            →  reasoning.synthesise
 *   scenarios  →  /api/scenarios           →  scenario.*
 *   forecast   →  /api/forecast            →  forecast.demand
 *
 * `solve` is the orchestrator's own work rather than one of the five
 * specialist layers, because that is what it is: the MILP runs through the
 * capability executor, and the design names no optimisation layer on the ring.
 * Putting it at the centre says what happened; inventing a sixth satellite for
 * it would not.
 */
const STAGE_LAYER = {
  structure: 'extraction',
  solve: 'orchestrator',
  insights: 'reasoning',
  scenarios: 'scenario',
  forecast: 'forecasting',
};

/** What is being passed, in the user's words, on each hand-off. */
const STAGE_MESSAGE = {
  structure: 'Reading the facilities, lanes and markets in your upload',
  solve: 'Solving the network — this is the optimisation every figure comes from',
  insights: 'Asking for a briefing on what the solved network shows',
  scenarios: 'Collecting the scenarios already solved for this network',
  forecast: 'Projecting demand from the history in your upload',
};

let state = null;

function subtitleFor(alreadyComputed) {
  return alreadyComputed
    ? 'Reading the analysis already computed for this network.'
    : 'Our orchestrator coordinates specialised agents to analyse your data '
      + 'and generate the best possible insights.';
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
  mountAgentLoading();
  state = { projectName: projectName || 'this project', open: new Set() };

  startRun({
    title: `Analysing ${state.projectName}`,
    verb: 'analysing your data',
    subtitle: subtitleFor(alreadyComputed),
    // The plan is hydration's own list of awaits, in the order it makes them.
    plan: HYDRATION_STAGES.map(([id, label]) => ({
      id,
      layer: STAGE_LAYER[id] || 'orchestrator',
      label,
      message: STAGE_MESSAGE[id] || label,
    })),
  });
}

/**
 * Revise the wording once it is known whether this network was solved before.
 *
 * Exists so the overlay can be raised BEFORE that is known. It used to go up
 * only after `getReadiness()` returned, which left the dashboard visible with
 * no figures behind it for one HTTP round trip — a short window, but the exact
 * thing the loading screen is for.
 */
export function refineAnalysisLoading(alreadyComputed) {
  if (!state) return;
  note('orchestrator', [subtitleFor(alreadyComputed)]);
}

/** Mark one real stage started or finished. Called from hydration. */
export function reportAnalysisStage(id, status, detail = '', executionId = null) {
  if (!state) return;
  if (status === 'done') {
    if (!state.open.has(id)) stepStart(id);
    state.open.delete(id);
    stepDone(id, { detail, executionId });
  } else {
    state.open.add(id);
    stepStart(id);
  }
}

/**
 * Take the loading screen down.
 *
 * `failure` is shown for a moment before closing, so a project that could not
 * be analysed says why here rather than revealing an empty dashboard and
 * leaving the banner to explain it.
 */
export function endAnalysisLoading(failure = null) {
  if (!state) {
    dismissAgentLoading();
    return;
  }
  // Anything still open when hydration settled did not finish. It is recorded
  // as the failure it was rather than being quietly marked complete.
  state.open.forEach((id) => stepFail(id, { error: failure || 'did not complete' }));
  state.open.clear();
  finishRun({ error: failure });
  state = null;
  dismissAgentLoading(failure ? 2600 : 350);
}

export function isAnalysisLoading() {
  return state !== null && isRunning();
}

if (typeof window !== 'undefined') {
  window.beginAnalysisLoading = beginAnalysisLoading;
  window.endAnalysisLoading = endAnalysisLoading;
}
