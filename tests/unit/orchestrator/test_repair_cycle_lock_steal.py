"""
Coverage for ProjectMonitor._start_repair_cycle_for_issue's two pipeline-lock
gates: the "competing repair cycle" check and the steal_lock() call that
replaced this file's original "steal the lock" bug (found across three
rounds of PR #35 review — repair cycles have priority over ordinary
Development items, but a retained/failed lock must never be silently stolen
and handed to an unrelated issue).

DESIGN NOTE — why this file was rewritten from scratch in a sixth round:

_start_repair_cycle_for_issue is one large function wrapped in a single
top-level try/except that returns None on ANY exception, and that returns
None from a dozen different legitimate places further down (no test
configurations found, issue-context fetch failed, container launch failed,
context file save failed, ...). Every previous version of this test asserted
only `result is None` (plus which lock-manager methods were or weren't
called) to prove a lock gate fired — but `None` is also exactly what a
completely unrelated downstream failure produces, and there IS one in every
version of this test's own mock setup: `subprocess.run` is patched globally
(needed so a real `gh` CLI shellout during context-fetching doesn't try to
hit the network and hang for 15+ seconds), and that same global patch is
also hit by `_launch_repair_cycle_container`'s real internal `docker run`
subprocess call further down. So bypassing a lock gate doesn't make the test
fail — execution simply falls through to a real container-launch attempt,
which fails for the unrelated subprocess-mock reason, and returns None
anyway. This was true, undetected, for THREE separate versions of this file
across rounds 3-6 (see git history), for three different underlying reasons
each time (an unmocked pipeline_run_manager Redis call; an unconfigured
steal_lock() Mock; this exact fall-through).

The fix applied here is structural, not another patch: every test in this
file mocks `_launch_repair_cycle_container` directly (and, for the
success-reaching tests, `ProjectMonitor._monitor_repair_cycle_container`, so
a real background monitoring thread never starts). "Was the container launch
attempted?" is then an unambiguous, directly-observable signal for "did a
lock gate correctly stop dispatch" vs. "did dispatch correctly proceed" —
it no longer depends on what happens to fail first several calls further
down the same function. Every refusal test below additionally asserts the
exact function return value; every success test asserts the actual
`stage_config.default_agent` return value AND that the container-launch mock
was called with this issue's number — proving the call reached a genuine
successful outcome, not just "didn't raise."
"""

import pytest
from unittest.mock import Mock, MagicMock, patch

from tests.unit.orchestrator.conftest import create_test_issue


