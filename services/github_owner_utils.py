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
from typing import Literal, Optional
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
                    id
                    title
                    items(first: 100, orderBy: {{field: POSITION, direction: ASC}}) {{
                        nodes {{
                            id
                            content {{
                                __typename
                                ... on Issue {{
                                    id
                                    number
                                    title
                                    state
                                    repository {{
                                        name
                                    }}
                                    updatedAt
                                }}
                            }}
                            fieldValues(first: 10) {{
                                nodes {{
                                    ... on ProjectV2ItemFieldSingleSelectValue {{
                                        name
                                        field {{
                                            ... on ProjectV2SingleSelectField {{
                                                name
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}
            }}
        }}'''
    else:  # organization
        query = f'''{{
            organization(login: "{owner_login}") {{
                projectV2(number: {project_number}) {{
                    id
                    title
                    items(first: 100, orderBy: {{field: POSITION, direction: ASC}}) {{
                        nodes {{
                            id
                            content {{
                                __typename
                                ... on Issue {{
                                    id
                                    number
                                    title
                                    state
                                    repository {{
                                        name
                                    }}
                                    updatedAt
                                }}
                            }}
                            fieldValues(first: 10) {{
                                nodes {{
                                    ... on ProjectV2ItemFieldSingleSelectValue {{
                                        name
                                        field {{
                                            ... on ProjectV2SingleSelectField {{
                                                name
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}
            }}
        }}'''
    
    return query


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
