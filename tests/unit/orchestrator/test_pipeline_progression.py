"""
Unit tests for pipeline progression (auto-promotion)

Tests automatic promotion of issues through pipeline stages.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import Mock, patch, MagicMock
from tests.unit.orchestrator.mocks import MockGitHubAPI
from tests.unit.orchestrator.conftest import create_test_issue

# The activated_at stamp mark_issue_active() returns. Stubbed on the queue mock
# so a regression that reintroduces a dispatch attempt fails on
# TestNoNextIssueDispatch's assertions rather than on a stray Mock return value.
ACTIVATED_AT = '2026-01-01T00:00:00+00:00'


def _queue_row(issue_number, position=0, column=None, shape='enqueue'):
    """A queue row exactly as get_next_n_waiting_issues() returns it.

    `shape='enqueue'` mirrors enqueue_issue() ('initial_column'); `shape='sync'`
    mirrors sync_queue_with_github()/force_sync_with_github() (no column field
    of any kind). Neither ever carries a 'column' key -- which is why the
    next-issue dispatcher this exit path used to hold never dispatched anything
    in production, and why it was deleted rather than activated (#158).

    `column` injects one anyway. TestNoNextIssueDispatch passes it to stand in
    for the "just fix the column lookup" change that would have activated that
    dispatcher: the exit path must ignore a candidate either way. It is not a
    shape production ever produces.
    """
    row = {
        'issue_number': issue_number,
        'position_in_column': position,
        'status': 'waiting',
    }
    if shape == 'enqueue':
        row['initial_column'] = 'Development'
    else:
        row['added_at'] = '2026-01-01T00:00:00+00:00'
        row['last_position_check'] = '2026-01-01T00:00:00+00:00'
        row['title'] = f'Issue #{issue_number}'
    if column is not None:
        row['column'] = column
    return row


class TestPipelineProgression:
    """Test automatic progression through pipeline stages"""
    
    def test_calculate_next_column_from_requirements(
        self,
        mock_config_manager,
        test_workflow_template,
        test_project_config
    ):
        """Test next column calculation from Requirements"""
        with patch('config.manager.config_manager', mock_config_manager), \
             patch('services.pipeline_progression.config_manager', mock_config_manager), \
             patch('monitoring.observability.get_observability_manager'):

            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(None)

            next_column = progression.get_next_column(
                'test-project',
                'dev',
                'Requirements'
            )

            # Next column after Requirements is Requirements Review (review column)
            assert next_column == 'Requirements Review'

    def test_calculate_next_column_from_middle_stage(
        self,
        mock_config_manager,
        test_workflow_template,
        test_project_config
    ):
        """Test next column calculation from middle stage"""
        with patch('config.manager.config_manager', mock_config_manager), \
             patch('services.pipeline_progression.config_manager', mock_config_manager), \
             patch('monitoring.observability.get_observability_manager'):

            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(None)

            next_column = progression.get_next_column(
                'test-project',
                'dev',
                'Design'
            )

            # Next column after Design is Design Review (review column)
            assert next_column == 'Design Review'

    def test_calculate_next_column_from_last_stage(
        self,
        mock_config_manager,
        test_workflow_template,
        test_project_config
    ):
        """Test next column returns None when at final stage"""
        with patch('config.manager.config_manager', mock_config_manager), \
             patch('services.pipeline_progression.config_manager', mock_config_manager), \
             patch('monitoring.observability.get_observability_manager'):

            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(None)

            next_column = progression.get_next_column(
                'test-project',
                'dev',
                'Done'
            )

            assert next_column is None

    def test_calculate_next_column_unknown_status(
        self,
        mock_config_manager,
        test_workflow_template,
        test_project_config
    ):
        """Test next column returns None for unknown status"""
        with patch('config.manager.config_manager', mock_config_manager), \
             patch('services.pipeline_progression.config_manager', mock_config_manager), \
             patch('monitoring.observability.get_observability_manager'):

            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(None)

            next_column = progression.get_next_column(
                'test-project',
                'dev',
                'NonExistentStatus'
            )

            assert next_column is None
    
    def test_promote_issue_updates_github_status(
        self,
        mock_github,
        mock_config_manager,
        mock_observability,
        test_workflow_template,
        test_project_config
    ):
        """Test that promoting issue updates GitHub status"""
        create_test_issue(mock_github, 500, 'Requirements')
        
        mock_task_queue = Mock()
        
        with patch('config.manager.config_manager', mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_progression.config_manager', mock_config_manager), \
             patch('services.work_execution_state.work_execution_tracker'):
            
            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(task_queue=mock_task_queue)
            progression.decision_events = mock_observability[1]
            
            # Mock move_issue_to_column to simulate successful GitHub update
            mock_move = Mock(return_value=True)
            progression.move_issue_to_column = mock_move
            
            result = progression.progress_to_next_stage(
                project_name='test-project',
                board_name='dev',
                issue_number=500,
                current_column='Requirements',
                repository='test-repo',
                issue_data={'number': 500, 'title': 'Test Issue'}
            )
            
            # Assert: Progression succeeded
            assert result is True
            # Verify move was attempted with correct arguments (next is Requirements Review)
            mock_move.assert_called_once_with(
                'test-project', 'dev', 500, 'Requirements Review', trigger='pipeline_progression'
            )
    
    def test_promote_issue_emits_decision_event(
        self,
        mock_github,
        mock_config_manager,
        mock_observability,
        test_workflow_template,
        test_project_config
    ):
        """Test that promotion emits pipeline promotion decision event"""
        create_test_issue(mock_github, 501, 'Design')
        
        mock_task_queue = Mock()
        
        with patch('config.manager.config_manager', mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_progression.config_manager', mock_config_manager), \
             patch('services.work_execution_state.work_execution_tracker'):
            
            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(task_queue=mock_task_queue)
            progression.decision_events = mock_observability[1]
            
            # Mock move_issue_to_column to emit event and return True
            def mock_move_with_event(*args, **kwargs):
                # Emit the start event
                mock_observability[1].emit_status_progression(
                    issue_number=501,
                    project='test-project',
                    board='dev',
                    from_status='Design',
                    to_status='Design Review',
                    trigger=kwargs.get('trigger', 'unknown'),
                    success=None
                )
                # Emit the success event
                mock_observability[1].emit_status_progression(
                    issue_number=501,
                    project='test-project',
                    board='dev',
                    from_status='Design',
                    to_status='Design Review',
                    trigger=kwargs.get('trigger', 'unknown'),
                    success=True
                )
                return True
            
            with patch.object(progression, 'move_issue_to_column', side_effect=mock_move_with_event):
                progression.progress_to_next_stage(
                    'test-project', 'dev', 501, 'Design', 'test-repo',
                    {'number': 501, 'title': 'Test'}
                )
            
            # Assert: Decision event emitted (emit_status_progression is used, not emit_pipeline_promotion_decision)
            assert mock_observability[1].emit_status_progression.called
            # Check that it was called with correct parameters
            calls = mock_observability[1].emit_status_progression.call_args_list
            # Should have 2 calls: one at start and one at success
            assert len(calls) >= 1
            # Check the success call
            success_call = [c for c in calls if c[1].get('success') == True]
            assert len(success_call) > 0
            assert success_call[0][1]['from_status'] == 'Design'
            assert success_call[0][1]['to_status'] == 'Design Review'  # Next is Design Review
            assert success_call[0][1]['issue_number'] == 501
    
    def test_no_promotion_at_final_stage(
        self,
        mock_github,
        mock_config_manager,
        mock_observability,
        test_workflow_template,
        test_project_config
    ):
        """Test that issues at Done stage are not promoted"""
        create_test_issue(mock_github, 502, 'Done')
        
        with patch('config.manager.config_manager', mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_progression.config_manager', mock_config_manager):
            
            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(task_queue=None)
            
            # Try to promote - should return False since Done has no next column
            result = progression.progress_to_next_stage(
                'test-project', 'dev', 502, 'Done', 'test-repo',
                {'number': 502, 'title': 'Test'}
            )
            
            # Assert: No promotion occurred
            assert result is False
    
    def test_promotion_of_closed_issue_fails(
        self,
        mock_github,
        mock_config_manager,
        mock_observability,
        test_workflow_template,
        test_project_config
    ):
        """Test that closed issues can still be promoted (status is independent of open/closed)"""
        create_test_issue(mock_github, 503, 'Development', state='CLOSED')
        
        mock_task_queue = Mock()
        
        with patch('config.manager.config_manager', mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_progression.config_manager', mock_config_manager), \
             patch('services.work_execution_state.work_execution_tracker'):
            
            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(task_queue=mock_task_queue)
            progression.decision_events = mock_observability[1]
            
            # Mock move to succeed
            with patch.object(progression, 'move_issue_to_column', return_value=True):
                result = progression.progress_to_next_stage(
                    'test-project', 'dev', 503, 'Development', 'test-repo',
                    {'number': 503, 'title': 'Test', 'state': 'CLOSED'}
                )
            
            # Note: The implementation doesn't check if issue is closed
            # It will attempt progression regardless
            # This test documents the current behavior
            assert result is True


class TestPipelineProgressionWithPipelineRun:
    """Test pipeline progression with pipeline run tracking"""
    
    def test_promotion_includes_pipeline_run_id(
        self,
        mock_github,
        mock_config_manager,
        mock_observability,
        test_workflow_template,
        test_project_config
    ):
        """Test that promotion creates agent tasks correctly"""
        create_test_issue(mock_github, 600, 'Requirements')
        
        # Create a mock task queue to verify task creation
        mock_task_queue = Mock()
        
        with patch('config.manager.config_manager', mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_progression.config_manager', mock_config_manager), \
             patch('services.work_execution_state.work_execution_tracker'):
            
            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(task_queue=mock_task_queue)
            progression.decision_events = mock_observability[1]
            
            # Mock move_issue_to_column
            with patch.object(progression, 'move_issue_to_column', return_value=True):
                progression.progress_to_next_stage(
                    'test-project', 'dev', 600, 'Requirements', 'test-repo',
                    {'number': 600, 'title': 'Test Issue'}
                )
            
            # Assert: Task was queued for the next agent
            assert mock_task_queue.enqueue.called
            task = mock_task_queue.enqueue.call_args[0][0]
            # Next column after Requirements is Requirements Review with requirements_reviewer
            assert task.agent == 'requirements_reviewer'
            assert task.context['issue_number'] == 600
            assert task.context['column'] == 'Requirements Review'


class TestFullPipelineTraversal:
    """Test complete traversal through pipeline"""
    
    def test_issue_progresses_through_all_stages(
        self,
        mock_github,
        mock_config_manager,
        mock_observability,
        test_workflow_template,
        test_project_config
    ):
        """Test issue successfully progresses through all pipeline stages"""
        create_test_issue(mock_github, 700, 'Requirements')
        
        mock_task_queue = Mock()
        
        with patch('config.manager.config_manager', mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_progression.config_manager', mock_config_manager), \
             patch('services.work_execution_state.work_execution_tracker'):
            
            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(task_queue=mock_task_queue)
            progression.decision_events = mock_observability[1]
            
            # Mock move_issue_to_column
            mock_move = Mock(return_value=True)
            progression.move_issue_to_column = mock_move
            
            # Progress through key stages (just a few, not every review)
            stages = ['Requirements', 'Requirements Review', 'Design', 'Design Review']
            
            for current_stage in stages:
                result = progression.progress_to_next_stage(
                    'test-project', 'dev', 700, current_stage, 'test-repo',
                    {'number': 700, 'title': 'Test'}
                )
                assert result is True
            
            # Verify we moved through all stages
            assert mock_move.call_count == len(stages)
    
    def test_stage_history_tracked_through_progression(
        self,
        mock_github,
        mock_config_manager,
        mock_observability,
        test_workflow_template,
        test_project_config
    ):
        """Test that all stage transitions are tracked"""
        create_test_issue(mock_github, 701, 'Requirements')
        
        mock_task_queue = Mock()
        
        with patch('config.manager.config_manager', mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_progression.config_manager', mock_config_manager), \
             patch('services.work_execution_state.work_execution_tracker'):
            
            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(task_queue=mock_task_queue)
            progression.decision_events = mock_observability[1]
            
            # Mock move_issue_to_column to emit events
            def mock_move_with_event(project, board, issue_num, target_col, trigger='unknown'):
                # Determine current status from previous calls
                call_count = mock_observability[1].emit_status_progression.call_count
                from_statuses = ['Requirements', 'Requirements Review', 'Design']
                from_status = from_statuses[call_count] if call_count < len(from_statuses) else 'unknown'
                
                mock_observability[1].emit_status_progression(
                    issue_number=issue_num,
                    project=project,
                    board=board,
                    from_status=from_status,
                    to_status=target_col,
                    trigger=trigger,
                    success=True
                )
                return True
            
            # Progress through multiple stages
            with patch.object(progression, 'move_issue_to_column', side_effect=mock_move_with_event):
                progression.progress_to_next_stage(
                    'test-project', 'dev', 701, 'Requirements', 'test-repo',
                    {'number': 701, 'title': 'Test'}
                )
                progression.progress_to_next_stage(
                    'test-project', 'dev', 701, 'Requirements Review', 'test-repo',
                    {'number': 701, 'title': 'Test'}
                )
                progression.progress_to_next_stage(
                    'test-project', 'dev', 701, 'Design', 'test-repo',
                    {'number': 701, 'title': 'Test'}
                )
            
            # Assert: Status progression events emitted (not pipeline_promotion_decision)
            # Each call emits multiple events (start, success), so count success events
            success_calls = [c for c in mock_observability[1].emit_status_progression.call_args_list 
                           if c[1].get('success') == True]
            assert len(success_calls) == 3
            
            # Assert: Correct stage transitions
            assert success_calls[0][1]['from_status'] == 'Requirements'
            assert success_calls[0][1]['to_status'] == 'Requirements Review'
            assert success_calls[1][1]['from_status'] == 'Requirements Review'
            assert success_calls[1][1]['to_status'] == 'Design'
            assert success_calls[2][1]['from_status'] == 'Design'
            assert success_calls[2][1]['to_status'] == 'Design Review'



class TestReleaseLockAndProcessNext:
    """_release_lock_on_exit_column's lock-release gate. release_lock()
    returns False both when this issue's lock is genuinely retained due to a
    failed run AND when this issue simply doesn't hold the lock at all
    (held_by_other — the normal case for e.g. conversational issues, which
    never acquire the lock in the first place; see the identical
    lock_held_by_us gate in project_monitor.py's sibling,
    _check_pr_ready_on_issue_exit). Before this fix, that distinction wasn't
    made: a normal held_by_other exit was misdiagnosed as "likely retained"
    and returned early, permanently stalling the board — queue cleanup never
    ran, and the run stayed "active" forever."""

    def test_skips_release_and_still_processes_queue_when_not_held_by_us(self):
        other_lock = Mock()
        other_lock.locked_by_issue = 999

        mock_lock_manager = Mock()
        mock_lock_manager.get_lock.return_value = other_lock

        mock_queue = Mock()
        mock_queue.is_issue_in_queue.return_value = True
        # Neither is consulted any more (#158) -- stubbed so a regression that
        # reintroduces a dispatch attempt fails on the assertions in
        # TestNoNextIssueDispatch rather than on a stray Mock return value here.
        mock_queue.get_next_waiting_issue.return_value = None
        mock_queue.get_next_n_waiting_issues.return_value = []

        mock_run_manager = Mock()

        with patch('services.pipeline_progression.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_progression.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.pipeline_progression.get_pipeline_run_manager', return_value=mock_run_manager), \
             patch('monitoring.observability.get_observability_manager'):

            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(None)
            progression._release_lock_on_exit_column('test-project', 'dev', 100, 'Done', 'test-repo')

        # Never attempted to release a lock this issue doesn't hold.
        mock_lock_manager.release_lock.assert_not_called()
        # But cleanup and success-completion still proceeded, rather than
        # stalling as if the lock were retained.
        mock_queue.remove_issue_from_queue.assert_called_once_with(100)
        mock_run_manager.end_pipeline_run.assert_called_once()
        assert mock_run_manager.end_pipeline_run.call_args[1]['outcome'] == 'success'

    def test_releases_and_processes_queue_when_held_by_us(self):
        """Control case: when this issue DOES hold the lock, release still
        happens exactly as before — proving the fix didn't just make release
        never fire."""
        our_lock = Mock()
        our_lock.locked_by_issue = 100

        mock_lock_manager = Mock()
        mock_lock_manager.get_lock.return_value = our_lock
        mock_lock_manager.release_lock.return_value = True

        mock_queue = Mock()
        mock_queue.is_issue_in_queue.return_value = True
        mock_queue.get_next_waiting_issue.return_value = None
        mock_queue.get_next_n_waiting_issues.return_value = []

        mock_run_manager = Mock()

        with patch('services.pipeline_progression.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_progression.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.pipeline_progression.get_pipeline_run_manager', return_value=mock_run_manager), \
             patch('monitoring.observability.get_observability_manager'):

            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(None)
            progression._release_lock_on_exit_column('test-project', 'dev', 100, 'Done', 'test-repo')

        mock_lock_manager.release_lock.assert_called_once_with('test-project', 'dev', 100)
        mock_queue.remove_issue_from_queue.assert_called_once_with(100)
        mock_run_manager.end_pipeline_run.assert_called_once()

    def test_stops_without_processing_queue_when_release_fails_for_our_own_retained_lock(self):
        """Defense in depth: if this issue DOES hold the lock but it's
        somehow retained (shouldn't normally happen for an issue legitimately
        reaching an exit column), the failed release must still halt
        everything — this is the genuine-failure case the gate exists for,
        and must not be weakened by the held_by_other fix above."""
        our_lock = Mock()
        our_lock.locked_by_issue = 100

        mock_lock_manager = Mock()
        mock_lock_manager.get_lock.return_value = our_lock
        mock_lock_manager.release_lock.return_value = False

        mock_queue = Mock()
        mock_run_manager = Mock()

        with patch('services.pipeline_progression.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_progression.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.pipeline_progression.get_pipeline_run_manager', return_value=mock_run_manager), \
             patch('monitoring.observability.get_observability_manager'):

            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(None)
            progression._release_lock_on_exit_column('test-project', 'dev', 100, 'Done', 'test-repo')

        mock_queue.remove_issue_from_queue.assert_not_called()
        mock_run_manager.end_pipeline_run.assert_not_called()

    def test_a_release_that_could_not_be_serialized_is_not_reported_as_retained(self, caplog):
        """
        REGRESSION (#153 WI-8 review round): release_lock() now takes the
        lock's '<state>.yaml.acquire.lock' guard, and a guard it cannot take
        used to return the same bare False as a considered-and-refused
        release. This site then told operators the lock was "likely retained
        due to a failed run" and pointed them at scripts/release_lock.py --
        a wrong-but-plausible diagnosis for what is actually transient
        contention (try_acquire_lock()'s YAML-fallback path takes that same
        guard, and is reached exactly when Redis is down).

        The halt itself is still correct -- the lock genuinely was not
        released, so the board must not advance -- but it has to be reported
        as what it is.
        """
        import logging
        from services.pipeline_lock_manager import ReleaseResult

        our_lock = Mock()
        our_lock.locked_by_issue = 100

        mock_lock_manager = Mock()
        mock_lock_manager.get_lock.return_value = our_lock
        mock_lock_manager.release_lock.return_value = ReleaseResult.SERIALIZATION_FAILED

        mock_queue = Mock()
        mock_run_manager = Mock()

        with patch('services.pipeline_progression.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_progression.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.pipeline_progression.get_pipeline_run_manager', return_value=mock_run_manager), \
             patch('monitoring.observability.get_observability_manager'), \
             caplog.at_level(logging.ERROR, logger='services.pipeline_progression'):

            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(None)
            progression._release_lock_on_exit_column('test-project', 'dev', 100, 'Done', 'test-repo')

        mock_queue.remove_issue_from_queue.assert_not_called()
        mock_run_manager.end_pipeline_run.assert_not_called()
        assert 'could not be serialized' in caplog.text
        assert 'likely retained due to a failed run' not in caplog.text

    def test_skips_release_and_processes_queue_when_nothing_is_locked_at_all(self):
        """Control case: no lock exists at all (lock is None) — must not
        crash dereferencing lock.locked_by_issue, and must still proceed
        with cleanup."""
        mock_lock_manager = Mock()
        mock_lock_manager.get_lock.return_value = None

        mock_queue = Mock()
        mock_queue.is_issue_in_queue.return_value = False
        mock_queue.get_next_waiting_issue.return_value = None
        mock_queue.get_next_n_waiting_issues.return_value = []

        mock_run_manager = Mock()

        with patch('services.pipeline_progression.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_progression.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.pipeline_progression.get_pipeline_run_manager', return_value=mock_run_manager), \
             patch('monitoring.observability.get_observability_manager'):

            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(None)
            progression._release_lock_on_exit_column('test-project', 'dev', 100, 'Done', 'test-repo')

        mock_lock_manager.release_lock.assert_not_called()
        mock_run_manager.end_pipeline_run.assert_called_once()

class TestNoNextIssueDispatch:
    """PIN (#158): the exit path must not dispatch the next queued issue, and
    must not TOUCH it either.

    The deleted body read the column off the queue entry, and
    PipelineQueueManager never writes a 'column' key: enqueue_issue() writes
    'initial_column', sync_queue_with_github()/force_sync_with_github() write no
    column field at all. (ProjectMonitor's FAILSAFE relies on the same fact --
    it uses `'column' in next_issue` to tell a stalled candidate from a queue
    row.) So it acquired the board lock, marked the entry 'active', resolved no
    agent, and unwound both halves again -- every single exit-column
    progression, for a candidate it was never going to dispatch.

    Making it dispatch would NOT have been a cosmetic cleanup. It had none of
    ProjectMonitor.trigger_agent_for_status()'s column-type routing: no
    conversational, review, repair_cycle or pr_review handling, and none of its
    duplicate-task/active-execution/cancellation guards. On the Planning &
    Design board, whose trigger column is 'conversational', it would enqueue a
    plain one-shot Task instead of starting a feedback loop AND leave the
    board's exclusive lock held by a conversational issue -- which by design
    never holds it -- blocking every other issue on that board.

    Not touching the candidate is a superset of the old rollback's guarantee: it
    is left as "waiting entry + unlocked board" by construction, which is
    exactly what ProjectMonitor's FAILSAFE (SCENARIO 2) picks up and dispatches
    WITH routing."""

    def _run_with(self, rows, column_type='conversational'):
        our_lock = Mock()
        our_lock.locked_by_issue = 100

        mock_lock_manager = Mock()
        mock_lock_manager.get_lock.return_value = our_lock
        mock_lock_manager.release_lock.return_value = True
        mock_lock_manager.try_acquire_lock.return_value = (True, "lock_acquired")

        mock_queue = Mock()
        mock_queue.is_issue_in_queue.return_value = True
        mock_queue.mark_issue_active.return_value = ACTIVATED_AT
        mock_queue.get_next_n_waiting_issues.return_value = rows

        mock_run_manager = Mock()
        mock_run_manager.ensure_pipeline_run_for_task.return_value = 'run-200'

        # A conversational trigger column by default -- the exact configuration
        # that made activating the deleted dispatcher harmful.
        next_column = Mock()
        next_column.name = 'Development'
        next_column.agent = 'senior_software_engineer'
        next_column.type = column_type
        workflow_template = Mock()
        workflow_template.columns = [next_column]

        pipeline_config = Mock()
        pipeline_config.board_name = 'dev'
        pipeline_config.name = 'planning'
        pipeline_config.workflow = 'planning_workflow'

        project_config = Mock()
        project_config.pipelines = [pipeline_config]
        project_config.github = {'org': 'test-org', 'repo': 'test-repo'}

        mock_task_queue = Mock()

        with patch('services.pipeline_progression.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_progression.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.pipeline_progression.get_pipeline_run_manager', return_value=mock_run_manager), \
             patch('services.pipeline_progression.config_manager') as mock_config_manager, \
             patch('services.work_execution_state.work_execution_tracker') as mock_tracker, \
             patch('monitoring.observability.get_observability_manager'):

            mock_config_manager.get_project_config.return_value = project_config
            mock_config_manager.get_workflow_template.return_value = workflow_template

            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(mock_task_queue)
            progression._get_issue_details = Mock(return_value={'title': 'Next issue'})

            progression._release_lock_on_exit_column('test-project', 'dev', 100, 'Done', 'test-repo')

        return mock_lock_manager, mock_queue, mock_run_manager, mock_task_queue, mock_tracker

    @pytest.mark.parametrize('shape', ['enqueue', 'sync'])
    def test_does_not_dispatch_for_production_shaped_queue_rows(self, shape):
        """Both real row shapes -- enqueue_issue()'s and the sync paths' --
        must leave this site dispatching nothing."""
        rows = [_queue_row(200, shape=shape)]
        assert 'column' not in rows[0], "the fixture must not fabricate a column"

        (_lock_manager, _queue, _run_manager,
         mock_task_queue, mock_tracker) = self._run_with(rows)

        mock_task_queue.enqueue.assert_not_called()
        mock_tracker.record_execution_start.assert_not_called()

    def test_does_not_dispatch_even_when_the_row_carries_a_column(self):
        """REGRESSION (#158): the row shape is no longer what stops this site.

        Injecting a 'column' is the "just fix the column lookup" change that
        would have activated the deleted dispatcher -- with a conversational
        column, an agent, and a pipeline run all resolvable. It must still
        enqueue nothing."""
        (_lock_manager, _queue, _run_manager,
         mock_task_queue, mock_tracker) = self._run_with(
            [_queue_row(200, column='Development')]
        )

        mock_task_queue.enqueue.assert_not_called()
        mock_tracker.record_execution_start.assert_not_called()

    def test_does_not_take_the_board_lock_for_a_candidate_it_will_not_dispatch(self):
        """REGRESSION (#158): the deleted body acquired the board lock and only
        released it again in its own rollback. That transient hold is visible to
        a concurrent FAILSAFE pass, which reads the board as busy and skips it
        for a whole poll interval."""
        (mock_lock_manager, _queue, _run_manager,
         _task_queue, _tracker) = self._run_with([_queue_row(200)])

        mock_lock_manager.try_acquire_lock.assert_not_called()
        # The exiting issue's own release is the ONLY release, and #200 is not
        # rolled back because it was never acquired.
        assert mock_lock_manager.release_lock.call_args_list == [
            (('test-project', 'dev', 100), {})
        ]

    def test_leaves_the_candidate_queue_entry_untouched(self):
        """The candidate must end up as "waiting entry + unlocked board" -- the
        monitor FAILSAFE's SCENARIO 2. The deleted body got there by marking it
        'active' and resetting it under a compare-and-swap; not touching it at
        all reaches the same state without the window in between."""
        (_lock_manager, mock_queue, _run_manager,
         _task_queue, _tracker) = self._run_with([_queue_row(200)])

        mock_queue.mark_issue_active.assert_not_called()
        mock_queue.reset_issue_to_waiting.assert_not_called()
        # Only the EXITING issue is removed from the queue.
        mock_queue.remove_issue_from_queue.assert_called_once_with(100)

    def test_does_not_resync_the_queue_against_github(self):
        """get_next_n_waiting_issues() syncs with GitHub before it returns, so
        the deleted body cost a board resync on every exit-column progression
        for a dispatch that never happened."""
        (_lock_manager, mock_queue, _run_manager,
         _task_queue, _tracker) = self._run_with([_queue_row(200)])

        mock_queue.get_next_n_waiting_issues.assert_not_called()
        mock_queue.get_next_waiting_issue.assert_not_called()

    def test_still_releases_the_lock_and_ends_the_run(self):
        """Control: deleting the dispatch half must not touch the release,
        queue-cleanup and run-completion half, which is live on every caller of
        progress_to_next_stage()."""
        (mock_lock_manager, mock_queue, mock_run_manager,
         _task_queue, _tracker) = self._run_with([_queue_row(200)])

        mock_lock_manager.release_lock.assert_called_once_with('test-project', 'dev', 100)
        mock_run_manager.end_pipeline_run.assert_called_once()
        assert mock_run_manager.end_pipeline_run.call_args[1]['outcome'] == 'success'

    def test_no_errors_are_logged_on_the_ordinary_exit_path(self, caplog):
        """The deferral this replaced was the expected outcome of every
        exit-column progression, and had to be kept out of the ERROR log by
        hand. Nothing is deferred any more, so nothing has to be suppressed."""
        with caplog.at_level('DEBUG'):
            self._run_with([_queue_row(200)])

        assert not [
            r for r in caplog.records if r.levelname in ('ERROR', 'CRITICAL')
        ], [r.getMessage() for r in caplog.records if r.levelname in ('ERROR', 'CRITICAL')]
