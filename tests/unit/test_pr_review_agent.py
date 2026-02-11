"""
Unit tests for PR Review Agent

Tests recommendation parsing, severity grouping, context gap detection,
cycle limit enforcement, and clean pass detection.

These tests mock the agent's dependencies so they can run outside Docker.
"""

import json
import sys
from unittest.mock import patch, MagicMock
import pytest


# Pre-mock modules that fail outside Docker (DevContainerStateManager
# creates /app/state/dev_containers at module level)
_can_import = True
try:
    # Only pre-mock if not already imported (i.e., not running in Docker)
    if 'services.dev_container_state' not in sys.modules:
        sys.modules['services.dev_container_state'] = MagicMock()

    with patch('agents.pr_review_agent.ConfigManager'), \
         patch('agents.pr_review_agent.GitHubStateManager'):
        from agents.pr_review_agent import PRReviewAgent
except Exception:
    _can_import = False

pytestmark = pytest.mark.skipif(
    not _can_import,
    reason="Cannot import PRReviewAgent (requires Docker container environment)"
)


@pytest.fixture
def agent():
    """Create a PRReviewAgent with mocked dependencies."""
    with patch('agents.pr_review_agent.ConfigManager'), \
         patch('agents.pr_review_agent.GitHubStateManager'):
        return PRReviewAgent()


class TestParseReviewFindings:
    """Test parsing of PR review output into structured findings."""

    def test_parses_all_severity_levels(self, agent):
        output = """
## PR Review Findings

### Critical Issues
- **SQL Injection**: User input passed directly to query without sanitization

### High Priority Issues
- **Missing Auth Check**: The /admin endpoint lacks authentication middleware

### Medium Priority Issues
- **Unused Import**: `os` imported but not used in handler.py

### Low Priority / Nice-to-Have
- **Variable Naming**: Consider renaming `x` to something more descriptive
"""
        findings = agent._parse_review_findings(output, "PR Code Review")
        assert len(findings) == 4
        severities = [f['severity'] for f in findings]
        assert 'critical' in severities
        assert 'high' in severities
        assert 'medium' in severities
        assert 'low' in severities

    def test_skips_none_found_sections(self, agent):
        output = """
### Critical Issues
None found

### High Priority Issues
- **Missing Validation**: Input not validated

### Medium Priority Issues
None found

### Low Priority / Nice-to-Have
None found
"""
        findings = agent._parse_review_findings(output, "PR Code Review")
        assert len(findings) == 1
        assert findings[0]['severity'] == 'high'

    def test_returns_empty_for_clean_output(self, agent):
        output = """
### Critical Issues
None found

### High Priority Issues
None found

### Medium Priority Issues
None found

### Low Priority / Nice-to-Have
None found
"""
        findings = agent._parse_review_findings(output, "PR Code Review")
        assert len(findings) == 0

    def test_parses_gaps_and_deviations(self, agent):
        output = """
### Gaps Found
- **Missing Feature X**: Specified in requirements but not implemented

### Deviations
- **Different API Format**: Returns JSON array instead of paginated response

### Verified
- User authentication flow works as specified
"""
        findings = agent._parse_review_findings(output, "Business Analyst Output")
        assert len(findings) == 2
        titles = [f['title'] for f in findings]
        assert any('gaps' in t.lower() for t in titles)
        assert any('deviations' in t.lower() for t in titles)

    def test_issue_title_includes_source(self, agent):
        output = """
### Critical Issues
- **Bug**: Something is broken
"""
        findings = agent._parse_review_findings(output, "Parent Issue Requirements")
        assert len(findings) == 1
        assert "Parent Issue Requirements" in findings[0]['title']

    def test_handles_empty_output(self, agent):
        findings = agent._parse_review_findings("", "PR Code Review")
        assert len(findings) == 0


class TestExtractSectionItems:
    """Test extraction of content under section headings."""

    def test_extracts_section_content(self, agent):
        text = """
### Critical Issues
- Item 1
- Item 2

### High Priority Issues
- Item 3
"""
        content = agent._extract_section_items(text, "Critical Issues")
        assert "Item 1" in content
        assert "Item 2" in content
        assert "Item 3" not in content

    def test_returns_empty_for_missing_section(self, agent):
        text = "No relevant sections here"
        assert agent._extract_section_items(text, "Critical Issues") == ''


class TestIsNoneFound:
    """Test detection of 'no findings' markers."""

    def test_detects_none_found(self, agent):
        assert agent._is_none_found("None found") is True
        assert agent._is_none_found("None") is True
        assert agent._is_none_found("N/A") is True
        assert agent._is_none_found("none found") is True
        assert agent._is_none_found("- None found") is True

    def test_detects_none_found_with_trailing_explanation(self, agent):
        """Exact bug reproduction: 'None found' followed by explanatory text."""
        assert agent._is_none_found(
            '"None found" - The PR documentation indicates 3 critical issues '
            'were already identified and resolved during development'
        ) is True

    def test_detects_already_resolved_language(self, agent):
        # Only matches when "already/previously" appears at line-start
        assert agent._is_none_found(
            "Already resolved during the development phase"
        ) is True
        assert agent._is_none_found(
            "Previously addressed in earlier commits"
        ) is True

    def test_already_resolved_mid_sentence_not_matched(self, agent):
        """Mid-sentence 'already resolved' must not match (could be inside a finding)."""
        assert agent._is_none_found(
            "Issues were already resolved during the development phase"
        ) is False

    def test_detects_clean_pass_language(self, agent):
        assert agent._is_none_found("Clean pass - no issues to report") is True
        assert agent._is_none_found("No concerns with the implementation") is True

    def test_detects_no_issues_found_variants(self, agent):
        assert agent._is_none_found("No issues found in this section") is True
        assert agent._is_none_found("No critical issues found") is True
        assert agent._is_none_found("No gaps found") is True
        assert agent._is_none_found("No deviations found") is True

    def test_detects_all_requirements_verified(self, agent):
        assert agent._is_none_found("All requirements verified and implemented correctly") is True
        assert agent._is_none_found("All requirements met") is True
        assert agent._is_none_found("All requirements satisfied") is True

    def test_detects_no_actionable(self, agent):
        assert agent._is_none_found("No actionable items in this review") is True

    def test_does_not_false_positive_on_none_substring(self, agent):
        """'None of these are blocking' contains 'none' but is real content."""
        assert agent._is_none_found("None of these are blocking but they should be fixed") is False

    def test_does_not_match_phrases_inside_finding_descriptions(self, agent):
        """Anchored patterns must not match when the phrase is inside a finding."""
        assert agent._is_none_found(
            "- **Previously Resolved Bug Returned**: The fix for #123 was reverted"
        ) is False
        assert agent._is_none_found(
            "- **No Concern For Security**: Endpoint exposes user data without auth"
        ) is False
        assert agent._is_none_found(
            "- **No Actionable Error Messages**: API returns generic 500s"
        ) is False

    def test_rejects_actual_content(self, agent):
        assert agent._is_none_found("- **Bug**: Something is broken") is False
        assert agent._is_none_found("Multiple issues found") is False


