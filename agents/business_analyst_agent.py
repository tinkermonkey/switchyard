from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from datetime import datetime
import json
import logging

logger = logging.getLogger(__name__)

class BusinessAnalystAgent(PipelineStage):
    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("business_analyst", agent_config=agent_config)

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute business analysis on the given issue/requirements"""

        issue = context.get('context', {}).get('issue', {})
        project = context.get('project', 'unknown')

        # Check if there's previous stage context
        previous_stage = context.get('context', {}).get('previous_stage_output', '')
        previous_stage_prompt = ""

        if previous_stage:
            previous_stage_prompt = f"""

PREVIOUS STAGE OUTPUT:
{previous_stage}

Build upon this previous analysis in your work.
"""

        # Check if this is a feedback loop
        feedback_data = context.get('context', {}).get('feedback')
        feedback_prompt = ""

        if feedback_data:
            feedback_prompt = f"""

HUMAN FEEDBACK RECEIVED:
{feedback_data.get('formatted_text', '')}

Please review your previous analysis and update it based on this feedback.
Address all concerns and questions raised by the human reviewer.
"""

        prompt = f"""
Analyze the following issue/requirement for project {project}:

Title: {issue.get('title', 'No title')}
Description: {issue.get('body', 'No description')}
Labels: {issue.get('labels', [])}
{previous_stage_prompt}
{feedback_prompt}

Provide a comprehensive business analysis following the format specified in your configuration.
Focus on extracting clear functional requirements and creating actionable user stories.

