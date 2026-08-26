/**
 * Netgravity Authentication Controller
 * =====================================
 * Single-page in-place authentication manager:
 * - Direct on-page Sign In, Create Account, and Password Reset
 * - Seamless transition to Home application
 * - Chatbot FAB display management
 */

export function navigateToAuth(view) {
  if (typeof window.switchAuthPanel === 'function') {
    window.switchAuthPanel(view);
  }
}

export function completeAuth() {
  // 1. Hide landing page
  const landing = document.getElementById('landing-page');
  if (landing) {
    landing.classList.add('hidden');
    landing.style.display = 'none';
  }

  // 2. Ensure app-shell is visible
  const appShell = document.querySelector('.app-shell');
  if (appShell) {
    appShell.style.display = 'flex';
  }

  // 3. Show floating chatbot FAB inside authenticated application
  const chatbotFab = document.getElementById('floating-chatbot-fab');
  if (chatbotFab) {
    chatbotFab.style.display = 'flex';
  }

  // 4. Navigate to Home Cockpit
  if (typeof window.navigateToTab === 'function') {
    window.navigateToTab('home');
  }

  // 5. Trigger resize and full render of Home components
  setTimeout(() => {
    if (typeof window.renderHome === 'function') {
      window.renderHome();
    }
    window.dispatchEvent(new Event('resize'));
  }, 60);
}

export function returnToLanding() {
  // 1. Hide app-shell
  const appShell = document.querySelector('.app-shell');
  if (appShell) {
    appShell.style.display = 'none';
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
    window.returnToLanding = returnToLanding;
  }
}
