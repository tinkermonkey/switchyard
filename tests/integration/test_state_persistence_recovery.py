"""
Integration tests for state persistence and recovery

Tests the end-to-end persistence and recovery of review cycles,
including restart scenarios and state reconstruction from discussions.

CRITICAL: This ensures the orchestrator can recover from failures
and resume work without losing progress.
"""

import pytest
import os
import tempfile
import yaml
from datetime import datetime, timezone
from services.review_cycle import ReviewCycleExecutor, ReviewCycleState
from services.project_monitor import ProjectMonitor


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


@pytest.mark.integration
@pytest.mark.asyncio
class TestPersistenceLifecycle:
    """Test complete persistence lifecycle"""

    async def test_save_load_cycle_roundtrip(
        self,
        executor_with_temp_state,
        review_cycle_builder
    ):
        """
        Test: Save and load cycle preserves all data

        Flow:
        1. Create cycle state with rich data
        2. Save to disk
        3. Load from disk
        4. Verify all fields preserved
        """
        # Create rich cycle state
        original = (review_cycle_builder
            .for_issue(96)
            .in_repository('context-studio')
            .with_agents('business_analyst', 'requirements_reviewer')
            .for_project('context-studio', 'idea-development')
            .in_discussion('D_test123')
            .at_iteration(2)
            .with_maker_output("BA initial output", iteration=0)
            .with_review_output("RR feedback iteration 1", iteration=1)
            .with_maker_output("BA revision iteration 2", iteration=2)
            .reviewer_working()
            .build())

        # Save state
        executor_with_temp_state._save_cycle_state(original)

        # Load state
        loaded_cycles = executor_with_temp_state._load_active_cycles('context-studio')

        # Verify
        assert len(loaded_cycles) == 1
        loaded = loaded_cycles[0]

        assert loaded.issue_number == original.issue_number
        assert loaded.repository == original.repository
        assert loaded.maker_agent == original.maker_agent
        assert loaded.reviewer_agent == original.reviewer_agent
        assert loaded.current_iteration == original.current_iteration
        assert loaded.status == original.status
        assert loaded.project_name == original.project_name
        assert loaded.board_name == original.board_name
        assert loaded.workspace_type == original.workspace_type
        assert loaded.discussion_id == original.discussion_id

        assert len(loaded.maker_outputs) == len(original.maker_outputs)
        assert len(loaded.review_outputs) == len(original.review_outputs)

        # Verify output content
        assert loaded.maker_outputs[0]['output'] == "BA initial output"
        assert loaded.maker_outputs[1]['output'] == "BA revision iteration 2"
        assert loaded.review_outputs[0]['output'] == "RR feedback iteration 1"

    async def test_multiple_concurrent_cycles(
        self,
        executor_with_temp_state,
        review_cycle_builder
    ):
        """
        Test: Multiple cycles can coexist in same project

        Flow:
        1. Create 3 cycles for different issues
        2. Save all
        3. Load all
        4. Verify all preserved
        """
        cycles = [
            (review_cycle_builder
                .for_issue(96)
                .for_project('context-studio')
                .at_iteration(1)
                .reviewer_working()
                .build()),
            (review_cycle_builder
                .for_issue(97)
                .for_project('context-studio')
                .at_iteration(2)
                .maker_working()
                .build()),
            (review_cycle_builder
                .for_issue(98)
                .for_project('context-studio')
                .at_iteration(0)
                .initialized()
                .build())
        ]

        # Save all
        for cycle in cycles:
            executor_with_temp_state._save_cycle_state(cycle)

        # Load all
        loaded = executor_with_temp_state._load_active_cycles('context-studio')

        # Verify
        assert len(loaded) == 3
        issue_numbers = {c.issue_number for c in loaded}
        assert issue_numbers == {96, 97, 98}

        # Verify statuses preserved
        status_map = {c.issue_number: c.status for c in loaded}
        assert status_map[96] == 'reviewer_working'
        assert status_map[97] == 'maker_working'
        assert status_map[98] == 'initialized'

    async def test_update_existing_cycle(
        self,
        executor_with_temp_state,
        review_cycle_builder
    ):
        """
        Test: Updating a cycle updates the file entry

        Flow:
        1. Save cycle at iteration 1
        2. Progress to iteration 2
        3. Save again
        4. Load - should have latest data only
        """
        cycle = (review_cycle_builder
            .for_issue(96)
            .for_project('context-studio')
            .at_iteration(1)
            .reviewer_working()
            .build())

        # Save iteration 1
        executor_with_temp_state._save_cycle_state(cycle)

        # Progress cycle
        cycle.current_iteration = 2
        cycle.status = 'maker_working'
        cycle.maker_outputs.append({
            'iteration': 2,
            'output': 'New maker output',
            'timestamp': datetime.now().isoformat()
        })

        # Save iteration 2
        executor_with_temp_state._save_cycle_state(cycle)

        # Load
        loaded = executor_with_temp_state._load_active_cycles('context-studio')

        # Should have only one cycle with latest data
        assert len(loaded) == 1
        assert loaded[0].current_iteration == 2
        assert loaded[0].status == 'maker_working'
        assert len(loaded[0].maker_outputs) == 1

    async def test_remove_completed_cycle(
        self,
        executor_with_temp_state,
        review_cycle_builder
    ):
        """
        Test: Removing cycle deletes from state file

        Flow:
        1. Save 2 cycles
        2. Remove 1
        3. Load - should only have 1 remaining
        """
        cycle1 = review_cycle_builder.for_issue(96).for_project('context-studio').build()
        cycle2 = review_cycle_builder.for_issue(97).for_project('context-studio').build()

        # Save both
        executor_with_temp_state._save_cycle_state(cycle1)
        executor_with_temp_state._save_cycle_state(cycle2)

        # Remove one
        executor_with_temp_state._remove_cycle_state(cycle1)

        # Load
        loaded = executor_with_temp_state._load_active_cycles('context-studio')

        # Should only have cycle2
        assert len(loaded) == 1
        assert loaded[0].issue_number == 97


