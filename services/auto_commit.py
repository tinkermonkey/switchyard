"""
Auto-Commit Service

Automatically commits code changes made by agents to feature branches.
"""

import subprocess
import logging
from enum import Enum
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)


class CommitResult(Enum):
    """
    Outcome of commit_agent_changes() -- three genuinely different states that a
    bare bool collapsed into two.

    Found in review (#148 I1): the live repair-cycle path in
    services/project_monitor.py logged a False return as "No changes to commit"
    and then auto-advanced the issue, while its restart-recovery twin in
    services/agent_container_recovery.py treats the same falsy value as
    mark_failed("Repair cycle passed but its fix was not committed"). The stated
    reason for not gating the live path was that False is indistinguishable from
    "nothing to commit" -- so this enum removes the ambiguity at the source
    instead of asking each caller to guess.

    Note for anyone reading the old comments this replaces: "nothing to commit"
    was never actually a False return. _commit_and_push() returns True for it
    (it still pushes any unpushed commits on the branch and falls through to
    `return True`), so every historical False was already a genuine failure --
    no project_dir, a non-existent directory, a main/master refusal, a failed
    `git commit`, or a non-lock exception. The bug was that nothing said so.

    * COMMITTED -- a real commit was created (and pushed, best-effort).
    * NOTHING_TO_COMMIT -- the working tree was clean. Not a failure: the agent
      legitimately changed nothing, or a previous attempt already committed.
      Any unpushed commits on the branch were still pushed.
    * FAILED -- the commit did not happen and the reason is a fault. Whatever
      the agent wrote (if anything) is still uncommitted on disk.

    A resource-lock timeout is deliberately NOT a member: it is raised, not
    returned, so callers route it to their own contention path (#148, see
    commit_agent_changes()'s Raises section and services/resource_lock_errors.py).

    __bool__ deliberately does NOT follow TouchResult/ResetResult's "only the
    one success value is truthy" rule, because here there are two non-failure
    values and the pre-existing bool contract already made both truthy. Keeping
    that contract is what lets this change be a pure refinement: every existing
    truthiness-based caller and assertion (`if commit_success`,
    `overall_success and not commit_success[0]`) keeps its exact original
    meaning, and only the callers that need the three-way distinction are
    updated to test identity against a member.
    """

    COMMITTED = "committed"
    NOTHING_TO_COMMIT = "nothing_to_commit"
    FAILED = "failed"

    def __bool__(self) -> bool:
        return self is not CommitResult.FAILED


