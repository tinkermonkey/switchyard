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

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude.docker_runner import DockerAgentRunner


def _git(repo_dir, *args, check=True):
    result = subprocess.run(
        ['git', '-C', str(repo_dir), *args],
        capture_output=True, text=True, timeout=10
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo_dir}: {result.stderr}")
    return result


def _make_real_worktree(tmp_path, branch_name='feature/issue-129-epic-a', pack_refs=True):
    """Build a real origin clone + a real linked worktree checked out to
    branch_name, mirroring production (ProjectWorkspaceManager.
    get_or_create_epic_worktree: fetch, worktree add, the base clone detached
    off the branch afterward). pack_refs=True (the default) additionally packs
    the branch so it's NOT a loose ref -- the exact packed-only case issue
    #129's own fix needs to handle correctly, since `git rev-parse` (what the
    fix's materialization step uses) transparently resolves either form.

    Returns (origin_clone_path, worktree_path).
    """
    origin = tmp_path / 'origin_clone'
    origin.mkdir()
    _git(origin, 'init', '-q')
    _git(origin, 'config', 'user.email', 'test@test.com')
    _git(origin, 'config', 'user.name', 'Test')
    (origin / 'file.txt').write_text('hello\n')
    _git(origin, 'add', '.')
    _git(origin, 'commit', '-q', '-m', 'initial')
    _git(origin, 'checkout', '-q', '-b', branch_name)
    (origin / 'work.txt').write_text('work\n')
    _git(origin, 'add', '.')
    _git(origin, 'commit', '-q', '-m', 'epic work')
    if pack_refs:
        _git(origin, 'pack-refs', '--all')
    # Detach the base clone off the branch -- git refuses `worktree add` for a
    # branch checked out anywhere else, including the primary checkout.
    _git(origin, 'checkout', '-q', '--detach', 'HEAD')

    worktree = tmp_path / 'the_worktree'
    _git(origin, 'worktree', 'add', str(worktree), branch_name)

    return origin, worktree


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
        # worktree_dir isn't a real git repository in this test process either
        # (just a bare .git pointer file with no working structure behind
        # it) -- `git symbolic-ref --short HEAD` against it fails, so refs/
        # heads write isolation (issue #129) degrades to "not set up" here,
        # the same soft-degrade a genuinely detached HEAD would produce. See
        # TestPrepareRefsHeadsStaging below for the real-worktree happy path.
        assert result.host_refs_heads_staging_path is None
        assert result.own_branch_relative_ref_path is None

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


