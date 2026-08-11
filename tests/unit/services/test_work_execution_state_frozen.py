"""
Tests for work_execution_state.py's 'frozen' outcome (renamed from 'blocked')
and the claude_session_id piggyback used for the resume-with-continuation-prompt
fork in agent_executor.py.
"""
import pytest

from services.work_execution_state import WorkExecutionStateTracker


def make_tracker(tmp_path):
    return WorkExecutionStateTracker(state_dir=tmp_path)


class TestFrozenOutcomeRecording:
    def test_record_frozen_outcome_without_session_id(self, tmp_path):
        tracker = make_tracker(tmp_path)
        tracker.record_execution_start(
            issue_number=42, column="Development", agent="senior_software_engineer",
            trigger_source="manual_move", project_name="proj"
        )
        tracker.record_execution_outcome(
            issue_number=42, column="Development", agent="senior_software_engineer",
            outcome="frozen", project_name="proj",
            error="Claude Code circuit breaker is OPEN. Resets at 04:20 PM"
        )

        last = tracker.get_last_execution("proj", 42, "Development", "senior_software_engineer")
        assert last["outcome"] == "frozen"
        assert "claude_session_id" not in last

    def test_record_frozen_outcome_with_session_id_piggybacks_onto_same_write(self, tmp_path):
        tracker = make_tracker(tmp_path)
        tracker.record_execution_start(
            issue_number=42, column="Development", agent="senior_software_engineer",
            trigger_source="manual_move", project_name="proj"
        )
        tracker.record_execution_outcome(
            issue_number=42, column="Development", agent="senior_software_engineer",
            outcome="frozen", project_name="proj",
            error="Claude Code rate limit confirmed",
            claude_session_id="53dc2d26-d820-4509-a9ed-a3f98b22d32b"
        )

        last = tracker.get_last_execution("proj", 42, "Development", "senior_software_engineer")
        assert last["outcome"] == "frozen"
        assert last["claude_session_id"] == "53dc2d26-d820-4509-a9ed-a3f98b22d32b"


class TestIsFrozenByCircuitBreaker:
    """
    Regression coverage for the bug where is_frozen_by_circuit_breaker() also
    substring-matched 'circuit breaker' in the human-readable error text. That
    text varies by which detection path recorded the freeze — the NEW
    structural detector's message ("Claude Code rate limit confirmed
    (source=...)...", see claude/docker_runner.py's _rate_limit_signal raise)
    never contains the literal phrase "circuit breaker", so the substring
    match silently missed every freeze caused by the new detection path. The
    fix: outcome=='frozen' is itself the single, unambiguous marker (it is
    written in exactly one place in the codebase — agent_executor.py's
    is_claude_breaker_failure branch), so the check must not depend on
    incidental wording in the error text at all.
    """

    @pytest.mark.parametrize("error_text", [
        # The NEW structural detector's actual message format (docker_runner.py)
        # — this exact shape is what the bug silently failed to match.
        "Claude Code rate limit confirmed (source=rate_limit_event, rate_limit_type=five_hour). Resets at 04:20 PM.",
        "Claude Code rate limit confirmed (source=assistant_error_field, rate_limit_type=None). Resets at 04:20 PM.",
        # The older pre-flight-open message format (agent_executor.py) that
        # happened to contain "circuit breaker" and used to pass by accident.
        "Claude Code circuit breaker is OPEN. Resets at 04:20 PM",
        # A hypothetical future/unrelated wording with no "circuit breaker"
        # phrase at all — must still be recognized, since outcome alone matters.
        "some unrelated freeze reason",
        "",
    ])
    def test_true_whenever_outcome_is_frozen_regardless_of_error_text(self, tmp_path, error_text):
        tracker = make_tracker(tmp_path)
        tracker.record_execution_start(
            issue_number=42, column="Development", agent="senior_software_engineer",
            trigger_source="manual_move", project_name="proj"
        )
        tracker.record_execution_outcome(
            issue_number=42, column="Development", agent="senior_software_engineer",
            outcome="frozen", project_name="proj", error=error_text
        )
        assert tracker.is_frozen_by_circuit_breaker("proj", 42) is True

    def test_false_when_last_outcome_is_success(self, tmp_path):
        tracker = make_tracker(tmp_path)
        tracker.record_execution_start(
            issue_number=42, column="Development", agent="senior_software_engineer",
            trigger_source="manual_move", project_name="proj"
        )
        tracker.record_execution_outcome(
            issue_number=42, column="Development", agent="senior_software_engineer",
            outcome="success", project_name="proj"
        )
        assert tracker.is_frozen_by_circuit_breaker("proj", 42) is False

    def test_false_when_last_outcome_is_failure_even_with_circuit_breaker_wording(self, tmp_path):
        """The outcome value, not the wording, is what must gate this — a
        generic 'failure' that happens to mention 'circuit breaker' in passing
        must not be misread as a freeze."""
        tracker = make_tracker(tmp_path)
        tracker.record_execution_start(
            issue_number=42, column="Development", agent="senior_software_engineer",
            trigger_source="manual_move", project_name="proj"
        )
        tracker.record_execution_outcome(
            issue_number=42, column="Development", agent="senior_software_engineer",
            outcome="failure", project_name="proj",
            error="unrelated failure that happens to mention circuit breaker in a stack trace"
        )
        assert tracker.is_frozen_by_circuit_breaker("proj", 42) is False

    def test_false_when_no_history(self, tmp_path):
        tracker = make_tracker(tmp_path)
        assert tracker.is_frozen_by_circuit_breaker("proj", 999) is False


