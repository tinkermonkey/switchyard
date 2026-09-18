"""
Tests for WorkBreakdownAgent's migration onto GitHubAPIClient.gh_cli()
(GitHub circuit breaker consolidation).

Before: ~10 raw `subprocess.run(['gh', ...])` calls across
_advance_parent_to_in_development, _post_comment, and _create_sub_issues,
none of them breaker-protected. After: all routed through gh_cli().

Unlike tests/unit/agents/test_work_breakdown_parsing.py, this file does not
module-skip on missing /app -- WorkBreakdownAgent instantiates fine locally
once ConfigManager/GitHubStateManager are patched (confirmed directly before
writing this file), and these tests need to actually run to mean anything.
"""
from unittest.mock import patch, MagicMock

import pytest

from services.github_api_client import GitHubBreaker, get_github_client


@pytest.fixture
def agent():
    with patch('agents.work_breakdown_agent.ConfigManager'), \
         patch('agents.work_breakdown_agent.GitHubStateManager'):
        from agents.work_breakdown_agent import WorkBreakdownAgent
        return WorkBreakdownAgent()


@pytest.fixture(autouse=True)
def reset_breaker():
    client = get_github_client()
    client.breaker.state = GitHubBreaker.CLOSED
    client.breaker._generic_failure_count = 0
    client.breaker.trip_reason = None
    yield
    client.breaker.state = GitHubBreaker.CLOSED
    client.breaker._generic_failure_count = 0
    client.breaker.trip_reason = None


