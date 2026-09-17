"""
Running a coroutine from sync code that may or may not be on the event loop.

`asyncio.run()` raises when the calling thread already has a running loop, and
several sync methods in services/project_monitor.py are reachable from both
contexts -- the monitor's loop-free daemon thread, and main.py's startup
lock-recovery straight out of `async def main()`. On 2026-09-17 that produced:

    CRITICAL: Failed to check PR ready on issue exit for #1017:
    asyncio.run() cannot be called from a running event loop
"""

import asyncio

import pytest

from utils.async_bridge import run_coroutine_blocking


class TestItWorksInBothLoopContexts:
    def test_off_the_loop(self):
        async def work():
            return 'off-loop'

        assert run_coroutine_blocking(lambda: work()) == 'off-loop'

    @pytest.mark.asyncio
    async def test_on_the_loop(self):
        """The case a bare asyncio.run() cannot do at all."""
        async def work():
            return 'on-loop'

        assert run_coroutine_blocking(lambda: work()) == 'on-loop'

    @pytest.mark.asyncio
    async def test_it_actually_awaits_rather_than_returning_a_coroutine(self):
        """A sync caller that got a coroutine object back would treat it as a
        truthy result and never run the work -- the silent version of this bug."""
        ran = []

        async def work():
            ran.append(True)
            return 'done'

        result = run_coroutine_blocking(lambda: work())
        assert ran == [True], "the coroutine must have actually executed"
        assert result == 'done'
        assert not asyncio.iscoroutine(result)


class TestErrorsSurviveUnchanged:
    """The trap this signature exists to close, documented at
    project_monitor.py's resolve_workspace() call site after review found it."""

    @pytest.mark.asyncio
    async def test_a_runtime_error_from_the_coroutine_is_not_masked(self):
        """RuntimeError is the collision case: the loop probe raises it too. A
        single try/except around probe AND call would catch the coroutine's own
        RuntimeError and re-raise something about asyncio instead."""
        async def boom():
            raise RuntimeError("the real cause")

        with pytest.raises(RuntimeError, match="the real cause"):
            run_coroutine_blocking(lambda: boom())

    def test_a_runtime_error_is_not_masked_off_the_loop_either(self):
        async def boom():
            raise RuntimeError("the real cause")

        with pytest.raises(RuntimeError, match="the real cause"):
            run_coroutine_blocking(lambda: boom())

    @pytest.mark.asyncio
    async def test_other_exception_types_propagate(self):
        async def boom():
            raise ValueError("specific failure")

        with pytest.raises(ValueError, match="specific failure"):
            run_coroutine_blocking(lambda: boom())

    @pytest.mark.asyncio
    async def test_no_cannot_reuse_coroutine_error(self):
        """The factory's whole purpose. Building one coroutine and using it in
        both branches made a failure in the first report itself as "cannot
        reuse already awaited coroutine" from the second."""
        async def boom():
            raise RuntimeError("original")

        with pytest.raises(RuntimeError) as exc:
            run_coroutine_blocking(lambda: boom())

        assert 'already awaited' not in str(exc.value)
        assert 'cannot be called from a running event loop' not in str(exc.value)

    @pytest.mark.asyncio
    async def test_each_call_gets_a_fresh_coroutine(self):
        """A factory called once per invocation, so the same callable can be
        reused by a retry loop -- which _post_pipeline_failure_comment does."""
        calls = []

        async def work():
            calls.append(1)
            return len(calls)

        assert run_coroutine_blocking(lambda: work()) == 1
        assert run_coroutine_blocking(lambda: work()) == 2


