"""
Test that dev container state is synchronized when cleanup detects stuck verifier.

This test validates the bug fix for the issue where WorkExecutionStateTracker
and DevContainerStateManager were not synchronized when a dev_environment_verifier
container died unexpectedly.
"""

import contextlib
import os
import pytest
import tempfile
import yaml
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock, patch, MagicMock

# Set ORCHESTRATOR_ROOT to temp dir before importing modules
# This prevents permission errors when modules create state directories at import time
_temp_root = tempfile.mkdtemp()
os.environ['ORCHESTRATOR_ROOT'] = _temp_root

# Skip if not in Docker environment
try:
    from services.work_execution_state import WorkExecutionStateTracker
    from services.dev_container_state import DevContainerStateManager, DevContainerStatus
except ImportError:
    pytest.skip("Requires Docker container environment", allow_module_level=True)


def _build_lock(granted: bool):
    """Stand-in for dev_container_build_lock_if_free_sync (#152 item A).

    cleanup_stuck_in_progress_states() now reconciles dev container state under
    that lock, non-blockingly, and skips the write when it is busy. These tests
    already replace Redis with a MagicMock, under which the real facade
    fail-closes to "busy", so the lock has to be stated explicitly rather than
    left to fall out of the mock.
    """
    @contextlib.contextmanager
    def _cm(*args, **kwargs):
        yield granted
    return _cm


