"""
Queuing work for a project with no config must name its caller.

Such a task can never succeed -- every agent resolves its project config first
-- so it fails with "Configuration file not found" after three attempts and 15
seconds of a real worker. Before this, the only log came from the worker that
dequeued it, which knows nothing about where it came from, and there was no
enqueue-side log at all.

That gap cost a long investigation on the live deployment: 94 tasks for a
project named `test-project` over four hours, at a steady rate, surviving a
restart, with the Redis queue never holding one when sampled. Every structural
guess about the source was wrong.
"""

import logging
import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import patch

from task_queue.task_manager import TaskQueue, Task, TaskPriority


def _task(project, task_id='probe'):
    return Task(
        id=task_id, agent='dev_environment_setup', project=project,
        priority=TaskPriority.HIGH, context={},
        created_at='2026-01-01T00:00:00Z',
    )


@pytest.fixture
def queue():
    q = TaskQueue()
    assert q.redis_client is None, "conftest guard should keep this off Redis"
    return q


class TestAnUnconfiguredProjectIsReported:
    def test_it_warns_and_names_the_project_and_task(self, queue, caplog):
        with caplog.at_level(logging.WARNING, logger='task_queue.task_manager'):
            with patch('config.manager.config_manager.list_projects',
                       return_value=['heimdall']):
                queue.enqueue(_task('test-project', task_id='abc123'))

        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert 'test-project' in message
        assert 'abc123' in message
        assert 'dev_environment_setup' in message

    def test_the_stack_is_included_so_the_caller_is_identifiable(self, queue, caplog):
        """The whole point. Without this the warning says a bad task exists,
        which the worker's own failure already said."""
        with caplog.at_level(logging.WARNING, logger='task_queue.task_manager'):
            with patch('config.manager.config_manager.list_projects',
                       return_value=['heimdall']):
                queue.enqueue(_task('test-project'))

        assert caplog.records[0].stack_info, (
            "no stack recorded; the warning cannot identify who queued the task"
        )
        assert 'test_the_stack_is_included' in caplog.records[0].stack_info

    def test_the_task_is_still_enqueued(self, queue):
        """A diagnostic, not a new rejection path. Changing the dispatch
        behaviour of a live orchestrator is not what this is for."""
        with patch('config.manager.config_manager.list_projects',
                   return_value=['heimdall']):
            queue.enqueue(_task('test-project'))

        assert not queue.fallback_queues[TaskPriority.HIGH].empty()


class TestTheNormalPathIsUntouched:
    def test_a_configured_project_logs_nothing(self, queue, caplog):
        with caplog.at_level(logging.WARNING, logger='task_queue.task_manager'):
            with patch('config.manager.config_manager.list_projects',
                       return_value=['heimdall']):
                queue.enqueue(_task('heimdall'))

        assert caplog.records == []

    def test_a_broken_config_directory_does_not_break_enqueue(self, queue):
        """A diagnostic that can take down the dispatch path is worse than no
        diagnostic."""
        with patch('config.manager.config_manager.list_projects',
                   side_effect=OSError("config directory unreadable")):
            queue.enqueue(_task('anything'))

        assert not queue.fallback_queues[TaskPriority.HIGH].empty()
