#!/usr/bin/env python3
"""
Re-queue investigations that have 'queued' status in Elasticsearch but are not in the Redis queue.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import asyncio
from elasticsearch import Elasticsearch
from services.medic.docker import DockerInvestigationQueue, DockerFailureSignatureStore
import redis


async def requeue_stale_investigations():
    """Find and re-queue stale queued investigations"""

    # Connect to Elasticsearch (use service name from Docker network)
    es = Elasticsearch(['http://elasticsearch:9200'])

    # Connect to Redis (use service name from Docker network)
    redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)

    # Create queue and signature store
    queue = DockerInvestigationQueue(redis_client)
    store = DockerFailureSignatureStore(es)

    # Query for queued investigations in Elasticsearch
    query = {
        "query": {
            "term": {
                "investigation_status": "queued"
            }
        },
        "size": 100
    }

    result = es.search(index="medic-docker-failures-*", body=query)

    print(f"Found {result['hits']['total']['value']} investigations with 'queued' status in Elasticsearch")

    requeued = 0
    already_queued = 0

    for hit in result['hits']['hits']:
        fingerprint_id = hit['_source']['fingerprint_id']

        # Check if actually in Redis queue
        status = queue.get_status(fingerprint_id)

        if status == "queued":
            print(f"✓ {fingerprint_id[:20]}... - Already in queue")
            already_queued += 1
        else:
            # Not in queue, re-queue it
            success = queue.enqueue(fingerprint_id, priority="normal")

            if success:
                print(f"✓ {fingerprint_id[:20]}... - Re-queued")
                requeued += 1
            else:
                # Might be in progress or completed
                new_status = queue.get_status(fingerprint_id)
                print(f"✗ {fingerprint_id[:20]}... - Could not re-queue (status: {new_status})")

    print(f"\n✅ Re-queued: {requeued}")
    print(f"📋 Already queued: {already_queued}")
    print(f"📊 Total: {result['hits']['total']['value']}")

    # Show current queue length
    queue_len = redis_client.llen("medic:docker_investigation:queue")
    print(f"\n🔢 Current queue length: {queue_len}")

    es.close()


if __name__ == "__main__":
    asyncio.run(requeue_stale_investigations())
