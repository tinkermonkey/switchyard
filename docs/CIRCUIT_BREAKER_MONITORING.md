# Circuit Breaker Monitoring in Observability Dashboard

## Overview

Circuit breaker status is now prominently displayed in the observability dashboard header, providing immediate visibility into service health.

## Architecture

### Data Flow

```
Pattern Ingestion Service
  ↓ (publishes every 5s)
Redis Key: orchestrator:pattern_ingestion_stats
  ↓ (reads)
Observability Server API: /api/circuit-breakers
  ↓ (polls every 5s)
Header Component
  ↓ (displays)
Alert Banner (when circuits open)
```

### Components

**1. Pattern Ingestion Service** (`services/pattern_ingestion_service.py`)
- Publishes circuit breaker stats to Redis every 5 seconds
- Stats include all 3 circuit breakers with full state
- Key: `orchestrator:pattern_ingestion_stats`
- TTL: 30 seconds (auto-expires if service dies)

**2. Observability Server API** (`services/observability_server.py`)
- New endpoint: `GET /api/circuit-breakers`
- Reads stats from Redis
- Transforms into user-friendly format
- Returns summary counts (open, half_open, healthy)

**3. Header Component** (`web_ui_v2/src/components/Header.jsx`)
- Polls circuit breaker API every 5 seconds
- Displays prominent alert banner when circuits open
- Shows "All Systems Operational" badge when healthy
- Color-coded by severity (red=open, yellow=half_open, green=closed)

## UI Features

### Alert Banner

**Shown when any circuit is open or half-open:**

- **Red banner** - One or more circuits OPEN
  - Shows count of open circuits
  - Lists each open circuit with:
    - Circuit name
    - Current state
    - Total rejected requests

- **Yellow banner** - Circuits in HALF_OPEN (testing recovery)
  - Shows count of circuits testing recovery
  - Lists each circuit with status

### Status Badges

**Next to connection status:**

- **"All Systems Operational"** (Green) - All circuits closed
- No badge shown when circuits are degraded (relies on alert banner)

### Circuit Details

Each non-healthy circuit shows:
- Icon indicating state (⚠️ open, ⚠ half-open)
- Circuit name (e.g., "Redis Streams")
- State in uppercase
- Additional context:
  - OPEN: Number of rejected requests
  - HALF_OPEN: "Testing..." status

## API Response Format

```json
{
  "success": true,
  "circuit_breakers": [
    {
      "name": "Redis Streams",
      "service": "log_collector",
      "state": "closed",
      "failure_count": 0,
      "total_failures": 0,
      "total_successes": 1154,
      "total_rejected": 0,
      "time_in_state": 181.09
    },
    {
      "name": "Elasticsearch Indexing",
      "service": "log_collector",
      "state": "closed",
      ...
    },
    {
      "name": "Pattern Detection Queries",
      "service": "pattern_detector",
      "state": "closed",
      ...
    }
  ],
  "summary": {
    "total": 3,
    "open": 0,
    "half_open": 0,
    "healthy": 3
  }
}
```

## Monitored Circuit Breakers

| Circuit Breaker | Service | Monitors |
|----------------|---------|----------|
| Redis Streams | log_collector | XREADGROUP operations on Redis |
| Elasticsearch Indexing | log_collector | Bulk indexing to ES |
| Pattern Detection Queries | pattern_detector | ES queries for patterns |

## Visual Indicators

### State Colors

| State | Color | Icon | Meaning |
|-------|-------|------|---------|
| closed | Green | ✓ | Healthy, requests flowing |
| half_open | Yellow | ⚠ | Testing recovery |
| open | Red | ⚠️ | Failing, rejecting requests |

### Banner Colors

- **Red background** (`bg-red-50`/`dark:bg-red-900/20`) - Critical: Circuits open
- **Yellow background** (`bg-yellow-50`/`dark:bg-yellow-900/20`) - Warning: Recovery testing

## Testing

### Manual Test

1. **Stop Redis** to trigger circuit breakers:
   ```bash
   docker-compose stop redis
   ```

2. **Watch dashboard** - After ~30s (5 failures at 5s intervals):
   - Red alert banner appears
   - "Redis Streams" circuit shows as OPEN
   - Rejected request count increments

3. **Restart Redis**:
   ```bash
   docker-compose start redis
   ```

4. **Watch recovery** - After 30s timeout:
   - Circuit transitions to HALF_OPEN (yellow banner)
   - After 2 successful requests: Circuit CLOSES
   - Green "All Systems Operational" badge returns

### API Testing

```bash
# Test circuit breaker API directly
curl http://localhost:5001/api/circuit-breakers | jq

# Check Redis stats key
docker-compose exec redis redis-cli GET orchestrator:pattern_ingestion_stats | jq
```

## Performance

- **Stats published**: Every 5 seconds to Redis
- **UI polls**: Every 5 seconds via API
- **Redis TTL**: 30 seconds (fails gracefully if service dies)
- **Latency**: <10ms API response time
- **Network impact**: Minimal (~2KB per poll)

## Error Handling

### Service Unavailable

If pattern-ingestion service is down:
- API returns 503 status
- UI shows connection error in console
- No alert banner (doesn't spam user)
- Last known state remains visible

### Redis Unavailable

If Redis is down:
- Pattern-ingestion service circuit breakers kick in
- Stats stop publishing
- After 30s: Stale stats expire from Redis
- API returns 503 "stats not available"

## Future Enhancements

1. **Historical Circuit State** - Track circuit open/close events over time
2. **Alert Notifications** - Browser notifications when circuits open
3. **Manual Reset** - Admin button to force circuit closed
4. **Circuit Health Page** - Dedicated page with charts and trends
5. **WebSocket Push** - Real-time updates instead of polling
