"""
Regression tests for the first failure in pipeline run 4cf816cf: card moves
bypassing the GitHub circuit breaker.

PipelineProgression.move_issue_to_column() used to run its Projects v2 queries
and its status mutation as bare `subprocess.run(['gh', 'api', 'graphql', ...],
check=True)` calls. That put the single most consequential GitHub write in the
system outside every protection GitHubAPIClient provides:

  * the circuit breaker — so while the breaker was open on an exhausted GraphQL
    budget and correctly refusing every OTHER caller in the process, this path
    kept firing into the same exhausted quota;
  * the adaptive throttle that backs off at 80/90/95% usage;
  * the rate-limit accounting, so these calls were invisible to the budget the
    rest of the system reasons about;
  * and the error text, because `str(CalledProcessError)` drops stderr.

The result, in production, was three consecutive card-move failures logged as
"Command '[...]' returned non-zero exit status 1." and a board stopped until a
human intervened.

These tests pin the routing itself rather than any particular error string,
because "someone reintroduces a subprocess here" is the regression that
reproduces the whole incident.
"""

import os
import pytest
from unittest.mock import Mock, patch

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from services.pipeline_progression import PipelineProgression


ITEM_RESPONSE = {
    "repository": {
        "issue": {
            "projectItems": {
                "nodes": [{"id": "PVTI_item", "project": {"number": 27}}]
            }
        }
    }
}


@pytest.fixture
def progression():
    with patch('services.pipeline_progression.get_pipeline_lock_manager'), \
         patch('monitoring.observability.get_observability_manager'):
        return PipelineProgression(task_queue=Mock())


@pytest.fixture
def board_state():
    column = Mock()
    column.name = 'Testing'
    column.id = 'opt_testing'

    board = Mock()
    board.project_number = 27
    board.project_id = 'PVT_board'
    board.status_field_id = 'PVTSSF_status'
    board.columns = [column]
    return board


@pytest.fixture
def wired(progression, board_state):
    """move_issue_to_column() with config/state stubbed out, so only the GitHub
    call path is under test."""
    project_config = Mock()
    project_config.github = {'org': 'tinkermonkey', 'repo': 'codetoreum'}

    project_state = Mock()
    project_state.boards = {'SDLC Execution': board_state}

    with patch('services.pipeline_progression.config_manager') as cfg, \
         patch('services.pipeline_progression.state_manager') as state, \
         patch('services.work_execution_state.work_execution_tracker'), \
         patch.object(progression, 'decision_events'):
        cfg.get_project_config.return_value = project_config
        state.load_project_state.return_value = project_state
        yield progression


def _move(progression):
    return progression.move_issue_to_column(
        project_name='codetoreum',
        board_name='SDLC Execution',
        issue_number=1045,
        target_column='Testing',
        trigger='review_cycle_completion',
    )


class TestTheMoveGoesThroughTheRateLimitedClient:
    def test_a_successful_move_makes_no_gh_subprocess_call(self, wired):
        client = Mock()
        client.graphql.side_effect = [
            (True, ITEM_RESPONSE),   # current-status probe
            (True, ITEM_RESPONSE),   # item id lookup
            (True, {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "x"}}}),
        ]

        with patch('services.github_api_client.get_github_client', return_value=client), \
             patch('services.pipeline_progression.subprocess.run') as mock_run:
            assert _move(wired) is True

        assert client.graphql.called, "the move must go through GitHubAPIClient"
        for call in mock_run.call_args_list:
            argv = call.args[0] if call.args else []
            assert 'graphql' not in argv, (
                "no raw `gh api graphql` subprocess may bypass the breaker here"
            )

    def test_an_open_breaker_fails_the_move_instead_of_calling_github(self, wired):
        """The breaker's refusal shape. Previously this path never asked."""
        client = Mock()
        client.graphql.return_value = (
            False, {"error": "GitHub API rate limit exceeded - circuit breaker open"}
        )

        with patch('services.github_api_client.get_github_client', return_value=client):
            assert _move(wired) is False

    def test_the_failure_reason_reaches_the_decision_event(self, wired):
        """Not "returned non-zero exit status 1". The whole point of routing
        through the client is that the reason survives to the operator."""
        client = Mock()
        client.graphql.return_value = (
            False, {"error": "rate_limited", "details": "API rate limit exceeded"}
        )

        with patch('services.github_api_client.get_github_client', return_value=client):
            _move(wired)

        errors = [
            call.kwargs.get('error', '')
            for call in wired.decision_events.emit_status_progression.call_args_list
            if call.kwargs.get('success') is False
        ]
        assert errors, "a failed move must emit a failed status_progression event"
        assert any('rate limit' in e.lower() for e in errors)
        assert not any('returned non-zero exit status' in e for e in errors)

    def test_a_rate_limited_move_says_so_rather_than_leaving_it_to_be_inferred(
        self, wired
    ):
        """The incident's cause had to be reconstructed from adjacent log lines
        hours later. It is now in the error itself."""
        client = Mock()
        client.graphql.side_effect = [
            (True, ITEM_RESPONSE),
            (True, ITEM_RESPONSE),
            (False, {"error": "rate_limited", "details": "API rate limit exceeded"}),
        ]

        with patch('services.github_api_client.get_github_client', return_value=client):
            assert _move(wired) is False

        errors = [
            call.kwargs.get('error', '')
            for call in wired.decision_events.emit_status_progression.call_args_list
            if call.kwargs.get('success') is False
        ]
        assert any('quota exhaustion' in e for e in errors)

    def test_a_rate_limited_mutation_stops_retrying_inside_the_window(self, wired):
        """Three attempts against an exhausted quota is three guaranteed
        rejections. The caller owns the decision to wait for the window — see
        card_move_retry_delay()."""
        client = Mock()
        client.graphql.side_effect = [
            (True, ITEM_RESPONSE),
            (True, ITEM_RESPONSE),
            (False, {"error": "rate_limited", "details": "API rate limit exceeded"}),
        ]

        with patch('services.github_api_client.get_github_client', return_value=client):
            _move(wired)

        # Exactly three: the two reads plus ONE mutation attempt.
        assert client.graphql.call_count == 3

    def test_an_ordinary_mutation_failure_still_retries(self, wired):
        """Control: the rate-limit short-circuit must not disable retries for a
        genuine transient blip."""
        client = Mock()
        client.graphql.side_effect = [
            (True, ITEM_RESPONSE),
            (True, ITEM_RESPONSE),
            (False, {"error": "server_error", "stderr": "HTTP 502"}),
            (False, {"error": "server_error", "stderr": "HTTP 502"}),
            (True, {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "x"}}}),
        ]

        with patch('services.github_api_client.get_github_client', return_value=client), \
             patch('services.pipeline_progression.time.sleep'):
            assert _move(wired) is True

        assert client.graphql.call_count == 5

    def test_a_missing_project_item_is_still_reported_as_such(self, wired):
        """The pre-existing "issue is not on this board" diagnosis must survive
        the client migration — it is a different fix from a quota problem."""
        empty = {"repository": {"issue": {"projectItems": {"nodes": []}}}}
        client = Mock()
        client.graphql.side_effect = [(True, empty), (True, empty)]

        with patch('services.github_api_client.get_github_client', return_value=client):
            assert _move(wired) is False
