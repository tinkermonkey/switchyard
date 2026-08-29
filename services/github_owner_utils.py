"""
GitHub Owner Type Detection Utility

This module provides utilities to determine whether a GitHub owner (login)
is a User or Organization, which is required for correctly querying
GitHub Projects v2 API.
"""

import subprocess
import json
import logging
import time
import redis
import asyncio
from typing import Any, Dict, List, Literal, Optional, Tuple
from functools import lru_cache, wraps
from services.circuit_breaker import CircuitBreaker, CircuitBreakerOpen

logger = logging.getLogger(__name__)

OwnerType = Literal['user', 'organization']


class GitHubCacheManager:
    """Redis-backed cache for GitHub API responses with fallback to in-memory cache."""

    def __init__(self):
        self.redis_client = None
        self._memory_cache = {}

        # Try multiple Redis hosts (Docker and localhost)
        for host in ['redis', 'localhost', '127.0.0.1']:
            try:
                self.redis_client = redis.Redis(
                    host=host,
                    port=6379,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=5
                )
                # Test connection
                self.redis_client.ping()
                logger.info(f"GitHub cache connected to Redis at {host}")
                break
            except Exception as e:
                logger.debug(f"Could not connect to Redis at {host}: {e}")
                continue

        if not self.redis_client:
            logger.warning("Redis unavailable for GitHub cache, using in-memory fallback")
            self.redis_client = None

    def get(self, key: str) -> Optional[str]:
        """Get value from cache."""
        if self.redis_client:
            try:
                return self.redis_client.get(key)
            except Exception as e:
                logger.warning(f"Redis get failed, falling back to memory cache: {e}")
                return self._memory_cache.get(key)
        else:
            return self._memory_cache.get(key)

    def set(self, key: str, value: str, ttl_seconds: int):
        """Set value in cache with TTL."""
        if self.redis_client:
            try:
                self.redis_client.setex(key, ttl_seconds, value)
                return
            except Exception as e:
                logger.warning(f"Redis set failed, using memory cache: {e}")

        # Fallback to in-memory cache (no TTL enforcement in memory)
        self._memory_cache[key] = value


# Global cache instance
_github_cache = GitHubCacheManager()

# Global circuit breaker for GitHub API calls
_github_circuit_breaker = CircuitBreaker(
    name="github_api_owner_utils",
    failure_threshold=5,
    recovery_timeout=60,
    expected_exception=subprocess.CalledProcessError
)


def _check_circuit_breaker():
    """
    Check if circuit breaker allows requests.
    Raises CircuitBreakerOpen if circuit is open.
    """
    from services.circuit_breaker import CircuitState
    from datetime import datetime

    if _github_circuit_breaker.state == CircuitState.OPEN:
        # Check if we should attempt reset
        if _github_circuit_breaker.last_failure_time:
            elapsed = (datetime.now() - _github_circuit_breaker.last_failure_time).total_seconds()
            if elapsed >= _github_circuit_breaker.recovery_timeout:
                # Transition to half-open
                _github_circuit_breaker._transition_to_half_open()
                return

        # Circuit is still open
        wait_time = _github_circuit_breaker._time_until_retry()
        raise CircuitBreakerOpen(
            f"GitHub API circuit breaker is open. Retry in {wait_time:.0f}s"
        )


def _record_success():
    """Record successful GitHub API call."""
    _github_circuit_breaker._on_success()


def _record_failure():
    """Record failed GitHub API call."""
    _github_circuit_breaker._on_failure()


def _is_rate_limited(error_message: str) -> bool:
    """Check if error message indicates GitHub rate limiting."""
    rate_limit_indicators = [
        "rate limit exceeded",
        "API rate limit",
        "You have exceeded",
        "secondary rate limit",
        "403",
        "abuse detection"
    ]
    return any(indicator.lower() in error_message.lower() for indicator in rate_limit_indicators)


