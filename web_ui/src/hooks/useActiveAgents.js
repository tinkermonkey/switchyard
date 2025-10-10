/**
 * Selector hook for active agent data
 */
import { useMemo } from 'react'
import { useAgentState } from '../contexts/AgentStateContext'

export function useActiveAgents() {
  const { activeAgents, activeAgentCount } = useAgentState()

  // Group agents by project
  const agentsByProject = useMemo(() => {
    const grouped = {}
    activeAgents.forEach((agent) => {
      const project = agent.project || 'unknown'
      if (!grouped[project]) {
        grouped[project] = []
      }
      grouped[project].push(agent)
    })
    return grouped
  }, [activeAgents])

  // Count containerized vs native agents
  const agentStats = useMemo(() => {
    const stats = {
      containerized: 0,
      native: 0,
    }
    activeAgents.forEach((agent) => {
      if (agent.is_containerized) {
        stats.containerized++
      } else {
        stats.native++
      }
    })
    return stats
  }, [activeAgents])

  return {
    // Agent data
    agents: activeAgents,
    agentCount: activeAgentCount,
    hasActiveAgents: activeAgentCount > 0,

    // Grouped data
    agentsByProject,
    agentStats,
  }
}
