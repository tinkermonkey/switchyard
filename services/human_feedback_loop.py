"""
Human Feedback Loop

Handles conversational workflows where agents respond to human feedback in discussion threads.
Pattern: agent produces output → monitor for human feedback → agent responds to feedback → repeat

For automated maker-checker review workflows, see review_cycle.py instead.
"""

import asyncio
import logging
import re
import subprocess
import threading
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
        # FIX #3: Track which comments we've already responded to (prevents duplicate responses)
        self.processed_comment_ids: set = set()


class HumanFeedbackLoopExecutor:
    """Executor for human feedback loops in discussion threads"""

    def __init__(self):
        self.review_parser = ReviewParser()
        # Don't initialize GitHubIntegration here - create it per-loop with proper repo context
        self.active_loops = {}  # Track active loops by issue number
        self._initialized = False
        self._stop_events: Dict[str, threading.Event] = {}  # Active stop signals keyed by "project:issue"

    def _update_loop_heartbeat(self, project_name: str, issue_number: int) -> None:
        """
        Update heartbeat timestamp for active feedback loop.

        Used for monitoring loop health and detecting stuck loops.
        Heartbeat is stored in Redis with a 5-minute TTL (10x poll interval for safety margin).
        """
        heartbeat_key = f"orchestrator:feedback_loop:heartbeat:{project_name}:{issue_number}"

        try:
            import redis
            redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
            redis_client.setex(
                heartbeat_key,
                300,  # 5 minute TTL (10x poll interval for safety margin)
                datetime.utcnow().isoformat()
            )
        except Exception as e:
            # Don't fail the loop if heartbeat update fails - it's just for monitoring
            logger.warning(f"Failed to update feedback loop heartbeat for {project_name}#{issue_number}: {e}")

    async def initialize(self):
        """Initialize executor and clean up stale locks from previous orchestrator runs"""
        if self._initialized:
            return

        logger.info("Initializing HumanFeedbackLoopExecutor and cleaning up stale locks...")

        # Clear in-memory state (always stale after restart)
        if self.active_loops:
            logger.warning(
                f"Clearing {len(self.active_loops)} stale in-memory loop states from previous run"
            )
            self.active_loops.clear()

        # Clean up stale Redis locks that don't have corresponding running containers
        await self._cleanup_stale_redis_locks()

        self._initialized = True
        logger.info("HumanFeedbackLoopExecutor initialization complete")

    async def _cleanup_stale_redis_locks(self):
        """
        Clean up Redis locks that don't have corresponding running agent containers.

        SAFETY: Only clears locks when:
        1. No Docker container exists with matching name pattern
        2. No Redis container tracking key exists
        3. Container exists but is not running (exited/stopped)

        This is safe across orchestrator restarts because:
        - Running agents have both: active container + Redis tracking key
        - Stale locks from crashes/forced stops have neither
        """
        try:
            import redis
            redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)

            # Find all conversational loop locks
            lock_pattern = "orchestrator:conversational_loop:*"
            lock_keys = redis_client.keys(lock_pattern)

            if not lock_keys:
                logger.info("No conversational loop locks found in Redis")
                return

            logger.info(f"Checking {len(lock_keys)} conversational loop locks for stale entries...")

            cleaned_count = 0
            for lock_key in lock_keys:
                try:
                    # Parse lock key: orchestrator:conversational_loop:{project}:{issue_number}
                    parts = lock_key.split(':')
                    if len(parts) != 4:
                        logger.warning(f"Malformed lock key: {lock_key}")
                        continue

                    project = parts[2]
                    issue_number = parts[3]
                    agent = redis_client.get(lock_key)  # Lock value = agent name

                    # Check if container exists for this lock
                    # Container naming: claude-agent-{project}-{task_id}
                    # We need to check all containers for this project
                    container_pattern = f"claude-agent-{project}-*"

                    result = subprocess.run(
                        ['docker', 'ps', '--filter', f'name={container_pattern}', '--format', '{{.Names}}'],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )

                    running_containers = []
                    if result.returncode == 0 and result.stdout.strip():
                        running_containers = [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]

                    # Check if any running container is associated with this issue
                    has_active_container = False
                    for container_name in running_containers:
                        # Check Redis tracking for this container
                        tracking_key = f'agent:container:{container_name}'
                        container_info = redis_client.hgetall(tracking_key)

                        if container_info:
                            tracked_issue = container_info.get('issue_number', '')
                            tracked_agent = container_info.get('agent', '')

                            # Match by issue number AND agent (must be exact match)
                            if tracked_issue == issue_number and tracked_agent == agent:
                                has_active_container = True
                                logger.info(
                                    f"✅ Lock {lock_key} is VALID: active container {container_name} "
                                    f"for issue #{issue_number} (agent={agent})"
                                )
                                break

                    # If no active container found, this is a stale lock
                    if not has_active_container:
                        redis_client.delete(lock_key)
                        cleaned_count += 1
                        logger.warning(
                            f"🧹 Cleaned up STALE lock: {lock_key} "
                            f"(issue #{issue_number}, agent={agent}, no active container found)"
                        )

                except subprocess.TimeoutExpired:
                    logger.warning(f"Timeout checking containers for lock {lock_key}")
                except Exception as e:
                    logger.warning(f"Error checking lock {lock_key}: {e}")

            if cleaned_count > 0:
                logger.info(f"Cleaned up {cleaned_count} stale conversational loop locks")
            else:
                logger.info("No stale conversational loop locks found")

        except Exception as e:
            logger.error(f"Error during stale lock cleanup: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def cleanup_loop(self, project_name: str, issue_number: int, reason: str = "issue moved to non-agent column") -> bool:
        """
        Safely clean up in-memory state and Redis lock for a specific conversational loop.

        This is called when an issue moves out of an agent column (e.g., moved to Backlog).

        Args:
            project_name: Project name (e.g., 'documentation_robotics')
            issue_number: Issue number
            reason: Human-readable reason for cleanup (for logging)

        Returns:
            True if cleanup was performed, False if loop wasn't active

        Safety guarantees:
        - Defensive: Only cleans up if loop exists in active_loops
        - Non-blocking: Never raises exceptions (logs errors instead)
        - Idempotent: Safe to call multiple times for same issue
        """
        try:
            # Signal the running loop thread to stop
            self.request_stop(project_name, issue_number)

            # Clean up in-memory state
            if issue_number in self.active_loops:
                state = self.active_loops[issue_number]
                agent = state.agent
                del self.active_loops[issue_number]
                logger.info(
                    f"🧹 Cleaned up conversational loop for {project_name}#{issue_number} "
                    f"(agent={agent}, reason={reason})"
                )

                # Clean up Redis lock
                try:
                    from services.redis_client import get_redis_client
                    redis_client = get_redis_client()
                    lock_key = f"orchestrator:conversational_loop:{project_name}:{issue_number}"

                    if redis_client.exists(lock_key):
                        redis_client.delete(lock_key)
                        logger.debug(f"Deleted Redis lock: {lock_key}")
                except Exception as e:
                    # Non-critical: Lock will eventually be cleaned up by stale lock cleanup
                    logger.warning(f"Failed to delete Redis lock for {project_name}#{issue_number}: {e}")

                return True
            else:
                logger.debug(
                    f"No active conversational loop to clean up for {project_name}#{issue_number} "
                    f"(reason={reason})"
                )
                return False

        except Exception as e:
            # Never raise - this is a cleanup operation that shouldn't break the caller
            logger.error(
                f"Error cleaning up conversational loop for {project_name}#{issue_number}: {e}",
                exc_info=True
            )
            return False

    def request_stop(self, project_name: str, issue_number: int) -> bool:
        """Signal an active feedback loop to stop. Thread-safe."""
        stop_key = f"{project_name}:{issue_number}"
        event = self._stop_events.get(stop_key)
        if event:
            event.set()
            logger.info(f"Sent stop signal to feedback loop for {project_name}#{issue_number}")
            return True
        logger.debug(
            f"No active stop event for {project_name}#{issue_number} "
            f"(loop may not have started yet or already exited)"
        )
        return False

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
        # Ensure executor is initialized (cleans up stale locks on first call)
        await self.initialize()

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
            discussion_id=discussion_id,
            pipeline_run_id=pipeline_run_id
        )

        # FIX #1: Check if loop already active (in-memory duplicate prevention)
        if issue_number in self.active_loops:
            existing_agent = self.active_loops[issue_number].agent

            # If same agent, it's a duplicate - reject immediately
            if existing_agent == column.agent:
                logger.warning(
                    f"⚠️  Conversational loop already active for issue #{issue_number}, "
                    f"skipping duplicate start (agent={column.agent}, project={project_name})"
                )
                return (None, False)

            # Different agent - agent transition during manual progression
            # The old agent's monitoring loop will exit on its next poll (within 30s)
            # We can proceed immediately and let the old loop clean up naturally
            logger.info(
                f"Agent transition detected for issue #{issue_number}: "
                f"{existing_agent} → {column.agent}. "
                f"Proceeding with transition (old loop will exit on next poll)."
            )

        # FIX #2: Acquire/update Redis distributed lock (prevents duplicate loops across instances)
        import redis
        redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
        lock_key = f"orchestrator:conversational_loop:{project_name}:{issue_number}"

        # Check if lock already exists
        existing_agent = redis_client.get(lock_key)

        if existing_agent is None:
            # No lock exists - acquire it with nx=True to prevent race conditions
            lock_acquired = redis_client.set(lock_key, column.agent, nx=True, ex=1800)
            if not lock_acquired:
                # Race condition - another instance acquired between GET and SET
                existing_agent = redis_client.get(lock_key)
                logger.warning(
                    f"⚠️  Lock acquired by another instance during race: "
                    f"issue #{issue_number} locked by {existing_agent}"
                )
                # Fall through to existing_agent handling below
            else:
                logger.info(
                    f"✅ Acquired new distributed lock for conversational loop: "
                    f"{project_name}#{issue_number} (agent={column.agent})"
                )

        if existing_agent is not None:
            # Lock exists - check if it's the same agent or different
            if existing_agent == column.agent:
                # Same agent - duplicate loop attempt
                logger.warning(
                    f"⚠️  Conversational loop already active for issue #{issue_number}, "
                    f"locked by same agent {existing_agent}. Skipping duplicate start."
                )
                return (None, False)
            else:
                # Different agent - agent transition during manual progression
                # Simply update the lock value to the new agent (no waiting needed!)
                redis_client.set(lock_key, column.agent, ex=1800)  # No nx=True - just update
                logger.info(
                    f"✅ Updated lock ownership for issue #{issue_number}: "
                    f"{existing_agent} → {column.agent} (agent transition)"
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
            # CRITICAL FIX: Load previous agent outputs BEFORE checking for initial user request
            # This ensures state.agent_outputs is populated so we can correctly detect threaded conversations
            logger.info(f"Loading previous agent outputs for issue #{issue_number} from {workspace_type} workspace")
            if workspace_type == 'discussions' and discussion_id:
                await self._load_previous_outputs_from_discussion(state, org)
            else:
                await self._load_previous_outputs_from_issue(state, org)
            
            # Check for existing user comments to reply to (instead of posting new top-level)
            initial_feedback = await self._get_initial_user_request(state, org)
            
            if initial_feedback:
                # CRITICAL FIX: When replying to existing feedback, set is_initial=False
                # This ensures the agent uses question/revision mode instead of initial mode
                logger.info(f"Found initial user request from {initial_feedback['author']}, replying to it as feedback")
                await self._execute_agent(
                    state,
                    column,
                    issue_data,
                    previous_stage_output,
                    org,
                    is_initial=False,  # FIX: False because we're responding to existing feedback
                    human_feedback=initial_feedback
                )
            else:
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
            # Clean up in-memory state
            if issue_number in self.active_loops:
                del self.active_loops[issue_number]

            # Release Redis distributed lock
            try:
                redis_client.delete(lock_key)
                logger.info(
                    f"✅ Released distributed lock for conversational loop: "
                    f"{project_name}#{issue_number}"
                )
            except Exception as e:
                logger.error(f"Failed to release distributed lock for issue #{issue_number}: {e}")

            # Clean up heartbeat key
            try:
                heartbeat_key = f"orchestrator:feedback_loop:heartbeat:{project_name}:{issue_number}"
                redis_client.delete(heartbeat_key)
                logger.debug(f"Deleted heartbeat key: {heartbeat_key}")
            except Exception as e:
                logger.warning(f"Failed to delete heartbeat key for issue #{issue_number}: {e}")

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

        # Register stop event so external callers can signal this loop to exit
        stop_key = f"{state.project_name}:{state.issue_number}"
        stop_event = threading.Event()
        self._stop_events[stop_key] = stop_event

        logger.info(
            f"Monitoring discussion {state.discussion_id} for human feedback "
            f"(will poll indefinitely until card moves to different column)"
        )

        # Emit decision event for feedback listening started
        from monitoring.decision_events import DecisionEventEmitter
        from monitoring.observability import get_observability_manager

        obs = get_observability_manager()
        decision_events = DecisionEventEmitter(obs)

        decision_events.emit_feedback_listening_started(
            issue_number=state.issue_number,
            project=state.project_name,
            board=state.board_name,
            agent=state.agent,
            monitoring_for=['discussion_replies', 'issue_comments'],
            workspace_type=state.workspace_type,
            pipeline_run_id=state.pipeline_run_id
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

        # CRITICAL FIX: Check for existing unprocessed feedback IMMEDIATELY before entering poll loop
        # This handles cases where:
        # 1. Orchestrator restarts and resumes monitoring (feedback may have arrived during downtime)
        # 2. Human posted feedback while agent was executing
        # 3. Race condition where feedback arrives between agent completion and monitoring start
        logger.info("Checking for existing unprocessed feedback before starting poll loop...")
        try:
            initial_human_feedback = await self._get_human_feedback_since_last_agent(
                state,
                org
            )
            
            if initial_human_feedback:
                logger.info(
                    f"Found existing unprocessed feedback from {initial_human_feedback['author']}, "
                    f"processing immediately (before entering poll loop)"
                )
                state.current_iteration += 1

                # Emit decision event for feedback detection
                from monitoring.decision_events import DecisionEventEmitter
                from monitoring.observability import get_observability_manager
                
                obs = get_observability_manager()
                decision_events = DecisionEventEmitter(obs)
                
                has_parent_comment = 'parent_comment' in initial_human_feedback and initial_human_feedback['parent_comment'] is not None
                predicted_mode = 'question' if has_parent_comment else 'revision'
                action_description = f"route_to_agent_{state.agent}_in_{predicted_mode}_mode"
                
                decision_events.emit_feedback_detected(
                    issue_number=state.issue_number,
                    project=state.project_name,
                    board=state.board_name,
                    feedback_source='discussion_reply' if state.workspace_type == 'discussions' else 'issue_comment',
                    feedback_content=initial_human_feedback.get('body', ''),
                    target_agent=state.agent,
                    action_taken=action_description,
                    workspace_type=state.workspace_type,
                    discussion_id=state.discussion_id,
                    pipeline_run_id=state.pipeline_run_id
                )

                # Execute agent with feedback
                await self._execute_agent(
                    state,
                    column,
                    issue_data,
                    None,
                    org,
                    is_initial=False,
                    human_feedback=initial_human_feedback
                )

                # Mark comment as processed
                comment_id = initial_human_feedback.get('comment_id')
                if comment_id:
                    state.processed_comment_ids.add(comment_id)
                    logger.info(f"✅ Marked comment {comment_id} as processed")
            else:
                logger.info("No existing unprocessed feedback found, entering poll loop...")
        except Exception as e:
            logger.error(f"Error checking for initial feedback: {e}")
            import traceback
            logger.error(traceback.format_exc())

        # Check if stop was requested during initial feedback processing
        if stop_event.is_set():
            logger.info(
                f"Stop signal received for issue #{state.issue_number} during initial processing. "
                f"Exiting feedback loop."
            )
            try:
                decision_events.emit_feedback_listening_stopped(
                    issue_number=state.issue_number,
                    project=state.project_name,
                    board=state.board_name,
                    agent=state.agent,
                    reason="Stop signal received (issue progressed to next column)",
                    feedback_received=state.current_iteration > 0,
                    pipeline_run_id=state.pipeline_run_id
                )
            except Exception as e:
                logger.warning(f"Failed to emit feedback_listening_stopped event: {e}")
            return (None, True)

        try:
            while True:
                # Sleep with 1-second granularity so stop signals are detected quickly
                for _ in range(poll_interval):
                    if stop_event.is_set():
                        logger.info(
                            f"Stop signal received for issue #{state.issue_number}. "
                            f"Exiting feedback loop."
                        )
                        try:
                            decision_events.emit_feedback_listening_stopped(
                                issue_number=state.issue_number,
                                project=state.project_name,
                                board=state.board_name,
                                agent=state.agent,
                                reason="Stop signal received (issue progressed to next column)",
                                feedback_received=state.current_iteration > 0,
                                pipeline_run_id=state.pipeline_run_id
                            )
                        except Exception as e:
                            logger.warning(f"Failed to emit feedback_listening_stopped event: {e}")
                        return (None, True)
                    await asyncio.sleep(1)

                poll_count += 1

                # Update heartbeat in Redis for monitoring/stuck loop detection
                self._update_loop_heartbeat(state.project_name, state.issue_number)

                # DEBUG: Log that we are polling
                if poll_count % 2 == 0:  # Log every minute
                    logger.debug(f"Polling for feedback on discussion {state.discussion_id} (iteration {poll_count})")

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
                        discussion_id=state.discussion_id,
                        pipeline_run_id=state.pipeline_run_id
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

                    # FIX #3: Mark comment as processed to prevent duplicate responses
                    comment_id = human_feedback.get('comment_id')
                    if comment_id:
                        state.processed_comment_ids.add(comment_id)
                        logger.info(f"✅ Marked comment {comment_id} as processed")

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
                    monitor = ProjectMonitor(None)

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

                        # Emit feedback listening stopped event
                        decision_events.emit_feedback_listening_stopped(
                            issue_number=state.issue_number,
                            project=state.project_name,
                            board=state.board_name,
                            agent=state.agent,
                            reason=f"Card moved from '{column.name}' to '{current_column}'",
                            feedback_received=state.current_iteration > 0,
                            pipeline_run_id=state.pipeline_run_id
                        )

                        return (None, True)  # Exit the loop

                    # Special case: If issue is in Backlog (no agent), stop monitoring
                    if current_column and current_column.lower() == 'backlog':
                        logger.info(
                            f"Issue #{state.issue_number} is in Backlog column. "
                            f"Stopping feedback monitoring."
                        )

                        # Emit feedback listening stopped event
                        decision_events.emit_feedback_listening_stopped(
                            issue_number=state.issue_number,
                            project=state.project_name,
                            board=state.board_name,
                            agent=state.agent,
                            reason="Card moved to Backlog",
                            feedback_received=state.current_iteration > 0,
                            pipeline_run_id=state.pipeline_run_id
                        )

                        return (None, True)  # Exit the loop

                except Exception as e:
                    logger.warning(f"Could not check current column for issue #{state.issue_number}: {e}")
        finally:
            self._stop_events.pop(stop_key, None)

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
            
            logger.info(f"Processing feedback from {feedback_author}. Comment ID: {comment_id}, Parent: {parent_comment.get('id') if parent_comment else 'None'}")

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
            else:
                # Fallback: if no comment ID, we can't thread, but we should log it
                logger.warning("No comment_id or parent_comment found in feedback - cannot thread reply")

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

        # Execute agent via centralized executor (ensures observability)
        # Mode detection happens in base_maker_agent._determine_execution_mode()
        logger.info(f"Executing {state.agent} (iteration {state.current_iteration}, is_initial={is_initial})")

        from services.agent_executor import get_agent_executor
        from services.work_execution_state import work_execution_tracker

        # Record execution start in work execution state
        trigger_source = 'human_feedback_loop_initial' if is_initial else 'human_feedback_loop_response'

        work_execution_tracker.record_execution_start(
            issue_number=state.issue_number,
            column=column.name,
            agent=state.agent,
            trigger_source=trigger_source,
            project_name=state.project_name
        )

        logger.info(
            f"Recorded execution start for {state.agent} on {state.project_name}/#{state.issue_number} "
            f"in column {column.name} (trigger: {trigger_source})"
        )

        executor = get_agent_executor()
        result = await executor.execute_agent(
            agent_name=state.agent,
            project_name=state.project_name,
            task_context=context,
            execution_type="conversational"
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
                workspace_type=state.workspace_type,
                pipeline_run_id=state.pipeline_run_id
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

    async def _get_initial_user_request(
        self,
        state: HumanFeedbackState,
        org: str
    ) -> Optional[Dict[str, Any]]:
        """
        Check for an initial user request/comment to reply to.
        Used when starting the loop to determine if we should reply to an existing comment
        instead of posting a new top-level comment.
        """
        # For issues workspace, use issue comment API
        if state.workspace_type == 'issues' or state.discussion_id is None:
            # For issues, we check comments on the issue
            return await self._get_initial_user_request_from_issue(state, org)

        # For discussions workspace, use discussion API
        from services.github_app import github_app
        from dateutil import parser as date_parser

        try:
            logger.debug(f"Checking discussion {state.discussion_id} for initial user request")

            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
                  comments(last: 100) {
                    nodes {
                      id
                      author { login }
                      body
                      createdAt
                      replies(last: 100) {
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

            if not result or 'node' not in result or not result['node']:
                return None

            comments = result['node']['comments']['nodes']
            
            # GitHub discussions use nested structure: top-level comments with nested replies
            # We need to check both top-level comments and their replies for user requests
            
            logger.info(f"DEBUG: Checking {len(comments)} comments for initial user request")
            
            most_recent_comment = None
            most_recent_time = None
            
            for comment in comments:
                if 'author' not in comment or 'createdAt' not in comment:
                    continue
                
                # Check top-level comment
                author = comment['author']['login']
                created_at = date_parser.parse(comment['createdAt'])
                comment_id = comment.get('id')
                
                logger.info(f"DEBUG: Comment {comment_id} from {author} at {created_at}")
                
                # Skip bot comments
                if not is_bot_user(author):
                    # Check for agent signature (skip if it's an agent output)
                    body = comment.get('body', '')
                    has_signature = "_Processed by the " in body
                    is_quote = False
                    
                    if has_signature:
                        # Check if signature line starts with > (quoted)
                        for line in body.split('\n'):
                            if "_Processed by the " in line and line.strip().startswith('>'):
                                is_quote = True
                                break
                    
                    if not has_signature or is_quote:
                        # This is a valid user comment
                        if most_recent_time is None or created_at > most_recent_time:
                            logger.info(f"DEBUG:   ✅ New most recent comment: {author} at {created_at}")
                            most_recent_time = created_at
                            most_recent_comment = {
                                'author': author,
                                'body': body,
                                'created_at': created_at.isoformat(),
                                'comment_id': comment_id,
                                'parent_comment': None
                            }
                        else:
                            logger.info(f"DEBUG:   Older than current most recent ({most_recent_time})")
                    else:
                        logger.info(f"DEBUG:   Skipping - has agent signature (not quoted)")
                else:
                    logger.info(f"DEBUG:   Skipping - bot user")
                
                # Check nested replies
                replies = comment.get('replies', {}).get('nodes', [])
                logger.info(f"DEBUG: Checking {len(replies)} replies to comment {comment_id}")
                
                for reply in replies:
                    if 'author' not in reply or 'createdAt' not in reply:
                        continue
                    
                    reply_author = reply['author']['login']
                    reply_created_at = date_parser.parse(reply['createdAt'])
                    reply_id = reply.get('id')
                    
                    logger.info(f"DEBUG: Reply {reply_id} from {reply_author} at {reply_created_at}")
                    
                    # Skip bot replies
                    if is_bot_user(reply_author):
                        logger.info(f"DEBUG:   Skipping - bot user")
                        continue
                    
                    # Check for agent signature
                    reply_body = reply.get('body', '')
                    has_signature = "_Processed by the " in reply_body
                    is_quote = False
                    
                    if has_signature:
                        for line in reply_body.split('\n'):
                            if "_Processed by the " in line and line.strip().startswith('>'):
                                is_quote = True
                                break
                    
                    if has_signature and not is_quote:
                        logger.info(f"DEBUG:   Skipping - has agent signature (not quoted)")
                        continue
                    
                    # This is a valid user reply
                    # CRITICAL: Only consider this reply if it's to THIS agent's comment
                    # This prevents agent A from replying in-thread to conversations with agent B
                    comment_body = comment.get('body', '')
                    agent_signature = f"_Processed by the {state.agent} agent_"
                    
                    if agent_signature not in comment_body:
                        logger.info(f"DEBUG:   Skipping reply - parent comment not from {state.agent}")
                        continue
                    
                    if most_recent_time is None or reply_created_at > most_recent_time:
                        logger.info(f"DEBUG:   ✅ New most recent comment: {reply_author} at {reply_created_at} (reply to {author} [{state.agent}])")
                        most_recent_time = reply_created_at
                        
                        # Build parent comment info (the top-level comment being replied to)
                        parent_comment = {
                            'id': comment_id,
                            'author': author,
                            'body': comment_body
                        }
                        
                        most_recent_comment = {
                            'author': reply_author,
                            'body': reply_body,
                            'created_at': reply_created_at.isoformat(),
                            'comment_id': reply_id,
                            'parent_comment': parent_comment
                        }
                    else:
                        logger.info(f"DEBUG:   Older than current most recent ({most_recent_time})")
            
            if most_recent_comment:
                logger.info(f"Found initial user request from {most_recent_comment['author']} at {most_recent_comment['created_at']} (ID: {most_recent_comment['comment_id']})")
            else:
                logger.info("No initial user request found in discussion comments (checked last 100 comments/replies)")

            return most_recent_comment

        except Exception as e:
            logger.error(f"Error checking for initial user request: {e}")
            return None

    async def _get_initial_user_request_from_issue(
        self,
        state: HumanFeedbackState,
        org: str
    ) -> Optional[Dict[str, Any]]:
        """Check for initial user request in issue comments"""
        from services.github_app import github_app
        from dateutil import parser as date_parser

        try:
            query = """
            query($org: String!, $repo: String!, $number: Int!) {
              repository(owner: $org, name: $repo) {
                issue(number: $number) {
                  comments(last: 20) {
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

            if not github_app.enabled:
                return None

            result = github_app.graphql_request(query, {
                'org': org,
                'repo': state.repository,
                'number': state.issue_number
            })

            if not result or 'repository' not in result or not result['repository']:
                return None

            comments = result['repository']['issue']['comments']['nodes']
            
            most_recent_comment = None
            most_recent_time = None
            
            for comment in comments:
                if 'author' in comment and 'createdAt' in comment:
                    author = comment['author']['login']
                    created_at = date_parser.parse(comment['createdAt'])
                    comment_id = comment.get('id')
                    body = comment.get('body', '')
                    
                    # Skip if already processed
                    if comment_id in state.processed_comment_ids:
                        logger.debug(f"Skipping already-processed comment {comment_id}")
                        continue
                    
                    if not is_bot_user(author):
                        # Check for agent signatures
                        has_signature = "_Processed by the " in body
                        agent_signature = f"_Processed by the {state.agent} agent_"
                        
                        # Skip if this is an agent output (not from this agent)
                        if has_signature and agent_signature not in body:
                            logger.debug(f"Skipping comment discussing different agent's work")
                            continue
                        
                        # Skip if this is our own agent output
                        if agent_signature in body:
                            logger.debug(f"Skipping our own agent output")
                            continue
                             
                        if most_recent_time is None or created_at > most_recent_time:
                            most_recent_time = created_at
                            most_recent_comment = {
                                'author': author,
                                'body': body,
                                'created_at': created_at.isoformat(),
                                'comment_id': comment_id,
                                'parent_comment': None
                            }
            
            return most_recent_comment

        except Exception as e:
            logger.error(f"Error checking for initial user request from issue: {e}")
            return None

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
                    comment_id = comment.get('id')

                    if created_at.tzinfo:
                        created_at = created_at.replace(tzinfo=None)

                    # FIX #3: Skip if already processed
                    if comment_id in state.processed_comment_ids:
                        logger.debug(f"Skipping already-processed issue comment {comment_id}")
                        continue

                    if not is_bot_user(author) and created_at > last_agent_time:
                        body = comment.get('body', '')
                        
                        # Check for agent signatures
                        has_signature = "_Processed by the " in body
                        agent_signature = f"_Processed by the {state.agent} agent_"
                        
                        # Skip if this is an agent output (not from this agent)
                        if has_signature and agent_signature not in body:
                            logger.debug(f"Skipping comment discussing different agent's work")
                            continue
                        
                        # Skip if this is our own agent output
                        if agent_signature in body:
                            logger.debug(f"Skipping our own agent output")
                            continue

                        logger.info(f"Found human feedback in issue comment from {author}")
                        return {
                            'author': author,
                            'body': body,
                            'created_at': created_at.isoformat(),
                            'comment_id': comment_id,
                            'parent_comment': None
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
                  comments(last: 100) {
                    nodes {
                      id
                      author { login }
                      body
                      createdAt
                      replies(last: 100) {
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

            # GitHub discussions use nested structure: top-level comments with nested replies
            logger.debug(f"Checking {len(comments)} comments for feedback (last_agent_time: {last_agent_time})")

            for comment in comments:
                if 'author' not in comment or 'createdAt' not in comment:
                    continue
                    
                author = comment['author']['login']
                created_at = date_parser.parse(comment['createdAt'])
                comment_id = comment.get('id')

                if created_at.tzinfo:
                    created_at = created_at.replace(tzinfo=None)

                # FIX #3: Skip if already processed
                if comment_id in state.processed_comment_ids:
                    logger.debug(f"Skipping already-processed comment {comment_id}")
                    continue

                # Check top-level comment
                if not is_bot_user(author) and created_at > last_agent_time:
                    # Check for agent signature (handles PAT users)
                    body = comment.get('body', '')
                    has_signature = "_Processed by the " in body
                    is_quote = False
                    
                    if has_signature:
                        # Check if signature line starts with > (quoted)
                        for line in body.split('\n'):
                            if "_Processed by the " in line and line.strip().startswith('>'):
                                is_quote = True
                                break
                    
                    if not has_signature or is_quote:
                        # Top-level comment (not a reply)
                        logger.info(f"Found human feedback in top-level comment from {author}")
                        return {
                            'author': author,
                            'body': body,
                            'created_at': created_at.isoformat(),
                            'comment_id': comment_id,
                            'parent_comment': None
                        }
                    else:
                        logger.debug(f"Skipping comment from {author} as it contains agent signature")

                # Check nested replies
                replies = comment.get('replies', {}).get('nodes', [])
                logger.debug(f"Checking {len(replies)} replies to comment {comment_id}")
                
                for reply in replies:
                    if 'author' not in reply or 'createdAt' not in reply:
                        continue
                    
                    reply_author = reply['author']['login']
                    reply_created_at = date_parser.parse(reply['createdAt'])
                    reply_id = reply.get('id')

                    if reply_created_at.tzinfo:
                        reply_created_at = reply_created_at.replace(tzinfo=None)

                    # FIX #3: Skip if already processed
                    if reply_id in state.processed_comment_ids:
                        logger.debug(f"Skipping already-processed reply {reply_id}")
                        continue

                    # Skip bot replies
                    if is_bot_user(reply_author):
                        continue
                        
                    # Only process replies after last agent output
                    if reply_created_at <= last_agent_time:
                        continue

                    # Check for agent signature
                    reply_body = reply.get('body', '')
                    has_signature = "_Processed by the " in reply_body
                    is_quote = False
                    
                    if has_signature:
                        for line in reply_body.split('\n'):
                            if "_Processed by the " in line and line.strip().startswith('>'):
                                is_quote = True
                                break
                    
                    if has_signature and not is_quote:
                        logger.debug(f"Skipping reply from {reply_author} as it contains agent signature")
                        continue

                    # Check if this reply is to our agent's comment
                    comment_body = comment.get('body', '')
                    agent_signature = f"_Processed by the {state.agent} agent_"
                    
                    if agent_signature not in comment_body:
                        logger.debug(f"Skipping reply to comment {comment_id} - not from {state.agent}")
                        continue
                    
                    # This is a reply to our agent's comment!
                    logger.info(f"Found human feedback in reply from {reply_author} to agent comment {comment_id}")
                    return {
                        'author': reply_author,
                        'body': reply_body,
                        'created_at': reply_created_at.isoformat(),
                        'comment_id': reply_id,
                        'parent_comment': {
                            'id': comment_id,
                            'body': comment_body,
                            'author': author
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
                body = comment.get('body', '')
                # Check for bot user OR agent signature (handles PAT users)
                is_agent_output = '_Processed by the ' in body and ' agent_' in body
                
                if is_bot_user(comment.get('author', {}).get('login', '')) or is_agent_output:
                    bot_comments.append({
                        'body': body,
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
                body = comment.get('body', '')
                is_agent_output = '_Processed by the ' in body and ' agent_' in body
                
                if is_bot_user(comment.get('author', {}).get('login', '')) or is_agent_output:
                    bot_comments.append({
                        'body': body,
                        'timestamp': comment.get('createdAt')
                    })

                # Check replies
                for reply in comment.get('replies', {}).get('nodes', []):
                    reply_body = reply.get('body', '')
                    is_reply_agent_output = '_Processed by the ' in reply_body and ' agent_' in reply_body
                    
                    if is_bot_user(reply.get('author', {}).get('login', '')) or is_reply_agent_output:
                        bot_comments.append({
                            'body': reply_body,
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
