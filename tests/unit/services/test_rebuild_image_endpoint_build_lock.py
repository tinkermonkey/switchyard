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

    def _invoke(self, rebuild_result, lock_busy=False):
        """Drive the endpoint's background worker synchronously."""
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

        state = MagicMock()
        state.set_status.side_effect = lambda project, status, **kw: events.append(
            ('set_status', status)
        )

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
        assert events[0] == ('lock:enter', 'proj')
        assert events[-1] == ('lock:exit', 'proj')
        assert ('set_status', DevContainerStatus.IN_PROGRESS) in events
        assert ('set_status', DevContainerStatus.BLOCKED) in events

    def test_the_rebuild_is_told_the_lock_is_already_held(self):
        response, events = self._invoke(rebuild_result=True)
        assert response.status_code == 200
        assert ('rebuild', True) in events

    def test_a_build_exception_still_marks_blocked_inside_the_lock(self):
        from services.dev_container_state import DevContainerStatus

        _, events = self._invoke(rebuild_result=RuntimeError("boom"))

        assert events[-1] == ('lock:exit', 'proj')
        assert ('set_status', DevContainerStatus.BLOCKED) in events

    def test_lock_contention_writes_no_state_at_all(self):
        """#148/WI-3: nothing ran, so nothing about this project's container
        state changed and none of it should be rewritten -- least of all
        IN_PROGRESS, which used to be stamped before any lock was attempted."""
        response, events = self._invoke(rebuild_result=True, lock_busy=True)

        assert response.status_code == 200
        assert events == []
