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


class TestReadDecideWriteCallersReDecideUnderTheLock:
    """#171. Serialising _merge_state() made each WRITE atomic; it did nothing
    for a caller that read, decided, and only then wrote -- the decision was
    already made on a released lock by the time the write ran."""

    def test_a_status_write_can_be_made_conditional_on_what_is_on_disk(self, manager):
        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")

        # Somebody else moved the project on while our caller was deciding.
        manager.set_status("proj", DevContainerStatus.IN_PROGRESS)

        manager.set_status(
            "proj",
            DevContainerStatus.UNVERIFIED,
            expect={'status': DevContainerStatus.VERIFIED.value},
        )

        assert manager.get_status("proj") == DevContainerStatus.IN_PROGRESS

    def test_the_conditional_write_still_lands_when_nothing_changed(self, manager):
        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")

        manager.set_status(
            "proj",
            DevContainerStatus.UNVERIFIED,
            expect={'status': DevContainerStatus.VERIFIED.value},
        )

        assert manager.get_status("proj") == DevContainerStatus.UNVERIFIED

    def test_an_unconditional_write_is_unaffected(self, manager):
        """Only callers whose decision depends on an earlier read pass
        expect; everything else must keep writing unconditionally."""
        manager.set_status("proj", DevContainerStatus.IN_PROGRESS)
        manager.set_status("proj", DevContainerStatus.BLOCKED, error_message="nope")

        assert manager.get_status("proj") == DevContainerStatus.BLOCKED

    def test_the_precondition_is_evaluated_inside_the_lock(self, manager, monkeypatch):
        """THE regression. A caller-side check -- get_status(), decide,
        set_status() -- releases the file lock between the read and the write,
        and the competing writer lands in exactly that gap. Only a check
        performed inside _merge_state()'s own critical section sees it.

        The interleaving write here is timed to land AFTER the caller has
        decided and called set_status(), but BEFORE _merge_state() takes the
        lock: the window a caller-side check cannot cover, and the one this
        check has to."""
        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")

        interleaved = threading.Event()
        original_merge = DevContainerStateManager._merge_state

        def _merge_after_an_interleaving_write(self, project_name, updates, expect=None):
            if expect is not None and not interleaved.is_set():
                interleaved.set()
                other = DevContainerStateManager(state_dir=self.state_dir)
                other.set_status(project_name, DevContainerStatus.IN_PROGRESS)
            return original_merge(self, project_name, updates, expect=expect)

        monkeypatch.setattr(
            DevContainerStateManager, '_merge_state', _merge_after_an_interleaving_write
        )

        manager.set_status(
            "proj",
            DevContainerStatus.UNVERIFIED,
            expect={'status': DevContainerStatus.VERIFIED.value},
        )

        assert interleaved.is_set(), "test setup: the competing write never ran"
        assert manager.get_status("proj") == DevContainerStatus.IN_PROGRESS


