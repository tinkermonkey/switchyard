"""
Integration test for pipeline run completion.

This test ensures that the end_pipeline_run() function properly marks
pipeline runs as completed without encountering import scoping issues.
"""

import pytest
import json
from unittest.mock import MagicMock, patch, call
from datetime import datetime
from services.pipeline_run import PipelineRunManager
from config.manager import WorkflowColumn


class MockElasticsearch:
    """Mock Elasticsearch client for testing"""

    def __init__(self):
        self.indexed_docs = []
        self.ilm = MagicMock()
        self.indices = MagicMock()
        # Mock indices.exists to return False (no existing indices)
        self.indices.exists.return_value = False

    def index(self, index, id=None, document=None, body=None):
        """Track indexed documents"""
        doc = document or body
        self.indexed_docs.append({'index': index, 'id': id, 'body': doc})
        return {'result': 'created'}


class MockRedis:
    """Mock Redis client for testing"""

    def __init__(self):
        self.data = {}

    def get(self, key):
        """Get value from mock Redis"""
        return self.data.get(key)

    def setex(self, key, ttl, value):
        """Set value with expiration in mock Redis"""
        self.data[key] = value
        return True

    def hget(self, key, field):
        """Get hash field from mock Redis"""
        if key in self.data and isinstance(self.data[key], dict):
            return self.data[key].get(field)
        return None

    def hset(self, key, field, value):
        """Set hash field in mock Redis"""
        if key not in self.data:
            self.data[key] = {}
        if not isinstance(self.data[key], dict):
            self.data[key] = {}
        self.data[key][field] = value
        return 1

    def hdel(self, key, field):
        """Delete hash field from mock Redis"""
        if key in self.data and isinstance(self.data[key], dict):
            self.data[key].pop(field, None)
        return 1

    def delete(self, key):
        """Delete key from mock Redis"""
        if key in self.data:
            del self.data[key]
        return 1


@pytest.fixture
def mock_elasticsearch():
    """Fixture providing a mock Elasticsearch client"""
    return MockElasticsearch()


@pytest.fixture
def mock_redis():
    """Fixture providing a mock Redis client"""
    return MockRedis()


@pytest.fixture
def pipeline_run_manager(mock_elasticsearch, mock_redis):
    """Fixture providing a PipelineRunManager with mocked dependencies"""
    with patch('services.pipeline_run.Elasticsearch', return_value=mock_elasticsearch), \
         patch('services.pipeline_run.redis.Redis', return_value=mock_redis):
        manager = PipelineRunManager()
        manager.es = mock_elasticsearch
        manager.redis = mock_redis
        return manager


@pytest.fixture
def mock_project_config():
    """Create a mock project configuration"""
    config = MagicMock()
    config.github = {
        'org': 'test-org',
        'repo': 'test-repo',
        'project_id': 'PVT_test123'
    }

    pipeline_config = MagicMock()
    pipeline_config.board_name = "Test Board"
    pipeline_config.workflow = "test_workflow"
    config.pipelines = [pipeline_config]

    return config


