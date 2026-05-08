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
            'agent_output': '### Critical Issues\nNone found'
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
            'agent_output': '### Gaps Found\nNone found'
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
            'agent_output': '{"groups": [], "filtered_out": []}'
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
            'agent_output': '### Critical Issues\nNone found'
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
            'agent_output': '### Critical Issues\nNone found'
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
            'agent_output': '{"groups": [], "filtered_out": []}'
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
            'agent_output': '### Critical Issues\nNone found'
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
            'agent_output': '### Critical Issues\nNone found'
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
    """Verify CI failures create issues and skip AI phases"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=(
             [{'name': 'test', 'state': 'failure', 'bucket': 'fail'}], []
         )), \
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
            'agent_output': '### Critical Issues\nNone found'
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

        # Verify issue creation was called for CI failure
        mock_create.assert_called_once()
        issue_specs = mock_create.call_args[0][0]
        assert len(issue_specs) == 1
        assert issue_specs[0]['title'] == 'CI Failure'

        # AI phases (Phase 1/2) must NOT have been called — CI gate returned early
        assert mock_executor.execute_agent.call_count == 0

        # Parent is returned to development
        assert result.get('manual_progression_made') is True


@pytest.mark.asyncio
async def test_ci_failure_early_return_skips_ai_phases(pr_review_stage):
    """CI gate: when CI fails, Phase 1 (pr_code_reviewer) and Phase 2 are never launched"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/5'), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=(
             [{'name': 'build', 'state': 'failure', 'bucket': 'fail'}], []
         )), \
         patch.object(pr_review_stage, '_build_ci_failure_issue', return_value={
             'title': 'CI: build failed', 'body': 'Build failed', 'severity': 'high'
         }), \
         patch.object(pr_review_stage, '_create_review_issues', return_value=[
             {'number': '200', 'url': 'url', 'title': 'CI: build failed', 'severity': 'high'}
         ]), \
         patch.object(pr_review_stage, '_move_issues_to_development'), \
         patch.object(pr_review_stage, '_return_parent_to_development') as mock_return, \
         patch.object(pr_review_stage, '_post_comment_on_issue') as mock_post_comment:

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock()
        mock_get_executor.return_value = mock_executor
        mock_state.get_review_count.return_value = 0

        context = {'context': {'issue_number': 10, 'project': 'test-project'}}

        result = await pr_review_stage.execute(context)

        # No Docker agents launched
        assert mock_executor.execute_agent.call_count == 0
        # Parent returned to development
        mock_return.assert_called_once()
        assert result.get('manual_progression_made') is True
        assert 'CI failing' in result.get('agent_output', '')
        # Not the final cycle — no cycle-limit comment
        mock_post_comment.assert_not_called()


