"""
Repair Cycle Pipeline Stage

Implements deterministic test-fix-validate cycles for automated testing workflows.

Architecture:
    RepairCycleStage is a specialized PipelineStage that orchestrates multiple
    agent invocations in a deterministic loop pattern:

    1. Run tests → get structured results
    2. Group failures by test file (direct Claude parse)
    3. For each file: fix failures (containerized agent)
    4. Re-run tests and validate
    5. Handle warnings (optional, same pattern)
    6. Final validation

    This is NOT a maker-checker pattern - it's a deterministic iteration loop
    with clear convergence criteria (all tests pass).

Usage:
    test_configs = [
        RepairTestRunConfig(
            test_type="unit",
            timeout=600,
            max_iterations=5,
            review_warnings=True
        )
    ]

    stage = RepairCycleStage(
        name="testing",
        test_configs=test_configs,
        agent_name="senior_software_engineer"
    )

    result = await stage.execute(context)
"""

import json
import logging
import asyncio
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict
from datetime import datetime

from pipeline.base import PipelineStage
from monitoring.timestamp_utils import utc_now, utc_isoformat, to_utc_isoformat
from monitoring.observability import EventType
from services.cancellation import CancellationError


logger = logging.getLogger(__name__)

# Shared output format appended to every test-runner prompt.
# Using a plain string (not an f-string) so the literal {} in the JSON examples
# are safe to concatenate with both plain and f-string prompt bodies.
_TEST_OUTPUT_FORMAT = """
CRITICAL: You MUST return ONLY valid JSON in this EXACT format (no markdown, no explanation):
{
    "passed": <number of passing checks or tests>,
    "failed": <number of failing checks or tests>,
    "warnings": <number of warnings (0 if none)>,
    "failures": [
        {"file": "<file path>", "test": "<check or test name>", "message": "<failure message>"},
        ...
    ],
    "warning_list": [
        {"file": "<file path>", "message": "<warning message>"},
        ...
    ]
}

If everything passes cleanly, return:
{"passed": 1, "failed": 0, "warnings": 0, "failures": [], "warning_list": []}

DO NOT include any explanation, markdown formatting, or other text - ONLY the JSON object."""

MAX_SYSTEMIC_SUB_CYCLES = 3   # max attempts per special-case sub-cycle


@dataclass
class SystemicAnalysisResult:
    """Result of systemic failure analysis"""
    has_env_issues: bool
    has_systemic_code_issues: bool
    env_issue_description: str        # plain text for setup agent prompt
    systemic_issue_description: str   # plain text for fix agent prompt
    affected_files: List[str]         # files involved in systemic code fix
    raw_json: Dict[str, Any]          # full parsed response


@dataclass
class RepairTestRunConfig:
    """Configuration for a single test type run cycle"""

    test_type: str
    timeout: int = 900  # Timeout in seconds
    max_iterations: int = 5  # Max test-fix-validate iterations
    review_warnings: bool = True  # Whether to review and fix warnings
    max_file_iterations: int = 3  # Max times to attempt fixing a single file
    systemic_analysis_threshold: int = 15  # Re-run systemic analysis when failure count exceeds this


@dataclass
class RepairTestFailure:
    """Individual test failure"""

    file: str  # Test file name
    test: str  # Test function/method name
    message: str  # Failure message


@dataclass
class RepairTestWarning:
    """Individual test warning"""

    file: str  # Source file name
    message: str  # Warning message


@dataclass
class RepairTestResult:
    """Structured test execution results"""

    test_type: str
    iteration: int
    passed: int
    failed: int
    warnings: int
    failures: List[RepairTestFailure]
    warning_list: List[RepairTestWarning]
    raw_output: str  # Full test output for debugging
    timestamp: str

    def has_failures(self) -> bool:
        """Check if there are any test failures"""
        return self.failed > 0

    def has_warnings(self) -> bool:
        """Check if there are any warnings"""
        return self.warnings > 0

    def group_failures_by_file(self) -> Dict[str, List[RepairTestFailure]]:
        """Group failures by test file"""
        grouped = {}
        for failure in self.failures:
            if failure.file not in grouped:
                grouped[failure.file] = []
            grouped[failure.file].append(failure)
        return grouped

    def group_warnings_by_file(self) -> Dict[str, List[RepairTestWarning]]:
        """Group warnings by source file"""
        grouped = {}
        for warning in self.warning_list:
            if warning.file not in grouped:
                grouped[warning.file] = []
            grouped[warning.file].append(warning)
        return grouped


@dataclass
class CycleResult:
    """Result of a complete test-fix cycle for one test type"""

    test_type: str
    passed: bool
    iterations: int
    final_result: Optional[RepairTestResult]
    error: Optional[str] = None
    files_fixed: int = 0
    warnings_reviewed: int = 0
    duration_seconds: float = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "test_type": self.test_type,
            "passed": self.passed,
            "iterations": self.iterations,
            "final_result": asdict(self.final_result) if self.final_result else None,
            "error": self.error,
            "files_fixed": self.files_fixed,
            "warnings_reviewed": self.warnings_reviewed,
            "duration_seconds": self.duration_seconds,
        }


