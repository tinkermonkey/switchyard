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

from datetime import datetime, timedelta

from services.dev_container_state import (
    PENDING_OPERATION_MAX_AGE_SECONDS,
    DevContainerStateManager,
    DevContainerStatus,
)



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


def _age_marker(manager, project, seconds):
    """Backdate the marker's timestamp, as an abandoned one would be."""
    import yaml

    state_file = manager.get_state_file(project)
    state = yaml.safe_load(state_file.read_text())
    state['pending_operation_at'] = (
        datetime.now() - timedelta(seconds=seconds)
    ).isoformat()
    state_file.write_text(yaml.dump(state, default_flow_style=False))


class TestAbandonedMarkersAgeOut:
    """#152 review: the marker's only writer is /api/projects/<p>/rebuild-image's
    worker, which is a DAEMON thread and is killed outright at interpreter
    shutdown. A SIGTERM or restart during its up-to-900s lock wait leaves the
    marker on disk with nobody coming back to clear it, and every consumer --
    get_image_build_status, /api/projects -- then reports a rebuild that will
    never start, forever, masking the project's real image state."""

    def test_a_marker_past_its_maximum_age_reads_as_absent(self, manager):
        manager.set_status("proj", DevContainerStatus.VERIFIED)
        manager.set_pending_operation("proj", "rebuild")
        _age_marker(manager, "proj", PENDING_OPERATION_MAX_AGE_SECONDS + 60)

        assert manager.get_pending_operation("proj") is None
        # The image's own state is untouched by the marker aging out.
        assert manager.get_status("proj") == DevContainerStatus.VERIFIED

    def test_a_marker_inside_its_maximum_age_is_still_reported(self, manager):
        """The bound must clear the abandoned ones without cutting short a
        rebuild that is legitimately still waiting for the lock."""
        manager.set_pending_operation("proj", "rebuild")
        _age_marker(manager, "proj", PENDING_OPERATION_MAX_AGE_SECONDS - 60)

        assert manager.get_pending_operation("proj")['operation'] == "rebuild"

    def test_the_bound_outlasts_the_wait_that_can_produce_a_marker(self):
        """The only thing that keeps a marker alive legitimately is the rebuild
        endpoint's lock wait, so the bound has to be longer than it."""
        from services import observability_server

        assert (
            PENDING_OPERATION_MAX_AGE_SECONDS
            > observability_server.REBUILD_ENDPOINT_LOCK_TIMEOUT_SECONDS
        )

    def test_a_marker_with_no_timestamp_is_not_immortal(self, manager):
        import yaml

        state_file = manager.get_state_file("proj")
        state_file.write_text(yaml.dump({'status': 'verified', 'pending_operation': 'rebuild'}))

        assert manager.get_pending_operation("proj") is None

    def test_stale_markers_are_swept_off_disk(self, manager):
        """The read-side bound stops one being REPORTED; only this removes it,
        which is why start_observability_server calls it -- that process is the
        marker's only writer and a compose singleton, so any stale one present at
        its startup is its own dead predecessor's."""
        import yaml

        manager.set_status("stale", DevContainerStatus.VERIFIED)
        manager.set_pending_operation("stale", "rebuild")
        _age_marker(manager, "stale", PENDING_OPERATION_MAX_AGE_SECONDS + 60)
        manager.set_status("live", DevContainerStatus.VERIFIED)
        manager.set_pending_operation("live", "rebuild")

        assert manager.clear_stale_pending_operations() == 1

        assert 'pending_operation' not in yaml.safe_load(
            manager.get_state_file("stale").read_text()
        )
        assert manager.get_status("stale") == DevContainerStatus.VERIFIED
        # A live wait is left alone.
        assert manager.get_pending_operation("live")['operation'] == "rebuild"


class TestLastOperationError:
    """The channel a dropped rebuild is recorded through. Deliberately not a
    status: lock contention is not a verdict on the image, and BLOCKED is
    terminal (validate_task_can_run gives the terminal statuses no staleness
    escape), so recording a drop as BLOCKED would refuse every task for the
    project until a human intervened (#152 review)."""

    def test_absent_by_default(self, manager):
        manager.set_status("proj", DevContainerStatus.VERIFIED)
        assert manager.get_last_operation_error("proj") is None

    def test_records_the_message_without_touching_the_status(self, manager):
        manager.set_status("proj", DevContainerStatus.UNVERIFIED)
        updated_at = manager.get_status_updated_at("proj")

        manager.set_last_operation_error("proj", "rebuild never started")

        recorded = manager.get_last_operation_error("proj")
        assert recorded['error'] == "rebuild never started"
        assert recorded['at']
        assert manager.get_status("proj") == DevContainerStatus.UNVERIFIED
        assert manager.get_status_updated_at("proj") == updated_at

    def test_clearing_removes_it(self, manager):
        manager.set_status("proj", DevContainerStatus.VERIFIED)
        manager.set_last_operation_error("proj", "rebuild never started")

        manager.clear_last_operation_error("proj")

        assert manager.get_last_operation_error("proj") is None
        assert manager.get_status("proj") == DevContainerStatus.VERIFIED


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

    def test_an_abandoned_marker_does_not_mask_the_real_status_forever(self, manager):
        """#152 review: this tool's docstring tells the calling agent that
        "queued" means a rebuild is waiting, so a marker left behind by a killed
        daemon thread reported a rebuild that would never start on every poll --
        for a project whose image is actually fine."""
        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")
        manager.set_pending_operation("proj", "rebuild")
        _age_marker(manager, "proj", PENDING_OPERATION_MAX_AGE_SECONDS + 60)

        result = self._status(manager, "proj")

        assert result['status'] == "verified"
        assert result['pending_operation'] is None

    def test_a_dropped_rebuild_is_surfaced_without_being_a_status(self, manager):
        """A rebuild that never got the lock has to reach the caller somehow --
        it is why the endpoint records anything at all -- but not by overwriting
        the image's own state."""
        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")
        manager.set_last_operation_error("proj", "rebuild never started: lock held")

        result = self._status(manager, "proj")

        assert result['status'] == "verified"
        assert result['last_operation_error']['error'] == "rebuild never started: lock held"
