.PHONY: help test test-unit test-integration test-all test-coverage test-verbose test-fast clean-test cleanup-branches cleanup-project rebuild-images rebuild-images-verify rebuild-images-agents rebuild-project

help:
	@echo "Claude Code Agent Orchestrator - Commands"
	@echo ""
	@echo "Testing:"
	@echo "  make test              Run unit tests (default)"
	@echo "  make test-unit         Run unit tests only"
	@echo "  make test-integration  Run integration tests only"
	@echo "  make test-all          Run all tests (unit + integration)"
	@echo "  make test-coverage     Run all tests with coverage report"
	@echo "  make test-verbose      Run tests with verbose output"
	@echo "  make test-fast         Run tests, stop on first failure"
	@echo "  make test-file         Run specific test file (usage: make test-file FILE=path/to/test.py)"
	@echo "  make clean-test        Clean test artifacts and cache"
	@echo ""
	@echo "Maintenance:"
	@echo "  make cleanup-branches         Cleanup orphaned feature branches (all projects)"
	@echo "  make cleanup-project          Cleanup orphaned branches for specific project (usage: make cleanup-project PROJECT=name)"
	@echo "  make rebuild-images           Rebuild all project Docker images"
	@echo "  make rebuild-images-verify    Rebuild images and update state to VERIFIED"
	@echo "  make rebuild-images-agents    Rebuild via dev environment agents"
	@echo "  make rebuild-project          Rebuild Docker image for specific project (usage: make rebuild-project PROJECT=name)"
	@echo ""

test: test-unit

test-unit:
	@echo "Running unit tests..."
	@./scripts/run_tests.sh --unit

test-integration:
	@echo "Running integration tests..."
	@./scripts/run_tests.sh --integration

test-all:
	@echo "Running all tests..."
	@./scripts/run_tests.sh --all

test-coverage:
	@echo "Running tests with coverage..."
	@./scripts/run_tests.sh --all --coverage

test-verbose:
	@echo "Running tests with verbose output..."
	@./scripts/run_tests.sh --all --verbose

test-fast:
	@echo "Running tests with fail-fast..."
	@./scripts/run_tests.sh --all --fail-fast

test-file:
ifdef FILE
	@echo "Running specific test: $(FILE)"
	@./scripts/run_tests.sh --test $(FILE) --verbose
else
	@echo "Error: FILE not specified"
	@echo "Usage: make test-file FILE=tests/unit/test_parser.py"
	@exit 1
endif

clean-test:
	@echo "Cleaning test artifacts..."
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete
	@find . -type f -name ".coverage" -delete
	@rm -rf .pytest_cache htmlcov coverage.xml
	@echo "✓ Test artifacts cleaned"

# Development helpers
lint:
	@echo "Running linters..."
	@flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
	@echo "✓ Linting passed"

format:
	@echo "Formatting code..."
	@black .
	@isort .
	@echo "✓ Code formatted"

format-check:
	@echo "Checking code format..."
	@black --check --diff .
	@isort --check-only --diff .

install-dev:
	@echo "Installing development dependencies..."
	@pip install -r requirements.txt
	@pip install pytest pytest-cov pytest-asyncio pytest-timeout
	@pip install flake8 black isort mypy
	@pip install bandit safety
	@echo "✓ Development dependencies installed"

# Maintenance tasks
cleanup-branches:
	@echo "Running orphaned branch cleanup for all projects..."
	@PYTHONPATH=. python scripts/cleanup_orphaned_branches.py
	@echo "✓ Cleanup complete"

cleanup-project:
ifdef PROJECT
	@echo "Running orphaned branch cleanup for project: $(PROJECT)"
	@PYTHONPATH=. python scripts/cleanup_orphaned_branches.py --project $(PROJECT)
	@echo "✓ Cleanup complete"
else
	@echo "Error: PROJECT not specified"
	@echo "Usage: make cleanup-project PROJECT=context-studio"
	@exit 1
endif

# Docker image rebuild
rebuild-images:
	@echo "Rebuilding all project Docker images..."
	@PYTHONPATH=. python scripts/rebuild_project_images.py
	@echo "✓ Image rebuild complete"

rebuild-images-verify:
	@echo "Rebuilding all project Docker images with state verification..."
	@PYTHONPATH=. python scripts/rebuild_project_images.py --update-state
	@echo "✓ Image rebuild and verification complete"

rebuild-images-agents:
	@echo "Rebuilding images via dev environment agents..."
	@PYTHONPATH=. python scripts/rebuild_project_images.py --with-agents
	@echo "✓ Agent tasks queued (monitor at http://localhost:5001/agents/active)"

rebuild-project:
ifdef PROJECT
	@echo "Rebuilding Docker image for project: $(PROJECT)"
	@PYTHONPATH=. python scripts/rebuild_project_images.py --project $(PROJECT)
	@echo "✓ Image rebuild complete"
else
	@echo "Error: PROJECT not specified"
	@echo "Usage: make rebuild-project PROJECT=context-studio"
	@exit 1
endif

# Quick commands
.PHONY: t tc tv
t: test
tc: test-coverage
tv: test-verbose
