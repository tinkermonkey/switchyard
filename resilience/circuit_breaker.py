from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Callable, Any
import time

class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject calls
    HALF_OPEN = "half_open"  # Testing if service recovered

@dataclass
class CircuitBreaker:
    """
    Circuit breaker pattern implementation
    Not from a package - this is our custom implementation
    """
    failure_threshold: int = 3
    recovery_timeout: int = 60  # seconds
    expected_exception: type = Exception
    
    # Internal state (not set by user)
    _failure_count: int = field(default=0, init=False)
    _last_failure_time: Optional[float] = field(default=None, init=False)
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    
    def __post_init__(self):
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = None
    
    @property
    def state(self) -> str:
        return self._state.value
    
    def is_open(self) -> bool:
        """Check if circuit breaker is open"""
        if self._state == CircuitState.OPEN:
            # Check if we should try half-open
            if self._last_failure_time:
                if time.time() - self._last_failure_time >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    return False
            return True
        return False
    
    def should_attempt_reset(self) -> bool:
        """Check if we should attempt to reset (move to half-open)"""
        return (self._state == CircuitState.OPEN and 
                self._last_failure_time and
                time.time() - self._last_failure_time >= self.recovery_timeout)
    
    def enter_half_open(self):
        """Transition to half-open state"""
        self._state = CircuitState.HALF_OPEN
    
    def record_success(self):
        """Record successful call"""
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = None
    
    def record_failure(self):
        """Record failed call"""
        self._failure_count += 1
        self._last_failure_time = time.time()
        
        if self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
    
    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection
        """
        if self.is_open():
            raise Exception(f"Circuit breaker is OPEN (failures: {self._failure_count})")
        
        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except self.expected_exception as e:
            self.record_failure()
            raise e