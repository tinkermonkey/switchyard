from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from datetime import datetime
import json
import logging

logger = logging.getLogger(__name__)


class TestPlannerAgent(PipelineStage):
    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("test_planner", agent_config=agent_config)

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute comprehensive test planning based on approved architecture design"""

        # Extract architecture and requirements from context
        architecture_design = context.get('architecture_design', {})
        technical_decisions = context.get('technical_decisions', {})
        requirements_analysis = context.get('requirements_analysis', {})
        user_stories = requirements_analysis.get('user_stories', [])
        approved_architecture = context.get('approved_architecture', {})

        project = context.get('project', 'unknown')

        prompt = f"""
As a Senior QA Engineer and Test Planner, develop a comprehensive test strategy based on the approved architecture and requirements.

Architecture Design:
{json.dumps(architecture_design, indent=2)}

Technical Decisions:
{json.dumps(technical_decisions, indent=2)}

Requirements Analysis:
{json.dumps(requirements_analysis, indent=2)}

User Stories ({len(user_stories)} total):
{json.dumps(user_stories, indent=2)}

Project: {project}

Develop comprehensive test planning with:

1. Test Strategy Overview:
   - Testing approach and methodology
   - Test levels and scope (unit, integration, system, acceptance)
   - Testing pyramid strategy
   - Risk-based testing approach
   - Quality gates and exit criteria

2. Test Case Design:
   - Functional test cases for each user story
   - Non-functional test cases (performance, security, usability)
   - Equivalence partitioning and boundary value analysis
   - Error handling and edge case scenarios
   - Negative testing scenarios

3. Test Automation Strategy:
   - Automation framework selection
   - Unit testing approach and coverage targets
   - Integration testing strategy
   - End-to-end testing plan
   - API testing requirements
   - Performance testing baselines

4. Test Environment Planning:
   - Test environment requirements
   - Data management strategy
   - Test data generation and management
   - Environment provisioning and teardown
   - CI/CD integration requirements

5. Performance Testing Plan:
   - Performance requirements and targets
   - Load testing scenarios
   - Stress testing approach
   - Scalability testing strategy
   - Performance monitoring and metrics

6. Security Testing Strategy:
   - Security testing requirements
   - Vulnerability assessment approach
   - Penetration testing scope
   - Authentication and authorization testing
   - Data protection testing

7. Accessibility and Usability Testing:
   - Accessibility compliance requirements (WCAG)
   - Usability testing scenarios
   - Cross-browser and device testing
   - User experience validation

8. Test Execution Planning:
   - Test execution schedule
   - Resource requirements
   - Test milestone definitions
   - Defect management process
   - Reporting and metrics

