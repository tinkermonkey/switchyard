"""
Unit tests for review parser

Tests the parsing logic for extracting status, findings, and scores
from agent review outputs.

CRITICAL: This determines whether work proceeds or goes back for revision.
"""

import pytest
from services.review_parser import (
    ReviewParser,
    ReviewStatus,
    ReviewFinding,
    ReviewResult
)


@pytest.fixture
def parser():
    """Create a ReviewParser instance"""
    return ReviewParser()


class TestStatusDetection:
    """Test detection of review status from text"""

    def test_explicit_approved_status(self, parser):
        """Test: Explicit 'Status: APPROVED' declaration"""
        review = """
        Status: APPROVED

        All requirements look good.
        """

        result = parser.parse_review(review)
        assert result.status == ReviewStatus.APPROVED

    def test_explicit_blocked_status(self, parser):
        """Test: Explicit 'Status: BLOCKED' declaration"""
        review = """
        Status: BLOCKED

        Critical issues must be addressed.
        """

        result = parser.parse_review(review)
        assert result.status == ReviewStatus.BLOCKED

    def test_explicit_changes_requested_status(self, parser):
        """Test: Explicit 'Status: CHANGES REQUESTED' declaration"""
        review = """
        Status: CHANGES REQUESTED

        Please address the following issues.
        """

        result = parser.parse_review(review)
        assert result.status == ReviewStatus.CHANGES_REQUESTED

    def test_approved_all_checks_passed(self, parser):
        """Test: APPROVED from 'all checks passed'"""
        review = """
        ## Review Complete

        All checks have passed. Ready to proceed to next stage.
        """

        result = parser.parse_review(review)
        assert result.status == ReviewStatus.APPROVED

    def test_approved_no_issues_found(self, parser):
        """Test: APPROVED from 'no issues found'"""
        review = """
        ## Review Results

        No blocking issues found. The requirements are clear and complete.
        """

        result = parser.parse_review(review)
        assert result.status == ReviewStatus.APPROVED

    def test_blocked_current_blocking_issues(self, parser):
        """Test: BLOCKED from current blocking issues"""
        review = """
        ## Review Results

        Current blocking issues remain:
        - Critical: Missing user authentication requirements
        - Critical: No error handling specified
        """

        result = parser.parse_review(review)
        assert result.status == ReviewStatus.BLOCKED

    def test_blocked_cannot_proceed(self, parser):
        """Test: BLOCKED from 'cannot proceed'"""
        review = """
        ## Review Results

        Cannot proceed until security requirements are defined.
        """

        result = parser.parse_review(review)
        assert result.status == ReviewStatus.BLOCKED

    def test_changes_requested_non_blocking_issues(self, parser):
        """Test: CHANGES_REQUESTED from non-blocking issues"""
        review = """
        ## Review Results

        Non-blocking issues identified:
        - Minor: Add more detail to user story 3
        - Low: Consider edge case for empty input
        """

        result = parser.parse_review(review)
        assert result.status == ReviewStatus.CHANGES_REQUESTED

    def test_resolved_issues_marked_approved(self, parser):
        """
        Test: Past tense blocking issues should be APPROVED not BLOCKED

        CRITICAL: "Successfully addressing all blocking issues" means
        issues are RESOLVED, not currently blocking.
        """
        review = """
        ## Iteration 3 Review

        Successfully addressing all blocking issues from iteration 2.
        All critical requirements are now complete and well-specified.

        Status: APPROVED
        """

        result = parser.parse_review(review)
        assert result.status == ReviewStatus.APPROVED

    def test_status_inference_from_blocking_findings(self, parser):
        """Test: Infer BLOCKED status from blocking findings"""
        review = """
        ## Issues Found

        - Blocking: Missing security requirements
        - High: Unclear acceptance criteria
        """

        result = parser.parse_review(review)
        # Should infer BLOCKED from blocking finding
        assert result.status == ReviewStatus.BLOCKED
        assert result.blocking_count == 1

    def test_status_inference_from_high_severity_findings(self, parser):
        """Test: Infer CHANGES_REQUESTED from high severity findings"""
        review = """
        ## Issues Found

        - High: Add more detail to user story
        - Medium: Consider edge case
        """

        result = parser.parse_review(review)
        # Should infer CHANGES_REQUESTED from high severity
        assert result.status == ReviewStatus.CHANGES_REQUESTED
        assert result.high_severity_count == 1

    def test_status_inference_no_findings_approved(self, parser):
        """Test: Infer APPROVED if no findings"""
        review = """
        ## Review Complete

        The requirements are well written and complete.
        """

        result = parser.parse_review(review)
        # No findings and no explicit status → should infer APPROVED
        assert result.status == ReviewStatus.APPROVED
        assert len(result.findings) == 0


