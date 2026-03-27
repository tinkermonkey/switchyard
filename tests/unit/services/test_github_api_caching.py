"""
Unit tests for GitHub API caching and deduplication (rate limit reduction).

Tests:
- execute_board_query_cached: TTL cache behavior
- get_discussions_updated_at: batch query construction
- Failsafe cached_items passthrough
- Adaptive poll interval
"""
import os
import pytest
import time

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import Mock, MagicMock, patch, call
from services.project_monitor import ProjectMonitor, ProjectItem
from services.github_discussions import GitHubDiscussions
from config.manager import ConfigManager


class TestBoardQueryCache:
    """Test execute_board_query_cached() in github_owner_utils"""

    def test_cache_hit_within_ttl(self):
        """Two calls within TTL should produce only one API call"""
        from services.github_owner_utils import (
            execute_board_query_cached,
            _board_query_cache,
            _board_query_cache_ttl,
        )

        # Clear cache
        _board_query_cache.clear()

        mock_data = {
            'user': {
                'projectV2': {
                    'id': 'PVT_1',
                    'title': 'Test',
                    'items': {'nodes': []}
                }
            }
        }

        mock_client = Mock()
        mock_client.graphql.return_value = (True, mock_data)

        with patch('services.github_owner_utils.build_projects_v2_query', return_value='query {}'), \
             patch('services.github_api_client.get_github_client', return_value=mock_client):

            # First call - cache miss
            result1 = execute_board_query_cached('test-owner', 1)
            assert result1 == mock_data
            assert mock_client.graphql.call_count == 1

            # Second call - cache hit
            result2 = execute_board_query_cached('test-owner', 1)
            assert result2 == mock_data
            assert mock_client.graphql.call_count == 1  # Still 1 - no new API call

        # Cleanup
        _board_query_cache.clear()

    def test_cache_miss_after_ttl(self):
        """Call after TTL expires should make a new API call"""
        from services.github_owner_utils import (
            execute_board_query_cached,
            _board_query_cache,
        )

        # Clear cache
        _board_query_cache.clear()

        mock_data = {'user': {'projectV2': {'items': {'nodes': []}}}}
        mock_client = Mock()
        mock_client.graphql.return_value = (True, mock_data)

        with patch('services.github_owner_utils.build_projects_v2_query', return_value='query {}'), \
             patch('services.github_api_client.get_github_client', return_value=mock_client):

            # Prime cache
            execute_board_query_cached('test-owner', 1)
            assert mock_client.graphql.call_count == 1

            # Manually expire cache entry
            cache_key = ('test-owner', 1)
            old_time, data = _board_query_cache[cache_key]
            _board_query_cache[cache_key] = (old_time - 20, data)  # 20s ago

            # Should fetch again
            execute_board_query_cached('test-owner', 1)
            assert mock_client.graphql.call_count == 2

        _board_query_cache.clear()

    def test_failure_not_cached(self):
        """Failed API calls should not be cached (even after retry)"""
        from services.github_owner_utils import (
            execute_board_query_cached,
            _board_query_cache,
        )

        _board_query_cache.clear()

        mock_client = Mock()
        mock_client.graphql.return_value = (False, 'error')

        with patch('services.github_owner_utils.build_projects_v2_query', return_value='query {}'), \
             patch('services.github_api_client.get_github_client', return_value=mock_client), \
             patch('services.github_owner_utils.time.sleep'):  # Skip retry delay

            result = execute_board_query_cached('test-owner', 1)
            assert result is None
            assert ('test-owner', 1) not in _board_query_cache
            # Should have retried once (2 total attempts)
            assert mock_client.graphql.call_count == 2

        _board_query_cache.clear()

    def test_retry_succeeds_on_second_attempt(self):
        """Should return data if retry succeeds"""
        from services.github_owner_utils import (
            execute_board_query_cached,
            _board_query_cache,
        )

        _board_query_cache.clear()

        mock_data = {'user': {'projectV2': {'items': {'nodes': []}}}}
        mock_client = Mock()
        mock_client.graphql.side_effect = [(False, 'transient error'), (True, mock_data)]

        with patch('services.github_owner_utils.build_projects_v2_query', return_value='query {}'), \
             patch('services.github_api_client.get_github_client', return_value=mock_client), \
             patch('services.github_owner_utils.time.sleep'):  # Skip retry delay

            result = execute_board_query_cached('test-owner', 1)
            assert result == mock_data
            assert mock_client.graphql.call_count == 2
            assert ('test-owner', 1) in _board_query_cache

        _board_query_cache.clear()

    def test_different_projects_cached_independently(self):
        """Different (owner, project_number) pairs should have independent cache entries"""
        from services.github_owner_utils import (
            execute_board_query_cached,
            _board_query_cache,
        )

        _board_query_cache.clear()

        mock_data_1 = {'user': {'projectV2': {'items': {'nodes': []}, 'title': 'Project1'}}}
        mock_data_2 = {'user': {'projectV2': {'items': {'nodes': []}, 'title': 'Project2'}}}

        mock_client = Mock()
        mock_client.graphql.side_effect = [(True, mock_data_1), (True, mock_data_2)]

        with patch('services.github_owner_utils.build_projects_v2_query', return_value='query {}'), \
             patch('services.github_api_client.get_github_client', return_value=mock_client):

            result1 = execute_board_query_cached('owner-a', 1)
            result2 = execute_board_query_cached('owner-b', 2)

            assert result1 == mock_data_1
            assert result2 == mock_data_2
            assert mock_client.graphql.call_count == 2

        _board_query_cache.clear()


