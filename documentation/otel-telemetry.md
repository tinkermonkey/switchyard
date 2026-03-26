# OTEL telemetry migration — Phase 2 and Phase 3

Phase 1 (complete) added the `otel-collector` service, the shared `ClaudeEnvironmentBuilder`, and wired OTEL env vars into all Claude Code launches. The collector runs on `otel/opentelemetry-collector-contrib:latest` (v0.148.0+) in `otel` mode, writing to ES 9.0 native OTEL data streams:

- **Logs**: `logs-claude.otel-default` — backed by `logs-otel@template` component chain
- **Metrics**: `metrics-claude.otel-default` — backed by `metrics-otel@template` component chain

Both use a custom override template (`claude-otel-logs-ilm`, `claude-otel-metrics-ilm`) that applies our `claude-otel-ilm-policy` (7-day delete) while composing the same OTEL component templates as the built-in templates. The existing Redis→Elasticsearch path via `docker-claude-wrapper.py` and `log_collector.py` continues to run in parallel during the validation period.

This document covers what to do next.

---

## Phase 2 — Remove legacy ES path; preserve real-time pub/sub

**Goal**: make the OTEL collector the sole source of truth for all persisted Claude Code event data, while keeping the real-time Redis pub/sub path in the wrapper intact for zero-latency live monitoring.

Two concerns are deliberately separated and evolve independently:

| Path | Owner | Latency | Purpose |
|---|---|---|---|
| `docker-claude-wrapper.py` → `orchestrator:claude_stream` → WebSocket | wrapper `publish()` | ~0 ms | Live monitoring in the UX |
| Claude Code → OTEL collector → `logs-claude.otel-default` | OTEL pipeline | ~5–10 s | Persistent history, analytics |

The real-time path is latency-sensitive and must stay synchronous. The history path is latency-tolerant; a 5–10 s write delay is acceptable because historical queries only serve completed or long-running runs.

### 2.1 Validate OTEL data quality before cutting over

Before removing anything, confirm the OTEL indices have equivalent coverage:

```bash
# Compare token counts in both systems for the same session
curl -s "http://localhost:9200/claude-otel-metrics-*/_search" \
  -H 'Content-Type: application/json' \
  -d '{"query":{"term":{"attributes.task_id":"<task_id>"}},"size":20}' | jq '.hits.hits[]._source'

curl -s "http://localhost:9200/claude-streams-*/_search" \
  -H 'Content-Type: application/json' \
  -d '{"query":{"term":{"task_id.keyword":"<task_id>"}},"size":20}' | jq '.hits.hits[]._source'
```

Confirm parity for: `token.usage` (input/output/cacheRead/cacheCreation), `tool_result` events, `api_error` events, and `cost.usage`.

**Also confirm OTEL field paths**: fetch a sample document from `logs-claude.otel-default` and record the exact paths for `task_id`, `pipeline_run_id`, `agent`, and `event.name`. These are needed for step 2.4. The standard OTEL exporter puts resource attributes under `attributes.*` (e.g. `attributes.task_id`) — verify this matches actual documents before writing any queries.

### 2.2 Remove claude-streams-* ES population from log_collector.py

**File**: `services/log_collector.py`

Remove the consumer loop that reads from `orchestrator:claude_logs_stream` and indexes to `claude-streams-*`. The OTEL pipeline is now the authoritative source for Claude Code events in ES.

Remove:
- Consumer loop for `orchestrator:claude_logs_stream` (the `self.claude_stream` stream key)
- `put_index_template` call for `claude-streams-template`
- Any schema constants referenced only by that loop

Keep:
- Consumer loop for `orchestrator:event_stream` — orchestrator lifecycle and pipeline decision events originate from `monitoring/observability.py`, not from OTEL
- ILM bootstrap for `claude-otel-ilm-policy`, `claude-otel-logs-ilm`, `claude-otel-metrics-ilm` — these manage the OTEL data streams

After this step `orchestrator:claude_logs_stream` has no consumers. Step 2.3 removes the producer.

### 2.3 Remove xadd from docker-claude-wrapper.py; keep publish

