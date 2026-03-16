#!/bin/bash
# Rebuild and restart the web UI container

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORCHESTRATOR_ROOT="$(dirname "$SCRIPT_DIR")"

echo "=========================================="
echo "Rebuilding Web UI"
echo "=========================================="

cd "$ORCHESTRATOR_ROOT"

echo ""
echo "Step 1: Rebuilding Docker image..."
docker compose build --no-cache web-ui
echo "✓ Image rebuilt"

echo ""
echo "Step 2: Stopping web-ui container via docker-compose..."
docker compose stop web-ui
echo "✓ Container stopped"

echo ""
echo "Step 3: Starting web-ui container..."
docker compose up -d --no-deps web-ui
echo "✓ Container started"

echo ""
echo "=========================================="
echo "Web UI rebuild complete!"
echo "=========================================="
echo ""
echo "Access the UI at: http://localhost:3000"
echo ""
