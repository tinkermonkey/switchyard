import psutil
import subprocess
import redis
from typing import Dict, Any
from datetime import datetime
from config.environment import Environment

class HealthMonitor:
    """Monitor system health and trigger recovery"""
    
    def __init__(self, orchestrator=None):
        self.orchestrator = orchestrator
        self.env = Environment()  # Load environment config
        self.health_checks = {
            'redis': self.check_redis,
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
    
    async def check_redis(self) -> Dict[str, bool]:
        """Check Redis connectivity"""
        try:
            redis_client = redis.Redis.from_url(self.env.redis_url)
            redis_client.ping()
            return {'healthy': True}
        except:
            return {'healthy': False}
    
    async def check_github(self) -> Dict[str, bool]:
        """Check GitHub API access"""
        result = subprocess.run(
            ['gh', 'api', 'user'],
            capture_output=True
        )
        return {'healthy': result.returncode == 0}
    
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