"""
Pytest configuration and shared fixtures

This file provides common fixtures and configuration for all tests.
"""

import pytest
import asyncio
from pathlib import Path
from typing import Dict, Any

# Import test utilities
from tests.mocks.github_mock import MockGitHubApp, MockGitHubIntegration, MockAgentExecutor
from tests.utils.builders import ReviewCycleStateBuilder, DiscussionBuilder, TaskContextBuilder


# ============================================================================
# Pytest Configuration
# ============================================================================

def pytest_configure(config):
    """Register custom markers"""
    config.addinivalue_line(
        "markers", "unit: Unit tests (fast, isolated)"
    )
    config.addinivalue_line(
        "markers", "integration: Integration tests (medium speed, real services)"
    )
    config.addinivalue_line(
        "markers", "e2e: End-to-end tests (slow, full system)"
    )
    config.addinivalue_line(
        "markers", "slow: Slow tests (skip in fast test runs)"
    )


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# ============================================================================
# Path Fixtures
# ============================================================================

@pytest.fixture
def tests_dir():
    """Path to tests directory"""
    return Path(__file__).parent


@pytest.fixture
def fixtures_dir(tests_dir):
    """Path to fixtures directory"""
    return tests_dir / 'fixtures'


@pytest.fixture
def discussions_fixtures_dir(fixtures_dir):
    """Path to discussion fixtures"""
    return fixtures_dir / 'discussions'


# ============================================================================
# Mock Fixtures
# ============================================================================

@pytest.fixture
def mock_github_app():
    """Create a MockGitHubApp instance"""
    app = MockGitHubApp()
    yield app
    app.reset()


@pytest.fixture
def mock_github_integration(mock_github_app):
    """Create a MockGitHubIntegration instance"""
    return MockGitHubIntegration(mock_github_app)


@pytest.fixture
def mock_agent_executor():
    """Create a MockAgentExecutor instance"""
    executor = MockAgentExecutor()
    yield executor
    executor.reset()


@pytest.fixture
def patch_github_api(monkeypatch, mock_github_app):
    """
    Patch GitHub API to use mock

    Usage:
        def test_something(patch_github_api):
            # GitHub API calls will use mock
            ...
    """
    from services.github_app import github_app
    monkeypatch.setattr(github_app, 'graphql_request', mock_github_app.graphql_request)
    monkeypatch.setattr(github_app, 'rest_request', mock_github_app.rest_request)
    monkeypatch.setattr(github_app, 'get_installation_token', mock_github_app.get_installation_token)

    return mock_github_app


# ============================================================================
# Builder Fixtures
# ============================================================================

@pytest.fixture
def review_cycle_builder():
    """Create a ReviewCycleStateBuilder"""
    return ReviewCycleStateBuilder()


@pytest.fixture
def discussion_builder():
    """Create a DiscussionBuilder"""
    return DiscussionBuilder()


@pytest.fixture
def task_context_builder():
    """Create a TaskContextBuilder"""
    return TaskContextBuilder()


# ============================================================================
# Common Test Data Fixtures
# ============================================================================

@pytest.fixture
def sample_issue_data():
    """Common issue data structure"""
    return {
        'number': 96,
        'title': 'Test Feature',
        'body': 'Test feature description',
        'state': 'open',
        'labels': []
    }


@pytest.fixture
def sample_ba_output():
    """Sample business analyst output"""
    return """## Business Requirements Analysis

**Feature**: Test Feature

## Functional Requirements

FR-1: The system shall do X
FR-2: The system shall do Y

## User Stories

US-1: As a user, I want to X

_Processed by the business_analyst agent_"""


@pytest.fixture
def sample_reviewer_feedback():
    """Sample requirements reviewer feedback"""
    return """## Review of Business Analysis

**Status**: Changes Requested

## Issues Found

### High Severity
- FR-1 lacks acceptance criteria

### Medium Severity
- US-1 needs more detail

_Processed by the requirements_reviewer agent_"""


@pytest.fixture
def sample_ba_revision():
    """Sample business analyst revision"""
    return """## Revision Notes
- Added acceptance criteria to FR-1
- Expanded US-1 with more detail

## Business Requirements Analysis (Revised)

FR-1: The system shall do X
  **Acceptance Criteria**: Given X, when Y, then Z

US-1: As a user, I want to X so that Y
  **Acceptance Criteria**:
  - Given A, when B, then C

_Processed by the business_analyst agent_"""


