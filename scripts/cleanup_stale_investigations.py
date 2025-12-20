#!/usr/bin/env python3
"""
Cleanup stale investigations that are marked as in_progress but have no running process.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import asyncio
from elasticsearch import Elasticsearch
from services.medic.docker import DockerFailureSignatureStore
from monitoring.timestamp_utils import utc_isoformat
import redis


async def cleanup_stale_docker_investigations():
    """Find and cleanup stale Docker investigations"""

    # Connect to Elasticsearch (use service name from Docker network)
    es = Elasticsearch(['http://elasticsearch:9200'])

    # Connect to Redis (use service name from Docker network)
    redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)

    # Create signature store (only needs ES client)
    store = DockerFailureSignatureStore(es)

    # Query for in_progress investigations
    query = {
        "query": {
            "term": {
                "investigation_status": "in_progress"
            }
        },
        "size": 100
    }

    # Query old index pattern (data hasn't been migrated yet)
    result = es.search(index="medic-failure-signatures-*", body=query)

    print(f"Found {result['hits']['total']['value']} in_progress investigations")

    cleaned = 0
    for hit in result['hits']['hits']:
        fingerprint_id = hit['_source']['fingerprint_id']

        # Check if process/container is actually running
        # Check both old and new key formats
        old_pid_key = f"medic:investigation:{fingerprint_id}:pid"
        new_pid_key = f"medic:docker_investigation:{fingerprint_id}:pid"
        container_key = f"medic:docker_investigation:{fingerprint_id}:container_name"

        old_pid = redis_client.get(old_pid_key)
        new_pid = redis_client.get(new_pid_key)
        container_name = redis_client.get(container_key)

        # Check if in active set
        old_active = redis_client.sismember("medic:investigation:active", fingerprint_id)
        new_active = redis_client.sismember("medic:docker_investigation:active", fingerprint_id)

        is_stale = True

        # Priority 1: Check container-based tracking (new containerized execution)
        if container_name:
            try:
                result = subprocess.run(
                    ['docker', 'inspect', '--format', '{{.State.Running}}', container_name],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0 and result.stdout.strip() == 'true':
                    is_stale = False
                    print(f"✓ {fingerprint_id[:16]}... - Actually running (container {container_name})")
                else:
                    print(f"✗ {fingerprint_id[:16]}... - Stale (container {container_name} not running)")
            except Exception as e:
                print(f"✗ {fingerprint_id[:16]}... - Stale (container check failed: {e})")

        # Priority 2: Check PID-based tracking (legacy)
        elif new_pid and int(new_pid) > 0:
            # Check if process exists
            try:
                os.kill(int(new_pid), 0)  # Signal 0 checks if process exists
                is_stale = False
                print(f"✓ {fingerprint_id[:16]}... - Actually running (PID {new_pid})")
            except OSError:
                print(f"✗ {fingerprint_id[:16]}... - Stale (PID {new_pid} not running)")
        elif old_pid and int(old_pid) > 0:
            try:
                os.kill(int(old_pid), 0)
                is_stale = False
                print(f"✓ {fingerprint_id[:16]}... - Actually running (old PID {old_pid})")
            except OSError:
                print(f"✗ {fingerprint_id[:16]}... - Stale (old PID {old_pid} not running)")
        else:
            print(f"✗ {fingerprint_id[:16]}... - Stale (PID is 0 or missing, no container)")

        # If stale, mark as failed (update directly in old index)
        if is_stale:
            # Update Elasticsearch directly since we're working with old indices
            index_name = hit['_index']
            doc_id = hit['_id']

            update_body = {
                "doc": {
                    "investigation_status": "failed",
                    "status": "failed",
                    "investigation_notes": "Investigation marked as failed due to stale status (process not running)",
                    "updated_at": utc_isoformat()
                }
            }

            es.update(index=index_name, id=doc_id, body=update_body)

            # Clean up old Redis keys
            keys_to_delete = [
                f"medic:investigation:{fingerprint_id}:pid",
                f"medic:investigation:{fingerprint_id}:status",
                f"medic:investigation:{fingerprint_id}:started_at",
                f"medic:investigation:{fingerprint_id}:heartbeat",
                f"medic:investigation:{fingerprint_id}:output_lines",
                f"medic:investigation:{fingerprint_id}:result",
            ]

            for key in keys_to_delete:
                redis_client.delete(key)

            # Remove from old active set
            redis_client.srem("medic:investigation:active", fingerprint_id)

            cleaned += 1
            print(f"  → Marked as failed and cleaned up Redis keys")

    print(f"\n✅ Cleaned up {cleaned} stale investigations")

    es.close()


if __name__ == "__main__":
    asyncio.run(cleanup_stale_docker_investigations())
