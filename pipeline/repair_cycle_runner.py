#!/usr/bin/env python3
"""
Repair Cycle Container Runner

Entry point for running repair cycles in Docker containers.
Designed to survive orchestrator restarts by running in an isolated container.

Usage:
    python -m pipeline.repair_cycle_runner \\
        --project project-name \\
        --issue 123 \\
        --pipeline-run-id abc123 \\
        --stage Testing \\
        --context /workspace/project/.repair_cycle_context.json

Container Lifecycle:
    1. Load context from file (contains all config)
    2. Check for existing checkpoint
    3. Resume from checkpoint or start fresh
    4. Execute repair cycle with periodic checkpointing
    5. Save final result to Redis (with 24-hour TTL)
    6. Exit with status code

State Storage:
    - Context and checkpoints: File-based in orchestrator_data/
    - Final results: Redis (key: repair_cycle:result:{project}:{issue}:{run_id})
    - Redis results survive container removal and orchestrator restarts
    - Results auto-expire after 24 hours

Exit Codes:
    0: Success - all tests passed
    1: Failure - tests failed or max iterations reached
    2: Error - execution error, agent failure, or failed to save result
    3: Timeout - repair cycle exceeded timeout
    4: Cancelled - repair cycle cancelled by user
"""

import argparse
import asyncio
import json
import logging
import sys
import os
import signal
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime
from services.cancellation import CancellationError

