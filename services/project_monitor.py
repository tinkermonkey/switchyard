#!/usr/bin/env python3

import time
import json
import subprocess
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from monitoring.timestamp_utils import utc_isoformat
from task_queue.task_manager import TaskQueue, Task, TaskPriority
from config.manager import ConfigManager

logger = logging.getLogger(__name__)

@dataclass
class ProjectItem:
    """Represents an item in a GitHub Projects v2 board"""
    item_id: str
    content_id: str
    issue_number: int
    title: str
    status: str
    repository: str
    last_updated: str

class ProjectMonitor:
    """Monitor GitHub Projects v2 boards for changes and trigger agent workflows"""

    def __init__(self, task_queue: TaskQueue, config_manager: ConfigManager = None):
        self.task_queue = task_queue
        self.config_manager = config_manager or ConfigManager()
        self.last_state = {}  # Store last known state of each project

        # Initialize feedback manager
        from services.feedback_manager import FeedbackManager
        self.feedback_manager = FeedbackManager()

        # Initialize workspace router for discussions
        from services.workspace_router import WorkspaceRouter
        from services.github_discussions import GitHubDiscussions
        self.workspace_router = WorkspaceRouter()
        self.discussions = GitHubDiscussions()

        # Initialize decision observability
        from monitoring.observability import get_observability_manager
        from monitoring.decision_events import DecisionEventEmitter
        self.obs = get_observability_manager()
        self.decision_events = DecisionEventEmitter(self.obs)

        # Initialize pipeline run manager
        from services.pipeline_run import get_pipeline_run_manager
        self.pipeline_run_manager = get_pipeline_run_manager()

        # Get polling interval from first project's orchestrator config (for now)
        projects = self.config_manager.list_projects()
        if projects:
            first_project = self.config_manager.get_project_config(projects[0])
            self.poll_interval = first_project.orchestrator.get("polling_interval", 30)
        else:
            self.poll_interval = 30

    def get_project_items(self, project_owner: str, project_number: int) -> List[ProjectItem]:
        """Query GitHub Projects v2 API to get current project items (excludes closed issues)"""
        from services.github_owner_utils import build_projects_v2_query, get_owner_type
        
        # Build the correct query based on owner type
        query = build_projects_v2_query(project_owner, project_number)
        
        if query is None:
            logger.error(f"Cannot query project items - unable to determine owner type for '{project_owner}'")
            return []

        try:
            result = subprocess.run(
                ['gh', 'api', 'graphql', '-f', f'query={query}'],
                capture_output=True, text=True, check=True
            )

            data = json.loads(result.stdout)
            
            # Get project data from the correct path based on owner type
            owner_type = get_owner_type(project_owner)
            if owner_type == 'user':
                project_data = data['data']['user']['projectV2']
            else:  # organization
                project_data = data['data']['organization']['projectV2']

            items = []
            for node in project_data['items']['nodes']:
                content = node.get('content')
                if not content:  # Skip draft items
                    continue

                # Skip closed issues - they should not trigger any agents
                issue_state = content.get('state', '').upper()
                if issue_state == 'CLOSED':
                    logger.debug(f"Skipping closed issue #{content['number']} in project query")
                    continue

                # Find status field
                status = "No Status"
                for field_value in node['fieldValues']['nodes']:
                    if field_value and field_value.get('field', {}).get('name') == 'Status':
                        status = field_value.get('name', 'No Status')
                        break

                item = ProjectItem(
                    item_id=node['id'],
                    content_id=content['id'],
                    issue_number=content['number'],
                    title=content['title'],
                    status=status,
                    repository=content['repository']['name'],
                    last_updated=content['updatedAt']
                )
                items.append(item)

            return items

        except subprocess.CalledProcessError as e:
            if "Resource not accessible by personal access token" in str(e):
                logger.error("GitHub token missing Projects v2 permissions!")
                logger.error("Please update your GITHUB_TOKEN to include 'project' or 'read:project' scope")
                logger.error("Visit: https://github.com/settings/tokens")
            else:
                logger.error(f"GraphQL query failed: {e}")
            return []
        except Exception as e:
            logger.error(f"Error querying project items: {e}")
            return []

    def detect_changes(self, project_name: str, current_items: List[ProjectItem]) -> List[Dict[str, Any]]:
        """Detect changes in project items since last poll"""
        changes = []

        # Create lookup by issue number for current items
        current_by_issue = {item.issue_number: item for item in current_items}

        # Get last known state
        last_items = self.last_state.get(project_name, {})

        # Check for status changes
        for issue_number, current_item in current_by_issue.items():
            last_item = last_items.get(issue_number)

            if last_item is None:
                # New item added to project
                changes.append({
                    'type': 'item_added',
                    'project': project_name,
                    'issue_number': issue_number,
                    'title': current_item.title,
                    'status': current_item.status,
                    'repository': current_item.repository
                })
            elif last_item.status != current_item.status:
                # Status changed
                changes.append({
                    'type': 'status_changed',
                    'project': project_name,
                    'issue_number': issue_number,
                    'title': current_item.title,
                    'old_status': last_item.status,
                    'new_status': current_item.status,
                    'repository': current_item.repository
                })
                
                # EMIT DECISION EVENT: Status change detected (if not already emitted)
                # Check if this was a recent programmatic change to avoid duplicate events
                from services.work_execution_state import work_execution_tracker
                
                # Extract project/board names from project_name key (format: project_board)
                actual_project = project_name.split('_', 1)[0] if '_' in project_name else project_name
                board_name = project_name.split('_', 1)[1] if '_' in project_name else 'unknown'
                
                # Check if this status change was recently made programmatically
                # (in which case the event was already emitted by pipeline_progression)
                was_programmatic = work_execution_tracker.was_recent_programmatic_change(
                    project_name=actual_project,
                    issue_number=issue_number,
                    to_status=current_item.status,
                    time_window_seconds=60
                )
                
                if not was_programmatic:
                    # Only emit if this appears to be a manual status change
                    self.decision_events.emit_status_progression(
                        issue_number=issue_number,
                        project=actual_project,
                        board=board_name,
                        from_status=last_item.status,
                        to_status=current_item.status,
                        trigger='manual',  # Status changes from GitHub polling are manual
                        success=True  # Already successfully moved in GitHub
                    )
                else:
                    logger.debug(
                        f"Skipping duplicate status_progression event for #{issue_number} "
                        f"({last_item.status} → {current_item.status}) - already emitted programmatically"
                    )

        # Update last state
        self.last_state[project_name] = current_by_issue

        return changes

    def get_issue_details(self, repository: str, issue_number: int, org: str) -> Dict[str, Any]:
        """Fetch full issue details from GitHub"""
        try:
            result = subprocess.run(
                ['gh', 'issue', 'view', str(issue_number), '--repo', f"{org}/{repository}", '--json', 'title,body,labels,state,author,createdAt,updatedAt'],
                capture_output=True, text=True, check=True
            )
            return json.loads(result.stdout)
        except Exception as e:
            logger.error(f"Error fetching issue #{issue_number} details: {e}")
            return {'title': f'Issue #{issue_number}', 'body': '', 'labels': []}

    def get_previous_stage_context(self, repository: str, issue_number: int, org: str,
                                   current_column: str, workflow_template,
                                   workspace_type: str = 'issues',
                                   discussion_id: Optional[str] = None,
                                   pipeline_config=None,
                                   current_stage_config=None,
                                   project_name: Optional[str] = None) -> str:
        """
        Fetch comments from the previous workflow stage agent and user comments since then.
        Works with both issues and discussions workspaces.

        If current_stage_config has inputs_from defined, will gather outputs from those
        specific agents instead of just the previous stage.

        Returns formatted context string.
        """
        # Check if this stage has specific input requirements
        if current_stage_config and hasattr(current_stage_config, 'inputs_from') and current_stage_config.inputs_from:
            # Try to find associated discussion even if we're in issues workspace
            actual_discussion_id = discussion_id
            
            if not actual_discussion_id and workspace_type == 'issues' and project_name:
                # We're in issues workspace but need discussion context
                # Look up the discussion associated with this issue's parent
                try:
                    # For sub-issues, we need to find the parent issue's discussion
                    from config.state_manager import GitHubStateManager
                    state_manager = GitHubStateManager()
                    github_state = state_manager.load_project_state(project_name)
                    
                    if github_state and github_state.issue_discussion_links:
                        # Try to get discussion for this issue
                        actual_discussion_id = github_state.issue_discussion_links.get(issue_number)
                        
                        if not actual_discussion_id:
                            # This might be a sub-issue, look for parent issue's discussion
                            # Get parent issue number from GitHub (sub-issues have trackedIn field)
                            try:
                                import subprocess
                                result = subprocess.run(
                                    ['gh', 'issue', 'view', str(issue_number),
                                     '--repo', f"{org}/{repository}",
                                     '--json', 'body'],
                                    capture_output=True, text=True, check=True
                                )
                                issue_data = json.loads(result.stdout)
                                body = issue_data.get('body', '')
                                
                                # Look for "Part of #NNN" pattern in issue body
                                import re
                                parent_match = re.search(r'Part of #(\d+)', body)
                                if parent_match:
                                    parent_issue_number = int(parent_match.group(1))
                                    actual_discussion_id = github_state.issue_discussion_links.get(parent_issue_number)
                                    if actual_discussion_id:
                                        logger.info(f"Found parent issue #{parent_issue_number} discussion for sub-issue #{issue_number}")
                                    else:
                                        logger.debug(f"Parent issue #{parent_issue_number} found but has no discussion")
                                else:
                                    logger.debug(f"No 'Part of #NNN' pattern found in issue #{issue_number} body")
                            except Exception as e:
                                logger.debug(f"Could not look up parent issue for #{issue_number}: {e}")
                    
                    if actual_discussion_id:
                        logger.info(f"Found associated discussion {actual_discussion_id} for issue #{issue_number}")
                except Exception as e:
                    logger.warning(f"Error looking up discussion for issue #{issue_number}: {e}")
            
            # Use discussion-based approach for gathering specific agent outputs
            if actual_discussion_id:
                logger.info(f"Gathering inputs from specific agents: {current_stage_config.inputs_from}")
                return self._get_agent_outputs_from_discussion(actual_discussion_id, current_stage_config.inputs_from)
            else:
                logger.warning(f"inputs_from specified but no discussion found for issue #{issue_number}")
                
                # Fallback 1: Check the current issue for agent outputs
                logger.info(f"Checking current issue #{issue_number} for outputs from: {current_stage_config.inputs_from}")
                issue_outputs = self._get_agent_outputs_from_issue(repository, issue_number, org, current_stage_config.inputs_from)
                
                if issue_outputs:
                    logger.info(f"Found agent outputs in current issue #{issue_number}")
                    return issue_outputs
                
                # Fallback 2: Check parent issue if one exists
                logger.info(f"No outputs found in issue #{issue_number}, checking for parent issue")
                try:
                    import asyncio
                    from services.github_integration import GitHubIntegration
                    from services.feature_branch_manager import feature_branch_manager
                    
                    # Get GitHub integration with proper repo context
                    github_integration = GitHubIntegration(repo_owner=org, repo_name=repository)
                    
                    # Create event loop if needed
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                    
                    # Get parent issue number
                    parent_issue_number = loop.run_until_complete(
                        feature_branch_manager.get_parent_issue(
                            github_integration,
                            issue_number,
                            project=project_name
                        )
                    )
                    
                    if parent_issue_number:
                        logger.info(f"Found parent issue #{parent_issue_number}, checking for agent outputs")
                        parent_outputs = self._get_agent_outputs_from_issue(
                            repository, parent_issue_number, org, current_stage_config.inputs_from
                        )
                        
                        if parent_outputs:
                            logger.info(f"Found agent outputs in parent issue #{parent_issue_number}")
                            return parent_outputs
                        else:
                            logger.info(f"No outputs found in parent issue #{parent_issue_number}")
                    else:
                        logger.info(f"No parent issue found for issue #{issue_number}")
                        
                except Exception as e:
                    logger.warning(f"Error checking parent issue: {e}")
                    import traceback
                    logger.warning(traceback.format_exc())
                
                # Fallback 3: Use general issue context
                logger.warning(f"No agent outputs found in issue or parent, falling back to general issue context")
                return self._get_issue_context(repository, issue_number, org, current_column, workflow_template)

        # For hybrid pipelines, determine if previous stage was in discussions or issues
        should_use_discussion = False

        if workspace_type == 'hybrid' and pipeline_config:
            # Find previous column
            column_names = [col.name for col in workflow_template.columns]
            if current_column in column_names:
                current_index = column_names.index(current_column)
                if current_index > 0:
                    previous_column_name = workflow_template.columns[current_index - 1].name
                    # Check if previous stage is in discussion_stages
                    discussion_stages = getattr(pipeline_config, 'discussion_stages', [])
                    # Convert column name to stage key (e.g., "Design" -> "design")
                    previous_stage_key = previous_column_name.lower().replace(' ', '_')
                    if previous_stage_key in [s.lower() for s in discussion_stages]:
                        should_use_discussion = True
                        logger.info(f"Hybrid pipeline: previous stage '{previous_column_name}' is in discussion_stages, will use discussion context")

        # Route to appropriate workspace
        if (workspace_type == 'discussions' or should_use_discussion) and discussion_id:
            return self._get_discussion_context(discussion_id, current_column, workflow_template)
        else:
            return self._get_issue_context(repository, issue_number, org, current_column, workflow_template)

    def _get_issue_context(self, repository: str, issue_number: int, org: str,
                           current_column: str, workflow_template) -> str:
        """
        Get previous stage context from issue comments.

        Now gathers ALL agent outputs and user feedback from the entire thread,
        not just the immediately previous column. This ensures that when an issue
        moves backwards in the workflow (e.g., from Testing back to Development),
        the agent receives all relevant context including QA feedback.
        """
        try:
            # Fetch all comments
            result = subprocess.run(
                ['gh', 'issue', 'view', str(issue_number), '--repo', f"{org}/{repository}", '--json', 'comments'],
                capture_output=True, text=True, check=True
            )
            data = json.loads(result.stdout)

            from dateutil import parser as date_parser
            from datetime import timezone

            # Collect ALL agent comments with their timestamps
            agent_comments = []
            for comment in data.get('comments', []):
                body = comment.get('body', '')
                # Match agent signature pattern
                if '_Processed by the ' in body and ' agent_' in body:
                    # Extract agent name from signature
                    import re
                    match = re.search(r'_Processed by the (.+?) agent_', body)
                    if match:
                        agent_name = match.group(1)
                        timestamp = comment.get('createdAt')
                        parsed_time = date_parser.parse(timestamp)
                        if parsed_time.tzinfo is None:
                            parsed_time = parsed_time.replace(tzinfo=timezone.utc)

                        agent_comments.append({
                            'agent': agent_name,
                            'body': body,
                            'timestamp': parsed_time,
                            'raw_timestamp': timestamp
                        })

            if not agent_comments:
                return ""  # No agent comments yet

            # Sort agent comments chronologically
            agent_comments.sort(key=lambda x: x['timestamp'])

            # Collect user comments (non-bot comments)
            user_comments = []
            for comment in data.get('comments', []):
                if not comment.get('author', {}).get('isBot', False):
                    timestamp = comment.get('createdAt')
                    parsed_time = date_parser.parse(timestamp)
                    if parsed_time.tzinfo is None:
                        parsed_time = parsed_time.replace(tzinfo=timezone.utc)

                    user_comments.append({
                        'author': comment.get('author', {}).get('login', 'unknown'),
                        'body': comment.get('body', ''),
                        'timestamp': parsed_time
                    })

            # Build chronological context with all agent outputs and user feedback
            context_parts = []
            context_parts.append("## Previous Work and Feedback")
            context_parts.append("\nThe following is a complete history of agent outputs and user feedback for this issue:\n")

            # Merge agent comments and user comments in chronological order
            all_items = []
            for ac in agent_comments:
                all_items.append(('agent', ac))
            for uc in user_comments:
                all_items.append(('user', uc))

            all_items.sort(key=lambda x: x[1]['timestamp'])

            # Format chronologically
            for item_type, item in all_items:
                if item_type == 'agent':
                    context_parts.append(f"\n### Output from {item['agent'].replace('_', ' ').title()}")
                    context_parts.append(item['body'])
                else:
                    context_parts.append(f"\n**User Feedback (@{item['author']})**:")
                    context_parts.append(item['body'])

            return "\n\n".join(context_parts)

        except Exception as e:
            logger.error(f"Error fetching previous stage context: {e}")
            return ""

    def _get_discussion_context(self, discussion_id: str, current_column: str, workflow_template) -> str:
        """Get previous stage context from discussion comments and threaded replies"""
        try:
            # Find previous column in workflow
            column_names = [col.name for col in workflow_template.columns]
            if current_column not in column_names:
                return ""

            current_index = column_names.index(current_column)
            if current_index == 0:
                return ""  # First column, no previous stage

            previous_column = workflow_template.columns[current_index - 1]
            previous_agent = previous_column.agent

            if not previous_agent or previous_agent == 'null':
                return ""  # No agent in previous stage

            # Get discussion with all comments AND REPLIES
            from services.github_app import github_app
            from dateutil import parser as date_parser
            from datetime import timezone

            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
                  number
                  comments(first: 100) {
                    nodes {
                      id
                      body
                      author {
                        login
                      }
                      createdAt
                      replies(first: 50) {
                        nodes {
                          id
                          body
                          author {
                            login
                          }
                          createdAt
                        }
                      }
                    }
                  }
                }
              }
            }
            """

            result = github_app.graphql_request(query, {'discussionId': discussion_id})
            if not result or 'node' not in result:
                logger.error(f"Failed to get discussion {discussion_id}")
                return ""

            all_comments = result['node']['comments']['nodes']

            # Find the last top-level comment from the previous agent
            # We only want the specific comment thread, not replies buried in other threads
            previous_agent_comment = None
            previous_agent_timestamp = None
            agent_signature = f"_Processed by the {previous_agent} agent_"

            for comment in reversed(all_comments):
                # Only check top-level comments for the agent's output
                if agent_signature in comment.get('body', ''):
                    comment_time = date_parser.parse(comment.get('createdAt'))
                    if previous_agent_timestamp is None or comment_time > previous_agent_timestamp:
                        previous_agent_comment = comment
                        previous_agent_timestamp = comment_time

            if not previous_agent_comment:
                return ""  # Previous agent hasn't processed yet

            # Make timezone-aware if naive
            if previous_agent_timestamp.tzinfo is None:
                previous_agent_timestamp = previous_agent_timestamp.replace(tzinfo=timezone.utc)

            # Get the agent's output
            previous_agent_output = previous_agent_comment.get('body', '')

            # Collect ONLY replies to this specific comment (threaded replies)
            user_feedback = []
            for reply in previous_agent_comment.get('replies', {}).get('nodes', []):
                reply_author = reply.get('author', {})
                reply_author_login = reply_author.get('login', '') if reply_author else ''
                reply_is_bot = 'bot' in reply_author_login.lower()

                # Only include non-bot replies
                if not reply_is_bot:
                    reply_time = date_parser.parse(reply.get('createdAt'))
                    if reply_time.tzinfo is None:
                        reply_time = reply_time.replace(tzinfo=timezone.utc)

                    user_feedback.append({
                        'author': reply_author_login,
                        'body': reply.get('body', ''),
                        'type': 'reply',
                        'time': reply_time
                    })

            # Sort feedback chronologically
            user_feedback.sort(key=lambda x: x['time'])

            # Format context
            context_parts = []
            context_parts.append(f"## Output from {previous_agent.replace('_', ' ').title()}")
            context_parts.append(previous_agent_output)

            if user_feedback:
                context_parts.append("\n## User Feedback Since Then")
                for feedback in user_feedback:
                    feedback_type = " (reply)" if feedback['type'] == 'reply' else ""
                    context_parts.append(f"**@{feedback['author']}**{feedback_type}: {feedback['body']}")

            return "\n\n".join(context_parts)

        except Exception as e:
            logger.error(f"Error fetching discussion context: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return ""

    def _get_agent_outputs_from_discussion(self, discussion_id: str, agent_names: List[str]) -> str:
        """
        Get outputs from specific agents with full threaded context.
        Used when a stage has inputs_from specified.
        
        For each agent:
        1. Find their final output (could be top-level OR a threaded reply)
        2. Collect all threaded conversation (human feedback + agent replies)
        3. Return complete context for each input agent

        Args:
            discussion_id: The discussion ID
            agent_names: List of agent names to get outputs from

        Returns:
            Formatted context string with all specified agent outputs and their threaded conversations
        """
        try:
            from services.github_app import github_app
            from dateutil import parser as date_parser

            # GraphQL query WITH replies to capture threaded conversations
            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
                  number
                  comments(first: 100) {
                    nodes {
                      id
                      body
                      author {
                        login
                      }
                      createdAt
                      replies(first: 50) {
                        nodes {
                          id
                          body
                          author {
                            login
                          }
                          createdAt
                        }
                      }
                    }
                  }
                }
              }
            }
            """

            result = github_app.graphql_request(query, {'discussionId': discussion_id})
            if not result or 'node' not in result:
                logger.error(f"Failed to get discussion {discussion_id}")
                return ""

            all_comments = result['node']['comments']['nodes']

            # Find outputs from each requested agent
            context_parts = []

            for agent_name in agent_names:
                agent_signature = f"_Processed by the {agent_name} agent_"

                # Find agent's FINAL output (could be in top-level OR threaded reply)
                final_output = None
                parent_comment_id = None
                final_timestamp = None

                # Check threaded replies first (most recent refinements)
                for comment in all_comments:
                    for reply in comment.get('replies', {}).get('nodes', []):
                        if agent_signature in reply.get('body', ''):
                            reply_time = date_parser.parse(reply.get('createdAt'))
                            if final_timestamp is None or reply_time > final_timestamp:
                                final_output = reply
                                parent_comment_id = comment['id']
                                final_timestamp = reply_time

                # Check top-level comments (initial outputs)
                for comment in all_comments:
                    if agent_signature in comment.get('body', ''):
                        comment_time = date_parser.parse(comment.get('createdAt'))
                        if final_timestamp is None or comment_time > final_timestamp:
                            final_output = comment
                            parent_comment_id = comment['id']
                            final_timestamp = comment_time

                if not final_output:
                    logger.warning(f"No output found from agent '{agent_name}' in discussion {discussion_id}")
                    continue

                # Build complete context for this agent
                agent_context = []
                agent_context.append(f"## Output from {agent_name.replace('_', ' ').title()}")

                # If we have a parent comment, get the full thread history
                if parent_comment_id:
                    thread_history = self.get_full_thread_history(all_comments, parent_comment_id)

                    if thread_history:
                        # Format thread chronologically
                        for msg in thread_history:
                            author = msg['author']
                            body = msg['body']
                            role = msg['role']

                            if role == 'agent':
                                agent_context.append(f"\n**{agent_name}** (agent):")
                                agent_context.append(body)
                            else:
                                agent_context.append(f"\n**@{author}** (human feedback):")
                                agent_context.append(body)
                    else:
                        # Fallback: just the final output
                        agent_context.append(final_output.get('body', ''))
                else:
                    # Just the final output (no thread)
                    agent_context.append(final_output.get('body', ''))

                context_parts.append('\n'.join(agent_context))

            return "\n\n---\n\n".join(context_parts) if context_parts else ""

        except Exception as e:
            logger.error(f"Error fetching agent outputs from discussion: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return ""

    def _get_agent_outputs_from_issue(self, repository: str, issue_number: int, org: str, agent_names: List[str]) -> str:
        """
        Get outputs from specific agents from an issue.
        Similar to _get_agent_outputs_from_discussion but for issues.
        
        For each agent:
        1. Find their most recent output in the issue comments
        2. Return the complete comment body

        Args:
            repository: Repository name
            issue_number: Issue number
            org: Organization name
            agent_names: List of agent names to get outputs from

        Returns:
            Formatted context string with all specified agent outputs
        """
        try:
            import subprocess
            import json
            from dateutil import parser as date_parser

            # Fetch all comments from the issue
            result = subprocess.run(
                ['gh', 'issue', 'view', str(issue_number), '--repo', f"{org}/{repository}", '--json', 'comments'],
                capture_output=True, text=True, check=True
            )
            data = json.loads(result.stdout)
            all_comments = data.get('comments', [])

            # Find outputs from each requested agent
            context_parts = []

            for agent_name in agent_names:
                agent_signature = f"_Processed by the {agent_name} agent_"

                # Find agent's most recent output
                final_output = None
                final_timestamp = None

                for comment in all_comments:
                    if agent_signature in comment.get('body', ''):
                        comment_time = date_parser.parse(comment.get('createdAt'))
                        if final_timestamp is None or comment_time > final_timestamp:
                            final_output = comment
                            final_timestamp = comment_time

                if not final_output:
                    logger.warning(f"No output found from agent '{agent_name}' in issue #{issue_number}")
                    continue

                # Build context for this agent
                agent_context = []
                agent_context.append(f"## Output from {agent_name.replace('_', ' ').title()}")
                agent_context.append(final_output.get('body', ''))

                context_parts.append('\n'.join(agent_context))

            return "\n\n---\n\n".join(context_parts) if context_parts else ""

        except subprocess.CalledProcessError as e:
            logger.error(f"Error fetching issue comments: {e}")
            return ""
        except Exception as e:
            logger.error(f"Error fetching agent outputs from issue: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return ""

    def _check_agent_processed_issue_sync(self, issue_number: int, agent: str, repository: str) -> bool:
        """Synchronous wrapper for checking if agent has processed issue"""
        try:
            import asyncio
            from services.github_integration import GitHubIntegration
            github = GitHubIntegration()
            return asyncio.run(github.has_agent_processed_issue(issue_number, agent, repository))
        except Exception as e:
            logger.warning(f"Could not check for prior agent work: {e}")
            return False

    def trigger_agent_for_status(self, project_name: str, board_name: str, issue_number: int, status: str, repository: str) -> Optional[str]:
        """Determine which agent should handle this status and create a task or review cycle"""
        try:
            # Get workflow template for this board
            project_config = self.config_manager.get_project_config(project_name)

            # DEFENSIVE: Check if issue is open before triggering any agents
            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])
            issue_state = issue_data.get('state', '').upper()

            if issue_state == 'CLOSED':
                logger.info(f"Skipping agent trigger for issue #{issue_number}: issue is CLOSED")
                return None

            # Find the pipeline config for this board
            pipeline_config = None
            for pipeline in project_config.pipelines:
                if pipeline.board_name == board_name:
                    pipeline_config = pipeline
                    break

            if not pipeline_config:
                logger.info(f"No pipeline config found for board {board_name}")
                return None

            # Get workflow template
            workflow_template = self.config_manager.get_workflow_template(pipeline_config.workflow)

            # Find the column that matches this status
            agent = None
            column = None
            for col in workflow_template.columns:
                if col.name == status:
                    agent = col.agent
                    column = col
                    break

            if agent and agent != 'null':
                # Determine workspace type and get discussion ID FIRST, before using them
                from config.state_manager import state_manager
                workspace_type = pipeline_config.workspace
                discussion_id = None

                if workspace_type in ['discussions', 'hybrid']:
                    discussion_id = state_manager.get_discussion_for_issue(project_name, issue_number)

                # Get or create pipeline run early so we can tag all events
                # Fetch issue details for pipeline run
                issue_data_early = self.get_issue_details(repository, issue_number, project_config.github['org'])
                pipeline_run = self.pipeline_run_manager.get_or_create_pipeline_run(
                    issue_number=issue_number,
                    issue_title=issue_data_early.get('title', f'Issue #{issue_number}'),
                    issue_url=issue_data_early.get('url', ''),
                    project=project_name,
                    board=board_name
                )
                logger.debug(f"Using pipeline run {pipeline_run.id} for issue #{issue_number}")

                # EMIT DECISION EVENT: Agent routing decision
                # Collect alternative agents from workflow
                alternative_agents = [
                    col.agent for col in workflow_template.columns
                    if col.agent and col.agent != 'null' and col.agent != agent
                ]
                
                self.decision_events.emit_agent_routing_decision(
                    issue_number=issue_number,
                    project=project_name,
                    board=board_name,
                    current_status=status,
                    selected_agent=agent,
                    reason=f"Status '{status}' maps to agent '{agent}' in workflow '{pipeline_config.workflow}'",
                    alternatives=alternative_agents,
                    workspace_type=workspace_type,
                    pipeline_run_id=pipeline_run.id
                )
                
                # Check if there's already a pending task for this issue and agent
                existing_tasks = self.task_queue.get_pending_tasks()
                for existing_task in existing_tasks:
                    task_context = existing_task.context
                    if (task_context.get('issue_number') == issue_number and
                        task_context.get('project') == project_name and
                        task_context.get('board') == board_name and
                        existing_task.agent == agent):
                        logger.info(f"Task already exists for {agent} on issue #{issue_number} - skipping duplicate")
                        return None

                # Check if agent should execute work using execution state tracker
                # This check must happen BEFORE column type routing to prevent duplicate runs
                import asyncio
                try:
                    # Create a new event loop for this thread if needed
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)

                    from services.work_execution_state import work_execution_tracker

                    # Determine trigger source (manual move from GitHub)
                    trigger_source = 'manual'

                    # Track if this issue has already been handled (don't start fresh work):
                    # 1. Work already executed (per execution state tracker)
                    # 2. Resume thread already started (for review/conversational columns)
                    already_handled = False

                    # Check if work should be executed using the new execution state logic
                    should_execute, reason = work_execution_tracker.should_execute_work(
                        issue_number=issue_number,
                        column=status,
                        agent=agent,
                        trigger_source=trigger_source,
                        project_name=project_name
                    )

                    if not should_execute:
                        already_handled = True  # Work was already executed
                        logger.info(
                            f"Skipping {agent} on issue #{issue_number} in {status}: {reason}"
                        )

                    # For backward compatibility, also check comment signatures for discussions
                    already_processed = False
                    if workspace_type in ['discussions', 'hybrid'] and discussion_id:
                        from services.github_integration import GitHubIntegration
                        github = GitHubIntegration()

                        # For discussions, also check discussion comments (fallback)
                        already_processed = loop.run_until_complete(
                            github.has_agent_processed_discussion(discussion_id, agent)
                        )
                        if already_processed:
                            logger.info(f"Agent {agent} has already processed discussion for issue #{issue_number} (comment signature found)")

                            # For review columns, attempt to resume the review cycle in background thread
                            if column and hasattr(column, 'type') and column.type == 'review':
                                logger.info(f"Attempting to resume review cycle for issue #{issue_number} in background thread")
                                try:
                                    from services.review_cycle import review_cycle_executor
                                    import threading

                                    def resume_in_thread():
                                        """Resume review cycle in background thread (non-blocking)"""
                                        try:
                                            loop = asyncio.new_event_loop()
                                            asyncio.set_event_loop(loop)

                                            next_column, success = loop.run_until_complete(
                                                review_cycle_executor.resume_review_cycle(
                                                    issue_number=issue_number,
                                                    repository=repository,
                                                    project_name=project_name,
                                                    board_name=board_name,
                                                    org=project_config.github['org'],
                                                    discussion_id=discussion_id,
                                                    column=column,
                                                    issue_data=self.get_issue_details(repository, issue_number, project_config.github['org']),
                                                    workflow_columns=workflow_template.columns
                                                )
                                            )

                                            if success:
                                                logger.info(f"Review cycle resumed successfully for issue #{issue_number}")
                                                if next_column and next_column != column.name:
                                                    logger.info(f"Review cycle complete, ready to advance to: {next_column}")
                                            else:
                                                logger.info(f"Review cycle could not be resumed for issue #{issue_number}")

                                            loop.close()
                                        except Exception as e:
                                            logger.error(f"Failed to resume review cycle: {e}")
                                            import traceback
                                            logger.error(traceback.format_exc())

                                    thread = threading.Thread(target=resume_in_thread, daemon=True)
                                    thread.start()
                                    logger.info(f"Review cycle resume thread started for issue #{issue_number}")

                                    # Resume thread is monitoring - don't start fresh work
                                    already_handled = True

                                except Exception as e:
                                    logger.error(f"Failed to start review cycle resume thread: {e}")
                                    import traceback
                                    logger.error(traceback.format_exc())
                            # For conversational columns, resume the feedback monitoring loop
                            elif column and hasattr(column, 'type') and column.type == 'conversational':
                                logger.info(f"Resuming conversational feedback loop for issue #{issue_number} in background thread")
                                try:
                                    from services.human_feedback_loop import human_feedback_loop_executor
                                    import threading

                                    def resume_feedback_loop():
                                        """Resume conversational feedback loop in background thread"""
                                        try:
                                            loop = asyncio.new_event_loop()
                                            asyncio.set_event_loop(loop)

                                            # Just start monitoring - no initial execution needed
                                            from services.human_feedback_loop import HumanFeedbackState

                                            state = HumanFeedbackState(
                                                issue_number=issue_number,
                                                repository=repository,
                                                agent=agent,
                                                project_name=project_name,
                                                board_name=board_name,
                                                workspace_type=workspace_type,
                                                discussion_id=discussion_id
                                            )

                                            # Load persisted session_id for continuity across restarts
                                            from services.conversational_session_state import conversational_session_state
                                            persisted_session = conversational_session_state.load_session(
                                                project_name=project_name,
                                                issue_number=issue_number,
                                                max_age_hours=24
                                            )
                                            if persisted_session:
                                                state.claude_session_id = persisted_session.session_id
                                                logger.info(f"Restored Claude Code session for #{issue_number}: {state.claude_session_id}")

                                            # Load previous outputs from discussion to rebuild conversation history
                                            loop.run_until_complete(
                                                human_feedback_loop_executor._load_previous_outputs_from_discussion(
                                                    state,
                                                    project_config.github['org']
                                                )
                                            )

                                            # Register and start monitoring
                                            human_feedback_loop_executor.active_loops[issue_number] = state
                                            human_feedback_loop_executor.workflow_columns = workflow_template.columns

                                            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

                                            loop.run_until_complete(
                                                human_feedback_loop_executor._conversational_loop(
                                                    state, column, issue_data, project_config.github['org']
                                                )
                                            )

                                            loop.close()
                                        except Exception as e:
                                            logger.error(f"Failed to resume feedback loop: {e}")
                                            import traceback
                                            logger.error(traceback.format_exc())

                                    thread = threading.Thread(target=resume_feedback_loop, daemon=True)
                                    thread.start()
                                    logger.info(f"Conversational feedback loop monitoring resumed for issue #{issue_number}")

                                    # Resume thread is monitoring - don't start fresh work
                                    already_handled = True

                                except Exception as e:
                                    logger.error(f"Failed to start conversational feedback loop thread: {e}")
                                    import traceback
                                    logger.error(traceback.format_exc())
                            else:
                                logger.info(f"Not a review or conversational column, skipping resume attempt")
                    else:
                        # For issues workspace, check deduplication
                        # BUT skip this check for review and conversational columns
                        if column and column.type == 'review':
                            # Review columns are handled by review cycle executor
                            # Don't use deduplication - cycles need reviewers to run multiple times
                            logger.debug(f"Skipping deduplication check for review column")
                        elif column and column.type == 'conversational':
                            # Check if there's existing work to resume
                            # Only resume if there's evidence of prior agent activity
                            has_prior_work = self._check_agent_processed_issue_sync(issue_number, agent, repository)

                            if has_prior_work:
                                # Resume existing conversational feedback loop
                                logger.info(f"Resuming conversational feedback loop for issue #{issue_number} in issues workspace")
                                try:
                                    from services.human_feedback_loop import human_feedback_loop_executor
                                    import threading

                                    def resume_feedback_loop():
                                        """Resume conversational feedback loop in background thread"""
                                        try:
                                            loop_new = asyncio.new_event_loop()
                                            asyncio.set_event_loop(loop_new)

                                            # Create state for this feedback loop
                                            from services.human_feedback_loop import HumanFeedbackState

                                            state = HumanFeedbackState(
                                                issue_number=issue_number,
                                                repository=repository,
                                                agent=agent,
                                                project_name=project_name,
                                                board_name=board_name,
                                                workspace_type='issues',
                                                discussion_id=None  # Issues don't have discussion IDs
                                            )

                                            # Load persisted session_id for continuity across restarts
                                            from services.conversational_session_state import conversational_session_state
                                            persisted_session = conversational_session_state.load_session(
                                                project_name=project_name,
                                                issue_number=issue_number,
                                                max_age_hours=24
                                            )
                                            if persisted_session:
                                                state.claude_session_id = persisted_session.session_id
                                                logger.info(f"Restored Claude Code session for #{issue_number}: {state.claude_session_id}")

                                            # Load previous outputs from issue to rebuild conversation history
                                            loop_new.run_until_complete(
                                                human_feedback_loop_executor._load_previous_outputs_from_issue(
                                                    state,
                                                    project_config.github['org']
                                                )
                                            )

                                            # Register and start monitoring
                                            human_feedback_loop_executor.active_loops[issue_number] = state
                                            human_feedback_loop_executor.workflow_columns = workflow_template.columns

                                            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

                                            loop_new.run_until_complete(
                                                human_feedback_loop_executor._conversational_loop(
                                                    state, column, issue_data, project_config.github['org']
                                                )
                                            )

                                            loop_new.close()
                                        except Exception as e:
                                            logger.error(f"Failed to resume feedback loop: {e}")
                                            import traceback
                                            logger.error(traceback.format_exc())

                                    thread = threading.Thread(target=resume_feedback_loop, daemon=True)
                                    thread.start()
                                    logger.info(f"Conversational feedback loop monitoring resumed for issue #{issue_number}")

                                    # Return early - we've started the monitoring loop
                                    return agent

                                except Exception as e:
                                    logger.error(f"Failed to start conversational feedback loop thread: {e}")
                                    import traceback
                                    logger.error(traceback.format_exc())
                                    # If resume fails, allow normal startup below
                            else:
                                # No prior work found, will start fresh conversational loop below
                                logger.debug(f"No prior work found for conversational column, will start fresh loop")
                        else:
                            # For non-review, non-conversational columns in issues workspace
                            # Rely on execution state tracker (already checked above)
                            pass

                    # If already handled (work executed or resume thread started), don't start fresh work
                    if already_handled:
                        return None

                except Exception as e:
                    logger.warning(f"Could not check if issue was already processed: {e}")
                    import traceback
                    logger.warning(traceback.format_exc())
                    # Continue anyway if we can't check

            # Check column type and route appropriately
            # This happens AFTER the "already processed" check to prevent duplicate runs
            if column and hasattr(column, 'type'):
                if column.type == 'conversational':
                    logger.info(f"Starting conversational loop for issue #{issue_number} in {status}")
                    return self._start_conversational_loop_for_issue(
                        project_name, board_name, issue_number, status,
                        repository, project_config, pipeline_config,
                        workflow_template, column
                    )
                elif column.type == 'review':
                    logger.info(f"Starting review cycle for issue #{issue_number} in {status}")
                    return self._start_review_cycle_for_issue(
                        project_name, board_name, issue_number, status,
                        repository, project_config, pipeline_config,
                        workflow_template, column
                    )

            if agent and agent != 'null':

                # Fetch full issue details from GitHub (use issue_data_early from above)
                issue_data = issue_data_early

                # Get the stage config from pipeline template for this column
                pipeline_template = self.config_manager.get_pipeline_template(pipeline_config.template)
                current_stage_config = None
                if column:
                    # Map column name to stage - column.stage_mapping should give us the stage name
                    stage_name = column.stage_mapping if hasattr(column, 'stage_mapping') else None
                    if stage_name and pipeline_template:
                        # Find the stage config in the pipeline template
                        for stage in pipeline_template.stages:
                            if stage.stage == stage_name:
                                current_stage_config = stage
                                break

                # Check if this is a repair_cycle stage and handle it specially
                if current_stage_config and getattr(current_stage_config, 'stage_type', None) == 'repair_cycle':
                    logger.info(f"Detected repair_cycle stage for issue #{issue_number} in {status}")
                    return self._start_repair_cycle_for_issue(
                        project_name, board_name, issue_number, status,
                        repository, project_config, pipeline_config,
                        workflow_template, column, current_stage_config
                    )

                # Fetch context from previous workflow stage (workspace-aware)
                previous_stage_context = self.get_previous_stage_context(
                    repository, issue_number, project_config.github['org'],
                    status, workflow_template,
                    workspace_type=workspace_type,
                    discussion_id=discussion_id,
                    pipeline_config=pipeline_config,
                    current_stage_config=current_stage_config,
                    project_name=project_name
                )

                # Pipeline run already created above, just use it
                logger.info(f"Creating task with pipeline run {pipeline_run.id} for issue #{issue_number}")

                # Create task for the agent
                task_context = {
                    'project': project_name,
                    'board': board_name,
                    'pipeline': pipeline_config.name,
                    'repository': repository,
                    'issue_number': issue_number,
                    'issue': issue_data,  # Include full issue details
                    'previous_stage_output': previous_stage_context,  # Include previous agent's work
                    'column': status,
                    'trigger': 'project_monitor',
                    'workspace_type': workspace_type,
                    'pipeline_run_id': pipeline_run.id,  # Include pipeline run ID
                    'timestamp': utc_isoformat()
                }

                # Add discussion_id if working in discussions
                if discussion_id:
                    task_context['discussion_id'] = discussion_id

                task = Task(
                    id=f"{agent}_{project_name}_{board_name}_{issue_number}_{int(time.time())}",
                    agent=agent,
                    project=project_name,
                    priority=TaskPriority.MEDIUM,
                    context=task_context,
                    created_at=utc_isoformat()
                )

                self.task_queue.enqueue(task)
                
                # EMIT DECISION EVENT: Task queued
                self.decision_events.emit_task_queued(
                    agent=agent,
                    project=project_name,
                    issue_number=issue_number,
                    board=board_name,
                    priority='MEDIUM',
                    reason=f"Agent '{agent}' assigned to issue #{issue_number} in status '{status}'",
                    pipeline_run_id=pipeline_run.id
                )

                # Record execution start in work execution state
                from services.work_execution_state import work_execution_tracker
                work_execution_tracker.record_execution_start(
                    issue_number=issue_number,
                    column=status,
                    agent=agent,
                    trigger_source='manual',  # Triggered from project monitor
                    project_name=project_name
                )

                logger.info(f"Created task for {agent} - Issue #{issue_number} moved to {status} on {board_name}")
                return agent
            else:
                # No agent for this column - end pipeline run if one exists
                logger.info(f"No agent assigned to column '{status}' in {board_name}")
                
                # End active pipeline run (issue has reached end of pipeline)
                ended = self.pipeline_run_manager.end_pipeline_run(
                    project=project_name,
                    issue_number=issue_number,
                    reason=f"Issue moved to column '{status}' with no agent"
                )
                if ended:
                    logger.info(f"Ended pipeline run for issue #{issue_number} (no agent in column '{status}')")
                
                return None

        except Exception as e:
            logger.error(f"Error triggering agent for status: {e}")
            return None

    def _start_conversational_loop_for_issue(
        self,
        project_name: str,
        board_name: str,
        issue_number: int,
        status: str,
        repository: str,
        project_config,
        pipeline_config,
        workflow_template,
        column
    ) -> Optional[str]:
        """Start a conversational loop (human feedback mode) for an issue"""
        try:
            import asyncio
            from services.human_feedback_loop import human_feedback_loop_executor

            # Get issue details
            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

            # Get workspace info
            workspace_type = pipeline_config.workspace
            from config.state_manager import state_manager
            discussion_id = None

            if workspace_type in ['discussions', 'hybrid']:
                discussion_id = state_manager.get_discussion_for_issue(project_name, issue_number)

            # Get the stage config from pipeline template for this column
            pipeline_template_data = self.config_manager.get_pipeline_template(pipeline_config.template)
            current_stage_config = None
            if column:
                # Map column name to stage - column.stage_mapping should give us the stage name
                stage_name = column.stage_mapping if hasattr(column, 'stage_mapping') else None
                if stage_name and pipeline_template_data:
                    # Find the stage config in the pipeline template
                    for stage in pipeline_template_data.stages:
                        if stage.stage == stage_name:
                            current_stage_config = stage
                            break

            # Get previous stage output if available
            previous_stage_context = self.get_previous_stage_context(
                repository, issue_number, project_config.github['org'],
                status, workflow_template,
                workspace_type=workspace_type,
                discussion_id=discussion_id,
                pipeline_config=pipeline_config,
                current_stage_config=current_stage_config,
                project_name=project_name
            )

            # Start conversational loop in background thread
            logger.info(
                f"Starting conversational loop for {column.agent} on issue #{issue_number} "
                f"(workspace: {workspace_type})"
            )

            import threading

            def run_loop_in_thread():
                """Run the async loop in a background thread"""
                try:
                    # Create new event loop for this thread
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                    next_column, success = loop.run_until_complete(
                        human_feedback_loop_executor.start_loop(
                            issue_number=issue_number,
                            repository=repository,
                            project_name=project_name,
                            board_name=board_name,
                            column=column,
                            issue_data=issue_data,
                            previous_stage_output=previous_stage_context,
                            org=project_config.github['org'],
                            workflow_columns=workflow_template.columns,
                            workspace_type=workspace_type,
                            discussion_id=discussion_id
                        )
                    )

                    logger.info(
                        f"Conversational loop completed for issue #{issue_number}, "
                        f"success={success}, next_column={next_column}"
                    )

                    # Move card to next column if auto_advance is enabled and we have a next column
                    if success and next_column:
                        auto_advance = getattr(column, 'auto_advance_on_approval', False)
                        if auto_advance:
                            logger.info(f"Auto-advancing issue #{issue_number} to {next_column}")
                            try:
                                from services.pipeline_progression import PipelineProgression
                                progression_service = PipelineProgression(self.task_queue)
                                progression_service.move_issue_to_column(
                                    project_name=project_name,
                                    board_name=board_name,
                                    issue_number=issue_number,
                                    target_column=next_column,
                                    trigger='conversational_loop_completion'
                                )
                                logger.info(f"Successfully moved issue #{issue_number} to {next_column}")
                            except Exception as move_error:
                                logger.error(f"Failed to move issue to next column: {move_error}")
                                import traceback
                                logger.error(traceback.format_exc())
                        else:
                            logger.info(f"Auto-advance disabled for column {column.name}, not moving card")

                    loop.close()
                except Exception as e:
                    logger.error(f"Error in conversational loop thread: {e}")
                    import traceback
                    logger.error(traceback.format_exc())

            # Start in background thread
            thread = threading.Thread(target=run_loop_in_thread, daemon=True)
            thread.start()

            logger.info(f"Conversational loop thread started for issue #{issue_number}")

            return column.agent

        except Exception as e:
            logger.error(f"Conversational loop failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def _start_review_cycle_for_issue(
        self,
        project_name: str,
        board_name: str,
        issue_number: int,
        status: str,
        repository: str,
        project_config,
        pipeline_config,
        workflow_template,
        column
    ) -> Optional[str]:
        """Start an automated review cycle for an issue in a review column (non-blocking)"""
        try:
            import asyncio
            import threading
            from services.review_cycle import review_cycle_executor

            # Get issue details
            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

            # Get previous stage context (maker's output)
            workspace_type = pipeline_config.workspace
            from config.state_manager import state_manager
            discussion_id = None

            if workspace_type in ['discussions', 'hybrid']:
                discussion_id = state_manager.get_discussion_for_issue(project_name, issue_number)

            # Get the stage config from pipeline template for this column
            pipeline_template_data = self.config_manager.get_pipeline_template(pipeline_config.template)
            current_stage_config = None
            if column:
                # Map column name to stage - column.stage_mapping should give us the stage name
                stage_name = column.stage_mapping if hasattr(column, 'stage_mapping') else None
                if stage_name and pipeline_template_data:
                    # Find the stage config in the pipeline template
                    for stage in pipeline_template_data.stages:
                        if stage.stage == stage_name:
                            current_stage_config = stage
                            break

            previous_stage_context = self.get_previous_stage_context(
                repository, issue_number, project_config.github['org'],
                status, workflow_template,
                workspace_type=workspace_type,
                discussion_id=discussion_id,
                pipeline_config=pipeline_config,
                current_stage_config=current_stage_config,
                project_name=project_name
            )

            if not previous_stage_context:
                logger.warning(f"No previous stage output found for issue #{issue_number} - cannot start review cycle")
                return None

            # Get or create pipeline run before starting the thread
            pipeline_run = self.pipeline_run_manager.get_or_create_pipeline_run(
                issue_number=issue_number,
                issue_title=issue_data.get('title', f'Issue #{issue_number}'),
                issue_url=issue_data.get('url', ''),
                project=project_name,
                board=board_name
            )
            logger.debug(f"Using pipeline run {pipeline_run.id} for review cycle on issue #{issue_number}")

            logger.info(
                f"Starting review cycle for issue #{issue_number} in background thread "
                f"(reviewer: {column.agent}, maker: {column.maker_agent})"
            )

            def run_cycle_in_thread():
                """Run the review cycle in a background thread"""
                try:
                    # Create new event loop for this thread
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                    # Post initial comment (workspace-aware)
                    from services.github_integration import GitHubIntegration
                    github = GitHubIntegration()

                    start_context = {
                        'issue_number': issue_number,
                        'repository': repository,
                        'workspace_type': workspace_type,
                        'discussion_id': discussion_id
                    }

                    loop.run_until_complete(
                        github.post_agent_output(
                            start_context,
                            f"""## 🔄 Starting Review Cycle

**Reviewer**: {column.agent.replace('_', ' ').title()}
**Maker**: {column.maker_agent.replace('_', ' ').title()}
**Max Iterations**: {column.max_iterations}

The automated maker-checker review cycle is now starting. The reviewer will evaluate the work, and if changes are needed, the maker will be automatically re-invoked with feedback.

---
_Review cycle initiated by Claude Code Orchestrator_
"""
                        )
                    )

                    # Create or update PR for code review if using git workflow
                    # Find the pipeline to check workspace type
                    pipeline = next(
                        (p for p in project_config.pipelines if p.board_name == board_name),
                        None
                    )
                    if pipeline and pipeline.workspace == 'issues':
                        from services.git_workflow_manager import git_workflow_manager
                        from services.project_workspace import workspace_manager

                        project_dir = workspace_manager.get_project_dir(project_name)

                        pr_result = loop.run_until_complete(
                            git_workflow_manager.create_or_update_pr(
                                project=project_name,
                                issue_number=issue_number,
                                project_dir=project_dir,
                                org=project_config.github['org'],
                                repo=project_config.github['repo'],
                                issue_title=issue_data.get('title', f'Issue #{issue_number}'),
                                issue_body=issue_data.get('body', ''),
                                draft=True  # Start as draft
                            )
                        )

                        if pr_result.get('success'):
                            logger.info(f"PR created/updated for issue #{issue_number}: {pr_result.get('pr_url')}")
                        else:
                            logger.warning(f"Failed to create PR: {pr_result.get('error')}")

                    # Execute review cycle
                    next_column, success = loop.run_until_complete(
                        review_cycle_executor.start_review_cycle(
                            issue_number=issue_number,
                            repository=repository,
                            project_name=project_name,
                            board_name=board_name,
                            column=column,
                            issue_data=issue_data,
                            previous_stage_output=previous_stage_context,
                            org=project_config.github['org'],
                            workflow_columns=workflow_template.columns,
                            workspace_type=workspace_type,
                            discussion_id=discussion_id,
                            pipeline_run_id=pipeline_run.id
                        )
                    )

                    logger.info(
                        f"Review cycle completed for issue #{issue_number}, "
                        f"success={success}, next_column={next_column}"
                    )

                    # Move card to next column if successful and next column specified
                    if success and next_column and next_column != status:
                        try:
                            logger.info(f"Moving issue #{issue_number} from {status} to {next_column}")

                            # Get the project and card IDs
                            project_state = state_manager.load_project_state(project_name)

                            # project_state is a GitHubProjectState object with boards attribute
                            board = project_state.boards.get(board_name) if project_state else None
                            project_id = board.project_id if board else None

                            if not project_id:
                                logger.error(f"No project ID found for {board_name}")
                            else:
                                # Find the target column
                                target_column = next((c for c in workflow_template.columns if c.name == next_column), None)
                                if not target_column:
                                    logger.error(f"Target column {next_column} not found in workflow")
                                else:
                                    # Move the card
                                    from services.pipeline_progression import PipelineProgression
                                    progression_service = PipelineProgression(self.task_queue)
                                    progression_service.move_issue_to_column(
                                        project_name=project_name,
                                        board_name=board_name,
                                        issue_number=issue_number,
                                        target_column=next_column,
                                        trigger='review_cycle_completion'
                                    )
                                    logger.info(f"Successfully moved issue #{issue_number} to {next_column}")
                        except Exception as move_error:
                            logger.error(f"Failed to move issue to next column: {move_error}")
                            import traceback
                            logger.error(traceback.format_exc())

                    loop.close()
                except Exception as e:
                    logger.error(f"Error in review cycle thread: {e}")
                    import traceback
                    logger.error(traceback.format_exc())

            # Start in background thread (non-blocking)
            thread = threading.Thread(target=run_cycle_in_thread, daemon=True)
            thread.start()

            logger.info(f"Review cycle thread started for issue #{issue_number}")

            return column.agent

        except Exception as e:
            logger.error(f"Error starting review cycle: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def _start_repair_cycle_for_issue(
        self,
        project_name: str,
        board_name: str,
        issue_number: int,
        status: str,
        repository: str,
        project_config,
        pipeline_config,
        workflow_template,
        column,
        stage_config
    ) -> Optional[str]:
        """Start an automated repair cycle (test-fix-validate) for an issue"""
        try:
            import asyncio
            import threading
            from pipeline.repair_cycle import RepairCycleStage, RepairTestRunConfig, RepairTestType

            # Get issue details
            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

            # Get workspace info
            workspace_type = pipeline_config.workspace
            from config.state_manager import state_manager
            discussion_id = None

            if workspace_type in ['discussions', 'hybrid']:
                discussion_id = state_manager.get_discussion_for_issue(project_name, issue_number)

            # Get previous stage context
            previous_stage_context = self.get_previous_stage_context(
                repository, issue_number, project_config.github['org'],
                status, workflow_template,
                workspace_type=workspace_type,
                discussion_id=discussion_id,
                pipeline_config=pipeline_config,
                current_stage_config=stage_config,
                project_name=project_name
            )

            # Load test configurations from project config
            testing_config = project_config.testing or {}
            test_configs = []
            
            for test_type_config in testing_config.get('types', []):
                test_type = RepairTestType(test_type_config['type'])
                test_configs.append(RepairTestRunConfig(
                    test_type=test_type,
                    timeout=test_type_config.get('timeout', 600),
                    max_iterations=test_type_config.get('max_iterations', 5),
                    review_warnings=test_type_config.get('review_warnings', True),
                    max_file_iterations=test_type_config.get('max_file_iterations', 3)
                ))

            if not test_configs:
                logger.warning(f"No test configurations found for project {project_name}")
                return None

            # Get global settings from stage config
            max_total_agent_calls = stage_config.max_total_agent_calls or 100
            checkpoint_interval = stage_config.checkpoint_interval or 5

            # Get or create pipeline run
            pipeline_run = self.pipeline_run_manager.get_or_create_pipeline_run(
                issue_number=issue_number,
                issue_title=issue_data.get('title', f'Issue #{issue_number}'),
                issue_url=issue_data.get('url', ''),
                project=project_name,
                board=board_name
            )
            logger.debug(f"Using pipeline run {pipeline_run.id} for repair cycle on issue #{issue_number}")

            logger.info(
                f"Starting repair cycle for issue #{issue_number} in background thread "
                f"(agent: {stage_config.default_agent}, test types: {[tc.test_type.value for tc in test_configs]})"
            )

            def run_cycle_in_thread():
                """Run the repair cycle in a background thread"""
                try:
                    # Create new event loop for this thread
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                    # Post initial comment (workspace-aware)
                    from services.github_integration import GitHubIntegration
                    github = GitHubIntegration()

                    start_context = {
                        'issue_number': issue_number,
                        'repository': repository,
                        'workspace_type': workspace_type,
                        'discussion_id': discussion_id
                    }

                    loop.run_until_complete(
                        github.post_agent_output(
                            start_context,
                            f"""## 🔧 Starting Repair Cycle (Testing)

**Agent**: {stage_config.default_agent.replace('_', ' ').title()}
**Test Types**: {', '.join([tc.test_type.value for tc in test_configs])}
**Max Iterations**: {max_total_agent_calls}

The automated test-fix-validate cycle is now starting. Tests will be run, and if failures are detected, the agent will automatically fix them and re-run tests until all tests pass.

---
_Repair cycle initiated by Claude Code Orchestrator_
"""
                        )
                    )

                    # Create RepairCycleStage
                    stage = RepairCycleStage(
                        name=status,
                        test_configs=test_configs,
                        agent_name=stage_config.default_agent,
                        max_total_agent_calls=max_total_agent_calls,
                        checkpoint_interval=checkpoint_interval
                    )

                    # Build context for stage execution
                    from services.project_workspace import workspace_manager
                    from monitoring.observability import get_observability_manager
                    
                    project_dir = workspace_manager.get_project_dir(project_name)
                    obs = get_observability_manager()

                    stage_context = {
                        'project': project_name,
                        'board': board_name,
                        'pipeline': pipeline_config.name,
                        'repository': repository,
                        'issue_number': issue_number,
                        'issue': issue_data,
                        'previous_stage_output': previous_stage_context,
                        'column': status,
                        'workspace_type': workspace_type,
                        'discussion_id': discussion_id,
                        'pipeline_run_id': pipeline_run.id,
                        'project_dir': project_dir,
                        'use_docker': True,  # Repair cycle should use Docker
                        'observability': obs,  # REQUIRED: Observability manager for event tracking
                        'task_id': f"repair_cycle_{issue_number}_{pipeline_run.id}"  # For observability tracking
                    }

                    # Execute repair cycle
                    result = loop.run_until_complete(stage.execute(stage_context))

                    overall_success = result.get('overall_success', False)
                    test_results = result.get('test_results', [])

                    logger.info(
                        f"Repair cycle completed for issue #{issue_number}, "
                        f"success={overall_success}, total_agent_calls={result.get('total_agent_calls', 0)}"
                    )

                    # Post summary comment
                    summary_lines = [
                        f"## ✅ Repair Cycle Complete" if overall_success else "## ❌ Repair Cycle Failed",
                        "",
                        f"**Total Agent Calls**: {result.get('total_agent_calls', 0)}",
                        f"**Duration**: {result.get('duration_seconds', 0):.1f}s",
                        ""
                    ]

                    for test_result in test_results:
                        test_type = test_result.get('test_type', 'unknown')
                        passed = test_result.get('passed', False)
                        iterations = test_result.get('iterations', 0)
                        summary_lines.append(
                            f"- **{test_type}**: {'✅ PASSED' if passed else '❌ FAILED'} "
                            f"({iterations} iterations)"
                        )

                    summary_lines.append("")
                    summary_lines.append("---")
                    summary_lines.append("_Repair cycle executed by Claude Code Orchestrator_")

                    loop.run_until_complete(
                        github.post_agent_output(
                            start_context,
                            "\n".join(summary_lines)
                        )
                    )

                    # If successful, auto-advance to next column
                    if overall_success:
                        # Find next column in workflow
                        current_index = next(
                            (i for i, col in enumerate(workflow_template.columns) if col.name == status),
                            None
                        )
                        
                        if current_index is not None and current_index + 1 < len(workflow_template.columns):
                            next_column = workflow_template.columns[current_index + 1]
                            
                            logger.info(f"Auto-advancing issue #{issue_number} from {status} to {next_column.name}")
                            
                            from services.pipeline_progression import PipelineProgression
                            progression_service = PipelineProgression(self.task_queue)
                            progression_service.move_issue_to_column(
                                project_name=project_name,
                                board_name=board_name,
                                issue_number=issue_number,
                                target_column=next_column.name,
                                trigger='repair_cycle_completion'
                            )
                            logger.info(f"Successfully moved issue #{issue_number} to {next_column.name}")

                    # Record execution completion in work execution state
                    from services.work_execution_state import work_execution_tracker
                    work_execution_tracker.record_execution_outcome(
                        issue_number=issue_number,
                        column=status,
                        agent=stage_config.default_agent,
                        outcome='success' if overall_success else 'failure',
                        project_name=project_name
                    )

                    loop.close()
                except Exception as e:
                    logger.error(f"Error in repair cycle thread: {e}")
                    import traceback
                    logger.error(traceback.format_exc())

            # Start in background thread (non-blocking)
            thread = threading.Thread(target=run_cycle_in_thread, daemon=True)
            thread.start()

            logger.info(f"Repair cycle thread started for issue #{issue_number}")

            # Record execution start in work execution state
            from services.work_execution_state import work_execution_tracker
            work_execution_tracker.record_execution_start(
                issue_number=issue_number,
                column=status,
                agent=stage_config.default_agent,
                trigger_source='manual',
                project_name=project_name
            )

            return stage_config.default_agent

        except Exception as e:
            logger.error(f"Error starting repair cycle: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def monitor_projects(self):
        """Main monitoring loop using new configuration system"""
        import sys
        import asyncio
        from config.state_manager import state_manager

        logger.info("Starting GitHub Projects v2 monitor...")
        sys.stdout.flush()

        # Resume any active review cycles from before restart
        logger.info("Checking for active review cycles to resume...")
        from services.review_cycle import review_cycle_executor

        for project_name in self.config_manager.list_visible_projects():
            project_config = self.config_manager.get_project_config(project_name)
            org = project_config.github['org']

            # Run async resume method
            asyncio.run(review_cycle_executor.resume_active_cycles(project_name, org))

        logger.info("Review cycle recovery complete, starting main monitoring loop...")

        while True:
            try:
                # Get all configured visible projects (exclude hidden/test projects)
                for project_name in self.config_manager.list_visible_projects():
                    project_config = self.config_manager.get_project_config(project_name)

                    # Get project state to find actual GitHub project numbers
                    project_state = state_manager.load_project_state(project_name)
                    if not project_state:
                        logger.error(f"FATAL: No GitHub state found for project '{project_name}'")
                        logger.error("This indicates GitHub project management failed during reconciliation")
                        logger.error("Project monitoring cannot function without GitHub project state")
                        logger.error("STOPPING PROJECT MONITOR: Core functionality is broken")
                        exit(1)  # Fatal error - stop immediately

                    # Monitor each active board
                    for pipeline in project_config.pipelines:
                        if not pipeline.active:
                            continue

                        # Get board state
                        board_state = project_state.boards.get(pipeline.board_name)
                        if not board_state:
                            logger.error(f"FATAL: No GitHub state found for board '{pipeline.board_name}' in project '{project_name}'")
                            logger.error("This indicates GitHub project board creation failed during reconciliation")
                            logger.error("STOPPING PROJECT MONITOR: GitHub board management is broken")
                            exit(1)  # Fatal error - stop immediately

                        logger.debug(f"Checking {project_config.github['org']} project #{board_state.project_number} ({pipeline.board_name})...")

                        # Get current project items
                        current_items = self.get_project_items(project_config.github['org'], board_state.project_number)

                        if current_items:
                            # Detect changes (use board-specific key for state tracking)
                            board_key = f"{project_name}_{pipeline.board_name}"
                            changes = self.detect_changes(board_key, current_items)

                            if changes:
                                logger.info(f"Detected {len(changes)} changes in {project_name}/{pipeline.board_name}")
                                # Process changes with new system
                                self.process_board_changes(changes, project_name, pipeline.board_name)
                            else:
                                logger.debug(f"No changes in {project_name}/{pipeline.board_name}")

                            # Check all items for feedback comments (in issues)
                            for item in current_items:
                                self.check_for_feedback(
                                    project_name,
                                    pipeline.board_name,
                                    item.issue_number,
                                    item.repository
                                )
                        else:
                            logger.debug(f"No items found in {project_name}/{pipeline.board_name}")

                        # Monitor discussions if pipeline uses discussions workspace
                        if pipeline.workspace in ['discussions', 'hybrid']:
                            self.monitor_discussions(
                                project_name,
                                pipeline.board_name,
                                project_config.github['org'],
                                project_config.github['repo']
                            )

                logger.debug(f"Sleeping for {self.poll_interval} seconds...")
                time.sleep(self.poll_interval)

            except KeyboardInterrupt:
                logger.info("Project monitor stopped")
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                time.sleep(10)  # Wait before retrying

    def check_for_feedback(self, project_name: str, board_name: str, issue_number: int, repository: str):
        """
        DELETED: Old feedback manager - replaced by conversational_loop and review_cycle
        """
        logger.debug("Old feedback manager disabled")
        return

    def _check_for_feedback_OLD_DELETED(self, project_name: str, board_name: str, issue_number: int, repository: str):
        """Check if there are new feedback comments mentioning @orchestrator-bot"""
        try:
            # Get project config
            project_config = self.config_manager.get_project_config(project_name)

            # Find the pipeline config for this board
            pipeline_config = None
            for pipeline in project_config.pipelines:
                if pipeline.board_name == board_name:
                    pipeline_config = pipeline
                    break

            if not pipeline_config:
                return

            # Get workflow template
            workflow_template = self.config_manager.get_workflow_template(pipeline_config.workflow)

            from services.github_integration import GitHubIntegration
            import asyncio

            # Create event loop if needed
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            github = GitHubIntegration()

            # Get all comments to find which agent the user is responding to
            all_feedback_comments = loop.run_until_complete(
                github.get_feedback_comments(issue_number, repository, since_timestamp=None)
            )

            # Filter to unprocessed comments only
            new_feedback = []
            for comment in all_feedback_comments:
                if not self.feedback_manager.is_comment_processed(issue_number, comment['id']):
                    new_feedback.append(comment)

            if not new_feedback:
                return

            # Fetch all issue comments to find the most recent agent output before user feedback
            result = subprocess.run(
                ['gh', 'issue', 'view', str(issue_number), '--repo',
                 f"{project_config.github['org']}/{repository}", '--json', 'comments'],
                capture_output=True, text=True, check=True
            )
            all_comments_data = json.loads(result.stdout)
            all_comments = all_comments_data.get('comments', [])

            # For each new feedback comment, find which agent it's responding to
            from dateutil import parser as date_parser
            from datetime import timezone

            for feedback_comment in new_feedback:
                feedback_time = date_parser.parse(feedback_comment['created_at'])
                if feedback_time.tzinfo is None:
                    feedback_time = feedback_time.replace(tzinfo=timezone.utc)

                # Find the most recent agent comment before this feedback
                target_agent = None
                agent_comment_body = None
                most_recent_agent_time = None

                for comment in all_comments:
                    comment_time = date_parser.parse(comment.get('createdAt'))
                    if comment_time.tzinfo is None:
                        comment_time = comment_time.replace(tzinfo=timezone.utc)

                    # Only consider comments before the feedback
                    if comment_time >= feedback_time:
                        continue

                    # Check if this is an agent comment
                    body = comment.get('body', '')
                    for column in workflow_template.columns:
                        agent = column.agent
                        if agent and agent != 'null':
                            # Look for agent signature in comment
                            if f"_Processed by the {agent} agent_" in body:
                                # Track the most recent agent comment
                                if most_recent_agent_time is None or comment_time > most_recent_agent_time:
                                    target_agent = agent
                                    agent_comment_body = body
                                    most_recent_agent_time = comment_time
                                break

                if target_agent:
                    # If the target agent is a reviewer, feedback should go to the maker agent instead
                    final_target_agent = target_agent
                    if 'reviewer' in target_agent or 'review' in target_agent:
                        # Find the maker agent that this reviewer was reviewing
                        maker_agent = self._find_maker_for_reviewer(
                            target_agent, workflow_template, all_comments, most_recent_agent_time
                        )
                        if maker_agent:
                            logger.info(f"Routing feedback from {target_agent} review to maker agent {maker_agent}")
                            final_target_agent = maker_agent
                            # Get the maker's output as previous_output instead of reviewer's
                            for comment in reversed(all_comments):
                                if f"_Processed by the {maker_agent} agent_" in comment.get('body', ''):
                                    agent_comment_body = comment.get('body', '')
                                    break
                        else:
                            logger.warning(f"Could not find maker for reviewer {target_agent}, sending feedback to reviewer")

                    logger.info(f"Found feedback for {final_target_agent} on issue #{issue_number}")

                    # Create a feedback task for this specific agent
                    self.create_feedback_task(
                        project_name, board_name, issue_number,
                        repository, final_target_agent, [feedback_comment], project_config,
                        previous_output=agent_comment_body
                    )
                else:
                    logger.warning(f"Could not determine which agent to route feedback to for issue #{issue_number}")
                    # Mark as processed anyway to avoid repeated attempts
                    self.feedback_manager.mark_comment_processed(issue_number, feedback_comment['id'], project_name)

        except Exception as e:
            logger.error(f"Error checking for feedback: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _find_maker_for_reviewer(self, reviewer_agent, workflow_template, all_comments, reviewer_time):
        """Find the maker agent that a reviewer was reviewing"""
        from dateutil import parser as date_parser
        from datetime import timezone

        # Look backwards through comments before the reviewer's comment
        # to find the most recent non-reviewer agent
        most_recent_maker = None
        most_recent_maker_time = None

        for comment in all_comments:
            comment_time = date_parser.parse(comment.get('createdAt'))
            if comment_time.tzinfo is None:
                comment_time = comment_time.replace(tzinfo=timezone.utc)

            # Only consider comments before the reviewer
            if comment_time >= reviewer_time:
                continue

            # Check if this is an agent comment
            body = comment.get('body', '')
            for column in workflow_template.columns:
                agent = column.agent
                if agent and agent != 'null':
                    # Look for agent signature
                    if f"_Processed by the {agent} agent_" in body:
                        # Skip if it's also a reviewer
                        if 'reviewer' not in agent and 'review' not in agent:
                            # Track the most recent maker
                            if most_recent_maker_time is None or comment_time > most_recent_maker_time:
                                most_recent_maker = agent
                                most_recent_maker_time = comment_time
                        break

        return most_recent_maker

    def create_feedback_task(self, project_name: str, board_name: str, issue_number: int,
                            repository: str, agent: str, feedback_comments: List[Dict[str, Any]],
                            project_config, previous_output: str = None):
        """Create a task to handle feedback for an agent"""
        try:
            # Fetch full issue details
            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

            # DEFENSIVE: Check if issue is open before creating feedback task
            issue_state = issue_data.get('state', '').upper()
            if issue_state == 'CLOSED':
                logger.info(f"Skipping feedback task for issue #{issue_number}: issue is CLOSED")
                return

            # Prepare feedback context
            feedback_text = "\n\n".join([
                f"**Feedback from @{comment['author']} at {comment['created_at']}:**\n{comment['body']}"
                for comment in feedback_comments
            ])

            # Create task context with feedback
            task_context = {
                'project': project_name,
                'board': board_name,
                'pipeline': board_name,  # Simplified
                'repository': repository,
                'issue_number': issue_number,
                'issue': issue_data,
                'column': 'feedback',
                'trigger': 'feedback_loop',
                'feedback': {
                    'comments': feedback_comments,
                    'formatted_text': feedback_text
                },
                'previous_output': previous_output,  # Include agent's previous work
                'timestamp': utc_isoformat()
            }

            task = Task(
                id=f"{agent}_feedback_{project_name}_{board_name}_{issue_number}_{int(time.time())}",
                agent=agent,
                project=project_name,
                priority=TaskPriority.HIGH,  # Feedback gets high priority
                context=task_context,
                created_at=utc_isoformat()
            )

            self.task_queue.enqueue(task)

            # Mark comments as processed
            for comment in feedback_comments:
                self.feedback_manager.mark_comment_processed(issue_number, comment['id'], project_name)

            logger.info(f"Created feedback task for {agent} on issue #{issue_number}")

        except Exception as e:
            logger.error(f"Failed to create feedback task: {e}")

    def process_board_changes(self, changes: List[Dict[str, Any]], project_name: str, board_name: str):
        """Process detected changes for a specific board"""
        for change in changes:
            logger.info(f"{change['type']}: #{change['issue_number']} - {change['title']}")

            if change['type'] == 'status_changed':
                logger.info(f"   Status: {change['old_status']} → {change['new_status']}")
                logger.info(f"   Board: {board_name}")

                # Record status change in work execution state (only if not programmatic)
                from services.work_execution_state import work_execution_tracker
                
                # Check if this was a recent programmatic change to avoid duplicate recording
                was_programmatic = work_execution_tracker.was_recent_programmatic_change(
                    project_name=project_name,
                    issue_number=change['issue_number'],
                    to_status=change['new_status'],
                    time_window_seconds=60
                )
                
                if not was_programmatic:
                    # Only record if this appears to be a manual status change
                    work_execution_tracker.record_status_change(
                        issue_number=change['issue_number'],
                        from_status=change['old_status'],
                        to_status=change['new_status'],
                        trigger='manual',  # Status changes from GitHub polling are manual
                        project_name=project_name
                    )
                else:
                    logger.debug(
                        f"Skipping duplicate status_change recording for #{change['issue_number']} "
                        f"({change['old_status']} → {change['new_status']}) - already recorded programmatically"
                    )

                # Check if this issue needs a discussion created BEFORE triggering agent
                # (Important for discussions workspaces)
                self._check_and_create_discussion(
                    project_name,
                    board_name,
                    change['issue_number'],
                    change['repository'],
                    change.get('new_status')  # Pass the new status for safety check
                )

                self.trigger_agent_for_status(
                    project_name,
                    board_name,
                    change['issue_number'],
                    change['new_status'],
                    change['repository']
                )
            elif change['type'] == 'item_added':
                logger.info(f"   Added to: {change['status']}")
                logger.info(f"   Board: {board_name}")

                # Check if this issue needs a discussion created
                self._check_and_create_discussion(
                    project_name,
                    board_name,
                    change['issue_number'],
                    change['repository'],
                    change.get('status')  # Pass the status for safety check
                )

                self.trigger_agent_for_status(
                    project_name,
                    board_name,
                    change['issue_number'],
                    change['status'],
                    change['repository']
                )

    def _check_and_create_discussion(self, project_name: str, board_name: str,
                                     issue_number: int, repository: str, status: Optional[str] = None):
        """Check if issue needs a discussion and create it if configured"""
        try:
            from config.state_manager import state_manager

            # Check if discussion already exists for this issue
            if state_manager.get_discussion_for_issue(project_name, issue_number):
                logger.debug(f"Discussion already exists for issue #{issue_number}")
                return

            # Get project config
            project_config = self.config_manager.get_project_config(project_name)

            # Find pipeline config
            pipeline_config = None
            for pipeline in project_config.pipelines:
                if pipeline.board_name == board_name:
                    pipeline_config = pipeline
                    break

            if not pipeline_config:
                return

            # Check if this pipeline uses discussions
            workspace = pipeline_config.workspace
            if workspace not in ['discussions', 'hybrid']:
                return

            # Check if auto-creation is enabled (default to True for discussion workspaces)
            auto_create = getattr(pipeline_config, 'auto_create_from_issues', True)
            if not auto_create:
                return

            # SAFETY LATCH: Only create discussions for items in Backlog column of Planning & Design board
            # This prevents erroneous discussion creation when sub-issues are linked to parent issues
            if status and status.lower() != 'backlog':
                logger.debug(f"Skipping discussion creation for issue #{issue_number} - not in Backlog column (current: {status})")
                return

            logger.info(f"Creating discussion for issue #{issue_number} (workspace: {workspace})")

            # Create the discussion
            self._create_discussion_from_issue(
                project_name,
                issue_number,
                repository,
                pipeline_config,
                project_config
            )

        except Exception as e:
            logger.error(f"Error checking/creating discussion: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _create_discussion_from_issue(self, project_name: str, issue_number: int,
                                     repository: str, pipeline_config, project_config):
        """Auto-create discussion from issue"""
        try:
            # Fetch issue details
            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

            # Get discussion category
            # Use first discussion stage if available, otherwise 'initial'
            stage = (pipeline_config.discussion_stages[0]
                    if hasattr(pipeline_config, 'discussion_stages') and pipeline_config.discussion_stages
                    else 'initial')
            workspace_type, category_id = self.workspace_router.determine_workspace(
                project_name,
                pipeline_config.board_name,
                stage
            )

            if not category_id:
                logger.warning(f"Could not determine discussion category for {project_name}/{pipeline_config.board_name} (GitHub App not configured)")
                return

            # Get repository ID for GraphQL
            repo_id = self.discussions.get_repository_id(
                project_config.github['org'],
                repository
            )

            if not repo_id:
                logger.error(f"Could not get repository ID for {project_config.github['org']}/{repository}")
                return

            # Format discussion title
            title_prefix = getattr(pipeline_config, 'discussion_title_prefix', 'Requirements: ')
            discussion_title = f"{title_prefix}{issue_data['title']}"

            # Format discussion body
            discussion_body = self._format_discussion_from_issue(issue_data, issue_number)

            # Create discussion
            discussion_id = self.discussions.create_discussion(
                owner=project_config.github['org'],
                repo=repository,
                repository_id=repo_id,
                category_id=category_id,
                title=discussion_title,
                body=discussion_body
            )

            if not discussion_id:
                logger.error(f"Failed to create discussion for issue #{issue_number}")
                return

            # Get discussion number for user-friendly reference
            discussion_details = self.discussions.get_discussion(discussion_id)
            discussion_number = discussion_details.get('number') if discussion_details else '?'

            logger.info(f"Created discussion #{discussion_number} (ID: {discussion_id}) for issue #{issue_number}")

            # Store link in state
            from config.state_manager import state_manager
            state_manager.link_issue_to_discussion(project_name, issue_number, discussion_id)

            # Add comment to issue linking to discussion
            discussion_url = discussion_details.get('url', '') if discussion_details else ''
            comment_body = f"""📋 Requirements analysis moved to Discussion #{discussion_number}

This issue will be updated with final requirements when ready for implementation.

_Link: {discussion_url}_"""

            subprocess.run(
                ['gh', 'issue', 'comment', str(issue_number),
                 '--repo', f"{project_config.github['org']}/{repository}",
                 '--body', comment_body],
                capture_output=True, text=True, check=True
            )

            logger.info(f"Added link comment to issue #{issue_number}")

        except Exception as e:
            logger.error(f"Failed to create discussion from issue: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _format_discussion_from_issue(self, issue_data: Dict[str, Any], issue_number: int) -> str:
        """Format discussion body from issue data"""
        labels = ', '.join([label['name'] for label in issue_data.get('labels', [])])
        author = issue_data.get('author', {}).get('login', 'unknown')

        return f"""# Requirements Analysis

Auto-created from Issue #{issue_number}

## User Request

{issue_data.get('body', '_No description provided_')}

---

**Labels**: {labels if labels else '_None_'}
**Requested by**: @{author}

---

The orchestrator will analyze this request and develop detailed requirements.
When complete, Issue #{issue_number} will be updated with final requirements.
"""

    def finalize_requirements_to_issue(self, project_name: str, board_name: str,
                                      issue_number: int, repository: str,
                                      discussion_id: Optional[str] = None):
        """
        Extract final requirements from discussion and update issue body.
        Called when requirements are approved or at transition stage.
        """
        try:
            from config.state_manager import state_manager

            # Get project config
            project_config = self.config_manager.get_project_config(project_name)

            # Get discussion ID from state if not provided
            if not discussion_id:
                discussion_id = state_manager.get_discussion_for_issue(project_name, issue_number)
                if not discussion_id:
                    logger.error(f"No discussion found for issue #{issue_number}")
                    return

            logger.info(f"Finalizing requirements from discussion to issue #{issue_number}")

            # Get full discussion with all comments
            discussion = self.discussions.get_discussion(discussion_id)
            if not discussion:
                logger.error(f"Could not retrieve discussion {discussion_id}")
                return

            # Extract requirements from agent comments
            requirements = self._extract_requirements_from_discussion(discussion_id, project_config, repository)

            if not requirements:
                logger.warning(f"No requirements found in discussion for issue #{issue_number}")
                return

            # Format new issue body
            discussion_number = discussion.get('number', '?')
            discussion_url = discussion.get('url', '')
            new_issue_body = self._format_finalized_requirements(
                discussion_number,
                discussion_url,
                requirements
            )

            # Update issue body
            result = subprocess.run(
                ['gh', 'issue', 'edit', str(issue_number),
                 '--repo', f"{project_config.github['org']}/{repository}",
                 '--body', new_issue_body],
                capture_output=True, text=True, check=True
            )

            logger.info(f"Updated issue #{issue_number} with finalized requirements")

            # Add "ready-for-implementation" label
            subprocess.run(
                ['gh', 'issue', 'edit', str(issue_number),
                 '--repo', f"{project_config.github['org']}/{repository}",
                 '--add-label', 'ready-for-implementation'],
                capture_output=True, text=True
            )

            # Post completion comment to discussion
            completion_comment = f"""✅ Requirements finalized and posted to Issue #{issue_number}

The issue has been updated with the final requirements from this discussion.
Moving to implementation phase.

[View Issue →]({discussion.get('url', '').replace(f'/discussions/{discussion_number}', f'/issues/{issue_number}')})"""

            self.discussions.add_discussion_comment(discussion_id, completion_comment)

            logger.info(f"Finalization complete for issue #{issue_number}")

        except Exception as e:
            logger.error(f"Failed to finalize requirements to issue: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _extract_requirements_from_discussion(self, discussion_id: str,
                                             project_config, repository: str) -> Dict[str, Any]:
        """
        Extract structured requirements from discussion comments.
        Looks for outputs from business_analyst, architect, and other agents.
        """
        try:
            # Get full discussion with comments
            org = project_config.github['org']

            # Use GraphQL to get discussion with all comments
            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
                  number
                  comments(first: 100) {
                    nodes {
                      id
                      body
                      author {
                        login
                      }
                      createdAt
                    }
                  }
                }
              }
            }
            """

            from services.github_app import github_app
            result = github_app.graphql_request(query, {'discussionId': discussion_id})

            if not result or 'node' not in result:
                logger.error(f"Failed to get discussion comments for {discussion_id}")
                return {}

            comments = result['node']['comments']['nodes']

            # Extract agent outputs
            requirements = {
                'executive_summary': '',
                'functional': [],
                'non_functional': [],
                'user_stories': [],
                'architecture': '',
                'acceptance_criteria': []
            }

            # Find the most recent business analyst output
            ba_output = None
            architect_output = None

            for comment in reversed(comments):
                body = comment.get('body', '')
                author = comment.get('author', {}).get('login', '')

                # Look for agent signatures
                if '_Processed by the business_analyst agent_' in body:
                    if not ba_output:
                        ba_output = body
                elif '_Processed by the software_architect agent_' in body:
                    if not architect_output:
                        architect_output = body

            # Parse business analyst output
            if ba_output:
                requirements['executive_summary'] = self._extract_section(ba_output, 'Executive Summary', 'Functional Requirements')
                requirements['functional'] = self._extract_list_items(ba_output, 'Functional Requirements')
                requirements['non_functional'] = self._extract_list_items(ba_output, 'Non-Functional Requirements')
                requirements['user_stories'] = self._extract_user_stories(ba_output)
                requirements['acceptance_criteria'] = self._extract_list_items(ba_output, 'Acceptance Criteria')

            # Parse architect output
            if architect_output:
                requirements['architecture'] = self._extract_section(architect_output, 'Architecture Overview', 'Component Design')

            return requirements

        except Exception as e:
            logger.error(f"Error extracting requirements from discussion: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {}

    def _extract_section(self, text: str, start_marker: str, end_marker: str) -> str:
        """Extract text between two section markers"""
        try:
            start = text.find(f"## {start_marker}")
            if start == -1:
                start = text.find(f"### {start_marker}")
            if start == -1:
                return ""

            end = text.find(f"## {end_marker}", start + 1)
            if end == -1:
                end = text.find(f"### {end_marker}", start + 1)
            if end == -1:
                end = len(text)

            section = text[start:end].strip()
            # Remove the header line
            lines = section.split('\n', 1)
            return lines[1].strip() if len(lines) > 1 else ""
        except Exception:
            return ""

    def _extract_list_items(self, text: str, section_name: str) -> List[str]:
        """Extract bullet point list items from a section"""
        try:
            section_text = self._extract_section(text, section_name, '---')
            if not section_text:
                return []

            items = []
            for line in section_text.split('\n'):
                line = line.strip()
                if line.startswith('- ') or line.startswith('* '):
                    items.append(line[2:].strip())
                elif line.startswith('• '):
                    items.append(line[2:].strip())
            return items
        except Exception:
            return []

    def _extract_user_stories(self, text: str) -> List[str]:
        """Extract user stories from business analyst output"""
        try:
            stories = []
            # Look for "As a..." patterns
            lines = text.split('\n')
            current_story = []

            for line in lines:
                line = line.strip()
                if line.startswith('**As a') or line.startswith('As a'):
                    if current_story:
                        stories.append(' '.join(current_story))
                    current_story = [line]
                elif current_story and (line.startswith('I want') or line.startswith('So that')):
                    current_story.append(line)
                elif current_story and not line:
                    stories.append(' '.join(current_story))
                    current_story = []

            if current_story:
                stories.append(' '.join(current_story))

            return stories
        except Exception:
            return []

    def _format_finalized_requirements(self, discussion_number: int, discussion_url: str,
                                      requirements: Dict[str, Any]) -> str:
        """Format the finalized requirements for issue body"""
        parts = []

        # Executive summary
        if requirements.get('executive_summary'):
            parts.append(requirements['executive_summary'])
            parts.append('')

        # Background section
        parts.append('## Background')
        parts.append(f'Full requirements analysis available in [Discussion #{discussion_number}]({discussion_url})')
        parts.append('')

        # Functional requirements
        if requirements.get('functional'):
            parts.append('## Functional Requirements')
            for item in requirements['functional']:
                parts.append(f'- {item}')
            parts.append('')

        # Non-functional requirements
        if requirements.get('non_functional'):
            parts.append('## Non-Functional Requirements')
            for item in requirements['non_functional']:
                parts.append(f'- {item}')
            parts.append('')

        # User stories
        if requirements.get('user_stories'):
            parts.append('## User Stories')
            for story in requirements['user_stories']:
                parts.append(f'- {story}')
            parts.append('')

        # Architecture notes
        if requirements.get('architecture'):
            parts.append('## Architecture Notes')
            parts.append(requirements['architecture'])
            parts.append('')

        # Acceptance criteria
        if requirements.get('acceptance_criteria'):
            parts.append('## Acceptance Criteria')
            for item in requirements['acceptance_criteria']:
                parts.append(f'- {item}')
            parts.append('')

        # Footer
        parts.append('---')
        parts.append(f'📋 Requirements finalized from [Discussion #{discussion_number}]({discussion_url})')
        parts.append('Ready for implementation.')
        parts.append('---')

        return '\n'.join(parts)

    def monitor_discussions(self, project_name: str, board_name: str, org: str, repo: str):
        """Monitor discussions for activity and feedback"""
        try:
            from config.state_manager import state_manager

            # Get project state to find linked discussions
            project_state = state_manager.load_project_state(project_name)
            if not project_state or not project_state.issue_discussion_links:
                logger.debug(f"No discussions linked for {project_name}")
                return

            # Get recent discussions (updated in last poll interval * 2)
            from datetime import datetime, timedelta, timezone
            since = datetime.now(timezone.utc) - timedelta(seconds=self.poll_interval * 2)

            # Check each linked discussion for new activity
            for issue_number, discussion_id in list(project_state.issue_discussion_links.items()):
                try:
                    # Get discussion details
                    discussion = self.discussions.get_discussion(discussion_id)
                    if not discussion:
                        # Discussion was deleted - remove from state
                        from config.state_manager import state_manager
                        logger.info(f"Discussion {discussion_id} for issue #{issue_number} no longer exists, removing from state")
                        state_manager.unlink_issue_discussion(project_name, int(issue_number))
                        continue

                    # Check if discussion has been updated recently
                    updated_at = datetime.fromisoformat(discussion['updatedAt'].replace('Z', '+00:00'))
                    if updated_at < since:
                        continue  # No recent activity

                    logger.debug(f"Checking discussion #{discussion.get('number')} for new activity")

                    # Check for feedback in discussion comments
                    self.check_for_feedback_in_discussion(
                        project_name,
                        board_name,
                        discussion_id,
                        issue_number,
                        repo
                    )

                except Exception as e:
                    logger.error(f"Error monitoring discussion {discussion_id}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Error in monitor_discussions: {e}")
            import traceback
            logger.error(traceback.format_exc())


    def get_full_thread_history(self, all_comments: List[Dict], parent_comment_id: str) -> List[Dict]:
        """
        Extract complete thread history for conversational context

        Args:
            all_comments: All comments from the discussion (with replies)
            parent_comment_id: The ID of the parent comment to extract thread from

        Returns:
            List of messages in chronological order with role, author, body, timestamp
        """
        thread_history = []

        # Find the parent comment
        for comment in all_comments:
            if comment['id'] == parent_comment_id:
                # Add parent comment (the agent's initial output or previous reply)
                author_login = comment.get('author', {}).get('login', 'unknown')
                is_bot = 'bot' in author_login.lower()

                thread_history.append({
                    'role': 'agent' if is_bot else 'user',
                    'author': author_login,
                    'body': comment.get('body', ''),
                    'timestamp': comment.get('createdAt'),
                    'is_agent': is_bot
                })

                # Add all replies in chronological order
                for reply in comment.get('replies', {}).get('nodes', []):
                    reply_author = reply.get('author', {})
                    reply_author_login = reply_author.get('login', '') if reply_author else 'unknown'
                    reply_is_bot = 'bot' in reply_author_login.lower()

                    thread_history.append({
                        'role': 'agent' if reply_is_bot else 'user',
                        'author': reply_author_login,
                        'body': reply.get('body', ''),
                        'timestamp': reply.get('createdAt'),
                        'is_agent': reply_is_bot
                    })

                break

        return thread_history

    def check_for_feedback_in_discussion(self, project_name: str, board_name: str,
                                         discussion_id: str, issue_number: int, repository: str):
        """
        Check if there's an escalated review cycle waiting for human feedback on this discussion.
        If so, check for new human comments and resume the cycle.

        Note: Regular feedback for conversational columns is handled by human_feedback_loop.
        This method only checks for escalated review cycles.
        """
        try:
            from services.review_cycle import review_cycle_executor
            from services.github_app import github_app
            from dateutil import parser as date_parser
            from datetime import datetime

            # Check if there's an escalated cycle for this issue (in-memory)
            if issue_number not in review_cycle_executor.active_cycles:
                return

            cycle_state = review_cycle_executor.active_cycles[issue_number]
            if cycle_state.status != 'awaiting_human_feedback':
                return

            # There's an escalated cycle waiting for feedback on this discussion
            logger.debug(f"Checking for human feedback on escalated cycle for issue #{issue_number}")

            # Get project config for org
            project_config = self.config_manager.get_project_config(project_name)
            org = project_config.github['org']

            # Query for recent comments
            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
                  comments(last: 20) {
                    nodes {
                      id
                      author {
                        login
                      }
                      body
                      createdAt
                    }
                  }
                }
              }
            }
            """

            result = github_app.graphql_request(query, {'discussionId': discussion_id})

            if result and 'node' in result and result['node']:
                comments = result['node']['comments']['nodes']

                # Get escalation time
                escalation_time = datetime.fromisoformat(cycle_state.escalation_time)
                if escalation_time.tzinfo:
                    escalation_time = escalation_time.replace(tzinfo=None)

                # Look for human feedback after escalation
                human_feedback = None
                for comment in reversed(comments):  # Most recent first
                    author = comment['author']['login']
                    created_at = date_parser.parse(comment['createdAt'])

                    # Convert to naive datetime
                    if created_at.tzinfo:
                        created_at = created_at.replace(tzinfo=None)

                    # Check if this is a human comment after escalation
                    if author != 'orchestrator-bot' and created_at > escalation_time:
                        human_feedback = comment['body']
                        logger.info(
                            f"Human feedback detected for escalated cycle #{issue_number} "
                            f"from {author}, resuming review cycle..."
                        )
                        break

                if human_feedback:
                    # Human feedback detected! Resume the review cycle in background thread
                    import asyncio
                    import threading

                    def resume_cycle_in_thread():
                        """Resume review cycle in background thread (non-blocking)"""
                        try:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)

                            loop.run_until_complete(
                                review_cycle_executor.resume_review_cycle_with_feedback(
                                    cycle_state=cycle_state,
                                    human_feedback=human_feedback,
                                    org=org
                                )
                            )
                            loop.close()

                            logger.info(f"Review cycle #{issue_number} resumed successfully")
                        except Exception as e:
                            logger.error(f"Error resuming review cycle #{issue_number}: {e}")
                            import traceback
                            logger.error(traceback.format_exc())

                    # Start in background thread
                    thread = threading.Thread(target=resume_cycle_in_thread, daemon=True)
                    thread.start()

                    logger.info(f"Review cycle resume thread started for issue #{issue_number}")

        except Exception as e:
            logger.error(f"Error checking escalated cycle for issue #{issue_number}: {e}")
            import traceback
            logger.error(traceback.format_exc())

        # OLD CODE BELOW (disabled)
        """Check discussion comments for user feedback mentioning @orchestrator-bot"""
        try:
            from config.state_manager import state_manager
            from dateutil import parser as date_parser
            from datetime import timezone

            # Get project config
            project_config = self.config_manager.get_project_config(project_name)

            # Find the pipeline config for this board
            pipeline_config = None
            for pipeline in project_config.pipelines:
                if pipeline.board_name == board_name:
                    pipeline_config = pipeline
                    break

            if not pipeline_config:
                return

            # Get workflow template
            workflow_template = self.config_manager.get_workflow_template(pipeline_config.workflow)

            # Get discussion with all comments and replies
            from services.github_app import github_app
            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
                  number
                  comments(first: 100) {
                    nodes {
                      id
                      body
                      author {
                        login
                        ... on User {
                          login
                        }
                        ... on Bot {
                          login
                        }
                      }
                      createdAt
                      replies(first: 50) {
                        nodes {
                          id
                          body
                          author {
                            login
                          }
                          createdAt
                        }
                      }
                    }
                  }
                }
              }
            }
            """

            result = github_app.graphql_request(query, {'discussionId': discussion_id})
            if not result or 'node' not in result:
                return

            all_comments = result['node']['comments']['nodes']

            # Find new feedback comments/replies (mentioning @orchestrator-bot from non-bot users)
            new_feedback = []

            for comment in all_comments:
                # Check top-level comment
                comment_id = comment['id']
                body = comment.get('body', '')
                author = comment.get('author', {})
                author_login = author.get('login', '') if author else ''

                # Skip bot comments
                if 'bot' not in author_login.lower():
                    # Check if this comment mentions the bot
                    if '@orchestrator-bot' in body:
                        # Check if we've already processed this comment
                        if not self.feedback_manager.is_comment_processed(issue_number, comment_id):
                            new_feedback.append({
                                'id': comment_id,
                                'body': body,
                                'author': author_login,
                                'created_at': comment['createdAt'],
                                'parent_comment_id': None,  # Top-level comment
                                'is_reply': False
                            })

                # Check replies to this comment
                for reply in comment.get('replies', {}).get('nodes', []):
                    reply_id = reply['id']
                    reply_body = reply.get('body', '')
                    reply_author = reply.get('author', {})
                    reply_author_login = reply_author.get('login', '') if reply_author else ''

                    # Skip bot replies
                    if 'bot' in reply_author_login.lower():
                        continue

                    # Check if this reply mentions the bot
                    if '@orchestrator-bot' not in reply_body:
                        continue

                    # Check if we've already processed this reply
                    if self.feedback_manager.is_comment_processed(issue_number, reply_id):
                        continue

                    new_feedback.append({
                        'id': reply_id,
                        'body': reply_body,
                        'author': reply_author_login,
                        'created_at': reply['createdAt'],
                        'parent_comment_id': comment_id,  # This is a reply to a comment
                        'is_reply': True
                    })

            if not new_feedback:
                return

            # For each feedback comment/reply, determine which agent to route to
            for feedback_comment in new_feedback:
                feedback_time = date_parser.parse(feedback_comment['created_at'])
                if feedback_time.tzinfo is None:
                    feedback_time = feedback_time.replace(tzinfo=timezone.utc)

                target_agent = None
                agent_comment_body = None
                agent_comment_id = None
                most_recent_agent_time = None

                # If this is a reply to a comment, check if the parent is an agent comment
                if feedback_comment['is_reply'] and feedback_comment['parent_comment_id']:
                    parent_id = feedback_comment['parent_comment_id']

                    # Find the parent comment
                    for comment in all_comments:
                        if comment['id'] == parent_id:
                            # Check if parent is an agent comment
                            body = comment.get('body', '')
                            for column in workflow_template.columns:
                                agent = column.agent
                                if agent and agent != 'null':
                                    if f"_Processed by the {agent} agent_" in body:
                                        target_agent = agent
                                        agent_comment_body = body
                                        agent_comment_id = parent_id
                                        logger.info(f"Reply is threaded to {agent} agent comment")
                                        break
                            break

                # If no agent found (not a reply or parent wasn't agent), use chronological search
                if not target_agent:
                    for comment in all_comments:
                        comment_time = date_parser.parse(comment.get('createdAt'))
                        if comment_time.tzinfo is None:
                            comment_time = comment_time.replace(tzinfo=timezone.utc)

                        # Only consider comments before the feedback
                        if comment_time >= feedback_time:
                            continue

                        # Check if this is an agent comment
                        body = comment.get('body', '')
                        for column in workflow_template.columns:
                            agent = column.agent
                            if agent and agent != 'null':
                                # Look for agent signature in comment
                                if f"_Processed by the {agent} agent_" in body:
                                    # Track the most recent agent comment
                                    if most_recent_agent_time is None or comment_time > most_recent_agent_time:
                                        target_agent = agent
                                        agent_comment_body = body
                                        agent_comment_id = comment['id']
                                        most_recent_agent_time = comment_time
                                    break

                if target_agent:
                    # If the target agent is a reviewer, feedback should go to the maker agent instead
                    final_target_agent = target_agent
                    if 'reviewer' in target_agent or 'review' in target_agent:
                        # Find the maker agent that this reviewer was reviewing
                        maker_agent = self._find_maker_for_reviewer_in_discussion(
                            target_agent, workflow_template, all_comments, most_recent_agent_time
                        )
                        if maker_agent:
                            logger.info(f"Routing feedback from {target_agent} review to maker agent {maker_agent}")
                            final_target_agent = maker_agent
                            # Get the maker's output as previous_output instead of reviewer's
                            for comment in reversed(all_comments):
                                if f"_Processed by the {maker_agent} agent_" in comment.get('body', ''):
                                    agent_comment_body = comment.get('body', '')
                                    break
                        else:
                            logger.warning(f"Could not find maker for reviewer {target_agent}, sending feedback to reviewer")

                    logger.info(f"Found feedback for {final_target_agent} in discussion for issue #{issue_number}")

                    # Extract full thread history if this is a threaded reply
                    thread_history = []
                    conversation_mode = None

                    if feedback_comment['is_reply'] and feedback_comment['parent_comment_id']:
                        thread_history = self.get_full_thread_history(
                            all_comments,
                            feedback_comment['parent_comment_id']
                        )
                        conversation_mode = 'threaded'
                        logger.info(f"Extracted thread history with {len(thread_history)} messages for conversational mode")

                    # Create a feedback task for this specific agent
                    self.create_feedback_task_for_discussion(
                        project_name, board_name, issue_number,
                        repository, final_target_agent, [feedback_comment], project_config,
                        discussion_id, previous_output=agent_comment_body,
                        reply_to_comment_id=agent_comment_id,  # For threaded replies
                        thread_history=thread_history,  # For conversational mode
                        conversation_mode=conversation_mode  # Signal conversational mode
                    )
                else:
                    logger.warning(f"Could not determine which agent to route feedback to in discussion for issue #{issue_number}")
                    # Mark as processed anyway to avoid repeated attempts
                    self.feedback_manager.mark_comment_processed(issue_number, feedback_comment['id'], project_name)

        except Exception as e:
            logger.error(f"Error checking for feedback in discussion: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _find_maker_for_reviewer_in_discussion(self, reviewer_agent, workflow_template, all_comments, reviewer_time):
        """Find the maker agent that a reviewer was reviewing (for discussions)"""
        from dateutil import parser as date_parser
        from datetime import timezone

        # Look backwards through comments before the reviewer's comment
        # to find the most recent non-reviewer agent
        most_recent_maker = None
        most_recent_maker_time = None

        for comment in all_comments:
            comment_time = date_parser.parse(comment.get('createdAt'))
            if comment_time.tzinfo is None:
                comment_time = comment_time.replace(tzinfo=timezone.utc)

            # Only consider comments before the reviewer
            if comment_time >= reviewer_time:
                continue

            # Check if this is an agent comment
            body = comment.get('body', '')
            for column in workflow_template.columns:
                agent = column.agent
                if agent and agent != 'null':
                    # Look for agent signature
                    if f"_Processed by the {agent} agent_" in body:
                        # Skip if it's also a reviewer
                        if 'reviewer' not in agent and 'review' not in agent:
                            # Track the most recent maker
                            if most_recent_maker_time is None or comment_time > most_recent_maker_time:
                                most_recent_maker = agent
                                most_recent_maker_time = comment_time
                        break

        return most_recent_maker

    def create_feedback_task_for_discussion(self, project_name: str, board_name: str, issue_number: int,
                                           repository: str, agent: str, feedback_comments: List[Dict[str, Any]],
                                           project_config, discussion_id: str, previous_output: str = None,
                                           reply_to_comment_id: str = None, thread_history: List[Dict] = None,
                                           conversation_mode: str = None):
        """Create a task to handle feedback for an agent (from discussion)"""
        try:
            # Fetch full issue details
            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

            # DEFENSIVE: Check if issue is open before creating feedback task
            issue_state = issue_data.get('state', '').upper()
            if issue_state == 'CLOSED':
                logger.info(f"Skipping feedback task for issue #{issue_number}: issue is CLOSED")
                return

            # Prepare feedback context
            feedback_text = "\n\n".join([
                f"**Feedback from @{comment['author']} at {comment['created_at']}:**\n{comment['body']}"
                for comment in feedback_comments
            ])

            # Create task context with feedback and discussion info
            task_context = {
                'project': project_name,
                'board': board_name,
                'pipeline': board_name,
                'repository': repository,
                'issue_number': issue_number,
                'issue': issue_data,
                'column': 'feedback',
                'trigger': 'feedback_loop',
                'workspace_type': 'discussions',
                'discussion_id': discussion_id,
                'reply_to_comment_id': reply_to_comment_id,  # For threaded replies
                'conversation_mode': conversation_mode,  # 'threaded' for conversational mode
                'thread_history': thread_history or [],  # Full conversation history
                'feedback': {
                    'comments': feedback_comments,
                    'formatted_text': feedback_text
                },
                'previous_output': previous_output,
                'timestamp': utc_isoformat()
            }

            from task_queue.task_manager import Task, TaskPriority
            task = Task(
                id=f"{agent}_feedback_{project_name}_{board_name}_{issue_number}_{int(time.time())}",
                agent=agent,
                project=project_name,
                priority=TaskPriority.HIGH,
                context=task_context,
                created_at=utc_isoformat()
            )

            self.task_queue.enqueue(task)

            # Mark comments as processed
            for comment in feedback_comments:
                self.feedback_manager.mark_comment_processed(issue_number, comment['id'], project_name)

            logger.info(f"Created feedback task for {agent} on discussion for issue #{issue_number}")

        except Exception as e:
            logger.error(f"Failed to create feedback task for discussion: {e}")

if __name__ == "__main__":
    # Initialize task queue and start monitoring
    task_queue = TaskQueue()
    monitor = ProjectMonitor(task_queue)
    monitor.monitor_projects()