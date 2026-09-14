"""
Regression tests for the GraphQL rate-limit handling on the card-move and
board-poll paths (pipeline run 4cf816cf).

The incident these are written against, in order:

  1. The account's GraphQL budget was exhausted by board polling. The shared
     circuit breaker opened and was correctly refusing every caller.
  2. A review cycle approved issue #1045's work. The card move to "Testing"
     then failed three times in 16 seconds — 5s and 10s apart — every attempt
     landing inside the same rate-limit window, which still had ~13 minutes to
     run. None of them could have succeeded.
  3. Those failures were logged as "Command '[...]' returned non-zero exit
     status 1." and nothing more, because `str(CalledProcessError)` drops the
     stderr `gh` writes the reason to. The real cause had to be inferred from
     surrounding log lines hours later.

The pure decisions that come out of that are tested here. Two of them --
card_move_retry_delay() and monitor_budget_backoff_seconds() -- are extracted
precisely because their live callers are hard to drive: a loop reached only
from a daemon thread, and a branch inside the monitor's `while True`. The
classification and rendering helpers ARE reachable through
PipelineProgression.move_issue_to_column(), and the sibling file
test_card_move_uses_rate_limited_client.py drives them that way; here they are
covered directly, at their boundaries.

  * describe_graphql_failure() / describe_subprocess_error(): the error text is
    the whole point of (3).
  * card_move_retry_delay(): back off to the quota WINDOW, not to a few
    seconds — (2).
  * monitor_budget_backoff_seconds(): stop the poll loop consuming the last of
    the budget the card move needs — (1).
"""

import math
import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

import subprocess

from services.github_api_client import (
    BREAKER_OPEN_ERROR,
    FAILURE_BREAKER_OPEN,
    FAILURE_OTHER,
    FAILURE_RATE_LIMITED,
    RATE_LIMITED_ERROR,
    classify_graphql_failure,
    describe_graphql_failure,
)
from services.pipeline_progression import CardMoveFailure, describe_subprocess_error
from services.project_monitor import (
    CARD_MOVE_MAX_RATE_LIMIT_WAIT_SECONDS,
    DEFAULT_MONITOR_MIN_BUDGET_FRACTION,
    MONITOR_BUDGET_BACKOFF_MAX_SECONDS,
    MONITOR_MIN_BUDGET_FRACTION_ENV,
    card_move_retry_delay,
    monitor_budget_backoff_seconds,
    monitor_min_budget_fraction,
)


class TestDescribeGraphqlFailure:
    def test_the_breaker_refusal_is_reported_verbatim(self):
        assert describe_graphql_failure({"error": BREAKER_OPEN_ERROR}) == BREAKER_OPEN_ERROR

    def test_error_and_detail_are_both_kept(self):
        text = describe_graphql_failure(
            {"error": RATE_LIMITED_ERROR, "details": "API rate limit exceeded"}
        )
        assert RATE_LIMITED_ERROR in text
        assert "API rate limit exceeded" in text

    def test_stderr_is_preferred_as_the_detail(self):
        text = describe_graphql_failure(
            {"error": "failed_after_retries", "stderr": "HTTP 403: was rate limited"}
        )
        assert "HTTP 403" in text

    def test_body_level_graphql_errors_survive(self):
        """Where GitHub actually reports RATE_LIMIT and INSUFFICIENT_SCOPES."""
        text = describe_graphql_failure(
            {"errors": [{"type": "RATE_LIMIT", "message": "API rate limit exceeded"}]}
        )
        assert "RATE_LIMIT" in text

    def test_a_long_detail_is_truncated_not_dropped(self):
        text = describe_graphql_failure({"error": "boom", "stderr": "x" * 5000})
        assert "boom" in text
        assert len(text) < 1000

    @pytest.mark.parametrize("payload", [None, {}, "", [], 0])
    def test_an_unusable_payload_still_renders_something_printable(self, payload):
        """This text goes into a decision event and a GitHub comment. An
        unexpected shape must not become a second failure on the failure path."""
        text = describe_graphql_failure(payload)
        assert isinstance(text, str)
        assert text.strip()

    def test_never_returns_the_bare_subprocess_string_the_incident_logged(self):
        """The exact regression: the old path logged only this, for every one
        of three consecutive failures."""
        text = describe_graphql_failure({"error": RATE_LIMITED_ERROR, "details": "quota"})
        assert "returned non-zero exit status" not in text


