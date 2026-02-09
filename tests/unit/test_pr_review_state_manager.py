"""
Unit tests for PR Review State Manager

Tests review cycle tracking, persistence, and cycle limit enforcement.
"""

import pytest
import yaml
from pathlib import Path
from state_management.pr_review_state_manager import PRReviewStateManager


@pytest.fixture
def tmp_state_dir(tmp_path):
    """Create a temporary state directory."""
    state_dir = tmp_path / "projects"
    state_dir.mkdir()
    return state_dir


@pytest.fixture
def manager(tmp_state_dir):
    """Create a PRReviewStateManager with a temp directory."""
    return PRReviewStateManager(state_root=str(tmp_state_dir))


class TestGetReviewCount:
    def test_returns_zero_for_new_project(self, manager):
        assert manager.get_review_count("my-project", 42) == 0

    def test_returns_zero_for_new_issue(self, manager):
        # Increment for a different issue first
        manager.increment_review_count("my-project", 100, [201, 202])
        assert manager.get_review_count("my-project", 42) == 0

    def test_returns_count_after_increment(self, manager):
        manager.increment_review_count("my-project", 42, [101])
        assert manager.get_review_count("my-project", 42) == 1

    def test_returns_count_after_multiple_increments(self, manager):
        manager.increment_review_count("my-project", 42, [101])
        manager.increment_review_count("my-project", 42, [102, 103])
        manager.increment_review_count("my-project", 42, [])
        assert manager.get_review_count("my-project", 42) == 3


class TestIncrementReviewCount:
    def test_creates_state_file(self, manager, tmp_state_dir):
        manager.increment_review_count("my-project", 42, [101])
        state_file = tmp_state_dir / "my-project" / "pr_review_state.yaml"
        assert state_file.exists()

    def test_records_iteration_with_issues(self, manager):
        manager.increment_review_count("my-project", 42, [101, 102])
        history = manager.get_review_history("my-project", 42)
        assert len(history) == 1
        assert history[0]["iteration"] == 1
        assert history[0]["issues_created"] == [101, 102]
        assert "timestamp" in history[0]

    def test_records_multiple_iterations(self, manager):
        manager.increment_review_count("my-project", 42, [101])
        manager.increment_review_count("my-project", 42, [102, 103])
        history = manager.get_review_history("my-project", 42)
        assert len(history) == 2
        assert history[0]["iteration"] == 1
        assert history[1]["iteration"] == 2
        assert history[1]["issues_created"] == [102, 103]

    def test_records_empty_issues_for_clean_pass(self, manager):
        manager.increment_review_count("my-project", 42, [])
        history = manager.get_review_history("my-project", 42)
        assert len(history) == 1
        assert history[0]["issues_created"] == []

    def test_persists_across_reloads(self, manager, tmp_state_dir):
        manager.increment_review_count("my-project", 42, [101])

        # Create new manager pointing to same directory
        manager2 = PRReviewStateManager(state_root=str(tmp_state_dir))
        assert manager2.get_review_count("my-project", 42) == 1

    def test_updates_last_review_at(self, manager):
        manager.increment_review_count("my-project", 42, [101])
        data = manager._load_state("my-project")
        assert "last_review_at" in data["pr_reviews"][42]


class TestGetReviewHistory:
    def test_returns_empty_for_unknown_issue(self, manager):
        assert manager.get_review_history("my-project", 999) == []

    def test_returns_full_history(self, manager):
        manager.increment_review_count("my-project", 42, [101])
        manager.increment_review_count("my-project", 42, [102])
        history = manager.get_review_history("my-project", 42)
        assert len(history) == 2


class TestStatePersistenceFormat:
    def test_yaml_structure(self, manager, tmp_state_dir):
        manager.increment_review_count("my-project", 42, [101, 102])
        state_file = tmp_state_dir / "my-project" / "pr_review_state.yaml"
        with open(state_file) as f:
            data = yaml.safe_load(f)

        assert "pr_reviews" in data
        assert 42 in data["pr_reviews"]
        assert data["pr_reviews"][42]["review_count"] == 1
        assert len(data["pr_reviews"][42]["iterations"]) == 1

    def test_multiple_projects_independent(self, manager):
        manager.increment_review_count("project-a", 1, [101])
        manager.increment_review_count("project-b", 1, [201])

        assert manager.get_review_count("project-a", 1) == 1
        assert manager.get_review_count("project-b", 1) == 1
        assert manager.get_review_history("project-a", 1)[0]["issues_created"] == [101]
        assert manager.get_review_history("project-b", 1)[0]["issues_created"] == [201]
