/**
 * NetGravity — Authoritative KPI Service
 * ======================================
 * Sourced strictly from the Phase 9.1 KPIRegistry and its evidence package.
 * Every request is project-scoped; the backend refuses an unbound project with
 * NO_NETWORK_BOUND rather than answering from another network.
 */

import { apiClient } from '../api-client.js';
import { CONFIG } from '../config.js';
import { getActiveProjectId } from '../project-context.js';

function scope(projectId, extra = {}) {
  return { project_id: projectId || getActiveProjectId(), ...extra };
}

/**
 * A KPI request may be waiting on a MILP solve, and must not be given the
 * ordinary 30-second request timeout.
 *
 * The first request for a network version runs the optimisation server-side.
 * On a real client network that is twenty to forty seconds, so the default
 * timeout aborted it at thirty and the dashboard reported "Analysis
 * unavailable: request timeout" for a solve that had in fact succeeded and was
 * about to be stored. Aborting the fetch does not stop the solve, so the user
 * was shown a failure and charged for the work anyway.
 */
const solveOptions = { timeout: CONFIG.SOLVE_TIMEOUT_MS };

export const kpiService = {
  async getNetworkKPIs(projectId = null) {
    return apiClient.get('/api/kpis/network', scope(projectId), solveOptions);
  },

  async getAllFacilityKPIs(projectId = null) {
    return apiClient.get('/api/kpis/facilities', scope(projectId), solveOptions);
  },

  /** Solved volume and cost for every lane the optimiser used. */
  async getFlowKPIs(projectId = null) {
    return apiClient.get('/api/kpis/flows', scope(projectId), solveOptions);
  },

  async getFacilityKPIs(facilityId, projectId = null) {
    return apiClient.get(`/api/kpis/facilities/${encodeURIComponent(facilityId)}`,
                         scope(projectId), solveOptions);
  },

  /**
   * Whether this project's analysis already exists — answered WITHOUT starting
   * one, so the loading screen can say what kind of wait this is.
   */
  async getReadiness(projectId = null) {
    return apiClient.get('/api/kpis/readiness', scope(projectId));
  },

  /** The complete AuthoritativeEvidencePackage the reasoning layer consumes. */
  async getEvidencePackage(projectId = null) {
    return apiClient.get('/api/kpis/evidence', scope(projectId), solveOptions);
  },

  async getThresholds() {
    return apiClient.get('/api/kpis/thresholds');
  },
};
