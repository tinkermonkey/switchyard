# Flowchart Events Inventory

This document lists every event type that can appear as a visual node on the pipeline flowchart. Events are sourced from `monitoring/observability.py` (`EventType` enum) and filtered through `web_ui/src/components/nodes/EVENT_TYPE_MAP.js`.

**Not included:** Events in `SKIP_EVENT_TYPES` (`buildFlowchart.js`) are intentionally not rendered: `task_received`, `agent_started`, `agent_completed`, `agent_failed`, `prompt_constructed`, `claude_api_call_*`, `container_launch_*`, `container_execution_*`, `response_chunk_received`, `response_processing_*`, `tool_execution_*`, `performance_metric`, `token_usage`.

**Hidden by default:** Events marked *(hidden by default)* are in `HIDDEN_BY_DEFAULT_TYPES` and require the "Show All" toggle to appear on the flowchart.

---

## Agent Execution

| Event Type | Display Title | Description |
|---|---|---|
| `agent_initialized` | Agent Initialized | Agent is initialized and ready to begin task processing |

## Routing & Selection

| Event Type | Display Title | Description |
|---|---|---|
| `agent_routing_decision` | Routing Decision | Orchestrator decides which agent to route the task to |
| `agent_selected` | Agent Selected | Specific agent selected for execution |
| `workspace_routing_decision` | Workspace Routing | Routing decision made for workspace assignment |

## Pipeline Lifecycle

| Event Type | Display Title | Description |
|---|---|---|
| `pipeline_run_started` | Pipeline Started | A new pipeline run has begun |
| `pipeline_run_completed` | Pipeline Completed | Pipeline run finished successfully |
| `pipeline_run_failed` | Pipeline Failed | Pipeline run ended in failure |
| `pipeline_stage_transition` | Stage Transition | Pipeline advanced to a new stage |

## Status Progression

| Event Type | Display Title | Description |
|---|---|---|
| `status_progression_started` | Progression Started | Status progression (column move) initiated |
| `status_progression_completed` | Progression Completed | Status progression completed successfully |
| `status_progression_failed` | Progression Failed | Status progression attempt failed |

## Review Cycle

| Event Type | Display Title | Description |
|---|---|---|
| `review_cycle_started` | Review Started | Maker-checker review cycle began |
| `review_cycle_iteration` | Review Iteration | A new review iteration is underway |
| `review_cycle_maker_selected` | Maker Selected | Maker agent chosen for this review iteration |
| `review_cycle_reviewer_selected` | Reviewer Selected | Reviewer agent chosen for this review iteration |
| `review_cycle_escalated` | Review Escalated | Review cycle escalated (max iterations reached) |
| `review_cycle_completed` | Review Completed | Review cycle finished successfully |

## Repair Cycle (Test-Fix)

| Event Type | Display Title | Description |
|---|---|---|
| `repair_cycle_started` | Repair Started | Repair cycle initiated to fix failing tests |
| `repair_cycle_iteration` | Repair Iteration | New repair iteration began |
| `repair_cycle_completed` | Repair Completed | Repair cycle completed successfully |
| `repair_cycle_failed` | Repair Failed | Repair cycle failed to resolve issues |
| `repair_cycle_env_rebuild_started` | Env Rebuild Started | Environment rebuild started during repair |
| `repair_cycle_env_rebuild_completed` | Env Rebuild Completed | Environment rebuild completed |

## Repair — Test Sub-cycle

| Event Type | Display Title | Description |
|---|---|---|
| `repair_cycle_test_cycle_started` | Test Cycle Started | Test cycle within repair began |
| `repair_cycle_test_cycle_completed` | Test Cycle Completed | Test cycle within repair completed |
| `repair_cycle_test_execution_started` | Test Execution Started | Individual test execution started |
| `repair_cycle_test_execution_completed` | Test Execution Completed | Individual test execution completed |

## Repair — Fix Sub-cycle

| Event Type | Display Title | Description |
|---|---|---|
| `repair_cycle_fix_cycle_started` | Fix Cycle Started | Fix cycle within repair began |
| `repair_cycle_fix_cycle_completed` | Fix Cycle Completed | Fix cycle within repair completed |
| `repair_cycle_file_fix_started` | File Fix Started | File-level fix agent started |
| `repair_cycle_file_fix_completed` | File Fix Completed | File-level fix completed successfully |
| `repair_cycle_file_fix_failed` | File Fix Failed | File-level fix attempt failed |

## Repair — Warning Review Sub-cycle

| Event Type | Display Title | Description |
|---|---|---|
| `repair_cycle_warning_review_started` | Warning Review Started | Warning review sub-cycle started |
| `repair_cycle_warning_review_completed` | Warning Review Completed | Warning review completed |
| `repair_cycle_warning_review_failed` | Warning Review Failed | Warning review failed |

