import { createContext, useContext, useState } from 'react'
import { agentApi } from '../services/agentApi'
import { useActivePipelineAgents } from '../hooks/useActivePipelineAgents'

const AgentStateContext = createContext()

/**
 * AgentStateProvider - Manages agent state from pipeline runs (source of truth)
 * and provides agent control operations
 *
 * REDESIGNED: Now uses pipeline-centric approach instead of event-based derivation
 * for more accurate and reliable active agent tracking.
 */
export function AgentStateProvider({ children }) {
  const [killingAgents, setKillingAgents] = useState(new Set())
  const [actionError, setActionError] = useState(null)

  // Get active agents from pipeline runs (NEW: source of truth)
  const { agents: activeAgents, loading, error: fetchError } = useActivePipelineAgents()

  // Kill agent operation
  const killAgent = async (containerName) => {
    if (!containerName) {
      const error = 'Cannot kill agent: no container name provided'
      setActionError(error)
      throw new Error(error)
    }

    // Add to killing set
    setKillingAgents((prev) => new Set([...prev, containerName]))
    setActionError(null)

    try {
      const result = await agentApi.killAgent(containerName)

      if (result.success) {
        // Agent will be removed from list automatically when events update
        return { success: true }
      } else {
        const error = result.error || 'Failed to kill agent'
        setActionError(error)
        throw new Error(error)
      }
    } catch (error) {
      console.error('Error killing agent:', error)
      setActionError(error.message)
      throw error
    } finally {
      // Remove from killing set
      setKillingAgents((prev) => {
        const next = new Set(prev)
        next.delete(containerName)
        return next
      })
    }
  }

  // Check if a specific agent is being killed
  const isKillingAgent = (containerName) => {
    return killingAgents.has(containerName)
  }

  // Clear error
  const clearError = () => {
    setActionError(null)
  }

  const value = {
    // Agent state
    activeAgents,
    activeAgentCount: activeAgents.length,

    // Agent operations
    killAgent,
    isKillingAgent,
    isKillingAny: killingAgents.size > 0,

    // Loading state (NEW)
    loading,

    // Error handling
    actionError,
    fetchError,
    clearError,
  }

  return (
    <AgentStateContext.Provider value={value}>
      {children}
    </AgentStateContext.Provider>
  )
}

/**
 * Hook to access agent state
 */
export function useAgentState() {
  const context = useContext(AgentStateContext)
  if (!context) {
    throw new Error('useAgentState must be used within AgentStateProvider')
  }
  return context
}
