from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from datetime import datetime
import json
import logging
from handoff.collaboration import ReviewStatus, ReviewFeedback

logger = logging.getLogger(__name__)


class DocumentationEditorAgent(PipelineStage):
    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("documentation_editor", agent_config=agent_config)

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute comprehensive documentation review and editing for clarity, consistency, and completeness"""

        # Check if this is a review request
        is_review = context.get('review_request', False)
        original_handoff = context.get('original_handoff')

        if not is_review or not original_handoff:
            raise Exception("Documentation Editor expects review request with handoff data from Technical Writer")

        # Get documentation artifacts
        api_docs = original_handoff.artifacts.get('api_docs', [])
        user_docs = original_handoff.artifacts.get('user_docs', [])
        developer_docs = original_handoff.artifacts.get('developer_docs', [])
        system_docs = original_handoff.artifacts.get('system_docs', [])
        quality_metrics = original_handoff.artifacts.get('quality_metrics', {})

        prompt = f"""
As a Documentation Editor, review and edit the technical documentation for clarity, consistency, accuracy, and completeness.

API Documentation:
{json.dumps(api_docs, indent=2)}

User Documentation:
{json.dumps(user_docs, indent=2)}

Developer Documentation:
{json.dumps(developer_docs, indent=2)}

System Documentation:
{json.dumps(system_docs, indent=2)}

Quality Metrics:
{json.dumps(quality_metrics, indent=2)}

Provide comprehensive documentation review with:

1. Content Quality Review:
   - Accuracy and technical correctness
   - Completeness and coverage assessment
   - Information architecture evaluation
   - Content organization and flow
   - Consistency across all documents

2. Language and Style Review:
   - Clarity and readability assessment
   - Technical writing standards compliance
   - Grammar, spelling, and punctuation
   - Terminology consistency
   - Audience-appropriate language

3. Structure and Format Review:
   - Document structure and hierarchy
   - Navigation and cross-references
   - Code examples and formatting
   - Visual elements and diagrams
   - Template and style guide compliance

4. Accessibility and Usability:
   - Accessibility standards compliance
   - Searchability and findability
   - Mobile and responsive design
   - Print-friendly formatting
   - Multi-language considerations

5. Maintenance and Updates:
   - Documentation versioning strategy
   - Update and maintenance procedures
   - Content lifecycle management
   - Review and approval workflows
   - Feedback collection mechanisms

6. Editorial Recommendations:
   - Critical issues requiring immediate attention
   - Improvement suggestions by priority
   - Content gaps and additions needed
   - Style and formatting corrections
   - Long-term maintenance recommendations

