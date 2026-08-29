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

Covers:
- Gated behind USE_BATCHED_BOARD_QUERIES (same flag as #94): off, the
  gathering/batching step is skipped entirely and every board falls back
  to being fetched individually - byte-identical to pre-#100 behavior.
- On: exactly ONE execute_batched_board_queries() call covers every
  active project/board this cycle needs (N boards, 1 call), and each
  board's pre-fetched result is threaded through to the corresponding
  get_next_waiting_issue() call via its new prefetched_board_data
  parameter.
- Bounded per-board fallback: a board whose batched fetch failed (present
  in the errors dict) or whose project/board state couldn't be resolved
  (mirrors _fetch_boards_batched()'s own per-board fallback) falls back
  to prefetched_board_data=None for that one board only - which makes
  get_next_waiting_issue() fetch that board itself the normal way. Other
  boards in the same cycle are unaffected.

No live network access - all GitHub API calls are mocked.
"""
import os

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


def _make_project_state(board_name, project_number):
    board_state = Mock()
    board_state.project_number = project_number
    project_state = Mock()
    project_state.boards = {board_name: board_state}
    return project_state


def _make_monitor(config_manager, batched_flag):
    task_queue = Mock()
    env = {'USE_BATCHED_BOARD_QUERIES': 'true'} if batched_flag else {}
    with patch.dict(os.environ, env):
        monitor = ProjectMonitor(task_queue, config_manager)
    monitor.trigger_agent_for_status = Mock()
    monitor.get_issue_column_sync = Mock(return_value='Development')
    return monitor


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

    states = {
        'project1': _make_project_state('Board1', 101),
        'project2': _make_project_state('Board2', 202),
    }

    mock_lock_manager = Mock()
    mock_lock_manager.get_lock.return_value = None  # unlocked
    mock_queue_manager = Mock()
    mock_queue_manager.get_next_waiting_issue.return_value = None  # nothing to trigger

    return {
        'config_manager': config_manager,
        'states': states,
        'lock_manager': mock_lock_manager,
        'queue_manager': mock_queue_manager,
        'pair1': ('org1', 101),
        'pair2': ('org2', 202),
    }


class TestFailsafeBatchedGatheringFlagged:
    """USE_BATCHED_BOARD_QUERIES=true: the new pre-fetch step runs."""

    def test_one_batched_call_covers_every_board(self, two_board_setup):
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
            'config.state_manager.state_manager'
        ) as mock_state_manager, patch(
            'services.github_owner_utils.execute_batched_board_queries',
            return_value=(results, {}),
        ) as mock_batched:
            mock_state_manager.load_project_state.side_effect = (
                lambda name: two_board_setup['states'][name]
            )

            monitor._check_and_process_waiting_issues_failsafe()

        # ONE call covering both boards, not one per board.
        assert mock_batched.call_count == 1
        called_pairs = set(mock_batched.call_args[0][0])
        assert called_pairs == {two_board_setup['pair1'], two_board_setup['pair2']}

        # Each board's get_next_waiting_issue() received its own pre-fetched
        # data via the new optional parameter - no board went unfetched.
        calls = two_board_setup['queue_manager'].get_next_waiting_issue.call_args_list
        assert len(calls) == 2
        forwarded = {c.kwargs['prefetched_board_data'] is not None for c in calls}
        assert forwarded == {True}
        forwarded_values = [c.kwargs['prefetched_board_data'] for c in calls]
        assert data1 in forwarded_values
        assert data2 in forwarded_values

    def test_bounded_fallback_for_board_whose_batch_fetch_failed(self, two_board_setup):
        monitor = _make_monitor(two_board_setup['config_manager'], batched_flag=True)

        data2 = {'items': {'nodes': [{'id': 'b'}]}}
        # pair1's batched fetch failed; pair2's succeeded in the same batch.
        results = {two_board_setup['pair2']: data2}
        errors = {two_board_setup['pair1']: 'boom'}

        with patch(
            'services.pipeline_lock_manager.get_pipeline_lock_manager',
            return_value=two_board_setup['lock_manager'],
        ), patch(
            'services.pipeline_queue_manager.get_pipeline_queue_manager',
            return_value=two_board_setup['queue_manager'],
        ), patch(
            'config.state_manager.state_manager'
        ) as mock_state_manager, patch(
            'services.github_owner_utils.execute_batched_board_queries',
            return_value=(results, errors),
        ) as mock_batched:
            mock_state_manager.load_project_state.side_effect = (
                lambda name: two_board_setup['states'][name]
            )

            # Must not raise - one bad board must not lose the rest of the cycle.
            monitor._check_and_process_waiting_issues_failsafe()

        assert mock_batched.call_count == 1

        calls = two_board_setup['queue_manager'].get_next_waiting_issue.call_args_list
        assert len(calls) == 2
        prefetched_values = [c.kwargs['prefetched_board_data'] for c in calls]
        # The failed board falls back to None (get_next_waiting_issue fetches
        # it itself, the normal single-board way); the healthy board still
        # got its pre-fetched data.
        assert None in prefetched_values
        assert data2 in prefetched_values

    def test_missing_project_state_falls_back_for_that_board_only(self, two_board_setup):
        """A board whose project state can't be resolved is simply dropped
        from the batch gathering pass (mirrors get_issues_in_column_order()'s
        own tolerant handling of missing state) - it still gets checked via
        the normal single-board fallback, and the other board is unaffected."""
        monitor = _make_monitor(two_board_setup['config_manager'], batched_flag=True)

        data1 = {'items': {'nodes': [{'id': 'a'}]}}
        results = {two_board_setup['pair1']: data1}

        with patch(
            'services.pipeline_lock_manager.get_pipeline_lock_manager',
            return_value=two_board_setup['lock_manager'],
        ), patch(
            'services.pipeline_queue_manager.get_pipeline_queue_manager',
            return_value=two_board_setup['queue_manager'],
        ), patch(
            'config.state_manager.state_manager'
        ) as mock_state_manager, patch(
            'services.github_owner_utils.execute_batched_board_queries',
            return_value=(results, {}),
        ) as mock_batched:
            # project2 has no resolvable state at all.
            mock_state_manager.load_project_state.side_effect = (
                lambda name: two_board_setup['states']['project1'] if name == 'project1' else None
            )

            monitor._check_and_process_waiting_issues_failsafe()

        # Only project1's board was gatherable, so only its pair was queried.
        assert mock_batched.call_count == 1
        called_pairs = set(mock_batched.call_args[0][0])
        assert called_pairs == {two_board_setup['pair1']}

        calls = two_board_setup['queue_manager'].get_next_waiting_issue.call_args_list
        assert len(calls) == 2
        prefetched_values = [c.kwargs['prefetched_board_data'] for c in calls]
        assert data1 in prefetched_values
        assert None in prefetched_values  # project2's board - ungatherable, falls back

    def test_circuit_breaker_open_skips_batched_prefetch_entirely(self, two_board_setup):
        """Matches _fetch_boards_batched()'s own breaker check: while the
        breaker is open, skip the batched pre-fetch entirely rather than
        issuing a query graphql() would just short-circuit anyway (which
        would otherwise log a spurious failure warning for every board,
        every cycle, for the duration of the outage). Every board falls
        back to its normal individual fetch."""
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
            'config.state_manager.state_manager'
        ) as mock_state_manager, patch(
            'services.project_monitor.get_github_client',
            return_value=mock_github_client,
        ), patch(
            'services.github_owner_utils.execute_batched_board_queries'
        ) as mock_batched:
            mock_state_manager.load_project_state.side_effect = (
                lambda name: two_board_setup['states'][name]
            )

            monitor._check_and_process_waiting_issues_failsafe()

        mock_batched.assert_not_called()
        calls = two_board_setup['queue_manager'].get_next_waiting_issue.call_args_list
        assert len(calls) == 2
        assert all(c.kwargs['prefetched_board_data'] is None for c in calls)

    def test_unexpected_error_during_gathering_falls_back_not_whole_cycle_skip(self, two_board_setup):
        """
        Regression test: before this fix, an unhandled exception anywhere in
        the gathering/batched-fetch step (e.g. a corrupted state file)
        propagated to this method's outer except and skipped STEP 2 & 3 -
        every project/board's waiting-issue processing - for the whole
        cycle, not just the one board that failed. It must instead degrade
        to prefetched_board_data={} (every board falls back to its own
        individual fetch, identical to the flag being off this cycle) while
        STEP 2 & 3 still runs for every board.
        """
        monitor = _make_monitor(two_board_setup['config_manager'], batched_flag=True)

        with patch(
            'services.pipeline_lock_manager.get_pipeline_lock_manager',
            return_value=two_board_setup['lock_manager'],
        ), patch(
            'services.pipeline_queue_manager.get_pipeline_queue_manager',
            return_value=two_board_setup['queue_manager'],
        ), patch(
            'config.state_manager.state_manager'
        ) as mock_state_manager:
            # Simulate a corrupted/unreadable state file during gathering.
            mock_state_manager.load_project_state.side_effect = RuntimeError("corrupted state file")

            # Must not raise, and STEP 2 & 3 must still run for both boards.
            monitor._check_and_process_waiting_issues_failsafe()

        calls = two_board_setup['queue_manager'].get_next_waiting_issue.call_args_list
        assert len(calls) == 2  # both boards still processed, not skipped
        assert all(c.kwargs['prefetched_board_data'] is None for c in calls)


class TestFailsafeBatchedGatheringUnflagged:
    """USE_BATCHED_BOARD_QUERIES unset (default False): the pre-fetch step
    must be a complete no-op, preserving pre-#100 behavior exactly."""

    def test_batched_call_never_made_when_flag_off(self, two_board_setup):
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
