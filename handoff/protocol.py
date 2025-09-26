# handoff/protocol.py
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional
from datetime import datetime
import json
from state_management.manager import StateManager

@dataclass
class HandoffPackage:
    """Standardized package for agent handoffs"""
    
    # Metadata
    handoff_id: str
    timestamp: str
    source_agent: str
    target_agent: str
    pipeline_id: str
    
    # Context
    task_context: Dict[str, Any]
    completed_work: List[str]
    decisions_made: List[Dict[str, str]]
    
    # Deliverables
    artifacts: Dict[str, Any]
    quality_metrics: Dict[str, float]
    validation_results: Dict[str, bool]
    
    # Next steps
    required_actions: List[str]
    constraints: List[str]
    success_criteria: List[str]
    
    # Optional fields
    notes: Optional[str] = None
    warnings: Optional[List[str]] = None
    dependencies: Optional[List[str]] = None
    
    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)
    
    @classmethod
    def from_json(cls, json_str: str) -> 'HandoffPackage':
        return cls(**json.loads(json_str))
    
    def validate(self) -> List[str]:
        """Validate the handoff package completeness"""
        issues = []
        
        if not self.artifacts:
            issues.append("No artifacts provided")
        
        if not self.quality_metrics:
            issues.append("No quality metrics provided")
        
        if not self.required_actions:
            issues.append("No required actions specified")
        
        return issues

class HandoffManager:
    """Manages handoff between agents"""
    
    def __init__(self, state_manager: StateManager):
        self.state_manager = state_manager
        from pathlib import Path
        self.handoff_dir = Path("orchestrator_data/handoffs")
        self.handoff_dir.mkdir(parents=True, exist_ok=True)
    
    async def create_handoff(
        self,
        source_agent: str,
        target_agent: str,
        context: Dict[str, Any],
        artifacts: Dict[str, Any]
    ) -> HandoffPackage:
        """Create a handoff package"""
        
        handoff = HandoffPackage(
            handoff_id=f"handoff_{datetime.now().timestamp()}",
            timestamp=datetime.now().isoformat(),
            source_agent=source_agent,
            target_agent=target_agent,
            pipeline_id=context.get('pipeline_id', 'unknown'),
            task_context=context.get('task', {}),
            completed_work=context.get('completed_work', []),
            decisions_made=context.get('decisions', []),
            artifacts=artifacts,
            quality_metrics=context.get('metrics', {}),
            validation_results=context.get('validation', {}),
            required_actions=context.get('next_steps', []),
            constraints=context.get('constraints', []),
            success_criteria=context.get('success_criteria', [])
        )
        
        # Save handoff package
        handoff_file = self.handoff_dir / f"{handoff.handoff_id}.json"
        with open(handoff_file, 'w') as f:
            f.write(handoff.to_json())
        
        # Update state
        await self.state_manager.save_agent_state(
            target_agent,
            {"pending_handoff": handoff.handoff_id}
        )
        
        return handoff
    
    async def receive_handoff(self, agent_id: str) -> Optional[HandoffPackage]:
        """Receive pending handoff for an agent"""
        
        state = await self.state_manager.load_agent_state(agent_id)
        if not state or 'pending_handoff' not in state:
            return None
        
        handoff_id = state['pending_handoff']
        handoff_file = self.handoff_dir / f"{handoff_id}.json"
        
        if not handoff_file.exists():
            return None
        
        with open(handoff_file, 'r') as f:
            handoff = HandoffPackage.from_json(f.read())
        
        # Validate handoff
        issues = handoff.validate()
        if issues:
            print(f"Warning: Handoff validation issues: {issues}")
        
        # Clear pending handoff
        state.pop('pending_handoff')
        await self.state_manager.save_agent_state(agent_id, state)
        
        return handoff