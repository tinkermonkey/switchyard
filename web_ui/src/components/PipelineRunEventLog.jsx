import { useState, useEffect, useMemo, memo } from 'react'
import { useSocket } from '../contexts/SocketContext'
import { 
  Activity, AlertCircle, CheckCircle, XCircle, GitBranch, MessageSquare, 
  PlayCircle, RotateCcw, AlertTriangle, Users, FileCode, Clock, ExternalLink
} from 'lucide-react'

// Base component for displaying common event metadata
const PipelineRunEventLogEvent = ({ event, children, icon: Icon, color = 'bg-gray-500', onIconClick }) => {
  const formatTimestamp = (timestamp) => {
    if (!timestamp) return ''
    const date = new Date(timestamp)
    return date.toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false
    })
  }

  return (
    <div className="flex gap-3 p-3 border-b border-gh-border hover:bg-gh-canvas-inset transition-colors">
      {/* Icon and Timeline Line */}
      <div className="flex flex-col items-center">
        <button
          onClick={() => onIconClick && onIconClick(event)}
          className={`${color} rounded-full p-2 text-white flex-shrink-0 hover:opacity-80 transition-opacity cursor-pointer`}
          title="View raw event JSON"
        >
          {Icon ? <Icon className="w-4 h-4" /> : <Activity className="w-4 h-4" />}
        </button>
        <div className="flex-1 w-0.5 bg-gh-border mt-2" />
      </div>

      {/* Event Content */}
      <div className="flex-1 min-w-0">
        <div className="flex items-start justify-between gap-2 mb-1">
          <div className="flex-1">
            <div className="font-semibold text-sm text-gh-fg">
              {event.event_type?.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())}
            </div>
            <div className="text-xs text-gh-fg-muted">
              {formatTimestamp(event.timestamp)} • Agent: {event.agent || 'orchestrator'}
            </div>
          </div>
          {event?.issue_number && (
            <div className="text-xs text-gh-fg-muted">
              #{event.issue_number}
            </div>
          )}
        </div>
        
        {/* Event-specific content */}
        <div className="text-sm text-gh-fg-muted">
          {children}
        </div>
      </div>
    </div>
  )
}

// Agent Lifecycle Events
const AgentInitializedEvent = memo(({ event, onIconClick }) => {
  // agent_execution_id can be at top level or in data (for backwards compatibility)
  const agentExecutionId = event.agent_execution_id || event.data?.agent_execution_id
  const taskId = event.task_id || event.data?.task_id

  return (
    <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={PlayCircle} color="bg-blue-600">
      <div>Agent initialized: <span className="font-mono text-gh-accent-fg">{event.agent}</span></div>
      {taskId && (
        <div className="text-xs mt-1">Task ID: <span className="font-mono">{taskId.substring(0, 16)}...</span></div>
      )}
      {agentExecutionId && (
        <div className="text-xs mt-1 flex items-center gap-2">
          <span>Execution ID: <span className="font-mono">{agentExecutionId.substring(0, 16)}...</span></span>
          <a 
            href={`/agent-execution/${agentExecutionId}`}
            rel="noopener noreferrer"
            className="text-gh-accent-fg hover:underline inline-flex items-center gap-1"
          >
            View Details <ExternalLink className="w-3 h-3" />
          </a>
        </div>
      )}
    </PipelineRunEventLogEvent>
  )
})

const AgentCompletedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={CheckCircle} color="bg-green-600">
    <div>Agent completed successfully: <span className="font-mono text-gh-success">{event.agent}</span></div>
    {event?.duration && (
      <div className="text-xs mt-1">Duration: {Math.floor(event.duration / 60)}m {Math.floor(event.duration % 60)}s</div>
    )}
  </PipelineRunEventLogEvent>
))

const AgentFailedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={XCircle} color="bg-red-600">
    <div>Agent failed: <span className="font-mono text-gh-danger">{event.agent}</span></div>
    {event?.error && (
      <div className="text-xs mt-1 text-red-400">{event.error}</div>
    )}
  </PipelineRunEventLogEvent>
))

// Agent Routing Events
const AgentRoutingDecisionEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={GitBranch} color="bg-blue-500">
    <div className="space-y-1">
      <div>Selected agent: <span className="font-mono text-gh-accent-fg">{event?.data?.decision?.selected_agent || event?.decision?.selected_agent}</span></div>
      {(event?.data?.reason || event?.reason) && (
        <div className="text-xs italic">{event?.data?.reason || event.reason}</div>
      )}
      {(event?.data?.inputs?.current_status || event?.inputs?.current_status) && (
        <div className="text-xs">From status: {event?.data?.inputs?.current_status || event.inputs.current_status}</div>
      )}
    </div>
  </PipelineRunEventLogEvent>
))

const AgentSelectedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={Users} color="bg-blue-500">
    <div className="space-y-1">
      <div>Agent selected: <span className="font-mono text-gh-accent-fg">{event?.data?.decision?.agent || event?.decision?.agent}</span></div>
      {(event?.data?.reason || event?.reason) && (
        <div className="text-xs italic">{event?.data?.reason || event.reason}</div>
      )}
    </div>
  </PipelineRunEventLogEvent>
))

// Feedback Events
const FeedbackDetectedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={MessageSquare} color="bg-orange-500">
    <div className="space-y-1">
      <div>Feedback detected from: {event?.data?.inputs?.feedback_source || event?.inputs?.feedback_source}</div>
      {(event?.data?.decision?.target_agent || event?.decision?.target_agent) && (
        <div className="text-xs">Routed to: <span className="font-mono">{event?.data?.decision?.target_agent || event.decision.target_agent}</span></div>
      )}
      {(event?.data?.inputs?.feedback_content || event?.inputs?.feedback_content) && (
        <div className="text-xs mt-1 italic max-w-2xl truncate">{event?.data?.inputs?.feedback_content || event.inputs.feedback_content}</div>
      )}
    </div>
  </PipelineRunEventLogEvent>
))

const FeedbackListeningStartedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={MessageSquare} color="bg-orange-400">
    <div>Started listening for feedback</div>
    {(event?.data?.monitoring_agent || event?.monitoring_agent) && (
      <div className="text-xs">Monitoring agent: {event?.data?.monitoring_agent || event.monitoring_agent}</div>
    )}
  </PipelineRunEventLogEvent>
))

const FeedbackListeningStoppedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={MessageSquare} color="bg-orange-400">
    <div>Stopped listening for feedback</div>
    {(event?.data?.reason || event?.reason) && (
      <div className="text-xs italic">{event?.data?.reason || event.reason}</div>
    )}
  </PipelineRunEventLogEvent>
))

// Status Progression Events
const StatusProgressionStartedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={RotateCcw} color="bg-green-500">
    <div className="space-y-1">
      <div>Status progression: {event?.data?.inputs?.from_status || event?.inputs?.from_status} → {event?.data?.decision?.to_status || event?.decision?.to_status}</div>
      {(event?.data?.inputs?.trigger || event?.inputs?.trigger) && (
        <div className="text-xs">Trigger: {event?.data?.inputs?.trigger || event.inputs.trigger}</div>
      )}
    </div>
  </PipelineRunEventLogEvent>
))

const StatusProgressionCompletedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={CheckCircle} color="bg-green-600">
    <div>Status progression completed: {event?.data?.decision?.to_status || event?.decision?.to_status}</div>
  </PipelineRunEventLogEvent>
))

const StatusProgressionFailedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={XCircle} color="bg-red-600">
    <div>Status progression failed</div>
    {(event?.data?.error || event?.error) && (
      <div className="text-xs mt-1 text-red-400">{event?.data?.error || event.error}</div>
    )}
  </PipelineRunEventLogEvent>
))

