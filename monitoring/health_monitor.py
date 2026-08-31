import psutil
import subprocess
import requests
import logging
import asyncio
from typing import Dict, Any, List
from datetime import datetime
from config.environment import Environment
from services.circuit_breaker import CircuitBreakerOpen
from services.github_api_client import get_github_client

logger = logging.getLogger(__name__)

class HealthMonitor:
    """Monitor system health and trigger recovery"""

    # Class-level variable to store last health check result (accessible by observability server)
    last_health_check = None

    # Cache GitHub authentication check (username lookup doesn't change frequently)
    _github_auth_cache = None
    _github_auth_cache_time = None
    _github_auth_cache_ttl = 1800  # 30 minutes

    # NOTE: there used to be a second, independent CircuitBreaker here
    # (_github_health_circuit_breaker) guarding only this health check's own
    # GitHub probe calls, disconnected from the real breaker
    # (services/github_api_client.py's GitHubAPIClient.breaker) the rest of
    # the system actually uses for GitHub API traffic. Removed - see
    # _github_api_call_with_circuit_breaker()'s docstring for why having two
    # independent breakers for "is GitHub usable" was actively misleading.

    def __init__(self, orchestrator=None):
        self.orchestrator = orchestrator
        self.env = Environment()  # Load environment config
        self.health_checks = {
            'github': self.check_github,
            'claude': self.check_claude,
            'disk': self.check_disk_space,
            'memory': self.check_memory,
        }
        
    async def check_health(self) -> Dict[str, Any]:
        """Run all health checks"""
        import logging
        logging.getLogger("orchestrator").info("⚕️  Running health check")
        results = {}

        for name, check in self.health_checks.items():
            try:
                results[name] = await check()
            except Exception as e:
                results[name] = {
                    'healthy': False,
                    'error': str(e)
                }

        overall_health = all(r.get('healthy', False) for r in results.values())

        # Check if any subsystem is degraded
        degraded = any(r.get('degraded', False) for r in results.values())

        health_result = {
            'healthy': overall_health,
            'degraded': degraded,
            'checks': results,
            'timestamp': datetime.now().isoformat()
        }

        # Store result in class variable for observability server to access
        HealthMonitor.last_health_check = health_result

        # Also store in Redis for cross-process access
        try:
            import redis
            import json
            redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
            redis_client.setex(
                'orchestrator:health',
                600,  # 10 minute TTL (health check max backoff is 5 minutes)
                json.dumps(health_result)
            )
            logging.getLogger("orchestrator").info(f"✓ Health check complete: healthy={health_result['healthy']}, stored in Redis")
        except Exception as e:
            # Log but don't fail health check if Redis is unavailable
            import logging
            logging.getLogger(__name__).warning(f"Failed to store health check in Redis: {e}")

        return health_result

    @staticmethod
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

    async def _run_subprocess_with_retry(
        self,
        cmd: List[str],
        timeout: int = 30,
        retries: int = 2,
        description: str = "command"
    ) -> subprocess.CompletedProcess:
        """Run subprocess with retry logic for transient failures and rate limits."""
        last_exception = None

        for attempt in range(retries + 1):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=timeout, check=True
                )
                if attempt > 0:
                    logger.info(f"{description} succeeded on attempt {attempt + 1}")
                return result

            except subprocess.TimeoutExpired as e:
                last_exception = e
                if attempt < retries:
                    wait = 2 ** attempt  # 1s, 2s
                    logger.warning(f"{description} timed out, retrying in {wait}s...")
                    await asyncio.sleep(wait)
                else:
                    raise

            except subprocess.CalledProcessError as e:
                # Don't retry auth errors
                if 'authentication' in e.stderr.lower():
                    raise

                # Handle rate limiting with longer backoff
                if self._is_rate_limited(e.stderr):
                    wait = 60 * (2 ** attempt)  # 60s, 120s, 240s
                    logger.warning(
                        f"{description} hit GitHub rate limit. "
                        f"Backing off for {wait}s (attempt {attempt + 1}/{retries + 1})"
                    )
                    if attempt < retries:
                        await asyncio.sleep(wait)
                    else:
                        logger.error(f"{description} rate limited after {retries + 1} attempts")
                        raise
                elif attempt < retries:
                    wait = 2 ** attempt
                    logger.warning(f"{description} failed, retrying in {wait}s...")
                    await asyncio.sleep(wait)
                else:
                    raise

        raise last_exception

    async def _github_api_call_with_circuit_breaker(
        self,
        cmd: List[str],
        timeout: int = 30,
        retries: int = 2,
        description: str = "command"
    ) -> subprocess.CompletedProcess:
        """
        Run GitHub API call with retry logic, gated by the SAME circuit
        breaker the real GitHub API traffic (GraphQL polling, dispatch,
        etc. - services/github_api_client.py's GitHubAPIClient.breaker)
        uses, instead of a separate, independent breaker.

        This used to guard these calls with their own CircuitBreaker
        instance (_github_health_circuit_breaker), disconnected from the
        real one. The two could - and during a real production incident,
        did - completely disagree: this health check's own probe calls
        (gh api user / gh api repos/...) are cheap REST calls that can
        keep succeeding even while the real breaker is open due to
        GraphQL exhaustion (REST and GraphQL are separate GitHub
        rate-limit buckets), so the dashboard kept reporting "no open
        circuit breakers" and a healthy rate limit while the actual
        polling/dispatch breaker was open and blocking all real work.
        Checking the real breaker here means this health check now
        reflects (and short-circuits consistently with) the same "is
        GitHub actually usable right now" signal the rest of the system
        acts on.
        """
        github_client = get_github_client()
        # Check for recovery first, matching every real call site in
        # services/github_api_client.py (graphql()/rest()/http_request()/
        # gh_cli() all call check_and_close() before is_open()) - is_open()
        # alone is a pure state read and won't itself flip OPEN -> HALF_OPEN
        # once reset_time has passed, which would otherwise leave this
        # health check reporting a stale "circuit open" for however long it
        # takes some unrelated real GitHub traffic to happen to trigger the
        # transition first.
        github_client.breaker.check_and_close()
        if github_client.breaker.is_open():
            raise CircuitBreakerOpen(
                "GitHub API circuit breaker is open (shared with GraphQL/REST polling) - "
                f"skipping health-check probe: {description}"
            )

        return await self._run_subprocess_with_retry(cmd, timeout, retries, description)

    async def _github_probe(
        self,
        cmd: List[str],
        description: str,
        timeout: int = 30,
        retries: int = 2,
    ) -> "tuple[subprocess.CompletedProcess, bool]":
        """
        Run one of check_github()'s gh CLI probes and classify the outcome,
        collapsing the repeated "call it, catch CircuitBreakerOpen
        separately from other failures, synthesize a failed
        CompletedProcess either way" pattern that used to be hand-copied
        at each of the three call sites in check_github() (PAT auth, user
        info, repo access). Keeping that logic in one place matters here
        specifically: a future fix to the breaker-open handling applied to
        only one of three copies would silently reintroduce the
        exit(1)-on-a-normal-rate-limit-window bug this same change fixed
        (see main.py's is_transient check and the 'transient' flag below).

        Returns:
            (result, breaker_open) - result.returncode == 0 on success;
            breaker_open is True only when the failure was specifically the
            shared circuit breaker being open (as opposed to a genuine
            auth/network/timeout failure), so callers can build an accurate,
            non-misleading error message and set 'transient' correctly.
        """
        try:
            result = await self._github_api_call_with_circuit_breaker(
                cmd, timeout=timeout, retries=retries, description=description
            )
            return result, False
        except CircuitBreakerOpen as e:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout='', stderr=str(e)), True
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout='', stderr=str(e)), False

    @staticmethod
    def _github_probe_error(breaker_open: bool, breaker_context: str, normal_error: str) -> str:
        """
        Build check_github()'s error message for a failed probe. A
        shared-breaker-open condition is not actually an auth/access
        failure - it's the same rate-limit/outage state the rest of the
        system already tolerates and self-recovers from. Distinguishing it
        here matters for two reasons: it stops an on-call reader from
        chasing a nonexistent auth problem, and main.py's health-check loop
        needs this to end up reflected in the 'transient' flag - the
        breaker's own error message doesn't contain any of the keywords
        main.py's fallback classifier looks for, so without this a normal,
        self-recovering breaker-open window gets treated as a persistent
        failure and can exit(1) the whole orchestrator after enough
        consecutive health-check cycles.
        """
        if breaker_open:
            return (
                'GitHub API circuit breaker is open (shared with real GraphQL/REST '
                f'traffic) - not {breaker_context}, will self-recover'
            )
        return normal_error

    async def check_github(self) -> Dict[str, Any]:
        """Check GitHub connectivity and project management permissions"""
        import json
        from services.github_capabilities import github_capabilities, GitHubCapability
        import time

        # Check cache first
        cache_valid = (
            HealthMonitor._github_auth_cache is not None and
            HealthMonitor._github_auth_cache_time is not None and
            time.time() - HealthMonitor._github_auth_cache_time < HealthMonitor._github_auth_cache_ttl
        )
        
        if cache_valid:
            # Return cached result
            cached_result = HealthMonitor._github_auth_cache.copy()
            cached_result['cached'] = True
            cached_result['cache_age_seconds'] = int(time.time() - HealthMonitor._github_auth_cache_time)
            return cached_result

        # Check all capabilities
        capability_status = github_capabilities.check_capabilities()

        # Check GitHub App authentication status for detailed reporting
        from services.github_app import github_app
        import subprocess

        github_app_status = {
            'enabled': github_app.enabled
        }

        if github_app.enabled:
            # Try to get installation token to verify it works
            token = github_app.get_installation_token()
            github_app_status['working'] = token is not None
            if not token:
                github_app_status['reason'] = 'Failed to get installation token'
        else:
            github_app_status['working'] = False
            github_app_status['reason'] = 'Not configured (missing app_id, installation_id, or private_key)'

        # Check PAT authentication via gh CLI
        # Note: gh auth status returns error if token is set via GITHUB_TOKEN env var
        # instead of gh auth login, so we test actual API functionality instead
        auth_result, auth_breaker_open = await self._github_probe(
            ['gh', 'api', 'user', '--jq', '.login'],
            description="GitHub PAT authentication check"
        )

        pat_status = {
            'authenticated': auth_result.returncode == 0
        }

        if auth_result.returncode != 0:
            return {
                'healthy': False,
                'error': self._github_probe_error(
                    auth_breaker_open, 'a PAT authentication problem',
                    f'GitHub PAT authentication failed: {auth_result.stderr}'
                ),
                'transient': auth_breaker_open,
                'auth_methods': {
                    'pat': pat_status,
                    'github_app': github_app_status
                },
                'critical': None if auth_breaker_open else 'At least PAT authentication is required for orchestrator to function'
            }

        # Check if we can access user info
        user_result, user_breaker_open = await self._github_probe(
            ['gh', 'api', 'user'],
            description="GitHub user info access check"
        )

        if user_result.returncode != 0:
            return {
                'healthy': False,
                'error': self._github_probe_error(
                    user_breaker_open, 'an access-permissions problem',
                    f'GitHub API access failed: {user_result.stderr}'
                ),
                'transient': user_breaker_open,
                'auth_methods': {
                    'pat': {'authenticated': False},
                    'github_app': github_app_status
                }
            }

        # Load projects from new config system
        from config.manager import ConfigManager
        try:
            config_manager = ConfigManager()
            projects = config_manager.list_projects()

            if not projects:
                return {
                    'healthy': True,
                    'auth_status': 'authenticated',
                    'warning': 'No projects configured to test'
                }

            # Test with first configured project
            project_name = projects[0]
            project_config = config_manager.get_project_config(project_name)
            org = project_config.github['org']
            repo = project_config.github['repo']

        except Exception as e:
            return {
                'healthy': False,
                'error': f'Failed to load project configuration: {e}',
                'config_error': True
            }

        # Check repository access
        repo_result, repo_breaker_open = await self._github_probe(
            ['gh', 'api', f'repos/{org}/{repo}'],
            description=f"GitHub repo access check for {org}/{repo}"
        )

        if repo_result.returncode != 0:
            return {
                'healthy': False,
                'error': self._github_probe_error(
                    repo_breaker_open, 'a repo-permissions problem',
                    f'Repository access failed for {org}/{repo}: {repo_result.stderr}'
                ),
                'transient': repo_breaker_open,
                'auth_methods': {
                    'pat': {'authenticated': True, 'repo_access': False},
                    'github_app': github_app_status
                },
                'repo_access': 'failed'
            }

        # Test GitHub Projects v2 permissions
        # This is the orchestrator's primary function
        from services.github_owner_utils import get_projects_list_for_owner
        
        projects_list = get_projects_list_for_owner(org)
        
        if projects_list is None:
            return {
                'healthy': False,
                'error': f'GitHub Projects access failed: unable to list projects for {org}',
                'auth_methods': {
                    'pat': {'authenticated': True, 'repo_access': True, 'projects_access': False},
                    'github_app': github_app_status
                },
                'repo_access': 'granted',
                'projects_access': 'failed',
                'critical': 'GitHub Projects v2 access is required for orchestrator to function'
            }

        # Determine if we have degraded functionality
        degraded = not github_capabilities.has_capability(GitHubCapability.GITHUB_APP_AUTH)

        # Get GitHub API rate limit and circuit breaker status
        # Note: The rate limit data includes default values (5000/5000) until the background
        # rate limit checker first runs (every 5 minutes). The /health endpoint will fetch
        # fresh rate limit data to avoid returning stale cached values.
        try:
            github_client = get_github_client()
            client_status = github_client.get_status()
            
            # 'api_rate_limit' is kept for backward compat and mirrors the
            # GraphQL bucket (see GitHubAPIClient.__init__); GraphQL and
            # REST are separate GitHub rate-limit buckets (issue #103), so
            # 'api_rate_limit_graphql' / 'api_rate_limit_rest' below are the
            # ones that should be read going forward.
            rate_limit_info = {
                'remaining': client_status['rate_limit']['remaining'],
                'limit': client_status['rate_limit']['limit'],
                'percentage_used': client_status['rate_limit']['percentage_used'],
                'reset_time': client_status['rate_limit']['reset_time'],
            }
            rate_limit_graphql_info = {
                'remaining': client_status['rate_limit_graphql']['remaining'],
                'limit': client_status['rate_limit_graphql']['limit'],
                'percentage_used': client_status['rate_limit_graphql']['percentage_used'],
                'reset_time': client_status['rate_limit_graphql']['reset_time'],
            }
            rate_limit_rest_info = {
                'remaining': client_status['rate_limit_rest']['remaining'],
                'limit': client_status['rate_limit_rest']['limit'],
                'percentage_used': client_status['rate_limit_rest']['percentage_used'],
                'reset_time': client_status['rate_limit_rest']['reset_time'],
            }
            
            circuit_breaker_info = {
                'state': client_status['breaker']['state'],
                'is_open': client_status['breaker']['is_open'],
                'opened_at': client_status['breaker']['opened_at'],
                'reset_time': client_status['breaker']['reset_time'],
            }
            
            # Check if either bucket is critically low
            if (client_status['rate_limit_graphql']['percentage_used'] > 95
                    or client_status['rate_limit_rest']['percentage_used'] > 95):
                degraded = True
        except Exception as e:
            logger.debug(f"Failed to get GitHub API client status: {e}")
            rate_limit_info = None
            rate_limit_graphql_info = None
            rate_limit_rest_info = None
            circuit_breaker_info = None

        result = {
            'healthy': True,  # Core functionality works with PAT
            'degraded': degraded,  # Some features unavailable
            'auth_methods': {
                'pat': pat_status,
                'github_app': github_app_status
            },
            'capabilities': capability_status['capabilities'],
            'warnings': capability_status['warnings'],
            'repo_access': 'granted',
            'projects_access': 'granted',
            'tested_org': org,
            'tested_repo': f'{org}/{repo}',
            'api_rate_limit': rate_limit_info,
            'api_rate_limit_graphql': rate_limit_graphql_info,
            'api_rate_limit_rest': rate_limit_rest_info,
            'circuit_breaker': circuit_breaker_info,
        }
        
        # Cache successful result
        HealthMonitor._github_auth_cache = result.copy()
        HealthMonitor._github_auth_cache_time = time.time()
        
        return result
    
    async def check_disk_space(self) -> Dict[str, Any]:
        """Check available disk space"""
        usage = psutil.disk_usage('/')
        healthy = usage.percent < 90  # Alert if >90% full
        
        return {
            'healthy': healthy,
            'usage_percent': usage.percent,
            'free_gb': usage.free / (1024**3)
        }

    async def check_memory(self) -> Dict[str, Any]:
        """Check available memory"""
        memory = psutil.virtual_memory()
        healthy = memory.percent < 85  # Alert if >85% used

        return {
            'healthy': healthy,
            'usage_percent': memory.percent,
            'available_gb': memory.available / (1024**3)
        }

    async def check_claude(self) -> Dict[str, bool]:
        """Check Claude Code CLI accessibility"""
        result = subprocess.run(
            ['claude', '--version'],
            capture_output=True,
            timeout=5
        )
        return {'healthy': result.returncode == 0}