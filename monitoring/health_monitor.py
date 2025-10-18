import psutil
import subprocess
import requests
import logging
from typing import Dict, Any
from datetime import datetime
from config.environment import Environment

logger = logging.getLogger(__name__)

class HealthMonitor:
    """Monitor system health and trigger recovery"""

    # Class-level variable to store last health check result (accessible by observability server)
    last_health_check = None
    
    # Cache GitHub authentication check (username lookup doesn't change frequently)
    _github_auth_cache = None
    _github_auth_cache_time = None
    _github_auth_cache_ttl = 1800  # 30 minutes

    def __init__(self, orchestrator=None):
        self.orchestrator = orchestrator
        self.env = Environment()  # Load environment config
        self.health_checks = {
            'github': self.check_github,
            'claude': self.check_claude,
            'disk': self.check_disk_space,
            'memory': self.check_memory,
            # Disabled: claude_usage requires Claude Code session data which doesn't exist
            # in the orchestrator container. The orchestrator launches agents in separate
            # containers and doesn't maintain its own Claude session history.
            # 'claude_usage': self.check_claude_usage
        }
        
    async def check_health(self) -> Dict[str, Any]:
        """Run all health checks"""
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
        except Exception as e:
            # Log but don't fail health check if Redis is unavailable
            import logging
            logging.getLogger(__name__).warning(f"Failed to store health check in Redis: {e}")

        return health_result
    
    
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
        auth_result = subprocess.run(
            ['gh', 'api', 'user', '--jq', '.login'],
            capture_output=True, text=True,
            timeout=5
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
        user_result = subprocess.run(
            ['gh', 'api', 'user'],
            capture_output=True, text=True,
            timeout=5
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
        repo_result = subprocess.run(
            ['gh', 'api', f'repos/{org}/{repo}'],
            capture_output=True, text=True,
            timeout=5
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

    async def check_claude_usage(self) -> Dict[str, Any]:
        """Check Claude Code usage statistics using ccusage"""
        import json
        import os
        from datetime import datetime, timedelta

        try:
            # Get data for last 7 days
            since_date = (datetime.now() - timedelta(days=7)).strftime('%Y%m%d')

            # Preserve environment but set HOME
            env = os.environ.copy()
            env['HOME'] = '/home/orchestrator'

            result = subprocess.run(
                ['npx', '--yes', 'ccusage', 'daily', '--json', '--since', since_date],
                capture_output=True,
                text=True,
                timeout=30,
                env=env
            )

            if result.returncode != 0:
                return {
                    'healthy': True,  # Usage tracking failure shouldn't mark system unhealthy
                    'available': False,
                    'error': f'ccusage failed: {result.stderr}'
                }

            # Parse JSON output
            usage_data = json.loads(result.stdout)

            # Calculate summary statistics
            total_cost = 0
            total_tokens = 0
            recent_day_cost = 0
            recent_day_tokens = 0
            models_used = set()

            if 'daily' in usage_data and usage_data['daily']:
                for day in usage_data['daily']:
                    total_cost += day.get('totalCost', 0)
                    total_tokens += day.get('totalTokens', 0)
                    models_used.update(day.get('modelsUsed', []))

                # Get most recent day's cost and tokens
                recent_day = usage_data['daily'][-1]
                recent_day_cost = recent_day.get('totalCost', 0)
                recent_day_tokens = recent_day.get('totalTokens', 0)

            # Get billing blocks for session and weekly tracking
            blocks_result = subprocess.run(
                ['npx', '--yes', 'ccusage', 'blocks', '--json'],
                capture_output=True,
                text=True,
                timeout=30,
                env=env
            )

            session_tokens = 0
            session_remaining_minutes = 0
            weekly_tokens = 0

            if blocks_result.returncode == 0:
                blocks_data = json.loads(blocks_result.stdout)
                blocks = blocks_data.get('blocks', [])

                # Find active billing block for session usage
                for block in blocks:
                    if block.get('isActive'):
                        session_tokens = block.get('totalTokens', 0)
                        if 'projection' in block and block['projection']:
                            session_remaining_minutes = block['projection'].get('remainingMinutes', 0)
                        break

                # Calculate weekly usage since last Wednesday 5PM EDT
                from datetime import timezone
                now = datetime.now(timezone.utc)
                # Find last Wednesday 5PM EDT (21:00 UTC)
                days_since_wednesday = (now.weekday() - 2) % 7  # Wednesday is 2
                last_wednesday = now - timedelta(days=days_since_wednesday)
                last_wednesday_5pm = last_wednesday.replace(hour=21, minute=0, second=0, microsecond=0)

                # If we haven't passed Wednesday 5PM this week, use last week's Wednesday
                if now < last_wednesday_5pm:
                    last_wednesday_5pm -= timedelta(days=7)

                # Sum tokens from blocks since last Wednesday 5PM
                for block in blocks:
                    block_end = block.get('actualEndTime')
                    if block_end:
                        block_end_dt = datetime.fromisoformat(block_end.replace('Z', '+00:00'))
                        if block_end_dt >= last_wednesday_5pm:
                            weekly_tokens += block.get('totalTokens', 0)
                    elif block.get('isActive'):
                        # Include active block
                        weekly_tokens += block.get('totalTokens', 0)

            response = {
                'healthy': True,
                'available': True,
                'last_7_days_cost': round(total_cost, 2),
                'last_7_days_tokens': total_tokens,
                'last_day_cost': round(recent_day_cost, 2),
                'last_day_tokens': recent_day_tokens,
                'models_used': list(models_used),
                'days_tracked': len(usage_data.get('daily', []))
            }

            # Add session quota information if configured
            if self.env.claude_code_session_token_quota:
                quota = self.env.claude_code_session_token_quota
                usage_percent = (session_tokens / quota * 100) if quota > 0 else 0
                response['session_quota'] = quota
                response['session_usage'] = session_tokens
                response['session_usage_percent'] = round(usage_percent, 2)
                response['session_remaining'] = quota - session_tokens
                response['session_remaining_minutes'] = session_remaining_minutes

            # Add weekly quota information if configured
            if self.env.claude_code_weekly_token_quota:
                quota = self.env.claude_code_weekly_token_quota
                usage_percent = (weekly_tokens / quota * 100) if quota > 0 else 0
                response['weekly_quota'] = quota
                response['weekly_usage'] = weekly_tokens
                response['weekly_usage_percent'] = round(usage_percent, 2)
                response['weekly_remaining'] = quota - weekly_tokens

            return response

        except subprocess.TimeoutExpired:
            return {
                'healthy': True,
                'available': False,
                'error': 'ccusage timed out'
            }
        except json.JSONDecodeError as e:
            return {
                'healthy': True,
                'available': False,
                'error': f'Failed to parse ccusage output: {e}'
            }
        except FileNotFoundError:
            return {
                'healthy': True,
                'available': False,
                'error': 'npx or ccusage not found - Node.js may not be installed'
            }
        except Exception as e:
            return {
                'healthy': True,
                'available': False,
                'error': f'Unexpected error: {e}'
            }