"""
PR Review Pipeline Stage

Multi-phase PR review orchestration that runs in orchestrator process.
Launches Docker containers for code review and verification phases.

Similar to RepairCycleStage architecture - orchestrates multiple agent
invocations without running in Docker itself.
"""

import logging
import subprocess
import json
import re
from typing import Dict, Any, List, Optional, Tuple
from pipeline.base import PipelineStage
from services.agent_executor import get_agent_executor
from config.manager import ConfigManager
from config.state_manager import GitHubStateManager
from state_management.pr_review_state_manager import pr_review_state_manager
from agents.non_retryable import NonRetryableAgentError
from monitoring.timestamp_utils import utc_now, utc_isoformat
from monitoring.observability import EventType
from monitoring.decision_events import DecisionEventEmitter
from services.cancellation import get_cancellation_signal

logger = logging.getLogger(__name__)

MAX_REVIEW_CYCLES = 3

# Patterns that indicate a section has no actionable findings.
# Used by _is_none_found() to prevent false-positive issue creation when Claude
# adds explanatory text after "None found" (e.g., "None found - issues were already resolved").
_NONE_FOUND_PATTERNS = [
    re.compile(r'^\s*[-*]?\s*["\']?\s*none\s+found\b', re.IGNORECASE),
    re.compile(r'^\s*[-*]?\s*(none|n/?a)\s*\.?\s*$', re.IGNORECASE),
    re.compile(r'^\s*[-*]?\s*no\s+(issues?|gaps?|deviations?|findings?|critical\s+issues?|problems?)\s+found\b', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^\s*[-*]?\s*all\s+requirements\s+(verified|met|satisfied)\b', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^\s*[-*]?\s*(already|previously)\s+(resolved|addressed|fixed|corrected|handled)\b', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^\s*[-*]?\s*no\s+actionable\b', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^\s*[-*]?\s*(clean\s+pass|no\s+concerns?)\b', re.IGNORECASE | re.MULTILINE),
]

# Structured finding patterns: Support multiple valid finding formats
_ACTIONABLE_FINDING_PATTERNS = [
    re.compile(r'^\s*[-*]\s+\*\*[^*]+\*\*\s*:', re.MULTILINE),     # - **Title**: desc
    re.compile(r'^\s*\d+\.\s+\*\*[^*]+\*\*\s*:', re.MULTILINE),   # 1. **Title**: desc
    re.compile(r'^\s*[-*]\s+\*\*[^*]+\*\*\s+', re.MULTILINE),     # - **Title** desc (no colon)
    re.compile(r'^\*\*[^*]+\*\*\s*:', re.MULTILINE),              # **Title**: desc (no bullet)
]


