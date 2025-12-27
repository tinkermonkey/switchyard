import redis
import json
import os
import sys

redis_host = os.environ.get('REDIS_HOST', 'localhost')
redis_port = int(os.environ.get('REDIS_PORT', 6379))

try:
    r = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
    
    print("Checking task queues...")
    
    queues = ['tasks:high', 'tasks:medium', 'tasks:low']
    found_tasks = False
    
    for q in queues:
        task_ids = r.lrange(q, 0, -1)
        if task_ids:
            print(f"Queue {q} has {len(task_ids)} tasks:")
            for tid in task_ids:
                task_key = f"task:{tid}"
                task_data = r.hgetall(task_key)
                
                # Parse context if it's a string
                context = task_data.get('context')
                if isinstance(context, str):
                    try:
                        context = json.loads(context)
                    except:
                        pass
                
                issue_num = context.get('issue_number') if isinstance(context, dict) else 'N/A'
                project = task_data.get('project')
                agent = task_data.get('agent')
                
                print(f"  Task ID: {tid}")
                print(f"    Project: {project}")
                print(f"    Agent: {agent}")
                print(f"    Issue: {issue_num}")
                
                if str(issue_num) == "41":
                    print("    *** FOUND ISSUE #41 ***")
                    print(json.dumps(task_data, indent=2))
                
                found_tasks = True
        else:
            print(f"Queue {q} is empty.")
            
    if not found_tasks:
        print("No pending tasks found.")

except Exception as e:
    print(f"Error: {e}")