def _mock_result(stdout="", returncode=0, stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestPostComment:
    def test_success_calls_gh_issue_comment(self, agent):
        agent.config_manager.get_project_config.return_value = MagicMock(
            github={'org': 'acme', 'repo': 'widgets'}
        )
        task_context = {'workspace_type': 'issues', 'issue_number': 42}

        with patch('subprocess.run', return_value=_mock_result()) as mock_run, \
             patch('monitoring.decision_events.get_decision_event_emitter'):
            agent._post_comment(task_context, 'acme-project', 'hello world')

        cmd = mock_run.call_args.args[0]
        assert cmd == ['gh', 'issue', 'comment', '42', '--repo', 'acme/widgets', '--body', 'hello world']

    def test_open_breaker_short_circuits_without_calling_subprocess(self, agent):
        agent.config_manager.get_project_config.return_value = MagicMock(
            github={'org': 'acme', 'repo': 'widgets'}
        )
        task_context = {'workspace_type': 'issues', 'issue_number': 42}
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None

        with patch('subprocess.run') as mock_run:
            # _post_comment swallows all exceptions and just logs -- the
            # breaker-open path must not raise out of this call.
            agent._post_comment(task_context, 'acme-project', 'hello world')

        mock_run.assert_not_called()


class TestAdvanceParentToInDevelopment:
    def _make_github_state(self):
        planning_board = MagicMock()
        planning_board.project_number = 11
        planning_board.project_id = "PLANNING_PROJECT_ID"
        planning_board.status_field_id = "STATUS_FIELD_ID"
        in_dev_column = MagicMock(name="In Development", id="IN_DEV_OPTION_ID")
        in_dev_column.name = "In Development"
        planning_board.columns = [in_dev_column]

        state = MagicMock()
        state.boards = {"Planning & Design": planning_board}
        state.discussion_issue_links = {}
        return state

    def test_success_advances_via_two_graphql_calls(self, agent):
        state = self._make_github_state()
        agent.state_manager.load_project_state.return_value = state
        agent.config_manager.get_project_config.return_value = MagicMock(
            github={'org': 'acme', 'repo': 'widgets'}
        )
        task_context = {'workspace_type': 'issues', 'issue_number': 7}

        query_response = _mock_result(stdout='{"data": {"repository": {"issue": {"projectItems": {"nodes": '
                                              '[{"id": "ITEM_ID", "project": {"number": 11}}]}}}}}')
        mutation_response = _mock_result(stdout='{}')

        with patch('subprocess.run', side_effect=[query_response, mutation_response]) as mock_run:
            agent._advance_parent_to_in_development(task_context, 'acme-project')

        assert mock_run.call_count == 2

    def test_open_breaker_short_circuits_without_calling_subprocess(self, agent):
        state = self._make_github_state()
        agent.state_manager.load_project_state.return_value = state
        agent.config_manager.get_project_config.return_value = MagicMock(
            github={'org': 'acme', 'repo': 'widgets'}
        )
        task_context = {'workspace_type': 'issues', 'issue_number': 7}
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None

        with patch('subprocess.run') as mock_run:
            agent._advance_parent_to_in_development(task_context, 'acme-project')

        mock_run.assert_not_called()


class TestCreateSubIssues:
    def _make_state_and_config(self):
        backlog_column = MagicMock()
        backlog_column.name = "Backlog"
        backlog_column.id = "BACKLOG_OPTION_ID"

        sdlc_board = MagicMock()
        sdlc_board.project_number = 22
        sdlc_board.project_id = "SDLC_PROJECT_ID"
        sdlc_board.status_field_id = "STATUS_FIELD_ID"
        sdlc_board.columns = [backlog_column]

        state = MagicMock()
        state.boards = {"SDLC Execution": sdlc_board}

        project_config = MagicMock(github={'org': 'acme', 'repo': 'widgets'})
        return state, project_config, sdlc_board

    @pytest.mark.asyncio
    async def test_full_happy_path_with_parent_linking(self, agent):
        state, project_config, sdlc_board = self._make_state_and_config()
        agent.state_manager.load_project_state.return_value = state
        agent.config_manager.get_project_config.return_value = project_config

        sub_issues = [{
            'title': 'Phase 1: Setup',
            'body': 'Do the setup',
            'phase': 'Phase 1: Setup',
            'dependencies': 'None',
        }]
        task_context = {'issue_number': 1, 'pipeline_run_id': 'run-1'}

        responses = [
            _mock_result(stdout='{"id": "PARENT_NODE_ID"}'),  # parent issue id lookup
            _mock_result(stdout='[]'),  # existing-issue search: none found
            _mock_result(stdout='https://github.com/acme/widgets/issues/123\n'),  # gh issue create
            _mock_result(stdout='{"id": "CHILD_NODE_ID", "number": 123, "url": "https://github.com/acme/widgets/issues/123"}'),  # view poll
            _mock_result(stdout=''),  # project item-add
            _mock_result(stdout='{"data": {"repository": {"issue": {"projectItems": {"nodes": '
                                 '[{"id": "SDLC_ITEM_ID", "project": {"number": 22, "id": "SDLC_PROJECT_ID", "title": "SDLC Execution"}}]}}}}}'),  # project items query
            _mock_result(stdout='{}'),  # status mutation
            _mock_result(stdout='{}'),  # addSubIssue mutation
        ]

        with patch('subprocess.run', side_effect=responses) as mock_run, \
             patch('monitoring.decision_events.get_decision_event_emitter'):
            created = await agent._create_sub_issues(sub_issues, task_context, 'acme-project')

        assert len(created) == 1
        assert created[0]['number'] == '123'
        assert mock_run.call_count == 8

        # The addSubIssue mutation (the last call) must carry the custom header.
        last_cmd = mock_run.call_args_list[-1].args[0]
        assert '-H' in last_cmd
        assert 'GraphQL-Features: sub_issues' in last_cmd

    @pytest.mark.asyncio
    async def test_open_breaker_short_circuits_creation_without_calling_subprocess(self, agent):
        """The whole point of the migration: a sustained GitHub outage now
        stops this loop cold instead of hammering `gh` ~8 times per sub-issue."""
        state, project_config, sdlc_board = self._make_state_and_config()
        agent.state_manager.load_project_state.return_value = state
        agent.config_manager.get_project_config.return_value = project_config

        sub_issues = [{
            'title': 'Phase 1: Setup',
            'body': 'Do the setup',
            'phase': 'Phase 1: Setup',
            'dependencies': 'None',
        }]
        task_context = {'issue_number': 1, 'pipeline_run_id': 'run-1'}

        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None

        with patch('subprocess.run') as mock_run, \
             patch('monitoring.decision_events.get_decision_event_emitter'):
            created = await agent._create_sub_issues(sub_issues, task_context, 'acme-project')

        assert created == []
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_issue_found_skips_creation(self, agent):
        """search finding an exact title match should reuse the existing issue
        rather than creating a duplicate (the 'zombie run' dedup path)."""
        state, project_config, sdlc_board = self._make_state_and_config()
        agent.state_manager.load_project_state.return_value = state
        agent.config_manager.get_project_config.return_value = project_config

        sub_issues = [{
            'title': 'Phase 1: Setup',
            'body': 'Do the setup',
            'phase': 'Phase 1: Setup',
            'dependencies': 'None',
        }]
        task_context = {'issue_number': None, 'pipeline_run_id': 'run-1'}

        responses = [
            _mock_result(stdout='[{"number": 99, "title": "Phase 1: Setup", "id": "EXISTING_ID", '
                                 '"url": "https://github.com/acme/widgets/issues/99"}]'),  # existing-issue search: match
            _mock_result(stdout=''),  # project item-add
            _mock_result(stdout='{"data": {"repository": {"issue": {"projectItems": {"nodes": []}}}}}'),  # project items query (not in SDLC yet)
        ]

        with patch('subprocess.run', side_effect=responses) as mock_run, \
             patch('monitoring.decision_events.get_decision_event_emitter'):
            created = await agent._create_sub_issues(sub_issues, task_context, 'acme-project')

        assert len(created) == 1
        assert created[0]['number'] == '99'
        # No parent_issue_number and no parent_issue_id -- addSubIssue is
        # never attempted, so only 3 calls total (no create/view-poll either).
        assert mock_run.call_count == 3
