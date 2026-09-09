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


def _process(result):
    """
    Drive _process_completed_repair_cycle() with `result`, and return the
    (run_manager, github, work_execution_tracker) mocks so the caller can inspect
    which teardown decision was taken.
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

    with patch('pathlib.Path.exists', return_value=True), \
         patch('builtins.open', mock_open(read_data=json.dumps(CONTEXT))), \
         patch('services.pipeline_run.PipelineRunManager', return_value=run_manager), \
         patch('services.pipeline_run.get_pipeline_run_manager', return_value=run_manager), \
         patch('config.manager.ConfigManager', return_value=MagicMock()), \
         patch('services.github_integration.GitHubIntegration', return_value=github), \
         patch('services.work_execution_state.work_execution_tracker', tracker), \
         patch('services.agent_container_recovery.subprocess'):

        recovery._process_completed_repair_cycle(
            container_name=CONTAINER,
            container_id='deadbeef',
            project=PROJECT,
            issue_number=ISSUE,
            result=result,
        )

    return run_manager, github, tracker


def _recorded_outcomes(tracker):
    return [
        call.kwargs.get('outcome')
        for call in tracker.record_execution_outcome.call_args_list
    ]


class TestLockContentionResult:
    RESULT = {
        'overall_success': False,
        'lock_contention': True,
        'error': "Could not acquire 'project_checkout' lock within 10900.0s",
    }

    def test_mark_failed_is_not_called(self):
        run_manager, _, _ = _process(dict(self.RESULT))
        run_manager.mark_failed.assert_not_called()

    def test_run_is_released_without_retaining_the_lock(self):
        run_manager, _, _ = _process(dict(self.RESULT))
        run_manager.end_pipeline_run.assert_called_once()
        assert run_manager.end_pipeline_run.call_args.kwargs['retain_lock'] is False

    def test_no_failure_summary_comment_is_posted(self):
        _, github, _ = _process(dict(self.RESULT))
        github.post_agent_output.assert_not_called()

    def test_outcome_is_recorded_as_lock_contention(self):
        _, _, tracker = _process(dict(self.RESULT))
        assert _recorded_outcomes(tracker) == ['lock_contention']


class TestOrdinaryFailureResultIsUnchanged:
    """Control: everything above must be specific to the contention flag."""

    RESULT = {'overall_success': False, 'error': 'integration tests still failing'}

    def test_mark_failed_is_still_called(self):
        run_manager, _, _ = _process(dict(self.RESULT))
        run_manager.mark_failed.assert_called_once()
        assert run_manager.mark_failed.call_args.kwargs['reason'] == "Repair cycle failed"

    def test_failure_summary_comment_is_still_posted(self):
        _, github, _ = _process(dict(self.RESULT))
        github.post_agent_output.assert_called_once()
        assert "Repair Cycle Failed" in github.post_agent_output.call_args.args[1]

    def test_lock_contention_outcome_is_not_recorded(self):
        _, _, tracker = _process(dict(self.RESULT))
        assert 'lock_contention' not in _recorded_outcomes(tracker)


class TestFrozenResultIsUnchanged:
    """Control: the frozen branch this one was modelled on must still win."""

    RESULT = {'overall_success': False, 'frozen': True, 'error': 'token limit'}

    def test_frozen_still_leaves_the_run_active_for_the_watchdog(self):
        run_manager, github, tracker = _process(dict(self.RESULT))
        run_manager.mark_failed.assert_not_called()
        run_manager.end_pipeline_run.assert_not_called()
        github.post_agent_output.assert_not_called()
        assert _recorded_outcomes(tracker) == ['frozen']
