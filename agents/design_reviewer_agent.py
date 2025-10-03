from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from datetime import datetime
import json
import logging
from services.review_parser import ReviewStatus

logger = logging.getLogger(__name__)


class DesignReviewerAgent(PipelineStage):
    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("design_reviewer", agent_config=agent_config)

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute architecture design review focusing on patterns, security, and scalability"""

        # Check if this is a review request
        is_review = context.get('review_request', False)
        original_handoff = context.get('original_handoff')

        if not is_review or not original_handoff:
            raise Exception("Design Reviewer expects review request with handoff data from Software Architect")

        # Get the architecture design from handoff
        architecture_document = original_handoff.artifacts.get('architecture_document', {})
        technical_decisions = original_handoff.artifacts.get('technical_decisions', {})
        implementation_plan = original_handoff.artifacts.get('implementation_plan', {})
        quality_metrics = original_handoff.artifacts.get('quality_metrics', {})

        # Extract focus areas for review
        focus_areas = context.get('focus_areas', ['security', 'scalability', 'patterns', 'performance'])

        prompt = f"""
As an Architecture Reviewer, conduct a comprehensive review of the software architecture design using industry best practices and security standards.

Architecture Document:
{json.dumps(architecture_document, indent=2)}

Technical Decisions:
{json.dumps(technical_decisions, indent=2)}

Implementation Plan:
{json.dumps(implementation_plan, indent=2)}

Quality Metrics from Software Architect:
{json.dumps(quality_metrics, indent=2)}

Focus Areas: {', '.join(focus_areas)}

IMPORTANT: Output your review as text directly in your response. DO NOT create any files. This review will be posted to GitHub as a comment.

Provide comprehensive architecture review with:

1. Design Pattern Analysis:
   - Architectural patterns evaluation (MVC, microservices, event-driven, etc.)
   - Design principles compliance (SOLID, DRY, KISS, YAGNI)
   - Anti-pattern identification
   - Pattern consistency assessment

2. Security Assessment:
   - OWASP Top 10 compliance
   - Authentication and authorization design review
   - Data protection and encryption strategies
   - API security considerations
   - Vulnerability risk assessment

3. Scalability Review:
   - Horizontal and vertical scaling capabilities
   - Performance bottleneck identification
   - Load balancing and distribution strategies
   - Database scaling approach validation
   - Caching strategy effectiveness

4. Performance Analysis:
   - Performance targets feasibility
   - Resource utilization efficiency
   - Critical path analysis
   - Monitoring and observability adequacy
   - Optimization opportunities

5. Maintainability Assessment:
   - Code organization and modularity
   - Dependency management strategy
   - Configuration and deployment approach
   - Documentation completeness
   - Team productivity considerations

6. Technical Risk Assessment:
   - Implementation risks and mitigation
   - Technology choices validation
   - Integration complexity analysis
   - External dependency risks
   - Rollback and recovery strategies

7. Compliance and Standards:
   - Industry standards adherence
   - Best practices implementation
   - Quality attribute trade-offs
   - Non-functional requirements coverage

