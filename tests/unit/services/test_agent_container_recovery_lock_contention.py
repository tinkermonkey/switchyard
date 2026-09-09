"""
Regression tests for #148: the restart-recovery consumer of a repair-cycle
result must honour 'lock_contention', not just 'frozen'.

pipeline/repair_cycle_runner.py persists {'overall_success': False,
'lock_contention': True, 'error': ...} to Redis (24h TTL) BEFORE exiting, so a
restart landing anywhere inside the multi-hour contention window leaves that
result for AgentContainerRecovery to pick up.
_process_completed_repair_cycle() read only result['frozen'], so a contention
result had overall_success=False and is_frozen=False and fell through to the
terminal else: mark_failed("Repair cycle failed") — durably retaining the
board's pipeline lock pending scripts/release_lock.py — plus a
"## ❌ Repair Cycle Failed" summary on an issue whose cycle never ran.
"""

import json
import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, mock_open, patch

from services.agent_container_recovery import AgentContainerRecovery
from services.auto_commit import CommitResult

PROJECT = 'test-project'
ISSUE = 5150
RUN_ID = 'run-xyz'
CONTAINER = f'repair-cycle-{PROJECT}-{ISSUE}'

CONTEXT = {
    'board': 'Development',
    'repository': 'test-repo',
    'column': 'Testing',
    'pipeline_run_id': RUN_ID,
    'project_dir': f'/workspace/{PROJECT}',
    'agent_name': 'senior_software_engineer',
}


def _recovery():
    recovery = object.__new__(AgentContainerRecovery)
    # None disables the Redis-backed comment-idempotency and tracking-cleanup
    # blocks, neither of which is under test here.
    recovery.redis = None
    return recovery


def _process(result, commit=None):
    """
    Drive _process_completed_repair_cycle() with `result`, and return the
    (run_manager, github, work_execution_tracker, progression) mocks so the
    caller can inspect which teardown decision was taken.

    `commit` is the auto_commit_service.commit_agent_changes stub (an async
    callable, or an exception instance to raise). Left as None when the result
    never reaches the commit at all.
    """
    recovery = _recovery()

    run_manager = MagicMock()
    run_manager.mark_failed.return_value = True
    active_run = MagicMock()
    active_run.id = RUN_ID
    run_manager.get_active_pipeline_run.return_value = active_run

    github = MagicMock()
    github.post_agent_output = AsyncMock()

    tracker = MagicMock()
    progression = MagicMock()

    if isinstance(commit, BaseException):
        raised = commit

        async def commit_agent_changes(**kwargs):
            raise raised
    elif commit is not None:
        returned = commit

        async def commit_agent_changes(**kwargs):
            return returned
    else:
        commit_agent_changes = AsyncMock(return_value=CommitResult.FAILED)

    auto_commit_service = MagicMock()
    auto_commit_service.commit_agent_changes = commit_agent_changes

    with patch('pathlib.Path.exists', return_value=True), \
         patch('builtins.open', mock_open(read_data=json.dumps(CONTEXT))), \
         patch('services.pipeline_run.PipelineRunManager', return_value=run_manager), \
         patch('services.pipeline_run.get_pipeline_run_manager', return_value=run_manager), \
         patch('config.manager.ConfigManager', return_value=MagicMock()), \
         patch('services.github_integration.GitHubIntegration', return_value=github), \
         patch('services.work_execution_state.work_execution_tracker', tracker), \
         patch('services.auto_commit.auto_commit_service', auto_commit_service), \
         patch('services.project_workspace.workspace_manager', MagicMock()), \
         patch('services.pipeline_progression.PipelineProgression', return_value=progression), \
         patch('task_queue.task_manager.TaskQueue', return_value=MagicMock()), \
         patch('services.agent_container_recovery.subprocess'):

        recovery._process_completed_repair_cycle(
            container_name=CONTAINER,
            container_id='deadbeef',
            project=PROJECT,
            issue_number=ISSUE,
            result=result,
        )

    return run_manager, github, tracker, progression