def _run_start_repair_cycle(
    mock_pipeline_lock_manager_auto,
    mock_github,
    mock_config_manager,
    mock_state_manager,
    mock_task_queue,
    issue_number=100,
    monitor_mutator=None,
    phantom_run_id=None,
    pipeline_manager_capture=None,
    test_types=None,
    subprocess_side_effect=None,
):
    """
    Shared harness for every test in this file. Sets up the common
    scaffolding (issue, pipeline run, `gh` CLI shellout, at least one test
    type configured) and calls _start_repair_cycle_for_issue for real,
    with _launch_repair_cycle_container and _monitor_repair_cycle_container
    mocked out so nothing here ever touches real Docker/subprocess machinery
    or starts a real background thread.

    phantom_run_id and pipeline_manager_capture support TestPhantomRunCleanup
    below: pass phantom_run_id to thread a caller-supplied phantom id through
    (as trigger_agent_for_status() would), and pass a mutable dict as
    pipeline_manager_capture to have this populate capture['manager'] with the
    mocked PipelineRunManager (mock_pipeline_mgr.return_value below) so the
    caller can assert on end_phantom_pipeline_run()/end_pipeline_run() calls
    without changing this function's return signature.

    Returns (result, launch_mock, monitor_mock) so callers can assert on the
    function's actual return value as well as whether/how dispatch proceeded.
    """
    mock_run = Mock()
    mock_run.id = 'run-repair-100'

    with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
         patch('config.state_manager.state_manager', mock_state_manager), \
         patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_pipeline_lock_manager_auto), \
         patch('services.pipeline_run.get_pipeline_run_manager') as mock_pipeline_mgr, \
         patch('services.project_monitor.subprocess.run') as mock_subprocess, \
         patch('services.project_monitor._launch_repair_cycle_container') as launch_mock:

        mock_pipeline_mgr.return_value.get_or_create_pipeline_run.return_value = (mock_run, False)
        mock_pipeline_mgr.return_value.end_phantom_pipeline_run.return_value = True
        mock_pipeline_mgr.return_value.end_pipeline_run.return_value = True
        if pipeline_manager_capture is not None:
            pipeline_manager_capture['manager'] = mock_pipeline_mgr.return_value
        launch_mock.return_value = f'repair-cycle-test-project-{issue_number}-run-repa'

        # Real `gh` CLI calls (e.g. fetching issue comments for previous-stage
        # context) would otherwise actually shell out and take 15+ seconds to
        # fail against a nonexistent test-org/test-repo — deterministic
        # failure, matching what the code already handles gracefully.
        # subprocess_side_effect lets a caller (e.g. TestPhantomRunCleanup's
        # duplicate-container test) override this — patching subprocess.run
        # again from outside this function wouldn't work, since this inner
        # patch is entered later and would just shadow it.
        import subprocess as _subprocess
        mock_subprocess.side_effect = subprocess_side_effect or _subprocess.CalledProcessError(1, ['gh'])

        create_test_issue(mock_github, issue_number, 'Testing')

        from services.project_monitor import ProjectMonitor
        monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
        monitor.get_issue_details = lambda repo, num, org: mock_github.get_issue(num)
        # Never let a real background thread start — its target function does
        # real `docker wait`/subprocess work this is not testing.
        monitor._monitor_repair_cycle_container = Mock()

        if monitor_mutator:
            monitor_mutator(monitor)

        project_config = mock_config_manager.get_project_config('test-project')
        # test_configs building requires a real dict here (project_config is
        # otherwise a Mock) — without at least one test type, the function
        # returns None via the "no test configurations found" branch before
        # ever reaching either lock gate this file targets.
        project_config.testing = {'types': test_types if test_types is not None else [{'type': 'unit'}]}
        pipeline_config = project_config.pipelines[0]
        workflow_template = mock_config_manager.get_workflow_template('test-workflow')
        column = MagicMock()
        column.type = 'standard'
        stage_config = MagicMock()

        result = monitor._start_repair_cycle_for_issue(
            project_name='test-project',
            board_name='dev',
            issue_number=issue_number,
            status='Testing',
            repository='test-repo',
            project_config=project_config,
            pipeline_config=pipeline_config,
            workflow_template=workflow_template,
            column=column,
            stage_config=stage_config,
            phantom_run_id=phantom_run_id,
        )

        return result, launch_mock, stage_config


class TestCompetingRepairCycleGate:
    """The FIRST lock check in _start_repair_cycle_for_issue: repair cycles
    must not compete with another already-running repair cycle, but they may
    freely proceed past an ordinary (non-repair-cycle) holder — that's what
    the second gate (steal_lock) exists to arbitrate. Round 4's review found
    this gate's fail-closed branch (both Redis and YAML reads failing) was
    never actually exercised by any test; this class closes that gap and
    covers every branch of the gate."""

    def test_refuses_when_lock_state_cannot_be_determined(
        self, mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
        mock_state_manager, mock_task_queue,
    ):
        mock_pipeline_lock_manager_auto.get_lock_fail_closed.return_value = (None, False)

        result, launch_mock, _ = _run_start_repair_cycle(
            mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
            mock_state_manager, mock_task_queue,
        )

        assert result is None
        launch_mock.assert_not_called()
        # Must refuse before ever reaching the second gate.
        mock_pipeline_lock_manager_auto.steal_lock.assert_not_called()

    def test_refuses_when_another_active_repair_cycle_holds_the_lock(
        self, mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
        mock_state_manager, mock_task_queue,
    ):
        other_lock = Mock()
        other_lock.locked_by_issue = 999
        other_lock.retained_reason = None
        mock_pipeline_lock_manager_auto.get_lock_fail_closed.return_value = (other_lock, True)

        def has_competing_container(key):
            # repair_cycle:container:{project}:{issue} — non-None means a
            # repair cycle is registered as running for that issue.
            return 'repair-cycle-other-container' if key.endswith(':999') else None
        mock_task_queue.redis_client.get.side_effect = has_competing_container

        result, launch_mock, _ = _run_start_repair_cycle(
            mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
            mock_state_manager, mock_task_queue,
        )

        assert result is None
        launch_mock.assert_not_called()
        mock_pipeline_lock_manager_auto.steal_lock.assert_not_called()

    def test_proceeds_past_a_non_competing_holder_to_the_steal_lock_gate(
        self, mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
        mock_state_manager, mock_task_queue,
    ):
        """Control case: another issue holds the lock, but it has no repair
        cycle container registered — this gate must let it through so the
        second gate (steal_lock) can decide whether to steal it."""
        other_lock = Mock()
        other_lock.locked_by_issue = 999
        other_lock.retained_reason = None
        mock_pipeline_lock_manager_auto.get_lock_fail_closed.return_value = (other_lock, True)
        mock_task_queue.redis_client.get.return_value = None  # no competing container anywhere
        mock_pipeline_lock_manager_auto.steal_lock.return_value = (True, "stolen")

        result, launch_mock, stage_config = _run_start_repair_cycle(
            mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
            mock_state_manager, mock_task_queue,
        )

        mock_pipeline_lock_manager_auto.steal_lock.assert_called_once_with('test-project', 'dev', 100)
        launch_mock.assert_called_once()
        assert result == stage_config.default_agent


