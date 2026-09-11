"""
GitHub App Integration for Orchestrator Bot

Handles authentication and API calls using GitHub App installation tokens.
This allows the bot to comment as "orchestrator-bot[bot]" instead of impersonating users.
"""

import os
import time
import jwt
import requests
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from services.github_api_client import is_graphql_rate_limit_error

logger = logging.getLogger(__name__)

# How long to stop issuing App GraphQL requests after GitHub says the App's
# GraphQL budget is already exhausted, when the response carries no
# x-ratelimit-reset to be precise about. Short on purpose: the cost of
# guessing too long is deferring calls that would have worked, and the
# suppression only exists to stop a burst of certain-to-fail requests, not
# to replace GitHub's own accounting.
RATE_LIMIT_HOLD_FALLBACK_SECONDS = 60

# Ceiling on a header-derived hold. GitHub's GraphQL budget resets hourly, so
# an honest x-ratelimit-reset can legitimately be ~an hour out; this only
# guards against a malformed/absurd header parking the App offline.
RATE_LIMIT_HOLD_MAX_SECONDS = 3600

# The two credentials graphql_request() can spend, held SEPARATELY because
# they are separate budgets: exhausting the App installation's 5000/hr says
# nothing about the PAT's, and a shared hold would park a working credential.
# The PAT leg is not hypothetical - graphql_request() never checks
# self.enabled, so a PAT-configured deployment (a supported mode) routes
# every discussions / human_feedback_loop / review_cycle / pr_review_stage
# query through it, and without a hold each one logs its own ERROR: the same
# 484-lines-in-three-hours burst #168 reports, just on the other credential.
CREDENTIAL_APP = 'app'
CREDENTIAL_PAT = 'pat'
_CREDENTIAL_LABELS = {
    CREDENTIAL_APP: 'GitHub App',
    CREDENTIAL_PAT: 'PAT fallback',
}


