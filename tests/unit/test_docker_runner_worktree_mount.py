"""
Unit tests for claude/docker_runner.py's DockerAgentRunner worktree-mount fix
(issue #127).

Background: ordinary-dispatch containers mount whatever project_dir resolves to
directly at /workspace. That's correct for a normal repository (project_dir's
.git is a real directory) but breaks a linked git worktree (project_dir's .git
is a pointer FILE to admin data living in the originating clone's
.git/worktrees/<id>/, a path outside the mount entirely) -- confirmed live in
production: agents hitting "fatal: not a git repository" inside such a
container self-repaired with `git init`, orphaning the worktree from the real
branch/remote.

Fix: _prepare_worktree_git_mount() detects a worktree project_dir and returns
HOST paths for (a) the originating clone's .git directory and (b) a freshly
written, corrected gitdir-pointer file -- _build_docker_command() mounts (a) at
/git-base and (b) as an override at /workspace/.git, both with the SAME :ro/:rw
mode /workspace itself gets. Verified end-to-end with a real git worktree (not
just these mocked unit tests) before this file was written -- see the PR
description/commit message for that manual verification.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude.docker_runner import DockerAgentRunner


@pytest.fixture
def runner():
    return DockerAgentRunner()


class TestPrepareWorktreeGitMount:
    """Direct tests of the extracted method -- no need to exercise the rest of
    _build_docker_command's ~360 lines for these."""

    def test_none_for_a_normal_repository(self, runner, tmp_path):
        project_dir = tmp_path / 'workspace' / 'my-project'
        (project_dir / '.git').mkdir(parents=True)

        result = runner._prepare_worktree_git_mount(project_dir, '/host/workspace', 'c1')

        assert result is None

    def test_returns_host_paths_for_a_worktree(self, runner, tmp_path):
        # Real worktree .git pointer files always use the literal /workspace/...
        # convention (the orchestrator's own mount root) -- NOT wherever this
        # test happens to be running from on disk.
        worktree_dir = tmp_path / 'worktree-content'
        worktree_dir.mkdir(parents=True)
        (worktree_dir / '.git').write_text(
            'gitdir: /workspace/phone-home/.git/worktrees/204\n'
        )
        override_file = tmp_path / 'worktree_gitdir_override_c1.git'
        with patch.object(
            DockerAgentRunner, '_worktree_git_override_path', return_value=str(override_file)
        ):
            result = runner._prepare_worktree_git_mount(worktree_dir, '/host/workspace', 'c1')

        assert result is not None
        host_git_base_path, host_override_path = result

        assert host_git_base_path == '/host/workspace/phone-home/.git'
        assert host_override_path == str(override_file)

        override_content = override_file.read_text()
        assert override_content == 'gitdir: /git-base/worktrees/204\n'

        # The worktree's own real .git file on disk is untouched -- other
        # consumers (the orchestrator's own process, repair-cycle containers)
        # use a different mount layout and depend on the original content.
        assert (worktree_dir / '.git').read_text() == 'gitdir: /workspace/phone-home/.git/worktrees/204\n' 

    def test_malformed_pointer_falls_back_to_none_without_crashing(self, runner, tmp_path):
        """A .git file that exists but doesn't parse as a worktree pointer
        (unexpected format, e.g. a submodule's differently-shaped gitdir:)
        must not crash the whole dispatch."""
        project_dir = tmp_path / 'workspace' / 'weird-project'
        project_dir.mkdir(parents=True)
        (project_dir / '.git').write_text('gitdir: ../.git/modules/some-submodule\n')

        result = runner._prepare_worktree_git_mount(project_dir, '/host/workspace', 'c1')

        assert result is None


class TestContainerWorkspacePathToHost:
    def test_workspace_prefix(self):
        assert DockerAgentRunner._container_workspace_path_to_host(
            '/workspace/phone-home', '/host/ws'
        ) == '/host/ws/phone-home'

    def test_app_prefix(self):
        assert DockerAgentRunner._container_workspace_path_to_host(
            '/app/orchestrator_data/tmp/f', '/host/ws'
        ) == '/host/ws/switchyard/orchestrator_data/tmp/f'

    def test_unrecognized_prefix_passed_through(self):
        assert DockerAgentRunner._container_workspace_path_to_host(
            '/elsewhere/project', '/host/ws'
        ) == '/elsewhere/project'


def _agent_config(filesystem_write_allowed):
    config = MagicMock()
    config.filesystem_write_allowed = filesystem_write_allowed
    return config


