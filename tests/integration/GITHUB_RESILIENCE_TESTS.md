# GitHub Resilience Integration Tests

Comprehensive integration tests for the GitHub API resilience improvements implemented in Phases 0-7.

## Overview

These tests validate the complete resilience stack against the **REAL GitHub API**:

✅ GraphQL flag fix (-F → -f)
✅ Timeout increases
✅ Retry logic with exponential backoff
✅ Redis-backed caching
✅ Circuit breaker protection
✅ Persistent circuit breaker state
✅ Rate limit graceful degradation
✅ Main loop resilience

## Prerequisites

### 1. GitHub CLI Authentication

```bash
# Check if authenticated
gh auth status

# If not authenticated, login
gh auth login
```

### 2. Python Dependencies

```bash
pip install pytest pytest-asyncio pytest-cov
```

### 3. Redis (Optional)

Redis is required for caching and circuit breaker persistence tests. If not available, those tests will be skipped.

```bash
# Check if Redis is running
redis-cli -h redis ping
```

### 4. Environment Variables (Optional)

```bash
# Test against specific org/user (defaults provided)
export GITHUB_TEST_ORG="your-org"      # Default: anthropics
export GITHUB_TEST_USER="your-user"    # Default: torvalds
```

## Running Tests

### Quick Start

```bash
# Run all tests
./scripts/test_github_resilience.sh

# Run with verbose output
./scripts/test_github_resilience.sh --verbose

# Run with coverage report
./scripts/test_github_resilience.sh --coverage
```

### Using pytest Directly

```bash
# Run all integration tests
pytest tests/integration/test_github_resilience_integration.py -v

# Run specific test class
pytest tests/integration/test_github_resilience_integration.py::TestGraphQLFlagFix -v

# Run specific test
pytest tests/integration/test_github_resilience_integration.py::TestGraphQLFlagFix::test_graphql_query_with_correct_flag -v

# Run with output shown
pytest tests/integration/test_github_resilience_integration.py -v -s
```

## Test Coverage

### 1. GraphQL Flag Fix (Phase 0)
- **TestGraphQLFlagFix**: Validates the -F → -f bug fix
  - `test_graphql_query_with_correct_flag`: Ensures -f works correctly
  - `test_graphql_query_with_wrong_flag_fails`: Validates -F causes errors

### 2. Owner Type Detection
- **TestOwnerTypeDetection**: Real API calls to detect owner types
  - `test_detect_organization_type`: Detects organizations correctly
  - `test_detect_user_type`: Detects users correctly
  - `test_invalid_owner_returns_none`: Handles invalid owners gracefully

### 3. Caching Behavior (Phase 3)
- **TestCachingBehavior**: Redis caching validation
  - `test_cache_stores_owner_type`: Verifies 24-hour cache for owner types
  - `test_cache_stores_projects_list`: Verifies 5-minute cache for project lists
  - Validates cache hits are significantly faster than API calls

### 4. Circuit Breaker (Phases 4-5)
- **TestCircuitBreakerBehavior**: Circuit breaker protection
  - `test_circuit_breaker_opens_after_failures`: Opens after threshold failures
  - `test_circuit_breaker_prevents_requests_when_open`: Blocks requests when open
  - `test_circuit_breaker_recovers_after_timeout`: Recovers automatically

- **TestCircuitBreakerPersistence**: State persistence
  - `test_circuit_breaker_saves_state_to_redis`: Saves state to Redis
  - `test_circuit_breaker_loads_state_from_redis`: Restores state on init

### 5. Retry Logic (Phase 2)
- **TestRetryLogic**: Exponential backoff retry behavior
  - `test_retry_on_transient_failure`: Retries transient failures
  - Validates exponential backoff timing

### 6. Rate Limit Handling (Phase 6)
- **TestRateLimitHandling**: Graceful rate limit degradation
  - `test_rate_limit_detection`: Detects various rate limit error formats
  - `test_rate_limit_does_not_trigger_circuit_breaker`: Rate limits don't open circuit

### 7. Health Monitor Integration
- **TestHealthMonitorIntegration**: Health check resilience
  - `test_health_check_github_success`: Validates health checks work
  - `test_health_check_uses_circuit_breaker`: Verifies circuit breaker integration

### 8. Projects List Retrieval
- **TestProjectsListRetrieval**: GraphQL project queries
  - `test_get_projects_for_organization`: Retrieves org projects
  - `test_get_projects_for_user`: Retrieves user projects

