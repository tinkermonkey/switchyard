"""
Report Manager for Medic Investigation Reports

Manages markdown report files in /medic/{fingerprint_id}/ directories.
"""

import os
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List
import json

logger = logging.getLogger(__name__)


class ReportManager:
    """
    Manages investigation report files on the file system.

    Directory structure:
        /medic/{fingerprint_id}/
        ├── context.json              # Investigation input
        ├── investigation_log.txt     # Claude Code execution log
        ├── diagnosis.md              # Root cause analysis
        ├── fix_plan.md               # Proposed fix
        ├── ignored.md                # Reason for ignoring (alternative to diagnosis+fix)
        └── attachments/              # Optional additional files
    """

    def __init__(self, base_dir: str = "/medic"):
        """
        Initialize report manager.

        Args:
            base_dir: Base directory for all investigation reports
        """
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"ReportManager initialized with base_dir: {self.base_dir}")

    def get_report_dir(self, fingerprint_id: str) -> Path:
        """Get the directory path for a fingerprint's reports"""
        return self.base_dir / fingerprint_id

    def ensure_report_dir(self, fingerprint_id: str) -> Path:
        """Ensure report directory exists and return path"""
        report_dir = self.get_report_dir(fingerprint_id)
        report_dir.mkdir(parents=True, exist_ok=True)
        return report_dir

    def write_context(
        self, fingerprint_id: str, signature_data: dict, sample_logs: List[dict]
    ) -> str:
        """
        Write investigation context file.

        Args:
            fingerprint_id: Failure signature ID
            signature_data: Signature metadata from Elasticsearch
            sample_logs: Sample log entries

        Returns:
            Path to context file
        """
        report_dir = self.ensure_report_dir(fingerprint_id)
        context_file = report_dir / "context.json"

        context = {
            "fingerprint_id": fingerprint_id,
            "signature": signature_data,
            "sample_logs": sample_logs,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        with open(context_file, "w") as f:
            json.dump(context, f, indent=2, default=str)

        logger.info(f"Wrote context file: {context_file}")
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

    def get_report_status(self, fingerprint_id: str) -> Dict[str, any]:
        """
        Get status of all reports for a fingerprint.

        Returns:
            Dict with has_* flags and file metadata
        """
        report_dir = self.get_report_dir(fingerprint_id)

        if not report_dir.exists():
            return {
                "has_context": False,
                "has_diagnosis": False,
                "has_fix_plan": False,
                "has_ignored": False,
                "has_investigation_log": False,
            }

        status = {}

        # Check each file
        for filename, key in [
            ("context.json", "has_context"),
            ("diagnosis.md", "has_diagnosis"),
            ("fix_plan.md", "has_fix_plan"),
            ("ignored.md", "has_ignored"),
            ("investigation_log.txt", "has_investigation_log"),
        ]:
            file_path = report_dir / filename
            status[key] = file_path.exists()

            if file_path.exists():
                stat = file_path.stat()
                status[f"{key}_size"] = stat.st_size
                status[f"{key}_modified"] = datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat()

        return status

    def list_all_investigations(self) -> List[str]:
        """
        List all fingerprint IDs that have investigation directories.

        Returns:
            List of fingerprint IDs
        """
        if not self.base_dir.exists():
            return []

        return [
            d.name
            for d in self.base_dir.iterdir()
            if d.is_dir() and d.name.startswith("sha256:")
        ]

    def get_all_investigations_summary(self) -> List[Dict]:
        """
        Get summary of all investigations.

        Returns:
            List of dicts with fingerprint_id and report status
        """
        investigations = []

        for fingerprint_id in self.list_all_investigations():
            status = self.get_report_status(fingerprint_id)
            status["fingerprint_id"] = fingerprint_id
            investigations.append(status)

        return investigations

    def count_log_lines(self, fingerprint_id: str) -> int:
        """
        Count lines in investigation log (for progress tracking).

        Returns:
            Number of lines, or 0 if file doesn't exist
        """
        log_file = self.get_report_dir(fingerprint_id) / "investigation_log.txt"

        if not log_file.exists():
            return 0

        try:
            with open(log_file, "r") as f:
                return sum(1 for _ in f)
        except Exception as e:
            logger.error(f"Failed to count log lines for {fingerprint_id}: {e}")
            return 0

    def get_attachments_dir(self, fingerprint_id: str) -> Path:
        """Get/create attachments directory"""
        attachments_dir = self.get_report_dir(fingerprint_id) / "attachments"
        attachments_dir.mkdir(parents=True, exist_ok=True)
        return attachments_dir

    def cleanup_investigation(self, fingerprint_id: str) -> bool:
        """
        Delete all files for an investigation.

        Returns:
            True if deleted, False if didn't exist
        """
        report_dir = self.get_report_dir(fingerprint_id)

        if not report_dir.exists():
            return False

        import shutil
        shutil.rmtree(report_dir)
        logger.info(f"Deleted investigation directory: {report_dir}")
        return True
