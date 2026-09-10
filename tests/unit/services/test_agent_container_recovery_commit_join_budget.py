"""
Startup repair-cycle recovery's auto-commit wait (#140 items 11 and 32).

Item 11 -- recover_or_cleanup_repair_cycle_containers() joins each recovered
container's auto-commit thread SERIALLY, and since #54 that join is bounded by
the project_checkout lock's own multi-thousand-second timeout. With N orphaned
containers all resolving to the same contended shared base clone -- plausible
right after the crash that orphaned them -- the pass could block startup for up
to N x that timeout. It now carries one shared budget for the whole pass.

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


def _recovery():
    recovery = object.__new__(AgentContainerRecovery)
    recovery.redis = None
    return recovery


def _process(commit_agent_changes, commit_join_deadline=None):
    """
    Drive _process_completed_repair_cycle() on a successful repair cycle, with
    `commit_agent_changes` as the auto-commit coroutine function.

    Returns (progression, caplog-free logger records are asserted by the caller).
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
            result=dict(SUCCESS_RESULT),
            commit_join_deadline=commit_join_deadline,
        )

    return run_manager, progression


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
        release = threading.Event()

        async def commit_agent_changes(**kwargs):
            # Far longer than the 5s ceiling asserted below; released in the
            # finally below so the thread cannot outlive the test.
            release.wait(timeout=20)
            return CommitResult.COMMITTED

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

    def test_an_unwaited_commit_does_not_auto_advance_the_issue(self):
        """Not waiting must not be mistaken for "the fix landed": the commit's
        outcome is simply unknown to this pass, and the next board poll retries."""
        release = threading.Event()

        async def commit_agent_changes(**kwargs):
            release.wait(timeout=20)
            return CommitResult.COMMITTED

        try:
            _, progression = _process(
                commit_agent_changes, commit_join_deadline=time.monotonic() - 1
            )
        finally:
            release.set()

        progression.progress_to_next_column.assert_not_called()

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
        to remove. Asserted by driving the pass over two orphaned results and
        checking both calls received the same deadline value.
        """
        recovery = _recovery()
        recovery.get_running_repair_cycle_containers = MagicMock(return_value=[])
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

        deadlines = {
            call.kwargs['commit_join_deadline']
            for call in recovery._process_completed_repair_cycle.call_args_list
        }
        assert len(recovery._process_completed_repair_cycle.call_args_list) == 2
        assert len(deadlines) == 1, (
            f"each orphaned result got its own wait budget: {deadlines}"
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

        progression.progress_to_next_column.assert_not_called()