class TestFindingExtraction:
    """Test extraction of review findings"""

    def test_extract_structured_findings(self, parser):
        """Test: Extract findings from structured markdown section"""
        review = """
        ## Issues Found

        - **Security**: Missing authentication requirements
        - **Clarity**: User story 3 needs more detail
        - **Completeness**: No error handling specified
        """

        result = parser.parse_review(review)

        assert len(result.findings) == 3
        assert result.findings[0].category == 'Security'
        assert result.findings[1].category == 'Clarity'
        assert result.findings[2].category == 'Completeness'

    def test_extract_severity_levels(self, parser):
        """Test: Extract severity from finding text"""
        review = """
        ## Issues Found

        - Blocking: Critical security issue
        - High: Important clarity improvement needed
        - Medium: Consider adding example
        - Low: Minor typo in user story
        """

        result = parser.parse_review(review)

        assert len(result.findings) >= 4
        # Find each severity level
        severities = [f.severity for f in result.findings]
        assert 'blocking' in severities
        assert 'high' in severities
        assert 'medium' in severities
        assert 'low' in severities

    def test_extract_inline_findings(self, parser):
        """Test: Extract high-priority findings from inline text"""
        review = """
        ## Review Results

        **Critical Issue**: Missing security requirements

        The requirements document lacks authentication and authorization
        specifications, which is a blocking issue.

        **High Priority**: Unclear acceptance criteria for story 5
        """

        result = parser.parse_review(review)

        # Should extract high-priority inline findings
        assert len(result.findings) >= 2
        categories = [f.category for f in result.findings]
        assert 'Critical Issue' in categories or 'general' in categories

    def test_extract_suggestions(self, parser):
        """Test: Extract suggestions for findings"""
        review = """
        ## Issues Found

        - **Security**: Missing authentication requirements
          Suggestion: Add section 3.1 for authentication flows
        - **Clarity**: User story 3 needs detail
          Suggestion: Include specific user actions
        """

        result = parser.parse_review(review)

        # At least one finding should have a suggestion
        suggestions = [f.suggestion for f in result.findings if f.suggestion]
        assert len(suggestions) > 0

    def test_deduplicate_findings(self, parser):
        """Test: Deduplicate findings with same message"""
        review = """
        ## Issues Found

        - Missing security requirements

        ## Critical Problems

        - Missing security requirements
        """

        result = parser.parse_review(review)

        # Should have only one finding, not two
        messages = [f.message for f in result.findings]
        assert len(messages) == len(set(messages))  # All unique

    def test_count_blocking_findings(self, parser):
        """Test: Count blocking findings correctly"""
        review = """
        ## Issues Found

        - Blocking: Issue 1
        - Blocking: Issue 2
        - High: Issue 3
        - Medium: Issue 4
        """

        result = parser.parse_review(review)

        assert result.blocking_count == 2
        assert result.high_severity_count == 1


class TestScoreExtraction:
    """Test extraction of quality scores"""

    def test_extract_percentage_score(self, parser):
        """Test: Extract score from percentage format"""
        review = """
        ## Review Results

        Score: 85%

        Good requirements overall.
        """

        result = parser.parse_review(review)

        assert result.score == 0.85

    def test_extract_decimal_score(self, parser):
        """Test: Extract score from decimal format"""
        review = """
        ## Review Results

        Quality: 0.92

        Excellent requirements.
        """

        result = parser.parse_review(review)

        assert result.score == 0.92

    def test_extract_fraction_score(self, parser):
        """Test: Extract score from fraction format"""
        review = """
        ## Review Results

        Rating: 8.5/10

        Very good requirements.
        """

        result = parser.parse_review(review)

        # 8.5/10 should normalize to 0.85
        assert result.score == 0.85

    def test_no_score_returns_zero(self, parser):
        """Test: Return 0.0 if no score found"""
        review = """
        ## Review Results

        Good requirements overall.
        """

        result = parser.parse_review(review)

        assert result.score == 0.0


