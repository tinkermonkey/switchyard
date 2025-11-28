#!/bin/bash

# Watch agent execution via observability server
# This is more reliable than following docker logs directly

OBSERVABILITY_URL="http://localhost:5001"

echo "Watching agent execution logs..."
echo "Press Ctrl+C to stop"
echo "---"

# Function to display agent status
show_active_agents() {
    echo ""
    echo "=== Active Agents ==="
    curl -s "$OBSERVABILITY_URL/agents/active" | jq -r '.[] | "[\(.status)] \(.agent_type) - \(.task_id) (started: \(.start_time))"' 2>/dev/null || echo "No active agents"
}

# Function to display recent history
show_recent_history() {
    echo ""
    echo "=== Recent Agent History (last 10) ==="
    curl -s "$OBSERVABILITY_URL/history?limit=10" | jq -r '.[] | "[\(.status)] \(.agent_type) - Duration: \(.duration)s - End: \(.end_time)"' 2>/dev/null || echo "No history available"
}

# Function to display Claude logs
show_claude_logs() {
    echo ""
    echo "=== Recent Claude Logs (last 20) ==="
    curl -s "$OBSERVABILITY_URL/claude-logs-history?limit=20" | jq -r '.[] | "\(.timestamp) [\(.level)] \(.message)"' 2>/dev/null || echo "No logs available"
}

# Main loop
LAST_ACTIVE=""
while true; do
    CURRENT_ACTIVE=$(curl -s "$OBSERVABILITY_URL/agents/active" | jq -r '.[] | .container_name' 2>/dev/null | sort | tr '\n' ',')

    # If active agents changed, show update
    if [ "$CURRENT_ACTIVE" != "$LAST_ACTIVE" ]; then
        clear
        show_active_agents
        show_recent_history
        LAST_ACTIVE="$CURRENT_ACTIVE"
    fi

    # Show Claude logs every iteration
    show_claude_logs

    sleep 5
done
