"""
Review Cycle Executor

Implements the synchronous maker-checker review loop with iteration tracking.
"""

import asyncio
import logging
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
from config.manager import WorkflowColumn
from services.review_parser import ReviewParser, ReviewStatus
from services.github_integration import GitHubIntegration

logger = logging.getLogger(__name__)


class ReviewCycleState:
    """Tracks state for an active review cycle"""

    def __init__(
        self,
        issue_number: int,
        repository: str,
        maker_agent: str,
        reviewer_agent: str,
        max_iterations: int,
        project_name: str,
        board_name: str,
        workspace_type: str = 'issues',
        discussion_id: Optional[str] = None
    ):
        self.issue_number = issue_number
        self.repository = repository
        self.maker_agent = maker_agent
        self.reviewer_agent = reviewer_agent
        self.max_iterations = max_iterations
        self.project_name = project_name
        self.board_name = board_name
        self.workspace_type = workspace_type
        self.discussion_id = discussion_id
        self.current_iteration = 0
        self.maker_outputs = []
        self.review_outputs = []
        self.created_at = datetime.now().isoformat()
        self.updated_at = datetime.now().isoformat()

        # Recovery state
        self.status = 'initialized'  # initialized, maker_working, reviewer_working, awaiting_human_feedback, completed
        self.escalation_time = None  # When escalated to human (ISO format)
        self.last_maker_comment_id = None  # Last comment ID from maker
        self.last_review_comment_id = None  # Last comment ID from reviewer
        self.last_escalation_comment_id = None  # Escalation comment ID for feedback tracking

    def to_dict(self) -> Dict[str, Any]:
        return {
            'issue_number': self.issue_number,
            'repository': self.repository,
            'maker_agent': self.maker_agent,
            'reviewer_agent': self.reviewer_agent,
            'max_iterations': self.max_iterations,
            'project_name': self.project_name,
            'board_name': self.board_name,
            'workspace_type': self.workspace_type,
            'discussion_id': self.discussion_id,
            'current_iteration': self.current_iteration,
            'maker_outputs': self.maker_outputs,
            'review_outputs': self.review_outputs,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'status': self.status,
            'escalation_time': self.escalation_time,
            'last_maker_comment_id': self.last_maker_comment_id,
            'last_review_comment_id': self.last_review_comment_id,
            'last_escalation_comment_id': self.last_escalation_comment_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ReviewCycleState':
        """Reconstruct state from persisted dict"""
        state = cls(
            issue_number=data['issue_number'],
            repository=data['repository'],
            maker_agent=data['maker_agent'],
            reviewer_agent=data['reviewer_agent'],
            max_iterations=data['max_iterations'],
            project_name=data['project_name'],
            board_name=data['board_name'],
            workspace_type=data.get('workspace_type', 'issues'),
            discussion_id=data.get('discussion_id')
        )
        state.current_iteration = data.get('current_iteration', 0)
        state.maker_outputs = data.get('maker_outputs', [])
        state.review_outputs = data.get('review_outputs', [])
        state.created_at = data.get('created_at', datetime.now().isoformat())
        state.updated_at = data.get('updated_at', datetime.now().isoformat())
        state.status = data.get('status', 'initialized')
        state.escalation_time = data.get('escalation_time')
        state.last_maker_comment_id = data.get('last_maker_comment_id')
        state.last_review_comment_id = data.get('last_review_comment_id')
        state.last_escalation_comment_id = data.get('last_escalation_comment_id')
        return state


class ReviewCycleExecutor:
    """Executes synchronous maker-checker review cycles"""

    def __init__(self):
        self.review_parser = ReviewParser()
        self.github = GitHubIntegration()
        self.active_cycles = {}  # Track active review cycles by issue number
        self.state_dir = None  # Will be set per project

    def _get_state_file_path(self, project_name: str) -> str:
        """Get path to active cycles state file for a project"""
        import os
        state_dir = os.path.join('state', 'projects', project_name, 'review_cycles')
        os.makedirs(state_dir, exist_ok=True)
        return os.path.join(state_dir, 'active_cycles.yaml')

    def _save_cycle_state(self, cycle_state: ReviewCycleState):
        """Persist cycle state to disk"""
        import yaml
        import os
        from datetime import datetime

        # Update timestamp
        cycle_state.updated_at = datetime.now().isoformat()

        state_file = self._get_state_file_path(cycle_state.project_name)

        # Load existing state
        if os.path.exists(state_file):
            with open(state_file, 'r') as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}

        # Initialize active_cycles if not present
        if 'active_cycles' not in data:
            data['active_cycles'] = []

        # Update or add this cycle
        cycle_dict = cycle_state.to_dict()
        updated = False
        for i, existing in enumerate(data['active_cycles']):
            if existing['issue_number'] == cycle_state.issue_number:
                data['active_cycles'][i] = cycle_dict
                updated = True
                break

        if not updated:
            data['active_cycles'].append(cycle_dict)

        # Write back
        with open(state_file, 'w') as f:
            yaml.dump(data, f, default_flow_style=False)

        logger.info(f"Saved cycle state for issue {cycle_state.issue_number} (status: {cycle_state.status})")

    def _load_active_cycles(self, project_name: str) -> Dict[int, ReviewCycleState]:
        """Load active cycles from disk for a project"""
        import yaml
        import os

        state_file = self._get_state_file_path(project_name)

        if not os.path.exists(state_file):
            logger.info(f"No active cycles state file found for {project_name}")
            return {}

        with open(state_file, 'r') as f:
            data = yaml.safe_load(f) or {}

        cycles = {}
        for cycle_data in data.get('active_cycles', []):
            cycle_state = ReviewCycleState.from_dict(cycle_data)
            cycles[cycle_state.issue_number] = cycle_state

        logger.info(f"Loaded {len(cycles)} active cycles for {project_name}")
        return cycles

    def _remove_cycle_state(self, cycle_state: ReviewCycleState):
        """Remove a completed cycle from active state"""
        import yaml
        import os

        state_file = self._get_state_file_path(cycle_state.project_name)

        if not os.path.exists(state_file):
            return

        with open(state_file, 'r') as f:
            data = yaml.safe_load(f) or {}

        # Remove this cycle
        data['active_cycles'] = [
            c for c in data.get('active_cycles', [])
            if c['issue_number'] != cycle_state.issue_number
        ]

        # Write back
        with open(state_file, 'w') as f:
            yaml.dump(data, f, default_flow_style=False)

        logger.info(f"Removed completed cycle state for issue {cycle_state.issue_number}")

    async def resume_active_cycles(self, project_name: str, org: str):
        """
        Resume all active review cycles for a project after restart

        This is called on orchestrator startup to recover in-progress review cycles.
        """
        from config.manager import config_manager

        logger.info(f"Checking for active review cycles to resume for {project_name}")

        # Load active cycles from disk and populate in-memory cache
        active_cycles = self._load_active_cycles(project_name)

        if not active_cycles:
            logger.info(f"No active cycles found for {project_name}")
            return

        logger.info(f"Found {len(active_cycles)} active cycles to resume")

        # Populate in-memory cache
        for issue_number, cycle_state in active_cycles.items():
            self.active_cycles[issue_number] = cycle_state

        # Resume each cycle based on its state
        for issue_number, cycle_state in active_cycles.items():
            logger.info(
                f"Resuming cycle for issue #{issue_number} "
                f"(status: {cycle_state.status}, iteration: {cycle_state.current_iteration}/{cycle_state.max_iterations})"
            )

            try:
                # If this cycle uses discussions, verify the discussion still exists
                if cycle_state.workspace_type == 'discussions' and cycle_state.discussion_id:
                    from services.github_discussions import GitHubDiscussions
                    discussions = GitHubDiscussions()
                    discussion = discussions.get_discussion(cycle_state.discussion_id)

                    if not discussion:
                        logger.warning(
                            f"Discussion {cycle_state.discussion_id} for issue #{issue_number} "
                            f"no longer exists. Removing cycle from active state."
                        )
                        self._remove_cycle_state(cycle_state)
                        continue

            except Exception as e:
                logger.error(f"Failed to resume cycle for issue #{issue_number}: {e}")
                continue

            try:
                if cycle_state.status == 'awaiting_human_feedback':
                    # Cycle is awaiting human feedback
                    logger.info(
                        f"Cycle for issue #{issue_number} is awaiting human feedback. "
                        f"Project monitor will check periodically and resume when feedback is detected."
                    )
                    # State is already saved, project_monitor.check_escalated_review_cycles() will handle this
                    continue

                elif cycle_state.status in ['maker_working', 'reviewer_working']:
                    # Agents were working when restart happened
                    # We'll wait for manual intervention or restart the work
                    logger.warning(
                        f"Cycle was in {cycle_state.status} state during restart. "
                        f"Manual intervention may be required to resume."
                    )
                    # Could implement: check GitHub for new comments from agents and continue

                elif cycle_state.status == 'completed':
                    # Shouldn't happen, but clean up if it does
                    logger.info(f"Cycle marked as completed, removing from active state")
                    self._remove_cycle_state(cycle_state)

                else:
                    logger.warning(f"Unknown cycle status: {cycle_state.status}")

            except Exception as e:
                logger.error(f"Failed to resume cycle for issue #{issue_number}: {e}", exc_info=True)

        logger.info("Finished resuming active cycles")

    async def start_review_cycle(
        self,
        issue_number: int,
        repository: str,
        project_name: str,
        board_name: str,
        column: WorkflowColumn,
        issue_data: Dict[str, Any],
        previous_stage_output: str,
        org: str,
        workflow_columns: list = None,
        workspace_type: str = 'issues',
        discussion_id: Optional[str] = None
    ) -> Tuple[str, bool]:
        """
        Start a review cycle for an issue

        Args:
            issue_number: GitHub issue number
            repository: Repository name
            project_name: Project name
            board_name: Board name
            column: Review column configuration
            issue_data: Full issue data
            previous_stage_output: Output from previous maker agent
            org: GitHub organization
            workspace_type: Workspace type ('issues' or 'discussions')
            discussion_id: Discussion ID if using discussions workspace

        Returns:
            Tuple of (next_column_name, cycle_complete)
        """
        # Check if there's already an active cycle for this issue
        if issue_number in self.active_cycles:
            logger.warning(
                f"Review cycle already active for issue #{issue_number}, "
                f"using existing cycle (status: {self.active_cycles[issue_number].status})"
            )
            cycle_state = self.active_cycles[issue_number]
        else:
            # Create new review cycle state
            cycle_state = ReviewCycleState(
                issue_number=issue_number,
                repository=repository,
                maker_agent=column.maker_agent,
                reviewer_agent=column.agent,
                max_iterations=column.max_iterations,
                project_name=project_name,
                board_name=board_name,
                workspace_type=workspace_type,
                discussion_id=discussion_id
            )

        # Store initial maker output
        cycle_state.maker_outputs.append({
            'iteration': 0,
            'output': previous_stage_output,
            'timestamp': datetime.now().isoformat()
        })

        # Track active cycle
        self.active_cycles[issue_number] = cycle_state
        self.workflow_columns = workflow_columns  # Store for next column lookup

        # Save initial state to disk
        cycle_state.status = 'maker_working'
        self._save_cycle_state(cycle_state)

        try:
            # Execute the review loop
            final_status, next_column = await self._execute_review_loop(
                cycle_state,
                column,
                issue_data,
                org
            )

            # Clean up
            del self.active_cycles[issue_number]

            return next_column, True

        except Exception as e:
            logger.error(f"Review cycle failed for issue #{issue_number}: {e}")
            # Clean up on error
            if issue_number in self.active_cycles:
                del self.active_cycles[issue_number]

            # Post error comment (workspace-aware)
            error_message = f"⚠️ **Review Cycle Error**\n\nThe automated review cycle encountered an error:\n```\n{str(e)}\n```\n\nPlease review manually."

            if workspace_type == 'discussions' and discussion_id:
                from services.github_discussions import GitHubDiscussions
                discussions = GitHubDiscussions()
                discussions.add_discussion_comment(discussion_id, error_message)
            else:
                await self.github.post_issue_comment(
                    issue_number,
                    error_message,
                    repository
                )

            raise

    async def resume_review_cycle_with_feedback(
        self,
        cycle_state: ReviewCycleState,
        human_feedback: str,
        org: str
    ):
        """
        Resume a review cycle that was escalated and now has human feedback.
        This is called by project_monitor when it detects feedback on an escalated cycle.

        Args:
            cycle_state: The cycle state from persistent storage
            human_feedback: The human feedback text detected
            org: GitHub organization
        """
        from config.manager import config_manager

        logger.info(f"Resuming escalated review cycle for issue #{cycle_state.issue_number} with human feedback")

        try:
            # Get column configuration
            workflow = config_manager.get_project_workflow(cycle_state.project_name, cycle_state.board_name)
            column = next(
                (col for col in workflow.columns if col.agent == cycle_state.reviewer_agent),
                None
            )

            if not column:
                logger.error(f"Could not find column for reviewer {cycle_state.reviewer_agent}")
                return

            # Get issue data
            issue_data = self.github.get_issue_details(cycle_state.repository, cycle_state.issue_number, org)

            # Update state: human feedback received, reviewer working
            cycle_state.status = 'reviewer_working'
            self._save_cycle_state(cycle_state)

            # Get current iteration from stored outputs
            iteration = cycle_state.current_iteration

            # Re-invoke reviewer with human feedback incorporated
            fresh_context_with_feedback = await self._get_fresh_discussion_context(
                cycle_state, org, iteration
            )

            reviewer_task_context = self._create_review_task_context(
                cycle_state,
                column,
                issue_data,
                iteration,
                full_discussion_context=fresh_context_with_feedback
            )

            # Add flag to indicate this is a re-review after human feedback
            reviewer_task_context['review_cycle']['post_human_feedback'] = True
            reviewer_task_context['review_cycle']['human_feedback'] = human_feedback

            await self._execute_agent_directly(
                cycle_state.reviewer_agent,
                reviewer_task_context,
                cycle_state.project_name
            )

            # Get updated review
            updated_review = await self._get_latest_agent_comment(
                cycle_state.issue_number,
                cycle_state.repository,
                cycle_state.reviewer_agent,
                cycle_state.workspace_type,
                cycle_state.discussion_id
            )

            # Store updated review
            cycle_state.review_outputs.append({
                'iteration': iteration,
                'output': updated_review,
                'post_human_feedback': True,
                'timestamp': datetime.now().isoformat()
            })

            # Parse updated review and continue based on new status
            review_result_parsed = self.review_parser.parse_review(updated_review)

            logger.info(
                f"Updated review status after human feedback: {review_result_parsed.status.value}, "
                f"blocking: {review_result_parsed.blocking_count}"
            )

            # If still blocked after human feedback, something is wrong
            if review_result_parsed.status == ReviewStatus.BLOCKED:
                logger.error("Review still blocked after human feedback and reviewer update")
                cycle_state.status = 'completed'  # Mark as done, needs manual intervention
                self._save_cycle_state(cycle_state)
                return

            # If approved, we're done!
            if review_result_parsed.status == ReviewStatus.APPROVED:
                await self._post_cycle_summary(
                    cycle_state,
                    "APPROVED",
                    f"Review approved after human feedback in iteration {iteration}"
                )
                cycle_state.status = 'completed'
                self._save_cycle_state(cycle_state)

                if column.auto_advance_on_approval:
                    next_column = self._get_next_column_name(column)
                    logger.info(f"Review cycle completed, advancing to {next_column}")

                logger.info(f"Review cycle completed successfully for issue #{cycle_state.issue_number}")
                return

            # If changes requested, continue the review cycle with maker
            if review_result_parsed.status == ReviewStatus.CHANGES_REQUESTED:
                logger.info("Changes requested after human feedback - continuing cycle with maker")

                # Get fresh context for maker
                fresh_context = await self._get_fresh_discussion_context(cycle_state, org, iteration)

                # Create maker revision context
                maker_task_context = self._create_maker_revision_task_context(
                    cycle_state,
                    column,
                    issue_data,
                    iteration,
                    updated_review,
                    full_discussion_context=fresh_context
                )

                # Invoke maker with updated review feedback
                cycle_state.status = 'maker_working'
                self._save_cycle_state(cycle_state)

                await self._execute_agent_directly(
                    cycle_state.maker_agent,
                    maker_task_context,
                    cycle_state.project_name
                )

                # Get maker output
                maker_output = await self._get_latest_agent_comment(
                    cycle_state.issue_number,
                    cycle_state.repository,
                    cycle_state.maker_agent,
                    cycle_state.workspace_type,
                    cycle_state.discussion_id
                )

                # Store maker output
                cycle_state.maker_outputs.append({
                    'iteration': iteration,
                    'output': maker_output,
                    'timestamp': datetime.now().isoformat()
                })

                # Increment iteration
                cycle_state.current_iteration += 1
                cycle_state.status = 'initialized'  # Ready for next review iteration
                self._save_cycle_state(cycle_state)

                logger.info(f"Maker completed revision, cycle ready for next iteration (now {cycle_state.current_iteration})")

                # The cycle is now ready for the next review iteration
                # Project monitor will pick it up in the next polling cycle

        except Exception as e:
            logger.error(f"Failed to resume review cycle with feedback: {e}")
            import traceback
            logger.error(traceback.format_exc())
            raise

    async def resume_review_cycle(
        self,
        issue_number: int,
        repository: str,
        project_name: str,
        board_name: str,
        org: str,
        discussion_id: str,
        column: WorkflowColumn,
        issue_data: Dict[str, Any],
        workflow_columns: list = None
    ) -> Tuple[str, bool]:
        """
        Resume a paused review cycle by analyzing discussion state

        Detects the current state from the discussion and resumes from there.

        Returns:
            Tuple of (next_column_name, cycle_complete)
        """
        from services.github_app import github_app
        from dateutil import parser as date_parser

        logger.info(f"Resuming review cycle for issue #{issue_number} from discussion {discussion_id}")

        try:
            # Step 1: Analyze discussion to determine state
            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
                  comments(last: 50) {
                    nodes {
                      id
                      author { login }
                      body
                      createdAt
                      replies(last: 20) {
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

            result = github_app.graphql_request(query, {'discussionId': discussion_id})

            if not result or 'data' not in result or not result['data']['node']:
                raise Exception(f"Could not fetch discussion {discussion_id}")

            all_comments = result['data']['node']['comments']['nodes']

            # Flatten comments and replies into chronological order
            timeline = []
            for comment in all_comments:
                timeline.append({
                    'author': comment['author']['login'],
                    'body': comment['body'],
                    'created_at': date_parser.parse(comment['createdAt']),
                    'is_reply': False
                })
                for reply in comment.get('replies', {}).get('nodes', []):
                    timeline.append({
                        'author': reply['author']['login'],
                        'body': reply['body'],
                        'created_at': date_parser.parse(reply['createdAt']),
                        'is_reply': True
                    })

            timeline.sort(key=lambda x: x['created_at'])

            # Step 2: Detect review cycle state
            last_escalation = None
            last_agent_comment = None
            human_feedback_after_escalation = []
            maker_agent = column.maker_agent if hasattr(column, 'maker_agent') else 'business_analyst'
            reviewer_agent = column.agent
            iteration = 0

            for item in timeline:
                author = item['author']
                body = item['body']
                created_at = item['created_at']

                # Track escalations
                if author == 'orchestrator-bot' and ('Review Blocked' in body or 'Max Review Iterations' in body):
                    last_escalation = {
                        'body': body,
                        'created_at': created_at
                    }
                    human_feedback_after_escalation = []  # Reset

                # Track iteration count
                if author == 'orchestrator-bot' and 'Iteration:' in body:
                    import re
                    match = re.search(r'Iteration[:\s]+(\d+)/(\d+)', body)
                    if match:
                        iteration = int(match.group(1))

                # Track agent comments
                if author == 'orchestrator-bot':
                    last_agent_comment = {
                        'agent': reviewer_agent if 'Review of' in body or 'APPROVED' in body or 'BLOCKED' in body else maker_agent,
                        'body': body,
                        'created_at': created_at
                    }

                # Track human feedback after escalation
                if last_escalation and author != 'orchestrator-bot' and created_at > last_escalation['created_at']:
                    human_feedback_after_escalation.append({
                        'author': author,
                        'body': body,
                        'created_at': created_at
                    })

            # Step 3: Determine next action
            logger.info(f"State detected - Last escalation: {bool(last_escalation)}, Human feedback: {len(human_feedback_after_escalation)}, Iteration: {iteration}")

            if last_escalation and human_feedback_after_escalation:
                # State: Escalated + Human responded → Re-invoke reviewer
                logger.info("Detected: Escalation with human feedback - re-invoking reviewer")

                # Combine all human feedback
                combined_feedback = "\n\n---\n\n".join([f"**From {f['author']} at {f['created_at']}:**\n{f['body']}" for f in human_feedback_after_escalation])

                # Create cycle state
                cycle_state = ReviewCycleState(
                    issue_number=issue_number,
                    repository=repository,
                    maker_agent=maker_agent,
                    reviewer_agent=reviewer_agent,
                    max_iterations=column.max_iterations if hasattr(column, 'max_iterations') else 3,
                    project_name=project_name,
                    board_name=board_name,
                    workspace_type='discussions',
                    discussion_id=discussion_id
                )
                cycle_state.current_iteration = iteration

                # Get fresh discussion context
                fresh_context = await self._get_fresh_discussion_context(cycle_state, org, iteration)

                # Create reviewer task context with post-human-feedback flag
                reviewer_task_context = self._create_review_task_context(
                    cycle_state,
                    column,
                    issue_data,
                    iteration,
                    full_discussion_context=fresh_context
                )
                reviewer_task_context['review_cycle']['post_human_feedback'] = True
                reviewer_task_context['review_cycle']['human_feedback'] = combined_feedback

                # Execute reviewer
                await self._execute_agent_directly(
                    reviewer_agent,
                    reviewer_task_context,
                    project_name
                )

                # Get updated review
                updated_review = await self._get_latest_agent_comment(
                    issue_number,
                    repository,
                    reviewer_agent,
                    'discussions',
                    discussion_id
                )

                # Parse updated review
                review_result = self.review_parser.parse_review(updated_review)

                logger.info(f"Reviewer update complete - Status: {review_result.status.value}")

                # Continue based on updated review
                if review_result.status == ReviewStatus.APPROVED:
                    await self._post_cycle_summary(
                        cycle_state,
                        "APPROVED",
                        f"Review approved after human feedback in iteration {iteration}"
                    )
                    if column.auto_advance_on_approval:
                        next_column = self._get_next_column_name(column)
                        return next_column, True
                    else:
                        return column.name, True

                elif review_result.status == ReviewStatus.CHANGES_REQUESTED:
                    # Invoke maker with updated feedback, then continue cycle
                    logger.info("Changes requested after human feedback - resuming full cycle")

                    # Store reviewer output
                    cycle_state.review_outputs.append({
                        'iteration': iteration,
                        'output': updated_review,
                        'post_human_feedback': True,
                        'timestamp': datetime.now().isoformat()
                    })

                    # Register in active cycles
                    self.active_cycles[issue_number] = cycle_state
                    self.workflow_columns = workflow_columns

                    # Continue the review loop from current iteration
                    next_column, cycle_complete = await self._execute_review_loop(
                        cycle_state,
                        column,
                        issue_data,
                        org
                    )

                    return next_column, cycle_complete

                else:  # BLOCKED
                    logger.error("Review still blocked after human feedback")
                    return column.name, False

            else:
                logger.warning("Could not determine resume action - no escalation or no human feedback detected")
                return column.name, False

        except Exception as e:
            logger.error(f"Failed to resume review cycle for issue #{issue_number}: {e}")
            raise

    async def _execute_review_loop(
        self,
        cycle_state: ReviewCycleState,
        column: WorkflowColumn,
        issue_data: Dict[str, Any],
        org: str
    ) -> Tuple[ReviewStatus, str]:
        """
        Execute the iterative maker-checker loop

        Returns:
            Tuple of (final_status, next_column_name)
        """

        while cycle_state.current_iteration < cycle_state.max_iterations:
            cycle_state.current_iteration += 1
            iteration = cycle_state.current_iteration

            logger.info(
                f"Review cycle iteration {iteration}/{cycle_state.max_iterations} "
                f"for issue #{cycle_state.issue_number}"
            )

            # Fetch fresh discussion context before each iteration
            # This ensures we get ALL comments including user feedback since the last agent output
            fresh_context = ""
            if cycle_state.workspace_type == 'discussions' and cycle_state.discussion_id:
                fresh_context = await self._get_fresh_discussion_context(
                    cycle_state,
                    org,
                    iteration
                )
                logger.debug(f"Fresh discussion context length: {len(fresh_context)}")

            # Step 1: Execute reviewer agent directly with full context
            review_task_context = self._create_review_task_context(
                cycle_state,
                column,
                issue_data,
                iteration,
                full_discussion_context=fresh_context
            )

            # Execute reviewer agent
            await self._execute_agent_directly(
                cycle_state.reviewer_agent,
                review_task_context,
                cycle_state.project_name
            )

            # Get reviewer's comment from GitHub (workspace-aware)
            review_comment = await self._get_latest_agent_comment(
                cycle_state.issue_number,
                cycle_state.repository,
                cycle_state.reviewer_agent,
                cycle_state.workspace_type,
                cycle_state.discussion_id
            )

            # Store review output
            cycle_state.review_outputs.append({
                'iteration': iteration,
                'output': review_comment,
                'timestamp': datetime.now().isoformat()
            })
            cycle_state.status = 'reviewer_working'
            cycle_state.last_review_comment_id = 'latest'  # TODO: capture actual comment ID
            self._save_cycle_state(cycle_state)

            # Step 2: Parse review feedback
            review_result_parsed = self.review_parser.parse_review(review_comment)

            logger.info(
                f"Review status: {review_result_parsed.status.value}, "
                f"blocking: {review_result_parsed.blocking_count}, "
                f"high: {review_result_parsed.high_severity_count}"
            )

            # Step 3: Make decision based on review status
            if review_result_parsed.status == ReviewStatus.APPROVED:
                # Success! Move to next column
                logger.info(f"Review approved for issue #{cycle_state.issue_number}")
                await self._post_cycle_summary(
                    cycle_state,
                    "APPROVED",
                    f"Review approved after {iteration} iteration(s)"
                )

                # Mark cycle as completed and remove from active state
                cycle_state.status = 'completed'
                self._save_cycle_state(cycle_state)
                self._remove_cycle_state(cycle_state)

                if column.auto_advance_on_approval:
                    next_column = self._get_next_column_name(column)
                    return ReviewStatus.APPROVED, next_column
                else:
                    # Stay in review column
                    return ReviewStatus.APPROVED, column.name

            elif review_result_parsed.status == ReviewStatus.BLOCKED:
                # Blocking issues found
                logger.warning(
                    f"Blocking issues found in review for issue #{cycle_state.issue_number}"
                )

                # Only escalate on the SECOND review (after maker has had a chance to fix)
                # First review with blocking issues: let maker try to fix
                # Second review (iteration > 1) with blocking issues: escalate
                if column.escalate_on_blocked and iteration > 1:
                    await self._escalate_blocked(cycle_state, review_result_parsed)

                    # Save state as awaiting human feedback and RETURN
                    # The project monitor will periodically check for feedback
                    cycle_state.status = 'awaiting_human_feedback'
                    cycle_state.escalation_time = datetime.now().isoformat()
                    self._save_cycle_state(cycle_state)

                    logger.info(
                        f"Review cycle escalated for issue #{cycle_state.issue_number}. "
                        f"Cycle paused, waiting for human feedback. "
                        f"Project monitor will check periodically and resume when feedback is detected."
                    )

                    # Return - don't block! Project monitor will resume this cycle when feedback is detected
                    return ReviewStatus.BLOCKED, column.name
                else:
                    # First iteration with blocking issues: treat as changes requested and give maker a chance
                    if iteration == 1:
                        logger.info("Blocking issues on first review - giving maker a chance to fix")
                    # Continue iteration to invoke maker
                    pass

            # Status is CHANGES_REQUESTED or BLOCKED (without escalation)
            # Check if we've hit max iterations
            if iteration >= cycle_state.max_iterations:
                logger.warning(
                    f"Max iterations ({cycle_state.max_iterations}) reached for "
                    f"issue #{cycle_state.issue_number}"
                )
                await self._escalate_max_iterations(cycle_state, review_result_parsed)
                return ReviewStatus.CHANGES_REQUESTED, column.name

            # Step 4: Re-invoke maker agent with feedback
            logger.info(f"Re-invoking {cycle_state.maker_agent} with reviewer feedback")

            # Fetch fresh context again (now includes reviewer's comment)
            if cycle_state.workspace_type == 'discussions' and cycle_state.discussion_id:
                fresh_context = await self._get_fresh_discussion_context(
                    cycle_state,
                    org,
                    iteration
                )
                logger.debug(f"Fresh context for maker (with review): {len(fresh_context)}")

            maker_task_context = self._create_maker_revision_task_context(
                cycle_state,
                column,
                issue_data,
                review_comment,
                iteration,
                full_discussion_context=fresh_context
            )

            # Execute maker agent directly
            await self._execute_agent_directly(
                cycle_state.maker_agent,
                maker_task_context,
                cycle_state.project_name
            )

            # Get maker's revised output from GitHub (workspace-aware)
            maker_comment = await self._get_latest_agent_comment(
                cycle_state.issue_number,
                cycle_state.repository,
                cycle_state.maker_agent,
                cycle_state.workspace_type,
                cycle_state.discussion_id
            )

            # Verify maker responded
            if not maker_comment:
                logger.error(f"Maker {cycle_state.maker_agent} did not post a response on iteration {iteration}")
                await self._escalate_max_iterations(cycle_state, review_result_parsed)
                return ReviewStatus.CHANGES_REQUESTED, column.name

            # Store maker output
            cycle_state.maker_outputs.append({
                'iteration': iteration,
                'output': maker_comment,
                'timestamp': datetime.now().isoformat()
            })

            # Continue to next iteration - the loop will go back to the reviewer
            logger.info(f"Maker responded, continuing to iteration {iteration + 1} for re-review")

        # Should not reach here, but handle gracefully
        logger.error(f"Review loop exited unexpectedly for issue #{cycle_state.issue_number}")
        return ReviewStatus.UNKNOWN, column.name

    def _create_review_task_context(
        self,
        cycle_state: ReviewCycleState,
        column: WorkflowColumn,
        issue_data: Dict[str, Any],
        iteration: int,
        full_discussion_context: str = ""
    ) -> Dict[str, Any]:
        """Create task context for reviewer agent"""
        # Get the latest maker output
        latest_maker_output = cycle_state.maker_outputs[-1]['output'] if cycle_state.maker_outputs else ""

        # Use full discussion context if provided, otherwise fall back to just the maker output
        context_for_review = full_discussion_context if full_discussion_context else latest_maker_output

        return {
            'project': cycle_state.project_name,
            'board': cycle_state.board_name,
            'repository': cycle_state.repository,
            'issue_number': cycle_state.issue_number,
            'issue': issue_data,
            'column': column.name,
            'trigger': 'review_cycle',
            'workspace_type': cycle_state.workspace_type,
            'discussion_id': cycle_state.discussion_id,
            'review_cycle': {
                'iteration': iteration,
                'max_iterations': cycle_state.max_iterations,
                'maker_agent': cycle_state.maker_agent,
                'previous_maker_output': latest_maker_output,
                'is_rereviewing': iteration > 1
            },
            'previous_stage_output': context_for_review,  # Full discussion context
            'timestamp': datetime.now().isoformat()
        }

    def _create_maker_revision_task_context(
        self,
        cycle_state: ReviewCycleState,
        column: WorkflowColumn,
        issue_data: Dict[str, Any],
        review_feedback: str,
        iteration: int,
        full_discussion_context: str = ""
    ) -> Dict[str, Any]:
        """Create task context for maker agent to address feedback"""
        # Get the maker's LAST output (not the original, but the most recent iteration)
        # This is what the reviewer just reviewed
        last_maker_output = cycle_state.maker_outputs[-1]['output'] if cycle_state.maker_outputs else ""

        # Get the original output for reference
        original_output = cycle_state.maker_outputs[0]['output'] if cycle_state.maker_outputs else ""

        return {
            'project': cycle_state.project_name,
            'board': cycle_state.board_name,
            'repository': cycle_state.repository,
            'issue_number': cycle_state.issue_number,
            'issue': issue_data,
            'column': column.name,
            'trigger': 'review_cycle_revision',  # Specific trigger for review cycle revisions
            'workspace_type': cycle_state.workspace_type,
            'discussion_id': cycle_state.discussion_id,
            'review_cycle': {
                'iteration': iteration,
                'max_iterations': cycle_state.max_iterations,
                'reviewer_agent': cycle_state.reviewer_agent,
                'review_feedback': review_feedback,
                'is_revision': True,
                'full_discussion_context': full_discussion_context  # Keep for reference but don't confuse with output
            },
            'revision': {
                'mode': 'targeted',  # Indicates this is a targeted revision, not full rewrite
                'previous_output': last_maker_output,  # The work being revised
                'original_output': original_output,  # First version for comparison
                'feedback': review_feedback,  # Concise feedback to address
                'formatted_feedback': f"**Review Feedback (Iteration {iteration}):**\n\n{review_feedback}"
            },
            'timestamp': datetime.now().isoformat()
        }

    async def _get_fresh_discussion_context(
        self,
        cycle_state: ReviewCycleState,
        org: str,
        iteration: int
    ) -> str:
        """
        Fetch the latest scoped discussion context for review cycles.

        Returns context containing only:
        - The last comment from the previous agent (maker/reviewer)
        - Human replies threaded to that specific comment

        This prevents context bloat by excluding previous iterations and unrelated comments.
        """
        from services.github_app import github_app

        try:
            # Query for discussion with all comments and replies
            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
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

            result = github_app.graphql_request(query, {'discussionId': cycle_state.discussion_id})
            if not result or 'node' not in result:
                logger.error(f"Failed to fetch discussion {cycle_state.discussion_id}")
                return ""

            all_comments = result['node']['comments']['nodes']

            # Determine which agent's comment we should extract
            # On iteration 0: reviewer sees initial maker's comment
            # On iteration 1+: maker sees last reviewer's comment, reviewer sees last maker's comment
            if iteration == 0:
                # First review - reviewer sees initial maker comment
                previous_agent = cycle_state.maker_agent
            else:
                # Subsequent iterations - depends on current role
                # If we're about to run maker, get reviewer's last comment
                # If we're about to run reviewer, get maker's last comment
                # Since this is called before execution, we need to determine next agent
                # In practice, cycles alternate: maker -> reviewer -> maker -> reviewer
                # On odd iterations (1, 3, 5), maker runs and needs reviewer's feedback
                # On even iterations (2, 4, 6), reviewer runs and needs maker's revision
                if iteration % 2 == 1:
                    # Maker's turn - get reviewer's last comment
                    previous_agent = cycle_state.reviewer_agent
                else:
                    # Reviewer's turn - get maker's last comment
                    previous_agent = cycle_state.maker_agent

            # Find the last comment from the previous agent
            agent_signature = f"_Processed by the {previous_agent} agent_"
            previous_agent_comment = None

            for comment in reversed(all_comments):
                if agent_signature in comment.get('body', ''):
                    previous_agent_comment = comment
                    break

            if not previous_agent_comment:
                logger.warning(f"No comment found from {previous_agent} in discussion {cycle_state.discussion_id}")
                return ""

            # Extract only the threaded replies to this specific comment (human feedback only)
            human_replies = []
            for reply in previous_agent_comment.get('replies', {}).get('nodes', []):
                reply_author = reply.get('author', {})
                reply_author_login = reply_author.get('login', '') if reply_author else ''
                reply_is_bot = 'bot' in reply_author_login.lower()

                if not reply_is_bot:
                    human_replies.append({
                        'author': reply_author_login,
                        'body': reply.get('body', ''),
                        'created_at': reply.get('createdAt', '')
                    })

            # Build scoped context
            context = previous_agent_comment.get('body', '')

            # Add human feedback if present
            if human_replies:
                context += "\n\n## Human Feedback:\n"
                for feedback in human_replies:
                    context += f"\n**Feedback from @{feedback['author']}:**\n{feedback['body']}\n"

            logger.debug(
                f"Built scoped context for {previous_agent}: "
                f"{len(context)} chars, {len(human_replies)} human replies"
            )
            return context

        except Exception as e:
            logger.error(f"Error fetching fresh discussion context: {e}")
            import traceback
            traceback.print_exc()
            return ""

    async def _execute_agent_directly(
        self,
        agent_name: str,
        task_context: Dict[str, Any],
        project_name: str
    ):
        """Execute agent using centralized executor (ensures observability)"""
        from services.agent_executor import get_agent_executor

        logger.info(f"Executing {agent_name} directly for review cycle")

        executor = get_agent_executor()
        return await executor.execute_agent(
            agent_name=agent_name,
            project_name=project_name,
            task_context=task_context,
            task_id_prefix="review_cycle"
        )

    async def _get_latest_agent_comment(
        self,
        issue_number: int,
        repository: str,
        agent_name: str,
        workspace_type: str = 'issues',
        discussion_id: Optional[str] = None
    ) -> str:
        """Get the most recent comment from a specific agent (workspace-aware)"""
        import subprocess
        import json

        try:
            agent_signature = f"_Processed by the {agent_name} agent_"

            if workspace_type == 'discussions' and discussion_id:
                # Fetch from discussion
                from services.github_app import github_app

                query = """
                query($discussionId: ID!) {
                  node(id: $discussionId) {
                    ... on Discussion {
                      comments(first: 100) {
                        nodes {
                          id
                          body
                          createdAt
                          replies(first: 50) {
                            nodes {
                              id
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

                result = github_app.graphql_request(query, {'discussionId': discussion_id})
                if not result or 'node' not in result:
                    logger.error(f"Failed to get discussion comments for {discussion_id}")
                    return ""

                all_comments = result['node']['comments']['nodes']

                # Search both top-level comments and replies
                latest_match = None
                latest_time = None

                for comment in all_comments:
                    # Check top-level comment
                    if agent_signature in comment.get('body', ''):
                        from dateutil import parser as date_parser
                        comment_time = date_parser.parse(comment.get('createdAt'))
                        if latest_time is None or comment_time > latest_time:
                            latest_match = comment.get('body', '')
                            latest_time = comment_time

                    # Check replies
                    for reply in comment.get('replies', {}).get('nodes', []):
                        if agent_signature in reply.get('body', ''):
                            from dateutil import parser as date_parser
                            reply_time = date_parser.parse(reply.get('createdAt'))
                            if latest_time is None or reply_time > latest_time:
                                latest_match = reply.get('body', '')
                                latest_time = reply_time

                if latest_match:
                    return latest_match
                else:
                    logger.warning(f"No comment found from {agent_name} in discussion {discussion_id}")
                    return ""

            else:
                # Fetch from issue (original behavior)
                result = subprocess.run(
                    ['gh', 'issue', 'view', str(issue_number), '--repo', f"{repository}", '--json', 'comments'],
                    capture_output=True,
                    text=True,
                    check=True
                )

                data = json.loads(result.stdout)
                comments = data.get('comments', [])

                # Find the most recent comment from this agent
                for comment in reversed(comments):
                    if agent_signature in comment.get('body', ''):
                        return comment.get('body', '')

                logger.warning(f"No comment found from {agent_name} on issue #{issue_number}")
                return ""

        except Exception as e:
            logger.error(f"Error fetching agent comment: {e}")
            return ""

    async def _post_cycle_summary(
        self,
        cycle_state: ReviewCycleState,
        final_status: str,
        message: str
    ):
        """Post a summary comment about the review cycle (workspace-aware)"""
        summary = f"""## 🔄 Review Cycle Complete

**Status**: {final_status}
**Iterations**: {cycle_state.current_iteration}/{cycle_state.max_iterations}
**Maker**: {cycle_state.maker_agent.replace('_', ' ').title()}
**Reviewer**: {cycle_state.reviewer_agent.replace('_', ' ').title()}

{message}

---
_Automated review cycle by Claude Code Orchestrator_
"""

        if cycle_state.workspace_type == 'discussions' and cycle_state.discussion_id:
            # Post to discussion
            from services.github_discussions import GitHubDiscussions
            discussions = GitHubDiscussions()
            discussions.add_discussion_comment(cycle_state.discussion_id, summary)
        else:
            # Post to issue
            await self.github.post_issue_comment(
                cycle_state.issue_number,
                summary,
                cycle_state.repository
            )

    async def _escalate_blocked(self, cycle_state: ReviewCycleState, review_result):
        """Escalate when blocking issues are found (workspace-aware)"""
        from services.github_integration import GitHubIntegration
        import subprocess

        # Add label (only for issues)
        if cycle_state.workspace_type == 'issues':
            subprocess.run(
                ['gh', 'issue', 'edit', str(cycle_state.issue_number),
                 '--repo', cycle_state.repository,
                 '--add-label', 'needs-human-review'],
                capture_output=True
            )

        # Post escalation comment
        blocking_issues = [
            f"- **{f.category}**: {f.message}"
            for f in review_result.findings
            if f.severity == 'blocking'
        ]

        escalation_comment = f"""## 🚫 Review Blocked - Human Review Required

The automated review identified **{review_result.blocking_count} blocking issue(s)** that require human attention.

### Blocking Issues
{chr(10).join(blocking_issues)}

### Next Steps
The review cycle is **paused and waiting for your feedback**. Please:

1. Review the blocking issues above
2. Post a comment with your guidance (the orchestrator will detect your response automatically)
3. The **{cycle_state.reviewer_agent}** will incorporate your feedback and post an updated review
4. The review cycle will then resume based on the updated review

**Your feedback can include:**
- Corrections to the reviewer's assessment
- Additional context or clarification
- Directions for the business analyst on how to proceed

**Iteration**: {cycle_state.current_iteration}/{cycle_state.max_iterations}

---
_Escalated by Claude Code Orchestrator - Monitoring for your response..._
"""

        if cycle_state.workspace_type == 'discussions' and cycle_state.discussion_id:
            # Post to discussion
            from services.github_discussions import GitHubDiscussions
            discussions = GitHubDiscussions()
            discussions.add_discussion_comment(cycle_state.discussion_id, escalation_comment)
        else:
            # Post to issue
            await self.github.post_issue_comment(
                cycle_state.issue_number,
                escalation_comment,
                cycle_state.repository
            )

        logger.warning(
            f"Escalated blocking issues for issue #{cycle_state.issue_number}: "
            f"{review_result.blocking_count} blocking"
        )

    async def _escalate_max_iterations(self, cycle_state: ReviewCycleState, review_result):
        """Escalate when max iterations reached without approval (workspace-aware)"""
        import subprocess

        # Add label (only for issues)
        if cycle_state.workspace_type == 'issues':
            subprocess.run(
                ['gh', 'issue', 'edit', str(cycle_state.issue_number),
                 '--repo', cycle_state.repository,
                 '--add-label', 'needs-human-review'],
                capture_output=True
            )

        # Post escalation comment
        escalation_comment = f"""## ⚠️ Max Review Iterations Reached

The automated review cycle has reached the maximum iterations ({cycle_state.max_iterations}) without approval.

### Current Status
- **Remaining Issues**: {len(review_result.findings)}
- **High Severity**: {review_result.high_severity_count}
- **Quality Score**: {review_result.score:.1%}

### Summary
{review_result.summary}

### Next Steps
The review cycle has paused for human intervention. Please:
1. Review the current state and feedback
2. Decide whether to accept the work as-is
3. Provide additional guidance if needed
4. Manually move the card when ready

---
_Escalated by Claude Code Orchestrator_
"""

        if cycle_state.workspace_type == 'discussions' and cycle_state.discussion_id:
            # Post to discussion
            from services.github_discussions import GitHubDiscussions
            discussions = GitHubDiscussions()
            discussions.add_discussion_comment(cycle_state.discussion_id, escalation_comment)
        else:
            # Post to issue
            await self.github.post_issue_comment(
                cycle_state.issue_number,
                escalation_comment,
                cycle_state.repository
            )

        logger.warning(
            f"Escalated max iterations for issue #{cycle_state.issue_number}: "
            f"{cycle_state.current_iteration}/{cycle_state.max_iterations}"
        )

    def _get_next_column_name(self, current_column: WorkflowColumn) -> str:
        """Determine next column name after approval"""
        if not hasattr(self, 'workflow_columns') or not self.workflow_columns:
            logger.warning("No workflow columns available, cannot determine next column")
            return current_column.name

        # Find current column index
        current_index = -1
        for i, col in enumerate(self.workflow_columns):
            if col.name == current_column.name:
                current_index = i
                break

        if current_index == -1:
            logger.warning(f"Current column {current_column.name} not found in workflow")
            return current_column.name

        # Get next column
        if current_index + 1 < len(self.workflow_columns):
            next_col = self.workflow_columns[current_index + 1]
            logger.info(f"Next column after {current_column.name}: {next_col.name}")
            return next_col.name
        else:
            logger.info(f"No next column after {current_column.name}, staying in current")
            return current_column.name


# Global executor instance
review_cycle_executor = ReviewCycleExecutor()
