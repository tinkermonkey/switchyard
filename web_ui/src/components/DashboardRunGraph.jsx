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

function fmtTok(n) {
  if (!n) return '0'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 10_000) return `${(n / 1_000).toFixed(0)}K`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

function fmtCost(usd) {
  if (!usd) return null
  if (usd < 0.01) return `$${usd.toFixed(4)}`
  return `$${usd.toFixed(2)}`
}

// Claude model pricing (USD per million tokens).
// Used to estimate cost from live token counts before the metrics job runs.
const MODEL_PRICING = {
  'claude-opus-4-6':            { input: 5,    output: 25,  cacheRead: 0.50,  cacheWrite: 6.25  },
  'claude-opus-4-5':            { input: 5,    output: 25,  cacheRead: 0.50,  cacheWrite: 6.25  },
  'claude-sonnet-4-6':          { input: 3,    output: 15,  cacheRead: 0.30,  cacheWrite: 3.75  },
  'claude-sonnet-4-5':          { input: 3,    output: 15,  cacheRead: 0.30,  cacheWrite: 3.75  },
  'claude-haiku-4-5-20251001':  { input: 1,    output: 5,   cacheRead: 0.10,  cacheWrite: 1.25  },
  'claude-haiku-4-5':           { input: 1,    output: 5,   cacheRead: 0.10,  cacheWrite: 1.25  },
}
const DEFAULT_PRICING = MODEL_PRICING['claude-sonnet-4-6']

