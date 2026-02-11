---
name: pipeline-investigate
description: Investigate a pipeline run - timeline, Docker logs, decision events, root cause analysis
user_invocable: true
args: "<pipeline_run_id>"
---

# Pipeline Run Investigation

You are investigating a pipeline run. Follow these steps systematically, using the Bash tool to execute commands against the running orchestrator stack. The user has provided a pipeline run ID as the argument: `$ARGUMENTS`.

If no run ID was provided, find recent pipeline runs first:
```bash
curl -s "http://localhost:9200/pipeline-runs-*/_search" -H 'Content-Type: application/json' -d '{"query":{"match_all":{}},"sort":[{"started_at":"desc"}],"size":10}' | jq '.hits.hits[]._source | {id, project, board, status, issue_number, issue_title, started_at, ended_at}'
```
Then ask the user which run to investigate.

## Step 1: Get Pipeline Run Metadata

```bash
curl -s "http://localhost:9200/pipeline-runs-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {"term": {"id": "<RUN_ID>"}},
  "size": 1
}' | jq '.hits.hits[]._source'
```

Extract: `project`, `board`, `status`, `issue_number`, `issue_title`, `started_at`, `ended_at`, `duration_ms`.

## Step 2: Run Pipeline Timeline Script

```bash
docker-compose exec orchestrator python scripts/inspect_pipeline_timeline.py <RUN_ID> --json
```

This gives the pre-built timeline view. If the script fails or the container isn't running, proceed with manual queries.

## Step 3: Query Decision Events (Chronological)

```bash
curl -s "http://localhost:9200/decision-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {"term": {"pipeline_run_id": "<RUN_ID>"}},
  "sort": [{"timestamp": "asc"}],
  "size": 500
}' | jq '.hits.hits[]._source | {timestamp, event_type, agent, project, data}'
```

Look for the overall flow: `pipeline_run_started` → stage transitions → review/repair cycles → completion or failure.

## Step 4: Query Agent Lifecycle Events

```bash
curl -s "http://localhost:9200/agent-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {"term": {"pipeline_run_id": "<RUN_ID>"}},
  "sort": [{"timestamp": "asc"}],
  "size": 100
}' | jq '.hits.hits[]._source | {timestamp, agent_name, event_type, task_id, duration_ms, success, error_message}'
```

From `agent_initialized` events, extract container names (pattern: `claude-agent-{project}-{task_id}`).

## Step 5: Get Docker Logs for Each Container

For each container found in step 4:
```bash
docker logs claude-agent-<project>-<task_id> 2>&1 | tail -500
```

If the container no longer exists, note it and move on.

## Step 6: If Failures Found, Query Claude Stream Events

```bash
curl -s "http://localhost:9200/claude-streams-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"pipeline_run_id": "<RUN_ID>"}},
        {"terms": {"event_category": ["tool_call", "tool_result", "agent_output"]}}
      ]
    }
  },
  "sort": [{"timestamp": "asc"}],
  "size": 200
}' | jq '.hits.hits[]._source | {timestamp, agent_name, event_type, event_category, tool_name, success, error_message}'
```

## Step 7: Construct Timeline and Synthesize

Build a markdown timeline table:

| Time | Event | Agent | Details |
|------|-------|-------|---------|
| ... | ... | ... | ... |

Then provide root cause analysis and recommendations.

## Interpretation Heuristics

When analyzing events, apply these patterns:

- **exit_code=137**: SIGKILL - likely OOM killed or manually terminated
- **exit_code=1**: Process error - check Docker logs for stack traces
- **`review_cycle_escalated`**: Max review iterations hit (blocking threshold exceeded). Check `iteration` count and reviewer feedback.
- **`empty_output_detected`**: Claude produced no output. Check for API errors, token limit issues, or prompt too large.
- **`circuit_breaker_opened`**: Repeated failures triggered circuit breaker. Check preceding `error_encountered` events.
- **`result_persistence_failed`**: Container output couldn't be saved. Check for filesystem issues or container crash.
- **Gap > 5 minutes between events**: Potential stall. Check if container was still running (`docker ps`), or if the orchestrator was blocked.
- **Repair cycle > 20 iterations**: Tests likely unfixable by agent. Check test output patterns for recurring failures. May need manual review.
- **`status_progression_failed`**: GitHub API issue or board state mismatch. Check `error_message` in event data.
- **`branch_conflict_detected`**: Git merge conflict. Check which files conflict and whether auto-resolution was attempted.
- **Multiple `retry_attempted` events**: Transient failures. Check if the retries eventually succeeded or exhausted.
