"""
Centralized GitHub API client with rate limiting, usage tracking, and circuit breaker.

This module provides a single point of control for all GitHub API interactions:
- Rate limit awareness and backoff
- Usage tracking and alarms
- Circuit breaker integration
- Exponential backoff on rate limiting
- Request queuing and throttling
"""

import logging
import subprocess
import json
import time
import re
import os
import traceback
import inspect
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple, List
from collections import deque
from threading import Lock, Thread

logger = logging.getLogger(__name__)

# Enable call stack tracing (set TRACE_GITHUB_API_CALLS=true to see where calls come from)
#TRACE_API_CALLS = os.environ.get('TRACE_GITHUB_API_CALLS', 'false').lower() == 'true'
TRACE_API_CALLS = True

# Redis keys each GitHubAPIClient mirrors its real-response-derived rate
# limit readings to, and that get_shared_rate_limit_status() reads back -
# the cross-process view of "how much quota is left right now", regardless
# of which process (orchestrator, a repair-cycle container, an agent
# container) actually made the call that produced the reading.
RATE_LIMIT_REDIS_KEYS = {
    'rest': 'github:rate_limit:rest',
    'graphql': 'github:rate_limit:graphql',
}

# A shared reading older than this is shown as stale rather than trusted.
# No longer tied to any polling cadence (the periodic /rate_limit poll this
# threshold used to be sized around was removed - issue #103 follow-up);
# this is now purely "how old a real-response-derived reading can be before
# a quiet period should start looking suspicious rather than just quiet".
RATE_LIMIT_STALE_THRESHOLD_SECONDS = 900

# No TTL on the two rate-limit mirror keys (deliberately, unlike most other
# Redis usage in this codebase): a bucket that's genuinely gone unused for
# a long time should keep reporting 'stale' with its true last-seen age,
# not silently flip to 'never_observed' - which claims no reading has ever
# been published, when one has, it's just old. An earlier version of this
# used a 2h TTL "for hygiene", but the REST bucket in particular can easily
# go longer than that between real calls (it's only used on pipeline-driven
# paths, not polled), which reintroduced exactly the "quiet is
# indistinguishable from broken" ambiguity this whole mechanism exists to
# avoid (see get_shared_rate_limit_status()'s docstring). Two small fixed
# keys costs nothing to keep around indefinitely.

_shared_redis_client = None
_shared_redis_client_lock = Lock()


def _get_shared_redis_client():
    """Lazily-initialized, reused Redis connection for the rate-limit
    mirror keys - shared across _mirror_rate_limit_to_redis() and
    get_shared_rate_limit_status() so every call doesn't open a fresh
    connection/pool. Honors REDIS_HOST like the rest of this codebase
    (services/pipeline_lock_manager.py, monitoring/observability.py, etc.)
    instead of a value hardcoded to the Docker Compose service name -
    matters for tests (tests/conftest.py's purge fixture reads REDIS_URL;
    a hardcoded host here would silently clean a different server than
    whatever host was actually written to) and for any environment where
    the Redis hostname isn't literally 'redis'.
    """
    global _shared_redis_client
    if _shared_redis_client is None:
        with _shared_redis_client_lock:
            if _shared_redis_client is None:
                import redis
                _shared_redis_client = redis.Redis(
                    host=os.environ.get('REDIS_HOST', 'redis'),
                    port=int(os.environ.get('REDIS_PORT', 6379)),
                    decode_responses=True,
                    socket_timeout=2, socket_connect_timeout=2,
                )
    return _shared_redis_client


