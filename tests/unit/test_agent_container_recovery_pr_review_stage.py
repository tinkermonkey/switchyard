"""
Regression test for the "agent mismatch" false positive that killed a live,
26-minutes-in PR review container on an orchestrator restart.

_start_pr_review_for_issue (services/project_monitor.py) deliberately records
in_progress execution state under the synthetic wrapper agent name
'pr_review_stage' rather than the real phase agent (pr_code_reviewer,
requirements_verifier, etc.) — see the comment there for why. But
AgentContainerRecovery.recover_or_cleanup_containers compared the container's
own resolved agent against that wrapper name verbatim, always found a
"mismatch", and killed the container.
"""

from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from services.agent_container_recovery import AgentContainerRecovery


def _future_created_at():
    """A created_at timestamp after this test process started, so recovery takes
    the 'already monitored, skip reconnect' shortcut instead of exercising the
    full DockerAgentRunner reconnect path."""
    return (datetime.utcnow() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S +0000 UTC')


def _make_container(agent='pr_code_reviewer'):
    return {
        'name': 'claude-agent-context-studio-9ed4ab1c',
        'id': 'abc123',
        'status': 'Up 26 minutes',
        'created_at': _future_created_at(),
        'image': 'context-studio-agent',
        'labels': (
            'org.switchyard.project=context-studio,'
            f'org.switchyard.agent={agent},'
            'org.switchyard.task_id=task-1,'
            'org.switchyard.issue_number=1137'
        ),
    }


def _make_recovery():
    return AgentContainerRecovery(redis_client=MagicMock())


class TestPrReviewStageWrapperAgentMismatch:
    def test_pr_code_reviewer_container_not_killed_under_pr_review_stage_wrapper(self):
        """The exact scenario that killed a live container: history recorded under
        the 'pr_review_stage' wrapper, container's own resolved agent is the real
        phase agent (pr_code_reviewer) — must be recovered, not killed."""
        recovery = _make_recovery()
        container = _make_container(agent='pr_code_reviewer')

        with patch.object(recovery, 'get_running_agent_containers', return_value=[container]), \
             patch.object(recovery, 'check_execution_history', return_value={
                 'agent': 'pr_review_stage',
                 'column': 'In Review',
                 'outcome': 'in_progress',
                 'timestamp': datetime.utcnow().isoformat(),
             }), \
             patch.object(recovery, 'kill_container') as mock_kill, \
             patch.object(recovery, 'cleanup_execution_state') as mock_cleanup, \
             patch.object(recovery, 'cleanup_orphaned_execution_history', return_value=0):
            recovered, killed, errors = recovery.recover_or_cleanup_containers()

        assert killed == 0, "pr_code_reviewer container must not be killed under the pr_review_stage wrapper"
        assert recovered == 1
        assert errors == 0
        mock_kill.assert_not_called()
        mock_cleanup.assert_not_called()

    def test_requirements_verifier_container_not_killed_under_pr_review_stage_wrapper(self):
        """Same wrapper, different real phase agent (Phase 2) — also must survive."""
        recovery = _make_recovery()
        container = _make_container(agent='requirements_verifier')

        with patch.object(recovery, 'get_running_agent_containers', return_value=[container]), \
             patch.object(recovery, 'check_execution_history', return_value={
                 'agent': 'pr_review_stage',
                 'column': 'In Review',
                 'outcome': 'in_progress',
                 'timestamp': datetime.utcnow().isoformat(),
             }), \
             patch.object(recovery, 'kill_container') as mock_kill, \
             patch.object(recovery, 'cleanup_orphaned_execution_history', return_value=0):
            recovered, killed, errors = recovery.recover_or_cleanup_containers()

        assert killed == 0
        assert recovered == 1
        mock_kill.assert_not_called()

    def test_genuine_agent_mismatch_outside_pr_review_stage_still_killed(self):
        """Guardrail: a real mismatch (history agent isn't the wrapper AND doesn't
        match the container's resolved agent) must still be killed — this fix only
        special-cases the 'pr_review_stage' wrapper, not mismatch detection broadly."""
        recovery = _make_recovery()
        container = _make_container(agent='senior_software_engineer')

        with patch.object(recovery, 'get_running_agent_containers', return_value=[container]), \
             patch.object(recovery, 'check_execution_history', return_value={
                 'agent': 'code_reviewer',
                 'column': 'In Review',
                 'outcome': 'in_progress',
                 'timestamp': datetime.utcnow().isoformat(),
             }), \
             patch.object(recovery, 'kill_container') as mock_kill, \
             patch.object(recovery, 'cleanup_execution_state') as mock_cleanup, \
             patch.object(recovery, 'cleanup_orphaned_execution_history', return_value=0):
            recovered, killed, errors = recovery.recover_or_cleanup_containers()

        assert killed == 1
        assert recovered == 0
        mock_kill.assert_called_once()
        mock_cleanup.assert_called_once_with(
            'context-studio', 1137, 'senior_software_engineer', 'agent_mismatch'
        )
