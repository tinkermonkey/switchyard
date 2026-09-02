"""
Unit tests for RepairCycleStage._run_env_rebuild_sub_cycle()'s retry semantics.

Covers the fix that gave the env-rebuild sub-cycle a genuine third outcome
(CHANGES_NEEDED) distinct from BLOCKED: a verifier that could not independently
confirm a REQUIRED FIX (as opposed to confirming it's still broken) should let
the sub-cycle retry, not permanently stop it the way BLOCKED does.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, Mock, patch

from pipeline.repair_cycle import (
    RepairCycleStage,
    RepairTestRunConfig,
    RepairTestResult,
    RepairTestFailure,
    SystemicAnalysisResult,
    MAX_SYSTEMIC_SUB_CYCLES,
)
from services.dev_container_state import DevContainerStatus


def _stage():
    return RepairCycleStage(
        name="test_repair_stage",
        test_configs=[RepairTestRunConfig(test_type="unit")],
    )


def _analysis():
    return SystemicAnalysisResult(
        has_env_issues=True,
        has_systemic_code_issues=False,
        env_issue_description="Add codegen extra to pyproject.toml",
        systemic_issue_description="",
        affected_files=[],
        raw_json={},
    )


def _passing_result():
    return RepairTestResult(
        test_type="unit", iteration=1, passed=5, failed=0, warnings=0,
        failures=[], warning_list=[], raw_output="", timestamp="2026-01-01T00:00:00",
    )


def _context():
    # No 'observability' key -> obs is None inside the method, so every `if obs:`
    # emit block is skipped without needing to mock an observability manager.
    return {'project': 'test-project', 'task_id': 'task-1', 'issue_number': None, 'cycle_stack': []}


@pytest.mark.asyncio
class TestEnvRebuildSubCycleRetrySemantics:

    async def test_changes_needed_retries_instead_of_stopping(self):
        """CHANGES_NEEDED on attempt 1, VERIFIED on attempt 2 -> the sub-cycle must
        retry (re-queue setup a second time) rather than stopping like BLOCKED does,
        and must return the passing test result from the successful attempt."""
        stage = _stage()

        with patch('agents.orchestrator_integration.queue_dev_environment_setup', new=AsyncMock()) as mock_queue, \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('pipeline.repair_cycle.asyncio.sleep', new=AsyncMock()), \
             patch.object(stage, '_run_tests', new=AsyncMock(return_value=_passing_result())) as mock_run_tests:

            mock_state.get_status.side_effect = [
                DevContainerStatus.CHANGES_NEEDED,  # attempt 1's poll
                DevContainerStatus.VERIFIED,        # attempt 2's poll
            ]

            result = await stage._run_env_rebuild_sub_cycle(
                _analysis(), RepairTestRunConfig(test_type="unit"), _context(),
                test_cycle_iteration=1, test_type_index=0,
            )

        assert mock_queue.call_count == 2  # retried: queued setup again after CHANGES_NEEDED
        mock_run_tests.assert_called_once()  # tests only run after the VERIFIED attempt
        assert result.has_failures() is False

    async def test_blocked_still_stops_after_one_attempt(self):
        """Regression guard: BLOCKED must still stop the sub-cycle immediately,
        unlike CHANGES_NEEDED — this behavior must not change."""
        stage = _stage()

        with patch('agents.orchestrator_integration.queue_dev_environment_setup', new=AsyncMock()) as mock_queue, \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('pipeline.repair_cycle.asyncio.sleep', new=AsyncMock()), \
             patch.object(stage, '_run_tests', new=AsyncMock()) as mock_run_tests:

            mock_state.get_status.return_value = DevContainerStatus.BLOCKED

            result = await stage._run_env_rebuild_sub_cycle(
                _analysis(), RepairTestRunConfig(test_type="unit"), _context(),
                test_cycle_iteration=1, test_type_index=0,
            )

        assert mock_queue.call_count == 1  # not retried
        mock_run_tests.assert_not_called()
        assert result.has_failures() is True  # synthetic failure — never got to run tests

        # BLOCKED is already terminal on arrival (the verifier set it) — the
        # exhaustion/circuit-breaker BLOCKED transition must not fire again on
        # top of it. Only the UNVERIFIED reset from the single attempt made.
        assert mock_state.set_status.call_count == 1

    async def test_verified_but_still_failing_exhaustion_does_not_force_blocked(self):
        """If every attempt reaches VERIFIED but tests keep failing, the loop
        exhausts the same way as the CHANGES_NEEDED case (no early break) — but
        this is a legitimate 'rebuild worked, code is still broken' outcome, not
        an unconfirmed-fix outcome, so it must NOT be forced into BLOCKED. Only
        the verifier itself (or the CHANGES_NEEDED-exhaustion path) may do that."""
        stage = _stage()
        failing_result = RepairTestResult(
            test_type="unit", iteration=1, passed=0, failed=3, warnings=0,
            failures=[RepairTestFailure(file="foo.py", test="test_x", message="boom")],
            warning_list=[], raw_output="", timestamp="2026-01-01T00:00:00",
        )

        with patch('agents.orchestrator_integration.queue_dev_environment_setup', new=AsyncMock()) as mock_queue, \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('pipeline.repair_cycle.asyncio.sleep', new=AsyncMock()), \
             patch.object(stage, '_run_tests', new=AsyncMock(return_value=failing_result)) as mock_run_tests:

            mock_state.get_status.return_value = DevContainerStatus.VERIFIED

            result = await stage._run_env_rebuild_sub_cycle(
                _analysis(), RepairTestRunConfig(test_type="unit"), _context(),
                test_cycle_iteration=1, test_type_index=0,
            )

        assert mock_queue.call_count == MAX_SYSTEMIC_SUB_CYCLES
        assert mock_run_tests.call_count == MAX_SYSTEMIC_SUB_CYCLES
        assert result.has_failures() is True
        assert not any(
            call.args[1] == DevContainerStatus.BLOCKED
            for call in mock_state.set_status.call_args_list
        )

    async def test_circuit_breaker_after_changes_needed_still_blocks_not_dangles(self):
        """If the circuit breaker trips on the attempt *after* a CHANGES_NEEDED
        poll, the loop exits via `break` — which skips the exhaustion `for...else`
        clause entirely. The dev container must still end up BLOCKED, not left
        dangling at CHANGES_NEEDED with no owner: this is the second, less
        obvious way to reproduce the exact 'stuck forever' incident this fix
        targets, found in PR review."""
        stage = _stage()
        stage.max_total_agent_calls = 1

        async def _queue_side_effect(*args, **kwargs):
            # Simulate the queued setup consuming the one allowed agent call, so
            # the circuit breaker trips at the top of the *next* attempt.
            stage._agent_call_count = stage.max_total_agent_calls

        with patch('agents.orchestrator_integration.queue_dev_environment_setup', new=AsyncMock(side_effect=_queue_side_effect)) as mock_queue, \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('pipeline.repair_cycle.asyncio.sleep', new=AsyncMock()), \
             patch.object(stage, '_run_tests', new=AsyncMock()) as mock_run_tests:

            mock_state.get_status.return_value = DevContainerStatus.CHANGES_NEEDED

            result = await stage._run_env_rebuild_sub_cycle(
                _analysis(), RepairTestRunConfig(test_type="unit"), _context(),
                test_cycle_iteration=1, test_type_index=0,
            )

        assert mock_queue.call_count == 1  # circuit breaker stopped it before a 2nd attempt
        mock_run_tests.assert_not_called()
        assert result.has_failures() is True

        last_call = mock_state.set_status.call_args_list[-1]
        assert last_call.args[1] == DevContainerStatus.BLOCKED

    async def test_changes_needed_exhausts_attempts_without_verified(self):
        """If every attempt comes back CHANGES_NEEDED, the sub-cycle must stop after
        MAX_SYSTEMIC_SUB_CYCLES attempts (not loop forever), return a failure having
        never run tests, and — critically — must leave the dev container in the
        terminal BLOCKED status rather than dangling at CHANGES_NEEDED. Nothing else
        owns retrying CHANGES_NEEDED once this sub-cycle gives up
        (validate_task_can_run() deliberately never re-triggers setup for it), so
        leaving it there would silently block every future task for the project
        forever. Regression guard for that exact incident."""
        stage = _stage()

        with patch('agents.orchestrator_integration.queue_dev_environment_setup', new=AsyncMock()) as mock_queue, \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('pipeline.repair_cycle.asyncio.sleep', new=AsyncMock()), \
             patch.object(stage, '_run_tests', new=AsyncMock()) as mock_run_tests:

            mock_state.get_status.return_value = DevContainerStatus.CHANGES_NEEDED

            result = await stage._run_env_rebuild_sub_cycle(
                _analysis(), RepairTestRunConfig(test_type="unit"), _context(),
                test_cycle_iteration=1, test_type_index=0,
            )

        assert mock_queue.call_count == MAX_SYSTEMIC_SUB_CYCLES
        mock_run_tests.assert_not_called()
        assert result.has_failures() is True

        # Last set_status call (after MAX_SYSTEMIC_SUB_CYCLES UNVERIFIED resets,
        # one per attempt) must be the terminal BLOCKED transition.
        last_call = mock_state.set_status.call_args_list[-1]
        assert last_call.args[1] == DevContainerStatus.BLOCKED
        assert "test-project" in last_call.args[0]
