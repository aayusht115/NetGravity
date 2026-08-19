/**
 * NetGravity — Scenario Workspace
 * =================================
 * Scenario comparison table, create scenario modal, mock MILP execution.
 */

import { SCENARIOS, formatCurrency, formatNumber } from './data.js';

// ─── State ──────────────────────────────────────────────────
let showFull = false;

// ─── Init ───────────────────────────────────────────────────
export function initScenarios() {
  renderComparisonTable();
  wireButtons();
}

// ─── Comparison Table ───────────────────────────────────────
function renderComparisonTable() {
  const container = document.getElementById('scenario-comparison');
  if (!container) return;

  const metrics = showFull ? [
    { key: 'totalCost',         label: 'Total Cost',         fmt: v => formatCurrency(v), best: 'low' },
    { key: 'transportCost',     label: 'Transport Cost',     fmt: v => formatCurrency(v), best: 'low' },
    { key: 'fixedCost',         label: 'Fixed Cost',         fmt: v => formatCurrency(v), best: 'low' },
    { key: 'inventoryCost',     label: 'Inventory Cost',     fmt: v => formatCurrency(v), best: 'low' },
    { key: 'sla',               label: 'SLA %',              fmt: v => v + '%', best: 'high' },
    { key: 'avgUtil',           label: 'Avg Utilisation %',  fmt: v => v + '%', best: 'balanced' },
    { key: 'maxUtil',           label: 'Peak Utilisation %', fmt: v => v + '%', best: 'low' },
    { key: 'carbonKg',          label: 'Carbon (kg CO₂)',    fmt: v => formatNumber(v), best: 'low' },
    { key: 'implementationCost',label: 'Implementation Cost',fmt: v => formatCurrency(v), best: 'low' },
    { key: 'resilience',        label: 'Resilience',         fmt: v => v, best: 'label' },
    { key: 'feasible',          label: 'Feasible',           fmt: v => v ? '✓' : '✕', best: 'bool' },
  ] : [
    { key: 'totalCost',  label: 'Total Cost',  fmt: v => formatCurrency(v), best: 'low' },
    { key: 'sla',        label: 'SLA %',       fmt: v => v + '%', best: 'high' },
    { key: 'maxUtil',    label: 'Peak Util %', fmt: v => v + '%', best: 'low' },
    { key: 'carbonKg',   label: 'Carbon (kg)', fmt: v => formatNumber(v), best: 'low' },
    { key: 'resilience', label: 'Resilience',  fmt: v => v, best: 'label' },
  ];

  const html = `
    <table class="ng-table scenario-table">
      <thead>
        <tr>
          <th style="text-align:left">Scenario</th>
          ${metrics.map(m => `<th>${m.label}</th>`).join('')}
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        ${SCENARIOS.map(s => {
          // Find best values for highlighting
          return `<tr>
            <td class="name-cell">
              <div style="font-weight:700">${s.name}</div>
              <div style="font-size:11px;color:var(--text-3);max-width:200px">${s.description}</div>
            </td>
            ${metrics.map(m => {
              const val = s[m.key];
              let cls = '';
              if (m.best === 'low') {
                const best = Math.min(...SCENARIOS.filter(x => x.feasible).map(x => x[m.key]));
                if (val === best && s.feasible) cls = 'scenario-winner';
              }
              if (m.best === 'high') {
                const best = Math.max(...SCENARIOS.filter(x => x.feasible).map(x => x[m.key]));
                if (val === best && s.feasible) cls = 'scenario-winner';
              }
              if (!s.feasible && s.sla < 95 && m.key === 'sla') cls = 'scenario-fail';
              return `<td class="${cls}">${m.fmt(val)}</td>`;
            }).join('')}
            <td>
              ${s.sla >= 95 || s.type === 'BASELINE' ? '<span class="tag tag-success">Pass</span>' : '<span class="tag tag-danger">SLA Fail</span>'}
            </td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>
  `;

  container.innerHTML = html;
}

// ─── Wire Buttons ───────────────────────────────────────────
function wireButtons() {
  // Full comparison toggle
  document.getElementById('btn-show-full')?.addEventListener('click', () => {
    showFull = !showFull;
    document.getElementById('btn-show-full').textContent = showFull ? 'Show Summary' : 'View Full Comparison';
    renderComparisonTable();
  });

  // Create scenario modal
  document.getElementById('btn-create-scenario')?.addEventListener('click', () => {
    document.getElementById('modal-create-scenario').classList.add('visible');
  });

  document.getElementById('modal-close-scenario')?.addEventListener('click', () => {
    document.getElementById('modal-create-scenario').classList.remove('visible');
  });

  document.getElementById('scn-cancel')?.addEventListener('click', () => {
    document.getElementById('modal-create-scenario').classList.remove('visible');
  });

  // Run scenario (mock)
  document.getElementById('scn-run')?.addEventListener('click', () => {
    const name = document.getElementById('scn-name')?.value || 'Custom Scenario';
    const type = document.getElementById('scn-type')?.value || 'CUSTOM';

    // Show loading state
    const btn = document.getElementById('scn-run');
    btn.textContent = '⏳ Running MILP Optimiser...';
    btn.disabled = true;

    // Simulate MILP execution time
    setTimeout(() => {
      btn.textContent = '✓ Scenario Evaluated';
      btn.style.background = 'var(--green)';

      // Add mock result notification
      setTimeout(() => {
        document.getElementById('modal-create-scenario').classList.remove('visible');
        btn.textContent = '▶ Run Through MILP Optimiser';
        btn.disabled = false;
        btn.style.background = '';

        // Show a temporary notification
        showNotification(`Scenario "${name}" evaluated. Results added to comparison.`);
      }, 1500);
    }, 2500);
  });
}

// ─── Notification ───────────────────────────────────────────
function showNotification(message) {
  const notif = document.createElement('div');
  notif.style.cssText = `
    position: fixed; bottom: 24px; right: 24px;
    background: #1a1a2e; color: white; padding: 14px 24px;
    border-radius: 10px; font-size: 13px; font-family: Inter, sans-serif;
    box-shadow: 0 8px 24px rgba(0,0,0,.2); z-index: 500;
    animation: fadeIn .3s ease;
  `;
  notif.textContent = message;
  document.body.appendChild(notif);
  setTimeout(() => {
    notif.style.opacity = '0';
    notif.style.transition = 'opacity .3s';
    setTimeout(() => notif.remove(), 300);
  }, 4000);
}
