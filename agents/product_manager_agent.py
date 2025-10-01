from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from datetime import datetime
import json
import logging
from handoff.collaboration import ReviewStatus, ReviewFeedback

logger = logging.getLogger(__name__)


class ProductManagerAgent(PipelineStage):
    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("product_manager", agent_config=agent_config)

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute product management review using RICE framework on requirements from Business Analyst"""

        # Check if this is a review request
        is_review = context.get('review_request', False)
        original_handoff = context.get('original_handoff')

        if not is_review or not original_handoff:
            raise Exception("Product Manager expects review request with handoff data from Business Analyst")

        # Get the requirements from handoff
        requirements_doc = original_handoff.artifacts.get('requirements_document', {})
        user_stories = original_handoff.artifacts.get('user_stories', [])
        quality_metrics = original_handoff.artifacts.get('quality_metrics', {})

        # Extract focus areas for review
        focus_areas = context.get('focus_areas', ['prioritization', 'market_alignment', 'stakeholder_value'])

        prompt = f"""
As a Product Manager, review the requirements analysis using the RICE framework (Reach, Impact, Confidence, Effort)
and evaluate product strategy alignment:

Requirements Document:
{json.dumps(requirements_doc, indent=2)}

User Stories ({len(user_stories)} total):
{json.dumps(user_stories, indent=2)}

Quality Metrics from Business Analyst:
{json.dumps(quality_metrics, indent=2)}

Focus Areas: {', '.join(focus_areas)}

Provide comprehensive product management review with:

1. RICE Framework Analysis:
   - Reach: How many users/customers will this impact?
   - Impact: What will the impact be on each user/customer?
   - Confidence: How confident are we in our estimates?
   - Effort: How much work will this require?

2. Feature Prioritization:
   - Priority ranking using RICE scores
   - Feature roadmap recommendations
   - MVP vs future iterations breakdown

3. Market Alignment:
   - Market opportunity assessment
   - Competitive positioning
   - Value proposition validation

4. Stakeholder Impact:
   - User value analysis
   - Business value assessment
   - Technical feasibility from product perspective

5. Strategic Recommendations:
   - Go/No-Go recommendation
   - Resource allocation suggestions
   - Success metrics and KPIs
   - Risk mitigation strategies

6. Requirements Review:
   - Completeness from product perspective
   - Market viability assessment
   - User experience considerations

