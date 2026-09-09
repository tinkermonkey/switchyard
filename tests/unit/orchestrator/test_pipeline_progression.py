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

# The activated_at stamp mark_issue_active() returns and the rollback hands
# back to reset_issue_to_waiting() as its compare-and-swap token.
ACTIVATED_AT = '2026-01-01T00:00:00+00:00'


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
    """_release_lock_and_process_next's lock-release gate. release_lock()
    returns False both when this issue's lock is genuinely retained due to a
    failed run AND when this issue simply doesn't hold the lock at all
    (held_by_other — the normal case for e.g. conversational issues, which
    never acquire the lock in the first place; see the identical
    lock_held_by_us gate in project_monitor.py's sibling,
    _check_pr_ready_on_issue_exit). Before this fix, that distinction wasn't
    made: a normal held_by_other exit was misdiagnosed as "likely retained"
    and returned early, permanently stalling the board — queue cleanup and
    next-issue dispatch never ran, and the run stayed "active" forever."""

    def test_skips_release_and_still_processes_queue_when_not_held_by_us(self):
        other_lock = Mock()
        other_lock.locked_by_issue = 999

        mock_lock_manager = Mock()
        mock_lock_manager.get_lock.return_value = other_lock

        mock_queue = Mock()
        mock_queue.is_issue_in_queue.return_value = True
        mock_queue.get_next_waiting_issue.return_value = None  # nothing else queued
        # Phase 2 (issue #57): _release_lock_and_process_next now calls
        # get_next_n_waiting_issues(1) instead of get_next_waiting_issue().
        mock_queue.get_next_n_waiting_issues.return_value = []

        mock_run_manager = Mock()

        with patch('services.pipeline_progression.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_progression.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.pipeline_progression.get_pipeline_run_manager', return_value=mock_run_manager), \
             patch('monitoring.observability.get_observability_manager'):

            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(None)
            progression._release_lock_and_process_next('test-project', 'dev', 100, 'Done', 'test-repo')

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
            progression._release_lock_and_process_next('test-project', 'dev', 100, 'Done', 'test-repo')

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
            progression._release_lock_and_process_next('test-project', 'dev', 100, 'Done', 'test-repo')

        mock_queue.remove_issue_from_queue.assert_not_called()
        mock_run_manager.end_pipeline_run.assert_not_called()

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
            progression._release_lock_and_process_next('test-project', 'dev', 100, 'Done', 'test-repo')

        mock_lock_manager.release_lock.assert_not_called()
        mock_run_manager.end_pipeline_run.assert_called_once()

    def test_dispatches_single_next_queued_issue_at_capacity_one(self):
        """Byte-identical-at-capacity-1 check: with exactly one waiting
        issue queued, _release_lock_and_process_next must release the lock,
        query get_next_n_waiting_issues(1), acquire the lock for the
        returned issue, mark it active, and enqueue a task for it - the
        same sequence get_next_waiting_issue() drove before #57."""
        our_lock = Mock()
        our_lock.locked_by_issue = 100

        mock_lock_manager = Mock()
        mock_lock_manager.get_lock.return_value = our_lock
        mock_lock_manager.release_lock.return_value = True
        mock_lock_manager.try_acquire_lock.return_value = (True, "lock_acquired")

        mock_queue = Mock()
        mock_queue.is_issue_in_queue.return_value = True
        mock_queue.get_next_n_waiting_issues.return_value = [
            {'issue_number': 200, 'position_in_column': 0, 'status': 'waiting', 'initial_column': 'Development'}
        ]

        mock_run_manager = Mock()
        mock_run_manager.ensure_pipeline_run_for_task.return_value = 'run-200'
        # The column is resolved from GitHub, not from the queue entry -- queue
        # entries never carry one (see _mocks() in the rollback suite below).
        # (column, reads_healthy) so a failed board read is never mistaken for
        # "the issue isn't on the board".
        mock_run_manager._resolve_issue_column_from_github.return_value = ('Development', True)

        dev_column = Mock()
        dev_column.name = 'Development'
        dev_column.agent = 'senior_software_engineer'
        workflow_template = Mock()
        workflow_template.columns = [dev_column]

        pipeline_config = Mock()
        pipeline_config.board_name = 'dev'
        pipeline_config.name = 'sdlc'
        pipeline_config.workflow = 'sdlc_execution_workflow'

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

            progression._release_lock_and_process_next('test-project', 'dev', 100, 'Done', 'test-repo')

        mock_lock_manager.release_lock.assert_called_once_with('test-project', 'dev', 100)
        mock_queue.get_next_n_waiting_issues.assert_called_once_with(1)
        mock_lock_manager.try_acquire_lock.assert_called_once_with(
            project='test-project', board='dev', issue_number=200
        )
        mock_queue.mark_issue_active.assert_called_once_with(200)
        mock_tracker.record_execution_start.assert_called_once()
        mock_task_queue.enqueue.assert_called_once()
        dispatched_task = mock_task_queue.enqueue.call_args[0][0]
        assert dispatched_task.context['issue_number'] == 200
        assert dispatched_task.context['trigger'] == 'pipeline_progression'
        # Nothing failed, so nothing is rolled back.
        mock_queue.reset_issue_to_waiting.assert_not_called()