def _utc_isoformat(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime for the cross-process/cross-browser rate-limit
    mirror as a UTC-aware ISO string (with a '+00:00' offset). `dt` may be
    naive (as datetime.now()/datetime.fromtimestamp() produce elsewhere in
    this file) - naive values are presumed to represent local system time,
    per Python's own datetime.astimezone() semantics, and converted to UTC
    rather than serialized as if they already were UTC.

    Why this matters: without an explicit offset, a naive ISO string is
    parsed by JavaScript's Date constructor as browser-local time, not the
    server's time zone - so a reading published by a UTC container would
    render as hours off (or even negative, clamped to "just now") for any
    viewer not in UTC. Explicit '+00:00' parses correctly everywhere.
    """
    if dt is None:
        return None
    # astimezone() correctly handles both cases in one call: an aware `dt`
    # is converted to UTC as normal, and a naive `dt` is first presumed to
    # represent local system time (per Python's documented behavior) before
    # converting - so this doesn't need to special-case naive vs. aware.
    return dt.astimezone(timezone.utc).isoformat()


def _parse_utc_isoformat(iso_string: Optional[str]) -> Optional[datetime]:
    """Inverse of _utc_isoformat(), tolerant of naive strings written by an
    older version of this code (before UTC-aware serialization) - treated
    as already UTC rather than raising or silently comparing incorrectly
    against an aware datetime.now(timezone.utc) elsewhere.
    """
    if not iso_string:
        return None
    dt = datetime.fromisoformat(iso_string)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class GitHubRateLimitStatus:
    """Track GitHub API rate limit status and remaining quota."""
    
    def __init__(self):
        self.limit = 5000  # Points per hour for authenticated user
        self.remaining = 5000
        self.reset_time: Optional[datetime] = None
        self.last_updated = datetime.now()
        self.resource_type = "graphql"  # or "rest"
        # False until a real GitHub response updates this bucket. Needed
        # because last_updated alone can't distinguish "genuinely healthy,
        # 0% used" from "never actually populated, still at the 5000/5000
        # defaults above" - both look identical in remaining/limit, and
        # last_updated gets stamped to "now" right here in __init__ before
        # any real data has arrived. See is_stale().
        self.ever_updated = False
        
    def update_from_response_headers(self, headers: Dict[str, str]):
        """Update rate limit info from GitHub API response headers."""
        try:
            if 'x-ratelimit-limit' in headers:
                self.limit = int(headers['x-ratelimit-limit'])
            if 'x-ratelimit-remaining' in headers:
                self.remaining = int(headers['x-ratelimit-remaining'])
            if 'x-ratelimit-reset' in headers:
                reset_timestamp = int(headers['x-ratelimit-reset'])
                self.reset_time = datetime.fromtimestamp(reset_timestamp)
            if 'x-ratelimit-resource' in headers:
                self.resource_type = headers['x-ratelimit-resource']
            
            self.last_updated = datetime.now()
            self.ever_updated = True
        except Exception as e:
            logger.error(f"Error parsing rate limit headers: {e}")
    
    def get_percentage_used(self) -> float:
        """Get percentage of rate limit used (0-100)."""
        if self.limit == 0:
            return 0
        return ((self.limit - self.remaining) / self.limit) * 100
    
    def is_stale(self, threshold_seconds: float = 900) -> bool:
        """True if this bucket has never been refreshed from a real GitHub
        response, or its last refresh is older than `threshold_seconds`
        (default 15 minutes). Not tied to any polling cadence - readings
        only arrive from real GitHub traffic now, not a periodic poll - this
        is purely "how old can a real reading be before a quiet period
        should start looking suspicious rather than just quiet". Lets a
        health payload tell a genuinely healthy "0% used" apart from "never
        actually populated" (issue #103) - both look identical in
        remaining/limit alone.
        """
        if not self.ever_updated:
            return True
        return (datetime.now() - self.last_updated).total_seconds() > threshold_seconds

    def get_time_until_reset(self) -> Optional[float]:
        """Get seconds until rate limit resets."""
        if not self.reset_time:
            return None
        
        # Handle both naive and aware datetimes
        if self.reset_time.tzinfo is not None:
            # Aware datetime - use timezone-aware now
            from datetime import timezone
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            reset = self.reset_time.replace(tzinfo=None)
        else:
            # Naive datetime
            now = datetime.now()
            reset = self.reset_time
        
        if now >= reset:
            return 0
        return (reset - now).total_seconds()
    
    def to_dict(self) -> dict:
        """Export status as dictionary."""
        return {
            'limit': self.limit,
            'remaining': self.remaining,
            'used': self.limit - self.remaining,
            'percentage_used': self.get_percentage_used(),
            'reset_time': self.reset_time.isoformat() if self.reset_time else None,
            'time_until_reset': self.get_time_until_reset(),
            'resource_type': self.resource_type,
            'last_updated': self.last_updated.isoformat(),
            'ever_updated': self.ever_updated,
            'stale': self.is_stale(),
        }


class GitHubBreaker:
    """
    Circuit breaker for GitHub API rate limits.
    
    States:
    - CLOSED: Normal operation
    - OPEN: Rate limit hit, reject requests
    - HALF_OPEN: Testing if rate limit reset
    """
    
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"
    
    def __init__(self):
        self.state = self.CLOSED
        self.opened_at: Optional[datetime] = None
        self.reset_time: Optional[datetime] = None
        self.redis_client = None
        self.redis_key = "orchestrator:github_api_breaker:state"
        
        try:
            import redis
            self.redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
            self.redis_client.ping()
            logger.info("GitHub API breaker connected to Redis")
        except Exception as e:
            logger.warning(f"Could not connect to Redis for GitHub breaker: {e}")
    
    def trip(self, reset_time: Optional[datetime] = None):
        """Open the breaker due to rate limit."""
        if self.state == self.CLOSED:
            self.state = self.OPEN
            self.opened_at = datetime.now()
            self.reset_time = reset_time or (datetime.now() + timedelta(hours=1))
            self._save_to_redis()
            logger.error(
                f"🔴 GITHUB API CIRCUIT BREAKER OPENED - Rate limit exceeded. "
                f"Will reset at {self.reset_time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
    
    def check_and_close(self) -> bool:
        """Check if rate limit reset and close breaker if so."""
        if self.state == self.CLOSED:
            return True
        
        if self.reset_time and datetime.now() >= self.reset_time:
            self.state = self.HALF_OPEN
            self._save_to_redis()
            logger.warning("🟡 GITHUB API BREAKER HALF-OPEN - Testing rate limit recovery...")
            return False
        
        return False
    
    def close(self):
        """Close the breaker (rate limit recovered)."""
        if self.state != self.CLOSED:
            self.state = self.CLOSED
            self.opened_at = None
            self.reset_time = None
            if self.redis_client:
                try:
                    self.redis_client.delete(self.redis_key)
                except Exception as e:
                    logger.error(f"Error deleting breaker state from Redis: {e}")
            logger.info("🟢 GITHUB API BREAKER CLOSED - Rate limit recovered")
    
    def is_open(self) -> bool:
        """Check if breaker is open."""
        return self.state == self.OPEN
    
    def is_half_open(self) -> bool:
        """Check if breaker is half-open."""
        return self.state == self.HALF_OPEN
    
    def _save_to_redis(self):
        """Persist breaker state to Redis."""
        if not self.redis_client:
            return
        try:
            state_dict = {
                'state': self.state,
                'opened_at': self.opened_at.isoformat() if self.opened_at else None,
                'reset_time': self.reset_time.isoformat() if self.reset_time else None,
            }
            self.redis_client.set(self.redis_key, json.dumps(state_dict), ex=86400)
        except Exception as e:
            logger.error(f"Error saving GitHub breaker state to Redis: {e}")


class GitHubAPIClient:
    """
    Centralized GitHub API client with rate limiting and circuit breaker.
    
    All GitHub API calls should go through this client to ensure:
    - Rate limit awareness and backoff
    - Usage tracking and alarming
    - Circuit breaker protection
    - Request queuing and throttling
    """
    
    def __init__(self):
        # Two independent buckets - GitHub enforces separate REST and
        # GraphQL rate limits (5000/hr each), but this client used to keep
        # a single `rate_limit` field whose `resource_type` got silently
        # overwritten by whichever call type most recently reported
        # response headers, making "X remaining" impossible to attribute
        # to either bucket (issue #103). `rate_limit` is kept as an alias
        # to the GraphQL bucket (same object, not a copy) for backward
        # compatibility with existing callers/tests that read
        # `client.rate_limit` directly.
        self.rate_limit_graphql = GitHubRateLimitStatus()
        self.rate_limit_graphql.resource_type = "graphql"
        self.rate_limit_rest = GitHubRateLimitStatus()
        self.rate_limit_rest.resource_type = "rest"
        self.rate_limit = self.rate_limit_graphql  # backward-compat alias
        self.breaker = GitHubBreaker()
        self.lock = Lock()
        
        # Request queue for throttling
        self.request_queue = deque()
        self.last_request_time = 0
        self.min_request_interval = 0.1  # Start with 100ms between requests
        
        # Usage statistics
        self.total_requests = 0
        self.failed_requests = 0
        self.rate_limited_requests = 0
        self.request_history = deque(maxlen=100)  # Track last 100 requests
        
        # Backoff state
        self.backoff_multiplier = 1.0
        self.max_backoff = 60  # Max 60 second backoff
        
        # Call trace tracking
        self.call_trace_buffer = []  # List of (timestamp, operation_type, caller_info) tuples
        self.call_trace_lock = Lock()

        # Tracks consecutive _mirror_rate_limit_to_redis() failures so a
        # sustained Redis outage gets one visible warning (and one
        # recovery notice) instead of being buried at debug level on every
        # single call - see _mirror_rate_limit_to_redis().
        self._redis_mirror_consecutive_failures = 0

        # Debounces alarm_if_needed() so it can safely be called on every
        # real rate-limit reading (see _update_rate_limit_from_http_headers
        # etc.) without spamming the same low-quota warning on every call
        # during a sustained low-quota period.
        self._last_alarm_check_at = None

        logger.info("GitHub API client initialized with rate limiting")

        # Periodically summarize/trim the call trace buffer. (This used to
        # also re-poll GitHub's dedicated /rate_limit endpoint every 5
        # minutes to populate rate_limit_rest/rate_limit_graphql, but that
        # endpoint was found to return bogus always-full data for at least
        # one token/org - see issue #103 follow-up. Rate limit state is now
        # sourced exclusively from real per-call response headers via
        # _update_rate_limit_from_headers()/_update_rate_limit_from_
        # graphql_response(), which also mirror each update to Redis for
        # other processes to read via get_shared_rate_limit_status() and
        # call the debounced alarm_if_needed() - so low-quota alerting is
        # now driven by real traffic instead of a fixed 5-minute poll, and
        # doesn't stop working just because this thread's periodic poll
        # was removed.)
        self._start_call_trace_summarizer()
    
    def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None, retries: int = 0) -> Tuple[bool, Any]:
        """
        Execute a GraphQL query with rate limiting and error handling.
        
        Args:
            query: GraphQL query string
            variables: Query variables (optional)
            retries: Current retry count (internal use)
            
        Returns:
            Tuple of (success, response_data)
        """
        # Check if breaker has recovered and can attempt again
        self.breaker.check_and_close()
        
        # Check if breaker is open
        if self.breaker.is_open():
            time_until_reset = self.breaker.reset_time - datetime.now() if self.breaker.reset_time else None
            wait_msg = f" (will retry in {time_until_reset.total_seconds():.0f}s)" if time_until_reset else ""
            logger.error(f"🔴 GitHub API breaker is OPEN - rejecting request{wait_msg}")
            return False, {"error": "GitHub API rate limit exceeded - circuit breaker open"}
        
        # Check if we should do adaptive throttling
        usage_percent = self.rate_limit_graphql.get_percentage_used()
        if usage_percent > 95:
            wait_time = 30  # Heavy backoff at 95%+ usage
            logger.warning(f"⚠️  GitHub API usage at {usage_percent:.1f}% - throttling requests (waiting {wait_time}s)")
            time.sleep(wait_time)
        elif usage_percent > 90:
            wait_time = 10
            logger.warning(f"⚠️  GitHub API usage at {usage_percent:.1f}% - backing off (waiting {wait_time}s)")
            time.sleep(wait_time)
        elif usage_percent > 80:
            logger.warning(f"⚠️  GitHub API usage at {usage_percent:.1f}% - approaching limit")
        
        # Apply rate limiting backoff
        self._apply_backoff()
        
        # Build command for GraphQL
        # When variables are present, use stdin to pass the full JSON payload
        # This avoids issues with -F flag not properly handling complex GraphQL variables
        # --include prefixes stdout with the HTTP status line and response
        # headers, same as rest()'s use of it - GitHub reports real,
        # per-call GraphQL-bucket usage there (x-ratelimit-remaining etc.)
        # regardless of whether the query itself asks for cost/rateLimit
        # data in its body. Without this, _update_rate_limit_from_graphql_
        # response()'s extensions.cost.rateLimit/data.rateLimit fallback
        # only fires for queries that explicitly request it, which most of
        # this client's real traffic doesn't - leaving the GraphQL bucket
        # populated only by the rare query that does.
        cmd = ['gh', 'api', 'graphql', '--include']
        input_data = None

        if variables:
            # Build JSON payload with query and variables
            payload = {
                "query": query,
                "variables": variables
            }
            input_data = json.dumps(payload)
            cmd.extend(['--input', '-'])
        else:
            # For simple queries without variables, use -f flag (lowercase for string parameters)
            cmd.extend(['-f', f'query={query}'])

        try:
            logger.debug(f"Executing GraphQL query (usage: {usage_percent:.1f}%)")
            result = subprocess.run(
                cmd,
                input=input_data,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            self.total_requests += 1
            self._record_request('graphql', True)

            # Recover the GraphQL-bucket rate limit info from the headers
            # --include prepends to stdout (see cmd construction above),
            # then parse the JSON body out of what's left. Done BEFORE the
            # returncode check below: `gh api graphql` exits 1 for every
            # GraphQL error response, including the 403 rate-limit response
            # whose x-ratelimit-remaining: 0 header is the single most
            # valuable reading available - parsing after an early return
            # would silently throw it away. This also fixes a real bug the
            # ordering used to cause: _extract_reset_time() below used to
            # receive raw `result.stdout`, which now starts with an HTTP
            # status line + headers because of --include, so its
            # json.loads() always raised and reset_time was always None -
            # trip() then always fell back to a full 1-hour backoff instead
            # of GitHub's actual (often much shorter) reset time.
            headers, body = self._parse_gh_api_include_output(result.stdout)
            if headers:
                self._update_rate_limit_from_graphql_headers(headers)

            # Check for rate limit error
            if result.returncode == 1:
                if 'rate limit' in body.lower() or 'rate limit' in result.stderr.lower():
                    self.rate_limited_requests += 1
                    logger.error("🔴 GitHub API rate limit hit")

                    # Extract reset time if possible - the parsed body (not
                    # raw stdout, which --include has prefixed with
                    # headers) and the headers dict itself, which carries
                    # x-ratelimit-reset directly and is preferred over
                    # regex-parsing the error message.
                    reset_time = self._extract_reset_time(body, result.stderr, headers=headers)
                    self.breaker.trip(reset_time)

                    return False, {"error": "rate_limited", "details": body}

                self.failed_requests += 1
                logger.error(f"GraphQL query failed: {result.stderr}")

                # Exponential backoff on transient errors
                if retries < 3:
                    wait_time = (2 ** retries) * 2  # 2s, 4s, 8s
                    logger.info(f"Retrying after {wait_time}s (attempt {retries + 1}/3)")
                    time.sleep(wait_time)
                    return self.graphql(query, variables, retries + 1)

                return False, {"error": "failed_after_retries", "stderr": result.stderr}

            # Parse response
            try:
                response = json.loads(body)

                # Fallback only: some queries also carry rate limit info in
                # the response body itself (extensions.cost.rateLimit /
                # data.rateLimit). Only consulted when the header-based
                # update above didn't run (headers missing/unparseable) -
                # both describe the same real call, so re-mirroring the
                # same reading to Redis a second time on every request
                # would just be a redundant synchronous round-trip.
                if not headers:
                    self._update_rate_limit_from_graphql_response(response)

                # Check for GraphQL errors
                if 'errors' in response:
                    logger.error(f"GraphQL errors: {response['errors']}")
                    return False, response

                # Reset backoff on success
                self.backoff_multiplier = 1.0

                # Track the operation
                self.track_gh_operation('graphql', 'GraphQL query executed successfully')

                return True, response.get('data', response)

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse GraphQL response: {e}")
                return False, {"error": "parse_error", "raw_output": body}
        
        except subprocess.TimeoutExpired:
            logger.error("GraphQL query timed out")
            self.failed_requests += 1
            return False, {"error": "timeout"}
        
        except Exception as e:
            logger.error(f"GraphQL query failed: {e}", exc_info=True)
            self.failed_requests += 1
            return False, {"error": str(e)}
    
    def rest(self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None, retries: int = 0) -> Tuple[bool, Any]:
        """
        Execute a REST API call with rate limiting and error handling.
        
        Args:
            method: HTTP method ('GET', 'POST', 'PATCH', 'PUT', 'DELETE')
            endpoint: GitHub REST endpoint (e.g., '/repos/owner/repo/issues/1')
            data: Optional request body for POST/PATCH
            retries: Current retry count (internal use)
            
        Returns:
            Tuple of (success, response_data)
        """
        # Check if breaker has recovered and can attempt again
        self.breaker.check_and_close()
        
        # Check if breaker is open
        if self.breaker.is_open():
            time_until_reset = self.breaker.reset_time - datetime.now() if self.breaker.reset_time else None
            wait_msg = f" (will retry in {time_until_reset.total_seconds():.0f}s)" if time_until_reset else ""
            logger.error(f"🔴 GitHub API breaker is OPEN - rejecting REST request{wait_msg}")
            return False, {"error": "GitHub API rate limit exceeded - circuit breaker open"}
        
        # Check usage and apply throttling (REST bucket - GraphQL and REST
        # are separate GitHub rate-limit buckets)
        usage_percent = self.rate_limit_rest.get_percentage_used()
        if usage_percent > 95:
            wait_time = 30
            logger.warning(f"⚠️  GitHub API usage at {usage_percent:.1f}% - throttling (waiting {wait_time}s)")
            time.sleep(wait_time)
        elif usage_percent > 90:
            wait_time = 10
            logger.warning(f"⚠️  GitHub API usage at {usage_percent:.1f}% - backing off (waiting {wait_time}s)")
            time.sleep(wait_time)
        
        # Apply backoff
        self._apply_backoff()
        
        # For REST API calls with data (POST/PATCH/PUT/DELETE), use http_request instead
        # as gh api doesn't handle JSON bodies well
        if data and method.upper() in ['POST', 'PATCH', 'PUT', 'DELETE']:
            # Use HTTP request instead
            url = f"https://api.github.com{endpoint}"
            return self.http_request(method, url, data, retries=retries)
        
        # Build gh CLI command for GET and other methods without body
        # gh api syntax: gh api [--include] [-X METHOD] ENDPOINT
        # --include prefixes stdout with the HTTP status line and response
        # headers (see _parse_gh_api_include_output) - the only way to
        # recover the REST bucket's x-ratelimit-* headers when going
        # through the `gh` CLI, which otherwise exposes no headers at all
        # (unlike http_request()'s direct `requests` calls below).
        # Only include -X if method is not GET (GET is the default)
        cmd = ['gh', 'api', '--include']
        if method.upper() != 'GET':
            cmd.extend(['-X', method.upper()])
        cmd.append(endpoint)
        
        try:
            logger.debug(f"Executing REST {method} {endpoint} (usage: {usage_percent:.1f}%)")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            self.total_requests += 1
            self._record_request('rest', True)
            
            # Recover REST-bucket rate limit info from the headers `gh api
            # --include` prepends to stdout - `gh api` otherwise gives no
            # access to response headers, which is why the REST bucket
            # went untracked here before (issue #103).
            headers, body = self._parse_gh_api_include_output(result.stdout)
            if headers:
                self._update_rate_limit_from_http_headers(headers)
            
            # Check for errors
            if result.returncode != 0:
                if 'rate limit' in result.stderr.lower() or 'rate limit' in result.stdout.lower():
                    self.rate_limited_requests += 1
                    logger.error("🔴 GitHub API rate limit hit (REST)")
                    self.breaker.trip()
                    return False, {"error": "rate_limited", "details": result.stderr}

                self.failed_requests += 1

                # Check for HTTP 410 (Gone/Deleted) - permanent error, don't retry
                if 'HTTP 410' in result.stderr or 'was deleted' in result.stderr:
                    logger.error(f"REST request failed with HTTP 410 (resource deleted): {method} {endpoint}")
                    logger.error(f"Error details: {result.stderr}")
                    return False, {"error": "resource_deleted", "http_code": 410, "stderr": result.stderr}

                # Check for other 4xx errors that shouldn't be retried
                if any(code in result.stderr for code in ['HTTP 404', 'HTTP 403', 'HTTP 401', 'HTTP 422']):
                    logger.error(f"REST request failed with client error: {method} {endpoint}")
                    logger.error(f"Error details: {result.stderr}")
                    return False, {"error": "client_error", "stderr": result.stderr}

                logger.error(f"REST request failed: {method} {endpoint}")
                logger.error(f"Error details: {result.stderr}")

                # Retry transient errors (5xx, network issues, etc.)
                if retries < 3:
                    wait_time = (2 ** retries) * 2
                    logger.info(f"Retrying after {wait_time}s (attempt {retries + 1}/3)")
                    time.sleep(wait_time)
                    return self.rest(method, endpoint, data, retries + 1)

                return False, {"error": "failed_after_retries", "stderr": result.stderr}
            
            # Success - parse response
            try:
                response = json.loads(body)
                self.backoff_multiplier = 1.0
                
                # Track the operation
                self.track_gh_operation('rest_api', f'REST {method} {endpoint} executed successfully')
                
                return True, response
            except json.JSONDecodeError:
                # Some endpoints return empty responses
                if body.strip() == '':
                    # Track empty response success
                    self.track_gh_operation('rest_api', f'REST {method} {endpoint} executed successfully (empty response)')
                    return True, {}
                logger.error(f"Failed to parse REST response: {body}")
                return False, {"error": "parse_error"}
        
        except subprocess.TimeoutExpired:
            logger.error("REST request timed out")
            self.failed_requests += 1
            return False, {"error": "timeout"}
        except Exception as e:
            logger.error(f"REST request failed: {e}", exc_info=True)
            self.failed_requests += 1
            return False, {"error": str(e)}
    
    def http_request(self, method: str, url: str, data: Optional[Dict[str, Any]] = None, 
                     headers: Optional[Dict[str, str]] = None, retries: int = 0) -> Tuple[bool, Any]:
        """
        Execute an HTTP request to GitHub API with rate limiting.
        
        Args:
            method: HTTP method ('GET', 'POST', 'PATCH', 'PUT', 'DELETE')
            url: Full URL (e.g., 'https://api.github.com/graphql')
            data: Optional request body (will be JSON encoded)
            headers: Optional headers to include
            retries: Current retry count (internal use)
            
        Returns:
            Tuple of (success, response_data)
        """
        try:
            import requests
        except ImportError:
            logger.error("requests library not installed")
            return False, {"error": "requests_not_installed"}
        
        # Check if breaker has recovered and can attempt again
        self.breaker.check_and_close()
        
        # Check if breaker is open
        if self.breaker.is_open():
            time_until_reset = self.breaker.reset_time - datetime.now() if self.breaker.reset_time else None
            wait_msg = f" (will retry in {time_until_reset.total_seconds():.0f}s)" if time_until_reset else ""
            logger.error(f"🔴 GitHub API breaker is OPEN - rejecting HTTP request{wait_msg}")
            return False, {"error": "GitHub API rate limit exceeded - circuit breaker open"}
        
        # Check usage and apply throttling (REST bucket - this method makes
        # direct REST/HTTP calls, never GraphQL)
        usage_percent = self.rate_limit_rest.get_percentage_used()
        if usage_percent > 95:
            wait_time = 30
            logger.warning(f"⚠️  GitHub API usage at {usage_percent:.1f}% - throttling (waiting {wait_time}s)")
            time.sleep(wait_time)
        elif usage_percent > 90:
            wait_time = 10
            logger.warning(f"⚠️  GitHub API usage at {usage_percent:.1f}% - backing off (waiting {wait_time}s)")
            time.sleep(wait_time)
        
        # Apply backoff
        self._apply_backoff()
        
        try:
            logger.debug(f"Executing HTTP {method} {url} (usage: {usage_percent:.1f}%)")
            
            # Prepare headers with authentication
            request_headers = headers or {}
            if 'Accept' not in request_headers:
                request_headers['Accept'] = 'application/vnd.github.v3+json'
            
            # Add GitHub token if not already in headers
            if 'Authorization' not in request_headers:
                token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
                if token:
                    request_headers['Authorization'] = f'token {token}'
                else:
                    logger.warning("No GitHub token found in environment variables")
            
            # Execute request based on method
            if method.upper() == 'GET':
                response = requests.get(url, headers=request_headers, timeout=30)
            elif method.upper() == 'POST':
                response = requests.post(url, json=data, headers=request_headers, timeout=30)
            elif method.upper() == 'PATCH':
                response = requests.patch(url, json=data, headers=request_headers, timeout=30)
            elif method.upper() == 'PUT':
                response = requests.put(url, json=data, headers=request_headers, timeout=30)
            elif method.upper() == 'DELETE':
                response = requests.delete(url, headers=request_headers, timeout=30)
            else:
                return False, {"error": f"Unsupported method: {method}"}
            
            self.total_requests += 1
            self._record_request('http', True)
            
            # Extract rate limit from response headers
            self._update_rate_limit_from_http_headers(response.headers)
            
            # Check for rate limit error
            if response.status_code == 403:
                if 'rate limit' in response.text.lower():
                    self.rate_limited_requests += 1
                    logger.error("🔴 GitHub API rate limit hit (HTTP)")
                    self.breaker.trip()
                    return False, {"error": "rate_limited", "status_code": 403}
            
            # Check for other errors
            if response.status_code >= 400:
                self.failed_requests += 1
                logger.error(f"HTTP request failed: {response.status_code} - {response.text[:200]}")
                
                # Retry transient errors (5xx)
                if response.status_code >= 500 and retries < 3:
                    wait_time = (2 ** retries) * 2
                    logger.info(f"Retrying after {wait_time}s (attempt {retries + 1}/3)")
                    time.sleep(wait_time)
                    return self.http_request(method, url, data, headers, retries + 1)
                
                return False, {"error": f"http_error_{response.status_code}", "status_code": response.status_code}
            
            # Success
            try:
                result_data = response.json()
            except ValueError:
                # Empty response
                result_data = {}
            
            self.backoff_multiplier = 1.0
            
            # Track the operation
            self.track_gh_operation('http_api', f'HTTP {method} {url} executed successfully')
            
            return True, result_data
        
        except requests.exceptions.Timeout:
            logger.error("HTTP request timed out")
            self.failed_requests += 1
            return False, {"error": "timeout"}
        except Exception as e:
            logger.error(f"HTTP request failed: {e}", exc_info=True)
            self.failed_requests += 1
            return False, {"error": str(e)}
    
    def _apply_backoff(self):
        """Apply exponential backoff based on recent failures."""
        with self.lock:
            now = time.time()
            time_since_last = now - self.last_request_time
            min_wait = self.min_request_interval * self.backoff_multiplier
            
            if time_since_last < min_wait:
                wait_time = min_wait - time_since_last
                logger.debug(f"Rate limiting: waiting {wait_time:.2f}s")
                time.sleep(wait_time)
            
            self.last_request_time = time.time()
    
    def _record_request(self, method: str, success: bool):
        """Record request in history."""
        self.request_history.append({
            'timestamp': datetime.now().isoformat(),
            'method': method,
            'success': success,
        })
    
    def _update_rate_limit_from_graphql_response(self, response: Dict[str, Any]):
        """Extract GraphQL-bucket rate limit info from a GraphQL response.
        
        Rate limit info can come from two places:
        1. extensions.cost.rateLimit - from query cost analysis
        2. data.rateLimit - if queried directly via the rateLimit query
        """
        try:
            rl = None
            
            # Try extensions.cost.rateLimit first (from cost analysis)
            if 'extensions' in response and 'cost' in response['extensions']:
                cost = response['extensions']['cost']
                if 'rateLimit' in cost:
                    rl = cost['rateLimit']
            
            # Fall back to data.rateLimit (if queried directly)
            if not rl and 'data' in response and 'rateLimit' in response['data']:
                rl = response['data']['rateLimit']
            
            # Update rate limit if we found it
            if rl:
                self.rate_limit_graphql.remaining = rl.get('remaining', self.rate_limit_graphql.remaining)
                self.rate_limit_graphql.limit = rl.get('limit', self.rate_limit_graphql.limit)
                reset_at = rl.get('resetAt')
                if reset_at:
                    # Parse ISO format datetime
                    self.rate_limit_graphql.reset_time = datetime.fromisoformat(reset_at.replace('Z', '+00:00'))
                self.rate_limit_graphql.resource_type = "graphql"
                self.rate_limit_graphql.last_updated = datetime.now()
                self.rate_limit_graphql.ever_updated = True

                logger.debug(
                    f"Rate limit update (GraphQL): {self.rate_limit_graphql.remaining}/{self.rate_limit_graphql.limit} "
                    f"({self.rate_limit_graphql.get_percentage_used():.1f}% used)"
                )

                self._mirror_rate_limit_to_redis(self.rate_limit_graphql, RATE_LIMIT_REDIS_KEYS['graphql'])
                self.alarm_if_needed()
        except Exception as e:
            logger.debug(f"Could not extract rate limit from response: {e}")

    def _extract_reset_time(
        self, stdout: str, stderr: str, headers: Optional[Dict[str, str]] = None,
    ) -> Optional[datetime]:
        """Try to extract the rate limit reset time from a failed response.

        Prefers `headers['x-ratelimit-reset']` when available - the exact
        epoch GitHub will reset at, straight from the response that just
        got rate-limited - falling back to regex-parsing an "available in
        N seconds" message out of the error body only when no headers were
        recovered (e.g. a REST caller that doesn't pass headers through).
        """
        if headers and 'x-ratelimit-reset' in headers:
            try:
                return datetime.fromtimestamp(int(headers['x-ratelimit-reset']))
            except (ValueError, TypeError) as e:
                logger.debug(f"Could not parse x-ratelimit-reset header: {e}")

        try:
            # Try parsing stdout as JSON
            data = json.loads(stdout)
            if 'errors' in data and len(data['errors']) > 0:
                error = data['errors'][0]
                if 'message' in error:
                    msg = error['message']
                    # Look for reset time pattern
                    if 'available in' in msg.lower():
                        # "API rate limit will be available in 3599 seconds"
                        match = re.search(r'available in (\d+) seconds', msg, re.IGNORECASE)
                        if match:
                            seconds = int(match.group(1))
                            return datetime.now() + timedelta(seconds=seconds)
        except Exception as e:
            logger.debug(f"Could not extract reset time: {e}")

        return None
    
    def _update_rate_limit_from_headers(
        self, headers: Dict[str, str], bucket: 'GitHubRateLimitStatus',
        resource_type: str, redis_key: str, log_label: str,
    ):
        """Shared implementation for updating one bucket from raw
        x-ratelimit-* response headers - both REST and GraphQL report the
        identical header shape, so the REST-specific
        (_update_rate_limit_from_http_headers) and GraphQL-specific
        (_update_rate_limit_from_graphql_headers) methods are now thin
        wrappers passing in which bucket/resource_type/Redis key to update.
        `resource_type` is always stamped explicitly rather than trusted
        from the header (GitHub reports the REST bucket's own header value
        as "core", not "rest"), so reporting stays consistent regardless
        of GitHub's internal label.
        """
        try:
            if 'x-ratelimit-limit' in headers:
                bucket.limit = int(headers['x-ratelimit-limit'])
            if 'x-ratelimit-remaining' in headers:
                bucket.remaining = int(headers['x-ratelimit-remaining'])
            if 'x-ratelimit-reset' in headers:
                reset_timestamp = int(headers['x-ratelimit-reset'])
                bucket.reset_time = datetime.fromtimestamp(reset_timestamp)
            bucket.resource_type = resource_type
            bucket.last_updated = datetime.now()
            bucket.ever_updated = True

            logger.debug(
                f"Rate limit update ({log_label}): {bucket.remaining}/{bucket.limit} "
                f"({bucket.get_percentage_used():.1f}% used)"
            )

            self._mirror_rate_limit_to_redis(bucket, redis_key)
            self.alarm_if_needed()
        except Exception as e:
            logger.debug(f"Could not extract rate limit from {log_label} headers: {e}")

    def _update_rate_limit_from_http_headers(self, headers: Dict[str, str]):
        """Update the REST bucket from raw x-ratelimit-* response headers.

        Used by both http_request() (real HTTP headers via `requests`) and
        rest()'s `gh api --include` path (headers recovered from CLI
        stdout - see _parse_gh_api_include_output) - the two ways this
        client makes REST calls.
        """
        self._update_rate_limit_from_headers(
            headers, self.rate_limit_rest, "rest", RATE_LIMIT_REDIS_KEYS['rest'], "REST"
        )

    def _update_rate_limit_from_graphql_headers(self, headers: Dict[str, str]):
        """Update the GraphQL bucket from raw x-ratelimit-* response headers.

        Used by graphql()'s `gh api graphql --include` path (headers
        recovered from CLI stdout - see _parse_gh_api_include_output).

        This is the primary signal for the GraphQL bucket: unlike the
        extensions.cost.rateLimit/data.rateLimit fields
        _update_rate_limit_from_graphql_response() looks for, these
        headers are present on every response regardless of what the
        query itself asked for.
        """
        self._update_rate_limit_from_headers(
            headers, self.rate_limit_graphql, "graphql", RATE_LIMIT_REDIS_KEYS['graphql'], "GraphQL headers"
        )

    def _parse_gh_api_include_output(self, stdout: str) -> Tuple[Dict[str, str], str]:
        """Split `gh api --include` stdout into (headers, body).

        With --include, `gh api` prefixes its output with an HTTP status
        line and response headers, separated from the body by a blank
        line - the same shape as a raw HTTP response. This is how the
        REST bucket's x-ratelimit-* headers get recovered when going
        through the `gh` CLI, which otherwise gives no access to response
        headers at all (unlike http_request()'s direct `requests` calls,
        which see real headers).

        Falls back to treating the whole input as the body (no headers)
        when the first line doesn't look like an HTTP status line - covers
        both plain `gh api` output (no --include, e.g. in tests that mock
        subprocess with a bare JSON string) and any future `gh` release
        that changes or drops the --include framing. Without this check, a
        JSON body that happens to contain a blank line right after some
        text resembling a status line could be mis-split; with it, that
        case instead falls through untouched to the body.
        """
        first_line, sep, _ = stdout.partition('\n')
        if not sep or not first_line.startswith('HTTP/'):
            return {}, stdout

        # An empty body here is legitimate (e.g. a 204, or any endpoint
        # that returns headers with no content) and must be preserved as
        # '' rather than treated as malformed - rest()'s caller already
        # handles an empty body as a successful empty-response result.
        header_block, _, body = stdout.partition('\n\n')

        headers: Dict[str, str] = {}
        for line in header_block.split('\n')[1:]:  # [0] is the "HTTP/2.0 200 OK" status line
            if ':' in line:
                key, _, value = line.partition(':')
                headers[key.strip().lower()] = value.strip()
        return headers, body
    
    def get_status(self) -> dict:
        """Get current API client status."""
        return {
            # Backward-compat: 'rate_limit' is the GraphQL bucket (see
            # __init__). New code should read 'rate_limit_graphql' /
            # 'rate_limit_rest' directly instead of this conflated field.
            'rate_limit': self.rate_limit.to_dict(),
            'rate_limit_graphql': self.rate_limit_graphql.to_dict(),
            'rate_limit_rest': self.rate_limit_rest.to_dict(),
            'breaker': {
                'state': self.breaker.state,
                'is_open': self.breaker.is_open(),
                'opened_at': self.breaker.opened_at.isoformat() if self.breaker.opened_at else None,
                'reset_time': self.breaker.reset_time.isoformat() if self.breaker.reset_time else None,
            },
            'stats': {
                'total_requests': self.total_requests,
                'failed_requests': self.failed_requests,
                'rate_limited_requests': self.rate_limited_requests,
                'backoff_multiplier': self.backoff_multiplier,
            }
        }
    
    # Minimum spacing between alarm_if_needed() checks, once every real
    # rate-limit reading triggers a call to it (see
    # _update_rate_limit_from_headers/_update_rate_limit_from_graphql_
    # response) rather than a 5-minute periodic poll. Without this, a
    # sustained low-quota period would re-log the same warning on every
    # single GitHub call instead of periodically.
    ALARM_CHECK_MIN_INTERVAL_SECONDS = 60

    def alarm_if_needed(self):
        """Check if we should alarm based on rate limit usage, for both the
        REST and GraphQL buckets. Debounced to at most once every
        ALARM_CHECK_MIN_INTERVAL_SECONDS - this is now called on every real
        rate-limit reading (previously only from a 5-minute periodic poll,
        which this PR removed - see _mirror_rate_limit_to_redis's
        docstring), so without debouncing a sustained low-quota period
        would spam the same warning on every single GitHub call.
        """
        now = time.monotonic()
        if (self._last_alarm_check_at is not None
                and now - self._last_alarm_check_at < self.ALARM_CHECK_MIN_INTERVAL_SECONDS):
            return
        self._last_alarm_check_at = now

        for bucket_label, status in (('GraphQL', self.rate_limit_graphql), ('REST', self.rate_limit_rest)):
            usage = status.get_percentage_used()
            remaining = status.remaining

            if remaining <= 100:
                logger.critical(
                    f"🚨 CRITICAL: GitHub {bucket_label} API rate limit critically low! "
                    f"Only {remaining} points remaining ({usage:.1f}% used)"
                )
            elif remaining <= 250:
                logger.error(
                    f"🔴 WARNING: GitHub {bucket_label} API rate limit low! "
                    f"Only {remaining} points remaining ({usage:.1f}% used)"
                )
            elif usage >= 95:
                logger.warning(
                    f"⚠️  GitHub {bucket_label} API usage at 95%+: {remaining} points remaining"
                )
            elif usage >= 90:
                logger.warning(
                    f"⚠️  GitHub {bucket_label} API usage at 90%: {remaining} points remaining"
                )
            elif usage >= 80:
                logger.warning(
                    f"ℹ️  GitHub {bucket_label} API usage at 80%: {remaining} points remaining"
                )
    
    def gh_cli(self, cmd: List[str], retries: int = 0) -> Tuple[bool, Any]:
        """
        Execute a GitHub CLI command with circuit breaker awareness.
        
        Use this for arbitrary 'gh' commands that need rate limiting and
        circuit breaker protection (e.g., 'gh project create', 'gh pr create', etc.)
        
        Args:
            cmd: List of command parts, e.g., ['gh', 'project', 'create', ...]
            retries: Current retry count (internal use)
            
        Returns:
            Tuple of (success, result) where result is parsed JSON if applicable, else raw output
        """
        # Check if breaker has recovered and can attempt again
        self.breaker.check_and_close()
        
        # Check if breaker is open
        if self.breaker.is_open():
            time_until_reset = self.breaker.reset_time - datetime.now() if self.breaker.reset_time else None
            wait_msg = f" (will retry in {time_until_reset.total_seconds():.0f}s)" if time_until_reset else ""
            logger.error(f"🔴 GitHub API breaker is OPEN - rejecting CLI command{wait_msg}")
            return False, {"error": "GitHub API rate limit exceeded - circuit breaker open"}
        
        # Apply backoff
        self._apply_backoff()
        
        try:
            logger.debug(f"Executing GitHub CLI: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
            
            self.total_requests += 1
            self._record_request('gh_cli', True)
            
            # Try to parse as JSON if --format json was used
            try:
                data = json.loads(result.stdout)
                self.backoff_multiplier = 1.0
                self.track_gh_operation('gh_cli', ' '.join(cmd))
                return True, data
            except (json.JSONDecodeError, ValueError):
                # Not JSON output, return raw
                self.backoff_multiplier = 1.0
                self.track_gh_operation('gh_cli', ' '.join(cmd))
                return True, {"output": result.stdout}
        
        except subprocess.CalledProcessError as e:
            self.failed_requests += 1

            # Check for rate limit error
            if 'rate limit' in e.stderr.lower() or 'rate limit' in e.stdout.lower():
                self.rate_limited_requests += 1
                logger.error("🔴 GitHub API rate limit hit (CLI command)")
                self.breaker.trip()
                return False, {"error": "rate_limited", "stderr": e.stderr}

            # Check for HTTP 410 (Gone/Deleted) - permanent error, don't retry
            if 'HTTP 410' in e.stderr or 'was deleted' in e.stderr:
                logger.error(f"GitHub CLI command failed with HTTP 410 (resource deleted): {' '.join(cmd)}")
                logger.error(f"Error details: {e.stderr}")
                return False, {"error": "resource_deleted", "http_code": 410, "stderr": e.stderr}

            # Check for other 4xx errors that shouldn't be retried
            if any(code in e.stderr for code in ['HTTP 404', 'HTTP 403', 'HTTP 401', 'HTTP 422']):
                logger.error(f"GitHub CLI command failed with client error: {' '.join(cmd)}")
                logger.error(f"Error details: {e.stderr}")
                return False, {"error": "client_error", "stderr": e.stderr}

            logger.error(f"GitHub CLI command failed: {' '.join(cmd)}")
            logger.error(f"Exit code: {e.returncode}")
            logger.error(f"STDERR: {e.stderr[:200]}")

            # Retry transient errors on 5xx or timeout-like errors
            if 'temporarily' in e.stderr.lower() or 'timeout' in e.stderr.lower():
                if retries < 3:
                    wait_time = (2 ** retries) * 2
                    logger.info(f"Retrying transient error after {wait_time}s (attempt {retries + 1}/3)")
                    time.sleep(wait_time)
                    return self.gh_cli(cmd, retries + 1)

            return False, {"error": f"cli_error", "exit_code": e.returncode, "stderr": e.stderr}
        
        except subprocess.TimeoutExpired:
            logger.error(f"GitHub CLI command timed out: {' '.join(cmd)}")
            self.failed_requests += 1
            return False, {"error": "timeout"}
        
        except Exception as e:
            logger.error(f"GitHub CLI command failed: {e}", exc_info=True)
            self.failed_requests += 1
            return False, {"error": str(e)}
    
    def track_gh_operation(self, operation_type: str, description: str) -> None:
        """
        Track a GitHub CLI operation that makes indirect API calls.
        
        Use this to track 'gh pr create', 'gh issue create', etc. which make
        API calls but aren't direct graphql/rest/http calls.
        
        Args:
            operation_type: Type of operation (e.g., 'gh_pr_create', 'gh_issue_create')
            description: Human-readable description of what was done
        """
        self.total_requests += 1

        # Log the operation at INFO level for visibility
        logger.info(f"📊 GitHub CLI operation tracked: {operation_type} - {description}")
        
        # Record in request history for debugging
        self._record_request(operation_type, True)
        
        # Log to stdout as well for visibility in container logs
        logger.debug(f"[GITHUB_API_TRACKING] {operation_type}: {description}")
        
        # Add stack trace if tracing is enabled
        if TRACE_API_CALLS:
            self._log_call_stack(operation_type)
    
    def _log_call_stack(self, operation_type: str) -> None:
        """Log the call stack showing where this API call came from"""
        stack = inspect.stack()
        
        # Skip internal frames (track_gh_operation, graphql, rest, etc.)
        relevant_frames = []
        for frame_info in stack[2:]:  # Skip _log_call_stack and track_gh_operation
            module_name = frame_info.filename.split('/')[-1]
            if 'github_api_client' in module_name:
                continue  # Skip frames inside this file
            relevant_frames.append(frame_info)
        
        if not relevant_frames:
            return
        
        # Get the immediate caller info for buffer tracking
        caller_info = None
        if relevant_frames:
            frame = relevant_frames[0]
            module = frame.filename.split('/')[-1].replace('.py', '')
            func_name = frame.function
            line_num = frame.lineno
            code_line = frame.code_context[0].strip() if frame.code_context else "???"
            caller_info = f"{module}:{func_name}():{line_num}"
            
            # Add to call trace buffer
            with self.call_trace_lock:
                self.call_trace_buffer.append({
                    'timestamp': datetime.now(),
                    'operation_type': operation_type,
                    'caller': caller_info,
                    'code_line': code_line
                })
        
        # Log at debug level (keep existing functionality)
        logger.debug(f"  📍 Call stack for {operation_type}:")
        
        # Show the most relevant frame (immediate caller)
        if relevant_frames:
            frame = relevant_frames[0]
            module = frame.filename.split('/')[-1].replace('.py', '')
            func_name = frame.function
            line_num = frame.lineno
            code_line = frame.code_context[0].strip() if frame.code_context else "???"
            
            logger.debug(f"    └─ {module}:{func_name}() [line {line_num}]")
            logger.debug(f"       {code_line}")
        
        # Show full stack if multiple levels
        if len(relevant_frames) > 1:
            logger.debug(f"  📍 Full call stack ({len(relevant_frames)} frames):")
            for i, frame in enumerate(relevant_frames[:2]):  # Show top 2 frames
                module = frame.filename.split('/')[-1].replace('.py', '')
                func_name = frame.function
                line_num = frame.lineno
                logger.debug(f"    {i+1}. {module}:{func_name}() [line {line_num}]")
    
    def _mirror_rate_limit_to_redis(self, bucket: 'GitHubRateLimitStatus', redis_key: str):
        """Best-effort publish of this bucket's latest real-response reading
        to Redis, so other processes (observability-server, and ephemeral
        repair-cycle/agent containers that each run their own
        GitHubAPIClient) can see this process's GitHub traffic without
        needing a GitHubAPIClient of their own - see get_shared_rate_limit_
        status(). This used to be handled by a periodic self-poll of
        GitHub's dedicated /rate_limit endpoint instead, but that endpoint
        was found to return bogus always-full data for at least one
        token/org (issue #103 follow-up) - removed rather than trusted.
        Only ever called with a bucket that a real GitHub response headers
        parse just updated, so there's no equivalent trust problem here.

        Guards against a slower, now-stale write from a sibling process
        clobbering a fresher, more-depleted reading already in Redis, two
        ways:
        - Within the window recorded there (`now < stored reset_time`),
          `remaining` can only stay flat or decrease, so an incoming value
          that's higher without also reporting a newer reset is discarded
          as an out-of-order race rather than applied.
        - An incoming reset_time strictly OLDER than the stored one is
          always discarded outright, regardless of remaining - this is the
          window-rollover case the remaining-only check above can't catch:
          a delayed pre-rollover write (low remaining, old window) arriving
          after a fresh post-rollover write (full remaining, new window)
          would otherwise pass the remaining-comparison check (its
          remaining is LOWER, not higher) and incorrectly overwrite the
          correct, newer reading with a stale one carrying an
          already-past reset_time.

        This is a best-effort read-modify-write, not atomic - two
        concurrent writers can both read the same "existing" value and
        both proceed to write. Acceptable here: the worst case is one of
        two genuinely-concurrent real readings winning arbitrarily, not an
        actually-wrong value being applied over a right one.
        """
        try:
            client = _get_shared_redis_client()

            now = datetime.now(timezone.utc)
            # bucket.reset_time is naive (produced by datetime.now()/
            # datetime.fromtimestamp() elsewhere in this file, deliberately
            # left that way since it's also compared against process-local
            # datetime.now() calls, e.g. GitHubBreaker). Normalize a local
            # copy to UTC-aware here since existing_reset (parsed from the
            # Redis-stored, already-UTC value) is always aware - comparing
            # naive against aware raises TypeError.
            bucket_reset_utc = (
                bucket.reset_time.astimezone(timezone.utc) if bucket.reset_time else None
            )
            existing_json = client.get(redis_key)
            if existing_json:
                try:
                    existing = json.loads(existing_json)
                    existing_reset = _parse_utc_isoformat(existing.get('reset_time'))

                    older_window = (
                        bucket_reset_utc is not None
                        and existing_reset is not None
                        and bucket_reset_utc < existing_reset
                    )
                    if older_window:
                        logger.debug(
                            f"Skipping rate limit mirror for {redis_key}: incoming reset_time "
                            f"({bucket_reset_utc}) is older than the stored window's reset_time "
                            f"({existing_reset}) - a delayed pre-rollover write, ignoring"
                        )
                        return

                    newer_window = (
                        bucket_reset_utc is not None
                        and existing_reset is not None
                        and bucket_reset_utc > existing_reset
                    )
                    still_in_window = existing_reset is not None and now < existing_reset

                    if (still_in_window and not newer_window
                            and existing.get('remaining') is not None
                            and bucket.remaining > existing['remaining']):
                        logger.debug(
                            f"Skipping rate limit mirror for {redis_key}: incoming remaining "
                            f"({bucket.remaining}) is higher than the stored value "
                            f"({existing['remaining']}) within the same window - "
                            f"likely an out-of-order write from another process, ignoring"
                        )
                        return
                except (ValueError, KeyError, TypeError) as parse_err:
                    # Not a routine condition (this key is only ever written by
                    # this same method) - either manual tampering, real data
                    # corruption, or a schema change deployed inconsistently
                    # across a rolling restart. Overwriting below self-heals
                    # it, but the anomaly itself is worth a visible log line.
                    logger.warning(f"Could not parse existing rate limit mirror at {redis_key}: {parse_err}")

            payload = {
                'remaining': bucket.remaining,
                'limit': bucket.limit,
                'reset_time': _utc_isoformat(bucket.reset_time),
                'last_updated': _utc_isoformat(bucket.last_updated),
            }
            # No TTL - see the module-level comment above RATE_LIMIT_STALE_
            # THRESHOLD_SECONDS for why these two keys are deliberately
            # left to persist indefinitely rather than expire.
            client.set(redis_key, json.dumps(payload))

            if self._redis_mirror_consecutive_failures > 0:
                logger.warning(
                    f"Rate limit mirror to Redis recovered for {redis_key} after "
                    f"{self._redis_mirror_consecutive_failures} consecutive failures"
                )
                self._redis_mirror_consecutive_failures = 0
        except Exception as e:
            self._redis_mirror_consecutive_failures += 1
            # First failure of a run logged loudly (a sustained outage should
            # be visible at default log verbosity, not require debug logging
            # to be enabled) - subsequent ones in the same outage stay at
            # debug to avoid spamming on every single GitHub call.
            if self._redis_mirror_consecutive_failures == 1:
                logger.warning(f"Failed to mirror rate limit to Redis ({redis_key}): {e}")
            else:
                logger.debug(
                    f"Failed to mirror rate limit to Redis ({redis_key}) "
                    f"(consecutive failure #{self._redis_mirror_consecutive_failures}): {e}"
                )

    def _start_call_trace_summarizer(self):
        """Start background thread that periodically summarizes/trims the
        call trace buffer (drives the "Call Summary" log line and keeps
        call_trace_buffer from growing unbounded). This used to also
        re-poll GitHub's /rate_limit endpoint on the same schedule; that
        poll was removed (see _mirror_rate_limit_to_redis's docstring) but
        the buffer still needs periodic cleanup independent of it.
        """
        def summarize_call_traces():
            import time
            while True:
                try:
                    time.sleep(300)  # Every 5 minutes
                    self._summarize_and_cleanup_call_traces()
                except Exception as e:
                    logger.debug(f"Error in call trace summarizer: {e}")

        # Start thread as daemon so it doesn't block shutdown
        thread = Thread(target=summarize_call_traces, daemon=True)
        thread.start()

    def _summarize_and_cleanup_call_traces(self):
        """Summarize call traces and remove old entries (older than 1 hour)"""
        with self.call_trace_lock:
            if not self.call_trace_buffer:
                return
            
            now = datetime.now()
            one_hour_ago = now - timedelta(hours=1)
            
            # Filter out entries older than 1 hour
            recent_traces = [
                trace for trace in self.call_trace_buffer
                if trace['timestamp'] > one_hour_ago
            ]
            
            # If no recent traces, just clear and return
            if not recent_traces:
                self.call_trace_buffer = []
                return
            
            # Group by caller for summarization
            from collections import Counter
            caller_counts = Counter()
            operation_by_caller = {}
            
            for trace in recent_traces:
                caller = trace['caller']
                operation = trace['operation_type']
                caller_counts[caller] += 1
                
                # Track operation types per caller
                if caller not in operation_by_caller:
                    operation_by_caller[caller] = Counter()
                operation_by_caller[caller][operation] += 1
            
            # Log summary at INFO level
            total_calls = len(recent_traces)
            unique_callers = len(caller_counts)
            
            logger.info(
                f"📊 GitHub API Call Summary (last hour): "
                f"{total_calls} total calls from {unique_callers} unique sources"
            )
            
            # Sort by call count (descending) and report top callers
            for caller, count in caller_counts.most_common(10):
                operations = operation_by_caller[caller]
                operation_summary = ", ".join([
                    f"{op_type}({op_count})"
                    for op_type, op_count in operations.most_common(3)
                ])
                
                logger.info(
                    f"  📍 {caller}: {count} calls "
                    f"[{operation_summary}]"
                )
            
            # Update buffer with only recent traces
            self.call_trace_buffer = recent_traces


# Global client instance
_github_client: Optional[GitHubAPIClient] = None


def get_github_client() -> GitHubAPIClient:
    """Get or create the global GitHub API client."""
    global _github_client
    if _github_client is None:
        _github_client = GitHubAPIClient()
    return _github_client


def get_shared_rate_limit_status(resource_type: str) -> dict:
    """Read the cross-process view of a rate-limit bucket ('rest' or
    'graphql') as mirrored to Redis by whichever process most recently
    made a real GitHub call and parsed its response headers (see
    GitHubAPIClient._mirror_rate_limit_to_redis).

    This is what anything display-facing (HealthMonitor.check_github(),
    the /health route) should read - never a locally-instantiated
    GitHubAPIClient's own get_status(), which only reflects real GitHub
    responses if that specific process happens to be the one making real
    GitHub traffic. observability-server, in particular, is not; querying
    its own idle client used to silently overwrite correct numbers with
    that client's permanent startup defaults (issue #103 follow-up).

    Returns a dict with 'never_observed' True when the Redis key is
    genuinely empty - no process has ever published a reading for this
    bucket (e.g. right after a fresh deploy, before any real call has been
    made anywhere) - and 'stale' True when the most recent reading is
    older than RATE_LIMIT_STALE_THRESHOLD_SECONDS. These are deliberately
    distinct signals for the UI to render differently ("never seen data"
    vs. "have a number, just an old one").

    A THIRD, distinct case - 'unavailable' True - covers everything that
    ISN'T "the key is genuinely empty": a Redis connection failure or
    timeout, corrupted/unparseable stored data, or any other read error.
    Conflating this with never_observed would be actively misleading: it
    would render identically to a calm, healthy "fresh deploy, no data
    yet" state, silently hiding an actual outage of the mechanism this
    function exists to provide (and, per check_github()'s degraded
    check below, would also silently disable rate-limit-exhaustion
    detection for as long as the outage lasts). Callers that care about
    distinguishing a real problem from a quiet bucket should check
    'unavailable' explicitly.
    """
    redis_key = RATE_LIMIT_REDIS_KEYS.get(resource_type)
    if not redis_key:
        raise ValueError(f"Unknown rate limit resource_type: {resource_type!r}")

    never_observed_result = {
        'remaining': None,
        'limit': None,
        'percentage_used': None,
        'reset_time': None,
        'last_updated': None,
        'never_observed': True,
        'unavailable': False,
        'stale': False,
    }
    unavailable_result = {**never_observed_result, 'never_observed': False, 'unavailable': True}

    try:
        client = _get_shared_redis_client()
        raw = client.get(redis_key)
        if not raw:
            return never_observed_result

        data = json.loads(raw)
        remaining = data.get('remaining')
        limit = data.get('limit')
        last_updated_str = data.get('last_updated')
        last_updated = _parse_utc_isoformat(last_updated_str)

        percentage_used = None
        if limit:
            percentage_used = ((limit - remaining) / limit) * 100

        stale = (
            last_updated is None
            or (datetime.now(timezone.utc) - last_updated).total_seconds() > RATE_LIMIT_STALE_THRESHOLD_SECONDS
        )

        return {
            'remaining': remaining,
            'limit': limit,
            'percentage_used': percentage_used,
            'reset_time': data.get('reset_time'),
            'last_updated': last_updated_str,
            'never_observed': False,
            'unavailable': False,
            'stale': stale,
        }
    except Exception as e:
        # Anything here (connection failure/timeout, corrupted stored
        # data, an unparseable timestamp) is a real read problem, not "no
        # reading exists yet" - see this function's docstring. Logged at
        # warning (not debug): a sustained outage of the mechanism should
        # be visible at default log verbosity.
        logger.warning(f"Failed to read shared rate limit status for {resource_type}: {e}")
        return unavailable_result

