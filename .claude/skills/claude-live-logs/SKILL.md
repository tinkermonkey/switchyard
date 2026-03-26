---
name: claude-live-logs
description: Search and analyze Claude Code live execution logs captured in Elasticsearch - tool calls, tool results, API usage, and errors for a specific task or pipeline run
user_invocable: true
args: "<task_id|pipeline_run_id|agent_name> [--recent [time_window]]"
---

# Claude Live Log Analysis

You are analyzing Claude Code live execution logs from Elasticsearch. The user's argument is: `$ARGUMENTS`.

Determine mode:
- **Specific execution**: A `task_id`, `pipeline_run_id`, or `agent_name` (e.g. `senior_software_engineer`)
- **Recent activity**: `--recent` optionally followed by a time window like `1h`, `6h`, `24h` (default: `1h`)

---

## Index Reference

All log data lives in Elasticsearch at `localhost:9200`. All indices are date-partitioned (`*-YYYY-MM-DD`) with 7-day ILM retention.

| Index | Content | Primary Keys |
|---|---|---|
| `logs-claude.otel-default` | **Live Claude Code execution**: tool results, API requests, API errors — from OTEL collector | `resource.attributes.task_id`, `resource.attributes.pipeline_run_id`, `resource.attributes.agent` |
| `agent-events-*` | Agent lifecycle: initialized, started, completed, failed; container launch/execution events; prompt_constructed | `task_id`, `pipeline_run_id`, `agent_name` |
| `decision-events-*` | Orchestrator routing, pipeline progression, errors | `task_id`, `pipeline_run_id`, `agent` |
| `pipeline-runs-*` | Pipeline run metadata (issue, project, status, duration) | `id` (= `pipeline_run_id`) |

### logs-claude.otel-default Field Schema

| Field | Type | Description |
|---|---|---|
| `@timestamp` | date | When the event occurred |
| `resource.attributes.task_id.keyword` | keyword | Task execution ID — primary lookup key (use `.keyword` for term queries) |
| `resource.attributes.pipeline_run_id.keyword` | keyword | Parent pipeline run ID |
| `resource.attributes.agent.keyword` | keyword | Agent name (e.g. `senior_software_engineer`) |
| `resource.attributes.project.keyword` | keyword | Project name |
| `event_name.keyword` | keyword | OTEL event name: `claude_code.tool_result`, `claude_code.api_request`, `claude_code.api_error`, `claude_code.api_rate_limit_error` |
| `attributes.tool_name.keyword` | keyword | Tool name for `tool_result` events (Bash, Read, Write, Edit, Glob, Grep, Task, etc.) |
| `attributes.success` | boolean | Whether tool call succeeded |
| `attributes.duration_ms` | float | Tool execution duration |
| `attributes.tool_parameters` | object | Tool input parameters |
| `attributes.input_tokens.keyword` | keyword | Input tokens (string — use script agg to sum) |
| `attributes.output_tokens.keyword` | keyword | Output tokens (string) |
| `attributes.cost_usd` | float | API call cost in USD |
| `attributes.message.keyword` | keyword | Error message for `api_error` events |

> **Note**: Full text output and agent thinking are captured in real-time via Redis pub/sub (`orchestrator:claude_stream`) but are not persisted to Elasticsearch. Only tool calls, API calls, and errors are indexed in OTEL.

---

## Mode A: Specific Task or Pipeline Run

### Step 1: Identify What You Have

**If given a `task_id`**, go directly to Step 2.

