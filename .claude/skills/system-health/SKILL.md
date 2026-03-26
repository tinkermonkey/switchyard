---
name: system-health
description: Comprehensive system health check - all components, queue, circuit breakers, active work, errors
user_invocable: true
---

# System Health Check

You are performing a comprehensive health check of the orchestrator system. Execute all checks below using the Bash tool, then synthesize into a structured health report.

## Step 1: Core Health Endpoint

```bash
curl -s http://localhost:5001/health | jq .
```

Reports status of: Redis, GitHub authentication, Docker socket.

## Step 2: Active Agents

Check both the observability API and actual Docker containers:

```bash
curl -s http://localhost:5001/agents/active | jq .
```

```bash
docker ps --filter "name=claude-agent-" --format "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}\t{{.Image}}"
```

Compare the two - if containers exist but aren't tracked by the API, there may be orphaned containers.

## Step 3: Task Queue Health

```bash
docker-compose exec orchestrator python scripts/inspect_task_health.py --json
```

If the script is unavailable, check Redis directly:
```bash
docker-compose exec redis redis-cli LLEN tasks:critical && docker-compose exec redis redis-cli LLEN tasks:high && docker-compose exec redis redis-cli LLEN tasks:medium && docker-compose exec redis redis-cli LLEN tasks:low
```

## Step 4: Circuit Breakers

```bash
docker-compose exec orchestrator python scripts/inspect_circuit_breakers.py
```

Also check API:
```bash
curl -s http://localhost:5001/api/circuit-breakers | jq .
```

Any open circuit breakers indicate repeated failures for a specific agent/project combination.

## Step 5: Active Pipeline Runs

```bash
curl -s http://localhost:5001/active-pipeline-runs | jq .
```

## Step 6: Recent Errors (Last 1 Hour)

```bash
curl -s "http://localhost:9200/decision-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"terms": {"event_type": ["error_encountered", "agent_failed", "circuit_breaker_opened", "pipeline_run_failed", "status_progression_failed", "container_execution_failed", "result_persistence_failed", "empty_output_detected"]}},
        {"range": {"timestamp": {"gte": "now-1h"}}}
      ]
    }
  },
  "sort": [{"timestamp": "desc"}],
  "size": 50
}' | jq '.hits.hits[]._source | {timestamp, event_type, agent, project, data}'
```

Also check agent-events for failures:
```bash
curl -s "http://localhost:9200/agent-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"event_type": "agent_failed"}},
        {"range": {"timestamp": {"gte": "now-1h"}}}
      ]
    }
  },
  "sort": [{"timestamp": "desc"}],
  "size": 20
}' | jq '.hits.hits[]._source | {timestamp, agent_name, project, task_id, error_message, duration_ms}'
```

## Step 7: Docker Infrastructure

```bash
docker-compose ps
```

```bash
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}" | head -20
```

## Step 8: Elasticsearch Index Health

```bash
curl -s "http://localhost:9200/_cat/indices?v&s=index" | grep -E "(logs-claude|metrics-claude|decision-events|agent-events|pipeline-runs|agent-logs|task-metrics)"
```

Check for red/yellow indices or unexpectedly large doc counts.

## Synthesize Health Report

Present findings as a structured report:

```
## System Health Report

### Component Status
| Component | Status | Details |
|-----------|--------|---------|
| Redis | ... | ... |
| GitHub | ... | ... |
| Docker | ... | ... |
| Elasticsearch | ... | ... |
| Orchestrator | ... | ... |

### Active Work
- Pipeline runs: ...
- Active agents: ...
- Queued tasks: ...

### Issues Detected
1. ...
2. ...

### Recommendations
1. ...
2. ...
```

Flag any of these conditions:
- **Open circuit breakers**: Repeated agent failures need investigation
- **Orphaned containers**: Docker containers not tracked by orchestrator
- **Queue backlog**: Tasks waiting longer than expected
- **Recent error spike**: Multiple errors in short window
- **Red ES indices**: Data integrity issues
- **High memory/CPU**: Resource pressure on containers
- **Stale pipeline runs**: Active runs with no recent events (>30 min)
