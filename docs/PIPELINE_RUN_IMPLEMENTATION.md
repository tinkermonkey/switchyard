# Pipeline Run Implementation Guide

## Overview

Pipeline runs provide end-to-end traceability of an issue's journey through the workflow. Every observability event and agent log is now tagged with a `pipeline_run_id`, enabling complete reconstruction of what happened during an issue's lifecycle.

## What is a Pipeline Run?

A **pipeline run** is a simple data object that tracks:
- `id`: Unique identifier (UUID)
- `issue_number`: GitHub issue number
- `issue_title`: Issue title
- `issue_url`: Issue URL  
- `project`: Project name
- `board`: Board name
- `started_at`: When the run started (ISO timestamp)
- `ended_at`: When the run ended (ISO timestamp, null if active)
- `status`: "active" or "completed"

## Lifecycle

### 1. Creation
A pipeline run is created when:
- The orchestrator is about to launch an agent for an issue
- There is no active pipeline run for that issue

### 2. Active State
While active, a pipeline run:
- Cannot be reused (one active run per issue at a time)
- Tags all events (decision events, agent logs, live logs)
- Is stored in Redis for fast lookup
- Is persisted to Elasticsearch for historical analysis

### 3. Completion
A pipeline run ends when:
- The issue moves to a column with no agent defined
- The orchestrator detects this on any board column transition
- The orchestrator finds it on restart (future enhancement)

Once ended, a pipeline run:
- Cannot be reused
- Remains in Elasticsearch for analysis
- Is removed from Redis active mapping after 1 hour

## Implementation Details

### Core Service: `services/pipeline_run.py`

**PipelineRun Class**
```python
@dataclass
class PipelineRun:
    id: str
    issue_number: int
    issue_title: str
    issue_url: str
    project: str
    board: str
    started_at: str
    ended_at: Optional[str] = None
    status: str = "active"
```

**PipelineRunManager Class**

Key methods:
- `create_pipeline_run()`: Create new run
- `get_active_pipeline_run()`: Get active run for an issue
- `get_or_create_pipeline_run()`: Get existing or create new
- `end_pipeline_run()`: End an active run
- `get_pipeline_run_by_id()`: Lookup by ID

Storage:
- **Redis**: Fast active run lookups
  - Key: `orchestrator:pipeline_run:{run_id}`
  - Mapping: `orchestrator:pipeline_run:issue_mapping` (hash: `{project}:{issue}` → `run_id`)
  - TTL: 2 hours for active, 1 hour after completion
- **Elasticsearch**: Historical persistence
  - Index: `pipeline-runs`
  - Indexed on create and update

### Integration Points

#### 1. Project Monitor (`services/project_monitor.py`)

**Initialization**
```python
from services.pipeline_run import get_pipeline_run_manager
self.pipeline_run_manager = get_pipeline_run_manager()
```

**Pipeline Run Creation** (before agent launch)
```python
# Get or create pipeline run early so we can tag all events
issue_data_early = self.get_issue_details(repository, issue_number, org)
pipeline_run = self.pipeline_run_manager.get_or_create_pipeline_run(
    issue_number=issue_number,
    issue_title=issue_data_early.get('title', f'Issue #{issue_number}'),
    issue_url=issue_data_early.get('url', ''),
    project=project_name,
    board=board_name
)
```

**Pipeline Run Completion** (column with no agent)
```python
# End active pipeline run (issue has reached end of pipeline)
ended = self.pipeline_run_manager.end_pipeline_run(
    project=project_name,
    issue_number=issue_number,
    reason=f"Issue moved to column '{status}' with no agent"
)
```

**Task Context**
```python
task_context = {
    ...
    'pipeline_run_id': pipeline_run.id,  # Include pipeline run ID
    ...
}
```

#### 2. Observability Events (`monitoring/observability.py`)

**Updated emit() method**
```python
def emit(self, event_type: EventType, agent: str, task_id: str,
         project: str, data: Dict[str, Any], pipeline_run_id: Optional[str] = None):
    if pipeline_run_id:
        data['pipeline_run_id'] = pipeline_run_id
    ...
```

**Updated event methods**
```python
def emit_task_received(self, agent: str, task_id: str, project: str,
                      context: Dict[str, Any], pipeline_run_id: Optional[str] = None):
    ...
```

#### 3. Decision Events (`monitoring/decision_events.py`)

Key methods updated to accept `pipeline_run_id`:
- `emit_agent_routing_decision()`
- `emit_task_queued()`
- All other decision event methods (pattern established)

