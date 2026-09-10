import asyncio
import functools
import subprocess
import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from config.manager import config_manager

logger = logging.getLogger(__name__)

# Acquire budget for an epic-worktree creation that has no dispatching issue
# number to attribute its project_checkout wait to (see
# ProjectWorkspaceManager._resolve_checkout_lock_timeout). Deliberately well
# under services/pipeline_watchdog.py's 30-minute zombie_threshold_minutes
# default: project_checkout_lock's in-process activity registry -- the thing
# that tells the watchdog a containerless run is parked on a lock rather than
# dead -- is keyed on (project, issue_number), so an unattributed wait publishes
# nothing and gets no exemption. Wait longer than the threshold with no
# attribution and the watchdog reaps the run and redispatches the issue while
# this thread is still waiting to create its worktree.
UNATTRIBUTED_CHECKOUT_LOCK_TIMEOUT_SECONDS = 900.0

# Resource name the per-epic serializer's waits are published under in
# project_checkout_lock's activity registry (code review on #151/WI-6). Distinct
# from that module's own RESOURCE_NAME ('project_checkout') on purpose: the two
# are genuinely different locks, and both registry consumers
# (services/pipeline_watchdog.py's zombie sweep and project_monitor.py's
# no-container reaper, via describe_active_resource_lock_activity) put the name
# straight into their log line, so calling a per-epic wait 'project_checkout'
# would misdescribe which lock a parked run is actually behind. Neither consumer
# matches on the name -- they only ask whether anything is live -- so a second
# resource name needs no change on their side.
EPIC_WORKTREE_RESOURCE_NAME = 'epic_worktree'

# Worker pool for ProjectWorkspaceManager's *_off_loop() entry points.
#
# Code review on #151/WI-6: get_or_create_epic_worktree()'s creation path now
# blocks in project_checkout_lock_sync()'s time.sleep() poll loop for up to
# DEFAULT_TIMEOUT_SECONDS (~3h), and every production caller reaches it through
# asyncio.to_thread -- i.e. on the event loop's DEFAULT ThreadPoolExecutor
# (min(32, cpu_count + 4) threads, 20 in the orchestrator container). That pool
# is also what project_checkout_lock_async uses to acquire AND to RELEASE:
# _acquire_and_start_heartbeat_off_loop() and _join_heartbeat_thread_async()
# both submit to run_in_executor(None, ...), and the join runs before the outer
# finally reaches _release_and_warn(). Saturate the default pool with waits for
# a lock and the coroutine holding that lock cannot get a thread to finish
# releasing it -- a circular wait, not merely slowness, in which every waiter is
# then guaranteed to time out. Giving the waits their own pool breaks the cycle:
# a wait can starve other waits, never the holder it is waiting for.
#
# Bounded rather than unbounded because a thread parked here is parked for
# hours; per-epic serialization (_epic_worktree_key_lock_held) already caps
# concurrent waiters at one per (project, epic), and concurrent dispatches are
# themselves capped by ORCHESTRATOR_WORKERS and the per-board pipeline lock, so
# this is comfortably above the realistic ceiling. Created lazily so importing
# this module never starts threads.
_EPIC_WORKTREE_EXECUTOR_MAX_WORKERS = 16
_epic_worktree_executor: Optional[ThreadPoolExecutor] = None
_epic_worktree_executor_guard = threading.Lock()

# Concurrency cap for initialize_all_projects()' per-project startup init
# (#140 item 3). Each worker runs one project's git clone/fetch under that
# project's own project_checkout lock, so workers never contend with each
# other; the cap exists to keep startup from fanning out unbounded network and
# disk work across every configured project at once. Its pool is built and torn
# down inside that call -- startup init runs exactly once per process, so a
# module-level pool would just hold idle threads forever.
_PROJECT_INIT_MAX_WORKERS = 8


def _get_epic_worktree_executor() -> ThreadPoolExecutor:
    """The dedicated pool described above, built on first use."""
    global _epic_worktree_executor
    with _epic_worktree_executor_guard:
        if _epic_worktree_executor is None:
            _epic_worktree_executor = ThreadPoolExecutor(
                max_workers=_EPIC_WORKTREE_EXECUTOR_MAX_WORKERS,
                thread_name_prefix='epic-worktree',
            )
        return _epic_worktree_executor


class SetupStatus(Enum):
    """
    Outcome of initialize_all_projects() for one project -- three genuinely
    different states that a bare bool collapsed into two.

    Found in review (#148, from #140 item 8): initialize_all_projects() caught
    EVERY failure out of initialize_project() -- a genuine clone failure, a
    missing/invalid repo_url, and (newly reachable since #54 put a
    project_checkout lock around that clone/update) a
    ProjectCheckoutLockTimeoutError from a stale lock left by a crashed prior
    process -- and recorded needs_setup[project] = False. But False is an
    ASSERTION: main.py's startup dispatch-queuing loop reads it as "confirmed,
    this project does not need dev environment setup". "We could not even
    attempt it, so we never found out" is categorically different, and
    silently asserting the confirmed answer for it is the bug.

    UNKNOWN deliberately covers both non-affirmative cases (lock timeout and
    outright initialization failure) rather than splitting them further: no
    caller can act differently on the two -- in both, the project's checkout
    was never inspected -- and the distinction that IS operationally useful
    (which failure happened) is carried in the per-project log line, which
    names the exception type. What downstream needs is only "did we actually
    determine this?", which is exactly the NEEDED/NOT_NEEDED vs UNKNOWN split.

    Why UNKNOWN does not retry in-loop: initialize_project()'s
    checkout_lock_timeout_seconds is deliberately short (120s) precisely
    because a restart is the natural retry for this startup-only call site --
    see its docstring. The dominant cause of contention here is a stale lock
    from a crashed process, whose own recovery windows (7200s Redis TTL,
    14400s YAML staleness) are far longer than any retry this loop could
    justify, so an immediate retry would just double every project's startup
    delay for no expected gain.

    Deliberately NOT given a __bool__: an earlier revision of this type defined
    one that was truthy only for NEEDED, which made `if status:` re-collapse
    UNKNOWN and NOT_NEEDED into the same answer at the only two places the value
    is ever read (resolve_setup_queue() below, which decides main.py's startup
    setup-queuing, and initialize_all_projects()'s own log line). A three-state
    type whose only public behavior is two-state does not
    encapsulate the invariant it was created to express, and it made the unsafe
    reading the ergonomic one -- any future call site would silently reproduce the
    conflation with nothing to catch it. Both call sites now name the member they
    mean (`is SetupStatus.NEEDED`), which is no less readable and cannot drift.
    Mirrors TouchResult (services/pipeline_lock_manager.py) and ResetResult
    (services/pipeline_queue_manager.py), introduced for the same reason.

    What UNKNOWN does today, stated plainly: it queues no dev_environment_setup
    task, which is the same end state the old bare False produced. That is the
    deliberate answer, not an unfinished one -- resolve_setup_queue()'s
    verify_and_update_status() call is an independent, positive check on the
    project's Docker image, and a verifiably missing image upgrades UNKNOWN to
    NEEDED there regardless of whether the checkout could be inspected. So the
    only projects UNKNOWN leaves alone are ones whose image is present. What
    changes is that the state is now named, logged, and available to any caller
    that wants to act on it (e.g. surfacing degraded startup on /health) instead
    of being asserted as a confirmed "no setup needed".
    """

    NEEDED = "needed"          # confirmed: newly cloned, or Dockerfile.agent missing
    NOT_NEEDED = "not_needed"  # confirmed: existing checkout that already has Dockerfile.agent
    UNKNOWN = "unknown"        # never determined: initialization did not complete


