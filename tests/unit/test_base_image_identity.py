"""
#251: nothing verified that `switchyard-orchestrator:latest` was the orchestrator.

`switchyard-orchestrator` is exactly the image name `docker compose` generates
for a service called `orchestrator` in a project called `switchyard` -- and
other compose files in this workspace define a service by that name too. On
2026-09-15 a managed project's compose build took the tag. For seven hours every
agent that runs in the base image launched a different project's application
image and died on:

    FileNotFoundError: [Errno 2] No such file or directory: 'claude'

which names neither the image nor the tag. Meanwhile the genuine orchestrator
image was left with no tags at all, alive only because the running container
pinned it by id.

The identical failure mode has been guarded for PROJECT images since #17
(697685d), using this same label; #199 later split out _probe_image() to tell
"Docker says no" apart from "Docker did not answer". The base image -- shared by
every project, and the FROM of every Dockerfile.agent -- was the one image with
no identity check.
"""

import subprocess
from unittest.mock import Mock, patch

import pytest

from claude.docker_runner import (
    ORCHESTRATOR_BASE_IMAGE,
    BaseImageIdentityError,
    DockerAgentRunner,
)


def _inspect(returncode=0, stdout="true", stderr=""):
    r = Mock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestTheBaseImageMustBeOurs:
    def test_an_image_without_our_label_is_refused(self):
        """The production shape: the tag resolved to a project's application
        image, which carried no labels at all."""
        with patch('claude.docker_runner.subprocess.run', return_value=_inspect(stdout="")):
            with pytest.raises(BaseImageIdentityError) as exc:
                DockerAgentRunner._assert_base_image_is_ours(ORCHESTRATOR_BASE_IMAGE)

        message = str(exc.value)
        assert 'not built by switchyard' in message
        assert 'orchestrator' in message, "the message must name the colliding service"

    def test_our_own_image_is_accepted(self):
        """Control: the guard must not ground every agent."""
        with patch('claude.docker_runner.subprocess.run', return_value=_inspect(stdout="true")):
            DockerAgentRunner._assert_base_image_is_ours(ORCHESTRATOR_BASE_IMAGE)

    def test_a_missing_image_is_refused(self):
        with patch('claude.docker_runner.subprocess.run',
                   return_value=_inspect(returncode=1, stderr="No such image")):
            with pytest.raises(BaseImageIdentityError):
                DockerAgentRunner._assert_base_image_is_ours(ORCHESTRATOR_BASE_IMAGE)

    def test_a_probe_timeout_does_not_ground_agents(self):
        """A docker hiccup is not evidence of a bad image. Failing closed here
        would turn a transient daemon blip into a total dispatch outage, which
        is worse than the collision this guards against -- that one at least
        announces itself."""
        with patch('claude.docker_runner.subprocess.run',
                   side_effect=subprocess.TimeoutExpired(cmd='docker', timeout=10)):
            DockerAgentRunner._assert_base_image_is_ours(ORCHESTRATOR_BASE_IMAGE)

    @pytest.mark.parametrize('stderr', [
        # Measured against this host's docker, not invented: every one of these
        # exits 1, exactly like "No such image".
        'Cannot connect to the Docker daemon at unix:///var/run/docker.sock. '
        'Is the docker daemon running?',
        'failed to connect to the docker API at unix:///tmp/nope.sock; check if '
        'the path is correct and if the daemon is running',
        'permission denied while trying to connect to the Docker daemon socket',
    ])
    def test_an_unreachable_daemon_does_not_ground_agents(self, stderr):
        """The bug the first draft of this guard shipped with: an unreachable
        daemon exits 1 and was read as "the image is absent", which both
        misdiagnoses the fault and grounds every agent on a blip -- the exact
        outage the fail-open was chosen to avoid. rc alone cannot tell the two
        apart; only stderr can."""
        with patch('claude.docker_runner.subprocess.run',
                   return_value=_inspect(returncode=1, stderr=stderr)):
            DockerAgentRunner._assert_base_image_is_ours(ORCHESTRATOR_BASE_IMAGE)

    def test_the_probe_asks_about_our_label_on_the_named_image(self):
        """Without this, the guard can ask the wrong question entirely -- a
        typo'd label key returns empty for EVERY image, which reads as "not
        ours" and grounds everything, and an image name that ignores the
        argument silently verifies the wrong tag."""
        from services.dev_container_state import SWITCHYARD_AGENT_ENV_LABEL

        with patch('claude.docker_runner.subprocess.run',
                   return_value=_inspect(stdout="true")) as run:
            DockerAgentRunner._assert_base_image_is_ours('some-image:v9')

        argv = run.call_args[0][0]
        assert argv[:3] == ['docker', 'image', 'inspect']
        assert 'some-image:v9' in argv, "the guard must probe the image it was given"
        assert any(SWITCHYARD_AGENT_ENV_LABEL in str(a) for a in argv), (
            "the guard must ask for the label the Dockerfile actually sets"
        )

    def test_the_refusal_is_not_retryable(self):
        """A wrong image on disk is permanent. As a bare RuntimeError this was
        retried 3 x 4 times and opened the per-agent circuit breaker, whose
        generic "Circuit is open" then replaced the diagnosis -- the whole
        point of the guard. Every retry loop keys off this one type."""
        from utils.non_retryable import NonRetryableAgentError

        assert issubclass(BaseImageIdentityError, NonRetryableAgentError)

    def test_the_label_is_the_one_the_dockerfile_sets(self):
        """The guard and the Dockerfile must agree, or it passes vacuously."""
        from pathlib import Path
        from services.dev_container_state import SWITCHYARD_AGENT_ENV_LABEL

        dockerfile = Path(__file__).resolve().parents[2] / 'Dockerfile'
        assert f'LABEL {SWITCHYARD_AGENT_ENV_LABEL}="true"' in dockerfile.read_text(), (
            "the base image no longer carries the label this guard checks"
        )