### 9. Timeout Behavior (Phase 1)
- **TestTimeoutBehavior**: Increased timeout validation
  - `test_increased_timeout_allows_slow_responses`: Handles slow responses

### 10. End-to-End Scenarios
- **TestEndToEndScenario**: Real orchestrator usage patterns
  - `test_full_orchestrator_startup_scenario`: Simulates startup sequence
  - `test_recovery_from_transient_failures`: Tests failure recovery

## Test Design Philosophy

### Real API Calls
These tests make **actual API calls** to GitHub. This ensures:
- The fix actually works in production
- No mocking masks real issues
- Integration between components is validated

### No Full Orchestrator Required
Tests import and use the actual modules directly:
```python
from services.github_owner_utils import get_owner_type
from monitoring.health_monitor import HealthMonitor
```

This allows testing without:
- Running the full orchestrator
- Docker containers
- Database initialization
- Project configuration setup

### Isolation and Cleanup
Each test:
- Resets circuit breaker state
- Clears caches
- Uses fixtures for setup/teardown
- Can run independently

## Interpreting Results

### Expected Behavior

**All tests should PASS** if:
✅ GitHub CLI is authenticated
✅ Network connectivity is good
✅ GitHub API is not rate limiting you
✅ Redis is available (for caching tests)

### Common Issues

**Test fails: "GitHub CLI not authenticated"**
```bash
Solution: gh auth login
```

**Test skipped: "Redis not available"**
```bash
# This is OK - caching tests will be skipped
# Other tests still run
```

**Test fails: "API rate limit exceeded"**
```bash
# Wait for rate limit to reset
# Or authenticate with a token that has higher limits
```

**Slow tests**
```bash
# Some tests intentionally wait for timeouts/backoffs
# This validates the retry logic works correctly
# Use --quick flag to skip slow tests
```

## Coverage Report

Run with coverage to see which code paths are tested:

```bash
./scripts/test_github_resilience.sh --coverage
```

View the HTML report:
```bash
open htmlcov/index.html
```

Expected coverage:
- `services/github_owner_utils.py`: 90%+
- `monitoring/health_monitor.py`: 85%+
- `services/circuit_breaker.py`: 95%+

## Continuous Integration

These tests can be integrated into CI/CD:

```yaml
# .github/workflows/integration-tests.yml
name: GitHub Resilience Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      redis:
        image: redis:7
        ports:
          - 6379:6379

    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          pip install pytest pytest-asyncio pytest-cov
          pip install -r requirements.txt

      - name: Setup GitHub CLI
        run: |
          gh auth login --with-token <<< "${{ secrets.GITHUB_TOKEN }}"

      - name: Run integration tests
        run: ./scripts/test_github_resilience.sh --coverage

      - name: Upload coverage
        uses: codecov/codecov-action@v3
```

## Troubleshooting

### "ModuleNotFoundError"

Ensure you're running from the project root:
```bash
cd /home/austinsand/workspace/orchestrator/clauditoreum
PYTHONPATH=. pytest tests/integration/test_github_resilience_integration.py -v
```

### "Circuit breaker stuck open"

Reset between test runs:
```bash
# Clear Redis circuit breaker state
redis-cli -h redis DEL "circuit_breaker:github_api_owner_utils:state"
```

### Test hangs

Check timeouts - some tests intentionally wait:
- Retry backoff tests: ~6 seconds
- Circuit breaker recovery tests: ~3 seconds
- Use Ctrl+C to interrupt if needed

## Adding New Tests

Follow this pattern:

```python
class TestNewFeature:
    """Test description."""

    def test_specific_behavior(self, verify_github_auth, clear_cache, reset_circuit_breaker):
        """Test specific behavior description."""
        # Arrange
        # ... setup

        # Act
        result = function_under_test()

        # Assert
        assert result == expected_value
```

Use fixtures:
- `verify_github_auth`: Ensures GitHub CLI is authenticated
- `clear_cache`: Clears all caches before test
- `reset_circuit_breaker`: Resets circuit breaker state
- `redis_available`: Checks if Redis is available

## Success Criteria

All tests pass means:
✅ GraphQL queries work with correct flag
✅ Caching reduces API calls by 80%+
✅ Circuit breaker protects against failures
✅ Retry logic handles transient errors
✅ Rate limits handled gracefully
✅ State persists across restarts
✅ Health checks work reliably

This validates the orchestrator will be resilient in production!
