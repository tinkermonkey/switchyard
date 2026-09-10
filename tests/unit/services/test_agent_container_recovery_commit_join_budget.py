"""
Startup repair-cycle recovery's auto-commit wait (#140 items 11 and 32,
#154/WI-9 review).

Item 11 -- recover_or_cleanup_repair_cycle_containers() joins each recovered
container's auto-commit thread SERIALLY, and since #54 that join is bounded by
the project_checkout lock's own multi-thousand-second timeout. With N orphaned
containers all resolving to the same contended shared base clone -- plausible
right after the crash that orphaned them -- the pass could block startup for up
to N x that timeout. It now carries one shared budget for the whole pass, with
a small per-container floor (_COMMIT_JOIN_FLOOR_SECONDS) under it.

#154/WI-9 review -- a join cut short by that budget left commit_success[0] at
its initial False, which the end-of-run chain could not tell apart from a real
CommitResult.FAILED. Every such container therefore reached
mark_failed("Repair cycle passed but its fix was not committed"), which durably
retains the board's pipeline lock: no issue dispatched onto that board until an
operator runs scripts/release_lock.py, over a commit that was still running and
usually landed seconds later. commit_in_flight is now its own outcome, and what
it does depends on the directory -- release an isolated epic worktree (shares
nothing), retain the shared base clone (holds the uncommitted fix, #148 C2).

Item 32 -- do_commit() runs on a background thread, and an exception raised
there (rather than inside commit_agent_changes()'s own try/except) would go to
threading.excepthook/stderr rather than `logger`, while the outer code's
"the specific cause was logged above" fallback said otherwise. VERIFIED ALREADY
FIXED by WI-3 (#148, commit 5ba58ee), which wrapped do_commit() in
commit_thread()'s own try/except. Pinned here so it cannot silently regress:
that file's existing tests cover the lock-timeout arm of that handler, not the
generic one.
"""

import json
import os
import time
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

import threading
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import services.agent_container_recovery as recovery_module
from services.agent_container_recovery import AgentContainerRecovery
from services.auto_commit import CommitResult

PROJECT = 'test-project'
ISSUE = 5151
RUN_ID = 'run-budget'
CONTAINER = f'repair-cycle-{PROJECT}-{ISSUE}'

CONTEXT = {
    'board': 'Development',
    'repository': 'test-repo',
    'column': 'Testing',
    'pipeline_run_id': RUN_ID,
    'project_dir': f'/workspace/{PROJECT}',
    'agent_name': 'senior_software_engineer',
}

SUCCESS_RESULT = {'overall_success': True}

# Small enough that a test can outrun it, large enough to be a real wait.
TEST_JOIN_FLOOR = 0.2


def _recovery():
    recovery = object.__new__(AgentContainerRecovery)
    recovery.redis = None
    return recovery


