"""
Unit tests for PRReviewStage

Test orchestration logic without actually launching Docker containers.

Requires Docker container environment: pipeline.pr_review_stage's import chain
needs orchestrator config/state modules that only resolve correctly there. Run via
`docker-compose exec orchestrator python -m pytest tests/unit/test_pr_review_stage.py`.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


@pytest.fixture
def pr_review_stage():
    """Create PRReviewStage with mocked dependencies.

    Does NOT wrap this import in patch.dict('sys.modules', {'services.dev_container_state':
    MagicMock()}) — that was here historically to dodge a real module-level import of
    dev_container_state (which creates /app/state/dev_containers as a side effect), but
    services.agent_executor now only imports it lazily, inside functions, so the
    workaround is obsolete. Worse, wrapping pipeline.pr_review_stage's *first-ever*
    import of the process in a temporary sys.modules patch caused a double-import
    artifact: the resulting PRReviewStage instance's bound methods kept referencing a
    stale, orphaned copy of the module's globals that later per-test patch(...) calls
    (targeting the live, current sys.modules entry) never reached. In practice this
    silently fell through to the real pr_review_state_manager singleton instead of the
    mock — see test_phase1_launches_pr_code_reviewer, which failed this way by reading
    real leftover state (state/projects/test-project/pr_review_state.yaml) until this
    fixture was fixed.
    """
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
         patch.object(pr_review_stage, '_advance_parent_to_documentation', return_value=True):

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
        # Labeled on the container so restart recovery can checkpoint this exact
        # phase (see pipeline/pr_review_checkpoint.py).
        assert first_call[1]['task_context']['review_cycle'] == 1
        assert first_call[1]['task_context']['pr_review_checkpoint_phase'] == 'code_review'


@pytest.mark.asyncio
async def test_phase1_saves_checkpoint_after_completing(pr_review_stage):
    """Phase 1's real output must be checkpointed as soon as it completes, so a
    restart between Phase 1 and Phase 2 doesn't lose it."""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch('pipeline.pr_review_stage.PRReviewCheckpoint') as MockCheckpoint, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])), \
         patch.object(pr_review_stage, '_parse_consolidated_findings', return_value=[]), \
         patch.object(pr_review_stage, '_advance_parent_to_documentation', return_value=True):

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock(return_value={
            'agent_output': '### Critical Issues\nNone found'
        })
        mock_get_executor.return_value = mock_executor
        mock_state.get_review_count.return_value = 0

        mock_checkpoint = MockCheckpoint.return_value
        mock_checkpoint.get_phase_output.return_value = None  # nothing checkpointed yet

        context = {'context': {'issue_number': 42, 'project': 'test-project'}}
        await pr_review_stage.execute(context)

        mock_checkpoint.save_phase_output.assert_any_call(
            1, 'code_review', '### Critical Issues\nNone found'
        )
        # A cycle that reaches a real conclusion (clean pass here) must clear the
        # checkpoint so a later cycle never reuses this cycle's phase output.
        mock_checkpoint.clear_checkpoint.assert_called_once()