// Review Cycle Events
const ReviewCycleStartedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={RotateCcw} color="bg-purple-600">
    <div className="space-y-1">
      <div>Review cycle started</div>
      {(event?.data?.inputs?.maker_agent || event?.inputs?.maker_agent) && (
        <div className="text-xs">Maker: {event?.data?.inputs?.maker_agent || event.inputs.maker_agent}</div>
      )}
      {(event?.data?.inputs?.reviewer_agent || event?.inputs?.reviewer_agent) && (
        <div className="text-xs">Reviewer: {event?.data?.inputs?.reviewer_agent || event.inputs.reviewer_agent}</div>
      )}
    </div>
  </PipelineRunEventLogEvent>
))

const ReviewCycleIterationEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={RotateCcw} color="bg-purple-500">
    <div>Review cycle iteration {event?.data?.inputs?.cycle_iteration || event?.inputs?.cycle_iteration || '?'}</div>
    {(event?.data?.reason || event?.reason) && (
      <div className="text-xs italic">{event?.data?.reason || event.reason}</div>
    )}
  </PipelineRunEventLogEvent>
))

const ReviewCycleMakerSelectedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={Users} color="bg-purple-500">
    <div>Maker selected: <span className="font-mono">{event?.data?.inputs?.maker_agent || event?.inputs?.maker_agent}</span></div>
  </PipelineRunEventLogEvent>
))

const ReviewCycleReviewerSelectedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={Users} color="bg-purple-500">
    <div>Reviewer selected: <span className="font-mono">{event?.data?.inputs?.reviewer_agent || event?.inputs?.reviewer_agent}</span></div>
  </PipelineRunEventLogEvent>
))

const ReviewCycleEscalatedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={AlertTriangle} color="bg-orange-600">
    <div>Review cycle escalated to human</div>
    {(event?.data?.reason || event?.reason) && (
      <div className="text-xs italic">{event?.data?.reason || event.reason}</div>
    )}
  </PipelineRunEventLogEvent>
))

const ReviewCycleCompletedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={CheckCircle} color="bg-purple-700">
    <div>Review cycle completed</div>
    {(event?.data?.inputs?.cycle_iteration || event?.inputs?.cycle_iteration) && (
      <div className="text-xs">Total iterations: {event?.data?.inputs?.cycle_iteration || event.inputs.cycle_iteration}</div>
    )}
  </PipelineRunEventLogEvent>
))

// Error Handling Events
const ErrorEncounteredEvent = memo(({ event, onIconClick }) => {
  const errorType = event?.data?.error_type || event?.error_type
  const errorMessage = event?.data?.error_message || event?.error_message
  const recoveryAction = event?.data?.decision?.recovery_action || event?.decision?.recovery_action

  // Transform technical error types to user-friendly titles
  const getErrorTitle = (type) => {
    if (type === 'TaskValidationError') {
      return '⚠️ Task Blocked - Prerequisites Missing'
    }
    return `Error: ${type}`
  }

  // Transform recovery actions to user-friendly messages
  const getRecoveryMessage = (action) => {
    if (action === 'queue_dev_environment_setup') {
      return 'Attempting to setup dev environment automatically...'
    }
    if (action === 'block_task') {
      return 'Task blocked until requirements are met'
    }
    return action
  }

  return (
    <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={AlertCircle} color="bg-red-600">
      <div className="space-y-1">
        <div className="font-semibold">{getErrorTitle(errorType)}</div>
        {errorMessage && (
          <div className="text-xs bg-red-900/30 border border-red-700/50 rounded px-2 py-1">
            {errorMessage}
          </div>
        )}
        {recoveryAction && (
          <div className="text-xs mt-1 text-orange-300">
            → {getRecoveryMessage(recoveryAction)}
          </div>
        )}
      </div>
    </PipelineRunEventLogEvent>
  )
})

