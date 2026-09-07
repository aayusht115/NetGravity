/**
 * NetGravity — Project Service
 * ============================
 * Manages project workspaces and snapshot context.
 */

import { apiClient } from '../api-client.js';

export const projectService = {
  async listProjects(params = {}) {
    const res = await apiClient.get('/api/projects', params);
    return res.projects || [];
  },

  async getProject(projectId) {
    return apiClient.get(`/api/projects/${projectId}`);
  },

  async createProject(projectData) {
    return apiClient.post('/api/projects', projectData);
  },

  async updateProject(projectId, updateData) {
    return apiClient.put(`/api/projects/${projectId}`, updateData);
  },
};
