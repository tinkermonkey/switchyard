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

    def test_unclosed_marker_strips_preamble_and_marker_literal(self):
        # Regression test: a business_analyst run (heimdall issue #145) opened
        # <<<FINAL_OUTPUT>>> and never emitted the closing tag in its only turn.
        # Previously this fell all the way back to the raw turn text, posting the
        # preamble commentary AND the literal "<<<FINAL_OUTPUT>>>" marker verbatim
        # to the GitHub comment. The unclosed marker should still win over the raw
        # fallback since no turn had a real closed block to protect.
        turn = (
            "Confirms repo is `tinkermonkey/heimdall`, no `.github/` workflows exist yet.\n\n"
            "<<<FINAL_OUTPUT>>>\n## Executive Summary\n\nGitHub Pages has been enabled..."
        )
        result = resolve_final_output([turn], fallback=turn)
        assert result == "## Executive Summary\n\nGitHub Pages has been enabled..."
        assert "<<<FINAL_OUTPUT>>>" not in result
        assert "Confirms repo is" not in result

    def test_unclosed_marker_prefers_last_occurrence_across_turns(self):
        turns = [
            "<<<FINAL_OUTPUT>>>\ndraft, never closed",
            "revising...\n<<<FINAL_OUTPUT>>>\nfinal version, also never closed",
        ]
        result = resolve_final_output(turns, fallback=turns[-1])
        assert result == "final version, also never closed"

    def test_closed_block_still_wins_over_later_unclosed_marker(self):
        # A real closed block anywhere must never be discarded in favor of an
        # unclosed marker in a later turn — the unclosed-marker fallback only
        # applies when NO turn produced a closed block at all.
        turns = [
            "<<<FINAL_OUTPUT>>>\n# Real Report\n<<<END_FINAL_OUTPUT>>>",
            "one more thought: <<<FINAL_OUTPUT>>> mentioned without closing it",
        ]
        result = resolve_final_output(turns, fallback=turns[-1])
        assert result == "# Real Report"

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
