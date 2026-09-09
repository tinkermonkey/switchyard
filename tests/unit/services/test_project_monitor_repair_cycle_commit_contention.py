"""
Regression tests for #148: the LIVE repair-cycle consumer's handling of an
auto-commit that did not land.

Originally (#148 first pass): commit_agent_changes() re-raises a resource-lock
timeout, and _monitor_repair_cycle_container swallowed that raise into a bare
`except Exception: logger.error(...)`, then fell straight into
`if overall_success:` — auto-advancing the issue, ending the run as a success
and cleaning up the repair-cycle state with the fix still uncommitted on disk.

Then (#148 C2, this file's current shape): routing it to the ordinary
contention path — release the board lock, let the next poll retry — was ALSO
wrong, and uniquely so. auto_commit only takes the project_checkout lock when
the directory is the SHARED base clone (is_base_clone_dir(); epic worktrees are
not gated by it), so this timeout proves the fix is uncommitted in a directory
the next dispatched issue will `git checkout` into with no stash and no reset.
Every OTHER contention outcome means "nothing ran, nothing is dirty"; this one
means "a fix is sitting in shared state". So it retains the board lock via
mark_failed() instead — the is_frozen precedent one step further — and says so
on the issue.

And (#148 I1): CommitResult now names the difference between an empty diff and
a genuine commit failure, so the live path gates on the latter exactly as its
restart-recovery twin in services/agent_container_recovery.py already did.

The closure runs in a daemon thread, so these tests capture the thread target at
launch and run it synchronously.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch

from services.auto_commit import CommitResult
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
    instance to raise, or a CommitResult to return).

    Returns (monitor, progression, tracker, cleanup, github) for inspection.
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

    return monitor, progression, tracker, cleanup, github


def _recorded_outcomes(tracker):
    return [
        call.kwargs.get('outcome')
        for call in tracker.record_execution_outcome.call_args_list
    ]


def _lock_timeout():
    return ProjectCheckoutLockTimeoutError(
        f"Could not acquire 'project_checkout' lock for project '{PROJECT}' within 10900.0s"
    )


def _posted(github):
    """The single repair-cycle summary comment, or None if none was posted."""
    calls = github.post_agent_output.await_args_list or github.post_agent_output.call_args_list
    if not calls:
        return None
    return calls[-1].args[1]


class TestAutoCommitLockContention:
    def test_the_issue_is_not_auto_advanced_with_the_fix_uncommitted(self):
        """The defect this exists for: the fix is still on disk, so advancing
        hands the next stage — and the PR reviewed downstream — no fix at all."""
        _, progression, _, _, _ = _run_monitor(commit=_lock_timeout())
        progression.move_issue_to_column.assert_not_called()

    def test_the_outcome_is_recorded_as_contention_not_success(self):
        _, _, tracker, _, _ = _run_monitor(commit=_lock_timeout())
        assert _recorded_outcomes(tracker) == ['lock_contention']

    def test_the_board_lock_is_retained_because_the_shared_clone_is_dirty(self):
        """#148 C2. This is the ONE contention outcome that must not release:
        the fix is uncommitted in the shared base clone (auto_commit only takes
        the project_checkout lock for that directory), and the next issue the
        failsafe pulls in runs a plain `git checkout` into it."""
        monitor, _, _, _, _ = _run_monitor(commit=_lock_timeout())
        monitor.pipeline_run_manager.mark_failed.assert_called_once()
        kwargs = monitor.pipeline_run_manager.mark_failed.call_args.kwargs
        assert 'uncommitted' in kwargs['reason']

    def test_the_run_is_not_released(self):
        """end_pipeline_run(retain_lock=False) here would hand the dirty shared
        clone to the next dispatched issue. mark_failed() is used rather than
        end_pipeline_run(retain_lock=True) because only mark_failed sets
        retained_reason — without it the lock is reclaimable as stale."""
        monitor, _, _, _, _ = _run_monitor(commit=_lock_timeout())
        monitor.pipeline_run_manager.end_pipeline_run.assert_not_called()

    def test_the_repair_cycle_state_is_kept_for_the_retry(self):
        _, _, _, cleanup, _ = _run_monitor(commit=_lock_timeout())
        cleanup.assert_not_called()

    def test_the_success_comment_is_corrected_not_posted(self):
        """#148 I3: the summary used to be posted BEFORE the commit was even
        attempted, so GitHub showed '✅ Repair Cycle Complete' on an issue that
        then never moved again."""
        _, _, _, _, github = _run_monitor(commit=_lock_timeout())
        posted = _posted(github)
        assert posted is not None
        assert 'Repair Cycle Complete' not in posted
        assert 'not committed' in posted.lower() or 'not Committed'.lower() in posted.lower()
        assert 'retained' in posted.lower()

    def test_a_wrapped_lock_timeout_is_recognised_too(self):
        cause = _lock_timeout()
        try:
            raise Exception("Auto-commit failed") from cause
        except Exception as wrapped:
            monitor, progression, tracker, _, _ = _run_monitor(commit=wrapped)
        progression.move_issue_to_column.assert_not_called()
        assert _recorded_outcomes(tracker) == ['lock_contention']
        monitor.pipeline_run_manager.mark_failed.assert_called_once()


class TestSuccessfulCommitIsUnchanged:
    """Control: a green cycle whose commit landed still advances and completes."""

    def test_the_issue_advances_and_the_run_ends_successfully(self):
        monitor, progression, tracker, cleanup, github = _run_monitor(
            commit=CommitResult.COMMITTED
        )
        progression.move_issue_to_column.assert_called_once()
        assert progression.move_issue_to_column.call_args.kwargs['target_column'] == 'Staged'
        assert _recorded_outcomes(tracker) == ['success']
        monitor.pipeline_run_manager.end_pipeline_run.assert_called_once()
        assert monitor.pipeline_run_manager.end_pipeline_run.call_args.kwargs['reason'] == (
            "Repair cycle completed successfully"
        )
        cleanup.assert_called_once()
        assert 'Repair Cycle Complete' in _posted(github)


class TestNothingToCommitIsUnchanged:
    """
    Control for #148 I1: an empty diff is NOT a failure and its behavior is
    unchanged — the issue still advances and the run still ends as a success.
    This is the case whose ambiguity with a genuine failure was the stated reason
    the live path went ungated; CommitResult removes the ambiguity without
    changing this outcome.
    """

    def test_nothing_to_commit_still_advances_and_records_success(self):
        monitor, progression, tracker, cleanup, github = _run_monitor(
            commit=CommitResult.NOTHING_TO_COMMIT
        )
        progression.move_issue_to_column.assert_called_once()
        assert _recorded_outcomes(tracker) == ['success']
        monitor.pipeline_run_manager.mark_failed.assert_not_called()
        cleanup.assert_called_once()
        assert 'Repair Cycle Complete' in _posted(github)


class TestGenuineCommitFailureIsGated:
    """
    #148 I1: `overall_success and CommitResult.FAILED` is the
    "Repair cycle passed but its fix was not committed" case that the
    restart-recovery twin in services/agent_container_recovery.py has always
    called mark_failed() for. The live path merely logged it as
    "No changes to commit" and auto-advanced, so the bug stayed fully open here
    for every commit failure that was not a lock timeout.
    """

    def test_a_failed_commit_does_not_advance(self):
        _, progression, _, _, _ = _run_monitor(commit=CommitResult.FAILED)
        progression.move_issue_to_column.assert_not_called()

    def test_a_failed_commit_is_recorded_as_failure_not_success(self):
        _, _, tracker, _, _ = _run_monitor(commit=CommitResult.FAILED)
        assert _recorded_outcomes(tracker) == ['failure']

    def test_a_failed_commit_retains_the_lock_like_the_twin(self):
        monitor, _, _, cleanup, _ = _run_monitor(commit=CommitResult.FAILED)
        monitor.pipeline_run_manager.mark_failed.assert_called_once()
        monitor.pipeline_run_manager.end_pipeline_run.assert_not_called()
        cleanup.assert_not_called()

    def test_an_ordinary_commit_exception_is_treated_the_same(self):
        """A non-lock exception out of commit_agent_changes() means the same
        thing as CommitResult.FAILED: the fix did not land."""
        monitor, progression, tracker, _, _ = _run_monitor(
            commit=RuntimeError("git index.lock exists")
        )
        progression.move_issue_to_column.assert_not_called()
        assert _recorded_outcomes(tracker) == ['failure']
        monitor.pipeline_run_manager.mark_failed.assert_called_once()

    def test_the_comment_says_the_fix_did_not_land(self):
        _, _, _, _, github = _run_monitor(commit=CommitResult.FAILED)
        posted = _posted(github)
        assert 'Repair Cycle Complete' not in posted
        assert 'could not be committed' in posted.lower()