def _recorded_outcomes(tracker):
    return [
        call.kwargs.get('outcome')
        for call in tracker.record_execution_outcome.call_args_list
    ]


class TestLockContentionResult:
    """Container-level contention: nothing ran and nothing is dirty, so this one
    releases the board lock — and must suppress the cancellation signal, or the
    release is not actually a retry point (#148 C1)."""

    RESULT = {
        'overall_success': False,
        'lock_contention': True,
        'error': "Could not acquire 'project_checkout' lock within 10900.0s",
    }

    def test_mark_failed_is_not_called(self):
        run_manager, _, _, _ = _process(dict(self.RESULT))
        run_manager.mark_failed.assert_not_called()

    def test_run_is_released_without_retaining_the_lock(self):
        run_manager, _, _, _ = _process(dict(self.RESULT))
        run_manager.end_pipeline_run.assert_called_once()
        assert run_manager.end_pipeline_run.call_args.kwargs['retain_lock'] is False

    def test_the_cancellation_signal_is_suppressed_so_the_next_poll_can_retry(self):
        """#148 C1: end_pipeline_run sets a 1-hour cancellation signal for every
        reason but 'feedback_loop_ended', and every automatic recovery path skips
        a cancelled issue — so without this the 'next poll retries' promise is
        false for up to an hour."""
        run_manager, _, _, _ = _process(dict(self.RESULT))
        assert run_manager.end_pipeline_run.call_args.kwargs['suppress_cancellation'] is True

    def test_no_failure_summary_comment_is_posted(self):
        _, github, _, _ = _process(dict(self.RESULT))
        github.post_agent_output.assert_not_called()

    def test_outcome_is_recorded_as_lock_contention(self):
        _, _, tracker, _ = _process(dict(self.RESULT))
        assert _recorded_outcomes(tracker) == ['lock_contention']


class TestOrdinaryFailureResultIsUnchanged:
    """Control: everything above must be specific to the contention flag."""

    RESULT = {'overall_success': False, 'error': 'integration tests still failing'}

    def test_mark_failed_is_still_called(self):
        run_manager, _, _, _ = _process(dict(self.RESULT))
        run_manager.mark_failed.assert_called_once()
        assert run_manager.mark_failed.call_args.kwargs['reason'] == "Repair cycle failed"

    def test_failure_summary_comment_is_still_posted(self):
        _, github, _, _ = _process(dict(self.RESULT))
        github.post_agent_output.assert_called_once()
        assert "Repair Cycle Failed" in github.post_agent_output.call_args.args[1]

    def test_lock_contention_outcome_is_not_recorded(self):
        _, _, tracker, _ = _process(dict(self.RESULT))
        assert 'lock_contention' not in _recorded_outcomes(tracker)


class TestFrozenResultIsUnchanged:
    """Control: the frozen branch this one was modelled on must still win."""

    RESULT = {'overall_success': False, 'frozen': True, 'error': 'token limit'}

    def test_frozen_still_leaves_the_run_active_for_the_watchdog(self):
        run_manager, github, tracker, _ = _process(dict(self.RESULT))
        run_manager.mark_failed.assert_not_called()
        run_manager.end_pipeline_run.assert_not_called()
        github.post_agent_output.assert_not_called()
        assert _recorded_outcomes(tracker) == ['frozen']


