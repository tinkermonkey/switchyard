"""
Unit tests for ProjectMonitor board position sorting
"""
import pytest
from unittest.mock import Mock, MagicMock
from services.project_monitor import ProjectMonitor
from config.manager import ConfigManager


class TestBoardPositionSorting:
    """Test _sort_items_by_board_position method"""

    @pytest.fixture
    def mock_config_manager(self):
        """Create a mock config manager with workflow template"""
        config_manager = Mock(spec=ConfigManager)

        # Mock workflow template with column order
        # Create column mocks with actual name attributes
        def create_column_mock(col_name):
            col = Mock()
            col.name = col_name
            return col

        workflow_template = Mock()
        workflow_template.columns = [
            create_column_mock("Backlog"),
            create_column_mock("Development"),
            create_column_mock("Code Review"),
            create_column_mock("Testing"),
            create_column_mock("Done")
        ]

        # Mock pipeline config
        pipeline = Mock()
        pipeline.board_name = "SDLC Execution"
        pipeline.workflow = "sdlc_execution_workflow"

        # Mock project config with orchestrator settings
        project_config = Mock()
        project_config.pipelines = [pipeline]
        project_config.orchestrator = {"polling_interval": 15}

        # Return empty list for list_projects to avoid initialization issues
        config_manager.list_projects.return_value = []
        config_manager.get_project_config.return_value = project_config
        config_manager.get_workflow_template.return_value = workflow_template

        return config_manager

    @pytest.fixture
    def project_monitor(self, mock_config_manager):
        """Create ProjectMonitor instance with mocked dependencies"""
        task_queue = Mock()
        monitor = ProjectMonitor(task_queue, mock_config_manager)
        return monitor

    def test_sort_by_issue_number_same_column(self, project_monitor):
        """Items in same column should be sorted by issue number (ascending)"""
        items = [
            {
                'project_name': 'test_project',
                'board_name': 'SDLC Execution',
                'change': {'issue_number': 152, 'status': 'Development'},
                'column_config': Mock()
            },
            {
                'project_name': 'test_project',
                'board_name': 'SDLC Execution',
                'change': {'issue_number': 146, 'status': 'Development'},
                'column_config': Mock()
            },
            {
                'project_name': 'test_project',
                'board_name': 'SDLC Execution',
                'change': {'issue_number': 147, 'status': 'Development'},
                'column_config': Mock()
            }
        ]

        sorted_items = project_monitor._sort_items_by_board_position(items)

        # Should be sorted 146, 147, 152
        assert sorted_items[0]['change']['issue_number'] == 146
        assert sorted_items[1]['change']['issue_number'] == 147
        assert sorted_items[2]['change']['issue_number'] == 152

    def test_sort_by_column_order(self, project_monitor):
        """Items should be sorted by column order first, then issue number"""
        items = [
            {
                'project_name': 'test_project',
                'board_name': 'SDLC Execution',
                'change': {'issue_number': 100, 'status': 'Testing'},
                'column_config': Mock()
            },
            {
                'project_name': 'test_project',
                'board_name': 'SDLC Execution',
                'change': {'issue_number': 200, 'status': 'Development'},
                'column_config': Mock()
            },
            {
                'project_name': 'test_project',
                'board_name': 'SDLC Execution',
                'change': {'issue_number': 150, 'status': 'Code Review'},
                'column_config': Mock()
            }
        ]

        sorted_items = project_monitor._sort_items_by_board_position(items)

        # Should be sorted by column order: Development (idx=1), Code Review (idx=2), Testing (idx=3)
        assert sorted_items[0]['change']['status'] == 'Development'
        assert sorted_items[0]['change']['issue_number'] == 200
        assert sorted_items[1]['change']['status'] == 'Code Review'
        assert sorted_items[1]['change']['issue_number'] == 150
        assert sorted_items[2]['change']['status'] == 'Testing'
        assert sorted_items[2]['change']['issue_number'] == 100

    def test_sort_mixed_columns_and_issues(self, project_monitor):
        """Test realistic scenario with multiple issues across columns"""
        items = [
            {
                'project_name': 'test_project',
                'board_name': 'SDLC Execution',
                'change': {'issue_number': 152, 'status': 'Development'},
                'column_config': Mock()
            },
            {
                'project_name': 'test_project',
                'board_name': 'SDLC Execution',
                'change': {'issue_number': 146, 'status': 'Development'},
                'column_config': Mock()
            },
            {
                'project_name': 'test_project',
                'board_name': 'SDLC Execution',
                'change': {'issue_number': 147, 'status': 'Code Review'},
                'column_config': Mock()
            },
            {
                'project_name': 'test_project',
                'board_name': 'SDLC Execution',
                'change': {'issue_number': 145, 'status': 'Testing'},
                'column_config': Mock()
            }
        ]

        sorted_items = project_monitor._sort_items_by_board_position(items)

        # Expected order: Development (146, 152), Code Review (147), Testing (145)
        assert sorted_items[0]['change']['issue_number'] == 146
        assert sorted_items[0]['change']['status'] == 'Development'
        assert sorted_items[1]['change']['issue_number'] == 152
        assert sorted_items[1]['change']['status'] == 'Development'
        assert sorted_items[2]['change']['issue_number'] == 147
        assert sorted_items[2]['change']['status'] == 'Code Review'
        assert sorted_items[3]['change']['issue_number'] == 145
        assert sorted_items[3]['change']['status'] == 'Testing'

    def test_empty_list(self, project_monitor):
        """Empty list should return empty list"""
        items = []
        sorted_items = project_monitor._sort_items_by_board_position(items)
        assert sorted_items == []

    def test_single_item(self, project_monitor):
        """Single item should return unchanged"""
        items = [
            {
                'project_name': 'test_project',
                'board_name': 'SDLC Execution',
                'change': {'issue_number': 123, 'status': 'Development'},
                'column_config': Mock()
            }
        ]

        sorted_items = project_monitor._sort_items_by_board_position(items)
        assert len(sorted_items) == 1
        assert sorted_items[0]['change']['issue_number'] == 123
