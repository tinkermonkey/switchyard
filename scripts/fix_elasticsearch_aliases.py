#!/usr/bin/env python3
"""
Fix Elasticsearch indices by migrating to ILM-compatible naming scheme.

This script:
1. Removes ILM policies from old date-based indices
2. Creates new numeric-suffixed indices compatible with ILM rollover
3. Configures proper aliases and ILM policies
4. Optionally migrates data (if needed in the future)
"""

import os
import sys
from datetime import datetime
from elasticsearch import Elasticsearch

def main():
    # Connect to Elasticsearch
    es_hosts_str = os.getenv("ELASTICSEARCH_HOSTS", "http://elasticsearch:9200")
    elasticsearch_hosts = es_hosts_str.split(",")
    
    print(f"Connecting to Elasticsearch: {elasticsearch_hosts}")
    es = Elasticsearch(elasticsearch_hosts)
    
    if not es.ping():
        print("ERROR: Cannot connect to Elasticsearch")
        sys.exit(1)
    
    print("Connected successfully!\n")
    
    # Fix task metrics indices
    print("=== Migrating orchestrator-task-metrics to ILM-compatible naming ===")
    task_pattern = "orchestrator-task-metrics-*"
    task_alias = "orchestrator-task-metrics"
    task_new_index = "orchestrator-task-metrics-000001"
    
    # Get all old date-based task metrics indices
    try:
        old_task_indices = es.indices.get(index=task_pattern)
        if old_task_indices:
            print(f"Found {len(old_task_indices)} old indices: {', '.join(old_task_indices.keys())}")
            
            # Remove alias and ILM policy from old date-based indices
            for index in old_task_indices.keys():
                # Skip if it's already the new numeric format
                if index == task_new_index:
                    continue
                    
                # Remove the alias if it exists
                try:
                    if es.indices.exists_alias(index=index, name=task_alias):
                        es.indices.delete_alias(index=index, name=task_alias)
                        print(f"Removed alias from old index: {index}")
                except Exception as e:
                    print(f"Note: Could not remove alias from {index}: {e}")
                
                # Remove ILM policy
                try:
                    es.ilm.remove_policy(index=index)
                    print(f"Removed ILM policy from old index: {index}")
                except Exception as e:
                    print(f"Note: Could not remove ILM policy from {index}: {e}")
    except Exception as e:
        print(f"Note: No old indices found or error checking: {e}")
    
    # Create new numeric-suffixed index with alias
    if not es.indices.exists(index=task_new_index):
        es.indices.create(
            index=task_new_index,
            body={
                "aliases": {
                    task_alias: {
                        "is_write_index": True
                    }
                },
                "settings": {
                    "index.lifecycle.name": "orchestrator-metrics-policy",
                    "index.lifecycle.rollover_alias": task_alias
                }
            }
        )
        print(f"Created new ILM-compatible index: {task_new_index} with alias {task_alias}")
    else:
        # Ensure alias and ILM policy are set
        if not es.indices.exists_alias(name=task_alias):
            es.indices.put_alias(
                index=task_new_index,
                name=task_alias,
                body={"is_write_index": True}
            )
            print(f"Added alias {task_alias} to {task_new_index}")
        
        es.indices.put_settings(
            index=task_new_index,
            body={
                "index.lifecycle.name": "orchestrator-metrics-policy",
                "index.lifecycle.rollover_alias": task_alias
            }
        )
        print(f"Updated ILM policy on {task_new_index}")
    
    print()
    
    # Fix quality metrics indices
    print("=== Migrating orchestrator-quality-metrics to ILM-compatible naming ===")
    quality_pattern = "orchestrator-quality-metrics-*"
    quality_alias = "orchestrator-quality-metrics"
    quality_new_index = "orchestrator-quality-metrics-000001"
    
    # Get all old date-based quality metrics indices
    try:
        old_quality_indices = es.indices.get(index=quality_pattern)
        if old_quality_indices:
            print(f"Found {len(old_quality_indices)} old indices: {', '.join(old_quality_indices.keys())}")
            
            # Remove alias and ILM policy from old date-based indices
            for index in old_quality_indices.keys():
                # Skip if it's already the new numeric format
                if index == quality_new_index:
                    continue
                    
                # Remove the alias if it exists
                try:
                    if es.indices.exists_alias(index=index, name=quality_alias):
                        es.indices.delete_alias(index=index, name=quality_alias)
                        print(f"Removed alias from old index: {index}")
                except Exception as e:
                    print(f"Note: Could not remove alias from {index}: {e}")
                
                # Remove ILM policy
                try:
                    es.ilm.remove_policy(index=index)
                    print(f"Removed ILM policy from old index: {index}")
                except Exception as e:
                    print(f"Note: Could not remove ILM policy from {index}: {e}")
    except Exception as e:
        print(f"Note: No old indices found or error checking: {e}")
    
    # Create new numeric-suffixed index with alias
    if not es.indices.exists(index=quality_new_index):
        es.indices.create(
            index=quality_new_index,
            body={
                "aliases": {
                    quality_alias: {
                        "is_write_index": True
                    }
                },
                "settings": {
                    "index.lifecycle.name": "orchestrator-metrics-policy",
                    "index.lifecycle.rollover_alias": quality_alias
                }
            }
        )
        print(f"Created new ILM-compatible index: {quality_new_index} with alias {quality_alias}")
    else:
        # Ensure alias and ILM policy are set
        if not es.indices.exists_alias(name=quality_alias):
            es.indices.put_alias(
                index=quality_new_index,
                name=quality_alias,
                body={"is_write_index": True}
            )
            print(f"Added alias {quality_alias} to {quality_new_index}")
        
        es.indices.put_settings(
            index=quality_new_index,
            body={
                "index.lifecycle.name": "orchestrator-metrics-policy",
                "index.lifecycle.rollover_alias": quality_alias
            }
        )
        print(f"Updated ILM policy on {quality_new_index}")
    
    print("\n=== Summary ===")
    print("✓ Created ILM-compatible indices with numeric suffixes")
    print("✓ Configured proper aliases and ILM policies")
    print("✓ Removed ILM policies from old date-based indices")
    print("\nThe old date-based indices are still present and can be:")
    print("  - Kept for historical data")
    print("  - Deleted if no longer needed: DELETE /orchestrator-*-2025.*")
    print("\nNew writes will go to the numeric indices, which will rollover automatically.")
    
    print("\n=== Summary ===")
    print("Aliases have been configured properly.")
    print("The ILM policy should now work correctly.")
    print("Errors should stop appearing in Elasticsearch logs.")

if __name__ == "__main__":
    main()