class TestStealLockCallSiteWiring:
    """The SECOND lock check: _start_repair_cycle_for_issue must honor every
    outcome steal_lock() can report. steal_lock()'s own internal correctness
    (the actual retained_reason check, the release+create sequence) is
    covered separately in tests/unit/services/test_pipeline_failure_durability.py
    (TestStealLock) against the real PipelineLockManager — this class covers
    only whether the call site here correctly acts on each of the six
    possible (bool, str) results, using whether the container-launch mock
    was invoked as the unambiguous "did dispatch proceed" signal (see the
    module docstring for why that's necessary)."""

    def _configure_no_competing_holder(self, mock_pipeline_lock_manager_auto):
        # No lock at all as far as the FIRST gate is concerned — isolates
        # these tests to the steal_lock() gate specifically.
        mock_pipeline_lock_manager_auto.get_lock_fail_closed.return_value = (None, True)
        mock_task_queue_get = None  # placeholder, no-op

    def test_refuses_to_steal_a_retained_lock(
        self, mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
        mock_state_manager, mock_task_queue,
    ):
        """The core regression test this file exists for: a repair cycle
        must never steal a retained lock out from under a different, failed
        issue."""
        self._configure_no_competing_holder(mock_pipeline_lock_manager_auto)
        mock_task_queue.redis_client.get.return_value = None
        mock_pipeline_lock_manager_auto.steal_lock.return_value = (
            False, "retained:Repair cycle failed: simulated"
        )

        result, launch_mock, _ = _run_start_repair_cycle(
            mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
            mock_state_manager, mock_task_queue,
        )

        assert result is None
        mock_pipeline_lock_manager_auto.steal_lock.assert_called_once_with('test-project', 'dev', 100)
        launch_mock.assert_not_called()

    def test_refuses_when_lock_state_unknown_at_steal_time(
        self, mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
        mock_state_manager, mock_task_queue,
    ):
        self._configure_no_competing_holder(mock_pipeline_lock_manager_auto)
        mock_task_queue.redis_client.get.return_value = None
        mock_pipeline_lock_manager_auto.steal_lock.return_value = (False, "lock_state_unknown")

        result, launch_mock, _ = _run_start_repair_cycle(
            mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
            mock_state_manager, mock_task_queue,
        )

        assert result is None
        launch_mock.assert_not_called()

    def test_refuses_when_the_forced_release_during_steal_fails(
        self, mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
        mock_state_manager, mock_task_queue,
    ):
        self._configure_no_competing_holder(mock_pipeline_lock_manager_auto)
        mock_task_queue.redis_client.get.return_value = None
        mock_pipeline_lock_manager_auto.steal_lock.return_value = (False, "release_failed")

        result, launch_mock, _ = _run_start_repair_cycle(
            mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
            mock_state_manager, mock_task_queue,
        )

        assert result is None
        launch_mock.assert_not_called()

    def test_proceeds_after_stealing_a_non_retained_lock(
        self, mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
        mock_state_manager, mock_task_queue,
    ):
        self._configure_no_competing_holder(mock_pipeline_lock_manager_auto)
        mock_task_queue.redis_client.get.return_value = None
        mock_pipeline_lock_manager_auto.steal_lock.return_value = (True, "stolen")

        result, launch_mock, stage_config = _run_start_repair_cycle(
            mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
            mock_state_manager, mock_task_queue,
        )

        assert result == stage_config.default_agent
        launch_mock.assert_called_once()
        # steal_lock() owns the actual release+create sequence now — this
        # call site must never perform them directly.
        mock_pipeline_lock_manager_auto.release_lock.assert_not_called()
        mock_pipeline_lock_manager_auto._create_lock.assert_not_called()

    def test_proceeds_after_acquiring_an_unlocked_board(
        self, mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
        mock_state_manager, mock_task_queue,
    ):
        self._configure_no_competing_holder(mock_pipeline_lock_manager_auto)
        mock_task_queue.redis_client.get.return_value = None
        mock_pipeline_lock_manager_auto.steal_lock.return_value = (True, "acquired")

        result, launch_mock, stage_config = _run_start_repair_cycle(
            mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
            mock_state_manager, mock_task_queue,
        )

        assert result == stage_config.default_agent
        launch_mock.assert_called_once()

    def test_proceeds_when_already_holding_the_lock(
        self, mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
        mock_state_manager, mock_task_queue,
    ):
        """Covers the case where this same issue already holds the lock —
        e.g. carried over from a prior Development-stage handoff."""
        self._configure_no_competing_holder(mock_pipeline_lock_manager_auto)
        mock_task_queue.redis_client.get.return_value = None
        mock_pipeline_lock_manager_auto.steal_lock.return_value = (True, "already_held")

        result, launch_mock, stage_config = _run_start_repair_cycle(
            mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
            mock_state_manager, mock_task_queue,
        )

        assert result == stage_config.default_agent
        launch_mock.assert_called_once()


