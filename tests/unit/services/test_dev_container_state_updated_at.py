"""
Tests for DevContainerStateManager.get_status_updated_at, added to support the
staleness fallback in agents.orchestrator_integration.validate_task_can_run
(see test_orchestrator_integration_ux.py for that side of the fix).
"""
import os
import tempfile
from datetime import datetime

import pytest

# services.dev_container_state builds a module-level singleton on import that
# defaults to ORCHESTRATOR_ROOT (or /app) for its state dir. Outside Docker
# that path isn't writable, which would otherwise fail collection for every
# test in this file. Point it at a throwaway temp dir before importing.
os.environ.setdefault('ORCHESTRATOR_ROOT', tempfile.mkdtemp(prefix='dev_container_state_test_'))

from services.dev_container_state import DevContainerStateManager, DevContainerStatus


@pytest.fixture
def manager(tmp_path):
    return DevContainerStateManager(state_dir=tmp_path)


class TestGetStatusUpdatedAt:
    def test_returns_none_for_unknown_project(self, manager):
        assert manager.get_status_updated_at("nonexistent") is None

    def test_returns_parsed_timestamp_after_set_status(self, manager):
        before = datetime.now()
        manager.set_status("proj", DevContainerStatus.IN_PROGRESS)
        after = datetime.now()

        updated_at = manager.get_status_updated_at("proj")
        assert updated_at is not None
        assert before <= updated_at <= after

    def test_updates_on_subsequent_set_status_calls(self, manager):
        manager.set_status("proj", DevContainerStatus.IN_PROGRESS)
        first = manager.get_status_updated_at("proj")

        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")
        second = manager.get_status_updated_at("proj")

        assert second >= first

    def test_returns_none_for_corrupt_state_file(self, manager, tmp_path):
        (tmp_path / "broken.yaml").write_text("{unclosed: [1, 2")
        assert manager.get_status_updated_at("broken") is None

    def test_returns_none_when_updated_at_missing(self, manager, tmp_path):
        (tmp_path / "partial.yaml").write_text("status: in_progress\n")
        assert manager.get_status_updated_at("partial") is None
