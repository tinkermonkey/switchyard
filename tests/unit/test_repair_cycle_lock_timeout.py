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
