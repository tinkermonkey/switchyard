#!/usr/bin/env python3
"""Test script to properly enqueue a dev_environment_setup task"""
import sys
sys.path.insert(0, '/app')

from datetime import datetime
from task_queue.task_manager import TaskQueue, Task, TaskPriority

# Initialize task queue with Redis
task_queue = TaskQueue(use_redis=True)

# Create a dev_environment_setup task
task = Task(
    id=f'dev_env_setup_context-studio_{int(datetime.now().timestamp())}',
    agent='dev_environment_setup',
    project='context-studio',
    priority=TaskPriority.HIGH,
    context={
        'issue': {
            'title': 'Development environment setup for context-studio',
            'body': 'Build and validate Dockerfile.agent with automated dependency troubleshooting',
            'number': 0
        },
        'issue_number': 0,
        'board': 'system',
        'repository': 'context-studio',
        'automated_setup': True,
        'use_docker': False  # Run locally in orchestrator environment to access Docker
    },
    created_at=datetime.now().isoformat()
)

# Enqueue the task
print(f"Enqueuing task: {task.id}")
print(f"Priority: {task.priority} (name: {task.priority.name}, value: {task.priority.value})")
print(f"Queue name will be: tasks:{task.priority.name.lower()}")
try:
    task_queue.enqueue(task)
    print("Task enqueued successfully")
except Exception as e:
    print(f"ERROR enqueuing: {e}")
    import traceback
    traceback.print_exc()

# Verify it's in the queue
pending = task_queue.get_pending_tasks()
print(f"\nPending tasks: {len(pending)}")
for t in pending:
    print(f"  - {t.id}: {t.agent} for {t.project} (priority: {t.priority.name})")
