"""
Unit tests for GitHub issue #100 (sub-issue of #36): batching
_check_and_process_waiting_issues_failsafe()'s per-board queue-sync
queries.

Before #100, this failsafe called pipeline_queue.get_next_waiting_issue()
once per (project, active pipeline) every poll cycle - unconditionally,
NOT gated by #94's per-board adaptive backoff - and each call fetched its
own board via execute_board_query_cached() (one fresh GraphQL request per
board, every cycle). Live production telemetry from the #36 soak
confirmed this was the dominant GraphQL call source in the system (195
calls/hour vs. 11 for the already-batched ProjectMonitor path).

An initial fix (this same file, first version) had #100's STEP 1.5
re-derive due-ness itself via _is_board_due() every cycle. That was
broken: by the time the failsafe runs, monitor_projects()'s own batched
branch has ALREADY advanced next_due for every board it just polled, so a
fresh _is_board_due() check moments later reads POST-mutation state and
finds almost nothing due - silently disabling Scenario 2 (the failsafe's
entire "check the Development queue" purpose) every cycle. Confirmed live:
execute_batched_board_queries() alone hit 674-736 calls/hour post-deploy,
driving the account to 100% of its GraphQL budget well before the hourly
reset - worse than intended, not better.

The fix tested here instead has monitor_projects() pass its OWN
due_boards list (computed BEFORE any state mutation) straight through to
the failsafe as due_boards_this_cycle - no re-derivation, no race, and
execute_batched_board_queries() ends up a guaranteed cache hit for those
boards (fetched seconds earlier by monitor_projects() itself), not a
duplicate fetch.

Covers:
- Gated on due_boards_this_cycle being provided (only true from
  monitor_projects()'s own batched branch): None/not provided, the
  pre-fetch step is skipped entirely and every board falls back to being
  checked individually - byte-identical to pre-#100 behavior.
- Provided: exactly ONE execute_batched_board_queries() call covers every
  board in due_boards_this_cycle (not every active board - only the ones
  the caller says were due), and each board's pre-fetched result is
  threaded through to the corresponding get_next_waiting_issue() call via
  its prefetched_board_data parameter.
- A board NOT in due_boards_this_cycle is skipped by STEP 2 & 3 entirely -
  it must NOT fall back to an individual fetch, which would silently
  re-check every not-due board every cycle and reproduce the incident.
- Bounded per-board fallback: a board IN due_boards_this_cycle whose
  batched fetch failed (present in the errors dict, or silently dropped
  before ever reaching a query) still falls back to
  prefetched_board_data=None for that one board only - which makes
  get_next_waiting_issue() fetch that board itself the normal way. Other
  boards in the same cycle are unaffected.

No live network access - all GitHub API calls are mocked.
"""
import os
import time

import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import Mock, patch

from services.project_monitor import ProjectMonitor
from config.manager import ConfigManager


def _make_pipeline(board_name):
    pipeline = Mock()
    pipeline.active = True
    pipeline.board_name = board_name
    pipeline.workflow = 'sdlc_execution_workflow'
    return pipeline


def _make_project_config(org, repo, pipelines):
    project_config = Mock()
    project_config.pipelines = pipelines
    project_config.github = {'org': org, 'repo': repo}
    project_config.orchestrator = {"polling_interval": 30}
    return project_config


def _make_monitor(config_manager, batched_flag):
    task_queue = Mock()
    env = {'USE_BATCHED_BOARD_QUERIES': 'true'} if batched_flag else {}
    with patch.dict(os.environ, env):
        monitor = ProjectMonitor(task_queue, config_manager)
    monitor.trigger_agent_for_status = Mock()
    monitor.get_issue_column_sync = Mock(return_value='Development')
    return monitor


def _due_board(project_name, owner, project_number, board_name):
    """Build one due_boards_this_cycle entry, matching the shape
    monitor_projects()'s own batched branch builds in its `all_boards`/
    `due_boards` lists."""
    return {
        'project_name': project_name,
        'owner': owner,
        'project_number': project_number,
        'board_key': (project_name, board_name),
    }


