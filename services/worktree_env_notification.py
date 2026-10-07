"""Post-VERIFIED worktree environment-fix notifications.

After ``dev_environment_verifier`` records ``DevContainerStatus.VERIFIED``,
this module enumerates every active epic worktree for the project and posts a
single GitHub comment on the issue that owns each worktree.

The comment tells the worktree's owner:

* What was fixed on the main branch and a reference to the fix commit.
* Whether the worktree's copy of those environment files is identical to
  main's fixed version (already aligned), behind but clean (just needs a
  rebase/merge), or diverged with a local conflicting constraint (needs
  manual reconciliation -- NOT a blind pull).

Phase 1: **comment-only**.  No automated commits, cherry-picks, or merges
are performed here, even when the worktree is clean.  Automated application
for the clean case is tracked as Phase 2 (separate ticket).

Acceptance criteria (from the issue):

- Exactly one comment is posted per currently-active worktree (not per-run,
  not duplicated on retries -- ``active_run_protected is True`` is the gate).
- Worktrees with ``UNKNOWN`` or ``UNOWNED`` ownership receive no comment.
- The comment correctly distinguishes clean from conflicting.
- No existing dev-environment setup/verification behaviour is changed.
"""
from __future__ import annotations

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


def _run_git(args: List[str], cwd: Path, *, check: bool = False) -> Tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            ['git'] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, ['git'] + args,
                output=result.stdout, stderr=result.stderr,
            )
        return result.returncode, result.stdout, result.stderr
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


def _read_file_at_head(repo_dir: Path, relative_path: str) -> Optional[str]:
    """Read the content of ``relative_path`` from HEAD of ``repo_dir`` via git.

    Returns ``None`` when the file does not exist at HEAD in that repo.
    """
    rc, content, _ = _run_git(
        ['show', f'HEAD:{relative_path}'],
        cwd=repo_dir,
    )
    return content if rc == 0 else None


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
    ) -> None:
        self.path = path
        self.main_content = main_content
        self.worktree_content = worktree_content

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


def _check_file_divergence(
    base_clone: Path,
    worktree_path: Path,
    changed_files: List[str],
) -> List[_FileDivergence]:
    """For each env file touched by the fix, compare main vs worktree."""
    results: List[_FileDivergence] = []
    for rel_path in changed_files:
        main_content = _read_file_at_head(base_clone, rel_path)
        worktree_content = _read_file_at_head(worktree_path, rel_path)
        results.append(_FileDivergence(rel_path, main_content, worktree_content))
    return results


def _cap(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit] + '\n... (truncated)'
    return text


def _build_comment(
    commit_sha: str,
    divergences: List[_FileDivergence],
    env_fix_description: str,
) -> str:
    """Build the GitHub comment body for one worktree."""
    has_conflicts = any(
        d.diverged or d.only_in_worktree or d.only_in_main for d in divergences
    )
    all_identical = all(d.identical for d in divergences)

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
        # (i.e. main added them).  Clean pull case.
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

        divergences = _check_file_divergence(base_clone, worktree_path, changed_files)
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