class TestVerifyAndUpdateStatusDoesNotClobberAFresherVerdict:
    """verify_and_update_status() reads VERIFIED, shells out to `docker image
    inspect` (10s timeout), and only then writes UNVERIFIED. Its two live
    callers are docker_runner's per-launch image resolution and main.py's
    startup sweep -- both of which can run while the observability server, a
    separate container, is finishing an operator-triggered rebuild (#171).

    The precondition names the VERSION it read, not just the status (#169
    review): a status-only check caught an interleaving writer that moved the
    record to IN_PROGRESS, but not the one that actually matters -- a rebuild
    that FINISHES in the window and writes a fresh VERIFIED."""

    @staticmethod
    def _probe(returns, side_effect=None):
        """Stand-in for verify_image_exists, recording the tag it was asked
        about and optionally letting a competing writer land first."""
        seen = []

        def _verify(self, project_name, image_name=None):
            seen.append(image_name)
            if side_effect is not None:
                side_effect()
            return returns

        return _verify, seen

    def test_a_rebuild_that_landed_during_the_docker_probe_is_not_reverted(self, manager):
        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")

        def _rebuild_lands_during_the_probe():
            manager.set_status(
                "proj", DevContainerStatus.IN_PROGRESS, image_name="proj-agent:latest"
            )

        probe, _seen = self._probe(False, side_effect=_rebuild_lands_during_the_probe)
        original = DevContainerStateManager.verify_image_exists
        try:
            DevContainerStateManager.verify_image_exists = probe
            result = manager.verify_and_update_status("proj")
        finally:
            DevContainerStateManager.verify_image_exists = original

        # The image this call actually looked at really was missing, so the
        # caller is still told not to launch against it...
        assert result is False
        # ...but the fresher verdict on disk is left alone.
        assert manager.get_status("proj") == DevContainerStatus.IN_PROGRESS

    def test_a_rebuild_that_FINISHED_during_the_probe_is_not_reverted_either(self, manager):
        """THE regression (#169 review). A finishing rebuild ends by writing
        VERIFIED -- observability_server's endpoint sets IN_PROGRESS and then
        rebuild_project_image(update_state=True) writes VERIFIED. Against a
        status-only precondition that still read 'verified' on disk, so the
        stale verdict landed: a freshly built, present image marked UNVERIFIED,
        and the next dispatch queuing a second hour-scale rebuild of it."""
        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")

        def _rebuild_finishes_during_the_probe():
            # Same status, same image name, new version of the record -- only
            # the timestamp distinguishes it from what this call read.
            time.sleep(0.01)
            manager.set_status(
                "proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest"
            )

        probe, _seen = self._probe(False, side_effect=_rebuild_finishes_during_the_probe)
        original = DevContainerStateManager.verify_image_exists
        try:
            DevContainerStateManager.verify_image_exists = probe
            result = manager.verify_and_update_status("proj")
        finally:
            DevContainerStateManager.verify_image_exists = original

        # The tag this call inspected was genuinely absent, so its own caller is
        # still told not to launch against it...
        assert result is False
        # ...but the rebuild that just finished keeps its verdict.
        assert manager.get_status("proj") == DevContainerStatus.VERIFIED

    def test_a_retag_during_the_probe_is_not_reverted(self, manager):
        """The image name is part of the version too: a verdict about
        proj-agent:latest must not land on a record that now names a different
        tag."""
        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")

        def _retag_during_the_probe():
            time.sleep(0.01)
            manager.set_status(
                "proj", DevContainerStatus.VERIFIED, image_name="proj-agent:v2"
            )

        probe, seen = self._probe(False, side_effect=_retag_during_the_probe)
        original = DevContainerStateManager.verify_image_exists
        try:
            DevContainerStateManager.verify_image_exists = probe
            result = manager.verify_and_update_status("proj")
        finally:
            DevContainerStateManager.verify_image_exists = original

        assert result is False
        assert seen == ["proj-agent:latest"], "the probe must inspect the tag it read"
        assert manager.get_image_name("proj") == "proj-agent:v2"
        assert manager.get_status("proj") == DevContainerStatus.VERIFIED

    def test_the_probe_inspects_the_tag_from_the_same_snapshot(self, manager):
        """Re-reading the image name after the probe would pair a verdict about
        one tag with a write about another."""
        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")

        probe, seen = self._probe(True)
        original = DevContainerStateManager.verify_image_exists
        try:
            DevContainerStateManager.verify_image_exists = probe
            assert manager.verify_and_update_status("proj") is True
        finally:
            DevContainerStateManager.verify_image_exists = original

        assert seen == ["proj-agent:latest"]

    def test_it_still_resets_a_status_nothing_else_touched(self, manager):
        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")

        probe, _seen = self._probe(False)
        original = DevContainerStateManager.verify_image_exists
        try:
            DevContainerStateManager.verify_image_exists = probe
            result = manager.verify_and_update_status("proj")
        finally:
            DevContainerStateManager.verify_image_exists = original

        assert result is False
        assert manager.get_status("proj") == DevContainerStatus.UNVERIFIED


