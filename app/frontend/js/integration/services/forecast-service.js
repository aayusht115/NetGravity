/**
 * NetGravity — Demand Forecast & Signals Service
 * ===============================================
 * Project-scoped forecasts produced by the real forecasting engines, routed
 * through the orchestrator's `forecast.demand` capability.
 *
 * The response always carries an explicit `status`. `FORECAST_UNAVAILABLE`
 * means the network has no observed demand history — a real answer, and the
 * caller must render it rather than substituting a cone.
 */

import { apiClient } from '../api-client.js';
import { CONFIG } from '../config.js';
import { getActiveProjectId } from '../project-context.js';

export const forecastService = {
  async getForecast(projectId = null, horizon = 6) {
    return apiClient.get('/api/forecast', {
      project_id: projectId || getActiveProjectId(),
      horizon,
      // The forecast runs through the orchestrator and can queue behind a
      // solve of the same snapshot.
    }, { timeout: CONFIG.SOLVE_TIMEOUT_MS });
  },

  async getSignals() {
    return apiClient.get('/api/signals');
  },
};
