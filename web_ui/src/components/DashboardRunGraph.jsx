import { useCallback, useMemo, useState, useEffect } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { MessageCircle } from 'lucide-react'
import PipelineFlowGraph from './PipelineFlowGraph'
import RunDuration from './RunDuration'
import CopyableId from './CopyableId'
import ToolUseTimeline from './ToolUseTimeline'
import { TodoList } from './AgentState'
import { useDashboardRunData } from '../hooks/useDashboardRunData'
import { useSocket } from '../contexts/SocketContext'

// Same palette as ToolUseTimeline — keeps badge colors in sync across both components
const TOOL_COLORS = {
  Read: '#4493f8', Edit: '#3fb950', Write: '#56d364', Bash: '#f0883e',
  Grep: '#ffa657', Glob: '#79c0ff', WebSearch: '#a371f7', WebFetch: '#d2a8ff',
  Agent: '#ff7b72', Task: '#ff7b72', Skill: '#58a6ff',
  TodoWrite: '#e3b341', TodoRead: '#e3b341', NotebookEdit: '#bc8cff',
}
const OVERFLOW_PALETTE = ['#f778ba', '#39d353', '#db6d28', '#0075ca', '#e4e669', '#c5def5', '#bfd4f2']

function toolColor(name) {
  if (TOOL_COLORS[name]) return TOOL_COLORS[name]
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return OVERFLOW_PALETTE[h % OVERFLOW_PALETTE.length]
}

function formatToolInput(name, input) {
  if (!input) return ''
  switch (name) {
    case 'Bash': return input.command || ''
    case 'Read': return input.file_path || ''
    case 'Grep': return `"${input.pattern || ''}" in ${input.path || '.'}`
    case 'Edit': return input.file_path || ''
    case 'Write': return input.file_path || ''
    case 'Glob': return input.pattern || ''
    default: return input.description || ''
  }
}

function fmtEventTime(date) {
  if (!date) return ''
  const m = date.getMinutes()
  const s = String(date.getSeconds()).padStart(2, '0')
  return `${m}:${s}`
}

/**
 * Parse tool_use items from a raw assistant stream event.
 * Returns [{ id, ts, toolName, input, outputTokens }, ...]
 */
function parseToolEvents(rawEvent, ts, idPrefix) {
  if (!rawEvent || rawEvent.type !== 'assistant') return []
  const content = rawEvent.message?.content
  if (!Array.isArray(content)) return []
  const outputTokens = rawEvent.message?.usage?.output_tokens || 0
  return content
    .filter(item => item.type === 'tool_use' && item.name)
    .map((item, i) => {
      const toolName = item.name === 'Task' && item.input?.subagent_type
        ? item.input.subagent_type
        : item.name === 'Skill' && item.input?.skill
          ? item.input.skill
          : item.name
      return { id: `${idPrefix}-${i}`, ts, toolName, input: item.input, outputTokens }
    })
}