**Example**
```python
self.decision_events.emit_agent_routing_decision(
    issue_number=issue_number,
    project=project_name,
    board=board_name,
    current_status=status,
    selected_agent=agent,
    reason=f"Status '{status}' maps to agent '{agent}'",
    alternatives=alternative_agents,
    workspace_type=workspace_type,
    pipeline_run_id=pipeline_run.id  # ← Tagged with pipeline run
)
```

### Elasticsearch Schema Updates

#### 1. New Index: `pipeline-runs`

**Mapping** (`services/elasticsearch_pattern_indices.py`)
```python
PIPELINE_RUNS_MAPPING = {
    "mappings": {
        "properties": {
            "id": {"type": "keyword"},
            "issue_number": {"type": "integer"},
            "issue_title": {"type": "text", "fields": {"keyword": {...}}},
            "issue_url": {"type": "keyword"},
            "project": {"type": "keyword"},
            "board": {"type": "keyword"},
            "started_at": {"type": "date"},
            "ended_at": {"type": "date"},
            "status": {"type": "keyword"},
            "duration_ms": {"type": "long"}
        }
    }
}
```

#### 2. Updated Existing Indexes

All agent event indexes now include:
```python
"pipeline_run_id": {"type": "keyword"}
```

Updated indexes:
- `AGENT_LOGS_MAPPING` - Live agent logs
- `AGENT_EVENTS_MAPPING` - Agent lifecycle events
- `CLAUDE_STREAMS_MAPPING` - Claude streaming logs

File: `services/pattern_detection_schema.py`

## Usage Examples

### Query Events by Pipeline Run

**Elasticsearch Query**
```json
{
  "query": {
    "term": {
      "pipeline_run_id": "550e8400-e29b-41d4-a716-446655440000"
    }
  },
  "sort": [
    {"timestamp": "asc"}
  ]
}
```

This returns all events (decision events, agent logs, tool calls, etc.) for a single pipeline run, in chronological order.

### Query Pipeline Runs

**Get all active runs**
```json
{
  "query": {
    "term": {
      "status": "active"
    }
  }
}
```

**Get runs for an issue**
```json
{
  "query": {
    "bool": {
      "must": [
        {"term": {"project": "context-studio"}},
        {"term": {"issue_number": 42}}
      ]
    }
  },
  "sort": [
    {"started_at": "desc"}
  ]
}
```

**Get completed runs with duration**
```json
{
  "query": {
    "term": {
      "status": "completed"
    }
  },
  "sort": [
    {"duration_ms": "desc"}
  ]
}
```

### Programmatic Access

```python
from services.pipeline_run import get_pipeline_run_manager

manager = get_pipeline_run_manager()

# Get active run for an issue
run = manager.get_active_pipeline_run(
    project="context-studio",
    issue_number=42
)

if run:
    print(f"Active run: {run.id}")
    print(f"Started: {run.started_at}")
    print(f"Issue: {run.issue_title}")
```

## Benefits

### 1. Complete Traceability
Every event related to an issue is now linked via `pipeline_run_id`. You can:
- See exactly what happened during an issue's workflow
- Trace from decision to execution to completion
- Debug failures with full context

### 2. Performance Analysis
With pipeline runs, you can:
- Calculate time-in-pipeline per issue
- Identify bottlenecks (stages that take longest)
- Compare pipeline performance across projects
- Track success/failure rates

### 3. Debugging
When something goes wrong:
```
1. Find the issue number
2. Query for pipeline_run_id
3. Query all events with that pipeline_run_id
4. See the complete story in chronological order
```

### 4. Observability Dashboard
The web UI can now show:
- Active pipeline runs (live issues being worked)
- Pipeline run history per issue
- Event timeline for a pipeline run
- Performance metrics per pipeline

## Future Enhancements

### 1. Automatic Recovery on Restart
- On orchestrator startup, scan all active issues
- End pipeline runs for issues in "no agent" columns
- Resume pipeline runs for issues in columns with agents

### 2. Pipeline Run Metrics
- Average duration by project/board
- Success rate tracking
- Stage transition times
- Agent handoff delays

### 3. Pipeline Run Annotations
- Add custom metadata to runs (tags, notes)
- Link to external systems (Jira, etc.)
- Track human interventions

### 4. Pipeline Run Visualization
- Swimlane view of issue journey
- Gantt chart of agent work
- Dependency graphs between issues

## Testing

### Manual Testing

1. **Create a pipeline run**
```python
from services.pipeline_run import get_pipeline_run_manager

manager = get_pipeline_run_manager()
run = manager.create_pipeline_run(
    issue_number=123,
    issue_title="Test Issue",
    issue_url="https://github.com/org/repo/issues/123",
    project="test-project",
    board="test-board"
)
print(f"Created: {run.id}")
```

