"""
Scheduled task to test Claude Code token availability.

When the circuit breaker is open, this scheduler runs a test 1 minute after
the detected reset time to verify tokens are available again.
"""

import logging
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class ClaudeTokenScheduler:
    """Manages scheduled testing of Claude Code token availability"""
    
    def __init__(self):
        self.test_scheduled_for: Optional[datetime] = None
        self.is_testing = False
    
    async def check_and_run_test(self):
        """
        Check if it's time to test token availability.
        Should be called periodically from the main loop.

        Returns:
            True if test was run and tokens are available
        """
        from monitoring.claude_code_breaker import get_breaker

        breaker = get_breaker()
        if not breaker:
            return True

        # Sync from Redis to get latest state (important for cross-process state changes)
        status = breaker.get_status()
        current_state = status['state']

        # Only test if breaker is open or half-open
        if current_state == breaker.CLOSED:
            return True
        
        # If no test scheduled yet and reset time is known, schedule one
        if not self.test_scheduled_for and breaker.reset_time:
            # Schedule test for 1 minute after reset time
            self.test_scheduled_for = breaker.reset_time + timedelta(minutes=1)
            logger.info(
                f"Scheduled Claude Code token availability test for {self.test_scheduled_for.strftime('%Y-%m-%d %H:%M:%S')}"
            )

        # Check if it's time to run the test
        if self.test_scheduled_for and datetime.now(timezone.utc) >= self.test_scheduled_for:
            if not self.is_testing:
                self.is_testing = True
                logger.warning("🟡 Testing Claude Code token availability...")

                try:
                    success = await self._run_token_test()
                    if success:
                        logger.info("✅ Claude Code tokens available! Closing circuit breaker...")
                        breaker.close()
                        self.test_scheduled_for = None
                        self.is_testing = False
                        return True
                    else:
                        logger.warning("❌ Claude Code tokens still unavailable. Will retry soon.")
                        # Reschedule for 2 minutes later
                        self.test_scheduled_for = datetime.now(timezone.utc) + timedelta(minutes=2)
                        self.is_testing = False
                except Exception as e:
                    logger.error(f"Error testing token availability: {e}", exc_info=True)
                    # Reschedule for 5 minutes later
                    self.test_scheduled_for = datetime.now(timezone.utc) + timedelta(minutes=5)
                    self.is_testing = False

        return False
    
    async def _run_token_test(self) -> bool:
        """
        Run a simple test to verify Claude Code tokens are available.
        
        This runs a non-container test (direct Claude Code invocation) to avoid
        needing to build and run a Docker container.
        
        Returns:
            True if tokens are available
        """
        try:
            import subprocess
            
            # Run a simple claude --version command to test token availability
            # This should fail quickly if tokens are exhausted
            logger.debug("Running 'claude --version' to test token availability...")
            
            result = subprocess.run(
                ['claude', '--version'],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                logger.info(f"Claude version check successful: {result.stdout.strip()}")
                return True
            else:
                logger.warning(f"Claude version check failed: {result.stderr.strip()}")
                return False
                
        except subprocess.TimeoutExpired:
            logger.warning("Claude token test timed out")
            return False
        except FileNotFoundError:
            logger.error("Claude CLI not found - cannot test token availability")
            return False
        except Exception as e:
            logger.error(f"Error running token test: {e}")
            return False


# Global scheduler instance
_scheduler: Optional[ClaudeTokenScheduler] = None


def get_scheduler() -> ClaudeTokenScheduler:
    """Get or create the global Claude token scheduler."""
    global _scheduler
    if _scheduler is None:
        _scheduler = ClaudeTokenScheduler()
    return _scheduler
