"""
Docker Investigation Orchestrator

Orchestrates investigation lifecycle for Docker container log failures.
"""

import logging
from typing import Dict, Any, Tuple
import redis
from elasticsearch import Elasticsearch

from services.medic.base import BaseInvestigationOrchestrator
from services.medic.docker.docker_signature_store import DockerFailureSignatureStore
from services.medic.docker.docker_investigation_queue import DockerInvestigationQueue
from services.medic.docker.docker_agent_runner import DockerInvestigationAgentRunner
from services.medic.docker.docker_report_manager import DockerReportManager

logger = logging.getLogger(__name__)


class DockerInvestigationOrchestrator(BaseInvestigationOrchestrator):
    """
    Docker-specific investigation orchestrator.

    Manages investigation lifecycle for Docker container log failures.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        es_client: Elasticsearch,
        workspace_root: str = "/workspace/clauditoreum",
        medic_dir: str = "/medic",
    ):
        """
        Initialize Docker investigation orchestrator.

        Args:
            redis_client: Redis client for queue management
            es_client: Elasticsearch client for signature data
            workspace_root: Path to Clauditoreum codebase
            medic_dir: Base directory for investigation reports
        """
        # Initialize Docker-specific components
        queue = DockerInvestigationQueue(redis_client)
        agent_runner = DockerInvestigationAgentRunner(workspace_root)
        report_manager = DockerReportManager(medic_dir)
        failure_store = DockerFailureSignatureStore(es_client)

        # Initialize base orchestrator
        super().__init__(
            redis_client=redis_client,
            es_client=es_client,
            queue=queue,
            agent_runner=agent_runner,
            report_manager=report_manager,
            failure_store=failure_store,
            workspace_root=workspace_root,
            medic_dir=medic_dir,
        )
        logger.info("DockerInvestigationOrchestrator initialized")

    def _prepare_investigation_context(
        self, fingerprint_id: str, signature: Dict[str, Any]
    ) -> Tuple[str, str]:
        """
        Prepare investigation context file and output path for Docker failures.

        Args:
            fingerprint_id: Fingerprint ID
            signature: Signature document from Elasticsearch

        Returns:
            Tuple of (context_file_path, output_log_path)
        """
        # Get sample log entries (top 10)
        sample_logs = signature.get("sample_entries", signature.get("sample_log_entries", []))[:10]

        # Write context file
        context_file = self.report_manager.write_context(
            fingerprint_id=fingerprint_id,
            signature_data=signature,
            sample_logs=sample_logs,
        )

        # Get output log path
        output_log = self.report_manager.get_investigation_log_path(fingerprint_id)

        return context_file, output_log

    def _validate_investigation_result(self, fingerprint_id: str) -> Tuple[str, str]:
        """
        Validate investigation result based on report files for Docker failures.

        Args:
            fingerprint_id: Fingerprint ID

        Returns:
            Tuple of (result, status) where:
            - result: "success", "ignored", "failed", "timeout"
            - status: "completed", "ignored", "failed", "timeout"
        """
        report_status = self.report_manager.get_report_status(fingerprint_id)

        has_diagnosis = report_status.get("has_diagnosis", False)
        has_fix_plan = report_status.get("has_fix_plan", False)
        has_ignored = report_status.get("has_ignored", False)

        if has_diagnosis and has_fix_plan:
            return self.queue.RESULT_SUCCESS, self.queue.STATUS_COMPLETED
        elif has_ignored:
            return self.queue.RESULT_IGNORED, self.queue.STATUS_IGNORED
        else:
            return self.queue.RESULT_FAILED, self.queue.STATUS_FAILED

    def _get_observability_data(
        self, fingerprint_id: str, signature: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Get observability event data for Docker investigation.

        Args:
            fingerprint_id: Fingerprint ID
            signature: Signature document

        Returns:
            Dict with observability event data
        """
        return {
            "fingerprint_id": fingerprint_id,
            "type": "docker",
            "project": "orchestrator",
            "severity": signature.get("severity"),
            "error_type": signature.get("signature", {}).get("error_type"),
            "container_pattern": signature.get("signature", {}).get("container_pattern"),
        }
