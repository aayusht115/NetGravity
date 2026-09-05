/**
 * Netgravity Authentication Controller
 * =====================================
 * Single-page in-place authentication manager:
 * - Direct on-page Sign In, Create Account, and Password Reset
 * - Hand-off to the project workspace screens (see projects.js)
 * - Chatbot FAB display management
 *
 * Phase 10.0. The prototype version read field ids that do not exist in the
 * markup (`panel-signin-email` rather than `signin-email`), so it always fell
 * back to a hardcoded address; and it wrapped both calls in `.catch(() => null)`
 * and continued into the app regardless, so a rejected login was
 * indistinguishable from a successful one. Credentials are now read from the
 * real fields, failures stop the flow, and the reason is shown on the panel.
 */

import { showSelectProject, showCreateProject, enterApp } from './projects.js';
import { setCurrentUser, loadIdentity } from './identity.js';
import { authService } from './integration/services/auth-service.js';

/** Matches the server's floor. Checking a lower one here only moves the
 *  rejection from the form to the request. */
const MIN_PASSWORD_LENGTH = 12;

/**
 * The only address domain this deployment creates accounts for.
 *
 * Enforced on the sign-up form, and deliberately NOT on sign-in or on a
 * password reset: an account can also arrive through single sign-on or an
 * administrator, and refusing to let such a user sign in — or recover — would
 * lock out someone the server is perfectly willing to authenticate. The
 * account-creation form is the one place where the rule decides anything.
 *
 * This is a client-side rule. `POST /api/auth/signup` still accepts any
 * syntactically valid address, so it is a guard on the form, not on the API.
 */
const ALLOWED_EMAIL_DOMAIN = 'kearney.com';

/**
 * The requirements the sign-up checklist shows, in the order it shows them.
 *
 * The server's floor is length plus a refusal of common passwords; the
 * composition rules below are the form's, and they are strictly stricter, so
 * nothing this form accepts can be rejected downstream for its shape. The one
 * arrangement that must never happen — and did — is the opposite: a form that
 * accepts what the server refuses, and reports the refusal as a generic error.
 */
const PASSWORD_RULES = [
  { key: 'length', test: (v) => v.length >= MIN_PASSWORD_LENGTH },
  { key: 'upper', test: (v) => /[A-Z]/.test(v) },
  { key: 'lower', test: (v) => /[a-z]/.test(v) },
  { key: 'digit', test: (v) => /[0-9]/.test(v) },
  { key: 'symbol', test: (v) => /[^A-Za-z0-9]/.test(v) },
];

/* The field and section marks the panels already use, so a step rendered
   after load is drawn from the same set rather than a lookalike. */
const LOCK_ICON =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
  + 'stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" '
  + 'height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>';
const SHIELD_ICON =
  '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" '
  + 'stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3'
  + '-8 3v7c0 6 8 10 8 10z"/><polyline points="9 12 11 14 15 10"/></svg>';
const KEY_ICON =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
  + 'stroke="currentColor" stroke-width="2"><path d="M21 2l-2 2m-7.61 7.61a5.5 '
  + '5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 '
  + '7l-3-3m-3.5 3.5L19 4"/></svg>';

/**
 * The single message every form gives for a password it will not send.
 *
 * `pointer` is the only thing that varies, because the checklist is not in
 * the same place relative to the message on every screen, and a message that
 * points the wrong way is worse than one that points nowhere.
 */
export function passwordRejection(pointer = 'above') {
  return `Password must be at least ${MIN_PASSWORD_LENGTH} characters and `
    + `meet all the requirements ${pointer}.`;
}

export const PASSWORD_REJECTION = passwordRejection('above');

/** The checklist's wording, in the order it is shown. */
export const PASSWORD_REQUIREMENT_LABELS = [
  ['length', `At least ${MIN_PASSWORD_LENGTH} characters`],
  ['upper', '1 uppercase letter'],
  ['lower', '1 lowercase letter'],
  ['digit', '1 number'],
  ['symbol', '1 special character'],
];

