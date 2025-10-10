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
        
        # Elasticsearch index
        self.es_index = "pipeline-runs"
        
        logger.info("PipelineRunManager initialized")
    
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
                
                result = self.es.search(index="pipeline-runs", body=query)
                
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
                        
                        self.es.index(
                            index="pipeline-runs",
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
        
        # Fall back to Elasticsearch
        if self.es:
            try:
                result = self.es.get(index=self.es_index, id=pipeline_run_id)
                if result and result.get('found'):
                    return PipelineRun.from_dict(result['_source'])
            except Exception as e:
                logger.debug(f"Pipeline run {pipeline_run_id} not found in Elasticsearch: {e}")
        
        return None
    
    def _persist_to_elasticsearch(self, pipeline_run: PipelineRun):
        """
        Persist pipeline run to Elasticsearch
        
        Args:
            pipeline_run: PipelineRun to persist
        """
        if not self.es:
            return
        
        try:
            self.es.index(
                index=self.es_index,
                id=pipeline_run.id,
                document=pipeline_run.to_dict()
            )
            logger.debug(f"Persisted pipeline run {pipeline_run.id} to Elasticsearch")
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
