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

from claude.docker_runner import DockerAgentRunner


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

        assert image == 'switchyard-orchestrator:latest'

    def test_uses_orchestrator_image_when_dev_container_not_required(self):
        runner = DockerAgentRunner()

        with patch('config.manager.config_manager.get_project_agent_config',
                   return_value=_agent_config(False)):
            image = runner._get_image_for_agent('code_reviewer', 'phone-home')

        assert image == 'switchyard-orchestrator:latest'
