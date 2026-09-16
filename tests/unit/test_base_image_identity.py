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

The identical failure mode has been guarded for PROJECT images since #199, using
this same label. The base image -- shared by every project, and the FROM of every
Dockerfile.agent -- was the one image with no identity check.
"""

import os
from unittest.mock import Mock, patch

import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

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

    def test_an_unanswerable_probe_does_not_ground_agents(self):
        """A docker hiccup is not evidence of a bad image. Failing closed here
        would turn a transient daemon blip into a total dispatch outage, which
        is worse than the collision this guards against -- that one at least
        announces itself."""
        with patch('claude.docker_runner.subprocess.run',
                   side_effect=RuntimeError("docker daemon unreachable")):
            DockerAgentRunner._assert_base_image_is_ours(ORCHESTRATOR_BASE_IMAGE)

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