@pytest.mark.integration
@pytest.mark.asyncio
class TestRecoveryScenarios:
    """Test recovery from various failure scenarios"""

    async def test_reconstruct_state_from_discussion(
        self,
        executor_with_temp_state,
        mock_github_app
    ):
        """
        Test: Reconstruct cycle state from discussion timeline

        Flow:
        1. State file is lost or corrupted
        2. Discussion has complete timeline
        3. Reconstruct maker/reviewer outputs from timeline
        """
        # Create discussion with complete timeline
        mock_github_app.create_discussion('D_test123', 'Requirements', 'Initial')

        # Timeline with multiple iterations
        timeline_items = [
            ('BA output 1', 'business_analyst', '2025-10-03T10:00:00Z'),
            ('RR feedback 1', 'requirements_reviewer', '2025-10-03T10:05:00Z'),
            ('BA revision 2', 'business_analyst', '2025-10-03T10:10:00Z'),
            ('RR feedback 2', 'requirements_reviewer', '2025-10-03T10:15:00Z'),
        ]

        for body, agent, created_at in timeline_items:
            mock_github_app.add_discussion_comment(
                'D_test123',
                f"{body}\n\n_Processed by the {agent} agent_",
                author='orchestrator-bot'
            )

        # Reconstruct state (simulating what resume_review_cycle does)
        cycle_state = ReviewCycleState(
            issue_number=96,
            repository='context-studio',
            maker_agent='business_analyst',
            reviewer_agent='requirements_reviewer',
            max_iterations=3,
            project_name='context-studio',
            board_name='idea-development',
            workspace_type='discussions',
            discussion_id='D_test123'
        )

        # Get timeline
        timeline = mock_github_app.get_discussion_comments('D_test123')

        # Reconstruct outputs
        maker_signature = "_Processed by the business_analyst agent_"
        reviewer_signature = "_Processed by the requirements_reviewer agent_"

        for comment in timeline:
            body = comment['body']
            if maker_signature in body:
                cycle_state.maker_outputs.append({
                    'iteration': len(cycle_state.maker_outputs),
                    'output': body,
                    'timestamp': comment['createdAt']
                })
            elif reviewer_signature in body:
                cycle_state.review_outputs.append({
                    'iteration': len(cycle_state.review_outputs),
                    'output': body,
                    'timestamp': comment['createdAt']
                })

        # Verify reconstruction
        assert len(cycle_state.maker_outputs) == 2
        assert len(cycle_state.review_outputs) == 2
        assert 'BA output 1' in cycle_state.maker_outputs[0]['output']
        assert 'BA revision 2' in cycle_state.maker_outputs[1]['output']
        assert 'RR feedback 1' in cycle_state.review_outputs[0]['output']
        assert 'RR feedback 2' in cycle_state.review_outputs[1]['output']


