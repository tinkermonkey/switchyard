"""
Board-lock heartbeat (#153 WI-8, from #140 item 19).

A board lock's Redis TTL was never refreshed while it was held. #140 item 19
describes the mechanism as "the dispatch path re-acquires via
try_acquire_lock() on every poll, and its already_holds_lock branch refreshes
only the TTL, never lock_acquired_at" -- but that re-acquire does not actually
happen: every try_acquire_lock() call site is a dispatch attempt for a
DIFFERENT issue than the current holder, and the two sites an issue can re-enter
through while holding the lock skip the call outright
(trigger_agent_for_status's `already_has_lock` branch, and the PR-review path's
`_lock_held_by_us` check). So nothing refreshed a held board lock at all, and
the real exposure is LOCK_TTL_SECONDS (7200s), not the 4-hour staleness
threshold: once the key lapses, try_acquire_lock()'s Redis transaction reads it
back as an empty dict, which is falsy, and grants the board to the next waiting
issue while the original agent (up to 10800s per config/foundations/agents.yaml)
is still running.

ProjectMonitor._refresh_held_board_locks() closes that, from the monitoring
thread that already visits every board every cycle, with no per-hold heartbeat
thread -- see its docstring for why the project_checkout_lock shape does not
fit the board lock.
"""
import inspect
import sys
import os
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, MagicMock, patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from services.pipeline_lock_manager import PipelineLock, TouchResult
from services.project_checkout_lock import (
    DEFAULT_TIMEOUT_SECONDS as CHECKOUT_LOCK_WAIT_BUDGET_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
)
from services.project_monitor import ProjectMonitor


def _lock(issue_number=123, age_seconds=HEARTBEAT_INTERVAL_SECONDS + 60, retained=None):
    acquired = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return PipelineLock(
        project="proj",
        board="board",
        locked_by_issue=issue_number,
        lock_acquired_at=acquired.isoformat(),
        lock_status='locked',
        retained_reason=retained,
    )


class BoardLockHeartbeatTestBase(unittest.TestCase):
    def setUp(self):
        pipeline = Mock()
        pipeline.active = True
        pipeline.board_name = "board"
        project_config = Mock()
        project_config.pipelines = [pipeline]

        self.config_manager = Mock()
        self.config_manager.list_projects.return_value = []
        self.config_manager.list_visible_projects.return_value = ["proj"]
        self.config_manager.get_project_config.return_value = project_config
        self.config_manager.get_agents.return_value = {}

        self.monitor = ProjectMonitor(Mock(), self.config_manager)
        self.monitor.pipeline_run_manager = Mock()
        self.monitor.pipeline_run_manager.get_active_pipeline_run.return_value = Mock(
            id="run-1", status='active'
        )
        # No decision-event heartbeat available (what an unreachable
        # Elasticsearch returns) unless a test says otherwise.
        self.monitor._get_last_pipeline_run_event_time_with_reason = Mock(
            return_value=(None, 'es_unavailable')
        )

        self.lock_manager = MagicMock()
        self.lock_manager.touch_lock.return_value = TouchResult.REFRESHED
        # The ordinary case: Redis still holds the key under this hold, so the
        # heartbeat interval is the only thing deciding whether a refresh is
        # due. Tests about a LOST key set this True explicitly.
        self.lock_manager.redis_lock_is_missing.return_value = False

    def sweep(self):
        with patch(
            'services.pipeline_lock_manager.get_pipeline_lock_manager',
            return_value=self.lock_manager,
        ):
            self.monitor._refresh_held_board_locks()


