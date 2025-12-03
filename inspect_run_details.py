import sys
import json
import redis
from elasticsearch import Elasticsearch
from services.pipeline_run import PipelineRunManager
from monitoring.observability import ObservabilityManager

def inspect_run(run_id):
    print(f"Inspecting pipeline run: {run_id}")
    
    # Connect to localhost services
    redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)
    es_client = Elasticsearch(["http://localhost:9200"])
    
    manager = PipelineRunManager(redis_client=redis_client, elasticsearch_client=es_client)
    
    # 1. Check Redis (Active)
    try:
        run = manager.get_pipeline_run_by_id(run_id)
        if run:
            print("\n=== Redis / Active State ===")
            print(json.dumps(run.to_dict(), indent=2))
        else:
            print("\n=== Redis / Active State ===")
            print("Not found in Redis (likely not active)")
    except Exception as e:
        print(f"Error checking Redis: {e}")

    # 2. Check Elasticsearch (History)
    print("\n=== Elasticsearch / History ===")
    try:
        # Search across all pipeline-runs-* indices
        query = {
            "query": {
                "term": {
                    "id": run_id
                }
            }
        }
        res = es_client.search(index="pipeline-runs-*", body=query)
        hits = res['hits']['hits']
        if hits:
            print(f"Found {len(hits)} records in Elasticsearch:")
            for hit in hits:
                print(f"\nIndex: {hit['_index']}")
                print(json.dumps(hit['_source'], indent=2))
        else:
            print("Not found in Elasticsearch")
            
    except Exception as e:
        print(f"Error querying Elasticsearch: {e}")

    # 3. Check Decision Events
    print("\n=== Decision Events ===")
    try:
        query = {
            "query": {
                "term": {
                    "pipeline_run_id": run_id
                }
            },
            "sort": [
                {"timestamp": {"order": "asc"}}
            ],
            "size": 100
        }
        res = es_client.search(index="decision-events-*", body=query)
        hits = res['hits']['hits']
        if hits:
            print(f"Found {len(hits)} events:")
            for hit in hits:
                source = hit['_source']
                print(f"[{source.get('timestamp')}] {source.get('event_type')} - {source.get('agent', 'unknown')}")
                # Print details if it's a failure
                if source.get('event_type') == 'agent_failed':
                    print(f"  Error: {source.get('error')}")
        else:
            print("No events found for this run.")

    except Exception as e:
        print(f"Error querying Decision Events: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python inspect_run_details.py <pipeline_run_id>")
        sys.exit(1)
        
    inspect_run(sys.argv[1])
