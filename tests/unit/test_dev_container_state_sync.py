"""
Test that dev container state is synchronized when cleanup detects stuck verifier.

This test validates the bug fix for the issue where WorkExecutionStateTracker
and DevContainerStateManager were not synchronized when a dev_environment_verifier
container died unexpectedly.
"""

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
                with patch('services.dev_container_state.dev_container_state', dev_container_mgr):
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
                with patch('services.dev_container_state.dev_container_state', dev_container_mgr):
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
                    with patch('services.dev_container_state.dev_container_state', dev_container_mgr):
                        # Run cleanup
                        execution_tracker.cleanup_stuck_in_progress_states()

        # Verify: Dev container state should remain VERIFIED
        status = dev_container_mgr.get_status(project_name)
        assert status == DevContainerStatus.VERIFIED

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
