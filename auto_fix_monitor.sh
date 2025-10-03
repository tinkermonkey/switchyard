#!/bin/bash

cd /Users/austinsand/workspace/orchestrator/clauditoreum

LOG_FILE="/tmp/autofix_monitor.log"
ERROR_FILE="/tmp/detected_errors.log"

echo "=== Auto-Fix Monitor Started at $(date) ===" > "$LOG_FILE"

while true; do
  # Capture recent logs
  RECENT_LOGS=$(docker-compose logs --since=30s orchestrator observability-server redis web-ui 2>&1)

  # Filter for errors (excluding known warnings)
  ERRORS=$(echo "$RECENT_LOGS" | grep -E "error|exception|traceback|failed|critical" -i | \
    grep -v "write() before start_response" | \
    grep -v "WARNING: This is a development server" | \
    grep -v "Do not use it in a production deployment")

  if [ -n "$ERRORS" ]; then
    echo "=== ERRORS DETECTED AT $(date) ===" | tee -a "$LOG_FILE"
    echo "$ERRORS" | tee -a "$LOG_FILE" "$ERROR_FILE"
    echo "=========================================" | tee -a "$LOG_FILE"

    # Signal that errors were found (create a marker file)
    echo "$ERRORS" > /tmp/error_detected_marker.txt
    echo "ERROR_DETECTED" > /tmp/monitor_status.txt

    # Exit monitoring to allow human intervention
    echo "Monitoring paused - errors detected. Check $ERROR_FILE for details."
    break
  else
    echo "OK" > /tmp/monitor_status.txt
    echo "[$(date)] No errors detected - monitoring..." | tee -a "$LOG_FILE"
  fi

  sleep 20
done