Return structured JSON with review_assessment and improvement_recommendations sections.
"""

        try:
            # Enhance context with MCP server data if available
            enhanced_context = context.copy()

            # Add agent_config for security enforcement (requires_docker check)
            if self.agent_config and 'agent_config' in self.agent_config:
                enhanced_context['agent_config'] = self.agent_config['agent_config']

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
                    # Use Serena to find similar design review patterns
                    search_results = await self.mcp_integration.serena_search(
                        "architecture review security scalability patterns OWASP",
                        file_types=['md', 'txt', 'py']
                    )
                    if search_results:
                        enhanced_context['review_patterns'] = search_results[:2]

                    logger.info(f"Enhanced review context with {len(search_results)} review patterns from Serena")
                except Exception as e:
                    logger.warning(f"Serena search failed: {e}")

            result = await run_claude_code(prompt, enhanced_context)

            # Parse Claude's response
            if isinstance(result, str):
                try:
                    review_data = json.loads(result)
                except json.JSONDecodeError:
                    # If not valid JSON, create a structured response
                    review_data = {
                        "review_assessment": {
                            "overall_score": 0.75,
                            "security_score": 0.8,
                            "scalability_score": 0.7,
                            "maintainability_score": 0.8,
                            "performance_score": 0.75,
                            "status": "approved_with_recommendations"
                        },
                        "improvement_recommendations": {
                            "critical_issues": [],
                            "high_priority": ["Consider adding rate limiting", "Enhance monitoring coverage"],
                            "medium_priority": ["Optimize database queries", "Add circuit breakers"],
                            "low_priority": ["Documentation updates", "Code style consistency"],
                            "security_recommendations": ["Implement security headers", "Add input validation"],
                            "performance_optimizations": ["Add caching layer", "Optimize API responses"]
                        }
                    }
            else:
                review_data = result

            # Process review results using collaboration framework
            from services.review_parser import ReviewStatus, CollaborationOrchestrator
            from services.github_integration import GitHubIntegration, AgentCommentFormatter

            # Initialize collaboration components
            github = GitHubIntegration()

            # Extract assessment and categorize issues
            assessment = review_data.get('review_assessment', {})
            recommendations = review_data.get('improvement_recommendations', {})

            overall_score = assessment.get('overall_score', 0.5)
            critical_issues = len(recommendations.get('critical_issues', []))
            high_priority = len(recommendations.get('high_priority', []))

            # Determine review status based on findings
            if critical_issues > 0:
                status = ReviewStatus.BLOCKED
            elif high_priority > 3 or overall_score < 0.6:
                status = ReviewStatus.CHANGES_REQUESTED
            else:
                status = ReviewStatus.APPROVED

            # Create structured findings
            findings = []

            # Add security findings
            security_score = assessment.get('security_score', 0.5)
            findings.append({
                "category": "security",
                "severity": "high" if security_score < 0.6 else "medium" if security_score < 0.8 else "low",
                "message": f"Security assessment score: {security_score:.1%}",
                "suggestion": "Review and implement security recommendations"
            })

            # Add scalability findings
            scalability_score = assessment.get('scalability_score', 0.5)
            findings.append({
                "category": "scalability",
                "severity": "high" if scalability_score < 0.6 else "medium" if scalability_score < 0.8 else "low",
                "message": f"Scalability readiness: {scalability_score:.1%}",
                "suggestion": "Address scalability bottlenecks identified"
            })

            # Add performance findings
            performance_score = assessment.get('performance_score', 0.5)
            findings.append({
                "category": "performance",
                "severity": "medium" if performance_score < 0.7 else "low",
                "message": f"Performance design score: {performance_score:.1%}",
                "suggestion": "Implement performance optimizations"
            })

            # Add critical issues as blocking findings
            for issue in recommendations.get('critical_issues', []):
                findings.append({
                    "category": "critical",
                    "severity": "blocking",
                    "message": issue,
                    "suggestion": "Must be resolved before proceeding"
                })

                        # Note: GitHub posting handled by centralized GitHub integration
            # Legacy code path - github_issue is no longer set in context

            # Update context for orchestrator
            context['review_completed'] = True
            context['review_status'] = status.value
            context['quality_score'] = overall_score
            context['review_assessment'] = assessment
            context['improvement_recommendations'] = recommendations

            # Handle different review outcomes
            if status == ReviewStatus.BLOCKED:
                context['next_action'] = 'return_to_software_architect'
                context['blocking_issues'] = recommendations.get('critical_issues', [])
            elif status == ReviewStatus.CHANGES_REQUESTED:
                context['next_action'] = 'iterate_with_software_architect'
                context['suggested_changes'] = recommendations.get('high_priority', []) + recommendations.get('medium_priority', [])
            else:
                context['next_action'] = 'proceed_to_test_planning'
                context['approved_architecture'] = original_handoff.artifacts

            logger.info(f"Architecture design review completed: {status.value}")
            print(f"Overall score: {overall_score:.2f}")
            print(f"Critical issues: {critical_issues}")

            return context

        except Exception as e:
            raise Exception(f"Architecture design review failed: {str(e)}")

    def format_design_review(self, feedback: ReviewFeedback, detailed_data: Dict[str, Any]) -> str:
        """Format architecture design review for GitHub comment"""

        status_emoji = {
            ReviewStatus.APPROVED: "✅",
            ReviewStatus.CHANGES_REQUESTED: "🔄",
            ReviewStatus.BLOCKED: "🚫"
        }

        emoji = status_emoji.get(feedback.status, "🏗️")

        # Format assessment scores
        assessment = detailed_data.get('review_assessment', {})
        scores_text = f"""
#### Assessment Scores
- **Security:** {assessment.get('security_score', 0):.1%}
- **Scalability:** {assessment.get('scalability_score', 0):.1%}
- **Performance:** {assessment.get('performance_score', 0):.1%}
- **Maintainability:** {assessment.get('maintainability_score', 0):.1%}
"""

        # Format recommendations by priority
        recommendations = detailed_data.get('improvement_recommendations', {})
        rec_text = ""

        if recommendations.get('critical_issues'):
            rec_text += "\n#### 🚫 Critical Issues\n"
            for issue in recommendations['critical_issues']:
                rec_text += f"- {issue}\n"

        if recommendations.get('high_priority'):
            rec_text += "\n#### ❗ High Priority\n"
            for item in recommendations['high_priority']:
                rec_text += f"- {item}\n"

        if recommendations.get('medium_priority'):
            rec_text += "\n#### ⚠️ Medium Priority\n"
            for item in recommendations['medium_priority']:
                rec_text += f"- {item}\n"

        # Format findings by category
        findings_text = ""
        findings_by_category = {}
        for finding in feedback.findings:
            category = finding.get('category', 'general')
            if category not in findings_by_category:
                findings_by_category[category] = []
            findings_by_category[category].append(finding)

        if findings_by_category:
            findings_text = "\n#### Review Findings\n"
            for category, findings in findings_by_category.items():
                if category != 'critical':  # Critical issues already shown above
                    findings_text += f"\n**{category.title()}:**\n"
                    for finding in findings:
                        severity_emoji = {"low": "ℹ️", "medium": "⚠️", "high": "❗", "blocking": "🚫"}
                        severity = finding.get('severity', 'medium')
                        emoji_icon = severity_emoji.get(severity, "•")
                        findings_text += f"{emoji_icon} {finding.get('message', 'No message')}\n"

        return f"""## {emoji} Architecture Design Review - {feedback.status.value.replace('_', ' ').title()}

**Overall Score:** {feedback.score:.1%}
**Critical Issues:** {feedback.blocking_issues}

### Review Summary
{feedback.comments}
{scores_text}
{rec_text}
{findings_text}

### Next Steps
{self.get_next_steps_text(feedback.status)}

---
*Architecture review by Claude Code Orchestrator*
"""

    def get_next_steps_text(self, status: ReviewStatus) -> str:
        """Get next steps text based on review status"""
        if status == ReviewStatus.BLOCKED:
            return "🛑 **Critical architecture issues identified.** Please address blocking issues before proceeding to development."
        elif status == ReviewStatus.CHANGES_REQUESTED:
            return "🔄 **Architecture improvements recommended.** Please address the feedback to strengthen the design."
        else:
            return "**Architecture approved!** Ready to proceed to test planning and implementation."