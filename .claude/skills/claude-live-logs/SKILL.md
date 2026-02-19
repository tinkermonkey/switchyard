---
name: claude-live-logs
description: Search and analyze Claude Code live execution logs captured in Elasticsearch - tool calls, tool results, agent output, and thinking for a specific task or pipeline run
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
| `claude-streams-*` | **Live Claude Code execution**: tool calls, tool results, text output, agent thinking | `task_id`, `pipeline_run_id`, `agent_name` |
| `agent-events-*` | Agent lifecycle: initialized, started, completed, failed | `task_id`, `pipeline_run_id`, `agent_name` |
| `decision-events-*` | Orchestrator routing, pipeline progression, errors | `task_id`, `pipeline_run_id`, `agent` |
| `pipeline-runs-*` | Pipeline run metadata (issue, project, status, duration) | `id` (= `pipeline_run_id`) |

### claude-streams-* Field Schema

| Field | Type | Description |
|---|---|---|
| `timestamp` | date | When the event occurred |
| `agent_name` | keyword | Agent that produced this event (e.g. `senior_software_engineer`) |
| `project` | keyword | Project name |
| `task_id` | keyword | Unique task execution ID — primary lookup key |
| `pipeline_run_id` | keyword | Parent pipeline run ID — groups multiple agent executions |
| `event_category` | keyword | Category: `tool_call`, `tool_result`, `claude_stream`, `agent_output`, `agent_thinking`, `other` |
| `event_type` | keyword | Type: `tool_call`, `tool_result`, `assistant_message`, `text_output`, `thinking`, `user_message`, `unknown` |
| `tool_name` | keyword | Name of tool called (Bash, Read, Write, Edit, Glob, Grep, Task, etc.) |
| `tool_params` | object | Tool input parameters (stored, not indexed — use `tool_params_text` to search) |
| `tool_params_text` | text | Searchable string version of tool parameters |
| `success` | boolean | Whether the tool call/result succeeded |
| `error_message` | text | Error details if `success=false` |
| `raw_event` | object | Complete Claude Code event from the stream (not indexed, for reference) |

### event_category Values

- `tool_call` — Claude invoked a tool (Bash, Read, Write, Edit, Glob, Grep, Task, etc.)
- `tool_result` — Tool returned a result back to Claude
- `agent_output` / `claude_stream` — Claude's text output or streaming message
- `agent_thinking` — Claude's extended thinking content
- `other` — Unknown or unclassified events

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

### Step 3: Get All Live Log Events (Chronological)

Full event stream for the execution:
```bash
curl -s "http://localhost:9200/claude-streams-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {"term": {"task_id": "<TASK_ID>"}},
  "sort": [{"timestamp": "asc"}],
  "size": 500
}' | jq '.hits.hits[]._source | {timestamp, event_category, event_type, tool_name, success, error_message}'
```

> Note: If a single execution has >500 events (long-running agents), paginate with `"from": 500`.

---

### Step 4: Get Tool Call Summary

Count tool calls by type:
```bash
curl -s "http://localhost:9200/claude-streams-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"task_id": "<TASK_ID>"}},
        {"term": {"event_category": "tool_call"}}
      ]
    }
  },
  "size": 0,
  "aggs": {
    "by_tool": {
      "terms": {"field": "tool_name", "size": 20}
    }
  }
}' | jq '.aggregations.by_tool.buckets[] | {tool: .key, count: .doc_count}'
```

Get tool calls with their parameters:
```bash
curl -s "http://localhost:9200/claude-streams-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"task_id": "<TASK_ID>"}},
        {"term": {"event_category": "tool_call"}}
      ]
    }
  },
  "sort": [{"timestamp": "asc"}],
  "size": 200
}' | jq '.hits.hits[]._source | {timestamp, tool_name, tool_params_text, success}'
```

---

### Step 5: Get Failed Tool Calls

