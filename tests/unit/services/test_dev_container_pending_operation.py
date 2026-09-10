"""
Tests for DevContainerStateManager's pending-operation marker and the
get_image_build_status MCP tool that surfaces it (#152 WI-7 review).

/api/projects/<p>/rebuild-image answers {"success": true, "triggered": true}
immediately and its worker thread then waits up to 900s for the
dev_container_build lock, marking IN_PROGRESS only once it has it. The state
file is that request's only feedback channel -- mcp/server.py's
get_image_build_status reads it directly -- so for the whole wait a caller
polling it read the project's PRE-EXISTING status, commonly 'verified', and
concluded the rebuild had already finished. The marker gives that wait its own
representation instead of leaving the previous status to speak for it.
"""
import os
import tempfile

import pytest

# services.dev_container_state builds a module-level singleton on import that
# defaults to ORCHESTRATOR_ROOT (or /app) for its state dir -- see
# test_dev_container_state_updated_at.py for why this is set before the import.
os.environ.setdefault('ORCHESTRATOR_ROOT', tempfile.mkdtemp(prefix='dev_container_state_test_'))

from services.dev_container_state import DevContainerStateManager, DevContainerStatus



def _import_mcp_server():
    """
    Import mcp/server.py under its bare `server` name, with sys.path mutated only
    for the duration of the import -- see
    tests/unit/test_mcp_server_board_scoped_lookup.py for why leaving those
    entries in place pollutes later collection, and why mcp/ deliberately has no
    __init__.py (it would shadow the installed `mcp` SDK).
    """
    import sys

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..'))
    mcp_dir = os.path.join(repo_root, 'mcp')
    sys.path.insert(0, repo_root)
    sys.path.insert(0, mcp_dir)
    try:
        import server as mcp_server
        return mcp_server
    finally:
        sys.path.remove(mcp_dir)
        sys.path.remove(repo_root)


@pytest.fixture
def manager(tmp_path):
    return DevContainerStateManager(state_dir=tmp_path)


class TestPendingOperationMarker:

    def test_absent_by_default(self, manager):
        assert manager.get_pending_operation("proj") is None
        manager.set_status("proj", DevContainerStatus.VERIFIED)
        assert manager.get_pending_operation("proj") is None

    def test_records_the_operation_and_when_it_was_requested(self, manager):
        manager.set_status("proj", DevContainerStatus.VERIFIED)
        manager.set_pending_operation("proj", "rebuild")

        pending = manager.get_pending_operation("proj")
        assert pending['operation'] == "rebuild"
        assert pending['requested_at']

    def test_does_not_disturb_the_status_it_is_waiting_on(self, manager):
        """THE point of the marker: the image really is still verified while a
        rebuild waits for the lock, so `status` must not be repurposed to say
        otherwise -- every `status ==` branch in the codebase would have to learn
        about a value that is not a state of the image."""
        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")
        updated_at = manager.get_status_updated_at("proj")

        manager.set_pending_operation("proj", "rebuild")

        assert manager.get_status("proj") == DevContainerStatus.VERIFIED
        assert manager.get_image_name("proj") == "proj-agent:latest"
        assert manager.get_status_updated_at("proj") == updated_at

    def test_clearing_removes_it(self, manager):
        manager.set_status("proj", DevContainerStatus.VERIFIED)
        manager.set_pending_operation("proj", "rebuild")

        manager.clear_pending_operation("proj")

        assert manager.get_pending_operation("proj") is None
        assert manager.get_status("proj") == DevContainerStatus.VERIFIED

    def test_clearing_when_nothing_is_pending_is_harmless(self, manager):
        """The endpoint clears in a finally block, so the happy path clears
        twice."""
        manager.set_status("proj", DevContainerStatus.VERIFIED)
        manager.clear_pending_operation("proj")
        manager.clear_pending_operation("proj")

        assert manager.get_pending_operation("proj") is None
        assert manager.get_status("proj") == DevContainerStatus.VERIFIED

    def test_a_marker_survives_a_later_status_write(self, manager):
        """set_status merges rather than rewrites, so an unrelated write by
        another owner does not silently drop a pending request."""
        manager.set_pending_operation("proj", "rebuild")
        manager.set_status("proj", DevContainerStatus.UNVERIFIED)

        assert manager.get_pending_operation("proj")['operation'] == "rebuild"


class TestGetImageBuildStatusSurfacesTheWait:
    """The reader half. Without it the marker is written and never seen."""

    def _status(self, manager, project):
        from unittest.mock import patch

        mcp_server = _import_mcp_server()
        with patch.object(mcp_server, '_resolve_project_name', side_effect=lambda p: p), \
             patch('services.dev_container_state.dev_container_state', manager):
            return mcp_server.get_image_build_status(project)

    def test_reports_queued_while_a_rebuild_waits(self, manager):
        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")
        manager.set_pending_operation("proj", "rebuild")

        result = self._status(manager, "proj")

        assert result['status'] == "queued"
        # The image's own state stays available and stays honest.
        assert result['image_status'] == "verified"
        assert result['pending_operation']['operation'] == "rebuild"

    def test_reports_the_real_status_once_the_build_owns_it(self, manager):
        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")
        manager.set_pending_operation("proj", "rebuild")
        manager.clear_pending_operation("proj")
        manager.set_status("proj", DevContainerStatus.IN_PROGRESS)

        result = self._status(manager, "proj")

        assert result['status'] == "in_progress"
        assert result['pending_operation'] is None

    def test_is_unchanged_for_a_project_with_nothing_pending(self, manager):
        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")

        result = self._status(manager, "proj")

        assert result['status'] == "verified"
        assert result['image_name'] == "proj-agent:latest"
