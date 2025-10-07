#!/bin/bash
# Reset project state and trigger dev-environment-setup
#
# Usage: ./reset_project_state.sh <project-name>
#
# This script:
# 1. Stops the orchestrator
# 2. Wipes GitHub project state files
# 3. Resets dev container verification state to trigger dev-environment-setup
# 4. Cleans up Docker images for the project
# 5. Restarts orchestrator

set -e

PROJECT_NAME=${1:-context-studio}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORCHESTRATOR_ROOT="$(dirname "$SCRIPT_DIR")"

echo "=============================================="
echo "Resetting state for project: $PROJECT_NAME"
echo "=============================================="

# Check if orchestrator container is running
if docker ps | grep -q clauditoreum-orchestrator-1; then
    echo ""
    echo "Step 1: Stopping orchestrator..."
    docker stop clauditoreum-orchestrator-1
    echo "✓ Orchestrator stopped"
else
    echo ""
    echo "Step 1: Orchestrator not running, skipping stop"
fi

# Wipe GitHub project state files
echo ""
echo "Step 2: Wiping GitHub project state files..."
STATE_DIR="$ORCHESTRATOR_ROOT/state/projects/$PROJECT_NAME"
if [ -d "$STATE_DIR" ]; then
    echo "Backing up state to ${STATE_DIR}.backup..."
    mv "$STATE_DIR" "${STATE_DIR}.backup.$(date +%Y%m%d_%H%M%S)"
    echo "✓ GitHub state files backed up and removed"
else
    echo "✓ No GitHub state files found"
fi

# Reset dev container verification state
echo ""
echo "Step 3: Resetting dev container verification state..."
DEV_CONTAINER_STATE="$ORCHESTRATOR_ROOT/state/dev_containers/$PROJECT_NAME.yaml"
if [ -f "$DEV_CONTAINER_STATE" ]; then
    echo "Backing up dev container state..."
    mv "$DEV_CONTAINER_STATE" "${DEV_CONTAINER_STATE}.backup.$(date +%Y%m%d_%H%M%S)"
    echo "✓ Dev container state reset (will trigger dev-environment-setup)"
else
    echo "✓ No dev container state found (will trigger dev-environment-setup)"
fi

# Clean up Docker images
echo ""
echo "Step 4: Cleaning up Docker images for $PROJECT_NAME..."
IMAGES=$(docker images | grep "$PROJECT_NAME" | awk '{print $3}' || true)
if [ -n "$IMAGES" ]; then
    echo "Found images to remove:"
    docker images | grep "$PROJECT_NAME" || true
    echo "$IMAGES" | xargs -r docker rmi -f
    echo "✓ Docker images cleaned"
else
    echo "✓ No Docker images found for $PROJECT_NAME"
fi

# Restart orchestrator
echo ""
echo "Step 5: Restarting orchestrator..."
docker start clauditoreum-orchestrator-1
echo "✓ Orchestrator started"

echo ""
echo "=============================================="
echo "State reset complete!"
echo "=============================================="
echo ""
echo "Next steps:"
echo "1. Manually delete duplicate GitHub project boards:"
echo "   gh project list --owner <org>"
echo "   gh project delete <number> --owner <org>"
echo ""
echo "2. Monitor orchestrator logs:"
echo "   docker logs -f clauditoreum-orchestrator-1"
echo ""
echo "3. The orchestrator will:"
echo "   - Discover existing boards by name (no duplicates)"
echo "   - Queue dev-environment-setup task (dev container not verified)"
echo ""
