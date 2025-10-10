# Quick Start - Claude Code Integration Tests

## TL;DR - Run Tests Now

```bash
# From project root
./tests/integration/run_claude_tests.sh
```

That's it! ✅ 13 tests will run and pass.

## What These Tests Verify

✅ **Monitoring Events** - Prompt construction, API calls, completion events  
✅ **Live Log Events** - Real-time stream events from Claude Code  
✅ **Redis Publishing** - Events published to pub/sub and stream  
✅ **Token Tracking** - Input/output token counts captured  
✅ **Session Management** - Multi-turn conversation support  
✅ **Error Handling** - Errors generate appropriate events  

## Test Commands

### Mocked Tests (Default - No API Key Needed)
```bash
./tests/integration/run_claude_tests.sh mocked
# or just:
./tests/integration/run_claude_tests.sh
```

### Real Claude CLI Tests (Requires API Key)
```bash
export ANTHROPIC_API_KEY="your-key-here"
./tests/integration/run_claude_tests.sh real
```

### All Tests
```bash
./tests/integration/run_claude_tests.sh all
```

### With Coverage Report
```bash
./tests/integration/run_claude_tests.sh coverage
```

## Alternative: Use Make
```bash
# Run specific file
make test-file FILE=tests/integration/test_claude_code_mocked.py

# Run all integration tests
make test-integration
```

## Alternative: Use Pytest Directly
```bash
source .venv/bin/activate
pytest tests/integration/test_claude_code_mocked.py -v
```

## What Gets Tested

### Event Flow
```
Prompt → Claude API → Stream Events → Redis → WebSocket → UI
  ↓           ↓              ↓            ↓         ↓
  ✅          ✅             ✅           ✅        ✅
```

### Monitoring Events (3 types)
1. `prompt_constructed` - Prompt details
2. `claude_api_call_started` - API call begins
3. `claude_api_call_completed` - Duration & tokens

### Stream Events (5+ types)
1. `status` - Status updates
2. `assistant` - AI responses
3. `usage` - Token counts
4. `error` - Error info
5. `session_id` - Session continuity

## Expected Output

```
=== Claude Code Integration Tests ===

Running mocked tests (no Claude CLI required)

================ test session starts ================

tests/integration/test_claude_code_mocked.py::TestClaudeCodeEventEmission::test_prompt_constructed_event PASSED
tests/integration/test_claude_code_mocked.py::TestClaudeCodeEventEmission::test_claude_call_started_event PASSED
tests/integration/test_claude_code_mocked.py::TestClaudeCodeEventEmission::test_claude_call_completed_event PASSED
tests/integration/test_claude_code_mocked.py::TestClaudeCodeStreamEvents::test_stream_callback_receives_events PASSED
tests/integration/test_claude_code_mocked.py::TestClaudeCodeStreamEvents::test_stream_events_capture_session_id PASSED
tests/integration/test_claude_code_mocked.py::TestClaudeCodeStreamEvents::test_response_text_collected_from_assistant_events PASSED
tests/integration/test_claude_code_mocked.py::TestClaudeCodeStreamEvents::test_token_usage_tracked_from_stream PASSED
tests/integration/test_claude_code_mocked.py::TestClaudeCodeRedisPublishing::test_events_published_to_pubsub_channel PASSED
tests/integration/test_claude_code_mocked.py::TestClaudeCodeRedisPublishing::test_events_added_to_redis_stream PASSED
tests/integration/test_claude_code_mocked.py::TestClaudeCodeRedisPublishing::test_stream_ttl_set PASSED
tests/integration/test_claude_code_mocked.py::TestClaudeCodeErrorScenarios::test_cli_failure_raises_exception PASSED
tests/integration/test_claude_code_mocked.py::TestClaudeCodeErrorScenarios::test_error_events_in_stream PASSED
tests/integration/test_claude_code_mocked.py::TestClaudeCodeSessionContinuity::test_session_resume_with_existing_id PASSED

========== 13 passed in 0.07s ==========

✓ Tests complete
```

## Files Created

```
tests/integration/
├── run_claude_tests.sh                  ← Quick test runner
├── test_claude_code_mocked.py           ← Main test suite (13 tests)
├── test_claude_code_integration.py      ← Real CLI tests
├── README_CLAUDE_CODE_TESTS.md          ← Full documentation
├── CLAUDE_CODE_TESTS_SUMMARY.md         ← Summary & examples
└── QUICKSTART.md                        ← This file
```

## Need Help?

```bash
./tests/integration/run_claude_tests.sh help
```

## View Coverage Report

```bash
./tests/integration/run_claude_tests.sh coverage
open htmlcov/index.html
```

## Debug a Specific Test

```bash
source .venv/bin/activate
pytest tests/integration/test_claude_code_mocked.py::TestClaudeCodeEventEmission::test_prompt_constructed_event -v -s
```

## Questions?

- 📖 See `README_CLAUDE_CODE_TESTS.md` for detailed documentation
- 📊 See `CLAUDE_CODE_TESTS_SUMMARY.md` for examples and flow diagrams
- 💻 See test files for implementation details

---

**Status**: ✅ All 13 tests passing  
**Last Updated**: October 10, 2025