/**
 * The checklist's items, for a list rendered after load.
 *
 * The sign-up panel carries the same five in the markup so the rule is on the
 * page before any script runs; everywhere else builds them from here, so a
 * change to the policy cannot leave one screen stating the old one.
 */
export function renderRequirementItems() {
  return PASSWORD_REQUIREMENT_LABELS.map(([key, label]) => `
    <li class="auth-req-item" data-req="${key}">
      <span class="auth-req-mark" aria-hidden="true"></span>
      <span class="auth-req-text">${label}</span>
      <span class="auth-req-state">not met</span>
    </li>`).join('');
}

/** Which requirements the given value meets. */
export function evaluatePassword(value) {
  const v = String(value ?? '');
  const state = {};
  PASSWORD_RULES.forEach((rule) => { state[rule.key] = rule.test(v); });
  return state;
}

export function passwordMeetsPolicy(value) {
  const state = evaluatePassword(value);
  return PASSWORD_RULES.every((rule) => state[rule.key]);
}

export function emailDomainAccepted(email) {
  return String(email ?? '').trim().toLowerCase()
    .endsWith(`@${ALLOWED_EMAIL_DOMAIN}`);
}

const EYE_OPEN_ICON =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
  + 'stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8'
  + '-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>';
const EYE_SHUT_ICON =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
  + 'stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 '
  + '0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 '
  + '0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 '
  + '1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line></svg>';

/**
 * Wire the show/hide control on every password field under `root`.
 *
 * Exported so the panels in the markup and the steps rendered after load —
 * the second factor, setting a new password from a link — get the same
 * control from the same place rather than each growing its own.
 */
export function bindPasswordToggles(root = document) {
  root.querySelectorAll('.auth-password-toggle').forEach((btn) => {
    if (btn.dataset.toggleBound === '1') return;
    btn.dataset.toggleBound = '1';
    btn.setAttribute('aria-label', 'Show password');
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      const input = btn.closest('.auth-input-wrap')?.querySelector('input');
      if (!input) return;
      const reveal = input.type === 'password';
      input.type = reveal ? 'text' : 'password';
      btn.innerHTML = reveal ? EYE_SHUT_ICON : EYE_OPEN_ICON;
      btn.setAttribute('aria-label', reveal ? 'Hide password' : 'Show password');
    });
  });
}

/**
 * Drive a requirement checklist from a password field.
 *
 * Each `<li data-req="…">` in the list gains or loses `is-met`, and its
 * visually hidden state text follows, so the list is as readable through a
 * screen reader as it is on screen.
 */
export function bindPasswordRequirements(inputId, listId) {
  const input = document.getElementById(inputId);
  const list = document.getElementById(listId);
  if (!input || !list) return () => {};

  const items = new Map();
  list.querySelectorAll('[data-req]').forEach((el) => {
    items.set(el.getAttribute('data-req'), el);
  });

  const paint = () => {
    const state = evaluatePassword(input.value);
    items.forEach((el, key) => {
      const met = !!state[key];
      el.classList.toggle('is-met', met);
      const label = el.querySelector('.auth-req-state');
      if (label) label.textContent = met ? 'met' : 'not met';
    });
    return state;
  };

  ['input', 'change', 'paste'].forEach((evt) => {
    // `paste` fires before the value lands, so read it on the next frame.
    input.addEventListener(evt, () => (evt === 'paste' ? setTimeout(paint, 0) : paint()));
  });
  paint();
  return paint;
}

/**
 * Show an error inside the active auth panel.
 *
 * The slot is in the markup — see the note beside it in index.html. Creating
 * it here and appending it to the form put the message past the bottom of a
 * page that is a fixed 100vh with `overflow: hidden`, so a rejected sign-up
 * looked like a form that had simply done nothing.
 */
