import { useEffect, useRef, useState } from 'react'
import { useSocket } from '../contexts/SocketContext'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Info } from 'lucide-react'
import Modal from './Modal'

export default function LiveLogs() {
  const { logs, clearLogs } = useSocket()
  const containerRef = useRef(null)
  const [autoScroll, setAutoScroll] = useState(true)
  const [selectedLog, setSelectedLog] = useState(null)

  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [logs, autoScroll])

  const formatToolUse = (item) => {
    const toolName = item.name
    const input = item.input || {}

    switch (toolName) {
      case 'Bash':
        return `Bash: ${input.command || ''}`
      case 'Read':
        return `Read: ${input.file_path || ''}`
      case 'Grep':
        return `Grep: "${input.pattern || ''}" in ${input.path || '.'}`
      case 'Edit':
        return `Edit: ${input.file_path || ''}`
      case 'Write':
        return `Write: ${input.file_path || ''}`
      case 'Glob':
        return `Glob: ${input.pattern || ''}`
      case 'TodoWrite':
        return `TodoWrite: ${input.todos?.length || 0} items`
      default:
        return `${toolName}${input.description ? ': ' + input.description : ''}`
    }
  }

  const renderTodoList = (todos) => {
    if (!todos || todos.length === 0) return null

    return (
      <div className="mt-2 space-y-1">
        {todos.map((todo, idx) => {
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
    )
  }

  const getLogContent = (data) => {
    const event = data.event
    let logType = 'text'
    let logContent = ''
    let toolData = null

    if (event.type === 'assistant') {
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
            logContent = formatToolUse(item)
            if (item.name === 'TodoWrite') {
              toolData = item
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
    } else if (event.type === 'user') {
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

  return (
    <>
      <div className="bg-gh-canvas-subtle rounded-md border border-gh-border mb-5">
        <div className="p-4 border-b border-gh-border flex justify-between items-center">
          <h2 className="text-gh-accent-primary text-base font-semibold">Claude Live Logs</h2>
          <div className="flex gap-2">
            <button
              onClick={() => setAutoScroll(!autoScroll)}
              className="px-3 py-1 bg-gh-canvas border border-gh-border rounded text-xs hover:bg-gh-border-muted transition-colors"
            >
              Auto-scroll: {autoScroll ? 'ON' : 'OFF'}
            </button>
            <button
              onClick={clearLogs}
              className="px-3 py-1 bg-gh-canvas border border-gh-border rounded text-xs hover:bg-gh-border-muted transition-colors"
            >
              Clear Logs
            </button>
          </div>
        </div>

        <div
          ref={containerRef}
          className="min-h-[200px] max-h-[30vh] overflow-y-auto font-mono text-xs"
        >
          {logs.length === 0 ? (
            <div className="p-4 text-center text-gh-fg-muted">
              Waiting for Claude activity...
            </div>
          ) : (
            logs.map((log, idx) => {
              const { logType, logContent, toolData } = getLogContent(log)
              if (!logContent) return null

              return (
                <div
                  key={idx}
                  className="flex gap-3 p-2 border-b border-gh-border-muted hover:bg-gh-canvas transition-colors items-start"
                >
                  <span className="text-gh-fg-subtle whitespace-nowrap">
                    {log.timestamp ? new Date(log.timestamp * 1000).toLocaleTimeString('en-US', { timeZone: 'UTC', hour12: false }) + ' UTC' : new Date().toLocaleTimeString('en-US', { timeZone: 'UTC', hour12: false }) + ' UTC'}
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
                    {toolData?.input?.todos && renderTodoList(toolData.input.todos)}
                  </div>
                  <button
                    onClick={() => setSelectedLog(log)}
                    className="text-gh-accent-primary hover:bg-gh-border-muted rounded p-1 opacity-60 hover:opacity-100 transition-opacity"
                    title="View details"
                  >
                    <Info className="w-3.5 h-3.5" />
                  </button>
                </div>
              )
            })
          )}
        </div>
      </div>

      {selectedLog && (
        <Modal
          title="Log Details"
          onClose={() => setSelectedLog(null)}
        >
          <pre className="bg-gh-canvas p-4 rounded overflow-auto max-h-[60vh] text-xs">
            {JSON.stringify(selectedLog, null, 2)}
          </pre>
        </Modal>
      )}
    </>
  )
}
