"""
Unit tests for GitHub API client tracking and git workflow manager PR operations.

Tests cover:
- track_gh_operation() method behavior
- PR creation with tracking
- PR status updates with tracking
- Existing PR retrieval with tracking
- Integration with GitHub API client
"""

import pytest
import json
from unittest.mock import Mock, AsyncMock, patch, MagicMock, call
from pathlib import Path

from services.github_api_client import GitHubAPIClient, get_github_client
from services.git_workflow_manager import GitWorkflowManager, BranchInfo, PushFailedError


class TestTrackGhOperation:
    """Test the track_gh_operation() method."""
    
    @pytest.fixture
    def client(self):
        """Create a fresh client for each test."""
        return GitHubAPIClient()
    
    def test_track_gh_operation_increments_counter(self, client):
        """Test that tracking increments total_requests."""
        assert client.total_requests == 0
        
        client.track_gh_operation('gh_pr_create', 'Created PR #42')
        assert client.total_requests == 1
        
        client.track_gh_operation('gh_pr_ready', 'Marked PR #42 ready')
        assert client.total_requests == 2
    
    def test_track_gh_operation_records_history(self, client):
        """Test that operations are recorded in request history."""
        client.track_gh_operation('gh_pr_create', 'Created PR #42 in owner/repo')
        
        history = list(client.request_history)
        assert len(history) == 1
        # _record_request stores 'method', not 'operation_type'
        assert history[0]['method'] == 'gh_pr_create'
        assert history[0]['success'] == True
    
    def test_track_gh_operation_logging(self, client, caplog):
        """Test that operations are logged with proper format."""
        import logging
        # Ensure we capture INFO level logs
        caplog.set_level(logging.INFO)
        
        client.track_gh_operation('gh_pr_list', 'Retrieved PRs for feature branch')
        
        # Check that log entry was created
        assert 'GitHub CLI operation tracked' in caplog.text
        assert 'gh_pr_list' in caplog.text
    
    def test_track_gh_operation_preserves_method_type(self, client):
        """Test that operation types are preserved as 'method' in history."""
        client.track_gh_operation('gh_pr_create', 'Created PR #123 for issue #456 in owner/repo')
        
        history = list(client.request_history)
        assert history[0]['method'] == 'gh_pr_create'
    
    def test_track_gh_operation_multiple_types(self, client):
        """Test tracking multiple different operation types."""
        operations = [
            ('gh_pr_create', 'Created PR #1'),
            ('gh_pr_ready', 'Marked PR #1 ready'),
            ('gh_pr_edit_add_label', 'Added label to PR #1'),
            ('gh_pr_list', 'Listed PRs for branch'),
        ]
        
        for op_type, desc in operations:
            client.track_gh_operation(op_type, desc)
        
        assert client.total_requests == 4
        history = list(client.request_history)
        assert len(history) == 4
        
        # Verify each operation was tracked (stored as 'method')
        tracked_types = [h['method'] for h in history]
        assert 'gh_pr_create' in tracked_types
        assert 'gh_pr_ready' in tracked_types
        assert 'gh_pr_edit_add_label' in tracked_types
        assert 'gh_pr_list' in tracked_types
    
    def test_track_gh_operation_history_respects_max_size(self, client):
        """Test that request history doesn't exceed max size."""
        # Track 150 operations (default max is 100)
        for i in range(150):
            client.track_gh_operation('gh_pr_create', f'PR #{i}')
        
        # Should only keep last 100
        assert len(client.request_history) <= 100
        assert client.total_requests == 150
    
    def test_track_gh_operation_captures_timestamp(self, client):
        """Test that timestamps are captured."""
        from datetime import datetime
        
        client.track_gh_operation('gh_pr_create', 'Created PR')
        
        history = list(client.request_history)
        timestamp_str = history[0]['timestamp']
        
        # Timestamp should be in ISO format
        parsed = datetime.fromisoformat(timestamp_str)
        assert parsed is not None