function showAuthError(panelId, message) {
  const panel = document.getElementById(panelId);
  if (!panel) return;
  let box = errorSlot(panel);
  if (!box) {
    box = document.createElement('div');
    box.className = 'auth-error-box';
    box.setAttribute('role', 'alert');
    const form = panel.querySelector('form');
    (form || panel).appendChild(box);
  }
  box.textContent = message;
  box.hidden = false;
}

function clearAuthError(panelId) {
  const panel = document.getElementById(panelId);
  if (!panel) return;
  panel.querySelectorAll('.auth-error-box').forEach((box) => { box.hidden = true; });
}

function setBusy(panelId, busy) {
  // `[data-auth-submit]` marks a button rendered into the panel after load —
  // the second-factor prompt, the set-new-password step — which is not a
  // `type=submit` because it is not inside a <form>. It is checked first: when
  // one of those steps is showing, the form that owns the submit button has
  // been hidden, and disabling a button nobody can see does nothing.
  const panel = document.getElementById(panelId);
  const btn = panel?.querySelector('[data-auth-submit]')
    || panel?.querySelector('button[type=submit]');
  if (!btn) return;
  btn.disabled = busy;
  btn.style.opacity = busy ? '0.6' : '';
}

/**
 * Reword a panel for a step it has moved on to, remembering what it said.
 *
 * A step rendered after load replaces the form the copy was written for, and
 * leaving "Enter your email and we'll send you a recovery link" above a
 * new-password field describes a form that is no longer there. The original
 * is stashed on the element so `restorePanelText` can put it back — the panel
 * is reused rather than rebuilt, so nothing else would.
 */
function setPanelText(el, text) {
  if (!el) return;
  if (el.dataset.defaultText === undefined) el.dataset.defaultText = el.textContent;
  el.textContent = text;
}

function setPanelSubtitle(panel, text) {
  setPanelText(panel?.querySelector('.auth-panel-subtitle'), text);
}

/** Undo every reword in the panel, whichever step made it. */
export function restorePanelText(panel) {
  panel?.querySelectorAll('[data-default-text]').forEach((el) => {
    el.textContent = el.dataset.defaultText;
  });
}

/**
 * The failure slot that is actually on screen.
 *
 * A panel can be showing a step rendered after load with the original form
 * hidden; the slot that ships inside that form goes down with it, so writing
 * the reason there would say nothing to anyone.
 */
function errorSlot(panel) {
  const boxes = Array.from(panel.querySelectorAll('.auth-error-box'));
  const onScreen = boxes.filter((box) => {
    for (let el = box.parentElement; el && el !== panel.parentElement;
         el = el.parentElement) {
      if (getComputedStyle(el).display === 'none') return false;
    }
    return true;
  });
  return onScreen[onScreen.length - 1] || boxes[boxes.length - 1] || null;
}

/**
 * Authentication succeeded. Neither path drops straight into the app —
 * a project has to be picked or created first:
 *
 *   'signup' (new user)      → Create Project  ("Create your first project")
 *   'signin' (existing user) → Select Project  (with a create option)
 */