@pytest.fixture
def two_board_setup():
    """Two projects, each with one active pipeline/board - the standard
    shape for asserting 'N boards, 1 batched call'."""
    config_manager = Mock(spec=ConfigManager)
    config_manager.list_projects.return_value = []
    config_manager.list_visible_projects.return_value = ['project1', 'project2']

    pipeline1 = _make_pipeline('Board1')
    pipeline2 = _make_pipeline('Board2')
    config1 = _make_project_config('org1', 'org1/repo1', [pipeline1])
    config2 = _make_project_config('org2', 'org2/repo2', [pipeline2])
    configs = {'project1': config1, 'project2': config2}
    config_manager.get_project_config.side_effect = lambda name: configs[name]

    mock_lock_manager = Mock()
    mock_lock_manager.get_lock.return_value = None  # unlocked
    mock_queue_manager = Mock()
    mock_queue_manager.get_next_waiting_issue.return_value = None  # nothing to trigger

    due1 = _due_board('project1', 'org1', 101, 'Board1')
    due2 = _due_board('project2', 'org2', 202, 'Board2')

    return {
        'config_manager': config_manager,
        'lock_manager': mock_lock_manager,
        'queue_manager': mock_queue_manager,
        'pair1': ('org1', 101),
        'pair2': ('org2', 202),
        'due1': due1,
        'due2': due2,
        'due_boards': [due1, due2],
    }