class TestGetResumableFrozenSession:
    def test_returns_session_id_when_frozen_with_prior_progress(self, tmp_path):
        tracker = make_tracker(tmp_path)
        tracker.record_execution_start(
            issue_number=42, column="Development", agent="senior_software_engineer",
            trigger_source="manual_move", project_name="proj"
        )
        tracker.record_execution_outcome(
            issue_number=42, column="Development", agent="senior_software_engineer",
            outcome="frozen", project_name="proj", error="rate limited",
            claude_session_id="abc-123"
        )
        assert tracker.get_resumable_frozen_session("proj", 42, "Development", "senior_software_engineer") == "abc-123"

    def test_returns_none_when_frozen_without_captured_session(self, tmp_path):
        """The common case: a first-turn rejection has nothing to resume, so no
        session_id was ever captured."""
        tracker = make_tracker(tmp_path)
        tracker.record_execution_start(
            issue_number=42, column="Development", agent="senior_software_engineer",
            trigger_source="manual_move", project_name="proj"
        )
        tracker.record_execution_outcome(
            issue_number=42, column="Development", agent="senior_software_engineer",
            outcome="frozen", project_name="proj", error="rate limited"
        )
        assert tracker.get_resumable_frozen_session("proj", 42, "Development", "senior_software_engineer") is None

    def test_returns_none_when_last_outcome_is_not_frozen(self, tmp_path):
        tracker = make_tracker(tmp_path)
        tracker.record_execution_start(
            issue_number=42, column="Development", agent="senior_software_engineer",
            trigger_source="manual_move", project_name="proj"
        )
        tracker.record_execution_outcome(
            issue_number=42, column="Development", agent="senior_software_engineer",
            outcome="success", project_name="proj"
        )
        assert tracker.get_resumable_frozen_session("proj", 42, "Development", "senior_software_engineer") is None

    def test_returns_none_for_unknown_issue(self, tmp_path):
        tracker = make_tracker(tmp_path)
        assert tracker.get_resumable_frozen_session("proj", 999, "Development", "senior_software_engineer") is None


class TestShouldExecuteWorkTreatsFrozenLikeFailure:
    def test_frozen_outcome_is_retry_eligible(self, tmp_path):
        tracker = make_tracker(tmp_path)
        tracker.record_execution_start(
            issue_number=42, column="Development", agent="senior_software_engineer",
            trigger_source="manual_move", project_name="proj"
        )
        tracker.record_execution_outcome(
            issue_number=42, column="Development", agent="senior_software_engineer",
            outcome="frozen", project_name="proj", error="rate limited"
        )
        should_execute, reason = tracker.should_execute_work(
            issue_number=42, column="Development", agent="senior_software_engineer",
            trigger_source="pipeline_progression", project_name="proj"
        )
        assert should_execute is True
        assert reason == "retry_after_frozen"
