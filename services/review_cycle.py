"""
Review Cycle Executor

Implements the synchronous maker-checker review loop with iteration tracking.
"""

import asyncio
import logging
import threading
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime
from config.manager import WorkflowColumn
from services.review_parser import ReviewParser, ReviewStatus
from services.github_integration import GitHubIntegration
from services.review_outcome_correlator import get_review_outcome_correlator
from monitoring.cycle_stack import push_frame, CycleFrame
from services.cancellation import CancellationError
from services.review_cycle_context import ReviewCycleContextWriter

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
        discussion_id: Optional[str] = None,
        pipeline_run_id: Optional[str] = None
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
        self.pipeline_run_id = pipeline_run_id
        self.cycle_stack: list = []  # parent cycle frames (JSON-serializable)
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
        self.last_approved_commit = None  # Git commit hash of last approved review (for scoped diffs)
        self.pre_maker_commit = None  # HEAD snapshot taken just before the maker agent runs each iteration
        self.context_dir: Optional[str] = None  # Container path to review cycle context directory
        self.context_writer: Optional[ReviewCycleContextWriter] = None  # In-memory handle (not persisted)

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
            'pipeline_run_id': self.pipeline_run_id,
            'cycle_stack': self.cycle_stack,
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
            'last_approved_commit': self.last_approved_commit,
            'pre_maker_commit': self.pre_maker_commit,
            'context_dir': self.context_dir,
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
            discussion_id=data.get('discussion_id'),
            pipeline_run_id=data.get('pipeline_run_id')
        )
        state.cycle_stack = data.get('cycle_stack', [])
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
        state.last_approved_commit = data.get('last_approved_commit')
        state.pre_maker_commit = data.get('pre_maker_commit')
        state.context_dir = data.get('context_dir')
        return state


