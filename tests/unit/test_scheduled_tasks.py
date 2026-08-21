"""
Unit tests for scheduled tasks service
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

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

    @pytest.mark.asyncio
    async def test_sweep_orphaned_parents_job_schedule(self, scheduled_tasks_service):
        """Test orphaned-parent sweep job is scheduled every 15 minutes"""
        scheduled_tasks_service.start()

        sweep_job = scheduled_tasks_service.scheduler.get_job('sweep_orphaned_parents')

        assert sweep_job is not None
        assert sweep_job.func == scheduled_tasks_service._sweep_orphaned_parents

        # Verify trigger (CronTrigger, minute='*/15')
        trigger = sweep_job.trigger
        minute_field = trigger.fields[6]
        assert str(minute_field) == '*/15'

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
        mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}

        with patch('config.manager.config_manager') as mock_config, \
             patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.github_integration.GitHubIntegration') as mock_gh_class:

            # Setup mocks
            mock_config.list_visible_projects.return_value = ['test-project']
            mock_config.get_project_config.return_value = mock_project_config

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
        mock_config1.github = {'org': 'org1', 'repo': 'repo1'}

        mock_config2 = MagicMock()
        mock_config2.github = {'org': 'org2', 'repo': 'repo2'}

        project_configs = {'project1': mock_config1, 'project2': mock_config2}

        with patch('config.manager.config_manager') as mock_config, \
             patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.github_integration.GitHubIntegration') as mock_gh_class:

            mock_config.list_visible_projects.return_value = ['project1', 'project2']
            mock_config.get_project_config.side_effect = lambda name: project_configs[name]

            mock_fbm.cleanup_orphaned_branches = AsyncMock()

            # Run cleanup
            await scheduled_tasks_service._cleanup_orphaned_branches()

            # Verify cleanup called for both projects
            assert mock_fbm.cleanup_orphaned_branches.call_count == 2

    @pytest.mark.asyncio
    async def test_cleanup_handles_errors_gracefully(self, scheduled_tasks_service):
        """Test cleanup continues even if one project fails"""
        mock_config1 = MagicMock()
        mock_config1.github = {'org': 'org1', 'repo': 'repo1'}

        mock_config2 = MagicMock()
        mock_config2.github = {'org': 'org2', 'repo': 'repo2'}

        project_configs = {'project1': mock_config1, 'project2': mock_config2}

        with patch('config.manager.config_manager') as mock_config, \
             patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.github_integration.GitHubIntegration') as mock_gh_class:

            mock_config.list_visible_projects.return_value = ['project1', 'project2']
            mock_config.get_project_config.side_effect = lambda name: project_configs[name]

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
        mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}

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

            mock_config.list_visible_projects.return_value = ['test-project']
            mock_config.get_project_config.return_value = mock_project_config

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


class TestSweepOrphanedParents:
    """Test the orphaned-parent sweep (safety net for _check_pr_ready_on_issue_exit misses)"""

    def _make_project_config(self, board_name='Planning & Design', pipeline_name='planning_design',
                              workflow='planning_design_workflow', active=True):
        pipeline = MagicMock()
        pipeline.active = active
        pipeline.name = pipeline_name
        pipeline.workflow = workflow
        pipeline.board_name = board_name

        project_config = MagicMock()
        project_config.pipelines = [pipeline]
        project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
        return project_config

    def _make_item(self, issue_number, status):
        from services.project_monitor import ProjectItem
        return ProjectItem(
            item_id=f'item-{issue_number}',
            content_id=f'content-{issue_number}',
            issue_number=issue_number,
            title=f'Issue #{issue_number}',
            status=status,
            repository='test-repo',
            last_updated='2026-01-01T00:00:00Z'
        )

    @pytest.mark.asyncio
    async def test_no_projects(self, scheduled_tasks_service):
        """Sweep with no visible projects completes without error and touches nothing"""
        with patch('config.manager.config_manager') as mock_config, \
             patch('services.project_monitor.ProjectMonitor') as mock_pm_class, \
             patch('task_queue.task_manager.TaskQueue'):
            mock_config.list_visible_projects.return_value = []

            await scheduled_tasks_service._sweep_orphaned_parents()

            mock_pm_class.return_value._advance_parent_for_pr_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_project_without_planning_pipeline(self, scheduled_tasks_service):
        """A project whose only active pipeline is SDLC (not planning) is skipped entirely"""
        sdlc_config = self._make_project_config(
            board_name='SDLC Execution', pipeline_name='sdlc_execution', workflow='sdlc_execution_workflow'
        )

        with patch('config.manager.config_manager') as mock_config, \
             patch('config.state_manager.state_manager') as mock_state, \
             patch('services.project_monitor.ProjectMonitor') as mock_pm_class, \
             patch('task_queue.task_manager.TaskQueue'):
            mock_config.list_visible_projects.return_value = ['test-project']
            mock_config.get_project_config.return_value = sdlc_config

            await scheduled_tasks_service._sweep_orphaned_parents()

            mock_state.load_project_state.assert_not_called()
            mock_pm_class.return_value._advance_parent_for_pr_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_board_state(self, scheduled_tasks_service):
        """A project with a planning pipeline but no GitHub board state is skipped"""
        project_config = self._make_project_config()

        with patch('config.manager.config_manager') as mock_config, \
             patch('config.state_manager.state_manager') as mock_state, \
             patch('services.project_monitor.ProjectMonitor') as mock_pm_class, \
             patch('task_queue.task_manager.TaskQueue'):
            mock_config.list_visible_projects.return_value = ['test-project']
            mock_config.get_project_config.return_value = project_config
            mock_state.load_project_state.return_value = None

            await scheduled_tasks_service._sweep_orphaned_parents()

            mock_pm_class.return_value._advance_parent_for_pr_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_in_development_items(self, scheduled_tasks_service):
        """Board items that aren't in 'In Development' are never checked"""
        project_config = self._make_project_config()
        mock_project_state = MagicMock()
        mock_board_state = MagicMock()
        mock_board_state.project_number = 29
        mock_project_state.boards = {'Planning & Design': mock_board_state}

        with patch('config.manager.config_manager') as mock_config, \
             patch('config.state_manager.state_manager') as mock_state, \
             patch('services.project_monitor.ProjectMonitor') as mock_pm_class, \
             patch('services.github_integration.GitHubIntegration'), \
             patch('task_queue.task_manager.TaskQueue'):
            mock_config.list_visible_projects.return_value = ['test-project']
            mock_config.get_project_config.return_value = project_config
            mock_state.load_project_state.return_value = mock_project_state

            mock_pm = mock_pm_class.return_value
            mock_pm.get_project_items.return_value = [
                self._make_item(822, 'In Review'),
                self._make_item(823, 'Done'),
            ]
            mock_pm._advance_parent_for_pr_review = AsyncMock()

            await scheduled_tasks_service._sweep_orphaned_parents()

            mock_pm._advance_parent_for_pr_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_issue_with_no_sub_issues(self, scheduled_tasks_service):
        """An 'In Development' issue with zero sub-issues is out of scope for this sweep
        (it may be a standalone issue that never went through work breakdown, and
        advancing it to 'In Review' would be premature/unrelated to this bug)."""
        project_config = self._make_project_config()
        mock_project_state = MagicMock()
        mock_board_state = MagicMock()
        mock_board_state.project_number = 29
        mock_project_state.boards = {'Planning & Design': mock_board_state}

        with patch('config.manager.config_manager') as mock_config, \
             patch('config.state_manager.state_manager') as mock_state, \
             patch('services.project_monitor.ProjectMonitor') as mock_pm_class, \
             patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.github_integration.GitHubIntegration') as mock_gh_class, \
             patch('task_queue.task_manager.TaskQueue'):
            mock_config.list_visible_projects.return_value = ['test-project']
            mock_config.get_project_config.return_value = project_config
            mock_state.load_project_state.return_value = mock_project_state

            mock_pm = mock_pm_class.return_value
            mock_pm.get_project_items.return_value = [self._make_item(822, 'In Development')]
            mock_pm._advance_parent_for_pr_review = AsyncMock()

            mock_gh = AsyncMock()
            mock_gh.get_issue.return_value = {'number': 822}
            mock_gh_class.return_value = mock_gh

            mock_fbm._get_sub_issues_from_parent = AsyncMock(return_value=[])

            await scheduled_tasks_service._sweep_orphaned_parents()

            mock_pm._advance_parent_for_pr_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_advances_parent_with_sub_issues(self, scheduled_tasks_service):
        """The core case: an 'In Development' parent with real sub-issues gets
        re-checked via _advance_parent_for_pr_review (this is what recovers a
        parent stranded by a transient GitHub API failure)."""
        project_config = self._make_project_config()
        mock_project_state = MagicMock()
        mock_board_state = MagicMock()
        mock_board_state.project_number = 29
        mock_project_state.boards = {'Planning & Design': mock_board_state}

        with patch('config.manager.config_manager') as mock_config, \
             patch('config.state_manager.state_manager') as mock_state, \
             patch('services.project_monitor.ProjectMonitor') as mock_pm_class, \
             patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.github_integration.GitHubIntegration') as mock_gh_class, \
             patch('task_queue.task_manager.TaskQueue'):
            mock_config.list_visible_projects.return_value = ['test-project']
            mock_config.get_project_config.return_value = project_config
            mock_state.load_project_state.return_value = mock_project_state

            mock_pm = mock_pm_class.return_value
            mock_pm.get_project_items.return_value = [self._make_item(822, 'In Development')]
            mock_pm._advance_parent_for_pr_review = AsyncMock()

            mock_gh = AsyncMock()
            mock_gh.get_issue.return_value = {'number': 822}
            mock_gh_class.return_value = mock_gh

            mock_fbm._get_sub_issues_from_parent = AsyncMock(
                return_value=[{'number': 824, 'state': 'CLOSED'}]
            )

            await scheduled_tasks_service._sweep_orphaned_parents()

            mock_pm._advance_parent_for_pr_review.assert_called_once_with(
                'test-project', 822, project_config
            )

    @pytest.mark.asyncio
    async def test_continues_after_per_item_error(self, scheduled_tasks_service):
        """One item raising an exception doesn't stop the sweep from checking the rest"""
        project_config = self._make_project_config()
        mock_project_state = MagicMock()
        mock_board_state = MagicMock()
        mock_board_state.project_number = 29
        mock_project_state.boards = {'Planning & Design': mock_board_state}

        with patch('config.manager.config_manager') as mock_config, \
             patch('config.state_manager.state_manager') as mock_state, \
             patch('services.project_monitor.ProjectMonitor') as mock_pm_class, \
             patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.github_integration.GitHubIntegration') as mock_gh_class, \
             patch('task_queue.task_manager.TaskQueue'):
            mock_config.list_visible_projects.return_value = ['test-project']
            mock_config.get_project_config.return_value = project_config
            mock_state.load_project_state.return_value = mock_project_state

            mock_pm = mock_pm_class.return_value
            mock_pm.get_project_items.return_value = [
                self._make_item(822, 'In Development'),
                self._make_item(900, 'In Development'),
            ]
            mock_pm._advance_parent_for_pr_review = AsyncMock()

            mock_gh = AsyncMock()
            mock_gh.get_issue.side_effect = [Exception("GitHub API rate limit"), {'number': 900}]
            mock_gh_class.return_value = mock_gh

            mock_fbm._get_sub_issues_from_parent = AsyncMock(
                return_value=[{'number': 901, 'state': 'CLOSED'}]
            )

            # Should not raise despite the first item's github.get_issue() blowing up
            await scheduled_tasks_service._sweep_orphaned_parents()

            # The second item still got processed
            mock_pm._advance_parent_for_pr_review.assert_called_once_with(
                'test-project', 900, project_config
            )

    @pytest.mark.asyncio
    async def test_continues_after_per_project_error(self, scheduled_tasks_service):
        """One project raising an exception doesn't stop the sweep from checking others"""
        good_config = self._make_project_config()
        mock_project_state = MagicMock()
        mock_board_state = MagicMock()
        mock_board_state.project_number = 29
        mock_project_state.boards = {'Planning & Design': mock_board_state}

        def get_project_config(name):
            if name == 'broken-project':
                raise Exception("Config load failed")
            return good_config

        with patch('config.manager.config_manager') as mock_config, \
             patch('config.state_manager.state_manager') as mock_state, \
             patch('services.project_monitor.ProjectMonitor') as mock_pm_class, \
             patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.github_integration.GitHubIntegration') as mock_gh_class, \
             patch('task_queue.task_manager.TaskQueue'):
            mock_config.list_visible_projects.return_value = ['broken-project', 'good-project']
            mock_config.get_project_config.side_effect = get_project_config
            mock_state.load_project_state.return_value = mock_project_state

            mock_pm = mock_pm_class.return_value
            mock_pm.get_project_items.return_value = [self._make_item(822, 'In Development')]
            mock_pm._advance_parent_for_pr_review = AsyncMock()

            mock_gh = AsyncMock()
            mock_gh.get_issue.return_value = {'number': 822}
            mock_gh_class.return_value = mock_gh

            mock_fbm._get_sub_issues_from_parent = AsyncMock(
                return_value=[{'number': 824, 'state': 'CLOSED'}]
            )

            # Should not raise despite 'broken-project' failing to load config
            await scheduled_tasks_service._sweep_orphaned_parents()

            # 'good-project' was still swept
            mock_pm._advance_parent_for_pr_review.assert_called_once_with(
                'good-project', 822, good_config
            )


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
