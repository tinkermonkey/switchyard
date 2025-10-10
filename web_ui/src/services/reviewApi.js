/**
 * Review learning API calls: filters, agents, review data
 */
import apiClient from './api'

// API base can be configured if needed
const API_BASE = ''

export const reviewApi = {
  /**
   * Get available agents for filtering
   */
  async getAvailableAgents() {
    return apiClient.get(`${API_BASE}/api/review-filters/agents`)
  },

  /**
   * Get review filters, optionally filtered by agent
   */
  async getFilters(agentFilter = null) {
    const endpoint = agentFilter
      ? `${API_BASE}/api/review-filters?agent=${agentFilter}`
      : `${API_BASE}/api/review-filters`
    return apiClient.get(endpoint)
  },

  /**
   * Toggle a filter's enabled state
   */
  async toggleFilter(filterId) {
    return apiClient.post(`${API_BASE}/api/review-filters/${filterId}/toggle`)
  },

  /**
   * Delete a review filter
   */
  async deleteFilter(filterId) {
    return apiClient.delete(`${API_BASE}/api/review-filters/${filterId}`)
  },

  /**
   * Create or update a review filter
   */
  async saveFilter(filter) {
    const endpoint = filter.filter_id
      ? `${API_BASE}/api/review-filters/${filter.filter_id}`
      : `${API_BASE}/api/review-filters`
    
    const method = filter.filter_id ? 'put' : 'post'
    return apiClient[method](endpoint, filter)
  },
}
