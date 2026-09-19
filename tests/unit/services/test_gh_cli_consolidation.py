"""
Tests for the GitHubAPIClient consolidation work (GitHub circuit breaker
coverage): gh_cli()'s hardened interface (GhCliResult, timeout,
acceptable_exit_codes, env override) and GitHubBreaker's new generic-failure
trip path.

Context: an audit found ~40 raw `subprocess.run(['gh', ...])` call sites
across the codebase bypassing GitHubBreaker entirely. This is step 1-2 of the
consolidation plan -- hardening gh_cli() and GitHubBreaker before any call
site is migrated onto them. These tests exist to prove the hardened interface
actually covers what the raw call sites need, and that the new generic-failure
trip does not disturb the existing rate-limit trip path.
"""

import json
import os
import subprocess
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, call

import pytest

from services.github_api_client import (  # noqa: E402
    GitHubAPIClient,
    GitHubBreaker,
    GhCliResult,
    CREDENTIAL_PREFERENCE_ENV,
)


@pytest.fixture
def client():
    """A client with credential preference neutralised and Redis mirroring
    stubbed, matching the pattern in test_github_credential_routing.py."""
    saved = os.environ.get(CREDENTIAL_PREFERENCE_ENV)
    os.environ.pop(CREDENTIAL_PREFERENCE_ENV, None)
    c = GitHubAPIClient()
    yield c
    if saved is None:
        os.environ.pop(CREDENTIAL_PREFERENCE_ENV, None)
    else:
        os.environ[CREDENTIAL_PREFERENCE_ENV] = saved


class TestGhCliResultLegacyCompatibility:
    """The 3 existing gh_cli() callers (github_project_manager.py's project
    create/link/field-list) read the result via .get()/[] against the old ad
    hoc dict shape. GhCliResult must keep answering those the same way."""

    def test_success_with_dict_data_supports_dict_style_get(self):
        result = GhCliResult(success=True, data={"id": "PVT_1", "number": 7})
        assert result.get("id") == "PVT_1"
        assert result["number"] == 7
        assert "id" in result
        assert result.get("missing", "default") == "default"

    def test_failure_exposes_legacy_error_key(self):
        result = GhCliResult(success=False, error_kind="generic", stderr="boom", returncode=1)
        assert result.get("error") == "cli_error"
        assert result["stderr"] == "boom"
        assert result["exit_code"] == 1

    def test_rate_limited_maps_to_legacy_sentinel(self):
        from services.github_api_client import RATE_LIMITED_ERROR
        result = GhCliResult(success=False, error_kind="rate_limited", stderr="rate limit exceeded")
        assert result.get("error") == RATE_LIMITED_ERROR

    def test_circuit_open_maps_to_legacy_sentinel(self):
        from services.github_api_client import BREAKER_OPEN_ERROR
        result = GhCliResult(success=False, error_kind="circuit_open")
        assert result.get("error") == BREAKER_OPEN_ERROR

    def test_success_with_raw_stdout_wraps_as_output_key(self):
        result = GhCliResult(success=True, data="plain text output")
        assert result.get("output") == "plain text output"


class TestGhCliBreakerGating:
    def test_open_breaker_rejects_without_touching_subprocess(self, client):
        client.breaker.state = GitHubBreaker.OPEN
        client.breaker.reset_time = None
        with patch("subprocess.run") as mock_run:
            success, result = client.gh_cli(["gh", "issue", "view", "1"])
        assert success is False
        assert result.error_kind == "circuit_open"
        mock_run.assert_not_called()


