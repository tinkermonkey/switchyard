"""
Unit tests for ClaudeCodeBreaker.detect_from_event() — the structural,
protocol-level usage-limit detector (the primary detection mechanism).

These are the safety-critical tests: an agent's ordinary conversation, code,
or documentation discussing rate limits must NEVER trip the breaker. Only the
Claude Code CLI's own type-tagged event fields should. Event shapes below are
taken verbatim from real captured container output (see the incident
investigation this fix is based on).
"""

import pytest
from datetime import datetime, timedelta, timezone
from monitoring.claude_code_breaker import ClaudeCodeBreaker


def make_breaker():
    """A ClaudeCodeBreaker with no Redis connection (pure in-memory state)."""
    breaker = ClaudeCodeBreaker.__new__(ClaudeCodeBreaker)
    breaker.state = ClaudeCodeBreaker.CLOSED
    breaker.opened_at = None
    breaker.reset_time = None
    breaker.rate_limit_type = None
    breaker.failure_count = 0
    breaker.max_failures = 1
    breaker.redis_client = None
    return breaker


REAL_REJECTED_RATE_LIMIT_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "rejected",
        "resetsAt": 1786378800,
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
        "overageDisabledReason": "out_of_credits",
        "isUsingOverage": False,
    },
    "uuid": "13ef6323-d16f-4593-8fc2-251ac9fc2c13",
    "session_id": "53dc2d26-d820-4509-a9ed-a3f98b22d32b",
}

REAL_ASSISTANT_ERROR_EVENT = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "type": "message",
        "content": [{"type": "text", "text": "You've hit your session limit · resets 4:20pm (UTC)"}],
    },
    "session_id": "53dc2d26-d820-4509-a9ed-a3f98b22d32b",
    "error": "rate_limit",
    "is_api_error_message": True,
}

REAL_RESULT_ERROR_EVENT = {
    "type": "result",
    "is_error": True,
    "terminal_reason": "api_error",
    "api_error_status": 429,
    "result": "You've hit your session limit · resets 4:20pm (UTC)",
    "session_id": "53dc2d26-d820-4509-a9ed-a3f98b22d32b",
}


class TestStructuralDetectionPositive:
    """The three real, confirmed structural signals must all be detected."""

    def test_rate_limit_event_rejected_status_detected(self):
        breaker = make_breaker()
        signal = breaker.detect_from_event(REAL_REJECTED_RATE_LIMIT_EVENT)
        assert signal is not None
        assert signal["source"] == "rate_limit_event"
        assert signal["raw"]["status"] == "rejected"

    def test_assistant_error_field_detected(self):
        breaker = make_breaker()
        signal = breaker.detect_from_event(REAL_ASSISTANT_ERROR_EVENT)
        assert signal is not None
        assert signal["source"] == "assistant_error_field"

    def test_result_terminal_reason_detected(self):
        breaker = make_breaker()
        signal = breaker.detect_from_event(REAL_RESULT_ERROR_EVENT)
        assert signal is not None
        assert signal["source"] == "result_terminal_reason"