class TestStatusAndTimestampComeFromOneSnapshot:
    """#171. get_status() and get_status_updated_at() each take the file lock
    separately, so a caller that reads both in sequence can pair a status from
    one version of the file with a timestamp from another -- and neither
    accessor can detect that it happened."""

    def test_both_values_come_from_a_single_read(self, manager):
        manager.set_status("proj", DevContainerStatus.VERIFIED, image_name="proj-agent:latest")

        reads = []
        original_read = DevContainerStateManager._read_state

        def _counting_read(self, project_name):
            reads.append(project_name)
            return original_read(self, project_name)

        try:
            DevContainerStateManager._read_state = _counting_read
            status, updated_at = manager.get_status_and_updated_at("proj")
        finally:
            DevContainerStateManager._read_state = original_read

        assert reads == ["proj"]
        assert status == DevContainerStatus.VERIFIED
        assert updated_at is not None

    def test_a_missing_file_degrades_the_same_way_the_single_accessors_do(self, manager):
        status, updated_at = manager.get_status_and_updated_at("never-seen")

        assert status == DevContainerStatus.UNVERIFIED
        assert updated_at is None

    def test_an_unparseable_timestamp_degrades_to_none_without_losing_the_status(
        self, manager
    ):
        manager.set_status("proj", DevContainerStatus.BLOCKED, error_message="x")
        manager._merge_state("proj", {'updated_at': 'not-a-timestamp'})

        status, updated_at = manager.get_status_and_updated_at("proj")

        assert status == DevContainerStatus.BLOCKED
        assert updated_at is None


class TestSetStatusReportsWhetherTheWriteLanded:
    """#171 review. set_status() computes _merge_state()'s bool and threw it
    away, so a write that never reached disk -- the file lock timing out at
    STATE_LOCK_TIMEOUT_SECONDS against the verifier's own OS process, an
    unwritable state dir -- was indistinguishable from one that did.

    queue_dev_environment_setup() depends on that distinction: its IN_PROGRESS
    mark is the ONLY record that a setup is coming, and enqueuing an hour-scale
    rebuild the state file does not record is what lets the next board poll read
    the unchanged status and queue a second one."""

    def test_a_landed_write_returns_true(self, manager):
        assert manager.set_status('proj', DevContainerStatus.IN_PROGRESS) is True
        assert manager.get_status('proj') == DevContainerStatus.IN_PROGRESS

    def test_a_failed_write_returns_false(self, manager, monkeypatch):
        def _explode(*args, **kwargs):
            raise OSError("state dir is read-only")

        monkeypatch.setattr(dev_container_state_module, 'open', _explode, raising=False)
        assert manager.set_status('proj', DevContainerStatus.VERIFIED) is False

    def test_a_lock_acquire_timeout_returns_false(self, manager):
        """The realistic failure: another OS process holds the state file's
        cross-process lock for longer than STATE_LOCK_TIMEOUT_SECONDS."""
        state_file = manager.get_state_file('proj')
        state_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = manager._state_lock_file(state_file)

        with file_lock(lock_path, timeout=5, enforce_timeout=True):
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(dev_container_state_module, 'STATE_LOCK_TIMEOUT_SECONDS', 0.1)
                assert manager.set_status('proj', DevContainerStatus.VERIFIED) is False

        assert manager.get_status('proj') == DevContainerStatus.UNVERIFIED

    def test_a_refused_precondition_returns_false(self, manager):
        """A compare-and-set whose `expect` no longer matches is also "the value
        you decided on is not what is on disk", which is the only thing a caller
        acts on."""
        manager.set_status('proj', DevContainerStatus.VERIFIED)
        assert manager.set_status(
            'proj', DevContainerStatus.UNVERIFIED, expect={'status': 'in_progress'}
        ) is False
        assert manager.get_status('proj') == DevContainerStatus.VERIFIED
