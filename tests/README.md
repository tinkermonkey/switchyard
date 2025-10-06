# Orchestrator Test Suite

Comprehensive testing infrastructure for the Claude Code Agent Orchestrator.

## Quick Start

```bash
# Run all tests
pytest

# Run only unit tests (fast)
pytest -m unit

# Run with coverage
pytest --cov --cov-report=html

# Run specific test file
pytest tests/unit/test_review_cycle_context_extraction.py

# Run with verbose output
pytest -v -s
```

## Test Organization

```
tests/
├── unit/                      # Fast, isolated unit tests
│   ├── test_review_cycle_state.py
│   ├── test_context_extraction.py
│   └── test_review_parser.py
├── integration/               # Service integration tests
│   ├── test_github_integration.py
│   ├── test_review_cycle_flow.py
│   └── test_conversational_loop.py
├── e2e/                       # End-to-end workflow tests
│   ├── test_complete_review_cycle.py
│   └── test_escalation_resume.py
├── fixtures/                  # Test data
│   ├── discussions/
│   ├── agent_outputs/
│   └── states/
├── mocks/                     # Mock implementations
│   ├── github_mock.py
│   ├── agent_mock.py
│   └── redis_mock.py
├── utils/                     # Test utilities
│   ├── builders.py
│   ├── assertions.py
│   └── fixtures.py
├── conftest.py                # Pytest configuration
└── TESTING_STRATEGY.md        # Comprehensive testing strategy
```

## Test Categories

### Unit Tests (`-m unit`)
Fast, isolated tests that verify individual functions and classes.

```bash
pytest -m unit
```

**Characteristics:**
- Run in milliseconds
- No external dependencies
- Mock all I/O
- Test single units of code

**Example:**
```python
def test_context_extraction_gets_last_maker_output():
    # Given
    discussion = create_test_discussion()

    # When
    context = extract_context(discussion, iteration=3)

    # Then
    assert 'latest maker output' in context
```

### Integration Tests (`-m integration`)
Tests that verify service interactions with real or realistic dependencies.

```bash
pytest -m integration
```

**Characteristics:**
- Run in seconds
- May use real Redis, file system
- Mock expensive operations (GitHub API, agents)
- Test service integration

**Example:**
```python
@pytest.mark.integration
async def test_review_cycle_persists_state():
    # Given
    cycle_state = create_cycle_state()

    # When
    executor._save_cycle_state(cycle_state)

    # Then
    loaded_state = executor._load_cycle_state(cycle_state.issue_number)
    assert loaded_state.status == cycle_state.status
```

### End-to-End Tests (`-m e2e`)
Complete workflow tests with real services (may use test GitHub repo).

```bash
pytest -m e2e
```

**Characteristics:**
- Run in minutes
- Use real GitHub test repository
- Full orchestrator execution
- Test complete user workflows

**Example:**
```python
@pytest.mark.e2e
@pytest.mark.slow
async def test_complete_review_cycle():
    # Setup test discussion
    discussion = create_test_discussion_in_repo()

    # Run orchestrator
    await orchestrator.process_discussion(discussion.number)

    # Verify workflow completed
    assert discussion_has_approval_comment()
```

## Writing Tests

### Using Builders

Builders provide fluent interfaces for creating test data:

```python
def test_something(review_cycle_builder):
    # Use builder to create test state
    cycle = (review_cycle_builder
        .for_issue(96)
        .with_agents('business_analyst', 'requirements_reviewer')
        .at_iteration(2)
        .with_maker_output("BA output")
        .escalated()
        .build())

    # Test code here
    assert cycle.status == 'awaiting_human_feedback'
```

### Using Mocks

Mocks simulate external services:

```python
def test_something(mock_github_app):
    # Load fixture into mock
    mock_github_app.load_discussion_fixture(
        'D_test123',
        {'comments': {'nodes': [...]}}
    )

    # Test code that uses GitHub API
    result = await function_that_calls_github()

    # Assert mock was called correctly
    assert mock_github_app.assert_comment_posted('Expected text')
```

### Using Assertions

Domain-specific assertions make tests more readable:

```python
from tests.utils.assertions import (
    assert_state_transition,
    assert_context_size,
    assert_single_agent_signature
)

def test_something():
    # Test code
    ...

    # Use domain assertions
    assert_state_transition(before, after, 'reviewer_working')
    assert_context_size(context, max_chars=50000)
    assert_single_agent_signature(context, 'business_analyst')
```

## Test Fixtures

### Common Fixtures

Available in all tests via `conftest.py`:

- `mock_github_app`: Mock GitHub API
- `mock_agent_executor`: Mock agent execution
- `review_cycle_builder`: Build review cycle states
- `discussion_builder`: Build discussion structures
- `task_context_builder`: Build agent task contexts
- `sample_ba_output`: Sample business analyst output
- `sample_reviewer_feedback`: Sample reviewer feedback
- `load_discussion_fixture`: Load fixtures from files

### Fixture Example

