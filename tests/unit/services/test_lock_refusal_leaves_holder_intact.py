"""
Call-site coverage for the try_acquire_lock() refusals that leave the caller
still holding the lock (#139 and #174 review rounds).

MOST False try_acquire_lock() returns mean "you do not hold this lock", and the
two dispatch gates that tear a pipeline run down on a refusal were written
directly against that reading — services/project_monitor.py's review-cycle gate
says so in a log line ("Another issue is currently working on this board") and
its repair-cycle gate said so in a comment ("this issue never actually holds the
lock — end_pipeline_run() will correctly no-op its lock-release logic").

Two things break that reading, and end_pipeline_run() releases the lock whenever
it finds the run's own issue recorded as the holder
(TestEndPipelineRunReleasesWhatItFinds below pins that), so on either one the
teardown those gates ran ended a live run and handed the board to the next
queued issue, on the stated belief that no lock was ever taken:

  * "lock_mirror_write_failed_while_held" (#139):
    _refuse_unmirrored_redis_grant()'s "already_holds_lock" branch refuses an
    acquisition whose durable YAML mirror did not land, but deliberately does
    NOT roll the Redis key back, because that key belongs to a live holder and
    deleting it would release a lock out from under a running pipeline. The
    caller is refused while remaining the recorded holder, provably.

  * "lock_acquire_serialization_timeout" / "..._unavailable" (#174): the
    acquire guard refuses before either store is read or written, so whoever
    held the lock still holds it — which, at these two gates, is quite often the
    caller itself (both are reached by an issue that may have carried the lock
    in from an earlier stage). These decide nothing on their own, so the holder
    has to actually be read.

refusal_must_not_end_caller_run() is what tells the two apart, covering both
cases, and these tests pin both gates honoring it.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from config.manager import ConfigManager
from services.pipeline_lock_manager import (
    PipelineLockManager,
    refusal_leaves_caller_holding_lock,
    refusal_must_not_end_caller_run,
)
from services.project_monitor import ProjectMonitor

# The reason string the whole mechanism turns on. Spelled out here rather than
# imported so a rename has to be a deliberate, visible edit at both ends.
HELD_REFUSAL = "lock_mirror_write_failed_while_held"


@pytest.fixture
def mock_config_manager():
    config_manager = Mock(spec=ConfigManager)
    config_manager.list_projects.return_value = []
    config_manager.get_pipeline_template.return_value = None
    return config_manager


@pytest.fixture
def project_monitor(mock_config_manager):
    task_queue = Mock()
    monitor = ProjectMonitor(task_queue, mock_config_manager)
    monitor.pipeline_run_manager = Mock()
    return monitor


def _review_column():
    column = Mock()
    column.type = 'review'
    column.agent = 'code_reviewer'
    column.maker_agent = 'senior_software_engineer'
    column.max_iterations = 5
    column.stage_mapping = None  # skip current_stage_config resolution
    return column


def _project_config():
    project_config = Mock()
    project_config.github = {'org': 'tinkermonkey', 'repo': 'rounds'}
    project_config.pipelines = []
    return project_config


def _pipeline_config():
    pipeline_config = Mock()
    pipeline_config.workspace = 'issues'
    pipeline_config.template = 'sdlc_execution'
    return pipeline_config


def _drive_review_cycle_gate(project_monitor, acquire_result, holder_read=None):
    """
    Run _start_review_cycle_for_issue as far as its pipeline-lock gate, with
    try_acquire_lock() reporting `acquire_result`, and return the mocked lock
    manager so callers can assert on what the gate did with it.

    `holder_read` is what get_lock_holder_fail_closed() reports — a
    (holder_issue, reads_healthy) tuple, or an exception instance to raise. Only
    the acquire-guard refusals consult it; every other reason answers the
    question on the reason string alone.
    """
    mock_lock_manager = Mock()
    mock_lock_manager.try_acquire_lock.return_value = acquire_result
    if isinstance(holder_read, BaseException):
        mock_lock_manager.get_lock_holder_fail_closed.side_effect = holder_read
    else:
        mock_lock_manager.get_lock_holder_fail_closed.return_value = holder_read

    with patch.object(project_monitor, 'get_issue_details',
                      return_value={'title': 'T', 'url': 'u'}), \
         patch.object(project_monitor, 'get_previous_stage_context',
                      return_value='## Previous Work\n\nSome real output'), \
         patch.object(project_monitor.pipeline_run_manager, 'get_or_create_pipeline_run',
                      return_value=(Mock(id='run-1'), False)), \
         patch('services.pipeline_lock_manager.get_pipeline_lock_manager',
               return_value=mock_lock_manager):
        # No thread patch here, deliberately. Every call site in this file
        # passes an acquire_result of (False, ...), so the gate refuses and
        # returns before reaching the spawn -- instrumented across all 13
        # tests: zero threads started. An earlier version patched
        # RecordedThread in anyway and claimed this file leaked a cycle; it
        # does not, and removing the patch changed nothing. The sibling file
        # test_review_cycle_missing_context_cleanup.py is the one that drives
        # the grant path, and it asserts started() == 1 so its patch cannot
        # rot into decoration the way this one had.
        result = project_monitor._start_review_cycle_for_issue(
            project_name='rounds',
            board_name='SDLC Execution',
            issue_number=159,
            status='Code Review',
            repository='rounds',
            project_config=_project_config(),
            pipeline_config=_pipeline_config(),
            workflow_template=Mock(),
            column=_review_column(),
        )

    return result, mock_lock_manager


class TestReviewCycleGateDoesNotTearDownALiveHolder:
    def test_a_mirror_failure_on_a_held_lock_leaves_run_and_lock_alone(
        self, project_monitor
    ):
        """The regression: issue #159 already holds the board lock and carries
        it into a review column; its durable mirror write fails; the refusal
        must not end #159's own run, because end_pipeline_run() would then find
        locked_by_issue == 159 and release the board to the next queued issue.
        """
        result, mock_lock_manager = _drive_review_cycle_gate(
            project_monitor, (False, HELD_REFUSAL)
        )

        assert result is None
        project_monitor.pipeline_run_manager.end_pipeline_run.assert_not_called()
        mock_lock_manager.release_lock.assert_not_called()

    def test_an_ordinary_refusal_still_ends_the_run(self, project_monitor):
        """Control: the exemption is exactly and only for the refusals that
        leave this issue holding the lock. Genuine contention must still clean
        up the run this gate created, or it leaks until the zombie watchdog's
        hourly sweep."""
        result, _ = _drive_review_cycle_gate(
            project_monitor, (False, "locked_by_issue_999")
        )

        assert result is None
        project_monitor.pipeline_run_manager.end_pipeline_run.assert_called_once()
        kwargs = project_monitor.pipeline_run_manager.end_pipeline_run.call_args.kwargs
        assert "locked_by_issue_999" in kwargs['reason']

    def test_every_other_degraded_refusal_still_ends_the_run(self, project_monitor):
        """The other refusals that can already reach this gate on main. Each one
        either left the caller demonstrably not-the-holder or left no lock a
        release could match, so none is exempt.

        Deliberately NOT in this list: the two acquire-guard refusals, which
        touch no store at all and so leave the previous holder in place — see
        TestAcquireGuardRefusalsConsultTheHolder.
        """
        for reason in (
            "lock_state_unknown_failing_closed",
            "lock_write_failed",
            "lock_mirror_write_failed",
        ):
            project_monitor.pipeline_run_manager.reset_mock()
            assert not refusal_leaves_caller_holding_lock(reason)

            _drive_review_cycle_gate(project_monitor, (False, reason))

            project_monitor.pipeline_run_manager.end_pipeline_run.assert_called_once()


GUARD_REFUSALS = (
    "lock_acquire_serialization_timeout",
    "lock_acquire_serialization_unavailable",
)


class TestAcquireGuardRefusalsConsultTheHolder:
    """
    #174 review round. try_acquire_lock() takes its acquire guard with the
    default 10s file-lock timeout and refuses on contention
    ("lock_acquire_serialization_timeout") or on a guard file it cannot open
    ("lock_acquire_serialization_unavailable") — in both cases before reading or
    writing either store, so the recorded holder is untouched. release_lock()
    deliberately waits far longer for that same guard
    (RELEASE_GUARD_TIMEOUT_SECONDS then RELEASE_GUARD_RETRY_TIMEOUT_SECONDS), so
    a guard contended past 10s refuses the acquire and then GRANTS the release:
    the teardown here would end issue #159's live run, release its lock, and
    hand the board to the next queued issue.

    The reason string cannot tell holder from contender on its own, so the gate
    has to read the holder.
    """

    def test_the_gate_leaves_a_run_alone_when_this_issue_still_holds_the_lock(
        self, project_monitor
    ):
        for reason in GUARD_REFUSALS:
            project_monitor.pipeline_run_manager.reset_mock()

            result, mock_lock_manager = _drive_review_cycle_gate(
                project_monitor, (False, reason), holder_read=(159, True)
            )

            assert result is None
            project_monitor.pipeline_run_manager.end_pipeline_run.assert_not_called()
            mock_lock_manager.release_lock.assert_not_called()
            mock_lock_manager.get_lock_holder_fail_closed.assert_called_once_with(
                'rounds', 'SDLC Execution'
            )

    def test_the_gate_still_ends_the_run_when_another_issue_holds_the_lock(
        self, project_monitor
    ):
        """Control: a guard refusal on a board genuinely held by someone else is
        ordinary contention, and the run this gate created must still be cleaned
        up or it leaks until the zombie watchdog's hourly sweep."""
        for reason in GUARD_REFUSALS:
            project_monitor.pipeline_run_manager.reset_mock()

            _drive_review_cycle_gate(
                project_monitor, (False, reason), holder_read=(999, True)
            )

            project_monitor.pipeline_run_manager.end_pipeline_run.assert_called_once()

    def test_the_gate_still_ends_the_run_when_the_board_is_free(self, project_monitor):
        """The other control: nobody holds the lock, so there is nothing for
        end_pipeline_run() to release and the run must not leak."""
        _drive_review_cycle_gate(
            project_monitor, (False, GUARD_REFUSALS[0]), holder_read=(None, True)
        )

        project_monitor.pipeline_run_manager.end_pipeline_run.assert_called_once()

    def test_an_unreadable_holder_fails_closed(self, project_monitor):
        """Both stores unreadable: the gate cannot tell whether this issue is
        the holder, and being wrong in the tear-down direction releases a live
        pipeline's board, while being wrong the other way leaks one run until
        the watchdog sweeps it. Leave it alone."""
        result, mock_lock_manager = _drive_review_cycle_gate(
            project_monitor, (False, GUARD_REFUSALS[0]), holder_read=(None, False)
        )

        assert result is None
        project_monitor.pipeline_run_manager.end_pipeline_run.assert_not_called()
        mock_lock_manager.release_lock.assert_not_called()

    def test_a_raising_holder_read_fails_closed(self, project_monitor):
        """Same posture when the holder read raises outright."""
        result, mock_lock_manager = _drive_review_cycle_gate(
            project_monitor,
            (False, GUARD_REFUSALS[0]),
            holder_read=ConnectionError("Redis unreachable"),
        )

        assert result is None
        project_monitor.pipeline_run_manager.end_pipeline_run.assert_not_called()
        mock_lock_manager.release_lock.assert_not_called()


