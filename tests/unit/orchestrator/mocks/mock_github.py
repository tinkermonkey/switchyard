"""
Mock GitHub API for testing orchestrator flows

Provides in-memory simulation of GitHub API responses for:
- Issue details
- Project items
- Status updates
- Comments
"""

from typing import Dict, List, Any, Optional
from datetime import datetime


class MockGitHubAPI:
    """Mock GitHub API responses for testing"""
    
    def __init__(self):
        self.issues: Dict[int, Dict[str, Any]] = {}
        self.comments: List[Dict[str, Any]] = []
        self.status_changes: List[tuple] = []
        self.project_items: List[Dict[str, Any]] = []
        self._next_comment_id = 1
        
    def create_issue(
        self,
        number: int,
        title: str,
        status: str,
        state: str = "OPEN",
        repository: str = "test-repo",
        body: str = "",
        labels: List[str] = None,
        assignees: List[str] = None
    ):
        """Create a mock issue"""
        self.issues[number] = {
            'id': f'issue-{number}',
            'number': number,
            'title': title,
            'body': body,
            'state': state,
            'status': status,
            'repository': {'name': repository},
            'url': f'https://github.com/test-org/{repository}/issues/{number}',
            'labels': {'nodes': [{'name': label} for label in (labels or [])]},
            'assignees': {'nodes': [{'login': assignee} for assignee in (assignees or [])]},
            'comments': {'nodes': []},
            'updatedAt': datetime.now().isoformat(),
            'createdAt': datetime.now().isoformat()
        }
        
        # Add to project items if has status
        if status:
            self._add_project_item(number, status, repository)
            
    def _add_project_item(self, issue_number: int, status: str, repository: str):
        """Add issue to project items list"""
        self.project_items.append({
            'id': f'item-{issue_number}',
            'content': {
                'id': f'issue-{issue_number}',
                'number': issue_number,
                'title': self.issues[issue_number]['title'],
                'state': self.issues[issue_number]['state'],
                'repository': {'name': repository},
                'updatedAt': datetime.now().isoformat()
            },
            'fieldValues': {
                'nodes': [{
                    'name': status,
                    'field': {'name': 'Status'}
                }]
            }
        })
    
    def get_issue(self, issue_number: int) -> Optional[Dict[str, Any]]:
        """Get issue details"""
        return self.issues.get(issue_number)
    
    def get_issue_details(self, repository: str, issue_number: int, org: str) -> Dict[str, Any]:
        """Get detailed issue information (matches project_monitor signature)"""
        issue = self.issues.get(issue_number)
        if not issue:
            raise ValueError(f"Issue #{issue_number} not found")
        return issue
    
    def update_issue_status(self, issue_number: int, new_status: str):
        """Update issue status"""
        if issue_number not in self.issues:
            raise ValueError(f"Issue #{issue_number} not found")
            
        old_status = self.issues[issue_number]['status']
        self.issues[issue_number]['status'] = new_status
        self.issues[issue_number]['updatedAt'] = datetime.now().isoformat()
        
        self.status_changes.append((issue_number, old_status, new_status))
        
        # Update project items
        for item in self.project_items:
            if item['content']['number'] == issue_number:
                item['fieldValues']['nodes'][0]['name'] = new_status
                break
    
    def add_issue_comment(self, issue_number: int, body: str, author: str = "bot") -> int:
        """Add a comment to an issue"""
        if issue_number not in self.issues:
            raise ValueError(f"Issue #{issue_number} not found")
            
        comment_id = self._next_comment_id
        self._next_comment_id += 1
        
        comment = {
            'id': comment_id,
            'body': body,
            'author': {'login': author},
            'createdAt': datetime.now().isoformat(),
            'issue_number': issue_number
        }
        
        self.comments.append(comment)
        self.issues[issue_number]['comments']['nodes'].append(comment)
        
        return comment_id
    
    def get_issue_comments(self, issue_number: int) -> List[Dict[str, Any]]:
        """Get all comments for an issue"""
        if issue_number not in self.issues:
            return []
        return self.issues[issue_number]['comments']['nodes']
    
    def get_project_items(self, project_owner: str, project_number: int) -> List[Dict[str, Any]]:
        """Get project items (matches project_monitor signature)"""
        return self.project_items
    
    def close_issue(self, issue_number: int):
        """Close an issue"""
        if issue_number in self.issues:
            self.issues[issue_number]['state'] = 'CLOSED'
            self.issues[issue_number]['updatedAt'] = datetime.now().isoformat()
    
    def get_status_changes(self, issue_number: Optional[int] = None) -> List[tuple]:
        """Get status change history"""
        if issue_number:
            return [sc for sc in self.status_changes if sc[0] == issue_number]
        return self.status_changes
    
    def reset(self):
        """Reset all mock data"""
        self.issues.clear()
        self.comments.clear()
        self.status_changes.clear()
        self.project_items.clear()
        self._next_comment_id = 1
    
    def get_issue_status(self, issue_number: int) -> Optional[str]:
        """Get current status of an issue"""
        issue = self.issues.get(issue_number)
        return issue['status'] if issue else None
    
    def move_issue_to_column(self, project_name: str, board_name: str, 
                            issue_number: int, target_column: str) -> bool:
        """Mock moving issue to a column (matches pipeline_progression signature)"""
        try:
            self.update_issue_status(issue_number, target_column)
            return True
        except Exception:
            return False
    
    # Aliases for convenience
    def add_comment(self, issue_number: int, body: str) -> str:
        """Alias for add_issue_comment"""
        return self.add_issue_comment(issue_number, body)
    
    async def post_issue_comment(self, issue_number: int, body: str, repository: str = None) -> None:
        """Async alias for add_issue_comment (matches GitHubIntegration API)"""
        self.add_issue_comment(issue_number, body)
    
    def get_comments(self, issue_number: int) -> List[Dict[str, Any]]:
        """Alias for get_issue_comments"""
        return self.get_issue_comments(issue_number)

