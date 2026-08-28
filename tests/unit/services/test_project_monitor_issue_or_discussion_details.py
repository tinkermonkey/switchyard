"""
Unit tests for ProjectMonitor.get_issue_or_discussion_details()

Covers the branches introduced to fix the bug where get_issue_details()
(which shells out to `gh issue view`) silently returned an empty placeholder
for items that are actually GitHub Discussions, tripping the downstream
empty-description guard and halting pipelines (see PR #85).
"""
import pytest
from unittest.mock import Mock, MagicMock, patch
from services.project_monitor import ProjectMonitor
from config.manager import ConfigManager


@pytest.fixture
def mock_config_manager():
    config_manager = Mock(spec=ConfigManager)
    config_manager.list_projects.return_value = []
    return config_manager


@pytest.fixture
def project_monitor(mock_config_manager):
    monitor = ProjectMonitor(Mock(), mock_config_manager)
    monitor.discussions = Mock()
    return monitor


class TestDiscussionResolvedAndFetched:
    """A linked discussion is found and its content is fetched successfully."""

    def test_uses_discussion_content_and_skips_get_issue_details(self, project_monitor):
        project_monitor.discussions.get_discussion.return_value = {
            'title': 'Real Discussion Title',
            'body': 'Real discussion body with actual content.',
            'url': 'https://github.com/org/repo/discussions/5',
            'number': 5,
            'closed': False,
            'author': {'login': 'someuser'},
            'createdAt': '2026-01-01T00:00:00Z',
            'updatedAt': '2026-01-02T00:00:00Z',
        }
        project_monitor.get_issue_details = Mock(side_effect=AssertionError(
            "get_issue_details() should not be called when a discussion is linked and fetched"
        ))

        result = project_monitor.get_issue_or_discussion_details(
            'repo', 941, 'org', discussion_id='D_abc123'
        )

        assert result['title'] == 'Real Discussion Title'
        assert result['body'] == 'Real discussion body with actual content.'
        assert result['url'] == 'https://github.com/org/repo/discussions/5'
        assert result['labels'] == []
        project_monitor.discussions.get_discussion.assert_called_once_with('D_abc123')

    def test_open_discussion_maps_to_open_state(self, project_monitor):
        project_monitor.discussions.get_discussion.return_value = {
            'title': 'T', 'body': 'B', 'closed': False,
        }
        result = project_monitor.get_issue_or_discussion_details(
            'repo', 941, 'org', discussion_id='D_abc123'
        )
        assert result['state'] == 'OPEN'

    def test_closed_discussion_maps_to_closed_state(self, project_monitor):
        """Regression guard: a discussion-linked issue must still be detectable as
        CLOSED by callers gating on issue_data.get('state') (e.g. trigger_agent_for_status,
        create_feedback_task) — dropping this previously made that check a silent no-op
        for every discussion-linked issue."""
        project_monitor.discussions.get_discussion.return_value = {
            'title': 'T', 'body': 'B', 'closed': True,
        }
        result = project_monitor.get_issue_or_discussion_details(
            'repo', 941, 'org', discussion_id='D_abc123'
        )
        assert result['state'] == 'CLOSED'

    def test_explicit_discussion_id_skips_state_manager_lookup(self, project_monitor):
        """Callers that already know the discussion id (e.g.
        create_feedback_task_for_discussion) should not pay for a redundant
        state_manager lookup."""
        project_monitor.discussions.get_discussion.return_value = {'title': 'T', 'body': 'B'}

        with patch('config.state_manager.state_manager') as mock_state:
            project_monitor.get_issue_or_discussion_details(
                'repo', 941, 'org', project_name='proj', discussion_id='D_abc123'
            )
            mock_state.get_discussion_for_issue.assert_not_called()


class TestDiscussionLinkedButFetchFails:
    """A discussion is linked, but fetching its content fails (deleted, API error)."""

    def test_falls_back_to_get_issue_details(self, project_monitor):
        project_monitor.discussions.get_discussion.return_value = None
        project_monitor.get_issue_details = Mock(return_value={'title': 'Fallback', 'body': 'x'})

        result = project_monitor.get_issue_or_discussion_details(
            'repo', 941, 'org', discussion_id='D_deleted'
        )

        assert result == {'title': 'Fallback', 'body': 'x'}
        project_monitor.get_issue_details.assert_called_once_with('repo', 941, 'org')


class TestNoDiscussionLinked:
    """No discussion is linked at all — behaves exactly like get_issue_details()."""

    def test_no_discussion_id_and_no_project_name_calls_get_issue_details_directly(self, project_monitor):
        project_monitor.get_issue_details = Mock(return_value={'title': 'Real Issue', 'body': 'x'})

        result = project_monitor.get_issue_or_discussion_details('repo', 941, 'org')

        assert result == {'title': 'Real Issue', 'body': 'x'}
        project_monitor.discussions.get_discussion.assert_not_called()

    def test_state_manager_reports_no_linked_discussion_calls_get_issue_details(self, project_monitor):
        project_monitor.get_issue_details = Mock(return_value={'title': 'Real Issue', 'body': 'x'})

        with patch('config.state_manager.state_manager') as mock_state:
            mock_state.get_discussion_for_issue.return_value = None

            result = project_monitor.get_issue_or_discussion_details(
                'repo', 941, 'org', project_name='proj'
            )

        assert result == {'title': 'Real Issue', 'body': 'x'}
        project_monitor.discussions.get_discussion.assert_not_called()


class TestStateManagerLookupFails:
    """An unexpected error while checking for a linked discussion must not
    propagate — it should degrade to the get_issue_details() fallback."""

    def test_unexpected_exception_falls_back_to_get_issue_details(self, project_monitor):
        project_monitor.get_issue_details = Mock(return_value={'title': 'Real Issue', 'body': 'x'})

        with patch('config.state_manager.state_manager') as mock_state:
            mock_state.get_discussion_for_issue.side_effect = OSError("disk full")

            result = project_monitor.get_issue_or_discussion_details(
                'repo', 941, 'org', project_name='proj'
            )

        assert result == {'title': 'Real Issue', 'body': 'x'}
        project_monitor.discussions.get_discussion.assert_not_called()
