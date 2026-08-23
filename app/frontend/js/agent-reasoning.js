/**
 * NetGravity — Autonomous Agent Reasoning Simulation Engine
 * ==========================================================
 * Interactive execution modal showing the multi-stage reasoning pipeline:
 * - Telemetry & Graph Ingestion
 * - Mathematical MILP Solver Formulation
 * - Multi-Agent Adversarial Challenger Debate
 * - Pareto Optimization & Prescriptive Synthesis
 */

const REASONING_STAGES = [
  {
    name: "Multi-Source Telemetry Ingestion & Graph Synthesis",
    desc: "Ingesting real-time data feeds across 19 network nodes, 24-month demand history, SAP WMS dispatch logs, and weather vulnerability matrices.",
    log: "[INGEST] 19 network facilities synced · Verified 14,200 active SKU flow vectors."
  },
  {
    name: "Mathematical Formulation & Mixed-Integer Linear Program (MILP)",
    desc: "Formulating multi-echelon cost minimization objective function subject to DC capacity ceilings, throughput constraints, and minimum SLA bounds (≥95%).",
    log: "[SOLVER] Google OR-Tools Simplex engine initialized · Primal-dual iterations converged in 320ms."
  },
  {
    name: "Multi-Agent Challenger Debate & Stress-Testing",
    desc: "Network Cost Optimizer Agent evaluated 4 topology candidates; Resilience Challenger Agent injected 14.2% festive surge stress to verify fault tolerance.",
    log: "[AGENTS] Cost Optimizer Agent & Risk Challenger completed adversarial debate: 0 SLA violations."
  },
  {
    name: "Pareto Optimal Frontier & Prescriptive Verdict",
    desc: "Synthesized final decision recommendation: Rebalance Baddi volume to Kolkata DC (-7.8% cost variance, 100% capacity relief, 96.7% SLA guaranteed).",
    log: "[SYNTHESIS] Decision synthesized successfully. Ready for executive simulation."
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
