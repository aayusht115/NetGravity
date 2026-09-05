/**
 * NetGravity — Ingestion Service
 * ==============================
 * Connects the data upload, document parsing, schema mapping review,
 * and canonical snapshot promotion workflow to the backend ingestion console.
 */

import { apiClient } from '../api-client.js';
import { CONFIG } from '../config.js';
import { getActiveProjectId } from '../project-context.js';

export const ingestionService = {
  /**
   * Parse uploaded files for mapping review. Returns detected columns, sample
   * values, measured data quality and the extracted structure.
   *
   * This does NOT make the data analysable — `commitPreview` does. Keeping the
   * two apart is what stops a half-understood spreadsheet from reaching the
   * solver.
   */
  async uploadAndParse(formData, projectId = null) {
    const pid = projectId || getActiveProjectId();
    if (pid && !formData.has('project_id')) formData.append('project_id', pid);
    return apiClient.upload(
      `/api/ingestions/preview/upload-and-parse?project_id=${encodeURIComponent(pid || '')}`,
      formData,
      // Reading a workbook is not an ordinary request. Without this it
      // inherited the 30-second default and reported a file as unreadable
      // when the client had simply stopped waiting for it.
      { timeout: CONFIG.PARSE_TIMEOUT_MS },
    );
  },

  /**
   * The preview this project's parse produced, once it appears.
   *
   * Aborting the upload does not stop the parse. The server finishes, stores
   * the preview and answers 200 to nobody; `GET /preview/active` returns that
   * same object, so a client that gave up early can still collect it instead
   * of telling the user their file could not be read.
   *
   * Returns null if nothing is there by `timeoutMs`, which is the only state
   * in which anything may be said about the file.
   */
  async findParsedPreview(projectId = null, {
    timeoutMs = 120000, intervalMs = 2500,
  } = {}) {
    const pid = projectId || getActiveProjectId();
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      let preview = null;
      try {
        preview = await this.getActivePreview(pid);
      } catch (e) {
        // A failed poll says nothing about the parse. Keep waiting.
        preview = null;
      }
      if (preview && preview.status === 'PREVIEW') return preview;
      // eslint-disable-next-line no-await-in-loop
      await new Promise((r) => setTimeout(r, intervalMs));
    }
    return null;
  },

  /**
   * Commit the reviewed upload: assemble a CanonicalNetwork, register it as a
   * snapshot, and bind it to the project. After this the project's KPIs,
   * scenarios and forecasts run against the user's own data.
   */
  async commitPreview(projectId = null) {
    const pid = projectId || getActiveProjectId();
    return apiClient.post('/api/ingestions/preview/commit', { project_id: pid });
  },

  /**
   * The sheets and columns an upload may contain.
   *
   * Generated on the server from the extractor's own column table, so the
   * downloadable template describes what the parser actually reads rather than
   * a list maintained separately in the frontend and free to drift from it.
   */
  async getUploadSchema() {
    return apiClient.get('/api/ingestions/preview/schema');
  },

  async getActivePreview(projectId = null) {
    return apiClient.get('/api/ingestions/preview/active', {
      project_id: projectId || getActiveProjectId(),
    });
  },

  /**
   * The audit record of the dataset this project is analysing.
   *
   * The file, the mapping decisions as confirmed, the measured quality, the
   * cross-sheet integrity findings, the assembly's assumptions, and the
   * snapshot it produced. This is what answers "what produced this number?" —
   * a question the product previously had no surface for at all.
   */
  async getDataset(projectId = null) {
    return apiClient.get('/api/ingestions/preview/dataset', {
      project_id: projectId || getActiveProjectId(),
    });
  },

  async uploadFiles(files, categories = {}) {
    const formData = new FormData();
    files.forEach(f => formData.append('files', f));
    formData.append('categories', JSON.stringify(categories));
    formData.append('client_id', 'default');
    return apiClient.upload('/api/ingestions', formData);
  },

  async getSession(runId) {
    return apiClient.get(`/api/ingestions/${runId}`);
  },

  async getDraft(runId) {
    return apiClient.get(`/api/ingestions/${runId}/draft`);
  },

  async getReviews(runId) {
    return apiClient.get(`/api/ingestions/${runId}/reviews`);
  },

  async answerReviews(runId, decisions, revision) {
    return apiClient.post(`/api/ingestions/${runId}/reviews`, {
      decisions,
      revision,
    });
  },

  async analyseReviewItem(runId, itemId, userQuestion) {
    return apiClient.post('/api/ingestions/reviews/analyse', {
      item_id: itemId,
      question: userQuestion,
    });
  },

  async finalize(runId, revision) {
    return apiClient.post(`/api/ingestions/${runId}/finalize`, { revision });
  },
};
