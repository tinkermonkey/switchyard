"""
Unit tests for ReviewCycleState.epic_id resolution (issue #47).

Covers ReviewCycleExecutor._resolve_epic_id_for_cycle(): the best-effort,
non-raising resolution of a sub-issue's parent epic, stored on
ReviewCycleState.epic_id for #48 to consume later. Per #46's gating decision
(EPIC_WORKTREE_SAFE_WORKSPACE_TYPES), nothing in review_cycle.py's
get_project_dir() call sites consumes epic_id yet -- these tests only cover
the resolution/storage/persistence of the value itself, not any directory
resolution behavior change.

NOTE: this method is deliberately not called from any ReviewCycleState
construction path today (pass-1 review of #47 found that calling it eagerly
put a real blocking network call ahead of the active_cycles dict write,
widening a concurrent-dispatch race for a value nothing consumes yet) -- these
tests exercise the method directly, not via start_review_cycle/resume_review_cycle.
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch

from services.review_cycle import ReviewCycleExecutor, ReviewCycleState


def make_state(**overrides):
    """Create a ReviewCycleState with sensible defaults for testing."""
    defaults = {
        'issue_number': 101,
        'repository': 'test-org/test-repo',
        'maker_agent': 'maker',
        'reviewer_agent': 'reviewer',
        'max_iterations': 3,
        'project_name': 'test-project',
        'board_name': 'dev',
        'workspace_type': 'issues',
    }
    defaults.update(overrides)
    return ReviewCycleState(**defaults)


@pytest.fixture
def executor():
    return ReviewCycleExecutor()


class TestReviewCycleStateEpicIdField:
    """ReviewCycleState carries epic_id, defaults to None, and persists."""

    def test_epic_id_defaults_to_none(self):
        state = make_state()
        assert state.epic_id is None

    def test_to_dict_includes_epic_id(self):
        state = make_state()
        state.epic_id = '42'
        assert state.to_dict()['epic_id'] == '42'

    def test_from_dict_restores_epic_id(self):
        state = make_state()
        state.epic_id = '42'
        restored = ReviewCycleState.from_dict(state.to_dict())
        assert restored.epic_id == '42'

    def test_from_dict_defaults_epic_id_to_none_when_absent(self):
        """Backward compatibility: state persisted before #47 has no 'epic_id'
        key in its dict at all -- from_dict must not raise a KeyError."""
        state = make_state()
        data = state.to_dict()
        del data['epic_id']

        restored = ReviewCycleState.from_dict(data)

        assert restored.epic_id is None


class TestResolveEpicIdForCycle:
    """ReviewCycleExecutor._resolve_epic_id_for_cycle(): best-effort resolution."""

    @pytest.mark.asyncio
    async def test_resolves_epic_id_on_success(self, executor):
        """A sub-issue with a parent resolves cycle_state.epic_id to that parent."""
        state = make_state(issue_number=101)

        mock_github_integration = Mock()
        with patch.object(executor, '_get_github_integration', return_value=mock_github_integration), \
             patch('services.feature_branch_manager.feature_branch_manager.resolve_epic_id',
                   new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = '42'

            await executor._resolve_epic_id_for_cycle(state)

            assert state.epic_id == '42'
            mock_resolve.assert_awaited_once_with(
                mock_github_integration, 101, project='test-project'
            )

    @pytest.mark.asyncio
    async def test_resolution_failure_falls_back_to_none_and_does_not_raise(self, executor):
        """A GitHub error (or any other failure) during resolution must be
        swallowed -- epic_id stays None and the cycle continues unaffected.
        This must never break a review cycle that worked before #47."""
        state = make_state(issue_number=101)

        with patch.object(executor, '_get_github_integration', return_value=Mock()), \
             patch('services.feature_branch_manager.feature_branch_manager.resolve_epic_id',
                   new_callable=AsyncMock) as mock_resolve:
            mock_resolve.side_effect = RuntimeError("simulated GitHub API failure")

            await executor._resolve_epic_id_for_cycle(state)  # must not raise

            assert state.epic_id is None

    @pytest.mark.asyncio
    async def test_missing_repo_owner_falls_back_to_none(self, executor):
        """_get_github_integration() raises ValueError when the project's repo
        owner can't be determined -- also swallowed, not propagated."""
        state = make_state(issue_number=101)

        with patch.object(executor, '_get_github_integration',
                           side_effect=ValueError("Cannot determine repo owner")):
            await executor._resolve_epic_id_for_cycle(state)  # must not raise

            assert state.epic_id is None

    @pytest.mark.asyncio
    async def test_is_idempotent_when_already_resolved(self, executor):
        """A cycle restored from persisted state (or resolved earlier in the
        same construction path) already has epic_id set -- must not re-resolve
        (avoids a redundant GitHub call each time a shared construction path
        runs for the same cycle)."""
        state = make_state(issue_number=101)
        state.epic_id = '7'  # already resolved

        with patch('services.feature_branch_manager.feature_branch_manager.resolve_epic_id',
                   new_callable=AsyncMock) as mock_resolve:
            await executor._resolve_epic_id_for_cycle(state)

            mock_resolve.assert_not_awaited()
            assert state.epic_id == '7'

    @pytest.mark.asyncio
    async def test_standalone_issue_resolves_to_self(self, executor):
        """An issue with no parent (standalone, or an epic dispatched
        directly) resolves epic_id to its own issue number -- mirrors
        FeatureBranchManager.resolve_epic_id()'s self-fallback semantics."""
        state = make_state(issue_number=200)

        with patch.object(executor, '_get_github_integration', return_value=Mock()), \
             patch('services.feature_branch_manager.feature_branch_manager.resolve_epic_id',
                   new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = '200'

            await executor._resolve_epic_id_for_cycle(state)

            assert state.epic_id == '200'