class TestBuildPrReviewPrompt:
    """Test PR review prompt construction."""

    def test_includes_pr_url(self, agent):
        prompt = agent._build_pr_review_prompt("https://github.com/org/repo/pull/42")
        assert "https://github.com/org/repo/pull/42" in prompt

    def test_includes_skill_reference(self, agent):
        prompt = agent._build_pr_review_prompt("https://github.com/org/repo/pull/42")
        assert "/pr-review-toolkit:review-pr" in prompt


class TestBuildVerificationPrompt:
    """Test verification prompt construction."""

    def test_includes_context_content(self, agent):
        prompt = agent._build_verification_prompt(
            "https://github.com/org/repo/pull/42",
            "Business Analyst Output",
            "Users must be able to login with email"
        )
        assert "Users must be able to login with email" in prompt
        assert "Business Analyst Output" in prompt

    def test_truncates_long_content(self, agent):
        long_content = "x" * 20000
        prompt = agent._build_verification_prompt(
            "https://github.com/org/repo/pull/42",
            "Test",
            long_content
        )
        assert "[... truncated ...]" in prompt

    def test_does_not_truncate_short_content(self, agent):
        short_content = "Short requirements text"
        prompt = agent._build_verification_prompt(
            "https://github.com/org/repo/pull/42",
            "Test",
            short_content
        )
        assert "[... truncated ...]" not in prompt


class TestAgentProperties:
    """Test agent identity properties."""

    def test_display_name(self, agent):
        assert agent.agent_display_name == "PR Review Specialist"

    def test_output_sections(self, agent):
        sections = agent.output_sections
        assert "PR Code Review" in sections
        assert "Requirements Verification" in sections

    def test_name(self, agent):
        assert agent.name == "pr_review_agent"


class TestCycleLimitComment:
    """Test cycle limit comment generation."""

    def test_includes_cycle_info(self, agent):
        issues = [
            {'number': '101', 'title': 'Critical bug'},
            {'number': '102', 'title': 'Missing feature'},
        ]
        comment = agent._build_cycle_limit_comment(3, issues)
        assert "Cycle 3/3" in comment
        assert "Final" in comment
        assert "#101" in comment
        assert "#102" in comment

    def test_mentions_manual_triage(self, agent):
        comment = agent._build_cycle_limit_comment(3, [{'number': '101', 'title': 'Bug'}])
        assert "manual" in comment.lower()


class TestResolveParentIssueNumber:
    """Test parent issue number resolution from task context."""

    def test_resolves_from_direct_issue_number(self, agent):
        result = agent._resolve_parent_issue_number({'issue_number': 42}, 'project')
        assert result == 42

    def test_resolves_from_string_issue_number(self, agent):
        result = agent._resolve_parent_issue_number({'issue_number': '42'}, 'project')
        assert result == 42
        assert isinstance(result, int)

    def test_resolves_from_nested_task_context(self, agent):
        result = agent._resolve_parent_issue_number(
            {'task_context': {'issue_number': 42}}, 'project'
        )
        assert result == 42

    def test_returns_none_when_no_issue_number(self, agent):
        result = agent._resolve_parent_issue_number({}, 'project')
        assert result is None

    def test_returns_none_for_empty_nested_context(self, agent):
        result = agent._resolve_parent_issue_number({'task_context': {}}, 'project')
        assert result is None


class TestExecuteEarlyReturns:
    """Test execute() early-return paths (no parent issue, cycle limit, no PR)."""

    @pytest.mark.asyncio
    async def test_returns_early_when_no_parent_issue(self, agent):
        context = {'context': {}}
        result = await agent.execute(context)
        assert "Failed" in result['markdown_analysis']
        assert "parent issue" in result['markdown_analysis'].lower()

    @pytest.mark.asyncio
    async def test_raises_when_cycle_limit_reached(self, agent):
        from agents.non_retryable import NonRetryableAgentError
        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state:
            mock_state.get_review_count.return_value = 3
            context = {'context': {'issue_number': 42, 'project': 'test-project'}}
            with pytest.raises(NonRetryableAgentError, match="Review cycle limit"):
                await agent.execute(context)

    @pytest.mark.asyncio
    async def test_manual_trigger_resets_cycle_count(self, agent):
        from agents.non_retryable import NonRetryableAgentError
        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch.object(agent, '_find_pr_url', return_value=None), \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 3
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'test-org', 'repo': 'test-repo'}
            )
            context = {'context': {
                'issue_number': 42,
                'project': 'test-project',
                'trigger_source': 'manual',
            }}
            # Should NOT raise cycle limit — manual trigger resets cycle count
            # But SHOULD raise NonRetryableAgentError for no PR found
            with pytest.raises(NonRetryableAgentError, match="No PR found"):
                await agent.execute(context)
            mock_state.reset_review_count.assert_called_once_with('test-project', 42)

    @pytest.mark.asyncio
    async def test_manual_trigger_below_limit_does_not_reset(self, agent):
        from agents.non_retryable import NonRetryableAgentError
        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch.object(agent, '_find_pr_url', return_value=None), \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 1  # below limit
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'test-org', 'repo': 'test-repo'}
            )
            context = {'context': {
                'issue_number': 42,
                'project': 'test-project',
                'trigger_source': 'manual',
            }}
            with pytest.raises(NonRetryableAgentError, match="No PR found"):
                await agent.execute(context)
            mock_state.reset_review_count.assert_not_called()

    @pytest.mark.asyncio
    async def test_pipeline_progression_at_limit_raises(self, agent):
        from agents.non_retryable import NonRetryableAgentError
        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state:
            mock_state.get_review_count.return_value = 3
            context = {'context': {
                'issue_number': 42,
                'project': 'test-project',
                'trigger_source': 'pipeline_progression',
            }}
            with pytest.raises(NonRetryableAgentError, match="Review cycle limit"):
                await agent.execute(context)
            mock_state.reset_review_count.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_when_no_pr_found(self, agent):
        from agents.non_retryable import NonRetryableAgentError
        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch.object(agent, '_find_pr_url', return_value=None), \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 0
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'test-org', 'repo': 'test-repo'}
            )
            context = {'context': {'issue_number': 42, 'project': 'test-project'}}
            with pytest.raises(NonRetryableAgentError, match="No PR found"):
                await agent.execute(context)