const ErrorRecoveredEvent = memo(({ event, onIconClick }) => {
  const errorType = event?.data?.error_type || event?.error_type
  const errorMessage = event?.data?.error_message || event?.error_message
  const recoveryAction = event?.data?.decision?.recovery_action || event?.decision?.recovery_action

  // Transform technical recovery actions to user-friendly messages
  const getRecoveryMessage = (action, error) => {
    if (action === 'queue_dev_environment_setup') {
      return '✓ Auto-queued dev environment setup task'
    }
    return action
  }

  return (
    <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={AlertTriangle} color="bg-orange-600">
      <div className="space-y-1">
        <div className="font-semibold">
          {errorType === 'TaskValidationError' ? 'Task Blocked - Recovery Initiated' : `Error Recovered: ${errorType}`}
        </div>
        {errorMessage && (
          <div className="text-xs bg-yellow-900/30 border border-yellow-700/50 rounded px-2 py-1">
            <span className="font-semibold">Issue:</span> {errorMessage}
          </div>
        )}
        {recoveryAction && (
          <div className="text-xs text-green-400">
            {getRecoveryMessage(recoveryAction, errorType)}
          </div>
        )}
      </div>
    </PipelineRunEventLogEvent>
  )
})

// Task Queue Events
const TaskQueuedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={Clock} color="bg-cyan-600">
    <div className="space-y-1">
      <div>Task queued for agent: <span className="font-mono">{event?.agent}</span></div>
      {(event?.data?.priority || event?.priority) && (
        <div className="text-xs">Priority: {event?.data?.priority || event.priority}</div>
      )}
    </div>
  </PipelineRunEventLogEvent>
))

const TaskDequeuedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={PlayCircle} color="bg-cyan-500">
    <div>Task dequeued for execution: <span className="font-mono">{event?.agent}</span></div>
  </PipelineRunEventLogEvent>
))

// Branch Management Events
const BranchCreatedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={GitBranch} color="bg-lime-600">
    <div className="space-y-1">
      <div>Branch created: <span className="font-mono">{event?.data?.decision?.branch_name || event?.decision?.branch_name}</span></div>
      {(event?.data?.reason || event?.reason) && (
        <div className="text-xs italic">{event?.data?.reason || event.reason}</div>
      )}
    </div>
  </PipelineRunEventLogEvent>
))

const BranchReusedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={GitBranch} color="bg-lime-500">
    <div className="space-y-1">
      <div>Branch reused: <span className="font-mono">{event?.data?.decision?.branch_name || event?.decision?.branch_name}</span></div>
      {(event?.data?.inputs?.confidence || event?.inputs?.confidence) && (
        <div className="text-xs">Confidence: {((event?.data?.inputs?.confidence || event.inputs.confidence) * 100).toFixed(0)}%</div>
      )}
    </div>
  </PipelineRunEventLogEvent>
))

// Conversational Loop Events
const ConversationalLoopStartedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={MessageSquare} color="bg-pink-600">
    <div>Conversational loop started with: <span className="font-mono">{event?.data?.agent || event?.agent}</span></div>
  </PipelineRunEventLogEvent>
))

const ConversationalQuestionRoutedEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={MessageSquare} color="bg-pink-500">
    <div className="space-y-1">
      <div>Question routed to: <span className="font-mono">{event?.data?.target_agent || event?.target_agent}</span></div>
      {(event?.data?.question || event?.question) && (
        <div className="text-xs italic max-w-2xl truncate">{event?.data?.question || event.question}</div>
      )}
    </div>
  </PipelineRunEventLogEvent>
))

// Default/Generic Event
const GenericEvent = memo(({ event, onIconClick }) => (
  <PipelineRunEventLogEvent event={event} onIconClick={onIconClick} icon={Activity} color="bg-gray-500">
    <div className="space-y-1">
      {(event?.data?.reason || event?.reason) && <div className="text-xs italic">{event?.data?.reason || event.reason}</div>}
      {(event?.data?.decision || event?.decision) && (
        <div className="text-xs font-mono">
          Decision: {JSON.stringify(event?.data?.decision || event.decision)}
        </div>
      )}
    </div>
  </PipelineRunEventLogEvent>
))

