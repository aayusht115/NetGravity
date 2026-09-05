/**
 * NetGravity — Suggested Analyses
 * ===============================
 * The next steps a planner can actually take, and what would sharpen the
 * answer they are looking at.
 *
 * Two different things, from two different places. The next steps are
 * predefined scenario templates prefilled from the solved network
 * (`scenario-templates.js`). The gaps are the briefing's own
 * `missing_information`, which never left the API before this.
 *
 * THE RULE: every suggestion rendered must be backed by something the system
 * can really do. A button that reads like an analysis and runs nothing is
 * worse than no button, because the user cannot tell. So what is offered here
 * is a `scenario-templates.js` entry — a predefined scenario, prefilled from
 * this network, that the builder can submit as it stands.
 *
 * Free text is deliberately gone. `suggested_questions` on the deterministic
 * path is two fixed strings, and rendering them as next steps handed the user
 * a sentence to translate into a form themselves.
 */

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/**
 * The card.
 *
 * @param templates          `availableTemplates()` output — predefined
 *                           scenarios, prefilled from this network. Each is
 *                           runnable; there is no free text here.
 * @param missingInformation briefing.missing_information (max 2)
 */
export function suggestedAnalysesHtml(templates, missingInformation) {
  const runnable = (templates || []).filter(t => t && t.id && t.action);
  const gaps = (missingInformation || []).filter(m => m && m.question);
  if (!runnable.length && !gaps.length) return '';

  return `
    <div class="sug-block">
      ${runnable.length ? `
        <div class="sug-title">What to test next</div>
        <div class="sug-list">
          ${runnable.map(t => `
            <div class="sug-item">
              <span class="sug-question">
                <span class="sug-template-label">${esc(t.label)}</span>
                ${t.why ? `<span class="sug-template-why">${esc(t.why)}</span>` : ''}
              </span>
              <button type="button" class="sug-action"
                      data-scenario-template="${esc(t.id)}">Set up</button>
            </div>`).join('')}
        </div>` : ''}

      ${gaps.length ? `
        <div class="sug-title">What would sharpen this</div>
        <ul class="sug-gaps">
          ${gaps.map(m => `
            <li class="sug-gap${m.blocking ? ' blocking' : ''}">
              <span class="sug-gap-q">${esc(m.question)}</span>
              ${m.impact ? `<span class="sug-gap-impact">${esc(m.impact)}</span>` : ''}
            </li>`).join('')}
        </ul>` : ''}
    </div>`;
}

/**
 * Wire the templates.
 *
 * `onRun(template)` opens the scenario builder PREFILLED with it. A screen
 * that cannot do that disables the buttons rather than offering a control
 * that does nothing.
 */
export function bindSuggestedAnalyses(root, onRun, templates = []) {
  if (!root) return;
  const byId = new Map((templates || []).map(t => [t.id, t]));
  root.querySelectorAll('[data-scenario-template]').forEach(btn => {
    if (btn.dataset.sugBound === '1') return;
    btn.dataset.sugBound = '1';
    const template = byId.get(btn.getAttribute('data-scenario-template'));
    if (typeof onRun !== 'function' || !template) {
      btn.disabled = true;
      btn.title = 'Not available from this screen.';
      return;
    }
    btn.addEventListener('click', () => onRun(template));
  });
}
