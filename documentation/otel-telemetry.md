# OTEL telemetry migration — Phase 2 and Phase 3

Phase 1 (complete) added the `otel-collector` service, the shared `ClaudeEnvironmentBuilder`, and wired OTEL env vars into all Claude Code launches. The collector runs on `otel/opentelemetry-collector-contrib:latest` (v0.148.0+) in `otel` mode, writing to ES 9.0 native OTEL data streams:

- **Logs**: `logs-claude.otel-default` — backed by `logs-otel@template` component chain
- **Metrics**: `metrics-claude.otel-default` — backed by `metrics-otel@template` component chain

Both use a custom override template (`claude-otel-logs-ilm`, `claude-otel-metrics-ilm`) that applies our `claude-otel-ilm-policy` (7-day delete) while composing the same OTEL component templates as the built-in templates. The existing Redis→Elasticsearch path via `docker-claude-wrapper.py` and `log_collector.py` continues to run in parallel during the validation period.

This document covers what to do next.

---

## Phase 2 — Replace stream parsing and add Redis fan-out

**Goal**: remove the custom JSON stream parsing from `docker-claude-wrapper.py` and `log_collector.py`, replacing those signals with OTEL data. Add a Redis pub/sub bridge so real-time dashboard events (currently written by the wrapper) come from the OTEL pipeline instead.

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

### 2.2 Add the Redis pub/sub bridge service

The wrapper currently writes real-time Claude stream events to:
- `orchestrator:claude_logs_stream` (Redis Stream, consumed by `log_collector`)
- `orchestrator:claude_stream` (Redis Pub/Sub, consumed by the websocket handler in `observability_server.py`)

Replace this with a lightweight OTLP→Redis bridge that receives log events from the OTEL collector and publishes them to the same Redis channels.

**New file**: `services/otel_redis_bridge.py`

```python
"""
Receives OTLP log events from the otel-collector and fans them out to Redis
pub/sub, replacing docker-claude-wrapper.py's direct Redis writes.

Exposes a minimal OTLP HTTP receiver on port 4320.
The otel-collector sends to this service via the otlphttp/redis_bridge exporter.
"""
```

The bridge translates an incoming OTLP `LogRecord` into the existing event dict shape and calls:

```python
redis_client.xadd('orchestrator:claude_logs_stream', {'log': json.dumps(event)}, maxlen=500, approximate=True)
redis_client.publish('orchestrator:claude_stream', json.dumps(event))
```

**docker-compose.yml** — add the bridge service (alongside `otel-collector`):

```yaml
  otel-redis-bridge:
    build: .
    environment:
      - REDIS_HOST=redis
      - REDIS_PORT=6379
    networks:
      - orchestrator-net
    ports:
      - "4320:4320"   # OTLP HTTP receiver — otel-collector connects here
    working_dir: /app
    command: ["python", "-m", "services.otel_redis_bridge"]
    depends_on:
      - redis
    restart: unless-stopped
```

**`config/otel-collector-config.yaml`** — uncomment the bridge exporter:

```yaml
exporters:
  # ... existing elasticsearch exporter ...
  otlphttp/redis_bridge:
    endpoint: http://otel-redis-bridge:4320
    retry_on_failure:
      enabled: false   # Best-effort; don't back up the file queue for pub/sub

service:
  pipelines:
    logs:
      receivers: [otlp]
      processors: [memory_limiter, batch, resource]
      exporters: [elasticsearch, otlphttp/redis_bridge]  # add bridge here
```

### 2.3 Remove Claude stream parsing from docker-claude-wrapper.py

Once the bridge is live and confirmed, strip the JSON parsing loop from the wrapper.

**File**: `scripts/docker-claude-wrapper.py`

Remove:
- The `write_claude_event()` method and its `xadd` / `publish` calls
- The per-line JSON parsing in the streaming loop (the `for line in iter(...)` body that decodes events and calls `write_claude_event`)
- Token accumulation variables (`input_tokens`, `output_tokens`, etc.) if only used for Redis events

Keep:
- `subprocess.Popen(['claude'] + args)` — Claude Code must still be launched
- Stdout passthrough to container stdout (for `docker logs`)
- The final result dict construction and Redis key write (`agent_result:{project}:{issue}:{task_id}`)
- File fallback result write (`/tmp/agent_result_{task_id}.json`)
- Signal handlers (SIGTERM/SIGINT) for graceful shutdown

The wrapper's sole remaining responsibility after this change is **result persistence** for the orchestrator handoff.

### 2.4 Remove claude-streams index population from log_collector.py

**File**: `services/log_collector.py`