```python
def test_with_fixtures(
    mock_github_app,
    review_cycle_builder,
    sample_ba_output
):
    # Fixtures are automatically injected
    cycle = review_cycle_builder.for_issue(96).build()
    mock_github_app.add_comment_to_discussion('D_test', sample_ba_output)

    # Test code here
```

## Running Specific Tests

```bash
# Run specific test function
pytest tests/unit/test_review_cycle.py::test_context_extraction

# Run all tests in a class
pytest tests/unit/test_review_cycle.py::TestContextExtraction

# Run tests matching a pattern
pytest -k "context_extraction"

# Skip slow tests
pytest -m "not slow"

# Run only async tests
pytest -m asyncio
```

## Coverage

```bash
# Run with coverage report
pytest --cov --cov-report=html

# Open coverage report
open htmlcov/index.html

# Show missing lines
pytest --cov --cov-report=term-missing

# Fail if coverage below threshold
pytest --cov --cov-fail-under=80
```

## Debugging Tests

```bash
# Run with Python debugger
pytest --pdb

# Drop into debugger on failure
pytest --pdb --maxfail=1

# Show print statements
pytest -s

# Show local variables in tracebacks
pytest --showlocals

# Verbose output
pytest -vv
```

## Best Practices

### 1. Test Naming
- Use descriptive names: `test_reviewer_gets_last_maker_output_not_reviewer_output`
- Follow pattern: `test_<what>_<condition>_<expected>`

### 2. Test Structure
- Use Arrange-Act-Assert pattern
- Keep tests focused (one concept per test)
- Use fixtures to reduce setup code

### 3. Assertions
- Use domain assertions when available
- Provide clear error messages
- Test one thing per assertion concept

### 4. Mocking
- Mock external dependencies (GitHub, Redis, agents)
- Don't mock what you're testing
- Prefer fixtures over inline mocks

### 5. Test Data
- Use builders for complex objects
- Load fixtures for real data structures
- Keep test data readable

## Running Tests Locally

### Using Makefile (Recommended)

```bash
# Run unit tests
make test

# Run all tests
make test-all

# Run with coverage
make test-coverage

# Run verbose
make test-verbose

# Run specific file
make test-file FILE=tests/unit/test_parser.py

# Clean test artifacts
make clean-test
```

### Using Test Runner Script

```bash
# Run unit tests
./scripts/run_tests.sh

# Run all tests
./scripts/run_tests.sh --all

# Run integration tests only
./scripts/run_tests.sh --integration

# Run with coverage
./scripts/run_tests.sh --all --coverage

# Run specific test
./scripts/run_tests.sh --test tests/unit/test_parser.py --verbose

# Stop on first failure
./scripts/run_tests.sh --all --fail-fast
```

### Using pytest directly

```bash
# Run all tests
pytest

# Run unit tests only
pytest tests/unit -v

# Run integration tests only
pytest tests/integration -v

# Run with coverage
pytest --cov=services --cov=agents --cov=pipeline --cov-report=html

# Run specific test file
pytest tests/unit/test_review_parser.py -v

# Run specific test function
pytest tests/unit/test_review_parser.py::TestStatusDetection::test_explicit_approved_status -v
```

## Continuous Integration

Tests run automatically via GitHub Actions on:
- Push to main branch
- Push to develop branch
- Pull request creation
- Pull request updates

### CI Pipeline Stages

1. **Test Matrix** (Python 3.10, 3.11, 3.12)
   - Install dependencies
   - Run unit tests with coverage (70% minimum)
   - Run integration tests
   - Upload coverage to Codecov

2. **Linting**
   - flake8 (syntax errors and undefined names)
   - black (code formatting check)
   - isort (import sorting check)

3. **Security Scanning**
   - bandit (security vulnerability scan)
   - safety (known vulnerability check)

### Viewing CI Results

- GitHub Actions tab shows all workflow runs
- PR checks show test status before merge
- Coverage reports available in Codecov
- Failed tests show detailed error messages

### Local Pre-commit Checks

Run these before pushing to avoid CI failures:

```bash
# Run all tests
make test-all

# Check code formatting
make format-check

# Run linter
make lint

# Or run everything at once
make test-all && make format-check && make lint
```

## Test Coverage Goals

| Module | Target | Status |
|--------|--------|--------|
| `review_cycle.py` | 90% | 🟡 In Progress |
| `human_feedback_loop.py` | 90% | 🟡 In Progress |
| `project_monitor.py` | 80% | 🔴 Not Started |
| `github_integration.py` | 70% | 🔴 Not Started |
| `review_parser.py` | 95% | 🔴 Not Started |

## Contributing

When adding new features:

1. Write tests first (TDD)
2. Aim for >80% coverage
3. Include unit, integration, and E2E tests
4. Update test documentation
5. Verify tests pass locally before pushing

## Resources

- [TESTING_STRATEGY.md](TESTING_STRATEGY.md) - Comprehensive testing strategy
- [pytest documentation](https://docs.pytest.org/)
- [pytest-asyncio](https://pytest-asyncio.readthedocs.io/)
- [unittest.mock](https://docs.python.org/3/library/unittest.mock.html)
