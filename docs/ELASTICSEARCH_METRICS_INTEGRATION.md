# Elasticsearch Metrics Integration

**Date:** October 10, 2025  
**Status:** ✅ Implemented

## Overview

The orchestrator now writes metrics to Elasticsearch indices with automatic daily rotation. This provides better querying capabilities, time-series analysis, and visualization options compared to the previous Prometheus implementation.

## Architecture

### Dual-Write Strategy
```
Agent Execution
      ↓
MetricsCollector
      ↓
   ┌──┴──┐
   ↓     ↓
Elasticsearch  JSON Files
(primary)      (backup)
```

**Benefits:**
- Elasticsearch for rich querying and visualization
- JSON files as backup if Elasticsearch is unavailable
- No data loss on Elasticsearch failures

## Indices

### orchestrator-task-metrics-YYYY.MM.DD

Tracks agent task execution:

**Fields:**
- `@timestamp` (date): When the task completed
- `agent` (keyword): Agent name (e.g., "business_analyst")
- `duration` (float): Execution time in seconds
- `success` (boolean): Whether the task succeeded

**Example Document:**
```json
{
  "@timestamp": "2025-10-10T14:23:45.123456",
  "agent": "business_analyst",
  "duration": 45.2,
  "success": true
}
```

### orchestrator-quality-metrics-YYYY.MM.DD

Tracks quality scores from agent outputs:

**Fields:**
- `@timestamp` (date): When the metric was recorded
- `agent` (keyword): Agent name
- `metric_name` (keyword): Type of metric (e.g., "completeness", "clarity")
- `score` (float): Quality score (0.0-1.0)

**Example Document:**
```json
{
  "@timestamp": "2025-10-10T14:23:45.123456",
  "agent": "business_analyst",
  "metric_name": "completeness",
  "score": 0.95
}
```

## Configuration

### Environment Variables

The orchestrator uses the existing `ELASTICSEARCH_HOSTS` environment variable:

```bash
# Default (from docker-compose.yml)
ELASTICSEARCH_HOSTS=http://elasticsearch:9200

# Multiple hosts
ELASTICSEARCH_HOSTS=http://es1:9200,http://es2:9200
```

### Automatic Initialization

On startup, `MetricsCollector`:
1. Connects to Elasticsearch
2. Tests connection with ping
3. Creates index templates for proper field mappings
4. Falls back to JSON-only mode if Elasticsearch unavailable

## Usage Examples

### Basic Queries

```bash
# View all task metrics indices
curl http://localhost:9200/_cat/indices/orchestrator-task-metrics-*?v

# View all quality metrics indices
curl http://localhost:9200/_cat/indices/orchestrator-quality-metrics-*?v

# Get most recent task metrics
curl -s "http://localhost:9200/orchestrator-task-metrics-*/_search?size=10&sort=@timestamp:desc" | jq '.hits.hits[]._source'

# Get most recent quality metrics
curl -s "http://localhost:9200/orchestrator-quality-metrics-*/_search?size=10&sort=@timestamp:desc" | jq '.hits.hits[]._source'
```

### Aggregations

```bash
# Count tasks by agent
curl -s "http://localhost:9200/orchestrator-task-metrics-*/_search" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "by_agent": {
      "terms": {
        "field": "agent",
        "size": 50
      }
    }
  }
}' | jq '.aggregations.by_agent.buckets'

# Average duration by agent
curl -s "http://localhost:9200/orchestrator-task-metrics-*/_search" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "by_agent": {
      "terms": {
        "field": "agent"
      },
      "aggs": {
        "avg_duration": {
          "avg": {
            "field": "duration"
          }
        },
        "max_duration": {
          "max": {
            "field": "duration"
          }
        }
      }
    }
  }
}' | jq '.aggregations.by_agent.buckets'

# Success rate by agent
curl -s "http://localhost:9200/orchestrator-task-metrics-*/_search" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "by_agent": {
      "terms": {
        "field": "agent"
      },
      "aggs": {
        "success_rate": {
          "avg": {
            "field": "success"
          }
        },
        "total": {
          "value_count": {
            "field": "success"
          }
        }
      }
    }
  }
}' | jq '.aggregations.by_agent.buckets'

# Average quality scores by metric type
curl -s "http://localhost:9200/orchestrator-quality-metrics-*/_search" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "by_metric": {
      "terms": {
        "field": "metric_name"
      },
      "aggs": {
        "avg_score": {
          "avg": {
            "field": "score"
          }
        },
        "min_score": {
          "min": {
            "field": "score"
          }
        },
        "max_score": {
          "max": {
            "field": "score"
          }
        }
      }
    }
  }
}' | jq '.aggregations.by_metric.buckets'
```

### Time-Based Queries

```bash
# Tasks in the last hour
curl -s "http://localhost:9200/orchestrator-task-metrics-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "range": {
      "@timestamp": {
        "gte": "now-1h"
      }
    }
  },
  "sort": [{"@timestamp": "desc"}],
  "size": 100
}' | jq '.hits.hits[]._source'

# Failed tasks today
curl -s "http://localhost:9200/orchestrator-task-metrics-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"success": false}},
        {"range": {"@timestamp": {"gte": "now/d"}}}
      ]
    }
  },
  "sort": [{"@timestamp": "desc"}]
}' | jq '.hits.hits[]._source'

# Task duration trend (hourly buckets)
curl -s "http://localhost:9200/orchestrator-task-metrics-*/_search" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "over_time": {
      "date_histogram": {
        "field": "@timestamp",
        "calendar_interval": "hour"
      },
      "aggs": {
        "avg_duration": {
          "avg": {
            "field": "duration"
          }
        }
      }
    }
  }
}' | jq '.aggregations.over_time.buckets'
```