**If given a `pipeline_run_id`**, find all task_ids for that run:
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
  "sort": [{"timestamp": "asc"}],
  "size": 50
}' | jq '.hits.hits[]._source | {agent_name, task_id, timestamp, pipeline_run_id}'
```

**If given an `agent_name`**, find recent executions:
```bash
curl -s "http://localhost:9200/agent-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"agent_name": "<AGENT_NAME>"}},
        {"term": {"event_type": "agent_initialized"}},
        {"range": {"timestamp": {"gte": "now-24h"}}}
      ]
    }
  },
  "sort": [{"timestamp": "desc"}],
  "size": 20
}' | jq '.hits.hits[]._source | {agent_name, task_id, timestamp, pipeline_run_id, project}'
```

---

### Step 2: Get Agent Lifecycle Summary

```bash
curl -s "http://localhost:9200/agent-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {"term": {"task_id": "<TASK_ID>"}},
  "sort": [{"timestamp": "asc"}],
  "size": 20
}' | jq '.hits.hits[]._source | {timestamp, event_type, agent_name, duration_ms, success, error_message, pipeline_run_id}'
```

Build timeline: `agent_initialized` → `agent_started` → `agent_completed` or `agent_failed`.

---

### Step 3: Get All OTEL Events (Chronological)

Full event stream for the execution from OTEL:
```bash
curl -s "http://localhost:9200/logs-claude.otel-default/_search" -H 'Content-Type: application/json' -d '{
  "query": {"term": {"resource.attributes.task_id.keyword": "<TASK_ID>"}},
  "sort": [{"@timestamp": "asc"}],
  "size": 500
}' | jq '.hits.hits[]._source | {"@timestamp", event_name, "tool_name": .attributes.tool_name, "success": .attributes.success, "error": .attributes.message}'
```

> Note: If a single execution has >500 events, paginate with `"from": 500`.

---

### Step 4: Get Tool Call Summary

Count tool calls by type:
```bash
curl -s "http://localhost:9200/logs-claude.otel-default/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"resource.attributes.task_id.keyword": "<TASK_ID>"}},
        {"term": {"event_name.keyword": "claude_code.tool_result"}}
      ]
    }
  },
  "size": 0,
  "aggs": {
    "by_tool": {
      "terms": {"field": "attributes.tool_name.keyword", "size": 20}
    }
  }
}' | jq '.aggregations.by_tool.buckets[] | {tool: .key, count: .doc_count}'
```

Get tool calls with their parameters:
```bash
curl -s "http://localhost:9200/logs-claude.otel-default/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"resource.attributes.task_id.keyword": "<TASK_ID>"}},
        {"term": {"event_name.keyword": "claude_code.tool_result"}}
      ]
    }
  },
  "sort": [{"@timestamp": "asc"}],
  "size": 200
}' | jq '.hits.hits[]._source | {"@timestamp", "tool_name": .attributes.tool_name, "params": .attributes.tool_parameters, "success": .attributes.success, "duration_ms": .attributes.duration_ms}'
```

---

### Step 5: Get Failed Tool Calls

```bash
curl -s "http://localhost:9200/logs-claude.otel-default/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"resource.attributes.task_id.keyword": "<TASK_ID>"}},
        {"term": {"event_name.keyword": "claude_code.tool_result"}},
        {"term": {"attributes.success": false}}
      ]
    }
  },
  "sort": [{"@timestamp": "asc"}],
  "size": 100
}' | jq '.hits.hits[]._source | {"@timestamp", "tool_name": .attributes.tool_name, "error": .attributes.error, "params": .attributes.tool_parameters}'
```

---

### Step 6: Get API Usage and Token Counts

```bash
curl -s "http://localhost:9200/logs-claude.otel-default/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"resource.attributes.task_id.keyword": "<TASK_ID>"}},
        {"term": {"event_name.keyword": "claude_code.api_request"}}
      ]
    }
  },
  "sort": [{"@timestamp": "asc"}],
  "size": 100
}' | jq '.hits.hits[]._source | {"@timestamp", "input_tokens": .attributes.input_tokens, "output_tokens": .attributes.output_tokens, "cost_usd": .attributes.cost_usd, "model": .attributes.model}'
```

---

### Step 7: Get API Errors

```bash
curl -s "http://localhost:9200/logs-claude.otel-default/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"resource.attributes.task_id.keyword": "<TASK_ID>"}},
        {"terms": {"event_name.keyword": ["claude_code.api_error", "claude_code.api_rate_limit_error"]}}
      ]
    }
  },
  "sort": [{"@timestamp": "asc"}],
  "size": 50
}' | jq '.hits.hits[]._source | {"@timestamp", event_name, "message": .attributes.message, "code": .attributes.code}'
```

---

## Mode B: Recent Activity

### Find Recent Agent Executions
```bash
curl -s "http://localhost:9200/agent-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"event_type": "agent_initialized"}},
        {"range": {"timestamp": {"gte": "now-<TIME_WINDOW>"}}}
      ]
    }
  },
  "sort": [{"timestamp": "desc"}],
  "size": 20
}' | jq '.hits.hits[]._source | {timestamp, agent_name, project, task_id, pipeline_run_id}'
```

### Find Recent Failures
```bash
curl -s "http://localhost:9200/logs-claude.otel-default/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"event_name.keyword": "claude_code.tool_result"}},
        {"term": {"attributes.success": false}},
        {"range": {"@timestamp": {"gte": "now-<TIME_WINDOW>"}}}
      ]
    }
  },
  "sort": [{"@timestamp": "desc"}],
  "size": 50
}' | jq '.hits.hits[]._source | {"@timestamp", "agent": .resource.attributes.agent, "project": .resource.attributes.project, "task_id": .resource.attributes.task_id, "tool_name": .attributes.tool_name, "error": .attributes.error}'
```

### Error Rate by Tool
```bash
curl -s "http://localhost:9200/logs-claude.otel-default/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"event_name.keyword": "claude_code.tool_result"}},
        {"range": {"@timestamp": {"gte": "now-<TIME_WINDOW>"}}}
      ]
    }
  },
  "size": 0,
  "aggs": {
    "by_tool": {
      "terms": {"field": "attributes.tool_name.keyword", "size": 20},
      "aggs": {
        "error_rate": {"avg": {"script": {"source": "doc['"'"'attributes.success'"'"'].value ? 0 : 1"}}}
      }
    }
  }
}' | jq '.aggregations.by_tool.buckets[] | {tool: .key, count: .doc_count}'
```

---

## Data Flow Reference

```
Claude Code process (in Docker container)
  ↓ OTEL SDK emits structured log events (tool_result, api_request, api_error, etc.)
