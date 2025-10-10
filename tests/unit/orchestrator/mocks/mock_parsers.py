"""
Mock Review Parser for testing orchestrator flows

Provides simulation of review parsing without actual LLM parsing.
Returns configurable review results (approved/changes_requested).
"""

from typing import Optional
from services.review_parser import ReviewStatus  # Import the real enum


class MockReviewResult:
    """Mock review result matching services.review_parser.ReviewResult"""
    
    def __init__(
        self,
        status: ReviewStatus,
        feedback: Optional[str] = None,
        blocking_issues: Optional[list] = None,
        non_blocking_issues: Optional[list] = None
    ):
        self.status = status
        self.feedback = feedback or ""
        self.blocking_issues = blocking_issues or []
        self.non_blocking_issues = non_blocking_issues or []
        self.approved = (status == ReviewStatus.APPROVED)
        
        # Add properties expected by production code
        self.blocking_count = len(self.blocking_issues)
        self.high_severity_count = len(self.blocking_issues)  # Treat blocking as high severity
        self.findings = self.blocking_issues + self.non_blocking_issues
        self.score = 1.0 if status == ReviewStatus.APPROVED else 0.5
        self.summary = feedback or f"Review status: {status.value}"


class MockReviewParser:
    """Mock review parser for testing"""
    
    def __init__(self):
        self.parse_calls = []
        self._default_result = MockReviewResult(ReviewStatus.APPROVED)
        self._results = {}
    
    def set_result(self, result_status: str, agent_name: Optional[str] = None):
        """
        Set the result for parsing
        
        Args:
            result_status: Either a string status ('approved', 'changes_requested', 'blocked')
                          or a MockReviewResult object
            agent_name: Optional agent name to set result for specific agent
        """
        # Handle string status
        if isinstance(result_status, str):
            if result_status == 'approved':
                result = MockReviewResult(ReviewStatus.APPROVED)
            elif result_status == 'changes_requested':
                result = MockReviewResult(ReviewStatus.CHANGES_REQUESTED)
            elif result_status == 'blocked':
                result = MockReviewResult(ReviewStatus.BLOCKED)
            else:
                raise ValueError(f"Unknown result status: {result_status}")
        else:
            result = result_status
        
        # Set for specific agent or as default
        if agent_name:
            self._results[agent_name] = result
        else:
            self._default_result = result
    
    def set_default_result(self, result: MockReviewResult):
        """Set default result for all agents"""
        self._default_result = result
    
    def parse_review(self, review_text: str, agent_name: Optional[str] = None) -> MockReviewResult:
        """Mock review parsing"""
        self.parse_calls.append((review_text, agent_name))
        
        # Return configured result
        if agent_name and agent_name in self._results:
            return self._results[agent_name]
        return self._default_result
    
    def reset(self):
        """Reset parser state"""
        self.parse_calls.clear()
        self._results.clear()


# Helper functions for creating common review results

def approved_review_result(feedback: str = "Looks good!") -> MockReviewResult:
    """Create an approved review result"""
    return MockReviewResult(
        status=ReviewStatus.APPROVED,
        feedback=feedback
    )


def changes_requested_result(
    feedback: str = "Please address these issues",
    issues: list = None
) -> MockReviewResult:
    """Create a changes requested review result"""
    return MockReviewResult(
        status=ReviewStatus.CHANGES_REQUESTED,
        feedback=feedback,
        non_blocking_issues=issues or ["Issue 1", "Issue 2"]
    )


def blocked_review_result(
    feedback: str = "Cannot proceed",
    blocking_issues: list = None
) -> MockReviewResult:
    """Create a blocked review result"""
    return MockReviewResult(
        status=ReviewStatus.BLOCKED,
        feedback=feedback,
        blocking_issues=blocking_issues or ["Critical issue"]
    )