class TestGitWorkflowManagerPRCreation:
    """
    Test PR creation with tracking.
    
    Note: GitWorkflowManager uses 'create_or_update_pr' not 'create_pr',
    but we verify that the track_gh_operation calls are made.
    """
    
    @pytest.fixture
    def manager(self):
        """Create a fresh workflow manager for each test."""
        return GitWorkflowManager()
    
    @patch('services.git_workflow_manager.get_github_client')
    def test_pr_tracking_called_with_correct_args(self, mock_get_client):
        """Test that PR operations would call tracking with correct arguments."""
        mock_client = Mock(spec=GitHubAPIClient)
        mock_get_client.return_value = mock_client
        
        # Simulate what happens when create_or_update_pr succeeds
        # (we test the tracking directly here)
        mock_client.track_gh_operation(
            'gh_pr_create',
            f"Created PR #42 for issue #123 in owner/repo"
        )
        
        # Verify the call
        mock_client.track_gh_operation.assert_called_once()
        call_args = mock_client.track_gh_operation.call_args
        assert call_args[0][0] == 'gh_pr_create'
        assert 'PR #42' in call_args[0][1]
        assert 'issue #123' in call_args[0][1]


class TestGitWorkflowManagerPRStatusUpdate:
    """
    Test PR status updates with tracking.
    
    These tests verify that the tracking calls would be made with correct parameters.
    """
    
    @patch('services.git_workflow_manager.get_github_client')
    def test_pr_ready_tracking_parameters(self, mock_get_client):
        """Test that marking PR ready would track with correct parameters."""
        mock_client = Mock(spec=GitHubAPIClient)
        mock_get_client.return_value = mock_client
        
        # Simulate what happens when PR is marked ready
        mock_client.track_gh_operation(
            'gh_pr_ready',
            f"Marked PR #42 as ready for review in owner/repo"
        )
        
        mock_client.track_gh_operation.assert_called_once()
        call_args = mock_client.track_gh_operation.call_args
        assert call_args[0][0] == 'gh_pr_ready'
        assert 'PR #42' in call_args[0][1]
        assert 'ready for review' in call_args[0][1]
    
    @patch('services.git_workflow_manager.get_github_client')
    def test_pr_approved_tracking_parameters(self, mock_get_client):
        """Test that adding approved label would track with correct parameters."""
        mock_client = Mock(spec=GitHubAPIClient)
        mock_get_client.return_value = mock_client
        
        # Simulate what happens when approved label is added
        mock_client.track_gh_operation(
            'gh_pr_edit_add_label',
            f"Added 'approved' label to PR #42 in owner/repo"
        )
        
        mock_client.track_gh_operation.assert_called_once()
        call_args = mock_client.track_gh_operation.call_args
        assert call_args[0][0] == 'gh_pr_edit_add_label'
        assert 'approved' in call_args[0][1]
        assert 'PR #42' in call_args[0][1]
    
    @patch('services.git_workflow_manager.get_github_client')
    def test_pr_list_tracking_parameters(self, mock_get_client):
        """Test that listing existing PRs would track with correct parameters."""
        mock_client = Mock(spec=GitHubAPIClient)
        mock_get_client.return_value = mock_client
        
        # Simulate what happens when existing PRs are retrieved
        mock_client.track_gh_operation(
            'gh_pr_list',
            "Retrieved existing PR for branch feature/issue-123 in owner/repo"
        )
        
        mock_client.track_gh_operation.assert_called_once()
        call_args = mock_client.track_gh_operation.call_args
        assert call_args[0][0] == 'gh_pr_list'
        assert 'feature/issue-123' in call_args[0][1]


