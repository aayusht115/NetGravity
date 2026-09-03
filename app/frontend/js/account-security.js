/**
 * NetGravity — Account security
 * =============================
 * The screen behind the profile menu: change a password, enrol a second
 * factor, and see and revoke live sessions.
 *
 * Why this exists as a screen and not only as endpoints
 * -----------------------------------------------------
 * A second factor nobody can enrol in is not a second factor, and a session
 * list nobody can reach is not session management. The API for all three
 * landed with this module; without a way to use them they would have been
 * features on paper.
 *
 * What it deliberately does not do
 * --------------------------------
 * It never shows a secret twice. The TOTP secret and the recovery codes are
 * returned once, at enrolment, and are not readable back out of the
 * application afterwards — so this renders them at that moment, says they will
 * not be shown again, and means it.
 */

import { authService } from './integration/services/auth-service.js';
import { getCurrentUser } from './identity.js';
import {
  bindPasswordRequirements,
  passwordMeetsPolicy,
  passwordRejection,
  renderRequirementItems,
} from './auth.js';

/** Repaints the checklist after the fields are cleared; set by render(). */
let repaintRequirements = () => {};

const MODAL_ID = 'account-security-overlay';

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function relativeTime(epochSeconds) {
  if (!epochSeconds) return 'unknown';
  const seconds = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return new Date(epochSeconds * 1000).toLocaleString();
}

function overlay() {
  let el = document.getElementById(MODAL_ID);
  if (!el) {
    el = document.createElement('div');
    el.id = MODAL_ID;
    el.className = 'ing-loading-overlay';
    el.addEventListener('click', (e) => { if (e.target === el) close(); });
    document.body.appendChild(el);
  }
  return el;
}

function close() {
  const el = document.getElementById(MODAL_ID);
  if (el) { el.classList.remove('active'); el.innerHTML = ''; }
}

function notice(message, tone = 'info') {
  const host = document.getElementById('acct-notice');
  if (!host) return;
  const colour = tone === 'error' ? 'var(--red,#dc2626)'
    : tone === 'success' ? 'var(--green,#157f3c)' : 'var(--blue,#2563eb)';
  host.textContent = message;
  host.style.cssText =
    `margin:10px 0;padding:9px 12px;border-radius:8px;font-size:12.5px;`
    + `font-weight:600;color:${colour};border:1px solid ${colour}33;`
    + `background:${colour}12`;
}

