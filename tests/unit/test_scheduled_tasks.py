"""
Unit tests for scheduled tasks service
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime

from services.scheduled_tasks import ScheduledTasksService


@pytest.fixture
def scheduled_tasks_service():
    """Create a ScheduledTasksService instance"""
    return ScheduledTasksService()


class TestSchedulerLifecycle:
    """Test scheduler start/stop lifecycle"""

    @pytest.mark.asyncio
    async def test_start_scheduler(self, scheduled_tasks_service):
        """Test starting the scheduler"""
        scheduled_tasks_service.start()

        assert scheduled_tasks_service.running is True
        assert scheduled_tasks_service.scheduler.running is True

        # Verify jobs were added
        jobs = scheduled_tasks_service.scheduler.get_jobs()
        job_ids = [job.id for job in jobs]

        assert 'cleanup_orphaned_branches' in job_ids
        assert 'check_stale_branches' in job_ids

        # Cleanup
        scheduled_tasks_service.stop()

    @pytest.mark.asyncio
    async def test_stop_scheduler(self, scheduled_tasks_service):
        """Test stopping the scheduler"""
        scheduled_tasks_service.start()
        assert scheduled_tasks_service.running is True

        scheduled_tasks_service.stop()
        assert scheduled_tasks_service.running is False

    @pytest.mark.asyncio
    async def test_start_already_running(self, scheduled_tasks_service, caplog):
        """Test starting scheduler when already running"""
        scheduled_tasks_service.start()
        scheduled_tasks_service.start()  # Second start

        assert "already running" in caplog.text

        # Cleanup
        scheduled_tasks_service.stop()

    def test_stop_not_running(self, scheduled_tasks_service):
        """Test stopping scheduler when not running"""
        # Should not raise error
        scheduled_tasks_service.stop()
        assert scheduled_tasks_service.running is False


class TestScheduledJobConfiguration:
    """Test scheduled job configurations"""

    @pytest.mark.asyncio
    async def test_cleanup_job_schedule(self, scheduled_tasks_service):
        """Test cleanup job is scheduled for 2 AM daily"""
        scheduled_tasks_service.start()

        cleanup_job = scheduled_tasks_service.scheduler.get_job('cleanup_orphaned_branches')

        assert cleanup_job is not None
        assert cleanup_job.name == 'Cleanup orphaned feature branches'

        # Verify trigger (CronTrigger)
        trigger = cleanup_job.trigger
        # Field 5 is hour, field 6 is minute
        hour_field = trigger.fields[5]
        minute_field = trigger.fields[6]
        
        # Check that hour is set to 2
        assert len(hour_field.expressions) == 1
        assert hour_field.expressions[0].first == 2
        
        # Check that minute is set to 0
        assert len(minute_field.expressions) == 1
        assert minute_field.expressions[0].first == 0

        # Cleanup
        scheduled_tasks_service.stop()

    @pytest.mark.asyncio
    async def test_stale_check_job_schedule(self, scheduled_tasks_service):
        """Test stale check job is scheduled for 9 AM daily"""
        scheduled_tasks_service.start()

        stale_job = scheduled_tasks_service.scheduler.get_job('check_stale_branches')

        assert stale_job is not None
        assert stale_job.name == 'Check for stale feature branches'

        # Verify trigger
        trigger = stale_job.trigger
        # Field 5 is hour, field 6 is minute
        hour_field = trigger.fields[5]
        minute_field = trigger.fields[6]
        
        # Check that hour is set to 9
        assert len(hour_field.expressions) == 1
        assert hour_field.expressions[0].first == 9
        
        # Check that minute is set to 0
        assert len(minute_field.expressions) == 1
        assert minute_field.expressions[0].first == 0

        # Cleanup
        scheduled_tasks_service.stop()


class TestCleanupTask:
    """Test orphaned branch cleanup task"""

    @pytest.mark.asyncio
    async def test_cleanup_orphaned_branches_no_projects(self, scheduled_tasks_service):
        """Test cleanup when no projects configured"""
        with patch('config.manager.config_manager') as mock_config:
            mock_config.get_all_project_configs.return_value = {}

            # Should not raise error
            await scheduled_tasks_service._cleanup_orphaned_branches()

    @pytest.mark.asyncio
    async def test_cleanup_orphaned_branches_single_project(self, scheduled_tasks_service):
        """Test cleanup for single project"""
        # Mock dependencies
        mock_project_config = MagicMock()
        mock_project_config.repository = "test-org/test-repo"

        with patch('config.manager.config_manager') as mock_config, \
             patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.github_integration.GitHubIntegration') as mock_gh_class:

            # Setup mocks
            mock_config.get_all_project_configs.return_value = {
                'test-project': mock_project_config
            }

            mock_gh = AsyncMock()
            mock_gh_class.return_value = mock_gh

            mock_fbm.cleanup_orphaned_branches = AsyncMock()

            # Run cleanup
            await scheduled_tasks_service._cleanup_orphaned_branches()

            # Verify cleanup was called
            mock_fbm.cleanup_orphaned_branches.assert_called_once_with(
                project='test-project',
                github_integration=mock_gh
            )

    @pytest.mark.asyncio
    async def test_cleanup_orphaned_branches_multiple_projects(self, scheduled_tasks_service):
        """Test cleanup for multiple projects"""
        # Mock project configs
        mock_config1 = MagicMock()
        mock_config1.repository = "org1/repo1"

        mock_config2 = MagicMock()
        mock_config2.repository = "org2/repo2"

        with patch('config.manager.config_manager') as mock_config, \
             patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.github_integration.GitHubIntegration') as mock_gh_class:

            mock_config.get_all_project_configs.return_value = {
                'project1': mock_config1,
                'project2': mock_config2
            }

            mock_fbm.cleanup_orphaned_branches = AsyncMock()

            # Run cleanup
            await scheduled_tasks_service._cleanup_orphaned_branches()

            # Verify cleanup called for both projects
            assert mock_fbm.cleanup_orphaned_branches.call_count == 2

    @pytest.mark.asyncio
    async def test_cleanup_handles_errors_gracefully(self, scheduled_tasks_service):
        """Test cleanup continues even if one project fails"""
        mock_config1 = MagicMock()
        mock_config1.repository = "org1/repo1"

        mock_config2 = MagicMock()
        mock_config2.repository = "org2/repo2"

        with patch('config.manager.config_manager') as mock_config, \
             patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.github_integration.GitHubIntegration') as mock_gh_class:

            mock_config.get_all_project_configs.return_value = {
                'project1': mock_config1,
                'project2': mock_config2
            }

            # First project fails, second succeeds
            mock_fbm.cleanup_orphaned_branches = AsyncMock(
                side_effect=[Exception("API error"), None]
            )

            # Should not raise exception
            await scheduled_tasks_service._cleanup_orphaned_branches()

            # Verify both projects were attempted
            assert mock_fbm.cleanup_orphaned_branches.call_count == 2


class TestStaleCheckTask:
    """Test stale branch check task"""

    @pytest.mark.asyncio
    async def test_check_stale_branches_no_projects(self, scheduled_tasks_service):
        """Test stale check when no projects configured"""
        with patch('config.manager.config_manager') as mock_config:
            mock_config.get_all_project_configs.return_value = {}

            # Should not raise error
            await scheduled_tasks_service._check_stale_branches()

    @pytest.mark.asyncio
    async def test_check_stale_branches_finds_stale(self, scheduled_tasks_service):
        """Test stale check detects and escalates stale branches"""
        from services.feature_branch_manager import FeatureBranch, SubIssueState

        mock_project_config = MagicMock()
        mock_project_config.repository = "test-org/test-repo"

        # Create a stale branch
        stale_branch = FeatureBranch(
            parent_issue=50,
            branch_name="feature/issue-50-old",
            created_at="2025-01-01T00:00:00Z",
            sub_issues=[SubIssueState(number=51, status="in_progress")]
        )

        with patch('config.manager.config_manager') as mock_config, \
             patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.github_integration.GitHubIntegration') as mock_gh_class:

            mock_config.get_all_project_configs.return_value = {
                'test-project': mock_project_config
            }

            mock_fbm.get_all_feature_branches.return_value = [stale_branch]
            mock_fbm.get_commits_behind_main = AsyncMock(return_value=60)  # Very stale
            mock_fbm.save_feature_branch_state = MagicMock()
            mock_fbm.escalate_stale_branch = AsyncMock()

            # Run stale check
            await scheduled_tasks_service._check_stale_branches()

            # Verify escalation was triggered
            mock_fbm.escalate_stale_branch.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_stale_branches_not_stale(self, scheduled_tasks_service):
        """Test stale check skips fresh branches"""
        from services.feature_branch_manager import FeatureBranch, SubIssueState

        mock_project_config = MagicMock()
        mock_project_config.repository = "test-org/test-repo"

        fresh_branch = FeatureBranch(
            parent_issue=50,
            branch_name="feature/issue-50-new",
            created_at="2025-01-05T00:00:00Z",
            sub_issues=[SubIssueState(number=51, status="in_progress")]
        )

        with patch('config.manager.config_manager') as mock_config, \
             patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.github_integration.GitHubIntegration') as mock_gh_class:

            mock_config.get_all_project_configs.return_value = {
                'test-project': mock_project_config
            }

            mock_fbm.get_all_feature_branches.return_value = [fresh_branch]
            mock_fbm.get_commits_behind_main = AsyncMock(return_value=5)  # Fresh
            mock_fbm.save_feature_branch_state = MagicMock()
            mock_fbm.escalate_stale_branch = AsyncMock()

            # Run stale check
            await scheduled_tasks_service._check_stale_branches()

            # Verify NO escalation
            mock_fbm.escalate_stale_branch.assert_not_called()


class TestManualTriggers:
    """Test manual task triggers"""

    @pytest.mark.asyncio
    async def test_run_cleanup_now(self, scheduled_tasks_service):
        """Test manual cleanup trigger"""
        with patch.object(scheduled_tasks_service, '_cleanup_orphaned_branches') as mock_cleanup, \
             patch('asyncio.create_task') as mock_create_task:
            
            mock_cleanup = AsyncMock()

            # Manually trigger
            scheduled_tasks_service.run_cleanup_now()

            # Verify task creation was called
            mock_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_stale_check_now(self, scheduled_tasks_service):
        """Test manual stale check trigger"""
        with patch.object(scheduled_tasks_service, '_check_stale_branches') as mock_check, \
             patch('asyncio.create_task') as mock_create_task:
            
            mock_check = AsyncMock()

            # Manually trigger
            scheduled_tasks_service.run_stale_check_now()

            # Verify task creation was called
            mock_create_task.assert_called_once()


class TestGlobalInstance:
    """Test global singleton instance"""

    def test_get_scheduled_tasks_service_singleton(self):
        """Test global instance is singleton"""
        from services.scheduled_tasks import get_scheduled_tasks_service

        service1 = get_scheduled_tasks_service()
        service2 = get_scheduled_tasks_service()

        assert service1 is service2  # Same instance


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
