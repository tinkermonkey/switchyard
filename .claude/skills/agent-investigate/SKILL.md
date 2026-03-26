---
name: agent-investigate
description: Deep-dive into agent execution - Docker logs, Claude stream events, tool calls, errors
user_invocable: true
args: "<container_name|task_id|--recent [time_window]>"
---

# Agent Execution Investigation

You are investigating agent execution(s). The user's argument is: `$ARGUMENTS`.

Determine the mode based on the argument:
- **Specific execution**: A container name (e.g., `claude-agent-myproject-abc123`), task ID, or `pipeline_run_id + agent_name`
- **Recent activity**: `--recent` optionally followed by a time window like `1h`, `6h`, `12h`, `24h` (default: `1h`)

---

## Mode A: Specific Execution

### Step 1: Identify the Execution

If given a **container name**, extract project and task_id from it (pattern: `claude-agent-{project}-{task_id}`).

If given a **task_id**, query for agent events:
```bash
curl -s "http://localhost:9200/agent-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {"term": {"task_id": "<TASK_ID>"}},
  "sort": [{"timestamp": "asc"}],
  "size": 20
}' | jq '.hits.hits[]._source | {timestamp, agent_name, event_type, duration_ms, success, error_message, pipeline_run_id}'
```

If given a **pipeline_run_id + agent_name**, find the specific execution:
```bash
curl -s "http://localhost:9200/agent-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"pipeline_run_id": "<RUN_ID>"}},
        {"term": {"agent_name": "<AGENT_NAME>"}}
      ]
    }
  },
  "sort": [{"timestamp": "asc"}],
  "size": 20
}' | jq '.hits.hits[]._source'
```

### Step 2: Get Docker Logs

```bash
docker logs claude-agent-<project>-<task_id> 2>&1
```

If container no longer exists, note it. Check if it's still running:
```bash
docker ps --filter "name=claude-agent-<project>-<task_id>" --format "{{.Names}}\t{{.Status}}\t{{.RunningFor}}"
```

### Step 3: Query OTEL Execution Events

```bash
curl -s "http://localhost:9200/logs-claude.otel-default/_search" -H 'Content-Type: application/json' -d '{
  "query": {"term": {"resource.attributes.task_id.keyword": "<TASK_ID>"}},
  "sort": [{"@timestamp": "asc"}],
  "size": 500
}' | jq '.hits.hits[]._source | {"@timestamp", event_name, "tool_name": .attributes.tool_name, "success": .attributes.success, "error": .attributes.error, "params": .attributes.tool_parameters}'
```

### Step 4: Analyze Tool Calls

From the stream events, build a breakdown:
- **By type**: Count of Bash, Read, Write, Edit, Glob, Grep tool calls
- **Success rate**: How many succeeded vs failed
- **Error patterns**: Group error messages to find recurring issues

Look for:
- Git failures (merge conflicts, push rejections)
- Test failures (recurring test names)
- Rate limit errors
- Context length / token limit errors
- File not found errors
- Permission errors

### Step 5: Get Agent Lifecycle Timing

```bash
curl -s "http://localhost:9200/agent-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {"term": {"task_id": "<TASK_ID>"}},
  "sort": [{"timestamp": "asc"}],
  "size": 10
}' | jq '.hits.hits[]._source | {timestamp, event_type, duration_ms, success, error_message}'
```

Build timeline: `agent_initialized` → `agent_started` → `agent_completed` or `agent_failed`.

### Step 6: Build Execution Narrative

Synthesize findings into a narrative:
1. What agent ran, for what project/issue
2. How long it ran
3. What it did (tool call summary)
4. Whether it succeeded or failed
5. If failed: root cause and recommendations

---

## Mode B: Recent Activity

### Step 1: Find Recent Agent Executions

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
  "size": 50
}' | jq '.hits.hits[]._source | {timestamp, agent_name, project, task_id, pipeline_run_id}'
```

### Step 2: List Active Containers

```bash
docker ps --filter "name=claude-agent-" --format "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}\t{{.Image}}"
```

### Step 3: Cross-reference with Completion/Failure Events

```bash
curl -s "http://localhost:9200/agent-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"terms": {"event_type": ["agent_completed", "agent_failed"]}},
        {"range": {"timestamp": {"gte": "now-<TIME_WINDOW>"}}}
      ]
    }
  },
  "sort": [{"timestamp": "desc"}],
  "size": 50
}' | jq '.hits.hits[]._source | {timestamp, agent_name, project, task_id, event_type, duration_ms, success, error_message}'
```

### Step 4: Build Summary Table

| Time | Agent | Project | Duration | Status | Container | Error |
|------|-------|---------|----------|--------|-----------|-------|
| ... | ... | ... | ... | ... | ... | ... |

### Step 5: Deep-dive into Failures

For any failed executions, run Mode A steps 2-6 to get details.

---

## Common Failure Patterns

- **Duration > 1800s without completion**: Agent likely stuck or in infinite loop. Check Docker logs for repeating patterns.
- **`success: false` with no `error_message`**: Container crashed. Check `docker logs` for OOM or signal.
- **Multiple tool call failures for same file**: Agent confused about codebase structure. Check if workspace was properly mounted.
- **Git push failures**: Branch protection rules, merge conflicts, or stale refs. Check `branch_conflict_detected` events.
- **Test run producing same failures repeatedly**: Agent not understanding test output. Check repair cycle iteration count.