def _run_build_docker_command(runner, project_dir: Path, filesystem_write_allowed: bool, container_name="test-container"):
    # ORCHESTRATOR_ROOT: _get_image_for_agent() (further down in
    # _build_docker_command, unrelated to this fix) reads/writes dev-container
    # state under ORCHESTRATOR_ROOT (default '/app', unwritable outside the
    # real container) -- give it somewhere writable for the test.
    with patch.dict(os.environ, {'ORCHESTRATOR_ROOT': tempfile.mkdtemp(prefix='switchyard-test-')}), \
         patch.object(DockerAgentRunner, '_detect_host_workspace_path', return_value='/host/workspace'), \
         patch.object(DockerAgentRunner, '_detect_host_home_path', return_value='/host/home'), \
         patch('config.manager.config_manager.get_project_agent_config',
               return_value=_agent_config(filesystem_write_allowed)), \
         patch('claude.environment.ClaudeEnvironmentBuilder') as mock_env_builder_cls:

        mock_env_builder = mock_env_builder_cls.return_value
        mock_env_builder.build.return_value = MagicMock(to_docker_env_args=lambda: [])

        cmd, _image = runner._build_docker_command(
            container_name=container_name,
            project_dir=project_dir,
            mcp_config_path=None,
            context={'agent': 'senior_software_engineer', 'project': 'test-project', 'task_id': 'task-1'},
        )
    return cmd


class TestBuildDockerCommandWiring:
    """Integration-level: confirms _build_docker_command actually wires
    _prepare_worktree_git_mount()'s result into -v args with the correct mode,
    not just that the extracted method itself works in isolation."""

    def test_no_extra_mounts_for_a_normal_repository(self, runner, tmp_path):
        project_dir = tmp_path / 'workspace' / 'my-project'
        (project_dir / '.git').mkdir(parents=True)

        cmd = _run_build_docker_command(runner, project_dir, filesystem_write_allowed=True)

        assert '/git-base' not in ' '.join(cmd)

    def test_worktree_mounts_are_read_write_when_workspace_is(self, runner, tmp_path):
        with patch.object(
            DockerAgentRunner, '_prepare_worktree_git_mount',
            return_value=('/host/workspace/phone-home/.git', '/host/workspace/.orchestrator/tmp/override.git'),
        ):
            project_dir = tmp_path / 'workspace' / '.orchestrator' / 'worktrees' / 'phone-home' / '204'
            project_dir.mkdir(parents=True)
            cmd = _run_build_docker_command(runner, project_dir, filesystem_write_allowed=True)

        assert '/host/workspace/phone-home/.git:/git-base:rw' in cmd
        assert '/host/workspace/.orchestrator/tmp/override.git:/workspace/.git:rw' in cmd

    def test_worktree_mounts_are_read_only_when_workspace_is(self, runner, tmp_path):
        """Code review finding, issue #127: a read-only agent must not get a
        backdoor to mutate the origin clone's real refs/object database through
        this second mount just because it's a different mount point."""
        with patch.object(
            DockerAgentRunner, '_prepare_worktree_git_mount',
            return_value=('/host/workspace/phone-home/.git', '/host/workspace/.orchestrator/tmp/override.git'),
        ):
            project_dir = tmp_path / 'workspace' / '.orchestrator' / 'worktrees' / 'phone-home' / '204'
            project_dir.mkdir(parents=True)
            cmd = _run_build_docker_command(runner, project_dir, filesystem_write_allowed=False)

        assert '/host/workspace/phone-home/.git:/git-base:ro' in cmd
        assert '/host/workspace/.orchestrator/tmp/override.git:/workspace/.git:ro' in cmd


class TestCleanupWorktreeGitOverride:
    def test_removes_the_override_file_if_present(self, runner, tmp_path):
        override_path = tmp_path / 'override.git'
        override_path.write_text('gitdir: /git-base/worktrees/204\n')

        with patch.object(DockerAgentRunner, '_worktree_git_override_path', return_value=str(override_path)):
            runner._cleanup_worktree_git_override('c1')

        assert not override_path.exists()

    def test_is_a_no_op_when_no_override_file_exists(self, runner, tmp_path):
        missing_path = tmp_path / 'does-not-exist.git'

        with patch.object(DockerAgentRunner, '_worktree_git_override_path', return_value=str(missing_path)):
            # Must not raise.
            runner._cleanup_worktree_git_override('c1')