**File**: `scripts/docker-claude-wrapper.py`

In `write_claude_event()`, remove:
- `redis_client.xadd('orchestrator:claude_logs_stream', ...)` — no consumers remain after 2.2
- `redis_client.expire('orchestrator:claude_logs_stream', 7200)` — stream is no longer written

Keep:
- `redis_client.publish('orchestrator:claude_stream', ...)` — feeds the real-time WebSocket
- The per-line JSON parsing loop and `write_claude_event()` call in `run_claude()` — still needed to publish each event to the pub/sub channel
- Result persistence (`write_final_result_with_retry`, `write_fallback_result`)
- Signal handlers (SIGTERM/SIGINT)

The wrapper retains two responsibilities: **real-time event publishing** and **result persistence**.

### 2.4 Migrate observability_server.py queries from claude-streams-* to logs-claude.otel-default

**Files**: `services/observability_server.py`, `services/token_metrics_service.py`, `services/project_metrics_service.py`

Eight query sites must be updated. Use the field paths confirmed in step 2.1.

**Confirmed field mapping** (validated against live OTEL documents — March 2026):

| `claude-streams-*` field | `logs-claude.otel-default` field | Notes |
|---|---|---|
| `task_id` | `resource.attributes.task_id.keyword` | `text+keyword` in logs; use `.keyword` for term/agg |
| `pipeline_run_id` | `resource.attributes.pipeline_run_id.keyword` | Same mapping as task_id |
| `agent_name.keyword` | `resource.attributes.agent.keyword` | |
| `event_type` | `event_name.keyword` | e.g. `tool_result`, `api_request`, `user_prompt` |
| `timestamp` (ISO-8601 string) | `@timestamp` | Standard ES date field; range queries and `now` math work |
| `raw_event` body | `attributes.*` | Per-event attributes: `tool_name`, `success`, `duration_ms`, `tool_parameters`, `input_tokens`, `cost_usd`, etc. |

**Metric-side field mapping** (`metrics-claude.otel-default`):

| Signal | Field path | Type | Notes |
|---|---|---|---|
| Token type | `attributes.type` | `keyword` | `input`, `output`, `cacheRead`, `cacheCreation` |
| Token value | `metrics."claude_code.token.usage"` | `float` | 10 s delta gauge; sum over session for total |
| Cost value | `metrics."claude_code.cost.usage"` | `float` | 10 s delta gauge; sum over session for total |
| Task filter | `resource.attributes.task_id` | `keyword` (no `.keyword` needed) | Plain keyword in metrics vs `text+keyword` in logs |

**Token totals**: prefer summing `attributes.input_tokens` / `output_tokens` / etc. from `api_request` log events (per-call precision). Fields are strings so script agg is required: `Long.parseLong(doc['attributes.input_tokens.keyword'].value)`. Alternatively, sum `metrics."claude_code.token.usage"` per `attributes.type` from the metrics index (numeric, no scripting needed).

**Query sites**:

| Location | Endpoint | Change |
|---|---|---|
| `get_claude_logs_history()` | `/claude-logs-history` | Remove Redis stream read (stream no longer populated); query ES directly. Remap `agent_name.keyword` → `attributes.agent` and source field extraction. |
| Pipeline run events fetch | `/api/pipeline-run/<id>/events` | Remap `pipeline_run_id` → `attributes.pipeline_run_id` |
| Execution detail logs | `/api/agent-execution/<id>` | Remap `task_id` → `attributes.task_id`; remap sort field `timestamp` → `@timestamp` |
| Prompt event fetch | `/api/agent-execution/<id>` | Query `agent-events-*` (NOT OTEL) — `prompt_constructed` is an orchestrator event; no field remapping needed; fields stay as `event_type` and `task_id` |
| Token usage per pipeline run | `/api/pipeline-run/<id>/token-usage` | Switch index; remap `pipeline_run_id` → `attributes.pipeline_run_id` |
| `_process_task_streams_per_task()` in `token_metrics_service.py` | internal — called by `/api/execution-summaries` | Switch to `logs-claude.otel-default`; filter `event_name.keyword: api_request`; read `attributes.input_tokens.keyword` / `output_tokens` / `cache_*_input_tokens` as strings (script agg required); dedup logic is not needed — OTEL `api_request` events are one-per-API-call, not streaming re-emissions |
| `prompt_constructed` fetch in `token_metrics_service.py` | internal — `get_execution_summaries_bulk()` step 4 | Switch to `agent-events-*`; filter `event_type: prompt_constructed`; field `prompt_length` and `task_id` are unchanged (not OTEL fields) |
| Tool/token query in `project_metrics_service.py` | internal — tool breakdown | Switch to `logs-claude.otel-default`; filter `event_name.keyword: tool_result` for tool breakdown; or `api_request` for token data; remap `task_id` → `resource.attributes.task_id.keyword` |

