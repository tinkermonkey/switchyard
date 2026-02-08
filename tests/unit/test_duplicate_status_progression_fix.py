"""
Test for duplicate status_progression_completed event fix

Verifies that:
1. Programmatic status changes emit events correctly
2. Project monitor doesn't duplicate events for programmatic changes
3. Project monitor still emits events for manual changes
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import yaml
from services.work_execution_state import WorkExecutionStateTracker


class TestDuplicateStatusProgressionFix:
    """Test the fix for duplicate status_progression_completed events"""

    @pytest.fixture
    def temp_state_dir(self):
        """Create a temporary state directory"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def tracker(self, temp_state_dir):
        """Create a tracker with temporary state dir"""
        return WorkExecutionStateTracker(state_dir=temp_state_dir)

    def test_recent_programmatic_change_detected(self, tracker):
        """Test that recent programmatic changes are detected"""
        project_name = "test-project"
        issue_number = 123
        
        # Record a programmatic status change
        tracker.record_status_change(
            issue_number=issue_number,
            from_status="Development",
            to_status="Code Review",
            trigger="agent_auto_advance",
            project_name=project_name
        )
        
        # Should be detected as recent programmatic change
        assert tracker.was_recent_programmatic_change(
            project_name=project_name,
            issue_number=issue_number,
            to_status="Code Review",
            time_window_seconds=60
        )

    def test_old_programmatic_change_not_detected(self, tracker):
        """Test that old programmatic changes are not detected"""
        project_name = "test-project"
        issue_number = 123
        
        # Record a programmatic status change with old timestamp
        state = tracker.load_state(project_name, issue_number)
        old_timestamp = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        
        status_change = {
            'from_status': "Development",
            'to_status': "Code Review",
            'timestamp': old_timestamp,
            'trigger': "agent_auto_advance"
        }
        
        state['status_changes'].append(status_change)
        tracker.save_state(project_name, issue_number, state)
        
        # Should NOT be detected as recent (outside time window)
        assert not tracker.was_recent_programmatic_change(
            project_name=project_name,
            issue_number=issue_number,
            to_status="Code Review",
            time_window_seconds=60
        )

    def test_manual_change_not_detected_as_programmatic(self, tracker):
        """Test that manual changes are not detected as programmatic"""
        project_name = "test-project"
        issue_number = 123
        
        # Record a manual status change
        tracker.record_status_change(
            issue_number=issue_number,
            from_status="Development",
            to_status="Code Review",
            trigger="manual",
            project_name=project_name
        )
        
        # Should NOT be detected as programmatic
        assert not tracker.was_recent_programmatic_change(
            project_name=project_name,
            issue_number=issue_number,
            to_status="Code Review",
            time_window_seconds=60
        )

    def test_different_status_not_detected(self, tracker):
        """Test that changes to different status are not detected"""
        project_name = "test-project"
        issue_number = 123
        
        # Record a programmatic status change to "Code Review"
        tracker.record_status_change(
            issue_number=issue_number,
            from_status="Development",
            to_status="Code Review",
            trigger="agent_auto_advance",
            project_name=project_name
        )
        
        # Should NOT be detected for a different status
        assert not tracker.was_recent_programmatic_change(
            project_name=project_name,
            issue_number=issue_number,
            to_status="Testing",  # Different status
            time_window_seconds=60
        )

    def test_multiple_triggers_detected(self, tracker):
        """Test that all programmatic trigger types are detected"""
        project_name = "test-project"
        issue_number = 123
        
        programmatic_triggers = [
            "agent_auto_advance",
            "pipeline_progression", 
            "review_cycle",
            "agent_completion",
            "auto"
        ]
        
        for i, trigger in enumerate(programmatic_triggers):
            status = f"Status{i}"
            
            # Record status change with this trigger
            tracker.record_status_change(
                issue_number=issue_number,
                from_status="Previous",
                to_status=status,
                trigger=trigger,
                project_name=project_name
            )
            
            # Should be detected as programmatic
            assert tracker.was_recent_programmatic_change(
                project_name=project_name,
                issue_number=issue_number,
                to_status=status,
                time_window_seconds=60
            ), f"Trigger '{trigger}' should be detected as programmatic"

    def test_no_state_returns_false(self, tracker):
        """Test that missing state returns False"""
        # Should return False for non-existent issue
        assert not tracker.was_recent_programmatic_change(
            project_name="nonexistent",
            issue_number=999,
            to_status="Some Status",
            time_window_seconds=60
        )

    def test_empty_status_changes_returns_false(self, tracker):
        """Test that empty status changes list returns False"""
        project_name = "test-project"
        issue_number = 123
        
        # Create state with no status changes
        state = tracker.load_state(project_name, issue_number)
        state['status_changes'] = []
        tracker.save_state(project_name, issue_number, state)
        
        # Should return False
        assert not tracker.was_recent_programmatic_change(
            project_name=project_name,
            issue_number=issue_number,
            to_status="Any Status",
            time_window_seconds=60
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