async function render() {
  const user = getCurrentUser() || {};
  let mfa = { enrolled: false, confirmed: false, recovery_codes_left: 0 };
  let sessions = { sessions: [], total: 0 };
  try { mfa = await authService.mfaStatus(); } catch (e) { /* reported below */ }
  try { sessions = await authService.listSessions(); } catch (e) { /* ditto */ }

  overlay().innerHTML = `
    <div class="ing-loading-card" role="dialog" aria-label="Account security"
         style="max-width:640px">
      <div class="ing-loading-head">
        <span class="ing-sparkle-icon">&#128274;</span>
        <div class="ing-loading-title">Account security</div>
      </div>
      <div class="ing-loading-sub">
        ${escapeHtml(user.name || '')} &middot; ${escapeHtml(user.email || '')}
        &middot; role ${escapeHtml(user.role || '—')}
      </div>
      <div id="acct-notice"></div>

      <div class="ing-loading-row">
        <div class="ing-loading-row-left" style="flex-direction:column;align-items:flex-start">
          <div class="ing-loading-row-title">Change password</div>
          <div class="ing-loading-row-sub">
            Every other session is signed out.
          </div>
          <input type="password" id="acct-current" class="auth-input"
                 style="margin-top:8px" placeholder="Current password"
                 autocomplete="current-password">
          <input type="password" id="acct-new" class="auth-input"
                 style="margin-top:6px" placeholder="New password"
                 autocomplete="new-password">
          <input type="password" id="acct-new-confirm" class="auth-input"
                 style="margin-top:6px" placeholder="Confirm new password"
                 autocomplete="new-password">
          <!-- The same checklist the sign-up panel shows, driven by the same
               rules from js/auth.js. One password policy, stated one way
               wherever a password is chosen; the bare "At least 12
               characters" line this replaces said a third of it. -->
          <div class="auth-requirements-box" style="margin-top:8px;width:100%">
            <div class="auth-requirements-header">
              <span>Password must contain:</span>
            </div>
            <ul class="auth-requirements-list" id="acct-requirements-list">
              ${renderRequirementItems()}
            </ul>
          </div>
          <button type="button" class="btn btn-secondary btn-sm"
                  id="acct-change-password" style="margin-top:8px">
            Change password
          </button>
        </div>
      </div>

      <div class="ing-loading-row${mfa.confirmed ? '' : ' highlight'}">
        <div class="ing-loading-row-left" style="flex-direction:column;align-items:flex-start">
          <div class="ing-loading-row-title">
            Two-factor authentication
            ${mfa.confirmed ? '&nbsp;&#10003;' : ''}
          </div>
          <div class="ing-loading-row-sub" id="acct-mfa-state">
            ${mfa.confirmed
              ? `On. ${mfa.recovery_codes_left} recovery code(s) unused.`
              : mfa.enrolled
                ? 'Started but not confirmed — enter a code to finish.'
                : 'Off. A password alone is the only thing between an attacker '
                  + 'and this account.'}
          </div>
          <div id="acct-mfa-body" style="width:100%"></div>
          <div style="margin-top:8px;display:flex;gap:8px">
            ${mfa.confirmed
              ? `<input type="password" id="acct-mfa-password" class="auth-input"
                        style="max-width:220px" placeholder="Password to turn off">
                 <button type="button" class="btn btn-secondary btn-sm"
                         id="acct-mfa-disable">Turn off</button>`
              : `<button type="button" class="btn btn-primary btn-sm"
                         id="acct-mfa-enrol">Set up</button>`}
          </div>
        </div>
      </div>

      <div class="ing-loading-row">
        <div class="ing-loading-row-left" style="flex-direction:column;align-items:flex-start;width:100%">
          <div class="ing-loading-row-title">Sessions (${sessions.total || 0})</div>
          <div style="margin-top:6px;width:100%">
            ${(sessions.sessions || []).map((s) => `
              <div style="display:flex;justify-content:space-between;gap:12px;
                          padding:5px 0;font-size:12px;border-bottom:1px solid #eee">
                <span>${s.current ? '<strong>This device</strong>' : escapeHtml((s.client || 'unknown').slice(0, 46))}</span>
                <span style="color:#6b7280">last seen ${escapeHtml(relativeTime(s.last_seen_at))}</span>
              </div>`).join('') || '<div class="ing-loading-row-sub">No other sessions.</div>'}
          </div>
          <button type="button" class="btn btn-secondary btn-sm"
                  id="acct-revoke" style="margin-top:8px">
            Sign out everywhere else
          </button>
        </div>
      </div>

      <div style="margin-top:12px;text-align:right">
        <button type="button" class="btn btn-secondary btn-sm" id="acct-close">Close</button>
      </div>
    </div>`;

  document.getElementById('acct-close')?.addEventListener('click', close);
  document.getElementById('acct-change-password')?.addEventListener('click', changePassword);
  repaintRequirements = bindPasswordRequirements('acct-new', 'acct-requirements-list');
  document.getElementById('acct-mfa-enrol')?.addEventListener('click', beginEnrolment);
  document.getElementById('acct-mfa-disable')?.addEventListener('click', disableMfa);
  document.getElementById('acct-revoke')?.addEventListener('click', revokeOthers);
  overlay().classList.add('active');
}

