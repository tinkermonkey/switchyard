"""
Unit tests for watchdog retry mechanism in work_execution_state.py

Tests:
- Empty output detection
- Retry eligibility checks
- Race condition protections
- GitHub output verification
"""

import logging
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

    def test_skips_when_already_waiting_in_pipeline_queue(self, tracker, temp_state_dir):
        """Issue #57 PROTECTION 3 fix: this used to import a nonexistent
        get_pipeline_queue() (only get_pipeline_queue_manager(project, board)
        exists), so the queue-status check was a silent no-op (ImportError
        swallowed by the broad except). Now that it actually calls
        get_pipeline_queue_manager(...).get_issue_status(), an issue already
        'waiting' in the queue must be skipped rather than marked failed -
        it's already about to be legitimately processed."""
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

        pipeline_cfg = MagicMock()
        pipeline_cfg.board_name = 'SDLC Execution'
        project_config = MagicMock()
        project_config.pipelines = [pipeline_cfg]

        mock_queue_manager = MagicMock()
        mock_queue_manager.get_issue_status.return_value = 'waiting'

        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.object(tracker, '_has_github_output', return_value=False):
                    with patch('utils.file_lock.file_lock'):
                        with patch('config.manager.config_manager') as mock_config_manager:
                            mock_config_manager.get_project_config.return_value = project_config
                            with patch(
                                'services.pipeline_queue_manager.get_pipeline_queue_manager',
                                return_value=mock_queue_manager
                            ):
                                retried_count = tracker.detect_and_retry_empty_successful_executions()

        assert retried_count == 0
        mock_queue_manager.get_issue_status.assert_called_once_with(123)

        with open(state_file) as f:
            updated_state = yaml.safe_load(f)
        assert updated_state['execution_history'][-1]['outcome'] == 'success'

    def test_proceeds_when_not_in_pipeline_queue(self, tracker, temp_state_dir):
        """Control case: get_issue_status() returns None (not in queue at
        all) - the watchdog must proceed exactly as before this fix."""
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

        pipeline_cfg = MagicMock()
        pipeline_cfg.board_name = 'SDLC Execution'
        project_config = MagicMock()
        project_config.pipelines = [pipeline_cfg]

        mock_queue_manager = MagicMock()
        mock_queue_manager.get_issue_status.return_value = None

        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.object(tracker, '_has_github_output', return_value=False):
                    with patch('utils.file_lock.file_lock'):
                        with patch('config.manager.config_manager') as mock_config_manager:
                            mock_config_manager.get_project_config.return_value = project_config
                            with patch(
                                'services.pipeline_queue_manager.get_pipeline_queue_manager',
                                return_value=mock_queue_manager
                            ):
                                retried_count = tracker.detect_and_retry_empty_successful_executions()

        assert retried_count == 1
        with open(state_file) as f:
            updated_state = yaml.safe_load(f)
        assert updated_state['execution_history'][-1]['outcome'] == 'failure'

    def test_skips_when_pipeline_lock_held_by_another_issue(self, tracker, temp_state_dir):
        """Issue #57 review: PROTECTION 2 previously called
        project_config.get('pipelines', {}).get('enabled', []) on a
        ProjectConfig dataclass (which has no .get() at all) and
        lock_manager.get_lock_status(...), a method that doesn't exist on
        PipelineLockManager -- both raised AttributeError on every single
        call, silently swallowed by the broad except, making this
        protection a permanent no-op (the same bug class already fixed for
        PROTECTION 3's dead get_pipeline_queue() import). Now uses the real
        ProjectPipeline.board_name attribute and
        PipelineLockManager.get_lock_holder() -- a pipeline board genuinely
        locked by a different issue must skip marking this execution for
        retry rather than racing the in-progress run."""
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

        pipeline_cfg = MagicMock()
        pipeline_cfg.board_name = 'SDLC Execution'
        project_config = MagicMock()
        project_config.pipelines = [pipeline_cfg]

        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_holder.return_value = 999  # locked by a different issue

        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.object(tracker, '_has_github_output', return_value=False):
                    with patch('utils.file_lock.file_lock'):
                        with patch('config.manager.config_manager') as mock_config_manager:
                            mock_config_manager.get_project_config.return_value = project_config
                            with patch(
                                'services.pipeline_lock_manager.get_pipeline_lock_manager',
                                return_value=mock_lock_manager
                            ):
                                retried_count = tracker.detect_and_retry_empty_successful_executions()

        assert retried_count == 0
        mock_lock_manager.get_lock_holder.assert_called_once_with('test-project', 'SDLC Execution')

        with open(state_file) as f:
            updated_state = yaml.safe_load(f)
        assert updated_state['execution_history'][-1]['outcome'] == 'success'

    def test_proceeds_when_pipeline_lock_is_free(self, tracker, temp_state_dir):
        """Control case: get_lock_holder() returns None (board unlocked) --
        the watchdog must proceed exactly as before this fix, and must not
        raise despite the real ProjectPipeline/PipelineLockManager objects
        now actually being called."""
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

        pipeline_cfg = MagicMock()
        pipeline_cfg.board_name = 'SDLC Execution'
        project_config = MagicMock()
        project_config.pipelines = [pipeline_cfg]

        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_holder.return_value = None

        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.object(tracker, '_has_github_output', return_value=False):
                    with patch('utils.file_lock.file_lock'):
                        with patch('config.manager.config_manager') as mock_config_manager:
                            mock_config_manager.get_project_config.return_value = project_config
                            with patch(
                                'services.pipeline_lock_manager.get_pipeline_lock_manager',
                                return_value=mock_lock_manager
                            ):
                                retried_count = tracker.detect_and_retry_empty_successful_executions()

        assert retried_count == 1
        with open(state_file) as f:
            updated_state = yaml.safe_load(f)
        assert updated_state['execution_history'][-1]['outcome'] == 'failure'

    def test_proceeds_when_the_issue_holds_its_own_lock(self, tracker, temp_state_dir):
        """CRITICAL regression (found in #58 review): the original PROTECTION 2
        fix skipped whenever ANYONE held the board lock, without checking
        whether the holder was this exact issue. Since an issue very often
        still holds its own lock right after finishing a stage (locks
        release only at specific exit columns, not after every stage), that
        version would have skipped almost every retry check, not just ones
        actually racing a different issue's in-progress work. get_lock_holder()
        returning this SAME issue_number must proceed exactly as if unlocked."""
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

        pipeline_cfg = MagicMock()
        pipeline_cfg.board_name = 'SDLC Execution'
        project_config = MagicMock()
        project_config.pipelines = [pipeline_cfg]

        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_holder.return_value = 123  # this SAME issue holds it

        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.object(tracker, '_has_github_output', return_value=False):
                    with patch('utils.file_lock.file_lock'):
                        with patch('config.manager.config_manager') as mock_config_manager:
                            mock_config_manager.get_project_config.return_value = project_config
                            with patch(
                                'services.pipeline_lock_manager.get_pipeline_lock_manager',
                                return_value=mock_lock_manager
                            ):
                                retried_count = tracker.detect_and_retry_empty_successful_executions()

        assert retried_count == 1
        with open(state_file) as f:
            updated_state = yaml.safe_load(f)
        assert updated_state['execution_history'][-1]['outcome'] == 'failure'

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


