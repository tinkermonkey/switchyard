"""
Claude Report Manager

Manages investigation reports for Claude Code tool execution failures.
"""

import logging
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from services.medic.base import BaseReportManager

logger = logging.getLogger(__name__)


class ClaudeReportManager(BaseReportManager):
    """
    Claude-specific report manager.

    Manages investigation reports in /medic/claude/{fingerprint_id}/ directories.
    """

    def __init__(self, base_dir: str = "/medic"):
        """
        Initialize Claude report manager.

        Args:
            base_dir: Base directory for investigation reports
        """
        # Claude reports go in /medic/claude/ subdirectory
        super().__init__(base_dir=base_dir, subdirectory="claude")
        logger.info("ClaudeReportManager initialized")

    def write_context(
        self,
        fingerprint_id: str,
        signature_data: dict,
        sample_clusters: List[dict],
        project: str,
        **kwargs
    ) -> str:
        """
        Write investigation context file for Claude failures.

        Args:
            fingerprint_id: Failure signature ID
            signature_data: Signature metadata from Elasticsearch
            sample_clusters: Sample failure clusters
            project: Project name
            **kwargs: Additional context data

        Returns:
            Path to context file
        """
        report_dir = self.ensure_report_dir(fingerprint_id)
        context_file = report_dir / "context.json"

        context = {
            "fingerprint_id": fingerprint_id,
            "project": project,
            "signature": signature_data,
            "sample_clusters": sample_clusters,
            "investigation_type": "claude_tool_execution",
            "created_at": datetime.now(timezone.utc).isoformat(),
            **kwargs
        }

        with open(context_file, "w") as f:
            json.dump(context, f, indent=2, default=str)

        logger.info(f"Wrote Claude investigation context file: {context_file}")
        return str(context_file)

    def get_status(self, fingerprint_id: str) -> str:
        """
        Determine investigation status based on files present.

        Returns:
            "not_started", "in_progress", "diagnosed", "ignored", "failed"
        """
        report_dir = self.get_report_dir(fingerprint_id)

        if not report_dir.exists():
            return "not_started"

        # Check for completed investigation markers
        diagnosis_exists = (report_dir / "diagnosis.md").exists()
        fix_plan_exists = (report_dir / "fix_plan.md").exists()
        ignored_exists = (report_dir / "ignored.md").exists()
        log_exists = (report_dir / "investigation_log.txt").exists()

        if ignored_exists:
            return "ignored"

        if diagnosis_exists and fix_plan_exists:
            return "diagnosed"

        if log_exists:
            return "in_progress"

        return "not_started"
