"""
Timestamp utilities for consistent UTC timestamp handling across the orchestrator.

All Elasticsearch writes must use UTC timestamps to ensure:
- Correct time-range queries across different data sources
- Accurate time-based aggregations
- Proper ILM policy execution
- Event correlation across services
"""
from datetime import datetime, timezone


def utc_now() -> datetime:
    """
    Get current time in UTC with timezone awareness.
    
    Returns:
        datetime: Current UTC time with timezone info
        
    Example:
        >>> timestamp = utc_now()
        >>> timestamp.tzinfo == timezone.utc
        True
    """
    return datetime.now(timezone.utc)


def utc_isoformat() -> str:
    """
    Get current time as ISO8601 string with 'Z' suffix indicating UTC.
    
    Returns:
        str: ISO8601 formatted timestamp with 'Z' suffix (e.g., '2025-10-10T12:34:56.789012Z')
        
    Example:
        >>> timestamp = utc_isoformat()
        >>> timestamp.endswith('Z')
        True
    """
    # Replace '+00:00' with 'Z' for proper ISO8601 UTC format
    return utc_now().isoformat().replace('+00:00', 'Z')


def to_utc_isoformat(dt: datetime) -> str:
    """
    Convert a datetime object to ISO8601 string with 'Z' suffix.
    
    Args:
        dt: datetime object (will be converted to UTC if needed)
        
    Returns:
        str: ISO8601 formatted timestamp with 'Z' suffix
        
    Example:
        >>> dt = datetime(2025, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
        >>> to_utc_isoformat(dt)
        '2025-10-10T12:00:00Z'
    """
    if dt.tzinfo is None:
        # Naive datetime - assume UTC
        dt = dt.replace(tzinfo=timezone.utc)
    elif dt.tzinfo != timezone.utc:
        # Convert to UTC
        dt = dt.astimezone(timezone.utc)
    
    return dt.isoformat().replace('+00:00', 'Z')


def timestamp_to_utc_isoformat(timestamp: float) -> str:
    """
    Convert Unix timestamp to ISO8601 string with 'Z' suffix.
    
    Args:
        timestamp: Unix timestamp (seconds since epoch)
        
    Returns:
        str: ISO8601 formatted timestamp with 'Z' suffix
        
    Example:
        >>> timestamp_to_utc_isoformat(1728561600.0)
        '2025-10-10T12:00:00Z'
    """
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.isoformat().replace('+00:00', 'Z')
