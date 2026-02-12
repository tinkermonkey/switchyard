#!/bin/bash
# Update Project - Convenience wrapper for agent regeneration and Docker image rebuild
#
# Usage: ./scripts/update_project.sh <project> [--dry-run]
#
# This script performs a complete project update:
# 1. Regenerates agents/skills based on current codebase
# 2. Rebuilds Docker image with updated agents
# 3. Validates generated artifacts
#
# Examples:
#   ./scripts/update_project.sh context-studio
#   ./scripts/update_project.sh documentation_robotics --dry-run

set -e

PROJECT=$1
DRY_RUN_FLAG=""

if [ -z "$PROJECT" ]; then
    echo "Usage: $0 <project> [--dry-run]"
    echo ""
    echo "Examples:"
    echo "  $0 context-studio"
    echo "  $0 documentation_robotics --dry-run"
    exit 1
fi

# Check for dry-run flag
if [ "$2" = "--dry-run" ]; then
    DRY_RUN_FLAG="--dry-run"
    echo "[DRY RUN MODE] No changes will be made"
    echo ""
fi

echo "==================================="
echo "Updating project: $PROJECT"
echo "==================================="

# Step 1: Regenerate agents/skills
echo ""
echo "Step 1/3: Regenerating agents/skills..."
python scripts/maintain_agent_team.py --project "$PROJECT" --auto-approve "$DRY_RUN_FLAG"

if [ $? -ne 0 ]; then
    echo ""
    echo "✗ Agent regeneration failed"
    exit 1
fi

# Step 2: Rebuild Docker image
echo ""
echo "Step 2/3: Rebuilding Docker image..."
python scripts/rebuild_project_images.py --project "$PROJECT" --update-state "$DRY_RUN_FLAG"

if [ $? -ne 0 ]; then
    echo ""
    echo "✗ Docker image rebuild failed"
    exit 1
fi

# Step 3: Validate
echo ""
echo "Step 3/3: Validating..."
python scripts/validate_artifacts.py --project "$PROJECT"

if [ $? -ne 0 ]; then
    echo ""
    echo "⚠ Validation warnings detected (but update completed)"
fi

echo ""
echo "✓ Update complete for $PROJECT"
