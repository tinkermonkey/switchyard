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
    agent: wsEvent.agent || agent,  // CRITICAL: Use executionData.agent as fallback
    task_id: wsEvent.task_id || taskId,  // CRITICAL: Use executionData.task_id as fallback
    event_type: 'claude_stream',  // WebSocket events don't have event_type at top level
    raw_event: { event: wsEvent.event }  // Use wsEvent.event, not wsEvent.data
  }))
  
  // Merge arrays
  const allEvents = [...logs, ...convertedWebSocketLogs]

  console.log('[EventMerging] Before deduplication:', {
    totalEvents: allEvents.length,
    apiLogs: logs.length,
    convertedWS: convertedWebSocketLogs.length,
    sampleKeys: allEvents.slice(0, 3).map(log => {
      const event = log.event || log.raw_event?.event
      const msgId = event?.message?.id || 'no-id'
      return `${log.timestamp}-${msgId.substring(0, 20)}`
    })
  })

  // Deduplicate using unique keys to prevent duplicate log entries
  // This is necessary because merging happens on every WebSocket event,
  // and without deduplication, previously merged events would be re-included
  const seenKeys = new Set()
  const deduplicated = allEvents.filter((log, index) => {
    // CRITICAL: Normalize timestamp to number for consistent key generation
    // Timestamps can be ISO strings or Unix timestamps - normalize to Unix timestamp
    const normalizedTs = normalizeTimestamp(log.timestamp)
    const logAgent = log.agent || agent  // Use executionData.agent as fallback
    const logTaskId = log.task_id || taskId || 'notask'  // Use executionData.task_id as fallback
    const eventType = log.event_type || 'unknown'

    // Extract event content for better uniqueness
    const event = log.event || log.raw_event?.event
    let contentHash = ''
    if (event) {
      // Use event type and message ID if available for stronger uniqueness
      const msgType = event.type || ''
      const msgId = event.message?.id || ''
      const msgModel = event.message?.model || ''
      contentHash = `${msgType}-${msgId}-${msgModel}`
    }

    // Use normalized timestamp and logAgent in key for consistency
    const key = `${normalizedTs}-${logAgent}-${logTaskId}-${eventType}-${contentHash}`

    // Debug first few items
    if (index < 3) {
      console.log(`[EventMerging] Key ${index}:`, {
        key: key.substring(0, 120),
        normalizedTs,
        rawTimestamp: log.timestamp,
        logAgent,
        logTaskId: logTaskId.substring(0, 50),
        eventType,
        contentHash: contentHash.substring(0, 50)
      })
    }

    if (seenKeys.has(key)) {
      console.log('[EventMerging] 🚫 Filtering duplicate log:', {
        key: key.substring(0, 100),
        normalizedTs,
        rawTimestamp: log.timestamp,
        logAgent,
        logTaskId: logTaskId.substring(0, 50),
        contentHash: contentHash.substring(0, 50)
      })
      return false // Skip duplicate
    }
    seenKeys.add(key)
    return true
  })

  console.log('[EventMerging] After deduplication:', {
    before: allEvents.length,
    after: deduplicated.length,
    removed: allEvents.length - deduplicated.length
  })

  // Sort by timestamp
  logs = deduplicated.sort((a, b) => {
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

  console.log('[EventMerging] mergePipelineRunEvents called:', {
    pipelineRunId: pipelineRunId.substring(0, 8),
    apiEventsCount: apiEvents.length,
    webSocketEventsCount: webSocketEvents.length,
    startTimestamp,
    endTimestamp
  })

  // Start with API events as baseline
  let events = [...apiEvents]

  // Find the latest timestamp in API events to avoid duplicates
  const lastApiEventTimestamp = apiEvents.length > 0
    ? Math.max(...apiEvents.map(event => normalizeTimestamp(event.timestamp) || 0))
    : startTimestamp

  console.log('[EventMerging] Last API event timestamp:', lastApiEventTimestamp)

  // Filter WebSocket events for this pipeline run that are newer than API events
  const newWebSocketEvents = webSocketEvents.filter(event => {
    // Check if event is related to this pipeline run
    // Pipeline run events have pipeline_run_id in their data or at top level
    const eventPipelineRunId = event.data?.pipeline_run_id || event.pipeline_run_id

    // Log events for debugging (only first 3)
    if (webSocketEvents.indexOf(event) < 3) {
      console.log('[EventMerging] Checking WebSocket event:', {
        event_type: event.event_type,
        eventPipelineRunId: eventPipelineRunId?.substring(0, 8),
        targetPipelineRunId: pipelineRunId.substring(0, 8),
        matches: eventPipelineRunId === pipelineRunId,
        timestamp: normalizeTimestamp(event.timestamp)
      })
    }

    if (eventPipelineRunId !== pipelineRunId) return false

    const eventTimestamp = normalizeTimestamp(event.timestamp)
    if (!eventTimestamp || !startTimestamp) return false
    if (eventTimestamp < startTimestamp) return false
    if (endTimestamp && eventTimestamp > endTimestamp) return false
    // Only include events AFTER the last API event to avoid duplicates
    if (eventTimestamp <= lastApiEventTimestamp) {
      console.log('[EventMerging] Filtering out duplicate event:', {
        event_type: event.event_type,
        eventTimestamp,
        lastApiEventTimestamp
      })
      return false
    }
    return true
  })

  console.log('[EventMerging] Filtered WebSocket events:', {
    filteredCount: newWebSocketEvents.length,
    sampleEvents: newWebSocketEvents.slice(0, 3).map(e => ({
      event_type: e.event_type,
      timestamp: normalizeTimestamp(e.timestamp)
    }))
  })

  // WebSocket events are already in the correct format, just append them
  events = [...events, ...newWebSocketEvents]

  // Sort by timestamp
  events.sort((a, b) => {
    const tsA = normalizeTimestamp(a.timestamp) || 0
    const tsB = normalizeTimestamp(b.timestamp) || 0
    return tsA - tsB
  })

  console.log('[EventMerging] Final merged events count:', events.length)

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

/**
 * Deep comparison of two objects (shallow properties only)
 * Used to detect if data has actually changed to avoid unnecessary re-renders
 *
 * @param {Object} obj1 - First object
 * @param {Object} obj2 - Second object
 * @returns {boolean} True if objects are equal
 */
const shallowEqual = (obj1, obj2) => {
  if (obj1 === obj2) return true
  if (!obj1 || !obj2) return false

  const keys1 = Object.keys(obj1)
  const keys2 = Object.keys(obj2)

  if (keys1.length !== keys2.length) return false

  for (const key of keys1) {
    if (obj1[key] !== obj2[key]) return false
  }

  return true
}

/**
 * Intelligently merge a new array of items with an existing array,
 * preserving object references when data hasn't changed.
 * This prevents React from unnecessarily re-rendering components.
 *
 * @param {Array} existingItems - Current array of items
 * @param {Array} newItems - New array of items from API
 * @param {string} idKey - Key to use for identifying items (default: 'id')
 * @returns {Array} Merged array with stable object references
 */
export const mergeArrayByIdStable = (existingItems, newItems, idKey = 'id') => {
  if (!Array.isArray(existingItems) || existingItems.length === 0) {
    return newItems
  }
  if (!Array.isArray(newItems) || newItems.length === 0) {
    return newItems
  }

  // Create a map of existing items by ID for quick lookup
  const existingMap = new Map(existingItems.map(item => [item[idKey], item]))

  // Merge new items, reusing existing objects when data hasn't changed
  const merged = newItems.map(newItem => {
    const existingItem = existingMap.get(newItem[idKey])

    // If item exists and data is the same, reuse the existing object reference
    if (existingItem && shallowEqual(existingItem, newItem)) {
      return existingItem
    }

    // Otherwise use the new item (data has changed or it's a new item)
    return newItem
  })

  return merged
}

/**
 * Merge a single object, preserving the reference if data hasn't changed.
 * Useful for single entity updates like execution data, user profiles, etc.
 *
 * @param {Object} existingObject - Current object
 * @param {Object} newObject - New object from API
 * @returns {Object} The existing object if unchanged, otherwise the new object
 */
export const mergeObjectStable = (existingObject, newObject) => {
  // If no existing object, use new one
  if (!existingObject) return newObject

  // If no new object, keep existing
  if (!newObject) return existingObject

  // If data is the same, return existing object reference
  if (shallowEqual(existingObject, newObject)) {
    return existingObject
  }

  // Data has changed, return new object
  return newObject
}

/**
 * Merge an object map/dictionary, preserving object references for unchanged entries.
 * Useful for state like { [key]: value } where keys map to objects.
 *
 * @param {Object} existingMap - Current object map
 * @param {Object} updates - Object with keys to update
 * @returns {Object} Merged object map with stable references
 */
export const mergeObjectMapStable = (existingMap, updates) => {
  if (!existingMap || Object.keys(existingMap).length === 0) {
    return updates
  }
  if (!updates || Object.keys(updates).length === 0) {
    return existingMap
  }

  const result = { ...existingMap }
  let hasChanges = false

  // Update only changed entries
  Object.keys(updates).forEach(key => {
    const existingValue = existingMap[key]
    const newValue = updates[key]

    // Check if value has changed
    if (!existingValue || !shallowEqual(existingValue, newValue)) {
      result[key] = newValue
      hasChanges = true
    }
  })

  // Return existing map if nothing changed
  return hasChanges ? result : existingMap
}
