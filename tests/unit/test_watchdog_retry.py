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
import threading

# Mock ORCHESTRATOR_ROOT before importing work_execution_state to avoid /app permission errors
with tempfile.TemporaryDirectory() as _tmpdir:
    with patch.dict(os.environ, {'ORCHESTRATOR_ROOT': _tmpdir}):
        from services.work_execution_state import WorkExecutionStateTracker

from config.manager import ProjectConfig


# detect_and_retry_empty_successful_executions() only examines a record that sits
# between its two time gates: newer than _WATCHDOG_MAX_RECORD_AGE_HOURS (the age
# gate that keeps a 4700-file sweep off GitHub) and older than PROTECTION 5's
# 5-minute recency window. A hard-coded 2025-01-01 fixture is outside both, so
# every fixture below that expects the sweep to reach its protections has to be
# dated relative to now.
_EXAMINABLE_COMPLETED_AT = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
_EXAMINABLE_TIMESTAMP = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()


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
                    'completed_at': _EXAMINABLE_COMPLETED_AT,
                    'timestamp': _EXAMINABLE_TIMESTAMP
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
                    'completed_at': _EXAMINABLE_COMPLETED_AT,
                    'timestamp': _EXAMINABLE_TIMESTAMP
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
                    'completed_at': _EXAMINABLE_COMPLETED_AT,
                    'timestamp': _EXAMINABLE_TIMESTAMP
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
                    'completed_at': _EXAMINABLE_COMPLETED_AT,
                    'timestamp': _EXAMINABLE_TIMESTAMP
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
        PipelineLockManager.get_lock_holder_fail_closed() -- a pipeline board genuinely
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
                    'completed_at': _EXAMINABLE_COMPLETED_AT,
                    'timestamp': _EXAMINABLE_TIMESTAMP
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
        mock_lock_manager.get_lock_holder_fail_closed.return_value = (999, True)  # locked by a different issue

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
        mock_lock_manager.get_lock_holder_fail_closed.assert_called_once_with('test-project', 'SDLC Execution')

        with open(state_file) as f:
            updated_state = yaml.safe_load(f)
        assert updated_state['execution_history'][-1]['outcome'] == 'success'

    def test_proceeds_when_pipeline_lock_is_free(self, tracker, temp_state_dir):
        """Control case: get_lock_holder_fail_closed() reports no holder on a
        healthy read (board unlocked) -- the watchdog must proceed exactly as
        before this fix, and must not raise despite the real
        ProjectPipeline/PipelineLockManager objects now actually being called."""
        state_file = temp_state_dir / "test_project_issue_123.yaml"
        state_data = {
            'project_name': 'test-project',
            'issue_number': 123,
            'execution_history': [
                {
                    'agent': 'test-agent',
                    'column': 'In Progress',
                    'outcome': 'success',
                    'completed_at': _EXAMINABLE_COMPLETED_AT,
                    'timestamp': _EXAMINABLE_TIMESTAMP
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
        mock_lock_manager.get_lock_holder_fail_closed.return_value = (None, True)

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
        actually racing a different issue's in-progress work.
        get_lock_holder_fail_closed() returning this SAME issue_number must
        proceed exactly as if unlocked."""
        state_file = temp_state_dir / "test_project_issue_123.yaml"
        state_data = {
            'project_name': 'test-project',
            'issue_number': 123,
            'execution_history': [
                {
                    'agent': 'test-agent',
                    'column': 'In Progress',
                    'outcome': 'success',
                    'completed_at': _EXAMINABLE_COMPLETED_AT,
                    'timestamp': _EXAMINABLE_TIMESTAMP
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
        mock_lock_manager.get_lock_holder_fail_closed.return_value = (123, True)  # this SAME issue holds it

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
                    'completed_at': _EXAMINABLE_COMPLETED_AT
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
    """Test GitHub output verification.

    These tests build a REAL ProjectConfig rather than a dict (#150). The dict
    stand-in they used before is what let _has_github_output() ship a
    project_config['github']['org'] subscript against a dataclass with no
    __getitem__: production raised TypeError on every single call, the broad
    handler swallowed it, and the gate answered "no output" unconditionally --
    with a green test suite the whole time.
    """

    @pytest.fixture
    def tracker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            return WorkExecutionStateTracker(state_dir=Path(tmpdir))

    @staticmethod
    def _project_config():
        return ProjectConfig(
            name='test-project',
            description='test',
            github={'org': 'test-org', 'repo': 'test-repo'},
            tech_stacks={},
            pipelines=[],
            pipeline_routing={},
        )

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
                mock_config.return_value = self._project_config()

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
                mock_config.return_value = self._project_config()

                has_output = tracker._has_github_output('test-project', 123, execution)

                assert has_output is False

    def test_has_github_output_api_failure(self, tracker):
        """An unverifiable answer must fail CLOSED (#150).

        This is the last gate before an execution is rewritten to 'failure' and
        an agent is redispatched, and the two wrong answers are not symmetric: a
        spurious "no output" launches a container onto an issue that already has
        its comment, while a spurious "has output" only defers -- the record
        stays 'success', no retry budget is spent, and the next sweep looks
        again. It used to return False here."""
        execution = {
            'agent': 'test-agent',
            'completed_at': '2025-01-01T12:00:00Z'
        }

        mock_gh_client = MagicMock()
        mock_gh_client.rest.return_value = (False, None)  # API failure

        with patch('services.github_api_client.get_github_client', return_value=mock_gh_client):
            with patch('config.manager.config_manager.get_project_config') as mock_config:
                mock_config.return_value = self._project_config()

                has_output = tracker._has_github_output('test-project', 123, execution)

                assert has_output is True

    def test_has_github_output_programming_error_fails_closed_and_logs_loudly(
        self, tracker, caplog
    ):
        """A dataclass/dict mixup -- the exact defect that made this gate a
        permanent "no output" -- must surface at ERROR with a traceback rather
        than becoming another quiet return value, and must not redispatch."""
        execution = {
            'agent': 'test-agent',
            'completed_at': '2025-01-01T12:00:00Z'
        }

        broken_config = object()  # no .github at all

        with patch('services.github_api_client.get_github_client', return_value=MagicMock()):
            with patch('config.manager.config_manager.get_project_config') as mock_config:
                mock_config.return_value = broken_config

                with caplog.at_level(logging.ERROR, logger='services.work_execution_state'):
                    has_output = tracker._has_github_output('test-project', 123, execution)

        assert has_output is True
        assert any(
            'programming error' in record.message for record in caplog.records
        ), caplog.text

    def test_has_github_output_with_no_timestamp_at_all_fails_closed(self, tracker):
        """"Was there a comment AFTER completion?" has no answer without a
        timestamp, so it is unverifiable, not verified-empty.

        Note the record must carry NEITHER completed_at NOR timestamp: a record
        with only the start timestamp is the normal on-disk shape and is
        answerable (see _execution_anchor_time). Gating on completed_at alone is
        what made this return True for every record in production."""
        execution = {'agent': 'test-agent'}  # no completed_at, no timestamp

        with patch('services.github_api_client.get_github_client', return_value=MagicMock()):
            with patch('config.manager.config_manager.get_project_config') as mock_config:
                mock_config.return_value = self._project_config()

                assert tracker._has_github_output('test-project', 123, execution) is True


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
                        'completed_at': _EXAMINABLE_COMPLETED_AT,
                        'timestamp': _EXAMINABLE_TIMESTAMP,
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
        mock_lock_manager.get_lock_holder_fail_closed.return_value = (999, True)  # locked by a DIFFERENT issue

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
        'completed_at': _EXAMINABLE_COMPLETED_AT,
        'timestamp': _EXAMINABLE_TIMESTAMP
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
            return (999, True) if board_name == 'Planning Design' else (None, True)

        mock_lock_manager.get_lock_holder_fail_closed.side_effect = _holder
        return mock_lock_manager

    @staticmethod
    def _run(tracker, mock_lock_manager):
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
        mock_lock_manager.get_lock_holder_fail_closed.assert_called_once_with('test-project', 'SDLC Execution')
        with open(state_file) as f:
            assert yaml.safe_load(f)['execution_history'][-1]['outcome'] == 'failure'

    def test_a_lock_on_the_executions_own_board_still_skips(self, tracker, temp_state_dir):
        """Control: scoping must not make PROTECTION 2 toothless. A lock held by
        a different issue on the execution's OWN board still skips the retry."""
        state_file = _write_state(temp_state_dir, 123, board_name='Planning Design')
        mock_lock_manager = self._lock_manager_with_planning_locked()

        retried_count = self._run(tracker, mock_lock_manager)

        assert retried_count == 0
        mock_lock_manager.get_lock_holder_fail_closed.assert_called_once_with('test-project', 'Planning Design')
        with open(state_file) as f:
            assert yaml.safe_load(f)['execution_history'][-1]['outcome'] == 'success'

    def test_a_board_that_is_no_longer_configured_falls_back_to_every_board(
        self, tracker, temp_state_dir
    ):
        """A recorded board that no longer resolves (a board rename, or the
        'system' pseudo-board some task contexts carry) must NOT be trusted:
        get_lock_holder on an unknown board name is not an error, both stores
        simply have no entry, so scoping to it would make PROTECTION 2 a
        guaranteed no-op -- strictly weaker than the every-board behavior it
        replaced, not more conservative than it."""
        state_file = _write_state(temp_state_dir, 123, board_name='Renamed Away')
        mock_lock_manager = self._lock_manager_with_planning_locked()

        retried_count = self._run(tracker, mock_lock_manager)

        assert retried_count == 0
        # Fell back to the configured boards rather than the recorded one.
        checked = {
            call.args[1]
            for call in mock_lock_manager.get_lock_holder_fail_closed.call_args_list
        }
        assert 'Renamed Away' not in checked
        assert 'Planning Design' in checked
        with open(state_file) as f:
            assert yaml.safe_load(f)['execution_history'][-1]['outcome'] == 'success'

    def test_an_unverifiable_lock_read_assumes_locked(self, tracker, temp_state_dir):
        """#150: get_lock_holder() drops the health flag both stores return, and
        those stores swallow their own exceptions -- so Redis down plus an
        unreadable YAML lock file used to surface here as "no holder", identical
        to an idle board, and the execution was marked for retry onto a board
        another issue was actively holding."""
        state_file = _write_state(temp_state_dir, 123, board_name='SDLC Execution')
        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_holder_fail_closed.return_value = (None, False)

        retried_count = self._run(tracker, mock_lock_manager)

        assert retried_count == 0
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
        mock_lock_manager.get_lock_holder_fail_closed.return_value = (None, True)

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
        mock_lock_manager.get_lock_holder_fail_closed.side_effect = AttributeError(
            "'ProjectConfig' object has no attribute 'get'"
        )

        with caplog.at_level(logging.DEBUG, logger='services.work_execution_state'):
            retried_count = self._run_with_lock_manager(tracker, mock_lock_manager)

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any('PROTECTION 2' in r.getMessage() for r in errors), \
            "a coding bug in PROTECTION 2 must be logged at ERROR, not debug"
        # ...and the sweep FALLS THROUGH rather than skipping the issue. See the
        # handler's comment: a permanent coding bug must not silently freeze the
        # un-sticking watchdog, and the redispatch this invites still contends on
        # the board's pipeline lock at project_monitor.
        assert retried_count == 1

    def test_protection_2_transient_failure_logs_at_warning(self, tracker, temp_state_dir, caplog):
        _write_state(temp_state_dir, 123)
        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_holder_fail_closed.side_effect = ConnectionError("Redis unreachable")

        with caplog.at_level(logging.DEBUG, logger='services.work_execution_state'):
            retried_count = self._run_with_lock_manager(tracker, mock_lock_manager)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any('PROTECTION 2 skipped' in r.getMessage() for r in warnings)
        # Same fall-through posture as the programming-error case above.
        assert retried_count == 1
        # A transient outage is NOT a coding bug -- it must not be logged as one.
        assert not [
            r for r in caplog.records
            if r.levelno == logging.ERROR and 'PROTECTION 2' in r.getMessage()
        ]

    def test_protection_3_programming_error_logs_at_error(self, tracker, temp_state_dir, caplog):
        _write_state(temp_state_dir, 123)
        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_holder_fail_closed.return_value = (None, True)
        mock_queue_manager = MagicMock()
        mock_queue_manager.get_issue_status.side_effect = TypeError(
            "'NoneType' object is not subscriptable"
        )

        with caplog.at_level(logging.DEBUG, logger='services.work_execution_state'):
            retried_count = self._run_with_lock_manager(
                tracker, mock_lock_manager, mock_queue_manager
            )

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any('PROTECTION 3' in r.getMessage() for r in errors)
        assert retried_count == 1

    def test_protection_3_transient_failure_logs_at_warning(self, tracker, temp_state_dir, caplog):
        _write_state(temp_state_dir, 123)
        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_holder_fail_closed.return_value = (None, True)
        mock_queue_manager = MagicMock()
        mock_queue_manager.get_issue_status.side_effect = TimeoutError("queue lock timeout")

        with caplog.at_level(logging.DEBUG, logger='services.work_execution_state'):
            retried_count = self._run_with_lock_manager(
                tracker, mock_lock_manager, mock_queue_manager
            )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any('PROTECTION 3 skipped' in r.getMessage() for r in warnings)
        assert retried_count == 1

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
        mock_lock_manager.get_lock_holder_fail_closed.return_value = (None, True)
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

    @staticmethod
    def _age_out_of_the_recency_window(tracker, issue_number):
        """Backdate the record past PROTECTION 5's 5-minute window.

        record_execution_outcome() now stamps completed_at with the real clock
        (#150), so a record written a millisecond ago is by definition "too
        recent" and the sweep defers it. These tests are about board scoping, not
        about the clock.
        """
        state = tracker.load_state('test-project', issue_number)
        stamped = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        state['execution_history'][-1]['timestamp'] = stamped
        state['execution_history'][-1]['completed_at'] = stamped
        tracker.save_state('test-project', issue_number, state)

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

    def test_board_name_survives_record_execution_outcome_and_scopes_the_sweep(
        self, tracker, temp_state_dir
    ):
        """The two halves of #144's fix only meet through
        record_execution_outcome() mutating the in_progress entry in place. If
        that ever stopped carrying board_name through, both sides would still
        pass their own tests while the fix quietly stopped applying."""
        tracker.record_execution_start(
            issue_number=123,
            column='In Progress',
            agent='test-agent',
            trigger_source='manual',
            project_name='test-project',
            board_name='SDLC Execution'
        )
        tracker.record_execution_outcome(
            issue_number=123,
            column='In Progress',
            agent='test-agent',
            outcome='success',
            project_name='test-project'
        )

        last_exec = tracker.load_state('test-project', 123)['execution_history'][-1]
        assert last_exec['outcome'] == 'success'
        assert last_exec['board_name'] == 'SDLC Execution'

        self._age_out_of_the_recency_window(tracker, 123)

        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_holder_fail_closed.return_value = (None, True)
        retried_count = TestProtection2BoardScoping._run(tracker, mock_lock_manager)

        assert retried_count == 1
        mock_lock_manager.get_lock_holder_fail_closed.assert_called_once_with(
            'test-project', 'SDLC Execution'
        )

    def test_the_crash_recovery_record_has_no_board_and_gets_the_fallback(
        self, tracker, temp_state_dir
    ):
        """record_execution_outcome() with no matching in_progress entry -- the
        documented orchestrator restart/crash case -- appends a record synthesised
        from what the caller knows now, which includes no board. That record gets
        PROTECTION 2's every-board fallback, deliberately rather than
        accidentally."""
        tracker.record_execution_outcome(
            issue_number=123,
            column='In Progress',
            agent='test-agent',
            outcome='success',
            project_name='test-project'
        )

        last_exec = tracker.load_state('test-project', 123)['execution_history'][-1]
        assert last_exec['trigger_source'] == 'unknown'
        assert 'board_name' not in last_exec

        self._age_out_of_the_recency_window(tracker, 123)

        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_holder_fail_closed.return_value = (None, True)
        TestProtection2BoardScoping._run(tracker, mock_lock_manager)

        checked = [
            call.args[1]
            for call in mock_lock_manager.get_lock_holder_fail_closed.call_args_list
        ]
        assert checked == ['Planning Design', 'SDLC Execution']

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


class TestSweepReachesItsProtectionsForReal:
    """End-to-end reachability of detect_and_retry_empty_successful_executions().

    Every other test in this file replaces has_active_execution() and
    _has_github_output() with constants. That is fine for unit-testing the
    protections between them, but it is also how 599 lines of green tests were
    written over a sweep that had never completed a single pass in production
    (#150): PROTECTION 1 called has_active_execution(), which re-read the state
    file through load_state(), which re-acquired the state file's own flock on a
    fresh fd -- and blocked forever, in the same thread, with no timeout. Nothing
    after it ran, including #144's board scoping.

    These tests use the REAL protections, the REAL file locking and a real temp
    state dir, and run the sweep on a worker thread with a hard join timeout so a
    re-entrancy regression fails the test instead of hanging the suite.
    """

    SWEEP_TIMEOUT_SECONDS = 20

    @pytest.fixture
    def temp_state_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def tracker(self, temp_state_dir):
        return WorkExecutionStateTracker(state_dir=temp_state_dir)

    @staticmethod
    def _write_state(tracker, history):
        # get_state_file(), not a hand-spelled name: the deadlock only reproduces
        # when the sweep's lock path and load_state()'s lock path are the same
        # file, which is exactly what production always has and what a
        # "test_project_issue_123.yaml" stand-in for project "test-project"
        # quietly does not.
        state_file = tracker.get_state_file('test-project', 123)
        with open(state_file, 'w') as f:
            yaml.dump(
                {
                    'project_name': 'test-project',
                    'issue_number': 123,
                    'execution_history': history,
                },
                f,
            )
        return state_file

    @staticmethod
    def _success_record(**overrides):
        record = {
            'agent': 'test-agent',
            'column': 'In Progress',
            'board_name': 'SDLC Execution',
            'outcome': 'success',
            'completed_at': _EXAMINABLE_COMPLETED_AT,
            'timestamp': _EXAMINABLE_TIMESTAMP,
        }
        record.update(overrides)
        return record

    def _run_sweep(self, tracker, comments):
        """Run the sweep with only the leaves mocked, on a bounded thread."""
        pipeline_cfg = MagicMock()
        pipeline_cfg.board_name = 'SDLC Execution'
        project_config = ProjectConfig(
            name='test-project',
            description='test',
            github={'org': 'test-org', 'repo': 'test-repo'},
            tech_stacks={},
            pipelines=[pipeline_cfg],
            pipeline_routing={},
        )

        gh_client = MagicMock()
        gh_client.rest.return_value = (True, comments)

        lock_manager = MagicMock()
        lock_manager.get_lock_holder_fail_closed.return_value = (None, True)

        queue_manager = MagicMock()
        queue_manager.get_issue_status.return_value = None

        result = {}

        def sweep():
            result['count'] = tracker.detect_and_retry_empty_successful_executions()

        with patch('config.manager.config_manager') as mock_config_manager, \
             patch('services.github_api_client.get_github_client', return_value=gh_client), \
             patch(
                 'services.pipeline_lock_manager.get_pipeline_lock_manager',
                 return_value=lock_manager
             ), \
             patch(
                 'services.pipeline_queue_manager.get_pipeline_queue_manager',
                 return_value=queue_manager
             ), \
             patch.object(
                 tracker, '_should_retry_failed_execution', return_value=(True, 'eligible')
             ), \
             patch.object(
                 tracker, '_check_redis_repair_cycle_tracking', return_value=False
             ), \
             patch('services.review_cycle.review_cycle_executor') as mock_rc, \
             patch('services.human_feedback_loop.human_feedback_loop_executor') as mock_hfl:
            mock_config_manager.get_project_config.return_value = project_config
            mock_rc._cycle_key.return_value = 'test-project:123'
            mock_rc.active_cycles = {}
            mock_hfl._loop_key.return_value = 'test-project:123'
            mock_hfl.active_loops = {}

            worker = threading.Thread(target=sweep, daemon=True)
            worker.start()
            worker.join(timeout=self.SWEEP_TIMEOUT_SECONDS)

        assert not worker.is_alive(), (
            f"detect_and_retry_empty_successful_executions() did not finish within "
            f"{self.SWEEP_TIMEOUT_SECONDS}s -- it is blocked, almost certainly on a "
            f"re-entrant acquire of a state file's own lock"
        )
        return result.get('count'), gh_client

    def test_the_sweep_completes_and_marks_an_output_less_execution(
        self, tracker, temp_state_dir
    ):
        """The reachability test: with nothing stubbed out between the state file
        and GitHub, the sweep runs to completion and actually rewrites the
        record."""
        state_file = self._write_state(tracker, [self._success_record()])

        count, gh_client = self._run_sweep(tracker, comments=[])

        assert count == 1
        (method, endpoint), _ = gh_client.rest.call_args
        assert method == 'GET'
        assert endpoint.startswith('repos/test-org/test-repo/issues/123/comments?')
        assert 'since=' in endpoint

        with open(state_file) as f:
            updated = yaml.safe_load(f)
        last_exec = updated['execution_history'][-1]
        assert last_exec['outcome'] == 'failure'
        assert last_exec['watchdog_retry_triggered'] is True

    def test_a_real_github_comment_after_completion_spares_the_execution(
        self, tracker, temp_state_dir
    ):
        """The real _has_github_output() must be able to answer "yes". It could
        not before #150 -- the ProjectConfig subscript raised TypeError on every
        call, so this gate said "no output" for every project and every agent
        that had posted its comment perfectly well."""
        state_file = self._write_state(tracker, [self._success_record()])

        posted_at = (datetime.now(timezone.utc) - timedelta(minutes=29)).isoformat()
        count, _ = self._run_sweep(
            tracker,
            comments=[{'created_at': posted_at, 'body': 'Agent output'}],
        )

        assert count == 0
        with open(state_file) as f:
            updated = yaml.safe_load(f)
        assert updated['execution_history'][-1]['outcome'] == 'success'

    def test_the_real_protection_1_still_blocks_on_in_progress_work(
        self, tracker, temp_state_dir
    ):
        """PROTECTION 1 has to keep working, not merely stop hanging: a live
        in_progress record for the same issue must skip the file."""
        state_file = self._write_state(
            tracker,
            [
                {
                    'agent': 'other-agent',
                    'column': 'Code Review',
                    'outcome': 'in_progress',
                    'trigger_source': 'manual',
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                },
                self._success_record(),
            ],
        )

        count, gh_client = self._run_sweep(tracker, comments=[])

        assert count == 0
        gh_client.rest.assert_not_called()
        with open(state_file) as f:
            updated = yaml.safe_load(f)
        assert updated['execution_history'][-1]['outcome'] == 'success'


class TestCompletedAtIsActuallyRecorded:
    """completed_at has to exist on real records, not just on fixtures (#150).

    Both watchdog gates that ask "did anything happen after this execution
    finished?" -- PROTECTION 5's recency window and _has_github_output() -- key
    off completed_at, and nothing in production ever wrote it: 0 of the 4721
    state files on the live orchestrator carried the field. Every sweep-level
    test in this file fabricated it, so the drift between fixture shape and
    on-disk shape was invisible. These tests use the real writers.
    """

    @pytest.fixture
    def tracker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield WorkExecutionStateTracker(state_dir=Path(tmpdir))

    def test_the_normal_path_stamps_completed_at(self, tracker):
        tracker.record_execution_start(
            issue_number=123, column='In Progress', agent='test-agent',
            trigger_source='manual_move', project_name='test-project',
            board_name='SDLC Execution',
        )
        tracker.record_execution_outcome(
            issue_number=123, column='In Progress', agent='test-agent',
            outcome='success', project_name='test-project',
        )

        last_exec = tracker.load_state('test-project', 123)['execution_history'][-1]
        assert last_exec['outcome'] == 'success'
        assert 'completed_at' in last_exec, (
            "record_execution_outcome() must stamp completed_at -- without it "
            "_has_github_output() returns 'cannot verify' for every record ever "
            "written and the whole sweep is a no-op"
        )
        # Parseable by the same helper the watchdog uses, and after the start.
        completed = datetime.fromisoformat(last_exec['completed_at'])
        started = datetime.fromisoformat(last_exec['timestamp'])
        assert completed >= started

    def test_the_crash_recovery_record_carries_completed_at(self, tracker):
        """The synthesised record (no matching in_progress entry) is the one path
        that appends rather than mutating, so it needs its own stamp."""
        tracker.record_execution_outcome(
            issue_number=123, column='In Progress', agent='test-agent',
            outcome='success', project_name='test-project',
        )

        last_exec = tracker.load_state('test-project', 123)['execution_history'][-1]
        assert last_exec['trigger_source'] == 'unknown'
        assert last_exec['completed_at'] == last_exec['timestamp']

    def test_apply_redis_result_carries_the_blobs_completed_at(self, tracker):
        """The Redis recovery path finalises a record too, so it must leave the
        same anchor behind -- otherwise every recovered execution is one the
        watchdog can never verify."""
        execution = {
            'agent': 'test-agent', 'column': 'In Progress', 'outcome': 'in_progress',
            'timestamp': '2025-01-01T11:00:00+00:00',
        }

        applied = tracker._apply_redis_result(
            execution,
            {'exit_code': 0, 'completed_at': '2025-01-01T12:00:00+00:00'},
            'agent_result:test-project:123:task-1', 'test-project', 123,
            'test-agent', 'In Progress', MagicMock(),
        )

        assert applied is True
        assert execution['outcome'] == 'success'
        assert execution['completed_at'] == '2025-01-01T12:00:00+00:00'

    def test_apply_redis_result_falls_back_when_the_blob_has_no_completed_at(self, tracker):
        execution = {
            'agent': 'test-agent', 'column': 'In Progress', 'outcome': 'in_progress',
            'timestamp': '2025-01-01T11:00:00+00:00',
        }

        tracker._apply_redis_result(
            execution, {'exit_code': 1, 'output': 'boom'},
            'agent_result:test-project:123:task-1', 'test-project', 123,
            'test-agent', 'In Progress', MagicMock(),
        )

        assert execution['outcome'] == 'failure'
        # A real timestamp, not a missing key -- the whole point of the fallback.
        datetime.fromisoformat(execution['completed_at'])


class TestSweepOnProductionShapedRecords:
    """The sweep against records shaped exactly like the ones on disk.

    Everything else in this file patches _has_github_output() to a constant and
    feeds it a 'completed_at' no real record has. That is what let the gate ship
    returning True unconditionally for production data -- the sweep reached its
    last gate for the first time (PROTECTION 1's flock wedge having been fixed)
    and that gate declined every single record. These tests use the REAL gate and
    the REAL field set record_execution_start()/record_execution_outcome() write.
    """

    @pytest.fixture
    def temp_state_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def tracker(self, temp_state_dir):
        return WorkExecutionStateTracker(state_dir=temp_state_dir)

    @staticmethod
    def _legacy_record(**overrides):
        """A record with the exact field set state/execution_history/*.yaml holds:
        no completed_at, no watchdog_* keys, start timestamp only."""
        record = {
            'column': 'In Progress',
            'agent': 'test-agent',
            'timestamp': (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
            'outcome': 'success',
            'trigger_source': 'pipeline_progression',
            'board_name': 'SDLC Execution',
        }
        record.update(overrides)
        return record

    @staticmethod
    def _write_state(tracker, history):
        state_file = tracker.get_state_file('test-project', 123)
        with open(state_file, 'w') as f:
            yaml.dump(
                {
                    'project_name': 'test-project',
                    'issue_number': 123,
                    'execution_history': history,
                },
                f,
            )
        return state_file

    @staticmethod
    def _paging_gh_client(comments):
        """A gh client that reproduces the real endpoint's paging defaults.

        GitHubAPIClient.rest() shells out to `gh api <path>` with no --paginate
        and no per_page, and GitHub's list-issue-comments endpoint defaults to
        per_page=30 sorted created/asc. So a caller asking for the bare path gets
        the OLDEST 30 comments -- which on a long-lived issue is 30 comments from
        months before the execution it is asking about.
        """
        import urllib.parse

        def rest(method, endpoint, *args, **kwargs):
            params = dict(urllib.parse.parse_qsl(endpoint.partition('?')[2]))
            page = comments
            if params.get('since'):
                since_dt = datetime.fromisoformat(params['since'].replace('Z', '+00:00'))
                page = [
                    c for c in page
                    if datetime.fromisoformat(c['created_at'].replace('Z', '+00:00')) >= since_dt
                ]
            return True, page[:int(params.get('per_page', 30))]

        client = MagicMock()
        client.rest.side_effect = rest
        return client

    def _run_sweep(self, tracker, gh_client):
        """Run the real sweep; only PROTECTION 2/3/4's external services are stubbed."""
        pipeline_cfg = MagicMock()
        pipeline_cfg.board_name = 'SDLC Execution'
        project_config = ProjectConfig(
            name='test-project',
            description='test',
            github={'org': 'test-org', 'repo': 'test-repo'},
            tech_stacks={},
            pipelines=[pipeline_cfg],
            pipeline_routing={},
        )

        lock_manager = MagicMock()
        lock_manager.get_lock_holder_fail_closed.return_value = (None, True)
        queue_manager = MagicMock()
        queue_manager.get_issue_status.return_value = None

        with patch('config.manager.config_manager') as mock_config_manager, \
             patch('services.github_api_client.get_github_client', return_value=gh_client), \
             patch(
                 'services.pipeline_lock_manager.get_pipeline_lock_manager',
                 return_value=lock_manager
             ), \
             patch(
                 'services.pipeline_queue_manager.get_pipeline_queue_manager',
                 return_value=queue_manager
             ), \
             patch.object(
                 tracker, '_should_retry_failed_execution', return_value=(True, 'eligible')
             ), \
             patch.object(tracker, '_check_redis_repair_cycle_tracking', return_value=False), \
             patch('services.review_cycle.review_cycle_executor') as mock_rc, \
             patch('services.human_feedback_loop.human_feedback_loop_executor') as mock_hfl:
            mock_config_manager.get_project_config.return_value = project_config
            mock_rc._cycle_key.return_value = 'test-project:123'
            mock_rc.active_cycles = {}
            mock_hfl._loop_key.return_value = 'test-project:123'
            mock_hfl.active_loops = {}

            return tracker.detect_and_retry_empty_successful_executions()

    def test_a_record_with_no_completed_at_is_still_swept(self, tracker):
        """The headline regression: with the real gate and a real-shaped record,
        the sweep must reach the retry marking. It used to bail at
        _has_github_output(), which read completed_at, found None, and returned
        True ("cannot verify") for every record on disk, on every pass, forever."""
        state_file = self._write_state(tracker, [self._legacy_record()])

        count = self._run_sweep(tracker, self._paging_gh_client([]))

        assert count == 1, (
            "the sweep declined a record shaped exactly like the 4721 on disk -- "
            "_has_github_output() has no anchor to compare against"
        )
        with open(state_file) as f:
            last_exec = yaml.safe_load(f)['execution_history'][-1]
        assert last_exec['outcome'] == 'failure'
        assert last_exec['watchdog_retry_triggered'] is True

    def test_a_start_timestamp_inside_the_recency_window_defers(self, tracker):
        """PROTECTION 5's 5-minute window gated on completed_at alone, so it never
        fired for any record on disk. With the start timestamp as its fallback
        anchor, an execution that started 30 seconds ago defers."""
        state_file = self._write_state(tracker, [
            self._legacy_record(
                timestamp=(datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
            )
        ])
        gh_client = self._paging_gh_client([])

        count = self._run_sweep(tracker, gh_client)

        assert count == 0
        gh_client.rest.assert_not_called()
        with open(state_file) as f:
            assert yaml.safe_load(f)['execution_history'][-1]['outcome'] == 'success'

    def test_a_comment_beyond_the_first_page_still_counts_as_output(self, tracker):
        """The truncation regression: an issue with 178 comments whose agent
        posted #179 successfully. Asking for the bare endpoint returns comments
        1-30 -- all months old -- and the gate answers a confident "no output",
        rewriting a finished execution to 'failure' and redispatching a container
        onto an issue that already has its comment."""
        anchor = datetime.now(timezone.utc) - timedelta(minutes=30)
        old = datetime.now(timezone.utc) - timedelta(days=60)
        comments = [
            {'created_at': (old + timedelta(minutes=i)).isoformat(), 'body': 'chatter'}
            for i in range(178)
        ]
        comments.append({
            'created_at': (anchor + timedelta(seconds=5)).isoformat(),
            'body': 'Agent output',
        })

        state_file = self._write_state(
            tracker, [self._legacy_record(timestamp=anchor.isoformat())]
        )
        gh_client = self._paging_gh_client(comments)

        count = self._run_sweep(tracker, gh_client)

        assert count == 0, (
            "the gate only saw the oldest page of comments and declared the "
            "execution output-less"
        )
        endpoint = gh_client.rest.call_args[0][1]
        assert 'since=' in endpoint and 'per_page=100' in endpoint
        with open(state_file) as f:
            assert yaml.safe_load(f)['execution_history'][-1]['outcome'] == 'success'

    def test_a_record_older_than_the_age_gate_costs_nothing(self, tracker):
        """PROTECTION 0. 4570 of the 4721 live state files end in 'success' and
        97% of them are months old; before this gate every one of them reached
        PROTECTION 4's GitHub query on every 15-minute sweep."""
        state_file = self._write_state(tracker, [
            self._legacy_record(
                timestamp=(datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
            )
        ])
        gh_client = self._paging_gh_client([])

        count = self._run_sweep(tracker, gh_client)

        assert count == 0
        gh_client.rest.assert_not_called()
        with open(state_file) as f:
            assert yaml.safe_load(f)['execution_history'][-1]['outcome'] == 'success'

    def test_the_age_gate_cutoff_is_configurable(self, tracker):
        """An operator investigating a long-stuck issue can widen the window
        without a code change."""
        self._write_state(tracker, [
            self._legacy_record(
                timestamp=(datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
            )
        ])

        with patch.dict(os.environ, {'WATCHDOG_MAX_RECORD_AGE_HOURS': '2400'}):
            count = self._run_sweep(tracker, self._paging_gh_client([]))

        assert count == 1


class TestRetryEligibilityDoesNotSpendGitHubBudgetFirst:
    """_should_retry_failed_execution() used to issue its GraphQL query before
    any cheap check could reject the record (#150). The sweep calls it once per
    'success' state file, so on the live orchestrator that was up to 4570
    queries per 15-minute sweep -- ~18k/hour against GitHub's 5000/hour budget,
    which would starve board polling and comment posting for the rest of the
    hour. The Redis-local "is there an active pipeline run?" check rejects
    almost all of them, so it has to come first.
    """

    @pytest.fixture
    def tracker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield WorkExecutionStateTracker(state_dir=Path(tmpdir))

    def test_no_active_pipeline_run_costs_no_github_query(self, tracker):
        github_client = MagicMock()
        run_manager = MagicMock()
        run_manager.get_active_pipeline_run.return_value = None

        with patch('services.github_api_client.get_github_client', return_value=github_client), \
             patch('services.pipeline_run.get_pipeline_run_manager', return_value=run_manager):
            should_retry, reason = tracker._should_retry_failed_execution(
                'test-project', 123, 'test-agent', 'In Progress', {}
            )

        assert should_retry is False
        assert reason == 'no_active_pipeline_run'
        github_client.graphql.assert_not_called()

    def test_a_passed_in_project_config_is_not_re_read_from_disk(self, tracker):
        """get_project_config() re-reads and re-parses the project's YAML on every
        call, and the sweep already caches it per project."""
        project_config = ProjectConfig(
            name='test-project', description='test',
            github={'org': 'test-org', 'repo': 'test-repo'},
            tech_stacks={}, pipelines=[], pipeline_routing={},
        )
        github_client = MagicMock()
        github_client.graphql.return_value = (True, {
            'repository': {'issue': {'state': 'OPEN', 'projectItems': {'nodes': []}}}
        })
        run_manager = MagicMock()
        run_manager.get_active_pipeline_run.return_value = MagicMock(board='SDLC Execution')

        with patch('services.github_api_client.get_github_client', return_value=github_client), \
             patch('services.pipeline_run.get_pipeline_run_manager', return_value=run_manager), \
             patch('config.manager.config_manager') as mock_config_manager:
            tracker._should_retry_failed_execution(
                'test-project', 123, 'test-agent', 'In Progress', {},
                project_config=project_config,
            )

        mock_config_manager.get_project_config.assert_not_called()


class TestReentrantLockErrorReachesTheCaller:
    """A re-entrant acquire must surface, not become 'no execution history'.

    load_state()/save_state() wrap their locked body in a broad
    `except Exception` that logs and returns _empty_state(). That turns
    ReentrantFileLockError -- a programming error the guard raises specifically
    so it shows up as a traceback -- into execution_history: [], which
    has_active_execution() reads as "nothing is running". The caller then
    dispatches: a double execution of a live issue, strictly worse than the
    deadlock the guard replaced, which at least failed safe.
    """

    @pytest.fixture
    def tracker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield WorkExecutionStateTracker(state_dir=Path(tmpdir))

    @staticmethod
    def _write_live_state(tracker):
        state_file = tracker.get_state_file('test-project', 123)
        with open(state_file, 'w') as f:
            yaml.dump(
                {
                    'project_name': 'test-project',
                    'issue_number': 123,
                    'execution_history': [
                        {
                            'agent': 'test-agent',
                            'column': 'In Progress',
                            'outcome': 'in_progress',
                            'trigger_source': 'manual_move',
                            'task_id': 'task-1',
                            'timestamp': datetime.now(timezone.utc).isoformat(),
                        }
                    ],
                },
                f,
            )
        return state_file

    def test_load_state_lets_it_surface(self, tracker):
        from utils.file_lock import ReentrantFileLockError, file_lock

        state_file = self._write_live_state(tracker)
        lock_file = state_file.with_suffix(state_file.suffix + '.lock')

        with file_lock(lock_file):
            with pytest.raises(ReentrantFileLockError):
                tracker.load_state('test-project', 123)

    def test_has_active_execution_raises_rather_than_answering_false(self, tracker):
        """The consequence that actually matters: the answer would have been
        False for an issue with a live in_progress record."""
        from utils.file_lock import ReentrantFileLockError, file_lock

        state_file = self._write_live_state(tracker)
        lock_file = state_file.with_suffix(state_file.suffix + '.lock')

        with file_lock(lock_file):
            with pytest.raises(ReentrantFileLockError):
                tracker.has_active_execution('test-project', 123)

    def test_save_state_lets_it_surface_rather_than_dropping_the_write(self, tracker):
        from utils.file_lock import ReentrantFileLockError, file_lock

        state_file = self._write_live_state(tracker)
        lock_file = state_file.with_suffix(state_file.suffix + '.lock')

        with file_lock(lock_file):
            with pytest.raises(ReentrantFileLockError):
                tracker.save_state(
                    'test-project', 123,
                    {'project_name': 'test-project', 'issue_number': 123,
                     'execution_history': []},
                )
