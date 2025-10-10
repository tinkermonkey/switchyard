import { useState } from 'react'
import { useSocket } from '../contexts/SocketContext'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import Modal from './Modal'
import {
  Inbox,
  Settings,
  FileText,
  Rocket,
  CheckCircle,
  XCircle,
  ChevronDown,
  ChevronRight,
  ExternalLink
} from 'lucide-react'

export default function EventTimeline() {
  const { events, clearEvents } = useSocket()
  const [autoScroll, setAutoScroll] = useState(true)

  const renderEvent = (event) => {
    const timestamp = new Date(event.timestamp).toLocaleTimeString('en-US', { timeZone: 'UTC', hour12: false }) + ' UTC'

    switch (event.event_type) {
      case 'task_received':
        return <TaskReceivedEvent event={event} timestamp={timestamp} />
      case 'agent_initialized':
        return <AgentInitializedEvent event={event} timestamp={timestamp} />
      case 'prompt_constructed':
        return <PromptConstructedEvent event={event} timestamp={timestamp} />
      case 'claude_api_call_started':
        return <ClaudeCallStartedEvent event={event} timestamp={timestamp} />
      case 'claude_api_call_completed':
        return <ClaudeCallCompletedEvent event={event} timestamp={timestamp} />
      case 'agent_completed':
      case 'agent_failed':
        return <AgentCompletedEvent event={event} timestamp={timestamp} />
      default:
        return <GenericEvent event={event} timestamp={timestamp} />
    }
  }

  return (
    <div>
      <div className="flex justify-between items-center mb-3">
        <h2 className="text-gh-accent-primary text-lg font-semibold">Event Timeline</h2>
        <div className="flex gap-2">
          <button
            onClick={() => setAutoScroll(!autoScroll)}
            className="px-3 py-1 bg-gh-canvas-subtle border border-gh-border rounded text-sm hover:bg-gh-border-muted transition-colors"
          >
            Auto-scroll: {autoScroll ? 'ON' : 'OFF'}
          </button>
          <button
            onClick={clearEvents}
            className="px-3 py-1 bg-gh-canvas-subtle border border-gh-border rounded text-sm hover:bg-gh-border-muted transition-colors"
          >
            Clear Events
          </button>
        </div>
      </div>

      <div className="space-y-3">
        {events.length === 0 ? (
          <div className="bg-gh-canvas-subtle p-4 rounded-md border border-gh-border text-center text-gh-fg-muted">
            No events yet
          </div>
        ) : (
          events.map((event, idx) => (
            <div key={idx}>
              {renderEvent(event)}
            </div>
          ))
        )}
      </div>
    </div>
  )
}

function TaskReceivedEvent({ event, timestamp }) {
  return (
    <EventCard icon={<Inbox />} title="Task Received" badge={event.agent} badgeColor="info" timestamp={timestamp}>
      <EventDetail label="Project" value={event.project} />
      <EventDetail label="Board" value={event.data?.board || 'N/A'} />
      <EventDetail label="Issue" value={`#${event.data?.issue_number || 'N/A'}`} />
      <EventDetail label="Trigger" value={event.data?.trigger || 'manual'} />
    </EventCard>
  )
}

function AgentInitializedEvent({ event, timestamp }) {
  const mcpCount = event.data?.mcp_servers?.length || 0
  return (
    <EventCard icon={<Settings />} title="Agent Initialized" badge={event.agent} badgeColor="info" timestamp={timestamp}>
      <EventDetail label="Model" value={event.data?.model || 'default'} />
      <EventDetail label="Timeout" value={`${event.data?.timeout || 'default'}s`} />
      <EventDetail label="MCP Servers" value={`${mcpCount} configured`} />
    </EventCard>
  )
}

function PromptConstructedEvent({ event, timestamp }) {
  const [showModal, setShowModal] = useState(false)
  const tokens = event.data?.estimated_tokens || Math.floor(event.data?.prompt_length / 4)

  return (
    <>
      <EventCard icon={<FileText />} title="Prompt Constructed" badge={event.agent} badgeColor="info" timestamp={timestamp}>
        <div className="flex gap-3">
          <Metric label="Length" value={event.data?.prompt_length?.toLocaleString()} />
          <Metric label="Est. Tokens" value={`~${tokens?.toLocaleString()}`} />
        </div>
        {event.data?.prompt && (
          <button
            onClick={() => setShowModal(true)}
            className="text-gh-accent-primary text-sm hover:underline flex items-center gap-1 mt-2"
          >
            <ExternalLink className="w-4 h-4" />
            View full prompt
          </button>
        )}
      </EventCard>

      {showModal && (
        <Modal title="Prompt" onClose={() => setShowModal(false)}>
          <div className="prose">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {event.data.prompt}
            </ReactMarkdown>
          </div>
        </Modal>
      )}
    </>
  )
}

