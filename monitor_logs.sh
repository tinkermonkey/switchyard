#!/bin/bash

cd /Users/austinsand/workspace/orchestrator/clauditoreum

while true; do
  ERRORS=$(docker-compose logs --since=30s orchestrator observability-server redis web-ui 2>&1 | grep -E "error|exception|traceback|failed|critical" -i | grep -v "write() before start_response" | grep -v "WARNING: This is a development server")

  if [ -n "$ERRORS" ]; then
    echo "=== ERRORS DETECTED AT $(date) ==="
    echo "$ERRORS"
    echo "========================================="
  else
    echo "[$(date)] No errors detected - monitoring..."
  fi

  sleep 20
done
