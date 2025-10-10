/**
 * Selector hook for system health data
 */
import { useMemo } from 'react'
import { useSystemState } from '../contexts/SystemStateContext'
import { getUnhealthyComponents } from '../utils/stateHelpers'

export function useSystemHealth() {
  const {
    systemHealth,
    healthLoading,
    healthError,
    isHealthy,
    isDegraded,
    isUnhealthy,
    isStarting,
    refreshHealth,
  } = useSystemState()

  // Compute derived values
  const unhealthyComponents = useMemo(() => {
    return getUnhealthyComponents(systemHealth)
  }, [systemHealth])

  const checks = systemHealth?.orchestrator?.checks || {}

  return {
    // Raw health data
    systemHealth,
    checks,

    // Loading and error states
    loading: healthLoading,
    error: healthError,

    // Status flags
    isHealthy,
    isDegraded,
    isUnhealthy,
    isStarting,
    status: systemHealth?.status,

    // Derived data
    unhealthyComponents,
    unhealthyCount: unhealthyComponents.length,

    // Actions
    refresh: refreshHealth,
  }
}
