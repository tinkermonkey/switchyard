#!/usr/bin/env python3

import time
import json
import subprocess
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
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

        # Get polling interval from first project's orchestrator config (for now)
        projects = self.config_manager.list_projects()
        if projects:
            first_project = self.config_manager.get_project_config(projects[0])
            self.poll_interval = first_project.orchestrator.get("polling_interval", 30)
        else:
            self.poll_interval = 30

    def get_project_items(self, project_owner: str, project_number: int) -> List[ProjectItem]:
        """Query GitHub Projects v2 API to get current project items"""
        query = f'''{{
            user(login: "{project_owner}") {{
                projectV2(number: {project_number}) {{
                    id
                    title
                    items(first: 100) {{
                        nodes {{
                            id
                            content {{
                                ... on Issue {{
                                    id
                                    number
                                    title
                                    repository {{
                                        name
                                    }}
                                    updatedAt
                                }}
                            }}
                            fieldValues(first: 10) {{
                                nodes {{
                                    ... on ProjectV2ItemFieldSingleSelectValue {{
                                        name
                                        field {{
                                            ... on ProjectV2SingleSelectField {{
                                                name
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}
            }}
        }}'''

        try:
            result = subprocess.run(
                ['gh', 'api', 'graphql', '-f', f'query={query}'],
                capture_output=True, text=True, check=True
            )

            data = json.loads(result.stdout)
            project_data = data['data']['user']['projectV2']

            items = []
            for node in project_data['items']['nodes']:
                content = node.get('content')
                if not content:  # Skip draft items
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
                                   current_column: str, workflow_template) -> str:
        """
        Fetch comments from the previous workflow stage agent and user comments since then.
        Returns formatted context string.
        """
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

            # Fetch all comments
            result = subprocess.run(
                ['gh', 'issue', 'view', str(issue_number), '--repo', f"{org}/{repository}", '--json', 'comments'],
                capture_output=True, text=True, check=True
            )
            data = json.loads(result.stdout)

            # Find the last comment from the previous agent
            previous_agent_comment = None
            previous_agent_timestamp = None

            for comment in reversed(data.get('comments', [])):
                if f"_Processed by the {previous_agent} agent_" in comment.get('body', ''):
                    previous_agent_comment = comment
                    previous_agent_timestamp = comment.get('createdAt')
                    break

            if not previous_agent_comment:
                return ""  # Previous agent hasn't processed yet

            # Collect user comments after the agent's comment
            from dateutil import parser as date_parser
            from datetime import timezone

            agent_time = date_parser.parse(previous_agent_timestamp)
            # Make timezone-aware if naive
            if agent_time.tzinfo is None:
                agent_time = agent_time.replace(tzinfo=timezone.utc)

            user_comments = []
            for comment in data.get('comments', []):
                comment_time = date_parser.parse(comment.get('createdAt'))
                # Make timezone-aware if naive
                if comment_time.tzinfo is None:
                    comment_time = comment_time.replace(tzinfo=timezone.utc)

                # Get comments after agent's comment that are from users (not bot)
                if comment_time > agent_time and not comment.get('author', {}).get('isBot', False):
                    user_comments.append(comment)

            # Format context
            context_parts = []
            context_parts.append(f"## Output from {previous_agent.replace('_', ' ').title()}")
            context_parts.append(previous_agent_comment.get('body', ''))

            if user_comments:
                context_parts.append("\n## User Feedback Since Then")
                for comment in user_comments:
                    author = comment.get('author', {}).get('login', 'unknown')
                    body = comment.get('body', '')
                    context_parts.append(f"**@{author}**: {body}")

            return "\n\n".join(context_parts)

        except Exception as e:
            logger.error(f"Error fetching previous stage context: {e}")
            return ""

    def trigger_agent_for_status(self, project_name: str, board_name: str, issue_number: int, status: str, repository: str) -> Optional[str]:
        """Determine which agent should handle this status and create a task"""
        try:
            # Get workflow template for this board
            project_config = self.config_manager.get_project_config(project_name)

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
            for column in workflow_template.columns:
                if column.name == status:
                    agent = column.agent
                    break

            if agent and agent != 'null':
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

                # Check if agent has already processed this issue (synchronous check using subprocess)
                import asyncio
                try:
                    # Check if agent has already processed this issue
                    # Create a new event loop for this thread if needed
                    try:
                        loop = asyncio.get_event_loop()
                    except RuntimeError:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)

                    from services.github_integration import GitHubIntegration
                    github = GitHubIntegration()

                    already_processed = loop.run_until_complete(
                        github.has_agent_processed_issue(issue_number, agent, repository)
                    )

                    if already_processed:
                        logger.info(f"Agent {agent} has already processed issue #{issue_number} - skipping reprocessing")
                        return None
                except Exception as e:
                    logger.warning(f"Could not check if issue was already processed: {e}")
                    # Continue anyway if we can't check

                # Fetch full issue details from GitHub
                issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

                # Fetch context from previous workflow stage
                previous_stage_context = self.get_previous_stage_context(
                    repository, issue_number, project_config.github['org'],
                    status, workflow_template
                )

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
                    'timestamp': datetime.now().isoformat()
                }

                task = Task(
                    id=f"{agent}_{project_name}_{board_name}_{issue_number}_{int(time.time())}",
                    agent=agent,
                    project=project_name,
                    priority=TaskPriority.MEDIUM,
                    context=task_context,
                    created_at=datetime.now().isoformat()
                )

                self.task_queue.enqueue(task)
                logger.info(f"Created task for {agent} - Issue #{issue_number} moved to {status} on {board_name}")
                return agent
            else:
                logger.info(f"No agent assigned to column '{status}' in {board_name}")
                return None

        except Exception as e:
            logger.error(f"Error triggering agent for status: {e}")
            return None


    def monitor_projects(self):
        """Main monitoring loop using new configuration system"""
        import sys
        from config.state_manager import state_manager

        logger.info("Starting GitHub Projects v2 monitor...")
        sys.stdout.flush()

        while True:
            try:
                # Get all configured projects
                for project_name in self.config_manager.list_projects():
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

                            # Check all items for feedback comments
                            for item in current_items:
                                self.check_for_feedback(
                                    project_name,
                                    pipeline.board_name,
                                    item.issue_number,
                                    item.repository
                                )
                        else:
                            logger.debug(f"No items found in {project_name}/{pipeline.board_name}")

                logger.debug(f"Sleeping for {self.poll_interval} seconds...")
                time.sleep(self.poll_interval)

            except KeyboardInterrupt:
                logger.info("Project monitor stopped")
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                time.sleep(10)  # Wait before retrying

    def check_for_feedback(self, project_name: str, board_name: str, issue_number: int, repository: str):
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

            # Find which agent is assigned to current column
            # For now, we'll check all agents that have processed this issue
            from services.github_integration import GitHubIntegration
            import asyncio

            # Create event loop if needed
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            github = GitHubIntegration()

            # Check each agent defined in the workflow
            for column in workflow_template.columns:
                agent = column.agent
                if not agent or agent == 'null':
                    continue

                # Get last time this agent commented
                last_comment_time = self.feedback_manager.get_last_agent_comment_time(issue_number, agent)

                # Get feedback comments since then
                feedback_comments = loop.run_until_complete(
                    github.get_feedback_comments(issue_number, repository, last_comment_time)
                )

                if feedback_comments:
                    # Filter out already processed comments
                    new_feedback = []
                    for comment in feedback_comments:
                        if not self.feedback_manager.is_comment_processed(issue_number, comment['id']):
                            new_feedback.append(comment)

                    if new_feedback:
                        logger.info(f"Found {len(new_feedback)} new feedback comment(s) for {agent} on issue #{issue_number}")

                        # Create a feedback task for the agent
                        self.create_feedback_task(
                            project_name, board_name, issue_number,
                            repository, agent, new_feedback, project_config
                        )
                        # Only process feedback for the first matching agent to avoid duplicates
                        break

        except Exception as e:
            logger.error(f"Error checking for feedback: {e}")

    def create_feedback_task(self, project_name: str, board_name: str, issue_number: int,
                            repository: str, agent: str, feedback_comments: List[Dict[str, Any]],
                            project_config):
        """Create a task to handle feedback for an agent"""
        try:
            # Fetch full issue details
            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

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
                'timestamp': datetime.now().isoformat()
            }

            task = Task(
                id=f"{agent}_feedback_{project_name}_{board_name}_{issue_number}_{int(time.time())}",
                agent=agent,
                project=project_name,
                priority=TaskPriority.HIGH,  # Feedback gets high priority
                context=task_context,
                created_at=datetime.now().isoformat()
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
                self.trigger_agent_for_status(
                    project_name,
                    board_name,
                    change['issue_number'],
                    change['status'],
                    change['repository']
                )

if __name__ == "__main__":
    # Initialize task queue and start monitoring
    task_queue = TaskQueue()
    monitor = ProjectMonitor(task_queue)
    monitor.monitor_projects()