```bash
curl -s "http://localhost:9200/claude-streams-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"task_id": "<TASK_ID>"}},
        {"term": {"success": false}}
      ]
    }
  },
  "sort": [{"timestamp": "asc"}],
  "size": 100
}' | jq '.hits.hits[]._source | {timestamp, event_category, tool_name, error_message, tool_params_text}'
```

---

### Step 6: Read Agent Text Output and Thinking

Get Claude's text output (what it communicated or its reasoning):
```bash
curl -s "http://localhost:9200/claude-streams-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"task_id": "<TASK_ID>"}},
        {"terms": {"event_category": ["agent_output", "agent_thinking"]}}
      ]
    }
  },
  "sort": [{"timestamp": "asc"}],
  "size": 100
}' | jq '.hits.hits[]._source | {timestamp, event_type, event_category}'
```

> The full text content is in `raw_event` (not indexed). To retrieve it, use `| .raw_event` in the jq filter.

---

### Step 7: Search Tool Params for Specific Content

Find Bash commands containing a specific string:
```bash
curl -s "http://localhost:9200/claude-streams-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"task_id": "<TASK_ID>"}},
        {"match": {"tool_params_text": "<search_term>"}}
      ]
    }
  },
  "sort": [{"timestamp": "asc"}],
  "size": 50
}' | jq '.hits.hits[]._source | {timestamp, tool_name, tool_params_text}'
```

Find all Bash tool calls:
```bash
curl -s "http://localhost:9200/claude-streams-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"task_id": "<TASK_ID>"}},
        {"term": {"tool_name": "Bash"}}
      ]
    }
  },
  "sort": [{"timestamp": "asc"}],
  "size": 100
}' | jq '.hits.hits[]._source | {timestamp, tool_params_text, success, error_message}'
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
curl -s "http://localhost:9200/claude-streams-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"success": false}},
        {"range": {"timestamp": {"gte": "now-<TIME_WINDOW>"}}}
      ]
    }
  },
  "sort": [{"timestamp": "desc"}],
  "size": 50
}' | jq '.hits.hits[]._source | {timestamp, agent_name, project, task_id, event_category, tool_name, error_message}'
```

### Error Rate by Tool
```bash
curl -s "http://localhost:9200/claude-streams-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"event_category": "tool_result"}},
        {"range": {"timestamp": {"gte": "now-<TIME_WINDOW>"}}}
      ]
    }
  },
  "size": 0,
  "aggs": {
    "by_tool": {
      "terms": {"field": "tool_name", "size": 20},
      "aggs": {
        "error_rate": {"avg": {"script": {"source": "doc['"'"'success'"'"'].value ? 0 : 1"}}}
      }
    }
  }
}' | jq '.aggregations.by_tool.buckets[] | {tool: .key, count: .doc_count}'
```

---

## Data Flow Reference

Understanding how live logs are captured:

```
Claude Code process (in Docker container)
  ↓ JSON events on stdout (tool_call, tool_result, assistant, user, etc.)
docker-claude-wrapper.py
  ↓ Wraps each event with {agent, task_id, project, issue_number, timestamp, event: <raw>}
  ↓ Writes to Redis Stream: orchestrator:claude_logs_stream (maxlen=500, 2h TTL)
  ↓ Publishes to Redis Pub/Sub: orchestrator:claude_stream (real-time UI)
log_collector.py (consumer group: log_collector)
  ↓ Reads from Redis Stream in batches
  ↓ Calls enrich_claude_log() → extracts event_category, tool_name, success, etc.
  ↓ Routes by event_category to correct ES index
Elasticsearch: claude-streams-YYYY-MM-DD
```

Also note: `ObservabilityManager.emit_claude_stream_event()` in `monitoring/observability.py` is an alternative direct write path to `claude-streams-*` used by certain code paths (bypasses Redis, goes direct to ES with `event_type: 'claude_stream'`, `event_category: 'claude_stream'`).