class TestReturnParentToDevelopment:
    """Test moving parent issue back to 'In Development' when PR review finds issues."""

    def test_moves_parent_to_in_development(self, agent):
        """Verifies move_issue_to_column called with correct args."""
        mock_board = MagicMock()
        mock_state = MagicMock()
        mock_state.boards = {'Planning & Design': mock_board}
        agent.state_manager.load_project_state.return_value = mock_state

        with patch('services.pipeline_progression.PipelineProgression') as MockProgression, \
             patch('task_queue.task_manager.TaskQueue'):
            mock_progression = MockProgression.return_value
            mock_progression.move_issue_to_column.return_value = True

            agent._return_parent_to_development('test-project', 42)

            mock_progression.move_issue_to_column.assert_called_once_with(
                'test-project', 'Planning & Design', 42,
                'In Development', trigger='pr_review_issues_found'
            )

    def test_handles_missing_github_state(self, agent):
        """Should log warning and return gracefully when no GitHub state."""
        agent.state_manager.load_project_state.return_value = None

        # Should not raise
        agent._return_parent_to_development('test-project', 42)

    def test_handles_missing_planning_board(self, agent):
        """Should log warning and not attempt move when no Planning board found."""
        mock_state = MagicMock()
        mock_state.boards = {'SDLC Execution': MagicMock()}
        agent.state_manager.load_project_state.return_value = mock_state

        with patch('services.pipeline_progression.PipelineProgression') as MockProgression, \
             patch('task_queue.task_manager.TaskQueue'):
            mock_progression = MockProgression.return_value
            agent._return_parent_to_development('test-project', 42)
            mock_progression.move_issue_to_column.assert_not_called()

    def test_handles_move_failure_gracefully(self, agent):
        """Should log error but not raise when move fails."""
        mock_board = MagicMock()
        mock_state = MagicMock()
        mock_state.boards = {'Planning & Design': mock_board}
        agent.state_manager.load_project_state.return_value = mock_state

        with patch('services.pipeline_progression.PipelineProgression') as MockProgression, \
             patch('task_queue.task_manager.TaskQueue'):
            mock_progression = MockProgression.return_value
            mock_progression.move_issue_to_column.return_value = False

            # Should not raise
            agent._return_parent_to_development('test-project', 42)

    def test_handles_exception_gracefully(self, agent):
        """Should catch exceptions and not propagate them."""
        agent.state_manager.load_project_state.side_effect = RuntimeError("state error")

        # Should not raise
        agent._return_parent_to_development('test-project', 42)


class TestFormatIssueBody:
    """Test issue body formatting."""

    def test_includes_source_attribution(self, agent):
        body = agent._format_issue_body("Critical", "- Bug found", "PR Code Review")
        assert "PR Code Review" in body
        assert "PR Review Agent" in body

    def test_includes_severity_heading(self, agent):
        body = agent._format_issue_body("High", "- Missing check", "Test Source")
        assert "## High Findings" in body


class TestHasActionableFindings:
    """Test detection of structured findings in the expected format."""

    def test_detects_bold_title_format(self, agent):
        content = '- **SQL Injection**: User input not sanitized'
        assert agent._has_actionable_findings(content) is True

    def test_detects_multiple_findings(self, agent):
        content = (
            '- **Missing Auth**: No authentication on admin endpoint\n'
            '- **XSS Risk**: User input rendered without escaping'
        )
        assert agent._has_actionable_findings(content) is True

    def test_detects_asterisk_bullets(self, agent):
        content = '* **Unused Import**: `os` imported but not used'
        assert agent._has_actionable_findings(content) is True

    def test_rejects_none_found(self, agent):
        assert agent._has_actionable_findings("None found") is False

    def test_rejects_plain_text(self, agent):
        assert agent._has_actionable_findings("The code looks good overall") is False

    def test_rejects_explanatory_text(self, agent):
        content = (
            '"None found" - The PR documentation indicates 3 critical issues '
            'were already identified and resolved during development'
        )
        assert agent._has_actionable_findings(content) is False

    def test_rejects_unstructured_bullets(self, agent):
        content = '- Some plain bullet without bold title format'
        assert agent._has_actionable_findings(content) is False