class TestStructuralDetectionSafeAgainstFalsePositives:
    """
    The core safety property: an agent's own conversational output — even text
    that explicitly discusses rate limits, in any event type — must never trip
    the breaker unless the CLI's own structural fields say so.
    """

    def test_assistant_turn_discussing_rate_limits_is_not_detected(self):
        """An agent writing docs/code about rate limiting must not self-trigger."""
        breaker = make_breaker()
        event = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "type": "message",
                "content": [{
                    "type": "text",
                    "text": (
                        "I've implemented the rate limiter. When a client hits their "
                        "session limit, resets happen on a rolling 5 hour window."
                    ),
                }],
            },
            "session_id": "some-session",
            # No error / is_api_error_message fields — this is a normal turn.
        }
        assert breaker.detect_from_event(event) is None

    def test_successful_result_with_rate_limit_discussion_is_not_detected(self):
        """
        A successful run whose final answer happens to discuss rate limiting
        (e.g. a PR review of rate-limiter code) must not be text-matched — the
        'result' field is overloaded (real answer on success, error string on
        failure) and must only ever be inspected once is_error/terminal_reason
        already confirm this is an error turn.
        """
        breaker = make_breaker()
        event = {
            "type": "result",
            "is_error": False,
            "terminal_reason": "completed",
            "result": (
                "## PR Review\n\nThe `DigestNotificationAdapter` correctly implements "
                "session limit checks — when a client has hit their limit, resets "
                "are computed from `rate_limit_info.resetsAt`. Looks good."
            ),
            "session_id": "some-session",
        }
        assert breaker.detect_from_event(event) is None

    def test_rate_limit_event_allowed_status_is_not_detected(self):
        """An informational 'allowed' status (call still succeeded) must not trip."""
        breaker = make_breaker()
        event = {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"},
        }
        assert breaker.detect_from_event(event) is None

    def test_rate_limit_event_allowed_warning_status_is_not_detected(self):
        """A soft quota warning (call still succeeded) must not trip — confirmed
        real production shape: utilization climbing toward a 7-day cap without
        ever being rejected."""
        breaker = make_breaker()
        event = {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed_warning",
                "rateLimitType": "seven_day",
                "utilization": 0.95,
                "surpassedThreshold": 0.75,
            },
        }
        assert breaker.detect_from_event(event) is None

    def test_unrecognized_status_does_not_trip_but_logs(self, caplog):
        """
        Conservative-to-availability: an unrecognized future status value must
        NOT trip the breaker (which would block every project on the
        orchestrator), only log loudly for triage.
        """
        breaker = make_breaker()
        event = {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "some_new_status_we_have_never_seen"},
        }
        with caplog.at_level("WARNING"):
            signal = breaker.detect_from_event(event)
        assert signal is None
        assert any("Unrecognized rate_limit_info.status" in r.message for r in caplog.records)

    def test_tool_use_event_is_not_detected(self):
        breaker = make_breaker()
        event = {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
        assert breaker.detect_from_event(event) is None

    def test_system_init_event_is_not_detected(self):
        breaker = make_breaker()
        event = {"type": "system", "subtype": "init", "session_id": "abc"}
        assert breaker.detect_from_event(event) is None

    def test_non_dict_event_is_not_detected(self):
        breaker = make_breaker()
        assert breaker.detect_from_event(None) is None
        assert breaker.detect_from_event("not a dict") is None
        assert breaker.detect_from_event([1, 2, 3]) is None

    def test_assistant_authentication_error_does_not_trip_rate_limit_breaker(self):
        """
        Regression coverage for Bug 2: the assistant branch used to check only
        truthiness of `error`, so ANY API error type (auth failures, etc.) that
        the CLI wraps in the same is_api_error_message envelope would trip the
        SAME global Claude Code breaker meant specifically for rate limits —
        freezing every project on the orchestrator for an unrelated outage.
        Only error == "rate_limit" exactly must trip.
        """
        breaker = make_breaker()
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Authentication failed"}]},
            "session_id": "some-session",
            "error": "authentication_error",
            "is_api_error_message": True,
        }
        assert breaker.detect_from_event(event) is None

    def test_assistant_overloaded_error_does_not_trip_rate_limit_breaker(self):
        breaker = make_breaker()
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Overloaded"}]},
            "error": "overloaded_error",
            "is_api_error_message": True,
        }
        assert breaker.detect_from_event(event) is None

    def test_result_api_error_with_non_429_status_does_not_trip(self):
        """
        Regression coverage for Bug 2: terminal_reason=="api_error" is a broad
        bucket covering any API-level failure, not just 429 rate limits (e.g.
        a 500 or 401 also sets terminal_reason=="api_error"). Only the
        confirmed real rate-limit shape (api_error_status==429) must trip.
        """
        breaker = make_breaker()
        event = {
            "type": "result",
            "is_error": True,
            "terminal_reason": "api_error",
            "api_error_status": 500,
            "result": "Internal server error",
            "session_id": "some-session",
        }
        assert breaker.detect_from_event(event) is None

    def test_result_api_error_without_status_field_does_not_trip(self):
        breaker = make_breaker()
        event = {
            "type": "result",
            "is_error": True,
            "terminal_reason": "api_error",
            "result": "Some API error with no status field",
        }
        assert breaker.detect_from_event(event) is None

    def test_assistant_error_field_without_is_api_error_message_not_detected(self):
        """error field alone, without is_api_error_message, is not sufficient —
        both must be present together (matches the real observed shape)."""
        breaker = make_breaker()
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "discussing an error"}]},
            "error": "rate_limit",
            # is_api_error_message deliberately absent
        }
        assert breaker.detect_from_event(event) is None

    def test_result_is_error_without_api_error_terminal_reason_not_detected(self):
        """A generic error result (e.g. a tool failure) is not a rate limit."""
        breaker = make_breaker()
        event = {
            "type": "result",
            "is_error": True,
            "terminal_reason": "tool_error",
            "result": "Bash command failed",
        }
        assert breaker.detect_from_event(event) is None


