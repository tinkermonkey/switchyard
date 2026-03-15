#!/bin/bash
# Test the Docker workspace fixture fix

cd /home/austinsand/workspace/orchestrator/switchyard

echo "=================================="
echo "Testing Docker Workspace Fixture"
echo "=================================="
echo ""
echo "This test demonstrates that the docker_workspace fixture"
echo "creates properly-permissioned directories that Docker"
echo "containers can write to."
echo ""

# Check if we're in a container
if [ -d "/workspace" ]; then
    echo "✓ Running inside orchestrator container"
    echo "  Will use /workspace/test_project/"
else
    echo "✓ Running on host machine"
    echo "  Will create temp directory with 0777 permissions"
fi

echo ""
echo "Running Docker execution tests..."
echo ""

source .venv/bin/activate
source .env

# Run just one Docker test to demonstrate
pytest tests/integration/test_claude_code_integration.py::TestClaudeCodeDockerExecution::test_docker_hello_world -v -s

echo ""
echo "=================================="
echo "Test Complete"
echo "=================================="