@pytest.fixture
def sample_reviewer_approval():
    """Sample requirements reviewer approval"""
    return """## Review Complete

All requirements have been addressed. The business analysis is comprehensive and ready to proceed.

**Status**: APPROVED

## Assessment

✅ All acceptance criteria defined
✅ User stories follow INVEST principles
✅ Requirements are clear and testable

_Processed by the requirements_reviewer agent_"""


@pytest.fixture
def simple_discussion(discussion_builder, sample_ba_output, sample_reviewer_feedback):
    """
    Simple discussion with 1 iteration:
    - BA initial output
    - Reviewer feedback
    """
    return (discussion_builder
        .with_id('D_test_simple')
        .with_number(1)
        .with_title('Simple Test Discussion')
        .with_comment('orchestrator-bot', sample_ba_output, is_ba=True)
        .with_comment('orchestrator-bot', sample_reviewer_feedback, is_reviewer=True)
        .build())


@pytest.fixture
def discussion_with_human_feedback(discussion_builder, sample_ba_output):
    """
    Discussion with BA output and human question
    """
    return (discussion_builder
        .with_id('D_test_feedback')
        .with_number(2)
        .with_title('Discussion with Feedback')
        .with_comment('orchestrator-bot', sample_ba_output, is_ba=True)
        .with_reply('tinkermonkey', 'Can you clarify requirement FR-1?', to_comment=0)
        .build())


@pytest.fixture
def multi_iteration_discussion(
    discussion_builder,
    sample_ba_output,
    sample_reviewer_feedback,
    sample_ba_revision
):
    """
    Discussion with 2 complete iterations:
    - BA initial → RR review → BA revision → RR review 2
    """
    return (discussion_builder
        .with_id('D_test_multi')
        .with_number(3)
        .with_title('Multi-Iteration Discussion')
        .with_comment('orchestrator-bot', sample_ba_output, is_ba=True)
        .with_comment('orchestrator-bot', sample_reviewer_feedback, is_reviewer=True)
        .with_comment('orchestrator-bot', sample_ba_revision, is_ba=True)
        .with_comment('orchestrator-bot', sample_reviewer_feedback, is_reviewer=True)
        .build())


# ============================================================================
# State Fixtures
# ============================================================================

@pytest.fixture
def initial_review_cycle_state(review_cycle_builder):
    """Review cycle state at initialization"""
    return (review_cycle_builder
        .for_issue(96)
        .in_repository('context-studio')
        .with_agents('business_analyst', 'requirements_reviewer')
        .for_project('context-studio', 'idea-development')
        .in_discussion('D_test123')
        .initialized()
        .build())


@pytest.fixture
def escalated_review_cycle_state(review_cycle_builder, sample_ba_output, sample_reviewer_feedback):
    """Review cycle state that has been escalated"""
    return (review_cycle_builder
        .for_issue(96)
        .in_repository('context-studio')
        .with_agents('business_analyst', 'requirements_reviewer')
        .for_project('context-studio', 'idea-development')
        .in_discussion('D_test123')
        .at_iteration(3)
        .with_maker_output(sample_ba_output, iteration=0)
        .with_review_output(sample_reviewer_feedback, iteration=1)
        .with_maker_output(sample_ba_output, iteration=2)
        .with_review_output(sample_reviewer_feedback, iteration=3)
        .escalated()
        .build())


# ============================================================================
# Fixture Loader
# ============================================================================

@pytest.fixture
def load_discussion_fixture(discussions_fixtures_dir):
    """
    Helper to load discussion fixtures from JSON files

    Usage:
        def test_something(load_discussion_fixture):
            discussion = load_discussion_fixture('discussion_95.json')
    """
    import json

    def _load(filename: str) -> Dict[str, Any]:
        filepath = discussions_fixtures_dir / filename
        if not filepath.exists():
            raise FileNotFoundError(f"Fixture not found: {filepath}")

        with open(filepath) as f:
            data = json.load(f)

        # Extract discussion node from repository wrapper if present
        if 'repository' in data and 'discussion' in data['repository']:
            return data['repository']['discussion']
        elif 'node' in data:
            return data['node']
        else:
            return data

    return _load


# ============================================================================
# Async Test Helpers
# ============================================================================

@pytest.fixture
def async_return():
    """
    Helper to create async functions that return a value

    Usage:
        mock_fn = async_return({'result': 'success'})
        result = await mock_fn()
    """
    def _create_async(value):
        async def _async_fn(*args, **kwargs):
            return value
        return _async_fn
    return _create_async
