/**
 * Netgravity — Workspace Chrome
 * =============================
 * The top bar the pre-app screens share: Select Project, Create Project and
 * the ingestion flow. One implementation rather than three, because the three
 * had drifted — Select Project carried a floating brand and a lone Back
 * button, Create Project carried the brand and nothing else, and the ingestion
 * screens carried a fourth, narrower bar with its own avatar handling.
 *
 * Everything in it does something. The bar shows a bell, a help control and an
 * account control, and each is wired to a real behaviour:
 *
 *   bell     → this session's activity, recorded by the code that performed it
 *   help     → what these screens expect of you, and where the data goes
 *   account  → Account security (password, second factor, sessions) · Sign out
 *
 * There is no notifications API in this build, so the bell shows the events
 * this session actually produced rather than an empty inbox pretending to be a
 * feed — and says so when there are none.
 */

import { getCurrentUser, initialsFor, clearCurrentUser } from './identity.js';
import { authService } from './integration/services/auth-service.js';
import { apiClient } from './integration/api-client.js';
import { setActiveProject } from './integration/project-context.js';
import { clearNetworkModel } from './data.js';

/* ─── Icons ──────────────────────────────────────────────────── */
export const CHROME_ICONS = {
  logo: `<svg class="wc-logo" viewBox="0 0 48 48" fill="none" aria-hidden="true">
      <line x1="10" y1="10" x2="10" y2="38" stroke="#9218EA" stroke-width="4.5" stroke-linecap="round"/>
      <line x1="38" y1="10" x2="38" y2="38" stroke="#9218EA" stroke-width="4.5" stroke-linecap="round"/>
      <line x1="12" y1="12" x2="36" y2="36" stroke="#9218EA" stroke-width="4" stroke-linecap="round"/>
      <circle cx="10" cy="10" r="5" fill="#fff" stroke="#9218EA" stroke-width="3"/>
      <circle cx="38" cy="10" r="5" fill="#fff" stroke="#9218EA" stroke-width="3"/>
      <circle cx="10" cy="38" r="5" fill="#fff" stroke="#9218EA" stroke-width="3"/>
      <circle cx="38" cy="38" r="5" fill="#fff" stroke="#9218EA" stroke-width="3"/>
    </svg>`,
  bell: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>`,
  help: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9.4"/><path d="M9.3 9.2a2.8 2.8 0 0 1 5.44.93c0 1.86-2.74 2.8-2.74 2.8"/><line x1="12" y1="17.1" x2="12" y2="17.11"/></svg>`,
  chevronDown: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>`,
  chevronLeft: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 6 9 12 15 18"/></svg>`,
  shield: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><polyline points="9 12 11 14 15 10"/></svg>`,
  signOut: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>`,
};