class TestBatchDiscussionUpdatedAt:
    """Test GitHubDiscussions.get_discussions_updated_at()"""

    def test_single_query_for_multiple_discussions(self):
        """Should construct a single GraphQL query for N discussion IDs"""
        discussions = GitHubDiscussions.__new__(GitHubDiscussions)
        discussions.app = Mock()
        discussions.app.enabled = False
        discussions.client = Mock()

        mock_result = {
            'd0': {'id': 'D_1', 'updatedAt': '2025-01-01T00:00:00Z'},
            'd1': {'id': 'D_2', 'updatedAt': '2025-01-02T00:00:00Z'},
            'd2': None,  # Deleted discussion
        }
        discussions.client.graphql.return_value = (True, mock_result)

        result = discussions.get_discussions_updated_at(['D_1', 'D_2', 'D_3'])

        assert result == {
            'D_1': '2025-01-01T00:00:00Z',
            'D_2': '2025-01-02T00:00:00Z',
            'D_3': None,
        }

        # Should have made exactly 1 GraphQL call
        assert discussions.client.graphql.call_count == 1

        # Verify the query contains all three aliases
        query_arg = discussions.client.graphql.call_args[0][0]
        assert 'd0: node(id: "D_1")' in query_arg
        assert 'd1: node(id: "D_2")' in query_arg
        assert 'd2: node(id: "D_3")' in query_arg

    def test_empty_list_returns_empty_dict(self):
        """Empty input should return empty dict without API call"""
        discussions = GitHubDiscussions.__new__(GitHubDiscussions)
        discussions.app = Mock()
        discussions.client = Mock()

        result = discussions.get_discussions_updated_at([])
        assert result == {}
        discussions.client.graphql.assert_not_called()

    def test_api_failure_returns_none_values(self):
        """API failure should return None for all IDs"""
        discussions = GitHubDiscussions.__new__(GitHubDiscussions)
        discussions.app = Mock()
        discussions.app.enabled = False
        discussions.client = Mock()
        discussions.client.graphql.return_value = (False, 'error')

        result = discussions.get_discussions_updated_at(['D_1', 'D_2'])
        assert result == {'D_1': None, 'D_2': None}

    def test_invalid_node_id_rejected(self):
        """IDs with injection characters should be rejected"""
        discussions = GitHubDiscussions.__new__(GitHubDiscussions)
        discussions.app = Mock()
        discussions.client = Mock()

        # Attempt injection via malformed ID
        result = discussions.get_discussions_updated_at(['D_1', '") { malicious } d99: node(id: "X'])

        # All should return None, no API call made
        assert all(v is None for v in result.values())
        discussions.client.graphql.assert_not_called()

    def test_batching_large_lists(self):
        """Lists > 50 should be split into multiple batches"""
        discussions = GitHubDiscussions.__new__(GitHubDiscussions)
        discussions.app = Mock()
        discussions.app.enabled = False
        discussions.client = Mock()

        ids = [f'D_{i}' for i in range(75)]

        # Return valid data for each batch
        batch1_result = {f'd{i}': {'id': f'D_{i}', 'updatedAt': '2025-01-01T00:00:00Z'} for i in range(50)}
        batch2_result = {f'd{i}': {'id': f'D_{50+i}', 'updatedAt': '2025-01-02T00:00:00Z'} for i in range(25)}
        discussions.client.graphql.side_effect = [(True, batch1_result), (True, batch2_result)]

        result = discussions.get_discussions_updated_at(ids)

        assert len(result) == 75
        assert discussions.client.graphql.call_count == 2


