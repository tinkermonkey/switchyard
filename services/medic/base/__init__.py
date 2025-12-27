"""
Abstract base classes for Medic service components.

Provides common patterns and shared logic for both Docker and Claude failure tracking systems.
"""

from .base_signature_store import BaseFailureSignatureStore
from .base_investigation_queue import BaseInvestigationQueue
from .base_investigation_orchestrator import BaseInvestigationOrchestrator
from .base_agent_runner import BaseInvestigationAgentRunner
from .base_report_manager import BaseReportManager
from .investigation_state_machine import InvestigationStateMachine

__all__ = [
    'BaseFailureSignatureStore',
    'BaseInvestigationQueue',
    'BaseInvestigationOrchestrator',
    'BaseInvestigationAgentRunner',
    'BaseReportManager',
    'InvestigationStateMachine',
]
