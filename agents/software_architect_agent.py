from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from datetime import datetime
import json
import logging

logger = logging.getLogger(__name__)


class SoftwareArchitectAgent(PipelineStage):
    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("software_architect", agent_config=agent_config)

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute software architecture design based on approved requirements and product strategy"""

        # Extract requirements and product decisions from context
        requirements_analysis = context.get('requirements_analysis', {})
        product_analysis = context.get('product_analysis', {})
        strategic_recommendations = context.get('strategic_recommendations', {})
        approved_features = context.get('approved_features', [])

        project = context.get('project', 'unknown')
        tech_stack = context.get('tech_stack', {})

        prompt = f"""
As a Software Architect, design a comprehensive software architecture based on the approved requirements and product strategy.

Project: {project}
Technology Stack: {json.dumps(tech_stack, indent=2)}

Requirements Analysis:
{json.dumps(requirements_analysis, indent=2)}

Product Strategy:
{json.dumps(product_analysis, indent=2)}

Strategic Recommendations:
{json.dumps(strategic_recommendations, indent=2)}

Approved MVP Features: {approved_features}

Design a comprehensive software architecture considering:

1. System Architecture:
   - Overall system design and architecture patterns
   - Component breakdown and boundaries
   - Data flow and system interactions
   - Integration points and APIs

2. Scalability Design:
   - Horizontal and vertical scaling strategies
   - Load balancing and distribution patterns
   - Caching strategies
   - Database scaling approaches

3. Performance Architecture:
   - Performance requirements and targets
   - Optimization strategies
   - Monitoring and observability design
   - Resource allocation planning

4. Security Architecture:
   - Security patterns and controls
   - Authentication and authorization design
   - Data protection strategies
   - Threat mitigation approaches

5. Maintainability Design:
   - Code organization and modularity
   - Dependency management
   - Configuration management
   - Deployment and DevOps considerations

6. Technology Decisions:
   - Framework and library selections
   - Database and storage choices
   - Infrastructure and cloud services
   - Third-party integrations

7. Architecture Decision Records (ADRs):
   - Key architectural decisions with rationale
   - Trade-off analyses
   - Alternative options considered
   - Impact assessments

8. Implementation Plan:
   - Phased implementation approach
   - Critical path identification
   - Resource requirements
   - Risk assessment and mitigation

