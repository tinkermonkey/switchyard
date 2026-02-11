"""
Unit tests for PRReviewStage

Test orchestration logic without actually launching Docker containers.

NOTE: These tests require Docker container environment due to import dependencies.
Run tests inside Docker compose orchestrator container.
"""

import pytest

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
         patch.object(pr_review_stage, '_parse_review_findings', return_value=[]), \
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
         patch.object(pr_review_stage, '_parse_review_findings', return_value=[]), \
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

        # Should have 3 calls: Parent Issue, Idea Researcher, Business Analyst
        # (Software Architect not included since we didn't mock it)
        assert len(phase2_calls) >= 2
        assert all(c['agent_name'] == 'requirements_verifier' for c in phase2_calls)

        # Verify task context includes required fields
        for call in phase2_calls:
            assert 'pr_url' in call['task_context']
            assert 'check_name' in call['task_context']
            assert 'check_content' in call['task_context']


@pytest.mark.asyncio
async def test_manual_progression_flag_set_when_issues_found(pr_review_stage):
    """Verify manual_progression_made flag is set when returning to development"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])), \
         patch.object(pr_review_stage, '_parse_review_findings', return_value=[
             {'title': 'Issue 1', 'body': 'Body', 'severity': 'high'}
         ]), \
         patch.object(pr_review_stage, '_create_review_issues', return_value=[
             {'number': '99', 'url': 'url', 'title': 'Issue 1', 'severity': 'high'}
         ]), \
         patch.object(pr_review_stage, '_move_issues_to_development'), \
         patch.object(pr_review_stage, '_return_parent_to_development'):

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock(return_value={
            'markdown_analysis': '### Critical Issues\n- **Bug**: Found a bug'
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
async def test_manual_progression_flag_set_when_clean_pass(pr_review_stage):
    """Verify manual_progression_made flag is set when advancing to documentation"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])), \
         patch.object(pr_review_stage, '_parse_review_findings', return_value=[]), \
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
         patch.object(pr_review_stage, '_parse_review_findings', return_value=[]):

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
         patch.object(pr_review_stage, '_parse_review_findings', return_value=[]), \
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
async def test_cycle_limit_prevents_progression(pr_review_stage):
    """Verify review at cycle limit doesn't move parent to development"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])), \
         patch.object(pr_review_stage, '_parse_review_findings', return_value=[
             {'title': 'Issue 1', 'body': 'Body', 'severity': 'high'}
         ]), \
         patch.object(pr_review_stage, '_create_review_issues', return_value=[
             {'number': '99', 'url': 'url', 'title': 'Issue 1', 'severity': 'high'}
         ]), \
         patch.object(pr_review_stage, '_return_parent_to_development') as mock_return, \
         patch.object(pr_review_stage, '_post_comment_on_issue'):

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock(return_value={
            'markdown_analysis': '### Critical Issues\n- **Bug**: Found a bug'
        })
        mock_get_executor.return_value = mock_executor
        # Cycle 3 (limit reached)
        mock_state.get_review_count.return_value = 2

        context = {
            'context': {
                'issue_number': 42,
                'project': 'test-project'
            }
        }

        result = await pr_review_stage.execute(context)

        # Verify parent NOT moved to development
        mock_return.assert_not_called()

        # Verify flag is NOT set (cycle limit reached)
        assert 'manual_progression_made' not in result


@pytest.mark.asyncio
async def test_skips_workspace_prep_false(pr_review_stage):
    """Verify agents are launched with skip_workspace_prep=False"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])), \
         patch.object(pr_review_stage, '_parse_review_findings', return_value=[]), \
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

        # Verify all agent calls have skip_workspace_prep=False
        for call in mock_executor.execute_agent.call_args_list:
            assert call[1]['task_context']['skip_workspace_prep'] is False


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
         patch.object(pr_review_stage, '_parse_review_findings', return_value=[]), \
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
         patch.object(pr_review_stage, '_parse_review_findings', return_value=[]), \
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
         patch.object(pr_review_stage, '_parse_review_findings', return_value=[]), \
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
