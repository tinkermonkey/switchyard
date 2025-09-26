import redis
from typing import Dict, Any
from dataclasses import dataclass
from enum import Enum

class TaskPriority(Enum):
    HIGH = 1    # PR reviews, blocking issues
    MEDIUM = 2  # Development tasks
    LOW = 3     # Documentation, cleanup

@dataclass
class Task:
    id: str
    agent: str
    project: str
    priority: TaskPriority
    context: Dict[str, Any]
    created_at: str
    status: str = "pending"
    
class TaskQueue:
    def __init__(self):
        self.redis_client = redis.Redis(
            host='localhost', 
            port=6379, 
            decode_responses=True
        )
        
    def enqueue(self, task: Task):
        """Add task to appropriate priority queue"""
        queue_name = f"tasks:{task.priority.name.lower()}"

        # Store task data (serialize enum values)
        task_data = {
            'id': task.id,
            'agent': task.agent,
            'project': task.project,
            'priority': task.priority.name,  # Convert enum to string
            'context': str(task.context),  # Convert dict to string
            'created_at': task.created_at,
            'status': task.status
        }

        self.redis_client.hset(
            f"task:{task.id}",
            mapping=task_data
        )

        # Add to priority queue
        self.redis_client.lpush(queue_name, task.id)
        
    def dequeue(self) -> Task:
        """Get highest priority task"""
        import json

        for priority in TaskPriority:
            queue_name = f"tasks:{priority.name.lower()}"
            task_id = self.redis_client.rpop(queue_name)

            if task_id:
                task_data = self.redis_client.hgetall(f"task:{task_id}")

                # Reconstruct Task object with proper types
                return Task(
                    id=task_data['id'],
                    agent=task_data['agent'],
                    project=task_data['project'],
                    priority=TaskPriority[task_data['priority']],  # Convert back to enum
                    context=eval(task_data['context']),  # Convert back to dict (unsafe but simple)
                    created_at=task_data['created_at'],
                    status=task_data['status']
                )

        return None
    
    def get_pending_tasks(self, agent: str = None) -> list:
        """Get all pending tasks, optionally filtered by agent"""
        tasks = []
        for priority in TaskPriority:
            queue_name = f"tasks:{priority.name.lower()}"
            task_ids = self.redis_client.lrange(queue_name, 0, -1)
            
            for task_id in task_ids:
                task_data = self.redis_client.hgetall(f"task:{task_id}")
                if agent is None or task_data.get('agent') == agent:
                    tasks.append(Task(**task_data))
        
        return tasks