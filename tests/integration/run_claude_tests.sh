#!/bin/bash
# Quick test runner for Claude Code integration tests

set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}=== Claude Code Integration Tests ===${NC}"
echo ""

# Activate virtual environment
if [ -f .venv/bin/activate ]; then
    echo -e "${YELLOW}Activating virtual environment...${NC}"
    source .venv/bin/activate
else
    echo "Error: Virtual environment not found at .venv/bin/activate"
    exit 1
fi

# Change to project root
cd "$(dirname "$0")/../.."

# Default: run mocked tests
TEST_TYPE="${1:-mocked}"

case "$TEST_TYPE" in
    mocked|mock)
        echo -e "${GREEN}Running mocked tests (no Claude CLI required)${NC}"
        echo ""
        python -m pytest tests/integration/test_claude_code_mocked.py -v
        ;;
    
    real|cli|all)
        echo -e "${YELLOW}The real-Claude-CLI tests (test_claude_code_integration.py) were${NC}"
        echo -e "${YELLOW}deleted in #213: they spent real API quota against a live account and${NC}"
        echo -e "${YELLOW}nobody ran them. Only the mocked suite remains -- use '$0 mocked'.${NC}"
        exit 1
        ;;

    coverage)
        echo -e "${GREEN}Running tests with coverage${NC}"
        echo ""
        python -m pytest tests/integration/test_claude_code_mocked.py \
            --cov=claude --cov=monitoring \
            --cov-report=term-missing \
            --cov-report=html \
            -v
        echo ""
        echo -e "${GREEN}Coverage report: htmlcov/index.html${NC}"
        ;;
    
    help|-h|--help)
        echo "Usage: $0 [TYPE]"
        echo ""
        echo "Types:"
        echo "  mocked    Run mocked tests only (default, no CLI needed)"
        echo "  coverage  Run with coverage report"
        echo "  help      Show this help"
        echo ""
        echo "('real' and 'all' were removed in #213 with the real-API tests.)"
        echo ""
        echo "Examples:"
        echo "  $0              # Run mocked tests"
        echo "  $0 mocked       # Run mocked tests"
        echo "  $0 coverage     # Run with coverage"
        exit 0
        ;;
    
    *)
        echo "Unknown test type: $TEST_TYPE"
        echo "Use '$0 help' for usage information"
        exit 1
        ;;
esac

echo ""
echo -e "${GREEN}✓ Tests complete${NC}"
