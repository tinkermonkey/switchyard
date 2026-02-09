"""
Unit tests for PR Review Agent

Tests recommendation parsing, severity grouping, context gap detection,
cycle limit enforcement, and clean pass detection.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import patch, MagicMock


@pytest.fixture
def agent():
    """Create a PRReviewAgent with mocked dependencies."""
    with patch('agents.pr_review_agent.ConfigManager'), \
         patch('agents.pr_review_agent.GitHubStateManager'):
        from agents.pr_review_agent import PRReviewAgent
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
