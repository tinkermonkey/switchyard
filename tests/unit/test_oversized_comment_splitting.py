"""
Unit tests for oversized GitHub comment splitting.

Verifies that comments exceeding GitHub's 65,536 character limit are
automatically split into multiple sequential comments.
"""

import pytest
from unittest.mock import patch, MagicMock, call

from services.github_integration import GitHubIntegration, GITHUB_MAX_COMMENT_LENGTH


class TestSplitOversizedComment:
    """Tests for GitHubIntegration._split_oversized_comment"""

    def test_under_limit_returns_single_chunk(self):
        """Comment under the limit is returned as a single-element list."""
        comment = "Short comment"
        result = GitHubIntegration._split_oversized_comment(comment)
        assert result == [comment]

    def test_exact_limit_returns_single_chunk(self):
        """Comment exactly at the limit is returned as a single-element list."""
        comment = "x" * GITHUB_MAX_COMMENT_LENGTH
        result = GitHubIntegration._split_oversized_comment(comment)
        assert result == [comment]

    def test_over_limit_splits_into_multiple_chunks(self):
        """Comment over the limit is split into multiple chunks."""
        comment = "x" * (GITHUB_MAX_COMMENT_LENGTH + 1000)
        result = GitHubIntegration._split_oversized_comment(comment)
        assert len(result) > 1

    def test_all_chunks_within_limit(self):
        """Every chunk must fit within the max length."""
        comment = "x" * (GITHUB_MAX_COMMENT_LENGTH * 3)
        result = GitHubIntegration._split_oversized_comment(comment)
        for chunk in result:
            assert len(chunk) <= GITHUB_MAX_COMMENT_LENGTH

    def test_split_at_newline(self):
        """Splitting should prefer newline boundaries over arbitrary positions."""
        # Build a comment with clear newline structure
        line = "a" * 100 + "\n"
        # Fill to just over the limit
        num_lines = (GITHUB_MAX_COMMENT_LENGTH // len(line)) + 10
        comment = line * num_lines

        result = GitHubIntegration._split_oversized_comment(comment)
        assert len(result) > 1
        # First chunk (after removing header) should end with the continuation footer
        assert "Continued in next comment..." in result[0]

    def test_part_headers_present(self):
        """Multi-part comments should have part N/M headers."""
        comment = "line\n" * (GITHUB_MAX_COMMENT_LENGTH // 4)
        result = GitHubIntegration._split_oversized_comment(comment)
        if len(result) > 1:
            total = len(result)
            for i, chunk in enumerate(result):
                assert f"**(Part {i+1}/{total})**" in chunk

    def test_continuation_footer_on_non_final_chunks(self):
        """Non-final chunks should have a continuation footer."""
        comment = "x\n" * GITHUB_MAX_COMMENT_LENGTH
        result = GitHubIntegration._split_oversized_comment(comment)
        assert len(result) > 1
        for chunk in result[:-1]:
            assert "Continued in next comment..." in chunk
        # Final chunk should NOT have the footer
        assert "Continued in next comment..." not in result[-1]

    def test_no_content_lost(self):
        """All original content should be present across chunks (minus formatting overhead)."""
        # Use simple content with newlines
        original_lines = [f"Line {i}\n" for i in range(1000)]
        comment = "".join(original_lines)
        result = GitHubIntegration._split_oversized_comment(comment)

        # Reconstruct content by stripping part headers and footers
        reconstructed = ""
        for chunk in result:
            # Strip part header
            lines = chunk.split("\n")
            start = 0
            if lines[0].startswith("**(Part"):
                start = 2  # Skip header line and blank line
            # Strip continuation footer
            content_lines = lines[start:]
            footer_lines = ["---", "*Continued in next comment...*"]
            while content_lines and content_lines[-1].strip() in footer_lines + [""]:
                content_lines.pop()
            reconstructed += "\n".join(content_lines) + "\n"

        # Every original line should appear in the reconstruction
        for line in original_lines[:50]:  # Spot-check first 50
            assert line.strip() in reconstructed

    def test_custom_max_length(self):
        """Custom max_length parameter should be respected."""
        comment = "a" * 500 + "\n" + "b" * 500
        result = GitHubIntegration._split_oversized_comment(comment, max_length=200)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= 200

    def test_no_newlines_hard_split(self):
        """Comment with no newlines should hard-split at effective limit."""
        comment = "x" * (GITHUB_MAX_COMMENT_LENGTH + 5000)
        result = GitHubIntegration._split_oversized_comment(comment)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= GITHUB_MAX_COMMENT_LENGTH

    def test_empty_comment(self):
        """Empty comment returns single-element list."""
        result = GitHubIntegration._split_oversized_comment("")
        assert result == [""]


class TestPostIssueCommentOversized:
    """Tests for post_issue_comment with oversized comments."""

    @pytest.fixture
    def github_integration(self):
        """Create GitHubIntegration with mocked auth."""
        with patch('services.github_app_auth.get_github_app_auth') as mock_auth:
            mock_auth_instance = MagicMock()
            mock_auth_instance.is_configured.return_value = False
            mock_auth.return_value = mock_auth_instance
            gi = GitHubIntegration(repo_owner="test-org", repo_name="test-repo")
            gi.github_org = "test-org"
            return gi

    @pytest.mark.asyncio
    async def test_normal_comment_single_api_call(self, github_integration):
        """Normal-sized comment should result in exactly one API call."""
        with patch('services.github_integration.get_github_client') as mock_client_fn:
            mock_client = MagicMock()
            mock_client.rest.return_value = (True, {'html_url': 'http://test', 'id': 1})
            mock_client_fn.return_value = mock_client

            result = await github_integration.post_issue_comment(1, "Short comment")

            assert result['success'] is True
            assert mock_client.rest.call_count == 1

    @pytest.mark.asyncio
    async def test_oversized_comment_multiple_api_calls(self, github_integration):
        """Oversized comment should result in multiple sequential API calls."""
        oversized = "x\n" * GITHUB_MAX_COMMENT_LENGTH  # Well over the limit

        with patch('services.github_integration.get_github_client') as mock_client_fn:
            mock_client = MagicMock()
            mock_client.rest.return_value = (True, {'html_url': 'http://test', 'id': 1})
            mock_client_fn.return_value = mock_client

            result = await github_integration.post_issue_comment(1, oversized)

            assert result['success'] is True
            assert mock_client.rest.call_count > 1
            # Verify each call posts a chunk that fits within the limit
            for c in mock_client.rest.call_args_list:
                data = c.kwargs.get('data') or c[1].get('data')
                assert len(data['body']) <= GITHUB_MAX_COMMENT_LENGTH

    @pytest.mark.asyncio
    async def test_oversized_first_chunk_failure_returns_failure(self, github_integration):
        """If the first chunk fails to post, the overall result should be failure."""
        oversized = "x\n" * GITHUB_MAX_COMMENT_LENGTH

        with patch('services.github_integration.get_github_client') as mock_client_fn:
            mock_client = MagicMock()
            mock_client.rest.return_value = (False, {'error': 'Server error'})
            mock_client_fn.return_value = mock_client

            result = await github_integration.post_issue_comment(1, oversized)

            assert result['success'] is False

    @pytest.mark.asyncio
    async def test_oversized_later_chunk_failure_returns_success(self, github_integration):
        """If a later chunk fails, the result should still be success (partial post)."""
        oversized = "x\n" * GITHUB_MAX_COMMENT_LENGTH

        with patch('services.github_integration.get_github_client') as mock_client_fn:
            mock_client = MagicMock()
            # First call succeeds, second fails
            mock_client.rest.side_effect = [
                (True, {'html_url': 'http://test', 'id': 1}),
                (False, {'error': 'Server error'}),
            ]
            mock_client_fn.return_value = mock_client

            result = await github_integration.post_issue_comment(1, oversized)

            assert result['success'] is True


class TestPostDiscussionCommentOversized:
    """Tests for _post_discussion_comment with oversized comments."""

    @pytest.fixture
    def github_integration(self):
        """Create GitHubIntegration with mocked auth."""
        with patch('services.github_app_auth.get_github_app_auth') as mock_auth:
            mock_auth_instance = MagicMock()
            mock_auth_instance.is_configured.return_value = False
            mock_auth.return_value = mock_auth_instance
            gi = GitHubIntegration(repo_owner="test-org", repo_name="test-repo")
            gi.github_org = "test-org"
            return gi

    @pytest.mark.asyncio
    async def test_oversized_discussion_comment_splits(self, github_integration):
        """Oversized discussion comment should result in multiple API calls."""
        oversized = "x\n" * GITHUB_MAX_COMMENT_LENGTH
        context = {'discussion_id': 'D_kwDtest123'}

        with patch('services.github_discussions.GitHubDiscussions') as MockDiscussions:
            mock_disc = MagicMock()
            mock_disc.add_discussion_comment.return_value = "comment_id_1"
            MockDiscussions.return_value = mock_disc

            result = await github_integration._post_discussion_comment(context, oversized, reply_to_id="reply_123")

            assert result['success'] is True
            assert mock_disc.add_discussion_comment.call_count > 1

            # First call should use reply_to_id, subsequent should not
            calls = mock_disc.add_discussion_comment.call_args_list
            assert calls[0][1]['reply_to_id'] == "reply_123"
            for call in calls[1:]:
                assert call[1]['reply_to_id'] is None
