/**
 * Project-related API calls: projects, issues, configurations
 */
import apiClient from './api'

export const projectApi = {
  /**
   * Get all configured projects
   */
  async getProjects() {
    return apiClient.get('/api/projects')
  },

  /**
   * Get a specific project by name
   */
  async getProject(projectName) {
    return apiClient.get(`/api/projects/${projectName}`)
  },
}
