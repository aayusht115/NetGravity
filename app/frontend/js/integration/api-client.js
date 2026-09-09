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

/**
 * Who wants to know when a request starts and finishes.
 *
 * One consumer today: the agent loading screen. It needs the correlation id
 * of a request WHILE that request is in flight, because that is the id the
 * orchestrator files the resulting execution under, and asking the control
 * plane what that execution is doing is the only way for a loading screen to
 * report a twenty-second solve as anything other than a spinner.
 *
 * Notification only. An observer cannot alter the request, cancel it, read
 * its body, or change what is returned — `notify` swallows whatever an
 * observer throws, so a broken observer cannot break an HTTP call.
 */
const requestObservers = new Set();

/** Watch every request this client makes. Returns an unsubscribe function. */
export function observeRequests(fn) {
  requestObservers.add(fn);
  return () => requestObservers.delete(fn);
}

function notifyObservers(event) {
  requestObservers.forEach((fn) => {
    try { fn(event); } catch (e) { /* an observer must never break a request */ }
  });
}

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

  /**
   * Fetch a FILE, with the same credentials every other call uses.
   *
   * `request()` reads the body as JSON or as text, so a .docx came back as a
   * mangled string. This is the same transport — the session cookie, the
   * request id, the timeout — returning the bytes and the name the server
   * asked the browser to save them under.
   *
   * Deliberately not `window.open(url)`, which is the short way to do this
   * and the wrong one: it cannot send the bearer token a harness uses, it
   * loses the error body on a 4xx (the reader gets a blank tab instead of a
   * reason), and a popup blocker eats it.
   */
  /**
   * Fetch a file, with the server's own filename.
   *
   * `options.timeout` because the default request budget is 30 seconds and a
   * document is not a request: building one runs a solve and, where the
   * gateway is configured, a text-generation call the gateway itself allows
   * 60 seconds for. Inheriting REQUEST_TIMEOUT_MS aborted the fetch while the
   * server was still writing the file, and the reader saw "Could not build
   * the document" for a document that was built.
   */
  async download(endpoint, params = {}, options = {}) {
    const query = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== undefined && v !== null),
    ).toString();
    const url = this._buildUrl(endpoint) + (query ? `?${query}` : '');

    const headers = new Headers();
    if (this.token) headers.set('Authorization', `Bearer ${this.token}`);
    headers.set('X-Request-ID', this._generateRequestId());

    const controller = new AbortController();
    const budget = options.timeout || CONFIG.REQUEST_TIMEOUT_MS;
    const timeout = setTimeout(() => controller.abort(), budget);
    try {
      const response = await fetch(url, {
        method: 'GET', headers, credentials: 'include', signal: controller.signal,
      });
      if (!response.ok) {
        // The server's own reason, where it sent one, rather than "download
        // failed".
        let detail = null;
        try { detail = await response.json(); } catch (e) { detail = null; }
        throw ApplicationError.fromHttp(response.status, detail || {});
      }
      // `filename="…"` off the Content-Disposition the server set, so the
      // file is named by whoever built it rather than by the URL.
      const disposition = response.headers.get('content-disposition') || '';
      const match = /filename="?([^"]+)"?/i.exec(disposition);
      return {
        blob: await response.blob(),
        filename: match ? match[1] : 'download',
      };
    } finally {
      clearTimeout(timeout);
    }
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

    // The id the server will file any resulting execution under. Announced
    // BEFORE the fetch, which is the only moment it is useful: after the
    // response there is an execution id and nothing left to watch.
    const correlationId = headers.get('X-Request-ID') || '';
    notifyObservers({ phase: 'start', correlationId, endpoint, method });

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
        notifyObservers({ phase: 'end', correlationId, endpoint, method,
                          ok: false, status: response.status });
        throw ApplicationError.fromHttp(response.status, data || {});
      }

      notifyObservers({ phase: 'end', correlationId, endpoint, method,
                        ok: true, status: response.status });
      return data;
    } catch (err) {
      clearTimeout(timeout);
      if (err instanceof ApplicationError) {
        throw err;
      }
      notifyObservers({ phase: 'end', correlationId, endpoint, method,
                        ok: false, status: 0 });
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
