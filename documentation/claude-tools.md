# Claude Code agents and skills

This codebase ships two Claude Code agents and six skills in `.claude/agents/` and `.claude/skills/`. The agents are purpose-built for operating and documenting the switchyard orchestrator. The skills are executable procedures that both agents and developers can invoke directly. All six skills are user-invocable by typing `/skill-name` in a Claude Code session.

---

## Agents

### `diagnostic-triage-engineer`

**File**: `.claude/agents/diagnostic-triage-engineer.md`
**Model**: claude-sonnet

Investigates system issues, debugging failures, and performs root cause analysis for the switchyard orchestrator. Use this agent when the orchestrator has stopped processing tasks, an agent container crashes, GitHub integration fails, or pipeline execution behaves unexpectedly.

**Invoke by describing the problem:**

```
"The orchestrator seems stuck - it's not picking up new tasks from the queue"
"The senior-software-engineer agent just crashed halfway through coding"
"Issues aren't being picked up from the GitHub project board"
"The SDLC pipeline keeps skipping the code review stage"
```

The agent delegates to skills and diagnostic scripts per its routing table (see below), cross-references evidence across multiple data sources, and produces a structured diagnostic report.

**Diagnostic methodology:**

1. Gather context: symptom, timing, last known good state, affected component
2. Collect evidence using skills and scripts (see routing table)
3. Form 2-3 hypotheses from the evidence
4. Test hypotheses with targeted queries
5. Identify root cause, distinguishing observation from inference
6. Recommend resolution steps

**Output format:**

- Issue Summary (description, affected components, severity)
- Evidence Collected (log findings, timestamps, correlation IDs, anomalies)
- Root Cause Analysis (primary failure point, contributing factors)
- Resolution Steps (immediate actions with expected outcomes)
- Prevention Recommendations (config changes, monitoring, code fixes)
- Follow-up Actions (verification steps, metrics to monitor)

**Diagnostic data sources the agent uses:**

| Source | What it covers |
|---|---|
| Skills (`system-health`, `pipeline-investigate`, etc.) | Structured investigations — always consult first |
| Diagnostic scripts (`scripts/inspect_*.py`) | Deeper automated analysis complementing skills |
| Docker (`docker ps`, `docker logs`, `docker inspect`) | Container state and output |
| Observability API (`localhost:5001`) | Health, active agents, circuit breakers, pipeline runs |
| Elasticsearch (`localhost:9200`) | Event history, metrics, Claude stream logs |
| State files (`state/projects/`, `state/dev_containers/`) | GitHub board state, dev container verification |
| Redis | Task queues, pipeline locks, event streams |

**Known diagnostic pitfalls the agent is trained to avoid:**

- Redis misdiagnosis: `TaskQueue` defaults to `use_redis=False`; helper components may report in-memory fallback even when Redis is healthy. The `/health` endpoint does not check Redis.
- Exit code 137 attribution: SIGKILL means something sent the signal — not necessarily the OOM killer. The agent checks Docker memory limits, kernel logs, orchestrator recovery logic, and Docker events before concluding OOM.
- Health endpoint gaps: `/health` checks Claude API, GitHub API, disk, and memory. It does not check Redis or Elasticsearch.

---

### `technical-writer`

**File**: `.claude/agents/technical-writer.md`
**Model**: claude-sonnet

Produces, revises, and improves software documentation. Use this agent when a new service needs documentation, existing docs are out of date after a refactor, or an operational procedure needs a runbook.

**Invoke by describing the documentation task:**

```
"Write documentation for the new FeatureBranchManager service"
"Update the architecture docs to reflect the pipeline changes"
"Write a runbook for recovering from a stuck pipeline"
```

**Documentation types this agent handles:**

- API references (endpoints, parameters, return values, errors, examples)
- Architecture documentation (component responsibilities, data flow, design rationale)
- Runbooks (trigger conditions, prerequisites, numbered steps, rollback procedures)
- Onboarding guides (system orientation, environment setup)
- Inline code comments (intent, workarounds, limitations)
- Changelogs (Added, Changed, Fixed, Removed, Deprecated)

