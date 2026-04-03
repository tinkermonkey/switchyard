"""
Unit tests for PRReviewStage prompt-builder methods.

These three methods are pure functions (depend only on default_loader and re)
and can be tested without the Docker container environment that the broader
test_pr_review_stage.py requires.

Covers:
  - _build_pr_review_prompt()
  - _build_verification_prompt()
  - _build_consolidation_prompt()
"""

import re
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Fixture: PRReviewStage instance with all Docker-dependent imports mocked
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def stage():
    """PRReviewStage with infrastructure dependencies mocked out.

    The module-level skip in test_pr_review_stage.py exists because importing
    PRReviewStage triggers services.dev_container_state which creates
    /app/state/dev_containers.  We mock that module in sys.modules before the
    import so the path is never touched.

    Uses yield (not return) so that patch.dict stays active for the entire
    module scope.  This prevents any test that accidentally calls a method
    relying on pr_review_state_manager or ConfigManager from hitting the real
    singleton.  Only the three prompt-builder methods under test are safe to
    call on this instance — they use only default_loader and re.
    """
    with patch.dict("sys.modules", {"services.dev_container_state": MagicMock()}):
        with patch("pipeline.pr_review_stage.ConfigManager"), \
             patch("pipeline.pr_review_stage.GitHubStateManager"), \
             patch("pipeline.pr_review_stage.pr_review_state_manager"):
            from pipeline.pr_review_stage import PRReviewStage
            yield PRReviewStage()


# ---------------------------------------------------------------------------
# _build_pr_review_prompt
# ---------------------------------------------------------------------------

class TestBuildPrReviewPrompt:

    def test_pr_url_appears_in_output(self, stage):
        result = stage._build_pr_review_prompt("https://github.com/org/repo/pull/42")
        assert "https://github.com/org/repo/pull/42" in result

    def test_checkout_instruction_added_when_pr_number_parseable(self, stage):
        result = stage._build_pr_review_prompt("https://github.com/org/repo/pull/42")
        assert "gh pr checkout 42" in result

    def test_checkout_instruction_absent_when_url_has_no_pr_number(self, stage):
        # URL without /pull/<number> suffix — regex won't match
        result = stage._build_pr_review_prompt("https://github.com/org/repo/pulls")
        assert "gh pr checkout" not in result

    def test_prior_cycle_section_absent_when_no_context(self, stage):
        result = stage._build_pr_review_prompt("https://github.com/org/repo/pull/1")
        # prior_cycles.md contains "Prior Review Cycles"
        assert "Prior Review Cycles" not in result

    def test_prior_cycle_section_present_when_context_provided(self, stage):
        result = stage._build_pr_review_prompt(
            "https://github.com/org/repo/pull/1",
            prior_cycle_context="Cycle 1 found and fixed auth bug.",
        )
        assert "Prior Review Cycles" in result
        assert "Cycle 1 found and fixed auth bug." in result

    def test_output_is_non_empty_string(self, stage):
        result = stage._build_pr_review_prompt("https://github.com/org/repo/pull/7")
        assert isinstance(result, str) and result

    def test_no_unresolved_format_placeholders(self, stage):
        result = stage._build_pr_review_prompt(
            "https://github.com/org/repo/pull/7",
            prior_cycle_context="some context",
        )
        # Checks for bare {lowercase_word} that survived .format() substitution.
        # This regex does NOT flag JSON keys like {"groups": ...} (quotes inside)
        # or escaped {{ }} literal braces (converted to { } by .format() only
        # when surrounding non-alphabetic content).  If a future template edit
        # adds an AI-facing {{example_var}} placeholder, it will appear as
        # {example_var} in the output and trigger this check — that is the
        # intended behaviour, as it signals a template convention change that
        # needs explicit review.
        unresolved = re.findall(r'\{[a-z_]+\}', result)
        assert unresolved == [], f"Unresolved placeholders: {unresolved}"

    def test_main_review_content_present(self, stage):
        result = stage._build_pr_review_prompt("https://github.com/org/repo/pull/1")
        # Key instructional phrase from main_review.md
        assert "PR Review Specialist" in result


# ---------------------------------------------------------------------------
# _build_verification_prompt
# ---------------------------------------------------------------------------