export async function completeAuth(origin, credentials = {}) {
  const panelId = origin === 'signup' ? 'panel-signup' : 'panel-signin';
  clearAuthError(panelId);
  setBusy(panelId, true);

  try {
    if (origin === 'signup') {
      const name = credentials.name ?? document.getElementById('signup-name')?.value?.trim() ?? '';
      const email = credentials.email ?? document.getElementById('signup-email')?.value?.trim() ?? '';
      const password = credentials.password ?? document.getElementById('signup-password')?.value ?? '';
      const confirm = credentials.confirm
        ?? document.getElementById('signup-password-confirm')?.value ?? '';

      if (!email) throw new Error('Enter your work email address.');
      if (!emailDomainAccepted(email)) {
        throw new Error(
          `Use your ${ALLOWED_EMAIL_DOMAIN} work email address — accounts are `
          + `only created for ${ALLOWED_EMAIL_DOMAIN} addresses.`);
      }
      // One message for every shape of bad password, because the checklist
      // above the message already says which part is missing. Checking 8 here
      // meant a password the server would refuse was accepted by the form and
      // rejected on submit, with the failure arriving as a generic error.
      if (!passwordMeetsPolicy(password)) throw new Error(PASSWORD_REJECTION);
      if (confirm !== password) {
        throw new Error('The two passwords do not match. Re-enter them to continue.');
      }

      const created = await authService.signup({ name, email, password });
      // The identity comes from the server's record, not from the form: the
      // server normalises a blank name out of the email address, and showing
      // what was typed would disagree with what was stored.
      setCurrentUser((created && created.user) || null);
      await loadIdentity();
      showCreateProject('first');
    } else {
      const email = credentials.email ?? document.getElementById('signin-email')?.value?.trim() ?? '';
      const password = credentials.password ?? document.getElementById('signin-password')?.value ?? '';

      if (!email) throw new Error('Enter your email address.');
      if (!password) throw new Error('Enter your password.');

      const session = await authService.login(email, password);

      // A second factor turns sign-in into two steps. No session exists yet:
      // the server returned a short-lived challenge, and `resolve_session`
      // refuses it everywhere, so a client that ignored this would get nothing
      // usable rather than a way past the factor.
      if (session && session.mfa_required) {
        setBusy(panelId, false);
        promptForSecondFactor(session.mfa_token);
        return;
      }

      setCurrentUser((session && session.user) || null);
      await loadIdentity();
      showSelectProject();
    }
  } catch (err) {
    // The flow stops here. Previously it continued into the app on failure,
    // which made authentication decorative.
    const message = err?.code === 'UNAUTHENTICATED'
      ? 'Email or password is incorrect.'
      : (err?.message || 'Sign-in failed. Please try again.');
    showAuthError(panelId, message);
  } finally {
    setBusy(panelId, false);
  }
}

/** Direct hand-off to the app shell, bypassing project selection. */
export function completeAuthDirect() {
  enterApp();
}


// ---------------------------------------------------------------------------
// Second factor
// ---------------------------------------------------------------------------

/**
 * Ask for the six-digit code, on the sign-in panel itself.
 *
 * Rendered rather than shipped in the markup so a deployment with no MFA
 * enrolled never shows a field for it.
 */
function promptForSecondFactor(mfaToken) {
  const panel = document.getElementById('panel-signin');
  if (!panel) return;
  // The credentials have already been accepted, so the fields that carried
  // them step aside rather than stacking above the code. Appending below them
  // pushed the code field and its button past the bottom of a page that is a
  // fixed 100vh with overflow hidden — the step the user had to complete was
  // the one part of it not on the screen.
  const form = document.getElementById('form-panel-signin');
  if (form) form.style.display = 'none';
  setPanelSubtitle(panel, 'Confirm it is you with your second factor.');

  let box = panel.querySelector('.auth-mfa-box');
  if (!box) {
    box = document.createElement('div');
    box.className = 'auth-mfa-box';
    panel.appendChild(box);
  }
  box.innerHTML = `
    <div class="auth-field-group">
      <label class="auth-label" for="signin-mfa-code">Two-factor code</label>
      <div class="auth-input-wrap">
        <span class="auth-input-icon">${KEY_ICON}</span>
        <input type="text" id="signin-mfa-code" class="auth-input"
               inputmode="numeric" autocomplete="one-time-code" maxlength="9"
               placeholder="6-digit code, or a recovery code">
      </div>
    </div>
    <div class="auth-error-box" role="alert" hidden></div>
    <button type="button" class="btn-landing-primary" id="signin-mfa-submit"
            data-auth-submit>
      <span>Verify</span>
    </button>
    <button type="button" class="btn-landing-secondary" id="signin-mfa-cancel">
      Back to sign in
    </button>
    <div class="auth-panel-footer" style="margin-top:calc(14 * var(--u))">
      From your authenticator app. A recovery code also works and is then spent.
    </div>`;

  const codeInput = document.getElementById('signin-mfa-code');
  const submit = document.getElementById('signin-mfa-submit');
  codeInput?.focus();

  const dismiss = () => {
    box.remove();
    if (form) form.style.display = '';
    restorePanelText(panel);
  };

  document.getElementById('signin-mfa-cancel')?.addEventListener('click', () => {
    clearAuthError('panel-signin');
    dismiss();
  });

  const verify = async () => {
    clearAuthError('panel-signin');
    setBusy('panel-signin', true);
    try {
      const session = await authService.completeMfa(mfaToken, codeInput.value.trim());
      dismiss();
      setCurrentUser((session && session.user) || null);
      await loadIdentity();
      showSelectProject();
    } catch (err) {
      showAuthError('panel-signin',
        err?.message || 'That code was not accepted. Try the next one.');
    } finally {
      setBusy('panel-signin', false);
    }
  };
  submit?.addEventListener('click', verify);
  codeInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); verify(); }
  });
}


