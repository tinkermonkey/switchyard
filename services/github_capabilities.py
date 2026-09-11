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
    PROJECTS_V2_WRITE = "projects_v2_write_access"


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

        # Which credential GitHubAPIClient is actually routing its own calls
        # on (WI-4). `gh auth status` succeeding no longer implies "a PAT is
        # present": it exits 0 just as happily for a GitHub App installation
        # token in GH_TOKEN, so credential identity has to be asked for
        # directly rather than inferred from that probe.
        from services.github_api_client import get_github_client
        from services.github_app_credentials import CREDENTIAL_APP
        try:
            active_credential = get_github_client()._resolve_credential()
        except Exception as e:
            logger.warning(f"Could not resolve active GitHub credential: {e}")
            active_credential = None

        projects_write, projects_detail = self._probe_projects_v2_write(
            active_credential, github_app_enabled, pat_authenticated
        )

        # Either credential is sufficient on its own for everything except
        # Discussions writes, which need the App.
        any_auth = pat_authenticated or github_app_enabled

        # Determine capabilities
        self._capabilities = {
            GitHubCapability.PAT_AUTH: pat_authenticated,
            GitHubCapability.GITHUB_APP_AUTH: github_app_enabled,
            GitHubCapability.REPO_ACCESS: any_auth,
            # No longer assumed from "a PAT exists" - actually probed, because
            # the failure mode it guards is silent (see
            # _probe_projects_v2_write).
            GitHubCapability.PROJECTS_V2: projects_write,
            GitHubCapability.PROJECTS_V2_WRITE: projects_write,
            GitHubCapability.ISSUES: any_auth,
            GitHubCapability.DISCUSSIONS: github_app_enabled,  # Requires GitHub App
            GitHubCapability.GRAPHQL_FULL: github_app_enabled,  # Full GraphQL needs GitHub App
            GitHubCapability.GRAPHQL_LIMITED: any_auth,
        }

        # Build warnings
        self._warnings = []
        if not any_auth:
            self._warnings.append(
                "CRITICAL: no usable GitHub credential (neither a PAT nor a "
                "configured GitHub App) - orchestrator cannot function"
            )
        if not github_app_enabled:
            self._warnings.append("GitHub App not configured - discussions and advanced GraphQL features unavailable")
        if not projects_write:
            self._warnings.append(
                f"CRITICAL: the active GitHub credential cannot write Projects v2 "
                f"boards ({projects_detail}). Board reconciliation will be SKIPPED - "
                f"without this guard a credential missing Projects permission reads "
                f"every existing board as absent and creates duplicates."
            )
        self._active_credential = active_credential
        self._projects_detail = projects_detail

        self._checked = True

        return {
            'capabilities': {cap.value: enabled for cap, enabled in self._capabilities.items()},
            'warnings': self._warnings
        }

    @staticmethod
    def _probe_projects_v2_write(active_credential, github_app_enabled, pat_authenticated):
        """(can_write_projects_v2, human_readable_detail).

        Why this asks the credential what it was GRANTED rather than asking
        GitHub for a list of boards: a Projects v2 query made with a
        credential that lacks Projects permission does not fail. It returns an
        empty, successful result - verified against a live installation, where
        the same query returned 55 projects on a PAT and 0 on an App token with
        no `projects` permission. Board reconciliation reads that empty result
        as "no board exists yet" and creates a duplicate of every board, so the
        probe has to be able to tell "none" from "not allowed", and only the
        grant can.

        GitHub App: the token-exchange response carries the granted
        permissions. Org-level Projects v2 appears as `organization_projects`;
        repository-scoped project permissions appear as `repository_projects`.
        Any key containing "project" with a non-read level is accepted, and the
        keys actually seen are reported, so a naming change surfaces as a
        legible diagnostic instead of a silent denial.

        PAT: `x-oauth-scopes` on any authenticated REST response lists the
        token's scopes; Projects v2 writes need `project` (`read:project`
        alone is not enough to create or update a board).
        """
        from services.github_app_credentials import CREDENTIAL_APP

        if active_credential == CREDENTIAL_APP:
            try:
                from services.github_app import github_app
                perms = github_app.get_installation_permissions()
            except Exception as e:
                return False, f"could not read GitHub App installation permissions: {e}"
            if perms is None:
                return False, "GitHub App installation permissions unavailable"
            project_perms = {k: v for k, v in perms.items() if 'project' in k.lower()}
            writable = [k for k, v in project_perms.items() if v != 'read']
            if writable:
                return True, f"GitHub App grants {', '.join(sorted(writable))}"
            return False, (
                f"GitHub App installation has no writable projects permission; "
                f"granted permissions are: {', '.join(sorted(perms)) or '(none)'}. "
                f"Grant 'Organization permissions -> Projects: Read and write' "
                f"and reinstall the App."
            )

        if not pat_authenticated:
            return False, "no authenticated credential to check"

        try:
            import subprocess
            result = subprocess.run(
                ['gh', 'api', '--include', '-X', 'GET', 'user'],
                capture_output=True, text=True, timeout=15,
            )
            scopes_line = next(
                (ln for ln in result.stdout.splitlines()
                 if ln.lower().startswith('x-oauth-scopes:')),
                None,
            )
        except Exception as e:
            return False, f"could not read PAT scopes: {e}"

        if scopes_line is None:
            # A fine-grained PAT reports no x-oauth-scopes header at all. It
            # may well have Projects access, but this probe cannot prove it -
            # report unknown-but-allowed rather than blocking reconciliation on
            # a check that does not apply to this token type.
            return True, "PAT scopes not reported (fine-grained token?) - not verified"

        scopes = {sc.strip() for sc in scopes_line.split(':', 1)[1].split(',')}
        if 'project' in scopes:
            return True, "PAT has the 'project' scope"
        return False, (
            f"PAT lacks the 'project' scope (has: {', '.join(sorted(s for s in scopes if s)) or '(none)'}). "
            f"Add it at https://github.com/settings/tokens"
        )

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
            'active_credential': getattr(self, '_active_credential', None),
            'projects_v2_detail': getattr(self, '_projects_detail', None),
            # Health means "some usable credential", not "a PAT specifically" -
            # an App-only deployment is a supported configuration (WI-4/WI-5).
            'healthy': (
                self._capabilities.get(GitHubCapability.PAT_AUTH, False)
                or self._capabilities.get(GitHubCapability.GITHUB_APP_AUTH, False)
            )
        }


# Global instance
github_capabilities = GitHubCapabilities()
