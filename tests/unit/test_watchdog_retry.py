"""
Unit tests for watchdog retry mechanism in work_execution_state.py

Tests:
- Empty output detection
- Retry eligibility checks
- Race condition protections
- GitHub output verification
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

import yaml
from unittest.mock import MagicMock, patch, mock_open
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

# Mock ORCHESTRATOR_ROOT before importing work_execution_state to avoid /app permission errors
with tempfile.TemporaryDirectory() as _tmpdir:
    with patch.dict(os.environ, {'ORCHESTRATOR_ROOT': _tmpdir}):
        from services.work_execution_state import WorkExecutionStateTracker


class TestEmptyOutputDetection:
    """Test detection of successful executions with no GitHub output"""

    @pytest.fixture
    def temp_state_dir(self):
        """Create temporary state directory"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def tracker(self, temp_state_dir):
        """Create WorkExecutionStateTracker with temp directory"""
        return WorkExecutionStateTracker(state_dir=temp_state_dir)

    def test_detect_successful_execution_with_no_output(self, tracker, temp_state_dir):
        """Test detects execution marked success but no GitHub output"""
        # Create state file with successful execution
        state_file = temp_state_dir / "test_project_issue_123.yaml"
        state_data = {
            'project_name': 'test-project',
            'issue_number': 123,
            'execution_history': [
                {
                    'agent': 'test-agent',
                    'column': 'In Progress',
                    'outcome': 'success',
                    'completed_at': '2025-01-01T12:00:00Z',
                    'timestamp': '2025-01-01T11:00:00Z'
                }
            ]
        }

        with open(state_file, 'w') as f:
            yaml.dump(state_data, f)

        # Mock dependencies
        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.object(tracker, '_has_github_output', return_value=False):
                    with patch('utils.file_lock.file_lock'):
                        # Run detection
                        retried_count = tracker.detect_and_retry_empty_successful_executions()

                        assert retried_count == 1

                        # Verify state was updated
                        with open(state_file) as f:
                            updated_state = yaml.safe_load(f)

                        last_exec = updated_state['execution_history'][-1]
                        assert last_exec['outcome'] == 'failure'
                        assert 'no visible GitHub output' in last_exec['error']
                        assert last_exec['watchdog_retry_triggered'] is True
                        assert last_exec['watchdog_retry_count'] == 1

    def test_ignores_execution_with_github_output(self, tracker, temp_state_dir):
        """Test ignores executions that have GitHub output"""
        state_file = temp_state_dir / "test_project_issue_123.yaml"
        state_data = {
            'project_name': 'test-project',
            'issue_number': 123,
            'execution_history': [
                {
                    'agent': 'test-agent',
                    'column': 'In Progress',
                    'outcome': 'success',
                    'completed_at': '2025-01-01T12:00:00Z',
                    'timestamp': '2025-01-01T11:00:00Z'
                }
            ]
        }

        with open(state_file, 'w') as f:
            yaml.dump(state_data, f)

        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.object(tracker, '_has_github_output', return_value=True):  # Output exists
                    with patch('utils.file_lock.file_lock'):
                        retried_count = tracker.detect_and_retry_empty_successful_executions()

                        assert retried_count == 0

                        # Verify state was NOT modified
                        with open(state_file) as f:
                            updated_state = yaml.safe_load(f)

                        last_exec = updated_state['execution_history'][-1]
                        assert last_exec['outcome'] == 'success'  # Still success

    def test_ignores_failed_executions(self, tracker, temp_state_dir):
        """Test only checks successful executions"""
        state_file = temp_state_dir / "test_project_issue_123.yaml"
        state_data = {
            'project_name': 'test-project',
            'issue_number': 123,
            'execution_history': [
                {
                    'agent': 'test-agent',
                    'column': 'In Progress',
                    'outcome': 'failure',  # Already failed
                    'completed_at': '2025-01-01T12:00:00Z',
                    'error': 'Some error'
                }
            ]
        }

        with open(state_file, 'w') as f:
            yaml.dump(state_data, f)

        with patch('utils.file_lock.file_lock'):
            retried_count = tracker.detect_and_retry_empty_successful_executions()

            assert retried_count == 0

    def test_ignores_in_progress_executions(self, tracker, temp_state_dir):
        """Test only checks completed executions"""
        state_file = temp_state_dir / "test_project_issue_123.yaml"
        state_data = {
            'project_name': 'test-project',
            'issue_number': 123,
            'execution_history': [
                {
                    'agent': 'test-agent',
                    'column': 'In Progress',
                    'outcome': 'in_progress',  # Still running
                    'timestamp': '2025-01-01T11:00:00Z'
                }
            ]
        }

        with open(state_file, 'w') as f:
            yaml.dump(state_data, f)

        with patch('utils.file_lock.file_lock'):
            retried_count = tracker.detect_and_retry_empty_successful_executions()

            assert retried_count == 0