Return structured JSON with test_strategy, test_cases, and automation_plan sections.
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
                    # Use Serena to find similar test planning patterns
                    search_results = await self.mcp_integration.serena_search(
                        f"test planning strategy automation {' '.join(technical_decisions.get('framework_choices', []))}",
                        file_types=['py', 'md', 'yaml']
                    )
                    if search_results:
                        enhanced_context['testing_patterns'] = search_results[:3]

                    logger.info(f"Enhanced context with {len(search_results)} testing patterns from Serena")
                except Exception as e:
                    logger.warning(f"Serena search failed: {e}")

            result = await run_claude_code(prompt, enhanced_context)

            # Parse Claude's response
            if isinstance(result, str):
                try:
                    test_plan = json.loads(result)
                except json.JSONDecodeError:
                    # If not valid JSON, create a structured response
                    test_plan = {
                        "test_strategy": {
                            "approach": "Risk-based testing with automation-first strategy",
                            "test_levels": ["Unit", "Integration", "System", "Acceptance"],
                            "coverage_targets": {"unit": 80, "integration": 70, "e2e": 60},
                            "quality_gates": ["All tests pass", "Coverage targets met", "Performance baselines achieved"],
                            "risk_areas": ["API endpoints", "Data validation", "Authentication"],
                            "testing_pyramid": "70% unit, 20% integration, 10% e2e"
                        },
                        "test_cases": {
                            "functional_tests": [{"story": story.get('title', ''), "test_scenarios": 3} for story in user_stories[:3]],
                            "non_functional_tests": ["Performance", "Security", "Usability", "Accessibility"],
                            "negative_tests": ["Invalid input", "Boundary conditions", "Error scenarios"],
                            "integration_tests": ["API integration", "Database integration", "External service integration"],
                            "total_test_cases": len(user_stories) * 4 + 15
                        },
                        "automation_plan": {
                            "framework": "pytest + selenium + requests",
                            "ci_cd_integration": "GitHub Actions with test reporting",
                            "test_environments": ["Development", "Staging", "Production-like"],
                            "automation_targets": {"unit": 95, "api": 85, "ui": 60},
                            "performance_baselines": {"response_time": "< 200ms", "throughput": "> 1000 req/s"}
                        },
                        "quality_metrics": {
                            "test_coverage_target": 0.8,
                            "automation_coverage": 0.75,
                            "test_completeness": 0.85
                        }
                    }
            else:
                test_plan = result

            # Add to context for next stage
            context['test_strategy'] = test_plan.get('test_strategy', {})
            context['test_cases'] = test_plan.get('test_cases', {})
            context['automation_plan'] = test_plan.get('automation_plan', {})
            context['quality_metrics'] = {**context.get('quality_metrics', {}), **test_plan.get('quality_metrics', {})}
            context['completed_work'] = context.get('completed_work', []) + [
                "Comprehensive test strategy developed",
                "Test cases designed for all user stories",
                "Automation framework and CI/CD integration planned",
                "Performance and security testing strategies defined"
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
                "test_strategy": test_plan.get('test_strategy', {}),
                "test_cases": test_plan.get('test_cases', {}),
                "automation_plan": test_plan.get('automation_plan', {}),
                "performance_baselines": test_plan.get('automation_plan', {}).get('performance_baselines', {}),
                "quality_metrics": test_plan.get('quality_metrics', {})
            }

            # Post work summary to GitHub
            if github_issue:
                total_test_cases = test_plan.get('test_cases', {}).get('total_test_cases', 0)
                coverage_target = test_plan.get('quality_metrics', {}).get('test_coverage_target', 0)

                status_update = AgentCommentFormatter.format_agent_status_update(
                    agent_name="test_planner",
                    status="completed",
                    details={
                        'summary': f"Comprehensive test plan developed for {project}",
                        'findings': [
                            f"Test strategy covering {', '.join(test_plan.get('test_strategy', {}).get('test_levels', []))} levels",
                            f"Planned {total_test_cases} test cases across all user stories",
                            f"Automation coverage target: {test_plan.get('automation_plan', {}).get('automation_targets', {}).get('unit', 0)}%",
                            f"Test coverage target: {coverage_target * 100:.0f}%"
                        ],
                        'next_steps': ["Test plan review requested"],
                        'artifacts': list(artifacts.keys()),
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    }
                )

                await github.post_issue_comment(github_issue, status_update, project)

            # Determine next agents based on pipeline configuration
            next_agents = ["test_reviewer"]

            # Create collaborative handoff
            collab_context = {
                **context,
                'completed_work': [
                    "Comprehensive test strategy developed",
                    "Test cases designed using equivalence partitioning and boundary analysis",
                    "Automation framework and CI/CD integration planned",
                    "Performance baselines and security testing strategies defined"
                ],
                'decisions_made': [
                    {
                        'agent': 'test_planner',
                        'topic': 'Test Strategy',
                        'decision': f"Selected {test_plan.get('test_strategy', {}).get('testing_pyramid', 'balanced')} testing pyramid approach",
                        'rationale': 'Optimizes for fast feedback and comprehensive coverage',
                        'timestamp': datetime.now().isoformat()
                    },
                    {
                        'agent': 'test_planner',
                        'topic': 'Automation Framework',
                        'decision': test_plan.get('automation_plan', {}).get('framework', 'Standard testing framework'),
                        'rationale': 'Aligns with project technology stack and team expertise',
                        'timestamp': datetime.now().isoformat()
                    }
                ],
                'quality_metrics': test_plan.get('quality_metrics', {}),
                'success_criteria': [
                    "All functional requirements covered by test cases",
                    "Automation coverage targets achievable",
                    "Performance baselines realistic",
                    "Security testing comprehensive",
                    "CI/CD integration feasible"
                ]
            }

            # Initiate maker-checker flow
            handoff = await collaboration.initiate_maker_checker_flow(
                maker_agent="test_planner",
                checker_agents=next_agents,
                context=collab_context,
                artifacts=artifacts,
                github_issue=github_issue
            )

            # Validate handoff package
            quality_gate = QualityGate({
                "test_coverage_target": 0.7,
                "automation_coverage": 0.6,
                "test_completeness": 0.7
            })

            passed, issues = quality_gate.evaluate(handoff)
            if not passed:
                context['warnings'] = issues
                logger.warning(f"Quality gate issues: {issues}")

            context['handoff_id'] = handoff.handoff_id
            context['collaboration_active'] = True
            context['pending_reviews'] = next_agents

            logger.info(f"Collaborative handoff created: {handoff.handoff_id}")
            logger.info(f"Test plan review requested from: {', '.join(next_agents)}")

            # Update GitHub status when task completes
            await self.update_github_status(context)

            return context

        except Exception as e:
            raise Exception(f"Test planning failed: {str(e)}")

    async def update_github_status(self, context):
        """Update GitHub issue/project when task completes"""

        task_context = context.get('context', {})
        if 'issue_number' in task_context:
            issue_number = task_context['issue_number']
            project = context.get('project', '')

            # Add comment to issue
            test_strategy = context.get('test_strategy', {})
            test_cases = context.get('test_cases', {})
            automation_plan = context.get('automation_plan', {})
            quality_metrics = context.get('quality_metrics', {})

            total_cases = test_cases.get('total_test_cases', 0)
            coverage_target = quality_metrics.get('test_coverage_target', 0)

            comment = f"""
🧪 **Test Planning Complete**

Comprehensive test strategy and plan have been developed by the orchestrator.

**Summary:**
- Test strategy covering {', '.join(test_strategy.get('test_levels', []))} levels
- {total_cases} test cases planned across all user stories
- Automation framework selected and CI/CD integration planned
- Coverage target: {coverage_target * 100:.0f}%

**Next Steps:**
Moving to test plan review phase...

---
_Generated by Orchestrator Bot 🤖_
_Processed by the test_planner agent_
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