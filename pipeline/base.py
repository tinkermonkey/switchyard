# pipeline/base.py
import json
import time
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Dict, Any
from abc import ABC, abstractmethod

class PipelineState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"

from services.circuit_breaker import CircuitBreaker

class PipelineStage(ABC):
    def __init__(self, name: str, circuit_breaker: Optional[CircuitBreaker] = None, agent_config: Optional[Dict[str, Any]] = None):
        self.name = name
        self.circuit_breaker = circuit_breaker or CircuitBreaker(name=name)
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