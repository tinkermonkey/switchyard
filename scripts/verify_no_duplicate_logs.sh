#!/bin/bash
# Verification script for duplicate Claude log fix
# Checks for duplicate message_ids in Elasticsearch

set -e

echo "=== Verifying No Duplicate Claude Logs ==="
echo ""

# Check if Elasticsearch is available
if ! curl -s http://localhost:9200/_cluster/health > /dev/null 2>&1; then
    echo "❌ Elasticsearch is not available at localhost:9200"
    echo "   Make sure docker-compose is running: docker-compose up -d"
    exit 1
fi

echo "✅ Elasticsearch is available"
echo ""

# Query recent Claude stream events
echo "Fetching last 200 Claude stream events..."
RESPONSE=$(curl -s "http://localhost:9200/claude-streams-*/_search" -H 'Content-Type: application/json' -d '{
  "size": 200,
  "sort": [{"timestamp": "desc"}],
  "query": {"term": {"event_category": "claude_stream"}},
  "_source": ["message_id", "timestamp", "redis_stream_id"]
}')

# Check if query was successful
if ! echo "$RESPONSE" | jq -e '.hits' > /dev/null 2>&1; then
    echo "❌ Failed to query Elasticsearch"
    echo "$RESPONSE" | jq '.'
    exit 1
fi

# Extract total hits
TOTAL=$(echo "$RESPONSE" | jq -r '.hits.total.value')
echo "Total events found: $TOTAL"
echo ""

if [ "$TOTAL" -eq 0 ]; then
    echo "⚠️  No Claude stream events found in Elasticsearch"
    echo "   Trigger an agent execution to generate test data"
    exit 0
fi

# Extract message IDs and check for duplicates
echo "Analyzing message IDs for duplicates..."
echo "$RESPONSE" | jq -r '.hits.hits[]._source.message_id' | sort > /tmp/message_ids.txt

UNIQUE_COUNT=$(sort -u /tmp/message_ids.txt | wc -l)
DUPLICATE_COUNT=$(sort /tmp/message_ids.txt | uniq -d | wc -l)

echo "Unique message IDs: $UNIQUE_COUNT"
echo "Duplicate message IDs: $DUPLICATE_COUNT"
echo ""

if [ "$DUPLICATE_COUNT" -eq 0 ]; then
    echo "✅ SUCCESS: No duplicate message IDs found!"
    echo "   The fix is working correctly."
    exit 0
else
    echo "❌ FAILURE: Found $DUPLICATE_COUNT duplicate message IDs"
    echo ""
    echo "Duplicate message IDs:"
    sort /tmp/message_ids.txt | uniq -d
    echo ""
    echo "Sample duplicate entries:"
    for msg_id in $(sort /tmp/message_ids.txt | uniq -d | head -3); do
        echo ""
        echo "Message ID: $msg_id"
        echo "$RESPONSE" | jq ".hits.hits[] | select(._source.message_id == \"$msg_id\") | {timestamp: ._source.timestamp, redis_stream_id: ._source.redis_stream_id}"
    done
    exit 1
fi
