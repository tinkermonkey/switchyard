import psutil
import subprocess
import requests
from typing import Dict, Any
from datetime import datetime
from config.environment import Environment

class HealthMonitor:
    """Monitor system health and trigger recovery"""

    # Class-level variable to store last health check result (accessible by observability server)
    last_health_check = None

    def __init__(self, orchestrator=None):
        self.orchestrator = orchestrator
        self.env = Environment()  # Load environment config
        self.health_checks = {
            'github': self.check_github,
            'claude': self.check_claude,
            'disk': self.check_disk_space,
            'memory': self.check_memory,
            'claude_usage': self.check_claude_usage
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

        health_result = {
            'healthy': overall_health,
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
                300,  # 5 minute TTL
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

        # Check basic authentication first
        auth_result = subprocess.run(
            ['gh', 'auth', 'status'],
            capture_output=True, text=True
        )

        if auth_result.returncode != 0:
            return {
                'healthy': False,
                'error': f'GitHub authentication failed: {auth_result.stderr}',
                'auth_status': 'failed'
            }

        # Check if we can access user info
        user_result = subprocess.run(
            ['gh', 'api', 'user'],
            capture_output=True, text=True
        )

        if user_result.returncode != 0:
            return {
                'healthy': False,
                'error': f'GitHub API access failed: {user_result.stderr}',
                'auth_status': 'failed'
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
            capture_output=True, text=True
        )

        if repo_result.returncode != 0:
            return {
                'healthy': False,
                'error': f'Repository access failed for {org}/{repo}: {repo_result.stderr}',
                'auth_status': 'authenticated',
                'repo_access': 'failed'
            }

        # Test GitHub Projects v2 permissions
        # This is the orchestrator's primary function
        projects_result = subprocess.run(
            ['gh', 'project', 'list', '--owner', org, '--format', 'json'],
            capture_output=True, text=True
        )

        if projects_result.returncode != 0:
            return {
                'healthy': False,
                'error': f'GitHub Projects access failed: {projects_result.stderr}',
                'auth_status': 'authenticated',
                'repo_access': 'granted',
                'projects_access': 'failed',
                'critical': 'GitHub Projects v2 access is required for orchestrator to function'
            }

        # Test if we can actually list projects (empty list is OK)
        try:
            json.loads(projects_result.stdout)
        except json.JSONDecodeError as e:
            return {
                'healthy': False,
                'error': f'Invalid response from GitHub Projects API: {e}',
                'projects_access': 'invalid_response'
            }

        return {
            'healthy': True,
            'auth_status': 'authenticated',
            'repo_access': 'granted',
            'projects_access': 'granted',
            'tested_org': org,
            'tested_repo': f'{org}/{repo}'
        }
    
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
            capture_output=True
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