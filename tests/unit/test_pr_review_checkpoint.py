"""
Unit tests for the PR review stage checkpoint system.

Mirrors tests/unit/test_repair_cycle_checkpoint.py's structure.
"""

import pytest

from pipeline.pr_review_checkpoint import PRReviewCheckpoint


@pytest.fixture
def checkpoint(tmp_path):
    """Checkpoint manager rooted under a temp dir instead of /workspace/switchyard."""
    return PRReviewCheckpoint("test-project", 123, base_dir=tmp_path)


class TestPRReviewCheckpoint:
    def test_no_checkpoint_initially(self, checkpoint):
        assert checkpoint.load_checkpoint() is None
        assert checkpoint.get_phase_output(1, "code_review") is None

    def test_save_and_get_phase_output(self, checkpoint):
        assert checkpoint.save_phase_output(1, "code_review", "review text") is True
        assert checkpoint.get_phase_output(1, "code_review") == "review text"

    def test_multiple_phases_same_cycle_accumulate(self, checkpoint):
        checkpoint.save_phase_output(1, "code_review", "phase 1 output")
        checkpoint.save_phase_output(1, "parent_issue", "phase 2 output")
        checkpoint.save_phase_output(1, "consolidation", "phase 4 output")

        assert checkpoint.get_phase_output(1, "code_review") == "phase 1 output"
        assert checkpoint.get_phase_output(1, "parent_issue") == "phase 2 output"
        assert checkpoint.get_phase_output(1, "consolidation") == "phase 4 output"

    def test_get_phase_output_missing_phase_returns_none(self, checkpoint):
        checkpoint.save_phase_output(1, "code_review", "phase 1 output")
        assert checkpoint.get_phase_output(1, "parent_issue") is None

    def test_stale_cycle_is_not_reused(self, checkpoint):
        """A phase output saved under cycle 1 must never answer a lookup for cycle 2 —
        cycle 2 is a fresh review pass and cycle 1's findings are no longer relevant."""
        checkpoint.save_phase_output(1, "code_review", "cycle 1 output")
        assert checkpoint.get_phase_output(2, "code_review") is None

    def test_saving_a_new_cycle_drops_the_previous_cycles_phases(self, checkpoint):
        """Once cycle 2 starts saving phases, cycle 1's phases must not resurface —
        otherwise a resume mid-cycle-2 could reuse a stale cycle-1 output for a phase
        cycle 2 hasn't actually run yet."""
        checkpoint.save_phase_output(1, "code_review", "cycle 1 output")
        checkpoint.save_phase_output(2, "code_review", "cycle 2 output")

        assert checkpoint.get_phase_output(2, "code_review") == "cycle 2 output"
        assert checkpoint.get_phase_output(1, "code_review") is None

    def test_clear_checkpoint_removes_all_phases(self, checkpoint):
        checkpoint.save_phase_output(1, "code_review", "output")
        assert checkpoint.clear_checkpoint() is True
        assert checkpoint.load_checkpoint() is None
        assert checkpoint.get_phase_output(1, "code_review") is None

    def test_clear_checkpoint_when_none_exists_is_a_no_op(self, checkpoint):
        assert checkpoint.clear_checkpoint() is True

    def test_separate_issues_do_not_share_a_checkpoint(self, tmp_path):
        cp_a = PRReviewCheckpoint("test-project", 1, base_dir=tmp_path)
        cp_b = PRReviewCheckpoint("test-project", 2, base_dir=tmp_path)

        cp_a.save_phase_output(1, "code_review", "issue 1 output")

        assert cp_a.get_phase_output(1, "code_review") == "issue 1 output"
        assert cp_b.get_phase_output(1, "code_review") is None

    def test_backup_recovers_from_corrupted_primary(self, checkpoint):
        checkpoint.save_phase_output(1, "code_review", "good output")
        checkpoint.save_phase_output(1, "parent_issue", "second save creates a backup")

        # Corrupt the primary file; the backup (from the save before this one) should
        # still resolve the earlier phase.
        checkpoint.checkpoint_file.write_text("{not valid json")

        recovered = checkpoint.load_checkpoint()
        assert recovered is not None
        assert recovered["phases"]["code_review"] == "good output"

    def test_reinstantiating_with_same_ids_sees_prior_saves(self, tmp_path):
        """A fresh PRReviewCheckpoint instance for the same (project, issue) — as
        happens when a new PRReviewStage.execute() call is started after a restart —
        must see whatever a previous instance already checkpointed."""
        PRReviewCheckpoint("test-project", 123, base_dir=tmp_path).save_phase_output(
            1, "code_review", "output from the interrupted run"
        )

        resumed = PRReviewCheckpoint("test-project", 123, base_dir=tmp_path)
        assert resumed.get_phase_output(1, "code_review") == "output from the interrupted run"

    def test_refuses_empty_output(self, checkpoint):
        """An empty result isn't a completed phase — checkpointing it as done would
        make a later resume skip a phase that actually still needs to run."""
        assert checkpoint.save_phase_output(1, "code_review", "") is False
        assert checkpoint.get_phase_output(1, "code_review") is None

    def test_refuses_whitespace_only_output(self, checkpoint):
        assert checkpoint.save_phase_output(1, "code_review", "   \n  ") is False
        assert checkpoint.get_phase_output(1, "code_review") is None

    def test_refuses_non_int_cycle(self, checkpoint):
        """A str cycle like '1' would compare unequal to the stored int 1, look like
        a new cycle, and silently discard real phase data — refuse it outright."""
        assert checkpoint.save_phase_output("1", "code_review", "output") is False
        assert checkpoint.get_phase_output(1, "code_review") is None
