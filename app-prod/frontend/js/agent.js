/**
 * NetGravity — AI Decision Orchestrator UI
 * ==========================================
 * Agent activity trace, tool-call log, challenger analysis,
 * objective input, and simulated investigation flow.
 */

import { AGENT_STATE, SCENARIOS, formatCurrency } from './data.js';

// ─── Init ───────────────────────────────────────────────────
export function initAgent() {
  renderObjective();
  renderTrace();
  renderToolCalls();
  renderChallenger();
  wireAgentInput();
}

// ─── Objective ──────────────────────────────────────────────
function renderObjective() {
  const el = document.getElementById('agent-objective');
  if (el) el.textContent = `"${AGENT_STATE.currentObjective}"`;
}

// ─── Activity Trace ─────────────────────────────────────────
function renderTrace() {
  const container = document.getElementById('agent-trace');
  if (!container) return;

  container.innerHTML = AGENT_STATE.activityTrace.map(step => `
    <div class="trace-step ${step.status}">
      <div class="trace-action">${step.status === 'done' ? '✓' : '◌'} ${step.action}</div>
      <div class="trace-detail">${step.detail}</div>
    </div>
  `).join('');
}

// ─── Tool Calls ─────────────────────────────────────────────
function renderToolCalls() {
  const container = document.getElementById('tool-calls');
  if (!container) return;

  container.innerHTML = AGENT_STATE.toolCalls.map(tc => `
    <div class="tool-call">
      <span class="tool-name">${tc.tool}</span>
      <span class="tool-result">${tc.result}</span>
    </div>
  `).join('');
}

// ─── Challenger ─────────────────────────────────────────────
function renderChallenger() {
  const container = document.getElementById('challenger-results');
  if (!container) return;

  const scenariosWithStress = SCENARIOS.filter(s => s.stressTestResult);

  container.innerHTML = `
    <table class="ng-table">
      <thead>
        <tr>
          <th>Scenario</th>
          <th>+15% Demand</th>
          <th>Lane Disruption</th>
          <th>Verdict</th>
        </tr>
      </thead>
      <tbody>
        ${scenariosWithStress.map(s => {
          const st = s.stressTestResult;
          const demandResult = st.demandSurge15 || '—';
          const laneResult = st.laneDisruption || '—';
          const passed = demandResult.startsWith('PASS') && (laneResult.startsWith('PASS') || laneResult === '—');
          return `
            <tr>
              <td style="font-weight:600">${s.name}</td>
              <td><span class="tag ${demandResult.startsWith('PASS') ? 'tag-success' : 'tag-danger'}">${demandResult.split('—')[0].trim()}</span></td>
              <td><span class="tag ${laneResult.startsWith('PASS') ? 'tag-success' : 'tag-danger'}">${laneResult.split('—')[0].trim()}</span></td>
              <td><span class="tag ${passed ? 'tag-success' : 'tag-danger'}">${passed ? 'Robust' : 'Rejected'}</span></td>
            </tr>
          `;
        }).join('')}
      </tbody>
    </table>
    <div class="text-sm text-muted mt-md" style="line-height:1.6">
      <strong>Challenger conclusion:</strong> DC Consolidation (lowest cost at ${formatCurrency(SCENARIOS.find(s => s.id === 'SCN_CONSOLIDATE')?.totalCost || 0)}/day) was rejected because it fails under +15% demand surge. 
      Flow Rebalancing is the most robust option — it passes all stress tests while delivering 7.8% cost reduction.
    </div>
  `;
}

// ─── Agent Input ────────────────────────────────────────────
function wireAgentInput() {
  const input = document.getElementById('agent-input');
  const submit = document.getElementById('agent-submit');
  if (!input || !submit) return;

  submit.addEventListener('click', () => {
    const query = input.value.trim();
    if (!query) return;

    // Show investigating state
    submit.textContent = '🔄 Investigating...';
    submit.disabled = true;

    // Simulate agent processing
    simulateInvestigation(query, () => {
      submit.textContent = 'Investigate →';
      submit.disabled = false;
      input.value = '';
    });
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submit.click();
  });
}

// ─── Simulated Investigation ────────────────────────────────
function simulateInvestigation(query, onComplete) {
  const traceContainer = document.getElementById('agent-trace');
  if (!traceContainer) { onComplete(); return; }

  const steps = [
    { action: `Understood objective: "${query}"`, detail: 'Parsing business constraints and targets', delay: 800 },
    { action: 'Inspecting current network state', detail: 'Calling get_network_summary()', delay: 1200 },
    { action: 'Identifying relevant bottlenecks', detail: 'Calling get_bottlenecks()', delay: 1000 },
    { action: 'Generating candidate interventions', detail: 'Based on network analysis and objective', delay: 1500 },
    { action: 'Testing scenarios through MILP optimiser', detail: 'Calling run_scenario() for each candidate', delay: 2000 },
    { action: 'Stress-testing leading options', detail: 'Calling run_sensitivity() and run_resilience()', delay: 1800 },
    { action: 'Analysis complete — recommendation ready', detail: 'Navigate to Recommendation tab to review', delay: 500 },
  ];

  // Clear existing trace
  traceContainer.innerHTML = '';

  let stepIndex = 0;

  function addNextStep() {
    if (stepIndex >= steps.length) {
      onComplete();
      return;
    }

    const step = steps[stepIndex];
    const stepEl = document.createElement('div');
    stepEl.className = 'trace-step active';
    stepEl.innerHTML = `
      <div class="trace-action">◌ ${step.action}</div>
      <div class="trace-detail">${step.detail}</div>
    `;
    traceContainer.appendChild(stepEl);

    // Scroll to bottom
    stepEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

    setTimeout(() => {
      stepEl.className = 'trace-step done';
      stepEl.querySelector('.trace-action').textContent = '✓ ' + step.action;
      stepIndex++;
      addNextStep();
    }, step.delay);
  }

  addNextStep();
}
