"""
Unit tests for concurrent access to review cycle state (active_cycles.yaml)

Verifies the flock-based locking added to ReviewCycleExecutor's
_save_cycle_state / _load_active_cycles / _remove_cycle_state / clear_cycle_state
prevents concurrent writers from silently dropping each other's updates.
"""

import pytest
import os
import tempfile
import concurrent.futures
from services.review_cycle import ReviewCycleExecutor


@pytest.fixture
def temp_state_dir():
    """Create temporary directory for state files"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def executor_with_temp_state(temp_state_dir, monkeypatch):
    """Create executor with temporary state directory"""
    executor = ReviewCycleExecutor()

    def mock_get_state_file_path(project_name: str) -> str:
        state_dir = os.path.join(temp_state_dir, 'projects', project_name, 'review_cycles')
        os.makedirs(state_dir, exist_ok=True)
        return os.path.join(state_dir, 'active_cycles.yaml')

    monkeypatch.setattr(executor, '_get_state_file_path', mock_get_state_file_path)

    return executor


class TestConcurrentSaveCycleState:
    """Concurrent _save_cycle_state calls for DIFFERENT issues must not drop
    each other's entries. Without a lock spanning the whole read-modify-write,
    two threads can both read the file before either writes, then each write
    back a version missing the other's cycle."""

    def test_concurrent_saves_different_issues_no_lost_updates(
        self, executor_with_temp_state, review_cycle_builder
    ):
        issue_numbers = list(range(1, 31))
        states = [
            review_cycle_builder.for_issue(n).for_project('context-studio').build()
            for n in issue_numbers
        ]

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(states)) as executor:
            futures = [
                executor.submit(executor_with_temp_state._save_cycle_state, state)
                for state in states
            ]
            concurrent.futures.wait(futures)
            for f in futures:
                f.result()  # re-raise any exception

        loaded = executor_with_temp_state._load_active_cycles('context-studio')
        loaded_issue_numbers = {c.issue_number for c in loaded}

        assert loaded_issue_numbers == set(issue_numbers), (
            f"Lost update: expected {len(issue_numbers)} cycles, "
            f"found {len(loaded_issue_numbers)}: {sorted(loaded_issue_numbers)}"
        )

    def test_concurrent_save_and_remove_leaves_consistent_state(
        self, executor_with_temp_state, review_cycle_builder
    ):
        """A save for one issue racing a remove for a different issue must not
        clobber each other — the surviving state must contain exactly the
        saved (non-removed) issue."""
        keep_state = review_cycle_builder.for_issue(1).for_project('context-studio').build()
        remove_state = review_cycle_builder.for_issue(2).for_project('context-studio').build()

        # Seed both cycles on disk first.
        executor_with_temp_state._save_cycle_state(keep_state)
        executor_with_temp_state._save_cycle_state(remove_state)

        other_state = review_cycle_builder.for_issue(3).for_project('context-studio').build()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(executor_with_temp_state._remove_cycle_state, remove_state),
                executor.submit(executor_with_temp_state._save_cycle_state, other_state),
            ]
            concurrent.futures.wait(futures)
            for f in futures:
                f.result()

        loaded_issue_numbers = {
            c.issue_number for c in executor_with_temp_state._load_active_cycles('context-studio')
        }
        assert loaded_issue_numbers == {1, 3}

    def test_state_file_not_corrupted_by_concurrent_writes(
        self, executor_with_temp_state, review_cycle_builder, temp_state_dir
    ):
        """The on-disk YAML must remain valid (parseable) after concurrent
        writers race it."""
        import yaml

        states = [
            review_cycle_builder.for_issue(n).for_project('context-studio').build()
            for n in range(1, 11)
        ]

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [
                executor.submit(executor_with_temp_state._save_cycle_state, state)
                for state in states
            ]
            concurrent.futures.wait(futures)
            for f in futures:
                f.result()

        state_file = os.path.join(
            temp_state_dir, 'projects', 'context-studio', 'review_cycles', 'active_cycles.yaml'
        )
        with open(state_file) as f:
            data = yaml.safe_load(f)  # must not raise

        assert len(data['active_cycles']) == 10
