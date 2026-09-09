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
            ) == 'feature/issue-5-epic'

    def test_refuses_another_issues_branch(self, executor):
        """THE regression: the repair agent's container left HEAD on
        `fix-attempt`, and the failsafe pushed the repair edits there."""
        with patch('subprocess.run', side_effect=_git_stub(branch_stdout='fix-attempt')):
            assert executor._verify_failsafe_branch(
                project_dir='/workspace/x',
                project_name='test-project',
                issue_number=7,
                expected_branch='feature/issue-5-epic',
            ) is None

    def test_refuses_a_detached_head(self, executor):
        """push_branch(project_dir, 'HEAD') fails and is logged at warning level
        only, leaving a dangling commit."""
        with patch('subprocess.run', side_effect=_git_stub(branch_stdout='HEAD')):
            assert executor._verify_failsafe_branch(
                project_dir='/workspace/x',
                project_name='test-project',
                issue_number=7,
                expected_branch=None,
            ) is None

    @pytest.mark.parametrize('default_branch', ['main', 'master'])
    def test_refuses_the_default_branch_even_without_an_expectation(self, executor, default_branch):
        """The refusal both guarded paths already enforce unconditionally."""
        with patch('subprocess.run', side_effect=_git_stub(branch_stdout=default_branch)):
            assert executor._verify_failsafe_branch(
                project_dir='/workspace/x',
                project_name='test-project',
                issue_number=7,
                expected_branch=None,
            ) is None

    def test_refuses_when_the_branch_cannot_be_read(self, executor):
        with patch('subprocess.run', side_effect=_git_stub(branch_rc=128)):
            assert executor._verify_failsafe_branch(
                project_dir='/workspace/x',
                project_name='test-project',
                issue_number=7,
                expected_branch='feature/issue-5-epic',
            ) is None

    def test_a_feature_branch_with_no_expectation_is_still_allowed(self, executor):
        """Callers with no resolved workspace to read an expectation from are
        degraded, not blocked -- the same trade the other two paths make."""
        with patch('subprocess.run', side_effect=_git_stub(branch_stdout='feature/issue-9')):
            assert executor._verify_failsafe_branch(
                project_dir='/workspace/x',
                project_name='test-project',
                issue_number=7,
                expected_branch=None,
            ) == 'feature/issue-9'


class TestFailsafeCommitCheckHonorsTheExpectation:

    @pytest.mark.asyncio
    async def test_a_wrong_branch_stages_nothing(self, executor):
        with patch('subprocess.run', side_effect=_git_stub(
                branch_stdout='fix-attempt', status_stdout='MM app.py')) as mock_run, \
             patch.object(executor, '_failsafe_stage_and_commit', new_callable=AsyncMock) as mock_commit:

            handled = await executor._failsafe_commit_check(
                project_name='test-project',
                agent_name='repair_fix',
                task_context={
                    'issue_number': 7,
                    'project_dir': '/workspace/x',
                    'branch_name': 'feature/issue-5-epic',
                },
                task_id='task-1',
            )

            assert handled is False
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
             patch.object(executor, '_failsafe_stage_and_commit', new_callable=AsyncMock) as mock_commit:

            handled = await executor._failsafe_commit_check(
                project_name='test-project',
                agent_name='repair_fix',
                task_context={
                    'issue_number': 7,
                    'project_dir': '/workspace/x',
                    'branch_name': 'feature/issue-5-epic',
                },
                task_id='task-1',
            )

            assert handled is True
            mock_commit.assert_awaited_once()
            # The verified branch is threaded through to the push rather than
            # re-read from ambient HEAD one step later.
            assert mock_commit.await_args[0][-1] == 'feature/issue-5-epic'

    @pytest.mark.asyncio
    async def test_a_clean_workspace_is_reported_handled(self, executor):
        with patch('subprocess.run', side_effect=_git_stub(status_stdout='')):
            handled = await executor._failsafe_commit_check(
                project_name='test-project',
                agent_name='repair_fix',
                task_context={
                    'issue_number': 7,
                    'project_dir': '/workspace/x',
                    'branch_name': 'feature/issue-5-epic',
                },
                task_id='task-1',
            )
            assert handled is True


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
