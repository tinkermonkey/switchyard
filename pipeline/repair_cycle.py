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
            test_type=RepairTestType.UNIT,
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
from enum import Enum
from datetime import datetime

from pipeline.base import PipelineStage
from monitoring.timestamp_utils import utc_now, utc_isoformat, to_utc_isoformat
from monitoring.observability import EventType


logger = logging.getLogger(__name__)


class RepairTestType(Enum):
    """Supported test types for repair cycles"""

    UNIT = "unit"
    INTEGRATION = "integration"
    E2E = "e2e"


@dataclass
class RepairTestRunConfig:
    """Configuration for a single test type run cycle"""

    test_type: RepairTestType
    timeout: int = 900  # Timeout in seconds
    max_iterations: int = 5  # Max test-fix-validate iterations
    review_warnings: bool = True  # Whether to review and fix warnings
    max_file_iterations: int = 3  # Max times to attempt fixing a single file


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

    test_type: RepairTestType
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

    test_type: RepairTestType
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
            "test_type": self.test_type.value,
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
                    "test_types": [tc.test_type.value for tc in self.test_configs],
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
                    f"Starting {test_config.test_type.value} test cycle "
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
                            "test_type": test_config.test_type.value,
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
                        f"{test_config.test_type.value} tests failed after {cycle_result.iterations} iterations"
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
                    "test_type": config.test_type.value,
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

        while test_cycle_iteration < config.max_iterations:
            test_cycle_iteration += 1
            logger.info(
                f"Test cycle iteration {test_cycle_iteration}/{config.max_iterations} "
                f"for {config.test_type.value} (test type {test_type_index}/{len(self.test_configs)})"
            )

            # Emit iteration started event
            if obs:
                obs.emit(
                    EventType.REPAIR_CYCLE_ITERATION,
                    "repair_cycle",
                    task_id,
                    project,
                    {
                        "test_type": config.test_type.value,
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
                
                # Note: Completion event will be emitted by caller
                return CycleResult(
                    test_type=config.test_type,
                    passed=False,
                    iterations=test_cycle_iteration,
                    final_result=None,
                    error="Circuit breaker: max agent calls reached",
                    files_fixed=files_fixed,
                    warnings_reviewed=warnings_reviewed,
                )
                
                # Emit test cycle completed event with failure
                if obs:
                    obs.emit(
                        EventType.REPAIR_CYCLE_TEST_CYCLE_COMPLETED,
                        "repair_cycle",
                        task_id,
                        project,
                        {
                            "test_type": config.test_type.value,
                            "test_type_index": test_type_index,
                            "passed": 0,  # Convert bool to int for ES schema
                            "test_cycle_iterations": test_cycle_iteration,
                            "error": "Circuit breaker: max agent calls reached",
                        },
                        pipeline_run_id=pipeline_run_id,
                    )
                
                # Emit cycle completed event with failure
                if obs:
                    obs.emit(
                        EventType.REPAIR_CYCLE_COMPLETED,
                        "repair_cycle",
                        task_id,
                        project,
                        {
                            "test_type": config.test_type.value,
                            "test_type_index": test_type_index,
                            "passed": 0,  # Convert bool to int for ES schema
                            "test_cycle_iterations": test_cycle_iteration,
                            "error": "Circuit breaker: max agent calls reached",
                        },
                        pipeline_run_id=pipeline_run_id,
                    )
                
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
                            "test_type": config.test_type.value,
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
                            "test_type": config.test_type.value,
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

            # Emit fix cycle started event
            if obs:
                obs.emit(
                    EventType.REPAIR_CYCLE_FIX_CYCLE_STARTED,
                    "repair_cycle",
                    task_id,
                    project,
                    {
                        "test_type": config.test_type.value,
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
                        "test_type": config.test_type.value,
                        "test_type_index": test_type_index,  # Which test type in sequence
                        "test_cycle_iteration": test_cycle_iteration,  # Which iteration of this test type
                        "files_fixed": fixed_count,
                        "total_files_fixed": files_fixed,
                    },
                    pipeline_run_id=pipeline_run_id,
                )

            # Checkpoint if needed
            if test_cycle_iteration % self.checkpoint_interval == 0:
                await self._checkpoint(config.test_type, test_cycle_iteration, context)

        # Max iterations reached
        logger.error(f"Max iterations ({config.max_iterations}) reached for " f"{config.test_type.value} tests")
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
        logger.info(f"Running {config.test_type.value} tests (test cycle iteration {test_cycle_iteration}/{config.max_iterations}, test type {test_type_index})")

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
                    "test_type": config.test_type.value,
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

        # Build task context for this agent execution
        task_context = {
            **context,
            "pipeline_run_id": pipeline_run_id,  # Ensure pipeline_run_id flows through
            "task_description": f"Run {config.test_type.value} tests (test cycle iteration {test_cycle_iteration})",
            "timeout": config.timeout,
            # Skip workspace preparation - repair cycle already runs in prepared workspace
            "skip_workspace_prep": True,
            # Clear review_cycle context to avoid iteration count confusion from previous review cycles
            "review_cycle": None,
            # Provide the custom instructions as a 'direct_prompt' so the agent can use it
            "direct_prompt": f"""Run all {config.test_type.value} tests for this project.

Please identify the appropriate test framework and location for {config.test_type.value} tests.

**IMPORTANT**: When running tests, save the test results to a file in /tmp so that you can refer back to them and so they're not made part of the codebase.

These tests runs can take some time to complete, please be patient and don't put time limits on the test execution. Make sure you're capturing the information you need to identify failures without having to re-run the tests multiple times.

Also, make sure you are capturing the **full** output of the test runs, including any warnings and errors, to disk to avoid having to re-run the tests multiple times.

Be mindful of environment setup steps like installing dependencies and activating virtual environments.

CRITICAL: You MUST return ONLY valid JSON in this EXACT format (no markdown, no explanation):
{{
    "passed": <number of tests that passed>,
    "failed": <number of tests that failed>,
    "warnings": <number of warnings>,
    "failures": [
        {{"file": "test_file.py", "test": "test_function_name", "message": "failure message"}},
        ...
    ],
    "warning_list": [
        {{"file": "source_file.py", "message": "warning message"}},
        ...
    ]
}}

DO NOT include any explanation, markdown formatting, or other text - ONLY the JSON object.""",
        }

        # Retry logic for infrastructure failures (e.g., JSON parsing)
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                # Increment agent call counter
                self._agent_call_count += 1

                # Execute agent through centralized executor with full observability
                result = await agent_executor.execute_agent(
                    agent_name=self.agent_name,
                    project_name=project,
                    task_context=task_context,
                    task_id_prefix=f"repair_test_{config.test_type.value}_type{test_type_index}_iter{test_cycle_iteration}_attempt{attempt}",
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
                            "test_type": config.test_type.value,
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
                    f"{task_id}_fix_{test_file.replace('/', '_').replace('.', '_')}",
                    project,
                    {
                        "test_file": test_file,
                        "failure_count": len(failures),
                        "test_type": config.test_type.value,
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
""",
            }

            # Increment agent call counter
            self._agent_call_count += 1

            # Execute agent through centralized executor with full observability
            try:
                result = await agent_executor.execute_agent(
                    agent_name=self.agent_name,
                    project_name=project,
                    task_context=task_context,
                    task_id_prefix=f"repair_fix_{test_file.replace('/', '_').replace('.', '_')}",
                )

                logger.info(f"Fixed failures in {test_file}")
                files_fixed += 1
                
                # Emit file fix completed event
                if obs:
                    obs.emit(
                        EventType.REPAIR_CYCLE_FILE_FIX_COMPLETED,
                        "repair_cycle_fix",
                        f"{task_id}_fix_{test_file.replace('/', '_').replace('.', '_')}",
                        project,
                        {
                            "test_file": test_file,
                            "failure_count": len(failures),
                            "test_type": config.test_type.value,
                            "success": True,
                        },
                        pipeline_run_id=pipeline_run_id,
                    )

            except Exception as e:
                logger.error(f"Failed to fix failures in {test_file}: {e}", exc_info=True)
                
                # Emit file fix failed event
                if obs:
                    obs.emit(
                        EventType.REPAIR_CYCLE_FILE_FIX_FAILED,
                        "repair_cycle_fix",
                        f"{task_id}_fix_{test_file.replace('/', '_').replace('.', '_')}",
                        project,
                        {
                            "test_file": test_file,
                            "failure_count": len(failures),
                            "test_type": config.test_type.value,
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
                    f"{task_id}_warn_{source_file.replace('/', '_').replace('.', '_')}",
                    project,
                    {
                        "source_file": source_file,
                        "warning_count": len(warnings),
                        "test_type": config.test_type.value,
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

            # Execute agent through centralized executor with full observability
            try:
                result = await agent_executor.execute_agent(
                    agent_name=self.agent_name,
                    project_name=project,
                    task_context=task_context,
                    task_id_prefix=f"repair_warn_{source_file.replace('/', '_').replace('.', '_')}",
                )

                logger.info(f"Reviewed warnings in {source_file}")
                warnings_reviewed += 1
                
                # Emit warning review completed event
                if obs:
                    obs.emit(
                        EventType.REPAIR_CYCLE_WARNING_REVIEW_COMPLETED,
                        "repair_cycle_warnings",
                        f"{task_id}_warn_{source_file.replace('/', '_').replace('.', '_')}",
                        project,
                        {
                            "source_file": source_file,
                            "warning_count": len(warnings),
                            "test_type": config.test_type.value,
                            "success": True,
                            "warnings": [asdict(w) for w in warnings],
                        },
                        pipeline_run_id=pipeline_run_id,
                    )

            except Exception as e:
                logger.error(f"Failed to review warnings in {source_file}: {e}", exc_info=True)
                
                # Emit warning review failed event
                if obs:
                    obs.emit(
                        EventType.REPAIR_CYCLE_WARNING_REVIEW_FAILED,
                        "repair_cycle_warnings",
                        f"{task_id}_warn_{source_file.replace('/', '_').replace('.', '_')}",
                        project,
                        {
                            "source_file": source_file,
                            "warning_count": len(warnings),
                            "test_type": config.test_type.value,
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

    async def _checkpoint(self, test_type: RepairTestType, test_cycle_iteration: int, context: Dict[str, Any]):
        """Save checkpoint for recovery"""
        logger.info(f"Checkpointing {test_type.value} test cycle at iteration {test_cycle_iteration}")

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
            test_type=test_type.value,
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