## Repair — Systemic Analysis Sub-cycle

| Event Type | Display Title | Description |
|---|---|---|
| `repair_cycle_systemic_analysis_started` | Systemic Analysis Started | Systemic analysis of root causes started |
| `repair_cycle_systemic_analysis_completed` | Systemic Analysis Completed | Systemic analysis completed |
| `repair_cycle_systemic_fix_started` | Systemic Fix Started | Systemic fix agent started |
| `repair_cycle_systemic_fix_completed` | Systemic Fix Completed | Systemic fix completed |

## Repair — Container Sub-cycle

| Event Type | Display Title | Description |
|---|---|---|
| `repair_cycle_container_started` | Container Started | Repair container launched |
| `repair_cycle_container_checkpoint_updated` | Container Checkpoint | Repair container checkpoint saved *(hidden by default)* |
| `repair_cycle_container_recovered` | Container Recovered | Repair container recovered from failure |
| `repair_cycle_container_killed` | Container Killed | Repair container forcibly terminated |
| `repair_cycle_container_completed` | Container Completed | Repair container finished |

## PR Review

| Event Type | Display Title | Description |
|---|---|---|
| `pr_review_stage_started` | PR Review Started | PR review stage initiated |
| `pr_review_phase_started` | PR Review Phase Started | A phase of PR review began |
| `pr_review_phase_completed` | PR Review Phase Completed | A phase of PR review completed |
| `pr_review_phase_failed` | PR Review Phase Failed | A phase of PR review failed |
| `pr_review_stage_completed` | PR Review Completed | PR review stage completed |

## Feedback

| Event Type | Display Title | Description |
|---|---|---|
| `feedback_detected` | Feedback Detected | User or reviewer feedback detected |
| `feedback_listening_started` | Listening for Feedback | Orchestrator began listening for feedback |
| `feedback_listening_stopped` | Stopped Listening | Orchestrator stopped listening for feedback |
| `feedback_ignored` | Feedback Ignored | Feedback was detected but intentionally ignored |

## Conversational Loop

| Event Type | Display Title | Description |
|---|---|---|
| `conversational_loop_started` | Loop Started | Conversational Q&A loop started |
| `conversational_question_routed` | Question Routed | Question routed to appropriate agent |
| `conversational_loop_paused` | Loop Paused | Conversational loop paused awaiting input |
| `conversational_loop_resumed` | Loop Resumed | Conversational loop resumed after pause |

## Error Handling & Circuit Breakers

| Event Type | Display Title | Description |
|---|---|---|
| `error_encountered` | Error Encountered | An error was encountered during execution |
| `error_recovered` | Error Recovered | System recovered from a previous error |
| `circuit_breaker_opened` | Circuit Breaker Opened | Circuit breaker tripped due to repeated failures |
| `circuit_breaker_closed` | Circuit Breaker Closed | Circuit breaker reset and service restored |
| `retry_attempted` | Retry Attempted | A failed operation is being retried *(hidden by default)* |

## Task Queue

| Event Type | Display Title | Description |
|---|---|---|
| `task_queued` | Task Queued | Task added to the priority queue |
| `task_dequeued` | Task Dequeued | Task pulled from the queue for processing |
| `task_priority_changed` | Priority Changed | Task priority was updated in the queue *(hidden by default)* |
| `task_cancelled` | Task Cancelled | Task removed from queue before execution |

## Branch Management

| Event Type | Display Title | Description |
|---|---|---|
| `branch_selected` | Branch Selected | Git branch selected for this task |
| `branch_created` | Branch Created | New feature branch created |
| `branch_reused` | Branch Reused | Existing branch reused for this task |
| `branch_conflict_detected` | Branch Conflict | Branch conflict detected |
| `branch_stale_detected` | Stale Branch | Stale branch detected |
| `branch_selection_escalated` | Branch Escalated | Branch selection escalated due to conflict |

## Issue Management

| Event Type | Display Title | Description |
|---|---|---|
| `sub_issue_created` | Sub-issue Created | Sub-issue created in GitHub |
| `sub_issue_creation_failed` | Sub-issue Failed | Sub-issue creation failed |

## System Operations

| Event Type | Display Title | Description |
|---|---|---|
| `execution_state_reconciled` | State Reconciled | Execution state reconciled after recovery |
| `status_validation_failure` | Validation Failure | Status validation check failed |
| `result_persistence_failed` | Persistence Failed | Agent result could not be persisted |
| `fallback_storage_used` | Fallback Storage | Fallback storage mechanism used for results |
| `output_validation_failed` | Output Validation Failed | Agent output failed validation |
| `empty_output_detected` | Empty Output | Agent produced empty output |
| `container_result_recovered` | Result Recovered | Container result recovered from fallback storage |
