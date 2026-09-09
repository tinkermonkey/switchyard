"""
Integration test for review cycle completion and lock management

Tests the complete review cycle workflow including the lock management fix
in the finally block to ensure pipeline locks are properly handled when
review cycles complete.
"""

import pytest
import asyncio
from unittest.mock import Mock, MagicMock, patch, AsyncMock, call
from datetime import datetime
from services.pipeline_queue_manager import ResetResult
from services.project_monitor import ProjectMonitor
from config.manager import ConfigManager


@pytest.fixture
def mock_config_manager():
    """Create a mock config manager with full workflow configuration"""
    config_manager = Mock(spec=ConfigManager)

    # Create workflow template with columns and exit columns
    column_dev = Mock()
    column_dev.name = "Development"
    column_dev.agent = "senior_software_engineer"
    column_dev.maker_agent = None
    column_dev.stage_mapping = "coding"
    column_dev.max_iterations = None

    column_review = Mock()
    column_review.name = "Code Review"
    column_review.agent = "code_review_specialist"
    column_review.maker_agent = "senior_software_engineer"
    column_review.stage_mapping = "code_review"
    column_review.max_iterations = 3

    column_testing = Mock()
    column_testing.name = "Testing"
    column_testing.agent = "qa_engineer"
    column_testing.maker_agent = None
    column_testing.stage_mapping = "testing"
    column_testing.max_iterations = None

    column_done = Mock()
    column_done.name = "Done"
    column_done.agent = None
    column_done.maker_agent = None
    column_done.stage_mapping = None
    column_done.max_iterations = None

    workflow_template = Mock()
    workflow_template.columns = [column_dev, column_review, column_testing, column_done]
    workflow_template.pipeline_exit_columns = ["Done", "Cancelled"]

    # Create pipeline config
    pipeline = Mock()
    pipeline.board_name = "SDLC Execution"
    pipeline.workflow = "sdlc_execution_workflow"
    pipeline.template = "sdlc_execution"
    pipeline.workspace = "issues"

    # Create project config
    project_config = Mock()
    project_config.pipelines = [pipeline]
    project_config.github = {
        'org': 'test-org',
        'repo': 'test-repo'
    }
    project_config.orchestrator = {"polling_interval": 15}

    # Setup mock returns
    config_manager.list_projects.return_value = []
    config_manager.get_project_config.return_value = project_config
    config_manager.get_workflow_template.return_value = workflow_template
    config_manager.get_pipeline_template.return_value = Mock(stages=[])

    return config_manager


@pytest.fixture
def project_monitor(mock_config_manager):
    """Create ProjectMonitor instance with mocked dependencies"""
    task_queue = Mock()
    monitor = ProjectMonitor(task_queue, mock_config_manager)

    # Mock external dependencies
    monitor.get_issue_details = Mock(return_value={
        'title': 'Test Issue',
        'body': 'Test body',
        'state': 'OPEN',
        'url': 'https://github.com/test-org/test-repo/issues/123'
    })
    monitor.get_previous_stage_context = Mock(return_value={
        'previous_agent': 'senior_software_engineer',
        'previous_output': 'Code implementation completed'
    })
    monitor.pipeline_run_manager = Mock()
    monitor.pipeline_run_manager.get_or_create_pipeline_run.return_value = (Mock(id='test-run-123'), False)
    monitor.pipeline_run_manager.end_pipeline_run = Mock()

    return monitor


