"""
Tests for WorkBreakdownAgent sub-issue parsing.

Covers _extract_sub_issues_section, _split_phases, and _parse_sub_issues_from_output
with emphasis on code-fence-aware boundary detection — the fix for the bug where
## headers inside markdown code blocks prematurely truncated section extraction.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import patch, MagicMock


@pytest.fixture
def agent():
    """Create a WorkBreakdownAgent with mocked dependencies."""
    with patch('agents.work_breakdown_agent.ConfigManager'), \
         patch('agents.work_breakdown_agent.GitHubStateManager'):
        from agents.work_breakdown_agent import WorkBreakdownAgent
        return WorkBreakdownAgent()


# ===========================================================================
# _extract_sub_issues_section
# ===========================================================================

class TestExtractSubIssuesSection:
    """Tests for code-fence-aware section extraction."""

    def test_basic_section_extraction(self, agent):
        """Extracts content between ## Sub-Issues to Create and next ## header."""
        md = (
            "## Summary\n"
            "Some summary.\n"
            "\n"
            "## Sub-Issues to Create\n"
            "\n"
            "### Phase 1: Setup\n"
            "Phase 1 content\n"
            "\n"
            "## Next Section\n"
            "Other content\n"
        )
        result = agent._extract_sub_issues_section(md)
        assert '### Phase 1: Setup' in result
        assert 'Phase 1 content' in result
        assert 'Next Section' not in result
        assert 'Some summary' not in result

    def test_section_at_end_of_document(self, agent):
        """Section extends to end of document when no trailing ## header."""
        md = (
            "## Sub-Issues to Create\n"
            "\n"
            "### Phase 1: Only phase\n"
            "Content here\n"
        )
        result = agent._extract_sub_issues_section(md)
        assert '### Phase 1: Only phase' in result
        assert 'Content here' in result

    def test_h2_inside_backtick_code_fence_ignored(self, agent):
        """The exact bug: ## inside ``` code blocks must not terminate the section."""
        md = (
            "## Sub-Issues to Create\n"
            "\n"
            "### Phase 1: Spec Updates\n"
            "**Design Guidance**:\n"
            "```markdown\n"
            "## [0.7.2] - 2026-02-06\n"
            "\n"
            "### Breaking Changes\n"
            "- Something changed\n"
            "```\n"
            "\n"
            "### Phase 2: Code Updates\n"
            "Phase 2 content\n"
            "\n"
            "## Another Section\n"
        )
        result = agent._extract_sub_issues_section(md)
        assert 'Phase 1' in result
        assert 'Phase 2' in result
        assert '## [0.7.2]' in result  # code block content preserved
        assert 'Another Section' not in result

    def test_h2_inside_tilde_code_fence_ignored(self, agent):
        """## inside ~~~ code blocks must not terminate the section."""
        md = (
            "## Sub-Issues to Create\n"
            "\n"
            "### Phase 1: Setup\n"
            "~~~\n"
            "## This is inside a tilde fence\n"
            "~~~\n"
            "\n"
            "### Phase 2: Build\n"
            "Phase 2 content\n"
        )
        result = agent._extract_sub_issues_section(md)
        assert 'Phase 1' in result
        assert 'Phase 2' in result

    def test_multiple_code_fences_across_phases(self, agent):
        """Multiple code fences across different phases are tracked correctly."""
        md = (
            "## Sub-Issues to Create\n"
            "\n"
            "### Phase 1: First\n"
            "```json\n"
            '{"## not a header": true}\n'
            "```\n"
            "\n"
            "### Phase 2: Second\n"
            "```yaml\n"
            "## also not a header\n"
            "```\n"
            "\n"
            "### Phase 3: Third\n"
            "Content\n"
        )
        result = agent._extract_sub_issues_section(md)
        assert 'Phase 1' in result
        assert 'Phase 2' in result
        assert 'Phase 3' in result

    def test_no_section_found(self, agent):
        """Returns empty string when section header is missing."""
        md = "## Some Other Section\nContent\n"
        result = agent._extract_sub_issues_section(md)
        assert result == ''

    def test_empty_section(self, agent):
        """Returns empty-ish content when section exists but has no phases."""
        md = (
            "## Sub-Issues to Create\n"
            "\n"
            "## Next Section\n"
        )
        result = agent._extract_sub_issues_section(md)
        assert result.strip() == ''

    def test_case_insensitive_header(self, agent):
        """Section header matching is case-insensitive."""
        md = (
            "## sub-issues to create\n"
            "### Phase 1: Setup\n"
            "Content\n"
        )
        result = agent._extract_sub_issues_section(md)
        assert 'Phase 1' in result

    def test_h3_header_does_not_terminate_section(self, agent):
        """### headers outside code fences do NOT terminate the ## section."""
        md = (
            "## Sub-Issues to Create\n"
            "\n"
            "### Phase 1: First\n"
            "Content 1\n"
            "\n"
            "### Phase 2: Second\n"
            "Content 2\n"
        )
        result = agent._extract_sub_issues_section(md)
        assert 'Phase 1' in result
        assert 'Phase 2' in result

    def test_indented_code_fence(self, agent):
        """Code fences with leading whitespace are recognized."""
        md = (
            "## Sub-Issues to Create\n"
            "\n"
            "### Phase 1: Setup\n"
            "  ```\n"
            "  ## indented code fence header\n"
            "  ```\n"
            "\n"
            "### Phase 2: Build\n"
            "Content\n"
        )
        result = agent._extract_sub_issues_section(md)
        assert 'Phase 1' in result
        assert 'Phase 2' in result

    def test_code_fence_with_language_tag(self, agent):
        """```python, ```json etc. are recognized as code fence boundaries."""
        md = (
            "## Sub-Issues to Create\n"
            "\n"
            "### Phase 1: Setup\n"
            "```python\n"
            "## Python comment that looks like header\n"
            "x = 1\n"
            "```\n"
            "\n"
            "### Phase 2: Build\n"
            "Content\n"
        )
        result = agent._extract_sub_issues_section(md)
        assert 'Phase 2' in result


