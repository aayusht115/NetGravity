/**
 * NetGravity — Scenario Service
 * =============================
 * Scenario retrieval, What-If simulation execution, and delta comparison.
 *
 * Every scenario figure originates in a real MILP solve reported through the
 * authoritative KPI layer. This module performs no arithmetic on business
 * values — it only transports them.
 */

import { apiClient } from '../api-client.js';
import { CONFIG } from '../config.js';
import { getActiveProjectId } from '../project-context.js';

export const scenarioService = {
  async listScenarios(projectId = null) {
    const res = await apiClient.get('/api/scenarios', {
      project_id: projectId || getActiveProjectId(),
    });
    return res.scenarios || [];
  },

  async getBaseline(projectId = null) {
    return apiClient.get('/api/scenarios/baseline', {
      project_id: projectId || getActiveProjectId(),
    }, { timeout: CONFIG.SOLVE_TIMEOUT_MS });
  },

  /**
   * Solve a what-if scenario.
   *
   * `params` must carry `action`, `facility_ids`, and the delta appropriate to
   * the action. The response carries `baseline_kpis`, `scenario_kpis`,
   * `deltas` and `provenance` — all authoritative, each with a KPIStatus.
   */
  async simulateScenario(params) {
    const projectId = params.project_id || getActiveProjectId();
    return apiClient.post(
      `/api/scenarios/simulate?project_id=${encodeURIComponent(projectId)}`,
      { ...params, project_id: projectId },
      // Two MILP solves: the scenario, and the re-optimised reference it is
      // measured against. Well past the 30-second default, which aborted the
      // request while the solver was still running and reported the scenario
      // as failed.
      { timeout: CONFIG.SOLVE_TIMEOUT_MS },
    );
  },

  /**
   * Discard a solved scenario.
   *
   * The comparison holds three at a time, so removing one is ordinary use.
   * Deletion was client-side only, which meant a scenario the user removed
   * reappeared on the next page load.
   */
  async deleteScenario(scenarioId, projectId = null) {
    const pid = projectId || getActiveProjectId();
    return apiClient.delete(
      `/api/scenarios/${encodeURIComponent(scenarioId)}`
      + `?project_id=${encodeURIComponent(pid)}`,
    );
  },
};
