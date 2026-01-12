"""
Pipeline Run Management for Orchestrator Observability

Tracks the lifecycle of an issue's journey through the workflow pipeline.
All observability events and logs are tagged with pipeline_run_id for traceability.
"""

import logging
import redis
import json
import uuid
from typing import Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass, asdict
from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)

# ILM Policy for pipeline runs (7-day retention)
PIPELINE_RUNS_ILM_POLICY = {
    "policy": {
        "phases": {
            "hot": {
                "min_age": "0ms",
                "actions": {
                    "set_priority": {
                        "priority": 100
                    }
                }
            },
            "warm": {
                "min_age": "3d",
                "actions": {
                    "set_priority": {
                        "priority": 50
                    }
                }
            },
            "delete": {
                "min_age": "7d",
                "actions": {
                    "delete": {
                        "delete_searchable_snapshot": True
                    }
                }
            }
        }
    }
}

# Index template for pipeline runs
PIPELINE_RUNS_TEMPLATE = {
    "index_patterns": ["pipeline-runs-*"],
    "template": {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 1,
            "index": {
                "lifecycle": {
                    "name": "pipeline-runs-ilm-policy"
                }
            }
        },
        "mappings": {
            "properties": {
                "id": {"type": "keyword"},
                "issue_number": {"type": "integer"},
                "issue_title": {"type": "text"},
                "issue_url": {"type": "keyword"},
                "project": {"type": "keyword"},
                "board": {"type": "keyword"},
                "started_at": {"type": "date"},
                "ended_at": {"type": "date"},
                "status": {"type": "keyword"}
            }
        }
    },
    "priority": 200
}


@dataclass
class PipelineRun:
    """
    Represents a single run of an issue through the workflow pipeline
    
    A pipeline run starts when an agent is about to be launched for an issue
    and ends when the issue reaches a column with no agent defined.
    """
    id: str
    issue_number: int
    issue_title: str
    issue_url: str
    project: str
    board: str
    started_at: str
    ended_at: Optional[str] = None
    status: str = "active"  # active, completed
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PipelineRun':
        """Create from dictionary"""
        return cls(**data)
    
    def is_active(self) -> bool:
        """Check if pipeline run is still active"""
        return self.status == "active" and self.ended_at is None


