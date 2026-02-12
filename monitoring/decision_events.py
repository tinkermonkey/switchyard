"""
Decision Event Emitter for Orchestrator Observability

Provides convenient methods for emitting orchestrator decision events.
Extends the existing ObservabilityManager with decision-specific event types.
"""

import logging
from typing import Dict, Any, Optional, List
from datetime import datetime
from monitoring.observability import ObservabilityManager, EventType

logger = logging.getLogger(__name__)


class DecisionEventEmitter:
    """
    Helper for emitting orchestrator decision events with consistent structure.
    
    Wraps ObservabilityManager to provide decision-specific convenience methods.
    All decision events follow a consistent schema with:
    - decision_category: High-level category (routing, progression, error_handling, etc.)
    - inputs: Data used to make the decision
    - decision: The decision that was made
    - reason: Human-readable explanation
    - reasoning_data: Structured reasoning details
    """
    
    def __init__(self, obs_manager: ObservabilityManager):
        """
        Initialize decision event emitter
        
        Args:
            obs_manager: ObservabilityManager instance to use for event emission
        """
        self.obs = obs_manager
    
    def emit_agent_routing_decision(
        self,
        issue_number: int,
        project: str,
        board: str,
        current_status: str,
        selected_agent: str,
        reason: str,
        alternatives: Optional[List[str]] = None,
        workspace_type: str = "issues",
        discussion_id: Optional[str] = None,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when orchestrator selects which agent to run
        
        Args:
            issue_number: GitHub issue number
            project: Project name
            board: Board/pipeline name
            current_status: Current column/status in board
            selected_agent: Agent that was selected
            reason: Why this agent was selected
            alternatives: Other agents that could have been selected
            workspace_type: "issues" or "discussions"
            discussion_id: Discussion ID if using discussions workspace
            pipeline_run_id: Pipeline run ID for traceability
        """
        task_id = f"routing_{project}_{issue_number}"
        
        self.obs.emit(
            EventType.AGENT_ROUTING_DECISION,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data={
                'decision_category': 'routing',
                'issue_number': issue_number,
                'board': board,
                'workspace_type': workspace_type,
                'discussion_id': discussion_id,
                'inputs': {
                    'current_status': current_status,
                    'available_agents': alternatives or []
                },
                'decision': {
                    'selected_agent': selected_agent
                },
                'reason': reason,
                'reasoning_data': {
                    'selection_method': 'workflow_mapping',
                    'alternatives_considered': alternatives or []
                }
            },
            pipeline_run_id=pipeline_run_id
        )
    
    def emit_agent_selected(
        self,
        issue_number: int,
        project: str,
        board: str,
        selected_agent: str,
        reason: str,
        selection_criteria: Optional[Dict[str, Any]] = None
    ):
        """
        Emit simplified agent selection event
        
        Args:
            issue_number: GitHub issue number
            project: Project name
            board: Board name
            selected_agent: Selected agent
            reason: Selection reason
            selection_criteria: Criteria used for selection
        """
        task_id = f"agent_selected_{project}_{issue_number}"
        
        self.obs.emit(
            EventType.AGENT_SELECTED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data={
                'decision_category': 'routing',
                'issue_number': issue_number,
                'board': board,
                'decision': {
                    'agent': selected_agent
                },
                'reason': reason,
                'selection_criteria': selection_criteria or {}
            }
        )
    
    def emit_feedback_detected(
        self,
        issue_number: int,
        project: str,
        board: str,
        feedback_source: str,
        feedback_content: str,
        target_agent: Optional[str],
        action_taken: str,
        workspace_type: str = "issues",
        discussion_id: Optional[str] = None,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when orchestrator detects feedback on an issue
        
        Args:
            issue_number: GitHub issue number
            project: Project name
            board: Board name
            feedback_source: Where feedback came from ("comment", "discussion_reply", "label", "status_change")
            feedback_content: The feedback content (will be truncated)
            target_agent: Agent the feedback is routed to (if any)
            action_taken: What action was taken ("queue_agent_task", "ignore", "escalate", etc.)
            workspace_type: "issues" or "discussions"
            discussion_id: Discussion ID if applicable
            pipeline_run_id: Pipeline run ID for traceability
        """
        task_id = f"feedback_{project}_{issue_number}_{datetime.now().timestamp()}"
        
        # Truncate feedback content for event
        truncated_content = feedback_content[:500] if feedback_content else ""
        if len(feedback_content) > 500:
            truncated_content += "..."
        
        self.obs.emit(
            EventType.FEEDBACK_DETECTED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data={
                'decision_category': 'feedback',
                'issue_number': issue_number,
                'board': board,
                'workspace_type': workspace_type,
                'discussion_id': discussion_id,
                'inputs': {
                    'feedback_source': feedback_source,
                    'feedback_content': truncated_content,
                    'feedback_length': len(feedback_content)
                },
                'decision': {
                    'action_taken': action_taken,
                    'target_agent': target_agent
                },
                'reason': f"Detected feedback from {feedback_source}, {action_taken}"
            },
            pipeline_run_id=pipeline_run_id
        )
    
    def emit_feedback_listening_started(
        self,
        issue_number: int,
        project: str,
        board: str,
        agent: str,
        monitoring_for: List[str],
        workspace_type: str = "issues",
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when orchestrator starts listening for feedback
        
        Args:
            issue_number: Issue number being monitored
            project: Project name
            board: Board name
            agent: Agent that's waiting for feedback
            monitoring_for: What types of feedback we're monitoring for
            workspace_type: "issues" or "discussions"
            pipeline_run_id: Pipeline run ID for traceability
        """
        task_id = f"feedback_listen_{project}_{issue_number}"
        
        self.obs.emit(
            EventType.FEEDBACK_LISTENING_STARTED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data={
                'decision_category': 'feedback',
                'issue_number': issue_number,
                'board': board,
                'workspace_type': workspace_type,
                'monitoring_agent': agent,
                'monitoring_for': monitoring_for,
                'reason': f"Starting feedback monitoring for {agent}"
            },
            pipeline_run_id=pipeline_run_id
        )
    
    def emit_feedback_listening_stopped(
        self,
        issue_number: int,
        project: str,
        board: str,
        agent: str,
        reason: str,
        feedback_received: bool = False,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when orchestrator stops listening for feedback
        
        Args:
            issue_number: Issue number
            project: Project name
            board: Board name
            agent: Agent that was waiting
            reason: Why monitoring stopped
            feedback_received: Whether feedback was received
            pipeline_run_id: Pipeline run ID for traceability
        """
        task_id = f"feedback_listen_{project}_{issue_number}"
        
        self.obs.emit(
            EventType.FEEDBACK_LISTENING_STOPPED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data={
                'decision_category': 'feedback',
                'issue_number': issue_number,
                'board': board,
                'monitoring_agent': agent,
                'feedback_received': feedback_received,
                'reason': reason
            },
            pipeline_run_id=pipeline_run_id
        )
    
    def emit_feedback_ignored(
        self,
        issue_number: int,
        project: str,
        board: str,
        feedback_source: str,
        reason: str
    ):
        """
        Emit event when feedback is detected but ignored
        
        Args:
            issue_number: Issue number
            project: Project name
            board: Board name
            feedback_source: Source of feedback
            reason: Why it was ignored
        """
        task_id = f"feedback_ignored_{project}_{issue_number}"
        
        self.obs.emit(
            EventType.FEEDBACK_IGNORED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data={
                'decision_category': 'feedback',
                'issue_number': issue_number,
                'board': board,
                'feedback_source': feedback_source,
                'reason': reason
            }
        )
    
    def emit_status_progression(
        self,
        issue_number: int,
        project: str,
        board: str,
        from_status: str,
        to_status: str,
        trigger: str,
        success: Optional[bool] = None,
        error: Optional[str] = None,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when orchestrator moves an issue to a new status
        
        Args:
            issue_number: GitHub issue number
            project: Project name
            board: Board name
            from_status: Current status/column
            to_status: Target status/column
            trigger: What triggered progression ("agent_completion", "manual", "auto_progression")
            success: Whether progression succeeded (None if not yet executed)
            error: Error message if failed
        """
        task_id = f"progression_{project}_{issue_number}"
        
        # Select event type based on success state
        if success is None:
            event_type = EventType.STATUS_PROGRESSION_STARTED
        elif success:
            event_type = EventType.STATUS_PROGRESSION_COMPLETED
        else:
            event_type = EventType.STATUS_PROGRESSION_FAILED
        
        self.obs.emit(
            event_type,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            pipeline_run_id=pipeline_run_id,
            data={
                'decision_category': 'progression',
                'issue_number': issue_number,
                'board': board,
                'inputs': {
                    'from_status': from_status,
                    'trigger': trigger
                },
                'decision': {
                    'to_status': to_status
                },
                'success': success,
                'error': error,
                'reason': f"{'Progressing' if success is None else 'Progressed' if success else 'Failed to progress'} issue from {from_status} to {to_status} (trigger: {trigger})"
            }
        )
    
    def emit_pipeline_stage_transition(
        self,
        issue_number: int,
        project: str,
        board: str,
        from_stage: str,
        to_stage: str,
        reason: str
    ):
        """
        Emit event when issue transitions between pipeline stages
        
        Args:
            issue_number: Issue number
            project: Project name
            board: Board name
            from_stage: Previous stage
            to_stage: New stage
            reason: Why transition occurred
        """
        task_id = f"stage_transition_{project}_{issue_number}"
        
        self.obs.emit(
            EventType.PIPELINE_STAGE_TRANSITION,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data={
                'decision_category': 'progression',
                'issue_number': issue_number,
                'board': board,
                'from_stage': from_stage,
                'to_stage': to_stage,
                'reason': reason
            }
        )
    
    def emit_review_cycle_decision(
        self,
        issue_number: int,
        project: str,
        board: str,
        cycle_iteration: int,
        decision_type: str,
        maker_agent: str,
        reviewer_agent: str,
        reason: str,
        additional_data: Optional[Dict[str, Any]] = None,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event for review cycle routing decisions
        
        Args:
            issue_number: Issue number
            project: Project name
            board: Board name
            cycle_iteration: Current iteration number
            decision_type: Type of decision ("start", "iteration", "maker_selected", 
                          "reviewer_selected", "escalate", "complete")
            maker_agent: Maker agent name
            reviewer_agent: Reviewer agent name
            reason: Why this decision was made
            additional_data: Additional context data
            pipeline_run_id: Pipeline run ID for traceability
        """
        # Map decision type to event type
        event_map = {
            'start': EventType.REVIEW_CYCLE_STARTED,
            'iteration': EventType.REVIEW_CYCLE_ITERATION,
            'maker_selected': EventType.REVIEW_CYCLE_MAKER_SELECTED,
            'reviewer_selected': EventType.REVIEW_CYCLE_REVIEWER_SELECTED,
            'escalate': EventType.REVIEW_CYCLE_ESCALATED,
            'complete': EventType.REVIEW_CYCLE_COMPLETED
        }
        
        event_type = event_map.get(decision_type, EventType.REVIEW_CYCLE_ITERATION)
        task_id = f"review_cycle_{project}_{issue_number}_{cycle_iteration}"
        
        data = {
            'decision_category': 'review_cycle',
            'issue_number': issue_number,
            'board': board,
            'inputs': {
                'cycle_iteration': cycle_iteration,
                'maker_agent': maker_agent,
                'reviewer_agent': reviewer_agent
            },
            'decision': {
                'decision_type': decision_type
            },
            'reason': reason
        }
        
        # Merge additional data
        if additional_data:
            data.update(additional_data)
        
        self.obs.emit(
            event_type,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data=data,
            pipeline_run_id=pipeline_run_id
        )
    
    def emit_conversational_loop_started(
        self,
        issue_number: int,
        project: str,
        board: str,
        agent: str,
        workspace_type: str = "issues",
        discussion_id: Optional[str] = None,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when conversational loop starts
        
        Args:
            issue_number: Issue number
            project: Project name
            board: Board name
            agent: Agent handling conversations
            workspace_type: "issues" or "discussions"
            discussion_id: Discussion ID if applicable
            pipeline_run_id: Pipeline run ID for traceability
        """
        task_id = f"conv_loop_{project}_{issue_number}"
        
        self.obs.emit(
            EventType.CONVERSATIONAL_LOOP_STARTED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data={
                'decision_category': 'conversational_loop',
                'issue_number': issue_number,
                'board': board,
                'workspace_type': workspace_type,
                'discussion_id': discussion_id,
                'agent': agent,
                'reason': f"Starting conversational loop with {agent}"
            },
            pipeline_run_id=pipeline_run_id
        )
    
    def emit_conversational_question_routed(
        self,
        issue_number: int,
        project: str,
        board: str,
        question: str,
        target_agent: str,
        reason: str,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when question is routed to specific agent
        
        Args:
            issue_number: Issue number
            project: Project name
            board: Board name
            question: The question (truncated)
            target_agent: Agent receiving the question
            reason: Why this agent was selected
            pipeline_run_id: Pipeline run ID for traceability
        """
        task_id = f"conv_route_{project}_{issue_number}_{datetime.now().timestamp()}"
        
        # Truncate question
        truncated_question = question[:200] if question else ""
        if len(question) > 200:
            truncated_question += "..."
        
        self.obs.emit(
            EventType.CONVERSATIONAL_QUESTION_ROUTED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data={
                'decision_category': 'conversational_loop',
                'issue_number': issue_number,
                'board': board,
                'question': truncated_question,
                'target_agent': target_agent,
                'reason': reason
            },
            pipeline_run_id=pipeline_run_id
        )
    
    def emit_conversational_loop_paused(
        self,
        issue_number: int,
        project: str,
        board: str,
        reason: str,
        workspace_type: str = "issues",
        discussion_id: Optional[str] = None,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when conversational loop is paused
        
        Args:
            issue_number: Issue number
            project: Project name
            board: Board name
            reason: Why the loop was paused
            workspace_type: "issues" or "discussions"
            discussion_id: Discussion ID if applicable
            pipeline_run_id: Pipeline run ID for traceability
        """
        task_id = f"conv_loop_{project}_{issue_number}"
        
        self.obs.emit(
            EventType.CONVERSATIONAL_LOOP_PAUSED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data={
                'decision_category': 'conversational_loop',
                'issue_number': issue_number,
                'board': board,
                'workspace_type': workspace_type,
                'discussion_id': discussion_id,
                'reason': reason
            },
            pipeline_run_id=pipeline_run_id
        )
    
    def emit_conversational_loop_resumed(
        self,
        issue_number: int,
        project: str,
        board: str,
        reason: str,
        workspace_type: str = "issues",
        discussion_id: Optional[str] = None,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when conversational loop resumes
        
        Args:
            issue_number: Issue number
            project: Project name
            board: Board name
            reason: Why the loop resumed
            workspace_type: "issues" or "discussions"
            discussion_id: Discussion ID if applicable
            pipeline_run_id: Pipeline run ID for traceability
        """
        task_id = f"conv_loop_{project}_{issue_number}"
        
        self.obs.emit(
            EventType.CONVERSATIONAL_LOOP_RESUMED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data={
                'decision_category': 'conversational_loop',
                'issue_number': issue_number,
                'board': board,
                'workspace_type': workspace_type,
                'discussion_id': discussion_id,
                'reason': reason
            },
            pipeline_run_id=pipeline_run_id
        )
    
    def emit_error_decision(
        self,
        error_type: str,
        error_message: str,
        context: Dict[str, Any],
        recovery_action: str,
        success: bool,
        project: str = "unknown",
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when orchestrator handles an error

        Args:
            error_type: Type/class of error
            error_message: Error message
            context: Context in which error occurred
            recovery_action: What action was taken to recover
            success: Whether recovery succeeded
            project: Project name (if applicable)
            pipeline_run_id: Optional pipeline run ID to associate with this error
        """
        task_id = f"error_{datetime.now().timestamp()}"
        event_type = EventType.ERROR_RECOVERED if success else EventType.ERROR_ENCOUNTERED

        self.obs.emit(
            event_type,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            pipeline_run_id=pipeline_run_id,
            data={
                'decision_category': 'error_handling',
                'error_type': error_type,
                'error_message': error_message,
                'context': context,
                'decision': {
                    'recovery_action': recovery_action
                },
                'success': success,
                'reason': f"{'Successfully recovered' if success else 'Failed to recover'} from {error_type}: {recovery_action}"
            }
        )

    def emit_execution_state_reconciled(
        self,
        agent: str,
        project: str,
        issue_number: int,
        column: str,
        recovered_outcome: str,
        context: Dict[str, Any],
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when a successful agent execution is recovered from Redis
        during orchestrator startup. This is not an error — the agent completed
        successfully and the result was found in Redis after a restart. The
        monitoring loop will re-detect the card position and continue the pipeline.

        Only used for the success path. Failed executions still use emit_error_decision.

        Args:
            agent: Agent name that completed
            project: Project name
            issue_number: Issue number
            column: Board column the issue was in
            recovered_outcome: The recovered outcome (currently always 'success')
            context: Additional context about the reconciliation
            pipeline_run_id: Optional pipeline run ID
        """
        task_id = f"reconcile_{project}_{issue_number}_{datetime.now().timestamp()}"

        self.obs.emit(
            EventType.EXECUTION_STATE_RECONCILED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            pipeline_run_id=pipeline_run_id,
            data={
                'decision_category': 'state_reconciliation',
                'agent_name': agent,
                'issue_number': issue_number,
                'column': column,
                'recovered_outcome': recovered_outcome,
                'context': context,
                'reason': (
                    f"Agent {agent} execution for #{issue_number} recovered from Redis "
                    f"after orchestrator restart (outcome: {recovered_outcome}). "
                    f"Monitoring loop will re-detect card position and continue pipeline."
                )
            }
        )

    def emit_circuit_breaker_opened(
        self,
        circuit_name: str,
        failure_count: int,
        threshold: int,
        last_error: Optional[str] = None
    ):
        """
        Emit event when circuit breaker opens
        
        Args:
            circuit_name: Name of circuit breaker
            failure_count: Number of failures
            threshold: Threshold that triggered opening
            last_error: Last error that triggered opening
        """
        task_id = f"circuit_breaker_{circuit_name}"
        
        self.obs.emit(
            EventType.CIRCUIT_BREAKER_OPENED,
            agent="orchestrator",
            task_id=task_id,
            project="system",
            data={
                'decision_category': 'error_handling',
                'circuit_name': circuit_name,
                'failure_count': failure_count,
                'threshold': threshold,
                'last_error': last_error,
                'reason': f"Circuit breaker '{circuit_name}' opened after {failure_count} failures (threshold: {threshold})"
            }
        )
    
    def emit_circuit_breaker_closed(
        self,
        circuit_name: str,
        reason: str
    ):
        """
        Emit event when circuit breaker closes
        
        Args:
            circuit_name: Name of circuit breaker
            reason: Why it was closed
        """
        task_id = f"circuit_breaker_{circuit_name}"
        
        self.obs.emit(
            EventType.CIRCUIT_BREAKER_CLOSED,
            agent="orchestrator",
            task_id=task_id,
            project="system",
            data={
                'decision_category': 'error_handling',
                'circuit_name': circuit_name,
                'reason': reason
            }
        )
    
    def emit_retry_attempted(
        self,
        operation_name: str,
        attempt_number: int,
        max_attempts: int,
        project: str,
        issue_number: Optional[int] = None,
        last_error: Optional[str] = None
    ):
        """
        Emit event when a retry is attempted
        
        Args:
            operation_name: Name of operation being retried
            attempt_number: Current attempt number (1-indexed)
            max_attempts: Maximum number of attempts
            project: Project name
            issue_number: Optional issue number if retry is issue-specific
            last_error: Error that triggered the retry
        """
        task_id = f"retry_{project}_{issue_number or operation_name}"
        
        self.obs.emit(
            EventType.RETRY_ATTEMPTED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data={
                'decision_category': 'error_handling',
                'issue_number': issue_number,
                'operation_name': operation_name,
                'attempt_number': attempt_number,
                'max_attempts': max_attempts,
                'last_error': last_error,
                'reason': f"Retry attempt {attempt_number}/{max_attempts} for {operation_name}"
            }
        )
    
    def emit_workspace_routing(
        self,
        issue_number: int,
        project: str,
        board: str,
        stage: str,
        selected_workspace: str,
        category_id: Optional[str],
        reason: str,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when orchestrator routes work to issues vs discussions
        
        Args:
            issue_number: Issue number
            project: Project name
            board: Board name
            stage: Pipeline stage
            selected_workspace: "issues" or "discussions"
            category_id: Discussion category ID (if discussions)
            reason: Why this workspace was selected
        """
        task_id = f"workspace_routing_{project}_{issue_number}"
        
        self.obs.emit(
            EventType.WORKSPACE_ROUTING_DECISION,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            pipeline_run_id=pipeline_run_id,
            data={
                'decision_category': 'routing',
                'issue_number': issue_number,
                'board': board,
                'inputs': {
                    'stage': stage
                },
                'decision': {
                    'workspace': selected_workspace,
                    'category_id': category_id
                },
                'reason': reason
            }
        )
    
    def emit_task_queued(
        self,
        agent: str,
        project: str,
        issue_number: int,
        board: str,
        priority: str,
        reason: str,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when task is queued
        
        Args:
            agent: Agent name
            project: Project name
            issue_number: Issue number
            board: Board name
            priority: Task priority
            reason: Why task was queued
            pipeline_run_id: Pipeline run ID for traceability
        """
        task_id = f"task_queue_{project}_{issue_number}_{datetime.now().timestamp()}"
        
        self.obs.emit(
            EventType.TASK_QUEUED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data={
                'decision_category': 'task_management',
                'agent': agent,
                'issue_number': issue_number,
                'board': board,
                'priority': priority,
                'reason': reason
            },
            pipeline_run_id=pipeline_run_id
        )
    
    def emit_task_dequeued(
        self,
        agent: str,
        project: str,
        issue_number: int,
        board: str,
        reason: Optional[str] = None,
        workspace_type: str = "issues"
    ):
        """
        Emit event when task is taken from queue for execution
        
        Args:
            agent: Agent name
            project: Project name
            issue_number: Issue number
            board: Board name
            reason: Optional reason why task was dequeued
            workspace_type: Type of workspace ("issues" or "discussions")
        """
        task_id = f"task_queue_{project}_{issue_number}"
        
        self.obs.emit(
            EventType.TASK_DEQUEUED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data={
                'decision_category': 'task_management',
                'agent': agent,
                'issue_number': issue_number,
                'board': board,
                'workspace_type': workspace_type,
                'reason': reason or f"Task dequeued for execution by agent '{agent}'"
            }
        )
    
    def emit_task_priority_changed(
        self,
        project: str,
        issue_number: int,
        board: str,
        old_priority: str,
        new_priority: str,
        reason: str,
        workspace_type: str = "issues"
    ):
        """
        Emit event when a task's priority is changed
        
        Args:
            project: Project name
            issue_number: Issue number
            board: Board name
            old_priority: Previous priority
            new_priority: New priority
            reason: Why priority was changed
            workspace_type: Type of workspace ("issues" or "discussions")
        """
        task_id = f"task_queue_{project}_{issue_number}"
        
        self.obs.emit(
            EventType.TASK_PRIORITY_CHANGED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data={
                'decision_category': 'task_management',
                'issue_number': issue_number,
                'board': board,
                'workspace_type': workspace_type,
                'old_priority': old_priority,
                'new_priority': new_priority,
                'decision': {
                    'action': 'change_priority',
                    'new_priority': new_priority
                },
                'reason': reason
            }
        )
    
    def emit_task_cancelled(
        self,
        project: str,
        issue_number: int,
        board: str,
        agent: str,
        reason: str,
        workspace_type: str = "issues"
    ):
        """
        Emit event when a task is cancelled
        
        Args:
            project: Project name
            issue_number: Issue number
            board: Board name
            agent: Agent the task was assigned to
            reason: Why the task was cancelled
            workspace_type: Type of workspace ("issues" or "discussions")
        """
        task_id = f"task_queue_{project}_{issue_number}"
        
        self.obs.emit(
            EventType.TASK_CANCELLED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            data={
                'decision_category': 'task_management',
                'agent': agent,
                'issue_number': issue_number,
                'board': board,
                'workspace_type': workspace_type,
                'decision': {
                    'action': 'cancel_task'
                },
                'reason': reason
            }
        )

    def emit_branch_reused(
        self,
        project: str,
        issue_number: int,
        branch_name: str,
        reason: Optional[str] = None,
        match_reason: Optional[str] = None,
        confidence: Optional[float] = None,
        parent_issue: Optional[int] = None,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when existing branch is reused

        Args:
            project: Project name
            issue_number: Issue number
            branch_name: Branch being reused
            reason: Why this branch was reused (alternative to match_reason)
            match_reason: Why this branch was selected (alternative to reason)
            confidence: Match confidence (0.0 to 1.0), optional
            parent_issue: Parent issue number if this is a sub-issue
        """
        task_id = f"branch_management_{project}_{issue_number}"

        # Use whichever reason parameter was provided
        final_reason = match_reason or reason or "Branch reused"

        # Determine reuse method based on reason content
        reuse_method = None
        if reason and "direct git" in reason.lower():
            reuse_method = "direct_git_fallback"
        elif confidence is not None:
            reuse_method = "confidence_match"

        # Build event data
        event_data = {
            'decision_category': 'branch_management',
            'issue_number': issue_number,
            'parent_issue': parent_issue,
            'inputs': {
                'existing_branch': branch_name,
            },
            'decision': {
                'action': 'reuse_existing_branch',
                'branch_name': branch_name
            },
            'reason': final_reason
        }

        # Add confidence data if available
        if confidence is not None:
            event_data['inputs']['confidence'] = confidence
            event_data['reasoning_data'] = {
                'confidence_score': confidence,
                'match_type': final_reason
            }

        # Add reuse method if determined
        if reuse_method:
            event_data['inputs']['reuse_method'] = reuse_method

        # Add has_parent indicator
        if parent_issue is not None:
            event_data['inputs']['has_parent'] = True

        self.obs.emit(
            EventType.BRANCH_REUSED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            pipeline_run_id=pipeline_run_id,
            data=event_data
        )
    
    def emit_branch_created(
        self,
        project: str,
        issue_number: int,
        branch_name: str,
        reason: str,
        parent_issue: Optional[int] = None,
        is_standalone: bool = True,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when new branch is created
        
        Args:
            project: Project name
            issue_number: Issue number
            branch_name: New branch name
            reason: Why new branch was created
            parent_issue: Parent issue number if feature branch
            is_standalone: Whether this is a standalone branch
        """
        task_id = f"branch_management_{project}_{issue_number}"
        
        branch_type = "standalone" if is_standalone else "feature"
        
        self.obs.emit(
            EventType.BRANCH_CREATED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            pipeline_run_id=pipeline_run_id,
            data={
                'decision_category': 'branch_management',
                'issue_number': issue_number,
                'parent_issue': parent_issue,
                'inputs': {
                    'is_standalone': is_standalone,
                    'has_parent': parent_issue is not None
                },
                'decision': {
                    'action': 'create_new_branch',
                    'branch_name': branch_name,
                    'branch_type': branch_type
                },
                'reason': reason
            }
        )


    def emit_branch_selection_escalated(
        self,
        project: str,
        issue_number: int,
        confidence: float,
        candidate_branches: List[Dict[str, Any]],
        reason: str,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when branch selection is escalated to human
        
        Args:
            project: Project name
            issue_number: Issue number
            confidence: Best match confidence
            candidate_branches: List of candidate branches with confidence
            reason: Why escalation was needed
        """
        task_id = f"branch_management_{project}_{issue_number}"
        
        self.obs.emit(
            EventType.BRANCH_SELECTION_ESCALATED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            pipeline_run_id=pipeline_run_id,
            data={
                'decision_category': 'branch_management',
                'issue_number': issue_number,
                'inputs': {
                    'best_confidence': confidence,
                    'candidate_count': len(candidate_branches),
                    'candidates': candidate_branches[:3]  # Top 3
                },
                'decision': {
                    'action': 'escalate_to_human',
                    'waiting_for': 'human_decision'
                },
                'reason': reason
            }
        )
    
    def emit_branch_conflict_detected(
        self,
        project: str,
        issue_number: int,
        branch_name: str,
        conflicting_files: List[str],
        parent_issue: Optional[int] = None,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when merge conflict is detected
        
        Args:
            project: Project name
            issue_number: Issue number
            branch_name: Branch with conflict
            conflicting_files: List of files with conflicts
            parent_issue: Parent issue number if applicable
        """
        task_id = f"branch_management_{project}_{issue_number}"
        
        self.obs.emit(
            EventType.BRANCH_CONFLICT_DETECTED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            pipeline_run_id=pipeline_run_id,
            data={
                'decision_category': 'branch_management',
                'issue_number': issue_number,
                'parent_issue': parent_issue,
                'branch_name': branch_name,
                'inputs': {
                    'conflicting_files': conflicting_files,
                    'conflict_count': len(conflicting_files)
                },
                'decision': {
                    'action': 'escalate_merge_conflict',
                    'requires_human_resolution': True
                },
                'reason': f"Merge conflict detected in {len(conflicting_files)} file(s) during git pull --rebase"
            }
        )
    
    def emit_branch_stale_detected(
        self,
        project: str,
        issue_number: int,
        branch_name: str,
        commits_behind: int,
        action_taken: str,
        parent_issue: Optional[int] = None,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Emit event when stale branch is detected
        
        Args:
            project: Project name
            issue_number: Issue number
            branch_name: Stale branch name
            commits_behind: Number of commits behind main
            action_taken: Action taken (warn, escalate, continue)
            parent_issue: Parent issue number if applicable
        """
        task_id = f"branch_management_{project}_{issue_number}"
        
        severity = "critical" if commits_behind > 50 else "warning" if commits_behind > 20 else "info"
        
        self.obs.emit(
            EventType.BRANCH_STALE_DETECTED,
            agent="orchestrator",
            task_id=task_id,
            project=project,
            pipeline_run_id=pipeline_run_id,
            data={
                'decision_category': 'branch_management',
                'issue_number': issue_number,
                'parent_issue': parent_issue,
                'branch_name': branch_name,
                'inputs': {
                    'commits_behind_main': commits_behind,
                    'severity': severity
                },
                'decision': {
                    'action': action_taken
                },
                'reason': f"Branch is {commits_behind} commits behind main ({severity} threshold)"
            }
        )


# Singleton getter for convenience
_decision_event_emitter: Optional[DecisionEventEmitter] = None


def get_decision_event_emitter() -> DecisionEventEmitter:
    """
    Get or create global DecisionEventEmitter instance
    
    Returns:
        DecisionEventEmitter instance
    """
    global _decision_event_emitter
    
    if _decision_event_emitter is None:
        from monitoring.observability import get_observability_manager
        obs = get_observability_manager()
        _decision_event_emitter = DecisionEventEmitter(obs)
    
    return _decision_event_emitter