class TestProjectConfigCacheDoesNotPoisonOnFailure:
    """
    Regression (found in final whole-PR review, #60): the per-sweep
    project_config_cache added to share one config fetch across PROTECTION
    2/3 must NOT cache a failure. A transient error on the first state file
    for a project must not silently degrade both protections into no-ops
    for every OTHER state file of that same project in the same sweep --
    only the state file that hit the actual failure should be affected.
    """

    @pytest.fixture
    def temp_state_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def tracker(self, temp_state_dir):
        return WorkExecutionStateTracker(state_dir=temp_state_dir)

    def test_a_later_state_file_for_the_same_project_still_gets_protected_after_an_earlier_failure(
        self, tracker, temp_state_dir
    ):
        # Two state files for the SAME project -- issue #123 (processed
        # first alphabetically) and #456.
        for issue_number in (123, 456):
            state_file = temp_state_dir / f"test_project_issue_{issue_number}.yaml"
            state_data = {
                'project_name': 'test-project',
                'issue_number': issue_number,
                'execution_history': [
                    {
                        'agent': 'test-agent',
                        'column': 'In Progress',
                        'outcome': 'success',
                        'completed_at': '2025-01-01T12:00:00Z',
                        'timestamp': '2025-01-01T11:00:00Z',
                    }
                ],
            }
            with open(state_file, 'w') as f:
                yaml.dump(state_data, f)

        pipeline_cfg = MagicMock()
        pipeline_cfg.board_name = 'SDLC Execution'
        real_project_config = MagicMock()
        real_project_config.pipelines = [pipeline_cfg]

        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_holder.return_value = 999  # locked by a DIFFERENT issue

        # First call (for whichever state file is processed first) raises;
        # every subsequent call succeeds.
        mock_config_manager = MagicMock()
        mock_config_manager.get_project_config.side_effect = [
            Exception("transient config read failure"),
            real_project_config,
        ]

        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.object(tracker, '_has_github_output', return_value=False):
                    with patch('utils.file_lock.file_lock'):
                        with patch('config.manager.config_manager', mock_config_manager):
                            with patch(
                                'services.pipeline_lock_manager.get_pipeline_lock_manager',
                                return_value=mock_lock_manager,
                            ):
                                retried_count = tracker.detect_and_retry_empty_successful_executions()

        # get_project_config() must have been retried for the second file,
        # not served a poisoned None from the cache.
        assert mock_config_manager.get_project_config.call_count == 2

        # The state file whose iteration hit the actual transient failure
        # legitimately proceeds without PROTECTION 2's lock check that one
        # time (config genuinely wasn't available for it) -- exactly 1 of
        # the 2 issues gets marked for retry. What this test actually
        # guards against is the SECOND file also losing protection due to a
        # poisoned cache entry: both issues are genuinely locked by a
        # different issue (#999), so whichever file's config fetch
        # SUCCEEDED must have PROTECTION 2 correctly skip it.
        assert retried_count == 1
        outcomes = {}
        for issue_number in (123, 456):
            with open(temp_state_dir / f"test_project_issue_{issue_number}.yaml") as f:
                state = yaml.safe_load(f)
            outcomes[issue_number] = state['execution_history'][-1]['outcome']
        # Exactly one 'failure' (the file whose config fetch genuinely
        # raised) and one 'success' (the file that must have been protected
        # by a real, non-poisoned config fetch).
        assert sorted(outcomes.values()) == ['failure', 'success']


