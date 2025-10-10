/**
 * Selector hook for circuit breaker data
 */
import { useMemo } from 'react'
import { useSystemState } from '../contexts/SystemStateContext'

export function useCircuitBreakers() {
  const {
    circuitBreakers,
    cbSummary,
    cbLoading,
    cbError,
    hasOpenBreakers,
    hasHalfOpenBreakers,
    refreshCircuitBreakers,
  } = useSystemState()

  // Get breakers that are not closed
  const problematicBreakers = useMemo(() => {
    return circuitBreakers.filter((cb) => cb.state !== 'closed')
  }, [circuitBreakers])

  return {
    // Raw circuit breaker data
    circuitBreakers,
    summary: cbSummary,

    // Loading and error states
    loading: cbLoading,
    error: cbError,

    // Status flags
    hasOpenBreakers,
    hasHalfOpenBreakers,
    allHealthy: !hasOpenBreakers && !hasHalfOpenBreakers,

    // Derived data
    problematicBreakers,
    openCount: cbSummary.open,
    halfOpenCount: cbSummary.half_open,
    healthyCount: cbSummary.healthy,

    // Actions
    refresh: refreshCircuitBreakers,
  }
}
