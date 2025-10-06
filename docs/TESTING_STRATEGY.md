# Orchestrator Testing Strategy

## Overview

The orchestrator manages complex, stateful workflows with external dependencies (GitHub, Redis, Docker, file system). This testing strategy ensures reliability through a multi-layered approach.

## Testing Pyramid

```
           ┌─────────────┐
           │  E2E Tests  │  ← Slow, full system (10%)
           ├─────────────┤
           │ Integration │  ← Medium, real services (30%)
           │    Tests    │
           ├─────────────┤
           │ Unit Tests  │  ← Fast, isolated (60%)
           └─────────────┘
```

## 1. Unit Tests (Fast, Isolated)

**Purpose**: Test individual functions and classes in isolation

**Key Areas**:

### State Management
- `ReviewCycleState` serialization/deserialization
- State transitions (initialized → maker_working → reviewer_working → awaiting_feedback)
- Invalid state detection

### Context Extraction
- `_get_fresh_discussion_context()` - extract maker's last output
- `_get_human_feedback_since_last_agent()` - detect parent comment
- Thread history building from GraphQL responses

### Parsing Logic
- Review feedback parsing (approved/changes_requested/blocked)
- Issue severity categorization
- Comment signature detection

### Business Logic
- Iteration counting
- Escalation conditions
- Similarity threshold filtering

**Testing Approach**:
```python
# Example: Test context extraction
def test_get_fresh_discussion_context_extracts_last_maker_output():
    # Given: Discussion with multiple BA outputs
    mock_discussion = load_fixture('discussion_multiple_ba_outputs.json')
    cycle_state = ReviewCycleState(...)

    # When: Extract context
    context = await executor._get_fresh_discussion_context(
        cycle_state, org='test-org', iteration=2
    )

    # Then: Should get ONLY last BA output
    assert 'Revision Notes' in context  # Latest BA marker
    assert context.count('_Processed by the business_analyst agent_') == 1
    assert len(context) < 20000  # Reasonable size
```

## 2. Integration Tests (Medium Speed)

**Purpose**: Test service interactions with real or realistic dependencies

**Key Areas**:

### GitHub API Integration
- GraphQL query execution with test tokens
- Discussion comment posting
- Project board operations
- Rate limiting handling

### Redis Integration
- Task queue operations
- State persistence
- Event streaming
- Cache operations

### File System Integration
- State file persistence
- Config loading
- Checkpoint creation/recovery

### Agent Execution Flow
- Mock agent execution (fast responses)
- Context passing
- Output capture
- Error handling

**Testing Approach**:
```python
# Example: Test review cycle with mocked GitHub
@pytest.mark.integration
async def test_review_cycle_full_flow():
    # Given: Mock GitHub returning specific responses
    with patch_github_api() as mock_github:
        mock_github.graphql_request.return_value = load_fixture('discussion_initial.json')

        # When: Start review cycle
        result = await review_cycle_executor.start_review_cycle(...)

        # Then: Verify state transitions
        assert cycle_state.status == 'reviewer_working'
        assert len(cycle_state.review_outputs) == 1
        assert mock_github.add_discussion_comment.called
```

## 3. End-to-End Tests (Slow, Full System)

**Purpose**: Test complete workflows with real services

**Key Areas**:

### Complete Review Cycles
- Start → reviewer → maker → re-review → approval
- Start → escalation → human feedback → resume
- Start → max iterations → escalation

### Conversational Loops
- Initial output → question → answer → question → answer
- Thread context preservation
- Parent comment tracking

### State Recovery
- Restart during review cycle
- Resume from escalation
- Reconstruct state from discussion