class TestRefusalMustNotEndCallerRun:
    """The predicate itself, independent of either gate."""

    def _manager(self, holder_read):
        manager = Mock()
        if isinstance(holder_read, BaseException):
            manager.get_lock_holder_fail_closed.side_effect = holder_read
        else:
            manager.get_lock_holder_fail_closed.return_value = holder_read
        return manager

    def test_the_held_mirror_failure_needs_no_holder_read(self):
        manager = self._manager((999, True))

        assert refusal_must_not_end_caller_run(
            HELD_REFUSAL, manager, "proj", "board", 159
        )
        # It PROVES the caller is the holder; reading the stores could only
        # weaken that with a stale answer.
        manager.get_lock_holder_fail_closed.assert_not_called()

    def test_a_guard_refusal_defers_to_the_recorded_holder(self):
        for reason in GUARD_REFUSALS:
            assert refusal_must_not_end_caller_run(
                reason, self._manager((159, True)), "proj", "board", 159
            )
            assert refusal_must_not_end_caller_run(
                reason, self._manager((999, True)), "proj", "board", 159
            ) is None

    def test_ordinary_refusals_are_decided_on_the_reason_alone(self):
        manager = self._manager((159, True))

        for reason in (
            "locked_by_issue_999",
            "locked_by_issue_159_failed",
            "lock_state_unknown_failing_closed",
            "lock_write_failed",
            "lock_mirror_write_failed",
            None,
        ):
            assert refusal_must_not_end_caller_run(
                reason, manager, "proj", "board", 159
            ) is None

        manager.get_lock_holder_fail_closed.assert_not_called()

    def test_it_returns_a_log_ready_explanation(self):
        """The gates interpolate the return value straight into their log line,
        so it has to read as a clause, not as a bare True."""
        explanation = refusal_must_not_end_caller_run(
            GUARD_REFUSALS[0], self._manager((159, True)), "proj", "board", 159
        )

        assert isinstance(explanation, str)
        assert "#159" in explanation