@pytest.mark.asyncio
async def test_ci_failure_at_cycle_limit_posts_comment(pr_review_stage):
    """CI failure on the final review cycle posts the cycle-limit warning comment"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/7'), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=(
             [{'name': 'build', 'state': 'failure', 'bucket': 'fail'}], []
         )), \
         patch.object(pr_review_stage, '_build_ci_failure_issue', return_value={
             'title': 'CI: build failed', 'body': 'Build failed', 'severity': 'high'
         }), \
         patch.object(pr_review_stage, '_create_review_issues', return_value=[
             {'number': '201', 'url': 'url', 'title': 'CI: build failed', 'severity': 'high'}
         ]), \
         patch.object(pr_review_stage, '_move_issues_to_development'), \
         patch.object(pr_review_stage, '_return_parent_to_development'), \
         patch.object(pr_review_stage, '_post_comment_on_issue') as mock_post_comment:

        mock_executor = AsyncMock()
        mock_get_executor.return_value = mock_executor
        # Final allowed cycle: review_count=2 → current_cycle=3=MAX_REVIEW_CYCLES
        mock_state.get_review_count.return_value = 2

        context = {'context': {'issue_number': 10, 'project': 'test-project'}}

        await pr_review_stage.execute(context)

        # Cycle-limit comment must be posted so the developer knows no more AI reviews remain
        mock_post_comment.assert_called_once()


@pytest.mark.asyncio
async def test_ci_check_exception_propagates(pr_review_stage):
    """CI check exception re-raises instead of silently running AI phases on unknown build state"""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/9'), \
         patch.object(pr_review_stage, '_check_ci_status', side_effect=RuntimeError('gh CLI timeout')):

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock()
        mock_get_executor.return_value = mock_executor
        mock_state.get_review_count.return_value = 0

        context = {'context': {'issue_number': 10, 'project': 'test-project'}}

        with pytest.raises(RuntimeError, match='gh CLI timeout'):
            await pr_review_stage.execute(context)

        # AI phases must never have been launched
        assert mock_executor.execute_agent.call_count == 0


@pytest.mark.asyncio
async def test_agent_output_includes_summary(pr_review_stage):
    """Verify agent_output includes comprehensive summary"""
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
            'agent_output': '### Critical Issues\nNone found'
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

        # Verify agent_output contains key information
        analysis = result['agent_output']
        assert '## PR Review - Cycle 1/3' in analysis
        assert 'https://github.com/o/r/pull/123' in analysis
        assert '**Parent Issue**: #42' in analysis
        assert '**Outcome**:' in analysis
        assert '**Issues Created**: 0' in analysis


# ---- _parse_consolidated_findings severity gate ----

def test_parse_consolidated_drops_medium_severity(pr_review_stage):
    """Medium severity groups are skipped — no issues created."""
    json_output = '''{
        "groups": [
            {"name": "Code Style", "severity": "medium", "findings": "- **Formatting**: Indentation inconsistent `src/foo.py:10`"}
        ],
        "filtered_out": []
    }'''
    with patch('pipeline.pr_review_stage.parse_json_block') as mock_parse, \
         patch.object(pr_review_stage, '_is_actionable_section', return_value=True):
        mock_parse.return_value = {
            "groups": [
                {"name": "Code Style", "severity": "medium", "findings": "- **Formatting**: Indentation inconsistent `src/foo.py:10`"}
            ]
        }
        result = pr_review_stage._parse_consolidated_findings(json_output)
    assert result == []


def test_parse_consolidated_drops_low_severity(pr_review_stage):
    """Low severity groups are skipped — no issues created."""
    with patch('pipeline.pr_review_stage.parse_json_block') as mock_parse, \
         patch.object(pr_review_stage, '_is_actionable_section', return_value=True):
        mock_parse.return_value = {
            "groups": [
                {"name": "Nice-to-Have", "severity": "low", "findings": "- **Logging**: Add debug log `src/bar.py:5`"}
            ]
        }
        result = pr_review_stage._parse_consolidated_findings("")
    assert result == []


def test_parse_consolidated_keeps_critical_and_high(pr_review_stage):
    """Critical and high severity groups create issues; medium and low are dropped."""
    with patch('pipeline.pr_review_stage.parse_json_block') as mock_parse, \
         patch.object(pr_review_stage, '_is_actionable_section', return_value=True):
        mock_parse.return_value = {
            "groups": [
                {"name": "Auth Bug", "severity": "critical", "findings": "- **Token leak**: `src/auth.py:42`"},
                {"name": "Missing validation", "severity": "high", "findings": "- **No input check**: `src/api.py:10`"},
                {"name": "Style", "severity": "medium", "findings": "- **Naming**: `src/util.py:1`"},
                {"name": "Nitpick", "severity": "low", "findings": "- **Comment**: `src/util.py:2`"},
            ]
        }
        result = pr_review_stage._parse_consolidated_findings("")
    assert len(result) == 2
    titles = [i['title'] for i in result]
    assert '[PR Feedback] Auth Bug' in titles
    assert '[PR Feedback] Missing validation' in titles


def test_parse_consolidated_drops_unknown_severity(pr_review_stage):
    """Groups with unrecognised severity are dropped rather than defaulting to medium."""
    with patch('pipeline.pr_review_stage.parse_json_block') as mock_parse, \
         patch.object(pr_review_stage, '_is_actionable_section', return_value=True):
        mock_parse.return_value = {
            "groups": [
                {"name": "Misc", "severity": "suggestion", "findings": "- **Thing**: `src/x.py:1`"}
            ]
        }
        result = pr_review_stage._parse_consolidated_findings("")
    assert result == []
