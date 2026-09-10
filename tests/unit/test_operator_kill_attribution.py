"""
Regression tests for #160 review: an operator kill must reach the retry loop as
a CANCELLATION, and the orchestrator's own grace-period kill must stay
RETRYABLE.

Two separate holes in the #160 mitigation, both of which turn a 137 into a
first-strike terminal 'failure' that feeds
work_execution_tracker.count_consecutive_failures() -> project_monitor's
MAX_CONSECUTIVE_DISPATCH_FAILURES -> mark_failed() and a durably retained board
lock:

  1. POST /agents/kill/<container> only took its cancellation branch when the
     `agent:container:*` Redis hash still existed. That hash had a 7200s TTL
     while config/foundations/agents.yaml gives senior_software_engineer a
     timeout of 10800s -- so the metadata expired under a live container after
     two hours, which is exactly the state an operator reaches for the kill
     switch in. It also vanishes whenever Redis is unreachable (the hgetall is
     wrapped in `except Exception`). The container's own Docker labels carry the
     same attribution and cannot expire under it.

  2. claude/docker_runner.py's grace-period kill -- the orchestrator killing a
     container that outlived _CLEANUP_GRACE_SECONDS after Claude's end_turn
     because background processes held it open -- also produces exit 137. Its
     own in-code comment says "let the failure stand so the cycle retries", and
     since #160 stopped the agent wrappers re-wrapping NonRetryableAgentError,
     raising that type for it made the promised retry impossible everywhere at
     once. Nothing external terminated that container; a relaunch very plausibly
     succeeds.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import MagicMock, patch

from agents.non_retryable import NonRetryableAgentError
from claude.docker_runner import ACTIVE_CONTAINER_TRACKING_TTL_SECONDS, DockerAgentRunner


def _inspect(stdout: str, returncode: int = 0) -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = ""
    return result


class TestTheKillSwitchRecoversAttributionFromDockerLabels:
    """The Redis hash is one source of the project/issue the cancellation flow
    needs, not the only one -- and which branch the endpoint takes decides
    whether the kill counts as an agent failure."""

    def _kill(self, redis_hash, inspect_stdout, inspect_returncode=0):
        """Drive POST /agents/kill/<container> with a given Redis hash and
        `docker inspect` answer, and report which branch it took."""
        from services import observability_server

        redis_client = MagicMock()
        redis_client.hgetall.return_value = redis_hash

        cancelled = []
        killed = []

        def _subprocess_run(cmd, *args, **kwargs):
            if cmd[:2] == ['docker', 'inspect']:
                return _inspect(inspect_stdout, inspect_returncode)
            killed.append(cmd)
            return _inspect("", 0)

        with patch.object(observability_server.redis, 'Redis', return_value=redis_client), \
             patch.object(observability_server.subprocess, 'run', side_effect=_subprocess_run), \
             patch('services.cancellation.cancel_issue_work',
                   side_effect=lambda p, i, r: cancelled.append((p, i))):
            client = observability_server.app.test_client()
            response = client.post('/agents/kill/claude-agent-context-studio-task-9')

        return response, cancelled, killed

    def test_an_expired_tracking_key_still_takes_the_cancellation_branch(self):
        """THE regression. 2h10m into a 3h senior_software_engineer run the hash
        is gone; before this, the endpoint dropped to a bare `docker rm -f`, set
        no cancellation signal, and the resulting 137 was recorded as an agent
        failure."""
        response, cancelled, killed = self._kill(
            redis_hash={},
            inspect_stdout="context-studio|417",
        )

        assert response.status_code == 200
        assert cancelled == [("context-studio", 417)]
        assert killed == [], "the cancellation flow kills the container itself"
        assert response.get_json()['issue_number'] == 417

    def test_an_unreadable_redis_still_takes_the_cancellation_branch(self):
        from services import observability_server

        cancelled = []

        def _subprocess_run(cmd, *args, **kwargs):
            assert cmd[:2] == ['docker', 'inspect']
            return _inspect("context-studio|417")

        with patch.object(observability_server.redis, 'Redis',
                          side_effect=ConnectionError("redis down")), \
             patch.object(observability_server.subprocess, 'run', side_effect=_subprocess_run), \
             patch('services.cancellation.cancel_issue_work',
                   side_effect=lambda p, i, r: cancelled.append((p, i))):
            client = observability_server.app.test_client()
            response = client.post('/agents/kill/claude-agent-context-studio-task-9')

        assert response.status_code == 200
        assert cancelled == [("context-studio", 417)]

    def test_redis_still_wins_when_it_has_the_answer(self):
        """The labels are the fallback, not a replacement -- no extra
        `docker inspect` on the ordinary path."""
        response, cancelled, killed = self._kill(
            redis_hash={'project': 'context-studio', 'issue_number': '417'},
            inspect_stdout="should-not-be-read|999",
        )

        assert response.status_code == 200
        assert cancelled == [("context-studio", 417)]

    def test_a_container_with_no_issue_label_falls_through_unattributed(self):
        """A project-scoped dispatch carries no issue_number label at all. There
        is nothing to cancel, so the bare kill is still the right answer -- it
        just must not be reached while an issue IS attributable."""
        response, cancelled, killed = self._kill(
            redis_hash={},
            inspect_stdout="context-studio|<no value>",
        )

        assert response.status_code == 200
        assert cancelled == []
        assert killed and killed[0][:3] == ['docker', 'rm', '-f']

    def test_an_uninspectable_container_falls_through_unattributed(self):
        response, cancelled, killed = self._kill(
            redis_hash={},
            inspect_stdout="",
            inspect_returncode=1,
        )

        assert response.status_code == 200
        assert cancelled == []
        assert killed and killed[0][:3] == ['docker', 'rm', '-f']


class TestTheTrackingKeyOutlastsTheLongestAgent:

    def test_the_ttl_exceeds_the_longest_configured_agent_timeout(self):
        """The hash must not be able to expire under a live container -- that is
        what made the kill switch's attribution a matter of luck."""
        import yaml

        with open('config/foundations/agents.yaml') as f:
            agents = yaml.safe_load(f)['agents']

        longest = max(
            agent.get('timeout', 0) for agent in agents.values() if isinstance(agent, dict)
        )

        assert longest > 0, "test setup: no agent timeouts found"
        assert ACTIVE_CONTAINER_TRACKING_TTL_SECONDS > longest


