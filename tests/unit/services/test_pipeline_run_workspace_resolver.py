"""
Unit tests for PipelineRunManager.resolve_workspace() (issue #120, WI-A of #119).

Purely additive infrastructure: this resolver is not yet called from any
production dispatch path, so these tests exercise it directly, mocking out
FeatureBranchManager's git/GitHub calls and ProjectWorkspaceManager's worktree
creation. Covers the scenarios called out in the issue -- an existing
FeatureBranchState hit, cold-start fallback naming, idempotent re-calls -- plus
two findings from this item's own code review: the hard-failure-on-no-parent
behavior matching project_monitor.py's _resolve_epic_worktree_target(), and
detecting/correcting a resolved branch_name that diverges from what the epic
worktree is actually checked out to.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from services.pipeline_run import PipelineRun, PipelineRunManager


class MockElasticsearch:
    """Mock Elasticsearch client for testing"""

    def __init__(self):
        self.indexed_docs = []
        self.ilm = MagicMock()

    def index(self, index, id=None, document=None, body=None):
        doc = document or body
        self.indexed_docs.append({'index': index, 'id': id, 'body': doc})
        return {'result': 'updated'}


class MockRedis:
    """Mock Redis client for testing"""

    def __init__(self):
        self.data = {}

    def setex(self, key, ttl, value):
        self.data[key] = value

    def hset(self, key, field, value):
        self.data.setdefault(key, {})[field] = value

    def hget(self, key, field):
        if key in self.data and isinstance(self.data[key], dict):
            return self.data[key].get(field)
        return None


@pytest.fixture
def mock_elasticsearch():
    return MockElasticsearch()


@pytest.fixture
def mock_redis():
    return MockRedis()


@pytest.fixture
def pipeline_run_manager(mock_elasticsearch, mock_redis):
    """PipelineRunManager with mocked Redis/Elasticsearch dependencies"""
    with patch('services.pipeline_run.Elasticsearch', return_value=mock_elasticsearch), \
         patch('services.pipeline_run.redis.Redis', return_value=mock_redis):
        manager = PipelineRunManager()
        manager.es = mock_elasticsearch
        manager.redis = mock_redis
        return manager


@pytest.fixture
def pipeline_run():
    return PipelineRun(
        id="run-1",
        issue_number=42,
        issue_title="Sub-issue of an epic",
        issue_url="https://github.com/org/repo/issues/42",
        project="context-studio",
        board="Dev Board",
        started_at="2026-09-04T00:00:00Z",
    )


@pytest.fixture
def mock_github_integration():
    return Mock()


class TestResolveWorkspaceExistingBranchHit:
    """FeatureBranchState/cache already has a branch for the resolved epic."""

    @pytest.mark.asyncio
    async def test_reuses_existing_branch_without_inventing_a_new_name(
        self, pipeline_run_manager, pipeline_run, mock_github_integration
    ):
        from services.feature_branch_manager import feature_branch_manager
        from services.project_workspace import workspace_manager

        with patch.object(feature_branch_manager, 'get_parent_issue', new=AsyncMock(return_value=10)), \
             patch.object(feature_branch_manager, 'resolve_epic_branch_name',
                           return_value="feature/issue-10-existing-epic") as mock_resolve_branch, \
             patch.object(feature_branch_manager, 'create_feature_branch_name') as mock_create_name, \
             patch.object(workspace_manager, 'get_or_create_epic_worktree',
                           return_value="/workspace/.orchestrator/worktrees/context-studio/10") as mock_worktree, \
             patch.object(workspace_manager, '_current_worktree_branch',
                           return_value="feature/issue-10-existing-epic"):

            result = await pipeline_run_manager.resolve_workspace(
                pipeline_run, mock_github_integration, workspace_type='issues'
            )

        mock_resolve_branch.assert_called_once_with("context-studio", "10")
        mock_create_name.assert_not_called()
        mock_worktree.assert_called_once_with(
            "context-studio", "10", "feature/issue-10-existing-epic"
        )

        assert result is pipeline_run
        assert pipeline_run.branch_name == "feature/issue-10-existing-epic"
        assert pipeline_run.project_dir == "/workspace/.orchestrator/worktrees/context-studio/10"

        # Persisted back to Redis
        redis_key = pipeline_run_manager._get_redis_key(pipeline_run.id)
        assert redis_key in pipeline_run_manager.redis.data


class TestResolveWorkspaceColdStart:
    """No existing branch/state -- falls back to deterministic naming."""

    @pytest.mark.asyncio
    async def test_falls_back_to_create_feature_branch_name(
        self, pipeline_run_manager, pipeline_run, mock_github_integration
    ):
        from services.feature_branch_manager import feature_branch_manager
        from services.project_workspace import workspace_manager

        with patch.object(feature_branch_manager, 'get_parent_issue', new=AsyncMock(return_value=42)), \
             patch.object(feature_branch_manager, 'resolve_epic_branch_name', return_value=None), \
             patch.object(feature_branch_manager, 'create_feature_branch_name',
                           return_value="feature/issue-42-feature") as mock_create_name, \
             patch.object(workspace_manager, 'get_or_create_epic_worktree',
                           return_value="/workspace/.orchestrator/worktrees/context-studio/42") as mock_worktree, \
             patch.object(workspace_manager, '_current_worktree_branch',
                           return_value="feature/issue-42-feature"):

            result = await pipeline_run_manager.resolve_workspace(
                pipeline_run, mock_github_integration, workspace_type='issues'
            )

        mock_create_name.assert_called_once_with(42, "")
        mock_worktree.assert_called_once_with(
            "context-studio", "42", "feature/issue-42-feature"
        )

        assert result.branch_name == "feature/issue-42-feature"
        assert result.project_dir == "/workspace/.orchestrator/worktrees/context-studio/42"


class TestResolveWorkspaceIdempotent:
    """A second call against an already-resolved run must not re-resolve."""

    @pytest.mark.asyncio
    async def test_second_call_returns_cached_values_without_touching_git_or_github(
        self, pipeline_run_manager, pipeline_run, mock_github_integration
    ):
        from services.feature_branch_manager import feature_branch_manager
        from services.project_workspace import workspace_manager

        with patch.object(feature_branch_manager, 'get_parent_issue', new=AsyncMock(return_value=42)), \
             patch.object(feature_branch_manager, 'resolve_epic_branch_name', return_value=None), \
             patch.object(feature_branch_manager, 'create_feature_branch_name',
                           return_value="feature/issue-42-feature"), \
             patch.object(workspace_manager, 'get_or_create_epic_worktree',
                           return_value="/workspace/.orchestrator/worktrees/context-studio/42"), \
             patch.object(workspace_manager, '_current_worktree_branch',
                           return_value="feature/issue-42-feature"):

            first = await pipeline_run_manager.resolve_workspace(
                pipeline_run, mock_github_integration, workspace_type='issues'
            )

        assert first.branch_name == "feature/issue-42-feature"

        with patch.object(feature_branch_manager, 'get_parent_issue', new=AsyncMock()) as mock_parent, \
             patch.object(feature_branch_manager, 'resolve_epic_branch_name') as mock_resolve_branch, \
             patch.object(feature_branch_manager, 'create_feature_branch_name') as mock_create_name, \
             patch.object(workspace_manager, 'get_or_create_epic_worktree') as mock_worktree, \
             patch.object(workspace_manager, '_current_worktree_branch') as mock_current_branch:

            second = await pipeline_run_manager.resolve_workspace(
                pipeline_run, mock_github_integration, workspace_type='issues'
            )

        mock_parent.assert_not_called()
        mock_resolve_branch.assert_not_called()
        mock_create_name.assert_not_called()
        mock_worktree.assert_not_called()
        mock_current_branch.assert_not_called()

        assert second is pipeline_run
        assert second.branch_name == "feature/issue-42-feature"
        assert second.project_dir == "/workspace/.orchestrator/worktrees/context-studio/42"


class TestResolveWorkspaceHybridUsesLenientFallback:
    """
    'hybrid', unlike 'issues', has no precedent guaranteeing a parent issue (a
    directly-dispatched epic or standalone issue can legitimately have none) --
    it must use resolve_epic_id()'s established lenient fallback, not the
    'issues'-only hard-fail. Code-review finding from this item's own review pass.
    """

    @pytest.mark.asyncio
    async def test_hybrid_falls_back_to_resolve_epic_id_instead_of_hard_failing(
        self, pipeline_run_manager, pipeline_run, mock_github_integration
    ):
        from services.feature_branch_manager import feature_branch_manager
        from services.project_workspace import workspace_manager

        with patch.object(feature_branch_manager, 'resolve_epic_id', new=AsyncMock(return_value="42")) as mock_epic_id, \
             patch.object(feature_branch_manager, 'get_parent_issue', new=AsyncMock()) as mock_parent, \
             patch.object(feature_branch_manager, 'resolve_epic_branch_name', return_value=None), \
             patch.object(feature_branch_manager, 'create_feature_branch_name',
                           return_value="feature/issue-42-feature"), \
             patch.object(workspace_manager, 'get_or_create_epic_worktree',
                           return_value="/workspace/.orchestrator/worktrees/context-studio/42"), \
             patch.object(workspace_manager, '_current_worktree_branch',
                           return_value="feature/issue-42-feature"):

            result = await pipeline_run_manager.resolve_workspace(
                pipeline_run, mock_github_integration, workspace_type='hybrid'
            )

        mock_epic_id.assert_called_once()
        mock_parent.assert_not_called()
        assert result.branch_name == "feature/issue-42-feature"


class TestResolveWorkspaceScopeGuard:
    """Only 'issues'/'hybrid' workspace types are resolved."""

    @pytest.mark.asyncio
    async def test_discussions_workspace_type_is_a_noop(
        self, pipeline_run_manager, pipeline_run, mock_github_integration
    ):
        from services.feature_branch_manager import feature_branch_manager

        with patch.object(feature_branch_manager, 'get_parent_issue', new=AsyncMock()) as mock_parent:
            result = await pipeline_run_manager.resolve_workspace(
                pipeline_run, mock_github_integration, workspace_type='discussions'
            )

        mock_parent.assert_not_called()
        assert result is pipeline_run
        assert result.branch_name is None
        assert result.project_dir is None


class TestResolveWorkspaceNoParentHardFails:
    """
    Missing parent issue must hard-fail, matching project_monitor.py's
    _resolve_epic_worktree_target() -- silently falling back to the sub-issue's
    own number (resolve_epic_id()'s behavior, deliberately not used here) would
    defeat cross-sub-issue isolation. Code-review finding from this item's own
    review pass.
    """

    @pytest.mark.asyncio
    async def test_raises_value_error_when_no_parent_issue_found(
        self, pipeline_run_manager, pipeline_run, mock_github_integration
    ):
        from services.feature_branch_manager import feature_branch_manager
        from services.project_workspace import workspace_manager

        with patch.object(feature_branch_manager, 'get_parent_issue', new=AsyncMock(return_value=None)), \
             patch.object(workspace_manager, 'get_or_create_epic_worktree') as mock_worktree:

            with pytest.raises(ValueError, match="could not resolve a parent epic issue"):
                await pipeline_run_manager.resolve_workspace(
                    pipeline_run, mock_github_integration, workspace_type='issues'
                )

        mock_worktree.assert_not_called()
        assert pipeline_run.branch_name is None
        assert pipeline_run.project_dir is None


class TestResolveWorkspacePersistenceFailureReverts:
    """
    A persistence failure must revert the in-memory branch_name/project_dir
    mutation -- otherwise a caller that catches the exception and retries
    resolve_workspace() against the SAME object hits the idempotency guard and
    silently never persists. Code-review finding from this item's own review pass.
    """

    @pytest.mark.asyncio
    async def test_retry_after_persistence_failure_actually_re_resolves(
        self, pipeline_run_manager, pipeline_run, mock_github_integration
    ):
        from services.feature_branch_manager import feature_branch_manager
        from services.project_workspace import workspace_manager

        with patch.object(feature_branch_manager, 'get_parent_issue', new=AsyncMock(return_value=42)), \
             patch.object(feature_branch_manager, 'resolve_epic_branch_name', return_value=None), \
             patch.object(feature_branch_manager, 'create_feature_branch_name',
                           return_value="feature/issue-42-feature"), \
             patch.object(workspace_manager, 'get_or_create_epic_worktree',
                           return_value="/workspace/.orchestrator/worktrees/context-studio/42"), \
             patch.object(workspace_manager, '_current_worktree_branch',
                           return_value="feature/issue-42-feature"), \
             patch.object(pipeline_run_manager, 'update_resolved_workspace',
                           side_effect=ConnectionError("redis down")):

            with pytest.raises(ConnectionError):
                await pipeline_run_manager.resolve_workspace(
                    pipeline_run, mock_github_integration, workspace_type='issues'
                )

        assert pipeline_run.branch_name is None
        assert pipeline_run.project_dir is None

        # A retry against the same object must actually re-resolve, not
        # short-circuit on the (now-reverted) idempotency guard.
        with patch.object(feature_branch_manager, 'get_parent_issue', new=AsyncMock(return_value=42)), \
             patch.object(feature_branch_manager, 'resolve_epic_branch_name', return_value=None), \
             patch.object(feature_branch_manager, 'create_feature_branch_name',
                           return_value="feature/issue-42-feature"), \
             patch.object(workspace_manager, 'get_or_create_epic_worktree',
                           return_value="/workspace/.orchestrator/worktrees/context-studio/42") as mock_worktree, \
             patch.object(workspace_manager, '_current_worktree_branch',
                           return_value="feature/issue-42-feature"):

            result = await pipeline_run_manager.resolve_workspace(
                pipeline_run, mock_github_integration, workspace_type='issues'
            )

        mock_worktree.assert_called_once()
        assert result.branch_name == "feature/issue-42-feature"


class TestResolveWorkspaceBranchDivergence:
    """
    get_or_create_epic_worktree() can silently adopt a pre-existing worktree
    that's actually on a different branch than requested (e.g. after a restart
    with an empty in-memory cache). resolve_workspace() must persist the
    worktree's REAL branch, not the locally-resolved one, and log a warning.
    Code-review finding from this item's own review pass.
    """

    @pytest.mark.asyncio
    async def test_persists_the_worktree_s_actual_branch_on_mismatch(
        self, pipeline_run_manager, pipeline_run, mock_github_integration
    ):
        from services.feature_branch_manager import feature_branch_manager
        from services.project_workspace import workspace_manager

        with patch.object(feature_branch_manager, 'get_parent_issue', new=AsyncMock(return_value=42)), \
             patch.object(feature_branch_manager, 'resolve_epic_branch_name', return_value=None), \
             patch.object(feature_branch_manager, 'create_feature_branch_name',
                           return_value="feature/issue-42-requested"), \
             patch.object(workspace_manager, 'get_or_create_epic_worktree',
                           return_value="/workspace/.orchestrator/worktrees/context-studio/42"), \
             patch.object(workspace_manager, '_current_worktree_branch',
                           return_value="feature/issue-42-actually-on-disk"):

            result = await pipeline_run_manager.resolve_workspace(
                pipeline_run, mock_github_integration, workspace_type='issues'
            )

        assert result.branch_name == "feature/issue-42-actually-on-disk"
        assert result.project_dir == "/workspace/.orchestrator/worktrees/context-studio/42"
