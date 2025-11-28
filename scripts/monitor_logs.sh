#!/bin/bash

# Robust log monitoring script that handles ephemeral agent containers
# Usage: ./scripts/monitor_logs.sh [service_name] [num_lines]

SERVICE="${1:-orchestrator}"  # Default to orchestrator service
NUM_LINES="${2:-1000}"        # Default to 1000 lines

echo "Monitoring logs for service: $SERVICE (showing last $NUM_LINES lines)"
echo "Press Ctrl+C to stop"
echo "---"

# Function to handle cleanup
cleanup() {
    echo ""
    echo "Stopping log monitor..."
    exit 0
}

trap cleanup SIGINT SIGTERM

# Main monitoring loop
while true; do
    # Use timeout to prevent hanging on dead containers
    timeout 30s docker compose logs -f -n "$NUM_LINES" "$SERVICE" 2>&1 || true

    # If the command exits, wait a bit before retrying
    # This handles the case where containers exit
    sleep 2

    # Check if the service still exists
    if ! docker compose ps "$SERVICE" &> /dev/null; then
        echo "Service $SERVICE no longer exists. Exiting..."
        exit 1
    fi
done
