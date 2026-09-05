/**
 * NetGravity — Landing page bootstrap
 * ===================================
 * Extracted verbatim from an inline <script> in index.html.
 *
 * The Content Security Policy is `script-src 'self'` with no 'unsafe-inline'.
 * An inline block would need either that allowance — which defeats the policy,
 * since injected script is inline script — or a per-response nonce, which
 * would mean templating a page that is otherwise a static file. Moving it to a
 * module is simpler than either and removes the exception entirely.
 */
window.switchAuthPanel = function(view) {
  document.querySelectorAll('.landing-auth-panel').forEach(function(p) { p.classList.remove('active'); });
  var fab = document.getElementById('floating-chatbot-fab');
  if (fab) fab.style.display = 'none';

  if (view === 'signup') {
    var p = document.getElementById('panel-signup');
    if (p) p.classList.add('active');
  } else if (view === 'reset') {
    var p = document.getElementById('panel-reset');
    if (p) {
      p.classList.add('active');
      p.classList.remove('is-confirmed');
      var f = document.getElementById('form-panel-reset');
      var c = document.getElementById('panel-reset-confirmation');
      if (f) f.style.display = 'block';
      if (c) c.style.display = 'none';
    }
  } else {
    var p = document.getElementById('panel-signin');
    if (p) p.classList.add('active');
  }
};

window.navigateToAuth = window.switchAuthPanel;

/* Pre-module fallback. projects.js/auth.js replace this on boot; until
   then, route sign-in to Select Project and sign-up to Create Project
   so the flow is the same whenever the form is submitted. */
window.completeAuth = function(origin) {
  if (origin === 'signup' && typeof window.showCreateProject === 'function') {
    window.showCreateProject('first');
    return;
  }
  if (origin !== 'signup' && typeof window.showSelectProject === 'function') {
    window.showSelectProject();
    return;
  }
  if (typeof window.enterApp === 'function') {
    window.enterApp();
    return;
  }
  var landing = document.getElementById('landing-page');
  if (landing) {
    landing.classList.add('hidden');
    landing.style.display = 'none';
  }
  var appShell = document.querySelector('.app-shell');
  if (appShell) {
    appShell.style.display = 'flex';
  }
  var fab = document.getElementById('floating-chatbot-fab');
  if (fab) {
    fab.style.display = 'flex';
  }
  if (typeof window.navigateToTab === 'function') {
    window.navigateToTab('home');
  }
  setTimeout(function() {
    if (typeof window.renderHome === 'function') window.renderHome();
    window.dispatchEvent(new Event('resize'));
  }, 50);
};

window.returnToLanding = function() {
  var appShell = document.querySelector('.app-shell');
  if (appShell) {
    appShell.style.display = 'none';
  }
  if (typeof window.hideProjectPages === 'function') {
    window.hideProjectPages();
  } else {
    ['select-project-page', 'create-project-page'].forEach(function(id) {
      var el = document.getElementById(id);
      if (el) el.classList.add('hidden');
    });
  }
  if (typeof window.hideIngestionPages === 'function') {
    window.hideIngestionPages();
  } else {
    ['upload-data-page', 'ingestion-page'].forEach(function(id) {
      var el = document.getElementById(id);
      if (el) el.classList.add('hidden');
    });
  }
  var fab = document.getElementById('floating-chatbot-fab');
  if (fab) {
    fab.style.display = 'none';
  }
  var landing = document.getElementById('landing-page');
  if (landing) {
    landing.classList.remove('hidden');
    landing.style.display = 'flex';
  }
  window.switchAuthPanel('signin');
};