class TestResetTimeFromSignal:
    """reset_time_from_signal() must prefer the machine-readable epoch over any
    text parsing, and fail safe to the existing 1h default convention."""

    def test_valid_resets_at_epoch_used_directly(self):
        breaker = make_breaker()
        future_epoch = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())
        signal = {
            "source": "rate_limit_event",
            "raw": {"resetsAt": future_epoch, "rateLimitType": "five_hour"},
        }
        reset_time, rate_limit_type = breaker.reset_time_from_signal(signal)
        assert abs((reset_time.timestamp() - future_epoch)) < 1
        assert rate_limit_type == "five_hour"

    def test_missing_resets_at_falls_back_to_1h_default(self):
        breaker = make_breaker()
        signal = {"source": "rate_limit_event", "raw": {"rateLimitType": "five_hour"}}
        before = datetime.now(timezone.utc)
        reset_time, rate_limit_type = breaker.reset_time_from_signal(signal)
        assert timedelta(minutes=55) < (reset_time - before) < timedelta(minutes=65)
        assert rate_limit_type == "five_hour"

    def test_resets_at_in_the_past_falls_back_to_1h_default(self):
        breaker = make_breaker()
        past_epoch = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
        signal = {"source": "rate_limit_event", "raw": {"resetsAt": past_epoch}}
        before = datetime.now(timezone.utc)
        reset_time, _ = breaker.reset_time_from_signal(signal)
        assert timedelta(minutes=55) < (reset_time - before) < timedelta(minutes=65)

    def test_resets_at_implausibly_far_future_falls_back_to_1h_default(self):
        breaker = make_breaker()
        far_epoch = int((datetime.now(timezone.utc) + timedelta(days=90)).timestamp())
        signal = {"source": "rate_limit_event", "raw": {"resetsAt": far_epoch}}
        before = datetime.now(timezone.utc)
        reset_time, _ = breaker.reset_time_from_signal(signal)
        assert timedelta(minutes=55) < (reset_time - before) < timedelta(minutes=65)

    def test_non_numeric_resets_at_falls_back_to_1h_default(self):
        breaker = make_breaker()
        signal = {"source": "rate_limit_event", "raw": {"resetsAt": "not-a-number"}}
        before = datetime.now(timezone.utc)
        reset_time, _ = breaker.reset_time_from_signal(signal)
        assert timedelta(minutes=55) < (reset_time - before) < timedelta(minutes=65)

    def test_non_rate_limit_event_signal_has_no_rate_limit_type(self):
        """assistant/result-sourced signals carry no rate_limit_info at all."""
        breaker = make_breaker()
        signal = {"source": "result_terminal_reason", "raw": REAL_RESULT_ERROR_EVENT}
        reset_time, rate_limit_type = breaker.reset_time_from_signal(signal)
        assert rate_limit_type is None
        assert reset_time is not None


class TestTripWithRateLimitType:
    def test_trip_stores_rate_limit_type(self):
        breaker = make_breaker()
        breaker._schedule_resume_check = lambda: None  # avoid touching the real scheduler
        breaker.trip(datetime.now(timezone.utc) + timedelta(hours=1), rate_limit_type="five_hour")
        assert breaker.state == ClaudeCodeBreaker.OPEN
        assert breaker.rate_limit_type == "five_hour"

    def test_close_clears_rate_limit_type(self):
        breaker = make_breaker()
        breaker._schedule_resume_check = lambda: None
        breaker.trip(datetime.now(timezone.utc) + timedelta(hours=1), rate_limit_type="five_hour")
        breaker.close()
        assert breaker.rate_limit_type is None

    def test_trip_backward_compatible_with_no_args(self):
        """Existing call sites (e.g. the pre-flight-open path in agent_executor.py)
        call trip() with no arguments at all — must still work."""
        breaker = make_breaker()
        breaker._schedule_resume_check = lambda: None
        breaker.trip()
        assert breaker.state == ClaudeCodeBreaker.OPEN
        assert breaker.reset_time is not None
        assert breaker.rate_limit_type is None
