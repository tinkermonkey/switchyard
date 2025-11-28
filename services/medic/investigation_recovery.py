"""
Investigation Recovery Logic

Handles startup recovery for in-progress investigations.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict
from .investigation_queue import InvestigationQueue
from .investigation_agent_runner import InvestigationAgentRunner
from .report_manager import ReportManager

logger = logging.getLogger(__name__)


class InvestigationRecovery:
    """
    Recovers stalled/incomplete investigations on startup.

    Recovery Logic:
    1. Find all investigations with status in_progress, starting, or stalled
    2. Check if process still exists (via PID)
    3. If process exists → recover and continue monitoring
    4. If process missing but reports exist → mark as completed
    5. If process missing, no reports, <30min elapsed → wait
    6. If process missing, no reports, >4hr elapsed → mark as failed/timeout
    7. If process missing, no reports, 30min-4hr → re-launch investigation
    """

    # Thresholds
    WAIT_THRESHOLD = 30 * 60  # 30 minutes - wait before re-launch
    TIMEOUT_THRESHOLD = 4 * 3600  # 4 hours - mark as timeout
    RELAUNCH_GRACE_PERIOD = 5 * 60  # 5 minutes - grace period after restart

    def __init__(
        self,
        queue: InvestigationQueue,
        agent_runner: InvestigationAgentRunner,
        report_manager: ReportManager,
    ):
        """
        Initialize recovery manager.

        Args:
            queue: Investigation queue manager
            agent_runner: Agent process runner
            report_manager: Report file manager
        """
        self.queue = queue
        self.agent_runner = agent_runner
        self.report_manager = report_manager
        logger.info("InvestigationRecovery initialized")

    def recover_all(self) -> Dict[str, int]:
        """
        Recover all incomplete investigations on startup.

        Returns:
            Dict with counts: recovered, completed, failed, relaunched, waiting
        """
        logger.info("Starting investigation recovery...")

        stats = {
            "recovered": 0,
            "completed": 0,
            "failed": 0,
            "relaunched": 0,
            "waiting": 0,
            "timeout": 0,
        }

        # Get all active investigations
        active_fps = self.queue.get_all_active()
        logger.info(f"Found {len(active_fps)} active investigations")

        for fingerprint_id in active_fps:
            result = self.recover_investigation(fingerprint_id)
            stats[result] += 1

        logger.info(f"Recovery complete: {stats}")
        return stats

    def recover_investigation(self, fingerprint_id: str) -> str:
        """
        Recover a single investigation.

        Returns:
            One of: "recovered", "completed", "failed", "relaunched", "waiting", "timeout"
        """
        info = self.queue.get_investigation_info(fingerprint_id)
        status = info["status"]
        pid = info["pid"]
        started_at = info["started_at"]

        logger.info(
            f"Recovering {fingerprint_id}: status={status}, pid={pid}, started={started_at}"
        )

        # Check if process still exists
        # Note: After refactor to run_claude_code(), pid will be 0 (placeholder)
        # Recovery now relies primarily on checking if reports exist
        process_exists = False
        if pid and pid != 0:
            process_exists = self.agent_runner.check_process(pid)

        # Calculate elapsed time
        elapsed_seconds = None
        if started_at:
            started_time = datetime.fromisoformat(started_at)
            elapsed = datetime.now(timezone.utc) - started_time
            elapsed_seconds = elapsed.total_seconds()

        # Decision logic
        if process_exists:
            # Process still running - keep monitoring
            logger.info(f"{fingerprint_id}: Process {pid} still running, continuing")
            return "recovered"

        # Process not running - check reports
        report_status = self.report_manager.get_report_status(fingerprint_id)
        has_reports = report_status["has_diagnosis"] or report_status["has_ignored"]

        if has_reports:
            # Reports exist - mark as completed
            logger.info(f"{fingerprint_id}: Reports exist, marking as completed")
            result = (
                InvestigationQueue.RESULT_IGNORED
                if report_status["has_ignored"]
                else InvestigationQueue.RESULT_SUCCESS
            )
            self.queue.mark_completed(fingerprint_id, result)
            return "completed"

        # No process, no reports - decide based on elapsed time
        if not elapsed_seconds:
            # No start time - shouldn't happen, but mark as failed
            logger.warning(f"{fingerprint_id}: No start time, marking as failed")
            self.queue.mark_completed(
                fingerprint_id,
                InvestigationQueue.RESULT_FAILED,
                "No start time recorded",
            )
            return "failed"

        if elapsed_seconds < self.WAIT_THRESHOLD:
            # Recently started - wait a bit more
            logger.info(
                f"{fingerprint_id}: Started {int(elapsed_seconds/60)}m ago, waiting"
            )
            return "waiting"

        if elapsed_seconds > self.TIMEOUT_THRESHOLD:
            # Exceeded timeout - mark as timeout
            logger.warning(
                f"{fingerprint_id}: Exceeded timeout ({int(elapsed_seconds/3600)}h), marking as timeout"
            )
            self.queue.mark_completed(
                fingerprint_id,
                InvestigationQueue.RESULT_TIMEOUT,
                f"Exceeded {self.TIMEOUT_THRESHOLD/3600}h timeout",
            )
            return "timeout"

        # Between 30min and 4hr - try to re-launch
        logger.info(
            f"{fingerprint_id}: Stalled at {int(elapsed_seconds/60)}m, attempting re-launch"
        )

        if self._relaunch_investigation(fingerprint_id):
            return "relaunched"
        else:
            logger.error(f"{fingerprint_id}: Re-launch failed, marking as failed")
            self.queue.mark_completed(
                fingerprint_id,
                InvestigationQueue.RESULT_FAILED,
                "Re-launch failed during recovery",
            )
            return "failed"

    def _relaunch_investigation(self, fingerprint_id: str) -> bool:
        """
        Attempt to re-launch a stalled investigation.

        Returns:
            True if successfully re-launched
        """
        try:
            # Try to acquire lock
            if not self.queue.acquire_lock(fingerprint_id):
                logger.warning(f"{fingerprint_id}: Failed to acquire lock for re-launch")
                return False

            # Get context file
            context_file = self.report_manager.get_report_dir(fingerprint_id) / "context.json"
            if not context_file.exists():
                logger.error(f"{fingerprint_id}: Context file not found")
                self.queue.release_lock(fingerprint_id)
                return False

            # Get output log path
            output_log = self.report_manager.get_investigation_log_path(fingerprint_id)

            # Launch process
            process = self.agent_runner.launch_investigation(
                fingerprint_id, str(context_file), output_log
            )

            if not process:
                logger.error(f"{fingerprint_id}: Failed to launch process")
                self.queue.release_lock(fingerprint_id)
                return False

            # Update queue state
            self.queue.set_pid(fingerprint_id, process.pid)
            self.queue.update_status(fingerprint_id, InvestigationQueue.STATUS_IN_PROGRESS)
            self.queue.update_heartbeat(fingerprint_id)

            logger.info(f"{fingerprint_id}: Successfully re-launched with PID={process.pid}")
            return True

        except Exception as e:
            logger.error(f"{fingerprint_id}: Error during re-launch: {e}", exc_info=True)
            return False

    def check_stalled_investigations(self) -> List[str]:
        """
        Check all active investigations for stalls.

        Returns:
            List of fingerprint IDs that are stalled
        """
        stalled = []
        active_fps = self.queue.get_all_active()

        for fingerprint_id in active_fps:
            if self.queue.check_stalled(fingerprint_id):
                logger.warning(f"{fingerprint_id}: Investigation stalled")
                stalled.append(fingerprint_id)
                self.queue.update_status(fingerprint_id, InvestigationQueue.STATUS_STALLED)

        return stalled

    def check_timeouts(self) -> List[str]:
        """
        Check all active investigations for timeouts.

        Returns:
            List of fingerprint IDs that timed out
        """
        timed_out = []
        active_fps = self.queue.get_all_active()

        for fingerprint_id in active_fps:
            if self.queue.check_timeout(fingerprint_id):
                logger.warning(f"{fingerprint_id}: Investigation timed out")

                # Kill process if still running
                pid = self.queue.get_pid(fingerprint_id)
                if pid and self.agent_runner.check_process(pid):
                    logger.info(f"{fingerprint_id}: Killing timed-out process {pid}")
                    self.agent_runner.terminate_process(pid)

                # Mark as timeout
                self.queue.mark_completed(
                    fingerprint_id,
                    InvestigationQueue.RESULT_TIMEOUT,
                    "Exceeded 4 hour timeout",
                )
                timed_out.append(fingerprint_id)

        return timed_out

    def cleanup_completed_investigations(self, retention_days: int = 30):
        """
        Clean up completed investigations older than retention period.

        Args:
            retention_days: Days to keep completed investigations
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        logger.info(f"Cleaning up investigations completed before {cutoff.isoformat()}")

        cleaned = 0
        for fingerprint_id in self.report_manager.list_all_investigations():
            info = self.queue.get_investigation_info(fingerprint_id)

            # Skip if not completed
            if info["status"] not in [
                InvestigationQueue.STATUS_COMPLETED,
                InvestigationQueue.STATUS_FAILED,
                InvestigationQueue.STATUS_IGNORED,
                InvestigationQueue.STATUS_TIMEOUT,
            ]:
                continue

            # Check completion time
            completed_at = info.get("completed_at")
            if not completed_at:
                continue

            completed_time = datetime.fromisoformat(completed_at)
            if completed_time < cutoff:
                logger.info(f"Cleaning up old investigation: {fingerprint_id}")
                self.queue.cleanup_investigation(fingerprint_id)
                self.report_manager.cleanup_investigation(fingerprint_id)
                cleaned += 1

        logger.info(f"Cleaned up {cleaned} old investigations")
        return cleaned
