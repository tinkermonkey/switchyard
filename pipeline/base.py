# pipeline/base.py
import json
import logging
import time
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Dict, Any
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

class PipelineState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"

from services.circuit_breaker import CircuitBreaker

class PipelineStage(ABC):
    def __init__(
        self,
        name: str,
        circuit_breaker: Optional[CircuitBreaker] = None,
        agent_config: Optional[Dict[str, Any]] = None,
        project_name: Optional[str] = None,
    ):
        self.name = name
        self.project_name = project_name
        if circuit_breaker:
            self.circuit_breaker = circuit_breaker
        elif project_name:
            self.circuit_breaker = CircuitBreaker(name=f"{project_name}:{name}")
        else:
            # No project_name available at this call site — fall back to the
            # bare stage name. This reproduces the pre-existing cross-project
            # bleed for whichever callers can't supply project_name, but keeps
            # them working rather than breaking construction outright.
            logger.warning(
                f"CircuitBreaker for stage '{name}' constructed without project_name — "
                f"falling back to un-namespaced key. This stage's failure count may be "
                f"shared across projects."
            )
            self.circuit_breaker = CircuitBreaker(name=name)
        self.agent_config = agent_config  # Store agent config for observability
        # MCP integration is no longer used - MCP servers are passed directly to Claude CLI
        # self.mcp_integration = create_mcp_integration(agent_config) if agent_config else None
        self.mcp_integration = None
        
    @abstractmethod
    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        pass
    
    async def run_with_circuit_breaker(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the stage wrapped in a circuit breaker"""
        return await self.circuit_breaker.call(self.execute, context)