class RepairCycleStage(PipelineStage):
    """
    Deterministic test-fix-validate cycle stage.

    Orchestrates multiple agent invocations to iteratively fix test failures
    until all tests pass or max iterations reached.

    Attributes:
        name: Stage name
        test_configs: List of test type configurations to run
        agent_name: Agent to use for fixes (default: senior_software_engineer)
        max_total_agent_calls: Circuit breaker for cost control
        checkpoint_interval: Save state every N iterations
    """

    def __init__(
        self,
        name: str,
        test_configs: List[RepairTestRunConfig],
        agent_name: str = "senior_software_engineer",
        max_total_agent_calls: int = 100,  # Circuit breaker for cost control
        checkpoint_interval: int = 5,  # Checkpoint every N iterations
        **kwargs,
    ):
        super().__init__(name, **kwargs)
        self.test_configs = test_configs
        self.agent_name = agent_name
        self.max_total_agent_calls = max_total_agent_calls
        self.checkpoint_interval = checkpoint_interval
        self._agent_call_count = 0
        self._systemic_analysis_done_for: set = set()  # test types that have had systemic analysis run

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        """
        Sanitize a filename for use in Docker volume mounts and file paths.

        Replaces all characters that could cause issues with underscores.
        This is critical for Docker volume mounts which interpret colons (:) as
        mount separators, causing "too many colons" errors.

        Args:
            filename: The filename to sanitize (may include path separators and line numbers)

        Returns:
            Sanitized filename safe for use in Docker mounts and filesystem paths

        Example:
            >>> RepairCycleStage._sanitize_filename("task_manager.py:269")
            'task_manager_py_269'
        """
        # Replace Docker mount separators and other problematic characters
        # Critical: colons (:) are interpreted by Docker as volume mount separators
        problematic_chars = ['/', '.', ':', ' ', '\\', '*', '?', '"', '<', '>', '|']
        for char in problematic_chars:
            filename = filename.replace(char, '_')
        return filename

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute test-fix cycles for each configured test type.

        Returns:
            Dict with test_results (list), overall_success (bool), and metrics
        """
        logger.info(f"Starting repair cycle stage: {self.name}")
        start_time = utc_now()

        # Get observability manager and emit lifecycle events
        obs = context.get("observability")
        project = context.get("project", "unknown")
        task_id = context.get("task_id", f"repair_cycle_{self.name}")
        pipeline_run_id = context.get("pipeline_run_id")

        # Emit task received event (repair cycle as a composite agent)
        if obs:
            obs.emit_task_received("repair_cycle", task_id, project, context, pipeline_run_id)

            # Emit agent initialized with repair cycle config
            obs.emit_agent_initialized(
                "repair_cycle",
                task_id,
                project,
                {
                    "agent_name": self.agent_name,
                    "test_types": [tc.test_type for tc in self.test_configs],
                    "max_total_agent_calls": self.max_total_agent_calls,
                },
                pipeline_run_id=pipeline_run_id,
            )

        results = []
        error_message = None

        try:
            # Track which test type in sequence (1st, 2nd, 3rd)
            for test_type_index, test_config in enumerate(self.test_configs, start=1):
                logger.info(
                    f"Starting {test_config.test_type} test cycle "
                    f"(test type {test_type_index}/{len(self.test_configs)})"
                )

                cycle_start = utc_now()
                cycle_result = await self._run_test_cycle(test_config, context, test_type_index)
                cycle_end = utc_now()

                cycle_result.duration_seconds = (cycle_end - cycle_start).total_seconds()
                results.append(cycle_result)

                # Emit test cycle completed event (always emit, regardless of outcome)
                if obs:
                    obs.emit(
                        EventType.REPAIR_CYCLE_TEST_CYCLE_COMPLETED,
                        "repair_cycle",
                        task_id,
                        project,
                        {
                            "test_type": test_config.test_type,
                            "test_type_index": test_type_index,
                            "passed": 1 if cycle_result.passed else 0,  # Convert bool to int for ES schema
                            "test_cycle_iterations": cycle_result.iterations,
                            "files_fixed": cycle_result.files_fixed,
                            "warnings_reviewed": cycle_result.warnings_reviewed,
                            "error": cycle_result.error,
                            "duration_seconds": cycle_result.duration_seconds,
                        },
                        pipeline_run_id=pipeline_run_id,
                    )

                # Emit observability metrics
                self._emit_cycle_metrics(cycle_result, context)

                if not cycle_result.passed:
                    # Fast-fail: if unit tests fail, don't run integration
                    error_message = (
                        f"{test_config.test_type} tests failed after {cycle_result.iterations} iterations"
                    )
                    logger.error(error_message)
                    break

            end_time = utc_now()
            duration = (end_time - start_time).total_seconds()
            duration_ms = duration * 1000

            overall_success = all(r.passed for r in results)

            # Emit agent completed event
            if obs:
                obs.emit_agent_completed(
                    "repair_cycle",
                    task_id,
                    project,
                    duration_ms,
                    overall_success,
                    error=error_message if not overall_success else None,
                    pipeline_run_id=pipeline_run_id,
                    output="Cycle completed successfully" if overall_success else "Cycle completed with failures",
                )

            return {
                "stage": self.name,
                "test_results": [r.to_dict() for r in results],
                "overall_success": overall_success,
                "total_agent_calls": self._agent_call_count,
                "duration_seconds": duration,
                "timestamp": to_utc_isoformat(end_time),
            }

        except Exception as e:
            # Emit agent failed event on exception
            end_time = utc_now()
            duration = (end_time - start_time).total_seconds()
            duration_ms = duration * 1000

            if obs:
                obs.emit_agent_completed(
                    "repair_cycle", task_id, project, duration_ms, False, error=str(e), pipeline_run_id=pipeline_run_id, output="Cycle failed due to exception"
                )

            raise

    async def _run_test_cycle(
        self, config: RepairTestRunConfig, context: Dict[str, Any], test_type_index: int
    ) -> CycleResult:
        """
        Run a complete test-fix cycle for one test type.

        Args:
            config: Test configuration for this test type
            context: Pipeline context
            test_type_index: Which test type in sequence (1=first, 2=second, etc.)

        Workflow:
            1. Run tests
            2. If failures: group by file → fix → repeat
            3. If warnings (and review_warnings=True): group by file → review/fix
            4. Final validation

        Returns:
            CycleResult with success status and metrics
        """
        # Emit test cycle started event
        obs = context.get("observability")
        project = context.get("project", "unknown")
        task_id = context.get("task_id", f"repair_cycle_{self.name}")
        pipeline_run_id = context.get("pipeline_run_id")
        
        if obs:
            obs.emit(
                EventType.REPAIR_CYCLE_TEST_CYCLE_STARTED,
                "repair_cycle",
                task_id,
                project,
                {
                    "test_type": config.test_type,
                    "test_type_index": test_type_index,  # Which test type in sequence
                    "total_test_types": len(self.test_configs),  # Total test types to run
                    "max_iterations": config.max_iterations,
                    "review_warnings": config.review_warnings,
                    "timeout": config.timeout,
                },
                pipeline_run_id=pipeline_run_id,
            )
        
        # Test cycle iteration counter (resets for each test type)
        test_cycle_iteration = 0
        files_fixed = 0
        warnings_reviewed = 0

        from services.cancellation import get_cancellation_signal
        issue_number = context.get('issue_number')

        while test_cycle_iteration < config.max_iterations:
            # Check for cancellation before starting new iteration
            if issue_number and get_cancellation_signal().is_cancelled(project, issue_number):
                logger.warning(f"Pipeline run cancelled for {project}/#{issue_number}, stopping repair cycle")
                return CycleResult(
                    test_type=config.test_type, passed=False,
                    iterations=test_cycle_iteration, final_result=None,
                    error="Pipeline run ended externally",
                    files_fixed=files_fixed, warnings_reviewed=warnings_reviewed,
                )

            test_cycle_iteration += 1
            logger.info(
                f"Test cycle iteration {test_cycle_iteration}/{config.max_iterations} "
                f"for {config.test_type} (test type {test_type_index}/{len(self.test_configs)})"
            )

            # Emit iteration started event
            if obs:
                obs.emit(
                    EventType.REPAIR_CYCLE_ITERATION,
                    "repair_cycle",
                    task_id,
                    project,
                    {
                        "test_type": config.test_type,
                        "test_type_index": test_type_index,  # Which test type in sequence
                        "test_cycle_iteration": test_cycle_iteration,  # Iteration within this test type
                        "max_test_cycle_iterations": config.max_iterations,
                        "files_fixed_so_far": files_fixed,
                        "warnings_reviewed_so_far": warnings_reviewed,
                    },
                    pipeline_run_id=pipeline_run_id,
                )

            # Check circuit breaker
            if self._agent_call_count >= self.max_total_agent_calls:
                logger.error(f"Circuit breaker triggered: max agent calls " f"({self.max_total_agent_calls}) reached")
                # Completion event is emitted by the caller (execute())
                return CycleResult(
                    test_type=config.test_type,
                    passed=False,
                    iterations=test_cycle_iteration,
                    final_result=None,
                    error="Circuit breaker: max agent calls reached",
                    files_fixed=files_fixed,
                    warnings_reviewed=warnings_reviewed,
                )

            # Step 1: Run tests (containerized agent)
            test_result = await self._run_tests(config, context, test_cycle_iteration, test_type_index)
            await self._checkpoint(config.test_type, test_cycle_iteration, context)

            # Check for infrastructure failures (indicated by __infrastructure__ file)
            infrastructure_failures = [f for f in test_result.failures if f.file == "__infrastructure__"]
            if infrastructure_failures:
                logger.error(
                    f"Infrastructure failure detected in test execution: " f"{infrastructure_failures[0].message}"
                )
                
                # Emit test cycle completed event with infrastructure failure
                if obs:
                    obs.emit(
                        EventType.REPAIR_CYCLE_TEST_CYCLE_COMPLETED,
                        "repair_cycle",
                        task_id,
                        project,
                        {
                            "test_type": config.test_type,
                            "test_type_index": test_type_index,
                            "passed": 0,  # Convert bool to int for ES schema
                            "test_cycle_iterations": test_cycle_iteration,
                            "error": f"Infrastructure failure: {infrastructure_failures[0].message}",
                        },
                        pipeline_run_id=pipeline_run_id,
                    )
                
                # Emit cycle completed event with infrastructure failure
                if obs:
                    obs.emit(
                        EventType.REPAIR_CYCLE_COMPLETED,
                        "repair_cycle",
                        task_id,
                        project,
                        {
                            "test_type": config.test_type,
                            "test_type_index": test_type_index,
                            "passed": 0,  # Convert bool to int for ES schema
                            "test_cycle_iterations": test_cycle_iteration,
                            "error": f"Infrastructure failure: {infrastructure_failures[0].message}",
                        },
                        pipeline_run_id=pipeline_run_id,
                    )
                
                return CycleResult(
                    test_type=config.test_type,
                    passed=False,
                    iterations=test_cycle_iteration,
                    final_result=test_result,
                    error=f"Infrastructure failure: {infrastructure_failures[0].message}",
                    files_fixed=files_fixed,
                    warnings_reviewed=warnings_reviewed,
                )

            # Step 2: Check for failures
            if not test_result.has_failures():
                logger.info(f"No test failures in test cycle iteration {test_cycle_iteration}")

                # Step 3: Handle warnings if configured
                if config.review_warnings and test_result.has_warnings():
                    logger.info(f"Reviewing {test_result.warnings} warnings")
                    warnings_fixed = await self._handle_warnings(test_result, config, context)
                    warnings_reviewed += warnings_fixed

                    # Re-run tests after fixing warnings
                    logger.info("Re-running tests after warning fixes")
                    test_result = await self._run_tests(config, context, test_cycle_iteration, test_type_index)

                    # If warning fixes broke tests, continue loop
                    if test_result.has_failures():
                        logger.warning("Warning fixes introduced test failures, continuing loop")
                        continue

                # Success!
                logger.info(f"Test cycle completed successfully after {test_cycle_iteration} iterations")
                
                # Note: Completion event will be emitted by caller
                return CycleResult(
                    test_type=config.test_type,
                    passed=True,
                    iterations=test_cycle_iteration,
                    final_result=test_result,
                    files_fixed=files_fixed,
                    warnings_reviewed=warnings_reviewed,
                )

            # Step 4: Group failures by file (direct Claude parse)
            grouped_failures = test_result.group_failures_by_file()
            logger.info(f"Found failures in {len(grouped_failures)} files: " f"{list(grouped_failures.keys())}")

            # --- SYSTEMIC ANALYSIS BLOCK ---
            # Run once per test type — the first time we encounter failures for this type.
            # Subsequent iterations skip this block (the set guards against repeated analysis).
            systemic_fix_ran_this_iteration = False
            if config.test_type not in self._systemic_analysis_done_for:
                self._systemic_analysis_done_for.add(config.test_type)
                analysis = await self._analyze_systemic_failures(
                    test_result, grouped_failures, config, context
                )

                if analysis.has_env_issues:
                    test_result = await self._run_env_rebuild_sub_cycle(
                        analysis, config, context, test_cycle_iteration, test_type_index
                    )
                    # Guard against infrastructure/env sentinels returned from the sub-cycle
                    env_rebuild_infra = [
                        f for f in test_result.failures
                        if f.file in ("__env__", "__infrastructure__")
                    ]
                    if env_rebuild_infra:
                        error_msg = f"Infrastructure failure in env rebuild: {env_rebuild_infra[0].message}"
                        logger.error(error_msg)
                        if obs:
                            obs.emit(
                                EventType.REPAIR_CYCLE_TEST_CYCLE_COMPLETED,
                                "repair_cycle", task_id, project,
                                {
                                    "test_type": config.test_type,
                                    "test_type_index": test_type_index,
                                    "passed": 0,
                                    "test_cycle_iterations": test_cycle_iteration,
                                    "error": error_msg,
                                },
                                pipeline_run_id=pipeline_run_id,
                            )
                            obs.emit(
                                EventType.REPAIR_CYCLE_COMPLETED,
                                "repair_cycle", task_id, project,
                                {
                                    "test_type": config.test_type,
                                    "test_type_index": test_type_index,
                                    "passed": 0,
                                    "test_cycle_iterations": test_cycle_iteration,
                                    "error": error_msg,
                                },
                                pipeline_run_id=pipeline_run_id,
                            )
                        return CycleResult(
                            test_type=config.test_type,
                            passed=False,
                            iterations=test_cycle_iteration,
                            final_result=test_result,
                            error=error_msg,
                            files_fixed=files_fixed,
                            warnings_reviewed=warnings_reviewed,
                        )
                    if not test_result.has_failures():
                        continue  # Back to top of test cycle loop (success path)
                    grouped_failures = test_result.group_failures_by_file()

                if analysis.has_systemic_code_issues:
                    test_result = await self._run_systemic_fix_sub_cycle(
                        analysis, test_result, grouped_failures, config, context,
                        test_cycle_iteration, test_type_index,
                    )
                    systemic_fix_ran_this_iteration = True
                    # Guard against infrastructure failures returned from the sub-cycle
                    sub_cycle_infra = [f for f in test_result.failures if f.file == "__infrastructure__"]
                    if sub_cycle_infra:
                        error_msg = f"Infrastructure failure in systemic sub-cycle: {sub_cycle_infra[0].message}"
                        logger.error(error_msg)
                        if obs:
                            obs.emit(
                                EventType.REPAIR_CYCLE_TEST_CYCLE_COMPLETED,
                                "repair_cycle", task_id, project,
                                {
                                    "test_type": config.test_type,
                                    "test_type_index": test_type_index,
                                    "passed": 0,
                                    "test_cycle_iterations": test_cycle_iteration,
                                    "error": error_msg,
                                },
                                pipeline_run_id=pipeline_run_id,
                            )
                            obs.emit(
                                EventType.REPAIR_CYCLE_COMPLETED,
                                "repair_cycle", task_id, project,
                                {
                                    "test_type": config.test_type,
                                    "test_type_index": test_type_index,
                                    "passed": 0,
                                    "test_cycle_iterations": test_cycle_iteration,
                                    "error": error_msg,
                                },
                                pipeline_run_id=pipeline_run_id,
                            )
                        return CycleResult(
                            test_type=config.test_type,
                            passed=False,
                            iterations=test_cycle_iteration,
                            final_result=test_result,
                            error=error_msg,
                            files_fixed=files_fixed,
                            warnings_reviewed=warnings_reviewed,
                        )
                    if not test_result.has_failures():
                        continue  # Back to top of test cycle loop (success path)
                    grouped_failures = test_result.group_failures_by_file()
            # --- END SYSTEMIC ANALYSIS BLOCK ---

            # --- THRESHOLD SYSTEMIC FIX ---
            # When failure count exceeds the threshold, dispatch the systemic fix agent
            # with a broad diagnostic prompt rather than grinding through per-file fixes.
            # Runs on any iteration where the threshold is met, unless the analysis block
            # already ran a systemic fix this iteration (which would be redundant).
            if (
                not systemic_fix_ran_this_iteration
                and grouped_failures
                and test_result.failed >= config.systemic_analysis_threshold
            ):
                threshold_analysis = SystemicAnalysisResult(
                    has_env_issues=False,
                    env_issue_description="",
                    has_systemic_code_issues=True,
                    systemic_issue_description="",  # digest built from live failure data
                    affected_files=[],
                    raw_json={},
                )
                test_result = await self._run_systemic_fix_sub_cycle(
                    threshold_analysis, test_result, grouped_failures, config, context,
                    test_cycle_iteration, test_type_index,
                )
                # Guard against infrastructure failures returned from the sub-cycle
                threshold_infra = [f for f in test_result.failures if f.file == "__infrastructure__"]
                if threshold_infra:
                    error_msg = f"Infrastructure failure in threshold systemic sub-cycle: {threshold_infra[0].message}"
                    logger.error(error_msg)
                    if obs:
                        obs.emit(
                            EventType.REPAIR_CYCLE_TEST_CYCLE_COMPLETED,
                            "repair_cycle", task_id, project,
                            {
                                "test_type": config.test_type,
                                "test_type_index": test_type_index,
                                "passed": 0,
                                "test_cycle_iterations": test_cycle_iteration,
                                "error": error_msg,
                            },
                            pipeline_run_id=pipeline_run_id,
                        )
                        obs.emit(
                            EventType.REPAIR_CYCLE_COMPLETED,
                            "repair_cycle", task_id, project,
                            {
                                "test_type": config.test_type,
                                "test_type_index": test_type_index,
                                "passed": 0,
                                "test_cycle_iterations": test_cycle_iteration,
                                "error": error_msg,
                            },
                            pipeline_run_id=pipeline_run_id,
                        )
                    return CycleResult(
                        test_type=config.test_type,
                        passed=False,
                        iterations=test_cycle_iteration,
                        final_result=test_result,
                        error=error_msg,
                        files_fixed=files_fixed,
                        warnings_reviewed=warnings_reviewed,
                    )
                if not test_result.has_failures():
                    continue  # Back to top of test cycle loop (success path)
                grouped_failures = test_result.group_failures_by_file()
            # --- END THRESHOLD SYSTEMIC FIX ---

            # Emit fix cycle started event
            if obs:
                obs.emit(
                    EventType.REPAIR_CYCLE_FIX_CYCLE_STARTED,
                    "repair_cycle",
                    task_id,
                    project,
                    {
                        "test_type": config.test_type,
                        "test_type_index": test_type_index,  # Which test type in sequence
                        "test_cycle_iteration": test_cycle_iteration,  # Which iteration of this test type
                        "file_count": len(grouped_failures),
                        "total_failures": len(test_result.failures),
                    },
                    pipeline_run_id=pipeline_run_id,
                )

            # Step 5: Fix each file (containerized agent)
            fixed_count = await self._fix_failures_by_file(grouped_failures, config, context)
            files_fixed += fixed_count

            # Emit fix cycle completed event
            if obs:
                obs.emit(
                    EventType.REPAIR_CYCLE_FIX_CYCLE_COMPLETED,
                    "repair_cycle",
                    task_id,
                    project,
                    {
                        "test_type": config.test_type,
                        "test_type_index": test_type_index,  # Which test type in sequence
                        "test_cycle_iteration": test_cycle_iteration,  # Which iteration of this test type
                        "files_fixed": fixed_count,
                        "total_files_fixed": files_fixed,
                    },
                    pipeline_run_id=pipeline_run_id,
                )

            await self._checkpoint(config.test_type, test_cycle_iteration, context)

        # Max iterations reached
        logger.error(f"Max iterations ({config.max_iterations}) reached for " f"{config.test_type} tests")
        final_result = await self._run_tests(config, context, test_cycle_iteration, test_type_index)

        # Check if tests actually passed, regardless of warnings
        # If all tests pass but warnings remain, that's still success
        tests_passed = not final_result.has_failures()

        if tests_passed:
            logger.info(f"Max iterations reached, but all tests passed. Success! (warnings may remain)")
        else:
            logger.error(f"Max iterations reached with {final_result.failed} test failures")

        # Note: Completion event will be emitted by caller
        return CycleResult(
            test_type=config.test_type,
            passed=tests_passed,  # Based on test results, not iterations
            iterations=test_cycle_iteration,
            final_result=final_result,
            error="Max iterations reached" if not tests_passed else None,
            files_fixed=files_fixed,
            warnings_reviewed=warnings_reviewed,
        )

    async def _run_tests(self, config: RepairTestRunConfig, context: Dict[str, Any], test_cycle_iteration: int, test_type_index: int
    ) -> RepairTestResult:
        """
        Run tests and return structured results.

        Uses centralized AgentExecutor to run agent with full observability.
        Implements retry logic for infrastructure failures (e.g., JSON parsing issues).

        Args:
            config: Test configuration
            context: Pipeline context
            iteration: Current iteration number

        Returns:
            RepairTestResult with parsed test output
        """
        logger.info(f"Running {config.test_type} tests (test cycle iteration {test_cycle_iteration}/{config.max_iterations}, test type {test_type_index})")

        # Get observability manager
        obs = context.get("observability")
        project = context.get("project", "unknown")
        task_id = context.get("task_id", f"repair_cycle_{self.name}")
        pipeline_run_id = context.get("pipeline_run_id")

        # Emit test execution started event
        if obs:
            obs.emit(
                EventType.REPAIR_CYCLE_TEST_EXECUTION_STARTED,
                "repair_cycle_test",
                f"{task_id}_test_iter{test_cycle_iteration}",
                project,
                {
                    "test_type": config.test_type,
                    "test_type_index": test_type_index,  # Which test type in sequence
                    "test_cycle_iteration": test_cycle_iteration,  # Which iteration of this test type
                    "max_test_cycle_iterations": config.max_iterations,
                    "timeout": config.timeout,
                },
                pipeline_run_id=pipeline_run_id,
            )

        # Get AgentExecutor
        from services.agent_executor import get_agent_executor

        agent_executor = get_agent_executor()

        # Get project info
        project = context.get("project", "unknown")
        pipeline_run_id = context.get("pipeline_run_id")

        # Build the appropriate prompt for this test type
        if config.test_type == "compilation":
            direct_prompt = """Ensure all code in this project passes compilation and linting checks.

