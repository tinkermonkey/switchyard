#!/usr/bin/env python3
"""
Diagnostic script to find orphaned and duplicate occurrence data in Elasticsearch.

This script identifies:
1. Duplicate fingerprint_ids across indices
2. Total occurrence counts from all documents vs. unique signatures
3. Orphaned data that should be cleaned up
"""

import os
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from elasticsearch import Elasticsearch
from collections import defaultdict
import json


def main():
    # Connect to Elasticsearch
    es_host = os.getenv('ELASTICSEARCH_HOST', 'elasticsearch')
    es_port = int(os.getenv('ELASTICSEARCH_PORT', 9200))

    print(f"Connecting to Elasticsearch at {es_host}:{es_port}...")
    es = Elasticsearch([f"http://{es_host}:{es_port}"])

    if not es.ping():
        print("ERROR: Cannot connect to Elasticsearch")
        return 1

    print("✓ Connected to Elasticsearch\n")

    # Get all documents from medic-docker-failures-* indices
    print("Fetching all failure signatures from medic-docker-failures-*...")

    # Use scroll API to get all documents
    query = {
        "query": {"match_all": {}},
        "size": 1000,
        "_source": ["fingerprint_id", "occurrence_count", "status", "created_at", "last_seen"]
    }

    page = es.search(
        index="medic-docker-failures-*",
        scroll='2m',
        body=query
    )

    scroll_id = page['_scroll_id']
    total_docs = page['hits']['total']['value']

    print(f"Total documents in Elasticsearch: {total_docs}\n")

    # Track fingerprints and their documents
    fingerprint_docs = defaultdict(list)
    total_occurrence_sum = 0

    hits = page['hits']['hits']
    while hits:
        for hit in hits:
            source = hit['_source']
            index_name = hit['_index']
            doc_id = hit['_id']
            fingerprint_id = source.get('fingerprint_id')
            occurrence_count = source.get('occurrence_count', 0)

            fingerprint_docs[fingerprint_id].append({
                'index': index_name,
                'doc_id': doc_id,
                'occurrence_count': occurrence_count,
                'status': source.get('status'),
                'created_at': source.get('created_at'),
                'last_seen': source.get('last_seen')
            })

            total_occurrence_sum += occurrence_count

        # Get next page
        page = es.scroll(scroll_id=scroll_id, scroll='2m')
        scroll_id = page['_scroll_id']
        hits = page['hits']['hits']

    # Clear scroll
    es.clear_scroll(scroll_id=scroll_id)

    # Analysis
    print("="*80)
    print("ANALYSIS RESULTS")
    print("="*80)
    print()

    unique_fingerprints = len(fingerprint_docs)
    duplicates = {fp: docs for fp, docs in fingerprint_docs.items() if len(docs) > 1}

    print(f"Total documents in ES: {total_docs}")
    print(f"Unique fingerprint_ids: {unique_fingerprints}")
    print(f"Fingerprints with duplicates: {len(duplicates)}")
    print()

    print(f"Sum of occurrence_count from all documents: {total_occurrence_sum}")

    # Calculate correct total (sum from unique fingerprints, taking max occurrence_count)
    correct_total = 0
    for fp, docs in fingerprint_docs.items():
        # Take the highest occurrence count from duplicates
        max_count = max(doc['occurrence_count'] for doc in docs)
        correct_total += max_count

    print(f"Correct total (from unique signatures): {correct_total}")
    print(f"Difference (orphaned occurrences): {total_occurrence_sum - correct_total}")
    print()

    # Show duplicates
    if duplicates:
        print("="*80)
        print(f"DUPLICATE FINGERPRINTS ({len(duplicates)})")
        print("="*80)
        print()

        for fp, docs in sorted(duplicates.items(), key=lambda x: len(x[1]), reverse=True)[:20]:
            print(f"Fingerprint: {fp}")
            print(f"  Number of duplicate documents: {len(docs)}")
            for i, doc in enumerate(docs, 1):
                print(f"  [{i}] Index: {doc['index']}")
                print(f"      Doc ID: {doc['doc_id']}")
                print(f"      Occurrences: {doc['occurrence_count']}")
                print(f"      Status: {doc['status']}")
                print(f"      Created: {doc['created_at']}")
                print(f"      Last Seen: {doc['last_seen']}")
            print()

        if len(duplicates) > 20:
            print(f"... and {len(duplicates) - 20} more duplicates")
            print()

    # Breakdown by status
    print("="*80)
    print("BREAKDOWN BY STATUS")
    print("="*80)
    print()

    status_counts = defaultdict(int)
    status_occurrences = defaultdict(int)

    for fp, docs in fingerprint_docs.items():
        # Use the most recent document's status
        latest_doc = max(docs, key=lambda d: d.get('last_seen', ''))
        status = latest_doc['status']
        status_counts[status] += 1

        # Use max occurrence count
        max_count = max(doc['occurrence_count'] for doc in docs)
        status_occurrences[status] += max_count

    for status in sorted(status_counts.keys()):
        print(f"{status:15s}: {status_counts[status]:5d} signatures, {status_occurrences[status]:6d} occurrences")

    print()
    print("="*80)

    return 0


if __name__ == '__main__':
    sys.exit(main())