def _successful_execution_state(issue_number, board_name=None):
    """A state file body whose last execution is a bare 'success' -- the shape
    detect_and_retry_empty_successful_executions() actually inspects."""
    execution = {
        'agent': 'test-agent',
        'column': 'In Progress',
        'outcome': 'success',
        'completed_at': '2025-01-01T12:00:00Z',
        'timestamp': '2025-01-01T11:00:00Z'
    }
    if board_name:
        execution['board_name'] = board_name
    return {
        'project_name': 'test-project',
        'issue_number': issue_number,
        'execution_history': [execution]
    }


def _write_state(temp_state_dir, issue_number, board_name=None):
    state_file = temp_state_dir / f"test_project_issue_{issue_number}.yaml"
    with open(state_file, 'w') as f:
        yaml.dump(_successful_execution_state(issue_number, board_name), f)
    return state_file


def _two_board_project_config():
    """A project with the planning board FIRST, so the pre-#144 every-board loop
    hits the locked board before it ever reaches the execution's own board."""
    planning = MagicMock()
    planning.board_name = 'Planning Design'
    sdlc = MagicMock()
    sdlc.board_name = 'SDLC Execution'
    project_config = MagicMock()
    project_config.pipelines = [planning, sdlc]
    return project_config


class TestProtection2BoardScoping:
    """
    #144: PROTECTION 2 looped over EVERY board configured for the project and
    skipped the retry-eligibility check if ANY of them was locked by a different
    issue -- so an execution stuck on a completely idle board was skipped
    because an unrelated board of the same project was busy with unrelated work.
    Execution records now carry the board they ran on (record_execution_start's
    board_name), and the lock check scopes to it.
    """

    @pytest.fixture
    def temp_state_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def tracker(self, temp_state_dir):
        return WorkExecutionStateTracker(state_dir=temp_state_dir)

    @staticmethod
    def _lock_manager_with_planning_locked():
        mock_lock_manager = MagicMock()

        def _holder(project_name, board_name):
            # Only the planning board is busy, and by an unrelated issue.
            return 999 if board_name == 'Planning Design' else None

        mock_lock_manager.get_lock_holder.side_effect = _holder
        return mock_lock_manager

    def _run(self, tracker, mock_lock_manager):
        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.object(tracker, '_has_github_output', return_value=False):
                    with patch('utils.file_lock.file_lock'):
                        with patch('config.manager.config_manager') as mock_config_manager:
                            mock_config_manager.get_project_config.return_value = _two_board_project_config()
                            with patch(
                                'services.pipeline_lock_manager.get_pipeline_lock_manager',
                                return_value=mock_lock_manager
                            ):
                                return tracker.detect_and_retry_empty_successful_executions()

    def test_another_boards_lock_does_not_skip_this_executions_retry(self, tracker, temp_state_dir):
        """REGRESSION (#144): the stuck execution ran on 'SDLC Execution', which
        is unlocked. 'Planning Design' being locked by issue #999 has nothing to
        do with it, and must not skip the retry."""
        state_file = _write_state(temp_state_dir, 123, board_name='SDLC Execution')
        mock_lock_manager = self._lock_manager_with_planning_locked()

        retried_count = self._run(tracker, mock_lock_manager)

        assert retried_count == 1
        # Scoped: only the execution's own board was consulted at all.
        mock_lock_manager.get_lock_holder.assert_called_once_with('test-project', 'SDLC Execution')
        with open(state_file) as f:
            assert yaml.safe_load(f)['execution_history'][-1]['outcome'] == 'failure'

    def test_a_lock_on_the_executions_own_board_still_skips(self, tracker, temp_state_dir):
        """Control: scoping must not make PROTECTION 2 toothless. A lock held by
        a different issue on the execution's OWN board still skips the retry."""
        state_file = _write_state(temp_state_dir, 123, board_name='Planning Design')
        mock_lock_manager = self._lock_manager_with_planning_locked()

        retried_count = self._run(tracker, mock_lock_manager)

        assert retried_count == 0
        mock_lock_manager.get_lock_holder.assert_called_once_with('test-project', 'Planning Design')
        with open(state_file) as f:
            assert yaml.safe_load(f)['execution_history'][-1]['outcome'] == 'success'

    def test_records_without_a_board_keep_the_every_board_behavior(self, tracker, temp_state_dir):
        """Execution records written before board_name existed have no board to
        scope to. "Some board of this project is busy" is then the only signal
        there is, so those keep the old conservative behavior rather than
        silently losing PROTECTION 2 entirely."""
        state_file = _write_state(temp_state_dir, 123, board_name=None)
        mock_lock_manager = self._lock_manager_with_planning_locked()

        retried_count = self._run(tracker, mock_lock_manager)

        assert retried_count == 0
        with open(state_file) as f:
            assert yaml.safe_load(f)['execution_history'][-1]['outcome'] == 'success'