def _process(commit_agent_changes, commit_join_deadline=None,
             is_base_clone=True, join_floor=TEST_JOIN_FLOOR):
    """
    Drive _process_completed_repair_cycle() on a successful repair cycle, with
    `commit_agent_changes` as the auto-commit coroutine function.

    `is_base_clone` is what workspace_manager.is_base_clone_dir() answers for
    the context's project_dir -- the shared-clone / epic-worktree fork the
    commit_in_flight branch takes its decision on.

    Returns (run_manager, progression).
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

    auto_commit_service = MagicMock()
    auto_commit_service.commit_agent_changes = commit_agent_changes

    workspace_manager = MagicMock()
    workspace_manager.is_base_clone_dir.return_value = is_base_clone

    with patch.object(recovery_module, '_COMMIT_JOIN_FLOOR_SECONDS', join_floor), \
         patch('pathlib.Path.exists', return_value=True), \
         patch('builtins.open', mock_open(read_data=json.dumps(CONTEXT))), \
         patch('services.pipeline_run.PipelineRunManager', return_value=run_manager), \
         patch('services.pipeline_run.get_pipeline_run_manager', return_value=run_manager), \
         patch('config.manager.ConfigManager', return_value=MagicMock()), \
         patch('services.github_integration.GitHubIntegration', return_value=github), \
         patch('services.work_execution_state.work_execution_tracker', tracker), \
         patch('services.auto_commit.auto_commit_service', auto_commit_service), \
         patch('services.project_workspace.workspace_manager', workspace_manager), \
         patch('services.pipeline_progression.PipelineProgression', return_value=progression), \
         patch('task_queue.task_manager.TaskQueue', return_value=MagicMock()), \
         patch('services.agent_container_recovery.subprocess'):

        recovery._process_completed_repair_cycle(
            container_name=CONTAINER,
            container_id='deadbeef',
            project=PROJECT,
            issue_number=ISSUE,
            result=dict(SUCCESS_RESULT),
            commit_join_deadline=commit_join_deadline,
        )

    return run_manager, progression


def _blocking_commit():
    """A commit that outlives any join this suite performs, plus the event that
    releases it so the thread cannot outlive the test."""
    release = threading.Event()

    async def commit_agent_changes(**kwargs):
        release.wait(timeout=20)
        return CommitResult.COMMITTED

    return commit_agent_changes, release


class TestSharedJoinBudget:
    """#140 item 11."""

    def test_an_exhausted_budget_does_not_wait_on_the_commit_thread(self):
        """
        THE regression. A commit that would block for a very long time, with the
        pass's shared budget already spent: the call must return promptly rather
        than adding another full lock timeout to a startup that has already spent
        one. Pre-fix there was no budget at all and this join carried
        DEFAULT_TIMEOUT_SECONDS + 60 of its own, unconditionally.
        """
        commit_agent_changes, release = _blocking_commit()

        started = time.monotonic()
        try:
            _process(commit_agent_changes, commit_join_deadline=time.monotonic() - 1)
        finally:
            release.set()
        elapsed = time.monotonic() - started

        assert elapsed < 5, (
            f"recovery waited {elapsed:.1f}s on a commit whose shared budget was "
            "already exhausted"
        )

    def test_an_exhausted_budget_still_waits_the_per_container_floor(self):
        """
        The budget is clamped to _COMMIT_JOIN_FLOOR_SECONDS, not to zero
        (#154/WI-9 review). An epic-worktree commit takes no project_checkout
        lock and finishes in seconds, so a zero-length join threw away an
        outcome this pass was about to get for free and pushed every container
        after the first onto the in-flight path.
        """
        commit_agent_changes, release = _blocking_commit()

        started = time.monotonic()
        try:
            _process(
                commit_agent_changes,
                commit_join_deadline=time.monotonic() - 1,
                join_floor=1.0,
            )
        finally:
            release.set()
        elapsed = time.monotonic() - started

        assert elapsed >= 1.0, (
            f"an exhausted budget must still wait the floor, waited {elapsed:.2f}s"
        )

    def test_a_fast_commit_is_still_observed_on_an_exhausted_budget(self):
        """The floor's whole point: the common case (isolated epic worktree,
        commit in milliseconds) is observed and auto-advances, even when an
        earlier container in the same pass spent the entire shared budget."""
        async def commit_agent_changes(**kwargs):
            return CommitResult.COMMITTED

        run_manager, _ = _process(
            commit_agent_changes,
            commit_join_deadline=time.monotonic() - 1,
            is_base_clone=False,
        )

        run_manager.mark_failed.assert_not_called()
        run_manager.end_pipeline_run.assert_called_once()
        assert run_manager.end_pipeline_run.call_args.kwargs['reason'] == (
            "Repair cycle completed successfully"
        )

    def test_a_live_budget_still_waits_for_a_fast_commit(self):
        """Control: the budget bounds the wait, it does not remove it. A commit
        that finishes normally is still observed, and the issue auto-advances."""
        async def commit_agent_changes(**kwargs):
            return CommitResult.COMMITTED

        run_manager, _ = _process(
            commit_agent_changes, commit_join_deadline=time.monotonic() + 300
        )

        run_manager.mark_failed.assert_not_called()

    def test_no_deadline_keeps_the_full_per_call_join(self):
        """The default (used by tests and any one-off caller) is unchanged."""
        async def commit_agent_changes(**kwargs):
            return CommitResult.COMMITTED

        run_manager, _ = _process(commit_agent_changes, commit_join_deadline=None)

        run_manager.mark_failed.assert_not_called()

    def test_the_recovery_pass_builds_one_budget_for_all_containers(self):
        """
        The budget has to be minted ONCE per pass, before the loop -- minting it
        per container would restore exactly the N x timeout behaviour it exists
        to remove. Asserted across BOTH forwarding call sites: the
        running-containers loop (the primary path -- containers still alive at
        restart) and the orphaned-results loop. Stubbing the running loop out
        (as this test originally did) left the site that matters most unpinned:
        dropping its commit_join_deadline= kwarg would have kept every test
        green while restoring N x ~3h of serial startup blocking.
        """
        recovery = _recovery()
        recovery.get_running_repair_cycle_containers = MagicMock(return_value=[
            {'name': f'repair-cycle-{PROJECT}-{ISSUE + 10}-abcdefgh',
             'id': 'c1', 'created_at': '2026-01-01 00:00:00'},
            {'name': f'repair-cycle-{PROJECT}-{ISSUE + 11}-abcdefgh',
             'id': 'c2', 'created_at': '2026-01-01 00:00:00'},
        ])
        recovery.check_repair_cycle_result = MagicMock(return_value=dict(SUCCESS_RESULT))
        recovery.find_orphaned_repair_cycle_results = MagicMock(return_value=[
            {'project': PROJECT, 'issue_number': ISSUE, 'run_id': RUN_ID,
             'result': dict(SUCCESS_RESULT)},
            {'project': PROJECT, 'issue_number': ISSUE + 1, 'run_id': RUN_ID,
             'result': dict(SUCCESS_RESULT)},
        ])
        recovery._process_completed_repair_cycle = MagicMock()

        run_manager = MagicMock()
        active_run = MagicMock()
        active_run.id = RUN_ID
        run_manager.get_active_pipeline_run.return_value = active_run

        with patch('config.manager.config_manager') as mock_config, \
             patch('services.pipeline_run.PipelineRunManager', return_value=run_manager):
            mock_config.get_agent.return_value = MagicMock(timeout=10800)
            recovery.recover_or_cleanup_repair_cycle_containers()

        calls = recovery._process_completed_repair_cycle.call_args_list
        assert len(calls) == 4, (
            f"expected 2 running + 2 orphaned containers processed, got {len(calls)}"
        )
        deadlines = {call.kwargs.get('commit_join_deadline') for call in calls}
        assert None not in deadlines, (
            "every call site must forward the pass's shared budget, including the "
            "running-containers loop"
        )
        assert len(deadlines) == 1, (
            f"each container got its own wait budget: {deadlines}"
        )