def retry_on_timeout(retries: int = 2, backoff: float = 2.0):
    """Decorator to retry functions on subprocess timeout and rate limits."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(retries + 1):
                try:
                    return func(*args, **kwargs)

                except subprocess.TimeoutExpired:
                    if attempt < retries:
                        wait_time = backoff ** attempt
                        logger.warning(f"{func.__name__} timed out, retrying in {wait_time}s (attempt {attempt + 1}/{retries})...")
                        time.sleep(wait_time)
                    else:
                        raise

                except subprocess.CalledProcessError as e:
                    # Check for rate limiting
                    if _is_rate_limited(e.stderr):
                        # Use longer backoff for rate limits
                        wait_time = 60 * (2 ** attempt)  # 60s, 120s, 240s
                        logger.warning(
                            f"{func.__name__} hit GitHub rate limit. "
                            f"Backing off for {wait_time}s (attempt {attempt + 1}/{retries + 1})"
                        )
                        if attempt < retries:
                            time.sleep(wait_time)
                        else:
                            logger.error(f"{func.__name__} rate limited after {retries + 1} attempts")
                            raise
                    else:
                        # Not a rate limit error, re-raise immediately
                        raise

            raise  # Should never reach here
        return wrapper
    return decorator


@retry_on_timeout()
def get_owner_type(owner_login: str) -> Optional[OwnerType]:
    """
    Determine if a GitHub owner is a User or Organization.
    Uses Redis cache with 24-hour TTL to reduce API calls.
    Protected by circuit breaker to prevent API overload.

    Args:
        owner_login: GitHub username or organization name

    Returns:
        'user' or 'organization', or None if unable to determine
    """
    # Check cache first (bypass circuit breaker for cached values)
    cache_key = f"github:owner_type:{owner_login}"
    cached_value = _github_cache.get(cache_key)

    if cached_value:
        logger.debug(f"Owner '{owner_login}' type from cache: {cached_value}")
        return cached_value  # type: ignore

    # Check circuit breaker before making API call
    try:
        _check_circuit_breaker()
    except CircuitBreakerOpen as e:
        logger.error(f"Cannot determine owner type for '{owner_login}': {e}")
        return None

    try:
        # Query GitHub API to get owner type
        result = subprocess.run(
            ['gh', 'api', f'/users/{owner_login}', '--jq', '.type'],
            capture_output=True,
            text=True,
            timeout=15,
            check=True
        )

        owner_type = result.stdout.strip().lower()

        if owner_type == 'user':
            logger.debug(f"Owner '{owner_login}' is a User")
            _github_cache.set(cache_key, 'user', ttl_seconds=86400)  # 24 hours
            _record_success()  # Record success for circuit breaker
            return 'user'
        elif owner_type == 'organization':
            logger.debug(f"Owner '{owner_login}' is an Organization")
            _github_cache.set(cache_key, 'organization', ttl_seconds=86400)  # 24 hours
            _record_success()  # Record success for circuit breaker
            return 'organization'
        else:
            logger.warning(f"Unknown owner type '{owner_type}' for '{owner_login}'")
            _record_success()  # Still a successful API call, even if unexpected result
            return None

    except subprocess.CalledProcessError as e:
        # Don't count rate limits as failures for circuit breaker
        if not _is_rate_limited(e.stderr):
            _record_failure()  # Record failure for circuit breaker
        logger.error(f"Failed to determine owner type for '{owner_login}': {e.stderr}")
        return None
    except subprocess.TimeoutExpired as e:
        _record_failure()  # Record timeout as failure
        logger.error(f"Timeout determining owner type for '{owner_login}': {e}")
        return None
    except Exception as e:
        logger.error(f"Error determining owner type for '{owner_login}': {e}")
        return None


# Field selection shared by every projectV2 board query. Extracted so the
# single-board query builder (build_projects_v2_query) and the batched/aliased
# cross-project builder (build_batched_projects_v2_query) can't drift apart -
# there is exactly one place that defines what a "board" query fetches.
#
# NOTE: This is inserted verbatim inside a `projectV2(number: N) { ... }`
# selection, so it must remain a plain (non-f-string-braced) GraphQL field
# selection - no `{{`/`}}` escaping needed when it's spliced into an f-string.
_PROJECT_V2_FIELDS = '''id
                    title
                    items(first: 100, orderBy: {field: POSITION, direction: ASC}) {
                        nodes {
                            id
                            content {
                                __typename
                                ... on Issue {
                                    id
                                    number
                                    title
                                    state
                                    repository {
                                        name
                                    }
                                    updatedAt
                                }
                            }
                            fieldValues(first: 10) {
                                nodes {
                                    ... on ProjectV2ItemFieldSingleSelectValue {
                                        name
                                        field {
                                            ... on ProjectV2SingleSelectField {
                                                name
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }'''


def build_projects_v2_query(owner_login: str, project_number: int) -> Optional[str]:
    """
    Build a GraphQL query for GitHub Projects v2 based on owner type.

    Args:
        owner_login: GitHub username or organization name
        project_number: Project number

    Returns:
        GraphQL query string, or None if owner type cannot be determined
    """
    owner_type = get_owner_type(owner_login)

    if owner_type is None:
        logger.error(f"Cannot build Projects v2 query - unable to determine owner type for '{owner_login}'")
        return None

    # Determine the correct GraphQL query based on owner type
    if owner_type == 'user':
        query = f'''{{
            user(login: "{owner_login}") {{
                projectV2(number: {project_number}) {{
                    {_PROJECT_V2_FIELDS}
                }}
            }}
        }}'''
    else:  # organization
        query = f'''{{
            organization(login: "{owner_login}") {{
                projectV2(number: {project_number}) {{
                    {_PROJECT_V2_FIELDS}
                }}
            }}
        }}'''

    return query


# ---------------------------------------------------------------------------
# Batched / aliased cross-project board query builder
#
# One-board-per-request polling (build_projects_v2_query + a request per
# board) burns one GraphQL request per board per poll cycle. GitHub lets a
# single query alias multiple fields under one root, so N boards belonging to
# the same owner can be fetched in one request as `p<number>: projectV2(...)`
# aliases under that owner's `user`/`organization` root field.
#
# build_batched_projects_v2_query(), build_batched_board_queries() and
# parse_batched_projects_v2_response() are independent of
# build_projects_v2_query() above and do not change any existing caller's
# behavior. execute_batched_board_queries() below shares _board_query_cache
# with execute_board_query_cached() (see the "Board query cache" section
# further down) - the two are one unified cache/dedup mechanism, not
# parallel systems. Wiring this batched path into the live polling loop
# (services/project_monitor.py) is still a separate follow-up task.
# ---------------------------------------------------------------------------

# GitHub's GraphQL API enforces per-query node-count, response-size and
# execution-time limits, so an unbounded number of aliased projectV2 fields
# (each fanning out into up to 100 items with nested content/fieldValues)
# risks a query being rejected or timing out. 12 is a conservative starting
# point chosen without a production token to test against; it is tunable and
# should be re-verified against GitHub's actual limits (e.g. by watching for
# node-count/complexity errors or elevated latency) once one is available.
MAX_BOARDS_PER_BATCH = 12


def build_batched_projects_v2_query(owner_login: str, project_numbers: List[int]) -> Optional[str]:
    """
    Build ONE aliased GraphQL query document fetching multiple projectV2
    boards for a single owner in a single request.

    Each project number is exposed under its own alias (`p<number>`) so all
    boards can be selected under one `user(...)`/`organization(...)` root
    field, e.g.:

        { organization(login: "acme") {
            p1: projectV2(number: 1) { id title items(...) { ... } } }
            p2: projectV2(number: 2) { id title items(...) { ... } } }
        } }

    This does not chunk `project_numbers` itself - callers with more than
    MAX_BOARDS_PER_BATCH boards for one owner should use
    build_batched_board_queries(), which chunks and calls this per chunk.

    Args:
        owner_login: GitHub username or organization name (single owner -
            different owners require separate queries, since they'd need
            two different, mutually exclusive root fields).
        project_numbers: Project numbers to fetch for this owner.

    Returns:
        GraphQL query string, or None if project_numbers is empty or the
        owner type cannot be determined.
    """
    if not project_numbers:
        logger.warning("build_batched_projects_v2_query() called with an empty project_numbers list")
        return None

    owner_type = get_owner_type(owner_login)

    if owner_type is None:
        logger.error(f"Cannot build batched Projects v2 query - unable to determine owner type for '{owner_login}'")
        return None

    if len(project_numbers) > MAX_BOARDS_PER_BATCH:
        logger.warning(
            f"build_batched_projects_v2_query() received {len(project_numbers)} project numbers "
            f"for '{owner_login}', exceeding MAX_BOARDS_PER_BATCH={MAX_BOARDS_PER_BATCH}. "
            f"Building the oversized query anyway - use build_batched_board_queries() to chunk automatically."
        )

    aliased_fields = "\n".join(
        f'''            p{project_number}: projectV2(number: {project_number}) {{
                    {_PROJECT_V2_FIELDS}
                }}'''
        for project_number in project_numbers
    )

    root_field = 'user' if owner_type == 'user' else 'organization'

    query = f'''{{
        {root_field}(login: "{owner_login}") {{
{aliased_fields}
        }}
    }}'''

    return query


def build_batched_board_queries(
    owner_project_pairs: List[Tuple[str, int]]
) -> List[Dict[str, Any]]:
    """
    Group (owner, project_number) pairs by owner and chunk each owner's
    boards into batches of at most MAX_BOARDS_PER_BATCH, building one
    aliased GraphQL query document per chunk via
    build_batched_projects_v2_query().

    Different owners are never mixed into the same query - each query has a
    single `user`/`organization` root field, so an owner boundary always
    starts a new chunk even if the previous chunk had room left.

    Args:
        owner_project_pairs: (owner_login, project_number) tuples for every
            board to fetch, in any order and across any number of owners.

    Returns:
        A list of batch descriptors, one per query to execute, each shaped:
            {
                'owner': owner_login,
                'query': <GraphQL query string>,
                'boards': [(owner_login, project_number, alias), ...],
            }
        `boards` records, for every board in this query, the alias it was
        given (`p<number>`) so a response for this query can later be handed
        to parse_batched_projects_v2_response() along with this list.

        A chunk whose query fails to build (e.g. owner type undeterminable)
        is skipped and logged rather than included with a None query.
    """
    boards_by_owner: Dict[str, List[int]] = {}
    for owner, project_number in owner_project_pairs:
        boards_by_owner.setdefault(owner, []).append(project_number)

    batches: List[Dict[str, Any]] = []

    for owner, project_numbers in boards_by_owner.items():
        for start in range(0, len(project_numbers), MAX_BOARDS_PER_BATCH):
            chunk = project_numbers[start:start + MAX_BOARDS_PER_BATCH]
            query = build_batched_projects_v2_query(owner, chunk)
            if query is None:
                logger.error(f"Skipping batched board query for owner '{owner}' (projects {chunk}) - query build failed")
                continue

            boards = [(owner, project_number, f'p{project_number}') for project_number in chunk]
            batches.append({'owner': owner, 'query': query, 'boards': boards})

    return batches


def parse_batched_projects_v2_response(
    response: Optional[dict],
    boards: List[Tuple[str, int, str]],
) -> Tuple[Dict[Tuple[str, int], dict], Dict[Tuple[str, int], str]]:
    """
    Parse one batched/aliased GraphQL response into per-board project data.

    Handles partial-batch failure: a response for a multi-alias query can
    have `data` populated for some aliases and a top-level `errors` array
    naming the alias(es) that failed (e.g. a deleted/renamed project) via
    each error's `path`. Every alias that isn't implicated by an error is
    still parsed and returned, so one bad board never loses the rest of the
    batch.

    Args:
        response: The raw response for the query built from `boards`.
            Accepts either shape GitHubAPIClient.graphql() can hand back:
            - the full GraphQL envelope `{'data': {...}, 'errors': [...]}`
              (returned on partial/total failure, success=False), or
            - just the unwrapped data payload, e.g. `{'organization': {...}}`
              (returned on full success, success=True - it never carries a
              top-level 'errors' key in that case).
        boards: The (owner, project_number, alias) triples the query was
            built from - the same list returned in a
            build_batched_board_queries() batch descriptor's 'boards' entry.

    Returns:
        Tuple of:
        - results: dict of (owner, project_number) -> project data dict
          (`{'id':..., 'title':..., 'items': {'nodes': [...]}}`, matching
          what build_projects_v2_query() callers already extract from
          `data['user']['projectV2']` / `data['organization']['projectV2']`)
          for every alias that parsed successfully.
        - errors: dict of (owner, project_number) -> error message string
          for every alias that failed, whether via an attributable
          top-level GraphQL error or because the alias was simply missing
          from the response.
    """
    results: Dict[Tuple[str, int], dict] = {}
    errors: Dict[Tuple[str, int], str] = {}

    if not boards:
        return results, errors

    if response is None:
        for owner, project_number, _alias in boards:
            errors[(owner, project_number)] = "No response received"
        return results, errors

    # Normalize both response shapes described above into (data, errors_list).
    if 'data' in response:
        data = response.get('data') or {}
        errors_list = response.get('errors') or []
    else:
        data = response
        errors_list = []

    alias_to_board = {alias: (owner, project_number) for owner, project_number, alias in boards}

    # Attribute each top-level GraphQL error to the board(s) named in its path.
    for error in errors_list:
        if isinstance(error, dict):
            message = error.get('message', 'Unknown GraphQL error')
            path = error.get('path') or []
        else:
            message = str(error)
            path = []

        matched = False
        for segment in path:
            if isinstance(segment, str) and segment in alias_to_board:
                board_key = alias_to_board[segment]
                errors.setdefault(board_key, message)  # keep the first error per board
                matched = True
                break

        if not matched:
            logger.warning(f"Batched board query returned an error with no attributable board path: {message}")

    # Merge every owner root field present (normally just one - a batched
    # query is built for a single owner - but this stays correct even if a
    # response somehow carries more than one).
    owner_roots = []
    if isinstance(data, dict):
        for root_field in ('user', 'organization'):
            root = data.get(root_field)
            if isinstance(root, dict):
                owner_roots.append(root)

    for owner, project_number, alias in boards:
        board_key = (owner, project_number)
        if board_key in errors:
            continue  # already recorded via error path attribution above

        project_data = None
        for root in owner_roots:
            if alias in root:
                project_data = root[alias]
                break

        if project_data is None:
            errors[board_key] = (
                f"No data returned for alias '{alias}' (project may have been deleted, "
                f"renamed, or the owner root field was missing from the response)"
            )
            continue

        results[board_key] = project_data

    return results, errors


def execute_batched_board_queries(
    owner_project_pairs: List[Tuple[str, int]],
) -> Tuple[Dict[Tuple[str, int], dict], Dict[Tuple[str, int], str]]:
    """
    Build and execute the minimal set of batched, aliased GraphQL queries
    needed to fetch every requested (owner, project_number) board, merging
    parsed results and errors across every batch/chunk executed.

    Cache-aware: shares _board_query_cache (same TTL, same lock) with
    execute_board_query_cached() via the _get_cached_board()/_cache_board()
    helpers below, so the two are one unified cache/dedup mechanism rather
    than two parallel ones. Before fetching, every requested board already
    cached and fresh (within _board_query_cache_ttl) is served from the
    cache and excluded from the batch(es) actually sent to GitHub. After a
    batch response is parsed, every successfully-parsed board is written
    into the same cache (in the same envelope shape
    execute_board_query_cached() stores) so a subsequent call through
    either path is a cache hit. Boards whose fetch failed are not cached,
    matching execute_board_query_cached()'s existing behavior.

    Nothing currently calls this from the live polling loop - wiring it in
    is a separate follow-up task.

    Args:
        owner_project_pairs: (owner_login, project_number) tuples for every
            board to fetch, across any number of owners.

    Returns:
        Tuple of:
        - results: dict of (owner, project_number) -> project data dict,
          for every board served from cache or freshly fetched.
        - errors: dict of (owner, project_number) -> error message, for
          every board whose batch request failed entirely or whose alias
          failed within an otherwise-successful batch response. Boards
          served from cache are never present here.
    """
    all_results: Dict[Tuple[str, int], dict] = {}
    all_errors: Dict[Tuple[str, int], str] = {}

    # De-dup while preserving first-seen order, splitting into boards
    # already cached and fresh (served straight from the cache, no
    # network call) vs boards that still need fetching.
    to_fetch: List[Tuple[str, int]] = []
    seen = set()
    for owner, project_number in owner_project_pairs:
        cache_key = (owner, project_number)
        if cache_key in seen:
            continue
        seen.add(cache_key)

        cached_envelope = _get_cached_board(cache_key)
        if cached_envelope is not None:
            project_data = _unwrap_board_envelope(cached_envelope)
            if project_data is not None:
                logger.debug(f"Board query cache hit for {owner}/project#{project_number}")
                all_results[cache_key] = project_data
                continue

        to_fetch.append(cache_key)

    if not to_fetch:
        return all_results, all_errors

    batches = build_batched_board_queries(to_fetch)
    if not batches:
        return all_results, all_errors

    from services.github_api_client import get_github_client
    github_client = get_github_client()

    for batch in batches:
        boards = batch['boards']
        success, data = github_client.graphql(batch['query'])

        if success or (isinstance(data, dict) and ('data' in data or 'errors' in data)):
            # Either fully successful (data is the unwrapped payload), or a
            # partial failure that still carries a 'data'/'errors' envelope
            # we can salvage per-alias results from.
            results, errors = parse_batched_projects_v2_response(data, boards)
        else:
            # Total failure for this batch (rate limit, timeout, transport
            # error, etc.) with no per-board data to salvage.
            message = data.get('error', str(data)) if isinstance(data, dict) else str(data)
            logger.warning(f"Batched board query failed for owner '{batch['owner']}' ({len(boards)} boards): {message}")
            results = {}
            errors = {(owner, project_number): message for owner, project_number, _alias in boards}

        if results:
            # Populate the shared cache with every successfully-parsed
            # board, wrapped in the same envelope shape
            # execute_board_query_cached() stores/returns, so a cache hit
            # read through either path returns an equivalent value.
            #
            # The root field is read off the ACTUAL response payload (the
            # same normalization parse_batched_projects_v2_response() uses)
            # rather than re-derived via a second get_owner_type() call.
            # get_owner_type() is independently cached/circuit-broken and
            # can return a different (or None) result on this second call
            # than it did when the query was built - re-deriving risked
            # caching a board under the wrong root key (e.g. 'organization'
            # for a 'user'-owned board) whenever that happened, which
            # downstream readers (execute_board_query_cached(),
            # _unwrap_board_envelope()) key off exactly.
            response_payload = data.get('data') or data if isinstance(data, dict) and 'data' in data else data
            root_field = 'user' if isinstance(response_payload, dict) and 'user' in response_payload else 'organization'
            for board_key, project_data in results.items():
                _cache_board(board_key, {root_field: {'projectV2': project_data}})
                logger.debug(f"Board query cache miss for {board_key[0]}/project#{board_key[1]}, cached result")

        all_results.update(results)
        all_errors.update(errors)

    return all_results, all_errors


# ---------------------------------------------------------------------------
# Board query cache for deduplicating identical GraphQL board queries.
#
# Backs BOTH execute_board_query_cached() (single-board queries) and
# execute_batched_board_queries() (aliased multi-board queries) above - one
# cache, one lock, one TTL, shared via the _get_cached_board()/_cache_board()
# helpers below, so a board fetched via either path is visible as a cache
# hit to the other within the TTL window. invalidate_board_query_cache()
# deletes directly from this same dict, so it correctly invalidates data
# regardless of which path originally populated it.
# ---------------------------------------------------------------------------
import threading
_board_query_cache: Dict[tuple, tuple] = {}  # (owner, project_number) -> (timestamp, data)
_board_query_cache_lock = threading.Lock()
_board_query_cache_ttl = 15  # seconds


def _get_cached_board(cache_key: Tuple[str, int]) -> Optional[dict]:
    """
    Thread-safe cache read for a single (owner, project_number) key, shared
    by execute_board_query_cached() and execute_batched_board_queries().

    Returns the cached data - the same envelope shape
    execute_board_query_cached() has always returned, e.g.
    {'user': {'projectV2': {...}}} or {'organization': {'projectV2': {...}}}
    - if there's a fresh (within _board_query_cache_ttl) entry, else None.
    """
    now = time.time()
    with _board_query_cache_lock:
        if cache_key in _board_query_cache:
            cached_time, cached_data = _board_query_cache[cache_key]
            if now - cached_time < _board_query_cache_ttl:
                return cached_data
    return None


def _cache_board(cache_key: Tuple[str, int], data: dict) -> None:
    """
    Thread-safe cache write for a single (owner, project_number) key, shared
    by execute_board_query_cached() and execute_batched_board_queries().

    `data` must be in the same envelope shape execute_board_query_cached()
    has always stored/returned (e.g. {'user': {'projectV2': {...}}}) so a
    read through either path returns an equivalent value.
    """
    with _board_query_cache_lock:
        _board_query_cache[cache_key] = (time.time(), data)


def _unwrap_board_envelope(data: dict) -> Optional[dict]:
    """
    Extract the projectV2 payload from the cache's envelope shape (e.g.
    {'user': {'projectV2': {...}}}), matching what
    parse_batched_projects_v2_response()/execute_batched_board_queries()
    return directly for a freshly-fetched board.

    Returns None if `data` isn't in the expected shape.
    """
    for root_field in ('user', 'organization'):
        root = data.get(root_field)
        if isinstance(root, dict) and 'projectV2' in root:
            return root['projectV2']
    return None


def execute_board_query_cached(owner: str, project_number: int) -> Optional[dict]:
    """
    Execute a board query with short-TTL caching to deduplicate identical queries.

    Multiple callers (ProjectMonitor.get_project_items, PipelineQueueManager.get_issues_in_column_order)
    execute the same GraphQL board query. This function ensures only one actual API call is made
    per board per TTL window.

    Thin wrapper over the shared _board_query_cache: cache reads/writes go
    through _get_cached_board()/_cache_board(), the same helpers
    execute_batched_board_queries() uses, so this is one unified cache
    rather than two parallel ones - a board cached via either path is a hit
    for the other.

    Includes a single retry on transient failures.

    Args:
        owner: GitHub owner login
        project_number: Project number

    Returns:
        Raw GraphQL response data dict, or None on failure
    """
    cache_key = (owner, project_number)

    # Check cache (thread-safe)
    cached_data = _get_cached_board(cache_key)
    if cached_data is not None:
        logger.debug(f"Board query cache hit for {owner}/project#{project_number}")
        return cached_data

    # Cache miss — execute query
    query = build_projects_v2_query(owner, project_number)
    if query is None:
        return None

    from services.github_api_client import get_github_client
    github_client = get_github_client()

    # Single retry on transient failure
    for attempt in range(2):
        success, data = github_client.graphql(query)
        if success:
            _cache_board(cache_key, data)
            logger.debug(f"Board query cache miss for {owner}/project#{project_number}, cached result")
            return data

        if attempt == 0:
            logger.debug(f"Board query failed for {owner}/project#{project_number}, retrying once...")
            time.sleep(2)

    logger.warning(f"Board query failed for {owner}/project#{project_number} after retry: {data}")
    return None


def invalidate_board_query_cache(owner: str, project_number: int):
    """
    Invalidate cached board query to force fresh fetch.

    Used by status validation retry logic to bypass stale cache
    when GitHub returns invalid/transient status values.
    """
    cache_key = (owner, project_number)
    with _board_query_cache_lock:
        if cache_key in _board_query_cache:
            del _board_query_cache[cache_key]
            logger.debug(f"Invalidated board query cache for {owner}/project#{project_number}")


@retry_on_timeout()
def get_projects_list_for_owner(owner_login: str) -> Optional[list]:
    """
    Get list of projects for a GitHub owner (user or organization).
    Uses Redis cache with 5-minute TTL to reduce API calls.
    Protected by circuit breaker to prevent API overload.

    Args:
        owner_login: GitHub username or organization name

    Returns:
        List of projects, or None if unable to fetch
    """
    # Check cache first (bypass circuit breaker for cached values)
    cache_key = f"github:projects_list:{owner_login}"
    cached_value = _github_cache.get(cache_key)

    if cached_value:
        try:
            logger.debug(f"Projects list for '{owner_login}' from cache")
            return json.loads(cached_value)
        except json.JSONDecodeError:
            logger.warning(f"Failed to decode cached projects list for '{owner_login}'")

    # Check circuit breaker before making API call
    try:
        _check_circuit_breaker()
    except CircuitBreakerOpen as e:
        logger.error(f"Cannot list projects for '{owner_login}': {e}")
        return None

    owner_type = get_owner_type(owner_login)

    if owner_type is None:
        logger.error(f"Cannot list projects - unable to determine owner type for '{owner_login}'")
        return None

    try:
        # For users, use GraphQL to list projects
        if owner_type == 'user':
            query = f'''{{
                user(login: "{owner_login}") {{
                    projectsV2(first: 100) {{
                        nodes {{
                            id
                            number
                            title
                            url
                        }}
                    }}
                }}
            }}'''
            
            result = subprocess.run(
                ['gh', 'api', 'graphql', '-f', f'query={query}'],
                capture_output=True,
                text=True,
                timeout=30,
                check=True
            )

            data = json.loads(result.stdout)
            projects = data.get('data', {}).get('user', {}).get('projectsV2', {}).get('nodes', [])

            # Cache the result with 5-minute TTL
            _github_cache.set(cache_key, json.dumps(projects), ttl_seconds=300)
            _record_success()  # Record success for circuit breaker
            return projects
            
        else:  # organization
            query = f'''{{
                organization(login: "{owner_login}") {{
                    projectsV2(first: 100) {{
                        nodes {{
                            id
                            number
                            title
                            url
                        }}
                    }}
                }}
            }}'''
            
            result = subprocess.run(
                ['gh', 'api', 'graphql', '-f', f'query={query}'],
                capture_output=True,
                text=True,
                timeout=30,
                check=True
            )

            data = json.loads(result.stdout)
            projects = data.get('data', {}).get('organization', {}).get('projectsV2', {}).get('nodes', [])

            # Cache the result with 5-minute TTL
            _github_cache.set(cache_key, json.dumps(projects), ttl_seconds=300)
            _record_success()  # Record success for circuit breaker
            return projects

    except subprocess.CalledProcessError as e:
        # Don't count rate limits as failures for circuit breaker
        if not _is_rate_limited(e.stderr):
            _record_failure()  # Record failure for circuit breaker
        logger.error(f"Failed to list projects for '{owner_login}': {e.stderr}")
        return None
    except subprocess.TimeoutExpired as e:
        _record_failure()  # Record timeout as failure
        logger.error(f"Timeout listing projects for '{owner_login}': {e}")
        return None
    except Exception as e:
        logger.error(f"Error listing projects for '{owner_login}': {e}")
        return None
