/**
 * NetGravity — Digital Twin Service
 * =================================
 * Retrieves published Digital Twin states, node capacities, flow corridors,
 * and baseline/scenario differential comparisons.
 */

import { apiClient } from '../api-client.js';

export const twinService = {
  async listStates(snapshotId = null) {
    const params = snapshotId ? { snapshot_id: snapshotId } : {};
    return apiClient.get('/orchestrator/twin/states', params);
  },

  async getState(stateId, options = {}) {
    const params = {
      flow_offset: options.flowOffset || 0,
      flow_limit: options.flowLimit !== undefined ? options.flowLimit : 0, // 0 = all flows
      include_flows: options.includeFlows !== undefined ? options.includeFlows : true,
    };
    return apiClient.get(`/orchestrator/twin/states/${stateId}`, params);
  },

  async getSnapshotState(snapshotId, scenarioId = null, options = {}) {
    const params = {
      scenario_id: scenarioId || '',
      flow_offset: options.flowOffset || 0,
      flow_limit: options.flowLimit !== undefined ? options.flowLimit : 0,
      include_flows: options.includeFlows !== undefined ? options.includeFlows : true,
    };
    return apiClient.get(`/orchestrator/twin/snapshots/${snapshotId}`, params);
  },

  async compare(snapshotId, scenarioId) {
    return apiClient.get('/orchestrator/twin/compare', {
      snapshot_id: snapshotId,
      scenario_id: scenarioId,
    });
  },
};