class TestFailsafeBatchedGatheringFlagged:
    """due_boards_this_cycle provided (the shape monitor_projects()'s own
    batched branch passes): the pre-fetch step runs, scoped to exactly
    those boards."""

    def test_one_batched_call_covers_every_due_board(self, two_board_setup):
        monitor = _make_monitor(two_board_setup['config_manager'], batched_flag=True)

        data1 = {'items': {'nodes': [{'id': 'a'}]}}
        data2 = {'items': {'nodes': [{'id': 'b'}]}}
        results = {
            two_board_setup['pair1']: data1,
            two_board_setup['pair2']: data2,
        }

        with patch(
            'services.pipeline_lock_manager.get_pipeline_lock_manager',
            return_value=two_board_setup['lock_manager'],
        ), patch(
            'services.pipeline_queue_manager.get_pipeline_queue_manager',
            return_value=two_board_setup['queue_manager'],
        ), patch(
            'services.github_owner_utils.execute_batched_board_queries',
            return_value=(results, {}),
        ) as mock_batched:
            monitor._check_and_process_waiting_issues_failsafe(
                due_boards_this_cycle=two_board_setup['due_boards']
            )

        # ONE call covering both due boards, not one per board.
        assert mock_batched.call_count == 1
        called_pairs = set(mock_batched.call_args[0][0])
        assert called_pairs == {two_board_setup['pair1'], two_board_setup['pair2']}

        # Each due board's get_next_waiting_issue() received its own
        # pre-fetched data via the prefetched_board_data parameter.
        calls = two_board_setup['queue_manager'].get_next_waiting_issue.call_args_list
        assert len(calls) == 2
        forwarded_values = [c.kwargs['prefetched_board_data'] for c in calls]
        assert data1 in forwarded_values
        assert data2 in forwarded_values

    def test_does_not_re_derive_due_ness_from_just_mutated_board_poll_state(self, two_board_setup):
        """
        The exact regression this redesign fixes: a first version of this
        step called _is_board_due() itself instead of trusting the caller's
        due_boards_this_cycle - but by the time this method runs,
        monitor_projects()'s own batched branch has ALREADY called
        _record_board_poll_outcome() for every board it just polled,
        advancing next_due into the future. Re-deriving due-ness at that
        point would find the board NOT due (even though it genuinely was
        due moments ago, which is exactly why the caller is passing it in)
        and silently skip it - disabling Scenario 2 every cycle.

        Simulates that exact post-mutation state directly: board1's
        next_due is set far in the future (as _record_board_poll_outcome()
        would have JUST left it), yet it's still passed in
        due_boards_this_cycle because it WAS due this cycle. It must still
        be checked - proving this step trusts the passed-in list rather
        than re-deriving due-ness from (now-stale) _board_poll_state.
        """
        monitor = _make_monitor(two_board_setup['config_manager'], batched_flag=True)
        monitor._board_poll_state[('project1', 'Board1')] = {
            'interval': 30.0,
            'idle_cycles': 0,
            'next_due': time.monotonic() + 30,  # just advanced by _record_board_poll_outcome()
        }

        data1 = {'items': {'nodes': [{'id': 'a'}]}}
        results = {two_board_setup['pair1']: data1}

        with patch(
            'services.pipeline_lock_manager.get_pipeline_lock_manager',
            return_value=two_board_setup['lock_manager'],
        ), patch(
            'services.pipeline_queue_manager.get_pipeline_queue_manager',
            return_value=two_board_setup['queue_manager'],
        ), patch(
            'services.github_owner_utils.execute_batched_board_queries',
            return_value=(results, {}),
        ) as mock_batched:
            monitor._check_and_process_waiting_issues_failsafe(
                due_boards_this_cycle=[two_board_setup['due1']]
            )

        assert mock_batched.call_count == 1
        calls = two_board_setup['queue_manager'].get_next_waiting_issue.call_args_list
        assert len(calls) == 1
        assert calls[0].kwargs['prefetched_board_data'] == data1

    def test_not_due_board_excluded_from_due_boards_this_cycle_is_skipped_entirely(self, two_board_setup):
        """
        Regression test for the production incident this fix addresses: a
        board NOT included in due_boards_this_cycle (i.e. not due, per the
        caller's own per-board backoff) must be excluded from the batch AND
        skipped by STEP 2 & 3 entirely - NOT fall back to an individual
        get_next_waiting_issue() fetch, which would silently re-check every
        not-due board every cycle and reproduce the incident (confirmed
        live: execute_batched_board_queries() alone hit 674-736 calls/hour,
        driving the account to 100% of its GraphQL budget).
        """
        monitor = _make_monitor(two_board_setup['config_manager'], batched_flag=True)

        # Only board1 is due this cycle - board2 is deliberately omitted,
        # exactly as monitor_projects() would omit a not-yet-due board from
        # its own due_boards list.
        data1 = {'items': {'nodes': [{'id': 'a'}]}}
        results = {two_board_setup['pair1']: data1}

        with patch(
            'services.pipeline_lock_manager.get_pipeline_lock_manager',
            return_value=two_board_setup['lock_manager'],
        ), patch(
            'services.pipeline_queue_manager.get_pipeline_queue_manager',
            return_value=two_board_setup['queue_manager'],
        ), patch(
            'services.github_owner_utils.execute_batched_board_queries',
            return_value=(results, {}),
        ) as mock_batched:
            monitor._check_and_process_waiting_issues_failsafe(
                due_boards_this_cycle=[two_board_setup['due1']]
            )

        # Only the due board's pair was ever queried.
        assert mock_batched.call_count == 1
        called_pairs = set(mock_batched.call_args[0][0])
        assert called_pairs == {two_board_setup['pair1']}

        # Only the due board was checked at all - the not-due board must NOT
        # appear in get_next_waiting_issue()'s calls in any form (not even
        # with prefetched_board_data=None), since that would mean it still
        # triggered an individual network fetch.
        calls = two_board_setup['queue_manager'].get_next_waiting_issue.call_args_list
        assert len(calls) == 1
        assert calls[0].kwargs['prefetched_board_data'] == data1

    def test_bounded_fallback_for_board_whose_batch_fetch_failed(self, two_board_setup):
        monitor = _make_monitor(two_board_setup['config_manager'], batched_flag=True)

        data2 = {'items': {'nodes': [{'id': 'b'}]}}
        # board1's batched fetch failed; board2's succeeded in the same batch.
        # Both were due this cycle, so both are still checked.
        results = {two_board_setup['pair2']: data2}
        errors = {two_board_setup['pair1']: 'boom'}

        with patch(
            'services.pipeline_lock_manager.get_pipeline_lock_manager',
            return_value=two_board_setup['lock_manager'],
        ), patch(
            'services.pipeline_queue_manager.get_pipeline_queue_manager',
            return_value=two_board_setup['queue_manager'],
        ), patch(
            'services.github_owner_utils.execute_batched_board_queries',
            return_value=(results, errors),
        ) as mock_batched:
            # Must not raise - one bad board must not lose the rest of the cycle.
            monitor._check_and_process_waiting_issues_failsafe(
                due_boards_this_cycle=two_board_setup['due_boards']
            )

        assert mock_batched.call_count == 1

        calls = two_board_setup['queue_manager'].get_next_waiting_issue.call_args_list
        assert len(calls) == 2
        prefetched_values = [c.kwargs['prefetched_board_data'] for c in calls]
        # The failed board still gets checked (it WAS due), falling back to
        # None (get_next_waiting_issue fetches it itself, the normal
        # single-board way); the healthy board still got its pre-fetched data.
        assert None in prefetched_values
        assert data2 in prefetched_values

    def test_board_missing_from_both_results_and_errors_logs_warning_and_falls_back(self, two_board_setup, caplog):
        """
        Regression test: a due board can be silently dropped before ever
        reaching a query (e.g. build_batched_board_queries() skips a whole
        chunk whose owner type can't be resolved), landing in neither
        batched_results nor batched_errors. That board must still be
        checked (falling back to an individual fetch) AND log a warning -
        not silently vanish with zero signal every cycle.
        """
        import logging
        monitor = _make_monitor(two_board_setup['config_manager'], batched_flag=True)

        data2 = {'items': {'nodes': [{'id': 'b'}]}}
        # board1 is in NEITHER results nor errors; board2 succeeded.
        results = {two_board_setup['pair2']: data2}
        errors = {}

        with patch(
            'services.pipeline_lock_manager.get_pipeline_lock_manager',
            return_value=two_board_setup['lock_manager'],
        ), patch(
            'services.pipeline_queue_manager.get_pipeline_queue_manager',
            return_value=two_board_setup['queue_manager'],
        ), patch(
            'services.github_owner_utils.execute_batched_board_queries',
            return_value=(results, errors),
        ), caplog.at_level(logging.WARNING, logger='services.project_monitor'):
            monitor._check_and_process_waiting_issues_failsafe(
                due_boards_this_cycle=two_board_setup['due_boards']
            )

        calls = two_board_setup['queue_manager'].get_next_waiting_issue.call_args_list
        assert len(calls) == 2
        prefetched_values = [c.kwargs['prefetched_board_data'] for c in calls]
        assert None in prefetched_values  # board1 falls back
        assert data2 in prefetched_values  # board2 unaffected
        # A warning was logged for the silently-dropped board, not just silence.
        assert any('project1' in r.message or 'Board1' in r.message for r in caplog.records)

    def test_circuit_breaker_open_skips_batched_prefetch_entirely(self, two_board_setup):
        """Matches _fetch_boards_batched()'s own breaker check: don't serve
        even a cache hit while the breaker is open. due_board_keys stays
        None (the safe 'check everything' default), so every board - due or
        not - still gets its normal individual-fetch attempt, which itself
        short-circuits harmlessly via the same breaker check inside
        execute_board_query_cached()/graphql()."""
        monitor = _make_monitor(two_board_setup['config_manager'], batched_flag=True)

        mock_github_client = Mock()
        mock_github_client.breaker.is_open.return_value = True

        with patch(
            'services.pipeline_lock_manager.get_pipeline_lock_manager',
            return_value=two_board_setup['lock_manager'],
        ), patch(
            'services.pipeline_queue_manager.get_pipeline_queue_manager',
            return_value=two_board_setup['queue_manager'],
        ), patch(
            'services.project_monitor.get_github_client',
            return_value=mock_github_client,
        ), patch(
            'services.github_owner_utils.execute_batched_board_queries'
        ) as mock_batched:
            monitor._check_and_process_waiting_issues_failsafe(
                due_boards_this_cycle=two_board_setup['due_boards']
            )

        mock_batched.assert_not_called()
        calls = two_board_setup['queue_manager'].get_next_waiting_issue.call_args_list
        assert len(calls) == 2
        assert all(c.kwargs['prefetched_board_data'] is None for c in calls)

    def test_unexpected_error_during_gathering_falls_back_not_whole_cycle_skip(self, two_board_setup):
        """
        Regression test: an unhandled exception anywhere in the
        batched-fetch step (e.g. execute_batched_board_queries() itself
        raising) must not propagate to this method's outer except and skip
        STEP 2 & 3 - every project/board's waiting-issue processing - for
        the whole cycle. It must instead degrade to due_board_keys=None
        (every board falls back to its own individual fetch, identical to
        due_boards_this_cycle not being provided at all) while STEP 2 & 3
        still runs for every board.
        """
        monitor = _make_monitor(two_board_setup['config_manager'], batched_flag=True)

        with patch(
            'services.pipeline_lock_manager.get_pipeline_lock_manager',
            return_value=two_board_setup['lock_manager'],
        ), patch(
            'services.pipeline_queue_manager.get_pipeline_queue_manager',
            return_value=two_board_setup['queue_manager'],
        ), patch(
            'services.github_owner_utils.execute_batched_board_queries',
            side_effect=RuntimeError("transport exploded"),
        ):
            # Must not raise, and STEP 2 & 3 must still run for both boards.
            monitor._check_and_process_waiting_issues_failsafe(
                due_boards_this_cycle=two_board_setup['due_boards']
            )

        calls = two_board_setup['queue_manager'].get_next_waiting_issue.call_args_list
        assert len(calls) == 2  # both boards still processed, not skipped
        assert all(c.kwargs['prefetched_board_data'] is None for c in calls)


    def test_empty_due_boards_list_means_check_no_board_not_check_every_board(self, two_board_setup):
        """
        Regression test: due_boards_this_cycle=[] (batching is on, but zero
        tracked boards happened to be due this exact cycle - a normal,
        common occurrence, e.g. right after a burst of boards all just had
        their next_due pushed out together) must mean "check no board" -
        NOT get conflated with None ("batching is off/unavailable", which
        means "check every board"). A plain truthiness check (`if
        due_boards_this_cycle:`) treats [] and None identically, silently
        reverting to check-everything on every empty-due-set cycle and
        reproducing the exact call-storm this whole fix exists for.
        """
        monitor = _make_monitor(two_board_setup['config_manager'], batched_flag=True)

        with patch(
            'services.pipeline_lock_manager.get_pipeline_lock_manager',
            return_value=two_board_setup['lock_manager'],
        ), patch(
            'services.pipeline_queue_manager.get_pipeline_queue_manager',
            return_value=two_board_setup['queue_manager'],
        ), patch(
            'services.github_owner_utils.execute_batched_board_queries',
            return_value=({}, {}),
        ) as mock_batched:
            monitor._check_and_process_waiting_issues_failsafe(due_boards_this_cycle=[])

        # execute_batched_board_queries([]) is called (it has its own cheap
        # empty-list guard - no network call) but critically no board falls
        # back to an individual fetch. If [] were mistaken for None, both
        # boards would show up here with prefetched_board_data=None.
        mock_batched.assert_called_once_with([])
        assert two_board_setup['queue_manager'].get_next_waiting_issue.call_count == 0