class TestQueueManagerCachedPerSweep:
    """
    #140 item 17: PROTECTION 3 built a brand-new PipelineQueueManager (state_dir
    mkdir included) once per board for EVERY state file examined, so an
    N-file x M-board sweep constructed N*M throwaway managers for the same M
    boards.
    """

    @pytest.fixture
    def temp_state_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def tracker(self, temp_state_dir):
        return WorkExecutionStateTracker(state_dir=temp_state_dir)

    def test_one_manager_per_board_per_sweep(self, tracker, temp_state_dir):
        _write_state(temp_state_dir, 123)
        _write_state(temp_state_dir, 456)

        pipeline_cfg = MagicMock()
        pipeline_cfg.board_name = 'SDLC Execution'
        project_config = MagicMock()
        project_config.pipelines = [pipeline_cfg]

        mock_queue_manager = MagicMock()
        mock_queue_manager.get_issue_status.return_value = None
        factory = MagicMock(return_value=mock_queue_manager)

        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_holder.return_value = None

        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.object(tracker, '_has_github_output', return_value=False):
                    with patch('utils.file_lock.file_lock'):
                        with patch('config.manager.config_manager') as mock_config_manager:
                            mock_config_manager.get_project_config.return_value = project_config
                            with patch(
                                'services.pipeline_lock_manager.get_pipeline_lock_manager',
                                return_value=mock_lock_manager
                            ):
                                with patch(
                                    'services.pipeline_queue_manager.get_pipeline_queue_manager',
                                    factory
                                ):
                                    retried_count = tracker.detect_and_retry_empty_successful_executions()

        assert retried_count == 2
        # One manager for the one board, reused across both state files...
        factory.assert_called_once_with('test-project', 'SDLC Execution')
        # ...but the queue itself is still re-read per check, never snapshotted:
        # PROTECTION 3 is a race guard and must not act on a stale view.
        assert mock_queue_manager.get_issue_status.call_count == 2