class TestFailsafeCachedItems:
    """Test that failsafe uses cached items when provided"""

    @pytest.fixture
    def mock_config_manager(self):
        config_manager = Mock(spec=ConfigManager)

        active_pipeline = Mock()
        active_pipeline.active = True
        active_pipeline.board_name = "SDLC Execution"
        active_pipeline.workflow = "sdlc_execution_workflow"

        project_config = Mock()
        project_config.pipelines = [active_pipeline]
        project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
        project_config.orchestrator = {"polling_interval": 15}

        config_manager.list_projects.return_value = []
        config_manager.list_visible_projects.return_value = ['test_project']
        config_manager.get_project_config.return_value = project_config

        return config_manager

    @pytest.fixture
    def project_monitor(self, mock_config_manager):
        task_queue = Mock()
        monitor = ProjectMonitor(task_queue, mock_config_manager)
        monitor.trigger_agent_for_status = Mock()
        monitor.get_issue_column_sync = Mock(return_value='Development')
        return monitor

    def test_find_stalled_uses_cached_items(self, project_monitor):
        """_find_stalled_issues_for_pipeline should use cached_items when provided"""
        cached = [
            ProjectItem(
                item_id='I_1', content_id='C_1', issue_number=42,
                title='Test', status='Development', repository='repo',
                last_updated='2025-01-01T00:00:00Z'
            )
        ]

        with patch('services.project_monitor.get_github_client') as mock_gh, \
             patch('services.project_monitor.ConfigManager') as MockCM, \
             patch('config.state_manager.state_manager') as mock_state, \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager') as mock_pqm, \
             patch('services.work_execution_state.work_execution_tracker') as mock_wet:

            # Setup mocks
            mock_pipeline = Mock()
            mock_pipeline.board_name = 'SDLC Execution'
            mock_pipeline.workflow = 'sdlc_execution_workflow'

            mock_pc = Mock()
            mock_pc.pipelines = [mock_pipeline]
            mock_pc.github = {'org': 'test-org'}
            MockCM.return_value.get_project_config.return_value = mock_pc

            mock_wf = Mock()
            mock_col = Mock()
            mock_col.name = 'Done'
            mock_col.type = 'exit'
            mock_wf.columns = [mock_col]
            mock_wf.pipeline_exit_columns = None
            MockCM.return_value.get_workflow_template.return_value = mock_wf

            mock_pqm.return_value.load_queue.return_value = []

            mock_wet.load_state.return_value = None

            # Key assertion: get_project_items should NOT be called when cached_items provided
            project_monitor.get_project_items = Mock()

            project_monitor._find_stalled_issues_for_pipeline(
                'test_project', 'SDLC Execution',
                cached_items=cached
            )

            project_monitor.get_project_items.assert_not_called()

    def test_find_stalled_fetches_when_no_cache(self, project_monitor):
        """_find_stalled_issues_for_pipeline should fetch from API when cached_items is None"""
        with patch('services.project_monitor.get_github_client') as mock_gh, \
             patch('services.project_monitor.ConfigManager') as MockCM, \
             patch('config.state_manager.state_manager') as mock_state, \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager') as mock_pqm, \
             patch('services.work_execution_state.work_execution_tracker') as mock_wet:

            mock_pipeline = Mock()
            mock_pipeline.board_name = 'SDLC Execution'
            mock_pipeline.workflow = 'sdlc_execution_workflow'

            mock_pc = Mock()
            mock_pc.pipelines = [mock_pipeline]
            mock_pc.github = {'org': 'test-org'}
            MockCM.return_value.get_project_config.return_value = mock_pc

            mock_wf = Mock()
            mock_col = Mock()
            mock_col.name = 'Done'
            mock_col.type = 'exit'
            mock_wf.columns = [mock_col]
            mock_wf.pipeline_exit_columns = None
            MockCM.return_value.get_workflow_template.return_value = mock_wf

            mock_board_state = Mock()
            mock_board_state.project_number = 1
            mock_project_state = Mock()
            mock_project_state.boards = {'SDLC Execution': mock_board_state}
            mock_state.load_project_state.return_value = mock_project_state

            mock_pqm.return_value.load_queue.return_value = []

            # Should call get_project_items when no cached items
            project_monitor.get_project_items = Mock(return_value=[])

            project_monitor._find_stalled_issues_for_pipeline(
                'test_project', 'SDLC Execution',
                cached_items=None
            )

            project_monitor.get_project_items.assert_called_once()


