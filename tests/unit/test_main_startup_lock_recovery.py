"""
Regression tests for #169 review: the startup ORDER around the project_checkout
lock, and where the epic-worktree sweep runs.

Both findings are properties of main()'s call sequence rather than of any one
function, and main() cannot be driven in a unit test (it starts the monitor
loop, the scheduler and the observability server). Its source is read instead --
the same approach tests/unit/services/test_stale_execution_history.py and
tests/unit/test_container_gated_reporting.py take for their own call-site
invariants.

  1. Nothing else in the process frees a project-scoped RESOURCE lock:
     main.py's board-scoped stale-lock recovery iterates configured pipeline
     boards, and a resource lock lives under the reserved
     `__resource__project_checkout` board (RESOURCE_BOARD_PREFIX), leaving
     PipelineLockManager's 7200s Redis TTL / 14400s YAML staleness window as the
     only way out. Both of the startup steps that take that lock per project --
     initialize_all_projects() and prune_epic_worktrees() -- are bounded, so a
     lock stamped by the crashed prior process turned each of them into a
     guaranteed no-op in exactly the case they exist for: the restart after a
     crash. The recovery therefore has to run BEFORE them, not next to the
     dev_container_build one ~50 lines later.

  2. prune_epic_worktrees() takes each project's project_checkout lock with a
     bounded WAIT. project_checkout_lock_sync() polls with time.sleep() and
     releases through a guard that can itself sleep for two guard budgets when
     Redis is down, and the in-process holders it waits behind release from
     coroutines on the event loop -- so it must not run on that loop.
"""

import re
from pathlib import Path

import pytest

MAIN_PY = Path(__file__).resolve().parents[2] / 'main.py'


@pytest.fixture(scope='module')
def main_source():
    return MAIN_PY.read_text()


def _index_of(source, needle):
    index = source.find(needle)
    assert index != -1, f"main.py no longer contains {needle!r}"
    return index


class TestProjectCheckoutOrphanRecoveryRunsFirst:

    def test_it_recovers_orphaned_project_checkout_locks_at_all(self, main_source):
        assert 'PROJECT_CHECKOUT_RESOURCE' in main_source
        assert re.search(
            r'recover_orphaned_resource_locks,?\s*\n?\s*PROJECT_CHECKOUT_RESOURCE',
            main_source,
        ), "startup no longer recovers orphaned project_checkout locks"

    def test_it_runs_before_the_two_steps_that_wait_on_that_lock(self, main_source):
        """THE regression. Both waiters are bounded, so a dead predecessor's
        lock made each a no-op until the Redis TTL lapsed -- and the next
        restart inside that window hit the identical refusal."""
        recovery = _index_of(main_source, 'PROJECT_CHECKOUT_RESOURCE,')
        initialize = _index_of(main_source, 'workspace_manager.initialize_all_projects')
        prune = _index_of(main_source, 'workspace_manager.prune_epic_worktrees')

        assert recovery < initialize
        assert recovery < prune

    def test_the_dev_container_build_recovery_is_still_there_too(self, main_source):
        """The two are separate resources with separate reasons; adding one must
        not have replaced the other (#152)."""
        assert 'DEV_CONTAINER_BUILD_RESOURCE' in main_source


class TestThePruneSweepRunsOffTheEventLoop:

    def test_the_sweep_is_hopped_off_the_loop(self, main_source):
        """Its lock wait is a time.sleep() poll and its release guard can sleep
        for two guard budgets; neither may run on the loop. Off the loop the
        wait can also succeed, because the holders it waits behind release from
        coroutines on the loop this hop keeps free."""
        assert 'await asyncio.to_thread(workspace_manager.prune_epic_worktrees)' in main_source

    def test_it_is_not_called_directly_on_the_loop(self, main_source):
        assert not re.search(
            r'^\s*workspace_manager\.prune_epic_worktrees\(\)', main_source, re.MULTILINE
        ), "prune_epic_worktrees() is being called directly on the event-loop thread again"
