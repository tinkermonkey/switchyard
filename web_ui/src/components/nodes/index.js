/**
 * Event node type registry — maps every node type key to its React component.
 *
 * Spread this into the ReactFlow `nodeTypes` prop alongside the container node types.
 * The 'pipelineEvent' key is the fallback for any event_type not present in EVENT_TYPE_MAP.
 *
 * Key naming convention: camelCase, matching the values in EVENT_TYPE_MAP.js.
 */

import PipelineEventNode from './PipelineEventNode'

// ── Pipeline lifecycle ────────────────────────────────────────────────────────
import PipelineStartedNode   from './pipeline/PipelineStartedNode'
import PipelineCompletedNode from './pipeline/PipelineCompletedNode'

// ── Agent execution ───────────────────────────────────────────────────────────
import AgentExecutionNode  from './agent/AgentExecutionNode'
import AgentCompletedNode  from './agent/AgentCompletedNode'
import AgentFailedNode     from './agent/AgentFailedNode'

// ── Review cycle ──────────────────────────────────────────────────────────────
import ReviewCycleStartedNode            from './cycles/review/ReviewCycleStartedNode'
import ReviewCycleIterationNode          from './cycles/review/ReviewCycleIterationNode'
import ReviewCycleMakerSelectedNode      from './cycles/review/ReviewCycleMakerSelectedNode'
import ReviewCycleReviewerSelectedNode   from './cycles/review/ReviewCycleReviewerSelectedNode'
import ReviewCycleEscalatedNode          from './cycles/review/ReviewCycleEscalatedNode'
import ReviewCycleCompletedNode          from './cycles/review/ReviewCycleCompletedNode'

// ── Repair cycle — top-level ──────────────────────────────────────────────────
import RepairCycleStartedNode             from './cycles/repair/RepairCycleStartedNode'
import RepairCycleIterationNode           from './cycles/repair/RepairCycleIterationNode'
import RepairCycleCompletedNode           from './cycles/repair/RepairCycleCompletedNode'
import RepairCycleFailedNode              from './cycles/repair/RepairCycleFailedNode'
import RepairCycleEnvRebuildStartedNode   from './cycles/repair/RepairCycleEnvRebuildStartedNode'
import RepairCycleEnvRebuildCompletedNode from './cycles/repair/RepairCycleEnvRebuildCompletedNode'

// ── Repair cycle — test sub-family ────────────────────────────────────────────
import RepairCycleTestCycleStartedNode        from './cycles/repair/test/RepairCycleTestCycleStartedNode'
import RepairCycleTestCycleCompletedNode      from './cycles/repair/test/RepairCycleTestCycleCompletedNode'
import RepairCycleTestExecutionStartedNode    from './cycles/repair/test/RepairCycleTestExecutionStartedNode'
import RepairCycleTestExecutionCompletedNode  from './cycles/repair/test/RepairCycleTestExecutionCompletedNode'

// ── Repair cycle — fix sub-family ─────────────────────────────────────────────
import RepairCycleFixCycleStartedNode   from './cycles/repair/fix/RepairCycleFixCycleStartedNode'
import RepairCycleFixCycleCompletedNode from './cycles/repair/fix/RepairCycleFixCycleCompletedNode'
import RepairCycleFileFixStartedNode    from './cycles/repair/fix/RepairCycleFileFixStartedNode'
import RepairCycleFileFixCompletedNode  from './cycles/repair/fix/RepairCycleFileFixCompletedNode'
import RepairCycleFileFixFailedNode     from './cycles/repair/fix/RepairCycleFileFixFailedNode'

// ── Repair cycle — warning review sub-family ──────────────────────────────────
import RepairCycleWarningReviewStartedNode   from './cycles/repair/warning/RepairCycleWarningReviewStartedNode'
import RepairCycleWarningReviewCompletedNode from './cycles/repair/warning/RepairCycleWarningReviewCompletedNode'
import RepairCycleWarningReviewFailedNode    from './cycles/repair/warning/RepairCycleWarningReviewFailedNode'

