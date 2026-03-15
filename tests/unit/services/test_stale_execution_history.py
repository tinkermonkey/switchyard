"""
Unit tests for stale in_progress execution history cleanup.

Covers:
1. Fix 1 — conditional guard in orchestrator_integration.process_task_integrated():
   a pre-enqueue 'manual' probe prevents a second 'task_queue' probe from being created.

2. Fix 2 — WorkExecutionStateTracker.abandon_stale_in_progress_entries():
   correctly abandons entries with no task_id or a task_id not in active_task_ids,
   while leaving entries with a task_id in active_task_ids untouched.

3. Fix 3 — AgentContainerRecovery.cleanup_orphaned_execution_history():
   globs state files, parses project/issue, and delegates abandonment correctly.
"""

import os
import sys
import re
import yaml
import pytest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock, call


# ---------------------------------------------------------------------------
# Helpers for importing WorkExecutionStateTracker with a custom state dir
# ---------------------------------------------------------------------------

def _import_tracker_class(tmp_path):
    """Force-reimport WorkExecutionStateTracker with ORCHESTRATOR_ROOT = tmp_path."""
    os.environ['ORCHESTRATOR_ROOT'] = str(tmp_path)
    sys.modules.pop('services.work_execution_state', None)
    from services.work_execution_state import WorkExecutionStateTracker
    return WorkExecutionStateTracker


# ===========================================================================
# Fix 1 — conditional guard in orchestrator_integration
# ===========================================================================

class TestConditionalExecutionStartGuard:
    """
    process_task_integrated() must NOT write a second in_progress probe when one
    already exists for the same agent/column (written by project_monitor before enqueue).
    """

    def _make_task(self, project='myproject', agent='software_engineer',
                   issue_number=42, column='In Development'):
        task = MagicMock()
        task.agent = agent
        task.project = project
        task.id = 'test-task-id'
        task.context = {
            'issue_number': issue_number,
            'column': column,
            'board': 'dev-board',
        }
        return task

    def test_no_duplicate_probe_when_in_progress_exists(self, tmp_path):
        """
        When a 'manual' probe already exists (written by project_monitor),
        process_task_integrated() must NOT call record_execution_start again.
        """
        cls = _import_tracker_class(tmp_path)
        tracker = cls(state_dir=tmp_path)

        # Simulate project_monitor writing the pre-enqueue probe
        tracker.record_execution_start(
            issue_number=42,
            column='In Development',
            agent='software_engineer',
            trigger_source='manual',
            project_name='myproject'
        )

        assert len(tracker.load_state('myproject', 42)['execution_history']) == 1

        # Patch the module-level singleton so our tracker is used
        with patch('services.work_execution_state.work_execution_tracker', tracker), \
             patch('agents.orchestrator_integration.validate_task_can_run',
                   return_value={'can_run': True, 'reason': 'ok'}), \
             patch('services.agent_executor.get_agent_executor') as mock_executor_factory:

            mock_executor = MagicMock()
            mock_executor.execute_agent = AsyncMock(return_value={'manual_progression_made': False})
            mock_executor_factory.return_value = mock_executor

            import asyncio
            from agents.orchestrator_integration import process_task_integrated

            task = self._make_task()
            state_manager = MagicMock()
            logger = MagicMock()
            logger.log_warning = MagicMock()

            asyncio.run(process_task_integrated(task, state_manager, logger))

        # Still only one entry — the pre-enqueue probe
        state = tracker.load_state('myproject', 42)
        assert len(state['execution_history']) == 1
        assert state['execution_history'][0]['trigger_source'] == 'manual'
        assert state['execution_history'][0]['outcome'] == 'in_progress'

    def test_writes_probe_when_none_exists(self, tmp_path):
        """
        When no in_progress entry exists (task queued by a path that skips project_monitor),
        process_task_integrated() MUST write the 'task_queue' probe.
        """
        cls = _import_tracker_class(tmp_path)
        tracker = cls(state_dir=tmp_path)

        # No pre-existing probe — state file is empty
        assert len(tracker.load_state('myproject', 42)['execution_history']) == 0

        with patch('services.work_execution_state.work_execution_tracker', tracker), \
             patch('agents.orchestrator_integration.validate_task_can_run',
                   return_value={'can_run': True, 'reason': 'ok'}), \
             patch('services.agent_executor.get_agent_executor') as mock_executor_factory:

            mock_executor = MagicMock()
            mock_executor.execute_agent = AsyncMock(return_value={'manual_progression_made': False})
            mock_executor_factory.return_value = mock_executor

            import asyncio
            from agents.orchestrator_integration import process_task_integrated

            task = self._make_task()
            state_manager = MagicMock()
            logger = MagicMock()
            logger.log_warning = MagicMock()

            asyncio.run(process_task_integrated(task, state_manager, logger))

        state = tracker.load_state('myproject', 42)
        assert len(state['execution_history']) == 1
        assert state['execution_history'][0]['trigger_source'] == 'task_queue'


