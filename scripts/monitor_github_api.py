#!/usr/bin/env python3

"""
GitHub API Monitoring Dashboard
Real-time monitoring of GitHub API usage, rate limits, and circuit breaker status.

Usage:
    python scripts/monitor_github_api.py              # Live dashboard (updates every 30s)
    python scripts/monitor_github_api.py --once       # Show once and exit
    python scripts/monitor_github_api.py --watch 10   # Custom refresh interval (10s)
    python scripts/monitor_github_api.py --json       # JSON output
    python scripts/monitor_github_api.py --alerts     # Show only alerts
"""

import sys
import time
import json
import argparse
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any
from urllib.parse import urljoin

try:
    import requests
except ImportError:
    print("Error: requests library required. Install with: pip install requests")
    sys.exit(1)


class GitHubAPIMonitor:
    """Monitor GitHub API usage via observability server"""
    
    def __init__(self, observability_url: str = "http://localhost:5001"):
        self.observability_url = observability_url
        self.endpoint = urljoin(observability_url, "/api/github-api-status")
    
    def get_status(self) -> Optional[Dict[str, Any]]:
        """Fetch current GitHub API status"""
        try:
            response = requests.get(self.endpoint, timeout=5)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.ConnectionError:
            print(f"❌ Error: Could not connect to {self.observability_url}")
            print("   Make sure the orchestrator is running: docker compose up -d")
            return None
        except requests.exceptions.Timeout:
            print(f"❌ Timeout connecting to {self.observability_url}")
            return None
        except Exception as e:
            print(f"❌ Error: {e}")
            return None
    
    def format_percentage_bar(self, percentage: float, width: int = 50) -> str:
        """Create a visual bar for percentage"""
        filled = int((percentage / 100) * width)
        empty = width - filled
        
        bar = "█" * filled + "░" * empty
        
        if percentage > 95:
            color = "🚨 CRITICAL"
        elif percentage > 90:
            color = "🔴 HIGH"
        elif percentage > 80:
            color = "🟡 ELEVATED"
        elif percentage > 50:
            color = "🟢 MODERATE"
        else:
            color = "🟢 LOW"
        
        return f"[{bar}] {color}"
    
    def print_dashboard(self, status: Dict[str, Any]):
        """Print formatted dashboard"""
        print("\033[H\033[J")  # Clear screen
        print("╔════════════════════════════════════════════════════════════════════╗")
        print("║           GitHub API Usage Monitoring Dashboard                   ║")
        print("╚════════════════════════════════════════════════════════════════════╝")
        print()
        
        rl = status['status']['rate_limit']
        breaker = status['status']['breaker']
        stats = status['status']['stats']
        
        # Rate limit section
        remaining = rl['remaining']
        limit = rl['limit']
        percentage = rl['percentage_used']
        reset_time = rl.get('time_until_reset') or 0
        reset_timestamp = rl.get('reset_time') or 'Unknown'
        
        print("📊 RATE LIMIT STATUS")
        print("━" * 72)
        print(f"  Remaining:  {remaining:5d} / {limit:5d} points")
        print(f"  Used:       {percentage:6.1f}%")
        print(f"  Time until reset:  {reset_time:.0f} seconds (~{reset_time/60:.0f} minutes)")
        print(f"  Reset at:   {reset_timestamp}")
        print()
        
        # Visual bar
        bar = self.format_percentage_bar(percentage)
        print(f"  {bar}")
        print()
        
        # Circuit breaker section
        print("🔌 CIRCUIT BREAKER")
        print("━" * 72)
        
        if breaker['is_open']:
            print("  Status: 🔴 OPEN (Requests being blocked)")
            if breaker['opened_at']:
                print(f"  Opened at: {breaker['opened_at']}")
            if breaker['reset_time']:
                print(f"  Will reset: {breaker['reset_time']}")
        else:
            print(f"  Status: 🟢 {breaker['state'].upper()} (Requests allowed)")
        print()
        
        # Statistics section
        print("📈 STATISTICS")
        print("━" * 72)
        print(f"  Total requests:        {stats['total_requests']}")
        print(f"  Failed requests:       {stats['failed_requests']}")
        print(f"  Rate-limited requests: {stats['rate_limited_requests']}")
        print(f"  Backoff multiplier:    {stats['backoff_multiplier']:.1f}×")
        print()
        
        print("━" * 72)
        print("🔄 Refreshing in 30 seconds... (Press Ctrl+C to exit)")
        print()
    
    def print_json(self, status: Dict[str, Any]):
        """Print status as JSON"""
        print(json.dumps(status, indent=2))
    
    def print_alerts(self, status: Dict[str, Any]):
        """Print only alerts/warnings"""
        rl = status['status']['rate_limit']
        breaker = status['status']['breaker']
        stats = status['status']['stats']
        
        percentage = rl['percentage_used']
        remaining = rl['remaining']
        
        alerts = []
        
        # Check rate limit
        if breaker['is_open']:
            alerts.append(f"🚨 CRITICAL: Circuit breaker is OPEN - requests being blocked")
            if breaker['reset_time']:
                alerts.append(f"   Will reset: {breaker['reset_time']}")
        
        if remaining <= 100:
            alerts.append(f"🚨 CRITICAL: Rate limit critically low! Only {remaining} points remaining")
        elif remaining <= 250:
            alerts.append(f"🔴 WARNING: Rate limit low! Only {remaining} points remaining ({percentage:.1f}% used)")
        elif percentage >= 95:
            alerts.append(f"⚠️  ELEVATED: API usage at {percentage:.1f}% ({remaining} points remaining)")
        elif percentage >= 90:
            alerts.append(f"ℹ️  HIGH: API usage at {percentage:.1f}% ({remaining} points remaining)")
        
        # Check for failures
        if stats['failed_requests'] > 0:
            alerts.append(f"⚠️  {stats['failed_requests']} failed requests")
        
        if stats['rate_limited_requests'] > 0:
            alerts.append(f"🔴 {stats['rate_limited_requests']} rate-limited requests")
        
        if stats['backoff_multiplier'] > 1.0:
            alerts.append(f"⚠️  Exponential backoff active: {stats['backoff_multiplier']:.1f}×")
        
        if alerts:
            print("\n".join(alerts))
            return 0
        else:
            print("✅ No alerts - GitHub API status is normal")
            return 0
    
    def monitor_continuous(self, interval: int = 30):
        """Monitor continuously with refresh interval"""
        try:
            while True:
                status = self.get_status()
                if status:
                    self.print_dashboard(status)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n✓ Monitoring stopped")
            sys.exit(0)
    
    def monitor_once(self):
        """Show status once and exit"""
        status = self.get_status()
        if status:
            self.print_dashboard(status)
            return 0
        return 1