class TestClassifyGraphqlFailure:
    """THE distinction the whole retry policy turns on.

    The breaker is shared across the client's GraphQL, REST, HTTP and CLI
    paths, but GitHub meters those as SEPARATE quotas. Conflating "the shared
    breaker is open" with "this call's quota is exhausted" let a REST
    exhaustion stop a board whose GraphQL budget was entirely healthy.
    """

    def test_a_breaker_refusal_is_not_confirmed_quota_exhaustion(self):
        assert classify_graphql_failure({"error": BREAKER_OPEN_ERROR}) == FAILURE_BREAKER_OPEN

    def test_githubs_own_rejection_is(self):
        assert classify_graphql_failure({"error": RATE_LIMITED_ERROR}) == FAILURE_RATE_LIMITED

    def test_a_body_level_rate_limit_is(self):
        """How GitHub reports a PRIMARY GraphQL rate limit — and it never trips
        the breaker, so this is the only signal a caller gets."""
        assert classify_graphql_failure(
            {"errors": [{"type": "RATE_LIMIT", "message": "API rate limit already exceeded"}]}
        ) == FAILURE_RATE_LIMITED

    def test_a_secondary_rate_limit_in_stderr_is(self):
        assert classify_graphql_failure(
            {"error": "failed_after_retries", "stderr": "You have exceeded a secondary rate limit"}
        ) == FAILURE_RATE_LIMITED

    def test_an_ordinary_failure_is_other(self):
        assert classify_graphql_failure({"error": "parse_error"}) == FAILURE_OTHER

    def test_a_scope_error_is_not_a_rate_limit(self):
        """INSUFFICIENT_SCOPES is permanent and retrying is pointless for a
        different reason — it must not be mistaken for a quota wait."""
        assert classify_graphql_failure(
            {"errors": [{"type": "INSUFFICIENT_SCOPES", "message": "needs read:project"}]}
        ) == FAILURE_OTHER

    @pytest.mark.parametrize("payload", [None, "rate limit", 42, []])
    def test_a_non_dict_payload_is_other(self, payload):
        assert classify_graphql_failure(payload) == FAILURE_OTHER

    def test_breaker_wording_drift_still_classifies_as_a_refusal(self):
        """The breaker's message is the one sentinel containing "rate limit"
        that does NOT mean this call's quota is gone, so the substring fallback
        must check for the breaker first."""
        assert classify_graphql_failure(
            {"error": "GitHub API rate limit guard - circuit breaker open (reworded)"}
        ) == FAILURE_BREAKER_OPEN

    def test_the_client_builds_its_payloads_from_these_constants(self):
        """Re-declaring the literals in a consumer meant a rename in the client
        silently turned every rate-limit classification into 'other'. The
        constants and the code that emits them now live in one module."""
        import inspect
        from services import github_api_client

        source = inspect.getsource(github_api_client)
        assert 'return False, {"error": BREAKER_OPEN_ERROR}' in source
        assert '"error": "GitHub API rate limit exceeded - circuit breaker open"}' not in source


class TestCardMoveFailure:
    def test_only_a_confirmed_quota_failure_is_exhaustion(self):
        assert CardMoveFailure(FAILURE_RATE_LIMITED, "x").is_quota_exhaustion is True

    def test_a_breaker_refusal_is_not(self):
        """The regression guard, stated on the value itself: a shared-breaker
        refusal must not drive the terminal quota path."""
        assert CardMoveFailure(FAILURE_BREAKER_OPEN, "x").is_quota_exhaustion is False

    def test_an_ordinary_failure_is_not(self):
        assert CardMoveFailure(FAILURE_OTHER, "x").is_quota_exhaustion is False