async function changePassword() {
  const current = document.getElementById('acct-current')?.value || '';
  const next = document.getElementById('acct-new')?.value || '';
  const confirm = document.getElementById('acct-new-confirm')?.value || '';
  // The same two checks the sign-up panel makes, in the same words. This
  // screen used to send whatever was typed and relay the server's refusal,
  // so a typo in a password nobody can read came back as a failed request.
  if (!passwordMeetsPolicy(next)) {
    // "below": on this screen the checklist sits under the fields, and the
    // notice is at the top of the card.
    notice(passwordRejection('listed below'), 'error');
    return;
  }
  if (confirm !== next) {
    notice('The two passwords do not match. Re-enter them to continue.', 'error');
    return;
  }
  try {
    await authService.changePassword(current, next);
    notice('Password changed. Other sessions have been signed out.', 'success');
    document.getElementById('acct-current').value = '';
    document.getElementById('acct-new').value = '';
    document.getElementById('acct-new-confirm').value = '';
    repaintRequirements();
  } catch (err) {
    notice(err?.message || 'Could not change the password.', 'error');
  }
}

async function beginEnrolment() {
  try {
    const enrolment = await authService.beginMfaEnrolment();
    const body = document.getElementById('acct-mfa-body');
    body.innerHTML = `
      <div style="margin-top:10px;padding:10px;border:1px solid #e5e7eb;border-radius:8px">
        <div style="font-size:12px;font-weight:700;margin-bottom:4px">
          1. Add this to your authenticator app
        </div>
        <code style="font-size:11.5px;word-break:break-all">${escapeHtml(enrolment.secret)}</code>
        <div style="font-size:11.5px;color:#6b7280;margin-top:4px">
          Or paste this URI: <code style="word-break:break-all">${escapeHtml(enrolment.otpauth_uri)}</code>
        </div>

        <div style="font-size:12px;font-weight:700;margin:10px 0 4px">
          2. Save these recovery codes — they are not shown again
        </div>
        <div style="font-family:monospace;font-size:12px;columns:2">
          ${(enrolment.recovery_codes || []).map((c) => `<div>${escapeHtml(c)}</div>`).join('')}
        </div>

        <div style="font-size:12px;font-weight:700;margin:10px 0 4px">
          3. Confirm with a code
        </div>
        <div style="display:flex;gap:8px">
          <input type="text" id="acct-mfa-code" class="auth-input"
                 style="max-width:160px" inputmode="numeric" maxlength="6"
                 placeholder="6-digit code">
          <button type="button" class="btn btn-primary btn-sm" id="acct-mfa-confirm">
            Confirm
          </button>
        </div>
      </div>`;
    document.getElementById('acct-mfa-confirm')?.addEventListener('click', confirmEnrolment);
    notice('Scan or paste the secret, then confirm. It is not active until you do.',
           'info');
  } catch (err) {
    notice(err?.message || 'Could not start enrolment.', 'error');
  }
}

async function confirmEnrolment() {
  const code = document.getElementById('acct-mfa-code')?.value || '';
  try {
    await authService.confirmMfaEnrolment(code);
    notice('Two-factor authentication is on.', 'success');
    await render();
  } catch (err) {
    notice(err?.message || 'That code was not accepted.', 'error');
  }
}

async function disableMfa() {
  const password = document.getElementById('acct-mfa-password')?.value || '';
  try {
    await authService.disableMfa(password);
    notice('Two-factor authentication is off.', 'success');
    await render();
  } catch (err) {
    notice(err?.message || 'Could not turn it off.', 'error');
  }
}

async function revokeOthers() {
  try {
    const result = await authService.revokeOtherSessions();
    notice(`${result.revoked} other session(s) signed out.`, 'success');
    await render();
  } catch (err) {
    notice(err?.message || 'Could not revoke sessions.', 'error');
  }
}

export function openAccountSecurity() {
  render().catch((err) => {
    overlay().classList.remove('active');
    console.error('account security:', err);
  });
}

if (typeof window !== 'undefined') {
  window.openAccountSecurity = openAccountSecurity;
}