class TestProtectionFailureVisibility:
    """
    #140 item 31: PROTECTION 2/3 logged every failure at debug. That is exactly
    how an AttributeError on a dataclass and an import of a function that never
    existed both survived as permanent silent no-ops through two review rounds.
    A programming error must now surface at ERROR, distinctly from a transient
    outage at WARNING.
    """

    @pytest.fixture
    def temp_state_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def tracker(self, temp_state_dir):
        return WorkExecutionStateTracker(state_dir=temp_state_dir)

    def _run_with_lock_manager(self, tracker, mock_lock_manager, mock_queue_manager=None):
        pipeline_cfg = MagicMock()
        pipeline_cfg.board_name = 'SDLC Execution'
        project_config = MagicMock()
        project_config.pipelines = [pipeline_cfg]

        if mock_queue_manager is None:
            mock_queue_manager = MagicMock()
            mock_queue_manager.get_issue_status.return_value = None

        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.object(tracker, '_has_github_output', return_value=False):
                    with patch('utils.file_lock.file_lock'):
                        with patch('config.manager.config_manager') as mock_config_manager:
                            mock_config_manager.get_project_config.return_value = project_config
                            with patch(
                                'services.pipeline_lock_manager.get_pipeline_lock_manager',
                                return_value=mock_lock_manager
                            ):
                                with patch(
                                    'services.pipeline_queue_manager.get_pipeline_queue_manager',
                                    return_value=mock_queue_manager
                                ):
                                    return tracker.detect_and_retry_empty_successful_executions()

    def test_protection_2_programming_error_logs_at_error(self, tracker, temp_state_dir, caplog):
        _write_state(temp_state_dir, 123)
        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_holder.side_effect = AttributeError(
            "'ProjectConfig' object has no attribute 'get'"
        )

        with caplog.at_level(logging.DEBUG, logger='services.work_execution_state'):
            self._run_with_lock_manager(tracker, mock_lock_manager)

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any('PROTECTION 2' in r.getMessage() for r in errors), \
            "a coding bug in PROTECTION 2 must be logged at ERROR, not debug"

    def test_protection_2_transient_failure_logs_at_warning(self, tracker, temp_state_dir, caplog):
        _write_state(temp_state_dir, 123)
        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_holder.side_effect = ConnectionError("Redis unreachable")

        with caplog.at_level(logging.DEBUG, logger='services.work_execution_state'):
            self._run_with_lock_manager(tracker, mock_lock_manager)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any('PROTECTION 2 skipped' in r.getMessage() for r in warnings)
        # A transient outage is NOT a coding bug -- it must not be logged as one.
        assert not [
            r for r in caplog.records
            if r.levelno == logging.ERROR and 'PROTECTION 2' in r.getMessage()
        ]

    def test_protection_3_programming_error_logs_at_error(self, tracker, temp_state_dir, caplog):
        _write_state(temp_state_dir, 123)
        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_holder.return_value = None
        mock_queue_manager = MagicMock()
        mock_queue_manager.get_issue_status.side_effect = TypeError(
            "'NoneType' object is not subscriptable"
        )

        with caplog.at_level(logging.DEBUG, logger='services.work_execution_state'):
            self._run_with_lock_manager(tracker, mock_lock_manager, mock_queue_manager)

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any('PROTECTION 3' in r.getMessage() for r in errors)

    def test_protection_3_transient_failure_logs_at_warning(self, tracker, temp_state_dir, caplog):
        _write_state(temp_state_dir, 123)
        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_holder.return_value = None
        mock_queue_manager = MagicMock()
        mock_queue_manager.get_issue_status.side_effect = TimeoutError("queue lock timeout")

        with caplog.at_level(logging.DEBUG, logger='services.work_execution_state'):
            self._run_with_lock_manager(tracker, mock_lock_manager, mock_queue_manager)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any('PROTECTION 3 skipped' in r.getMessage() for r in warnings)

    def test_project_config_failure_logs_at_warning(self, tracker, temp_state_dir, caplog):
        """A config read that fails degrades BOTH protections to no-ops for that
        state file -- the same silent-degradation class, so the same visibility."""
        _write_state(temp_state_dir, 123)

        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.object(tracker, '_has_github_output', return_value=False):
                    with patch('utils.file_lock.file_lock'):
                        with patch('config.manager.config_manager') as mock_config_manager:
                            mock_config_manager.get_project_config.side_effect = Exception("config read failed")
                            with caplog.at_level(logging.DEBUG, logger='services.work_execution_state'):
                                tracker.detect_and_retry_empty_successful_executions()

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any('PROTECTION 2/3 degraded' in r.getMessage() for r in warnings)


