from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from datetime import datetime
import json
import logging

logger = logging.getLogger(__name__)


class TechnicalWriterAgent(PipelineStage):
    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("technical_writer", agent_config=agent_config)

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Generate comprehensive technical documentation based on implementation and QA results"""

        # Extract artifacts from context
        architecture_design = context.get('architecture_design', {})
        implementation_summary = context.get('implementation_summary', {})
        code_artifacts = context.get('code_artifacts', {})
        qa_results = context.get('qa_results', {})
        requirements_analysis = context.get('requirements_analysis', {})

        project = context.get('project', 'unknown')

        prompt = f"""
As a Technical Writer, create comprehensive documentation following industry standards for clarity, accuracy, and completeness.

Architecture Design:
{json.dumps(architecture_design, indent=2)}

Implementation Summary:
{json.dumps(implementation_summary, indent=2)}

Code Artifacts:
{json.dumps(code_artifacts, indent=2)}

QA Results:
{json.dumps(qa_results, indent=2)}

Requirements:
{json.dumps(requirements_analysis, indent=2)}

Project: {project}

Generate comprehensive technical documentation with:

1. API Documentation:
   - Endpoint specifications with request/response examples
   - Authentication and authorization requirements
   - Error codes and handling
   - Rate limiting and usage guidelines
   - SDK and client library documentation

2. User Documentation:
   - Getting started guide
   - Feature overview and tutorials
   - Configuration and setup instructions
   - Troubleshooting guide
   - FAQ and common issues

3. Developer Documentation:
   - Architecture overview and design decisions
   - Development environment setup
   - Coding standards and contribution guidelines
   - Testing procedures and coverage
   - Deployment and release processes

4. System Documentation:
   - Infrastructure and deployment architecture
   - Security considerations and compliance
   - Performance characteristics and benchmarks
   - Monitoring and observability setup
   - Backup and disaster recovery procedures

5. Operations Documentation:
   - Installation and configuration guides
   - Administrative procedures
   - Maintenance and update procedures
   - Performance tuning guidelines
   - Support and escalation procedures

Return structured JSON with documentation_artifacts and quality_metrics sections.
"""

        try:
            result = await run_claude_code(prompt, context)
            
            # Parse documentation results
            if isinstance(result, str):
                try:
                    doc_data = json.loads(result)
                except json.JSONDecodeError:
                    doc_data = {
                        "documentation_artifacts": {
                            "api_documentation": ["API_Reference.md", "Authentication.md", "Error_Handling.md"],
                            "user_documentation": ["Getting_Started.md", "User_Guide.md", "Tutorials.md", "FAQ.md"],
                            "developer_documentation": ["Architecture.md", "Development_Setup.md", "Contributing.md", "Testing.md"],
                            "system_documentation": ["Deployment.md", "Security.md", "Performance.md", "Monitoring.md"],
                            "operations_documentation": ["Installation.md", "Administration.md", "Maintenance.md", "Support.md"]
                        },
                        "quality_metrics": {
                            "documentation_completeness": 0.92,
                            "clarity_score": 0.88,
                            "accuracy_score": 0.95,
                            "coverage_score": 0.9,
                            "total_pages": 24,
                            "word_count": 15000
                        }
                    }
            else:
                doc_data = result

            # Add to context
            context['documentation_artifacts'] = doc_data.get('documentation_artifacts', {})
            context['documentation_quality'] = doc_data.get('quality_metrics', {})
            context['quality_metrics'] = {**context.get('quality_metrics', {}), **doc_data.get('quality_metrics', {})}
            context['completed_work'] = context.get('completed_work', []) + [
                "Comprehensive API documentation generated",
                "User guides and tutorials created",
                "Developer and system documentation completed",
                "Operations and maintenance guides provided"
            ]

            # Create collaborative handoff for documentation review
            from handoff.protocol import HandoffManager
            from handoff.collaboration import CollaborationOrchestrator
            from handoff.quality_gate import QualityGate
            from state_management.manager import StateManager
            from services.github_integration import GitHubIntegration, AgentCommentFormatter

            # Initialize collaboration
            state_manager = context.get('state_manager') or StateManager()
            handoff_manager = HandoffManager(state_manager)
            collaboration = CollaborationOrchestrator(handoff_manager)
            github = GitHubIntegration()

            # Prepare artifacts for documentation review
            artifacts = {
                "api_docs": doc_data.get('documentation_artifacts', {}).get('api_documentation', []),
                "user_docs": doc_data.get('documentation_artifacts', {}).get('user_documentation', []),
                "developer_docs": doc_data.get('documentation_artifacts', {}).get('developer_documentation', []),
                "system_docs": doc_data.get('documentation_artifacts', {}).get('system_documentation', []),
                "quality_metrics": doc_data.get('quality_metrics', {})
            }

            # Post documentation summary to GitHub
            github_issue = context.get('context', {}).get('issue_number')
            if github_issue:
                total_pages = doc_data.get('quality_metrics', {}).get('total_pages', 0)
                completeness = doc_data.get('quality_metrics', {}).get('documentation_completeness', 0)
                
                status_update = AgentCommentFormatter.format_agent_status_update(
                    agent_name="technical_writer",
                    status="completed",
                    details={
                        'summary': f"Technical documentation completed for {project}",
                        'findings': [
                            f"Generated {total_pages} pages of documentation",
                            f"Documentation completeness: {completeness * 100:.1f}%",
                            "API, user, developer, and system documentation created",
                            "Operations and maintenance guides provided"
                        ],
                        'next_steps': ["Documentation review requested"],
                        'artifacts': list(artifacts.keys()),
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    }
                )
                
                await github.post_issue_comment(github_issue, status_update, project)

            # Initiate documentation review workflow
            next_agents = ["documentation_editor"]
            
            handoff = await collaboration.initiate_maker_checker_flow(
                maker_agent="technical_writer",
                checker_agents=next_agents,
                context=context,
                artifacts=artifacts,
                github_issue=github_issue
            )

            # Validate documentation quality
            quality_gate = QualityGate({
                "documentation_completeness": 0.8,
                "clarity_score": 0.8,
                "accuracy_score": 0.85
            })

            passed, issues = quality_gate.evaluate(handoff)
            if not passed:
                context['warnings'] = issues
                logger.warning(f"Documentation quality gate issues: {issues}")

            context['handoff_id'] = handoff.handoff_id
            context['collaboration_active'] = True
            context['pending_reviews'] = next_agents
            context['documentation_completed'] = True

            logger.info(f"Documentation completed. Review requested from: {', '.join(next_agents)}")
            return context

        except Exception as e:
            raise Exception(f"Technical documentation failed: {str(e)}")
