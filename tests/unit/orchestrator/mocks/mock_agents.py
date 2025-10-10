"""
Mock Agent Executor for testing orchestrator flows

Provides simulation of agent execution without actually running agents.
Tracks which agents were called and returns configurable results.
"""

from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from unittest.mock import AsyncMock


@dataclass
class AgentResult:
    """Result from agent execution"""
    success: bool
    output: str
    approved: Optional[bool] = None  # For reviewer agents
    feedback: Optional[str] = None    # For reviewer agents
    error: Optional[str] = None


class MockAgentExecutor:
    """Mock agent execution for testing"""
    
    def __init__(self):
        self.executions: List[Tuple[str, Dict[str, Any]]] = []
        self.results: Dict[str, AgentResult] = {}
        self._default_result = AgentResult(success=True, output="Default output")
        
    def set_result(self, agent_name: str, result: AgentResult):
        """Set the result that will be returned when this agent is executed"""
        self.results[agent_name] = result
    
    def set_default_result(self, result: AgentResult):
        """Set default result for agents without specific results"""
        self._default_result = result
    
    async def execute_agent(self, agent: str, task: Dict[str, Any]) -> AgentResult:
        """Mock agent execution"""
        # Record the execution
        self.executions.append((agent, task))
        
        # Return configured result or default
        return self.results.get(agent, self._default_result)
    
    def was_executed(self, agent: str, issue: Optional[int] = None) -> bool:
        """Check if agent was executed (optionally for specific issue)"""
        for exec_agent, exec_task in self.executions:
            if exec_agent == agent:
                if issue is None:
                    return True
                if exec_task.get('context', {}).get('issue_number') == issue:
                    return True
        return False
    
    def get_executions(self, agent: Optional[str] = None, issue: Optional[int] = None) -> List[Tuple[str, Dict]]:
        """Get execution history (optionally filtered)"""
        filtered = []
        for exec_agent, exec_task in self.executions:
            if agent and exec_agent != agent:
                continue
            if issue and exec_task.get('context', {}).get('issue_number') != issue:
                continue
            filtered.append((exec_agent, exec_task))
        return filtered
    
    def execution_count(self, agent: Optional[str] = None, issue: Optional[int] = None) -> int:
        """Count how many times an agent was executed"""
        return len(self.get_executions(agent, issue))
    
    def reset(self):
        """Reset execution history"""
        self.executions.clear()
        self.results.clear()
    
    def get_last_execution(self, agent: str) -> Optional[Dict[str, Any]]:
        """Get the most recent execution of an agent"""
        for exec_agent, exec_task in reversed(self.executions):
            if exec_agent == agent:
                return exec_task
        return None


# Helper functions for creating common results

def success_result(output: str = "Success") -> AgentResult:
    """Create a successful agent result"""
    return AgentResult(success=True, output=output)


def failure_result(error: str = "Failed") -> AgentResult:
    """Create a failed agent result"""
    return AgentResult(success=False, output="", error=error)


def approved_review(output: str = "Looks good!") -> AgentResult:
    """Create an approved review result"""
    return AgentResult(
        success=True,
        output=output,
        approved=True,
        feedback=None
    )


def rejected_review(feedback: str = "Needs changes") -> AgentResult:
    """Create a rejected review result"""
    return AgentResult(
        success=True,
        output=feedback,
        approved=False,
        feedback=feedback
    )


def maker_output(output: str) -> AgentResult:
    """Create a maker agent result"""
    return AgentResult(success=True, output=output)


class MockAgentExecutorContext:
    """Context manager for patching agent execution"""
    
    def __init__(self, mock_executor: MockAgentExecutor):
        self.mock_executor = mock_executor
        self.patches = []
    
    def __enter__(self):
        # This would be used with unittest.mock.patch
        return self.mock_executor
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        # Cleanup patches
        for patch in self.patches:
            patch.stop()
