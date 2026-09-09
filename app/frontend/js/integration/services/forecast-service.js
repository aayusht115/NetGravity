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

  /**
   * One forecast series, as a .docx a reader can take into a meeting.
   *
   * UNLIKE `getForecast` this THROWS. That one returns its failure as a
   * status the screen renders, because a dashboard must draw something. A
   * download is a thing a person just asked for, and a button that silently
   * does nothing is the worst possible answer to a click.
   */
  async downloadDerivation(options = {}) {
    return apiClient.download('/api/forecast/document', {
      project_id: options.projectId || getActiveProjectId(),
      // The series the chart is SHOWING, not the first one the response
      // happens to carry: the reader is asking about the line in front of
      // them, and a document about a different market would be worse than no
      // document at all.
      market_id: options.marketId || undefined,
      product_id: options.productId || undefined,
      horizon: options.horizon || undefined,
    }, { timeout: CONFIG.DOCUMENT_TIMEOUT_MS });
  },

  async getSignals() {
    return apiClient.get('/api/signals');
  },
};
