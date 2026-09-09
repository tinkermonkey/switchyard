"""
Regression tests for #148: the repair-cycle runner -> monitor contract for the
two outcomes that are NOT repair-cycle failures.

The contract is a bare integer agreed in two files by hand — RepairCycleRunner.
run() returns 6 for lock contention, and project_monitor matches it — plus a
duplicate flag in the result dict the runner persists to Redis before exiting.
Nothing pinned either half. If a new exit code is inserted, or the
`elif result.get('lock_contention')` chain is reordered relative to the
`elif result.get('error')` that follows it (the contention result carries an
'error' key too, so that ordering is load-bearing), contention silently reverts
to a generic exit-2 failure — which lands in the failure branch, calls
mark_failed() and durably retains the board's lock. A wrong-but-plausible
default with a wide blast radius.

classify_repair_cycle_outcome() is the monitor half, shared by the live
container-completion handler and agent_container_recovery's restart-recovery
consumer of the same result dict, so the two cannot drift apart again.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import MagicMock, patch

from pipeline.repair_cycle_runner import RepairCycleRunner
from services.project_monitor import (
    classify_repair_cycle_outcome,
    REPAIR_CYCLE_EXIT_FROZEN,
    REPAIR_CYCLE_EXIT_LOCK_CONTENTION,
)


def _runner(result):
    """A RepairCycleRunner whose execute_repair_cycle() yields `result`, with
    every side effect (context load, stage init, Redis persistence) stubbed. The
    constructor is bypassed: it reads context.json off disk, which has nothing
    to do with the exit-code mapping under test."""
    runner = object.__new__(RepairCycleRunner)
    runner.args = MagicMock(project='test-project', issue=1, pipeline_run_id='run-1')
    runner._cancelled = False
    runner.load_context = MagicMock(return_value={})
    runner.initialize_stage = MagicMock(return_value=True)
    runner.save_result_to_redis = MagicMock(return_value=True)

    async def _execute():
        return result

    runner.execute_repair_cycle = _execute
    return runner


class TestRunnerExitCodes:
    def test_lock_contention_returns_its_own_exit_code(self):
        runner = _runner({
            'overall_success': False,
            'lock_contention': True,
            'error': "Could not acquire 'project_checkout' lock within 10900.0s",
        })
        assert runner.run() == REPAIR_CYCLE_EXIT_LOCK_CONTENTION

    def test_lock_contention_wins_over_the_error_key_it_also_carries(self):
        """The contention result always carries 'error' as well, so the
        `elif lock_contention` must stay ahead of the `elif error` that maps to
        the generic exit code 2."""
        runner = _runner({
            'overall_success': False,
            'lock_contention': True,
            'error': 'anything at all',
        })
        assert runner.run() != 2

    def test_error_only_result_still_returns_the_generic_error_code(self):
        runner = _runner({'overall_success': False, 'error': 'pytest exploded'})
        assert runner.run() == 2

    def test_frozen_result_still_returns_its_own_exit_code(self):
        runner = _runner({'overall_success': False, 'frozen': True, 'error': 'token limit'})
        assert runner.run() == REPAIR_CYCLE_EXIT_FROZEN

    def test_success_still_returns_zero(self):
        runner = _runner({'overall_success': True})
        assert runner.run() == 0

    def test_result_is_persisted_before_the_contention_exit_code(self):
        """The restart-recovery path only ever sees the Redis result — if the
        contention branch skipped save_result_to_redis(), a restart during the
        contention window would lose the signal entirely."""
        result = {'overall_success': False, 'lock_contention': True, 'error': 'busy'}
        runner = _runner(result)
        assert runner.run() == REPAIR_CYCLE_EXIT_LOCK_CONTENTION
        runner.save_result_to_redis.assert_called_once_with(result)


class TestClassifyRepairCycleOutcome:
    def test_exit_code_alone_is_enough(self):
        assert classify_repair_cycle_outcome(
            REPAIR_CYCLE_EXIT_LOCK_CONTENTION, None, False
        ) == 'lock_contention'
        assert classify_repair_cycle_outcome(
            REPAIR_CYCLE_EXIT_FROZEN, None, False
        ) == 'frozen'

    def test_result_flag_alone_is_enough(self):
        """The restart-recovery consumer passes exit_code=None — the container is
        long gone by then, so the flag is all that survives."""
        assert classify_repair_cycle_outcome(
            None, {'overall_success': False, 'lock_contention': True}, False
        ) == 'lock_contention'
        assert classify_repair_cycle_outcome(
            None, {'overall_success': False, 'frozen': True}, False
        ) == 'frozen'

    def test_generic_failure_is_not_contention(self):
        assert classify_repair_cycle_outcome(
            2, {'overall_success': False, 'error': 'tests failed'}, False
        ) == 'failure'
        assert classify_repair_cycle_outcome(None, None, False) == 'failure'

    def test_success(self):
        assert classify_repair_cycle_outcome(0, {'overall_success': True}, True) == 'success'

    def test_contention_is_not_masked_by_a_partial_success(self):
        assert classify_repair_cycle_outcome(
            REPAIR_CYCLE_EXIT_LOCK_CONTENTION, {'overall_success': True}, True
        ) == 'lock_contention'

    def test_frozen_wins_when_both_flags_somehow_appear(self):
        """Frozen has a watchdog resume path and contention does not, so an
        ambiguous result must take the one that leaves the run recoverable."""
        assert classify_repair_cycle_outcome(
            None, {'frozen': True, 'lock_contention': True}, False
        ) == 'frozen'
