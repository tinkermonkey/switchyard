#!/bin/bash
# Convenience script to test agent filesystem access

PROJECT=${1:-context-studio}
AGENT=${2:-senior_software_engineer}

echo "Testing filesystem access for agent: $AGENT on project: $PROJECT"
echo ""

./docker-compose.sh exec orchestrator python scripts/test_agent_filesystem.py "$PROJECT" --agent "$AGENT"
