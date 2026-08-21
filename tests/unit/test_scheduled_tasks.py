"""
Unit tests for scheduled tasks service
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone, timedelta

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

    @pytest.mark.asyncio
    async def test_reap_orphaned_test_containers_job_schedule(self, scheduled_tasks_service):
        """Test orphaned testcontainers reaper job is scheduled every 15 minutes"""
        scheduled_tasks_service.start()

        reaper_job = scheduled_tasks_service.scheduler.get_job('reap_orphaned_test_containers')

        assert reaper_job is not None
        assert reaper_job.func == scheduled_tasks_service._reap_orphaned_test_containers

        # Verify trigger (CronTrigger, minute='*/15')
        trigger = reaper_job.trigger
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

    @pytest.mark.asyncio
    async def test_run_test_container_reaper_now(self, scheduled_tasks_service):
        """Test manual testcontainers reaper trigger"""
        with patch.object(scheduled_tasks_service, '_reap_orphaned_test_containers') as mock_reap, \
             patch('asyncio.create_task') as mock_create_task:

            mock_reap = AsyncMock()

            # Manually trigger
            scheduled_tasks_service.run_test_container_reaper_now()

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


class TestReapOrphanedTestContainers:
    """Test orphaned testcontainers reaper task"""

    @staticmethod
    def _docker_inspect_entry(container_id: str, name: str, age_minutes: float,
                              session_id: str = "abc-123", image: str = "elasticsearch:8.17.0") -> dict:
        created = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
        return {
            "Id": container_id,
            "Name": f"/{name}",
            "Created": created.strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z",
            "Config": {
                "Image": image,
                "Labels": {"org.testcontainers": "true", "org.testcontainers.session-id": session_id},
            },
        }

    @staticmethod
    def _mock_subprocess_run(responses: dict):
        """Build a subprocess.run side_effect keyed by the docker subcommand (argv[1])."""
        def _run(cmd, **kwargs):
            result = MagicMock()
            subcommand = cmd[1] if len(cmd) > 1 else None
            result.returncode, result.stdout, result.stderr = responses.get(subcommand, (0, "", ""))
            return result
        return _run

    @pytest.mark.asyncio
    async def test_reaps_container_older_than_threshold(self, scheduled_tasks_service):
        """A testcontainers-labeled container past the age threshold gets force-removed"""
        entry = self._docker_inspect_entry("abc111", "gifted_bhaskara", age_minutes=600)

        with patch('subprocess.run', side_effect=self._mock_subprocess_run({
            'ps': (0, "abc111\n", ""),
            'inspect': (0, json.dumps([entry]), ""),
            'rm': (0, "abc111\n", ""),
        })) as mock_run:
            await scheduled_tasks_service._reap_orphaned_test_containers()

        rm_calls = [c for c in mock_run.call_args_list if c.args[0][1] == 'rm']
        assert len(rm_calls) == 1
        assert rm_calls[0].args[0] == ['docker', 'rm', '-f', 'abc111']

    @pytest.mark.asyncio
    async def test_skips_container_younger_than_threshold(self, scheduled_tasks_service):
        """A fresh testcontainers-labeled container (e.g. mid-test) is left alone"""
        entry = self._docker_inspect_entry("abc222", "boring_wilbur", age_minutes=2)

        with patch('subprocess.run', side_effect=self._mock_subprocess_run({
            'ps': (0, "abc222\n", ""),
            'inspect': (0, json.dumps([entry]), ""),
        })) as mock_run:
            await scheduled_tasks_service._reap_orphaned_test_containers()

        rm_calls = [c for c in mock_run.call_args_list if c.args[0][1] == 'rm']
        assert rm_calls == []

    @pytest.mark.asyncio
    async def test_no_testcontainers_labeled_containers(self, scheduled_tasks_service):
        """Nothing to do when docker ps finds no matches"""
        with patch('subprocess.run', side_effect=self._mock_subprocess_run({
            'ps': (0, "", ""),
        })) as mock_run:
            await scheduled_tasks_service._reap_orphaned_test_containers()

        assert all(c.args[0][1] != 'inspect' for c in mock_run.call_args_list)
        assert all(c.args[0][1] != 'rm' for c in mock_run.call_args_list)

    @pytest.mark.asyncio
    async def test_handles_docker_ps_failure_gracefully(self, scheduled_tasks_service, caplog):
        """A docker CLI failure is logged, not raised"""
        with patch('subprocess.run', side_effect=self._mock_subprocess_run({
            'ps': (1, "", "Cannot connect to the Docker daemon"),
        })):
            # Should not raise
            await scheduled_tasks_service._reap_orphaned_test_containers()

        assert "Error in orphaned testcontainers reaper" in caplog.text

    @pytest.mark.asyncio
    async def test_respects_reap_age_env_override(self, scheduled_tasks_service, monkeypatch):
        """TESTCONTAINERS_REAP_AGE_MINUTES lowers/raises the age threshold"""
        monkeypatch.setenv('TESTCONTAINERS_REAP_AGE_MINUTES', '5')
        entry = self._docker_inspect_entry("abc333", "quizzical_chatelet", age_minutes=10)

        with patch('subprocess.run', side_effect=self._mock_subprocess_run({
            'ps': (0, "abc333\n", ""),
            'inspect': (0, json.dumps([entry]), ""),
            'rm': (0, "abc333\n", ""),
        })) as mock_run:
            await scheduled_tasks_service._reap_orphaned_test_containers()

        rm_calls = [c for c in mock_run.call_args_list if c.args[0][1] == 'rm']
        assert len(rm_calls) == 1

    @pytest.mark.asyncio
    async def test_multi_container_sweep_only_reaps_old_ones(self, scheduled_tasks_service):
        """A batch with old and young containers only removes the old ones, by the right IDs"""
        old_entry = self._docker_inspect_entry("old111", "gifted_bhaskara", age_minutes=600)
        young_entry = self._docker_inspect_entry("young222", "boring_wilbur", age_minutes=2)

        with patch('subprocess.run', side_effect=self._mock_subprocess_run({
            'ps': (0, "old111\nyoung222\n", ""),
            'inspect': (0, json.dumps([old_entry, young_entry]), ""),
            'rm': (0, "old111\n", ""),
        })) as mock_run:
            await scheduled_tasks_service._reap_orphaned_test_containers()

        rm_calls = [c.args[0] for c in mock_run.call_args_list if c.args[0][1] == 'rm']
        assert rm_calls == [['docker', 'rm', '-f', 'old111']]

    @pytest.mark.asyncio
    async def test_rm_failure_on_one_container_does_not_abort_others(self, scheduled_tasks_service, caplog):
        """One container's docker rm failing must not prevent removing the rest of the batch"""
        entry_a = self._docker_inspect_entry("failer", "container-a", age_minutes=600)
        entry_b = self._docker_inspect_entry("succeeder", "container-b", age_minutes=600)

        def _run(cmd, **kwargs):
            result = MagicMock()
            if cmd[1] == 'ps':
                result.returncode, result.stdout, result.stderr = (0, "failer\nsucceeder\n", "")
            elif cmd[1] == 'inspect':
                result.returncode, result.stdout, result.stderr = (0, json.dumps([entry_a, entry_b]), "")
            elif cmd[1] == 'rm':
                if cmd[-1] == 'failer':
                    result.returncode, result.stdout, result.stderr = (1, "", "container is restarting")
                else:
                    result.returncode, result.stdout, result.stderr = (0, "succeeder\n", "")
            return result

        with patch('subprocess.run', side_effect=_run) as mock_run:
            # Should not raise despite one rm failing
            await scheduled_tasks_service._reap_orphaned_test_containers()

        rm_calls = [c.args[0] for c in mock_run.call_args_list if c.args[0][1] == 'rm']
        assert ['docker', 'rm', '-f', 'failer'] in rm_calls
        assert ['docker', 'rm', '-f', 'succeeder'] in rm_calls
        # Both attempts are logged, including the failure with its error and the successful removal
        assert "removed=False" in caplog.text
        assert "container is restarting" in caplog.text
        assert "removed=True" in caplog.text

    @pytest.mark.asyncio
    async def test_unparseable_created_timestamp_is_skipped_and_logged(self, scheduled_tasks_service, caplog):
        """A container with a Created value we can't parse is skipped (not crashed on) and reported"""
        entry = self._docker_inspect_entry("weird1", "mystery-container", age_minutes=600)
        entry["Created"] = "not-a-timestamp"

        with patch('subprocess.run', side_effect=self._mock_subprocess_run({
            'ps': (0, "weird1\n", ""),
            'inspect': (0, json.dumps([entry]), ""),
        })) as mock_run:
            # Should not raise
            await scheduled_tasks_service._reap_orphaned_test_containers()

        rm_calls = [c for c in mock_run.call_args_list if c.args[0][1] == 'rm']
        assert rm_calls == []
        assert "could not parse Created" in caplog.text
        assert "weird1" in caplog.text

    @pytest.mark.parametrize("created_suffix", [
        "Z",                 # no fractional seconds at all
        ".1Z",               # 1 fractional digit
        ".12345Z",           # 5 fractional digits
        ".123456789Z",       # full 9-digit nanosecond precision
    ])
    @pytest.mark.asyncio
    async def test_handles_variable_length_fractional_seconds(self, scheduled_tasks_service, created_suffix):
        """Docker strips trailing zeros from fractional seconds -- every valid length must parse"""
        old_created = (datetime.now(timezone.utc) - timedelta(minutes=600)).strftime("%Y-%m-%dT%H:%M:%S") + created_suffix
        entry = self._docker_inspect_entry("varfrac1", "container-x", age_minutes=600)
        entry["Created"] = old_created

        with patch('subprocess.run', side_effect=self._mock_subprocess_run({
            'ps': (0, "varfrac1\n", ""),
            'inspect': (0, json.dumps([entry]), ""),
            'rm': (0, "varfrac1\n", ""),
        })) as mock_run:
            await scheduled_tasks_service._reap_orphaned_test_containers()

        rm_calls = [c.args[0] for c in mock_run.call_args_list if c.args[0][1] == 'rm']
        assert rm_calls == [['docker', 'rm', '-f', 'varfrac1']]

    @pytest.mark.asyncio
    async def test_docker_inspect_partial_failure_still_processes_returned_containers(
        self, scheduled_tasks_service, caplog
    ):
        """One container vanishing between `docker ps` and `docker inspect` shouldn't
        discard results for the containers that were still found."""
        entry = self._docker_inspect_entry("stillhere", "container-y", age_minutes=600)

        with patch('subprocess.run', side_effect=self._mock_subprocess_run({
            'ps': (0, "stillhere\nvanished\n", ""),
            # docker inspect exits 1 but still prints the containers it could find
            'inspect': (1, json.dumps([entry]), "Error: No such object: vanished"),
            'rm': (0, "stillhere\n", ""),
        })) as mock_run:
            await scheduled_tasks_service._reap_orphaned_test_containers()

        rm_calls = [c.args[0] for c in mock_run.call_args_list if c.args[0][1] == 'rm']
        assert rm_calls == [['docker', 'rm', '-f', 'stillhere']]
        assert "removed concurrently" in caplog.text

    @pytest.mark.asyncio
    async def test_docker_inspect_total_failure_still_raises(self, scheduled_tasks_service, caplog):
        """No usable output at all from docker inspect is a real failure, not a partial-results case"""
        with patch('subprocess.run', side_effect=self._mock_subprocess_run({
            'ps': (0, "abc\n", ""),
            'inspect': (1, "", "Cannot connect to the Docker daemon"),
        })) as mock_run:
            await scheduled_tasks_service._reap_orphaned_test_containers()

        rm_calls = [c for c in mock_run.call_args_list if c.args[0][1] == 'rm']
        assert rm_calls == []
        assert "Error in orphaned testcontainers reaper" in caplog.text

    @pytest.mark.asyncio
    async def test_reaper_exempt_label_is_never_removed(self, scheduled_tasks_service):
        """A container explicitly opted out via label is skipped regardless of age"""
        entry = self._docker_inspect_entry("exempt1", "debug-session", age_minutes=600)
        entry["Config"]["Labels"]["org.testcontainers.reaper-exempt"] = "true"

        with patch('subprocess.run', side_effect=self._mock_subprocess_run({
            'ps': (0, "exempt1\n", ""),
            'inspect': (0, json.dumps([entry]), ""),
        })) as mock_run:
            await scheduled_tasks_service._reap_orphaned_test_containers()

        rm_calls = [c for c in mock_run.call_args_list if c.args[0][1] == 'rm']
        assert rm_calls == []

    @pytest.mark.asyncio
    async def test_age_exactly_at_threshold_is_reaped(self, scheduled_tasks_service, monkeypatch):
        """Boundary: a container exactly at the threshold counts as reapable, not just strictly older"""
        monkeypatch.setenv('TESTCONTAINERS_REAP_AGE_MINUTES', '30')
        entry = self._docker_inspect_entry("boundary1", "container-z", age_minutes=30.01)

        with patch('subprocess.run', side_effect=self._mock_subprocess_run({
            'ps': (0, "boundary1\n", ""),
            'inspect': (0, json.dumps([entry]), ""),
            'rm': (0, "boundary1\n", ""),
        })) as mock_run:
            await scheduled_tasks_service._reap_orphaned_test_containers()

        rm_calls = [c for c in mock_run.call_args_list if c.args[0][1] == 'rm']
        assert len(rm_calls) == 1

    @pytest.mark.asyncio
    async def test_invalid_env_override_falls_back_to_default(self, scheduled_tasks_service, monkeypatch, caplog):
        """A malformed TESTCONTAINERS_REAP_AGE_MINUTES doesn't crash the job -- it logs and uses the default"""
        monkeypatch.setenv('TESTCONTAINERS_REAP_AGE_MINUTES', 'not-a-number')
        entry = self._docker_inspect_entry("badenv1", "container-w", age_minutes=600)

        with patch('subprocess.run', side_effect=self._mock_subprocess_run({
            'ps': (0, "badenv1\n", ""),
            'inspect': (0, json.dumps([entry]), ""),
            'rm': (0, "badenv1\n", ""),
        })) as mock_run:
            # Should not raise
            await scheduled_tasks_service._reap_orphaned_test_containers()

        assert "Invalid TESTCONTAINERS_REAP_AGE_MINUTES" in caplog.text
        rm_calls = [c for c in mock_run.call_args_list if c.args[0][1] == 'rm']
        assert len(rm_calls) == 1  # still reaps using the fallback default (600m old container)

    @pytest.mark.asyncio
    async def test_zero_or_negative_env_override_falls_back_to_default(self, scheduled_tasks_service, monkeypatch, caplog):
        """A zero/negative threshold would reap everything instantly -- reject it instead"""
        monkeypatch.setenv('TESTCONTAINERS_REAP_AGE_MINUTES', '0')
        entry = self._docker_inspect_entry("zeroenv1", "container-v", age_minutes=2)

        with patch('subprocess.run', side_effect=self._mock_subprocess_run({
            'ps': (0, "zeroenv1\n", ""),
            'inspect': (0, json.dumps([entry]), ""),
        })) as mock_run:
            await scheduled_tasks_service._reap_orphaned_test_containers()

        assert "Invalid TESTCONTAINERS_REAP_AGE_MINUTES" in caplog.text
        rm_calls = [c for c in mock_run.call_args_list if c.args[0][1] == 'rm']
        # 2-minute-old container must NOT be reaped once the invalid override is rejected
        assert rm_calls == []


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
