"""
PR Review Agent - Automated PR review and requirements verification

This agent runs after all sub-issues complete their SDLC pipelines.
It performs three phases:

Phase 1: PR Code Review
  - Uses /pr-review-toolkit:review-pr skill to review the accumulated PR
  - Creates GitHub issues grouped by severity

Phase 2: Context Verification (up to 4 separate Claude Code calls)
  - Verifies implementation against parent issue requirements
  - Verifies against idea researcher, business analyst, and architect outputs
  - Creates issues for any gaps found

Phase 3: CI Status Check
  - Checks CI check status via `gh pr checks`
  - Creates issues for any failing CI checks
  - Pending checks are noted but do not block a clean pass

Review cycle management:
  - Cycles 1-2: Create issues and move to Development for resolution
  - Cycle 3: Create issues but leave in Backlog, post summary on parent
  - Beyond cycle 3: Fail with NonRetryableAgentError (manual trigger resets count)
"""

from typing import Dict, Any, List, Optional
from agents.non_retryable import NonRetryableAgentError
from agents.base_analysis_agent import AnalysisAgent
from config.manager import ConfigManager
from config.state_manager import GitHubStateManager
from state_management.pr_review_state_manager import pr_review_state_manager
from claude.claude_integration import run_claude_code
import logging
import json
import re
import subprocess

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

# Structured finding pattern: bullet with bold title followed by colon.
# Matches the required output format: - **[Title]**: [Description]
_ACTIONABLE_FINDING_PATTERN = re.compile(r'^\s*[-*]\s+\*\*[^*]+\*\*\s*:', re.MULTILINE)


