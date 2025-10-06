"""
Circuit Breaker Pattern Implementation

Prevents cascading failures by stopping requests to failing services
and allowing them time to recover.
"""

import asyncio
import logging
from enum import Enum
from datetime import datetime, timedelta
from typing import Callable, Any, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"        # Normal operation, requests flow through
    OPEN = "open"            # Too many failures, reject requests
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    """Circuit breaker configuration"""
    failure_threshold: int = 5        # Failures before opening circuit
    recovery_timeout: int = 30        # Seconds to wait before testing recovery
    success_threshold: int = 2        # Successes in half-open before closing
    expected_exception: type = Exception


class CircuitBreakerOpen(Exception):
    """Raised when circuit breaker is open"""
    pass


class CircuitBreaker:
    """
    Circuit Breaker implementation

    States:
    - CLOSED: Normal operation, all requests pass through
    - OPEN: Too many failures, reject all requests
    - HALF_OPEN: Testing recovery, allow limited requests

    Transitions:
    - CLOSED -> OPEN: After failure_threshold consecutive failures
    - OPEN -> HALF_OPEN: After recovery_timeout seconds
    - HALF_OPEN -> CLOSED: After success_threshold consecutive successes
    - HALF_OPEN -> OPEN: On any failure
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 30,
        success_threshold: int = 2,
        expected_exception: type = Exception
    ):
        """
        Initialize circuit breaker

        Args:
            name: Circuit breaker name (for logging)
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Seconds to wait before attempting recovery
            success_threshold: Successes needed in half-open to close circuit
            expected_exception: Exception type that triggers circuit breaker
        """
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold
        self.expected_exception = expected_exception

        # State tracking
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[datetime] = None
        self.last_state_change: datetime = datetime.now()

        # Metrics
        self.total_failures = 0
        self.total_successes = 0
        self.total_rejected = 0

        logger.info(
            f"Circuit breaker '{name}' initialized: "
            f"failure_threshold={failure_threshold}, "
            f"recovery_timeout={recovery_timeout}s"
        )

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection

        Args:
            func: Function to execute (can be async or sync)
            *args, **kwargs: Arguments to pass to function

        Returns:
            Function result

        Raises:
            CircuitBreakerOpen: If circuit is open
            Original exception: If function fails
        """
        # Check if circuit is open
        if self.state == CircuitState.OPEN:
            if self._should_attempt_reset():
                self._transition_to_half_open()
            else:
                self.total_rejected += 1
                wait_time = self._time_until_retry()
                raise CircuitBreakerOpen(
                    f"Circuit '{self.name}' is open. "
                    f"Retry in {wait_time:.0f}s"
                )

        # Attempt to execute function
        try:
            # Handle both sync and async functions
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)

            self._on_success()
            return result

        except self.expected_exception as e:
            self._on_failure()
            raise
        except Exception as e:
            # Unexpected exception - don't count against circuit breaker
            logger.warning(
                f"Circuit '{self.name}' caught unexpected exception: {type(e).__name__}"
            )
            raise

    def _on_success(self):
        """Handle successful execution"""
        self.total_successes += 1

        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            logger.debug(
                f"Circuit '{self.name}' half-open success "
                f"({self.success_count}/{self.success_threshold})"
            )

            if self.success_count >= self.success_threshold:
                self._transition_to_closed()

        elif self.state == CircuitState.CLOSED:
            # Reset failure count on success
            if self.failure_count > 0:
                logger.debug(
                    f"Circuit '{self.name}' recovered, resetting failure count"
                )
                self.failure_count = 0

    def _on_failure(self):
        """Handle failed execution"""
        self.total_failures += 1
        self.last_failure_time = datetime.now()

        if self.state == CircuitState.HALF_OPEN:
            # Any failure in half-open state reopens circuit
            logger.warning(
                f"Circuit '{self.name}' failed in half-open state, reopening"
            )
            self._transition_to_open()

        elif self.state == CircuitState.CLOSED:
            self.failure_count += 1
            logger.debug(
                f"Circuit '{self.name}' failure "
                f"({self.failure_count}/{self.failure_threshold})"
            )

            if self.failure_count >= self.failure_threshold:
                self._transition_to_open()

    def _transition_to_open(self):
        """Transition to OPEN state"""
        self.state = CircuitState.OPEN
        self.last_state_change = datetime.now()
        self.success_count = 0

        logger.warning(
            f"Circuit '{self.name}' OPENED after {self.failure_count} failures. "
            f"Will retry in {self.recovery_timeout}s"
        )

    def _transition_to_half_open(self):
        """Transition to HALF_OPEN state"""
        self.state = CircuitState.HALF_OPEN
        self.last_state_change = datetime.now()
        self.failure_count = 0
        self.success_count = 0

        logger.info(f"Circuit '{self.name}' entering HALF_OPEN state (testing recovery)")

    def _transition_to_closed(self):
        """Transition to CLOSED state"""
        self.state = CircuitState.CLOSED
        self.last_state_change = datetime.now()
        self.failure_count = 0
        self.success_count = 0

        logger.info(f"Circuit '{self.name}' CLOSED (recovered)")

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt recovery"""
        if self.last_failure_time is None:
            return True

        elapsed = (datetime.now() - self.last_failure_time).total_seconds()
        return elapsed >= self.recovery_timeout

    def _time_until_retry(self) -> float:
        """Calculate seconds until next retry attempt"""
        if self.last_failure_time is None:
            return 0

        elapsed = (datetime.now() - self.last_failure_time).total_seconds()
        return max(0, self.recovery_timeout - elapsed)

    def get_state(self) -> dict:
        """Get current circuit breaker state"""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "total_rejected": self.total_rejected,
            "last_state_change": self.last_state_change.isoformat(),
            "time_in_state": (datetime.now() - self.last_state_change).total_seconds()
        }

    def reset(self):
        """Manually reset circuit breaker to CLOSED state"""
        logger.info(f"Circuit '{self.name}' manually reset")
        self._transition_to_closed()
