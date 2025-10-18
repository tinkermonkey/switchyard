import { createFileRoute } from '@tanstack/react-router'
import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { RefreshCw, CheckCircle2, Circle, PlayCircle, ChevronDown, ChevronUp } from 'lucide-react'
import Header from '../components/Header'
import NavigationTabs from '../components/NavigationTabs'
import { useSocket } from '../contexts/SocketContext'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { normalizeTimestamp, formatTimestamp, mergeAgentExecutionEvents } from '../utils/eventMerging'

  const formatAgentName = (agentName) => {
    if (!agentName || typeof agentName !== 'string' || agentName.trim() === '') return 'Unknown Agent'
    return agentName.split('_').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ')
  }

function AgentExecutionView() {
  const { executionId } = Route.useParams()
  const { logs: allLogs } = useSocket()
  const [executionData, setExecutionData] = useState(null)
  const [executionLogs, setExecutionLogs] = useState([])
  const [promptEvent, setPromptEvent] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [isMessageExpanded, setIsMessageExpanded] = useState(false)
  const [isPromptExpanded, setIsPromptExpanded] = useState(false)
  const [isPreviousResultExpanded, setIsPreviousResultExpanded] = useState(false)
  const [autoScroll, setAutoScroll] = useState(true)
  const logsContainerRef = useRef(null)
  
  // Fetch execution data
  const fetchExecutionData = useCallback(async () => {
    try {
      setLoading(true)
      const response = await fetch(`/api/agent-execution/${executionId}`)
      const data = await response.json()
      
      if (data.success) {
        setExecutionData(data.execution)
        setExecutionLogs(data.logs || [])
        setPromptEvent(data.prompt_event || null)
        setError(null)
      } else {
        setError(data.error || 'Failed to load execution data')
      }
    } catch (err) {
      console.error('Error fetching execution data:', err)
      setError('Failed to load execution data')
    } finally {
      setLoading(false)
    }
  }, [executionId])
  
  useEffect(() => {
    fetchExecutionData()
  }, [fetchExecutionData])
  
  // Merge API logs with live WebSocket updates and build agent state
  const { executionEvents, agentState, mergedLogs } = useMemo(() => {
    if (!executionData) {
      return { executionEvents: [], agentState: {}, mergedLogs: [] }
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
    if (promptEvent && promptEvent.data?.prompt) {
      const eventTimestamp = normalizeTimestamp(promptEvent.timestamp)
      if (eventTimestamp) {
        inputPrompt = {
          text: promptEvent.data.prompt,
          timestamp: eventTimestamp,
          agent: promptEvent.agent
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
      mergedLogs: logs
    }
  }, [executionData, executionLogs, allLogs, promptEvent])
  
  // Auto-scroll logs
  useEffect(() => {
    if (autoScroll && logsContainerRef.current) {
      logsContainerRef.current.scrollTop = logsContainerRef.current.scrollHeight
    }
  }, [mergedLogs, autoScroll])
  
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
  
  const formatLogContent = (log) => {
    const event = log.raw_event?.event
    let logType = 'text'
    let logContent = ''
    let toolData = null
    
    // If there's no event data (e.g., system events), return empty
    if (!event) {
      return { logType: 'text', logContent: log.event_type || '', toolData: null }
    }
    
    if (event?.type === 'assistant') {
      const msg = event.message
      if (msg?.content) {
        const contents = Array.isArray(msg.content) ? msg.content : [msg.content]
        
        for (const item of contents) {
          if (item.type === 'text') {
            logType = 'text'
            logContent = item.text || ''
            break
          } else if (item.type === 'tool_use') {
            logType = 'tool'
            const toolName = item.name
            const input = item.input || {}
            
            switch (toolName) {
              case 'Bash':
                logContent = `Bash: ${input.command || ''}`
                break
              case 'Read':
                logContent = `Read: ${input.file_path || ''}`
                break
              case 'Grep':
                logContent = `Grep: "${input.pattern || ''}" in ${input.path || '.'}`
                break
              case 'Edit':
                logContent = `Edit: ${input.file_path || ''}`
                break
              case 'Write':
                logContent = `Write: ${input.file_path || ''}`
                break
              case 'Glob':
                logContent = `Glob: ${input.pattern || ''}`
                break
              case 'TodoWrite':
                logContent = `TodoWrite: ${input.todos?.length || 0} items`
                toolData = item
                break
              default:
                logContent = `${toolName}${input.description ? ': ' + input.description : ''}`
            }
            break
          }
        }
        
        if (!logContent && msg.usage) {
          logType = 'usage'
          const usage = msg.usage
          const parts = [`${usage.input_tokens || 0} in`, `${usage.output_tokens || 0} out`]
          if (usage.cache_read_input_tokens) parts.push(`${usage.cache_read_input_tokens} cache`)
          logContent = `📊 Tokens: ${parts.join(' / ')}`
        }
      }
    } else if (event?.type === 'user') {
      const msg = event.message
      if (msg?.content) {
        const contents = Array.isArray(msg.content) ? msg.content : [msg.content]
        for (const item of contents) {
          if (item.type === 'tool_result') {
            logType = 'result'
            const contentStr = typeof item.content === 'string' ? item.content : JSON.stringify(item.content)
            const preview = contentStr?.substring(0, 60) || ''
            logContent = `Tool result${item.is_error ? ' (error)' : ''}: ${preview}${contentStr?.length > 60 ? '...' : ''}`
            break
          }
        }
      }
    }
    
    return { logType, logContent, toolData }
  }
  
  const getLogTypeColor = (type) => {
    switch (type) {
      case 'tool': return 'bg-gh-warning'
      case 'text': return 'bg-gh-success'
      case 'usage': return 'bg-gh-fg-subtle'
      case 'error': return 'bg-gh-danger'
      case 'result': return 'bg-gh-accent-emphasis'
      default: return 'bg-gh-fg-muted'
    }
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
          
          <div className="flex gap-4">
            {/* Left column - 70% width */}
            <div className="flex-[7] space-y-4">
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
                    <span className="px-2 py-1 bg-gh-warning rounded text-xs font-semibold text-white">
                      {lastToolCall.name}
                    </span>
                    <span className="text-sm text-gh-fg font-mono">
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
                    <span className="px-2 py-1 bg-gh-warning rounded text-xs font-semibold text-white">
                      {previousToolCall.name}
                    </span>
                    <span className="text-sm text-gh-fg font-mono">
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
            <div className="flex-[3]">
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
      </div>
      
      {/* Live Logs Section */}
      <div className="bg-gh-canvas-subtle rounded-md border border-gh-border">
        <div className="p-4 border-b border-gh-border flex justify-between items-center">
          <h2 className="text-gh-accent-primary text-base font-semibold">
            Live Logs for Execution
          </h2>
          <div className="flex gap-2 items-center">
            <span className="text-xs text-gh-fg-muted">
              {mergedLogs.length} log entries
              {mergedLogs.length > executionLogs.length && (
                <span className="ml-1 text-gh-success">
                  (+{mergedLogs.length - executionLogs.length} live)
                </span>
              )}
            </span>
            <button
              onClick={() => setAutoScroll(!autoScroll)}
              className="px-3 py-1 bg-gh-canvas border border-gh-border rounded text-xs hover:bg-gh-border-muted transition-colors"
            >
              Auto-scroll: {autoScroll ? 'ON' : 'OFF'}
            </button>
          </div>
        </div>
        
        <div
          ref={logsContainerRef}
          className="min-h-[300px] max-h-[50vh] overflow-y-auto font-mono text-xs"
        >
          {mergedLogs.length === 0 ? (
            <div className="p-4 text-center text-gh-fg-muted">
              No logs found for this execution
            </div>
          ) : (
            mergedLogs.map((log, idx) => {
              const { logType, logContent, toolData } = formatLogContent(log)
              if (!logContent) return null
              
              return (
                <div
                  key={idx}
                  className="flex gap-3 p-2 border-b border-gh-border-muted hover:bg-gh-canvas transition-colors items-start"
                >
                  <span className="text-gh-fg-subtle whitespace-nowrap">
                    {formatTimestamp(normalizeTimestamp(log.timestamp))}
                  </span>
                  <span className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase whitespace-nowrap ${getLogTypeColor(logType)} text-white`}>
                    {logType}
                  </span>
                  <div className="flex-1 min-w-0">
                    {logType === 'text' ? (
                      <div className="prose prose-sm prose-invert max-w-none">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {logContent}
                        </ReactMarkdown>
                      </div>
                    ) : (
                      <span className="break-words">{logContent}</span>
                    )}
                    {toolData?.input?.todos && (
                      <div className="mt-2 space-y-1">
                        {toolData.input.todos.map((todo, idx) => {
                          const isCompleted = todo.status === 'completed'
                          return (
                            <div key={idx} className="flex items-start gap-2">
                              <span className={isCompleted ? 'text-gh-success' : 'text-gh-fg-muted'}>
                                {isCompleted ? '☑' : '☐'}
                              </span>
                              <span className={isCompleted ? 'line-through text-gh-fg-muted' : ''}>
                                {todo.content}
                              </span>
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>
                </div>
              )
            })
          )}
        </div>
      </div>
    </div>
  )
}

export const Route = createFileRoute('/agent-execution/$executionId')({
  component: AgentExecutionView,
})