Your goal is to identify and report compilation errors and linting violations so they can be fixed before running tests.

1. Identify the project's tech stack by inspecting config files (package.json, pyproject.toml, tsconfig.json, setup.py, etc.)

2. Run the appropriate compilation and linting tools:
   - **TypeScript/JavaScript**: `npx tsc --noEmit` (or the configured tsconfig) and ESLint if configured
   - **Python**:
     a. First auto-fix: `ruff check --fix --unsafe-fixes .` (removes unused variables, fixes comparison style, etc.)
     b. Then check remaining: `ruff check .` — report only violations that require manual fixes
     c. Type check: `mypy src/` (or `mypy .` if no src/ layout)
   - **Other languages**: use the project's configured build/lint toolchain

3. Save the full output to /tmp/compilation_results.txt for reference.

4. Return structured results. Each distinct error is a separate failure entry. Use the source file as "file", the error code or rule as "test", and the error message as "message".""" + _TEST_OUTPUT_FORMAT

        elif config.test_type == "pre-commit":
            direct_prompt = """Run the pre-commit scripts for this project and report any failures.

1. Identify how pre-commit is configured by inspecting the project root (e.g. .pre-commit-config.yaml, package.json scripts, Makefile targets named "pre-commit").

2. Run the pre-commit scripts against all files:
   - If using the `pre-commit` tool: `pre-commit run --all-files`
   - If using a npm/package.json script: `npm run pre-commit` (or the configured script name)
   - If using a Makefile target: `make pre-commit`

