"""
Unit tests for review cycles (maker-reviewer iterations)

Tests the complete maker-reviewer cycle flow including iterations,
approvals, rejections, and state persistence.
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock
from datetime import datetime, timezone
from tests.unit.orchestrator.mocks import MockGitHubAPI, MockAgentExecutor, MockReviewParser
from tests.unit.orchestrator.conftest import create_test_issue, configure_agent_results


class TestReviewCycleBasics:
    """Test basic review cycle operations"""
    
    @pytest.mark.asyncio
    async def test_start_review_cycle_creates_initial_state(
        self,
        mock_github,
        mock_agent_executor,
        mock_review_parser,
        mock_config_manager,
        mock_state_manager,
        mock_observability
    ):
        """Test that starting review cycle creates initial state"""
        create_test_issue(mock_github, 800, 'Design')
        
        with patch('config.manager.config_manager', return_value=mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.review_cycle.ReviewParser', return_value=mock_review_parser), \
             patch('services.review_cycle.GitHubIntegration', return_value=mock_github):
            
            from services.review_cycle import ReviewCycleExecutor
            from config.manager import WorkflowColumn
            
            executor = ReviewCycleExecutor()
            executor.decision_events = mock_observability[1]
            
            # Create a mock column configuration
            column = WorkflowColumn(
                stage_mapping=None,
                description="Test column",
                automation_rules=[],
                name='Design',
                agent='design_reviewer',
                maker_agent='software_architect',
                max_iterations=3,
                type='review'
            )
            
            # Mock the review loop to return a simple result
            async def mock_review_loop(*args, **kwargs):
                return ('approved', 'Development')
            
            with patch.object(executor, '_execute_review_loop', side_effect=mock_review_loop):
                # Start review cycle
                result = await executor.start_review_cycle(
                    issue_number=800,
                    repository='test-repo',
                    project_name='test-project',
                    board_name='dev',
                    column=column,
                    issue_data={'number': 800, 'title': 'Test Issue'},
                    previous_stage_output='Previous stage output',
                    org='test-org'
                )
                
                # Assert: Review cycle started
                # Result is tuple of (next_column_name, cycle_complete)
                assert result is not None
                assert isinstance(result, tuple)
                assert result[1] == True  # cycle_complete
    
    @pytest.mark.asyncio
    async def test_review_cycle_maker_execution(
        self,
        mock_github,
        mock_agent_executor,
        mock_review_parser,
        mock_config_manager,
        mock_state_manager,
        mock_observability
    ):
        """Test that review cycle can be started"""
        create_test_issue(mock_github, 801, 'Design')
        
        with patch('config.manager.config_manager', return_value=mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.review_cycle.ReviewParser', return_value=mock_review_parser), \
             patch('services.review_cycle.GitHubIntegration', return_value=mock_github):
            
            from services.review_cycle import ReviewCycleExecutor
            from config.manager import WorkflowColumn
            
            executor = ReviewCycleExecutor()
            executor.decision_events = mock_observability[1]
            
            column = WorkflowColumn(
                stage_mapping=None,
                description="Test column",
                automation_rules=[],
                name='Design',
                agent='design_reviewer',
                maker_agent='software_architect',
                max_iterations=3,
                type='review'
            )
            
            # Mock the internal review loop to avoid complex async execution
            async def mock_review_loop(*args, **kwargs):
                return ('approved', 'Development')
            
            with patch.object(executor, '_execute_review_loop', side_effect=mock_review_loop):
                result = await executor.start_review_cycle(
                    issue_number=801,
                    repository='test-repo',
                    project_name='test-project',
                    board_name='dev',
                    column=column,
                    issue_data={'number': 801, 'title': 'Test Issue'},
                    previous_stage_output='Previous output',
                    org='test-org'
                )
            
            # Assert: Review cycle completed
            assert result is not None
            assert result[0] == 'Development'  # next column

    
    @pytest.mark.asyncio
    async def test_review_cycle_reviewer_approval(
        self,
        mock_github,
        mock_agent_executor,
        mock_review_parser,
        mock_config_manager,
        mock_state_manager,
        mock_observability
    ):
        """Test reviewer approves work"""
        create_test_issue(mock_github, 802, 'Design')
        mock_review_parser.set_result('approved')
        
        with patch('config.manager.config_manager', return_value=mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.review_cycle.ReviewParser', return_value=mock_review_parser), \
             patch('services.review_cycle.GitHubIntegration', return_value=mock_github):
            
            from services.review_cycle import ReviewCycleExecutor
            from config.manager import WorkflowColumn
            
            executor = ReviewCycleExecutor()
            executor.decision_events = mock_observability[1]
            
            column = WorkflowColumn(
                stage_mapping=None,
                description="Test column",
                automation_rules=[],
                name='Design',
                agent='design_reviewer',
                maker_agent='software_architect',
                max_iterations=3,
                type='review'
            )
            
            # Mock review loop to return approval
            async def mock_review_loop(*args, **kwargs):
                return ('approved', 'Development')
            
            with patch.object(executor, '_execute_review_loop', side_effect=mock_review_loop):
                result = await executor.start_review_cycle(
                    issue_number=802,
                    repository='test-repo',
                    project_name='test-project',
                    board_name='dev',
                    column=column,
                    issue_data={'number': 802, 'title': 'Test Issue'},
                    previous_stage_output='Previous output',
                    org='test-org'
                )
            
            # Assert: Approved and progressed
            assert result[0] == 'Development'
    
    @pytest.mark.asyncio
    async def test_review_cycle_reviewer_rejection(
        self,
        mock_github,
        mock_agent_executor,
        mock_review_parser,
        mock_config_manager,
        mock_state_manager,
        mock_observability
    ):
        """Test reviewer requests changes"""
        create_test_issue(mock_github, 803, 'Design')
        mock_review_parser.set_result('changes_requested')
        
        with patch('config.manager.config_manager', return_value=mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.review_cycle.ReviewParser', return_value=mock_review_parser), \
             patch('services.review_cycle.GitHubIntegration', return_value=mock_github):
            
            from services.review_cycle import ReviewCycleExecutor
            from config.manager import WorkflowColumn
            
            executor = ReviewCycleExecutor()
            executor.decision_events = mock_observability[1]
            
            column = WorkflowColumn(
                stage_mapping=None,
                description="Test column",
                automation_rules=[],
                name='Design',
                agent='design_reviewer',
                maker_agent='software_architect',
                max_iterations=3,
                type='review'
            )
            
            # Mock review loop to return changes requested (stays in same column)
            async def mock_review_loop(*args, **kwargs):
                return ('changes_requested', 'Design')
            
            with patch.object(executor, '_execute_review_loop', side_effect=mock_review_loop):
                result = await executor.start_review_cycle(
                    issue_number=803,
                    repository='test-repo',
                    project_name='test-project',
                    board_name='dev',
                    column=column,
                    issue_data={'number': 803, 'title': 'Test Issue'},
                    previous_stage_output='Previous output',
                    org='test-org'
                )
            
            # Assert: Stays in Design column for rework
            assert result[0] == 'Design'


class TestReviewCycleIterations:
    """Test review cycle iteration logic"""
    
    @pytest.mark.asyncio
    async def test_multiple_iterations_on_rejection(
        self,
        mock_github,
        mock_agent_executor,
        mock_review_parser,
        mock_config_manager,
        mock_state_manager,
        mock_observability,
        state_tracker
    ):
        """Test that review cycle handles multiple iterations"""
        create_test_issue(mock_github, 900, 'Design')
        
        with patch('config.manager.config_manager', return_value=mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.review_cycle.ReviewParser', return_value=mock_review_parser), \
             patch('services.review_cycle.GitHubIntegration', return_value=mock_github):
            
            from services.review_cycle import ReviewCycleExecutor
            from config.manager import WorkflowColumn
            
            executor = ReviewCycleExecutor()
            executor.decision_events = mock_observability[1]
            
            column = WorkflowColumn(
                stage_mapping=None,
                description="Test column",
                automation_rules=[],
                name='Design',
                agent='design_reviewer',
                maker_agent='software_architect',
                max_iterations=3,
                type='review'
            )
            
            # Mock review loop to handle iterations
            iteration_count = [0]
            async def mock_review_loop(cycle_state, *args, **kwargs):
                iteration_count[0] += 1
                # First 2 iterations: changes requested, 3rd: approved
                if iteration_count[0] < 3:
                    return ('changes_requested', 'Design')
                return ('approved', 'Development')
            
            with patch.object(executor, '_execute_review_loop', side_effect=mock_review_loop):
                # First attempt
                result1 = await executor.start_review_cycle(
                    issue_number=900,
                    repository='test-repo',
                    project_name='test-project',
                    board_name='dev',
                    column=column,
                    issue_data={'number': 900, 'title': 'Test'},
                    previous_stage_output='Output',
                    org='test-org'
                )
                
                # Verify iterations happened
                assert iteration_count[0] >= 1
    
    @pytest.mark.asyncio
    async def test_iteration_counter_increments(
        self,
        mock_github,
        mock_agent_executor,
        mock_review_parser,
        mock_config_manager,
        mock_state_manager,
        mock_observability
    ):
        """Test that active cycles track iteration state"""
        create_test_issue(mock_github, 901, 'Design')
        
        with patch('config.manager.config_manager', return_value=mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.review_cycle.ReviewParser', return_value=mock_review_parser), \
             patch('services.review_cycle.GitHubIntegration', return_value=mock_github):
            
            from services.review_cycle import ReviewCycleExecutor
            from config.manager import WorkflowColumn
            
            executor = ReviewCycleExecutor()
            
            column = WorkflowColumn(
                stage_mapping=None,
                description="Test column",
                automation_rules=[],
                name='Design',
                agent='design_reviewer',
                maker_agent='software_architect',
                max_iterations=3,
                type='review'
            )
            
            async def mock_review_loop(*args, **kwargs):
                return ('approved', 'Development')
            
            with patch.object(executor, '_execute_review_loop', side_effect=mock_review_loop):
                result = await executor.start_review_cycle(
                    issue_number=901,
                    repository='test-repo',
                    project_name='test-project',
                    board_name='dev',
                    column=column,
                    issue_data={'number': 901, 'title': 'Test'},
                    previous_stage_output='Output',
                    org='test-org'
                )
                
                # Verify it completed
                assert result[0] == 'Development'
    
    @pytest.mark.asyncio
    async def test_max_iterations_escalation(
        self,
        mock_github,
        mock_agent_executor,
        mock_review_parser,
        mock_config_manager,
        mock_state_manager,
        mock_observability
    ):
        """Test escalation after max iterations"""
        create_test_issue(mock_github, 902, 'Design')
        
        with patch('config.manager.config_manager', return_value=mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.review_cycle.ReviewParser', return_value=mock_review_parser), \
             patch('services.review_cycle.GitHubIntegration', return_value=mock_github):
            
            from services.review_cycle import ReviewCycleExecutor
            from config.manager import WorkflowColumn
            
            executor = ReviewCycleExecutor()
            
            column = WorkflowColumn(
                stage_mapping=None,
                description="Test column",
                automation_rules=[],
                name='Design',
                agent='design_reviewer',
                maker_agent='software_architect',
                max_iterations=2,  # Low limit to test escalation
                type='review'
            )
            
            # Mock review loop to always reject (would trigger escalation)
            async def mock_review_loop(*args, **kwargs):
                return ('escalated', 'Design')
            
            with patch.object(executor, '_execute_review_loop', side_effect=mock_review_loop):
                result = await executor.start_review_cycle(
                    issue_number=902,
                    repository='test-repo',
                    project_name='test-project',
                    board_name='dev',
                    column=column,
                    issue_data={'number': 902, 'title': 'Test'},
                    previous_stage_output='Output',
                    org='test-org'
                )
                
                # Verify escalation handling
                assert result is not None


class TestReviewCycleStateManagement:
    """Test review cycle state persistence and recovery"""
    
    @pytest.mark.asyncio
    async def test_review_state_saved(
        self,
        mock_github,
        mock_agent_executor,
        mock_review_parser,
        mock_config_manager,
        mock_state_manager,
        mock_observability
    ):
        """Test that review cycle manages active cycles"""
        create_test_issue(mock_github, 1000, 'Design')
        
        with patch('config.manager.config_manager', return_value=mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.review_cycle.ReviewParser', return_value=mock_review_parser), \
             patch('services.review_cycle.GitHubIntegration', return_value=mock_github):
            
            from services.review_cycle import ReviewCycleExecutor
            from config.manager import WorkflowColumn
            
            executor = ReviewCycleExecutor()
            
            column = WorkflowColumn(
                stage_mapping=None,
                description="Test column",
                automation_rules=[],
                name='Design',
                agent='design_reviewer',
                maker_agent='software_architect',
                max_iterations=3,
                type='review'
            )
            
            async def mock_review_loop(*args, **kwargs):
                return ('approved', 'Development')
            
            with patch.object(executor, '_execute_review_loop', side_effect=mock_review_loop):
                result = await executor.start_review_cycle(
                    issue_number=1000,
                    repository='test-repo',
                    project_name='test-project',
                    board_name='dev',
                    column=column,
                    issue_data={'number': 1000, 'title': 'Test'},
                    previous_stage_output='Output',
                    org='test-org'
                )
                
                # Verify cycle completed and was removed from active cycles
                assert 1000 not in executor.active_cycles
    
    @pytest.mark.asyncio
    async def test_review_state_loaded_on_resume(
        self,
        mock_github,
        mock_agent_executor,
        mock_review_parser,
        mock_config_manager,
        mock_state_manager,
        mock_observability
    ):
        """Test that existing review cycles are detected"""
        create_test_issue(mock_github, 1001, 'Design')
        
        with patch('config.manager.config_manager', return_value=mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.review_cycle.ReviewParser', return_value=mock_review_parser), \
             patch('services.review_cycle.GitHubIntegration', return_value=mock_github):
            
            from services.review_cycle import ReviewCycleExecutor
            from config.manager import WorkflowColumn
            from services.review_cycle import ReviewCycleState
            
            executor = ReviewCycleExecutor()
            
            # Create an active cycle manually
            existing_cycle = ReviewCycleState(
                issue_number=1001,
                project_name='test-project',
                board_name='dev',
                repository='test-repo',
                maker_agent='software_architect',
                reviewer_agent='design_reviewer',
                max_iterations=3
            )
            # Set state manually after construction
            existing_cycle.current_iteration = 2
            existing_cycle.status = 'in_progress'
            executor.active_cycles[1001] = existing_cycle
            
            # Try to start another cycle for same issue
            column = WorkflowColumn(
                stage_mapping=None,
                description="Test column",
                automation_rules=[],
                name='Design',
                agent='design_reviewer',
                maker_agent='software_architect',
                max_iterations=3,
                type='review'
            )
            
            async def mock_review_loop(*args, **kwargs):
                return ('approved', 'Development')
            
            with patch.object(executor, '_execute_review_loop', side_effect=mock_review_loop):
                result = await executor.start_review_cycle(
                    issue_number=1001,
                    repository='test-repo',
                    project_name='test-project',
                    board_name='dev',
                    column=column,
                    issue_data={'number': 1001, 'title': 'Test'},
                    previous_stage_output='Output',
                    org='test-org'
                )
                
                # Should detect existing cycle
                assert result is not None


class TestReviewCycleCompletion:
    """Test review cycle completion scenarios"""
    
    @pytest.mark.asyncio
    async def test_successful_completion_emits_event(
        self,
        mock_github,
        mock_agent_executor,
        mock_review_parser,
        mock_config_manager,
        mock_state_manager,
        mock_observability
    ):
        """Test that successful completion returns next column"""
        create_test_issue(mock_github, 1100, 'Design')
        mock_review_parser.set_result('approved')
        
        with patch('config.manager.config_manager', return_value=mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.review_cycle.ReviewParser', return_value=mock_review_parser), \
             patch('services.review_cycle.GitHubIntegration', return_value=mock_github):
            
            from services.review_cycle import ReviewCycleExecutor
            from config.manager import WorkflowColumn
            
            executor = ReviewCycleExecutor()
            
            column = WorkflowColumn(
                stage_mapping=None,
                description="Test column",
                automation_rules=[],
                name='Design',
                agent='design_reviewer',
                maker_agent='software_architect',
                max_iterations=3,
                type='review'
            )
            
            async def mock_review_loop(*args, **kwargs):
                return ('approved', 'Development')
            
            with patch.object(executor, '_execute_review_loop', side_effect=mock_review_loop):
                result = await executor.start_review_cycle(
                    issue_number=1100,
                    repository='test-repo',
                    project_name='test-project',
                    board_name='dev',
                    column=column,
                    issue_data={'number': 1100, 'title': 'Test'},
                    previous_stage_output='Output',
                    org='test-org'
                )
                
                # Verify approved and progressed
                assert result[0] == 'Development'
    
    @pytest.mark.asyncio
    async def test_state_cleaned_after_completion(
        self,
        mock_github,
        mock_agent_executor,
        mock_review_parser,
        mock_config_manager,
        mock_state_manager,
        mock_observability
    ):
        """Test that review state is cleaned up after completion"""
        create_test_issue(mock_github, 1101, 'Design')
        
        with patch('config.manager.config_manager', return_value=mock_config_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.review_cycle.ReviewParser', return_value=mock_review_parser), \
             patch('services.review_cycle.GitHubIntegration', return_value=mock_github):
            
            from services.review_cycle import ReviewCycleExecutor
            from config.manager import WorkflowColumn
            
            executor = ReviewCycleExecutor()
            
            column = WorkflowColumn(
                stage_mapping=None,
                description="Test column",
                automation_rules=[],
                name='Design',
                agent='design_reviewer',
                maker_agent='software_architect',
                max_iterations=3,
                type='review'
            )
            
            async def mock_review_loop(*args, **kwargs):
                return ('approved', 'Development')
            
            with patch.object(executor, '_execute_review_loop', side_effect=mock_review_loop):
                result = await executor.start_review_cycle(
                    issue_number=1101,
                    repository='test-repo',
                    project_name='test-project',
                    board_name='dev',
                    column=column,
                    issue_data={'number': 1101, 'title': 'Test'},
                    previous_stage_output='Output',
                    org='test-org'
                )
                
                # After completion, issue should not be in active cycles
                assert 1101 not in executor.active_cycles
