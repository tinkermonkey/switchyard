"""
Enhanced collaboration patterns for multi-agent orchestration
"""

import asyncio
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, asdict
from enum import Enum
from handoff.protocol import HandoffPackage, HandoffManager
from services.github_integration import GitHubIntegration

class ReviewStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    BLOCKED = "blocked"

@dataclass
class ReviewFeedback:
    """Structured review feedback"""
    reviewer_agent: str
    status: ReviewStatus
    findings: List[Dict[str, Any]]  # [{"category": "completeness", "severity": "high", "message": "...", "suggestion": "..."}]
    blocking_issues: int
    score: float  # 0.0 to 1.0
    comments: str
    github_comment_id: Optional[int] = None

@dataclass
class CollaborativeHandoff(HandoffPackage):
    """Extended handoff with collaboration support"""

    # Review and collaboration fields
    review_feedback: List[ReviewFeedback] = None
    github_issue_number: Optional[int] = None
    github_pr_number: Optional[int] = None
    conversation_context: List[Dict[str, Any]] = None  # Previous agent interactions
    cross_references: Dict[str, str] = None  # Links to related issues/PRs

    def __post_init__(self):
        if self.review_feedback is None:
            self.review_feedback = []
        if self.conversation_context is None:
            self.conversation_context = []
        if self.cross_references is None:
            self.cross_references = {}

