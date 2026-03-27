"""
Unit tests for ProjectMonitor queue processing failsafe mechanism
"""
import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import Mock, MagicMock, patch, call
from services.project_monitor import ProjectMonitor
from config.manager import ConfigManager


class TestQueueProcessingFailsafe:
    """Test _check_and_process_waiting_issues_failsafe method"""

    @pytest.fixture
    def mock_config_manager(self):
        """Create a mock config manager"""
        config_manager = Mock(spec=ConfigManager)

        # Create mock pipelines
        active_pipeline = Mock()
        active_pipeline.active = True
        active_pipeline.board_name = "SDLC Execution"
        active_pipeline.workflow = "sdlc_execution_workflow"

        inactive_pipeline = Mock()
        inactive_pipeline.active = False
        inactive_pipeline.board_name = "Inactive Board"

        # Create mock project config
        project_config = Mock()
        project_config.pipelines = [active_pipeline, inactive_pipeline]
        project_config.github = {'repo': 'test-org/test-repo'}
        project_config.orchestrator = {"polling_interval": 30}

        # Create mock workflow template with iterable columns
        mock_column = Mock()
        mock_column.name = 'Development'
        mock_column.type = 'standard'
        workflow_template = Mock()
        workflow_template.columns = [mock_column]
        workflow_template.pipeline_trigger_columns = None
        workflow_template.pipeline_exit_columns = None

        # Mock list_projects to return empty list (for initialization)
        config_manager.list_projects.return_value = []

        # Mock list_visible_projects to return test project
        config_manager.list_visible_projects.return_value = ['test_project']
        config_manager.get_project_config.return_value = project_config
        config_manager.get_workflow_template.return_value = workflow_template

        return config_manager

    @pytest.fixture
    def project_monitor(self, mock_config_manager):
        """Create ProjectMonitor instance with mocked dependencies"""
        task_queue = Mock()
        monitor = ProjectMonitor(task_queue, mock_config_manager)

        # Mock trigger_agent_for_status to avoid actual agent triggering
        monitor.trigger_agent_for_status = Mock()

        # Mock get_issue_column_sync to return a column
        monitor.get_issue_column_sync = Mock(return_value='Development')

        return monitor

    @pytest.fixture
    def mock_lock_manager(self):
        """Create mock lock manager"""
        lock_manager = Mock()
        return lock_manager

    @pytest.fixture
    def mock_queue_manager(self):
        """Create mock queue manager"""
        queue_manager = Mock()
        return queue_manager

    def test_failsafe_triggers_when_unlocked_with_waiting_issues(
        self, project_monitor, mock_lock_manager, mock_queue_manager
    ):
        """
        Test that failsafe triggers agent when:
        - Pipeline is unlocked
        - Waiting issues exist
        """
        # Setup: Pipeline is unlocked
        mock_lock = Mock()
        mock_lock.lock_status = 'unlocked'
        mock_lock_manager.get_lock.return_value = mock_lock

        # Setup: Waiting issue exists
        waiting_issue = {
            'issue_number': 155,
            'status': 'waiting',
            'title': 'Test Issue'
        }
        mock_queue_manager.get_next_waiting_issue.return_value = waiting_issue

        # Setup: Lock acquisition succeeds
        mock_lock_manager.try_acquire_lock.return_value = (True, "lock_acquired")

        # Patch the manager getters (patching where they're imported FROM, not where they're used)
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue_manager):

            # Execute failsafe
            project_monitor._check_and_process_waiting_issues_failsafe()

        # Verify: Lock was checked
        mock_lock_manager.get_lock.assert_called_once_with('test_project', 'SDLC Execution')

        # Verify: Next waiting issue was retrieved
        mock_queue_manager.get_next_waiting_issue.assert_called_once()

        # Verify: Lock acquisition was attempted
        mock_lock_manager.try_acquire_lock.assert_called_once_with(
            project='test_project',
            board='SDLC Execution',
            issue_number=155
        )

        # Verify: Issue was marked as active
        mock_queue_manager.mark_issue_active.assert_called_once_with(155)

        # Verify: Agent was triggered
        project_monitor.trigger_agent_for_status.assert_called_once_with(
            'test_project',
            'SDLC Execution',
            155,
            'Development',
            'test-org/test-repo',
            lock_already_acquired=True
        )

    def test_failsafe_skips_when_pipeline_locked(
        self, project_monitor, mock_lock_manager, mock_queue_manager
    ):
        """
        Test that failsafe skips when pipeline is locked
        """
        # Setup: Pipeline is locked
        mock_lock = Mock()
        mock_lock.lock_status = 'locked'
        mock_lock_manager.get_lock.return_value = mock_lock

        # Patch the manager getters (patching where they're imported FROM, not where they're used)
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue_manager):

            # Execute failsafe
            project_monitor._check_and_process_waiting_issues_failsafe()

        # Verify: Lock was checked
        mock_lock_manager.get_lock.assert_called_once()

        # Verify: Did NOT check for waiting issues (skipped early)
        mock_queue_manager.get_next_waiting_issue.assert_not_called()

        # Verify: Did NOT trigger agent
        project_monitor.trigger_agent_for_status.assert_not_called()

    def test_failsafe_skips_when_no_waiting_issues(
        self, project_monitor, mock_lock_manager, mock_queue_manager
    ):
        """
        Test that failsafe skips when no waiting issues exist
        """
        # Setup: Pipeline is unlocked
        mock_lock_manager.get_lock.return_value = None  # No lock = unlocked

        # Setup: No waiting issues
        mock_queue_manager.get_next_waiting_issue.return_value = None

        # Patch the manager getters (patching where they're imported FROM, not where they're used)
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue_manager):

            # Execute failsafe
            project_monitor._check_and_process_waiting_issues_failsafe()

        # Verify: Checked for waiting issues
        mock_queue_manager.get_next_waiting_issue.assert_called_once()

        # Verify: Did NOT attempt lock acquisition
        mock_lock_manager.try_acquire_lock.assert_not_called()

        # Verify: Did NOT trigger agent
        project_monitor.trigger_agent_for_status.assert_not_called()

    def test_failsafe_handles_lock_acquisition_failure_gracefully(
        self, project_monitor, mock_lock_manager, mock_queue_manager
    ):
        """
        Test that failsafe handles lock acquisition failure gracefully
        (another process already processing the issue)
        """
        # Setup: Pipeline is unlocked
        mock_lock_manager.get_lock.return_value = None

        # Setup: Waiting issue exists
        waiting_issue = {
            'issue_number': 155,
            'status': 'waiting',
            'title': 'Test Issue'
        }
        mock_queue_manager.get_next_waiting_issue.return_value = waiting_issue

        # Setup: Lock acquisition FAILS (another process got it)
        mock_lock_manager.try_acquire_lock.return_value = (False, "locked_by_issue_156")

        # Patch the manager getters (patching where they're imported FROM, not where they're used)
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue_manager):

            # Execute failsafe - should not raise exception
            project_monitor._check_and_process_waiting_issues_failsafe()

        # Verify: Lock acquisition was attempted
        mock_lock_manager.try_acquire_lock.assert_called_once()

        # Verify: Did NOT mark issue as active (lock failed)
        mock_queue_manager.mark_issue_active.assert_not_called()

        # Verify: Did NOT trigger agent (lock failed)
        project_monitor.trigger_agent_for_status.assert_not_called()

    def test_failsafe_cleans_up_issue_not_in_any_column(
        self, project_monitor, mock_lock_manager, mock_queue_manager
    ):
        """
        Test that failsafe cleans up issues that are no longer in any column
        """
        # Setup: Pipeline is unlocked
        mock_lock_manager.get_lock.return_value = None

        # Setup: Waiting issue exists
        waiting_issue = {
            'issue_number': 114,
            'status': 'waiting',
            'title': 'Zombie Issue'
        }
        mock_queue_manager.get_next_waiting_issue.return_value = waiting_issue

        # Setup: Lock acquisition succeeds
        mock_lock_manager.try_acquire_lock.return_value = (True, "lock_acquired")

        # Setup: Issue NOT found in any column (returns None)
        project_monitor.get_issue_column_sync = Mock(return_value=None)

        # Patch the manager getters (patching where they're imported FROM, not where they're used)
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue_manager):

            # Execute failsafe
            project_monitor._check_and_process_waiting_issues_failsafe()

        # Verify: Issue was removed from queue
        mock_queue_manager.remove_issue_from_queue.assert_called_once_with(114)

        # Verify: Lock was released
        mock_lock_manager.release_lock.assert_called_once_with(
            'test_project',
            'SDLC Execution',
            114
        )

        # Verify: Agent was NOT triggered (issue doesn't exist)
        project_monitor.trigger_agent_for_status.assert_not_called()

    def test_failsafe_handles_exceptions_without_crashing(
        self, project_monitor, mock_lock_manager, mock_queue_manager
    ):
        """
        Test that exceptions in failsafe are caught and logged, don't crash the monitor
        """
        # Setup: Pipeline check raises exception
        mock_lock_manager.get_lock.side_effect = Exception("Redis connection failed")

        # Patch the manager getters (patching where they're imported FROM, not where they're used)
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue_manager):

            # Execute failsafe - should not raise exception
            try:
                project_monitor._check_and_process_waiting_issues_failsafe()
            except Exception as e:
                pytest.fail(f"Failsafe should not raise exceptions, but got: {e}")

        # Verify: Did NOT trigger agent (exception occurred)
        project_monitor.trigger_agent_for_status.assert_not_called()

    def test_failsafe_skips_inactive_pipelines(
        self, project_monitor, mock_lock_manager, mock_queue_manager
    ):
        """
        Test that failsafe only processes active pipelines
        """
        # The fixture already has one active and one inactive pipeline
        # The inactive one should not be processed

        # Setup: Pipeline is unlocked
        mock_lock_manager.get_lock.return_value = None

        # Setup: Waiting issue exists
        waiting_issue = {
            'issue_number': 155,
            'status': 'waiting',
            'title': 'Test Issue'
        }
        mock_queue_manager.get_next_waiting_issue.return_value = waiting_issue

        # Setup: Lock acquisition succeeds
        mock_lock_manager.try_acquire_lock.return_value = (True, "lock_acquired")

        # Patch the manager getters (patching where they're imported FROM, not where they're used)
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue_manager):

            # Execute failsafe
            project_monitor._check_and_process_waiting_issues_failsafe()

        # Verify: Only called once for the ACTIVE pipeline (not the inactive one)
        # get_pipeline_queue_manager should be called once
        assert mock_lock_manager.get_lock.call_count == 1
        mock_lock_manager.get_lock.assert_called_with('test_project', 'SDLC Execution')

    def test_failsafe_processes_multiple_projects(self, mock_config_manager):
        """
        Test that failsafe checks all visible projects
        """
        # Mock list_projects to return empty list (for initialization)
        mock_config_manager.list_projects.return_value = []

        # Setup: Two projects
        mock_config_manager.list_visible_projects.return_value = ['project1', 'project2']

        # Create pipeline configs for both projects
        pipeline1 = Mock()
        pipeline1.active = True
        pipeline1.board_name = "Board1"

        pipeline2 = Mock()
        pipeline2.active = True
        pipeline2.board_name = "Board2"

        config1 = Mock()
        config1.pipelines = [pipeline1]
        config1.github = {'repo': 'org/repo1'}
        config1.orchestrator = {"polling_interval": 30}

        config2 = Mock()
        config2.pipelines = [pipeline2]
        config2.github = {'repo': 'org/repo2'}
        config2.orchestrator = {"polling_interval": 30}

        # Mock get_project_config to return different configs
        mock_config_manager.get_project_config.side_effect = [config1, config2]

        # Create monitor
        task_queue = Mock()
        monitor = ProjectMonitor(task_queue, mock_config_manager)
        monitor.trigger_agent_for_status = Mock()
        monitor.get_issue_column_sync = Mock(return_value='Development')

        # Setup mocks
        mock_lock_manager = Mock()
        mock_lock_manager.get_lock.return_value = None  # Unlocked

        mock_queue_manager = Mock()
        mock_queue_manager.get_next_waiting_issue.return_value = None  # No waiting issues

        # Patch the manager getters (patching where they're imported FROM, not where they're used)
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue_manager):

            # Execute failsafe
            monitor._check_and_process_waiting_issues_failsafe()

        # Verify: Checked both projects
        assert mock_lock_manager.get_lock.call_count == 2
        mock_lock_manager.get_lock.assert_any_call('project1', 'Board1')
        mock_lock_manager.get_lock.assert_any_call('project2', 'Board2')

    def test_failsafe_with_lock_already_acquired_by_same_issue(
        self, project_monitor, mock_lock_manager, mock_queue_manager
    ):
        """
        Test that failsafe handles case where lock is already held by the same issue
        (though this shouldn't normally happen with waiting issues)
        """
        # Setup: Pipeline is unlocked
        mock_lock_manager.get_lock.return_value = None

        # Setup: Waiting issue exists
        waiting_issue = {
            'issue_number': 155,
            'status': 'waiting',
            'title': 'Test Issue'
        }
        mock_queue_manager.get_next_waiting_issue.return_value = waiting_issue

        # Setup: Lock already held by same issue (returns True with "already_holds_lock")
        mock_lock_manager.try_acquire_lock.return_value = (True, "already_holds_lock")

        # Patch the manager getters (patching where they're imported FROM, not where they're used)
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue_manager):

            # Execute failsafe
            project_monitor._check_and_process_waiting_issues_failsafe()

        # Verify: Issue was marked as active
        mock_queue_manager.mark_issue_active.assert_called_once_with(155)

        # Verify: Agent was triggered (we have the lock)
        project_monitor.trigger_agent_for_status.assert_called_once()