2. **Move issue through workflow**
- Trigger agent for the issue
- Observe events tagged with pipeline_run_id
- Check Redis and Elasticsearch

3. **End the pipeline run**
```python
ended = manager.end_pipeline_run(
    project="test-project",
    issue_number=123
)
print(f"Ended: {ended}")
```

4. **Query events**
```bash
curl -X GET "localhost:9200/agent-events-*/_search?pretty" -H 'Content-Type: application/json' -d'
{
  "query": {
    "term": {
      "pipeline_run_id": "YOUR_RUN_ID"
    }
  }
}
'
```

### Integration Testing

1. Start orchestrator with updated code
2. Create a test issue in a project
3. Move issue to first agent column
4. Verify pipeline run created in logs
5. Move issue through multiple columns
6. Verify all events have same pipeline_run_id
7. Move issue to final column (no agent)
8. Verify pipeline run ended
9. Query Elasticsearch to see complete history

## Deployment

### 1. Update Elasticsearch Indices

The indices will be automatically created on next service start, but you can manually create them:

```bash
docker compose exec orchestrator python -c "
from elasticsearch import Elasticsearch
from services.elasticsearch_pattern_indices import create_all_indices

es = Elasticsearch(['http://elasticsearch:9200'])
create_all_indices(es)
print('Indices created')
"
```

### 2. Restart Services

```bash
docker compose restart orchestrator pattern-ingestion
```

### 3. Monitor Logs

```bash
docker compose logs -f orchestrator | grep -i "pipeline run"
```

You should see:
- "Using pipeline run {id} for issue #{number}"
- "Creating task with pipeline run {id} for issue #{number}"
- "Ended pipeline run {id} for {project} issue #{number}"

### 4. Verify in Elasticsearch

```bash
# Check pipeline-runs index exists
curl -X GET "localhost:9200/pipeline-runs?pretty"

# Query pipeline runs
curl -X GET "localhost:9200/pipeline-runs/_search?pretty"

# Check events have pipeline_run_id
curl -X GET "localhost:9200/agent-events-*/_search?pretty" -H 'Content-Type: application/json' -d'
{
  "query": {"exists": {"field": "pipeline_run_id"}},
  "size": 5
}
'
```

## Troubleshooting

### Pipeline runs not being created

Check:
1. Issue details are being fetched successfully
2. `pipeline_run_manager` is initialized in ProjectMonitor
3. No exceptions in logs during `get_or_create_pipeline_run()`

### Events not tagged with pipeline_run_id

Check:
1. Task context includes `pipeline_run_id`
2. Events are being emitted with `pipeline_run_id` parameter
3. Agent is reading `pipeline_run_id` from context and passing to events

### Pipeline runs not ending

Check:
1. Issue is moving to a column with `agent: null` or no agent defined
2. `end_pipeline_run()` is being called in `trigger_agent_for_status()`
3. Redis mapping is being cleaned up

### Elasticsearch not persisting runs

Check:
1. Elasticsearch is running and accessible
2. `pipeline-runs` index exists and has correct mapping
3. No exceptions in logs during `_persist_to_elasticsearch()`

## Migration Notes

### Backward Compatibility

- All changes are backward compatible
- `pipeline_run_id` is optional in all event methods
- Existing code without `pipeline_run_id` will continue to work
- Events without `pipeline_run_id` are still valid

### Gradual Rollout

1. Deploy pipeline_run.py (defines data model)
2. Deploy Elasticsearch schema updates (adds indexes)
3. Deploy monitoring updates (adds pipeline_run_id parameter)
4. Deploy project_monitor updates (uses pipeline runs)
5. Deploy agent updates (propagates pipeline_run_id)

Each step is independent and safe.

## Monitoring

### Key Metrics

- **Active pipeline runs**: Current issues being worked
- **Pipeline run duration**: Average time from start to end
- **Pipeline run success rate**: Completed vs. failed
- **Events per pipeline run**: Average number of events

### Alerts

Consider alerting on:
- Pipeline runs active > 24 hours (stuck issues)
- Pipeline runs without events (broken tagging)
- High failure rate (systemic issues)
- Excessive duration (performance problems)

## Summary

Pipeline runs provide the missing link in observability:
- ✅ End-to-end issue traceability
- ✅ Complete event history per issue
- ✅ Performance analysis capability
- ✅ Simplified debugging workflow
- ✅ Foundation for advanced analytics

All observability events and agent logs are now part of a traceable journey through the workflow pipeline.
