#!/usr/bin/env python3
"""
Task Queue Health Inspector

Monitors task queue health by detecting stuck tasks, analyzing queue depth trends,
and identifying retry patterns.

Usage:
    python scripts/inspect_task_health.py
    python scripts/inspect_task_health.py --show-all
    python scripts/inspect_task_health.py --project context-studio
    python scripts/inspect_task_health.py --json

Exit Codes:
    0 - Healthy (no stuck tasks)
    1 - Warning (some stuck tasks detected)
    2 - Critical (many stuck tasks or queue issues)
"""

import sys
import os
import json
import argparse
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Any, Optional
import redis


class TaskHealthMonitor:
    """Monitors task queue health"""

    # Default age thresholds (in seconds)
    DEFAULT_HIGH_THRESHOLD = 30 * 60      # 30 minutes
    DEFAULT_MEDIUM_THRESHOLD = 60 * 60    # 1 hour
    DEFAULT_LOW_THRESHOLD = 4 * 60 * 60   # 4 hours

    def __init__(self, redis_client: redis.Redis):
        self.redis_client = redis_client
        self.priorities = ['high', 'medium', 'low']

    def get_all_tasks(self) -> List[Dict[str, Any]]:
        """Retrieve all tasks from all priority queues"""
        all_tasks = []

        for priority in self.priorities:
            queue_name = f"tasks:{priority}"
            try:
                task_ids = self.redis_client.lrange(queue_name, 0, -1)

                for task_id in task_ids:
                    task_key = f"task:{task_id}"
                    task_data = self.redis_client.hgetall(task_key)

                    if task_data:
                        # Parse context if it's a JSON string
                        context = task_data.get('context', '{}')
                        if isinstance(context, str):
                            try:
                                context = json.loads(context)
                            except json.JSONDecodeError:
                                context = {}

                        task = {
                            'id': task_id,
                            'priority': priority,
                            'agent': task_data.get('agent', 'unknown'),
                            'project': task_data.get('project', 'unknown'),
                            'status': task_data.get('status', 'pending'),
                            'created_at': task_data.get('created_at', ''),
                            'context': context,
                            'issue_number': context.get('issue_number', 'N/A'),
                        }
                        all_tasks.append(task)

            except Exception as e:
                print(f"Warning: Error reading {queue_name}: {e}", file=sys.stderr)

        return all_tasks

    def calculate_task_age(self, created_at: str) -> Optional[timedelta]:
        """Calculate how long a task has been in the queue"""
        if not created_at:
            return None

        try:
            created = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            now = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
            return now - created
        except Exception:
            return None

    def format_duration(self, delta: timedelta) -> str:
        """Format timedelta as human-readable string"""
        total_seconds = int(delta.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60

        if hours > 0:
            return f"{hours} hour{'s' if hours != 1 else ''} {minutes} minute{'s' if minutes != 1 else ''}"
        elif minutes > 0:
            return f"{minutes} minute{'s' if minutes != 1 else ''}"
        else:
            return f"{seconds} second{'s' if seconds != 1 else ''}"

    def detect_stuck_tasks(
        self,
        tasks: List[Dict[str, Any]],
        high_threshold: int = None,
        medium_threshold: int = None,
        low_threshold: int = None
    ) -> List[Dict[str, Any]]:
        """Identify tasks that exceed age thresholds"""
        if high_threshold is None:
            high_threshold = self.DEFAULT_HIGH_THRESHOLD
        if medium_threshold is None:
            medium_threshold = self.DEFAULT_MEDIUM_THRESHOLD
        if low_threshold is None:
            low_threshold = self.DEFAULT_LOW_THRESHOLD

        thresholds = {
            'high': high_threshold,
            'medium': medium_threshold,
            'low': low_threshold
        }

        stuck_tasks = []

        for task in tasks:
            age = self.calculate_task_age(task['created_at'])
            if not age:
                continue

            priority = task['priority']
            threshold = thresholds.get(priority, self.DEFAULT_LOW_THRESHOLD)

            if age.total_seconds() > threshold:
                task['age'] = age
                task['threshold'] = threshold
                task['exceeded_by'] = age.total_seconds() - threshold
                stuck_tasks.append(task)

        # Sort by how much they exceed the threshold
        stuck_tasks.sort(key=lambda t: t['exceeded_by'], reverse=True)

        return stuck_tasks

    def analyze_distribution(self, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze task distribution by project and agent"""
        by_project = defaultdict(int)
        by_agent = defaultdict(int)
        by_priority = defaultdict(int)

        for task in tasks:
            by_project[task['project']] += 1
            by_agent[task['agent']] += 1
            by_priority[task['priority']] += 1

        total = len(tasks)

        return {
            'by_project': dict(by_project),
            'by_agent': dict(by_agent),
            'by_priority': dict(by_priority),
            'total': total
        }

    def generate_health_report(
        self,
        tasks: List[Dict[str, Any]],
        stuck_tasks: List[Dict[str, Any]],
        distribution: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate health report with status and recommendations"""
        total_tasks = len(tasks)
        num_stuck = len(stuck_tasks)

        # Determine health status
        if num_stuck == 0:
            status = 'healthy'
            status_icon = '✓'
            exit_code = 0
        elif num_stuck <= 2:
            status = 'warning'
            status_icon = '⚠️'
            exit_code = 1
        else:
            status = 'critical'
            status_icon = '✗'
            exit_code = 2

        # Generate recommendations
        recommendations = []
        if num_stuck > 0:
            recommendations.append(f"Investigate {num_stuck} stuck task{'s' if num_stuck != 1 else ''}")
            recommendations.append("Check if orchestrator is processing tasks")
            recommendations.append("Verify agent containers are running: docker ps")
            recommendations.append("Check circuit breakers: python scripts/inspect_circuit_breakers.py")

        return {
            'status': status,
            'status_icon': status_icon,
            'exit_code': exit_code,
            'total_tasks': total_tasks,
            'stuck_tasks': num_stuck,
            'distribution': distribution,
            'recommendations': recommendations
        }

    def print_report(
        self,
        tasks: List[Dict[str, Any]],
        stuck_tasks: List[Dict[str, Any]],
        distribution: Dict[str, Any],
        health: Dict[str, Any],
        show_all: bool = False
    ):
        """Print health report in human-readable format"""
        print("\nTask Queue Health Report")
        print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("━" * 80)

        # Queue depth summary
        print("\nQueue Depth Summary:")
        by_priority = distribution['by_priority']
        print(f"  High Priority:   {by_priority.get('high', 0)} tasks")
        print(f"  Medium Priority: {by_priority.get('medium', 0)} tasks")
        print(f"  Low Priority:    {by_priority.get('low', 0)} tasks")
        print(f"  Total:           {health['total_tasks']} tasks")

        print("\n" + "━" * 80)

        # Stuck tasks
        if stuck_tasks:
            print(f"\n⚠️  STUCK TASKS DETECTED: {len(stuck_tasks)}\n")

            for task in stuck_tasks:
                age_str = self.format_duration(task['age'])
                threshold_str = self.format_duration(timedelta(seconds=task['threshold']))
                exceeded_str = self.format_duration(timedelta(seconds=task['exceeded_by']))

                print(f"[{task['priority'].upper()}] Task: {task['id']}")
                print(f"  Agent: {task['agent']}")
                print(f"  Project: {task['project']}")
                print(f"  Issue: #{task['issue_number']}")
                print(f"  Age: {age_str} (threshold: {threshold_str})")
                print(f"  Created: {task['created_at']}")
                print(f"  Status: {task['status']}")
                print(f"  ⚠️  STUCK - Exceeds age threshold by {exceeded_str}")
                print()
        else:
            print("\n✓ No stuck tasks detected")

        print("━" * 80)

        # Show all tasks if requested
        if show_all and tasks:
            print("\nAll Tasks:")
            for task in tasks:
                age = self.calculate_task_age(task['created_at'])
                age_str = self.format_duration(age) if age else 'unknown'

                print(f"\n[{task['priority'].upper()}] Task: {task['id']}")
                print(f"  Agent: {task['agent']}")
                print(f"  Project: {task['project']}")
                print(f"  Issue: #{task['issue_number']}")
                print(f"  Age: {age_str}")
                print(f"  Status: {task['status']}")

            print("\n" + "━" * 80)

        # Distribution
        print("\nDistribution by Project:")
        by_project = distribution['by_project']
        total = health['total_tasks']
        for project, count in sorted(by_project.items(), key=lambda x: x[1], reverse=True):
            pct = (count / total * 100) if total > 0 else 0
            print(f"  {project}: {count} tasks ({pct:.0f}%)")

        print("\nDistribution by Agent:")
        by_agent = distribution['by_agent']
        for agent, count in sorted(by_agent.items(), key=lambda x: x[1], reverse=True):
            pct = (count / total * 100) if total > 0 else 0
            print(f"  {agent}: {count} tasks ({pct:.0f}%)")

        print("\n" + "━" * 80)

        # Health status
        print(f"\nHealth Status: {health['status_icon']} {health['status'].upper()}", end='')
        if stuck_tasks:
            print(f" - {len(stuck_tasks)} stuck task{'s' if len(stuck_tasks) != 1 else ''} detected")
        else:
            print()

        # Recommendations
        if health['recommendations']:
            print("\nRecommendations:")
            for rec in health['recommendations']:
                print(f"  - {rec}")

        print()


def main():
    parser = argparse.ArgumentParser(description='Monitor task queue health')
    parser.add_argument('--show-all', action='store_true',
                        help='Show all tasks, not just stuck ones')
    parser.add_argument('--project', type=str,
                        help='Filter tasks by project name')
    parser.add_argument('--json', action='store_true',
                        help='Output as JSON instead of formatted text')
    parser.add_argument('--high-threshold', type=int,
                        help=f'Age threshold for HIGH priority tasks in seconds (default: 1800)')
    parser.add_argument('--medium-threshold', type=int,
                        help=f'Age threshold for MEDIUM priority tasks in seconds (default: 3600)')
    parser.add_argument('--low-threshold', type=int,
                        help=f'Age threshold for LOW priority tasks in seconds (default: 14400)')
    parser.add_argument('--redis-host', default='localhost',
                        help='Redis host (default: localhost)')
    parser.add_argument('--redis-port', type=int, default=6379,
                        help='Redis port (default: 6379)')

    args = parser.parse_args()

    # Connect to Redis
    try:
        redis_client = redis.Redis(
            host=args.redis_host,
            port=args.redis_port,
            decode_responses=True
        )
        redis_client.ping()
    except Exception as e:
        print(f"Error connecting to Redis: {e}", file=sys.stderr)
        sys.exit(2)

    # Create health monitor
    monitor = TaskHealthMonitor(redis_client)

    # Get all tasks
    all_tasks = monitor.get_all_tasks()

    # Filter by project if specified
    if args.project:
        all_tasks = [t for t in all_tasks if t['project'] == args.project]

    # Detect stuck tasks
    stuck_tasks = monitor.detect_stuck_tasks(
        all_tasks,
        high_threshold=args.high_threshold,
        medium_threshold=args.medium_threshold,
        low_threshold=args.low_threshold
    )

    # Analyze distribution
    distribution = monitor.analyze_distribution(all_tasks)

    # Generate health report
    health = monitor.generate_health_report(all_tasks, stuck_tasks, distribution)

    # Output
    if args.json:
        output = {
            'timestamp': datetime.now().isoformat(),
            'status': health['status'],
            'total_tasks': health['total_tasks'],
            'stuck_tasks': health['stuck_tasks'],
            'distribution': distribution,
            'stuck_task_details': [
                {
                    'id': t['id'],
                    'priority': t['priority'],
                    'agent': t['agent'],
                    'project': t['project'],
                    'issue_number': t['issue_number'],
                    'age_seconds': int(t['age'].total_seconds()),
                    'threshold_seconds': t['threshold'],
                    'exceeded_by_seconds': int(t['exceeded_by']),
                    'created_at': t['created_at'],
                }
                for t in stuck_tasks
            ],
            'recommendations': health['recommendations']
        }
        if args.show_all:
            output['all_tasks'] = [
                {
                    'id': t['id'],
                    'priority': t['priority'],
                    'agent': t['agent'],
                    'project': t['project'],
                    'issue_number': t['issue_number'],
                    'created_at': t['created_at'],
                }
                for t in all_tasks
            ]
        print(json.dumps(output, indent=2))
    else:
        monitor.print_report(all_tasks, stuck_tasks, distribution, health, show_all=args.show_all)

    # Exit with appropriate code
    sys.exit(health['exit_code'])


if __name__ == '__main__':
    main()
