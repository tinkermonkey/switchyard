---
name: orchestrator-ref
description: Quick reference for Elasticsearch indices, event types, Docker patterns, and pipeline flows
user_invocable: true
---

# Orchestrator Reference

You are providing reference data for the Claude Code Agent Orchestrator. Print the requested sections or all sections if no specific topic is requested.

## Elasticsearch Indices

6 indices, all date-partitioned (`*-YYYY-MM-DD`), 7-day retention via ILM, accessible at `localhost:9200`.

| Index Pattern | Purpose | Key Fields | Source |
|---|---|---|---|
| `decision-events-*` | Orchestrator decisions: routing, progression, review/repair cycles, errors, queue ops, branch mgmt | `timestamp`, `event_type`, `event_category`, `agent`, `task_id`, `project`, `pipeline_run_id`, `decision_category`, `selected_agent`, `from_status`, `to_status`, `iteration`, `feedback_source` | `monitoring/observability.py` |
| `agent-events-*` | Agent lifecycle: initialized, started, completed, failed | `timestamp`, `agent_name`, `project`, `task_id`, `event_type`, `event_category`, `duration_ms`, `context_tokens`, `success`, `error_message`, `issue_number`, `board`, `pipeline_type`, `pipeline_run_id` | `services/pattern_detection_schema.py` |
| `logs-claude.otel-default` | Claude Code execution: tool results, API requests, API errors — OTEL data stream | `@timestamp`, `resource.attributes.task_id`, `resource.attributes.pipeline_run_id`, `resource.attributes.agent`, `event_name`, `attributes.tool_name`, `attributes.success`, `attributes.tool_parameters` | OTEL collector → ES |
| `pipeline-runs-*` | Pipeline run tracking | `id`, `issue_number`, `issue_title`, `issue_url`, `project`, `board`, `started_at`, `ended_at`, `status`, `duration_ms` | `services/pattern_detection_schema.py` |
| `agent-logs-*` | Legacy combined agent logs (tool calls, results, all events) | `timestamp`, `session_id`, `agent_name`, `project`, `task_id`, `event_type`, `event_category`, `tool_name`, `duration_ms`, `success`, `error_message`, `pipeline_run_id` | `services/pattern_detection_schema.py` |
| `orchestrator-task-metrics-*` | Task execution metrics | `@timestamp`, `agent`, `duration`, `success` | `monitoring/` |

## Event Type Taxonomy

Source: `monitoring/observability.py` `EventType` enum.

### Lifecycle Events (non-ES, Redis pub/sub only)
- `task_received`, `agent_initialized`, `agent_started`, `agent_completed`, `agent_failed`
- `prompt_constructed`, `claude_api_call_started`, `claude_api_call_completed`, `claude_api_call_failed`
- `container_launch_started`, `container_launch_succeeded`, `container_launch_failed`
- `container_execution_started`, `container_execution_completed`, `container_execution_failed`
- `response_chunk_received`, `response_processing_started`, `response_processing_completed`
- `tool_execution_started`, `tool_execution_completed`
- `performance_metric`, `token_usage`

Note: `agent_initialized`, `agent_started`, `agent_completed`, `agent_failed` also go to `agent-events-*` index.

### Decision Events (indexed in `decision-events-*`)
**Feedback Monitoring**: `feedback_detected`, `feedback_listening_started`, `feedback_listening_stopped`, `feedback_ignored`
**Agent Routing**: `agent_routing_decision`, `agent_selected`, `workspace_routing_decision`
**Pipeline Progression**: `status_progression_started`, `status_progression_completed`, `status_progression_failed`, `pipeline_stage_transition`
**Pipeline Runs**: `pipeline_run_started`, `pipeline_run_completed`, `pipeline_run_failed`
**Review Cycles**: `review_cycle_started`, `review_cycle_iteration`, `review_cycle_maker_selected`, `review_cycle_reviewer_selected`, `review_cycle_escalated`, `review_cycle_completed`
**Repair Cycles**: `repair_cycle_started`, `repair_cycle_iteration`, `repair_cycle_test_cycle_started/completed`, `repair_cycle_test_execution_started/completed`, `repair_cycle_fix_cycle_started/completed`, `repair_cycle_file_fix_started/completed/failed`, `repair_cycle_warning_review_started/completed/failed`, `repair_cycle_completed`
**Repair Containers**: `repair_cycle_container_started`, `repair_cycle_container_checkpoint_updated`, `repair_cycle_container_recovered`, `repair_cycle_container_killed`, `repair_cycle_container_completed`
**Conversational**: `conversational_loop_started`, `conversational_question_routed`, `conversational_loop_paused`, `conversational_loop_resumed`
**Errors**: `error_encountered`, `error_recovered`, `circuit_breaker_opened`, `circuit_breaker_closed`, `retry_attempted`
**Result Persistence**: `result_persistence_failed`, `fallback_storage_used`, `output_validation_failed`, `empty_output_detected`, `container_result_recovered`
**Task Queue**: `task_queued`, `task_dequeued`, `task_priority_changed`, `task_cancelled`
**Branch Management**: `branch_selected`, `branch_created`, `branch_reused`, `branch_conflict_detected`, `branch_stale_detected`, `branch_selection_escalated`