class CollaborationOrchestrator:
    """Orchestrates multi-agent collaboration with GitHub integration"""

    def __init__(self, handoff_manager: HandoffManager):
        self.handoff_manager = handoff_manager
        self.github = GitHubIntegration()
        self.active_reviews = {}  # Track ongoing reviews

    async def initiate_maker_checker_flow(
        self,
        maker_agent: str,
        checker_agents: List[str],
        context: Dict[str, Any],
        artifacts: Dict[str, Any],
        github_issue: Optional[int] = None
    ) -> CollaborativeHandoff:
        """Start a maker-checker collaboration flow"""

        # Create collaborative handoff
        handoff = CollaborativeHandoff(
            handoff_id=f"collab_{context.get('pipeline_id')}_{maker_agent}",
            timestamp=context.get('timestamp'),
            source_agent=maker_agent,
            target_agent="review_coordinator",
            pipeline_id=context.get('pipeline_id'),
            task_context=context,
            completed_work=context.get('completed_work', []),
            decisions_made=context.get('decisions_made', []),
            artifacts=artifacts,
            quality_metrics=context.get('quality_metrics', {}),
            validation_results={},
            required_actions=[f"Review by {', '.join(checker_agents)}"],
            constraints=context.get('constraints', []),
            success_criteria=context.get('success_criteria', []),
            github_issue_number=github_issue
        )

        # Post maker's work to GitHub
        if github_issue:
            comment = await self.format_maker_comment(maker_agent, handoff)
            comment_response = await self.github.post_issue_comment(
                github_issue, comment
            )

            # Add GitHub reference
            handoff.cross_references['maker_comment'] = comment_response.get('html_url')

        # Initiate parallel reviews
        review_tasks = []
        for checker in checker_agents:
            review_tasks.append(
                self.request_agent_review(checker, handoff)
            )

        # Wait for all reviews (with timeout)
        try:
            reviews = await asyncio.wait_for(
                asyncio.gather(*review_tasks),
                timeout=300  # 5 minutes
            )
            handoff.review_feedback.extend(reviews)
        except asyncio.TimeoutError:
            handoff.warnings = handoff.warnings or []
            handoff.warnings.append("Some reviews timed out")

        # Process review results
        final_status = await self.consolidate_reviews(handoff)

        # Post consolidated results to GitHub
        if github_issue:
            consolidated_comment = await self.format_review_summary(handoff, final_status)
            await self.github.post_issue_comment(github_issue, consolidated_comment)

        return handoff

    async def request_agent_review(self, reviewer_agent: str, handoff: CollaborativeHandoff) -> ReviewFeedback:
        """Request review from a specific agent"""

        # Create review context
        review_context = {
            'review_request': True,
            'original_handoff': handoff,
            'focus_areas': self.get_review_focus(reviewer_agent),
            'github_issue': handoff.github_issue_number
        }

        # Create handoff to reviewer
        review_handoff = await self.handoff_manager.create_handoff(
            source_agent="review_coordinator",
            target_agent=reviewer_agent,
            context=review_context,
            artifacts=handoff.artifacts
        )

        # Execute reviewer agent (this would be handled by the pipeline)
        # For now, return placeholder - actual execution happens in pipeline
        return ReviewFeedback(
            reviewer_agent=reviewer_agent,
            status=ReviewStatus.PENDING,
            findings=[],
            blocking_issues=0,
            score=0.0,
            comments=f"Review requested from {reviewer_agent}"
        )

    async def process_review_response(
        self,
        reviewer_agent: str,
        review_result: Dict[str, Any],
        handoff_id: str
    ) -> ReviewFeedback:
        """Process a review response from an agent"""

        # Parse review result into structured feedback
        findings = review_result.get('findings', [])
        blocking_count = sum(1 for f in findings if f.get('severity') == 'blocking')

        # Determine status
        if blocking_count > 0:
            status = ReviewStatus.BLOCKED
        elif any(f.get('severity') == 'high' for f in findings):
            status = ReviewStatus.CHANGES_REQUESTED
        else:
            status = ReviewStatus.APPROVED

        # Create review feedback
        feedback = ReviewFeedback(
            reviewer_agent=reviewer_agent,
            status=status,
            findings=findings,
            blocking_issues=blocking_count,
            score=review_result.get('quality_score', 0.5),
            comments=review_result.get('summary', '')
        )

        # Post individual review to GitHub if issue specified
        handoff = self.get_handoff(handoff_id)
        if handoff and handoff.github_issue_number:
            comment = await self.format_individual_review(feedback)
            comment_response = await self.github.post_issue_comment(
                handoff.github_issue_number, comment
            )
            feedback.github_comment_id = comment_response.get('id')

        return feedback

    async def consolidate_reviews(self, handoff: CollaborativeHandoff) -> str:
        """Consolidate multiple reviews into final decision"""

        approved_count = sum(1 for r in handoff.review_feedback if r.status == ReviewStatus.APPROVED)
        blocked_count = sum(1 for r in handoff.review_feedback if r.status == ReviewStatus.BLOCKED)
        changes_count = sum(1 for r in handoff.review_feedback if r.status == ReviewStatus.CHANGES_REQUESTED)

        total_blocking = sum(r.blocking_issues for r in handoff.review_feedback)
        avg_score = sum(r.score for r in handoff.review_feedback) / len(handoff.review_feedback) if handoff.review_feedback else 0

        # Decision logic
        if blocked_count > 0 or total_blocking > 0:
            return "BLOCKED"
        elif changes_count > approved_count:
            return "CHANGES_REQUESTED"
        elif avg_score >= 0.7:
            return "APPROVED"
        else:
            return "NEEDS_IMPROVEMENT"

    async def format_maker_comment(self, maker_agent: str, handoff: CollaborativeHandoff) -> str:
        """Format maker agent's work as GitHub comment"""

        completed_work = '\n'.join(f"✅ {work}" for work in handoff.completed_work)
        decisions = '\n'.join(f"🔹 **{d.get('topic', 'Decision')}**: {d.get('decision', '')}" for d in handoff.decisions_made)

        artifacts_summary = ""
        for name, artifact in handoff.artifacts.items():
            if isinstance(artifact, dict) and 'summary' in artifact:
                artifacts_summary += f"\n📋 **{name}**: {artifact['summary']}"

        return f"""## 🤖 {maker_agent.replace('_', ' ').title()} - Work Complete

### Summary
{handoff.task_context.get('summary', 'Analysis completed')}

### Work Completed
{completed_work}

### Key Decisions Made
{decisions}

### Artifacts Generated
{artifacts_summary}

### Quality Metrics
{self.format_quality_metrics(handoff.quality_metrics)}

### Next Steps
Requesting review from: {', '.join(handoff.required_actions)}

---
*Generated by Claude Code Orchestrator*
"""

    async def format_individual_review(self, feedback: ReviewFeedback) -> str:
        """Format individual review as GitHub comment"""

        status_emoji = {
            ReviewStatus.APPROVED: "✅",
            ReviewStatus.CHANGES_REQUESTED: "🔄",
            ReviewStatus.BLOCKED: "🚫",
            ReviewStatus.PENDING: "⏳"
        }

        findings_text = ""
        for finding in feedback.findings:
            severity_emoji = {"low": "ℹ️", "medium": "⚠️", "high": "❗", "blocking": "🚫"}
            emoji = severity_emoji.get(finding.get('severity', 'medium'), "•")
            findings_text += f"\n{emoji} **{finding.get('category', 'General')}**: {finding.get('message', '')}"
            if finding.get('suggestion'):
                findings_text += f"\n   💡 *Suggestion: {finding.get('suggestion')}*"

        return f"""## {status_emoji[feedback.status]} {feedback.reviewer_agent.replace('_', ' ').title()} Review

**Overall Score:** {feedback.score:.1%}
**Status:** {feedback.status.value.replace('_', ' ').title()}

### Review Findings
{findings_text or "No specific issues found."}

### Summary
{feedback.comments}

---
*Review by Claude Code Orchestrator*
"""

    async def format_review_summary(self, handoff: CollaborativeHandoff, final_status: str) -> str:
        """Format consolidated review summary"""

        status_emoji = {"APPROVED": "✅", "CHANGES_REQUESTED": "🔄", "BLOCKED": "🚫", "NEEDS_IMPROVEMENT": "⚠️"}

        review_summary = ""
        for feedback in handoff.review_feedback:
            review_summary += f"\n• **{feedback.reviewer_agent.replace('_', ' ').title()}**: {feedback.status.value} (Score: {feedback.score:.1%})"

        return f"""## {status_emoji.get(final_status, '📋')} Review Summary - {final_status.replace('_', ' ').title()}

### Review Results
{review_summary}

### Overall Assessment
{self.get_status_description(final_status)}

### Next Actions
{self.get_next_actions(final_status)}

---
*Consolidated review by Claude Code Orchestrator*
"""

    def format_quality_metrics(self, metrics: Dict[str, float]) -> str:
        """Format quality metrics for display"""
        if not metrics:
            return "No metrics available"

        formatted = []
        for metric, value in metrics.items():
            percentage = f"{value:.1%}" if isinstance(value, float) and value <= 1 else str(value)
            formatted.append(f"**{metric.replace('_', ' ').title()}**: {percentage}")

        return '\n'.join(formatted)

    def get_review_focus(self, reviewer_agent: str) -> List[str]:
        """Get focus areas for specific reviewer types"""
        focus_map = {
            'requirements_reviewer': ['completeness', 'clarity', 'testability'],
            'architecture_reviewer': ['scalability', 'maintainability', 'security'],
            'code_reviewer': ['quality', 'performance', 'best_practices'],
            'security_reviewer': ['vulnerabilities', 'compliance', 'authentication'],
            'performance_reviewer': ['efficiency', 'scalability', 'resource_usage']
        }
        return focus_map.get(reviewer_agent, ['general_quality'])

    def get_status_description(self, status: str) -> str:
        descriptions = {
            'APPROVED': 'All reviews passed. Ready to proceed to next stage.',
            'CHANGES_REQUESTED': 'Non-blocking issues identified. Address feedback and continue.',
            'BLOCKED': 'Critical issues must be resolved before proceeding.',
            'NEEDS_IMPROVEMENT': 'Overall quality below threshold. Revision recommended.'
        }
        return descriptions.get(status, 'Status unknown')

    def get_next_actions(self, status: str) -> str:
        actions = {
            'APPROVED': '🚀 Proceed to next pipeline stage',
            'CHANGES_REQUESTED': '📝 Address feedback and update work',
            'BLOCKED': '🛑 Resolve blocking issues before continuing',
            'NEEDS_IMPROVEMENT': '🔄 Revise and resubmit for review'
        }
        return actions.get(status, 'Review status and determine next steps')

    def get_handoff(self, handoff_id: str) -> Optional[CollaborativeHandoff]:
        """Retrieve handoff by ID (placeholder - implement with state manager)"""
        # This would retrieve from state manager
        return None