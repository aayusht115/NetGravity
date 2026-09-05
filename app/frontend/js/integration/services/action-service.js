/**
 * NetGravity — Action Service
 * ===========================
 * The screen's route to the Action Agent: what is outstanding on this
 * project, who a request can go to, and sending one.
 *
 * `/api/actions` is project-scoped and returns items already grouped by
 * field — one action per missing column, carrying the sites it is missing
 * from — because that is the shape a person acts on. The grouping is the
 * server's, not this file's: fifteen distribution centres missing a fixed
 * cost is one request to one person, and deciding that is a judgement about
 * the data, which belongs beside the data.
 *
 * Reads return null on failure rather than throwing, matching
 * insight-service.js: an action feed is additive to the overview, and an
 * Action Agent that is unreachable must not stop the KPIs rendering. A SEND
 * throws, because a person pressed a button and is owed an answer either
 * way — silence after "Send" is indistinguishable from success.
 */

import { apiClient } from '../api-client.js';
import { getActiveProjectId } from '../project-context.js';

export const actionService = {
  /** Open actions, the standing recipient list, and whether email is live. */
  async getActions(projectId = null) {
    try {
      return await apiClient.get('/api/actions', {
        project_id: projectId || getActiveProjectId(),
      });
    } catch (err) {
      console.warn('[actions] could not load action items:', err.message);
      return null;
    }
  },

  /**
   * Send one action's request.
   *
   * `to` is the full recipient list as edited on screen. `remember` appends
   * any new address to the standing list, which is how that list grows —
   * the second time you ask this person for something they are already
   * offered.
   *
   * The response's `delivery` is the honest one: "sent", "stubbed" (no
   * outbound credential is configured, so nothing left the machine) or
   * "failed". The caller must show it rather than assuming success.
   */
  async dispatch(actionId, { to, subject, body, remember = true, projectId = null } = {}) {
    return apiClient.post(`/api/actions/${encodeURIComponent(actionId)}/dispatch`, {
      project_id: projectId || getActiveProjectId(),
      to,
      subject,
      body,
      remember,
    });
  },

  async addRecipient(email, label = '', projectId = null) {
    return apiClient.post('/api/actions/recipients', {
      project_id: projectId || getActiveProjectId(), email, label,
    });
  },

  async removeRecipient(email) {
    // Query string, not a body: `apiClient.delete` will send a JSON body,
    // but a DELETE body is ignored by enough of the stack that the server
    // reads this one off the query string instead.
    return apiClient.delete(
      `/api/actions/recipients?project_id=${encodeURIComponent(getActiveProjectId())}`
      + `&email=${encodeURIComponent(email)}`);
  },

  /** What has been sent on this project, newest first. */
  async getDispatches(projectId = null) {
    try {
      return await apiClient.get('/api/actions/dispatches', {
        project_id: projectId || getActiveProjectId(),
      });
    } catch (err) {
      console.warn('[actions] could not load the dispatch log:', err.message);
      return null;
    }
  },
};