class TestGhCliTimeoutAndExitCodes:
    def test_custom_timeout_is_passed_to_subprocess(self, client):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            client.gh_cli(["gh", "pr", "checks", "1"], timeout=60)
        assert mock_run.call_args.kwargs["timeout"] == 60

    def test_default_timeout_is_30(self, client):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            client.gh_cli(["gh", "issue", "view", "1"])
        assert mock_run.call_args.kwargs["timeout"] == 30

    def test_gh_pr_checks_exit_code_1_is_success_when_acceptable(self, client):
        """`gh pr checks` returns exit 1 for pending checks and 8 for failing
        checks -- both valid answers a caller wants to parse, not failures."""
        mock_result = MagicMock(returncode=1, stdout='[{"name": "ci"}]', stderr="")
        with patch("subprocess.run", return_value=mock_result):
            success, result = client.gh_cli(
                ["gh", "pr", "checks", "1"], acceptable_exit_codes={0, 1, 8}
            )
        assert success is True
        assert result.data == [{"name": "ci"}]

    def test_exit_code_1_is_failure_when_not_in_acceptable_set(self, client):
        mock_result = MagicMock(returncode=1, stdout="", stderr="some error")
        with patch("subprocess.run", return_value=mock_result):
            success, result = client.gh_cli(["gh", "issue", "view", "1"])
        assert success is False
        assert result.returncode == 1


class TestGhCliEnvOverride:
    def test_explicit_env_override_is_used_verbatim(self, client):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        custom_env = {"GH_TOKEN": "explicit-token"}
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            client.gh_cli(["gh", "issue", "view", "1"], env=custom_env)
        assert mock_run.call_args.kwargs["env"] == custom_env

    def test_default_env_uses_routed_credential(self, client):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run, \
             patch.object(client, "_auth_env", return_value=({"GH_TOKEN": "routed"}, "pat")) as mock_auth:
            client.gh_cli(["gh", "issue", "view", "1"])
        mock_auth.assert_called_once()
        assert mock_run.call_args.kwargs["env"] == {"GH_TOKEN": "routed"}


class TestGhCliCwd:
    def test_cwd_is_passed_to_subprocess(self, client):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            client.gh_cli(["gh", "pr", "create"], cwd="/workspace/widgets")
        assert mock_run.call_args.kwargs["cwd"] == "/workspace/widgets"

    def test_default_cwd_is_none(self, client):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            client.gh_cli(["gh", "issue", "view", "1"])
        assert mock_run.call_args.kwargs["cwd"] is None

    def test_retry_forwards_cwd(self, client):
        transient = MagicMock(returncode=1, stdout="", stderr="temporarily unavailable")
        success_result = MagicMock(returncode=0, stdout="{}", stderr="")
        with patch("subprocess.run", side_effect=[transient, success_result]) as mock_run, \
             patch("time.sleep"):
            client.gh_cli(["gh", "pr", "create"], cwd="/workspace/widgets")
        for call in mock_run.call_args_list:
            assert call.kwargs["cwd"] == "/workspace/widgets"


class TestGhCliStderrPreservedForTextMatching:
    """Several migrated callers branch on stderr text ("already exists",
    "already linked", "is not a draft", "'approved' not found") -- the
    hardened gh_cli() must keep stderr available on both success and failure."""

    def test_stderr_preserved_on_classified_client_error(self, client):
        mock_result = MagicMock(returncode=1, stdout="", stderr="HTTP 422: label 'approved' not found")
        with patch("subprocess.run", return_value=mock_result):
            success, result = client.gh_cli(["gh", "pr", "edit", "1", "--add-label", "approved"])
        assert success is False
        assert "'approved' not found" in result.stderr

    def test_stderr_preserved_on_generic_failure(self, client):
        mock_result = MagicMock(returncode=1, stdout="", stderr="already exists")
        with patch("subprocess.run", return_value=mock_result):
            success, result = client.gh_cli(["gh", "pr", "create"])
        assert success is False
        assert result.stderr == "already exists"