class TestFailsafeBatchedGatheringUnflagged:
    """due_boards_this_cycle not provided (None) - the shape the caller
    passes when USE_BATCHED_BOARD_QUERIES is off, or isn't in its batched
    branch: the pre-fetch step must be a complete no-op, preserving
    pre-#100 behavior exactly."""

    def test_batched_call_never_made_when_due_boards_not_provided(self, two_board_setup):
        monitor = _make_monitor(two_board_setup['config_manager'], batched_flag=False)
        assert monitor._use_batched_board_queries is False

        with patch(
            'services.pipeline_lock_manager.get_pipeline_lock_manager',
            return_value=two_board_setup['lock_manager'],
        ), patch(
            'services.pipeline_queue_manager.get_pipeline_queue_manager',
            return_value=two_board_setup['queue_manager'],
        ), patch(
            'services.github_owner_utils.execute_batched_board_queries'
        ) as mock_batched:
            monitor._check_and_process_waiting_issues_failsafe()

        mock_batched.assert_not_called()

        # Every board falls back to the un-prefetched path, exactly as
        # before #100 - get_next_waiting_issue() always sees
        # prefetched_board_data=None.
        calls = two_board_setup['queue_manager'].get_next_waiting_issue.call_args_list
        assert len(calls) == 2
        for c in calls:
            assert c.kwargs.get('prefetched_board_data') is None
