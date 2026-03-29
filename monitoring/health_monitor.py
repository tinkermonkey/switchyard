import psutil
import subprocess
import requests
import logging
import asyncio
from typing import Dict, Any, List
from datetime import datetime
from config.environment import Environment
from services.circuit_breaker import CircuitBreaker, CircuitBreakerOpen

logger = logging.getLogger(__name__)

class HealthMonitor:
    """Monitor system health and trigger recovery"""

    # Class-level variable to store last health check result (accessible by observability server)
    last_health_check = None

    # Cache GitHub authentication check (username lookup doesn't change frequently)
    _github_auth_cache = None
    _github_auth_cache_time = None
    _github_auth_cache_ttl = 1800  # 30 minutes

    # Circuit breaker for GitHub health checks
    _github_health_circuit_breaker = CircuitBreaker(
        name="github_health_checks",
        failure_threshold=3,
        recovery_timeout=60,
        expected_exception=subprocess.CalledProcessError
    )

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
        Run GitHub API call with both retry logic and circuit breaker protection.
        Checks circuit breaker before making call, then runs with retry logic.
        """
        # Define the function to call through circuit breaker
        async def make_call():
            return await self._run_subprocess_with_retry(cmd, timeout, retries, description)

        # Use circuit breaker to protect the call
        try:
            return await HealthMonitor._github_health_circuit_breaker.call(make_call)
        except CircuitBreakerOpen as e:
            logger.warning(f"GitHub health check circuit breaker open: {e}")
            raise

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
        try:
            auth_result = await self._github_api_call_with_circuit_breaker(
                ['gh', 'api', 'user', '--jq', '.login'],
                timeout=30,
                retries=2,
                description="GitHub PAT authentication check"
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, CircuitBreakerOpen) as e:
            auth_result = subprocess.CompletedProcess(
                ['gh', 'api', 'user', '--jq', '.login'],
                returncode=1,
                stdout='',
                stderr=str(e)
            )

        pat_status = {
            'authenticated': auth_result.returncode == 0
        }

        if auth_result.returncode != 0:
            return {
                'healthy': False,
                'error': f'GitHub PAT authentication failed: {auth_result.stderr}',
                'auth_methods': {
                    'pat': pat_status,
                    'github_app': github_app_status
                },
                'critical': 'At least PAT authentication is required for orchestrator to function'
            }

        # Check if we can access user info
        try:
            user_result = await self._github_api_call_with_circuit_breaker(
                ['gh', 'api', 'user'],
                timeout=30,
                retries=2,
                description="GitHub user info access check"
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, CircuitBreakerOpen) as e:
            user_result = subprocess.CompletedProcess(
                ['gh', 'api', 'user'],
                returncode=1,
                stdout='',
                stderr=str(e)
            )

        if user_result.returncode != 0:
            return {
                'healthy': False,
                'error': f'GitHub API access failed: {user_result.stderr}',
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
        try:
            repo_result = await self._github_api_call_with_circuit_breaker(
                ['gh', 'api', f'repos/{org}/{repo}'],
                timeout=30,
                retries=2,
                description=f"GitHub repo access check for {org}/{repo}"
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, CircuitBreakerOpen) as e:
            repo_result = subprocess.CompletedProcess(
                ['gh', 'api', f'repos/{org}/{repo}'],
                returncode=1,
                stdout='',
                stderr=str(e)
            )

        if repo_result.returncode != 0:
            return {
                'healthy': False,
                'error': f'Repository access failed for {org}/{repo}: {repo_result.stderr}',
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
        from services.github_api_client import get_github_client
        try:
            github_client = get_github_client()
            client_status = github_client.get_status()
            
            rate_limit_info = {
                'remaining': client_status['rate_limit']['remaining'],
                'limit': client_status['rate_limit']['limit'],
                'percentage_used': client_status['rate_limit']['percentage_used'],
                'reset_time': client_status['rate_limit']['reset_time'],
            }
            
            circuit_breaker_info = {
                'state': client_status['breaker']['state'],
                'is_open': client_status['breaker']['is_open'],
                'opened_at': client_status['breaker']['opened_at'],
                'reset_time': client_status['breaker']['reset_time'],
            }
            
            # Check if rate limit is critically low
            if client_status['rate_limit']['percentage_used'] > 95:
                degraded = True
        except Exception as e:
            logger.debug(f"Failed to get GitHub API client status: {e}")
            rate_limit_info = None
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