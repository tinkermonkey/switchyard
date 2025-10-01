import subprocess
from typing import Dict, Any

class KanbanGitWorkflow:
    """Maps Kanban column transitions to git operations"""
    
    def __init__(self, orchestrator, branch_manager):
        self.orchestrator = orchestrator
        self.branch_manager = branch_manager
        
        # Map columns to git actions
        self.column_actions = {
            'Backlog': self._on_backlog,
            'Requirements Analysis': self._on_requirements,
            'Design': self._on_design,
            'Ready for Development': self._on_ready,
            'In Development': self._on_development_start,
            'Code Review': self._on_code_review,
            'Testing': self._on_testing,
            'Ready for Deploy': self._on_ready_deploy,
            'Done': self._on_done
        }
    
    async def handle_card_move(self, event: Dict[str, Any]):
        """Main entry point for card movement events"""
        
        column = event['project_card']['column_name']
        issue = event['issue']
        project_name = event['project']['name']
        
        # Get the appropriate handler
        handler = self.column_actions.get(column)
        if handler:
            await handler(issue, project_name)
    
    async def _on_backlog(self, issue: Dict, project: str):
        """Card added to backlog - no git action needed"""
        pass
    
    async def _on_requirements(self, issue: Dict, project: str):
        """Start requirements - work on main branch"""
        # Requirements work happens on main branch in documents
        await self.orchestrator.trigger_agent(
            agent='business_analyst',
            project=project,
            context={
                'issue': issue,
                'branch': 'main',  # Stay on main for requirements
                'work_type': 'documentation'
            }
        )
    
    async def _on_development_start(self, issue: Dict, project: str):
        """Start development - create feature branch"""
        
        # Create feature branch
        branch_name = self.branch_manager.create_feature_branch_from_issue(issue)
        
        # Update issue with branch info
        self._update_issue_labels(issue['number'], ['in-development'])
        
        # Trigger development agent
        await self.orchestrator.trigger_agent(
            agent='senior_software_engineer',
            project=project,
            context={
                'issue': issue,
                'branch': branch_name,
                'requirements': self._get_requirements(issue)
            }
        )
    
    async def _on_code_review(self, issue: Dict, project: str):
        """Move to code review - create PR"""
        
        # Get current branch
        branch = self._get_issue_branch(issue)
        
        # Run final tests and commit
        test_results = await self._run_pre_pr_tests(project, branch)
        
        # Create PR
        pr_url = self.branch_manager.create_pull_request(
            issue=issue,
            branch_name=branch,
            completion_summary=test_results
        )
        
        # Trigger code review agent
        await self.orchestrator.trigger_agent(
            agent='code_reviewer',
            project=project,
            context={
                'issue': issue,
                'pr_url': pr_url,
                'branch': branch
            }
        )
        
        # Update issue
        self._update_issue_labels(issue['number'], ['needs-review'])
    
    async def _on_testing(self, issue: Dict, project: str):
        """QA testing phase - work on feature branch"""
        
        branch = self._get_issue_branch(issue)
        
        # Trigger QA agent
        await self.orchestrator.trigger_agent(
            agent='qa_engineer',
            project=project,
            context={
                'issue': issue,
                'branch': branch,
                'pr_url': self._get_pr_url(issue)
            }
        )
    
    async def _on_done(self, issue: Dict, project: str):
        """Merge PR and clean up branch"""
        
        pr_url = self._get_pr_url(issue)
        
        if pr_url and self._is_pr_approved(pr_url):
            # Merge PR
            subprocess.run([
                'gh', 'pr', 'merge', pr_url,
                '--merge',  # or --squash based on preference
                '--delete-branch'
            ])
            
            # Update issue
            self._update_issue_labels(issue['number'], ['completed'])
            self._comment_on_issue(
                issue['number'],
                f"Merged to main and deleted feature branch"
            )