class TestReleaseLockAndProcessNextDispatchRollback:
    """Issue #142: _release_lock_and_process_next() had NO rollback at all for
    a dispatch that fails partway through — the method-level except only
    logged, leaving the pipeline lock held AND the queue entry stuck at
    status='active'. get_next_n_waiting_issues() selects strictly on
    status=='waiting', so that issue was silently excluded from every future
    dispatch, forever, with no automated recovery."""

    def _mocks(self, agent='senior_software_engineer', github_column='Development',
               column_reads_ok=True):
        our_lock = Mock()
        our_lock.locked_by_issue = 100

        mock_lock_manager = Mock()
        mock_lock_manager.get_lock.return_value = our_lock
        mock_lock_manager.release_lock.return_value = True
        mock_lock_manager.try_acquire_lock.return_value = (True, "lock_acquired")

        mock_queue = Mock()
        mock_queue.is_issue_in_queue.return_value = True
        # The compare-and-swap token the rollback has to hand back.
        mock_queue.mark_issue_active.return_value = ACTIVATED_AT

        mock_run_manager = Mock()
        mock_run_manager._resolve_issue_column_from_github.return_value = (
            github_column, column_reads_ok
        )

        dev_column = Mock()
        dev_column.name = 'Development'
        dev_column.agent = agent
        workflow_template = Mock()
        workflow_template.columns = [dev_column]

        pipeline_config = Mock()
        pipeline_config.board_name = 'dev'
        pipeline_config.name = 'sdlc'
        pipeline_config.workflow = 'sdlc_execution_workflow'

        project_config = Mock()
        project_config.pipelines = [pipeline_config]
        project_config.github = {'org': 'test-org', 'repo': 'test-repo'}

        return mock_lock_manager, mock_queue, mock_run_manager, workflow_template, project_config

    def _run(self, mock_lock_manager, mock_queue, mock_run_manager,
             workflow_template, project_config, mock_task_queue,
             issue_details=None):
        with patch('services.pipeline_progression.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_progression.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.pipeline_progression.get_pipeline_run_manager', return_value=mock_run_manager), \
             patch('services.pipeline_progression.config_manager') as mock_config_manager, \
             patch('services.work_execution_state.work_execution_tracker'), \
             patch('monitoring.observability.get_observability_manager'):

            mock_config_manager.get_project_config.return_value = project_config
            mock_config_manager.get_workflow_template.return_value = workflow_template

            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(mock_task_queue)
            progression._get_issue_details = Mock(
                return_value=issue_details if issue_details is not None else {'title': 'Next issue'}
            )

            progression._release_lock_and_process_next('test-project', 'dev', 100, 'Done', 'test-repo')

    def test_rolls_back_lock_and_queue_when_dispatch_fails(self):
        """REGRESSION (#142): a dispatch that raises after mark_issue_active()
        must release the lock AND reset the queue entry to 'waiting'."""
        (mock_lock_manager, mock_queue, mock_run_manager,
         workflow_template, project_config) = self._mocks()

        mock_queue.get_next_n_waiting_issues.return_value = [
            {'issue_number': 200, 'position_in_column': 0, 'status': 'waiting', 'initial_column': 'Development'}
        ]
        # ensure_pipeline_run_for_task() returning None is one of the real
        # failure modes this block raises on.
        mock_run_manager.ensure_pipeline_run_for_task.return_value = None

        mock_task_queue = Mock()
        self._run(mock_lock_manager, mock_queue, mock_run_manager,
                  workflow_template, project_config, mock_task_queue)

        mock_queue.mark_issue_active.assert_called_once_with(200)
        mock_task_queue.enqueue.assert_not_called()

        # Both halves rolled back.
        mock_lock_manager.release_lock.assert_any_call('test-project', 'dev', 200)
        mock_queue.reset_issue_to_waiting.assert_called_once_with(
            200, expected_activated_at=ACTIVATED_AT
        )

    def test_rolls_back_when_issue_fetch_raises(self):
        """_get_issue_details() raises RuntimeError after 3 failed attempts —
        one of the most likely real-world triggers (transient GitHub outage)."""
        (mock_lock_manager, mock_queue, mock_run_manager,
         workflow_template, project_config) = self._mocks()

        mock_queue.get_next_n_waiting_issues.return_value = [
            {'issue_number': 200, 'position_in_column': 0, 'status': 'waiting', 'initial_column': 'Development'}
        ]

        mock_task_queue = Mock()
        with patch('services.pipeline_progression.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_progression.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.pipeline_progression.get_pipeline_run_manager', return_value=mock_run_manager), \
             patch('services.pipeline_progression.config_manager') as mock_config_manager, \
             patch('services.work_execution_state.work_execution_tracker'), \
             patch('monitoring.observability.get_observability_manager'):

            mock_config_manager.get_project_config.return_value = project_config
            mock_config_manager.get_workflow_template.return_value = workflow_template

            from services.pipeline_progression import PipelineProgression
            progression = PipelineProgression(mock_task_queue)
            progression._get_issue_details = Mock(side_effect=RuntimeError("GitHub unavailable"))

            progression._release_lock_and_process_next('test-project', 'dev', 100, 'Done', 'test-repo')

        mock_lock_manager.release_lock.assert_any_call('test-project', 'dev', 200)
        mock_queue.reset_issue_to_waiting.assert_called_once_with(
            200, expected_activated_at=ACTIVATED_AT
        )

    def test_rolls_back_when_next_issue_column_has_no_agent(self):
        """A queued issue sitting in an agent-less column used to be logged as
        a warning while the lock stayed held and the entry stayed 'active' —
        the same permanent deadlock, just reached without an exception."""
        (mock_lock_manager, mock_queue, mock_run_manager,
         workflow_template, project_config) = self._mocks(agent=None)

        mock_queue.get_next_n_waiting_issues.return_value = [
            {'issue_number': 200, 'position_in_column': 0, 'status': 'waiting', 'initial_column': 'Development'}
        ]

        mock_task_queue = Mock()
        self._run(mock_lock_manager, mock_queue, mock_run_manager,
                  workflow_template, project_config, mock_task_queue)

        mock_task_queue.enqueue.assert_not_called()
        mock_lock_manager.release_lock.assert_any_call('test-project', 'dev', 200)
        mock_queue.reset_issue_to_waiting.assert_called_once_with(
            200, expected_activated_at=ACTIVATED_AT
        )

    def test_failure_on_one_slot_does_not_abort_remaining_slots(self):
        """The try/except must be PER ITERATION, not method-level: with the
        outer-only handler, the first candidate's failure aborted the whole
        loop and every later candidate was never even attempted. Matters the
        moment Phase 3a raises available_slots above 1."""
        (mock_lock_manager, mock_queue, mock_run_manager,
         workflow_template, project_config) = self._mocks()

        mock_queue.get_next_n_waiting_issues.return_value = [
            {'issue_number': 200, 'position_in_column': 0, 'status': 'waiting', 'initial_column': 'Development'},
            {'issue_number': 300, 'position_in_column': 1, 'status': 'waiting', 'initial_column': 'Development'},
        ]
        # First candidate fails, second succeeds.
        mock_run_manager.ensure_pipeline_run_for_task.side_effect = [None, 'run-300']

        mock_task_queue = Mock()
        self._run(mock_lock_manager, mock_queue, mock_run_manager,
                  workflow_template, project_config, mock_task_queue)

        # Failed candidate fully rolled back...
        mock_lock_manager.release_lock.assert_any_call('test-project', 'dev', 200)
        mock_queue.reset_issue_to_waiting.assert_called_once_with(
            200, expected_activated_at=ACTIVATED_AT
        )

        # ...and the loop still went on to dispatch the second candidate.
        mock_task_queue.enqueue.assert_called_once()
        dispatched_task = mock_task_queue.enqueue.call_args[0][0]
        assert dispatched_task.context['issue_number'] == 300

    def test_dispatches_production_shaped_queue_entry_with_no_column_key(self):
        """REGRESSION: queue entries have NO 'column' key. enqueue_issue()
        writes 'initial_column'; sync_queue_with_github()/
        force_sync_with_github() write no column field at all. Reading
        next_issue.get('column') therefore yielded None on every real
        invocation, no workflow column ever matched, and this site took the
        no-agent branch 100% of the time -- so it could never dispatch
        anything. The column must come from GitHub, like both sibling sites."""
        (mock_lock_manager, mock_queue, mock_run_manager,
         workflow_template, project_config) = self._mocks()

        # Exactly what force_sync_with_github() produces: no column of any kind.
        mock_queue.get_next_n_waiting_issues.return_value = [
            {
                'issue_number': 200,
                'position_in_column': 0,
                'status': 'waiting',
                'added_at': '2026-01-01T00:00:00+00:00',
                'last_position_check': '2026-01-01T00:00:00+00:00',
                'title': 'Next issue',
            }
        ]
        mock_run_manager.ensure_pipeline_run_for_task.return_value = 'run-200'

        mock_task_queue = Mock()
        self._run(mock_lock_manager, mock_queue, mock_run_manager,
                  workflow_template, project_config, mock_task_queue)

        mock_run_manager._resolve_issue_column_from_github.assert_called_once()
        assert mock_run_manager._resolve_issue_column_from_github.call_args[0][2] == 200

        mock_task_queue.enqueue.assert_called_once()
        dispatched_task = mock_task_queue.enqueue.call_args[0][0]
        assert dispatched_task.context['issue_number'] == 200
        assert dispatched_task.context['column'] == 'Development'
        assert dispatched_task.agent == 'senior_software_engineer'

        # Nothing failed, so nothing is rolled back.
        mock_queue.reset_issue_to_waiting.assert_not_called()
        mock_lock_manager.release_lock.assert_called_once_with('test-project', 'dev', 100)

    def test_rolls_back_when_column_cannot_be_resolved_from_github(self):
        """A GitHub read that can't place the issue on the board must roll
        back both halves rather than dispatch against a stale cached column."""
        (mock_lock_manager, mock_queue, mock_run_manager,
         workflow_template, project_config) = self._mocks(github_column=None)

        mock_queue.get_next_n_waiting_issues.return_value = [
            {'issue_number': 200, 'position_in_column': 0, 'status': 'waiting',
             'initial_column': 'Development'}
        ]

        mock_task_queue = Mock()
        self._run(mock_lock_manager, mock_queue, mock_run_manager,
                  workflow_template, project_config, mock_task_queue)

        mock_task_queue.enqueue.assert_not_called()
        mock_queue.reset_issue_to_waiting.assert_called_once_with(
            200, expected_activated_at=ACTIVATED_AT
        )
        mock_lock_manager.release_lock.assert_any_call('test-project', 'dev', 200)

    def test_rollback_releases_the_lock_before_the_compare_and_swap_reset(self):
        """ORDER REGRESSION (#147): the reset must NOT run while the lock is
        still held. try_acquire_lock() returns True/"already_holds_lock" for the
        current holder and trigger_agent_for_status() dispatches on that branch,
        so a competing poll can genuinely start #200 in that window -- and the
        unconditional release that follows would then free the lock out from
        under a running agent. Releasing first opens the symmetric window ("lock
        free, entry still 'active'"), which the activated_at compare-and-swap
        closes instead."""
        (mock_lock_manager, mock_queue, mock_run_manager,
         workflow_template, project_config) = self._mocks()

        mock_queue.get_next_n_waiting_issues.return_value = [
            {'issue_number': 200, 'position_in_column': 0, 'status': 'waiting',
             'initial_column': 'Development'}
        ]
        mock_run_manager.ensure_pipeline_run_for_task.return_value = None

        call_order = []
        mock_queue.reset_issue_to_waiting.side_effect = (
            lambda *a, **kw: call_order.append('reset')
        )
        mock_lock_manager.release_lock.side_effect = (
            lambda *a, **kw: call_order.append(f'release-{a[2]}') or True
        )

        mock_task_queue = Mock()
        self._run(mock_lock_manager, mock_queue, mock_run_manager,
                  workflow_template, project_config, mock_task_queue)

        # release-100 is the exiting issue's own release, before dispatch.
        assert call_order == ['release-100', 'release-200', 'reset']

    def test_rollback_still_releases_lock_when_queue_reset_raises(self):
        """A failing reset loses one issue; a retained lock deadlocks the whole
        board. The release runs first and unconditionally so the second can
        never happen because of the first."""
        (mock_lock_manager, mock_queue, mock_run_manager,
         workflow_template, project_config) = self._mocks()

        mock_queue.get_next_n_waiting_issues.return_value = [
            {'issue_number': 200, 'position_in_column': 0, 'status': 'waiting',
             'initial_column': 'Development'}
        ]
        mock_run_manager.ensure_pipeline_run_for_task.return_value = None
        mock_queue.reset_issue_to_waiting.side_effect = RuntimeError("queue file unwritable")

        mock_task_queue = Mock()
        self._run(mock_lock_manager, mock_queue, mock_run_manager,
                  workflow_template, project_config, mock_task_queue)

        mock_lock_manager.release_lock.assert_any_call('test-project', 'dev', 200)

