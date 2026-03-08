/**
 * EVENT_TYPE_MAP — canonical mapping from backend event_type strings to ReactFlow node type keys.
 *
 * This is the single source of truth that bridges the Python EventType enum
 * (monitoring/observability.py) and the React component registry (nodes/index.js).
 *
 * Rules:
 *   - Keys are the exact `event_type` strings emitted by the backend.
 *   - Values are the ReactFlow node type keys registered in nodes/index.js.
 *   - Unknown event types fall back to 'pipelineEvent' (PipelineEventNode base).
 *   - When a new EventType is added to the backend, add it here and create its component.
 *
 * Component hierarchy (composition chain):
 *
 *   PipelineEventNode                          ← root: handles, active-stripe, label/metadata
 *   ├── pipeline/PipelineLifecycleNode
 *   │   ├── PipelineStartedNode                pipeline_created (static)
 *   │   └── PipelineCompletedNode              pipeline_completed (static)
 *   ├── agent/AgentExecutionNode               agent_initialized
 *   ├── cycles/CycleEventNode
 *   │   ├── cycles/review/ReviewCycleEventNode (purple family)
 *   │   │   ├── ReviewCycleStartedNode         review_cycle_started
 *   │   │   ├── ReviewCycleIterationNode        review_cycle_iteration
 *   │   │   ├── ReviewCycleMakerSelectedNode    review_cycle_maker_selected
 *   │   │   ├── ReviewCycleReviewerSelectedNode review_cycle_reviewer_selected
 *   │   │   ├── ReviewCycleEscalatedNode        review_cycle_escalated
 *   │   │   └── ReviewCycleCompletedNode        review_cycle_completed
 *   │   └── cycles/repair/RepairCycleEventNode  (orange family)
 *   │       ├── RepairCycleStartedNode          repair_cycle_started
 *   │       ├── RepairCycleIterationNode        repair_cycle_iteration
 *   │       ├── RepairCycleCompletedNode        repair_cycle_completed
 *   │       ├── RepairCycleFailedNode           repair_cycle_failed
 *   │       ├── RepairCycleEnvRebuildStartedNode   repair_cycle_env_rebuild_started
 *   │       ├── RepairCycleEnvRebuildCompletedNode repair_cycle_env_rebuild_completed
 *   │       ├── test/RepairCycleTestCycleNode   (amber test sub-family)
 *   │       │   ├── RepairCycleTestCycleStartedNode     repair_cycle_test_cycle_started
 *   │       │   ├── RepairCycleTestCycleCompletedNode   repair_cycle_test_cycle_completed
 *   │       │   ├── RepairCycleTestExecutionStartedNode repair_cycle_test_execution_started
 *   │       │   └── RepairCycleTestExecutionCompletedNode repair_cycle_test_execution_completed
 *   │       ├── fix/RepairCycleFixCycleNode     (red fix sub-family)
 *   │       │   ├── RepairCycleFixCycleStartedNode   repair_cycle_fix_cycle_started
 *   │       │   ├── RepairCycleFixCycleCompletedNode repair_cycle_fix_cycle_completed
 *   │       │   ├── RepairCycleFileFixStartedNode    repair_cycle_file_fix_started
 *   │       │   ├── RepairCycleFileFixCompletedNode  repair_cycle_file_fix_completed
 *   │       │   └── RepairCycleFileFixFailedNode     repair_cycle_file_fix_failed
 *   │       ├── warning/RepairCycleWarningReviewNode (amber warning sub-family)
 *   │       │   ├── RepairCycleWarningReviewStartedNode   repair_cycle_warning_review_started
 *   │       │   ├── RepairCycleWarningReviewCompletedNode repair_cycle_warning_review_completed
 *   │       │   └── RepairCycleWarningReviewFailedNode    repair_cycle_warning_review_failed
 *   │       ├── systemic/RepairCycleSystemicNode (violet systemic sub-family)
 *   │       │   ├── RepairCycleSystemicAnalysisStartedNode   repair_cycle_systemic_analysis_started
 *   │       │   ├── RepairCycleSystemicAnalysisCompletedNode repair_cycle_systemic_analysis_completed
 *   │       │   ├── RepairCycleSystemicFixStartedNode        repair_cycle_systemic_fix_started
 *   │       │   └── RepairCycleSystemicFixCompletedNode      repair_cycle_systemic_fix_completed
 *   │       └── container/RepairCycleContainerEventNode (slate container sub-family)
 *   │           ├── RepairCycleContainerStartedNode             repair_cycle_container_started
 *   │           ├── RepairCycleContainerCheckpointUpdatedNode   repair_cycle_container_checkpoint_updated
 *   │           ├── RepairCycleContainerRecoveredNode           repair_cycle_container_recovered
 *   │           ├── RepairCycleContainerKilledNode              repair_cycle_container_killed
 *   │           └── RepairCycleContainerCompletedNode           repair_cycle_container_completed
 *   ├── pr/PRReviewNode (indigo PR family)
 *   │   ├── PRReviewStageStartedNode   pr_review_stage_started
 *   │   ├── PRReviewPhaseStartedNode   pr_review_phase_started
 *   │   ├── PRReviewPhaseCompletedNode pr_review_phase_completed
 *   │   ├── PRReviewPhaseFailedNode    pr_review_phase_failed
 *   │   └── PRReviewStageCompletedNode pr_review_stage_completed
 *   ├── routing/RoutingDecisionNode (blue routing family)
 *   │   ├── AgentRoutingDecisionNode   agent_routing_decision
 *   │   ├── AgentSelectedNode          agent_selected
 *   │   └── WorkspaceRoutingNode       workspace_routing_decision
 *   ├── progression/ProgressionNode (green progression family)
 *   │   ├── StatusProgressionStartedNode    status_progression_started
 *   │   ├── StatusProgressionCompletedNode  status_progression_completed
 *   │   ├── StatusProgressionFailedNode     status_progression_failed
 *   │   ├── PipelineStageTransitionNode     pipeline_stage_transition
 *   │   ├── PipelineRunStartedNode          pipeline_run_started
 *   │   ├── PipelineRunCompletedNode        pipeline_run_completed
 *   │   └── PipelineRunFailedNode           pipeline_run_failed
 *   ├── feedback/FeedbackNode (amber feedback family)
 *   │   ├── FeedbackDetectedNode          feedback_detected
 *   │   ├── FeedbackListeningStartedNode  feedback_listening_started
 *   │   ├── FeedbackListeningStoppedNode  feedback_listening_stopped
 *   │   └── FeedbackIgnoredNode           feedback_ignored
 *   ├── conversational/ConversationalLoopNode (pink conversational family)
 *   │   ├── ConversationalLoopStartedNode    conversational_loop_started
 *   │   ├── ConversationalQuestionRoutedNode conversational_question_routed
 *   │   ├── ConversationalLoopPausedNode     conversational_loop_paused
 *   │   └── ConversationalLoopResumedNode    conversational_loop_resumed
 *   ├── error/ErrorEventNode (red error family)
 *   │   ├── ErrorEncounteredNode     error_encountered
 *   │   ├── ErrorRecoveredNode       error_recovered
 *   │   ├── CircuitBreakerOpenedNode circuit_breaker_opened
 *   │   ├── CircuitBreakerClosedNode circuit_breaker_closed
 *   │   └── RetryAttemptedNode       retry_attempted
 *   ├── task/TaskManagementNode (cyan task family)
 *   │   ├── TaskQueuedNode          task_queued
 *   │   ├── TaskDequeuedNode        task_dequeued
 *   │   ├── TaskPriorityChangedNode task_priority_changed
 *   │   └── TaskCancelledNode       task_cancelled
 *   ├── branch/BranchManagementNode (lime branch family)
 *   │   ├── BranchSelectedNode           branch_selected
 *   │   ├── BranchCreatedNode            branch_created
 *   │   ├── BranchReusedNode             branch_reused
 *   │   ├── BranchConflictDetectedNode   branch_conflict_detected
 *   │   ├── BranchStaleDetectedNode      branch_stale_detected
 *   │   └── BranchSelectionEscalatedNode branch_selection_escalated
 *   ├── issue/IssueManagementNode (cyan issue family)
 *   │   ├── SubIssueCreatedNode         sub_issue_created
 *   │   └── SubIssueCreationFailedNode  sub_issue_creation_failed
 *   └── system/SystemOperationsNode (slate system family)
 *       ├── ExecutionStateReconciledNode  execution_state_reconciled
 *       ├── StatusValidationFailureNode   status_validation_failure
 *       ├── ResultPersistenceFailedNode   result_persistence_failed
 *       ├── FallbackStorageUsedNode       fallback_storage_used
 *       ├── OutputValidationFailedNode    output_validation_failed
 *       ├── EmptyOutputDetectedNode       empty_output_detected
 *       └── ContainerResultRecoveredNode  container_result_recovered
 */
