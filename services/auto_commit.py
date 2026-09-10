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
        expected_branch: Optional[str] = None,
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
            expected_branch: The branch this commit is meant to land on, read
                from the SAME already-resolved source the caller reads
                project_dir from -- pipeline_run.branch_name, which
                resolve_workspace() derives from the epic/issue context and
                get_or_create_epic_worktree() checks the worktree out to. This
                is the independently derivable expectation #143 asks for, and
                it is deliberately caller-supplied rather than re-derived here:
                #123 removed this method's own epic_id/branch_name resolution
                precisely because a second, divergent derivation of the
                caller's already-decided workspace is the bug, not the fix.
                When supplied, a checked-out branch that disagrees with it is
                refused outright (CommitResult.FAILED, changes left on disk) --
                in an epic worktree as well as the shared base clone, see
                _verify_commit_branch(). Optional -- when omitted, the branch
                observed before the lock wait stands in as a weaker
                expectation, so a caller that cannot resolve one is degraded
                rather than blocked.

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

        # Fast-fail pre-check (unreadable branch, or main/master) BEFORE
        # acquiring the project_checkout lock below (#56 review): a project_dir
        # stuck on main/master will fail this check regardless of lock state, so
        # checking first avoids turning an instant rejection into a long
        # stall if the lock happens to be contended. In the shared base clone
        # this is ONLY an early exit, not the value actually used to
        # commit/push -- see below.
        pre_lock_branch = self._get_current_branch(project_dir)
        if pre_lock_branch is None:
            # #149 item 33: _get_current_branch() used to return None silently,
            # and every `branch in ['main', 'master']` guard read that None as
            # "a perfectly good non-main branch" and waved it through. Refusing
            # here closes that pass-through at the top of the flow -- with no
            # branch name, neither the expectation check below nor the push
            # target has anything to compare against.
            logger.error(
                f"Auto-commit could not determine the current branch in {project_dir} "
                f"(project={project}, agent={agent}, issue={issue_number}) -- refusing "
                "to commit against an unknown branch. See the _get_current_branch() "
                "error logged above for the specific git failure."
            )
            return CommitResult.FAILED
        if pre_lock_branch in ['main', 'master']:
            logger.error(f"WORKFLOW BUG: Agent executed on {pre_lock_branch} branch without proper branch preparation!")
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
            # #149 item 23 collapsed the locked and unlocked paths onto one
            # self._commit_and_push(...) call via a nullcontext, so a future
            # argument change cannot be applied to one and missed on the other.
            # #140 item 4 then moved the is_base_clone_dir() decision itself
            # into the lock module, where the two other copies of this guard
            # (claude/claude_integration.py) now go too. `is_shared_dir` is the
            # yielded value rather than a second is_base_clone_dir() call -- the
            # branch re-read below depends on it being the SAME answer the lock
            # decision was made on.
            #
            # issue_number here is log attribution only, not the lock's holder
            # identity -- see project_checkout_lock.py's module docstring ("Why
            # every acquisition gets its own unique holder id").
            from services.project_checkout_lock import project_checkout_lock_if_shared_async

            async with project_checkout_lock_if_shared_async(
                project, project_dir, issue_number
            ) as is_shared_dir:
                # CRITICAL: in the shared base clone, re-read the branch HERE,
                # AFTER acquiring the lock, rather than reusing the value read
                # before it (found in final whole-PR review): this lock exists
                # because a DIFFERENT operation (another board of the same
                # project) can check out a DIFFERENT branch in this same shared
                # directory while we wait for it, and the stale name would push
                # the on-disk tree to the WRONG branch ref. An isolated epic
                # worktree shares its directory with nothing and was not waited
                # on at all, so nothing can have moved its HEAD in between --
                # the pre-lock read is still authoritative there, and re-reading
                # would only be a second pointless subprocess.
                commit_branch = (
                    self._get_current_branch(project_dir) if is_shared_dir else pre_lock_branch
                )
                if not self._verify_commit_branch(
                    commit_branch=commit_branch,
                    expected_branch=expected_branch,
                    pre_lock_branch=pre_lock_branch,
                    project=project,
                    agent=agent,
                    issue_number=issue_number,
                    project_dir=project_dir,
                    is_shared_dir=is_shared_dir,
                ):
                    return CommitResult.FAILED

                return await self._commit_and_push(
                    project, agent, task_id, project_dir, commit_branch, issue_number, custom_message
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

    def _verify_commit_branch(
        self,
        commit_branch: Optional[str],
        expected_branch: Optional[str],
        pre_lock_branch: Optional[str],
        project: str,
        agent: str,
        issue_number: Optional[int],
        project_dir: Path,
        is_shared_dir: bool,
    ) -> bool:
        """
        Decide whether `commit_branch` -- whatever is checked out in project_dir
        at the moment of committing -- is genuinely THIS commit's own target.

        #143 (#149 finding A): the previous guard rejected only the two
        always-wrong values (main/master), so the one branch it structurally
        could not catch was another issue's feature branch. In the shared base
        clone: board A's agent container releases the project_checkout lock when
        it exits, board B takes it and checks out B's OWN feature branch in that
        same directory, A's commit_agent_changes() then acquires the lock, reads
        a branch that is not main/master, and commits + pushes A's uncommitted
        work onto B's branch.

        Why the pre-lock branch is only a fallback expectation, not the fix:
        claude_integration.py holds the checkout lock for the container's
        lifetime and releases it at container exit, and commit_agent_changes()
        re-acquires it separately -- so B can win the gap BETWEEN those two
        acquisitions, and A's pre-lock read already sees B's branch. Comparing
        pre-lock against post-lock therefore cannot close this on its own; only
        an expectation derived outside the shared directory's ambient git state
        (expected_branch) can. The pre-lock value is still used when no
        expected_branch is supplied, because it does close the narrower "branch
        changed during the lock wait" sequence and is strictly better than the
        main/master-only test it replaces.

        Refuses rather than checking the expected branch out: the working tree
        here holds the agent's uncommitted changes on top of whatever baseline
        the other holder left, so `git checkout` would either fail outright or
        silently reinterpret those changes against the wrong baseline. Returning
        False leaves the work on disk for the caller's own failure path -- WI-3
        (#148) established that the right response to a dirty SHARED clone is
        mark_failed() with the board lock retained, not a release over dirty
        state.

        A supplied expected_branch is fatal on mismatch in BOTH directory
        kinds. This used to be fatal only in the shared base clone, on the
        reasoning that an epic worktree's checked-out branch IS that epic's own
        branch, so a disagreement had to mean the expectation had gone stale.
        Found in review: that made the refusal structurally unreachable. Every
        production call site resolves an epic worktree (review_cycle.py's three
        sites are gated on workspace_type == 'issues'; project_monitor.py's and
        agent_container_recovery.py's repair-cycle sites take project_dir from
        the same resolve_workspace() result), and the ONLY producer of a shared
        base clone -- _resolve_workspace_for_cycle()'s git-free 'discussions'
        branch -- returns branch_name None by construction. So the two states
        never co-occurred: wherever an expectation existed the mismatch only
        warned, and wherever the refusal was fatal there was no expectation to
        compare against. #143's own scenario was still reachable straight
        through the new guard.

        The staleness the old leniency protected against is also not the real
        risk it was taken for: resolve_workspace() re-derives the worktree's
        ACTUAL branch (workspace_manager._current_worktree_branch()) at
        resolution time rather than trusting its own locally-resolved name, so
        an adopted-on-restart worktree persists the branch it is really on.
        Nothing in the orchestrator checks a branch out inside an epic worktree
        either -- every checkout_branch() call site is base-clone-scoped. What
        is left as a mismatch source is the agent container's own git moving
        HEAD, and committing there pushes this agent's work onto whatever
        branch it moved to: #143's corruption reached through a different door.
        Refusing leaves that work on disk for the caller's failure path, which
        is the same trade the shared clone already makes.

        This is not the only commit path in the orchestrator, and the invariant
        is only worth as much as the weakest one: FeatureBranchManager
        .finalize_feature_branch_work() is the equivalent step for ordinary
        'issues'/'hybrid' dispatch (the higher-volume path -- this method covers
        the review-cycle and repair-cycle ones), and it used to do the opposite
        here, adopting whatever branch git reported as its push target. It now
        takes the same caller-supplied expected_branch off the same
        pipeline_run.branch_name and refuses on the same terms -- see
        _verify_finalize_branch() (#149 WI-4 review). The third path, and the one
        that was actually the weakest of them, is agent_executor.py's
        _failsafe_commit_check(): an unguarded `git add -A` + `git commit
        --no-verify` + push of ambient HEAD, reached on every skip_workspace_prep
        dispatch (all of pipeline/repair_cycle.py's inner agent calls). It now
        reads the same expectation off task_context['branch_name'] and refuses on
        the same terms -- see _verify_failsafe_branch() there.

        is_shared_dir still decides fatality for the FALLBACK expectation
        (pre_lock_branch, used when no expected_branch is supplied): there it
        closes the narrower "another board checked out during our lock wait"
        sequence, and in an unlocked worktree the fallback and commit_branch
        are the same value by construction, so the test is vacuous anyway.

        Returns:
            True to proceed with the commit; False to refuse (the caller
            returns CommitResult.FAILED).
        """
        if commit_branch is None:
            # #149 item 33 again, at the point it actually matters: in the
            # shared base clone this is a *fresh* read taken under the lock, so
            # the pre-lock refusal above did not cover it.
            logger.error(
                f"Auto-commit could not determine the current branch in {project_dir} "
                f"after acquiring the project_checkout lock (project={project}, "
                f"agent={agent}, issue={issue_number}) -- refusing to commit against "
                "an unknown branch."
            )
            return False

        if commit_branch in ['main', 'master']:
            # Only reachable for the shared base clone: an epic worktree's
            # commit_branch IS the pre-lock value, which the fast-fail check in
            # commit_agent_changes() already rejected.
            logger.error(
                f"WORKFLOW BUG: {project_dir} is on {commit_branch} at commit time "
                f"(was on {pre_lock_branch!r} before) -- another operation must have "
                f"checked out {commit_branch} in this shared directory while we "
                f"waited for the lock. Project: {project}, Agent: {agent}, "
                f"Issue: {issue_number}. Refusing to commit."
            )
            return False

        # `or` rather than `is not None` throughout this block, so an empty
        # expectation is treated as no expectation by every test below rather
        # than by only some of them.
        target_branch = expected_branch or pre_lock_branch
        if not expected_branch:
            # An expectation-less commit in the SHARED clone is the one shape
            # #143 is undefended against -- the pre-lock fallback below cannot
            # catch a checkout that won the gap before our first read (see this
            # method's docstring), so it is an error, not a note. In an isolated
            # worktree the fallback and the "real" expectation are the same
            # value anyway, so it stays at debug.
            log_missing = logger.error if is_shared_dir else logger.debug
            log_missing(
                f"commit_agent_changes() got no expected_branch for {project}/"
                f"#{issue_number} (agent={agent}) -- falling back to the branch seen "
                f"before the lock ({pre_lock_branch!r}) as this commit's target. "
                "Callers that resolve a workspace should pass "
                "pipeline_run.branch_name."
            )

        if target_branch and commit_branch != target_branch:
            detail = (
                f"{project_dir} is on {commit_branch!r} but this commit's target is "
                f"{target_branch!r} (project={project}, agent={agent}, "
                f"issue={issue_number})"
            )
            if expected_branch:
                # Fatal in both directory kinds -- see the docstring for why
                # scoping this to the shared clone made it unreachable.
                where = (
                    "this shared base clone" if is_shared_dir
                    else "this epic's own worktree"
                )
                logger.error(
                    f"Refusing to auto-commit onto the wrong branch: {detail}. "
                    f"Something checked out a different branch in {where}; committing "
                    "here would push this agent's work onto an unrelated issue's "
                    "branch (#143). The changes are left uncommitted on disk."
                )
                return False
            if is_shared_dir:
                logger.error(
                    f"Refusing to auto-commit onto the wrong branch: {detail}. Another "
                    "operation checked out its own branch in this shared base clone "
                    "while we waited for the project_checkout lock; committing here "
                    "would push this agent's work onto an unrelated issue's branch "
                    "(#143). The changes are left uncommitted on disk."
                )
                return False
            # Unreachable today: without an expectation the target IS
            # pre_lock_branch, and an unlocked worktree's commit_branch is that
            # same object. Kept so a future re-read on this path cannot silently
            # become a no-op comparison.
            logger.warning(
                f"Auto-commit branch mismatch in an isolated epic worktree with no "
                f"expected_branch to verify against: {detail}. Proceeding anyway."
            )

        return True

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
        try/except that still lives in commit_agent_changes() around it.
        `current_branch` is resolved and verified once by the caller (under
        the lock, when project_dir is the shared base clone -- see
        commit_agent_changes()'s own comment, #56 review, and
        _verify_commit_branch()) rather than re-read here.

        Defense-in-depth main/master guard, found in a later review pass:
        the caller already refuses to reach this method with current_branch
        on main/master, but a bare stage-and-commit here with no guard of
        its own meant that guarantee lived ONLY in the caller -- a future
        second call site (or the caller's own logic change) could silently
        reintroduce a real commit onto the shared clone's main branch. This
        check makes the invariant hold structurally, not just by caller
        discipline. None is refused by the same guard for the same reason
        (#149 item 33): it used to pass straight through the `in` test as
        "not main/master". 'HEAD' -- what git prints for a detached HEAD --
        is refused alongside it: _get_current_branch() now maps that to None
        before it can ever get here, so this is the same defense-in-depth for
        a future call site that resolves its branch some other way.
        """
        if current_branch is None or current_branch in ['main', 'master', 'HEAD']:
            logger.error(
                f"WORKFLOW BUG: _commit_and_push() called with current_branch="
                f"{current_branch!r} for {project_dir} -- refusing to stage or "
                f"commit onto {current_branch}. Project: {project}, Agent: "
                f"{agent}, Issue: {issue_number}."
            )
            return CommitResult.FAILED

        # Check if there are changes to commit
        has_changes = self._check_for_changes(project_dir)
        if has_changes is None:
            # #149 item 33 at the module's last unguarded git boundary: git
            # could not say whether the tree is dirty, and the old bool
            # contract answered "clean" -- which skipped the commit, returned
            # the truthy NOTHING_TO_COMMIT, and let both repair-cycle callers
            # advance the issue with the fix still sitting on disk. Refusing
            # keeps the work where it is and routes them to their existing
            # mark_failed/retain-lock paths instead.
            logger.error(
                f"Auto-commit could not determine whether {project_dir} has "
                f"uncommitted changes (project={project}, agent={agent}, "
                f"issue={issue_number}) -- refusing to report a clean tree. See "
                "the _check_for_changes() error logged above for the specific "
                "git failure. Any changes are left uncommitted on disk."
            )
            return CommitResult.FAILED

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

    def _check_for_changes(self, project_dir: Path) -> Optional[bool]:
        """
        Whether there are uncommitted changes, or None if git could not say.

        The None is _get_current_branch()'s sibling (#149 item 33), at the last
        unguarded git boundary left in this module. This used to ignore
        result.returncode entirely and return bool(result.stdout.strip()), with
        the `except` arm returning False -- so a `git status` that exited 128 on
        an `.git/index.lock` left behind by a killed agent container, or that
        exceeded the timeout on a large tree the agent had just rewritten, read
        as "clean tree". _commit_and_push() then skipped the commit, pushed
        nothing new and returned NOTHING_TO_COMMIT, which is truthy, so
        project_monitor.py's and agent_container_recovery.py's repair-cycle
        paths logged "No changes to commit" and advanced the issue to PR review
        on a branch with no fix on it -- the exact tests-passed-but-fix-never-
        committed outcome CommitResult exists to make impossible. The
        non-zero-returncode variant said nothing at all.

        Both failure arms are now None, distinguishable from a genuinely clean
        tree, and the caller turns it into CommitResult.FAILED.
        """
        try:
            result = subprocess.run(
                ['git', 'status', '--porcelain'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                logger.error(
                    f"Failed to check for changes in {project_dir}: git status "
                    f"exited {result.returncode}: {result.stderr.strip()}"
                )
                return None

            # If output is empty, no changes
            return bool(result.stdout.strip())

        except Exception as e:
            logger.error(f"Failed to check for changes in {project_dir}: {e}")
            return None

    def _get_current_branch(self, project_dir: Path) -> Optional[str]:
        """
        Get the current branch name, or None if there isn't a usable one.

        Every None return is now logged with its cause (#149 item 33): a
        non-zero `git rev-parse` used to fall out of the `if` and return None
        silently, and each caller's `branch in ['main', 'master']` test then
        read that None as "a perfectly good non-main branch" and proceeded.
        The callers refuse on None; this makes the reason recoverable from the
        logs instead of leaving an unexplained commit refusal.

        A DETACHED HEAD is folded into that same None (later review pass on
        #149 item 33): `git rev-parse --abbrev-ref HEAD` prints the literal
        string 'HEAD' with exit 0 when nothing is checked out, and that value
        is not None and not main/master, so it sailed through every guard --
        the commit landed on no branch at all and the subsequent
        `git push -u origin HEAD` failed with "The destination you provided is
        not a full refname", which _commit_and_push() only warns about. The
        result was a dangling commit inside an epic worktree that the startup
        prune_epic_worktrees() sweep can later remove, reported to the caller
        as COMMITTED. ProjectWorkspaceManager._current_worktree_branch()
        already treats 'HEAD' as unusable for exactly this reason; this is the
        same rule at this module's own git boundary, so all three None
        refusals cover it without each growing a second sentinel test.
        """
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                branch = result.stdout.strip()
                if branch and branch != 'HEAD':
                    return branch
                if branch == 'HEAD':
                    logger.error(
                        f"{project_dir} is on a DETACHED HEAD (git rev-parse "
                        "--abbrev-ref HEAD printed 'HEAD') -- there is no branch to "
                        "commit onto or push to. An interrupted rebase/bisect, or an "
                        "agent running `git checkout <sha>` in its own worktree, "
                        "leaves this state."
                    )
                else:
                    logger.error(
                        f"git rev-parse --abbrev-ref HEAD succeeded but printed nothing in "
                        f"{project_dir} -- cannot determine the current branch."
                    )
            else:
                logger.error(
                    f"Failed to get current branch in {project_dir}: git rev-parse "
                    f"exited {result.returncode}: {result.stderr.strip()}"
                )

        except Exception as e:
            logger.error(f"Failed to get current branch in {project_dir}: {e}")

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
