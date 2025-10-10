# Orchestrator State Machine Test Design

## Overview
Comprehensive unit tests for the orchestrator's state machine, covering:
- GitHub monitoring and issue detection
- Agent routing and execution
- Maker-reviewer cycles
- Pipeline progression
- State transitions

## Key Flows to Test

### 1. Simple Agent Execution Flow
```
GitHub Issue Status Change → Agent Selection → Task Queue → Agent Execution → Complete
```

### 2. Maker-Reviewer Cycle Flow
```
Status Change → Maker Agent → Review Agent → 
  ├─ Approved → Next Stage
  └─ Changes Requested → Maker Agent (iterate)
```

### 3. Multi-Stage Pipeline Flow
```
Stage 1 (Ready) → Agent A → 
Stage 2 (In Progress) → Agent B → 
Stage 3 (Review) → Reviewer → 
Stage 4 (Done)
```

### 4. Combined Flow (Most Complex)
```
Requirements → Business Analyst (maker) → Requirements Reviewer (reviewer) →
  └─ Approved → Design → Software Architect (maker) → Design Reviewer (reviewer) →
      └─ Approved → Development → Software Engineer (maker) → Code Reviewer (reviewer) →
          └─ Approved → QA → QA Engineer → Done
```

## Test Architecture

### Mock Strategy

#### GitHub API Mocks
- Mock issue details retrieval
- Mock status field updates
- Mock comment posting
- Mock project item queries
- Return predictable data structures

#### Agent Execution Mocks  
- Mock agent executor to avoid actual execution
- Return configurable success/failure responses
- Simulate agent outputs
- Track which agents were "executed"

#### Review Parser Mocks
- Mock review result parsing
- Return configurable approved/changes_requested status
- Simulate reviewer feedback

### Test Fixtures

#### Configuration Fixtures
```python
@pytest.fixture
def test_project_config():
    """Multi-stage pipeline with maker-reviewer cycles"""
    return {
        'stages': [
            {'name': 'Requirements', 'maker': 'business_analyst', 'reviewer': 'requirements_reviewer'},
            {'name': 'Design', 'maker': 'software_architect', 'reviewer': 'design_reviewer'},
            {'name': 'Development', 'maker': 'senior_software_engineer', 'reviewer': 'code_reviewer'},
            {'name': 'QA', 'agent': 'qa_engineer'}
        ]
    }
```

#### GitHub Response Fixtures
```python
@pytest.fixture
def mock_github_issue():
    """Returns issue data structure"""
    
@pytest.fixture  
def mock_project_items():
    """Returns project board items"""
```

#### State Tracking Fixtures
```python
@pytest.fixture
def state_tracker():
    """Tracks state transitions during test execution"""
```

## Test Cases

### 1. Basic Agent Routing Tests
- `test_trigger_agent_for_status_routes_to_correct_agent`
- `test_agent_routing_decision_event_emitted`
- `test_skip_agent_for_closed_issue`
- `test_no_agent_for_status_without_mapping`
- `test_skip_duplicate_tasks`

### 2. Maker-Reviewer Cycle Tests
- `test_start_review_cycle_queues_maker`
- `test_maker_completes_then_reviewer_runs`
- `test_reviewer_approves_ends_cycle`
- `test_reviewer_requests_changes_queues_maker_again`
- `test_review_cycle_iteration_limit`
- `test_review_cycle_escalation`
- `test_review_cycle_state_persistence`

### 3. Pipeline Progression Tests
- `test_get_next_column_returns_correct_stage`
- `test_move_issue_to_next_column`
- `test_progression_triggers_next_agent`
- `test_progression_skips_columns_without_agents`
- `test_end_of_pipeline_handling`

### 4. Integration Flow Tests
- `test_simple_single_agent_flow`
- `test_maker_reviewer_with_approval`
- `test_maker_reviewer_with_iterations`
- `test_multi_stage_pipeline_progression`
- `test_full_pipeline_with_multiple_maker_reviewer_cycles`

### 5. State Machine Tests
- `test_state_transitions_recorded`
- `test_decision_events_emitted_at_each_step`
- `test_pipeline_run_tracking`
- `test_concurrent_issues_different_states`

### 6. Error Handling Tests
- `test_agent_execution_failure_handling`
- `test_github_api_failure_recovery`
- `test_invalid_status_handling`
- `test_missing_configuration_handling`

## Test Utilities

### MockGitHubAPI
```python
class MockGitHubAPI:
    """Mock GitHub API responses"""
    def __init__(self):
        self.issues = {}
        self.comments = []
        self.status_changes = []
    
    def get_issue(self, issue_number):
        return self.issues.get(issue_number)
    
    def update_status(self, issue_number, new_status):
        self.status_changes.append((issue_number, new_status))
```

