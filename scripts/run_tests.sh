#!/bin/bash
# Test runner script for local development

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Default values
RUN_UNIT=true
RUN_INTEGRATION=false
RUN_ALL=false
VERBOSE=false
COVERAGE=false
FAIL_FAST=false
SPECIFIC_TEST=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -a|--all)
            RUN_ALL=true
            RUN_UNIT=true
            RUN_INTEGRATION=true
            shift
            ;;
        -i|--integration)
            RUN_INTEGRATION=true
            RUN_UNIT=false
            shift
            ;;
        -u|--unit)
            RUN_UNIT=true
            RUN_INTEGRATION=false
            shift
            ;;
        -v|--verbose)
            VERBOSE=true
            shift
            ;;
        -c|--coverage)
            COVERAGE=true
            shift
            ;;
        -x|--fail-fast)
            FAIL_FAST=true
            shift
            ;;
        -t|--test)
            SPECIFIC_TEST="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  -a, --all           Run all tests (unit + integration)"
            echo "  -u, --unit          Run unit tests only (default)"
            echo "  -i, --integration   Run integration tests only"
            echo "  -v, --verbose       Verbose output"
            echo "  -c, --coverage      Generate coverage report"
            echo "  -x, --fail-fast     Stop on first failure"
            echo "  -t, --test <path>   Run specific test file or pattern"
            echo "  -h, --help          Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0                                    # Run unit tests"
            echo "  $0 --all --coverage                   # Run all tests with coverage"
            echo "  $0 --integration --verbose            # Run integration tests verbosely"
            echo "  $0 --test tests/unit/test_parser.py  # Run specific test file"
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Change to project root
cd "$(dirname "$0")/.."

echo -e "${GREEN}=== Claude Code Agent Orchestrator Test Suite ===${NC}"
echo ""

# Build pytest command
PYTEST_CMD="pytest"
PYTEST_ARGS=""

# Verbosity
if [ "$VERBOSE" = true ]; then
    PYTEST_ARGS="$PYTEST_ARGS -vv -s"
else
    PYTEST_ARGS="$PYTEST_ARGS -v"
fi

# Fail fast
if [ "$FAIL_FAST" = true ]; then
    PYTEST_ARGS="$PYTEST_ARGS -x"
fi

# Coverage
if [ "$COVERAGE" = true ]; then
    PYTEST_ARGS="$PYTEST_ARGS --cov=services --cov=agents --cov=pipeline"
    PYTEST_ARGS="$PYTEST_ARGS --cov-report=term-missing --cov-report=html"
fi

# Specific test
if [ -n "$SPECIFIC_TEST" ]; then
    echo -e "${YELLOW}Running specific test: $SPECIFIC_TEST${NC}"
    $PYTEST_CMD "$SPECIFIC_TEST" $PYTEST_ARGS
    exit $?
fi

# Run tests based on flags
EXIT_CODE=0

if [ "$RUN_UNIT" = true ]; then
    echo -e "${YELLOW}Running unit tests...${NC}"
    $PYTEST_CMD tests/unit $PYTEST_ARGS || EXIT_CODE=$?
    echo ""
fi

if [ "$RUN_INTEGRATION" = true ]; then
    echo -e "${YELLOW}Running integration tests...${NC}"
    $PYTEST_CMD tests/integration $PYTEST_ARGS || EXIT_CODE=$?
    echo ""
fi

# Summary
if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}✓ All tests passed!${NC}"

    if [ "$COVERAGE" = true ]; then
        echo ""
        echo -e "${GREEN}Coverage report generated in htmlcov/index.html${NC}"
    fi
else
    echo -e "${RED}✗ Tests failed with exit code $EXIT_CODE${NC}"
fi

exit $EXIT_CODE