The agent reads actual source files before writing, verifies all commands and file paths against the codebase, and does not infer behavior it cannot verify.

---

## Skills

Skills are invoked by typing `/skill-name` in a Claude Code session. The `diagnostic-triage-engineer` agent also invokes them programmatically using the Skill tool. All six skills listed here are user-invocable.

### Skill routing table

This table is taken directly from `diagnostic-triage-engineer.md` and is the authoritative guide for which tool to use in each situation.

| Situation | Use |
|---|---|
| Overall system health check | `/system-health` |
| Investigate a specific pipeline run (timeline, stages, failures) | `/pipeline-investigate <pipeline_run_id>` |
| Audit actual vs. expected pipeline flow | `/pipeline-flow-audit <pipeline_run_id>` |
| Investigate a specific agent execution (Docker logs, tool calls, errors) | `/agent-investigate <task_id\|container_name>` |
| Search and analyze Claude Code live logs (tool calls, tool results, output) | `/claude-live-logs <task_id\|pipeline_run_id\|agent_name>` |
| Look up ES index schemas, event types, pipeline flows, access points | `/orchestrator-ref` |

For deeper or automated analysis, complement skills with diagnostic scripts:

| Script | Best for |
|---|---|
| `inspect_run_details.py <run_id>` | Comprehensive pipeline run diagnosis — cross-references Redis, ES, and decision events |
| `inspect_pipeline_timeline.py <run_id> [--verbose\|--json]` | Visual chronological timeline with durations and bottlenecks |
| `inspect_task_health.py [--show-all\|--project\|--json]` | Queue health, stuck task detection (exit 0=ok, 1=stuck, 2=critical) |
| `inspect_checkpoint.py [<run_id>] [--verify-recovery]` | Checkpoint recovery readiness |
| `inspect_circuit_breakers.py` | Open circuit breakers and failure counts |
| `inspect_queue.py` | Task queue depth, metadata, oldest tasks |
| `release_lock.py` | Release a stuck pipeline lock |

Run scripts via:

```bash
docker-compose exec orchestrator python scripts/<script>
```

---

### `system-health`

**File**: `.claude/skills/system-health/SKILL.md`
**Args**: none

Comprehensive health check across all orchestrator components. Run this first when diagnosing any system problem. Executes 8 check steps and synthesizes a structured health report.

**Invocation:**

```
/system-health
```

**Steps executed:**

1. Core health endpoint (`GET /health`) — Redis, GitHub, Docker status
2. Active agents — observability API vs. actual Docker containers (detects orphaned containers)
3. Task queue health — `inspect_task_health.py` or direct Redis queue lengths
4. Circuit breakers — `inspect_circuit_breakers.py` and `GET /api/circuit-breakers`
5. Active pipeline runs — `GET /active-pipeline-runs`
6. Recent errors (last 1 hour) — `decision-events-*` and `agent-events-*` for failure event types
7. Docker infrastructure — `docker-compose ps` and `docker stats`
8. Elasticsearch index health — checks for red/yellow indices or unexpected document counts

**Output:** A structured markdown table with component status (Redis, GitHub, Docker, Elasticsearch, Orchestrator), active work summary (pipeline runs, agents, queued tasks), list of detected issues, and recommendations.

**Flagged conditions:**

- Open circuit breakers
- Orphaned containers (Docker containers not tracked by the observability API)
- Queue backlog
- Recent error spike
- Red Elasticsearch indices
- High memory or CPU on containers
- Stale pipeline runs (active runs with no events in the past 30 minutes)

---

### `pipeline-investigate`

**File**: `.claude/skills/pipeline-investigate/SKILL.md`
**Args**: `<pipeline_run_id>`

Investigates a specific pipeline run: timeline, Docker logs, decision events, and root cause analysis. Use when a pipeline run failed, stalled, or produced unexpected output.

**Invocation:**

```
/pipeline-investigate <pipeline_run_id>
```