class PRReviewAgent(AnalysisAgent):
    """
    PR Review Agent for automated code review and requirements verification.

    Unlike standard maker agents, this agent:
    1. Makes multiple Claude Code calls (up to 5)
    2. Creates GitHub issues for findings
    3. Manages review cycle limits
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("pr_review_agent", agent_config=agent_config)
        self.config_manager = ConfigManager()
        self.state_manager = GitHubStateManager()

    @property
    def agent_display_name(self) -> str:
        return "PR Review Specialist"

    @property
    def agent_role_description(self) -> str:
        return """I perform automated PR code review and verify implementations against original requirements, creating actionable issues for any gaps found."""

    @property
    def output_sections(self) -> List[str]:
        return [
            "PR Code Review",
            "Requirements Verification",
            "CI Status",
            "Issues Created",
            "Review Summary"
        ]

    def get_initial_guidelines(self) -> str:
        return ""

    def get_quality_standards(self) -> str:
        return ""

    # ==================================================================================
    # MAIN EXECUTION - Custom multi-phase logic
    # ==================================================================================

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute PR review with multi-phase analysis and issue creation.

        Overrides the standard execute() entirely with custom logic.
        """
        task_context = context.get('context', {})
        project_name = task_context.get('project', 'unknown')
        issue_number = task_context.get('issue_number')

        # Resolve parent issue number
        parent_issue_number = self._resolve_parent_issue_number(task_context, project_name)
        if not parent_issue_number:
            logger.error("Could not determine parent issue number for PR review")
            context['markdown_analysis'] = "## PR Review Failed\n\nCould not determine parent issue number."
            return context

        logger.info(f"PR Review Agent executing for parent issue #{parent_issue_number} in {project_name}")

        # Manual triggers reset the cycle count so the review can proceed
        trigger_source = task_context.get('trigger_source', '')
        review_count = pr_review_state_manager.get_review_count(project_name, parent_issue_number)
        if trigger_source == 'manual' and review_count >= MAX_REVIEW_CYCLES:
            logger.info(
                f"Manual trigger detected for #{parent_issue_number} with review_count={review_count} — "
                f"resetting cycle count"
            )
            pr_review_state_manager.reset_review_count(project_name, parent_issue_number)
            review_count = 0

        # Check cycle limit
        if review_count >= MAX_REVIEW_CYCLES:
            msg = (
                f"Review cycle limit ({MAX_REVIEW_CYCLES}) reached for #{parent_issue_number}. "
                f"No further automated reviews will be performed. "
                f"Manually move the issue to 'In Review' to reset the cycle count and trigger a new review."
            )
            logger.warning(f"Review cycle limit reached for #{parent_issue_number}, failing")
            raise NonRetryableAgentError(msg)

        current_cycle = review_count + 1
        logger.info(f"Starting review cycle {current_cycle}/{MAX_REVIEW_CYCLES} for #{parent_issue_number}")

        # Get project config
        project_config = self.config_manager.get_project_config(project_name)
        github_config = project_config.github
        repo = f"{github_config['org']}/{github_config['repo']}"
        # Find the PR for this parent issue
        pr_url = await self._find_pr_url(github_config, parent_issue_number)
        if not pr_url:
            logger.warning(f"No PR found for parent issue #{parent_issue_number}")
            context['markdown_analysis'] = (
                f"## PR Review Skipped\n\n"
                f"No open PR found for parent issue #{parent_issue_number}."
            )
            return context

        # Load context from discussion (agent outputs)
        discussion_outputs = self._load_discussion_outputs(project_name, parent_issue_number)

        # Get parent issue body
        parent_issue_body = self._get_parent_issue_body(repo, parent_issue_number)

        # Prepare enhanced context for Claude Code calls
        enhanced_context = context.copy()
        if self.agent_config and 'agent_config' in self.agent_config:
            enhanced_context['agent_config'] = self.agent_config['agent_config']
        if self.agent_config and 'mcp_servers' in self.agent_config:
            enhanced_context['mcp_servers'] = self.agent_config['mcp_servers']

        all_created_issues = []
        review_summary_parts = []
        review_found_issues = False  # True if any phase found issues (before creation)
        phases_attempted = 0  # Phases that were attempted (not skipped)
        phases_completed = 0  # Phases that completed without exception

        # ---- Phase 1: PR Code Review ----
        logger.info(f"Phase 1: Running PR code review for {pr_url}")
        phases_attempted += 1
        try:
            pr_review_prompt = self._build_pr_review_prompt(pr_url)
            pr_review_result = await run_claude_code(pr_review_prompt, enhanced_context)

            # VALIDATE that the skill was actually invoked
            tools_used = pr_review_result.get('tools_used', []) if isinstance(pr_review_result, dict) else []
            skill_invoked = any(
                'pr-review-toolkit' in tool.get('name', '')
                for tool in tools_used
            )

            if not skill_invoked:
                # Skill was not invoked - this is a critical issue
                error_msg = (
                    f"PR review skill was NOT invoked for {pr_url}. "
                    f"Claude provided a direct response instead of using the skill."
                )
                logger.error(error_msg)

                # Post comment to GitHub explaining the issue
                # Wrap in try-except so GitHub API failures don't mask the validation error
                try:
                    from services.github_integration import GitHubIntegration
                    github = GitHubIntegration(
                        repo_owner=github_config['org'],
                        repo_name=github_config['repo']
                    )
                    await github.post_comment(
                        parent_issue_number,
                        f"⚠️ **PR Review Incomplete**\n\n"
                        f"The PR review skill was not invoked during automated review. "
                        f"This may indicate:\n"
                        f"- The PR URL is inaccessible to the review tool\n"
                        f"- Claude chose to provide a direct response instead\n"
                        f"- The skill configuration needs attention\n\n"
                        f"**Action Required**: Manual PR review recommended."
                    )
                except Exception as comment_error:
                    logger.warning(
                        f"Failed to post skill validation comment to GitHub: {comment_error}. "
                        f"Continuing with validation error."
                    )

                # Raise exception to mark phase as failed (triggers inconclusive path)
                raise Exception(
                    f"PR review skill not invoked - cannot validate PR code quality"
                )

            # Continue with normal processing...
            pr_review_text = pr_review_result.get('result', '') if isinstance(pr_review_result, dict) else str(pr_review_result)
            review_issues = self._parse_review_findings(pr_review_text, "PR Code Review")
            review_summary_parts.append(f"### PR Code Review\n\n{pr_review_text}")
            phases_completed += 1
            if review_issues:
                review_found_issues = True

            if review_issues:
                created = await self._create_review_issues(
                    review_issues, repo, github_config, parent_issue_number, project_name
                )
                all_created_issues.extend(created)
                logger.info(f"Phase 1: Created {len(created)} issues from PR review")
            else:
                logger.info("Phase 1: No issues found in PR review")
        except Exception as e:
            logger.error(f"Phase 1 PR review failed: {e}", exc_info=True)
            review_summary_parts.append(f"### PR Code Review\n\nFailed: {e}")

        # ---- Phase 2: Context Verification ----
        context_checks = [
            ("Parent Issue Requirements", parent_issue_body),
            ("Idea Researcher Output", discussion_outputs.get('idea_researcher')),
            ("Business Analyst Output", discussion_outputs.get('business_analyst')),
            ("Software Architect Output", discussion_outputs.get('software_architect')),
        ]

        for check_name, check_content in context_checks:
            if not check_content:
                logger.info(f"Skipping {check_name} verification (no content)")
                continue

            logger.info(f"Phase 2: Verifying against {check_name}")
            phases_attempted += 1
            try:
                verification_prompt = self._build_verification_prompt(
                    pr_url, check_name, check_content
                )
                verification_result = await run_claude_code(verification_prompt, enhanced_context)

                verification_text = verification_result.get('result', '') if isinstance(verification_result, dict) else str(verification_result)
                gap_issues = self._parse_review_findings(verification_text, check_name)
                review_summary_parts.append(f"### {check_name} Verification\n\n{verification_text}")
                phases_completed += 1
                if gap_issues:
                    review_found_issues = True

                if gap_issues:
                    created = await self._create_review_issues(
                        gap_issues, repo, github_config, parent_issue_number, project_name
                    )
                    all_created_issues.extend(created)
                    logger.info(f"Created {len(created)} issues from {check_name} verification")
                else:
                    logger.info(f"No gaps found in {check_name} verification")
            except Exception as e:
                logger.error(f"{check_name} verification failed: {e}", exc_info=True)
                review_summary_parts.append(f"### {check_name} Verification\n\nFailed: {e}")

        # ---- Phase 3: CI Status Check ----
        logger.info(f"Phase 3: Checking CI status for {pr_url}")
        phases_attempted += 1
        try:
            failures, pending = self._check_ci_status(pr_url, repo)
            phases_completed += 1

            if failures:
                review_found_issues = True
                ci_issue_spec = self._build_ci_failure_issue(failures, pr_url)
                created = await self._create_review_issues(
                    [ci_issue_spec], repo, github_config, parent_issue_number, project_name
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
                    f"### CI Status\n\n{len(pending)} check(s) still pending (not treated as failure):\n\n"
                    + self._format_ci_table(pending)
                )
            else:
                logger.info("Phase 3: All CI checks passed")
                review_summary_parts.append("### CI Status\n\nAll CI checks passed.")
        except Exception as e:
            logger.error(f"Phase 3 CI status check failed: {e}", exc_info=True)
            review_summary_parts.append(f"### CI Status\n\nFailed: {e}")

        # ---- Post-review actions ----
        created_issue_numbers = [int(i['number']) for i in all_created_issues]

        if phases_completed == 0:
            # ALL phases threw exceptions — inconclusive, do NOT advance
            logger.error(
                f"All review phases failed for #{parent_issue_number}. "
                f"Leaving issue in current column (no advancement)."
            )
            pr_review_state_manager.increment_review_count(
                project_name, parent_issue_number, []
            )
        elif phases_completed < phases_attempted and not review_found_issues:
            # Some phases failed, surviving phases found nothing — inconclusive.
            # Don't advance to Documentation since the failed phases may have
            # caught issues we couldn't see.
            logger.warning(
                f"Only {phases_completed}/{phases_attempted} review phases completed "
                f"for #{parent_issue_number}. Treating as inconclusive (no advancement)."
            )
            pr_review_state_manager.increment_review_count(
                project_name, parent_issue_number, []
            )
        elif review_found_issues:
            # Found issues (regardless of whether GitHub issue creation succeeded)
            pr_review_state_manager.increment_review_count(
                project_name, parent_issue_number, created_issue_numbers
            )

            if not all_created_issues:
                logger.warning(
                    f"Review found issues for #{parent_issue_number} but failed to create "
                    f"GitHub issues. Returning parent to In Development anyway."
                )

            if current_cycle < MAX_REVIEW_CYCLES:
                if all_created_issues:
                    await self._move_issues_to_development(
                        all_created_issues, project_name, github_config
                    )
                    logger.info(f"Moved {len(all_created_issues)} review issues to Development")
                self._return_parent_to_development(project_name, parent_issue_number)
            else:
                if all_created_issues:
                    summary_comment = self._build_cycle_limit_comment(
                        current_cycle, all_created_issues
                    )
                    self._post_comment_on_issue(repo, parent_issue_number, summary_comment)
                logger.info(f"Cycle {current_cycle} (limit): left issues in Backlog, posted summary")
        else:
            # Clean pass — all completed phases found nothing
            logger.info(f"Clean pass for #{parent_issue_number}, advancing to Documentation")
            pr_review_state_manager.increment_review_count(
                project_name, parent_issue_number, []
            )
            self._advance_parent_to_documentation(project_name, parent_issue_number)

        # Build final summary for GitHub comment
        issues_summary = ""
        if all_created_issues:
            issues_list = "\n".join(
                f"- #{i['number']}: {i['title']}" for i in all_created_issues
            )
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
        return context

    # ==================================================================================
    # PARENT ISSUE RESOLUTION
    # ==================================================================================

    def _resolve_parent_issue_number(self, task_context: Dict[str, Any], project_name: str) -> Optional[int]:
        """Resolve the parent issue number from task context."""
        # Direct issue number
        issue_number = task_context.get('issue_number')
        if issue_number:
            return int(issue_number)

        # From nested task_context
        nested = task_context.get('task_context', {})
        if nested and nested.get('issue_number'):
            return int(nested['issue_number'])

        return None

    # ==================================================================================
    # PR URL LOOKUP
    # ==================================================================================

    async def _find_pr_url(self, github_config: Dict, parent_issue_number: int) -> Optional[str]:
        """Find the PR URL for a parent issue using feature branch manager."""
        try:
            from services.feature_branch_manager import feature_branch_manager
            from services.github_integration import GitHubIntegration

            github = GitHubIntegration(
                repo_owner=github_config['org'],
                repo_name=github_config['repo']
            )

            # Try to find feature branch state
            # Need to iterate possible project names since github_config doesn't directly have it
            feature_branch = None
            for proj_name in self.state_manager.list_managed_projects():
                fb = feature_branch_manager.get_feature_branch_state(proj_name, parent_issue_number)
                if fb:
                    feature_branch = fb
                    break

            if not feature_branch:
                logger.warning(f"No feature branch found for #{parent_issue_number}")
                return None

            pr_data = await github.find_pr_by_branch(feature_branch.branch_name)
            if pr_data:
                pr_number = pr_data.get('pr_number')
                return f"https://github.com/{github_config['org']}/{github_config['repo']}/pull/{pr_number}"

            return None
        except Exception as e:
            logger.error(f"Failed to find PR for #{parent_issue_number}: {e}", exc_info=True)
            return None

    # ==================================================================================
    # DISCUSSION OUTPUT LOADING
    # ==================================================================================

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

            # Agent signature pattern: _Processed by the X agent_
            # Signatures use underscore-separated names (e.g., "idea_researcher")
            agent_keys = ['idea_researcher', 'business_analyst', 'software_architect']

            all_bodies = []
            for comment in comments:
                all_bodies.append(comment.get('body', ''))
                for reply in comment.get('replies', {}).get('nodes', []):
                    all_bodies.append(reply.get('body', ''))

            for body in all_bodies:
                for key in agent_keys:
                    if f'_Processed by the {key} agent_' in body:
                        # Keep the most recent output for each agent
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

    # ==================================================================================
    # CI STATUS CHECK
    # ==================================================================================

    def _check_ci_status(self, pr_url: str, repo: str) -> tuple:
        """Check CI check status for a PR.

        Returns (failures, pending) where each is a list of check dicts
        with keys: name, state, bucket, description, link.

        Exit codes from `gh pr checks`:
          0 = all checks passed
          1 = one or more checks failed
          8 = checks still pending (no failures)
        """
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
            f"_Created by PR Review Agent_"
        )
        return {
            'title': '[PR Review] CI check failures',
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

    # ==================================================================================
    # PROMPT BUILDERS
    # ==================================================================================

    def _build_pr_review_prompt(self, pr_url: str) -> str:
        return f"""
You are a PR Review Specialist. Review the following pull request for code quality issues.

**CRITICAL**: You MUST use the /pr-review-toolkit:review-pr skill for this task.
DO NOT provide a manual review - the skill provides comprehensive automated analysis.

Use the /pr-review-toolkit:review-pr skill to review this PR: {pr_url}

After running the review skill, organize your findings into severity levels.

## Output Format

Structure your findings EXACTLY like this:

```
## PR Review Findings

### Critical Issues
- **[Finding Title]**: [Description of the critical issue and what needs to change]

### High Priority Issues
- **[Finding Title]**: [Description]

### Medium Priority Issues
- **[Finding Title]**: [Description]

### Low Priority / Nice-to-Have
- **[Finding Title]**: [Description]

### Clean Areas
- [Areas that passed review with no issues]
```

If there are NO issues at any severity level, write ONLY "None found" under that heading — do not add any explanation, context, or commentary.
If the PR looks good overall, state that clearly in a separate summary paragraph.
"""

    def _build_verification_prompt(self, pr_url: str, context_name: str, context_content: str) -> str:
        # Truncate very long context to avoid prompt bloat
        max_context_len = 15000
        if len(context_content) > max_context_len:
            context_content = context_content[:max_context_len] + "\n\n[... truncated ...]"

        return f"""
You are a Requirements Verification Specialist. Your job is to verify that a PR's implementation
fully addresses the requirements from a specific context source.

## PR to Verify
{pr_url}

Review the PR diff to understand what was implemented.

## Context Source: {context_name}

The following is the original context that should be fully addressed by the PR:

---
{context_content}
---

## Your Task

1. Read the PR diff carefully
2. Compare against the context source above
3. Identify any requirements, specifications, or design decisions from the context that are:
   - NOT implemented in the PR
   - Partially implemented (missing aspects)
   - Implemented differently than specified (potential deviation)

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

    # ==================================================================================
    # FINDING PARSING
    # ==================================================================================

    def _parse_review_findings(self, output: str, source: str) -> List[Dict[str, Any]]:
        """
        Parse review output into structured findings grouped by severity.

        Returns a list of issue specs, one per severity level that has findings.
        """
        issues = []

        # Parse severity sections
        severity_sections = {
            'Critical': self._extract_section_items(output, 'Critical Issues'),
            'High': self._extract_section_items(output, 'High Priority Issues'),
            'Medium': self._extract_section_items(output, 'Medium Priority Issues'),
            'Low': self._extract_section_items(output, 'Low Priority / Nice-to-Have'),
        }

        # Parse gap/deviation sections (from context verification)
        gaps = self._extract_section_items(output, 'Gaps Found')
        deviations = self._extract_section_items(output, 'Deviations')

        # Create one issue per severity with findings
        for severity, items in severity_sections.items():
            if self._is_actionable_section(items, severity, source):
                issues.append({
                    'title': f"[PR Review] {severity} issues from {source}",
                    'body': self._format_issue_body(severity, items, source),
                    'severity': severity.lower(),
                })

        # Create issues for gaps and deviations
        if self._is_actionable_section(gaps, "Gaps", source):
            issues.append({
                'title': f"[PR Review] Implementation gaps - {source}",
                'body': self._format_issue_body("Gap", gaps, source),
                'severity': 'high',
            })

        if self._is_actionable_section(deviations, "Deviations", source):
            issues.append({
                'title': f"[PR Review] Implementation deviations - {source}",
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
        """Check if the section content indicates no findings.

        Uses regex patterns to detect "none found" variants even when Claude
        appends explanatory text (e.g., 'None found - issues were already resolved').
        """
        return any(p.search(content) for p in _NONE_FOUND_PATTERNS)

    def _has_actionable_findings(self, content: str) -> bool:
        """Check if content contains at least one structured finding.

        Validates that the section has bullet points in the expected
        '- **[Title]**: [Description]' format. This acts as defense-in-depth
        against false positives when _is_none_found() misses a variant.
        """
        return bool(_ACTIONABLE_FINDING_PATTERN.search(content))

    def _is_actionable_section(self, content: str, label: str, source: str) -> bool:
        """Determine whether a review section contains actionable findings.

        Checks _has_actionable_findings() first so that structured findings
        are never silently suppressed, even if the text also contains
        'none found'-like phrases (e.g., describing previously resolved issues).
        """
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
            f"## {severity} Findings\n\n"
            f"**Source**: {source}\n\n"
            f"{items}\n\n"
            f"---\n"
            f"_Created by PR Review Agent_"
        )

    # ==================================================================================
    # ISSUE CREATION - similar pattern to work_breakdown_agent
    # ==================================================================================

    async def _create_review_issues(
        self,
        issue_specs: List[Dict[str, Any]],
        repo: str,
        github_config: Dict,
        parent_issue_number: int,
        project_name: str
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

                created_issues.append({
                    'number': issue_number,
                    'url': issue_url,
                    'title': spec['title'],
                    'severity': spec.get('severity', 'medium'),
                })

                logger.info(f"Created review issue #{issue_number}: {spec['title']}")

            except Exception as e:
                logger.error(f"Failed to create review issue '{spec['title']}': {e}", exc_info=True)

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

    # ==================================================================================
    # POST-REVIEW ACTIONS
    # ==================================================================================

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

            # Find the Planning board name
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
        """Move the parent issue from 'In Review' back to 'In Development' on the Planning board.

        Called when PR review finds issues (cycles 1-2). This enables the natural
        re-review cycle: child fix-issues complete → all_subtasks_completed automation
        moves parent back to 'In Review' → project monitor triggers PR review agent.
        """
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
            f"but will remain in Backlog for manual triage:\n\n"
            f"{issues_list}\n\n"
            f"Further review and resolution should be handled manually."
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
