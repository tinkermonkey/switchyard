"""
Unit tests for services.project_monitor._end_pr_review_pipeline_run_on_failure.

Extracted from the run_pr_review() closure inside ProjectMonitor.trigger_agent_
for_status specifically so this decision logic — round 5's migration of the PR
review stage's retain=True branch to the shared mark_failed() entry point,
found untested by PR #35's round-6 review — could be unit tested directly,
rather than requiring the full enclosing dispatch path to be driven (which
this file's own history shows is prone to producing a vacuous test; see
tests/unit/orchestrator/test_repair_cycle_lock_steal.py's module docstring).
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import MagicMock

from services.project_monitor import _end_pr_review_pipeline_run_on_failure
from agents.non_retryable import NonRetryableAgentError


class TestEndPrReviewPipelineRunOnFailure:
    def test_non_retryable_error_durably_marks_failed(self):
        """The retain=True branch: a NonRetryableAgentError must go through
        mark_failed() (which sets outcome="failed" internally), not a bare
        end_pipeline_run — otherwise retained_reason is never set and the
        lock is silently reclaimed as stale."""
        mock_manager = MagicMock()
        mock_manager.mark_failed.return_value = True

        result = _end_pr_review_pipeline_run_on_failure(
            mock_manager, "proj", "board", 123, NonRetryableAgentError("cycle limit exceeded"),
        )

        mock_manager.mark_failed.assert_called_once()
        call = mock_manager.mark_failed.call_args
        assert call.kwargs["project"] == "proj"
        assert call.kwargs["board"] == "board"
        assert call.kwargs["issue_number"] == 123
        assert "NonRetryableAgentError" in call.kwargs["reason"]
        mock_manager.end_pipeline_run.assert_not_called()
        assert result is True

    def test_non_retryable_error_returns_false_when_mark_failed_fails(self):
        """The caller relies on this return value to know whether retention
        actually succeeded — must not silently report success."""
        mock_manager = MagicMock()
        mock_manager.mark_failed.return_value = False

        result = _end_pr_review_pipeline_run_on_failure(
            mock_manager, "proj", "board", 123, NonRetryableAgentError("cycle limit exceeded"),
        )

        assert result is False

    def test_ordinary_error_releases_instead_of_retaining(self):
        """The retain=False branch: an ordinary (retryable) exception must
        release the lock via end_pipeline_run(retain_lock=False), allowing
        the next poll to retry — not durably retain it."""
        mock_manager = MagicMock()

        result = _end_pr_review_pipeline_run_on_failure(
            mock_manager, "proj", "board", 123, ValueError("transient failure"),
        )

        mock_manager.mark_failed.assert_not_called()
        mock_manager.end_pipeline_run.assert_called_once_with(
            project="proj",
            board="board",
            issue_number=123,
            reason="PR review stage exception: ValueError",
            retain_lock=False,
            # Not contention -- an ordinary retryable failure keeps setting the
            # cancellation signal exactly as it always did (#148 C1 scoped the
            # suppression to contention only).
            suppress_cancellation=False,
        )
        # No retention was attempted — nothing to report on.
        assert result is None


class TestLockTimeoutReleasesRatherThanRetaining:
    """
    #148: a project resource-lock timeout is contention, not a review failure —
    the stage abandons the review on the first one (pr_review_stage.py's phase
    handlers) precisely so it lands here, and this function must release so the
    next board poll retries. A lock timeout arriving wrapped in something
    non-retryable must still release, which is why the check is explicit rather
    than left to the isinstance() fall-through.
    """

    def test_lock_timeout_releases(self):
        from services.project_checkout_lock import ProjectCheckoutLockTimeoutError

        mock_manager = MagicMock()

        result = _end_pr_review_pipeline_run_on_failure(
            mock_manager, "proj", "board", 123,
            ProjectCheckoutLockTimeoutError("could not acquire lock within 10900.0s"),
        )

        mock_manager.mark_failed.assert_not_called()
        assert mock_manager.end_pipeline_run.call_args.kwargs["retain_lock"] is False
        # #148 C1: the release exists so the next poll retries; the cancellation
        # signal would hide the issue from every path that could do that.
        assert mock_manager.end_pipeline_run.call_args.kwargs["suppress_cancellation"] is True
        assert result is None

    def test_non_retryable_wrapping_a_lock_timeout_still_releases(self):
        from services.project_checkout_lock import ProjectCheckoutLockTimeoutError

        inner = ProjectCheckoutLockTimeoutError("busy")
        wrapper = NonRetryableAgentError("All review phases failed for #123")
        wrapper.__cause__ = inner

        mock_manager = MagicMock()

        result = _end_pr_review_pipeline_run_on_failure(
            mock_manager, "proj", "board", 123, wrapper,
        )

        mock_manager.mark_failed.assert_not_called()
        assert mock_manager.end_pipeline_run.call_args.kwargs["retain_lock"] is False
        assert mock_manager.end_pipeline_run.call_args.kwargs["suppress_cancellation"] is True
        assert result is None


class TestPrReviewFailureReport:
    """
    #148: the run releases the lock for contention, but the two operator-facing
    reports the same handler produces — the 'pr_review_stage' execution-history
    record and the "PR Review Failed" issue comment — were still failure-shaped,
    so pure contention left a bogus 'failed' entry in the history and pasted the
    raw lock-timeout text onto the issue.
    """

    def test_lock_timeout_is_reported_as_contention_without_a_comment(self):
        from services.project_monitor import _pr_review_failure_report
        from services.project_checkout_lock import ProjectCheckoutLockTimeoutError

        outcome, post_comment = _pr_review_failure_report(
            ProjectCheckoutLockTimeoutError("could not acquire lock within 10900.0s")
        )

        assert outcome == 'lock_contention'
        assert post_comment is False

    def test_wrapped_lock_timeout_is_recognised(self):
        from services.project_monitor import _pr_review_failure_report
        from services.project_checkout_lock import ProjectCheckoutLockTimeoutError

        wrapper = NonRetryableAgentError("All review phases failed for #123")
        wrapper.__cause__ = ProjectCheckoutLockTimeoutError("busy")

        assert _pr_review_failure_report(wrapper) == ('lock_contention', False)

    def test_ordinary_failure_is_still_reported_as_failed_with_a_comment(self):
        from services.project_monitor import _pr_review_failure_report

        assert _pr_review_failure_report(ValueError("boom")) == ('failed', True)