class PipelineRunManager:
    """
    Manages pipeline run lifecycle with dual storage:
    - Redis: Fast access for active pipeline runs
    - Elasticsearch: Historical persistence for analysis
    """
    
    def __init__(
        self,
        redis_client: Optional[redis.Redis] = None,
        elasticsearch_client: Optional[Elasticsearch] = None
    ):
        """
        Initialize pipeline run manager
        
        Args:
            redis_client: Redis client for fast access
            elasticsearch_client: Elasticsearch client for persistence
        """
        # Redis for fast active run lookups
        if redis_client:
            self.redis = redis_client
        else:
            self.redis = redis.Redis(
                host='redis',
                port=6379,
                decode_responses=True
            )
        
        # Elasticsearch for historical persistence
        self.es = elasticsearch_client
        if elasticsearch_client is None:
            try:
                self.es = Elasticsearch(["http://elasticsearch:9200"])
            except Exception as e:
                logger.warning(f"Failed to connect to Elasticsearch: {e}")
                self.es = None
        
        # Redis key prefix
        self.redis_prefix = "orchestrator:pipeline_run"
        self.redis_issue_mapping = "orchestrator:pipeline_run:issue_mapping"

        # Elasticsearch index pattern (date-based for ILM)
        self.es_index_pattern = "pipeline-runs"

        # Setup Elasticsearch ILM and templates if available
        if self.es:
            self._setup_elasticsearch()

        logger.info("PipelineRunManager initialized")

    def _setup_elasticsearch(self):
        """Setup Elasticsearch ILM policy and index templates"""
        if not self.es:
            return

        try:
            # Create ILM policy for pipeline runs (7-day retention)
            self.es.ilm.put_lifecycle(
                name="pipeline-runs-ilm-policy",
                body=PIPELINE_RUNS_ILM_POLICY
            )
            logger.info("Created/updated ILM policy: pipeline-runs-ilm-policy (7-day retention)")

            # Create index template for pipeline runs
            self.es.indices.put_index_template(
                name="pipeline-runs-template",
                body=PIPELINE_RUNS_TEMPLATE
            )
            logger.info("Created/updated index template: pipeline-runs-template")

        except Exception as e:
            logger.error(f"Failed to setup Elasticsearch for pipeline runs: {e}")

    def _get_es_index_name(self, date: Optional[datetime] = None) -> str:
        """
        Get date-based Elasticsearch index name for pipeline runs

        Args:
            date: Optional date to use for index name (defaults to today)

        Returns:
            Index name like 'pipeline-runs-2025-11-05'
        """
        if date is None:
            date = datetime.utcnow()
        return f"{self.es_index_pattern}-{date.strftime('%Y-%m-%d')}"

    def _get_redis_key(self, pipeline_run_id: str) -> str:
        """Get Redis key for pipeline run"""
        return f"{self.redis_prefix}:{pipeline_run_id}"
    
    def _get_issue_key(self, project: str, issue_number: int) -> str:
        """Get Redis hash field for issue mapping"""
        return f"{project}:{issue_number}"
    
    def create_pipeline_run(
        self,
        issue_number: int,
        issue_title: str,
        issue_url: str,
        project: str,
        board: str
    ) -> PipelineRun:
        """
        Create a new pipeline run
        
        Args:
            issue_number: GitHub issue number
            issue_title: Issue title
            issue_url: Issue URL
            project: Project name
            board: Board name
            
        Returns:
            New PipelineRun instance
        """
        pipeline_run_id = str(uuid.uuid4())
        
        pipeline_run = PipelineRun(
            id=pipeline_run_id,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_url=issue_url,
            project=project,
            board=board,
            started_at=datetime.utcnow().isoformat() + 'Z',
            status="active"
        )
        
        # Store in Redis for fast access
        redis_key = self._get_redis_key(pipeline_run_id)
        self.redis.setex(
            redis_key,
            7200,  # 2 hour TTL
            json.dumps(pipeline_run.to_dict())
        )
        
        # Map issue to pipeline run ID
        issue_key = self._get_issue_key(project, issue_number)
        self.redis.hset(
            self.redis_issue_mapping,
            issue_key,
            pipeline_run_id
        )
        
        # Persist to Elasticsearch
        self._persist_to_elasticsearch(pipeline_run)
        
        logger.info(
            f"Created pipeline run {pipeline_run_id} for "
            f"{project} issue #{issue_number}"
        )
        
        return pipeline_run
    
    def get_active_pipeline_run(
        self,
        project: str,
        issue_number: int
    ) -> Optional[PipelineRun]:
        """
        Get active pipeline run for an issue
        
        Args:
            project: Project name
            issue_number: Issue number
            
        Returns:
            PipelineRun if active run exists, None otherwise
        """
        # Check issue mapping
        issue_key = self._get_issue_key(project, issue_number)
        pipeline_run_id = self.redis.hget(self.redis_issue_mapping, issue_key)
        
        if not pipeline_run_id:
            # FALLBACK: Check Elasticsearch for active runs that aged out of Redis
            # This handles cases where pipeline runs are older than Redis TTL
            if self.es:
                try:
                    result = self.es.search(
                        index=f"{self.es_index_pattern}-*",
                        body={
                            "query": {
                                "bool": {
                                    "must": [
                                        {"term": {"project": project}},
                                        {"term": {"issue_number": issue_number}},
                                        {"term": {"status": "active"}}
                                    ]
                                }
                            },
                            "size": 1,
                            "sort": [{"started_at": {"order": "desc"}}]
                        }
                    )
                    
                    if result['hits']['total']['value'] > 0:
                        pipeline_run = PipelineRun.from_dict(result['hits']['hits'][0]['_source'])
                        logger.debug(f"Found active pipeline run {pipeline_run.id} in Elasticsearch (not in Redis)")
                        
                        # Restore to Redis for future lookups
                        redis_key = self._get_redis_key(pipeline_run.id)
                        self.redis.setex(
                            redis_key,
                            3600,  # 1 hour TTL
                            json.dumps(pipeline_run.to_dict())
                        )
                        self.redis.hset(self.redis_issue_mapping, issue_key, pipeline_run.id)
                        
                        return pipeline_run
                except Exception as e:
                    logger.debug(f"Error searching Elasticsearch for active pipeline run: {e}")
            
            return None
        
        # Get pipeline run data
        redis_key = self._get_redis_key(pipeline_run_id)
        data = self.redis.get(redis_key)
        
        if not data:
            # Mapping exists but data is gone - clean up
            self.redis.hdel(self.redis_issue_mapping, issue_key)
            return None
        
        try:
            pipeline_run = PipelineRun.from_dict(json.loads(data))
            
            # Verify it's still active
            if pipeline_run.is_active():
                return pipeline_run
            else:
                # Not active anymore, clean up
                self.redis.hdel(self.redis_issue_mapping, issue_key)
                return None
                
        except Exception as e:
            logger.error(f"Error deserializing pipeline run: {e}")
            return None
    
    def get_or_create_pipeline_run(
        self,
        issue_number: int,
        issue_title: str,
        issue_url: str,
        project: str,
        board: str
    ) -> PipelineRun:
        """
        Get existing active pipeline run or create a new one
        
        This method ensures that only ONE active run exists per issue by:
        1. Checking Redis for an active run
        2. If not in Redis, querying Elasticsearch for any active runs
        3. Ending any old active runs found in Elasticsearch
        4. Creating a new run if needed
        
        Args:
            issue_number: GitHub issue number
            issue_title: Issue title
            issue_url: Issue URL
            project: Project name
            board: Board name
            
        Returns:
            PipelineRun instance (existing or new)
        """
        # Use a lock to prevent race conditions when creating runs
        lock_key = f"{self.redis_prefix}:lock:{project}:{issue_number}"
        
        try:
            # Try to acquire lock (wait up to 5 seconds)
            with self.redis.lock(lock_key, timeout=5, blocking_timeout=5):
                # Check for existing active run in Redis
                existing = self.get_active_pipeline_run(project, issue_number)
                
                if existing:
                    logger.debug(
                        f"Using existing pipeline run {existing.id} for "
                        f"{project} issue #{issue_number}"
                    )
                    return existing
                
                # Redis doesn't have an active run, but Elasticsearch might have old ones
                # Query Elasticsearch for any active runs for this issue
                if self.es:
                    try:
                        query = {
                            "query": {
                                "bool": {
                                    "must": [
                                        {"term": {"project": project}},
                                        {"term": {"issue_number": issue_number}},
                                        {"term": {"status": "active"}}
                                    ]
                                }
                            },
                            "size": 100  # Get all active runs for this issue
                        }
                        
                        result = self.es.search(index="pipeline-runs-*", body=query)
                        
                        if result['hits']['total']['value'] > 0:
                            logger.warning(
                                f"Found {result['hits']['total']['value']} orphaned active pipeline runs "
                                f"in Elasticsearch for {project} issue #{issue_number}. Ending them."
                            )
                            
                            # End all old active runs
                            for hit in result['hits']['hits']:
                                old_run_data = hit['_source']
                                old_run_id = old_run_data['id']
                                
                                # Update the run in Elasticsearch to mark it as completed
                                old_run_data['ended_at'] = datetime.utcnow().isoformat() + 'Z'
                                old_run_data['status'] = 'completed'
                                
                                # Update in the same index where it was found
                                # Extract the index from the hit's _index field
                                old_index = hit['_index']
                                self.es.index(
                                    index=old_index,
                                    id=old_run_id,
                                    document=old_run_data
                                )
                                
                                logger.info(
                                    f"Ended orphaned pipeline run {old_run_id} for "
                                    f"{project} issue #{issue_number}"
                                )
                    except Exception as e:
                        logger.error(f"Error checking/ending old pipeline runs in Elasticsearch: {e}")
                
                # Create new run
                return self.create_pipeline_run(
                    issue_number=issue_number,
                    issue_title=issue_title,
                    issue_url=issue_url,
                    project=project,
                    board=board
                )
        except redis.exceptions.LockError:
            logger.warning(f"Could not acquire lock for pipeline run creation: {project} #{issue_number}")
            # Fallback: try to get existing one last time
            existing = self.get_active_pipeline_run(project, issue_number)
            if existing:
                return existing
            
            # If we can't get lock and no existing run, proceed with creation anyway
            # This is a best-effort fallback
            logger.warning("Proceeding with pipeline run creation without lock")
            return self.create_pipeline_run(
                issue_number=issue_number,
                issue_title=issue_title,
                issue_url=issue_url,
                project=project,
                board=board
            )
    
    def end_pipeline_run(
        self,
        project: str,
        issue_number: int,
        reason: Optional[str] = None
    ) -> bool:
        """
        End an active pipeline run
        
        Args:
            project: Project name
            issue_number: Issue number
            reason: Optional reason for ending (for logging)
            
        Returns:
            True if run was ended, False if no active run found
        """
        # Get active run
        pipeline_run = self.get_active_pipeline_run(project, issue_number)
        
        if not pipeline_run:
            logger.debug(
                f"No active pipeline run to end for {project} issue #{issue_number}"
            )
            return False
        
        # Mark as completed
        pipeline_run.ended_at = datetime.utcnow().isoformat() + 'Z'
        pipeline_run.status = "completed"
        
        # Update Redis
        redis_key = self._get_redis_key(pipeline_run.id)
        self.redis.setex(
            redis_key,
            3600,  # Keep for 1 hour after completion
            json.dumps(pipeline_run.to_dict())
        )
        
        # Remove from issue mapping (can't be reused)
        issue_key = self._get_issue_key(project, issue_number)
        self.redis.hdel(self.redis_issue_mapping, issue_key)
        
        # Update in Elasticsearch
        self._persist_to_elasticsearch(pipeline_run)
        
        # Release pipeline lock if this issue holds it
        # This prevents stale locks from blocking other issues when runs end due to errors
        try:
            from services.pipeline_lock_manager import get_pipeline_lock_manager
            lock_manager = get_pipeline_lock_manager()
            current_lock = lock_manager.get_lock(project, pipeline_run.board)
            if current_lock and current_lock.lock_status == 'locked' and current_lock.locked_by_issue == issue_number:
                lock_manager.release_lock(project, pipeline_run.board, issue_number)
                logger.info(f"Released pipeline lock for {project} issue #{issue_number} after ending run")
                
                # CRITICAL: Process next waiting issue in queue after lock release
                # This ensures queued issues are picked up when the current issue completes
                try:
                    from services.pipeline_queue_manager import get_pipeline_queue_manager
                    from task_queue.task_manager import Task, TaskPriority
                    import time

                    pipeline_queue = get_pipeline_queue_manager(project, pipeline_run.board)
                    next_issue = pipeline_queue.get_next_waiting_issue()
                    
                    if next_issue:
                        logger.info(f"Attempting to acquire lock for next queued issue #{next_issue['issue_number']} after #{issue_number} completed")
                        
                        # Try to acquire lock for next issue
                        acquired, acquire_reason = lock_manager.try_acquire_lock(
                            project=project,
                            board=pipeline_run.board,
                            issue_number=next_issue['issue_number']
                        )
                        
                        if acquired:
                            # CRITICAL: Mark issue active IMMEDIATELY after lock acquisition
                            # This prevents monitoring loop from seeing "issue has lock" and creating duplicate task
                            # The monitoring loop checks if issue holds lock (line 1507 in project_monitor.py)
                            # If yes, it assumes work is resuming and proceeds to create task
                            # We must mark active BEFORE that check can happen
                            pipeline_queue.mark_issue_active(next_issue['issue_number'])
                            logger.info(f"Successfully acquired lock for issue #{next_issue['issue_number']}")
                            
                            # CRITICAL: Actually dispatch the agent by creating a task
                            # Not sufficient to just acquire lock - need to enqueue task
                            # SAFETY: Track task_created for rollback if creation fails
                            task_created = False
                            try:
                                from config.manager import ConfigManager
                                config_manager = ConfigManager()
                                project_config = config_manager.get_project_config(project)
                                pipeline_config = next(p for p in project_config.pipelines if p.board_name == pipeline_run.board)
                                workflow_template = config_manager.get_workflow_template(pipeline_config.workflow)
                                
                                # SAFETY: Re-fetch issue from GitHub to verify it hasn't moved columns
                                # The queue cache might be stale if user moved the issue
                                # FIX: Use GraphQL query instead of gh issue view --json projectItems
                                # because projectItems can be stale/empty due to GitHub eventual consistency
                                actual_column = self._get_issue_column_from_github(
                                    project_config, pipeline_config, next_issue['issue_number']
                                )

                                if not actual_column:
                                    raise Exception(f"Issue #{next_issue['issue_number']} not found on board '{pipeline_run.board}'")
                                
                                # Get agent for ACTUAL current column (not cached column)
                                agent = None
                                for col in workflow_template.columns:
                                    if col.name == actual_column:
                                        agent = col.agent
                                        break
                                
                                if agent and agent != 'null':
                                    # Create task for next issue with ACTUAL column (not cached)
                                    task_context = {
                                        'project': project,
                                        'board': pipeline_run.board,
                                        'pipeline': pipeline_config.name,
                                        'repository': project_config.github['repo'],
                                        'issue_number': next_issue['issue_number'],
                                        'column': actual_column,  # Use verified actual column
                                        'trigger': 'lock_release_queue_processing',
                                        'timestamp': datetime.utcnow().isoformat() + 'Z'
                                    }
                                    
                                    # Instantiate TaskQueue using the correct import
                                    from task_queue.task_manager import TaskQueue
                                    task_queue = TaskQueue(use_redis=True)

                                    task = Task(
                                        id=f"{agent}_{project}_{pipeline_run.board}_{next_issue['issue_number']}_{int(time.time())}",
                                        agent=agent,
                                        project=project,
                                        priority=TaskPriority.MEDIUM,
                                        context=task_context,
                                        created_at=datetime.utcnow().isoformat() + 'Z'
                                    )

                                    task_queue.enqueue(task)
                                    task_created = True
                                    
                                    logger.info(
                                        f"Dispatched agent {agent} for next queued issue #{next_issue['issue_number']} "
                                        f"in column '{actual_column}'"
                                    )
                                else:
                                    raise Exception(
                                        f"Next queued issue #{next_issue['issue_number']} in column '{actual_column}' "
                                        f"has no agent configured"
                                    )
                            except Exception as dispatch_error:
                                # CRITICAL: Rollback lock acquisition if task creation failed
                                # Otherwise lock is held with no work happening (deadlock)
                                if not task_created:
                                    logger.error(
                                        f"Task creation failed for issue #{next_issue['issue_number']}, "
                                        f"rolling back lock acquisition to prevent deadlock"
                                    )
                                    try:
                                        lock_manager.release_lock(project, pipeline_run.board, next_issue['issue_number'])
                                        logger.info(f"Rolled back lock for issue #{next_issue['issue_number']}")
                                    except Exception as rollback_error:
                                        logger.error(f"Failed to rollback lock: {rollback_error}")
                                
                                logger.error(f"Error dispatching agent for next issue: {dispatch_error}")
                                import traceback
                                logger.error(traceback.format_exc())
                        else:
                            logger.info(
                                f"Could not acquire lock for next issue #{next_issue['issue_number']}: {acquire_reason}"
                            )
                    else:
                        logger.debug(f"No more issues waiting in queue for {project}/{pipeline_run.board}")
                except Exception as queue_error:
                    logger.error(f"Error processing next queued issue for {project}/{pipeline_run.board}: {queue_error}")
                    import traceback
                    logger.error(traceback.format_exc())
                    
        except Exception as e:
            logger.warning(f"Failed to release pipeline lock for {project} issue #{issue_number}: {e}")
        
        reason_msg = f" ({reason})" if reason else ""
        logger.info(
            f"Ended pipeline run {pipeline_run.id} for "
            f"{project} issue #{issue_number}{reason_msg}"
        )
        
        return True
    
    def get_pipeline_run_by_id(self, pipeline_run_id: str) -> Optional[PipelineRun]:
        """
        Get pipeline run by ID (from Redis or Elasticsearch)

        Args:
            pipeline_run_id: Pipeline run ID

        Returns:
            PipelineRun if found, None otherwise
        """
        # Try Redis first
        redis_key = self._get_redis_key(pipeline_run_id)
        data = self.redis.get(redis_key)

        if data:
            try:
                return PipelineRun.from_dict(json.loads(data))
            except Exception as e:
                logger.error(f"Error deserializing pipeline run from Redis: {e}")

        # Fall back to Elasticsearch (search across all date-based indices)
        if self.es:
            try:
                # Use search instead of get to query across date-based indices
                result = self.es.search(
                    index=f"{self.es_index_pattern}-*",
                    body={
                        "query": {
                            "term": {
                                "_id": pipeline_run_id
                            }
                        },
                        "size": 1
                    }
                )

                if result and result['hits']['total']['value'] > 0:
                    return PipelineRun.from_dict(result['hits']['hits'][0]['_source'])
            except Exception as e:
                logger.debug(f"Pipeline run {pipeline_run_id} not found in Elasticsearch: {e}")

        return None
    
    def _persist_to_elasticsearch(self, pipeline_run: PipelineRun):
        """
        Persist pipeline run to Elasticsearch (date-based index)

        Args:
            pipeline_run: PipelineRun to persist
        """
        if not self.es:
            return

        try:
            # Use date-based index name derived from started_at to ensure updates go to the same index
            index_name = self._get_es_index_name()
            try:
                if pipeline_run.started_at:
                    # Parse started_at (format: "2025-11-29T18:29:17.250Z" or similar)
                    started_at_str = pipeline_run.started_at.replace('Z', '+00:00')
                    started_date = datetime.fromisoformat(started_at_str)
                    index_name = self._get_es_index_name(started_date)
            except Exception as e:
                logger.warning(f"Could not parse started_at '{pipeline_run.started_at}', using current date for index: {e}")

            self.es.index(
                index=index_name,
                id=pipeline_run.id,
                document=pipeline_run.to_dict()
            )
            logger.debug(f"Persisted pipeline run {pipeline_run.id} to {index_name}")
        except Exception as e:
            logger.error(f"Failed to persist pipeline run to Elasticsearch: {e}")
    
    def cleanup_expired_mappings(self, max_age_seconds: int = 7200):
        """
        Clean up expired pipeline run mappings from Redis
        
        This is a maintenance function that should be called periodically.
        It removes stale mappings where the pipeline run data no longer exists.
        
        Args:
            max_age_seconds: Maximum age in seconds before cleanup
        """
        try:
            # Get all issue mappings
            all_mappings = self.redis.hgetall(self.redis_issue_mapping)
            
            cleaned = 0
            for issue_key, pipeline_run_id in all_mappings.items():
                redis_key = self._get_redis_key(pipeline_run_id)
                
                # Check if pipeline run data still exists
                if not self.redis.exists(redis_key):
                    self.redis.hdel(self.redis_issue_mapping, issue_key)
                    cleaned += 1
            
            if cleaned > 0:
                logger.info(f"Cleaned up {cleaned} expired pipeline run mappings")
                
        except Exception as e:
            logger.error(f"Error cleaning up pipeline run mappings: {e}")
    
    def cleanup_stale_active_runs_on_startup(self, retriggered_issues: set = None):
        """
        Clean up stale 'active' pipeline runs on orchestrator startup.
        
        On startup, we need to:
        1. Query Elasticsearch for all 'active' pipeline runs
        2. Check if each issue is actually in a column with an agent
        3. End runs for issues in Done/Backlog or columns without agents
        4. Keep runs active only if they're in columns with agents assigned
        
        This fixes the issue where runs remain 'active' after:
        - Orchestrator restarts
        - Issues manually moved to Done
        - Issues moved to Backlog
        
        Args:
            retriggered_issues: Set of (project, issue_number) tuples for issues
                that were just re-triggered during lock recovery. These should
                NOT be cleaned up since they're about to start work.
        """
        if not self.es:
            logger.warning("Elasticsearch not available, skipping stale pipeline run cleanup")
            return
        
        if retriggered_issues is None:
            retriggered_issues = set()
        
        try:
            from config.manager import config_manager
            import subprocess
            import json
            
            # Get all active pipeline runs from Elasticsearch
            query = {
                "query": {
                    "term": {"status": "active"}
                },
                "size": 1000  # Get all active runs
            }
            
            result = self.es.search(index=f"{self.es_index_pattern}-*", body=query)
            
            if result['hits']['total']['value'] == 0:
                logger.info("No active pipeline runs to clean up")
                return
            
            logger.info(f"Found {result['hits']['total']['value']} active pipeline runs, checking if they should be ended")
            
            ended_count = 0
            kept_active_count = 0
            
            for hit in result['hits']['hits']:
                run = hit['_source']
                original_index = hit['_index']
                pipeline_run_id = run['id']
                project = run['project']
                issue_number = run['issue_number']
                board = run['board']

                try:
                    # CRITICAL: Skip issues that were just re-triggered during lock recovery
                    # They have tasks queued but haven't started executing yet
                    if (project, issue_number) in retriggered_issues:
                        logger.info(
                            f"Skipping cleanup for {project} issue #{issue_number} "
                            f"(run {pipeline_run_id[:8]}...) - was just re-triggered during lock recovery"
                        )
                        kept_active_count += 1
                        continue
                    
                    # Get project config
                    project_config = config_manager.get_project_config(project)
                    if not project_config:
                        logger.warning(f"No config for project {project}, ending run {pipeline_run_id}")
                        self._end_run_in_elasticsearch(run, "Project config not found", original_index)
                        ended_count += 1
                        continue
                    
                    # Find pipeline config for this board
                    pipeline_config = next(
                        (p for p in project_config.pipelines if p.board_name == board),
                        None
                    )
                    
                    if not pipeline_config:
                        logger.warning(f"No pipeline config for board {board}, ending run {pipeline_run_id}")
                        self._end_run_in_elasticsearch(run, "Pipeline config not found", original_index)
                        ended_count += 1
                        continue
                    
                    # Get workflow template
                    workflow_template = config_manager.get_workflow_template(pipeline_config.workflow)
                    
                    # Get current column for this issue from GitHub Projects v2
                    # We need to query the project board to see what column the issue is in
                    current_column = self._get_issue_column_from_github(
                        project_config, pipeline_config, issue_number
                    )
                    
                    if not current_column:
                        logger.warning(
                            f"Could not determine column for issue #{issue_number}, "
                            f"ending run {pipeline_run_id} (issue may have been removed from board)"
                        )
                        self._end_run_in_elasticsearch(run, "Issue not found on board", original_index)
                        ended_count += 1
                        continue
                    
                    # Check if this column has an agent assigned
                    column_config = next(
                        (c for c in workflow_template.columns if c.name == current_column),
                        None
                    )
                    
                    if not column_config:
                        logger.warning(
                            f"Column {current_column} not in workflow, "
                            f"ending run {pipeline_run_id}"
                        )
                        self._end_run_in_elasticsearch(run, f"Column '{current_column}' not in workflow", original_index)
                        ended_count += 1
                        continue
                    
                    # CRITICAL: Determine if run should be active based on GitHub issue status
                    # A pipeline run is active if and only if:
                    # 1. Issue is NOT in an exit column (Done, Staged, etc.)
                    # 2. Issue IS in a column with an agent assigned
                    
                    # Check if issue is in an exit column
                    exit_columns = getattr(workflow_template, 'pipeline_exit_columns', [])
                    is_in_exit_column = current_column in exit_columns
                    
                    # Check if column has an agent
                    has_agent = column_config.agent and column_config.agent != 'null'
                    
                    if is_in_exit_column:
                        # Issue reached completion - end the run
                        logger.info(
                            f"Issue #{issue_number} in exit column '{current_column}', "
                            f"ending run {pipeline_run_id}"
                        )
                        self._end_run_in_elasticsearch(
                            run, 
                            f"Issue in exit column '{current_column}'", 
                            original_index
                        )
                        ended_count += 1
                    elif not has_agent:
                        # Column has no agent (e.g., Backlog) - end the run
                        logger.info(
                            f"Issue #{issue_number} in column '{current_column}' with no agent, "
                            f"ending run {pipeline_run_id}"
                        )
                        self._end_run_in_elasticsearch(
                            run, 
                            f"Issue in column '{current_column}' with no agent", 
                            original_index
                        )
                        ended_count += 1
                    else:
                        # Issue is in a column with an agent - keep run active
                        # The GitHub issue status is the source of truth
                        logger.debug(
                            f"Issue #{issue_number} in column '{current_column}' with agent {column_config.agent}, "
                            f"keeping run {pipeline_run_id} active"
                        )
                        kept_active_count += 1
                    
                except Exception as e:
                    logger.error(
                        f"Error checking pipeline run {pipeline_run_id} for "
                        f"{project} issue #{issue_number}: {e}"
                    )
                    # Keep active on error to be safe
                    kept_active_count += 1
            
            logger.info(
                f"Pipeline run cleanup complete: ended {ended_count} stale runs, "
                f"kept {kept_active_count} runs active"
            )
            
        except Exception as e:
            logger.error(f"Error during stale pipeline run cleanup: {e}")
    
    # REMOVED: _verify_pipeline_run_is_active()
    # The old approach tried to infer pipeline state from timing signals (recent activity,
    # running containers, queued tasks). This was fundamentally flawed because:
    # 1. Timing-based checks created race conditions during startup
    # 2. The "10 minute activity window" was arbitrary and unreliable
    # 3. Container/queue checks didn't account for legitimate pauses (waiting for human feedback)
    #
    # NEW APPROACH: Use GitHub issue status as the single source of truth
    # A pipeline run is active if and only if the issue is in a column with an agent
    # and NOT in an exit column. This is simple, deterministic, and testable.
    
    def _get_issue_column_from_github(self, project_config, pipeline_config, issue_number: int) -> Optional[str]:
        """
        Query GitHub Projects v2 to get the current column for an issue
        
        Args:
            project_config: Project configuration
            pipeline_config: Pipeline configuration
            issue_number: Issue number to look up
            
        Returns:
            Column name if found, None otherwise
        """
        try:
            import subprocess
            import json
            
            # Extract project number from board name (e.g., "Development Pipeline" -> number from GitHub)
            # We need to query the organization's projects to find the right one
            org = project_config.github.get('org')
            repo = project_config.github.get('repo')
            board_name = pipeline_config.board_name
            
            # Get project number from state manager
            from config.state_manager import state_manager
            github_state = state_manager.load_project_state(project_config.name)
            
            if not github_state or not github_state.boards:
                logger.warning(f"No GitHub state for project {project_config.name}")
                return None
            
            # github_state.boards is a dict, not a list
            board_state = github_state.boards.get(board_name)
            if not board_state or not board_state.project_number:
                logger.warning(f"No project number for board {board_name}")
                return None
            
            project_number = board_state.project_number
            
            # Query GitHub Projects v2 API for this specific issue
            from services.github_owner_utils import get_owner_type
            
            owner_type = get_owner_type(org)
            if owner_type is None:
                logger.error(f"Cannot query project items - unable to determine owner type for '{org}'")
                return None
            
            # Build the correct query based on owner type
            if owner_type == 'user':
                query = f'''{{
                    user(login: "{org}") {{
                        projectV2(number: {project_number}) {{
                            items(first: 100) {{
                                nodes {{
                                    content {{
                                        ... on Issue {{
                                            number
                                        }}
                                    }}
                                    fieldValues(first: 10) {{
                                        nodes {{
                                            ... on ProjectV2ItemFieldSingleSelectValue {{
                                                name
                                                field {{
                                                    ... on ProjectV2SingleSelectField {{
                                                        name
                                                    }}
                                                }}
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}'''
            else:  # organization
                query = f'''{{
                    organization(login: "{org}") {{
                        projectV2(number: {project_number}) {{
                            items(first: 100) {{
                                nodes {{
                                    content {{
                                        ... on Issue {{
                                            number
                                        }}
                                    }}
                                    fieldValues(first: 10) {{
                                        nodes {{
                                            ... on ProjectV2ItemFieldSingleSelectValue {{
                                                name
                                                field {{
                                                    ... on ProjectV2SingleSelectField {{
                                                        name
                                                    }}
                                                }}
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}'''
            
            result = subprocess.run(
                ['gh', 'api', 'graphql', '-f', f'query={query}'],
                capture_output=True, text=True, check=True, timeout=30
            )
            
            data = json.loads(result.stdout)
            
            # Get project data from the correct path based on owner type
            owner_key = 'user' if owner_type == 'user' else 'organization'
            project_data = data['data'][owner_key]['projectV2']
            
            # Find the item matching our issue number
            for node in project_data['items']['nodes']:
                content = node.get('content')
                if content and content.get('number') == issue_number:
                    # Found the issue, extract status field
                    for field_value in node['fieldValues']['nodes']:
                        if field_value and field_value.get('field', {}).get('name') == 'Status':
                            column_name = field_value.get('name')
                            logger.debug(f"Found issue #{issue_number} in column '{column_name}'")
                            return column_name
            
            logger.debug(f"Issue #{issue_number} not found on board {board_name}")
            return None
            
        except subprocess.CalledProcessError as e:
            logger.error(f"GraphQL query failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Error querying issue column: {e}")
            return None
    
    def _end_run_in_elasticsearch(self, run_data: Dict[str, Any], reason: str, index: Optional[str] = None):
        """
        End a pipeline run directly in Elasticsearch (cleanup helper)

        Args:
            run_data: Pipeline run data from Elasticsearch
            reason: Reason for ending (for logging)
            index: Optional index name where the document exists (if not provided, uses started_at or today's index)
        """
        try:
            pipeline_run_id = run_data['id']
            run_data['ended_at'] = datetime.utcnow().isoformat() + 'Z'
            run_data['status'] = 'completed'

            # Use the provided index (where the document was found) or derive from started_at
            target_index = index
            if not target_index:
                try:
                    started_at = run_data.get('started_at')
                    if started_at:
                        started_at_str = started_at.replace('Z', '+00:00')
                        started_date = datetime.fromisoformat(started_at_str)
                        target_index = self._get_es_index_name(started_date)
                except Exception as e:
                    logger.warning(f"Could not parse started_at '{run_data.get('started_at')}', using current date for index: {e}")
            
            if not target_index:
                target_index = self._get_es_index_name()

            self.es.index(
                index=target_index,
                id=pipeline_run_id,
                document=run_data
            )
            
            # Also clean up Redis if it exists
            redis_key = self._get_redis_key(pipeline_run_id)
            if self.redis.exists(redis_key):
                self.redis.setex(
                    redis_key,
                    3600,  # Keep for 1 hour after completion
                    json.dumps(run_data)
                )
            
            # Remove from issue mapping
            project = run_data['project']
            issue_number = run_data['issue_number']
            issue_key = self._get_issue_key(project, issue_number)
            self.redis.hdel(self.redis_issue_mapping, issue_key)
            
            logger.info(f"Ended stale pipeline run {pipeline_run_id}: {reason}")
            
        except Exception as e:
            logger.error(f"Error ending pipeline run {run_data.get('id')}: {e}")


# Global pipeline run manager instance
_pipeline_run_manager: Optional[PipelineRunManager] = None


def get_pipeline_run_manager() -> PipelineRunManager:
    """
    Get or create global PipelineRunManager instance
    
    Returns:
        PipelineRunManager instance
    """
    global _pipeline_run_manager
    
    if _pipeline_run_manager is None:
        _pipeline_run_manager = PipelineRunManager()
    
    return _pipeline_run_manager
