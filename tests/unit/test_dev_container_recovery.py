"""
Tests for dev container recovery deduplication.

Verifies that queue_dev_environment_setup() is idempotent and that
process_task_integrated() raises NonRetryableAgentError (not generic Exception)
when a task is blocked by dev container validation.
"""

import os
import pytest
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import MagicMock, Mock, AsyncMock, patch

# agents module requires Docker container environment
if not os.path.exists('/app/state/dev_containers'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)


def _status_snapshot_from_stubs(mock_state):
    """Keep a mocked dev_container_state's get_status_and_updated_at() (#171)
    consistent with the per-field stubs a test sets.

    queue_dev_environment_setup() now reads the status and its timestamp from
    ONE snapshot, so that a decision built from both cannot straddle an
    interleaving write. Tests still state the two separately; this derives the
    snapshot from them rather than making every test say it twice.
    """
    mock_state.get_status_and_updated_at.side_effect = lambda project: (
        mock_state.get_status.return_value,
        mock_state.get_status_updated_at.return_value,
    )


@contextmanager
def _build_lock(acquired: bool, reason: str = "lock_state_unknown_failing_closed"):
    """Stand-in for dev_container_build_lock_attempt_async (#171).

    queue_dev_environment_setup() takes that lock around its check-then-mark.
    These tests are about the guard, not the lock, and the real acquire would
    reach Redis / the on-disk lock store; the lock's own behaviour is covered by
    tests/unit/services/test_dev_container_build_lock.py.

    `reason` is what the refusal is attributed to, and it decides which branch
    the caller takes (#169 review): a live holder means defer, anything else
    means fall back unserialized. The default is a degraded store, so every test
    that only cares "the acquire failed" keeps the fallback it was written for.
    """
    @asynccontextmanager
    async def _ctx(project, issue_number=None, facade=None):
        yield (True, None) if acquired else (False, reason)

    with patch(
        'services.dev_container_build_lock.dev_container_build_lock_attempt_async', _ctx
    ):
        yield


@contextmanager
def _build_lock_row(*, held=True, holder_is_live=True, retained_reason=None):
    """Stand-in for the dev_container_build lock ROW that
    _live_build_lock_reason() reads (#169 review).

    "The acquire was refused naming a holder" and "that holder still exists" are
    different questions, and only the second one licenses deferring: a release
    that could not be serialized against a concurrent acquire/refresh leaves a
    row behind that nothing in this process will ever clean up, and it reads as
    genuine contention until its TTL lapses. `holder_is_live` is the heartbeat
    evidence that tells the two apart.
    """
    lock = Mock()
    lock.retained_reason = retained_reason
    lock.lock_acquired_at = '2026-01-01T00:00:00+00:00'
    lock.locked_by_issue = -4242

    facade = MagicMock()
    facade.get_resource_lock.return_value = lock if held else None
    facade.holder_liveness_is_fresh.return_value = holder_is_live

    with patch(
        'services.project_resource_lock_manager.ProjectResourceLockManager',
        return_value=facade,
    ):
        yield facade


@pytest.fixture
def mock_logger():
    """Create a mock logger."""
    logger = Mock()
    logger.log_warning = Mock()
    logger.info = Mock()
    logger.error = Mock()
    return logger


@pytest.fixture
def mock_task():
    """Create a mock task object."""
    task = Mock()
    task.id = "test-task-123"
    task.agent = "senior_software_engineer"
    task.project = "test-project"
    task.context = {
        'board': 'dev_board',
        'issue_number': 42,
    }
    return task


# ---------------------------------------------------------------------------
# queue_dev_environment_setup tests
# ---------------------------------------------------------------------------

