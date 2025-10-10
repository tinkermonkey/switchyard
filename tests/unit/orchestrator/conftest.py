"""
Shared fixtures for orchestrator state machine tests
"""

import pytest
from unittest.mock import Mock, MagicMock, AsyncMock, patch
from typing import Dict, Any

from tests.unit.orchestrator.mocks import MockGitHubAPI, MockAgentExecutor, MockReviewParser
from tests.unit.orchestrator.mocks.mock_agents import success_result, approved_review, rejected_review
from tests.unit.orchestrator.mocks.mock_parsers import approved_review_result, changes_requested_result


# ============================================================================
# Mock Service Fixtures
# ============================================================================

@pytest.fixture
def mock_github():
    """Mock GitHub API"""
    return MockGitHubAPI()


@pytest.fixture
def mock_agent_executor():
    """Mock agent executor"""
    return MockAgentExecutor()


@pytest.fixture
def mock_review_parser():
    """Mock review parser"""
    return MockReviewParser()


# ============================================================================
# Configuration Fixtures
# ============================================================================

@pytest.fixture
def test_project_config():
    """Test project configuration with multi-stage pipeline"""
    return {
        'name': 'test-project',
        'github': {
            'org': 'test-org',
            'repo': 'test-repo',
            'project_owner': 'test-org',
            'project_number': 1
        },
        'pipelines': [{
            'board_name': 'dev',
            'workflow': 'test-workflow',
            'workspace': 'issues'
        }]
    }


@pytest.fixture
def test_workflow_template():
    """Test workflow template with maker-reviewer stages"""
    return {
        'name': 'test-workflow',
        'columns': [
            {
                'name': 'Requirements',
                'agent': 'business_analyst',
                'type': 'conversational'
            },
            {
                'name': 'Requirements Review',
                'agent': 'requirements_reviewer',
                'maker_agent': 'business_analyst',
                'type': 'review',
                'max_iterations': 3
            },
            {
                'name': 'Design',
                'agent': 'software_architect',
                'type': 'conversational'
            },
            {
                'name': 'Design Review',
                'agent': 'design_reviewer',
                'maker_agent': 'software_architect',
                'type': 'review',
                'max_iterations': 3
            },
            {
                'name': 'Development',
                'agent': 'senior_software_engineer',
                'type': 'standard'
            },
            {
                'name': 'Code Review',
                'agent': 'code_reviewer',
                'maker_agent': 'senior_software_engineer',
                'type': 'review',
                'max_iterations': 3
            },
            {
                'name': 'QA',
                'agent': 'qa_engineer',
                'type': 'standard'
            },
            {
                'name': 'Done',
                'agent': None,
                'type': 'terminal'
            }
        ]
    }


@pytest.fixture
def simple_workflow_template():
    """Simple workflow without review cycles"""
    return {
        'name': 'simple-workflow',
        'columns': [
            {'name': 'Ready', 'agent': 'agent_a', 'type': 'standard'},
            {'name': 'In Progress', 'agent': 'agent_b', 'type': 'standard'},
            {'name': 'Review', 'agent': 'agent_c', 'type': 'standard'},
            {'name': 'Done', 'agent': None, 'type': 'terminal'}
        ]
    }


# ============================================================================
# Mock Configuration Manager
# ============================================================================

@pytest.fixture
def mock_config_manager(test_project_config, test_workflow_template):
    """Mock ConfigManager"""
    mock = Mock()
    
    # Mock project config
    project_config = Mock()
    project_config.github = test_project_config['github']
    project_config.pipelines = [Mock()]
    project_config.pipelines[0].board_name = 'dev'
    project_config.pipelines[0].workflow = 'test-workflow'
    project_config.pipelines[0].workspace = 'issues'
    project_config.orchestrator = {'polling_interval': 30}
    
    mock.get_project_config.return_value = project_config
    mock.list_projects.return_value = ['test-project']
    
    # Mock workflow template
    workflow = Mock()
    workflow.columns = []
    for col_data in test_workflow_template['columns']:
        col = Mock()
        col.name = col_data['name']
        col.agent = col_data['agent']
        col.type = col_data.get('type', 'standard')
        col.maker_agent = col_data.get('maker_agent')
        col.max_iterations = col_data.get('max_iterations', 3)
        workflow.columns.append(col)
    
    mock.get_workflow_template.return_value = workflow
    
    return mock