# ===========================================================================
# Fix 2 — WorkExecutionStateTracker.abandon_stale_in_progress_entries()
# ===========================================================================

class TestAbandonStaleInProgressEntries:
    """Tests for WorkExecutionStateTracker.abandon_stale_in_progress_entries()."""

    def _make_tracker(self, tmp_path):
        cls = _import_tracker_class(tmp_path)
        return cls(state_dir=tmp_path)

    def _seed_state(self, tracker, project, issue, executions):
        """Write a state file with a custom execution_history list."""
        state = {
            'issue_number': issue,
            'project_name': project,
            'execution_history': executions,
            'status_changes': [],
            'current_status': None,
            'last_updated': None,
        }
        tracker.save_state(project, issue, state)

    # ------------------------------------------------------------------
    # Basic cases
    # ------------------------------------------------------------------

    def test_no_in_progress_returns_zero(self, tmp_path):
        """When no in_progress entries exist, nothing is abandoned."""
        tracker = self._make_tracker(tmp_path)
        self._seed_state(tracker, 'proj', 1, [
            {'column': 'Done', 'agent': 'eng', 'outcome': 'success',
             'timestamp': '2025-01-01T00:00:00+00:00', 'trigger_source': 'manual'},
        ])
        count = tracker.abandon_stale_in_progress_entries('proj', 1, active_task_ids=set())
        assert count == 0

    def test_in_progress_no_task_id_is_abandoned(self, tmp_path):
        """in_progress entry with no task_id is always abandoned (pure probe)."""
        tracker = self._make_tracker(tmp_path)
        self._seed_state(tracker, 'proj', 2, [
            {'column': 'In Development', 'agent': 'eng', 'outcome': 'in_progress',
             'timestamp': '2025-01-01T00:00:00+00:00', 'trigger_source': 'manual'},
        ])
        count = tracker.abandon_stale_in_progress_entries('proj', 2, active_task_ids=set())
        assert count == 1
        state = tracker.load_state('proj', 2)
        assert state['execution_history'][0]['outcome'] == 'abandoned'
        assert 'Orchestrator restarted' in state['execution_history'][0]['error']

    def test_in_progress_with_task_id_not_in_active_set_is_abandoned(self, tmp_path):
        """in_progress entry whose task_id is NOT in active_task_ids is abandoned."""
        tracker = self._make_tracker(tmp_path)
        self._seed_state(tracker, 'proj', 3, [
            {'column': 'In Development', 'agent': 'eng', 'outcome': 'in_progress',
             'timestamp': '2025-01-01T00:00:00+00:00', 'trigger_source': 'task_queue',
             'task_id': 'dead-task-id'},
        ])
        count = tracker.abandon_stale_in_progress_entries('proj', 3, active_task_ids={'other-task'})
        assert count == 1
        state = tracker.load_state('proj', 3)
        assert state['execution_history'][0]['outcome'] == 'abandoned'

    def test_in_progress_with_task_id_in_active_set_is_skipped(self, tmp_path):
        """in_progress entry whose task_id IS in active_task_ids is left untouched."""
        tracker = self._make_tracker(tmp_path)
        self._seed_state(tracker, 'proj', 4, [
            {'column': 'In Development', 'agent': 'eng', 'outcome': 'in_progress',
             'timestamp': '2025-01-01T00:00:00+00:00', 'trigger_source': 'task_queue',
             'task_id': 'live-task-id'},
        ])
        count = tracker.abandon_stale_in_progress_entries('proj', 4, active_task_ids={'live-task-id'})
        assert count == 0
        state = tracker.load_state('proj', 4)
        assert state['execution_history'][0]['outcome'] == 'in_progress'

    # ------------------------------------------------------------------
    # Mixed entries
    # ------------------------------------------------------------------

    def test_mixed_entries_only_stale_abandoned(self, tmp_path):
        """
        Given:
          - entry A: in_progress, task_id in active set → keep
          - entry B: in_progress, task_id not in active set → abandon
          - entry C: in_progress, no task_id → abandon
          - entry D: success → keep

        Only B and C are abandoned.
        """
        tracker = self._make_tracker(tmp_path)
        self._seed_state(tracker, 'proj', 5, [
            {'column': 'In Development', 'agent': 'eng', 'outcome': 'in_progress',
             'timestamp': '2025-01-01T00:00:00+00:00', 'trigger_source': 'task_queue',
             'task_id': 'live-task-id'},   # A — keep
            {'column': 'In Review', 'agent': 'reviewer', 'outcome': 'in_progress',
             'timestamp': '2025-01-01T01:00:00+00:00', 'trigger_source': 'task_queue',
             'task_id': 'dead-task-id'},   # B — abandon
            {'column': 'Planning', 'agent': 'planner', 'outcome': 'in_progress',
             'timestamp': '2025-01-01T02:00:00+00:00', 'trigger_source': 'manual'},    # C — abandon (no task_id)
            {'column': 'Done', 'agent': 'eng', 'outcome': 'success',
             'timestamp': '2025-01-01T03:00:00+00:00', 'trigger_source': 'task_queue'}, # D — keep
        ])

        count = tracker.abandon_stale_in_progress_entries(
            'proj', 5, active_task_ids={'live-task-id'}
        )
        assert count == 2

        state = tracker.load_state('proj', 5)
        hist = state['execution_history']
        assert hist[0]['outcome'] == 'in_progress'   # A kept
        assert hist[1]['outcome'] == 'abandoned'      # B abandoned
        assert hist[2]['outcome'] == 'abandoned'      # C abandoned
        assert hist[3]['outcome'] == 'success'        # D kept

    def test_returns_zero_for_missing_state_file(self, tmp_path):
        """When the state file doesn't exist, returns 0 gracefully."""
        tracker = self._make_tracker(tmp_path)
        count = tracker.abandon_stale_in_progress_entries('proj', 999, active_task_ids=set())
        assert count == 0


