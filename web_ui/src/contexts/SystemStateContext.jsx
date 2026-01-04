import { createContext, useContext, useEffect, useState } from 'react'
import { systemApi } from '../services/systemApi'
import { startPolling, POLLING_INTERVALS } from '../utils/polling'

const SystemStateContext = createContext()

/**
 * SystemStateProvider - Manages system health and circuit breaker state
 * Polls health and circuit breaker endpoints at regular intervals
 */
export function SystemStateProvider({ children }) {
  const [systemHealth, setSystemHealth] = useState(null)
  const [healthLoading, setHealthLoading] = useState(true)
  const [healthError, setHealthError] = useState(null)

  const [circuitBreakers, setCircuitBreakers] = useState([])
  const [cbSummary, setCbSummary] = useState({ open: 0, half_open: 0, healthy: 0 })
  const [cbLoading, setCbLoading] = useState(true)
  const [cbError, setCbError] = useState(null)

  // Fetch health status
  const fetchHealth = async () => {
    try {
      const data = await systemApi.getHealth()
      setSystemHealth(data)
      setHealthError(null)
    } catch (error) {
      console.error('Error fetching health status:', error)
      setSystemHealth({ status: 'error', error: error.message })
      setHealthError(error.message)
    } finally {
      setHealthLoading(false)
    }
  }

  // Fetch circuit breaker status
  const fetchCircuitBreakers = async () => {
    try {
      const data = await systemApi.getCircuitBreakers()

      if (data.success) {
        setCircuitBreakers(data.circuit_breakers || [])
        setCbSummary(data.summary || { open: 0, half_open: 0, healthy: 0 })
        setCbError(null)
      }
    } catch (error) {
      console.error('Error fetching circuit breakers:', error)
      setCbError(error.message)
    } finally {
      setCbLoading(false)
    }
  }

  // Reset a circuit breaker
  const resetCircuitBreaker = async (circuitBreaker) => {
    try {
      let response
      
      // Determine which API to call based on breaker service/type
      if (circuitBreaker.service === 'agent_execution' && circuitBreaker.agent) {
        response = await systemApi.resetAgentBreaker(circuitBreaker.agent)
      } else if (circuitBreaker.service === 'claude_code') {
        response = await systemApi.resetClaudeCodeBreaker()
      } else if (circuitBreaker.service === 'github_api') {
        response = await systemApi.resetGitHubApiBreaker()
      } else {
        throw new Error(`Unknown circuit breaker type: ${circuitBreaker.service}`)
      }

      if (response.success) {
        // Refresh circuit breakers to show updated state
        await fetchCircuitBreakers()
        return { success: true, message: response.message }
      } else {
        return { success: false, error: response.error || 'Reset failed' }
      }
    } catch (error) {
      console.error('Error resetting circuit breaker:', error)
      return { success: false, error: error.message }
    }
  }

  // Set up polling for health checks
  useEffect(() => {
    const cleanup = startPolling(fetchHealth, POLLING_INTERVALS.HEALTH_CHECK)
    return cleanup
  }, [])

  // Set up polling for circuit breakers
  useEffect(() => {
    const cleanup = startPolling(fetchCircuitBreakers, POLLING_INTERVALS.CIRCUIT_BREAKERS)
    return cleanup
  }, [])

  const value = {
    // Health state
    systemHealth,
    healthLoading,
    healthError,
    isHealthy: systemHealth?.status === 'healthy',
    isDegraded: systemHealth?.status === 'degraded',
    isUnhealthy: systemHealth?.status === 'unhealthy' || systemHealth?.status === 'error',
    isStarting: systemHealth?.status === 'starting',

    // Circuit breaker state
    circuitBreakers,
    cbSummary,
    cbLoading,
    cbError,
    hasOpenBreakers: cbSummary.open > 0,
    hasHalfOpenBreakers: cbSummary.half_open > 0,

    // Manual refresh functions
    refreshHealth: fetchHealth,
    refreshCircuitBreakers: fetchCircuitBreakers,
    
    // Circuit breaker actions
    resetCircuitBreaker,
  }

  return (
    <SystemStateContext.Provider value={value}>
      {children}
    </SystemStateContext.Provider>
  )
}

/**
 * Hook to access system state
 */
export function useSystemState() {
  const context = useContext(SystemStateContext)
  if (!context) {
    throw new Error('useSystemState must be used within SystemStateProvider')
  }
  return context
}