export function wcEscape(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* ═══════════════════════════════════════════════════════════════
   Session activity — what the bell shows
   ═══════════════════════════════════════════════════════════════ */

/**
 * Events this session actually performed, newest first.
 *
 * Deliberately not persisted and not fetched: nothing on the server records a
 * per-user activity feed, and a bell that silently showed an empty list would
 * read as "nothing has happened" rather than "this build does not keep a
 * history". `recordActivity` is called from the places that just did the thing.
 */
const ACTIVITY = [];
const ACTIVITY_LIMIT = 30;

export function recordActivity(text, tone = 'info') {
  if (!text) return;
  ACTIVITY.unshift({ text: String(text), tone, at: Date.now(), seen: false });
  ACTIVITY.length = Math.min(ACTIVITY.length, ACTIVITY_LIMIT);
  paintBellDot();
}

export function unseenActivityCount() {
  return ACTIVITY.filter((a) => !a.seen).length;
}

function relativeMoment(ms) {
  const seconds = Math.max(0, (Date.now() - ms) / 1000);
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hr ago`;
  return `${Math.floor(hours / 24)} d ago`;
}

function paintBellDot() {
  const count = unseenActivityCount();
  document.querySelectorAll('.wc-bell').forEach((btn) => {
    btn.classList.toggle('has-unseen', count > 0);
    btn.setAttribute('aria-label', count
      ? `Activity — ${count} new` : 'Activity');
  });
}

/* ═══════════════════════════════════════════════════════════════
   Shared modal
   ═══════════════════════════════════════════════════════════════ */

/**
 * A titled panel with a body. Used for help and for anything else that would
 * otherwise have been an `alert()`.
 */
export function showInfoPanel(title, bodyHtml) {
  document.getElementById('ng-info-panel')?.remove();

  const overlay = document.createElement('div');
  overlay.id = 'ng-info-panel';
  overlay.className = 'wc-modal-overlay';
  overlay.innerHTML = `
    <div class="wc-modal" role="dialog" aria-modal="true" aria-label="${wcEscape(title)}">
      <div class="wc-modal-head">
        <div class="wc-modal-title">${wcEscape(title)}</div>
        <button class="wc-modal-close" type="button" id="ng-info-panel-close"
                aria-label="Close">&#10005;</button>
      </div>
      <div class="wc-modal-body">${bodyHtml}</div>
    </div>`;

  const close = () => {
    overlay.remove();
    document.removeEventListener('keydown', onKey);
  };
  const onKey = (e) => { if (e.key === 'Escape') close(); };

  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  document.body.appendChild(overlay);
  overlay.querySelector('#ng-info-panel-close')?.addEventListener('click', close);
  document.addEventListener('keydown', onKey);
  overlay.querySelector('#ng-info-panel-close')?.focus();
  return close;
}

/* ═══════════════════════════════════════════════════════════════
   Sign out
   ═══════════════════════════════════════════════════════════════ */

/**
 * End the session everywhere it exists.
 *
 * One implementation, shared with the app shell's own profile menu. It revokes
 * the session server-side, drops the token, empties the network model and
 * clears the active project before returning to the landing page — a local
 * "sign out" that left any of those behind would sign the next person in as
 * the last one.
 */
export async function signOut() {
  try {
    await authService.logout();
  } catch (e) {
    // The local session goes regardless: a server that cannot be reached must
    // not leave the user apparently signed in.
    apiClient.setToken(null);
  }
  clearCurrentUser();
  clearNetworkModel();
  setActiveProject(null);
  ACTIVITY.length = 0;
  if (typeof window.returnToLanding === 'function') window.returnToLanding();
}

/* ═══════════════════════════════════════════════════════════════
   Top bar
   ═══════════════════════════════════════════════════════════════ */

/**
 * The shared top bar.
 *
 * `variant` only affects width: 'wide' spans the screen (Select Project),
 * 'default' matches the narrower content column of the other two.
 */
/**
 * The role as a label.
 *
 * The server stores it as a constant — `PLANNER`, `ADMIN` — and shouting it at
 * the user is a rendering of a database value, not a description of them. The
 * text is still the server's; only its case changes.
 */
function roleLabel(role) {
  const raw = String(role || '').trim();
  if (!raw) return '';
  return raw.replace(/[_-]+/g, ' ').toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function workspaceTopbarHtml({ variant = 'default' } = {}) {
  const user = getCurrentUser();
  const name = (user && (user.name || '').trim()) || (user && user.email) || '';
  const role = roleLabel(user && user.role);
  return `
    <header class="wc-topbar wc-topbar-${wcEscape(variant)}">
      <div class="wc-topbar-inner">
        <div class="wc-brand" data-wc="home" role="button" tabindex="0"
             title="Netgravity">
          ${CHROME_ICONS.logo}
          <div class="wc-brand-text">
            <div class="wc-brand-title">Netgravity</div>
            <div class="wc-brand-sub">by Kearney</div>
          </div>
        </div>
        <div class="wc-topbar-right">
          <button class="wc-icon-btn wc-bell" type="button" data-wc="bell"
                  aria-label="Activity">${CHROME_ICONS.bell}</button>
          <button class="wc-icon-btn" type="button" data-wc="help"
                  aria-label="Help">${CHROME_ICONS.help}</button>
          <span class="wc-divider" aria-hidden="true"></span>
          <button class="wc-account" type="button" data-wc="account"
                  aria-haspopup="menu" aria-expanded="false">
            <span class="wc-avatar user-avatar-ak"></span>
            <span class="wc-account-text">
              <span class="wc-account-name">${wcEscape(name)}</span>
              <span class="wc-account-role">${wcEscape(role)}</span>
            </span>
            <span class="wc-account-caret">${CHROME_ICONS.chevronDown}</span>
          </button>
        </div>
      </div>
    </header>`;
}

/** The help copy is per screen, so each caller supplies its own. */
const HELP_TEXT = {
  projects: `
    <p>A <strong>project</strong> is one logistics network and everything
      derived from it — the uploaded data, the solved baseline, the scenarios
      run against it and the insights written about it.</p>
    <ul>
      <li><strong>Open</strong> loads the project's own figures. Nothing is
        shared between projects.</li>
      <li><strong>Status</strong> is the server's: <em>Awaiting data</em> until
        a network has been ingested, <em>Analysis ready</em> afterwards.</li>
      <li><strong>Region / Scope</strong> is what you stated, or what was
        inferred from the coordinates in your upload — marked
        <em>(from data)</em> when it was inferred.</li>
    </ul>`,
  create: `
    <p>Only the <strong>project name</strong> is required. Both other fields can
      be filled in later.</p>
    <ul>
      <li><strong>Region / Scope</strong> left blank is inferred from the
        coordinates in the data you upload, which is better evidence than a
        dropdown. It sets the map, the currency defaults and the wording used
        across the app.</li>
      <li><strong>Client</strong> is free text and is only ever shown back to
        you.</li>
    </ul>
    <p>The project is created with no network bound. Uploading data is the next
      step and is what makes it analysable.</p>`,
  upload: `
    <p>Upload the workbook or CSVs describing your network. Every column is
      read, shown back to you, and mapped to a field this build understands
      before anything is committed.</p>
    <ul>
      <li><strong>Excel (.xlsx, .xls)</strong> — one sheet per table is ideal;
        sheets are identified by their columns, not their names.</li>
      <li><strong>CSV (.csv)</strong> — one table per file.</li>
      <li><strong>PDF (.pdf)</strong> — rate cards and contracts.</li>
      <li>Up to 25 MB per file.</li>
    </ul>
    <p><strong>Download template</strong> gives you a CSV per table, with the
      exact column headers the parser recognises.</p>
    <p>Nothing you upload reaches the optimiser until you have reviewed the
      mapping and confirmed it.</p>`,
};

/**
 * Wire the bar. Safe to call after every re-render — the screens rebuild their
 * own markup, so listeners are attached to the elements that exist now.
 */
export function bindWorkspaceTopbar(root = document, { help = 'projects' } = {}) {
  const host = root.querySelector ? root.querySelector('.wc-topbar') : null;
  if (!host) return;

  if (typeof window.applyIdentity === 'function') window.applyIdentity();
  paintBellDot();

  const closeMenus = () => {
    document.querySelectorAll('.wc-menu').forEach((m) => m.remove());
    host.querySelectorAll('[aria-expanded="true"]')
      .forEach((b) => b.setAttribute('aria-expanded', 'false'));
  };

  // One document-level dismisser, replaced rather than stacked, so repeated
  // renders do not leave a listener per render behind.
  if (window.__wcDismiss) document.removeEventListener('click', window.__wcDismiss);
  window.__wcDismiss = (e) => {
    if (e.target.closest('.wc-menu') || e.target.closest('[data-wc]')) return;
    closeMenus();
  };
  document.addEventListener('click', window.__wcDismiss);

  host.querySelector('[data-wc="home"]')?.addEventListener('click', () => {
    if (typeof window.showSelectProject === 'function') window.showSelectProject();
  });

  host.querySelector('[data-wc="help"]')?.addEventListener('click', () => {
    closeMenus();
    showInfoPanel('Help', HELP_TEXT[help] || HELP_TEXT.projects);
  });

  host.querySelector('[data-wc="bell"]')?.addEventListener('click', (e) => {
    const open = document.querySelector('.wc-menu-activity');
    closeMenus();
    if (open) return;
    openActivityMenu(e.currentTarget);
  });

  host.querySelector('[data-wc="account"]')?.addEventListener('click', (e) => {
    const open = document.querySelector('.wc-menu-account');
    closeMenus();
    if (open) return;
    openAccountMenu(e.currentTarget);
  });
}

/** Anchor a floating menu under a control, kept inside the viewport. */
function mountMenu(anchor, className, html) {
  const menu = document.createElement('div');
  menu.className = `wc-menu ${className}`;
  menu.setAttribute('role', 'menu');
  menu.innerHTML = html;
  document.body.appendChild(menu);

  const rect = anchor.getBoundingClientRect();
  const width = menu.offsetWidth;
  const left = Math.max(12, Math.min(rect.right - width, window.innerWidth - width - 12));
  menu.style.top = `${Math.round(rect.bottom + 8)}px`;
  menu.style.left = `${Math.round(left)}px`;
  anchor.setAttribute('aria-expanded', 'true');
  return menu;
}

function openActivityMenu(anchor) {
  const items = ACTIVITY.length
    ? ACTIVITY.map((a) => `
        <div class="wc-activity-row tone-${wcEscape(a.tone)}">
          <span class="wc-activity-dot"></span>
          <div>
            <div class="wc-activity-text">${wcEscape(a.text)}</div>
            <div class="wc-activity-when">${wcEscape(relativeMoment(a.at))}</div>
          </div>
        </div>`).join('')
    : `<div class="wc-menu-empty">
         Nothing yet in this session.<br>
         <span>Creating a project, uploading data and running an analysis are
         recorded here. This build keeps no history across sessions.</span>
       </div>`;

  mountMenu(anchor, 'wc-menu-activity', `
    <div class="wc-menu-head">Activity</div>
    <div class="wc-menu-scroll">${items}</div>`);

  ACTIVITY.forEach((a) => { a.seen = true; });
  paintBellDot();
}

function openAccountMenu(anchor) {
  const user = getCurrentUser();
  const name = (user && (user.name || '').trim()) || '';
  const email = (user && user.email) || '';
  const menu = mountMenu(anchor, 'wc-menu-account', `
    <div class="wc-menu-identity">
      <div class="wc-menu-identity-name">${wcEscape(name || email || 'Signed in')}</div>
      <div class="wc-menu-identity-mail">${wcEscape(email)}</div>
    </div>
    <button class="wc-menu-item" type="button" data-wc-menu="security" role="menuitem">
      ${CHROME_ICONS.shield}<span>Account security</span>
    </button>
    <button class="wc-menu-item danger" type="button" data-wc-menu="signout" role="menuitem">
      ${CHROME_ICONS.signOut}<span>Sign out</span>
    </button>`);

  menu.querySelector('[data-wc-menu="security"]')?.addEventListener('click', () => {
    menu.remove();
    anchor.setAttribute('aria-expanded', 'false');
    import('./account-security.js')
      .then((m) => m.openAccountSecurity())
      .catch(() => showInfoPanel('Account security',
        '<p>The account security screen could not be loaded.</p>'));
  });

  menu.querySelector('[data-wc-menu="signout"]')?.addEventListener('click', () => {
    menu.remove();
    anchor.setAttribute('aria-expanded', 'false');
    signOut();
  });
}

/* Read by initialsFor() consumers that render their own avatar markup. */
export { initialsFor };

if (typeof window !== 'undefined') {
  window.recordActivity = recordActivity;
  window.showInfoPanel = showInfoPanel;
  window.ngSignOut = signOut;
}
