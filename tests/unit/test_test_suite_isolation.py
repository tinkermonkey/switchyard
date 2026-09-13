"""
The suite must not reach the running deployment's state.

Inside the orchestrator container -- the documented way to run these tests --
`redis` is the live deployment's Redis, holding its work queue and the
rate-limit mirror its own restart logic reads. Three separate leaks were found
there, and each was invisible from inside the suite because the suite passed
either way:

  1. Enqueued tasks landed on the deployment's tasks:{priority} lists. The live
     workers picked them up and failed, 15 seconds each: "Configuration file not
     found: /app/config/projects/test-project.yaml". 189 such lines in 90
     minutes, in bursts matching suite runs from more than one session.

  2. Mocked-subprocess tests ran the real rate-limit mirror code and wrote
     fabricated readings into the global keys production reads.

  3. Compensating for (2), the session purge DELETED those keys -- destroying
     the deployment's own reading, which #216's cold-start budget pre-flight
     depends on. A restart shortly after any test run found nothing and stood
     its guard down.

The guards live in tests/conftest.py. These tests exist because a guard that
quietly fails to install is indistinguishable from one that is working: the
first version of the task-queue guard named a class that does not exist
(TaskManager, not TaskQueue), the except swallowed the ImportError, and a full
green suite reported nothing.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)


class TestTasksStayOutOfTheDeploymentsQueue:
    def test_a_default_queue_does_not_connect_to_redis(self):
        from task_queue.task_manager import TaskQueue

        q = TaskQueue()

        assert q.use_redis is False
        assert q.redis_client is None, (
            "a TaskQueue holding a real Redis client will enqueue onto the "
            "deployment's tasks:{priority} lists, and its live workers will "
            "run them"
        )

    def test_enqueueing_uses_the_in_memory_fallback(self):
        """The property that matters, at the method that does the damage.

        The precondition is not decoration. An earlier version went straight to
        enqueue(), so when the guard was removed to mutation-test it, this test
        pushed its own probe onto the DEPLOYMENT's queue and a live worker
        spent 15s failing it three times -- a test that pollutes production
        precisely when it is about to report the leak. Assert the client is
        absent first, and a broken guard fails here instead.
        """
        from task_queue.task_manager import TaskQueue, Task, TaskPriority

        q = TaskQueue()
        assert q.redis_client is None, (
            "refusing to enqueue: this queue is backed by a real Redis, so the "
            "probe below would land on the deployment's work queue"
        )

        task = Task(
            id='isolation-probe', agent='business_analyst',
            project='test-project', priority=TaskPriority.HIGH, context={},
            created_at='2026-01-01T00:00:00Z',
        )
        q.enqueue(task)

        assert not q.fallback_queues[TaskPriority.HIGH].empty(), (
            "the task went somewhere other than the in-memory queue"
        )

    def test_a_test_that_wants_redis_can_still_ask(self):
        """The guard fills in an omitted keyword; it does not override one."""
        from task_queue.task_manager import TaskQueue

        assert TaskQueue(use_redis=True).use_redis is True


class TestTheRateLimitMirrorIsThisProcessOnly:
    def test_the_shared_client_is_not_a_real_redis_client(self):
        import services.github_api_client as github_api_client

        client = github_api_client._get_shared_redis_client()

        assert type(client).__name__ != 'Redis', (
            "the rate-limit mirror is pointed at a real Redis; writes from "
            "this suite will land in the keys the deployment reads"
        )

    def test_a_write_here_is_not_visible_to_anything_else(self):
        """Round-trips through the real production code path, then proves the
        value stayed in-process."""
        import json
        import services.github_api_client as github_api_client
        from services.github_api_client import RATE_LIMIT_REDIS_KEYS

        key = RATE_LIMIT_REDIS_KEYS['graphql']
        github_api_client._get_shared_redis_client().set(
            key, json.dumps({'remaining': 4242, 'limit': 5000})
        )

        # readable here...
        stored = json.loads(github_api_client._get_shared_redis_client().get(key))
        assert stored['remaining'] == 4242

        # ...and absent from the real server, if one is even reachable.
        try:
            import redis as redis_lib
            real = redis_lib.Redis(
                host=os.environ.get('REDIS_HOST', 'redis'), port=6379,
                decode_responses=True, socket_timeout=2,
            )
            raw = real.get(key)
        except Exception:
            pytest.skip("no reachable Redis to prove non-leakage against")

        if raw is not None:
            assert json.loads(raw).get('remaining') != 4242, (
                "this suite's fabricated reading reached the deployment's "
                "rate-limit mirror"
            )

    def test_the_session_purge_no_longer_deletes_the_mirror(self):
        """#216's cold-start pre-flight reads these keys; deleting them is how
        it silently stood down.

        Walks the AST rather than grepping the source. The first version of
        this test did the latter and PASSED with the delete put straight back
        -- comments in the function mention the key names, so any substring
        test is answering a question about prose. This asks whether a delete()
        call takes RATE_LIMIT_REDIS_KEYS as an argument, which is the thing
        that does the damage.
        """
        import ast
        import inspect
        import textwrap
        import tests.conftest as conftest

        tree = ast.parse(textwrap.dedent(inspect.getsource(conftest._purge_redis)))

        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == 'delete'):
                continue
            args = node.args + [a.value for a in node.keywords if a.value]
            for arg in args:
                referenced = {
                    n.id for n in ast.walk(arg) if isinstance(n, ast.Name)
                } | {
                    n.attr for n in ast.walk(arg) if isinstance(n, ast.Attribute)
                }
                if any(name.startswith('RATE_LIMIT_REDIS_KEYS') for name in referenced):
                    offenders.append(node.lineno)

        assert not offenders, (
            f"_purge_redis deletes the rate-limit mirror at line(s) {offenders}. "
            f"That destroys the deployment's own reading, not just this "
            f"suite's -- and the write it was compensating for is now "
            f"prevented at source by "
            f"_keep_the_rate_limit_mirror_in_this_process()."
        )
