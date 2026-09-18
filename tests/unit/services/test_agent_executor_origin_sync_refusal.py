"""
Regression tests for the failsafe path's epic-worktree origin-sync refusal.

When the failsafe branch check passes, HEAD is already on the expected epic
branch. A later refusal from sync_epic_worktree_before_commit() therefore means
"this branch is stale or diverged relative to origin", not "HEAD is on the
wrong branch". The operator-facing comment must say so, and the call site must
thread that reason into the shared blocking helper.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.non_retryable import NonRetryableAgentError


@pytest.fixture
def executor():
    from services.agent_executor import AgentExecutor

    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        return AgentExecutor()


class TestOriginSyncRefusalComment:
    @pytest.mark.asyncio
    async def test_comment_describes_origin_sync_not_wrong_branch(self, executor):
        captured = {}

        async def _capture(**kwargs):
            captured['failure_label'] = kwargs['failure_label']
            captured['comment'] = kwargs['build_comment']('_lock retained_', 'dev_workflow')

        with patch.object(executor, '_block_pipeline_with_comment', side_effect=_capture):
            with pytest.raises(NonRetryableAgentError):
                await executor._handle_wrong_branch_refusal(
                    project_name='test-project',
                    task_context={
                        'issue_number': 7,
                        'project_dir': '/workspace/.orchestrator/worktrees/test-project/5',
                        'branch_name': 'feature/issue-5-epic',
                    },
                    pipeline_run_id='run-1',
                    error_detail='Refusing failsafe commit: branch diverged from origin',
                    expected_branch='feature/issue-5-epic',
                    current_branch='feature/issue-5-epic',
                    origin_sync_detail='branch diverged from origin',
                )

        assert captured['failure_label'] == 'an origin-sync refusal'
        assert 'Out of Sync with Origin' in captured['comment']
        assert "workspace is on this issue's branch" in captured['comment']
        assert "not on this issue's branch" not in captured['comment']
        assert 'origin/feature/issue-5-epic' in captured['comment']
        assert 'reset --hard origin/feature/issue-5-epic' in captured['comment']


class TestFailsafeSyncRefusalCallSite:
    @staticmethod
    def _git_branch_only(cmd, **kwargs):
        result = MagicMock()
        if cmd == ['git', 'rev-parse', '--abbrev-ref', 'HEAD']:
            result.returncode = 0
            result.stdout = 'feature/issue-5-epic'
            result.stderr = ''
            return result
        raise AssertionError(f"Unexpected subprocess.run call after branch read: {cmd}")

    @pytest.mark.asyncio
    async def test_failsafe_threads_origin_sync_detail_into_blocking_helper(self, executor):
        with patch('subprocess.run', side_effect=self._git_branch_only), \
             patch('services.project_workspace.workspace_manager.is_base_clone_dir', return_value=False), \
             patch(
                 'services.git_workflow_manager.git_workflow_manager.sync_epic_worktree_before_commit',
                 new=AsyncMock(return_value=SimpleNamespace(
                     ok=False,
                     detail='Epic worktree has diverged from origin/feature/issue-5-epic',
                 )),
             ), \
             patch.object(executor, '_handle_wrong_branch_refusal', new_callable=AsyncMock) as mock_handle:

            result = await executor._failsafe_commit_check(
                project_name='test-project',
                agent_name='repair_fix',
                task_context={
                    'issue_number': 7,
                    'project_dir': '/workspace/.orchestrator/worktrees/test-project/5',
                    'branch_name': 'feature/issue-5-epic',
                    'pipeline_run_id': 'run-1',
                },
                task_id='task-1',
            )

        assert result.commit_branch is None
        mock_handle.assert_awaited_once()
        assert mock_handle.await_args.kwargs['current_branch'] == 'feature/issue-5-epic'
        assert mock_handle.await_args.kwargs['expected_branch'] == 'feature/issue-5-epic'
        assert (
            mock_handle.await_args.kwargs['origin_sync_detail']
            == 'Epic worktree has diverged from origin/feature/issue-5-epic'
        )
