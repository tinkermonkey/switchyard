/**
 * Helper functions for state transformations and data processing
 */

/**
 * Calculate active agents from event stream
 * @param {Array} events - Array of events from WebSocket
 * @returns {Array} Array of active agent objects
 */
export function deriveActiveAgentsFromEvents(events) {
  if (!events || events.length === 0) {
    return []
  }

  const agentStates = new Map()

  // Process events from oldest to newest to build up state correctly
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i]
    const agent = event.agent
    const eventType = event.event_type

    if (!agent) continue

    // Track agent state changes
    if (eventType === 'agent_initialized') {
      // Always set/update agent state when initialized
      agentStates.set(agent, {
        agent,
        project: event.project,
        task_id: event.task_id,
        issue_number: event.data?.issue_number || event.data?.discussion_id || 'unknown',
        branch_name: event.data?.branch_name || null,
        started_at: event.timestamp,
        status: 'running',
        container_name: event.data?.container_name || null,
        is_containerized: !!event.data?.container_name,
      })
    } else if (eventType === 'agent_completed' || eventType === 'agent_failed') {
      // Mark agent as completed/failed
      const agentState = agentStates.get(agent)
      if (agentState) {
        agentState.status = eventType === 'agent_completed' ? 'completed' : 'failed'
      }
    }
  }

  // Filter to only show running agents
  const runningAgents = Array.from(agentStates.values()).filter((agent) => agent.status === 'running')
  
  // Debug logging (can be removed later)
  if (typeof window !== 'undefined') {
    console.debug(`[deriveActiveAgentsFromEvents] Processed ${events.length} events, found ${runningAgents.length} active agents`)
  }
  
  return runningAgents
}

/**
 * Parse a timestamp into a Date object, handling multiple formats
 * Assumes timestamps without timezone indicators are UTC (as they are from our backend)
 * @param {number|string} timestamp - Unix timestamp (seconds or milliseconds) or ISO string
 * @returns {Date|null} Parsed Date object or null if invalid
 */
export function parseTimestamp(timestamp) {
  if (!timestamp) {
    return null
  }

  try {
    let date
    
    // Handle different timestamp formats
    if (typeof timestamp === 'string') {
      // If the ISO string doesn't have a timezone indicator, assume it's UTC
      // Backend sends UTC timestamps without 'Z' suffix
      let timestampStr = timestamp
      if (!timestamp.includes('Z') && !timestamp.includes('+') && !timestamp.includes('-', 10)) {
        // Add 'Z' to indicate UTC if no timezone is present
        timestampStr = timestamp + 'Z'
      }
      date = new Date(timestampStr)
    } else if (timestamp > 10000000000) {
      // Timestamp in milliseconds
      date = new Date(timestamp)
    } else {
      // Timestamp in seconds
      date = new Date(timestamp * 1000)
    }
    
    // Validate the date
    if (isNaN(date.getTime())) {
      console.warn('[parseTimestamp] Invalid timestamp:', timestamp)
      return null
    }
    
    return date
  } catch (error) {
    console.error('[parseTimestamp] Error parsing timestamp:', error, 'timestamp:', timestamp)
    return null
  }
}

/**
 * Calculate runtime in milliseconds from a timestamp to now
 * @param {number|string} startedAt - Unix timestamp (seconds or milliseconds) or ISO string
 * @returns {number} Runtime in milliseconds, or 0 if invalid
 */
export function getRuntimeMs(startedAt) {
  const start = parseTimestamp(startedAt)
  if (!start) {
    return 0
  }
  
  const now = new Date()
  const diffMs = now - start
  
  // Handle negative durations (future timestamps)
  if (diffMs < 0) {
    console.warn('[getRuntimeMs] Negative duration - timestamp is in the future:', startedAt)
    return 0
  }
  
  return diffMs
}

/**
 * Format duration from a Unix timestamp to now
 * @param {number|string} startedAt - Unix timestamp (seconds or milliseconds) or ISO string
 * @returns {string} Formatted duration
 */
export function formatDuration(startedAt) {
  const diffMs = getRuntimeMs(startedAt)
  
  if (diffMs === 0) {
    return '0s'
  }
  
  const diffMins = Math.floor(diffMs / 60000)
  const diffSecs = Math.floor((diffMs % 60000) / 1000)

  if (diffMins > 0) {
    return `${diffMins}m ${diffSecs}s`
  }
  return `${diffSecs}s`
}

/**
 * Get unhealthy components from system health data
 * @param {Object} systemHealth - Health check data
 * @returns {Array} Array of [key, check] tuples for unhealthy components
 */
export function getUnhealthyComponents(systemHealth) {
  if (!systemHealth?.orchestrator?.checks) return []
  
  return Object.entries(systemHealth.orchestrator.checks).filter(
    ([, check]) => !check.healthy
  )
}

/**
 * Format token count (e.g., 1500000 -> "1.5M")
 * @param {number} tokens - Token count
 * @returns {string} Formatted token string
 */
export function formatTokens(tokens) {
  if (!tokens) return '0'
  const millions = tokens / 1000000
  return millions >= 1000 ? `${(millions / 1000).toFixed(2)}B` : `${millions.toFixed(1)}M`
}
