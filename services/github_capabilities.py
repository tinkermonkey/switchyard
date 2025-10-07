"""
GitHub Capabilities Tracker

Centralized tracking of what GitHub features are available based on authentication methods.
"""

import logging
from typing import Dict, List
from enum import Enum

logger = logging.getLogger(__name__)


class GitHubCapability(Enum):
    """Capabilities that may or may not be available based on authentication"""
    PAT_AUTH = "pat_authentication"
    GITHUB_APP_AUTH = "github_app_authentication"
    REPO_ACCESS = "repository_access"
    PROJECTS_V2 = "projects_v2_access"
    GRAPHQL_FULL = "graphql_full_access"
    GRAPHQL_LIMITED = "graphql_limited_access"
    DISCUSSIONS = "discussions_access"
    ISSUES = "issues_access"


class GitHubCapabilities:
    """Track and check GitHub capabilities"""

    def __init__(self):
        self._capabilities: Dict[GitHubCapability, bool] = {}
        self._warnings: List[str] = []
        self._checked = False

    def check_capabilities(self) -> Dict[str, any]:
        """
        Check all GitHub capabilities and store results

        Returns:
            Dictionary with capability status and warnings
        """
        from services.github_app import github_app
        import subprocess

        # Check PAT authentication
        pat_result = subprocess.run(
            ['gh', 'auth', 'status'],
            capture_output=True,
            text=True
        )
        pat_authenticated = pat_result.returncode == 0

        # Check GitHub App
        github_app_enabled = github_app.enabled

        # Determine capabilities
        self._capabilities = {
            GitHubCapability.PAT_AUTH: pat_authenticated,
            GitHubCapability.GITHUB_APP_AUTH: github_app_enabled,
            GitHubCapability.REPO_ACCESS: pat_authenticated,  # Requires at least PAT
            GitHubCapability.PROJECTS_V2: pat_authenticated,  # Can work with PAT
            GitHubCapability.ISSUES: pat_authenticated,  # Can work with PAT
            GitHubCapability.DISCUSSIONS: github_app_enabled,  # Requires GitHub App
            GitHubCapability.GRAPHQL_FULL: github_app_enabled,  # Full GraphQL needs GitHub App
            GitHubCapability.GRAPHQL_LIMITED: pat_authenticated,  # Some GraphQL via gh CLI
        }

        # Build warnings
        self._warnings = []
        if not pat_authenticated:
            self._warnings.append("CRITICAL: PAT authentication failed - orchestrator cannot function")
        if not github_app_enabled:
            self._warnings.append("GitHub App not configured - discussions and advanced GraphQL features unavailable")

        self._checked = True

        return {
            'capabilities': {cap.value: enabled for cap, enabled in self._capabilities.items()},
            'warnings': self._warnings
        }

    def has_capability(self, capability: GitHubCapability) -> bool:
        """
        Check if a specific capability is available

        Args:
            capability: The capability to check

        Returns:
            True if capability is available, False otherwise
        """
        if not self._checked:
            self.check_capabilities()

        return self._capabilities.get(capability, False)

    def require_capability(self, capability: GitHubCapability, operation: str = "operation") -> bool:
        """
        Check if capability is available and log appropriate message if not

        Args:
            capability: Required capability
            operation: Description of operation being attempted

        Returns:
            True if capability is available, False otherwise
        """
        if self.has_capability(capability):
            return True

        logger.warning(f"Cannot perform {operation} - missing capability: {capability.value}")
        return False

    def get_status(self) -> Dict[str, any]:
        """Get current capability status"""
        if not self._checked:
            self.check_capabilities()

        return {
            'capabilities': {cap.value: enabled for cap, enabled in self._capabilities.items()},
            'warnings': self._warnings,
            'healthy': self._capabilities.get(GitHubCapability.PAT_AUTH, False)
        }


# Global instance
github_capabilities = GitHubCapabilities()
