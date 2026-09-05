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
  /**
   * Rank a set of solved scenarios and get the verdict.
   *
   * The ranking and the recommendation are the SERVER's, from authoritative
   * KPI values. The browser used to decide this itself, which put a
   * recommendation in JavaScript where nothing could audit or test it.
   */
  async compare(scenarioIds, projectId = null) {
    return apiClient.post('/api/scenarios/compare', {
      project_id: projectId || getActiveProjectId(),
      scenario_ids: scenarioIds,
    });
  },

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
   * The scenario this project stored for `name` since `sinceMs`, or null.
   *
   * Aborting a fetch does not stop a solve. When `simulateScenario` times out
   * the server is still working, and it finishes into a connection nobody is
   * listening on — it builds the record, persists it and answers 201 to no
   * one. The record is the same object the 201 carried, and `GET /api/scenarios`
   * returns it, so it can simply be collected.
   *
   * Polls until `timeoutMs` elapses. `sinceMs` guards against adopting an
   * older scenario that happens to share a name.
   */
  async findScenarioCreatedSince(name, sinceMs, {
    timeoutMs = 240000, intervalMs = 3000, projectId = null, onWait = null,
  } = {}) {
    const wanted = String(name || '').trim();
    const deadline = Date.now() + timeoutMs;
    // `created_at` is seconds on the record and milliseconds here.
    const after = (sinceMs - 2000) / 1000;
    while (Date.now() < deadline) {
      if (onWait) onWait(Math.round((Date.now() - (deadline - timeoutMs)) / 1000));
      let found = null;
      try {
        const all = await this.listScenarios(projectId);
        found = all
          .filter((r) => String(r.name || '').trim() === wanted
                      && Number(r.created_at || 0) >= after)
          .sort((x, y) => Number(y.created_at || 0) - Number(x.created_at || 0))[0]
          || null;
      } catch (e) {
        // A failed poll says nothing about the solve. Keep waiting.
        found = null;
      }
      if (found) return found;
      // eslint-disable-next-line no-await-in-loop
      await new Promise((r) => setTimeout(r, intervalMs));
    }
    return null;
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
