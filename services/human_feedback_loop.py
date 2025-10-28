"""
Human Feedback Loop

Handles conversational workflows where agents respond to human feedback in discussion threads.
Pattern: agent produces output → monitor for human feedback → agent responds to feedback → repeat

For automated maker-checker review workflows, see review_cycle.py instead.
"""

import asyncio
import logging
import re
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
from config.manager import WorkflowColumn
from services.review_parser import ReviewParser, ReviewStatus
from services.github_integration import GitHubIntegration
from services.conversational_session_state import conversational_session_state

logger = logging.getLogger(__name__)

# Regex pattern to match bot usernames (GitHub Apps, Actions, etc.)
# - orchestrator-bot (with or without [bot] suffix) - our bot
# - github-actions[bot] - GitHub Actions (requires [bot] suffix)
# - app/orchestrator-bot - alternative GitHub App format
BOT_USERNAME_PATTERN = re.compile(
    r'^orchestrator-bot(\[bot\])?$|^github-actions\[bot\]$|^app/orchestrator-bot$'
)

def is_bot_user(username: str) -> bool:
    """Check if a username belongs to a bot account"""
    return BOT_USERNAME_PATTERN.match(username) is not None


class HumanFeedbackState:
    """Tracks state for an active human feedback loop"""

    def __init__(
        self,
        issue_number: int,
        repository: str,
        agent: str,
        project_name: str,
        board_name: str,
        workspace_type: str = 'discussions',
        discussion_id: Optional[str] = None,
        pipeline_run_id: Optional[str] = None
    ):
        self.issue_number = issue_number
        self.repository = repository
        self.agent = agent
        self.project_name = project_name
        self.board_name = board_name
        self.workspace_type = workspace_type
        self.discussion_id = discussion_id
        self.pipeline_run_id = pipeline_run_id
        self.current_iteration = 0
        self.agent_outputs = []
        self.created_at = datetime.now().isoformat()
        self.claude_session_id: Optional[str] = None  # Track Claude Code session for continuity


