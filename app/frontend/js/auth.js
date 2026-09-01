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

export function navigateToAuth(view) {
  if (typeof window.switchAuthPanel === 'function') {
    window.switchAuthPanel(view);
  }
}

/** Show an error inside the active auth panel, using its own visual language. */
function showAuthError(panelId, message) {
  const panel = document.getElementById(panelId);
  if (!panel) return;
  let box = panel.querySelector('.auth-error-box');
  if (!box) {
    box = document.createElement('div');
    box.className = 'auth-error-box';
    box.setAttribute('role', 'alert');
    box.style.cssText =
      'margin-top:12px;padding:10px 12px;border-radius:8px;'
      + 'background:var(--red-bg,#fef2f2);color:var(--red,#dc2626);'
      + 'border:1px solid #fecaca;font-size:12.5px;line-height:1.5;font-weight:600';
    const form = panel.querySelector('form');
    (form || panel).appendChild(box);
  }
  box.textContent = message;
  box.style.display = 'block';
}

function clearAuthError(panelId) {
  const box = document.getElementById(panelId)?.querySelector('.auth-error-box');
  if (box) box.style.display = 'none';
}

function setBusy(panelId, busy) {
  const btn = document.getElementById(panelId)?.querySelector('button[type=submit]');
  if (!btn) return;
  btn.disabled = busy;
  btn.style.opacity = busy ? '0.6' : '';
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

      if (!email) throw new Error('Enter your work email address.');
      // 12, matching the server. Checking 8 here meant a password the server
      // would refuse was accepted by the form and rejected on submit, with the
      // failure arriving as a generic error.
      if (!password || password.length < MIN_PASSWORD_LENGTH) {
        throw new Error(
          `Choose a password of at least ${MIN_PASSWORD_LENGTH} characters. `
          + 'A longer passphrase is stronger than a short complicated one.');
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
  let box = panel.querySelector('.auth-mfa-box');
  if (!box) {
    box = document.createElement('div');
    box.className = 'auth-mfa-box';
    panel.appendChild(box);
  }
  box.innerHTML = `
    <div class="auth-label" style="margin-top:12px">Two-factor code</div>
    <input type="text" id="signin-mfa-code" class="auth-input"
           inputmode="numeric" autocomplete="one-time-code" maxlength="9"
           placeholder="6-digit code, or a recovery code">
    <button type="button" class="btn-landing-primary" id="signin-mfa-submit"
            style="margin-top:10px;width:100%">Verify</button>
    <div class="text-xs text-muted" style="margin-top:8px">
      From your authenticator app. A recovery code also works and is then spent.
    </div>`;

  const codeInput = document.getElementById('signin-mfa-code');
  const submit = document.getElementById('signin-mfa-submit');
  codeInput?.focus();

  const verify = async () => {
    clearAuthError('panel-signin');
    setBusy('panel-signin', true);
    try {
      const session = await authService.completeMfa(mfaToken, codeInput.value.trim());
      box.remove();
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
    const confirmation = document.getElementById('panel-reset-confirmation');
    if (confirmation) {
      confirmation.style.display = 'block';
      confirmation.textContent =
        'If an account exists for that address, a reset link is on its way. '
        + 'It is valid for 30 minutes and can be used once.';
    }
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
  const box = document.createElement('div');
  box.className = 'auth-reset-complete';
  box.innerHTML = `
    <div class="auth-label" style="margin-top:12px">Choose a new password</div>
    <input type="password" id="reset-new-password" class="auth-input"
           autocomplete="new-password" placeholder="At least 12 characters">
    <button type="button" class="btn-landing-primary" id="reset-submit"
            style="margin-top:10px;width:100%">Set new password</button>
    <div class="text-xs text-muted" style="margin-top:8px">
      Every other session on this account will be signed out.
    </div>`;
  panel.appendChild(box);

  document.getElementById('reset-submit')?.addEventListener('click', async () => {
    clearAuthError('panel-reset');
    setBusy('panel-reset', true);
    try {
      const password = document.getElementById('reset-new-password')?.value || '';
      await authService.confirmPasswordReset(token, password);
      box.remove();
      const confirmation = document.getElementById('panel-reset-confirmation');
      if (confirmation) {
        confirmation.style.display = 'block';
        confirmation.textContent =
          'Password changed. Sign in with your new password.';
      }
      if (typeof window.switchAuthPanel === 'function') window.switchAuthPanel('signin');
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