class TestBuildVerificationPrompt:

    def _call(self, stage, authority_key="business_analyst",
              context_content="Requirements text", context_name="Business Requirements",
              pr_url="https://github.com/org/repo/pull/99"):
        return stage._build_verification_prompt(pr_url, context_name, authority_key, context_content)

    def test_pr_url_appears_in_output(self, stage):
        result = self._call(stage)
        assert "https://github.com/org/repo/pull/99" in result

    def test_context_name_appears_in_output(self, stage):
        result = self._call(stage, context_name="Architecture Spec")
        assert "Architecture Spec" in result

    def test_context_content_appears_in_output(self, stage):
        result = self._call(stage, context_content="Must support OAuth2")
        assert "Must support OAuth2" in result

    def test_business_analyst_authority_framing(self, stage):
        result = self._call(stage, authority_key="business_analyst")
        # authority_business_analyst.md: "functional business requirements"
        assert "functional business requirements" in result
        # Negative: must NOT contain phrases unique to the other two authority files
        assert "architectural decisions" not in result
        assert "Acceptance Criteria" not in result

    def test_software_architect_authority_framing(self, stage):
        result = self._call(stage, authority_key="software_architect")
        # authority_software_architect.md: "architectural decisions"
        assert "architectural decisions" in result
        # Negative: must NOT contain phrases unique to the other two authority files
        assert "functional business requirements" not in result
        assert "Acceptance Criteria" not in result

    def test_unknown_authority_key_uses_default_framing(self, stage):
        result = self._call(stage, authority_key="idea_researcher")
        # authority_default.md: "Acceptance Criteria"
        assert "Acceptance Criteria" in result
        # Negative: must NOT contain phrases unique to the named authority files
        assert "functional business requirements" not in result
        assert "architectural decisions" not in result

    def test_empty_authority_key_uses_default_framing(self, stage):
        result = self._call(stage, authority_key="")
        assert "Acceptance Criteria" in result
        assert "functional business requirements" not in result
        assert "architectural decisions" not in result

    def test_context_truncated_at_15000_chars(self, stage):
        long_content = "x" * 20000
        result = self._call(stage, context_content=long_content)
        assert "[... truncated ...]" in result
        # The raw over-length content should not be fully present
        assert "x" * 20000 not in result

    def test_short_context_not_truncated(self, stage):
        content = "Short requirements text"
        result = self._call(stage, context_content=content)
        assert "[... truncated ...]" not in result
        assert content in result

    def test_output_is_non_empty_string(self, stage):
        result = self._call(stage)
        assert isinstance(result, str) and result

    def test_no_unresolved_format_placeholders(self, stage):
        result = self._call(stage)
        # See note in TestBuildPrReviewPrompt.test_no_unresolved_format_placeholders
        # for the documented limitations of this regex.
        unresolved = re.findall(r'\{[a-z_]+\}', result)
        assert unresolved == [], f"Unresolved placeholders: {unresolved}"

    def test_verification_specialist_content_present(self, stage):
        result = self._call(stage)
        # Key phrase from verification_main.md
        assert "Requirements Verification Specialist" in result


# ---------------------------------------------------------------------------
# _build_consolidation_prompt
# ---------------------------------------------------------------------------

class TestBuildConsolidationPrompt:

    def test_single_source_block_embedded(self, stage):
        result = stage._build_consolidation_prompt([
            ("Code Review", "### Critical Issues\n- **Missing auth**: No OAuth check"),
        ])
        assert "### Source: Code Review" in result
        assert "Missing auth" in result

    def test_multiple_source_blocks_all_embedded(self, stage):
        result = stage._build_consolidation_prompt([
            ("Code Review", "findings A"),
            ("Requirements Verification", "findings B"),
        ])
        assert "### Source: Code Review" in result
        assert "findings A" in result
        assert "### Source: Requirements Verification" in result
        assert "findings B" in result

    def test_source_blocks_separated_by_horizontal_rule(self, stage):
        result = stage._build_consolidation_prompt([
            ("Phase 1", "text1"),
            ("Phase 2", "text2"),
        ])
        # Each block ends with --- separator
        assert "---" in result

    def test_empty_phase_outputs_produces_valid_prompt(self, stage):
        result = stage._build_consolidation_prompt([])
        assert isinstance(result, str) and result
        # phase_blocks is empty string; template content still present
        assert "PR Review Consolidator" in result

    def test_output_is_non_empty_string(self, stage):
        result = stage._build_consolidation_prompt([("Source", "findings")])
        assert isinstance(result, str) and result

    def test_no_unresolved_format_placeholders(self, stage):
        result = stage._build_consolidation_prompt([("Source A", "text")])
        # consolidation.md uses {{ }} around its JSON schema keys (e.g. {{"groups": ...}})
        # which become literal { } after .format().  Those JSON keys contain quotes, colons,
        # or uppercase — not matched by \{[a-z_]+\}.  See the note in
        # TestBuildPrReviewPrompt.test_no_unresolved_format_placeholders for full caveat.
        unresolved = re.findall(r'\{[a-z_]+\}', result)
        assert unresolved == [], f"Unresolved placeholders: {unresolved}"

    def test_consolidation_content_present(self, stage):
        result = stage._build_consolidation_prompt([("Source", "text")])
        # Key phrase from consolidation.md
        assert "PR Review Consolidator" in result
