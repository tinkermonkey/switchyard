"""
Unit tests for Claude Code rate limit detection via JSON stream output.

Regression test for RC1: detect_session_limit() was silently returning (False, None)
for structured JSON events because the raw line exceeded MAX_MESSAGE_LENGTH (150 chars).
The fix in docker_runner.py read_stream() extracts the inner text content from the JSON
envelope before calling detect_session_limit().
"""

import json
import pytest
from monitoring.claude_code_breaker import ClaudeCodeBreaker, ClaudeCodeRateLimitError


class TestDetectSessionLimitJsonExtraction:
    """Tests that confirm Fix 1: text extraction from Claude Code JSON stream events."""

    def setup_method(self):
        self.breaker = ClaudeCodeBreaker.__new__(ClaudeCodeBreaker)
        self.breaker.MAX_MESSAGE_LENGTH = ClaudeCodeBreaker.MAX_MESSAGE_LENGTH
        self.breaker.SESSION_LIMIT_PATTERN = ClaudeCodeBreaker.SESSION_LIMIT_PATTERN

    def _make_assistant_event(self, text: str) -> str:
        """Build a realistic Claude Code stdout JSON line containing a rate limit message."""
        return json.dumps({
            "type": "assistant",
            "session_id": "sess_01AbCdEfGhIjKlMnOpQrStUv",
            "message": {
                "id": "msg_01AbCdEfGhIjKlMnOpQrStUv",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "model": "claude-opus-4-5-20251101",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 12},
            },
        })

    def _extract_text_from_json(self, line: str) -> str:
        """Mirror the extraction logic from docker_runner.py read_stream()."""
        try:
            parsed = json.loads(line)
            content = parsed.get("message", {}).get("content", [])
            for item in content if isinstance(content, list) else []:
                if isinstance(item, dict) and item.get("type") == "text":
                    return item.get("text", line)
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
        return line

    # ------------------------------------------------------------------
    # RC1 regression: raw JSON envelope exceeds MAX_MESSAGE_LENGTH
    # ------------------------------------------------------------------

    def test_raw_json_event_exceeds_max_length_returns_false(self):
        """Raw Claude Code JSON line is >150 chars — detect_session_limit returns False without extraction."""
        raw = self._make_assistant_event("You've hit your limit · resets 4pm (UTC)")
        assert len(raw) > self.breaker.MAX_MESSAGE_LENGTH, (
            f"Test precondition failed: raw line is only {len(raw)} chars, expected >150"
        )
        is_limit, reset_time = self.breaker.detect_session_limit(raw)
        assert is_limit is False  # The bug: silently missed by the length guard

    def test_extracted_text_detects_limit(self):
        """After extraction, detect_session_limit correctly identifies the rate limit."""
        raw = self._make_assistant_event("You've hit your limit · resets 4pm (UTC)")
        text = self._extract_text_from_json(raw)
        is_limit, reset_time = self.breaker.detect_session_limit(text)
        assert is_limit is True
        assert reset_time is not None
        assert reset_time.hour == 16  # 4pm UTC

    def test_extraction_various_reset_times(self):
        """Extraction works for different reset time formats seen in production."""
        cases = [
            ("You've hit your limit · resets 3pm (UTC)", 15),
            ("You've hit your limit · resets 5pm (UTC)", 17),
            ("hit your limit · resets 12am (UTC)", 0),
        ]
        for message, expected_hour in cases:
            raw = self._make_assistant_event(message)
            text = self._extract_text_from_json(raw)
            is_limit, reset_time = self.breaker.detect_session_limit(text)
            assert is_limit is True, f"Failed to detect limit for: {message}"
            assert reset_time.hour == expected_hour, (
                f"Expected hour {expected_hour}, got {reset_time.hour} for: {message}"
            )

    # ------------------------------------------------------------------
    # Extraction fallback: malformed JSON falls back to raw line
    # ------------------------------------------------------------------

    def test_malformed_json_falls_back_to_raw_line(self):
        """Non-JSON lines (e.g. stderr) are passed through unchanged."""
        non_json = "You've hit your limit · resets 4pm (UTC)"  # Plain text, short enough
        result = self._extract_text_from_json(non_json)
        assert result == non_json

    def test_extraction_on_partial_json_falls_back(self):
        """Partial/invalid JSON falls back to raw line."""
        partial = '{"type": "assistant", "message": {"content": ['
        result = self._extract_text_from_json(partial)
        assert result == partial

    def test_extraction_on_event_without_text_content_falls_back(self):
        """JSON events without text content (e.g. tool_use) fall back to the raw line."""
        raw = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "tool_1", "name": "Bash", "input": {}}]
            }
        })
        result = self._extract_text_from_json(raw)
        assert result == raw

    def test_extraction_on_non_rate_limit_text_content(self):
        """Text content that does not contain a rate limit message is not falsely detected."""
        raw = self._make_assistant_event(
            "You've hit your limit of 10 retries for this operation."
        )
        text = self._extract_text_from_json(raw)
        # This message does NOT have "resets" — should not match
        is_limit, _ = self.breaker.detect_session_limit(text)
        assert is_limit is False


class TestClaudeCodeRateLimitError:
    """Verify the typed exception exists and is correctly classified."""

    def test_exception_is_importable(self):
        from monitoring.claude_code_breaker import ClaudeCodeRateLimitError
        assert issubclass(ClaudeCodeRateLimitError, Exception)

    def test_isinstance_check(self):
        e = ClaudeCodeRateLimitError("test")
        assert isinstance(e, ClaudeCodeRateLimitError)
        assert isinstance(e, Exception)

    def test_docker_runner_imports_same_class(self):
        from monitoring.claude_code_breaker import ClaudeCodeRateLimitError as Canonical
        from claude.docker_runner import ClaudeCodeRateLimitError as FromRunner
        assert Canonical is FromRunner, (
            "docker_runner.py must import from monitoring.claude_code_breaker, "
            "not use the fallback local class, to ensure isinstance() works across modules"
        )
