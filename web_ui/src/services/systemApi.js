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
}