Remove the consumer loop that reads from `orchestrator:claude_logs_stream` and indexes to `claude-streams-*`. The OTEL pipeline now populates `claude-otel-logs-*` and `claude-otel-metrics-*` with richer, schema-consistent data.

Keep the consumer loop for `orchestrator:event_stream` (orchestrator lifecycle events, pipeline decisions) — those still originate from `monitoring/observability.py`, not from Claude Code OTEL.

### 2.5 Update observability_server.py websocket handler

**File**: `services/observability_server.py`

The websocket handler that streams Claude events to the web UI consumes from `orchestrator:claude_stream`. No change to the Redis channel is needed — the bridge writes to the same channel. Confirm the event dict shape matches what the web UI expects; adapt the bridge's output format if necessary.

---

## Phase 3 — Rationalise docker-claude-wrapper.py

**Goal**: evaluate whether the wrapper can be removed entirely, simplifying the container launch to a direct `claude` invocation.

### The wrapper's remaining responsibilities after Phase 2

After Phase 2 the wrapper does two things:

1. **Result persistence** — writes the final Claude response to `agent_result:{project}:{issue}:{task_id}` in Redis and `/tmp/agent_result_{task_id}.json` as fallback, so the orchestrator can retrieve it after the container exits.
2. **Signal handling** — catches SIGTERM/SIGINT and flushes the result before the container stops, supporting graceful shutdown during orchestrator restarts.

### Option A — Move result persistence to the orchestrator (preferred)

The orchestrator already watches Docker containers via `docker logs -f`. It could capture the final `result` line from stdout instead of reading a Redis key. This requires:

1. Agree on a sentinel line format that Claude Code (via `--output-format stream-json`) emits at completion. The `result` event type in the stream-json format is the natural choice.
2. Update `docker_runner.py`'s log streaming loop to detect the result event and persist it, replacing the current Redis key read.
3. Remove the wrapper entirely. The container launch command becomes a direct `claude` invocation with prompt piped via stdin.

This approach is architecturally cleaner: the orchestrator owns result retrieval, and the container is a pure Claude Code process with no wrapper overhead.

### Option B — Keep a minimal wrapper

If the signal handling complexity or the fallback-file pattern are needed for reliability, keep the wrapper but reduce it to ~50 lines:

```python
#!/usr/bin/env python3
"""Thin result-persistence wrapper for Claude Code containers."""
import json, os, signal, subprocess, sys
import redis

redis_client = redis.Redis(host=os.environ['REDIS_HOST'], port=int(os.environ['REDIS_PORT']))
result_key = f"agent_result:{os.environ['PROJECT']}:{os.environ['ISSUE_NUMBER']}:{os.environ['TASK_ID']}"
final_result = None

def persist(signum=None, frame=None):
    if final_result:
        redis_client.setex(result_key, 7200, json.dumps(final_result))
    sys.exit(0)

signal.signal(signal.SIGTERM, persist)
signal.signal(signal.SIGINT, persist)

proc = subprocess.run(['claude'] + sys.argv[1:], stdin=sys.stdin, capture_output=False)
# ... parse last result event from stdout, assign to final_result, persist
persist()
```

### Decision criteria

Choose Option A if:
- The orchestrator's log streaming loop is already reliable for long-running containers
- No external consumers depend on the Redis result key format

Choose Option B if:
- The `/tmp/agent_result_{task_id}.json` fallback is needed for recovery scenarios where Redis is unavailable
- The signal handler pattern has prevented data loss in practice

### Cleanup checklist for Phase 3

- [ ] Remove `scripts/docker-claude-wrapper.py` (or reduce to Option B minimal form)
- [ ] Remove wrapper mount from `docker_runner.py` (`-v {wrapper_host_path}:/app/scripts/docker-claude-wrapper.py:ro`)
- [ ] Update the container launch command in `docker_runner.py` to invoke `claude` directly
- [ ] Remove `claude-streams-template` from `services/pattern_detection_schema.py` (index schema no longer needed)
- [ ] Update `documentation/agent-execution-architecture.md` Phase 6 section to reflect the new launch pattern
- [ ] Archive or delete `claude-streams-YYYY.MM.DD` indices in Elasticsearch (data already in `logs-claude.otel-default`)

---

## Elasticsearch index reference

| Index / data stream | Source | Retention | Phase added |
|---|---|---|---|
| `logs-claude.otel-default` | OTEL collector (log events) | 7 days | Phase 1 |
| `metrics-claude.otel-default` | OTEL collector (metrics) | 7 days | Phase 1 |
| `claude-streams-*` | log_collector ← Redis ← wrapper | 7 days | Legacy (remove Phase 2) |
| `agent-events-*` | monitoring/observability.py | 7 days | Keep |
| `decision-events-*` | monitoring/observability.py | 7 days | Keep |

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