class TestFalsePositivePrevention:
    """End-to-end tests for false-positive issue prevention in _parse_review_findings()."""

    def test_exact_bug_scenario_returns_zero_findings(self, agent):
        """Reproduce the exact bug from issue #355: 'None found' with trailing explanation."""
        output = """
## PR Review Findings

### Critical Issues
"None found" - The PR documentation indicates 3 critical issues were already identified and resolved during development

### High Priority Issues
None found

### Medium Priority Issues
None found

### Low Priority / Nice-to-Have
None found
"""
        findings = agent._parse_review_findings(output, "PR Code Review")
        assert len(findings) == 0

    def test_already_resolved_language_returns_zero_findings(self, agent):
        output = """
### Critical Issues
Issues were previously resolved in earlier commits

### High Priority Issues
All items already addressed during development

### Medium Priority Issues
None found

### Low Priority / Nice-to-Have
None found
"""
        findings = agent._parse_review_findings(output, "PR Code Review")
        assert len(findings) == 0

    def test_real_findings_still_detected(self, agent):
        """Regression test: real structured findings must still create issues."""
        output = """
### Critical Issues
- **SQL Injection**: User input passed directly to query without sanitization

### High Priority Issues
- **Missing Auth Check**: The /admin endpoint lacks authentication middleware

### Medium Priority Issues
None found

### Low Priority / Nice-to-Have
None found
"""
        findings = agent._parse_review_findings(output, "PR Code Review")
        assert len(findings) == 2
        severities = [f['severity'] for f in findings]
        assert 'critical' in severities
        assert 'high' in severities

    def test_gap_verification_false_positive_prevented(self, agent):
        """Gaps section with explanatory text but no structured findings."""
        output = """
### Gaps Found
No gaps found - all requirements from the business analyst were fully implemented

### Deviations
No deviations found

### Verified
- User authentication flow works correctly
- API response format matches specification
"""
        findings = agent._parse_review_findings(output, "Business Analyst Output")
        assert len(findings) == 0

    def test_real_gaps_still_detected(self, agent):
        """Regression test: real gaps must still create issues."""
        output = """
### Gaps Found
- **Missing Pagination**: API returns all results instead of paginated response as specified

### Deviations
- **Different Auth Method**: Uses session-based auth instead of JWT as designed

### Verified
- User registration flow works as specified
"""
        findings = agent._parse_review_findings(output, "Software Architect Output")
        assert len(findings) == 2
        titles = [f['title'] for f in findings]
        assert any('gaps' in t.lower() for t in titles)
        assert any('deviations' in t.lower() for t in titles)

    def test_unstructured_prose_prevented_by_actionable_check(self, agent):
        """Content that passes _is_none_found but fails _has_actionable_findings."""
        output = """
### Critical Issues
The code has some potential concerns around error handling that may warrant attention in future iterations.

### High Priority Issues
None found

### Medium Priority Issues
None found

### Low Priority / Nice-to-Have
None found
"""
        findings = agent._parse_review_findings(output, "PR Code Review")
        assert len(findings) == 0

    def test_structured_findings_not_suppressed_by_none_found_phrases(self, agent):
        """Real findings describing previously-resolved items must still create issues."""
        output = """
### Critical Issues
- **Previously Resolved Bug Returned**: The fix for #123 was reverted by this PR

### High Priority Issues
None found

### Medium Priority Issues
None found

### Low Priority / Nice-to-Have
None found
"""
        findings = agent._parse_review_findings(output, "PR Code Review")
        assert len(findings) == 1
        assert findings[0]['severity'] == 'critical'

    def test_mixed_content_with_finding_and_none_language(self, agent):
        """A real finding followed by 'no issues found' text should still create an issue."""
        output = """
### Critical Issues
- **SQL Injection**: User input not sanitized
No other issues found in this module

### High Priority Issues
None found

### Medium Priority Issues
None found

### Low Priority / Nice-to-Have
None found
"""
        findings = agent._parse_review_findings(output, "PR Code Review")
        assert len(findings) == 1
        assert findings[0]['severity'] == 'critical'

    def test_gaps_unstructured_prose_prevented(self, agent):
        """Gaps with prose but no structured findings should not create issues."""
        output = """
### Gaps Found
The implementation covers the core requirements well, though there are minor areas that could benefit from additional polish.

### Deviations
The overall approach aligns with the architectural design with some minor stylistic differences.

### Verified
- Core authentication flow
"""
        findings = agent._parse_review_findings(output, "Software Architect Output")
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# Helpers for execute() integration tests
# ---------------------------------------------------------------------------

def _make_execute_context(project='test-project', issue_number=42):
    """Build a minimal context dict for execute() tests."""
    return {'context': {'issue_number': issue_number, 'project': project}}


def _clean_review_output():
    """Claude output with no actionable findings."""
    return {
        'result': """
## PR Review Findings

### Critical Issues
None found

### High Priority Issues
None found

### Medium Priority Issues
None found

### Low Priority / Nice-to-Have
None found
""",
        'tools_used': [
            {'name': 'pr-review-toolkit:review-pr', 'timestamp': '2025-01-01T00:00:00Z'}
        ]
    }


def _issues_found_output():
    """Claude output with a critical finding."""
    return {
        'result': """
## PR Review Findings

### Critical Issues
- **SQL Injection**: User input not sanitized

### High Priority Issues
None found

### Medium Priority Issues
None found

### Low Priority / Nice-to-Have
None found
""",
        'tools_used': [
            {'name': 'pr-review-toolkit:review-pr', 'timestamp': '2025-01-01T00:00:00Z'}
        ]
    }