// Event renderer mapping
const getEventComponent = (event, onIconClick) => {
  const eventTypeMap = {
    // Lifecycle
    'agent_initialized': AgentInitializedEvent,
    'agent_completed': AgentCompletedEvent,
    'agent_failed': AgentFailedEvent,

    // Routing
    'agent_routing_decision': AgentRoutingDecisionEvent,
    'agent_selected': AgentSelectedEvent,

    // Feedback
    'feedback_detected': FeedbackDetectedEvent,
    'feedback_listening_started': FeedbackListeningStartedEvent,
    'feedback_listening_stopped': FeedbackListeningStoppedEvent,

    // Status Progression
    'status_progression_started': StatusProgressionStartedEvent,
    'status_progression_completed': StatusProgressionCompletedEvent,
    'status_progression_failed': StatusProgressionFailedEvent,

    // Review Cycles
    'review_cycle_started': ReviewCycleStartedEvent,
    'review_cycle_iteration': ReviewCycleIterationEvent,
    'review_cycle_maker_selected': ReviewCycleMakerSelectedEvent,
    'review_cycle_reviewer_selected': ReviewCycleReviewerSelectedEvent,
    'review_cycle_escalated': ReviewCycleEscalatedEvent,
    'review_cycle_completed': ReviewCycleCompletedEvent,

    // Error Handling
    'error_encountered': ErrorEncounteredEvent,
    'error_recovered': ErrorRecoveredEvent,

    // Task Queue
    'task_queued': TaskQueuedEvent,
    'task_dequeued': TaskDequeuedEvent,

    // Branch Management
    'branch_created': BranchCreatedEvent,
    'branch_reused': BranchReusedEvent,

    // Conversational
    'conversational_loop_started': ConversationalLoopStartedEvent,
    'conversational_question_routed': ConversationalQuestionRoutedEvent,
  }

  const Component = eventTypeMap[event.event_type] || GenericEvent
  return <Component key={event.event_id} event={event} onIconClick={onIconClick} />
}

