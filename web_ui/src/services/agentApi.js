/**
 * Agent-related API calls: agent control, status, operations
 */
import apiClient from './api'

export const agentApi = {
  /**
   * Kill a running agent container
   */
  async killAgent(containerName) {
    return apiClient.post(`/agents/kill/${containerName}`)
  },
}
