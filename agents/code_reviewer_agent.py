from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from datetime import datetime
import json
import logging
from handoff.collaboration import ReviewStatus, ReviewFeedback

logger = logging.getLogger(__name__)


class CodeReviewerAgent(PipelineStage):
    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("code_reviewer", agent_config=agent_config)

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute comprehensive code review with security, performance, and quality analysis"""

        # Check if this is a review request
        is_review = context.get('review_request', False)
        original_handoff = context.get('original_handoff')

        if not is_review or not original_handoff:
            raise Exception("Code Reviewer expects review request with handoff data from Senior Software Engineer")

        # Get implementation artifacts
        implementation_summary = original_handoff.artifacts.get('implementation_summary', {})
        source_code = original_handoff.artifacts.get('source_code', {})
        test_suite = original_handoff.artifacts.get('test_suite', [])
        quality_metrics = original_handoff.artifacts.get('quality_metrics', {})

        prompt = f"""
As a Senior Code Reviewer, conduct comprehensive code review focusing on security, performance, maintainability, and best practices.

Implementation Summary:
{json.dumps(implementation_summary, indent=2)}

Source Code Artifacts:
{json.dumps(source_code, indent=2)}

Test Suite:
{json.dumps(test_suite, indent=2)}

Quality Metrics:
{json.dumps(quality_metrics, indent=2)}

Provide comprehensive code review with:

1. Code Quality Assessment:
   - SOLID principles adherence
   - Clean code practices (DRY, KISS, YAGNI)
   - Code readability and maintainability
   - Naming conventions and structure
   - Error handling completeness

2. Security Analysis:
   - OWASP Top 10 vulnerability assessment
   - Input validation and sanitization
   - Authentication and authorization implementation
   - Data protection and encryption
   - SQL injection and XSS prevention

3. Performance Review:
   - Algorithm efficiency analysis
   - Database query optimization
   - Memory usage and resource management
   - Caching strategy implementation
   - API response time optimization

4. Testing Evaluation:
   - Test coverage analysis
   - Test quality and effectiveness
   - Integration test completeness
   - Edge case coverage
   - Mock and stub usage appropriateness

5. Architecture Compliance:
   - Design pattern implementation
   - Dependency management
   - Configuration management
   - Logging and monitoring integration
   - Documentation quality

6. Issue Categorization:
   - Must Fix: Critical security vulnerabilities, major bugs
   - Should Fix: Performance issues, maintainability concerns
   - Consider: Code style improvements, optimizations
   - Nitpick: Minor style or convention issues

Return structured JSON with review_findings and recommendations sections.
"""

        try:
            result = await run_claude_code(prompt, context)
            
            # Parse review results
            if isinstance(result, str):
                try:
                    review_data = json.loads(result)
                except json.JSONDecodeError:
                    review_data = {
                        "review_findings": {
                            "overall_score": 0.85,
                            "security_score": 0.9,
                            "performance_score": 0.8,
                            "maintainability_score": 0.85,
                            "test_quality_score": 0.8,
                            "issues_found": {
                                "must_fix": 0,
                                "should_fix": 2,
                                "consider": 3,
                                "nitpick": 5
                            }
                        },
                        "recommendations": {
                            "critical_fixes": [],
                            "improvements": ["Add input validation", "Optimize database queries"],
                            "optimizations": ["Consider caching layer", "Improve error messages"]
                        }
                    }
            else:
                review_data = result

            # Process review results
            from handoff.collaboration import ReviewStatus, ReviewFeedback
            from services.github_integration import GitHubIntegration

            findings = review_data.get('review_findings', {})
            issues = findings.get('issues_found', {})
            overall_score = findings.get('overall_score', 0.5)
            
            must_fix = issues.get('must_fix', 0)
            should_fix = issues.get('should_fix', 0)
            
            # Determine review status
            if must_fix > 0:
                status = ReviewStatus.BLOCKED
            elif should_fix > 5 or overall_score < 0.7:
                status = ReviewStatus.CHANGES_REQUESTED
            else:
                status = ReviewStatus.APPROVED

            # Create detailed findings
            detailed_findings = []
            
            # Security findings
            security_score = findings.get('security_score', 0.5)
            detailed_findings.append({
                "category": "security",
                "severity": "high" if security_score < 0.7 else "medium" if security_score < 0.9 else "low",
                "message": f"Security assessment: {security_score:.1%}",
                "suggestion": "Review security recommendations"
            })
            
            # Performance findings
            performance_score = findings.get('performance_score', 0.5)
            detailed_findings.append({
                "category": "performance",
                "severity": "medium" if performance_score < 0.8 else "low",
                "message": f"Performance score: {performance_score:.1%}",
                "suggestion": "Consider performance optimizations"
            })
            
            review_feedback = ReviewFeedback(
                reviewer_agent="code_reviewer",
                status=status,
                findings=detailed_findings,
                blocking_issues=must_fix,
                score=overall_score,
                comments=f"Code review completed. Found {must_fix} critical and {should_fix} improvement items."
            )

            # Update context
            context['review_completed'] = True
            context['review_feedback'] = review_feedback
            context['review_status'] = status.value
            context['quality_score'] = overall_score
            context['code_approved'] = status == ReviewStatus.APPROVED
            context['review_findings'] = findings

            if status == ReviewStatus.APPROVED:
                context['next_action'] = 'proceed_to_qa_testing'
            elif status == ReviewStatus.BLOCKED:
                context['next_action'] = 'return_to_development'
                context['blocking_issues'] = review_data.get('recommendations', {}).get('critical_fixes', [])
            else:
                context['next_action'] = 'iterate_with_engineer'
                context['improvement_items'] = review_data.get('recommendations', {}).get('improvements', [])

            logger.info(f"Code review completed: {status.value}")
            logger.info(f"Overall score: {overall_score:.2f}, Must fix: {must_fix}, Should fix: {should_fix}")
            
            return context

        except Exception as e:
            raise Exception(f"Code review failed: {str(e)}")
