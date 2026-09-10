"""
Unit tests for watchdog retry mechanism in work_execution_state.py

Tests:
- Empty output detection
- Retry eligibility checks
- Race condition protections
- GitHub output verification
"""

import contextlib
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
        from services.work_execution_state import (
            WorkExecutionStateTracker,
            _WATCHDOG_ATTRIBUTABLE_TRIGGER_SOURCES,
        )

from config.manager import ProjectConfig


# detect_and_retry_empty_successful_executions() only examines a record that sits
# between its two time gates: newer than _WATCHDOG_MAX_RECORD_AGE_HOURS (the age
# gate that keeps a 4700-file sweep off GitHub) and older than PROTECTION 5's
# 5-minute recency window. A hard-coded 2025-01-01 fixture is outside both, so
# every fixture below that expects the sweep to reach its protections has to be
# dated relative to now.
_EXAMINABLE_COMPLETED_AT = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
_EXAMINABLE_TIMESTAMP = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()


def _iso(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


# The execution start _has_github_output() anchors on throughout
# TestGitHubOutputVerification, and the `since=` bound the gate derives from it
# (seconds precision, UTC, no '+' -- a '+' in a query string decodes to a space).
_ANCHOR = _iso(minutes_ago=60)
_ANCHOR_SINCE = datetime.fromisoformat(_ANCHOR).strftime('%Y-%m-%dT%H:%M:%SZ')


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
    """_has_github_output(), the last gate before a 'success' record is rewritten
    to 'failure' and its agent redispatched.

    These tests build a REAL ProjectConfig rather than a dict (#150). The dict
    stand-in they used before is what let _has_github_output() ship a
    project_config['github']['org'] subscript against a dataclass with no
    __getitem__: production raised TypeError on every single call, the broad
    handler swallowed it, and the gate answered "no output" unconditionally --
    with a green test suite the whole time.

    The gate went live in #166, so every case below is now a real answer rather
    than the "cannot verify" the whole class used to collapse to. The three
    defects that had to close first each have their own section: it never looked
    at Discussions, it had no honest start time to anchor against, and it counted
    any comment in the window as the agent's own work.
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

    @staticmethod
    def _execution(**overrides):
        """The shape record_execution_start() writes: a start timestamp, a real
        trigger_source, and no completion time at all.

        'task_queue' because that is what the ordinary agent dispatch's last
        record actually carries -- the task-queue worker records a second start
        that supersedes project_monitor's 'manual' one -- and because it is one of
        the dispatch paths whose output this gate can attribute at all.
        """
        execution = {
            'agent': 'test-agent',
            'column': 'In Progress',
            'outcome': 'success',
            'timestamp': _ANCHOR,
            'trigger_source': 'task_queue',
        }
        execution.update(overrides)
        return execution

    @staticmethod
    def _agent_comment(created_at, agent='test-agent'):
        return {
            'created_at': created_at,
            'body': f"# Analysis\n\nbody\n\n---\n_Processed by the {agent} agent_",
        }

    @staticmethod
    def _discussion_payload(comments):
        """A GraphQL `data` payload for the gate's Discussion query.

        `comments` is a list of (created_at, body, replies) triples, oldest first
        -- the order GitHub returns a `last:` page in.
        """
        return {
            'node': {
                'comments': {
                    'totalCount': len(comments),
                    'nodes': [
                        {
                            'createdAt': created_at,
                            'body': body,
                            'replies': {
                                'totalCount': len(replies),
                                'nodes': [
                                    {'createdAt': r_created, 'body': r_body}
                                    for r_created, r_body in replies
                                ],
                            },
                        }
                        for created_at, body, replies in comments
                    ],
                }
            }
        }

    @contextlib.contextmanager
    def _gate_environment(
        self, gh_client, workspace_type='issues', discussion_id=None,
        project_config=None,
    ):
        """Everything _has_github_output() reaches outside itself."""
        state_manager = MagicMock()
        state_manager.get_discussion_for_issue.return_value = discussion_id

        with patch('services.github_api_client.get_github_client', return_value=gh_client), \
             patch('config.manager.config_manager.get_project_config') as mock_config, \
             patch(
                 'claude.docker_runner.resolve_workspace_type_for_column',
                 return_value=workspace_type
             ), \
             patch('config.state_manager.state_manager', state_manager):
            mock_config.return_value = (
                self._project_config() if project_config is None else project_config
            )
            yield state_manager

    # -- the anchor -------------------------------------------------------

    def test_the_anchor_is_the_start_not_the_completion(self, tracker):
        """The 29-of-30 bug. Both completion paths post the agent's comment BEFORE
        calling record_execution_outcome(), so a comment that proves output sits
        BETWEEN the start and the completion. Anchoring on the completion answers
        "nothing posted since" for exactly the executions that worked."""
        posted_at = _iso(minutes_ago=50)  # after the start, before the completion
        execution = self._execution(completed_at=_iso(minutes_ago=40))

        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [self._agent_comment(posted_at)])

        with self._gate_environment(gh_client):
            assert tracker._has_github_output('test-project', 123, execution) is True

        (_, endpoint), _ = gh_client.rest.call_args
        assert f"since={_ANCHOR_SINCE}" in endpoint, (
            "the gate filtered from a time other than the execution's start"
        )

    def test_a_crash_recovery_record_is_never_verified(self, tracker):
        """record_execution_outcome() stamps `timestamp` at outcome-recording time
        when it finds no in_progress entry, i.e. AFTER the agent already posted.
        That record has no start to anchor against and must stay unverifiable --
        8,692 of 54,594 live 'success' records have this shape."""
        execution = self._execution(trigger_source='unknown', start_time_unknown=True)

        gh_client = MagicMock()
        with self._gate_environment(gh_client):
            assert tracker._has_github_output('test-project', 123, execution) is True

        gh_client.rest.assert_not_called()

    def test_a_legacy_crash_recovery_record_is_never_verified(self, tracker):
        """The same shape as it exists on disk today: written before
        start_time_unknown was stamped, so trigger_source is the only marker."""
        execution = self._execution(trigger_source='unknown')

        gh_client = MagicMock()
        with self._gate_environment(gh_client):
            assert tracker._has_github_output('test-project', 123, execution) is True

        gh_client.rest.assert_not_called()

    def test_a_record_with_no_trigger_source_is_never_verified(self, tracker):
        """A record that names no dispatch path names no start either."""
        execution = self._execution()
        del execution['trigger_source']

        gh_client = MagicMock()
        with self._gate_environment(gh_client):
            assert tracker._has_github_output('test-project', 123, execution) is True

        gh_client.rest.assert_not_called()

    def test_an_undatable_start_is_never_verified(self, tracker):
        execution = self._execution(timestamp='not a timestamp')

        gh_client = MagicMock()
        with self._gate_environment(gh_client):
            assert tracker._has_github_output('test-project', 123, execution) is True

        gh_client.rest.assert_not_called()

    def test_a_record_with_no_agent_is_never_verified(self, tracker):
        execution = self._execution(agent=None)

        gh_client = MagicMock()
        with self._gate_environment(gh_client):
            assert tracker._has_github_output('test-project', 123, execution) is True

        gh_client.rest.assert_not_called()

    # -- attributable dispatch paths --------------------------------------

    @pytest.mark.parametrize('trigger_source', [
        'repair_cycle_test', 'repair_cycle_fix', 'repair_cycle_warning_review',
    ])
    def test_a_repair_cycle_record_is_never_verified(self, tracker, trigger_source):
        """The measured false-positive class (#166). A repair cycle's agent calls
        run inside the repair-cycle container and post nothing individually --
        documentation_robotics #909 recorded 23 agent calls and not one signed
        comment -- so the cycle's own summary, signed "_Repair cycle executed by
        Switchyard (containerized)_", is the whole of its GitHub output.

        Evaluating the activated gate over seven days of live records without this
        exclusion answered "no output" for 83 of 96, every one of them a repair
        cycle that had demonstrably posted its summary. That is 2,053 of 4,566
        last-record successes, and it is the same shape of wrong answer that got
        activation split out of #150 in the first place."""
        execution = self._execution(trigger_source=trigger_source, column='Testing')

        gh_client = MagicMock()
        with self._gate_environment(gh_client):
            assert tracker._has_github_output('test-project', 123, execution) is True

        gh_client.rest.assert_not_called()

    def test_a_manual_dispatch_record_is_never_verified(self, tracker):
        """project_monitor uses 'manual' for the ordinary board dispatch AND for
        the two wrapper stages (pr_review_stage, repair cycle), which record an
        outcome under a name their sub-run does not post under. The ordinary
        dispatch's own last record is 'task_queue', so declining the ambiguous
        name costs nothing verifiable."""
        gh_client = MagicMock()
        with self._gate_environment(gh_client):
            assert tracker._has_github_output(
                'test-project', 123, self._execution(trigger_source='manual')
            ) is True

        gh_client.rest.assert_not_called()

    def test_an_unrecognised_dispatch_path_is_never_verified(self, tracker):
        """The allowlist is an allowlist so that a dispatch path added later
        defaults to "cannot verify" -- which defers -- rather than to "verified
        empty", which redispatches."""
        gh_client = MagicMock()
        with self._gate_environment(gh_client):
            assert tracker._has_github_output(
                'test-project', 123, self._execution(trigger_source='some_new_path')
            ) is True

        gh_client.rest.assert_not_called()

    @pytest.mark.parametrize('trigger_source', sorted(
        _WATCHDOG_ATTRIBUTABLE_TRIGGER_SOURCES
    ))
    def test_every_attributable_dispatch_path_is_actually_verified(
        self, tracker, trigger_source
    ):
        """The other side of the allowlist: each path on it does reach GitHub and
        does answer for real, so the list cannot quietly become a way of turning
        the whole gate back off."""
        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [])

        with self._gate_environment(gh_client):
            assert tracker._has_github_output(
                'test-project', 123, self._execution(trigger_source=trigger_source)
            ) is False

        gh_client.rest.assert_called_once()

    # -- authorship -------------------------------------------------------

    def test_the_agents_own_comment_is_output(self, tracker):
        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [self._agent_comment(_iso(minutes_ago=50))])

        with self._gate_environment(gh_client):
            assert tracker._has_github_output(
                'test-project', 123, self._execution()
            ) is True

    def test_a_comment_that_is_not_agent_output_does_not_count(self, tracker):
        """The gate used to count ANY comment in the window as the agent's, which
        made the pipeline watchdog's own "Pipeline Stuck" notice -- posted onto
        precisely the stuck issues this sweep exists to un-stick -- read as proof
        the agent had worked."""
        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [
            {'created_at': _iso(minutes_ago=50), 'body': '## Pipeline Stuck\n\nNo activity.'},
            {'created_at': _iso(minutes_ago=45), 'body': 'Any update on this?'},
        ])

        with self._gate_environment(gh_client):
            assert tracker._has_github_output(
                'test-project', 123, self._execution()
            ) is False

    def test_another_agents_output_is_ambiguous_not_empty(self, tracker):
        """Some agent posted inside this window, just not under this record's
        name. The wrapper stages record an outcome under a name their sub-run does
        not post under, so this is a genuine "don't know" -- and a "don't know"
        leaves the record alone."""
        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [
            self._agent_comment(_iso(minutes_ago=50), agent='some-other-agent')
        ])

        with self._gate_environment(gh_client):
            assert tracker._has_github_output(
                'test-project', 123, self._execution()
            ) is True

    def test_the_agents_own_comment_from_before_the_start_does_not_count(self, tracker):
        """A previous run of the same agent in the same column is not this
        execution's output."""
        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [
            self._agent_comment(_iso(minutes_ago=600))
        ])

        with self._gate_environment(gh_client):
            assert tracker._has_github_output(
                'test-project', 123, self._execution()
            ) is False

    def test_no_comments_at_all_is_verified_empty(self, tracker):
        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [])

        with self._gate_environment(gh_client):
            assert tracker._has_github_output(
                'test-project', 123, self._execution()
            ) is False

    # -- Discussions ------------------------------------------------------

    def test_output_posted_to_a_discussion_is_output(self, tracker):
        """The confirmed live false positive: phone-home #72, agent
        idea_researcher, column Research. Its report went to Discussion #191 three
        minutes after the execution started, and a gate that only queried
        repos/{org}/{repo}/issues/72/comments answered "demonstrably produced no
        output" -- which rewrites the record and redispatches the agent. 3,266 of
        54,594 'success' records sit in discussion-workspace columns."""
        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [])  # nothing on the issue itself
        gh_client.graphql.return_value = (True, self._discussion_payload([
            (
                _iso(minutes_ago=50),
                '# Idea Research\n\n---\n_Processed by the test-agent agent_',
                [],
            ),
        ]))

        with self._gate_environment(
            gh_client, workspace_type='discussions', discussion_id='D_kwDO123'
        ):
            assert tracker._has_github_output(
                'test-project', 123, self._execution(column='Research')
            ) is True

        assert gh_client.graphql.called

    def test_output_posted_as_a_threaded_discussion_reply_is_output(self, tracker):
        """A human_feedback_loop response is posted with reply_to_comment_id, so
        it lands as a reply rather than a top-level discussion comment."""
        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [])
        gh_client.graphql.return_value = (True, self._discussion_payload([
            (
                _iso(minutes_ago=600),
                'A human question',
                [(
                    _iso(minutes_ago=50),
                    '# Idea Research\n\n---\n_Processed by the test-agent agent_',
                )],
            ),
        ]))

        with self._gate_environment(
            gh_client, workspace_type='discussions', discussion_id='D_kwDO123'
        ):
            assert tracker._has_github_output(
                'test-project', 123, self._execution(column='Research')
            ) is True

    def test_an_empty_discussion_and_an_empty_issue_is_verified_empty(self, tracker):
        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [])
        gh_client.graphql.return_value = (True, self._discussion_payload([]))

        with self._gate_environment(
            gh_client, workspace_type='discussions', discussion_id='D_kwDO123'
        ):
            assert tracker._has_github_output(
                'test-project', 123, self._execution(column='Research')
            ) is False

    def test_a_discussion_workspace_with_no_recorded_discussion_is_unverifiable(
        self, tracker
    ):
        """post_agent_output() falls back to an issue comment when it has no
        discussion id -- but only if the link was also missing when the agent
        posted. unlink_issue_discussion() exists, so a link removed since would
        leave the output somewhere this gate can no longer look."""
        gh_client = MagicMock()

        with self._gate_environment(
            gh_client, workspace_type='discussions', discussion_id=None
        ):
            assert tracker._has_github_output(
                'test-project', 123, self._execution(column='Research')
            ) is True

        gh_client.rest.assert_not_called()

    def test_a_failed_discussion_query_fails_closed(self, tracker):
        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [])
        gh_client.graphql.return_value = (False, {'error': 'rate_limited'})

        with self._gate_environment(
            gh_client, workspace_type='discussions', discussion_id='D_kwDO123'
        ):
            assert tracker._has_github_output(
                'test-project', 123, self._execution(column='Research')
            ) is True

    def test_a_discussion_is_checked_even_in_an_issues_column(self, tracker):
        """workspace_type and discussion_id are independent, and
        post_agent_output() routes on discussion_id presence BEFORE it consults
        workspace_type. Checking only the workspace the column names would miss
        exactly the mismatch that routing order creates."""
        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [])
        gh_client.graphql.return_value = (True, self._discussion_payload([
            (_iso(minutes_ago=50), '_Processed by the test-agent agent_', []),
        ]))

        with self._gate_environment(
            gh_client, workspace_type='issues', discussion_id='D_kwDO123'
        ):
            assert tracker._has_github_output(
                'test-project', 123, self._execution()
            ) is True

    def test_a_truncated_discussion_page_is_unverifiable(self, tracker):
        """`last: 100` drops the OLDEST comments, so the page covers the window
        only when its own first node already predates the anchor. A page whose
        oldest node is newer than the anchor may have dropped the agent's."""
        payload = self._discussion_payload([
            (_iso(minutes_ago=50), 'a later human comment', []),
        ])
        payload['node']['comments']['totalCount'] = 500

        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [])
        gh_client.graphql.return_value = (True, payload)

        with self._gate_environment(
            gh_client, workspace_type='discussions', discussion_id='D_kwDO123'
        ):
            assert tracker._has_github_output(
                'test-project', 123, self._execution(column='Research')
            ) is True

    def test_a_discussion_page_that_reaches_past_the_anchor_is_enough(self, tracker):
        """The same truncation, but the page's oldest node predates the execution
        -- everything dropped is older still, so nothing in the window is missing
        and the gate may answer for real."""
        payload = self._discussion_payload([
            (_iso(minutes_ago=600), 'an older human comment', []),
        ])
        payload['node']['comments']['totalCount'] = 500

        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [])
        gh_client.graphql.return_value = (True, payload)

        with self._gate_environment(
            gh_client, workspace_type='discussions', discussion_id='D_kwDO123'
        ):
            assert tracker._has_github_output(
                'test-project', 123, self._execution(column='Research')
            ) is False

    # -- the bounded fetch ------------------------------------------------

    def test_the_issue_comment_fetch_is_bounded_and_anchored(self, tracker):
        """`gh api` is called with no --paginate and the list-comments endpoint
        defaults to per_page=30 sorted created/ascending, so the bare path
        returned the OLDEST 30 comments -- every one of which predates the
        execution on an issue with any history at all."""
        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [])

        with self._gate_environment(gh_client):
            tracker._has_github_output('test-project', 123, self._execution())

        (method, endpoint), _ = gh_client.rest.call_args
        assert method == 'GET'
        assert endpoint.startswith('repos/test-org/test-repo/issues/123/comments?')
        assert 'per_page=100' in endpoint
        assert f"since={_ANCHOR_SINCE}" in endpoint

    def test_a_full_page_of_comments_is_unverifiable(self, tracker):
        """A full page is indistinguishable from a clipped one, and the page holds
        the OLDEST comments in the window -- the agent's could be past it."""
        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [
            {'created_at': _iso(minutes_ago=50), 'body': 'chatter'}
            for _ in range(100)
        ])

        with self._gate_environment(gh_client):
            assert tracker._has_github_output(
                'test-project', 123, self._execution()
            ) is True

    # -- fail-closed on everything it cannot answer ------------------------

    def test_has_github_output_api_failure(self, tracker):
        """An unverifiable answer must fail CLOSED (#150).

        This is the last gate before an execution is rewritten to 'failure' and
        an agent is redispatched, and the two wrong answers are not symmetric: a
        spurious "no output" launches a container onto an issue that already has
        its comment, while a spurious "has output" only defers -- the record
        stays 'success', no retry budget is spent, and the next sweep looks
        again. It used to return False here."""
        gh_client = MagicMock()
        gh_client.rest.return_value = (False, None)  # API failure

        with self._gate_environment(gh_client):
            assert tracker._has_github_output(
                'test-project', 123, self._execution()
            ) is True

    def test_has_github_output_non_list_response_fails_closed(self, tracker):
        """rest() hands back whatever the body decoded to. A dict (an error
        envelope) or a string iterates into something the comment scan silently
        skips, ending in a confident "no output" derived from a body that was
        never a comment list."""
        gh_client = MagicMock()
        gh_client.rest.return_value = (True, {'message': 'Not Found'})

        with self._gate_environment(gh_client):
            assert tracker._has_github_output(
                'test-project', 123, self._execution()
            ) is True

    def test_an_undatable_comment_fails_closed(self, tracker):
        """Skipping a comment this cannot place relative to the anchor is the
        assumption that redispatches an agent whose output is sitting right
        there."""
        gh_client = MagicMock()
        gh_client.rest.return_value = (True, [
            {'created_at': None, 'body': 'something'}
        ])

        with self._gate_environment(gh_client):
            assert tracker._has_github_output(
                'test-project', 123, self._execution()
            ) is True

    def test_has_github_output_programming_error_fails_closed_and_logs_loudly(
        self, tracker, caplog
    ):
        """A dataclass/dict mixup -- the exact defect that made this gate a
        permanent "no output" -- must surface at ERROR with a traceback rather
        than becoming another quiet return value, and must not redispatch."""
        broken_config = object()  # no .github at all

        with self._gate_environment(MagicMock(), project_config=broken_config):
            with caplog.at_level(logging.ERROR, logger='services.work_execution_state'):
                has_output = tracker._has_github_output(
                    'test-project', 123, self._execution()
                )

        assert has_output is True
        assert any(
            'programming error' in record.message for record in caplog.records
        ), caplog.text


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
    def _backdate_the_record(tracker, issue_number):
        """Backdate the record written by the real writers a little.

        Keeps the on-disk shape production actually has -- a start timestamp and
        no completed_at -- while putting the record far enough in the past that
        nothing time-based interferes with what these tests are about, which is
        board scoping.
        """
        state = tracker.load_state('test-project', issue_number)
        state['execution_history'][-1]['timestamp'] = (
            datetime.now(timezone.utc) - timedelta(minutes=30)
        ).isoformat()
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

        self._backdate_the_record(tracker, 123)

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

        self._backdate_the_record(tracker, 123)

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
        """A record in exactly the shape record_execution_start() writes.

        It used to carry a synthetic completed_at, because the gate keyed off a
        field no production writer stamps and these tests could not otherwise
        tell "the sweep ran to completion" from "the sweep wedged". Since #166 the
        gate anchors on `timestamp` -- the start -- so the real shape reaches the
        last gate and past it, and the fixture no longer has to invent anything.
        """
        record = {
            'agent': 'test-agent',
            'column': 'In Progress',
            'board_name': 'SDLC Execution',
            'outcome': 'success',
            # One of the dispatch paths whose output the gate can attribute --
            # see _WATCHDOG_ATTRIBUTABLE_TRIGGER_SOURCES. A record the gate
            # declines outright never reaches the last gate at all, which is what
            # these reachability tests exist to measure.
            'trigger_source': 'task_queue',
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
            comments=[{
                'created_at': posted_at,
                'body': 'Agent output\n\n---\n_Processed by the test-agent agent_',
            }],
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


class TestCompletedAtIsNotStampedTheAnchorIsTheStart:
    """No production writer stamps completed_at, and that stayed true through #166.

    It was load-bearing for a different reason before: the gate keyed off
    completed_at, so its absence was the only thing keeping the watchdog off
    54,594 'success' records. #166 activated the gate on the START timestamp
    instead -- because both completion paths post the agent's comment BEFORE
    recording the outcome, so a completion anchor post-dates the very comment
    that proves output, which is what a dry run measured as 29 wrong answers in
    30 real successes.

    So the field stays unwritten, and these tests keep pinning that, now guarding
    the one consumer left: PROTECTION 5's 5-minute recency window. Stamping it is
    a one-line change in each of these three writers and would wake that window
    across production the moment anyone made it.
    """

    @pytest.fixture
    def tracker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield WorkExecutionStateTracker(state_dir=Path(tmpdir))

    def test_the_normal_path_does_not_stamp_completed_at(self, tracker):
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
        assert 'completed_at' not in last_exec, (
            "stamping completed_at wakes PROTECTION 5's recency window across "
            "every 'success' record in production -- see the class docstring"
        )
        # The record the gate DOES anchor on: a start time, and a trigger_source
        # that says the start is real.
        assert last_exec['timestamp']
        assert last_exec['trigger_source'] == 'manual_move'
        assert 'start_time_unknown' not in last_exec

    def test_the_crash_recovery_record_declares_it_has_no_start_time(self, tracker):
        """The synthesised record (no matching in_progress entry) is the one path
        that appends rather than mutating, and the one whose `timestamp` is not a
        real start time at all -- it is stamped at outcome-recording time, after
        the agent has already posted. Anchoring the empty-output gate on it asks
        "has anything been posted since?" of an instant that is really the finish,
        and answers "no output" for an execution that posted perfectly well:
        8,692 of 54,594 live 'success' records (15.9%) have this shape.

        The record therefore says so outright (#166) rather than leaving the gate
        to infer it from a trigger_source that means something else."""
        tracker.record_execution_outcome(
            issue_number=123, column='In Progress', agent='test-agent',
            outcome='success', project_name='test-project',
        )

        last_exec = tracker.load_state('test-project', 123)['execution_history'][-1]
        assert last_exec['trigger_source'] == 'unknown'
        assert last_exec['start_time_unknown'] is True, (
            "the crash-recovery record's timestamp is its finish time -- without "
            "this flag the empty-output gate treats it as a start and redispatches "
            "agents that already posted"
        )
        assert 'completed_at' not in last_exec
        assert tracker._output_anchor_for_record(last_exec) is None

    def test_apply_redis_result_does_not_stamp_completed_at(self, tracker):
        """The Redis recovery path finalises a record too, so it is the third
        place a stamp would leak in."""
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
        assert 'completed_at' not in execution


class TestSweepOnProductionShapedRecords:
    """The sweep against records shaped exactly like the ones on disk.

    Most of this file patches _has_github_output() to a constant, which is fine
    for unit-testing the protections in front of it and useless for the question
    that actually decides whether this watchdog is safe to run: what does the
    whole chain do to a record with the field set production really writes?

    Before #166 the answer was "nothing, ever" -- the gate keyed off a
    completed_at no writer stamps, so all 54,594 'success' records answered
    "cannot verify". These tests now pin the activated behaviour end to end: a
    record whose agent genuinely posted nothing is rewritten, and every shape the
    gate cannot honestly settle -- a Discussion carrying the output, a
    crash-recovery record with no real start time -- is still left alone.
    """

    @pytest.fixture
    def temp_state_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def tracker(self, temp_state_dir):
        return WorkExecutionStateTracker(state_dir=temp_state_dir)

    @staticmethod
    def _production_record(**overrides):
        """A record with the exact field set state/execution_history/*.yaml holds:
        no completed_at, no watchdog_* keys, start timestamp only."""
        record = {
            'column': 'In Progress',
            'agent': 'test-agent',
            'timestamp': (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat(),
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
    def _gh_client(comments, discussion_comments=None):
        client = MagicMock()
        client.rest.return_value = (True, comments)
        client.graphql.return_value = (True, {
            'node': {
                'comments': {
                    'totalCount': len(discussion_comments or []),
                    'nodes': [
                        {
                            'createdAt': created_at,
                            'body': body,
                            'replies': {'totalCount': 0, 'nodes': []},
                        }
                        for created_at, body in (discussion_comments or [])
                    ],
                }
            }
        })
        return client

    def _run_sweep(
        self, tracker, gh_client, has_github_output=None,
        workspace_type='issues', discussion_id=None,
    ):
        """Run the real sweep; only PROTECTION 2/3/4's external services are stubbed.

        has_github_output stays None -- the REAL gate -- unless a test is about a
        protection that sits in front of it and needs the sweep to be able to
        reach the retry marking at all.

        workspace_type/discussion_id are the two inputs the gate resolves from the
        pipeline config and the GitHub state file; they are stubbed here for the
        same reason the lock and queue managers are.
        """
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

        state_manager = MagicMock()
        state_manager.get_discussion_for_issue.return_value = discussion_id

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
             patch(
                 'claude.docker_runner.resolve_workspace_type_for_column',
                 return_value=workspace_type
             ), \
             patch('config.state_manager.state_manager', state_manager), \
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

            if has_github_output is None:
                return tracker.detect_and_retry_empty_successful_executions()
            with patch.object(
                tracker, '_has_github_output', return_value=has_github_output
            ):
                return tracker.detect_and_retry_empty_successful_executions()

    def test_a_production_shaped_record_with_no_output_is_rewritten(self, tracker):
        """The activation itself (#166): with the real gate and a record shaped
        exactly like the ones on the live orchestrator, an execution that posted
        nothing anywhere is rewritten to 'failure' so project_monitor redispatches
        it. Before #166 this answered "cannot verify" for every record that has
        ever existed."""
        state_file = self._write_state(tracker, [self._production_record()])
        gh_client = self._gh_client([])

        count = self._run_sweep(tracker, gh_client)

        assert count == 1
        (method, endpoint), _ = gh_client.rest.call_args
        assert method == 'GET'
        assert 'per_page=100' in endpoint and 'since=' in endpoint
        with open(state_file) as f:
            last_exec = yaml.safe_load(f)['execution_history'][-1]
        assert last_exec['outcome'] == 'failure'
        assert last_exec['watchdog_retry_triggered'] is True

    def test_a_production_shaped_record_with_its_comment_is_left_alone(self, tracker):
        """The other half of the same activation, and the one that matters: an
        agent that did post must not be redispatched onto its own work."""
        state_file = self._write_state(tracker, [self._production_record()])
        gh_client = self._gh_client([{
            'created_at': (datetime.now(timezone.utc) - timedelta(minutes=80)).isoformat(),
            'body': '# Implementation\n\n---\n_Processed by the test-agent agent_',
        }])

        count = self._run_sweep(tracker, gh_client)

        assert count == 0
        with open(state_file) as f:
            last_exec = yaml.safe_load(f)['execution_history'][-1]
        assert last_exec['outcome'] == 'success'
        assert 'watchdog_retry_triggered' not in last_exec

    def test_a_record_whose_output_went_to_a_discussion_is_left_alone(self, tracker):
        """The confirmed live false positive, end to end: phone-home #72's
        idea_researcher posted its report to Discussion #191 and the pre-#166 gate
        -- which queried only the issue-comments endpoint -- answered
        "demonstrably produced no output". 3,266 of 54,594 'success' records sit
        in discussion-workspace columns, so this is 6% of the corpus, not an edge
        case."""
        state_file = self._write_state(tracker, [
            self._production_record(column='Research', agent='idea_researcher')
        ])
        gh_client = self._gh_client(
            comments=[],
            discussion_comments=[(
                (datetime.now(timezone.utc) - timedelta(minutes=85)).isoformat(),
                '# Idea Research\n\n---\n_Processed by the idea_researcher agent_',
            )],
        )

        count = self._run_sweep(
            tracker, gh_client, workspace_type='discussions', discussion_id='D_kwDO191'
        )

        assert count == 0, (
            "the sweep rewrote a record whose agent posted its report to the "
            "issue's Discussion -- the gate is only looking at issue comments again"
        )
        with open(state_file) as f:
            assert yaml.safe_load(f)['execution_history'][-1]['outcome'] == 'success'

    def test_the_crash_recovery_shape_is_never_rewritten(self, tracker):
        """15.9% of live 'success' records (8,692 of 54,594) are the
        trigger_source: 'unknown' shape record_execution_outcome() synthesises
        when it finds no matching in_progress entry. Its `timestamp` is stamped
        at outcome-recording time, i.e. AFTER the agent posted, so it is not a
        start time and cannot anchor a "has anything been posted since?"
        question. It must stay unverifiable too."""
        tracker.record_execution_outcome(
            issue_number=123, column='In Progress', agent='test-agent',
            outcome='success', project_name='test-project',
        )
        state = tracker.load_state('test-project', 123)
        state['execution_history'][-1]['timestamp'] = (
            datetime.now(timezone.utc) - timedelta(minutes=90)
        ).isoformat()
        tracker.save_state('test-project', 123, state)

        gh_client = self._gh_client([])
        count = self._run_sweep(tracker, gh_client)

        assert count == 0
        gh_client.rest.assert_not_called()

    def test_a_completed_at_inside_the_recency_window_defers(self, tracker):
        """PROTECTION 5 still guards a record that does carry a completion time:
        five minutes is measured from the end of the execution. Nothing in
        production writes the field today, so this is the gate's behaviour on the
        shape it will have once the watchdog is activated, pinned now."""
        finished = datetime.now(timezone.utc) - timedelta(minutes=1)
        state_file = self._write_state(tracker, [
            self._production_record(
                timestamp=(finished - timedelta(minutes=20)).isoformat(),
                completed_at=finished.isoformat(),
            )
        ])
        gh_client = self._gh_client([])

        count = self._run_sweep(tracker, gh_client)

        assert count == 0
        gh_client.rest.assert_not_called()
        with open(state_file) as f:
            assert yaml.safe_load(f)['execution_history'][-1]['outcome'] == 'success'

    def test_a_record_older_than_the_age_gate_costs_nothing(self, tracker):
        """PROTECTION 0. 4570 of the 4721 live state files end in 'success' and
        97% of them are months old; without this gate every one of them reaches
        PROTECTION 4's GitHub query on every 15-minute sweep -- ~18k GraphQL
        queries an hour against a 5000/hour budget.

        _has_github_output() is stubbed to False so that "the sweep declined" can
        only mean the age gate; with the real gate every record declines and the
        test would pass whether or not PROTECTION 0 exists.
        """
        state_file = self._write_state(tracker, [
            self._production_record(
                timestamp=(datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
            )
        ])
        gh_client = self._gh_client([])

        count = self._run_sweep(tracker, gh_client, has_github_output=False)

        assert count == 0
        with open(state_file) as f:
            assert yaml.safe_load(f)['execution_history'][-1]['outcome'] == 'success'

    def test_the_age_gate_cutoff_is_configurable(self, tracker):
        """An operator investigating a long-stuck issue can widen the window
        without a code change. Same stub as above, for the same reason."""
        self._write_state(tracker, [
            self._production_record(
                timestamp=(datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
            )
        ])

        with patch.dict(os.environ, {'WATCHDOG_MAX_RECORD_AGE_HOURS': '2400'}):
            count = self._run_sweep(
                tracker, self._gh_client([]), has_github_output=False
            )

        assert count == 1


class TestRetryEligibilityLookups:
    """_should_retry_failed_execution()'s own I/O.

    Its "is there an active pipeline run?" check keeps its position after the
    GitHub issue-state query, where it has always been. Moving it ahead was part
    of the empty-output activation -- it only mattered once PROTECTION 1 stopped
    wedging and the sweep started reaching this method for every 'success'
    record -- and it puts an Elasticsearch fallback that WRITES back to Redis on
    records that never used to reach it. The read-only flag stays regardless:
    see test_the_run_lookup_is_read_only.
    """

    @pytest.fixture
    def tracker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield WorkExecutionStateTracker(state_dir=Path(tmpdir))

    @staticmethod
    def _project_config():
        return ProjectConfig(
            name='test-project', description='test',
            github={'org': 'test-org', 'repo': 'test-repo'},
            tech_stacks={}, pipelines=[], pipeline_routing={},
        )

    def test_the_run_lookup_is_read_only(self, tracker):
        """get_active_pipeline_run() is not the plain hash lookup it looks like:
        on a mapping miss -- the normal case once end_pipeline_run() has deleted
        the mapping -- it searches Elasticsearch and, on a hit, writes the run
        back with a fresh TTL under the board-less legacy issue key. A periodic
        sweep must not do that: a crashed run whose ES doc still reads 'active'
        would be resurrected on every pass, and the legacy key it lands under can
        shadow a later board-scoped lookup."""
        github_client = MagicMock()
        github_client.graphql.return_value = (True, {
            'repository': {
                'issue': {
                    'state': 'OPEN',
                    'projectItems': {'nodes': [
                        {'fieldValueByName': {'name': 'In Progress'}}
                    ]},
                }
            }
        })
        run_manager = MagicMock()
        run_manager.get_active_pipeline_run.return_value = None

        with patch('services.github_api_client.get_github_client', return_value=github_client), \
             patch('services.pipeline_run.get_pipeline_run_manager', return_value=run_manager):
            should_retry, reason = tracker._should_retry_failed_execution(
                'test-project', 123, 'test-agent', 'In Progress', {},
                project_config=self._project_config(),
            )

        assert should_retry is False
        assert reason == 'no_active_pipeline_run'
        assert run_manager.get_active_pipeline_run.call_args.kwargs.get(
            'restore_to_redis'
        ) is False

    def test_a_passed_in_project_config_is_not_re_read_from_disk(self, tracker):
        """get_project_config() re-reads and re-parses the project's YAML on every
        call, and the sweep already caches it per project."""
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
                project_config=self._project_config(),
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