3. Save the full output to /tmp/pre_commit_results.txt for reference.

4. Return structured results. Each distinct hook failure is a separate entry. Use the hook name as "test" and the file it failed on (if reported) as "file"; use the hook name as "file" if no specific file is identified.""" + _TEST_OUTPUT_FORMAT

        elif config.test_type == "ci":
            # Calculate a safe polling deadline from the configured timeout, leaving
            # buffer for the JSON response. Agent needs to know when to stop gracefully.
            poll_deadline_minutes = max(5, (config.timeout - 120) // 60)
            direct_prompt = f"""Check whether CI (continuous integration) is passing for this project's current branch.

You have approximately {poll_deadline_minutes} minutes to complete this check before your session times out.
If CI has not completed within that window, return a failure result indicating CI is still running.

Follow these steps exactly:

0. Check whether CI is configured for this project:
   Look for CI configuration files (e.g. `.github/workflows/`, `.circleci/`, `.travis.yml`, `Jenkinsfile`, `azure-pipelines.yml`).
   If none exist, CI is not set up for this project — return success immediately:
   {{"passed": 1, "failed": 0, "warnings": 0, "failures": [], "warning_list": []}}

1. Verify the current branch is a feature branch (not main/master/develop):
   `git rev-parse --abbrev-ref HEAD`
   If the branch is main, master, or develop — stop immediately and return:
   {{"passed": 0, "failed": 1, "warnings": 0, "failures": [{{"file": "git", "test": "branch-check", "message": "CI test type should not run on default branch"}}], "warning_list": []}}

2. Check whether there are local commits not yet pushed to origin:
   `git status -sb`
   If there are unpushed commits, attempt to push:
   `git push origin HEAD`
   If the push succeeds, proceed to step 3.
   If the push fails due to authentication/network issues, do NOT treat this as a CI failure.
   Instead, check if any CI run exists for the current commit SHA that was already pushed earlier:
   `git rev-parse origin/$(git rev-parse --abbrev-ref HEAD)` to get the last pushed SHA
   Then query for CI runs on that commit. If the most recent run for the pushed commit is a success, return success.
   If there is no successful run and push failed, return:
   {{"passed": 0, "failed": 1, "warnings": 0, "failures": [{{"file": "git", "test": "push", "message": "Cannot push unpushed commits — authentication failed. CI cannot be verified against latest code."}}], "warning_list": []}}

3. Wait for the most recent CI run to complete:
   `gh run list --limit 1 --branch $(git rev-parse --abbrev-ref HEAD) --json databaseId,status,conclusion,workflowName`
   Poll every 30 seconds until `status` is `completed`.
   **IMPORTANT**: Only check the CI run that was triggered by the push in step 2 (or the most recent one if already up-to-date).
   Do NOT report a stale CI run that predates the current commit as the result.
   Verify the run's head SHA matches the current commit: `gh run view <run_id> --json headSha`
   If no CI run appears within 3 minutes of pushing, return:
   {{"passed": 0, "failed": 1, "warnings": 0, "failures": [{{"file": "ci", "test": "trigger", "message": "No CI run triggered within 3 minutes of push — CI may not be configured for this branch"}}], "warning_list": []}}
   If CI has still not completed when you are within 2 minutes of your deadline, stop polling and return:
   {{"passed": 0, "failed": 1, "warnings": 0, "failures": [{{"file": "ci", "test": "timeout", "message": "CI run did not complete within the allotted time ({poll_deadline_minutes} minutes)"}}], "warning_list": []}}

4. Once completed, check the conclusion. If `conclusion` is `success`, CI passed — return the passing result.

5. If CI failed, retrieve failure details:
   `gh run view <run_id> --log-failed`
   Save the full output to /tmp/ci_failures.txt for reference.

6. Parse the failures into the structured format below. Each CI job failure is a separate entry.
   Use the source file path from the log as "file" (or the job/workflow name if no file is identifiable).
   Use the job name and step name as "test".""" + _TEST_OUTPUT_FORMAT

        elif config.test_type == "storybook":
            direct_prompt = """Run the Storybook story tests for this project.

1. Check if Storybook is configured by looking for storybook-related scripts in package.json.
   If Storybook is not configured, return success immediately:
   {"passed": 1, "failed": 0, "warnings": 0, "failures": [], "warning_list": []}

2. Run the full Storybook test suite using the all-in-one script:
   `npm run test:storybook:full`
   This script builds Storybook, serves it locally on port 61001, waits for the server to be ready,
   and runs all story tests (including accessibility checks via axe-core).

3. Save the full output to /tmp/storybook_results.txt for reference.

4. Return structured results. Each failing story test is a separate failure entry.
   Use the story file path as "file", the story/test name as "test", and the error message as "message".""" + _TEST_OUTPUT_FORMAT

        else:
            direct_prompt = f"""Run all {config.test_type} tests for this project.

Please identify the appropriate test framework and location for {config.test_type} tests.

**IMPORTANT**: When running tests, save the test results to a file in /tmp so that you can refer back to them and so they're not made part of the codebase.

These tests runs can take some time to complete, please be patient and don't put time limits on the test execution. Make sure you're capturing the information you need to identify failures without having to re-run the tests multiple times.

Also, make sure you are capturing the **full** output of the test runs, including any warnings and errors, to disk to avoid having to re-run the tests multiple times.