function estimateCostFromUsage(usage, model) {
  if (!usage) return 0
  const p = MODEL_PRICING[model] || DEFAULT_PRICING
  const M = 1_000_000
  return (
    (usage.input_tokens                || 0) * p.input       / M +
    (usage.output_tokens               || 0) * p.output      / M +
    (usage.cache_read_input_tokens     || 0) * p.cacheRead   / M +
    (usage.cache_creation_input_tokens || 0) * p.cacheWrite  / M
  )
}

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
  // Agent identifier of the most recent live execution — resets todos when the active agent changes.
  const [currentLiveAgent, setCurrentLiveAgent] = useState(null)

  // Token summary fetched from /api/pipeline-run/<id>/token-usage-live.
  // Aggregates claude-streams-* directly — as fresh as the ES refresh interval (~10s).
  const [tokenSummary, setTokenSummary] = useState(null)
  // Timestamp of the most recent event included in tokenSummary (ISO string from ES).
  // Used to filter liveTokenAccum so the socket delta only covers events after the ES snapshot.
  const [streamSummaryTs, setStreamSummaryTs] = useState(null)
  // Individual token events accumulated live from socket — each entry has { ts, input, output, cacheRead, cacheCreation, cost }.
  // Kept as a list (not a running total) so we can filter by timestamp against streamSummaryTs.
  const [liveTokenAccum, setLiveTokenAccum] = useState([])
  // URL of the most recent GitHub comment posted for this run — used to deep-link to the
  // actual comment in feedback_listening state instead of just the issue/discussion root.
  const [latestCommentUrl, setLatestCommentUrl] = useState(null)

  // Reset live state when the run changes
  useEffect(() => {
    setLiveToolEvents([])
    setCurrentLiveAgent(null)
    setTokenSummary(null)
    setStreamSummaryTs(null)
    setLiveTokenAccum([])
    setLatestCommentUrl(null)
  }, [run?.id])

  // Stable fetch function so the agent-completion listener can trigger an immediate refresh.
  // Accepts an optional AbortSignal so the polling effect can cancel in-flight requests
  // when the run changes, preventing a stale response from overwriting the reset state.
  const fetchTokenSummary = useCallback((signal) => {
    if (!run?.id) return
    fetch(`/api/pipeline-run/${run.id}/token-usage-live`, signal ? { signal } : undefined)
      .then(r => r.json())
      .then(data => {
        if (data.success) {
          setTokenSummary(data.summary || null)
          setStreamSummaryTs(data.latest_event_timestamp ? new Date(data.latest_event_timestamp) : null)
        }
      })
      .catch(() => {})
  }, [run?.id])

  // Poll the live stream aggregation every 30s.
  // The endpoint queries claude-streams-* directly so data is at most ~10s stale
  // (ES refresh interval). Socket events newer than streamSummaryTs fill the gap.
  useEffect(() => {
    if (!run?.id) return
    const controller = new AbortController()
    fetchTokenSummary(controller.signal)
    const id = setInterval(() => fetchTokenSummary(controller.signal), 30000)
    return () => { controller.abort(); clearInterval(id) }
  }, [run?.id, fetchTokenSummary])

  // Fetch the URL of the most recent GitHub comment so we can deep-link to it
  // when the run is in feedback_listening state (conversational loops post a
  // comment on a discussion or issue; linking to the issue root isn't helpful).
  useEffect(() => {
    if (!run?.id || run.status !== 'feedback_listening') return
    let cancelled = false
    const fetchComment = () => {
      fetch(`/api/pipeline-run/${run.id}/latest-comment`)
        .then(r => r.json())
        .then(data => { if (!cancelled && data.success && data.comment_url) setLatestCommentUrl(data.comment_url) })
        .catch(() => {})
    }
    fetchComment()
    // Poll in case there's a brief ES indexing lag after the comment was posted.
    const id = setInterval(fetchComment, 10000)
    return () => { cancelled = true; clearInterval(id) }
  }, [run?.id, run?.status])

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

      // Collect per-event token data so the display can filter by timestamp against the ES snapshot.
      const usage = data.event?.message?.usage
      if (usage) {
        const model = data.event?.message?.model
        setLiveTokenAccum(prev => [...prev, {
          ts,
          input:         usage.input_tokens                  || 0,
          output:        usage.output_tokens                 || 0,
          cacheRead:     usage.cache_read_input_tokens       || 0,
          cacheCreation: usage.cache_creation_input_tokens   || 0,
          cost:          estimateCostFromUsage(usage, model),
        }])
      }

      const idPrefix = `live-${data.timestamp}-${data.agent || ''}`
      const parsed = parseToolEvents(data.event, ts, idPrefix)
      if (parsed.length === 0) return
      const agent = data.agent ?? null
      setCurrentLiveAgent(agent)
      setLiveToolEvents(prev => {
        const existingIds = new Set(prev.map(e => e.id))
        const fresh = parsed.filter(e => !existingIds.has(e.id)).map(e => ({ ...e, agent }))
        if (fresh.length === 0) return prev
        return [...prev, ...fresh]
      })
    }
    socket.on('claude_stream_event', handleEvent)
    return () => socket.off('claude_stream_event', handleEvent)
  }, [socket, run?.id])

  // Refresh token summary immediately when an agent finishes so cost updates without
  // waiting for the next 30s poll cycle.
  useEffect(() => {
    if (!socket || !run?.id) return
    const handleAgentEvent = (data) => {
      if (data.pipeline_run_id !== run.id) return
      if (data.event_type === 'agent_completed' || data.event_type === 'agent_failed') {
        fetchTokenSummary()
      }
    }
    socket.on('agent_event', handleAgentEvent)
    return () => socket.off('agent_event', handleAgentEvent)
  }, [socket, run?.id, fetchTokenSummary])

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

  // Combined token display value: ES snapshot (tokenSummary, ~10s stale) plus socket
  // events newer than the snapshot timestamp (liveTokenAccum delta).
  // This gives a near-real-time view without double-counting events already indexed in ES.
  const displayTokens = useMemo(() => {
    const cutoffMs = streamSummaryTs ? streamSummaryTs.getTime() : 0
    let di = 0, dout = 0, dcr = 0, dcc = 0, dcost = 0
    for (const ev of liveTokenAccum) {
      if (ev.ts.getTime() > cutoffMs) {
        di    += ev.input
        dout  += ev.output
        dcr   += ev.cacheRead
        dcc   += ev.cacheCreation
        dcost += ev.cost
      }
    }
    if (!tokenSummary && di + dout === 0) return null
    const base = tokenSummary || { total_direct_input: 0, total_output_tokens: 0, total_cache_read: 0, total_cache_creation: 0, total_cost_usd: 0 }
    return {
      total_direct_input:   base.total_direct_input   + di,
      total_output_tokens:  base.total_output_tokens  + dout,
      total_cache_read:     base.total_cache_read      + dcr,
      total_cache_creation: base.total_cache_creation  + dcc,
      total_cost_usd:       base.total_cost_usd        + dcost,
    }
  }, [tokenSummary, streamSummaryTs, liveTokenAccum])

  // Most recent tool event — drives the overlay badge.
  // Same source as ToolUseTimeline so both always reflect the same event.
  const latestToolEvent = useMemo(() => {
    if (run?.status !== 'active' || toolEvents.length === 0) return null
    return toolEvents.reduce((latest, e) => (!latest || e.ts > latest.ts ? e : latest), null)
  }, [toolEvents, run?.status])

  // Most recent TodoWrite input — drives the compact todo overlay.
  // Scoped to the current live agent execution so todos clear when the active agent changes.
  const latestTodos = useMemo(() => {
    if (run?.status !== 'active') return null
    const writes = liveToolEvents.filter(e => e.toolName === 'TodoWrite' && e.agent === currentLiveAgent)
    if (writes.length === 0) return null
    const latest = writes.reduce((a, b) => (a.ts > b.ts ? a : b))
    const todos = latest.input?.todos
    return todos?.length ? todos : null
  }, [liveToolEvents, currentLiveAgent, run?.status])

  const handleClick = useCallback(() => {
    navigate({ to: '/pipeline-run', search: { runId: run.id } })
  }, [navigate, run.id])

  return (
    <div
      className="bg-gh-canvas-subtle border border-gh-border rounded-md overflow-hidden hover:border-gh-accent-primary transition-colors flex flex-col min-h-[500px] md:min-h-0"
    >
      {/* Compact header */}
      <div className="flex flex-wrap md:flex-nowrap items-center gap-x-3 gap-y-1 px-3 py-2 border-b border-gh-border min-w-0 flex-shrink-0">
        <div className="min-w-0 w-full md:w-auto md:flex-1">
          <div className="text-xs text-gh-fg-muted font-normal leading-tight md:truncate">
            {run.project}
          </div>
          <h3
            onClick={handleClick}
            className="text-sm font-semibold md:truncate cursor-pointer">
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

      {/* Tool use timeline + token counter */}
      <div className="flex-shrink-0 border-b border-gh-border flex items-stretch min-w-0">
        <div className="flex-1 min-w-0">
          <ToolUseTimeline toolEvents={toolEvents} />
        </div>
        {displayTokens && (() => {
          const tok = displayTokens
          const cost = fmtCost(tok.total_cost_usd)
          return (
            <div className="flex-shrink-0 border-l border-gh-border px-3 py-2 flex flex-col justify-center gap-1 min-w-[90px]">
              {cost && (
                <>
                  <div className="text-xs text-gh-fg-muted font-semibold uppercase tracking-wide leading-none">
                    Cost <span className="font-normal normal-case tracking-normal">(est)</span>
                  </div>
                  <div className="text-xs font-mono font-semibold text-gh-fg mb-1">~{cost}</div>
                </>
              )}
              <div className="text-xs text-gh-fg-muted font-semibold uppercase tracking-wide leading-none mb-1">Tokens</div>
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-xs text-gh-fg-muted">input</span>
                <span className="text-xs font-mono text-gh-fg">{fmtTok(tok.total_direct_input || 0)}</span>
              </div>
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-xs text-gh-fg-muted">output</span>
                <span className="text-xs font-mono text-gh-fg">{fmtTok(tok.total_output_tokens || 0)}</span>
              </div>
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-xs text-gh-fg-muted">c.read</span>
                <span className="text-xs font-mono text-gh-fg">{fmtTok(tok.total_cache_read || 0)}</span>
              </div>
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-xs text-gh-fg-muted">c.write</span>
                <span className="text-xs font-mono text-gh-fg">{fmtTok(tok.total_cache_creation || 0)}</span>
              </div>
            </div>
          )
        })()}
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
            <RunDuration
              startedAt={latestToolEvent.ts.getTime()}
              className="text-xs text-gh-fg-muted font-mono flex-shrink-0"
            />
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
        {run.status === 'feedback_listening' && (latestCommentUrl || run.issue_url) && (
          <a
            href={latestCommentUrl ?? run.issue_url}
            target="_blank"
            rel="noopener noreferrer"
            className="absolute top-2 right-2 z-20 animate-pulse hover:opacity-100"
            style={{ color: '#f0883e' }}
            title={latestCommentUrl ? "Awaiting feedback — click to view comment" : "Awaiting feedback — click to open issue"}
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
