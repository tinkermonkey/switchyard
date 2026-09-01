"""
Tests for services/project_monitor.py's _launch_repair_cycle_container() mount
building (final whole-PR review pass on #87, addressed while unblocking #52).

Found: this function accepted a `project_dir` parameter but never actually used
it to build a mount -- the container was always bind-mounted at a hardcoded
/workspace/<project_name> (the shared base clone) regardless of what directory
the orchestrator had actually resolved for this repair cycle (which may be an
isolated epic worktree, e.g. /workspace/.orchestrator/worktrees/<project>/<epic_id>,
for 'issues'-workspace repair cycles -- see #46/#48). Fixed to translate
project_dir's container-side path into its host-side equivalent (mirroring
claude/docker_runner.py's own established translation for ordinary agent
containers) and mount it explicitly at its own path, in addition to the base
clone mount kept for anything else that still expects it there.
"""

from unittest.mock import patch, MagicMock

from services.project_monitor import _launch_repair_cycle_container


def _mock_env():
    env = MagicMock()
    env.redis_url = "redis://localhost:6379"
    env.anthropic_api_key = None
    env.claude_code_oauth_token = None
    env.github_token = None
    return env


def _run_launch(project_dir: str):
    # DockerAgentRunner is imported locally inside _launch_repair_cycle_container
    # (`from claude.docker_runner import DockerAgentRunner`), so it must be patched
    # at its defining module -- patching services.project_monitor.DockerAgentRunner
    # has no effect (that attribute doesn't exist until the function runs).
    with patch("claude.docker_runner.DockerAgentRunner") as mock_runner_cls, \
         patch("config.environment.load_environment", return_value=_mock_env()), \
         patch("services.project_monitor.subprocess.run") as mock_subprocess_run:

        mock_runner = mock_runner_cls.return_value
        mock_runner._detect_host_workspace_path.return_value = "/host/workspace"
        mock_runner.network_name = "switchyard_orchestrator-net"
        mock_runner_cls._sanitize_container_name.side_effect = lambda n: n
        mock_runner_cls._detect_host_home_path.return_value = "/host/home"

        mock_subprocess_run.return_value = MagicMock(returncode=0, stdout="containerid123\n", stderr="")

        with patch("monitoring.observability.get_observability_manager"):
            _launch_repair_cycle_container(
                project_name="my-project",
                issue_number=100,
                pipeline_run_id="run-abc12345",
                stage_name="Testing",
                context_file="/workspace/switchyard/orchestrator_data/repair_cycles/my-project/100/context.json",
                project_dir=project_dir,
            )

        docker_cmd = mock_subprocess_run.call_args.args[0]
        return docker_cmd


class TestMountUsesResolvedProjectDir:
    def test_epic_worktree_project_dir_gets_its_own_mount(self):
        """The core bug: project_dir pointing at an epic worktree must actually
        be mounted at that path, translated to its host equivalent -- not
        silently ignored in favor of the hardcoded base-clone mount alone."""
        epic_worktree_dir = "/workspace/.orchestrator/worktrees/my-project/42"

        docker_cmd = _run_launch(epic_worktree_dir)

        expected_mount = (
            "/host/workspace/.orchestrator/worktrees/my-project/42:"
            "/workspace/.orchestrator/worktrees/my-project/42"
        )
        assert expected_mount in docker_cmd, (
            f"epic worktree mount {expected_mount!r} missing from docker_cmd: {docker_cmd}"
        )

        # The base clone mount is also still present (kept for anything else
        # that expects it there) -- both coexist, not one replacing the other.
        assert "/host/workspace/my-project:/workspace/my-project" in docker_cmd

    def test_base_clone_project_dir_still_works_unchanged(self):
        """The common case (no epic worktree in play, project_dir == the base
        clone) must produce the same mount either way -- the new project_dir
        mount is a harmless duplicate of the existing base-clone mount, not a
        behavior change for this case."""
        base_clone_dir = "/workspace/my-project"

        docker_cmd = _run_launch(base_clone_dir)

        assert docker_cmd.count("/host/workspace/my-project:/workspace/my-project") >= 1

    def test_non_workspace_project_dir_falls_back_gracefully(self):
        """A project_dir that somehow doesn't start with /workspace/ (shouldn't
        happen in practice, but must not crash the launch) falls back to the
        base-clone mount instead of producing a malformed mount spec."""
        docker_cmd = _run_launch("/some/other/path")

        assert "/host/workspace/my-project:/workspace/my-project" in docker_cmd
        assert not any("/some/other/path" in arg for arg in docker_cmd)
