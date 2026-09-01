/**
 * NetGravity — Signed-in identity
 * ===============================
 * One owner for "who is using this application", written into every place the
 * markup shows a person.
 *
 * The application shipped with a single hard-coded user, spelled out in six
 * different places:
 *
 *   index.html   `<strong id="logged-in-user-name">Amit Kumar</strong>`
 *   index.html   `<div class="user-avatar-ak">AK</div>`
 *   index.html   `title="User Profile: Amit Kumar"`
 *   index.html   `<h4 class="chatbot-welcome-greeting">Hi Amit! 👋</h4>`
 *   app.js       alert('… Logged in as: Amit Kumar\nRole: Lead Supply Chain
 *                Architect (Admin)\nOrganization: Kearney Decision Systems')
 *   ingestion.js `<div class="user-avatar-ak" title="Amit Kumar">AK</div>`
 *
 * None of it moved when somebody else signed in. A planner who had just created
 * an account was greeted by name as a different person, and the profile menu
 * reported a role and an organisation neither they nor the server had ever
 * stated.
 *
 * The account is the single source. `/api/auth/me` returns the name, email,
 * role and organisation held against the session; nothing here invents a
 * fallback for a field the server did not send.
 */

import { authService } from './integration/services/auth-service.js';

/** The signed-in user, or null before a session is established. */
let currentUser = null;

/** Everything that shows a person. Kept here so nothing is missed twice. */
const SELECTORS = {
  greetingName: '#logged-in-user-name',
  profileButton: '#btn-topbar-profile',
  chatGreeting: '.chatbot-welcome-greeting',
  avatars: '.user-avatar-ak',
};

export function getCurrentUser() {
  return currentUser;
}

/**
 * Initials for the avatar chip.
 *
 * Two letters from a two-part name, otherwise the first two of a single word.
 * Falls back to the email's local part so an account created with no name
 * still gets something recognisable rather than the prototype's "AK".
 */
export function initialsFor(user) {
  const source = (user && (user.name || '').trim())
    || (user && (user.email || '').split('@')[0].replace(/[._-]+/g, ' '))
    || '';
  const parts = source.split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

/** The name to greet someone by: their given name, or their full name. */
export function firstNameOf(user) {
  const name = (user && (user.name || '').trim()) || '';
  if (name) return name.split(/\s+/)[0];
  const email = (user && user.email) || '';
  const local = email.split('@')[0] || '';
  const guess = local.split(/[._-]/)[0];
  return guess ? guess.charAt(0).toUpperCase() + guess.slice(1) : 'there';
}

/**
 * Write the current user into the DOM.
 *
 * Idempotent and safe to call before an element exists — every write is
 * guarded, because the chatbot markup and the ingestion header mount at
 * different times.
 */
export function applyIdentity() {
  const user = currentUser;
  if (!user) return;

  const name = (user.name || '').trim() || user.email || 'Signed in';
  const initials = initialsFor(user);

  const greeting = document.querySelector(SELECTORS.greetingName);
  if (greeting) greeting.textContent = name;

  document.querySelectorAll(SELECTORS.avatars).forEach((el) => {
    el.textContent = initials;
    el.setAttribute('title', name);
  });

  const profileBtn = document.querySelector(SELECTORS.profileButton);
  if (profileBtn) profileBtn.title = `User profile: ${name}`;

  const chatGreeting = document.querySelector(SELECTORS.chatGreeting);
  if (chatGreeting) chatGreeting.textContent = `Hi ${firstNameOf(user)}! 👋`;
}

/**
 * Record the signed-in user and write them onto the screen.
 *
 * Called after login, after signup, and after a session is restored on page
 * load. The `identityChanged` event lets a screen that renders later — the
 * ingestion header, the chatbot modal — pick the user up without this module
 * needing to know it exists.
 */
export function setCurrentUser(user) {
  currentUser = user || null;
  applyIdentity();
  if (typeof window !== 'undefined') {
    window.__ngCurrentUser = currentUser;
    window.dispatchEvent(new CustomEvent('identityChanged', { detail: currentUser }));
  }
}

/** Clear the identity on sign-out, so the next user does not see the last one. */
export function clearCurrentUser() {
  setCurrentUser(null);
  const greeting = document.querySelector(SELECTORS.greetingName);
  if (greeting) greeting.textContent = '';
  document.querySelectorAll(SELECTORS.avatars).forEach((el) => {
    el.textContent = '';
    el.removeAttribute('title');
  });
}

/**
 * Fetch the signed-in user from the server and apply them.
 *
 * Returns the user, or null when there is no valid session. The server is the
 * authority — the client never assembles an identity from what it typed into
 * the signup form, so a name normalised or defaulted server-side is the one
 * shown.
 */
export async function loadIdentity() {
  try {
    const res = await authService.getCurrentUser();
    const user = (res && (res.user || res)) || null;
    if (user && (user.email || user.id)) {
      setCurrentUser(user);
      return user;
    }
  } catch (e) {
    /* No session. The caller decides what to do about it. */
  }
  setCurrentUser(null);
  return null;
}

if (typeof window !== 'undefined') {
  window.applyIdentity = applyIdentity;
  window.getCurrentUser = getCurrentUser;
  // The chatbot modal and the ingestion header render after boot; both re-apply
  // on this event rather than each fetching the user for themselves.
  window.addEventListener('identityChanged', () => {
    // Give any listener that renders in response a tick to mount first.
    setTimeout(applyIdentity, 0);
  });
}