class TestExecutePostReviewDecision:
    """Test the three-way post-review decision: inconclusive / issues found / clean pass."""

    @pytest.mark.asyncio
    async def test_clean_pass_advances_to_documentation(self, agent):
        """When all phases complete and find nothing, parent advances to Documentation."""
        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch('agents.pr_review_agent.run_claude_code', return_value=_clean_review_output()), \
             patch.object(agent, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
             patch.object(agent, '_load_discussion_outputs', return_value={}), \
             patch.object(agent, '_get_parent_issue_body', return_value='body'), \
             patch.object(agent, '_check_ci_status', return_value=([], [])), \
             patch.object(agent, '_advance_parent_to_documentation') as mock_advance, \
             patch.object(agent, '_return_parent_to_development') as mock_return, \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 0
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'o', 'repo': 'r'}
            )

            result = await agent.execute(_make_execute_context())

            mock_advance.assert_called_once_with('test-project', 42)
            mock_return.assert_not_called()
            assert "Clean pass" in result['markdown_analysis']

    @pytest.mark.asyncio
    async def test_issues_found_returns_to_development(self, agent):
        """When issues are found and created, parent returns to In Development."""
        created_issue = [{'number': '99', 'url': 'u', 'title': 't', 'severity': 'critical'}]

        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch('agents.pr_review_agent.run_claude_code', return_value=_issues_found_output()), \
             patch.object(agent, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
             patch.object(agent, '_load_discussion_outputs', return_value={}), \
             patch.object(agent, '_get_parent_issue_body', return_value='body'), \
             patch.object(agent, '_check_ci_status', return_value=([], [])), \
             patch.object(agent, '_create_review_issues', return_value=created_issue), \
             patch.object(agent, '_move_issues_to_development') as mock_move, \
             patch.object(agent, '_return_parent_to_development') as mock_return, \
             patch.object(agent, '_advance_parent_to_documentation') as mock_advance, \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 0
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'o', 'repo': 'r'}
            )

            result = await agent.execute(_make_execute_context())

            mock_return.assert_called_once_with('test-project', 42)
            mock_advance.assert_not_called()
            mock_move.assert_called_once()
            assert "Issues found" in result['markdown_analysis']

    @pytest.mark.asyncio
    async def test_issues_found_but_creation_fails_still_returns_to_development(self, agent):
        """KEY BUG SCENARIO: findings detected but _create_review_issues returns [].

        Before the fix, the code fell to the 'else' branch and advanced to Documentation.
        Now it should still return the parent to In Development.
        """
        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch('agents.pr_review_agent.run_claude_code', return_value=_issues_found_output()), \
             patch.object(agent, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
             patch.object(agent, '_load_discussion_outputs', return_value={}), \
             patch.object(agent, '_get_parent_issue_body', return_value='body'), \
             patch.object(agent, '_check_ci_status', return_value=([], [])), \
             patch.object(agent, '_create_review_issues', return_value=[]), \
             patch.object(agent, '_move_issues_to_development') as mock_move, \
             patch.object(agent, '_return_parent_to_development') as mock_return, \
             patch.object(agent, '_advance_parent_to_documentation') as mock_advance, \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 0
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'o', 'repo': 'r'}
            )

            result = await agent.execute(_make_execute_context())

            mock_return.assert_called_once_with('test-project', 42)
            mock_advance.assert_not_called()
            # No issues to move since creation failed
            mock_move.assert_not_called()
            assert "Issues found" in result['markdown_analysis']

    @pytest.mark.asyncio
    async def test_all_phases_throw_exceptions_does_not_advance(self, agent):
        """When every phase throws, neither advance nor return is called."""
        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch('agents.pr_review_agent.run_claude_code', side_effect=RuntimeError("boom")), \
             patch.object(agent, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
             patch.object(agent, '_load_discussion_outputs', return_value={}), \
             patch.object(agent, '_get_parent_issue_body', return_value='body'), \
             patch.object(agent, '_check_ci_status', side_effect=RuntimeError("gh not available")), \
             patch.object(agent, '_return_parent_to_development') as mock_return, \
             patch.object(agent, '_advance_parent_to_documentation') as mock_advance, \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 0
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'o', 'repo': 'r'}
            )

            result = await agent.execute(_make_execute_context())

            mock_return.assert_not_called()
            mock_advance.assert_not_called()
            assert "Inconclusive" in result['markdown_analysis']

    @pytest.mark.asyncio
    async def test_partial_phase_success_clean_pass_advances(self, agent):
        """Phase 1 completes clean, Phase 2 checks skipped (no content) — should advance."""
        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch('agents.pr_review_agent.run_claude_code', return_value=_clean_review_output()), \
             patch.object(agent, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
             patch.object(agent, '_load_discussion_outputs', return_value={}), \
             patch.object(agent, '_get_parent_issue_body', return_value=''), \
             patch.object(agent, '_check_ci_status', return_value=([], [])), \
             patch.object(agent, '_advance_parent_to_documentation') as mock_advance, \
             patch.object(agent, '_return_parent_to_development') as mock_return, \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 0
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'o', 'repo': 'r'}
            )

            # Phase 1 runs clean, Phase 2 has no content to check (empty discussion + empty body)
            result = await agent.execute(_make_execute_context())

            mock_advance.assert_called_once_with('test-project', 42)
            mock_return.assert_not_called()
            assert "Clean pass" in result['markdown_analysis']

    @pytest.mark.asyncio
    async def test_phase1_clean_phase2_throws_is_inconclusive(self, agent):
        """HIGH fix: Phase 1 clean but Phase 2 exceptions should NOT advance to Documentation."""
        def claude_side_effect(prompt, ctx):
            # Phase 1 (PR review) succeeds clean
            if 'PR Review Specialist' in prompt:
                return _clean_review_output()
            # Phase 2 (verification) throws
            raise RuntimeError("Claude API timeout")

        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch('agents.pr_review_agent.run_claude_code', side_effect=claude_side_effect), \
             patch.object(agent, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
             patch.object(agent, '_load_discussion_outputs', return_value={
                 'idea_researcher': 'some output',
             }), \
             patch.object(agent, '_get_parent_issue_body', return_value='requirements here'), \
             patch.object(agent, '_check_ci_status', return_value=([], [])), \
             patch.object(agent, '_advance_parent_to_documentation') as mock_advance, \
             patch.object(agent, '_return_parent_to_development') as mock_return, \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 0
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'o', 'repo': 'r'}
            )

            result = await agent.execute(_make_execute_context())

            # Must NOT advance — Phase 2 checks failed, review is incomplete
            mock_advance.assert_not_called()
            mock_return.assert_not_called()
            assert "Inconclusive" in result['markdown_analysis']

    @pytest.mark.asyncio
    async def test_phase1_throws_phase2_finds_issues_returns_to_development(self, agent):
        """When Phase 1 fails but Phase 2 finds issues, parent returns to Development."""
        def claude_side_effect(prompt, ctx):
            # Phase 1 (PR review) throws
            if 'PR Review Specialist' in prompt:
                raise RuntimeError("boom")
            # Phase 2 (verification) finds issues
            return _issues_found_output()

        created_issue = [{'number': '99', 'url': 'u', 'title': 't', 'severity': 'critical'}]

        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch('agents.pr_review_agent.run_claude_code', side_effect=claude_side_effect), \
             patch.object(agent, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
             patch.object(agent, '_load_discussion_outputs', return_value={}), \
             patch.object(agent, '_get_parent_issue_body', return_value='requirements'), \
             patch.object(agent, '_check_ci_status', return_value=([], [])), \
             patch.object(agent, '_create_review_issues', return_value=created_issue), \
             patch.object(agent, '_move_issues_to_development') as mock_move, \
             patch.object(agent, '_return_parent_to_development') as mock_return, \
             patch.object(agent, '_advance_parent_to_documentation') as mock_advance, \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 0
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'o', 'repo': 'r'}
            )

            result = await agent.execute(_make_execute_context())

            # Issues found by Phase 2 should return to development despite Phase 1 failure
            mock_return.assert_called_once_with('test-project', 42)
            mock_advance.assert_not_called()
            assert "Issues found" in result['markdown_analysis']

    @pytest.mark.asyncio
    async def test_phase2_findings_return_to_development(self, agent):
        """Phase 2 context verification findings trigger return to Development."""
        call_count = 0

        def claude_side_effect(prompt, ctx):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Phase 1: clean
                return _clean_review_output()
            else:
                # Phase 2: finds gaps
                return {
                    'result': """
### Gaps Found
- **Missing Pagination**: API returns all results instead of paginated response

### Deviations
None found

### Verified
- Auth flow works
"""
                }

        created_issue = [{'number': '55', 'url': 'u', 'title': 't', 'severity': 'high'}]

        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch('agents.pr_review_agent.run_claude_code', side_effect=claude_side_effect), \
             patch.object(agent, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
             patch.object(agent, '_load_discussion_outputs', return_value={
                 'business_analyst': 'analyst output here',
             }), \
             patch.object(agent, '_get_parent_issue_body', return_value=''), \
             patch.object(agent, '_check_ci_status', return_value=([], [])), \
             patch.object(agent, '_create_review_issues', return_value=created_issue), \
             patch.object(agent, '_move_issues_to_development') as mock_move, \
             patch.object(agent, '_return_parent_to_development') as mock_return, \
             patch.object(agent, '_advance_parent_to_documentation') as mock_advance, \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 0
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'o', 'repo': 'r'}
            )

            result = await agent.execute(_make_execute_context())

            mock_return.assert_called_once_with('test-project', 42)
            mock_advance.assert_not_called()
            assert "Issues found" in result['markdown_analysis']

    @pytest.mark.asyncio
    async def test_cycle_limit_with_issues_does_not_return_to_development(self, agent):
        """At cycle 3, issues found should NOT return parent to Development."""
        created_issue = [{'number': '99', 'url': 'u', 'title': 't', 'severity': 'critical'}]

        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch('agents.pr_review_agent.run_claude_code', return_value=_issues_found_output()), \
             patch.object(agent, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
             patch.object(agent, '_load_discussion_outputs', return_value={}), \
             patch.object(agent, '_get_parent_issue_body', return_value='body'), \
             patch.object(agent, '_check_ci_status', return_value=([], [])), \
             patch.object(agent, '_create_review_issues', return_value=created_issue), \
             patch.object(agent, '_move_issues_to_development') as mock_move, \
             patch.object(agent, '_return_parent_to_development') as mock_return, \
             patch.object(agent, '_advance_parent_to_documentation') as mock_advance, \
             patch.object(agent, '_post_comment_on_issue') as mock_comment, \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 2  # cycle 3 = limit
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'o', 'repo': 'r'}
            )

            await agent.execute(_make_execute_context())

            mock_return.assert_not_called()
            mock_advance.assert_not_called()
            mock_move.assert_not_called()
            mock_comment.assert_called_once()  # summary posted on parent

    @pytest.mark.asyncio
    async def test_cycle_limit_with_creation_failure_does_not_advance(self, agent):
        """At cycle 3, issues found but creation fails — no advance, no comment."""
        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch('agents.pr_review_agent.run_claude_code', return_value=_issues_found_output()), \
             patch.object(agent, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
             patch.object(agent, '_load_discussion_outputs', return_value={}), \
             patch.object(agent, '_get_parent_issue_body', return_value='body'), \
             patch.object(agent, '_check_ci_status', return_value=([], [])), \
             patch.object(agent, '_create_review_issues', return_value=[]), \
             patch.object(agent, '_return_parent_to_development') as mock_return, \
             patch.object(agent, '_advance_parent_to_documentation') as mock_advance, \
             patch.object(agent, '_post_comment_on_issue') as mock_comment, \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 2  # cycle 3 = limit
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'o', 'repo': 'r'}
            )

            await agent.execute(_make_execute_context())

            mock_return.assert_not_called()
            mock_advance.assert_not_called()
            # No issues to list, so no comment posted
            mock_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_ci_failures_trigger_return_to_development(self, agent):
        """Phase 3 CI failures should set review_found_issues and return to Development."""
        ci_failures = [{'name': 'tests', 'state': 'FAILURE', 'bucket': 'fail',
                        'description': 'Tests failed', 'link': 'https://ci.example.com/1'}]
        created_issue = [{'number': '77', 'url': 'u', 'title': '[PR Review] CI check failures', 'severity': 'high'}]

        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch('agents.pr_review_agent.run_claude_code', return_value=_clean_review_output()), \
             patch.object(agent, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
             patch.object(agent, '_load_discussion_outputs', return_value={}), \
             patch.object(agent, '_get_parent_issue_body', return_value='body'), \
             patch.object(agent, '_check_ci_status', return_value=(ci_failures, [])), \
             patch.object(agent, '_create_review_issues', return_value=created_issue), \
             patch.object(agent, '_move_issues_to_development') as mock_move, \
             patch.object(agent, '_return_parent_to_development') as mock_return, \
             patch.object(agent, '_advance_parent_to_documentation') as mock_advance, \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 0
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'o', 'repo': 'r'}
            )

            result = await agent.execute(_make_execute_context())

            mock_return.assert_called_once_with('test-project', 42)
            mock_advance.assert_not_called()
            assert "Issues found" in result['markdown_analysis']

    @pytest.mark.asyncio
    async def test_ci_failures_but_creation_fails_still_returns_to_development(self, agent):
        """CI failures detected but _create_review_issues returns [] — still returns to Development."""
        ci_failures = [{'name': 'tests', 'state': 'FAILURE', 'bucket': 'fail',
                        'description': 'Tests failed', 'link': 'https://ci.example.com/1'}]

        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch('agents.pr_review_agent.run_claude_code', return_value=_clean_review_output()), \
             patch.object(agent, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
             patch.object(agent, '_load_discussion_outputs', return_value={}), \
             patch.object(agent, '_get_parent_issue_body', return_value='body'), \
             patch.object(agent, '_check_ci_status', return_value=(ci_failures, [])), \
             patch.object(agent, '_create_review_issues', return_value=[]), \
             patch.object(agent, '_move_issues_to_development') as mock_move, \
             patch.object(agent, '_return_parent_to_development') as mock_return, \
             patch.object(agent, '_advance_parent_to_documentation') as mock_advance, \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 0
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'o', 'repo': 'r'}
            )

            result = await agent.execute(_make_execute_context())

            mock_return.assert_called_once_with('test-project', 42)
            mock_advance.assert_not_called()
            mock_move.assert_not_called()
            assert "Issues found" in result['markdown_analysis']

    @pytest.mark.asyncio
    async def test_ci_all_passing_does_not_block_clean_pass(self, agent):
        """When CI passes and other phases are clean, should advance to Documentation."""
        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch('agents.pr_review_agent.run_claude_code', return_value=_clean_review_output()), \
             patch.object(agent, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
             patch.object(agent, '_load_discussion_outputs', return_value={}), \
             patch.object(agent, '_get_parent_issue_body', return_value='body'), \
             patch.object(agent, '_check_ci_status', return_value=([], [])), \
             patch.object(agent, '_advance_parent_to_documentation') as mock_advance, \
             patch.object(agent, '_return_parent_to_development') as mock_return, \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 0
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'o', 'repo': 'r'}
            )

            result = await agent.execute(_make_execute_context())

            mock_advance.assert_called_once_with('test-project', 42)
            mock_return.assert_not_called()
            assert "Clean pass" in result['markdown_analysis']
            assert "CI" in result['markdown_analysis']

    @pytest.mark.asyncio
    async def test_ci_pending_does_not_block_clean_pass(self, agent):
        """Pending CI checks should not prevent a clean pass (noted in summary)."""
        pending_checks = [{'name': 'slow-test', 'state': 'PENDING', 'bucket': 'pending',
                           'description': 'Running', 'link': ''}]

        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch('agents.pr_review_agent.run_claude_code', return_value=_clean_review_output()), \
             patch.object(agent, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
             patch.object(agent, '_load_discussion_outputs', return_value={}), \
             patch.object(agent, '_get_parent_issue_body', return_value='body'), \
             patch.object(agent, '_check_ci_status', return_value=([], pending_checks)), \
             patch.object(agent, '_advance_parent_to_documentation') as mock_advance, \
             patch.object(agent, '_return_parent_to_development') as mock_return, \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 0
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'o', 'repo': 'r'}
            )

            result = await agent.execute(_make_execute_context())

            mock_advance.assert_called_once_with('test-project', 42)
            mock_return.assert_not_called()
            assert "Clean pass" in result['markdown_analysis']
            assert "pending" in result['markdown_analysis'].lower()

    @pytest.mark.asyncio
    async def test_phase3_exception_marks_phase_not_completed(self, agent):
        """Phase 3 exception should not increment phases_completed."""
        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch('agents.pr_review_agent.run_claude_code', return_value=_clean_review_output()), \
             patch.object(agent, '_find_pr_url', return_value='https://github.com/o/r/pull/1'), \
             patch.object(agent, '_load_discussion_outputs', return_value={}), \
             patch.object(agent, '_get_parent_issue_body', return_value='body'), \
             patch.object(agent, '_check_ci_status', side_effect=RuntimeError("gh not found")), \
             patch.object(agent, '_advance_parent_to_documentation') as mock_advance, \
             patch.object(agent, '_return_parent_to_development') as mock_return, \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 0
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'o', 'repo': 'r'}
            )

            result = await agent.execute(_make_execute_context())

            # Phase 1 completed (1), Phase 3 failed — 1/2 attempted = inconclusive
            mock_advance.assert_not_called()
            mock_return.assert_not_called()
            assert "Inconclusive" in result['markdown_analysis']