class TestTheCallSitesWorkOnTheLoop:
    """Regression tests for the five sites in project_monitor.py.

    All five are sync methods reachable from trigger_agent_for_status(), which
    main.py:767 calls directly from `async def main()` during startup lock
    recovery. Each used a bare asyncio.run() and each swallowed the resulting
    RuntimeError, so on that path they failed silently in a different way:

      _release_pipeline_lock_and_process_next  PR-ready check never ran
      _post_pipeline_failure_comment          failure comment never posted
      _check_agent_processed_issue_sync       returned False -> re-dispatch
      _escalate_sustained_lock_contention     escalation never posted
      _start_repair_cycle_for_issue           real error masked by the fallback

    Each test runs the method from inside a running loop, which is what a unit
    test on an `async def` gives for free -- the context that used to fail.
    """

    def _monitor(self):
        from unittest.mock import MagicMock
        from services.project_monitor import ProjectMonitor
        m = ProjectMonitor.__new__(ProjectMonitor)
        m.config_manager = MagicMock()
        return m

    @pytest.mark.asyncio
    async def test_agent_processed_check_returns_the_real_answer_on_the_loop(self):
        """The most consequential of the five: `except -> return False` means
        "no prior agent work", so on the loop the re-dispatch guard was simply
        absent and an agent that had already run could be run again."""
        from unittest.mock import AsyncMock, MagicMock, patch

        monitor = self._monitor()
        github = MagicMock()
        github.has_agent_processed_issue = AsyncMock(return_value=True)

        with patch('services.github_integration.GitHubIntegration', return_value=github):
            result = monitor._check_agent_processed_issue_sync(
                issue_number=1017, agent='code_reviewer',
                repository='acme/thing', org='acme',
            )

        assert result is True, (
            "a bare asyncio.run() raised here on the loop, and the except "
            "turned that into False -- 'agent has not processed this issue'"
        )
        assert github.has_agent_processed_issue.await_count == 1

    @pytest.mark.asyncio
    async def test_agent_processed_check_still_works_for_discussions(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        monitor = self._monitor()
        github = MagicMock()
        github.has_agent_processed_discussion = AsyncMock(return_value=True)

        with patch('services.github_integration.GitHubIntegration', return_value=github):
            result = monitor._check_agent_processed_issue_sync(
                issue_number=1017, agent='code_reviewer',
                repository='acme/thing', org='acme',
                workspace_type='discussions', discussion_id='D_123',
            )

        assert result is True
        assert github.has_agent_processed_discussion.await_count == 1

    @pytest.mark.asyncio
    async def test_the_failure_comment_actually_posts_on_the_loop(self):
        """Both retry attempts failed with the same RuntimeError here, so the
        dispatch diagnosis #254 put into `reason` never reached the issue --
        the exact outcome that change existed to prevent."""
        from unittest.mock import AsyncMock, MagicMock, patch

        monitor = self._monitor()
        monitor.config_manager.get_project_config.return_value = MagicMock(
            github={'org': 'acme'}
        )
        github = MagicMock()
        github.post_comment = AsyncMock()

        with patch('services.github_integration.GitHubIntegration', return_value=github):
            monitor._post_pipeline_failure_comment(
                project_name='thing', board_name='dev_workflow',
                repository='acme/thing', issue_number=1017,
                reason='3 consecutive dispatch failures\n\nLast error: base image not ours',
            )

        assert github.post_comment.await_count == 1, (
            "the operator's only signal must survive the loop context"
        )
        body = github.post_comment.await_args.args[1]
        assert 'base image not ours' in body, "the diagnosis must reach the issue"

    def test_no_on_loop_reachable_site_still_calls_asyncio_run_bare(self):
        """Source-level guard over the four fixed methods.

        The behavioural tests above are the real protection; this catches a new
        bare asyncio.run() being reintroduced into one of them, which would
        restore a silent failure rather than an obvious one.

        Inspects the AST rather than grepping the text. A first version matched
        on the string and reported two of the fixed methods as offenders -- both
        hits were this change's OWN prose, one in a `#` comment and one in a
        docstring explaining the bug. A guard that cannot tell code from a
        comment about code is not a guard.

        Deliberately scoped to these four. The module's other asyncio.run()
        calls are correct as they stand: each runs on a pool thread, inside its
        own spawned thread, or behind its own loop probe.
        """
        import ast
        import inspect
        import textwrap
        from services.project_monitor import ProjectMonitor

        def bare_run_lines(func):
            tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
            return [
                node.lineno for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'run'
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == 'asyncio'
            ]

        fixed = [
            '_release_pipeline_lock_and_process_next',
            '_post_pipeline_failure_comment',
            '_check_agent_processed_issue_sync',
            '_escalate_sustained_lock_contention',
        ]
        offenders = {
            name: lines for name in fixed
            if (lines := bare_run_lines(getattr(ProjectMonitor, name)))
        }
        assert offenders == {}, f"bare asyncio.run() reintroduced in: {offenders}"

    def test_each_fixed_site_uses_the_helper(self):
        """The inverse of the guard above: absence of asyncio.run would also be
        satisfied by deleting the call entirely."""
        import inspect
        from services.project_monitor import ProjectMonitor

        for name in [
            '_release_pipeline_lock_and_process_next',
            '_post_pipeline_failure_comment',
            '_check_agent_processed_issue_sync',
            '_escalate_sustained_lock_contention',
        ]:
            src = inspect.getsource(getattr(ProjectMonitor, name))
            assert 'run_coroutine_blocking' in src, f"{name} no longer bridges contexts"
