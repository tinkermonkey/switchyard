"""
Regression tests for #148: WorkerPool's retry loop must treat a project
resource-lock timeout as a distinct, non-retryable outcome.

TaskWorker.run() wraps process_task_integrated() in its own retry loop
(max_retries=3, i.e. 4 attempts) — a peer of the inner loop in
services/agent_executor.py, which agents/non_retryable.py's docstring already
names as a site that has to carry the same exemptions. It is also the live
dispatch path whenever ORCHESTRATOR_WORKERS > 1 (main.py starts the pool).

Without the exemption, every one of those four attempts re-enters
run_claude_code() and re-runs the lock's entire (~3h / ~1h) poll for an outcome
only a different holder finishing can change: one contention event becomes ~12h
of wall clock with a worker slot pinned throughout, which is the compounding
#148 exists to eliminate. These tests fail without it by observing
process_task_integrated called 4 times instead of 1.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch

from services.worker_pool import TaskWorker
from services.cancellation import CancellationError
from agents.non_retryable import NonRetryableAgentError
from monitoring.claude_code_breaker import ClaudeCodeRateLimitError
from services.project_checkout_lock import ProjectCheckoutLockTimeoutError
from services.dev_container_build_lock import DevContainerBuildLockTimeoutError


def _task():
    task = MagicMock()
    task.id = 'task-1'
    task.agent = 'business_analyst'
    task.project = 'test-project'
    task.context = {}
    return task


async def _run_worker_with_error(error):
    """
    Drive one pass of TaskWorker.run() with process_task_integrated raising
    `error`, and return the mock so the caller can count attempts.

    The queue hands out exactly one task and then stops the worker, so run()
    returns instead of looping forever. asyncio.sleep is stubbed so the loop's
    5s retry backoff doesn't make a without-the-fix failure take 15 seconds.
    """
    worker = TaskWorker(
        worker_id=1,
        task_queue=MagicMock(),
        metrics=MagicMock(),
        orchestrator_logger=MagicMock(),
    )

    dequeued = []

    def dequeue():
        if dequeued:
            worker.running = False
            return None
        dequeued.append(True)
        return _task()

    worker.task_queue.dequeue.side_effect = dequeue

    with patch('services.worker_pool.process_task_integrated',
               new=AsyncMock(side_effect=error)) as mock_process, \
         patch('services.worker_pool.asyncio.sleep', new=AsyncMock()):
        await worker.run()

    return worker, mock_process


class TestLockTimeoutIsNotRetried:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_cls", [ProjectCheckoutLockTimeoutError, DevContainerBuildLockTimeoutError]
    )
    async def test_lock_timeout_is_attempted_exactly_once(self, error_cls):
        worker, mock_process = await _run_worker_with_error(
            error_cls("could not acquire lock within 10900.0s")
        )

        assert mock_process.call_count == 1
        assert worker.tasks_failed == 1

    @pytest.mark.asyncio
    async def test_wrapped_lock_timeout_is_also_not_retried(self):
        """An agent wrapper that re-raises `Exception(...) from <timeout>` must
        not defeat the exemption — see services/resource_lock_errors.py."""
        inner = ProjectCheckoutLockTimeoutError("busy")
        wrapper = Exception(f"Business Analyst execution failed: {inner}")
        wrapper.__cause__ = inner

        _, mock_process = await _run_worker_with_error(wrapper)

        assert mock_process.call_count == 1


class TestRateLimitIsNotRetried:
    """Same family, same reasoning: agent_executor has already tripped the
    Claude Code breaker and recorded 'frozen' for automatic resume, so retrying
    here only re-dispatches into the now-open breaker."""

    @pytest.mark.asyncio
    async def test_rate_limit_is_attempted_exactly_once(self):
        _, mock_process = await _run_worker_with_error(
            ClaudeCodeRateLimitError("token limit reached")
        )

        assert mock_process.call_count == 1


class TestExistingExemptionsUnchanged:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error", [CancellationError("stopped"), NonRetryableAgentError("exit_code=137")]
    )
    async def test_preexisting_exemptions_still_attempt_once(self, error):
        _, mock_process = await _run_worker_with_error(error)

        assert mock_process.call_count == 1


class TestOrdinaryFailuresStillRetry:
    """Regression guard: the exemptions must be scoped to these families only."""

    @pytest.mark.asyncio
    async def test_ordinary_error_still_uses_all_attempts(self):
        worker, mock_process = await _run_worker_with_error(
            RuntimeError("agent produced garbage")
        )

        assert mock_process.call_count == 4  # 1 initial + max_retries=3
        assert worker.tasks_failed == 1
