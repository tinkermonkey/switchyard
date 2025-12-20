"""
Docker Report Manager

Manages investigation reports for Docker container log failures.
"""

import logging
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from services.medic.base import BaseReportManager

logger = logging.getLogger(__name__)


class DockerReportManager(BaseReportManager):
    """
    Docker-specific report manager.

    Manages investigation reports in /medic/{fingerprint_id}/ directories.
    """

    def __init__(self, base_dir: str = "/medic"):
        """
        Initialize Docker report manager.

        Args:
            base_dir: Base directory for investigation reports
        """
        # Docker reports go directly in /medic/ (no subdirectory)
        super().__init__(base_dir=base_dir, subdirectory="")
        logger.info("DockerReportManager initialized")

    def write_context(
        self,
        fingerprint_id: str,
        signature_data: dict,
        sample_logs: List[dict],
        **kwargs
    ) -> str:
        """
        Write investigation context file for Docker failures.

        Args:
            fingerprint_id: Failure signature ID
            signature_data: Signature metadata from Elasticsearch
            sample_logs: Sample log entries
            **kwargs: Additional context data

        Returns:
            Path to context file
        """
        report_dir = self.ensure_report_dir(fingerprint_id)
        context_file = report_dir / "context.json"

        context = {
            "fingerprint_id": fingerprint_id,
            "signature": signature_data,
            "sample_logs": sample_logs,
            "investigation_type": "docker_container_logs",
            "created_at": datetime.now(timezone.utc).isoformat(),
            **kwargs
        }

        with open(context_file, "w") as f:
            json.dump(context, f, indent=2, default=str)

        logger.info(f"Wrote Docker investigation context file: {context_file}")
        return str(context_file)