class TestDescribeSubprocessError:
    def test_stderr_is_recovered_from_a_called_process_error(self):
        exc = subprocess.CalledProcessError(
            1, ['gh', 'api', 'graphql'], output='', stderr='API rate limit exceeded'
        )
        text = describe_subprocess_error(exc)
        assert "API rate limit exceeded" in text
        assert "returned non-zero exit status 1" in text

    def test_bytes_stderr_is_decoded(self):
        exc = subprocess.CalledProcessError(
            1, ['gh'], output=b'', stderr=b'HTTP 403: forbidden'
        )
        assert "HTTP 403" in describe_subprocess_error(exc)

    def test_stdout_is_used_when_stderr_is_empty(self):
        exc = subprocess.CalledProcessError(1, ['gh'], output='detail on stdout', stderr='')
        assert "detail on stdout" in describe_subprocess_error(exc)

    def test_a_plain_exception_degrades_to_str(self):
        assert describe_subprocess_error(ValueError("nope")) == "nope"

    def test_the_bare_string_alone_is_what_this_exists_to_prevent(self):
        """Asserted as an inequality because the bare form is precisely what
        made three production card-move failures unattributable."""
        exc = subprocess.CalledProcessError(1, ['gh'], output='', stderr='the real reason')
        assert describe_subprocess_error(exc) != str(exc)


class TestCardMoveRetryDelay:
    """Now returns (verdict, seconds). The verdict is returned rather than
    left for the caller to infer, because the caller got that inference wrong:
    it branched its operator-facing log on `if reset_seconds:`, which disagrees
    with this function for any negative value."""

    def test_a_non_quota_failure_keeps_the_exponential_ladder(self):
        """5s, 10s, 20s — unchanged for an ordinary transient GitHub blip."""
        assert card_move_retry_delay(1, False, None) == ('exponential', 5.0)
        assert card_move_retry_delay(2, False, None) == ('exponential', 10.0)
        assert card_move_retry_delay(3, False, None) == ('exponential', 20.0)

    def test_a_breaker_refusal_takes_the_ladder_not_the_quota_path(self):
        """THE REGRESSION GUARD, at the decision itself. quota_exhausted=False
        is what a shared-breaker refusal produces, and it must not be able to
        reach 'stop' however long the reported window is."""
        verdict, delay = card_move_retry_delay(1, False, 3600.0)
        assert verdict == 'exponential'
        assert delay == 5.0

    def test_a_confirmed_quota_exhaustion_waits_for_the_window(self):
        """The incident's own numbers: ~780s left, against a ladder that spent
        all three attempts inside it."""
        verdict, delay = card_move_retry_delay(1, True, 780.0)
        assert verdict == 'quota_window'
        assert delay == pytest.approx(785.0)

    def test_the_wait_clears_the_reset_boundary(self):
        _verdict, delay = card_move_retry_delay(1, True, 100.0)
        assert delay > 100.0

    def test_a_window_beyond_the_cap_stops(self):
        assert card_move_retry_delay(
            1, True, CARD_MOVE_MAX_RATE_LIMIT_WAIT_SECONDS + 1
        ) == ('stop', None)

    def test_the_cap_boundary_itself_still_waits(self):
        verdict, _delay = card_move_retry_delay(
            1, True, CARD_MOVE_MAX_RATE_LIMIT_WAIT_SECONDS
        )
        assert verdict == 'quota_window'

    def test_an_already_elapsed_window_does_not_impose_a_quota_sized_wait(self):
        assert card_move_retry_delay(1, True, 0.0) == ('exponential', 5.0)
        assert card_move_retry_delay(1, True, -30.0) == ('exponential', 5.0)

    @pytest.mark.parametrize(
        "reset", [None, "780", float('nan'), True, False, object()]
    )
    def test_a_nonsensical_reset_degrades_to_exponential_not_a_raise(self, reset):
        assert card_move_retry_delay(1, True, reset) == ('exponential', 5.0)

    def test_a_custom_base_delay_is_honoured(self):
        assert card_move_retry_delay(2, False, None, base_delay=1.0) == ('exponential', 2.0)

    @pytest.mark.parametrize("quota", [True, False])
    @pytest.mark.parametrize("reset", [None, -1.0, 0.0, 100.0, 100000.0])
    def test_every_combination_yields_a_known_verdict(self, quota, reset):
        verdict, delay = card_move_retry_delay(1, quota, reset)
        assert verdict in {'exponential', 'quota_window', 'stop'}
        assert (delay is None) == (verdict == 'stop')


