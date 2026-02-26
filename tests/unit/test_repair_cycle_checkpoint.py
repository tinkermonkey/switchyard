"""
Unit tests for repair cycle checkpoint system and systemic failure detection
"""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from pipeline.repair_cycle_checkpoint import (
    RepairCycleCheckpoint,
    create_checkpoint_state
)
from pipeline.repair_cycle import (
    RepairCycleStage,
    RepairTestRunConfig,
    RepairTestResult,
    RepairTestFailure,
    SystemicAnalysisResult,
)


@pytest.fixture
def temp_project_dir(tmp_path):
    """Create a temporary project directory"""
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()
    return str(project_dir)


@pytest.fixture
def checkpoint_manager(temp_project_dir):
    """Create a checkpoint manager"""
    return RepairCycleCheckpoint(temp_project_dir)


class TestRepairCycleCheckpoint:
    """Test checkpoint save/load functionality"""

    def test_create_checkpoint_state(self):
        """Test creating checkpoint state dict"""
        state = create_checkpoint_state(
            project="test_project",
            issue_number=123,
            pipeline_run_id="abc123",
            stage_name="Testing",
            test_type="unit",
            test_type_index=0,
            iteration=3,
            agent_call_count=15,
            files_fixed=["test_user.py", "test_auth.py"]
        )

        assert state["project"] == "test_project"
        assert state["issue_number"] == 123
        assert state["pipeline_run_id"] == "abc123"
        assert state["stage_name"] == "Testing"
        assert state["test_type"] == "unit"
        assert state["test_type_index"] == 0
        assert state["iteration"] == 3
        assert state["agent_call_count"] == 15
        assert state["files_fixed"] == ["test_user.py", "test_auth.py"]

    def test_save_checkpoint(self, checkpoint_manager, temp_project_dir):
        """Test saving checkpoint"""
        state = create_checkpoint_state(
            project="test_project",
            issue_number=123,
            pipeline_run_id="abc123",
            stage_name="Testing",
            test_type="unit",
            test_type_index=0,
            iteration=3,
            agent_call_count=15,
            files_fixed=["test_user.py"]
        )

        result = checkpoint_manager.save_checkpoint(state)
        assert result is True

        # Verify file exists
        checkpoint_file = Path(temp_project_dir) / ".repair_cycle_checkpoint.json"
        assert checkpoint_file.exists()

        # Verify content
        with open(checkpoint_file, 'r') as f:
            saved = json.load(f)

        assert saved["version"] == "1.0"
        assert saved["project"] == "test_project"
        assert saved["iteration"] == 3
        assert saved["agent_call_count"] == 15
        assert "checkpoint_time" in saved

    def test_load_checkpoint(self, checkpoint_manager):
        """Test loading checkpoint"""
        # Save first
        state = create_checkpoint_state(
            project="test_project",
            issue_number=123,
            pipeline_run_id="abc123",
            stage_name="Testing",
            test_type="unit",
            test_type_index=0,
            iteration=5,
            agent_call_count=20,
            files_fixed=["test_user.py", "test_auth.py"]
        )

        checkpoint_manager.save_checkpoint(state)

        # Load
        loaded = checkpoint_manager.load_checkpoint()
        assert loaded is not None
        assert loaded["project"] == "test_project"
        assert loaded["iteration"] == 5
        assert loaded["agent_call_count"] == 20
        assert loaded["files_fixed"] == ["test_user.py", "test_auth.py"]

    def test_load_nonexistent_checkpoint(self, checkpoint_manager):
        """Test loading when no checkpoint exists"""
        loaded = checkpoint_manager.load_checkpoint()
        assert loaded is None

    def test_checkpoint_backup(self, checkpoint_manager, temp_project_dir):
        """Test that backup is created on second save"""
        state1 = create_checkpoint_state(
            project="test_project",
            issue_number=123,
            pipeline_run_id="abc123",
            stage_name="Testing",
            test_type="unit",
            test_type_index=0,
            iteration=1,
            agent_call_count=5,
            files_fixed=[]
        )

        checkpoint_manager.save_checkpoint(state1)

        # Save again with different state
        state2 = create_checkpoint_state(
            project="test_project",
            issue_number=123,
            pipeline_run_id="abc123",
            stage_name="Testing",
            test_type="unit",
            test_type_index=0,
            iteration=2,
            agent_call_count=10,
            files_fixed=["test_user.py"]
        )

        checkpoint_manager.save_checkpoint(state2)

        # Verify backup exists
        backup_file = Path(temp_project_dir) / ".repair_cycle_checkpoint.backup.json"
        assert backup_file.exists()

        # Verify backup contains first state
        with open(backup_file, 'r') as f:
            backup = json.load(f)
        assert backup["iteration"] == 1
        assert backup["agent_call_count"] == 5

    def test_clear_checkpoint(self, checkpoint_manager, temp_project_dir):
        """Test clearing checkpoint"""
        state = create_checkpoint_state(
            project="test_project",
            issue_number=123,
            pipeline_run_id="abc123",
            stage_name="Testing",
            test_type="unit",
            test_type_index=0,
            iteration=3,
            agent_call_count=15,
            files_fixed=[]
        )

        checkpoint_manager.save_checkpoint(state)

        # Clear
        result = checkpoint_manager.clear_checkpoint()
        assert result is True

        # Verify files removed
        checkpoint_file = Path(temp_project_dir) / ".repair_cycle_checkpoint.json"
        assert not checkpoint_file.exists()

    def test_checkpoint_exists(self, checkpoint_manager):
        """Test checking if checkpoint exists"""
        # Initially no checkpoint
        assert not checkpoint_manager.checkpoint_exists()

        # Save checkpoint
        state = create_checkpoint_state(
            project="test_project",
            issue_number=123,
            pipeline_run_id="abc123",
            stage_name="Testing",
            test_type="unit",
            test_type_index=0,
            iteration=1,
            agent_call_count=5,
            files_fixed=[]
        )

        checkpoint_manager.save_checkpoint(state)

        # Now checkpoint exists
        assert checkpoint_manager.checkpoint_exists()

    def test_get_checkpoint_age(self, checkpoint_manager):
        """Test getting checkpoint age"""
        import time

        # No checkpoint
        age = checkpoint_manager.get_checkpoint_age_seconds()
        assert age is None

        # Save checkpoint
        state = create_checkpoint_state(
            project="test_project",
            issue_number=123,
            pipeline_run_id="abc123",
            stage_name="Testing",
            test_type="unit",
            test_type_index=0,
            iteration=1,
            agent_call_count=5,
            files_fixed=[]
        )

        checkpoint_manager.save_checkpoint(state)
        time.sleep(0.1)

        # Get age
        age = checkpoint_manager.get_checkpoint_age_seconds()
        assert age is not None
        assert age >= 0.1  # At least 100ms old
        assert age < 2.0   # Less than 2 seconds old

    def test_atomic_write(self, checkpoint_manager, temp_project_dir):
        """Test that save is atomic (temp file then move)"""
        state = create_checkpoint_state(
            project="test_project",
            issue_number=123,
            pipeline_run_id="abc123",
            stage_name="Testing",
            test_type="unit",
            test_type_index=0,
            iteration=1,
            agent_call_count=5,
            files_fixed=[]
        )

        checkpoint_manager.save_checkpoint(state)

        # Verify temp file is gone (moved to actual checkpoint)
        temp_file = Path(temp_project_dir) / ".repair_cycle_checkpoint.tmp"
        assert not temp_file.exists()

        # Verify actual checkpoint exists
        checkpoint_file = Path(temp_project_dir) / ".repair_cycle_checkpoint.json"
        assert checkpoint_file.exists()

    def test_backup_recovery(self, checkpoint_manager, temp_project_dir):
        """Test loading from backup if primary is corrupted"""
        # Save valid checkpoint
        state = create_checkpoint_state(
            project="test_project",
            issue_number=123,
            pipeline_run_id="abc123",
            stage_name="Testing",
            test_type="unit",
            test_type_index=0,
            iteration=1,
            agent_call_count=5,
            files_fixed=[]
        )

        checkpoint_manager.save_checkpoint(state)

        # Save another to create backup
        state2 = create_checkpoint_state(
            project="test_project",
            issue_number=123,
            pipeline_run_id="abc123",
            stage_name="Testing",
            test_type="unit",
            test_type_index=0,
            iteration=2,
            agent_call_count=10,
            files_fixed=["test_user.py"]
        )

        checkpoint_manager.save_checkpoint(state2)

        # Corrupt primary checkpoint
        checkpoint_file = Path(temp_project_dir) / ".repair_cycle_checkpoint.json"
        with open(checkpoint_file, 'w') as f:
            f.write("CORRUPTED JSON{{{")

        # Load should fall back to backup
        loaded = checkpoint_manager.load_checkpoint()
        assert loaded is not None
        assert loaded["iteration"] == 1  # From backup
        assert loaded["agent_call_count"] == 5