## Docker Container Patterns

- **Naming**: `claude-agent-{project}-{task_id}` (sanitized, see `claude/docker_runner.py:340`)
- **List active agents**: `docker ps --filter "name=claude-agent-"`
- **Get logs**: `docker logs <container_name> 2>&1`
- **Redis tracking**: Container state tracked via Redis keys
- **Network**: Containers share Docker network with orchestrator stack

## Pipeline Flows

### sdlc_execution (sub-issues)
```
Development (senior_software_engineer)
  → Code Review (review cycle: senior_software_engineer + code_reviewer, max 5 iterations, escalate at 1 blocking)
  → Testing (repair cycle: senior_software_engineer, max 100 agent calls, checkpoint every 5)
  → Staged (senior_software_engineer)
```
Board columns: Backlog → Development → Code Review → Testing → Staged → Done

### planning_design (epics)
```
Research (idea_researcher, conversational)
  → Requirements (business_analyst, conversational)
  → Design (software_architect, conversational)
  → Work Breakdown (work_breakdown_agent, conversational)
  → In Development (tracking)
  → In Review (pr_review_agent)
  → Documentation (technical_writer)
  → Documentation Review (documentation_editor, review cycle max 3 iterations)
```

### environment_support
```
In Progress (dev_environment_setup)
  → Verification (dev_environment_verifier)
```

## Common Queries

### Find a pipeline run by ID
```bash
curl -s "http://localhost:9200/pipeline-runs-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {"term": {"id": "<RUN_ID>"}},
  "size": 1
}' | jq '.hits.hits[]._source'
```

### Get all decision events for a pipeline run (chronological)
```bash
curl -s "http://localhost:9200/decision-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {"term": {"pipeline_run_id": "<RUN_ID>"}},
  "sort": [{"timestamp": "asc"}],
  "size": 500
}' | jq '.hits.hits[]._source'
```

### Get agent lifecycle events for a pipeline run
```bash
curl -s "http://localhost:9200/agent-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {"term": {"pipeline_run_id": "<RUN_ID>"}},
  "sort": [{"timestamp": "asc"}],
  "size": 100
}' | jq '.hits.hits[]._source'
```

### Get OTEL execution events for a task
```bash
curl -s "http://localhost:9200/logs-claude.otel-default/_search" -H 'Content-Type: application/json' -d '{
  "query": {"term": {"resource.attributes.task_id.keyword": "<TASK_ID>"}},
  "sort": [{"@timestamp": "asc"}],
  "size": 500
}' | jq '.hits.hits[]._source | {"@timestamp", event_name, "tool_name": .attributes.tool_name, "success": .attributes.success}'
```

### Find recent errors (last 1 hour)
```bash
curl -s "http://localhost:9200/decision-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"terms": {"event_type": ["error_encountered", "agent_failed", "circuit_breaker_opened", "pipeline_run_failed"]}},
        {"range": {"timestamp": {"gte": "now-1h"}}}
      ]
    }
  },
  "sort": [{"timestamp": "desc"}],
  "size": 50
}' | jq '.hits.hits[]._source'
```

### Find recent pipeline runs
```bash
curl -s "http://localhost:9200/pipeline-runs-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {"match_all": {}},
  "sort": [{"started_at": "desc"}],
  "size": 10
}' | jq '.hits.hits[]._source'
```

### Find containers for a pipeline run
```bash
curl -s "http://localhost:9200/agent-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"pipeline_run_id": "<RUN_ID>"}},
        {"term": {"event_type": "agent_initialized"}}
      ]
    }
  },
  "size": 50
}' | jq '.hits.hits[]._source | {agent_name, task_id, timestamp}'
```

### List all ES orchestrator indices
```bash
curl -s "http://localhost:9200/_cat/indices?v&s=index" | grep -E "(logs-claude|metrics-claude|decision-events|agent-events|pipeline-runs|agent-logs|task-metrics)"
```

## Access Points

| Service | URL | Notes |
|---|---|---|
| Elasticsearch | `localhost:9200` | Direct curl queries |
| Observability API | `localhost:5001` | Health, agents, history, pipeline runs |
| Redis | `localhost:6379` | Task queue, pub/sub events |
| Web UI | `localhost:3000` | Visual dashboard |

## Diagnostic Scripts

All run via `docker-compose exec orchestrator python scripts/<script>`:
- `inspect_pipeline_timeline.py <RUN_ID> [--verbose] [--json]` - Pipeline execution timeline
- `inspect_task_health.py [--show-all] [--project <name>] [--json]` - Task queue health
- `inspect_checkpoint.py [<RUN_ID>] [--verify-recovery]` - Pipeline checkpoint state
- `inspect_circuit_breakers.py` - Circuit breaker states