class TestDevContainerStateSync:
    """Test synchronization between work execution and dev container state"""

    @pytest.fixture
    def temp_dirs(self):
        """Create temporary directories for state files"""
        with tempfile.TemporaryDirectory() as exec_dir, \
             tempfile.TemporaryDirectory() as dev_dir:
            yield Path(exec_dir), Path(dev_dir)

    @pytest.fixture
    def execution_tracker(self, temp_dirs):
        """Create WorkExecutionStateTracker with temp directory"""
        exec_dir, _ = temp_dirs
        return WorkExecutionStateTracker(state_dir=exec_dir)

    @pytest.fixture
    def dev_container_mgr(self, temp_dirs):
        """Create DevContainerStateManager with temp directory"""
        _, dev_dir = temp_dirs
        return DevContainerStateManager(state_dir=dev_dir)

    def test_stuck_verifier_resets_dev_container_state(
        self,
        execution_tracker,
        dev_container_mgr,
        temp_dirs
    ):
        """
        Test that when cleanup detects a stuck dev_environment_verifier,
        it resets the dev container state to UNVERIFIED.
        """
        project_name = "test-project"
        issue_number = 42
        agent = "dev_environment_verifier"
        column = "Verification"

        # Setup: Create stuck execution state
        execution_tracker.record_execution_start(
            issue_number=issue_number,
            column=column,
            agent=agent,
            trigger_source='manual',
            project_name=project_name
        )

        # Setup: Set dev container state to IN_PROGRESS
        dev_container_mgr.set_status(
            project_name=project_name,
            status=DevContainerStatus.IN_PROGRESS,
            image_name=f"{project_name}-agent:latest"
        )

        # Verify initial state
        assert dev_container_mgr.get_status(project_name) == DevContainerStatus.IN_PROGRESS

        # Mock subprocess to simulate no running containers
        with patch('subprocess.run') as mock_run:
            # Mock docker ps to return empty (no containers)
            mock_run.return_value = Mock(
                returncode=0,
                stdout='',
                stderr=''
            )

            # Mock Redis to simulate no tracking keys
            with patch('redis.Redis') as mock_redis:
                mock_redis_client = MagicMock()
                mock_redis.return_value = mock_redis_client

                # No Redis tracking keys found
                mock_redis_client.scan_iter.return_value = []
                mock_redis_client.exists.return_value = False
                mock_redis_client.keys.return_value = []
                mock_redis_client.lrange.return_value = []

                # Patch the module-level singleton used in cleanup_stuck_in_progress_states
                with patch('services.dev_container_state.dev_container_state', dev_container_mgr), \
                     patch('services.dev_container_build_lock.dev_container_build_lock_if_free_sync', _build_lock(True)):
                    # Run cleanup
                    execution_tracker.cleanup_stuck_in_progress_states()

        # Verify: Work execution state should be marked as failure
        last_exec = execution_tracker.get_last_execution(
            project_name=project_name,
            issue_number=issue_number,
            column=column,
            agent=agent
        )
        assert last_exec is not None
        assert last_exec['outcome'] == 'failure'
        assert 'interrupted' in last_exec['error'].lower()

        # Verify: Dev container state should be reset to UNVERIFIED
        status = dev_container_mgr.get_status(project_name)
        assert status == DevContainerStatus.UNVERIFIED

    def test_recovered_verifier_failure_blocks_dev_container(
        self,
        execution_tracker,
        dev_container_mgr,
        temp_dirs
    ):
        """
        Test that when a dev_environment_verifier execution is recovered
        from Redis with failure, the dev container state is marked as BLOCKED.
        """
        project_name = "test-project"
        issue_number = 43
        agent = "dev_environment_verifier"
        column = "Verification"

        # Setup: Create stuck execution state
        execution_tracker.record_execution_start(
            issue_number=issue_number,
            column=column,
            agent=agent,
            trigger_source='manual',
            project_name=project_name
        )

        # Setup: Set dev container state to IN_PROGRESS
        dev_container_mgr.set_status(
            project_name=project_name,
            status=DevContainerStatus.IN_PROGRESS,
            image_name=f"{project_name}-agent:latest"
        )

        # Mock subprocess to simulate no running containers
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout='',
                stderr=''
            )

            # Mock Redis to simulate result recovery with failure
            with patch('redis.Redis') as mock_redis:
                mock_redis_client = MagicMock()
                mock_redis.return_value = mock_redis_client

                mock_redis_client.scan_iter.side_effect = [
                    # First call: tracking keys (agent:container:*) — none found
                    [],
                    # Second call: result keys (agent_result:*) — found one
                    [f"agent_result:{project_name}:{issue_number}:task123"],
                ]
                mock_redis_client.exists.return_value = False
                mock_redis_client.keys.return_value = []
                mock_redis_client.lrange.return_value = []

                # Mock Redis result with failure (exit_code != 0)
                import json
                mock_redis_client.get.return_value = json.dumps({
                    'agent': agent,
                    'exit_code': 1,
                    'output': 'Docker build failed'
                })

                # Patch the module-level singleton
                with patch('services.dev_container_state.dev_container_state', dev_container_mgr), \
                     patch('services.dev_container_build_lock.dev_container_build_lock_if_free_sync', _build_lock(True)):
                    # Run cleanup
                    execution_tracker.cleanup_stuck_in_progress_states()

        # Verify: Dev container state should be BLOCKED
        status = dev_container_mgr.get_status(project_name)
        assert status == DevContainerStatus.BLOCKED

    def test_recovered_verifier_success_verifies_state(
        self,
        execution_tracker,
        dev_container_mgr,
        temp_dirs
    ):
        """
        Test that when a dev_environment_verifier execution is recovered
        from Redis with success, the dev container state is verified as VERIFIED.
        """
        project_name = "test-project"
        issue_number = 44
        agent = "dev_environment_verifier"
        column = "Verification"

        # Setup: Create stuck execution state
        execution_tracker.record_execution_start(
            issue_number=issue_number,
            column=column,
            agent=agent,
            trigger_source='manual',
            project_name=project_name
        )

        # Setup: Simulate agent already marked state as VERIFIED before exit
        dev_container_mgr.set_status(
            project_name=project_name,
            status=DevContainerStatus.VERIFIED,
            image_name=f"{project_name}-agent:latest"
        )

        # Mock subprocess to simulate no running containers
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout='',
                stderr=''
            )

            # Mock Redis to simulate result recovery with success
            with patch('redis.Redis') as mock_redis:
                mock_redis_client = MagicMock()
                mock_redis.return_value = mock_redis_client

                mock_redis_client.scan_iter.side_effect = [
                    # First call: tracking keys (agent:container:*) — none found
                    [],
                    # Second call: result keys (agent_result:*) — found one
                    [f"agent_result:{project_name}:{issue_number}:task123"],
                ]
                mock_redis_client.exists.return_value = False
                mock_redis_client.keys.return_value = []
                mock_redis_client.lrange.return_value = []

                # Mock Redis result with success (exit_code == 0)
                import json
                mock_redis_client.get.return_value = json.dumps({
                    'agent': agent,
                    'exit_code': 0,
                    'output': 'Build succeeded'
                })

                # Mock decision events and observability
                with patch('monitoring.decision_events.DecisionEventEmitter'), \
                     patch('monitoring.observability.get_observability_manager'), \
                     patch('services.pipeline_run.get_pipeline_run_manager'):
                    # Patch the module-level singleton
                    with patch('services.dev_container_state.dev_container_state', dev_container_mgr), \
                     patch('services.dev_container_build_lock.dev_container_build_lock_if_free_sync', _build_lock(True)):
                        # Run cleanup
                        execution_tracker.cleanup_stuck_in_progress_states()

        # Verify: Dev container state should remain VERIFIED
        status = dev_container_mgr.get_status(project_name)
        assert status == DevContainerStatus.VERIFIED

    def test_stuck_setup_resets_dev_container_state(
        self,
        execution_tracker,
        dev_container_mgr,
        temp_dirs
    ):
        """
        Test that when cleanup detects a stuck dev_environment_setup,
        it resets the dev container state to UNVERIFIED (not BLOCKED),
        so setup can be automatically retried.
        """
        project_name = "test-project"
        issue_number = 46
        agent = "dev_environment_setup"
        column = "Setup"

        # Setup: Create stuck execution state
        execution_tracker.record_execution_start(
            issue_number=issue_number,
            column=column,
            agent=agent,
            trigger_source='manual',
            project_name=project_name
        )

        # Setup: Set dev container state to IN_PROGRESS
        dev_container_mgr.set_status(
            project_name=project_name,
            status=DevContainerStatus.IN_PROGRESS,
            image_name=f"{project_name}-agent:latest"
        )

        # Verify initial state
        assert dev_container_mgr.get_status(project_name) == DevContainerStatus.IN_PROGRESS

        # Mock subprocess to simulate no running containers
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout='',
                stderr=''
            )

            # Mock Redis to simulate no tracking keys and no results
            with patch('redis.Redis') as mock_redis:
                mock_redis_client = MagicMock()
                mock_redis.return_value = mock_redis_client

                mock_redis_client.scan_iter.return_value = []
                mock_redis_client.exists.return_value = False
                mock_redis_client.keys.return_value = []
                mock_redis_client.lrange.return_value = []

                # Patch the module-level singleton
                with patch('services.dev_container_state.dev_container_state', dev_container_mgr), \
                     patch('services.dev_container_build_lock.dev_container_build_lock_if_free_sync', _build_lock(True)):
                    execution_tracker.cleanup_stuck_in_progress_states()

        # Verify: Work execution state should be marked as failure
        last_exec = execution_tracker.get_last_execution(
            project_name=project_name,
            issue_number=issue_number,
            column=column,
            agent=agent
        )
        assert last_exec is not None
        assert last_exec['outcome'] == 'failure'

        # Verify: Dev container state should be reset to UNVERIFIED (not BLOCKED)
        status = dev_container_mgr.get_status(project_name)
        assert status == DevContainerStatus.UNVERIFIED

    def test_non_verifier_agent_does_not_affect_dev_container_state(
        self,
        execution_tracker,
        dev_container_mgr,
        temp_dirs
    ):
        """
        Test that cleanup of non-verifier agents does not affect dev container state.
        """
        project_name = "test-project"
        issue_number = 45
        agent = "senior_software_engineer"  # Different agent
        column = "Development"

        # Setup: Create stuck execution state for different agent
        execution_tracker.record_execution_start(
            issue_number=issue_number,
            column=column,
            agent=agent,
            trigger_source='manual',
            project_name=project_name
        )

        # Setup: Set dev container state to VERIFIED (should not change)
        dev_container_mgr.set_status(
            project_name=project_name,
            status=DevContainerStatus.VERIFIED,
            image_name=f"{project_name}-agent:latest"
        )

        # Mock subprocess to simulate no running containers
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout='',
                stderr=''
            )

            # Mock Redis
            with patch('redis.Redis') as mock_redis:
                mock_redis_client = MagicMock()
                mock_redis.return_value = mock_redis_client

                mock_redis_client.scan_iter.return_value = []
                mock_redis_client.exists.return_value = False
                mock_redis_client.keys.return_value = []
                mock_redis_client.lrange.return_value = []

                # Run cleanup
                execution_tracker.cleanup_stuck_in_progress_states()

        # Verify: Dev container state should remain VERIFIED (unchanged)
        status = dev_container_mgr.get_status(project_name)
        assert status == DevContainerStatus.VERIFIED


