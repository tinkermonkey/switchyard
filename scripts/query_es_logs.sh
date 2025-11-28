#!/bin/bash

# Query Elasticsearch for orchestrator logs
# Usage: ./scripts/query_es_logs.sh [query] [limit]

QUERY="${1:-*}"
LIMIT="${2:-50}"
ES_URL="http://localhost:9200"

echo "Querying Elasticsearch for: $QUERY (limit: $LIMIT)"
echo "---"

# Get recent logs matching query
curl -s "${ES_URL}/orchestrator-logs-*/_search" -H 'Content-Type: application/json' -d "{
  \"size\": ${LIMIT},
  \"sort\": [{\"@timestamp\": {\"order\": \"desc\"}}],
  \"query\": {
    \"query_string\": {
      \"query\": \"${QUERY}\",
      \"default_field\": \"message\"
    }
  }
}" | jq -r '.hits.hits[]._source | "\(.["@timestamp"]) [\(.level)] \(.logger_name): \(.message)"' 2>/dev/null

# Also show agent-specific logs if available
echo ""
echo "=== Agent Container Logs ==="
curl -s "${ES_URL}/orchestrator-logs-*/_search" -H 'Content-Type: application/json' -d "{
  \"size\": ${LIMIT},
  \"sort\": [{\"@timestamp\": {\"order\": \"desc\"}}],
  \"query\": {
    \"bool\": {
      \"must\": [
        {\"exists\": {\"field\": \"container_name\"}},
        {\"query_string\": {\"query\": \"${QUERY}\", \"default_field\": \"message\"}}
      ]
    }
  }
}" | jq -r '.hits.hits[]._source | "\(.["@timestamp"]) [\(.container_name)] \(.message)"' 2>/dev/null || echo "No container logs found"
