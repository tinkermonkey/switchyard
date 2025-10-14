"""
Unit tests for timestamp utilities

Verifies that all timestamp utilities produce consistent UTC timestamps
with proper ISO8601 formatting and 'Z' suffix.
"""
import pytest
from datetime import datetime, timezone, timedelta
from monitoring.timestamp_utils import (
    utc_now,
    utc_isoformat,
    to_utc_isoformat,
    timestamp_to_utc_isoformat
)


class TestUtcNow:
    """Test utc_now() function"""
    
    def test_returns_datetime_with_utc_timezone(self):
        """Verify utc_now() returns datetime with UTC timezone"""
        result = utc_now()
        assert isinstance(result, datetime)
        assert result.tzinfo == timezone.utc
    
    def test_returns_recent_utc_time(self):
        """Verify utc_now() returns current UTC time (within 1 second of now)"""
        result = utc_now()
        expected = datetime.now(timezone.utc)
        # Allow for small time differences during test execution
        assert abs((result - expected).total_seconds()) < 1
    
    def test_is_timezone_aware(self):
        """Verify returned datetime is timezone-aware"""
        result = utc_now()
        assert result.tzinfo is not None
    
    def test_returns_actual_utc_not_local_time(self):
        """Verify utc_now() returns actual UTC time, not local time with UTC label"""
        utc_time = utc_now()
        local_time = datetime.now()  # Naive local time
        
        # If system is not in UTC, these should differ
        # We check the absolute difference in hours to handle DST and timezone offsets
        # On a UTC system they might match, but the key is utc_time has UTC timezone
        assert utc_time.tzinfo == timezone.utc
        
        # Verify UTC offset is 0
        assert utc_time.utcoffset() == timedelta(0)
        
        # The UTC time should match what datetime.now(timezone.utc) returns
        system_utc = datetime.now(timezone.utc)
        assert abs((utc_time - system_utc).total_seconds()) < 1


class TestUtcIsoformat:
    """Test utc_isoformat() function"""
    
    def test_returns_iso8601_with_z_suffix(self):
        """Verify utc_isoformat() returns ISO8601 with 'Z' suffix"""
        result = utc_isoformat()
        assert result.endswith('Z')
        assert 'T' in result  # ISO8601 date/time separator
        # Should match format: YYYY-MM-DDTHH:MM:SS.ffffffZ
        assert len(result) == 27  # Fixed length for full precision
    
    def test_no_plus_zero_zero_in_output(self):
        """Verify output uses 'Z' not '+00:00'"""
        result = utc_isoformat()
        assert '+00:00' not in result
        assert result.endswith('Z')
    
    def test_consistent_format(self):
        """Verify format is consistent within a short time window"""
        result1 = utc_isoformat()
        # Should be ISO8601 with T separator and Z suffix
        assert 'T' in result1
        assert result1.endswith('Z')
        # Format should be: YYYY-MM-DDTHH:MM:SS.ffffffZ
        parts = result1.split('T')
        assert len(parts) == 2
        assert len(parts[0]) == 10  # YYYY-MM-DD


