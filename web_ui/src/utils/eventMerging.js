/**
 * Utility functions for merging API data with live WebSocket events
 */

/**
 * Unified timestamp normalization function
 */
export const normalizeTimestamp = (timestamp) => {
  if (!timestamp) return null
  
  try {
    if (typeof timestamp === 'string') {
      const date = new Date(timestamp)
      if (isNaN(date.getTime())) {
        console.error('[EventMerging] Invalid ISO timestamp string:', timestamp)
        return null
      }
      return date.getTime() / 1000
    }
    
    if (typeof timestamp === 'number') {
      return timestamp > 10000000000 ? timestamp / 1000 : timestamp
    }
    
    console.error('[EventMerging] Unknown timestamp format:', timestamp, typeof timestamp)
    return null
  } catch (e) {
    console.error('[EventMerging] Error normalizing timestamp:', timestamp, e)
    return null
  }
}

/**
 * Merge API events with live WebSocket events for agent executions
 * 
 * @param {Array} apiLogs - Logs from API response
 * @param {Array} webSocketEvents - Live WebSocket events
 * @param {Object} executionData - Execution metadata (agent, started_at, ended_at)
 * @returns {Array} Merged and sorted logs
 */
export const mergeAgentExecutionEvents = (apiLogs, webSocketEvents, executionData) => {
  if (!executionData) return []
  
  const agent = executionData.agent
  const taskId = executionData.task_id
  const startTimestamp = normalizeTimestamp(executionData.started_at)
  const endTimestamp = executionData.ended_at ? normalizeTimestamp(executionData.ended_at) : null
  
  console.log('[EventMerging] mergeAgentExecutionEvents called:', {
    agent,
    taskId,
    apiLogsCount: apiLogs.length,
    webSocketEventsCount: webSocketEvents.length,
    startTimestamp,
    endTimestamp
  })
  
  // Start with API logs as baseline
  let logs = [...apiLogs]
  
  // Find the latest timestamp in API logs to avoid duplicates
  const lastApiLogTimestamp = apiLogs.length > 0
    ? Math.max(...apiLogs.map(log => normalizeTimestamp(log.timestamp) || 0))
    : startTimestamp
  
  console.log('[EventMerging] Last API log timestamp:', lastApiLogTimestamp)
  
  // Filter WebSocket events for this execution that are newer than API logs
  const newWebSocketEvents = webSocketEvents.filter(event => {
    // Must match both agent AND task_id
    if (event.agent !== agent || event.task_id !== taskId) {
      return false
    }
    const eventTimestamp = normalizeTimestamp(event.timestamp)
    if (!eventTimestamp || !startTimestamp) {
      return false
    }
    if (eventTimestamp < startTimestamp) {
      return false
    }
    if (endTimestamp && eventTimestamp > endTimestamp) {
      return false
    }
    // Only include events AFTER the last API log to avoid duplicates
    if (eventTimestamp <= lastApiLogTimestamp) {
      return false
    }
    return true
  })
  
  console.log('[EventMerging] Filtered WebSocket events:', {
    filteredCount: newWebSocketEvents.length,
    sampleEvent: newWebSocketEvents[0]
  })
  
  // Convert WebSocket events to log format and append
  // WebSocket events have structure: { agent, task_id, project, pipeline_run_id, timestamp, event: {...} }
  const convertedWebSocketLogs = newWebSocketEvents.map(wsEvent => ({
    timestamp: wsEvent.timestamp,
    agent: wsEvent.agent,
    event_type: 'claude_stream',  // WebSocket events don't have event_type at top level
    raw_event: { event: wsEvent.event }  // Use wsEvent.event, not wsEvent.data
  }))
  
  // Merge and sort by timestamp
  logs = [...logs, ...convertedWebSocketLogs].sort((a, b) => {
    const tsA = normalizeTimestamp(a.timestamp) || 0
    const tsB = normalizeTimestamp(b.timestamp) || 0
    return tsA - tsB
  })
  
  return logs
}

/**
 * Merge API events with live WebSocket events for pipeline runs
 * 
 * @param {Array} apiEvents - Events from API response
 * @param {Array} webSocketEvents - Live WebSocket events
 * @param {Object} pipelineRun - Pipeline run metadata (id, started_at, ended_at)
 * @returns {Array} Merged and sorted events
 */
export const mergePipelineRunEvents = (apiEvents, webSocketEvents, pipelineRun) => {
  if (!pipelineRun) return []
  
  const pipelineRunId = pipelineRun.id
  const startTimestamp = normalizeTimestamp(pipelineRun.started_at)
  const endTimestamp = pipelineRun.ended_at ? normalizeTimestamp(pipelineRun.ended_at) : null
  
  // Start with API events as baseline
  let events = [...apiEvents]
  
  // Find the latest timestamp in API events to avoid duplicates
  const lastApiEventTimestamp = apiEvents.length > 0
    ? Math.max(...apiEvents.map(event => normalizeTimestamp(event.timestamp) || 0))
    : startTimestamp
  
  // Filter WebSocket events for this pipeline run that are newer than API events
  const newWebSocketEvents = webSocketEvents.filter(event => {
    // Check if event is related to this pipeline run
    // Pipeline run events have pipeline_run_id in their data
    const eventPipelineRunId = event.data?.pipeline_run_id || event.pipeline_run_id
    if (eventPipelineRunId !== pipelineRunId) return false
    
    const eventTimestamp = normalizeTimestamp(event.timestamp)
    if (!eventTimestamp || !startTimestamp) return false
    if (eventTimestamp < startTimestamp) return false
    if (endTimestamp && eventTimestamp > endTimestamp) return false
    // Only include events AFTER the last API event to avoid duplicates
    if (eventTimestamp <= lastApiEventTimestamp) return false
    return true
  })
  
  // WebSocket events are already in the correct format, just append them
  events = [...events, ...newWebSocketEvents]
  
  // Sort by timestamp
  events.sort((a, b) => {
    const tsA = normalizeTimestamp(a.timestamp) || 0
    const tsB = normalizeTimestamp(b.timestamp) || 0
    return tsA - tsB
  })
  
  return events
}

/**
 * Format timestamp for display
 * 
 * @param {number|string} timestamp - Timestamp to format
 * @returns {string} Formatted timestamp string
 */
export const formatTimestamp = (timestamp) => {
  if (!timestamp) return ''
  
  try {
    const normalizedTimestamp = normalizeTimestamp(timestamp)
    if (!normalizedTimestamp) return 'Invalid date'
    
    const date = new Date(normalizedTimestamp * 1000)
    
    if (isNaN(date.getTime())) {
      console.error('[EventMerging] Invalid timestamp after normalization:', timestamp)
      return 'Invalid date'
    }
    
    // Use browser's local timezone for easier reading
    return date.toLocaleTimeString('en-US', { hour12: false })
  } catch (e) {
    console.error('[EventMerging] Error formatting timestamp:', timestamp, e)
    return 'Error'
  }
}