OTEL collector (otel-collector service)
  ↓ Batches and exports to Elasticsearch
Elasticsearch: logs-claude.otel-default

docker-claude-wrapper.py
  ↓ Also publishes each JSON line to Redis Pub/Sub: orchestrator:claude_stream (real-time UI only)
  → NOT persisted to ES — ephemeral pub/sub only
```

---

## Common Failure Patterns

| Pattern | What to look for |
|---|---|
| **Agent stuck/infinite loop** | Many repeated `tool_result` events for same tool with same `tool_parameters`; no progress |
| **Test fix failures** | Repeated Bash tool calls with `pytest` / `npm test`; `attributes.success: false` |
| **Git push rejected** | Bash tool calls with `git push` in `tool_parameters`; error in `attributes.error` |
| **Rate limit errors** | `event_name: claude_code.api_rate_limit_error` events |
| **API errors** | `event_name: claude_code.api_error` with `attributes.code` and `attributes.message` |
| **File not found** | `success: false` on Read/Glob/Grep tool results; error in `attributes.error` |
| **Permission errors** | `success: false` on Write/Edit; `attributes.error` containing `permission denied` |

---

## Useful Combinations

### Full execution audit (task_id to complete narrative)
```bash
# 1. Lifecycle
curl -s "http://localhost:9200/agent-events-*/_search" -H 'Content-Type: application/json' \
  -d '{"query":{"term":{"task_id":"<TASK_ID>"}},"sort":[{"timestamp":"asc"}],"size":10}' | \
  jq '.hits.hits[]._source | {timestamp, event_type, duration_ms, success}'

# 2. Tool call summary
curl -s "http://localhost:9200/logs-claude.otel-default/_search" -H 'Content-Type: application/json' \
  -d '{"query":{"bool":{"must":[{"term":{"resource.attributes.task_id.keyword":"<TASK_ID>"}},{"term":{"event_name.keyword":"claude_code.tool_result"}}]}},"size":0,"aggs":{"by_tool":{"terms":{"field":"attributes.tool_name.keyword","size":20}}}}' | \
  jq '.aggregations.by_tool.buckets[]'

# 3. Failures only
curl -s "http://localhost:9200/logs-claude.otel-default/_search" -H 'Content-Type: application/json' \
  -d '{"query":{"bool":{"must":[{"term":{"resource.attributes.task_id.keyword":"<TASK_ID>"}},{"term":{"event_name.keyword":"claude_code.tool_result"}},{"term":{"attributes.success":false}}]}},"sort":[{"@timestamp":"asc"}],"size":50}' | \
  jq '.hits.hits[]._source | {"@timestamp", "tool_name": .attributes.tool_name, "error": .attributes.error}'
```

### Correlate pipeline run with live logs
```bash
# Step 1: Get all task_ids for a pipeline run
TASK_IDS=$(curl -s "http://localhost:9200/agent-events-*/_search" -H 'Content-Type: application/json' \
  -d '{"query":{"bool":{"must":[{"term":{"pipeline_run_id":"<RUN_ID>"}},{"term":{"event_type":"agent_initialized"}}]}},"size":20}' | \
  jq -r '[.hits.hits[]._source.task_id] | join(" ")') && echo "$TASK_IDS"

# Step 2: For each task_id, query logs-claude.otel-default
# (Use each task_id from above output)
```

### Check total event count before paginating
```bash
curl -s "http://localhost:9200/logs-claude.otel-default/_count" -H 'Content-Type: application/json' \
  -d '{"query":{"term":{"resource.attributes.task_id.keyword":"<TASK_ID>"}}}' | jq '.count'
```

### List all live indices
```bash
curl -s "http://localhost:9200/_cat/indices?v&s=index" | grep -E "(logs-claude|metrics-claude|agent-events|decision-events|pipeline-runs)"
```