**Testing Approach**:
```python
# Example: Full E2E test with test repository
@pytest.mark.e2e
@pytest.mark.slow
async def test_complete_review_cycle_e2e():
    # Setup: Create test discussion in real test repo
    test_repo = "tinkermonkey/orchestrator-test"
    discussion = create_test_discussion(test_repo, "Test Feature")

    # When: Run orchestrator
    await orchestrator.process_discussion(discussion.number)

    # Then: Verify workflow completion
    comments = fetch_discussion_comments(discussion.number)
    assert_comment_from_agent('business_analyst', comments)
    assert_comment_from_agent('requirements_reviewer', comments)
    assert_final_state('approved')
```

## 4. Test Fixtures and Utilities

### Fixture Library

```
tests/fixtures/
├── discussions/
│   ├── discussion_95.json              # Real snapshot (existing)
│   ├── discussion_simple_approval.json # Happy path
│   ├── discussion_escalation.json      # Blocked + escalation
│   ├── discussion_max_iterations.json  # 3 iterations
│   └── discussion_conversational.json  # Q&A thread
├── agent_outputs/
│   ├── business_analyst_initial.md
│   ├── business_analyst_revision.md
│   └── requirements_reviewer_feedback.md
├── states/
│   ├── cycle_state_iteration_1.yaml
│   ├── cycle_state_escalated.yaml
│   └── cycle_state_awaiting_feedback.yaml
└── graphql_responses/
    ├── discussion_query.json
    ├── comment_mutation.json
    └── project_item_query.json
```

### Test Utilities

```python
# tests/utils/builders.py

class ReviewCycleBuilder:
    """Fluent builder for test cycle states"""
    def __init__(self):
        self._state = ReviewCycleState(...)

    def with_iteration(self, n: int):
        self._state.current_iteration = n
        return self

    def with_maker_output(self, output: str):
        self._state.maker_outputs.append({...})
        return self

    def escalated(self):
        self._state.status = 'awaiting_human_feedback'
        return self

    def build(self):
        return self._state

# Usage:
cycle = (ReviewCycleBuilder()
    .with_iteration(2)
    .with_maker_output("BA revision")
    .escalated()
    .build())
```

```python
# tests/utils/assertions.py

def assert_state_transition(before_state, after_state, expected_transition):
    """Verify valid state machine transition"""
    valid_transitions = {
        'initialized': ['maker_working', 'reviewer_working'],
        'reviewer_working': ['maker_working', 'awaiting_human_feedback', 'completed'],
        'maker_working': ['reviewer_working'],
        'awaiting_human_feedback': ['reviewer_working', 'completed']
    }

    assert after_state.status in valid_transitions[before_state.status], \
        f"Invalid transition: {before_state.status} → {after_state.status}"

def assert_context_size(context: str, max_chars: int = 50000):
    """Verify context doesn't exceed reasonable size"""
    assert len(context) < max_chars, \
        f"Context too large: {len(context)} chars (max: {max_chars})"

def assert_comment_threaded_correctly(discussion, comment_id, expected_parent_id):
    """Verify GitHub comment threading"""
    # ... GraphQL to check parent relationship
```

## 5. Mocking Strategy

### GitHub API Mock

```python
# tests/mocks/github_mock.py

class MockGitHubApp:
    """Mock GitHub App for testing"""

    def __init__(self):
        self._discussions = {}
        self._comments = {}

    def graphql_request(self, query: str, variables: dict):
        """Simulate GraphQL responses"""
        if 'discussion(' in query:
            discussion_id = variables['discussionId']
            return self._discussions.get(discussion_id)
        # ... handle other query types

    def load_discussion_fixture(self, discussion_id: str, fixture_path: str):
        """Pre-load discussion data for tests"""
        with open(fixture_path) as f:
            self._discussions[discussion_id] = json.load(f)
```

### Agent Executor Mock

```python
# tests/mocks/agent_mock.py

class MockAgentExecutor:
    """Mock agent execution for fast tests"""

    def __init__(self):
        self._responses = {}

    def set_response(self, agent_name: str, output: str):
        """Configure mock agent response"""
        self._responses[agent_name] = output

    async def execute_agent(self, agent_name: str, context: dict):
        """Return pre-configured response instantly"""
        return {
            'output': self._responses.get(agent_name, "Mock output"),
            'success': True,
            'duration_ms': 100
        }
```

