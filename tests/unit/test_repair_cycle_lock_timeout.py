"""
Regression tests for #148: RepairCycleStage must propagate a project
resource-lock timeout instead of retrying it or converting it into a result.

pipeline/repair_cycle.py has its own retry loop and its own exemption lists,
which already re-raise CancellationError and ClaudeCodeRateLimitError by type.
A lock timeout previously fell through to the generic `except Exception`
handlers, which do two harmful things:

  * _run_tests() retries it (max_retries=2, so three attempts), re-running the
    lock's full ~3h poll each time — the ~9h compounding #148 exists to remove —
    and then fabricates a RepairTestResult carrying an "__infrastructure__"
    failure, so pure contention is reported as a failing test that the cycle
    then dispatches fix agents against.
  * the per-file loops and the systemic-analysis handler swallow it, paying the
    same wait once per file and reporting "0 files fixed" / "no systemic issues
    found" for analyses that never ran.

repair_cycle_runner then classifies the propagated timeout as its own outcome
(exit code 6) so project_monitor releases the pipeline run for the next poll
instead of durably retaining the board's lock.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch

from pipeline.repair_cycle import (
    RepairCycleStage,
    RepairTestRunConfig,
    RepairTestResult,
    RepairTestFailure,
)
from services.project_checkout_lock import ProjectCheckoutLockTimeoutError
from services.dev_container_build_lock import DevContainerBuildLockTimeoutError


def _stage():
    return RepairCycleStage(
        name="test_repair_stage",
        test_configs=[RepairTestRunConfig(test_type="integration")],
    )


def _context():
    # No 'observability' key -> obs is None, so every `if obs:` emit block is
    # skipped without needing an observability manager.
    return {
        'project': 'test-project',
        'task_id': 'task-1',
        'issue_number': None,
        'cycle_stack': [],
    }


def _executor_raising(error):
    executor = MagicMock()
    executor.execute_agent = AsyncMock(side_effect=error)
    return executor


@pytest.mark.asyncio
class TestRunTestsDoesNotRetryOrFabricate:
    @pytest.mark.parametrize(
        "error_cls", [ProjectCheckoutLockTimeoutError, DevContainerBuildLockTimeoutError]
    )
    async def test_lock_timeout_propagates_after_one_attempt(self, error_cls):
        stage = _stage()
        executor = _executor_raising(error_cls("could not acquire lock within 10900.0s"))

        with patch('services.agent_executor.get_agent_executor', return_value=executor):
            with pytest.raises(error_cls):
                await stage._run_tests(stage.test_configs[0], _context(), 1, 0)

        assert executor.execute_agent.call_count == 1

    async def test_no_infrastructure_failure_is_fabricated(self):
        """The fabricated RepairTestResult is the harmful part: the cycle would
        dispatch fix agents against a '__infrastructure__' file for a test run
        that never happened."""
        stage = _stage()
        executor = _executor_raising(ProjectCheckoutLockTimeoutError("busy"))

        with patch('services.agent_executor.get_agent_executor', return_value=executor):
            with pytest.raises(ProjectCheckoutLockTimeoutError):
                await stage._run_tests(stage.test_configs[0], _context(), 1, 0)

    async def test_ordinary_execution_failure_still_retries_and_degrades(self):
        """Regression guard: the exemption is scoped to lock timeouts only."""
        stage = _stage()
        executor = _executor_raising(RuntimeError("container died"))

        with patch('services.agent_executor.get_agent_executor', return_value=executor):
            result = await stage._run_tests(stage.test_configs[0], _context(), 1, 0)

        assert executor.execute_agent.call_count == 3  # 1 + max_retries=2
        assert result.failed == 1
        assert result.failures[0].file == "__infrastructure__"


@pytest.mark.asyncio
class TestPerFileLoopsPropagate:
    async def test_fix_failures_by_file_propagates(self):
        stage = _stage()
        executor = _executor_raising(ProjectCheckoutLockTimeoutError("busy"))
        failures_by_file = {
            "tests/test_a.py": [RepairTestFailure(file="tests/test_a.py", test="t", message="boom")],
            "tests/test_b.py": [RepairTestFailure(file="tests/test_b.py", test="t", message="boom")],
        }

        with patch('services.agent_executor.get_agent_executor', return_value=executor):
            with pytest.raises(ProjectCheckoutLockTimeoutError):
                await stage._fix_failures_by_file(
                    failures_by_file, stage.test_configs[0], _context()
                )

        # Bailed on the first file rather than paying the same wait for each.
        assert executor.execute_agent.call_count == 1


@pytest.mark.asyncio
class TestSystemicAnalysisPropagates:
    async def test_analysis_does_not_report_no_issues_found(self):
        """Swallowing here returns has_env_issues=False/has_systemic_code_issues=
        False — an assertion that the analysis found nothing, for an analysis
        that never ran."""
        stage = _stage()
        executor = _executor_raising(ProjectCheckoutLockTimeoutError("busy"))
        failure = RepairTestFailure(file="tests/test_a.py", test="t", message="boom")
        test_result = RepairTestResult(
            test_type="integration", iteration=1, passed=0, failed=1, warnings=0,
            failures=[failure], warning_list=[], raw_output="",
            timestamp="2026-01-01T00:00:00",
        )

        with patch('services.agent_executor.get_agent_executor', return_value=executor):
            with pytest.raises(ProjectCheckoutLockTimeoutError):
                await stage._analyze_systemic_failures(
                    test_result, {"tests/test_a.py": [failure]},
                    stage.test_configs[0], _context()
                )


class TestRunnerClassifiesContentionDistinctly:
    """
    repair_cycle_runner.execute_repair_cycle() must give the propagated timeout
    its own result flag and exit code, the way it already does for
    ClaudeCodeRateLimitError — otherwise project_monitor reads it as a generic
    repair-cycle failure and calls mark_failed(), durably retaining the board's
    pipeline lock over pure contention.
    """

    def test_lock_timeout_returns_lock_contention_result(self):
        import asyncio
        from pipeline.repair_cycle_runner import RepairCycleRunner

        runner = RepairCycleRunner.__new__(RepairCycleRunner)
        runner.stage = MagicMock()
        runner.stage.execute = AsyncMock(side_effect=ProjectCheckoutLockTimeoutError("busy"))
        runner.context = {}
        runner.checkpoint_manager = MagicMock()
        runner._cancelled = False

        result = asyncio.run(runner.execute_repair_cycle())

        assert result['overall_success'] is False
        assert result['lock_contention'] is True
        assert 'error' in result

    def test_ordinary_failure_has_no_contention_flag(self):
        import asyncio
        from pipeline.repair_cycle_runner import RepairCycleRunner

        runner = RepairCycleRunner.__new__(RepairCycleRunner)
        runner.stage = MagicMock()
        runner.stage.execute = AsyncMock(side_effect=RuntimeError("boom"))
        runner.context = {}
        runner.checkpoint_manager = MagicMock()
        runner._cancelled = False

        result = asyncio.run(runner.execute_repair_cycle())

        assert result['overall_success'] is False
        assert 'lock_contention' not in result


@pytest.mark.asyncio
class TestRunTestsDoesNotRetryATerminatedContainer:
    """#160 rider 3. claude/docker_runner.py raises NonRetryableAgentError for
    container exit codes 137/143 -- the OOM killer, or an operator's kill switch.
    Neither changes on a second attempt: an OOM reproduces on every run of the
    same suite, and relaunching a container an operator just killed is the
    opposite of what they asked for. Exhausting the retries is worse than not
    retrying, because it fabricates the same '__infrastructure__' RepairTestResult
    the lock-timeout case above exists to prevent -- and the cycle then dispatches
    fix agents against a container that was killed, not a broken test."""

    async def test_a_terminated_container_propagates_after_one_attempt(self):
        from agents.non_retryable import NonRetryableAgentError

        stage = _stage()
        executor = _executor_raising(
            NonRetryableAgentError(
                "Agent container was terminated by signal (exit_code=137): OOM"
            )
        )

        with patch('services.agent_executor.get_agent_executor', return_value=executor):
            with pytest.raises(NonRetryableAgentError):
                await stage._run_tests(stage.test_configs[0], _context(), 1, 0)

        assert executor.execute_agent.call_count == 1


class TestTheEnvRebuildPollIsBoundedWhenNothingWasQueued:
    """#169 review. The sub-cycle followed queue_dev_environment_setup() with a
    `while True` poll that exits only on a terminal dev container status, with
    no timer -- justified by "image builds can legitimately take longer than any
    test-type timeout", which assumes a build was queued.

    queue_dev_environment_setup() frequently queues nothing and raises nothing:
    it defers to whatever holds the dev_container_build lock. Some of those
    holders do end in a terminal status; the short bookkeeping ones
    (dev_container_build_lock_if_free_sync, _finalize_unconfirmed_changes_needed)
    never do, and neither does a holder that dies inside its window. The poll
    then waited forever for a write nobody would make, and the comment's stated
    backstop does not cover it -- _monitor_repair_cycle_container's 1-hour stall
    check lives in services/project_monitor.py and watches a repair-cycle
    CONTAINER, not this in-process loop."""

    async def _run(self, outcome, statuses):
        from services.dev_container_state import DevContainerStatus
        from pipeline.repair_cycle import SystemicAnalysisResult

        stage = _stage()
        analysis = SystemicAnalysisResult(
            has_env_issues=True,
            has_systemic_code_issues=False,
            env_issue_description="pytest is missing from the image",
            systemic_issue_description="",
            affected_files=[],
            raw_json={},
        )

        slept = []

        async def _no_sleep(seconds):
            slept.append(seconds)

        clock = {'now': 0.0}

        def _monotonic():
            # Every poll interval advances the clock by that interval, so the
            # deadline is reached in bounded test time rather than real time.
            return clock['now']

        def _advance_and_get(project):
            clock['now'] += 30.0
            return statuses.pop(0) if statuses else DevContainerStatus.UNVERIFIED

        with patch('agents.orchestrator_integration.queue_dev_environment_setup',
                   AsyncMock(return_value=outcome)), \
             patch('pipeline.repair_cycle.asyncio.sleep', _no_sleep), \
             patch('pipeline.repair_cycle.time.monotonic', _monotonic), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch.object(stage, '_run_tests', AsyncMock(return_value=RepairTestResult(
                 test_type="integration", iteration=1, passed=1, failed=0, warnings=0,
                 failures=[], warning_list=[], raw_output="",
                 timestamp="2026-01-01T00:00:00",
             ))):
            mock_state.set_status.return_value = True
            mock_state.get_status.side_effect = _advance_and_get

            result = await stage._run_env_rebuild_sub_cycle(
                analysis, RepairTestRunConfig(test_type="integration"),
                _context(), 1, 0,
            )

        return result, slept, mock_state

    @pytest.mark.asyncio
    async def test_a_deferral_does_not_poll_forever(self):
        """THE regression. With nothing queued and no terminal status ever
        written, this used to be an infinite loop whose only escape was the
        pipeline watchdog reaping the run as a containerless zombie."""
        from agents.orchestrator_integration import DevSetupQueueOutcome
        from pipeline.repair_cycle import (
            DEFERRED_ENV_REBUILD_POLL_SECONDS,
            MAX_SYSTEMIC_SUB_CYCLES,
        )

        result, slept, _state = await self._run(
            DevSetupQueueOutcome.DEFERRED_BUILD_LOCK_HELD, statuses=[]
        )

        # Bounded: at most one deadline's worth of 30s polls per attempt, and
        # the attempt loop is itself bounded by MAX_SYSTEMIC_SUB_CYCLES.
        assert len(slept) <= (
            (DEFERRED_ENV_REBUILD_POLL_SECONDS / 30.0) + 1
        ) * MAX_SYSTEMIC_SUB_CYCLES
        assert result.has_failures(), "no rebuild ever completed, so this is a failure"

    @pytest.mark.asyncio
    async def test_every_attempt_is_retried_rather_than_abandoned(self):
        """A deferred attempt is not a dead end: the next `for attempt`
        iteration resets to UNVERIFIED and re-queues, so all
        MAX_SYSTEMIC_SUB_CYCLES attempts still get made."""
        from agents.orchestrator_integration import DevSetupQueueOutcome
        from pipeline.repair_cycle import MAX_SYSTEMIC_SUB_CYCLES

        _result, _slept, mock_state = await self._run(
            DevSetupQueueOutcome.DEFERRED_RUN_IN_FLIGHT, statuses=[]
        )

        # One UNVERIFIED reset per attempt, plus the terminal CHANGES_NEEDED
        # finalization check (which finds no CHANGES_NEEDED and writes nothing).
        unverified_writes = [
            c for c in mock_state.set_status.call_args_list
            if c.args[1].value == 'unverified'
        ]
        assert len(unverified_writes) == MAX_SYSTEMIC_SUB_CYCLES

    @pytest.mark.asyncio
    async def test_a_real_queue_still_gets_the_untimed_poll(self):
        """The case the "no timer here" comment was written for is unchanged: a
        setup really was queued, so the poll waits however long the build takes
        rather than giving up on it."""
        from agents.orchestrator_integration import DevSetupQueueOutcome
        from services.dev_container_state import DevContainerStatus
        from pipeline.repair_cycle import DEFERRED_ENV_REBUILD_POLL_SECONDS

        # Stays non-terminal for longer than the deferred deadline would allow,
        # then verifies. A bounded poll would have given up before this.
        polls_past_the_deadline = int(DEFERRED_ENV_REBUILD_POLL_SECONDS / 30.0) + 5
        statuses = (
            [DevContainerStatus.IN_PROGRESS] * polls_past_the_deadline
            + [DevContainerStatus.VERIFIED]
        )

        result, slept, _state = await self._run(
            DevSetupQueueOutcome.QUEUED, statuses=statuses
        )

        assert len(slept) == polls_past_the_deadline + 1
        assert not result.has_failures()
