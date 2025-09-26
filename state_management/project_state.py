import os
from pathlib import Path
from .manager import StateManager


class ProjectStateManager(StateManager):
    def __init__(self, orchestrator_dir: Path, project_name: str):
        # State lives in orchestrator but is project-scoped
        state_dir = orchestrator_dir / "state" / "projects" / project_name
        super().__init__(state_dir)
        
        # Also maintain a symlink in the project for debugging
        project_state_link = Path(f"~/development/{project_name}/.claude/state")
        if not project_state_link.exists():
            project_state_link.symlink_to(state_dir)