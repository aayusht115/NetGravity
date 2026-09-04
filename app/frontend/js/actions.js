/**
 * NetGravity — Declarative click actions
 * ======================================
 * One delegated listener that dispatches `data-action` attributes through an
 * explicit allowlist.
 *
 * What this replaces
 * ------------------
 * Thirty-five inline `onclick="…"` attributes. Those are script, and a Content
 * Security Policy that permits them has to permit ALL inline script — which is
 * to say, it has to permit exactly the thing it exists to prevent. Nonces do
 * not help: they apply to `<script>` elements, not to event-handler attributes.
 *
 * So this is not a stylistic change. `script-src 'self'` is only enforceable
 * because no handler is written in markup any more.
 *
 * Why an allowlist rather than a lookup
 * ------------------------------------
 * `window[el.dataset.action]()` would let any attribute value reach any global
 * — including, on a page that has an injection, one the attacker chose. ACTIONS
 * below is the complete set of things a click may do, and anything else is
 * logged and ignored.
 *
 * Usage in markup:
 *     data-action="askChatbotPrompt" data-arg="Which DC is most utilised?"
 *     data-action="closeChatbotModal" data-self-only="1"   (backdrop clicks)
 *     data-action="preventDefault"
 */

/** Call a global by name, if the module that owns it has loaded. */
function callGlobal(name, arg) {
  const fn = window[name];
  if (typeof fn !== 'function') return;
  if (arg === undefined || arg === null || arg === '') fn();
  else fn(arg);
}

/**
 * Every action a click may perform. Adding a behaviour to the page means
 * adding it here, which is the point: the set is reviewable in one place.
 */
const ACTIONS = {
  // Landing / auth
  switchAuthPanel: (arg) => callGlobal('switchAuthPanel', arg),
  navigateToAuth: (arg) => callGlobal('navigateToAuth', arg),
  completeAuth: (arg) => callGlobal('completeAuth', arg),
  returnToLanding: () => callGlobal('returnToLanding'),

  // Navigation
  navigateToTab: (arg) => callGlobal('navigateToTab', arg),
  showCreateProject: (arg) => callGlobal('showCreateProject', arg),
  showSelectProject: () => callGlobal('showSelectProject'),

  // Assistant
  openChatbotModal: () => callGlobal('openChatbotModal'),
  closeChatbotModal: () => callGlobal('closeChatbotModal'),
  resetChatbotView: () => callGlobal('resetChatbotView'),
  sendChatbotInput: () => callGlobal('sendChatbotInput'),
  askChatbotPrompt: (arg) => callGlobal('askChatbotPrompt', arg),

  // Panels and drawers
  closeActionDrawer: () => callGlobal('closeActionDrawer'),
  closeFacilityPanel: () => {
    document.getElementById('facility-panel-overlay')?.classList.remove('active');
  },
  exportFacilityReport: () => callGlobal('exportFacilityReport'),

  /**
   * The assistant's "Explore in Digital Twin" button.
   *
   * It navigates. It used to spend two seconds first, in a modal that
   * advanced four "reasoning stages" on 450ms timers, filled a progress bar
   * to 100%, and printed lines like "NetGravity Agentic Kernel v2.4
   * initialized" into a fake terminal — in front of no work whatsoever, since
   * the only thing waiting at the end of it was a tab change. Removed with
   * the rest of that modal.
   *
   * The topic is read from a dataset attribute rather than interpolated into
   * an inline handler: chat content can carry text from an uploaded file, and
   * that must never reach an executable position.
   */
  exploreInTwin: (tab) => {
    callGlobal('closeChatbotModal');
    callGlobal('navigateToTab', tab);
  },

  // Generic
  preventDefault: () => { /* handled below, before dispatch */ },
  closeSelf: (_arg, el) => el.classList.remove('visible'),
};

function handleClick(event) {
  const el = event.target.closest('[data-action]');
  if (!el) return;

  // A backdrop closes only when the backdrop itself was clicked, not when a
  // click inside the dialog bubbles out to it.
  if (el.dataset.selfOnly === '1' && event.target !== el) return;

  const action = ACTIONS[el.dataset.action];
  if (!action) {
    console.warn('Unknown data-action:', el.dataset.action);
    return;
  }
  if (el.dataset.action === 'preventDefault' || el.dataset.preventDefault === '1') {
    event.preventDefault();
  }
  action(el.dataset.arg, el, event);
}

export function initActions() {
  document.addEventListener('click', handleClick);
}

if (typeof window !== 'undefined') {
  window.initActions = initActions;
}