class TestGhCliRetryPreservesKwargs:
    """The recursive retry call must forward timeout/acceptable_exit_codes/env,
    not silently fall back to defaults on retry (a real bug in the pre-image
    which only forwarded `cmd` and the incrementing `retries` counter)."""

    def test_retry_forwards_timeout_and_env(self, client):
        transient = MagicMock(returncode=1, stdout="", stderr="temporarily unavailable")
        success_result = MagicMock(returncode=0, stdout="{}", stderr="")
        custom_env = {"GH_TOKEN": "x"}
        with patch("subprocess.run", side_effect=[transient, success_result]) as mock_run, \
             patch("time.sleep"):
            success, result = client.gh_cli(
                ["gh", "issue", "view", "1"], timeout=15, env=custom_env
            )
        assert success is True
        assert mock_run.call_count == 2
        for call in mock_run.call_args_list:
            assert call.kwargs["timeout"] == 15
            assert call.kwargs["env"] == custom_env


class TestGhCliCustomHeadersPassthrough:
    """The two `addSubIssue` mutation call sites need `-H 'GraphQL-Features:
    sub_issues'`. gh_cli() has no first-class header parameter -- cmd is
    forwarded to subprocess verbatim -- so this proves that claim rather than
    just asserting it in a docstring."""

    def test_custom_header_flags_reach_subprocess_unmodified(self, client):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        cmd = [
            "gh", "api", "graphql",
            "-H", "GraphQL-Features: sub_issues",
            "-f", "query=mutation { addSubIssue(...) { issue { id } } }",
        ]
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            client.gh_cli(cmd)
        assert mock_run.call_args.args[0] == cmd


class TestGhCliErrorClassification:
    """gh_cli()'s error_kind is what migrated callers are meant to branch on
    instead of re-deriving it from raw stderr -- these must actually come out
    right, not just preserve stderr text."""

    def test_http_410_classified_as_not_found(self, client):
        mock_result = MagicMock(returncode=1, stdout="", stderr="HTTP 410: Gone, was deleted")
        with patch("subprocess.run", return_value=mock_result):
            success, result = client.gh_cli(["gh", "issue", "view", "1"])
        assert success is False
        assert result.error_kind == "not_found"

    @pytest.mark.parametrize("http_code", ["HTTP 404", "HTTP 403", "HTTP 401", "HTTP 422"])
    def test_client_errors_classified_as_forbidden(self, client, http_code):
        mock_result = MagicMock(returncode=1, stdout="", stderr=f"{http_code}: some detail")
        with patch("subprocess.run", return_value=mock_result):
            success, result = client.gh_cli(["gh", "issue", "view", "1"])
        assert success is False
        assert result.error_kind == "forbidden"

    def test_classified_client_errors_do_not_count_toward_outage_trip(self, client):
        """410/404/403/401/422 are expected, specific negative answers about
        one resource -- not outage evidence -- so they must never accumulate
        toward the generic-failure breaker trip."""
        mock_result = MagicMock(returncode=1, stdout="", stderr="HTTP 404: Not Found")
        with patch("subprocess.run", return_value=mock_result):
            for _ in range(GitHubBreaker.GENERIC_FAILURE_THRESHOLD * 2):
                client.gh_cli(["gh", "issue", "view", "1"])
        assert client.breaker.is_open() is False


