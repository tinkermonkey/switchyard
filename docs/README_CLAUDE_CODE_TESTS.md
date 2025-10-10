# Claude Code Integration Tests

This directory contains integration tests that verify Claude Code execution and monitoring event generation.

## Test Files

### test_claude_code_mocked.py
**Purpose**: Test event emission and monitoring flow WITHOUT requiring Claude CLI

**What it tests**:
- ✅ Monitoring events are correctly emitted (prompt_constructed, claude_api_call_started, etc.)
- ✅ Stream events are captured and forwarded to callbacks
- ✅ Events are published to Redis pub/sub and stream
- ✅ Response text is properly collected from assistant events
- ✅ Token usage is tracked and reported
- ✅ Session continuity (session_id management)
- ✅ Error handling and error event generation

**Requirements**: None (uses mocks, can run in CI/CD)

**Run with**:
```bash
pytest tests/integration/test_claude_code_mocked.py -v
```

### test_claude_code_integration.py
**Purpose**: Test actual Claude CLI execution with real API calls

**What it tests**:
- ✅ Claude CLI is invoked correctly
- ✅ Simple prompts ("hello world", "list files") execute successfully
- ✅ Monitoring events are generated during real execution
- ✅ Stream events from real Claude API are captured
- ✅ Docker container execution works correctly
- ✅ File operations in workspace work
- ✅ Session continuity across multiple turns

**Requirements**: 
- Claude CLI installed (`npm install -g @anthropic-ai/claude-code`)
- ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN set
- Docker (for Docker execution tests)

**Run with**:
```bash
pytest tests/integration/test_claude_code_integration.py -v -s
```

**Skip if Claude CLI not installed**:
```bash
# Tests automatically skip if Claude CLI not found
pytest tests/integration/test_claude_code_integration.py -v
```

## Event Flow Verified

The tests verify this complete event flow:

```
1. Prompt Construction
   └─> emit_prompt_constructed()
       └─> Redis pub/sub: orchestrator:agent_events
       └─> Redis stream: orchestrator:event_stream

2. Claude API Call Start
   └─> emit_claude_call_started()
       └─> Redis pub/sub: orchestrator:agent_events
       └─> Redis stream: orchestrator:event_stream

3. Stream Events (real-time)
   └─> stream_callback(event)
       └─> WebSocket to UI (not tested here)
       └─> Token usage tracking
       └─> Session ID capture

4. Claude API Call Complete
   └─> emit_claude_call_completed()
       └─> Redis pub/sub: orchestrator:agent_events
       └─> Redis stream: orchestrator:event_stream
       └─> Includes: duration_ms, input_tokens, output_tokens
```

## Monitoring Event Structure

### prompt_constructed
```json
{
  "timestamp": "2025-10-10T12:00:00Z",
  "event_type": "prompt_constructed",
  "agent": "test_agent",
  "task_id": "test_001",
  "project": "test_project",
  "data": {
    "prompt": "Say hello",
    "prompt_preview": "Say hello",
    "prompt_length": 9,
    "estimated_tokens": null
  }
}
```

### claude_api_call_started
```json
{
  "timestamp": "2025-10-10T12:00:01Z",
  "event_type": "claude_api_call_started",
  "agent": "test_agent",
  "task_id": "test_001",
  "project": "test_project",
  "data": {
    "model": "claude-sonnet-4-5-20250929",
    "start_time": "2025-10-10T12:00:01Z"
  }
}
```

### claude_api_call_completed
```json
{
  "timestamp": "2025-10-10T12:00:05Z",
  "event_type": "claude_api_call_completed",
  "agent": "test_agent",
  "task_id": "test_001",
  "project": "test_project",
  "data": {
    "duration_ms": 4523.5,
    "input_tokens": 50,
    "output_tokens": 25,
    "total_tokens": 75
  }
}
```

## Stream Event Structure

Claude Code emits events in `stream-json` format:

