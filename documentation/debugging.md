# Orchestrator debugging guide

This guide covers how to diagnose and resolve problems with the switchyard orchestrator. All commands assume you are running from the `switchyard/` directory unless stated otherwise. Commands prefixed with `docker-compose exec orchestrator` run inside the orchestrator container.

---

## Table of contents

1. [Observability endpoints](#1-observability-endpoints)
2. [Diagnostic scripts](#2-diagnostic-scripts)
3. [Common failure scenarios](#3-common-failure-scenarios)
4. [Reading the logs](#4-reading-the-logs)
5. [Elasticsearch queries](#5-elasticsearch-queries)
6. [Circuit breakers](#6-circuit-breakers)
7. [State file inspection](#7-state-file-inspection)

---

## 1. Observability endpoints

All endpoints are served on port 5001. The observability server runs as a separate Docker Compose service (`observability-server`), not inside the `orchestrator` container. To view its logs: `docker-compose logs -f observability-server`.

### System health

**`GET /health`**

Returns the current health of all subsystems. Read this first when diagnosing any problem.

```bash
curl -s http://localhost:5001/health | jq .
```

Key fields in the response:

- `status` — one of `healthy`, `degraded`, `unhealthy`, `starting`, `error`
- `orchestrator.checks.redis.healthy` — whether Redis is reachable
- `orchestrator.checks.github.healthy` — whether GitHub API is authenticated
- `orchestrator.checks.github.api_usage.remaining` — remaining GitHub API requests before reset
- `orchestrator.checks.github.circuit_breaker.is_open` — whether GitHub API is blocked
- `orchestrator.checks.claude.circuit_breaker.is_open` — whether Claude Code is blocked
- `subscriber_health.is_running` — whether the Redis pub/sub subscriber is running

A `503` response means the orchestrator has not yet completed its first health check (still starting) or cannot reach Redis.

---

### Active agents

**`GET /agents/active`**

Lists agent containers currently tracked in Redis. Returns container name, agent type, project, task ID, issue number, and start time.

```bash
curl -s http://localhost:5001/agents/active | jq '.agents[]'
```

Use this to confirm an agent is running, or to find the container name before killing it.

---

**`GET /api/active-agents`**

More detailed view that combines Elasticsearch pipeline-run data with Redis container tracking. Returns agents from active and recently-completed pipeline runs. Includes `pipeline_run_id`, `branch_name`, `issue_title`, and `is_containerized`. This is the source of truth used by the web UI.

```bash
curl -s http://localhost:5001/api/active-agents | jq '.agents[]'
```

---

### Killing an agent

**`POST /agents/kill/<container_name>`**

Stops a container and, if the container is linked to a project and issue in Redis, cancels the work via the full cancellation flow.

```bash
curl -s -X POST http://localhost:5001/agents/kill/claude-agent-context-studio-abc123 | jq .
```

---

### Pipeline runs

**`GET /active-pipeline-runs`**

Returns all pipeline runs with `status: active` from Elasticsearch. Includes lock status (`holding_lock`, `waiting_for_lock`) and which issue holds the lock.

```bash
curl -s http://localhost:5001/active-pipeline-runs | jq '.runs[]'
```

---

**`GET /completed-pipeline-runs`**

Returns completed pipeline runs with pagination. Useful for checking recent outcomes.

```bash
# Last 10 completed runs
curl -s "http://localhost:5001/completed-pipeline-runs?limit=10" | jq '.runs[] | {issue_number, project, board, outcome, ended_at}'

# Filter by project
curl -s "http://localhost:5001/completed-pipeline-runs?project=context-studio&limit=20" | jq .

# Filter by outcome
curl -s "http://localhost:5001/completed-pipeline-runs?outcome=failed&limit=10" | jq '.runs[] | {issue_number, project, outcome}'
```

---

**`GET /pipeline-run-events?pipeline_run_id=<id>`**

Returns all events (decision events, agent lifecycle events, Claude stream logs) for a specific pipeline run in chronological order. The most complete view of what happened during a run.

```bash
curl -s "http://localhost:5001/pipeline-run-events?pipeline_run_id=<run_id>" | jq '.events[] | {timestamp, event_type, agent, event_category}'
```

---

**`POST /pipeline-runs/<pipeline_run_id>/kill`**

Terminates an active pipeline run. Ends the run in Redis and Elasticsearch and clears execution state so the issue can be re-triggered.

```bash
curl -s -X POST http://localhost:5001/pipeline-runs/<run_id>/kill | jq .
```

---

### Event history

**`GET /history?count=<n>`**

Returns the last N agent lifecycle events from the Redis event stream, falling back to Elasticsearch if the stream is empty. Capped at 500. Returns events in chronological order.

```bash
curl -s "http://localhost:5001/history?count=50" | jq '.events[] | {timestamp, event_type, agent, project}'
```

---

**`GET /current-pipeline`**

Reconstructs the current (or most recent) pipeline state from the Redis event stream. Returns stage names, statuses (`pending`, `running`, `completed`), and overall progress percentage. This is a derived view, not authoritative state.

```bash
curl -s http://localhost:5001/current-pipeline | jq '.pipeline'
```

---

### Claude logs

**`GET /claude-logs-history?count=<n>&agent=<agent_name>`**

Returns recent Claude streaming logs from the Redis stream, falling back to the `claude-streams-*` Elasticsearch index. Supports filtering by agent name.

```bash
# Recent logs for all agents
curl -s "http://localhost:5001/claude-logs-history?count=50" | jq .

# Logs for a specific agent
curl -s "http://localhost:5001/claude-logs-history?count=100&agent=senior_software_engineer" | jq .
```

---

### Circuit breakers

**`GET /api/circuit-breakers`**

Returns all circuit breaker states: Redis Streams, Elasticsearch Indexing, Pattern Detection, Claude Code Token Limit, GitHub API Rate Limit, and per-agent breakers. See [section 6](#6-circuit-breakers) for details.

```bash
curl -s http://localhost:5001/api/circuit-breakers | jq '.circuit_breakers[] | {name, state, failure_count}'
```

---

**`POST /api/circuit-breakers/claude-code/reset`**
**`POST /api/circuit-breakers/github-api/reset`**
**`POST /api/circuit-breakers/agent/<agent_name>/reset`**

Manually close a circuit breaker. See [section 6](#6-circuit-breakers).

---

### GitHub API status

**`GET /api/github-api-status`**

Returns the full GitHub API client status: rate limit remaining, percentage used, reset time, circuit breaker state, and request statistics (total, failed, rate-limited, backoff multiplier).

```bash
curl -s http://localhost:5001/api/github-api-status | jq '.status'
```

---

### Projects

**`GET /api/projects`**

Returns all configured projects with their dev container status (`verified`, `not_verified`, `error`, `n/a`), workspace path existence, active pipelines, and pipeline lock status.

```bash
curl -s http://localhost:5001/api/projects | jq '.projects[] | {name, "container_status": .dev_container.status}'
```

---

### Pipeline locks

**`GET /api/pipeline-locks`**

Returns all active pipeline locks across all projects. A lock indicates which issue is currently executing in a given pipeline board. Only one issue executes per board at a time.

```bash
curl -s http://localhost:5001/api/pipeline-locks | jq '.locks[]'
```

---

**`POST /api/projects/<project>/pipelines/<board>/release-lock`**

Force-releases a stuck pipeline lock. Use this when an agent has crashed and left a lock that prevents other issues from executing.

```bash
curl -s -X POST "http://localhost:5001/api/projects/context-studio/pipelines/SDLC%20Execution/release-lock" | jq .

# Release lock only if held by a specific issue
curl -s -X POST "http://localhost:5001/api/projects/context-studio/pipelines/SDLC%20Execution/release-lock" \
  -H "Content-Type: application/json" \
  -d '{"issue_number": 42}' | jq .
```

---

### Queue management

**`GET /api/pipeline-queue/<project>/<board>`**

Returns the current queue for a pipeline board: which issue holds the lock, which issues are waiting, and their positions.

```bash
curl -s "http://localhost:5001/api/pipeline-queue/context-studio/SDLC%20Execution" | jq .
```

---

**`POST /api/pipeline-queue/<project>/<board>/refresh`**

Forces the queue to re-sync with the current GitHub board order. Use this after manually reordering cards on the board.

```bash
curl -s -X POST "http://localhost:5001/api/pipeline-queue/context-studio/SDLC%20Execution/refresh" | jq .
```

---

**`GET /api/blocked-issues`**

Returns issues that hold a pipeline lock, had their last execution fail, and have no running container. These require manual intervention (move to Backlog, or close the issue).

```bash
curl -s http://localhost:5001/api/blocked-issues | jq '.blocked_issues[]'
```

---

### State reconciliation

**`POST /api/reconcile-state`**

Triggers immediate reconciliation of Docker container state and pipeline queues. Reconciliation runs asynchronously; check logs for results.

```bash
curl -s -X POST http://localhost:5001/api/reconcile-state \
  -H "Content-Type: application/json" \
  -d '{"docker": true, "queues": true}' | jq .

# Reconcile only a specific project
curl -s -X POST http://localhost:5001/api/reconcile-state \
  -H "Content-Type: application/json" \
  -d '{"project": "context-studio"}' | jq .
```

---

### Feedback loops

**`GET /api/feedback-loops/active`**

Returns all active human feedback loops (conversational Q&A sessions). Each loop has a `health` field: `healthy` (heartbeat < 2 min old), `stale` (2-10 min), `stuck` (> 10 min).

```bash
curl -s http://localhost:5001/api/feedback-loops/active | jq '.active_loops[]'
```

---

### Agent execution detail

**`GET /api/agent-execution/<execution_id>`**

Returns detailed information about a specific agent execution by `execution_id`, including the initialized event, completion or failure event, and associated Claude stream logs.

```bash
curl -s "http://localhost:5001/api/agent-execution/<execution_id>" | jq .
```

---

## 2. Diagnostic scripts

All scripts run inside the orchestrator container. Connect with `docker-compose exec orchestrator bash` or prefix each command with `docker-compose exec orchestrator`.

### inspect_pipeline_timeline.py

Visualizes the complete chronological sequence of events for a pipeline run. Pulls from `decision-events-*` and `agent-events-*` Elasticsearch indices. Prints stage transitions, agent assignments, durations, and review cycle counts.

**When to use:** After a pipeline completes or fails and you want to understand exactly what happened step by step.

```bash
# Human-readable timeline
docker-compose exec orchestrator python scripts/inspect_pipeline_timeline.py <pipeline_run_id>

# Verbose: includes feedback comment text
docker-compose exec orchestrator python scripts/inspect_pipeline_timeline.py <pipeline_run_id> --verbose

# JSON output for scripting
docker-compose exec orchestrator python scripts/inspect_pipeline_timeline.py <pipeline_run_id> --json
```

Key output to look for:
- `AGENT FAILED` entries with `Error:` lines directly below — these show why the stage failed
- The `Summary` section shows total agents invoked, review cycle count, and per-agent durations
- A long duration on a single agent suggests the container ran long before timing out or being killed

---

### inspect_task_health.py

Checks the Redis task queues (`tasks:high`, `tasks:medium`, `tasks:low`) for stuck tasks. A task is considered stuck if it exceeds its age threshold: 30 minutes for high-priority, 1 hour for medium, 4 hours for low. Exit codes: 0 (healthy), 1 (warning: 1-2 stuck), 2 (critical: 3+ stuck).

**When to use:** When issues are not being processed and you suspect the task queue is backed up.

```bash
# Quick health check
docker-compose exec orchestrator python scripts/inspect_task_health.py

# Show all queued tasks, not just stuck ones
docker-compose exec orchestrator python scripts/inspect_task_health.py --show-all

# Filter to a single project
docker-compose exec orchestrator python scripts/inspect_task_health.py --project context-studio

# JSON output (suitable for monitoring)
docker-compose exec orchestrator python scripts/inspect_task_health.py --json

# Use custom thresholds (in seconds)
docker-compose exec orchestrator python scripts/inspect_task_health.py --high-threshold 900
```

Key output to look for:
- `STUCK TASKS DETECTED` with task IDs, agent names, project, and issue numbers
- Distribution by agent — a single agent accumulating all stuck tasks points to a container problem for that agent
- The script prints recommendations when stuck tasks are found

---

### inspect_checkpoint.py

Inspects pipeline recovery checkpoints stored in `orchestrator_data/state/checkpoints/`. Checkpoints are written after each pipeline stage completes and allow the orchestrator to resume from the last completed stage after a crash.

**When to use:** When a pipeline was interrupted and you want to confirm it will resume from the right point, or when you want to see how far a failed pipeline got.

```bash
# List all recent checkpoints
docker-compose exec orchestrator python scripts/inspect_checkpoint.py

# Inspect a specific pipeline
docker-compose exec orchestrator python scripts/inspect_checkpoint.py <pipeline_run_id>

# Show the full context JSON stored in the checkpoint
docker-compose exec orchestrator python scripts/inspect_checkpoint.py <pipeline_run_id> --show-context

# Verify the checkpoint is valid for recovery
docker-compose exec orchestrator python scripts/inspect_checkpoint.py <pipeline_run_id> --verify-recovery
```

Key output to look for:
- `Status: READY` vs `Status: NOT READY` under Recovery Verification
- A `Checkpoint is stale (>24 hours old)` warning means the pipeline will likely restart from the beginning rather than resume
- The stage progression list shows which stages completed (with checkpoints) and which was not reached

---

### inspect_circuit_breakers.py

Reads all circuit breaker state keys from Redis (pattern: `circuit_breaker:*:state`) and prints state, failure count, last failure time, and total failures.

**When to use:** When agents are being rejected before they even start, or when you suspect a specific agent is being blocked.

```bash
docker-compose exec orchestrator python scripts/inspect_circuit_breakers.py
```

Key output to look for:
- `State: open` — this breaker is blocking all requests to that agent
- `Failures:` count close to the threshold (default: 3 failures to open) means the breaker is about to trip

---

### inspect_queue.py

Quick read of the Redis task queues. Prints task IDs, project, agent, and issue number for every queued task.

**When to use:** A fast alternative to `inspect_task_health.py` when you just want to see what is in the queue without health analysis.

```bash
docker-compose exec orchestrator python scripts/inspect_queue.py
```

---

### inspect_run.py

Fetches a single pipeline run record from Redis by run ID.

**When to use:** When you have a run ID and want to quickly see the raw data stored for that run without Elasticsearch.

```bash
docker-compose exec orchestrator python scripts/inspect_run.py <pipeline_run_id>
```

---

### inspect_run_details.py

More comprehensive than `inspect_run.py`. Checks both Redis (active state) and Elasticsearch (history), then prints all decision events for the run with timestamps and failure details.

**When to use:** When you want a compact summary of what decisions were made during a specific run, including which agent failed and what the error was.

```bash
docker-compose exec orchestrator PYTHONPATH=/app python scripts/inspect_run_details.py <pipeline_run_id>
```

---

### debug_redis.py

Looks up a specific pipeline run key in Redis and lists all `orchestrator:pipeline_run:*` keys. Hardcoded run ID at the top must be replaced or a different approach used.

**When to use:** Quick check to see what pipeline runs are currently in Redis (active runs only — completed runs are evicted to Elasticsearch).

```bash
docker-compose exec orchestrator python scripts/debug_redis.py
```

---

### watch_agent_logs.sh

Polls the observability server every 5 seconds. Shows active agents, recent history (last 10), and Claude logs (last 20). Clears the screen when the set of active agents changes.

**When to use:** Live monitoring during an active agent execution.

```bash
./scripts/watch_agent_logs.sh
```

---

### monitor_logs.sh

Follows `docker-compose logs -f` for a service with automatic restart on container exit.

**When to use:** When you want a stable tail of orchestrator logs without `logs -f` dropping on container restart.

```bash
./scripts/monitor_logs.sh orchestrator
./scripts/monitor_logs.sh orchestrator 500   # Show last 500 lines first
```

---

### monitor_github_api.sh

Continuous dashboard showing GitHub API rate limit, circuit breaker state, and request statistics. Refreshes every 30 seconds. Reads from `/api/github-api-status`.

**When to use:** When you suspect GitHub API rate limiting is slowing down or stopping the orchestrator.

```bash
./scripts/monitor_github_api.sh          # Continuous mode
./scripts/monitor_github_api.sh --once   # Show once and exit
```

---

### query_es_logs.sh

Queries `orchestrator-logs-*` in Elasticsearch for log messages matching a pattern. Also queries for container-specific logs.

**When to use:** When you need to search historical logs by keyword without access to docker logs.

```bash
./scripts/query_es_logs.sh "docker_runner" 100
./scripts/query_es_logs.sh "agent_failed"
```

---

### reset_project_state.sh

Stops the orchestrator, backs up and removes the GitHub state files and dev container verification state for a project, removes project Docker images, then restarts the orchestrator. This forces a full re-initialization for the project.

**When to use:** When a project's board state has become corrupted, or when the dev container image needs to be rebuilt from scratch.

```bash
./scripts/reset_project_state.sh context-studio
```

> **Note:** This script backs up state files before removing them (with timestamps), so the operation is reversible. Docker image removal is permanent.

---

## 3. Common failure scenarios

### Orchestrator not picking up issues from the board

The orchestrator polls GitHub boards on a configurable interval (default: 15 seconds per project, with adaptive backoff up to 60 seconds when idle).

**Step 1.** Confirm the orchestrator is running.

```bash
docker-compose ps
```

**Step 2.** Check GitHub authentication.

```bash
docker-compose exec orchestrator gh auth status
```

**Step 3.** Check the GitHub API circuit breaker. If it is open, the orchestrator is not making any GitHub API calls.

```bash
curl -s http://localhost:5001/api/github-api-status | jq '.status.breaker'
```

**Step 4.** Confirm the issue has the correct label for the pipeline you expect (`pipeline:dev` for SDLC execution, `pipeline:epic` for planning).

```bash
gh issue view <issue_number> --repo <org>/<repo> --json labels
```

**Step 5.** Check that the issue is in a trigger column. Look at the board column definitions in `config/foundations/workflows.yaml` to confirm which columns trigger agents.

**Step 6.** Confirm the GitHub board state is current in the state file. If the column IDs are stale the monitor may silently skip the board.

```bash
cat state/projects/<project>/github_state.yaml
```

**Step 7.** Look for polling errors in the logs.

```bash
docker-compose logs orchestrator | grep -i "error\|failed\|exception" | tail -50
```

**Step 8.** If the project state is corrupted, reset it.

```bash
./scripts/reset_project_state.sh <project>
```

---

### Agent container fails to start

**Step 1.** Check active agents and recent history to see what happened.

```bash
curl -s http://localhost:5001/api/active-agents | jq .
curl -s "http://localhost:5001/history?count=20" | jq '.events[] | select(.event_type == "container_launch_failed" or .event_type == "agent_failed")'
```

**Step 2.** Check the dev container status for the project. If the image is not verified, agent containers cannot start.

```bash
curl -s http://localhost:5001/api/projects | jq '.projects[] | {name, "status": .dev_container.status, "image": .dev_container.image_name}'
cat state/dev_containers/<project>.yaml
```

**Step 3.** Verify the Docker image exists.

```bash
docker images | grep <project>
```

**Step 4.** If the image is missing, check whether a `dev_environment_setup` task is queued or has recently run.

```bash
docker-compose exec orchestrator python scripts/inspect_task_health.py --show-all | grep dev_environment
```

**Step 5.** If the image is missing and no setup task is queued, remove the dev container state file and restart the orchestrator to trigger re-verification.

```bash
rm state/dev_containers/<project>.yaml
docker-compose restart orchestrator
```

**Step 6.** If the image exists but the container still fails to start, try launching it manually to see the error.

```bash
docker run --rm -v /workspace/<project>:/workspace <project>-agent:latest /bin/bash -c "echo ok"
```

---

### Agent container exits unexpectedly

**Step 1.** Find the container name from recent history.

```bash
curl -s "http://localhost:5001/history?count=20" | jq '.events[] | select(.event_type == "container_execution_failed") | {agent, container_name: .data.container_name, exit_code: .data.exit_code}'
```

**Step 2.** Check the exit code.

- **Exit code 137** means the container was killed with SIGKILL. This is either an OOM kill (the container exceeded memory limits) or a manual kill via the Web UI or `docker kill`. The orchestrator treats this as a `NonRetryableAgentError` — it will not retry automatically and will retain the pipeline lock.
- **Exit code 143** means the container was terminated with SIGTERM. Same behavior as 137.
- **Exit code 1** means the agent process itself failed. This is retryable.

**Step 3.** For exit code 137, check whether Docker killed the container for OOM.

```bash
docker inspect <container_name> --format '{{.State.OOMKilled}}'
```

**Step 4.** For exit code 1, retrieve the stderr from the orchestrator logs or from the Elasticsearch decision events.

```bash
docker-compose logs orchestrator | grep "exit_code=1\|Agent failed\|agent_failed" | tail -20
```

```bash
curl -s "http://localhost:9200/decision-events-*/_search" \
  -H "Content-Type: application/json" \
  -d '{
    "query": {"term": {"event_type": "agent_failed"}},
    "sort": [{"timestamp": {"order": "desc"}}],
    "size": 5
  }' | jq '.hits.hits[]._source | {timestamp, agent, error}'
```

**Step 5.** If exit code 137 was an OOM kill, the agent's task is too large for the default container memory. Check whether the task can be scoped down, or increase the Docker memory limit.

**Step 6.** After a 137/143 exit, the pipeline lock will be retained. Release it manually once you have addressed the root cause.

```bash
curl -s -X POST "http://localhost:5001/api/projects/<project>/pipelines/<board>/release-lock" | jq .
```

---

### Pipeline stuck / not progressing

**Step 1.** Check for blocked issues (failed agent with active lock, no running container).

```bash
curl -s http://localhost:5001/api/blocked-issues | jq '.blocked_issues[]'
```

**Step 2.** Check the pipeline lock state.

```bash
curl -s http://localhost:5001/api/pipeline-locks | jq .
```

**Step 3.** Check for stuck tasks in the queue.

```bash
docker-compose exec orchestrator python scripts/inspect_task_health.py
```

**Step 4.** If there is a lock with no running container and no queued task, the lock is orphaned. Release it.

```bash
curl -s -X POST "http://localhost:5001/api/projects/<project>/pipelines/<board>/release-lock" | jq .
```

**Step 5.** If no lock exists but the issue is not being picked up, trigger state reconciliation.

```bash
curl -s -X POST http://localhost:5001/api/reconcile-state \
  -H "Content-Type: application/json" \
  -d '{"docker": true, "queues": true}' | jq .
```

**Step 6.** Inspect the checkpoint state to see whether the pipeline crashed mid-run and whether recovery is viable.

```bash
docker-compose exec orchestrator python scripts/inspect_checkpoint.py <pipeline_run_id> --verify-recovery
```

---

### GitHub API rate limit exceeded

The GitHub API allows 5,000 requests per hour for a PAT, or higher limits for a GitHub App. When the rate limit is exhausted, the GitHub API circuit breaker opens and all GitHub operations stop until the limit resets.

**Step 1.** Check current rate limit and reset time.

```bash
curl -s http://localhost:5001/api/github-api-status | jq '.status.rate_limit'
```

**Step 2.** Monitor with the dashboard script.

```bash
./scripts/monitor_github_api.sh --once
```

**Step 3.** Check when the limit resets. The orchestrator will automatically close the breaker when the reset time passes (`check_and_close()` runs each poll cycle).

**Step 4.** If you need to resume immediately and have confirmed the limit has reset (remaining > 0), reset the circuit breaker manually.

```bash
curl -s -X POST http://localhost:5001/api/circuit-breakers/github-api/reset | jq .
```

**Step 5.** If this is a recurring problem, switch from a PAT to a GitHub App installation, which receives 5,000 requests per hour per installation in addition to higher secondary rate limits.

---

### Redis connection failure

The orchestrator uses Redis for task queues, circuit breaker state, event streaming, agent container tracking, and pipeline lock state. An in-memory fallback activates for the task queue only; everything else requires Redis.

**Step 1.** Check Redis container status.

```bash
docker-compose ps redis
```

**Step 2.** Test connectivity.

```bash
docker-compose exec orchestrator redis-cli -h redis ping
```

**Step 3.** Check whether the health endpoint reports the Redis failure.

```bash
curl -s http://localhost:5001/health | jq '.orchestrator.checks.redis'
```

**Step 4.** If Redis is down, restart it.

```bash
docker-compose restart redis
```

**Step 5.** After Redis is back up, restart the orchestrator to restore all Redis-backed state.

```bash
docker-compose restart orchestrator
```

**Step 6.** Check whether any circuit breaker states are missing (they would have been reset when Redis restarted). Re-check open breakers.

```bash
curl -s http://localhost:5001/api/circuit-breakers | jq '.circuit_breakers[] | select(.state == "open")'
```

---

### Elasticsearch indexing failures

Elasticsearch stores decision events, agent events, pipeline run records, Claude stream logs, task metrics, and quality metrics. Indexing failures do not stop agent execution — the observability manager uses a retry buffer (`es:failed_events` in Redis) and retries with exponential backoff (2s, 4s, 8s, 16s, 30s, up to 5 attempts).

**Step 1.** Check whether Elasticsearch is reachable.

```bash
curl -s http://localhost:9200/_cluster/health | jq '{status, number_of_nodes}'
```

**Step 2.** Check the Elasticsearch indexing circuit breaker.

```bash
curl -s http://localhost:5001/api/circuit-breakers | jq '.circuit_breakers[] | select(.name == "Elasticsearch Indexing")'
```

**Step 3.** Check the size of the ES backup buffer in Redis (events that failed to index).

```bash
docker-compose exec orchestrator redis-cli -h redis llen es:failed_events
```

**Step 4.** If the backup buffer is large and Elasticsearch is healthy, drain it by calling the health endpoint (which triggers a drain attempt).

```bash
curl -s http://localhost:5001/health > /dev/null
```

**Step 5.** Check for indexing errors in the orchestrator logs.

```bash
docker-compose logs orchestrator | grep -i "elasticsearch\|ES index\|failed_events" | tail -30
```

**Step 6.** If an index has grown too large, clean up old indices.

```bash
./scripts/cleanup_old_indices.sh
```

---

### Docker image build failure for a project

Each project requires a `Dockerfile.agent` at its workspace root. The `dev_environment_setup` agent creates this file. The `dev_environment_verifier` agent builds the image and marks it as verified in `state/dev_containers/<project>.yaml`.

**Step 1.** Check the dev container state.

```bash
cat state/dev_containers/<project>.yaml
```

If `status: error`, the `error_message` field contains the failure reason.

**Step 2.** Check whether the `Dockerfile.agent` exists in the project workspace.

```bash
ls /workspace/<project>/Dockerfile.agent
```

**Step 3.** Try building the image manually to see the full error output.

```bash
docker build -f /workspace/<project>/Dockerfile.agent -t <project>-agent:latest /workspace/<project>
```

**Step 4.** Common causes:
- Missing base image — the `Dockerfile.agent` references an image that does not exist or requires authentication
- Network failure during `apt-get` or `pip install` — transient; retry the build
- Syntax error in `Dockerfile.agent` — the `dev_environment_setup` agent produced an invalid file; check the GitHub issue comment for the agent's output

**Step 5.** After fixing the `Dockerfile.agent`, remove the stale state and let the verifier re-run.

```bash
rm state/dev_containers/<project>.yaml
docker-compose exec orchestrator redis-cli -h redis publish orchestrator:commands '{"command": "verify_dev_container", "project": "<project>"}'
```

Or restart the orchestrator to trigger automatic verification on startup.

```bash
docker-compose restart orchestrator
```

---

## 4. Reading the logs

### Log format

The orchestrator uses Python's standard logging module with this format:

```
%(asctime)s - %(name)s - %(levelname)s - %(message)s
```

Example:

```
2026-03-15 14:32:11,043 - docker_runner - INFO - Agent completed: senior_software_engineer, exit_code=0
```

Fields:
- **asctime** — local time of the log entry
- **name** — the Python logger name, which maps to the module or service (`docker_runner`, `project_monitor`, `pipeline.orchestrator`, etc.)
- **levelname** — `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`
- **message** — the log message

The log level is controlled by the `LOG_LEVEL` environment variable (default: `INFO`). Third-party libraries (`elasticsearch`, `urllib3`, `docker`, `werkzeug`) are suppressed to `WARNING` to reduce noise.

### Following logs

```bash
# Follow orchestrator logs
docker-compose logs -f orchestrator

# Follow from a specific number of lines back
docker-compose logs -f --tail 200 orchestrator

# Use the monitor script for stable following (auto-restarts on container exit)
./scripts/monitor_logs.sh orchestrator
```

### Filtering by level or module

```bash
# Show only errors and criticals
docker-compose logs orchestrator | grep -E " ERROR | CRITICAL "

# Show logs from a specific module
docker-compose logs orchestrator | grep " - docker_runner - "
docker-compose logs orchestrator | grep " - project_monitor - "
docker-compose logs orchestrator | grep " - pipeline.orchestrator - "

# Combine: errors from a specific module
docker-compose logs orchestrator | grep " - docker_runner - " | grep " ERROR "
```

### Correlating a pipeline run across logs

Every pipeline run has a `pipeline_run_id` (a UUID). This ID appears in Elasticsearch events and in log messages that reference agent execution. The steps to correlate a run:

**Step 1.** Find the pipeline run ID. If you know the issue number:

```bash
curl -s "http://localhost:9200/pipeline-runs-*/_search" \
  -H "Content-Type: application/json" \
  -d '{"query": {"term": {"issue_number": <issue_number>}}, "sort": [{"started_at": "desc"}], "size": 5}' \
  | jq '.hits.hits[]._source | {id, issue_number, status, started_at}'
```

**Step 2.** Use the timeline script to get all events for the run.

```bash
docker-compose exec orchestrator python scripts/inspect_pipeline_timeline.py <pipeline_run_id>
```

**Step 3.** Find the container name from the `agent_initialized` events, then get raw Docker logs.

```bash
# Container names follow the pattern: claude-agent-<project>-<short_task_id>
docker logs claude-agent-<project>-<short_id> 2>&1 | tail -100
```

**Step 4.** Cross-reference timestamps from the timeline against orchestrator log entries.

```bash
# Get orchestrator log lines within a time window
docker-compose logs orchestrator | grep "14:30\|14:31\|14:32"
```

### Key log patterns to recognize

| Pattern | Meaning |
|---|---|
| `Agent completed: <agent>, exit_code=0` | Successful completion |
| `Agent failed in container (exit_code=<n>)` | Container exited with an error |
| `Container <name> was terminated by signal (exit_code=137)` | OOM kill or manual kill |
| `Circuit breaker <name> opened` | A breaker has tripped; requests to that service are blocked |
| `Rate limit breaker opened` | GitHub API is exhausted |
| `Error connecting to Redis` | Redis is unreachable |
| `STUCK - Exceeds age threshold` | Task has been in queue too long |
| `Releasing pipeline lock` | A pipeline completed and released the lock |
| `Pipeline lock is held by issue` | The pipeline is waiting for another issue to finish |

---

## 5. Elasticsearch queries

All queries target `http://localhost:9200` (from outside the container) or `http://elasticsearch:9200` (from inside the orchestrator container).

### Key indices

| Index pattern | Contents |
|---|---|
| `decision-events-YYYY.MM.DD` | Orchestrator decisions: agent routing, status progressions, review cycles, circuit breaker events |
| `agent-events-YYYY.MM.DD` | Agent lifecycle: `agent_initialized`, `agent_completed`, `agent_failed`, token usage, container launch events |
| `pipeline-runs-YYYY.MM.DD` | Pipeline run records with status, issue number, start/end times, outcome |
| `claude-streams-YYYY.MM.DD` | Claude streaming output logs |
| `orchestrator-task-metrics-YYYY.MM.DD` | Task execution metrics (agent, duration, success) |
| `orchestrator-quality-metrics-YYYY.MM.DD` | Quality scores per agent and metric |

All date-partitioned indices are queried with wildcards (e.g., `decision-events-*`).

### Finding all events for a pipeline run

```bash
curl -s "http://localhost:9200/decision-events-*/_search" \
  -H "Content-Type: application/json" \
  -d '{
    "query": {"term": {"pipeline_run_id": "<run_id>"}},
    "sort": [{"timestamp": {"order": "asc"}}],
    "size": 200
  }' | jq '.hits.hits[]._source | {timestamp, event_type, agent, from_status, to_status}'
```

```bash
curl -s "http://localhost:9200/agent-events-*/_search" \
  -H "Content-Type: application/json" \
  -d '{
    "query": {"term": {"pipeline_run_id": "<run_id>"}},
    "sort": [{"timestamp": {"order": "asc"}}],
    "size": 200
  }' | jq '.hits.hits[]._source | {timestamp, event_type, agent, exit_code}'
```

### Finding failures for a project

```bash
curl -s "http://localhost:9200/decision-events-*/_search" \
  -H "Content-Type: application/json" \
  -d '{
    "query": {
      "bool": {
        "must": [
          {"term": {"event_type": "agent_failed"}},
          {"term": {"project": "context-studio"}}
        ]
      }
    },
    "sort": [{"timestamp": {"order": "desc"}}],
    "size": 10
  }' | jq '.hits.hits[]._source | {timestamp, agent, error, pipeline_run_id}'
```

### Finding all pipeline runs for an issue

```bash
curl -s "http://localhost:9200/pipeline-runs-*/_search" \
  -H "Content-Type: application/json" \
  -d '{
    "query": {
      "bool": {
        "must": [
          {"term": {"issue_number": <issue_number>}},
          {"term": {"project": "<project>"}}
        ]
      }
    },
    "sort": [{"started_at": {"order": "desc"}}],
    "size": 10
  }' | jq '.hits.hits[]._source | {id, status, outcome, started_at, ended_at}'
```

### Checking Claude stream logs for a run

```bash
curl -s "http://localhost:9200/claude-streams-*/_search" \
  -H "Content-Type: application/json" \
  -d '{
    "query": {"term": {"pipeline_run_id": "<run_id>"}},
    "sort": [{"timestamp": {"order": "asc"}}],
    "size": 1000
  }' | jq '.hits.hits[]._source | {timestamp, agent_name, message}'
```

### Agent success rate by agent (last 7 days)

```bash
curl -s "http://localhost:9200/orchestrator-task-metrics-*/_search" \
  -H "Content-Type: application/json" \
  -d '{
    "size": 0,
    "aggs": {
      "by_agent": {
        "terms": {"field": "agent"},
        "aggs": {"success_rate": {"avg": {"field": "success"}}}
      }
    }
  }' | jq '.aggregations.by_agent.buckets[] | {agent: .key, success_rate: .success_rate.value, count: .doc_count}'
```

### Recent task metrics

```bash
curl -s "http://localhost:9200/orchestrator-task-metrics-*/_search?size=10&sort=@timestamp:desc" \
  | jq '.hits.hits[]._source | {agent, duration, success, project}'
```

### Circuit breaker events

```bash
curl -s "http://localhost:9200/decision-events-*/_search" \
  -H "Content-Type: application/json" \
  -d '{
    "query": {
      "terms": {
        "event_type": ["circuit_breaker_opened", "circuit_breaker_closed"]
      }
    },
    "sort": [{"timestamp": {"order": "desc"}}],
    "size": 20
  }' | jq '.hits.hits[]._source | {timestamp, event_type, agent}'
```

### Querying from inside the container

Replace `http://localhost:9200` with `http://elasticsearch:9200`:

```bash
docker-compose exec orchestrator curl -s "http://elasticsearch:9200/pipeline-runs-*/_search" \
  -H "Content-Type: application/json" \
  -d '{"query": {"term": {"status": "active"}}, "size": 10}' \
  | jq '.hits.hits[]._source | {id, issue_number, project, started_at}'
```

---

## 6. Circuit breakers

Circuit breakers prevent cascading failures. When a protected service fails repeatedly, the breaker opens and subsequent calls are immediately rejected (raising `CircuitBreakerOpen`) rather than hanging or failing slowly.

### States

| State | Behavior |
|---|---|
| `closed` | Normal operation; requests pass through |
| `open` | All requests rejected immediately; no calls to the protected service |
| `half_open` | Test mode; one request is allowed through to check if the service has recovered |

### Transitions

- `closed` → `open`: After `failure_threshold` consecutive failures (default: 3)
- `open` → `half_open`: After `recovery_timeout` seconds (default: 600 seconds / 10 minutes)
- `half_open` → `closed`: After `success_threshold` consecutive successes (default: 2)
- `half_open` → `open`: On any failure

### What each circuit breaker protects

| Name | Protects | Where state is stored |
|---|---|---|
| `Claude Code Token Limit` | Prevents new agent containers when token limit is exhausted | In-process (Python object) |
| `GitHub API Rate Limit` | Prevents GitHub API calls when rate limit is exhausted | In-process (Python object); checks reset time against GitHub API |
| `Redis Streams` | Protects pattern ingestion log collector from Redis failures | Redis: `orchestrator:pattern_ingestion_stats` |
| `Elasticsearch Indexing` | Protects log indexing from Elasticsearch failures | Redis: `orchestrator:pattern_ingestion_stats` |
| `Pattern Detection Queries` | Protects pattern detection from Elasticsearch failures | Redis: `orchestrator:pattern_ingestion_stats` |
| Per-agent breakers | Blocks a specific agent after repeated failures | Redis: `circuit_breaker:<agent_name>:state` |

> **Note:** The Redis Streams, Elasticsearch Indexing, and Pattern Detection breakers are only present in the `/api/circuit-breakers` response when the `orchestrator:pattern_ingestion_stats` key exists in Redis. This key is written by the pattern ingestion service, which is disabled by default in `docker-compose.yml`. In a standard deployment these three breakers will not appear in the response.

### Checking circuit breaker states

```bash
# All breakers via API
curl -s http://localhost:5001/api/circuit-breakers | jq '.circuit_breakers[] | {name, state, is_open, failure_count}'

# Summary (how many are open)
curl -s http://localhost:5001/api/circuit-breakers | jq '.summary'

# Agent-specific breakers in Redis directly
docker-compose exec orchestrator python scripts/inspect_circuit_breakers.py

# GitHub API specifically
curl -s http://localhost:5001/api/github-api-status | jq '.status.breaker'
```

### Resetting circuit breakers

The breakers for GitHub API and Claude Code will reset automatically based on their recovery timeouts. For agent-specific breakers stored in Redis, the recovery timeout is 30 seconds. Manual reset is available via the API:

```bash
# Reset Claude Code breaker
curl -s -X POST http://localhost:5001/api/circuit-breakers/claude-code/reset | jq .

# Reset GitHub API breaker
curl -s -X POST http://localhost:5001/api/circuit-breakers/github-api/reset | jq .

# Reset an agent-specific breaker (replace agent_name with the actual name)
curl -s -X POST http://localhost:5001/api/circuit-breakers/agent/senior_software_engineer/reset | jq .
```

To delete an agent breaker directly from Redis:

```bash
docker-compose exec orchestrator redis-cli -h redis del circuit_breaker:senior_software_engineer:state
```

---

## 7. State file inspection

The orchestrator maintains several categories of state files under `state/`. These are read by the orchestrator at startup and updated during operation.

### Dev container state

**Path:** `state/dev_containers/<project>.yaml`

Records whether the project's agent Docker image has been built and verified.

```yaml
image_name: context-studio-agent:latest
status: verified
updated_at: '2026-03-09T01:37:58.560025'
```

`status` values: `verified`, `not_verified`, `error`. If `error`, an `error_message` field is present.

**Use when:** An agent cannot start because the image is missing or stale.

---

### GitHub project state

**Path:** `state/projects/<project>/github_state.yaml`

Contains the GitHub Projects v2 board IDs and column IDs for each project. The orchestrator reconciles this on startup against the actual GitHub state.

**Use when:** The orchestrator is not recognizing board column movements, or when project boards have been recreated.

The directory also contains timestamped backup files (`github_state_backup_*.yaml`) created before each reconciliation.

---

### PR review state

**Path:** `state/projects/<project>/pr_review_state.yaml`

Tracks PR review cycle counts per pull request (how many times a PR has been reviewed and sent back for revisions). This prevents infinite review loops.

**Use when:** A PR seems to be stuck in review, or you need to reset the review count to allow another cycle.

---

### Pipeline locks

**Path:** `state/pipeline_locks/`

File-based lock records for each pipeline board. Files follow the pattern `<project>_<board_name>.yaml`. A corresponding `.lock` file is used as a mutex during reads and writes.

```bash
ls state/pipeline_locks/
cat "state/pipeline_locks/context-studio_SDLC Execution.yaml"
```

**Use when:** A lock file persists after the orchestrator crashed and the lock release was not written. Inspect the lock file to find the issue number and confirm no container is running before deleting it.

> **Note:** Do not delete lock files while the orchestrator is running. Use the API endpoint instead: `POST /api/projects/<project>/pipelines/<board>/release-lock`.

---

### Pipeline queues

**Path:** `state/pipeline_queues/`

Queue position records for issues waiting to execute. Follows the pattern `<project>_<board_name>.yaml`.

**Use when:** Issues are not being picked up in the expected order, or you want to inspect which issues are waiting.

---

### Checkpoints

**Path:** `orchestrator_data/state/checkpoints/`

Pipeline recovery checkpoints. One file per completed stage, named `<pipeline_run_id>_stage_<n>.json`.

```bash
ls orchestrator_data/state/checkpoints/ | head -20
docker-compose exec orchestrator python scripts/inspect_checkpoint.py
```

**Use when:** A pipeline was interrupted and you want to confirm it will resume from the right stage, or to diagnose why recovery did not happen.

---

### Execution history

**Path:** `state/execution_history/`

Records of recent agent execution outcomes per project and issue. Used for stale detection and work execution state tracking.

---

### Review and repair cycle state

**Path:** `state/projects/<project>/review_cycles/`
**Path:** `state/projects/<project>/repair_cycles/`

Records for in-progress review and repair cycles. Used by the orchestrator to resume interrupted cycles after a restart.

**Use when:** A review or repair cycle appears stuck and you need to inspect its current state.