class PRReviewStage(PipelineStage):
    """
    PR Review orchestration stage.

    Runs in orchestrator (not Docker) and launches containers for:
    - Phase 1: PR code review (pr_code_reviewer agent)
    - Phase 2: Requirements verification (requirements_verifier agent, up to 4x)
    - Phase 3: CI status check (local gh CLI, no Docker)

    The stage itself has access to:
    - Project directories via workspace_manager
    - GitHub CLI (gh)
    - All orchestrator services

    Agents launched by this stage run in Docker with project code mounted.
    """

    def __init__(
        self,
        name: str = "pr_review",
        pr_review_agent: str = "pr_code_reviewer",
        requirements_verifier_agent: str = "requirements_verifier",
        max_agent_calls: int = 20,
        **kwargs
    ):
        super().__init__(name, **kwargs)

        # Validate required parameters
        if not pr_review_agent or not requirements_verifier_agent:
            raise ValueError("pr_review_agent and requirements_verifier_agent are required")

        self.pr_review_agent = pr_review_agent
        self.requirements_verifier_agent = requirements_verifier_agent
        self.config_manager = ConfigManager()
        self.state_manager = GitHubStateManager()

        # Circuit breaker for cost control
        self.max_agent_calls = max_agent_calls
        self._agent_call_count = 0

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute multi-phase PR review.

        This method runs in the orchestrator process (not Docker).
        It orchestrates multiple agent invocations, each running in Docker.
        """
        # Reset circuit breaker counter for this execution
        self._agent_call_count = 0

        # Get observability manager
        obs = context.get("observability")
        start_time = utc_now()

        task_context = context.get('context', {})
        project_name = task_context.get('project', 'unknown')
        issue_number = task_context.get('issue_number')
        pipeline_run_id = context.get("pipeline_run_id")

        # Resolve parent issue number
        parent_issue_number = self._resolve_parent_issue_number(task_context, project_name)
        if not parent_issue_number:
            logger.error("Could not determine parent issue number for PR review")
            context['markdown_analysis'] = "## PR Review Failed\n\nCould not determine parent issue number."
            return context

        task_id = context.get("task_id", f"pr_review_{parent_issue_number}")

        # Emit task received
        if obs:
            obs.emit_task_received("pr_review_stage", task_id, project_name, context, pipeline_run_id)

        logger.info(f"PR Review Stage executing for parent issue #{parent_issue_number} in {project_name}")

        # NOTE: record_execution_start is the caller's responsibility
        # (e.g. _start_pr_review_for_issue), matching the repair cycle pattern.

        try:
            # Check review cycle count
            review_count = pr_review_state_manager.get_review_count(project_name, parent_issue_number)

            if review_count >= MAX_REVIEW_CYCLES:
                msg = f"Review cycle limit ({MAX_REVIEW_CYCLES}) reached for #{parent_issue_number}"
                logger.warning(msg)
                raise NonRetryableAgentError(msg)

            current_cycle = review_count + 1
            logger.info(f"Starting review cycle {current_cycle}/{MAX_REVIEW_CYCLES}")

            # Get project config
            project_config = self.config_manager.get_project_config(project_name)
            github_config = project_config.github
            repo = f"{github_config['org']}/{github_config['repo']}"

            # Find PR for this parent issue
            pr_url = await self._find_pr_url(github_config, parent_issue_number)
            if not pr_url:
                raise NonRetryableAgentError(
                    f"No PR found for parent issue #{parent_issue_number} in {repo}"
                )

            # Emit agent initialized
            if obs:
                obs.emit_agent_initialized("pr_review_stage", task_id, project_name, {
                    "parent_issue": parent_issue_number,
                    "pr_url": pr_url,
                    "review_cycle": current_cycle,
                    "max_cycles": MAX_REVIEW_CYCLES,
                }, pipeline_run_id)

            # Get AgentExecutor (for launching Docker containers)
            agent_executor = get_agent_executor()

            all_created_issues = []
            review_summary_parts = []
            review_found_issues = False
            phases_attempted = 0
            phases_completed = 0

            # ---- Phase 1: PR Code Review ----
            # Check for cancellation
            if issue_number and get_cancellation_signal().is_cancelled(project_name, issue_number):
                logger.warning(f"PR review cancelled for {project_name}/#{issue_number}")
                context['markdown_analysis'] = "## PR Review Cancelled\n\nPipeline run ended externally."
                return context

            # Check circuit breaker
            if self._agent_call_count >= self.max_agent_calls:
                logger.error(f"Circuit breaker triggered: {self._agent_call_count} >= {self.max_agent_calls}")
                context['markdown_analysis'] = "## PR Review Failed\n\nCircuit breaker triggered (max agent calls reached)."
                return context

            logger.info(f"Phase 1: Running PR code review for {pr_url}")
            phase1_start = utc_now()
            phases_attempted += 1

            # Emit phase started event
            if obs:
                obs.emit(EventType.PR_REVIEW_PHASE_STARTED, "pr_review_stage", task_id, project_name, {
                    "phase": 1,
                    "phase_name": "PR Code Review",
                    "agent": self.pr_review_agent,
                }, pipeline_run_id)

            try:
                self._agent_call_count += 1
                # Build prior cycle context for reviewer memory (only on cycles 2+)
                prior_cycle_context = ""
                if current_cycle > 1:
                    prior_cycle_context = self._build_prior_cycle_context(
                        project_name, parent_issue_number, repo
                    )
                    if prior_cycle_context:
                        logger.info(
                            f"Injecting prior cycle history into reviewer prompt "
                            f"(cycle {current_cycle})"
                        )

                # Build prompt for pr_code_reviewer
                pr_review_prompt = self._build_pr_review_prompt(pr_url, prior_cycle_context)

                # Launch pr_code_reviewer in Docker (via AgentExecutor)
                pr_review_result = await agent_executor.execute_agent(
                    agent_name=self.pr_review_agent,
                    project_name=project_name,
                    task_context={
                        **task_context,
                        'pr_url': pr_url,
                        'phase': 'code_review',
                        'direct_prompt': pr_review_prompt,
                        # IMPORTANT: Don't skip workspace prep - agent needs project code
                        'skip_workspace_prep': False,
                    },
                    execution_type="pr_review_phase1"
                )

                # Parse findings
                pr_review_text = pr_review_result.get('markdown_analysis', '')
                review_issues = self._parse_review_findings(pr_review_text, "PR Code Review")
                review_summary_parts.append(f"### PR Code Review\n\n{pr_review_text}")
                phases_completed += 1

                phase1_duration = (utc_now() - phase1_start).total_seconds()
                logger.info(f"Phase 1 completed in {phase1_duration:.1f}s")

                if review_issues:
                    review_found_issues = True
                    created = await self._create_review_issues(
                        review_issues, repo, github_config, parent_issue_number, project_name,
                        obs, pipeline_run_id, pr_url
                    )
                    all_created_issues.extend(created)
                    logger.info(f"Phase 1: Created {len(created)} issues from PR review")
                else:
                    logger.info("Phase 1: No issues found in PR review")

                # Emit phase completed event
                if obs:
                    obs.emit(EventType.PR_REVIEW_PHASE_COMPLETED, "pr_review_stage", task_id, project_name, {
                        "phase": 1,
                        "phase_name": "PR Code Review",
                        "success": True,
                        "issues_found": len(review_issues),
                        "duration_seconds": phase1_duration,
                    }, pipeline_run_id)

            except Exception as e:
                logger.error(f"Phase 1 PR review failed: {e}", exc_info=True)
                review_summary_parts.append(f"### PR Code Review\n\nFailed: {e}")

                # Emit phase failed event
                if obs:
                    phase1_duration = (utc_now() - phase1_start).total_seconds()
                    obs.emit(EventType.PR_REVIEW_PHASE_FAILED, "pr_review_stage", task_id, project_name, {
                        "phase": 1,
                        "phase_name": "PR Code Review",
                        "success": False,
                        "error": str(e),
                        "duration_seconds": phase1_duration,
                    }, pipeline_run_id)

            # ---- Phase 2: Context Verification ----
            # Load context from discussion and parent issue
            discussion_outputs = self._load_discussion_outputs(project_name, parent_issue_number)
            parent_issue_body = self._get_parent_issue_body(repo, parent_issue_number)

            # Each entry: (display_name, authority_key, content)
            # The authority_key is passed to _build_verification_prompt to select
            # context-specific framing without relying on substring matching.
            context_checks = [
                ("Parent Issue Requirements", "parent_issue", parent_issue_body),
                ("Idea Researcher Output", "idea_researcher", discussion_outputs.get('idea_researcher')),
                ("Business Analyst Output", "business_analyst", discussion_outputs.get('business_analyst')),
                ("Software Architect Output", "software_architect", discussion_outputs.get('software_architect')),
            ]

            phase2_index = 0
            for check_name, authority_key, check_content in context_checks:
                if not check_content:
                    logger.info(f"Skipping {check_name} verification (no content)")
                    continue

                # Check for cancellation before each verification
                if issue_number and get_cancellation_signal().is_cancelled(project_name, issue_number):
                    logger.warning(f"PR review cancelled for {project_name}/#{issue_number}")
                    context['markdown_analysis'] = "## PR Review Cancelled\n\nPipeline run ended externally."
                    return context

                # Check circuit breaker before each verification
                if self._agent_call_count >= self.max_agent_calls:
                    logger.error(f"Circuit breaker triggered: {self._agent_call_count} >= {self.max_agent_calls}")
                    context['markdown_analysis'] = "## PR Review Failed\n\nCircuit breaker triggered (max agent calls reached)."
                    return context

                phase2_index += 1
                logger.info(f"Phase 2.{phase2_index}: Verifying against {check_name}")
                phase2_start = utc_now()
                phases_attempted += 1

                # Emit phase started event
                if obs:
                    obs.emit(EventType.PR_REVIEW_PHASE_STARTED, "pr_review_stage", task_id, project_name, {
                        "phase": 2,
                        "sub_phase": phase2_index,
                        "phase_name": f"Context Verification: {check_name}",
                        "agent": self.requirements_verifier_agent,
                    }, pipeline_run_id)

                try:
                    self._agent_call_count += 1
                    # Build verification prompt with authority framing for this source
                    verification_prompt = self._build_verification_prompt(
                        pr_url, check_name, authority_key, check_content
                    )

                    # Launch requirements_verifier in Docker (via AgentExecutor)
                    verification_result = await agent_executor.execute_agent(
                        agent_name=self.requirements_verifier_agent,
                        project_name=project_name,
                        task_context={
                            **task_context,
                            'pr_url': pr_url,
                            'check_name': check_name,
                            'check_content': check_content,
                            'phase': 'requirements_verification',
                            'direct_prompt': verification_prompt,
                            # IMPORTANT: Don't skip workspace prep - agent needs project code
                            'skip_workspace_prep': False,
                        },
                        execution_type="pr_review_phase2"
                    )

                    # Parse findings
                    verification_text = verification_result.get('markdown_analysis', '')
                    gap_issues = self._parse_review_findings(verification_text, check_name)
                    review_summary_parts.append(f"### {check_name} Verification\n\n{verification_text}")
                    phases_completed += 1

                    phase2_duration = (utc_now() - phase2_start).total_seconds()
                    logger.info(f"Phase 2.{phase2_index} completed in {phase2_duration:.1f}s")

                    if gap_issues:
                        review_found_issues = True
                        created = await self._create_review_issues(
                            gap_issues, repo, github_config, parent_issue_number, project_name,
                            obs, pipeline_run_id, pr_url
                        )
                        all_created_issues.extend(created)
                        logger.info(f"Created {len(created)} issues from {check_name} verification")
                    else:
                        logger.info(f"No gaps found in {check_name} verification")

                    # Emit phase completed event
                    if obs:
                        obs.emit(EventType.PR_REVIEW_PHASE_COMPLETED, "pr_review_stage", task_id, project_name, {
                            "phase": 2,
                            "sub_phase": phase2_index,
                            "phase_name": f"Context Verification: {check_name}",
                            "success": True,
                            "issues_found": len(gap_issues),
                            "duration_seconds": phase2_duration,
                        }, pipeline_run_id)

                except Exception as e:
                    logger.error(f"{check_name} verification failed: {e}", exc_info=True)
                    review_summary_parts.append(f"### {check_name} Verification\n\nFailed: {e}")

                    # Emit phase failed event
                    if obs:
                        phase2_duration = (utc_now() - phase2_start).total_seconds()
                        obs.emit(EventType.PR_REVIEW_PHASE_FAILED, "pr_review_stage", task_id, project_name, {
                            "phase": 2,
                            "sub_phase": phase2_index,
                            "phase_name": f"Context Verification: {check_name}",
                            "success": False,
                            "error": str(e),
                            "duration_seconds": phase2_duration,
                        }, pipeline_run_id)

            # ---- Phase 3: CI Status Check ----
            # Check for cancellation
            if issue_number and get_cancellation_signal().is_cancelled(project_name, issue_number):
                logger.warning(f"PR review cancelled for {project_name}/#{issue_number}")
                context['markdown_analysis'] = "## PR Review Cancelled\n\nPipeline run ended externally."
                return context

            # This runs locally (no Docker) using gh CLI
            logger.info(f"Phase 3: Checking CI status for {pr_url}")
            phase3_start = utc_now()
            phases_attempted += 1

            # Emit phase started event
            if obs:
                obs.emit(EventType.PR_REVIEW_PHASE_STARTED, "pr_review_stage", task_id, project_name, {
                    "phase": 3,
                    "phase_name": "CI Status Check",
                    "agent": "local_gh_cli",
                }, pipeline_run_id)

            try:
                failures, pending = self._check_ci_status(pr_url, repo)
                phases_completed += 1

                phase3_duration = (utc_now() - phase3_start).total_seconds()
                logger.info(f"Phase 3 completed in {phase3_duration:.1f}s")

                if failures:
                    review_found_issues = True
                    ci_issue_spec = self._build_ci_failure_issue(failures, pr_url)
                    created = await self._create_review_issues(
                        [ci_issue_spec], repo, github_config, parent_issue_number, project_name,
                        obs, pipeline_run_id, pr_url
                    )
                    all_created_issues.extend(created)
                    logger.info(f"Phase 3: {len(failures)} CI checks failing, created {len(created)} issues")
                    review_summary_parts.append(
                        f"### CI Status\n\n{len(failures)} failing check(s):\n\n"
                        + self._format_ci_table(failures)
                    )
                elif pending:
                    logger.warning(f"Phase 3: {len(pending)} CI checks still pending")
                    review_summary_parts.append(
                        f"### CI Status\n\n{len(pending)} check(s) still pending:\n\n"
                        + self._format_ci_table(pending)
                    )
                else:
                    logger.info("Phase 3: All CI checks passed")
                    review_summary_parts.append("### CI Status\n\nAll CI checks passed.")

                # Emit phase completed event
                if obs:
                    obs.emit(EventType.PR_REVIEW_PHASE_COMPLETED, "pr_review_stage", task_id, project_name, {
                        "phase": 3,
                        "phase_name": "CI Status Check",
                        "success": True,
                        "failures_found": len(failures),
                        "pending_count": len(pending),
                        "duration_seconds": phase3_duration,
                    }, pipeline_run_id)

            except Exception as e:
                logger.error(f"Phase 3 CI status check failed: {e}", exc_info=True)
                review_summary_parts.append(f"### CI Status\n\nFailed: {e}")

                # Emit phase failed event
                if obs:
                    phase3_duration = (utc_now() - phase3_start).total_seconds()
                    obs.emit(EventType.PR_REVIEW_PHASE_FAILED, "pr_review_stage", task_id, project_name, {
                        "phase": 3,
                        "phase_name": "CI Status Check",
                        "success": False,
                        "error": str(e),
                        "duration_seconds": phase3_duration,
                    }, pipeline_run_id)

            # ---- Post-review decision ----
            created_issue_numbers = [int(i['number']) for i in all_created_issues]
            manual_progression_made = False

            if phases_completed == 0:
                # All phases failed - inconclusive
                logger.error(f"All review phases failed for #{parent_issue_number}")
                pr_review_state_manager.increment_review_count(project_name, parent_issue_number, [])

            elif phases_completed < phases_attempted and not review_found_issues:
                # Some phases failed, no issues found - inconclusive
                logger.warning(
                    f"Only {phases_completed}/{phases_attempted} phases completed "
                    f"for #{parent_issue_number}. Treating as inconclusive."
                )
                pr_review_state_manager.increment_review_count(project_name, parent_issue_number, [])

            elif review_found_issues:
                # Issues found - return to development
                pr_review_state_manager.increment_review_count(
                    project_name, parent_issue_number, created_issue_numbers
                )

                if all_created_issues:
                    await self._move_issues_to_development(
                        all_created_issues, project_name, github_config
                    )
                self._return_parent_to_development(project_name, parent_issue_number)
                manual_progression_made = True

                if current_cycle >= MAX_REVIEW_CYCLES and all_created_issues:
                    summary_comment = self._build_cycle_limit_comment(
                        current_cycle, all_created_issues
                    )
                    self._post_comment_on_issue(repo, parent_issue_number, summary_comment)

            else:
                # Clean pass - advance to documentation
                logger.info(f"Clean pass for #{parent_issue_number}, advancing to Documentation")
                pr_review_state_manager.increment_review_count(project_name, parent_issue_number, [])
                self._advance_parent_to_documentation(project_name, parent_issue_number)
                manual_progression_made = True

            # Build final summary
            issues_summary = ""
            if all_created_issues:
                issues_list = "\n".join(f"- #{i['number']}: {i['title']}" for i in all_created_issues)
                issues_summary = f"\n\n### Issues Created\n\n{issues_list}"

            if phases_completed == 0:
                review_outcome = "Inconclusive (all phases failed)"
            elif phases_completed < phases_attempted and not review_found_issues:
                review_outcome = f"Inconclusive ({phases_completed}/{phases_attempted} phases completed)"
            elif review_found_issues:
                review_outcome = "Issues found"
            else:
                review_outcome = "Clean pass"

            context['markdown_analysis'] = (
                f"## PR Review - Cycle {current_cycle}/{MAX_REVIEW_CYCLES}\n\n"
                f"**PR**: {pr_url}\n"
                f"**Parent Issue**: #{parent_issue_number}\n"
                f"**Outcome**: {review_outcome}\n"
                f"**Issues Created**: {len(all_created_issues)}\n"
                + issues_summary + "\n\n"
                + "\n\n---\n\n".join(review_summary_parts)
            )

            context['created_review_issues'] = all_created_issues

            # Set manual progression flag to prevent auto-advancement
            if manual_progression_made:
                context['manual_progression_made'] = True

            # Emit agent completed event
            end_time = utc_now()
            duration_ms = (end_time - start_time).total_seconds() * 1000

            if obs:
                obs.emit_agent_completed("pr_review_stage", task_id, project_name, duration_ms,
                                       True, pipeline_run_id=pipeline_run_id)

            return context

        except Exception as e:
            logger.exception(f"PR review stage failed: {e}")

            # Emit failure event
            end_time = utc_now()
            duration_ms = (end_time - start_time).total_seconds() * 1000

            if obs:
                obs.emit_agent_completed("pr_review_stage", task_id, project_name,
                                        duration_ms, False, error=str(e),
                                        pipeline_run_id=pipeline_run_id)

            # Re-raise to let orchestrator handle
            raise

    # ==================================================================================
    # HELPER METHODS
    # ==================================================================================

    def _resolve_parent_issue_number(self, task_context: Dict[str, Any], project_name: str) -> Optional[int]:
        """Resolve the parent issue number from task context."""
        issue_number = task_context.get('issue_number')
        if issue_number:
            return int(issue_number)

        nested = task_context.get('task_context', {})
        if nested and nested.get('issue_number'):
            return int(nested['issue_number'])

        return None

    async def _find_pr_url(self, github_config: Dict, parent_issue_number: int) -> Optional[str]:
        """Find the PR URL for a parent issue using gh CLI directly."""
        repo = f"{github_config['org']}/{github_config['repo']}"
        branch_prefix = f'feature/issue-{parent_issue_number}-'

        try:
            result = subprocess.run(
                ['gh', 'pr', 'list', '-R', repo,
                 '--state', 'open',
                 '--json', 'number,url,headRefName',
                 '--limit', '100'],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                prs = json.loads(result.stdout)
                for pr in prs:
                    if pr.get('headRefName', '').startswith(branch_prefix):
                        logger.info(f"Found PR #{pr['number']} for parent #{parent_issue_number} (branch: {pr['headRefName']})")
                        return pr['url']

            logger.warning(f"No open PR found with branch prefix '{branch_prefix}' in {repo}")
            return None
        except Exception as e:
            logger.error(f"Failed to find PR for #{parent_issue_number}: {e}", exc_info=True)
            return None

    def _load_discussion_outputs(self, project_name: str, parent_issue_number: int) -> Dict[str, str]:
        """Load agent outputs from the discussion linked to the parent issue."""
        outputs = {}

        discussion_id = self.state_manager.get_discussion_for_issue(project_name, parent_issue_number)
        if not discussion_id:
            logger.info(f"No discussion found for #{parent_issue_number}")
            return outputs

        try:
            from services.github_app import github_app

            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
                  comments(last: 50) {
                    nodes {
                      body
                      replies(last: 50) {
                        nodes {
                          body
                        }
                      }
                    }
                  }
                }
              }
            }
            """

            result = github_app.graphql_request(query, {'discussionId': discussion_id})
            if not result or 'node' not in result or not result['node']:
                return outputs

            comments = result['node']['comments']['nodes']

            agent_keys = ['idea_researcher', 'business_analyst', 'software_architect']

            all_bodies = []
            for comment in comments:
                all_bodies.append(comment.get('body', ''))
                for reply in comment.get('replies', {}).get('nodes', []):
                    all_bodies.append(reply.get('body', ''))

            for body in all_bodies:
                for key in agent_keys:
                    if f'_Processed by the {key} agent_' in body:
                        outputs[key] = body

        except Exception as e:
            logger.error(f"Failed to load discussion outputs: {e}", exc_info=True)

        logger.info(f"Loaded discussion outputs for agents: {list(outputs.keys())}")
        return outputs

    def _get_parent_issue_body(self, repo: str, issue_number: int) -> str:
        """Get the body of the parent issue."""
        try:
            result = subprocess.run(
                ['gh', 'issue', 'view', str(issue_number), '-R', repo, '--json', 'body'],
                capture_output=True, text=True, check=True, timeout=30
            )
            data = json.loads(result.stdout)
            return data.get('body', '')
        except Exception as e:
            logger.error(f"Failed to get parent issue body: {e}", exc_info=True)
            return ''

    def _check_ci_status(self, pr_url: str, repo: str) -> tuple:
        """Check CI check status for a PR. Returns (failures, pending)."""
        match = re.search(r'/pull/(\d+)$', pr_url)
        if not match:
            raise ValueError(f"Could not extract PR number from URL: {pr_url}")

        pr_number = match.group(1)

        try:
            result = subprocess.run(
                ['gh', 'pr', 'checks', pr_number, '-R', repo,
                 '--json', 'name,state,bucket,description,link'],
                capture_output=True, text=True, timeout=60
            )

            if result.returncode not in (0, 1, 8):
                raise RuntimeError(
                    f"Unexpected exit code {result.returncode} from gh pr checks: "
                    f"{result.stderr.strip()}"
                )

            stdout = result.stdout.strip()
            if not stdout:
                logger.info("No CI checks configured for this PR")
                return ([], [])

            checks = json.loads(stdout)

            failures = [c for c in checks if c.get('bucket') == 'fail']
            pending = [c for c in checks if c.get('bucket') == 'pending']

            return (failures, pending)

        except Exception as e:
            logger.error(f"Failed to check CI status: {e}", exc_info=True)
            raise

    def _build_ci_failure_issue(self, failures: list, pr_url: str) -> Dict[str, Any]:
        """Build an issue spec for CI check failures."""
        table = self._format_ci_table(failures)
        body = (
            f"## CI Check Failures\n\n"
            f"**PR**: {pr_url}\n\n"
            f"The following CI checks are failing:\n\n"
            f"{table}\n\n"
            f"---\n"
            f"_Created by PR Review Stage_"
        )
        return {
            'title': '[PR Feedback] CI check failures',
            'body': body,
            'severity': 'high',
        }

    def _format_ci_table(self, checks: list) -> str:
        """Render a markdown table for CI check results."""
        lines = ["| Check | State | Details |", "| --- | --- | --- |"]
        for check in checks:
            name = check.get('name', 'Unknown')
            state = check.get('state', 'unknown')
            link = check.get('link', '')
            description = check.get('description', '')
            details = f"[View]({link})" if link else description
            lines.append(f"| {name} | {state} | {details} |")
        return "\n".join(lines)

    def _build_prior_cycle_context(
        self, project_name: str, parent_issue_number: int, repo: str
    ) -> str:
        """
        Build a summary of prior review cycle findings for injection into the reviewer prompt.

        Fetches issue titles from GitHub for each issue created in previous cycles so the
        reviewer can avoid re-reporting already-fixed issues and can flag regressions explicitly.
        """
        history = pr_review_state_manager.get_review_history(project_name, parent_issue_number)
        if not history:
            return ""

        lines = []
        for iteration in history:
            cycle_num = iteration.get('iteration', '?')
            timestamp = iteration.get('timestamp', '')
            issue_numbers = iteration.get('issues_created', [])

            if not issue_numbers:
                lines.append(f"\n**Cycle {cycle_num}** ({timestamp[:10]}): Clean pass — no issues found")
                continue

            lines.append(f"\n**Cycle {cycle_num}** ({timestamp[:10]}): {len(issue_numbers)} issue(s) created and closed")
            for num in issue_numbers:
                try:
                    result = subprocess.run(
                        ['gh', 'issue', 'view', str(num), '-R', repo,
                         '--json', 'title,state'],
                        capture_output=True, text=True, timeout=15
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        data = json.loads(result.stdout)
                        title = data.get('title', f'Issue #{num}')
                        state = data.get('state', 'unknown').lower()
                        lines.append(f"  - #{num} ({state}): {title}")
                    else:
                        lines.append(f"  - #{num}: (could not fetch title)")
                except Exception:
                    lines.append(f"  - #{num}")

        if not lines:
            return ""

        return "\n".join(lines)

    def _build_pr_review_prompt(self, pr_url: str, prior_cycle_context: str = "") -> str:
        """
        Build PR review prompt that encourages skill usage while enforcing parseable output structure.

        Pattern borrowed from work_breakdown_agent:
        - STEP 1 (PRIMARY): Do the work (use the skill)
        - STEP 2 (SECONDARY): Format the output (for automation)
        """
        # Extract PR number for checkout command
        match = re.search(r'/pull/(\d+)$', pr_url)
        pr_number = match.group(1) if match else None

        checkout_instruction = ""
        if pr_number:
            checkout_instruction = f"""
First, checkout the PR branch so review tools can analyze the changes:

```bash
gh pr checkout {pr_number}
```
"""

        prior_cycle_section = ""
        if prior_cycle_context:
            prior_cycle_section = f"""
## Prior Review Cycles

The following issues were found and closed in previous automated review cycles for this PR.
Do NOT re-report issues that were fixed in prior cycles unless you have concrete evidence
the fix was reverted or the issue exists at a different location.

For each issue you report, explicitly note in the description whether it is:
- **NEW**: First time this issue has been identified
- **REGRESSION**: Was previously fixed but has reappeared

{prior_cycle_context}

---
"""

        return f"""You are a PR Review Specialist reviewing PR: {pr_url}
{prior_cycle_section}
## STEP 1: Run Comprehensive Review

**REQUIRED**: Use the pr-review-toolkit skill to run specialized review agents.
{checkout_instruction}
Then run the comprehensive review:

/pr-review-toolkit:review-pr all

**IMPORTANT**: Do NOT use parallel mode or set `run_in_background: true` when invoking review agents.
Run the review agents sequentially, waiting for each to complete before proceeding.
This ensures you collect ALL review findings before compiling the final output.

The skill will launch specialized agents (code-reviewer, test-analyzer, silent-failure-hunter, comment-analyzer, type-design-analyzer) and provide detailed findings.

## STEP 2: Structure Results for Issue Creation

After the review skill completes, you MUST format the findings in this EXACT structure so they can be parsed and converted to GitHub issues:

```
## PR Review Findings

### Critical Issues
- **[Finding Title]**: [Description with file:line references] (NEW / REGRESSION)

### High Priority Issues
- **[Finding Title]**: [Description with file:line references] (NEW / REGRESSION)

### Medium Priority Issues
- **[Finding Title]**: [Description with file:line references] (NEW / REGRESSION)

### Low Priority / Nice-to-Have
- **[Finding Title]**: [Description with file:line references] (NEW / REGRESSION)

### Clean Areas
- [Areas that passed review with no issues]
```

**IMPORTANT FORMATTING RULES**:
1. Section must start with `## PR Review Findings`
2. Use exact heading names: "Critical Issues", "High Priority Issues", "Medium Priority Issues", "Low Priority / Nice-to-Have"
3. Each finding must use format: `- **[Title]**: [Description]`
4. If no issues at a severity level, write ONLY "None found" - no additional text
5. Include file:line references where applicable (e.g., `/workspace/file.ts:123`)
6. Tag each finding as (NEW) or (REGRESSION) if prior cycle history was provided above

This structured format enables automatic GitHub issue creation from your findings.
"""

    def _build_verification_prompt(
        self, pr_url: str, context_name: str, authority_key: str, context_content: str
    ) -> str:
        max_context_len = 15000
        if len(context_content) > max_context_len:
            context_content = context_content[:max_context_len] + "\n\n[... truncated ...]"

        # Dispatch on the explicit authority_key (not the display name) so the framing
        # is robust against future renames of the human-readable context_name.
        _authority_framings = {
            "idea_researcher": (
                "## Context Authority: Research Suggestions\n\n"
                "This context source represents **aspirational research and early ideation** — "
                "suggestions and possibilities explored during the research phase, NOT committed requirements.\n\n"
                "**Flag a gap ONLY IF** the Software Architect output or Parent Issue explicitly committed "
                "to implementing a specific feature from this source. "
                "Do NOT flag missing research suggestions, stretch goals, future enhancement ideas, "
                "or exploratory concepts as implementation gaps."
            ),
            "business_analyst": (
                "## Context Authority: Functional Requirements\n\n"
                "This context source represents **functional business requirements**. "
                "Flag gaps for items explicitly described as required, must-have, or core functionality. "
                "Skip items described as nice-to-have, future enhancements, or optional."
            ),
            "software_architect": (
                "## Context Authority: Committed Technical Specifications\n\n"
                "This context source represents **committed architectural decisions and technical specifications** — "
                "the agreed-upon technical contracts that must be implemented.\n\n"
                "Flag ALL gaps, deviations from specified patterns, and missing components. "
                "These specifications carry the highest authority."
            ),
        }
        authority_framing = _authority_framings.get(
            authority_key,
            # Default (parent_issue or unknown): treat as acceptance criteria
            "## Context Authority: Acceptance Criteria\n\n"
            "This context source is the **source of truth for acceptance criteria**. "
            "Every explicit requirement listed here must be implemented. "
            "Flag any missing or partially implemented requirements."
        )

        return f"""
You are a Requirements Verification Specialist. Your job is to verify that a PR's implementation
fully addresses the requirements from a specific context source.

## PR to Verify
{pr_url}

Review the PR diff to understand what was implemented.

{authority_framing}

## Context Source: {context_name}

The following is the original context that should be fully addressed by the PR:

---
{context_content}
---

## Your Task

1. Read the PR diff carefully
2. Compare against the context source above
3. Identify any requirements, specifications, or design decisions from the context that are:
   - NOT implemented in the PR (gap)
   - Partially implemented — missing aspects (gap)
   - Implemented differently than specified (deviation)

Apply the authority framing above when deciding what qualifies as a gap. Research suggestions
and aspirational ideas from the Idea Researcher are NOT gaps unless explicitly committed to.

## Output Format

Structure your findings EXACTLY like this:

```
## {context_name} Verification

### Gaps Found
- **[Gap Title]**: [What was specified vs what was implemented or missing]

### Deviations
- **[Deviation Title]**: [What was specified vs what was actually done]

### Verified
- [Requirements that were correctly implemented]
```

Under "### Gaps Found" and "### Deviations", write ONLY "None found" if there are none — no additional text.
If all requirements are met, write "All requirements verified - no gaps found" and list what was verified.
"""

    def _parse_review_findings(self, output: str, source: str) -> List[Dict[str, Any]]:
        """Parse review output into structured findings grouped by severity."""
        issues = []

        severity_sections = {
            'Critical': self._extract_section_items(output, 'Critical Issues'),
            'High': self._extract_section_items(output, 'High Priority Issues'),
            'Medium': self._extract_section_items(output, 'Medium Priority Issues'),
            'Low': self._extract_section_items(output, 'Low Priority / Nice-to-Have'),
        }

        gaps = self._extract_section_items(output, 'Gaps Found')
        deviations = self._extract_section_items(output, 'Deviations')

        for severity, items in severity_sections.items():
            if self._is_actionable_section(items, severity, source):
                issues.append({
                    'title': f"[PR Feedback] {severity} issues from {source}",
                    'body': self._format_issue_body(severity, items, source),
                    'severity': severity.lower(),
                })

        if self._is_actionable_section(gaps, "Gaps", source):
            issues.append({
                'title': f"[PR Feedback] Implementation gaps - {source}",
                'body': self._format_issue_body("Gap", gaps, source),
                'severity': 'high',
            })

        if self._is_actionable_section(deviations, "Deviations", source):
            issues.append({
                'title': f"[PR Feedback] Implementation deviations - {source}",
                'body': self._format_issue_body("Deviation", deviations, source),
                'severity': 'medium',
            })

        return issues

    def _extract_section_items(self, text: str, section_name: str) -> str:
        """Extract content under a ### section heading."""
        pattern = rf'###\s+{re.escape(section_name)}\s*\n(.*?)(?=\n###|\Z)'
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return ''

    def _is_none_found(self, content: str) -> bool:
        """Check if the section content indicates no findings."""
        return any(p.search(content) for p in _NONE_FOUND_PATTERNS)

    def _has_actionable_findings(self, content: str) -> bool:
        """Check if content contains at least one structured finding."""
        return any(pattern.search(content) for pattern in _ACTIONABLE_FINDING_PATTERNS)

    def _is_actionable_section(self, content: str, label: str, source: str) -> bool:
        """Determine whether a review section contains actionable findings."""
        if not content:
            return False
        if self._has_actionable_findings(content):
            if self._is_none_found(content):
                logger.info(
                    f"PR review {label} section from {source} contains both none-found "
                    f"language and structured findings — creating issue"
                )
            return True
        if not self._is_none_found(content):
            logger.warning(
                f"PR review false-positive prevented: {label} section from {source} "
                f"has content but no structured findings: {content[:200]}"
            )
        return False

    def _format_issue_body(self, severity: str, items: str, source: str) -> str:
        return (
            f"Based on feedback from the {source}, address the following issues:\n\n"
            f"## {severity} Findings\n\n"
            f"{items}\n\n"
            f"---\n"
            f"_Created by PR Review Stage_"
        )

    async def _create_review_issues(
        self,
        issue_specs: List[Dict[str, Any]],
        repo: str,
        github_config: Dict,
        parent_issue_number: int,
        project_name: str,
        obs: Optional[Any] = None,
        pipeline_run_id: Optional[str] = None,
        pr_url: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Create GitHub issues for review findings and link as sub-issues."""
        github_state = self.state_manager.load_project_state(project_name)
        if not github_state:
            logger.error(f"No GitHub state for {project_name}")
            return []

        # Find SDLC board
        sdlc_board = None
        for board_name, board in github_state.boards.items():
            if 'sdlc' in board_name.lower() or board_name == 'SDLC Execution':
                sdlc_board = board
                break

        if not sdlc_board:
            logger.error(f"SDLC board not found for {project_name}")
            return []

        # Find Backlog column
        backlog_column = None
        for column in sdlc_board.columns:
            if column.name.lower() == 'backlog':
                backlog_column = column
                break

        if not backlog_column:
            logger.error("Backlog column not found in SDLC board")
            return []

        # Get parent issue node ID
        parent_issue_id = None
        try:
            result = subprocess.run(
                ['gh', 'issue', 'view', str(parent_issue_number), '-R', repo, '--json', 'id'],
                capture_output=True, text=True, check=True, timeout=30
            )
            parent_issue_id = json.loads(result.stdout)['id']
        except Exception as e:
            logger.error(f"Failed to get parent issue ID: {e}", exc_info=True)

        created_issues = []

        for spec in issue_specs:
            try:
                # Create the issue
                result = subprocess.run(
                    ['gh', 'issue', 'create', '-R', repo,
                     '--title', spec['title'],
                     '--body', spec['body']],
                    capture_output=True, text=True, check=True, timeout=30
                )
                issue_url = result.stdout.strip()
                url_match = re.search(r'/issues/(\d+)$', issue_url)
                if not url_match:
                    logger.error(f"Could not extract issue number from URL: {issue_url}")
                    continue

                issue_number = url_match.group(1)

                # Get node ID
                view_result = subprocess.run(
                    ['gh', 'issue', 'view', issue_number, '-R', repo,
                     '--json', 'id,number,url'],
                    capture_output=True, text=True, check=True, timeout=30
                )
                issue_data = json.loads(view_result.stdout)
                issue_id = issue_data['id']

                # Add to SDLC board
                subprocess.run(
                    ['gh', 'project', 'item-add', str(sdlc_board.project_number),
                     '--owner', github_config['org'],
                     '--url', issue_url],
                    capture_output=True, text=True, check=True, timeout=30
                )

                # Set status to Backlog
                self._set_issue_status_on_board(
                    issue_number, repo, github_config, sdlc_board, backlog_column
                )

                # Link as sub-issue to parent
                if parent_issue_id:
                    self._link_sub_issue(parent_issue_id, issue_id, issue_number, parent_issue_number)

                    # Emit SUCCESS event
                    if obs:
                        decision_emitter = DecisionEventEmitter(obs)
                        current_review_cycle = pr_review_state_manager.get_review_count(
                            project_name, parent_issue_number
                        )

                        decision_emitter.emit_sub_issue_created(
                            project=project_name,
                            parent_issue=parent_issue_number,
                            issue_number=int(issue_number),
                            title=spec['title'],
                            board="SDLC Execution",
                            reason=f"PR review finding: {spec.get('severity', 'medium')} severity issue",
                            source="pr_review",
                            context_data={
                                'severity': spec.get('severity', 'medium'),
                                'source_phase': spec.get('source', 'unknown'),
                                'review_cycle': current_review_cycle,
                                'pr_url': pr_url
                            },
                            pipeline_run_id=pipeline_run_id
                        )

                created_issues.append({
                    'number': issue_number,
                    'url': issue_url,
                    'title': spec['title'],
                    'severity': spec.get('severity', 'medium'),
                    'body': spec.get('body', ''),
                })

                logger.info(f"Created review issue #{issue_number}: {spec['title']}")

            except Exception as e:
                logger.error(f"Failed to create review issue '{spec['title']}': {e}", exc_info=True)

                # Emit FAILURE event
                if obs:
                    decision_emitter = DecisionEventEmitter(obs)
                    current_review_cycle = pr_review_state_manager.get_review_count(
                        project_name, parent_issue_number
                    )

                    decision_emitter.emit_sub_issue_creation_failed(
                        project=project_name,
                        parent_issue=parent_issue_number,
                        title=spec['title'],
                        board="SDLC Execution",
                        error=e,
                        source="pr_review",
                        context_data={
                            'severity': spec.get('severity', 'medium'),
                            'source_phase': spec.get('source', 'unknown'),
                            'review_cycle': current_review_cycle,
                            'pr_url': pr_url
                        },
                        pipeline_run_id=pipeline_run_id
                    )
                # Continue with other issues

        # Two-pass: update each issue body with sibling context so fix agents can coordinate.
        # Only applies when multiple issues were created in this review cycle.
        if len(created_issues) > 1:
            sibling_header = (
                "\n\n---\n\n"
                "## Concurrent Fix Issues (Same Review Cycle)\n\n"
                "The following issues are being fixed simultaneously in this review cycle. "
                "Check these issues for any files they modify before making your own changes "
                "to avoid introducing conflicts:\n\n"
            )
            for issue_data in created_issues:
                sibling_lines = "\n".join(
                    f"- #{s['number']}: {s['title']}"
                    for s in created_issues
                    if s['number'] != issue_data['number']
                )
                new_body = issue_data['body'] + sibling_header + sibling_lines
                try:
                    subprocess.run(
                        ['gh', 'issue', 'edit', issue_data['number'], '-R', repo,
                         '--body', new_body],
                        capture_output=True, text=True, check=True, timeout=30
                    )
                    logger.info(f"Updated #{issue_data['number']} with sibling issue context")
                except Exception as e:
                    logger.error(
                        f"Failed to add sibling context to #{issue_data['number']}: {e}",
                        exc_info=True
                    )

        return created_issues

    def _set_issue_status_on_board(self, issue_number: str, repo: str,
                                    github_config: Dict, board, column):
        """Set an issue's status on a project board."""
        try:
            query = f'''{{
                repository(owner: "{github_config['org']}", name: "{github_config['repo']}") {{
                    issue(number: {issue_number}) {{
                        projectItems(first: 10) {{
                            nodes {{
                                id
                                project {{ number }}
                            }}
                        }}
                    }}
                }}
            }}'''

            result = subprocess.run(
                ['gh', 'api', 'graphql', '-f', f'query={query}'],
                capture_output=True, text=True, check=True, timeout=30
            )
            data = json.loads(result.stdout)
            items = data['data']['repository']['issue']['projectItems']['nodes']

            item_id = None
            for item in items:
                if item['project']['number'] == board.project_number:
                    item_id = item['id']
                    break

            if not item_id:
                logger.warning(f"Issue #{issue_number} not found on board (project #{board.project_number})")
                return
            if not board.status_field_id:
                logger.warning(f"No status_field_id for board, cannot set status for #{issue_number}")
                return
            if item_id and board.status_field_id:
                mutation = f'''
                mutation {{
                    updateProjectV2ItemFieldValue(
                        input: {{
                            projectId: "{board.project_id}"
                            itemId: "{item_id}"
                            fieldId: "{board.status_field_id}"
                            value: {{ singleSelectOptionId: "{column.id}" }}
                        }}
                    ) {{
                        projectV2Item {{ id }}
                    }}
                }}
                '''
                subprocess.run(
                    ['gh', 'api', 'graphql', '-f', f'query={mutation}'],
                    capture_output=True, text=True, check=True, timeout=30
                )
                logger.info(f"Set issue #{issue_number} to {column.name} on board")
        except Exception as e:
            logger.error(f"Failed to set status for issue #{issue_number}: {e}", exc_info=True)

    def _link_sub_issue(self, parent_issue_id: str, child_issue_id: str,
                        child_number: str, parent_number: int):
        """Link a child issue as sub-issue of parent."""
        try:
            mutation = f"""
            mutation {{
              addSubIssue(input: {{
                issueId: "{parent_issue_id}",
                subIssueId: "{child_issue_id}"
              }}) {{
                issue {{ title }}
                subIssue {{ title }}
              }}
            }}
            """
            subprocess.run(
                ['gh', 'api', 'graphql',
                 '-H', 'GraphQL-Features: sub_issues',
                 '-f', f'query={mutation}'],
                capture_output=True, text=True, check=True, timeout=30
            )
            logger.info(f"Linked #{child_number} as sub-issue of #{parent_number}")
        except Exception as e:
            logger.error(f"Failed to link #{child_number} as sub-issue: {e}", exc_info=True)

    async def _move_issues_to_development(
        self,
        issues: List[Dict[str, Any]],
        project_name: str,
        github_config: Dict
    ):
        """Move created review issues from Backlog to Development on SDLC board."""
        github_state = self.state_manager.load_project_state(project_name)
        if not github_state:
            logger.warning(f"No GitHub state for {project_name}, cannot move issues to Development")
            return

        sdlc_board = None
        for board_name, board in github_state.boards.items():
            if 'sdlc' in board_name.lower() or board_name == 'SDLC Execution':
                sdlc_board = board
                break

        if not sdlc_board:
            logger.warning(f"SDLC board not found for {project_name}, cannot move issues to Development")
            return

        dev_column = None
        for column in sdlc_board.columns:
            if column.name.lower() == 'development':
                dev_column = column
                break

        if not dev_column:
            logger.warning("Development column not found in SDLC board")
            return

        repo = f"{github_config['org']}/{github_config['repo']}"
        for issue in issues:
            self._set_issue_status_on_board(
                issue['number'], repo, github_config, sdlc_board, dev_column
            )

    def _advance_parent_to_documentation(self, project_name: str, parent_issue_number: int):
        """Advance the parent issue from 'In Review' to 'Documentation' on the Planning board."""
        try:
            from services.pipeline_progression import PipelineProgression
            from task_queue.task_manager import TaskQueue

            task_queue = TaskQueue()
            progression = PipelineProgression(task_queue)

            github_state = self.state_manager.load_project_state(project_name)
            if not github_state:
                logger.warning(f"No GitHub state for {project_name}, cannot advance to Documentation")
                return

            planning_board = None
            for board_name, board in github_state.boards.items():
                if 'planning' in board_name.lower():
                    planning_board = board_name
                    break

            if planning_board:
                success = progression.move_issue_to_column(
                    project_name, planning_board, parent_issue_number,
                    "Documentation", trigger='pr_review_clean_pass'
                )
                if success:
                    logger.info(f"Advanced #{parent_issue_number} to Documentation (clean pass)")
                else:
                    logger.error(f"Failed to advance #{parent_issue_number} to Documentation")
            else:
                logger.warning(f"Planning board not found for {project_name}, cannot advance to Documentation")
        except Exception as e:
            logger.error(f"Failed to advance parent to Documentation: {e}", exc_info=True)

    def _return_parent_to_development(self, project_name: str, parent_issue_number: int):
        """Move the parent issue from 'In Review' back to 'In Development' on the Planning board."""
        try:
            from services.pipeline_progression import PipelineProgression
            from task_queue.task_manager import TaskQueue

            task_queue = TaskQueue()
            progression = PipelineProgression(task_queue)

            github_state = self.state_manager.load_project_state(project_name)
            if not github_state:
                logger.warning(f"No GitHub state for {project_name}, cannot return parent to In Development")
                return

            planning_board = None
            for board_name, board in github_state.boards.items():
                if 'planning' in board_name.lower():
                    planning_board = board_name
                    break

            if planning_board:
                success = progression.move_issue_to_column(
                    project_name, planning_board, parent_issue_number,
                    "In Development", trigger='pr_review_issues_found'
                )
                if success:
                    logger.info(f"Returned #{parent_issue_number} to In Development (issues found)")
                else:
                    logger.error(f"Failed to return #{parent_issue_number} to In Development")
            else:
                logger.warning(f"Planning board not found for {project_name}, cannot return parent to In Development")
        except Exception as e:
            logger.error(f"Failed to return parent to In Development: {e}", exc_info=True)

    def _build_cycle_limit_comment(self, cycle: int, issues: List[Dict]) -> str:
        """Build a comment for when the cycle limit is reached."""
        issues_list = "\n".join(f"- #{i['number']}: {i['title']}" for i in issues)
        return (
            f"## PR Review - Cycle {cycle}/{MAX_REVIEW_CYCLES} (Final)\n\n"
            f"This is the final automated review cycle. The following issues were identified "
            f"and moved to Development:\n\n"
            f"{issues_list}\n\n"
            f"No further automated reviews will be triggered after these are resolved. "
            f"Manually move the parent issue to 'In Review' to reset the cycle count."
        )

    def _post_comment_on_issue(self, repo: str, issue_number: int, comment: str):
        """Post a comment on a GitHub issue."""
        try:
            subprocess.run(
                ['gh', 'issue', 'comment', str(issue_number), '-R', repo, '--body', comment],
                capture_output=True, text=True, check=True, timeout=30
            )
        except Exception as e:
            logger.error(f"Failed to post comment on #{issue_number}: {e}", exc_info=True)