class TestGhCliRateLimitDetection:
    """The breaker's original, primary purpose -- must still work end to end
    through the hardened gh_cli(), not just via GitHubBreaker.trip() in
    isolation."""

    def test_rate_limit_in_stderr_trips_breaker_and_classifies(self, client):
        mock_result = MagicMock(returncode=1, stdout="", stderr="API rate limit exceeded for user")
        with patch("subprocess.run", return_value=mock_result):
            success, result = client.gh_cli(["gh", "issue", "view", "1"])
        assert success is False
        assert result.error_kind == "rate_limited"
        assert client.breaker.is_open() is True
        assert client.breaker.trip_reason == "rate_limit"

    def test_rate_limit_in_stdout_also_detected(self, client):
        mock_result = MagicMock(returncode=1, stdout="you have exceeded a secondary rate limit", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            success, result = client.gh_cli(["gh", "issue", "view", "1"])
        assert result.error_kind == "rate_limited"

    def test_rate_limit_trip_keeps_the_long_recovery_window_not_the_short_one(self, client):
        mock_result = MagicMock(returncode=1, stdout="", stderr="rate limit exceeded")
        with patch("subprocess.run", return_value=mock_result):
            client.gh_cli(["gh", "issue", "view", "1"])
        window = (client.breaker.reset_time - client.breaker.opened_at).total_seconds()
        assert window > GitHubBreaker.GENERIC_FAILURE_RECOVERY_SECONDS * 10


class TestGhCliRetryBehavior:
    def test_retries_on_temporarily_text_and_eventually_succeeds(self, client):
        transient = MagicMock(returncode=1, stdout="", stderr="temporarily unavailable, try again")
        success_result = MagicMock(returncode=0, stdout="{}", stderr="")
        with patch("subprocess.run", side_effect=[transient, success_result]) as mock_run, \
             patch("time.sleep") as mock_sleep:
            success, result = client.gh_cli(["gh", "issue", "view", "1"])
        assert success is True
        assert mock_run.call_count == 2
        # _apply_backoff() also sleeps on every attempt regardless of retry
        # outcome, so this only confirms the retry's own 2s backoff fired,
        # not that sleep was called exactly once overall.
        assert call(2) in mock_sleep.call_args_list

    def test_retries_on_timeout_text_in_stderr(self, client):
        transient = MagicMock(returncode=1, stdout="", stderr="request timeout, please retry")
        success_result = MagicMock(returncode=0, stdout="{}", stderr="")
        with patch("subprocess.run", side_effect=[transient, success_result]), patch("time.sleep"):
            success, result = client.gh_cli(["gh", "issue", "view", "1"])
        assert success is True

    def test_retries_exhausted_returns_generic_failure_after_four_attempts(self, client):
        transient = MagicMock(returncode=1, stdout="", stderr="temporarily unavailable")
        with patch("subprocess.run", return_value=transient) as mock_run, patch("time.sleep"):
            success, result = client.gh_cli(["gh", "issue", "view", "1"])
        assert success is False
        assert result.error_kind == "generic"
        # 1 initial attempt + 3 retries (retries=0,1,2 each retry once more)
        assert mock_run.call_count == 4

    def test_retries_exhausted_counts_once_not_once_per_attempt(self, client):
        """Only the terminal failure should count toward the outage streak --
        not every intermediate retry attempt, which would trip the breaker
        four times faster than a caller retrying transient errors deserves."""
        transient = MagicMock(returncode=1, stdout="", stderr="temporarily unavailable")
        with patch("subprocess.run", return_value=transient), patch("time.sleep"):
            client.gh_cli(["gh", "issue", "view", "1"])
        assert client.breaker._generic_failure_count == 1


class TestGhCliAcceptableExitCodeEight:
    def test_gh_pr_checks_exit_code_8_is_success_when_acceptable(self, client):
        """gh pr checks: 0 = all pass, 1 = pending, 8 = some failing -- all
        three are valid answers the caller wants to parse."""
        mock_result = MagicMock(returncode=8, stdout='[{"name": "ci", "bucket": "fail"}]', stderr="")
        with patch("subprocess.run", return_value=mock_result):
            success, result = client.gh_cli(
                ["gh", "pr", "checks", "1"], acceptable_exit_codes={0, 1, 8}
            )
        assert success is True
        assert result.data == [{"name": "ci", "bucket": "fail"}]


class TestGitHubBreakerGenericFailureTrip:
    """Today trip() only fires on an explicit rate-limit response -- a plain
    outage (timeouts, connection errors, unclassified 5xx) never opened the
    breaker. record_generic_failure()/record_generic_success() close that
    gap with a short-recovery secondary trip."""

    def test_does_not_trip_below_threshold(self):
        breaker = GitHubBreaker.__new__(GitHubBreaker)
        breaker.state = GitHubBreaker.CLOSED
        breaker.redis_client = None
        breaker._generic_failure_count = 0
        breaker.trip_reason = None
        for _ in range(GitHubBreaker.GENERIC_FAILURE_THRESHOLD - 1):
            breaker.record_generic_failure()
        assert breaker.state == GitHubBreaker.CLOSED
        assert breaker.is_open() is False

    def test_trips_at_threshold_with_short_recovery(self):
        breaker = GitHubBreaker.__new__(GitHubBreaker)
        breaker.state = GitHubBreaker.CLOSED
        breaker.redis_client = None
        breaker._generic_failure_count = 0
        breaker.trip_reason = None
        breaker.opened_at = None
        breaker.reset_time = None
        for _ in range(GitHubBreaker.GENERIC_FAILURE_THRESHOLD):
            breaker.record_generic_failure()
        assert breaker.is_open() is True
        assert breaker.trip_reason == "generic_failure"
        window = (breaker.reset_time - breaker.opened_at).total_seconds()
        assert window == pytest.approx(GitHubBreaker.GENERIC_FAILURE_RECOVERY_SECONDS, abs=1)

    def test_success_resets_the_streak(self):
        breaker = GitHubBreaker.__new__(GitHubBreaker)
        breaker.state = GitHubBreaker.CLOSED
        breaker.redis_client = None
        breaker._generic_failure_count = GitHubBreaker.GENERIC_FAILURE_THRESHOLD - 1
        breaker.trip_reason = None
        breaker.record_generic_success()
        assert breaker._generic_failure_count == 0

    def test_rate_limit_trip_uses_long_recovery_independent_of_generic_counter(self):
        """The two trip paths must not interfere: a rate-limit trip keeps its
        long (~1h) window even if a generic-failure streak was mid-count."""
        breaker = GitHubBreaker.__new__(GitHubBreaker)
        breaker.state = GitHubBreaker.CLOSED
        breaker.redis_client = None
        breaker._generic_failure_count = GitHubBreaker.GENERIC_FAILURE_THRESHOLD - 1
        breaker.trip_reason = None
        breaker.opened_at = None
        breaker.reset_time = None
        breaker.trip()
        assert breaker.is_open() is True
        assert breaker.trip_reason == "rate_limit"
        window = (breaker.reset_time - breaker.opened_at).total_seconds()
        assert window > GitHubBreaker.GENERIC_FAILURE_RECOVERY_SECONDS * 10

    def test_generic_failure_does_not_accumulate_while_already_open(self):
        breaker = GitHubBreaker.__new__(GitHubBreaker)
        breaker.state = GitHubBreaker.OPEN
        breaker.redis_client = None
        breaker._generic_failure_count = 0
        breaker.trip_reason = "rate_limit"
        breaker.record_generic_failure()
        assert breaker._generic_failure_count == 0

    def test_close_resets_generic_failure_state(self):
        breaker = GitHubBreaker.__new__(GitHubBreaker)
        breaker.state = GitHubBreaker.OPEN
        breaker.redis_client = None
        breaker._generic_failure_count = 3
        breaker.trip_reason = "generic_failure"
        breaker.opened_at = None
        breaker.reset_time = None
        breaker.close()
        assert breaker.state == GitHubBreaker.CLOSED
        assert breaker.trip_reason is None
        assert breaker._generic_failure_count == 0


class TestGitHubBreakerHalfOpenRecovery:
    """Before this fix, nothing ever transitioned HALF_OPEN back to CLOSED
    (short of a manual admin reset) and both trip()/record_generic_failure()
    were guarded to fire only from CLOSED -- so a breaker that had tripped
    and recovered even once became permanently unprotected: is_open() reads
    False for HALF_OPEN, and every subsequent real failure was silently a
    no-op. These tests drive a full trip -> recover -> re-trip cycle for
    both trip paths, which is exactly the sequence that exposed the bug."""

    def test_success_while_half_open_closes_the_breaker(self):
        breaker = GitHubBreaker.__new__(GitHubBreaker)
        breaker.state = GitHubBreaker.HALF_OPEN
        breaker.redis_client = None
        breaker._generic_failure_count = 0
        breaker.trip_reason = "generic_failure"
        breaker.opened_at = datetime.now()
        breaker.reset_time = datetime.now()

        breaker.record_generic_success()

        assert breaker.state == GitHubBreaker.CLOSED
        assert breaker.trip_reason is None
        assert breaker.is_open() is False

    def test_generic_failure_while_half_open_reopens_immediately(self):
        """A single failed probe must reopen right away, not require
        re-accumulating GENERIC_FAILURE_THRESHOLD failures from zero."""
        breaker = GitHubBreaker.__new__(GitHubBreaker)
        breaker.state = GitHubBreaker.HALF_OPEN
        breaker.redis_client = None
        breaker._generic_failure_count = 0
        breaker.trip_reason = "generic_failure"
        breaker.opened_at = None
        breaker.reset_time = None

        breaker.record_generic_failure()

        assert breaker.is_open() is True
        assert breaker.trip_reason == "generic_failure"
        window = (breaker.reset_time - breaker.opened_at).total_seconds()
        assert window == pytest.approx(GitHubBreaker.GENERIC_FAILURE_RECOVERY_SECONDS, abs=1)

    def test_rate_limit_trip_while_half_open_reopens(self):
        breaker = GitHubBreaker.__new__(GitHubBreaker)
        breaker.state = GitHubBreaker.HALF_OPEN
        breaker.redis_client = None
        breaker._generic_failure_count = 0
        breaker.trip_reason = "generic_failure"
        breaker.opened_at = None
        breaker.reset_time = None

        breaker.trip()

        assert breaker.is_open() is True
        assert breaker.trip_reason == "rate_limit"

    def test_trip_and_record_generic_failure_are_noops_while_open(self):
        """Neither trip path should touch state while already OPEN -- the
        upstream is_open() gate already prevents any call from reaching
        GitHub to report a new failure/success in the first place."""
        breaker = GitHubBreaker.__new__(GitHubBreaker)
        breaker.state = GitHubBreaker.OPEN
        breaker.redis_client = None
        breaker._generic_failure_count = 0
        breaker.trip_reason = "rate_limit"
        breaker.opened_at = datetime(2020, 1, 1)
        breaker.reset_time = datetime(2020, 1, 1)

        breaker.trip()
        breaker.record_generic_failure()

        assert breaker.opened_at == datetime(2020, 1, 1)
        assert breaker.reset_time == datetime(2020, 1, 1)
        assert breaker.trip_reason == "rate_limit"

    def test_full_cycle_trip_recover_fail_again_retrips(self):
        """The end-to-end regression case: trip on generic failures, let the
        recovery window pass and transition to HALF_OPEN via
        check_and_close(), have the probe itself fail, and confirm the
        breaker is open again rather than permanently unprotected."""
        breaker = GitHubBreaker.__new__(GitHubBreaker)
        breaker.state = GitHubBreaker.CLOSED
        breaker.redis_client = None
        breaker._generic_failure_count = 0
        breaker.trip_reason = None
        breaker.opened_at = None
        breaker.reset_time = None

        for _ in range(GitHubBreaker.GENERIC_FAILURE_THRESHOLD):
            breaker.record_generic_failure()
        assert breaker.is_open() is True

        breaker.reset_time = datetime.now() - timedelta(seconds=1)
        breaker.check_and_close()
        assert breaker.state == GitHubBreaker.HALF_OPEN
        assert breaker.is_open() is False

        breaker.record_generic_failure()
        assert breaker.is_open() is True
        assert breaker.trip_reason == "generic_failure"


class TestGhCliWiresBreakerOnRealPath:
    """End-to-end through gh_cli() itself (not the breaker directly): repeated
    unclassified failures via gh_cli() should trip the breaker, and a
    subsequent call should then be rejected without invoking subprocess."""

    def test_repeated_generic_gh_cli_failures_trip_breaker(self, client):
        mock_result = MagicMock(returncode=1, stdout="", stderr="unexpected server error")
        with patch("subprocess.run", return_value=mock_result):
            for _ in range(GitHubBreaker.GENERIC_FAILURE_THRESHOLD):
                success, result = client.gh_cli(["gh", "issue", "view", "1"])
                assert success is False

        assert client.breaker.is_open() is True

        with patch("subprocess.run") as mock_run:
            success, result = client.gh_cli(["gh", "issue", "view", "1"])
        assert success is False
        assert result.error_kind == "circuit_open"
        mock_run.assert_not_called()

    def test_success_after_failures_resets_streak_without_tripping(self, client):
        failure = MagicMock(returncode=1, stdout="", stderr="unexpected server error")
        success_result = MagicMock(returncode=0, stdout="{}", stderr="")
        with patch("subprocess.run", side_effect=[failure, failure, success_result]):
            client.gh_cli(["gh", "issue", "view", "1"])
            client.gh_cli(["gh", "issue", "view", "1"])
            ok, _ = client.gh_cli(["gh", "issue", "view", "1"])
        assert ok is True
        assert client.breaker.is_open() is False
        assert client.breaker._generic_failure_count == 0


class TestGraphqlBreakerWiring:
    """graphql() got the same record_generic_success()/record_generic_failure()
    wiring as gh_cli() -- these exercise every branch that touches it."""

    def test_success_resets_generic_failure_streak(self, client):
        client.breaker._generic_failure_count = 3
        response = {"data": {"viewer": {"login": "octocat"}}}
        mock_result = MagicMock(returncode=0, stdout=json.dumps(response), stderr="")
        with patch("subprocess.run", return_value=mock_result):
            success, data = client.graphql("query { viewer { login } }")
        assert success is True
        assert client.breaker._generic_failure_count == 0

    def test_generic_exception_counts_toward_outage_trip(self, client):
        with patch("subprocess.run", side_effect=RuntimeError("boom")):
            for _ in range(GitHubBreaker.GENERIC_FAILURE_THRESHOLD):
                success, _ = client.graphql("query { viewer { login } }")
                assert success is False
        assert client.breaker.is_open() is True
        assert client.breaker.trip_reason == "generic_failure"

    def test_timeout_counts_as_generic_failure(self, client):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30)):
            client.graphql("query { viewer { login } }")
        assert client.breaker._generic_failure_count == 1

    def test_failed_after_retries_counts_once_as_generic_failure(self, client):
        mock_result = MagicMock(returncode=1, stdout="", stderr="some transient garbage")
        with patch("subprocess.run", return_value=mock_result), patch("time.sleep"):
            success, data = client.graphql("query { viewer { login } }")
        assert success is False
        assert client.breaker._generic_failure_count == 1

    def test_unparseable_body_counts_as_generic_failure(self, client):
        mock_result = MagicMock(returncode=0, stdout="not json at all {{{", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            success, data = client.graphql("query { viewer { login } }")
        assert success is False
        assert client.breaker._generic_failure_count == 1

    def test_rate_limit_does_not_touch_generic_failure_counter(self, client):
        """The rate-limit trip is a separate signal entirely -- it must not
        also increment the generic-failure streak."""
        mock_result = MagicMock(
            returncode=1, stdout="rate limit exceeded", stderr="rate limit exceeded",
        )
        with patch("subprocess.run", return_value=mock_result):
            client.graphql("query { viewer { login } }")
        assert client.breaker.trip_reason == "rate_limit"
        assert client.breaker._generic_failure_count == 0


class TestRestBreakerWiring:
    def test_success_resets_generic_failure_streak(self, client):
        client.breaker._generic_failure_count = 2
        include_stdout = (
            "HTTP/2.0 200 OK\n"
            "X-Ratelimit-Remaining: 111\n"
            "\n"
            '{"ok": true}'
        )
        mock_result = MagicMock(returncode=0, stdout=include_stdout, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            success, data = client.rest("GET", "/some/endpoint")
        assert success is True
        assert client.breaker._generic_failure_count == 0

    def test_empty_body_success_also_resets_streak(self, client):
        """A 200 with an empty body (e.g. DELETE) is still success -- must
        record it as such, not skip the wiring for the no-JSON branch."""
        client.breaker._generic_failure_count = 2
        include_stdout = "HTTP/2.0 204 No Content\n\n"
        mock_result = MagicMock(returncode=0, stdout=include_stdout, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            success, data = client.rest("DELETE", "/some/endpoint")
        assert success is True
        assert client.breaker._generic_failure_count == 0

    def test_generic_exception_counts_toward_outage_trip(self, client):
        with patch("subprocess.run", side_effect=RuntimeError("boom")):
            for _ in range(GitHubBreaker.GENERIC_FAILURE_THRESHOLD):
                client.rest("GET", "/x")
        assert client.breaker.is_open() is True
        assert client.breaker.trip_reason == "generic_failure"

    def test_timeout_counts_as_generic_failure(self, client):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30)):
            client.rest("GET", "/x")
        assert client.breaker._generic_failure_count == 1

    def test_client_error_4xx_does_not_count_as_generic_failure(self, client):
        mock_result = MagicMock(returncode=1, stdout="", stderr="HTTP 404: Not Found")
        with patch("subprocess.run", return_value=mock_result):
            client.rest("GET", "/x")
        assert client.breaker._generic_failure_count == 0

    def test_failed_after_retries_counts_once_as_generic_failure(self, client):
        mock_result = MagicMock(returncode=1, stdout="", stderr="some transient garbage")
        with patch("subprocess.run", return_value=mock_result), patch("time.sleep"):
            client.rest("GET", "/x")
        assert client.breaker._generic_failure_count == 1

    def test_unparseable_nonempty_body_counts_as_generic_failure(self, client):
        include_stdout = "HTTP/2.0 200 OK\n\nnot json {{{"
        mock_result = MagicMock(returncode=0, stdout=include_stdout, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            success, data = client.rest("GET", "/x")
        assert success is False
        assert client.breaker._generic_failure_count == 1


class TestHttpRequestBreakerWiring:
    def test_success_resets_generic_failure_streak(self, client):
        client.breaker._generic_failure_count = 2
        mock_response = MagicMock(status_code=200, headers={})
        mock_response.json.return_value = {"ok": True}
        with patch("requests.get", return_value=mock_response):
            success, data = client.http_request("GET", "https://api.github.com/user")
        assert success is True
        assert client.breaker._generic_failure_count == 0

    def test_timeout_counts_as_generic_failure(self, client):
        import requests
        with patch("requests.get", side_effect=requests.exceptions.Timeout("timed out")):
            client.http_request("GET", "https://api.github.com/user")
        assert client.breaker._generic_failure_count == 1

    def test_generic_exception_counts_as_generic_failure(self, client):
        with patch("requests.get", side_effect=RuntimeError("boom")):
            client.http_request("GET", "https://api.github.com/user")
        assert client.breaker._generic_failure_count == 1

    def test_5xx_after_retries_exhausted_counts_once_as_generic_failure(self, client):
        mock_response = MagicMock(status_code=502, text="Bad Gateway")
        with patch("requests.get", return_value=mock_response), patch("time.sleep"):
            success, data = client.http_request("GET", "https://api.github.com/user")
        assert success is False
        assert client.breaker._generic_failure_count == 1

    def test_4xx_does_not_count_as_generic_failure(self, client):
        """A 404/422/etc is a specific, expected answer -- not outage evidence."""
        mock_response = MagicMock(status_code=404, text="Not Found")
        with patch("requests.get", return_value=mock_response):
            success, data = client.http_request("GET", "https://api.github.com/user")
        assert success is False
        assert client.breaker._generic_failure_count == 0

    def test_403_rate_limit_does_not_touch_generic_failure_counter(self, client):
        mock_response = MagicMock(status_code=403, text="rate limit exceeded")
        with patch("requests.get", return_value=mock_response):
            client.http_request("GET", "https://api.github.com/user")
        assert client.breaker.trip_reason == "rate_limit"
        assert client.breaker._generic_failure_count == 0