### assistant (response content)
```json
{
  "type": "assistant",
  "message": {
    "content": [
      {"type": "text", "text": "Hello, World!"}
    ]
  },
  "session_id": "session-abc-123"
}
```

### usage (token counts)
```json
{
  "type": "usage",
  "usage": {
    "input_tokens": 50,
    "output_tokens": 25
  }
}
```

### error (API errors)
```json
{
  "type": "error",
  "error": "Invalid model specified",
  "message": "Model not found"
}
```

## Running Tests

### Run all Claude Code tests (mocked only)
```bash
pytest tests/integration/test_claude_code_mocked.py -v
```

### Run with real Claude CLI (requires API key)
```bash
export ANTHROPIC_API_KEY="your-key-here"
pytest tests/integration/test_claude_code_integration.py -v -s
```

### Run specific test class
```bash
pytest tests/integration/test_claude_code_mocked.py::TestClaudeCodeEventEmission -v
```

### Run specific test
```bash
pytest tests/integration/test_claude_code_mocked.py::TestClaudeCodeEventEmission::test_prompt_constructed_event -v
```

### Run with verbose output
```bash
pytest tests/integration/test_claude_code_mocked.py -v -s
```

### Run in CI/CD (skip Claude CLI tests)
```bash
pytest tests/integration/test_claude_code_mocked.py -v
# test_claude_code_integration.py will auto-skip if CLI not available
```

## Test Coverage

| Component | Mocked Tests | Real CLI Tests |
|-----------|--------------|----------------|
| Event emission | ✅ | ✅ |
| Stream event capture | ✅ | ✅ |
| Redis pub/sub | ✅ | ✅ |
| Redis stream | ✅ | ✅ |
| Token usage tracking | ✅ | ✅ |
| Session continuity | ✅ | ✅ |
| Error handling | ✅ | ✅ |
| Response text collection | ✅ | ✅ |
| Docker execution | ❌ | ✅ |
| File operations | ❌ | ✅ |
| Tool usage | ❌ | ✅ |

## Debugging

### View all events emitted
```bash
pytest tests/integration/test_claude_code_mocked.py::TestClaudeCodeEventEmission::test_prompt_constructed_event -v -s --log-cli-level=DEBUG
```

### Check stream events
```python
# Add to test:
print(f"Stream events: {stream_events.events}")
for event in stream_events.events:
    print(f"  {event.get('type')}: {event}")
```

### Check Redis events
```python
# Add to test:
redis_client = obs_manager.redis
publish_calls = redis_client.publish.call_args_list
print(f"Published {len(publish_calls)} events")
for call in publish_calls:
    channel, event_json = call[0]
    event = json.loads(event_json)
    print(f"  {event['event_type']}")
```

## Expected Behavior

### Successful Execution
1. prompt_constructed event emitted
2. claude_api_call_started event emitted
3. Stream events start flowing
4. Assistant events contain response text
5. Usage events contain token counts
6. Session ID captured (if present)
7. claude_api_call_completed event emitted with metrics
8. All events published to Redis pub/sub
9. All events added to Redis stream with TTL

### Error Scenarios
1. CLI not found → Graceful fallback or clear error
2. API error → Error event captured, exception raised
3. Invalid model → Error event captured, exception raised
4. Timeout → Process killed, exception raised

## Integration with Web UI

These events flow to the web UI for real-time observation:

1. **Redis pub/sub** → WebSocket → UI live updates
2. **Redis stream** → REST API → UI history view
3. **Stream events** → WebSocket → UI progress indicators

The web UI observability server (`monitoring/observability_server.py`) subscribes to these events and forwards them to connected WebSocket clients.

## Future Enhancements

- [ ] Add tests for MCP server configuration
- [ ] Test large prompt handling (>50KB)
- [ ] Test multi-turn conversations (revision mode)
- [ ] Test conversational mode (Q&A)
- [ ] Add performance benchmarks
- [ ] Test retry logic and circuit breakers
- [ ] Test workspace mounting in Docker
- [ ] Test dev container image selection