### 2.5 Fix misrouted orchestrator events out of claude-streams-*

**Background**: `log_collector._consume_agent_events()` reads from `orchestrator:event_stream` and routes every event through `enrich_event()`. That function assigns `event_category = "other"` to anything it doesn't recognise, and `get_index_name()` maps `"other"` to `claude-streams-*`. The result is that non-Claude-Code events have been accumulating in `claude-streams-*` for the entire lifetime of the system.

Audit of the 62,000+ events in `claude-streams-*` by correct home:

| Event type(s) | Count (all-time) | Currently in | Should be in | Status |
|---|---|---|---|---|
| `tool_result`, `tool_call`, `user_message`, `unknown`, `text_output` | ~79,000 | `claude-streams-*` | OTEL `logs-claude.otel-default` | Handled by steps 2.2–2.3 |
| `agent_routing_decision`, `branch_*`, `review_cycle_*`, `repair_cycle_*` (all 40+ decision types), `status_progression_*`, `pr_review_phase_*`, `feedback_*`, `error_encountered/recovered`, `task_queued`, `sub_issue_created`, `prompt_size_warning`, `retry_attempted`, `conversational_loop_started`, `workspace_routing_decision`, `status_validation_failure` | ~17,000 | `claude-streams-*` AND `decision-events-*` | `decision-events-*` only | **Duplicate** — `observability.py` already writes these correctly; log_collector creates a copy |
| `container_launch_started`, `container_launch_succeeded`, `container_launch_failed`, `container_execution_completed`, `container_execution_failed` | ~6,000 | `claude-streams-*` only | `agent-events-*` | **Lost** — not in their correct index at all |
| `pipeline_run_completed`, `repair_cycle_container_started`, `repair_cycle_container_completed`, `performance_metric` | ~4,200 | `claude-streams-*` only | `decision-events-*` | **Lost** — not in their correct index at all |
| `prompt_constructed` | ~4,090 | `claude-streams-*` only | `agent-events-*` | **Lost** — orchestrator-constructed prompt context, useful for debugging |

**Fix — two files:**

**`monitoring/observability.py`**

Add the lost event types to the correct classification sets so `observability.py` writes them directly to ES (the same pattern already used for all other decision and lifecycle events):

```python
def _is_agent_lifecycle_event(self, event_type):
    lifecycle_events = {
        # existing entries ...
        EventType.CONTAINER_LAUNCH_STARTED,
        EventType.CONTAINER_LAUNCH_SUCCEEDED,
        EventType.CONTAINER_LAUNCH_FAILED,
        EventType.CONTAINER_EXECUTION_COMPLETED,
        EventType.CONTAINER_EXECUTION_FAILED,
        EventType.PROMPT_CONSTRUCTED,    # if EventType exists; else add to enum
    }

def _is_decision_event(self, event_type):
    decision_events = {
        # existing entries ...
        EventType.PIPELINE_RUN_COMPLETED,
        EventType.REPAIR_CYCLE_CONTAINER_STARTED,
        EventType.REPAIR_CYCLE_CONTAINER_COMPLETED,
        EventType.REPAIR_CYCLE_CONTAINER_RECOVERED,
        EventType.REPAIR_CYCLE_CONTAINER_KILLED,
        EventType.REPAIR_CYCLE_CONTAINER_CHECKPOINT_UPDATED,
        EventType.PERFORMANCE_METRIC,
    }
```

