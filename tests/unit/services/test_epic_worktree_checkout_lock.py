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

This closes the CREATION side only. Epic-worktree TEARDOWN --
cleanup_epic_worktree() and prune_epic_worktrees(), both of which run `git -C
<base clone> worktree remove --force` against that same .git/worktrees/ -- is
still an unlocked base-clone writer, tracked in #169.

Covers:
- the lock is acquired BEFORE any git command touches the base clone and released
  after (including when the guarded body itself fails), scoped to that project;
- the acquire budget: the full calibrated wait when the dispatching issue is
  known (so project_checkout_lock's activity registry can exempt the waiting run
  from the watchdog), a capped one when it isn't, and a single non-blocking
  attempt when the caller is on the event-loop thread;
- the paths that touch nothing shared -- a cache hit, a restart adoption, the
  corruption/missing-branch_name refusals -- never take it;
- a genuinely contended base clone makes the creation FAIL LOUD
  (ProjectCheckoutLockTimeoutError) with no git run and nothing tracked, rather
  than proceeding unlocked;
- one epic's contended creation does not block another epic's (or another
  project's) resolution.

All git operations are mocked (subprocess.run) -- no real git commands run.
"""

import asyncio
import logging
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import services.project_checkout_lock as project_checkout_lock
from services.pipeline_lock_manager import PipelineLockManager
from services.project_checkout_lock import (
    DEFAULT_TIMEOUT_SECONDS,
    ProjectCheckoutLockTimeoutError,
    RESOURCE_NAME,
)
from services.project_workspace import UNATTRIBUTED_CHECKOUT_LOCK_TIMEOUT_SECONDS
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

    def test_lock_is_scoped_to_the_project_and_falls_back_to_the_epic_id(self, manager, tmp_path):
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
        # With no dispatching issue supplied the epic id stands in -- correct for
        # planning_design, where the board item IS the epic.
        assert call['issue_number'] == 200
        # ...and an attributed wait gets the full calibrated budget, because
        # project_checkout_lock publishes it under that key and pipeline_watchdog
        # reads that registry before reaping a containerless run.
        assert call['timeout_seconds'] == DEFAULT_TIMEOUT_SECONDS

    def test_the_dispatching_issue_beats_the_epic_id_for_attribution(self, manager, tmp_path):
        """The wait's registry key is what exempts a run from the watchdog, and
        the run at risk is the SUB-issue's, not the epic's (#151/WI-6 review)."""
        _make_base_clone(tmp_path, "my-project")
        recording_lock = _RecordingLock([])

        with patch('services.project_checkout_lock.project_checkout_lock_sync', recording_lock), \
             patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = list(_EXISTING_BRANCH_SEQUENCE)
            manager.get_or_create_epic_worktree(
                "my-project", "200", branch_name="feature/issue-200-existing",
                issue_number=207,
            )

        assert recording_lock.calls[0]['issue_number'] == 207
        assert recording_lock.calls[0]['timeout_seconds'] == DEFAULT_TIMEOUT_SECONDS

    def test_an_unattributable_wait_is_capped_below_the_zombie_threshold(self, manager, tmp_path):
        """No issue number anywhere -> nothing publishes the wait, so nothing
        vouches for the waiting run. The budget has to stay well under
        pipeline_watchdog's 30-minute zombie threshold or the run gets reaped and
        redispatched while this thread is still waiting."""
        _make_base_clone(tmp_path, "my-project")
        recording_lock = _RecordingLock([])

        with patch('services.project_checkout_lock.project_checkout_lock_sync', recording_lock), \
             patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = list(_EXISTING_BRANCH_SEQUENCE)
            manager.get_or_create_epic_worktree(
                "my-project", "epic-alpha", branch_name="feature/epic-alpha"
            )

        assert recording_lock.calls[0]['issue_number'] is None
        assert recording_lock.calls[0]['timeout_seconds'] == (
            UNATTRIBUTED_CHECKOUT_LOCK_TIMEOUT_SECONDS
        )
        assert UNATTRIBUTED_CHECKOUT_LOCK_TIMEOUT_SECONDS < 30 * 60

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

    def test_caller_can_override_the_acquire_timeout(self, manager, tmp_path):
        """An off-loop caller that wants a different budget than the calibrated
        default can say so."""
        _make_base_clone(tmp_path, "my-project")
        recording_lock = _RecordingLock([])

        with patch('services.project_checkout_lock.project_checkout_lock_sync', recording_lock), \
             patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = list(_EXISTING_BRANCH_SEQUENCE)
            manager.get_or_create_epic_worktree(
                "my-project", "201", branch_name="feature/issue-201",
                checkout_lock_timeout_seconds=300.0,
            )

        assert recording_lock.calls[0]['timeout_seconds'] == 300.0


class TestOnTheEventLoopThreadItRefusesToPoll:
    """#151/WI-6 review. project_checkout_lock_sync()'s poll loop is
    time.sleep(), and EVERY in-process holder of this lock releases from a
    coroutine on the event loop (claude_integration's `async with
    project_checkout_lock_async` around a container run, auto_commit,
    finalize_feature_branch_work). So a poll on the loop thread does not merely
    stall other coroutines for its duration -- it starves the very holder it is
    waiting for, and the wait is guaranteed to fail after freezing the whole
    orchestrator for its full budget.

    Every production caller now hops off the loop first (asyncio.to_thread);
    these tests pin the backstop for one that doesn't."""

    def test_a_caller_on_the_loop_gets_a_single_non_blocking_attempt(self, manager, tmp_path, caplog):
        _make_base_clone(tmp_path, "my-project")
        recording_lock = _RecordingLock([])

        async def _from_the_loop():
            manager.get_or_create_epic_worktree(
                "my-project", "700", branch_name="feature/issue-700",
                issue_number=701,
            )

        with patch('services.project_checkout_lock.project_checkout_lock_sync', recording_lock), \
             patch('services.project_workspace.subprocess.run') as mock_run, \
             caplog.at_level(logging.ERROR, logger='services.project_workspace'):
            mock_run.side_effect = list(_EXISTING_BRANCH_SEQUENCE)
            asyncio.run(_from_the_loop())

        assert recording_lock.calls[0]['timeout_seconds'] == 0.0, \
            "an on-loop caller must not be allowed to poll with time.sleep()"
        assert any('event-loop thread' in r.message for r in caplog.records), \
            "the caller bug must be logged loudly, not silently degraded"

    def test_an_explicit_budget_does_not_buy_an_on_loop_caller_a_wait(self, manager, tmp_path):
        """The clamp overrides checkout_lock_timeout_seconds: an on-loop wait is
        not merely undesirable, it cannot succeed."""
        _make_base_clone(tmp_path, "my-project")
        recording_lock = _RecordingLock([])

        async def _from_the_loop():
            manager.get_or_create_epic_worktree(
                "my-project", "702", branch_name="feature/issue-702",
                checkout_lock_timeout_seconds=600.0,
            )

        with patch('services.project_checkout_lock.project_checkout_lock_sync', recording_lock), \
             patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = list(_EXISTING_BRANCH_SEQUENCE)
            asyncio.run(_from_the_loop())

        assert recording_lock.calls[0]['timeout_seconds'] == 0.0

    def test_off_the_loop_the_full_wait_is_restored(self, manager, tmp_path):
        """The clamp keys on a RUNNING loop on THIS thread, so the ordinary
        asyncio.to_thread call sites are unaffected."""
        _make_base_clone(tmp_path, "my-project")
        recording_lock = _RecordingLock([])

        async def _via_to_thread():
            await asyncio.to_thread(
                manager.get_or_create_epic_worktree,
                "my-project", "703", "feature/issue-703",
            )

        with patch('services.project_checkout_lock.project_checkout_lock_sync', recording_lock), \
             patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = list(_EXISTING_BRANCH_SEQUENCE)
            asyncio.run(_via_to_thread())

        assert recording_lock.calls[0]['timeout_seconds'] == DEFAULT_TIMEOUT_SECONDS


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

    def test_creation_proceeds_once_an_off_thread_holder_releases(self, manager, tmp_path):
        """Deliberately an OS-thread holder, which is what this method's callers
        actually contend with once they all reach it off the event loop
        (asyncio.to_thread workers, and initialize_project() at startup before any
        loop exists). It is NOT a claim that an in-process ASYNC holder can be
        waited out from the loop thread -- it cannot; see
        TestOnTheEventLoopThreadItRefusesToPoll for what happens there instead."""
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

    def test_a_failure_inside_the_guarded_body_still_releases_the_lock(self, manager, tmp_path):
        """_add_epic_worktree() has a documented, realistic raise path (`git
        worktree add` refusing "already used by worktree at ...", plus its
        subprocess timeouts) and IS the whole guarded body. If the release ever
        regressed to an explicit acquire/release, that failure would leave the
        base-clone lock held with a live heartbeat refreshing it -- every
        subsequent base-clone operation for this project would then block until
        its own timeout, recovered only by a process restart."""
        _make_base_clone(tmp_path, "my-project")

        with patch('services.project_checkout_lock.ProjectResourceLockManager',
                   return_value=self.facade), \
             patch('services.project_workspace.subprocess.run',
                   side_effect=[_ok(), _ok(), _ok(), _fail("no local ref"),
                                _fail("already used by worktree at ...")]):
            with pytest.raises(RuntimeError):
                manager.get_or_create_epic_worktree(
                    "my-project", "602", branch_name="feature/issue-602",
                )

        # The lock is free again despite the raise.
        held, reason = self.facade.acquire_resource("my-project", RESOURCE_NAME, -997)
        assert held, f"the lock was not released on the failure path: {reason}"
        # ...and the epic is not tracked as if it had a worktree.
        assert ("my-project", "602") not in manager._epic_worktrees


class TestContentionIsScopedToTheEpic:
    """#151/WI-6 review: the check-then-create used to run under ONE process-wide
    threading.Lock shared by every project and every epic, with the
    project_checkout wait inside it. One project's contended creation therefore
    blocked every other project's worktree resolution -- including cache hits
    that are otherwise a single dict lookup."""

    def test_a_stalled_creation_does_not_block_another_epics_cache_hit(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")

        # Epic 800 already resolved and cached -- the fast path.
        with patch('services.project_checkout_lock.project_checkout_lock_sync',
                   _RecordingLock([])), \
             patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = list(_EXISTING_BRANCH_SEQUENCE)
            cached = manager.get_or_create_epic_worktree(
                "my-project", "800", branch_name="feature/issue-800"
            )
        (cached / '.git').mkdir(parents=True, exist_ok=True)

        # Epic 801 is mid-creation and parked in its lock wait.
        entered = threading.Event()
        release = threading.Event()

        class _StalledLock:
            def __call__(self, project, issue_number=None, **kwargs):
                outer = self

                class _Ctx:
                    def __enter__(self_inner):
                        entered.set()
                        release.wait(10)
                        return None

                    def __exit__(self_inner, *exc):
                        return False

                return _Ctx()

        def _stall_creation():
            with patch('services.project_checkout_lock.project_checkout_lock_sync',
                       _StalledLock()), \
                 patch('services.project_workspace.subprocess.run') as mock_run:
                mock_run.side_effect = list(_EXISTING_BRANCH_SEQUENCE)
                manager.get_or_create_epic_worktree(
                    "my-project", "801", branch_name="feature/issue-801"
                )

        staller = threading.Thread(target=_stall_creation, daemon=True)
        staller.start()
        try:
            assert entered.wait(10), "test setup: the stalled creation never reached its lock"

            started = time.monotonic()
            reused = manager.get_or_create_epic_worktree("my-project", "800")
            elapsed = time.monotonic() - started

            assert reused == cached
            assert elapsed < 2.0, (
                "an unrelated epic's cache hit waited on the stalled creation -- "
                "the check-then-create guard is process-global again"
            )
        finally:
            release.set()
            staller.join(10)

    def test_two_calls_for_the_same_epic_still_serialize(self, manager, tmp_path):
        """The per-key split must not weaken what the global lock guaranteed:
        two concurrent calls for the SAME epic still cannot both create."""
        _make_base_clone(tmp_path, "my-project")
        in_body = threading.Event()
        release = threading.Event()
        creations = []

        real_add = manager._add_epic_worktree

        def _slow_add(base_repo_dir, worktree_path, branch_name, default_branch):
            creations.append(str(worktree_path))
            in_body.set()
            release.wait(10)
            (Path(worktree_path) / '.git').mkdir(parents=True, exist_ok=True)

        with patch('services.project_checkout_lock.project_checkout_lock_sync',
                   _RecordingLock([])), \
             patch.object(manager, '_add_epic_worktree', side_effect=_slow_add):
            first = threading.Thread(
                target=manager.get_or_create_epic_worktree,
                args=("my-project", "802", "feature/issue-802"),
                daemon=True,
            )
            first.start()
            assert in_body.wait(10), "test setup: the first creation never started"

            second_result = []
            second = threading.Thread(
                target=lambda: second_result.append(
                    manager.get_or_create_epic_worktree(
                        "my-project", "802", "feature/issue-802"
                    )
                ),
                daemon=True,
            )
            second.start()
            # The second call must be blocked behind the first, not creating too.
            second.join(0.5)
            assert second.is_alive(), "a second call for the SAME epic was not serialized"

            release.set()
            first.join(10)
            second.join(10)

        assert creations == [
            str(tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '802')
        ], "the same epic was created twice"
        assert second_result == [
            tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '802'
        ]


class TestPruneDoesNotDeleteAnInFlightWorktree:
    """Code review on #151/WI-6. prune_epic_worktrees()'s per-worktree
    tracked-check used to be a genuine barrier: _epic_worktree_lock was
    get_or_create_epic_worktree()'s whole serializer, so acquiring it here
    blocked until any concurrent adoption/creation had finished AND registered
    itself in _epic_worktrees. Demoting that lock to a map guard (so one epic's
    slow creation stops blocking every other epic) turned the check into an
    instant read that returns 'untracked' for a worktree another thread is
    actively populating -- and this sweep then force-removes the directory,
    which is exactly the outcome prune's own docstring says the check exists to
    prevent ("every subsequent operation on that epic would fail until the next
    restart, silently defeating #48's own fix").

    _epic_worktrees_pending restores the skip decision without restoring the
    blocking: prune runs on the event-loop thread at startup, so taking the
    per-key lock here would freeze the loop for the creation's whole budget.
    """

    def _quiet_prune_environment(self, manager):
        """prune's own side calls, stubbed: the docker liveness probe (no
        containers running) and every git subprocess it may run."""
        return (
            patch.object(manager, '_get_running_container_mount_sources', return_value=set()),
            patch('services.project_workspace.subprocess.run', return_value=_ok()),
        )

    def test_a_worktree_being_adopted_is_not_pruned(self, manager, tmp_path):
        """The concrete startup interleaving: container recovery's
        _process_completed_repair_cycle() adopts a surviving worktree on its
        monitor thread while main.py's prune sweep runs on the event loop."""
        _make_base_clone(tmp_path, "my-project")
        worktree_path = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '900'
        (worktree_path / '.git').mkdir(parents=True)

        adopting = threading.Event()
        release = threading.Event()

        def _slow_branch_probe(_path):
            adopting.set()
            release.wait(10)
            return "feature/issue-900"

        liveness_patch, subprocess_patch = self._quiet_prune_environment(manager)
        with patch.object(manager, '_current_worktree_branch', side_effect=_slow_branch_probe), \
             liveness_patch, subprocess_patch as mock_run:
            adopter = threading.Thread(
                target=manager.get_or_create_epic_worktree,
                args=("my-project", "900", "feature/issue-900"),
                daemon=True,
            )
            adopter.start()
            try:
                assert adopting.wait(10), "test setup: the adoption never started"
                manager.prune_epic_worktrees()
            finally:
                release.set()
                adopter.join(10)

        assert worktree_path.exists(), (
            "prune deleted a worktree another thread was mid-adoption of -- the "
            "adopting thread then caches a path that no longer exists"
        )
        removals = [
            call for call in mock_run.call_args_list
            if 'worktree' in call.args[0] and 'remove' in call.args[0]
        ]
        assert not removals, f"prune ran a worktree removal anyway: {removals}"

    def test_a_worktree_being_created_is_not_pruned(self, manager, tmp_path):
        """Same window on the creation path, where it is seconds-to-hours wide
        rather than milliseconds: `git worktree add` has made the directory but
        the map write is still behind a project_checkout wait."""
        _make_base_clone(tmp_path, "my-project")
        worktree_path = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '901'

        creating = threading.Event()
        release = threading.Event()

        def _slow_add(_base_repo_dir, path, _branch_name, _default_branch):
            (Path(path) / '.git').mkdir(parents=True)
            creating.set()
            release.wait(10)

        liveness_patch, subprocess_patch = self._quiet_prune_environment(manager)
        with patch('services.project_checkout_lock.project_checkout_lock_sync', _RecordingLock([])), \
             patch.object(manager, '_add_epic_worktree', side_effect=_slow_add), \
             liveness_patch, subprocess_patch as mock_run:
            creator = threading.Thread(
                target=manager.get_or_create_epic_worktree,
                args=("my-project", "901", "feature/issue-901"),
                daemon=True,
            )
            creator.start()
            try:
                assert creating.wait(10), "test setup: the creation never started"
                manager.prune_epic_worktrees()
            finally:
                release.set()
                creator.join(10)

        assert worktree_path.exists(), (
            "prune deleted a worktree `git worktree add` had just created but "
            "whose creating thread had not yet registered it"
        )
        removals = [
            call for call in mock_run.call_args_list
            if 'worktree' in call.args[0] and 'remove' in call.args[0]
        ]
        assert not removals, f"prune ran a worktree removal anyway: {removals}"

    def test_a_genuinely_idle_worktree_is_still_pruned(self, manager, tmp_path):
        """The negative control: the in-flight marker must not turn this sweep
        into a no-op for the crash leftovers it exists to clean up."""
        _make_base_clone(tmp_path, "my-project")
        worktree_path = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '902'
        (worktree_path / '.git').mkdir(parents=True)

        liveness_patch, subprocess_patch = self._quiet_prune_environment(manager)
        with liveness_patch, subprocess_patch as mock_run:
            manager.prune_epic_worktrees()

        removals = [
            call for call in mock_run.call_args_list
            if 'worktree' in call.args[0] and 'remove' in call.args[0]
        ]
        assert removals, "an untracked, unmounted, uncorrupted leftover was not pruned"
        assert not worktree_path.exists()

    def test_the_pending_marker_is_dropped_when_a_creation_fails(self, manager, tmp_path):
        """The marker is a suppression, so it has to be released on every exit --
        a leaked one would make this epic's leftovers un-prunable for the life of
        the process."""
        _make_base_clone(tmp_path, "my-project")

        with patch('services.project_checkout_lock.project_checkout_lock_sync', _RecordingLock([])), \
             patch.object(manager, '_add_epic_worktree',
                          side_effect=RuntimeError("git worktree add failed")):
            with pytest.raises(RuntimeError):
                manager.get_or_create_epic_worktree(
                    "my-project", "903", branch_name="feature/issue-903"
                )

        assert manager._epic_worktrees_pending == {}


class TestSameEpicContentionIsBoundedAndAttributed:
    """Code review on #151/WI-6. The per-key serializer was taken as a bare,
    untimed `with lock:` -- one level ABOVE the point where
    project_checkout_lock publishes a wait to its activity registry. So a second
    dispatch for the SAME epic queued behind a winner that could sit in its
    project_checkout wait for ~3h, with nothing published under its own
    (project, issue_number) and no budget of its own: past
    zombie_threshold_minutes the watchdog reaped that run and redispatched the
    issue while this thread was still queued, which is the exact double
    execution the registry was added to prevent."""

    def _parked_winner(self, manager, epic_id):
        """A thread holding this epic's serializer, parked where a real winner
        parks: inside the guarded body, with the map guard released."""
        in_body = threading.Event()
        release = threading.Event()

        def _slow_add(_base_repo_dir, path, _branch_name, _default_branch):
            (Path(path) / '.git').mkdir(parents=True)
            in_body.set()
            release.wait(10)

        return in_body, release, _slow_add

    def test_a_second_call_for_the_same_epic_publishes_its_wait(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")
        in_body, release, slow_add = self._parked_winner(manager, "910")

        with patch('services.project_checkout_lock.project_checkout_lock_sync', _RecordingLock([])), \
             patch.object(manager, '_add_epic_worktree', side_effect=slow_add):
            winner = threading.Thread(
                target=manager.get_or_create_epic_worktree,
                args=("my-project", "910", "feature/issue-910"),
                kwargs={'issue_number': 911},
                daemon=True,
            )
            winner.start()
            try:
                assert in_body.wait(10), "test setup: the winner never reached its body"

                loser = threading.Thread(
                    target=manager.get_or_create_epic_worktree,
                    args=("my-project", "910", "feature/issue-910"),
                    kwargs={'issue_number': 912},
                    daemon=True,
                )
                loser.start()

                # The loser's wait must vouch for ITS OWN issue -- that is what
                # pipeline_watchdog reads to tell "parked on a lock" from "zombie".
                deadline = time.monotonic() + 10
                described = None
                while time.monotonic() < deadline:
                    described = project_checkout_lock.describe_active_resource_lock_activity(
                        "my-project", 912
                    )
                    if described:
                        break
                    time.sleep(0.05)
                assert described, (
                    "a second dispatch for the same epic waited with nothing published "
                    "under its own issue -- pipeline_watchdog will reap and redispatch it"
                )
                assert 'epic_worktree' in described
            finally:
                release.set()
                winner.join(10)
                loser.join(10)

        assert project_checkout_lock.describe_active_resource_lock_activity(
            "my-project", 912
        ) is None, "the wait's registry entry outlived its frame"

    def test_a_second_call_for_the_same_epic_gives_up_instead_of_queueing_forever(
        self, manager, tmp_path
    ):
        from services.resource_lock_errors import is_lock_timeout_error

        _make_base_clone(tmp_path, "my-project")
        in_body, release, slow_add = self._parked_winner(manager, "913")
        raised = []

        with patch('services.project_checkout_lock.project_checkout_lock_sync', _RecordingLock([])), \
             patch.object(manager, '_add_epic_worktree', side_effect=slow_add):
            winner = threading.Thread(
                target=manager.get_or_create_epic_worktree,
                args=("my-project", "913", "feature/issue-913"),
                kwargs={'issue_number': 914},
                daemon=True,
            )
            winner.start()
            try:
                assert in_body.wait(10), "test setup: the winner never reached its body"

                def _loser():
                    try:
                        manager.get_or_create_epic_worktree(
                            "my-project", "913", "feature/issue-913",
                            issue_number=915,
                            checkout_lock_timeout_seconds=0.5,
                        )
                    except BaseException as e:
                        raised.append(e)

                loser = threading.Thread(target=_loser, daemon=True)
                loser.start()
                loser.join(10)
                assert not loser.is_alive(), "the second call queued past its own budget"
            finally:
                release.set()
                winner.join(10)

        assert len(raised) == 1, f"expected the loser to fail loud, got {raised}"
        assert isinstance(raised[0], ProjectCheckoutLockTimeoutError)
        assert is_lock_timeout_error(raised[0]), (
            "the loser must route through the 'lock_contention' path, not count as "
            "a dispatch failure"
        )


class TestBlockingWaitsGetTheirOwnExecutor:
    """Code review on #151/WI-6. asyncio.to_thread() runs on the loop's DEFAULT
    executor, and project_checkout_lock_async uses that same pool to acquire the
    lock AND to release it (_join_heartbeat_thread_async submits the heartbeat
    join to run_in_executor(None, ...) and awaits it BEFORE the outer finally
    reaches _release_and_warn). Park enough up-to-3h waits for that lock in the
    default pool and the coroutine holding it cannot get a thread to finish
    letting go -- a circular wait in which every waiter is then guaranteed to
    time out. The off-loop entry points use a dedicated pool so a wait can only
    ever starve other waits."""

    def test_the_wait_runs_off_the_default_executor(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")
        parked = threading.Event()
        release = threading.Event()
        worker_names = []

        def _slow_add(_base_repo_dir, path, _branch_name, _default_branch):
            worker_names.append(threading.current_thread().name)
            (Path(path) / '.git').mkdir(parents=True)
            parked.set()
            release.wait(10)

        async def _scenario():
            loop = asyncio.get_running_loop()
            # A single-thread default executor makes the hazard exact: if the
            # resolution below went through asyncio.to_thread it would occupy
            # the only thread project_checkout_lock_async has to release with.
            loop.set_default_executor(
                ThreadPoolExecutor(max_workers=1, thread_name_prefix='default-pool')
            )
            resolution = asyncio.create_task(
                manager.get_or_create_epic_worktree_off_loop(
                    "my-project", "920", "feature/issue-920"
                )
            )
            await asyncio.to_thread(parked.wait, 10)
            assert parked.is_set(), "test setup: the resolution never parked"
            # The default pool must still be usable while that wait is parked.
            still_free = await asyncio.wait_for(
                asyncio.to_thread(lambda: 'free'), timeout=5
            )
            assert still_free == 'free'
            release.set()
            return await resolution

        with patch('services.project_checkout_lock.project_checkout_lock_sync', _RecordingLock([])), \
             patch.object(manager, '_add_epic_worktree', side_effect=_slow_add):
            resolved = asyncio.run(_scenario())

        assert resolved == tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '920'
        assert worker_names and worker_names[0].startswith('epic-worktree'), (
            f"the blocking wait ran on {worker_names!r} -- it must not share a pool "
            "with project_checkout_lock_async's acquire/release submissions"
        )

    def test_the_off_loop_entry_point_forwards_its_arguments_verbatim(self, manager, tmp_path):
        """It is a thread hop and nothing else -- no defaults of its own to drift
        away from the method it wraps."""
        _make_base_clone(tmp_path, "my-project")

        with patch.object(manager, 'get_project_dir', return_value=Path('/x')) as mock_get:
            resolved = asyncio.run(
                manager.get_project_dir_off_loop(
                    "my-project", "921", "feature/issue-921", issue_number=922
                )
            )

        assert resolved == Path('/x')
        mock_get.assert_called_once_with(
            "my-project", "921", "feature/issue-921", issue_number=922
        )
