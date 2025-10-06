# Circuit Breaker Implementation

## Overview

Added circuit breaker pattern to prevent cascading failures in the pattern ingestion pipeline. Circuit breakers automatically detect failing services and stop making requests to them, allowing time for recovery.

## Architecture

### Circuit Breaker States

1. **CLOSED** (Green) - Normal operation, all requests pass through
2. **OPEN** (Red) - Too many failures, reject all requests
3. **HALF_OPEN** (Yellow) - Testing recovery, allow limited requests

### State Transitions

```
CLOSED --[5 failures]--> OPEN
OPEN --[30s timeout]--> HALF_OPEN
HALF_OPEN --[2 successes]--> CLOSED
HALF_OPEN --[any failure]--> OPEN
```

## Implementation

### Three Circuit Breakers Deployed

#### 1. Redis Streams Circuit Breaker
- **Name**: `redis_streams`
- **Location**: `services/log_collector.py`
- **Protects**: Redis stream consumption (XREADGROUP operations)
- **Config**:
  - Failure threshold: 5 consecutive failures
  - Recovery timeout: 30 seconds
  - Expected exception: `redis.ResponseError`

#### 2. Elasticsearch Indexing Circuit Breaker
- **Name**: `elasticsearch_indexing`
- **Location**: `services/log_collector.py`
- **Protects**: Bulk indexing operations to Elasticsearch
- **Config**:
  - Failure threshold: 3 consecutive failures
  - Recovery timeout: 60 seconds
  - Buffers events in memory when circuit is open

#### 3. Pattern Detection Queries Circuit Breaker
- **Name**: `pattern_detection_queries`
- **Location**: `services/pattern_detector_es.py`
- **Protects**: Elasticsearch queries for pattern detection
- **Config**:
  - Failure threshold: 3 consecutive failures
  - Recovery timeout: 60 seconds
  - Skips pattern detection rules when circuit is open

## Benefits

### Before Circuit Breakers
- Tight retry loops (100ms) during outages
- Log spam (thousands of error messages)
- Resource exhaustion from repeated failed requests
- No visibility into service health
- Cascading failures across components

### After Circuit Breakers
- **Fail Fast**: Stop trying immediately after threshold
- **Auto Recovery**: Test recovery after timeout
- **Resource Protection**: Prevent hammering failing services
- **Clean Logs**: Debug-level messages when circuit open
- **Health Visibility**: Circuit state exposed via metrics

## Monitoring

### Log Messages

Circuit breaker state changes are logged:

```
INFO - Circuit breaker 'redis_streams' initialized: failure_threshold=5, recovery_timeout=30s
WARNING - Circuit 'redis_streams' OPENED after 5 failures. Will retry in 30s
INFO - Circuit 'redis_streams' entering HALF_OPEN state (testing recovery)
INFO - Circuit 'redis_streams' CLOSED (recovered)
```

### API Endpoint

Circuit breaker state is exposed via the pattern ingestion service stats:

```json
{
  "health": {
    "redis_circuit": "closed",
    "elasticsearch_indexing_circuit": "closed",
    "pattern_detection_circuit": "closed"
  },
  "log_collector": {
    "circuit_breakers": {
      "redis": {
        "name": "redis_streams",
        "state": "closed",
        "failure_count": 0,
        "total_failures": 0,
        "total_successes": 1234,
        "total_rejected": 0,
        "time_in_state": 3600.5
      },
      "elasticsearch": { ... }
    }
  },
  "pattern_detector": {
    "circuit_breaker": { ... }
  }
}
```

## Code Structure

### Core Implementation

**`services/circuit_breaker.py`** - Reusable circuit breaker class
- 250 lines
- Async/sync function support
- State machine with transitions
- Metrics tracking
- Configurable thresholds

### Integration Points

1. **Log Collector** (`services/log_collector.py`)
   - Lines 90-101: Circuit breaker initialization
   - Lines 162-170: Redis consumption protection
   - Lines 315-324: Elasticsearch indexing protection

2. **Pattern Detector** (`services/pattern_detector_es.py`)
   - Lines 206-212: Circuit breaker initialization
   - Lines 282-286: Query protection with graceful degradation

## Example Failure Scenario

### Redis Outage

1. Redis becomes unavailable
2. After 5 consecutive XREADGROUP failures: **Circuit OPENS**
3. Log collector stops trying Redis, logs at debug level
4. Events buffer in memory (up to batch size)
5. After 30 seconds: **Circuit enters HALF_OPEN**
6. Next successful read: **Circuit CLOSES**
7. Normal operation resumes

### What Prevented
- ❌ Thousands of error log lines every 100ms
- ❌ CPU/network exhaustion from repeated failures
- ❌ Blocking other services waiting for Redis
- ❌ Lost visibility in logs due to noise

### What Happened Instead
- ✅ Clean "Circuit open, retry in 30s" message
- ✅ Service continues processing Elasticsearch side
- ✅ Auto-recovery when Redis returns
- ✅ Zero manual intervention needed

## Testing

### Manual Test

Simulate Redis failure:
```bash
docker-compose stop redis
# Watch logs - circuit should open after 5 failures
docker-compose logs pattern-ingestion --follow

# Start Redis
docker-compose start redis
# Circuit should recover after 30s
```

### Expected Log Output

```
2025-10-06 13:48:00 - Circuit 'redis_streams' failure (5/5)
2025-10-06 13:48:00 - WARNING - Circuit 'redis_streams' OPENED after 5 failures. Will retry in 30s
2025-10-06 13:48:00 - DEBUG - Redis circuit open for agent events: Circuit 'redis_streams' is open. Retry in 30s
2025-10-06 13:48:30 - INFO - Circuit 'redis_streams' entering HALF_OPEN state (testing recovery)
2025-10-06 13:48:31 - INFO - Circuit 'redis_streams' CLOSED (recovered)
```

## Configuration

### Tuning Circuit Breakers

Edit the initialization in each service:

```python
self.redis_breaker = CircuitBreaker(
    name="redis_streams",
    failure_threshold=5,      # Increase for less sensitive
    recovery_timeout=30,      # Increase for longer backoff
    success_threshold=2,      # Successes needed to close
    expected_exception=redis.ResponseError
)
```

### Recommended Settings

| Service | Failure Threshold | Recovery Timeout | Notes |
|---------|------------------|------------------|-------|
| Redis Streams | 5 | 30s | Quick recovery for streams |
| ES Indexing | 3 | 60s | Longer timeout for bulk ops |
| Pattern Detection | 3 | 60s | Non-critical, can wait |

## Future Enhancements

1. **Metrics Export** - Expose to Prometheus/Grafana
2. **Alert Integration** - Notify when circuits open
3. **Manual Reset** - API endpoint to force circuit closed
4. **Adaptive Timeouts** - Increase timeout on repeated failures
5. **Bulkhead Pattern** - Isolate resource pools
