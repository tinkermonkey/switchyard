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
from config.manager import ConfigManager
from config.state_manager import GitHubStateManager
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
                    disc_id = github_state.issue_discussion_links.get(int(issue_number))
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
        if discussion_number != 'unknown':
            discussion_reference = f"**Discussion**: This work is detailed in discussion [{discussion_number}]({discussion_url})"

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
- [Relevant API endpoints or signatures]
- [Data model details for this phase]
- [Component interaction details]
- [All relevant technical details, guidance, and context from the architecture design]

**Acceptance Criteria**:
- [ ] [Testable criterion from test plan]
- [ ] [Another criterion]
- [ ] [Code is reviewed and approved]

**Dependencies**: None (or list phase numbers: "Phase 2, Phase 3")

**Parent Issue**: #{parent_issue_number}

{discussion_reference}

---

### Phase 2: [Next Phase Title]
[... same structure ...]
```

**IMPORTANT FORMATTING RULES**:
1. Use `### Phase N: [Title]` for each phase header
2. Use `**Field**: Value` for metadata fields
3. Separate phases with `---`
4. Ensure the section starts with `## Sub-Issues to Create`


Each sub-issue will be created as a sub-task of issue #{parent_issue_number} and placed in the SDLC board's Backlog column, ordered by dependency.

Make sure to:
1. Extract phases from the software architect's design (or create logical phases if not explicit)
2. Break work into smaller chunks if phases are too large
3. **CRITICAL**: Pull specific requirements from the business analyst's work and specific design guidance from the software architect.
4. **CRITICAL**: Include detailed technical specifications (API signatures, data models, component interactions) in the Design Guidance section. The sub-issue must be self-contained.
5. Order phases by dependencies (foundational work first)
6. Keep titles concise and descriptive
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

        # Split by phase markers (### Phase N...)
        # Relaxed pattern: Allow optional colon, hyphen, or just space
        # Also handle the lookahead for the next phase more flexibly
        phase_pattern = r'### (Phase \d+.*?)\n(.*?)(?=\n### Phase \d+|\Z)'
        phase_matches = re.finditer(phase_pattern, section_content, re.DOTALL | re.IGNORECASE)

        for phase_match in phase_matches:
            phase_title = phase_match.group(1).strip()
            phase_content = phase_match.group(2).strip()

            # Extract fields - capture full content including formatting
            title = self._extract_field(phase_content, 'Title', default=phase_title)
            description = self._extract_field(phase_content, 'Description')
            requirements = self._extract_field(phase_content, 'Requirements')
            design_guidance = self._extract_field(phase_content, 'Design Guidance')
            acceptance_criteria = self._extract_field(phase_content, 'Acceptance Criteria')
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
                body_parts.append(requirements)
                body_parts.append("")

            if design_guidance:
                body_parts.append("## Design Guidance")
                body_parts.append(design_guidance)
                body_parts.append("")

            if acceptance_criteria:
                body_parts.append("## Acceptance Criteria")
                body_parts.append(acceptance_criteria)
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
        """
        Extract a single field value from markdown content.
        Captures everything until the next field marker or end of section.
        Preserves all formatting (newlines, code blocks, lists).
        """
        # Look for **Field**: Value
        # Stop at next **Field**: (start of line) or end of string
        pattern = rf'\*\*{re.escape(field_name)}\*\*:\s*(.+?)(?=\n\*\*.+?\*\*[:]|\Z)'
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

                created_issues.append({
                    'number': issue_number,
                    'url': issue_url,
                    'title': sub_issue['title'],
                    'phase': sub_issue['phase']
                })

                logger.info(f"Created sub-issue #{issue_number}: {sub_issue['title']}")

            except Exception as e:
                logger.error(f"Failed to create sub-issue '{sub_issue['title']}': {e}")
                # Continue with other issues

        return created_issues