class TestPipelineRunCompletion:
    """Test suite for pipeline run completion functionality"""

    def test_end_pipeline_run_marks_as_completed(
        self,
        pipeline_run_manager,
        mock_elasticsearch,
        mock_redis,
        mock_project_config
    ):
        """Test that end_pipeline_run successfully marks run as completed"""
        # Setup: Create an active pipeline run
        run_id = 'test-run-123'
        pipeline_run_data = {
            'id': run_id,
            'issue_number': 42,
            'issue_title': 'Test Issue',
            'issue_url': 'https://github.com/test-org/test-repo/issues/42',
            'project': 'test-project',
            'board': 'Test Board',
            'started_at': datetime.utcnow().isoformat(),
            'status': 'active'
        }

        # Store in mock Redis
        redis_key = f"orchestrator:pipeline_run:test-project:42"
        mock_redis.setex(redis_key, 3600, json.dumps(pipeline_run_data))

        with patch('config.manager.config_manager') as mock_config:
            mock_config.get_project_config.return_value = mock_project_config

            # Mock subprocess.run for issue fetching
            with patch('subprocess.run') as mock_subprocess:
                mock_subprocess.return_value = MagicMock(
                    returncode=0,
                    stdout='[]'  # No next issue
                )

                # Execute: End the pipeline run
                pipeline_run_manager.end_pipeline_run(
                    'test-project',
                    42,
                    reason='Testing completion'
                )

        # Verify: Pipeline run should be marked as completed
        # Check that Redis was updated with completed status
        updated_data = mock_redis.get(redis_key)
        assert updated_data is not None

        updated_run = json.loads(updated_data)
        assert updated_run['status'] == 'completed'
        assert 'completed_at' in updated_run
        assert updated_run['completion_reason'] == 'Testing completion'

    def test_end_pipeline_run_handles_next_issue_fetch(
        self,
        pipeline_run_manager,
        mock_elasticsearch,
        mock_redis,
        mock_project_config
    ):
        """Test that end_pipeline_run handles fetching next issue details"""
        # Setup
        run_id = 'test-run-456'
        pipeline_run_data = {
            'id': run_id,
            'issue_number': 100,
            'issue_title': 'Another Test',
            'issue_url': 'https://github.com/test-org/test-repo/issues/100',
            'project': 'test-project',
            'board': 'Test Board',
            'started_at': datetime.utcnow().isoformat(),
            'status': 'active'
        }

        redis_key = f"orchestrator:pipeline_run:test-project:100"
        mock_redis.setex(redis_key, 3600, json.dumps(pipeline_run_data))

        # Store a next issue in the pipeline queue
        next_issue_data = {
            'project': 'test-project',
            'issue_number': 101,
            'column': 'Development',
            'agent': 'senior_software_engineer'
        }
        queue_key = f"orchestrator:pipeline_queue:test-project"
        mock_redis.data[queue_key] = json.dumps([next_issue_data])

        with patch('config.manager.config_manager') as mock_config:
            mock_config.get_project_config.return_value = mock_project_config

            # Mock subprocess.run for issue fetching
            with patch('subprocess.run') as mock_subprocess:
                mock_subprocess.return_value = MagicMock(
                    returncode=0,
                    stdout=json.dumps([{
                        'title': 'Next Issue Title',
                        'url': 'https://github.com/test-org/test-repo/issues/101'
                    }])
                )

                # Execute: End the pipeline run (should fetch next issue)
                pipeline_run_manager.end_pipeline_run(
                    'test-project',
                    100,
                    reason='Completed successfully'
                )

        # Verify: Run should be completed and subprocess was called
        updated_data = mock_redis.get(redis_key)
        updated_run = json.loads(updated_data)
        assert updated_run['status'] == 'completed'

        # Verify subprocess was called to fetch issue details
        assert mock_subprocess.called

    def test_end_pipeline_run_handles_fetch_error_gracefully(
        self,
        pipeline_run_manager,
        mock_elasticsearch,
        mock_redis,
        mock_project_config
    ):
        """Test that end_pipeline_run completes even if next issue fetch fails"""
        # Setup
        run_id = 'test-run-789'
        pipeline_run_data = {
            'id': run_id,
            'issue_number': 200,
            'issue_title': 'Test with Error',
            'issue_url': 'https://github.com/test-org/test-repo/issues/200',
            'project': 'test-project',
            'board': 'Test Board',
            'started_at': datetime.utcnow().isoformat(),
            'status': 'active'
        }

        redis_key = f"orchestrator:pipeline_run:test-project:200"
        mock_redis.setex(redis_key, 3600, json.dumps(pipeline_run_data))

        with patch('config.manager.config_manager') as mock_config:
            mock_config.get_project_config.return_value = mock_project_config

            # Mock subprocess.run to raise an error
            with patch('subprocess.run') as mock_subprocess:
                mock_subprocess.side_effect = Exception("GitHub API error")

                # Execute: Should complete successfully despite error
                pipeline_run_manager.end_pipeline_run(
                    'test-project',
                    200,
                    reason='Completed with fetch error'
                )

        # Verify: Run should still be completed
        updated_data = mock_redis.get(redis_key)
        assert updated_data is not None

        updated_run = json.loads(updated_data)
        assert updated_run['status'] == 'completed'
        assert 'completed_at' in updated_run

    def test_end_pipeline_run_releases_lock(
        self,
        pipeline_run_manager,
        mock_elasticsearch,
        mock_redis,
        mock_project_config
    ):
        """Test that end_pipeline_run releases the pipeline lock"""
        # Setup
        pipeline_run_data = {
            'id': 'test-run-lock',
            'issue_number': 300,
            'issue_title': 'Test Lock Release',
            'issue_url': 'https://github.com/test-org/test-repo/issues/300',
            'project': 'test-project',
            'board': 'Test Board',
            'started_at': datetime.utcnow().isoformat(),
            'status': 'active'
        }

        redis_key = f"orchestrator:pipeline_run:test-project:300"
        lock_key = "orchestrator:pipeline_locks"

        mock_redis.setex(redis_key, 3600, json.dumps(pipeline_run_data))
        mock_redis.data[lock_key] = {'test-project': '300'}

        with patch('config.manager.config_manager') as mock_config:
            mock_config.get_project_config.return_value = mock_project_config

            with patch('subprocess.run') as mock_subprocess:
                mock_subprocess.return_value = MagicMock(
                    returncode=0,
                    stdout='[]'
                )

                # Execute
                pipeline_run_manager.end_pipeline_run(
                    'test-project',
                    300,
                    reason='Test lock release'
                )

        # Verify: Lock should be released
        # The hdel should have been called to remove the lock
        assert 'test-project' not in mock_redis.data.get(lock_key, {})

    def test_end_pipeline_run_retain_lock_skips_release(
        self,
        pipeline_run_manager,
        mock_elasticsearch,
        mock_redis,
        mock_project_config
    ):
        """
        Regression test: end_pipeline_run(retain_lock=True) must NOT release the
        pipeline lock. This is the path taken on repair cycle failure — the lock
        must stay held so the issue blocks the pipeline until a human moves it.
        """
        run_id = 'test-run-retain-lock'
        pipeline_run_data = {
            'id': run_id,
            'issue_number': 350,
            'issue_title': 'Test Retain Lock',
            'issue_url': 'https://github.com/test-org/test-repo/issues/350',
            'project': 'test-project',
            'board': 'Test Board',
            'started_at': datetime.utcnow().isoformat(),
            'status': 'active'
        }
        # Seed the run data (keyed by run_id, not issue number)
        run_redis_key = f"orchestrator:pipeline_run:{run_id}"
        mock_redis.setex(run_redis_key, 3600, json.dumps(pipeline_run_data))
        # Seed the issue → run_id mapping so get_active_pipeline_run() finds it
        mock_redis.hset("orchestrator:pipeline_run:issue_mapping", "test-project:350", run_id)

        mock_lock_mgr = MagicMock()
        mock_lock_mgr.release_lock = MagicMock()

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_mgr):
            result = pipeline_run_manager.end_pipeline_run(
                'test-project',
                350,
                reason='Repair cycle failed: tests still failing',
                retain_lock=True
            )

        # Lock must NOT have been released
        mock_lock_mgr.release_lock.assert_not_called()

        # But the pipeline run itself should still be persisted as completed
        updated_data = mock_redis.get(run_redis_key)
        assert updated_data is not None
        updated_run = json.loads(updated_data)
        assert updated_run['status'] == 'completed'

        # And end_pipeline_run should have returned True (run was ended)
        assert result is True

    def test_end_pipeline_run_with_no_active_run(
        self,
        pipeline_run_manager,
        mock_redis,
        mock_project_config
    ):
        """Test that end_pipeline_run handles case where no active run exists"""
        with patch('config.manager.config_manager') as mock_config:
            mock_config.get_project_config.return_value = mock_project_config

            # Execute: Try to end a non-existent run
            # Should not raise an exception
            pipeline_run_manager.end_pipeline_run(
                'test-project',
                999,
                reason='Non-existent run'
            )

        # Verify: Should complete without error
        assert True  # Test passes if no exception raised


