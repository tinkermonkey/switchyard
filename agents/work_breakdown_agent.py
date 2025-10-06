"""
Work Breakdown Agent - Break approved designs into phase-based sub-issues

This agent takes the outputs from business_analyst, software_architect, and test_planner
and creates a set of sub-issues that capture the work needed for implementation.

Key responsibilities:
- Parse requirements, design (including phases), and test planning from previous stages
- Create logical work segments (phases) for developer/QA execution
- Generate sub-issues with clear acceptance criteria
- Place sub-issues in SDLC board's Backlog in dependency order
- Handle conversational adjustments (reorder, update, add/remove issues)
"""

from typing import Dict, Any, List, Optional
from agents.base_maker_agent import MakerAgent
from config.manager import ConfigManager
from config.state_manager import GitHubStateManager
import logging
import json
import re

logger = logging.getLogger(__name__)


class WorkBreakdownAgent(MakerAgent):
    """
    Work Breakdown Agent for decomposing epics into phase-based sub-issues.

    Unlike other maker agents that only post to discussions, this agent:
    1. Creates GitHub sub-issues in the SDLC board
    2. Posts a summary to the discussion
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("work_breakdown_agent", agent_config=agent_config)
        self.config_manager = ConfigManager()
        self.state_manager = GitHubStateManager()

    # ==================================================================================
    # REQUIRED PROPERTIES - Define this agent's identity
    # ==================================================================================

    @property
    def agent_display_name(self) -> str:
        return "Work Breakdown Specialist"

    @property
    def agent_role_description(self) -> str:
        return """I decompose approved designs into phase-based sub-issues for implementation, ensuring each issue has clear requirements, design guidance, and acceptance criteria."""

    @property
    def output_sections(self) -> List[str]:
        return [
            "Work Breakdown Summary",
            "Sub-Issues Created",
            "Dependencies",
            "Next Steps"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS - Agent-specific guidelines
    # ==================================================================================

    def get_initial_guidelines(self) -> str:
        return """
## Important Guidelines

- Break work into logical phases based on the architecture design
- Each sub-issue should be a cohesive unit of work for a developer or QA engineer
- Include specific requirements, design guidance, and acceptance criteria in each sub-issue
- Order sub-issues by dependencies (earlier phases first)
- Keep phase titles concise: "Phase 1: Infrastructure setup"
- Do NOT include effort estimates or timeline predictions
- Focus on WHAT needs to be done in each phase, not HOW long it will take
"""

    def get_quality_standards(self) -> str:
        return """
