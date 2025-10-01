# pipeline/base.py
import json
import time
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Dict, Any
from abc import ABC, abstractmethod
from mcp import create_mcp_integration, MCPIntegration

class PipelineState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"

@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    timeout_seconds: int = 60
    half_open_requests: int = 1
    
    def __post_init__(self):
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "closed"  # closed, open, half-open

class PipelineStage(ABC):
    def __init__(self, name: str, circuit_breaker: Optional[CircuitBreaker] = None, agent_config: Optional[Dict[str, Any]] = None):
        self.name = name
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.agent_config = agent_config  # Store agent config for observability
        # MCP integration is no longer used - MCP servers are passed directly to Claude CLI
        # self.mcp_integration = create_mcp_integration(agent_config) if agent_config else None
        self.mcp_integration = None
        
    @abstractmethod
    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        pass
    
    async def run_with_circuit_breaker(self, context: Dict[str, Any]) -> Dict[str, Any]:
        if self.circuit_breaker.state == "open":
            if time.time() - self.circuit_breaker.last_failure_time > self.circuit_breaker.timeout_seconds:
                self.circuit_breaker.state = "half-open"
            else:
                raise Exception(f"Circuit breaker OPEN for {self.name}")
        
        try:
            result = await self.execute(context)
            if self.circuit_breaker.state == "half-open":
                self.circuit_breaker.state = "closed"
                self.circuit_breaker.failure_count = 0
            return result
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Stage {self.name} failed with exception: {e}", exc_info=True)

            self.circuit_breaker.failure_count += 1
            self.circuit_breaker.last_failure_time = time.time()

            if self.circuit_breaker.failure_count >= self.circuit_breaker.failure_threshold:
                self.circuit_breaker.state = "open"
                logger.error(f"Circuit breaker opened for {self.name} after {self.circuit_breaker.failure_count} failures")
            raise e