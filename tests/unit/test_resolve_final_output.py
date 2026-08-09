"""
Unit tests for claude.docker_runner.resolve_final_output / _extract_marked_output.

Covers the bug where a session that runs extra turns after posting its real
answer (e.g. because it spawned a background subagent via the Task/Agent tool
and kept going while waiting on it) ends with a short wrap-up turn that would
otherwise clobber the actual deliverable — including the case where that
trailing turn incidentally mentions the marker token in prose without
actually re-wrapping any content (rounds issue #85, software_architect).
"""

import pytest
from claude.docker_runner import resolve_final_output, _extract_marked_output


class TestExtractMarkedOutput:
    def test_no_marker_returns_none(self):
        assert _extract_marked_output("just some plain text") is None

    def test_extracts_content_between_markers(self):
        text = "preamble\n<<<FINAL_OUTPUT>>>\nthe real report\n<<<END_FINAL_OUTPUT>>>\ntrailer"
        assert _extract_marked_output(text) == "the real report"

    def test_missing_closing_marker_returns_none(self):
        # A start marker with no matching close is not a real block — could be an
        # incidental mention in prose rather than an intentional (if truncated) wrap.
        text = "<<<FINAL_OUTPUT>>>\nthe real report, no closing tag"
        assert _extract_marked_output(text) is None


class TestResolveFinalOutput:
    def test_no_marker_in_any_turn_falls_back_to_last_turn(self):
        turns = ["turn one", "turn two", "final unmarked turn"]
        assert resolve_final_output(turns, fallback="final unmarked turn") == "final unmarked turn"

    def test_prefers_marked_block_over_trailing_wrapup_turn(self):
        turns = [
            "doing some research...",
            "<<<FINAL_OUTPUT>>>\n# Full Report\n\nlots of content here\n<<<END_FINAL_OUTPUT>>>",
            "The specialist agent has completed as well. The analysis I provided above is complete.",
        ]
        fallback = turns[-1]
        result = resolve_final_output(turns, fallback=fallback)
        assert result == "# Full Report\n\nlots of content here"

    def test_last_marked_block_wins_over_earlier_marked_block(self):
        turns = [
            "<<<FINAL_OUTPUT>>>\ndraft version\n<<<END_FINAL_OUTPUT>>>",
            "<<<FINAL_OUTPUT>>>\nrevised version\n<<<END_FINAL_OUTPUT>>>",
        ]
        result = resolve_final_output(turns, fallback=turns[-1])
        assert result == "revised version"

    def test_empty_turns_falls_back(self):
        assert resolve_final_output([], fallback="fallback text") == "fallback text"

    def test_incidental_marker_mention_in_later_turn_does_not_override(self):
        # Regression test for rounds issue #85: software_architect posted its full
        # design correctly wrapped in turn 2, then took one more turn confirming a
        # background subagent's completion that happened to reference the marker
        # token in prose ("...complete between the `<<<FINAL_OUTPUT>>>` markers.")
        # without a closing tag. That turn must not be treated as a new block.
        turns = [
            "researching the codebase...",
            "<<<FINAL_OUTPUT>>>\n# Full Design\n\nlots of real content here\n<<<END_FINAL_OUTPUT>>>",
            "The specialist agent has completed. I've already incorporated its findings "
            "into the design document above, which is complete between the "
            "`<<<FINAL_OUTPUT>>>` markers.\n\nNo corrections needed to the design.",
        ]
        fallback = turns[-1]
        result = resolve_final_output(turns, fallback=fallback)
        assert result == "# Full Design\n\nlots of real content here"