class ReviewCycleExecutor:
    """Executes synchronous maker-checker review cycles"""

    def __init__(self):
        self.review_parser = ReviewParser()
        # Don't initialize GitHubIntegration here - create it per-call with proper repo context
        self.active_cycles = {}  # Track active review cycles by (project_name, issue_number) tuple
        self.state_dir = None  # Will be set per project
        self.outcome_correlator = get_review_outcome_correlator()
        # Per-issue locks that prevent concurrent _execute_review_loop / _continue_cycle_from_review
        # calls for the same issue (e.g. resume_active_cycles racing with the project monitor poll)
        self._cycle_execution_locks: Dict[Tuple, threading.Lock] = {}
        self._cycle_locks_lock = threading.Lock()  # guards the dict above

        # Initialize decision observability
        from monitoring.observability import get_observability_manager
        from monitoring.decision_events import DecisionEventEmitter
        self.obs = get_observability_manager()
        self.decision_events = DecisionEventEmitter(self.obs)

    @staticmethod
    def _cycle_key(project_name: str, issue_number: int) -> Tuple[str, int]:
        """Return the key used to index active_cycles and per-issue locks."""
        return (project_name, issue_number)

    def _get_cycle_lock(self, project_name: str, issue_number: int) -> threading.Lock:
        """Return the execution lock for a project/issue, creating it on first use."""
        key = self._cycle_key(project_name, issue_number)
        with self._cycle_locks_lock:
            if key not in self._cycle_execution_locks:
                self._cycle_execution_locks[key] = threading.Lock()
            return self._cycle_execution_locks[key]

    def _get_github_integration(self, cycle_state: ReviewCycleState) -> GitHubIntegration:
        """Get properly initialized GitHubIntegration for this cycle"""
        from config.manager import config_manager
        
        project_config = config_manager.get_project_config(cycle_state.project_name)
        repo_owner = project_config.github.get('org') if project_config and hasattr(project_config, 'github') else None
        
        if not repo_owner:
            raise ValueError(f"Cannot determine repo owner for project {cycle_state.project_name}")
            
        return GitHubIntegration(repo_owner=repo_owner, repo_name=cycle_state.repository)

    def _get_github_for_project(self, project_name: str, repository: str) -> GitHubIntegration:
        """Get properly initialized GitHubIntegration for a project/repository"""
        from config.manager import config_manager

        project_config = config_manager.get_project_config(project_name)
        repo_owner = project_config.github.get('org') if project_config and hasattr(project_config, 'github') else None

        if not repo_owner:
            raise ValueError(f"Cannot determine repo owner for project {project_name}")

        return GitHubIntegration(repo_owner=repo_owner, repo_name=repository)

    def _ensure_context_writer(
        self,
        cycle_state: ReviewCycleState,
        issue_data: Optional[Dict[str, Any]] = None,
    ) -> Optional[ReviewCycleContextWriter]:
        """
        Return the context writer for this cycle, restoring it after a restart if needed.

        Recovery cases (in order of likelihood):
          1. writer already in memory          → return as-is
          2. context_dir persisted + dir exists → re-attach with from_existing()
          3. context_dir persisted + dir missing → re-create and repopulate from state
          4. context_dir absent (legacy state)  → return None (fall back to embedded prompts)
        """
        if cycle_state.context_writer is not None:
            return cycle_state.context_writer

        if not cycle_state.context_dir:
            # Legacy state predating this feature — embedded-prompt fallback
            return None

        writer = ReviewCycleContextWriter.from_existing(cycle_state.context_dir)
        if not writer.exists():
            logger.warning(
                f"Review cycle context dir missing for issue #{cycle_state.issue_number} "
                f"({cycle_state.context_dir}) — repopulating from state"
            )
            if issue_data:
                writer.repopulate_from_state(
                    issue=issue_data,
                    maker_outputs=cycle_state.maker_outputs,
                    review_outputs=cycle_state.review_outputs,
                )
            else:
                logger.warning(
                    f"Cannot repopulate context dir without issue_data for "
                    f"issue #{cycle_state.issue_number}; falling back to embedded prompts"
                )
                return None

        cycle_state.context_writer = writer
        return writer

    async def _analyze_review_cycle_outcomes(self, cycle_state: ReviewCycleState):
        """
        Analyze completed review cycle for learning.

        Extracts review outcomes to feed into the pattern detection and learning system.
        This runs asynchronously in the background to not block cycle completion.
        """
        try:
            logger.info(f"Analyzing review cycle outcomes for issue #{cycle_state.issue_number}")
            outcomes = await self.outcome_correlator.analyze_review_cycle_outcome(cycle_state)
            logger.info(f"Extracted {len(outcomes)} review outcomes for learning")
        except Exception as e:
            logger.warning(f"Failed to analyze review outcomes (non-critical): {e}")
            # Don't fail the cycle if learning fails

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

    def _load_active_cycles(self, project_name: str) -> List[ReviewCycleState]:
        """Load active cycles from disk for a project"""
        import yaml
        import os

        state_file = self._get_state_file_path(project_name)

        if not os.path.exists(state_file):
            logger.info(f"No active cycles state file found for {project_name}")
            return []

        with open(state_file, 'r') as f:
            data = yaml.safe_load(f) or {}

        cycles = []
        for cycle_data in data.get('active_cycles', []):
            cycle_state = ReviewCycleState.from_dict(cycle_data)
            cycles.append(cycle_state)

        logger.info(f"Loaded {len(cycles)} active cycles for {project_name}")
        return cycles

    def _remove_cycle_state(self, cycle_state: ReviewCycleState):
        """Remove a completed cycle from active state and clean up its context directory."""
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

        # Clean up the file-based context directory (best-effort), deferred by 15 minutes.
        # The deferral outlasts the circuit breaker recovery window (10 min) so that maker
        # agents scheduled during recovery still find the context dir on disk.
        # issue_data is not available here; if the dir was already lost (volume remount),
        # _ensure_context_writer returns None and cleanup is a silent no-op — which is fine,
        # since there is nothing to clean up in that case.
        _CONTEXT_CLEANUP_DELAY_SECS = 900  # 15 min > circuit breaker recovery_timeout (10 min)
        writer = self._ensure_context_writer(cycle_state)
        if writer:
            import threading
            t = threading.Timer(_CONTEXT_CLEANUP_DELAY_SECS, writer.cleanup)
            t.daemon = True
            t.start()
            logger.info(
                f"Scheduled context dir cleanup for issue {cycle_state.issue_number} "
                f"in {_CONTEXT_CLEANUP_DELAY_SECS}s"
            )

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
        for cycle_state in active_cycles:
            key = self._cycle_key(cycle_state.project_name, cycle_state.issue_number)
            self.active_cycles[key] = cycle_state

        # Resume each cycle based on its state
        for cycle_state in active_cycles:
            logger.info(
                f"Resuming cycle for issue #{cycle_state.issue_number} "
                f"(status: {cycle_state.status}, iteration: {cycle_state.current_iteration}/{cycle_state.max_iterations})"
            )

            try:
                # FIRST: Validate that the issue is still in a column that uses review cycles
                # This must run BEFORE max iterations check to remove stale cycles
                # If the issue moved to a different column (e.g., from Code Review to Testing),
                # the old review cycle state should be removed
                from services.project_monitor import ProjectMonitor
                from config.manager import config_manager

                temp_monitor = ProjectMonitor(None, config_manager)
                current_column = temp_monitor.get_issue_column_sync(
                    cycle_state.project_name,
                    cycle_state.board_name,
                    cycle_state.issue_number
                )

                if current_column:
                    # Get workflow template to check if current column uses review cycles
                    project_config = config_manager.get_project_config(cycle_state.project_name)
                    pipeline = next((p for p in project_config.pipelines if p.board_name == cycle_state.board_name), None)

                    if pipeline:
                        workflow_template = config_manager.get_workflow_template(pipeline.workflow)
                        current_column_config = next((c for c in workflow_template.columns if c.name == current_column), None)

                        # Check if current column uses review cycles (has both agent and maker_agent)
                        uses_review_cycle = (
                            current_column_config and
                            current_column_config.agent and
                            current_column_config.maker_agent and
                            current_column_config.agent != 'null' and
                            current_column_config.maker_agent != 'null'
                        )

                        if not uses_review_cycle:
                            logger.warning(
                                f"Issue #{cycle_state.issue_number} is in column '{current_column}' which doesn't use review cycles. "
                                f"Removing stale review cycle state (was for {cycle_state.reviewer_agent}/{cycle_state.maker_agent})."
                            )
                            self._remove_cycle_state(cycle_state)
                            ck = self._cycle_key(cycle_state.project_name, cycle_state.issue_number)
                            if ck in self.active_cycles:
                                del self.active_cycles[ck]
                            continue

                        # Check if the agents match (column config may have changed)
                        if (current_column_config.agent != cycle_state.reviewer_agent or
                            current_column_config.maker_agent != cycle_state.maker_agent):
                            logger.warning(
                                f"Issue #{cycle_state.issue_number} review cycle agents don't match current column config. "
                                f"Removing stale state (was: {cycle_state.reviewer_agent}/{cycle_state.maker_agent}, "
                                f"now: {current_column_config.agent}/{current_column_config.maker_agent})."
                            )
                            self._remove_cycle_state(cycle_state)
                            ck = self._cycle_key(cycle_state.project_name, cycle_state.issue_number)
                            if ck in self.active_cycles:
                                del self.active_cycles[ck]
                            continue
                else:
                    # Issue not found on board, remove stale state
                    logger.warning(
                        f"Issue #{cycle_state.issue_number} not found on board '{cycle_state.board_name}'. "
                        f"Removing stale review cycle state."
                    )
                    self._remove_cycle_state(cycle_state)
                    ck = self._cycle_key(cycle_state.project_name, cycle_state.issue_number)
                    if ck in self.active_cycles:
                        del self.active_cycles[ck]
                    continue

            except Exception as e:
                logger.error(f"Failed to validate review cycle column for issue #{cycle_state.issue_number}: {e}", exc_info=True)
                # Continue with other validation checks

            # Validate that the cycle hasn't exceeded max iterations (corrupted state check)
            if cycle_state.current_iteration >= cycle_state.max_iterations:
                logger.warning(
                    f"Cycle for issue #{cycle_state.issue_number} has exceeded max iterations "
                    f"({cycle_state.current_iteration}/{cycle_state.max_iterations}). "
                    f"Removing corrupted cycle state and ending pipeline run."
                )
                
                # End the pipeline run if it exists
                if cycle_state.pipeline_run_id:
                    try:
                        from services.pipeline_run import get_pipeline_run_manager
                        pipeline_run_manager = get_pipeline_run_manager()
                        pipeline_run_manager.end_pipeline_run(
                            cycle_state.project_name,
                            cycle_state.issue_number,
                            reason="review_cycle_exceeded_max_iterations_on_resume",
                            retain_lock=True
                        )
                        logger.info(f"Ended pipeline run {cycle_state.pipeline_run_id} due to max iterations exceeded on resume")
                    except Exception as e:
                        logger.error(f"Failed to end pipeline run {cycle_state.pipeline_run_id}: {e}", exc_info=True)
                
                # Remove the corrupted state
                self._remove_cycle_state(cycle_state)
                ck = self._cycle_key(cycle_state.project_name, cycle_state.issue_number)
                if ck in self.active_cycles:
                    del self.active_cycles[ck]
                continue

            try:
                # If this cycle uses discussions, verify the discussion still exists
                if cycle_state.workspace_type == 'discussions' and cycle_state.discussion_id:
                    from services.github_discussions import GitHubDiscussions
                    discussions = GitHubDiscussions()
                    discussion = discussions.get_discussion(cycle_state.discussion_id)

                    if not discussion:
                        logger.warning(
                            f"Discussion {cycle_state.discussion_id} for issue #{cycle_state.issue_number} "
                            f"no longer exists. Removing cycle from active state."
                        )
                        self._remove_cycle_state(cycle_state)
                        continue

            except Exception as e:
                logger.error(f"Failed to resume cycle for issue #{cycle_state.issue_number}: {e}")
                continue

            try:
                if cycle_state.status == 'initialized':
                    # Cycle is initialized - check if there's pending work to continue

                    # Check if we have maker output but no corresponding review (or just starting)
                    # For iteration 0, we always have maker output (from previous stage), and we want to start iteration 1
                    is_start = cycle_state.current_iteration == 0 and len(cycle_state.maker_outputs) > 0
                    
                    # Check if we have completed maker work for the current iteration that needs review
                    # (This happens if maker finished, we crashed, resumed, and _continue_cycle_from_maker set status to initialized)
                    has_maker_output = cycle_state.maker_outputs and any(m['iteration'] == cycle_state.current_iteration for m in cycle_state.maker_outputs)
                    
                    if is_start or (has_maker_output and cycle_state.current_iteration < cycle_state.max_iterations):
                        logger.info(f"Cycle in initialized state with pending maker output - starting/resuming review loop")

                        # CRITICAL: Check if agent is already running from recovered container
                        # This prevents launching duplicate agents after orchestrator restart
                        from services.work_execution_state import work_execution_tracker

                        if work_execution_tracker.has_active_execution(
                            cycle_state.project_name,
                            cycle_state.issue_number
                        ):
                            logger.info(
                                f"Skipping review cycle resume for issue #{cycle_state.issue_number}: "
                                f"agent already active (likely from recovered container). "
                                f"Will wait for active execution to complete."
                            )
                            continue

                        # Guard against concurrent execution from the project monitor poll
                        lock = self._get_cycle_lock(cycle_state.project_name, cycle_state.issue_number)
                        if not lock.acquire(blocking=False):
                            logger.info(
                                f"Review cycle execution already in progress for issue "
                                f"#{cycle_state.issue_number}, skipping resume"
                            )
                            continue

                        # Fetch necessary context to run the loop
                        try:
                            from config.manager import config_manager
                            workflow_template = config_manager.get_project_workflow(cycle_state.project_name, cycle_state.board_name)
                            column = next((c for c in workflow_template.columns if c.agent == cycle_state.reviewer_agent), None)

                            if column:
                                github = self._get_github_integration(cycle_state)
                                issue_data = await github.get_issue_details(cycle_state.issue_number, cycle_state.repository)

                                # Execute the review loop and capture result for column progression
                                # Note: _execute_review_loop will increment iteration at the start
                                result = await self._execute_review_loop(
                                    cycle_state,
                                    column,
                                    issue_data,
                                    org
                                )

                                # Move the issue to the next column if the cycle completed with approval
                                if result:
                                    _, next_col_name = result
                                    if next_col_name and next_col_name != column.name:
                                        await self._advance_issue_after_review_approval(
                                            cycle_state, column.name, next_col_name
                                        )
                            else:
                                logger.error(f"Could not find column config for reviewer {cycle_state.reviewer_agent}")
                        except CancellationError as e:
                            logger.info(f"Review cycle resume skipped (cancelled): {e}")
                        except Exception as e:
                            logger.error(f"Failed to resume review loop: {e}", exc_info=True)
                        finally:
                            lock.release()

                        continue

                    if cycle_state.review_outputs and len(cycle_state.review_outputs) > 0:
                        # Reviewer has completed at least one iteration
                        logger.info(f"Cycle in initialized state with pending review - continuing")
                        lock = self._get_cycle_lock(cycle_state.project_name, cycle_state.issue_number)
                        if not lock.acquire(blocking=False):
                            logger.info(
                                f"Review cycle execution already in progress for issue "
                                f"#{cycle_state.issue_number}, skipping resume"
                            )
                            continue
                        try:
                            await self._continue_cycle_from_review(cycle_state, org)
                        finally:
                            lock.release()
                        continue
                    else:
                        # No work done yet, needs to be triggered by column movement
                        logger.info(f"Cycle in initialized state with no outputs - waiting for trigger")
                        continue

                elif cycle_state.status == 'awaiting_human_feedback':
                    # Cycle is awaiting human feedback
                    logger.info(
                        f"Cycle for issue #{cycle_state.issue_number} is awaiting human feedback. "
                        f"Project monitor will check periodically and resume when feedback is detected."
                    )
                    # State is already saved, project_monitor.check_escalated_review_cycles() will handle this
                    continue

                elif cycle_state.status in ['maker_working', 'reviewer_working']:
                    # Agents were working when restart happened
                    # Check if they actually completed (state wasn't updated before restart)
                    logger.warning(
                        f"Cycle was in {cycle_state.status} state during restart. "
                        f"Checking if work completed..."
                    )

                    # Check if agent is already running (from recovered container)
                    from services.work_execution_state import work_execution_tracker
                    if work_execution_tracker.has_active_execution(
                        cycle_state.project_name, cycle_state.issue_number
                    ):
                        logger.info(
                            f"Skipping resume for issue #{cycle_state.issue_number}: "
                            f"agent already active (likely from recovered container)"
                        )
                        continue

                    # If reviewer was working, check if there's a review output for this iteration
                    if (cycle_state.status == 'reviewer_working' and
                        cycle_state.review_outputs and
                        any(r['iteration'] == cycle_state.current_iteration for r in cycle_state.review_outputs)):
                        logger.info(f"Reviewer completed iteration {cycle_state.current_iteration}, resuming cycle")
                        cycle_state.status = 'initialized'
                        self._save_cycle_state(cycle_state)

                        # Resume the cycle from the review point
                        lock = self._get_cycle_lock(cycle_state.project_name, cycle_state.issue_number)
                        if not lock.acquire(blocking=False):
                            logger.info(
                                f"Review cycle execution already in progress for issue "
                                f"#{cycle_state.issue_number}, skipping resume"
                            )
                            continue
                        try:
                            await self._continue_cycle_from_review(cycle_state, org)
                        finally:
                            lock.release()
                        continue

                    # If maker was working, check if there's a maker output for this iteration
                    if (cycle_state.status == 'maker_working' and
                        cycle_state.maker_outputs and
                        any(m['iteration'] == cycle_state.current_iteration for m in cycle_state.maker_outputs)):
                        logger.info(f"Maker completed iteration {cycle_state.current_iteration}, resuming cycle")
                        cycle_state.status = 'initialized'
                        self._save_cycle_state(cycle_state)

                        # Resume the cycle from the maker point
                        lock = self._get_cycle_lock(cycle_state.project_name, cycle_state.issue_number)
                        if not lock.acquire(blocking=False):
                            logger.info(
                                f"Review cycle execution already in progress for issue "
                                f"#{cycle_state.issue_number}, skipping resume"
                            )
                            continue
                        try:
                            await self._continue_cycle_from_maker(cycle_state, org)
                        finally:
                            lock.release()
                        continue

                    logger.warning("Agent didn't complete before restart, manual intervention may be required.")

                elif cycle_state.status == 'completed':
                    # Shouldn't happen, but clean up if it does
                    logger.info(f"Cycle marked as completed, removing from active state")
                    self._remove_cycle_state(cycle_state)

                else:
                    logger.warning(f"Unknown cycle status: {cycle_state.status}")

            except Exception as e:
                logger.error(f"Failed to resume cycle for issue #{cycle_state.issue_number}: {e}", exc_info=True)

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
        discussion_id: Optional[str] = None,
        pipeline_run_id: Optional[str] = None
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
        key = self._cycle_key(project_name, issue_number)
        if key in self.active_cycles:
            existing_cycle = self.active_cycles[key]
            # Check if the existing cycle is for the same agents
            if (existing_cycle.maker_agent == column.maker_agent and
                existing_cycle.reviewer_agent == column.agent):
                logger.info(
                    f"Review cycle already active for issue #{issue_number}, "
                    f"using existing cycle (status: {existing_cycle.status}, "
                    f"iteration: {existing_cycle.current_iteration}/{existing_cycle.max_iterations})"
                )
                
                # Check if the cycle is already beyond max iterations (corrupted state)
                if existing_cycle.current_iteration >= existing_cycle.max_iterations:
                    logger.warning(
                        f"Review cycle for issue #{issue_number} is already at or beyond max iterations "
                        f"({existing_cycle.current_iteration}/{existing_cycle.max_iterations}). "
                        f"This indicates a corrupted state. Cleaning up and escalating."
                    )
                    
                    # Remove corrupted cycle
                    self._remove_cycle_state(existing_cycle)
                    del self.active_cycles[key]

                    # End the pipeline run if it exists
                    if existing_cycle.pipeline_run_id:
                        try:
                            from services.pipeline_run import get_pipeline_run_manager
                            pipeline_run_manager = get_pipeline_run_manager()
                            pipeline_run_manager.end_pipeline_run(
                                project_name,
                                issue_number,
                                reason="review_cycle_exceeded_max_iterations",
                                retain_lock=True
                            )
                            logger.info(f"Ended pipeline run {existing_cycle.pipeline_run_id} due to max iterations exceeded")
                        except Exception as e:
                            logger.error(f"Failed to end pipeline run {existing_cycle.pipeline_run_id}: {e}", exc_info=True)
                    
                    # Post escalation message
                    escalation_message = (
                        f"⚠️ **Review Cycle Exceeded Max Iterations**\n\n"
                        f"The review cycle has already reached or exceeded the maximum iterations "
                        f"({existing_cycle.current_iteration}/{existing_cycle.max_iterations}). "
                        f"Manual review is required.\n\n"
                        f"**Maker Agent:** {existing_cycle.maker_agent}\n"
                        f"**Reviewer Agent:** {existing_cycle.reviewer_agent}\n\n"
                        f"Please review the work and provide feedback to continue."
                    )
                    
                    if workspace_type == 'discussions' and discussion_id:
                        from services.github_discussions import GitHubDiscussions
                        discussions = GitHubDiscussions()
                        discussions.add_discussion_comment(discussion_id, escalation_message,
                                                          pipeline_run_id=existing_cycle.pipeline_run_id)
                    else:
                        github = self._get_github_for_project(project_name, repository)
                        await github.post_issue_comment(
                            issue_number,
                            escalation_message,
                            repository,
                            pipeline_run_id=existing_cycle.pipeline_run_id,
                        )

                    # Return current column, cycle not complete
                    return column.name, False
                
                # Reuse existing cycle - do NOT append maker output or reset state
                cycle_state = existing_cycle

                # Guard against concurrent execution (e.g. resume_active_cycles racing with the
                # project monitor poll).  Non-blocking: if another thread already holds the lock
                # for this issue there is nothing new to dispatch.
                lock = self._get_cycle_lock(project_name, issue_number)
                if not lock.acquire(blocking=False):
                    logger.info(
                        f"Review cycle execution already in progress for issue #{issue_number}, "
                        f"skipping concurrent call"
                    )
                    return column.name, False

                # Continue directly to executing the review loop
                # The existing state already has the maker outputs and iteration tracking
                try:
                    # Execute the review loop
                    final_status, next_column = await self._execute_review_loop(
                        cycle_state,
                        column,
                        issue_data,
                        org
                    )

                    # Clean up only if not awaiting human feedback.
                    # Escalated cycles must remain in active_cycles so that
                    # check_escalated_review_cycles() can detect human feedback.
                    if cycle_state.status != 'awaiting_human_feedback':
                        del self.active_cycles[key]

                    return next_column, True

                except Exception as e:
                    logger.error(f"Review cycle failed for issue #{issue_number}: {e}")

                    # EMIT ERROR EVENT for UI visibility
                    if key in self.active_cycles:
                        existing_cycle = self.active_cycles[key]
                        self._emit_review_cycle_error(
                            cycle_state=existing_cycle,
                            error=e,
                            error_context="execution",
                            recovery_action="Review cycle terminated, GitHub comment posted"
                        )

                    # Clean up on error (both in-memory and on-disk)
                    if key in self.active_cycles:
                        self._remove_cycle_state(self.active_cycles[key])
                        del self.active_cycles[key]

                    # End the pipeline run if it exists
                    if hasattr(existing_cycle, 'pipeline_run_id') and existing_cycle.pipeline_run_id:
                        try:
                            from services.pipeline_run import get_pipeline_run_manager
                            pipeline_run_manager = get_pipeline_run_manager()
                            pipeline_run_manager.end_pipeline_run(
                                project_name,
                                issue_number,
                                reason=f"review_cycle_failed: {str(e)[:100]}",
                                retain_lock=True,
                                outcome="failed"
                            )
                            logger.info(f"Ended pipeline run {existing_cycle.pipeline_run_id} due to review cycle failure")
                        except Exception as prm_error:
                            logger.error(f"Failed to end pipeline run {existing_cycle.pipeline_run_id}: {prm_error}", exc_info=True)

                    # Post error comment (workspace-aware)
                    error_message = f"⚠️ **Review Cycle Error**\n\nThe automated review cycle encountered an error:\n```\n{str(e)}\n```\n\nPlease review manually."

                    if workspace_type == 'discussions' and discussion_id:
                        from services.github_discussions import GitHubDiscussions
                        discussions = GitHubDiscussions()
                        discussions.add_discussion_comment(discussion_id, error_message,
                                                          pipeline_run_id=existing_cycle.pipeline_run_id)
                    else:
                        github = self._get_github_for_project(project_name, repository)
                        await github.post_issue_comment(
                            issue_number,
                            error_message,
                            repository,
                            pipeline_run_id=existing_cycle.pipeline_run_id,
                        )

                    raise
                finally:
                    lock.release()
            else:
                # Different review column - remove old cycle and create new one
                logger.info(
                    f"Removing old review cycle for issue #{issue_number} "
                    f"(was: {existing_cycle.reviewer_agent}/{existing_cycle.maker_agent}, "
                    f"now: {column.agent}/{column.maker_agent})"
                )
                self._remove_cycle_state(existing_cycle)
                del self.active_cycles[key]
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
                    discussion_id=discussion_id,
                    pipeline_run_id=pipeline_run_id
                )
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
                discussion_id=discussion_id,
                pipeline_run_id=pipeline_run_id
            )

        # Set up file-based context directory for this new cycle
        try:
            writer = ReviewCycleContextWriter.setup(issue_number, pipeline_run_id or '')
            cycle_state.context_writer = writer
            cycle_state.context_dir = writer.context_dir
            writer.write_initial_request(
                issue_data.get('title', ''),
                issue_data.get('body', ''),
            )
        except Exception as ctx_err:
            logger.warning(
                f"Failed to set up review cycle context dir for issue #{issue_number}: {ctx_err}. "
                f"Falling back to embedded-prompt context."
            )

        # Store initial maker output (only for NEW cycles)
        cycle_state.maker_outputs.append({
            'iteration': 0,
            'output': previous_stage_output,
            'timestamp': datetime.now().isoformat()
        })
        if cycle_state.context_writer:
            try:
                cycle_state.context_writer.write_maker_output(previous_stage_output, 1)
            except Exception as e:
                logger.warning(f"Failed to write initial maker output to context dir: {e}")

        # Track active cycle
        self.active_cycles[key] = cycle_state
        # Save initial state to disk
        cycle_state.status = 'maker_working'
        self._save_cycle_state(cycle_state)
        
        # EMIT DECISION EVENT: Review cycle started
        self.decision_events.emit_review_cycle_decision(
            issue_number=issue_number,
            project=project_name,
            board=board_name,
            cycle_iteration=0,
            decision_type='start',
            maker_agent=column.maker_agent,
            reviewer_agent=column.agent,
            reason=f"Starting review cycle with maker '{column.maker_agent}' and reviewer '{column.agent}' (max iterations: {column.max_iterations})",
            additional_data={
                'max_iterations': column.max_iterations,
                'workspace_type': workspace_type,
                'discussion_id': discussion_id
            },
            pipeline_run_id=cycle_state.pipeline_run_id
        )

        # Guard against concurrent execution (e.g. resume_active_cycles racing with the project
        # monitor poll).  Non-blocking: if another thread is already executing the loop for this
        # issue (having just added the cycle to active_cycles above), skip rather than duplicate.
        lock = self._get_cycle_lock(project_name, issue_number)
        if not lock.acquire(blocking=False):
            logger.info(
                f"Review cycle execution already in progress for issue #{issue_number}, "
                f"skipping concurrent call"
            )
            return column.name, False

        try:
            # Execute the review loop
            final_status, next_column = await self._execute_review_loop(
                cycle_state,
                column,
                issue_data,
                org
            )

            # Clean up only if not awaiting human feedback.
            # Escalated cycles must remain in active_cycles so that
            # check_escalated_review_cycles() can detect human feedback.
            if cycle_state.status != 'awaiting_human_feedback':
                del self.active_cycles[key]

            return next_column, True

        except Exception as e:
            # CancellationError: deliberate stop — clean up silently without posting error comment
            from services.cancellation import CancellationError
            if isinstance(e, CancellationError):
                logger.info(f"Review cycle cancelled for issue #{issue_number}: {e}")
                if key in self.active_cycles:
                    self._remove_cycle_state(self.active_cycles[key])
                    del self.active_cycles[key]
                raise

            logger.error(f"Review cycle failed for issue #{issue_number}: {e}")

            # EMIT ERROR EVENT for UI visibility
            if key in self.active_cycles:
                cycle = self.active_cycles[key]
                self._emit_review_cycle_error(
                    cycle_state=cycle,
                    error=e,
                    error_context="startup",
                    recovery_action="Review cycle terminated, GitHub error comment posted"
                )

            # Clean up on error (both in-memory and on-disk)
            if key in self.active_cycles:
                self._remove_cycle_state(self.active_cycles[key])
                del self.active_cycles[key]

            # Post error comment (workspace-aware)
            error_message = f"⚠️ **Review Cycle Error**\n\nThe automated review cycle encountered an error:\n```\n{str(e)}\n```\n\nPlease review manually."

            if workspace_type == 'discussions' and discussion_id:
                from services.github_discussions import GitHubDiscussions
                discussions = GitHubDiscussions()
                discussions.add_discussion_comment(discussion_id, error_message,
                                                  pipeline_run_id=pipeline_run_id)
            else:
                github = self._get_github_for_project(project_name, repository)
                await github.post_issue_comment(
                    issue_number,
                    error_message,
                    repository,
                    pipeline_run_id=pipeline_run_id,
                )

            raise
        finally:
            lock.release()

    async def _continue_cycle_from_review(self, cycle_state: ReviewCycleState, org: str):
        """Continue a stuck cycle that completed a review but state wasn't updated"""
        from config.manager import config_manager

        try:
            # Get the review output for this iteration
            review_output = next(
                (r['output'] for r in cycle_state.review_outputs if r['iteration'] == cycle_state.current_iteration),
                None
            )

            if not review_output:
                logger.error(f"No review output found for iteration {cycle_state.current_iteration}")
                return

            # Parse the review to determine next action
            review_result = self.review_parser.parse_review(review_output)

            # Get necessary context
            project_config = config_manager.get_project_config(cycle_state.project_name)
            workflow_template = config_manager.get_project_workflow(cycle_state.project_name, cycle_state.board_name)
            column = next((c for c in workflow_template.columns if c.agent == cycle_state.reviewer_agent), None)

            if not column:
                logger.error(f"No column found for reviewer agent {cycle_state.reviewer_agent} in {cycle_state.board_name}")
                return

            github = self._get_github_integration(cycle_state)
            issue_data = await github.get_issue_details(cycle_state.issue_number, cycle_state.repository)

            # Restore context writer from persisted context_dir (handles restarts)
            self._ensure_context_writer(cycle_state, issue_data)

            # Safety net: APPROVED with high-severity findings is a reviewer protocol violation.
            # The reviewer prompt explicitly forbids this combination; if it occurs anyway,
            # override to CHANGES_REQUESTED so the findings are not silently dropped.
            if (review_result.status == ReviewStatus.APPROVED
                    and (review_result.high_severity_count > 0 or review_result.blocking_count > 0)):
                logger.warning(
                    f"Reviewer returned APPROVED with {review_result.blocking_count} blocking and "
                    f"{review_result.high_severity_count} high-severity finding(s) — "
                    f"overriding to CHANGES_REQUESTED so findings are not lost"
                )
                review_result.status = ReviewStatus.CHANGES_REQUESTED

            # Continue with appropriate action based on review status
            if review_result.status == ReviewStatus.APPROVED:
                logger.info(f"Review was approved, completing cycle")

                # Store current commit hash for future scoped reviews (issues workspace)
                if cycle_state.workspace_type == 'issues':
                    from services.project_workspace import workspace_manager
                    project_dir = workspace_manager.get_project_dir(cycle_state.project_name)
                    current_commit = self._get_git_commit_hash(str(project_dir))
                    if current_commit:
                        cycle_state.last_approved_commit = current_commit
                        logger.info(f"Stored approved commit {current_commit[:8]} for future scoped reviews")

                cycle_state.status = 'completed'
                self._save_cycle_state(cycle_state)

                # Analyze review cycle outcomes for learning (async, non-blocking)
                await self._analyze_review_cycle_outcomes(cycle_state)

                self._remove_cycle_state(cycle_state)
                ck = self._cycle_key(cycle_state.project_name, cycle_state.issue_number)
                del self.active_cycles[ck]

                # NOTE: Do NOT update PR status here!
                # PR status is managed by feature_branch_manager.finalize_workspace()
                # which checks if ALL sub-issues are complete before marking PR ready.
                # The review cycle only approves individual code changes, not the entire PR.

                # Advance the issue to the next column (recovery path — project_monitor won't do this)
                if column.auto_advance_on_approval:
                    next_col_name = self._get_next_column_name(column, cycle_state.project_name, cycle_state.board_name)
                    if next_col_name and next_col_name != column.name:
                        await self._advance_issue_after_review_approval(
                            cycle_state, column.name, next_col_name
                        )

            elif review_result.status == ReviewStatus.CHANGES_REQUESTED:
                logger.info(f"Changes requested, executing maker for revision")

                # Get fresh context for maker (for discussions workspace)
                fresh_context = ""
                if cycle_state.workspace_type == 'discussions' and cycle_state.discussion_id:
                    fresh_context = await self._get_fresh_discussion_context(
                        cycle_state,
                        org,
                        cycle_state.current_iteration
                    )

                # Create maker revision context
                maker_task_context = self._create_maker_revision_task_context(
                    cycle_state,
                    column,
                    issue_data,
                    review_output,  # review_feedback
                    cycle_state.current_iteration,  # iteration
                    full_discussion_context=fresh_context
                )

                # Ensure we're on the correct branch (for issues workspace)
                if cycle_state.workspace_type == 'issues':
                    from services.project_workspace import workspace_manager
                    from services.git_workflow_manager import git_workflow_manager

                    branch_info = git_workflow_manager.get_branch_info(
                        cycle_state.project_name,
                        cycle_state.issue_number
                    )

                    if branch_info:
                        # Checkout existing branch
                        from services.git_workflow_manager import git_workflow_manager
                        project_dir = workspace_manager.get_project_dir(cycle_state.project_name)
                        await git_workflow_manager.checkout_branch(str(project_dir), branch_info.branch_name)
                        logger.info(f"Switched to branch {branch_info.branch_name} for maker revision")

                # Execute maker agent
                cycle_state.status = 'maker_working'
                if cycle_state.workspace_type == 'issues':
                    from services.project_workspace import workspace_manager
                    _pd = workspace_manager.get_project_dir(cycle_state.project_name)
                    commit_hash = self._get_git_commit_hash(str(_pd))
                    if commit_hash:
                        cycle_state.pre_maker_commit = commit_hash
                self._save_cycle_state(cycle_state)

                await self._execute_agent_directly(
                    cycle_state.maker_agent,
                    maker_task_context,
                    cycle_state.project_name
                )

                # Auto-commit changes (for issues workspace)
                if cycle_state.workspace_type == 'issues':
                    from services.auto_commit import auto_commit_service
                    from config.manager import config_manager

                    agent_config = config_manager.get_project_agent_config(
                        cycle_state.project_name,
                        cycle_state.maker_agent
                    )
                    makes_code_changes = getattr(agent_config, 'makes_code_changes', False)

                    if makes_code_changes:
                        await auto_commit_service.commit_agent_changes(
                            project=cycle_state.project_name,
                            agent=cycle_state.maker_agent,
                            task_id=f"review_cycle_iter_{cycle_state.current_iteration}",
                            issue_number=cycle_state.issue_number,
                            custom_message=f"Address code review feedback (iteration {cycle_state.current_iteration})\n\nIssue #{cycle_state.issue_number}"
                        )

                # Get maker output
                maker_output = await self._get_latest_agent_comment(
                    cycle_state.issue_number,
                    cycle_state.repository,
                    cycle_state.maker_agent,
                    cycle_state.workspace_type,
                    cycle_state.discussion_id,
                    org
                )

                # Store maker output
                cycle_state.maker_outputs.append({
                    'iteration': cycle_state.current_iteration,
                    'output': maker_output,
                    'timestamp': datetime.now().isoformat()
                })
                _writer = self._ensure_context_writer(cycle_state, issue_data)
                if _writer:
                    try:
                        _writer.write_maker_output(maker_output, cycle_state.current_iteration + 1)
                    except Exception as e:
                        logger.warning(f"Failed to write recovered maker output to context dir: {e}")
                # Do not increment iteration here - _execute_review_loop increments at the top
                # of each iteration, so current_iteration stays at N and the loop will run N+1.
                cycle_state.status = 'initialized'
                self._save_cycle_state(cycle_state)

                logger.info(
                    f"Maker completed revision (recovery path), continuing directly to "
                    f"iteration {cycle_state.current_iteration + 1}"
                )

                # Continue directly rather than waiting for resume_active_cycles.
                # The lock is already held by the caller (process_recovered_reviewer_output)
                # and _execute_review_loop does not acquire it, so this is safe.
                if cycle_state.current_iteration < cycle_state.max_iterations:
                    result = await self._execute_review_loop(cycle_state, column, issue_data, org)
                    ck = self._cycle_key(cycle_state.project_name, cycle_state.issue_number)
                    if ck in self.active_cycles:
                        del self.active_cycles[ck]
                    if result:
                        final_status, next_col_name = result
                        if final_status == ReviewStatus.APPROVED and next_col_name and next_col_name != column.name:
                            await self._advance_issue_after_review_approval(
                                cycle_state, column.name, next_col_name
                            )
                else:
                    logger.warning(
                        f"Max iterations ({cycle_state.max_iterations}) reached after recovery-path "
                        f"maker run for issue #{cycle_state.issue_number}. Escalating."
                    )
                    await self._escalate_max_iterations(cycle_state, self.review_parser.parse_review(review_output))
                    cycle_state.status = 'awaiting_human_feedback'
                    cycle_state.escalation_time = datetime.now().isoformat()
                    self._save_cycle_state(cycle_state)
                    try:
                        from services.pipeline_run import get_pipeline_run_manager
                        get_pipeline_run_manager().update_run_status(
                            cycle_state.project_name, cycle_state.issue_number, "feedback_listening"
                        )
                    except Exception as _e:
                        logger.warning(f"Failed to set feedback_listening status after max_iterations escalation: {_e}")

            elif review_result.status == ReviewStatus.BLOCKED:
                logger.info(f"Review blocked, escalating")
                await self._escalate_blocked(cycle_state, review_result)
                cycle_state.status = 'awaiting_human_feedback'
                cycle_state.escalation_time = datetime.now().isoformat()
                self._save_cycle_state(cycle_state)
                try:
                    from services.pipeline_run import get_pipeline_run_manager
                    get_pipeline_run_manager().update_run_status(
                        cycle_state.project_name, cycle_state.issue_number, "feedback_listening"
                    )
                except Exception as _e:
                    logger.warning(f"Failed to set feedback_listening status after blocked escalation in recovery path: {_e}")

        except Exception as e:
            if isinstance(e, CancellationError):
                raise
            logger.error(f"Failed to continue cycle from review: {e}", exc_info=True)

    async def _continue_cycle_from_maker(self, cycle_state: ReviewCycleState, org: str):
        """Continue a stuck cycle that completed maker work but state wasn't updated"""
        try:
            logger.info(f"Continuing cycle from completed maker work")
            # Maker completed, now run reviewer
            # For now, just mark as ready and let next resume iteration handle it
            cycle_state.status = 'initialized'
            self._save_cycle_state(cycle_state)
        except Exception as e:
            logger.error(f"Failed to continue cycle from maker: {e}", exc_info=True)

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

        # Guard against concurrent calls from successive polling iterations
        lock = self._get_cycle_lock(cycle_state.project_name, cycle_state.issue_number)
        if not lock.acquire(blocking=False):
            logger.info(
                f"Resume already in progress for issue #{cycle_state.issue_number}, "
                f"skipping concurrent call"
            )
            return

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
            github = self._get_github_integration(cycle_state)
            issue_data = await github.get_issue_details(cycle_state.issue_number, cycle_state.repository)

            # Update state: human feedback received, reviewer working
            cycle_state.status = 'reviewer_working'
            self._save_cycle_state(cycle_state)

            # Restore pipeline run to "active" now that we're resuming work
            try:
                from services.pipeline_run import get_pipeline_run_manager
                get_pipeline_run_manager().update_run_status(
                    cycle_state.project_name, cycle_state.issue_number, "active"
                )
            except Exception as e:
                logger.warning(f"Failed to restore active status for pipeline run on review cycle resume: {e}")

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

            reviewer_context = await self._execute_agent_directly(
                cycle_state.reviewer_agent,
                reviewer_task_context,
                cycle_state.project_name
            )

            # Get updated review from context (avoids GitHub API timing issues)
            updated_review = reviewer_context.get('agent_output') or reviewer_context.get('markdown_review', '')

            # Fallback: if no agent_output in context, fetch from GitHub
            if not updated_review:
                logger.warning(f"No agent_output in context, fetching from GitHub for {cycle_state.reviewer_agent}")
                updated_review = await self._get_latest_agent_comment(
                    cycle_state.issue_number,
                    cycle_state.repository,
                    cycle_state.reviewer_agent,
                    cycle_state.workspace_type,
                    cycle_state.discussion_id,
                    org
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
                self._remove_cycle_state(cycle_state)
                ck = self._cycle_key(cycle_state.project_name, cycle_state.issue_number)
                if ck in self.active_cycles:
                    del self.active_cycles[ck]
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
                self._remove_cycle_state(cycle_state)
                ck = self._cycle_key(cycle_state.project_name, cycle_state.issue_number)
                if ck in self.active_cycles:
                    del self.active_cycles[ck]

                # Analyze review cycle outcomes for learning (async, non-blocking)
                await self._analyze_review_cycle_outcomes(cycle_state)

                if column.auto_advance_on_approval:
                    next_column = self._get_next_column_name(column, cycle_state.project_name, cycle_state.board_name)
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
                if cycle_state.workspace_type == 'issues':
                    from services.project_workspace import workspace_manager
                    _pd = workspace_manager.get_project_dir(cycle_state.project_name)
                    commit_hash = self._get_git_commit_hash(str(_pd))
                    if commit_hash:
                        cycle_state.pre_maker_commit = commit_hash
                self._save_cycle_state(cycle_state)

                maker_context = await self._execute_agent_directly(
                    cycle_state.maker_agent,
                    maker_task_context,
                    cycle_state.project_name
                )

                # Get maker output from context (avoids GitHub API timing issues)
                maker_output = maker_context.get('agent_output', '')

                # Fallback: if no agent_output in context, fetch from GitHub
                if not maker_output:
                    logger.warning(f"No agent_output in context, fetching from GitHub for {cycle_state.maker_agent}")
                    maker_output = await self._get_latest_agent_comment(
                        cycle_state.issue_number,
                        cycle_state.repository,
                        cycle_state.maker_agent,
                        cycle_state.workspace_type,
                        cycle_state.discussion_id,
                        org
                    )

                # Store maker output
                cycle_state.maker_outputs.append({
                    'iteration': iteration,
                    'output': maker_output,
                    'timestamp': datetime.now().isoformat()
                })

                # Do not increment iteration here - let the review loop handle it
                # cycle_state.current_iteration += 1
                cycle_state.status = 'initialized'  # Ready for next review iteration
                self._save_cycle_state(cycle_state)

                logger.info(f"Maker completed revision, cycle ready for next iteration (now {cycle_state.current_iteration + 1})")

                # The cycle is now ready for the next review iteration
                # Project monitor will pick it up in the next polling cycle

        except Exception as e:
            logger.error(f"Failed to resume review cycle with feedback: {e}")
            import traceback
            logger.error(traceback.format_exc())
            # Restore awaiting_human_feedback so the next poll can retry
            ck = self._cycle_key(cycle_state.project_name, cycle_state.issue_number)
            if ck in self.active_cycles:
                cycle_state.status = 'awaiting_human_feedback'
                self._save_cycle_state(cycle_state)
                # Restore feedback_listening so the zombie watchdog doesn't kill
                # the run while we wait for the next retry/poll.
                try:
                    from services.pipeline_run import get_pipeline_run_manager
                    get_pipeline_run_manager().update_run_status(
                        cycle_state.project_name, cycle_state.issue_number, "feedback_listening"
                    )
                except Exception as e:
                    logger.warning(f"Failed to restore feedback_listening status on review cycle error recovery: {e}")
            raise

        finally:
            lock.release()

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

            if not result or 'node' not in result or not result['node']:
                raise Exception(f"Could not fetch discussion {discussion_id}")

            all_comments = result['node']['comments']['nodes']

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

            # Check if there's a recent reviewer comment with status
            last_reviewer_comment = None
            for item in reversed(timeline):
                if item['author'] == 'orchestrator-bot':
                    body = item['body']
                    # Check if this is a reviewer output
                    if any(status in body for status in ['APPROVED', 'CHANGES REQUESTED', 'CHANGES NEEDED', 'BLOCKED']) or '## Issues Found' in body or '### Issues Found' in body:
                        last_reviewer_comment = {
                            'body': body,
                            'created_at': item['created_at']
                        }
                        break

            # Case 1: Recent review with CHANGES_REQUESTED (not escalated) → Resume maker-checker cycle
            if last_reviewer_comment and not last_escalation:
                # Parse the review to determine status
                logger.info(f"Found reviewer comment from {last_reviewer_comment['created_at']}, parsing status...")
                comment_body = last_reviewer_comment['body']
                logger.info(f"Comment body has 'BLOCKED': {'BLOCKED' in comment_body}")
                logger.info(f"Comment body has 'CHANGES NEEDED': {'CHANGES NEEDED' in comment_body}")
                status_lines = [line for line in comment_body.split('\n') if 'Status' in line]
                logger.info(f"Status line in comment: {status_lines[:2] if status_lines else 'None found'}")
                review_result = self.review_parser.parse_review(comment_body)

                logger.info(f"Parser returned status: {review_result.status.value}, blocking_count: {len([f for f in review_result.findings if f.severity == 'blocking'])}")
                logger.info(f"Detected recent review with status: {review_result.status.value}")

                if review_result.status == ReviewStatus.APPROVED:
                    logger.info("Review already approved - advancing to next column")
                    if column.auto_advance_on_approval:
                        next_column = self._get_next_column_name(column, project_name, board_name)
                        return next_column, True
                    else:
                        return column.name, True

                elif review_result.status == ReviewStatus.CHANGES_REQUESTED:
                    logger.info("Detected: Changes requested - resuming maker-checker cycle")

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

                    # Reconstruct outputs from timeline
                    maker_signature = f"_Processed by the {maker_agent} agent_"
                    reviewer_signature = f"_Processed by the {reviewer_agent} agent_"

                    for item in timeline:
                        body = item['body']
                        if maker_signature in body:
                            cycle_state.maker_outputs.append({
                                'iteration': len(cycle_state.maker_outputs),
                                'output': body,
                                'timestamp': item['created_at'].isoformat()
                            })
                        elif reviewer_signature in body or '## Issues Found' in body:
                            cycle_state.review_outputs.append({
                                'iteration': len(cycle_state.review_outputs),
                                'output': body,
                                'timestamp': item['created_at'].isoformat()
                            })

                    # Register in active cycles
                    rk = self._cycle_key(project_name, issue_number)
                    self.active_cycles[rk] = cycle_state
                    self._save_cycle_state(cycle_state)

                    logger.info(f"Resuming review cycle - iteration {iteration + 1}/{cycle_state.max_iterations}")

                    # Guard against concurrent execution
                    lock = self._get_cycle_lock(project_name, issue_number)
                    if not lock.acquire(blocking=False):
                        logger.info(
                            f"Review cycle execution already in progress for issue #{issue_number}, "
                            f"skipping concurrent resume"
                        )
                        return column.name, False

                    # Continue the review loop
                    try:
                        next_column, cycle_complete = await self._execute_review_loop(
                            cycle_state,
                            column,
                            issue_data,
                            org
                        )
                        return next_column, cycle_complete
                    finally:
                        lock.release()

                # BLOCKED status handled by escalation logic below

            # Case 2: Escalation with human feedback
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
                reviewer_context = await self._execute_agent_directly(
                    reviewer_agent,
                    reviewer_task_context,
                    project_name
                )

                # Get updated review from context (avoids GitHub API timing issues)
                updated_review = reviewer_context.get('agent_output') or reviewer_context.get('markdown_review', '')

                # Fallback: if no agent_output in context, fetch from GitHub
                if not updated_review:
                    logger.warning(f"No agent_output in context, fetching from GitHub for {reviewer_agent}")
                    updated_review = await self._get_latest_agent_comment(
                        issue_number,
                        repository,
                        reviewer_agent,
                        'discussions',
                        discussion_id,
                        org
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
                        next_column = self._get_next_column_name(column, project_name, board_name)
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
                    rk = self._cycle_key(project_name, issue_number)
                    self.active_cycles[rk] = cycle_state

                    # Guard against concurrent execution
                    lock = self._get_cycle_lock(project_name, issue_number)
                    if not lock.acquire(blocking=False):
                        logger.info(
                            f"Review cycle execution already in progress for issue #{issue_number}, "
                            f"skipping concurrent resume"
                        )
                        return column.name, False

                    # Continue the review loop from current iteration
                    try:
                        next_column, cycle_complete = await self._execute_review_loop(
                            cycle_state,
                            column,
                            issue_data,
                            org
                        )
                        return next_column, cycle_complete
                    finally:
                        lock.release()

                else:  # BLOCKED
                    logger.error("Review still blocked after human feedback")
                    return column.name, False

            elif last_escalation and not human_feedback_after_escalation:
                # State: Escalated but no human feedback yet → Recreate awaiting state
                logger.info("Detected: Escalation without human feedback - recreating awaiting_human_feedback state")

                # Create cycle state and save as awaiting feedback
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
                cycle_state.status = 'awaiting_human_feedback'
                cycle_state.escalation_time = last_escalation['created_at'].isoformat()

                # Reconstruct outputs from discussion timeline
                maker_signature = f"_Processed by the {maker_agent} agent_"
                reviewer_signature = f"_Processed by the {reviewer_agent} agent_"

                for item in timeline:
                    body = item['body']
                    if maker_signature in body:
                        cycle_state.maker_outputs.append({
                            'iteration': len(cycle_state.maker_outputs),
                            'output': body,
                            'timestamp': item['created_at'].isoformat()
                        })
                    elif reviewer_signature in body or 'Review of' in body:
                        cycle_state.review_outputs.append({
                            'iteration': len(cycle_state.review_outputs),
                            'output': body,
                            'timestamp': item['created_at'].isoformat()
                        })

                logger.info(
                    f"Reconstructed cycle state: {len(cycle_state.maker_outputs)} maker outputs, "
                    f"{len(cycle_state.review_outputs)} reviewer outputs"
                )

                # Register in active cycles and save state
                rk = self._cycle_key(project_name, issue_number)
                self.active_cycles[rk] = cycle_state
                self._save_cycle_state(cycle_state)

                # Mark the pipeline run as "feedback_listening" so the zombie watchdog
                # does not kill it while we wait for human input.
                try:
                    from services.pipeline_run import get_pipeline_run_manager
                    get_pipeline_run_manager().update_run_status(
                        cycle_state.project_name, cycle_state.issue_number, "feedback_listening"
                    )
                except Exception as e:
                    logger.warning(f"Failed to set feedback_listening status for pipeline run during review recovery: {e}")

                logger.info(
                    f"Review cycle state restored for issue #{issue_number}. "
                    f"Status: awaiting_human_feedback. "
                    f"Project monitor will check periodically and resume when feedback is detected."
                )

                # Don't try to resume yet - wait for human feedback
                return column.name, False

            else:
                # Check if there's a BLOCKED review that needs escalation
                if last_reviewer_comment:
                    review_result = self.review_parser.parse_review(last_reviewer_comment['body'])
                    if review_result.status == ReviewStatus.BLOCKED:
                        logger.warning("Review is BLOCKED but no escalation detected - may need manual intervention")
                        return column.name, False

                logger.warning("Could not determine resume action - no review output or escalation detected")
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
        # Ensure context writer is available (handles restart recovery automatically)
        self._ensure_context_writer(cycle_state, issue_data)

        while cycle_state.current_iteration < cycle_state.max_iterations:
            # Check cancellation before each iteration
            from services.cancellation import get_cancellation_signal, CancellationError
            if get_cancellation_signal().is_cancelled(cycle_state.project_name, cycle_state.issue_number):
                logger.info(f"Review cycle cancelled for issue #{cycle_state.issue_number}")
                raise CancellationError(
                    f"Review cycle cancelled for {cycle_state.project_name}/#{cycle_state.issue_number}"
                )

            cycle_state.current_iteration += 1
            iteration = cycle_state.current_iteration

            logger.info(
                f"Review cycle iteration {iteration}/{cycle_state.max_iterations} "
                f"for issue #{cycle_state.issue_number}"
            )

            # EMIT DECISION EVENT: Review cycle iteration
            self.decision_events.emit_review_cycle_decision(
                issue_number=cycle_state.issue_number,
                project=cycle_state.project_name,
                board=cycle_state.board_name,
                cycle_iteration=iteration,
                decision_type='iteration',
                maker_agent=cycle_state.maker_agent,
                reviewer_agent=cycle_state.reviewer_agent,
                reason=f"Starting iteration {iteration} of {cycle_state.max_iterations}",
                additional_data={
                    'max_iterations': cycle_state.max_iterations,
                    'workspace_type': cycle_state.workspace_type
                },
                pipeline_run_id=cycle_state.pipeline_run_id
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

            # Update current_diff.md so the reviewer can read the latest changes
            if cycle_state.context_writer:
                try:
                    cycle_state.context_writer.write_current_diff(
                        review_task_context.get('change_manifest', '')
                    )
                except Exception as e:
                    logger.warning(f"Failed to write current_diff to context dir: {e}")

            # EMIT DECISION EVENT: Reviewer selected
            self.decision_events.emit_review_cycle_decision(
                issue_number=cycle_state.issue_number,
                project=cycle_state.project_name,
                board=cycle_state.board_name,
                cycle_iteration=iteration,
                decision_type='reviewer_selected',
                maker_agent=cycle_state.maker_agent,
                reviewer_agent=cycle_state.reviewer_agent,
                reason=f"Executing reviewer agent '{cycle_state.reviewer_agent}' for iteration {iteration}",
                pipeline_run_id=cycle_state.pipeline_run_id
            )

            # Execute reviewer agent
            reviewer_context = await self._execute_agent_directly(
                cycle_state.reviewer_agent,
                review_task_context,
                cycle_state.project_name
            )

            # Get reviewer's output from context (avoids GitHub API timing issues).
            # Guard against a non-dict return (e.g. "" from a validation failure in the
            # docker runner) — treat it as no inline output and fall through to the GitHub
            # fetch below rather than crashing with AttributeError.
            review_comment = (reviewer_context.get('agent_output') or reviewer_context.get('markdown_review', '')) if isinstance(reviewer_context, dict) else ''

            # Fallback: if no agent_output in context, fetch from GitHub
            if not review_comment:
                logger.warning(f"No agent_output in context, fetching from GitHub for {cycle_state.reviewer_agent}")
                review_comment = await self._get_latest_agent_comment(
                    cycle_state.issue_number,
                    cycle_state.repository,
                    cycle_state.reviewer_agent,
                    cycle_state.workspace_type,
                    cycle_state.discussion_id,
                    org
                )

            # Store review output
            cycle_state.review_outputs.append({
                'iteration': iteration,
                'output': review_comment,
                'timestamp': datetime.now().isoformat()
            })
            if cycle_state.context_writer:
                try:
                    cycle_state.context_writer.write_review_feedback(review_comment, iteration)
                except Exception as e:
                    logger.warning(f"Failed to write review_feedback_{iteration} to context dir: {e}")
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

            # Update state after reviewer completes
            cycle_state.status = 'initialized'  # Reset to initialized, ready for next action
            self._save_cycle_state(cycle_state)

            # Step 3: Make decision based on review status
            if review_result_parsed.status == ReviewStatus.APPROVED:
                # Success! Move to next column
                logger.info(f"Review approved for issue #{cycle_state.issue_number}")
                
                # EMIT DECISION EVENT: Review cycle completed
                self.decision_events.emit_review_cycle_decision(
                    issue_number=cycle_state.issue_number,
                    project=cycle_state.project_name,
                    board=cycle_state.board_name,
                    cycle_iteration=iteration,
                    decision_type='complete',
                    maker_agent=cycle_state.maker_agent,
                    reviewer_agent=cycle_state.reviewer_agent,
                    reason=f"Review cycle completed successfully: approved after {iteration} iteration(s)",
                    additional_data={
                        'total_iterations': iteration,
                        'status': 'approved'
                    },
                    pipeline_run_id=cycle_state.pipeline_run_id
                )
                
                await self._post_cycle_summary(
                    cycle_state,
                    "APPROVED",
                    f"Review approved after {iteration} iteration(s)"
                )

                # Store current commit hash for future scoped reviews (issues workspace)
                if cycle_state.workspace_type == 'issues':
                    from services.project_workspace import workspace_manager
                    project_dir = workspace_manager.get_project_dir(cycle_state.project_name)
                    current_commit = self._get_git_commit_hash(str(project_dir))
                    if current_commit:
                        cycle_state.last_approved_commit = current_commit
                        logger.info(f"Stored approved commit {current_commit[:8]} for future scoped reviews")

                # Mark cycle as completed and remove from active state
                cycle_state.status = 'completed'
                self._save_cycle_state(cycle_state)
                self._remove_cycle_state(cycle_state)

                # NOTE: Do NOT update PR status here!
                # PR status is managed by feature_branch_manager.finalize_workspace()
                # which checks if ALL sub-issues are complete before marking PR ready.
                # The review cycle only approves individual code changes, not the entire PR.

                if column.auto_advance_on_approval:
                    next_column = self._get_next_column_name(column, cycle_state.project_name, cycle_state.board_name)
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

                    # Mark the pipeline run as "feedback_listening" so the zombie watchdog
                    # (which only queries status="active") does not kill it while we wait
                    # for human input — which can legitimately take many hours.
                    try:
                        from services.pipeline_run import get_pipeline_run_manager
                        get_pipeline_run_manager().update_run_status(
                            cycle_state.project_name, cycle_state.issue_number, "feedback_listening"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to set feedback_listening status for pipeline run during review escalation: {e}")

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

            elif review_result_parsed.status == ReviewStatus.CHANGES_REQUESTED:
                # Changes requested - check if we've hit max iterations
                if iteration >= cycle_state.max_iterations:
                    logger.warning(
                        f"Max iterations ({cycle_state.max_iterations}) reached for "
                        f"issue #{cycle_state.issue_number}"
                    )
                    await self._escalate_max_iterations(cycle_state, review_result_parsed)
                    cycle_state.status = 'awaiting_human_feedback'
                    cycle_state.escalation_time = datetime.now().isoformat()
                    self._save_cycle_state(cycle_state)
                    try:
                        from services.pipeline_run import get_pipeline_run_manager
                        get_pipeline_run_manager().update_run_status(
                            cycle_state.project_name, cycle_state.issue_number, "feedback_listening"
                        )
                    except Exception as _e:
                        logger.warning(f"Failed to set feedback_listening status after max_iterations escalation: {_e}")
                    return ReviewStatus.CHANGES_REQUESTED, column.name
                # Continue to Step 4 to invoke maker with feedback

            elif review_result_parsed.status == ReviewStatus.UNKNOWN:
                # Could not parse review status - escalate
                logger.error(
                    f"Could not determine review status from reviewer output for issue #{cycle_state.issue_number}. "
                    f"Review text length: {len(review_comment)} chars. Escalating."
                )
                await self._escalate_blocked(cycle_state, review_result_parsed)
                return ReviewStatus.BLOCKED, column.name

            else:
                # Unexpected status (PENDING, etc.) - log and escalate
                logger.error(
                    f"Unexpected review status '{review_result_parsed.status.value}' for issue #{cycle_state.issue_number}. "
                    f"Escalating."
                )
                await self._escalate_blocked(cycle_state, review_result_parsed)
                return ReviewStatus.BLOCKED, column.name

            # Step 4: Re-invoke maker agent with feedback
            logger.info(f"Re-invoking {cycle_state.maker_agent} with reviewer feedback")
            
            # EMIT DECISION EVENT: Maker selected
            self.decision_events.emit_review_cycle_decision(
                issue_number=cycle_state.issue_number,
                project=cycle_state.project_name,
                board=cycle_state.board_name,
                cycle_iteration=iteration,
                decision_type='maker_selected',
                maker_agent=cycle_state.maker_agent,
                reviewer_agent=cycle_state.reviewer_agent,
                reason=f"Executing maker agent '{cycle_state.maker_agent}' to address review feedback in iteration {iteration}",
                pipeline_run_id=cycle_state.pipeline_run_id
            )

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

            # Ensure we're on the correct branch for git workflow
            if cycle_state.workspace_type == 'issues':
                from services.project_workspace import workspace_manager
                from services.git_workflow_manager import git_workflow_manager

                # Get or create feature branch
                branch_info = git_workflow_manager.get_branch_info(
                    cycle_state.project_name,
                    cycle_state.issue_number
                )

                if branch_info:
                    # Switch to existing branch
                    from services.git_workflow_manager import git_workflow_manager
                    project_dir = workspace_manager.get_project_dir(cycle_state.project_name)
                    await git_workflow_manager.checkout_branch(str(project_dir), branch_info.branch_name)
                    logger.info(f"Switched to branch {branch_info.branch_name} for maker revision")
                else:
                    # Create branch if it doesn't exist - use FeatureBranchManager to respect parent/sub-issue relationships
                    from services.feature_branch_manager import feature_branch_manager
                    from services.github_integration import GitHubIntegration
                    
                    # Get github integration instance
                    github_integration = self._get_github_for_project(cycle_state.project_name, cycle_state.repository)
                    
                    try:
                        project_dir = workspace_manager.get_project_dir(cycle_state.project_name)
                        # Use FeatureBranchManager which handles parent/sub-issue detection
                        branch_name = await feature_branch_manager.prepare_feature_branch(
                            project=cycle_state.project_name,
                            issue_number=cycle_state.issue_number,
                            github_integration=github_integration,
                            issue_title=issue_data.get('title') or f"Issue {cycle_state.issue_number}"
                        )
                        
                        # Track the branch for git workflow (this will be done by feature_branch_manager too, but ensure it)
                        if not git_workflow_manager.get_branch_info(cycle_state.project_name, cycle_state.issue_number):
                            # Check if this is a sub-issue
                            feature_branch = await feature_branch_manager.get_feature_branch_for_issue(cycle_state.project_name, cycle_state.issue_number, github_integration)
                            if feature_branch:
                                # Track against parent for PR creation
                                git_workflow_manager.track_branch(
                                    cycle_state.project_name,
                                    feature_branch.parent_issue,  # Track with parent issue number
                                    branch_name
                                )
                                logger.info(f"Tracked sub-issue #{cycle_state.issue_number} branch {branch_name} under parent #{feature_branch.parent_issue}")
                            else:
                                # Standalone issue
                                git_workflow_manager.track_branch(
                                    cycle_state.project_name,
                                    cycle_state.issue_number,
                                    branch_name
                                )
                                logger.info(f"Tracked standalone issue #{cycle_state.issue_number} branch {branch_name}")
                        
                        logger.info(f"Prepared branch {branch_name} for maker revision using FeatureBranchManager")
                    except Exception as e:
                        logger.error(f"Failed to prepare branch for issue #{cycle_state.issue_number}: {e}")
                        # Continue without branch - agent will work on current branch
                        logger.warning(f"Continuing on current branch for issue #{cycle_state.issue_number}")

            # Snapshot HEAD before the maker runs so the reviewer diffs only this iteration
            if cycle_state.workspace_type == 'issues':
                from services.project_workspace import workspace_manager
                _pd = workspace_manager.get_project_dir(cycle_state.project_name)
                commit_hash = self._get_git_commit_hash(str(_pd))
                if commit_hash:
                    cycle_state.pre_maker_commit = commit_hash
                self._save_cycle_state(cycle_state)

            # Execute maker agent directly
            await self._execute_agent_directly(
                cycle_state.maker_agent,
                maker_task_context,
                cycle_state.project_name
            )

            # Auto-commit changes if maker makes code changes
            if cycle_state.workspace_type == 'issues':
                from services.auto_commit import auto_commit_service
                from config.manager import config_manager

                agent_config = config_manager.get_project_agent_config(
                    cycle_state.project_name,
                    cycle_state.maker_agent
                )
                makes_code_changes = getattr(agent_config, 'makes_code_changes', False)

                if makes_code_changes:
                    logger.info(f"Agent {cycle_state.maker_agent} makes code changes, attempting auto-commit")
                    commit_success = await auto_commit_service.commit_agent_changes(
                        project=cycle_state.project_name,
                        agent=cycle_state.maker_agent,
                        task_id=f"review_cycle_iter_{iteration}",
                        issue_number=cycle_state.issue_number,
                        custom_message=f"Address code review feedback (iteration {iteration})\n\nIssue #{cycle_state.issue_number}"
                    )

                    if commit_success:
                        logger.info(f"Auto-committed changes for iteration {iteration}")
                    else:
                        logger.warning(f"No changes to commit for iteration {iteration}")

            # Get maker's revised output from GitHub (workspace-aware)
            maker_comment = await self._get_latest_agent_comment(
                cycle_state.issue_number,
                cycle_state.repository,
                cycle_state.maker_agent,
                cycle_state.workspace_type,
                cycle_state.discussion_id,
                org
            )

            # Verify maker responded
            if not maker_comment:
                logger.error(f"Maker {cycle_state.maker_agent} did not post a response on iteration {iteration}")
                await self._escalate_max_iterations(cycle_state, review_result_parsed)
                cycle_state.status = 'awaiting_human_feedback'
                cycle_state.escalation_time = datetime.now().isoformat()
                self._save_cycle_state(cycle_state)
                try:
                    from services.pipeline_run import get_pipeline_run_manager
                    get_pipeline_run_manager().update_run_status(
                        cycle_state.project_name, cycle_state.issue_number, "feedback_listening"
                    )
                except Exception as _e:
                    logger.warning(f"Failed to set feedback_listening status after max_iterations escalation: {_e}")
                return ReviewStatus.CHANGES_REQUESTED, column.name

            # Store maker output and persist immediately so a restart mid-loop
            # doesn't leave the on-disk state missing the maker's work
            cycle_state.maker_outputs.append({
                'iteration': iteration,
                'output': maker_comment,
                'timestamp': datetime.now().isoformat()
            })
            if cycle_state.context_writer:
                try:
                    # maker_output index = iteration + 1 (iteration 1 review → maker_output_2, etc.)
                    cycle_state.context_writer.write_maker_output(maker_comment, iteration + 1)
                except Exception as e:
                    logger.warning(f"Failed to write maker_output_{iteration + 1} to context dir: {e}")
            self._save_cycle_state(cycle_state)

            # Continue to next iteration - the loop will go back to the reviewer
            logger.info(f"Maker responded, continuing to iteration {iteration + 1} for re-review")

        # Should not reach here, but handle gracefully
        logger.error(f"Review loop exited unexpectedly for issue #{cycle_state.issue_number}")
        return ReviewStatus.UNKNOWN, column.name

    def _get_git_commit_hash(self, project_dir: str, ref: str = 'HEAD') -> str:
        """Resolve a git ref to its full commit SHA (defaults to HEAD)"""
        import subprocess
        try:
            result = subprocess.run(
                ['git', 'rev-parse', ref],
                cwd=project_dir,
                capture_output=True,
                text=True,
                check=True
            )
            return result.stdout.strip()
        except Exception as e:
            logger.warning(f"Failed to resolve git ref {ref!r}: {e}")
            return ""

    def _get_change_manifest(self, project_dir: str, base_commit: str) -> str:
        """Build a compact change manifest (stat + commits) for the reviewer.

        Returns a formatted string the reviewer uses to decide which files to
        inspect via 'git diff <base_commit> HEAD -- <file>'.
        """
        import subprocess

        def run(cmd):
            try:
                return subprocess.run(
                    cmd, cwd=project_dir, capture_output=True, text=True, check=True
                ).stdout.strip()
            except Exception as e:
                logger.warning(f"git command {cmd} failed: {e}")
                return ""

        stat = run(['git', 'diff', '--stat', base_commit, 'HEAD'])
        commits = run(['git', 'log', '--oneline', f'{base_commit}..HEAD'])

        if not stat:
            return ""

        base_label = "pre-maker snapshot" if base_commit != "HEAD~1" else "HEAD~1 fallback"
        commit_section = f"\n### Commits\n```\n{commits}\n```" if commits else ""
        return (
            f"**Base commit**: `{base_commit[:8]}` ({base_label})"
            f"{commit_section}\n\n"
            f"### Files changed\n```\n{stat}\n```\n\n"
            f"Use your bash tool to examine the actual changes:\n"
            f"- Full diff: `git diff {base_commit[:8]} HEAD`\n"
            f"- Single file: `git diff {base_commit[:8]} HEAD -- <path/to/file>`"
        )

    def _create_review_task_context(
        self,
        cycle_state: ReviewCycleState,
        column: WorkflowColumn,
        issue_data: Dict[str, Any],
        iteration: int,
        full_discussion_context: str = ""
    ) -> Dict[str, Any]:
        """Create task context for reviewer agent"""
        # Get the latest maker output (kept for review_cycle metadata only)
        latest_maker_output = cycle_state.maker_outputs[-1]['output'] if cycle_state.maker_outputs else ""

        # For discussions workspace, pass full_discussion_context; for issues, leave empty
        context_for_review = full_discussion_context if full_discussion_context else ""

        # Build a compact change manifest for issues workspace
        change_manifest = ""
        if cycle_state.workspace_type == 'issues':
            from services.project_workspace import workspace_manager
            project_dir = workspace_manager.get_project_dir(cycle_state.project_name)

            # Primary: changes since the HEAD snapshot taken just before the maker ran
            base_commit = cycle_state.pre_maker_commit
            if base_commit:
                change_manifest = self._get_change_manifest(str(project_dir), base_commit)
                if change_manifest:
                    logger.info(f"Generated change manifest from pre-maker snapshot {base_commit[:8]}")
                else:
                    logger.info(f"No changes since pre-maker snapshot {base_commit[:8]}")

            # Fallback: most recent commit only — resolve to a pinned SHA so the
            # manifest contains a stable reference rather than the symbolic HEAD~1.
            if not change_manifest:
                resolved = self._get_git_commit_hash(str(project_dir), 'HEAD~1')
                base_commit = resolved if resolved else 'HEAD~1'
                change_manifest = self._get_change_manifest(str(project_dir), base_commit)
                if change_manifest:
                    logger.info(f"Fallback: generated change manifest from HEAD~1 ({base_commit[:8]})")

            if not change_manifest:
                raise RuntimeError(
                    f"Cannot build reviewer context for issue #{cycle_state.issue_number}: "
                    f"no git changes found. pre_maker_commit="
                    f"{cycle_state.pre_maker_commit!r} and HEAD~1 fallback both returned empty. "
                    f"Aborting review cycle."
                )

        _review_stack = push_frame(
            cycle_state.cycle_stack,
            CycleFrame(
                cycle_type="review_cycle",
                cycle_id=cycle_state.pipeline_run_id or str(cycle_state.issue_number),
                iteration=iteration,
                max_iterations=cycle_state.max_iterations,
                label=f"review_cycle[#{cycle_state.issue_number} iter {iteration}]",
            ),
        )
        task_context = {
            'project': cycle_state.project_name,
            'board': cycle_state.board_name,
            'repository': cycle_state.repository,
            'issue_number': cycle_state.issue_number,
            'issue': issue_data,
            'column': column.name,
            'trigger': 'review_cycle',
            'workspace_type': cycle_state.workspace_type,
            'discussion_id': cycle_state.discussion_id,
            'pipeline_run_id': cycle_state.pipeline_run_id,  # Include pipeline run ID for event tracking
            'cycle_stack': _review_stack,
            'review_cycle': {
                'iteration': iteration,
                'max_iterations': cycle_state.max_iterations,
                'maker_agent': cycle_state.maker_agent,
                'previous_maker_output': latest_maker_output,
                'is_rereviewing': iteration > 1,
                'previous_review_feedback': (
                    cycle_state.review_outputs[-1]['output']
                    if cycle_state.review_outputs else None
                ),
            },
            'previous_stage_output': context_for_review,  # Discussions context only; empty for issues
            'change_manifest': change_manifest,  # Compact manifest; reviewer fetches diffs via bash
            'review_cycle_context_dir': cycle_state.context_dir,  # File-based context (mounted in container)
            'timestamp': datetime.now().isoformat()
        }
        logger.info(f"[DIAGNOSTIC] Created review task_context with pipeline_run_id: {cycle_state.pipeline_run_id} for issue #{cycle_state.issue_number}")
        return task_context

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

        _maker_stack = push_frame(
            cycle_state.cycle_stack,
            CycleFrame(
                cycle_type="review_cycle",
                cycle_id=cycle_state.pipeline_run_id or str(cycle_state.issue_number),
                iteration=iteration,
                max_iterations=cycle_state.max_iterations,
                label=f"review_cycle[#{cycle_state.issue_number} revision {iteration}]",
            ),
        )
        task_context = {
            'project': cycle_state.project_name,
            'board': cycle_state.board_name,
            'repository': cycle_state.repository,
            'issue_number': cycle_state.issue_number,
            'issue': issue_data,
            'column': column.name,
            'trigger': 'review_cycle_revision',  # Specific trigger for review cycle revisions
            'workspace_type': cycle_state.workspace_type,
            'discussion_id': cycle_state.discussion_id,
            'pipeline_run_id': cycle_state.pipeline_run_id,  # Include pipeline run ID for event tracking
            'cycle_stack': _maker_stack,
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
            'review_cycle_context_dir': cycle_state.context_dir,  # File-based context (mounted in container)
            'timestamp': datetime.now().isoformat()
        }
        logger.info(f"[DIAGNOSTIC] Created maker revision task_context with pipeline_run_id: {cycle_state.pipeline_run_id} for issue #{cycle_state.issue_number}")
        return task_context

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

            # This function is called right before the REVIEWER executes at the start of each iteration
            # The reviewer ALWAYS needs to see the maker's latest output
            # The loop structure is: reviewer → (if changes needed) → maker → next iteration
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
        from services.work_execution_state import work_execution_tracker
        from services.cancellation import get_cancellation_signal, CancellationError

        # Check cancellation before launching agent
        issue_number = task_context.get('issue_number')
        if issue_number and get_cancellation_signal().is_cancelled(project_name, issue_number):
            raise CancellationError(
                f"Agent {agent_name} not launched: work cancelled for {project_name}/#{issue_number}"
            )

        logger.info(f"Executing {agent_name} directly for review cycle")

        # Record execution start for state tracking
        if 'issue_number' in task_context and 'column' in task_context:
            work_execution_tracker.record_execution_start(
                issue_number=task_context['issue_number'],
                column=task_context['column'],
                agent=agent_name,
                trigger_source='review_cycle',  # Clear audit trail
                project_name=project_name
            )
        else:
            logger.warning(
                f"Cannot record execution start for {agent_name}: "
                f"missing issue_number or column in task_context"
            )

        executor = get_agent_executor()
        return await executor.execute_agent(
            agent_name=agent_name,
            project_name=project_name,
            task_context=task_context,
            execution_type="review_cycle"
        )

    def _emit_review_cycle_error(
        self,
        cycle_state: ReviewCycleState,
        error: Exception,
        error_context: str,
        recovery_action: str = "Review cycle terminated"
    ):
        """Emit error event for UI visibility"""
        try:
            # Get pipeline_run_id from cycle state if available
            pipeline_run_id = getattr(cycle_state, 'pipeline_run_id', None)

            self.decision_events.emit_error_decision(
                error_type=f"review_cycle_{error_context}",
                error_message=str(error),
                context={
                    'issue_number': cycle_state.issue_number,
                    'project': cycle_state.project_name,
                    'board': cycle_state.board_name,
                    'cycle_iteration': cycle_state.current_iteration,
                    'maker_agent': cycle_state.maker_agent,
                    'reviewer_agent': cycle_state.reviewer_agent,
                    'workspace_type': cycle_state.workspace_type
                },
                recovery_action=recovery_action,
                success=False,
                project=cycle_state.project_name,
                pipeline_run_id=pipeline_run_id
            )
        except Exception as emit_error:
            logger.error(f"Failed to emit review cycle error event: {emit_error}", exc_info=True)

    async def _get_latest_agent_comment(
        self,
        issue_number: int,
        repository: str,
        agent_name: str,
        workspace_type: str = 'issues',
        discussion_id: Optional[str] = None,
        org: str = None
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
                # Build repo string in org/repo format for gh CLI
                repo_arg = f"{org}/{repository}" if org else repository
                result = subprocess.run(
                    ['gh', 'issue', 'view', str(issue_number), '--repo', repo_arg, '--json', 'comments'],
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
            discussions.add_discussion_comment(cycle_state.discussion_id, summary,
                                              pipeline_run_id=cycle_state.pipeline_run_id)
        else:
            # Post to issue
            github = self._get_github_integration(cycle_state)
            await github.post_issue_comment(
                cycle_state.issue_number,
                summary,
                cycle_state.repository,
                pipeline_run_id=cycle_state.pipeline_run_id,
            )

    async def _escalate_blocked(self, cycle_state: ReviewCycleState, review_result):
        """Escalate when blocking issues are found (workspace-aware)"""
        from services.github_integration import GitHubIntegration
        import subprocess
        
        # EMIT DECISION EVENT: Review cycle escalated
        blocking_issues = [
            f.message for f in review_result.findings if f.severity == 'blocking'
        ]
        self.decision_events.emit_review_cycle_decision(
            issue_number=cycle_state.issue_number,
            project=cycle_state.project_name,
            board=cycle_state.board_name,
            cycle_iteration=cycle_state.current_iteration,
            decision_type='escalate',
            maker_agent=cycle_state.maker_agent,
            reviewer_agent=cycle_state.reviewer_agent,
            reason=f"Escalating due to {review_result.blocking_count} blocking issue(s) found by reviewer",
            additional_data={
                'escalation_reason': 'blocking_issues',
                'blocking_count': review_result.blocking_count,
                'blocking_issues': blocking_issues[:5]  # First 5 for brevity
            },
            pipeline_run_id=cycle_state.pipeline_run_id
        )

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
            discussions.add_discussion_comment(cycle_state.discussion_id, escalation_comment,
                                              pipeline_run_id=cycle_state.pipeline_run_id)
        else:
            # Post to issue
            github = self._get_github_integration(cycle_state)
            await github.post_issue_comment(
                cycle_state.issue_number,
                escalation_comment,
                cycle_state.repository,
                pipeline_run_id=cycle_state.pipeline_run_id,
            )

        logger.warning(
            f"Escalated blocking issues for issue #{cycle_state.issue_number}: "
            f"{review_result.blocking_count} blocking"
        )

    async def _escalate_max_iterations(self, cycle_state: ReviewCycleState, review_result):
        """Escalate when max iterations reached without approval (workspace-aware)"""
        import subprocess
        
        # EMIT DECISION EVENT: Review cycle escalated
        self.decision_events.emit_review_cycle_decision(
            issue_number=cycle_state.issue_number,
            project=cycle_state.project_name,
            board=cycle_state.board_name,
            cycle_iteration=cycle_state.current_iteration,
            decision_type='escalate',
            maker_agent=cycle_state.maker_agent,
            reviewer_agent=cycle_state.reviewer_agent,
            reason=f"Escalating: max iterations ({cycle_state.max_iterations}) reached without approval",
            additional_data={
                'escalation_reason': 'max_iterations',
                'remaining_issues': len(review_result.findings),
                'quality_score': review_result.score
            },
            pipeline_run_id=cycle_state.pipeline_run_id
        )

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
            discussions.add_discussion_comment(cycle_state.discussion_id, escalation_comment,
                                              pipeline_run_id=cycle_state.pipeline_run_id)
        else:
            # Post to issue
            github = self._get_github_integration(cycle_state)
            await github.post_issue_comment(
                cycle_state.issue_number,
                escalation_comment,
                cycle_state.repository,
                pipeline_run_id=cycle_state.pipeline_run_id,
            )

        logger.warning(
            f"Escalated max iterations for issue #{cycle_state.issue_number}: "
            f"{cycle_state.current_iteration}/{cycle_state.max_iterations}"
        )

    def _get_next_column_name(self, current_column: WorkflowColumn, project_name: str, board_name: str) -> str:
        """Determine next column name after approval"""
        from config.manager import config_manager

        try:
            workflow_template = config_manager.get_project_workflow(project_name, board_name)
            workflow_columns = workflow_template.columns
        except Exception as e:
            logger.warning(f"Could not load workflow columns for {project_name}/{board_name}: {e}")
            return current_column.name

        # Find current column index
        current_index = -1
        for i, col in enumerate(workflow_columns):
            if col.name == current_column.name:
                current_index = i
                break

        if current_index == -1:
            logger.warning(f"Current column {current_column.name} not found in workflow for {project_name}/{board_name}")
            return current_column.name

        # Get next column
        if current_index + 1 < len(workflow_columns):
            next_col = workflow_columns[current_index + 1]
            logger.info(f"Next column after {current_column.name}: {next_col.name}")
            return next_col.name
        else:
            logger.info(f"No next column after {current_column.name}, staying in current")
            return current_column.name

    async def _advance_issue_after_review_approval(
        self, cycle_state, current_column_name: str, next_column_name: str
    ):
        """Move the issue to the next column after review approval.

        Used in recovery paths (resume_active_cycles / _continue_cycle_from_review) where
        project_monitor is not involved and won't perform the progression itself.
        """
        try:
            from services.pipeline_progression import PipelineProgression
            from task_queue.task_manager import TaskQueue

            task_queue = TaskQueue()
            progression = PipelineProgression(task_queue)
            logger.info(
                f"Recovery: advancing issue #{cycle_state.issue_number} "
                f"from {current_column_name} to {next_column_name} after review approval"
            )
            result = progression.move_issue_to_column(
                project_name=cycle_state.project_name,
                board_name=cycle_state.board_name,
                issue_number=cycle_state.issue_number,
                target_column=next_column_name,
                trigger='review_cycle_completion'
            )
            if result:
                logger.info(
                    f"Successfully moved issue #{cycle_state.issue_number} to {next_column_name}"
                )
            else:
                logger.error(
                    f"Failed to move issue #{cycle_state.issue_number} to {next_column_name} "
                    f"after review approval (move_issue_to_column returned False)"
                )
        except Exception as e:
            logger.error(
                f"Error advancing issue #{cycle_state.issue_number} to {next_column_name} "
                f"after review approval: {e}",
                exc_info=True
            )

    async def process_recovered_reviewer_output(
        self,
        project_name: str,
        issue_number: int,
        reviewer_output: str,
    ) -> None:
        """
        Process the verdict from a reviewer agent that completed via container
        recovery (after orchestrator restart).

        The normal path processes the verdict inline in _execute_review_loop()
        immediately after the reviewer returns. When a restart occurs while the
        reviewer container is running, recovery posts the output to GitHub but
        skips verdict processing. This method fills that gap.
        """
        logger.info(
            f"Processing recovered reviewer output for {project_name} issue #{issue_number}"
        )

        # 1. Resolve active cycle (in-memory first, then disk fallback)
        rk = self._cycle_key(project_name, issue_number)
        cycle_state = self.active_cycles.get(rk)
        if cycle_state is None:
            loaded = self._load_active_cycles(project_name)
            cycle_state = next(
                (c for c in loaded if c.issue_number == issue_number), None
            )
            if cycle_state:
                self.active_cycles[rk] = cycle_state

        if not cycle_state:
            logger.warning(
                f"process_recovered_reviewer_output: no active review cycle "
                f"for {project_name} issue #{issue_number} — skipping"
            )
            return

        # 2. Acquire per-issue execution lock (prevents race with project monitor poll)
        lock = self._get_cycle_lock(project_name, issue_number)
        if not lock.acquire(blocking=False):
            logger.warning(
                f"Review cycle lock already held for {project_name} issue "
                f"#{issue_number} — skipping recovered reviewer processing"
            )
            return

        try:
            # 3. Determine the correct iteration number.
            #    _execute_review_loop() increments current_iteration BEFORE running
            #    the reviewer, but saves state AFTER it returns. If the restart
            #    happened during reviewer execution, the on-disk current_iteration is
            #    still the pre-increment value (e.g. 0). Use
            #    max(current_iteration, len(review_outputs)+1) to get iteration 1
            #    regardless of whether the increment was persisted.
            target_iteration = max(
                cycle_state.current_iteration,
                len(cycle_state.review_outputs) + 1
            )

            # Idempotency guard: skip if review output already recorded
            if any(r['iteration'] == target_iteration for r in cycle_state.review_outputs):
                logger.info(
                    f"Reviewer output for iteration {target_iteration} already "
                    f"recorded for {project_name} issue #{issue_number} — skipping"
                )
                return

            # 4. Persist reviewer output and update iteration counter.
            #    Set status to 'reviewer_working' so that if _continue_cycle_from_review
            #    crashes, the next resume_active_cycles() (lines 540-559) will correctly
            #    detect "reviewer completed, needs processing" and retry.
            cycle_state.review_outputs.append({
                'iteration': target_iteration,
                'output': reviewer_output,
                'timestamp': datetime.now().isoformat(),
            })
            # issue_data is not available here (fetched later in _continue_cycle_from_review).
            # If the context dir was lost, _ensure_context_writer returns None and the
            # feedback is not written to a file — the fallback to embedded prompts handles it.
            _writer = self._ensure_context_writer(cycle_state)
            if _writer:
                try:
                    _writer.write_review_feedback(reviewer_output, target_iteration)
                except Exception as e:
                    logger.warning(f"Failed to write recovered review_feedback_{target_iteration}: {e}")
            cycle_state.current_iteration = target_iteration
            cycle_state.status = 'reviewer_working'
            self._save_cycle_state(cycle_state)

            logger.info(
                f"Stored recovered reviewer output for {project_name} issue "
                f"#{issue_number} (iteration {target_iteration})"
            )

            # 5. Load org from project config
            from config.manager import config_manager
            project_config = config_manager.get_project_config(project_name)
            org = project_config.github.get('org', '') if project_config else ''

            # 6. Process verdict via the existing path
            await self._continue_cycle_from_review(cycle_state, org)

        finally:
            lock.release()


# Global executor instance
review_cycle_executor = ReviewCycleExecutor()