If no run ID is provided, the skill queries `pipeline-runs-*` for the 10 most recent runs and asks which one to investigate.

**Steps executed:**

1. Get pipeline run metadata from `pipeline-runs-*` (project, board, status, issue number, duration)
2. Run `inspect_pipeline_timeline.py <RUN_ID> --json` for the pre-built timeline
3. Query `decision-events-*` chronologically for the complete event flow
4. Query `agent-events-*` for agent lifecycle events; extract container names
5. Retrieve Docker logs for each container (`docker logs claude-agent-<project>-<task_id>`)
6. If failures found, query `claude-streams-*` for tool call and tool result events
7. Build a markdown timeline table and provide root cause analysis

**Interpretation heuristics applied:**

| Event or pattern | Interpretation |
|---|---|
| `exit_code=137` | SIGKILL received — investigate cause (see diagnostic-triage-engineer pitfalls) |
| `exit_code=1` | Process error — check Docker logs for stack traces |
| `review_cycle_escalated` | Max review iterations reached; check `iteration` count and reviewer feedback |
| `empty_output_detected` | Claude produced no output; check for API errors or oversized prompt |
| `circuit_breaker_opened` | Repeated failures; check preceding `error_encountered` events |
| `result_persistence_failed` | Container output could not be saved; check filesystem or container crash |
| Gap > 5 minutes between events | Potential stall; check `docker ps` and whether the orchestrator was blocked |
| Repair cycle > 20 iterations | Tests likely unfixable by agent; may need manual review |
| `status_progression_failed` | GitHub API issue or board state mismatch |
| `branch_conflict_detected` | Git merge conflict; check whether auto-resolution was attempted |

---

### `pipeline-flow-audit`

**File**: `.claude/skills/pipeline-flow-audit/SKILL.md`
**Args**: `<pipeline_run_id>`

Compares actual pipeline execution against the expected flow defined in `config/foundations/pipelines.yaml` and `config/foundations/workflows.yaml`. Use when a pipeline completed but behaved unexpectedly (wrong agents, skipped stages, excessive iterations, early termination).

**Invocation:**

```
/pipeline-flow-audit <pipeline_run_id>
```

**Steps executed:**

1. Get pipeline run metadata; determine pipeline type from the `board` field
2. Load expected flow from `config/foundations/pipelines.yaml` and `config/foundations/workflows.yaml`
3. Query `decision-events-*` and `agent-events-*` for the complete event sequence
4. Reconstruct actual stage sequence: agents run, columns visited, review cycle iterations, repair cycle iterations, escalations
5. Build a side-by-side comparison table
6. Explain each deviation: skipped stage, wrong agent, excessive iterations, early termination, extra stages
7. Fetch GitHub issue context to verify label routing and final state

**Expected flows reference (from `pipelines.yaml`):**

`sdlc_execution`: Development (`senior_software_engineer`) → Code Review (review cycle, max 5 iterations) → Testing (repair cycle, max 100 agent calls) → Staged (`senior_software_engineer`)

`planning_design`: Research (`idea_researcher`) → Requirements (`business_analyst`) → Design (`software_architect`) → Work Breakdown (`work_breakdown_agent`) → In Development (tracking) → In Review (`pr_review_agent`) → Documentation (`technical_writer`) → Documentation Review (`documentation_editor`, max 3 iterations)

`environment_support`: In Progress (`dev_environment_setup`) → Verification (`dev_environment_verifier`)

**Output:** A structured audit report with the comparison table and stage status values: `matched`, `deviated`, `skipped`, `extra`, or `failed`.

---

### `agent-investigate`

**File**: `.claude/skills/agent-investigate/SKILL.md`
**Args**: `<container_name|task_id|--recent [time_window]>`

Deep-dive into a single agent execution. Operates in two modes based on the argument.

**Invocation:**

```
/agent-investigate claude-agent-myproject-abc123
/agent-investigate <task_id>
/agent-investigate --recent 6h
```