# ---------------------------------------------------------------------------
# Systemic failure detection tests
# ---------------------------------------------------------------------------

def _make_test_result(failed: int = 3) -> RepairTestResult:
    """Helper: build a RepairTestResult with N failures across N files."""
    failures = [
        RepairTestFailure(file=f"src/comp{i}.tsx", test="test_render", message="import error")
        for i in range(failed)
    ]
    return RepairTestResult(
        test_type="unit",
        iteration=1,
        passed=0,
        failed=failed,
        warnings=0,
        failures=failures,
        warning_list=[],
        raw_output="",
        timestamp="2026-01-01T00:00:00Z",
    )


def _make_stage() -> RepairCycleStage:
    """Helper: create a RepairCycleStage without Docker-only imports."""
    return RepairCycleStage(
        name="testing",
        test_configs=[RepairTestRunConfig(test_type="unit")],
        agent_name="senior_software_engineer",
    )


class TestSystemicFailureDetection:
    """Tests for systemic failure analysis and routing in RepairCycleStage."""

    def test_systemic_analysis_done_flag_initialized_false(self):
        """_systemic_analysis_done_for is an empty set on construction."""
        stage = _make_stage()
        assert stage._systemic_analysis_done_for == set()

    @pytest.mark.asyncio
    async def test_analyze_systemic_failures_called_on_first_iteration_with_failures(self):
        """_systemic_analysis_done_for set gates the analysis call per test type.

        Verifies that the guard logic in _run_test_cycle invokes
        _analyze_systemic_failures exactly once for a given test type, and adds
        the type to the set afterwards so subsequent iterations skip it.
        """
        stage = _make_stage()
        assert stage._systemic_analysis_done_for == set()

        no_issues = SystemicAnalysisResult(
            has_env_issues=False,
            has_systemic_code_issues=False,
            env_issue_description="",
            systemic_issue_description="",
            affected_files=[],
            raw_json={},
        )

        config = stage.test_configs[0]

        with patch.object(stage, "_analyze_systemic_failures", new=AsyncMock(return_value=no_issues)) as mock_analyze:
            # Replicate the guard block from _run_test_cycle for the first call
            test_result = _make_test_result(failed=2)
            grouped = test_result.group_failures_by_file()

            if config.test_type not in stage._systemic_analysis_done_for:
                stage._systemic_analysis_done_for.add(config.test_type)
                await stage._analyze_systemic_failures(
                    test_result, grouped, config, {}
                )

            mock_analyze.assert_awaited_once()
            assert config.test_type in stage._systemic_analysis_done_for

            # Simulate a second iteration — the guard must skip the call
            if config.test_type not in stage._systemic_analysis_done_for:
                await stage._analyze_systemic_failures(
                    test_result, grouped, config, {}
                )

            # Still only called once
            assert mock_analyze.await_count == 1

    @pytest.mark.asyncio
    async def test_systemic_fix_sub_cycle_skips_non_affected_files(self):
        """_fix_failures_by_file receives only the non-systemic files after systemic fix."""
        stage = _make_stage()

        # Systemic analysis says src/comp0.tsx and src/comp1.tsx are systemic;
        # src/comp2.tsx is an isolated issue.
        systemic_analysis = SystemicAnalysisResult(
            has_env_issues=False,
            has_systemic_code_issues=True,
            env_issue_description="",
            systemic_issue_description="Replace deprecated API across all files",
            affected_files=["src/comp0.tsx", "src/comp1.tsx"],
            raw_json={},
        )

        # After the systemic fix, only src/comp2.tsx remains failing
        remaining_result = RepairTestResult(
            test_type="unit",
            iteration=1,
            passed=0,
            failed=1,
            warnings=0,
            failures=[RepairTestFailure(file="src/comp2.tsx", test="test_render", message="other error")],
            warning_list=[],
            raw_output="",
            timestamp="2026-01-01T00:00:00Z",
        )

        with patch.object(stage, "_analyze_systemic_failures", new=AsyncMock(return_value=systemic_analysis)), \
             patch.object(stage, "_run_systemic_fix_sub_cycle", new=AsyncMock(return_value=remaining_result)) as mock_fix, \
             patch.object(stage, "_fix_failures_by_file", new=AsyncMock(return_value=1)) as mock_per_file, \
             patch.object(stage, "_checkpoint", new=AsyncMock()):

            # Simulate the block in _run_test_cycle that routes after analysis
            test_result = _make_test_result(failed=3)
            grouped = test_result.group_failures_by_file()

            config = stage.test_configs[0]
            if config.test_type not in stage._systemic_analysis_done_for:
                stage._systemic_analysis_done_for.add(config.test_type)
                analysis = await stage._analyze_systemic_failures(
                    test_result, grouped, config, {}
                )
                if analysis.has_systemic_code_issues:
                    test_result = await stage._run_systemic_fix_sub_cycle(
                        analysis, config, {}, 1, 1
                    )
                    grouped = test_result.group_failures_by_file()

            await stage._fix_failures_by_file(grouped, stage.test_configs[0], {})

            # Per-file fixer should only see the one remaining isolated file
            called_files = list(mock_per_file.call_args[0][0].keys())
            assert called_files == ["src/comp2.tsx"]
            assert "src/comp0.tsx" not in called_files
            assert "src/comp1.tsx" not in called_files

    @pytest.mark.asyncio
    async def test_systemic_analysis_not_repeated_on_second_iteration(self):
        """_analyze_systemic_failures is NOT called if test type is already in _systemic_analysis_done_for."""
        stage = _make_stage()
        config = stage.test_configs[0]
        stage._systemic_analysis_done_for = {config.test_type}  # Already ran for this type

        with patch.object(stage, "_analyze_systemic_failures", new=AsyncMock()) as mock_analyze:
            # Simulate the guard in _run_test_cycle
            if config.test_type not in stage._systemic_analysis_done_for:
                await stage._analyze_systemic_failures(MagicMock(), {}, MagicMock(), {})

            mock_analyze.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_systemic_analysis_runs_once_per_test_type(self):
        """_analyze_systemic_failures runs once per distinct test type, not once per repair cycle.

        With two test types ("compilation" and "unit"), the systemic check should
        run the first time each type is encountered and be skipped on repeat
        iterations of the same type.
        """
        stage = RepairCycleStage(
            name="testing",
            test_configs=[
                RepairTestRunConfig(test_type="compilation"),
                RepairTestRunConfig(test_type="unit"),
            ],
            agent_name="senior_software_engineer",
        )

        no_issues = SystemicAnalysisResult(
            has_env_issues=False,
            has_systemic_code_issues=False,
            env_issue_description="",
            systemic_issue_description="",
            affected_files=[],
            raw_json={},
        )

        with patch.object(stage, "_analyze_systemic_failures", new=AsyncMock(return_value=no_issues)) as mock_analyze:
            test_result = _make_test_result(failed=1)
            grouped = test_result.group_failures_by_file()

            compilation_config = stage.test_configs[0]
            unit_config = stage.test_configs[1]

            # First encounter of "compilation" — should run
            if compilation_config.test_type not in stage._systemic_analysis_done_for:
                stage._systemic_analysis_done_for.add(compilation_config.test_type)
                await stage._analyze_systemic_failures(test_result, grouped, compilation_config, {})

            assert mock_analyze.await_count == 1

            # Second encounter of "compilation" — should be skipped
            if compilation_config.test_type not in stage._systemic_analysis_done_for:
                await stage._analyze_systemic_failures(test_result, grouped, compilation_config, {})

            assert mock_analyze.await_count == 1

            # First encounter of "unit" — should run (different type)
            if unit_config.test_type not in stage._systemic_analysis_done_for:
                stage._systemic_analysis_done_for.add(unit_config.test_type)
                await stage._analyze_systemic_failures(test_result, grouped, unit_config, {})

            assert mock_analyze.await_count == 2
            assert stage._systemic_analysis_done_for == {"compilation", "unit"}

    @pytest.mark.asyncio
    async def test_analyze_systemic_failures_returns_no_issues_on_parse_failure(self):
        """_analyze_systemic_failures falls back to no-issues on JSON parse failure."""
        import sys

        stage = _make_stage()
        test_result = _make_test_result(failed=2)
        grouped = test_result.group_failures_by_file()
        context = {"project": "test-project", "observability": None}

        # Inject a mock for services.agent_executor into sys.modules so the
        # deferred `from services.agent_executor import get_agent_executor` inside
        # _analyze_systemic_failures never triggers the Docker-only pipeline.factory
        # → agents import chain.
        mock_executor = MagicMock()
        mock_executor.execute_agent = AsyncMock(
            return_value={"markdown_analysis": "This is not JSON at all!"}
        )
        mock_agent_executor_mod = MagicMock()
        mock_agent_executor_mod.get_agent_executor = MagicMock(return_value=mock_executor)

        with patch.dict(sys.modules, {"services.agent_executor": mock_agent_executor_mod}):
            result = await stage._analyze_systemic_failures(
                test_result, grouped, stage.test_configs[0], context
            )

        assert result.has_env_issues is False
        assert result.has_systemic_code_issues is False
        assert result.affected_files == []