**`services/pattern_detection_schema.py`**

Fix the `"other"` catch-all in `get_index_name()` — nothing from `orchestrator:event_stream` belongs in `claude-streams-*`:

```python
def get_index_name(date=None, event_category=None):
    if event_category in ['agent_lifecycle', 'claude_api']:
        prefix = 'agent-events'
    elif event_category in ['claude_stream', 'tool_call', 'tool_result', 'agent_output', 'agent_thinking']:
        prefix = 'claude-streams'   # only until Phase 2 removes this index
    else:
        # 'other' and anything unrecognised → decision-events, not claude-streams
        prefix = 'decision-events'
```

Also update `enrich_event()` to give container lifecycle events the correct category so the log_collector consumer routes them to `agent-events-*` (belt-and-suspenders until the index is removed):

```python
elif event_type in [
    "container_launch_started", "container_launch_succeeded",
    "container_launch_failed", "container_execution_started",
    "container_execution_completed", "container_execution_failed",
    "prompt_constructed",
]:
    enriched["event_category"] = "agent_lifecycle"   # → agent-events-*
```

**Verify after applying**: query `decision-events-*` for `pipeline_run_completed`, `repair_cycle_container_*`, `performance_metric`; query `agent-events-*` for `container_launch_*` and `prompt_constructed`. Confirm zero new writes to `claude-streams-*` for any of these types.

### 2.6 Cohesive live + history view

With both paths active, the UX operates cleanly in two modes with no hybrid query needed:

**During an active run** — the WebSocket subscription delivers events in real time (sub-second). The history query for this run returns events up to ~10 s old. No UX issue: the live tab is active; the history tab is not in focus.

**After a run completes** — all events land in `logs-claude.otel-default` within ~10 s of the final event. By the time a user navigates from the live view to the history view, the data is complete.

**Edge case: immediate transition** — if a user completes a run and immediately queries history, the last ~10 s of events may not yet be indexed. This is acceptable: the WebSocket buffer in the browser already holds those events and the live panel shows them. An optional UX improvement is to delay the history query by 15 s after observing an `agent_completed` event, but this is not required for the migration.

The latency concern applies **only to the history path**. The live path is unaffected — it continues to receive events synchronously from the wrapper via Redis pub/sub at ~0 ms latency.

---

## Phase 3 — Slim docker-claude-wrapper.py

**Goal**: reduce the wrapper to its two permanent responsibilities and eliminate the dead code removed in Phase 2.

### The wrapper's remaining responsibilities after Phase 2

After Phase 2 the wrapper has two permanent responsibilities:

1. **Real-time event publishing** — per-line JSON parsing and `publish()` to `orchestrator:claude_stream` for the live UX. This cannot move to the OTEL pipeline without accepting the ~5–10 s latency penalty on the live view, which is a UX regression. The wrapper stays as long as sub-second live event delivery is a requirement.
2. **Result persistence** — writes the final Claude response to `agent_result:{project}:{issue}:{task_id}` in Redis and `/tmp/agent_result_{task_id}.json` as fallback, so the orchestrator can retrieve it after the container exits. Includes SIGTERM/SIGINT signal handling to flush the result before container stop.

The wrapper is a permanent fixture, not a transitional shim. Phase 3 is about reducing it to these two concerns only.

### Option A — Move result persistence to the orchestrator (preferred)

Move responsibility 2 out of the wrapper so the wrapper owns only the pub/sub path:

1. Agree on a sentinel line format that Claude Code (via `--output-format stream-json`) emits at completion. The `result` event type in the stream-json format is the natural choice.
2. Update `docker_runner.py`'s log streaming loop to detect the result event and persist the `agent_result:*` Redis key directly, replacing the current post-container key read.
3. Remove `write_final_result_with_retry`, `write_fallback_result`, SIGTERM/SIGINT handlers, and the `output_lines` accumulator from the wrapper.

