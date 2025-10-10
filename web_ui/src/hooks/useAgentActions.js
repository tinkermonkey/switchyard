/**
 * Selector hook for agent actions/operations
 */
import { useAgentState } from '../contexts/AgentStateContext'

export function useAgentActions() {
  const {
    killAgent,
    isKillingAgent,
    isKillingAny,
    actionError,
    clearError,
  } = useAgentState()

  return {
    // Actions
    killAgent,

    // Status checks
    isKillingAgent,
    isKillingAny,

    // Error handling
    error: actionError,
    clearError,
    hasError: !!actionError,
  }
}
