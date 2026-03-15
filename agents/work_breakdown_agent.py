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
from agents.base_analysis_agent import AnalysisAgent
from agents.utils import parse_json_block
from config.manager import ConfigManager
from config.state_manager import GitHubStateManager
from monitoring.decision_events import DecisionEventEmitter
import logging
import json
import re

logger = logging.getLogger(__name__)


class WorkBreakdownAgent(AnalysisAgent):
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
        # Output format is fully controlled by sub_issue_instructions in
        # _build_initial_prompt — no generic sections needed here.
        return []

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS - Agent-specific guidelines
    # ==================================================================================

    def get_initial_guidelines(self) -> str:
        return """
## Important Guidelines

- Break work into logical phases based on the architecture design
- Each sub-issue should be a cohesive unit of work for a developer
- **CRITICAL**: Include DETAILED technical design in each sub-issue. The developer should not need to look up the original architecture document.
- Copy relevant API signatures, data models, and component interactions directly into the sub-issue.
- Include all specific requirements, design guidance, and acceptance criteria in each sub-issue
- Order sub-issues by dependencies (earlier phases first)
- Keep phase titles concise: "Phase 1: Infrastructure setup"
- Do NOT include effort estimates or timeline predictions
- Focus on WHAT needs to be done in each phase, not HOW long it will take

**IMPORTANT**: The engineer won't be given the full requirements/design again, so ensure each sub-issue is self-contained including all necessary details.

"""

    def get_quality_standards(self) -> str:
        return """
- Each sub-issue has clear, testable acceptance criteria
- Dependencies between sub-issues are explicitly stated
- Requirements trace back to the original business requirements
- Design guidance captures relevant sections of the architecture with full architectural context
"""

    # ==================================================================================
    # ENHANCED EXECUTION - Create sub-issues in addition to discussion post
    # ==================================================================================

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute work breakdown with sub-issue creation.

        Suppresses the default raw-output GitHub post from docker_runner and
        instead posts a clean summary comment listing the created sub-issues.
        """
        # Suppress docker_runner's automatic GitHub post — we own all posting
        # from this point: either a clean creation summary or an error note.
        # Setting output_posted=True also blocks the agent_executor fallback path.
        task_context = context.get('context', {})
        task_context['suppress_github_post'] = True

        context = await super().execute(context)

        # Block agent_executor's fallback posting path regardless of outcome.
        context['output_posted'] = True

        # Re-fetch task_context after super() (it may have been mutated)
        task_context = context.get('context', {})

        # Determine execution mode
        mode = self._determine_execution_mode(task_context)

        # Only create/manage sub-issues in initial mode or when explicitly requested
        if mode == 'initial' or self._should_manage_sub_issues(task_context):
            project_name = task_context.get('project', 'unknown')
            try:
                markdown_output = context.get('markdown_analysis', '')
                sub_issues = self._parse_sub_issues_from_output(markdown_output)

                if sub_issues:
                    created_issues = await self._create_sub_issues(
                        sub_issues,
                        task_context,
                        project_name
                    )

                    context['created_sub_issues'] = created_issues
                    logger.info(f"Created {len(created_issues)} sub-issues for {project_name}")

                    if created_issues:
                        self._post_creation_summary(task_context, created_issues, project_name)
                        self._advance_parent_to_in_development(task_context, project_name)
                    else:
                        self._post_error_comment(
                            task_context, project_name,
                            "Work breakdown ran but no sub-issues were created. "
                            "The agent may have returned an empty list."
                        )
                else:
                    self._post_error_comment(
                        task_context, project_name,
                        "Work breakdown ran but could not parse any sub-issues from the agent output. "
                        "Please review the agent logs and re-run."
                    )

            except Exception as e:
                logger.error(f"Failed to create sub-issues: {e}", exc_info=True)
                context['sub_issue_creation_error'] = str(e)
                self._post_error_comment(task_context, project_name, f"Work breakdown failed: {e}")

        return context

    def _advance_parent_to_in_development(self, task_context: Dict[str, Any], project_name: str):
        """
        Move the parent issue from 'Work Breakdown' to 'In Development' on the Planning board.

        Called after successful sub-issue creation so the parent enters the tracking phase.
        """
        try:
            import subprocess as sp

            # Determine parent issue number
            workspace_type = task_context.get('workspace_type', 'issues')
            parent_issue_number = None

            if workspace_type == 'discussions':
                discussion_id = task_context.get('discussion_id')
                if discussion_id:
                    github_state = self.state_manager.load_project_state(project_name)
                    if github_state and github_state.discussion_issue_links:
                        parent_issue_number = github_state.discussion_issue_links.get(discussion_id)
            else:
                parent_issue_number = task_context.get('issue_number')

            if not parent_issue_number:
                logger.warning("Could not determine parent issue number for auto-advance")
                return

            parent_issue_number = int(parent_issue_number)

            # Find the Planning board
            github_state = self.state_manager.load_project_state(project_name)
            if not github_state:
                logger.warning(f"No GitHub state for {project_name}, skipping auto-advance")
                return

            planning_board = None
            planning_board_name = None
            for board_name, board in github_state.boards.items():
                if 'planning' in board_name.lower():
                    planning_board = board
                    planning_board_name = board_name
                    break

            if not planning_board:
                logger.warning("Planning board not found, skipping auto-advance")
                return

            # Find "In Development" column
            in_dev_column = None
            for column in planning_board.columns:
                if column.name == "In Development":
                    in_dev_column = column
                    break

            if not in_dev_column:
                logger.warning("'In Development' column not found on Planning board")
                return

            # Get project config for org/repo
            project_config = self.config_manager.get_project_config(project_name)
            github_config = project_config.github

            # Find the project item ID for the parent issue on the Planning board
            query = f'''{{
                repository(owner: "{github_config['org']}", name: "{github_config['repo']}") {{
                    issue(number: {parent_issue_number}) {{
                        projectItems(first: 10) {{
                            nodes {{
                                id
                                project {{ number }}
                            }}
                        }}
                    }}
                }}
            }}'''

            result = sp.run(
                ['gh', 'api', 'graphql', '-f', f'query={query}'],
                capture_output=True, text=True, check=True, timeout=30
            )
            data = json.loads(result.stdout)
            items = data['data']['repository']['issue']['projectItems']['nodes']

            item_id = None
            for item in items:
                if item['project']['number'] == planning_board.project_number:
                    item_id = item['id']
                    break

            if not item_id:
                logger.warning(f"Parent #{parent_issue_number} not found on Planning board")
                return

            # Set status to "In Development"
            mutation = f'''
            mutation {{
                updateProjectV2ItemFieldValue(
                    input: {{
                        projectId: "{planning_board.project_id}"
                        itemId: "{item_id}"
                        fieldId: "{planning_board.status_field_id}"
                        value: {{ singleSelectOptionId: "{in_dev_column.id}" }}
                    }}
                ) {{
                    projectV2Item {{ id }}
                }}
            }}
            '''

            sp.run(
                ['gh', 'api', 'graphql', '-f', f'query={mutation}'],
                capture_output=True, text=True, check=True, timeout=30
            )

            logger.info(
                f"Auto-advanced parent #{parent_issue_number} to 'In Development' "
                f"on Planning board after sub-issue creation"
            )

        except Exception as e:
            logger.error(f"Failed to auto-advance parent to 'In Development': {e}", exc_info=True)

    def _post_creation_summary(
        self,
        task_context: Dict[str, Any],
        created_issues: List[Dict[str, Any]],
        project_name: str,
    ) -> None:
        """Post a clean bullet-list comment listing the created sub-issues."""
        lines = [f"**Work Breakdown Complete** — created {len(created_issues)} sub-issue(s):\n"]
        for issue in created_issues:
            number = issue.get('number', '?')
            title = issue.get('title', 'Untitled')
            url = issue.get('url', '')
            deps = (issue.get('dependencies') or 'None').strip()
            link = f"[{title}]({url})" if url else title
            dep_note = f" *(depends on {deps})*" if deps.lower() != 'none' else ''
            lines.append(f"- #{number} {link}{dep_note}")
        self._post_comment(task_context, project_name, '\n'.join(lines))

    def _post_error_comment(
        self,
        task_context: Dict[str, Any],
        project_name: str,
        message: str,
    ) -> None:
        """Post a warning comment when sub-issue creation cannot complete."""
        self._post_comment(task_context, project_name, f"**Work Breakdown Warning**: {message}")

    def _post_comment(
        self,
        task_context: Dict[str, Any],
        project_name: str,
        body: str,
    ) -> None:
        """
        Post a comment to the discussion or issue associated with this task.
        This is the single posting path used by both summary and error comments,
        replacing the raw agent output that docker_runner would otherwise post.
        """
        try:
            workspace_type = task_context.get('workspace_type', 'issues')
            if workspace_type == 'discussions':
                discussion_id = task_context.get('discussion_id')
                if discussion_id:
                    from services.github_discussions import GitHubDiscussions
                    GitHubDiscussions().add_discussion_comment(discussion_id, body)
                else:
                    logger.warning("No discussion_id in task_context — cannot post comment")
            else:
                import subprocess as sp
                project_config = self.config_manager.get_project_config(project_name)
                github_config = project_config.github
                repo = f"{github_config['org']}/{github_config['repo']}"
                issue_number = task_context.get('issue_number')
                sp.run(
                    ['gh', 'issue', 'comment', str(issue_number), '--repo', repo, '--body', body],
                    check=True, capture_output=True, text=True,
                )
        except Exception as e:
            logger.error(f"Failed to post comment: {e}", exc_info=True)

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
            # IMPORTANT: issue_number is actually the PARENT ISSUE number, not the discussion number
            # We need to look up both the parent issue and get the discussion number
            try:
                if discussion_id:
                    # discussion_id is the internal GitHub node ID for the discussion
                    github_state = self.state_manager.load_project_state(project)
                    if github_state and github_state.discussion_issue_links:
                        parent_issue_number = github_state.discussion_issue_links.get(discussion_id)
                        if parent_issue_number:
                            parent_issue_url = f"{repo_url}/issues/{parent_issue_number}"
                            logger.info(f"Found parent issue #{parent_issue_number} for discussion (node_id: {discussion_id})")
                        else:
                            logger.warning(f"No parent issue found for discussion node_id {discussion_id}")
                    else:
                        logger.warning(f"No discussion_issue_links in GitHub state for project {project}")

                    # Get discussion number from GitHub using the discussion_id
                    from services.github_discussions import GitHubDiscussions
                    discussions = GitHubDiscussions()
                    discussion_details = discussions.get_discussion(discussion_id)
                    if discussion_details:
                        discussion_number = discussion_details.get('number', 'unknown')
                        discussion_url = f"{repo_url}/discussions/{discussion_number}"
                        logger.info(f"Discussion #{discussion_number} (node_id: {discussion_id})")
                    else:
                        logger.warning(f"Could not fetch discussion details for {discussion_id}")
                else:
                    logger.error(f"No discussion_id provided in task_context")
            except Exception as e:
                logger.error(f"Error looking up discussion/issue: {e}", exc_info=True)
        else:
            # We're working on an issue directly
            parent_issue_number = issue_number
            parent_issue_url = f"{repo_url}/issues/{issue_number}"

            # If working on an issue, there might still be an associated discussion
            # Look it up for the sub-issue body
            try:
                github_state = self.state_manager.load_project_state(project)
                if github_state and github_state.issue_discussion_links:
                    # Convert to string - YAML keys are strings even for numeric values
                    disc_id = github_state.issue_discussion_links.get(str(issue_number))
                    if disc_id:
                        # Get discussion number for this issue
                        from services.github_discussions import GitHubDiscussions
                        discussions = GitHubDiscussions()
                        discussion_details = discussions.get_discussion(disc_id)
                        if discussion_details:
                            discussion_number = discussion_details.get('number', 'unknown')
                            discussion_url = f"{repo_url}/discussions/{discussion_number}"
                            logger.info(f"Issue #{issue_number} has associated discussion #{discussion_number}")
            except Exception as e:
                logger.debug(f"Could not look up discussion for issue #{issue_number}: {e}")

        # Add sub-issue formatting instructions
        # Build discussion reference based on what we have
        discussion_reference = ""
        discussion_reference_json = ""
        if discussion_number != 'unknown':
            discussion_reference = f"**Discussion**: This work is detailed in discussion [{discussion_number}]({discussion_url})"
            discussion_reference_json = f"This work is detailed in discussion [{discussion_number}]({discussion_url})"

        sub_issue_instructions = f"""