// ── Repair cycle — systemic sub-family ───────────────────────────────────────
import RepairCycleSystemicAnalysisStartedNode   from './cycles/repair/systemic/RepairCycleSystemicAnalysisStartedNode'
import RepairCycleSystemicAnalysisCompletedNode from './cycles/repair/systemic/RepairCycleSystemicAnalysisCompletedNode'
import RepairCycleSystemicFixStartedNode        from './cycles/repair/systemic/RepairCycleSystemicFixStartedNode'
import RepairCycleSystemicFixCompletedNode      from './cycles/repair/systemic/RepairCycleSystemicFixCompletedNode'

// ── Repair cycle — container sub-family ──────────────────────────────────────
import RepairCycleContainerStartedNode            from './cycles/repair/container/RepairCycleContainerStartedNode'
import RepairCycleContainerCheckpointUpdatedNode  from './cycles/repair/container/RepairCycleContainerCheckpointUpdatedNode'
import RepairCycleContainerRecoveredNode          from './cycles/repair/container/RepairCycleContainerRecoveredNode'
import RepairCycleContainerKilledNode             from './cycles/repair/container/RepairCycleContainerKilledNode'
import RepairCycleContainerCompletedNode          from './cycles/repair/container/RepairCycleContainerCompletedNode'

// ── PR review ─────────────────────────────────────────────────────────────────
import PRReviewStageStartedNode   from './pr/PRReviewStageStartedNode'
import PRReviewPhaseStartedNode   from './pr/PRReviewPhaseStartedNode'
import PRReviewPhaseCompletedNode from './pr/PRReviewPhaseCompletedNode'
import PRReviewPhaseFailedNode    from './pr/PRReviewPhaseFailedNode'
import PRReviewStageCompletedNode from './pr/PRReviewStageCompletedNode'

// ── Routing ───────────────────────────────────────────────────────────────────
import AgentRoutingDecisionNode from './routing/AgentRoutingDecisionNode'
import AgentSelectedNode        from './routing/AgentSelectedNode'
import WorkspaceRoutingNode     from './routing/WorkspaceRoutingNode'

// ── Progression ───────────────────────────────────────────────────────────────
import StatusProgressionStartedNode   from './progression/StatusProgressionStartedNode'
import StatusProgressionCompletedNode from './progression/StatusProgressionCompletedNode'
import StatusProgressionFailedNode    from './progression/StatusProgressionFailedNode'
import PipelineStageTransitionNode    from './progression/PipelineStageTransitionNode'
import PipelineRunStartedNode         from './progression/PipelineRunStartedNode'
import PipelineRunCompletedNode       from './progression/PipelineRunCompletedNode'
import PipelineRunFailedNode          from './progression/PipelineRunFailedNode'

// ── Feedback ──────────────────────────────────────────────────────────────────
import FeedbackDetectedNode          from './feedback/FeedbackDetectedNode'
import FeedbackListeningStartedNode  from './feedback/FeedbackListeningStartedNode'
import FeedbackListeningStoppedNode  from './feedback/FeedbackListeningStoppedNode'
import FeedbackIgnoredNode           from './feedback/FeedbackIgnoredNode'

// ── Conversational loop ───────────────────────────────────────────────────────
import ConversationalLoopStartedNode    from './conversational/ConversationalLoopStartedNode'
import ConversationalQuestionRoutedNode from './conversational/ConversationalQuestionRoutedNode'
import ConversationalLoopPausedNode     from './conversational/ConversationalLoopPausedNode'
import ConversationalLoopResumedNode    from './conversational/ConversationalLoopResumedNode'

// ── Error handling ────────────────────────────────────────────────────────────
import ErrorEncounteredNode     from './error/ErrorEncounteredNode'
import ErrorRecoveredNode       from './error/ErrorRecoveredNode'
import CircuitBreakerOpenedNode from './error/CircuitBreakerOpenedNode'
import CircuitBreakerClosedNode from './error/CircuitBreakerClosedNode'
import RetryAttemptedNode       from './error/RetryAttemptedNode'

// ── Task management ───────────────────────────────────────────────────────────
import TaskQueuedNode          from './task/TaskQueuedNode'
import TaskDequeuedNode        from './task/TaskDequeuedNode'
import TaskPriorityChangedNode from './task/TaskPriorityChangedNode'
import TaskCancelledNode       from './task/TaskCancelledNode'

