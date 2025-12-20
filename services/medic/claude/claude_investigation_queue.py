"""
Claude Investigation Queue

Redis-based queue for Claude Code tool execution failure investigations.
"""

import logging
import redis

from services.medic.base import BaseInvestigationQueue

logger = logging.getLogger(__name__)


class ClaudeInvestigationQueue(BaseInvestigationQueue):
    """
    Claude-specific investigation queue.

    Simple wrapper around BaseInvestigationQueue with Claude-specific key prefix.
    All logic is inherited from the base class.
    """

    def __init__(self, redis_client: redis.Redis):
        """
        Initialize Claude investigation queue.

        Args:
            redis_client: Redis client
        """
        super().__init__(redis_client, key_prefix="medic:claude_investigation")
        logger.info("ClaudeInvestigationQueue initialized")