class TestPrepareRefsHeadsStaging:
    """Issue #129: per-branch refs/heads write isolation. Unlike most of
    TestPrepareWorktreeGitMount above, these tests exercise real git worktrees
    (not bare pointer-file fixtures) -- this sub-feature's own correctness
    (branch resolution, packed-vs-loose materialization) can only be verified
    against real git state, the same rigor the manual end-to-end verification
    that motivated this design used."""

    @staticmethod
    def _prepare(runner, worktree_path, tmp_path, container_name='c1'):
        override_file = tmp_path / f'worktree_gitdir_override_{container_name}.git'
        staging_dir = tmp_path / f'refs_heads_staging_{container_name}'
        meta_file = tmp_path / f'refs_heads_staging_{container_name}.meta.json'
        with patch.object(DockerAgentRunner, '_worktree_git_override_path',
                           return_value=str(override_file)), \
             patch.object(DockerAgentRunner, '_worktree_refs_heads_staging_dir',
                           return_value=str(staging_dir)), \
             patch.object(DockerAgentRunner, '_worktree_refs_heads_staging_meta_path',
                           return_value=str(meta_file)):
            result = runner._prepare_worktree_git_mount(worktree_path, '/host/workspace', container_name)
        return result, staging_dir, meta_file

    @pytest.mark.parametrize('pack_refs', [True, False], ids=['packed-only', 'already-loose'])
    def test_prepares_staging_with_the_branchs_current_sha(self, runner, tmp_path, pack_refs):
        """The core regression case (issue #129's own "why this wasn't fixed
        in #128" point 3): must work identically whether the branch is
        packed-only or already loose in the origin clone -- `git rev-parse`
        (what the fix's materialization step uses) resolves either
        transparently, unlike the ORIGINALLY proposed approach in the issue,
        which needed the ref to already be loose."""
        origin, worktree = _make_real_worktree(tmp_path, pack_refs=pack_refs)
        expected_sha = _git(origin, 'rev-parse', 'feature/issue-129-epic-a').stdout.strip()

        result, staging_dir, meta_file = self._prepare(runner, worktree, tmp_path)

        assert result.own_branch_relative_ref_path == 'feature/issue-129-epic-a'
        assert result.host_refs_heads_staging_path == str(staging_dir)

        staged_ref_file = staging_dir / 'feature' / 'issue-129-epic-a'
        assert staged_ref_file.read_text().strip() == expected_sha

        meta = json.loads(meta_file.read_text())
        assert meta['origin_clone_container_path'] == str(origin)
        assert meta['own_branch_relative_ref_path'] == 'feature/issue-129-epic-a'

    def test_detached_head_worktree_skips_refs_heads_staging(self, runner, tmp_path):
        """Not expected in practice for these worktrees (get_or_create_epic_
        worktree always checks out a real branch), but not asserted against
        either -- must degrade gracefully (refs/heads left exposed, matching
        pre-#129 behavior), not crash the whole worktree-mount fix."""
        origin, worktree = _make_real_worktree(tmp_path)
        _git(worktree, 'checkout', '-q', '--detach', 'HEAD')

        result, _staging_dir, meta_file = self._prepare(runner, worktree, tmp_path)

        assert result is not None  # the core #127 fix must still apply
        assert result.host_refs_heads_staging_path is None
        assert result.own_branch_relative_ref_path is None
        assert not meta_file.exists()

    def test_nested_branch_name_produces_a_nested_staged_path(self, runner, tmp_path):
        """Branch names in this codebase always nest one level
        (create_feature_branch_name(): "feature/issue-N-slug") -- confirm the
        staging file lands at the correspondingly nested path, not flattened
        or rejected."""
        origin, worktree = _make_real_worktree(tmp_path, branch_name='feature/issue-777-deeply-nested-name')

        result, staging_dir, _meta_file = self._prepare(runner, worktree, tmp_path)

        assert result.own_branch_relative_ref_path == 'feature/issue-777-deeply-nested-name'
        assert (staging_dir / 'feature' / 'issue-777-deeply-nested-name').is_file()


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
    def _fake_mount(protected_relative_paths=(), host_refs_heads_staging_path=None,
                     own_branch_relative_ref_path=None):
        return DockerAgentRunner.WorktreeGitMount(
            host_git_base_path='/host/workspace/phone-home/.git',
            worktree_admin_id='204',
            host_override_path='/host/workspace/.orchestrator/tmp/override.git',
            protected_relative_paths=protected_relative_paths,
            host_refs_heads_staging_path=host_refs_heads_staging_path,
            own_branch_relative_ref_path=own_branch_relative_ref_path,
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

    def test_refs_heads_staging_mounted_with_workspace_mount_mode(self, runner, tmp_path):
        """Issue #129: when _prepare_worktree_git_mount() set up refs/heads
        staging, it must be mounted onto /git-base/refs/heads with the SAME
        :ro/:rw mode as the rest of /git-base -- a read-only agent must not
        get a write path to even its own branch pointer."""
        with patch.object(
            DockerAgentRunner, '_prepare_worktree_git_mount',
            return_value=self._fake_mount(
                host_refs_heads_staging_path='/host/workspace/.orchestrator/tmp/refs_heads_staging_c1',
                own_branch_relative_ref_path='feature/issue-129-epic-a',
            ),
        ):
            project_dir = tmp_path / 'workspace' / '.orchestrator' / 'worktrees' / 'phone-home' / '204'
            project_dir.mkdir(parents=True)
            cmd_rw = _run_build_docker_command(runner, project_dir, filesystem_write_allowed=True)
            cmd_ro = _run_build_docker_command(runner, project_dir, filesystem_write_allowed=False)

        assert (
            '/host/workspace/.orchestrator/tmp/refs_heads_staging_c1:/git-base/refs/heads:rw'
            in cmd_rw
        )
        assert (
            '/host/workspace/.orchestrator/tmp/refs_heads_staging_c1:/git-base/refs/heads:ro'
            in cmd_ro
        )

    def test_no_refs_heads_mount_when_staging_was_not_set_up(self, runner, tmp_path):
        """host_refs_heads_staging_path is None whenever _prepare_worktree_git_
        mount() couldn't determine the current branch (e.g. detached HEAD) --
        must not add a mount at all in that case, leaving refs/heads exposed
        exactly as it was pre-#129 (a soft degrade, not a launch failure)."""
        with patch.object(
            DockerAgentRunner, '_prepare_worktree_git_mount',
            return_value=self._fake_mount(),  # both new fields default to None
        ):
            project_dir = tmp_path / 'workspace' / '.orchestrator' / 'worktrees' / 'phone-home' / '204'
            project_dir.mkdir(parents=True)
            cmd = _run_build_docker_command(runner, project_dir, filesystem_write_allowed=True)

        assert '/git-base/refs/heads' not in ' '.join(cmd)


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


class TestCleanupWorktreeRefsHeadsStaging:
    """Issue #129: _cleanup_worktree_refs_heads_staging() is the other half of
    the fix -- it syncs whatever the container wrote to its own staged branch
    ref back into the REAL origin clone (every other consumer, including the
    orchestrator's own unmasked git commands and the NEXT container launch
    for this epic, reads the real one directly, not the ephemeral staging
    copy), then removes the staging directory."""

    @staticmethod
    def _set_up_staging(tmp_path, origin, staged_value, branch_name='feature/issue-129-epic-a'):
        staging_dir = tmp_path / 'refs_heads_staging_c1'
        staged_ref_path = staging_dir / branch_name
        staged_ref_path.parent.mkdir(parents=True)
        staged_ref_path.write_text(staged_value + '\n')

        meta_path = tmp_path / 'refs_heads_staging_c1.meta.json'
        meta_path.write_text(json.dumps({
            'origin_clone_container_path': str(origin),
            'own_branch_relative_ref_path': branch_name,
        }))
        return staging_dir, meta_path

    def test_syncs_the_updated_sha_back_and_removes_staging(self, runner, tmp_path):
        origin, worktree = _make_real_worktree(tmp_path)
        # Simulate the container having made a new commit: a plausible-looking
        # but different SHA staged for its own branch.
        new_sha = '1' * 40
        staging_dir, meta_path = self._set_up_staging(tmp_path, origin, new_sha)

        with patch.object(DockerAgentRunner, '_worktree_refs_heads_staging_dir',
                           return_value=str(staging_dir)), \
             patch.object(DockerAgentRunner, '_worktree_refs_heads_staging_meta_path',
                           return_value=str(meta_path)):
            runner._cleanup_worktree_refs_heads_staging('c1')

        real_ref_path = origin / '.git' / 'refs' / 'heads' / 'feature' / 'issue-129-epic-a'
        assert real_ref_path.read_text().strip() == new_sha
        assert not staging_dir.exists()
        assert not meta_path.exists()

    def test_rejects_a_value_that_does_not_look_like_a_sha(self, runner, tmp_path):
        """Defense against a corrupted/unexpected staged file -- must leave
        the real origin clone's ref untouched rather than write garbage that
        every other consumer would then trust."""
        origin, worktree = _make_real_worktree(tmp_path)
        original_sha = _git(origin, 'rev-parse', 'feature/issue-129-epic-a').stdout.strip()
        staging_dir, meta_path = self._set_up_staging(tmp_path, origin, 'not-a-real-sha-at-all')

        with patch.object(DockerAgentRunner, '_worktree_refs_heads_staging_dir',
                           return_value=str(staging_dir)), \
             patch.object(DockerAgentRunner, '_worktree_refs_heads_staging_meta_path',
                           return_value=str(meta_path)):
            # Must not raise.
            runner._cleanup_worktree_refs_heads_staging('c1')

        # The real ref (still loose from _make_real_worktree's own resolution
        # -- unaffected either way) must be unchanged.
        real_ref_path = origin / '.git' / 'refs' / 'heads' / 'feature' / 'issue-129-epic-a'
        assert _git(origin, 'rev-parse', 'feature/issue-129-epic-a').stdout.strip() == original_sha
        # Staging must still be cleaned up even though the sync was rejected.
        assert not staging_dir.exists()
        assert not meta_path.exists()

    def test_is_a_no_op_when_no_metadata_file_exists(self, runner, tmp_path):
        """The common case for every non-worktree, or detached-HEAD, launch."""
        missing_meta = tmp_path / 'does-not-exist.meta.json'

        with patch.object(DockerAgentRunner, '_worktree_refs_heads_staging_meta_path',
                           return_value=str(missing_meta)):
            # Must not raise.
            runner._cleanup_worktree_refs_heads_staging('c1')

    def test_cleans_up_staging_even_if_the_origin_clone_path_is_gone(self, runner, tmp_path):
        """A launch-teardown step must never leak its own temp resources just
        because the sync-back half of it failed for some other reason."""
        staging_dir = tmp_path / 'refs_heads_staging_c1'
        staging_dir.mkdir()
        (staging_dir / 'some-file').write_text('leftover')
        meta_path = tmp_path / 'refs_heads_staging_c1.meta.json'
        meta_path.write_text(json.dumps({
            'origin_clone_container_path': str(tmp_path / 'nonexistent-origin'),
            'own_branch_relative_ref_path': 'feature/issue-129-epic-a',
        }))

        with patch.object(DockerAgentRunner, '_worktree_refs_heads_staging_dir',
                           return_value=str(staging_dir)), \
             patch.object(DockerAgentRunner, '_worktree_refs_heads_staging_meta_path',
                           return_value=str(meta_path)):
            # Must not raise, even though the staged ref file itself doesn't
            # exist (only an unrelated leftover file does) and the origin
            # clone path in the metadata doesn't exist either.
            runner._cleanup_worktree_refs_heads_staging('c1')

        assert not staging_dir.exists()
        assert not meta_path.exists()