// ---------------------------------------------------------------------------
// Password reset
// ---------------------------------------------------------------------------

/**
 * Ask for a reset link.
 *
 * The panel says the same thing whatever happened, because the server does:
 * an unknown address, a rate-limited one and a real one are indistinguishable
 * by design, and a UI that revealed the difference would undo that.
 */
export async function requestPasswordReset(email) {
  const panelId = 'panel-reset';
  clearAuthError(panelId);
  setBusy(panelId, true);
  const address = email
    ?? document.getElementById('panel-reset-email')?.value?.trim() ?? '';
  try {
    if (!address) throw new Error('Enter the email address on your account.');
    await authService.requestPasswordReset(address);
    // Only now: the request form was previously hidden and the confirmation
    // shown by a second submit handler that ran before the call and never
    // looked at its result, so a rejected request still said "Check your
    // email". The form stays put until the server has actually accepted it.
    const form = document.getElementById('form-panel-reset');
    if (form) form.style.display = 'none';
    document.getElementById('panel-reset')?.classList.add('is-confirmed');
    const confirmation = document.getElementById('panel-reset-confirmation');
    if (confirmation) confirmation.style.display = 'flex';
    // The copy for this state is already in the markup, and says the same
    // thing whatever happened — the address is not echoed back, because the
    // server will not say whether an account exists for it.
  } catch (err) {
    showAuthError(panelId, err?.message || 'Could not start a password reset.');
  } finally {
    setBusy(panelId, false);
  }
}

/**
 * Complete a reset when the page was opened from a link.
 *
 * The token is removed from the URL as soon as it is read, so it does not sit
 * in the address bar, in history, or in a `Referer` header on the next request.
 */
