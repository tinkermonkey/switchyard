#!/bin/bash

# GitHub API Monitoring Dashboard
# Real-time view of GitHub API usage, rate limits, and circuit breaker status

set -e

OBSERVABILITY_URL="${OBSERVABILITY_URL:-http://localhost:5001}"

show_status() {
    clear
    
    echo "╔════════════════════════════════════════════════════════════════════╗"
    echo "║           GitHub API Usage Monitoring Dashboard                   ║"
    echo "╚════════════════════════════════════════════════════════════════════╝"
    echo ""
    
    # Fetch current status
    STATUS=$(curl -s "$OBSERVABILITY_URL/api/github-api-status")
    
    if [ $? -ne 0 ]; then
        echo "❌ Error: Could not connect to observability server at $OBSERVABILITY_URL"
        echo ""
        echo "Make sure the orchestrator is running:"
        echo "  docker compose up -d"
        echo ""
        return 1
    fi
    
    # Extract and format rate limit info
    REMAINING=$(echo "$STATUS" | jq -r '.status.rate_limit.remaining')
    LIMIT=$(echo "$STATUS" | jq -r '.status.rate_limit.limit')
    PERCENTAGE=$(echo "$STATUS" | jq -r '.status.rate_limit.percentage_used')
    RESET_TIME=$(echo "$STATUS" | jq -r '.status.rate_limit.time_until_reset')
    RESET_TIMESTAMP=$(echo "$STATUS" | jq -r '.status.rate_limit.reset_time')
    
    echo "📊 RATE LIMIT STATUS"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    printf "  Remaining:  %5d / %5d points\n" "$REMAINING" "$LIMIT"
    printf "  Used:       %6.1f%%\n" "$PERCENTAGE"
    printf "  Time until reset:  %.0f seconds (~%.0f minutes)\n" "$RESET_TIME" "$((RESET_TIME / 60))"
    printf "  Reset at:   %s\n" "$RESET_TIMESTAMP"
    echo ""
    
    # Color-coded usage bar
    PERCENTAGE_INT=$(echo "$PERCENTAGE" | cut -d. -f1)
    BAR_LENGTH=50
    FILLED=$(( (PERCENTAGE_INT * BAR_LENGTH) / 100 ))
    EMPTY=$(( BAR_LENGTH - FILLED ))
    
    printf "  ["
    printf "%${FILLED}s" | tr ' ' '█'
    printf "%${EMPTY}s" | tr ' ' '░'
    printf "] "
    
    if [ "$PERCENTAGE_INT" -gt 95 ]; then
        printf "🚨 CRITICAL\n"
    elif [ "$PERCENTAGE_INT" -gt 90 ]; then
        printf "🔴 HIGH\n"
    elif [ "$PERCENTAGE_INT" -gt 80 ]; then
        printf "🟡 ELEVATED\n"
    elif [ "$PERCENTAGE_INT" -gt 50 ]; then
        printf "🟢 MODERATE\n"
    else
        printf "🟢 LOW\n"
    fi
    echo ""
    
    # Circuit breaker status
    BREAKER_STATE=$(echo "$STATUS" | jq -r '.status.breaker.state')
    BREAKER_OPEN=$(echo "$STATUS" | jq -r '.status.breaker.is_open')
    
    echo "🔌 CIRCUIT BREAKER"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    
    if [ "$BREAKER_OPEN" = "true" ]; then
        echo "  Status: 🔴 OPEN (Requests being blocked)"
        OPENED_AT=$(echo "$STATUS" | jq -r '.status.breaker.opened_at')
        BREAKER_RESET=$(echo "$STATUS" | jq -r '.status.breaker.reset_time')
        printf "  Opened at: %s\n" "$OPENED_AT"
        printf "  Will reset: %s\n" "$BREAKER_RESET"
    else
        echo "  Status: 🟢 $BREAKER_STATE (Requests allowed)"
    fi
    echo ""
    
    # Statistics
    TOTAL=$(echo "$STATUS" | jq -r '.status.stats.total_requests')
    FAILED=$(echo "$STATUS" | jq -r '.status.stats.failed_requests')
    RATE_LIMITED=$(echo "$STATUS" | jq -r '.status.stats.rate_limited_requests')
    BACKOFF=$(echo "$STATUS" | jq -r '.status.stats.backoff_multiplier')
    
    echo "📈 STATISTICS"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    printf "  Total requests:        %d\n" "$TOTAL"
    printf "  Failed requests:       %d\n" "$FAILED"
    printf "  Rate-limited requests: %d\n" "$RATE_LIMITED"
    printf "  Backoff multiplier:    %.1f×\n" "$BACKOFF"
    echo ""
    
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "🔄 Refreshing in 30 seconds... (Press Ctrl+C to exit)"
    echo ""
    
    return 0
}

# Main loop
if [ "$1" = "-o" ] || [ "$1" = "--once" ]; then
    # Show once and exit
    show_status
else
    # Show continuously
    while true; do
        if show_status; then
            sleep 30
        else
            sleep 5
        fi
    done
fi