# ===========================================================================
# Fix 3 — AgentContainerRecovery.cleanup_orphaned_execution_history()
# ===========================================================================

class TestCleanupOrphanedExecutionHistory:
    """Tests for AgentContainerRecovery.cleanup_orphaned_execution_history()."""

    def _make_recovery(self, mock_redis=None):
        if mock_redis is None:
            mock_redis = MagicMock()
        with patch('redis.Redis', return_value=mock_redis):
            from services.agent_container_recovery import AgentContainerRecovery
            return AgentContainerRecovery(redis_client=mock_redis)

    def _seed_history_file(self, state_dir, project, issue, executions):
        """Write a YAML state file into the given state_dir."""
        filename = f"{project}_issue_{issue}.yaml"
        state = {
            'issue_number': issue,
            'project_name': project,
            'execution_history': executions,
            'status_changes': [],
            'current_status': None,
            'last_updated': None,
        }
        (state_dir / filename).write_text(yaml.dump(state))

    def test_abandons_in_progress_with_no_task_id(self, tmp_path):
        """Entries with no task_id are abandoned (no active containers)."""
        state_dir = tmp_path / 'state' / 'execution_history'
        state_dir.mkdir(parents=True)
        self._seed_history_file(state_dir, 'proj', 1, [
            {'column': 'In Development', 'agent': 'eng', 'outcome': 'in_progress',
             'timestamp': '2025-01-01T00:00:00+00:00', 'trigger_source': 'manual'},
        ])

        recovery = self._make_recovery()

        import sys
        sys.modules.pop('services.work_execution_state', None)
        os.environ['ORCHESTRATOR_ROOT'] = str(tmp_path)

        # Re-import so the singleton uses our state_dir
        from services.work_execution_state import WorkExecutionStateTracker
        tracker = WorkExecutionStateTracker(state_dir=state_dir)

        with patch('services.agent_container_recovery.work_execution_tracker', tracker, create=True), \
             patch('services.work_execution_state.work_execution_tracker', tracker):
            # Patch the import inside cleanup_orphaned_execution_history
            with patch('services.agent_container_recovery.AgentContainerRecovery.cleanup_orphaned_execution_history',
                       wraps=recovery.cleanup_orphaned_execution_history):
                pass  # just verifying the method exists

            # Directly call the method on a fresh recovery instance,
            # but patch the tracker it imports
            with patch('services.work_execution_state.work_execution_tracker', tracker):
                total = recovery.cleanup_orphaned_execution_history(recovered_task_ids=set())

        assert total == 1

        # Verify the file was actually updated
        filename = state_dir / 'proj_issue_1.yaml'
        state = yaml.safe_load(filename.read_text())
        assert state['execution_history'][0]['outcome'] == 'abandoned'

    def test_skips_entry_with_active_task_id(self, tmp_path):
        """Entries with a task_id in recovered_task_ids are not abandoned."""
        state_dir = tmp_path / 'state' / 'execution_history'
        state_dir.mkdir(parents=True)
        self._seed_history_file(state_dir, 'proj', 2, [
            {'column': 'In Development', 'agent': 'eng', 'outcome': 'in_progress',
             'timestamp': '2025-01-01T00:00:00+00:00', 'trigger_source': 'task_queue',
             'task_id': 'alive-task'},
        ])

        recovery = self._make_recovery()

        sys.modules.pop('services.work_execution_state', None)
        os.environ['ORCHESTRATOR_ROOT'] = str(tmp_path)
        from services.work_execution_state import WorkExecutionStateTracker
        tracker = WorkExecutionStateTracker(state_dir=state_dir)

        with patch('services.work_execution_state.work_execution_tracker', tracker):
            total = recovery.cleanup_orphaned_execution_history(recovered_task_ids={'alive-task'})

        assert total == 0

        filename = state_dir / 'proj_issue_2.yaml'
        state = yaml.safe_load(filename.read_text())
        assert state['execution_history'][0]['outcome'] == 'in_progress'

    def test_multiple_files_mixed_state(self, tmp_path):
        """
        Multiple state files with mixed entries — only stale ones are abandoned.
        """
        state_dir = tmp_path / 'state' / 'execution_history'
        state_dir.mkdir(parents=True)

        # File 1: one stale entry (no task_id), one active
        self._seed_history_file(state_dir, 'alpha', 10, [
            {'column': 'In Development', 'agent': 'eng', 'outcome': 'in_progress',
             'timestamp': '2025-01-01T00:00:00+00:00', 'trigger_source': 'manual'},       # stale
            {'column': 'In Review', 'agent': 'reviewer', 'outcome': 'in_progress',
             'timestamp': '2025-01-01T01:00:00+00:00', 'trigger_source': 'task_queue',
             'task_id': 'live-1'},                                                          # active
        ])

        # File 2: dead container task_id
        self._seed_history_file(state_dir, 'beta', 20, [
            {'column': 'Testing', 'agent': 'tester', 'outcome': 'in_progress',
             'timestamp': '2025-01-01T00:00:00+00:00', 'trigger_source': 'task_queue',
             'task_id': 'dead-2'},                                                          # stale
        ])

        # File 3: already completed, nothing to abandon
        self._seed_history_file(state_dir, 'gamma', 30, [
            {'column': 'Done', 'agent': 'eng', 'outcome': 'success',
             'timestamp': '2025-01-01T00:00:00+00:00', 'trigger_source': 'task_queue'},
        ])

        recovery = self._make_recovery()

        sys.modules.pop('services.work_execution_state', None)
        os.environ['ORCHESTRATOR_ROOT'] = str(tmp_path)
        from services.work_execution_state import WorkExecutionStateTracker
        tracker = WorkExecutionStateTracker(state_dir=state_dir)

        with patch('services.work_execution_state.work_execution_tracker', tracker):
            total = recovery.cleanup_orphaned_execution_history(
                recovered_task_ids={'live-1'}  # dead-2 is absent → stale
            )

        # alpha/10 probe (no task_id) + beta/20 dead task_id = 2
        assert total == 2

        alpha_state = yaml.safe_load((state_dir / 'alpha_issue_10.yaml').read_text())
        assert alpha_state['execution_history'][0]['outcome'] == 'abandoned'
        assert alpha_state['execution_history'][1]['outcome'] == 'in_progress'  # still live

        beta_state = yaml.safe_load((state_dir / 'beta_issue_20.yaml').read_text())
        assert beta_state['execution_history'][0]['outcome'] == 'abandoned'

        gamma_state = yaml.safe_load((state_dir / 'gamma_issue_30.yaml').read_text())
        assert gamma_state['execution_history'][0]['outcome'] == 'success'

    def test_ignores_non_matching_yaml_files(self, tmp_path):
        """YAML files that don't match the project_issue_N.yaml pattern are skipped."""
        state_dir = tmp_path / 'state' / 'execution_history'
        state_dir.mkdir(parents=True)

        # File with non-matching name
        (state_dir / 'some_other_config.yaml').write_text('key: value\n')

        recovery = self._make_recovery()

        sys.modules.pop('services.work_execution_state', None)
        os.environ['ORCHESTRATOR_ROOT'] = str(tmp_path)
        from services.work_execution_state import WorkExecutionStateTracker
        tracker = WorkExecutionStateTracker(state_dir=state_dir)

        with patch('services.work_execution_state.work_execution_tracker', tracker):
            total = recovery.cleanup_orphaned_execution_history(recovered_task_ids=set())

        # No matching files → 0 abandoned
        assert total == 0

    def test_returns_zero_when_state_dir_empty(self, tmp_path):
        """Returns 0 without crashing when the state directory has no YAML files."""
        state_dir = tmp_path / 'state' / 'execution_history'
        state_dir.mkdir(parents=True)

        recovery = self._make_recovery()

        sys.modules.pop('services.work_execution_state', None)
        os.environ['ORCHESTRATOR_ROOT'] = str(tmp_path)
        from services.work_execution_state import WorkExecutionStateTracker
        tracker = WorkExecutionStateTracker(state_dir=state_dir)

        with patch('services.work_execution_state.work_execution_tracker', tracker):
            total = recovery.cleanup_orphaned_execution_history(recovered_task_ids=set())

        assert total == 0


