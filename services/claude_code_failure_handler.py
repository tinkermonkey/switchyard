"""
Handler for marking in-progress agent executions as failed when Claude Code
circuit breaker opens due to token limits.
"""

import logging
import glob
import json
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)


def mark_in_progress_executions_as_failed(reason: str = "Claude Code token limit reached"):
    """
    Mark all in-progress agent executions as failed.
    
    This is called when the Claude Code circuit breaker opens to prevent
    the orchestrator from waiting for agents that can't possibly run.
    
    Args:
        reason: Reason for marking as failed
    """
    try:
        state_dir = Path('/app/state/execution_history')
        if not state_dir.exists():
            logger.warning(f"Execution history directory not found: {state_dir}")
            return
        
        marked_count = 0
        
        # Find all in-progress execution files
        for state_file in glob.glob(str(state_dir / '*.yaml')):
            try:
                import yaml
                with open(state_file, 'r') as f:
                    state = yaml.safe_load(f)
                
                if state and state.get('status') == 'in_progress':
                    # Mark as failed
                    state['status'] = 'failed'
                    state['outcome'] = 'failure'
                    state['failure_reason'] = reason
                    state['failed_at'] = datetime.now().isoformat()
                    
                    # Write back
                    with open(state_file, 'w') as f:
                        yaml.dump(state, f)
                    
                    project = state.get('project', 'unknown')
                    issue = state.get('issue_number', 'unknown')
                    agent = state.get('agent', 'unknown')
                    logger.warning(
                        f"Marked as failed: {project}#{issue} {agent} - {reason}"
                    )
                    marked_count += 1
            except Exception as e:
                logger.error(f"Error processing state file {state_file}: {e}")
        
        if marked_count > 0:
            logger.info(f"Marked {marked_count} in-progress executions as failed")
    
    except Exception as e:
        logger.error(f"Error marking in-progress executions as failed: {e}")


def get_in_progress_execution_count() -> int:
    """Get count of in-progress agent executions."""
    try:
        state_dir = Path('/app/state/execution_history')
        if not state_dir.exists():
            return 0
        
        count = 0
        for state_file in glob.glob(str(state_dir / '*.yaml')):
            try:
                import yaml
                with open(state_file, 'r') as f:
                    state = yaml.safe_load(f)
                
                if state and state.get('status') == 'in_progress':
                    count += 1
            except Exception:
                pass
        
        return count
    except Exception as e:
        logger.error(f"Error counting in-progress executions: {e}")
        return 0