class TestQueueDevEnvironmentSetup:
    """Tests for idempotent dev environment setup queuing."""

    @pytest.fixture(autouse=True)
    def _serialized_by_default(self):
        """queue_dev_environment_setup() runs its check-then-mark inside this
        project's dev_container_build lock (#171). Granted by default here; the
        tests that care about a refused acquire re-patch it themselves."""
        with _build_lock(True):
            yield

    @pytest.mark.asyncio
    async def test_skips_when_already_in_progress(self, mock_logger):
        """When status is a RECENT IN_PROGRESS, should skip queuing entirely."""
        from datetime import datetime
        from services.dev_container_state import DevContainerStatus

        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status.return_value = DevContainerStatus.IN_PROGRESS
            mock_state.get_status_updated_at.return_value = datetime.now()
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            _status_snapshot_from_stubs(mock_state)
            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("test-project", mock_logger)

            # Should NOT set status or enqueue
            mock_state.set_status.assert_not_called()
            mock_queue_instance.enqueue.assert_not_called()
            # Should log the skip
            assert any("skipping duplicate" in str(call) for call in mock_logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_sets_in_progress_before_queuing(self, mock_logger):
        """When status is UNVERIFIED, should set IN_PROGRESS before enqueuing."""
        from services.dev_container_state import DevContainerStatus

        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status.return_value = DevContainerStatus.UNVERIFIED
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            # Track call order
            call_order = []
            # Returns True: set_status()'s bool is the "the mark reached disk"
            # signal queue_dev_environment_setup() now refuses to enqueue
            # without (#171 review), so a recorder that returned None would
            # be stubbing a failed write.
            mock_state.set_status.side_effect = (
                lambda *a, **kw: (call_order.append('set_status'), True)[1]
            )
            mock_queue_instance.enqueue.side_effect = lambda *a, **kw: call_order.append('enqueue')

            _status_snapshot_from_stubs(mock_state)
            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("test-project", mock_logger)

            # Should set status first, then enqueue
            mock_state.set_status.assert_called_once_with(
                "test-project",
                DevContainerStatus.IN_PROGRESS,
                image_name="test-project-agent:latest"
            )
            mock_queue_instance.enqueue.assert_called_once()
            assert call_order == ['set_status', 'enqueue']

    @pytest.mark.asyncio
    async def test_queues_task_when_unverified(self, mock_logger):
        """When status is UNVERIFIED, should create and enqueue a setup task."""
        from services.dev_container_state import DevContainerStatus

        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status.return_value = DevContainerStatus.UNVERIFIED
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            _status_snapshot_from_stubs(mock_state)
            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("my-project", mock_logger)

            mock_queue_instance.enqueue.assert_called_once()
            # Verify the enqueued task has the right agent
            enqueued_task = mock_queue_instance.enqueue.call_args[0][0]
            assert enqueued_task.agent == "dev_environment_setup"
            assert enqueued_task.project == "my-project"

    @pytest.mark.asyncio
    async def test_issue_body_includes_required_fix_marker_when_change_description_given(self, mock_logger):
        """A non-empty change_description must be wrapped in a literal '## REQUIRED FIX'
        section — this exact heading is what dev_environment_setup/guidelines.md and
        dev_environment_verifier/review_task.md key their behavior off of."""
        from services.dev_container_state import DevContainerStatus

        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status.return_value = DevContainerStatus.UNVERIFIED
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            _status_snapshot_from_stubs(mock_state)
            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup(
                "my-project", mock_logger,
                change_description="Add codegen extra to pyproject.toml"
            )

            enqueued_task = mock_queue_instance.enqueue.call_args[0][0]
            body = enqueued_task.context['issue']['body']
            assert "## REQUIRED FIX" in body
            assert "Add codegen extra to pyproject.toml" in body

    @pytest.mark.asyncio
    async def test_issue_body_is_base_body_only_when_change_description_empty(self, mock_logger):
        """With no change_description, the issue body must be exactly the base
        auto-triggered message and must NOT contain a REQUIRED FIX section — the
        verifier's Step 0 relies on the marker's absence here."""
        from services.dev_container_state import DevContainerStatus

        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status.return_value = DevContainerStatus.UNVERIFIED
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            _status_snapshot_from_stubs(mock_state)
            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("my-project", mock_logger)

            enqueued_task = mock_queue_instance.enqueue.call_args[0][0]
            body = enqueued_task.context['issue']['body']
            assert body == 'Auto-triggered: Agent requires dev container but it is not verified'
            assert "REQUIRED FIX" not in body

    @pytest.mark.asyncio
    async def test_issue_body_is_base_body_only_when_change_description_whitespace_only(self, mock_logger):
        """A whitespace-only change_description is truthy in Python and must not be
        treated as a real REQUIRED FIX — it would otherwise render an empty section."""
        from services.dev_container_state import DevContainerStatus

        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status.return_value = DevContainerStatus.UNVERIFIED
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            _status_snapshot_from_stubs(mock_state)
            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("my-project", mock_logger, change_description="   ")

            enqueued_task = mock_queue_instance.enqueue.call_args[0][0]
            body = enqueued_task.context['issue']['body']
            assert body == 'Auto-triggered: Agent requires dev container but it is not verified'
            assert "REQUIRED FIX" not in body

    @pytest.mark.asyncio
    async def test_consecutive_calls_only_queue_once(self, mock_logger):
        """First call sets IN_PROGRESS and queues; second call sees IN_PROGRESS and skips."""
        from datetime import datetime
        from services.dev_container_state import DevContainerStatus

        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            _status_snapshot_from_stubs(mock_state)
            from agents.orchestrator_integration import queue_dev_environment_setup

            # First call: UNVERIFIED -> sets IN_PROGRESS and queues
            mock_state.get_status.return_value = DevContainerStatus.UNVERIFIED
            await queue_dev_environment_setup("test-project", mock_logger)
            assert mock_queue_instance.enqueue.call_count == 1

            # Second call: now IN_PROGRESS (and fresh) -> skips
            mock_state.get_status.return_value = DevContainerStatus.IN_PROGRESS
            mock_state.get_status_updated_at.return_value = datetime.now()
            await queue_dev_environment_setup("test-project", mock_logger)
            # Still only 1 enqueue total
            assert mock_queue_instance.enqueue.call_count == 1

    @staticmethod
    def _no_live_setup():
        """
        Patch out the three liveness probes _dev_setup_in_flight_reason() makes,
        so a test can exercise the genuinely-dead case: nothing queued, nothing
        running, no build holding the lock. Each is patched at its own source so
        neither the real task queue, the real execution-state directory nor the
        real Redis lock store is touched.
        """
        tracker = MagicMock()
        tracker.load_state.return_value = {'execution_history': []}
        facade = MagicMock()
        facade.get_resource_lock.return_value = None
        return (
            patch('services.work_execution_state.work_execution_tracker', tracker),
            patch('services.project_resource_lock_manager.ProjectResourceLockManager', return_value=facade),
        )

    @pytest.mark.asyncio
    async def test_a_stale_in_progress_with_no_live_run_is_requeued(self, mock_logger):
        """#152 review: this guard and validate_task_can_run()'s staleness check
        disagreed, silently. Past STALE_IN_PROGRESS_MINUTES validation returns
        needs_dev_setup=True, its caller calls this function, this function saw
        IN_PROGRESS and returned having queued nothing, and the status was never
        written -- so the next task repeated it, forever, emitting a 'Recovery
        successful' decision event each pass."""
        from datetime import datetime, timedelta
        from agents.orchestrator_integration import STALE_IN_PROGRESS_MINUTES
        from services.dev_container_state import DevContainerStatus

        patch_tracker, patch_facade = self._no_live_setup()
        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue, \
             patch_tracker, patch_facade:

            mock_state.get_status.return_value = DevContainerStatus.IN_PROGRESS
            mock_state.get_status_updated_at.return_value = (
                datetime.now() - timedelta(minutes=STALE_IN_PROGRESS_MINUTES + 1)
            )
            mock_queue_instance = Mock()
            mock_queue_instance.get_pending_tasks.return_value = []
            MockTaskQueue.return_value = mock_queue_instance

            _status_snapshot_from_stubs(mock_state)
            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("test-project", mock_logger)

            assert mock_queue_instance.enqueue.call_count == 1
            assert mock_state.set_status.call_args.args[1] == DevContainerStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_a_stale_in_progress_is_not_requeued_while_a_task_is_still_queued(self, mock_logger):
        """#152 review: the staleness window measures the age of the last status
        WRITE, and nothing refreshes it while a setup sits in the queue. With
        ORCHESTRATOR_WORKERS defaulting to 1, a queued setup routinely waits past
        20 minutes, and re-queuing then is a duplicate hour-scale agent run, not
        a recovery."""
        from datetime import datetime, timedelta
        from agents.orchestrator_integration import STALE_IN_PROGRESS_MINUTES
        from services.dev_container_state import DevContainerStatus

        queued = Mock()
        queued.project = "test-project"
        queued.id = "queued-task-id"

        patch_tracker, patch_facade = self._no_live_setup()
        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue, \
             patch_tracker, patch_facade:

            mock_state.get_status.return_value = DevContainerStatus.IN_PROGRESS
            mock_state.get_status_updated_at.return_value = (
                datetime.now() - timedelta(minutes=STALE_IN_PROGRESS_MINUTES + 1)
            )
            mock_queue_instance = Mock()
            mock_queue_instance.get_pending_tasks.return_value = [queued]
            MockTaskQueue.return_value = mock_queue_instance

            _status_snapshot_from_stubs(mock_state)
            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("test-project", mock_logger)

            mock_queue_instance.enqueue.assert_not_called()
            mock_state.set_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_stale_in_progress_is_not_requeued_while_an_execution_is_running(self, mock_logger):
        """A dequeued setup that is mid-build refreshes nothing either -- its
        agent timeout is 3600s precisely because builds run long."""
        from datetime import datetime, timedelta
        from agents.orchestrator_integration import STALE_IN_PROGRESS_MINUTES
        from services.dev_container_state import DevContainerStatus

        tracker = MagicMock()
        tracker.load_state.return_value = {
            'execution_history': [
                {'agent': 'dev_environment_setup', 'outcome': 'in_progress', 'timestamp': 'now'}
            ]
        }
        facade = MagicMock()
        facade.get_resource_lock.return_value = None

        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue, \
             patch('services.work_execution_state.work_execution_tracker', tracker), \
             patch('services.project_resource_lock_manager.ProjectResourceLockManager', return_value=facade):

            mock_state.get_status.return_value = DevContainerStatus.IN_PROGRESS
            mock_state.get_status_updated_at.return_value = (
                datetime.now() - timedelta(minutes=STALE_IN_PROGRESS_MINUTES + 1)
            )
            mock_queue_instance = Mock()
            mock_queue_instance.get_pending_tasks.return_value = []
            MockTaskQueue.return_value = mock_queue_instance

            _status_snapshot_from_stubs(mock_state)
            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("test-project", mock_logger)

            mock_queue_instance.enqueue.assert_not_called()
            mock_state.set_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_stale_in_progress_is_not_requeued_while_the_build_lock_is_held(self, mock_logger):
        """A held dev_container_build lock means a build genuinely IS running,
        whoever started it -- the one signal that outlasts the staleness window
        most often.

        A held lock is also exactly what refuses this function's own
        non-blocking acquire (#171), so the run reaches probe 3 through the
        unserialized fallback -- which is the branch that still has to make this
        judgement, and the reason probe 3 was kept rather than replaced by the
        acquire."""
        from datetime import datetime, timedelta
        from agents.orchestrator_integration import STALE_IN_PROGRESS_MINUTES
        from services.dev_container_state import DevContainerStatus

        held = Mock()
        held.retained_reason = None
        held.lock_acquired_at = '2026-01-01T00:00:00+00:00'
        held.locked_by_issue = -4242

        tracker = MagicMock()
        tracker.load_state.return_value = {'execution_history': []}
        facade = MagicMock()
        facade.get_resource_lock.return_value = held
        # Stated rather than left to a MagicMock's truthiness (#169 review):
        # probe 3 now asks whether the recorded holder is still HEARTBEATING,
        # and this test is about the branch where it is.
        facade.holder_liveness_is_fresh.return_value = True

        with _build_lock(False), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue, \
             patch('services.work_execution_state.work_execution_tracker', tracker), \
             patch('services.project_resource_lock_manager.ProjectResourceLockManager', return_value=facade):

            mock_state.get_status.return_value = DevContainerStatus.IN_PROGRESS
            mock_state.get_status_updated_at.return_value = (
                datetime.now() - timedelta(minutes=STALE_IN_PROGRESS_MINUTES + 1)
            )
            mock_queue_instance = Mock()
            mock_queue_instance.get_pending_tasks.return_value = []
            MockTaskQueue.return_value = mock_queue_instance

            _status_snapshot_from_stubs(mock_state)
            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("test-project", mock_logger)

            mock_queue_instance.enqueue.assert_not_called()
            mock_state.set_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_probe_that_raises_keeps_the_duplicate_guard(self, mock_logger):
        """Re-queuing wrongly costs an hour-scale redundant agent run; skipping
        wrongly costs one 30-second sweep. An unreadable probe takes the cheap
        side."""
        from datetime import datetime, timedelta
        from agents.orchestrator_integration import STALE_IN_PROGRESS_MINUTES
        from services.dev_container_state import DevContainerStatus

        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status.return_value = DevContainerStatus.IN_PROGRESS
            mock_state.get_status_updated_at.return_value = (
                datetime.now() - timedelta(minutes=STALE_IN_PROGRESS_MINUTES + 1)
            )
            mock_queue_instance = Mock()
            mock_queue_instance.get_pending_tasks.side_effect = RuntimeError("redis down")
            MockTaskQueue.return_value = mock_queue_instance

            _status_snapshot_from_stubs(mock_state)
            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("test-project", mock_logger)

            mock_queue_instance.enqueue.assert_not_called()
            mock_state.set_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unreadable_timestamp_keeps_the_duplicate_guard(self, mock_logger):
        """No timestamp is not evidence of staleness -- fall back to skipping,
        the behaviour that has always been safe against a duplicate queue."""
        from services.dev_container_state import DevContainerStatus

        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status.return_value = DevContainerStatus.IN_PROGRESS
            mock_state.get_status_updated_at.return_value = None
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            _status_snapshot_from_stubs(mock_state)
            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("test-project", mock_logger)

            mock_queue_instance.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_rolls_back_status_on_enqueue_failure(self, mock_logger):
        """If enqueue fails, status should roll back to UNVERIFIED to prevent deadlock."""
        from services.dev_container_state import DevContainerStatus

        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status.return_value = DevContainerStatus.UNVERIFIED
            mock_queue_instance = Mock()
            mock_queue_instance.enqueue.side_effect = ConnectionError("Redis unavailable")
            MockTaskQueue.return_value = mock_queue_instance

            _status_snapshot_from_stubs(mock_state)
            from agents.orchestrator_integration import queue_dev_environment_setup

            with pytest.raises(ConnectionError, match="Redis unavailable"):
                await queue_dev_environment_setup("test-project", mock_logger)

            # Should have set IN_PROGRESS first, then rolled back to UNVERIFIED
            assert mock_state.set_status.call_count == 2
            first_call = mock_state.set_status.call_args_list[0]
            assert first_call[0] == ("test-project", DevContainerStatus.IN_PROGRESS)
            second_call = mock_state.set_status.call_args_list[1]
            assert second_call[0] == ("test-project", DevContainerStatus.UNVERIFIED)

            # Should have logged the error
            assert any("Rolling back" in str(call) for call in mock_logger.error.call_args_list)


# ---------------------------------------------------------------------------
# queue_dev_environment_setup: the check-then-mark is one critical section
# ---------------------------------------------------------------------------

class TestTheDuplicateGuardIsAtomic:
    """#171. "Mark as in-progress BEFORE queuing to prevent races" only prevents
    them if the read that decided to mark and the mark itself are one critical
    section. They were two, so two concurrent callers for the same project both
    read a non-IN_PROGRESS status, both wrote IN_PROGRESS and both enqueued --
    the duplicate hour-scale rebuild _dev_setup_in_flight_reason()'s docstring
    describes, reached from the other direction."""

    @pytest.mark.asyncio
    async def test_the_status_is_re_read_inside_the_lock(self, mock_logger):
        """THE regression: the interleaving write lands after this caller's own
        caller decided it needed a setup, but before the guard reads. A guard
        reading outside the lock cannot see it; one reading inside must."""
        from datetime import datetime
        from services.dev_container_state import DevContainerStatus

        reads = []

        def _status_at_read_time(project):
            # First read happens inside the lock; by then another dispatch has
            # already marked the project IN_PROGRESS.
            reads.append(project)
            return (DevContainerStatus.IN_PROGRESS, datetime.now())

        with _build_lock(True), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status_and_updated_at.side_effect = _status_at_read_time
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("test-project", mock_logger)

        assert reads == ["test-project"], "the guard did not re-read the status"
        mock_state.set_status.assert_not_called()
        mock_queue_instance.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_decision_and_the_mark_happen_inside_the_same_hold(self, mock_logger):
        from services.dev_container_state import DevContainerStatus

        events = []

        @asynccontextmanager
        async def _recording_lock(project, issue_number=None, facade=None):
            events.append('lock_acquired')
            try:
                yield True, None
            finally:
                events.append('lock_released')

        with patch('services.dev_container_build_lock.dev_container_build_lock_attempt_async',
                   _recording_lock), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status_and_updated_at.side_effect = (
                lambda project: (events.append('status_read'),
                                 (DevContainerStatus.UNVERIFIED, None))[1]
            )
            mock_state.set_status.side_effect = (
                lambda *a, **kw: (events.append('set_status'), True)[1]
            )
            mock_queue_instance = Mock()
            mock_queue_instance.enqueue.side_effect = lambda *a, **kw: events.append('enqueue')
            MockTaskQueue.return_value = mock_queue_instance

            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("test-project", mock_logger)

        assert events == [
            'lock_acquired', 'status_read', 'set_status', 'enqueue', 'lock_released'
        ], events

    @pytest.mark.asyncio
    async def test_a_contended_acquire_defers_instead_of_queuing_a_duplicate(self, mock_logger):
        """THE #169-review regression. The earlier fallback ran the identical
        unserialized sequence on ANY refusal, which left the race wide open for
        the racer the lock was added to stop: A wins the lock, B is refused, B
        reads UNVERIFIED before A's IN_PROGRESS write lands, and both enqueue.
        A live holder is either that winner (queuing on our behalf) or a
        build/verify session (a setup already running) -- both mean stop."""
        from services.dev_container_state import DevContainerStatus

        with _build_lock(False, reason="locked_by_issue_-4242"), \
             _build_lock_row(held=True, holder_is_live=True), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status_and_updated_at.return_value = (
                DevContainerStatus.UNVERIFIED, None
            )
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("test-project", mock_logger)

        # Nothing read, nothing marked, nothing queued -- the loser stops.
        mock_state.get_status_and_updated_at.assert_not_called()
        mock_state.set_status.assert_not_called()
        mock_queue_instance.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_retained_lock_is_not_treated_as_a_live_holder(self, mock_logger):
        """`locked_by_issue_N_failed` is a durable marker left for deliberate
        human recovery -- its 'holder' is a run that already ended. Deferring to
        it would be a NEW way for a project to never get a setup queued."""
        from services.dev_container_state import DevContainerStatus

        with _build_lock(False, reason="locked_by_issue_42_failed"), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status_and_updated_at.return_value = (
                DevContainerStatus.UNVERIFIED, None
            )
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("test-project", mock_logger)

        mock_queue_instance.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_refused_acquire_falls_back_rather_than_never_queuing(self, mock_logger):
        """A degraded/fail-closed lock store refuses the acquire without any
        build being in flight -- and probe 3 of _dev_setup_in_flight_reason()
        deliberately does not treat it as a live run. Skipping outright there
        would be a NEW way for a project to never get a setup queued, so the
        fallback keeps the previous (unserialized) behaviour and says so.
        Contrast test_a_contended_acquire_defers_instead_of_queuing_a_duplicate:
        the two refusals call for opposite behaviour, which is why the acquire
        surfaces its reason (#169 review)."""
        from services.dev_container_state import DevContainerStatus

        with _build_lock(False), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status_and_updated_at.return_value = (
                DevContainerStatus.UNVERIFIED, None
            )
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("test-project", mock_logger)

        mock_queue_instance.enqueue.assert_called_once()
        assert any(
            "WITHOUT its dev_container_build lock" in str(call)
            for call in mock_logger.warning.call_args_list
        )

    @pytest.mark.asyncio
    async def test_holding_the_lock_suppresses_the_probe_that_would_find_it(self, mock_logger):
        """_dev_setup_in_flight_reason()'s probe 3 asks whether the
        dev_container_build lock is held. Run from inside the hold it would find
        this call's OWN lock and report a build that is not running, turning the
        stale-IN_PROGRESS recovery into a permanent no-op."""
        from datetime import datetime, timedelta
        from agents.orchestrator_integration import STALE_IN_PROGRESS_MINUTES
        from services.dev_container_state import DevContainerStatus

        held = Mock()
        held.retained_reason = None
        held.lock_acquired_at = '2026-01-01T00:00:00+00:00'

        tracker = MagicMock()
        tracker.load_state.return_value = {'execution_history': []}
        facade = MagicMock()
        facade.get_resource_lock.return_value = held

        with _build_lock(True), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue, \
             patch('services.work_execution_state.work_execution_tracker', tracker), \
             patch('services.project_resource_lock_manager.ProjectResourceLockManager',
                   return_value=facade):

            mock_state.get_status_and_updated_at.return_value = (
                DevContainerStatus.IN_PROGRESS,
                datetime.now() - timedelta(minutes=STALE_IN_PROGRESS_MINUTES + 1),
            )
            mock_queue_instance = Mock()
            mock_queue_instance.get_pending_tasks.return_value = []
            MockTaskQueue.return_value = mock_queue_instance

            from agents.orchestrator_integration import queue_dev_environment_setup

            await queue_dev_environment_setup("test-project", mock_logger)

        facade.get_resource_lock.assert_not_called()
        mock_queue_instance.enqueue.assert_called_once()


# ---------------------------------------------------------------------------
# process_task_integrated: NonRetryableAgentError tests
# ---------------------------------------------------------------------------

class TestProcessTaskIntegratedValidation:
    """Tests that process_task_integrated raises NonRetryableAgentError on blocked tasks."""

    @pytest.mark.asyncio
    async def test_raises_non_retryable_when_needs_dev_setup(self, mock_task, mock_logger):
        """When validation fails with needs_dev_setup, should raise NonRetryableAgentError."""
        from agents.non_retryable import NonRetryableAgentError

        with patch('agents.orchestrator_integration.validate_task_can_run', new_callable=AsyncMock) as mock_validate, \
             patch('agents.orchestrator_integration.queue_dev_environment_setup', new_callable=AsyncMock) as mock_queue, \
             patch('config.manager.config_manager') as mock_config, \
             patch('monitoring.observability.get_observability_manager'), \
             patch('monitoring.decision_events.DecisionEventEmitter') as mock_emitter_cls:

            mock_validate.return_value = {
                'can_run': False,
                'reason': 'Dev container not yet verified',
                'needs_dev_setup': True
            }
            mock_config.get_project_config.return_value = Mock(pipelines=[])
            mock_emitter_cls.return_value = Mock()

            from agents.orchestrator_integration import process_task_integrated

            with pytest.raises(NonRetryableAgentError, match="Task blocked"):
                await process_task_integrated(mock_task, Mock(), mock_logger)

            # Should still queue dev setup before raising
            mock_queue.assert_awaited_once_with(mock_task.project, mock_logger)

    @pytest.mark.asyncio
    async def test_raises_non_retryable_when_blocked_no_dev_setup(self, mock_task, mock_logger):
        """When validation fails without needs_dev_setup, should still raise NonRetryableAgentError."""
        from agents.non_retryable import NonRetryableAgentError

        with patch('agents.orchestrator_integration.validate_task_can_run', new_callable=AsyncMock) as mock_validate, \
             patch('config.manager.config_manager') as mock_config, \
             patch('monitoring.observability.get_observability_manager'), \
             patch('monitoring.decision_events.DecisionEventEmitter') as mock_emitter_cls:

            mock_validate.return_value = {
                'can_run': False,
                'reason': 'Dev container setup is blocked',
                'needs_dev_setup': False
            }
            mock_config.get_project_config.return_value = Mock(pipelines=[])
            mock_emitter_cls.return_value = Mock()

            from agents.orchestrator_integration import process_task_integrated

            with pytest.raises(NonRetryableAgentError, match="Task blocked"):
                await process_task_integrated(mock_task, Mock(), mock_logger)

    @pytest.mark.asyncio
    async def test_raises_non_retryable_even_when_queue_fails(self, mock_task, mock_logger):
        """If queue_dev_environment_setup fails, should still raise NonRetryableAgentError (not the queue error)."""
        from agents.non_retryable import NonRetryableAgentError

        with patch('agents.orchestrator_integration.validate_task_can_run', new_callable=AsyncMock) as mock_validate, \
             patch('agents.orchestrator_integration.queue_dev_environment_setup', new_callable=AsyncMock) as mock_queue, \
             patch('config.manager.config_manager') as mock_config, \
             patch('monitoring.observability.get_observability_manager'), \
             patch('monitoring.decision_events.DecisionEventEmitter') as mock_emitter_cls:

            mock_validate.return_value = {
                'can_run': False,
                'reason': 'Dev container not yet verified',
                'needs_dev_setup': True
            }
            mock_queue.side_effect = ConnectionError("Redis unavailable")
            mock_config.get_project_config.return_value = Mock(pipelines=[])
            mock_emitter_cls.return_value = Mock()

            from agents.orchestrator_integration import process_task_integrated

            # Should raise NonRetryableAgentError, NOT ConnectionError
            with pytest.raises(NonRetryableAgentError, match="Task blocked"):
                await process_task_integrated(mock_task, Mock(), mock_logger)

    def test_non_retryable_error_is_runtime_error(self):
        """NonRetryableAgentError is a RuntimeError so worker_pool can distinguish it."""
        from agents.non_retryable import NonRetryableAgentError
        assert issubclass(NonRetryableAgentError, RuntimeError)
        err = NonRetryableAgentError("test")
        assert isinstance(err, NonRetryableAgentError)
        assert isinstance(err, RuntimeError)


class TestChangesNeededHasAStalenessEscape:
    """
    #152 review: CHANGES_NEEDED deliberately returns needs_dev_setup=False
    because the repair cycle's env-rebuild sub-cycle owns retrying it. It is
    therefore the ONE status whose retry has a single owner, so any path that
    stops that sub-cycle without completing its terminal CHANGES_NEEDED ->
    BLOCKED transition (a crash, or a dev_container_build lock it could not
    take) left the project refusing every future task forever, with the
    operator-facing reason still claiming a retry was under way.
    """

    def _task(self):
        task = Mock()
        task.id = "t-1"
        task.agent = "senior_software_engineer"
        task.project = "test-project"
        return task

    async def _validate(self, mock_logger, updated_at):
        from services.dev_container_state import DevContainerStatus

        with patch('config.manager.config_manager') as mock_config, \
             patch('services.dev_container_state.dev_container_state') as mock_state:

            mock_config.get_project_agent_config.return_value = Mock(requires_dev_container=True)
            mock_state.get_status.return_value = DevContainerStatus.CHANGES_NEEDED
            mock_state.get_status_updated_at.return_value = updated_at
            _status_snapshot_from_stubs(mock_state)

            from agents.orchestrator_integration import validate_task_can_run
            return await validate_task_can_run(self._task(), mock_logger)

    @pytest.mark.asyncio
    async def test_a_live_sub_cycle_is_still_left_to_own_the_retry(self, mock_logger):
        """A healthy sub-cycle polls every 30s and resets to UNVERIFIED at the
        top of its next attempt, so a recent CHANGES_NEEDED must NOT trigger a
        second, redundant queue_dev_environment_setup() racing it."""
        from datetime import datetime

        result = await self._validate(mock_logger, datetime.now())

        assert result['can_run'] is False
        assert result['needs_dev_setup'] is False

    @pytest.mark.asyncio
    async def test_a_stale_changes_needed_re_triggers_setup(self, mock_logger):
        from datetime import datetime, timedelta
        from agents.orchestrator_integration import STALE_CHANGES_NEEDED_MINUTES

        result = await self._validate(
            mock_logger,
            datetime.now() - timedelta(minutes=STALE_CHANGES_NEEDED_MINUTES + 1),
        )

        assert result['can_run'] is False
        assert result['needs_dev_setup'] is True

    @pytest.mark.asyncio
    async def test_an_unreadable_timestamp_keeps_the_sub_cycle_as_owner(self, mock_logger):
        result = await self._validate(mock_logger, None)

        assert result['needs_dev_setup'] is False

    @pytest.mark.asyncio
    async def test_the_window_is_wider_than_a_healthy_sub_cycle_poll(self, mock_logger):
        """The escape must not fire on a sub-cycle that is simply between polls."""
        from agents.orchestrator_integration import (
            STALE_CHANGES_NEEDED_MINUTES,
            STALE_IN_PROGRESS_MINUTES,
        )

        assert STALE_CHANGES_NEEDED_MINUTES >= STALE_IN_PROGRESS_MINUTES


class TestTheOutcomeIsReportedToCallers:
    """#169 review. queue_dev_environment_setup() has four ways to return
    without enqueuing anything, and it used to report all of them exactly as it
    reports success: by returning None. Neither caller could tell, and both
    assumed the good one -- the dispatch path emitted "setup has been queued ...
    auto_queued: True" on every pass, and repair_cycle's env-rebuild sub-cycle
    followed the call with an unbounded poll for a terminal status nobody was
    going to write."""

    @pytest.mark.asyncio
    async def test_a_successful_queue_says_queued(self, mock_logger):
        from services.dev_container_state import DevContainerStatus
        from agents.orchestrator_integration import (
            DevSetupQueueOutcome,
            queue_dev_environment_setup,
        )

        with _build_lock(True), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status_and_updated_at.return_value = (
                DevContainerStatus.UNVERIFIED, None
            )
            mock_state.set_status.return_value = True
            MockTaskQueue.return_value = Mock()

            outcome = await queue_dev_environment_setup("test-project", mock_logger)

        assert outcome is DevSetupQueueOutcome.QUEUED
        assert outcome.queued is True

    @pytest.mark.asyncio
    async def test_a_contended_acquire_says_deferred_not_queued(self, mock_logger):
        from services.dev_container_state import DevContainerStatus
        from agents.orchestrator_integration import (
            DevSetupQueueOutcome,
            queue_dev_environment_setup,
        )

        with _build_lock(False, reason="locked_by_issue_-4242"), \
             _build_lock_row(held=True, holder_is_live=True), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status_and_updated_at.return_value = (
                DevContainerStatus.UNVERIFIED, None
            )
            MockTaskQueue.return_value = Mock()

            outcome = await queue_dev_environment_setup("test-project", mock_logger)

        assert outcome is DevSetupQueueOutcome.DEFERRED_BUILD_LOCK_HELD
        assert outcome.queued is False

    @pytest.mark.asyncio
    async def test_a_fresh_in_progress_says_deferred_not_queued(self, mock_logger):
        from datetime import datetime
        from services.dev_container_state import DevContainerStatus
        from agents.orchestrator_integration import (
            DevSetupQueueOutcome,
            queue_dev_environment_setup,
        )

        with _build_lock(True), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status_and_updated_at.return_value = (
                DevContainerStatus.IN_PROGRESS, datetime.now()
            )
            MockTaskQueue.return_value = Mock()

            outcome = await queue_dev_environment_setup("test-project", mock_logger)

        assert outcome is DevSetupQueueOutcome.DEFERRED_IN_PROGRESS
        assert outcome.queued is False

    @pytest.mark.asyncio
    async def test_a_live_run_found_behind_a_stale_mark_says_deferred(self, mock_logger):
        from datetime import datetime, timedelta
        from services.dev_container_state import DevContainerStatus
        from agents.orchestrator_integration import (
            STALE_IN_PROGRESS_MINUTES,
            DevSetupQueueOutcome,
            queue_dev_environment_setup,
        )

        with _build_lock(True), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('agents.orchestrator_integration._dev_setup_in_flight_reason',
                   return_value="a dev_environment_setup task (abc) is still queued"), \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status_and_updated_at.return_value = (
                DevContainerStatus.IN_PROGRESS,
                datetime.now() - timedelta(minutes=STALE_IN_PROGRESS_MINUTES + 1),
            )
            MockTaskQueue.return_value = Mock()

            outcome = await queue_dev_environment_setup("test-project", mock_logger)

        assert outcome is DevSetupQueueOutcome.DEFERRED_RUN_IN_FLIGHT
        assert outcome.queued is False


class TestAFailedInProgressMarkBlocksTheQueue:
    """THE #171-review regression. set_status() swallows its own write errors --
    _merge_state()'s file_lock acquire timing out at STATE_LOCK_TIMEOUT_SECONDS
    against the verifier's in-session write or scripts/rebuild_project_images.py
    becomes an ERROR log and a False -- and this function used to ignore the
    result and enqueue anyway.

    That is the duplicate hour-scale rebuild this whole critical section exists
    to prevent, reached from a third direction: the state file still says
    UNVERIFIED, so the next 30s board poll re-decides needs_dev_setup, takes the
    now-free build lock, reads UNVERIFIED (so the stale/in-flight probes never
    run) and queues a SECOND setup."""

    @pytest.mark.asyncio
    async def test_a_failed_mark_queues_nothing(self, mock_logger):
        from services.dev_container_state import DevContainerStatus
        from agents.orchestrator_integration import (
            DevSetupQueueOutcome,
            queue_dev_environment_setup,
        )

        with _build_lock(True), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status_and_updated_at.return_value = (
                DevContainerStatus.UNVERIFIED, None
            )
            mock_state.set_status.return_value = False
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            outcome = await queue_dev_environment_setup("test-project", mock_logger)

        mock_queue_instance.enqueue.assert_not_called()
        assert outcome is DevSetupQueueOutcome.FAILED_STATUS_WRITE
        assert outcome.queued is False

    @pytest.mark.asyncio
    async def test_a_failed_mark_does_not_claim_it_wrote_one(self, mock_logger):
        """The "Set dev container status to IN_PROGRESS" line was unconditional,
        so a swallowed write produced a flatly false log statement -- the only
        operator-facing record of a mark that never landed."""
        from services.dev_container_state import DevContainerStatus
        from agents.orchestrator_integration import queue_dev_environment_setup

        with _build_lock(True), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status_and_updated_at.return_value = (
                DevContainerStatus.UNVERIFIED, None
            )
            mock_state.set_status.return_value = False
            MockTaskQueue.return_value = Mock()

            await queue_dev_environment_setup("test-project", mock_logger)

        info_lines = " ".join(str(c) for c in mock_logger.info.call_args_list)
        assert "Set dev container status to IN_PROGRESS" not in info_lines
        assert mock_logger.error.called


class TestAnAbandonedBuildLockDoesNotBlockSetupForever:
    """#169 review, second pass. The contention short-circuit trusts the acquire's
    refusal REASON, which names the holder recorded in the row -- not one observed
    to be running. A row outlives its holder in a way nothing in this process
    cleans up: when release_resource() returns SERIALIZATION_FAILED the release
    did not complete and the holder id it was for is known to nobody, so the row
    leaks until the Redis TTL or the 4-hour staleness heuristic (see
    project_checkout_lock._release_and_warn).

    That row reads as `locked_by_issue_<n>` with no `_failed` suffix -- genuine
    contention by acquire_failure_is_contention()'s definition -- and startup's
    orphan recovery only runs at startup. So every dispatch for the project hit
    the short-circuit, queued nothing, and reported `success=True,
    recovery_action='deferred_to_existing_dev_setup'` every 30s board poll, for
    hours, deferring to a setup that did not exist and could not be started.

    The evidence that tells a leaked row from a busy one is the heartbeat every
    holder of this lock ticks while it works."""

    @pytest.mark.asyncio
    async def test_an_abandoned_row_falls_through_and_queues(self, mock_logger):
        """THE regression."""
        from services.dev_container_state import DevContainerStatus
        from agents.orchestrator_integration import (
            DevSetupQueueOutcome,
            queue_dev_environment_setup,
        )

        with _build_lock(False, reason="locked_by_issue_-4242"), \
             _build_lock_row(held=True, holder_is_live=False), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status_and_updated_at.return_value = (
                DevContainerStatus.UNVERIFIED, None
            )
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            outcome = await queue_dev_environment_setup("test-project", mock_logger)

        assert outcome is DevSetupQueueOutcome.QUEUED
        mock_queue_instance.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_abandoned_row_is_reported_at_warning(self, mock_logger):
        """An operator has to be able to find the leaked lock; the fall-through
        is a workaround for it, not a fix."""
        from services.dev_container_state import DevContainerStatus
        from agents.orchestrator_integration import queue_dev_environment_setup

        with _build_lock(False, reason="locked_by_issue_-4242"), \
             _build_lock_row(held=True, holder_is_live=False), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status_and_updated_at.return_value = (
                DevContainerStatus.UNVERIFIED, None
            )
            MockTaskQueue.return_value = Mock()

            await queue_dev_environment_setup("test-project", mock_logger)

        warnings = " ".join(str(call) for call in mock_logger.warning.call_args_list)
        assert "abandoned rather than busy" in warnings
        assert "WITHOUT its dev_container_build lock" in warnings

    @pytest.mark.asyncio
    async def test_a_live_holder_is_still_deferred_to(self, mock_logger):
        """The other side. A holder that is heartbeating is either the winner of
        this exact race (queuing on our behalf) or a build/verify session -- both
        mean stop, and this must not have become a way to queue duplicates."""
        from services.dev_container_state import DevContainerStatus
        from agents.orchestrator_integration import (
            DevSetupQueueOutcome,
            queue_dev_environment_setup,
        )

        with _build_lock(False, reason="locked_by_issue_-4242"), \
             _build_lock_row(held=True, holder_is_live=True), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status_and_updated_at.return_value = (
                DevContainerStatus.UNVERIFIED, None
            )
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            outcome = await queue_dev_environment_setup("test-project", mock_logger)

        assert outcome is DevSetupQueueOutcome.DEFERRED_BUILD_LOCK_HELD
        mock_queue_instance.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_lock_store_that_cannot_be_read_still_fails_closed(self, mock_logger):
        """Re-queueing wrongly costs an hour-scale redundant agent run; skipping
        wrongly costs one 30-second sweep. An unreadable store takes the cheap
        side, exactly as the other probes do."""
        from services.dev_container_state import DevContainerStatus
        from agents.orchestrator_integration import (
            DevSetupQueueOutcome,
            queue_dev_environment_setup,
        )

        with _build_lock(False, reason="locked_by_issue_-4242"), \
             patch('services.project_resource_lock_manager.ProjectResourceLockManager',
                   side_effect=RuntimeError("redis down")), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status_and_updated_at.return_value = (
                DevContainerStatus.UNVERIFIED, None
            )
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            outcome = await queue_dev_environment_setup("test-project", mock_logger)

        assert outcome is DevSetupQueueOutcome.DEFERRED_BUILD_LOCK_HELD
        mock_queue_instance.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_row_released_in_the_meantime_is_not_deferred_to(self, mock_logger):
        """The acquire lost a race that was over by the time the row was read.
        Whoever won it holds no lock now, and the check-then-mark below reads the
        IN_PROGRESS they wrote under it -- so falling through costs nothing and
        deferring to a hold that has ended costs a poll."""
        from datetime import datetime
        from services.dev_container_state import DevContainerStatus
        from agents.orchestrator_integration import (
            DevSetupQueueOutcome,
            queue_dev_environment_setup,
        )

        with _build_lock(False, reason="locked_by_issue_-4242"), \
             _build_lock_row(held=False), \
             patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status_and_updated_at.return_value = (
                DevContainerStatus.IN_PROGRESS, datetime.now()
            )
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            outcome = await queue_dev_environment_setup("test-project", mock_logger)

        assert outcome is DevSetupQueueOutcome.DEFERRED_IN_PROGRESS
        mock_queue_instance.enqueue.assert_not_called()


class TestProbeThreeAsksTheSameLivenessQuestion:
    """_dev_setup_in_flight_reason()'s probe 3 had the identical blindness --
    `lock and not lock.retained_reason` with no liveness test -- and it is the
    probe that keeps a stale IN_PROGRESS from ever being re-queued. Both call
    sites now route through _live_build_lock_reason(), so the fix is one
    judgement rather than two that can drift."""

    def test_an_abandoned_row_is_not_a_live_run(self, mock_logger):
        from agents.orchestrator_integration import _live_build_lock_reason

        with _build_lock_row(held=True, holder_is_live=False):
            assert _live_build_lock_reason("test-project", mock_logger) is None

    def test_a_heartbeating_row_is(self, mock_logger):
        from agents.orchestrator_integration import _live_build_lock_reason

        with _build_lock_row(held=True, holder_is_live=True):
            reason = _live_build_lock_reason("test-project", mock_logger)

        assert reason is not None
        assert "a build is running" in reason

    def test_a_retained_row_is_not_a_live_run_and_is_not_probed_for_liveness(
        self, mock_logger
    ):
        """A retained marker's 'holder' is a run that already ended, so its
        liveness is not a question worth asking."""
        from agents.orchestrator_integration import _live_build_lock_reason

        with _build_lock_row(held=True, retained_reason="build blew up") as facade:
            assert _live_build_lock_reason("test-project", mock_logger) is None

        facade.holder_liveness_is_fresh.assert_not_called()

    def test_an_unreadable_store_is_reported_as_uncheckable(self, mock_logger):
        from agents.orchestrator_integration import _live_build_lock_reason

        with patch('services.project_resource_lock_manager.ProjectResourceLockManager',
                   side_effect=RuntimeError("redis down")):
            reason = _live_build_lock_reason("test-project", mock_logger)

        assert reason == "the dev_container_build lock could not be checked"