Be mindful of environment setup steps like installing dependencies and activating virtual environments.""" + _TEST_OUTPUT_FORMAT

        # Build task context for this agent execution
        task_context = {
            **context,
            "pipeline_run_id": pipeline_run_id,  # Ensure pipeline_run_id flows through
            "task_description": f"Run {config.test_type} tests (test cycle iteration {test_cycle_iteration})",
            "timeout": config.timeout,
            # Skip workspace preparation - repair cycle already runs in prepared workspace
            "skip_workspace_prep": True,
            # Clear review_cycle context to avoid iteration count confusion from previous review cycles
            "review_cycle": None,
            # Provide the custom instructions as a 'direct_prompt' so the agent can use it
            "direct_prompt": direct_prompt,
        }

        # Retry logic for infrastructure failures (e.g., JSON parsing)
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                # Increment agent call counter
                self._agent_call_count += 1

                # Record execution start for state tracking
                if 'issue_number' in task_context and 'column' in task_context:
                    from services.work_execution_state import work_execution_tracker
                    work_execution_tracker.record_execution_start(
                        issue_number=task_context['issue_number'],
                        column=task_context['column'],
                        agent=self.agent_name,
                        trigger_source='repair_cycle_test',
                        project_name=project
                    )

                # Execute agent through centralized executor with full observability
                result = await agent_executor.execute_agent(
                    agent_name=self.agent_name,
                    project_name=project,
                    task_context=task_context,
                    execution_type="repair_test",
                )

                # Extract result text from agent output
                # Agent returns a context dict with markdown_analysis or raw_analysis_result
                result_text = result.get("markdown_analysis") or result.get("raw_analysis_result")
                
                if not result_text or not result_text.strip():
                    logger.error(f"Agent returned result with keys: {list(result.keys())}")
                    raise ValueError("Agent returned empty output")

                # Parse JSON result - this will raise ValueError if parsing fails
                result_json = self._extract_json_from_response(result_text)

                # Log the parsed JSON structure for debugging
                logger.info(
                    f"Parsed test result JSON keys: {list(result_json.keys())}, "
                    f"warnings type: {type(result_json.get('warnings'))}, "
                    f"warning_list type: {type(result_json.get('warning_list'))}"
                )

                # Successfully parsed - convert to RepairTestResult
                # Handle failures list
                failures_data = result_json.get("failures", [])
                if not isinstance(failures_data, list):
                    logger.warning(f"Expected 'failures' to be a list, got {type(failures_data)}, using empty list")
                    failures_data = []
                failures = [RepairTestFailure(**f) for f in failures_data]

                # Handle warning_list - support both 'warning_list' and 'warnings' (as list)
                warning_list_data = result_json.get("warning_list")
                if warning_list_data is None:
                    # Fallback to 'warnings' field if it's a list
                    warnings_field = result_json.get("warnings")
                    if isinstance(warnings_field, list):
                        logger.info("Using 'warnings' field as list (legacy format)")
                        warning_list_data = warnings_field
                    else:
                        warning_list_data = []
                
                if not isinstance(warning_list_data, list):
                    logger.warning(
                        f"Expected 'warning_list' to be a list, got {type(warning_list_data)}, using empty list"
                    )
                    warning_list_data = []
                
                warning_list = [RepairTestWarning(**w) for w in warning_list_data]

                # Get warning count - prefer explicit count, fallback to list length
                warnings_count = result_json.get("warnings")
                if isinstance(warnings_count, int):
                    warnings_count = warnings_count
                else:
                    warnings_count = len(warning_list)
                    logger.info(f"Warning count not provided as int, using list length: {warnings_count}")

                test_result = RepairTestResult(
                    test_type=config.test_type,
                    iteration=test_cycle_iteration,
                    passed=result_json.get("passed", 0),
                    failed=result_json.get("failed", 0),
                    warnings=warnings_count,
                    failures=failures,
                    warning_list=warning_list,
                    raw_output=result_text,
                    timestamp=utc_isoformat(),
                )
                
                # Emit test execution completed event
                if obs:
                    obs.emit(
                        EventType.REPAIR_CYCLE_TEST_EXECUTION_COMPLETED,
                        "repair_cycle_test",
                        f"{task_id}_test_iter{test_cycle_iteration}",
                        project,
                        {
                            "test_type": config.test_type,
                            "test_type_index": test_type_index,  # Which test type in sequence
                            "test_cycle_iteration": test_cycle_iteration,  # Which iteration of this test type
                            "max_test_cycle_iterations": config.max_iterations,
                            "passed": test_result.passed,
                            "failed": test_result.failed,
                            "warnings": test_result.warnings,
                            "has_failures": test_result.has_failures(),
                            "failures": [asdict(f) for f in test_result.failures],
                        },
                        pipeline_run_id=pipeline_run_id,
                    )
                
                return test_result

            except CancellationError:
                raise  # Never retry cancellations
            except ValueError as e:
                # JSON parsing failure - this is an infrastructure issue, not a test failure
                error_msg = str(e)
                logger.warning(
                    f"Failed to parse test results (attempt {attempt + 1}/{max_retries + 1}): {error_msg}"
                )
                
                if attempt < max_retries:
                    # Retry with more explicit instructions
                    logger.info(f"Retrying with enhanced instructions...")
                    task_context["direct_prompt"] += f"\n\nPREVIOUS ATTEMPT FAILED: {error_msg}\nPlease ensure you return ONLY the JSON object with no additional text."
                    continue
                else:
                    # Max retries reached - this is a critical failure
                    logger.error(
                        f"Failed to get valid JSON test results after {max_retries + 1} attempts. "
                        f"This is an infrastructure failure, not a test failure."
                    )
                    # Return a special result indicating infrastructure failure
                    return RepairTestResult(
                        test_type=config.test_type,
                        iteration=test_cycle_iteration,
                        passed=0,
                        failed=0,  # 0 instead of 999 to indicate infrastructure failure
                        warnings=0,
                        failures=[
                            RepairTestFailure(
                                file="__infrastructure__",
                                test="test_execution_json_parse",
                                message=f"INFRASTRUCTURE FAILURE: Agent failed to return valid JSON test results after {max_retries + 1} attempts: {error_msg}",
                            )
                        ],
                        warning_list=[],
                        raw_output=result_text if 'result_text' in locals() else str(e),
                        timestamp=utc_isoformat(),
                    )

            except Exception as e:
                # Other execution failure (timeout, container failure, etc.)
                logger.error(f"Test execution failed (attempt {attempt + 1}/{max_retries + 1}): {e}", exc_info=True)
                
                if attempt < max_retries:
                    logger.info("Retrying test execution...")
                    continue
                else:
                    # Max retries reached - return infrastructure failure
                    return RepairTestResult(
                        test_type=config.test_type,
                        iteration=test_cycle_iteration,
                        passed=0,
                        failed=0,
                        warnings=0,
                        failures=[
                            RepairTestFailure(
                                file="__infrastructure__",
                                test="test_execution_failure",
                                message=f"INFRASTRUCTURE FAILURE: Test execution failed after {max_retries + 1} attempts: {str(e)}",
                            )
                        ],
                        warning_list=[],
                        raw_output=str(e),
                        timestamp=utc_isoformat(),
                    )

        # Should never reach here, but just in case
        return RepairTestResult(
            test_type=config.test_type,
            iteration=test_cycle_iteration,
            passed=0,
            failed=0,
            warnings=0,
            failures=[
                RepairTestFailure(
                    file="__infrastructure__",
                    test="test_execution_unknown",
                    message="INFRASTRUCTURE FAILURE: Unexpected error in test execution retry loop",
                )
            ],
            warning_list=[],
            raw_output="Unknown error",
            timestamp=utc_isoformat(),
        )

    async def _fix_failures_by_file(
        self, grouped_failures: Dict[str, List[RepairTestFailure]], config: RepairTestRunConfig, context: Dict[str, Any]
    ) -> int:
        """
        Fix failures for each file using centralized AgentExecutor.

        Args:
            grouped_failures: Dict mapping test file to list of failures
            config: Test configuration
            context: Pipeline context

        Returns:
            Number of files where fixes were attempted
        """
        files_fixed = 0

        # Get observability manager
        obs = context.get("observability")
        project = context.get("project", "unknown")
        task_id = context.get("task_id", f"repair_cycle_{self.name}")
        pipeline_run_id = context.get("pipeline_run_id")

        # Get AgentExecutor
        from services.agent_executor import get_agent_executor

        agent_executor = get_agent_executor()

        for test_file, failures in grouped_failures.items():
            logger.info(f"Fixing {len(failures)} failures in {test_file}")

            # Emit file fix started event
            if obs:
                obs.emit(
                    EventType.REPAIR_CYCLE_FILE_FIX_STARTED,
                    "repair_cycle_fix",
                    f"{task_id}_fix_{self._sanitize_filename(test_file)}",
                    project,
                    {
                        "test_file": test_file,
                        "failure_count": len(failures),
                        "test_type": config.test_type,
                    },
                    pipeline_run_id=pipeline_run_id,
                )

            # Build failure list for prompt
            failure_messages = [f"- {f.test}: {f.message}" for f in failures]
            failure_text = "\n".join(failure_messages)

            # Build task context for this agent execution
            task_context = {
                **context,
                "pipeline_run_id": pipeline_run_id,  # Ensure pipeline_run_id flows through
                "task_description": f"Fix failures in {test_file}",
                "timeout": config.timeout,
                # Skip workspace preparation - repair cycle already runs in prepared workspace
                "skip_workspace_prep": True,
                # Clear review_cycle context to avoid iteration count confusion from previous review cycles
                "review_cycle": None,
                # Provide the fix instructions as a 'direct_prompt' so the agent can use it
                "direct_prompt": f"""Fix these test failures in {test_file}:

{failure_text}

**IMPORTANT**: For each failure:
- Identify the root cause in the source code, explore similar functionality elsewhere in the code to see how this is handled
- Evaluate if the test is still relevant and necessary, if it's not, remove it completely
- If the test is meant for functionality which is not implemented, remove the test entirely

**For formatter/linter failures** (e.g. "would reformat", "reformatted", style violation):
- Always run the project's formatter on the file — do NOT manually reformat lines or adjust indentation by hand; formatters apply rules that hand-edits rarely satisfy exactly.
- Discover the project's formatter from its config files (pyproject.toml, package.json, .prettierrc, .eslintrc, etc.).
- Run the formatter from the **project root directory** so it picks up the project's own config (line length, rules, etc.).
  - Python: `black <file>` (reads pyproject.toml/setup.cfg), `ruff check --fix <file>` then `ruff check <file>`
  - JavaScript/TypeScript: `npx prettier --write <file>`, `npx eslint --fix <file>`
  - Go: `gofmt -w <file>`
  - Rust: `cargo fmt`
- After running the formatter, confirm the check passes (e.g. `black --check <file>`, `npx prettier --check <file>`) before committing.
""",
            }

            # Increment agent call counter
            self._agent_call_count += 1

            # Record execution start for state tracking
            if 'issue_number' in task_context and 'column' in task_context:
                from services.work_execution_state import work_execution_tracker
                work_execution_tracker.record_execution_start(
                    issue_number=task_context['issue_number'],
                    column=task_context['column'],
                    agent=self.agent_name,
                    trigger_source='repair_cycle_fix',
                    project_name=project
                )

            # Execute agent through centralized executor with full observability
            try:
                result = await agent_executor.execute_agent(
                    agent_name=self.agent_name,
                    project_name=project,
                    task_context=task_context,
                    execution_type="repair_fix",
                )

                logger.info(f"Fixed failures in {test_file}")
                files_fixed += 1
                
                # Emit file fix completed event
                if obs:
                    obs.emit(
                        EventType.REPAIR_CYCLE_FILE_FIX_COMPLETED,
                        "repair_cycle_fix",
                        f"{task_id}_fix_{self._sanitize_filename(test_file)}",
                        project,
                        {
                            "test_file": test_file,
                            "failure_count": len(failures),
                            "test_type": config.test_type,
                            "success": True,
                        },
                        pipeline_run_id=pipeline_run_id,
                    )

            except CancellationError:
                raise  # Never retry cancellations
            except Exception as e:
                logger.error(f"Failed to fix failures in {test_file}: {e}", exc_info=True)
                
                # Emit file fix failed event
                if obs:
                    obs.emit(
                        EventType.REPAIR_CYCLE_FILE_FIX_FAILED,
                        "repair_cycle_fix",
                        f"{task_id}_fix_{self._sanitize_filename(test_file)}",
                        project,
                        {
                            "test_file": test_file,
                            "failure_count": len(failures),
                            "test_type": config.test_type,
                            "error": str(e),
                        },
                        pipeline_run_id=pipeline_run_id,
                    )

        return files_fixed

    async def _handle_warnings(
        self, test_result: RepairTestResult, config: RepairTestRunConfig, context: Dict[str, Any]
    ) -> int:
        """
        Review and fix unexpected warnings using centralized AgentExecutor.

        Args:
            test_result: Test result with warnings
            config: Test configuration
            context: Pipeline context

        Returns:
            Number of warning files reviewed
        """
        grouped_warnings = test_result.group_warnings_by_file()
        warnings_reviewed = 0

        # Get observability manager
        obs = context.get("observability")
        project = context.get("project", "unknown")
        task_id = context.get("task_id", f"repair_cycle_{self.name}")
        pipeline_run_id = context.get("pipeline_run_id")

        # Get AgentExecutor
        from services.agent_executor import get_agent_executor

        agent_executor = get_agent_executor()

        for source_file, warnings in grouped_warnings.items():
            logger.info(f"Reviewing {len(warnings)} warnings in {source_file}")

            # Emit warning review started event
            if obs:
                obs.emit(
                    EventType.REPAIR_CYCLE_WARNING_REVIEW_STARTED,
                    "repair_cycle_warnings",
                    f"{task_id}_warn_{self._sanitize_filename(source_file)}",
                    project,
                    {
                        "source_file": source_file,
                        "warning_count": len(warnings),
                        "test_type": config.test_type,
                        "warnings": [asdict(w) for w in warnings],
                    },
                    pipeline_run_id=pipeline_run_id,
                )

            # Build warning list for prompt
            warning_messages = [f"- {w.message}" for w in warnings]
            warning_text = "\n".join(warning_messages)

            # Build task context for this agent execution
            task_context = {
                **context,
                "pipeline_run_id": pipeline_run_id,  # Ensure pipeline_run_id flows through
                "task_description": f"Review warnings in {source_file}",
                "timeout": config.timeout,
                # Skip workspace preparation - repair cycle already runs in prepared workspace
                "skip_workspace_prep": True,
                # Clear review_cycle context to avoid iteration count confusion from previous review cycles
                "review_cycle": None,
                # Provide the review instructions as a 'direct_prompt' so the agent can use it
                "direct_prompt": f"""Review these warnings from a run of {source_file}:

{warning_text}

For each warning:
- Determine if it's expected in this context
- If not expected, fix the underlying issue
""",
            }

            # Increment agent call counter
            self._agent_call_count += 1

            # Record execution start for state tracking
            if 'issue_number' in task_context and 'column' in task_context:
                from services.work_execution_state import work_execution_tracker
                work_execution_tracker.record_execution_start(
                    issue_number=task_context['issue_number'],
                    column=task_context['column'],
                    agent=self.agent_name,
                    trigger_source='repair_cycle_warning_review',
                    project_name=project
                )

            # Execute agent through centralized executor with full observability
            try:
                result = await agent_executor.execute_agent(
                    agent_name=self.agent_name,
                    project_name=project,
                    task_context=task_context,
                    execution_type="repair_warning",
                )

                logger.info(f"Reviewed warnings in {source_file}")
                warnings_reviewed += 1
                
                # Emit warning review completed event
                if obs:
                    obs.emit(
                        EventType.REPAIR_CYCLE_WARNING_REVIEW_COMPLETED,
                        "repair_cycle_warnings",
                        f"{task_id}_warn_{self._sanitize_filename(source_file)}",
                        project,
                        {
                            "source_file": source_file,
                            "warning_count": len(warnings),
                            "test_type": config.test_type,
                            "success": True,
                            "warnings": [asdict(w) for w in warnings],
                        },
                        pipeline_run_id=pipeline_run_id,
                    )

            except CancellationError:
                raise  # Never retry cancellations
            except Exception as e:
                logger.error(f"Failed to review warnings in {source_file}: {e}", exc_info=True)
                
                # Emit warning review failed event
                if obs:
                    obs.emit(
                        EventType.REPAIR_CYCLE_WARNING_REVIEW_FAILED,
                        "repair_cycle_warnings",
                        f"{task_id}_warn_{self._sanitize_filename(source_file)}",
                        project,
                        {
                            "source_file": source_file,
                            "warning_count": len(warnings),
                            "test_type": config.test_type,
                            "error": str(e),
                        },
                        pipeline_run_id=pipeline_run_id,
                    )

        return warnings_reviewed

    def _extract_json_from_response(self, response: str) -> Dict[str, Any]:
        """
        Extract JSON from agent response.

        Handles cases where agent returns markdown code blocks or explanation text.

        Args:
            response: Raw agent response

        Returns:
            Parsed JSON dict

        Raises:
            ValueError: If no valid JSON found
        """
        import re
        
        if not response or not response.strip():
            raise ValueError("Empty response from agent")

        # Try direct parse first (response is pure JSON)
        try:
            parsed = json.loads(response)
            # Validate the structure
            if not isinstance(parsed, dict):
                raise ValueError(f"Response is not a JSON object: {type(parsed)}")
            if "passed" not in parsed or "failed" not in parsed:
                raise ValueError(f"Response missing required fields 'passed' and 'failed': {list(parsed.keys())}")
            logger.info(f"Successfully parsed JSON directly. Keys: {list(parsed.keys())}")
            return parsed
        except json.JSONDecodeError as e:
            logger.debug(f"Direct JSON result parse failed: {e} - trying markdown extraction")

        # Try to extract from markdown code block (```json ... ``` or ``` ... ```)
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(1))
                if not isinstance(parsed, dict):
                    raise ValueError(f"Extracted JSON is not an object: {type(parsed)}")
                if "passed" not in parsed or "failed" not in parsed:
                    raise ValueError(f"Extracted JSON missing required fields: {list(parsed.keys())}")
                return parsed
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse JSON from code block: {e}")

        # Try to find any JSON object in the response (last resort)
        # Look for the largest JSON object in the response
        json_objects = re.findall(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", response, re.DOTALL)
        for json_str in reversed(json_objects):  # Try largest first
            try:
                parsed = json.loads(json_str)
                if isinstance(parsed, dict) and "passed" in parsed and "failed" in parsed:
                    logger.info("Successfully extracted JSON from nested content")
                    return parsed
            except json.JSONDecodeError:
                continue

        # No valid JSON found - provide detailed error message
        logger.error(f"Failed to extract valid JSON from response. Response length: {len(response)}")
        logger.error(f"Response preview (first 1000 chars): {response[:1000]}")
        logger.error(f"Response preview (last 500 chars): {response[-500:]}")
        
        raise ValueError(
            f"No valid JSON test result found in response. "
            f"Expected format: {{'passed': <int>, 'failed': <int>, 'failures': [...], ...}}. "
            f"Response length: {len(response)} chars. "
            f"Response preview: {response[:500]}..."
        )

    async def _checkpoint(self, test_type: str, test_cycle_iteration: int, context: Dict[str, Any]):
        """Save checkpoint for recovery"""
        logger.info(f"Checkpointing {test_type} test cycle at iteration {test_cycle_iteration}")

        # Get checkpoint manager from context or create one
        from pipeline.repair_cycle_checkpoint import RepairCycleCheckpoint, create_checkpoint_state
        
        project_dir = context.get('project_dir')
        project = context.get('project', 'unknown')
        issue_number = context.get('issue_number')

        if not project_dir:
            logger.warning("No project_dir in context, skipping checkpoint")
            return

        if not issue_number:
            logger.warning("No issue_number in context, skipping checkpoint")
            return

        checkpoint_manager = RepairCycleCheckpoint(project_dir, project_name=project, issue_number=issue_number)
        
        # Create checkpoint state
        checkpoint_state = create_checkpoint_state(
            project=context.get('project', 'unknown'),
            issue_number=context.get('issue_number', 0),
            pipeline_run_id=context.get('pipeline_run_id', 'unknown'),
            stage_name=self.name,
            test_type=test_type,
            test_type_index=self.test_configs.index(
                next(tc for tc in self.test_configs if tc.test_type == test_type)
            ),
            iteration=test_cycle_iteration,
            agent_call_count=self._agent_call_count,
            files_fixed=[],  # TODO: Track files fixed
            test_results=None,  # TODO: Track latest test results
            cycle_results=[]  # TODO: Track completed cycle results
        )
        
        # Save checkpoint
        if checkpoint_manager.save_checkpoint(checkpoint_state):
            logger.info(f"Checkpoint saved successfully at iteration {test_cycle_iteration}")
        else:
            logger.error(f"Failed to save checkpoint at iteration {test_cycle_iteration}")

    def _emit_cycle_metrics(self, cycle_result: CycleResult, context: Dict[str, Any]):
        """Emit observability metrics for a completed cycle"""
        obs = context.get("observability")
        if not obs:
            return

        project = context.get("project", "unknown")
        task_id = context.get("task_id", "unknown")

        # Emit performance metrics using the standard emit_performance_metric method
        try:
            obs.emit_performance_metric(
                agent="repair_cycle",
                task_id=task_id,
                project=project,
                metric_name="repair_cycle_iterations",
                value=float(cycle_result.iterations),
                unit="iterations",
            )

            obs.emit_performance_metric(
                agent="repair_cycle",
                task_id=task_id,
                project=project,
                metric_name="repair_cycle_duration",
                value=cycle_result.duration_seconds,
                unit="seconds",
            )

            obs.emit_performance_metric(
                agent="repair_cycle",
                task_id=task_id,
                project=project,
                metric_name="repair_cycle_files_fixed",
                value=float(cycle_result.files_fixed),
                unit="files",
            )

            obs.emit_performance_metric(
                agent="repair_cycle",
                task_id=task_id,
                project=project,
                metric_name="repair_cycle_warnings_reviewed",
                value=float(cycle_result.warnings_reviewed),
                unit="warnings",
            )

            # Emit success rate as binary metric (1.0 or 0.0)
            obs.emit_performance_metric(
                agent="repair_cycle",
                task_id=task_id,
                project=project,
                metric_name="repair_cycle_success",
                value=1.0 if cycle_result.passed else 0.0,
                unit="success",
            )

        except Exception as e:
            logger.error(f"Failed to emit cycle metrics: {e}", exc_info=True)

    async def _analyze_systemic_failures(
        self,
        test_result: "RepairTestResult",
        grouped_failures: Dict[str, List["RepairTestFailure"]],
        config: "RepairTestRunConfig",
        context: Dict[str, Any],
    ) -> SystemicAnalysisResult:
        """
        Analyze test failures for systemic root causes via a single SSE call.

        Classifies failures into:
        - Environmental: require Docker image rebuild (Dockerfile.agent changes)
        - Systemic code: same bug pattern across many files, one global fix applies

        Falls back to a "no issues" result (allowing per-file fixing to proceed)
        if the SSE call fails or JSON parsing fails.
        """
        import re

        obs = context.get("observability")
        project = context.get("project", "unknown")
        task_id = context.get("task_id", f"repair_cycle_{self.name}")
        pipeline_run_id = context.get("pipeline_run_id")

        if obs:
            obs.emit(
                EventType.REPAIR_CYCLE_SYSTEMIC_ANALYSIS_STARTED,
                "repair_cycle",
                task_id,
                project,
                {
                    "test_type": config.test_type,
                    "total_failures": test_result.failed,
                    "file_count": len(grouped_failures),
                },
                pipeline_run_id=pipeline_run_id,
            )

        # Build a compact failure summary for the prompt
        failure_lines = []
        for file, failures in grouped_failures.items():
            failure_lines.append(f"File: {file} ({len(failures)} failures)")
            for f in failures[:5]:
                failure_lines.append(f"  - [{f.test}] {f.message[:200]}")
            if len(failures) > 5:
                failure_lines.append(f"  ... and {len(failures) - 5} more")
        failure_summary = "\n".join(failure_lines)

        direct_prompt = f"""Analyze these test failures for systemic root causes.