class TestReconciliationTakesTheDevContainerBuildLock:
    """
    #152 item A: cleanup_stuck_in_progress_states() writes dev container state
    at five points when a dev_environment_setup/verifier execution is found
    stuck, and every one of them used to be an unlocked read-then-write -- run
    from main.py's startup, i.e. exactly when a build started by the previous
    process can still be in flight and holding that project's build lock.
    """

    @pytest.fixture
    def temp_dirs(self):
        with tempfile.TemporaryDirectory() as exec_dir, \
             tempfile.TemporaryDirectory() as dev_dir:
            yield Path(exec_dir), Path(dev_dir)

    @pytest.fixture
    def execution_tracker(self, temp_dirs):
        exec_dir, _ = temp_dirs
        return WorkExecutionStateTracker(state_dir=exec_dir)

    @pytest.fixture
    def dev_container_mgr(self, temp_dirs):
        _, dev_dir = temp_dirs
        return DevContainerStateManager(state_dir=dev_dir)

    def _run_cleanup_with_stuck_verifier(self, execution_tracker, dev_container_mgr,
                                         lock_granted, initial_status):
        project_name = "test-project"
        execution_tracker.record_execution_start(
            issue_number=42,
            column="Verification",
            agent="dev_environment_verifier",
            trigger_source='manual',
            project_name=project_name
        )
        dev_container_mgr.set_status(
            project_name=project_name,
            status=initial_status,
            image_name=f"{project_name}-agent:latest"
        )

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout='', stderr='')
            with patch('redis.Redis') as mock_redis:
                mock_redis_client = MagicMock()
                mock_redis.return_value = mock_redis_client
                mock_redis_client.scan_iter.return_value = []
                mock_redis_client.exists.return_value = False
                mock_redis_client.keys.return_value = []
                mock_redis_client.lrange.return_value = []

                with patch('services.dev_container_state.dev_container_state', dev_container_mgr), \
                     patch('services.dev_container_build_lock.dev_container_build_lock_if_free_sync',
                           _build_lock(lock_granted)):
                    execution_tracker.cleanup_stuck_in_progress_states()

        return project_name

    def test_a_busy_build_lock_skips_the_dev_container_write(
        self, execution_tracker, dev_container_mgr, temp_dirs
    ):
        """THE regression: a build/verify holding the lock owns this project's
        container state, and this stuck record must not overwrite it. Skipping
        is recoverable -- validate_task_can_run()'s staleness check on
        get_status_updated_at() picks up a status left at IN_PROGRESS."""
        project_name = self._run_cleanup_with_stuck_verifier(
            execution_tracker, dev_container_mgr,
            lock_granted=False, initial_status=DevContainerStatus.IN_PROGRESS,
        )

        assert dev_container_mgr.get_status(project_name) == DevContainerStatus.IN_PROGRESS

    def test_the_work_execution_record_is_still_reconciled_when_the_lock_is_busy(
        self, execution_tracker, dev_container_mgr, temp_dirs
    ):
        """Only the dev container write is skipped: the execution record itself
        is this module's own state and has nothing to do with the build lock."""
        self._run_cleanup_with_stuck_verifier(
            execution_tracker, dev_container_mgr,
            lock_granted=False, initial_status=DevContainerStatus.IN_PROGRESS,
        )

        last_exec = execution_tracker.get_last_execution(
            project_name="test-project",
            issue_number=42,
            column="Verification",
            agent="dev_environment_verifier",
        )
        assert last_exec['outcome'] == 'failure'

    def test_a_status_that_moved_under_the_lock_is_not_clobbered(
        self, execution_tracker, dev_container_mgr, temp_dirs
    ):
        """The re-read happens INSIDE the lock, so a VERIFIED written by a later
        execution that already succeeded survives this superseded record."""
        project_name = self._run_cleanup_with_stuck_verifier(
            execution_tracker, dev_container_mgr,
            lock_granted=True, initial_status=DevContainerStatus.VERIFIED,
        )

        assert dev_container_mgr.get_status(project_name) == DevContainerStatus.VERIFIED
