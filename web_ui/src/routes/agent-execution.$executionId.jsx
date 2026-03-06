import { createFileRoute, useNavigate } from '@tanstack/react-router'
import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { RefreshCw } from 'lucide-react'
import Header from '../components/Header'
import LiveLogs from '../components/LiveLogs'
import { useSocket } from '../contexts/SocketContext'
import { normalizeTimestamp, formatTimestamp, mergeAgentExecutionEvents, mergeObjectStable, mergeArrayByIdStable } from '../utils/eventMerging'
import TokenUsagePanel from '../components/TokenUsagePanel'
import AgentExecutionNavBar from '../components/AgentExecutionNavBar'
import AgentExecutionState from '../components/AgentExecutionState'

function AgentExecutionView() {
  const { executionId } = Route.useParams()
  const searchParams = Route.useSearch()
  const navigate = useNavigate()
  const { logs: allLogs } = useSocket()
  const [executionData, setExecutionData] = useState(null)
  const [executionLogs, setExecutionLogs] = useState([])
  const [promptEvent, setPromptEvent] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [autoScroll, setAutoScroll] = useState(true)

  // Agent execution navigation state
  const [pipelineRunId, setPipelineRunId] = useState(null)
  const [pipelineExecutions, setPipelineExecutions] = useState([])
  const [pipelineEvents, setPipelineEvents] = useState([])
  const [autoAdvance, setAutoAdvance] = useState(searchParams.autoAdvance ?? false)
  const [loadingExecutions, setLoadingExecutions] = useState(false)

  // Refs to prevent concurrent fetches and track state
  const isFetchingExecutionsRef = useRef(false)
  const pipelineRunIdRef = useRef(pipelineRunId)
  const lastProcessedSocketEventRef = useRef(null)
  const previousExecutionsLengthRef = useRef(0)

  // Update pipelineRunId ref when it changes
  useEffect(() => {
    pipelineRunIdRef.current = pipelineRunId
  }, [pipelineRunId])

  // Fetch execution data
  const fetchExecutionData = useCallback(async (isInitialLoad = false) => {
    try {
      // Only show loading spinner on initial load, not on background refreshes
      if (isInitialLoad) {
        setLoading(true)
      }
      const response = await fetch(`/api/agent-execution/${executionId}`)
      const data = await response.json()

      if (data.success) {
        // Use stable merge to prevent unnecessary re-renders
        setExecutionData(current => mergeObjectStable(current, data.execution))
        setExecutionLogs(current => mergeArrayByIdStable(current, data.logs || [], 'id'))
        setPromptEvent(current => mergeObjectStable(current, data.prompt_event || null))
        setError(null)

        // Extract pipeline_run_id from execution data
        if (data.execution?.pipeline_run_id) {
          setPipelineRunId(data.execution.pipeline_run_id)
        }
      } else {
        setError(data.error || 'Failed to load execution data')
      }
    } catch (err) {
      console.error('Error fetching execution data:', err)
      setError('Failed to load execution data')
    } finally {
      if (isInitialLoad) {
        setLoading(false)
      }
    }
  }, [executionId])

  // Fetch all executions in the pipeline run
  const fetchPipelineExecutions = useCallback(async (runId, isInitialLoad = false) => {
    if (!runId) return

    // Guard against concurrent fetches
    if (isFetchingExecutionsRef.current) {
      console.log('[AgentExecution] Skipping pipeline executions fetch - already in progress')
      return
    }

    try {
      isFetchingExecutionsRef.current = true
      // Only show loading spinner on initial load, not on background refreshes
      if (isInitialLoad) {
        setLoadingExecutions(true)
      }
      const response = await fetch(`/pipeline-run-events?pipeline_run_id=${runId}`)
      const data = await response.json()

      if (data.success) {
        // Extract agent executions from events
        const executionMap = new Map()

        data.events.forEach(event => {
          if (event.event_type === 'agent_initialized') {
            const execId = event.agent_execution_id
            if (execId && !executionMap.has(execId)) {
              executionMap.set(execId, {
                execution_id: execId,
                agent_name: event.agent,
                started_at: event.timestamp,
                status: 'running'
              })
            }
          } else if (event.event_type === 'agent_completed' || event.event_type === 'agent_failed') {
            const execId = event.agent_execution_id
            if (execId && executionMap.has(execId)) {
              executionMap.get(execId).status = event.event_type === 'agent_completed' ? 'completed' : 'failed'
              executionMap.get(execId).ended_at = event.timestamp
            }
          }
        })

        const executions = Array.from(executionMap.values())
        // Sort by start time
        executions.sort((a, b) => new Date(a.started_at) - new Date(b.started_at))

        console.log('[AgentExecution] Found pipeline executions:', executions.length)
        // Use stable merge to prevent unnecessary re-renders
        setPipelineExecutions(current => mergeArrayByIdStable(current, executions, 'execution_id'))

        // Store all pipeline events for repair cycle status
        if (data.events) {
          setPipelineEvents(data.events)
        }
      }
    } catch (err) {
      console.error('Error fetching pipeline executions:', err)
    } finally {
      if (isInitialLoad) {
        setLoadingExecutions(false)
      }
      isFetchingExecutionsRef.current = false
    }
  }, [])

  useEffect(() => {
    fetchExecutionData(true) // Pass true for initial load
  }, [fetchExecutionData])

  // Fetch pipeline executions when pipeline_run_id is available
  useEffect(() => {
    if (pipelineRunId) {
      fetchPipelineExecutions(pipelineRunId, true) // Pass true for initial load
    }
  }, [pipelineRunId, fetchPipelineExecutions])

  // Periodic refresh to detect new agent executions
  useEffect(() => {
    if (!pipelineRunId) return

    const intervalId = setInterval(() => {
      console.log('[AgentExecution] Periodic refresh of pipeline executions')
      // Use ref to get current pipeline run ID
      if (pipelineRunIdRef.current) {
        fetchPipelineExecutions(pipelineRunIdRef.current, false) // Background refresh, no loading spinner
      }
    }, 10000) // Refresh every 10 seconds

    return () => clearInterval(intervalId)
  }, [fetchPipelineExecutions, pipelineRunId])

  // WebSocket event handling to detect new agent executions
  useEffect(() => {
    if (allLogs.length > 0 && pipelineRunIdRef.current) {
      const latestLog = allLogs[allLogs.length - 1]

      // Skip if we've already processed this event
      if (lastProcessedSocketEventRef.current === latestLog) {
        return
      }
      lastProcessedSocketEventRef.current = latestLog

      // Refresh pipeline executions on agent lifecycle events
      if (latestLog.event_type === 'agent_initialized' ||
          latestLog.event_type === 'agent_completed' ||
          latestLog.event_type === 'agent_failed') {
        console.log('[AgentExecution] WebSocket event detected:', latestLog.event_type)
        fetchPipelineExecutions(pipelineRunIdRef.current, false) // Background refresh from WebSocket event
      }
    }
  }, [allLogs, fetchPipelineExecutions])

  // Get current execution index
  const currentExecutionIndex = useMemo(() => {
    return pipelineExecutions.findIndex(exec => exec.execution_id === executionId)
  }, [pipelineExecutions, executionId])

  // Auto-advance to latest execution when new executions appear
  useEffect(() => {
    const currentLength = pipelineExecutions.length
    const previousLength = previousExecutionsLengthRef.current

    // Only auto-advance when length actually increases (new execution added)
    if (autoAdvance && currentLength > previousLength && currentLength > 0) {
      const latestExecution = pipelineExecutions[pipelineExecutions.length - 1]
      // Only auto-advance if we're not already viewing the latest execution
      if (latestExecution.execution_id !== executionId) {
        console.log('[AgentExecution] Auto-advancing to latest execution:', latestExecution.execution_id)
        navigate({
          to: '/agent-execution/$executionId',
          params: { executionId: latestExecution.execution_id },
          search: { autoAdvance }
        })
      }
    }

    // Update ref after processing
    previousExecutionsLengthRef.current = currentLength
  }, [pipelineExecutions, autoAdvance, executionId, navigate])

  // Navigation handlers
  const handlePreviousExecution = useCallback(() => {
    if (currentExecutionIndex > 0) {
      setAutoAdvance(false)
      const prevExecution = pipelineExecutions[currentExecutionIndex - 1]
      navigate({
        to: '/agent-execution/$executionId',
        params: { executionId: prevExecution.execution_id },
        search: { autoAdvance: false }
      })
    }
  }, [currentExecutionIndex, pipelineExecutions, navigate])

  const handleNextExecution = useCallback(() => {
    if (currentExecutionIndex < pipelineExecutions.length - 1) {
      setAutoAdvance(false)
      const nextExecution = pipelineExecutions[currentExecutionIndex + 1]
      navigate({
        to: '/agent-execution/$executionId',
        params: { executionId: nextExecution.execution_id },
        search: { autoAdvance: false }
      })
    }
  }, [currentExecutionIndex, pipelineExecutions, navigate])

  const handleAutoAdvanceToggle = useCallback((e) => {
    const checked = e.target.checked
    setAutoAdvance(checked)
    if (checked && pipelineExecutions.length > 0) {
      const latestExecution = pipelineExecutions[pipelineExecutions.length - 1]
      if (latestExecution.execution_id !== executionId) {
        navigate({
          to: '/agent-execution/$executionId',
          params: { executionId: latestExecution.execution_id },
          search: { autoAdvance: true }
        })
      }
    }
  }, [pipelineExecutions, executionId, navigate])

  // Merge API logs with live WebSocket updates and build agent state
  const { executionEvents, agentState, mergedLogs, mergedPipelineEvents } = useMemo(() => {
    if (!executionData) {
      return { executionEvents: [], agentState: {}, mergedLogs: [], mergedPipelineEvents: [] }
    }

    const agent = executionData.agent
    const startTimestamp = normalizeTimestamp(executionData.started_at)
    const endTimestamp = executionData.ended_at ? normalizeTimestamp(executionData.ended_at) : null

    console.log('[AgentExecution] useMemo triggered:', {
      agent,
      executionLogsCount: executionLogs.length,
      allLogsCount: allLogs.length,
      startTimestamp,
      endTimestamp
    })

    // Use shared utility to merge API logs with WebSocket events
    const logs = mergeAgentExecutionEvents(executionLogs, allLogs, executionData)

    console.log('[AgentExecution] Merged logs:', {
      mergedCount: logs.length,
      apiLogsCount: executionLogs.length,
      newLogsFromWebSocket: logs.length - executionLogs.length
    })

    // Filter events for this execution (for display purposes only, not for state)
    const filteredEvents = allLogs.filter(event => {
      if (event.agent !== agent) return false
      const eventTimestamp = normalizeTimestamp(event.timestamp)
      if (!eventTimestamp || !startTimestamp) return false
      if (eventTimestamp < startTimestamp) return false
      if (endTimestamp && eventTimestamp > endTimestamp) return false
      return true
    })

    // Merge pipeline events with live logs for repair cycle status
    let pipelineEventsList = [...pipelineEvents]
    if (pipelineRunId) {
      // Find timestamp of last historical event
      let lastHistTimestamp = 0
      if (pipelineEvents.length > 0) {
        const lastEvent = pipelineEvents[pipelineEvents.length - 1]
        lastHistTimestamp = normalizeTimestamp(lastEvent.timestamp || lastEvent.created_at) || 0
      }

      // Add new live events for this pipeline run
      const livePipelineEvents = allLogs.filter(event => {
        // Check if event belongs to this pipeline run
        const eventRunId = event.pipeline_run_id || (event.data && event.data.pipeline_run_id)
        if (eventRunId !== pipelineRunId) return false

        // Check if it's newer than historical data
        const eventTimestamp = normalizeTimestamp(event.timestamp)
        return eventTimestamp > lastHistTimestamp
      })

      if (livePipelineEvents.length > 0) {
        pipelineEventsList = [...pipelineEventsList, ...livePipelineEvents]
      }
    }

    // Build agent state from merged logs (API + WebSocket)
    let lastTodoWrite = null
    let lastTextMessage = null
    let lastToolCall = null
    let previousToolCall = null
    let previousToolResult = null

    // Process merged logs in reverse (most recent first) to find latest states
    for (let i = logs.length - 1; i >= 0; i--) {
      const log = logs[i]
      const event = log.raw_event?.event
      const normalizedLogTimestamp = normalizeTimestamp(log.timestamp)

      if (event?.type === 'assistant' && event?.message?.content) {
        const contents = Array.isArray(event.message.content)
          ? event.message.content
          : [event.message.content]

        for (const item of contents) {
          if (item.type === 'text' && !lastTextMessage && item.text?.trim()) {
            lastTextMessage = {
              text: item.text,
              timestamp: normalizedLogTimestamp,
              agent: log.agent
            }
          } else if (item.type === 'tool_use') {
            if (item.name === 'TodoWrite' && !lastTodoWrite) {
              lastTodoWrite = {
                todos: item.input?.todos || [],
                timestamp: normalizedLogTimestamp,
                agent: log.agent
              }
            } else if (item.name !== 'TodoWrite') {
              if (!lastToolCall) {
                lastToolCall = {
                  name: item.name,
                  input: item.input,
                  timestamp: normalizedLogTimestamp,
                  agent: log.agent
                }
              } else if (!previousToolCall) {
                previousToolCall = {
                  name: item.name,
                  input: item.input,
                  timestamp: normalizedLogTimestamp,
                  agent: log.agent
                }
              }
            }
          }
        }
      }

      if (lastTodoWrite && lastTextMessage && lastToolCall && previousToolCall) break
    }

    // Find tool result for previous tool call
    if (previousToolCall) {
      for (let i = logs.length - 1; i >= 0; i--) {
        const log = logs[i]
        const event = log.raw_event?.event
        const normalizedLogTimestamp = normalizeTimestamp(log.timestamp)

        const isBeforeOrAtLastToolCall = normalizedLogTimestamp && lastToolCall.timestamp && normalizedLogTimestamp <= lastToolCall.timestamp
        const isAfterOrAtPreviousToolCall = normalizedLogTimestamp && previousToolCall.timestamp && normalizedLogTimestamp >= previousToolCall.timestamp

        if (isBeforeOrAtLastToolCall && isAfterOrAtPreviousToolCall && event?.type === 'user' && event?.message?.content) {
          const contents = Array.isArray(event.message.content)
            ? event.message.content
            : [event.message.content]

          for (const item of contents) {
            if (item.type === 'tool_result') {
              const timeDiff = Math.abs(normalizedLogTimestamp - previousToolCall.timestamp)
              if (timeDiff < 60) {
                previousToolResult = {
                  content: item.content,
                  timestamp: normalizedLogTimestamp,
                  agent: log.agent
                }
                break
              }
            }
          }
          if (previousToolResult) break
        }
      }
    }

    // Use prompt_event from API response instead of searching events
    let inputPrompt = null
    if (promptEvent && promptEvent.raw_event?.data?.prompt) {
      const eventTimestamp = normalizeTimestamp(promptEvent.timestamp)
      if (eventTimestamp) {
        inputPrompt = {
          text: promptEvent.raw_event.data.prompt,
          timestamp: eventTimestamp,
          agent: promptEvent.agent_name || promptEvent.agent
        }
      }
    }

    return {
      executionEvents: filteredEvents,
      agentState: {
        lastTodoWrite,
        lastTextMessage,
        lastToolCall,
        previousToolCall,
        previousToolResult,
        inputPrompt
      },
      mergedLogs: logs,
      mergedPipelineEvents: pipelineEventsList,
    }
  }, [executionData, executionLogs, allLogs, promptEvent, pipelineEvents, pipelineRunId])

  if (loading) {
    return (
      <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
        <div className="flex items-center justify-center h-96">
          <RefreshCw className="w-8 h-8 animate-spin text-gh-accent-primary" />
        </div>
      </div>
    )
  }

  if (error || !executionData) {
    return (
      <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">
        <div className="flex items-center justify-center h-96">
          <div className="text-center">
            <p className="text-gh-danger text-lg mb-2">Error</p>
            <p className="text-gh-fg-muted">{error || 'Execution not found'}</p>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen p-5 bg-gh-canvas text-gh-fg">

      <Header />

      <AgentExecutionNavBar
        pipelineRunId={pipelineRunId}
        pipelineExecutions={pipelineExecutions}
        currentExecutionIndex={currentExecutionIndex}
        executionData={executionData}
        autoAdvance={autoAdvance}
        onPrevious={handlePreviousExecution}
        onNext={handleNextExecution}
        onAutoAdvanceToggle={handleAutoAdvanceToggle}
      />

      <TokenUsagePanel logs={mergedLogs} promptText={agentState.inputPrompt?.text} />

      <AgentExecutionState
        executionId={executionId}
        executionData={executionData}
        pipelineRunId={pipelineRunId}
        agentState={agentState}
        mergedPipelineEvents={mergedPipelineEvents}
      />

      {/* Live Logs Section */}
      <LiveLogs
        logs={mergedLogs}
        title="Live Logs for Execution"
        showClearButton={false}
        formatTimestamp={(timestamp) => formatTimestamp(normalizeTimestamp(timestamp))}
        minHeight="300px"
        maxHeight="50vh"
        onAutoScrollChange={setAutoScroll}
        headerControls={
          <span className="text-xs text-gh-fg-muted">
            {mergedLogs.length} log entries
            {mergedLogs.length > executionLogs.length && (
              <span className="ml-1 text-gh-success">
                (+{mergedLogs.length - executionLogs.length} live)
              </span>
            )}
          </span>
        }
      />
    </div>
  )
}

export const Route = createFileRoute('/agent-execution/$executionId')({
  component: AgentExecutionView,
  validateSearch: (search) => {
    return {
      autoAdvance: search.autoAdvance === true || search.autoAdvance === 'true'
    }
  },
})
