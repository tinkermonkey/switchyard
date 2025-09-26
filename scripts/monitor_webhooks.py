#!/usr/bin/env python3

import time
import requests
from datetime import datetime
from collections import deque

class WebhookMonitor:
    def __init__(self):
        self.events = deque(maxlen=50)  # Keep last 50 events
        
    def monitor(self):
        print("\nWebhook Monitor Started")
        print("=" * 60)
        
        last_check = None
        
        while True:
            try:
                # Check ngrok
                ngrok_response = requests.get('http://localhost:4040/api/tunnels')
                if ngrok_response.status_code == 200:
                    tunnels = ngrok_response.json()['tunnels']
                    if tunnels:
                        url = tunnels[0]['public_url']
                        print(f"\r📡 ngrok: {url}/github-webhook", end="")
                    else:
                        print(f"\r⚠️  ngrok: No tunnels active", end="")
                
                # Check webhook health
                health_response = requests.get('http://localhost:3000/health')
                if health_response.status_code == 200:
                    print(f" | 💚 Webhook: healthy", end="")
                else:
                    print(f" | 🔴 Webhook: unhealthy", end="")
                
                # Check Redis queue (if you implement an endpoint)
                # queue_response = requests.get('http://localhost:3000/queue-status')
                # print(f" | Queue: {queue_response.json()['pending']}", end="")
                
                time.sleep(2)
                
            except KeyboardInterrupt:
                print("\n\n👋 Monitor stopped")
                break
            except Exception as e:
                print(f"\r❌ Error: {e}", end="")
                time.sleep(5)

if __name__ == "__main__":
    monitor = WebhookMonitor()
    monitor.monitor()