class TestMonitorBudgetBackoff:
    FLOOR = 0.10
    BASE = 15.0

    def test_a_healthy_budget_polls_as_normal(self):
        assert monitor_budget_backoff_seconds(0.85, self.FLOOR, 1800.0, self.BASE) == 0.0

    def test_exactly_at_the_floor_still_polls(self):
        assert monitor_budget_backoff_seconds(self.FLOOR, self.FLOOR, 1800.0, self.BASE) == 0.0

    def test_below_the_floor_pauses(self):
        assert monitor_budget_backoff_seconds(0.02, self.FLOOR, 300.0, self.BASE) > 0

    def test_unknown_is_not_low(self):
        """None means no real reading exists yet — a cold start, or an
        unreadable Redis mirror. Pausing the monitor on a number that does not
        exist would break every restart, which is the one time the system most
        needs to see its boards. Matches
        GitHubAPIClient.graphql_budget_fraction_remaining()'s contract."""
        assert monitor_budget_backoff_seconds(None, self.FLOOR, 300.0, self.BASE) == 0.0

    @pytest.mark.parametrize("fraction", ["0.05", float('nan'), True, object()])
    def test_a_non_numeric_fraction_is_treated_as_unknown_not_as_a_raise(self, fraction):
        """This sits in the monitor's main loop, where an exception is a dead
        orchestrator."""
        assert monitor_budget_backoff_seconds(fraction, self.FLOOR, 300.0, self.BASE) == 0.0

    def test_the_pause_is_capped(self):
        """Bounded rather than 'sleep until reset' so the monitor keeps
        reporting and picks up an early recovery."""
        delay = monitor_budget_backoff_seconds(0.01, self.FLOOR, 3500.0, self.BASE)
        assert delay <= MONITOR_BUDGET_BACKOFF_MAX_SECONDS

    def test_the_pause_is_never_shorter_than_one_poll_interval(self):
        delay = monitor_budget_backoff_seconds(0.01, self.FLOOR, 1.0, self.BASE)
        assert delay >= self.BASE

    def test_an_unknown_reset_still_produces_a_bounded_pause(self):
        delay = monitor_budget_backoff_seconds(0.01, self.FLOOR, None, self.BASE)
        assert 0 < delay <= MONITOR_BUDGET_BACKOFF_MAX_SECONDS

    def test_a_short_window_is_waited_out_rather_than_over_waited(self):
        delay = monitor_budget_backoff_seconds(0.01, self.FLOOR, 120.0, self.BASE)
        assert delay == pytest.approx(125.0)


class TestMonitorMinBudgetFraction:
    def test_the_default_leaves_headroom_without_silencing_the_monitor(self, monkeypatch):
        monkeypatch.delenv(MONITOR_MIN_BUDGET_FRACTION_ENV, raising=False)
        assert monitor_min_budget_fraction() == DEFAULT_MONITOR_MIN_BUDGET_FRACTION
        assert 0.0 < DEFAULT_MONITOR_MIN_BUDGET_FRACTION < 0.25, (
            "a floor at or above reconciliation's 0.25 would pause polling for a "
            "large part of every hour"
        )

    def test_a_valid_override_is_used(self, monkeypatch):
        monkeypatch.setenv(MONITOR_MIN_BUDGET_FRACTION_ENV, '0.3')
        assert monitor_min_budget_fraction() == 0.3

    @pytest.mark.parametrize("raw", ['nonsense', '', '0', '1', '-0.5', '2'])
    def test_a_typo_falls_back_rather_than_stopping_the_monitor(self, monkeypatch, raw):
        """0 disables the guard silently and 1 pauses polling forever — both
        are treated as typos, matching
        github_project_manager._reconcile_min_budget_fraction()."""
        monkeypatch.setenv(MONITOR_MIN_BUDGET_FRACTION_ENV, raw)
        assert monitor_min_budget_fraction() == DEFAULT_MONITOR_MIN_BUDGET_FRACTION