class TestRaceConditionProtections:
    """Test race condition protections in watchdog"""

    @pytest.fixture
    def temp_state_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def tracker(self, temp_state_dir):
        return WorkExecutionStateTracker(state_dir=temp_state_dir)

    def test_skips_if_active_execution(self, tracker, temp_state_dir):
        """Test PROTECTION 1: Skips if work already in progress"""
        state_file = temp_state_dir / "test_project_issue_123.yaml"
        state_data = {
            'project_name': 'test-project',
            'issue_number': 123,
            'execution_history': [
                {
                    'agent': 'test-agent',
                    'column': 'In Progress',
                    'outcome': 'success',
                    'completed_at': '2025-01-01T12:00:00Z'
                }
            ]
        }

        with open(state_file, 'w') as f:
            yaml.dump(state_data, f)

        # Mock active execution
        with patch.object(tracker, 'has_active_execution', return_value=True):
            with patch('utils.file_lock.file_lock'):
                retried_count = tracker.detect_and_retry_empty_successful_executions()

                assert retried_count == 0

    def test_skips_if_recent_execution(self, tracker, temp_state_dir):
        """Test PROTECTION 5: Skips if execution completed recently (<5 min)"""
        state_file = temp_state_dir / "test_project_issue_123.yaml"

        # Recent completion (1 minute ago)
        recent_time = datetime.now(timezone.utc) - timedelta(minutes=1)
        state_data = {
            'project_name': 'test-project',
            'issue_number': 123,
            'execution_history': [
                {
                    'agent': 'test-agent',
                    'column': 'In Progress',
                    'outcome': 'success',
                    'completed_at': recent_time.isoformat()
                }
            ]
        }

        with open(state_file, 'w') as f:
            yaml.dump(state_data, f)

        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.object(tracker, '_has_github_output', return_value=False):
                    with patch('utils.file_lock.file_lock'):
                        retried_count = tracker.detect_and_retry_empty_successful_executions()

                        assert retried_count == 0

    def test_processes_if_old_execution(self, tracker, temp_state_dir):
        """Test processes if execution completed >5 minutes ago"""
        state_file = temp_state_dir / "test_project_issue_123.yaml"

        # Old completion (10 minutes ago)
        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        state_data = {
            'project_name': 'test-project',
            'issue_number': 123,
            'execution_history': [
                {
                    'agent': 'test-agent',
                    'column': 'In Progress',
                    'outcome': 'success',
                    'completed_at': old_time.isoformat(),
                    'timestamp': old_time.isoformat()
                }
            ]
        }

        with open(state_file, 'w') as f:
            yaml.dump(state_data, f)

        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.object(tracker, '_has_github_output', return_value=False):
                    with patch('utils.file_lock.file_lock'):
                        retried_count = tracker.detect_and_retry_empty_successful_executions()

                        assert retried_count == 1

    def test_skips_if_not_eligible(self, tracker, temp_state_dir):
        """Test PROTECTION 4: Skips if eligibility checks fail"""
        state_file = temp_state_dir / "test_project_issue_123.yaml"

        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        state_data = {
            'project_name': 'test-project',
            'issue_number': 123,
            'execution_history': [
                {
                    'agent': 'test-agent',
                    'column': 'In Progress',
                    'outcome': 'success',
                    'completed_at': old_time.isoformat(),
                    'timestamp': old_time.isoformat()
                }
            ]
        }

        with open(state_file, 'w') as f:
            yaml.dump(state_data, f)

        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(False, "not eligible")):
                with patch('utils.file_lock.file_lock'):
                    retried_count = tracker.detect_and_retry_empty_successful_executions()

                    assert retried_count == 0


class TestRetryEligibility:
    """Test retry eligibility checks"""

    @pytest.fixture
    def tracker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            return WorkExecutionStateTracker(state_dir=Path(tmpdir))

    def test_should_retry_max_retries_exceeded(self, tracker):
        """Test retry refused when max retries exceeded"""
        # Mock load_state to return execution with high retry count
        state = {
            'execution_history': [
                {
                    'agent': 'test-agent',
                    'column': 'In Progress',
                    'watchdog_retry_count': 3  # Already at max
                }
            ]
        }

        with patch.object(tracker, 'load_state', return_value=state):
            with patch.dict(os.environ, {'WATCHDOG_MAX_RETRIES': '3'}):
                should_retry, reason = tracker.should_retry_execution('test-project', 123)

                assert should_retry is False
                assert 'max_retries_exceeded' in reason

    def test_should_retry_within_limit(self, tracker):
        """Test retry allowed when within limits"""
        state = {
            'execution_history': [
                {
                    'agent': 'test-agent',
                    'column': 'In Progress',
                    'watchdog_retry_count': 1  # Still under max
                }
            ]
        }

        with patch.object(tracker, 'load_state', return_value=state):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.dict(os.environ, {'WATCHDOG_MAX_RETRIES': '3'}):
                    should_retry, reason = tracker.should_retry_execution('test-project', 123)

                    assert should_retry is True

    def test_should_retry_no_execution_state(self, tracker):
        """Test retry refused when no execution state found"""
        with patch.object(tracker, 'load_state', return_value={'execution_history': []}):
            should_retry, reason = tracker.should_retry_execution('test-project', 123)

            assert should_retry is False
            assert 'No execution state' in reason


