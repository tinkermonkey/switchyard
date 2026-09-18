"""
#266: an agent rewriting history that was already pushed.

An agent has a writable worktree with the base clone's `.git` mounted, so it can
reset, rebase, amend or cherry-pick over commits the orchestrator has already
published. Nothing checked. Run fbd185c9-ec5b-475c-83de-769531240c8d is the
worked example -- reset past two pushed commits and a cherry-pick of one back,
at 19:32:19Z and 19:32:21Z, inside the agent's own 19:31:59-19:32:37 container
run. It surfaced minutes later as a bare non-fast-forward push rejection whose
cause had to be guessed.

Real git throughout, not mocks: the whole question is what git considers
reachable, and a mocked `merge-base` would only assert this file's own beliefs
about it. The repos are tiny and local.
"""

import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.git_workflow_manager import (
    AgentRewroteHistoryError,
    GitWorkflowManager,
)

_ENV = {
    'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
    'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
    'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null',
    'HOME': '/nonexistent', 'PATH': '/usr/bin:/bin',
}


def _git(cwd, *args, check=True):
    r = subprocess.run(['git', '-C', str(cwd), *args],
                       capture_output=True, text=True, env=_ENV)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A worktree with an origin it has actually pushed to."""
    import shutil
    if shutil.which('git') is None:
        pytest.skip("git not available")

    remote = tmp_path / 'remote.git'
    wt = tmp_path / 'wt'
    subprocess.run(['git', 'init', '-q', '--bare', str(remote)], check=True, env=_ENV)
    subprocess.run(['git', 'clone', '-q', str(remote), str(wt)],
                   capture_output=True, check=True, env=_ENV)
    _git(wt, 'checkout', '-q', '-b', 'main')
    (wt / 'f.txt').write_text('a\n')
    _git(wt, 'add', '-A')
    _git(wt, 'commit', '-qm', 'base')
    _git(wt, 'push', '-q', '-u', 'origin', 'main')
    return wt


class TestDroppedPublishedCommitsAreFound:
    def _m(self):
        return GitWorkflowManager.__new__(GitWorkflowManager)

    def test_the_incident_shape_is_detected(self, repo):
        """Reset past two pushed commits, cherry-pick one back. Both dropped
        commits were on origin, so both must be reported."""
        (repo / 'f.txt').write_text('b\n')
        _git(repo, 'commit', '-qam', 'revert docstring change')
        (repo / 'f.txt').write_text('c\n')
        _git(repo, 'commit', '-qam', 'fix audit drift')
        _git(repo, 'push', '-q', 'origin', 'main')
        pre = _git(repo, 'rev-parse', 'HEAD')

        _git(repo, 'reset', '-q', '--hard', 'HEAD~2')
        subprocess.run(['git', '-C', str(repo), 'cherry-pick', pre],
                       capture_output=True, env=_ENV)

        dropped = self._m().find_dropped_pushed_commits(str(repo), pre)

        subjects = sorted(sub for _, sub in dropped)
        assert subjects == ['fix audit drift', 'revert docstring change'], dropped
        assert all(len(sha) == 40 for sha, _ in dropped)

    def test_amending_unpushed_work_is_not_flagged(self, repo):
        """The false positive a plain `merge-base --is-ancestor` check would
        produce, and the reason this measures published-ness instead. Rebasing
        or amending work that was never pushed is legitimate and common."""
        (repo / 'g.txt').write_text('local\n')
        _git(repo, 'add', '-A')
        _git(repo, 'commit', '-qm', 'local only, not pushed')
        pre = _git(repo, 'rev-parse', 'HEAD')

        _git(repo, 'commit', '-q', '--amend', '-m', 'local only, amended')

        assert self._m().find_dropped_pushed_commits(str(repo), pre) == []

    def test_an_ordinary_new_commit_is_not_flagged(self, repo):
        """Control: the common case must stay silent, or this blocks every run."""
        pre = _git(repo, 'rev-parse', 'HEAD')
        (repo / 'h.txt').write_text('more\n')
        _git(repo, 'add', '-A')
        _git(repo, 'commit', '-qm', 'ordinary work')

        assert self._m().find_dropped_pushed_commits(str(repo), pre) == []

    def test_an_unchanged_head_is_not_flagged(self, repo):
        pre = _git(repo, 'rev-parse', 'HEAD')
        assert self._m().find_dropped_pushed_commits(str(repo), pre) == []

    def test_a_branch_never_pushed_is_not_flagged(self, repo):
        """No origin/<branch> means nothing dropped can have been published."""
        _git(repo, 'checkout', '-q', '-b', 'feature/never-pushed')
        (repo / 'i.txt').write_text('x\n')
        _git(repo, 'add', '-A')
        _git(repo, 'commit', '-qm', 'unpublished work')
        pre = _git(repo, 'rev-parse', 'HEAD')
        _git(repo, 'reset', '-q', '--hard', 'HEAD~1')

        assert self._m().find_dropped_pushed_commits(str(repo), pre) == []

    def test_no_fetch_is_performed(self, repo):
        """`git push` already updated origin/<branch>, so the check stays off
        the network -- it runs on every agent execution. A stale tracking ref
        can only under-detect, never refuse wrongly."""
        (repo / 'f.txt').write_text('z\n')
        _git(repo, 'commit', '-qam', 'pushed change')
        _git(repo, 'push', '-q', 'origin', 'main')
        pre = _git(repo, 'rev-parse', 'HEAD')
        _git(repo, 'reset', '-q', '--hard', 'HEAD~1')

        real_run = subprocess.run
        seen = []

        def _spy(cmd, **kwargs):
            if isinstance(cmd, list):
                seen.append(cmd)
            return real_run(cmd, **kwargs)

        with patch('services.git_workflow_manager.subprocess.run', side_effect=_spy):
            dropped = self._m().find_dropped_pushed_commits(str(repo), pre)

        assert len(dropped) == 1
        assert not any('fetch' in c for c in seen), "must not hit the network"


class TestItNeverBecomesItsOwnFailure:
    def _m(self):
        return GitWorkflowManager.__new__(GitWorkflowManager)

    def test_a_non_repository_is_silent(self, tmp_path):
        assert self._m().find_dropped_pushed_commits(str(tmp_path), 'a' * 40) == []

    def test_no_pre_head_is_silent(self, repo):
        assert self._m().find_dropped_pushed_commits(str(repo), None) == []
        assert self._m().find_dropped_pushed_commits(str(repo), '') == []

    def test_a_git_failure_is_swallowed(self, repo):
        """Runs on every agent execution; a git hiccup must not invent a new way
        for an otherwise healthy run to fail."""
        with patch('services.git_workflow_manager.subprocess.run',
                   side_effect=OSError("git vanished")):
            assert self._m().find_dropped_pushed_commits(str(repo), 'a' * 40) == []

    def test_read_head_returns_none_off_a_repo(self, tmp_path, repo):
        m = self._m()
        assert m.read_head(str(tmp_path)) is None
        assert m.read_head(str(repo)) == _git(repo, 'rev-parse', 'HEAD')

    def test_read_head_survives_git_itself_failing(self, repo):
        """A non-repo makes git exit non-zero, which the returncode branch
        handles -- it never reaches the except. Only a raising subprocess does,
        and that is the branch that matters: read_head runs before EVERY agent
        container launch, so raising here would ground all of them."""
        m = self._m()
        with patch('services.git_workflow_manager.subprocess.run',
                   side_effect=OSError("git vanished")):
            assert m.read_head(str(repo)) is None

        with patch('services.git_workflow_manager.subprocess.run',
                   side_effect=subprocess.TimeoutExpired(cmd='git', timeout=15)):
            assert m.read_head(str(repo)) is None


class TestTheRefusalIsWiredAndNonRetryable:
    def test_the_error_is_non_retryable(self):
        """No retry can bring the commits back, and every retry loop keys off
        this one type rather than any docstring claim."""
        from utils.non_retryable import NonRetryableAgentError
        assert issubclass(AgentRewroteHistoryError, NonRetryableAgentError)

    @pytest.mark.asyncio
    async def test_run_agent_in_container_refuses_after_a_rewrite(self, tmp_path):
        """The wiring. The detector being correct is not the fix -- refusing is."""
        from claude.docker_runner import DockerAgentRunner

        runner = DockerAgentRunner.__new__(DockerAgentRunner)
        ctx = {'agent': 'senior_software_engineer', 'task_id': 't1', 'project': 'p'}

        with patch('claude.docker_runner.get_breaker', return_value=None), \
             patch.object(DockerAgentRunner, '_sanitize_container_name',
                          return_value='c'), \
             patch.object(DockerAgentRunner, '_build_docker_command',
                          return_value=(['docker'], 'img')), \
             patch.object(DockerAgentRunner, '_requires_docker_socket_access',
                          return_value=False), \
             patch.object(DockerAgentRunner, '_execute_in_container',
                          new_callable=AsyncMock, return_value='agent output'), \
             patch.object(DockerAgentRunner, '_register_active_container'), \
             patch.object(DockerAgentRunner, '_cleanup_reference_worktrees'), \
             patch.object(DockerAgentRunner, '_cleanup_container'), \
             patch.object(DockerAgentRunner, '_unregister_active_container'), \
             patch('services.git_workflow_manager.git_workflow_manager.read_head',
                   return_value='b' * 40), \
             patch('services.git_workflow_manager.git_workflow_manager'
                   '.find_dropped_pushed_commits',
                   return_value=[('a' * 40, 'fix audit drift')]) as detector:
            with pytest.raises(AgentRewroteHistoryError) as exc:
                await runner.run_agent_in_container('prompt', ctx, tmp_path)

        # The snapshot has to be taken BEFORE the agent runs and threaded into
        # the check; passing None would disable the detector silently while
        # every other assertion here still passed.
        assert detector.call_args.args[1] == 'b' * 40, (
            "the pre-agent HEAD snapshot must reach the detector"
        )

        message = str(exc.value)
        assert 'senior_software_engineer' in message, "name the agent responsible"
        assert 'bbbbbbbb' in message, (
            "the pre-agent HEAD must be reported -- it is the recovery handle"
        )
        assert 'fix audit drift' in message, "name what was dropped"
        assert 'aaaaaaaa' in message
        assert 'can be recovered' in message, "say the commits still exist"

    @pytest.mark.asyncio
    async def test_a_clean_run_returns_its_output(self, tmp_path):
        """Control: the check must not stand between every agent and its result."""
        from claude.docker_runner import DockerAgentRunner

        runner = DockerAgentRunner.__new__(DockerAgentRunner)
        ctx = {'agent': 'code_reviewer', 'task_id': 't2', 'project': 'p'}

        with patch('claude.docker_runner.get_breaker', return_value=None), \
             patch.object(DockerAgentRunner, '_sanitize_container_name',
                          return_value='c'), \
             patch.object(DockerAgentRunner, '_build_docker_command',
                          return_value=(['docker'], 'img')), \
             patch.object(DockerAgentRunner, '_requires_docker_socket_access',
                          return_value=False), \
             patch.object(DockerAgentRunner, '_execute_in_container',
                          new_callable=AsyncMock, return_value='agent output'), \
             patch.object(DockerAgentRunner, '_register_active_container'), \
             patch.object(DockerAgentRunner, '_cleanup_reference_worktrees'), \
             patch.object(DockerAgentRunner, '_cleanup_container'), \
             patch.object(DockerAgentRunner, '_unregister_active_container'), \
             patch('services.git_workflow_manager.git_workflow_manager.read_head',
                   return_value='b' * 40), \
             patch('services.git_workflow_manager.git_workflow_manager'
                   '.find_dropped_pushed_commits', return_value=[]):
            assert await runner.run_agent_in_container('prompt', ctx, tmp_path) == 'agent output'


class TestTheCheckSaysWhichAnswerItGave:
    """#271: the detector returned [] in production on the exact shape it was
    built for, and left nothing in the logs to say which of its six early exits
    it took -- so the cause still is not known.

    `[]` means two completely different things: "I checked and nothing published
    was dropped", and "I could not check". These pin them apart. Asserting on log
    records rather than return values, because the return value is precisely what
    cannot distinguish them.
    """

    def _m(self):
        return GitWorkflowManager.__new__(GitWorkflowManager)

    def test_a_missing_pre_head_warns_that_nothing_was_checked(self, caplog):
        """The branch that made the production miss invisible."""
        import logging
        with caplog.at_level(logging.WARNING):
            assert self._m().find_dropped_pushed_commits('/wt', None) == []

        assert 'no pre-agent head' in caplog.text.lower()
        assert 'would not see it' in caplog.text, (
            "must say detection is DISABLED, not that the tree is clean"
        )

    def test_a_clean_run_does_not_warn(self, repo, caplog):
        """Control: the common case must stay quiet, or the warning above is
        noise and gets ignored."""
        import logging
        pre = _git(repo, 'rev-parse', 'HEAD')
        (repo / 'n.txt').write_text('x\n')
        _git(repo, 'add', '-A')
        _git(repo, 'commit', '-qm', 'ordinary work')

        with caplog.at_level(logging.WARNING):
            assert self._m().find_dropped_pushed_commits(str(repo), pre) == []

        assert caplog.text.strip() == '', f"unexpected warning: {caplog.text}"

    def test_a_rewrite_of_local_only_work_is_reported_as_such(self, repo, caplog):
        """Rewriting unpushed work is legitimate, but it is NOT the same as no
        rewrite at all -- and the logs should show the difference, since a
        rewrite is the thing that precedes the damaging case."""
        import logging
        (repo / 'g.txt').write_text('local\n')
        _git(repo, 'add', '-A')
        _git(repo, 'commit', '-qm', 'local only')
        pre = _git(repo, 'rev-parse', 'HEAD')
        _git(repo, 'commit', '-q', '--amend', '-m', 'local only, amended')

        with caplog.at_level(logging.INFO):
            assert self._m().find_dropped_pushed_commits(str(repo), pre) == []

        assert 'history was rewritten' in caplog.text
        assert 'local-only' in caplog.text or 'never been pushed' in caplog.text

    def test_a_detached_head_says_it_cannot_judge(self, repo, caplog):
        """No origin/<branch> to compare against, so the answer is unknown --
        which must not read as an all-clear."""
        import logging
        (repo / 'd.txt').write_text('x\n')
        _git(repo, 'add', '-A')
        _git(repo, 'commit', '-qm', 'work')
        pre = _git(repo, 'rev-parse', 'HEAD')
        _git(repo, 'checkout', '-q', '--detach', 'HEAD~1')

        with caplog.at_level(logging.WARNING):
            assert self._m().find_dropped_pushed_commits(str(repo), pre) == []

        assert 'detached HEAD' in caplog.text
        assert 'cannot tell' in caplog.text

    def test_a_git_failure_says_it_could_not_check(self, repo, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            with patch('services.git_workflow_manager.subprocess.run',
                       side_effect=OSError("git vanished")):
                assert self._m().find_dropped_pushed_commits(str(repo), 'a' * 40) == []

        assert 'could not check' in caplog.text.lower()

    def test_read_head_says_why_it_found_nothing(self, tmp_path, caplog):
        import logging
        with caplog.at_level(logging.DEBUG):
            assert self._m().read_head(str(tmp_path)) is None
        assert 'no head to snapshot' in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_the_container_warns_when_no_snapshot_was_taken(self, tmp_path, caplog):
        """The call-site half of the same blind spot. Without this warning the
        only trace of a disabled check is its absence, which is what made #271
        undiagnosable: detection was off for that run and nothing said so."""
        import logging
        from claude.docker_runner import DockerAgentRunner

        runner = DockerAgentRunner.__new__(DockerAgentRunner)
        ctx = {'agent': 'senior_software_engineer', 'task_id': 't9', 'project': 'p'}

        with caplog.at_level(logging.WARNING), \
             patch('claude.docker_runner.get_breaker', return_value=None), \
             patch.object(DockerAgentRunner, '_sanitize_container_name', return_value='c'), \
             patch.object(DockerAgentRunner, '_build_docker_command',
                          return_value=(['docker'], 'img')), \
             patch.object(DockerAgentRunner, '_requires_docker_socket_access',
                          return_value=False), \
             patch.object(DockerAgentRunner, '_execute_in_container',
                          new_callable=AsyncMock, return_value='out'), \
             patch.object(DockerAgentRunner, '_register_active_container'), \
             patch.object(DockerAgentRunner, '_cleanup_reference_worktrees'), \
             patch.object(DockerAgentRunner, '_cleanup_container'), \
             patch.object(DockerAgentRunner, '_unregister_active_container'), \
             patch('services.git_workflow_manager.git_workflow_manager.read_head',
                   return_value=None), \
             patch('services.git_workflow_manager.git_workflow_manager'
                   '.find_dropped_pushed_commits', return_value=[]):
            assert await runner.run_agent_in_container('prompt', ctx, tmp_path) == 'out'

        assert 'No pre-agent HEAD captured' in caplog.text
        assert 'will NOT be' in caplog.text, "must say detection is disabled"
        assert 'senior_software_engineer' in caplog.text, "name the agent it applies to"


class TestAnInheritedGitEnvCannotRedirectTheCheck:
    """#271 follow-up. `git -C <path>` is overridden by GIT_DIR / GIT_WORK_TREE
    in the environment, so an inherited one silently points every command at a
    DIFFERENT repository.

    The failure mode is the worst available here: the pre-snapshot and the
    post-read both come from the wrong repo, compare equal, and the check
    reports a clean tree for a worktree that was just rewritten. Measured
    against the unscrubbed version, the detector returned False for a genuinely
    dropped published commit.

    project_workspace's own git probe scrubs for exactly this reason (#253).
    This one did not.
    """

    def _m(self):
        return GitWorkflowManager.__new__(GitWorkflowManager)

    @pytest.fixture
    def unrelated_repo(self, tmp_path):
        other = tmp_path / 'unrelated'
        other.mkdir()
        subprocess.run(['git', 'init', '-q', str(other)], check=True, env=_ENV)
        (other / 'x.txt').write_text('x\n')
        _git(other, 'add', '-A')
        _git(other, 'commit', '-qm', 'unrelated')
        return other

    def test_the_detector_still_sees_the_rewrite(self, repo, unrelated_repo, monkeypatch):
        (repo / 'f.txt').write_text('b\n')
        _git(repo, 'commit', '-qam', 'published work')
        _git(repo, 'push', '-q', 'origin', 'main')
        pre = _git(repo, 'rev-parse', 'HEAD')
        _git(repo, 'reset', '--hard', 'HEAD~1')

        monkeypatch.setenv('GIT_DIR', str(unrelated_repo / '.git'))

        dropped = self._m().find_dropped_pushed_commits(str(repo), pre)
        assert [s for _, s in dropped] == ['published work'], (
            "an inherited GIT_DIR must not redirect the check at another repo"
        )

    def test_read_head_still_reads_the_right_repo(self, repo, unrelated_repo, monkeypatch):
        expected = _git(repo, 'rev-parse', 'HEAD')
        monkeypatch.setenv('GIT_DIR', str(unrelated_repo / '.git'))

        assert self._m().read_head(str(repo)) == expected

    def test_git_work_tree_is_scrubbed_too(self, repo, unrelated_repo, monkeypatch):
        expected = _git(repo, 'rev-parse', 'HEAD')
        monkeypatch.setenv('GIT_WORK_TREE', str(unrelated_repo))
        monkeypatch.setenv('GIT_DIR', str(unrelated_repo / '.git'))

        assert self._m().read_head(str(repo)) == expected


class TestTheQuietExitsAreVisibleAtInfo:
    """#271's remaining blind spot. These two were the only exits still at
    `debug`, which the orchestrator does not emit -- so a run where the snapshot
    logged correctly and the agent demonstrably rewrote history still produced
    nothing readable either way. One line per agent run is a cheap price."""

    def _m(self):
        return GitWorkflowManager.__new__(GitWorkflowManager)

    def test_head_unchanged_logs_at_info(self, repo, caplog):
        import logging
        pre = _git(repo, 'rev-parse', 'HEAD')
        with caplog.at_level(logging.INFO):
            assert self._m().find_dropped_pushed_commits(str(repo), pre) == []
        assert 'HEAD unchanged' in caplog.text
        assert any(r.levelno >= logging.INFO for r in caplog.records)

    def test_still_an_ancestor_logs_at_info(self, repo, caplog):
        import logging
        pre = _git(repo, 'rev-parse', 'HEAD')
        (repo / 'k.txt').write_text('x\n')
        _git(repo, 'add', '-A')
        _git(repo, 'commit', '-qm', 'ordinary work')

        with caplog.at_level(logging.INFO):
            assert self._m().find_dropped_pushed_commits(str(repo), pre) == []
        assert 'still an ancestor' in caplog.text
        assert any(r.levelno >= logging.INFO for r in caplog.records)
