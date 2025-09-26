import subprocess
import json
from pathlib import Path
from typing import Dict, Any
from datetime import datetime

class ClaudeSessionManager:
    """Manages long-running Claude Code sessions"""
    
    def __init__(self, sessions_dir: Path = Path(".claude/sessions")):
        self.sessions_dir = sessions_dir
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.active_sessions = {}
    
    def start_session(self, agent: str, project: str) -> str:
        """Start a new Claude Code session"""
        session_id = f"{agent}_{project}_{datetime.now().timestamp()}"
        
        session_info = {
            'id': session_id,
            'agent': agent,
            'project': project,
            'started_at': datetime.now().isoformat(),
            'pid': None,
            'status': 'active'
        }
        
        # Save session info
        session_file = self.sessions_dir / f"{session_id}.json"
        with open(session_file, 'w') as f:
            json.dump(session_info, f)
        
        self.active_sessions[session_id] = session_info
        return session_id
    
    def execute_in_session(
        self, 
        session_id: str, 
        prompt: str,
        working_dir: Path
    ) -> Dict[str, Any]:
        """Execute command in existing session"""
        
        cmd = [
            'claude',
            '-p', prompt,
            '--continue-session', session_id,
            '--output-format', 'json',
            '--no-interactive'
        ]
        
        result = subprocess.run(
            cmd,
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )
        
        return {
            'output': result.stdout,
            'error': result.stderr,
            'return_code': result.returncode
        }
    
    def pause_session(self, session_id: str):
        """Pause a session (for resumption later)"""
        if session_id in self.active_sessions:
            self.active_sessions[session_id]['status'] = 'paused'
            self._save_session(session_id)
    
    def resume_session(self, session_id: str) -> bool:
        """Resume a paused session"""
        session_file = self.sessions_dir / f"{session_id}.json"
        
        if session_file.exists():
            with open(session_file, 'r') as f:
                session_info = json.load(f)
            
            session_info['status'] = 'active'
            session_info['resumed_at'] = datetime.now().isoformat()
            
            self.active_sessions[session_id] = session_info
            self._save_session(session_id)
            return True
        
        return False