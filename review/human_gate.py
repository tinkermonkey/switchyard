import json
import subprocess
import asyncio
from dataclasses import dataclass, field
from datetime import datetime

class HumanReviewGate:
    """Pause for human review at PR stage"""
    
    async def wait_for_pr_approval(self, pr_url: str, timeout_hours: int = 48):
        """Wait for human PR approval"""
        
        start_time = datetime.now()
        
        while (datetime.now() - start_time).total_seconds() < timeout_hours * 3600:
            # Check PR status
            result = subprocess.run([
                'gh', 'pr', 'view', pr_url, '--json', 'state,reviews'
            ], capture_output=True, text=True)
            
            pr_data = json.loads(result.stdout)
            
            if pr_data['state'] == 'MERGED':
                return {'status': 'merged'}
            
            if pr_data['state'] == 'CLOSED':
                return {'status': 'closed'}
            
            # Check for approvals
            approvals = [r for r in pr_data['reviews'] if r['state'] == 'APPROVED']
            if approvals:
                return {'status': 'approved', 'approvers': approvals}
            
            # Check for requested changes
            changes_requested = [r for r in pr_data['reviews'] if r['state'] == 'CHANGES_REQUESTED']
            if changes_requested:
                # Trigger revision workflow
                return {'status': 'changes_requested', 'reviews': changes_requested}
            
            # Wait before checking again
            await asyncio.sleep(300)  # Check every 5 minutes
        
        return {'status': 'timeout'}