class TestRefreshesLiveBoardLocks(BoardLockHeartbeatTestBase):

    def test_refreshes_an_aged_board_lock_whose_run_is_still_active(self):
        """The core regression: nothing else in the system resets a held board
        lock's TTL or lock_acquired_at."""
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)

        self.sweep()

        self.lock_manager.touch_lock.assert_called_once_with(
            "proj", "board", 123
        )

    def test_the_liveness_lookup_is_board_scoped_and_read_only(self):
        """
        Both arguments are load-bearing and were missing. Without board=,
        get_active_pipeline_run() checks only the LEGACY board-less Redis
        mapping -- which create_pipeline_run() never writes -- so every lookup
        falls through to Elasticsearch and returns None whenever ES is down,
        silently turning this whole heartbeat off (and, when ES is up, letting
        one board's run keep a leaked lock alive on another board). Without
        restore_to_redis=False, this periodic sweep re-setexes a crashed run's
        blob on every pass, keeping alive the very predicate that decides to
        pin the lock -- exactly what that flag's docstring warns sweeps about.
        """
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)

        self.sweep()

        self.monitor.pipeline_run_manager.get_active_pipeline_run.assert_called_once_with(
            "proj", 123, board="board", restore_to_redis=False
        )

    def test_does_not_refresh_a_run_parked_in_feedback_listening(self):
        """
        get_active_pipeline_run() also returns runs whose status is
        'feedback_listening' -- the status the human-feedback loop sets
        precisely so the zombie watchdog will NOT kill it while a human takes
        "many hours" to reply. Heartbeating that hold would pin the whole board
        for the entire wait, with no automatic recovery: the TTL and the 4-hour
        staleness heuristic are the only continuous reclaim paths board locks
        have, and refreshing disables both.
        """
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)
        self.monitor.pipeline_run_manager.get_active_pipeline_run.return_value = Mock(
            id="run-1", status='feedback_listening'
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_stops_refreshing_a_run_that_has_gone_silent_longer_than_any_holder_may_be(self):
        """
        The bound on pinning. A run that dies without ever being marked ended
        keeps reading 'active' in Elasticsearch, and refreshing it forever
        would wedge the board until the orchestrator restarts --
        _reconcile_active_runs()'s stale-lock watchdog runs only at startup,
        and pipeline_watchdog reaps runs, not locks (and skips its whole pass
        while the Claude Code breaker is open, which is the window this sweep
        is placed above the breaker checks to keep running through).
        """
        self.config_manager.get_agents.return_value = {'agent': Mock(timeout=3600)}
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)
        self.monitor._get_last_pipeline_run_event_time_with_reason = Mock(
            return_value=(
                datetime.now(timezone.utc) - timedelta(
                    seconds=3600 + CHECKOUT_LOCK_WAIT_BUDGET_SECONDS
                    + HEARTBEAT_INTERVAL_SECONDS + 60
                ),
                'ok',
            )
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_still_refreshes_a_run_whose_last_event_is_within_that_window(self):
        """The silence window has to clear the longest an agent may legitimately
        run without writing a decision event, or this bound would itself
        re-open the double-dispatch the sweep exists to prevent."""
        self.config_manager.get_agents.return_value = {'agent': Mock(timeout=3600)}
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)
        self.monitor._get_last_pipeline_run_event_time_with_reason = Mock(
            return_value=(datetime.now(timezone.utc) - timedelta(seconds=3600 - 60), 'ok')
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_called_once_with(
            "proj", "board", 123
        )

    def test_the_silence_bound_covers_the_project_checkout_lock_wait_too(self):
        """
        Found in the WI-8 review round: the bound was the agent timeout alone
        (3.5h with the margin), but a board-lock holder's real silence is the
        checkout-lock wait PLUS the agent run. claude_integration.py wraps
        run_agent_in_container() in project_checkout_lock_async(), and that
        wait runs with the board lock already held and emits no decision
        events at all (_log_busy() only logs and sleeps). Sized at the shorter
        bound, this stopped refreshing a live holder's lock roughly 3.5h in --
        and its Redis key then expired under a running agent, which is the
        double-dispatch the sweep exists to prevent.
        """
        self.config_manager.get_agents.return_value = {'agent': Mock(timeout=10800)}
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)
        # Silent for 5h: past the agent-only bound, inside the real one.
        self.monitor._get_last_pipeline_run_event_time_with_reason = Mock(
            return_value=(datetime.now(timezone.utc) - timedelta(hours=5), 'ok')
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_called_once_with("proj", "board", 123)

    def test_does_not_refresh_a_lock_younger_than_the_heartbeat_interval(self):
        """Aged against the lock's OWN lock_acquired_at (which touch_lock
        resets), so no per-board timer has to survive a restart."""
        self.lock_manager.get_lock_fail_closed.return_value = (
            _lock(age_seconds=HEARTBEAT_INTERVAL_SECONDS - 60), True
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_a_lost_redis_key_is_re_established_without_waiting_for_the_interval(self):
        """
        REGRESSION (WI-8 review round): the heartbeat interval gated
        re-establishment as well as the liveness refresh, so the sweep could
        not heal the exact failure it exists for during the first 30 minutes of
        every hold -- the window most stages live in.

        `docker-compose restart redis` (or a FLUSHDB, or an allkeys-lru
        eviction) two minutes into a hold drops the key while
        state/pipeline_locks/<proj>_board.yaml still reads locked. The durable
        gate in try_acquire_lock() passes it (nothing is retained), its Redis
        transaction reads the absent key back as an empty dict and returns
        "lock_acquired" without ever consulting that record, and
        _create_lock_yaml_only() then overwrites it -- two agents on one board.
        Nothing else re-creates the key at runtime: sync_yaml_locks_to_redis()
        only runs from main.py's startup block.
        """
        self.lock_manager.get_lock_fail_closed.return_value = (
            _lock(age_seconds=120), True
        )
        self.lock_manager.redis_lock_is_missing.return_value = True

        self.sweep()

        self.lock_manager.touch_lock.assert_called_once_with("proj", "board", 123)

    def test_a_lost_redis_key_is_still_held_to_the_same_liveness_predicate(self):
        """
        Re-establishment skips the AGE gate, not the liveness gate. Re-creating
        a key for a holder whose run has already died would pin the board with
        the one clock that could still have reclaimed it (the 7200s TTL, which
        the missing key had effectively already run out) reset.
        """
        self.lock_manager.get_lock_fail_closed.return_value = (
            _lock(age_seconds=120), True
        )
        self.lock_manager.redis_lock_is_missing.return_value = True
        self.monitor.pipeline_run_manager.get_active_pipeline_run.return_value = None

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_a_yaml_only_deployment_is_not_read_as_a_lost_redis_key(self):
        """
        redis_lock_is_missing() answers False when no Redis client is
        configured at all -- otherwise every lock in a YAML-only deployment
        would be touched on every 60s sweep instead of once per interval, and
        the age gate would effectively not exist.
        """
        from services.pipeline_lock_manager import PipelineLockManager

        manager = PipelineLockManager.__new__(PipelineLockManager)
        manager.redis_client = None

        self.assertFalse(manager.redis_lock_is_missing("proj", "board"))

    def test_a_redis_read_failure_is_not_read_as_a_lost_redis_key(self):
        """An unreadable Redis is not evidence the key is gone -- that is the
        same fail-closed posture get_lock_fail_closed() takes."""
        from services.pipeline_lock_manager import PipelineLockManager

        manager = PipelineLockManager.__new__(PipelineLockManager)
        manager.redis_client = MagicMock()
        manager.redis_client.hgetall.side_effect = Exception("redis down")

        self.assertFalse(manager.redis_lock_is_missing("proj", "board"))

    def test_does_not_refresh_when_the_holder_has_no_active_pipeline_run(self):
        """Refreshing unconditionally would pin an abandoned lock forever and
        disable both recovery paths board locks actually have at runtime (the
        7200s TTL and the 4-hour age heuristic) -- _reconcile_active_runs()'s
        stale-lock watchdog only runs at startup."""
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)
        self.monitor.pipeline_run_manager.get_active_pipeline_run.return_value = None

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_does_not_refresh_a_retained_lock(self):
        """release_lock()'s "not_found is the normal steady state for a lock
        retained more than two hours" reasoning depends on nothing re-touching
        a retained lock's Redis copy."""
        self.lock_manager.get_lock_fail_closed.return_value = (
            _lock(retained="agent crashed"), True
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_does_not_refresh_an_unlocked_board(self):
        self.lock_manager.get_lock_fail_closed.return_value = (None, True)

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_skips_a_board_whose_lock_state_could_not_be_read(self):
        """Fail closed, matching every other read site in this mechanism: an
        unknown state is not a licence to write."""
        self.lock_manager.get_lock_fail_closed.return_value = (None, False)

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_skips_an_inactive_pipeline(self):
        self.config_manager.get_project_config.return_value.pipelines[0].active = False
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)

        self.sweep()

        self.lock_manager.get_lock_fail_closed.assert_not_called()


class TestNoEventTimeIsNotOneState(BoardLockHeartbeatTestBase):
    """
    Found in the WI-8 review round, and narrowed again in the round after it.
    `last_event_at is None` was read as "still progressing" unconditionally,
    justified by a bound that only covers ONE of the three ways
    _get_last_pipeline_run_event_time() produces it: 'es_unavailable', where
    PipelineRunManager has no usable client either and get_active_pipeline_run()
    resolves entirely from a Redis run blob a dead run stops refreshing.

    Neither of the other two has that bound. Both happen with an ES client
    present, where get_active_pipeline_run() keeps resolving the run from the
    pipeline-runs-* search for as long as its doc reads 'active' -- so both are
    bounded by the run's own started_at instead. 'no_events' was fixed first;
    'query_failed' kept returning True unconditionally for one more round.
    """

    def test_es_being_unavailable_still_counts_as_progressing(self):
        """Refusing here would re-open the double-dispatch this sweep exists
        to prevent for the whole of every ES outage. Bounded from the other
        side: without ES, get_active_pipeline_run() resolves from the Redis run
        blob, which a dead run stops refreshing."""
        self.config_manager.get_agents.return_value = {'agent': Mock(timeout=3600)}
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)
        self.monitor.pipeline_run_manager.get_active_pipeline_run.return_value = Mock(
            id="run-1", status='active',
            started_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
        )
        self.monitor._get_last_pipeline_run_event_time_with_reason = Mock(
            return_value=(None, 'es_unavailable')
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_called_once_with("proj", "board", 123)

    def test_a_failed_events_query_is_bounded_by_started_at_too(self):
        """
        REGRESSION (WI-8 review round): 'query_failed' returned True
        unconditionally, borrowing 'es_unavailable's bound -- and that bound
        does not exist here. obs.es being present means PipelineRunManager.es
        is too (both build Elasticsearch([...]) lazily, so the client exists
        even with ES down), so get_active_pipeline_run() keeps resolving the
        run from the pipeline-runs-* search for as long as its doc reads
        'active' and the Redis-blob TTL never applies. A run whose container
        was killed out from under the orchestrator plus a persistently failing
        decision-events-* query therefore had its lock touched every 60s
        forever, defeating BOTH the Redis TTL and the 4-hour staleness
        heuristic. The run's own started_at bounds it without needing events
        at all.
        """
        self.config_manager.get_agents.return_value = {'agent': Mock(timeout=3600)}
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)
        self.monitor.pipeline_run_manager.get_active_pipeline_run.return_value = Mock(
            id="run-1", status='active',
            started_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
        )
        self.monitor._get_last_pipeline_run_event_time_with_reason = Mock(
            return_value=(None, 'query_failed')
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_a_failed_events_query_on_a_young_run_still_counts_as_progressing(self):
        """
        The other half of that bound: a failing events query must not become a
        reason to stop protecting a hold that has not yet been running long
        enough for any holder to have gone legitimately silent -- that would
        re-open the double-dispatch for the whole of every ES incident.
        """
        self.config_manager.get_agents.return_value = {'agent': Mock(timeout=3600)}
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)
        self.monitor.pipeline_run_manager.get_active_pipeline_run.return_value = Mock(
            id="run-1", status='active',
            started_at=(datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(),
        )
        self.monitor._get_last_pipeline_run_event_time_with_reason = Mock(
            return_value=(None, 'query_failed')
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_called_once_with("proj", "board", 123)

    def test_a_run_that_never_wrote_an_event_is_bounded_by_its_started_at(self):
        """
        The reachable wedge: a run created by get_or_create_pipeline_run()
        whose dispatch died before any emitter wrote a decision event carrying
        its id (or whose decision-events-* daily index has rolled off while its
        pipeline-runs-* doc still reads 'active'). ES answers, with zero hits.
        Same started_at fallback _find_stalled_issues_for_pipeline() already
        applies to this identical ambiguity.
        """
        self.config_manager.get_agents.return_value = {'agent': Mock(timeout=3600)}
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)
        self.monitor.pipeline_run_manager.get_active_pipeline_run.return_value = Mock(
            id="run-1", status='active',
            started_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
        )
        self.monitor._get_last_pipeline_run_event_time_with_reason = Mock(
            return_value=(None, 'no_events')
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_a_young_run_that_has_not_written_an_event_yet_is_left_alone(self):
        """Dispatch legitimately precedes the first decision event, and the
        checkout-lock wait can precede it by hours -- so "no events yet" only
        becomes evidence of death once started_at clears the same bound."""
        self.config_manager.get_agents.return_value = {'agent': Mock(timeout=3600)}
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)
        self.monitor.pipeline_run_manager.get_active_pipeline_run.return_value = Mock(
            id="run-1", status='active',
            started_at=(datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(),
        )
        self.monitor._get_last_pipeline_run_event_time_with_reason = Mock(
            return_value=(None, 'no_events')
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_called_once_with("proj", "board", 123)

    def test_a_run_with_no_usable_started_at_is_left_alone(self):
        """Nothing to bound it with; do not un-pin a lock on no evidence."""
        self.config_manager.get_agents.return_value = {'agent': Mock(timeout=3600)}
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)
        self.monitor.pipeline_run_manager.get_active_pipeline_run.return_value = Mock(
            id="run-1", status='active', started_at=None,
        )
        self.monitor._get_last_pipeline_run_event_time_with_reason = Mock(
            return_value=(None, 'no_events')
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_called_once_with("proj", "board", 123)


class TestAFailingQueryIsBoundedByObservedProgressNotRunAge(BoardLockHeartbeatTestBase):
    """
    Found in the WI-8 review round after 'query_failed' was first given
    'no_events'' started_at bound. The two reasons are not symmetric, and
    treating them so traded one failure mode for a worse, correlated one.

    'no_events' is an ANSWER -- ES searched and this run has written nothing --
    so started_at genuinely measures its silence. 'query_failed' is not an
    answer at all: the run may be writing a decision event every 30 seconds and
    the probe simply cannot see them, so started_at is just run age. A red
    shard on decision-events-*, or a sort-on-timestamp mapping conflict after a
    daily rollover, makes every board's probe fail at once -- and every hold
    older than the ~6.5h max-silence bound (ordinary for a multi-stage
    sdlc_execution run) would then stop being heartbeated, lose its Redis key
    to the 7200s TTL under a still-running agent, and be granted to the next
    queued issue. Two agents in one shared /workspace/<project> checkout, on
    every board simultaneously.

    So a failing query is measured from the newest decision event this process
    has actually READ for the run, which is a real silence measurement (at most
    one sweep stale), and only a run with no such observation falls back to
    started_at.
    """

    def _run(self, started_minutes_ago=45):
        return Mock(
            id="run-1", status='active',
            started_at=(
                datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago)
            ).isoformat(),
        )

    def test_a_failing_query_keeps_refreshing_a_long_run_that_was_recently_progressing(self):
        """The correlated-ES-failure case. This run is far older than the
        max-silence bound, but the last event this process managed to read for
        it was seconds ago -- that is evidence of progress, and run age is not
        evidence of anything."""
        self.config_manager.get_agents.return_value = {'agent': Mock(timeout=3600)}
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)
        self.monitor.pipeline_run_manager.get_active_pipeline_run.return_value = self._run(
            started_minutes_ago=60 * 24 * 30
        )
        self.monitor._board_lock_last_seen_event_at["run-1"] = (
            datetime.now(timezone.utc) - timedelta(seconds=30)
        )
        self.monitor._get_last_pipeline_run_event_time_with_reason = Mock(
            return_value=(None, 'query_failed')
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_called_once_with("proj", "board", 123)

    def test_a_successful_probe_records_the_anchor_a_later_failing_one_uses(self):
        """The anchor is not something a caller supplies -- it is laid down by
        the ordinary 'ok' path, so the protection above exists without anyone
        having to arrange it."""
        self.config_manager.get_agents.return_value = {'agent': Mock(timeout=3600)}
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)
        self.monitor.pipeline_run_manager.get_active_pipeline_run.return_value = self._run(
            started_minutes_ago=60 * 24 * 30
        )
        last_event = datetime.now(timezone.utc) - timedelta(seconds=30)
        self.monitor._get_last_pipeline_run_event_time_with_reason = Mock(
            return_value=(last_event, 'ok')
        )

        self.sweep()

        self.assertEqual(
            self.monitor._board_lock_last_seen_event_at["run-1"], last_event
        )

        # Now the probe starts failing, on a run old enough that run age would
        # have un-pinned it immediately.
        self.lock_manager.touch_lock.reset_mock()
        self.monitor._last_board_lock_heartbeat_at -= (
            self.monitor._board_lock_sweep_interval_seconds + 1
        )
        self.monitor._get_last_pipeline_run_event_time_with_reason = Mock(
            return_value=(None, 'query_failed')
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_called_once_with("proj", "board", 123)

    def test_a_failing_query_still_un_pins_a_run_whose_last_observed_event_is_old(self):
        """The other side: the bound must still fire for a run that genuinely
        died, or 'query_failed' is fail-open again and a killed container plus
        a persistently failing query pins the board forever."""
        self.config_manager.get_agents.return_value = {'agent': Mock(timeout=3600)}
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)
        self.monitor.pipeline_run_manager.get_active_pipeline_run.return_value = self._run(
            started_minutes_ago=60 * 24 * 30
        )
        self.monitor._board_lock_last_seen_event_at["run-1"] = (
            datetime.now(timezone.utc) - timedelta(days=2)
        )
        self.monitor._get_last_pipeline_run_event_time_with_reason = Mock(
            return_value=(None, 'query_failed')
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_no_events_is_an_answer_and_ignores_the_anchor(self):
        """'no_events' means ES searched and found nothing for this run, so the
        run's own start is the right measurement -- an anchor from before the
        index rolled off must not keep a dead run pinned."""
        self.config_manager.get_agents.return_value = {'agent': Mock(timeout=3600)}
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)
        self.monitor.pipeline_run_manager.get_active_pipeline_run.return_value = self._run(
            started_minutes_ago=60 * 24 * 30
        )
        self.monitor._board_lock_last_seen_event_at["run-1"] = (
            datetime.now(timezone.utc) - timedelta(seconds=30)
        )
        self.monitor._get_last_pipeline_run_event_time_with_reason = Mock(
            return_value=(None, 'no_events')
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_anchors_older_than_the_bound_are_pruned_each_sweep(self):
        """The dict is keyed by pipeline run id, so without pruning it would
        grow for the life of the process. An anchor past the bound cannot
        change an answer anyway -- the started_at fallback it leaves behind
        fails the same comparison, because a run cannot start after its own
        last event."""
        self.lock_manager.get_lock_fail_closed.return_value = (None, True)
        self.monitor._board_lock_last_seen_event_at = {
            "old-run": datetime.now(timezone.utc) - timedelta(days=7),
            "live-run": datetime.now(timezone.utc) - timedelta(seconds=30),
        }

        self.sweep()

        self.assertEqual(
            list(self.monitor._board_lock_last_seen_event_at), ["live-run"]
        )


class TestEventTimeReasonsAreDistinguished(unittest.TestCase):
    """
    _get_last_pipeline_run_event_time_with_reason() is what makes the three
    None cases above distinguishable at all; the plain
    _get_last_pipeline_run_event_time() keeps its original signature for
    _find_stalled_issues_for_pipeline().
    """

    def setUp(self):
        config_manager = Mock()
        config_manager.list_projects.return_value = []
        self.monitor = ProjectMonitor(Mock(), config_manager)

    def _with_obs(self, obs):
        return patch('monitoring.observability.get_observability_manager', return_value=obs)

    def test_no_elasticsearch_client_reports_es_unavailable(self):
        obs = Mock()
        obs.es = None

        with self._with_obs(obs):
            self.assertEqual(
                self.monitor._get_last_pipeline_run_event_time_with_reason("run-1"),
                (None, 'es_unavailable'),
            )

    def test_a_raising_search_reports_query_failed_and_is_logged_above_debug(self):
        """The whole point: a persistently failing liveness probe used to leave
        no trace at all (logger.debug), while every caller read its failure as
        'still alive'."""
        obs = Mock()
        obs.es.search.side_effect = Exception("search_phase_execution_exception")

        with self._with_obs(obs):
            with self.assertLogs('services.project_monitor', level='WARNING'):
                result = self.monitor._get_last_pipeline_run_event_time_with_reason("run-1")

        self.assertEqual(result, (None, 'query_failed'))

    def test_zero_hits_reports_no_events(self):
        obs = Mock()
        obs.es.search.return_value = {'hits': {'hits': []}}

        with self._with_obs(obs):
            self.assertEqual(
                self.monitor._get_last_pipeline_run_event_time_with_reason("run-1"),
                (None, 'no_events'),
            )

    def test_a_hit_reports_ok_and_the_plain_helper_still_returns_the_timestamp(self):
        obs = Mock()
        obs.es.search.return_value = {
            'hits': {'hits': [{'_source': {'timestamp': '2026-01-01T00:00:00+00:00'}}]}
        }

        with self._with_obs(obs):
            ts, reason = self.monitor._get_last_pipeline_run_event_time_with_reason("run-1")
            plain = self.monitor._get_last_pipeline_run_event_time("run-1")

        self.assertEqual(reason, 'ok')
        self.assertEqual(ts, datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(plain, ts)


class TestSweepIsResilientAndRateLimited(BoardLockHeartbeatTestBase):

    def test_one_boards_failure_does_not_stop_the_others(self):
        """This runs at the top of the monitoring cycle -- a raise here would
        take the whole cycle into its generic 10s-backoff handler."""
        second = Mock()
        second.active = True
        second.board_name = "board2"
        self.config_manager.get_project_config.return_value.pipelines.append(second)
        self.lock_manager.get_lock_fail_closed.side_effect = [
            Exception("redis exploded"),
            (_lock(), True),
        ]

        self.sweep()

        self.lock_manager.touch_lock.assert_called_once_with(
            "proj", "board2", 123
        )

    def test_a_project_whose_config_cannot_be_loaded_is_skipped(self):
        self.config_manager.get_project_config.side_effect = Exception("no config")

        self.sweep()

        self.lock_manager.get_lock_fail_closed.assert_not_called()

    def test_the_sweep_is_rate_limited_between_cycles(self):
        """The call site sits above monitor_projects()' circuit-breaker
        `continue`s, where the loop spins on a 5s sleep -- a lock read per board
        every 5s would be pointless I/O."""
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)

        self.sweep()
        self.sweep()

        self.assertEqual(self.lock_manager.get_lock_fail_closed.call_count, 1)

    def test_the_sweep_runs_again_once_its_interval_has_elapsed(self):
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)

        self.sweep()
        self.monitor._last_board_lock_heartbeat_at -= (
            self.monitor._board_lock_sweep_interval_seconds + 1
        )
        self.sweep()

        self.assertEqual(self.lock_manager.get_lock_fail_closed.call_count, 2)


class TestSweepRunsBeforeTheCircuitBreakerChecks(unittest.TestCase):
    """
    Placement matters and is not observable from _refresh_held_board_locks()
    itself. monitor_projects() short-circuits its whole cycle with `continue`
    while either the GitHub or the Claude Code circuit breaker is open -- but an
    agent that was already running when a breaker opened keeps running and keeps
    holding its board lock, and the release-and-dispatch-next paths in
    pipeline_progression and review_cycle keep running on worker threads
    regardless. The heartbeat touches only local Redis/YAML, so it must sit
    above both.
    """

    def test_the_heartbeat_call_precedes_both_breaker_checks(self):
        source = inspect.getsource(ProjectMonitor.monitor_projects)
        loop_body = source.split("while True:", 1)[1]

        heartbeat_at = loop_body.index("self._refresh_held_board_locks()")
        github_breaker_at = loop_body.index("github_client.breaker.is_open()")
        claude_breaker_at = loop_body.index("breaker and breaker.is_open()")

        self.assertLess(heartbeat_at, github_breaker_at)
        self.assertLess(heartbeat_at, claude_breaker_at)


if __name__ == '__main__':
    unittest.main()
