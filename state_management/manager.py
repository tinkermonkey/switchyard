# state/manager.py
import json
import os
from datetime import datetime
from typing import Dict, Any, Optional
import aiofiles
from pathlib import Path

class StateManager:
    def __init__(self, state_dir: str = "orchestrator_data/state"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoints_dir = self.state_dir / "checkpoints"
        self.checkpoints_dir.mkdir(exist_ok=True)
        
    async def checkpoint(self, pipeline_id: str, stage_index: int, context: Dict[str, Any]):
        """Create a checkpoint for pipeline recovery"""
        # Filter out non-serializable objects from context
        serializable_context = {}
        for key, value in context.items():
            if key in ['state_manager', 'logger', 'mcp_integration']:
                # Skip non-serializable objects
                continue
            try:
                json.dumps(value)  # Test if serializable
                serializable_context[key] = value
            except (TypeError, ValueError):
                # Skip non-serializable values
                pass

        checkpoint_data = {
            "pipeline_id": pipeline_id,
            "stage_index": stage_index,
            "timestamp": datetime.now().isoformat(),
            "context": serializable_context
        }

        checkpoint_file = self.checkpoints_dir / f"{pipeline_id}_stage_{stage_index}.json"

        async with aiofiles.open(checkpoint_file, 'w') as f:
            await f.write(json.dumps(checkpoint_data, indent=2))
    
    async def get_latest_checkpoint(self, pipeline_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve the most recent checkpoint for a pipeline"""
        checkpoints = list(self.checkpoints_dir.glob(f"{pipeline_id}_stage_*.json"))
        
        if not checkpoints:
            return None
            
        # Get the checkpoint with highest stage number
        latest = max(checkpoints, key=lambda x: int(x.stem.split('_stage_')[1]))
        
        async with aiofiles.open(latest, 'r') as f:
            data = await f.read()
            return json.loads(data)
    
    async def save_agent_state(self, agent_id: str, state: Dict[str, Any]):
        """Save agent-specific state"""
        state_file = self.state_dir / f"agent_{agent_id}.json"
        
        state_data = {
            "agent_id": agent_id,
            "timestamp": datetime.now().isoformat(),
            "state": state
        }
        
        async with aiofiles.open(state_file, 'w') as f:
            await f.write(json.dumps(state_data, indent=2))
    
    async def load_agent_state(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Load agent-specific state"""
        state_file = self.state_dir / f"agent_{agent_id}.json"
        
        if not state_file.exists():
            return None
            
        async with aiofiles.open(state_file, 'r') as f:
            data = await f.read()
            return json.loads(data)['state']
    
    async def log_stage_completion(self, pipeline_id: str, stage_name: str, context: Dict[str, Any]):
        """Log successful stage completion"""
        log_entry = {
            "pipeline_id": pipeline_id,
            "stage_name": stage_name,
            "status": "completed",
            "timestamp": datetime.now().isoformat(),
            "metrics": context.get('metrics', {})
        }
        
        await self._append_to_log(pipeline_id, log_entry)
    
    async def log_stage_failure(self, pipeline_id: str, stage_name: str, error: str):
        """Log stage failure"""
        log_entry = {
            "pipeline_id": pipeline_id,
            "stage_name": stage_name,
            "status": "failed",
            "timestamp": datetime.now().isoformat(),
            "error": error
        }
        
        await self._append_to_log(pipeline_id, log_entry)
    
    async def _append_to_log(self, pipeline_id: str, entry: Dict[str, Any]):
        """Append entry to pipeline log"""
        log_file = self.state_dir / f"{pipeline_id}.log"
        
        async with aiofiles.open(log_file, 'a') as f:
            await f.write(json.dumps(entry) + '\n')