@pytest.mark.integration
@pytest.mark.asyncio
class TestReviewCycleCompletion:
    """Test review cycle completion scenarios"""

    async def test_review_cycle_completes_to_non_exit_column_keeps_lock(
        self,
        project_monitor,
        mock_config_manager
    ):
        """
        Test: Review cycle completes with approval, moves to Testing (non-exit column)
        Expected: Pipeline lock should NOT be released (retained for next stage)
        """
        # Setup - Get the workflow template
        project_config = mock_config_manager.get_project_config("test_project")
        workflow_template = mock_config_manager.get_workflow_template("sdlc_execution_workflow")
        column = workflow_template.columns[1]  # Code Review column

        # Mock lock manager
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager') as mock_get_lock_mgr:
            mock_lock_mgr = Mock()
            mock_lock_mgr.try_acquire_lock.return_value = (True, "lock_acquired")
            mock_lock_mgr.release_lock = Mock()
            mock_get_lock_mgr.return_value = mock_lock_mgr

            # Mock review cycle executor to return "Testing" (non-exit column)
            with patch('services.review_cycle.review_cycle_executor') as mock_review_executor:
                # Simulate review cycle completing with approval -> move to Testing
                async def mock_start_review_cycle(*args, **kwargs):
                    # Return (next_column_name, success)
                    return "Testing", True

                mock_review_executor.start_review_cycle = AsyncMock(side_effect=mock_start_review_cycle)

                # Mock GitHub integration
                with patch('services.github_integration.GitHubIntegration') as mock_github_cls:
                    mock_github = Mock()
                    mock_github.post_agent_output = AsyncMock()
                    mock_github_cls.return_value = mock_github

                    # Mock state manager
                    with patch('config.state_manager.state_manager') as mock_state_mgr:
                        mock_state_mgr.get_discussion_for_issue.return_value = None

                        # Mock pipeline queue manager to avoid actual queue processing
                        with patch('services.pipeline_queue_manager.get_pipeline_queue_manager') as mock_get_queue_mgr:
                            mock_queue_mgr = Mock()
                            mock_queue_mgr.peek_next_waiting_issue.return_value = None
                            mock_get_queue_mgr.return_value = mock_queue_mgr

                            # Execute the review cycle (runs in background thread, but we'll wait a bit)
                            result = project_monitor._start_review_cycle_for_issue(
                                project_name="test_project",
                                board_name="SDLC Execution",
                                issue_number=123,
                                status="Code Review",
                                repository="test-repo",
                                project_config=project_config,
                                pipeline_config=project_config.pipelines[0],
                                workflow_template=workflow_template,
                                column=column
                            )

                            # Give background thread time to execute
                            await asyncio.sleep(0.5)

                            # Verify lock was acquired
                            mock_lock_mgr.try_acquire_lock.assert_called_once_with(
                                project="test_project",
                                board="SDLC Execution",
                                issue_number=123
                            )

                            # CRITICAL: Verify lock was NOT released (Testing is not an exit column)
                            # The lock should be retained for the next stage (Testing)
                            mock_lock_mgr.release_lock.assert_not_called()

    async def test_review_cycle_completes_to_exit_column_movement_handled_elsewhere(
        self,
        project_monitor,
        mock_config_manager
    ):
        """
        Test: Review cycle completes with approval, moves to Done (exit column)
        Expected: Lock release is handled by card movement handler, NOT in finally block

        NOTE: The review cycle finally block only releases locks if the issue is STARTING
        in an exit column. When a review cycle MOVES an issue to an exit column, the
        card movement is handled by PipelineProgression.move_issue_to_column which
        triggers the card movement handler that manages lock release.
        """
        # Setup
        project_config = mock_config_manager.get_project_config("test_project")
        workflow_template = mock_config_manager.get_workflow_template("sdlc_execution_workflow")
        column = workflow_template.columns[1]  # Code Review column

        # Mock lock manager
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager') as mock_get_lock_mgr:
            mock_lock_mgr = Mock()
            mock_lock_mgr.try_acquire_lock.return_value = (True, "lock_acquired")
            mock_lock_mgr.release_lock = Mock()
            mock_get_lock_mgr.return_value = mock_lock_mgr

            # Mock review cycle executor to return "Done" (exit column)
            with patch('services.review_cycle.review_cycle_executor') as mock_review_executor:
                # Simulate review cycle completing with approval -> move to Done
                async def mock_start_review_cycle(*args, **kwargs):
                    return "Done", True

                mock_review_executor.start_review_cycle = AsyncMock(side_effect=mock_start_review_cycle)

                # Mock GitHub integration
                with patch('services.github_integration.GitHubIntegration') as mock_github_cls:
                    mock_github = Mock()
                    mock_github.post_agent_output = AsyncMock()
                    mock_github_cls.return_value = mock_github

                    # Mock state manager
                    with patch('config.state_manager.state_manager') as mock_state_mgr:
                        mock_state_mgr.get_discussion_for_issue.return_value = None

                        # Mock pipeline queue manager
                        with patch('services.pipeline_queue_manager.get_pipeline_queue_manager') as mock_get_queue_mgr:
                            mock_queue_mgr = Mock()
                            mock_queue_mgr.peek_next_waiting_issue.return_value = None
                            mock_get_queue_mgr.return_value = mock_queue_mgr

                            # Execute the review cycle
                            result = project_monitor._start_review_cycle_for_issue(
                                project_name="test_project",
                                board_name="SDLC Execution",
                                issue_number=123,
                                status="Code Review",
                                repository="test-repo",
                                project_config=project_config,
                                pipeline_config=project_config.pipelines[0],
                                workflow_template=workflow_template,
                                column=column
                            )

                            # Give background thread time to execute
                            await asyncio.sleep(0.5)

                            # Verify lock was acquired
                            mock_lock_mgr.try_acquire_lock.assert_called_once()

                            # CRITICAL: Verify lock was NOT released in finally block
                            # Lock release for exit columns is handled by the card movement handler
                            # (PipelineProgression.move_issue_to_column), not in review cycle finally
                            mock_lock_mgr.release_lock.assert_not_called()

    async def test_workflow_lookup_error_keeps_lock_safe_default(
        self,
        project_monitor,
        mock_config_manager
    ):
        """
        Test: Workflow lookup error in finally block should default to NOT releasing lock
        This is the safe behavior - better to keep lock than corrupt state
        """
        # Setup
        project_config = mock_config_manager.get_project_config("test_project")
        workflow_template = mock_config_manager.get_workflow_template("sdlc_execution_workflow")
        column = workflow_template.columns[1]  # Code Review column

        # Mock lock manager
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager') as mock_get_lock_mgr:
            mock_lock_mgr = Mock()
            mock_lock_mgr.try_acquire_lock.return_value = (True, "lock_acquired")
            mock_lock_mgr.release_lock = Mock()
            mock_get_lock_mgr.return_value = mock_lock_mgr

            # Make get_workflow_template raise an error when called from finally block
            call_count = [0]
            original_get_workflow = mock_config_manager.get_workflow_template

            def get_workflow_with_error(workflow_name):
                call_count[0] += 1
                # First call succeeds (from main code), second call fails (from finally block)
                if call_count[0] == 1:
                    return original_get_workflow(workflow_name)
                else:
                    raise Exception("Simulated workflow lookup error in finally block")

            mock_config_manager.get_workflow_template = Mock(side_effect=get_workflow_with_error)

            # Mock review cycle executor
            with patch('services.review_cycle.review_cycle_executor') as mock_review_executor:
                async def mock_start_review_cycle(*args, **kwargs):
                    return "Testing", True

                mock_review_executor.start_review_cycle = AsyncMock(side_effect=mock_start_review_cycle)

                # Mock GitHub integration
                with patch('services.github_integration.GitHubIntegration') as mock_github_cls:
                    mock_github = Mock()
                    mock_github.post_agent_output = AsyncMock()
                    mock_github_cls.return_value = mock_github

                    # Mock state manager
                    with patch('config.state_manager.state_manager') as mock_state_mgr:
                        mock_state_mgr.get_discussion_for_issue.return_value = None

                        # Mock pipeline queue manager
                        with patch('services.pipeline_queue_manager.get_pipeline_queue_manager') as mock_get_queue_mgr:
                            mock_queue_mgr = Mock()
                            mock_queue_mgr.peek_next_waiting_issue.return_value = None
                            mock_get_queue_mgr.return_value = mock_queue_mgr

                            # Execute the review cycle
                            result = project_monitor._start_review_cycle_for_issue(
                                project_name="test_project",
                                board_name="SDLC Execution",
                                issue_number=123,
                                status="Code Review",
                                repository="test-repo",
                                project_config=project_config,
                                pipeline_config=project_config.pipelines[0],
                                workflow_template=workflow_template,
                                column=column
                            )

                            # Give background thread time to execute
                            await asyncio.sleep(0.5)

                            # Verify lock was acquired
                            mock_lock_mgr.try_acquire_lock.assert_called_once()

                            # CRITICAL: Verify lock was NOT released due to error (safe default)
                            mock_lock_mgr.release_lock.assert_not_called()