class TestCommitLockContention:
    """
    The SECOND, independent contention source on this path — and the one
    contention outcome anywhere in #148 that RETAINS the board lock rather than
    releasing it (#148 C2).

    services/auto_commit.py only takes the project_checkout lock when the
    directory is the SHARED base clone (is_base_clone_dir(); epic worktrees are
    not gated by it at all), so a timeout on it proves the recovered cycle's fix
    is sitting uncommitted in a directory the next dispatched issue will
    `git checkout` into with no stash and no reset. Releasing therefore either
    carries this fix onto another issue's branch or breaks that issue's dispatch.
    Every other contention outcome means "nothing ran, nothing is dirty"; this
    one does not, so it goes through mark_failed() — which, unlike
    end_pipeline_run(retain_lock=True), actually sets retained_reason.
    """

    RESULT = {'overall_success': True}

    @staticmethod
    def _timeout():
        from services.project_checkout_lock import ProjectCheckoutLockTimeoutError
        return ProjectCheckoutLockTimeoutError(
            "Could not acquire 'project_checkout' lock for project 'test-project' within 10900.0s"
        )

    def test_the_board_lock_is_retained_because_the_shared_clone_is_dirty(self):
        run_manager, _, _, _ = _process(dict(self.RESULT), commit=self._timeout())
        run_manager.mark_failed.assert_called_once()
        assert 'uncommitted' in run_manager.mark_failed.call_args.kwargs['reason']

    def test_the_run_is_not_released(self):
        run_manager, _, _, _ = _process(dict(self.RESULT), commit=self._timeout())
        run_manager.end_pipeline_run.assert_not_called()

    def test_outcome_is_recorded_as_lock_contention(self):
        _, _, tracker, _ = _process(dict(self.RESULT), commit=self._timeout())
        assert _recorded_outcomes(tracker) == ['lock_contention']

    def test_the_issue_is_not_auto_advanced_on_an_uncommitted_fix(self):
        """Advancing here hands the next stage — and the PR reviewed downstream —
        a branch with no fix on it."""
        _, _, _, progression = _process(dict(self.RESULT), commit=self._timeout())
        progression.move_issue_to_column.assert_not_called()

    def test_a_wrapped_lock_timeout_is_recognised_too(self):
        cause = self._timeout()
        try:
            raise Exception("Auto-commit failed") from cause
        except Exception as wrapped:
            run_manager, _, tracker, _ = _process(dict(self.RESULT), commit=wrapped)
        run_manager.mark_failed.assert_called_once()
        assert _recorded_outcomes(tracker) == ['lock_contention']


class TestCommitFailureIsUnchanged:
    """
    Control: the contention handling above must be specific to the lock timeout.
    A commit that genuinely FAILED still means the cycle claimed success without
    landing a fix, and that must still be escalated — this is the branch the live
    path was missing entirely until #148 I1.
    """

    RESULT = {'overall_success': True}

    def test_mark_failed_still_fires_for_an_uncommitted_fix(self):
        run_manager, _, _, _ = _process(dict(self.RESULT), commit=CommitResult.FAILED)
        run_manager.mark_failed.assert_called_once()
        assert run_manager.mark_failed.call_args.kwargs['reason'] == (
            "Repair cycle passed but its fix was not committed"
        )

    def test_no_lock_contention_outcome_is_recorded(self):
        _, _, tracker, _ = _process(dict(self.RESULT), commit=CommitResult.FAILED)
        assert 'lock_contention' not in _recorded_outcomes(tracker)


class TestSuccessfulCommitIsUnchanged:
    """Control: a green cycle whose fix did land still ends as a success."""

    RESULT = {'overall_success': True}

    def test_the_run_ends_successfully_and_nothing_is_marked_failed(self):
        run_manager, _, _, _ = _process(dict(self.RESULT), commit=CommitResult.COMMITTED)
        run_manager.mark_failed.assert_not_called()
        run_manager.end_pipeline_run.assert_called_once()
        assert run_manager.end_pipeline_run.call_args.kwargs['reason'] == (
            "Repair cycle completed successfully"
        )

    def test_nothing_to_commit_is_still_treated_as_a_landed_run(self):
        """#148 I1 preserved CommitResult's truthiness precisely so this
        pre-existing behavior is unchanged: an empty diff is not a failure."""
        run_manager, _, _, _ = _process(
            dict(self.RESULT), commit=CommitResult.NOTHING_TO_COMMIT
        )
        run_manager.mark_failed.assert_not_called()
        run_manager.end_pipeline_run.assert_called_once()
