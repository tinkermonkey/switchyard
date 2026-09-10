"""
Tests for ProjectWorkspaceManager.get_or_create_epic_worktree()'s project_checkout
lock coverage (issue #151/WI-6 item 1, from #140 item 1).

_add_epic_worktree() is a base-clone WRITER -- `git fetch` into the base clone's
refs, a `worktree add` that registers the new worktree in the base clone's own
.git/worktrees/, plus a detach (_free_branch_from_base_clone) and a push
(_push_stray_branch_if_ahead) from it. Nothing gated it, because every other
base-clone call site decides whether to lock from its FINAL resolved directory
(is_base_clone_dir()), which here is the new worktree path -- by definition never
the base clone. So creating an epic worktree could run concurrently with the
startup clone/update, a base-clone-scoped container run, or auto_commit's
add/commit/push against that same .git.

Covers:
- the lock is acquired BEFORE any git command touches the base clone and released
  after, scoped to that project, with the short (120s) acquire timeout chosen for
  this call site rather than project_checkout_lock's ~3h default;
- the paths that touch nothing shared -- a cache hit, a restart adoption, the
  corruption/missing-branch_name refusals -- never take it;
- a genuinely contended base clone makes the creation FAIL LOUD
  (ProjectCheckoutLockTimeoutError) with no git run and nothing tracked, rather
  than proceeding unlocked.

All git operations are mocked (subprocess.run) -- no real git commands run.
"""

import shutil
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import services.project_checkout_lock as project_checkout_lock
from services.pipeline_lock_manager import PipelineLockManager
from services.project_checkout_lock import (
    ProjectCheckoutLockTimeoutError,
    RESOURCE_NAME,
)
from services.project_resource_lock_manager import ProjectResourceLockManager
from services.project_workspace import ProjectWorkspaceManager


def _ok(stdout: str = "") -> Mock:
    result = Mock()
    result.returncode = 0
    result.stdout = stdout
    result.stderr = ""
    return result


def _fail(stderr: str = "error") -> Mock:
    result = Mock()
    result.returncode = 1
    result.stdout = ""
    result.stderr = stderr
    return result


# The existing-branch creation sequence, as test_project_workspace.py documents
# it: _free_branch_from_base_clone's status/rev-parse, the fetch of the target
# branch, _push_stray_branch_if_ahead's rev-parse --verify (no local ref), then
# `worktree add`.
_EXISTING_BRANCH_SEQUENCE = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]


@pytest.fixture
def manager(tmp_path):
    return ProjectWorkspaceManager(workspace_root=tmp_path)


def _make_base_clone(workspace_root: Path, project_name: str) -> Path:
    project_dir = workspace_root / project_name
    (project_dir / '.git').mkdir(parents=True)
    return project_dir


class _RecordingLock:
    """Stand-in for project_checkout_lock_sync() that records its call and the
    order of enter/exit relative to the git commands it is meant to bracket."""

    def __init__(self, events):
        self.events = events
        self.calls = []

    def __call__(self, project, issue_number=None, **kwargs):
        self.calls.append({'project': project, 'issue_number': issue_number, **kwargs})
        events = self.events

        class _Ctx:
            def __enter__(self_inner):
                events.append('lock_acquired')
                return None

            def __exit__(self_inner, *exc):
                events.append('lock_released')
                return False

        return _Ctx()


def _yaml_only_facade(tmp_dir: str) -> ProjectResourceLockManager:
    """A real facade over PipelineLockManager's YAML-only fallback.

    redis_client is cleared explicitly after construction rather than just passed
    as None, because None makes the constructor build a real client from
    REDIS_HOST, which succeeds inside the orchestrator container -- the same
    reason test_project_checkout_lock.py's own YAML-only fixture does this.
    """
    lock_manager = PipelineLockManager(state_dir=Path(tmp_dir), redis_client=None)
    lock_manager.redis_client = None
    return ProjectResourceLockManager(lock_manager=lock_manager)


