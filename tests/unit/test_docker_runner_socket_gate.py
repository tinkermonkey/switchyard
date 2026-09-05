"""
Unit tests for the docker-socket-access concurrency gate's wiring into
claude/docker_runner.py's DockerAgentRunner.run_agent_in_container (switchyard
#51).

Covers:
- _requires_docker_socket_access() mirrors _build_docker_command's own mount
  condition (dev_environment_setup always; other agents only when project
  config sets docker_socket_access=True).
- run_agent_in_container acquires the gate before launching the container and
  releases it after a successful run.
- run_agent_in_container releases the gate even when container execution
  raises (the try/finally must not leak the gate on a crash).
- Agents WITHOUT docker socket access never touch the gate at all.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude.docker_runner import DockerAgentRunner


def _base_context(agent="senior_software_engineer", project="phone-home", task_id="task-1"):
    return {
        "agent": agent,
        "project": project,
        "task_id": task_id,
        "context": {},
    }


class TestRequiresDockerSocketAccess:
    def test_dev_environment_setup_always_true(self):
        assert DockerAgentRunner._requires_docker_socket_access(
            "dev_environment_setup", "phone-home"
        ) is True

    def test_true_when_project_config_enables_it(self):
        agent_config = MagicMock()
        agent_config.docker_socket_access = True
        with patch(
            "config.manager.config_manager.get_project_agent_config",
            return_value=agent_config,
        ):
            assert DockerAgentRunner._requires_docker_socket_access(
                "senior_software_engineer", "phone-home"
            ) is True

    def test_false_when_project_config_disables_it(self):
        agent_config = MagicMock()
        agent_config.docker_socket_access = False
        with patch(
            "config.manager.config_manager.get_project_agent_config",
            return_value=agent_config,
        ):
            assert DockerAgentRunner._requires_docker_socket_access(
                "senior_software_engineer", "phone-home"
            ) is False

    def test_false_on_configuration_error(self):
        from config.manager import ConfigurationError

        with patch(
            "config.manager.config_manager.get_project_agent_config",
            side_effect=ConfigurationError("unknown project"),
        ):
            assert DockerAgentRunner._requires_docker_socket_access(
                "some_system_agent", "unknown-project"
            ) is False


@pytest.fixture
def runner():
    return DockerAgentRunner()


class TestGateWiringForDockerSocketAgents:
    @pytest.mark.asyncio
    async def test_gate_acquired_before_launch_and_released_after_success(self, runner):
        context = _base_context(agent="dev_environment_setup", project="phone-home", task_id="task-1")

        fake_gate = MagicMock()
        fake_gate.acquire = AsyncMock(return_value=12345)
        fake_gate.release = AsyncMock(return_value=True)

        with patch("claude.docker_runner.get_breaker", return_value=None), \
             patch.object(runner, "_build_docker_command", return_value=(["docker", "run"], "some-image")), \
             patch.object(runner, "_execute_in_container", AsyncMock(return_value="agent output")) as mock_execute, \
             patch.object(runner, "_cleanup_container"), \
             patch.object(runner, "_unregister_active_container"), \
             patch.object(runner, "_cleanup_reference_worktrees"), \
             patch.object(runner, "_requires_docker_socket_access", return_value=True), \
             patch(
                 "services.docker_socket_access_gate.get_docker_socket_access_gate",
                 return_value=fake_gate,
             ):
            result = await runner.run_agent_in_container(
                prompt="do the thing",
                context=context,
                project_dir=Path("/workspace/phone-home"),
            )

            assert result == "agent output"
            fake_gate.acquire.assert_awaited_once_with("phone-home", "task-1")
            fake_gate.release.assert_called_once_with("phone-home", 12345)
            mock_execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gate_released_when_container_execution_raises(self, runner):
        """The try/finally must release the gate even when the container run
        itself blows up -- a crash must never leak the gate."""
        context = _base_context(agent="dev_environment_setup", project="phone-home", task_id="task-2")

        fake_gate = MagicMock()
        fake_gate.acquire = AsyncMock(return_value=99999)
        fake_gate.release = AsyncMock(return_value=True)

        with patch("claude.docker_runner.get_breaker", return_value=None), \
             patch.object(runner, "_build_docker_command", return_value=(["docker", "run"], "some-image")), \
             patch.object(
                 runner, "_execute_in_container",
                 AsyncMock(side_effect=RuntimeError("container blew up")),
             ), \
             patch.object(runner, "_cleanup_container"), \
             patch.object(runner, "_unregister_active_container"), \
             patch.object(runner, "_cleanup_reference_worktrees"), \
             patch.object(runner, "_requires_docker_socket_access", return_value=True), \
             patch(
                 "services.docker_socket_access_gate.get_docker_socket_access_gate",
                 return_value=fake_gate,
             ):
            with pytest.raises(RuntimeError, match="container blew up"):
                await runner.run_agent_in_container(
                    prompt="do the thing",
                    context=context,
                    project_dir=Path("/workspace/phone-home"),
                )

            fake_gate.acquire.assert_awaited_once_with("phone-home", "task-2")
            fake_gate.release.assert_called_once_with("phone-home", 99999)

    @pytest.mark.asyncio
    async def test_mcp_config_cleaned_up_even_when_gate_acquire_itself_fails(self, runner):
        """(#51 review, pass 1) acquire() itself can raise (e.g. TimeoutError
        under real contention) or the awaiting task can be cancelled. That must
        not skip the finally block's MCP-config-file cleanup -- previously
        acquire() ran BEFORE the try/finally, so a raise there leaked the temp
        file written a few lines earlier. Also confirms release() is never
        attempted when acquire() never actually returned a holder id."""
        context = _base_context(agent="dev_environment_setup", project="phone-home", task_id="task-3")

        fake_gate = MagicMock()
        fake_gate.acquire = AsyncMock(side_effect=TimeoutError("gate timed out"))
        fake_gate.release = AsyncMock(return_value=True)

        with patch("claude.docker_runner.get_breaker", return_value=None), \
             patch.object(runner, "_build_docker_command", return_value=(["docker", "run"], "some-image")), \
             patch.object(runner, "_execute_in_container", AsyncMock(return_value="agent output")), \
             patch.object(runner, "_cleanup_container"), \
             patch.object(runner, "_unregister_active_container"), \
             patch.object(runner, "_cleanup_reference_worktrees"), \
             patch.object(runner, "_requires_docker_socket_access", return_value=True), \
             patch.object(runner, "_write_mcp_config_file", return_value="/tmp/mcp-config-fake.json"), \
             patch("claude.docker_runner.os.path.exists", return_value=True), \
             patch("claude.docker_runner.os.remove") as mock_remove, \
             patch(
                 "services.docker_socket_access_gate.get_docker_socket_access_gate",
                 return_value=fake_gate,
             ):
            with pytest.raises(TimeoutError, match="gate timed out"):
                await runner.run_agent_in_container(
                    prompt="do the thing",
                    context=context,
                    project_dir=Path("/workspace/phone-home"),
                    mcp_servers=[{"name": "test-server", "type": "http", "url": "http://x"}],
                )

            fake_gate.acquire.assert_awaited_once_with("phone-home", "task-3")
            # The MCP config temp file must still be cleaned up despite acquire()
            # raising before the container was ever launched. (Also removes a
            # worktree-gitdir-override file that, per this test's own blanket
            # os.path.exists=True patch, "exists" too -- issue #127's
            # _cleanup_worktree_git_override() runs unconditionally in the same
            # finally block and is a genuine no-op here in practice, since
            # _build_docker_command is mocked out above and never actually wrote
            # one; this test isn't about that cleanup path.)
            mock_remove.assert_any_call("/tmp/mcp-config-fake.json")
            # Nothing was ever acquired -- release() must not be called at all.
            fake_gate.release.assert_not_called()

    @pytest.mark.asyncio
    async def test_gate_not_touched_for_non_docker_socket_agents(self, runner):
        context = _base_context(agent="senior_software_engineer", project="phone-home", task_id="task-3")

        fake_gate = MagicMock()
        fake_gate.acquire = AsyncMock(return_value=1)
        fake_gate.release = AsyncMock(return_value=True)

        with patch("claude.docker_runner.get_breaker", return_value=None), \
             patch.object(runner, "_build_docker_command", return_value=(["docker", "run"], "some-image")), \
             patch.object(runner, "_execute_in_container", AsyncMock(return_value="agent output")), \
             patch.object(runner, "_cleanup_container"), \
             patch.object(runner, "_unregister_active_container"), \
             patch.object(runner, "_cleanup_reference_worktrees"), \
             patch.object(runner, "_requires_docker_socket_access", return_value=False), \
             patch(
                 "services.docker_socket_access_gate.get_docker_socket_access_gate",
                 return_value=fake_gate,
             ):
            result = await runner.run_agent_in_container(
                prompt="do the thing",
                context=context,
                project_dir=Path("/workspace/phone-home"),
            )

            assert result == "agent output"
            fake_gate.acquire.assert_not_awaited()
            fake_gate.release.assert_not_called()
