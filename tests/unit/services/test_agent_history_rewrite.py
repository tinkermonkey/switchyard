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