function ClaudeCallStartedEvent({ event, timestamp }) {
  const model = event.data?.model || 'claude'
  return (
    <EventCard icon={<Rocket />} title="Claude API Call Started" badge={model} badgeColor="warning" timestamp={timestamp}>
      <EventDetail label="Agent" value={event.agent} />
      {event.data?.input_tokens && (
        <Metric label="Input Tokens" value={event.data.input_tokens.toLocaleString()} />
      )}
      <div className="w-full h-1.5 bg-gh-border-muted rounded-full mt-2 overflow-hidden">
        <div className="h-full bg-gradient-to-r from-gh-accent-emphasis to-gh-accent-primary animate-pulse-custom w-full" />
      </div>
    </EventCard>
  )
}

function ClaudeCallCompletedEvent({ event, timestamp }) {
  const duration = ((event.data?.duration_ms || 0) / 1000).toFixed(2)
  const inputTokens = event.data?.input_tokens || 0
  const outputTokens = event.data?.output_tokens || 0
  const totalTokens = event.data?.total_tokens || (inputTokens + outputTokens)
  const costEstimate = ((inputTokens * 0.003 + outputTokens * 0.015) / 1000).toFixed(4)

  return (
    <EventCard icon={<CheckCircle />} title="Claude API Call Completed" badge={`${duration}s`} badgeColor="success" timestamp={timestamp}>
      <div className="flex gap-3 flex-wrap">
        <Metric label="Input" value={inputTokens.toLocaleString()} />
        <Metric label="Output" value={outputTokens.toLocaleString()} />
        <Metric label="Total" value={totalTokens.toLocaleString()} />
        <Metric label="Est. Cost" value={`$${costEstimate}`} />
      </div>
    </EventCard>
  )
}

function AgentCompletedEvent({ event, timestamp }) {
  const isSuccess = event.event_type === 'agent_completed'
  const durationMs = event.data?.duration_ms
  const duration = durationMs
    ? durationMs < 60000
      ? `${(durationMs / 1000).toFixed(1)}s`
      : `${(durationMs / 60000).toFixed(1)} min`
    : 'N/A'

  return (
    <EventCard
      icon={isSuccess ? <CheckCircle /> : <XCircle />}
      title={isSuccess ? 'Agent Completed' : 'Agent Failed'}
      badge={event.agent}
      badgeColor={isSuccess ? 'success' : 'error'}
      timestamp={timestamp}
    >
      <EventDetail label="Duration" value={duration} />
      <EventDetail label="Status" value={isSuccess ? '✓ Success' : '✗ Failed'} />
      {!isSuccess && event.data?.error && (
        <EventDetail label="Error" value={event.data.error} />
      )}
    </EventCard>
  )
}

function GenericEvent({ event, timestamp }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <EventCard icon={<FileText />} title={event.event_type} badge={event.agent} badgeColor="info" timestamp={timestamp}>
      <div className="text-xs text-gh-fg-subtle">Task: {event.task_id}</div>
      <button
        onClick={() => setExpanded(!expanded)}
        className="text-gh-accent-primary text-sm hover:underline flex items-center gap-1 mt-1"
      >
        {expanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
        View details
      </button>
      {expanded && (
        <pre className="mt-2 bg-gh-canvas p-2 rounded overflow-auto text-xs">
          {JSON.stringify(event.data, null, 2)}
        </pre>
      )}
    </EventCard>
  )
}

function EventCard({ icon, title, badge, badgeColor, timestamp, children }) {
  const badgeColors = {
    info: 'bg-gh-accent-emphasis text-white',
    success: 'bg-gh-success text-white',
    warning: 'bg-gh-warning text-white',
    error: 'bg-gh-danger text-white',
  }

  return (
    <div className="bg-gh-canvas-subtle rounded-md p-4 border border-gh-border">
      <div className="flex items-center gap-3 pb-3 mb-3 border-b border-gh-border-muted">
        <span className="text-xl">{icon}</span>
        <div className="flex-1 font-semibold">{title}</div>
        <span className={`px-2 py-1 rounded-full text-xs font-semibold ${badgeColors[badgeColor]}`}>
          {badge}
        </span>
        <span className="text-gh-fg-subtle text-xs">{timestamp}</span>
      </div>
      <div className="space-y-2">
        {children}
      </div>
    </div>
  )
}

function EventDetail({ label, value }) {
  return (
    <div className="flex gap-3 text-sm">
      <span className="text-gh-fg-muted min-w-[120px]">{label}</span>
      <span className="flex-1">{value}</span>
    </div>
  )
}

function Metric({ label, value }) {
  return (
    <div className="inline-flex items-center gap-2 px-3 py-1 bg-gh-canvas rounded text-xs">
      <span className="text-gh-fg-muted">{label}</span>
      <span className="text-gh-accent-primary font-semibold">{value}</span>
    </div>
  )
}