// Event JSON Modal Component
const EventJsonModal = ({ event, onClose }) => {
  if (!event) return null

  // Close modal on Escape key
  useEffect(() => {
    const handleEscape = (e) => {
      if (e.key === 'Escape') {
        onClose()
      }
    }
    window.addEventListener('keydown', handleEscape)
    return () => window.removeEventListener('keydown', handleEscape)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4"
      onClick={onClose}
    >
      <div
        className="bg-gh-canvas border border-gh-border rounded-lg max-w-4xl w-full max-h-[90vh] overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Modal Header */}
        <div className="p-4 border-b border-gh-border flex items-center justify-between">
          <h3 className="text-lg font-semibold text-gh-fg">Event JSON</h3>
          <button
            onClick={onClose}
            className="text-gh-fg-muted hover:text-gh-fg transition-colors"
          >
            <XCircle className="w-5 h-5" />
          </button>
        </div>

        {/* Modal Body */}
        <div className="flex-1 overflow-auto p-4">
          <pre className="text-xs font-mono bg-gh-canvas-subtle p-4 rounded border border-gh-border overflow-x-auto">
            {JSON.stringify(event, null, 2)}
          </pre>
        </div>

        {/* Modal Footer */}
        <div className="p-4 border-t border-gh-border flex justify-end gap-2">
          <button
            onClick={() => {
              navigator.clipboard.writeText(JSON.stringify(event, null, 2))
            }}
            className="px-4 py-2 bg-gh-accent-emphasis text-white rounded hover:bg-opacity-90 transition-colors"
          >
            Copy to Clipboard
          </button>
          <button
            onClick={onClose}
            className="px-4 py-2 bg-gh-canvas-subtle border border-gh-border rounded hover:bg-gh-border-muted transition-colors"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  )
}

// Main PipelineRunEventLog component
function PipelineRunEventLog({ pipelineRun, events: initialEvents = [], isActive = false }) {
  const [events, setEvents] = useState(initialEvents)
  const [selectedEventForModal, setSelectedEventForModal] = useState(null)
  const { events: socketEvents } = useSocket()

  const handleIconClick = (event) => {
    setSelectedEventForModal(event)
  }

  const closeModal = () => {
    setSelectedEventForModal(null)
  }

  // Update events when new socket events arrive for this pipeline run
  // Note: This is NOT redundant with parent's mergedEvents - parent provides initial state,
  // but this component independently subscribes to live updates for active runs
  useEffect(() => {
    if (!isActive || !pipelineRun) return

    // Filter socket events for this pipeline run
    const newEvents = socketEvents.filter(e =>
      e.pipeline_run_id === pipelineRun.id &&
      !events.find(existing => existing.event_id === e.event_id)
    )

    if (newEvents.length > 0) {
      setEvents(prev => [...prev, ...newEvents].sort((a, b) =>
        new Date(a.timestamp) - new Date(b.timestamp)
      ))
    }
  }, [socketEvents, isActive, pipelineRun, events])
  
  // Update events when initialEvents prop changes
  useEffect(() => {
    setEvents(initialEvents)
  }, [initialEvents])
  
  // Sort events oldest to newest
  const sortedEvents = useMemo(() => {
    return [...events].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))
  }, [events])
  
  if (!pipelineRun) {
    return (
      <div className="flex items-center justify-center h-96 text-gh-fg-muted">
        Select a pipeline run to view events
      </div>
    )
  }
  
  return (
    <div className="bg-gh-canvas-subtle rounded-md border border-gh-border">
      <div className="p-4 border-b border-gh-border">
        <h2 className="text-xl font-semibold">{pipelineRun.issue_title}</h2>
        <p className="text-sm text-gh-fg-muted mt-1">
          {pipelineRun.project} • Issue #{pipelineRun.issue_number} • Board: {pipelineRun.board} • Status: {pipelineRun.status}  • ID {pipelineRun.id}
        </p>
        <p className="text-sm text-gh-fg-muted">
          Started: {new Date(pipelineRun.started_at).toLocaleString()}
          {pipelineRun.ended_at && ` • Ended: ${new Date(pipelineRun.ended_at).toLocaleString()}`}
        </p>
        <div className="mt-2 flex items-center gap-2">
          <div className={`w-2 h-2 rounded-full ${isActive ? 'bg-green-500 animate-pulse' : 'bg-gray-500'}`} />
          <span className="text-xs text-gh-fg-muted">
            {isActive ? 'Active - Live Updating' : 'Completed'} • {sortedEvents.length} events
          </span>
        </div>
      </div>
      
      <div className="">
        {sortedEvents.length === 0 ? (
          <div className="flex items-center justify-center h-96 text-gh-fg-muted">
            No events found for this pipeline run
          </div>
        ) : (
          <div className="relative">
            {sortedEvents.map(event => getEventComponent(event, handleIconClick))}
          </div>
        )}
      </div>

      {/* Event JSON Modal */}
      {selectedEventForModal && (
        <EventJsonModal event={selectedEventForModal} onClose={closeModal} />
      )}
    </div>
  )
}

// Memoized wrapper to prevent unnecessary re-renders
const PipelineRunEventLogMemoized = memo(PipelineRunEventLog, (prevProps, nextProps) => {
  // Only re-render if pipeline run ID, events array identity, or isActive changed
  // Use array identity (===) not length, since parent computes events via useMemo
  return (
    prevProps.pipelineRun?.id === nextProps.pipelineRun?.id &&
    prevProps.events === nextProps.events &&
    prevProps.isActive === nextProps.isActive
  )
})

export default PipelineRunEventLogMemoized
