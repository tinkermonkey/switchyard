import subprocess
import logging
import shutil
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

                # Check if project needs dev environment setup.
                # INTENTIONALLY base-clone-scoped, not migrated to epic-worktree
                # resolution (#48). This startup loop runs once per project before
                # any board is polled and before any issue/epic exists to scope a
                # worktree by -- Dockerfile.agent presence is a per-project, not
                # per-epic, property anyway. If a future caller needs a
                # worktree-scoped result here, that's a larger change than this
                # startup check.
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
        epic_id_str = str(epic_id).strip()
        if not epic_id_str:
            raise ValueError(
                f"epic_id must be a non-empty value to scope a worktree path for "
                f"project {project_name!r} (got {epic_id!r}); an empty/falsy epic_id "
                "would collapse the path to the shared per-project staging directory."
            )
        return self.workspace_root / '.orchestrator' / 'worktrees' / project_name / epic_id_str

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
          origin/<default_branch>` — the same fetch-then-branch-from-main pattern
          FeatureBranchManager.create_branch_from_main used against the shared base
          clone before that method was removed as dead code (#124/WI-E, #119).
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
        newly_created = False
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

            # Already on disk despite an empty in-memory cache -- e.g. this epic's
            # worktree was created before an orchestrator restart. self._epic_worktrees
            # is populated fresh on every process start, but the directory and git's
            # own worktree registration persist across restarts. Adopt it instead of
            # attempting `git worktree add` again, which git unconditionally refuses
            # ("already used by worktree at ...") since it's already registered --
            # confirmed via the #48 review to otherwise crash restart-recovery's
            # auto-commit path outright.
            if worktree_path.exists() and (worktree_path / '.git').exists():
                actual_branch = self._current_worktree_branch(worktree_path) or branch_name
                if branch_name and actual_branch and branch_name != actual_branch:
                    logger.warning(
                        f"Adopting pre-existing epic worktree for {project_name} epic "
                        f"#{epic_id} at {worktree_path}: requested branch_name="
                        f"{branch_name!r} but it's actually on {actual_branch!r} -- "
                        "keeping the worktree's real branch rather than the request."
                    )
                self._epic_worktrees[key] = str(worktree_path)
                self._epic_worktree_branches[key] = actual_branch
                logger.info(
                    f"Adopted pre-existing epic worktree for {project_name} epic "
                    f"#{epic_id} at {worktree_path} (branch={actual_branch})"
                )
                return worktree_path

            worktree_path.parent.mkdir(parents=True, exist_ok=True)

            self._add_epic_worktree(base_repo_dir, worktree_path, branch_name, default_branch)

            self._epic_worktrees[key] = str(worktree_path)
            self._epic_worktree_branches[key] = branch_name
            logger.info(
                f"Created epic worktree for {project_name} epic #{epic_id} "
                f"at {worktree_path} (branch={branch_name})"
            )
            newly_created = True

        # Best-effort baked-dependency extraction runs in a detached background
        # thread, OUTSIDE _epic_worktree_lock (#50 review, 2nd pass): docker
        # create/cp/rm can take up to _DOCKER_CP_TIMEOUT_SECONDS (5 min) for a large
        # dependency tree. get_or_create_epic_worktree() is a plain sync method
        # called directly (no asyncio.to_thread) from async callers like
        # agent_executor.py's execute_agent -- running extraction inline, even
        # unlocked, would still block whichever thread called this (the event loop
        # thread, in the common case) for that entire window. Extraction is a pure
        # performance optimization, never correctness-critical: a fresh install by
        # the dev-environment agent is the documented fallback whenever it hasn't
        # finished (or hasn't run at all, e.g. an old-convention image) by the time
        # an agent actually needs the dependency, exactly like the "nothing at
        # BAKED_DEPS_PATH yet" case already behaves -- so not waiting for it here
        # doesn't add a new class of risk, it just makes the already-existing
        # fallback path a little more likely to be hit on a freshly-created epic's
        # very first task. Runs only on this brand-new-worktree path (never cache-hit
        # reuse or restart-adoption, both handled inside the lock above).
        if newly_created:
            threading.Thread(
                target=self._extract_baked_dependencies_if_available,
                args=(project_name, worktree_path),
                name=f"extract-deps-{project_name}-{epic_id}",
                daemon=True,
            ).start()

        return worktree_path

    @staticmethod
    def _extract_baked_dependencies_if_available(project_name: str, worktree_path: Path) -> None:
        """Best-effort copy of a project's out-of-tree baked dependencies (issue #50)
        into a freshly-created epic worktree.

        Never raises and never blocks worktree creation: by the time this runs,
        `git worktree add` has already succeeded, so any failure here is logged and
        swallowed rather than surfaced. No-ops (with a clear log line) whenever there's
        nothing to extract yet -- project not verified, no image recorded, or the
        image predates the out-of-tree baked-dependency convention -- all of which are
        expected, self-healing states rather than errors.

        See services/baked_dependency_extractor.py for the actual docker create/cp/rm
        mechanism and services/dev_container_state.py for the verified-image lookup.
        """
        try:
            # Lazy import, mirroring claude/docker_runner.py's own established pattern
            # for this singleton: DevContainerStateManager's constructor touches the
            # filesystem (ORCHESTRATOR_ROOT/state/dev_containers), which doesn't exist
            # outside the orchestrator container (e.g. plain local test runs) -- a
            # module-level import here would make importing project_workspace at all
            # fail in those environments.
            from services.dev_container_state import dev_container_state

            if not dev_container_state.is_verified(project_name):
                logger.debug(
                    f"Skipping baked-dependency extraction for {project_name}: dev "
                    "container not verified, nothing to extract yet."
                )
                return

            image_name = dev_container_state.get_image_name(project_name)
            if not image_name:
                logger.debug(
                    f"Skipping baked-dependency extraction for {project_name}: no "
                    "image name recorded despite verified status."
                )
                return

            # Live re-check, not just the cached is_verified() status above: the same
            # safeguard claude/docker_runner.py's _get_image_for_agent performs before
            # using this same image_name, guarding against a real prior incident where
            # an unrelated Docker Compose service silently overwrote a project's
            # `<project>-agent:latest` tag while cached state still read VERIFIED.
            # Cheap (a single `docker image inspect`, 10s timeout) relative to the
            # create/cp/rm extraction it gates.
            if not dev_container_state.verify_image_exists(project_name):
                logger.debug(
                    f"Skipping baked-dependency extraction for {project_name}: "
                    f"image {image_name!r} failed live verification (missing, or "
                    "tag hijacked by an unrelated image) despite cached VERIFIED "
                    "status."
                )
                return

            from services.baked_dependency_extractor import extract_baked_dependencies
            extract_baked_dependencies(project_name, image_name, worktree_path)
        except Exception as e:
            # Belt-and-braces: extract_baked_dependencies() itself already never
            # raises, but this wrapper -- and everything leading up to it, including
            # the dev_container_state lookup -- must guarantee it too. Debug level:
            # this is expected to fire routinely in environments without a real
            # ORCHESTRATOR_ROOT (e.g. local unit test runs), not just on genuine
            # operational problems (those are already logged clearly at warning level
            # inside extract_baked_dependencies itself).
            logger.debug(
                f"Baked-dependency extraction lookup for {project_name} raised "
                f"unexpectedly ({e}); continuing without it."
            )

    @staticmethod
    def _current_worktree_branch(worktree_path: Path) -> Optional[str]:
        """Best-effort read of the branch actually checked out in an existing worktree.

        Returns None (never raises) on any failure -- callers fall back to whatever
        branch_name they already have on hand.
        """
        try:
            result = subprocess.run(
                ['git', '-C', str(worktree_path), 'rev-parse', '--abbrev-ref', 'HEAD'],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return None
            branch = result.stdout.strip()
            return branch if branch and branch != 'HEAD' else None
        except Exception:
            return None

    @staticmethod
    def _push_stray_branch_if_ahead(base_repo_dir: Path, branch_name: str) -> None:
        """Best-effort push of a local branch ref that might hold real unpushed
        commits, before `worktree add -B` is about to reset it.

        Found in a final whole-PR review pass on #87, pass 2: -B unconditionally
        resets an existing local ref to match the given start-point, discarding
        anything it pointed to first. That's the correct, safe behavior for a
        *stray* ref (e.g. left behind by a partial worktree-add failure -- see
        _add_epic_worktree's own docstring/comments) -- but a local branch ref can
        ALSO be left in base_repo_dir holding real, never-pushed commits: e.g. a
        worktree was force-removed (cleanup_epic_worktree/prune_epic_worktrees)
        after its own push-before-removal attempt (_push_local_commits_if_any)
        failed. `git worktree remove` only removes the checkout, not the
        underlying branch ref, which lives in the shared repo. Reactivating that
        same epic later would hit this exact -B reset and silently discard those
        commits with zero trace. Uses explicit refspecs throughout (referencing
        the branch by name directly) rather than checking it out, since this runs
        against the shared base_repo_dir, not a dedicated worktree -- checking out
        an arbitrary branch there could itself be unsafe/unnecessary.
        """
        try:
            verify = subprocess.run(
                ['git', '-C', str(base_repo_dir), 'rev-parse', '--verify', '--quiet',
                 f'refs/heads/{branch_name}'],
                capture_output=True, text=True, timeout=10
            )
            if verify.returncode != 0:
                return  # no local ref by this name -- nothing to protect

            ahead_result = subprocess.run(
                ['git', '-C', str(base_repo_dir), 'rev-list', '--count',
                 f'origin/{branch_name}..{branch_name}'],
                capture_output=True, text=True, timeout=10
            )
            if ahead_result.returncode == 0:
                ahead_count = int(ahead_result.stdout.strip() or '0')
                if ahead_count == 0:
                    return  # already matches origin -- safe to reset
                ahead_desc = f"{ahead_count} unpushed commit(s)"
            else:
                # origin/<branch_name> doesn't exist at all -- the whole local
                # branch is unpushed; can't compute a count, but there's
                # something real to try to save.
                ahead_count = None
                ahead_desc = "unpushed commits (no origin ref to compare against)"

            logger.warning(
                f"Local branch {branch_name!r} in {base_repo_dir} has {ahead_desc} "
                "and is about to be reset by worktree creation -- attempting to "
                "push it first."
            )
            push_result = subprocess.run(
                ['git', '-C', str(base_repo_dir), 'push', 'origin', f'{branch_name}:{branch_name}'],
                capture_output=True, text=True, timeout=30
            )
            if push_result.returncode == 0:
                logger.info(f"Pushed stray local branch {branch_name!r} to origin before reset")
            else:
                logger.error(
                    f"Could not push stray local branch {branch_name!r} before it's "
                    f"reset by worktree creation ({push_result.stderr.strip()}). "
                    "Its commits are lost."
                )
        except Exception as e:
            logger.warning(
                f"Failed to check/push stray local branch {branch_name!r} before reset: {e}"
            )

    @staticmethod
    def _free_branch_from_base_clone(base_repo_dir: Path, branch_name: str, default_branch: str) -> None:
        """If branch_name is currently checked out in the base clone itself, free
        it (detach the base clone's HEAD) so an epic worktree can claim it --
        best-effort, never raises.

        `git worktree add` unconditionally refuses to check a branch out into a
        NEW worktree if that same branch is already checked out ANYWHERE else --
        including the primary checkout (git counts it as worktree #0). Ordinary
        ('issues'/'hybrid') dispatch no longer independently checks an epic's
        branch out on this base clone (that used to be
        FeatureBranchManager.prepare_feature_branch(); #122/#123 migrated
        'issues'/'hybrid' dispatch to resolve_workspace()/
        get_or_create_epic_worktree() instead, so this specific collision source
        is gone), but _update_repository (this class's startup sync)
        deliberately never resets the base clone back to default_branch on its
        own, and other stray/leftover local state (an orchestrator restart
        mid-checkout, a manual debugging session, an older worktree that was
        never cleaned up) can still leave the base clone sitting on a branch a
        worktree now needs. When it does, EVERY subsequent `worktree add` for
        that same branch is doomed -- not a rare race, but a deterministic,
        permanent failure (confirmed live: one project's repair cycle failed
        this way every hour for 10+ consecutive hours, before that specific
        collision source was fixed). This call is what makes that safe: freeing
        the branch here, once, before the epic's worktree is first created, so
        the worktree (not the base clone) ends up holding it from then on.

        Safe by construction, not by assumption -- but "by construction" here means
        an EXPLICIT `git status --porcelain` guard, not relying on `git checkout`'s
        own refusal behavior: a plain checkout only refuses when switching branches
        would overwrite a file that actually DIFFERS between the two commits. A
        file with uncommitted changes that happens to be IDENTICAL on both branches
        (the common case -- most files in a repo aren't touched by any one epic's
        commits) checks out cleanly and SILENTLY CARRIES THE UNCOMMITTED CHANGES
        OVER onto default_branch (verified empirically -- this is real git
        behavior, not a hypothetical). Repair cycles steal the pipeline lock from a
        non-retained ordinary holder (see steal_lock() in project_monitor.py), so a
        live 'issues'-workspace agent genuinely can be mid-edit in this exact base
        clone when this runs. So: bail out entirely (no checkout attempted at all)
        if the tree is dirty in ANY way, regardless of which files. When it's
        clean, `--detach` is used rather than a plain branch checkout -- it frees
        branch_name just the same (HEAD no longer references it) without leaving
        the base clone itself parked on default_branch as a named checkout, which
        would just reproduce this exact bug for whichever OTHER epic uses
        default_branch's name as a starting point.
        """
        try:
            status = subprocess.run(
                ['git', '-C', str(base_repo_dir), 'status', '--porcelain'],
                capture_output=True, text=True, timeout=10
            )
            if status.returncode != 0:
                logger.warning(
                    f"Could not check working-tree cleanliness for base clone "
                    f"{base_repo_dir} (git status failed: {status.stderr.strip()}) "
                    "-- not attempting to free any branch from it"
                )
                return
            if status.stdout.strip():
                logger.info(
                    f"Base clone {base_repo_dir} has uncommitted changes -- not "
                    f"attempting to free branch {branch_name!r} from it (something "
                    "may be actively using it)"
                )
                return

            current = subprocess.run(
                ['git', '-C', str(base_repo_dir), 'rev-parse', '--abbrev-ref', 'HEAD'],
                capture_output=True, text=True, timeout=10
            )
            if current.returncode != 0:
                logger.warning(
                    f"Could not determine base clone {base_repo_dir}'s current "
                    f"branch (rev-parse failed: {current.stderr.strip()}) -- not "
                    f"attempting to free branch {branch_name!r} from it"
                )
                return
            if current.stdout.strip() != branch_name:
                return

            result = subprocess.run(
                ['git', '-C', str(base_repo_dir), 'checkout', '--detach', default_branch],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                logger.info(
                    f"Freed branch {branch_name!r} from base clone {base_repo_dir} "
                    f"(detached HEAD at {default_branch!r} there instead) so its "
                    "epic worktree can be created"
                )
            else:
                logger.warning(
                    f"Could not free branch {branch_name!r} from base clone "
                    f"{base_repo_dir} (detach to {default_branch!r} failed: "
                    f"{result.stderr.strip()}) -- leaving it as-is"
                )
        except subprocess.SubprocessError as e:
            logger.warning(
                f"Failed to check/free branch {branch_name!r} from base clone "
                f"{base_repo_dir} (subprocess error, e.g. a timeout -- possibly a "
                f"stale git index.lock): {e}"
            )
        except OSError as e:
            logger.warning(
                f"Failed to check/free branch {branch_name!r} from base clone "
                f"{base_repo_dir} (OS error, e.g. git binary or path issue): {e}"
            )
        except Exception as e:
            logger.error(
                f"Unexpected error checking/freeing branch {branch_name!r} from "
                f"base clone {base_repo_dir}: {e}",
                exc_info=True
            )

    @staticmethod
    def _add_epic_worktree(base_repo_dir: Path, worktree_path: Path, branch_name: str, default_branch: str) -> None:
        """Run the actual `git worktree add` for a new epic worktree.

        Tries the existing-branch path first (fetch origin/<branch_name> then a plain
        `worktree add`); falls back to creating a brand-new branch from
        origin/<default_branch> when branch_name doesn't exist on origin yet.

        Frees branch_name from the base clone first if it's checked out there --
        see _free_branch_from_base_clone's own docstring for why this is needed
        and why it's safe.
        """
        ProjectWorkspaceManager._free_branch_from_base_clone(base_repo_dir, branch_name, default_branch)

        fetch_existing = subprocess.run(
            ['git', '-C', str(base_repo_dir), 'fetch', 'origin',
             f'{branch_name}:refs/remotes/origin/{branch_name}', '--quiet'],
            capture_output=True, text=True, timeout=30
        )

        if fetch_existing.returncode == 0:
            ProjectWorkspaceManager._push_stray_branch_if_ahead(base_repo_dir, branch_name)
            result = subprocess.run(
                ['git', '-C', str(base_repo_dir), 'worktree', 'add', '-B', branch_name,
                 str(worktree_path), f'origin/{branch_name}'],
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

        # -B (create-or-RESET), not -b: found in a final whole-PR review pass on #87
        # that -b left a real, reproducible bug -- a partial failure (e.g. a bad
        # target path, or a 30s timeout after git had already created the local
        # branch ref but before `worktree add` finished) can leave a stray local
        # branch ref with nothing on origin. Neither this call's own retry below
        # (which only helps the genuine "another process just pushed it" race, since
        # its re-fetch has nothing to find for a purely-local stray ref) nor any
        # later call ever cleans that ref up, so every subsequent attempt for this
        # epic hits "fatal: a branch named '<branch>' already exists" forever,
        # surviving restarts. -B is safe here specifically because worktree add
        # already refuses outright if branch_name is checked out in ANY other
        # worktree (regardless of -b/-B) -- so -B's reset semantics only ever
        # trigger on a stray/stale local ref exactly like the one this bug leaves
        # behind, self-healing it instead of requiring detection/cleanup logic.
        # _push_stray_branch_if_ahead guards against -B's OTHER edge case (also
        # found in review, pass 2): a stray ref can hold real unpushed commits
        # (e.g. from a worktree that was force-removed after its own push-before-
        # removal attempt failed) -- -B would silently discard those too if this
        # didn't try to save them first.
        ProjectWorkspaceManager._push_stray_branch_if_ahead(base_repo_dir, branch_name)
        result = subprocess.run(
            ['git', '-C', str(base_repo_dir), 'worktree', 'add', '-B', branch_name,
             str(worktree_path), f'origin/{default_branch}'],
            capture_output=True, text=True, timeout=30
        )
        created_new_branch = result.returncode == 0
        if result.returncode != 0:
            # Race with a concurrently-created branch of the same name (e.g. another
            # process just pushed it) — retry once as the existing-branch case.
            retry_fetch = subprocess.run(
                ['git', '-C', str(base_repo_dir), 'fetch', 'origin',
                 f'{branch_name}:refs/remotes/origin/{branch_name}', '--quiet'],
                capture_output=True, text=True, timeout=30
            )
            if retry_fetch.returncode == 0:
                ProjectWorkspaceManager._push_stray_branch_if_ahead(base_repo_dir, branch_name)
                result = subprocess.run(
                    ['git', '-C', str(base_repo_dir), 'worktree', 'add', '-B', branch_name,
                 str(worktree_path), f'origin/{branch_name}'],
                    capture_output=True, text=True, timeout=30
                )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to create worktree branch {branch_name}: {result.stderr.strip()}"
                )

        if created_new_branch:
            # Push the brand-new branch immediately (the same push+tracking-setup
            # pattern FeatureBranchManager.create_branch_from_main used before it was
            # removed as dead code, #124/WI-E): without it, a plain `git pull` inside
            # the worktree fails with "no tracking information", and the
            # push-before-removal safety net in _push_local_commits_if_any has no
            # origin/<branch> ref to compare against.
            push_result = subprocess.run(
                ['git', '-C', str(worktree_path), 'push', '-u', 'origin', branch_name],
                capture_output=True, text=True, timeout=30
            )
            if push_result.returncode != 0:
                logger.warning(
                    f"Created worktree branch {branch_name} but failed to push it to "
                    f"origin ({push_result.stderr.strip()}); it has no upstream tracking "
                    "until something inside the worktree pushes it successfully."
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
                # No origin/<branch> tracking ref at all — most likely this branch was
                # created but its initial push never succeeded. We can't tell how many
                # commits would be lost, but there's at least HEAD; attempt a first push
                # now rather than silently discarding everything on removal.
                logger.warning(
                    f"No origin/{branch} tracking ref found for {worktree_path}; "
                    "attempting an initial push before removal so commits aren't lost."
                )
                push_result = subprocess.run(
                    ['git', '-C', str(worktree_path), 'push', '-u', 'origin', branch],
                    capture_output=True, text=True, timeout=30
                )
                if push_result.returncode == 0:
                    logger.info(f"Pushed {branch} to origin for the first time from {worktree_path}")
                else:
                    logger.error(
                        f"Could not push {branch} to origin from {worktree_path} before "
                        f"removal ({push_result.stderr.strip()}). Any commits in this "
                        "worktree are lost."
                    )
                return
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

    @staticmethod
    def _get_running_container_mount_sources() -> set:
        """Host-side paths currently bind-mounted into any running switchyard-
        managed container (docker inspect's Mounts[].Source, not the container-
        side path) -- used by prune_epic_worktrees() to avoid force-removing a
        worktree a live container still has mounted.

        Best-effort: returns an empty set (callers proceed as if nothing is
        running) on any failure. Must never block or fail startup over a
        liveness-check problem -- a missed liveness check just means prune falls
        back to its pre-existing (already-accepted) behavior for that worktree,
        not a new failure mode.
        """
        try:
            names_result = subprocess.run(
                ['docker', 'ps', '--filter', 'label=org.switchyard.managed=true',
                 '--format', '{{.Names}}'],
                capture_output=True, text=True, timeout=10
            )
            if names_result.returncode != 0:
                return set()
            names = [n for n in names_result.stdout.strip().split('\n') if n]
            if not names:
                return set()

            inspect_result = subprocess.run(
                ['docker', 'inspect', '--format', '{{json .Mounts}}'] + names,
                capture_output=True, text=True, timeout=10
            )
            if inspect_result.returncode != 0:
                return set()

            import json
            sources = set()
            for line in inspect_result.stdout.strip().split('\n'):
                if not line:
                    continue
                try:
                    for mount in json.loads(line):
                        src = mount.get('Source')
                        if src:
                            sources.add(src)
                except Exception:
                    continue
            return sources
        except Exception as e:
            logger.warning(f"Failed to check running-container mount sources: {e}")
            return set()

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

        On a fresh process self._epic_worktrees normally starts empty, so historically
        any worktree found on disk at startup was safe to remove unconditionally --
        this call runs before anything else could populate the cache. That's no longer
        universally true (found in a final whole-PR review pass on #87): main.py calls
        DockerAgentRunner/AgentContainerRecovery's repair-cycle container recovery
        BEFORE this prune sweep, and recovering an already-COMPLETED repair cycle calls
        commit_agent_changes(epic_id=..., branch_name=...) -> get_project_dir() ->
        get_or_create_epic_worktree(), which can ADOPT an on-disk worktree into
        self._epic_worktrees (see #48's restart-adoption fix) before this method ever
        runs. Deleting that just-adopted worktree here would leave the cache pointing
        at a now-missing directory -- every subsequent operation on that epic would
        fail until the next restart, silently defeating #48's own fix. So: skip any
        worktree currently tracked in self._epic_worktrees (actively in use / just
        adopted this process).

        Also skips any worktree currently bind-mounted into a live, running
        switchyard-managed container (e.g. a repair-cycle container that survived
        the restart and is still running -- reconnect_repair_cycle_container()
        resumes monitoring it without ever populating self._epic_worktrees, so the
        tracked-check above alone wouldn't catch this case). Directly relevant to
        #52's pilot rollout, which explicitly wants to soak-test a forced restart
        mid-epic.

        Neither check is a complete guarantee (both are inherently racy against a
        container starting or finishing between the check and the actual removal
        below -- see the per-worktree lock re-check a few lines down for the
        narrower, still-not-fully-closed version of this same class of race), but
        together they cover the two realistic startup scenarios: a just-adopted
        worktree, and a still-running container's worktree neither adopted nor
        finished. A worktree matching NEITHER check is still safe to remove
        unconditionally -- it will be transparently recreated (fetch + worktree
        add, cheap) the next time get_project_dir()/get_or_create_epic_worktree()
        is called for that epic.
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

            # Computed once for the whole sweep, not per-worktree -- a single
            # `docker ps` + batched `docker inspect` covers every running
            # container regardless of how many worktrees are being considered.
            running_mount_sources = self._get_running_container_mount_sources()

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
                    # Re-check right before acting on each worktree, not once
                    # up front (review pass 2 on #87): container recovery can
                    # still be adopting/creating worktrees concurrently on
                    # another thread while this sweep is mid-loop -- a single
                    # snapshot taken before the loop started could already be
                    # stale by the time a later iteration gets here, especially
                    # for a first-time worktree creation via the slower
                    # multi-subprocess _add_epic_worktree path.
                    with self._epic_worktree_lock:
                        currently_tracked = str(worktree_path) in self._epic_worktrees.values()
                    if currently_tracked:
                        logger.debug(
                            f"Skipping prune of {worktree_path} -- currently tracked in "
                            "_epic_worktrees (adopted or created earlier this process)"
                        )
                        continue

                    # Liveness check: is this worktree's HOST path currently
                    # bind-mounted into a running container? worktree_path is
                    # container-side (rooted at self.workspace_root, i.e.
                    # /workspace in-container); running_mount_sources holds HOST
                    # paths (docker inspect's Mounts[].Source), so translate
                    # before comparing -- same /workspace/ -> host_workspace_path
                    # translation established in project_monitor.py's
                    # _launch_repair_cycle_container.
                    if running_mount_sources:
                        worktree_path_str = str(worktree_path)
                        if worktree_path_str.startswith('/workspace/'):
                            try:
                                from claude.docker_runner import DockerAgentRunner
                                host_workspace_path = DockerAgentRunner._detect_host_workspace_path()
                                host_worktree_path = (
                                    f"{host_workspace_path}/"
                                    f"{worktree_path_str[len('/workspace/'):]}"
                                )
                                if host_worktree_path in running_mount_sources:
                                    logger.info(
                                        f"Skipping prune of {worktree_path} -- currently "
                                        "bind-mounted into a live, running container "
                                        "(e.g. a repair-cycle container that survived "
                                        "the restart)"
                                    )
                                    continue
                            except Exception as e:
                                logger.warning(
                                    f"Failed to check container liveness for "
                                    f"{worktree_path}, proceeding with prune: {e}"
                                )

                    self._push_local_commits_if_any(worktree_path)
                    try:
                        subprocess.run(
                            ['git', '-C', str(repo_path), 'worktree', 'remove', '--force', str(worktree_path)],
                            capture_output=True, timeout=15
                        )
                    except Exception:
                        pass
                    if worktree_path.is_dir():
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
        # INTENTIONALLY base-clone-scoped, not migrated to epic-worktree resolution
        # (#48). Deprecated, project-level API with no issue/epic argument at all --
        # there is no task/epic context here to resolve a worktree from, and (per a
        # full-codebase grep) it has no callers left in production code or tests.
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
        # INTENTIONALLY base-clone-scoped, not migrated to epic-worktree resolution
        # (#48). Project-level introspection with no issue/epic argument -- there is
        # no task/epic context here to resolve a worktree from. Unrelated to
        # FeatureBranchManager.get_current_branch() / GitWorkflowManager's
        # same-named method (those take an explicit project_dir and are the ones
        # real callers use); this method has no callers left in production code or
        # tests.
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