// ── Branch management ─────────────────────────────────────────────────────────
import BranchSelectedNode            from './branch/BranchSelectedNode'
import BranchCreatedNode             from './branch/BranchCreatedNode'
import BranchReusedNode              from './branch/BranchReusedNode'
import BranchConflictDetectedNode    from './branch/BranchConflictDetectedNode'
import BranchStaleDetectedNode       from './branch/BranchStaleDetectedNode'
import BranchSelectionEscalatedNode  from './branch/BranchSelectionEscalatedNode'

// ── Issue management ──────────────────────────────────────────────────────────
import SubIssueCreatedNode         from './issue/SubIssueCreatedNode'
import SubIssueCreationFailedNode  from './issue/SubIssueCreationFailedNode'

// ── System operations ─────────────────────────────────────────────────────────
import ExecutionStateReconciledNode  from './system/ExecutionStateReconciledNode'
import StatusValidationFailureNode   from './system/StatusValidationFailureNode'
import ResultPersistenceFailedNode   from './system/ResultPersistenceFailedNode'
import FallbackStorageUsedNode       from './system/FallbackStorageUsedNode'
import OutputValidationFailedNode    from './system/OutputValidationFailedNode'
import EmptyOutputDetectedNode       from './system/EmptyOutputDetectedNode'
import ContainerResultRecoveredNode  from './system/ContainerResultRecoveredNode'

/**
 * All event node types keyed by their ReactFlow node type string.
 * Spread into the nodeTypes map in PipelineFlowGraph alongside container types.
 *
 * 'pipelineEvent' is the fallback for unknown event_type values.
 */
