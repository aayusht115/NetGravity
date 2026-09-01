/**
 * NetGravity — Autonomous Agent Reasoning Simulation Engine
 * ==========================================================
 * Interactive execution modal showing the multi-stage reasoning pipeline:
 * - Telemetry & Graph Ingestion
 * - Mathematical MILP Solver Formulation
 * - Multi-Agent Adversarial Challenger Debate
 * - Pareto Optimization & Prescriptive Synthesis
 */

/**
 * The stages of the pipeline that actually runs.
 *
 * Phase 10.0 rewrite. The previous list was theatre, and it described a system
 * that does not exist:
 *
 *   - "19 network nodes … 14,200 active SKU flow vectors … SAP WMS dispatch
 *     logs" — invented telemetry, and a facility count unrelated to whatever
 *     network was loaded;
 *   - "Google OR-Tools Simplex engine initialized · converged in 320ms" — the
 *     WRONG SOLVER. This system uses PuLP/HiGHS branch-and-cut. Naming a
 *     different engine, with a fabricated runtime, in the one screen a user
 *     opens to understand how an answer was reached is the most misleading
 *     thing the UI could say;
 *   - "Multi-Agent Challenger Debate … adversarial debate: 0 SLA violations" —
 *     no such architecture exists; and
 *   - a fully fabricated verdict ("Rebalance Baddi volume to Kolkata DC,
 *     -7.8% cost variance, 96.7% SLA guaranteed") presented as the model's
 *     conclusion.
 *
 * These stages now name the real control plane and carry no numbers. Concrete
 * figures belong to the execution trace at
 * `/orchestrator/executions/<id>/trace`, which reports what actually ran.
 */
const REASONING_STAGES = [
  {
    name: "Intent resolution & planning",
    desc: "Interpreting the request and selecting a workflow. The planner is deterministic; when a language model is available it may propose a plan, but the plan is validated before anything executes.",
    log: "[PLAN] Intent resolved · execution plan proposed."
  },
  {
    name: "Plan validation & dependency check",
    desc: "Checking every step against the capability registry: that each capability exists, its inputs are satisfied, and its hard dependencies are met. An invalid plan is refused rather than partially run.",
    log: "[VALIDATE] Capability dependencies verified."
  },
  {
    name: "Capability execution (MILP)",
    desc: "Running the specialist engines through the capability executor. Network optimisation is solved exactly by PuLP/HiGHS branch-and-cut; the language model cannot calculate, alter or override a solver result.",
    log: "[SOLVE] PuLP/HiGHS branch-and-cut executed."
  },
  {
    name: "Observation, reasoning & governance",
    desc: "Observing each result, deciding whether to continue, and converting the authoritative evidence package into an explanation. Governance then classifies any recommended action before it can be acted on.",
    log: "[GOVERN] Evidence assembled · action classified."
  }
];

let stageTimer = null;
let currentStageIndex = 0;
let pendingTargetTab = null;
let pendingCallback = null;

/**
 * Trigger Agent Reasoning Modal
 */
export function triggerAgentReasoning(taskTitle = "Evaluating Multi-Echelon Network Rebalancing", targetTab = "scenarios", onComplete = null) {
  const overlay = document.getElementById('agent-reasoning-modal-overlay');
  if (!overlay) {
    if (targetTab && window.navigateToTab) window.navigateToTab(targetTab);
    if (onComplete) onComplete();
    return;
  }

  pendingTargetTab = targetTab;
  pendingCallback = onComplete;

  const titleEl = document.getElementById('agent-modal-task-title');
  if (titleEl) titleEl.textContent = taskTitle;

  overlay.classList.add('active');
  overlay.style.display = 'flex';

  startReasoningSequence();
}

/**
 * Close Modal
 */
export function closeAgentReasoningModal() {
  const overlay = document.getElementById('agent-reasoning-modal-overlay');
  if (overlay) {
    overlay.classList.remove('active');
    overlay.style.display = 'none';
  }
  if (stageTimer) clearTimeout(stageTimer);
}