# Setup logging (initially just to stdout, file handler added after parsing args)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class RepairCycleRunner:
    """Containerized repair cycle executor"""

    def __init__(self, args):
        self.args = args
        self.project_dir = Path(f"/workspace/{args.project}")
        self.context_file = Path(args.context) if args.context else None
        self.context = None
        self.stage = None
        self.checkpoint_manager = None
        self._cancelled = False

        # Add file handler now that we know the project directory
        log_file = self.project_dir / ".repair_cycle.log"
        file_handler = logging.FileHandler(log_file, mode='a')
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        logging.getLogger().addHandler(file_handler)
        logger.info(f"Logging to {log_file}")

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, frame):
        """Handle termination signals gracefully"""
        logger.warning(f"Received signal {signum}, cancelling repair cycle...")
        self._cancelled = True

    def load_context(self) -> Dict[str, Any]:
        """Load context from file or environment"""
        try:
            if self.context_file and self.context_file.exists():
                logger.info(f"Loading context from {self.context_file}")
                with open(self.context_file, 'r') as f:
                    context = json.load(f)
            else:
                # Build minimal context from args
                logger.info("Building context from arguments")
                context = {
                    'project': self.args.project,
                    'issue_number': self.args.issue,
                    'pipeline_run_id': self.args.pipeline_run_id,
                    'stage_name': self.args.stage,
                    'project_dir': str(self.project_dir),
                    'use_docker': True
                }

            # Validate required fields
            required = ['project', 'issue_number', 'pipeline_run_id']
            missing = [f for f in required if f not in context]
            if missing:
                raise ValueError(f"Missing required context fields: {missing}")

            self.context = context
            logger.info(f"Context loaded: project={context['project']}, issue={context['issue_number']}")
            return context

        except Exception as e:
            logger.error(f"Failed to load context: {e}", exc_info=True)
            sys.exit(2)

    def initialize_stage(self) -> bool:
        """Initialize RepairCycleStage with config from context"""
        try:
            from pipeline.repair_cycle import RepairCycleStage, RepairTestRunConfig
            from pipeline.repair_cycle_checkpoint import RepairCycleCheckpoint

            # Initialize checkpoint manager with project and issue info
            project_name = self.context.get('project')
            issue_number = self.context.get('issue_number')
            self.checkpoint_manager = RepairCycleCheckpoint(
                str(self.project_dir),
                project_name=project_name,
                issue_number=issue_number
            )

            # Load test configs from context
            test_configs = []
            for tc in self.context.get('test_configs', []):
                test_type = tc['test_type']
                test_configs.append(RepairTestRunConfig(
                    test_type=test_type,
                    max_iterations=tc.get('max_iterations', 5),
                    review_warnings=tc.get('review_warnings', True),
                    max_file_iterations=tc.get('max_file_iterations', 3),
                    systemic_analysis_threshold=tc.get('systemic_analysis_threshold', 6)
                ))

            if not test_configs:
                logger.error("No test configurations found in context")
                return False

            # Create stage
            self.stage = RepairCycleStage(
                name=self.context.get('stage_name', 'Testing'),
                test_configs=test_configs,
                agent_name=self.context.get('agent_name', 'senior_software_engineer'),
                max_total_agent_calls=self.context.get('max_total_agent_calls', 100),
                checkpoint_interval=self.context.get('checkpoint_interval', 5)
            )

            logger.info(
                f"Stage initialized: {len(test_configs)} test types, "
                f"agent={self.context.get('agent_name')}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to initialize stage: {e}", exc_info=True)
            return False

    def load_checkpoint(self) -> Optional[Dict[str, Any]]:
        """Load checkpoint if exists"""
        try:
            checkpoint = self.checkpoint_manager.load_checkpoint()
            if checkpoint:
                logger.info(
                    f"Resuming from checkpoint: iteration={checkpoint.get('iteration')}, "
                    f"test_type={checkpoint.get('test_type')}"
                )
                return checkpoint
            return None

        except Exception as e:
            logger.error(f"Failed to load checkpoint: {e}", exc_info=True)
            return None

    async def execute_repair_cycle(self) -> Dict[str, Any]:
        """Execute repair cycle with checkpointing"""
        try:
            # Initialize observability manager and add to context
            from monitoring.observability import get_observability_manager
            observability = get_observability_manager()
            self.context['observability'] = observability
            logger.info("Observability manager initialized and added to context")
            
            # Ensure pipeline_run_id is in context (read from environment if not already set)
            if 'pipeline_run_id' not in self.context:
                pipeline_run_id = os.environ.get('PIPELINE_RUN_ID')
                if pipeline_run_id:
                    self.context['pipeline_run_id'] = pipeline_run_id
                    logger.info(f"Loaded pipeline_run_id from environment: {pipeline_run_id}")
                else:
                    logger.warning("No pipeline_run_id found in context or environment")
            
            # Check for checkpoint
            checkpoint = self.load_checkpoint()
            if checkpoint:
                current_run_id = self.context.get('pipeline_run_id')
                checkpoint_run_id = checkpoint.get('pipeline_run_id')
                if current_run_id and checkpoint_run_id and current_run_id == checkpoint_run_id:
                    # Same pipeline run — restore agent_call_count for restart resilience
                    self.stage._agent_call_count = checkpoint.get('agent_call_count', 0)
                    logger.info(f"Restored agent call count: {self.stage._agent_call_count}")
                else:
                    # Stale checkpoint from a different pipeline run — clear it and start fresh
                    logger.info(
                        f"Stale checkpoint found (run {checkpoint_run_id} vs current {current_run_id}), "
                        f"clearing and starting fresh (had agent_call_count={checkpoint.get('agent_call_count')})"
                    )
                    self.checkpoint_manager.clear_checkpoint()

            # Execute stage
            logger.info("Starting repair cycle execution...")
            result = await self.stage.execute(self.context)

            # Clear checkpoint on success
            if result.get('overall_success'):
                self.checkpoint_manager.clear_checkpoint()
                logger.info("Repair cycle completed successfully, checkpoint cleared")

            return result

        except asyncio.CancelledError:
            logger.warning("Repair cycle cancelled")
            return {'overall_success': False, 'error': 'cancelled'}
        except CancellationError:
            logger.warning("Repair cycle cancelled by pipeline run lifecycle")
            return {'overall_success': False, 'error': 'Pipeline run ended externally'}
        except Exception as e:
            logger.error(f"Repair cycle execution failed: {e}", exc_info=True)
            return {'overall_success': False, 'error': str(e)}

    def save_result_to_redis(self, result: Dict[str, Any]) -> bool:
        """
        Save final result to Redis for recovery and monitoring.

        This replaces file-based result storage to avoid polluting project repos.
        Results are stored in Redis with a 24-hour TTL.

        Args:
            result: Result dictionary from repair cycle execution

        Returns:
            True if saved successfully, False otherwise
        """
        try:
            import redis

            project_name = self.context.get('project')
            issue_number = self.context.get('issue_number')
            run_id = self.args.pipeline_run_id

            # Connect to Redis
            redis_host = os.environ.get('REDIS_HOST', 'redis')
            redis_client = redis.Redis(
                host=redis_host,
                port=6379,
                decode_responses=True,
                socket_connect_timeout=5,  # 5 second timeout
                socket_timeout=5
            )

            # Test connection
            redis_client.ping()
            logger.debug(f"Redis connection successful to {redis_host}")

            # Store result in Redis with 24-hour TTL
            # Key format: repair_cycle:result:{project}:{issue}:{run_id}
            redis_key = f"repair_cycle:result:{project_name}:{issue_number}:{run_id}"

            # Convert result to JSON string
            result_json = json.dumps(result, default=str)

            # Store with 24 hour TTL (86400 seconds)
            redis_client.setex(redis_key, 86400, result_json)

            logger.info(f"Result saved to Redis: {redis_key}")
            logger.info(f"Result summary: success={result.get('overall_success')}, "
                       f"agent_calls={result.get('total_agent_calls')}, "
                       f"duration={result.get('duration_seconds', 0):.1f}s")

            return True

        except Exception as e:
            logger.error(f"CRITICAL: Failed to save result to Redis: {e}", exc_info=True)
            return False

    def run(self) -> int:
        """
        Main execution flow.

        Returns:
            Exit code (0=success, 1=failure, 2=error, 3=timeout, 4=cancelled)
        """
        try:
            logger.info("=" * 80)
            logger.info(f"Repair Cycle Container Runner Starting")
            logger.info(f"Project: {self.args.project}")
            logger.info(f"Issue: {self.args.issue}")
            logger.info(f"Pipeline Run: {self.args.pipeline_run_id}")
            logger.info("=" * 80)

            # Load context
            self.load_context()

            # Initialize stage
            if not self.initialize_stage():
                logger.error("Stage initialization failed")
                return 2

            # Execute repair cycle
            result = asyncio.run(self.execute_repair_cycle())

            # Check if cancelled
            if self._cancelled:
                logger.warning("Repair cycle was cancelled")
                return 4

            # Save result to Redis (CRITICAL: must succeed for recovery)
            if not self.save_result_to_redis(result):
                logger.error("CRITICAL: Failed to save result to Redis - container cannot be recovered!")
                return 2  # Return error if we can't persist state

            # Determine exit code
            if result.get('overall_success'):
                logger.info("Repair cycle succeeded - all tests passed!")
                return 0
            elif result.get('error'):
                logger.error(f"Repair cycle failed with error: {result.get('error')}")
                return 2
            else:
                logger.warning("Repair cycle failed - max iterations reached or tests still failing")
                return 1

        except KeyboardInterrupt:
            logger.warning("Interrupted by user")
            return 4
        except CancellationError:
            logger.warning("Repair cycle cancelled by pipeline lifecycle")
            return 4  # Exit code for cancelled
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            return 2
        finally:
            logger.info("Repair cycle container runner exiting")


def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description='Run repair cycle in Docker container',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument(
        '--project',
        required=True,
        help='Project name'
    )

    parser.add_argument(
        '--issue',
        type=int,
        required=True,
        help='GitHub issue number'
    )

    parser.add_argument(
        '--pipeline-run-id',
        required=True,
        help='Pipeline run ID'
    )

    parser.add_argument(
        '--stage',
        default='Testing',
        help='Stage name (default: Testing)'
    )

    parser.add_argument(
        '--context',
        help='Path to context JSON file'
    )

    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug logging'
    )

    return parser.parse_args()


def main():
    """Entry point"""
    args = parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    runner = RepairCycleRunner(args)
    exit_code = runner.run()
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