export const eventNodeTypes = {
  // Fallback
  pipelineEvent: PipelineEventNode,

  // Pipeline lifecycle
  pipelineStarted:   PipelineStartedNode,
  pipelineCompleted: PipelineCompletedNode,

  // Agent execution
  agentExecution: AgentExecutionNode,
  agentCompleted: AgentCompletedNode,
  agentFailed:    AgentFailedNode,

  // Review cycle
  reviewCycleStarted:           ReviewCycleStartedNode,
  reviewCycleIteration:         ReviewCycleIterationNode,
  reviewCycleMakerSelected:     ReviewCycleMakerSelectedNode,
  reviewCycleReviewerSelected:  ReviewCycleReviewerSelectedNode,
  reviewCycleEscalated:         ReviewCycleEscalatedNode,
  reviewCycleCompleted:         ReviewCycleCompletedNode,

  // Repair cycle — top-level
  repairCycleStarted:             RepairCycleStartedNode,
  repairCycleIteration:           RepairCycleIterationNode,
  repairCycleCompleted:           RepairCycleCompletedNode,
  repairCycleFailed:              RepairCycleFailedNode,
  repairCycleEnvRebuildStarted:   RepairCycleEnvRebuildStartedNode,
  repairCycleEnvRebuildCompleted: RepairCycleEnvRebuildCompletedNode,

  // Repair cycle — test sub-family
  repairCycleTestCycleStarted:       RepairCycleTestCycleStartedNode,
  repairCycleTestCycleCompleted:     RepairCycleTestCycleCompletedNode,
  repairCycleTestExecutionStarted:   RepairCycleTestExecutionStartedNode,
  repairCycleTestExecutionCompleted: RepairCycleTestExecutionCompletedNode,

  // Repair cycle — fix sub-family
  repairCycleFixCycleStarted:   RepairCycleFixCycleStartedNode,
  repairCycleFixCycleCompleted: RepairCycleFixCycleCompletedNode,
  repairCycleFileFixStarted:    RepairCycleFileFixStartedNode,
  repairCycleFileFixCompleted:  RepairCycleFileFixCompletedNode,
  repairCycleFileFixFailed:     RepairCycleFileFixFailedNode,

  // Repair cycle — warning review sub-family
  repairCycleWarningReviewStarted:   RepairCycleWarningReviewStartedNode,
  repairCycleWarningReviewCompleted: RepairCycleWarningReviewCompletedNode,
  repairCycleWarningReviewFailed:    RepairCycleWarningReviewFailedNode,

  // Repair cycle — systemic sub-family
  repairCycleSystemicAnalysisStarted:   RepairCycleSystemicAnalysisStartedNode,
  repairCycleSystemicAnalysisCompleted: RepairCycleSystemicAnalysisCompletedNode,
  repairCycleSystemicFixStarted:        RepairCycleSystemicFixStartedNode,
  repairCycleSystemicFixCompleted:      RepairCycleSystemicFixCompletedNode,

  // Repair cycle — container sub-family
  repairCycleContainerStarted:            RepairCycleContainerStartedNode,
  repairCycleContainerCheckpointUpdated:  RepairCycleContainerCheckpointUpdatedNode,
  repairCycleContainerRecovered:          RepairCycleContainerRecoveredNode,
  repairCycleContainerKilled:             RepairCycleContainerKilledNode,
  repairCycleContainerCompleted:          RepairCycleContainerCompletedNode,

  // PR review
  prReviewStageStarted:   PRReviewStageStartedNode,
  prReviewPhaseStarted:   PRReviewPhaseStartedNode,
  prReviewPhaseCompleted: PRReviewPhaseCompletedNode,
  prReviewPhaseFailed:    PRReviewPhaseFailedNode,
  prReviewStageCompleted: PRReviewStageCompletedNode,

  // Routing
  agentRoutingDecision:     AgentRoutingDecisionNode,
  agentSelected:            AgentSelectedNode,
  workspaceRoutingDecision: WorkspaceRoutingNode,

  // Progression
  statusProgressionStarted:   StatusProgressionStartedNode,
  statusProgressionCompleted: StatusProgressionCompletedNode,
  statusProgressionFailed:    StatusProgressionFailedNode,
  pipelineStageTransition:    PipelineStageTransitionNode,
  pipelineRunStarted:         PipelineRunStartedNode,
  pipelineRunCompleted:       PipelineRunCompletedNode,
  pipelineRunFailed:          PipelineRunFailedNode,

  // Feedback
  feedbackDetected:         FeedbackDetectedNode,
  feedbackListeningStarted: FeedbackListeningStartedNode,
  feedbackListeningStopped: FeedbackListeningStoppedNode,
  feedbackIgnored:          FeedbackIgnoredNode,

  // Conversational loop
  conversationalLoopStarted:    ConversationalLoopStartedNode,
  conversationalQuestionRouted: ConversationalQuestionRoutedNode,
  conversationalLoopPaused:     ConversationalLoopPausedNode,
  conversationalLoopResumed:    ConversationalLoopResumedNode,

  // Error handling
  errorEncountered:     ErrorEncounteredNode,
  errorRecovered:       ErrorRecoveredNode,
  circuitBreakerOpened: CircuitBreakerOpenedNode,
  circuitBreakerClosed: CircuitBreakerClosedNode,
  retryAttempted:       RetryAttemptedNode,

  // Task management
  taskQueued:          TaskQueuedNode,
  taskDequeued:        TaskDequeuedNode,
  taskPriorityChanged: TaskPriorityChangedNode,
  taskCancelled:       TaskCancelledNode,

  // Branch management
  branchSelected:           BranchSelectedNode,
  branchCreated:            BranchCreatedNode,
  branchReused:             BranchReusedNode,
  branchConflictDetected:   BranchConflictDetectedNode,
  branchStaleDetected:      BranchStaleDetectedNode,
  branchSelectionEscalated: BranchSelectionEscalatedNode,

  // Issue management
  subIssueCreated:        SubIssueCreatedNode,
  subIssueCreationFailed: SubIssueCreationFailedNode,

  // System operations
  executionStateReconciled: ExecutionStateReconciledNode,
  statusValidationFailure:  StatusValidationFailureNode,
  resultPersistenceFailed:  ResultPersistenceFailedNode,
  fallbackStorageUsed:      FallbackStorageUsedNode,
  outputValidationFailed:   OutputValidationFailedNode,
  emptyOutputDetected:      EmptyOutputDetectedNode,
  containerResultRecovered: ContainerResultRecoveredNode,
}