export async function handleResetLink() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get('reset_token');
  if (!token) return false;

  window.history.replaceState({}, document.title, window.location.pathname);
  if (typeof window.switchAuthPanel === 'function') window.switchAuthPanel('reset');

  const panel = document.getElementById('panel-reset');
  if (!panel) return false;

  // The request form asks for an address; arriving from a link, the address
  // is already settled and the only thing left is the new password. Replacing
  // the form rather than appending below it keeps the panel inside the slot
  // reserved for it, on a page that cannot scroll.
  const requestForm = document.getElementById('form-panel-reset');
  if (requestForm) requestForm.style.display = 'none';
  setPanelSubtitle(panel, 'Choose the password you will sign in with.');

  const box = document.createElement('div');
  box.className = 'auth-reset-complete';
  // Same checklist and confirmation field as sign-up: one password policy,
  // stated the same way wherever a password is chosen.
  box.innerHTML = `
    <div class="auth-field-group">
      <label class="auth-label" for="reset-new-password">New password</label>
      <div class="auth-input-wrap">
        <span class="auth-input-icon">${LOCK_ICON}</span>
        <input type="password" id="reset-new-password" class="auth-input"
               autocomplete="new-password" placeholder="Choose a password">
        <button type="button" class="auth-password-toggle"
                title="Toggle password visibility">${EYE_OPEN_ICON}</button>
      </div>
    </div>
    <div class="auth-field-group">
      <label class="auth-label" for="reset-confirm-password">Confirm new password</label>
      <div class="auth-input-wrap">
        <span class="auth-input-icon">${LOCK_ICON}</span>
        <input type="password" id="reset-confirm-password" class="auth-input"
               autocomplete="new-password" placeholder="Re-enter your password">
        <button type="button" class="auth-password-toggle"
                title="Toggle password visibility">${EYE_OPEN_ICON}</button>
      </div>
    </div>
    <div class="auth-requirements-box">
      <div class="auth-requirements-header">
        ${SHIELD_ICON}
        <span>Password must contain:</span>
      </div>
      <ul class="auth-requirements-list" id="reset-requirements-list">
        ${renderRequirementItems()}
      </ul>
    </div>
    <div class="auth-error-box" role="alert" hidden></div>
    <button type="button" class="btn-landing-primary" id="reset-submit" data-auth-submit>
      <span>Set new password</span>
    </button>
    <button type="button" class="btn-landing-secondary" id="reset-cancel">
      Back to sign in
    </button>`;
  // The slot inside the box, not the panel's: the panel's lives in the request
  // form, which is hidden for the whole of this step.
  panel.appendChild(box);

  bindPasswordToggles(box);
  bindPasswordRequirements('reset-new-password', 'reset-requirements-list');

  document.getElementById('reset-cancel')?.addEventListener('click', () => {
    box.remove();
    if (requestForm) requestForm.style.display = 'block';
    restorePanelText(panel);
    if (typeof window.switchAuthPanel === 'function') window.switchAuthPanel('signin');
  });

  document.getElementById('reset-submit')?.addEventListener('click', async () => {
    clearAuthError('panel-reset');
    setBusy('panel-reset', true);
    try {
      const password = document.getElementById('reset-new-password')?.value || '';
      const confirm = document.getElementById('reset-confirm-password')?.value || '';
      if (!passwordMeetsPolicy(password)) throw new Error(PASSWORD_REJECTION);
      if (confirm !== password) {
        throw new Error('The two passwords do not match. Re-enter them to continue.');
      }
      await authService.confirmPasswordReset(token, password);
      box.remove();
      panel.classList.add('is-confirmed');
      const confirmation = document.getElementById('panel-reset-confirmation');
      if (confirmation) confirmation.style.display = 'flex';
      // The heading and the description element, never the box that holds
      // them: writing over the box removed the icon, the heading and the
      // button back to sign-in, which is how this screen lost its way out.
      // Both are stashed, so re-entering the panel restores the wording the
      // "check your email" state needs.
      setPanelText(confirmation?.querySelector('.auth-conf-title'),
                   'Password changed');
      setPanelText(document.getElementById('panel-reset-conf-desc'),
                   'Your password has been changed and every other session has '
                   + 'been signed out. Sign in with the new password.');
    } catch (err) {
      showAuthError('panel-reset',
        err?.message || 'That reset link is not valid any more.');
    } finally {
      setBusy('panel-reset', false);
    }
  });
  return true;
}

