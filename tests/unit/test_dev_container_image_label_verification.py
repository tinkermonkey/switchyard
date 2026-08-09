"""
Test that DevContainerStateManager verifies image *identity* (via the
SWITCHYARD_AGENT_ENV_LABEL docker label), not just tag existence.

Regression coverage for the incident where phone-home's own "agent"
docker-compose service (an unrelated microservice, built FROM python:3.12-slim)
was tagged phone-home-agent:latest by Docker Compose's default
<compose-project>-<service> naming — silently overwriting the tag switchyard's
orchestrator relies on for the project's Claude-Code agent environment.
`docker image inspect <tag>` succeeded throughout (the tag existed), so the
old existence-only check never caught it; every senior_software_engineer
container launch booted the wrong entrypoint and failed on a missing
AGENT_BEARER_TOKEN that was never switchyard's to set.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

_temp_root = tempfile.mkdtemp()
os.environ.setdefault('ORCHESTRATOR_ROOT', _temp_root)

try:
    from services.dev_container_state import (
        DevContainerStateManager,
        DevContainerStatus,
        SWITCHYARD_AGENT_ENV_LABEL,
    )
except ImportError:
    pytest.skip("Requires Docker container environment", allow_module_level=True)


@pytest.fixture
def dev_container_mgr():
    with tempfile.TemporaryDirectory() as state_dir:
        yield DevContainerStateManager(state_dir=Path(state_dir))


def _mock_inspect(returncode: int, label_value: str):
    return Mock(returncode=returncode, stdout=label_value, stderr='')


class TestVerifyImageExists:
    def test_true_when_image_carries_switchyard_label(self, dev_container_mgr):
        dev_container_mgr.set_status(
            "phone-home", DevContainerStatus.VERIFIED, image_name="phone-home-agent:latest"
        )

        with patch('subprocess.run', return_value=_mock_inspect(0, "true\n")) as mock_run:
            assert dev_container_mgr.verify_image_exists("phone-home") is True

        args = mock_run.call_args[0][0]
        assert args[:3] == ['docker', 'image', 'inspect']
        assert any(SWITCHYARD_AGENT_ENV_LABEL in a for a in args)

    def test_false_when_tag_exists_but_label_missing(self, dev_container_mgr):
        """The exact hijack scenario: the tag resolves, but to a foreign image."""
        dev_container_mgr.set_status(
            "phone-home", DevContainerStatus.VERIFIED, image_name="phone-home-agent:latest"
        )

        # `docker image inspect --format` on an image with no such label
        # prints the Go template's zero value: an empty string.
        with patch('subprocess.run', return_value=_mock_inspect(0, "\n")):
            assert dev_container_mgr.verify_image_exists("phone-home") is False

    def test_false_when_label_explicitly_not_true(self, dev_container_mgr):
        dev_container_mgr.set_status(
            "phone-home", DevContainerStatus.VERIFIED, image_name="phone-home-agent:latest"
        )

        with patch('subprocess.run', return_value=_mock_inspect(0, "false\n")):
            assert dev_container_mgr.verify_image_exists("phone-home") is False

    def test_false_when_image_missing_entirely(self, dev_container_mgr):
        dev_container_mgr.set_status(
            "phone-home", DevContainerStatus.VERIFIED, image_name="phone-home-agent:latest"
        )

        with patch('subprocess.run', return_value=_mock_inspect(1, "")):
            assert dev_container_mgr.verify_image_exists("phone-home") is False

    def test_false_when_no_image_name_recorded(self, dev_container_mgr):
        assert dev_container_mgr.verify_image_exists("never-built-project") is False


class TestVerifyAndUpdateStatus:
    def test_resets_to_unverified_on_label_mismatch(self, dev_container_mgr):
        """Even though the tag exists, a label mismatch must force a rebuild
        rather than let the pipeline keep launching the wrong container."""
        dev_container_mgr.set_status(
            "phone-home", DevContainerStatus.VERIFIED, image_name="phone-home-agent:latest"
        )

        with patch('subprocess.run', return_value=_mock_inspect(0, "\n")):
            result = dev_container_mgr.verify_and_update_status("phone-home")

        assert result is False
        assert dev_container_mgr.get_status("phone-home") == DevContainerStatus.UNVERIFIED

    def test_stays_verified_when_label_present(self, dev_container_mgr):
        dev_container_mgr.set_status(
            "phone-home", DevContainerStatus.VERIFIED, image_name="phone-home-agent:latest"
        )

        with patch('subprocess.run', return_value=_mock_inspect(0, "true\n")):
            result = dev_container_mgr.verify_and_update_status("phone-home")

        assert result is True
        assert dev_container_mgr.get_status("phone-home") == DevContainerStatus.VERIFIED