class TestEndPipelineRunReleasesWhatItFinds(unittest.TestCase):
    """
    Why the gates above must branch at all: end_pipeline_run() does not ask who
    asked it to run, it asks the lock manager who holds the lock — and releases
    it whenever that is this run's own issue. Against a real
    PipelineLockManager holding a real lock for #159, that is a release, not
    the no-op the repair-cycle gate's comment used to promise.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_es = MagicMock()
        self.mock_es.search.return_value = {'hits': {'total': {'value': 0}, 'hits': []}}
        self.mock_redis = MagicMock()
        with patch('services.pipeline_run.Elasticsearch', return_value=self.mock_es), \
             patch('services.pipeline_run.redis.Redis', return_value=self.mock_redis):
            from services.pipeline_run import PipelineRunManager
            self.manager = PipelineRunManager()
        self.manager.es = self.mock_es
        self.manager.redis = self.mock_redis

        self.lock_manager = PipelineLockManager(
            state_dir=Path(self.test_dir), use_redis=False
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_it_releases_a_lock_held_by_the_runs_own_issue(self):
        self.assertEqual(
            self.lock_manager.try_acquire_lock("proj", "board", 159),
            (True, "lock_acquired"),
        )

        run = self.manager.create_pipeline_run(
            issue_number=159, issue_title="t", issue_url="u",
            project="proj", board="board",
        )
        self.mock_redis.hget.return_value = run.id
        self.mock_redis.get.side_effect = lambda key: (
            json.dumps(run.to_dict()) if key == self.manager._get_redis_key(run.id) else None
        )

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager',
                   return_value=self.lock_manager):
            self.manager.end_pipeline_run(
                project="proj", issue_number=159,
                reason="Could not acquire lock: something", retain_lock=False,
            )

        self.assertIsNone(
            self.lock_manager.get_lock("proj", "board"),
            "end_pipeline_run released the lock this issue was still holding — "
            "which is exactly why a refusal that leaves the caller holding it "
            "must not reach this call",
        )


# The repair-cycle half of the same gate lives with the rest of that method's
# lock-gate coverage, in tests/unit/orchestrator/test_repair_cycle_lock_steal.py
# (TestLockAcquisitionGate), which already owns the heavy harness needed to
# drive _start_repair_cycle_for_issue for real.
