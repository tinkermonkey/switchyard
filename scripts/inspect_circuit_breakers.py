
import redis
import json
import os
import sys

def inspect_circuit_breakers():
    # Try to connect to Redis
    redis_host = os.environ.get('REDIS_HOST', 'localhost')
    redis_port = int(os.environ.get('REDIS_PORT', 6379))
    
    print(f"Connecting to Redis at {redis_host}:{redis_port}...")
    
    try:
        r = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
        r.ping()
        print("Connected successfully.")
    except Exception as e:
        print(f"Failed to connect to Redis: {e}")
        return

    # Scan for circuit breaker keys
    cursor = '0'
    keys = []
    while cursor != 0:
        cursor, new_keys = r.scan(cursor=cursor, match="circuit_breaker:*:state", count=100)
        keys.extend(new_keys)

    if not keys:
        print("No circuit breaker states found in Redis.")
        return

    print(f"\nFound {len(keys)} circuit breaker states:")
    print("-" * 50)

    for key in keys:
        try:
            data = r.get(key)
            if data:
                state = json.loads(data)
                name = state.get('name', 'unknown') # Name might not be in the json, but it is in the key
                # Extract name from key if needed: circuit_breaker:{name}:state
                key_parts = key.split(':')
                if len(key_parts) >= 3:
                    extracted_name = key_parts[1]
                else:
                    extracted_name = "unknown"
                
                cb_state = state.get('state', 'unknown')
                failures = state.get('failure_count', 0)
                
                print(f"Name: {extracted_name}")
                print(f"State: {cb_state}")
                print(f"Failures: {failures}")
                print(f"Last Failure: {state.get('last_failure_time')}")
                print(f"Total Failures: {state.get('total_failures')}")
                print("-" * 50)
        except Exception as e:
            print(f"Error reading key {key}: {e}")

if __name__ == "__main__":
    inspect_circuit_breakers()
