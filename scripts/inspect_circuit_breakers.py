
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
                # The JSON body's own 'name' field is authoritative (it's exactly
                # self.name from services/circuit_breaker.py at save time) — prefer
                # it over re-deriving from the key. Key-based extraction is only a
                # fallback for older entries saved without a 'name' field.
                #
                # Key format is circuit_breaker:{project}:{agent}:state as of #42
                # (project:agent_name scoping); circuit_breaker:{agent}:state
                # (pre-#42) may still be present for up to its 24h TTL after
                # deploy, so handle both shapes rather than assuming one.
                key_parts = key.split(':')
                if len(key_parts) >= 4:
                    extracted_name = ':'.join(key_parts[1:-1])  # project:agent (colon-joined, in case of nesting)
                elif len(key_parts) == 3:
                    extracted_name = key_parts[1]  # pre-#42 bare agent name
                else:
                    extracted_name = "unknown"

                display_name = state.get('name') or extracted_name

                cb_state = state.get('state', 'unknown')
                failures = state.get('failure_count', 0)

                print(f"Name: {display_name}")
                print(f"State: {cb_state}")
                print(f"Failures: {failures}")
                print(f"Last Failure: {state.get('last_failure_time')}")
                print(f"Total Failures: {state.get('total_failures')}")
                print("-" * 50)
        except Exception as e:
            print(f"Error reading key {key}: {e}")

if __name__ == "__main__":
    inspect_circuit_breakers()
