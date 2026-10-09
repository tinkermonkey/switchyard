"""Post-VERIFIED worktree environment-fix notifications.

After ``dev_environment_verifier`` records ``DevContainerStatus.VERIFIED``,
this module enumerates every active epic worktree for the project and posts a
single GitHub comment on the issue that owns each worktree.

Phase 1 (comment-only): describes what changed and whether the worktree needs
a manual merge or has a conflicting local constraint.

Phase 2 (auto-apply for clean case): when the worktree has no conflicting
local changes the fix is automatically applied (targeted per-file checkout)
onto the worktree's feature branch and pushed.  A distinct "already done"
comment is posted so the owner is not confused by instructions that are no
longer needed.  For the conflicting case Phase 1's comment-only behaviour is
preserved intact.

Divergence classification uses a **three-way comparison** against the parent of
the fix commit (``commit_sha~1``).  A worktree file is only counted as a local
conflict when it differs from the pre-fix baseline — "just behind" worktrees
(same content as pre-fix) are correctly treated as clean.

Concurrency: each worktree gets a try-only ``asyncio.Lock``.  If the lock is
already held (another auto-apply coroutine is in flight for that worktree) the
caller falls back to Phase 1's comment-only behaviour immediately.

Acceptance criteria:

- Exactly one comment is posted per currently-active worktree (not per-run,
  not duplicated on retries -- ``active_run_protected is True`` is the gate).
- Worktrees with ``UNKNOWN`` or ``UNOWNED`` ownership receive no comment.
- The comment correctly distinguishes clean from conflicting.
- The auto-apply path never touches a worktree when ``has_conflicts`` is True.
- A failed auto-apply falls back gracefully to Phase 1's comment.
- No existing dev-environment setup/verification behaviour is changed.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Environment-relevant file patterns -- only commits that touched at least one
# of these are considered env-fix commits.  The actual changed-file list comes
# from git; this set is used to narrow the last-N-commits search so that a
# routine code commit doesn't accidentally trigger a flood of notifications.
_ENV_FILE_PATTERNS: Set[str] = {
    'pyproject.toml',
    'requirements.txt',
    'requirements-dev.txt',
    'requirements_dev.txt',
    'Pipfile',
    'Pipfile.lock',
    'poetry.lock',
    'uv.lock',
    'setup.py',
    'setup.cfg',
    'Dockerfile.agent',
    'Dockerfile',
    'docker-compose.yml',
    'docker-compose.yaml',
    '.python-version',
    'runtime.txt',
    'environment.yml',
}

# Look back at most this many commits when searching for the most recent
# env-touching commit.  Keeps the git log call cheap and bounded.
_COMMIT_SEARCH_DEPTH = 20

# Cap for file content displayed in comments (characters).
_FILE_CONTENT_CAP = 3_000
# Cap for diff output displayed in comments (characters).
_DIFF_CAP = 2_000


def _run_git(
    args: List[str],
    cwd: Path,
    *,
    check: bool = False,
    timeout: Optional[int] = None,
) -> Tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            ['git'] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, ['git'] + args,
                output=result.stdout, stderr=result.stderr,
            )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        logger.warning("git %s timed out after %ss in %s", args[0], timeout, cwd)
        return 1, '', f'git command timed out after {timeout}s'
    except FileNotFoundError:
        logger.warning("git binary not found; cannot perform worktree diff")
        return 1, '', 'git binary not found'


def _get_env_fix_commit(base_clone: Path) -> Optional[Tuple[str, List[str]]]:
    """Return (commit_sha, [changed_env_files]) for the most recent commit on
    the base clone that touched at least one environment-relevant file.

    Returns ``None`` when:
    - git is unavailable
    - the repository has no commits
    - none of the last ``_COMMIT_SEARCH_DEPTH`` commits touched an env file
    """
    # Retrieve the last N commit SHAs.
    rc, stdout, _ = _run_git(
        ['log', '--format=%H', f'-{_COMMIT_SEARCH_DEPTH}'],
        cwd=base_clone,
    )
    if rc != 0 or not stdout.strip():
        return None

    for sha in stdout.strip().splitlines():
        sha = sha.strip()
        if not sha:
            continue
        # Get the list of files changed in this commit.
        rc2, files_out, _ = _run_git(
            ['show', '--name-only', '--format=', sha],
            cwd=base_clone,
        )
        if rc2 != 0:
            continue
        changed = [
            f.strip() for f in files_out.strip().splitlines() if f.strip()
        ]
        env_files = [
            f for f in changed
            if any(f == pat or f.endswith('/' + pat) for pat in _ENV_FILE_PATTERNS)
        ]
        if env_files:
            return sha, env_files

    return None


def _read_file_at_ref(repo_dir: Path, ref: str, relative_path: str) -> Optional[str]:
    """Read the content of ``relative_path`` at git ref ``ref`` in ``repo_dir``.

    Returns ``None`` when the file does not exist at that ref.
    """
    rc, content, _ = _run_git(['show', f'{ref}:{relative_path}'], cwd=repo_dir)
    return content if rc == 0 else None


def _read_file_at_head(repo_dir: Path, relative_path: str) -> Optional[str]:
    """Read the content of ``relative_path`` from HEAD of ``repo_dir`` via git.

    Returns ``None`` when the file does not exist at HEAD in that repo.
    """
    return _read_file_at_ref(repo_dir, 'HEAD', relative_path)


def _unified_diff(a_label: str, a_text: str, b_label: str, b_text: str) -> str:
    """Return a unified diff string (capped to ``_DIFF_CAP`` chars)."""
    diff_lines = list(difflib.unified_diff(
        a_text.splitlines(keepends=True),
        b_text.splitlines(keepends=True),
        fromfile=a_label,
        tofile=b_label,
        lineterm='',
    ))
    raw = ''.join(diff_lines)
    if len(raw) > _DIFF_CAP:
        raw = raw[:_DIFF_CAP] + '\n... (diff truncated)'
    return raw


class _FileDivergence:
    """Result of comparing one environment file between main and a worktree."""

    def __init__(
        self,
        path: str,
        main_content: Optional[str],
        worktree_content: Optional[str],
        pre_fix_content: Optional[str] = None,
    ) -> None:
        self.path = path
        self.main_content = main_content
        self.worktree_content = worktree_content
        # Content of the file at the parent of the fix commit (commit_sha~1).
        # Used for three-way classification — None when unavailable (falls back
        # to two-way comparison).
        self.pre_fix_content = pre_fix_content

    @property
    def only_in_main(self) -> bool:
        return self.main_content is not None and self.worktree_content is None

    @property
    def only_in_worktree(self) -> bool:
        return self.main_content is None and self.worktree_content is not None

    @property
    def identical(self) -> bool:
        return self.main_content == self.worktree_content

    @property
    def diverged(self) -> bool:
        """True when both sides have content but it differs."""
        return (
            self.main_content is not None
            and self.worktree_content is not None
            and self.main_content != self.worktree_content
        )

    @property
    def locally_conflicts(self) -> bool:
        """True when the worktree's state conflicts with a clean auto-apply.

        Uses **three-way** comparison when ``pre_fix_content`` is available:

        * Worktree deleted a file that existed before the fix → conflict.
        * Worktree's content differs from the pre-fix baseline → conflict.
        * Worktree's content equals the pre-fix baseline (just behind, not
          locally modified) → *not* a conflict; the targeted checkout will
          bring it up to date.

        Falls back to the two-way ``diverged or only_in_worktree`` test when
        ``pre_fix_content`` is unavailable (e.g. the fix is the initial commit
        and has no parent).
        """
        if self.pre_fix_content is not None:
            if self.worktree_content is None:
                # Worktree deleted a file that existed pre-fix.
                return True
            return self.worktree_content != self.pre_fix_content
        # No pre-fix baseline: conservative two-way fallback.
        return self.diverged or self.only_in_worktree


def _get_pre_fix_sha(base_clone: Path, commit_sha: str) -> Optional[str]:
    """Return the SHA of ``commit_sha``'s parent (``commit_sha~1``).

    Returns ``None`` when the parent cannot be resolved (initial commit, git
    unavailable, etc.).  When ``None``, callers fall back to two-way
    comparison.
    """
    rc, sha_out, _ = _run_git(['rev-parse', f'{commit_sha}~1'], cwd=base_clone)
    if rc != 0 or not sha_out.strip():
        return None
    return sha_out.strip()


def _check_file_divergence(
    base_clone: Path,
    worktree_path: Path,
    changed_files: List[str],
    pre_fix_sha: Optional[str] = None,
) -> List[_FileDivergence]:
    """For each env file touched by the fix, compare main vs worktree.

    When ``pre_fix_sha`` is provided each ``_FileDivergence`` also carries the
    file's content at that ref (the parent of the fix commit), enabling the
    three-way ``locally_conflicts`` classification.
    """
    results: List[_FileDivergence] = []
    for rel_path in changed_files:
        main_content = _read_file_at_head(base_clone, rel_path)
        worktree_content = _read_file_at_head(worktree_path, rel_path)
        pre_fix_content = (
            _read_file_at_ref(base_clone, pre_fix_sha, rel_path)
            if pre_fix_sha
            else None
        )
        results.append(_FileDivergence(rel_path, main_content, worktree_content, pre_fix_content))
    return results


def _cap(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit] + '\n... (truncated)'
    return text


def _classify_divergence(divergences: List[_FileDivergence]) -> Tuple[bool, bool]:
    """Return ``(has_conflicts, all_identical)`` for a list of divergences.

    ``has_conflicts`` is True when any file reports ``locally_conflicts``
    (three-way when a pre-fix baseline is available, two-way fallback
    otherwise) or ``only_in_worktree``.

    ``only_in_main`` is *not* a conflict — a targeted checkout will add it
    cleanly unless the pre-fix baseline shows the worktree deleted it (handled
    inside ``locally_conflicts``).

    ``all_identical`` is True when every touched file is already identical
    between main and the worktree.
    """
    has_conflicts = any(d.locally_conflicts for d in divergences)
    all_identical = all(d.identical for d in divergences)
    return has_conflicts, all_identical


def _build_comment(
    commit_sha: str,
    divergences: List[_FileDivergence],
    env_fix_description: str,
) -> str:
    """Build the GitHub comment body for one worktree."""
    has_conflicts, all_identical = _classify_divergence(divergences)

    lines: List[str] = []
    lines.append(
        "## ⚠️ Dev Environment Fix Applied to Main Branch\n"
    )
    lines.append(
        "The `dev_environment_verifier` has approved an environment fix on the "
        "main branch for this project.  **This worktree may need to incorporate "
        "that fix.**\n"
    )

    # Fix description (trimmed from previous_stage_output).
    if env_fix_description:
        description_snippet = env_fix_description.strip()
        # Prefer the "Problem Analysis" section if present.
        marker = 'Problem Analysis'
        idx = description_snippet.find(marker)
        if idx >= 0:
            description_snippet = description_snippet[idx:]
        description_snippet = _cap(description_snippet, 1_500)
        lines.append("### What Was Fixed\n")
        lines.append(f"```\n{description_snippet}\n```\n")

    lines.append(f"**Fix commit (on main):** `{commit_sha}`\n")
    lines.append(
        "**Environment files touched:** "
        + ', '.join(f'`{d.path}`' for d in divergences)
        + "\n"
    )

    if all_identical:
        lines.append(
            "### ✅ No Action Required\n\n"
            "This worktree's copies of the changed files are **identical** to "
            "the fixed version on main.  No merge or rebase is needed.\n"
        )
        return '\n'.join(lines)

    if not has_conflicts:
        # All changed files in the worktree either match main or are missing
        # (i.e. main added them).  Clean merge case.
        lines.append(
            "### 🔄 Clean Merge Available\n\n"
            "This worktree's environment files do not show a conflicting local "
            "constraint.  To apply the fix, rebase or merge the main branch "
            "into this worktree's feature branch:\n\n"
            "```bash\n"
            "git fetch origin\n"
            "git rebase origin/<default-branch>\n"
            "# or: git merge origin/<default-branch>\n"
            "```\n"
        )
    else:
        lines.append(
            "### ❌ Manual Reconciliation Required\n\n"
            "This worktree has **local changes that conflict with the fix** on "
            "main.  Do **not** blindly pull or rebase — the worktree's own "
            "constraint may be intentional (e.g. an upper-bound pin for a "
            "different reason).  Please review the differences below and "
            "reconcile manually.\n"
        )

    # Per-file detail.
    for d in divergences:
        lines.append(f"\n---\n\n#### `{d.path}`\n")
        if d.identical:
            lines.append("_Identical to main — no action needed._\n")
        elif d.only_in_main:
            content_snippet = _cap(d.main_content or '', _FILE_CONTENT_CAP)
            lines.append(
                "_File exists on main but **not** in this worktree.  "
                "A rebase/merge will add it._\n\n"
                f"<details><summary>Main's version</summary>\n\n"
                f"```\n{content_snippet}\n```\n\n</details>\n"
            )
        elif d.only_in_worktree:
            content_snippet = _cap(d.worktree_content or '', _FILE_CONTENT_CAP)
            lines.append(
                "_File exists in this worktree but **not** on main.  "
                "Check whether it should be removed or merged._\n\n"
                f"<details><summary>Worktree's version</summary>\n\n"
                f"```\n{content_snippet}\n```\n\n</details>\n"
            )
        else:
            # Both exist but differ.
            diff_text = _unified_diff(
                f'main/{d.path}', d.main_content or '',
                f'worktree/{d.path}', d.worktree_content or '',
            )
            lines.append(
                "<details><summary>Diff (main → worktree)</summary>\n\n"
                f"```diff\n{diff_text}\n```\n\n</details>\n"
            )
            main_snippet = _cap(d.main_content or '', _FILE_CONTENT_CAP)
            worktree_snippet = _cap(d.worktree_content or '', _FILE_CONTENT_CAP)
            lines.append(
                "<details><summary>Main's version (fixed)</summary>\n\n"
                f"```\n{main_snippet}\n```\n\n</details>\n"
            )
            lines.append(
                "<details><summary>This worktree's version</summary>\n\n"
                f"```\n{worktree_snippet}\n```\n\n</details>\n"
            )

    return '\n'.join(lines)


def _build_auto_applied_comment(
    commit_sha: str,
    new_commit_sha: str,
    changed_files: List[str],
    env_fix_description: str,
) -> str:
    """Build the comment posted after a successful auto-apply.

    This is intentionally different from the Phase 1 "Clean Merge Available"
    comment: it tells the owner the fix has *already been applied* so no
    manual action is needed.
    """
    lines: List[str] = []
    lines.append("## ✅ Dev Environment Fix Auto-Applied to This Branch\n")
    lines.append(
        "The `dev_environment_verifier` approved an environment fix on the "
        "main branch.  Because this worktree had **no conflicting local "
        "constraints**, the fix was automatically applied and pushed "
        "to this branch — **no action is required from you**.\n"
    )

    if env_fix_description:
        description_snippet = env_fix_description.strip()
        marker = 'Problem Analysis'
        idx = description_snippet.find(marker)
        if idx >= 0:
            description_snippet = description_snippet[idx:]
        description_snippet = _cap(description_snippet, 1_500)
        lines.append("### What Was Fixed\n")
        lines.append(f"```\n{description_snippet}\n```\n")

    lines.append(f"**Original fix commit (on main):** `{commit_sha}`\n")
    lines.append(f"**Auto-applied commit on this branch:** `{new_commit_sha}`\n")
    lines.append(
        "**Environment files updated:** "
        + ', '.join(f'`{f}`' for f in changed_files)
        + "\n"
    )
    lines.append(
        "_If you believe this auto-apply was incorrect, revert the commit "
        "above and reconcile manually._\n"
    )
    return '\n'.join(lines)


# Per-worktree asyncio locks: keyed by str(worktree_path).
# The try-only pattern (``if lock.locked(): skip``) prevents two concurrent
# auto-apply coroutines from racing on the same worktree.  The dict is
# module-level so the same lock instance is shared across all coroutines in the
# process lifetime.  asyncio.Lock() is safe to create before any event loop is
# running (Python 3.10+).
_worktree_apply_locks: Dict[str, asyncio.Lock] = {}


def _get_worktree_apply_lock(worktree_path: Path) -> asyncio.Lock:
    """Return (creating if needed) the asyncio.Lock for ``worktree_path``."""
    key = str(worktree_path)
    if key not in _worktree_apply_locks:
        _worktree_apply_locks[key] = asyncio.Lock()
    return _worktree_apply_locks[key]


def _apply_env_fix_to_worktree(
    worktree_path: Path,
    commit_sha: str,
    changed_files: List[str],
) -> Optional[str]:
    """Apply each env file from the fix commit to the worktree branch and push.

    Uses targeted per-file checkout (``git checkout <sha> -- <file>``) for
    each env file rather than cherry-picking the whole commit.  This ensures
    only the environment files are touched and avoids applying unrelated code
    changes from the same commit.

    This function is **synchronous** and is intended to be run via
    ``asyncio.to_thread()`` so the event loop is not blocked.

    Push safeguards (mirrors ``git_workflow_manager.sync_epic_worktree_before_commit``):
    1. Fetch origin with a timeout.
    2. Reject dirty working trees.
    3. Reject worktrees that are ahead of the remote branch (agent may have
       committed — do not clobber unpushed work).
    4. Reset clean behind-only worktrees to the remote ref before applying, so
       the push is a simple fast-forward.
    5. On push failure roll back the local commit **only if HEAD is still the
       commit we made** — if the agent committed on top of us in the meantime
       we leave HEAD untouched rather than deleting the agent's work.

    All cleanup (unstage/restore/clean) is **scoped to ``changed_files``
    only** so that an agent's in-progress edits to other files are never
    touched, even in the failure path.

    Returns the new commit SHA on success, ``None`` on any failure.
    """
    # -- fetch so we have fresh remote refs (network call — needs timeout) ---
    _run_git(['fetch', 'origin'], cwd=worktree_path, timeout=30)
    # Fetch failure is non-fatal: proceed; if the branch state is stale the
    # ahead/behind checks will use whatever refs we have and the push will
    # reject the attempt safely.

    # -- determine the current branch name ----------------------------------
    rc, branch_out, _ = _run_git(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=worktree_path)
    if rc != 0 or not branch_out.strip() or branch_out.strip() == 'HEAD':
        logger.warning(
            "Cannot determine branch for worktree %s (detached HEAD?); skipping auto-apply",
            worktree_path,
        )
        return None
    branch = branch_out.strip()
    remote_ref = f'origin/{branch}'

    # -- safety: reject dirty working tree ----------------------------------
    rc_status, status_out, _ = _run_git(['status', '--porcelain'], cwd=worktree_path)
    if rc_status != 0:
        logger.warning("git status failed in worktree %s; skipping auto-apply", worktree_path)
        return None
    if status_out.strip():
        logger.warning(
            "Worktree %s has uncommitted changes; skipping auto-apply to avoid data loss",
            worktree_path,
        )
        return None

    # -- check ahead/behind relative to remote ------------------------------
    rc_ahead, ahead_out, _ = _run_git(
        ['rev-list', '--count', f'{remote_ref}..HEAD'],
        cwd=worktree_path,
    )
    if rc_ahead == 0 and ahead_out.strip().isdigit() and int(ahead_out.strip()) > 0:
        logger.warning(
            "Worktree %s is %s commit(s) ahead of %s; "
            "an agent may be working there — skipping auto-apply",
            worktree_path, ahead_out.strip(), remote_ref,
        )
        return None

    # -- if behind (and not ahead, not dirty) reset to remote ---------------
    rc_behind, behind_out, _ = _run_git(
        ['rev-list', '--count', f'HEAD..{remote_ref}'],
        cwd=worktree_path,
    )
    if rc_behind == 0 and behind_out.strip().isdigit() and int(behind_out.strip()) > 0:
        rc_reset, _, reset_err = _run_git(
            ['reset', '--hard', remote_ref],
            cwd=worktree_path,
        )
        if rc_reset != 0:
            logger.warning(
                "Failed to reset worktree %s to %s before env-fix apply: %s",
                worktree_path, remote_ref, reset_err.strip(),
            )
            return None
        logger.info(
            "Reset clean worktree %s to %s before applying env fix",
            worktree_path, remote_ref,
        )

    # -- record HEAD before we make any commits so we can roll back safely --
    # We use this SHA to verify that we only undo *our* commit on push
    # failure, not a commit made by an agent that raced us.
    rc_pre, pre_apply_sha, _ = _run_git(['rev-parse', 'HEAD'], cwd=worktree_path)
    if rc_pre != 0 or not pre_apply_sha.strip():
        logger.warning(
            "Could not read HEAD SHA for worktree %s before apply; skipping",
            worktree_path,
        )
        return None
    pre_apply_sha = pre_apply_sha.strip()

    def _cleanup_env_files_only() -> None:
        """Unstage and restore only the env files we touched.

        Deliberately scoped to ``changed_files`` so that an agent's
        in-progress edits to other files are never reverted.
        """
        # Unstage any staged changes for our files only.
        _run_git(['reset', 'HEAD', '--'] + changed_files, cwd=worktree_path)
        # Restore tracked env files to their HEAD state (pre-checkout).
        _run_git(['checkout', '--'] + changed_files, cwd=worktree_path)
        # Remove untracked env files that were added by the checkout (e.g. a
        # brand-new requirements.txt that the fix commit introduced).
        for rel_path in changed_files:
            full_path = worktree_path / rel_path
            if full_path.exists():
                # Only remove if not tracked at HEAD (it's an untracked new file).
                rc_ls, _, _ = _run_git(
                    ['ls-files', '--error-unmatch', rel_path], cwd=worktree_path
                )
                if rc_ls != 0:
                    try:
                        full_path.unlink()
                    except OSError:
                        pass

    # -- stage the env files from the fix commit ----------------------------
    staged_files: List[str] = []
    for rel_path in changed_files:
        rc_co, _, co_err = _run_git(
            ['checkout', commit_sha, '--', rel_path],
            cwd=worktree_path,
        )
        if rc_co != 0:
            logger.warning(
                "Could not check out %s from %s in worktree %s: %s",
                rel_path, commit_sha[:12], worktree_path, co_err.strip(),
            )
            _cleanup_env_files_only()
            return None
        staged_files.append(rel_path)

    # -- verify something was actually staged --------------------------------
    rc_diff, diff_out, _ = _run_git(['diff', '--cached', '--name-only'], cwd=worktree_path)
    if rc_diff != 0 or not diff_out.strip():
        logger.info(
            "No staged changes after applying env files to %s "
            "(files already up to date); nothing to commit",
            worktree_path,
        )
        _cleanup_env_files_only()
        return None

    # -- commit the staged changes ------------------------------------------
    commit_msg = (
        f"chore: auto-apply env fix from {commit_sha[:12]} (worktree_env_notification)\n\n"
        f"Automatically applied by worktree_env_notification Phase 2.\n"
        f"Source fix commit: {commit_sha}\n"
        f"Files: {', '.join(changed_files)}"
    )
    rc_commit, _, commit_err = _run_git(
        ['commit', '-m', commit_msg],
        cwd=worktree_path,
    )
    if rc_commit != 0:
        logger.warning(
            "git commit failed in worktree %s: %s", worktree_path, commit_err.strip()
        )
        _cleanup_env_files_only()
        return None

    # Read the SHA of the commit we just made so we can compare it later.
    rc_our, our_sha_out, _ = _run_git(['rev-parse', 'HEAD'], cwd=worktree_path)
    our_commit_sha = our_sha_out.strip() if (rc_our == 0 and our_sha_out.strip()) else None

    # -- push to remote (network call — needs timeout) ----------------------
    rc_push, _, push_err = _run_git(
        ['push', 'origin', f'{branch}:{branch}'],
        cwd=worktree_path,
        timeout=30,
    )
    if rc_push != 0:
        logger.warning(
            "git push failed for worktree %s branch %s: %s",
            worktree_path, branch, push_err.strip(),
        )
        # Roll back only if HEAD is still our commit — if an agent committed
        # on top of us in the window between our commit and the push failure,
        # do not delete the agent's work.
        if our_commit_sha:
            rc_cur, cur_sha_out, _ = _run_git(['rev-parse', 'HEAD'], cwd=worktree_path)
            current_sha = cur_sha_out.strip() if rc_cur == 0 else None
            if current_sha and current_sha == our_commit_sha:
                # HEAD is still our commit — safe to roll back to pre-apply state.
                rc_rb, _, rb_err = _run_git(
                    ['reset', '--hard', pre_apply_sha],
                    cwd=worktree_path,
                )
                if rc_rb != 0:
                    logger.warning(
                        "Rollback to %s failed for worktree %s: %s",
                        pre_apply_sha[:12], worktree_path, rb_err.strip(),
                    )
            else:
                logger.info(
                    "HEAD moved since our commit in worktree %s "
                    "(agent may have committed); skipping rollback",
                    worktree_path,
                )
        return None

    # -- retrieve the new commit SHA ----------------------------------------
    rc_sha, sha_out, _ = _run_git(['rev-parse', 'HEAD'], cwd=worktree_path)
    if rc_sha != 0 or not sha_out.strip():
        logger.warning(
            "Push succeeded but could not read HEAD SHA for worktree %s; "
            "falling back to comment-only",
            worktree_path,
        )
        return None
    return sha_out.strip()


async def notify_active_worktrees_of_env_fix(
    project_name: str,
    env_fix_description: str,
) -> None:
    """Post a GitHub comment on each active worktree's epic issue.

    Called by ``DevEnvironmentVerifierAgent`` immediately after recording
    ``DevContainerStatus.VERIFIED``.  Errors are logged but never raised so
    they cannot affect the verification result that was already persisted.

    Args:
        project_name: The project whose dev container was just verified.
        env_fix_description: The ``previous_stage_output`` from the
            ``dev_environment_setup`` agent — used as the "what was fixed"
            description in the comment.
    """
    try:
        await _notify_active_worktrees_of_env_fix(project_name, env_fix_description)
    except Exception:
        logger.exception(
            "notify_active_worktrees_of_env_fix failed for %s; "
            "verification result is unaffected",
            project_name,
        )


def _get_workspace_manager():
    """Late-bound accessor so tests can patch this function or swap the module attr."""
    # Imported here to avoid a circular import at module load time.
    from services.project_workspace import workspace_manager as _wm  # noqa: PLC0415
    return _wm


def _get_config_manager():
    """Late-bound accessor for the config manager."""
    from config.manager import config_manager as _cm  # noqa: PLC0415
    return _cm


def _make_github_integration(repo_owner, repo_name):
    """Late-bound factory so tests can patch this function."""
    from services.github_integration import GitHubIntegration  # noqa: PLC0415
    return GitHubIntegration(repo_owner=repo_owner, repo_name=repo_name)


async def _notify_active_worktrees_of_env_fix(
    project_name: str,
    env_fix_description: str,
) -> None:
    """Inner implementation -- see ``notify_active_worktrees_of_env_fix``."""
    _workspace_manager = _get_workspace_manager()

    # 1. Locate the base clone (no epic_id → shared base clone path).
    base_clone = _workspace_manager.get_project_dir(project_name)
    # Verify the base clone is a live git repository before proceeding.
    rc_base, _, _ = _run_git(['rev-parse', '--git-dir'], cwd=base_clone)
    if rc_base != 0:
        logger.debug(
            "Base clone for %s at %s is not a valid git working tree; "
            "skipping worktree notifications",
            project_name, base_clone,
        )
        return

    # 2. Find the most recent env-fix commit and the files it touched.
    fix_info = _get_env_fix_commit(base_clone)
    if not fix_info:
        logger.debug(
            "No env-touching commit found in the last %d commits for %s; "
            "skipping worktree notifications",
            _COMMIT_SEARCH_DEPTH, project_name,
        )
        return

    commit_sha, changed_files = fix_info
    logger.info(
        "Env-fix commit %s changed %d env file(s) for %s: %s",
        commit_sha[:12], len(changed_files), project_name, changed_files,
    )

    # Compute the parent of the fix commit for three-way divergence comparison.
    # If None (initial commit / git error), _check_file_divergence falls back
    # to the two-way comparison from Phase 1.
    pre_fix_sha = _get_pre_fix_sha(base_clone, commit_sha)

    # 3. Enumerate active worktrees -- filter to active_run_protected is True.
    worktrees = _workspace_manager.survey_epic_worktrees(project_name)
    active = [w for w in worktrees if w.get('active_run_protected') is True]
    if not active:
        logger.debug(
            "No active-run-protected worktrees for %s; skipping notifications",
            project_name,
        )
        return

    # 4. Set up GitHub integration for this project.
    try:
        project_config = _get_config_manager().get_project_config(project_name)
        repo_owner = (
            project_config.github.get('org')
            if project_config and hasattr(project_config, 'github')
            else None
        )
        repo_name = (
            project_config.github.get('repo')
            if project_config and hasattr(project_config, 'github')
            else None
        )
    except Exception:
        logger.exception(
            "Could not load project config for %s; skipping worktree notifications",
            project_name,
        )
        return

    if repo_owner is None or repo_name is None:
        logger.warning(
            "Project config for %s is missing 'org' or 'repo' under github; "
            "skipping worktree notifications",
            project_name,
        )
        return

    github = _make_github_integration(repo_owner, repo_name)

    # 5. Post exactly one comment per epic (deduplicate by epic_id).
    seen_epic_ids: Set[str] = set()
    for worktree in active:
        epic_id = worktree.get('epic_id')
        worktree_path_str = worktree.get('path')
        if not epic_id or not worktree_path_str:
            continue
        if epic_id in seen_epic_ids:
            continue
        seen_epic_ids.add(epic_id)

        try:
            issue_number = int(epic_id)
        except (ValueError, TypeError):
            logger.warning(
                "Cannot convert epic_id %r to an issue number for %s; skipping",
                epic_id, project_name,
            )
            continue

        worktree_path = Path(worktree_path_str)
        # Use git to verify the path is a live git working tree rather than
        # just checking Path.exists() -- a directory can exist without being
        # a valid worktree (e.g. a stale directory after pruning).
        rc, _, _ = _run_git(['rev-parse', '--git-dir'], cwd=worktree_path)
        if rc != 0:
            logger.debug(
                "Worktree path %s for epic %s is not a valid git working tree; skipping",
                worktree_path, epic_id,
            )
            continue

        divergences = _check_file_divergence(
            base_clone, worktree_path, changed_files, pre_fix_sha=pre_fix_sha,
        )
        has_conflicts, all_identical = _classify_divergence(divergences)
        # Note: divergences reflects the worktree state at this moment.  The
        # apply function re-reads the live tree (including fetching and
        # potentially resetting to origin/<branch>), so if the worktree moves
        # between here and the apply call the apply handles it gracefully:
        # already-up-to-date files produce no staged changes and the function
        # returns None → Phase 1 fallback.  The divergences object is used for
        # the comment body regardless of which code path is taken.

        if all_identical or has_conflicts:
            # Already aligned or conflicting local constraint: Phase 1 comment-only.
            # Never auto-apply when has_conflicts is True.
            comment = _build_comment(commit_sha, divergences, env_fix_description)
        else:
            # Clean case: attempt auto-apply (Phase 2).
            # Try-only lock: if another coroutine is already applying the fix
            # to this worktree, fall back to Phase 1's comment immediately
            # rather than waiting or racing.
            apply_lock = _get_worktree_apply_lock(worktree_path)
            if apply_lock.locked():
                logger.info(
                    "Auto-apply already in progress for worktree %s (epic %s); "
                    "falling back to Phase 1 comment",
                    worktree_path, epic_id,
                )
                comment = _build_comment(commit_sha, divergences, env_fix_description)
            else:
                # No await between the locked() check and the acquire below, so
                # no other coroutine can interleave and steal the lock.
                async with apply_lock:
                    logger.info(
                        "Attempting auto-apply of env-fix %s to worktree %s (epic %s)",
                        commit_sha[:12], worktree_path, epic_id,
                    )
                    new_sha = await asyncio.to_thread(
                        _apply_env_fix_to_worktree,
                        worktree_path, commit_sha, changed_files,
                    )
                if new_sha:
                    logger.info(
                        "Auto-applied env-fix to worktree %s; new commit %s",
                        worktree_path, new_sha[:12],
                    )
                    comment = _build_auto_applied_comment(
                        commit_sha, new_sha, changed_files, env_fix_description,
                    )
                else:
                    # Auto-apply failed — fall back to Phase 1's comment.
                    logger.warning(
                        "Auto-apply failed for worktree %s (epic %s); "
                        "falling back to Phase 1 comment",
                        worktree_path, epic_id,
                    )
                    comment = _build_comment(commit_sha, divergences, env_fix_description)

        try:
            await github.post_issue_comment(issue_number, comment)
            logger.info(
                "Posted env-fix notification on issue #%d (epic %s) for %s",
                issue_number, epic_id, project_name,
            )
        except Exception:
            logger.exception(
                "Failed to post env-fix notification on issue #%d for %s",
                issue_number, project_name,
            )