# ============================================================================
# Mock State Manager
# ============================================================================

@pytest.fixture
def mock_state_manager():
    """Mock state_manager"""
    mock = Mock()
    
    # Mock project state
    project_state = Mock()
    
    # Mock board state with columns
    board_state = Mock()
    board_state.status_field_id = 'field_123'
    board_state.project_id = 'proj_123'
    board_state.project_number = 1
    
    # Mock columns list
    mock_columns = []
    column_names = ['Requirements', 'Requirements Review', 'Design', 'Design Review', 
                    'Development', 'Code Review', 'QA', 'Done']
    for idx, name in enumerate(column_names):
        col = Mock()
        col.name = name
        col.id = f'col_{idx}'
        mock_columns.append(col)
    board_state.columns = mock_columns
    
    project_state.boards = {'dev': board_state}
    
    mock.load_project_state.return_value = project_state
    mock.get_discussion_for_issue.return_value = None
    
    return mock


# ============================================================================
# State Tracker Fixture
# ============================================================================

class StateTracker:
    """Track state transitions and events during tests"""
    
    def __init__(self):
        self.transitions = []
        self.events = []
        self.states = {}  # Current state per issue
    
    def record_transition(self, issue_number: int, from_state: str, to_state: str, reason: str = ""):
        """Record a state transition"""
        self.transitions.append({
            'issue': issue_number,
            'from': from_state,
            'to': to_state,
            'reason': reason
        })
        self.states[issue_number] = to_state
    
    def record_event(self, event_type: str, issue_number: int, data: Dict[str, Any]):
        """Record an event"""
        self.events.append({
            'type': event_type,
            'issue': issue_number,
            'data': data
        })
    
    def current_state(self, issue_number: int) -> str:
        """Get current state of an issue"""
        return self.states.get(issue_number, 'unknown')
    
    def get_events(self, issue: int = None, event_type: str = None):
        """Get filtered events"""
        filtered = self.events
        if issue:
            filtered = [e for e in filtered if e['issue'] == issue]
        if event_type:
            filtered = [e for e in filtered if e['type'] == event_type]
        return filtered
    
    def reset(self):
        """Reset tracker"""
        self.transitions.clear()
        self.events.clear()
        self.states.clear()


@pytest.fixture
def state_tracker():
    """State tracker for monitoring test execution"""
    return StateTracker()


# ============================================================================
# Mock Task Queue
# ============================================================================

@pytest.fixture
def mock_task_queue():
    """Mock task queue"""
    mock = Mock()
    mock.get_pending_tasks.return_value = []
    mock.add_task = AsyncMock()
    mock.get_task = AsyncMock(return_value=None)
    return mock


# ============================================================================
# Mock Observability
# ============================================================================

@pytest.fixture
def mock_observability():
    """Mock observability manager and decision events"""
    obs_mock = Mock()
    obs_mock.emit = Mock()
    
    decision_events_mock = Mock()
    decision_events_mock.emit_agent_routing_decision = Mock()
    decision_events_mock.emit_review_cycle_decision = Mock()
    decision_events_mock.emit_status_progression = Mock()
    
    return obs_mock, decision_events_mock


# ============================================================================
# Helper Functions
# ============================================================================

def create_test_issue(mock_github: MockGitHubAPI, issue_number: int, status: str, **kwargs):
    """Helper to create a test issue"""
    defaults = {
        'title': f'Test Issue #{issue_number}',
        'state': 'OPEN',
        'repository': 'test-repo'
    }
    defaults.update(kwargs)
    mock_github.create_issue(number=issue_number, status=status, **defaults)


def configure_agent_results(mock_executor: MockAgentExecutor, agent_name: str, **kwargs):
    """
    Helper to configure agent execution results
    
    Args:
        mock_executor: The mock agent executor
        agent_name: Name of the agent to configure
        **kwargs: Result configuration (success=True, approved=True, rejected=True, etc.)
    """
    result = {}
    
    # Handle common kwargs
    if 'success' in kwargs:
        result['success'] = kwargs['success']
    if 'approved' in kwargs:
        result['approved'] = kwargs['approved']
    if 'rejected' in kwargs:
        result['rejected'] = kwargs['rejected']
    if 'output' in kwargs:
        result['output'] = kwargs['output']
    
    # If no specific keys provided, default to success
    if not result:
        result['success'] = True
    
    mock_executor.set_result(agent_name, result)
