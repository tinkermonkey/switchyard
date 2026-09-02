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


def _past_created_at(minutes_ago=30):
    """A created_at timestamp before this test process started, so recovery takes
    the full reconnect path instead of the 'already monitored' shortcut."""
    return (datetime.utcnow() - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%d %H:%M:%S +0000 UTC')


class TestPrReviewPhaseLabelsPassedToReconnect:
    """A recovered PR-review-stage phase container carries which phase and review
    cycle it belongs to (org.switchyard.pr_review_phase / pr_review_cycle labels) so
    completion handling can checkpoint its real output instead of posting it as a
    stray terminal comment — see claude/docker_runner.py's
    _process_recovered_pr_review_phase_completion."""

    def test_phase_and_cycle_labels_are_read_and_passed_through(self):
        recovery = _make_recovery()
        container = {
            'name': 'claude-agent-context-studio-9ed4ab1c',
            'id': 'abc123',
            'status': 'Up 30 minutes',
            'created_at': _past_created_at(),
            'image': 'context-studio-agent',
            'labels': (
                'org.switchyard.project=context-studio,'
                'org.switchyard.agent=pr_code_reviewer,'
                'org.switchyard.task_id=task-1,'
                'org.switchyard.issue_number=1137,'
                'org.switchyard.pr_review_phase=code_review,'
                'org.switchyard.pr_review_cycle=1'
            ),
        }

        mock_docker_runner_instance = MagicMock()
        mock_docker_runner_class = MagicMock(return_value=mock_docker_runner_instance)

        with patch.object(recovery, 'get_running_agent_containers', return_value=[container]), \
             patch.object(recovery, 'check_execution_history', return_value={
                 'agent': 'pr_review_stage',
                 'column': 'In Review',
                 'outcome': 'in_progress',
                 'timestamp': datetime.utcnow().isoformat(),
             }), \
             patch.object(recovery, 'cleanup_orphaned_execution_history', return_value=0), \
             patch.dict('sys.modules', {'claude.docker_runner': MagicMock(
                 DockerAgentRunner=mock_docker_runner_class
             )}):
            recovered, killed, errors = recovery.recover_or_cleanup_containers()

        assert recovered == 1
        assert killed == 0
        mock_docker_runner_instance.reconnect_to_container.assert_called_once()
        call_kwargs = mock_docker_runner_instance.reconnect_to_container.call_args.kwargs
        assert call_kwargs['pr_review_phase'] == 'code_review'
        assert call_kwargs['pr_review_cycle'] == '1'

    def test_labels_absent_for_non_pr_review_container(self):
        """A container with no pr_review_phase label (any other agent type) must pass
        None through, not fail or fabricate a phase."""
        recovery = _make_recovery()
        container = _make_container(agent='senior_software_engineer')
        container['created_at'] = _past_created_at()

        mock_docker_runner_instance = MagicMock()
        mock_docker_runner_class = MagicMock(return_value=mock_docker_runner_instance)

        with patch.object(recovery, 'get_running_agent_containers', return_value=[container]), \
             patch.object(recovery, 'check_execution_history', return_value={
                 'agent': 'senior_software_engineer',
                 'column': 'Development',
                 'outcome': 'in_progress',
                 'timestamp': datetime.utcnow().isoformat(),
             }), \
             patch.object(recovery, 'cleanup_orphaned_execution_history', return_value=0), \
             patch.dict('sys.modules', {'claude.docker_runner': MagicMock(
                 DockerAgentRunner=mock_docker_runner_class
             )}):
            recovery.recover_or_cleanup_containers()

        call_kwargs = mock_docker_runner_instance.reconnect_to_container.call_args.kwargs
        assert call_kwargs['pr_review_phase'] is None
        assert call_kwargs['pr_review_cycle'] is None
