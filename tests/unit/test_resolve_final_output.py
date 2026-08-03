"""
Unit tests for claude.docker_runner.resolve_final_output / _extract_marked_output.

Covers the bug where a session that runs extra turns after posting its real
answer (e.g. because it spawned a background subagent via the Task/Agent tool
and kept going while waiting on it) ends with a short wrap-up turn that would
otherwise clobber the actual deliverable.
"""

import pytest
from claude.docker_runner import resolve_final_output, _extract_marked_output


class TestExtractMarkedOutput:
    def test_no_marker_returns_none(self):
        assert _extract_marked_output("just some plain text") is None

    def test_extracts_content_between_markers(self):
        text = "preamble\n<<<FINAL_OUTPUT>>>\nthe real report\n<<<END_FINAL_OUTPUT>>>\ntrailer"
        assert _extract_marked_output(text) == "the real report"

    def test_missing_closing_marker_takes_rest_of_turn(self):
        text = "<<<FINAL_OUTPUT>>>\nthe real report, no closing tag"
        assert _extract_marked_output(text) == "the real report, no closing tag"


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
