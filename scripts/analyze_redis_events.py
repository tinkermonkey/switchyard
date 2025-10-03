#!/usr/bin/env python3
"""
Analyze observability events from Redis to check actual context sizes

This script connects to Redis and analyzes the stored events to see
what prompt sizes are actually being passed to agents.
"""

import sys
import json
import redis
from pathlib import Path
from collections import defaultdict

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


def analyze_redis_events():
    """Pull and analyze events from Redis Stream"""

    # Connect to Redis
    r = redis.Redis(host='redis', port=6379, decode_responses=True)

    print("=" * 80)
    print("REDIS EVENT STREAM ANALYSIS")
    print("=" * 80)

    # Check the event stream
    stream_key = "orchestrator:event_stream"

    try:
        total_count = r.xlen(stream_key)
        print(f"\nTotal events in stream: {total_count}")
    except:
        print(f"\nStream '{stream_key}' not found")
        return

    if total_count == 0:
        print("No events in stream")
        return

    # Read events from the stream (last 100)
    print(f"Reading last 100 events from stream...")
    events = r.xrevrange(stream_key, '+', '-', count=100)

    print(f"Retrieved {len(events)} events\n")

    # Parse events
    events_by_type = defaultdict(list)
    all_events = []

    for event_id, event_data in events:
        try:
            event_json = event_data.get('event')
            if event_json:
                event = json.loads(event_json)
                event_type = event.get('type', 'unknown')
                events_by_type[event_type].append(event)
                all_events.append(event)
        except Exception as e:
            print(f"Warning: Failed to parse event {event_id}: {e}")

    print(f"\nEvent types found: {list(events_by_type.keys())}")

    # Analyze claude_call_started events for prompt sizes
    if 'claude_call_started' in events_by_type:
        print(f"\n{'=' * 80}")
        print(f"CLAUDE API CALL ANALYSIS")
        print(f"{'=' * 80}\n")

        started_events = events_by_type['claude_call_started']
        print(f"Total claude_call_started events: {len(started_events)}")

        # Group by agent
        by_agent = defaultdict(list)
        for event in started_events:
            agent = event.get('agent', 'unknown')
            by_agent[agent].append(event)

        # Analyze each agent's calls
        for agent, calls in sorted(by_agent.items()):
            print(f"\n{'─' * 80}")
            print(f"Agent: {agent}")
            print(f"{'─' * 80}")
            print(f"Total calls: {len(calls)}")

            # Get prompt data if available
            prompt_sizes = []
            for call in calls:
                # Check if we have prompt data in the event
                if 'prompt_length' in call:
                    prompt_sizes.append(call['prompt_length'])
                elif 'context' in call and 'prompt' in call['context']:
                    prompt_sizes.append(len(call['context']['prompt']))

            if prompt_sizes:
                avg_size = sum(prompt_sizes) / len(prompt_sizes)
                max_size = max(prompt_sizes)
                min_size = min(prompt_sizes)

                print(f"  Prompt sizes:")
                print(f"    Min: {min_size:,} chars ({min_size/1024:.1f} KB)")
                print(f"    Max: {max_size:,} chars ({max_size/1024:.1f} KB)")
                print(f"    Avg: {avg_size:,.0f} chars ({avg_size/1024:.1f} KB)")

                # Show distribution
                if len(prompt_sizes) > 1:
                    print(f"  Distribution:")
                    for i, size in enumerate(sorted(prompt_sizes, reverse=True)[:5]):
                        print(f"    Call {i+1}: {size:,} chars ({size/1024:.1f} KB)")
            else:
                print("  No prompt size data available in events")

    # Look for specific event data with prompts
    print(f"\n{'=' * 80}")
    print(f"SEARCHING FOR EVENTS WITH PROMPT DATA")
    print(f"{'=' * 80}\n")

    events_with_prompts = 0
    for event_type, events in events_by_type.items():
        for event in events:
            # Check various places where prompt data might be stored
            if 'prompt' in event:
                events_with_prompts += 1
                prompt_len = len(event['prompt']) if isinstance(event['prompt'], str) else 0
                print(f"{event_type}: {prompt_len:,} chars ({prompt_len/1024:.1f} KB)")
            elif 'data' in event and isinstance(event['data'], dict):
                if 'prompt' in event['data']:
                    events_with_prompts += 1
                    prompt_len = len(event['data']['prompt']) if isinstance(event['data']['prompt'], str) else 0
                    print(f"{event_type}: {prompt_len:,} chars ({prompt_len/1024:.1f} KB)")

    if events_with_prompts == 0:
        print("No events found with embedded prompt data")
        print("\nNote: Prompt data might be stored separately or not persisted to Redis")
        print("Checking agent execution logs instead...")

    # Get recent agent execution info from logs (stored in Redis)
    print(f"\n{'=' * 80}")
    print(f"CHECKING RECENT EXECUTIONS")
    print(f"{'=' * 80}\n")

    # Look for any keys that might contain execution data
    exec_keys = r.keys('*execution*') + r.keys('*agent*') + r.keys('*task*')
    if exec_keys:
        print(f"Found {len(exec_keys)} execution-related keys:")
        for key in sorted(exec_keys)[:10]:  # Show first 10
            print(f"  {key}")
    else:
        print("No execution-related keys found")


if __name__ == '__main__':
    try:
        analyze_redis_events()
    except redis.ConnectionError as e:
        print(f"ERROR: Could not connect to Redis: {e}")
        print("Make sure Redis is running (docker-compose up redis)")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