@pytest.mark.integration
@pytest.mark.asyncio
class TestCorruptedStateHandling:
    """Test handling of corrupted or invalid state files"""

    async def test_corrupted_yaml_returns_empty(
        self,
        executor_with_temp_state,
        temp_state_dir
    ):
        """
        Test: Corrupted YAML file handled gracefully

        Flow:
        1. Manually create corrupted YAML file
        2. Try to load
        3. Should handle gracefully (return empty or raise specific error)
        """
        # Create corrupted YAML file
        state_path = os.path.join(
            temp_state_dir,
            'projects/context-studio/review_cycles'
        )
        os.makedirs(state_path, exist_ok=True)

        with open(os.path.join(state_path, 'active_cycles.yaml'), 'w') as f:
            f.write('invalid: yaml: content: [[[')

        # Try to load
        try:
            loaded = executor_with_temp_state._load_active_cycles('context-studio')
            # If it returns, should be empty or None
            assert loaded is None or len(loaded) == 0
        except yaml.YAMLError:
            # Also acceptable - corrupted YAML raises error
            pass

    async def test_missing_state_file_returns_empty(
        self,
        executor_with_temp_state
    ):
        """
        Test: Missing state file returns empty list (not error)

        Flow:
        1. Try to load from non-existent project
        2. Should return empty list gracefully
        """
        loaded = executor_with_temp_state._load_active_cycles('nonexistent-project')

        assert loaded == []

    async def test_incomplete_state_data(
        self,
        executor_with_temp_state,
        temp_state_dir
    ):
        """
        Test: State with missing required fields handled

        Flow:
        1. Create state file with incomplete data
        2. Try to load
        3. Should handle gracefully or raise clear error
        """
        # Create incomplete state data
        state_path = os.path.join(
            temp_state_dir,
            'projects/context-studio/review_cycles'
        )
        os.makedirs(state_path, exist_ok=True)

        incomplete_data = {
            'active_cycles': [
                {
                    'issue_number': 96,
                    # Missing maker_agent, reviewer_agent, etc.
                }
            ]
        }

        with open(os.path.join(state_path, 'active_cycles.yaml'), 'w') as f:
            yaml.dump(incomplete_data, f)

        # Try to load
        try:
            loaded = executor_with_temp_state._load_active_cycles('context-studio')
            # May return empty if validation fails
            assert isinstance(loaded, list)
        except (KeyError, TypeError) as e:
            # Also acceptable - missing fields cause error
            pass


@pytest.mark.integration
@pytest.mark.asyncio
class TestStateFileStructure:
    """Test YAML state file structure and format"""

    async def test_state_file_is_valid_yaml(
        self,
        executor_with_temp_state,
        review_cycle_builder,
        temp_state_dir
    ):
        """
        Test: Saved state file is valid YAML

        Flow:
        1. Save cycle state
        2. Read file directly
        3. Verify it's valid YAML
        """
        cycle = (review_cycle_builder
            .for_issue(96)
            .for_project('context-studio')
            .at_iteration(1)
            .build())

        # Save
        executor_with_temp_state._save_cycle_state(cycle)

        # Read file directly
        state_file = os.path.join(
            temp_state_dir,
            'projects/context-studio/review_cycles/active_cycles.yaml'
        )

        with open(state_file) as f:
            data = yaml.safe_load(f)

        # Verify structure
        assert 'active_cycles' in data
        assert isinstance(data['active_cycles'], list)
        assert len(data['active_cycles']) == 1
        assert data['active_cycles'][0]['issue_number'] == 96

    async def test_state_file_human_readable(
        self,
        executor_with_temp_state,
        review_cycle_builder,
        temp_state_dir
    ):
        """
        Test: State file is human-readable for debugging

        Flow:
        1. Save cycle with rich data
        2. Read file as text
        3. Verify key information is readable
        """
        cycle = (review_cycle_builder
            .for_issue(96)
            .in_repository('context-studio')
            .with_agents('business_analyst', 'requirements_reviewer')
            .for_project('context-studio', 'idea-development')
            .at_iteration(2)
            .with_maker_output("BA output")
            .reviewer_working()
            .build())

        # Save
        executor_with_temp_state._save_cycle_state(cycle)

        # Read as text
        state_file = os.path.join(
            temp_state_dir,
            'projects/context-studio/review_cycles/active_cycles.yaml'
        )

        with open(state_file) as f:
            content = f.read()

        # Verify human-readable content
        assert 'issue_number: 96' in content
        assert 'context-studio' in content
        assert 'business_analyst' in content
        assert 'requirements_reviewer' in content
        assert 'current_iteration: 2' in content
        assert 'reviewer_working' in content


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