### MockAgentExecutor
```python
class MockAgentExecutor:
    """Mock agent execution"""
    def __init__(self):
        self.executions = []
        self.results = {}
    
    async def execute_agent(self, agent, task):
        self.executions.append((agent, task))
        return self.results.get(agent, success_result())
```

### StateTracker
```python
class StateTracker:
    """Track state transitions during test"""
    def __init__(self):
        self.transitions = []
        self.events = []
    
    def record_transition(self, from_state, to_state, reason):
        self.transitions.append((from_state, to_state, reason))
```

## Test Organization

```
tests/unit/orchestrator/
├── __init__.py
├── conftest.py                          # Shared fixtures
├── test_github_monitoring.py            # GitHub monitoring tests
├── test_agent_routing.py                # Agent selection tests
├── test_review_cycles.py                # Maker-reviewer cycle tests
├── test_pipeline_progression.py         # Auto-progression tests
├── test_state_machine_integration.py    # Full flow integration tests
└── mocks/
    ├── __init__.py
    ├── mock_github.py                   # GitHub API mocks
    ├── mock_agents.py                   # Agent execution mocks
    └── mock_parsers.py                  # Parser mocks
```

## Success Criteria

1. ✅ All basic flows work correctly with mocked dependencies
2. ✅ Maker-reviewer cycles iterate correctly
3. ✅ Pipeline auto-progression triggers correctly
4. ✅ State transitions are tracked and validated
5. ✅ Decision events are emitted at correct points
6. ✅ Error scenarios are handled gracefully
7. ✅ Tests run quickly (< 1 second each)
8. ✅ Tests are deterministic and repeatable
9. ✅ Clear assertions on expected behavior
10. ✅ No actual GitHub API calls or agent executions

## Implementation Plan

1. Create mock utilities and fixtures
2. Implement basic routing tests
3. Implement maker-reviewer cycle tests
4. Implement pipeline progression tests
5. Implement integration flow tests
6. Add error handling tests
7. Validate full test coverage

## Example Test

```python
@pytest.mark.asyncio
async def test_full_pipeline_with_maker_reviewer_cycle(
    mock_github,
    mock_agent_executor,
    mock_review_parser,
    project_monitor,
    state_tracker
):
    """Test complete flow: Requirements → Design → Development with review cycles"""
    
    # Setup: Issue in Requirements stage
    mock_github.create_issue(
        number=100,
        title="New Feature",
        status="Requirements",
        state="OPEN"
    )
    
    # Configure mock responses
    mock_agent_executor.set_result('business_analyst', success_with_output("BA analysis"))
    mock_agent_executor.set_result('requirements_reviewer', approved_review())
    mock_agent_executor.set_result('software_architect', success_with_output("Architecture doc"))
    mock_agent_executor.set_result('design_reviewer', approved_review())
    
    # Trigger: Issue moved to Requirements
    await project_monitor.trigger_agent_for_status(
        project_name="test-project",
        board_name="dev",
        issue_number=100,
        status="Requirements",
        repository="test-repo"
    )
    
    # Assert: Review cycle started for Requirements stage
    assert state_tracker.current_state(100) == "maker_working"
    assert mock_agent_executor.was_executed('business_analyst', issue=100)
    
    # Complete maker
    await complete_agent_task('business_analyst', issue=100)
    
    # Assert: Reviewer executed
    assert mock_agent_executor.was_executed('requirements_reviewer', issue=100)
    
    # Assert: On approval, progressed to Design
    assert mock_github.get_issue_status(100) == "Design"
    
    # Assert: Design review cycle started
    assert mock_agent_executor.was_executed('software_architect', issue=100)
    
    # Complete full flow
    await complete_agent_task('software_architect', issue=100)
    await complete_agent_task('design_reviewer', issue=100)
    
    # Assert: Progressed to Development
    assert mock_github.get_issue_status(100) == "Development"
    
    # Verify decision events emitted
    events = state_tracker.get_events(issue=100)
    assert any(e['type'] == 'AGENT_ROUTING_DECISION' for e in events)
    assert any(e['type'] == 'REVIEW_CYCLE_STARTED' for e in events)
    assert any(e['type'] == 'STATUS_PROGRESSION_COMPLETED' for e in events)
```

## Notes

- Tests should be fast (<1s each)
- Use AsyncMock for async methods
- Mock at service boundary, not implementation details
- Each test should be independent
- Use descriptive test names
- Add comments explaining complex scenarios
- Validate both happy path and error cases
