"""
Feedback Manager for handling human-in-the-loop interactions via GitHub comments
"""

import redis
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class FeedbackManager:
    """Manages feedback loop state using Redis for persistence"""

    def __init__(self, redis_client: redis.Redis = None):
        """Initialize feedback manager with Redis client"""
        if redis_client:
            self.redis = redis_client
        else:
            # Create default Redis connection
            self.redis = redis.Redis(
                host='redis',
                port=6379,
                decode_responses=True,
                socket_connect_timeout=5
            )

        # Default TTL for feedback tracking (90 days)
        self.default_ttl = 90 * 24 * 60 * 60

    def mark_comment_processed(self, issue_number: int, comment_id: str, project: str) -> bool:
        """Mark a comment as processed to prevent re-processing"""
        try:
            key = f"feedback:{project}:issue:{issue_number}:processed_comments"
            self.redis.sadd(key, comment_id)
            self.redis.expire(key, self.default_ttl)

            logger.debug(f"Marked comment {comment_id} as processed for {project}#{issue_number}")
            return True
        except Exception as e:
            logger.error(f"Failed to mark comment as processed: {e}")
            return False

    def is_comment_processed(self, issue_number: int, comment_id: str, project: str) -> bool:
        """Check if a comment has already been processed"""
        try:
            key = f"feedback:{project}:issue:{issue_number}:processed_comments"
            return self.redis.sismember(key, comment_id)
        except Exception as e:
            logger.error(f"Failed to check if comment was processed: {e}")
            return False

    def set_last_agent_comment_time(self, issue_number: int, agent: str, timestamp: str, project: str):
        """Record when an agent last commented on an issue"""
        try:
            key = f"feedback:{project}:issue:{issue_number}:last_agent_comment:{agent}"
            self.redis.set(key, timestamp)
            self.redis.expire(key, self.default_ttl)

            logger.debug(f"Recorded last comment time for {agent} on {project}#{issue_number}: {timestamp}")
        except Exception as e:
            logger.error(f"Failed to set last agent comment time: {e}")

    def get_last_agent_comment_time(self, issue_number: int, agent: str, project: str) -> Optional[str]:
        """Get the timestamp of the last agent comment"""
        try:
            key = f"feedback:{project}:issue:{issue_number}:last_agent_comment:{agent}"
            return self.redis.get(key)
        except Exception as e:
            logger.error(f"Failed to get last agent comment time: {e}")
            return None

    def store_feedback_context(self, issue_number: int, agent: str, context: Dict[str, Any], project: str):
        """Store feedback context for an issue"""
        try:
            import json
            key = f"feedback:{project}:issue:{issue_number}:context:{agent}"
            self.redis.set(key, json.dumps(context))
            self.redis.expire(key, self.default_ttl)

            logger.debug(f"Stored feedback context for {agent} on {project}#{issue_number}")
        except Exception as e:
            logger.error(f"Failed to store feedback context: {e}")

    def get_feedback_context(self, issue_number: int, agent: str, project: str) -> Optional[Dict[str, Any]]:
        """Retrieve feedback context for an issue"""
        try:
            import json
            key = f"feedback:{project}:issue:{issue_number}:context:{agent}"
            data = self.redis.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            logger.error(f"Failed to get feedback context: {e}")
            return None

    def clear_issue_feedback(self, issue_number: int, project: str):
        """Clear all feedback tracking for an issue (useful when issue is closed)"""
        try:
            pattern = f"feedback:{project}:issue:{issue_number}:*"
            keys = self.redis.keys(pattern)
            if keys:
                self.redis.delete(*keys)
                logger.info(f"Cleared feedback tracking for {project}#{issue_number}")
        except Exception as e:
            logger.error(f"Failed to clear issue feedback: {e}")

    def get_processed_comment_count(self, issue_number: int, project: str) -> int:
        """Get count of processed comments for an issue"""
        try:
            key = f"feedback:{project}:issue:{issue_number}:processed_comments"
            return self.redis.scard(key)
        except Exception as e:
            logger.error(f"Failed to get processed comment count: {e}")
            return 0