# ===========================================================================
# Integration: recover_or_cleanup_containers collects task_ids correctly
# ===========================================================================

class TestRecoverOrCleanupContainersTracksTaskIds:
    """
    Verify that recover_or_cleanup_containers() passes the correct task_id set
    to cleanup_orphaned_execution_history().
    """

    def test_recovered_task_ids_passed_to_cleanup(self, tmp_path):
        """
        When a container is successfully recovered, its task_id must appear in
        the active_task_ids set passed to cleanup_orphaned_execution_history().
        """
        mock_redis = MagicMock()

        with patch('redis.Redis', return_value=mock_redis):
            from services.agent_container_recovery import AgentContainerRecovery
            recovery = AgentContainerRecovery(redis_client=mock_redis)

        # Use a recent timestamp so the container is not killed as "too old"
        recent_ts = (datetime.utcnow() - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')

        # Simulate one running container that will be recovered
        recovery.get_running_agent_containers = MagicMock(return_value=[{
            'name': 'claude-agent-myproject-abc123',
            'id': 'container-id-1',
            'status': 'Up 5 minutes',
            'created_at': f'{recent_ts} +0000 UTC',
            'image': 'myproject-agent',
            'labels': (
                'org.switchyard.project=myproject,'
                'org.switchyard.agent=software_engineer,'
                'org.switchyard.task_id=abc123,'
                'org.switchyard.issue_number=42'
            ),
        }])

        # Execution history shows a matching in_progress entry
        recovery.check_execution_history = MagicMock(return_value={
            'column': 'In Development',
            'agent': 'software_engineer',
            'outcome': 'in_progress',
            'timestamp': '2025-01-01T12:00:00+00:00',
        })

        # No cancellation
        mock_cancel_signal = MagicMock()
        mock_cancel_signal.is_cancelled.return_value = False

        # Container is not too old (created_at within 2 hours)
        # Already set above

        # Mock reconnect
        mock_docker_runner = MagicMock()

        captured_task_ids = []

        def capture_cleanup(recovered_task_ids):
            captured_task_ids.append(set(recovered_task_ids))
            return 0

        recovery.cleanup_orphaned_execution_history = capture_cleanup

        with patch('services.cancellation.get_cancellation_signal', return_value=mock_cancel_signal), \
             patch('claude.docker_runner.DockerAgentRunner', return_value=mock_docker_runner):
            recovery.recover_or_cleanup_containers()

        assert len(captured_task_ids) == 1
        assert 'abc123' in captured_task_ids[0]
