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

  /**
   * Release a pipeline lock for a project/board
   */
  async releasePipelineLock(projectName, boardName, issueNumber = null) {
    const body = issueNumber ? { issue_number: issueNumber } : {}
    return apiClient.post(`/api/projects/${projectName}/pipelines/${encodeURIComponent(boardName)}/release-lock`, body)
  },
}
