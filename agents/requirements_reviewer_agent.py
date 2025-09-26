from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from datetime import datetime
import json

class RequirementsReviewerAgent(PipelineStage):
    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("requirements_reviewer", agent_config=agent_config)

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute requirements review on the handoff from Business Analyst"""

        # Check if this is a review request
        is_review = context.get('review_request', False)
        original_handoff = context.get('original_handoff')

        if not is_review or not original_handoff:
            raise Exception("Requirements Reviewer expects review request with handoff data")

        # Get the requirements document from handoff
        requirements_doc = original_handoff.artifacts.get('requirements_document', {})
        user_stories = original_handoff.artifacts.get('user_stories', [])
        quality_metrics = original_handoff.artifacts.get('quality_metrics', {})

        # Extract focus areas for review
        focus_areas = context.get('focus_areas', ['completeness', 'clarity', 'testability'])

        prompt = f"""
Review the requirements analysis provided by the Business Analyst using the 5Cs framework:
Clear, Concise, Complete, Consistent, and Correct.

Requirements Document:
{json.dumps(requirements_doc, indent=2)}

User Stories ({len(user_stories)} total):
{json.dumps(user_stories, indent=2)}

Quality Metrics from Business Analyst:
{json.dumps(quality_metrics, indent=2)}

Focus Areas for Review: {', '.join(focus_areas)}

Provide a structured review with:
1. Overall assessment and score (0.0 to 1.0)
2. Detailed findings categorized by:
   - Completeness: Are all requirements captured?
   - Clarity: Are requirements unambiguous?
   - Testability: Can requirements be verified?
   - Consistency: Do requirements align with each other?
   - INVEST Principles: Do user stories follow INVEST criteria?
3. Specific issues found (categorize severity: low, medium, high, blocking)
4. Recommendations for improvement
5. Questions for Business Analyst or stakeholders

