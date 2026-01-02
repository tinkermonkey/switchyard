"""
Circuit Breaker Pattern Implementation

Prevents cascading failures by stopping requests to failing services
and allowing them time to recover.
"""

import asyncio
import logging
import redis
import json
import os
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
    failure_threshold: int = 3        # Failures before opening circuit
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
        failure_threshold: int = 3,
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

        # Redis client for state persistence
        self.redis_client = None
        
        # Try configured host first, then fallbacks
        redis_host = os.environ.get('REDIS_HOST')
        redis_port = int(os.environ.get('REDIS_PORT', 6379))
        
        hosts_to_try = [redis_host] if redis_host else ['redis', 'localhost', '127.0.0.1']
        
        for host in hosts_to_try:
            try:
                self.redis_client = redis.Redis(
                    host=host,
                    port=redis_port,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2
                )
                self.redis_client.ping()
                logger.debug(f"Circuit breaker '{name}' connected to Redis at {host}:{redis_port}")
                break
            except Exception as e:
                logger.debug(f"Could not connect to Redis at {host}:{redis_port}: {e}")
                self.redis_client = None
                continue

        if not self.redis_client:
            logger.warning(f"Redis unavailable for circuit breaker '{name}' persistence")
            self.redis_client = None

        # State tracking (defaults)
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[datetime] = None
        self.last_state_change: datetime = datetime.now()

        # Metrics
        self.total_failures = 0
        self.total_successes = 0
        self.total_rejected = 0

        # Try to restore state from Redis
        self._load_state()

        logger.info(
            f"Circuit breaker '{name}' initialized: "
            f"failure_threshold={failure_threshold}, "
            f"recovery_timeout={recovery_timeout}s, "
            f"state={self.state.value}"
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
            logger.info(  # Changed from debug to info for visibility
                f"Circuit '{self.name}' half-open success "
                f"({self.success_count}/{self.success_threshold})"
            )

            # CRITICAL FIX: Save state immediately after increment
            # This ensures new circuit breaker instances see the accumulated count
            self._save_state()

            if self.success_count >= self.success_threshold:
                self._transition_to_closed()

        elif self.state == CircuitState.CLOSED:
            # Reset failure count on success
            if self.failure_count > 0:
                logger.debug(
                    f"Circuit '{self.name}' recovered, resetting failure count"
                )
                self.failure_count = 0
                # CRITICAL FIX: Save state when resetting failure count
                # This prevents accumulation of stale failure counts
                self._save_state()

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
        self._save_state()  # Persist state change

    def _transition_to_half_open(self):
        """Transition to HALF_OPEN state"""
        self.state = CircuitState.HALF_OPEN
        self.last_state_change = datetime.now()
        self.failure_count = 0
        self.success_count = 0

        logger.info(f"Circuit '{self.name}' entering HALF_OPEN state (testing recovery)")
        self._save_state()  # Persist state change

    def _transition_to_closed(self):
        """Transition to CLOSED state"""
        self.state = CircuitState.CLOSED
        self.last_state_change = datetime.now()
        self.failure_count = 0
        self.success_count = 0

        logger.info(f"Circuit '{self.name}' CLOSED (recovered)")
        self._save_state()  # Persist state change

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

    def _save_state(self):
        """Save circuit breaker state to Redis for persistence across restarts."""
        if not self.redis_client:
            return

        try:
            state_data = {
                "state": self.state.value,
                "failure_count": self.failure_count,
                "success_count": self.success_count,
                "last_failure_time": self.last_failure_time.isoformat() if self.last_failure_time else None,
                "last_state_change": self.last_state_change.isoformat(),
                "total_failures": self.total_failures,
                "total_successes": self.total_successes,
                "total_rejected": self.total_rejected
            }

            # Store with 24-hour TTL (circuit breaker state shouldn't persist indefinitely)
            self.redis_client.setex(
                f"circuit_breaker:{self.name}:state",
                86400,  # 24 hours
                json.dumps(state_data)
            )

            # Log state save for debugging (use debug level to avoid spam)
            logger.debug(
                f"Circuit '{self.name}' state saved to Redis: "
                f"state={self.state.value}, "
                f"success_count={self.success_count}, "
                f"failure_count={self.failure_count}"
            )
        except Exception as e:
            logger.warning(f"Failed to save circuit breaker '{self.name}' state to Redis: {e}")

    def _load_state(self):
        """Load circuit breaker state from Redis if available."""
        if not self.redis_client:
            return

        try:
            state_json = self.redis_client.get(f"circuit_breaker:{self.name}:state")
            if not state_json:
                logger.debug(f"No saved state found for circuit breaker '{self.name}'")
                return

            state_data = json.loads(state_json)

            # Restore state
            self.state = CircuitState(state_data["state"])
            self.failure_count = state_data["failure_count"]
            self.success_count = state_data["success_count"]
            self.last_failure_time = (
                datetime.fromisoformat(state_data["last_failure_time"])
                if state_data["last_failure_time"]
                else None
            )
            self.last_state_change = datetime.fromisoformat(state_data["last_state_change"])
            self.total_failures = state_data["total_failures"]
            self.total_successes = state_data["total_successes"]
            self.total_rejected = state_data["total_rejected"]

            # Enhanced logging for HALF_OPEN state to track success accumulation
            if self.state == CircuitState.HALF_OPEN:
                logger.info(
                    f"Circuit breaker '{self.name}' state restored from Redis: "
                    f"state={self.state.value}, "
                    f"success_count={self.success_count}/{self.success_threshold}, "
                    f"failure_count={self.failure_count}"
                )
            else:
                logger.info(
                    f"Circuit breaker '{self.name}' state restored from Redis: "
                    f"state={self.state.value}, failures={self.failure_count}"
                )
        except Exception as e:
            logger.warning(f"Failed to load circuit breaker '{self.name}' state from Redis: {e}")
            # Keep default state on load failure
