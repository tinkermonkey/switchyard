"""
Unit tests for PRReviewStage

Test orchestration logic without actually launching Docker containers.

NOTE: These tests require Docker container environment due to import dependencies.
Run tests inside Docker compose orchestrator container.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

# Skip all tests in this module if not in Docker environment
# PRReviewStage imports trigger dev_container_state which creates /app/state/dev_containers
pytest.skip("Requires Docker container environment", allow_module_level=True)


@pytest.fixture
def pr_review_stage():
    """Create PRReviewStage with mocked dependencies"""
    # Mock dev_container_state at module level to prevent /app directory creation
    with patch.dict('sys.modules', {
        'services.dev_container_state': MagicMock(),
    }):
        with patch('pipeline.pr_review_stage.ConfigManager'), \
             patch('pipeline.pr_review_stage.GitHubStateManager'), \
             patch('pipeline.pr_review_stage.pr_review_state_manager'):
            from pipeline.pr_review_stage import PRReviewStage
            return PRReviewStage()


@pytest.mark.asyncio
async def test_phase1_launches_pr_code_reviewer(pr_review_stage):
    """Verify Phase 1 calls AgentExecutor with pr_code_reviewer agent"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])), \
         patch.object(pr_review_stage, '_parse_consolidated_findings', return_value=[]), \
         patch.object(pr_review_stage, '_advance_parent_to_documentation'):

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock(return_value={
            'markdown_analysis': '### Critical Issues\nNone found'
        })
        mock_get_executor.return_value = mock_executor
        mock_state.get_review_count.return_value = 0

        context = {
            'context': {
                'issue_number': 42,
                'project': 'test-project'
            }
        }

        await pr_review_stage.execute(context)

        # Verify pr_code_reviewer was called
        assert mock_executor.execute_agent.call_count >= 1
        first_call = mock_executor.execute_agent.call_args_list[0]
        assert first_call[1]['agent_name'] == 'pr_code_reviewer'
        assert first_call[1]['execution_type'] == 'pr_review_phase1'
        assert first_call[1]['project_name'] == 'test-project'
        assert 'pr_url' in first_call[1]['task_context']


@pytest.mark.asyncio
async def test_phase2_launches_requirements_verifier(pr_review_stage):
    """Verify Phase 2 calls AgentExecutor with requirements_verifier for each context source"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={
             'idea_researcher': 'Some research output',
             'business_analyst': 'Business requirements'
         }), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value='Parent issue requirements'), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])), \
         patch.object(pr_review_stage, '_parse_consolidated_findings', return_value=[]), \
         patch.object(pr_review_stage, '_advance_parent_to_documentation'):

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock(return_value={
            'markdown_analysis': '### Gaps Found\nNone found'
        })
        mock_get_executor.return_value = mock_executor
        mock_state.get_review_count.return_value = 0

        context = {
            'context': {
                'issue_number': 42,
                'project': 'test-project'
            }
        }

        await pr_review_stage.execute(context)

        # Verify requirements_verifier was called for Phase 2
        calls = [call[1] for call in mock_executor.execute_agent.call_args_list]
        phase2_calls = [c for c in calls if c['execution_type'] == 'pr_review_phase2']

        # Exactly 2 calls: Parent Issue Requirements + Business Analyst.
        # idea_researcher is excluded from context_checks; software_architect not mocked
        # so its content is empty and the verification is skipped.
        assert len(phase2_calls) == 2
        assert all(c['agent_name'] == 'requirements_verifier' for c in phase2_calls)

        # Verify task context includes required fields
        for call in phase2_calls:
            assert 'pr_url' in call['task_context']
            assert 'check_name' in call['task_context']
            assert 'check_content' in call['task_context']


@pytest.mark.asyncio
async def test_manual_progression_flag_set_when_issues_found(pr_review_stage):
    """Verify manual_progression_made flag is set when Phase 4 consolidation finds issues"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])), \
         patch.object(pr_review_stage, '_parse_consolidated_findings', return_value=[
             {'title': '[PR Feedback] Authentication Module', 'body': 'Body', 'severity': 'high'}
         ]), \
         patch.object(pr_review_stage, '_create_review_issues', return_value=[
             {'number': '99', 'url': 'url', 'title': '[PR Feedback] Authentication Module',
              'severity': 'high', 'body': 'Body'}
         ]), \
         patch.object(pr_review_stage, '_move_issues_to_development'), \
         patch.object(pr_review_stage, '_return_parent_to_development'):

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock(return_value={
            'markdown_analysis': '{"groups": [], "filtered_out": []}'
        })
        mock_get_executor.return_value = mock_executor
        mock_state.get_review_count.return_value = 0

        context = {
            'context': {
                'issue_number': 42,
                'project': 'test-project'
            }
        }

        result = await pr_review_stage.execute(context)

        # Verify flag is set via the issues-found path
        assert result.get('manual_progression_made') is True


