"""
Claude Report Manager for Claude Medic Investigation Reports

Manages markdown report files in /medic/claude/{fingerprint_id}/ directories.
"""

import os
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List
import json

logger = logging.getLogger(__name__)


class ClaudeReportManager:
    """
    Manages Claude investigation report files on the file system.

    Directory structure:
        /medic/claude/{fingerprint_id}/
        ├── context.json              # Investigation input (signature + cluster data)
        ├── investigation_log.txt     # Claude Code execution log
        ├── diagnosis.md              # Root cause analysis
        ├── fix_plan.md               # Recommendations (CLAUDE.md, sub-agents, skills)
        ├── ignored.md                # Reason for ignoring (alternative to diagnosis+fix)
        └── attachments/              # Optional additional files
    """

    def __init__(self, base_dir: str = "/medic"):
        """
        Initialize Claude report manager.

        Args:
            base_dir: Base directory for all medic reports
        """
        # Claude Medic reports go in /medic/claude/ subdirectory
        self.base_dir = Path(base_dir) / "claude"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"ClaudeReportManager initialized with base_dir: {self.base_dir}")

    def get_report_dir(self, fingerprint_id: str) -> Path:
        """Get the directory path for a fingerprint's reports"""
        return self.base_dir / fingerprint_id

    def ensure_report_dir(self, fingerprint_id: str) -> Path:
        """Ensure report directory exists and return path"""
        report_dir = self.get_report_dir(fingerprint_id)
        report_dir.mkdir(parents=True, exist_ok=True)
        return report_dir

    def write_context(
        self,
        fingerprint_id: str,
        signature_data: dict,
        sample_clusters: List[dict],
        project: str
    ) -> str:
        """
        Write investigation context file for Claude failures.

        Args:
            fingerprint_id: Failure signature ID
            signature_data: Signature metadata from Elasticsearch
            sample_clusters: Sample failure clusters (not individual log lines)
            project: Project name

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
        }

        with open(context_file, "w") as f:
            json.dump(context, f, indent=2, default=str)

        logger.info(f"Wrote Claude investigation context file: {context_file}")
        return str(context_file)

    def read_context(self, fingerprint_id: str) -> Optional[dict]:
        """Read investigation context file"""
        context_file = self.get_report_dir(fingerprint_id) / "context.json"

        if not context_file.exists():
            return None

        with open(context_file, "r") as f:
            return json.load(f)

    def get_investigation_log_path(self, fingerprint_id: str) -> str:
        """Get path to investigation log file"""
        report_dir = self.ensure_report_dir(fingerprint_id)
        return str(report_dir / "investigation_log.txt")

    def read_diagnosis(self, fingerprint_id: str) -> Optional[str]:
        """Read diagnosis.md if it exists"""
        diagnosis_file = self.get_report_dir(fingerprint_id) / "diagnosis.md"

        if not diagnosis_file.exists():
            return None

        with open(diagnosis_file, "r") as f:
            return f.read()

    def read_fix_plan(self, fingerprint_id: str) -> Optional[str]:
        """Read fix_plan.md if it exists"""
        fix_plan_file = self.get_report_dir(fingerprint_id) / "fix_plan.md"

        if not fix_plan_file.exists():
            return None

        with open(fix_plan_file, "r") as f:
            return f.read()

    def read_ignored(self, fingerprint_id: str) -> Optional[str]:
        """Read ignored.md if it exists"""
        ignored_file = self.get_report_dir(fingerprint_id) / "ignored.md"

        if not ignored_file.exists():
            return None

        with open(ignored_file, "r") as f:
            return f.read()

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

    def count_log_lines(self, fingerprint_id: str) -> int:
        """Count lines in investigation log (progress indicator)"""
        log_file = self.get_report_dir(fingerprint_id) / "investigation_log.txt"

        if not log_file.exists():
            return 0

        try:
            with open(log_file, "r") as f:
                return sum(1 for _ in f)
        except Exception as e:
            logger.error(f"Failed to count log lines for {fingerprint_id}: {e}")
            return 0

    def list_all_investigations(self) -> List[str]:
        """
        List all fingerprint IDs with investigation reports.

        Returns:
            List of fingerprint IDs (directory names)
        """
        if not self.base_dir.exists():
            return []

        investigations = []
        for item in self.base_dir.iterdir():
            if item.is_dir() and item.name.startswith("sha256:"):
                investigations.append(item.name)

        return sorted(investigations)

    def get_investigation_summary(self, fingerprint_id: str) -> Dict:
        """
        Get summary of investigation for API responses.

        Returns:
            Dictionary with investigation metadata
        """
        report_dir = self.get_report_dir(fingerprint_id)

        if not report_dir.exists():
            return {
                "fingerprint_id": fingerprint_id,
                "status": "not_started",
                "has_diagnosis": False,
                "has_fix_plan": False,
                "has_ignored": False,
                "log_lines": 0,
            }

        diagnosis_file = report_dir / "diagnosis.md"
        fix_plan_file = report_dir / "fix_plan.md"
        ignored_file = report_dir / "ignored.md"
        log_file = report_dir / "investigation_log.txt"
        context_file = report_dir / "context.json"

        summary = {
            "fingerprint_id": fingerprint_id,
            "status": self.get_status(fingerprint_id),
            "has_diagnosis": diagnosis_file.exists(),
            "has_fix_plan": fix_plan_file.exists(),
            "has_ignored": ignored_file.exists(),
            "log_lines": self.count_log_lines(fingerprint_id),
        }

        # Add file modification timestamps
        if diagnosis_file.exists():
            summary["diagnosis_updated_at"] = datetime.fromtimestamp(
                diagnosis_file.stat().st_mtime, tz=timezone.utc
            ).isoformat()

        if fix_plan_file.exists():
            summary["fix_plan_updated_at"] = datetime.fromtimestamp(
                fix_plan_file.stat().st_mtime, tz=timezone.utc
            ).isoformat()

        if ignored_file.exists():
            summary["ignored_updated_at"] = datetime.fromtimestamp(
                ignored_file.stat().st_mtime, tz=timezone.utc
            ).isoformat()

        if log_file.exists():
            summary["log_updated_at"] = datetime.fromtimestamp(
                log_file.stat().st_mtime, tz=timezone.utc
            ).isoformat()

        # Add project from context
        context = self.read_context(fingerprint_id)
        if context:
            summary["project"] = context.get("project")
            summary["created_at"] = context.get("created_at")

        return summary

    def delete_investigation(self, fingerprint_id: str) -> bool:
        """
        Delete all investigation files for a fingerprint.

        Args:
            fingerprint_id: Fingerprint ID

        Returns:
            True if deleted, False if didn't exist
        """
        report_dir = self.get_report_dir(fingerprint_id)

        if not report_dir.exists():
            return False

        import shutil
        shutil.rmtree(report_dir)
        logger.info(f"Deleted Claude investigation reports for {fingerprint_id}")
        return True

    def cleanup_old_investigations(self, days: int = 30) -> int:
        """
        Delete investigation directories older than specified days.

        Args:
            days: Delete investigations older than this many days

        Returns:
            Number of investigations deleted
        """
        cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
        deleted = 0

        for fingerprint_id in self.list_all_investigations():
            report_dir = self.get_report_dir(fingerprint_id)

            # Check directory modification time
            if report_dir.stat().st_mtime < cutoff:
                self.delete_investigation(fingerprint_id)
                deleted += 1

        if deleted > 0:
            logger.info(f"Cleaned up {deleted} Claude investigation reports older than {days} days")

        return deleted

    def cleanup_investigation_reports(self, fingerprint_ids: List[str]) -> int:
        """
        Delete investigation report directories for specific fingerprint IDs.

        This is used to clean up reports for signatures that have been deleted
        from Elasticsearch due to inactivity.

        Args:
            fingerprint_ids: List of fingerprint IDs to delete

        Returns:
            Number of directories deleted
        """
        if not fingerprint_ids:
            return 0

        deleted_count = 0

        for fp_id in fingerprint_ids:
            if self.delete_investigation(fp_id):
                deleted_count += 1

        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} investigation report directories")

        return deleted_count


# Export
__all__ = ['ClaudeReportManager']