class TestJsonImportScoping:
    """Test that verifies the json import scoping issue is fixed"""

    def test_json_module_accessible_throughout_function(
        self,
        pipeline_run_manager,
        mock_elasticsearch,
        mock_redis,
        mock_project_config
    ):
        """
        Regression test for the duplicate import json bug.

        This test ensures that json.dumps() and json.loads() work correctly
        throughout the end_pipeline_run function, without UnboundLocalError.
        """
        # Setup
        pipeline_run_data = {
            'id': 'test-json-scoping',
            'issue_number': 400,
            'issue_title': 'Test JSON Scoping',
            'issue_url': 'https://github.com/test-org/test-repo/issues/400',
            'project': 'test-project',
            'board': 'Test Board',
            'started_at': datetime.utcnow().isoformat(),
            'status': 'active'
        }

        redis_key = f"orchestrator:pipeline_run:test-project:400"
        mock_redis.setex(redis_key, 3600, json.dumps(pipeline_run_data))

        with patch('config.manager.config_manager') as mock_config:
            mock_config.get_project_config.return_value = mock_project_config

            # Mock subprocess to return JSON that needs to be parsed
            with patch('subprocess.run') as mock_subprocess:
                mock_subprocess.return_value = MagicMock(
                    returncode=0,
                    stdout=json.dumps([{'title': 'Next', 'url': 'http://test'}])
                )

                # Execute: This should NOT raise UnboundLocalError
                try:
                    pipeline_run_manager.end_pipeline_run(
                        'test-project',
                        400,
                        reason='Test JSON scoping fix'
                    )
                    success = True
                except UnboundLocalError as e:
                    if 'json' in str(e):
                        pytest.fail(f"UnboundLocalError with json: {e}")
                    raise

                assert success, "end_pipeline_run completed without UnboundLocalError"