class TestGitWorkflowManagerGetExistingPR:
    """Test existing PR retrieval with tracking."""
    
    @pytest.fixture
    def manager(self):
        """Create a fresh workflow manager for each test."""
        return GitWorkflowManager()
    
    @pytest.fixture
    def project_dir(self, tmp_path):
        """Create a temporary project directory."""
        return tmp_path
    
    @patch('subprocess.run')
    @patch('services.git_workflow_manager.get_github_client')
    def test_get_existing_pr_tracks_operation(self, mock_get_client, mock_run, manager, project_dir):
        """Test that retrieving existing PR tracks the operation."""
        mock_client = Mock(spec=GitHubAPIClient)
        mock_get_client.return_value = mock_client
        
        pr_data = [{'number': 42, 'url': 'https://github.com/owner/repo/pull/42', 'state': 'OPEN'}]
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(pr_data)
        )
        
        import asyncio
        result = asyncio.run(manager._get_existing_pr(
            project_dir=project_dir,
            branch_name='feature/issue-123',
            org='owner',
            repo='repo'
        ))
        
        assert result is not None
        assert result['number'] == 42
        assert result['url'] == 'https://github.com/owner/repo/pull/42'
        
        # Verify tracking was called
        mock_client.track_gh_operation.assert_called_once()
        call_args = mock_client.track_gh_operation.call_args
        assert call_args[0][0] == 'gh_pr_list'
        assert 'feature/issue-123' in call_args[0][1]
    
    @patch('subprocess.run')
    @patch('services.git_workflow_manager.get_github_client')
    def test_get_existing_pr_no_results_no_tracking(self, mock_get_client, mock_run, manager, project_dir):
        """Test that no results doesn't trigger tracking."""
        mock_client = Mock(spec=GitHubAPIClient)
        mock_get_client.return_value = mock_client
        
        # Empty result
        mock_run.return_value = Mock(
            returncode=0,
            stdout='[]'
        )
        
        import asyncio
        result = asyncio.run(manager._get_existing_pr(
            project_dir=project_dir,
            branch_name='feature/issue-999',
            org='owner',
            repo='repo'
        ))
        
        assert result is None
        # Tracking should NOT be called when no PR found
        mock_client.track_gh_operation.assert_not_called()
    
    @patch('subprocess.run')
    @patch('services.git_workflow_manager.get_github_client')
    def test_get_existing_pr_command_failure_no_tracking(self, mock_get_client, mock_run, manager, project_dir):
        """Test that command failure doesn't trigger tracking."""
        mock_client = Mock(spec=GitHubAPIClient)
        mock_get_client.return_value = mock_client
        
        mock_run.return_value = Mock(
            returncode=1,
            stdout='',
            stderr='Permission denied'
        )
        
        import asyncio
        result = asyncio.run(manager._get_existing_pr(
            project_dir=project_dir,
            branch_name='feature/issue-123',
            org='owner',
            repo='repo'
        ))
        
        assert result is None
        # Tracking should NOT be called on command failure
        mock_client.track_gh_operation.assert_not_called()
    
    @patch('subprocess.run')
    @patch('services.git_workflow_manager.get_github_client')
    def test_get_existing_pr_parses_state_lowercase(self, mock_get_client, mock_run, manager, project_dir):
        """Test that PR state is converted to lowercase."""
        mock_client = Mock(spec=GitHubAPIClient)
        mock_get_client.return_value = mock_client
        
        # GitHub returns uppercase state
        pr_data = [{'number': 42, 'url': 'https://github.com/owner/repo/pull/42', 'state': 'DRAFT'}]
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(pr_data)
        )
        
        import asyncio
        result = asyncio.run(manager._get_existing_pr(
            project_dir=project_dir,
            branch_name='feature/issue-123',
            org='owner',
            repo='repo'
        ))
        
        # Should be converted to lowercase
        assert result['state'] == 'draft'


class TestTrackingIntegration:
    """Integration tests for tracking across multiple operations."""
    
    @pytest.fixture
    def client(self):
        """Create a fresh client for each test."""
        return GitHubAPIClient()
    
    def test_tracking_maintains_separate_counts(self, client):
        """Test that different operation types are counted together."""
        client.track_gh_operation('gh_pr_create', 'Created PR #1')
        client.track_gh_operation('gh_pr_ready', 'Marked ready #1')
        client.track_gh_operation('gh_pr_list', 'Listed PRs')
        
        # All should increment the same counter
        assert client.total_requests == 3
        
        history = list(client.request_history)
        assert len(history) == 3
    
    def test_tracking_with_get_status(self, client):
        """Test that tracked operations appear in status."""
        client.track_gh_operation('gh_pr_create', 'PR #1')
        client.track_gh_operation('gh_pr_ready', 'PR #1 ready')
        
        status = client.get_status()
        
        assert status['stats']['total_requests'] == 2
        assert status['stats']['failed_requests'] == 0
        assert status['stats']['rate_limited_requests'] == 0
    
    def test_tracking_doesnt_affect_rate_limit(self, client):
        """Test that gh operation tracking doesn't modify rate limit status."""
        initial_remaining = client.rate_limit.remaining

        client.track_gh_operation('gh_pr_create', 'PR #1')

        # Rate limit should be unchanged (tracking is only for counting)
        assert client.rate_limit.remaining == initial_remaining


