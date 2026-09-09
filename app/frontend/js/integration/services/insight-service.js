/**
 * NetGravity — Insights Service
 * =============================
 * The dashboard's route to the Reasoning Agent's findings about the network a
 * project is bound to.
 *
 * There was no route before this. `/orchestrator/insights` existed and worked,
 * a `reasoning-service.js` wrapped it, and the Reasoning Agent produced grounded
 * briefings — but nothing on any screen called any of them, and the structures
 * the Home feed reads (`HOME_INSIGHTS`, `HOME_ACTION_ITEMS`) were initialised
 * empty and written by nothing. Every user who uploaded their own data saw "No
 * insights have been generated for this network yet" permanently, on a fully
 * solved network. That wrapper is gone: this module replaces its only
 * meaningful method, and its other two had no caller.
 *
 * The orchestrator endpoint could not have closed that on its own: it is keyed
 * by Digital Twin `state_id`, and resolving a `project_id` to the right state
 * is control-plane knowledge that does not belong in a browser. `/api/insights`
 * answers the question this screen actually has.
 */

import { apiClient } from '../api-client.js';
import { CONFIG } from '../config.js';
import { getActiveProjectId } from '../project-context.js';

export const insightService = {
  /**
   * Insights and a recommendation for the whole network.
   *
   * Returns null on any failure rather than throwing: an insight feed is
   * additive to a dashboard, and a reasoning failure must not stop the KPIs
   * from rendering. The caller renders its empty state, which says no insight
   * has been generated — never that the network is healthy.
   */
  async getNetworkInsights(projectId = null) {
    try {
      return await apiClient.get('/api/insights', {
        project_id: projectId || getActiveProjectId(),
        scope: 'NETWORK',
      });
    } catch (err) {
      return null;
    }
  },

  /**
   * One finding, as a .docx a reader can take into a meeting.
   *
   * UNLIKE the other methods here this one THROWS. They return null because
   * an insight feed is additive to a dashboard and a reasoning failure must
   * not stop the KPIs rendering — but a download is something a person just
   * asked for, and a button that silently does nothing is the worst possible
   * answer to a click.
   */
  async downloadDerivation(insightId, options = {}) {
    // SCOPE COMES FROM THE RECORD, not from an assumption here.
    //
    // This sent `scope: 'NETWORK'` unconditionally. The deep dive opens a
    // facility-scoped finding as readily as a network one, and the server
    // looks the id up inside the briefing for the scope it was asked for — so
    // every download from a facility finding answered 404 about an id that
    // was on the screen.
    const scope = String(options.scope || 'NETWORK').toUpperCase();
    return apiClient.download(`/api/insights/${encodeURIComponent(insightId)}/document`, {
      project_id: options.projectId || getActiveProjectId(),
      scope,
      entity_id: (scope === 'NETWORK') ? undefined : (options.entityId || undefined),
    }, { timeout: CONFIG.DOCUMENT_TIMEOUT_MS });
  },

  /** Insights scoped to one facility. Same failure contract as above. */
  async getFacilityInsights(facilityId, projectId = null) {
    if (!facilityId) return null;
    try {
      return await apiClient.get('/api/insights', {
        project_id: projectId || getActiveProjectId(),
        scope: 'FACILITY',
        entity_id: facilityId,
      });
    } catch (err) {
      return null;
    }
  },

  /** Insights scoped to one lane, addressed as `ORIGIN->DESTINATION`. */
  async getLaneInsights(laneId, projectId = null) {
    if (!laneId) return null;
    try {
      return await apiClient.get('/api/insights', {
        project_id: projectId || getActiveProjectId(),
        scope: 'LANE',
        entity_id: laneId,
      });
    } catch (err) {
      return null;
    }
  },
};