class TestSummaryExtraction:
    """Test extraction of review summary"""

    def test_extract_summary_section(self, parser):
        """Test: Extract summary from dedicated section"""
        review = """
        ## Summary

        The requirements are clear and complete. Ready to proceed.

        ## Details

        All user stories follow INVEST principles.
        """

        result = parser.parse_review(review)

        assert 'clear and complete' in result.summary
        assert 'Ready to proceed' in result.summary

    def test_extract_overall_section(self, parser):
        """Test: Extract from 'Overall' section"""
        review = """
        ## Overall

        Excellent requirements with no blocking issues.

        ## Details

        Minor improvements suggested.
        """

        result = parser.parse_review(review)

        assert 'Excellent requirements' in result.summary

    def test_fallback_to_first_paragraph(self, parser):
        """Test: Fallback to first paragraph if no summary section"""
        review = """
        This is the first paragraph with the main conclusion.

        ## Details

        More details here.
        """

        result = parser.parse_review(review)

        assert 'first paragraph' in result.summary


class TestReviewResultSerialization:
    """Test ReviewResult serialization"""

    def test_review_result_to_dict(self, parser):
        """Test: ReviewResult serializes to dict correctly"""
        review = """
        Status: APPROVED

        Score: 90%

        ## Summary
        Excellent work.

        ## Issues Found
        - Low: Minor typo
        """

        result = parser.parse_review(review)
        result_dict = result.to_dict()

        assert result_dict['status'] == 'approved'
        assert result_dict['score'] == 0.90
        assert 'Excellent work' in result_dict['summary']
        assert len(result_dict['findings']) >= 1
        assert result_dict['blocking_count'] == 0

    def test_finding_to_dict(self):
        """Test: ReviewFinding serializes to dict correctly"""
        finding = ReviewFinding(
            category='Security',
            severity='blocking',
            message='Missing authentication requirements',
            suggestion='Add section 3.1'
        )

        finding_dict = finding.to_dict()

        assert finding_dict['category'] == 'Security'
        assert finding_dict['severity'] == 'blocking'
        assert finding_dict['message'] == 'Missing authentication requirements'
        assert finding_dict['suggestion'] == 'Add section 3.1'


class TestEdgeCases:
    """Test edge cases and error handling"""

    def test_empty_review(self, parser):
        """Test: Handle empty review gracefully"""
        review = ""

        result = parser.parse_review(review)

        # Should return UNKNOWN or APPROVED (no findings)
        assert result.status in [ReviewStatus.UNKNOWN, ReviewStatus.APPROVED]
        assert len(result.findings) == 0

    def test_review_with_only_whitespace(self, parser):
        """Test: Handle whitespace-only review"""
        review = "   \n\n   \t\t   \n   "

        result = parser.parse_review(review)

        assert result.status in [ReviewStatus.UNKNOWN, ReviewStatus.APPROVED]
        assert len(result.findings) == 0

    def test_mixed_status_signals(self, parser):
        """
        Test: Handle mixed status signals (explicit status wins)

        Text contains both "blocking issues" and "Status: APPROVED"
        Explicit status declaration should take precedence.
        """
        review = """
        ## Iteration 3 Review

        The previous iteration had blocking issues, but these have
        been successfully resolved.

        Status: APPROVED
        """

        result = parser.parse_review(review)

        # Explicit "Status: APPROVED" should override "blocking issues" text
        assert result.status == ReviewStatus.APPROVED

    def test_case_insensitive_status_detection(self, parser):
        """Test: Status detection is case-insensitive"""
        reviews = [
            "status: approved",
            "STATUS: APPROVED",
            "Status: Approved",
            "all checks have PASSED"
        ]

        for review in reviews:
            result = parser.parse_review(review)
            assert result.status == ReviewStatus.APPROVED

    def test_unicode_emoji_markers(self, parser):
        """Test: Handle unicode emoji markers"""
        review = """
        Approved

        One blocking issue:
        - Missing security requirements

        High priority:
        - Add more detail
        """

        result = parser.parse_review(review)

        # Should extract findings
        assert len(result.findings) >= 2

    def test_multiple_findings_same_severity(self, parser):
        """Test: Handle multiple findings with same severity"""
        review = """
        ## Issues Found

        - Blocking: Issue 1
        - Blocking: Issue 2
        - Blocking: Issue 3
        """

        result = parser.parse_review(review)

        assert result.blocking_count == 3
        assert len(result.findings) == 3

    def test_finding_without_category(self, parser):
        """Test: Handle findings without explicit category"""
        review = """
        ## Issues Found

        - Missing security requirements
        - Unclear acceptance criteria
        """

        result = parser.parse_review(review)

        # Should assign default 'general' category
        assert len(result.findings) >= 2
        categories = [f.category for f in result.findings]
        assert 'general' in categories

    def test_very_long_review(self, parser):
        """Test: Handle very long review text"""
        # Create a review with many findings
        issues = "\n".join([f"- High: Issue {i}" for i in range(50)])
        review = f"""
        ## Issues Found

        {issues}
        """

        result = parser.parse_review(review)

        # Should extract all 50 findings
        assert len(result.findings) == 50
        assert result.high_severity_count == 50


