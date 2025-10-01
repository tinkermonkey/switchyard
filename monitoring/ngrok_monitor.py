import requests
import time
import logging

logger = logging.getLogger(__name__)

class NgrokMonitor:
    """Monitor ngrok tunnel health"""
    
    def __init__(self, check_interval: int = 60):
        self.check_interval = check_interval
        self.api_url = "http://localhost:4040/api"
    
    def get_tunnel_status(self):
        """Get current tunnel status"""
        try:
            response = requests.get(f"{self.api_url}/tunnels")
            tunnels = response.json()['tunnels']
            
            for tunnel in tunnels:
                if tunnel['proto'] == 'https':
                    return {
                        'status': 'connected',
                        'url': tunnel['public_url'],
                        'started': tunnel['started_at'],
                        'metrics': tunnel['metrics']
                    }
            
            return {'status': 'no_tunnel'}
            
        except Exception as e:
            return {'status': 'error', 'error': str(e)}
    
    def monitor_loop(self):
        """Continuous monitoring loop"""
        while True:
            status = self.get_tunnel_status()
            
            if status['status'] == 'connected':
                logger.info(f"ngrok connected: {status['url']}")
            else:
                logger.warning(f"ngrok issue: {status}")
            
            time.sleep(self.check_interval)