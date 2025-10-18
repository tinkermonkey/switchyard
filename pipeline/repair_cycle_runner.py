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
    5. Save final result and exit with status code

Exit Codes:
    0: Success - all tests passed
    1: Failure - tests failed or max iterations reached
    2: Error - execution error or agent failure
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
            from pipeline.repair_cycle import RepairCycleStage, RepairTestRunConfig, RepairTestType
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
                test_type = RepairTestType(tc['test_type'])
                test_configs.append(RepairTestRunConfig(
                    test_type=test_type,
                    timeout=tc.get('timeout', 600),
                    max_iterations=tc.get('max_iterations', 5),
                    review_warnings=tc.get('review_warnings', True),
                    max_file_iterations=tc.get('max_file_iterations', 3)
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
                # Restore state from checkpoint
                self.stage._agent_call_count = checkpoint.get('agent_call_count', 0)
                logger.info(f"Restored agent call count: {self.stage._agent_call_count}")

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
        except Exception as e:
            logger.error(f"Repair cycle execution failed: {e}", exc_info=True)
            return {'overall_success': False, 'error': str(e)}

    def save_result(self, result: Dict[str, Any]) -> bool:
        """Save final result to file in orchestrator_data directory"""
        try:
            # Save to orchestrator_data directory (keeps project workspace clean)
            project_name = self.context.get('project')
            issue_number = self.context.get('issue_number')
            
            orchestrator_data_dir = Path("/workspace/clauditoreum/orchestrator_data/repair_cycles")
            repair_cycle_dir = orchestrator_data_dir / project_name / str(issue_number)
            repair_cycle_dir.mkdir(parents=True, exist_ok=True)
            
            result_file = repair_cycle_dir / "result.json"
            with open(result_file, 'w') as f:
                json.dump(result, f, indent=2, default=str)

            logger.info(f"Result saved to {result_file}")
            return True

        except Exception as e:
            logger.error(f"Failed to save result: {e}", exc_info=True)
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

            # Save result
            self.save_result(result)

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
