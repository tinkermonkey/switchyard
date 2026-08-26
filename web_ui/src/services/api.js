/**
 * Base API client with common configuration and error handling
 */

class ApiClient {
  constructor(baseURL = '') {
    this.baseURL = baseURL
  }

  async request(endpoint, options = {}) {
    const url = `${this.baseURL}${endpoint}`
    const config = {
      headers: {
        'Content-Type': 'application/json',
        ...options.headers,
      },
      ...options,
    }

    try {
      const response = await fetch(url, config)
      
      // Handle non-JSON responses (like plain text)
      const contentType = response.headers.get('content-type')
      if (contentType && contentType.includes('application/json')) {
        const data = await response.json()
        
        if (!response.ok) {
          const err = new Error(data.error || data.message || `HTTP ${response.status}`)
          // Preserve the full parsed body so callers that need structured detail
          // (e.g. a 409 "confirm and retry with force" response) aren't limited
          // to the flattened message string — existing catch blocks that only
          // read error.message are unaffected.
          err.data = data
          err.status = response.status
          throw err
        }

        return data
      }
      
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`)
      }
      
      return response
    } catch (error) {
      console.error(`API Error [${endpoint}]:`, error)
      throw error
    }
  }

  async get(endpoint, options = {}) {
    return this.request(endpoint, { ...options, method: 'GET' })
  }

  async post(endpoint, data, options = {}) {
    return this.request(endpoint, {
      ...options,
      method: 'POST',
      body: JSON.stringify(data),
    })
  }

  async put(endpoint, data, options = {}) {
    return this.request(endpoint, {
      ...options,
      method: 'PUT',
      body: JSON.stringify(data),
    })
  }

  async delete(endpoint, options = {}) {
    return this.request(endpoint, { ...options, method: 'DELETE' })
  }
}

// Create a singleton instance
const apiClient = new ApiClient()

export default apiClient
