"""
Pytest configuration and shared fixtures

This file provides common fixtures and configuration for all tests.
"""

import pytest
import asyncio
import os
from pathlib import Path
from typing import Dict, Any

# Import test utilities
from tests.mocks.github_mock import MockGitHubApp, MockGitHubIntegration, MockAgentExecutor
from tests.utils.builders import ReviewCycleStateBuilder, DiscussionBuilder, TaskContextBuilder


# ============================================================================
# Pytest Configuration
# ============================================================================

def pytest_configure(config):
    """Register custom markers and load environment"""
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

    # Load environment variables from .env file for integration tests
    # This ensures API keys and other config are available
    from config.environment import Environment
    try:
        env = Environment()
        # Export environment variables if they're configured
        if env.claude_code_oauth_token:
            os.environ['CLAUDE_CODE_OAUTH_TOKEN'] = env.claude_code_oauth_token.get_secret_value()
        if env.anthropic_api_key:
            os.environ['ANTHROPIC_API_KEY'] = env.anthropic_api_key.get_secret_value()
        if env.github_token:
            os.environ['GITHUB_TOKEN'] = env.github_token.get_secret_value()
    except Exception as e:
        # Don't fail tests if .env is missing - some tests don't need it
        pass


# ============================================================================
# Container-gated test reporting
# ============================================================================

# The reason string ~60 test files pass to pytest.skip(..., allow_module_level=True)
# when /app is absent. They import agents/__init__.py and other modules that
# genuinely cannot import outside the orchestrator container (see CLAUDE.md,
# "Docker-only imports"), so the gate itself is correct.
CONTAINER_ONLY_SKIP_REASON = "Requires Docker container environment"

# What those files test for.
ORCHESTRATOR_CONTAINER_MARKER = '/app'


def running_in_orchestrator_container():
    return os.path.isdir(ORCHESTRATOR_CONTAINER_MARKER)


def pytest_report_header(config):
    """
    Say up front whether the container-gated portion of the suite can run at all
    (#140 item 37).

    A host run skips those files wholesale, and pytest's summary line reports
    those skips indistinguishably from any other -- so the run looks green while
    a large share of it never executed. It is now stated before the first test.
    """
    if running_in_orchestrator_container():
        return f"orchestrator container: yes ({ORCHESTRATOR_CONTAINER_MARKER} present)"
    return (
        f"orchestrator container: NO ({ORCHESTRATOR_CONTAINER_MARKER} absent) -- every "
        "container-gated test file will be SKIPPED, not run. For full coverage: "
        "docker exec -w /workspace/switchyard switchyard-orchestrator-1 python -m pytest <path>"
    )


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """
    Count the container-gated files that never ran, at the bottom of the report
    where the green/red verdict is (#140 item 37).

    Deliberately reporting, not failing: a host run of a scoped subset is a
    legitimate thing to do, and turning it into an error would just teach people
    to pass -p no:cacheprovider-style opt-outs. What it must not do is look like
    a clean full pass.
    """
    if running_in_orchestrator_container():
        return

    gated = [
        report for report in terminalreporter.stats.get('skipped', [])
        if CONTAINER_ONLY_SKIP_REASON in str(getattr(report, 'longrepr', ''))
    ]
    if not gated:
        return

    terminalreporter.write_sep(
        '=', f"{len(gated)} container-gated test file(s) did NOT run", red=True
    )
    terminalreporter.write_line(
        f"These skipped because {ORCHESTRATOR_CONTAINER_MARKER} is absent, not because they "
        "passed. This result does not cover them."
    )
    terminalreporter.write_line(
        "Re-run inside the orchestrator container: "
        "docker exec -w /workspace/switchyard switchyard-orchestrator-1 python -m pytest <path>"
    )


# ============================================================================
# Project config isolation
# ============================================================================

# Tracked home of the suite's fake project configs. See #140 items 35/38.
FIXTURE_PROJECTS_DIR = Path(__file__).parent / 'fixtures' / 'config' / 'projects'


def build_project_config_overlay(overlay_dir: Path, fixture_dir: Path,
                                 deployment_dir: Path) -> Path:
    """
    Populate `overlay_dir` with a symlink per project config: every fixture in
    `fixture_dir` first, then every real config in `deployment_dir` that a
    fixture has not already claimed by name.

    ADDITIVE, deliberately (#154/WI-9 review). Replacing projects_dir outright
    made the fixture a silent, global, opt-out-less redirect: any test that asks
    ConfigManager for a real deployment project by name -- e.g.
    tests/integration/test_readonly_filesystem.py's
    get_project_agent_config('context-studio', ...) -- got a FileNotFoundError
    naming a path under tests/fixtures/, with nothing to suggest a session
    fixture had moved the directory out from under it. Overlaying keeps the
    fixtures reachable without taking the real ones away.

    Fixtures shadow same-named deployment files rather than the reverse: a stray
    config/projects/test_project.yaml left behind in a deployment (exactly what
    #162 is about) must not be what the suite reads.

    Snapshot, not a live view: a config written into `deployment_dir` after this
    runs is not picked up. Nothing in the suite does that, and a live view would
    need a projects_dir shim rather than a real directory.
    """
    overlay_dir.mkdir(parents=True, exist_ok=True)
    for source_dir in (fixture_dir, deployment_dir):
        if not source_dir.is_dir():
            continue
        for source in sorted(source_dir.glob('*.yaml')):
            target = overlay_dir / source.name
            if target.exists() or target.is_symlink():
                continue
            target.symlink_to(source.resolve())
    return overlay_dir