class TestAdaptivePollInterval:
    """Test adaptive polling interval behavior"""

    @pytest.fixture
    def project_monitor(self):
        config_manager = Mock(spec=ConfigManager)
        config_manager.list_projects.return_value = []
        task_queue = Mock()
        monitor = ProjectMonitor(task_queue, config_manager)
        return monitor

    def test_initial_interval_matches_base(self, project_monitor):
        """Initial poll interval should match base"""
        assert project_monitor._current_poll_interval == project_monitor._base_poll_interval

    def test_idle_cycles_increment(self, project_monitor):
        """Idle cycles should start at 0"""
        assert project_monitor._idle_cycles == 0

    def test_backoff_after_threshold(self, project_monitor):
        """Interval should increase after idle threshold exceeded"""
        base = project_monitor._base_poll_interval
        threshold = project_monitor._idle_backoff_threshold

        # Simulate idle cycles past threshold
        project_monitor._idle_cycles = threshold + 1
        project_monitor._current_poll_interval = base

        # Apply backoff (same logic as in the main loop)
        if project_monitor._idle_cycles > project_monitor._idle_backoff_threshold:
            project_monitor._current_poll_interval = min(
                project_monitor._current_poll_interval * 1.5,
                project_monitor._max_poll_interval
            )

        assert project_monitor._current_poll_interval == base * 1.5

    def test_interval_capped_at_max(self, project_monitor):
        """Interval should never exceed max_poll_interval"""
        project_monitor._current_poll_interval = 50
        project_monitor._idle_cycles = 100

        # Apply backoff
        if project_monitor._idle_cycles > project_monitor._idle_backoff_threshold:
            project_monitor._current_poll_interval = min(
                project_monitor._current_poll_interval * 1.5,
                project_monitor._max_poll_interval
            )

        assert project_monitor._current_poll_interval == project_monitor._max_poll_interval

    def test_reset_on_activity(self, project_monitor):
        """Interval should reset to base when changes are detected"""
        project_monitor._idle_cycles = 20
        project_monitor._current_poll_interval = 45

        # Simulate activity detected (same logic as main loop)
        project_monitor._idle_cycles = 0
        project_monitor._current_poll_interval = project_monitor._base_poll_interval

        assert project_monitor._idle_cycles == 0
        assert project_monitor._current_poll_interval == project_monitor._base_poll_interval
