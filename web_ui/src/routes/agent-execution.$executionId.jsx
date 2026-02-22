import { createFileRoute, useNavigate, Link } from '@tanstack/react-router'
import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { RefreshCw, CheckCircle2, Circle, PlayCircle, ChevronDown, ChevronUp, ChevronLeft, ChevronRight, ArrowLeft } from 'lucide-react'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import LiveLogs from '../components/LiveLogs'
import HeaderActiveAgents from '../components/HeaderActiveAgents'
import HeaderSystemHealth from '../components/HeaderSystemHealth'
import HeaderCircuitBreakers from '../components/HeaderCircuitBreakers'
import { useSocket } from '../contexts/SocketContext'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { normalizeTimestamp, formatTimestamp, mergeAgentExecutionEvents, mergeObjectStable, mergeArrayByIdStable } from '../utils/eventMerging'
import RepairCycleStatus from '../components/RepairCycleStatus'

  const formatAgentName = (agentName) => {
    if (!agentName || typeof agentName !== 'string' || agentName.trim() === '') return 'Unknown Agent'
    return agentName.split('_').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ')
  }

const getToolResultLength = (content) => {
  if (!content) return 0
  if (typeof content === 'string') return content.length
  if (Array.isArray(content)) {
    return content.reduce((sum, item) => {
      if (typeof item === 'string') return sum + item.length
      if (item?.type === 'text') return sum + (item.text?.length || 0)
      return sum
    }, 0)
  }
  return 0
}

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
  const [isMessageExpanded, setIsMessageExpanded] = useState(false)
  const [isPromptExpanded, setIsPromptExpanded] = useState(false)
  const [isPreviousResultExpanded, setIsPreviousResultExpanded] = useState(false)
  const [isUsageExpanded, setIsUsageExpanded] = useState(false)
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
  const { executionEvents, agentState, mergedLogs, mergedPipelineEvents, tokenUsage } = useMemo(() => {
    if (!executionData) {
      return { executionEvents: [], agentState: {}, mergedLogs: [], mergedPipelineEvents: [], tokenUsage: { hasData: false } }
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
    
    // Derive token usage from merged logs
    let firstInput = null
    let cumulativeLastInput = 0
    let cumulativeLastOutput = 0
    let lastCacheRead = 0
    let lastCacheCreation = 0
    let lastDirectInput = 0
    const modelsUsed = new Set()
    const toolCallCounts = {}
    const toolIdToName = {}            // tool_use id → tool name, for matching results
    const toolResultChars = {}         // tool name → total result chars across all calls
    const toolContextGrowthTokens = {} // tool name → context growth tokens (delta effective_input, prev turn)
    let prevEffectiveInput = null      // effective_input from previous assistant turn
    let prevTurnTools = []             // tool names called in the previous assistant turn
    const tokenToolsAvailable = []
    const mcpServersAvailable = []

    for (const log of logs) {
      const evt = log.raw_event?.event
      if (!evt) continue

      if (evt.type === 'system' && evt.subtype === 'init') {
        if (Array.isArray(evt.tools) && tokenToolsAvailable.length === 0) {
          tokenToolsAvailable.push(...evt.tools.map(t => t.name || String(t)))
        }
        if (evt.mcp_servers && typeof evt.mcp_servers === 'object' && mcpServersAvailable.length === 0) {
          mcpServersAvailable.push(...Object.keys(evt.mcp_servers))
        }
      }

      if (evt.type === 'assistant' && evt.message?.usage) {
        const usage = evt.message.usage
        const model = evt.message.model
        if (model) modelsUsed.add(model)

        const inputDirect = usage.input_tokens || 0
        const cacheRead = usage.cache_read_input_tokens || 0
        const cacheCreation = usage.cache_creation_input_tokens || 0
        const effectiveInput = inputDirect + cacheRead + cacheCreation
        const outputTokens = usage.output_tokens || 0

        if (firstInput === null) firstInput = effectiveInput
        cumulativeLastInput = effectiveInput
        cumulativeLastOutput = outputTokens
        // These fields are cumulative in the Anthropic API — use last-value semantics
        lastCacheRead = cacheRead
        lastCacheCreation = cacheCreation
        lastDirectInput = inputDirect

        // Attribute context growth from this turn's input delta to the previous turn's tools.
        // Delta = full context size now minus full context size then, which captures the
        // tool results + assistant output that were appended between turns.
        if (prevEffectiveInput !== null && prevTurnTools.length > 0) {
          const delta = Math.max(0, effectiveInput - prevEffectiveInput)
          const perTool = delta / prevTurnTools.length
          for (const toolName of prevTurnTools) {
            toolContextGrowthTokens[toolName] = (toolContextGrowthTokens[toolName] || 0) + perTool
          }
        }

        const currentTurnTools = []
        const contents = Array.isArray(evt.message.content) ? evt.message.content : []
        for (const item of contents) {
          if (item.type === 'tool_use' && item.name) {
            toolCallCounts[item.name] = (toolCallCounts[item.name] || 0) + 1
            if (item.id) toolIdToName[item.id] = item.name
            currentTurnTools.push(item.name)
          }
        }

        prevEffectiveInput = effectiveInput
        prevTurnTools = currentTurnTools
      }

      if (evt.type === 'user' && Array.isArray(evt.message?.content)) {
        for (const item of evt.message.content) {
          if (item.type === 'tool_result' && item.tool_use_id) {
            const toolName = toolIdToName[item.tool_use_id]
            if (toolName) {
              toolResultChars[toolName] = (toolResultChars[toolName] || 0) + getToolResultLength(item.content)
            }
          }
        }
      }
    }

    const promptLength = inputPrompt?.text?.length || 0

    const peakContext = cumulativeLastInput
    const contextGrowth = cumulativeLastInput - (firstInput || 0)

    const tokenUsage = {
      hasData: firstInput !== null,
      initialInput: firstInput || 0,
      peakContext,
      contextGrowth,
      totalOutput: cumulativeLastOutput,
      totalCacheRead: lastCacheRead,
      totalCacheCreation: lastCacheCreation,
      totalDirectInput: lastDirectInput,
      totalAll: cumulativeLastInput + cumulativeLastOutput,
      promptLength,
      modelsUsed: Array.from(modelsUsed),
      toolsAvailable: tokenToolsAvailable,
      mcpServersAvailable,
      toolsSummary: Object.entries(toolCallCounts)
        .map(([name, calls]) => ({
          name,
          calls,
          resultChars: toolResultChars[name] || 0,
          contextGrowthTokens: Math.round(toolContextGrowthTokens[name] || 0)
        }))
        .sort((a, b) => (b.contextGrowthTokens - a.contextGrowthTokens) || (b.resultChars - a.resultChars) || (b.calls - a.calls))
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
      tokenUsage
    }
  }, [executionData, executionLogs, allLogs, promptEvent, pipelineEvents, pipelineRunId])

  const formatTokenCount = (n) => {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
    return String(n)
  }

  const formatToolCall = (toolCall) => {
    if (!toolCall) return null
    
    const { name, input } = toolCall
    
    switch (name) {
      case 'Bash':
        return `${input.command || ''}`
      case 'Read':
        return `${input.file_path || ''}`
      case 'Grep':
        return `"${input.pattern || ''}" in ${input.path || '.'}`
      case 'Edit':
        return `${input.file_path || ''}`
      case 'Write':
        return `${input.file_path || ''}`
      case 'Glob':
        return `${input.pattern || ''}`
      default:
        return input.description || JSON.stringify(input)
    }
  }
  
  const getTodoStats = (todos) => {
    if (!todos || todos.length === 0) return { total: 0, completed: 0, inProgress: 0, pending: 0 }
    
    const completed = todos.filter(t => t.status === 'completed').length
    const inProgress = todos.filter(t => t.status === 'in_progress').length
    const pending = todos.filter(t => t.status === 'pending').length
    
    return { total: todos.length, completed, inProgress, pending }
  }
  
  const getStatusIndicator = () => {
    if (!executionData) {
      return { color: 'bg-gh-fg-muted', label: 'Unknown' }
    }
    if (executionData.status === 'failed') {
      return { color: 'bg-gh-danger', label: 'Failed' }
    }
    if (executionData.status === 'completed') {
      return { color: 'bg-gh-success', label: 'Completed' }
    }
    if (executionData.status === 'running') {
      return { color: 'bg-gh-success', label: 'Running' }
    }
    return { color: 'bg-gh-fg-muted', label: 'Idle' }
  }

  const status = getStatusIndicator()
  const { lastTodoWrite, lastTextMessage, lastToolCall, previousToolCall, previousToolResult, inputPrompt } = agentState
  const todoStats = getTodoStats(lastTodoWrite?.todos)
  const isExecuting = executionData?.status === 'running'
  
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

      {/* Agent Execution Navigation Header */}
      {pipelineExecutions.length > 0 && currentExecutionIndex >= 0 && (
        <div className="mb-3 p-3 bg-gh-canvas-subtle rounded-md border border-gh-border">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              {/* Back to Pipeline Run Debug link */}
              <Link
                to="/pipeline-run-debug"
                className="flex items-center gap-1.5 px-3 py-2 bg-gh-canvas border border-gh-border rounded hover:bg-gh-border-muted transition-colors text-sm"
                title="Back to Pipeline Run Debug"
              >
                <ArrowLeft className="w-4 h-4" />
                <span>Pipeline Runs</span>
              </Link>

              {/* Navigation buttons */}
              <div className="flex gap-1 pl-3 border-l border-gh-border">
                <button
                  onClick={handlePreviousExecution}
                  disabled={currentExecutionIndex === 0}
                  className="p-2 bg-gh-canvas border border-gh-border rounded hover:bg-gh-border-muted transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                  title="Previous agent execution"
                >
                  <ChevronLeft className="w-4 h-4" />
                </button>
                <button
                  onClick={handleNextExecution}
                  disabled={currentExecutionIndex === pipelineExecutions.length - 1}
                  className="p-2 bg-gh-canvas border border-gh-border rounded hover:bg-gh-border-muted transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                  title="Next agent execution"
                >
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>

              {/* Execution counter and agent info */}
              <div className="flex items-center gap-2 pl-2 border-l border-gh-border">
                <span className="text-sm font-mono font-semibold">
                  {currentExecutionIndex + 1} / {pipelineExecutions.length}
                </span>
                <span className="text-gh-fg-muted">·</span>
                <span className="text-sm font-medium">
                  {formatAgentName(executionData.agent)}
                </span>
                <span
                  className={`text-xs px-2 py-0.5 rounded-full ${
                    executionData.status === 'completed'
                      ? 'bg-green-900/30 text-green-400'
                      : executionData.status === 'failed'
                      ? 'bg-red-900/30 text-red-400'
                      : 'bg-blue-900/30 text-blue-400'
                  }`}
                >
                  {executionData.status}
                </span>
              </div>

              {/* Auto-advance checkbox */}
              <label className="flex items-center gap-2 cursor-pointer pl-2 border-l border-gh-border">
                <input
                  type="checkbox"
                  checked={autoAdvance}
                  onChange={handleAutoAdvanceToggle}
                  className="w-4 h-4 rounded border-gh-border bg-gh-canvas text-gh-accent-emphasis focus:ring-2 focus:ring-gh-accent-emphasis focus:ring-offset-0"
                />
                <span className="text-sm">Auto-advance</span>
              </label>
            </div>

            {/* Header Cards */}
            <div className="flex gap-4">
              <HeaderActiveAgents />
              <HeaderSystemHealth />
              <HeaderCircuitBreakers />
            </div>
          </div>
        </div>
      )}

      {/* Token Usage Panel */}
      {tokenUsage.hasData && (
        <div className="mb-3 bg-gh-canvas-subtle rounded-md border border-gh-border">
          <button
            onClick={() => setIsUsageExpanded(!isUsageExpanded)}
            className="w-full flex items-center justify-between p-3 hover:bg-gh-border-muted transition-colors rounded-md"
          >
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-gh-fg">Token Usage</span>
              <span className="text-gh-fg-muted text-sm">·</span>
              <span className="text-sm text-gh-fg-muted">
                {formatTokenCount(tokenUsage.totalAll)} total
              </span>
              {tokenUsage.modelsUsed.length > 0 && (
                <>
                  <span className="text-gh-fg-muted text-sm">·</span>
                  <span className="text-sm text-gh-fg-muted font-mono">{tokenUsage.modelsUsed[0]}</span>
                </>
              )}
              {tokenUsage.toolsSummary.length > 0 && (
                <>
                  <span className="text-gh-fg-muted text-sm">·</span>
                  <span className="text-sm text-gh-fg-muted">{tokenUsage.toolsSummary.length} tools used</span>
                </>
              )}
            </div>
            {isUsageExpanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
          </button>

          {isUsageExpanded && (
            <div className="border-t border-gh-border p-3 grid grid-cols-3 gap-4">
              {/* Col 1: Token counts */}
              <div>
                <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Token Counts</h4>
                <table className="w-full text-sm">
                  <thead>
                    <tr>
                      <th className="text-left text-gh-fg-muted font-normal text-xs pb-1"></th>
                      <th className="text-right text-gh-fg-muted font-normal text-xs pb-1">tokens</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td className="text-gh-fg-muted py-0.5">Initial prompt</td>
                      <td className="text-right font-mono">{formatTokenCount(tokenUsage.initialInput)}</td>
                    </tr>
                    <tr>
                      <td className="text-gh-fg-muted py-0.5">Context growth</td>
                      <td className="text-right font-mono">{formatTokenCount(tokenUsage.contextGrowth)}</td>
                    </tr>
                    <tr className="border-t border-gh-border">
                      <td className="text-gh-fg-muted py-0.5">Peak context</td>
                      <td className="text-right font-mono">{formatTokenCount(tokenUsage.peakContext)}</td>
                    </tr>
                    <tr>
                      <td className="text-gh-fg-muted py-0.5 pl-3 text-xs">↳ direct input</td>
                      <td className="text-right font-mono text-xs">{formatTokenCount(tokenUsage.totalDirectInput)}</td>
                    </tr>
                    <tr>
                      <td className="text-gh-fg-muted py-0.5 pl-3 text-xs">↳ cache reads</td>
                      <td className="text-right font-mono text-xs">{formatTokenCount(tokenUsage.totalCacheRead)}</td>
                    </tr>
                    <tr>
                      <td className="text-gh-fg-muted py-0.5 pl-3 text-xs">↳ cache writes</td>
                      <td className="text-right font-mono text-xs">{formatTokenCount(tokenUsage.totalCacheCreation)}</td>
                    </tr>
                    <tr className="border-t border-gh-border">
                      <td className="text-gh-fg-muted py-0.5">Output</td>
                      <td className="text-right font-mono">{formatTokenCount(tokenUsage.totalOutput)}</td>
                    </tr>
                    <tr className="border-t border-gh-border">
                      <td className="text-gh-fg font-semibold py-0.5">Grand total</td>
                      <td className="text-right font-mono font-semibold">{formatTokenCount(tokenUsage.totalAll)}</td>
                    </tr>
                    {tokenUsage.promptLength > 0 && (
                      <tr className="border-t border-gh-border">
                        <td className="text-gh-fg-muted py-0.5 pt-1.5">Prompt size</td>
                        <td className="text-right font-mono pt-1.5 text-gh-fg-muted">{formatTokenCount(tokenUsage.promptLength)} chars</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>

              {/* Col 2: Models + Tools Available */}
              <div>
                <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Models</h4>
                <div className="flex flex-wrap gap-1 mb-3">
                  {tokenUsage.modelsUsed.map(m => (
                    <span key={m} className="px-2 py-0.5 bg-gh-accent-subtle border border-gh-accent-muted rounded text-xs font-mono">
                      {m}
                    </span>
                  ))}
                </div>
                {tokenUsage.toolsAvailable.length > 0 && (
                  <>
                    <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Tools Available</h4>
                    <div className="flex flex-wrap gap-1">
                      {tokenUsage.toolsAvailable.map(t => {
                        const used = tokenUsage.toolsSummary.some(s => s.name === t)
                        return (
                          <span
                            key={t}
                            className={`px-2 py-0.5 rounded text-xs font-mono ${
                              used
                                ? 'bg-gh-warning-subtle border border-gh-warning text-gh-fg'
                                : 'bg-gh-canvas border border-gh-border text-gh-fg-muted'
                            }`}
                          >
                            {t}
                          </span>
                        )
                      })}
                    </div>
                  </>
                )}
              </div>

              {/* Col 3: Tools Used */}
              <div>
                <h4 className="text-xs font-semibold text-gh-fg-muted uppercase mb-2">Tools Used</h4>
                {tokenUsage.toolsSummary.length > 0 ? (
                  <table className="w-full text-sm">
                    <thead>
                      <tr>
                        <th className="text-left text-gh-fg-muted font-normal text-xs pb-1">Tool</th>
                        <th className="text-right text-gh-fg-muted font-normal text-xs pb-1">Calls</th>
                        <th className="text-right text-gh-fg-muted font-normal text-xs pb-1">Result (chars)</th>
                        <th className="text-right text-gh-fg-muted font-normal text-xs pb-1">Ctx growth (tok)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {tokenUsage.toolsSummary.map(({ name, calls, resultChars, contextGrowthTokens }) => (
                        <tr key={name}>
                          <td className="text-gh-fg font-mono py-0.5 text-xs">{name}</td>
                          <td className="text-right text-gh-fg py-0.5">{calls}</td>
                          <td className="text-right text-gh-fg py-0.5">{resultChars > 0 ? formatTokenCount(resultChars) : '—'}</td>
                          <td className="text-right text-gh-fg py-0.5">{contextGrowthTokens > 0 ? formatTokenCount(contextGrowthTokens) : '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <p className="text-gh-fg-muted text-xs">No tool calls recorded</p>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Agent State Section - Replicated from AgentState component */}
      <div className="bg-gh-canvas-subtle rounded-md border border-gh-border mb-5">
        <div className="p-4 border-b border-gh-border flex items-center justify-between">
          <div className="flex items-center gap-3">
            <h2 className="text-gh-accent-primary text-base font-semibold">Agent Execution</h2>
            <span className="text-lg font-semibold text-gh-fg">
              {formatAgentName(executionData.agent)}
            </span>
            {executionData.branch_name && (
              <span className="px-2 py-1 bg-gh-canvas border border-gh-border rounded text-xs font-mono">
                {executionData.branch_name}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <div className={`w-2.5 h-2.5 ${status.color} rounded-full ${isExecuting ? 'animate-pulse' : ''}`}></div>
            <span className="text-sm text-gh-fg-muted">
              {status.label}
            </span>
          </div>
        </div>
        
        <div className="p-4">
          <div className="grid grid-cols-2 gap-4 mb-4">
            <div className="space-y-2">
              {pipelineRunId && (
                <div className="text-sm">
                  <span className="text-gh-fg-muted">Pipeline Run ID:</span>
                  <Link
                    to="/pipeline-run-debug"
                    search={{ runId: pipelineRunId }}
                    className="ml-2 font-mono text-xs text-gh-accent-fg hover:underline"
                    title="View pipeline run in debug view"
                  >
                    {pipelineRunId}
                  </Link>
                </div>
              )}
              <div className="text-sm">
                <span className="text-gh-fg-muted">Execution ID:</span>
                <span className="ml-2 font-mono text-xs">{executionId}</span>
              </div>
              <div className="text-sm">
                <span className="text-gh-fg-muted">Task ID:</span>
                <span className="ml-2 font-mono text-xs">{executionData.task_id}</span>
              </div>
              <div className="text-sm">
                <span className="text-gh-fg-muted">Project:</span>
                <span className="ml-2">{executionData.project}</span>
              </div>
              {/* Agent Routing Decision section (fix agent name display) */}
              {executionData.agent && (
                <div className="text-sm mt-2">
                  <span className="text-gh-fg-muted">Selected agent:</span>
                  <span className="ml-2 font-mono text-xs">{formatAgentName(executionData.agent)}</span>
                </div>
              )}
              {!executionData.agent && (
                <div className="text-sm mt-2">
                  <span className="text-gh-fg-muted">Selected agent:</span>
                  <span className="ml-2 font-mono text-xs">Unknown Agent</span>
                </div>
              )}
            </div>
            <div className="space-y-2">
              <div className="text-sm">
                <span className="text-gh-fg-muted">Started:</span>
                <span className="ml-2">{new Date(executionData.started_at).toLocaleString()}</span>
              </div>
              {executionData.ended_at && (
                <div className="text-sm">
                  <span className="text-gh-fg-muted">Ended:</span>
                  <span className="ml-2">{new Date(executionData.ended_at).toLocaleString()}</span>
                </div>
              )}
              {executionData.duration && (
                <div className="text-sm">
                  <span className="text-gh-fg-muted">Duration:</span>
                  <span className="ml-2">{Math.floor(executionData.duration / 60)}m {Math.floor(executionData.duration % 60)}s</span>
                </div>
              )}
            </div>
          </div>
          
          {/* When completed, use two-row layout */}
          {executionData.status === 'completed' ? (
            <div className="space-y-4">
              {/* First row: Input Prompt and Latest Message side-by-side */}
              {inputPrompt && lastTextMessage && (
                <div className="flex gap-4">
                  {/* Input Prompt - 50% width */}
                  <div className="flex-1 min-w-0 bg-gh-canvas rounded-md border border-gh-border p-3">
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <h3 className="text-sm font-semibold text-gh-fg">Input Prompt</h3>
                        <span className="text-xs text-gh-fg-muted">
                          {formatTimestamp(inputPrompt.timestamp)}
                        </span>
                      </div>
                      <button
                        onClick={() => setIsPromptExpanded(!isPromptExpanded)}
                        className="text-gh-accent-primary hover:bg-gh-border-muted rounded p-1 transition-colors"
                        title={isPromptExpanded ? 'Collapse' : 'Expand'}
                      >
                        {isPromptExpanded ? (
                          <ChevronUp className="w-4 h-4" />
                        ) : (
                          <ChevronDown className="w-4 h-4" />
                        )}
                      </button>
                    </div>
                    <div className="relative">
                      <div
                        className={`prose prose-sm prose-invert font-mono text-xs max-w-none transition-all break-words ${
                          isPromptExpanded ? '' : 'max-h-[200px] overflow-y-auto'
                        }`}
                        style={{ overflowWrap: 'break-word' }}
                      >
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {inputPrompt.text}
                        </ReactMarkdown>
                      </div>
                      {!isPromptExpanded && inputPrompt.text.length > 500 && (
                        <div className="absolute bottom-0 left-0 right-0 h-12 bg-gradient-to-t from-gh-canvas via-gh-canvas/80 to-transparent pointer-events-none" />
                      )}
                    </div>
                  </div>

                  {/* Latest Message - 50% width */}
                  <div className="flex-1 min-w-0 bg-gh-canvas rounded-md border border-gh-border p-3">
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <h3 className="text-sm font-semibold text-gh-fg">Latest Message</h3>
                        <span className="text-xs text-gh-fg-muted">
                          {formatTimestamp(lastTextMessage.timestamp)}
                        </span>
                      </div>
                      <button
                        onClick={() => setIsMessageExpanded(!isMessageExpanded)}
                        className="text-gh-accent-primary hover:bg-gh-border-muted rounded p-1 transition-colors"
                        title={isMessageExpanded ? 'Collapse' : 'Expand'}
                      >
                        {isMessageExpanded ? (
                          <ChevronUp className="w-4 h-4" />
                        ) : (
                          <ChevronDown className="w-4 h-4" />
                        )}
                      </button>
                    </div>
                    <div className="relative">
                      <div
                        className={`prose prose-sm prose-invert font-mono text-xs max-w-none transition-all break-words ${
                          isMessageExpanded ? '' : 'max-h-[200px] overflow-y-auto'
                        }`}
                        style={{ overflowWrap: 'break-word' }}
                      >
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {lastTextMessage.text}
                        </ReactMarkdown>
                      </div>
                      {!isMessageExpanded && lastTextMessage.text.length > 500 && (
                        <div className="absolute bottom-0 left-0 right-0 h-12 bg-gradient-to-t from-gh-canvas via-gh-canvas/80 to-transparent pointer-events-none" />
                      )}
                    </div>
                  </div>
                </div>
              )}

              {/* Second row: Tool calls/results (left 70%) and Current Tasks (right 30%) */}
              <div className="flex gap-4">
                {/* Left side - Tool calls and results */}
                <div className="flex-[7] min-w-0 space-y-4">
                  {lastToolCall && (
                    <div className="bg-gh-canvas rounded-md border border-gh-border p-3">
                      <div className="flex items-center gap-2 mb-2">
                        <h3 className="text-sm font-semibold text-gh-fg">Latest Tool Call</h3>
                        <span className="text-xs text-gh-fg-muted">
                          {formatTimestamp(lastToolCall.timestamp)}
                        </span>
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="px-2 py-1 bg-gh-warning rounded text-xs font-semibold text-white shrink-0">
                          {lastToolCall.name}
                        </span>
                        <span className="text-sm text-gh-fg font-mono break-all whitespace-pre-wrap">
                          {formatToolCall(lastToolCall)}
                        </span>
                      </div>
                    </div>
                  )}

                  {previousToolCall && (
                    <div className="bg-gh-canvas rounded-md border border-gh-border p-3">
                      <div className="flex items-center gap-2 mb-2">
                        <h3 className="text-sm font-semibold text-gh-fg">Previous Tool Call</h3>
                        <span className="text-xs text-gh-fg-muted">
                          {formatTimestamp(previousToolCall.timestamp)}
                        </span>
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="px-2 py-1 bg-gh-warning rounded text-xs font-semibold text-white shrink-0">
                          {previousToolCall.name}
                        </span>
                        <span className="text-sm text-gh-fg font-mono break-all whitespace-pre-wrap">
                          {formatToolCall(previousToolCall)}
                        </span>
                      </div>
                    </div>
                  )}

                  {previousToolResult && (
                    <div className="bg-gh-canvas rounded-md border border-gh-border p-3">
                      <div className="flex items-center justify-between mb-2">
                        <div className="flex items-center gap-2">
                          <h3 className="text-sm font-semibold text-gh-fg">Previous Tool Result</h3>
                          <span className="text-xs text-gh-fg-muted">
                            {formatTimestamp(previousToolResult.timestamp)}
                          </span>
                        </div>
                        <button
                          onClick={() => setIsPreviousResultExpanded(!isPreviousResultExpanded)}
                          className="text-gh-accent-primary hover:bg-gh-border-muted rounded p-1 transition-colors"
                          title={isPreviousResultExpanded ? 'Collapse' : 'Expand'}
                        >
                          {isPreviousResultExpanded ? (
                            <ChevronUp className="w-4 h-4" />
                          ) : (
                            <ChevronDown className="w-4 h-4" />
                          )}
                        </button>
                      </div>
                      <div className="relative">
                        <div
                          className={`prose prose-sm prose-invert font-mono text-xs max-w-none transition-all break-words ${
                            isPreviousResultExpanded ? '' : 'max-h-[200px] overflow-y-auto'
                          }`}
                          style={{ overflowWrap: 'break-word' }}
                        >
                          {typeof previousToolResult.content === 'string' ? (
                            <pre className="whitespace-pre-wrap break-words" style={{ overflowWrap: 'break-word' }}>{previousToolResult.content}</pre>
                          ) : (
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>
                              {JSON.stringify(previousToolResult.content, null, 2)}
                            </ReactMarkdown>
                          )}
                        </div>
                        {!isPreviousResultExpanded && JSON.stringify(previousToolResult.content).length > 500 && (
                          <div className="absolute bottom-0 left-0 right-0 h-12 bg-gradient-to-t from-gh-canvas via-gh-canvas/80 to-transparent pointer-events-none" />
                        )}
                      </div>
                    </div>
                  )}
                </div>

                {/* Right side - Current Tasks */}
                <div className="flex-[3] min-w-0">
                  {executionData.trigger_source?.startsWith('repair_cycle') && (
                    <RepairCycleStatus events={mergedPipelineEvents} />
                  )}
                  <div className="bg-gh-canvas rounded-md border border-gh-border p-3">
                    <div className="mb-3">
                      <div className="flex items-center justify-between mb-1">
                        <div className="flex items-center gap-2">
                          <h3 className="text-sm font-semibold text-gh-fg">Current Tasks</h3>
                          {lastTodoWrite && (
                            <span className="text-xs text-gh-fg-muted">
                              {formatTimestamp(lastTodoWrite.timestamp)}
                            </span>
                          )}
                        </div>
                        {lastTodoWrite && lastTodoWrite.todos.length > 0 && (
                          <span className="text-xs text-gh-success">
                            {todoStats.completed}/{todoStats.total}
                          </span>
                        )}
                      </div>
                    </div>
                    {lastTodoWrite && lastTodoWrite.todos.length > 0 ? (
                      <div className="space-y-2">
                        {lastTodoWrite.todos.map((todo, idx) => {
                          const isCompleted = todo.status === 'completed'
                          const isInProgress = todo.status === 'in_progress'

                          return (
                            <div
                              key={idx}
                              className={`flex items-start gap-2 p-2 rounded ${
                                isInProgress ? 'bg-gh-warning-subtle border border-gh-warning' : ''
                              }`}
                            >
                              {isCompleted ? (
                                <CheckCircle2 className="w-4 h-4 mt-0.5 text-gh-success flex-shrink-0" />
                              ) : isInProgress ? (
                                <PlayCircle className="w-4 h-4 mt-0.5 text-gh-warning flex-shrink-0" />
                              ) : (
                                <Circle className="w-4 h-4 mt-0.5 text-gh-fg-muted flex-shrink-0" />
                              )}
                              <span
                                className={`text-sm ${
                                  isCompleted
                                    ? 'line-through text-gh-fg-muted'
                                    : isInProgress
                                    ? 'text-gh-fg font-medium'
                                    : 'text-gh-fg'
                                }`}
                              >
                                {todo.content}
                              </span>
                            </div>
                          )
                        })}
                      </div>
                    ) : (
                      <div className="text-center text-gh-fg-muted text-sm py-4">
                        No current task list
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          ) : (
            /* When not completed, use original layout */
            <div className="flex gap-4">
              {/* Left column - 70% width */}
              <div className="flex-[7] min-w-0 space-y-4">
                {inputPrompt && (
                  <div className="bg-gh-canvas rounded-md border border-gh-border p-3">
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <h3 className="text-sm font-semibold text-gh-fg">Input Prompt</h3>
                        <span className="text-xs text-gh-fg-muted">
                          {formatTimestamp(inputPrompt.timestamp)}
                        </span>
                      </div>
                      <button
                        onClick={() => setIsPromptExpanded(!isPromptExpanded)}
                        className="text-gh-accent-primary hover:bg-gh-border-muted rounded p-1 transition-colors"
                        title={isPromptExpanded ? 'Collapse' : 'Expand'}
                      >
                        {isPromptExpanded ? (
                          <ChevronUp className="w-4 h-4" />
                        ) : (
                          <ChevronDown className="w-4 h-4" />
                        )}
                      </button>
                    </div>
                    <div className="relative">
                      <div
                        className={`prose prose-sm prose-invert font-mono text-xs max-w-none transition-all break-words ${
                          isPromptExpanded ? '' : 'max-h-[200px] overflow-y-auto'
                        }`}
                        style={{ overflowWrap: 'break-word' }}
                      >
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {inputPrompt.text}
                        </ReactMarkdown>
                      </div>
                      {!isPromptExpanded && inputPrompt.text.length > 500 && (
                        <div className="absolute bottom-0 left-0 right-0 h-12 bg-gradient-to-t from-gh-canvas via-gh-canvas/80 to-transparent pointer-events-none" />
                      )}
                    </div>
                  </div>
                )}

                {lastTextMessage && (
                  <div className="bg-gh-canvas rounded-md border border-gh-border p-3">
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <h3 className="text-sm font-semibold text-gh-fg">Latest Message</h3>
                        <span className="text-xs text-gh-fg-muted">
                          {formatTimestamp(lastTextMessage.timestamp)}
                        </span>
                      </div>
                      <button
                        onClick={() => setIsMessageExpanded(!isMessageExpanded)}
                        className="text-gh-accent-primary hover:bg-gh-border-muted rounded p-1 transition-colors"
                        title={isMessageExpanded ? 'Collapse' : 'Expand'}
                      >
                        {isMessageExpanded ? (
                          <ChevronUp className="w-4 h-4" />
                        ) : (
                          <ChevronDown className="w-4 h-4" />
                        )}
                      </button>
                    </div>
                    <div className="relative">
                      <div
                        className={`prose prose-sm prose-invert font-mono text-xs max-w-none transition-all break-words ${
                          isMessageExpanded ? '' : 'max-h-[200px] overflow-y-auto'
                        }`}
                        style={{ overflowWrap: 'break-word' }}
                      >
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {lastTextMessage.text}
                        </ReactMarkdown>
                      </div>
                      {!isMessageExpanded && lastTextMessage.text.length > 500 && (
                        <div className="absolute bottom-0 left-0 right-0 h-12 bg-gradient-to-t from-gh-canvas via-gh-canvas/80 to-transparent pointer-events-none" />
                      )}
                    </div>
                  </div>
                )}

                {lastToolCall && (
                  <div className="bg-gh-canvas rounded-md border border-gh-border p-3">
                    <div className="flex items-center gap-2 mb-2">
                      <h3 className="text-sm font-semibold text-gh-fg">Latest Tool Call</h3>
                      <span className="text-xs text-gh-fg-muted">
                        {formatTimestamp(lastToolCall.timestamp)}
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="px-2 py-1 bg-gh-warning rounded text-xs font-semibold text-white shrink-0">
                        {lastToolCall.name}
                      </span>
                      <span className="text-sm text-gh-fg font-mono break-all whitespace-pre-wrap">
                        {formatToolCall(lastToolCall)}
                      </span>
                    </div>
                  </div>
                )}

                {previousToolCall && (
                  <div className="bg-gh-canvas rounded-md border border-gh-border p-3">
                    <div className="flex items-center gap-2 mb-2">
                      <h3 className="text-sm font-semibold text-gh-fg">Previous Tool Call</h3>
                      <span className="text-xs text-gh-fg-muted">
                        {formatTimestamp(previousToolCall.timestamp)}
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="px-2 py-1 bg-gh-warning rounded text-xs font-semibold text-white shrink-0">
                        {previousToolCall.name}
                      </span>
                      <span className="text-sm text-gh-fg font-mono break-all whitespace-pre-wrap">
                        {formatToolCall(previousToolCall)}
                      </span>
                    </div>
                  </div>
                )}

                {previousToolResult && (
                  <div className="bg-gh-canvas rounded-md border border-gh-border p-3">
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <h3 className="text-sm font-semibold text-gh-fg">Previous Tool Result</h3>
                        <span className="text-xs text-gh-fg-muted">
                          {formatTimestamp(previousToolResult.timestamp)}
                        </span>
                      </div>
                      <button
                        onClick={() => setIsPreviousResultExpanded(!isPreviousResultExpanded)}
                        className="text-gh-accent-primary hover:bg-gh-border-muted rounded p-1 transition-colors"
                        title={isPreviousResultExpanded ? 'Collapse' : 'Expand'}
                      >
                        {isPreviousResultExpanded ? (
                          <ChevronUp className="w-4 h-4" />
                        ) : (
                          <ChevronDown className="w-4 h-4" />
                        )}
                      </button>
                    </div>
                    <div className="relative">
                      <div
                        className={`prose prose-sm prose-invert font-mono text-xs max-w-none transition-all break-words ${
                          isPreviousResultExpanded ? '' : 'max-h-[200px] overflow-y-auto'
                        }`}
                        style={{ overflowWrap: 'break-word' }}
                      >
                        {typeof previousToolResult.content === 'string' ? (
                          <pre className="whitespace-pre-wrap break-words" style={{ overflowWrap: 'break-word' }}>{previousToolResult.content}</pre>
                        ) : (
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>
                            {JSON.stringify(previousToolResult.content, null, 2)}
                          </ReactMarkdown>
                        )}
                      </div>
                      {!isPreviousResultExpanded && JSON.stringify(previousToolResult.content).length > 500 && (
                        <div className="absolute bottom-0 left-0 right-0 h-12 bg-gradient-to-t from-gh-canvas via-gh-canvas/80 to-transparent pointer-events-none" />
                      )}
                    </div>
                  </div>
                )}
              </div>

              {/* Right column - 30% width */}
              <div className="flex-[3] min-w-0">
                {executionData.trigger_source?.startsWith('repair_cycle') && (
                  <RepairCycleStatus events={mergedPipelineEvents} />
                )}
                <div className="bg-gh-canvas rounded-md border border-gh-border p-3">
                  <div className="mb-3">
                    <div className="flex items-center justify-between mb-1">
                      <div className="flex items-center gap-2">
                        <h3 className="text-sm font-semibold text-gh-fg">Current Tasks</h3>
                        {lastTodoWrite && (
                          <span className="text-xs text-gh-fg-muted">
                            {formatTimestamp(lastTodoWrite.timestamp)}
                          </span>
                        )}
                      </div>
                      {lastTodoWrite && lastTodoWrite.todos.length > 0 && (
                        <span className="text-xs text-gh-success">
                          {todoStats.completed}/{todoStats.total}
                        </span>
                      )}
                    </div>
                  </div>
                  {lastTodoWrite && lastTodoWrite.todos.length > 0 ? (
                    <div className="space-y-2">
                      {lastTodoWrite.todos.map((todo, idx) => {
                        const isCompleted = todo.status === 'completed'
                        const isInProgress = todo.status === 'in_progress'

                        return (
                          <div
                            key={idx}
                            className={`flex items-start gap-2 p-2 rounded ${
                              isInProgress ? 'bg-gh-warning-subtle border border-gh-warning' : ''
                            }`}
                          >
                            {isCompleted ? (
                              <CheckCircle2 className="w-4 h-4 mt-0.5 text-gh-success flex-shrink-0" />
                            ) : isInProgress ? (
                              <PlayCircle className="w-4 h-4 mt-0.5 text-gh-warning flex-shrink-0" />
                            ) : (
                              <Circle className="w-4 h-4 mt-0.5 text-gh-fg-muted flex-shrink-0" />
                            )}
                            <span
                              className={`text-sm ${
                                isCompleted
                                  ? 'line-through text-gh-fg-muted'
                                  : isInProgress
                                  ? 'text-gh-fg font-medium'
                                  : 'text-gh-fg'
                              }`}
                            >
                              {todo.content}
                            </span>
                          </div>
                        )
                      })}
                    </div>
                  ) : (
                    <div className="text-center text-gh-fg-muted text-sm py-4">
                      No current task list
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

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
