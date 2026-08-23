/**
 * Netgravity Authentication Controller
 * Handles 3 distinct views: Sign In, Create New Account, Reset Password
 */

export function navigateToAuth(view) {
  // 1. Hide landing page
  const landing = document.getElementById('landing-page');
  if (landing) {
    landing.style.display = 'none';
    landing.classList.add('hidden');
  }

  // 2. Hide all auth shells first
  const signinShell = document.getElementById('auth-page-signin');
  const signupShell = document.getElementById('auth-page-signup');
  const resetShell = document.getElementById('auth-page-reset');

  if (signinShell) { signinShell.classList.remove('active'); signinShell.style.display = 'none'; }
  if (signupShell) { signupShell.classList.remove('active'); signupShell.style.display = 'none'; }
  if (resetShell) { resetShell.classList.remove('active'); resetShell.style.display = 'none'; }

  // 3. Activate requested view
  if (view === 'signin') {
    if (signinShell) {
      signinShell.classList.add('active');
      signinShell.style.display = 'flex';
    }
  } else if (view === 'signup') {
    if (signupShell) {
      signupShell.classList.add('active');
      signupShell.style.display = 'flex';
    }
  } else if (view === 'reset') {
    if (resetShell) {
      resetShell.classList.add('active');
      resetShell.style.display = 'flex';
    }
  }

  window.scrollTo({ top: 0, behavior: 'smooth' });
}

export function completeAuth() {
  // 1. Hide all auth shells
  document.querySelectorAll('.auth-page-shell').forEach(shell => {
    shell.classList.remove('active');
    shell.style.display = 'none';
  });

  // 2. Hide landing page
  const landing = document.getElementById('landing-page');
  if (landing) {
    landing.classList.add('hidden');
    landing.style.display = 'none';
  }

  // 3. Ensure app-layout is visible and active
  const appLayout = document.querySelector('.app-layout');
  if (appLayout) {
    appLayout.style.display = 'flex';
  }

  // 4. Navigate to Home
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
  // 1. Hide all auth shells
  document.querySelectorAll('.auth-page-shell').forEach(shell => {
    shell.classList.remove('active');
    shell.style.display = 'none';
  });

  // 2. Show landing page
  const landing = document.getElementById('landing-page');
  if (landing) {
    landing.classList.remove('hidden');
    landing.style.display = 'flex';
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

  // 1. Password toggles
  document.querySelectorAll('.auth-password-toggle').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      const wrap = btn.closest('.auth-input-wrap');
      const input = wrap ? wrap.querySelector('input') : null;
      if (input) {
        if (input.type === 'password') {
          input.type = 'text';
          btn.innerHTML = `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line></svg>`;
        } else {
          input.type = 'password';
          btn.innerHTML = `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>`;
        }
      }
    });
  });

  // 2. Forms Submit Handlers
  document.getElementById('form-auth-signin')?.addEventListener('submit', (e) => {
    e.preventDefault();
    completeAuth();
  });

  document.getElementById('form-auth-signup')?.addEventListener('submit', (e) => {
    e.preventDefault();
    completeAuth();
  });

  document.getElementById('form-auth-reset')?.addEventListener('submit', (e) => {
    e.preventDefault();
    alert('Password reset link has been sent to your email!\nPlease check your inbox to proceed.');
  });

  // 3. Social / Secondary Action Handlers
  document.getElementById('btn-signin-google')?.addEventListener('click', () => {
    completeAuth();
  });

  document.getElementById('btn-help-auth')?.addEventListener('click', () => {
    alert('Need help signing in?\n\nContact Netgravity IT Support at support@netgravity.kearney.com or refer to your corporate single sign-on directory.');
  });
}
