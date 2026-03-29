"""
Unit tests for the feedback loop active stop signal mechanism.

Tests request_stop(), _stop_events lifecycle, and cleanup_loop() integration.
The stop event logic itself does not require Docker, but these tests are skipped
outside Docker because importing HumanFeedbackLoopExecutor transitively imports
modules that require the container environment.
"""

import threading
import pytest

import os
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from services.human_feedback_loop import HumanFeedbackLoopExecutor


class TestRequestStop:
    """Tests for HumanFeedbackLoopExecutor.request_stop()"""

    def test_request_stop_sets_registered_event(self):
        """request_stop() should set the event when one is registered."""
        executor = HumanFeedbackLoopExecutor()
        event = threading.Event()
        executor._stop_events["myproject:42"] = event

        result = executor.request_stop("myproject", 42)

        assert result is True
        assert event.is_set()

    def test_request_stop_returns_false_when_no_event(self):
        """request_stop() should return False when no event is registered."""
        executor = HumanFeedbackLoopExecutor()

        result = executor.request_stop("myproject", 42)

        assert result is False

    def test_request_stop_only_sets_matching_event(self):
        """request_stop() should only set the event for the matching key."""
        executor = HumanFeedbackLoopExecutor()
        event_a = threading.Event()
        event_b = threading.Event()
        executor._stop_events["projA:1"] = event_a
        executor._stop_events["projB:2"] = event_b

        executor.request_stop("projA", 1)

        assert event_a.is_set()
        assert not event_b.is_set()

    def test_request_stop_is_idempotent(self):
        """Calling request_stop() twice should not raise."""
        executor = HumanFeedbackLoopExecutor()
        event = threading.Event()
        executor._stop_events["proj:10"] = event

        assert executor.request_stop("proj", 10) is True
        assert executor.request_stop("proj", 10) is True
        assert event.is_set()


class TestCleanupLoopStopIntegration:
    """Tests that cleanup_loop() also sends the stop signal."""

    def test_cleanup_loop_signals_stop_event(self):
        """cleanup_loop() should set the stop event for the issue."""
        executor = HumanFeedbackLoopExecutor()
        event = threading.Event()
        executor._stop_events["myproject:42"] = event

        # Add active loop state so cleanup_loop has something to clean
        from services.human_feedback_loop import HumanFeedbackState
        state = HumanFeedbackState(
            issue_number=42,
            repository="my-repo",
            agent="software_architect",
            project_name="myproject",
            board_name="dev",
        )
        executor.active_loops[executor._loop_key("myproject", 42)] = state

        executor.cleanup_loop("myproject", 42, reason="test")

        assert event.is_set()

    def test_cleanup_loop_without_stop_event_does_not_raise(self):
        """cleanup_loop() should not fail if no stop event is registered."""
        executor = HumanFeedbackLoopExecutor()

        # No stop event registered, no active loop — should return False without error
        result = executor.cleanup_loop("myproject", 42, reason="test")

        assert result is False


class TestStopEventsInit:
    """Tests that _stop_events is properly initialized."""

    def test_stop_events_initialized_as_empty_dict(self):
        executor = HumanFeedbackLoopExecutor()
        assert executor._stop_events == {}
        assert isinstance(executor._stop_events, dict)
