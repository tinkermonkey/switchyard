import { createContext, useContext, useState, useMemo } from 'react'
import { useSocket } from './SocketContext'
import { agentApi } from '../services/agentApi'
import { deriveActiveAgentsFromEvents } from '../utils/stateHelpers'

const AgentStateContext = createContext()

/**
 * AgentStateProvider - Manages agent state derived from WebSocket events
 * and provides agent control operations
 */
export function AgentStateProvider({ children }) {
  const { events } = useSocket()
  const [killingAgents, setKillingAgents] = useState(new Set())
  const [actionError, setActionError] = useState(null)

  // Derive active agents from WebSocket events
  const activeAgents = useMemo(() => {
    return deriveActiveAgentsFromEvents(events)
  }, [events])

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

    // Error handling
    actionError,
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