@pytest.mark.asyncio
async def test_phase1_reuses_checkpointed_output_after_restart_recovery(pr_review_stage):
    """If Phase 1's container already finished before an orchestrator restart (see
    claude/docker_runner.py's recovered-container handling), a fresh execute() call
    must reuse the checkpointed output instead of paying for another Phase 1 review."""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch('pipeline.pr_review_stage.PRReviewCheckpoint') as MockCheckpoint, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])), \
         patch.object(pr_review_stage, '_parse_consolidated_findings', return_value=[]), \
         patch.object(pr_review_stage, '_advance_parent_to_documentation', return_value=True):

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock(return_value={
            'agent_output': '### Critical Issues\nNone found (from phase 4)'
        })
        mock_get_executor.return_value = mock_executor
        mock_state.get_review_count.return_value = 0

        mock_checkpoint = MockCheckpoint.return_value

        def _cached(cycle, phase_key):
            if cycle == 1 and phase_key == 'code_review':
                return 'checkpointed phase 1 output from before the restart'
            return None

        mock_checkpoint.get_phase_output.side_effect = _cached

        context = {'context': {'issue_number': 42, 'project': 'test-project'}}
        await pr_review_stage.execute(context)

        # No call re-launched Phase 1's container...
        calls = [c[1] for c in mock_executor.execute_agent.call_args_list]
        phase1_calls = [c for c in calls if c['execution_type'] == 'pr_review_phase1']
        assert phase1_calls == []
        # ...and the checkpointed Phase 1 text was never re-saved (it was reused, not
        # redone) — Phase 4 legitimately still saves its own ('consolidation') output.
        saved_phases = [call.args[1] for call in mock_checkpoint.save_phase_output.call_args_list]
        assert 'code_review' not in saved_phases


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
         patch.object(pr_review_stage, '_advance_parent_to_documentation', return_value=True):

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
         patch.object(pr_review_stage, '_return_parent_to_development', return_value=True):

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
async def test_manual_progression_flag_not_set_when_move_fails(pr_review_stage):
    """When the parent issue can't be moved back to 'In Development', manual_progression_made
    must be False rather than blindly True -- the flag should reflect whether this stage
    actually handled progression, and a standalone failure warning must be posted since
    this isn't the final review cycle (so the cycle-limit comment path never fires)."""
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
         patch.object(pr_review_stage, '_return_parent_to_development', return_value=False), \
         patch.object(pr_review_stage, '_post_comment_on_issue') as mock_post_comment:

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock(return_value={
            'agent_output': '{"groups": [], "filtered_out": []}'
        })
        mock_get_executor.return_value = mock_executor
        # Not the final cycle, so the cycle-limit comment (which folds in its own
        # move_succeeded warning) never fires -- only the standalone failure path can.
        mock_state.get_review_count.return_value = 0

        context = {
            'context': {
                'issue_number': 42,
                'project': 'test-project'
            }
        }

        result = await pr_review_stage.execute(context)

        # The move failed, so this stage did NOT successfully handle progression.
        # Matches the codebase's existing convention (see
        # test_manual_progression_flag_not_set_when_inconclusive): the key is only
        # ever set to True, never to an explicit False, so "not made" means absent.
        assert not result.get('manual_progression_made')

        # The failure must still be surfaced as a comment, not just logged.
        mock_post_comment.assert_called_once()
        posted_comment = mock_post_comment.call_args[0][2]
        assert 'could not automatically move' in posted_comment.lower()


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
         patch.object(pr_review_stage, '_advance_parent_to_documentation', return_value=True):

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
async def test_clean_pass_advance_fails_not_manual_progression(pr_review_stage):
    """When advancing the parent issue to 'Done' fails on a clean pass,
    manual_progression_made must not be set, and a failure warning (with 'Done'
    as the target column) must be posted rather than silently logged."""
    with patch('pipeline.pr_review_stage.get_agent_executor') as mock_get_executor, \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
         patch.object(pr_review_stage, '_load_discussion_outputs', return_value={}), \
         patch.object(pr_review_stage, '_get_parent_issue_body', return_value=''), \
         patch.object(pr_review_stage, '_check_ci_status', return_value=([], [])), \
         patch.object(pr_review_stage, '_parse_consolidated_findings', return_value=[]), \
         patch.object(pr_review_stage, '_advance_parent_to_documentation', return_value=False), \
         patch.object(pr_review_stage, '_post_comment_on_issue') as mock_post_comment:

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

        assert not result.get('manual_progression_made')
        mock_post_comment.assert_called_once()
        posted_comment = mock_post_comment.call_args[0][2]
        assert "could not automatically move this issue to 'done'" in posted_comment.lower()


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
         patch.object(pr_review_stage, '_advance_parent_to_documentation', return_value=True):

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
         patch.object(pr_review_stage, '_return_parent_to_development', return_value=True) as mock_return, \
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
async def test_cycle_limit_comment_reflects_move_failure(pr_review_stage):
    """At the final review cycle, if the move back to 'In Development' also fails,
    the cycle-limit comment must use the failure wording ("remains in 'In Review'"),
    not the success wording that tells the operator to move it "to In Review" --
    which would be misleading since the issue never left In Review."""
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
         patch.object(pr_review_stage, '_return_parent_to_development', return_value=False) as mock_return, \
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

        mock_return.assert_called_once()
        assert not result.get('manual_progression_made')

        # Exactly one comment (the cycle-limit comment) -- not also a standalone
        # failure comment, since move_succeeded=False is already folded into it.
        mock_post_comment.assert_called_once()
        posted_comment = mock_post_comment.call_args[0][2]
        assert "remains in 'in review'" in posted_comment.lower()
        assert "manually move the parent issue to 'in review'" not in posted_comment.lower()


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
         patch.object(pr_review_stage, '_advance_parent_to_documentation', return_value=True):

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
async def test_already_merged_pr_advances_to_done(pr_review_stage):
    """No open PR, but a merged PR is found for this issue's branch -- the parent
    should advance to Done and the "already merged" explanation should be posted."""
    merged_pr = {'number': '77', 'url': 'https://github.com/o/r/pull/77',
                 'headRefName': 'feature/issue-42-thing'}

    with patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value=None), \
         patch.object(pr_review_stage, '_find_merged_pr', return_value=merged_pr), \
         patch.object(pr_review_stage, '_advance_parent_to_documentation', return_value=True), \
         patch.object(pr_review_stage, '_post_comment_on_issue') as mock_post_comment:

        mock_state.get_review_count.return_value = 0

        context = {'context': {'issue_number': 42, 'project': 'test-project'}}

        result = await pr_review_stage.execute(context)

        assert result.get('manual_progression_made') is True
        mock_post_comment.assert_called_once()
        posted_comment = mock_post_comment.call_args[0][2]
        assert 'was already merged' in posted_comment.lower()
        assert 'advancing to done' in posted_comment.lower()


