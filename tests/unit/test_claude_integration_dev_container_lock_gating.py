"""
Unit tests for run_claude_code()'s dev_container_build lock gating
(issue #152 item B, from #140 items 20 + 26).

The local-execution branch used to acquire dev_container_build_lock_async()
unconditionally, on the stated assumption that only dev_environment_setup and
dev_environment_verifier ever reached it. The branch is actually gated on
`use_docker=False`, which five more callers set:

  - pipeline_analysis, via services/pipeline_run_analysis.py -- and because that
    module hardcoded project="switchyard", EVERY project's post-run analysis
    contended for switchyard's build lock (see
    test_pipeline_run_analysis_project_scoping.py for that half of the fix).
  - scripts/analyze_codebase.py's three discovery passes,
    scripts/generate_strategy.py and scripts/generate_artifacts.py, which pass
    use_docker=False with no agent_config at all.

None of those builds an image, so each was liable to block for up to
DEFAULT_TIMEOUT_SECONDS (~1h) behind that project's real dev_environment_setup
or verifier build. The gate is now agent identity
(dev_container_build_lock.agent_holds_build_window).
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from claude.claude_integration import run_claude_code
from services.dev_container_build_lock import (
    BUILD_WINDOW_AGENTS,
    agent_holds_build_window,
)


class _RecordingLock:
    """Stands in for dev_container_build_lock_async, recording every acquisition."""

    def __init__(self):
        self.acquired_for = []

    @asynccontextmanager
    async def __call__(self, project, issue_number=None, **kwargs):
        self.acquired_for.append((project, issue_number))
        yield


@asynccontextmanager
async def _noop_lock(*args, **kwargs):
    yield


def _local_context(agent, **overrides):
    """A use_docker=False context -- no agent_config, so run_claude_code() falls
    through to the context flag exactly as the scripts/ entry points do."""
    context = {
        'agent': agent,
        'task_id': f'task-{agent}',
        'project': 'test-project',
        'use_docker': False,
        'work_dir': '/workspace/test-project',
        'context': {'issue_number': 42},
    }
    context.update(overrides)
    return context


async def _run(agent, build_lock, *, is_base_clone=False, checkout_lock=_noop_lock,
               **overrides):
    # is_base_clone_dir() is patched where the LOCK MODULE imports it, not where
    # claude_integration does (#154/WI-9 review). #140 item 4 moved the decision
    # into project_checkout_lock_if_shared_async(), which does a function-local
    # `from services.project_workspace import workspace_manager` to avoid an
    # import cycle -- so patching claude.claude_integration.workspace_manager
    # left is_base_clone dead configuration and the real filesystem decided the
    # branch (and it fails CLOSED, so is_base_clone=False silently ran the
    # LOCKED branch: the opposite of what the parameter said).
    with patch('services.dev_container_build_lock.dev_container_build_lock_async', build_lock), \
         patch('services.project_checkout_lock.project_checkout_lock_async', checkout_lock), \
         patch('services.project_workspace.workspace_manager') as mock_wm, \
         patch('claude.claude_integration._run_claude_code_locally',
               new_callable=AsyncMock, return_value='done') as mock_local:
        mock_wm.is_base_clone_dir.return_value = is_base_clone
        result = await run_claude_code('prompt', _local_context(agent, **overrides))
    return result, mock_local


class TestAgentHoldsBuildWindow:

    def test_the_two_build_agents_hold_the_window(self):
        assert agent_holds_build_window('dev_environment_setup') is True
        assert agent_holds_build_window('dev_environment_verifier') is True
        assert BUILD_WINDOW_AGENTS == {'dev_environment_setup', 'dev_environment_verifier'}

    def test_pipeline_analysis_does_not(self):
        """requires_docker: false in agents.yaml, but it only queries ES."""
        assert agent_holds_build_window('pipeline_analysis') is False

    def test_the_scripts_entry_point_agents_do_not(self):
        for agent in (
            'architecture_discoverer',
            'techstack_discoverer',
            'conventions_discoverer',
            'strategy_generator',
            'artifact_generator',
            'quality_reviewer',
        ):
            assert agent_holds_build_window(agent) is False, agent

    def test_an_unknown_or_missing_agent_does_not(self):
        """Fails towards NOT taking a lock it has no use for: an agent this
        module has never heard of does not build images, and a new one that does
        is added to BUILD_WINDOW_AGENTS explicitly."""
        assert agent_holds_build_window(None) is False
        assert agent_holds_build_window('unknown') is False
        assert agent_holds_build_window('some_future_agent') is False


@pytest.mark.asyncio
class TestLocalExecutionLockGating:

    async def test_dev_environment_setup_still_takes_the_build_lock(self):
        """The behavior this lock exists for must not regress: this agent's
        session IS the project's docker build."""
        build_lock = _RecordingLock()
        result, mock_local = await _run('dev_environment_setup', build_lock)

        assert build_lock.acquired_for == [('test-project', 42)]
        assert result == 'done'
        mock_local.assert_awaited_once()

    async def test_dev_environment_verifier_still_takes_the_build_lock(self):
        build_lock = _RecordingLock()
        await _run('dev_environment_verifier', build_lock)
        assert build_lock.acquired_for == [('test-project', 42)]

    async def test_pipeline_analysis_takes_no_build_lock(self):
        """THE regression (#140 item 20): post-run analysis never builds or
        inspects an image, and used to serialize against every real build."""
        build_lock = _RecordingLock()
        result, mock_local = await _run('pipeline_analysis', build_lock)

        assert build_lock.acquired_for == []
        assert result == 'done'
        mock_local.assert_awaited_once()

    async def test_the_scripts_entry_points_take_no_build_lock(self):
        """THE regression (#140 item 26): an ad hoc analysis/strategy/artifact
        run for a project could block ~1h behind that project's real build."""
        for agent in (
            'architecture_discoverer',
            'techstack_discoverer',
            'conventions_discoverer',
            'strategy_generator',
            'artifact_generator',
            'quality_reviewer',
        ):
            build_lock = _RecordingLock()
            _, mock_local = await _run(agent, build_lock)
            assert build_lock.acquired_for == [], agent
            mock_local.assert_awaited_once()

    async def test_the_checkout_lock_decision_is_unchanged_for_a_non_build_agent(self):
        """Dropping the build lock must not drop the project_checkout lock with
        it: that one is gated on the directory, not the agent, and still applies
        to any local run whose cwd IS the shared base clone."""
        build_lock = _RecordingLock()
        checkout_calls = []

        @asynccontextmanager
        async def _recording_checkout_lock(project, issue_number=None, **kwargs):
            checkout_calls.append((project, issue_number))
            yield

        with patch('services.dev_container_build_lock.dev_container_build_lock_async', build_lock), \
             patch('services.project_checkout_lock.project_checkout_lock_async', _recording_checkout_lock), \
             patch('services.project_workspace.workspace_manager') as mock_wm, \
             patch('claude.claude_integration._run_claude_code_locally',
                   new_callable=AsyncMock, return_value='done') as mock_local:
            mock_wm.is_base_clone_dir.return_value = True
            await run_claude_code('prompt', _local_context('pipeline_analysis'))

        assert build_lock.acquired_for == []
        assert checkout_calls == [('test-project', 42)]
        mock_local.assert_awaited_once()

    async def test_an_epic_worktree_run_takes_neither_lock(self):
        """
        The negative half of the decision, and the one nothing pinned at this
        call site (#154/WI-9 review): an epic-worktree-scoped run must NOT take
        the project_checkout lock. Without this, regressing
        project_checkout_lock_if_shared_async() to lock unconditionally left the
        whole suite green while every sibling epic of a project serialized on
        the shared base-clone lock -- the exact cross-epic throughput cost the
        guard exists to avoid, with no error and no failing test.
        """
        build_lock = _RecordingLock()
        checkout_calls = []

        @asynccontextmanager
        async def _recording_checkout_lock(project, issue_number=None, **kwargs):
            checkout_calls.append((project, issue_number))
            yield

        result, mock_local = await _run(
            'pipeline_analysis', build_lock,
            is_base_clone=False, checkout_lock=_recording_checkout_lock,
        )

        assert build_lock.acquired_for == []
        assert checkout_calls == [], (
            "an epic-worktree-scoped local run must take no project_checkout lock"
        )
        assert result == 'done'
        mock_local.assert_awaited_once()

    async def test_a_build_agent_nests_both_locks_in_the_documented_order(self):
        """dev_container_build OUTER, project_checkout INNER -- reversing them
        risks a mutual timeout (see both lock modules' 'Acquisition order')."""
        order = []

        @asynccontextmanager
        async def _build(project, issue_number=None, **kwargs):
            order.append('build:enter')
            yield
            order.append('build:exit')

        @asynccontextmanager
        async def _checkout(project, issue_number=None, **kwargs):
            order.append('checkout:enter')
            yield
            order.append('checkout:exit')

        with patch('services.dev_container_build_lock.dev_container_build_lock_async', _build), \
             patch('services.project_checkout_lock.project_checkout_lock_async', _checkout), \
             patch('services.project_workspace.workspace_manager') as mock_wm, \
             patch('claude.claude_integration._run_claude_code_locally',
                   new_callable=AsyncMock, return_value='done'):
            mock_wm.is_base_clone_dir.return_value = True
            await run_claude_code('prompt', _local_context('dev_environment_setup'))

        assert order == ['build:enter', 'checkout:enter', 'checkout:exit', 'build:exit']