class HumanFeedbackLoopExecutor:
    """Executor for human feedback loops in discussion threads"""

    def __init__(self):
        self.review_parser = ReviewParser()
        # Don't initialize GitHubIntegration here - create it per-loop with proper repo context
        self.active_loops = {}  # Track active loops by issue number

    async def start_loop(
        self,
        issue_number: int,
        repository: str,
        project_name: str,
        board_name: str,
        column: WorkflowColumn,
        issue_data: Dict[str, Any],
        previous_stage_output: Optional[str],
        org: str,
        workflow_columns: list = None,
        workspace_type: str = 'discussions',
        discussion_id: Optional[str] = None,
        pipeline_run_id: Optional[str] = None
    ) -> Tuple[str, bool]:
        """
        Start a human feedback loop where agent responds to human comments

        Returns:
            Tuple of (next_column_name, success)
        """
        logger.info(
            f"Starting human feedback loop for issue #{issue_number} "
            f"with agent {column.agent}"
        )

        # Emit decision event for loop start
        from monitoring.decision_events import DecisionEventEmitter
        from monitoring.observability import get_observability_manager
        
        obs = get_observability_manager()
        decision_events = DecisionEventEmitter(obs)
        
        decision_events.emit_conversational_loop_started(
            issue_number=issue_number,
            project=project_name,
            board=board_name,
            agent=column.agent,
            workspace_type=workspace_type,
            discussion_id=discussion_id
        )

        # Create state
        state = HumanFeedbackState(
            issue_number=issue_number,
            repository=repository,
            agent=column.agent,
            project_name=project_name,
            board_name=board_name,
            workspace_type=workspace_type,
            discussion_id=discussion_id,
            pipeline_run_id=pipeline_run_id
        )

        # Register active loop
        self.active_loops[issue_number] = state
        self.workflow_columns = workflow_columns

        try:
            # Step 1: Execute the agent (initial output)
            await self._execute_agent(
                state,
                column,
                issue_data,
                previous_stage_output,
                org,
                is_initial=True
            )

            # Step 2: Monitor for human feedback
            result = await self._conversational_loop(state, column, issue_data, org)

            return result

        except Exception as e:
            logger.error(f"Conversational loop failed for issue #{issue_number}: {e}")
            raise
        finally:
            # Clean up
            if issue_number in self.active_loops:
                del self.active_loops[issue_number]

    async def _conversational_loop(
        self,
        state: HumanFeedbackState,
        column: WorkflowColumn,
        issue_data: Dict[str, Any],
        org: str
    ) -> Tuple[str, bool]:
        """
        Monitor for human feedback and respond (runs indefinitely until card moves)

        Returns when:
        - Card is moved to a different column (detected via project state)
        - Process terminates (daemon thread)
        """
        poll_interval = 30  # Check every 30 seconds
        poll_count = 0

        logger.info(
            f"Monitoring discussion {state.discussion_id} for human feedback "
            f"(will poll indefinitely until card moves to different column)"
        )
        
        # Safety check: Verify we have valid state
        if not state.agent_outputs:
            logger.warning(
                f"⚠️  SAFETY WARNING: No previous agent outputs loaded for issue #{state.issue_number}. "
                f"This means we cannot determine what feedback is 'new' vs 'old'. "
                f"The monitoring loop will treat ALL historical human comments as new feedback. "
                f"This likely indicates a bug in state loading or bot username filtering."
            )
        else:
            logger.info(
                f"✅ State validated: {len(state.agent_outputs)} previous outputs loaded. "
                f"Will only respond to feedback after {state.agent_outputs[-1]['timestamp']}"
            )

        while True:
            await asyncio.sleep(poll_interval)
            poll_count += 1

            # Check for human feedback
            try:
                human_feedback = await self._get_human_feedback_since_last_agent(
                    state,
                    org
                )
            except Exception as e:
                logger.error(f"Error checking for feedback: {e}")
                import traceback
                logger.error(traceback.format_exc())
                human_feedback = None

            if human_feedback:
                logger.info(f"Human feedback detected from {human_feedback['author']}")
                state.current_iteration += 1

                # Emit decision event for feedback detection
                from monitoring.decision_events import DecisionEventEmitter
                from monitoring.observability import get_observability_manager
                
                obs = get_observability_manager()
                decision_events = DecisionEventEmitter(obs)
                
                # Determine the mode that will be used based on context that will be built
                # Question mode: threaded conversation with parent comment context
                # Revision mode: feedback without thread history (top-level or no parent)
                has_parent_comment = 'parent_comment' in human_feedback and human_feedback['parent_comment'] is not None
                predicted_mode = 'question' if has_parent_comment else 'revision'
                
                # Build action description with mode info
                action_description = f"route_to_agent_{state.agent}_in_{predicted_mode}_mode"
                
                decision_events.emit_feedback_detected(
                    issue_number=state.issue_number,
                    project=state.project_name,
                    board=state.board_name,
                    feedback_source='discussion_reply' if state.workspace_type == 'discussions' else 'issue_comment',
                    feedback_content=human_feedback.get('body', ''),
                    target_agent=state.agent,
                    action_taken=action_description,
                    workspace_type=state.workspace_type,
                    discussion_id=state.discussion_id
                )

                # Execute agent with human feedback (pass full dict with author)
                await self._execute_agent(
                    state,
                    column,
                    issue_data,
                    None,  # No previous stage output
                    org,
                    is_initial=False,
                    human_feedback=human_feedback
                )

                logger.debug("Agent responded to feedback, continuing to monitor")

            # Log progress every 5 minutes
            if poll_count % 10 == 0:  # Every 10 polls = 5 minutes
                logger.debug(
                    f"Still monitoring for feedback "
                    f"(polls: {poll_count}, iterations: {state.current_iteration})"
                )

            # Check if card has moved to a different column
            # This would indicate human intervention or workflow progression
            try:
                from services.project_monitor import ProjectMonitor
                monitor = ProjectMonitor()
                
                # Get current column for this issue
                current_column = await monitor.get_issue_column_async(
                    state.project_name,
                    state.board_name,
                    state.issue_number
                )
                
                # If issue has moved to a different column, or to Backlog, stop monitoring
                if current_column and current_column != column.name:
                    logger.info(
                        f"Issue #{state.issue_number} moved from '{column.name}' to '{current_column}'. "
                        f"Stopping feedback monitoring."
                    )
                    return (None, True)  # Exit the loop
                
                # Special case: If issue is in Backlog (no agent), stop monitoring
                if current_column and current_column.lower() == 'backlog':
                    logger.info(
                        f"Issue #{state.issue_number} is in Backlog column. "
                        f"Stopping feedback monitoring."
                    )
                    return (None, True)  # Exit the loop
                    
            except Exception as e:
                logger.debug(f"Could not check current column (will continue monitoring): {e}")

    async def _execute_agent(
        self,
        state: HumanFeedbackState,
        column: WorkflowColumn,
        issue_data: Dict[str, Any],
        previous_stage_output: Optional[str],
        org: str,
        is_initial: bool = False,
        human_feedback: Optional[Dict[str, Any]] = None
    ):
        """Execute the agent and post output to discussion"""
        from pipeline.factory import PipelineFactory

        # Build context for agent
        context = {
            'project': state.project_name,
            'board': state.board_name,
            'repository': state.repository,
            'issue_number': state.issue_number,
            'issue': issue_data,
            'column': column.name,
            'trigger': 'feedback_loop' if human_feedback else 'conversational_loop',
            'workspace_type': state.workspace_type,
            'discussion_id': state.discussion_id,
            'pipeline_run_id': state.pipeline_run_id,  # Include pipeline run ID for event tracking
            'timestamp': datetime.now().isoformat(),
            'use_docker': True,  # Run agents in Docker for filesystem isolation
            'agent': state.agent  # Pass agent name for observability
        }

        if previous_stage_output:
            context['previous_stage_output'] = previous_stage_output

        if human_feedback:
            # Extract feedback body and author
            feedback_body = human_feedback.get('body', '') if isinstance(human_feedback, dict) else str(human_feedback)
            feedback_author = human_feedback.get('author', 'human') if isinstance(human_feedback, dict) else 'human'
            comment_id = human_feedback.get('comment_id') if isinstance(human_feedback, dict) else None
            parent_comment = human_feedback.get('parent_comment') if isinstance(human_feedback, dict) else None

            # Add feedback context
            context['feedback'] = {
                'formatted_text': feedback_body
            }
            # Enable conversational mode (short threaded responses)
            context['conversation_mode'] = 'threaded'

            # Set reply_to_comment_id for threaded responses
            # GitHub only allows one level of threading, so we must reply to the top-level parent
            if parent_comment:
                # Use the parent comment ID (top-level) for threading
                parent_id = parent_comment.get('id')
                context['reply_to_comment_id'] = parent_id
                logger.debug(f"Set reply_to_comment_id to parent (top-level): {parent_id}")
            elif comment_id:
                # Top-level human comment, can reply directly
                context['reply_to_comment_id'] = comment_id
                logger.debug(f"Set reply_to_comment_id: {comment_id}")

            # Build thread history ONLY from the parent comment being replied to
            # This is deterministic - we know exactly which comment the human replied to
            thread_history = []

            if parent_comment:
                # Include ONLY the parent comment (the specific agent output being discussed)
                thread_history.append({
                    'role': 'agent',
                    'author': parent_comment.get('author', 'orchestrator-bot'),
                    'body': parent_comment.get('body', '')
                })
                logger.info(f"Thread context: parent comment from {parent_comment.get('author')} ({len(parent_comment.get('body', ''))} chars)")
            else:
                # Top-level comment - include the last agent output as context
                if state.agent_outputs:
                    thread_history.append({
                        'role': 'agent',
                        'author': 'orchestrator-bot',
                        'body': state.agent_outputs[-1]['output']
                    })
                    logger.info(f"Thread context: last agent output ({len(state.agent_outputs[-1]['output'])} chars)")

            # Add the human's reply
            thread_history.append({
                'role': 'user',
                'author': feedback_author,
                'body': feedback_body
            })

            context['thread_history'] = thread_history

            # Log context size for debugging
            total_context_chars = sum(len(msg['body']) for msg in thread_history)
            logger.info(f"Total thread context: {total_context_chars} chars ({len(thread_history)} messages)")

            context['conversational_loop'] = {
                'iteration': state.current_iteration,
                'has_human_feedback': True
            }
            # Add previous output so agent can reference it (for backward compatibility)
            if state.agent_outputs:
                context['previous_output'] = state.agent_outputs[-1]['output']

        # Pass existing session_id for session continuity
        if state.claude_session_id:
            context['claude_session_id'] = state.claude_session_id
            logger.info(f"Resuming Claude Code session: {state.claude_session_id}")

        # Determine what execution mode the agent will use based on context
        # This matches the logic in base_maker_agent._determine_execution_mode()
        if human_feedback and context.get('conversation_mode') == 'threaded' and len(context.get('thread_history', [])) > 0:
            execution_mode = 'question'
        elif human_feedback or context.get('trigger') == 'feedback_loop':
            execution_mode = 'revision'
        else:
            execution_mode = 'initial'
        
        logger.info(f"Agent will execute in {execution_mode.upper()} mode (iteration {state.current_iteration}, is_initial={is_initial})")

        # Execute agent via centralized executor (ensures observability)
        logger.info(f"Executing {state.agent} (iteration {state.current_iteration})")

        from services.agent_executor import get_agent_executor

        executor = get_agent_executor()
        result = await executor.execute_agent(
            agent_name=state.agent,
            project_name=state.project_name,
            task_context=context,
            task_id_prefix="conversational"
        )

        # Extract and store session_id for continuity
        if 'claude_session_id' in result:
            state.claude_session_id = result['claude_session_id']
            logger.info(f"Stored Claude Code session_id: {state.claude_session_id}")

            # Persist session_id to disk for restart continuity
            conversational_session_state.save_session(
                project_name=state.project_name,
                issue_number=state.issue_number,
                session_id=state.claude_session_id,
                agent=state.agent,
                workspace_type=state.workspace_type
            )
        else:
            # Update last_interaction timestamp even if no new session_id
            conversational_session_state.update_last_interaction(
                project_name=state.project_name,
                issue_number=state.issue_number
            )

        # Store output
        state.agent_outputs.append({
            'iteration': state.current_iteration,
            'output': result,
            'is_initial': is_initial,
            'has_feedback': human_feedback is not None,
            'timestamp': datetime.now().isoformat()
        })

        logger.info(f"Agent {state.agent} completed successfully")

    async def _get_human_feedback_from_issue(
        self,
        state: HumanFeedbackState,
        org: str
    ) -> Optional[Dict[str, Any]]:
        """
        Check for human feedback on an issue (not discussion)

        Returns:
            Dict with author and body, or None if no feedback
        """
        from services.github_app import github_app
        from dateutil import parser as date_parser

        try:
            logger.debug(f"Fetching comments from issue #{state.issue_number}")

            # Use GraphQL to get issue comments
            query = """
            query($org: String!, $repo: String!, $number: Int!) {
              repository(owner: $org, name: $repo) {
                issue(number: $number) {
                  comments(last: 50) {
                    nodes {
                      id
                      author { login }
                      body
                      createdAt
                    }
                  }
                }
              }
            }
            """

            # Check if GitHub App is configured before attempting GraphQL
            if not github_app.enabled:
                logger.debug(f"Cannot check issue #{state.issue_number} for feedback - GitHub App not configured")
                return None

            result = github_app.graphql_request(query, {
                'org': org,
                'repo': state.repository,
                'number': state.issue_number
            })

            if not result or 'repository' not in result or not result['repository']:
                logger.warning(f"Issue #{state.issue_number} could not be accessed via GraphQL (may not exist or insufficient permissions)")
                return None

            comments = result['repository']['issue']['comments']['nodes']

            # Get timestamp of last agent output
            if state.agent_outputs:
                last_agent_time = datetime.fromisoformat(
                    state.agent_outputs[-1]['timestamp']
                )
                if last_agent_time.tzinfo:
                    last_agent_time = last_agent_time.replace(tzinfo=None)
            else:
                # No agent output yet, use loop start time
                last_agent_time = datetime.fromisoformat(state.created_at)
                if last_agent_time.tzinfo:
                    last_agent_time = last_agent_time.replace(tzinfo=None)

            logger.debug(f"Checking {len(comments)} comments for feedback (last_agent_time: {last_agent_time})")

            # Find human comments after last agent output
            for comment in comments:
                if 'author' in comment and 'createdAt' in comment:
                    author = comment['author']['login']
                    created_at = date_parser.parse(comment['createdAt'])
                    if created_at.tzinfo:
                        created_at = created_at.replace(tzinfo=None)

                    if not is_bot_user(author) and created_at > last_agent_time:
                        logger.info(f"Found human feedback in issue comment from {author}")
                        return {
                            'author': author,
                            'body': comment.get('body', ''),
                            'created_at': created_at.isoformat(),
                            'comment_id': comment.get('id')
                        }

            return None

        except Exception as e:
            logger.error(f"Error checking for feedback on issue: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    async def _get_human_feedback_since_last_agent(
        self,
        state: HumanFeedbackState,
        org: str
    ) -> Optional[Dict[str, Any]]:
        """
        Check for human feedback since the last agent output

        Routes to appropriate method based on workspace type.

        Returns:
            Dict with author and body, or None if no feedback
        """
        # For issues workspace, use issue comment API
        if state.workspace_type == 'issues' or state.discussion_id is None:
            return await self._get_human_feedback_from_issue(state, org)

        # For discussions workspace, use discussion API
        from services.github_app import github_app
        from dateutil import parser as date_parser

        try:
            logger.debug(f"Fetching comments from discussion {state.discussion_id}")

            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
                  comments(last: 20) {
                    nodes {
                      id
                      author { login }
                      body
                      createdAt
                      replies(last: 20) {
                        nodes {
                          id
                          author { login }
                          body
                          createdAt
                        }
                      }
                    }
                  }
                }
              }
            }
            """

            result = github_app.graphql_request(query, {'discussionId': state.discussion_id})

            logger.debug(f"GraphQL result: {result is not None}")

            if not result:
                logger.warning(f"No result from GraphQL query for discussion {state.discussion_id}")
                return None

            # GitHub App graphql_request returns direct data, not wrapped in 'data' key
            if 'node' not in result:
                logger.error(f"GraphQL query missing 'node' key: {result}")
                return None

            if not result['node']:
                logger.warning(f"Discussion {state.discussion_id} not found")
                return None

            comments = result['node']['comments']['nodes']

            # Get timestamp of last agent output (make naive for comparison)
            if state.agent_outputs:
                last_agent_time = datetime.fromisoformat(
                    state.agent_outputs[-1]['timestamp']
                )
                # Strip timezone to match comment timestamps (which are also made naive)
                if last_agent_time.tzinfo:
                    last_agent_time = last_agent_time.replace(tzinfo=None)
            else:
                # No agent output yet, use loop start time
                last_agent_time = datetime.fromisoformat(state.created_at)
                if last_agent_time.tzinfo:
                    last_agent_time = last_agent_time.replace(tzinfo=None)

            # Flatten comments and replies, look for human comments after last agent output
            logger.debug(f"Checking {len(comments)} comments for feedback (last_agent_time: {last_agent_time})")

            for comment in comments:
                # Check top-level comment
                if 'author' in comment and 'createdAt' in comment:
                    author = comment['author']['login']
                    created_at = date_parser.parse(comment['createdAt'])
                    if created_at.tzinfo:
                        created_at = created_at.replace(tzinfo=None)

                    if not is_bot_user(author) and created_at > last_agent_time:
                        logger.info(f"Found human feedback in top-level comment from {author}")
                        return {
                            'author': author,
                            'body': comment.get('body', ''),
                            'created_at': created_at.isoformat(),
                            'comment_id': comment.get('id'),
                            'parent_comment': None  # Top-level, no parent
                        }

                # Check replies - the parent comment is already known!
                replies = comment.get('replies', {}).get('nodes', [])
                logger.debug(f"Comment has {len(replies)} replies")

                for reply in replies:
                    author = reply['author']['login']
                    created_at = date_parser.parse(reply['createdAt'])

                    # Convert to naive datetime
                    if created_at.tzinfo:
                        created_at = created_at.replace(tzinfo=None)

                    logger.debug(f"Reply from {author} at {created_at} (last_agent: {last_agent_time})")

                    # Check if human comment after last agent output
                    if not is_bot_user(author) and created_at > last_agent_time:
                        logger.info(f"Found human feedback in reply from {author} to comment {comment.get('id')}")
                        # Return the parent comment info so we can build correct thread context
                        return {
                            'author': author,
                            'body': reply['body'],
                            'created_at': created_at.isoformat(),
                            'comment_id': reply.get('id'),
                            'parent_comment': {
                                'id': comment.get('id'),
                                'body': comment.get('body', ''),
                                'author': comment.get('author', {}).get('login', 'unknown')
                            }
                        }

            return None

        except Exception as e:
            logger.error(f"Error checking for human feedback: {e}")
            return None

    async def _load_previous_outputs_from_issue(self, state: HumanFeedbackState, org: str):
        """Load previous agent outputs from issue comments to rebuild conversation history"""
        from services.github_app import github_app
        from dateutil import parser as date_parser

        try:
            # Use GraphQL to get issue comments
            query = """
            query($org: String!, $repo: String!, $number: Int!) {
              repository(owner: $org, name: $repo) {
                issue(number: $number) {
                  comments(last: 50) {
                    nodes {
                      author { login }
                      body
                      createdAt
                    }
                  }
                }
              }
            }
            """

            # Check if GitHub App is configured before attempting GraphQL
            if not github_app.enabled:
                logger.warning(f"Cannot load previous outputs for issue #{state.issue_number} - GitHub App not configured")
                return

            result = github_app.graphql_request(query, {
                'org': org,
                'repo': state.repository,
                'number': state.issue_number
            })

            if not result or 'repository' not in result or not result['repository']:
                logger.warning(f"Could not load issue #{state.issue_number}")
                return

            comments = result['repository']['issue']['comments']['nodes']

            # Collect all bot comments in chronological order
            bot_comments = []

            for comment in comments:
                if is_bot_user(comment.get('author', {}).get('login', '')):
                    bot_comments.append({
                        'body': comment.get('body', ''),
                        'timestamp': comment.get('createdAt')
                    })

            # Sort by timestamp and add to state
            bot_comments.sort(key=lambda x: x['timestamp'])

            for i, comment in enumerate(bot_comments):
                state.agent_outputs.append({
                    'iteration': i,
                    'output': comment['body'],
                    'is_initial': i == 0,
                    'has_feedback': i > 0,
                    'timestamp': comment['timestamp']
                })

            state.current_iteration = len(bot_comments)
            logger.info(f"Loaded {len(bot_comments)} previous agent outputs from issue #{state.issue_number}")
            
            if len(bot_comments) > 0:
                logger.info(f"Most recent agent output timestamp: {bot_comments[-1]['timestamp']}")
            else:
                logger.warning(f"No previous agent outputs found for issue #{state.issue_number} - all comments will appear as 'new feedback'")

        except Exception as e:
            logger.error(f"Failed to load previous outputs from issue: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def _load_previous_outputs_from_discussion(self, state: HumanFeedbackState, org: str):
        """Load previous agent outputs from discussion to rebuild conversation history"""
        from services.github_app import github_app
        from dateutil import parser as date_parser

        try:
            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
                  comments(last: 50) {
                    nodes {
                      author { login }
                      body
                      createdAt
                      replies(last: 50) {
                        nodes {
                          author { login }
                          body
                          createdAt
                        }
                      }
                    }
                  }
                }
              }
            }
            """

            result = github_app.graphql_request(query, {'discussionId': state.discussion_id})

            if not result or 'node' not in result or not result['node']:
                logger.warning(f"Could not load discussion {state.discussion_id}")
                return

            comments = result['node']['comments']['nodes']

            # Collect all bot comments (both top-level and replies) in chronological order
            bot_comments = []

            for comment in comments:
                # Check top-level comment
                if is_bot_user(comment.get('author', {}).get('login', '')):
                    bot_comments.append({
                        'body': comment.get('body', ''),
                        'timestamp': comment.get('createdAt')
                    })

                # Check replies
                for reply in comment.get('replies', {}).get('nodes', []):
                    if is_bot_user(reply.get('author', {}).get('login', '')):
                        bot_comments.append({
                            'body': reply.get('body', ''),
                            'timestamp': reply.get('createdAt')
                        })

            # Sort by timestamp and add to state
            bot_comments.sort(key=lambda x: x['timestamp'])

            for i, comment in enumerate(bot_comments):
                state.agent_outputs.append({
                    'iteration': i,
                    'output': comment['body'],
                    'is_initial': i == 0,
                    'has_feedback': i > 0,
                    'timestamp': comment['timestamp']
                })

            state.current_iteration = len(bot_comments)
            logger.info(f"Loaded {len(bot_comments)} previous agent outputs from discussion")
            
            if len(bot_comments) > 0:
                logger.info(f"Most recent agent output timestamp: {bot_comments[-1]['timestamp']}")
            else:
                logger.warning(f"No previous agent outputs found for issue #{state.issue_number} - all comments will appear as 'new feedback'")

        except Exception as e:
            logger.error(f"Failed to load previous outputs from discussion: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _get_next_column_name(self, current_column: WorkflowColumn) -> str:
        """Determine next column name"""
        if not hasattr(self, 'workflow_columns') or not self.workflow_columns:
            logger.warning("No workflow columns available")
            return current_column.name

        # Find current column index
        current_index = -1
        for i, col in enumerate(self.workflow_columns):
            if col.name == current_column.name:
                current_index = i
                break

        if current_index == -1 or current_index >= len(self.workflow_columns) - 1:
            logger.warning(f"Cannot determine next column for {current_column.name}")
            return current_column.name

        # Return next column name
        next_col = self.workflow_columns[current_index + 1]
        logger.info(f"Next column after {current_column.name}: {next_col.name}")
        return next_col.name


# Global singleton
human_feedback_loop_executor = HumanFeedbackLoopExecutor()
