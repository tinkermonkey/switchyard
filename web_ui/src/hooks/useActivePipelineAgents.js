/**
 * Hook to fetch active agents from pipeline runs (source of truth)
 *
 * This replaces the old event-based active agent detection with a more reliable
 * pipeline-centric approach that queries Elasticsearch for actual running agents.
 *
 * Data Flow:
 * 1. Poll /api/active-agents endpoint every 5 seconds
 * 2. Backend queries active pipeline runs from Elasticsearch
 * 3. For each pipeline, find agents with agent_initialized but no completion event
 * 4. Return complete agent data with project, issue, container info
 *
 * Benefits over event-based approach:
 * - More accurate: Uses actual pipeline state, not event stream inference
 * - More complete: Includes all active agents, not limited to last N events
 * - More reliable: Elasticsearch queries are idempotent and consistent
 */

import { useState, useEffect } from 'react'

export function useActivePipelineAgents() {
  const [loading, setLoading] = useState(true)
  const [agents, setAgents] = useState([])
  const [error, setError] = useState(null)
  const [lastFetch, setLastFetch] = useState(null)

  useEffect(() => {
    let isMounted = true
    let pollInterval = null

    const fetchActiveAgents = async () => {
      try {
        const response = await fetch('/api/active-agents')

        if (!response.ok) {
          throw new Error(`HTTP ${response.status}: ${response.statusText}`)
        }

        const data = await response.json()

        if (isMounted) {
          if (data.success) {
            setAgents(data.agents || [])
            setError(null)
            setLastFetch(new Date())
          } else {
            throw new Error(data.error || 'Unknown error fetching active agents')
          }
          setLoading(false)
        }
      } catch (err) {
        console.error('[useActivePipelineAgents] Error fetching active agents:', err)
        if (isMounted) {
          setError(err.message)
          setLoading(false)
        }
      }
    }

    // Initial fetch
    fetchActiveAgents()

    // Poll every 5 seconds
    pollInterval = setInterval(fetchActiveAgents, 5000)

    // Cleanup on unmount
    return () => {
      isMounted = false
      if (pollInterval) {
        clearInterval(pollInterval)
      }
    }
  }, [])

  // Group agents by project (for potential future use)
  const agentsByProject = agents.reduce((acc, agent) => {
    const project = agent.project || 'unknown'
    if (!acc[project]) {
      acc[project] = []
    }
    acc[project].push(agent)
    return acc
  }, {})

  // Count containerized vs native agents
  const agentStats = agents.reduce(
    (stats, agent) => {
      if (agent.is_containerized) {
        stats.containerized++
      } else {
        stats.native++
      }
      return stats
    },
    { containerized: 0, native: 0 }
  )

  return {
    // Agent data
    agents,
    agentCount: agents.length,
    hasActiveAgents: agents.length > 0,

    // Grouped data (for backward compatibility with old hook)
    agentsByProject,
    agentStats,

    // State
    loading,
    error,
    lastFetch,
  }
}
