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
usually landed seconds later. commit_in_flight is now its own outcome, with an
accurate reason.

#154/WI-9 second review -- releasing the run for a per-epic worktree ("shares
nothing") gave away a guarantee the pre-budget code accidentally provided. It
shares nothing with SIBLING epics, which is why project_checkout does not gate
it; it is not isolated from its OWN re-dispatch, which is precisely what a
release invites (should_execute_work() retries this outcome, resolve_workspace()
hands back the same directory) while `git add -A`/`commit`/`push` are still
running in it. Both directories now retain, a bounded grace join for the
worktree case (nothing gates it, so one more short wait usually observes it)
keeps that retention rare, and the directory is marked in use for the commit
thread's whole life so main.py's prune sweep cannot remove it mid-commit.

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
             is_base_clone=True, join_floor=TEST_JOIN_FLOOR,
             thread_class=None, workspace_manager=None):
    """
    Drive _process_completed_repair_cycle() on a successful repair cycle, with
    `commit_agent_changes` as the auto-commit coroutine function.

    `is_base_clone` is what workspace_manager.is_base_clone_dir() answers for
    the context's project_dir -- the shared-clone / per-epic-worktree fork the
    in-flight path takes its grace-join decision on.

    `thread_class` stands in for threading.Thread (imported inside the method
    under test, so it can only be patched at its source), letting a test watch
    the joins -- see _commit_join_spy. `workspace_manager` lets a caller keep
    the mock and assert on it after the call.

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

    if workspace_manager is None:
        workspace_manager = MagicMock()
    workspace_manager.is_base_clone_dir.return_value = is_base_clone

    with patch.object(recovery_module, '_COMMIT_JOIN_FLOOR_SECONDS', join_floor), \
         patch('threading.Thread', thread_class or threading.Thread), \
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


def _commit_join_spy(release_after_first_join=None):
    """
    A stand-in for the `threading` module agent_container_recovery builds its
    threads from, recording every join() timeout the AUTO-COMMIT wait passes.

    Filtered to the commit thread by its target's name: the same method also
    joins a GitHub-comment thread earlier on, and mixing the two would make the
    recorded sequence meaningless. If commit_thread() is ever renamed this spy
    records nothing and its assertions fail loudly, which is the intended
    failure mode.

    `release_after_first_join` is set once the FIRST commit join returns, which
    is how a test lands a commit inside the grace join deterministically rather
    than by racing a sleep against it.

    Returns (thread_class, joins).
    """
    joins = []

    class SpyThread(threading.Thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            target = kwargs.get('target')
            self._is_commit_thread = getattr(target, '__name__', '') == 'commit_thread'

        def join(self, timeout=None):
            if not self._is_commit_thread:
                return super().join(timeout)
            first = not joins
            joins.append(timeout)
            result = super().join(timeout)
            if first and release_after_first_join is not None:
                release_after_first_join.set()
            return result

    return SpyThread, joins


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
             patch('services.pipeline_run.PipelineRunManager', return_value=run_manager), \
             patch('elasticsearch.Elasticsearch') as mock_es_class:
            # This test drives the REAL recover_or_cleanup_repair_cycle_containers(),
            # whose tail block builds its own `Elasticsearch(['http://elasticsearch:9200'])`
            # -- inside the orchestrator container, the live cluster -- and then
            # PUTs repair-cycle-recovery-ilm-policy and
            # repair-cycle-recovery-template and indexes a metrics document built
            # from THIS TEST's fabricated container counts into
            # repair-cycle-recovery-<today>. Measured: one `pytest tests/unit`
            # performed exactly those three writes, from exactly this test.
            #
            # The client is imported inside the function, so the patch target is
            # the source module rather than services.agent_container_recovery.
            mock_config.get_agent.return_value = MagicMock(timeout=10800)
            recovery.recover_or_cleanup_repair_cycle_containers()

        # The metrics block ran against a mock instead of being skipped, so the
        # patch is holding the writes rather than an exception swallowing them.
        assert mock_es_class.called

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

    def test_an_unwaited_worktree_commit_retains_rather_than_releasing(self):
        """
        THE second-review regression (#154/WI-9). A commit still running in a
        per-epic worktree is a live git writer in the directory this issue's own
        next dispatch resolves straight back into: should_execute_work() treats
        'commit_in_flight' as a retry point, and resolve_workspace() keys the
        worktree by epic_id. Nothing serializes the two --
        project_checkout_lock_if_shared_async() deliberately does not gate a
        worktree -- so releasing the run hands the directory to a container that
        starts editing files while `git add -A`/`commit`/`push` are mid-flight
        in it: .git/index.lock contention, or the new run's half-written files
        staged and pushed under this issue's commit message.

        "Shares nothing" is true of SIBLING epics, not of the re-dispatch the
        release itself triggers, so this retains exactly as the shared base
        clone does -- with a reason that names the worktree.
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

        run_manager.end_pipeline_run.assert_not_called()
        run_manager.mark_failed.assert_called_once()
        reason = run_manager.mark_failed.call_args.kwargs['reason']
        assert 'still in flight' in reason, reason
        assert 'worktree' in reason, reason

    def test_an_unwaited_shared_clone_commit_retains_with_an_accurate_reason(self):
        """
        The shared base clone genuinely does hold an uncommitted fix until the
        thread lands it, and handing that dirty clone to the next issue is the
        #148 C2 hazard -- so this one retains too. What must not survive is the
        reason: an operator reading "its fix was not committed" for a commit
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


class TestTheWorktreeGraceJoin:
    """
    #154/WI-9 second review. Retaining the board's lock on a still-running
    commit is correct but expensive (an operator has to run
    scripts/release_lock.py), so the case where waiting is cheap gets one more
    bounded wait first: a commit in a per-epic worktree takes no
    project_checkout lock at all, so it is not queued behind anything -- it is a
    slow `git push`, and a second short join usually observes it. The shared base
    clone, whose commit may be waiting out the whole lock timeout, gets none:
    that is the N x cost the shared budget exists to remove.
    """

    def test_a_worktree_commit_gets_a_second_bounded_wait(self):
        commit_agent_changes, release = _blocking_commit()
        thread_class, joins = _commit_join_spy()

        try:
            _process(
                commit_agent_changes,
                commit_join_deadline=time.monotonic() - 1,
                is_base_clone=False,
                join_floor=TEST_JOIN_FLOOR,
                thread_class=thread_class,
            )
        finally:
            release.set()

        assert joins == [TEST_JOIN_FLOOR, TEST_JOIN_FLOOR], (
            f"expected a floor-sized join followed by a floor-sized grace join, got {joins}"
        )

    def test_a_shared_clone_commit_gets_no_second_wait(self):
        """The grace join must not become a blanket doubling of the pass's worst
        case: a shared-clone commit can legitimately be waiting out the whole
        project_checkout timeout, and waiting on it N times is the exact cost
        #140 item 11 removed."""
        commit_agent_changes, release = _blocking_commit()
        thread_class, joins = _commit_join_spy()

        try:
            _process(
                commit_agent_changes,
                commit_join_deadline=time.monotonic() - 1,
                is_base_clone=True,
                thread_class=thread_class,
            )
        finally:
            release.set()

        assert joins == [TEST_JOIN_FLOOR], (
            f"the shared base clone must get exactly one join, got {joins}"
        )

    def test_a_worktree_commit_that_lands_in_the_grace_join_is_observed(self):
        """
        The point of the grace join: a commit that finishes during it is a
        NORMAL success -- the run ends, no lock is retained, no operator is
        involved. Deterministic rather than timing-based: the commit is released
        by the spy the instant the first join returns, so it can only land
        inside the second one.
        """
        commit_agent_changes, release = _blocking_commit()
        thread_class, joins = _commit_join_spy(release_after_first_join=release)

        run_manager, _ = _process(
            commit_agent_changes,
            commit_join_deadline=time.monotonic() - 1,
            is_base_clone=False,
            thread_class=thread_class,
        )

        assert len(joins) == 2, f"the commit must have survived the first join: {joins}"
        run_manager.mark_failed.assert_not_called()
        run_manager.end_pipeline_run.assert_called_once()
        assert run_manager.end_pipeline_run.call_args.kwargs['reason'] == (
            "Repair cycle completed successfully"
        )


class TestTheCommitDirectoryIsHeldAgainstThePruneSweep:
    """
    #154/WI-9 second review, the secondary window. main.py runs
    workspace_manager.prune_epic_worktrees() the moment
    recover_or_cleanup_repair_cycle_containers() returns. Before the shared join
    budget that pass always waited the commit out, so no commit thread was ever
    live when the sweep started; now one can be, and the sweep force-removes an
    untracked worktree -- mid-`git commit`, losing the repair cycle's fix. The
    pre-existing get_or_create_epic_worktree() re-registration covers this only
    when context.json still carries epic_id AND branch_name (this suite's context
    carries neither) and only when it does not raise.
    """

    def test_an_in_flight_commit_holds_its_directory_for_the_whole_pass(self):
        commit_agent_changes, release = _blocking_commit()
        workspace_manager = MagicMock()

        try:
            _process(
                commit_agent_changes,
                commit_join_deadline=time.monotonic() - 1,
                is_base_clone=False,
                workspace_manager=workspace_manager,
            )
            # Still blocked, i.e. exactly the state main.py's prune sweep would
            # find the directory in.
            workspace_manager.mark_worktree_path_in_use.assert_called_once_with(
                CONTEXT['project_dir']
            )
            workspace_manager.clear_worktree_path_in_use.assert_not_called()
        finally:
            release.set()

    def test_the_hold_is_released_once_the_commit_thread_finishes(self):
        """Held for the WRITER's life, not forever -- a permanent hold would make
        every subsequent startup's prune sweep a no-op for that directory."""
        async def commit_agent_changes(**kwargs):
            return CommitResult.COMMITTED

        workspace_manager = MagicMock()
        _process(
            commit_agent_changes,
            is_base_clone=False,
            workspace_manager=workspace_manager,
        )

        workspace_manager.mark_worktree_path_in_use.assert_called_once_with(
            CONTEXT['project_dir']
        )
        workspace_manager.clear_worktree_path_in_use.assert_called_once_with(
            CONTEXT['project_dir']
        )

    def test_the_hold_is_released_even_when_the_commit_raises(self):
        async def commit_agent_changes(**kwargs):
            raise RuntimeError("git index.lock is held")

        workspace_manager = MagicMock()
        _process(commit_agent_changes, workspace_manager=workspace_manager)

        workspace_manager.clear_worktree_path_in_use.assert_called_once_with(
            CONTEXT['project_dir']
        )
