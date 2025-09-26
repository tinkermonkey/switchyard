import os
import subprocess
from pathlib import Path
from typing import Dict, Any
from pipeline.base import PipelineStage

class SoftwareEngineerAgent(PipelineStage):
    def __init__(self, project_path: Path):
        super().__init__("software_engineer")
        self.project_path = project_path
    
    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        # Change to project directory
        os.chdir(self.project_path)
        
        # Read project-specific CLAUDE.md
        backend_claude = self.project_path / "backend" / "CLAUDE.md"
        if backend_claude.exists():
            with open(backend_claude) as f:
                project_context = f.read()
        
        # Execute Claude Code in project context
        result = subprocess.run([
            "claude",
            "-p", f"{context['task_description']}\n\nProject context:\n{project_context}",
            "--continue-session", context.get('session_id'),
            "--output-format", "json"
        ], capture_output=True, cwd=self.project_path / "backend")
        
        return {**context, "code_changes": result.stdout}