## 6. Critical Test Scenarios

### Scenario: Review Cycle Context Bug (from recent fix)

```python
def test_reviewer_iteration_3_gets_maker_output_not_reviewer_output():
    """
    Regression test for bug where iteration 3 reviewer received
    its own previous output instead of maker's revision.
    """
    # Given: Discussion with 2 complete iterations
    discussion = load_fixture('discussion_iteration_2_complete.json')
    cycle_state = ReviewCycleState(current_iteration=2, ...)

    # When: Get context for iteration 3 (reviewer's turn)
    context = await executor._get_fresh_discussion_context(
        cycle_state, org='test-org', iteration=3
    )

    # Then: Should contain maker's output, not reviewer's
    assert '_Processed by the business_analyst agent_' in context
    assert 'Revision Notes' in context  # Maker's revision marker
    assert '## Issues Found' not in context  # Reviewer's marker
    assert len(context) > 10000  # Should be full maker output
```

### Scenario: Conversational Thread Context

```python
def test_conversational_response_uses_only_parent_comment():
    """
    Test that question mode extracts ONLY the parent comment
    being replied to, not entire discussion.
    """
    # Given: Discussion with BA output and 3 threaded questions
    discussion = load_fixture('discussion_with_questions.json')
    human_feedback = {
        'body': 'What about X?',
        'author': 'tinkermonkey',
        'parent_comment': {
            'id': 'comment_123',
            'body': 'BA output here...',
            'author': 'orchestrator-bot'
        }
    }

    # When: Build thread context
    context = feedback_loop._build_context(human_feedback)

    # Then: Thread history should have exactly 2 items
    assert len(context['thread_history']) == 2
    assert context['thread_history'][0]['role'] == 'agent'
    assert context['thread_history'][1]['role'] == 'user'

    # Total context should be small (not entire discussion)
    total_chars = sum(len(msg['body']) for msg in context['thread_history'])
    assert total_chars < 30000
```

### Scenario: State Recovery After Restart

```python
async def test_resume_escalated_cycle_without_feedback():
    """
    Test that escalated cycles are properly restored on restart
    even when no human feedback has been provided yet.
    """
    # Given: Discussion with escalation but no human feedback
    discussion = load_fixture('discussion_escalated_no_feedback.json')

    # When: Orchestrator restarts and attempts resume
    next_column, success = await review_cycle_executor.resume_review_cycle(
        issue_number=96,
        discussion_id='D_kwDOPH6wk84AiPtN',
        ...
    )

    # Then: Should recreate awaiting_human_feedback state
    cycle_state = review_cycle_executor.active_cycles[96]
    assert cycle_state.status == 'awaiting_human_feedback'
    assert cycle_state.escalation_time is not None
    assert len(cycle_state.maker_outputs) == 6  # Reconstructed
    assert len(cycle_state.review_outputs) == 3  # Reconstructed

    # State should be saved to disk
    saved_state = load_cycle_state_from_disk(96)
    assert saved_state.status == 'awaiting_human_feedback'
```

## 7. Property-Based Testing

Use Hypothesis for property-based testing:

```python
from hypothesis import given, strategies as st

@given(
    iteration=st.integers(min_value=1, max_value=10),
    num_maker_outputs=st.integers(min_value=1, max_value=10)
)
def test_context_extraction_always_returns_last_maker_output(iteration, num_maker_outputs):
    """
    Property: Context extraction should ALWAYS return the most recent
    maker output, regardless of iteration number or output count.
    """
    # Generate discussion with N maker outputs
    discussion = generate_discussion_with_maker_outputs(num_maker_outputs)

    # Extract context
    context = extract_context(discussion, iteration)

    # Should contain exactly one maker signature (the last one)
    assert context.count('_Processed by the business_analyst agent_') == 1
```

## 8. Test Organization