- Each sub-issue has clear, testable acceptance criteria
- Dependencies between sub-issues are explicitly stated
- Requirements trace back to the original business requirements
- Design guidance references specific sections of the architecture
- Test criteria come from the test plan
"""

    # ==================================================================================
    # ENHANCED EXECUTION - Create sub-issues in addition to discussion post
    # ==================================================================================

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute work breakdown with sub-issue creation.

        This overrides the base execute() to add sub-issue creation functionality
        while maintaining the standard discussion posting behavior.
        """
        # First, run the standard maker agent execution (creates discussion post)
        context = await super().execute(context)

        # Extract task context
        task_context = context.get('context', {})

        # Determine execution mode
        mode = self._determine_execution_mode(task_context)

        # Only create/manage sub-issues in initial mode or when explicitly requested
        if mode == 'initial' or self._should_manage_sub_issues(task_context):
            try:
                # Get project configuration
                project_name = task_context.get('project', 'unknown')

                # Parse the markdown analysis to extract sub-issue specifications
                markdown_output = context.get('markdown_analysis', '')
                sub_issues = self._parse_sub_issues_from_output(markdown_output)

                if sub_issues:
                    # Create sub-issues in GitHub
                    created_issues = await self._create_sub_issues(
                        sub_issues,
                        task_context,
                        project_name
                    )

                    # Store created issue info in context
                    context['created_sub_issues'] = created_issues
                    logger.info(f"Created {len(created_issues)} sub-issues for {project_name}")
                else:
                    logger.warning("No sub-issues parsed from agent output")

            except Exception as e:
                logger.error(f"Failed to create sub-issues: {e}", exc_info=True)
                # Don't fail the entire task - the discussion post is still valuable
                context['sub_issue_creation_error'] = str(e)

        return context

    def _should_manage_sub_issues(self, task_context: Dict[str, Any]) -> bool:
        """Determine if this execution should manage sub-issues"""
        # Check for conversational mode with sub-issue management keywords
        if task_context.get('trigger') == 'feedback_loop':
            feedback_text = task_context.get('feedback', {}).get('formatted_text', '').lower()
            keywords = ['reorder', 'add issue', 'remove issue', 'update issue',
                       'change phase', 'split', 'merge']
            return any(keyword in feedback_text for keyword in keywords)
        return False

    # ==================================================================================
    # OVERRIDE: Enhanced prompts with sub-issue formatting instructions
    # ==================================================================================

    def _build_initial_prompt(self, task_context: Dict[str, Any]) -> str:
        """Build prompt for initial work breakdown with sub-issue creation instructions"""
        base_prompt = super()._build_initial_prompt(task_context)

        # Get workspace info
        project = task_context.get('project', 'unknown')
        workspace_type = task_context.get('workspace_type', 'issues')
        issue_number = task_context.get('issue_number', 'unknown')
        discussion_id = task_context.get('discussion_id')  # Internal GitHub discussion ID

        # Get project config to find repo URL
        try:
            project_config = self.config_manager.get_project_config(project)
            github_config = project_config.github
            repo_url = f"https://github.com/{github_config['org']}/{github_config['repo']}"
        except Exception as e:
            logger.warning(f"Could not get project config: {e}")
            repo_url = "https://github.com/unknown/unknown"

        # Determine discussion number and parent issue number
        discussion_number = 'unknown'
        parent_issue_number = 'unknown'
        discussion_url = "[discussion link]"
        parent_issue_url = "[parent issue link]"

        if workspace_type == 'discussions':
            # We're working on a discussion
            # issue_number in this context is actually the discussion number
            discussion_number = issue_number
            discussion_url = f"{repo_url}/discussions/{discussion_number}"

            # Look up parent issue from state using the internal discussion_id
            try:
                if not discussion_id:
                    logger.warning(f"No discussion_id in task_context for discussion #{discussion_number}")
                    # Fallback: try to look it up from issue number
                    github_state = self.state_manager.load_project_state(project)
                    if github_state and github_state.issue_discussion_links:
                        # This is backwards - we need to find the issue that has this discussion
                        for issue_num, disc_id in github_state.issue_discussion_links.items():
                            # Get the discussion details to match the number
                            # This is inefficient but a fallback
                            logger.warning(f"Attempting fallback lookup for discussion #{discussion_number}")
                            discussion_id = disc_id  # Might not work, but trying
                            break

                if discussion_id:
                    github_state = self.state_manager.load_project_state(project)
                    if github_state and github_state.discussion_issue_links:
                        parent_issue_number = github_state.discussion_issue_links.get(discussion_id)
                        if parent_issue_number:
                            parent_issue_url = f"{repo_url}/issues/{parent_issue_number}"
                            logger.info(f"Found parent issue #{parent_issue_number} for discussion #{discussion_number} (ID: {discussion_id})")
                        else:
                            logger.warning(f"No parent issue found for discussion_id {discussion_id}")
                else:
                    logger.error(f"Could not determine discussion_id for discussion #{discussion_number}")
            except Exception as e:
                logger.error(f"Error looking up parent issue: {e}", exc_info=True)
        else:
            # We're working on an issue directly
            parent_issue_number = issue_number
            parent_issue_url = f"{repo_url}/issues/{issue_number}"

        # Add sub-issue formatting instructions
        sub_issue_instructions = f"""

## CRITICAL: Sub-Issue Creation Format

After your analysis, you MUST include a structured section for sub-issues to create.

Format your sub-issues section EXACTLY like this:

```
## Sub-Issues to Create

### Phase 1: [Phase Title]

**Title**: Phase 1: [Concise description]

**Description**:
Brief overview of this phase's goals.

**Requirements**:
- [Specific requirement from business analyst]
- [Another requirement]

**Design Guidance**:
- [Specific architecture/design direction]
- [Technical constraints or patterns to follow]

**Acceptance Criteria**:
- [ ] [Testable criterion from test plan]
- [ ] [Another criterion]
- [ ] [Code is reviewed and approved]

**Dependencies**: None (or list phase numbers: "Phase 2, Phase 3")

**Parent Issue**: #{parent_issue_number}

**Discussion**: This work is detailed in discussion [{discussion_number}]({discussion_url})

---

### Phase 2: [Next Phase Title]
[... same structure ...]
```

Each sub-issue will be created as a sub-task of issue #{parent_issue_number} and placed in the SDLC board's Backlog column, ordered by dependency.

Make sure to:
1. Extract phases from the software architect's design (or create logical phases if not explicit)
2. Pull specific requirements from the business analyst's work
3. Pull test criteria from the test planner's output
4. Order phases by dependencies (foundational work first)
5. Keep titles concise and descriptive
"""

        return base_prompt + sub_issue_instructions

    # ==================================================================================
    # SUB-ISSUE PARSING AND CREATION
    # ==================================================================================

    def _parse_sub_issues_from_output(self, markdown: str) -> List[Dict[str, Any]]:
        """
        Parse sub-issue specifications from the agent's markdown output.

        Looks for the "Sub-Issues to Create" section and extracts structured data.
        """
        sub_issues = []

        # Find the sub-issues section
        section_match = re.search(
            r'## Sub-Issues to Create\s*\n(.*?)(?=\n## |\Z)',
            markdown,
            re.DOTALL | re.IGNORECASE
        )

        if not section_match:
            logger.warning("No 'Sub-Issues to Create' section found in output")
            return sub_issues

        section_content = section_match.group(1)

        # Split by phase markers (### Phase N:)
        phase_pattern = r'### (Phase \d+: .+?)\n(.*?)(?=\n### Phase \d+:|\Z)'
        phase_matches = re.finditer(phase_pattern, section_content, re.DOTALL)

        for phase_match in phase_matches:
            phase_title = phase_match.group(1).strip()
            phase_content = phase_match.group(2).strip()

            # Extract fields
            title = self._extract_field(phase_content, 'Title', default=phase_title)
            description = self._extract_field(phase_content, 'Description')
            requirements = self._extract_list_field(phase_content, 'Requirements')
            design_guidance = self._extract_list_field(phase_content, 'Design Guidance')
            acceptance_criteria = self._extract_checklist_field(phase_content, 'Acceptance Criteria')
            dependencies = self._extract_field(phase_content, 'Dependencies', default='None')
            parent_issue = self._extract_field(phase_content, 'Parent Issue')
            discussion_link = self._extract_field(phase_content, 'Discussion')

            # Build sub-issue body
            body_parts = []

            if description:
                body_parts.append(description)
                body_parts.append("")

            if requirements:
                body_parts.append("## Requirements")
                for req in requirements:
                    body_parts.append(f"- {req}")
                body_parts.append("")

            if design_guidance:
                body_parts.append("## Design Guidance")
                for guide in design_guidance:
                    body_parts.append(f"- {guide}")
                body_parts.append("")

            if acceptance_criteria:
                body_parts.append("## Acceptance Criteria")
                for criterion in acceptance_criteria:
                    body_parts.append(f"- [ ] {criterion}")
                body_parts.append("")

            if dependencies and dependencies.lower() != 'none':
                body_parts.append(f"## Dependencies")
                body_parts.append(f"{dependencies}")
                body_parts.append("")

            if parent_issue:
                body_parts.append(f"## Parent Issue")
                body_parts.append(f"Part of {parent_issue}")
                body_parts.append("")

            if discussion_link:
                body_parts.append(f"## Discussion")
                body_parts.append(discussion_link)

            sub_issues.append({
                'title': title,
                'body': '\n'.join(body_parts),
                'dependencies': dependencies,
                'parent_issue': parent_issue,
                'phase': phase_title
            })

        logger.info(f"Parsed {len(sub_issues)} sub-issues from output")
        return sub_issues

    def _extract_field(self, content: str, field_name: str, default: str = '') -> str:
        """Extract a single field value from markdown content"""
        pattern = rf'\*\*{re.escape(field_name)}\*\*:\s*(.+?)(?=\n\*\*|\n\n|\Z)'
        match = re.search(pattern, content, re.DOTALL)
        if match:
            return match.group(1).strip()
        return default

    def _extract_list_field(self, content: str, field_name: str) -> List[str]:
        """Extract a bulleted list field from markdown content"""
        # Find the field header
        pattern = rf'\*\*{re.escape(field_name)}\*\*:\s*\n((?:- .+?\n)+)'
        match = re.search(pattern, content, re.DOTALL)
        if match:
            list_content = match.group(1)
            items = re.findall(r'- (.+)', list_content)
            return [item.strip() for item in items]
        return []

    def _extract_checklist_field(self, content: str, field_name: str) -> List[str]:
        """Extract a checklist field from markdown content"""
        # Find the field header
        pattern = rf'\*\*{re.escape(field_name)}\*\*:\s*\n((?:- \[ \] .+?\n)+)'
        match = re.search(pattern, content, re.DOTALL)
        if match:
            list_content = match.group(1)
            items = re.findall(r'- \[ \] (.+)', list_content)
            return [item.strip() for item in items]
        return []

    async def _create_sub_issues(
        self,
        sub_issues: List[Dict[str, Any]],
        task_context: Dict[str, Any],
        project_name: str
    ) -> List[Dict[str, Any]]:
        """
        Create GitHub sub-issues in the SDLC board's Backlog column.

        Returns list of created issue metadata (number, url, etc.)
        """
        from claude.claude_integration import run_claude_code

        # Get project configuration
        project_config = self.config_manager.get_project_config(project_name)
        github_config = project_config.github

        # Get GitHub state to find SDLC board
        github_state = self.state_manager.load_project_state(project_name)
        if not github_state:
            raise Exception(f"No GitHub state found for project {project_name}")

        # Find SDLC board (look for "SDLC Execution" board)
        sdlc_board = None
        for board_name, board in github_state.boards.items():
            if 'sdlc' in board_name.lower() or board_name == 'SDLC Execution':
                sdlc_board = board
                break

        if not sdlc_board:
            raise Exception(f"SDLC board not found for project {project_name}")

        # Find Backlog column
        backlog_column = None
        for column in sdlc_board.columns:
            if column.name.lower() == 'backlog':
                backlog_column = column
                break

        if not backlog_column:
            raise Exception(f"Backlog column not found in SDLC board")

        logger.info(f"Creating sub-issues in project {sdlc_board.project_number}, column {backlog_column.name}")

        # Extract parent issue number (should be same for all sub-issues)
        parent_issue_number = None
        if sub_issues and 'parent_issue' in sub_issues[0]:
            parent_issue_str = sub_issues[0]['parent_issue']
            # Extract number from "#123" format
            import re as regex
            match = regex.search(r'#(\d+)', parent_issue_str)
            if match:
                parent_issue_number = match.group(1)
                logger.info(f"Parent issue: #{parent_issue_number}")

        # Create each sub-issue
        created_issues = []
        for idx, sub_issue in enumerate(sub_issues, start=1):
            try:
                # Create issue using GitHub CLI
                create_prompt = f"""
Create a GitHub issue with the following details:

Repository: {github_config['org']}/{github_config['repo']}
Title: {sub_issue['title']}

Body:
{sub_issue['body']}

Use the `gh issue create` command to create this issue.
After creating the issue, add it to project {sdlc_board.project_number} in the Backlog column using `gh project item-add`.

Return ONLY the issue number and URL in this format:
Issue #123: https://github.com/org/repo/issues/123
"""

                # Execute with Claude Code SDK to create the issue
                result = await run_claude_code(create_prompt, {})

                # Parse the result to extract issue number and URL
                issue_match = re.search(r'Issue #(\d+):\s*(https://\S+)', result)
                if issue_match:
                    issue_number = issue_match.group(1)
                    issue_url = issue_match.group(2)

                    created_issues.append({
                        'number': issue_number,
                        'url': issue_url,
                        'title': sub_issue['title'],
                        'phase': sub_issue['phase']
                    })

                    logger.info(f"Created sub-issue #{issue_number}: {sub_issue['title']}")
                else:
                    logger.warning(f"Could not parse issue creation result: {result}")

            except Exception as e:
                logger.error(f"Failed to create sub-issue '{sub_issue['title']}': {e}")
                # Continue with other issues

        # Update parent issue with task list of all sub-issues
        if parent_issue_number and created_issues:
            try:
                task_list = "\n".join([
                    f"- [ ] #{issue['number']} {issue['title']}"
                    for issue in created_issues
                ])

                update_prompt = f"""
Update GitHub issue #{parent_issue_number} in repository {github_config['org']}/{github_config['repo']}.

Add the following task list to the issue body (append to existing content):

## Implementation Tasks

{task_list}

Use `gh issue edit #{parent_issue_number} --body-file -` with a heredoc or temp file to preserve existing content and append the task list.

Return "Updated issue #{parent_issue_number} with task list" when done.
"""
                result = await run_claude_code(update_prompt, {})
                logger.info(f"Updated parent issue #{parent_issue_number} with {len(created_issues)} sub-tasks")

            except Exception as e:
                logger.error(f"Failed to update parent issue #{parent_issue_number}: {e}")
                # Don't fail the whole operation if parent update fails

        return created_issues
