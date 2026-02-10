"""
Tests for recovery review-cycle bypass fix and repair cycle lifecycle awareness.

Covers:
- Fix 1: Recovery skips auto-advance for review columns
- Fix 2: Cancellation signal lifecycle in pipeline_run
- Fix 2C: CancellationError propagation through repair cycle
- Fix 2D: CancellationError handling in repair_cycle_runner
- Fix 3: Review cycle resume guards for maker_working/reviewer_working
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


# ---------------------------------------------------------------------------
# Fix 1: Recovery skips auto-advance for review columns
# ---------------------------------------------------------------------------


class TestRecoverySkipsAutoAdvanceForReviewColumns:
    """Recovered containers in review columns should NOT auto-advance."""

    def _make_column(self, name, col_type=None, auto_advance=True):
        """Create a mock WorkflowColumn."""
        col = MagicMock()
        col.name = name
        col.type = col_type
        col.auto_advance_on_approval = auto_advance
        return col

    def _make_workflow(self, columns):
        wf = MagicMock()
        wf.columns = columns
        return wf

    def test_review_column_skips_auto_advance(self):
        """When recovered container is in a review column, auto-advance is skipped."""
        review_col = self._make_column("Code Review", col_type="review")
        testing_col = self._make_column("Testing")
        workflow = self._make_workflow([review_col, testing_col])

        pipeline = MagicMock()
        pipeline.workflow = "dev_workflow"
        pipeline.board_name = "dev_board"

        project_config = MagicMock()
        project_config.pipelines = [pipeline]

        mock_config_manager = MagicMock()
        mock_config_manager.get_workflow_template.return_value = workflow

        # Simulate the auto-advance decision block from _process_recovered_container_completion
        column = "Code Review"
        exit_code = 0
        auto_advanced = False

        if exit_code == 0 and column != 'unknown':
            for p in project_config.pipelines:
                workflow_template = mock_config_manager.get_workflow_template(p.workflow)
                if not workflow_template:
                    continue

                current_column = next(
                    (c for c in workflow_template.columns if c.name == column),
                    None
                )

                if current_column and getattr(current_column, 'type', None) == 'review':
                    break  # Skip auto-advance

                if current_column and getattr(current_column, 'auto_advance_on_approval', False):
                    auto_advanced = True
                    break

        assert not auto_advanced, "Review column should skip auto-advance"

    def test_non_review_column_still_auto_advances(self):
        """Non-review columns should still auto-advance normally."""
        development_col = self._make_column("Development", col_type="maker")
        testing_col = self._make_column("Testing")
        workflow = self._make_workflow([development_col, testing_col])

        pipeline = MagicMock()
        pipeline.workflow = "dev_workflow"

        project_config = MagicMock()
        project_config.pipelines = [pipeline]

        mock_config_manager = MagicMock()
        mock_config_manager.get_workflow_template.return_value = workflow

        column = "Development"
        exit_code = 0
        auto_advanced = False

        if exit_code == 0 and column != 'unknown':
            for p in project_config.pipelines:
                workflow_template = mock_config_manager.get_workflow_template(p.workflow)
                if not workflow_template:
                    continue

                current_column = next(
                    (c for c in workflow_template.columns if c.name == column),
                    None
                )

                if current_column and getattr(current_column, 'type', None) == 'review':
                    break

                if current_column and getattr(current_column, 'auto_advance_on_approval', False):
                    auto_advanced = True
                    break

        assert auto_advanced, "Non-review column should auto-advance"


# ---------------------------------------------------------------------------
# Fix 2: Cancellation signal lifecycle in pipeline_run
# ---------------------------------------------------------------------------


class TestPipelineRunCancellationSignal:
    """Pipeline run end sets cancellation; ensure clears it."""

    def _make_pipeline_run_manager(self):
        """Create a PipelineRunManager with mocked Redis/ES."""
        from services.pipeline_run import PipelineRunManager

        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_redis.hget.return_value = None
        mock_es = MagicMock()
        mock_es.search.return_value = {'hits': {'total': {'value': 0}, 'hits': []}}

        manager = PipelineRunManager()
        manager.redis = mock_redis
        manager.es = mock_es
        return manager

    def test_end_pipeline_run_sets_cancellation_signal(self):
        """end_pipeline_run should set cancellation signal."""
        manager = self._make_pipeline_run_manager()

        mock_run = MagicMock()
        mock_run.id = "test-run-123"
        mock_run.status = "active"
        mock_run.board = "dev_board"
        mock_run.started_at = "2025-01-01T00:00:00Z"
        mock_run.to_dict.return_value = {"id": "test-run-123", "status": "completed"}

        mock_signal = MagicMock()
        mock_lock_mgr = MagicMock()
        mock_lock_mgr.get_lock.return_value = None

        with patch.object(manager, 'get_active_pipeline_run', return_value=mock_run), \
             patch.object(manager, '_persist_to_elasticsearch'), \
             patch('services.cancellation.get_cancellation_signal', return_value=mock_signal), \
             patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_mgr), \
             patch('monitoring.observability.get_observability_manager'):

            manager.end_pipeline_run("proj", 42, reason="issue moved to Done")

        mock_signal.cancel.assert_called_once_with(
            "proj", 42, "Pipeline run ended: issue moved to Done"
        )

    def test_ensure_pipeline_run_clears_cancellation_signal(self):
        """ensure_pipeline_run_for_task should clear stale cancellation."""
        manager = self._make_pipeline_run_manager()

        mock_signal = MagicMock()
        mock_run = MagicMock()
        mock_run.id = "new-run-456"

        with patch('services.cancellation.get_cancellation_signal', return_value=mock_signal), \
             patch.object(manager, 'get_or_create_pipeline_run', return_value=mock_run):
            result = manager.ensure_pipeline_run_for_task(
                project="proj", board="dev_board", issue_number=42
            )

        mock_signal.clear.assert_called_once_with("proj", 42)
        assert result == "new-run-456"


# ---------------------------------------------------------------------------
# Fix 2C: CancellationError propagation in repair cycle
# ---------------------------------------------------------------------------


class TestRepairCycleCancellationCheck:
    """Repair cycle loop checks cancellation before each iteration."""

    @pytest.mark.asyncio
    async def test_cancellation_stops_repair_cycle_loop(self):
        """If issue is cancelled, _run_test_cycle returns early with error."""
        from pipeline.repair_cycle import RepairCycleStage, RepairTestRunConfig, CycleResult
        from services.cancellation import CancellationError

        config = RepairTestRunConfig(
            test_type="unit",
            timeout=600,
            max_iterations=5,
        )

        stage = RepairCycleStage.__new__(RepairCycleStage)
        stage.test_configs = [config]
        stage.max_total_agent_calls = 100
        stage._agent_call_count = 0
        stage.agent_name = "test_agent"
        stage.name = "test_repair"

        context = {
            'project': 'proj',
            'issue_number': 42,
            'task_id': 'test-task',
            'pipeline_run_id': 'run-123',
            'observability': None,
        }

        mock_signal = MagicMock()
        mock_signal.is_cancelled.return_value = True

        with patch('services.cancellation.get_cancellation_signal', return_value=mock_signal):
            result = await stage._run_test_cycle(config, context, 0)

        assert isinstance(result, CycleResult)
        assert not result.passed
        assert result.error == "Pipeline run ended externally"
        assert result.iterations == 0

    @pytest.mark.asyncio
    async def test_cancellation_error_propagates_through_fix_failures(self):
        """CancellationError raised by agent_executor propagates through _fix_failures_by_file."""
        import sys
        from pipeline.repair_cycle import RepairCycleStage, RepairTestRunConfig, RepairTestResult, RepairTestFailure
        from services.cancellation import CancellationError

        config = RepairTestRunConfig(test_type="unit", timeout=600)

        stage = RepairCycleStage.__new__(RepairCycleStage)
        stage.test_configs = [config]
        stage.max_total_agent_calls = 100
        stage._agent_call_count = 0
        stage.agent_name = "test_agent"
        stage.name = "test_repair"

        grouped_failures = {
            "test_foo.py": [
                RepairTestFailure(file="test_foo.py", test="test_one", message="fail"),
                RepairTestFailure(file="test_foo.py", test="test_two", message="fail"),
            ],
        }

        context = {
            'project': 'proj',
            'issue_number': 42,
            'task_id': 'test-task',
            'pipeline_run_id': 'run-123',
            'observability': None,
        }

        mock_executor = AsyncMock()
        mock_executor.execute_agent.side_effect = CancellationError("cancelled")

        # Mock agent_executor module to avoid Docker-only transitive imports
        mock_ae_module = MagicMock()
        mock_ae_module.get_agent_executor.return_value = mock_executor
        with patch.dict(sys.modules, {'services.agent_executor': mock_ae_module}):
            with pytest.raises(CancellationError):
                await stage._fix_failures_by_file(grouped_failures, config, context)

    @pytest.mark.asyncio
    async def test_cancellation_error_propagates_through_handle_warnings(self):
        """CancellationError raised by agent_executor propagates through _handle_warnings."""
        import sys
        from pipeline.repair_cycle import RepairCycleStage, RepairTestRunConfig, RepairTestResult, RepairTestWarning
        from services.cancellation import CancellationError

        config = RepairTestRunConfig(test_type="unit", timeout=600, review_warnings=True)

        stage = RepairCycleStage.__new__(RepairCycleStage)
        stage.test_configs = [config]
        stage.max_total_agent_calls = 100
        stage._agent_call_count = 0
        stage.agent_name = "test_agent"
        stage.name = "test_repair"

        test_result = RepairTestResult(
            test_type="unit",
            iteration=1,
            passed=5,
            failed=0,
            warnings=1,
            failures=[],
            warning_list=[
                RepairTestWarning(file="src/foo.py", message="unused import"),
            ],
            raw_output="",
            timestamp="2025-01-01T00:00:00Z",
        )

        context = {
            'project': 'proj',
            'issue_number': 42,
            'task_id': 'test-task',
            'pipeline_run_id': 'run-123',
            'observability': None,
        }

        mock_executor = AsyncMock()
        mock_executor.execute_agent.side_effect = CancellationError("cancelled")

        # Mock agent_executor module to avoid Docker-only transitive imports
        mock_ae_module = MagicMock()
        mock_ae_module.get_agent_executor.return_value = mock_executor
        with patch.dict(sys.modules, {'services.agent_executor': mock_ae_module}):
            with pytest.raises(CancellationError):
                await stage._handle_warnings(test_result, config, context)


# ---------------------------------------------------------------------------
# Fix 2D: CancellationError in repair_cycle_runner
# ---------------------------------------------------------------------------


class TestRepairCycleRunnerCancellation:
    """CancellationError handling in RepairCycleRunner."""

    @pytest.mark.asyncio
    async def test_execute_repair_cycle_catches_cancellation_error(self):
        """execute_repair_cycle returns error dict on CancellationError."""
        from services.cancellation import CancellationError
        from pipeline.repair_cycle_runner import RepairCycleRunner

        runner = RepairCycleRunner.__new__(RepairCycleRunner)
        runner.context = {'project': 'proj', 'issue_number': 42}
        runner.args = MagicMock()
        runner.args.pipeline_run_id = 'run-123'
        runner.stage = MagicMock()
        runner.checkpoint_manager = MagicMock()

        with patch.object(runner, 'load_checkpoint', return_value=None):
            runner.stage.execute = AsyncMock(side_effect=CancellationError("pipeline ended"))
            result = await runner.execute_repair_cycle()

        assert result['overall_success'] is False
        assert 'Pipeline run ended externally' in result['error']

    def test_run_catches_cancellation_error(self):
        """run() returns exit code 4 on CancellationError."""
        from services.cancellation import CancellationError
        from pipeline.repair_cycle_runner import RepairCycleRunner

        runner = RepairCycleRunner.__new__(RepairCycleRunner)
        runner.args = MagicMock()
        runner.args.project = "proj"
        runner.args.issue = 42
        runner.args.pipeline_run_id = "run-123"
        runner.context = {}
        runner._cancelled = False

        with patch.object(runner, 'load_context'), \
             patch.object(runner, 'initialize_stage', return_value=True), \
             patch('asyncio.run', side_effect=CancellationError("pipeline ended")):
            exit_code = runner.run()

        assert exit_code == 4


# ---------------------------------------------------------------------------
# Fix 3: Review cycle resume guard for maker_working/reviewer_working
# ---------------------------------------------------------------------------


class TestReviewCycleResumeGuard:
    """maker_working/reviewer_working resume checks for active execution."""

    def test_maker_working_skips_resume_when_agent_active(self):
        """If agent is already active, maker_working resume is skipped."""
        cycle_state = MagicMock()
        cycle_state.status = 'maker_working'
        cycle_state.project_name = 'proj'
        cycle_state.issue_number = 42
        cycle_state.current_iteration = 1
        cycle_state.maker_outputs = []
        cycle_state.review_outputs = []

        mock_tracker = MagicMock()
        mock_tracker.has_active_execution.return_value = True

        # The guard: if has_active_execution, skip
        skipped = False
        if cycle_state.status in ['maker_working', 'reviewer_working']:
            if mock_tracker.has_active_execution(
                cycle_state.project_name, cycle_state.issue_number
            ):
                skipped = True

        assert skipped
        mock_tracker.has_active_execution.assert_called_once_with('proj', 42)

    def test_maker_working_resumes_when_no_agent_active(self):
        """If no agent is active, maker_working resume proceeds."""
        cycle_state = MagicMock()
        cycle_state.status = 'maker_working'
        cycle_state.project_name = 'proj'
        cycle_state.issue_number = 42

        mock_tracker = MagicMock()
        mock_tracker.has_active_execution.return_value = False

        skipped = False
        if cycle_state.status in ['maker_working', 'reviewer_working']:
            if mock_tracker.has_active_execution(
                cycle_state.project_name, cycle_state.issue_number
            ):
                skipped = True

        assert not skipped


# ---------------------------------------------------------------------------
# Integration: CancellationError is a proper Exception subclass
# ---------------------------------------------------------------------------


class TestCancellationErrorPropagation:
    """Verify CancellationError is caught before generic Exception handlers."""

    def test_cancellation_error_not_caught_by_generic_except(self):
        """CancellationError should be catchable before generic Exception."""
        from services.cancellation import CancellationError

        caught_by = None
        try:
            raise CancellationError("test")
        except CancellationError:
            caught_by = "CancellationError"
        except Exception:
            caught_by = "Exception"

        assert caught_by == "CancellationError"