export default function DashboardRunGraph({ run }) {
  const navigate = useNavigate()
  const { graphEvents, mergedEvents, workflowConfig, loading } = useDashboardRunData(run)
  const { socket } = useSocket()

  // Live tool events collected directly from the socket for this run.
  const [liveToolEvents, setLiveToolEvents] = useState([])

  // Reset live events when the run changes
  useEffect(() => {
    setLiveToolEvents([])
  }, [run?.id])

  // Subscribe directly to claude_stream_event for this run.
  // Bypasses useSocket().logs (a shared 200-item buffer across all runs,
  // pre-populated with flat ES records that lack the `event` field).
  useEffect(() => {
    if (!socket || !run?.id) return
    const handleEvent = (data) => {
      if (data.pipeline_run_id !== run.id) return
      const ts = new Date(typeof data.timestamp === 'number'
        ? data.timestamp * 1000
        : data.timestamp)
      const idPrefix = `live-${data.timestamp}-${data.agent || ''}`
      const parsed = parseToolEvents(data.event, ts, idPrefix)
      if (parsed.length === 0) return
      setLiveToolEvents(prev => {
        const existingIds = new Set(prev.map(e => e.id))
        const fresh = parsed.filter(e => !existingIds.has(e.id))
        if (fresh.length === 0) return prev
        return [...prev, ...fresh]
      })
    }
    socket.on('claude_stream_event', handleEvent)
    return () => socket.off('claude_stream_event', handleEvent)
  }, [socket, run?.id])

  // Parse historical tool events from the already-fetched mergedEvents
  const historicalToolEvents = useMemo(() => {
    const events = []
    for (const evt of mergedEvents) {
      if (evt.event_category !== 'claude_log') continue
      events.push(...parseToolEvents(
        evt.raw_event?.event,
        new Date(evt.timestamp),
        evt.id || evt.timestamp,
      ))
    }
    return events
  }, [mergedEvents])

  // Merge historical + live, dedup by id (historical first so live events
  // win on duplicates when the API poll catches up to what the socket already saw)
  const toolEvents = useMemo(() => {
    const seen = new Set()
    const merged = []
    for (const e of [...historicalToolEvents, ...liveToolEvents]) {
      if (!seen.has(e.id)) {
        seen.add(e.id)
        merged.push(e)
      }
    }
    return merged
  }, [historicalToolEvents, liveToolEvents])

  // Most recent tool event — drives the overlay badge.
  // Same source as ToolUseTimeline so both always reflect the same event.
  const latestToolEvent = useMemo(() => {
    if (run?.status !== 'active' || toolEvents.length === 0) return null
    return toolEvents.reduce((latest, e) => (!latest || e.ts > latest.ts ? e : latest), null)
  }, [toolEvents, run?.status])

  // Most recent TodoWrite input — drives the compact todo overlay.
  // Derived from the same toolEvents so it stays in sync with the timeline.
  const latestTodos = useMemo(() => {
    const writes = toolEvents.filter(e => e.toolName === 'TodoWrite')
    if (writes.length === 0) return null
    const latest = writes.reduce((a, b) => (a.ts > b.ts ? a : b))
    const todos = latest.input?.todos
    return todos?.length ? todos : null
  }, [toolEvents])

  const handleClick = useCallback(() => {
    navigate({ to: '/pipeline-run', search: { runId: run.id } })
  }, [navigate, run.id])

  return (
    <div
      className="bg-gh-canvas-subtle border border-gh-border rounded-md overflow-hidden hover:border-gh-accent-primary transition-colors flex flex-col min-h-[400px] md:min-h-0"
    >
      {/* Compact header */}
      <div className="flex items-center gap-3 px-3 py-2 border-b border-gh-border min-w-0 flex-shrink-0">
        <div className="min-w-0 flex-1 truncate">
          <div className="text-xs text-gh-fg-muted font-normal leading-tight truncate">
            {run.project}
          </div>
          <h3
            onClick={handleClick}
            className="text-sm font-semibold truncate cursor-pointer">
            {run.issue_title}
          </h3>
        </div>
        {run.issue_url ? (
          <a
            href={run.issue_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-gh-accent-fg hover:underline flex-shrink-0"
            onClick={e => e.stopPropagation()}
          >
            #{run.issue_number}
          </a>
        ) : (
          <span className="text-xs text-gh-fg-muted flex-shrink-0">#{run.issue_number}</span>
        )}
        <CopyableId id={run.id} className="text-xs text-gh-fg-muted flex-shrink-0" />
        <RunDuration
          startedAt={run.started_at}
          endedAt={run.ended_at}
          className="text-xs text-gh-fg-muted flex-shrink-0"
        />
      </div>

      {/* Tool use timeline */}
      <div className="flex-shrink-0 border-b border-gh-border">
        <ToolUseTimeline toolEvents={toolEvents} />
      </div>

      {/* Graph body */}
      <div className="flex-1 min-h-0 relative">
        {run.status === 'feedback_listening' && (
          <div
            className="absolute inset-0 z-10 pointer-events-none"
            style={{ animation: 'feedbackBorderPulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite' }}
          />
        )}
        <PipelineFlowGraph
          graphEvents={graphEvents}
          allEvents={mergedEvents}
          workflowConfig={workflowConfig}
          selectedPipelineRun={run}
          height="100%"
          fitViewAlign="center"
          showAllNodes={false}
          minZoom={0.1}
          maxZoom={0.8}
          loading={loading}
          emptyMessage="Waiting for events..."
        />
        {latestToolEvent && (
          <div
            className="absolute z-10 top-2 flex items-center gap-1.5 bg-gh-canvas rounded px-2 py-1 pointer-events-none overflow-hidden"
            style={{ left: 52, right: 8, opacity: 0.75 }}
          >
            <span className="text-xs text-gh-fg-muted font-mono flex-shrink-0">
              {fmtEventTime(latestToolEvent.ts)}
            </span>
            <span
              className="px-1.5 py-0.5 rounded text-xs font-semibold flex-shrink-0"
              style={{ backgroundColor: toolColor(latestToolEvent.toolName), color: '#fff' }}
            >
              {latestToolEvent.toolName}
            </span>
            <span className="text-xs text-gh-fg font-mono truncate">
              {formatToolInput(latestToolEvent.toolName, latestToolEvent.input)}
            </span>
          </div>
        )}
        {latestTodos && (
          <div
            className="absolute z-10 bg-gh-canvas border border-gh-border rounded px-2 py-1.5 pointer-events-none overflow-y-auto"
            style={{ right: 8, top: latestToolEvent ? 38 : 8, maxWidth: 220, maxHeight: 180, opacity: 0.85 }}
          >
            <TodoList todos={latestTodos} compact />
          </div>
        )}
        {run.status === 'feedback_listening' && run.issue_url && (
          <a
            href={run.issue_url}
            target="_blank"
            rel="noopener noreferrer"
            className="absolute top-2 right-2 z-20 animate-pulse hover:opacity-100"
            style={{ color: '#f0883e' }}
            title="Awaiting feedback — click to open issue"
            onClick={e => e.stopPropagation()}
          >
            <MessageCircle size={24} />
          </a>
        )}
      </div>

      <style>{`
        @keyframes feedbackBorderPulse {
          0%, 100% { box-shadow: inset 0 0 0 1px rgba(240, 136, 62, 0.35); }
          50%       { box-shadow: inset 0 0 0 3px rgba(240, 136, 62, 0.85); }
        }
      `}</style>
    </div>
  )
}
