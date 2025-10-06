"""
Centralized logging configuration for orchestrator services
"""
import logging
import os


def setup_service_logging(service_name: str = None):
    """
    Setup logging with appropriate levels for orchestrator services.

    Reduces noise from verbose libraries while maintaining visibility
    into important events.
    """
    # Get log level from environment, default to INFO
    log_level = os.getenv('LOG_LEVEL', 'INFO').upper()

    # Configure root logger
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Suppress noisy third-party loggers
    logging.getLogger('elastic_transport').setLevel(logging.WARNING)
    logging.getLogger('elasticsearch').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('docker').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)

    # Allow WARNING for werkzeug (Flask)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    # Service-specific logger
    if service_name:
        logger = logging.getLogger(service_name)
        logger.setLevel(log_level)
        return logger

    return logging.getLogger(__name__)