class TestACutShortJoinIsNotAFailedCommit:
    """
    #154/WI-9 review. commit_success[0]'s initial False is indistinguishable
    from CommitResult.FAILED without checking thread.is_alive(), so a join the
    shared budget cut short used to land on
    mark_failed("Repair cycle passed but its fix was not committed") -- durably
    retaining the board's pipeline lock over a commit that was still running.
    """

    def test_an_unwaited_commit_does_not_auto_advance_the_issue(self):
        """Not waiting must not be mistaken for "the fix landed": the commit's
        outcome is simply unknown to this pass."""
        commit_agent_changes, release = _blocking_commit()

        try:
            _, progression = _process(
                commit_agent_changes, commit_join_deadline=time.monotonic() - 1
            )
        finally:
            release.set()

        # move_issue_to_column is the method PipelineProgression actually has
        # (services/pipeline_progression.py:98) and the one
        # agent_container_recovery.py calls. The earlier assertion here named a
        # method that does not exist, so MagicMock auto-created it and
        # assert_not_called() passed unconditionally -- the test could not fail.
        progression.move_issue_to_column.assert_not_called()

    def test_an_unwaited_worktree_commit_releases_rather_than_marking_failed(self):
        """
        THE regression. An isolated epic worktree shares its directory with
        nothing, so a commit still running in it leaves nothing dirty for the
        next issue: the run is released (retain_lock=False) and the next board
        poll retries. It must NOT be marked failed -- mark_failed() durably sets
        retained_reason on the board's pipeline lock, blocking every issue on
        that board until an operator runs scripts/release_lock.py, and its
        reason ("its fix was not committed") is false while the commit is in
        flight.
        """
        commit_agent_changes, release = _blocking_commit()

        try:
            run_manager, _ = _process(
                commit_agent_changes,
                commit_join_deadline=time.monotonic() - 1,
                is_base_clone=False,
            )
        finally:
            release.set()

        run_manager.mark_failed.assert_not_called()
        run_manager.end_pipeline_run.assert_called_once()
        kwargs = run_manager.end_pipeline_run.call_args.kwargs
        assert kwargs['retain_lock'] is False
        assert kwargs['suppress_cancellation'] is True
        assert 'in flight' in kwargs['reason']

    def test_an_unwaited_shared_clone_commit_retains_with_an_accurate_reason(self):
        """
        The shared base clone genuinely does hold an uncommitted fix until the
        thread lands it, and handing that dirty clone to the next issue is the
        #148 C2 hazard -- so this one still retains. What must not survive is
        the reason: an operator reading "its fix was not committed" for a commit
        that is running (and about to succeed) is being told something false.
        """
        commit_agent_changes, release = _blocking_commit()

        try:
            run_manager, _ = _process(
                commit_agent_changes,
                commit_join_deadline=time.monotonic() - 1,
                is_base_clone=True,
            )
        finally:
            release.set()

        run_manager.mark_failed.assert_called_once()
        reason = run_manager.mark_failed.call_args.kwargs['reason']
        assert 'still in flight' in reason, reason
        assert reason != "Repair cycle passed but its fix was not committed"
        run_manager.end_pipeline_run.assert_not_called()

    def test_a_genuinely_failed_commit_still_marks_the_run_failed(self):
        """The branch commit_in_flight sits in front of must keep working: a
        commit that RETURNED FAILED (thread finished, no fix on disk) is still
        the case mark_failed() exists for."""
        async def commit_agent_changes(**kwargs):
            return CommitResult.FAILED

        run_manager, _ = _process(
            commit_agent_changes, commit_join_deadline=time.monotonic() + 300
        )

        run_manager.mark_failed.assert_called_once()
        assert run_manager.mark_failed.call_args.kwargs['reason'] == (
            "Repair cycle passed but its fix was not committed"
        )


class TestCommitThreadExceptionsReachTheLogger:
    """
    #140 item 32 -- already closed by WI-3 (#148). Pinned, not re-fixed.

    An exception out of do_commit() (as opposed to one commit_agent_changes()
    handles itself) runs on a background thread, where an unhandled raise goes
    to threading.excepthook/stderr instead of `logger` -- and the outer code
    would then report "the specific cause was logged above", which nothing had.
    """

    def test_a_generic_commit_exception_is_logged_by_the_recovery_logger(self, caplog):
        import logging

        async def commit_agent_changes(**kwargs):
            raise RuntimeError("git index.lock is held")

        with caplog.at_level(logging.ERROR, logger='services.agent_container_recovery'):
            _process(commit_agent_changes)

        messages = [r.message for r in caplog.records]
        assert any('git index.lock is held' in m for m in messages), (
            "the commit thread's own exception must reach the logger, not "
            f"threading.excepthook: {messages}"
        )

    def test_a_generic_commit_exception_does_not_auto_advance(self):
        async def commit_agent_changes(**kwargs):
            raise RuntimeError("git index.lock is held")

        _, progression = _process(commit_agent_changes)

        progression.move_issue_to_column.assert_not_called()
