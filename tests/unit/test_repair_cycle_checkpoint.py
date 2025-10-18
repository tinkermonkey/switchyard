"""
Unit tests for repair cycle checkpoint system
"""

import json
import pytest
from pathlib import Path
from pipeline.repair_cycle_checkpoint import (
    RepairCycleCheckpoint,
    create_checkpoint_state
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
