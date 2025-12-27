import redis
import json
import os

redis_host = os.environ.get('REDIS_HOST', 'redis')
redis_port = int(os.environ.get('REDIS_PORT', 6379))

try:
    r = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
    
    run_id = "0b2d120f-a482-4ed3-87cc-7e92d94815fe"
    key = f"orchestrator:pipeline_run:{run_id}"
    
    print(f"Checking key: {key}")
    data = r.get(key)
    
    if data:
        print("Found data:")
        print(json.dumps(json.loads(data), indent=2))
    else:
        print("No data found for this run ID.")
        
    print("\nListing all pipeline run keys:")
    keys = r.keys("orchestrator:pipeline_run:*")
    for k in keys:
        print(k)
        
except Exception as e:
    print(f"Error: {e}")