**Mode A — specific execution** (container name or task ID):

1. Identify the execution via `agent-events-*` if given a task ID, or parse the container name directly
2. Retrieve Docker logs for the container
3. Query `claude-streams-*` for tool call and tool result events
4. Analyze tool calls by type (Bash, Read, Write, Edit, Glob, Grep), success rate, and error patterns
5. Get agent lifecycle timing (`agent_initialized` → `agent_started` → `agent_completed` or `agent_failed`)
6. Synthesize an execution narrative: what agent ran, how long, what it did, outcome, and root cause if failed

**Mode B — recent activity** (`--recent [time_window]`, default `1h`):

1. Find all agent executions initialized within the time window
2. List active Docker containers
3. Cross-reference with completion and failure events
4. Build a summary table with per-agent status
5. Deep-dive on any failures using Mode A steps

**Common failure patterns:**

| Pattern | Signal |
|---|---|
| Duration > 1800s without completion | Agent stuck or in infinite loop; check Docker logs for repeating patterns |
| `success: false` with no `error_message` | Container crashed; check `docker logs` for OOM or signal |
| Multiple tool call failures for same file | Workspace mount issue; verify the project directory is mounted correctly |
| Git push failures | Branch protection, merge conflict, or stale ref; check `branch_conflict_detected` events |
| Same test failures repeating | Agent not interpreting test output; check repair cycle iteration count |

---

### `claude-live-logs`

**File**: `.claude/skills/claude-live-logs/SKILL.md`
**Args**: `<task_id|pipeline_run_id|agent_name> [--recent [time_window]]`

Searches and analyzes Claude Code live execution logs stored in Elasticsearch index `claude-streams-*`. Surfaces tool calls, tool results, agent text output, and thinking. Use when you need to understand exactly what Claude did inside a container — which commands it ran, which files it read, what it wrote, and where it failed.

**Invocation:**

```
/claude-live-logs <task_id>
/claude-live-logs <pipeline_run_id>
/claude-live-logs senior_software_engineer
/claude-live-logs --recent 1h
/claude-live-logs --recent 6h
```

**Data flow (how live logs are captured):**

```
Claude Code process (inside Docker container)
  → JSON events on stdout (tool_call, tool_result, assistant, user, etc.)
docker-claude-wrapper.py
  → Wraps each event with {agent, task_id, project, issue_number, timestamp, event: <raw>}
  → Writes to Redis Stream: orchestrator:claude_logs_stream (maxlen=500, 2h TTL)
  → Publishes to Redis Pub/Sub: orchestrator:claude_stream (real-time UI)
log_collector.py (consumer group: log_collector)
  → Reads from Redis Stream in batches
  → Calls enrich_claude_log() — extracts event_category, tool_name, success, etc.
  → Routes by event_category to Elasticsearch
Elasticsearch: claude-streams-YYYY-MM-DD
```

There is also a direct write path: `ObservabilityManager.emit_claude_stream_event()` in `monitoring/observability.py` bypasses Redis and writes directly to `claude-streams-*` for certain code paths.

**Mode A — specific execution** (task ID, pipeline run ID, or agent name):

1. Identify the task ID (resolves pipeline run IDs and agent names via `agent-events-*`)
2. Get agent lifecycle summary from `agent-events-*`
3. Query full event stream from `claude-streams-*` (chronological, up to 500 events; paginate with `"from": 500` for longer runs)
4. Aggregate tool call counts by type
5. Retrieve failed tool calls with error messages
6. Read agent text output and thinking (from `agent_output` and `agent_thinking` event categories)
7. Search tool parameters for specific content using `tool_params_text` full-text field

**Mode B — recent activity** (`--recent [time_window]`, default `1h`):

- Find recent agent executions
- Find recent failures across all agents
- Compute error rate by tool type

**`claude-streams-*` key fields:**

