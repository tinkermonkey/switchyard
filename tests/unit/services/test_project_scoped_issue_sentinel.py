"""
Regression tests for #162: a project-scoped agent run has NO GitHub issue.

dev_environment_setup (and dev_environment_verifier behind it) are dispatched
against a PROJECT, not an issue. That was expressed as the placeholder
``issue_number=0``, and every "is there an issue?" test in
services/agent_executor.py is a KEY-PRESENCE test -- hence the production log's
own ``has_issue_number=True, issue_number=0``. The output-posting path then
treated 0 as a real issue, tried to comment on issue #0, failed, and retried
three times before falling back to a local file:

    ERROR services.github_integration: No issue_number in context for issue post
    ERROR services.agent_executor: Failed to post dev_environment_setup output to
          GitHub for issue #0 after 3 attempt(s): No issue_number.

~3 ERRORs per affected run, ~87 in a 12-hour window, and three GitHub calls per
run against an endpoint that cannot succeed.

The fix is Optional[int]/None semantics, expressed here as ABSENCE of the key:
None would still be a present key, and would flip all ~25 downstream
key-presence tests to "yes, there is an issue" -- strictly worse than 0.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch

from services.agent_executor import AgentExecutor, normalize_issue_scope


class TestNormalizeIssueScope:

    def test_the_zero_sentinel_is_stripped(self):
        context = {'issue_number': 0, 'project': 'p'}

        assert normalize_issue_scope(context) is True
        assert 'issue_number' not in context

    def test_none_is_stripped_too(self):
        """A present-but-None key reads as "yes" to a key-presence test just as
        0 did."""
        context = {'issue_number': None, 'project': 'p'}

        assert normalize_issue_scope(context) is True
        assert 'issue_number' not in context

    def test_a_real_issue_is_left_alone(self):
        context = {'issue_number': 42, 'project': 'p'}

        assert normalize_issue_scope(context) is False
        assert context['issue_number'] == 42

    def test_an_already_issueless_context_is_untouched(self):
        context = {'project': 'p'}

        assert normalize_issue_scope(context) is False
        assert context == {'project': 'p'}

    def test_the_synthetic_issue_payload_is_left_alone(self):
        """It carries a title/body for the agent prompt, not an identity the
        posting or state paths key on."""
        context = {'issue': {'title': 't', 'body': 'b', 'number': 0}, 'issue_number': 0}

        normalize_issue_scope(context)

        assert context['issue']['number'] == 0


class TestNoGitHubPostForAProjectScopedRun:

    @staticmethod
    async def _post(task_context):
        executor = object.__new__(AgentExecutor)

        project_config = MagicMock()
        project_config.github = {'org': 'test-org', 'repo': 'test-repo'}

        with patch('services.agent_executor.config_manager') as config, \
             patch('services.agent_executor.GitHubIntegration') as github_cls:
            config.get_project_config.return_value = project_config
            github = AsyncMock()
            github.post_agent_output = AsyncMock(
                return_value={'success': False, 'error': 'No issue_number'}
            )
            github_cls.return_value = github

            await executor._post_agent_output_to_github(
                agent_name='dev_environment_setup',
                task_context=task_context,
                result={'status': 'success', 'agent_output': '# Setup complete'},
            )

            return github

    @pytest.mark.asyncio
    async def test_the_zero_sentinel_does_not_produce_a_post_attempt(self):
        """The regression: this used to attempt the post and retry it 3x, one
        ERROR per attempt, against an issue that does not exist."""
        github = await self._post({
            'issue_number': 0,
            'project': 'test-project',
            'automated_setup': True,
        })

        github.post_agent_output.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_absent_issue_number_does_not_produce_a_post_attempt(self):
        github = await self._post({'project': 'test-project', 'automated_setup': True})

        github.post_agent_output.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_real_issue_is_still_posted_to(self):
        """The skip must be about "no issue", not about this agent."""
        github = await self._post({'issue_number': 42, 'project': 'test-project'})

        github.post_agent_output.assert_called()


class TestVerifierHandoffDoesNotReinjectTheSentinel:
    """_queue_environment_verifier() defaulted to the literal 0, which put the
    sentinel back into the NEXT task's context — handing dev_environment_verifier
    exactly the same three failing GitHub calls."""

    @staticmethod
    async def _queued_context(setup_context):
        executor = object.__new__(AgentExecutor)

        queue = MagicMock()
        with patch('task_queue.task_manager.TaskQueue', return_value=queue), \
             patch('services.agent_executor.config_manager') as config:
            config.get_project_config.side_effect = Exception('no board lookup needed')

            await executor._queue_environment_verifier(
                'test-project', setup_context, setup_output='# setup output'
            )

        queue.enqueue.assert_called_once()
        return queue.enqueue.call_args[0][0].context

    @pytest.mark.asyncio
    async def test_no_issue_number_is_carried_when_there_is_no_issue(self):
        context = await self._queued_context({
            'project': 'test-project', 'board': 'system', 'automated_setup': True,
        })

        assert 'issue_number' not in context

    @pytest.mark.asyncio
    async def test_the_sentinel_is_not_carried_either(self):
        context = await self._queued_context({
            'issue_number': 0, 'project': 'test-project', 'board': 'system',
        })

        assert 'issue_number' not in context

    @pytest.mark.asyncio
    async def test_a_real_issue_is_carried_through(self):
        context = await self._queued_context({
            'issue_number': 42, 'project': 'test-project', 'board': 'dev',
        })

        assert context['issue_number'] == 42


class TestTheProducersNoLongerEmitTheSentinel:
    """The three live producers of a project-scoped dev_environment_setup task.
    Normalization at dispatch covers tasks already queued in Redis across a
    deploy; these pin that no NEW ones are created carrying the sentinel."""

    @pytest.mark.asyncio
    async def test_the_auto_trigger_queues_no_issue_number(self):
        from contextlib import asynccontextmanager

        from agents.orchestrator_integration import queue_dev_environment_setup
        from services.dev_container_state import DevContainerStatus

        # queue_dev_environment_setup() reads the status and its timestamp as one
        # snapshot, inside this project's dev_container_build lock (#171). Both
        # are stubbed here so this stays a test of the task context it builds.
        # The stub MUST be of dev_container_build_lock_attempt_async, the symbol
        # that function actually calls, and MUST yield its (acquired, reason)
        # 2-tuple: an earlier version patched ..._if_free_async with a bare True
        # and was therefore inert, leaving the test to do live acquire/release
        # I/O against the container's Redis and to invert silently the moment
        # 'test-project' happened to be locked (#171 review).
        @asynccontextmanager
        async def _free(project, issue_number=None, facade=None):
            yield (True, None)

        state = MagicMock()
        state.get_status_and_updated_at.return_value = (DevContainerStatus.UNVERIFIED, None)
        state.set_status.return_value = True

        queue = MagicMock()
        with patch('task_queue.task_manager.TaskQueue', return_value=queue), \
             patch('services.dev_container_build_lock.dev_container_build_lock_attempt_async', _free), \
             patch('services.dev_container_state.dev_container_state', state):
            await queue_dev_environment_setup('test-project', MagicMock())

        queue.enqueue.assert_called_once()
        assert 'issue_number' not in queue.enqueue.call_args[0][0].context

    def test_the_startup_queue_emits_no_issue_number(self):
        """main.py's startup dev_environment_setup context. Read as source
        rather than executed: the surrounding startup loop is not unit-callable,
        and the point being pinned is that this literal is gone."""
        import re
        from pathlib import Path

        source = Path(__file__).resolve().parents[3] / 'main.py'
        assert not re.search(r"^\s*'issue_number': 0,", source.read_text(), re.MULTILINE)

    def test_the_manual_rebuild_script_emits_no_issue_number(self):
        import re
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[3] / 'scripts' / 'rebuild_project_images.py'
        )
        assert not re.search(r"^\s*'issue_number': 0,", source.read_text(), re.MULTILINE)
