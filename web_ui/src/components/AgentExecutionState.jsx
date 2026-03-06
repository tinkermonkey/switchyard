import { useState } from 'react'
import { Link } from '@tanstack/react-router'
import { ChevronDown, ChevronUp } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { formatTimestamp } from '../utils/eventMerging'
import RepairCycleStatus from './RepairCycleStatus'
import AgentExecutionCurrentTasks from './AgentExecutionCurrentTasks'

const formatAgentName = (agentName) => {
  if (!agentName || typeof agentName !== 'string' || agentName.trim() === '') return 'Unknown Agent'
  return agentName.split('_').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ')
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

const getStatusIndicator = (executionData) => {
  if (!executionData) return { color: 'bg-gh-fg-muted', label: 'Unknown' }
  if (executionData.status === 'failed') return { color: 'bg-gh-danger', label: 'Failed' }
  if (executionData.status === 'completed') return { color: 'bg-gh-success', label: 'Completed' }
  if (executionData.status === 'running') return { color: 'bg-gh-success', label: 'Running' }
  return { color: 'bg-gh-fg-muted', label: 'Idle' }
}

export default function AgentExecutionState({
  executionId,
  executionData,
  pipelineRunId,
  agentState,
  mergedPipelineEvents,
}) {
  const [isMessageExpanded, setIsMessageExpanded] = useState(false)
  const [isPromptExpanded, setIsPromptExpanded] = useState(false)
  const [isPreviousResultExpanded, setIsPreviousResultExpanded] = useState(false)

  const status = getStatusIndicator(executionData)
  const { lastTodoWrite, lastTextMessage, lastToolCall, previousToolCall, previousToolResult, inputPrompt } = agentState
  const isExecuting = executionData?.status === 'running'

  const metaCols = [
    ...(pipelineRunId ? [{
      label: 'Pipeline Run',
      value: (
        <Link
          to="/pipeline-run"
          search={{ runId: pipelineRunId }}
          className="font-mono text-xs text-gh-accent-fg hover:underline"
          title="View pipeline run"
        >
          {pipelineRunId}
        </Link>
      ),
    }] : []),
    { label: 'Execution ID', value: <span className="font-mono">{executionId}</span> },
    { label: 'Task ID', value: <span className="font-mono">{executionData.task_id}</span> },
    { label: 'Project', value: executionData.project },
    { label: 'Agent', value: formatAgentName(executionData.agent) },
    { label: 'Started', value: new Date(executionData.started_at).toLocaleString() },
    ...(executionData.ended_at ? [{ label: 'Ended', value: new Date(executionData.ended_at).toLocaleString() }] : []),
    ...(executionData.duration ? [{ label: 'Duration', value: `${Math.floor(executionData.duration / 60)}m ${Math.floor(executionData.duration % 60)}s` }] : []),
  ]

  return (
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
        <div className="overflow-x-auto mb-4">
          <table className="text-xs border-collapse">
            <thead>
              <tr>
                {metaCols.map(col => (
                  <th key={col.label} className="text-left font-medium pr-5 pb-0.5 whitespace-nowrap text-gh-fg-muted opacity-70">{col.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr>
                {metaCols.map(col => (
                  <td key={col.label} className="pr-5 whitespace-nowrap text-gh-fg-muted">{col.value}</td>
                ))}
              </tr>
            </tbody>
          </table>
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
                      className={`prose prose-sm prose-invert font-mono text-xs max-w-none transition-all break-words ${isPromptExpanded ? '' : 'max-h-[200px] overflow-y-auto'
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
                      className={`prose prose-sm prose-invert font-mono text-xs max-w-none transition-all break-words ${isMessageExpanded ? '' : 'max-h-[200px] overflow-y-auto'
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
                        className={`prose prose-sm prose-invert font-mono text-xs max-w-none transition-all break-words ${isPreviousResultExpanded ? '' : 'max-h-[200px] overflow-y-auto'
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
                <AgentExecutionCurrentTasks lastTodoWrite={lastTodoWrite} />
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
                      className={`prose prose-sm prose-invert font-mono text-xs max-w-none transition-all break-words ${isPromptExpanded ? '' : 'max-h-[200px] overflow-y-auto'
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
                      className={`prose prose-sm prose-invert font-mono text-xs max-w-none transition-all break-words ${isMessageExpanded ? '' : 'max-h-[200px] overflow-y-auto'
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
                      className={`prose prose-sm prose-invert font-mono text-xs max-w-none transition-all break-words ${isPreviousResultExpanded ? '' : 'max-h-[200px] overflow-y-auto'
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
              <AgentExecutionCurrentTasks lastTodoWrite={lastTodoWrite} />
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
