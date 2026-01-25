#!/usr/bin/env python3
"""
Pipeline Timeline Inspector

Visualizes pipeline execution as a chronological timeline showing stage transitions,
agent assignments, decision points, and bottlenecks.

Usage:
    python scripts/inspect_pipeline_timeline.py <pipeline_run_id>
    python scripts/inspect_pipeline_timeline.py <pipeline_run_id> --verbose
    python scripts/inspect_pipeline_timeline.py <pipeline_run_id> --json
"""

import sys
import os
import json
import argparse
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Any, Optional
import redis
from elasticsearch import Elasticsearch

# Add parent directory to path to import orchestrator modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.pipeline_run import PipelineRunManager


class PipelineTimeline:
    """Visualizes pipeline execution timeline"""

    def __init__(self, redis_client: redis.Redis, es_client: Elasticsearch):
        self.redis_client = redis_client
        self.es_client = es_client
        self.manager = PipelineRunManager(redis_client=redis_client, elasticsearch_client=es_client)

    def get_pipeline_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Get pipeline run metadata from Redis or Elasticsearch"""
        # Try Redis first
        run = self.manager.get_pipeline_run_by_id(run_id)
        if run:
            return run.to_dict()

        # Fall back to Elasticsearch
        try:
            query = {"query": {"term": {"id": run_id}}}
            res = self.es_client.search(index="pipeline-runs-*", body=query)
            hits = res['hits']['hits']
            if hits:
                return hits[0]['_source']
        except Exception as e:
            print(f"Error querying Elasticsearch: {e}", file=sys.stderr)

        return None

    def get_all_events(self, run_id: str) -> List[Dict[str, Any]]:
        """Get all events for a pipeline run, sorted chronologically"""
        all_events = []

        # Get decision events
        try:
            query = {
                "query": {"term": {"pipeline_run_id": run_id}},
                "sort": [{"timestamp": {"order": "asc"}}],
                "size": 1000
            }
            res = self.es_client.search(index="decision-events-*", body=query)
            for hit in res['hits']['hits']:
                event = hit['_source']
                event['_source_index'] = 'decision-events'
                all_events.append(event)
        except Exception as e:
            print(f"Warning: Could not fetch decision events: {e}", file=sys.stderr)

        # Get agent events
        try:
            query = {
                "query": {"term": {"pipeline_run_id": run_id}},
                "sort": [{"timestamp": {"order": "asc"}}],
                "size": 1000
            }
            res = self.es_client.search(index="agent-events-*", body=query)
            for hit in res['hits']['hits']:
                event = hit['_source']
                event['_source_index'] = 'agent-events'
                all_events.append(event)
        except Exception as e:
            print(f"Warning: Could not fetch agent events: {e}", file=sys.stderr)

        # Sort all events by timestamp
        all_events.sort(key=lambda x: x.get('timestamp', ''))

        return all_events

    def format_duration(self, start_time: str, end_time: str) -> str:
        """Calculate and format duration between two ISO timestamps"""
        try:
            start = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
            end = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
            delta = end - start

            hours = int(delta.total_seconds() // 3600)
            minutes = int((delta.total_seconds() % 3600) // 60)
            seconds = int(delta.total_seconds() % 60)

            if hours > 0:
                return f"{hours}h {minutes}m {seconds}s"
            elif minutes > 0:
                return f"{minutes}m {seconds}s"
            else:
                return f"{seconds}s"
        except Exception:
            return "unknown"

    def format_timestamp(self, timestamp: str) -> str:
        """Format timestamp for display"""
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            return dt.strftime('%H:%M:%S')
        except Exception:
            return timestamp[:8] if len(timestamp) >= 8 else timestamp

    def get_event_icon(self, event_type: str) -> str:
        """Get icon for event type"""
        icons = {
            'pipeline_run_started': '▶',
            'pipeline_run_completed': '✓',
            'pipeline_run_failed': '✗',
            'agent_initialized': '▶',
            'agent_started': '⚙',
            'agent_completed': '✓',
            'agent_failed': '✗',
            'agent_routing_decision': '⚙',
            'status_progression_started': '⚙',
            'status_progression_completed': '✓',
            'review_cycle_started': '⚙',
            'review_cycle_completed': '✓',
            'feedback_detected': '⚠',
            'error': '✗',
        }
        return icons.get(event_type, '•')

    def visualize_timeline(self, run_id: str, verbose: bool = False) -> Dict[str, Any]:
        """Generate timeline visualization"""
        run_data = self.get_pipeline_run(run_id)
        if not run_data:
            return {
                'error': f'Pipeline run {run_id} not found',
                'run_id': run_id
            }

        events = self.get_all_events(run_id)

        # Calculate summary statistics
        agent_count = defaultdict(int)
        review_cycles = 0
        status_changes = 0
        errors = 0
        agent_durations = []

        agent_start_times = {}

        for event in events:
            event_type = event.get('event_type', '')

            if event_type == 'agent_initialized':
                agent = event.get('agent', 'unknown')
                agent_count[agent] += 1
                agent_start_times[event.get('task_id', '')] = event.get('timestamp')

            elif event_type == 'agent_completed':
                task_id = event.get('task_id', '')
                if task_id in agent_start_times:
                    duration = self.format_duration(agent_start_times[task_id], event.get('timestamp'))
                    agent_durations.append({
                        'agent': event.get('agent', 'unknown'),
                        'duration': duration
                    })

            elif event_type == 'review_cycle_started':
                review_cycles += 1

            elif event_type == 'status_progression_completed':
                status_changes += 1

            elif 'failed' in event_type or 'error' in event_type:
                errors += 1

        result = {
            'run_id': run_id,
            'run_data': run_data,
            'events': events,
            'summary': {
                'total_agents': sum(agent_count.values()),
                'agent_breakdown': dict(agent_count),
                'review_cycles': review_cycles,
                'status_changes': status_changes,
                'errors': errors,
                'agent_durations': agent_durations
            }
        }

        return result

    def print_timeline(self, timeline_data: Dict[str, Any], verbose: bool = False):
        """Print timeline in human-readable format"""
        if 'error' in timeline_data:
            print(f"Error: {timeline_data['error']}")
            return

        run_data = timeline_data['run_data']
        events = timeline_data['events']
        summary = timeline_data['summary']

        # Header
        print(f"\nPipeline Run Timeline: {timeline_data['run_id']}")
        print(f"Issue: #{run_data.get('issue_number')} - \"{run_data.get('issue_title', 'N/A')}\"")
        print(f"Project: {run_data.get('project')} | Board: {run_data.get('board')}")

        started_at = run_data.get('started_at', 'N/A')
        ended_at = run_data.get('ended_at', 'In Progress')
        duration = self.format_duration(started_at, ended_at) if ended_at != 'In Progress' else 'In Progress'

        print(f"Started: {started_at} | Ended: {ended_at} | Duration: {duration}")
        print(f"Status: {run_data.get('status', 'unknown')}")

        # Timeline
        print("\nTimeline:")
        print("━" * 80)

        for event in events:
            timestamp = self.format_timestamp(event.get('timestamp', ''))
            event_type = event.get('event_type', 'unknown')
            icon = self.get_event_icon(event_type)

            print(f"\n[{timestamp}] {icon} {event_type.upper().replace('_', ' ')}")

            # Event-specific details
            if event_type == 'pipeline_run_started':
                print(f"           Project: {event.get('project')}, Board: {event.get('board')}")

            elif event_type == 'agent_routing_decision':
                print(f"           Selected: {event.get('selected_agent')}")
                if 'reason' in event:
                    print(f"           Reason: {event.get('reason')}")

            elif event_type == 'agent_initialized':
                print(f"           Agent: {event.get('agent')}")
                print(f"           Task: {event.get('task_id')}")
                if 'container_name' in event:
                    print(f"           Container: {event.get('container_name')}")

            elif event_type == 'agent_completed':
                print(f"           Agent: {event.get('agent')}")
                task_id = event.get('task_id', '')
                # Find matching start event to calculate duration
                for e in events:
                    if e.get('event_type') == 'agent_initialized' and e.get('task_id') == task_id:
                        duration = self.format_duration(e.get('timestamp'), event.get('timestamp'))
                        print(f"           Duration: {duration}")
                        break
                print(f"           Success: {event.get('success', 'unknown')}")

            elif event_type == 'agent_failed':
                print(f"           Agent: {event.get('agent')}")
                print(f"           Error: {event.get('error', 'Unknown error')}")

            elif event_type == 'status_progression_started':
                print(f"           From: {event.get('from_status')} → To: {event.get('to_status')}")

            elif event_type == 'status_progression_completed':
                print(f"           Now in: {event.get('to_status')}")

            elif event_type == 'review_cycle_started':
                print(f"           Reviewer: {event.get('agent')}")
                print(f"           Iteration: {event.get('iteration', 1)}")

            elif event_type == 'review_cycle_completed':
                print(f"           Total iterations: {event.get('iteration', 'unknown')}")
                print(f"           Final status: {event.get('final_status', 'unknown')}")

            elif event_type == 'feedback_detected':
                print(f"           Source: {event.get('feedback_source')}")
                if verbose and 'comment' in event:
                    print(f"           Comment: {event.get('comment')}")

        print("\n" + "━" * 80)

        # Summary
        print("\nSummary:")
        print(f"  Total Agents: {summary['total_agents']}")
        if summary['agent_breakdown']:
            breakdown = ', '.join([f"{agent} x{count}" for agent, count in summary['agent_breakdown'].items()])
            print(f"    ({breakdown})")
        print(f"  Review Cycles: {summary['review_cycles']}")
        print(f"  Status Changes: {summary['status_changes']}")
        print(f"  Errors: {summary['errors']}")

        if summary['agent_durations']:
            print(f"\n  Agent Durations:")
            for ad in summary['agent_durations']:
                print(f"    {ad['agent']}: {ad['duration']}")

        print()


def main():
    parser = argparse.ArgumentParser(description='Visualize pipeline run timeline')
    parser.add_argument('run_id', help='Pipeline run ID to inspect')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Show verbose output including full event details')
    parser.add_argument('--json', action='store_true',
                        help='Output as JSON instead of formatted text')
    parser.add_argument('--redis-host', default='localhost',
                        help='Redis host (default: localhost)')
    parser.add_argument('--redis-port', type=int, default=6379,
                        help='Redis port (default: 6379)')
    parser.add_argument('--es-host', default='localhost',
                        help='Elasticsearch host (default: localhost)')
    parser.add_argument('--es-port', type=int, default=9200,
                        help='Elasticsearch port (default: 9200)')

    args = parser.parse_args()

    # Connect to services
    try:
        redis_client = redis.Redis(
            host=args.redis_host,
            port=args.redis_port,
            decode_responses=True
        )
        redis_client.ping()
    except Exception as e:
        print(f"Error connecting to Redis: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        es_client = Elasticsearch([f"http://{args.es_host}:{args.es_port}"])
        es_client.info()
    except Exception as e:
        print(f"Error connecting to Elasticsearch: {e}", file=sys.stderr)
        sys.exit(1)

    # Create timeline inspector
    timeline = PipelineTimeline(redis_client, es_client)

    # Generate timeline
    result = timeline.visualize_timeline(args.run_id, verbose=args.verbose)

    # Output
    if args.json:
        # Convert to JSON-serializable format
        output = {
            'run_id': result['run_id'],
            'run_data': result.get('run_data', {}),
            'events': result.get('events', []),
            'summary': result.get('summary', {}),
            'error': result.get('error')
        }
        print(json.dumps(output, indent=2))
    else:
        timeline.print_timeline(result, verbose=args.verbose)

    # Exit with error code if pipeline run not found
    if 'error' in result:
        sys.exit(1)


if __name__ == '__main__':
    main()
