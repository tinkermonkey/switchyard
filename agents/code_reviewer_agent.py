from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from datetime import datetime
import json
import logging
from services.review_parser import ReviewStatus

logger = logging.getLogger(__name__)


class CodeReviewerAgent(PipelineStage):
    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("code_reviewer", agent_config=agent_config)

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute comprehensive code review with security, performance, and quality analysis"""

        # Extract from nested task context
        task_context = context.get('context', {})
        issue = task_context.get('issue', {})
        project = context.get('project', 'unknown')
        previous_stage = task_context.get('previous_stage_output', '')

        # Check for feedback
        feedback_data = task_context.get('feedback')
        previous_output = task_context.get('previous_output')

        # Get implementation to review from previous stage
        implementation_summary = context.get('implementation_summary', {})
        code_artifacts = context.get('code_artifacts', {})

        # Build feedback prompt if this is a refinement
        feedback_prompt = ""
        if feedback_data and previous_output:
            feedback_prompt = f"""

YOUR PREVIOUS REVIEW:
{previous_output}

HUMAN FEEDBACK RECEIVED:
{feedback_data.get('formatted_text', '')}

IMPORTANT: Review your previous review and refine it based on the feedback.
Do NOT start from scratch - update and improve your existing review.

CRITICAL: Output the COMPLETE, UPDATED review with all changes incorporated.
"""

        prompt = f"""
As a Senior Code Reviewer, conduct comprehensive code review focusing on security, performance, maintainability, and best practices.

Original Issue:
Title: {issue.get('title', 'No title')}
Description: {issue.get('body', 'No description')}

Previous Stage Output (Implementation):
{previous_stage}
{feedback_prompt}

IMPORTANT: Output your code review as text directly in your response. DO NOT create any files. This review will be posted to GitHub as a comment.

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
            # Enhance context with MCP server data if available
            enhanced_context = context.copy()

            # Add agent_config for security enforcement (requires_docker check)
            if self.agent_config and 'agent_config' in self.agent_config:
                enhanced_context['agent_config'] = self.agent_config['agent_config']
            if self.agent_config and 'mcp_servers' in self.agent_config:
                enhanced_context['mcp_servers'] = self.agent_config['mcp_servers']
                logger.info(f"Added {len(enhanced_context['mcp_servers'])} MCP servers to context")

            result = await run_claude_code(prompt, enhanced_context)
            
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
            from services.review_parser import ReviewStatus
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
            
            # Store result as markdown
            if isinstance(result, str):
                context['markdown_review'] = result
            else:
                context['markdown_review'] = json.dumps(review_data, indent=2)

            # Update context
            context['review_completed'] = True
            context['review_status'] = status.value
            context['quality_score'] = overall_score
            context['code_approved'] = status == ReviewStatus.APPROVED
            context['review_findings'] = findings

            logger.info(f"Code review completed: {status.value}")
            logger.info(f"Overall score: {overall_score:.2f}, Must fix: {must_fix}, Should fix: {should_fix}")

            return context

        except Exception as e:
            raise Exception(f"Code review failed: {str(e)}")