class AutoCommitService:
    """Handles automatic git commits for agent changes"""

    async def commit_agent_changes(
        self,
        project: str,
        agent: str,
        task_id: str,
        project_dir: Union[str, Path],
        issue_number: Optional[int] = None,
        custom_message: Optional[str] = None,
    ) -> CommitResult:
        """
        Commit changes made by an agent

        Args:
            project: Project name
            agent: Agent name that made the changes
            task_id: Task ID
            project_dir: The SAME directory the agent actually wrote to -- callers
                must pass their already-resolved pipeline_run.project_dir (or the
                shared base clone directory, for workspace types that stay there)
                rather than having this method re-resolve it independently (issue
                #123, WI-D of #119). Every caller now reads this from a single
                resolved pipeline_run/workspace-context source instead of each
                deriving its own via get_project_dir(epic_id=...) -- the divergent
                epic_id/branch_name parameters this method used to accept (#48)
                are gone; there is nothing left to independently resolve.
            issue_number: GitHub issue number (if applicable)
            custom_message: Custom commit message (optional)

        Returns:
            A CommitResult (see its docstring): COMMITTED, NOTHING_TO_COMMIT or
            FAILED. Truthiness is unchanged from the previous bool contract --
            only FAILED is falsy -- so existing `if commit_success:` callers
            behave identically; callers that must distinguish "the fix never
            landed" from "there was nothing to land" compare against the member.

        Raises:
            ProjectCheckoutLockTimeoutError / DevContainerBuildLockTimeoutError:
                the ONE outcome that is not folded into a returned CommitResult
                (#148). It is not FAILED: nothing is wrong with this project or
                this agent, the commit simply never ran because a different
                holder owned the lock for its whole timeout, and the agent's
                work IS on disk uncommitted. Callers route it to their own
                lock-contention path (see services/resource_lock_errors.py),
                which retries rather than reporting a fault.
        """
        if not project_dir:
            # A caller with no resolved directory (e.g. an old context.json
            # predating this field) must not reach a bare Path(None) TypeError
            # here -- this method's documented contract is a CommitResult for
            # every failure mode except a resource-lock timeout (#148, see Raises
            # above), matching its repair-cycle callers, whose threading.Thread
            # wrapper handles only that one exception.
            logger.error(
                f"commit_agent_changes() called for {project}/#{issue_number} "
                f"(agent={agent}) with no project_dir -- cannot determine which "
                "directory to commit."
            )
            return CommitResult.FAILED

        project_dir = Path(project_dir)

        if not project_dir.exists():
            logger.error(f"Project directory does not exist: {project_dir}")
            return CommitResult.FAILED

        # Fast-fail pre-check (not main/master) BEFORE acquiring the
        # project_checkout lock below (#56 review): a project_dir stuck on
        # main/master will fail this check regardless of lock state, so
        # checking first avoids turning an instant rejection into a long
        # stall if the lock happens to be contended. This is ONLY an early
        # exit, not the value actually used to commit/push -- see below.
        current_branch = self._get_current_branch(project_dir)
        if current_branch in ['main', 'master']:
            logger.error(f"WORKFLOW BUG: Agent executed on {current_branch} branch without proper branch preparation!")
            logger.error(f"Project: {project}, Agent: {agent}, Issue: {issue_number}")
            logger.error(f"FeatureBranchManager should have created a branch BEFORE agent execution")
            logger.error(f"Auto-commit REFUSED to create emergency branch - this would bypass parent/sub-issue logic")
            return CommitResult.FAILED

        try:
            # project_checkout lock (#54): if project_dir is the shared base
            # clone (not an isolated epic worktree -- see is_base_clone_dir()),
            # this commit/add/push must serialize against every other operation
            # touching that same directory (another board's checkout, the
            # startup clone/update, a container run bind-mounting it) instead of
            # racing it. Epic-worktree-scoped commits are deliberately NOT
            # locked -- they don't share a directory with anything else.
            from services.project_workspace import workspace_manager
            if workspace_manager.is_base_clone_dir(project, project_dir):
                from services.project_checkout_lock import project_checkout_lock_async

                # issue_number here is log attribution only, not the lock's
                # holder identity -- see project_checkout_lock.py's module
                # docstring ("Why every acquisition gets its own unique
                # holder id").
                async with project_checkout_lock_async(project, issue_number):
                    # CRITICAL: re-read current_branch here, AFTER acquiring
                    # the lock, not the value read before it (found in final
                    # whole-PR review): the pre-lock read above can be stale
                    # by the time we actually get here -- this exact lock
                    # exists because a DIFFERENT operation (another board of
                    # the same project) can check out a DIFFERENT branch in
                    # this same shared directory while we wait for it. Using
                    # the stale branch name would push the on-disk tree
                    # (whatever the lock's previous holder left checked out)
                    # to the WRONG branch ref, corrupting it with unrelated
                    # commits. Only the fast-fail decision above is safe to
                    # make with the pre-lock value; the actual push must use
                    # fresh state.
                    fresh_branch = self._get_current_branch(project_dir)
                    # Re-apply the same WORKFLOW BUG refusal the pre-lock
                    # fast-fail check above already does, using the fresh
                    # (post-lock) value -- belt-and-suspenders with
                    # _commit_and_push()'s own main/master guard below: this
                    # refuses with a message specific to "raced onto
                    # main/master during the lock wait," while that guard is
                    # the structural backstop that holds even if a future
                    # caller skips this check.
                    if fresh_branch in ['main', 'master']:
                        logger.error(
                            f"WORKFLOW BUG: {project_dir} is on {fresh_branch} after "
                            f"acquiring the project_checkout lock (was on a feature "
                            f"branch before the wait) -- another operation must have "
                            f"checked out {fresh_branch} in this shared directory "
                            f"while we waited. Project: {project}, Agent: {agent}, "
                            f"Issue: {issue_number}. Refusing to commit."
                        )
                        return CommitResult.FAILED
                    return await self._commit_and_push(
                        project, agent, task_id, project_dir, fresh_branch, issue_number, custom_message
                    )

            return await self._commit_and_push(
                project, agent, task_id, project_dir, current_branch, issue_number, custom_message
            )

        except Exception as e:
            # Project resource-lock timeout (#148): the commit never ran because
            # another holder owned this project's base clone for the whole of that
            # lock's timeout, and the maker's work IS on disk, uncommitted. Even
            # with CommitResult in place this must not be folded into a return
            # value: FAILED would send agent_container_recovery.py (and now the
            # live path too) to mark_failed("Repair cycle passed but its fix was
            # not committed"), durably retaining the board lock over contention
            # that clears itself, and NOTHING_TO_COMMIT would lose the fix
            # outright. Re-raise so callers see the same typed exception every
            # other dispatch path now routes to its contention path.
            from services.resource_lock_errors import is_lock_timeout_error, describe_lock_timeout
            if is_lock_timeout_error(e):
                logger.warning(
                    f"Auto-commit for {project} (agent {agent}, issue {issue_number}) could "
                    f"not acquire the project_checkout lock -- propagating rather than "
                    f"reporting 'nothing to commit': {describe_lock_timeout(e)}"
                )
                raise

            logger.error(f"Failed to auto-commit changes for {project}: {e}")
            return CommitResult.FAILED

    async def _commit_and_push(
        self,
        project: str,
        agent: str,
        task_id: str,
        project_dir: Path,
        current_branch: Optional[str],
        issue_number: Optional[int],
        custom_message: Optional[str],
    ) -> CommitResult:
        """
        The actual git add/commit/push sequence, split out of
        commit_agent_changes() (#54) so its caller can wrap it in the
        project_checkout lock only when needed, without duplicating the
        try/except that still lives in commit_agent_changes() around both the
        locked and unlocked call paths. `current_branch` is resolved once by
        the caller (before the lock -- see commit_agent_changes()'s own
        comment, #56 review) rather than re-read here.

        Defense-in-depth main/master guard, found in a later review pass:
        both callers already refuse to reach this method with current_branch
        on main/master, but a bare stage-and-commit here with no guard of
        its own meant that guarantee lived ONLY in the callers -- a future
        third call site (or a caller's own logic change) could silently
        reintroduce a real commit onto the shared clone's main branch. This
        check makes the invariant hold structurally, not just by caller
        discipline.
        """
        if current_branch in ['main', 'master']:
            logger.error(
                f"WORKFLOW BUG: _commit_and_push() called with current_branch="
                f"{current_branch!r} for {project_dir} -- refusing to stage or "
                f"commit onto {current_branch}. Project: {project}, Agent: "
                f"{agent}, Issue: {issue_number}."
            )
            return CommitResult.FAILED

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
                return CommitResult.FAILED

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

        # NOTE: a failed push is deliberately still not a FAILED result -- that
        # was true of the bool contract too (it only warns). The commit itself
        # landed locally; only the push didn't. Changing that is a separate
        # question from #148 I1, which is about the commit never happening.
        return CommitResult.COMMITTED if has_changes else CommitResult.NOTHING_TO_COMMIT

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
