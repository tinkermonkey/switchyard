"""
Regression tests for #148: the LIVE repair-cycle consumer must route its
auto-commit's lock timeout to the contention path, exactly as the
restart-recovery twin in services/agent_container_recovery.py does.

services/auto_commit.py's commit_agent_changes() re-raises a resource-lock
timeout instead of returning False, because False is indistinguishable from
"nothing to commit" and would let the maker's uncommitted work be read as
absent. project_monitor._monitor_repair_cycle_container swallowed that raise
into a bare `except Exception: logger.error(...)` and then fell straight into
`if overall_success:` — auto-advancing the issue to the next column, ending the
pipeline run as a success and cleaning up the repair-cycle state, with the
cycle's fix still sitting uncommitted on disk. The PR reviewed downstream
contained no fix, and nothing anywhere said so.

The closure runs in a daemon thread, so these tests capture the thread target at
launch and run it synchronously.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch

from services.project_monitor import ProjectMonitor
from services.project_checkout_lock import ProjectCheckoutLockTimeoutError

PROJECT = 'test-project'
BOARD = 'Development'
ISSUE = 7373
STATUS = 'Testing'
RUN_ID = 'run-repair'
CONTAINER = f'repair-cycle-{PROJECT}-{ISSUE}'


def _monitor():
    monitor = object.__new__(ProjectMonitor)
    monitor.task_queue = MagicMock()
    monitor.pipeline_run_manager = MagicMock()
    monitor.pipeline_run_manager.end_pipeline_run.return_value = True
    monitor.pipeline_run_manager.mark_failed.return_value = True
    monitor.config_manager = MagicMock()
    monitor.decision_events = MagicMock()
    return monitor


def _project_config():
    config = MagicMock()
    config.github = {'org': 'test-org', 'repo': 'test-repo'}
    return config


def _workflow_template():
    template = MagicMock()
    testing = MagicMock()
    testing.name = STATUS
    staged = MagicMock()
    staged.name = 'Staged'
    template.columns = [testing, staged]
    return template


def _run_monitor(commit):
    """
    Drive _monitor_repair_cycle_container's thread body for a repair cycle that
    finished green (exit 0, overall_success=True), with
    auto_commit_service.commit_agent_changes stubbed by `commit` (an exception
    instance to raise, or a bool to return).

    Returns (monitor, progression, tracker, cleanup) for inspection.
    """
    monitor = _monitor()
    progression = MagicMock()
    tracker = MagicMock()
    cleanup = MagicMock()

    if isinstance(commit, BaseException):
        raised = commit

        async def commit_agent_changes(**kwargs):
            raise raised
    else:
        returned = commit

        async def commit_agent_changes(**kwargs):
            return returned

    auto_commit_service = MagicMock()
    auto_commit_service.commit_agent_changes = commit_agent_changes

    process = MagicMock()
    process.stdout.read.return_value = '0\n'

    github = MagicMock()
    github.post_agent_output = AsyncMock()

    captured = {}

    def fake_thread(target=None, daemon=None, **kwargs):
        captured['target'] = target
        return MagicMock()

    with patch('threading.Thread', side_effect=fake_thread):
        monitor._monitor_repair_cycle_container(
            container_name=CONTAINER,
            project_name=PROJECT,
            board_name=BOARD,
            issue_number=ISSUE,
            status=STATUS,
            repository='test-repo',
            project_config=_project_config(),
            workflow_template=_workflow_template(),
            agent_name='senior_software_engineer',
            pipeline_run_id=RUN_ID,
            project_dir=f'/workspace/{PROJECT}',
        )

    with patch('subprocess.Popen', return_value=process), \
         patch('subprocess.run'), \
         patch(
             'services.project_monitor._capture_container_logs_via_follower',
             return_value=(MagicMock(), lambda: ''),
         ), \
         patch(
             'services.project_monitor._load_repair_cycle_result_from_redis',
             return_value={'overall_success': True, 'total_agent_calls': 3},
         ), \
         patch('services.project_monitor._cleanup_repair_cycle_state', cleanup), \
         patch('monitoring.observability.get_observability_manager', return_value=MagicMock()), \
         patch('services.github_integration.GitHubIntegration', return_value=github), \
         patch('services.auto_commit.auto_commit_service', auto_commit_service), \
         patch('services.pipeline_progression.PipelineProgression', return_value=progression), \
         patch('services.work_execution_state.work_execution_tracker', tracker):

        captured['target']()

    return monitor, progression, tracker, cleanup


def _recorded_outcomes(tracker):
    return [
        call.kwargs.get('outcome')
        for call in tracker.record_execution_outcome.call_args_list
    ]


def _lock_timeout():
    return ProjectCheckoutLockTimeoutError(
        f"Could not acquire 'project_checkout' lock for project '{PROJECT}' within 10900.0s"
    )


class TestAutoCommitLockContention:
    def test_the_issue_is_not_auto_advanced_with_the_fix_uncommitted(self):
        """The defect this exists for: the fix is still on disk, so advancing
        hands the next stage — and the PR reviewed downstream — no fix at all."""
        _, progression, _, _ = _run_monitor(commit=_lock_timeout())
        progression.move_issue_to_column.assert_not_called()

    def test_the_outcome_is_recorded_as_contention_not_success(self):
        _, _, tracker, _ = _run_monitor(commit=_lock_timeout())
        assert _recorded_outcomes(tracker) == ['lock_contention']

    def test_the_run_is_released_without_retaining_the_lock(self):
        monitor, _, _, _ = _run_monitor(commit=_lock_timeout())
        monitor.pipeline_run_manager.end_pipeline_run.assert_called_once()
        kwargs = monitor.pipeline_run_manager.end_pipeline_run.call_args.kwargs
        assert kwargs['retain_lock'] is False
        assert 'resource-lock timeout' in kwargs['reason']

    def test_the_run_is_not_marked_failed(self):
        """mark_failed() durably retains the BOARD's lock pending
        scripts/release_lock.py — over a lock that was working as designed."""
        monitor, _, _, _ = _run_monitor(commit=_lock_timeout())
        monitor.pipeline_run_manager.mark_failed.assert_not_called()

    def test_the_repair_cycle_state_is_kept_for_the_retry(self):
        _, _, _, cleanup = _run_monitor(commit=_lock_timeout())
        cleanup.assert_not_called()

    def test_a_wrapped_lock_timeout_is_recognised_too(self):
        cause = _lock_timeout()
        try:
            raise Exception("Auto-commit failed") from cause
        except Exception as wrapped:
            monitor, progression, tracker, _ = _run_monitor(commit=wrapped)
        progression.move_issue_to_column.assert_not_called()
        assert _recorded_outcomes(tracker) == ['lock_contention']
        monitor.pipeline_run_manager.mark_failed.assert_not_called()


class TestSuccessfulCommitIsUnchanged:
    """Control: a green cycle whose commit landed still advances and completes."""

    def test_the_issue_advances_and_the_run_ends_successfully(self):
        monitor, progression, tracker, cleanup = _run_monitor(commit=True)
        progression.move_issue_to_column.assert_called_once()
        assert progression.move_issue_to_column.call_args.kwargs['target_column'] == 'Staged'
        assert _recorded_outcomes(tracker) == ['success']
        monitor.pipeline_run_manager.end_pipeline_run.assert_called_once()
        assert monitor.pipeline_run_manager.end_pipeline_run.call_args.kwargs['reason'] == (
            "Repair cycle completed successfully"
        )
        cleanup.assert_called_once()


class TestNothingToCommitIsUnchanged:
    """
    Control: a False return still means "nothing to commit, or a failure already
    logged by commit_agent_changes() itself" on this path, and must not be
    silently promoted into contention. Unlike the restart-recovery twin, the live
    path deliberately does NOT gate its auto-advance on commit_success — that is
    pre-existing behavior outside this fix's scope, and changing it would block
    every repair cycle that legitimately had nothing left to commit.
    """

    def test_a_false_return_still_advances_and_records_success(self):
        monitor, progression, tracker, _ = _run_monitor(commit=False)
        progression.move_issue_to_column.assert_called_once()
        assert _recorded_outcomes(tracker) == ['success']
        monitor.pipeline_run_manager.mark_failed.assert_not_called()

    def test_an_ordinary_commit_exception_still_advances(self):
        """Only a lock timeout suppresses the advance — an ordinary commit error
        keeps the pre-existing 'changes are still in workspace' behavior."""
        monitor, progression, tracker, _ = _run_monitor(
            commit=RuntimeError("git push rejected")
        )
        progression.move_issue_to_column.assert_called_once()
        assert _recorded_outcomes(tracker) == ['success']
