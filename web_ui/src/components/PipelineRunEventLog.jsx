import { useState, useEffect, useMemo } from 'react'
import { useSocket } from '../contexts/SocketContext'
import { 
  Activity, AlertCircle, CheckCircle, XCircle, GitBranch, MessageSquare, 
  PlayCircle, RotateCcw, AlertTriangle, Users, FileCode, Clock, ExternalLink
} from 'lucide-react'

// Base component for displaying common event metadata
const PipelineRunEventLogEvent = ({ event, children, icon: Icon, color = 'bg-gray-500' }) => {
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
        <div className={`${color} rounded-full p-2 text-white flex-shrink-0`}>
          {Icon ? <Icon className="w-4 h-4" /> : <Activity className="w-4 h-4" />}
        </div>
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
const AgentInitializedEvent = ({ event }) => {
  // agent_execution_id can be at top level or in data (for backwards compatibility)
  const agentExecutionId = event.agent_execution_id || event.data?.agent_execution_id
  const taskId = event.task_id || event.data?.task_id
  
  return (
    <PipelineRunEventLogEvent event={event} icon={PlayCircle} color="bg-blue-600">
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
}

const AgentCompletedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={CheckCircle} color="bg-green-600">
    <div>Agent completed successfully: <span className="font-mono text-gh-success">{event.agent}</span></div>
    {event?.duration && (
      <div className="text-xs mt-1">Duration: {Math.floor(event.duration / 60)}m {Math.floor(event.duration % 60)}s</div>
    )}
  </PipelineRunEventLogEvent>
)

const AgentFailedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={XCircle} color="bg-red-600">
    <div>Agent failed: <span className="font-mono text-gh-danger">{event.agent}</span></div>
    {event?.error && (
      <div className="text-xs mt-1 text-red-400">{event.error}</div>
    )}
  </PipelineRunEventLogEvent>
)

// Agent Routing Events
const AgentRoutingDecisionEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={GitBranch} color="bg-blue-500">
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
)

const AgentSelectedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={Users} color="bg-blue-500">
    <div className="space-y-1">
      <div>Agent selected: <span className="font-mono text-gh-accent-fg">{event?.data?.decision?.agent || event?.decision?.agent}</span></div>
      {(event?.data?.reason || event?.reason) && (
        <div className="text-xs italic">{event?.data?.reason || event.reason}</div>
      )}
    </div>
  </PipelineRunEventLogEvent>
)

// Feedback Events
const FeedbackDetectedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={MessageSquare} color="bg-orange-500">
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
)

const FeedbackListeningStartedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={MessageSquare} color="bg-orange-400">
    <div>Started listening for feedback</div>
    {(event?.data?.monitoring_agent || event?.monitoring_agent) && (
      <div className="text-xs">Monitoring agent: {event?.data?.monitoring_agent || event.monitoring_agent}</div>
    )}
  </PipelineRunEventLogEvent>
)

const FeedbackListeningStoppedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={MessageSquare} color="bg-orange-400">
    <div>Stopped listening for feedback</div>
    {(event?.data?.reason || event?.reason) && (
      <div className="text-xs italic">{event?.data?.reason || event.reason}</div>
    )}
  </PipelineRunEventLogEvent>
)

// Status Progression Events
const StatusProgressionStartedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={RotateCcw} color="bg-green-500">
    <div className="space-y-1">
      <div>Status progression: {event?.data?.inputs?.from_status || event?.inputs?.from_status} → {event?.data?.decision?.to_status || event?.decision?.to_status}</div>
      {(event?.data?.inputs?.trigger || event?.inputs?.trigger) && (
        <div className="text-xs">Trigger: {event?.data?.inputs?.trigger || event.inputs.trigger}</div>
      )}
    </div>
  </PipelineRunEventLogEvent>
)

const StatusProgressionCompletedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={CheckCircle} color="bg-green-600">
    <div>Status progression completed: {event?.data?.decision?.to_status || event?.decision?.to_status}</div>
  </PipelineRunEventLogEvent>
)

const StatusProgressionFailedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={XCircle} color="bg-red-600">
    <div>Status progression failed</div>
    {(event?.data?.error || event?.error) && (
      <div className="text-xs mt-1 text-red-400">{event?.data?.error || event.error}</div>
    )}
  </PipelineRunEventLogEvent>
)

// Review Cycle Events
const ReviewCycleStartedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={RotateCcw} color="bg-purple-600">
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
)

const ReviewCycleIterationEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={RotateCcw} color="bg-purple-500">
    <div>Review cycle iteration {event?.data?.inputs?.cycle_iteration || event?.inputs?.cycle_iteration || '?'}</div>
    {(event?.data?.reason || event?.reason) && (
      <div className="text-xs italic">{event?.data?.reason || event.reason}</div>
    )}
  </PipelineRunEventLogEvent>
)

const ReviewCycleMakerSelectedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={Users} color="bg-purple-500">
    <div>Maker selected: <span className="font-mono">{event?.data?.inputs?.maker_agent || event?.inputs?.maker_agent}</span></div>
  </PipelineRunEventLogEvent>
)

const ReviewCycleReviewerSelectedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={Users} color="bg-purple-500">
    <div>Reviewer selected: <span className="font-mono">{event?.data?.inputs?.reviewer_agent || event?.inputs?.reviewer_agent}</span></div>
  </PipelineRunEventLogEvent>
)

const ReviewCycleEscalatedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={AlertTriangle} color="bg-orange-600">
    <div>Review cycle escalated to human</div>
    {(event?.data?.reason || event?.reason) && (
      <div className="text-xs italic">{event?.data?.reason || event.reason}</div>
    )}
  </PipelineRunEventLogEvent>
)

const ReviewCycleCompletedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={CheckCircle} color="bg-purple-700">
    <div>Review cycle completed</div>
    {(event?.data?.inputs?.cycle_iteration || event?.inputs?.cycle_iteration) && (
      <div className="text-xs">Total iterations: {event?.data?.inputs?.cycle_iteration || event.inputs.cycle_iteration}</div>
    )}
  </PipelineRunEventLogEvent>
)

// Error Handling Events
const ErrorEncounteredEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={AlertCircle} color="bg-red-600">
    <div className="space-y-1">
      <div>Error encountered: {event?.data?.error_type || event?.error_type}</div>
      {(event?.data?.error_message || event?.error_message) && (
        <div className="text-xs text-red-400">{event?.data?.error_message || event.error_message}</div>
      )}
      {(event?.data?.decision?.recovery_action || event?.decision?.recovery_action) && (
        <div className="text-xs mt-1">Recovery: {event?.data?.decision?.recovery_action || event.decision.recovery_action}</div>
      )}
    </div>
  </PipelineRunEventLogEvent>
)

const ErrorRecoveredEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={CheckCircle} color="bg-green-600">
    <div>Error recovered: {event?.data?.error_type || event?.error_type}</div>
    {(event?.data?.decision?.recovery_action || event?.decision?.recovery_action) && (
      <div className="text-xs">{event?.data?.decision?.recovery_action || event.decision.recovery_action}</div>
    )}
  </PipelineRunEventLogEvent>
)

// Task Queue Events
const TaskQueuedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={Clock} color="bg-cyan-600">
    <div className="space-y-1">
      <div>Task queued for agent: <span className="font-mono">{event?.agent}</span></div>
      {(event?.data?.priority || event?.priority) && (
        <div className="text-xs">Priority: {event?.data?.priority || event.priority}</div>
      )}
    </div>
  </PipelineRunEventLogEvent>
)

const TaskDequeuedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={PlayCircle} color="bg-cyan-500">
    <div>Task dequeued for execution: <span className="font-mono">{event?.agent}</span></div>
  </PipelineRunEventLogEvent>
)

// Branch Management Events
const BranchCreatedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={GitBranch} color="bg-lime-600">
    <div className="space-y-1">
      <div>Branch created: <span className="font-mono">{event?.data?.decision?.branch_name || event?.decision?.branch_name}</span></div>
      {(event?.data?.reason || event?.reason) && (
        <div className="text-xs italic">{event?.data?.reason || event.reason}</div>
      )}
    </div>
  </PipelineRunEventLogEvent>
)

const BranchReusedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={GitBranch} color="bg-lime-500">
    <div className="space-y-1">
      <div>Branch reused: <span className="font-mono">{event?.data?.decision?.branch_name || event?.decision?.branch_name}</span></div>
      {(event?.data?.inputs?.confidence || event?.inputs?.confidence) && (
        <div className="text-xs">Confidence: {((event?.data?.inputs?.confidence || event.inputs.confidence) * 100).toFixed(0)}%</div>
      )}
    </div>
  </PipelineRunEventLogEvent>
)

// Conversational Loop Events
const ConversationalLoopStartedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={MessageSquare} color="bg-pink-600">
    <div>Conversational loop started with: <span className="font-mono">{event?.data?.agent || event?.agent}</span></div>
  </PipelineRunEventLogEvent>
)

const ConversationalQuestionRoutedEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={MessageSquare} color="bg-pink-500">
    <div className="space-y-1">
      <div>Question routed to: <span className="font-mono">{event?.data?.target_agent || event?.target_agent}</span></div>
      {(event?.data?.question || event?.question) && (
        <div className="text-xs italic max-w-2xl truncate">{event?.data?.question || event.question}</div>
      )}
    </div>
  </PipelineRunEventLogEvent>
)

// Default/Generic Event
const GenericEvent = ({ event }) => (
  <PipelineRunEventLogEvent event={event} icon={Activity} color="bg-gray-500">
    <div className="space-y-1">
      {(event?.data?.reason || event?.reason) && <div className="text-xs italic">{event?.data?.reason || event.reason}</div>}
      {(event?.data?.decision || event?.decision) && (
        <div className="text-xs font-mono">
          Decision: {JSON.stringify(event?.data?.decision || event.decision)}
        </div>
      )}
    </div>
  </PipelineRunEventLogEvent>
)

// Event renderer mapping
const getEventComponent = (event) => {
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
  return <Component key={event.event_id} event={event} />
}

// Main PipelineRunEventLog component
export default function PipelineRunEventLog({ pipelineRun, events: initialEvents = [], isActive = false }) {
  const [events, setEvents] = useState(initialEvents)
  const { events: socketEvents } = useSocket()
  
  // Update events when new socket events arrive for this pipeline run
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
            {sortedEvents.map(event => getEventComponent(event))}
          </div>
        )}
      </div>
    </div>
  )
}