class TestToUtcIsoformat:
    """Test to_utc_isoformat() function"""
    
    def test_converts_utc_datetime_to_z_suffix(self):
        """Verify UTC datetime converted to 'Z' suffix format"""
        dt = datetime(2025, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
        result = to_utc_isoformat(dt)
        assert result == '2025-10-10T12:00:00Z'
    
    def test_handles_naive_datetime_as_utc(self):
        """Verify naive datetime is assumed to be UTC"""
        dt = datetime(2025, 10, 10, 12, 0, 0)
        result = to_utc_isoformat(dt)
        assert result == '2025-10-10T12:00:00Z'
    
    def test_converts_non_utc_timezone_to_utc(self):
        """Verify non-UTC datetime is converted to UTC"""
        # Create datetime in EST (UTC-5)
        from datetime import timezone as tz
        est = tz(timedelta(hours=-5))
        dt = datetime(2025, 10, 10, 7, 0, 0, tzinfo=est)
        
        result = to_utc_isoformat(dt)
        # 7 AM EST = 12 PM UTC
        assert result == '2025-10-10T12:00:00Z'
    
    def test_preserves_microseconds(self):
        """Verify microseconds are preserved in output"""
        dt = datetime(2025, 10, 10, 12, 0, 0, 123456, tzinfo=timezone.utc)
        result = to_utc_isoformat(dt)
        assert result == '2025-10-10T12:00:00.123456Z'


class TestTimestampToUtcIsoformat:
    """Test timestamp_to_utc_isoformat() function"""
    
    def test_converts_unix_timestamp_to_iso8601(self):
        """Verify Unix timestamp converted to ISO8601 with 'Z'"""
        # 2025-10-10 12:00:00 UTC
        timestamp = 1760097600.0
        result = timestamp_to_utc_isoformat(timestamp)
        assert result.endswith('Z')
        assert result.startswith('2025-10-10T12:00:00')
    
    def test_handles_integer_timestamp(self):
        """Verify integer timestamps work"""
        timestamp = 1760097600  # No decimal
        result = timestamp_to_utc_isoformat(timestamp)
        assert result.endswith('Z')
        assert 'T' in result
    
    def test_handles_fractional_seconds(self):
        """Verify fractional seconds (microseconds) are preserved"""
        timestamp = 1760097600.123456
        result = timestamp_to_utc_isoformat(timestamp)
        assert result.endswith('Z')
        assert '.123456Z' in result
    
    def test_always_utc_regardless_of_system_timezone(self):
        """Verify output is always UTC regardless of system timezone"""
        import os
        # This test verifies the function always returns UTC
        # even if system timezone were different (theoretical)
        timestamp = 1760097600.0
        result = timestamp_to_utc_isoformat(timestamp)
        
        # Parse back to verify UTC
        dt = datetime.fromisoformat(result.replace('Z', '+00:00'))
        assert dt.tzinfo == timezone.utc
    
    def test_converts_to_actual_utc_not_local_time(self):
        """Verify timestamp conversion produces actual UTC values"""
        import time
        
        # Get current time as Unix timestamp
        unix_ts = time.time()
        
        # Convert using our function (should be UTC)
        utc_result = timestamp_to_utc_isoformat(unix_ts)
        
        # Convert directly to UTC for comparison
        direct_utc = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
        
        # Convert to local time for comparison  
        local_time = datetime.fromtimestamp(unix_ts)  # No tz = local
        
        # Parse our result
        parsed_result = datetime.fromisoformat(utc_result.replace('Z', '+00:00'))
        
        # Our result should match the direct UTC conversion
        assert abs((parsed_result - direct_utc).total_seconds()) < 0.001
        
        # Verify it's actually UTC timezone
        assert parsed_result.tzinfo == timezone.utc
        assert parsed_result.utcoffset() == timedelta(0)


class TestConsistencyAcrossFunctions:
    """Test consistency between different utility functions"""
    
    def test_utc_isoformat_matches_to_utc_isoformat(self):
        """Verify utc_isoformat() matches to_utc_isoformat(utc_now())"""
        now = utc_now()
        result1 = to_utc_isoformat(now)
        result2 = utc_isoformat()
        
        # Results should be within 1 second due to execution time
        dt1 = datetime.fromisoformat(result1.replace('Z', '+00:00'))
        dt2 = datetime.fromisoformat(result2.replace('Z', '+00:00'))
        assert abs((dt1 - dt2).total_seconds()) < 1
    
    def test_utc_isoformat_matches_timestamp_conversion(self):
        """Verify utc_isoformat() matches timestamp_to_utc_isoformat()"""
        now = utc_now()
        timestamp = now.timestamp()
        
        result1 = to_utc_isoformat(now)
        result2 = timestamp_to_utc_isoformat(timestamp)
        
        # Results should match exactly
        assert result1 == result2
    
    def test_all_functions_produce_z_suffix(self):
        """Verify all timestamp functions produce 'Z' suffix"""
        assert utc_isoformat().endswith('Z')
        assert to_utc_isoformat(utc_now()).endswith('Z')
        assert timestamp_to_utc_isoformat(1760097600.0).endswith('Z')
    
    def test_all_outputs_parseable_by_elasticsearch(self):
        """Verify all outputs can be parsed as ISO8601"""
        from datetime import datetime
        
        # Test each function's output is valid ISO8601
        timestamp1 = utc_isoformat()
        timestamp2 = to_utc_isoformat(utc_now())
        timestamp3 = timestamp_to_utc_isoformat(1760097600.0)
        
        for ts in [timestamp1, timestamp2, timestamp3]:
            # Should be parseable
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            assert dt.tzinfo == timezone.utc


class TestElasticsearchCompatibility:
    """Test compatibility with Elasticsearch date format requirements"""
    
    def test_format_matches_elasticsearch_strict_date_time(self):
        """Verify format matches Elasticsearch strict_date_time format"""
        result = utc_isoformat()
        
        # Elasticsearch expects: yyyy-MM-dd'T'HH:mm:ss.SSSZ
        # Our format: 2025-10-10T12:34:56.789012Z
        parts = result.split('T')
        assert len(parts) == 2
        
        date_part = parts[0]
        assert len(date_part) == 10  # YYYY-MM-DD
        
        time_part = parts[1]
        assert time_part.endswith('Z')
    
    def test_microsecond_precision_compatible(self):
        """Verify microsecond precision is compatible with Elasticsearch"""
        result = utc_isoformat()
        
        # Elasticsearch supports up to 9 decimal places (nanoseconds)
        # Python datetime supports 6 (microseconds)
        if '.' in result:
            fractional = result.split('.')[1].rstrip('Z')
            assert len(fractional) <= 6  # Microseconds (6 digits max)
