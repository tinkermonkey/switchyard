import redis
import json
import os
import sys

redis_host = os.environ.get('REDIS_HOST', 'localhost')
redis_port = int(os.environ.get('REDIS_PORT', 6379))

run_id = "b624a0f7-b7c6-4398-996e-46e9b7a7107b"
if len(sys.argv) > 1:
    run_id = sys.argv[1]

try:
    r = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
    
    key = f"orchestrator:pipeline_run:{run_id}"
    
    print(f"Checking key: {key}")
    data = r.get(key)
    
    if data:
        print("Found data:")
        print(json.dumps(json.loads(data), indent=2))
    else:
        print("No data found for this run ID.")
        
except Exception as e:
    print(f"Error: {e}")