class TestTheFallbackPathIsGuarded:
    def test_choosing_the_base_image_verifies_it(self):
        """The guard has to sit on the path that actually returns the base
        image, not merely exist."""
        runner = DockerAgentRunner.__new__(DockerAgentRunner)

        from config.manager import ConfigurationError

        with patch.object(DockerAgentRunner, '_assert_base_image_is_ours') as guard, \
             patch('config.manager.config_manager.get_project_agent_config',
                   side_effect=ConfigurationError("no project config")):
            image = runner._get_image_for_agent('work_breakdown_agent', 'any-project')

        assert image == ORCHESTRATOR_BASE_IMAGE
        guard.assert_called_once_with(ORCHESTRATOR_BASE_IMAGE)


class TestTheRepairCycleLaunchIsGuardedToo:
    """#251's guard covered agent launches only.

    `_launch_repair_cycle_container` runs `python -m pipeline.repair_cycle_runner`
    -- a module that exists ONLY in our image -- from the same base tag, via a
    third spelling of it (`"switchyard-orchestrator"`, no `:latest`) that
    bypassed `_get_image_for_agent` entirely. Under the exact #251 conditions it
    died on `No module named pipeline.repair_cycle_runner`, naming neither the
    image nor the tag: the failure this issue is about, on a path the first draft
    of the fix left uncovered.
    """

    def test_it_uses_the_same_constant_rather_than_a_fourth_literal(self):
        import inspect
        from services import project_monitor

        src = inspect.getsource(project_monitor._launch_repair_cycle_container)
        assert 'repair_cycle_image = ORCHESTRATOR_BASE_IMAGE' in src, (
            "the repair cycle must not re-spell the base tag; the two drifted once already"
        )

    def test_a_hijacked_tag_refuses_the_launch_instead_of_running_it(self):
        from services import project_monitor

        with patch.object(DockerAgentRunner, '_assert_base_image_is_ours',
                          side_effect=BaseImageIdentityError("not ours")) as guard, \
             patch('subprocess.run') as run:
            result = project_monitor._launch_repair_cycle_container(
                project_name='any-project',
                issue_number=1,
                pipeline_run_id='run-1',
                stage_name='Development',
                context_file='/tmp/ctx.json',
                project_dir='/workspace/any-project',
            )

        assert result is None, "a refused launch must report failure, not a container name"
        assert guard.called, "the repair cycle must verify the base image before launching"
        assert not run.called, "nothing may be launched from an unverified base tag"
