"""
Unit tests for the /api/projects/<project>/rebuild-image endpoint's
dev_container_build locking (issue #152 item A).

That endpoint is the third operator-triggered build of a project's agent image,
alongside scripts/rebuild_project_images.py and
scripts/set_dev_container_verified.py -- the two #56 wired to the lock. It was
the one still writing dev_container_state outside it: the IN_PROGRESS mark ran
before rebuild_project_image() took the lock, and both BLOCKED marks ran after
it had released.

Because every acquisition of that lock mints its own holder id and is therefore
NOT reentrant (see services/dev_container_build_lock.py's module docstring), the
endpoint holding it across the whole sequence means rebuild_project_image() must
NOT acquire it again -- hence lock_held_by_caller.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

import contextlib
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.rebuild_project_images import rebuild_project_image
from services.dev_container_build_lock import RESOURCE_NAME


class TestRebuildProjectImageLockHeldByCaller:
    """The half of the fix that would deadlock if it were wrong."""

    def _run(self, lock_held_by_caller):
        acquisitions = []

        @contextlib.contextmanager
        def _recording_lock(project, *args, **kwargs):
            acquisitions.append(project)
            yield

        workspace = Path(tempfile.mkdtemp())
        try:
            (workspace / 'proj').mkdir()
            (workspace / 'proj' / 'Dockerfile.agent').write_text("FROM scratch\n")

            with patch('scripts.rebuild_project_images.get_workspace_root', return_value=workspace), \
                 patch('scripts.rebuild_project_images.dev_container_build_lock_sync', _recording_lock), \
                 patch('scripts.rebuild_project_images.subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
                ok = rebuild_project_image(
                    'proj', update_state=False, lock_held_by_caller=lock_held_by_caller
                )
        finally:
            shutil.rmtree(workspace)

        return ok, acquisitions

    def test_acquires_the_lock_by_default(self):
        """Unchanged for the CLI callers, which own no wider sequence."""
        ok, acquisitions = self._run(lock_held_by_caller=False)
        assert ok is True
        assert acquisitions == ['proj']

    def test_does_not_acquire_when_the_caller_already_holds_it(self):
        """THE deadlock guard: re-acquiring here would self-block for the lock's
        full timeout, since a matching holder id is never minted twice."""
        ok, acquisitions = self._run(lock_held_by_caller=True)
        assert ok is True
        assert acquisitions == []


class TestRebuildImageEndpointHoldsTheLockAcrossItsStateWrites:

    def _invoke(self, rebuild_result, lock_busy=False, if_free_granted=True, status=None):
        """Drive the endpoint's background worker synchronously.

        `status` is what get_status() reports -- read inside the if_free lock on
        the contention path to re-decide whether BLOCKED is still the right thing
        to write. None leaves it a bare MagicMock, i.e. "some status that is
        neither verified nor in_progress".
        """
        from services import observability_server
        from services.dev_container_build_lock import DevContainerBuildLockTimeoutError

        events = []

        @contextlib.contextmanager
        def _lock(project, *args, **kwargs):
            if lock_busy:
                raise DevContainerBuildLockTimeoutError(f"busy: {project}")
            events.append(('lock:enter', project))
            try:
                yield
            finally:
                events.append(('lock:exit', project))

        @contextlib.contextmanager
        def _if_free_lock(project, *args, **kwargs):
            events.append(('if_free:attempt', project))
            yield if_free_granted

        state = MagicMock()
        state.set_status.side_effect = lambda project, new_status, **kw: events.append(
            ('set_status', new_status)
        )
        state.set_pending_operation.side_effect = lambda project, operation: events.append(
            ('pending:set', operation)
        )
        state.clear_pending_operation.side_effect = lambda project: events.append(
            ('pending:clear', project)
        )
        if status is not None:
            state.get_status.return_value = status

        def _rebuild(project, update_state=False, lock_held_by_caller=False):
            events.append(('rebuild', lock_held_by_caller))
            if isinstance(rebuild_result, Exception):
                raise rebuild_result
            return rebuild_result

        threads = []

        class _ImmediateThread:
            def __init__(self, target, daemon=None):
                self._target = target
                threads.append(self)

            def start(self):
                self._target()

        with patch('scripts.rebuild_project_images.rebuild_project_image', _rebuild), \
             patch('services.dev_container_build_lock.dev_container_build_lock_sync', _lock), \
             patch('services.dev_container_build_lock.dev_container_build_lock_if_free_sync', _if_free_lock), \
             patch('services.dev_container_state.dev_container_state', state), \
             patch.object(observability_server, 'threading') as mock_threading:
            mock_threading.Thread = _ImmediateThread
            client = observability_server.app.test_client()
            response = client.post('/api/projects/proj/rebuild-image')

        return response, events

    def test_every_state_write_happens_inside_the_lock(self):
        """THE regression: IN_PROGRESS used to be written before the lock was
        taken (in the request thread, no less) and BLOCKED after it was released."""
        from services.dev_container_state import DevContainerStatus

        response, events = self._invoke(rebuild_result=False)

        assert response.status_code == 200
        # The pending-rebuild marker deliberately brackets the lock (it describes
        # the WAIT), so the window is measured against the status writes only.
        enter = events.index(('lock:enter', 'proj'))
        exit_ = events.index(('lock:exit', 'proj'))
        writes = [i for i, e in enumerate(events) if e[0] == 'set_status']
        assert writes
        assert all(enter < i < exit_ for i in writes)
        assert ('set_status', DevContainerStatus.IN_PROGRESS) in events
        assert ('set_status', DevContainerStatus.BLOCKED) in events

    def test_the_rebuild_is_told_the_lock_is_already_held(self):
        response, events = self._invoke(rebuild_result=True)
        assert response.status_code == 200
        assert ('rebuild', True) in events

    def test_a_build_exception_still_marks_blocked_inside_the_lock(self):
        from services.dev_container_state import DevContainerStatus

        _, events = self._invoke(rebuild_result=RuntimeError("boom"))

        assert events.index(('set_status', DevContainerStatus.BLOCKED)) < events.index(
            ('lock:exit', 'proj')
        )

    def test_lock_contention_never_marks_in_progress(self):
        """#148/WI-3: nothing ran, so IN_PROGRESS -- which used to be stamped in
        the request thread before any lock was attempted -- must not be written."""
        from services.dev_container_state import DevContainerStatus

        response, events = self._invoke(rebuild_result=True, lock_busy=True)

        assert response.status_code == 200
        assert ('set_status', DevContainerStatus.IN_PROGRESS) not in events
        assert ('rebuild', True) not in [e for e in events]

    def test_lock_contention_records_that_the_rebuild_never_started(self):
        """#152 review: the endpoint answers {"success": true, "triggered": true}
        and its only feedback channel is the state file every caller polls
        (mcp/server.py's get_image_build_status reads it directly). A bare log
        line left that caller reading the project's PRE-EXISTING status --
        commonly 'verified' -- and concluding the rebuild had finished."""
        from services.dev_container_state import DevContainerStatus

        response, events = self._invoke(rebuild_result=True, lock_busy=True)

        assert response.status_code == 200
        assert ('if_free:attempt', 'proj') in events
        assert ('set_status', DevContainerStatus.BLOCKED) in events

    def test_the_drop_is_recorded_under_the_lock_not_over_a_live_holder(self):
        """That record still never lands on top of a build that is genuinely
        running: it goes through the non-blocking variant, so a lock that is
        still held leaves the holder's own status alone."""
        from services.dev_container_state import DevContainerStatus

        _, events = self._invoke(
            rebuild_result=True, lock_busy=True, if_free_granted=False
        )

        assert ('if_free:attempt', 'proj') in events
        assert ('set_status', DevContainerStatus.BLOCKED) not in events

    def test_the_drop_is_not_recorded_over_a_status_that_moved_during_the_wait(self):
        """#152 review: taking the if_free lock is not enough on its own. Between
        the failed blocking acquire and this one the previous holder can finish
        and release -- log formatting plus a possible Redis connect sit in
        between -- so BLOCKED could land on top of the VERIFIED a verifier had
        just written. BLOCKED is terminal and validate_task_can_run gives the
        terminal statuses no staleness escape, so every task for the project
        would be refused indefinitely over lock contention against a healthy,
        freshly-verified image."""
        from services.dev_container_state import DevContainerStatus

        _, events = self._invoke(
            rebuild_result=True, lock_busy=True, status=DevContainerStatus.VERIFIED
        )

        assert ('if_free:attempt', 'proj') in events
        assert ('set_status', DevContainerStatus.BLOCKED) not in events

    def test_the_drop_is_not_recorded_over_a_build_that_just_started(self):
        """Same re-read, other direction: a holder that took the lock, wrote
        IN_PROGRESS and released it back in the gap still owns the status."""
        from services.dev_container_state import DevContainerStatus

        _, events = self._invoke(
            rebuild_result=True, lock_busy=True, status=DevContainerStatus.IN_PROGRESS
        )

        assert ('set_status', DevContainerStatus.BLOCKED) not in events

    def test_the_drop_is_still_recorded_when_nothing_better_has_been_written(self):
        """The re-read narrows the write, it does not remove it -- an UNVERIFIED
        project whose rebuild never started must still say so."""
        from services.dev_container_state import DevContainerStatus

        _, events = self._invoke(
            rebuild_result=True, lock_busy=True, status=DevContainerStatus.UNVERIFIED
        )

        assert ('set_status', DevContainerStatus.BLOCKED) in events

    def test_the_wait_is_visible_before_the_build_starts(self):
        """#152 review: IN_PROGRESS is written INSIDE the lock, so for the whole
        (up to 900s) wait a caller polling the state file -- mcp/server.py's
        get_image_build_status, the endpoint's only feedback channel -- read the
        project's PRE-EXISTING status, commonly 'verified', and concluded the
        rebuild had finished. The wait gets its own marker instead, set before
        the worker thread starts so the first poll already sees it."""
        response, events = self._invoke(rebuild_result=True)

        assert response.get_json()['status'] == 'queued'
        assert events[0] == ('pending:set', 'rebuild')
        assert events.index(('pending:set', 'rebuild')) < events.index(('lock:enter', 'proj'))

    def test_the_wait_marker_is_cleared_once_the_build_owns_the_status(self):
        from services.dev_container_state import DevContainerStatus

        _, events = self._invoke(rebuild_result=True)

        cleared = events.index(('pending:clear', 'proj'))
        assert cleared < events.index(('set_status', DevContainerStatus.IN_PROGRESS))
        assert events[-1] == ('pending:clear', 'proj')

    def test_the_wait_marker_does_not_outlive_a_dropped_rebuild(self):
        """A rebuild that never got the lock is not still pending."""
        _, events = self._invoke(rebuild_result=True, lock_busy=True)

        assert ('pending:clear', 'proj') in events
        assert events[-1] == ('pending:clear', 'proj')

    def test_the_operator_path_does_not_wait_out_a_whole_build_window(self):
        """3700s is calibrated for an agent's build window, not an interactive
        request whose caller is polling the state file for an answer."""
        from services import observability_server
        from services.dev_container_build_lock import DEFAULT_TIMEOUT_SECONDS

        assert observability_server.REBUILD_ENDPOINT_LOCK_TIMEOUT_SECONDS < DEFAULT_TIMEOUT_SECONDS
