/**
 * NetGravity — Auth Service
 * =========================
 * Manages login, signup, session restoration, and logout.
 */

import { apiClient } from '../api-client.js';

export const authService = {
  /**
   * Sign in.
   *
   * The response's `token` is NOT stored. The server sets an httpOnly session
   * cookie the browser attaches on its own, which is the whole point of the
   * change: a token this code could hold is a token injected script could
   * read. The field is still returned for scripts and harnesses that
   * authenticate without a browser session.
   *
   * A second factor turns this into a two-step sign-in: `mfa_required` means
   * no session was issued and `completeMfa` must be called with a code.
   */
  async login(email, password) {
    return apiClient.post('/api/auth/login', { email, password });
  },

  /** Finish a sign-in that asked for a second factor. */
  async completeMfa(mfaToken, code) {
    return apiClient.post('/api/auth/login/mfa', { mfa_token: mfaToken, code });
  },

  async requestPasswordReset(email) {
    return apiClient.post('/api/auth/password/reset', { email });
  },

  async confirmPasswordReset(token, password) {
    return apiClient.post('/api/auth/password/reset/confirm', { token, password });
  },

  async changePassword(currentPassword, password) {
    return apiClient.post('/api/auth/password', {
      current_password: currentPassword, password,
    });
  },

  async mfaStatus() { return apiClient.get('/api/auth/mfa'); },
  async beginMfaEnrolment() { return apiClient.post('/api/auth/mfa/enrol'); },
  async confirmMfaEnrolment(code) { return apiClient.post('/api/auth/mfa/confirm', { code }); },
  async disableMfa(password) { return apiClient.delete('/api/auth/mfa', { password }); },

  async listSessions() { return apiClient.get('/api/auth/sessions'); },
  async revokeOtherSessions() { return apiClient.delete('/api/auth/sessions'); },

  /**
   * Register an account.
   *
   * Accepts either `signup({ name, email, password })` or the older positional
   * `signup(name, email, password)` — the prototype's auth.js still uses the
   * latter, and silently changing its meaning would have broken that caller.
   */
  async signup(nameOrPayload, email, password) {
    const payload = (nameOrPayload && typeof nameOrPayload === 'object')
      ? nameOrPayload
      : { name: nameOrPayload, email, password };
    // As with login: the cookie is the credential; the token is not stored.
    return apiClient.post('/api/auth/signup', payload);
  },

  async getCurrentUser() {
    return apiClient.get('/api/auth/me');
  },

  async logout() {
    try {
      await apiClient.post('/api/auth/logout');
    } finally {
      apiClient.setToken(null);
    }
  },
};