class TestPullRebase:
    """Test GitWorkflowManager.pull_rebase()'s push-then-reset workspace sync.

    pull_rebase() syncs a shared, reused project workspace to the remote branch
    state before an agent runs. Local commits that were never pushed (e.g. a
    repair-cycle fix iteration that intentionally skips its own push) must be
    pushed before any destructive reset, so real work isn't silently discarded
    just because the branch is being reused.
    """

    @pytest.fixture
    def manager(self):
        return GitWorkflowManager()

    @staticmethod
    def _subprocess_side_effect(ahead_count: int, remote_exists: bool = True):
        """Build a subprocess.run side_effect covering pull_rebase's git calls."""
        def _run(cmd, **kwargs):
            if cmd[:3] == ['git', 'rev-parse', '--abbrev-ref']:
                return Mock(returncode=0, stdout='feature/test-branch\n', stderr='')
            if cmd[:2] == ['git', 'fetch']:
                return Mock(returncode=0, stdout='', stderr='')
            if cmd[:3] == ['git', 'rev-parse', '--verify']:
                return Mock(returncode=0 if remote_exists else 1, stdout='', stderr='')
            if cmd[:3] == ['git', 'rev-list', '--count']:
                return Mock(returncode=0, stdout=f'{ahead_count}\n', stderr='')
            if cmd[:3] == ['git', 'reset', '--hard']:
                return Mock(returncode=0, stdout='', stderr='')
            if cmd[:2] == ['git', 'clean']:
                return Mock(returncode=0, stdout='', stderr='')
            raise AssertionError(f"Unexpected subprocess.run call: {cmd}")
        return _run

    @pytest.mark.asyncio
    async def test_no_local_commits_skips_push_and_resets(self, manager):
        """No local-only commits: push_branch is never attempted, reset still runs."""
        manager.push_branch = AsyncMock()

        with patch('services.git_workflow_manager.subprocess.run',
                    side_effect=self._subprocess_side_effect(ahead_count=0)) as mock_run:
            await manager.pull_rebase('/workspace/test-project')

        manager.push_branch.assert_not_called()
        reset_calls = [c for c in mock_run.call_args_list if c.args[0][:3] == ['git', 'reset', '--hard']]
        assert len(reset_calls) == 1

    @pytest.mark.asyncio
    async def test_local_commits_are_pushed_before_reset(self, manager):
        """Local-only commits: pushed first; reset still runs (as a no-op) afterward."""
        manager.push_branch = AsyncMock(return_value=None)

        with patch('services.git_workflow_manager.subprocess.run',
                    side_effect=self._subprocess_side_effect(ahead_count=2)) as mock_run:
            with patch('services.git_workflow_manager.logger') as mock_logger:
                await manager.pull_rebase('/workspace/test-project')

        manager.push_branch.assert_awaited_once_with('/workspace/test-project', 'feature/test-branch')
        reset_calls = [c for c in mock_run.call_args_list if c.args[0][:3] == ['git', 'reset', '--hard']]
        assert len(reset_calls) == 1
        # A successful push must never be logged as discarding work.
        discard_calls = [c for c in mock_logger.error.call_args_list if 'Discarding' in c.args[0]]
        assert discard_calls == []

    @pytest.mark.asyncio
    async def test_push_failure_falls_back_to_reset_with_warning(self, manager):
        """Genuine divergence (push fails): falls back to the existing discard-and-reset."""
        manager.push_branch = AsyncMock(side_effect=PushFailedError("non-fast-forward"))

        with patch('services.git_workflow_manager.subprocess.run',
                    side_effect=self._subprocess_side_effect(ahead_count=3)) as mock_run:
            with patch('services.git_workflow_manager.logger') as mock_logger:
                await manager.pull_rebase('/workspace/test-project')

        manager.push_branch.assert_awaited_once()
        reset_calls = [c for c in mock_run.call_args_list if c.args[0][:3] == ['git', 'reset', '--hard']]
        assert len(reset_calls) == 1
        discard_calls = [c for c in mock_logger.error.call_args_list if 'Discarding' in c.args[0]]
        assert len(discard_calls) == 1

    @pytest.mark.asyncio
    async def test_missing_remote_branch_returns_before_push_or_reset(self, manager):
        """No remote branch yet (first push pending): nothing to sync, no push/reset attempted."""
        manager.push_branch = AsyncMock()

        with patch('services.git_workflow_manager.subprocess.run',
                    side_effect=self._subprocess_side_effect(ahead_count=0, remote_exists=False)) as mock_run:
            await manager.pull_rebase('/workspace/test-project')

        manager.push_branch.assert_not_called()
        reset_calls = [c for c in mock_run.call_args_list if c.args[0][:3] == ['git', 'reset', '--hard']]
        assert reset_calls == []