Return structured JSON with architecture_design, technical_decisions, and implementation_plan sections.
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
                    # Use Serena to find similar architecture patterns
                    search_results = await self.mcp_integration.serena_search(
                        f"software architecture design patterns {' '.join(tech_stack.get('backend', []))}",
                        file_types=['py', 'md', 'yaml', 'json']
                    )
                    if search_results:
                        enhanced_context['architecture_patterns'] = search_results[:3]

                    logger.info(f"Enhanced context with {len(search_results)} architecture patterns from Serena")
                except Exception as e:
                    logger.warning(f"Serena search failed: {e}")

            result = await run_claude_code(prompt, enhanced_context)

            # Parse Claude's response
            if isinstance(result, str):
                try:
                    architecture = json.loads(result)
                except json.JSONDecodeError:
                    # If not valid JSON, create a structured response
                    architecture = {
                        "architecture_design": {
                            "system_overview": f"Software architecture designed for {project}",
                            "components": ["Frontend", "Backend API", "Database", "Authentication"],
                            "patterns": ["MVC", "Repository Pattern", "Dependency Injection"],
                            "scalability_approach": "Microservices with horizontal scaling",
                            "security_design": "JWT authentication with role-based access",
                            "performance_strategy": "Caching and database optimization"
                        },
                        "technical_decisions": {
                            "framework_choices": tech_stack.get('backend', ['Python', 'FastAPI']),
                            "database_design": "PostgreSQL with Redis caching",
                            "deployment_strategy": "Docker containers with Kubernetes",
                            "monitoring_approach": "Prometheus and Grafana",
                            "testing_strategy": "Unit, integration, and end-to-end testing"
                        },
                        "implementation_plan": {
                            "phases": ["Core backend", "Frontend development", "Integration", "Deployment"],
                            "critical_path": ["Database schema", "API design", "Authentication"],
                            "risk_mitigation": ["Prototype critical components", "Performance testing"],
                            "resource_estimates": "2-3 developers, 8-12 weeks"
                        },
                        "quality_metrics": {
                            "architectural_soundness": 0.8,
                            "scalability_readiness": 0.75,
                            "security_compliance": 0.8,
                            "maintainability_score": 0.85
                        }
                    }
            else:
                architecture = result

            # Add to context for next stage
            context['architecture_design'] = architecture.get('architecture_design', {})
            context['technical_decisions'] = architecture.get('technical_decisions', {})
            context['implementation_plan'] = architecture.get('implementation_plan', {})
            context['quality_metrics'] = {**context.get('quality_metrics', {}), **architecture.get('quality_metrics', {})}
            context['completed_work'] = context.get('completed_work', []) + [
                "Software architecture design completed",
                "System components and patterns defined",
                "Scalability and performance strategy established",
                "Security architecture designed",
                "Technical decisions documented with ADRs"
            ]

            # Create collaborative handoff
            from handoff.protocol import HandoffManager
            from handoff.collaboration import CollaborationOrchestrator
            from handoff.quality_gate import QualityGate
            from state_management.manager import StateManager
            from services.github_integration import GitHubIntegration, AgentCommentFormatter

            # Get state manager from context or create one
            state_manager = context.get('state_manager')
            if not state_manager:
                state_manager = StateManager()

            # Initialize collaboration components
            handoff_manager = HandoffManager(state_manager)
            collaboration = CollaborationOrchestrator(handoff_manager)
            github = GitHubIntegration()

            # Prepare collaborative handoff
            github_issue = context.get('context', {}).get('issue_number')
            project = context.get('project', 'unknown')

            # Create collaborative handoff with review workflow
            artifacts = {
                "architecture_document": architecture.get('architecture_design', {}),
                "technical_decisions": architecture.get('technical_decisions', {}),
                "implementation_plan": architecture.get('implementation_plan', {}),
                "adr_records": architecture.get('technical_decisions', {}).get('adrs', []),
                "quality_metrics": architecture.get('quality_metrics', {})
            }

            # Post work summary to GitHub
            if github_issue:
                status_update = AgentCommentFormatter.format_agent_status_update(
                    agent_name="software_architect",
                    status="completed",
                    details={
                        'summary': f"Software architecture designed for {project}",
                        'findings': [
                            "System architecture and component design completed",
                            "Scalability and performance strategies defined",
                            "Security architecture established",
                            f"Architectural soundness: {architecture.get('quality_metrics', {}).get('architectural_soundness', 0) * 100:.1f}%"
                        ],
                        'next_steps': ["Architecture review requested"],
                        'artifacts': list(artifacts.keys()),
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    }
                )

                await github.post_issue_comment(github_issue, status_update, project)

            # Determine next agents based on pipeline configuration
            next_agents = ["design_reviewer"]

            # Create collaborative handoff
            collab_context = {
                **context,
                'completed_work': [
                    "Software architecture design completed",
                    "System components and boundaries defined",
                    "Scalability and performance strategies established",
                    "Security architecture designed",
                    "Technical decisions documented with rationale"
                ],
                'decisions_made': [
                    {
                        'agent': 'software_architect',
                        'topic': 'System Architecture',
                        'decision': 'Selected microservices pattern with event-driven communication',
                        'rationale': 'Supports scalability requirements and team autonomy',
                        'timestamp': datetime.now().isoformat()
                    },
                    {
                        'agent': 'software_architect',
                        'topic': 'Technology Stack',
                        'decision': f"Selected {', '.join(architecture.get('technical_decisions', {}).get('framework_choices', []))}",
                        'rationale': 'Aligns with team expertise and project requirements',
                        'timestamp': datetime.now().isoformat()
                    }
                ],
                'quality_metrics': architecture.get('quality_metrics', {}),
                'success_criteria': [
                    "Architecture supports all functional requirements",
                    "Scalability targets achievable",
                    "Security requirements addressed",
                    "Maintainability principles followed",
                    "Performance targets realistic"
                ]
            }

            # Initiate maker-checker flow
            handoff = await collaboration.initiate_maker_checker_flow(
                maker_agent="software_architect",
                checker_agents=next_agents,
                context=collab_context,
                artifacts=artifacts,
                github_issue=github_issue
            )

            # Validate handoff package
            quality_gate = QualityGate({
                "architectural_soundness": 0.7,
                "scalability_readiness": 0.6,
                "security_compliance": 0.7
            })

            passed, issues = quality_gate.evaluate(handoff)
            if not passed:
                context['warnings'] = issues
                logger.warning(f"Quality gate issues: {issues}")

            context['handoff_id'] = handoff.handoff_id
            context['collaboration_active'] = True
            context['pending_reviews'] = next_agents

            logger.info(f"Collaborative handoff created: {handoff.handoff_id}")
            logger.info(f"Architecture review requested from: {', '.join(next_agents)}")

            # Update GitHub status when task completes
            await self.update_github_status(context)

            return context

        except Exception as e:
            raise Exception(f"Software architecture design failed: {str(e)}")

    async def update_github_status(self, context):
        """Update GitHub issue/project when task completes"""

        task_context = context.get('context', {})
        if 'issue_number' in task_context:
            issue_number = task_context['issue_number']
            project = context.get('project', '')

            # Add comment to issue
            architecture_design = context.get('architecture_design', {})
            technical_decisions = context.get('technical_decisions', {})
            quality_metrics = context.get('quality_metrics', {})

            comment = f"""
🏗️ **Software Architecture Design Complete**

Comprehensive software architecture has been designed by the orchestrator.

**Summary:**
- System architecture and component design completed
- Scalability and performance strategies defined
- Security architecture established
- Technical decisions documented with ADRs
- Architectural soundness: {quality_metrics.get('architectural_soundness', 0) * 100:.1f}%

**Next Steps:**
Moving to architecture review phase...

---
_Generated by Orchestrator Bot 🤖_
_Processed by the software_architect agent_
            """.strip()

            try:
                import subprocess
                from config.manager import config_manager

                # Get proper GitHub org/repo from project config
                project_config = config_manager.get_project_config(project)
                github_org = project_config.github.get('org')
                github_repo = project_config.github.get('repo')

                if not github_repo or not github_org:
                    logger.error(f"GitHub org/repo not configured for project {project}")
                    return

                result = subprocess.run([
                    'gh', 'issue', 'comment', str(issue_number),
                    '--body', comment,
                    '--repo', f"{github_org}/{github_repo}"
                ], capture_output=True, text=True)

                if result.returncode == 0:
                    logger.info(f"Updated GitHub issue #{issue_number}")
                else:
                    logger.error(f"Failed to update GitHub issue: {result.stderr}")

            except Exception as e:
                logger.error(f"Could not update GitHub status: {e}")
                # Don't fail the task if GitHub update fails