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
        )
        # No retention was attempted — nothing to report on.
        assert result is None