Return the response as structured JSON with requirements_analysis and quality_metrics sections.
"""

        try:
            # Enhance context with MCP server data if available
            enhanced_context = context.copy()

            # Add MCP server configuration to context for Claude Code
            if hasattr(self, 'mcp_integration') and self.mcp_integration:
                # Pass MCP server URLs to Claude Code
                enhanced_context['mcp_servers'] = []
                for name, server in self.mcp_integration.servers.items():
                    enhanced_context['mcp_servers'].append({
                        'name': name,
                        'url': server.url,
                        'capabilities': server.capabilities
                    })

                try:
                    # Use Serena to analyze related code patterns in the orchestrator
                    search_results = await self.mcp_integration.serena_search(
                        f"requirements analysis {issue.get('title', '')}",
                        file_types=['py', 'md', 'yaml']
                    )
                    if search_results:
                        enhanced_context['related_patterns'] = search_results[:3]  # Top 3 results

                    logger.info(f"Enhanced context with {len(search_results)} related patterns from Serena")
                except Exception as e:
                    logger.warning(f"Serena search failed: {e}")

            result = await run_claude_code(prompt, enhanced_context)

            # Parse Claude's response (it might be JSON string or already parsed)
            if isinstance(result, str):
                try:
                    analysis = json.loads(result)
                except json.JSONDecodeError:
                    # If not valid JSON, create a structured response
                    analysis = {
                        "requirements_analysis": {
                            "summary": f"Analysis completed for: {issue.get('title', 'Untitled')}",
                            "functional_requirements": ["Basic functionality extracted from issue"],
                            "non_functional_requirements": ["Performance and reliability requirements"],
                            "user_stories": [{
                                "title": issue.get('title', 'User Story'),
                                "description": f"As a user I want {issue.get('title', 'functionality')} so that I can achieve my goals",
                                "acceptance_criteria": ["Given a user", "When they interact", "Then they should see results"],
                                "priority": "Medium"
                            }],
                            "risks": ["Implementation complexity"],
                            "assumptions": ["User requirements understood"]
                        },
                        "quality_metrics": {
                            "completeness_score": 0.75,
                            "clarity_score": 0.80,
                            "testability_score": 0.70
                        }
                    }
            else:
                analysis = result

            # Add to context for next stage
            context['requirements_analysis'] = analysis.get('requirements_analysis', {})
            context['quality_metrics'] = analysis.get('quality_metrics', {})
            context['completed_work'] = context.get('completed_work', []) + [
                "Business requirements analysis completed",
                f"Generated {len(analysis.get('requirements_analysis', {}).get('user_stories', []))} user stories"
            ]

            # Create collaborative handoff with GitHub integration
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
            repository = context.get('context', {}).get('repository', project)

            # Create collaborative handoff with review workflow
            artifacts = {
                "requirements_document": analysis.get('requirements_analysis', {}),
                "user_stories": analysis.get('requirements_analysis', {}).get('user_stories', []),
                "quality_metrics": analysis.get('quality_metrics', {})
            }

            # GitHub comment will be posted in update_github_status method
            # Store the raw result for the comment
            context['raw_analysis_result'] = result

            # Determine next agents based on pipeline configuration
            next_agents = ["requirements_reviewer"]  # Could be configured

            # Create collaborative handoff
            collab_context = {
                **context,
                'completed_work': [
                    "Business requirements analysis completed",
                    f"Generated {len(artifacts.get('user_stories', []))} user stories",
                    "Identified functional and non-functional requirements"
                ],
                'decisions_made': [
                    {
                        'agent': 'business_analyst',
                        'topic': 'Requirements Structure',
                        'decision': 'Used INVEST criteria for user stories',
                        'rationale': 'Ensures stories are Independent, Negotiable, Valuable, Estimable, Small, and Testable',
                        'timestamp': datetime.now().isoformat()
                    }
                ],
                'quality_metrics': analysis.get('quality_metrics', {}),
                'success_criteria': [
                    "All requirements are clear and testable",
                    "User stories follow INVEST principles",
                    "Acceptance criteria are well-defined"
                ]
            }

            # Initiate maker-checker flow
            handoff = await collaboration.initiate_maker_checker_flow(
                maker_agent="business_analyst",
                checker_agents=next_agents,
                context=collab_context,
                artifacts=artifacts,
                github_issue=github_issue
            )

            # Validate handoff package
            quality_gate = QualityGate({
                "completeness_score": 0.7,
                "clarity_score": 0.7
            })

            passed, issues = quality_gate.evaluate(handoff)
            if not passed:
                context['warnings'] = issues
                logger.warning(f"Quality gate issues: {issues}")

            context['handoff_id'] = handoff.handoff_id
            context['collaboration_active'] = True
            context['pending_reviews'] = next_agents

            logger.info(f"Collaborative handoff created: {handoff.handoff_id}")
            logger.info(f"Review requested from: {', '.join(next_agents)}")

            # Update GitHub status when task completes
            await self.update_github_status(context)

            return context

        except Exception as e:
            raise Exception(f"Business analysis failed: {str(e)}")

    async def update_github_status(self, context):
        """Update GitHub issue/project when task completes"""

        task_context = context.get('context', {})
        if 'issue_number' in task_context:
            issue_number = task_context['issue_number']
            project = context.get('project', '')

            # Get the actual analysis output from Claude
            raw_analysis = context.get('raw_analysis_result', 'No analysis available')
            requirements_analysis = context.get('requirements_analysis', {})
            user_stories = requirements_analysis.get('user_stories', [])
            quality_metrics = context.get('quality_metrics', {})

            # Use AgentCommentFormatter for consistent formatting
            from services.github_integration import AgentCommentFormatter

            comment = AgentCommentFormatter.format_agent_completion(
                agent_name='business_analyst',
                output=raw_analysis,
                summary_stats={
                    'user_stories_generated': len(user_stories),
                    'functional_requirements_identified': len(requirements_analysis.get('functional_requirements', [])),
                    'non_functional_requirements_identified': len(requirements_analysis.get('non_functional_requirements', [])),
                    'completeness_score': quality_metrics.get('completeness_score', 0),
                    'clarity_score': quality_metrics.get('clarity_score', 0),
                    'testability_score': quality_metrics.get('testability_score', 0)
                },
                next_steps='Moving to requirements review phase...'
            )

            try:
                import subprocess
                from config.manager import config_manager
                from services.feedback_manager import FeedbackManager

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

                    # Track comment timestamp for feedback loop
                    feedback_manager = FeedbackManager()
                    from datetime import timezone
                    feedback_manager.set_last_agent_comment_time(
                        issue_number,
                        'business_analyst',
                        datetime.now(timezone.utc).isoformat()
                    )
                else:
                    logger.error(f"Failed to update GitHub issue: {result.stderr}")

            except Exception as e:
                logger.error(f"Could not update GitHub status: {e}")
                # Don't fail the task if GitHub update fails