import { useMemo, useState } from 'react'
import { useSocket } from '../contexts/SocketContext'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { CheckCircle2, Circle, PlayCircle, ChevronDown, ChevronUp } from 'lucide-react'

export default function AgentState() {
  const { logs, events } = useSocket()
  const [isMessageExpanded, setIsMessageExpanded] = useState(false)

  const agentState = useMemo(() => {
    let lastTodoWrite = null
    let lastTextMessage = null
    let lastToolCall = null
    let currentAgent = null

    // Start from most recent logs (end of array since they're appended)
    for (let i = logs.length - 1; i >= 0; i--) {
      const log = logs[i]
      const event = log.event

      // Extract agent name from most recent log
      if (!currentAgent && log.agent) {
        currentAgent = log.agent
      }

      // Only collect data from the current agent to avoid mixing state
      if (log.agent === currentAgent && event?.type === 'assistant' && event?.message?.content) {
        const contents = Array.isArray(event.message.content)
          ? event.message.content
          : [event.message.content]

        for (const item of contents) {
          if (item.type === 'text' && !lastTextMessage && item.text?.trim()) {
            lastTextMessage = {
              text: item.text,
              timestamp: log.timestamp,
              agent: log.agent
            }
          } else if (item.type === 'tool_use') {
            if (item.name === 'TodoWrite' && !lastTodoWrite) {
              lastTodoWrite = {
                todos: item.input?.todos || [],
                timestamp: log.timestamp,
                agent: log.agent
              }
            } else if (item.name !== 'TodoWrite' && !lastToolCall) {
              lastToolCall = {
                name: item.name,
                input: item.input,
                timestamp: log.timestamp,
                agent: log.agent
              }
            }
          }
        }
      }

      if (lastTodoWrite && lastTextMessage && lastToolCall && currentAgent) break
    }

    // Check for agent status in the events array (not logs)
    // We need to find the MOST RECENT status-changing event for this agent
    // Events are ordered newest first (prepended to array)
    let agentCompleted = false
    let agentFailed = false
    let agentStatus = null // 'initialized', 'completed', 'failed', or null

    if (currentAgent) {
      // Find the most recent status event for this agent
      for (let i = 0; i < events.length; i++) {
        const event = events[i]
        if (event.agent === currentAgent) {
          if (event.event_type === 'agent_completed') {
            agentStatus = 'completed'
            agentCompleted = true
            break
          } else if (event.event_type === 'agent_failed') {
            agentStatus = 'failed'
            agentFailed = true
            break
          } else if (event.event_type === 'agent_initialized') {
            agentStatus = 'initialized'
            break
          }
        }
      }
    }

    // Determine if agent is executing based on most recent status event
    let isExecuting = false
    if (agentStatus === 'initialized') {
      // Most recent event is initialization, so agent is running
      isExecuting = true
    } else if (agentStatus === 'completed' || agentStatus === 'failed') {
      // Most recent event is completion/failure, so agent is not running
      isExecuting = false
    } else if (currentAgent) {
      // No status events found, fall back to checking todos and recent activity
      const hasInProgressTodos = lastTodoWrite?.todos?.some(t => t.status === 'in_progress')
      const mostRecentTimestamp = logs.length > 0 ? logs[logs.length - 1].timestamp : null
      const recentActivity = mostRecentTimestamp
        ? (Date.now() / 1000 - mostRecentTimestamp) < 30
        : false
      isExecuting = hasInProgressTodos || recentActivity
    }

    return {
      lastTodoWrite,
      lastTextMessage,
      lastToolCall,
      currentAgent,
      isExecuting,
      agentCompleted,
      agentFailed
    }
  }, [logs, events])

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

  const { lastTodoWrite, lastTextMessage, lastToolCall, currentAgent, isExecuting, agentCompleted, agentFailed } = agentState

  const formatAgentName = (agentName) => {
    if (!agentName) return 'Unknown Agent'
    return agentName.split('_').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ')
  }

  const formatTimestamp = (timestamp) => {
    if (!timestamp) return ''
    const date = new Date(timestamp * 1000)  // Convert seconds to milliseconds
    return date.toLocaleTimeString('en-US', { timeZone: 'UTC', hour12: false }) + ' UTC'
  }

  const todoStats = getTodoStats(lastTodoWrite?.todos)

  const getStatusIndicator = () => {
    if (!currentAgent) {
      return { color: 'bg-gh-fg-muted', label: 'Idle' }
    }
    if (agentFailed) {
      return { color: 'bg-gh-danger', label: 'Failed' }
    }
    if (agentCompleted) {
      return { color: 'bg-gh-success', label: 'Completed' }
    }
    if (isExecuting) {
      return { color: 'bg-gh-success', label: 'Executing' }
    }
    return { color: 'bg-gh-fg-muted', label: 'Idle' }
  }

  const status = getStatusIndicator()

  return (
    <div className="bg-gh-canvas-subtle rounded-md border border-gh-border mb-5">
      <div className="p-4 border-b border-gh-border flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h2 className="text-gh-accent-primary text-base font-semibold">Agent State</h2>
          {currentAgent && (
            <span className="text-lg font-semibold text-gh-fg">
              {formatAgentName(currentAgent)}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <div className={`w-2.5 h-2.5 ${status.color} rounded-full ${isExecuting && !agentCompleted && !agentFailed ? 'animate-pulse' : ''}`}></div>
          <span className="text-sm text-gh-fg-muted">
            {status.label}
          </span>
        </div>
      </div>

      {!lastTodoWrite && !lastTextMessage && !lastToolCall ? (
        <div className="p-4 text-center text-gh-fg-muted text-sm">
          Waiting for agent activity...
        </div>
      ) : (
        <div className="p-4 flex gap-4">
          {/* Left column - 70% width */}
          <div className="flex-[7] space-y-4">
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
                    className={`prose prose-sm prose-invert font-mono text-xs max-w-none transition-all ${
                      isMessageExpanded ? '' : 'max-h-[200px] overflow-y-auto'
                    }`}
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
      )}
    </div>
  )
}
