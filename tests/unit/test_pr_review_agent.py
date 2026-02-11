"""
Unit tests for PR Review Agent

Tests recommendation parsing, severity grouping, context gap detection,
cycle limit enforcement, and clean pass detection.

These tests mock the agent's dependencies so they can run outside Docker.
"""

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
        assert agent._is_none_found(
            "Issues were already resolved during the development phase"
        ) is True
        assert agent._is_none_found(
            "All items previously addressed in earlier commits"
        ) is True

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
            # Should NOT raise — manual trigger resets cycle count
            result = await agent.execute(context)
            mock_state.reset_review_count.assert_called_once_with('test-project', 42)
            # Proceeds past cycle limit, but exits at "no PR found"
            assert "No open PR" in result['markdown_analysis']

    @pytest.mark.asyncio
    async def test_manual_trigger_below_limit_does_not_reset(self, agent):
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
    async def test_returns_early_when_no_pr_found(self, agent):
        with patch('agents.pr_review_agent.pr_review_state_manager') as mock_state, \
             patch.object(agent, '_find_pr_url', return_value=None), \
             patch.object(agent, 'config_manager') as mock_config:
            mock_state.get_review_count.return_value = 0
            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'test-org', 'repo': 'test-repo'}
            )
            context = {'context': {'issue_number': 42, 'project': 'test-project'}}
            result = await agent.execute(context)
            assert "Skipped" in result['markdown_analysis']
            assert "No open PR" in result['markdown_analysis']


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
