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


def _mock_env(*, bedrock_token=None, use_bedrock=None, aws_region=None):
    env = MagicMock()
    env.redis_url = "redis://localhost:6379"
    env.anthropic_api_key = None
    env.claude_code_oauth_token = None
    env.github_token = None
    env.aws_bearer_token_bedrock = bedrock_token
    env.claude_code_use_bedrock = use_bedrock
    env.aws_region = aws_region
    return env


def _run_launch(project_dir: str, env=None):
    if env is None:
        env = _mock_env()
    # DockerAgentRunner is imported locally inside _launch_repair_cycle_container
    # (`from claude.docker_runner import DockerAgentRunner`), so it must be patched
    # at its defining module -- patching services.project_monitor.DockerAgentRunner
    # has no effect (that attribute doesn't exist until the function runs).
    with patch("claude.docker_runner.DockerAgentRunner") as mock_runner_cls, \
         patch("config.environment.load_environment", return_value=env), \
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


class TestBedrockAuthForwarding:
    """Bedrock auth env vars must be forwarded into the repair-cycle container
    so nested senior_software_engineer sub-containers can authenticate. The
    original bug (100% auth failures on Bedrock deployments) was caused by
    these vars never being passed at all."""

    def test_bedrock_token_forwarded_when_set(self):
        """AWS_BEARER_TOKEN_BEDROCK must appear in docker_cmd when the env var is set."""
        mock_token = MagicMock()
        mock_token.get_secret_value.return_value = "test-bedrock-token"
        env = _mock_env(bedrock_token=mock_token)

        docker_cmd = _run_launch("/workspace/my-project", env=env)

        assert "AWS_BEARER_TOKEN_BEDROCK=test-bedrock-token" in docker_cmd, (
            f"AWS_BEARER_TOKEN_BEDROCK not forwarded; docker_cmd: {docker_cmd}"
        )

    def test_bedrock_token_empty_when_unset(self):
        """AWS_BEARER_TOKEN_BEDROCK should still appear (as empty string) when unset,
        so the var is consistently present in the container's environment."""
        env = _mock_env(bedrock_token=None)

        docker_cmd = _run_launch("/workspace/my-project", env=env)

        assert any("AWS_BEARER_TOKEN_BEDROCK=" in arg for arg in docker_cmd)

    def test_use_bedrock_forwarded_when_set(self):
        """CLAUDE_CODE_USE_BEDROCK must appear when set, so the container enables Bedrock,
        AND it must appear before the image name so Docker treats it as a -e flag rather
        than an argument to the container process."""
        env = _mock_env(use_bedrock="1")

        docker_cmd = _run_launch("/workspace/my-project", env=env)

        assert "CLAUDE_CODE_USE_BEDROCK=1" in docker_cmd, (
            f"CLAUDE_CODE_USE_BEDROCK not forwarded; docker_cmd: {docker_cmd}"
        )
        image_idx = docker_cmd.index("switchyard-orchestrator:latest")
        bedrock_flag_idx = docker_cmd.index("CLAUDE_CODE_USE_BEDROCK=1")
        assert bedrock_flag_idx < image_idx, (
            f"CLAUDE_CODE_USE_BEDROCK=1 must come before the image name "
            f"(flag at {bedrock_flag_idx}, image at {image_idx}); docker_cmd: {docker_cmd}"
        )

    def test_use_bedrock_omitted_when_unset(self):
        """CLAUDE_CODE_USE_BEDROCK must NOT be forwarded when unset, so the
        container's own ClaudeEnvironmentBuilder default ('1') is not shadowed
        by a present-but-empty env var."""
        env = _mock_env(use_bedrock=None)

        docker_cmd = _run_launch("/workspace/my-project", env=env)

        assert not any("CLAUDE_CODE_USE_BEDROCK" in arg for arg in docker_cmd), (
            f"CLAUDE_CODE_USE_BEDROCK should be absent but found in docker_cmd: {docker_cmd}"
        )

    def test_aws_region_forwarded_when_set(self):
        """AWS_REGION must appear when set, AND it must appear before the image name
        so Docker treats it as a -e flag rather than an argument to the container process."""
        env = _mock_env(aws_region="us-east-1")

        docker_cmd = _run_launch("/workspace/my-project", env=env)

        assert "AWS_REGION=us-east-1" in docker_cmd, (
            f"AWS_REGION not forwarded; docker_cmd: {docker_cmd}"
        )
        image_idx = docker_cmd.index("switchyard-orchestrator:latest")
        region_flag_idx = docker_cmd.index("AWS_REGION=us-east-1")
        assert region_flag_idx < image_idx, (
            f"AWS_REGION=us-east-1 must come before the image name "
            f"(flag at {region_flag_idx}, image at {image_idx}); docker_cmd: {docker_cmd}"
        )

    def test_aws_region_omitted_when_unset(self):
        """AWS_REGION must NOT be forwarded when unset (omit-if-absent pattern)."""
        env = _mock_env(aws_region=None)

        docker_cmd = _run_launch("/workspace/my-project", env=env)

        assert not any("AWS_REGION" in arg for arg in docker_cmd), (
            f"AWS_REGION should be absent but found in docker_cmd: {docker_cmd}"
        )
