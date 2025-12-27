"""
Investigation State Machine

Enforces valid state transitions for investigation lifecycle (ES-first architecture).
"""

import logging
from typing import Optional, Dict, Set, Any
from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)


class InvestigationStateMachine:
    """
    State machine with ES-first updates and transition validation.

    Ensures investigations can only move through valid state transitions.
    """

    # Valid state transitions (from_state -> {valid_to_states})
    VALID_TRANSITIONS = {
        None: {'queued'},  # Initial state
        'not_started': {'queued'},
        'queued': {'starting', 'failed'},
        'starting': {'in_progress', 'failed'},
        'in_progress': {'stalled', 'completed', 'failed', 'timeout'},
        'stalled': {'in_progress', 'failed', 'timeout'},
        # Terminal states - no transitions allowed
        'completed': set(),
        'failed': set(),
        'timeout': set(),
        'ignored': set(),
    }

    def __init__(self, failure_store):
        """
        Initialize state machine.

        Args:
            failure_store: BaseFailureSignatureStore instance (provides ES access)
        """
        self.failure_store = failure_store
        self.es = failure_store.es
        self.index_prefix = failure_store.INDEX_PREFIX

    def get_current_status(self, fingerprint_id: str) -> Optional[str]:
        """
        Get current investigation status from ES (source of truth).

        Args:
            fingerprint_id: Fingerprint ID

        Returns:
            Current investigation_status or None if not found
        """
        signature = self.failure_store.get_signature(fingerprint_id)
        if signature:
            return signature.get('investigation_status', 'not_started')
        return None

    def is_valid_transition(self, current_status: Optional[str], target_status: str) -> bool:
        """
        Check if transition from current_status to target_status is valid.

        Args:
            current_status: Current status (None if new)
            target_status: Desired target status

        Returns:
            True if transition is valid, False otherwise
        """
        allowed = self.VALID_TRANSITIONS.get(current_status, set())
        return target_status in allowed

    def transition(
        self,
        fingerprint_id: str,
        target_status: str,
        metadata: Optional[Dict] = None,
        force: bool = False
    ) -> bool:
        """
        Execute state transition with validation (ES-first).

        Reads current state from ES, validates transition, updates ES.

        Args:
            fingerprint_id: Fingerprint ID
            target_status: Desired target status
            metadata: Optional investigation_metadata fields to update
            force: If True, skip validation (use with caution)

        Returns:
            True if transition succeeded, False if invalid or failed
        """
        # Get current status from ES (source of truth)
        current_status = self.get_current_status(fingerprint_id)

        if not force:
            # Validate transition
            if not self.is_valid_transition(current_status, target_status):
                logger.error(
                    f"Invalid transition for {fingerprint_id[:16]}...: "
                    f"{current_status} -> {target_status} (not allowed)"
                )
                return False

        # Execute ES-first update with optimistic locking
        expected_current = [current_status] if current_status else None
        success = self.failure_store.update_investigation_status_es_first(
            fingerprint_id,
            status=target_status,
            metadata=metadata,
            expected_current=expected_current
        )

        if success:
            logger.info(
                f"State transition: {fingerprint_id[:16]}... "
                f"{current_status} -> {target_status}"
            )
            return True
        else:
            logger.warning(
                f"State transition failed for {fingerprint_id[:16]}...: "
                f"{current_status} -> {target_status}"
            )
            return False

    def is_terminal(self, status: str) -> bool:
        """
        Check if status is terminal (no further transitions allowed).

        Args:
            status: Investigation status

        Returns:
            True if terminal, False otherwise
        """
        return status in {'completed', 'failed', 'timeout', 'ignored'}

    def get_valid_next_states(self, current_status: Optional[str]) -> Set[str]:
        """
        Get all valid next states from current status.

        Args:
            current_status: Current status (None if new)

        Returns:
            Set of valid next states
        """
        return self.VALID_TRANSITIONS.get(current_status, set())