@pytest.fixture(scope="session", autouse=True)
def isolated_project_configs(tmp_path_factory):
    """
    Point the process-wide ConfigManager at a session overlay of
    tests/fixtures/config/projects/ over config/projects/ (#140 items 35/38).

    Two problems, one root cause. `config/projects/` is gitignored AS A
    DIRECTORY, so the `test_project.yaml` / `test-project.yaml` fixtures several
    test files need could not be committed there -- every fresh checkout and
    every new worktree failed those tests until somebody hand-copied the files
    in. And because that directory is also the REAL deployment's project config
    directory, the copies that did exist were loaded by the running orchestrator
    as ordinary projects: a 166-day-old stale `test-project/planning` pipeline
    lock re-evaluated on every startup, board reconciliation and workspace init
    for a project that does not exist, and real dev_environment_setup /
    dev_environment_verifier agent runs dispatched against
    /workspace/test-project -- burning tokens and container slots, with their
    output addressed to issue #0 (see #162, and #149's FAILSAFE branch guard,
    which is what finally made those runs visible by refusing to commit them).

    The same file cannot be both test input that must exist and deployment
    config that must not. So the fixtures live here, tracked, and this fixture
    redirects lookups at them; `config/projects/` is left to real projects only.

    Session-scoped and autouse rather than opt-in: the tests that need it reach
    config_manager indirectly (PipelineQueueManager._get_pipeline_trigger_column()
    -> config_manager.get_project_config(self.project_name)), so there is no
    call site to opt in at, and a test that forgot to would silently read the
    deployment's real projects instead.

    Autouse also means it applies to tests that never asked for it, which is why
    it OVERLAYS rather than replaces -- see build_project_config_overlay(). The
    real configs stay reachable by name; only the two fixture projects are added.

    Only the singleton is redirected. A test constructing its own
    ConfigManager() still gets `config/projects/` directly, which on a clean
    checkout is empty -- the same answer for the fixture projects, since both are
    `hidden: true` and so never appear in list_visible_projects() either way.
    """
    from config.manager import config_manager

    original = config_manager.projects_dir
    overlay = build_project_config_overlay(
        tmp_path_factory.mktemp('project-configs'), FIXTURE_PROJECTS_DIR, original
    )
    config_manager.projects_dir = overlay
    config_manager.reload_config()
    try:
        yield overlay
    finally:
        config_manager.projects_dir = original
        config_manager.reload_config()


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

All acceptance criteria defined
User stories follow INVEST principles
Requirements are clear and testable

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


# ============================================================================
# Test Data Cleanup
# ============================================================================

# All project names used exclusively in tests — safe to purge completely.
_TEST_PROJECT_NAMES = ["test-project", "test_project", "test-proj"]

# ES indices that carry a top-level `project` field written by tests.
_TEST_ES_INDICES = [
    "pipeline-runs-*",
    "decision-events-*",
    "agent-events-*",
    "orchestrator-test-cycle-records",
    "orchestrator-task-metrics-*",
    "orchestrator-quality-metrics-*",
]


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_data():
    """
    Purge Elasticsearch and Redis data belonging to test-only projects.

    Runs once before and once after the entire test session so leftover data
    from a previous crashed run is also removed. Unit tests that mock ES/Redis
    are unaffected — both cleanup calls are no-ops when the services are
    unreachable.
    """
    _purge_test_data()
    yield
    _purge_test_data()


def _purge_test_data():
    _purge_elasticsearch()
    _purge_redis()


def _purge_elasticsearch():
    try:
        import os
        from elasticsearch import Elasticsearch
        es_url = os.environ.get("ELASTICSEARCH_URL", "http://elasticsearch:9200")
        es = Elasticsearch(es_url, request_timeout=5)
        if not es.ping():
            return
        query = {"query": {"terms": {"project": _TEST_PROJECT_NAMES}}}
        for index in _TEST_ES_INDICES:
            try:
                es.delete_by_query(
                    index=index,
                    body=query,
                    ignore_unavailable=True,
                    refresh=True,
                )
            except Exception:
                pass
    except Exception:
        pass


def _purge_redis():
    try:
        import os
        import redis as redis_lib
        from urllib.parse import urlparse
        redis_url = os.environ.get("REDIS_URL", "redis://redis:6379")
        parsed = urlparse(redis_url)
        r = redis_lib.Redis(host=parsed.hostname, port=parsed.port or 6379, socket_timeout=2)
        r.ping()
        for project in _TEST_PROJECT_NAMES:
            cursor = 0
            while True:
                cursor, keys = r.scan(cursor, match=f"*{project}*", count=100)
                if keys:
                    r.delete(*keys)
                if cursor == 0:
                    break

        # GitHubAPIClient mirrors real-response-derived rate limit readings
        # to a couple of small *global* Redis keys (not namespaced by
        # project, since GitHub's quota is account-wide) that the live
        # dashboard reads directly - see get_shared_rate_limit_status().
        # A unit test that exercises graphql()/rest()/http_request() with a
        # mocked subprocess/response still runs the real mirror code, which
        # would otherwise leave fabricated numbers sitting in the same keys
        # production reads from. Purge them explicitly since they don't
        # match the project-name pattern above.
        try:
            from services.github_api_client import RATE_LIMIT_REDIS_KEYS
            r.delete(*RATE_LIMIT_REDIS_KEYS.values())
        except Exception:
            pass
    except Exception:
        pass