class TestCorruptedStateFile:
    """
    A truncated/empty state file parses to None, and .setdefault() on it raised
    into load_state()'s generic handler -- producing
    "Failed to load state for rounds/#159: 'NoneType' object has no attribute
    'setdefault'" at ERROR on every load of that issue, forever, without ever
    naming the file that needed repairing.
    """

    @pytest.fixture
    def temp_state_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def tracker(self, temp_state_dir):
        return WorkExecutionStateTracker(state_dir=temp_state_dir)

    def test_empty_file_is_reported_as_corrupted_not_as_an_attributeerror(
        self, tracker, temp_state_dir, caplog
    ):
        state_file = temp_state_dir / "rounds_issue_159.yaml"
        state_file.write_text("")

        with caplog.at_level(logging.DEBUG, logger='services.work_execution_state'):
            state = tracker.load_state('rounds', 159)

        # Falls back to the same empty state a missing file gets.
        assert state['execution_history'] == []
        assert state['status_changes'] == []
        assert state['issue_number'] == 159

        messages = [r.getMessage() for r in caplog.records]
        assert any(
            'Corrupted execution state file' in m and str(state_file) in m
            for m in messages
        ), "the corrupted file must be named so it can be repaired"
        assert not any('has no attribute' in m for m in messages), \
            "must not surface as a generic AttributeError any more"

    def test_non_mapping_file_is_reported_as_corrupted(self, tracker, temp_state_dir, caplog):
        state_file = temp_state_dir / "rounds_issue_160.yaml"
        state_file.write_text("just a bare string\n")

        with caplog.at_level(logging.DEBUG, logger='services.work_execution_state'):
            state = tracker.load_state('rounds', 160)

        assert state['execution_history'] == []
        assert any(
            'Corrupted execution state file' in r.getMessage() and str(state_file) in r.getMessage()
            for r in caplog.records
        )

    def test_unparseable_yaml_is_reported_as_corrupted(self, tracker, temp_state_dir, caplog):
        state_file = temp_state_dir / "rounds_issue_161.yaml"
        state_file.write_text("execution_history: [\n  - unterminated\n")

        with caplog.at_level(logging.DEBUG, logger='services.work_execution_state'):
            state = tracker.load_state('rounds', 161)

        assert state['execution_history'] == []
        assert any(
            'Corrupted execution state file' in r.getMessage()
            for r in caplog.records
        )

    def test_sweep_skips_a_corrupted_file_by_name(self, tracker, temp_state_dir, caplog):
        """The watchdog sweep reads state files itself rather than through
        load_state(), so it needs the same treatment -- otherwise a non-mapping
        file surfaced as a generic TypeError from the loop's outer handler."""
        corrupt = temp_state_dir / "rounds_issue_162.yaml"
        corrupt.write_text("just a bare string\n")
        _write_state(temp_state_dir, 123)

        pipeline_cfg = MagicMock()
        pipeline_cfg.board_name = 'SDLC Execution'
        project_config = MagicMock()
        project_config.pipelines = [pipeline_cfg]
        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_holder.return_value = None
        mock_queue_manager = MagicMock()
        mock_queue_manager.get_issue_status.return_value = None

        with caplog.at_level(logging.DEBUG, logger='services.work_execution_state'):
            with patch.object(tracker, 'has_active_execution', return_value=False):
                with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                    with patch.object(tracker, '_has_github_output', return_value=False):
                        with patch('utils.file_lock.file_lock'):
                            with patch('config.manager.config_manager') as mock_config_manager:
                                mock_config_manager.get_project_config.return_value = project_config
                                with patch(
                                    'services.pipeline_lock_manager.get_pipeline_lock_manager',
                                    return_value=mock_lock_manager
                                ):
                                    with patch(
                                        'services.pipeline_queue_manager.get_pipeline_queue_manager',
                                        return_value=mock_queue_manager
                                    ):
                                        retried_count = tracker.detect_and_retry_empty_successful_executions()

        # The healthy file is still processed.
        assert retried_count == 1
        assert any(
            'Corrupted execution state file' in r.getMessage() and str(corrupt) in r.getMessage()
            for r in caplog.records
        )
        assert not any(
            'Error processing' in r.getMessage() for r in caplog.records
        ), "a corrupted file is a known condition, not an unhandled loop error"


class TestRecordExecutionStartBoardName:
    """record_execution_start() is the write side of #144's fix."""

    @pytest.fixture
    def temp_state_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def tracker(self, temp_state_dir):
        return WorkExecutionStateTracker(state_dir=temp_state_dir)

    def test_board_name_is_persisted_on_the_execution_record(self, tracker):
        tracker.record_execution_start(
            issue_number=123,
            column='In Progress',
            agent='test-agent',
            trigger_source='manual',
            project_name='test-project',
            board_name='SDLC Execution'
        )

        state = tracker.load_state('test-project', 123)
        assert state['execution_history'][-1]['board_name'] == 'SDLC Execution'

    def test_board_name_is_omitted_rather_than_written_as_none(self, tracker):
        """An explicit None would be indistinguishable from a board recorded as
        empty; consumers key off "absent or falsy" either way."""
        tracker.record_execution_start(
            issue_number=124,
            column='In Progress',
            agent='test-agent',
            trigger_source='manual',
            project_name='test-project'
        )

        state = tracker.load_state('test-project', 124)
        assert 'board_name' not in state['execution_history'][-1]
