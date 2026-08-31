"""
Unit tests for RepairCycleStage._run_test_cycle()'s handling of "__infrastructure__"
failures.

Before this change, ANY "__infrastructure__"-tagged failure hard-stopped the whole
test-type cycle immediately — no retry, no systemic analysis, no env-rebuild attempt —
regardless of whether it carried real diagnostic content or was just the agent never
returning usable output. That turned a single bad turn into a full pipeline failure
requiring manual intervention. See pipeline runs a507e510-c62b-4a93-af86-2f78ff3c24da
and 5a4d12e7-72d2-4b11-84e2-263952488037 for the incidents that motivated this.

Now the two situations are distinguished by the failure's `.test` sentinel name:
  - content-less (_run_tests()'s own retry-exhaustion sentinels): retried a bounded
    number of times via a plain re-run.
  - content-bearing (the agent's own runner-reported crash/collection failure): routed
    through the normal systemic-analysis / fix-cycle machinery instead of failing
    immediately.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, patch

from pipeline.repair_cycle import (
    RepairCycleStage,
    RepairTestRunConfig,
    RepairTestResult,
    RepairTestFailure,
    SystemicAnalysisResult,
    MAX_SYSTEMIC_SUB_CYCLES,
)


def _stage():
    return RepairCycleStage(
        name="test_repair_stage",
        test_configs=[RepairTestRunConfig(test_type="integration")],
    )


def _context():
    # No 'observability' key -> obs is None inside the method, so every `if obs:`
    # emit block is skipped without needing to mock an observability manager.
    return {'project': 'test-project', 'task_id': 'task-1', 'issue_number': None, 'cycle_stack': []}


def _no_content_infra_result(test_name="test_execution_json_parse"):
    return RepairTestResult(
        test_type="integration", iteration=1, passed=0, failed=1, warnings=0,
        failures=[RepairTestFailure(
            file="__infrastructure__", test=test_name,
            message="INFRASTRUCTURE FAILURE: Agent failed to return valid JSON test results "
                    "after 3 attempts: No valid JSON test result found in response.",
        )],
        warning_list=[], raw_output="", timestamp="2026-01-01T00:00:00",
    )


def _content_bearing_infra_result():
    return RepairTestResult(
        test_type="integration", iteration=1, passed=0, failed=1, warnings=0,
        failures=[RepairTestFailure(
            file="__infrastructure__", test="pytest_execution_failed",
            message="ModuleNotFoundError: No module named 'utils' — collection failed with 41 errors",
        )],
        warning_list=[], raw_output="", timestamp="2026-01-01T00:00:00",
    )


def _passing_result():
    return RepairTestResult(
        test_type="integration", iteration=1, passed=5, failed=0, warnings=0,
        failures=[], warning_list=[], raw_output="", timestamp="2026-01-01T00:00:00",
    )


def _no_systemic_issues():
    return SystemicAnalysisResult(
        has_env_issues=False, has_systemic_code_issues=False,
        env_issue_description="", systemic_issue_description="",
        affected_files=[], raw_json={},
    )


@pytest.mark.asyncio
class TestNoContentInfrastructureFailureRetries:
    """Content-less infrastructure failures (the agent never returned usable output)
    get a bounded plain re-run instead of failing the whole test type immediately."""

    async def test_recovers_after_one_no_content_failure(self):
        """Matches documentation_robotics run 5a4d12e7's shape at smaller scale:
        one content-less infra failure, then a clean pass on retry."""
        stage = _stage()
        with patch.object(stage, '_run_tests', new=AsyncMock(
                side_effect=[_no_content_infra_result(), _passing_result()])) as mock_run_tests, \
             patch.object(stage, '_checkpoint', new=AsyncMock()):
            result = await stage._run_test_cycle(
                RepairTestRunConfig(test_type="integration"), _context(), test_type_index=1,
            )

        assert mock_run_tests.call_count == 2
        assert result.passed is True

    async def test_gives_up_after_exhausting_retry_budget(self):
        """If every attempt is content-less, the cycle must still eventually fail
        (not loop forever) — bounded by MAX_SYSTEMIC_SUB_CYCLES, independent of
        config.max_iterations."""
        stage = _stage()
        with patch.object(stage, '_run_tests', new=AsyncMock(
                return_value=_no_content_infra_result())) as mock_run_tests, \
             patch.object(stage, '_checkpoint', new=AsyncMock()):
            result = await stage._run_test_cycle(
                RepairTestRunConfig(test_type="integration", max_iterations=10), _context(), test_type_index=1,
            )

        assert mock_run_tests.call_count == MAX_SYSTEMIC_SUB_CYCLES + 1
        assert result.passed is False
        assert "Infrastructure failure" in result.error

    async def test_execution_failure_sentinel_also_retries(self):
        """The exception-path sentinel ('test_execution_failure', not the JSON-parse
        one) gets the same content-less treatment."""
        stage = _stage()
        with patch.object(stage, '_run_tests', new=AsyncMock(side_effect=[
                _no_content_infra_result(test_name="test_execution_failure"),
                _passing_result(),
            ])) as mock_run_tests, \
             patch.object(stage, '_checkpoint', new=AsyncMock()):
            result = await stage._run_test_cycle(
                RepairTestRunConfig(test_type="integration"), _context(), test_type_index=1,
            )

        assert mock_run_tests.call_count == 2
        assert result.passed is True


@pytest.mark.asyncio
class TestContentBearingInfrastructureFailureRouting:
    """Infrastructure failures with real diagnostic content (the agent faithfully
    reporting a genuine crash/collection failure) are routed through the normal
    systemic-analysis / fix-cycle machinery instead of failing immediately."""

    async def test_routes_to_systemic_analysis_instead_of_failing_immediately(self):
        """Matches context-studio run a507e510's shape: a real ModuleNotFoundError
        reported through the __infrastructure__ sentinel must reach systemic
        analysis, not bail before ever calling it."""
        stage = _stage()
        with patch.object(stage, '_run_tests', new=AsyncMock(side_effect=[
                _content_bearing_infra_result(),
                _passing_result(),
            ])) as mock_run_tests, \
             patch.object(stage, '_checkpoint', new=AsyncMock()), \
             patch.object(stage, '_analyze_systemic_failures', new=AsyncMock(
                 return_value=_no_systemic_issues())) as mock_analyze, \
             patch.object(stage, '_fix_failures_by_file', new=AsyncMock(return_value=1)) as mock_fix:
            result = await stage._run_test_cycle(
                RepairTestRunConfig(test_type="integration"), _context(), test_type_index=1,
            )

        mock_analyze.assert_called_once()
        # The systemic analysis call must see the infrastructure failure grouped
        # exactly like any other failing "file".
        grouped_failures_arg = mock_analyze.call_args.args[1]
        assert "__infrastructure__" in grouped_failures_arg
        mock_fix.assert_called_once()
        assert mock_run_tests.call_count == 2
        assert result.passed is True

    async def test_env_rebuild_invoked_when_analysis_flags_env_issue(self):
        """If systemic analysis classifies the content-bearing infra failure as an
        environment issue, the env-rebuild sub-cycle (not immediate failure) must
        be what runs."""
        stage = _stage()
        env_issue_analysis = SystemicAnalysisResult(
            has_env_issues=True, has_systemic_code_issues=False,
            env_issue_description="Missing 'utils' package on PYTHONPATH",
            systemic_issue_description="", affected_files=[], raw_json={},
        )
        with patch.object(stage, '_run_tests', new=AsyncMock(side_effect=[
                _content_bearing_infra_result(),
                _passing_result(),
            ])) as mock_run_tests, \
             patch.object(stage, '_checkpoint', new=AsyncMock()), \
             patch.object(stage, '_analyze_systemic_failures', new=AsyncMock(
                 return_value=env_issue_analysis)), \
             patch.object(stage, '_run_env_rebuild_sub_cycle', new=AsyncMock(
                 return_value=_passing_result())) as mock_env_rebuild, \
             patch.object(stage, '_fix_failures_by_file', new=AsyncMock(return_value=0)) as mock_fix:
            result = await stage._run_test_cycle(
                RepairTestRunConfig(test_type="integration"), _context(), test_type_index=1,
            )

        mock_env_rebuild.assert_called_once()
        mock_fix.assert_not_called()  # the env rebuild path resolved it; no per-file fix needed
        assert mock_run_tests.call_count == 2
        assert result.passed is True
