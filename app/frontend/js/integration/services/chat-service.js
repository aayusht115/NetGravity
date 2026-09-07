/**
 * NetGravity — Chat Service
 * =========================
 * Connects the "Ask Netgravity" assistant to the Orchestrator ChatService.
 */

import { apiClient } from '../api-client.js';
import { getActiveSnapshotId } from '../project-context.js';

export const chatService = {
  async sendMessage(message, conversationId = null, snapshotId = null) {
    // Default to the snapshot the active project is bound to. Every caller
    // omitted this argument, so the orchestrator fell back to the network it
    // boots with and answered questions about facilities the user has never
    // seen.
    const snapshot = snapshotId || getActiveSnapshotId();
    return apiClient.post('/orchestrator/chat', {
      message,
      conversation_id: conversationId || undefined,
      network_snapshot_id: snapshot || undefined,
      disable_llm: false,
    }, {
      // A question to the assistant runs a solve and a reasoning pass, which
      // is minutes of work in the worst case — not the sub-second fetch the
      // 30s default was sized for. Under that default the request aborted
      // mid-analysis and the user was told the engine was unreachable while it
      // was still working. The typing indicator is showing throughout.
      timeout: 180000,
    });
  },

  async getHistory(conversationId) {
    return apiClient.get(`/orchestrator/chat/${conversationId}/history`);
  },
};