export function returnToLanding() {
  // 1. Hide app-shell and the project screens
  const appShell = document.querySelector('.app-shell');
  if (appShell) {
    appShell.style.display = 'none';
  }
  if (typeof window.hideProjectPages === 'function') {
    window.hideProjectPages();
  }
  if (typeof window.hideIngestionPages === 'function') {
    window.hideIngestionPages();
  }

  // 2. Hide floating chatbot FAB
  const chatbotFab = document.getElementById('floating-chatbot-fab');
  if (chatbotFab) {
    chatbotFab.style.display = 'none';
  }

  // 3. Show landing page
  const landing = document.getElementById('landing-page');
  if (landing) {
    landing.classList.remove('hidden');
    landing.style.display = 'flex';
  }

  // Reset to Sign In panel
  if (typeof window.switchAuthPanel === 'function') {
    window.switchAuthPanel('signin');
  }

  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/**
 * Show a single sign-on button, but only when one can work.
 *
 * `/api/auth/oidc/providers` answers whether a provider is configured for this
 * deployment. Nothing is rendered when it is not: a "Sign in with SSO" button
 * that leads to an error page is worse than no button, because it teaches a
 * user that the application is broken rather than that the feature is off.
 *
 * The whole thing is best-effort. An unreachable endpoint leaves the password
 * form exactly as it was.
 */
async function renderSsoOption() {
  const host = document.getElementById('auth-sso-option');
  if (!host) return;
  let info = null;
  try {
    const response = await fetch('/api/auth/oidc/providers',
                                 { credentials: 'same-origin' });
    if (!response.ok) return;
    info = await response.json();
  } catch (e) {
    return;
  }
  if (!info || !info.enabled || !(info.providers || []).length) return;

  const provider = info.providers[0];
  const label = String(provider.name || 'Single sign-on');
  const safe = label.replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  host.innerHTML = `
    <div class="auth-sso-divider"><span>or</span></div>
    <a class="btn-landing-secondary auth-sso-btn" id="auth-sso-start"
       href="/api/auth/oidc/start">
      <span>Continue with ${safe}</span>
    </a>`;
  host.hidden = false;

  // Surface the reason a redirect came back rejected. The reason is one of a
  // fixed set of tokens chosen by `api/oidc.py`, never provider output — a
  // provider's error body can contain the authorization code.
  const reason = new URLSearchParams(window.location.search).get('sso_error');
  if (reason) {
    const explained = {
      sso_not_configured: 'Single sign-on is not configured for this deployment.',
      provider_unavailable: 'The identity provider could not be reached.',
      provider_declined: 'The identity provider declined the sign-in.',
      malformed_callback: 'The sign-in response was incomplete.',
      unknown_or_used_state: 'That sign-in link has already been used or has expired. Try again.',
      expired_state: 'The sign-in took too long. Try again.',
      token_verification_failed: 'The identity token could not be verified.',
      email_not_usable: 'Your provider did not supply a verified e-mail address this deployment accepts.',
      no_account_and_provisioning_disabled:
        'You were signed in successfully, but no NetGravity account exists for you and this deployment does not create them automatically. Ask an administrator to invite you.',
    }[reason] || 'Single sign-on did not complete.';
    const note = document.createElement('div');
    note.className = 'auth-sso-error';
    note.textContent = explained;
    host.appendChild(note);
  }
}

export function initAuth() {
  renderSsoOption();

  // The markup ships with a placeholder value of literal bullet characters in
  // the password field, which was fine when no password was ever checked. It
  // would now be submitted verbatim, so it is cleared here rather than by
  // editing the approved markup.
  const pw = document.getElementById('signin-password');
  if (pw && /^[••]+$/.test(pw.value)) pw.value = '';

  bindPasswordRequirements('signup-password', 'signup-requirements-list');

  // A message that survived the correction it asked for would keep telling
  // the user about a password they have already replaced.
  [['panel-signup', ['signup-name', 'signup-email', 'signup-password',
                     'signup-password-confirm']],
   ['panel-signin', ['signin-email', 'signin-password']],
   ['panel-reset', ['panel-reset-email']],
  ].forEach(([panelId, fieldIds]) => {
    fieldIds.forEach((fieldId) => {
      document.getElementById(fieldId)
        ?.addEventListener('input', () => clearAuthError(panelId));
    });
  });

  if (typeof window !== 'undefined') {
    window.navigateToAuth = navigateToAuth;
    window.completeAuth = completeAuth;
    window.completeAuthDirect = completeAuthDirect;
    window.returnToLanding = returnToLanding;
  }
}


if (typeof window !== 'undefined') {
  window.requestPasswordReset = requestPasswordReset;
  window.handleResetLink = handleResetLink;
}
