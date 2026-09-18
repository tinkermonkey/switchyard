"""
Circuit breaker for the GitHub App auth/API path.

services/github_app.py and services/github_app_auth.py independently mint
installation tokens for the same GitHub App and make raw `requests.*` calls
that bypass services/github_api_client.py's GitHubBreaker entirely -- a JWT
exchange for an installation token is a fundamentally different credential
flow than the PAT-env-var routing GitHubBreaker/`gh_cli()` protect, so it
can't just route through that client. Confirmed audit finding: neither module
had ANY breaker protection before this -- a sustained App-auth outage (GitHub
down, a revoked/expired private key, a misconfigured installation) would keep
firing token-mint requests indefinitely.

One shared instance, not one per module: both spend the SAME App
installation's quota, so a sustained failure minting/using that token from
either module should protect the other too.

Deliberately its own instance, not github_api_client.py's GitHubBreaker --
that one is REST/GraphQL/CLI rate-limit-specific and Redis-keyed for a
different slot. Mirrors the pattern services/github_owner_utils.py already
uses for its own independent breaker (`_check_circuit_breaker()` /
`_github_circuit_breaker`), including the manual check/record style: this
breaker's own `.call()` wrapper is async, and every call site here is sync.
"""
import requests
from datetime import datetime

from services.circuit_breaker import CircuitBreaker, CircuitBreakerOpen, CircuitState

github_app_breaker = CircuitBreaker(
    name="github_app_auth",
    failure_threshold=5,
    recovery_timeout=60,
    expected_exception=requests.exceptions.RequestException,
)


def check_github_app_breaker() -> None:
    """Raise CircuitBreakerOpen if the shared breaker is open.

    Flips OPEN -> HALF_OPEN once the recovery window has elapsed (same
    transition github_owner_utils._check_circuit_breaker() performs), letting
    the next call through as a probe.
    """
    if github_app_breaker.state != CircuitState.OPEN:
        return

    if github_app_breaker.last_failure_time:
        elapsed = (datetime.now() - github_app_breaker.last_failure_time).total_seconds()
        if elapsed >= github_app_breaker.recovery_timeout:
            github_app_breaker._transition_to_half_open()
            return

    wait_time = github_app_breaker._time_until_retry()
    raise CircuitBreakerOpen(
        f"GitHub App auth circuit breaker is open. Retry in {wait_time:.0f}s"
    )


def record_github_app_success() -> None:
    github_app_breaker._on_success()


def record_github_app_failure() -> None:
    github_app_breaker._on_failure()
