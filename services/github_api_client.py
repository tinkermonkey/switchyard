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
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple, List
from collections import deque
from threading import Lock, Thread

logger = logging.getLogger(__name__)

# Enable call stack tracing (set TRACE_GITHUB_API_CALLS=true to see where calls come from)
#TRACE_API_CALLS = os.environ.get('TRACE_GITHUB_API_CALLS', 'false').lower() == 'true'
TRACE_API_CALLS = True


class GitHubRateLimitStatus:
    """Track GitHub API rate limit status and remaining quota."""
    
    def __init__(self):
        self.limit = 5000  # Points per hour for authenticated user
        self.remaining = 5000
        self.reset_time: Optional[datetime] = None
        self.last_updated = datetime.now()
        self.resource_type = "graphql"  # or "rest"
        
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
        except Exception as e:
            logger.error(f"Error parsing rate limit headers: {e}")
    
    def get_percentage_used(self) -> float:
        """Get percentage of rate limit used (0-100)."""
        if self.limit == 0:
            return 0
        return ((self.limit - self.remaining) / self.limit) * 100
    
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
        self.rate_limit = GitHubRateLimitStatus()
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
        
        logger.info("GitHub API client initialized with rate limiting")
        
        # Fetch initial rate limit from GitHub API (async - don't block initialization)
        self._fetch_initial_rate_limit()
        
        # Start background rate limit checker thread (runs every 5 minutes)
        self._start_rate_limit_checker()
    
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
        usage_percent = self.rate_limit.get_percentage_used()
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
        cmd = ['gh', 'api', 'graphql']
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
            
            # Check for rate limit error
            if result.returncode == 1:
                if 'rate limit' in result.stdout.lower() or 'rate limit' in result.stderr.lower():
                    self.rate_limited_requests += 1
                    logger.error("🔴 GitHub API rate limit hit")
                    
                    # Extract reset time if possible
                    reset_time = self._extract_reset_time(result.stdout, result.stderr)
                    self.breaker.trip(reset_time)
                    
                    return False, {"error": "rate_limited", "details": result.stdout}
                
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
                response = json.loads(result.stdout)
                
                # Update rate limit from response
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
                return False, {"error": "parse_error", "raw_output": result.stdout}
        
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
            method: HTTP method ('GET', 'POST', 'PATCH', 'DELETE')
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
        
        # Check usage and apply throttling
        usage_percent = self.rate_limit.get_percentage_used()
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
        
        # For REST API calls with data (POST/PATCH/DELETE), use http_request instead
        # as gh api doesn't handle JSON bodies well
        if data and method.upper() in ['POST', 'PATCH', 'DELETE']:
            # Use HTTP request instead
            url = f"https://api.github.com{endpoint}"
            return self.http_request(method, url, data, retries=retries)
        
        # Build gh CLI command for GET and other methods without body
        # gh api syntax: gh api [-X METHOD] ENDPOINT
        # Only include -X if method is not GET (GET is the default)
        cmd = ['gh', 'api']
        if method.upper() != 'GET':
            cmd.extend(['-X', method.upper()])
        cmd.append(endpoint)
        
        try:
            logger.debug(f"Executing REST {method} {endpoint} (usage: {usage_percent:.1f}%)")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            self.total_requests += 1
            self._record_request('rest', True)
            
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
                response = json.loads(result.stdout)
                self.backoff_multiplier = 1.0
                
                # Track the operation
                self.track_gh_operation('rest_api', f'REST {method} {endpoint} executed successfully')
                
                return True, response
            except json.JSONDecodeError:
                # Some endpoints return empty responses
                if result.stdout.strip() == '':
                    # Track empty response success
                    self.track_gh_operation('rest_api', f'REST {method} {endpoint} executed successfully (empty response)')
                    return True, {}
                logger.error(f"Failed to parse REST response: {result.stdout}")
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
            method: HTTP method ('GET', 'POST', 'PATCH', 'DELETE')
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
        
        # Check usage and apply throttling
        usage_percent = self.rate_limit.get_percentage_used()
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
        """Extract rate limit info from GraphQL response.
        
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
                self.rate_limit.remaining = rl.get('remaining', self.rate_limit.remaining)
                self.rate_limit.limit = rl.get('limit', self.rate_limit.limit)
                reset_at = rl.get('resetAt')
                if reset_at:
                    # Parse ISO format datetime
                    self.rate_limit.reset_time = datetime.fromisoformat(reset_at.replace('Z', '+00:00'))
                self.rate_limit.resource_type = "graphql"
                self.rate_limit.last_updated = datetime.now()
                
                logger.debug(
                    f"Rate limit update: {self.rate_limit.remaining}/{self.rate_limit.limit} "
                    f"({self.rate_limit.get_percentage_used():.1f}% used)"
                )
        except Exception as e:
            logger.debug(f"Could not extract rate limit from response: {e}")
    
    def _extract_reset_time(self, stdout: str, stderr: str) -> Optional[datetime]:
        """Try to extract rate limit reset time from error response."""
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
    
    def _update_rate_limit_from_http_headers(self, headers: Dict[str, str]):
        """Extract rate limit info from HTTP response headers."""
        try:
            if 'x-ratelimit-limit' in headers:
                self.rate_limit.limit = int(headers['x-ratelimit-limit'])
            if 'x-ratelimit-remaining' in headers:
                self.rate_limit.remaining = int(headers['x-ratelimit-remaining'])
            if 'x-ratelimit-reset' in headers:
                reset_timestamp = int(headers['x-ratelimit-reset'])
                self.rate_limit.reset_time = datetime.fromtimestamp(reset_timestamp)
            if 'x-ratelimit-resource' in headers:
                self.rate_limit.resource_type = headers['x-ratelimit-resource']
            
            self.rate_limit.last_updated = datetime.now()
            
            logger.debug(
                f"Rate limit update (HTTP): {self.rate_limit.remaining}/{self.rate_limit.limit} "
                f"({self.rate_limit.get_percentage_used():.1f}% used)"
            )
        except Exception as e:
            logger.debug(f"Could not extract rate limit from HTTP headers: {e}")
    
    def get_status(self) -> dict:
        """Get current API client status."""
        return {
            'rate_limit': self.rate_limit.to_dict(),
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
    
    def alarm_if_needed(self):
        """Check if we should alarm based on rate limit usage."""
        usage = self.rate_limit.get_percentage_used()
        remaining = self.rate_limit.remaining
        
        if remaining <= 100:
            logger.critical(
                f"🚨 CRITICAL: GitHub API rate limit critically low! "
                f"Only {remaining} points remaining ({usage:.1f}% used)"
            )
        elif remaining <= 250:
            logger.error(
                f"🔴 WARNING: GitHub API rate limit low! "
                f"Only {remaining} points remaining ({usage:.1f}% used)"
            )
        elif usage >= 95:
            logger.warning(
                f"⚠️  GitHub API usage at 95%+: {self.rate_limit.remaining} points remaining"
            )
        elif usage >= 90:
            logger.warning(
                f"⚠️  GitHub API usage at 90%: {self.rate_limit.remaining} points remaining"
            )
        elif usage >= 80:
            logger.warning(
                f"ℹ️  GitHub API usage at 80%: {self.rate_limit.remaining} points remaining"
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
    
    def _fetch_rate_limit_from_api(self) -> bool:
        """
        Fetch current rate limit information from GitHub API.
        
        This method queries the GitHub API for rate limit status and updates
        the internal rate_limit object with the latest values.
        
        Returns:
            bool: True if rate limit was successfully fetched and updated, False otherwise
        """
        try:
            # Query rate limit from GitHub API
            query = """
            {
                rateLimit {
                    limit
                    remaining
                    resetAt
                }
            }
            """
            
            success, response = self.graphql(query)
            
            if success and 'rateLimit' in response:
                rl = response['rateLimit']
                
                # Update rate limit state variables
                self.rate_limit.limit = rl.get('limit', 5000)
                self.rate_limit.remaining = rl.get('remaining', 0)
                
                # Parse reset time
                reset_at = rl.get('resetAt')
                if reset_at:
                    # Parse ISO format datetime
                    self.rate_limit.reset_time = datetime.fromisoformat(reset_at.replace('Z', '+00:00'))
                
                self.rate_limit.resource_type = "graphql"
                self.rate_limit.last_updated = datetime.now()
                
                # Calculate usage percentage
                percentage_used = self.rate_limit.get_percentage_used()
                
                logger.debug(
                    f"📊 GitHub API Rate Limit: {self.rate_limit.remaining}/{self.rate_limit.limit} remaining "
                    f"({percentage_used:.1f}% used)"
                )
                
                # Check and alarm if needed
                self.alarm_if_needed()
                
                return True
            else:
                logger.debug("Failed to fetch rate limit from GitHub API")
                return False
                
        except Exception as e:
            logger.debug(f"Error fetching rate limit from API: {e}")
            return False
    
    def _fetch_initial_rate_limit(self):
        """
        Fetch rate limit on startup in background thread.
        
        This ensures the rate limit is populated with actual GitHub API data
        rather than default values, so the /health endpoint shows accurate data
        from the start.
        """
        def fetch_on_startup():
            """Background thread to fetch initial rate limit"""
            import time
            time.sleep(5)  # Wait 5 seconds after startup to let services initialize
            
            if self._fetch_rate_limit_from_api():
                logger.info(
                    f"📊 GitHub API Rate Limit Status: {self.rate_limit.remaining}/{self.rate_limit.limit} remaining "
                    f"({self.rate_limit.get_percentage_used():.1f}% used)"
                )
            else:
                logger.debug("Failed to fetch initial rate limit on startup")
        
        # Start thread as daemon so it doesn't block initialization
        thread = Thread(target=fetch_on_startup, daemon=True)
        thread.start()
    
    def _start_rate_limit_checker(self):
        """Start background thread to check rate limits every 5 minutes"""
        def check_rate_limits():
            """Background thread that checks rate limits periodically"""
            import time
            while True:
                try:
                    time.sleep(300)  # Check every 5 minutes
                    
                    if self._fetch_rate_limit_from_api():
                        logger.info(
                            f"📊 GitHub API Rate Limit Check: {self.rate_limit.remaining}/{self.rate_limit.limit} remaining "
                            f"({self.rate_limit.get_percentage_used():.1f}% used)"
                        )
                    
                    # Summarize and clean up call trace buffer
                    self._summarize_and_cleanup_call_traces()
                    
                except Exception as e:
                    logger.debug(f"Error in rate limit checker: {e}")
        
        # Start thread as daemon so it doesn't block shutdown
        thread = Thread(target=check_rate_limits, daemon=True)
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

