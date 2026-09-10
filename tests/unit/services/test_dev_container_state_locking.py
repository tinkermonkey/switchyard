"""
Tests that every writer of state/dev_containers/<project>.yaml is serialised by
the state file's own lock (#152 WI-7 review).

The rest of this work item's premise is "every writer of this file is inside the
dev_container_build lock". That is not true of this file and cannot be made
true: the pending-operation marker is deliberately set from the rebuild
endpoint's REQUEST thread, before the lock wait even starts, precisely so it is
visible to the first poll -- and the endpoint's `finally` clear runs after that
lock has been released, at the exact moment the next waiter is acquiring it and
writing IN_PROGRESS.

Both of those go through _merge_state(), a whole-document read-modify-write. Un-
serialised, either can land on top of a set_status() made in between and carry
it backwards -- a freshly VERIFIED image reading back as UNVERIFIED, which makes
validate_task_can_run refuse every task for the project and queue a redundant
full dev_environment_setup run. The writers are in different CONTAINERS (the
observability server and the orchestrator), so the serialisation has to be the
file lock rather than anything in-process.
"""
import os
import tempfile
import threading
import time

import pytest

# services.dev_container_state builds a module-level singleton on import that
# defaults to ORCHESTRATOR_ROOT (or /app) for its state dir -- see
# test_dev_container_state_updated_at.py for why this is set before the import.
os.environ.setdefault('ORCHESTRATOR_ROOT', tempfile.mkdtemp(prefix='dev_container_state_test_'))

import services.dev_container_state as dev_container_state_module
from services.dev_container_state import DevContainerStateManager, DevContainerStatus
from utils.file_lock import file_lock


@pytest.fixture
def manager(tmp_path):
    return DevContainerStateManager(state_dir=tmp_path)


class _SlowDump:
    """yaml stand-in that stalls between a caller's read and its write.

    Widens the read-modify-write window that the lost update lives in, so the
    race is exercised deterministically instead of being hunted for.
    """

    def __init__(self, real, delay):
        self._real = real
        self._delay = delay
        self.stalled = threading.Event()

    def safe_load(self, stream):
        return self._real.safe_load(stream)

    def dump(self, data, stream=None, **kwargs):
        self.stalled.set()
        time.sleep(self._delay)
        return self._real.dump(data, stream, **kwargs)


class TestWritersAreSerialised:

    def test_a_marker_write_cannot_carry_a_concurrent_status_write_backwards(
        self, manager, monkeypatch
    ):
        """THE lost update: the request thread reads {status: unverified}, the
        setup agent finishes and writes VERIFIED in the gap, and the request
        thread's whole-document dump restores 'unverified' over it."""
        manager.set_status("proj", DevContainerStatus.UNVERIFIED)

        slow = _SlowDump(dev_container_state_module.yaml, delay=0.4)
        monkeypatch.setattr(dev_container_state_module, 'yaml', slow)

        marker = threading.Thread(
            target=manager.set_pending_operation, args=("proj", "rebuild")
        )
        marker.start()
        assert slow.stalled.wait(timeout=5), "marker write never reached its dump"

        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")
        marker.join(timeout=5)

        assert manager.get_status("proj") == DevContainerStatus.VERIFIED
        assert manager.get_image_name("proj") == "proj-agent:latest"
        # And the write that had to wait is not lost either.
        assert manager.get_pending_operation("proj")['operation'] == "rebuild"

    def test_the_finally_clear_cannot_revert_the_next_lock_holder(
        self, manager, monkeypatch
    ):
        """Same race, the endpoint's other unlocked writer: clear_pending_operation
        runs in the worker's `finally`, AFTER the dev_container_build lock is
        released -- inside the ~5s poll interval in which the next waiter acquires
        it and writes IN_PROGRESS."""
        manager.set_status("proj", DevContainerStatus.BLOCKED, error_message="build failed")
        manager.set_pending_operation("proj", "rebuild")

        slow = _SlowDump(dev_container_state_module.yaml, delay=0.4)
        monkeypatch.setattr(dev_container_state_module, 'yaml', slow)

        clearer = threading.Thread(target=manager.clear_pending_operation, args=("proj",))
        clearer.start()
        assert slow.stalled.wait(timeout=5), "clear never reached its dump"

        manager.set_status("proj", DevContainerStatus.IN_PROGRESS)
        clearer.join(timeout=5)

        assert manager.get_status("proj") == DevContainerStatus.IN_PROGRESS
        assert manager.get_pending_operation("proj") is None

    def test_writes_wait_on_the_state_files_own_lock(self, manager):
        """The serialisation is the file lock, not anything in-process: the
        writers are in different containers, sharing only the bind mount."""
        manager.set_status("proj", DevContainerStatus.UNVERIFIED)
        lock_file = DevContainerStateManager._state_lock_file(manager.get_state_file("proj"))

        done = threading.Event()

        def _write():
            manager.set_status("proj", DevContainerStatus.VERIFIED)
            done.set()

        with file_lock(lock_file):
            writer = threading.Thread(target=_write)
            writer.start()
            time.sleep(0.3)
            assert not done.is_set(), "write proceeded while the state file was locked"

        writer.join(timeout=5)
        assert done.is_set()
        assert manager.get_status("proj") == DevContainerStatus.VERIFIED

    def test_a_read_never_sees_a_half_written_file(self, manager, monkeypatch):
        """Reads take the same lock, so a reader landing mid-dump blocks rather
        than parsing a truncated document and reporting UNVERIFIED."""
        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")

        slow = _SlowDump(dev_container_state_module.yaml, delay=0.4)
        monkeypatch.setattr(dev_container_state_module, 'yaml', slow)

        writer = threading.Thread(
            target=manager.set_pending_operation, args=("proj", "rebuild")
        )
        writer.start()
        assert slow.stalled.wait(timeout=5), "write never reached its dump"

        assert manager.get_status("proj") == DevContainerStatus.VERIFIED
        writer.join(timeout=5)