Project: {project}
Test type: {config.test_type}
Total failures: {test_result.failed}
Files with failures: {len(grouped_failures)}

Failure summary:
{failure_summary}

Classify the failures into:
1. Environmental issues: version mismatches, missing packages, stale node_modules, outdated Docker image, or any issue requiring Dockerfile.agent changes
2. Systemic code issues: the same code pattern or bug repeated across many files that can be fixed with a single global change

You MUST return ONLY valid JSON in this EXACT format (no markdown, no explanation):
{{
    "has_env_issues": true,
    "env_issue_description": "describe what Dockerfile.agent or environment changes are needed, or empty string if none",
    "has_systemic_code_issues": false,
    "systemic_issue_description": "describe the global code fix needed and how to apply it, or empty string if none",
    "affected_files": ["list of files involved in the systemic code fix, or empty array"]
}}

If the failures appear to be isolated per-file issues with different root causes, return has_env_issues: false and has_systemic_code_issues: false."""

        task_context = {
            **context,
            "pipeline_run_id": pipeline_run_id,
            "task_description": f"Analyze systemic failures in {config.test_type} tests",
            "timeout": min(config.timeout, 300),  # Cap at 5 minutes for analysis
            "skip_workspace_prep": True,
            "review_cycle": None,
            "direct_prompt": direct_prompt,
        }

        no_issues = SystemicAnalysisResult(
            has_env_issues=False,
            has_systemic_code_issues=False,
            env_issue_description="",
            systemic_issue_description="",
            affected_files=[],
            raw_json={},
        )

        try:
            if self._agent_call_count >= self.max_total_agent_calls:
                logger.warning("Circuit breaker reached before systemic analysis, skipping")
                return no_issues

            from services.agent_executor import get_agent_executor
            agent_executor = get_agent_executor()

            self._agent_call_count += 1

            result = await agent_executor.execute_agent(
                agent_name=self.agent_name,
                project_name=project,
                task_context=task_context,
                execution_type="repair_systemic_analysis",
            )

            result_text = result.get("markdown_analysis") or result.get("raw_analysis_result", "")

            if not result_text or not result_text.strip():
                logger.warning("Systemic analysis returned empty output, skipping")
                return no_issues

            # Extract JSON (no required-field validation — different schema than test results)
            parsed = None
            try:
                parsed = json.loads(result_text.strip())
            except json.JSONDecodeError:
                json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", result_text, re.DOTALL)
                if json_match:
                    try:
                        parsed = json.loads(json_match.group(1))
                    except json.JSONDecodeError:
                        pass

            if parsed is None:
                json_objects = re.findall(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", result_text, re.DOTALL)
                for json_str in reversed(json_objects):
                    try:
                        candidate = json.loads(json_str)
                        if isinstance(candidate, dict) and "has_env_issues" in candidate:
                            parsed = candidate
                            break
                    except json.JSONDecodeError:
                        continue

            if not isinstance(parsed, dict):
                logger.warning(
                    f"Systemic analysis did not return a JSON object, falling back to per-file fixes. "
                    f"Response preview: {result_text[:500]}"
                )
                return no_issues

            analysis_result = SystemicAnalysisResult(
                has_env_issues=bool(parsed.get("has_env_issues", False)),
                has_systemic_code_issues=bool(parsed.get("has_systemic_code_issues", False)),
                env_issue_description=parsed.get("env_issue_description", ""),
                systemic_issue_description=parsed.get("systemic_issue_description", ""),
                affected_files=parsed.get("affected_files", []),
                raw_json=parsed,
            )

            logger.info(
                f"Systemic analysis: env_issues={analysis_result.has_env_issues}, "
                f"code_issues={analysis_result.has_systemic_code_issues}, "
                f"affected_files={len(analysis_result.affected_files)}"
            )

            if obs:
                obs.emit(
                    EventType.REPAIR_CYCLE_SYSTEMIC_ANALYSIS_COMPLETED,
                    "repair_cycle",
                    task_id,
                    project,
                    {
                        "test_type": config.test_type,
                        "has_env_issues": analysis_result.has_env_issues,
                        "has_systemic_code_issues": analysis_result.has_systemic_code_issues,
                        "affected_files_count": len(analysis_result.affected_files),
                    },
                    pipeline_run_id=pipeline_run_id,
                )

            return analysis_result

        except CancellationError:
            raise
        except Exception as e:
            logger.warning(
                f"Systemic failure analysis failed: {e}, falling back to per-file fixes",
                exc_info=True,
            )
            if obs:
                obs.emit(
                    EventType.REPAIR_CYCLE_SYSTEMIC_ANALYSIS_COMPLETED,
                    "repair_cycle",
                    task_id,
                    project,
                    {
                        "test_type": config.test_type,
                        "has_env_issues": False,
                        "has_systemic_code_issues": False,
                        "error": str(e),
                    },
                    pipeline_run_id=pipeline_run_id,
                )
            return no_issues

    async def _run_env_rebuild_sub_cycle(
        self,
        analysis: SystemicAnalysisResult,
        config: "RepairTestRunConfig",
        context: Dict[str, Any],
        test_cycle_iteration: int,
        test_type_index: int,
    ) -> "RepairTestResult":
        """
        Coordinate dev environment rebuild to fix environmental failures.

        Resets container state, queues dev_environment_setup with the specific
        change description, and polls until VERIFIED or BLOCKED/timeout.
        Loops up to MAX_SYSTEMIC_SUB_CYCLES times if rebuild succeeds but tests
        still fail (suggesting incomplete fix).

        Returns the last RepairTestResult obtained, so _run_test_cycle() can
        use it directly without a redundant test re-run.
        """
        from services.dev_container_state import dev_container_state, DevContainerStatus

        obs = context.get("observability")
        project = context.get("project", "unknown")
        task_id = context.get("task_id", f"repair_cycle_{self.name}")
        pipeline_run_id = context.get("pipeline_run_id")
        issue_number = context.get("issue_number")

        if obs:
            obs.emit(
                EventType.REPAIR_CYCLE_ENV_REBUILD_STARTED,
                "repair_cycle",
                task_id,
                project,
                {
                    "test_type": config.test_type,
                    "env_issue_description": analysis.env_issue_description,
                },
                pipeline_run_id=pipeline_run_id,
            )

        last_test_result = None
        attempts_made = 0

        for attempt in range(MAX_SYSTEMIC_SUB_CYCLES):
            attempts_made = attempt + 1

            # Check circuit breaker before each rebuild attempt
            if self._agent_call_count >= self.max_total_agent_calls:
                logger.error("Circuit breaker triggered during env rebuild sub-cycle")
                break

            logger.info(
                f"Env rebuild sub-cycle attempt {attempts_made}/{MAX_SYSTEMIC_SUB_CYCLES}: "
                f"{analysis.env_issue_description[:100]}"
            )

            try:
                # Reset container state to UNVERIFIED to allow re-queuing
                dev_container_state.set_status(
                    project,
                    DevContainerStatus.UNVERIFIED,
                    error_message=f"Reset for systemic env fix: {analysis.env_issue_description[:200]}",
                )

                # Queue dev environment setup with the change description
                from agents.orchestrator_integration import queue_dev_environment_setup
                await queue_dev_environment_setup(
                    project, logger, change_description=analysis.env_issue_description
                )

            except CancellationError:
                raise
            except Exception as e:
                logger.error(f"Env rebuild setup failed on attempt {attempts_made}: {e}", exc_info=True)
                break

            # Poll until VERIFIED, BLOCKED, or timeout
            poll_timeout = config.timeout // 2
            poll_interval = 30
            elapsed = 0
            final_status = None

            while elapsed < poll_timeout:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

                # Check for cancellation during long polling wait
                if issue_number:
                    from services.cancellation import get_cancellation_signal
                    if get_cancellation_signal().is_cancelled(project, issue_number):
                        logger.warning(
                            f"Pipeline cancelled during env rebuild polling for {project}/#{issue_number}"
                        )
                        raise CancellationError(
                            f"Pipeline cancelled for {project}/#{issue_number}"
                        )

                status = dev_container_state.get_status(project)
                logger.info(
                    f"Dev container status for {project}: {status.value} (elapsed: {elapsed}s)"
                )
                if status in (DevContainerStatus.VERIFIED, DevContainerStatus.BLOCKED):
                    final_status = status
                    break

            if final_status == DevContainerStatus.VERIFIED:
                # Rebuild succeeded; run tests to see if the env fix resolved failures
                last_test_result = await self._run_tests(
                    config, context, test_cycle_iteration, test_type_index
                )
                if not last_test_result.has_failures():
                    break  # All tests pass — done
                # Tests still failing after rebuild; try again (up to MAX attempts)
            else:
                # BLOCKED or timeout — stop retrying
                logger.warning(
                    f"Env rebuild ended with status={final_status} for {project} "
                    f"after {elapsed}s, stopping sub-cycle"
                )
                # If we timed out (final_status is None), the dev container may still be
                # in IN_PROGRESS from when we queued the setup agent. Reset to UNVERIFIED
                # so future pipeline executions aren't blocked by a stale in_progress state.
                if final_status is None:
                    current = dev_container_state.get_status(project)
                    if current == DevContainerStatus.IN_PROGRESS:
                        logger.warning(
                            f"Env rebuild timed out for {project} with container still "
                            f"IN_PROGRESS — resetting to UNVERIFIED to unblock future runs"
                        )
                        dev_container_state.set_status(
                            project,
                            DevContainerStatus.UNVERIFIED,
                            error_message="Reset after env rebuild polling timeout",
                        )
                break

        if obs:
            obs.emit(
                EventType.REPAIR_CYCLE_ENV_REBUILD_COMPLETED,
                "repair_cycle",
                task_id,
                project,
                {
                    "test_type": config.test_type,
                    "attempts": attempts_made,
                    "tests_pass": last_test_result is not None and not last_test_result.has_failures(),
                },
                pipeline_run_id=pipeline_run_id,
            )

        # If we never got to run tests (e.g., BLOCKED immediately), return a synthetic failure
        if last_test_result is None:
            last_test_result = RepairTestResult(
                test_type=config.test_type,
                iteration=test_cycle_iteration,
                passed=0,
                failed=1,
                warnings=0,
                failures=[
                    RepairTestFailure(
                        file="__env__",
                        test="env_rebuild",
                        message=(
                            f"Env rebuild did not reach VERIFIED status for {project}. "
                            f"Check dev container state and Dockerfile.agent."
                        ),
                    )
                ],
                warning_list=[],
                raw_output="",
                timestamp=utc_isoformat(),
            )

        return last_test_result

    @staticmethod
    def _build_failure_digest(
        test_result: "RepairTestResult",
        grouped_failures: Dict[str, List["RepairTestFailure"]],
        max_error_types: int = 10,
        examples_per_type: int = 3,
        max_top_files: int = 15,
    ) -> str:
        """
        Build a compact, stratified summary of test failures for the systemic fix prompt.

        Groups by error type to show the pattern landscape, then lists the most-affected
        files by count. Keeps total output bounded regardless of total failure count.
        """
        lines = [
            f"Total failures: {test_result.failed} across {len(grouped_failures)} files",
            "",
        ]

        # Group failures by error type (f.test field, e.g. "mypy[arg-type]")
        by_type: Dict[str, List["RepairTestFailure"]] = {}
        for failures in grouped_failures.values():
            for f in failures:
                by_type.setdefault(f.test, []).append(f)

        sorted_types = sorted(by_type.items(), key=lambda kv: len(kv[1]), reverse=True)

        lines.append("## Error types (by frequency)")
        shown_types = 0
        for error_type, type_failures in sorted_types:
            if shown_types >= max_error_types:
                remaining = len(sorted_types) - shown_types
                lines.append(f"  ... and {remaining} more error type(s)")
                break
            lines.append(f"  {error_type}: {len(type_failures)} occurrences")
            seen_files: set = set()
            examples_shown = 0
            for f in type_failures:
                if f.file not in seen_files and examples_shown < examples_per_type:
                    msg = f.message[:147] + "..." if len(f.message) > 150 else f.message
                    lines.append(f"    e.g. {f.file}: {msg}")
                    seen_files.add(f.file)
                    examples_shown += 1
            shown_types += 1

        # Top files by failure count
        top_files = sorted(grouped_failures.items(), key=lambda kv: len(kv[1]), reverse=True)
        lines.append("")
        lines.append(f"## Most affected files (top {min(max_top_files, len(top_files))} of {len(grouped_failures)})")
        for file, failures in top_files[:max_top_files]:
            type_counts: Dict[str, int] = {}
            for f in failures:
                type_counts[f.test] = type_counts.get(f.test, 0) + 1
            type_summary = ", ".join(f"{t}×{c}" for t, c in sorted(type_counts.items(), key=lambda x: -x[1])[:3])
            lines.append(f"  {file}: {len(failures)} failures ({type_summary})")

        return "\n".join(lines)

    async def _run_systemic_fix_sub_cycle(
        self,
        analysis: SystemicAnalysisResult,
        initial_test_result: "RepairTestResult",
        initial_grouped_failures: Dict[str, List["RepairTestFailure"]],
        config: "RepairTestRunConfig",
        context: Dict[str, Any],
        test_cycle_iteration: int,
        test_type_index: int,
    ) -> "RepairTestResult":
        """
        Apply a global fix for a systemic code issue across all affected files.

        Each attempt receives an open-ended prompt built from the current failure
        state — not a predetermined file list — so the agent greps for all affected
        files itself and adapts as failures are resolved between attempts.

        Returns the last RepairTestResult obtained, so _run_test_cycle() can
        use it directly without a redundant test re-run.
        """
        obs = context.get("observability")
        project = context.get("project", "unknown")
        task_id = context.get("task_id", f"repair_cycle_{self.name}")
        pipeline_run_id = context.get("pipeline_run_id")

        if obs:
            obs.emit(
                EventType.REPAIR_CYCLE_SYSTEMIC_FIX_STARTED,
                "repair_cycle",
                task_id,
                project,
                {
                    "test_type": config.test_type,
                    "systemic_issue_description": analysis.systemic_issue_description,
                    "affected_files_count": len(initial_grouped_failures),
                },
                pipeline_run_id=pipeline_run_id,
            )

        last_test_result = None
        attempts_made = 0
        current_test_result = initial_test_result
        current_grouped = initial_grouped_failures

        for attempt in range(MAX_SYSTEMIC_SUB_CYCLES):
            attempts_made = attempt + 1

            # Check circuit breaker before each attempt; hitting the limit terminates the sub-cycle immediately
            if self._agent_call_count >= self.max_total_agent_calls:
                logger.error(
                    f"Circuit breaker triggered during systemic fix sub-cycle for "
                    f"{project}/{task_id}: agent_call_count={self._agent_call_count} "
                    f">= max={self.max_total_agent_calls}"
                )
                # Preserve real failure state so the caller doesn't receive a misleading sentinel
                last_test_result = current_test_result
                break

            failure_digest = self._build_failure_digest(current_test_result, current_grouped)
            # Guard against None if JSON parser returns null for this field
            raw_description = analysis.systemic_issue_description
            if raw_description is None:
                logger.warning(
                    f"systemic_issue_description is None for {project}/{task_id} — "
                    f"root cause hint will be omitted from the fix prompt"
                )
            description = (raw_description or "").strip()
            known_pattern = (
                f"\n\nAnalysis has identified the likely root cause:\n{description}"
                if description
                else ""
            )
            attempt_note = (
                f"\n\nThis is attempt {attempts_made}/{MAX_SYSTEMIC_SUB_CYCLES}. "
                f"Focus on the remaining failures shown above."
                if attempts_made > 1
                else ""
            )

            logger.info(
                f"Systemic fix sub-cycle attempt {attempts_made}/{MAX_SYSTEMIC_SUB_CYCLES}: "
                f"{current_test_result.failed} {config.test_type} failures remaining"
            )

            direct_prompt = f"""Your goal is to fix ALL failing {config.test_type} tests in this project. Every failure listed below must pass before you are done.{known_pattern}

