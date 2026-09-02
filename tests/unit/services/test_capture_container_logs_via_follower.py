"""
Tests for services/project_monitor.py's _capture_container_logs_via_follower.

Added as part of PR #115's follow-up fix: the original container-log-capture
implementation issued a single `docker logs` call AFTER `docker wait`
returned, which reliably lost the race against the repair-cycle container's
own `--rm` auto-removal (verified empirically: 5/5 runs hit "No such
container" on a normal Docker host when logs were fetched post-wait). This
helper instead attaches via `docker logs -f` as early as possible (started
before `docker wait` in the caller) and streams output into a bounded,
thread-safe buffer, so the race is against "how fast can this thread start
and the docker CLI connect" rather than "the full container runtime plus
--rm cleanup".
"""
import subprocess
from unittest.mock import MagicMock, patch

from services.project_monitor import _capture_container_logs_via_follower


def _fake_popen(lines, wait_return=0):
    """Stand in for subprocess.Popen(['docker', 'logs', '-f', ...]): .stdout
    iterates the given lines (as `docker logs -f` would stream them), .wait()
    returns once the stream is exhausted (as it would once the container
    stops producing output)."""
    mock_proc = MagicMock()
    mock_proc.stdout = iter(lines)
    mock_proc.wait.return_value = wait_return
    return mock_proc


class TestCaptureContainerLogsViaFollower:
    def test_captures_streamed_output_and_attaches_correctly(self):
        with patch('services.project_monitor.subprocess.Popen') as mock_popen:
            mock_popen.return_value = _fake_popen(["line1\n", "line2\n", "line3\n"])

            thread, get_captured = _capture_container_logs_via_follower("my-container")
            thread.join(timeout=5)

        assert get_captured() == "line1\nline2\nline3\n"
        mock_popen.assert_called_once_with(
            ['docker', 'logs', '-f', 'my-container'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

    def test_bounds_buffer_to_max_lines(self):
        lines = [f"line{i}\n" for i in range(150)]
        with patch('services.project_monitor.subprocess.Popen') as mock_popen:
            mock_popen.return_value = _fake_popen(lines)

            thread, get_captured = _capture_container_logs_via_follower(
                "my-container", max_lines=100
            )
            thread.join(timeout=5)

        captured_lines = get_captured().splitlines(keepends=True)
        assert len(captured_lines) == 100
        # Bounded to the *last* 100 lines, not the first 100.
        assert captured_lines[0] == "line50\n"
        assert captured_lines[-1] == "line149\n"

    def test_popen_failure_returns_empty_capture_without_raising(self):
        """docker binary missing, daemon unreachable, container already gone
        before the follower even attaches, etc. -- must degrade to an empty
        buffer, never raise into the caller."""
        with patch('services.project_monitor.subprocess.Popen') as mock_popen:
            mock_popen.side_effect = FileNotFoundError("docker: command not found")

            thread, get_captured = _capture_container_logs_via_follower("my-container")
            thread.join(timeout=5)

        assert get_captured() == ""

    def test_returns_immediately_with_thread_already_started(self):
        """The caller (project_monitor.py's monitor_thread) starts this
        follower BEFORE `docker wait`, specifically so it's already attached
        by the time the container could plausibly exit -- this only works if
        the thread is started synchronously within this call, not lazily."""
        with patch('services.project_monitor.subprocess.Popen') as mock_popen:
            mock_popen.return_value = _fake_popen(["line1\n"])

            thread, _ = _capture_container_logs_via_follower("my-container")

            # .ident is only assigned once start() has actually run -- confirms
            # the thread was started synchronously by the function itself,
            # not merely constructed for the caller to start later.
            assert thread.ident is not None
        assert thread.daemon is True
        thread.join(timeout=5)