class TestGitHubOutputVerification:
    """Test GitHub output verification"""

    @pytest.fixture
    def tracker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            return WorkExecutionStateTracker(state_dir=Path(tmpdir))

    def test_has_github_output_comment_found(self, tracker):
        """Test detects GitHub comment after execution"""
        execution = {
            'agent': 'test-agent',
            'completed_at': '2025-01-01T12:00:00Z'
        }

        # Mock GitHub client to return comment after execution
        mock_gh_client = MagicMock()
        mock_comments = [
            {
                'created_at': '2025-01-01T12:05:00Z',  # After execution
                'body': 'Agent output'
            }
        ]
        mock_gh_client.rest.return_value = (True, mock_comments)

        with patch('services.github_api_client.get_github_client', return_value=mock_gh_client):
            with patch('config.manager.config_manager.get_project_config') as mock_config:
                mock_config.return_value = {
                    'github': {'org': 'test-org', 'repo': 'test-repo'}
                }

                has_output = tracker._has_github_output('test-project', 123, execution)

                assert has_output is True

    def test_has_github_output_no_comment_found(self, tracker):
        """Test no GitHub comment after execution"""
        execution = {
            'agent': 'test-agent',
            'completed_at': '2025-01-01T12:00:00Z'
        }

        # Mock GitHub client to return no comments after execution
        mock_gh_client = MagicMock()
        mock_comments = [
            {
                'created_at': '2025-01-01T11:00:00Z',  # Before execution
                'body': 'Old comment'
            }
        ]
        mock_gh_client.rest.return_value = (True, mock_comments)

        with patch('services.github_api_client.get_github_client', return_value=mock_gh_client):
            with patch('config.manager.config_manager.get_project_config') as mock_config:
                mock_config.return_value = {
                    'github': {'org': 'test-org', 'repo': 'test-repo'}
                }

                has_output = tracker._has_github_output('test-project', 123, execution)

                assert has_output is False

    def test_has_github_output_api_failure(self, tracker):
        """Test assumes no output when API fails (safe default)"""
        execution = {
            'agent': 'test-agent',
            'completed_at': '2025-01-01T12:00:00Z'
        }

        mock_gh_client = MagicMock()
        mock_gh_client.rest.return_value = (False, None)  # API failure

        with patch('services.github_api_client.get_github_client', return_value=mock_gh_client):
            with patch('config.manager.config_manager.get_project_config') as mock_config:
                mock_config.return_value = {
                    'github': {'org': 'test-org', 'repo': 'test-repo'}
                }

                has_output = tracker._has_github_output('test-project', 123, execution)

                # Assumes no output to be safe (triggers retry)
                assert has_output is False


class TestWatchdogIncrementsRetryCount:
    """Test watchdog increments retry count"""

    @pytest.fixture
    def temp_state_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def tracker(self, temp_state_dir):
        return WorkExecutionStateTracker(state_dir=temp_state_dir)

    def test_increments_retry_count(self, tracker, temp_state_dir):
        """Test retry count is incremented"""
        state_file = temp_state_dir / "test_project_issue_123.yaml"

        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        state_data = {
            'project_name': 'test-project',
            'issue_number': 123,
            'execution_history': [
                {
                    'agent': 'test-agent',
                    'column': 'In Progress',
                    'outcome': 'success',
                    'completed_at': old_time.isoformat(),
                    'timestamp': old_time.isoformat(),
                    'watchdog_retry_count': 1  # Already retried once
                }
            ]
        }

        with open(state_file, 'w') as f:
            yaml.dump(state_data, f)

        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.object(tracker, '_has_github_output', return_value=False):
                    with patch('utils.file_lock.file_lock'):
                        retried_count = tracker.detect_and_retry_empty_successful_executions()

                        assert retried_count == 1

                        with open(state_file) as f:
                            updated_state = yaml.safe_load(f)

                        last_exec = updated_state['execution_history'][-1]
                        assert last_exec['watchdog_retry_count'] == 2  # Incremented
                        assert 'watchdog_last_retry_at' in last_exec
