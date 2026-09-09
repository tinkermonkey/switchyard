"""
Unit tests for agent_executor.py's failsafe commit path (issue #149 WI-4
review).

auto_commit.py's commit_agent_changes() and feature_branch_manager's
finalize_feature_branch_work() both verify the checked-out branch against a
caller-supplied expectation before committing. _failsafe_commit_check() -- the
third commit path -- did not: `git add -A`, `git commit --no-verify`, then a
push to whatever `git rev-parse --abbrev-ref HEAD` reported, with no branch
check at all, not even the main/master refusal the other two enforce.

That made it the weakest of the three, and it is reached on the
workspace_context is None branch, i.e. every skip_workspace_prep dispatch:
pipeline/repair_cycle.py sets that for repair_fix, repair_warning and
repair_systemic_fix. A repair agent that left HEAD on `fix-attempt` had its
edits committed and pushed there before the hardened commit_agent_changes()
downstream ever got a chance to refuse.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch

from services.agent_executor import FailsafeOutcome


@pytest.fixture
def executor():
    from services.agent_executor import AgentExecutor

    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        return AgentExecutor()


def _git_stub(branch_stdout='feature/issue-5-epic', branch_rc=0, status_stdout=''):
    """subprocess.run stub answering the two reads _failsafe_commit_check does."""
    def _run(cmd, **kwargs):
        result = MagicMock()
        if 'rev-parse' in cmd:
            result.returncode = branch_rc
            result.stdout = branch_stdout
            result.stderr = 'boom'
        else:
            result.returncode = 0
            result.stdout = status_stdout
            result.stderr = ''
        return result
    return _run


class TestVerifyFailsafeBranch:

    def test_accepts_the_dispatchs_own_branch(self, executor):
        with patch('subprocess.run', side_effect=_git_stub()):
            assert executor._verify_failsafe_branch(
                project_dir='/workspace/x',
                project_name='test-project',
                issue_number=7,
                expected_branch='feature/issue-5-epic',
            ).commit_branch == 'feature/issue-5-epic'

    def test_refuses_another_issues_branch(self, executor):
        """THE regression: the repair agent's container left HEAD on
        `fix-attempt`, and the failsafe pushed the repair edits there."""
        with patch('subprocess.run', side_effect=_git_stub(branch_stdout='fix-attempt')):
            check = executor._verify_failsafe_branch(
                project_dir='/workspace/x',
                project_name='test-project',
                issue_number=7,
                expected_branch='feature/issue-5-epic',
            )
            assert check.commit_branch is None
            # Drift the sweep can SEE: the branch was read, and it was wrong.
            assert check.refusal is FailsafeOutcome.BRANCH_DRIFTED
            assert check.current_branch == 'fix-attempt'

    def test_refuses_a_detached_head(self, executor):
        """push_branch(project_dir, 'HEAD') fails and is logged at warning level
        only, leaving a dangling commit."""
        with patch('subprocess.run', side_effect=_git_stub(branch_stdout='HEAD')):
            check = executor._verify_failsafe_branch(
                project_dir='/workspace/x',
                project_name='test-project',
                issue_number=7,
                expected_branch=None,
            )
            assert check.commit_branch is None
            # UNSAFE, not DRIFTED: there was no target to drift from.
            assert check.refusal is FailsafeOutcome.BRANCH_UNSAFE

    def test_a_detached_head_where_a_branch_was_expected_is_drift(self, executor):
        with patch('subprocess.run', side_effect=_git_stub(branch_stdout='HEAD')):
            check = executor._verify_failsafe_branch(
                project_dir='/workspace/x',
                project_name='test-project',
                issue_number=7,
                expected_branch='feature/issue-5-epic',
            )
            assert check.commit_branch is None
            assert check.refusal is FailsafeOutcome.BRANCH_DRIFTED

    @pytest.mark.parametrize('default_branch', ['main', 'master'])
    def test_refuses_the_default_branch_even_without_an_expectation(self, executor, default_branch):
        """The refusal both guarded paths already enforce unconditionally.

        UNSAFE rather than DRIFTED, and so not escalated: the shared base clone
        sitting on its default branch is the NORMAL state of a dispatch that never
        resolved a workspace (dev_environment_setup and friends), and blocking the
        board on it would be an over-escalation, not a safety net.
        """
        with patch('subprocess.run', side_effect=_git_stub(branch_stdout=default_branch)):
            check = executor._verify_failsafe_branch(
                project_dir='/workspace/x',
                project_name='test-project',
                issue_number=7,
                expected_branch=None,
            )
            assert check.commit_branch is None
            assert check.refusal is FailsafeOutcome.BRANCH_UNSAFE
            assert check.refusal.branch_refused is False

    @pytest.mark.parametrize('default_branch', ['main', 'master'])
    def test_the_default_branch_where_a_feature_branch_was_expected_is_drift(
        self, executor, default_branch
    ):
        with patch('subprocess.run', side_effect=_git_stub(branch_stdout=default_branch)):
            check = executor._verify_failsafe_branch(
                project_dir='/workspace/x',
                project_name='test-project',
                issue_number=7,
                expected_branch='feature/issue-5-epic',
            )
            assert check.commit_branch is None
            assert check.refusal is FailsafeOutcome.BRANCH_DRIFTED
            assert check.refusal.branch_refused is True

    def test_refuses_when_the_branch_cannot_be_read(self, executor):
        """A DIFFERENT refusal from the drifted ones above: nothing was observed
        on disk, so this one must not quarantine the epic."""
        with patch('subprocess.run', side_effect=_git_stub(branch_rc=128)):
            check = executor._verify_failsafe_branch(
                project_dir='/workspace/x',
                project_name='test-project',
                issue_number=7,
                expected_branch='feature/issue-5-epic',
            )
            assert check.commit_branch is None
            assert check.refusal is FailsafeOutcome.BRANCH_UNVERIFIABLE
            assert check.current_branch is None

    def test_a_feature_branch_with_no_expectation_is_still_allowed(self, executor):
        """Callers with no resolved workspace to read an expectation from are
        degraded, not blocked -- the same trade the other two paths make."""
        with patch('subprocess.run', side_effect=_git_stub(branch_stdout='feature/issue-9')):
            assert executor._verify_failsafe_branch(
                project_dir='/workspace/x',
                project_name='test-project',
                issue_number=7,
                expected_branch=None,
            ).commit_branch == 'feature/issue-9'


class TestFailsafeCommitCheckHonorsTheExpectation:

    @pytest.mark.asyncio
    async def test_a_wrong_branch_stages_nothing(self, executor):
        with patch('subprocess.run', side_effect=_git_stub(
                branch_stdout='fix-attempt', status_stdout='MM app.py')) as mock_run, \
             patch.object(executor, '_failsafe_stage_and_commit', new_callable=AsyncMock) as mock_commit:

            result = await executor._failsafe_commit_check(
                project_name='test-project',
                agent_name='repair_fix',
                task_context={
                    'issue_number': 7,
                    'project_dir': '/workspace/x',
                    'branch_name': 'feature/issue-5-epic',
                },
                task_id='task-1',
            )

            assert result.outcome is FailsafeOutcome.BRANCH_DRIFTED
            assert result.handled is False
            assert result.branch_refused is True
            assert result.current_branch == 'fix-attempt'
            mock_commit.assert_not_called()
            # Refused before even reading git status -- nothing but the branch
            # read may happen against a workspace we won't commit from.
            assert all('status' not in call.args[0] for call in mock_run.call_args_list)

    @pytest.mark.asyncio
    async def test_the_matching_branch_commits_as_before(self, executor):
        # 'MM' -- staged AND unstaged, the mixed state that routes to
        # _failsafe_stage_and_commit (git status output is stripped before
        # parsing, so a leading-space code would not survive).
        with patch('subprocess.run', side_effect=_git_stub(status_stdout='MM app.py')), \
             patch.object(executor, '_failsafe_stage_and_commit', new_callable=AsyncMock,
                          return_value=True) as mock_commit:

            result = await executor._failsafe_commit_check(
                project_name='test-project',
                agent_name='repair_fix',
                task_context={
                    'issue_number': 7,
                    'project_dir': '/workspace/x',
                    'branch_name': 'feature/issue-5-epic',
                },
                task_id='task-1',
            )

            assert result.outcome is FailsafeOutcome.COMMITTED
            assert result.handled is True
            mock_commit.assert_awaited_once()
            # The verified branch is threaded through to the push rather than
            # re-read from ambient HEAD one step later.
            assert mock_commit.await_args[0][-1] == 'feature/issue-5-epic'

    @pytest.mark.asyncio
    async def test_a_clean_workspace_is_reported_handled(self, executor):
        with patch('subprocess.run', side_effect=_git_stub(status_stdout='')):
            result = await executor._failsafe_commit_check(
                project_name='test-project',
                agent_name='repair_fix',
                task_context={
                    'issue_number': 7,
                    'project_dir': '/workspace/x',
                    'branch_name': 'feature/issue-5-epic',
                },
                task_id='task-1',
            )
            assert result.outcome is FailsafeOutcome.CLEAN
            assert result.handled is True


class TestFailsafePushUsesTheVerifiedBranch:

    @pytest.mark.asyncio
    async def test_pushes_the_branch_it_was_given(self, executor):
        mock_git = MagicMock()
        mock_git.push_branch = AsyncMock()

        with patch('services.git_workflow_manager.git_workflow_manager', mock_git):
            await executor._failsafe_push(
                '/workspace/x', 'test-project', 7, 'feature/issue-5-epic'
            )

        mock_git.push_branch.assert_awaited_once_with(
            '/workspace/x', 'feature/issue-5-epic'
        )


def _git_stub_full(branch='feature/issue-5-epic', status='', add_rc=0, commit_rc=0):
    """subprocess.run stub covering the full failsafe sequence: the branch read,
    `git status --porcelain`, `git add -A` and `git commit`."""
    def _run(cmd, **kwargs):
        result = MagicMock()
        result.stdout = ''
        result.stderr = 'boom'
        result.returncode = 0
        if 'rev-parse' in cmd:
            result.stdout = branch
        elif 'status' in cmd:
            result.stdout = status
        elif 'add' in cmd:
            result.returncode = add_rc
        elif 'commit' in cmd:
            result.returncode = commit_rc
        return result
    return _run


class TestOnlyWorkThatReachedOriginCountsAsHandled:
    """
    _failsafe_commit_check() promised "True means the workspace was handled
    (committed, or confirmed to have nothing that needs committing)" and then
    answered True from four states in which nothing reached origin. The
    branch_unverifiable finalization path gates the whole pipeline block on that
    answer, so each of these used to end in record_execution_outcome(
    outcome='success') with the agent's work sitting uncommitted on disk -- the
    exact failure this work item exists to remove (#149 WI-4 review).
    """

    _CONTEXT = {
        'issue_number': 7,
        'project_dir': '/workspace/x',
        'branch_name': 'feature/issue-5-epic',
    }

    async def _check(self, executor, **stub_kwargs):
        with patch('subprocess.run', side_effect=_git_stub_full(**stub_kwargs)), \
             patch('glob.glob', return_value=[]):
            return await executor._failsafe_commit_check(
                project_name='test-project',
                agent_name='code_implementer',
                task_context=dict(self._CONTEXT),
                task_id='task-1',
            )

    @pytest.mark.asyncio
    async def test_untracked_only_changes_are_not_handled(self, executor):
        """An agent whose task was to CREATE files produces exactly this state.
        Nothing is committed here by design (the files may be build artifacts) --
        but calling that "handled" advanced the card with the whole deliverable
        untracked on disk."""
        result = await self._check(executor, status='?? new_module.py')

        assert result.outcome is FailsafeOutcome.NOT_COMMITTED
        assert result.handled is False
        # Not a branch refusal: the branch was fine, so nothing gets quarantined.
        assert result.branch_refused is False

    @pytest.mark.asyncio
    async def test_a_failed_commit_is_not_handled(self, executor):
        result = await self._check(executor, status=' M app.py', commit_rc=1)

        assert result.outcome is FailsafeOutcome.NOT_COMMITTED
        assert result.handled is False

    @pytest.mark.asyncio
    async def test_a_failed_git_add_is_not_handled(self, executor):
        # 'MM' -- the mixed state, the only one of the three that routes through
        # _failsafe_stage_and_commit()'s `git add -A` (git status output is
        # stripped before parsing, so an unstaged-only ' M' code cannot survive).
        result = await self._check(executor, status='MM app.py', add_rc=1)

        assert result.outcome is FailsafeOutcome.NOT_COMMITTED
        assert result.handled is False

    @pytest.mark.asyncio
    async def test_a_push_that_throws_leaves_the_commit_local_only(self, executor):
        """_failsafe_push() swallows every non-PushFailedError at warning level.
        The commit exists locally, but nothing is on origin and no PR follows."""
        mock_git = MagicMock()
        mock_git.push_branch = AsyncMock(side_effect=RuntimeError('ssh died'))

        with patch('services.git_workflow_manager.git_workflow_manager', mock_git):
            result = await self._check(executor, status=' M app.py')

        assert result.outcome is FailsafeOutcome.NOT_COMMITTED
        assert result.handled is False

    @pytest.mark.asyncio
    async def test_a_committed_and_pushed_workspace_is_handled(self, executor):
        """The control: the one path that really does put the work on origin."""
        mock_git = MagicMock()
        mock_git.push_branch = AsyncMock()

        with patch('services.git_workflow_manager.git_workflow_manager', mock_git):
            result = await self._check(executor, status=' M app.py')

        assert result.outcome is FailsafeOutcome.COMMITTED
        assert result.handled is True
        mock_git.push_branch.assert_awaited_once_with('/workspace/x', 'feature/issue-5-epic')
