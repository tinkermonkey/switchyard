import { useState, useEffect, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { RefreshCw, X, ExternalLink } from 'lucide-react'
import { normalizeTimestamp, mergeAgentExecutionEvents, mergeObjectStable, mergeArrayByIdStable } from '../utils/eventMerging'
import { useSocket } from '../contexts/SocketContext'
import TokenUsagePanel from './TokenUsagePanel'
import AgentExecutionState from './AgentExecutionState'

export default function AgentExecutionDetailModal({ executionId, onClose }) {
  const [executionData, setExecutionData] = useState(null)
  const [executionLogs, setExecutionLogs] = useState([])
  const [promptEvent, setPromptEvent] = useState(null)
  const [pipelineEvents, setPipelineEvents] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const { logs: allLogs } = useSocket()

  useEffect(() => {
    async function fetchData() {
      try {
        const response = await fetch(`/api/agent-execution/${executionId}`)
        const data = await response.json()
        if (data.success) {
          setExecutionData(current => mergeObjectStable(current, data.execution))
          setExecutionLogs(current => mergeArrayByIdStable(current, data.logs || [], 'id'))
          setPromptEvent(current => mergeObjectStable(current, data.prompt_event || null))
        } else {
          setError(data.error || 'Failed to load execution data')
        }
      } catch (err) {
        setError('Failed to load execution data')
      } finally {
        setLoading(false)
      }
    }
    fetchData()
  }, [executionId])

  // Fetch pipeline events once pipeline_run_id is available
  useEffect(() => {
    const pipelineRunId = executionData?.pipeline_run_id
    if (!pipelineRunId) return

    async function fetchPipelineEvents() {
      try {
        const response = await fetch(`/pipeline-run-events?pipeline_run_id=${pipelineRunId}`)
        const data = await response.json()
        if (data.success) {
          setPipelineEvents(current => mergeArrayByIdStable(current, data.events || [], 'event_id'))
        }
      } catch (err) {
        console.error('Error fetching pipeline events:', err)
      }
    }
    fetchPipelineEvents()
  }, [executionData?.pipeline_run_id])

  // Close on Escape key
  useEffect(() => {
    const handleKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [onClose])

  const pipelineRunId = executionData?.pipeline_run_id ?? null

  const { agentState, mergedLogs, mergedPipelineEvents } = useMemo(() => {
    if (!executionData) return { agentState: {}, mergedLogs: [], mergedPipelineEvents: [] }

    // Merge API logs with live WebSocket events
    const logs = mergeAgentExecutionEvents(executionLogs, allLogs, executionData)

    // Merge historical pipeline events with any newer live events
    let pipelineEventsList = [...pipelineEvents]
    if (pipelineRunId) {
      let lastHistTimestamp = 0
      if (pipelineEvents.length > 0) {
        const lastEvent = pipelineEvents[pipelineEvents.length - 1]
        lastHistTimestamp = normalizeTimestamp(lastEvent.timestamp || lastEvent.created_at) || 0
      }
      const livePipelineEvents = allLogs.filter(event => {
        const eventRunId = event.pipeline_run_id || event.data?.pipeline_run_id
        if (eventRunId !== pipelineRunId) return false
        const eventTimestamp = normalizeTimestamp(event.timestamp)
        return eventTimestamp > lastHistTimestamp
      })
      if (livePipelineEvents.length > 0) {
        pipelineEventsList = [...pipelineEventsList, ...livePipelineEvents]
      }
    }

    // Build agent state from merged logs (most recent first)
    let lastTodoWrite = null
    let lastTextMessage = null
    let lastToolCall = null
    let previousToolCall = null
    let previousToolResult = null

    for (let i = logs.length - 1; i >= 0; i--) {
      const log = logs[i]
      const event = log.raw_event?.event
      const ts = normalizeTimestamp(log.timestamp)

      if (event?.type === 'assistant' && event?.message?.content) {
        const contents = Array.isArray(event.message.content)
          ? event.message.content
          : [event.message.content]

        for (const item of contents) {
          if (item.type === 'text' && !lastTextMessage && item.text?.trim()) {
            lastTextMessage = { text: item.text, timestamp: ts, agent: log.agent }
          } else if (item.type === 'tool_use') {
            if (item.name === 'TodoWrite' && !lastTodoWrite) {
              lastTodoWrite = { todos: item.input?.todos || [], timestamp: ts, agent: log.agent }
            } else if (item.name !== 'TodoWrite') {
              if (!lastToolCall) {
                lastToolCall = { name: item.name, input: item.input, timestamp: ts, agent: log.agent }
              } else if (!previousToolCall) {
                previousToolCall = { name: item.name, input: item.input, timestamp: ts, agent: log.agent }
              }
            }
          }
        }
      }

      if (lastTodoWrite && lastTextMessage && lastToolCall && previousToolCall) break
    }

    if (previousToolCall) {
      for (let i = logs.length - 1; i >= 0; i--) {
        const log = logs[i]
        const event = log.raw_event?.event
        const ts = normalizeTimestamp(log.timestamp)

        const isBeforeOrAtLast = ts && lastToolCall?.timestamp && ts <= lastToolCall.timestamp
        const isAfterOrAtPrev = ts && previousToolCall?.timestamp && ts >= previousToolCall.timestamp

        if (isBeforeOrAtLast && isAfterOrAtPrev && event?.type === 'user' && event?.message?.content) {
          const contents = Array.isArray(event.message.content)
            ? event.message.content
            : [event.message.content]

          for (const item of contents) {
            if (item.type === 'tool_result') {
              const timeDiff = Math.abs(ts - previousToolCall.timestamp)
              if (timeDiff < 60) {
                previousToolResult = { content: item.content, timestamp: ts, agent: log.agent }
                break
              }
            }
          }
          if (previousToolResult) break
        }
      }
    }

    let inputPrompt = null
    if (promptEvent) {
      // agent-events-* has two document formats depending on indexing path:
      //   - Directly indexed by observability.py: prompt at top level (promptEvent.prompt)
      //   - Indexed via log_collector raw_event wrapper: promptEvent.raw_event.data.prompt
      const promptText = promptEvent.prompt || promptEvent.raw_event?.data?.prompt
      const ts = normalizeTimestamp(promptEvent.timestamp)
      if (promptText && ts) {
        inputPrompt = {
          text: promptText,
          timestamp: ts,
          agent: promptEvent.agent_name || promptEvent.agent,
        }
      }
    }

    return {
      agentState: { lastTodoWrite, lastTextMessage, lastToolCall, previousToolCall, previousToolResult, inputPrompt },
      mergedLogs: logs,
      mergedPipelineEvents: pipelineEventsList,
    }
  }, [executionData, executionLogs, allLogs, promptEvent, pipelineEvents, pipelineRunId])

  const agentLabel = executionData?.agent
    ? executionData.agent.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
    : 'Agent Execution'

  const modal = (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 9999,
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'center',
        paddingTop: 48,
        paddingBottom: 48,
        background: 'rgba(0,0,0,0.6)',
      }}
      onClick={onClose}
    >
      <div
        style={{
          width: '90vw',
          maxWidth: 1100,
          maxHeight: 'calc(100vh - 96px)',
          overflowY: 'auto',
          background: 'var(--gh-canvas, #0d1117)',
          borderRadius: 10,
          border: '1px solid var(--gh-border, #30363d)',
          boxShadow: '0 24px 48px rgba(0,0,0,0.5)',
          display: 'flex',
          flexDirection: 'column',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Modal header */}
        <div style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '12px 20px',
          borderBottom: '1px solid var(--gh-border, #30363d)',
          flexShrink: 0,
        }}>
          <span style={{ fontWeight: 600, fontSize: 14 }}>{agentLabel}</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <a
              href={`/agent-execution/${executionId}`}
              target="_blank"
              rel="noreferrer"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 4,
                fontSize: 12,
                color: 'var(--gh-accent-fg, #58a6ff)',
                textDecoration: 'none',
              }}
            >
              View full execution <ExternalLink size={12} />
            </a>
            <button
              onClick={onClose}
              style={{
                background: 'none',
                border: 'none',
                cursor: 'pointer',
                color: 'var(--gh-fg-muted, #8b949e)',
                display: 'flex',
                padding: 4,
              }}
              title="Close"
            >
              <X size={16} />
            </button>
          </div>
        </div>

        {/* Modal body */}
        <div style={{ padding: 20, overflowY: 'auto' }}>
          {loading && (
            <div style={{ display: 'flex', justifyContent: 'center', padding: 64 }}>
              <RefreshCw size={32} className="animate-spin" style={{ color: 'var(--gh-accent-fg, #58a6ff)', animation: 'spin 1s linear infinite' }} />
            </div>
          )}
          {error && (
            <div style={{ textAlign: 'center', padding: 64, color: 'var(--gh-danger, #f85149)' }}>
              {error}
            </div>
          )}
          {!loading && !error && executionData && (
            <>
              <TokenUsagePanel logs={mergedLogs} promptText={agentState.inputPrompt?.text} />
              <AgentExecutionState
                executionId={executionId}
                executionData={executionData}
                pipelineRunId={executionData.pipeline_run_id}
                agentState={agentState}
                mergedPipelineEvents={mergedPipelineEvents}
              />
            </>
          )}
        </div>
      </div>
    </div>
  )

  return createPortal(modal, document.body)
}
