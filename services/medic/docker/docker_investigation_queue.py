"""
Docker Investigation Queue

Redis-based queue for Docker container log failure investigations.
"""

import logging
import redis

from services.medic.base import BaseInvestigationQueue

logger = logging.getLogger(__name__)


class DockerInvestigationQueue(BaseInvestigationQueue):
    """
    Docker-specific investigation queue.

    Simple wrapper around BaseInvestigationQueue with Docker-specific key prefix.
    All logic is inherited from the base class.
    """

    def __init__(self, redis_client: redis.Redis):
        """
        Initialize Docker investigation queue.

        Args:
            redis_client: Redis client
        """
        super().__init__(redis_client, key_prefix="medic:docker_investigation")
        logger.info("DockerInvestigationQueue initialized")