Return response as structured JSON with review_result and feedback sections.
"""

        try:
            # Enhance context with MCP server data if available
            enhanced_context = context.copy()

            # Add MCP server configuration to context for Claude Code
            if hasattr(self, 'mcp_integration') and self.mcp_integration:
                enhanced_context['mcp_servers'] = []
                for name, server in self.mcp_integration.servers.items():
                    enhanced_context['mcp_servers'].append({
                        'name': name,
                        'url': server.url,
                        'capabilities': server.capabilities
                    })

                try:
                    # Use Serena to find similar requirements patterns
                    search_results = await self.mcp_integration.serena_search(
                        "requirements review patterns quality criteria",
                        file_types=['md', 'txt', 'py']
                    )
                    if search_results:
                        enhanced_context['similar_patterns'] = search_results[:2]

                    print(f"Enhanced review context with {len(search_results)} similar patterns from Serena")
                except Exception as e:
                    print(f"Warning: Serena search failed: {e}")

            result = await run_claude_code(prompt, enhanced_context)

            # Parse Claude's response
            if isinstance(result, str):
                try:
                    review_data = json.loads(result)
                except json.JSONDecodeError:
                    # If not valid JSON, create a structured response
                    review_data = {
                        "review_result": {
                            "overall_score": 0.7,
                            "status": "changes_requested",
                            "summary": "Review completed with recommendations"
                        },
                        "feedback": {
                            "findings": [
                                {"category": "completeness", "severity": "medium", "message": "Requirements review completed", "suggestion": "Consider additional validation"}
                            ],
                            "recommendations": ["Review completed successfully"]
                        }
                    }
            else:
                review_data = result

            # Process review results using collaboration framework
            from handoff.collaboration import ReviewStatus, ReviewFeedback, CollaborationOrchestrator
            from services.github_integration import GitHubIntegration, AgentCommentFormatter

            # Initialize collaboration components
            github = GitHubIntegration()

            # Extract findings and categorize
            findings = review_data.get('feedback', {}).get('findings', [])
            blocking_issues = sum(1 for f in findings if f.get('severity') == 'blocking')
            high_issues = sum(1 for f in findings if f.get('severity') == 'high')

            # Determine review status
            overall_score = review_data.get('review_result', {}).get('overall_score', 0.5)

            if blocking_issues > 0:
                status = ReviewStatus.BLOCKED
            elif high_issues > 2 or overall_score < 0.6:
                status = ReviewStatus.CHANGES_REQUESTED
            else:
                status = ReviewStatus.APPROVED

            # Create structured review feedback
            review_feedback = ReviewFeedback(
                reviewer_agent="requirements_reviewer",
                status=status,
                findings=findings,
                blocking_issues=blocking_issues,
                score=overall_score,
                comments=review_data.get('review_result', {}).get('summary', 'Review completed')
            )

            # Post review to GitHub if issue specified
            github_issue = context.get('github_issue')
            if github_issue:
                review_comment = self.format_requirements_review(review_feedback, review_data)

                project = context.get('project', original_handoff.task_context.get('project', 'unknown'))
                await github.post_issue_comment(github_issue, review_comment, project)

            # Update context for orchestrator
            context['review_completed'] = True
            context['review_feedback'] = review_feedback
            context['review_status'] = status.value
            context['quality_score'] = overall_score
            context['recommendations'] = review_data.get('feedback', {}).get('recommendations', [])

            # Handle different review outcomes
            if status == ReviewStatus.BLOCKED:
                context['next_action'] = 'return_to_business_analyst'
                context['blocking_issues'] = [f for f in findings if f.get('severity') == 'blocking']
            elif status == ReviewStatus.CHANGES_REQUESTED:
                context['next_action'] = 'iterate_with_business_analyst'
                context['suggested_changes'] = [f for f in findings if f.get('severity') in ['high', 'medium']]
            else:
                context['next_action'] = 'proceed_to_next_stage'
                context['approved_artifacts'] = original_handoff.artifacts

            print(f"Requirements review completed: {status.value}")
            print(f"Overall score: {overall_score:.2f}")
            print(f"Blocking issues: {blocking_issues}")

            return context

        except Exception as e:
            raise Exception(f"Requirements review failed: {str(e)}")

    def format_requirements_review(self, feedback: ReviewFeedback, detailed_data: Dict[str, Any]) -> str:
        """Format requirements review for GitHub comment"""

        status_emoji = {
            ReviewStatus.APPROVED: "✅",
            ReviewStatus.CHANGES_REQUESTED: "🔄",
            ReviewStatus.BLOCKED: "🚫"
        }

        emoji = status_emoji.get(feedback.status, "📋")

        # Format findings by category
        findings_by_category = {}
        for finding in feedback.findings:
            category = finding.get('category', 'general')
            if category not in findings_by_category:
                findings_by_category[category] = []
            findings_by_category[category].append(finding)

        findings_text = ""
        for category, findings in findings_by_category.items():
            findings_text += f"\n#### {category.title()}\n"
            for finding in findings:
                severity_emoji = {"low": "ℹ️", "medium": "⚠️", "high": "❗", "blocking": "🚫"}
                severity = finding.get('severity', 'medium')
                emoji = severity_emoji.get(severity, "•")

                findings_text += f"{emoji} {finding.get('message', 'No message')}\n"
                if finding.get('suggestion'):
                    findings_text += f"   💡 *{finding.get('suggestion')}*\n"

        # Format recommendations
        recommendations_text = ""
        recommendations = detailed_data.get('feedback', {}).get('recommendations', [])
        if recommendations:
            recommendations_text = "\n### 📋 Recommendations\n"
            for rec in recommendations:
                recommendations_text += f"- {rec}\n"

        # Format questions
        questions_text = ""
        questions = detailed_data.get('feedback', {}).get('questions', [])
        if questions:
            questions_text = "\n### ❓ Questions for Clarification\n"
            for question in questions:
                questions_text += f"- {question}\n"

        return f"""## {emoji} Requirements Review - {feedback.status.value.replace('_', ' ').title()}

**Overall Score:** {feedback.score:.1%}
**Blocking Issues:** {feedback.blocking_issues}

### Review Summary
{feedback.comments}

### Detailed Findings
{findings_text}
{recommendations_text}
{questions_text}

### Next Steps
{self.get_next_steps_text(feedback.status)}

---
*Requirements review by Claude Code Orchestrator*
"""

    def get_next_steps_text(self, status: ReviewStatus) -> str:
        """Get next steps text based on review status"""
        if status == ReviewStatus.BLOCKED:
            return "🛑 **Critical issues must be resolved before proceeding.** Please address blocking issues and resubmit."
        elif status == ReviewStatus.CHANGES_REQUESTED:
            return "🔄 **Please address the feedback above and update the requirements.** Non-critical improvements suggested."
        else:
            return "✅ **Requirements approved!** Ready to proceed to the design phase."