## Output Format

Output ONLY a ```json code block containing an array of sub-issue objects.
Do not add any other text before or after the JSON.

```json
[
  {{
    "title": "Phase 1: [Concise description]",
    "description": "Brief overview of this phase's goals.",
    "requirements": "- Specific requirement 1\\n- Specific requirement 2",
    "design_guidance": "- Technical detail 1\\n- API signature or data model",
    "acceptance_criteria": "- [ ] Testable criterion\\n- [ ] Code is reviewed and approved",
    "dependencies": "None",
    "parent_issue": "#{parent_issue_number}",
    "discussion": "{discussion_reference_json}",
    "phase": "Phase 1: [Concise description]"
  }},
  {{
    "title": "Phase 2: [Concise description]",
    "description": "...",
    "requirements": "...",
    "design_guidance": "...",
    "acceptance_criteria": "...",
    "dependencies": "Phase 1",
    "parent_issue": "#{parent_issue_number}",
    "discussion": "{discussion_reference_json}",
    "phase": "Phase 2: [Concise description]"
  }}
]
```

**Rules**:
- One object per phase, ordered by dependency (foundational work first)
- `requirements`, `design_guidance`, and `acceptance_criteria` are multi-line markdown strings — use `\\n` for newlines within each JSON string value
- `dependencies`: `"None"` or phase titles like `"Phase 1"` or `"Phase 1, Phase 2"`
- The JSON array must be syntactically valid

**Content requirements**:
1. Extract phases from the software architect's design (or create logical phases if not explicit)
2. Break work into smaller chunks if phases are too large
3. **CRITICAL**: Pull specific requirements from the business analyst's work and specific design guidance from the software architect
4. **CRITICAL**: Include detailed technical specifications (API signatures, data models, component interactions) in `design_guidance` — the sub-issue must be self-contained
5. Keep titles concise and descriptive
"""

        return base_prompt + sub_issue_instructions

    # ==================================================================================
    # SUB-ISSUE PARSING AND CREATION
    # ==================================================================================

    def _parse_sub_issues_from_output(self, markdown: str) -> List[Dict[str, Any]]:
        """
        Parse sub-issue specifications from the agent's markdown output.

        Tries to extract a JSON data block first (primary path). Falls back to
        markdown field parsing if no valid JSON block is found.
        """
        json_sub_issues = self._extract_json_sub_issues(markdown)
        if json_sub_issues is not None:
            logger.info(f"Parsed {len(json_sub_issues)} sub-issues from JSON block")
            return json_sub_issues

        logger.warning("JSON sub-issue block not found or malformed, falling back to markdown parsing")
        return self._parse_sub_issues_from_markdown(markdown)

    def _extract_json_sub_issues(self, markdown: str) -> Optional[List[Dict[str, Any]]]:
        """
        Extract sub-issue specs from a ```json code block in the output.

        Scans all ```json blocks from last to first (since the data block appears
        at the end of output), using parse_json_block() on each one. Validates
        that the result is a list of phase objects before accepting it.

        Returns None if no valid block is found so the caller can fall back to
        markdown parsing.
        """
        blocks = re.findall(r'```json\s*\n(.*?)\n```', markdown, re.DOTALL | re.IGNORECASE)
        if not blocks:
            return None

        for block_text in reversed(blocks):
            data = parse_json_block(block_text, first_delimiter='[')
            if not isinstance(data, list):
                continue
            if not all(isinstance(item, dict) and item.get('title') for item in data):
                continue
            return self._json_items_to_sub_issues(data)

        return None

    def _json_items_to_sub_issues(self, items: list) -> List[Dict[str, Any]]:
        """Convert parsed JSON phase objects to the sub-issue dict format."""
        sub_issues = []
        for item in items:
            title = (item.get('title') or '').strip()
            if not title:
                logger.warning(f"Skipping JSON sub-issue with no title: {item!r}")
                continue

            description = (item.get('description') or '').strip()
            requirements = (item.get('requirements') or '').strip()
            design_guidance = (item.get('design_guidance') or '').strip()
            acceptance_criteria = (item.get('acceptance_criteria') or '').strip()
            dependencies = (item.get('dependencies') or 'None').strip()
            parent_issue = (item.get('parent_issue') or '').strip()
            discussion_link = (item.get('discussion') or '').strip()
            phase = (item.get('phase') or title).strip()

            body_parts = []
            if description:
                body_parts.append(description)
                body_parts.append('')
            if requirements:
                body_parts.append('## Requirements')
                body_parts.append(requirements)
                body_parts.append('')
            if design_guidance:
                body_parts.append('## Design Guidance')
                body_parts.append(design_guidance)
                body_parts.append('')
            if acceptance_criteria:
                body_parts.append('## Acceptance Criteria')
                body_parts.append(acceptance_criteria)
                body_parts.append('')
            if dependencies and dependencies.lower() != 'none':
                body_parts.append('## Dependencies')
                body_parts.append(dependencies)
                body_parts.append('')
            if parent_issue:
                body_parts.append('## Parent Issue')
                body_parts.append(f'Part of {parent_issue}')
                body_parts.append('')
            if discussion_link:
                body_parts.append('## Discussion')
                body_parts.append(discussion_link)

            sub_issues.append({
                'title': title,
                'body': '\n'.join(body_parts),
                'dependencies': dependencies,
                'parent_issue': parent_issue,
                'phase': phase,
            })
        return sub_issues

    def _parse_sub_issues_from_markdown(self, markdown: str) -> List[Dict[str, Any]]:
        """
        Fallback: parse sub-issue specs from the human-readable markdown section.

        Looks for the "Sub-Issues to Create" section and extracts structured data
        using **Field**: value markers. Less reliable than JSON parsing — used only
        when no valid JSON block is present in the output.
        """
        sub_issues = []

        # Extract the sub-issues section using code-fence-aware parsing so that
        # ## headers inside fenced code blocks don't prematurely end the section.
        section_content = self._extract_sub_issues_section(markdown)

        if not section_content:
            logger.warning("No 'Sub-Issues to Create' section found in output")
            return sub_issues

        phases = self._split_phases(section_content)

        for phase_title, phase_content in phases:
            title = self._extract_field(phase_content, 'Title', default=phase_title)
            description = self._extract_field(phase_content, 'Description')
            requirements = self._extract_field(phase_content, 'Requirements')
            design_guidance = self._extract_field(phase_content, 'Design Guidance')
            acceptance_criteria = self._extract_field(phase_content, 'Acceptance Criteria')
            dependencies = self._extract_field(phase_content, 'Dependencies', default='None')
            parent_issue = self._extract_field(phase_content, 'Parent Issue')
            discussion_link = self._extract_field(phase_content, 'Discussion')

            body_parts = []
            if description:
                body_parts.append(description)
                body_parts.append('')
            if requirements:
                body_parts.append('## Requirements')
                body_parts.append(requirements)
                body_parts.append('')
            if design_guidance:
                body_parts.append('## Design Guidance')
                body_parts.append(design_guidance)
                body_parts.append('')
            if acceptance_criteria:
                body_parts.append('## Acceptance Criteria')
                body_parts.append(acceptance_criteria)
                body_parts.append('')
            if dependencies and dependencies.lower() != 'none':
                body_parts.append('## Dependencies')
                body_parts.append(dependencies)
                body_parts.append('')
            if parent_issue:
                body_parts.append('## Parent Issue')
                body_parts.append(f'Part of {parent_issue}')
                body_parts.append('')
            if discussion_link:
                body_parts.append('## Discussion')
                body_parts.append(discussion_link)

            sub_issues.append({
                'title': title,
                'body': '\n'.join(body_parts),
                'dependencies': dependencies,
                'parent_issue': parent_issue,
                'phase': phase_title,
            })

        logger.info(f"Parsed {len(sub_issues)} sub-issues from markdown")
        return sub_issues

    @staticmethod
    def _is_code_fence(line: str) -> bool:
        """Check if a line is a code fence boundary (``` or ~~~)."""
        stripped = line.strip()
        return stripped.startswith('```') or stripped.startswith('~~~')

    def _extract_sub_issues_section(self, markdown: str) -> str:
        """
        Extract the 'Sub-Issues to Create' section content from markdown.

        Walks line-by-line tracking code fence state so that ## headers inside
        fenced code blocks (e.g., changelog examples) do not prematurely
        terminate the section.
        """
        lines = markdown.split('\n')
        in_code_fence = False
        section_start = None

        for i, line in enumerate(lines):
            if self._is_code_fence(line):
                in_code_fence = not in_code_fence
                continue

            if in_code_fence:
                continue

            if section_start is None:
                # Look for the target section header
                if re.match(r'^##\s+Sub-Issues to Create', line, re.IGNORECASE):
                    section_start = i + 1
            else:
                # Next H2 header outside a code fence ends the section.
                # ^##\s+ matches "## X" but NOT "### X" (third char would be #, not space).
                if re.match(r'^##\s+', line):
                    return '\n'.join(lines[section_start:i])

        if section_start is not None:
            return '\n'.join(lines[section_start:])
        return ''

    def _split_phases(self, section_content: str) -> list:
        """
        Split section content into phases by ``### Phase N`` headers.

        Uses the same code-fence-aware line walking as _extract_sub_issues_section
        so that ### Phase headers inside code blocks are not treated as boundaries.

        Returns:
            List of (phase_title, phase_content) tuples.
        """
        lines = section_content.split('\n')
        in_code_fence = False
        phases = []
        current_title = None
        current_start = None

        for i, line in enumerate(lines):
            if self._is_code_fence(line):
                in_code_fence = not in_code_fence
                continue

            if in_code_fence:
                continue

            phase_match = re.match(r'^###\s+(Phase\s+\d+.*)', line, re.IGNORECASE)
            if phase_match:
                if current_title is not None:
                    content = '\n'.join(lines[current_start:i]).strip()
                    phases.append((current_title, content))
                current_title = phase_match.group(1).strip()
                current_start = i + 1

        # Capture the last phase
        if current_title is not None:
            content = '\n'.join(lines[current_start:]).strip()
            phases.append((current_title, content))

        return phases

    def _extract_field(self, content: str, field_name: str, default: str = '') -> str:
        """
        Extract a single field value from markdown content.
        Captures everything until the next field marker or end of section.
        Preserves all formatting (newlines, code blocks, lists).
        """
        # Define the known field markers to stop at
        # This prevents stopping at random bold text in markdown tables/content
        known_fields = [
            'Title', 'Description', 'Requirements', 'Design Guidance',
            'Acceptance Criteria', 'Dependencies', 'Parent Issue', 'Discussion'
        ]
        
        # Build lookahead pattern that only matches known field markers at line start
        # This ensures we capture ALL content between fields, including tables, code blocks, etc.
        field_alternation = '|'.join(re.escape(f) for f in known_fields)
        pattern = rf'\*\*{re.escape(field_name)}\*\*:\s*(.+?)(?=\n\*\*(?:{field_alternation})\*\*:|\Z)'
        
        match = re.search(pattern, content, re.DOTALL)
        if match:
            return match.group(1).strip()
        return default

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
        import subprocess
        import json as json_lib

        pipeline_run_id = task_context.get('pipeline_run_id')

        # Get project configuration
        project_config = self.config_manager.get_project_config(project_name)
        github_config = project_config.github
        repo = f"{github_config['org']}/{github_config['repo']}"

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

        # Use task_context issue_number as the authoritative parent — never trust the
        # agent's JSON output for this, as Claude may confuse issues mentioned in the
        # discussion context with the actual parent.
        parent_issue_number = str(task_context.get('issue_number')) if task_context.get('issue_number') else None
        if parent_issue_number:
            logger.info(f"Parent issue: #{parent_issue_number} (from task_context)")
        elif sub_issues and 'parent_issue' in sub_issues[0]:
            import re as regex
            match = regex.search(r'#(\d+)', sub_issues[0]['parent_issue'])
            if match:
                parent_issue_number = match.group(1)
                logger.warning(f"Parent issue: #{parent_issue_number} (from agent JSON — task_context had no issue_number)")

        # Get parent issue ID (node ID, not number) for GraphQL
        parent_issue_id = None
        if parent_issue_number:
            try:
                result = subprocess.run(
                    ['gh', 'issue', 'view', parent_issue_number, '-R', repo, '--json', 'id'],
                    capture_output=True,
                    text=True,
                    check=True
                )
                parent_data = json_lib.loads(result.stdout)
                parent_issue_id = parent_data['id']
                logger.info(f"Parent issue #{parent_issue_number} has ID: {parent_issue_id}")
            except Exception as e:
                logger.error(f"Failed to get parent issue ID: {e}")

        # Create each sub-issue
        created_issues = []
        for idx, sub_issue in enumerate(sub_issues, start=1):
            try:
                # Check for existing issue with same title to prevent duplicates
                # This handles the "zombie run" scenario where the agent runs again
                existing_issue = None
                try:
                    # Escape quotes in title for search
                    search_title = sub_issue['title'].replace('"', '\\"')
                    search_cmd = ['gh', 'issue', 'list', '-R', repo, '--search', f'"{search_title}" in:title', '--json', 'number,title,id,url', '--state', 'all']
                    search_result = subprocess.run(
                        search_cmd,
                        capture_output=True,
                        text=True,
                        check=True
                    )
                    search_data = json_lib.loads(search_result.stdout)
                    
                    # Find exact match
                    for item in search_data:
                        if item['title'] == sub_issue['title']:
                            existing_issue = item
                            logger.info(f"Found existing issue #{item['number']} with title '{sub_issue['title']}'")
                            break
                except Exception as e:
                    logger.warning(f"Failed to check for existing issue: {e}")

                if existing_issue:
                    # Use existing issue
                    issue_number = str(existing_issue['number'])
                    issue_url = existing_issue['url']
                    issue_id = existing_issue['id']
                    logger.info(f"Skipping creation of '{sub_issue['title']}' - using existing issue #{issue_number}")
                else:
                    # Create issue using GitHub CLI (returns URL directly)
                    result = subprocess.run(
                        ['gh', 'issue', 'create',
                         '-R', repo,
                         '--title', sub_issue['title'],
                         '--body', sub_issue['body']],
                        capture_output=True,
                        text=True,
                        check=True
                    )

                    # gh issue create returns the issue URL
                    issue_url = result.stdout.strip()
                    
                    # Extract issue number from URL (e.g., https://github.com/org/repo/issues/123)
                    import re as regex
                    url_match = regex.search(r'/issues/(\d+)$', issue_url)
                    if not url_match:
                        raise Exception(f"Could not extract issue number from URL: {issue_url}")
                    
                    issue_number = url_match.group(1)
                    
                    # Get full issue details including node ID using gh issue view
                    view_result = subprocess.run(
                        ['gh', 'issue', 'view', issue_number,
                         '-R', repo,
                         '--json', 'id,number,url'],
                        capture_output=True,
                        text=True,
                        check=True
                    )
                    
                    issue_data = json_lib.loads(view_result.stdout)
                    issue_id = issue_data['id']
                    issue_number = str(issue_data['number'])

                # Add issue to SDLC board's Backlog column (idempotent-ish, but good to ensure)
                subprocess.run(
                    ['gh', 'project', 'item-add', str(sdlc_board.project_number),
                     '--owner', github_config['org'],
                     '--url', issue_url],
                    capture_output=True,
                    text=True,
                    check=True
                )

                # Get the project item ID and set status to "Backlog"
                try:
                    # Query to get all project items for this issue
                    query = f'''{{
                        repository(owner: "{github_config['org']}", name: "{github_config['repo']}") {{
                            issue(number: {issue_number}) {{
                                projectItems(first: 10) {{
                                    nodes {{
                                        id
                                        project {{
                                            number
                                            id
                                            title
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}'''

                    result = subprocess.run(
                        ['gh', 'api', 'graphql', '-f', f'query={query}'],
                        capture_output=True,
                        text=True,
                        check=True
                    )

                    query_data = json_lib.loads(result.stdout)
                    project_items = query_data['data']['repository']['issue']['projectItems']['nodes']

                    logger.info(f"Issue #{issue_number} is in {len(project_items)} project(s): {[p['project']['title'] for p in project_items]}")

                    # Find the item ID for the SDLC board and remove from other boards
                    sdlc_item_id = None
                    other_project_items = []
                    
                    for item in project_items:
                        if item['project']['number'] == sdlc_board.project_number:
                            sdlc_item_id = item['id']
                            logger.info(f"Found issue #{issue_number} in SDLC board (project #{sdlc_board.project_number})")
                        else:
                            # Track items in other projects to remove them
                            other_project_items.append(item)
                            logger.info(f"Found issue #{issue_number} in other board: {item['project']['title']} (project #{item['project']['number']})")

                    # Remove from other boards (like Planning & Design)
                    if other_project_items:
                        logger.info(f"Removing issue #{issue_number} from {len(other_project_items)} non-SDLC board(s)")
                        for item in other_project_items:
                            try:
                                delete_mutation = f'''
                                mutation {{
                                    deleteProjectV2Item(input: {{
                                        projectId: "{item['project']['id']}"
                                        itemId: "{item['id']}"
                                    }}) {{
                                        deletedItemId
                                    }}
                                }}
                                '''
                                delete_result = subprocess.run(
                                    ['gh', 'api', 'graphql', '-f', f'query={delete_mutation}'],
                                    capture_output=True,
                                    text=True,
                                    check=True
                                )
                                logger.info(f"✓ Removed issue #{issue_number} from project '{item['project']['title']}' (#{item['project']['number']})")
                            except Exception as e:
                                logger.warning(f"✗ Failed to remove issue #{issue_number} from project #{item['project']['number']}: {e}")
                    else:
                        logger.info(f"Issue #{issue_number} is only in SDLC board - no removal needed")

                    if sdlc_item_id:
                        # Get the Backlog column option ID
                        backlog_option_id = backlog_column.id
                        status_field_id = sdlc_board.status_field_id

                        if not status_field_id:
                            logger.warning(f"No status_field_id found in SDLC board state")
                        else:
                            # Set the status to Backlog
                            status_mutation = f'''
                            mutation {{
                                updateProjectV2ItemFieldValue(
                                    input: {{
                                        projectId: "{sdlc_board.project_id}"
                                        itemId: "{sdlc_item_id}"
                                        fieldId: "{status_field_id}"
                                        value: {{
                                            singleSelectOptionId: "{backlog_option_id}"
                                        }}
                                    }}
                                ) {{
                                    projectV2Item {{
                                        id
                                    }}
                                }}
                            }}
                            '''

                            subprocess.run(
                                ['gh', 'api', 'graphql', '-f', f'query={status_mutation}'],
                                capture_output=True,
                                text=True,
                                check=True
                            )
                            logger.info(f"Set issue #{issue_number} status to Backlog in SDLC board")
                    else:
                        logger.warning(f"Could not find project item ID for issue #{issue_number} in SDLC board")

                except Exception as e:
                    logger.error(f"Failed to set status for issue #{issue_number}: {e}")
                    # Don't fail the entire operation - the issue was created and added to the board

                # Link as sub-issue to parent using GraphQL
                if parent_issue_id:
                    graphql_query = f"""
                    mutation {{
                      addSubIssue(input: {{
                        issueId: "{parent_issue_id}",
                        subIssueId: "{issue_id}"
                      }}) {{
                        issue {{
                          title
                        }}
                        subIssue {{
                          title
                        }}
                      }}
                    }}
                    """

                    subprocess.run(
                        ['gh', 'api', 'graphql',
                         '-H', 'GraphQL-Features: sub_issues',
                         '-f', f'query={graphql_query}'],
                        capture_output=True,
                        text=True,
                        check=True
                    )
                    logger.info(f"Linked issue #{issue_number} as sub-issue of #{parent_issue_number}")

                    # Emit SUCCESS event (only for newly created issues, not existing)
                    if not existing_issue:
                        obs = task_context.get('observability')
                        if obs:
                            decision_emitter = DecisionEventEmitter(obs)
                            decision_emitter.emit_sub_issue_created(
                                project=project_name,
                                parent_issue=int(parent_issue_number),
                                issue_number=int(issue_number),
                                title=sub_issue['title'],
                                board="SDLC Execution",
                                reason=f"Work breakdown phase: {sub_issue['phase']}",
                                source="work_breakdown",
                                issue_url=issue_url,
                                body=sub_issue.get('body', ''),
                                context_data={
                                    'phase': sub_issue['phase'],
                                    'order_in_phase': idx,
                                },
                                pipeline_run_id=pipeline_run_id
                            )

                created_issues.append({
                    'number': issue_number,
                    'url': issue_url,
                    'title': sub_issue['title'],
                    'phase': sub_issue['phase']
                })

                logger.info(f"Created sub-issue #{issue_number}: {sub_issue['title']}")

            except Exception as e:
                logger.error(f"Failed to create sub-issue '{sub_issue['title']}': {e}", exc_info=True)

                # Emit FAILURE event
                obs = task_context.get('observability')
                if obs:
                    decision_emitter = DecisionEventEmitter(obs)
                    decision_emitter.emit_sub_issue_creation_failed(
                        project=project_name,
                        parent_issue=int(parent_issue_number),
                        title=sub_issue['title'],
                        board="SDLC Execution",
                        error=e,
                        source="work_breakdown",
                        context_data={
                            'phase': sub_issue['phase'],
                            'order_in_phase': idx,
                        },
                        pipeline_run_id=pipeline_run_id
                    )
                # Continue with other issues

        return created_issues
