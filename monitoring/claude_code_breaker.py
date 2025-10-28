"""
Circuit breaker for Claude Code token limits.

Detects when Claude Code hits token limits and prevents agent execution
until tokens are available again.

Uses Redis for state persistence so that:
- Breaker state survives application restarts
- State is consistent across multiple processes
"""

import logging
import re
import json
from datetime import datetime, timedelta
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class ClaudeCodeBreaker:
    """
    Circuit breaker for Claude Code token limits.
    
    States:
    - CLOSED: Normal operation, agents can run
    - OPEN: Token limit reached, no agents should run
    - HALF_OPEN: Testing if tokens are available again
    
    State is persisted to Redis for resilience across restarts.
    """
    
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"
    
    # Redis key for persisting breaker state
    REDIS_KEY = "orchestrator:claude_code_breaker:state"
    
    # Pattern to detect session limit messages in multiple formats:
    # - "Session limit reached ∙ resets 12am" (in assistant content)
    # - "session limit exceeded. Reset at 1:30pm"
    # - "SESSION_LIMIT_IN_RESPONSE: ..."
    # - "token limit reached/exceeded" (with action verb)
    # - "Session limit reached" (without reset time)
    #
    # IMPORTANT: Must be specific to avoid false positives!
    # Only match specific limit types (session/token/rate/request/usage) with action verbs
    # Don't match generic "limit reached" which appears in documentation
    SESSION_LIMIT_PATTERN = re.compile(
        r'(?:SESSION_LIMIT_IN_RESPONSE:\s+)?(?:'
        r'(?:session|token|request|rate|usage)\s+(?:limit|quota)\s+(?:reached|exceeded|hit|met|violated)'
        r'|(?:reached|exceeded|hit|met|violated)\s+(?:session|token|request|rate|usage)\s+(?:limit|quota)'
        r')(?:.*?(?:resets?|reset)(?:\s+at)?\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?)?',
        re.IGNORECASE | re.DOTALL
    )
    
    def __init__(self):
        self.state = self.CLOSED
        self.opened_at: Optional[datetime] = None
        self.reset_time: Optional[datetime] = None
        self.failure_count = 0
        self.max_failures = 1  # Trip after 1 token limit error
        self.redis_client = None
        
        # Try to initialize Redis client
        try:
            import redis
            self.redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
            self.redis_client.ping()
            # Load persisted state from Redis
            self._load_from_redis()
            logger.info(f"Claude Code breaker initialized with Redis persistence. State: {self.state}")
        except Exception as e:
            logger.warning(f"Could not connect to Redis for breaker persistence: {e}. Using in-memory state.")
            self.redis_client = None
    
    def _load_from_redis(self):
        """Load breaker state from Redis if it exists."""
        try:
            if not self.redis_client:
                return
            
            state_json = self.redis_client.get(self.REDIS_KEY)
            if state_json:
                state_dict = json.loads(state_json)
                self.state = state_dict.get('state', self.CLOSED)
                
                # Parse ISO format datetime strings
                opened_at_str = state_dict.get('opened_at')
                if opened_at_str:
                    self.opened_at = datetime.fromisoformat(opened_at_str)
                
                reset_time_str = state_dict.get('reset_time')
                if reset_time_str:
                    self.reset_time = datetime.fromisoformat(reset_time_str)
                
                self.failure_count = state_dict.get('failure_count', 0)
                
                logger.info(
                    f"Loaded breaker state from Redis: state={self.state}, "
                    f"opened_at={self.opened_at}, reset_time={self.reset_time}"
                )
        except Exception as e:
            logger.error(f"Error loading breaker state from Redis: {e}")
    
    def _save_to_redis(self):
        """Save breaker state to Redis."""
        try:
            if not self.redis_client:
                return
            
            state_dict = {
                'state': self.state,
                'opened_at': self.opened_at.isoformat() if self.opened_at else None,
                'reset_time': self.reset_time.isoformat() if self.reset_time else None,
                'failure_count': self.failure_count,
            }
            
            self.redis_client.set(
                self.REDIS_KEY,
                json.dumps(state_dict),
                ex=86400  # Expire after 24 hours
            )
            
            logger.debug(f"Saved breaker state to Redis: {state_dict}")
        except Exception as e:
            logger.error(f"Error saving breaker state to Redis: {e}")
    
    def detect_session_limit(self, message: str) -> Tuple[bool, Optional[datetime]]:
        """
        Detect if a message contains session limit reached error.
        
        Returns:
            Tuple of (is_session_limited, reset_datetime)
        """
        if not message:
            return False, None
            
        match = self.SESSION_LIMIT_PATTERN.search(message)
        if not match:
            return False, None
        
        try:
            # group(1) is the reset time (if present)
            reset_time_str = match.group(1) if match.lastindex and match.lastindex >= 1 else None
            
            if reset_time_str:
                reset_time = self._parse_reset_time(reset_time_str.strip())
                logger.warning(f"🔴 Detected Claude Code session limit. Resets at: {reset_time}")
                return True, reset_time
            else:
                # No reset time in message, assume 1 hour
                logger.warning(f"🔴 Detected Claude Code session limit (no reset time found)")
                reset_time = datetime.now() + timedelta(hours=1)
                return True, reset_time
        except Exception as e:
            logger.error(f"Failed to parse reset time from message: {e}")
            # Still consider it a session limit even if we can't parse time
            reset_time = datetime.now() + timedelta(hours=1)
            return True, reset_time
    
    def _parse_reset_time(self, time_str: str) -> datetime:
        """Parse reset time string like '12am', '1:30pm', etc."""
        now = datetime.now()
        
        # Remove whitespace and convert to lowercase
        time_str = time_str.strip().lower()
        
        # Try to parse with various formats
        for fmt in ['%I%p', '%I:%M%p', '%I %p', '%I:%M %p']:
            try:
                parsed = datetime.strptime(time_str, fmt)
                # Combine with today's date
                reset_dt = now.replace(
                    hour=parsed.hour,
                    minute=parsed.minute,
                    second=0,
                    microsecond=0
                )
                
                # If reset time is in the past, it must be tomorrow
                if reset_dt <= now:
                    reset_dt += timedelta(days=1)
                
                return reset_dt
            except ValueError:
                continue
        
        # If we couldn't parse, assume 1 hour from now
        logger.warning(f"Could not parse reset time: {time_str}, assuming 1 hour")
        return now + timedelta(hours=1)
    
    def trip(self, reset_time: Optional[datetime] = None):
        """
        Trip the circuit breaker (open it).
        
        Args:
            reset_time: When tokens should be available again
        """
        if self.state == self.CLOSED:
            self.state = self.OPEN
            self.opened_at = datetime.now()
            self.reset_time = reset_time or (datetime.now() + timedelta(hours=1))
            self.failure_count = 0
            
            # Save to Redis immediately
            self._save_to_redis()
            
            logger.error(
                f"🔴 CLAUDE CODE BREAKER OPENED - Session limit reached. "
                f"Will reset at {self.reset_time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
    
    def check_and_close(self) -> bool:
        """
        Check if tokens are available again and close the breaker if so.
        Also checks Redis to detect if breaker was externally reset.

        Returns:
            True if breaker is now closed, False if still open
        """
        # First check if breaker was externally reset via Redis
        if self.state != self.CLOSED and self.redis_client:
            try:
                state_json = self.redis_client.get(self.REDIS_KEY)
                if not state_json:
                    # Redis key deleted = breaker was externally closed
                    logger.info("🟢 Detected external breaker reset via Redis - closing breaker")
                    self.state = self.CLOSED
                    self.opened_at = None
                    self.reset_time = None
                    self.failure_count = 0
                    return True
            except Exception as e:
                logger.debug(f"Error checking Redis for external reset: {e}")

        if self.state == self.CLOSED:
            return True

        if self.reset_time and datetime.now() >= self.reset_time:
            self.state = self.HALF_OPEN
            self._save_to_redis()
            logger.warning(
                "🟡 CLAUDE CODE BREAKER HALF-OPEN - Testing token availability..."
            )
            return False  # Still not fully closed, need successful test

        return False
    
    def close(self):
        """Close the breaker (tokens are available)."""
        if self.state != self.CLOSED:
            self.state = self.CLOSED
            self.opened_at = None
            self.reset_time = None
            self.failure_count = 0

            # Save to Redis and clear the key
            if self.redis_client:
                try:
                    self.redis_client.delete(self.REDIS_KEY)
                    logger.debug(f"Cleared breaker state from Redis")
                except Exception as e:
                    logger.error(f"Error clearing breaker state from Redis: {e}")

            logger.info("🟢 CLAUDE CODE BREAKER CLOSED - Tokens available, resuming operations")
    
    def is_open(self) -> bool:
        """Check if breaker is open (agents cannot run).
        Also checks Redis to detect external resets."""
        # Check Redis for external reset before checking state
        if self.state == self.OPEN:
            self.check_and_close()  # Will detect and apply external reset
        return self.state == self.OPEN
    
    def is_half_open(self) -> bool:
        """Check if breaker is half-open (testing availability)."""
        return self.state == self.HALF_OPEN
    
    def get_status(self) -> dict:
        """Get current breaker status.
        Syncs from Redis first to ensure accuracy across processes."""
        # Sync from Redis to get latest state (especially important for observability server)
        if self.redis_client:
            try:
                state_json = self.redis_client.get(self.REDIS_KEY)
                if state_json:
                    # Load latest state from Redis
                    state_dict = json.loads(state_json)
                    self.state = state_dict.get('state', self.CLOSED)

                    opened_at_str = state_dict.get('opened_at')
                    self.opened_at = datetime.fromisoformat(opened_at_str) if opened_at_str else None

                    reset_time_str = state_dict.get('reset_time')
                    self.reset_time = datetime.fromisoformat(reset_time_str) if reset_time_str else None

                    self.failure_count = state_dict.get('failure_count', 0)
                elif self.state != self.CLOSED:
                    # Redis key missing but we think we're open = external reset
                    logger.debug("Redis key missing in get_status, closing breaker")
                    self.state = self.CLOSED
                    self.opened_at = None
                    self.reset_time = None
                    self.failure_count = 0
            except Exception as e:
                logger.debug(f"Could not sync from Redis in get_status: {e}")

        return {
            'state': self.state,
            'opened_at': self.opened_at.isoformat() if self.opened_at else None,
            'reset_time': self.reset_time.isoformat() if self.reset_time else None,
            'time_until_reset': (
                (self.reset_time - datetime.now()).total_seconds()
                if self.reset_time and self.state != self.CLOSED
                else None
            ),
            'is_open': self.is_open(),
            'failure_count': self.failure_count,
        }


# Global breaker instance
_breaker: Optional[ClaudeCodeBreaker] = None


def get_breaker() -> ClaudeCodeBreaker:
    """Get or create the global Claude Code breaker."""
    global _breaker
    if _breaker is None:
        _breaker = ClaudeCodeBreaker()
    return _breaker


def check_breaker_before_agent_execution(agent_name: str) -> Tuple[bool, Optional[str]]:
    """
    Check if agent can be executed given current breaker state.
    
    Returns:
        Tuple of (can_execute, error_message)
    """
    breaker = get_breaker()
    
    if not breaker.is_open() and not breaker.is_half_open():
        return True, None
    
    reset_time = breaker.reset_time
    if reset_time:
        time_until = (reset_time - datetime.now()).total_seconds()
        message = (
            f"Claude Code token limit reached. Agent '{agent_name}' cannot run. "
            f"Tokens reset in {time_until:.0f} seconds at {reset_time.strftime('%I:%M %p')}"
        )
    else:
        message = (
            f"Claude Code token limit reached. Agent '{agent_name}' cannot run. "
            f"Awaiting token reset."
        )
    
    return False, message