class TestRealWorldExamples:
    """Test with real-world review examples"""

    def test_requirements_reviewer_approved(self, parser):
        """Test: Real requirements reviewer approval"""
        review = """
## Review Complete - Iteration 3

Successfully addressing all blocking issues from iteration 2.

## Assessment

### Completeness
All critical requirements now specified:
- Authentication flows (new section 3.1)
- Error handling scenarios (new section 4.2)
- Data validation rules (expanded section 2.3)

### Clarity
User stories now follow INVEST principles with clear acceptance criteria.

### Quality Score
Rating: 9.5/10

## Summary

All blocking issues have been resolved. Requirements are now complete,
clear, and ready for architecture design.

Status: APPROVED
        """

        result = parser.parse_review(review)

        assert result.status == ReviewStatus.APPROVED
        assert result.score == 0.95
        assert result.blocking_count == 0

    def test_requirements_reviewer_blocked(self, parser):
        """Test: Real requirements reviewer blocking review"""
        review = """
## Review Results - Iteration 1

## Issues Found

### Blocking Issues

- **Security**: Missing authentication and authorization requirements
  Suggestion: Add section 3.1 detailing authentication flows and role-based access control

- **Critical Gap**: No error handling scenarios specified
  Suggestion: Add section 4.2 covering error conditions and user feedback

- **Data Validation**: Insufficient validation rules for user inputs
  Recommended: Expand section 2.3 with specific validation criteria

### High Priority

- **User Story 3**: Lacks specific acceptance criteria
  Suggestion: Use Given-When-Then format for clarity

## Summary

Cannot proceed to architecture design until critical security and error
handling requirements are defined. These are blocking issues that must
be addressed in the next iteration.

Status: BLOCKED
        """

        result = parser.parse_review(review)

        assert result.status == ReviewStatus.BLOCKED
        assert result.blocking_count == 3
        assert result.high_severity_count == 1
        assert len(result.findings) >= 4

        # Check suggestions were extracted
        suggestions = [f.suggestion for f in result.findings if f.suggestion]
        assert len(suggestions) >= 2

    def test_code_reviewer_changes_requested(self, parser):
        """Test: Real code reviewer requesting changes"""
        review = """
## Code Review - Senior Software Engineer

## Summary
Good implementation overall, but some improvements needed before merging.

Score: 75%

## Issues Found

### Must Fix
- **Error Handling**: Missing try-catch blocks in API calls (lines 45-67)
- **Security**: SQL injection vulnerability in query builder (line 123)

### Should Fix
- **Performance**: N+1 query in user lookup (lines 89-95)
- **Code Quality**: Duplicated logic in validation methods

### Consider
- **Maintainability**: Extract magic numbers to constants
- **Testing**: Add edge case tests for empty inputs

## Recommendation

Status: CHANGES REQUESTED

Please address Must Fix and Should Fix items, then request re-review.
        """

        result = parser.parse_review(review)

        assert result.status == ReviewStatus.CHANGES_REQUESTED
        assert result.score == 0.75
        assert len(result.findings) >= 6


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