# ===========================================================================
# _split_phases
# ===========================================================================

class TestSplitPhases:
    """Tests for code-fence-aware phase splitting."""

    def test_basic_split(self, agent):
        """Splits two clean phases correctly."""
        content = (
            "### Phase 1: Setup\n"
            "Phase 1 body\n"
            "\n"
            "---\n"
            "\n"
            "### Phase 2: Build\n"
            "Phase 2 body\n"
        )
        phases = agent._split_phases(content)
        assert len(phases) == 2
        assert phases[0][0] == 'Phase 1: Setup'
        assert 'Phase 1 body' in phases[0][1]
        assert phases[1][0] == 'Phase 2: Build'
        assert 'Phase 2 body' in phases[1][1]

    def test_three_phases(self, agent):
        """Handles three phases."""
        content = (
            "### Phase 1: A\n"
            "Body 1\n"
            "### Phase 2: B\n"
            "Body 2\n"
            "### Phase 3: C\n"
            "Body 3\n"
        )
        phases = agent._split_phases(content)
        assert len(phases) == 3
        assert phases[2][0] == 'Phase 3: C'

    def test_phase_header_inside_code_fence_ignored(self, agent):
        """### Phase inside code blocks is not treated as a boundary."""
        content = (
            "### Phase 1: Real Phase\n"
            "Some content\n"
            "```\n"
            "### Phase 99: Fake Phase In Code Block\n"
            "```\n"
            "More content for phase 1\n"
            "\n"
            "### Phase 2: Real Phase Two\n"
            "Phase 2 content\n"
        )
        phases = agent._split_phases(content)
        assert len(phases) == 2
        assert phases[0][0] == 'Phase 1: Real Phase'
        assert 'Fake Phase' in phases[0][1]  # code block content is in phase 1
        assert phases[1][0] == 'Phase 2: Real Phase Two'

    def test_no_phases(self, agent):
        """Returns empty list when no phase headers found."""
        content = "Just some text without phase headers\n"
        phases = agent._split_phases(content)
        assert phases == []

    def test_single_phase(self, agent):
        """Single phase returns a list of one."""
        content = (
            "### Phase 1: Only Phase\n"
            "Content\n"
        )
        phases = agent._split_phases(content)
        assert len(phases) == 1
        assert phases[0][0] == 'Phase 1: Only Phase'

    def test_content_before_first_phase_discarded(self, agent):
        """Any content before the first ### Phase header is not included."""
        content = (
            "Preamble text\n"
            "\n"
            "### Phase 1: Start\n"
            "Phase content\n"
        )
        phases = agent._split_phases(content)
        assert len(phases) == 1
        assert 'Preamble' not in phases[0][1]

    def test_phase_title_preserved_with_special_chars(self, agent):
        """Phase titles with colons, hyphens, etc. are captured fully."""
        content = (
            "### Phase 1: Rename spec files and update schema internals for data-store\n"
            "Content\n"
        )
        phases = agent._split_phases(content)
        assert phases[0][0] == 'Phase 1: Rename spec files and update schema internals for data-store'