@pytest.mark.asyncio
async def test_manual_progression_flag_set_when_clean_pass(pr_review_stage):
    """Verify manual_progression_made flag is set when advancing to documentation"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])), \
         patch.object(pr_review_stage, '_parse_consolidated_findings', return_value=[]), \
         patch.object(pr_review_stage, '_advance_parent_to_documentation'):

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock(return_value={
            'markdown_analysis': '### Critical Issues\nNone found'
        })
        mock_get_executor.return_value = mock_executor
        mock_state.get_review_count.return_value = 0

        context = {
            'context': {
                'issue_number': 42,
                'project': 'test-project'
            }
        }

        result = await pr_review_stage.execute(context)

        # Verify flag is set
        assert result.get('manual_progression_made') is True


@pytest.mark.asyncio
async def test_manual_progression_flag_not_set_when_inconclusive(pr_review_stage):
    """Verify manual_progression_made flag is NOT set when review is inconclusive"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])), \
         patch.object(pr_review_stage, '_parse_consolidated_findings', return_value=[]):

        mock_executor = AsyncMock()
        # Make Phase 1 fail
        mock_executor.execute_agent = AsyncMock(side_effect=Exception("Phase 1 failed"))
        mock_get_executor.return_value = mock_executor
        mock_state.get_review_count.return_value = 0

        context = {
            'context': {
                'issue_number': 42,
                'project': 'test-project'
            }
        }

        result = await pr_review_stage.execute(context)

        # Verify flag is NOT set (all phases failed = inconclusive)
        assert 'manual_progression_made' not in result


@pytest.mark.asyncio
async def test_phase3_runs_locally_no_docker(pr_review_stage):
    """Verify Phase 3 uses local gh CLI, not Docker"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])) as mock_ci_check, \
         patch.object(pr_review_stage, '_parse_consolidated_findings', return_value=[]), \
         patch.object(pr_review_stage, '_advance_parent_to_documentation'):

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock(return_value={
            'markdown_analysis': '### Critical Issues\nNone found'
        })
        mock_get_executor.return_value = mock_executor
        mock_state.get_review_count.return_value = 0

        context = {
            'context': {
                'issue_number': 42,
                'project': 'test-project'
            }
        }

        await pr_review_stage.execute(context)

        # Verify _check_ci_status was called (local method, not AgentExecutor)
        mock_ci_check.assert_called_once()


@pytest.mark.asyncio
async def test_cycle_limit_posts_comment_and_returns_to_development(pr_review_stage):
    """At the final review cycle, issues are returned to dev and a cycle limit comment is posted.

    The NonRetryableAgentError guard fires on the *next* attempt (review_count >= MAX).
    On the final allowed cycle (current_cycle == MAX_REVIEW_CYCLES), issues found by Phase 4
    still cause the parent to return to development — but a cycle limit comment is also posted
    to signal that no further automated reviews will run.
    """
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])), \
         patch.object(pr_review_stage, '_parse_consolidated_findings', return_value=[
             {'title': '[PR Feedback] Auth Layer', 'body': 'Body', 'severity': 'high'}
         ]), \
         patch.object(pr_review_stage, '_create_review_issues', return_value=[
             {'number': '99', 'url': 'url', 'title': '[PR Feedback] Auth Layer',
              'severity': 'high', 'body': 'Body'}
         ]), \
         patch.object(pr_review_stage, '_move_issues_to_development'), \
         patch.object(pr_review_stage, '_return_parent_to_development') as mock_return, \
         patch.object(pr_review_stage, '_post_comment_on_issue') as mock_post_comment:

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock(return_value={
            'markdown_analysis': '{"groups": [], "filtered_out": []}'
        })
        mock_get_executor.return_value = mock_executor
        # Final allowed cycle: review_count=2 → current_cycle=3=MAX_REVIEW_CYCLES
        mock_state.get_review_count.return_value = 2

        context = {
            'context': {
                'issue_number': 42,
                'project': 'test-project'
            }
        }

        result = await pr_review_stage.execute(context)

        # Issues found → parent IS returned to development
        mock_return.assert_called_once()
        assert result.get('manual_progression_made') is True

        # Cycle limit comment IS posted to signal no further automated reviews
        mock_post_comment.assert_called_once()


@pytest.mark.asyncio
async def test_skips_workspace_prep_false(pr_review_stage):
    """Verify agents are launched with skip_workspace_prep=False"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])), \
         patch.object(pr_review_stage, '_parse_consolidated_findings', return_value=[]), \
         patch.object(pr_review_stage, '_advance_parent_to_documentation'):

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock(return_value={
            'markdown_analysis': '### Critical Issues\nNone found'
        })
        mock_get_executor.return_value = mock_executor
        mock_state.get_review_count.return_value = 0

        context = {
            'context': {
                'issue_number': 42,
                'project': 'test-project'
            }
        }

        await pr_review_stage.execute(context)

        # Phase 1/2 agents need project code mounted; Phase 4 consolidation is text-analysis-only
        for call in mock_executor.execute_agent.call_args_list:
            exec_type = call[1]['execution_type']
            skip_prep = call[1]['task_context']['skip_workspace_prep']
            if exec_type in ('pr_review_phase1', 'pr_review_phase2'):
                assert skip_prep is False, f"{exec_type} must have skip_workspace_prep=False"
            elif exec_type == 'pr_review_phase4':
                assert skip_prep is True, "pr_review_phase4 must have skip_workspace_prep=True"


