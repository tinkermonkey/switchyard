"""
Unit tests for the retry-with-backoff added to ProjectMonitor._check_pr_ready_on_issue_exit()
around the parent-issue GitHub lookup (Step 3).

Background: this is a one-shot, event-triggered check with no other retry path. If
github.get_issue(parent_issue_number) fails transiently (e.g. the GitHub API rate-limit
circuit breaker is open at that exact moment), the parent issue could be stranded
permanently, since the sub-issue that triggered the check will never re-exit the
pipeline again. See ScheduledTasksService._sweep_orphaned_parents for the periodic
safety net that also covers this scenario.
"""
import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import Mock, AsyncMock, patch
from services.project_monitor import ProjectMonitor
from config.manager import ConfigManager


class TestCheckPrReadyOnIssueExitRetry:
    """Test the get_issue() retry loop inside _check_pr_ready_on_issue_exit()."""

    @pytest.fixture
    def project_config(self):
        config = Mock()
        config.github = {'org': 'test-org', 'repo': 'test-repo'}
        return config

    @pytest.fixture
    def mock_config_manager(self, project_config):
        config_manager = Mock(spec=ConfigManager)
        config_manager.list_projects.return_value = []
        config_manager.get_project_config.return_value = project_config
        return config_manager

    @pytest.fixture
    def monitor(self, mock_config_manager):
        return ProjectMonitor(Mock(), mock_config_manager)

    async def _run(self, monitor, get_issue_side_effect):
        """Invoke the real method with feature_branch_manager/GitHubIntegration mocked,
        stopping cleanly right after the retry loop by having _get_sub_issues_from_parent
        return an empty list (which triggers a debug-log early return one step later)."""
        mock_github = AsyncMock()
        mock_github.get_issue.side_effect = get_issue_side_effect

        with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.github_integration.GitHubIntegration', return_value=mock_github), \
             patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:

            mock_fbm.get_parent_issue = AsyncMock(return_value=822)
            mock_fbm._get_sub_issues_from_parent = AsyncMock(return_value=[])

            await monitor._check_pr_ready_on_issue_exit('test-project', 826, 'Staged')

        return {
            'get_issue': mock_github.get_issue,
            'sub_issues': mock_fbm._get_sub_issues_from_parent,
            'sleep': mock_sleep,
        }

    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt_no_retry(self, monitor):
        """No retries or sleeps when the first get_issue() call succeeds."""
        result = await self._run(monitor, get_issue_side_effect=[{'number': 822}])

        assert result['get_issue'].call_count == 1
        result['sleep'].assert_not_called()
        # Proceeded past Step 3 into sub-issue lookup
        result['sub_issues'].assert_called_once()

    @pytest.mark.asyncio
    async def test_retries_with_backoff_then_succeeds(self, monitor):
        """Two transient failures (None) followed by success: 3 attempts, 2 backoff sleeps
        (5s, then 10s), and processing continues afterward."""
        result = await self._run(
            monitor, get_issue_side_effect=[None, None, {'number': 822}]
        )

        assert result['get_issue'].call_count == 3
        assert result['sleep'].call_args_list == [
            (( 5,), {}),
            ((10,), {}),
        ]
        result['sub_issues'].assert_called_once()

    @pytest.mark.asyncio
    async def test_exhausts_retries_and_skips_cleanly(self, monitor):
        """All 3 attempts fail: exactly 3 get_issue() calls, exactly 2 backoff sleeps
        (none after the final attempt), and the function returns early without
        crashing or proceeding to sub-issue verification."""
        result = await self._run(monitor, get_issue_side_effect=[None, None, None])

        assert result['get_issue'].call_count == 3
        assert result['sleep'].call_count == 2
        result['sub_issues'].assert_not_called()