def main():
    parser = argparse.ArgumentParser(
        description="Monitor GitHub API usage in real-time",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Live dashboard (updates every 30s)
  python scripts/monitor_github_api.py

  # Show once and exit
  python scripts/monitor_github_api.py --once

  # Custom refresh interval
  python scripts/monitor_github_api.py --watch 10

  # JSON output
  python scripts/monitor_github_api.py --json

  # Show only alerts
  python scripts/monitor_github_api.py --alerts
        """
    )
    
    parser.add_argument(
        "--once",
        action="store_true",
        help="Show status once and exit"
    )
    
    parser.add_argument(
        "--watch",
        type=int,
        metavar="SECONDS",
        help="Custom refresh interval (default: 30)"
    )
    
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON"
    )
    
    parser.add_argument(
        "--alerts",
        action="store_true",
        help="Show only alerts and warnings"
    )
    
    parser.add_argument(
        "--url",
        default="http://localhost:5001",
        help="Observability server URL (default: http://localhost:5001)"
    )
    
    args = parser.parse_args()
    
    monitor = GitHubAPIMonitor(args.url)
    
    if args.alerts:
        status = monitor.get_status()
        if status:
            return monitor.print_alerts(status)
        return 1
    
    if args.json:
        status = monitor.get_status()
        if status:
            monitor.print_json(status)
            return 0
        return 1
    
    if args.once:
        return monitor.monitor_once()
    
    interval = args.watch or 30
    monitor.monitor_continuous(interval)


if __name__ == "__main__":
    sys.exit(main())