class TestCheckCiStatus:
    """Tests for _check_ci_status() method."""

    def test_all_checks_passing(self, agent):
        checks_json = json.dumps([
            {'name': 'tests', 'state': 'SUCCESS', 'bucket': 'pass', 'description': '', 'link': ''},
            {'name': 'lint', 'state': 'SUCCESS', 'bucket': 'pass', 'description': '', 'link': ''},
        ])
        mock_result = MagicMock(returncode=0, stdout=checks_json, stderr='')

        with patch('agents.pr_review_agent.subprocess.run', return_value=mock_result):
            failures, pending = agent._check_ci_status(
                'https://github.com/o/r/pull/5', 'o/r'
            )

        assert failures == []
        assert pending == []

    def test_failures_and_pending(self, agent):
        checks_json = json.dumps([
            {'name': 'tests', 'state': 'FAILURE', 'bucket': 'fail', 'description': 'Failed', 'link': 'http://ci/1'},
            {'name': 'lint', 'state': 'PENDING', 'bucket': 'pending', 'description': 'Running', 'link': ''},
            {'name': 'build', 'state': 'SUCCESS', 'bucket': 'pass', 'description': '', 'link': ''},
        ])
        mock_result = MagicMock(returncode=1, stdout=checks_json, stderr='')

        with patch('agents.pr_review_agent.subprocess.run', return_value=mock_result):
            failures, pending = agent._check_ci_status(
                'https://github.com/o/r/pull/5', 'o/r'
            )

        assert len(failures) == 1
        assert failures[0]['name'] == 'tests'
        assert len(pending) == 1
        assert pending[0]['name'] == 'lint'

    def test_no_checks_configured(self, agent):
        mock_result = MagicMock(returncode=0, stdout='', stderr='')

        with patch('agents.pr_review_agent.subprocess.run', return_value=mock_result):
            failures, pending = agent._check_ci_status(
                'https://github.com/o/r/pull/5', 'o/r'
            )

        assert failures == []
        assert pending == []

    def test_unexpected_exit_code_raises(self, agent):
        mock_result = MagicMock(returncode=2, stdout='', stderr='some error')

        with patch('agents.pr_review_agent.subprocess.run', return_value=mock_result):
            with pytest.raises(RuntimeError, match="Unexpected exit code 2"):
                agent._check_ci_status('https://github.com/o/r/pull/5', 'o/r')

    def test_invalid_pr_url_raises(self, agent):
        with pytest.raises(ValueError, match="Could not extract PR number"):
            agent._check_ci_status('https://github.com/o/r/issues/5', 'o/r')

    def test_extracts_pr_number_from_url(self, agent):
        mock_result = MagicMock(returncode=0, stdout='[]', stderr='')

        with patch('agents.pr_review_agent.subprocess.run', return_value=mock_result) as mock_run:
            agent._check_ci_status('https://github.com/org/repo/pull/123', 'org/repo')

        args = mock_run.call_args[0][0]
        assert '123' in args

    def test_pending_exit_code_8(self, agent):
        checks_json = json.dumps([
            {'name': 'tests', 'state': 'PENDING', 'bucket': 'pending', 'description': 'Running', 'link': ''},
        ])
        mock_result = MagicMock(returncode=8, stdout=checks_json, stderr='')

        with patch('agents.pr_review_agent.subprocess.run', return_value=mock_result):
            failures, pending = agent._check_ci_status(
                'https://github.com/o/r/pull/5', 'o/r'
            )

        assert failures == []
        assert len(pending) == 1

    def test_subprocess_exception_propagates(self, agent):
        with patch('agents.pr_review_agent.subprocess.run', side_effect=OSError("no gh")):
            with pytest.raises(OSError, match="no gh"):
                agent._check_ci_status('https://github.com/o/r/pull/5', 'o/r')