| Field | Description |
|---|---|
| `task_id` | Primary lookup key for a specific execution |
| `pipeline_run_id` | Groups all executions within one pipeline run |
| `event_category` | `tool_call`, `tool_result`, `agent_output`, `agent_thinking`, `claude_stream`, `other` |
| `tool_name` | Bash, Read, Write, Edit, Glob, Grep, Task, etc. |
| `tool_params_text` | Searchable string version of tool parameters |
| `success` | Whether the call or result succeeded |
| `error_message` | Error details when `success=false` |
| `raw_event` | Complete Claude Code JSON event — not indexed; use `\| .raw_event` in jq to retrieve |

**Common failure patterns:**

| Pattern | Signal |
|---|---|
| Agent stuck or in infinite loop | Repeated `tool_call` events for the same tool with identical parameters; no progress in `agent_output` |
| Test fix failures | Repeated Bash calls with `pytest` or `npm test`; `success: false` on tool results |
| Git push rejected | Bash calls containing `git push`; `error_message` with `rejected` or `conflict` |
| Context length exceeded | `error_message` containing `context` or `token`; `agent_failed` event shortly after |
| File not found | `success: false` on Read/Glob/Grep; `error_message` with `not found` or `no such file` |
| No tool calls at all | Only `claude_stream` or `agent_output` events; Claude may be outputting plain text instead of acting |

---

### `orchestrator-ref`

**File**: `.claude/skills/orchestrator-ref/SKILL.md`
**Args**: none

Quick reference for Elasticsearch indices, event type taxonomy, Docker container naming patterns, pipeline flows, common queries, and system access points. Use when you need to look up an index name, event type, or construct a query without searching the codebase.

**Invocation:**

```
/orchestrator-ref
```

**Elasticsearch indices** (all date-partitioned `*-YYYY-MM-DD`, 7-day retention):

| Index pattern | Content |
|---|---|
| `decision-events-*` | Orchestrator routing decisions, pipeline progression, review/repair cycles, errors, branch management |
| `agent-events-*` | Agent lifecycle: initialized, started, completed, failed |
| `claude-streams-*` | Claude Code streaming: tool calls, tool results, thinking, text output |
| `pipeline-runs-*` | Pipeline run metadata: issue, project, board, status, duration |
| `agent-logs-*` | Legacy combined agent logs (tool calls, results, all events) |
| `orchestrator-task-metrics-*` | Task execution metrics: agent name, duration, success |

**Event type taxonomy** (source: `EventType` enum in `monitoring/observability.py`):

Decision events (indexed in `decision-events-*`) include: feedback monitoring, agent routing, pipeline progression, pipeline run lifecycle, review cycles, repair cycles, repair containers, conversational loops, errors, result persistence, task queue operations, and branch management events. See `orchestrator-ref` output or `monitoring/observability.py` for the complete list.

**Docker container naming:**

- Agent containers: `claude-agent-{project}-{task_id}`
- Repair cycle containers: `repair-cycle-{project}-{issue}-{run_id[:8]}`

**System access points:**

| Service | URL |
|---|---|
| Elasticsearch | `localhost:9200` |
| Observability API | `localhost:5001` |
| Redis | `localhost:6379` |
| Web UI | `localhost:3000` |

---

## Relationship between the diagnostic-triage-engineer agent and skills

The `diagnostic-triage-engineer` agent does not query Elasticsearch or run Docker commands directly as its first action. Its routing table mandates that it invoke the appropriate skill first, using the Skill tool. Skills contain authoritative, up-to-date query templates that the agent would otherwise have to reconstruct from memory.

The relationship is:

```
User reports system issue
  → diagnostic-triage-engineer agent invoked
    → invokes system-health skill (always first for unknown issues)
    → invokes pipeline-investigate or agent-investigate for specific failures
    → invokes orchestrator-ref to look up schemas or event types
    → runs diagnostic scripts for deeper analysis
    → synthesizes all findings into a structured report
```

Skills can also be invoked directly by a developer without going through the agent. Direct skill invocation is appropriate when the problem is already scoped (e.g., you know the pipeline run ID and want the timeline), and you don't need the agent's synthesis, hypothesis formation, and reporting.
