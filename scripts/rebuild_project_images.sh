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

    # Run inside the orchestrator container where Redis is available
    docker compose exec orchestrator python scripts/rebuild_project_images.py "$@"
else
    # Direct rebuild can run on the host
    PYTHONPATH=. python scripts/rebuild_project_images.py "$@"
fi
