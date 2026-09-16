"""
Unit tests for DockerAgentRunner._get_image_for_agent's live dev-container
verification.

Regression coverage for the incident where a project's dev container state
file said VERIFIED (set at a previous orchestrator startup) while, mid-session,
an unrelated build silently overwrote the same-named image tag. Trusting the
cached VERIFIED flag alone meant the orchestrator kept handing out the wrong
image to every agent container launch until the next restart. _get_image_for_agent
must re-verify live (via dev_container_state.verify_and_update_status) at the
moment of each launch, not just trust the cached status.
"""

from unittest.mock import MagicMock, patch

import pytest
from claude.docker_runner import DockerAgentRunner, ORCHESTRATOR_BASE_IMAGE


@pytest.fixture(autouse=True)
def _stub_base_image_guard():
    """Keep these tests hermetic.

    _get_image_for_agent now shells out to `docker image inspect` to verify the
    base tag is ours (#251). These tests are about image SELECTION and command
    WIRING, not image identity, and they pass on a developer box only because it
    happens to have a correctly-labelled image: with docker installed and the
    image absent -- i.e. CI -- all of them fail on a guard they never meant to
    exercise. tests/unit/test_base_image_identity.py owns the guard's behaviour.
    """
    with patch.object(DockerAgentRunner, '_assert_base_image_is_ours'):
        yield


def _agent_config(requires_dev_container=True):
    config = MagicMock()
    config.requires_dev_container = requires_dev_container
    return config


class TestGetImageForAgent:
    def test_uses_project_image_when_verified_live(self):
        runner = DockerAgentRunner()

        with patch('config.manager.config_manager.get_project_agent_config',
                   return_value=_agent_config(True)), \
             patch('services.dev_container_state.dev_container_state.is_verified', return_value=True), \
             patch('services.dev_container_state.dev_container_state.verify_and_update_status', return_value=True), \
             patch('services.dev_container_state.dev_container_state.get_image_name',
                   return_value='phone-home-agent:latest'):
            image = runner._get_image_for_agent('senior_software_engineer', 'phone-home')

        assert image == 'phone-home-agent:latest'

    def test_falls_back_when_cached_verified_but_live_check_fails(self):
        """The tag-hijack case: state file says VERIFIED, but the image behind
        the tag has been swapped out (e.g. an unrelated project/compose service
        overwrote it). The cached flag alone must not be trusted."""
        runner = DockerAgentRunner()

        with patch('config.manager.config_manager.get_project_agent_config',
                   return_value=_agent_config(True)), \
             patch('services.dev_container_state.dev_container_state.is_verified', return_value=True), \
             patch('services.dev_container_state.dev_container_state.verify_and_update_status', return_value=False), \
             patch('services.dev_container_state.dev_container_state.get_status') as mock_get_status, \
             patch('services.dev_container_state.dev_container_state.get_image_name',
                   return_value='phone-home-agent:latest'):
            mock_get_status.return_value.value = 'unverified'
            image = runner._get_image_for_agent('senior_software_engineer', 'phone-home')

        assert image == ORCHESTRATOR_BASE_IMAGE

    def test_uses_orchestrator_image_when_dev_container_not_required(self):
        runner = DockerAgentRunner()

        with patch('config.manager.config_manager.get_project_agent_config',
                   return_value=_agent_config(False)):
            image = runner._get_image_for_agent('code_reviewer', 'phone-home')

        assert image == ORCHESTRATOR_BASE_IMAGE