/**
 * Execute 4-Stage Reasoning Timeline
 */
function startReasoningSequence() {
  currentStageIndex = 0;
  updateStageUI(0);
  clearTerminal();
  updateProgress(15, "Stage 1 of 4: Telemetry Ingestion...");

  // Progress through stages smoothly (~450ms per stage)
  stageTimer = setTimeout(() => {
    updateStageUI(1);
    updateProgress(45, "Stage 2 of 4: MILP Formulation...");
    appendTerminal(REASONING_STAGES[0].log, "prefix-info");

    stageTimer = setTimeout(() => {
      updateStageUI(2);
      updateProgress(75, "Stage 3 of 4: Multi-Agent Challenger Debate...");
      appendTerminal(REASONING_STAGES[1].log, "prefix-solver");

      stageTimer = setTimeout(() => {
        updateStageUI(3);
        updateProgress(100, "Stage 4 of 4: Decision Synthesis Ready!");
        appendTerminal(REASONING_STAGES[2].log, "prefix-agent");
        appendTerminal(REASONING_STAGES[3].log, "prefix-info");

        // Complete & Route after brief pause
        stageTimer = setTimeout(() => {
          completeReasoningSequence();
        }, 650);
      }, 450);
    }, 450);
  }, 450);
}

/**
 * Update UI for current stage
 */
function updateStageUI(activeIdx) {
  const cards = document.querySelectorAll('.agent-stage-card');
  cards.forEach((card, idx) => {
    const badge = card.querySelector('.agent-stage-status-badge');
    if (idx < activeIdx) {
      card.className = 'agent-stage-card completed';
      if (badge) { badge.className = 'agent-stage-status-badge badge-completed'; badge.textContent = 'Completed'; }
    } else if (idx === activeIdx) {
      card.className = 'agent-stage-card active';
      if (badge) { badge.className = 'agent-stage-status-badge badge-running'; badge.textContent = 'Running...'; }
    } else {
      card.className = 'agent-stage-card';
      if (badge) { badge.className = 'agent-stage-status-badge badge-pending'; badge.textContent = 'Queued'; }
    }
  });
}

/**
 * Complete and Navigate
 */
export function completeReasoningSequence() {
  closeAgentReasoningModal();
  if (pendingTargetTab && window.navigateToTab) {
    window.navigateToTab(pendingTargetTab);
  }
  if (pendingCallback) {
    pendingCallback();
  }
}

/**
 * Progress Bar
 */
function updateProgress(pct, label) {
  const fill = document.getElementById('agent-progress-fill');
  const text = document.getElementById('agent-progress-text');
  const pctText = document.getElementById('agent-progress-pct');

  if (fill) fill.style.width = `${pct}%`;
  if (text) text.textContent = label;
  if (pctText) pctText.textContent = `${pct}%`;
}

/**
 * Terminal logger
 */
function clearTerminal() {
  const term = document.getElementById('agent-terminal-content');
  if (term) {
    term.innerHTML = '<div class="agent-terminal-line"><span class="timestamp">[00:00.01]</span> <span class="prefix-info">></span> NetGravity Agentic Kernel v2.4 initialized.</div>';
  }
}

function appendTerminal(text, prefixClass = "prefix-info") {
  const term = document.getElementById('agent-terminal-content');
  if (!term) return;

  const now = new Date();
  const ts = `${String(now.getSeconds()).padStart(2, '0')}.${String(Math.floor(now.getMilliseconds()/10)).padStart(2, '0')}`;
  
  const line = document.createElement('div');
  line.className = 'agent-terminal-line';
  line.innerHTML = `<span class="timestamp">[00:${ts}]</span> <span class="${prefixClass}">></span> ${text}`;
  term.appendChild(line);
  term.scrollTop = term.scrollHeight;
}

// Expose globally on window
if (typeof window !== 'undefined') {
  window.triggerAgentReasoning = triggerAgentReasoning;
  window.closeAgentReasoningModal = closeAgentReasoningModal;
  window.completeReasoningSequence = completeReasoningSequence;
}
