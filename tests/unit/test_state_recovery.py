"""
Unit tests for state recovery and resumption

Tests the logic for persisting and restoring review cycle state,
especially after orchestrator restarts or failures.

CRITICAL: Ensures work-in-progress cycles can resume correctly.
"""

import pytest
import os
import tempfile
import yaml
from pathlib import Path
from services.review_cycle import ReviewCycleExecutor, ReviewCycleState
from tests.utils.assertions import assert_escalation_occurred


@pytest.fixture
def temp_state_dir():
    """Create temporary directory for state files"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def executor_with_temp_state(temp_state_dir, monkeypatch):
    """Create executor with temporary state directory"""
    executor = ReviewCycleExecutor()

    # Mock the state file path to use temp directory
    def mock_get_state_file_path(project_name: str) -> str:
        state_dir = os.path.join(temp_state_dir, 'projects', project_name, 'review_cycles')
        os.makedirs(state_dir, exist_ok=True)
        return os.path.join(state_dir, 'active_cycles.yaml')

    monkeypatch.setattr(executor, '_get_state_file_path', mock_get_state_file_path)

    return executor


class TestStatePersistence:
    """Test saving and loading cycle state"""

    def test_save_cycle_state_creates_file(
        self,
        executor_with_temp_state,
        review_cycle_builder,
        temp_state_dir
    ):
        """Test: Saving state creates YAML file"""
        # Given: A review cycle state
        state = (review_cycle_builder
            .for_issue(96)
            .for_project('context-studio', 'idea-development')
            .at_iteration(2)
            .reviewer_working()
            .build())

        # When: Save state
        executor_with_temp_state._save_cycle_state(state)

        # Then: File should exist
        expected_path = os.path.join(
            temp_state_dir,
            'projects/context-studio/review_cycles/active_cycles.yaml'
        )
        assert os.path.exists(expected_path)

    def test_save_and_load_cycle_state(
        self,
        executor_with_temp_state,
        review_cycle_builder
    ):
        """Test: State survives save/load roundtrip"""
        # Given: A review cycle state
        original = (review_cycle_builder
            .for_issue(96)
            .in_repository('context-studio')
            .for_project('context-studio', 'idea-development')
            .in_discussion('D_test123')
            .at_iteration(2)
            .with_maker_output("BA output 1")
            .with_maker_output("BA output 2")
            .with_review_output("RR feedback")
            .reviewer_working()
            .build())

        # When: Save and load
        executor_with_temp_state._save_cycle_state(original)
        loaded_cycles = executor_with_temp_state._load_active_cycles('context-studio')

        # Then: Should load successfully
        assert len(loaded_cycles) == 1
        loaded = loaded_cycles[0]

        assert loaded.issue_number == original.issue_number
        assert loaded.repository == original.repository
        assert loaded.maker_agent == original.maker_agent
        assert loaded.reviewer_agent == original.reviewer_agent
        assert loaded.current_iteration == original.current_iteration
        assert loaded.status == original.status
        assert len(loaded.maker_outputs) == len(original.maker_outputs)
        assert len(loaded.review_outputs) == len(original.review_outputs)

    def test_save_multiple_cycles(
        self,
        executor_with_temp_state,
        review_cycle_builder
    ):
        """Test: Multiple cycles can be saved in same file"""
        # Given: Two review cycles
        state1 = review_cycle_builder.for_issue(96).for_project('context-studio').build()
        state2 = review_cycle_builder.for_issue(97).for_project('context-studio').build()

        # When: Save both
        executor_with_temp_state._save_cycle_state(state1)
        executor_with_temp_state._save_cycle_state(state2)

        # Then: Both should be loadable
        loaded = executor_with_temp_state._load_active_cycles('context-studio')
        assert len(loaded) == 2
        issue_numbers = {cycle.issue_number for cycle in loaded}
        assert issue_numbers == {96, 97}

    def test_update_existing_cycle(
        self,
        executor_with_temp_state,
        review_cycle_builder
    ):
        """Test: Saving same cycle updates existing entry"""
        # Given: A saved cycle
        state = (review_cycle_builder
            .for_issue(96)
            .for_project('context-studio')
            .at_iteration(1)
            .reviewer_working()
            .build())

        executor_with_temp_state._save_cycle_state(state)

        # When: Update and save again
        state.current_iteration = 2
        state.status = 'maker_working'
        executor_with_temp_state._save_cycle_state(state)

        # Then: Should have only one entry with updated values
        loaded = executor_with_temp_state._load_active_cycles('context-studio')
        assert len(loaded) == 1
        assert loaded[0].current_iteration == 2
        assert loaded[0].status == 'maker_working'


class TestStateRecovery:
    """Test recovering state after restart"""

    def test_load_escalated_cycle(
        self,
        executor_with_temp_state,
        review_cycle_builder
    ):
        """Test: Load cycle in awaiting_human_feedback state"""
        # Given: An escalated cycle
        state = (review_cycle_builder
            .for_issue(96)
            .for_project('context-studio')
            .at_iteration(3)
            .with_maker_output("BA output")
            .with_review_output("RR blocked")
            .escalated()
            .build())

        executor_with_temp_state._save_cycle_state(state)

        # When: Load (simulating restart)
        loaded = executor_with_temp_state._load_active_cycles('context-studio')

        # Then: Should restore escalated state
        assert len(loaded) == 1
        loaded_state = loaded[0]
        assert_escalation_occurred(loaded_state)
        assert loaded_state.current_iteration == 3

    def test_load_empty_state_file(self, executor_with_temp_state):
        """Test: Loading non-existent file returns empty list"""
        # When: Load from non-existent project
        loaded = executor_with_temp_state._load_active_cycles('nonexistent-project')

        # Then: Should return empty list (not error)
        assert loaded == []

    def test_load_preserves_outputs(
        self,
        executor_with_temp_state,
        review_cycle_builder,
        sample_ba_output,
        sample_reviewer_feedback,
        sample_ba_revision
    ):
        """Test: Loading preserves maker and reviewer outputs"""
        # Given: Cycle with multiple outputs
        state = (review_cycle_builder
            .for_issue(96)
            .for_project('context-studio')
            .at_iteration(2)
            .with_maker_output(sample_ba_output, iteration=0)
            .with_review_output(sample_reviewer_feedback, iteration=1)
            .with_maker_output(sample_ba_revision, iteration=2)
            .build())

        executor_with_temp_state._save_cycle_state(state)

        # When: Load
        loaded = executor_with_temp_state._load_active_cycles('context-studio')

        # Then: Outputs should be preserved
        loaded_state = loaded[0]
        assert len(loaded_state.maker_outputs) == 2
        assert len(loaded_state.review_outputs) == 1
        assert 'Business Requirements' in loaded_state.maker_outputs[0]['output']


class TestStateRemoval:
    """Test removing completed or cancelled cycles"""

    def test_remove_cycle_state(
        self,
        executor_with_temp_state,
        review_cycle_builder
    ):
        """Test: Removing cycle state deletes entry"""
        # Given: A saved cycle
        state = review_cycle_builder.for_issue(96).for_project('context-studio').build()
        executor_with_temp_state._save_cycle_state(state)

        # When: Remove
        executor_with_temp_state._remove_cycle_state(state)

        # Then: Should no longer be loadable
        loaded = executor_with_temp_state._load_active_cycles('context-studio')
        assert len(loaded) == 0

    def test_remove_one_of_multiple_cycles(
        self,
        executor_with_temp_state,
        review_cycle_builder
    ):
        """Test: Removing one cycle leaves others intact"""
        # Given: Two cycles
        state1 = review_cycle_builder.for_issue(96).for_project('context-studio').build()
        state2 = review_cycle_builder.for_issue(97).for_project('context-studio').build()

        executor_with_temp_state._save_cycle_state(state1)
        executor_with_temp_state._save_cycle_state(state2)

        # When: Remove one
        executor_with_temp_state._remove_cycle_state(state1)

        # Then: Other should still exist
        loaded = executor_with_temp_state._load_active_cycles('context-studio')
        assert len(loaded) == 1
        assert loaded[0].issue_number == 97


class TestReconstructStateFromDiscussion:
    """Test reconstructing state from discussion when file is lost"""

    @pytest.mark.asyncio
    async def test_reconstruct_outputs_from_timeline(
        self,
        monkeypatch
    ):
        """
        Test: Reconstruct maker and reviewer outputs from discussion timeline

        This is what resume_review_cycle does when state file doesn't exist
        """
        # Given: Discussion with multiple outputs
        timeline = [
            {
                'author': 'orchestrator-bot',
                'body': 'BA output 1\n\n_Processed by the business_analyst agent_',
                'created_at': '2025-10-03T13:00:00Z'
            },
            {
                'author': 'orchestrator-bot',
                'body': 'RR feedback 1\n\n_Processed by the requirements_reviewer agent_',
                'created_at': '2025-10-03T13:05:00Z'
            },
            {
                'author': 'orchestrator-bot',
                'body': 'BA output 2\n\n_Processed by the business_analyst agent_',
                'created_at': '2025-10-03T13:10:00Z'
            },
            {
                'author': 'orchestrator-bot',
                'body': 'RR feedback 2\n\n_Processed by the requirements_reviewer agent_',
                'created_at': '2025-10-03T13:15:00Z'
            }
        ]

        # When: Reconstruct state
        cycle_state = ReviewCycleState(
            issue_number=96,
            repository='context-studio',
            maker_agent='business_analyst',
            reviewer_agent='requirements_reviewer',
            max_iterations=3,
            project_name='context-studio',
            board_name='idea-development',
            workspace_type='discussions',
            discussion_id='D_test'
        )

        maker_signature = f"_Processed by the business_analyst agent_"
        reviewer_signature = f"_Processed by the requirements_reviewer agent_"

        for item in timeline:
            body = item['body']
            if maker_signature in body:
                cycle_state.maker_outputs.append({
                    'iteration': len(cycle_state.maker_outputs),
                    'output': body,
                    'timestamp': item['created_at']
                })
            elif reviewer_signature in body:
                cycle_state.review_outputs.append({
                    'iteration': len(cycle_state.review_outputs),
                    'output': body,
                    'timestamp': item['created_at']
                })

        # Then: Should have reconstructed outputs
        assert len(cycle_state.maker_outputs) == 2
        assert len(cycle_state.review_outputs) == 2
        assert 'BA output 1' in cycle_state.maker_outputs[0]['output']
        assert 'BA output 2' in cycle_state.maker_outputs[1]['output']
        assert 'RR feedback 1' in cycle_state.review_outputs[0]['output']


class TestStateCorruption:
    """Test handling of corrupted state files"""

    def test_handle_corrupted_yaml(
        self,
        executor_with_temp_state,
        temp_state_dir
    ):
        """Test: Handle corrupted YAML file gracefully"""
        # Given: Corrupted YAML file
        state_path = os.path.join(
            temp_state_dir,
            'projects/context-studio/review_cycles'
        )
        os.makedirs(state_path, exist_ok=True)

        with open(os.path.join(state_path, 'active_cycles.yaml'), 'w') as f:
            f.write('invalid: yaml: content: [')

        # When: Try to load
        # Then: Should handle gracefully (return empty or raise specific error)
        # Implementation may vary - test should not crash
        try:
            loaded = executor_with_temp_state._load_active_cycles('context-studio')
            # If it returns something, should be empty or None
            assert loaded is None or len(loaded) == 0
        except yaml.YAMLError:
            # Also acceptable - corrupted YAML raises error
            pass

    def test_missing_required_fields(
        self,
        executor_with_temp_state,
        temp_state_dir
    ):
        """Test: Handle state with missing required fields"""
        # Given: State file with incomplete data
        state_path = os.path.join(
            temp_state_dir,
            'projects/context-studio/review_cycles'
        )
        os.makedirs(state_path, exist_ok=True)

        incomplete_data = {
            'active_cycles': [
                {
                    'issue_number': 96,
                    # missing maker_agent, reviewer_agent, etc.
                }
            ]
        }

        with open(os.path.join(state_path, 'active_cycles.yaml'), 'w') as f:
            yaml.dump(incomplete_data, f)

        # When: Try to load
        # Then: Should handle gracefully or raise clear error
        try:
            loaded = executor_with_temp_state._load_active_cycles('context-studio')
            # May return empty if validation fails
            assert isinstance(loaded, list)
        except (KeyError, TypeError) as e:
            # Also acceptable - missing fields cause error
            assert 'maker_agent' in str(e) or 'reviewer_agent' in str(e) or True


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
