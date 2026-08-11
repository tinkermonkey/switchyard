"""
Unit tests for AgentExecutor._apply_frozen_session_resume() — the fork that
resumes a Claude Code session (with a short continuation prompt) instead of
rebuilding a stage's normal prompt, when the immediately-preceding execution
for the same (project, issue, column, agent) was frozen by the Claude Code
breaker with evidence of prior progress.
"""
import pytest
from unittest.mock import patch

import os
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from services.agent_executor import AgentExecutor


@pytest.fixture
def agent_executor():
    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        return AgentExecutor()


class TestFrozenSessionResumeFork:
    def test_resumes_session_when_captured_with_prior_progress(self, agent_executor):
        execution_context = {}
        task_context = {'issue_number': 42, 'column': 'Development'}

        with patch('services.work_execution_state.work_execution_tracker') as mock_tracker:
            mock_tracker.get_resumable_frozen_session.return_value = 'abc-session-id'
            agent_executor._apply_frozen_session_resume(
                execution_context, task_context, 'proj', 'senior_software_engineer'
            )

        mock_tracker.get_resumable_frozen_session.assert_called_once_with(
            'proj', 42, 'Development', 'senior_software_engineer'
        )
        assert execution_context['claude_session_id'] == 'abc-session-id'
        assert 'continue' in task_context['direct_prompt'].lower()

    def test_no_op_when_no_resumable_session(self, agent_executor):
        """The common case: a first-turn rejection captured nothing to resume."""
        execution_context = {}
        task_context = {'issue_number': 42, 'column': 'Development'}

        with patch('services.work_execution_state.work_execution_tracker') as mock_tracker:
            mock_tracker.get_resumable_frozen_session.return_value = None
            agent_executor._apply_frozen_session_resume(
                execution_context, task_context, 'proj', 'senior_software_engineer'
            )

        assert 'claude_session_id' not in execution_context
        assert 'direct_prompt' not in task_context

    def test_no_op_when_no_issue_number(self, agent_executor):
        execution_context = {}
        task_context = {}

        with patch('services.work_execution_state.work_execution_tracker') as mock_tracker:
            agent_executor._apply_frozen_session_resume(
                execution_context, task_context, 'proj', 'senior_software_engineer'
            )

        mock_tracker.get_resumable_frozen_session.assert_not_called()
        assert 'claude_session_id' not in execution_context

    def test_never_overrides_caller_supplied_direct_prompt(self, agent_executor):
        """e.g. pr_review_stage's Phase 2 verification calls already set their
        own direct_prompt — must never be clobbered by this fork."""
        execution_context = {}
        task_context = {
            'issue_number': 42,
            'column': 'Code Review',
            'direct_prompt': 'Verify the PR against these specific requirements...',
        }

        with patch('services.work_execution_state.work_execution_tracker') as mock_tracker:
            agent_executor._apply_frozen_session_resume(
                execution_context, task_context, 'proj', 'requirements_verifier'
            )

        mock_tracker.get_resumable_frozen_session.assert_not_called()
        assert task_context['direct_prompt'] == 'Verify the PR against these specific requirements...'
        assert 'claude_session_id' not in execution_context

    def test_defaults_column_to_unknown_when_missing(self, agent_executor):
        execution_context = {}
        task_context = {'issue_number': 42}

        with patch('services.work_execution_state.work_execution_tracker') as mock_tracker:
            mock_tracker.get_resumable_frozen_session.return_value = None
            agent_executor._apply_frozen_session_resume(
                execution_context, task_context, 'proj', 'senior_software_engineer'
            )

        mock_tracker.get_resumable_frozen_session.assert_called_once_with(
            'proj', 42, 'unknown', 'senior_software_engineer'
        )

    def test_lookup_failure_does_not_raise(self, agent_executor):
        """A broken lookup must degrade to normal (non-resumed) execution, not
        crash the whole dispatch."""
        execution_context = {}
        task_context = {'issue_number': 42, 'column': 'Development'}

        with patch('services.work_execution_state.work_execution_tracker') as mock_tracker:
            mock_tracker.get_resumable_frozen_session.side_effect = Exception("boom")
            agent_executor._apply_frozen_session_resume(
                execution_context, task_context, 'proj', 'senior_software_engineer'
            )

        assert 'claude_session_id' not in execution_context
        assert 'direct_prompt' not in task_context
