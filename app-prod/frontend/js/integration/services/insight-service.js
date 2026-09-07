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