class TestBuildCiFailureIssue:
    """Tests for _build_ci_failure_issue() method."""

    def test_issue_spec_structure(self, agent):
        failures = [
            {'name': 'tests', 'state': 'FAILURE', 'bucket': 'fail',
             'description': 'Tests failed', 'link': 'https://ci.example.com/1'},
        ]
        spec = agent._build_ci_failure_issue(failures, 'https://github.com/o/r/pull/5')

        assert spec['title'] == '[PR Review] CI check failures'
        assert spec['severity'] == 'high'
        assert 'https://github.com/o/r/pull/5' in spec['body']
        assert 'PR Review Agent' in spec['body']

    def test_includes_failure_table(self, agent):
        failures = [
            {'name': 'unit-tests', 'state': 'FAILURE', 'bucket': 'fail',
             'description': '', 'link': 'https://ci/1'},
            {'name': 'integration', 'state': 'FAILURE', 'bucket': 'fail',
             'description': 'Timed out', 'link': ''},
        ]
        spec = agent._build_ci_failure_issue(failures, 'https://github.com/o/r/pull/5')

        assert 'unit-tests' in spec['body']
        assert 'integration' in spec['body']


class TestFormatCiTable:
    """Tests for _format_ci_table() method."""

    def test_renders_header_row(self, agent):
        table = agent._format_ci_table([])
        assert '| Check | State | Details |' in table
        assert '| --- | --- | --- |' in table

    def test_renders_data_rows(self, agent):
        checks = [
            {'name': 'tests', 'state': 'FAILURE', 'bucket': 'fail',
             'description': 'Failed', 'link': 'https://ci/1'},
        ]
        table = agent._format_ci_table(checks)
        assert '| tests |' in table
        assert 'FAILURE' in table

    def test_link_takes_precedence_over_description(self, agent):
        checks = [
            {'name': 'tests', 'state': 'FAILURE', 'bucket': 'fail',
             'description': 'Failed', 'link': 'https://ci/1'},
        ]
        table = agent._format_ci_table(checks)
        assert '[View](https://ci/1)' in table

    def test_falls_back_to_description_when_no_link(self, agent):
        checks = [
            {'name': 'tests', 'state': 'FAILURE', 'bucket': 'fail',
             'description': 'Tests timed out', 'link': ''},
        ]
        table = agent._format_ci_table(checks)
        assert 'Tests timed out' in table
        assert '[View]' not in table

    def test_multiple_checks(self, agent):
        checks = [
            {'name': 'unit', 'state': 'FAILURE', 'bucket': 'fail', 'description': '', 'link': ''},
            {'name': 'lint', 'state': 'FAILURE', 'bucket': 'fail', 'description': '', 'link': ''},
        ]
        table = agent._format_ci_table(checks)
        lines = table.strip().split('\n')
        # Header + separator + 2 data rows
        assert len(lines) == 4


