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
                                {"term": {"project.keyword": project}},
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
    
    def cleanup_stale_active_runs_on_startup(self):
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
        """
        if not self.es:
            logger.warning("Elasticsearch not available, skipping stale pipeline run cleanup")
            return
        
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
                    
                    # Check if column has an agent
                    has_agent = column_config.agent and column_config.agent != 'null'
                    
                    if not has_agent:
                        logger.info(
                            f"Issue #{issue_number} in column '{current_column}' with no agent, "
                            f"ending run {pipeline_run_id}"
                        )
                        self._end_run_in_elasticsearch(run, f"Issue in column '{current_column}' with no agent", original_index)
                        ended_count += 1
                    else:
                        # Column has an agent - now verify work is actually in progress
                        is_actually_active = self._verify_pipeline_run_is_active(
                            pipeline_run_id, project, issue_number, current_column
                        )
                        
                        if is_actually_active:
                            logger.debug(
                                f"Issue #{issue_number} in column '{current_column}' with agent {column_config.agent}, "
                                f"keeping run {pipeline_run_id} active (work verified)"
                            )
                            kept_active_count += 1
                        else:
                            logger.info(
                                f"Issue #{issue_number} in column '{current_column}' appears stalled "
                                f"(no active agents, no queued tasks), ending run {pipeline_run_id}"
                            )
                            self._end_run_in_elasticsearch(run, "No work in progress detected", original_index)
                            ended_count += 1
                    
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
    
    def _verify_pipeline_run_is_active(self, pipeline_run_id: str, project: str, 
                                       issue_number: int, current_column: str) -> bool:
        """
        Verify that a pipeline run actually has work in progress.
        
        Checks:
        1. Are there any active agent containers running in Docker for this issue?
        2. Are there any active agents in Redis tracking for this issue?
        3. Are there any tasks in the queue for this issue?
        4. Has there been any recent activity (agent events in last 10 minutes)?
        
        Args:
            pipeline_run_id: Pipeline run ID
            project: Project name
            issue_number: Issue number
            current_column: Current column name
            
        Returns:
            True if work is actually in progress, False if stalled
        """
        try:
            import redis
            import subprocess
            from datetime import datetime, timedelta
            
            # Check 0: CRITICAL - Check execution state FIRST before looking at containers
            # If there's an in_progress execution, the run is definitely active
            # This prevents race conditions during startup where containers might be
            # in the process of being recovered
            try:
                from services.work_execution_state import work_execution_tracker
                execution_history = work_execution_tracker.get_execution_history(project, issue_number)

                # Check if there's any in_progress execution
                for execution in reversed(execution_history):
                    if execution.get('outcome') == 'in_progress':
                        logger.info(
                            f"Pipeline run {pipeline_run_id} has in_progress execution: "
                            f"agent={execution.get('agent')}, column={execution.get('column')}"
                        )
                        return True
            except Exception as e:
                logger.warning(f"Error checking execution state: {e}")

            # Check 1: Are there actual Docker containers running for this issue?
            # CRITICAL FIX: Use proper container name parsing to handle projects with dashes
            try:
                # Get ALL agent containers and parse them properly
                # We can't use precise Docker filters because project names can contain dashes
                result = subprocess.run(
                    ['docker', 'ps', '--filter', 'name=claude-agent-',
                     '--format', '{{.Names}}'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )

                if result.returncode == 0 and result.stdout.strip():
                    container_names = result.stdout.strip().split('\n')

                    # Use the proper parsing logic from agent_container_recovery
                    from services.agent_container_recovery import get_agent_container_recovery
                    recovery_service = get_agent_container_recovery()

                    for container_name in container_names:
                        # Parse the container name properly
                        metadata = recovery_service.parse_container_name(container_name)

                        if metadata and metadata['project'] == project:
                            # Check if this container is for our issue
                            # The task_id or container name might contain the issue number
                            issue_str = str(issue_number)
                            if issue_str in container_name:
                                logger.info(
                                    f"Pipeline run {pipeline_run_id} has running agent container: {container_name} "
                                    f"(project={metadata['project']}, task_id={metadata.get('task_id')})"
                                )
                                return True

                # Check for repair cycle containers (format: repair-cycle-{project}-{issue}-{run_id})
                # Get all repair cycle containers and parse them properly
                result = subprocess.run(
                    ['docker', 'ps', '--filter', 'name=repair-cycle-',
                     '--format', '{{.Names}}'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )

                if result.returncode == 0 and result.stdout.strip():
                    container_names = result.stdout.strip().split('\n')

                    # Use the proper parsing logic from agent_container_recovery
                    from services.agent_container_recovery import get_agent_container_recovery
                    recovery_service = get_agent_container_recovery()

                    for container_name in container_names:
                        # Parse repair cycle container name properly
                        metadata = recovery_service.parse_repair_cycle_container_name(container_name)

                        if (metadata and
                            metadata['project'] == project and
                            int(metadata['issue_number']) == issue_number):
                            
                            # Verify run_id matches if available
                            container_run_id = metadata.get('run_id')
                            if container_run_id and not pipeline_run_id.startswith(container_run_id):
                                logger.debug(
                                    f"Skipping container {container_name} for run {pipeline_run_id}: "
                                    f"run_id mismatch ({container_run_id})"
                                )
                                continue

                            logger.info(
                                f"Pipeline run {pipeline_run_id} has running repair cycle container: {container_name} "
                                f"(project={metadata['project']}, issue={metadata['issue_number']})"
                            )
                            return True

            except Exception as e:
                logger.warning(f"Error checking Docker containers: {e}")
            
            # Check 2: Are there active agents in Redis tracking for this issue?
            redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
            agent_keys = redis_client.keys('agent:container:*')
            
            for key in agent_keys:
                try:
                    container_info = redis_client.hgetall(key)
                    if (container_info.get('project') == project and 
                        container_info.get('issue_number') == str(issue_number)):
                        logger.info(
                            f"Pipeline run {pipeline_run_id} has active agent in Redis: "
                            f"{container_info.get('container_name')}"
                        )
                        return True
                except Exception as e:
                    logger.warning(f"Error checking agent key {key}: {e}")
                    continue
            
            # Check 3: Are there queued tasks for this issue?
            # Tasks in the queue have the project and issue_number in their context
            queue_length = redis_client.llen('orchestrator:task_queue')
            if queue_length > 0:
                # Check up to 100 tasks in the queue
                tasks = redis_client.lrange('orchestrator:task_queue', 0, min(100, queue_length - 1))
                for task_json in tasks:
                    try:
                        import json
                        task = json.loads(task_json)
                        task_context = task.get('context', {})
                        if (task_context.get('project') == project and 
                            task_context.get('issue_number') == issue_number):
                            logger.info(
                                f"Pipeline run {pipeline_run_id} has queued task for issue #{issue_number}"
                            )
                            return True
                    except Exception as e:
                        continue
            
            # Check 4: Has there been recent activity? (agent events in last 10 minutes)
            if self.es:
                try:
                    ten_minutes_ago = (datetime.utcnow() - timedelta(minutes=10)).isoformat() + 'Z'
                    
                    activity_query = {
                        "query": {
                            "bool": {
                                "must": [
                                    {"term": {"project.keyword": project}},
                                    {"term": {"issue_number": issue_number}},
                                    {"range": {"timestamp": {"gte": ten_minutes_ago}}}
                                ]
                            }
                        },
                        "size": 1
                    }
                    
                    result = self.es.search(index="agent-events-*", body=activity_query)
                    
                    if result['hits']['total']['value'] > 0:
                        logger.info(
                            f"Pipeline run {pipeline_run_id} has recent activity "
                            f"(events in last 10 minutes)"
                        )
                        return True
                        
                except Exception as e:
                    logger.warning(f"Error checking recent activity for {pipeline_run_id}: {e}")
            
            # No active work detected
            logger.debug(
                f"Pipeline run {pipeline_run_id} appears stalled: "
                f"no active agents, no queued tasks, no recent activity"
            )
            return False
            
        except Exception as e:
            logger.error(f"Error verifying pipeline run {pipeline_run_id}: {e}")
            # Return True on error to be safe (don't end runs if we can't verify)
            return True
    
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
