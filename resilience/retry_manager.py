# sdlc-orchestrator/resilience/retry_manager.py
import asyncio
from typing import Callable, Any, Optional
from functools import wraps
import random
import time
import logging
from .circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

class RetryManager:
    """Manages retries with exponential backoff and circuit breaker integration"""
    
    @staticmethod
    def exponential_backoff_with_jitter(
        attempt: int,
        initial_delay: float = 1.0,
        backoff_factor: float = 2.0,
        max_delay: float = 60.0
    ) -> float:
        """Calculate delay with exponential backoff and jitter"""
        delay = min(initial_delay * (backoff_factor ** attempt), max_delay)
        # Add jitter to prevent thundering herd
        jitter = random.uniform(0, delay * 0.1)
        return delay + jitter
    
    @staticmethod
    def with_retry(
        max_attempts: int = 3,
        initial_delay: float = 1.0,
        backoff_factor: float = 2.0,
        max_delay: float = 60.0,
        circuit_breaker: Optional[CircuitBreaker] = None
    ):
        """
        Decorator for retryable operations with optional circuit breaker
        """
        def decorator(func: Callable) -> Callable:
            @wraps(func)
            async def async_wrapper(*args, **kwargs) -> Any:
                attempt = 0
                last_exception = None
                
                while attempt < max_attempts:
                    try:
                        # Check circuit breaker first
                        if circuit_breaker and circuit_breaker.is_open():
                            raise Exception(f"Circuit breaker OPEN for {func.__name__}")
                        
                        # Try the function
                        result = await func(*args, **kwargs)
                        
                        # Record success if we have circuit breaker
                        if circuit_breaker:
                            circuit_breaker.record_success()
                        
                        return result
                        
                    except Exception as e:
                        last_exception = e
                        attempt += 1
                        
                        # Record failure if we have circuit breaker
                        if circuit_breaker:
                            circuit_breaker.record_failure()
                        
                        if attempt >= max_attempts:
                            logger.error(f"{func.__name__} failed after {max_attempts} attempts")
                            raise e
                        
                        # Calculate delay
                        delay = RetryManager.exponential_backoff_with_jitter(
                            attempt, initial_delay, backoff_factor, max_delay
                        )
                        
                        logger.warning(f"Attempt {attempt}/{max_attempts} failed: {e}")
                        logger.info(f"Retrying in {delay:.2f} seconds...")
                        
                        await asyncio.sleep(delay)
                
                raise last_exception
            
            @wraps(func)
            def sync_wrapper(*args, **kwargs) -> Any:
                """Synchronous version of retry wrapper"""
                attempt = 0
                last_exception = None
                
                while attempt < max_attempts:
                    try:
                        if circuit_breaker and circuit_breaker.is_open():
                            raise Exception(f"Circuit breaker OPEN for {func.__name__}")
                        
                        result = func(*args, **kwargs)
                        
                        if circuit_breaker:
                            circuit_breaker.record_success()
                        
                        return result
                        
                    except Exception as e:
                        last_exception = e
                        attempt += 1
                        
                        if circuit_breaker:
                            circuit_breaker.record_failure()
                        
                        if attempt >= max_attempts:
                            raise e
                        
                        delay = RetryManager.exponential_backoff_with_jitter(
                            attempt, initial_delay, backoff_factor, max_delay
                        )
                        
                        time.sleep(delay)
                
                raise last_exception
            
            # Return async or sync wrapper based on function type
            if asyncio.iscoroutinefunction(func):
                return async_wrapper
            else:
                return sync_wrapper
                
        return decorator