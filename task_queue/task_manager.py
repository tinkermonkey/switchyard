import redis
import json
import queue
import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict
from enum import Enum
from datetime import datetime

logger = logging.getLogger(__name__)

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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Task':
        return cls(**data)

class TaskQueue:
    def __init__(self, use_redis: bool = False):
        self.use_redis = use_redis
        self.redis_client = None
        self.fallback_queues = {
            TaskPriority.HIGH: queue.PriorityQueue(),
            TaskPriority.MEDIUM: queue.PriorityQueue(),
            TaskPriority.LOW: queue.PriorityQueue()
        }

        if use_redis:
            try:
                self.redis_client = redis.Redis(
                    host='redis',
                    port=6379,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=5
                )
                # Test connection
                self.redis_client.ping()
                logger.info("Connected to Redis for task queue")
            except Exception as e:
                logger.warning(f"Redis connection failed, using in-memory fallback: {e}")
                self.redis_client = None
                self.use_redis = False
        
    def enqueue(self, task: Task):
        """Add task to appropriate priority queue"""
        if self.redis_client:
            self._enqueue_redis(task)
        else:
            self._enqueue_fallback(task)

    def _enqueue_redis(self, task: Task):
        """Enqueue task using Redis"""
        queue_name = f"tasks:{task.priority.name.lower()}"

        # Store task data (serialize enum values)
        task_data = {
            'id': task.id,
            'agent': task.agent,
            'project': task.project,
            'priority': task.priority.name,  # Convert enum to string
            'context': json.dumps(task.context),  # Properly serialize dict
            'created_at': task.created_at,
            'status': task.status
        }

        self.redis_client.hset(
            f"task:{task.id}",
            mapping=task_data
        )

        # Add to priority queue
        self.redis_client.lpush(queue_name, task.id)

    def _enqueue_fallback(self, task: Task):
        """Enqueue task using in-memory fallback"""
        # Priority queue orders by (priority_value, timestamp)
        timestamp = datetime.now().timestamp()
        self.fallback_queues[task.priority].put((task.priority.value, timestamp, task))
        
    def dequeue(self) -> Optional[Task]:
        """Get highest priority task"""
        if self.redis_client:
            return self._dequeue_redis()
        else:
            return self._dequeue_fallback()

    def _dequeue_redis(self) -> Optional[Task]:
        """Dequeue task using Redis"""
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
                    context=json.loads(task_data['context']),  # Safely deserialize dict
                    created_at=task_data['created_at'],
                    status=task_data['status']
                )

        return None

    def _dequeue_fallback(self) -> Optional[Task]:
        """Dequeue task using in-memory fallback"""
        # Check queues in priority order
        for priority in TaskPriority:
            q = self.fallback_queues[priority]
            if not q.empty():
                try:
                    priority_value, timestamp, task = q.get_nowait()
                    return task
                except queue.Empty:
                    continue
        return None
    
    def get_pending_tasks(self, agent: str = None) -> list:
        """Get all pending tasks, optionally filtered by agent"""
        if self.redis_client:
            return self._get_pending_tasks_redis(agent)
        else:
            return self._get_pending_tasks_fallback(agent)

    def _get_pending_tasks_redis(self, agent: str = None) -> list:
        """Get pending tasks using Redis"""
        tasks = []
        for priority in TaskPriority:
            queue_name = f"tasks:{priority.name.lower()}"
            task_ids = self.redis_client.lrange(queue_name, 0, -1)

            for task_id in task_ids:
                task_data = self.redis_client.hgetall(f"task:{task_id}")
                if agent is None or task_data.get('agent') == agent:
                    # Reconstruct Task with proper types
                    task = Task(
                        id=task_data['id'],
                        agent=task_data['agent'],
                        project=task_data['project'],
                        priority=TaskPriority[task_data['priority']],
                        context=json.loads(task_data['context']),
                        created_at=task_data['created_at'],
                        status=task_data['status']
                    )
                    tasks.append(task)

        return tasks

    def _get_pending_tasks_fallback(self, agent: str = None) -> list:
        """Get pending tasks using in-memory fallback"""
        tasks = []
        for priority in TaskPriority:
            q = self.fallback_queues[priority]
            # Create a temporary list to examine queue contents
            temp_items = []

            # Extract all items from queue
            while not q.empty():
                try:
                    item = q.get_nowait()
                    temp_items.append(item)
                    priority_value, timestamp, task = item
                    if agent is None or task.agent == agent:
                        tasks.append(task)
                except queue.Empty:
                    break

            # Put items back in queue
            for item in temp_items:
                q.put(item)

        return tasks

    def clear_all(self):
        """Clear all tasks from all queues (useful for testing)"""
        if self.redis_client:
            # Clear Redis
            for priority in TaskPriority:
                queue_name = f"tasks:{priority.name.lower()}"
                self.redis_client.delete(queue_name)
        else:
            # Clear in-memory queues
            for priority in TaskPriority:
                while not self.fallback_queues[priority].empty():
                    try:
                        self.fallback_queues[priority].get_nowait()
                    except queue.Empty:
                        break