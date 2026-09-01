import subprocess
import logging
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple
from config.manager import config_manager

logger = logging.getLogger(__name__)


class ProjectWorkspaceManager:
    """Manages project repository checkouts and branch management"""

    def __init__(self, workspace_root: Path = None):
        """
        Initialize workspace manager

        Args:
            workspace_root: Root directory for project checkouts (default: /workspace in container, or parent of orchestrator locally)
        """
        if workspace_root is None:
            # Check if running in container (has /workspace mount)
            container_workspace = Path('/workspace')
            if container_workspace.exists() and container_workspace.is_dir():
                workspace_root = container_workspace
                logger.info("Detected container environment, using /workspace")
            else:
                # Default to sibling directory of orchestrator for local development
                orchestrator_dir = Path(__file__).parent.parent
                workspace_root = orchestrator_dir.parent
                logger.info("Using local development workspace (parent of orchestrator)")

        self.workspace_root = workspace_root
        logger.info(f"ProjectWorkspaceManager initialized with workspace root: {workspace_root}")

        # In-flight per-epic worktrees, keyed by (project_name, epic_id). An epic's
        # worktree spans every sequential sub-issue pipeline run for that epic (created
        # once, reused by every subsequent sub-issue, torn down only on epic completion)
        # - NOT per-container-launch and NOT per-individual-pipeline-run. Mirrors
        # DockerAgentRunner._active_worktrees, but keyed by epic rather than by
        # container, since lifetime spans many container launches.
        self._epic_worktrees: Dict[Tuple[str, str], str] = {}
        # Branch each tracked epic worktree was actually checked out to (for the
        # cache-hit mismatch check in get_or_create_epic_worktree).
        self._epic_worktree_branches: Dict[Tuple[str, str], str] = {}
        # Guards check-then-create/cleanup on _epic_worktrees so two concurrent
        # calls for the same (project, epic_id) can't both attempt to create (or
        # one create/one cleanup can't race) the same worktree.
        self._epic_worktree_lock = threading.Lock()

    def initialize_all_projects(self) -> Dict[str, bool]:
        """
        Initialize workspaces for all configured projects (excludes hidden/test projects)

        Returns:
            Dict mapping project names to whether they need dev environment setup (True = newly cloned/missing Dockerfile.agent)
        """
        logger.info("Initializing all project workspaces")

        # Only initialize visible (non-hidden) projects
        projects = config_manager.list_visible_projects()
        needs_setup = {}

        for project_name in projects:
            try:
                project_config = config_manager.get_project_config(project_name)
                was_cloned = self.initialize_project(project_name, project_config)

                # Check if project needs dev environment setup
                project_dir = self.get_project_dir(project_name)
                dockerfile_agent = project_dir / 'Dockerfile.agent'

                # Need setup if: newly cloned OR missing Dockerfile.agent
                needs_setup[project_name] = was_cloned or not dockerfile_agent.exists()

                if needs_setup[project_name]:
                    logger.info(f"Project {project_name} needs dev environment setup (newly_cloned={was_cloned}, has_dockerfile={dockerfile_agent.exists()})")

            except Exception as e:
                logger.error(f"Failed to initialize project {project_name}: {e}")
                needs_setup[project_name] = False

        return needs_setup

    def initialize_project(self, project_name: str, project_config) -> bool:
        """
        Initialize a project workspace by checking if it exists

        Args:
            project_name: Name of the project
            project_config: Project configuration object

        Returns:
            True if project was newly cloned, False if it already existed
        """
        repo_url = project_config.github.get('repo_url')
        default_branch = project_config.github.get('branch', 'main')

        if not repo_url:
            raise ValueError(f"No repo_url configured for project {project_name}")

        project_dir = self.workspace_root / project_name
        was_cloned = False

        if project_dir.exists() and (project_dir / '.git').exists():
            logger.info(f"Project {project_name} found at {project_dir}")
            # Ensure we're on the default branch and up to date
            self._update_repository(project_dir, default_branch)
        else:
            # Try to clone if directory doesn't exist
            # Note: In container environments with mounted host directories, projects should already exist
            logger.warning(f"Project {project_name} not found at {project_dir}")
            logger.info(f"Attempting to clone from {self._redact_url(repo_url)}")
            try:
                self._clone_repository(repo_url, project_dir, default_branch)
                was_cloned = True
            except Exception as e:
                logger.error(f"Failed to clone {project_name}: {e}")
                logger.info("If running in Docker, ensure project is checked out on host and mounted correctly")
                raise

        # Ensure the remote uses SSH — agent containers have SSH keys but no HTTPS
        # credentials, so an HTTPS remote (e.g. from a prior HTTPS clone) will break
        # every git fetch/pull/push.
        self._ensure_ssh_remote(project_dir)

        return was_cloned

    @staticmethod
    def _redact_url(url: str) -> str:
        """Redact credentials from a URL before logging (https://token@host → https://<redacted>@host)."""
        import re
        return re.sub(r'://[^@]+@', '://<redacted>@', url)

    def _ensure_ssh_remote(self, project_dir: Path):
        """
        Ensure the git remote uses SSH rather than HTTPS.

        Agent containers have SSH keys mounted but no HTTPS credential helper, so
        any workspace cloned via HTTPS will fail on fetch/pull/push. This detects
        an HTTPS origin and rewrites it to the equivalent SSH URL in-place.
        """
        import re
        result = subprocess.run(
            ['git', 'remote', 'get-url', 'origin'],
            cwd=project_dir, capture_output=True, text=True
        )
        current_url = result.stdout.strip()
        if not current_url or current_url.startswith('git@'):
            return  # already SSH or no remote — nothing to do

        match = re.search(r'github\.com[/:](.+?)(?:\.git)?$', current_url)
        if match:
            ssh_url = f"git@github.com:{match.group(1)}.git"
            subprocess.run(
                ['git', 'remote', 'set-url', 'origin', ssh_url],
                cwd=project_dir, capture_output=True
            )
            logger.info(f"Converted remote URL to SSH: {self._redact_url(current_url)} → {ssh_url}")

    def _clone_repository(self, repo_url: str, target_dir: Path, branch: str):
        """Clone a repository to the target directory"""
        try:
            cmd = ['git', 'clone', '--branch', branch, repo_url, str(target_dir)]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            if result.returncode != 0:
                raise Exception(f"Git clone failed: {self._redact_url(result.stderr)}")

            logger.info(f"Successfully cloned repository to {target_dir}")
        except subprocess.TimeoutExpired:
            raise Exception("Git clone timed out")
        except Exception as e:
            raise Exception(f"Failed to clone repository: {e}")

    def _update_repository(self, repo_dir: Path, branch: str):
        """Update an existing repository to latest"""
        try:
            # Fetch latest changes
            result = subprocess.run(
                ['git', 'fetch', 'origin'],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode != 0:
                logger.warning(f"Git fetch failed: {result.stderr}")
                return

            # Check current branch
            result = subprocess.run(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=10
            )

            current_branch = result.stdout.strip()
            logger.info(f"Repository is on branch: {current_branch}")

            # Pull latest changes for whatever branch is currently checked out
            # Note: We don't force a branch switch here because:
            # 1. Agents always prepare the correct branch when they launch
            # 2. Forcing to default branch can destroy state and cause timing issues
            # 3. Projects may legitimately be on feature branches between agent runs
            result = subprocess.run(
                ['git', 'pull', '--ff-only'],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode != 0:
                logger.warning(f"Git pull failed: {result.stderr}")
            else:
                logger.info(f"Updated repository to latest {branch}")

        except Exception as e:
            logger.warning(f"Failed to update repository: {e}")

    def get_project_dir(
        self,
        project_name: str,
        epic_id: Optional[str] = None,
        branch_name: Optional[str] = None,
        default_branch: str = 'main',
    ) -> Path:
        """Get the directory path for a project.

        With no epic_id, this is behaviorally identical to the original single-checkout
        implementation: it returns the base clone path (a plain path-join, no side
        effects) — the ~5 existing call sites that must keep operating on the shared
        base clone are unaffected.

        When epic_id is given, resolves to an isolated, branch-aware (non-detached) git
        worktree for that epic instead of the base clone — lazily creating it if this is
        the epic's first sub-issue, or idempotently reusing the existing worktree if one
        is already in flight for (project_name, epic_id). Worktree granularity is per
        epic, not per sub-issue: every sequential sub-issue of the same epic resolves to
        the same worktree path, isolated from the base clone and from other epics'
        worktrees.

        Args:
            project_name: Name of the project
            epic_id: Epic issue number to scope an isolated worktree to. For
                planning_design, this is the board item's own issue number. For
                sdlc_execution, this is the sub-issue's PARENT epic issue number
                (resolving that parent, e.g. via FeatureBranchManager.get_parent_issue,
                is the caller's job — not this method's). None (default) preserves
                today's shared-base-clone behavior exactly.
            branch_name: Branch the epic's worktree should be checked out to. Only
                consulted (and required) the first time a given epic's worktree is
                created; ignored on every subsequent call that just reuses the
                already-in-flight worktree.
            default_branch: Base branch to cut a brand-new epic branch from, when
                branch_name doesn't already exist on origin. Defaults to 'main'.

        Returns:
            The base clone path (epic_id=None), or the epic's isolated worktree path.
        """
        if epic_id is None:
            return self.workspace_root / project_name

        return self.get_or_create_epic_worktree(
            project_name, epic_id, branch_name, default_branch=default_branch
        )

    def _epic_worktree_path(self, project_name: str, epic_id: str) -> Path:
        """Staging path for an epic's worktree: .orchestrator/worktrees/<project>/<epic_id>/

        Deliberately a different staging subdirectory than DockerAgentRunner's
        `.orchestrator/tmp/ref-worktrees/` (detached, per-container reference-repo
        worktrees) so the two namespaces, and their respective prune sweeps, never
        collide.
        """
        return self.workspace_root / '.orchestrator' / 'worktrees' / project_name / str(epic_id)

    def get_or_create_epic_worktree(
        self,
        project_name: str,
        epic_id: str,
        branch_name: Optional[str] = None,
        default_branch: str = 'main',
    ) -> Path:
        """
        Get (creating if absent) an isolated, non-detached git worktree for one epic.

        Mirrors DockerAgentRunner._create_reference_worktree's create-if-absent pattern,
        but sourced from the primary (non-bare) base clone rather than a dedicated
        reference repo, checked out to a real branch (not `--detach`), and long-lived
        across every sub-issue pipeline run of the epic rather than scoped to one
        container launch.

        - New-branch case (branch_name doesn't exist on origin yet): fetches
          origin/<default_branch> and runs `git worktree add -b <branch_name> <path>
          origin/<default_branch>` — mirrors FeatureBranchManager.create_branch_from_main.
        - Existing-branch case: fetches origin/<branch_name>, then runs a plain
          `git worktree add <path> <branch_name>`.

        Idempotent: a second call for the same (project_name, epic_id) returns the
        already-in-flight worktree path without touching git again, so two sequential
        sub-issues of the same epic always resolve to the same worktree.

        Args:
            project_name: Name of the project (must already have a base clone).
            epic_id: Epic issue number scoping this worktree.
            branch_name: Branch to check the worktree out to. Required the first time
                this epic's worktree is created; unused on reuse.
            default_branch: Base branch to cut a new epic branch from if branch_name
                doesn't exist on origin yet.

        Returns:
            The epic's worktree path.

        Raises:
            ValueError: No worktree exists yet for this epic and branch_name was not
                given, or the project has no base clone to source the worktree from.
            RuntimeError: The underlying git worktree add command failed.
        """
        key = (project_name, str(epic_id))
        with self._epic_worktree_lock:
            existing = self._epic_worktrees.get(key)
            if existing is not None:
                tracked_branch = self._epic_worktree_branches.get(key)
                if branch_name and tracked_branch and branch_name != tracked_branch:
                    logger.warning(
                        f"get_or_create_epic_worktree called for {project_name} epic #{epic_id} "
                        f"with branch_name={branch_name!r}, but its existing worktree is already "
                        f"on {tracked_branch!r}; worktrees are per-epic (not per-branch), so the "
                        "existing worktree is returned unchanged."
                    )
                logger.debug(f"Reusing existing worktree for {project_name} epic #{epic_id}: {existing}")
                return Path(existing)

            if not branch_name:
                raise ValueError(
                    f"No worktree exists yet for {project_name} epic #{epic_id}; "
                    "branch_name is required to create one"
                )

            base_repo_dir = self.workspace_root / project_name
            if not (base_repo_dir / '.git').exists():
                raise ValueError(f"Base clone for project {project_name} not found at {base_repo_dir}")

            worktree_path = self._epic_worktree_path(project_name, epic_id)
            worktree_path.parent.mkdir(parents=True, exist_ok=True)

            self._add_epic_worktree(base_repo_dir, worktree_path, branch_name, default_branch)

            self._epic_worktrees[key] = str(worktree_path)
            self._epic_worktree_branches[key] = branch_name
            logger.info(
                f"Created epic worktree for {project_name} epic #{epic_id} "
                f"at {worktree_path} (branch={branch_name})"
            )
            return worktree_path

    @staticmethod
    def _add_epic_worktree(base_repo_dir: Path, worktree_path: Path, branch_name: str, default_branch: str) -> None:
        """Run the actual `git worktree add` for a new epic worktree.

        Tries the existing-branch path first (fetch origin/<branch_name> then a plain
        `worktree add`); falls back to creating a brand-new branch from
        origin/<default_branch> when branch_name doesn't exist on origin yet.
        """
        fetch_existing = subprocess.run(
            ['git', '-C', str(base_repo_dir), 'fetch', 'origin', branch_name, '--quiet'],
            capture_output=True, text=True, timeout=30
        )

        if fetch_existing.returncode == 0:
            result = subprocess.run(
                ['git', '-C', str(base_repo_dir), 'worktree', 'add', str(worktree_path), branch_name],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to add worktree for existing branch {branch_name}: {result.stderr.strip()}"
                )
            return

        # branch_name doesn't exist on origin yet — cut a new one from default_branch
        fetch_default = subprocess.run(
            ['git', '-C', str(base_repo_dir), 'fetch', 'origin', default_branch, '--quiet'],
            capture_output=True, text=True, timeout=30
        )
        if fetch_default.returncode != 0:
            raise RuntimeError(
                f"Failed to fetch origin/{default_branch} while creating worktree "
                f"branch {branch_name}: {fetch_default.stderr.strip()}"
            )

        result = subprocess.run(
            ['git', '-C', str(base_repo_dir), 'worktree', 'add', '-b', branch_name,
             str(worktree_path), f'origin/{default_branch}'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            # Race with a concurrently-created branch of the same name (e.g. another
            # process just pushed it) — retry once as the existing-branch case.
            retry_fetch = subprocess.run(
                ['git', '-C', str(base_repo_dir), 'fetch', 'origin', branch_name, '--quiet'],
                capture_output=True, text=True, timeout=30
            )
            if retry_fetch.returncode == 0:
                result = subprocess.run(
                    ['git', '-C', str(base_repo_dir), 'worktree', 'add', str(worktree_path), branch_name],
                    capture_output=True, text=True, timeout=30
                )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to create worktree branch {branch_name}: {result.stderr.strip()}"
                )

    @staticmethod
    def _push_local_commits_if_any(worktree_path) -> None:
        """Best-effort push of any local-only commits before an epic worktree is torn down.

        Mirrors GitWorkflowManager.pull_rebase()'s established pattern (commit 6eea6ef):
        force-removing a worktree that holds a locally-committed-but-not-yet-pushed fix
        (e.g. a repair-cycle step that skipped its own push) would otherwise silently and
        permanently discard that work. Only logs and proceeds on push failure — cleanup
        must never hang or block on a genuine conflict.
        """
        try:
            branch_result = subprocess.run(
                ['git', '-C', str(worktree_path), 'rev-parse', '--abbrev-ref', 'HEAD'],
                capture_output=True, text=True, timeout=10
            )
            if branch_result.returncode != 0:
                return
            branch = branch_result.stdout.strip()
            if not branch or branch == 'HEAD':
                return  # detached HEAD; nothing meaningful to push

            ahead_result = subprocess.run(
                ['git', '-C', str(worktree_path), 'rev-list', '--count', f'origin/{branch}..HEAD'],
                capture_output=True, text=True, timeout=10
            )
            if ahead_result.returncode != 0:
                return  # e.g. no such remote branch yet; nothing we can safely reconcile here
            ahead_count = int(ahead_result.stdout.strip() or '0')
            if ahead_count == 0:
                return

            logger.info(
                f"{ahead_count} local commit(s) not on origin/{branch} in {worktree_path}; "
                "pushing before removal"
            )
            push_result = subprocess.run(
                ['git', '-C', str(worktree_path), 'push', 'origin', branch],
                capture_output=True, text=True, timeout=30
            )
            if push_result.returncode == 0:
                logger.info(
                    f"Pushed {ahead_count} previously-local commit(s) from {worktree_path} "
                    f"to origin/{branch}"
                )
            else:
                logger.error(
                    f"Could not push {ahead_count} local commit(s) from {worktree_path} to "
                    f"origin/{branch} before removal ({push_result.stderr.strip()}). "
                    "Discarding them — work in these commits is lost."
                )
        except Exception as e:
            logger.warning(f"Failed to check/push local commits in {worktree_path} before removal: {e}")

    def cleanup_epic_worktree(self, project_name: str, epic_id: str) -> bool:
        """
        Remove an epic's worktree once the whole epic is complete.

        This is tied to EPIC completion (all sub-issues done for sdlc_execution, or the
        epic issue's own board exit/closure for planning_design) — NOT to any individual
        sub-issue's pipeline-run completion, and NOT to container completion. Wiring this
        up to actual epic-completion detection is out of scope here; this only exposes
        the mechanism for those callers to invoke.

        A crash before this ever runs simply leaves the worktree on disk — it's caught by
        prune_epic_worktrees() on the next orchestrator startup instead.

        Returns:
            True if a worktree was found and removed (or already gone), False if this
            epic had no tracked worktree to clean up.
        """
        key = (project_name, str(epic_id))
        with self._epic_worktree_lock:
            worktree_path = self._epic_worktrees.get(key)

            if worktree_path is None:
                logger.debug(f"No in-flight worktree tracked for {project_name} epic #{epic_id}; nothing to clean up")
                return False

            self._push_local_commits_if_any(worktree_path)

            base_repo_dir = self.workspace_root / project_name
            removed = False
            try:
                result = subprocess.run(
                    ['git', '-C', str(base_repo_dir), 'worktree', 'remove', '--force', worktree_path],
                    capture_output=True, text=True, timeout=15
                )
                if result.returncode == 0:
                    removed = True
                else:
                    logger.warning(
                        f"git worktree remove failed for {worktree_path}: {result.stderr.strip()}; "
                        "removing directory directly"
                    )
                    import shutil
                    shutil.rmtree(worktree_path, ignore_errors=True)
                    subprocess.run(
                        ['git', '-C', str(base_repo_dir), 'worktree', 'prune'],
                        capture_output=True, timeout=15
                    )
                    removed = not Path(worktree_path).exists()
            except Exception as e:
                logger.warning(f"Failed to clean up epic worktree {worktree_path}: {e}")
                removed = not Path(worktree_path).exists()

            if removed:
                self._epic_worktrees.pop(key, None)
                self._epic_worktree_branches.pop(key, None)
                logger.info(f"Cleaned up epic worktree for {project_name} epic #{epic_id} at {worktree_path}")
            else:
                logger.error(
                    f"Failed to remove epic worktree for {project_name} epic #{epic_id} at "
                    f"{worktree_path}; leaving it tracked rather than silently losing the reference "
                    "to a worktree that still exists on disk"
                )

            return removed

    def prune_epic_worktrees(self) -> None:
        """Remove all staged epic worktrees and prune git metadata.

        Call this at orchestrator startup to clean up any epic worktrees left behind by
        a previous crash. Safe to call even if the directory doesn't exist.

        Sibling of DockerAgentRunner.prune_reference_worktrees() for the `.orchestrator/
        worktrees/` staging namespace (per-epic, branch-aware worktrees) rather than
        `.orchestrator/tmp/ref-worktrees/` (per-container, detached reference worktrees)
        — kept as a parallel routine rather than a shared one since the two live in
        different modules with different owning lifecycles (ProjectWorkspaceManager vs.
        DockerAgentRunner).

        Since self._epic_worktrees starts empty on every fresh process, any worktree
        found on disk at startup is — from this process's perspective — untracked; it is
        safe to remove unconditionally because it will be transparently recreated (fetch
        + worktree add, cheap) the next time get_project_dir()/get_or_create_epic_worktree()
        is called for that epic, exactly like a reference worktree is recreated per
        container run.
        """
        staging_root = self.workspace_root / '.orchestrator' / 'worktrees'
        try:
            if not staging_root.is_dir():
                return

            try:
                project_stagings = list(staging_root.iterdir())
            except OSError as e:
                logger.warning(f"Failed to list epic worktree staging root {staging_root}: {e}")
                return

            for project_staging in project_stagings:
                if not project_staging.is_dir():
                    continue
                repo_path = self.workspace_root / project_staging.name
                try:
                    worktree_paths = list(project_staging.iterdir())
                except OSError as e:
                    logger.warning(f"Failed to list epic worktrees under {project_staging}: {e}")
                    continue
                for worktree_path in worktree_paths:
                    if not worktree_path.is_dir():
                        continue
                    self._push_local_commits_if_any(worktree_path)
                    try:
                        subprocess.run(
                            ['git', '-C', str(repo_path), 'worktree', 'remove', '--force', str(worktree_path)],
                            capture_output=True, timeout=15
                        )
                    except Exception:
                        pass
                    if worktree_path.is_dir():
                        import shutil
                        shutil.rmtree(worktree_path, ignore_errors=True)
                # Prune any remaining stale metadata entries
                try:
                    subprocess.run(
                        ['git', '-C', str(repo_path), 'worktree', 'prune'],
                        capture_output=True, timeout=15
                    )
                except Exception:
                    pass
                # Remove the now-empty staging directory for this project
                try:
                    if project_staging.is_dir() and not any(project_staging.iterdir()):
                        project_staging.rmdir()
                except OSError as e:
                    logger.warning(f"Failed to remove empty epic worktree staging dir {project_staging}: {e}")

            logger.info(f"Pruned epic worktrees under {staging_root}")
        except Exception as e:
            # This runs on every orchestrator startup, unconditionally (main.py has no
            # try/except around the call site) — it must never raise, or it would take
            # down orchestrator startup entirely over a stale-worktree cleanup failure.
            logger.error(f"prune_epic_worktrees failed unexpectedly: {e}", exc_info=True)

    def ensure_branch(self, project_name: str, branch_name: str, create_if_missing: bool = True) -> bool:
        """
        DEPRECATED: Use GitWorkflowManager.checkout_branch() instead.

        This method is deprecated because it can create branches without proper tracking.
        Use services.git_workflow_manager.checkout_branch() for checkout operations,
        or services.feature_branch_manager.ensure_and_prepare_branch() for branch creation.

        Args:
            project_name: Name of the project
            branch_name: Branch to switch to
            create_if_missing: Create branch if it doesn't exist (DANGEROUS - use FeatureBranchManager instead)

        Returns:
            True if successful, False otherwise
        """
        logger.warning(
            f"DEPRECATED: ensure_branch() called for {project_name}/{branch_name}. "
            "Use GitWorkflowManager.checkout_branch() or FeatureBranchManager instead."
        )
        project_dir = self.get_project_dir(project_name)

        if not project_dir.exists():
            logger.error(f"Project directory does not exist: {project_dir}")
            return False

        try:
            # Check if branch exists
            result = subprocess.run(
                ['git', 'rev-parse', '--verify', f'origin/{branch_name}'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=10
            )

            branch_exists = result.returncode == 0

            if not branch_exists and create_if_missing:
                # Create new branch from current HEAD
                logger.info(f"Creating new branch {branch_name}")
                result = subprocess.run(
                    ['git', 'checkout', '-b', branch_name],
                    cwd=project_dir,
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                if result.returncode != 0:
                    logger.error(f"Failed to create branch: {result.stderr}")
                    return False
            else:
                # Checkout existing branch
                logger.info(f"Checking out branch {branch_name}")
                result = subprocess.run(
                    ['git', 'checkout', branch_name],
                    cwd=project_dir,
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                if result.returncode != 0:
                    logger.error(f"Failed to checkout branch: {result.stderr}")
                    return False

            return True

        except Exception as e:
            logger.error(f"Failed to ensure branch {branch_name}: {e}")
            return False

    def get_current_branch(self, project_name: str) -> Optional[str]:
        """Get the current branch name for a project"""
        project_dir = self.get_project_dir(project_name)

        if not project_dir.exists():
            return None

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


# Global instance
workspace_manager = ProjectWorkspaceManager()
