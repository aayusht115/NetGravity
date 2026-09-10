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

  /**
   * The warehouse deep dive: health, rankings, and — on request — sizing and
   * a before-and-after.
   *
   * `growthPct` / `regionGrowth` state a demand growth rate. Without one the
   * response's sizing section reports why it is empty rather than sizing the
   * footprint against a rate nobody stated.
   *
   * `includeOptimized` runs a SECOND optimisation of the whole network to
   * produce the per-site before-and-after, so it takes the solve timeout and
   * is never sent by default.
   */
  async getWarehouseDeepDive(projectId = null, options = {}) {
    const params = scope(projectId);
    if (options.growthPct !== null && options.growthPct !== undefined
        && options.growthPct !== '') {
      params.growth_pct = String(options.growthPct);
    }
    if (options.regionGrowth) params.region_growth = options.regionGrowth;
    if (options.targetUtilizationPct) {
      params.target_utilization_pct = String(options.targetUtilizationPct);
    }
    if (options.growthSource) params.growth_source = options.growthSource;
    if (options.includeOptimized) params.include = 'optimized';
    return apiClient.get('/api/kpis/warehouse', params, solveOptions);
  },

  /**
   * The KPI screen, as a workbook.
   *
   * UNLIKE the read methods on this service it THROWS. Those return a failure
   * as a status the screen renders, because a dashboard must draw something;
   * a download is a thing a person just asked for, and a button that silently
   * does nothing is the worst possible answer to a click.
   *
   * `scope` carries what is on screen — the sites, the lens and the filters —
   * so the workbook's cover states the population its figures are of.
   */
  async downloadWorkbook(scope = {}, projectId = null) {
    const id = projectId || getActiveProjectId();
    return apiClient.download('/api/kpis/export.xlsx',
      { project_id: id }, {
        method: 'POST',
        body: {
          project_id: id,
          facility_ids: scope.facilityIds || [],
          lens_label: scope.lensLabel || '',
          filters: scope.filters || '',
          horizon: scope.horizon || '',
        },
        timeout: CONFIG.DOCUMENT_TIMEOUT_MS,
      });
  },

  /**
   * How the figures on this screen are calculated, as a .docx.
   *
   * Throws, like every other download on this service: a button that
   * silently does nothing is the worst possible answer to a click.
   */
  async downloadMethod(scope = {}, projectId = null) {
    const id = projectId || getActiveProjectId();
    return apiClient.download('/api/kpis/method.docx',
      { project_id: id }, {
        method: 'POST',
        body: {
          project_id: id,
          facility_ids: scope.facilityIds || [],
          lens_label: scope.lensLabel || '',
        },
        timeout: CONFIG.DOCUMENT_TIMEOUT_MS,
      });
  },

  async getThresholds() {
    return apiClient.get('/api/kpis/thresholds');
  },

  /**
   * What ONE chart on the KPI screen means.
   *
   * Sent only when a reader presses Explain — never on render. The backend
   * keeps one record per chart per analysis, so re-opening the same
   * explanation costs nothing there; `kpi-explain.js` keeps its own copy so
   * re-opening costs nothing HERE either, not even a round trip.
   *
   * `facilityIds` are the sites the chart actually drew, after the screen's
   * filters. Without them the briefing would describe the whole network while
   * the reader looks at three sites of it.
   */
  async explainChart(chart, { facilityIds = null, facilityId = null,
                              projectId = null } = {}) {
    const body = { chart, project_id: projectId || getActiveProjectId() };
    if (facilityIds) body.facility_ids = facilityIds;
    if (facilityId) body.facility_id = facilityId;
    // The solve timeout, not the default: the first explanation of a network
    // version can be the request that triggers its analysis.
    return apiClient.post(
      `/api/kpis/explain?project_id=${encodeURIComponent(body.project_id)}`,
      body, solveOptions);
  },
};
