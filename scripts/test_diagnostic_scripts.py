#!/usr/bin/env python3
"""
Simple test to verify diagnostic scripts work correctly
"""

import sys
import os
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_checkpoint_inspector():
    """Test checkpoint inspector with mock data"""
    from scripts.inspect_checkpoint import CheckpointInspector

    # Create temporary directory with mock checkpoints
    with tempfile.TemporaryDirectory() as tmpdir:
        inspector = CheckpointInspector(checkpoints_dir=tmpdir, es_client=None)

        # Create mock checkpoint files
        pipeline_id = "test-pipeline-12345"

        for stage in range(3):
            checkpoint_data = {
                "pipeline_id": pipeline_id,
                "stage_index": stage,
                "timestamp": (datetime.now() - timedelta(hours=stage)).isoformat(),
                "context": {
                    "project": "test-project",
                    "issue_number": 42,
                    "board": "Test Board",
                    "previous_stage_output": "Some output from previous stage",
                    "metrics": {"success": True, "duration_ms": 1000 * (stage + 1)}
                }
            }

            checkpoint_file = Path(tmpdir) / f"{pipeline_id}_stage_{stage}.json"
            with open(checkpoint_file, 'w') as f:
                json.dump(checkpoint_data, f, indent=2)

        # Test finding checkpoints
        files = inspector.find_checkpoint_files(pipeline_id)
        assert len(files) == 3, f"Expected 3 checkpoint files, found {len(files)}"

        # Test getting latest checkpoint
        latest = inspector.get_latest_checkpoint(pipeline_id)
        assert latest is not None, "Latest checkpoint should not be None"
        assert latest['stage_index'] == 2, f"Expected stage 2, got {latest['stage_index']}"

        # Test recovery verification
        verification = inspector.verify_recovery(latest)
        assert verification['ready'], "Checkpoint should be ready for recovery"

        # Test listing recent checkpoints
        recent = inspector.list_recent_checkpoints()
        assert len(recent) == 1, f"Expected 1 recent checkpoint, found {len(recent)}"

        print("✓ CheckpointInspector tests passed")


def test_task_health_monitor():
    """Test task health monitor logic"""
    from scripts.inspect_task_health import TaskHealthMonitor
    from unittest.mock import Mock

    # Create mock Redis client
    mock_redis = Mock()
    monitor = TaskHealthMonitor(mock_redis)

    # Test stuck task detection
    now = datetime.now()
    tasks = [
        {
            'id': 'task1',
            'priority': 'high',
            'agent': 'test-agent',
            'project': 'test-project',
            'created_at': (now - timedelta(minutes=45)).isoformat(),
            'status': 'pending',
            'issue_number': 1,
            'context': {}
        },
        {
            'id': 'task2',
            'priority': 'medium',
            'agent': 'test-agent',
            'project': 'test-project',
            'created_at': (now - timedelta(minutes=15)).isoformat(),
            'status': 'pending',
            'issue_number': 2,
            'context': {}
        }
    ]

    # Detect stuck tasks (high threshold = 30 min)
    stuck = monitor.detect_stuck_tasks(tasks)
    assert len(stuck) == 1, f"Expected 1 stuck task, found {len(stuck)}"
    assert stuck[0]['id'] == 'task1', f"Expected task1 to be stuck, got {stuck[0]['id']}"

    # Test distribution analysis
    distribution = monitor.analyze_distribution(tasks)
    assert distribution['total'] == 2
    assert distribution['by_priority']['high'] == 1
    assert distribution['by_priority']['medium'] == 1

    print("✓ TaskHealthMonitor tests passed")


def test_pipeline_timeline():
    """Test pipeline timeline logic"""
    from scripts.inspect_pipeline_timeline import PipelineTimeline
    from unittest.mock import Mock

    # Create mock clients
    mock_redis = Mock()
    mock_es = Mock()

    timeline = PipelineTimeline(mock_redis, mock_es)

    # Test duration formatting
    duration = timeline.format_duration(
        "2025-01-25T10:00:00",
        "2025-01-25T11:15:30"
    )
    assert "1h 15m 30s" == duration, f"Expected '1h 15m 30s', got '{duration}'"

    # Test event icon mapping
    assert timeline.get_event_icon('agent_completed') == '✓'
    assert timeline.get_event_icon('agent_failed') == '✗'
    assert timeline.get_event_icon('agent_initialized') == '▶'

    print("✓ PipelineTimeline tests passed")


def main():
    """Run all tests"""
    print("\nRunning diagnostic scripts tests...\n")

    try:
        test_checkpoint_inspector()
        test_task_health_monitor()
        test_pipeline_timeline()

        print("\n✓ All tests passed!\n")
        return 0
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}\n")
        return 1
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}\n")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