## Kibana Integration

### Setup

Add Kibana to `docker-compose.yml`:

```yaml
kibana:
  image: docker.elastic.co/kibana/kibana:9.0.0
  environment:
    - ELASTICSEARCH_HOSTS=http://elasticsearch:9200
    - xpack.security.enabled=false
  depends_on:
    - elasticsearch
  networks:
    - orchestrator-net
  ports:
    - "5601:5601"
```

### Create Index Patterns

1. Open Kibana: http://localhost:5601
2. Go to Management → Stack Management → Index Patterns
3. Create pattern: `orchestrator-task-metrics-*`
   - Time field: `@timestamp`
4. Create pattern: `orchestrator-quality-metrics-*`
   - Time field: `@timestamp`

### Suggested Visualizations

**Task Metrics Dashboard:**
- Line chart: Task count over time (by agent)
- Bar chart: Average duration by agent
- Pie chart: Success vs failure rate
- Table: Recent failed tasks
- Metric: Total tasks today
- Metric: Average success rate

**Quality Metrics Dashboard:**
- Line chart: Quality scores over time (by metric_name)
- Gauge: Average completeness score
- Gauge: Average clarity score
- Table: Quality scores by agent
- Heat map: Agent vs metric_name scores

## Index Lifecycle Management

### Retention Policy

Configure ILM to automatically delete old indices:

```bash
# Create ILM policy (delete after 90 days)
curl -X PUT "http://localhost:9200/_ilm/policy/orchestrator-metrics-policy" -H 'Content-Type: application/json' -d '{
  "policy": {
    "phases": {
      "hot": {
        "actions": {
          "rollover": {
            "max_age": "1d"
          }
        }
      },
      "delete": {
        "min_age": "90d",
        "actions": {
          "delete": {}
        }
      }
    }
  }
}'

# Update index templates to use ILM policy
curl -X PUT "http://localhost:9200/_index_template/orchestrator-task-metrics" -H 'Content-Type: application/json' -d '{
  "index_patterns": ["orchestrator-task-metrics-*"],
  "template": {
    "settings": {
      "index.lifecycle.name": "orchestrator-metrics-policy"
    }
  }
}'
```

### Manual Cleanup

```bash
# Delete indices older than 30 days
curl -X DELETE "http://localhost:9200/orchestrator-task-metrics-2025.09.*"
curl -X DELETE "http://localhost:9200/orchestrator-quality-metrics-2025.09.*"
```

## Monitoring

### Health Check

```bash
# Check Elasticsearch cluster health
curl http://localhost:9200/_cluster/health?pretty

# Check index sizes
curl http://localhost:9200/_cat/indices/orchestrator-*?v&s=store.size:desc

# Check document counts
curl http://localhost:9200/_cat/count/orchestrator-*?v
```

### Alerting

Create alerts in Kibana for:
- High task failure rate (>10% in last hour)
- Slow tasks (p99 duration > threshold)
- Low quality scores (avg < 0.7)
- No tasks executed in last 10 minutes (orchestrator down)

## Troubleshooting

### Metrics Not Appearing in Elasticsearch

1. **Check orchestrator logs:**
   ```bash
   docker logs orchestrator | grep -i elasticsearch
   ```
   
2. **Verify Elasticsearch is running:**
   ```bash
   curl http://localhost:9200/_cluster/health
   ```

3. **Check for connection errors:**
   Look for "Failed to write metrics to Elasticsearch" in logs

4. **Verify indices exist:**
   ```bash
   curl http://localhost:9200/_cat/indices/orchestrator-*?v
   ```

### Elasticsearch Connection Failed

If Elasticsearch is unavailable:
- Metrics will still be written to JSON files
- No data loss occurs
- Connection will be retried on next metric write
- Check `orchestrator_data/metrics/*.jsonl` for backup data

### Import JSON Backups to Elasticsearch

If you need to backfill from JSON files:

```bash
# Convert JSONL to bulk import format
cat orchestrator_data/metrics/task_metrics_2025-10-10.jsonl | jq -c '. | {index: {_index: "orchestrator-task-metrics-2025.10.10"}}, .' | curl -X POST "http://localhost:9200/_bulk" -H 'Content-Type: application/x-ndjson' --data-binary @-
```

## Performance Considerations

- **Index per day**: Keeps indices small and manageable
- **No replicas**: Single-node cluster doesn't need replication
- **1 shard**: Small data volume doesn't require multiple shards
- **Async writes**: Metrics writing doesn't block agent execution
- **Bulk API**: Consider batching if metrics volume increases

## Future Enhancements

1. **Batch writes**: Buffer metrics and write in bulk
2. **Alerting integration**: Send alerts to Slack/PagerDuty
3. **Grafana dashboards**: Alternative to Kibana
4. **Custom retention**: Different retention periods per metric type
5. **Anomaly detection**: ML-based alerting on unusual patterns

---

**Related Documentation:**
- [Prometheus Removal](./PROMETHEUS_REMOVAL.md)
- [Elasticsearch Pattern Detection](../config/pattern_detection.yaml)
