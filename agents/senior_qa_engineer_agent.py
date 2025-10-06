from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class SeniorQAEngineerAgent(MakerAgent):
    """
    Senior QA Engineer agent for quality assurance execution.

    Performs comprehensive testing and production readiness assessment.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("senior_qa_engineer", agent_config=agent_config)

    # ==================================================================================
    # REQUIRED PROPERTIES
    # ==================================================================================

    @property
    def agent_display_name(self) -> str:
        return "Senior QA Engineer"

    @property
    def agent_role_description(self) -> str:
        return "I write and execute comprehensive quality assurance tests including integration tests, end-to-end tests, performance tests, and production readiness validation."

    @property
    def output_sections(self) -> List[str]:
        return [
            "Unit Test Results",
            "Integration Test Results",
            "End-to-End Test Results",
            "Test Coverage Analysis",
            "Production Readiness Assessment"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS
    # ==================================================================================

    def get_initial_guidelines(self) -> str:
        return """
## QA Testing Requirements

**CRITICAL**: Your role has TWO phases:
1. **WRITE missing tests** (integration, e2e, performance)
2. **EXECUTE all tests** and report results

### Phase 1: Write Missing Tests

The Senior Software Engineer wrote unit tests for their implementation. You must now write:

1. **Integration Tests** (`tests/integration/`):
   - Test how components work together
   - Test external dependencies (database, APIs, file system)
   - Test cross-module interactions
   - Verify data flows through multiple layers

2. **End-to-End Tests** (`tests/e2e/` or `tests/integration/`):
   - Test complete user workflows
   - Simulate real-world usage scenarios
   - Test from entry point to final output

3. **Performance Tests** (if applicable):
   - Load testing for scalability
   - Response time benchmarks
   - Resource usage validation

**Write comprehensive tests that would catch production issues.**

### Phase 2: Execute All Tests

After writing your tests, execute the full test suite:

1. **Run Unit Tests** (written by Software Engineer):
   ```bash
   pytest tests/unit/ -v --tb=short
   ```
   - Report: Total tests, passed, failed, skipped
   - For ANY failures: Include full stack trace and error message

2. **Run Integration Tests** (written by you):
   ```bash
   pytest tests/integration/ -v --tb=short
   ```
   - Report: Total tests, passed, failed, skipped
   - For ANY failures: Include full stack trace and error message

3. **Run End-to-End Tests** (written by you):
   ```bash
   pytest tests/e2e/ -v --tb=short  # or tests/integration/test_e2e_*.py
   ```
   - Report: Total tests, passed, failed, skipped
   - For ANY failures: Include full stack trace and error message

4. **Run Test Coverage Analysis**:
   ```bash
   pytest tests/ --cov=. --cov-report=term-missing
   ```
   - Report: Overall coverage percentage
   - **IMPORTANT**: New/changed code must have ≥80% coverage
   - Identify new/changed files with <80% coverage
   - List uncovered lines in new/changed code

5. **Run All Tests Together** (final validation):
   ```bash
   pytest tests/ -v
   ```
   - Confirm all tests pass in full suite
   - Verify no conflicts between test categories

### Output Format for Test Results:

For each test category, provide:
```
## [Test Category] Results

**Command**: `[exact command run]`
**Result**: PASS/FAIL
**Total**: X tests
**Passed**: X
**Failed**: X
**Skipped**: X

[If failures exist:]
### Failed Tests:
1. **test_name**:
   - Error: [error message]
   - Stack trace: [relevant trace]
   - Root cause: [your analysis]
   - Recommendation: [fix needed]
```

### Production Readiness Checklist:

After running all tests, assess:
- ✅ All unit tests passing (written by Software Engineer)
- ✅ All integration tests passing (written by you)
- ✅ All end-to-end tests passing (written by you)
- ✅ New/changed code coverage ≥80%
- ✅ No performance regressions
- ✅ No security vulnerabilities detected
- ✅ Error handling tested comprehensively
- ✅ Edge cases covered

**If ANY tests fail, your work is NOT complete. Fix the failures and re-run tests.**
"""

    def get_quality_standards(self) -> str:
        return """
- All critical user flows are tested end-to-end
- Integration points are validated with comprehensive integration tests
- New/changed code has ≥80% test coverage
- Performance benchmarks are met
- Security vulnerabilities are identified and addressed
- Production deployment checklist is complete
- ALL tests (unit, integration, e2e) must pass before work is considered complete
"""
