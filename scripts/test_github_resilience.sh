#!/bin/bash
#
# Test runner for GitHub resilience integration tests
#
# Usage: ./scripts/test_github_resilience.sh [OPTIONS]
#
# Options:
#   --quick     Run only fast tests (skip slow integration tests)
#   --full      Run all tests including slow ones
#   --verbose   Show detailed output
#   --coverage  Generate coverage report
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Default values
QUICK_MODE=false
VERBOSE=""
COVERAGE=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --quick)
            QUICK_MODE=true
            shift
            ;;
        --full)
            QUICK_MODE=false
            shift
            ;;
        --verbose)
            VERBOSE="-v -s"
            shift
            ;;
        --coverage)
            COVERAGE="--cov=services --cov=monitoring --cov-report=html --cov-report=term"
            shift
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo -e "${GREEN}=== GitHub Resilience Integration Tests ===${NC}"
echo ""

# Check prerequisites
echo -e "${YELLOW}Checking prerequisites...${NC}"

# Check if GitHub CLI is installed
if ! command -v gh &> /dev/null; then
    echo -e "${RED}ERROR: GitHub CLI (gh) is not installed${NC}"
    echo "Install with: sudo apt install gh"
    exit 1
fi

# Check if GitHub CLI is authenticated
if ! gh auth status &> /dev/null; then
    echo -e "${RED}ERROR: GitHub CLI is not authenticated${NC}"
    echo "Run: gh auth login"
    exit 1
fi

echo -e "${GREEN}✓ GitHub CLI authenticated${NC}"

# Check if Redis is running (optional for some tests)
REDIS_AVAILABLE=false

if command -v redis-cli &> /dev/null; then
    for host in localhost 127.0.0.1 redis; do
        if redis-cli -h $host ping &> /dev/null 2>&1; then
            echo -e "${GREEN}✓ Redis is available at $host${NC}"
            REDIS_AVAILABLE=true
            break
        fi
    done
fi

# If redis-cli failed or not installed, try Python
if [ "$REDIS_AVAILABLE" = false ]; then
    if python3 -c "import redis; r=redis.Redis(host='localhost', port=6379, socket_connect_timeout=2); r.ping()" &> /dev/null 2>&1; then
        echo -e "${GREEN}✓ Redis is available at localhost${NC}"
        REDIS_AVAILABLE=true
    fi
fi

if [ "$REDIS_AVAILABLE" = false ]; then
    echo -e "${YELLOW}⚠ Redis not available (some tests will be skipped)${NC}"
fi

# Check if pytest is installed
if ! python3 -c "import pytest" &> /dev/null; then
    echo -e "${RED}ERROR: pytest is not installed${NC}"
    echo "Install with: pip install pytest pytest-asyncio pytest-cov"
    exit 1
fi

echo -e "${GREEN}✓ pytest is available${NC}"
echo ""

# Set environment variables for test targets
export GITHUB_TEST_ORG="${GITHUB_TEST_ORG:-anthropics}"
export GITHUB_TEST_USER="${GITHUB_TEST_USER:-torvalds}"

echo -e "${YELLOW}Test configuration:${NC}"
echo "  Organization: $GITHUB_TEST_ORG"
echo "  User: $GITHUB_TEST_USER"
echo ""

# Determine which tests to run
if [ "$QUICK_MODE" = true ]; then
    echo -e "${YELLOW}Running quick tests only (excluding slow integration tests)${NC}"
    MARKERS="-m 'not slow'"
else
    echo -e "${YELLOW}Running all integration tests${NC}"
    MARKERS=""
fi

# Run tests
echo -e "${GREEN}Running tests...${NC}"
echo ""

# Change to project root
cd "$(dirname "$0")/.."

# Run pytest
if python3 -m pytest \
    tests/integration/test_github_resilience_integration.py \
    $MARKERS \
    $VERBOSE \
    $COVERAGE \
    --tb=short; then

    echo ""
    echo -e "${GREEN}=== All tests passed! ===${NC}"

    if [ -n "$COVERAGE" ]; then
        echo ""
        echo -e "${GREEN}Coverage report generated at: htmlcov/index.html${NC}"
    fi

    exit 0
else
    echo ""
    echo -e "${RED}=== Some tests failed ===${NC}"
    exit 1
fi
