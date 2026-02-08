"""
Auto-Commit Service

Automatically commits code changes made by agents to feature branches.
"""

import subprocess
import logging
from pathlib import Path
from typing import Optional
from services.project_workspace import workspace_manager

logger = logging.getLogger(__name__)


class AutoCommitService:
    """Handles automatic git commits for agent changes"""

    async def commit_agent_changes(
        self,
        project: str,
        agent: str,
        task_id: str,
        issue_number: Optional[int] = None,
        custom_message: Optional[str] = None
    ) -> bool:
        """
        Commit changes made by an agent

        Args:
            project: Project name
            agent: Agent name that made the changes
            task_id: Task ID
            issue_number: GitHub issue number (if applicable)
            custom_message: Custom commit message (optional)

        Returns:
            True if commit was successful, False otherwise
        """
        project_dir = workspace_manager.get_project_dir(project)

        if not project_dir.exists():
            logger.error(f"Project directory does not exist: {project_dir}")
            return False

        try:
            # Ensure we're on a feature branch (not main/master)
            current_branch = self._get_current_branch(project_dir)
            if current_branch in ['main', 'master']:
                logger.error(f"WORKFLOW BUG: Agent executed on {current_branch} branch without proper branch preparation!")
                logger.error(f"Project: {project}, Agent: {agent}, Issue: {issue_number}")
                logger.error(f"FeatureBranchManager should have created a branch BEFORE agent execution")
                logger.error(f"Auto-commit REFUSED to create emergency branch - this would bypass parent/sub-issue logic")
                return False

            # Check if there are changes to commit
            has_changes = self._check_for_changes(project_dir)

            if has_changes:
                # Stage all changes
                self._stage_changes(project_dir)

                # Create commit message
                if custom_message:
                    commit_message = custom_message
                else:
                    commit_message = self._generate_commit_message(agent, task_id, issue_number)

                # Commit
                success = self._commit(project_dir, commit_message)
                if not success:
                    logger.error("Failed to commit changes")
                    return False

                logger.info(f"Successfully committed changes for {project} (agent: {agent})")
            else:
                logger.info(f"No changes to commit for {project} after {agent} execution")

            # Always push branch to remote (even if no new commits, there may be unpushed commits)
            if current_branch and current_branch not in ['main', 'master']:
                push_success = self._push_branch(project_dir, current_branch)
                if push_success:
                    logger.info(f"Successfully pushed branch {current_branch} to remote")
                else:
                    logger.warning(f"Failed to push branch {current_branch}, continuing anyway")

            return True

        except Exception as e:
            logger.error(f"Failed to auto-commit changes for {project}: {e}")
            return False

    def _check_for_changes(self, project_dir: Path) -> bool:
        """Check if there are uncommitted changes"""
        try:
            result = subprocess.run(
                ['git', 'status', '--porcelain'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=10
            )

            # If output is empty, no changes
            return bool(result.stdout.strip())

        except Exception as e:
            logger.error(f"Failed to check for changes: {e}")
            return False

    def _get_current_branch(self, project_dir: Path) -> Optional[str]:
        """Get the current branch name"""
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                return result.stdout.strip()

        except Exception as e:
            logger.error(f"Failed to get current branch: {e}")

        return None

    def _stage_changes(self, project_dir: Path) -> bool:
        """Stage all changes"""
        try:
            result = subprocess.run(
                ['git', 'add', '-A'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                logger.info("Staged all changes")
                return True
            else:
                logger.error(f"Failed to stage changes: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Failed to stage changes: {e}")
            return False

    def _commit(self, project_dir: Path, message: str) -> bool:
        """
        Create a commit.

        Skips pre-commit hooks (--no-verify) since it can cause havoc with the orchestrator.
        """
        try:
            # Use heredoc format for multi-line commit message
            # Skip pre-commit hooks for orchestrator commits
            result = subprocess.run(
                ['git', 'commit', '-m', message, '--no-verify'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                logger.info(f"Created commit (skipped hooks): {message[:50]}...")
                return True
            else:
                logger.error(f"Failed to commit: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Failed to create commit: {e}")
            return False

    def _push_branch(self, project_dir: Path, branch_name: str) -> bool:
        """Push branch to remote"""
        try:
            # First, set upstream if not already set
            result = subprocess.run(
                ['git', 'push', '-u', 'origin', branch_name],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                logger.info(f"Pushed branch {branch_name} to origin")
                return True
            else:
                logger.error(f"Failed to push branch: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Failed to push branch {branch_name}: {e}")
            return False

    def _generate_commit_message(
        self,
        agent: str,
        task_id: str,
        issue_number: Optional[int] = None
    ) -> str:
        """Generate a commit message for agent changes"""

        # Map agent names to action verbs
        agent_actions = {
            'dev_environment_setup': 'Configure development environment',
            'business_analyst': 'Add requirements documentation',
            'software_architect': 'Add architecture design',
            'senior_software_engineer': 'Implement feature',
            'senior_qa_engineer': 'Add tests',
            'technical_writer': 'Add documentation',
            'idea_researcher': 'Add research findings'
        }

        action = agent_actions.get(agent, f'Update from {agent}')

        if issue_number:
            message = f"{action} (#{issue_number})\n\n"
            message += f"Automated changes by {agent} agent\n"
            message += f"Task: {task_id}\n\n"
        else:
            message = f"{action}\n\n"
            message += f"Automated changes by {agent} agent\n"
            message += f"Task: {task_id}\n\n"

        message += "🤖 Generated with [Claude Code](https://claude.com/claude-code)\n\n"
        message += "Co-Authored-By: Claude <noreply@anthropic.com>"

        return message


# Global instance
auto_commit_service = AutoCommitService()