class TestTheOrchestratorsOwnKillStaysRetryable:
    """docker_runner kills a container that outlived _CLEANUP_GRACE_SECONDS
    after end_turn. When the captured turn was not clean it deliberately leaves
    exit_code at 137 so "the cycle retries" -- which only works if the exception
    is retryable."""

    def test_a_grace_period_kill_raises_a_retryable_exception(self):
        runner = DockerAgentRunner()

        with pytest.raises(Exception) as excinfo:
            runner._raise_for_failed_exit_code(137, "stderr", orchestrator_killed=True)

        assert not isinstance(excinfo.value, NonRetryableAgentError)
        assert "Retryable" in str(excinfo.value)

    def test_an_external_137_is_still_non_retryable(self):
        """An OOM kill or an operator kill reproduces on every run; #160's whole
        point is that it must not be relaunched."""
        runner = DockerAgentRunner()

        with pytest.raises(NonRetryableAgentError):
            runner._raise_for_failed_exit_code(137, "OOM")

    def test_an_external_143_is_still_non_retryable(self):
        runner = DockerAgentRunner()

        with pytest.raises(NonRetryableAgentError):
            runner._raise_for_failed_exit_code(143, "SIGTERM")

    def test_an_ordinary_non_zero_exit_is_unaffected(self):
        runner = DockerAgentRunner()

        with pytest.raises(Exception) as excinfo:
            runner._raise_for_failed_exit_code(1, "tests failed")

        assert not isinstance(excinfo.value, NonRetryableAgentError)