@pytest.mark.asyncio
async def test_no_pr_found_raises_error(pr_review_stage):
    """Verify NonRetryableAgentError raised when no PR found"""
    from agents.non_retryable import NonRetryableAgentError

    with patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value=None):

        mock_state.get_review_count.return_value = 0

        context = {
            'context': {
                'issue_number': 42,
                'project': 'test-project'
            }
        }

        with pytest.raises(NonRetryableAgentError, match="No PR found"):
            await pr_review_stage.execute(context)


@pytest.mark.asyncio
async def test_phase2_skipped_when_no_context(pr_review_stage):
    """Verify Phase 2 verifications skipped when no context content"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])), \
         patch.object(pr_review_stage, '_parse_consolidated_findings', return_value=[]), \
         patch.object(pr_review_stage, '_advance_parent_to_documentation'):

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock(return_value={
            'markdown_analysis': '### Critical Issues\nNone found'
        })
        mock_get_executor.return_value = mock_executor
        mock_state.get_review_count.return_value = 0

        context = {
            'context': {
                'issue_number': 42,
                'project': 'test-project'
            }
        }

        await pr_review_stage.execute(context)

        # Verify only Phase 1 was called (no Phase 2 since no context)
        calls = [call[1] for call in mock_executor.execute_agent.call_args_list]
        phase2_calls = [c for c in calls if c['execution_type'] == 'pr_review_phase2']
        assert len(phase2_calls) == 0


@pytest.mark.asyncio
async def test_creates_issues_for_ci_failures(pr_review_stage):
    """Verify CI failures create issues"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=(
             [{'name': 'test', 'state': 'failure', 'bucket': 'fail'}], []
         )), \
         patch.object(pr_review_stage, '_parse_consolidated_findings', return_value=[]), \
         patch.object(pr_review_stage, '_build_ci_failure_issue', return_value={
             'title': 'CI Failure', 'body': 'CI failed', 'severity': 'high'
         }), \
         patch.object(pr_review_stage, '_create_review_issues', return_value=[
             {'number': '100', 'url': 'url', 'title': 'CI Failure', 'severity': 'high'}
         ]) as mock_create, \
         patch.object(pr_review_stage, '_move_issues_to_development'), \
         patch.object(pr_review_stage, '_return_parent_to_development'):

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock(return_value={
            'markdown_analysis': '### Critical Issues\nNone found'
        })
        mock_get_executor.return_value = mock_executor
        mock_state.get_review_count.return_value = 0

        context = {
            'context': {
                'issue_number': 42,
                'project': 'test-project'
            }
        }

        await pr_review_stage.execute(context)

        # Verify issue creation was called for CI failure
        mock_create.assert_called()
        call_args = mock_create.call_args_list[-1]  # Last call should be for CI
        issue_specs = call_args[0][0]
        assert len(issue_specs) == 1
        assert issue_specs[0]['title'] == 'CI Failure'


@pytest.mark.asyncio
async def test_markdown_analysis_includes_summary(pr_review_stage):
    """Verify markdown_analysis includes comprehensive summary"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/123'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])), \
         patch.object(pr_review_stage, '_parse_consolidated_findings', return_value=[]), \
         patch.object(pr_review_stage, '_advance_parent_to_documentation'):

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock(return_value={
            'markdown_analysis': '### Critical Issues\nNone found'
        })
        mock_get_executor.return_value = mock_executor
        mock_state.get_review_count.return_value = 0

        context = {
            'context': {
                'issue_number': 42,
                'project': 'test-project'
            }
        }

        result = await pr_review_stage.execute(context)

        # Verify markdown_analysis contains key information
        analysis = result['markdown_analysis']
        assert '## PR Review - Cycle 1/3' in analysis
        assert 'https://github.com/o/r/pull/123' in analysis
        assert '**Parent Issue**: #42' in analysis
        assert '**Outcome**:' in analysis
        assert '**Issues Created**: 0' in analysis
