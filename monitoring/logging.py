import logging
import json
from pythonjsonlogger import jsonlogger
from datetime import datetime

class OrchestratorLogger:
    def __init__(self, name: str):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)

        # Ensure orchestrator logs directory exists
        from pathlib import Path
        log_dir = Path("orchestrator_data/logs")
        log_dir.mkdir(parents=True, exist_ok=True)

        # JSON formatter for structured logs
        formatter = jsonlogger.JsonFormatter(
            fmt='%(asctime)s %(name)s %(levelname)s %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)

        # File handler for audit trail - now in orchestrator_data/logs
        file_handler = logging.FileHandler(log_dir / f'{name}_orchestrator.log')
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)
    
    def log_agent_start(self, agent: str, task_id: str, context: dict):
        self.logger.info(
            "Agent started",
            extra={
                'agent': agent,
                'task_id': task_id,
                'context': context,
                'timestamp': datetime.now().isoformat()
            }
        )
    
    def log_agent_complete(self, agent: str, task_id: str, duration: float, result: dict):
        self.logger.info(
            "Agent completed",
            extra={
                'agent': agent,
                'task_id': task_id,
                'duration_seconds': duration,
                'result': result,
                'timestamp': datetime.now().isoformat()
            }
        )

    def log_info(self, message: str, extra: dict = None):
        self.logger.info(message, extra=extra)

    def log_error(self, message: str, extra: dict = None):
        self.logger.error(message, extra=extra)

    def log_warning(self, message: str, extra: dict = None):
        self.logger.warning(message, extra=extra)