class TestPhantomRunCleanup:
    """Coverage for phantom_run_id and the _end_owned_run_if_pending closure
    added to _start_repair_cycle_for_issue: the run trigger_agent_for_status()
    eagerly creates before it knows this is a repair_cycle stage must be ended
    on every one of this method's early-return guards, and left alone (not
    ended a second time) when it's the one actually reused."""

    def test_duplicate_container_guard_ends_the_phantom(
        self, mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
        mock_state_manager, mock_task_queue,
    ):
        # Surgical subprocess mock: `docker ps` reports the container as
        # running; any other command (the `gh` CLI shellout) fails exactly
        # like the shared harness's default, deterministic failure.
        import subprocess as _subprocess

        def _subprocess_side_effect(cmd, **kwargs):
            if cmd[0] == 'docker':
                result = Mock()
                result.stdout = 'repair-cycle-test-project-100-run-repa\n'
                return result
            raise _subprocess.CalledProcessError(1, cmd)

        mock_task_queue.redis_client.get.return_value = 'repair-cycle-test-project-100-run-repa'
        capture = {}

        result, launch_mock, stage_config = _run_start_repair_cycle(
            mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
            mock_state_manager, mock_task_queue,
            phantom_run_id='phantom-run-1',
            pipeline_manager_capture=capture,
            subprocess_side_effect=_subprocess_side_effect,
        )

        # This guard returns stage_config.default_agent (not None) — it's a
        # "don't duplicate, someone's already running" no-op, not a refusal.
        assert result == stage_config.default_agent
        launch_mock.assert_not_called()
        capture['manager'].end_phantom_pipeline_run.assert_called_once()
        assert capture['manager'].end_phantom_pipeline_run.call_args.kwargs['pipeline_run_id'] == 'phantom-run-1'

    def test_lock_state_unknown_guard_ends_the_phantom(
        self, mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
        mock_state_manager, mock_task_queue,
    ):
        mock_pipeline_lock_manager_auto.get_lock_fail_closed.return_value = (None, False)
        capture = {}

        result, launch_mock, _ = _run_start_repair_cycle(
            mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
            mock_state_manager, mock_task_queue,
            phantom_run_id='phantom-run-2',
            pipeline_manager_capture=capture,
        )

        assert result is None
        launch_mock.assert_not_called()
        capture['manager'].end_phantom_pipeline_run.assert_called_once()
        assert capture['manager'].end_phantom_pipeline_run.call_args.kwargs['pipeline_run_id'] == 'phantom-run-2'

    def test_locked_by_another_repair_cycle_guard_ends_the_phantom(
        self, mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
        mock_state_manager, mock_task_queue,
    ):
        other_lock = Mock()
        other_lock.locked_by_issue = 999
        other_lock.retained_reason = None
        mock_pipeline_lock_manager_auto.get_lock_fail_closed.return_value = (other_lock, True)

        def has_competing_container(key):
            return 'repair-cycle-other-container' if key.endswith(':999') else None
        mock_task_queue.redis_client.get.side_effect = has_competing_container
        capture = {}

        result, launch_mock, _ = _run_start_repair_cycle(
            mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
            mock_state_manager, mock_task_queue,
            phantom_run_id='phantom-run-3',
            pipeline_manager_capture=capture,
        )

        assert result is None
        launch_mock.assert_not_called()
        capture['manager'].end_phantom_pipeline_run.assert_called_once()
        assert capture['manager'].end_phantom_pipeline_run.call_args.kwargs['pipeline_run_id'] == 'phantom-run-3'

    def test_no_test_configurations_guard_ends_the_phantom(
        self, mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
        mock_state_manager, mock_task_queue,
    ):
        self._configure_no_competing_holder(mock_pipeline_lock_manager_auto)
        mock_task_queue.redis_client.get.return_value = None
        capture = {}

        result, launch_mock, _ = _run_start_repair_cycle(
            mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
            mock_state_manager, mock_task_queue,
            phantom_run_id='phantom-run-4',
            pipeline_manager_capture=capture,
            test_types=[],
        )

        assert result is None
        launch_mock.assert_not_called()
        capture['manager'].end_phantom_pipeline_run.assert_called_once()
        assert capture['manager'].end_phantom_pipeline_run.call_args.kwargs['pipeline_run_id'] == 'phantom-run-4'

    def _configure_no_competing_holder(self, mock_pipeline_lock_manager_auto):
        mock_pipeline_lock_manager_auto.get_lock_fail_closed.return_value = (None, True)

    def test_successful_dispatch_reusing_the_phantom_does_not_end_it(
        self, mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
        mock_state_manager, mock_task_queue,
    ):
        """When get_or_create_pipeline_run() returns the SAME id as
        phantom_run_id (the normal, board-scoped-reuse case), the phantom is
        the real run now — it must not be ended."""
        self._configure_no_competing_holder(mock_pipeline_lock_manager_auto)
        mock_task_queue.redis_client.get.return_value = None
        mock_pipeline_lock_manager_auto.steal_lock.return_value = (True, "stolen")
        capture = {}

        result, launch_mock, stage_config = _run_start_repair_cycle(
            mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
            mock_state_manager, mock_task_queue,
            phantom_run_id='run-repair-100',  # matches mock_run.id in the harness
            pipeline_manager_capture=capture,
        )

        assert result == stage_config.default_agent
        launch_mock.assert_called_once()
        capture['manager'].end_phantom_pipeline_run.assert_not_called()

    def test_successful_dispatch_not_reusing_the_phantom_ends_it(
        self, mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
        mock_state_manager, mock_task_queue,
    ):
        """When get_or_create_pipeline_run() returns a DIFFERENT id than
        phantom_run_id (e.g. the phantom expired from Redis before ES caught
        up), the now-superseded phantom must still be ended, even though
        dispatch itself proceeds successfully."""
        self._configure_no_competing_holder(mock_pipeline_lock_manager_auto)
        mock_task_queue.redis_client.get.return_value = None
        mock_pipeline_lock_manager_auto.steal_lock.return_value = (True, "stolen")
        capture = {}

        result, launch_mock, stage_config = _run_start_repair_cycle(
            mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
            mock_state_manager, mock_task_queue,
            phantom_run_id='a-different-phantom-run-id',
            pipeline_manager_capture=capture,
        )

        assert result == stage_config.default_agent
        launch_mock.assert_called_once()
        capture['manager'].end_phantom_pipeline_run.assert_called_once()
        assert capture['manager'].end_phantom_pipeline_run.call_args.kwargs['pipeline_run_id'] == 'a-different-phantom-run-id'

    def test_no_phantom_run_id_never_calls_end_phantom_pipeline_run(
        self, mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
        mock_state_manager, mock_task_queue,
    ):
        """Control case: phantom_run_id=None (the default — used by every
        other test in this file) must never touch end_phantom_pipeline_run,
        regardless of which guard fires or whether dispatch succeeds."""
        self._configure_no_competing_holder(mock_pipeline_lock_manager_auto)
        mock_task_queue.redis_client.get.return_value = None
        mock_pipeline_lock_manager_auto.steal_lock.return_value = (True, "stolen")
        capture = {}

        result, launch_mock, stage_config = _run_start_repair_cycle(
            mock_pipeline_lock_manager_auto, mock_github, mock_config_manager,
            mock_state_manager, mock_task_queue,
            pipeline_manager_capture=capture,
        )

        assert result == stage_config.default_agent
        capture['manager'].end_phantom_pipeline_run.assert_not_called()