The wrapper shrinks to ~40 lines: connect Redis, stream stdout, parse each line as JSON, `publish()`. The orchestrator owns result retrieval.

### Option B — Keep all responsibilities in the wrapper

No structural change. The Phase 2 simplification (removing `xadd` and `expire`) already reduces the wrapper by ~15 lines. The wrapper stays at its current ~130 lines handling pub/sub, result persistence, and signal handling together.

### Decision criteria

Choose **Option A** if:
- The orchestrator's `docker logs -f` streaming loop is already reliable for long-running containers
- No external consumers depend on the Redis result key being written by the container itself (rather than the orchestrator)

Choose **Option B** if:
- The `/tmp/agent_result_{task_id}.json` fallback has prevented data loss in practice when Redis was transiently unavailable
- The SIGTERM signal handler has been exercised during orchestrator restarts and the behavior is depended on

### Cleanup checklist for Phase 3

- [ ] Remove `xadd` and `expire` calls from `write_claude_event()` in `docker-claude-wrapper.py` (Phase 2 step 2.3 prerequisite)
- [ ] If Option A: remove `write_final_result_with_retry`, `write_fallback_result`, signal handlers, and `output_lines` from the wrapper
- [ ] Remove `claude-streams-template` from `services/pattern_detection_schema.py` (schema no longer needed after Phase 2)
- [ ] Remove `claude_stream` / `"claude-streams"` routing from `pattern_detection_schema.get_index_name()` — the `elif event_category in ['claude_stream', ...]` branch becomes dead code once the index is gone
- [ ] Remove `_consume_claude_logs()` consumer and the `self.claude_stream` stream key from `log_collector.py` (Phase 2 step 2.2); confirm `_consume_agent_events()` still runs for `task_received` and `claude_api_call_*` routing to `agent-events-*`
- [ ] If Option A: update `documentation/agent-execution-architecture.md` Phase 6 section to reflect that result persistence moved to the orchestrator
- [ ] Archive or delete `claude-streams-YYYY.MM.DD` indices in Elasticsearch (data now in `logs-claude.otel-default`)

---

## Elasticsearch index reference

| Index / data stream | Source | Retention | Status |
|---|---|---|---|
| `logs-claude.otel-default` | OTEL collector (log events) | 7 days | Active — sole source for history queries after Phase 2 |
| `metrics-claude.otel-default` | OTEL collector (metrics) | 7 days | Active — token, cost, commit, PR, LOC signals |
| `claude-streams-*` | log_collector ← Redis ← wrapper | 7 days | Remove in Phase 2; stop writing, migrate queries, archive indices |
| `agent-events-*` | monitoring/observability.py (direct) + log_collector for `task_received`, `claude_api_call_*` | 7 days | Keep — add `container_launch_*`, `container_execution_*`, `prompt_constructed` in Phase 2 step 2.5 |
| `decision-events-*` | monitoring/observability.py (direct) | 7 days | Keep — add `pipeline_run_completed`, `repair_cycle_container_*`, `performance_metric` in Phase 2 step 2.5 |

## OTEL signal coverage

| Signal | OTEL metric/event | Notes |
|---|---|---|
| Token usage | `claude_code.token.usage` | Attributes: `type` (input/output/cacheRead/cacheCreation), `model` |
| Session cost | `claude_code.cost.usage` | USD; not available in legacy wrapper |
| Tool calls | `claude_code.tool_result` log event | Includes `tool_parameters` with bash commands, MCP names |
| API errors | `claude_code.api_error` log event | Replaces wrapper error catch |
| Commits created | `claude_code.commit.count` | Not available in legacy wrapper |
| PRs created | `claude_code.pull_request.count` | Not available in legacy wrapper |
| Lines changed | `claude_code.lines_of_code.count` | Attributes: `type` (added/removed) |
| Active time | `claude_code.active_time.total` | Attributes: `type` (user/cli) |
| Container lifecycle | `agent-events-*` via observability.py | Not replaced by OTEL |
| Pipeline decisions | `decision-events-*` via observability.py | Not replaced by OTEL |