### raw_event Structure

The `raw_event` field stores the original Claude Code JSON event. Key shapes:
- **Tool call**: `{type: "assistant", message: {content: [{type: "tool_use", name: "Bash", input: {command: "..."}}]}}`
- **Tool result**: `{type: "user", message: {content: [{type: "tool_result", tool_use_id: "...", is_error: false, content: "..."}]}}`
- **Text output**: `{type: "assistant", message: {content: [{type: "text", text: "..."}]}}`
- **Thinking**: `{type: "assistant", message: {content: [{type: "thinking", thinking: "..."}]}}`

---

## Common Failure Patterns

| Pattern | What to look for |
|---|---|
| **Agent stuck/infinite loop** | Many repeated `tool_call` events for same tool with same params; no progress in `agent_output` |
| **Test fix failures** | Repeated Bash tool calls with `pytest` / `npm test`; `success: false` on tool_result; repair cycle iteration count in `decision-events-*` |
| **Git push rejected** | Bash tool calls containing `git push`; `error_message` containing `rejected` or `conflict` |
| **Context length exceeded** | `error_message` containing `context` or `token`; agent_failed event shortly after |
| **File not found** | `success: false` on Read/Glob/Grep tool results; `error_message` containing `not found` or `no such file` |
| **Permission errors** | `success: false` on Write/Edit; `error_message` containing `permission denied` |
| **No tool calls at all** | Only `claude_stream` or `agent_output` events; Claude may be confused or outputting plain text |

---

## Useful Combinations

### Full execution audit (task_id to complete narrative)
```bash
# 1. Lifecycle
curl -s "http://localhost:9200/agent-events-*/_search" -H 'Content-Type: application/json' \
  -d '{"query":{"term":{"task_id":"<TASK_ID>"}},"sort":[{"timestamp":"asc"}],"size":10}' | \
  jq '.hits.hits[]._source | {timestamp, event_type, duration_ms, success}'

# 2. Tool call summary
curl -s "http://localhost:9200/claude-streams-*/_search" -H 'Content-Type: application/json' \
  -d '{"query":{"bool":{"must":[{"term":{"task_id":"<TASK_ID>"}},{"term":{"event_category":"tool_call"}}]}},"size":0,"aggs":{"by_tool":{"terms":{"field":"tool_name","size":20}}}}' | \
  jq '.aggregations.by_tool.buckets[]'

# 3. Failures only
curl -s "http://localhost:9200/claude-streams-*/_search" -H 'Content-Type: application/json' \
  -d '{"query":{"bool":{"must":[{"term":{"task_id":"<TASK_ID>"}},{"term":{"success":false}}]}},"sort":[{"timestamp":"asc"}],"size":50}' | \
  jq '.hits.hits[]._source | {timestamp, tool_name, error_message}'
```

### Correlate pipeline run with live logs
```bash
# Step 1: Get all task_ids for a pipeline run
TASK_IDS=$(curl -s "http://localhost:9200/agent-events-*/_search" -H 'Content-Type: application/json' \
  -d '{"query":{"bool":{"must":[{"term":{"pipeline_run_id":"<RUN_ID>"}},{"term":{"event_type":"agent_initialized"}}]}},"size":20}' | \
  jq -r '[.hits.hits[]._source.task_id] | join(" ")') && echo "$TASK_IDS"

# Step 2: For each task_id, query claude-streams-*
# (Use each task_id from above output)
```

### Check total event count before paginating
```bash
curl -s "http://localhost:9200/claude-streams-*/_count" -H 'Content-Type: application/json' \
  -d '{"query":{"term":{"task_id":"<TASK_ID>"}}}' | jq '.count'
```

### List all live indices
```bash
curl -s "http://localhost:9200/_cat/indices?v&s=index" | grep -E "(claude-streams|agent-events|decision-events|pipeline-runs)"
```
