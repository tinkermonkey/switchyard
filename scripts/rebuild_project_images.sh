#!/bin/bash
#
# Shell wrapper for rebuild_project_images.py
# Provides a convenient shell script interface for users who prefer bash
#
# Usage:
#   ./scripts/rebuild_project_images.sh [options]
#   ./scripts/rebuild_project_images.sh --help
#

# Get script directory and change to project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.." || exit 1

# Run Python script with all arguments passed through
PYTHONPATH=. python scripts/rebuild_project_images.py "$@"
