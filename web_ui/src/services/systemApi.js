/**
 * System-level API calls: health checks, circuit breakers, system status
 */
import apiClient from './api'

export const systemApi = {
  /**
   * Get system health status
   */
  async getHealth() {
    return apiClient.get('/health')
  },

  /**
   * Get circuit breaker states
   */
  async getCircuitBreakers() {
    return apiClient.get('/api/circuit-breakers')
  },

  /**
   * Reset an agent circuit breaker
   * @param {string} agentName - Name of the agent (e.g., 'code_reviewer')
   */
  async resetAgentBreaker(agentName) {
    return apiClient.post(`/api/circuit-breakers/agent/${agentName}/reset`)
  },

  /**
   * Reset the Claude Code circuit breaker
   */
  async resetClaudeCodeBreaker() {
    return apiClient.post('/api/circuit-breakers/claude-code/reset')
  },

  /**
   * Reset the GitHub API circuit breaker
   */
  async resetGitHubApiBreaker() {
    return apiClient.post('/api/circuit-breakers/github-api/reset')
  },
}