def resolve_setup_queue(
    statuses: Dict[str, 'SetupStatus'],
    image_verified: Callable[[str], bool],
) -> List[str]:
    """
    Which projects get a dev_environment_setup task queued at startup, given
    initialize_all_projects()'s per-project SetupStatus and an independent check
    on whether that project's agent Docker image actually exists.

    The two rules, which are the whole reason SetupStatus exists (#148):

      * A verifiably missing Docker image upgrades UNKNOWN to NEEDED. That check
        is a positive observation about the image, entirely independent of whether
        the checkout could be inspected this startup, so it stands on its own.
      * NEEDED is tested by MEMBER, never by truthiness. SetupStatus deliberately
        defines no __bool__ (see its docstring), so enum members are all truthy and
        a `if needs_setup:` here would queue a HIGH-priority setup task for EVERY
        configured project on every startup — each one acquiring dev_container_build
        and rebuilding Dockerfile.agent. That is the failure this function exists
        to hold still and be tested against; inline in main.py's startup loop it
        was reachable by no test at all.

    `image_verified` is passed in rather than imported so the caller keeps
    ownership of the dev-container state side effect (it also updates status), and
    so this stays a pure decision. It is called once per project, in `statuses`
    order.

    Returns:
        The project names to queue, in `statuses` order.
    """
    to_queue: List[str] = []

    for project_name, status in statuses.items():
        if not image_verified(project_name):
            # Image was marked verified but doesn't exist - mark for setup.
            logger.info(f"Project {project_name} needs dev environment setup (Docker image missing)")
            status = SetupStatus.NEEDED

        if status is SetupStatus.NEEDED:
            to_queue.append(project_name)

    return to_queue


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
        # Epic worktrees whose directory is being adopted or created RIGHT NOW,
        # keyed the same way and populated before any git work starts (code
        # review on #151/WI-6). Purely a visibility marker for
        # prune_epic_worktrees(): while _epic_worktree_lock was the whole
        # serializer, prune's tracked-check blocked until a concurrent
        # creation/adoption had finished AND registered itself in
        # _epic_worktrees, so it could never see an in-flight worktree as
        # untracked. Splitting the serializer out to _epic_worktree_key_lock()
        # made that check return immediately instead, opening a window --
        # seconds for an adoption, up to the whole project_checkout budget for a
        # creation -- in which prune would force-remove a directory another
        # thread was actively populating. Restoring the barrier by having prune
        # take the per-key lock is not an option: prune runs on the event-loop
        # thread at startup (main.py), so it would freeze the loop for exactly
        # as long as the creation it is waiting on. A marker prune can READ gets
        # the same skip decision without anyone blocking.
        self._epic_worktrees_pending: Dict[Tuple[str, str], str] = {}
        # MAP guard only: held for dict reads/writes on the three maps above
        # (and on _epic_worktree_key_locks), never across git work or a lock wait.
        # Code review on #151/WI-6: this used to be the whole serializer for
        # get_or_create_epic_worktree()/cleanup_epic_worktree(), which meant one
        # project's `git worktree add` -- and, once that path started waiting on
        # the project_checkout lock, one project's lock WAIT -- blocked every
        # other project's epic resolution, including cache hits that are
        # otherwise a single dict lookup. The per-(project, epic_id) locks below
        # do the actual check-then-create/cleanup serialization instead, so the
        # blast radius of a slow creation is the one epic it belongs to.
        self._epic_worktree_lock = threading.Lock()
        # One serializer per (project_name, epic_id), created on demand under the
        # map guard above. Never evicted: one small Lock per epic this process
        # has touched, and popping an entry a thread is still holding would
        # silently let the next caller create a second serializer for the same
        # key -- exactly the race these exist to prevent.
        self._epic_worktree_key_locks: Dict[Tuple[str, str], threading.Lock] = {}

    def initialize_all_projects(self) -> Dict[str, 'SetupStatus']:
        """
        Initialize workspaces for all configured projects (excludes hidden/test projects)

        Returns:
            Dict mapping project names to a SetupStatus: NEEDED (newly cloned
            or missing Dockerfile.agent), NOT_NEEDED (confirmed neither), or
            UNKNOWN (initialization never completed -- a lock timeout or an
            outright failure -- so the question was never answered). See
            SetupStatus's docstring for why UNKNOWN exists; callers must test
            for a specific member (`is SetupStatus.NEEDED`), never truthiness.
        """
        logger.info("Initializing all project workspaces")

        # Only initialize visible (non-hidden) projects
        projects = config_manager.list_visible_projects()

        # Concurrently, not one after another (#140 item 3). The project_checkout
        # lock is per-PROJECT, so no two of these contend with each other; a
        # sequential loop nonetheless made one project's wait everybody else's
        # wait. That wait is bounded (initialize_project()'s deliberately short
        # 120s default, see its docstring), so the sequential worst case was
        # ~len(projects) x 120s of startup -- roughly half an hour at this
        # deployment's project count, for contention that is per-project and
        # almost always a stale lock left by a crashed prior process.
        #
        # Bounded pool, created and shut down inside this call: this runs once
        # per process, so a module-level pool would hold idle threads for the
        # life of the orchestrator. Each worker does a git clone/fetch against a
        # different repository, so the cap is about not fanning out unbounded
        # network/disk work, not about correctness.
        max_workers = min(len(projects), _PROJECT_INIT_MAX_WORKERS) or 1
        with ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix='project-init'
        ) as executor:
            results = executor.map(self._initialize_one_project, projects)
            needs_setup = dict(zip(projects, results))

        unknown = sorted(p for p, status in needs_setup.items() if status is SetupStatus.UNKNOWN)
        if unknown:
            logger.warning(
                f"Project workspace initialization did not complete for {len(unknown)} "
                f"project(s): {', '.join(unknown)}. Their dev environment setup need was "
                f"never determined; no setup task will be queued for them on the strength "
                f"of this startup (one is still queued if the Docker image is verifiably "
                f"missing). Restart once the underlying cause is cleared."
            )

        return needs_setup

    def _initialize_one_project(self, project_name: str) -> 'SetupStatus':
        """
        One project's share of initialize_all_projects(), so that loop can run
        its projects concurrently (#140 item 3).

        Split out rather than left inline as a closure: this is the unit whose
        failure must stay per-project. It never raises -- every outcome, success
        or failure, comes back as a SetupStatus, so one project's broken config
        or held lock cannot take down the others' initialization or the startup
        sequence itself.
        """
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
            status = (
                SetupStatus.NEEDED
                if (was_cloned or not dockerfile_agent.exists())
                else SetupStatus.NOT_NEEDED
            )

            if status is SetupStatus.NEEDED:
                logger.info(f"Project {project_name} needs dev environment setup (newly_cloned={was_cloned}, has_dockerfile={dockerfile_agent.exists()})")

            return status

        except Exception as e:
            # UNKNOWN, never False (#148): this project's checkout was never
            # inspected, so we cannot assert that it does not need setup.
            # A lock timeout is called out separately because it is not a
            # fault of this project's configuration or repository at all --
            # another holder owned the project_checkout lock for the whole
            # of initialize_project()'s (deliberately short) wait, most
            # likely a stale lock left by a crashed prior process.
            from services.resource_lock_errors import is_lock_timeout_error
            if is_lock_timeout_error(e):
                logger.error(
                    f"Could not initialize project {project_name}: the project_checkout "
                    f"lock was held for the whole wait, so the checkout was never "
                    f"inspected — dev environment setup need is UNKNOWN for this "
                    f"startup: {e}"
                )
            else:
                logger.error(
                    f"Failed to initialize project {project_name} — dev environment "
                    f"setup need is UNKNOWN: {type(e).__name__}: {e}"
                )
            return SetupStatus.UNKNOWN

    def initialize_project(
        self, project_name: str, project_config, checkout_lock_timeout_seconds: float = 120.0
    ) -> bool:
        """
        Initialize a project workspace by checking if it exists

        Args:
            project_name: Name of the project
            project_config: Project configuration object
            checkout_lock_timeout_seconds: How long to wait for the
                project_checkout lock below before giving up (found in PR
                #138 review, /pr-review-toolkit:review-pr). Deliberately
                short by default: this method's only caller today
                (initialize_all_projects()) runs once per project at
                orchestrator STARTUP, before the dispatch loop begins, and
                already catches and logs a per-project failure rather than
                aborting the whole startup sequence. Waiting out
                project_checkout_lock's own default (~3h,
                DEFAULT_TIMEOUT_SECONDS) here would mean a single stale lock
                left by a crashed prior process -- which startup's own later
                stale-lock recovery step doesn't cover for resource locks
                like this one -- stalls the ENTIRE startup sequence (every
                other, unrelated project) for hours, with no automated way
                out. A restart is itself the natural retry for this specific
                call site, so failing fast and letting the per-project
                except handle it is strictly better than a multi-hour wait.
                A future on-demand (non-startup) caller of this method can
                pass a longer value if a real wait is actually wanted there.

        Returns:
            True if project was newly cloned, False if it already existed
        """
        # Cheap config validation BEFORE acquiring the lock below (#56 review,
        # mirroring the same fix applied to auto_commit.py's branch check):
        # this outcome can't change based on lock state, so checking it first
        # means a misconfigured project fails instantly instead of first
        # polling for up to project_checkout_lock's own DEFAULT_TIMEOUT_SECONDS
        # if the lock happened to be contended at startup.
        repo_url = project_config.github.get('repo_url')
        default_branch = project_config.github.get('branch', 'main')

        if not repo_url:
            raise ValueError(f"No repo_url configured for project {project_name}")

        # Serialize against every other operation on this project's shared base
        # clone (#54): today this runs once per project at startup, before the
        # dispatch loop begins, so it is safe only by ordering accident -- a
        # future on-demand re-initialization call (or a slow startup racing an
        # operator-triggered early dispatch) would otherwise be able to clone/
        # fetch/checkout into the exact directory another operation is already
        # reading or building from. No real GitHub issue is in scope at
        # project-initialization time -- pass None (log attribution only, not
        # the lock's holder identity; see project_checkout_lock.py's module
        # docstring).
        from services.project_checkout_lock import project_checkout_lock_sync

        with project_checkout_lock_sync(
            project_name, None, timeout_seconds=checkout_lock_timeout_seconds
        ):
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
        issue_number: Optional[int] = None,
        checkout_lock_timeout_seconds: Optional[float] = None,
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
            issue_number: The DISPATCHING issue whose pipeline run is waiting on this
                resolution. Forwarded, with checkout_lock_timeout_seconds, to
                get_or_create_epic_worktree() -- see its docstring for why the
                creation path's project_checkout wait needs it (the watchdog
                exemption is keyed on it, and the acquire budget follows from
                that). Code review on #151/WI-6: neither was forwarded here at
                first, so no caller reaching the creation path through
                get_project_dir() could supply either.
            checkout_lock_timeout_seconds: See get_or_create_epic_worktree().

        Returns:
            The base clone path (epic_id=None), or the epic's isolated worktree path.
        """
        if epic_id is None:
            return self.workspace_root / project_name

        return self.get_or_create_epic_worktree(
            project_name,
            epic_id,
            branch_name,
            default_branch=default_branch,
            issue_number=issue_number,
            checkout_lock_timeout_seconds=checkout_lock_timeout_seconds,
        )

    async def _off_loop(self, func: Callable[..., Any], *args, **kwargs) -> Any:
        """Run one blocking worktree resolution on the dedicated pool at the top
        of this module instead of asyncio's default executor.

        Drop-in replacement for asyncio.to_thread() at the async call sites that
        can reach get_or_create_epic_worktree()'s creation path.
        asyncio.to_thread() is hardcoded to the loop's DEFAULT executor, which is
        the one pool these waits must not occupy -- see
        _get_epic_worktree_executor() for the circular wait that produces.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _get_epic_worktree_executor(), functools.partial(func, *args, **kwargs)
        )

    async def get_project_dir_off_loop(self, *args, **kwargs) -> Path:
        """get_project_dir(), run off the event loop on the dedicated
        epic-worktree pool. Async callers that might pass an epic_id MUST use this
        rather than asyncio.to_thread(); see _off_loop().

        A transparent pass-through rather than a restated signature: this adds a
        thread hop and nothing else, and duplicating get_project_dir()'s
        parameters here would only create somewhere for them to drift apart.
        """
        return await self._off_loop(self.get_project_dir, *args, **kwargs)

    async def get_or_create_epic_worktree_off_loop(self, *args, **kwargs) -> Path:
        """get_or_create_epic_worktree(), run off the event loop on the dedicated
        epic-worktree pool -- see get_project_dir_off_loop()."""
        return await self._off_loop(self.get_or_create_epic_worktree, *args, **kwargs)

    def is_base_clone_dir(self, project_name: str, project_dir) -> bool:
        """
        True if `project_dir` IS project_name's shared base clone (the
        get_project_dir(project_name, epic_id=None) path) rather than an
        isolated epic worktree or some other directory.

        Used (#54) to scope the project_checkout resource lock
        (services/project_checkout_lock.py) to genuinely shared-directory
        operations only. Locking epic-worktree-scoped operations too would
        serialize sibling epics of the same project against each other even
        though they never touch the same physical directory -- exactly the
        cross-epic throughput cost the epic-worktree isolation work (#119)
        exists to avoid, which would violate #54's own "no behavior change
        for the common case" requirement.

        Compares resolved (symlink-following, absolute) paths rather than the
        raw strings a caller might pass in a different but equivalent form
        (relative, trailing slash, unresolved symlink, ...). Fails closed --
        if path resolution raises for any reason, OR project_dir doesn't
        exist on disk at all (Path.resolve() succeeds without error even for a
        nonexistent path, so an existence check is needed too), returns True
        (assume it IS the shared base clone) rather than silently skipping the
        lock this method exists to gate.

        What fails-closed deliberately does NOT cover is a caller's own
        placeholder for a MISSING directory (#151/WI-6 item 12): the case
        originally cited here was claude_integration.py's
        Path(context.get('work_dir', '.')), and '.' resolves to the
        orchestrator's own cwd, which always exists -- so the existence check
        never fired for it and the real comparison returned False, skipping the
        lock. That is not fixable here (this method cannot tell a deliberate '.'
        from a defaulted one); it is fixed at the call site, which now refuses a
        missing work_dir outright. See claude_integration._require_work_dir().
        """
        try:
            resolved_dir = Path(project_dir).resolve()
            if not resolved_dir.exists():
                logger.warning(
                    f"is_base_clone_dir() called with a directory that doesn't exist "
                    f"for project {project_name!r}: {project_dir!r} (resolved to "
                    f"{resolved_dir}) -- treating as the shared base clone (fail closed) "
                    "rather than silently assuming it isn't"
                )
                return True
            return resolved_dir == self.get_project_dir(project_name).resolve()
        except Exception as e:
            logger.warning(
                f"is_base_clone_dir() could not resolve paths for project "
                f"{project_name!r}, dir={project_dir!r}: {e} -- treating as the "
                "shared base clone (fail closed)"
            )
            return True

    def _epic_worktree_key_lock(self, key: Tuple[str, str]) -> threading.Lock:
        """The per-(project_name, epic_id) serializer for check-then-create and
        cleanup on that one epic's worktree.

        Guards exactly what _epic_worktree_lock used to guard globally, minus the
        cross-project/cross-epic blocking: two concurrent calls for the SAME epic
        still cannot both attempt a create (or race a create against a cleanup),
        while an unrelated epic's cache hit stays a dict lookup even while this
        one is mid-`git worktree add` or mid-project_checkout-wait.

        Lock ordering, everywhere these two are used together: per-key lock
        OUTER, _epic_worktree_lock (the map guard) INNER, never the reverse.
        """
        with self._epic_worktree_lock:
            key_lock = self._epic_worktree_key_locks.get(key)
            if key_lock is None:
                key_lock = threading.Lock()
                self._epic_worktree_key_locks[key] = key_lock
            return key_lock

    @contextmanager
    def _epic_worktree_key_lock_held(
        self,
        key: Tuple[str, str],
        project_name: str,
        epic_id: str,
        issue_number: Optional[int],
        timeout_seconds: float,
    ):
        """Hold this epic's serializer for the body, with a BUDGET on the wait and
        that wait published to project_checkout_lock's activity registry.

        Code review on #151/WI-6: the per-key lock was taken as a bare, untimed
        `with self._epic_worktree_key_lock(key):`. Once the winner started waiting
        on project_checkout INSIDE it, a second dispatch for the SAME epic could
        sit on that plain threading.Lock for the winner's entire budget (~3h) with
        nothing published under its own (project, issue_number) and no acquire
        budget of its own -- one level above the registration that makes the
        winner's own wait legitimate. services/pipeline_watchdog.py then sees a
        run marked active, past zombie_threshold_minutes, with no container: it
        reaps the run and redispatches the same issue while this thread is still
        queued, which is precisely the double execution
        project_checkout_lock's registry was added to prevent (see its
        _resource_activity comment).

        Both halves are closed here. The wait is published, so the waiting run is
        exempt from reaping exactly as the winner's own project_checkout wait is;
        and it is bounded by the same budget that wait gets, failing with
        ProjectCheckoutLockTimeoutError -- the type
        services/resource_lock_errors.is_lock_timeout_error() recognizes -- so a
        loser routes through the existing 'lock_contention' path rather than
        counting as a dispatch failure toward MAX_CONSECUTIVE_DISPATCH_FAILURES.
        """
        from services.project_checkout_lock import (
            ProjectCheckoutLockTimeoutError,
            tracked_resource_activity,
        )

        key_lock = self._epic_worktree_key_lock(key)
        with tracked_resource_activity(
            EPIC_WORKTREE_RESOURCE_NAME,
            project_name,
            issue_number,
            wait_budget_seconds=timeout_seconds,
        ) as activity:
            # A zero budget is _resolve_checkout_lock_timeout()'s "one attempt, no
            # sleeping" clamp (an on-event-loop caller, or a caller that asked for
            # it explicitly because it is blocking a loop of its own).
            # threading.Lock.acquire() spells that as blocking=False, not timeout=0.
            if timeout_seconds > 0:
                acquired = key_lock.acquire(timeout=timeout_seconds)
            else:
                acquired = key_lock.acquire(blocking=False)
            if not acquired:
                raise ProjectCheckoutLockTimeoutError(
                    f"Could not acquire the per-epic worktree serializer for "
                    f"{project_name} epic #{epic_id} within {timeout_seconds}s -- "
                    "another call for this same epic is still resolving its worktree "
                    "(most likely parked on this project's project_checkout lock). "
                    "Nothing was fetched, checked out or registered."
                )
            if activity is not None:
                activity.mark_held()
            try:
                yield
            finally:
                key_lock.release()

    def _resolve_epic_lock_issue_number(
        self, epic_id: str, issue_number: Optional[int]
    ) -> Optional[int]:
        """Attribution for both of get_or_create_epic_worktree()'s waits: the
        dispatching issue when the caller supplied one, else the epic id when it
        is a plain number.

        Falling back to the epic id is exactly right for planning_design (the
        board item IS the epic) and is the only attribution this method had
        before the issue_number parameter existed. Never the lock's holder
        identity -- see project_checkout_lock.py's module docstring.
        """
        if issue_number is not None:
            return issue_number
        try:
            return int(str(epic_id).strip())
        except (TypeError, ValueError):
            return None

    def _resolve_checkout_lock_timeout(
        self,
        project_name: str,
        epic_id: str,
        issue_number: Optional[int],
        requested_seconds: Optional[float],
    ) -> float:
        """How long get_or_create_epic_worktree() may wait for a lock -- both this
        epic's own serializer (_epic_worktree_key_lock_held) and, on the creation
        path, this project's project_checkout lock.

        One budget for both because they are one queue: a caller blocked on the
        per-key lock is blocked on the winner's project_checkout wait, so giving
        the outer wait a different allowance would either cut a legitimate one
        short or let it outlive the thing it is waiting for.

        Three cases, in priority order (code review on #151/WI-6):

        1. A running event loop on THIS thread -> 0.0, i.e. one attempt and no
           time.sleep() at all. project_checkout_lock_sync()'s poll loop on the
           loop thread does not merely stall other coroutines for its duration:
           every in-process holder of this same lock (claude_integration's
           `async with project_checkout_lock_async` around a container run,
           auto_commit, finalize_feature_branch_work) can only reach its release
           by being rescheduled on that loop, so a poll there STARVES the holder
           it is waiting for and the wait is guaranteed to fail. Every production
           caller now hops off the loop (asyncio.to_thread) before reaching here;
           this clamp is the backstop that turns a future on-loop caller into a
           loud, immediate failure instead of a frozen orchestrator, and it
           overrides an explicitly requested budget for the same reason.
        2. An explicit requested_seconds (off-loop) -> honoured as given.
        3. Otherwise the calibrated wait, which depends on whether this wait is
           attributable: project_checkout_lock publishes waits to its
           in-process activity registry keyed on (project, issue_number), and
           services/pipeline_watchdog.py reads that registry to know a
           containerless run is legitimately parked rather than a zombie. WITH a
           dispatching issue number the wait is exempt, so it can use the same
           ~3h DEFAULT_TIMEOUT_SECONDS every other project_checkout acquisition
           uses -- calibrated (see services/resource_lock_errors.py) to outlast
           the longest legitimate holder, which for this lock is a base-clone-
           scoped agent container run of up to agents.yaml's 10800s. Anything
           shorter turns a perfectly legitimate holder into a
           ProjectCheckoutLockTimeoutError, and three of those on consecutive
           board polls escalate to a human via project_monitor's
           MAX_CONSECUTIVE_LOCK_CONTENTIONS. WITHOUT one, nothing vouches for
           the waiting run, so the budget has to stay well under the watchdog's
           30-minute zombie threshold instead.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            logger.error(
                f"get_or_create_epic_worktree({project_name!r}, epic #{epic_id}) reached "
                "its creation path on the event-loop thread. Its project_checkout wait "
                "is a time.sleep() poll loop, and the holders it would wait behind "
                "release from coroutines on this same loop -- so waiting here freezes "
                "the orchestrator and starves the holder. Failing immediately instead. "
                "This is a bug in the caller: resolve the worktree off the loop "
                "(asyncio.to_thread), the way PipelineRunManager.resolve_workspace() does."
            )
            return 0.0

        if requested_seconds is not None:
            return requested_seconds

        if issue_number is None:
            return UNATTRIBUTED_CHECKOUT_LOCK_TIMEOUT_SECONDS

        from services.project_checkout_lock import DEFAULT_TIMEOUT_SECONDS
        return DEFAULT_TIMEOUT_SECONDS

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
        issue_number: Optional[int] = None,
        checkout_lock_timeout_seconds: Optional[float] = None,
    ) -> Path:
        """
        Get (creating if absent) an isolated, non-detached git worktree for one epic.

        MUST NOT be called from the event-loop thread when it might have to CREATE
        (see checkout_lock_timeout_seconds below and _resolve_checkout_lock_timeout()
        for what happens if it is). Every production caller hops off the loop via
        asyncio.to_thread first.

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
            issue_number: The DISPATCHING issue whose pipeline run is waiting on this
                resolution -- the sub-issue for sdlc_execution, the board item itself
                for planning_design. Used for the creation path's project_checkout
                acquisition: it is log attribution for the lock itself, but it is
                also the key project_checkout_lock's activity registry publishes the
                WAIT under, which is what exempts the waiting run from
                services/pipeline_watchdog.py's zombie reaping. Supply it whenever
                one is in scope; omitting it caps the wait at
                UNATTRIBUTED_CHECKOUT_LOCK_TIMEOUT_SECONDS, because nothing would
                vouch for a longer one. Defaults to the epic id when it is a plain
                number (planning_design's board item, and the only attribution this
                method had before the parameter existed).
            checkout_lock_timeout_seconds: Overrides how long this call may wait for
                a lock before giving up (#151/WI-6 item 1) -- both this epic's own
                serializer (every path takes that, cache hits included) and, on the
                brand-new-worktree path, this project's project_checkout lock. None
                (the default) means "decide from the calling thread and the
                attribution above" -- see _resolve_checkout_lock_timeout(), which is
                also where an on-event-loop caller gets clamped to a single
                non-blocking attempt regardless of what is passed here. Pass 0.0
                explicitly from a caller that is blocking an event loop the clamp's
                own probe cannot see (project_monitor._start_repair_cycle_for_issue's
                asyncio.run-on-a-pool-thread branch is the one such call site).

        Returns:
            The epic's worktree path.

        Raises:
            ValueError: No worktree exists yet for this epic and branch_name was not
                given, or the project has no base clone to source the worktree from.
            ProjectCheckoutLockTimeoutError: Either a brand-new worktree had to be
                created but this project's shared base clone stayed held for the
                whole of the acquire budget (_resolve_checkout_lock_timeout()), or
                another call for this SAME epic held its per-key serializer for
                that whole budget (see _epic_worktree_key_lock_held(), which is
                where the second case is raised) -- in both, nothing was fetched,
                checked out or registered. Propagates untouched (#148/WI-3
                keeps lock-timeout types out of the generic retry loop and the
                circuit breaker) so the dispatch fails loudly and its next trigger
                retries. Callers MUST route it through
                services/resource_lock_errors.is_lock_timeout_error() and record
                'lock_contention' rather than 'failure', or three contended polls
                reach MAX_CONSECUTIVE_DISPATCH_FAILURES and mark_failed() retains
                the board's lock over contention that clears itself.
            RuntimeError: Either the underlying `git worktree add` command failed
                (the pre-existing cause), OR (issue found investigating a
                code-wrapper dev-container block, 2026-09-06) the worktree
                directory exists on disk, non-empty, but has no .git at all --
                corrupted, checked both on a fresh resolution and on a cache hit
                for an epic already tracked in this process (e.g. a container's
                own git-state self-repair can remove a worktree's .git with no
                orchestrator restart in between).

                This deliberately does NOT attempt automatic cleanup (an earlier
                version of this fix did, via shutil.rmtree, caught by code review
                before merging):
                1. It wouldn't even work for the realistic trigger. git tracks a
                   worktree by metadata under the base repo's OWN
                   .git/worktrees/<id>/, not by whether the target directory
                   exists -- a container's self-repair removes only the
                   worktree's own .git pointer, never that base-repo-side
                   registration, so a subsequent `git worktree add` for the same
                   path still refuses ("is a missing but already registered
                   worktree") even after the directory itself is gone. Only `git
                   worktree remove --force` (or `prune`), run against the base
                   clone, actually clears it (verified empirically).
                2. Even done via that correct command instead of a raw rmtree,
                   it's still fundamentally unsafe to do automatically: without
                   .git there is no way to tell "just the unmodified checkout,
                   safe to discard" apart from "an agent's real, uncommitted
                   work sitting in a directory that happens to be missing
                   .git" -- both destroy either one identically. Two callers
                   reach this exact directory expecting to commit real content
                   from it afterward (agent_container_recovery.py's
                   restart-recovery flow, agent_executor.py's
                   _failsafe_commit_check()); silently destroying their target
                   first would be permanent, undiagnosed data loss for a
                   just-completed fix, not a recoverable retry.

                A genuinely EMPTY pre-existing directory is NOT treated as this
                corrupted case -- it has nothing to lose, and `git worktree add`
                succeeds into it exactly as it always has, so that case falls
                through to the normal creation path below instead.

                Note this raise does not universally guarantee an existing
                retry/escalation mechanism sees it -- that depends entirely on
                what this method's various callers each do with it (some
                propagate to a durable, GitHub-visible retry/escalation path;
                at least one known caller, agent_executor.py's best-effort
                _failsafe_commit_check(), logs and continues by its own
                pre-existing design). What this raise DOES guarantee
                unconditionally is that nothing on disk is touched or destroyed.
        """
        key = (project_name, str(epic_id))
        newly_created = False
        # Resolved BEFORE the per-key acquire below, not just for the
        # project_checkout wait further down: both waits are the same queue and
        # share one budget and one attribution. See
        # _resolve_checkout_lock_timeout() and _epic_worktree_key_lock_held().
        lock_issue_number = self._resolve_epic_lock_issue_number(epic_id, issue_number)
        lock_timeout_seconds = self._resolve_checkout_lock_timeout(
            project_name, epic_id, lock_issue_number, checkout_lock_timeout_seconds
        )
        # Per-epic, not process-global (code review on #151/WI-6): everything
        # below -- the git subprocess work AND, on the creation path, a wait for
        # this project's project_checkout lock -- used to run under one
        # threading.Lock shared by every project and every epic, so a single slow
        # creation blocked unrelated projects' cache hits too. See
        # _epic_worktree_key_lock() for the ordering rule against the map guard,
        # and _epic_worktree_key_lock_held() for why this acquire is budgeted and
        # published to the watchdog's activity registry rather than a bare
        # `with lock:`.
        with self._epic_worktree_key_lock_held(
            key, project_name, epic_id, lock_issue_number, lock_timeout_seconds
        ):
            with self._epic_worktree_lock:
                existing = self._epic_worktrees.get(key)
            if existing is not None:
                # Code review finding on this method's own corruption check
                # further down: that check only runs on the NOT-yet-cached
                # path, so it was dead code for any epic already tracked in
                # this process -- e.g. a running container's own self-repair
                # removes .git from an ALREADY-cached worktree with no
                # orchestrator restart in between (the exact trigger this
                # whole safety check exists for). Re-check here too, on every
                # cache hit: cheap (a single stat, not a git subprocess call)
                # and closes that gap without duplicating the corruption
                # branch itself -- just falls through to it below instead of
                # returning early.
                if (Path(existing) / '.git').exists():
                    with self._epic_worktree_lock:
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
                # Raised directly here rather than falling through to the
                # not-yet-cached code path below: that path assumes it might
                # be a genuine first-time creation and requires branch_name
                # to be given for that case -- a cache-hit reuse call
                # legitimately omits it, relying on the cache, which would
                # make the fall-through raise the WRONG (branch_name-missing)
                # error here instead of this one.
                raise RuntimeError(
                    f"Cached epic worktree for {project_name} epic #{epic_id} at {existing} "
                    "has lost its .git since it was last used this process -- corrupted "
                    "(not a recognized worktree, and not safely auto-recoverable: see this "
                    "method's docstring for why). Needs manual inspection (the directory may "
                    f"hold real uncommitted work) followed by `git worktree remove --force "
                    f"{existing}` (or `git worktree prune`) run against the base clone before "
                    "this epic can be retried."
                )

            if not branch_name:
                raise ValueError(
                    f"No worktree exists yet for {project_name} epic #{epic_id}; "
                    "branch_name is required to create one"
                )

            base_repo_dir = self.workspace_root / project_name
            if not (base_repo_dir / '.git').exists():
                raise ValueError(f"Base clone for project {project_name} not found at {base_repo_dir}")

            worktree_path = self._epic_worktree_path(project_name, epic_id)

            # In-flight marker for prune_epic_worktrees() (code review on
            # #151/WI-6). Everything below -- _current_worktree_branch()'s
            # subprocess on the adopt path, and on the create path the mkdir, the
            # whole project_checkout wait and _add_epic_worktree()'s fetch +
            # `worktree add` -- runs with the map guard RELEASED, so until the
            # registration at the end of it this worktree is invisible to prune's
            # tracked-check, which would then force-remove the directory this
            # thread is in the middle of populating (see _epic_worktrees_pending
            # in __init__ for the interleaving and why prune cannot simply take
            # this epic's per-key lock instead). Published under the map guard so
            # prune sees it atomically, and dropped in a finally -- after the
            # real registration on the success paths, so there is no instant in
            # which neither map names this worktree.
            with self._epic_worktree_lock:
                self._epic_worktrees_pending[key] = str(worktree_path)
            try:
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
                    with self._epic_worktree_lock:
                        self._epic_worktrees[key] = str(worktree_path)
                        self._epic_worktree_branches[key] = actual_branch
                    logger.info(
                        f"Adopted pre-existing epic worktree for {project_name} epic "
                        f"#{epic_id} at {worktree_path} (branch={actual_branch})"
                    )
                    return worktree_path

                if self._is_corrupted_non_empty_worktree(worktree_path):
                    # On disk, non-empty, but .git is completely missing --
                    # corrupted, not a recognized worktree to adopt above. See
                    # this method's own docstring (Raises section) for the full
                    # rationale on why this deliberately does not attempt any
                    # automatic cleanup, and _is_corrupted_non_empty_worktree()'s
                    # own docstring for why non-empty is the specific condition
                    # that matters (a genuinely empty pre-existing directory has
                    # nothing to lose -- `git worktree add` succeeds into it
                    # exactly as it always has, self-healing via the normal path
                    # below instead of being needlessly escalated).
                    raise RuntimeError(
                        f"Epic worktree directory for {project_name} epic #{epic_id} "
                        f"exists at {worktree_path} but has no .git at all -- corrupted "
                        "(not a recognized worktree, and not safely auto-recoverable: "
                        "see this method's docstring for why). Needs manual inspection "
                        "(the directory may hold real uncommitted work) followed by "
                        f"`git worktree remove --force {worktree_path}` (or `git "
                        "worktree prune`) run against the base clone before this epic "
                        "can be retried."
                    )

                worktree_path.parent.mkdir(parents=True, exist_ok=True)

                # project_checkout lock (#151/WI-6 item 1). _add_epic_worktree() is a
                # base-clone WRITER: it fetches into base_repo_dir's refs, detaches its
                # HEAD (_free_branch_from_base_clone), pushes from it
                # (_push_stray_branch_if_ahead) and registers the new worktree in its
                # own .git/worktrees/. Nothing gated it, because every other call site
                # decides whether to lock from its FINAL resolved directory
                # (is_base_clone_dir()) -- which here is the new worktree path, by
                # definition never the base clone -- so this was the last unlocked
                # base-clone writer on the CREATION side, free to race the startup
                # clone/update, a base-clone-scoped container run and auto_commit's
                # add/commit/push against that same .git. Epic-worktree TEARDOWN is
                # still an unlocked writer of this same .git/worktrees/ --
                # cleanup_epic_worktree() and prune_epic_worktrees() both run `git -C
                # <base clone> worktree remove --force` with no project_checkout lock
                # -- tracked separately in #169; do not read this block as saying
                # base-clone coverage is now complete.
                #
                # Sync, not async: this is a plain sync method with sync callers. #146
                # WI-1 made that safe for the HOLD -- the heartbeat runs on a real OS
                # thread, so a guarded body that never yields to the event loop still
                # gets its lock refreshed, with no change to this method's control
                # flow. It does NOT make the WAIT safe, and the failure mode is worse
                # than a stall: project_checkout_lock_sync()'s poll loop is
                # time.sleep(), and every in-process holder of this lock releases from
                # a coroutine on the event loop -- so a poll on the loop thread starves
                # the holder it is waiting for and can never succeed. Every production
                # caller therefore reaches this method off the loop (asyncio.to_thread),
                # and _resolve_checkout_lock_timeout() clamps a stray on-loop caller to
                # a single non-blocking attempt rather than freezing the process.
                #
                # Off the loop, the budget is project_checkout's ordinary calibrated
                # ~3h (see _resolve_checkout_lock_timeout() for the two things that
                # change it): waiting is what this lock is FOR, the legitimate holder
                # here can be an agent container run of up to agents.yaml's 10800s, and
                # a budget below that would convert normal contention into a
                # ProjectCheckoutLockTimeoutError that project_monitor escalates to a
                # human after three consecutive polls. A timeout raises rather than
                # proceeding unlocked, so nothing is fetched, checked out or registered
                # when it does.
                #
                # Lock ordering: this epic's per-key lock is held here and the
                # project_checkout lock is taken INSIDE it. Nothing acquires them the
                # other way round -- no project_checkout holder calls back into this
                # method -- so the nesting cannot deadlock. The process-global map
                # guard (_epic_worktree_lock) is deliberately NOT held across this
                # wait; see _epic_worktree_key_lock().
                from services.project_checkout_lock import project_checkout_lock_sync

                # lock_issue_number is attribution for the lock's logs AND the key
                # the wait's entry in project_checkout_lock's activity registry is
                # published under, which is what exempts the waiting pipeline run
                # from the watchdog (see the issue_number arg docs). Never the
                # lock's holder identity -- see project_checkout_lock.py's module
                # docstring. Both it and the budget were resolved at the top of
                # this method, because the per-key acquire that got us here needed
                # exactly the same two values.
                with project_checkout_lock_sync(
                    project_name,
                    lock_issue_number,
                    timeout_seconds=lock_timeout_seconds,
                ):
                    self._add_epic_worktree(base_repo_dir, worktree_path, branch_name, default_branch)

                with self._epic_worktree_lock:
                    self._epic_worktrees[key] = str(worktree_path)
                    self._epic_worktree_branches[key] = branch_name
                logger.info(
                    f"Created epic worktree for {project_name} epic #{epic_id} "
                    f"at {worktree_path} (branch={branch_name})"
                )
                newly_created = True
            finally:
                with self._epic_worktree_lock:
                    self._epic_worktrees_pending.pop(key, None)

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
    def _is_corrupted_non_empty_worktree(worktree_path: Path) -> bool:
        """True if worktree_path exists, has real content, but no .git at all --
        the specific shape get_or_create_epic_worktree() and
        prune_epic_worktrees() both refuse to auto-clean-up (see
        get_or_create_epic_worktree()'s own docstring for the full rationale:
        it's not safely distinguishable from real, precious uncommitted work).
        Third-pass code review: this exact condition used to be hand-written
        independently at both call sites (plus a simpler .git-only variant at
        a third, the in-process cache-hit check) -- one shared, tested
        implementation instead.

        Race-tolerant (also third-pass code review): `any(iterdir())` -- unlike
        `Path.exists()` -- does not swallow OSError, so a directory removed or
        replaced by something else between the .exists() checks and this call
        (both call sites already document their own broader raciness against
        concurrent worktree creation/adoption/removal on another thread) would
        otherwise raise FileNotFoundError/NotADirectoryError instead of this
        method's clean bool contract. In get_or_create_epic_worktree() that
        surprises callers expecting only the documented ValueError/RuntimeError;
        in prune_epic_worktrees() it's worse -- uncaught there, it reaches that
        method's single top-level except and aborts pruning EVERY OTHER
        project's/epic's worktree for that startup, not just the one that
        raced. Treated as "not this corrupted shape" (False) instead: the
        caller's own subsequent real operation (`git worktree add`/`remove`)
        already raises its own clear, well-handled failure for whatever the
        directory turns out to actually be by the time that runs.
        """
        try:
            return (
                worktree_path.exists()
                and not (worktree_path / '.git').exists()
                and any(worktree_path.iterdir())
            )
        except OSError as e:
            logger.warning(
                f"Could not determine whether {worktree_path} is a corrupted "
                f"worktree (treating as no): {e}"
            )
            return False

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
        behavior, not a hypothetical). Historically (before #58, Phase 2 of #34's
        concurrency redesign), repair cycles stole the pipeline lock from a
        non-retained ordinary holder via steal_lock() -- since removed -- so a
        live 'issues'-workspace agent could genuinely be mid-edit in this exact
        base clone when this ran. Repair cycles now wait for the lock instead of
        forcing their way in, but the other leftover-state causes described above
        (an orchestrator restart mid-checkout, a manual debugging session, an
        older worktree never cleaned up, PipelineLockManager's own stale-lock
        auto-recovery) remain live, so a dirty base clone here is still a real,
        reachable case, not a fossil this guard is holding onto out of caution
        alone. So: bail out entirely (no checkout attempted at all)
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
        # This epic's own serializer, not the process-global map guard (code
        # review on #151/WI-6): the body below runs `git worktree remove --force`
        # (15s), a directory removal, a `worktree prune` (15s) and possibly a push
        # (30s), and holding the global guard across all of that blocked every
        # other project's worktree resolution too. Same lock ordering as
        # get_or_create_epic_worktree(): per-key OUTER, map guard INNER, so a
        # cleanup and a creation for the SAME epic still cannot interleave.
        with self._epic_worktree_key_lock(key):
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
                with self._epic_worktree_lock:
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
        adopted this process) OR named in self._epic_worktrees_pending (a resolution
        that has started its git work but not yet registered -- see the per-worktree
        check below and _epic_worktrees_pending's own comment in __init__ for why
        the tracked map alone stopped covering that case once #151/WI-6 split the
        per-epic serializer out of the map guard).

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
        finished. A worktree matching NEITHER check is safe to remove
        unconditionally IF it still has a working .git -- it will be
        transparently recreated (fetch + worktree add, cheap) the next time
        get_project_dir()/get_or_create_epic_worktree() is called for that
        epic, and _push_local_commits_if_any() below is a real safety net for
        it (anything genuinely uncommitted gets a chance to reach origin
        first). A THIRD case, added after code review found this sweep was an
        unprotected second path to the same risk get_or_create_epic_worktree()
        guards against: a non-empty worktree with NO .git at all (corrupted)
        is explicitly skipped rather than force-removed -- _push_local_commits_
        if_any() is a no-op with no .git to run git commands against, so the
        "cheap, no real loss" assumption this paragraph otherwise relies on
        does not hold for it.
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
                    #
                    # _epic_worktrees_pending is what makes that re-check mean
                    # anything for a resolution still IN FLIGHT (code review on
                    # #151/WI-6). While _epic_worktree_lock was
                    # get_or_create_epic_worktree()'s whole serializer, this
                    # acquire blocked until a concurrent adoption/creation had
                    # finished and registered itself, so _epic_worktrees alone
                    # could never miss one. It is now only a map guard -- the git
                    # work and the project_checkout wait run with it released --
                    # so _epic_worktrees is empty for a worktree another thread
                    # is actively populating, and this sweep would force-remove
                    # it. The pending map is published under this same guard
                    # BEFORE any of that work starts, so the check still sees the
                    # in-flight case, without prune ever blocking (it runs on the
                    # event-loop thread at startup; taking the per-key lock here
                    # would freeze the loop for the creation's whole ~3h budget).
                    with self._epic_worktree_lock:
                        currently_tracked = (
                            str(worktree_path) in self._epic_worktrees.values()
                            or str(worktree_path) in self._epic_worktrees_pending.values()
                        )
                    if currently_tracked:
                        logger.debug(
                            f"Skipping prune of {worktree_path} -- currently tracked in "
                            "_epic_worktrees (adopted or created earlier this process) "
                            "or being adopted/created right now on another thread"
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

                    # Code review finding on get_or_create_epic_worktree()'s own
                    # new corruption guard: this sweep was a second, unprotected
                    # path to the identical destructive operation that guard
                    # exists to prevent -- neither the tracked-check nor the
                    # liveness check above catches a worktree that's corrupted
                    # (no .git at all) but currently untracked and unmounted,
                    # and _push_local_commits_if_any() below is a no-op with no
                    # .git to run git commands against, so the safety net that
                    # normally justifies "safe to remove unconditionally, cheaply
                    # recreated on demand" for this method's whole design doesn't
                    # apply here: there is no way to tell "just the unmodified
                    # checkout, safe to lose" apart from "an agent's real,
                    # uncommitted work that never got the chance to be pushed
                    # before whatever caused the corruption interrupted it" --
                    # exactly the scenario a corrupted (not merely idle) worktree
                    # is more likely to correlate with. Skip it here too, for a
                    # human to resolve the same way get_or_create_epic_worktree()
                    # asks them to -- unless it's genuinely empty, which has
                    # nothing to lose either way.
                    if self._is_corrupted_non_empty_worktree(worktree_path):
                        logger.warning(
                            f"Skipping prune of {worktree_path} -- has no .git at all "
                            "(corrupted, not a recognized worktree) but is non-empty, so "
                            "it may hold real uncommitted work. Needs manual inspection, "
                            f"then `git worktree remove --force {worktree_path}` (or "
                            "`git worktree prune`) run against the base clone."
                        )
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