class TestFindPrUrl:
    """Test _find_pr_url() method that uses gh pr list."""

    @pytest.mark.asyncio
    async def test_finds_matching_pr(self, agent):
        """Should return PR URL when a PR matches the branch prefix."""
        prs_json = json.dumps([
            {'number': 100, 'url': 'https://github.com/org/repo/pull/100',
             'headRefName': 'feature/issue-42-add-login'},
            {'number': 101, 'url': 'https://github.com/org/repo/pull/101',
             'headRefName': 'feature/issue-99-other'},
        ])
        mock_result = MagicMock(returncode=0, stdout=prs_json, stderr='')

        with patch('agents.pr_review_agent.subprocess.run', return_value=mock_result):
            url = await agent._find_pr_url({'org': 'org', 'repo': 'repo'}, 42)

        assert url == 'https://github.com/org/repo/pull/100'

    @pytest.mark.asyncio
    async def test_returns_none_when_no_match(self, agent):
        """Should return None when no PR matches the branch prefix."""
        prs_json = json.dumps([
            {'number': 101, 'url': 'https://github.com/org/repo/pull/101',
             'headRefName': 'feature/issue-99-other'},
        ])
        mock_result = MagicMock(returncode=0, stdout=prs_json, stderr='')

        with patch('agents.pr_review_agent.subprocess.run', return_value=mock_result):
            url = await agent._find_pr_url({'org': 'org', 'repo': 'repo'}, 42)

        assert url is None

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_pr_list(self, agent):
        """Should return None when gh pr list returns empty list."""
        mock_result = MagicMock(returncode=0, stdout='[]', stderr='')

        with patch('agents.pr_review_agent.subprocess.run', return_value=mock_result):
            url = await agent._find_pr_url({'org': 'org', 'repo': 'repo'}, 42)

        assert url is None

    @pytest.mark.asyncio
    async def test_returns_none_on_gh_cli_failure(self, agent):
        """Should return None (not raise) when gh CLI fails."""
        with patch('agents.pr_review_agent.subprocess.run', side_effect=OSError("gh not found")):
            url = await agent._find_pr_url({'org': 'org', 'repo': 'repo'}, 42)

        assert url is None

    @pytest.mark.asyncio
    async def test_returns_none_on_nonzero_exit(self, agent):
        """Should return None when gh returns non-zero exit code."""
        mock_result = MagicMock(returncode=1, stdout='', stderr='auth required')

        with patch('agents.pr_review_agent.subprocess.run', return_value=mock_result):
            url = await agent._find_pr_url({'org': 'org', 'repo': 'repo'}, 42)

        assert url is None

    @pytest.mark.asyncio
    async def test_uses_correct_branch_prefix(self, agent):
        """Should search for branches starting with feature/issue-{number}-."""
        mock_result = MagicMock(returncode=0, stdout='[]', stderr='')

        with patch('agents.pr_review_agent.subprocess.run', return_value=mock_result) as mock_run:
            await agent._find_pr_url({'org': 'myorg', 'repo': 'myrepo'}, 249)

        args = mock_run.call_args[0][0]
        assert '-R' in args
        assert 'myorg/myrepo' in args