@pytest.mark.integration
@pytest.mark.asyncio
class TestReviewCycleCompletionQueueDispatch:
    """Phase 2 (issue #57): the review-cycle-completion exit-column handler
    (inside _start_review_cycle_for_issue's background thread finally block)
    is one of the "get next -> try_acquire_lock -> mark_issue_active" dispatch
    call sites generalized to loop over available slots. Today's
    available_slots is hardcoded to 1 (PipelineLockManager still allows only
    one lock per (project, board)), so get_next_n_waiting_issues(1) must
    drive the exact same single-dispatch behavior as the pre-#57
    get_next_waiting_issue() call it replaced.

    Note: is_exit_column here is based on the STARTING status passed into
    _start_review_cycle_for_issue (i.e. the review cycle already started in
    an exit column), NOT wherever start_review_cycle() says it moved to --
    see the sibling tests above documenting the same behavior.
    """

    async def test_dispatches_single_next_queued_issue_after_exit_column_release(
        self,
        project_monitor,
        mock_config_manager
    ):
        """Byte-identical-at-capacity-1 check: with exactly one waiting issue
        queued, the exit-column finally block must release the lock for the
        completed issue, fetch the next queued issue, acquire the lock for
        it, mark it active, and dispatch a task for it -- the same sequence
        get_next_waiting_issue() drove before #57."""
        project_config = mock_config_manager.get_project_config("test_project")
        workflow_template = mock_config_manager.get_workflow_template("sdlc_execution_workflow")
        # Use the "Code Review" column object (has a real agent/maker_agent,
        # needed for run_cycle_in_thread's report text) while passing
        # status="Done" -- is_exit_column is keyed off the STARTING status
        # param, not the column object's name (see class docstring).
        review_column = workflow_template.columns[1]  # "Code Review"

        project_monitor.get_issue_column_sync = Mock(return_value='Development')

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager') as mock_get_lock_mgr:
            mock_lock_mgr = Mock()
            mock_lock_mgr.try_acquire_lock.return_value = (True, "lock_acquired")
            mock_lock_mgr.release_lock.return_value = True
            mock_get_lock_mgr.return_value = mock_lock_mgr

            with patch('services.review_cycle.review_cycle_executor') as mock_review_executor:
                async def mock_start_review_cycle(*args, **kwargs):
                    return "Done", True

                mock_review_executor.start_review_cycle = AsyncMock(side_effect=mock_start_review_cycle)

                with patch('services.github_integration.GitHubIntegration') as mock_github_cls:
                    mock_github = Mock()
                    mock_github.post_agent_output = AsyncMock()
                    mock_github_cls.return_value = mock_github

                    with patch('config.state_manager.state_manager') as mock_state_mgr:
                        mock_state_mgr.get_discussion_for_issue.return_value = None

                        with patch('services.pipeline_queue_manager.get_pipeline_queue_manager') as mock_get_queue_mgr, \
                             patch('services.pipeline_run.get_pipeline_run_manager') as mock_get_run_mgr:
                            mock_queue_mgr = Mock()
                            mock_queue_mgr.get_next_n_waiting_issues.return_value = [
                                {'issue_number': 456, 'position_in_column': 0}
                            ]
                            mock_get_queue_mgr.return_value = mock_queue_mgr

                            mock_run_mgr = Mock()
                            mock_run_mgr.ensure_pipeline_run_for_task.return_value = 'run-456'
                            mock_get_run_mgr.return_value = mock_run_mgr

                            project_monitor._start_review_cycle_for_issue(
                                project_name="test_project",
                                board_name="SDLC Execution",
                                issue_number=123,
                                status="Done",
                                repository="test-repo",
                                project_config=project_config,
                                pipeline_config=project_config.pipelines[0],
                                workflow_template=workflow_template,
                                column=review_column
                            )

                            await asyncio.sleep(0.5)

                        # Lock released for the completed issue (123)
                        mock_lock_mgr.release_lock.assert_called_once_with(
                            "test_project", "SDLC Execution", 123
                        )

                        # Queried for exactly 1 slot (today's hardcoded available_slots)
                        mock_queue_mgr.get_next_n_waiting_issues.assert_called_once_with(1)

                        # Lock acquired for BOTH the original issue and the next queued one
                        assert mock_lock_mgr.try_acquire_lock.call_args_list == [
                            call(project="test_project", board="SDLC Execution", issue_number=123),
                            call(project="test_project", board="SDLC Execution", issue_number=456),
                        ]

                        # Next issue marked active and a task dispatched for it
                        mock_queue_mgr.mark_issue_active.assert_called_once_with(456)
                        project_monitor.task_queue.enqueue.assert_called_once()
                        dispatched_task = project_monitor.task_queue.enqueue.call_args[0][0]
                        assert dispatched_task.context['issue_number'] == 456
                        assert dispatched_task.context['trigger'] == 'review_cycle_completion_queue_processing'

    async def test_no_dispatch_when_queue_empty(
        self,
        project_monitor,
        mock_config_manager
    ):
        """Control case: empty queue (get_next_n_waiting_issues(1) -> []) must
        release the lock for the completed issue and dispatch nothing --
        matching the pre-#57 get_next_waiting_issue() -> None behavior."""
        project_config = mock_config_manager.get_project_config("test_project")
        workflow_template = mock_config_manager.get_workflow_template("sdlc_execution_workflow")
        review_column = workflow_template.columns[1]  # "Code Review"

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager') as mock_get_lock_mgr:
            mock_lock_mgr = Mock()
            mock_lock_mgr.try_acquire_lock.return_value = (True, "lock_acquired")
            mock_lock_mgr.release_lock.return_value = True
            mock_get_lock_mgr.return_value = mock_lock_mgr

            with patch('services.review_cycle.review_cycle_executor') as mock_review_executor:
                async def mock_start_review_cycle(*args, **kwargs):
                    return "Done", True

                mock_review_executor.start_review_cycle = AsyncMock(side_effect=mock_start_review_cycle)

                with patch('services.github_integration.GitHubIntegration') as mock_github_cls:
                    mock_github = Mock()
                    mock_github.post_agent_output = AsyncMock()
                    mock_github_cls.return_value = mock_github

                    with patch('config.state_manager.state_manager') as mock_state_mgr:
                        mock_state_mgr.get_discussion_for_issue.return_value = None

                        with patch('services.pipeline_queue_manager.get_pipeline_queue_manager') as mock_get_queue_mgr:
                            mock_queue_mgr = Mock()
                            mock_queue_mgr.get_next_n_waiting_issues.return_value = []
                            mock_get_queue_mgr.return_value = mock_queue_mgr

                            project_monitor._start_review_cycle_for_issue(
                                project_name="test_project",
                                board_name="SDLC Execution",
                                issue_number=123,
                                status="Done",
                                repository="test-repo",
                                project_config=project_config,
                                pipeline_config=project_config.pipelines[0],
                                workflow_template=workflow_template,
                                column=review_column
                            )

                            await asyncio.sleep(0.5)

                        mock_lock_mgr.release_lock.assert_called_once_with(
                            "test_project", "SDLC Execution", 123
                        )
                        mock_queue_mgr.get_next_n_waiting_issues.assert_called_once_with(1)
                        mock_queue_mgr.mark_issue_active.assert_not_called()
                        # Only the initial try_acquire_lock for issue 123 -- none for a next issue
                        mock_lock_mgr.try_acquire_lock.assert_called_once_with(
                            project="test_project", board="SDLC Execution", issue_number=123
                        )
                        project_monitor.task_queue.enqueue.assert_not_called()


    async def test_rolls_back_lock_and_queue_when_dispatch_fails(
        self,
        project_monitor,
        mock_config_manager
    ):
        """REGRESSION (#142): this third dispatch call site rolled back ONLY
        the lock, leaving the queue entry at status='active'.
        get_next_n_waiting_issues() selects strictly on status=='waiting', so
        the issue was silently excluded from every future dispatch, forever --
        and the stranded-'active' sweep in scheduled_tasks cannot recover THIS
        site, because ensure_pipeline_run_for_task() has already created an
        active PipelineRun by the time the enqueue can fail and the sweep skips
        any entry that has one.

        The lock is released FIRST and the reset then runs under the
        mark_issue_active() compare-and-swap token (#147): holding the lock
        across the reset does not exclude a competing dispatcher, because
        try_acquire_lock() returns True/"already_holds_lock" for the current
        holder and trigger_agent_for_status() dispatches on that branch -- the
        unconditional release would then free the lock out from under a real
        agent."""
        project_config = mock_config_manager.get_project_config("test_project")
        workflow_template = mock_config_manager.get_workflow_template("sdlc_execution_workflow")
        review_column = workflow_template.columns[1]  # "Code Review"

        project_monitor.get_issue_column_sync = Mock(return_value='Development')
        # A Redis blip on the enqueue is the most plausible real trigger.
        project_monitor.task_queue.enqueue = Mock(side_effect=RuntimeError("redis down"))

        call_order = []

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager') as mock_get_lock_mgr:
            mock_lock_mgr = Mock()
            mock_lock_mgr.try_acquire_lock.return_value = (True, "lock_acquired")
            mock_lock_mgr.release_lock.side_effect = (
                lambda *a, **kw: call_order.append(f'release-{a[2]}') or True
            )
            mock_get_lock_mgr.return_value = mock_lock_mgr

            with patch('services.review_cycle.review_cycle_executor') as mock_review_executor:
                async def mock_start_review_cycle(*args, **kwargs):
                    return "Done", True

                mock_review_executor.start_review_cycle = AsyncMock(side_effect=mock_start_review_cycle)

                with patch('services.github_integration.GitHubIntegration') as mock_github_cls:
                    mock_github = Mock()
                    mock_github.post_agent_output = AsyncMock()
                    mock_github_cls.return_value = mock_github

                    with patch('config.state_manager.state_manager') as mock_state_mgr:
                        mock_state_mgr.get_discussion_for_issue.return_value = None

                        with patch('services.pipeline_queue_manager.get_pipeline_queue_manager') as mock_get_queue_mgr, \
                             patch('services.pipeline_run.get_pipeline_run_manager') as mock_get_run_mgr:
                            mock_queue_mgr = Mock()
                            mock_queue_mgr.get_next_n_waiting_issues.return_value = [
                                {'issue_number': 456, 'position_in_column': 0}
                            ]
                            mock_queue_mgr.mark_issue_active.return_value = '2026-01-01T00:00:00+00:00'
                            mock_queue_mgr.reset_issue_to_waiting.side_effect = (
                                lambda *a, **kw: call_order.append('reset') or True
                            )
                            mock_get_queue_mgr.return_value = mock_queue_mgr

                            mock_run_mgr = Mock()
                            mock_run_mgr.ensure_pipeline_run_for_task.return_value = 'run-456'
                            mock_get_run_mgr.return_value = mock_run_mgr

                            project_monitor._start_review_cycle_for_issue(
                                project_name="test_project",
                                board_name="SDLC Execution",
                                issue_number=123,
                                status="Done",
                                repository="test-repo",
                                project_config=project_config,
                                pipeline_config=project_config.pipelines[0],
                                workflow_template=workflow_template,
                                column=review_column
                            )

                            await asyncio.sleep(0.5)

                        mock_queue_mgr.mark_issue_active.assert_called_once_with(456)
                        # BOTH halves rolled back: lock first, then the entry
                        # under the compare-and-swap token mark_issue_active()
                        # returned.
                        mock_queue_mgr.reset_issue_to_waiting.assert_called_once_with(
                            456, expected_activated_at='2026-01-01T00:00:00+00:00'
                        )
                        assert call_order == ['release-123', 'release-456', 'reset']

    async def test_refused_compare_and_swap_is_not_reported_at_critical(
        self,
        project_monitor,
        mock_config_manager,
        caplog
    ):
        """REGRESSION (#147 review): a falsy return used to be paged as CRITICAL
        ("still 'active' and will be excluded from all future dispatch until a
        human intervenes"). A refused compare-and-swap means another dispatcher
        legitimately re-activated the issue, so leaving the entry 'active' is
        the CORRECT outcome, not an emergency -- and the other two falsy cases
        (NOT_ACTIVE / NOT_FOUND) are not even about an entry left active."""
        project_config = mock_config_manager.get_project_config("test_project")
        workflow_template = mock_config_manager.get_workflow_template("sdlc_execution_workflow")
        review_column = workflow_template.columns[1]  # "Code Review"

        project_monitor.get_issue_column_sync = Mock(return_value='Development')
        project_monitor.task_queue.enqueue = Mock(side_effect=RuntimeError("redis down"))

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager') as mock_get_lock_mgr:
            mock_lock_mgr = Mock()
            mock_lock_mgr.try_acquire_lock.return_value = (True, "lock_acquired")
            mock_lock_mgr.release_lock.return_value = True
            mock_get_lock_mgr.return_value = mock_lock_mgr

            with patch('services.review_cycle.review_cycle_executor') as mock_review_executor:
                async def mock_start_review_cycle(*args, **kwargs):
                    return "Done", True

                mock_review_executor.start_review_cycle = AsyncMock(side_effect=mock_start_review_cycle)

                with patch('services.github_integration.GitHubIntegration') as mock_github_cls:
                    mock_github = Mock()
                    mock_github.post_agent_output = AsyncMock()
                    mock_github_cls.return_value = mock_github

                    with patch('config.state_manager.state_manager') as mock_state_mgr:
                        mock_state_mgr.get_discussion_for_issue.return_value = None

                        with patch('services.pipeline_queue_manager.get_pipeline_queue_manager') as mock_get_queue_mgr, \
                             patch('services.pipeline_run.get_pipeline_run_manager') as mock_get_run_mgr:
                            mock_queue_mgr = Mock()
                            mock_queue_mgr.get_next_n_waiting_issues.return_value = [
                                {'issue_number': 456, 'position_in_column': 0}
                            ]
                            mock_queue_mgr.mark_issue_active.return_value = '2026-01-01T00:00:00+00:00'
                            # The entry was re-activated concurrently: the reset
                            # refuses and the entry stays 'active'.
                            mock_queue_mgr.reset_issue_to_waiting.return_value = (
                                ResetResult.REACTIVATED
                            )
                            mock_get_queue_mgr.return_value = mock_queue_mgr

                            mock_run_mgr = Mock()
                            mock_run_mgr.ensure_pipeline_run_for_task.return_value = 'run-456'
                            mock_get_run_mgr.return_value = mock_run_mgr

                            with caplog.at_level('DEBUG'):
                                project_monitor._start_review_cycle_for_issue(
                                    project_name="test_project",
                                    board_name="SDLC Execution",
                                    issue_number=123,
                                    status="Done",
                                    repository="test-repo",
                                    project_config=project_config,
                                    pipeline_config=project_config.pipelines[0],
                                    workflow_template=workflow_template,
                                    column=review_column
                                )

                                await asyncio.sleep(0.5)

                        assert not [
                            r for r in caplog.records if r.levelname == 'CRITICAL'
                        ], [
                            r.getMessage() for r in caplog.records
                            if r.levelname == 'CRITICAL'
                        ]
                        assert any(
                            'correct outcome' in record.getMessage()
                            for record in caplog.records
                        )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
