/**
 * NetGravity — Ingestion Service
 * ==============================
 * Connects the data upload, document parsing, schema mapping review,
 * and canonical snapshot promotion workflow to the backend ingestion console.
 */

import { apiClient } from '../api-client.js';
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
    );
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
