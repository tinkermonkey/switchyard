# state/git_state.py
import subprocess
from pathlib import Path
from manager import StateManager
from typing import Dict, Any, List, Optional
from datetime import datetime
import json
from dataclasses import dataclass

@dataclass
class GitWorkflowState:
    """Track git state for each issue"""
    
    issue_number: int
    project: str
    branch_name: Optional[str] = None
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    status: str = "pending"  # pending, in-progress, review, approved, merged
    commits: List[str] = None
    created_at: str = None
    updated_at: str = None
    
    def __post_init__(self):
        if self.commits is None:
            self.commits = []
    
    def to_json(self):
        return json.dumps(self.__dict__, indent=2)
    
    @classmethod
    def from_json(cls, json_str: str):
        return cls(**json.loads(json_str))

class GitStateManager(StateManager):
    """Extension of StateManager that commits state to git for versioning"""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir / "git_workflow"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    async def checkpoint(self, pipeline_id: str, stage_index: int, context: Dict[str, Any]):
        await super().checkpoint(pipeline_id, stage_index, context)
        
        # Commit checkpoint to git
        subprocess.run([
            "git", "add", f".claude/state/checkpoints/{pipeline_id}_stage_{stage_index}.json"
        ], cwd=self.state_dir.parent.parent)
        
        subprocess.run([
            "git", "commit", "-m", 
            f"Checkpoint: {pipeline_id} at stage {stage_index}"
        ], cwd=self.state_dir.parent.parent)

    async def create_workflow(self, issue_number: int, project: str) -> GitWorkflowState:
        """Initialize git workflow for an issue"""
        state = GitWorkflowState(
            issue_number=issue_number,
            project=project,
            created_at=datetime.now().isoformat()
        )
        
        await self.save_state(state)
        return state
    
    async def update_branch(self, issue_number: int, branch_name: str):
        """Update branch information"""
        state = await self.load_state(issue_number)
        state.branch_name = branch_name
        state.status = "in-progress"
        state.updated_at = datetime.now().isoformat()
        await self.save_state(state)
    
    async def update_pr(self, issue_number: int, pr_url: str, pr_number: int):
        """Update PR information"""
        state = await self.load_state(issue_number)
        state.pr_url = pr_url
        state.pr_number = pr_number
        state.status = "review"
        state.updated_at = datetime.now().isoformat()
        await self.save_state(state)