class GitHubApp:
    """GitHub App authentication and API client"""

    def __init__(self):
        """Initialize GitHub App with credentials from environment"""
        self.app_id = os.environ.get('GITHUB_APP_ID')
        self.installation_id = os.environ.get('GITHUB_APP_INSTALLATION_ID')
        self.private_key_path = os.environ.get('GITHUB_APP_PRIVATE_KEY_PATH')
        self._installation_token = None
        self._token_expires_at = None
        # Permissions GitHub reports on the token-exchange response. This is
        # the ONLY authoritative answer to "can this credential touch X" -
        # probing by listing resources cannot distinguish "none exist" from
        # "not permitted" (a Projects v2 list with no Projects permission
        # comes back empty and successful). See
        # services/github_capabilities.py (WI-5).
        self._installation_permissions = None

        # Rate-limit hold state, one entry per credential (#168). Once GitHub
        # reports a GraphQL budget exhausted, every further query on that
        # credential in that window is certain to fail, and the observed cost
        # of issuing them anyway was 484 ERROR lines in three hours - enough
        # to bury the one genuinely new error an operator is reading the log
        # for. While a hold is in force graphql_request() returns None WITHOUT
        # a network call: the same value every caller already handles for a
        # failed query, so no caller sees new behaviour, just fewer wasted
        # calls. Kept per-credential - see CREDENTIAL_APP/CREDENTIAL_PAT.
        self._graphql_holds: Dict[str, Dict[str, Any]] = {
            CREDENTIAL_APP: {'until': None, 'reset_at': None, 'suppressed': 0},
            CREDENTIAL_PAT: {'until': None, 'reset_at': None, 'suppressed': 0},
        }

        if not all([self.app_id, self.installation_id, self.private_key_path]):
            logger.warning("GitHub App credentials not fully configured - some features may be limited")
            self.enabled = False
            return

        # Load private key
        try:
            with open(self.private_key_path, 'r') as key_file:
                self.private_key = key_file.read()
            self.enabled = True
            logger.info(f"GitHub App initialized (App ID: {self.app_id}, Installation ID: {self.installation_id})")
        except Exception as e:
            logger.error(f"Failed to load GitHub App private key: {e}")
            self.enabled = False

    def _generate_jwt(self) -> str:
        """Generate JWT for GitHub App authentication"""
        now = int(time.time())
        payload = {
            # iat is backdated 60s so clock drift between this host and GitHub
            # can't fail the 'iat' claim check -- that surfaces as a 401 on the
            # token exchange ("'iat' claim timestamp check failed"), which the
            # caller can only report as "Failed to get installation token".
            # GitHub caps a JWT's lifetime at 10 minutes measured from iat, so
            # exp is +540 to keep exp-iat at exactly 600s.
            'iat': now - 60,
            'exp': now + (9 * 60),
            'iss': self.app_id
        }

        return jwt.encode(payload, self.private_key, algorithm='RS256')

    def get_installation_token(self) -> Optional[str]:
        """Get or refresh installation access token"""
        if not self.enabled:
            return None

        # Check if token is still valid
        if self._installation_token and self._token_expires_at:
            from datetime import timezone
            now = datetime.now(timezone.utc)
            if now < self._token_expires_at - timedelta(minutes=5):
                return self._installation_token

        # Generate new token
        try:
            jwt_token = self._generate_jwt()
            headers = {
                'Authorization': f'Bearer {jwt_token}',
                'Accept': 'application/vnd.github.v3+json'
            }

            url = f'https://api.github.com/app/installations/{self.installation_id}/access_tokens'
            response = requests.post(url, headers=headers)
            response.raise_for_status()

            data = response.json()
            self._installation_token = data['token']
            # Captured on every mint so it can never drift from the token in
            # hand (an admin changing the App's permissions takes effect on
            # the next exchange, not retroactively).
            self._installation_permissions = data.get('permissions') or {}
            self._token_expires_at = datetime.fromisoformat(data['expires_at'].replace('Z', '+00:00'))

            logger.info(f"Generated new GitHub App installation token (expires: {self._token_expires_at})")
            return self._installation_token

        except Exception as e:
            logger.error(f"Failed to get installation token: {e}")
            return None

    def get_installation_permissions(self) -> Optional[dict]:
        """Permissions this installation granted, e.g. {'issues': 'write', ...}.

        Returns None when the App isn't configured or a token has never been
        minted - callers must treat that as "unknown", not as "denied", since
        an unconfigured App simply means this deployment authenticates some
        other way.
        """
        if not self.enabled:
            return None
        if self._installation_permissions is None:
            # Minting refreshes the cached permissions as a side effect.
            self.get_installation_token()
        return self._installation_permissions

    def _invalidate_token(self):
        """Invalidate cached installation token so the next call generates a fresh one."""
        self._installation_token = None
        self._token_expires_at = None

    def _get_token(self, force_refresh: bool = False) -> Optional[str]:
        """Get an authentication token, preferring App installation token with PAT fallback."""
        if self.enabled:
            if force_refresh:
                self._invalidate_token()
            token = self.get_installation_token()
            if token:
                return token
            if force_refresh:
                logger.warning("App token refresh failed, falling back to PAT")

        return os.environ.get('GITHUB_TOKEN')

    def _report_call(
        self,
        rate_limited: bool = False,
        failed: bool = False,
        headers: Optional[Any] = None,
    ):
        """Report this call into GitHubAPIClient's shared accounting.

        This module bypasses GitHubAPIClient entirely (different credential,
        different quota - see GitHubAPIClient.rate_limit_app_graphql), so
        without this hook none of its traffic or failures appeared in
        /health's api_call_stats: `failed_requests: 0, rate_limited_requests: 0`
        across a window with 16 logged RATE_LIMIT errors (#168).

        Best-effort by design - accounting must never be able to fail a real
        GitHub call.
        """
        try:
            from services.github_api_client import get_github_client

            get_github_client().record_external_call(
                rate_limited=rate_limited,
                failed=failed,
                # Keys LOWERCASED, not just copied out of the
                # CaseInsensitiveDict `requests` returns: GitHub sends
                # `X-RateLimit-Remaining` and
                # GitHubRateLimitStatus.update_from_response_headers() looks
                # up the lowercase spelling. A bare dict() preserves GitHub's
                # casing, every lookup misses, and the bucket keeps its
                # 5000/5000 constructor defaults while being stamped
                # ever_updated - /health would then report a fresh, healthy
                # App budget for a fully exhausted one, which is the exact
                # misdiagnosis rate_limit_app_graphql exists to prevent
                # (#168).
                app_graphql_headers=(
                    {str(k).lower(): v for k, v in headers.items()} if headers else None
                ),
            )
        except Exception as e:
            logger.debug(f"Could not record GitHub App call in API accounting: {e}")

    def _graphql_hold_remaining(self, credential: str = CREDENTIAL_APP) -> Optional[float]:
        """Seconds left on `credential`'s GraphQL rate-limit hold, or None if
        none is in force. Clears an expired hold (and reports what it
        suppressed)."""
        hold = self._graphql_holds[credential]
        if hold['until'] is None:
            return None

        remaining = hold['until'] - time.monotonic()
        if remaining > 0:
            return remaining

        logger.info(
            f"{_CREDENTIAL_LABELS[credential]} GraphQL rate-limit hold expired "
            f"({hold['suppressed']} request(s) skipped while it was in force) - "
            f"resuming GraphQL requests on this credential"
        )
        hold['until'] = None
        hold['reset_at'] = None
        hold['suppressed'] = 0
        return None

    def get_graphql_hold_status(self) -> Dict[str, Dict[str, Any]]:
        """Read-only view of every credential's GraphQL rate-limit hold (#168).

        While a hold is in force this module answers every GraphQL query with
        None and no network call, so the hold - not either rate-limit bucket -
        is the state that actually decides whether GraphQL works right now.
        The PAT leg makes that distinction load-bearing rather than cosmetic:
        a PAT-credential hold updates NO bucket at all (graphql_request only
        attributes response headers when the call used the installation token
        - see `app_headers` there), so a percentage-based check cannot see it.
        Exposed for /health, which had no machine-readable signal for any of
        this and reported `degraded: false` for the full hold window.

        Deliberately does NOT call _graphql_hold_remaining(): that clears an
        expired hold and logs the "hold expired" summary, and a health probe
        must not consume a state transition the request path is meant to
        report. An expired-but-uncleared hold is reported as simply not
        active, with the suppression count it accumulated left intact.
        """
        now = time.monotonic()
        status: Dict[str, Dict[str, Any]] = {}
        for credential, hold in self._graphql_holds.items():
            remaining = None
            if hold['until'] is not None:
                seconds_left = hold['until'] - now
                if seconds_left > 0:
                    remaining = seconds_left
            status[credential] = {
                'active': remaining is not None,
                'remaining_seconds': round(remaining, 1) if remaining is not None else None,
                'reset_at': hold['reset_at'].isoformat() if hold['reset_at'] else None,
                'suppressed_requests': hold['suppressed'],
            }
        return status

    def _start_graphql_hold(self, headers: Optional[Any], errors: Any,
                            credential: str = CREDENTIAL_APP):
        """Begin (or extend) `credential`'s GraphQL rate-limit hold after
        GitHub reported that budget exhausted.

        Prefers the response's own x-ratelimit-reset over a guess, and logs
        ONCE per hold at WARNING instead of once per rejected query at ERROR -
        the same collapse the all-NOT_FOUND branch in graphql_request() already
        applies, and the reason the raw volume was 484 ERROR lines in three
        hours (#168).

        Applied to the PAT leg too, not just the App's: a PAT-configured
        deployment sends every one of this module's queries on the PAT, so
        leaving that leg unheld reproduces the whole of #168 in a supported
        configuration. The two holds are independent - see CREDENTIAL_APP.
        """
        hold_seconds = RATE_LIMIT_HOLD_FALLBACK_SECONDS
        reset_at = None

        reset_header = (headers or {}).get('x-ratelimit-reset') if headers else None
        if reset_header:
            try:
                from datetime import timezone
                reset_at = datetime.fromtimestamp(int(reset_header), tz=timezone.utc)
                hold_seconds = (reset_at - datetime.now(timezone.utc)).total_seconds()
                # A reset already in the past means the budget is back; still
                # hold briefly rather than not at all, since GitHub just told
                # us this very request was rejected.
                hold_seconds = max(1.0, min(hold_seconds, RATE_LIMIT_HOLD_MAX_SECONDS))
            except (ValueError, TypeError) as e:
                logger.debug(f"Could not parse x-ratelimit-reset from response: {e}")
                reset_at = None

        already_held = self._graphql_hold_remaining(credential) is not None
        hold = self._graphql_holds[credential]
        hold['until'] = time.monotonic() + hold_seconds
        hold['reset_at'] = reset_at

        if not already_held:
            if credential == CREDENTIAL_APP:
                budget_note = (
                    "NOTE: this is the App installation's own budget, which is "
                    "separate from the PAT budget `gh api rate_limit` reports. "
                )
            else:
                budget_note = (
                    "NOTE: this is the PAT budget (the App credential was "
                    "unavailable or not configured), the same one `gh api "
                    "rate_limit` reports. "
                )
            logger.warning(
                f"🔴 {_CREDENTIAL_LABELS[credential]} GraphQL rate limit exhausted - "
                f"pausing GraphQL requests on this credential for {hold_seconds:.0f}s"
                + (f" (resets at {reset_at.isoformat()})" if reset_at else " (no reset header)")
                + f". {budget_note}GitHub said: {errors}"
            )

    def graphql_request(self, query: str, variables: Dict[str, Any] = None) -> Optional[Dict]:
        """Execute a GraphQL request using GitHub App authentication (with PAT fallback)"""

        token = self._get_token()
        if not token:
            logger.error("No installation token or PAT available for GraphQL request")
            return None

        # Which credential this request is actually spending. `_get_token()`
        # falls back to a PAT when the App is disabled or its token fetch
        # failed, and the two have SEPARATE quotas, so everything downstream
        # that attributes a rate-limit reading has to know which one this was.
        #
        # `token and` is load-bearing: _get_token(force_refresh=True) below
        # invalidates the cached installation token FIRST, so a failed refresh
        # with no PAT configured leaves both sides None and a bare `==` reads
        # None == None as "this was the App credential" - attributing an
        # unauthenticated 60/hr reading to the App bucket at exactly the moment
        # App auth is broken (#168's misdiagnosis, relocated).
        used_app_token = bool(self.enabled and token and token == self._installation_token)
        credential = CREDENTIAL_APP if used_app_token else CREDENTIAL_PAT

        # Checked AFTER resolving the credential: a hold belongs to one budget,
        # and holding a PAT-fallback query because the App is exhausted (or the
        # reverse) would suppress calls that would have succeeded.
        hold_remaining = self._graphql_hold_remaining(credential)
        if hold_remaining is not None:
            hold = self._graphql_holds[credential]
            hold['suppressed'] += 1
            logger.debug(
                f"Skipping {_CREDENTIAL_LABELS[credential]} GraphQL request: "
                f"rate-limit hold has {hold_remaining:.0f}s left "
                f"({hold['suppressed']} skipped so far)"
            )
            return None

        payload = {'query': query}
        if variables:
            payload['variables'] = variables

        # Set before the try so the exception handlers below can report a
        # transport failure (which produced no response, hence no headers).
        app_headers = None

        try:
            response = requests.post(
                'https://api.github.com/graphql',
                headers={
                    'Authorization': f'Bearer {token}',
                    'Accept': 'application/vnd.github.v3+json'
                },
                json=payload
            )

            # On 401, refresh the token and retry once.
            # GitHub rejects 401 before processing, so retrying mutations is safe.
            if response.status_code == 401 and self.enabled:
                logger.warning("GraphQL request got 401, refreshing token and retrying")
                token = self._get_token(force_refresh=True)
                # See the `token and` note above: force_refresh clears
                # _installation_token before fetching, so a failed refresh
                # with no PAT leaves both None and a bare `==` would claim
                # this unauthenticated request was on the App credential.
                used_app_token = bool(
                    self.enabled and token and token == self._installation_token
                )
                credential = CREDENTIAL_APP if used_app_token else CREDENTIAL_PAT
                if token:
                    response = requests.post(
                        'https://api.github.com/graphql',
                        headers={
                            'Authorization': f'Bearer {token}',
                            'Accept': 'application/vnd.github.v3+json'
                        },
                        json=payload
                    )

                    # If retry also fails with 401, fall back to PAT
                    if response.status_code == 401:
                        pat = os.environ.get('GITHUB_TOKEN')
                        if pat and pat != token:
                            logger.warning("GraphQL retry still 401, falling back to PAT")
                            used_app_token = False
                            credential = CREDENTIAL_PAT
                            response = requests.post(
                                'https://api.github.com/graphql',
                                headers={
                                    'Authorization': f'Bearer {pat}',
                                    'Accept': 'application/vnd.github.v3+json'
                                },
                                json=payload
                            )
                        elif not pat:
                            logger.error("GraphQL retry still 401 and no GITHUB_TOKEN configured for PAT fallback")
                else:
                    logger.error("Cannot retry GraphQL request, no token available after refresh")

            # Whichever credential the request ended up on decides which
            # rate-limit bucket these headers describe. Only an installation
            # token's headers belong to the App bucket; a PAT fallback's
            # headers describe the same budget `gh` spends and would silently
            # merge the two quotas this bucket exists to keep apart (#168).
            app_headers = response.headers if used_app_token else None

            response.raise_for_status()

            data = response.json()
            if 'errors' in data:
                # Check if all errors are NOT_FOUND (common for deleted resources)
                errors = data['errors']
                all_not_found = all(err.get('type') == 'NOT_FOUND' for err in errors)

                if all_not_found:
                    self._report_call(failed=True, headers=app_headers)
                    logger.debug(f"GraphQL NOT_FOUND errors: {errors}")
                    return None

                # GitHub answers a primary GraphQL rate limit with HTTP 200 and
                # a RATE_LIMIT error in the body, so raise_for_status() above
                # never sees it and nothing counted it (#168). Classify it,
                # count it, and stop issuing queries that cannot succeed until
                # the budget resets, rather than logging one ERROR per attempt.
                #
                # Held per credential, including the PAT leg: `graphql_request`
                # never checks self.enabled, so a PAT-configured deployment
                # sends ALL of this module's queries on the PAT, and logging
                # one ERROR per rejected query there is the same burst #168
                # measured, just on the other budget.
                if is_graphql_rate_limit_error(errors):
                    self._report_call(rate_limited=True, headers=app_headers)
                    self._start_graphql_hold(response.headers, errors, credential)
                    return None

                self._report_call(failed=True, headers=app_headers)
                logger.error(f"GraphQL errors: {errors}")
                return None

            self._report_call(headers=app_headers)
            return data.get('data')

        except requests.exceptions.HTTPError as e:
            self._report_call(failed=True, headers=app_headers)
            logger.error(f"GraphQL request failed: {e}")
            return None
        except Exception as e:
            self._report_call(failed=True, headers=app_headers)
            logger.error(f"GraphQL request failed (unexpected): {e}", exc_info=True)
            return None

    def _rest_call(self, method: str, url: str, headers: Dict, data: Dict = None) -> requests.Response:
        """Execute a single REST API call."""
        method = method.upper()
        if method == 'GET':
            return requests.get(url, headers=headers)
        elif method == 'POST':
            return requests.post(url, headers=headers, json=data)
        elif method == 'PATCH':
            return requests.patch(url, headers=headers, json=data)
        elif method == 'DELETE':
            return requests.delete(url, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

    def rest_request(self, method: str, path: str, data: Dict = None) -> Optional[Dict]:
        """Execute a REST API request using GitHub App authentication (with PAT fallback)"""

        token = self._get_token()
        if not token:
            logger.error("No installation token or PAT available for REST request")
            return None

        url = f'https://api.github.com{path}'
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github.v3+json'
        }

        try:
            response = self._rest_call(method, url, headers, data)

            # On 401, refresh the token and retry once.
            # GitHub rejects 401 before processing, so retrying mutations is safe.
            if response.status_code == 401 and self.enabled:
                logger.warning(f"REST request got 401 for {method} {path}, refreshing token and retrying")
                token = self._get_token(force_refresh=True)
                if token:
                    headers['Authorization'] = f'Bearer {token}'
                    response = self._rest_call(method, url, headers, data)

                    # If retry also fails with 401, fall back to PAT
                    if response.status_code == 401:
                        pat = os.environ.get('GITHUB_TOKEN')
                        if pat and pat != token:
                            logger.warning(f"REST retry still 401 for {method} {path}, falling back to PAT")
                            headers['Authorization'] = f'Bearer {pat}'
                            response = self._rest_call(method, url, headers, data)
                        elif not pat:
                            logger.error(f"REST retry still 401 for {method} {path} and no GITHUB_TOKEN configured for PAT fallback")
                else:
                    logger.error(f"Cannot retry REST request for {method} {path}, no token available after refresh")

            response.raise_for_status()
            self._report_call()
            return response.json() if response.text else {}

        except requests.exceptions.HTTPError as e:
            # Counted for the same reason the GraphQL path is (#168): this
            # module's traffic never reached the shared accounting, so
            # /health could not see its failures at all. No App REST bucket
            # is populated here - GraphQL is the budget #168 observed being
            # exhausted, and a bucket nothing reads is state without a reader.
            rest_response = getattr(e, 'response', None)
            rate_limited = bool(
                rest_response is not None
                and rest_response.status_code in (403, 429)
                and 'rate limit' in (rest_response.text or '').lower()
            )
            self._report_call(rate_limited=rate_limited, failed=not rate_limited)
            logger.error(f"REST request failed: {e}")
            return None
        except Exception as e:
            self._report_call(failed=True)
            logger.error(f"REST request failed (unexpected): {e}", exc_info=True)
            return None


# Global singleton instance
github_app = GitHubApp()