```
tests/
├── unit/
│   ├── test_review_cycle_state.py
│   ├── test_context_extraction.py
│   ├── test_feedback_detection.py
│   ├── test_review_parser.py
│   └── test_state_transitions.py
├── integration/
│   ├── test_github_integration.py
│   ├── test_redis_integration.py
│   ├── test_review_cycle_flow.py
│   └── test_conversational_loop_flow.py
├── e2e/
│   ├── test_complete_review_cycle.py
│   ├── test_escalation_and_resume.py
│   └── test_state_recovery.py
├── fixtures/
│   ├── discussions/
│   ├── agent_outputs/
│   └── states/
├── mocks/
│   ├── github_mock.py
│   ├── agent_mock.py
│   └── redis_mock.py
├── utils/
│   ├── builders.py
│   ├── assertions.py
│   └── fixtures.py
├── conftest.py                    # pytest configuration
├── TESTING_STRATEGY.md            # This file
└── test_context_extraction.py     # Existing (keep)
```

## 9. CI/CD Integration

### GitHub Actions Workflow

```yaml
name: Test Suite

on: [push, pull_request]

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Run unit tests
        run: pytest tests/unit/ -v --cov

  integration-tests:
    runs-on: ubuntu-latest
    services:
      redis:
        image: redis:7
    steps:
      - uses: actions/checkout@v3
      - name: Run integration tests
        run: pytest tests/integration/ -v

  e2e-tests:
    runs-on: ubuntu-latest
    if: github.event_name == 'pull_request'
    steps:
      - uses: actions/checkout@v3
      - name: Run E2E tests
        run: pytest tests/e2e/ -v -m e2e
        env:
          GITHUB_TEST_TOKEN: ${{ secrets.GITHUB_TEST_TOKEN }}
```

## 10. Test Coverage Goals

- **Unit Tests**: >80% code coverage
- **Integration Tests**: All critical service interactions
- **E2E Tests**: All documented user workflows

### Coverage by Module

| Module | Target Coverage | Current | Priority |
|--------|----------------|---------|----------|
| `review_cycle.py` | 90% | 0% | HIGH |
| `human_feedback_loop.py` | 90% | 0% | HIGH |
| `project_monitor.py` | 80% | 0% | HIGH |
| `github_integration.py` | 70% | 0% | MEDIUM |
| `review_parser.py` | 95% | 0% | HIGH |
| `agent_executor.py` | 80% | 0% | MEDIUM |

## 11. Testing Best Practices

1. **Fixtures over mocks**: Use real data snapshots when possible
2. **Test behavior, not implementation**: Test what code does, not how
3. **Independent tests**: Each test should run in isolation
4. **Fast feedback**: Unit tests should complete in seconds
5. **Meaningful names**: Test names describe the scenario
6. **Arrange-Act-Assert**: Clear test structure
7. **One assertion concept per test**: Test one thing at a time
8. **Test edge cases**: Null inputs, empty collections, boundaries

## 12. Implementation Priority

### Phase 1: Critical Path Unit Tests (Week 1)
- [ ] Review cycle state transitions
- [ ] Context extraction (the recent bug)
- [ ] Thread context building
- [ ] Review parser

### Phase 2: Integration Tests (Week 2)
- [ ] Review cycle full flow (mocked GitHub)
- [ ] Conversational loop flow
- [ ] State persistence and recovery

### Phase 3: Test Infrastructure (Week 3)
- [ ] Mock framework
- [ ] Fixture library
- [ ] Test utilities and builders
- [ ] CI/CD setup

### Phase 4: E2E and Property Tests (Week 4)
- [ ] Complete workflow tests
- [ ] Property-based testing
- [ ] Performance tests
- [ ] Load tests

## 13. Next Steps

1. Start with `test_review_cycle_context_extraction.py` - add regression test for recent bug
2. Create mock GitHub API framework
3. Build test fixture library from existing discussion snapshots
4. Implement state builders for easy test setup
5. Set up pytest configuration with markers for slow/fast tests