## Current failure state
{failure_digest}{attempt_note}

## How to approach this
1. Examine representative failing files to understand the root cause. The failure state above shows a sample — the full set of affected files may be larger.
2. Use grep or glob to discover every file in the project that exhibits the same pattern, not just the ones listed above.
3. Apply fixes comprehensively across all affected files. Prefer bulk approaches (scripted edits, sed, ast-based transforms) over editing files one at a time.
4. After fixing, run the {config.test_type} checks to measure progress. Repeat until all failures are resolved."""

            task_description = (
                f"Fix {current_test_result.failed} {config.test_type} failures"
                + (f": {analysis.systemic_issue_description[:80]}" if analysis.systemic_issue_description else "")
            )
            task_context = {
                **context,
                "pipeline_run_id": pipeline_run_id,
                "task_description": task_description,
                "timeout": config.timeout,
                "skip_workspace_prep": True,
                "review_cycle": None,
                "direct_prompt": direct_prompt,
            }

            self._agent_call_count += 1

            try:
                from services.agent_executor import get_agent_executor
                agent_executor = get_agent_executor()

                await agent_executor.execute_agent(
                    agent_name=self.agent_name,
                    project_name=project,
                    task_context=task_context,
                    execution_type="repair_systemic_fix",
                )
                logger.info(f"Systemic fix attempt {attempts_made} completed")

            except CancellationError:
                raise
            except (ImportError, AttributeError, TypeError) as e:
                # Non-retryable: agent executor module is missing, its interface does
                # not match the call site, or arguments are wrong type. None of these
                # conditions improve on retry.
                logger.error(
                    f"Systemic fix agent executor unavailable on attempt {attempts_made}: {e}",
                    exc_info=True,
                )
                # Preserve real failure state so the caller doesn't misread a stale result
                last_test_result = current_test_result
                break
            except Exception as e:
                logger.error(
                    f"Systemic fix attempt {attempts_made}/{MAX_SYSTEMIC_SUB_CYCLES} agent "
                    f"execution failed for {project}/{task_id} "
                    f"(pipeline_run_id={pipeline_run_id}, test_type={config.test_type}): {e}",
                    exc_info=True,
                )
                # Continue to test run — partial fix may still have helped

            # Re-run tests to evaluate the fix; update state for next attempt
            last_test_result = await self._run_tests(
                config, context, test_cycle_iteration, test_type_index
            )

            # Detect infrastructure failure (indicated by __infrastructure__ sentinel file)
            infra_failures = [f for f in last_test_result.failures if f.file == "__infrastructure__"]
            if infra_failures:
                logger.error(
                    f"Test runner returned infrastructure failure on attempt {attempts_made}; "
                    f"cannot evaluate fix outcome: {infra_failures[0].message}"
                )
                break

            if not last_test_result.has_failures():
                break  # Fix resolved all failures
            current_test_result = last_test_result
            current_grouped = last_test_result.group_failures_by_file()

        if obs:
            obs.emit(
                EventType.REPAIR_CYCLE_SYSTEMIC_FIX_COMPLETED,
                "repair_cycle",
                task_id,
                project,
                {
                    "test_type": config.test_type,
                    "attempts": attempts_made,
                    "tests_pass": last_test_result is not None and not last_test_result.has_failures(),
                },
                pipeline_run_id=pipeline_run_id,
            )

        # Should not be None (loop always runs at least once), but guard defensively
        if last_test_result is None:
            last_test_result = RepairTestResult(
                test_type=config.test_type,
                iteration=test_cycle_iteration,
                passed=0,
                failed=1,
                warnings=0,
                failures=[
                    RepairTestFailure(
                        file="__infrastructure__",
                        test="systemic_fix",
                        message="Systemic fix sub-cycle produced no test result",
                    )
                ],
                warning_list=[],
                raw_output="",
                timestamp=utc_isoformat(),
            )

        return last_test_result