# ===========================================================================
# _is_code_fence
# ===========================================================================

class TestIsCodeFence:
    """Tests for code fence detection."""

    def test_backtick_fence(self, agent):
        assert agent._is_code_fence('```') is True

    def test_backtick_fence_with_language(self, agent):
        assert agent._is_code_fence('```python') is True

    def test_tilde_fence(self, agent):
        assert agent._is_code_fence('~~~') is True

    def test_indented_fence(self, agent):
        assert agent._is_code_fence('   ```') is True

    def test_normal_line(self, agent):
        assert agent._is_code_fence('## Header') is False

    def test_inline_backticks(self, agent):
        assert agent._is_code_fence('Use `code` here') is False

    def test_empty_line(self, agent):
        assert agent._is_code_fence('') is False


# ===========================================================================
# _parse_sub_issues_from_output (integration of section + phase + field extraction)
# ===========================================================================

class TestParseSubIssuesFromOutput:
    """Integration tests for the full parsing pipeline."""

    def test_basic_two_phase_output(self, agent):
        """Standard two-phase output is parsed correctly."""
        md = (
            "# Work Breakdown Analysis\n"
            "\n"
            "Some preamble text.\n"
            "\n"
            "## Sub-Issues to Create\n"
            "\n"
            "### Phase 1: Database Setup\n"
            "\n"
            "**Title**: Phase 1: Create database schema\n"
            "\n"
            "**Description**:\n"
            "Set up the initial database tables.\n"
            "\n"
            "**Requirements**:\n"
            "- Create users table\n"
            "- Create sessions table\n"
            "\n"
            "**Design Guidance**:\n"
            "Use PostgreSQL.\n"
            "\n"
            "**Acceptance Criteria**:\n"
            "- [ ] Tables exist\n"
            "- [ ] Migrations pass\n"
            "\n"
            "**Dependencies**: None\n"
            "\n"
            "**Parent Issue**: #100\n"
            "\n"
            "---\n"
            "\n"
            "### Phase 2: API Layer\n"
            "\n"
            "**Title**: Phase 2: Build REST endpoints\n"
            "\n"
            "**Description**:\n"
            "Create CRUD endpoints.\n"
            "\n"
            "**Requirements**:\n"
            "- GET /users\n"
            "- POST /users\n"
            "\n"
            "**Design Guidance**:\n"
            "Use FastAPI.\n"
            "\n"
            "**Acceptance Criteria**:\n"
            "- [ ] Endpoints respond\n"
            "\n"
            "**Dependencies**: Phase 1\n"
            "\n"
            "**Parent Issue**: #100\n"
        )
        result = agent._parse_sub_issues_from_output(md)
        assert len(result) == 2
        assert result[0]['title'] == 'Phase 1: Create database schema'
        assert result[0]['phase'] == 'Phase 1: Database Setup'
        assert '#100' in result[0]['parent_issue']
        assert result[1]['title'] == 'Phase 2: Build REST endpoints'
        assert 'Phase 1' in result[1]['dependencies']

    def test_real_world_code_fence_bug(self, agent):
        """
        Reproduces the exact bug from pipeline run 670c75cf:
        A changelog example inside a code fence contained ## [0.7.2] which
        caused the regex to terminate the section after only Phase 1.
        """
        md = (
            "## Sub-Issues to Create\n"
            "\n"
            "### Phase 1: Specification File Updates\n"
            "\n"
            "**Title**: Phase 1: Rename spec files\n"
            "\n"
            "**Description**:\n"
            "Update specification files.\n"
            "\n"
            "**Design Guidance**:\n"
            "**Changelog Entry** (in `spec/CHANGELOG.md`):\n"
            "```markdown\n"
            "## [0.7.2] - 2026-02-06\n"
            "\n"
            "### Breaking Changes\n"
            "- Layer 8 naming changed\n"
            "```\n"
            "\n"
            "**Acceptance Criteria**:\n"
            "- [ ] Files renamed\n"
            "\n"
            "**Dependencies**: None\n"
            "\n"
            "**Parent Issue**: #277\n"
            "\n"
            "---\n"
            "\n"
            "### Phase 2: Example Updates\n"
            "\n"
            "**Title**: Phase 2: Update examples\n"
            "\n"
            "**Description**:\n"
            "Update example manifests.\n"
            "\n"
            "**Acceptance Criteria**:\n"
            "- [ ] Examples updated\n"
            "\n"
            "**Dependencies**: Phase 1\n"
            "\n"
            "**Parent Issue**: #277\n"
            "\n"
            "---\n"
            "\n"
            "### Phase 3: CLI Updates\n"
            "\n"
            "**Title**: Phase 3: Update CLI schemas\n"
            "\n"
            "**Description**:\n"
            "Update CLI layer references.\n"
            "\n"
            "**Acceptance Criteria**:\n"
            "- [ ] CLI works\n"
            "\n"
            "**Dependencies**: Phase 1, Phase 2\n"
            "\n"
            "**Parent Issue**: #277\n"
        )
        result = agent._parse_sub_issues_from_output(md)
        assert len(result) == 3, (
            f"Expected 3 phases but got {len(result)}. "
            f"Phases found: {[r['phase'] for r in result]}"
        )
        assert result[0]['title'] == 'Phase 1: Rename spec files'
        assert result[1]['title'] == 'Phase 2: Update examples'
        assert result[2]['title'] == 'Phase 3: Update CLI schemas'
        # Verify code fence content is preserved in the body
        assert '## [0.7.2]' in result[0]['body']

    def test_no_sub_issues_section(self, agent):
        """Returns empty list when no Sub-Issues section exists."""
        md = "# Just a regular document\n\nSome text.\n"
        result = agent._parse_sub_issues_from_output(md)
        assert result == []

    def test_section_with_no_phases(self, agent):
        """Returns empty list when section exists but has no phase headers."""
        md = (
            "## Sub-Issues to Create\n"
            "\n"
            "This section has no phases.\n"
        )
        result = agent._parse_sub_issues_from_output(md)
        assert result == []

    def test_phase_title_used_as_fallback_for_missing_title_field(self, agent):
        """When **Title** field is missing, phase header is used as title."""
        md = (
            "## Sub-Issues to Create\n"
            "\n"
            "### Phase 1: Setup Infrastructure\n"
            "\n"
            "**Description**:\n"
            "Set up the basics.\n"
            "\n"
            "**Acceptance Criteria**:\n"
            "- [ ] Done\n"
        )
        result = agent._parse_sub_issues_from_output(md)
        assert len(result) == 1
        assert result[0]['title'] == 'Phase 1: Setup Infrastructure'

    def test_multiple_code_fences_with_h2_headers(self, agent):
        """Multiple code blocks each containing ## headers don't break parsing."""
        md = (
            "## Sub-Issues to Create\n"
            "\n"
            "### Phase 1: First\n"
            "\n"
            "**Title**: Phase 1: First phase\n"
            "\n"
            "**Design Guidance**:\n"
            "```markdown\n"
            "## Header in block 1\n"
            "```\n"
            "```yaml\n"
            "## Header in block 2\n"
            "```\n"
            "\n"
            "**Acceptance Criteria**:\n"
            "- [ ] Ok\n"
            "\n"
            "---\n"
            "\n"
            "### Phase 2: Second\n"
            "\n"
            "**Title**: Phase 2: Second phase\n"
            "\n"
            "**Description**:\n"
            "Content.\n"
            "\n"
            "**Acceptance Criteria**:\n"
            "- [ ] Ok\n"
        )
        result = agent._parse_sub_issues_from_output(md)
        assert len(result) == 2

    def test_trailing_h2_terminates_section(self, agent):
        """A real ## header after the section properly terminates it."""
        md = (
            "## Sub-Issues to Create\n"
            "\n"
            "### Phase 1: Only\n"
            "\n"
            "**Title**: Phase 1: Only phase\n"
            "\n"
            "**Description**:\n"
            "Content.\n"
            "\n"
            "## Summary\n"
            "\n"
            "This should NOT be in the section.\n"
        )
        result = agent._parse_sub_issues_from_output(md)
        assert len(result) == 1
        assert 'should NOT be' not in result[0]['body']
