import psutil
import subprocess
import requests
from typing import Dict, Any
from datetime import datetime
from config.environment import Environment

class HealthMonitor:
    """Monitor system health and trigger recovery"""
    
    def __init__(self, orchestrator=None):
        self.orchestrator = orchestrator
        self.env = Environment()  # Load environment config
        self.health_checks = {
            'github': self.check_github,
            'claude': self.check_claude,
            'disk': self.check_disk_space,
            'memory': self.check_memory
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
        
        return {
            'healthy': overall_health,
            'checks': results,
            'timestamp': datetime.now().isoformat()
        }
    
    
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