"""
Unit tests for RepairCycleStage

Tests the deterministic test-fix-validate cycle implementation.
"""

import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime

from pipeline.repair_cycle import (
    RepairCycleStage,
    RepairTestRunConfig,
    RepairTestResult,
    RepairTestFailure,
    RepairTestWarning,
    CycleResult,
    RepairTestType
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def unit_test_config():
    """Basic unit test configuration"""
    return RepairTestRunConfig(
        test_type=RepairTestType.UNIT,
        command="pytest tests/unit/ -v --json-report",
        timeout=600,
        max_iterations=5,
        review_warnings=True,
        max_file_iterations=3
    )


@pytest.fixture
def integration_test_config():
    """Basic integration test configuration"""
    return RepairTestRunConfig(
        test_type=RepairTestType.INTEGRATION,
        command="pytest tests/integration/ -v --json-report",
        timeout=900,
        max_iterations=3,
        review_warnings=False,
        max_file_iterations=2
    )


@pytest.fixture
def repair_cycle_stage(unit_test_config):
    """RepairCycleStage instance with basic config"""
    return RepairCycleStage(
        name="testing",
        test_configs=[unit_test_config],
        agent_name="senior_software_engineer",
        max_total_agent_calls=100,
        checkpoint_interval=5
    )


@pytest.fixture
def context_with_observability():
    """Execution context with observability manager"""
    obs_mock = Mock()
    obs_mock.emit_performance_metric = Mock()
    
    return {
        'project': 'test-project',
        'task_id': 'task-123',
        'agent': 'repair_cycle',
        'observability': obs_mock,
        'timeout': 3600
    }


@pytest.fixture
def passing_test_result():
    """Test result with all tests passing"""
    return RepairTestResult(
        test_type=RepairTestType.UNIT,
        iteration=1,
        passed=10,
        failed=0,
        warnings=0,
        failures=[],
        warning_list=[],
        raw_output="All tests passed",
        timestamp="2025-10-11T12:00:00.000000Z"
    )


@pytest.fixture
def failing_test_result():
    """Test result with failures"""
    return RepairTestResult(
        test_type=RepairTestType.UNIT,
        iteration=1,
        passed=8,
        failed=2,
        warnings=0,
        failures=[
            RepairTestFailure(
                file="test_user.py",
                test="test_user_creation",
                message="AssertionError: Expected 'John' but got 'Jane'"
            ),
            RepairTestFailure(
                file="test_user.py",
                test="test_user_deletion",
                message="AttributeError: 'User' object has no attribute 'delete'"
            ),
            RepairTestFailure(
                file="test_product.py",
                test="test_product_price",
                message="ValueError: Price cannot be negative"
            )
        ],
        warning_list=[],
        raw_output="2 tests failed",
        timestamp="2025-10-11T12:00:00.000000Z"
    )


@pytest.fixture
def test_result_with_warnings():
    """Test result with warnings"""
    return RepairTestResult(
        test_type=RepairTestType.UNIT,
        iteration=1,
        passed=10,
        failed=0,
        warnings=3,
        failures=[],
        warning_list=[
            RepairTestWarning(
                file="user.py",
                message="DeprecationWarning: Function 'old_method' is deprecated"
            ),
            RepairTestWarning(
                file="user.py",
                message="PendingDeprecationWarning: 'legacy_field' will be removed"
            ),
            RepairTestWarning(
                file="product.py",
                message="RuntimeWarning: Implicit type conversion"
            )
        ],
        raw_output="All tests passed with 3 warnings",
        timestamp="2025-10-11T12:00:00.000000Z"
    )


# ============================================================================
# RepairTestRunConfig Tests
# ============================================================================

class TestTestRunConfig:
    """Test RepairTestRunConfig dataclass"""
    
    def test_creates_with_required_fields(self):
        """Test creating config with required fields"""
        config = RepairTestRunConfig(
            test_type=RepairTestType.UNIT,
            command="pytest tests/unit/"
        )
        
        assert config.test_type == RepairTestType.UNIT
        assert config.command == "pytest tests/unit/"
        assert config.timeout == 600  # Default
        assert config.max_iterations == 5  # Default
        assert config.review_warnings is True  # Default
    
    def test_creates_with_custom_values(self):
        """Test creating config with custom values"""
        config = RepairTestRunConfig(
            test_type=RepairTestType.E2E,
            command="pytest tests/e2e/",
            timeout=1800,
            max_iterations=2,
            review_warnings=False,
            max_file_iterations=1
        )
        
        assert config.timeout == 1800
        assert config.max_iterations == 2
        assert config.review_warnings is False
        assert config.max_file_iterations == 1


# ============================================================================
# RepairTestResult Tests
# ============================================================================

class TestTestResult:
    """Test RepairTestResult dataclass and methods"""
    
    def test_has_failures_true(self, failing_test_result):
        """Test has_failures returns True when failures exist"""
        assert failing_test_result.has_failures() is True
    
    def test_has_failures_false(self, passing_test_result):
        """Test has_failures returns False when no failures"""
        assert passing_test_result.has_failures() is False
    
    def test_has_warnings_true(self, test_result_with_warnings):
        """Test has_warnings returns True when warnings exist"""
        assert test_result_with_warnings.has_warnings() is True
    
    def test_has_warnings_false(self, passing_test_result):
        """Test has_warnings returns False when no warnings"""
        assert passing_test_result.has_warnings() is False
    
    def test_group_failures_by_file(self, failing_test_result):
        """Test grouping failures by test file"""
        grouped = failing_test_result.group_failures_by_file()
        
        assert len(grouped) == 2
        assert "test_user.py" in grouped
        assert "test_product.py" in grouped
        assert len(grouped["test_user.py"]) == 2
        assert len(grouped["test_product.py"]) == 1
    
    def test_group_warnings_by_file(self, test_result_with_warnings):
        """Test grouping warnings by source file"""
        grouped = test_result_with_warnings.group_warnings_by_file()
        
        assert len(grouped) == 2
        assert "user.py" in grouped
        assert "product.py" in grouped
        assert len(grouped["user.py"]) == 2
        assert len(grouped["product.py"]) == 1


# ============================================================================
# CycleResult Tests
# ============================================================================

class TestCycleResult:
    """Test CycleResult dataclass"""
    
    def test_to_dict_successful_cycle(self, passing_test_result):
        """Test converting successful cycle to dict"""
        cycle_result = CycleResult(
            test_type=RepairTestType.UNIT,
            passed=True,
            iterations=2,
            final_result=passing_test_result,
            files_fixed=3,
            warnings_reviewed=1,
            duration_seconds=45.5
        )
        
        result_dict = cycle_result.to_dict()
        
        assert result_dict['test_type'] == 'unit'
        assert result_dict['passed'] is True
        assert result_dict['iterations'] == 2
        assert result_dict['files_fixed'] == 3
        assert result_dict['warnings_reviewed'] == 1
        assert result_dict['duration_seconds'] == 45.5
        assert result_dict['error'] is None
    
    def test_to_dict_failed_cycle(self):
        """Test converting failed cycle to dict"""
        cycle_result = CycleResult(
            test_type=RepairTestType.INTEGRATION,
            passed=False,
            iterations=5,
            final_result=None,
            error="Max iterations reached",
            duration_seconds=120.0
        )
        
        result_dict = cycle_result.to_dict()
        
        assert result_dict['test_type'] == 'integration'
        assert result_dict['passed'] is False
        assert result_dict['iterations'] == 5
        assert result_dict['error'] == "Max iterations reached"


# ============================================================================
# RepairCycleStage Integration Tests
# ============================================================================

class TestRepairCycleStageExecution:
    """Test RepairCycleStage execution flow"""
    
    @pytest.mark.asyncio
    async def test_execute_with_passing_tests(
        self,
        repair_cycle_stage,
        context_with_observability,
        passing_test_result
    ):
        """Test execution when tests pass on first run"""
        # Mock _run_tests to return passing result
        with patch.object(
            repair_cycle_stage,
            '_run_tests',
            new_callable=AsyncMock,
            return_value=passing_test_result
        ):
            result = await repair_cycle_stage.execute(context_with_observability)
            
            assert result['overall_success'] is True
            assert len(result['test_results']) == 1
            assert result['test_results'][0]['passed'] is True
            assert result['test_results'][0]['iterations'] == 1
    
    @pytest.mark.asyncio
    async def test_execute_with_multiple_test_types(
        self,
        unit_test_config,
        integration_test_config,
        context_with_observability,
        passing_test_result
    ):
        """Test execution with multiple test types"""
        stage = RepairCycleStage(
            name="testing",
            test_configs=[unit_test_config, integration_test_config],
            agent_name="senior_software_engineer"
        )
        
        # Mock _run_tests to return passing results
        with patch.object(
            stage,
            '_run_tests',
            new_callable=AsyncMock,
            return_value=passing_test_result
        ):
            result = await stage.execute(context_with_observability)
            
            assert result['overall_success'] is True
            assert len(result['test_results']) == 2
            assert result['test_results'][0]['test_type'] == 'unit'
            assert result['test_results'][1]['test_type'] == 'integration'
    
    @pytest.mark.asyncio
    async def test_fast_fail_on_first_test_type(
        self,
        unit_test_config,
        integration_test_config,
        context_with_observability,
        failing_test_result
    ):
        """Test that execution stops when first test type fails"""
        stage = RepairCycleStage(
            name="testing",
            test_configs=[unit_test_config, integration_test_config],
            agent_name="senior_software_engineer"
        )
        
        # Mock to always fail on max iterations
        async def mock_run_test_cycle(config, context):
            return CycleResult(
                test_type=config.test_type,
                passed=False,
                iterations=config.max_iterations,
                final_result=failing_test_result,
                error="Max iterations reached"
            )
        
        with patch.object(
            stage,
            '_run_test_cycle',
            side_effect=mock_run_test_cycle
        ):
            result = await stage.execute(context_with_observability)
            
            # Should only have unit test result (integration not run)
            assert result['overall_success'] is False
            assert len(result['test_results']) == 1
            assert result['test_results'][0]['test_type'] == 'unit'
    
    @pytest.mark.asyncio
    async def test_emits_observability_metrics(
        self,
        repair_cycle_stage,
        context_with_observability,
        passing_test_result
    ):
        """Test that observability metrics are emitted"""
        obs_mock = context_with_observability['observability']
        
        with patch.object(
            repair_cycle_stage,
            '_run_tests',
            new_callable=AsyncMock,
            return_value=passing_test_result
        ):
            await repair_cycle_stage.execute(context_with_observability)
            
            # Check that metrics were emitted
            assert obs_mock.emit_performance_metric.called
            
            # Check specific metrics
            metric_calls = obs_mock.emit_performance_metric.call_args_list
            metric_names = [call[1]['metric_name'] for call in metric_calls]
            
            assert 'repair_cycle_iterations' in metric_names
            assert 'repair_cycle_duration' in metric_names
            assert 'repair_cycle_success' in metric_names


class TestRepairCycleIteration:
    """Test repair cycle iteration logic"""
    
    @pytest.mark.asyncio
    async def test_fixes_failures_until_passing(
        self,
        repair_cycle_stage,
        context_with_observability,
        failing_test_result,
        passing_test_result
    ):
        """Test that cycle fixes failures and re-runs until passing"""
        # First call: failures, second call: passing
        test_results = [failing_test_result, passing_test_result]
        
        with patch.object(
            repair_cycle_stage,
            '_run_tests',
            new_callable=AsyncMock,
            side_effect=test_results
        ), patch.object(
            repair_cycle_stage,
            '_fix_failures_by_file',
            new_callable=AsyncMock,
            return_value=2
        ):
            result = await repair_cycle_stage.execute(context_with_observability)
            
            assert result['overall_success'] is True
            assert result['test_results'][0]['iterations'] == 2
            assert result['test_results'][0]['files_fixed'] == 2
    
    @pytest.mark.asyncio
    async def test_stops_at_max_iterations(
        self,
        repair_cycle_stage,
        context_with_observability,
        failing_test_result
    ):
        """Test that cycle stops at max iterations"""
        # Always return failures
        with patch.object(
            repair_cycle_stage,
            '_run_tests',
            new_callable=AsyncMock,
            return_value=failing_test_result
        ), patch.object(
            repair_cycle_stage,
            '_fix_failures_by_file',
            new_callable=AsyncMock,
            return_value=2
        ):
            result = await repair_cycle_stage.execute(context_with_observability)
            
            assert result['overall_success'] is False
            # Should hit max_iterations (5 in unit_test_config)
            assert result['test_results'][0]['iterations'] == 5
            assert result['test_results'][0]['error'] == "Max iterations reached"
    
    @pytest.mark.asyncio
    async def test_circuit_breaker_triggers(
        self,
        context_with_observability,
        failing_test_result,
        unit_test_config
    ):
        """Test that circuit breaker stops execution"""
        # Create stage with low max_total_agent_calls
        stage = RepairCycleStage(
            name="testing",
            test_configs=[unit_test_config],
            agent_name="senior_software_engineer",
            max_total_agent_calls=3  # Very low for testing
        )
        
        # Mock to increment agent call count
        async def mock_run_tests(*args):
            stage._agent_call_count += 1
            return failing_test_result
        
        with patch.object(
            stage,
            '_run_tests',
            side_effect=mock_run_tests
        ), patch.object(
            stage,
            '_fix_failures_by_file',
            new_callable=AsyncMock,
            return_value=2
        ):
            result = await stage.execute(context_with_observability)
            
            assert result['overall_success'] is False
            assert 'Circuit breaker' in result['test_results'][0]['error']


class TestWarningHandling:
    """Test warning review and fix logic"""
    
    @pytest.mark.asyncio
    async def test_reviews_warnings_when_configured(
        self,
        repair_cycle_stage,
        context_with_observability,
        test_result_with_warnings
    ):
        """Test that warnings are reviewed when review_warnings=True"""
        # Return result with warnings
        with patch.object(
            repair_cycle_stage,
            '_run_tests',
            new_callable=AsyncMock,
            side_effect=[test_result_with_warnings, test_result_with_warnings]
        ), patch.object(
            repair_cycle_stage,
            '_handle_warnings',
            new_callable=AsyncMock,
            return_value=2
        ) as mock_handle_warnings:
            result = await repair_cycle_stage.execute(context_with_observability)
            
            # Should have called warning handler
            assert mock_handle_warnings.called
            assert result['test_results'][0]['warnings_reviewed'] == 2
    
    @pytest.mark.asyncio
    async def test_skips_warnings_when_disabled(
        self,
        context_with_observability,
        test_result_with_warnings
    ):
        """Test that warnings are skipped when review_warnings=False"""
        config = RepairTestRunConfig(
            test_type=RepairTestType.INTEGRATION,
            command="pytest tests/integration/",
            review_warnings=False  # Disabled
        )
        
        stage = RepairCycleStage(
            name="testing",
            test_configs=[config],
            agent_name="senior_software_engineer"
        )
        
        with patch.object(
            stage,
            '_run_tests',
            new_callable=AsyncMock,
            return_value=test_result_with_warnings
        ), patch.object(
            stage,
            '_handle_warnings',
            new_callable=AsyncMock
        ) as mock_handle_warnings:
            result = await stage.execute(context_with_observability)
            
            # Should NOT have called warning handler
            assert not mock_handle_warnings.called
            assert result['test_results'][0]['warnings_reviewed'] == 0


class TestJSONExtraction:
    """Test JSON extraction from agent responses"""
    
    def test_extract_plain_json(self, repair_cycle_stage):
        """Test extracting plain JSON"""
        response = '{"passed": 10, "failed": 0, "warnings": 0, "failures": [], "warning_list": []}'
        result = repair_cycle_stage._extract_json_from_response(response)
        
        assert result['passed'] == 10
        assert result['failed'] == 0
    
    def test_extract_from_markdown_code_block(self, repair_cycle_stage):
        """Test extracting JSON from markdown code block"""
        response = '''Here are the results:
```json
{"passed": 8, "failed": 2, "warnings": 1, "failures": [], "warning_list": []}
```
All done!'''
        result = repair_cycle_stage._extract_json_from_response(response)
        
        assert result['passed'] == 8
        assert result['failed'] == 2
    
    def test_extract_from_plain_code_block(self, repair_cycle_stage):
        """Test extracting JSON from plain code block"""
        response = '''```
{"passed": 5, "failed": 5, "warnings": 0, "failures": [], "warning_list": []}
```'''
        result = repair_cycle_stage._extract_json_from_response(response)
        
        assert result['passed'] == 5
        assert result['failed'] == 5
    
    def test_extract_from_embedded_json(self, repair_cycle_stage):
        """Test extracting JSON embedded in text"""
        response = '''I ran the tests and got these results:
        {"passed": 10, "failed": 0, "warnings": 2, "failures": [], "warning_list": []}
        Looking good!'''
        result = repair_cycle_stage._extract_json_from_response(response)
        
        assert result['passed'] == 10
        assert result['warnings'] == 2
    
    def test_raises_on_invalid_response(self, repair_cycle_stage):
        """Test that ValueError is raised for invalid response"""
        response = "No JSON here, just plain text!"
        
        with pytest.raises(ValueError, match="No valid JSON found"):
            repair_cycle_stage._extract_json_from_response(response)


class TestCheckpointing:
    """Test checkpoint support"""
    
    @pytest.mark.asyncio
    async def test_checkpoint_called_at_interval(
        self,
        context_with_observability,
        failing_test_result,
        unit_test_config
    ):
        """Test that checkpoints are created at configured interval"""
        stage = RepairCycleStage(
            name="testing",
            test_configs=[unit_test_config],
            agent_name="senior_software_engineer",
            checkpoint_interval=2  # Checkpoint every 2 iterations
        )
        
        checkpoint_calls = []
        
        async def mock_checkpoint(*args, **kwargs):
            checkpoint_calls.append(args)
        
        # Run 5 iterations: 1-4 fail (checkpoint at 2 and 4), 5 passes
        iteration_count = 0
        async def mock_run_tests(*args):
            nonlocal iteration_count
            iteration_count += 1
            if iteration_count >= 5:
                # Return passing on 5th iteration
                return RepairTestResult(
                    test_type=RepairTestType.UNIT,
                    iteration=iteration_count,
                    passed=10,
                    failed=0,
                    warnings=0,
                    failures=[],
                    warning_list=[],
                    raw_output="All passed",
                    timestamp="2025-10-11T12:00:00.000000Z"
                )
            return failing_test_result
        
        with patch.object(
            stage,
            '_run_tests',
            side_effect=mock_run_tests
        ), patch.object(
            stage,
            '_fix_failures_by_file',
            new_callable=AsyncMock,
            return_value=1
        ), patch.object(
            stage,
            '_checkpoint',
            side_effect=mock_checkpoint
        ):
            await stage.execute(context_with_observability)
            
            # Should have checkpointed at iterations 2 and 4 (before iteration 5 passed)
            assert len(checkpoint_calls) == 2


# ============================================================================
# Integration with Config Manager
# ============================================================================

class TestConfigIntegration:
    """Test integration with config manager"""
    
    def test_stage_creation_from_config(self):
        """Test creating RepairCycleStage from config data"""
        # This would be tested in integration tests with actual config manager
        # Here we just verify the expected structure
        from pipeline.repair_cycle import RepairTestType, RepairTestRunConfig
        
        # Simulate config loading
        config_data = {
            'type': 'unit',
            'command': 'pytest tests/unit/',
            'timeout': 600,
            'max_iterations': 5,
            'review_warnings': True,
            'max_file_iterations': 3
        }
        
        test_config = RepairTestRunConfig(
            test_type=RepairTestType(config_data['type']),
            command=config_data['command'],
            timeout=config_data['timeout'],
            max_iterations=config_data['max_iterations'],
            review_warnings=config_data['review_warnings'],
            max_file_iterations=config_data['max_file_iterations']
        )
        
        assert test_config.test_type == RepairTestType.UNIT
        assert test_config.command == 'pytest tests/unit/'
        assert test_config.max_iterations == 5
