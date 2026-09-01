/**
 * NetGravity — Centralized HTTP API Client
 * ========================================
 * Owns HTTP transport, authorization headers, request correlation,
 * timeout lifecycle, and error normalization.
 */

import { CONFIG } from './config.js';
import { ApplicationError, ErrorCode } from './errors.js';

/** Read one cookie by name. Returns '' when it is absent. */
function readCookie(name) {
  if (typeof document === 'undefined') return '';
  const match = document.cookie.match(
    new RegExp(`(?:^|;\\s*)${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : '';
}

// The session cookie's name, for documentation only: it is httpOnly and
// this file can never read it. Anything here that needs to know whether a
// session exists reads the CSRF cookie, which is set alongside it.
// const SESSION_COOKIE = 'ng_session';
const CSRF_COOKIE = 'ng_csrf';
const CSRF_HEADER = 'X-CSRF-Token';
const LEGACY_TOKEN_KEY = 'ngt_auth_token';
const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

class ApiClient {
  /**
   * The session no longer lives in `localStorage`.
   *
   * It was kept there and attached as `Authorization: Bearer`, which means any
   * script running on the page could read it — so one XSS anywhere in the
   * application exfiltrated a credential that stayed valid for eight hours, on
   * any machine, with no further access needed.
   *
   * The server now sets an httpOnly cookie that script cannot read at all. The
   * browser attaches it automatically, so nothing here has to hold it, and an
   * injected script can act inside the page while it runs but cannot carry the
   * session away.
   *
   * Because a cookie IS sent automatically, unsafe methods carry a
   * double-submit CSRF token: `ng_csrf` is readable by design, and echoing it
   * in a header is exactly what a cross-site page cannot do.
   */
  constructor() {
    this.token = null;
    // A token left over from the previous scheme is cleared rather than used.
    // Leaving it would keep a long-lived credential in a place we have just
    // finished saying is unsafe.
    if (typeof localStorage !== 'undefined') {
      try { localStorage.removeItem(LEGACY_TOKEN_KEY); } catch (e) { /* ignore */ }
    }
  }

  /**
   * Hold a bearer token in memory for this page only.
   *
   * Used by scripts and harnesses that authenticate without a browser session.
   * The browser client does not call this on sign-in any more: the cookie is
   * the credential, and it is never written to storage.
   */
  setToken(token) {
    this.token = token || null;
  }

  /**
   * True when this browser looks like it holds a session.
   *
   * Checks the CSRF cookie, NOT the session cookie. The session cookie is
   * httpOnly — which is the entire point of it — so `document.cookie` never
   * contains it and testing for it is always false. Reading it here meant
   * `restoreSession()` returned early on every page load and a refresh dropped
   * a signed-in user back to the landing page with a perfectly valid session
   * in their browser.
   *
   * `ng_csrf` is set and cleared alongside the session and IS readable by
   * design, so it is the honest marker. It is only a hint: the server decides,
   * and `/api/auth/me` is what actually verifies.
   */
  get hasSession() {
    return Boolean(readCookie(CSRF_COOKIE)) || Boolean(this.token);
  }

  getToken() {
    return this.token;
  }

  _buildUrl(endpoint) {
    if (endpoint.startsWith('http://') || endpoint.startsWith('https://')) {
      return endpoint;
    }
    const cleanBase = CONFIG.API_BASE_URL.replace(/\/+$/, '');
    const cleanEndpoint = endpoint.replace(/^\/+/, '');
    return `${cleanBase}/${cleanEndpoint}`;
  }

  _generateRequestId() {
    return 'req_' + Math.random().toString(36).substr(2, 9) + Date.now().toString(36);
  }

  async request(endpoint, options = {}) {
    const url = this._buildUrl(endpoint);
    const headers = new Headers(options.headers || {});

    // Bearer only where a token was handed to us explicitly — a script or a
    // harness. In the browser the cookie is the credential and nothing here
    // holds it.
    if (this.token && !headers.has('Authorization')) {
      headers.set('Authorization', `Bearer ${this.token}`);
    }
    // Double-submit CSRF token on every unsafe method. Harmless when the
    // request is authenticated by bearer instead; the server only requires it
    // for cookie-authenticated ones.
    const method = (options.method || 'GET').toUpperCase();
    if (UNSAFE_METHODS.has(method) && !headers.has(CSRF_HEADER)) {
      const csrf = readCookie(CSRF_COOKIE);
      if (csrf) headers.set(CSRF_HEADER, csrf);
    }
    if (!headers.has('X-Request-ID')) {
      headers.set('X-Request-ID', this._generateRequestId());
    }
    if (!headers.has('Content-Type') && !(options.body instanceof FormData)) {
      headers.set('Content-Type', 'application/json');
    }

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.timeout || CONFIG.REQUEST_TIMEOUT_MS);

    try {
      const response = await fetch(url, {
        ...options,
        headers,
        // The session is an httpOnly cookie. `same-origin` is the default for
        // same-origin requests but is stated because a configured
        // `API_BASE_URL` makes some of these cross-origin, where the default
        // would silently omit the cookie and every request would 401.
        credentials: 'include',
        signal: controller.signal,
      });
      clearTimeout(timeout);

      let data = null;
      const contentType = response.headers.get('content-type') || '';
      if (contentType.includes('application/json')) {
        data = await response.json().catch(() => null);
      } else {
        data = await response.text().catch(() => null);
      }

      if (!response.ok) {
        throw ApplicationError.fromHttp(response.status, data || {});
      }

      return data;
    } catch (err) {
      clearTimeout(timeout);
      if (err instanceof ApplicationError) {
        throw err;
      }
      if (err.name === 'AbortError') {
        throw new ApplicationError(ErrorCode.TIMEOUT, `Request to '${endpoint}' timed out.`);
      }
      throw new ApplicationError(ErrorCode.NETWORK_ERROR, err.message || 'Network connection error.', { raw: err });
    }
  }

  get(endpoint, params = {}, options = {}) {
    let url = endpoint;
    const query = new URLSearchParams();
    for (const [k, v] of Object.entries(params || {})) {
      if (v !== undefined && v !== null && v !== '') {
        query.append(k, String(v));
      }
    }
    const qStr = query.toString();
    if (qStr) {
      url += (url.includes('?') ? '&' : '?') + qStr;
    }
    return this.request(url, { ...options, method: 'GET' });
  }

  post(endpoint, body = {}, options = {}) {
    return this.request(endpoint, {
      ...options,
      method: 'POST',
      body: body instanceof FormData ? body : JSON.stringify(body),
    });
  }

  put(endpoint, body = {}, options = {}) {
    return this.request(endpoint, {
      ...options,
      method: 'PUT',
      body: JSON.stringify(body),
    });
  }

  /**
   * DELETE, optionally with a body.
   *
   * `DELETE /api/auth/mfa` carries the password that authorises removing a
   * second factor, so a body is needed here even though DELETE usually has
   * none.
   */
  delete(endpoint, body = null, options = {}) {
    const request = { ...options, method: 'DELETE' };
    if (body !== null && body !== undefined) request.body = JSON.stringify(body);
    return this.request(endpoint, request);
  }

  upload(endpoint, formData, options = {}) {
    return this.request(endpoint, {
      ...options,
      method: 'POST',
      body: formData,
    });
  }
}

export const apiClient = new ApiClient();