class TestCreationTakesTheLock:

    def test_lock_brackets_every_base_clone_git_command(self, manager, tmp_path):
        """THE regression: without the fix not a single git command below is
        inside a lock, so an epic-worktree creation can fetch/detach/push/register
        against a base clone another operation is mid-way through using."""
        base_clone = _make_base_clone(tmp_path, "my-project")
        events = []
        recording_lock = _RecordingLock(events)
        results = iter(_EXISTING_BRANCH_SEQUENCE)

        def _record_git(args, **kwargs):
            if str(base_clone) in args:
                events.append('git_against_base_clone')
            return next(results)

        with patch('services.project_checkout_lock.project_checkout_lock_sync', recording_lock), \
             patch('services.project_workspace.subprocess.run', side_effect=_record_git):
            manager.get_or_create_epic_worktree(
                "my-project", "100", branch_name="feature/issue-100-epic"
            )

        assert 'git_against_base_clone' in events, \
            "expected the creation path to run git against the base clone"
        assert events[0] == 'lock_acquired'
        assert events[-1] == 'lock_released'
        # Every base-clone command sits strictly between the two.
        assert all(
            0 < i < len(events) - 1
            for i, e in enumerate(events) if e == 'git_against_base_clone'
        )

    def test_lock_is_scoped_to_the_project_with_the_short_acquire_timeout(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")
        recording_lock = _RecordingLock([])

        with patch('services.project_checkout_lock.project_checkout_lock_sync', recording_lock), \
             patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = list(_EXISTING_BRANCH_SEQUENCE)
            manager.get_or_create_epic_worktree(
                "my-project", "200", branch_name="feature/issue-200-existing"
            )

        assert len(recording_lock.calls) == 1
        call = recording_lock.calls[0]
        assert call['project'] == "my-project"
        # Log attribution only -- the epic id is the nearest real GitHub issue
        # number in scope here.
        assert call['issue_number'] == 200
        # Deliberately NOT project_checkout_lock's ~3h DEFAULT_TIMEOUT_SECONDS:
        # this method is reachable from the event-loop thread.
        assert call['timeout_seconds'] == 120.0

    def test_a_non_numeric_epic_id_still_takes_the_lock(self, manager, tmp_path):
        """issue_number is log attribution only, so an epic id that isn't an int
        must degrade to None rather than skipping (or crashing) the lock."""
        _make_base_clone(tmp_path, "my-project")
        recording_lock = _RecordingLock([])

        with patch('services.project_checkout_lock.project_checkout_lock_sync', recording_lock), \
             patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = list(_EXISTING_BRANCH_SEQUENCE)
            manager.get_or_create_epic_worktree(
                "my-project", "epic-alpha", branch_name="feature/epic-alpha"
            )

        assert len(recording_lock.calls) == 1
        assert recording_lock.calls[0]['issue_number'] is None

    def test_caller_can_widen_the_acquire_timeout(self, manager, tmp_path):
        """The short default is calibrated for the event-loop-thread callers; a
        caller that knows it is off the loop can ask for a real wait."""
        _make_base_clone(tmp_path, "my-project")
        recording_lock = _RecordingLock([])

        with patch('services.project_checkout_lock.project_checkout_lock_sync', recording_lock), \
             patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = list(_EXISTING_BRANCH_SEQUENCE)
            manager.get_or_create_epic_worktree(
                "my-project", "201", branch_name="feature/issue-201",
                checkout_lock_timeout_seconds=900.0,
            )

        assert recording_lock.calls[0]['timeout_seconds'] == 900.0


class TestPathsThatTouchNothingSharedDoNotLock:

    def test_cache_hit_does_not_take_the_lock(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")
        recording_lock = _RecordingLock([])

        with patch('services.project_checkout_lock.project_checkout_lock_sync', recording_lock), \
             patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = list(_EXISTING_BRANCH_SEQUENCE)
            first = manager.get_or_create_epic_worktree(
                "my-project", "300", branch_name="feature/issue-300"
            )
            # `git worktree add` is mocked, so materialize the .git the cache-hit
            # corruption re-check stats.
            (first / '.git').mkdir(parents=True, exist_ok=True)
            recording_lock.calls.clear()

            second = manager.get_or_create_epic_worktree("my-project", "300")

        assert second == first
        assert recording_lock.calls == []

    def test_restart_adoption_does_not_take_the_lock(self, manager, tmp_path):
        """An on-disk worktree with an empty in-memory cache is adopted, not
        re-created -- it runs `rev-parse` inside the WORKTREE, never against the
        base clone."""
        _make_base_clone(tmp_path, "my-project")
        worktree_path = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '400'
        (worktree_path / '.git').mkdir(parents=True)
        recording_lock = _RecordingLock([])

        with patch('services.project_checkout_lock.project_checkout_lock_sync', recording_lock), \
             patch('services.project_workspace.subprocess.run', return_value=_ok("feature/issue-400\n")):
            adopted = manager.get_or_create_epic_worktree(
                "my-project", "400", branch_name="feature/issue-400"
            )

        assert adopted == worktree_path
        assert recording_lock.calls == []

    def test_missing_branch_name_refusal_does_not_take_the_lock(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")
        recording_lock = _RecordingLock([])

        with patch('services.project_checkout_lock.project_checkout_lock_sync', recording_lock):
            with pytest.raises(ValueError):
                manager.get_or_create_epic_worktree("my-project", "500")

        assert recording_lock.calls == []


class TestContendedBaseCloneFailsLoud:
    """End-to-end through the REAL lock (a facade over PipelineLockManager's
    YAML-only fallback), not a stand-in -- the point is that a base clone
    genuinely held by somebody else stops the creation dead."""

    def setup_method(self):
        self.test_dir = tempfile.mkdtemp()
        self.facade = _yaml_only_facade(self.test_dir)

    def teardown_method(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_creation_fails_loud_and_runs_no_git_while_the_base_clone_is_held(
        self, manager, tmp_path
    ):
        _make_base_clone(tmp_path, "my-project")

        # Somebody else is mid-operation against this project's base clone.
        can_execute, _reason = self.facade.acquire_resource("my-project", RESOURCE_NAME, -999)
        assert can_execute, "test setup: the other holder should have won the lock"

        with patch('services.project_checkout_lock.ProjectResourceLockManager',
                   return_value=self.facade), \
             patch('services.project_workspace.subprocess.run') as mock_run:
            with pytest.raises(ProjectCheckoutLockTimeoutError):
                manager.get_or_create_epic_worktree(
                    "my-project", "600", branch_name="feature/issue-600",
                    # 0 rather than a small positive value: the poll interval is
                    # a def-time default (5s) with no seam here, so any timeout
                    # that survives the first failed attempt costs a real sleep.
                    checkout_lock_timeout_seconds=0.0,
                )

        # Nothing was fetched, detached, pushed or registered...
        mock_run.assert_not_called()
        # ...and the epic is not tracked as if it had a worktree.
        assert ("my-project", "600") not in manager._epic_worktrees

    def test_creation_proceeds_once_the_other_holder_releases(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")

        can_execute, _reason = self.facade.acquire_resource("my-project", RESOURCE_NAME, -998)
        assert can_execute

        def _release_soon():
            self.facade.release_resource("my-project", RESOURCE_NAME, -998)

        releaser = threading.Timer(0.2, _release_soon)
        releaser.start()
        try:
            with patch('services.project_checkout_lock.ProjectResourceLockManager',
                       return_value=self.facade), \
                 patch('services.project_workspace.subprocess.run') as mock_run:
                mock_run.side_effect = list(_EXISTING_BRANCH_SEQUENCE)
                result = manager.get_or_create_epic_worktree(
                    "my-project", "601", branch_name="feature/issue-601",
                    checkout_lock_timeout_seconds=30.0,
                )
        finally:
            releaser.cancel()

        assert result == tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '601'
        # Released again on the way out, so the next epic isn't blocked.
        held, _reason = self.facade.acquire_resource("my-project", RESOURCE_NAME, -997)
        assert held