/**
 * Backend event types intentionally NOT mapped here because buildFlowchart.js
 * filters them out before reaching getNodeType() — they never render as graph nodes:
 *
 *   agent_lifecycle (except agent_initialized):
 *     task_received, agent_started, agent_completed, agent_failed
 *
 *   Infrastructure / telemetry:
 *     prompt_constructed, claude_api_call_started, claude_api_call_completed,
 *     claude_api_call_failed, container_launch_started, container_launch_succeeded,
 *     container_launch_failed, container_execution_started, container_execution_completed,
 *     container_execution_failed, response_chunk_received, response_processing_started,
 *     response_processing_completed, tool_execution_started, tool_execution_completed,
 *     performance_metric, token_usage
 *
 * Note: pipeline_created and pipeline_completed are NOT emitted as backend events.
 * Those boundary nodes are constructed statically in buildFlowchart.js from
 * selectedPipelineRun metadata and assigned type 'pipelineStarted'/'pipelineCompleted'
 * directly — getNodeType() is never called for them.
 */
export const EVENT_TYPE_MAP = {
  // ── Agent execution ──────────────────────────────────────────────────────────────
  agent_initialized: 'agentExecution',

  // ── Review cycle ─────────────────────────────────────────────────────────────────
  review_cycle_started:            'reviewCycleStarted',
  review_cycle_iteration:          'reviewCycleIteration',
  review_cycle_maker_selected:     'reviewCycleMakerSelected',
  review_cycle_reviewer_selected:  'reviewCycleReviewerSelected',
  review_cycle_escalated:          'reviewCycleEscalated',
  review_cycle_completed:          'reviewCycleCompleted',

  // ── Repair cycle ─────────────────────────────────────────────────────────────────
  repair_cycle_started:    'repairCycleStarted',
  repair_cycle_iteration:  'repairCycleIteration',
  repair_cycle_completed:  'repairCycleCompleted',
  repair_cycle_failed:     'repairCycleFailed',

  repair_cycle_env_rebuild_started:   'repairCycleEnvRebuildStarted',
  repair_cycle_env_rebuild_completed: 'repairCycleEnvRebuildCompleted',

  // Repair cycle — test sub-family
  repair_cycle_test_cycle_started:        'repairCycleTestCycleStarted',
  repair_cycle_test_cycle_completed:      'repairCycleTestCycleCompleted',
  repair_cycle_test_execution_started:    'repairCycleTestExecutionStarted',
  repair_cycle_test_execution_completed:  'repairCycleTestExecutionCompleted',

  // Repair cycle — fix sub-family
  repair_cycle_fix_cycle_started:   'repairCycleFixCycleStarted',
  repair_cycle_fix_cycle_completed: 'repairCycleFixCycleCompleted',
  repair_cycle_file_fix_started:    'repairCycleFileFixStarted',
  repair_cycle_file_fix_completed:  'repairCycleFileFixCompleted',
  repair_cycle_file_fix_failed:     'repairCycleFileFixFailed',

  // Repair cycle — warning review sub-family
  repair_cycle_warning_review_started:   'repairCycleWarningReviewStarted',
  repair_cycle_warning_review_completed: 'repairCycleWarningReviewCompleted',
  repair_cycle_warning_review_failed:    'repairCycleWarningReviewFailed',

  // Repair cycle — systemic sub-family
  repair_cycle_systemic_analysis_started:   'repairCycleSystemicAnalysisStarted',
  repair_cycle_systemic_analysis_completed: 'repairCycleSystemicAnalysisCompleted',
  repair_cycle_systemic_fix_started:        'repairCycleSystemicFixStarted',
  repair_cycle_systemic_fix_completed:      'repairCycleSystemicFixCompleted',

  // Repair cycle — container lifecycle sub-family
  repair_cycle_container_started:              'repairCycleContainerStarted',
  repair_cycle_container_checkpoint_updated:   'repairCycleContainerCheckpointUpdated',
  repair_cycle_container_recovered:            'repairCycleContainerRecovered',
  repair_cycle_container_killed:               'repairCycleContainerKilled',
  repair_cycle_container_completed:            'repairCycleContainerCompleted',

  // ── PR review ────────────────────────────────────────────────────────────────────
  pr_review_stage_started:   'prReviewStageStarted',
  pr_review_phase_started:   'prReviewPhaseStarted',
  pr_review_phase_completed: 'prReviewPhaseCompleted',
  pr_review_phase_failed:    'prReviewPhaseFailed',
  pr_review_stage_completed: 'prReviewStageCompleted',

  // ── Status & pipeline progression ────────────────────────────────────────────────
  status_progression_started:   'statusProgressionStarted',
  status_progression_completed: 'statusProgressionCompleted',
  status_progression_failed:    'statusProgressionFailed',
  pipeline_stage_transition:    'pipelineStageTransition',
  pipeline_run_started:         'pipelineRunStarted',
  pipeline_run_completed:       'pipelineRunCompleted',
  pipeline_run_failed:          'pipelineRunFailed',

  // ── Agent routing & selection ─────────────────────────────────────────────────────
  agent_routing_decision:     'agentRoutingDecision',
  agent_selected:             'agentSelected',
  workspace_routing_decision: 'workspaceRoutingDecision',

  // ── Feedback monitoring ───────────────────────────────────────────────────────────
  feedback_detected:          'feedbackDetected',
  feedback_listening_started: 'feedbackListeningStarted',
  feedback_listening_stopped: 'feedbackListeningStopped',
  feedback_ignored:           'feedbackIgnored',

  // ── Conversational loop ───────────────────────────────────────────────────────────
  conversational_loop_started:    'conversationalLoopStarted',
  conversational_question_routed: 'conversationalQuestionRouted',
  conversational_loop_paused:     'conversationalLoopPaused',
  conversational_loop_resumed:    'conversationalLoopResumed',

  // ── Error handling & circuit breakers ────────────────────────────────────────────
  error_encountered:      'errorEncountered',
  error_recovered:        'errorRecovered',
  circuit_breaker_opened: 'circuitBreakerOpened',
  circuit_breaker_closed: 'circuitBreakerClosed',
  retry_attempted:        'retryAttempted',

  // ── Task queue management ─────────────────────────────────────────────────────────
  task_queued:           'taskQueued',
  task_dequeued:         'taskDequeued',
  task_priority_changed: 'taskPriorityChanged',
  task_cancelled:        'taskCancelled',

  // ── Branch management ─────────────────────────────────────────────────────────────
  branch_selected:            'branchSelected',
  branch_created:             'branchCreated',
  branch_reused:              'branchReused',
  branch_conflict_detected:   'branchConflictDetected',
  branch_stale_detected:      'branchStaleDetected',
  branch_selection_escalated: 'branchSelectionEscalated',

  // ── Issue management ──────────────────────────────────────────────────────────────
  sub_issue_created:         'subIssueCreated',
  sub_issue_creation_failed: 'subIssueCreationFailed',

  // ── System operations ─────────────────────────────────────────────────────────────
  execution_state_reconciled: 'executionStateReconciled',
  status_validation_failure:  'statusValidationFailure',
  result_persistence_failed:  'resultPersistenceFailed',
  fallback_storage_used:      'fallbackStorageUsed',
  output_validation_failed:   'outputValidationFailed',
  empty_output_detected:      'emptyOutputDetected',
  container_result_recovered: 'containerResultRecovered',
}