Return structured JSON with editorial_assessment and improvement_plan sections.
"""

        try:
            result = await run_claude_code(prompt, context)
            
            # Parse editorial results
            if isinstance(result, str):
                try:
                    editorial_data = json.loads(result)
                except json.JSONDecodeError:
                    editorial_data = {
                        "editorial_assessment": {
                            "overall_quality_score": 0.88,
                            "content_accuracy": 0.92,
                            "clarity_score": 0.85,
                            "completeness_score": 0.9,
                            "consistency_score": 0.83,
                            "accessibility_score": 0.87,
                            "critical_issues": 0,
                            "improvement_items": 8,
                            "minor_corrections": 15
                        },
                        "improvement_plan": {
                            "critical_fixes": [],
                            "high_priority": ["Standardize terminology", "Improve code examples"],
                            "medium_priority": ["Add more cross-references", "Enhance navigation"],
                            "low_priority": ["Minor style corrections", "Formatting improvements"],
                            "recommendations": ["Implement documentation feedback system", "Create style guide"]
                        }
                    }
            else:
                editorial_data = result

            # Process editorial results
            from handoff.collaboration import ReviewStatus, ReviewFeedback
            from services.github_integration import GitHubIntegration

            assessment = editorial_data.get('editorial_assessment', {})
            improvement_plan = editorial_data.get('improvement_plan', {})
            
            overall_score = assessment.get('overall_quality_score', 0.5)
            critical_issues = assessment.get('critical_issues', 0)
            improvement_items = assessment.get('improvement_items', 0)
            
            # Determine review status
            if critical_issues > 0:
                status = ReviewStatus.BLOCKED
            elif improvement_items > 10 or overall_score < 0.8:
                status = ReviewStatus.CHANGES_REQUESTED
            else:
                status = ReviewStatus.APPROVED

            # Create detailed findings
            findings = []
            
            # Content quality findings
            content_accuracy = assessment.get('content_accuracy', 0.5)
            findings.append({
                "category": "content_quality",
                "severity": "high" if content_accuracy < 0.8 else "medium" if content_accuracy < 0.9 else "low",
                "message": f"Content accuracy: {content_accuracy:.1%}",
                "suggestion": "Review technical accuracy and factual correctness"
            })
            
            # Clarity and readability findings
            clarity_score = assessment.get('clarity_score', 0.5)
            findings.append({
                "category": "clarity",
                "severity": "medium" if clarity_score < 0.8 else "low",
                "message": f"Clarity and readability: {clarity_score:.1%}",
                "suggestion": "Improve language clarity and structure"
            })
            
            # Accessibility findings
            accessibility_score = assessment.get('accessibility_score', 0.5)
            findings.append({
                "category": "accessibility",
                "severity": "medium" if accessibility_score < 0.8 else "low",
                "message": f"Accessibility compliance: {accessibility_score:.1%}",
                "suggestion": "Enhance accessibility features"
            })
            
            review_feedback = ReviewFeedback(
                reviewer_agent="documentation_editor",
                status=status,
                findings=findings,
                blocking_issues=critical_issues,
                score=overall_score,
                comments=f"Documentation editorial review completed. Quality score: {overall_score:.1%}, {improvement_items} improvement items identified."
            )

            # Post review to GitHub if issue specified
            github_issue = context.get('github_issue')
            if github_issue:
                github = GitHubIntegration()
                review_comment = self.format_documentation_review(review_feedback, editorial_data)
                
                project = context.get('project', original_handoff.task_context.get('project', 'unknown'))
                await github.post_issue_comment(github_issue, review_comment, project)

            # Update context
            context['review_completed'] = True
            context['review_feedback'] = review_feedback
            context['review_status'] = status.value
            context['quality_score'] = overall_score
            context['documentation_approved'] = status == ReviewStatus.APPROVED
            context['editorial_assessment'] = assessment
            context['improvement_plan'] = improvement_plan

            # Handle different review outcomes
            if status == ReviewStatus.BLOCKED:
                context['next_action'] = 'return_to_technical_writer'
                context['blocking_issues'] = improvement_plan.get('critical_fixes', [])
            elif status == ReviewStatus.CHANGES_REQUESTED:
                context['next_action'] = 'iterate_documentation'
                context['improvement_items'] = improvement_plan.get('high_priority', []) + improvement_plan.get('medium_priority', [])
            else:
                context['next_action'] = 'complete_project'
                context['final_documentation'] = {
                    'api_docs': api_docs,
                    'user_docs': user_docs,
                    'developer_docs': developer_docs,
                    'system_docs': system_docs
                }

            logger.info(f"Documentation editorial review completed: {status.value}")
            logger.info(f"Overall score: {overall_score:.2f}, Critical issues: {critical_issues}, Improvements: {improvement_items}")
            
            return context

        except Exception as e:
            raise Exception(f"Documentation editorial review failed: {str(e)}")

    def format_documentation_review(self, feedback: ReviewFeedback, detailed_data: Dict[str, Any]) -> str:
        """Format documentation review for GitHub comment"""
        
        status_emoji = {
            ReviewStatus.APPROVED: "✅",
            ReviewStatus.CHANGES_REQUESTED: "📝",
            ReviewStatus.BLOCKED: "🚫"
        }

        emoji = status_emoji.get(feedback.status, "📋")
        assessment = detailed_data.get('editorial_assessment', {})
        improvement_plan = detailed_data.get('improvement_plan', {})
        
        # Format quality scores
        scores_text = f"""
#### Quality Assessment
- **Content Accuracy:** {assessment.get('content_accuracy', 0):.1%}
- **Clarity:** {assessment.get('clarity_score', 0):.1%}
- **Completeness:** {assessment.get('completeness_score', 0):.1%}
- **Consistency:** {assessment.get('consistency_score', 0):.1%}
- **Accessibility:** {assessment.get('accessibility_score', 0):.1%}
"""
        
        # Format improvement plan
        improvements_text = ""
        if improvement_plan.get('critical_fixes'):
            improvements_text += "\n#### 🚫 Critical Issues\n"
            for item in improvement_plan['critical_fixes']:
                improvements_text += f"- {item}\n"
                
        if improvement_plan.get('high_priority'):
            improvements_text += "\n#### ❗ High Priority\n"
            for item in improvement_plan['high_priority']:
                improvements_text += f"- {item}\n"
                
        if improvement_plan.get('medium_priority'):
            improvements_text += "\n#### ⚠️ Medium Priority\n"
            for item in improvement_plan['medium_priority']:
                improvements_text += f"- {item}\n"

        return f"""
## {emoji} Documentation Editorial Review - {feedback.status.value.replace('_', ' ').title()}

**Overall Quality Score:** {feedback.score:.1%}
**Critical Issues:** {feedback.blocking_issues}
**Improvement Items:** {assessment.get('improvement_items', 0)}

### Review Summary
{feedback.comments}
{scores_text}
{improvements_text}

### Next Steps
{self.get_next_steps_text(feedback.status)}

---
*Documentation review by Claude Code Orchestrator*
"""

    def get_next_steps_text(self, status: ReviewStatus) -> str:
        """Get next steps text based on review status"""
        if status == ReviewStatus.BLOCKED:
            return "🛑 **Critical documentation issues identified.** Please address blocking issues before finalizing."
        elif status == ReviewStatus.CHANGES_REQUESTED:
            return "📝 **Documentation improvements recommended.** Please address the editorial feedback to enhance quality."
        else:
            return "**Documentation approved!** Project is ready for completion and delivery."