@pytest.mark.asyncio
async def test_already_merged_pr_advance_fails_not_manual_progression(pr_review_stage):
    """Same as above, but the move to Done fails: manual_progression_made must not
    be set, and the posted comment must include BOTH the merge explanation (why
    Done is correct) and the failure warning (that the move didn't happen) --
    an either/or here would silently drop one of the two."""
    merged_pr = {'number': '77', 'url': 'https://github.com/o/r/pull/77',
                 'headRefName': 'feature/issue-42-thing'}

    with patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value=None), \
         patch.object(pr_review_stage, '_find_merged_pr', return_value=merged_pr), \
         patch.object(pr_review_stage, '_advance_parent_to_documentation', return_value=False), \
         patch.object(pr_review_stage, '_post_comment_on_issue', return_value=True) as mock_post_comment:

        mock_state.get_review_count.return_value = 0

        context = {'context': {'issue_number': 42, 'project': 'test-project'}}

        result = await pr_review_stage.execute(context)

        assert not result.get('manual_progression_made')
        mock_post_comment.assert_called_once()
        posted_comment = mock_post_comment.call_args[0][2]
        assert 'was already merged' in posted_comment.lower()
        assert "could not automatically move this issue to 'done'" in posted_comment.lower()


@pytest.mark.asyncio
async def test_already_merged_pr_advance_fails_and_comment_fails_escalates(pr_review_stage):
    """When the move to Done AND posting the failure warning both fail, the stage
    must raise rather than silently return -- otherwise the issue is stranded
    with zero human-visible signal anywhere."""
    from agents.non_retryable import NonRetryableAgentError

    merged_pr = {'number': '77', 'url': 'https://github.com/o/r/pull/77',
                 'headRefName': 'feature/issue-42-thing'}

    with patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch.object(pr_review_stage, '_find_pr_url', return_value=None), \
         patch.object(pr_review_stage, '_find_merged_pr', return_value=merged_pr), \
         patch.object(pr_review_stage, '_advance_parent_to_documentation', return_value=False), \
         patch.object(pr_review_stage, '_post_comment_on_issue', return_value=False):

        mock_state.get_review_count.return_value = 0

        context = {'context': {'issue_number': 42, 'project': 'test-project'}}

        with pytest.raises(NonRetryableAgentError, match="stranded"):
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
         patch.object(pr_review_stage, '_advance_parent_to_documentation', return_value=True):

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
         patch.object(pr_review_stage, '_return_parent_to_development', return_value=True):

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
         patch.object(pr_review_stage, '_return_parent_to_development', return_value=True) as mock_return, \
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
async def test_ci_failure_move_fails_not_manual_progression(pr_review_stage):
    """CI-failure branch analog of test_manual_progression_flag_not_set_when_move_fails:
    when the move back to 'In Development' fails, manual_progression_made must not be
    set, and a standalone failure warning must be posted (not the final cycle, so the
    cycle-limit comment path never fires)."""
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
         patch.object(pr_review_stage, '_return_parent_to_development', return_value=False) as mock_return, \
         patch.object(pr_review_stage, '_post_comment_on_issue') as mock_post_comment:

        mock_executor = AsyncMock()
        mock_executor.execute_agent = AsyncMock()
        mock_get_executor.return_value = mock_executor
        # Not the final cycle, so the cycle-limit comment (which folds in its own
        # move_succeeded warning) never fires -- only the standalone failure path can.
        mock_state.get_review_count.return_value = 0

        context = {'context': {'issue_number': 10, 'project': 'test-project'}}

        result = await pr_review_stage.execute(context)

        mock_return.assert_called_once()
        # The move failed, so this stage did NOT successfully handle progression.
        assert not result.get('manual_progression_made')
        # The failure must still be surfaced as a comment, not just logged.
        mock_post_comment.assert_called_once()
        posted_comment = mock_post_comment.call_args[0][2]
        assert 'could not automatically move' in posted_comment.lower()


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
         patch.object(pr_review_stage, '_return_parent_to_development', return_value=True), \
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
         patch.object(pr_review_stage, '_advance_parent_to_documentation', return_value=True):

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
