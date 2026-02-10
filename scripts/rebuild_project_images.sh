#!/bin/bash
#
# Shell wrapper for rebuild_project_images.py
#
# Automatically detects whether the command needs to run inside Docker
# (--with-agents requires Redis) and delegates accordingly.
#
# Usage:
#   ./scripts/rebuild_project_images.sh [options]
#   ./scripts/rebuild_project_images.sh --help
#

set -euo pipefail

# Get script directory and change to project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.." || exit 1

# Check if --with-agents is present in arguments (requires running inside Docker)
needs_docker=false
for arg in "$@"; do
    if [[ "$arg" == "--with-agents" ]]; then
        needs_docker=true
        break
    fi
done

if $needs_docker; then
    # Verify the orchestrator container is running
    if ! docker compose ps --status running orchestrator --quiet 2>/dev/null | grep -q .; then
        echo "Error: orchestrator container is not running." >&2
        echo "Start it with: docker compose up -d" >&2
        exit 1
    fi

    # Record time before enqueuing so log tail captures everything
    start_time=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    # Enqueue task(s) inside the orchestrator container
    enqueue_output=$(docker compose exec -T orchestrator \
        python scripts/rebuild_project_images.py "$@" 2>&1)
    enqueue_exit=$?
    echo "$enqueue_output"

    if [ $enqueue_exit -ne 0 ]; then
        exit $enqueue_exit
    fi

    # Extract task IDs from "task_id: <id>" in output
    task_ids=$(echo "$enqueue_output" | grep -oP 'task_id: \K[^\)]+' || true)

    if [ -z "$task_ids" ]; then
        # No tasks queued (dry-run or no matching projects)
        exit 0
    fi

    # Build grep pattern matching any of the queued task IDs
    pattern=$(echo "$task_ids" | paste -sd '|')
    expected=$(echo "$task_ids" | wc -l)

    echo ""
    echo "=== Watching Agent Execution ==="
    echo ""

    completed=0
    while IFS= read -r line; do
        # Strip docker compose service prefix for cleaner output
        clean=$(echo "$line" | sed 's/^orchestrator-[0-9]*\s*| //')
        echo "$clean"

        # Worker pool logs "Completed task" on success, "Task ... failed" on failure
        if [[ "$line" == *"Completed task"* ]] || [[ "$line" == *"failed after"* ]]; then
            completed=$((completed + 1))
            if [ "$completed" -ge "$expected" ]; then
                break
            fi
        fi
    done < <(timeout 1800 docker compose logs -f --since "$start_time" orchestrator 2>&1 \
        | grep --line-buffered -E "$pattern")

    if [ "$completed" -lt "$expected" ]; then
        echo "" >&2
        echo "Timed out waiting for agent execution (30m limit)." >&2
        exit 1
    fi
else
    # Direct rebuild can run on the host
    PYTHONPATH=. python scripts/rebuild_project_images.py "$@"
fi