/**
 * Returns the ReactFlow node type key for a given backend event_type string.
 * Falls back to 'pipelineEvent' (base PipelineEventNode) for unrecognised types.
 */
export function getNodeType(eventType) {
  return EVENT_TYPE_MAP[eventType] ?? 'pipelineEvent'
}

/**
 * Node types that are hidden by default (cycle boundary events).
 * These nodes exist in the graph for edge routing but are not rendered unless
 * the user explicitly enables full visibility (e.g. "Show all nodes" toggle).
 */
export const HIDDEN_BY_DEFAULT_TYPES = new Set([
  // Review cycle boundaries
  'reviewCycleStarted',
  'reviewCycleCompleted',

  // Repair cycle boundaries
  'repairCycleCompleted',
  'repairCycleFailed',

  // Repair cycle — test sub-family boundaries
  'repairCycleTestCycleStarted',
  'repairCycleTestCycleCompleted',

  // Repair cycle — fix sub-family boundaries
  'repairCycleFixCycleStarted',
  'repairCycleFixCycleCompleted',

  // Repair cycle — warning review boundaries
  'repairCycleWarningReviewStarted',
  'repairCycleWarningReviewCompleted',
  'repairCycleWarningReviewFailed',

  // Repair cycle — systemic boundaries
  'repairCycleSystemicAnalysisStarted',
  'repairCycleSystemicAnalysisCompleted',
  'repairCycleSystemicFixStarted',
  'repairCycleSystemicFixCompleted',

  // Repair cycle — container lifecycle boundaries
  'repairCycleContainerStarted',
  'repairCycleContainerCompleted',
  'repairCycleContainerKilled',

  // PR review boundaries
  'prReviewStageStarted',
  'prReviewStageCompleted',
  'prReviewPhaseStarted',
  'prReviewPhaseCompleted',
  'prReviewPhaseFailed',

  // Conversational loop boundaries
  'conversationalLoopStarted',

  // Status progression boundaries
  'statusProgressionStarted',
  'statusProgressionCompleted',
  'statusProgressionFailed',
])
