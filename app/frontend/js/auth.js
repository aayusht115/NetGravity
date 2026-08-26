/**
 * Netgravity Authentication Controller
 * =====================================
 * Single-page in-place authentication manager:
 * - Direct on-page Sign In, Create Account, and Password Reset
 * - Hand-off to the project workspace screens (see projects.js)
 * - Chatbot FAB display management
 */

import { showSelectProject, showCreateProject, enterApp } from './projects.js';

export function navigateToAuth(view) {
  if (typeof window.switchAuthPanel === 'function') {
    window.switchAuthPanel(view);
  }
}

/**
 * Authentication succeeded. Neither path drops straight into the app —
 * a project has to be picked or created first:
 *
 *   'signup' (new user)      → Create Project  ("Create your first project")
 *   'signin' (existing user) → Select Project  (with a create option)
 */
export function completeAuth(origin) {
  if (origin === 'signup') {
    showCreateProject('first');
  } else {
    showSelectProject();
  }
}

/** Direct hand-off to the app shell, bypassing project selection. */
export function completeAuthDirect() {
  enterApp();
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

export function initAuth() {
  // Expose globally on window
  if (typeof window !== 'undefined') {
    window.navigateToAuth = navigateToAuth;
    window.completeAuth = completeAuth;
    window.completeAuthDirect = completeAuthDirect;
    window.returnToLanding = returnToLanding;
  }
}