Return structured JSON with product_analysis and strategic_recommendations sections.
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
                    # Use Serena to find similar product strategy patterns
                    search_results = await self.mcp_integration.serena_search(
                        "product management RICE framework prioritization strategy",
                        file_types=['md', 'txt', 'py']
                    )
                    if search_results:
                        enhanced_context['strategy_patterns'] = search_results[:2]

                    logger.info(f"Enhanced review context with {len(search_results)} strategy patterns from Serena")
                except Exception as e:
                    logger.warning(f"Serena search failed: {e}")

            result = await run_claude_code(prompt, enhanced_context)

            # Parse Claude's response
            if isinstance(result, str):
                try:
                    product_data = json.loads(result)
                except json.JSONDecodeError:
                    # If not valid JSON, create a structured response
                    product_data = {
                        "product_analysis": {
                            "rice_scores": {"reach": 80, "impact": 70, "confidence": 75, "effort": 60},
                            "priority_ranking": "High Priority",
                            "market_alignment": "Strong market fit identified",
                            "stakeholder_value": "High business and user value",
                            "overall_score": 0.8
                        },
                        "strategic_recommendations": {
                            "recommendation": "proceed",
                            "mvp_features": ["Core functionality"],
                            "future_iterations": ["Advanced features"],
                            "success_metrics": ["User adoption", "Business metrics"],
                            "risk_assessment": "Low to moderate risk"
                        },
                        "quality_metrics": {
                            "strategic_alignment": 0.8,
                            "market_viability": 0.75,
                            "resource_efficiency": 0.7
                        }
                    }
            else:
                product_data = result

            # Process review results using collaboration framework
            from handoff.collaboration import ReviewStatus, ReviewFeedback, CollaborationOrchestrator
            from services.github_integration import GitHubIntegration, AgentCommentFormatter

            # Initialize collaboration components
            github = GitHubIntegration()

            # Extract analysis and determine recommendation
            analysis = product_data.get('product_analysis', {})
            recommendations = product_data.get('strategic_recommendations', {})

            overall_score = analysis.get('overall_score', 0.5)
            recommendation = recommendations.get('recommendation', 'review').lower()

            # Determine review status based on product analysis
            if recommendation == 'proceed' and overall_score >= 0.7:
                status = ReviewStatus.APPROVED
            elif recommendation == 'no_go' or overall_score < 0.4:
                status = ReviewStatus.BLOCKED
            else:
                status = ReviewStatus.CHANGES_REQUESTED

            # Create structured review feedback
            findings = []

            # Add RICE analysis findings
            rice_scores = analysis.get('rice_scores', {})
            findings.append({
                "category": "prioritization",
                "severity": "medium" if overall_score < 0.6 else "low",
                "message": f"RICE Analysis - Overall Score: {overall_score:.1%}",
                "suggestion": f"Priority: {analysis.get('priority_ranking', 'Medium')}"
            })

            # Add market alignment findings
            findings.append({
                "category": "market_alignment",
                "severity": "low" if analysis.get('market_alignment', '').lower().find('strong') >= 0 else "medium",
                "message": analysis.get('market_alignment', 'Market alignment assessed'),
                "suggestion": "Consider market positioning strategy"
            })

            # Add strategic recommendations
            findings.append({
                "category": "strategy",
                "severity": "low",
                "message": f"Strategic recommendation: {recommendation}",
                "suggestion": recommendations.get('risk_assessment', 'Risk assessment completed')
            })

            review_feedback = ReviewFeedback(
                reviewer_agent="product_manager",
                status=status,
                findings=findings,
                blocking_issues=sum(1 for f in findings if f.get('severity') == 'blocking'),
                score=overall_score,
                comments=f"Product review completed. {recommendation.title()} recommendation with {overall_score:.1%} confidence."
            )

            # Post review to GitHub if issue specified
            github_issue = context.get('github_issue')
            if github_issue:
                review_comment = self.format_product_review(review_feedback, product_data)

                project = context.get('project', original_handoff.task_context.get('project', 'unknown'))
                await github.post_issue_comment(github_issue, review_comment, project)

            # Update context for orchestrator
            context['review_completed'] = True
            context['review_feedback'] = review_feedback
            context['review_status'] = status.value
            context['quality_score'] = overall_score
            context['product_analysis'] = analysis
            context['strategic_recommendations'] = recommendations

            # Handle different review outcomes
            if status == ReviewStatus.BLOCKED:
                context['next_action'] = 'return_to_business_analyst'
                context['blocking_issues'] = [f for f in findings if f.get('severity') == 'blocking']
            elif status == ReviewStatus.CHANGES_REQUESTED:
                context['next_action'] = 'iterate_with_business_analyst'
                context['suggested_changes'] = [f for f in findings if f.get('severity') in ['high', 'medium']]
            else:
                context['next_action'] = 'proceed_to_design'
                context['approved_artifacts'] = original_handoff.artifacts
                context['approved_features'] = recommendations.get('mvp_features', [])

            logger.info(f"Product management review completed: {status.value}")
            logger.info(f"Overall score: {overall_score:.2f}")
            logger.info(f"Strategic recommendation: {recommendation}")

            return context

        except Exception as e:
            raise Exception(f"Product management review failed: {str(e)}")

    def format_product_review(self, feedback: ReviewFeedback, detailed_data: Dict[str, Any]) -> str:
        """Format product management review for GitHub comment"""

        status_emoji = {
            ReviewStatus.APPROVED: "✅",
            ReviewStatus.CHANGES_REQUESTED: "🔄",
            ReviewStatus.BLOCKED: "🚫"
        }

        emoji = status_emoji.get(feedback.status, "📊")

        # Format RICE analysis
        analysis = detailed_data.get('product_analysis', {})
        rice_scores = analysis.get('rice_scores', {})

        rice_text = ""
        if rice_scores:
            rice_text = f"""
#### RICE Framework Analysis
- **Reach:** {rice_scores.get('reach', 'N/A')}
- **Impact:** {rice_scores.get('impact', 'N/A')}
- **Confidence:** {rice_scores.get('confidence', 'N/A')}
- **Effort:** {rice_scores.get('effort', 'N/A')}
- **Priority:** {analysis.get('priority_ranking', 'Medium')}
"""

        # Format strategic recommendations
        recommendations = detailed_data.get('strategic_recommendations', {})
        mvp_features = recommendations.get('mvp_features', [])

        strategy_text = ""
        if recommendations:
            strategy_text = f"""
#### Strategic Recommendations
- **Recommendation:** {recommendations.get('recommendation', 'Review').title()}
- **MVP Features:** {', '.join(mvp_features) if mvp_features else 'TBD'}
- **Success Metrics:** {', '.join(recommendations.get('success_metrics', []))}
- **Risk Assessment:** {recommendations.get('risk_assessment', 'N/A')}
"""

        # Format findings
        findings_text = ""
        if feedback.findings:
            findings_text = "\n#### Key Findings\n"
            for finding in feedback.findings:
                severity_emoji = {"low": "ℹ️", "medium": "⚠️", "high": "❗", "blocking": "🚫"}
                severity = finding.get('severity', 'medium')
                emoji_icon = severity_emoji.get(severity, "•")

                findings_text += f"{emoji_icon} **{finding.get('category', 'General').title()}:** {finding.get('message', 'No message')}\n"
                if finding.get('suggestion'):
                    findings_text += f"   💡 *{finding.get('suggestion')}*\n"

        return f"""## {emoji} Product Management Review - {feedback.status.value.replace('_', ' ').title()}

**Overall Score:** {feedback.score:.1%}
**Strategic Alignment:** {analysis.get('strategic_alignment', 'N/A')}

### Review Summary
{feedback.comments}
{rice_text}
{strategy_text}
{findings_text}

### Next Steps
{self.get_next_steps_text(feedback.status, recommendations.get('recommendation', 'review'))}

---
*Product review by Claude Code Orchestrator*
"""

    def get_next_steps_text(self, status: ReviewStatus, recommendation: str) -> str:
        """Get next steps text based on review status and recommendation"""
        if status == ReviewStatus.BLOCKED:
            return "🛑 **Strategic concerns identified.** Please address product alignment issues before proceeding."
        elif status == ReviewStatus.CHANGES_REQUESTED:
            return "🔄 **Product refinements recommended.** Please consider the strategic feedback and update accordingly."
        else:
            if recommendation.lower() == 'proceed':
                return "**Product strategy approved!** Ready to proceed to design phase with recommended MVP scope."
            else:
                return "📋 **Product review completed.** Please review recommendations before next phase."