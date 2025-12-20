"""
Base Report Manager

Abstract base class for investigation report management.
Provides common file operations with customizable subdirectory structure.
"""

import logging
import json
from abc import ABC, abstractmethod
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)


class BaseReportManager(ABC):
    """
    Abstract base class for investigation report management.

    Provides common file operations for reading/writing investigation reports.
    Subclasses customize directory structure and context file format.

    Directory structure:
        {base_dir}/{subdirectory}/{fingerprint_id}/
        ├── context.json              # Investigation input
        ├── investigation_log.txt     # Claude Code execution log
        ├── diagnosis.md              # Root cause analysis
        ├── fix_plan.md               # Proposed fix/recommendations
        ├── ignored.md                # Reason for ignoring (alternative)
        └── attachments/              # Optional additional files
    """

    def __init__(self, base_dir: str = "/medic", subdirectory: str = ""):
        """
        Initialize report manager.

        Args:
            base_dir: Base directory for all medic reports
            subdirectory: Subdirectory within base_dir (empty for Docker, "claude" for Claude)
        """
        if subdirectory:
            self.base_dir = Path(base_dir) / subdirectory
        else:
            self.base_dir = Path(base_dir)

        self.base_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"{self.__class__.__name__} initialized with base_dir: {self.base_dir}")

    @abstractmethod
    def write_context(self, fingerprint_id: str, **kwargs) -> str:
        """
        Write investigation context file.

        Args:
            fingerprint_id: Failure signature ID
            **kwargs: Context data (varies by system)

        Returns:
            Path to context file
        """
        pass

    def get_report_dir(self, fingerprint_id: str) -> Path:
        """Get the directory path for a fingerprint's reports"""
        return self.base_dir / fingerprint_id

    def ensure_report_dir(self, fingerprint_id: str) -> Path:
        """Ensure report directory exists and return path"""
        report_dir = self.get_report_dir(fingerprint_id)
        report_dir.mkdir(parents=True, exist_ok=True)
        return report_dir

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

    def list_all_investigations(self) -> List[str]:
        """
        List all fingerprint IDs that have investigation directories.

        Returns:
            List of fingerprint IDs
        """
        if not self.base_dir.exists():
            return []

        investigations = []
        for item in self.base_dir.iterdir():
            if item.is_dir() and item.name.startswith("sha256:"):
                investigations.append(item.name)

        return sorted(investigations)

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
                self.cleanup_investigation(fingerprint_id)
                deleted += 1

        if deleted > 0:
            logger.info(f"Cleaned up {deleted} investigation reports older than {days} days")

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
            if self.cleanup_investigation(fp_id):
                deleted_count += 1

        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} investigation report directories")

        return deleted_count
