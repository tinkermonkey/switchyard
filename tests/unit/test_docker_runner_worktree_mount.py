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
mode /workspace itself gets. Review pass 3: the worktrees/ mask alone still
left hooks/, config, and packed-refs in the shared .git writable (an
arbitrary-code-execution vector via hooks); each is now remounted read-only
on top of /git-base, unconditionally, when it exists in the origin clone.
Verified end-to-end with a real git worktree (not just these mocked unit
tests) before this file was written -- see the PR description/commit message
for that manual verification.
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
        assert result.host_git_base_path == '/host/workspace/phone-home/.git'
        assert result.worktree_admin_id == '204'
        assert result.host_override_path == str(override_file)
        # /workspace/phone-home isn't a real path in THIS test process, so
        # the origin clone's hooks/config/packed-refs can't be found here --
        # covered instead by test_protected_relative_paths_reflect_what_
        # actually_exists below, which points the pointer file at a real
        # tmp_path-based origin clone so the existence checks have something
        # real to check against.
        assert result.protected_relative_paths == ()

        override_content = override_file.read_text()
        assert override_content == 'gitdir: /git-base/worktrees/204\n'

        # The worktree's own real .git file on disk is untouched -- other
        # consumers (the orchestrator's own process, repair-cycle containers)
        # use a different mount layout and depend on the original content.
        assert (worktree_dir / '.git').read_text() == 'gitdir: /workspace/phone-home/.git/worktrees/204\n' 

    def test_protected_relative_paths_reflect_what_actually_exists(self, runner, tmp_path):
        """Review pass 3: hooks/config/packed-refs must each be reported only
        if they actually exist in the origin clone -- guessing wrong (e.g.
        assuming packed-refs always exists) would make the caller mount a
        nonexistent host path, which Docker auto-vivifies as an unwanted
        empty directory inside the REAL base clone's .git."""
        # Use a passthrough (non /workspace, non /app) origin path so this
        # method's own existence checks land on a real, test-controlled
        # directory instead of the unrelated real /workspace on this machine.
        origin_clone_path = tmp_path / 'origin-clone'
        origin_git_dir = origin_clone_path / '.git'
        origin_git_dir.mkdir(parents=True)
        (origin_git_dir / 'hooks').mkdir()
        (origin_git_dir / 'config').write_text('[core]\n')
        # packed-refs deliberately NOT created.

        worktree_dir = tmp_path / 'worktree-content'
        worktree_dir.mkdir(parents=True)
        (worktree_dir / '.git').write_text(
            f'gitdir: {origin_clone_path}/.git/worktrees/204\n'
        )
        override_file = tmp_path / 'worktree_gitdir_override_c1.git'
        with patch.object(
            DockerAgentRunner, '_worktree_git_override_path', return_value=str(override_file)
        ):
            result = runner._prepare_worktree_git_mount(worktree_dir, '/host/workspace', 'c1')

        assert result is not None
        assert set(result.protected_relative_paths) == {'hooks', 'config'}
        assert 'packed-refs' not in result.protected_relative_paths

        # Now add packed-refs and confirm it's picked up too.
        (origin_git_dir / 'packed-refs').write_text('# pack-refs\n')
        with patch.object(
            DockerAgentRunner, '_worktree_git_override_path', return_value=str(override_file)
        ):
            result2 = runner._prepare_worktree_git_mount(worktree_dir, '/host/workspace', 'c1')
        assert set(result2.protected_relative_paths) == {'hooks', 'config', 'packed-refs'}

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

    @staticmethod
    def _fake_mount(protected_relative_paths=()):
        return DockerAgentRunner.WorktreeGitMount(
            host_git_base_path='/host/workspace/phone-home/.git',
            worktree_admin_id='204',
            host_override_path='/host/workspace/.orchestrator/tmp/override.git',
            protected_relative_paths=protected_relative_paths,
        )

    def test_worktree_mounts_are_read_write_when_workspace_is(self, runner, tmp_path):
        with patch.object(
            DockerAgentRunner, '_prepare_worktree_git_mount',
            return_value=self._fake_mount(),
        ):
            project_dir = tmp_path / 'workspace' / '.orchestrator' / 'worktrees' / 'phone-home' / '204'
            project_dir.mkdir(parents=True)
            cmd = _run_build_docker_command(runner, project_dir, filesystem_write_allowed=True)

        assert '/host/workspace/phone-home/.git:/git-base:rw' in cmd
        assert '/host/workspace/phone-home/.git/worktrees/204:/git-base/worktrees/204:rw' in cmd
        assert '/host/workspace/.orchestrator/tmp/override.git:/workspace/.git:rw' in cmd

    def test_worktree_mounts_are_read_only_when_workspace_is(self, runner, tmp_path):
        """Code review finding, issue #127: a read-only agent must not get a
        backdoor to mutate the origin clone's real refs/object database through
        this second mount just because it's a different mount point."""
        with patch.object(
            DockerAgentRunner, '_prepare_worktree_git_mount',
            return_value=self._fake_mount(),
        ):
            project_dir = tmp_path / 'workspace' / '.orchestrator' / 'worktrees' / 'phone-home' / '204'
            project_dir.mkdir(parents=True)
            cmd = _run_build_docker_command(runner, project_dir, filesystem_write_allowed=False)

        assert '/host/workspace/phone-home/.git:/git-base:ro' in cmd
        assert '/host/workspace/phone-home/.git/worktrees/204:/git-base/worktrees/204:ro' in cmd
        assert '/host/workspace/.orchestrator/tmp/override.git:/workspace/.git:ro' in cmd

    def test_worktree_mounts_mask_sibling_worktrees(self, runner, tmp_path):
        """Code review finding, issue #127 review pass 2: mounting the whole
        origin clone's .git at /git-base would also expose every OTHER epic's
        own worktree admin subdirectory (all epics of one project share a
        single base clone) -- a tmpfs must mask /git-base/worktrees before
        this worktree's own admin subdir is remounted back on top of it."""
        with patch.object(
            DockerAgentRunner, '_prepare_worktree_git_mount',
            return_value=self._fake_mount(),
        ):
            project_dir = tmp_path / 'workspace' / '.orchestrator' / 'worktrees' / 'phone-home' / '204'
            project_dir.mkdir(parents=True)
            cmd = _run_build_docker_command(runner, project_dir, filesystem_write_allowed=True)

        assert '--tmpfs' in cmd
        tmpfs_index = cmd.index('--tmpfs')
        assert cmd[tmpfs_index + 1] == '/git-base/worktrees:size=1k'
        # The mask must come after the base /git-base mount and before the
        # specific-subdir remount, or Docker's mount resolution won't layer
        # them the way this fix depends on (verified empirically -- see the
        # PR/commit description).
        git_base_index = cmd.index('/host/workspace/phone-home/.git:/git-base:rw')
        remount_index = cmd.index('/host/workspace/phone-home/.git/worktrees/204:/git-base/worktrees/204:rw')
        assert git_base_index < tmpfs_index < remount_index

    def test_hooks_config_packed_refs_remounted_read_only_even_when_workspace_is_rw(self, runner, tmp_path):
        """Code review finding, issue #127 review pass 3: hooks is an
        arbitrary-code-execution vector (a container could plant a hook that
        runs inside a LATER container on a sibling epic) and config/
        packed-refs could redirect remotes or bulk-rewrite ref positions --
        none of the three are ever legitimately written to by an agent's
        ordinary git operations, so they stay read-only regardless of
        workspace_mount_mode."""
        with patch.object(
            DockerAgentRunner, '_prepare_worktree_git_mount',
            return_value=self._fake_mount(protected_relative_paths=('hooks', 'config')),
        ):
            project_dir = tmp_path / 'workspace' / '.orchestrator' / 'worktrees' / 'phone-home' / '204'
            project_dir.mkdir(parents=True)
            cmd = _run_build_docker_command(runner, project_dir, filesystem_write_allowed=True)

        assert '/host/workspace/phone-home/.git/hooks:/git-base/hooks:ro' in cmd
        assert '/host/workspace/phone-home/.git/config:/git-base/config:ro' in cmd
        assert '/git-base/packed-refs' not in ' '.join(cmd)

    def test_no_protected_path_mounts_when_none_exist(self, runner, tmp_path):
        with patch.object(
            DockerAgentRunner, '_prepare_worktree_git_mount',
            return_value=self._fake_mount(protected_relative_paths=()),
        ):
            project_dir = tmp_path / 'workspace' / '.orchestrator' / 'worktrees' / 'phone-home' / '204'
            project_dir.mkdir(parents=True)
            cmd = _run_build_docker_command(runner, project_dir, filesystem_write_allowed=True)

        assert '/git-base/hooks' not in ' '.join(cmd)
        assert '/git-base/config' not in ' '.join(cmd)
        assert '/git-base/packed-refs' not in ' '.join(cmd)


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
