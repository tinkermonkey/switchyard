# CLAUDE.md

This file provides guidance to coding agents when working with this codebase.

**IMPORTANT**: Do not create markdown files or any documentation during your tasks unless creating documentation is explicitly part of the task requirements.

## Quick Reference by Role

- **Coding Agent**: Core Architecture (§11), Development Workflow (§280)
- **DevOps Engineer**: Running the Orchestrator (§175), Troubleshooting (§471)
- **System Designer**: Project Overview (§7), Configuration Management (§97)
- **Deep Technical Details**: See `documentation/agent-execution-architecture.md`

## Project Overview

This is the Claude Code Agent Orchestrator - an autonomous AI development system that manages GitHub-integrated software development workflows. The orchestrator coordinates specialized AI agents through GitHub Projects v2 Kanban boards, executing complete SDLC pipelines from requirements analysis to deployment.

## Core Architecture

### Technology Stack
- **Language**: Python 3.11+
- **Framework**: Async/await with asyncio
- **Queue**: Redis (with in-memory fallback)
- **Search/Analytics**: Elasticsearch 9.0
- **GitHub**: GraphQL API, REST API, GitHub CLI
- **Claude**: Anthropic Claude API (Opus 4.5, Sonnet 4.5, Haiku 4.5)
- **Docker**: Container orchestration for agent isolation

### Key Components

**Pipeline System** (`pipeline/`)
- Sequential pipeline orchestration with checkpoints
- Circuit breaker pattern for fault tolerance
- State persistence and recovery

**Agent System** (`agents/`)
- Specialized agents for different SDLC stages
- Base classes: `PipelineStage` (base), `MakerAgent` (creates output), `AnalysisAgent` (analysis-only, extends MakerAgent)
- Three execution modes: Initial, Revision, Question (conversational)
- Docker-in-Docker execution with workspace mounting

**Configuration System** (`config/`)
- Three-layer architecture: Foundations, Projects, State
- `foundations/`: Agent definitions, pipeline templates, workflow templates
- `projects/`: Project-specific configurations
- `state/`: Runtime GitHub state (auto-managed)

**Services** (`services/`)
- `GitHubProjectManager`: Automatic project board reconciliation
- `ProjectMonitor`: Polls GitHub boards for changes
- `GitWorkflowManager`: Automated git operations and PR management
- `FeatureBranchManager`: Branch lifecycle management
- `DevContainerStateManager`: Docker image verification
- `ProjectWorkspaceManager`: Workspace initialization

**Task Queue** (`task_queue/`)
- Redis-backed priority queue
- Task lifecycle management
- Priority levels: LOW, MEDIUM, HIGH, CRITICAL

**State Management** (`state_management/`)
- Checkpointing for pipeline recovery
- Git state tracking
- Conversation history for multi-turn interactions
- PR review cycle tracking (`PRReviewStateManager`)

**Debug Scripts** (`scripts/`)
- Utilities for maintenance tasks (branch cleanup, state inspection)
- Use these to investigate issues or perform routine maintenance

### Workspace Isolation

**Host File System**:
```
./                                # Orchestrator isolated workspace
│   ├── clauditoreum/             # This codebase
│   └── <project-name>/           # Managed project checkouts (e.g., context-studio/)
```

**Inside Orchestrator Container**:
The orchestrator always runs in Docker. The container can only see the orchestrator workspace:
```
/app/                             # Orchestrator code (clauditoreum/)
/workspace/                       # Orchestrator workspace root
├── clauditoreum/                 # Same as /app (this codebase)
└── <project-name>/               # Managed checkouts ONLY
```

**Container Volume Mounts** (from docker-compose.yml):
```yaml
volumes:
  - ./:/app                        # Host: clauditoreum/ → Container: /app
  - ..:/workspace                  # Host: orchestrator/ → Container: /workspace
  - ~/.ssh/id_ed25519:/home/orchestrator/.ssh/id_ed25519:ro
  - ~/.gitconfig:/home/orchestrator/.gitconfig:ro
  - ~/.orchestrator:/home/orchestrator/.orchestrator  # GitHub App private keys
  - /var/run/docker.sock:/var/run/docker.sock  # Docker-in-Docker for agent containers
```

**Critical Isolation Rules**:
- The orchestrator container can ONLY access `/workspace/` which maps to a location on the host defined in the docker-compose.yml
- Projects are cloned into `/workspace/<project>/` (inside the orchestrator workspace)
- All agent operations happen within the isolated orchestrator workspace

## Configuration Management

### Foundational Layer (`config/foundations/`)

**agents.yaml**: Defines 13 agents with capabilities, timeouts, Docker requirements (11 registered in `AGENT_REGISTRY`)
- `requires_dev_container: true` - Needs project dependencies
- `requires_docker: true` - Must run in Docker
- `makes_code_changes: true` - Modifies codebase
- `filesystem_write_allowed: true` - Can write to /workspace

**pipelines.yaml**: Pipeline templates (planning_design, environment_support, sdlc_execution)
- Maker-checker workflow pattern
- Stage dependencies and inputs
- Review requirements and escalation rules

**workflows.yaml**: Kanban board templates with column definitions

### Project Layer (`config/projects/<project>.yaml`)

```yaml
project:
  name: "project-name"
  github:
    org: "your-org"
    repo: "your-repo"
    repo_url: "git@github.com:your-org/your-repo.git"
  tech_stacks:
    backend: "python, fastapi"
    frontend: "react, typescript"
  pipelines:
    enabled:
      - template: "planning_design"
        workflow: "planning_workflow"
      - template: "sdlc_execution"
        workflow: "dev_workflow"
```

### State Layer (`state/projects/<project>/`)

Auto-managed runtime state (DO NOT edit manually):
- `github_state.yaml` - Board IDs, column IDs, sync status
- `pr_review_state.yaml` - PR review cycle counts and history

Note: Dev container state is tracked separately in `state/dev_containers/`

> **For detailed configuration reference:**
> See `documentation/agent-execution-architecture.md` lines 99-152

## Agent Execution Modes

All maker agents support three modes determined automatically from task context:
- **Initial Mode**: First-time creation from requirements
- **Revision Mode**: Update based on reviewer feedback (`trigger: 'review_cycle_revision'`)
- **Question Mode**: Conversational Q&A (`trigger: 'feedback_loop'` + `conversation_mode: 'threaded'`)

> **For detailed mode detection logic and prompt building:**
> See `documentation/agent-execution-architecture.md` lines 948-1105

## Docker-in-Docker Agent Execution

Agents run in isolated Docker containers with project dependencies:

**Container Creation** (`claude/docker_runner.py`):
```python
# Build project-specific agent image (Dockerfile.agent in project)
docker build -f /workspace/<project>/Dockerfile.agent -t <project>-agent

# Run agent container with mounts
docker run \
  -v /workspace/<project>:/workspace \
  -v /home/orchestrator/.ssh:/home/orchestrator/.ssh:ro \
  <project>-agent \
  claude --project /workspace <task>
```

**Environment Requirements**:
- `dev_environment_setup` agent creates `Dockerfile.agent` for each project
- `dev_environment_verifier` agent validates the Docker image
- Images must include: project dependencies, git, Claude CLI, GitHub CLI

> **For complete container execution flow:**
> See `documentation/agent-execution-architecture.md` Phase 6 (lines 311-381)

## Common Commands

### Running the Orchestrator

```bash
# Local development (requires Python 3.11+, Redis)
python main.py

# Docker Compose (recommended)
docker-compose up -d

# View logs
docker-compose logs -f orchestrator

# Check health
curl http://localhost:5001/health
```

### Testing

```bash
# Run unit tests with pytest
pytest tests/unit/

# Run integration tests
pytest tests/integration/

# Run all tests
pytest

# Run with coverage
pytest --cov=. --cov-report=html

# Run specific test file
pytest tests/unit/test_parser.py -v

# Run with verbose output
pytest -v

# Stop on first failure
pytest -x

# Alternative: Use the test script
./scripts/run_tests.sh --unit
./scripts/run_tests.sh --integration
./scripts/run_tests.sh --all --coverage

# Alternative: Use Make commands (wrapper around scripts)
make test              # Run unit tests
make test-integration  # Run integration tests
make test-all          # Run all tests
make test-coverage     # Run with coverage
make clean-test        # Clean test artifacts
```

### Project Management

```bash
# Project workspaces are initialized automatically on startup

# Cleanup orphaned feature branches (use Make or direct Python)
make cleanup-branches
# or
PYTHONPATH=. python scripts/cleanup_orphaned_branches.py

# Cleanup specific project
make cleanup-project PROJECT=context-studio
# or
PYTHONPATH=. python scripts/cleanup_orphaned_branches.py --project context-studio

# Docker images for verified projects are checked automatically on startup
```

### GitHub Operations

```bash
# GitHub CLI must be authenticated
gh auth status

# View project boards
gh project list --owner <org>

# View issues in project
gh issue list --repo <org>/<repo>

# Create issue
gh issue create --title "..." --body "..." --label "pipeline:dev"
```

### Docker Operations

```bash
# View running containers
docker ps

# Build project agent image
docker build -f /workspace/<project>/Dockerfile.agent -t <project>-agent /workspace/<project>

# Test agent container
docker run -v /workspace/<project>:/workspace <project>-agent /bin/bash

# Clean Docker artifacts
docker system prune -a
```

## Development Workflow

### Adding a New Agent

1. Create agent class in `agents/<agent_name>_agent.py`:
```python
# Use MakerAgent for agents that create output (code, docs)
# Use AnalysisAgent for analysis-only agents (review, breakdown, PR review)
from agents.base_maker_agent import MakerAgent
from agents.base_analysis_agent import AnalysisAgent

class CustomAgent(MakerAgent):  # or AnalysisAgent
    @property
    def agent_display_name(self) -> str:
        return "Custom Agent"

    @property
    def agent_role_description(self) -> str:
        return "Brief role description"

    @property
    def output_sections(self) -> List[str]:
        return ["section1", "section2"]
```

2. Register in `agents/__init__.py`:
```python
from .custom_agent import CustomAgent

AGENT_REGISTRY = {
    "custom_agent": CustomAgent,
}
```

3. Add to `config/foundations/agents.yaml`:
```yaml
agents:
  custom_agent:
    description: "Agent description"
    model: "claude-sonnet-4-5-20250929"
    timeout: 300
    retries: 2
    makes_code_changes: false
    requires_dev_container: false
    requires_docker: true
```

### Adding a New Pipeline

1. Add template to `config/foundations/pipelines.yaml`:
```yaml
pipeline_templates:
  custom_pipeline:
    name: "Custom Pipeline"
    stages:
      - stage: "stage_name"
        default_agent: "agent_name"
        review_required: true
```

2. Enable in project config `config/projects/<project>.yaml`:
```yaml
pipelines:
  enabled:
    - template: "custom_pipeline"
      name: "custom"
```

### Modifying Agent Behavior

Agents use Claude instructions via `claude/claude_integration.py`:
- Instructions are in `.claude/agents/<agent-name>.md`
- Context includes: issue details, previous outputs, review feedback
- Output posted to GitHub as discussion comment or issue comment

## GitHub Integration

### Project Board Automation

The orchestrator automatically manages GitHub Projects v2 boards:

1. **Reconciliation Loop** (on startup):
   - Compares config vs GitHub state
   - Creates/updates project boards and columns
   - Creates repository labels for pipeline routing

2. **Monitoring Loop** (continuous):
   - Polls GitHub boards every 30 seconds
   - Detects card movements between columns
   - Enqueues tasks for appropriate agents

3. **Label-Based Routing**:
   - `pipeline:dev` → SDLC execution pipeline
   - `pipeline:epic` → Planning & design pipeline

### GitHub App vs PAT Authentication

**Personal Access Token** (simple):
- Requires `repo` and `project` scopes
- Comments appear as your user account

**GitHub App** (recommended):
- Comments appear as bot with `[bot]` badge
- Better rate limits (5000 req/hour)
- Requires private key at `~/.orchestrator/<app-name>.pem`
- Set `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`

## Monitoring and Observability

### Observability Server

The observability server runs on port 5001 and provides REST APIs for the web UI:

```bash
# Main endpoints
curl http://localhost:5001/health              # System health status
curl http://localhost:5001/agents/active       # Currently running agents
curl http://localhost:5001/history             # Agent execution history
curl http://localhost:5001/claude-logs-history # Claude logs
curl http://localhost:5001/current-pipeline    # Current pipeline state
curl http://localhost:5001/pipeline-run-events # Pipeline run events
curl http://localhost:5001/active-pipeline-runs # Active pipeline runs

# Review filter management
curl http://localhost:5001/api/review-filters              # GET filters
curl http://localhost:5001/api/review-filters -X POST      # Create filter
curl http://localhost:5001/api/review-filters/<id> -X PUT  # Update filter
curl http://localhost:5001/api/review-filters/<id> -X DELETE # Delete filter

# Circuit breaker monitoring
curl http://localhost:5001/api/circuit-breakers  # Circuit breaker states

# Project management
curl http://localhost:5001/api/projects  # Project list and status
```

**Agent Operations**:
```bash
# Kill a running agent container
curl -X POST http://localhost:5001/agents/kill/<container_name>
```

### Health Check Response

```json
{
  "healthy": true,
  "checks": {
    "redis": {"healthy": true, "message": "Connected"},
    "github": {"healthy": true, "message": "Authenticated"},
    "docker": {"healthy": true, "message": "Socket accessible"}
  },
  "timestamp": "2025-10-10T12:00:00Z"
}
```

### Logs

- **Structured JSON logging** to stdout (container logs)
- **File logging** to `orchestrator_data/logs/`
- **Pattern detection** via Elasticsearch (if enabled)
- **Web UI** at http://localhost:3000

### Metrics

Task execution and quality metrics are written to **Elasticsearch indices** with JSON file backup:

**Elasticsearch Indices:**
- `orchestrator-task-metrics-YYYY.MM.DD` - Task execution (agent, duration, success)
- `orchestrator-quality-metrics-YYYY.MM.DD` - Quality scores (agent, metric_name, score)

**Query Examples:**
```bash
# Recent task metrics
curl -s "http://localhost:9200/orchestrator-task-metrics-*/_search?size=10&sort=@timestamp:desc" | jq '.hits.hits[]._source'

# Success rate by agent
curl -s "http://localhost:9200/orchestrator-task-metrics-*/_search" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "by_agent": {
      "terms": {"field": "agent"},
      "aggs": {"success_rate": {"avg": {"field": "success"}}}
    }
  }
}' | jq '.aggregations.by_agent.buckets'
```

**JSON Backup Files:**
```bash
cat orchestrator_data/metrics/task_metrics_<date>.jsonl
cat orchestrator_data/metrics/quality_metrics_<date>.jsonl
```

## Troubleshooting

### GitHub Authentication Fails

```bash
# Check authentication
gh auth status

# Refresh token
gh auth refresh

# Verify scopes (must include 'project')
gh auth status --show-token
```

### Docker Image Build Fails

```bash
# View dev_environment_setup logs
docker-compose logs orchestrator | grep dev_environment_setup

# Manually build to debug
docker build -f /workspace/<project>/Dockerfile.agent -t <project>-agent /workspace/<project>

# Check dev container state
cat state/dev_containers/<project>_verified.yaml
```

### Agent Task Fails

```bash
# View agent logs in GitHub issue comments
# Check orchestrator logs
docker-compose logs -f orchestrator

# View task queue
# Redis CLI (queue names follow pattern: tasks:{priority})
redis-cli
> LRANGE tasks:high 0 -1
> LRANGE tasks:medium 0 -1
> LRANGE tasks:low 0 -1
```

### Redis Connection Issues

```bash
# Check Redis is running
docker-compose ps redis

# Test connection
redis-cli -h localhost -p 6379 ping

# Orchestrator falls back to in-memory queue if Redis unavailable
```

### Diagnostic Scripts

Three specialized diagnostic scripts provide deep visibility into pipeline execution and queue health:

**Pipeline Timeline** - Visualize complete pipeline execution history:
```bash
# Show timeline for a specific pipeline run
docker-compose exec orchestrator python scripts/inspect_pipeline_timeline.py <pipeline_run_id>

# With verbose output
docker-compose exec orchestrator python scripts/inspect_pipeline_timeline.py <pipeline_run_id> --verbose

# JSON output
docker-compose exec orchestrator python scripts/inspect_pipeline_timeline.py <pipeline_run_id> --json
```

**Task Health** - Monitor task queue health and detect stuck tasks:
```bash
# Check queue health
docker-compose exec orchestrator python scripts/inspect_task_health.py

# Show all tasks
docker-compose exec orchestrator python scripts/inspect_task_health.py --show-all

# Filter by project
docker-compose exec orchestrator python scripts/inspect_task_health.py --project context-studio

# JSON output (suitable for monitoring systems)
docker-compose exec orchestrator python scripts/inspect_task_health.py --json
```

**Checkpoint Inspector** - Verify pipeline recovery state:
```bash
# List recent checkpoints
docker-compose exec orchestrator python scripts/inspect_checkpoint.py

# Inspect specific pipeline
docker-compose exec orchestrator python scripts/inspect_checkpoint.py <pipeline_run_id>

# Verify recovery readiness
docker-compose exec orchestrator python scripts/inspect_checkpoint.py <pipeline_run_id> --verify-recovery
```

See `scripts/DIAGNOSTIC_SCRIPTS.md` for complete documentation, examples, and common workflows.

## Security Considerations

- API keys stored in `.env` (NEVER commit)
- SSH keys mounted read-only into containers
- Docker socket access controlled via group membership
- GitHub App private keys at `~/.orchestrator/` (mounted into container)
- Agent containers run as non-root user (UID 1000)
- Docker is run in rootfull mode because rootless docker-in-docker is not yet stable

## File Structure Reference

```
clauditoreum/
├── agents/                      # 11 registered AI agents
│   ├── base_maker_agent.py     # MakerAgent base class
│   ├── base_analysis_agent.py  # AnalysisAgent base class
│   ├── pr_review_agent.py      # PR review with requirements verification
│   └── ...
├── config/                      # Configuration system
│   ├── foundations/            # Agent, pipeline, workflow definitions
│   ├── projects/               # Project-specific configs
│   └── state_manager.py        # GitHub state management
├── pipeline/                    # Pipeline orchestration
│   ├── base.py                 # PipelineStage base class
│   └── orchestrator.py         # Sequential pipeline executor
├── services/                    # Service layer
│   ├── github_project_manager.py  # Board reconciliation
│   ├── project_monitor.py      # Board polling
│   ├── git_workflow_manager.py # Git automation
│   └── ...
├── state_management/           # Checkpointing, recovery, PR review state
├── task_queue/                 # Redis task queue
├── claude/                     # Claude integration
│   ├── claude_integration.py  # Claude API wrapper
│   ├── docker_runner.py       # Docker-in-Docker execution
│   └── session_manager.py     # Conversation state
├── monitoring/                 # Logging and metrics
├── tests/                      # Unit and integration tests
├── main.py                     # Orchestrator entry point
├── Dockerfile                  # Orchestrator container
├── docker-compose.yml          # Service orchestration
└── Makefile                    # Common commands
```

## Best Practices

- **Don't Document**: Don't create markdown files or any documentation during your tasks unless creating documentation is explicitly part of the task requirements.
- **Workspace Isolation**: The orchestrator runs in Docker - all operations are within `/workspace/` container boundary
- **Path Usage**: Use workspace-relative paths, never absolute paths that could escape boundaries
- **File Operations**: All file operations must be within `/workspace/` (the isolated orchestrator workspace)
- **Testing**: Use pytest directly or the provided test scripts/Makefile wrappers
- **Agent Output**: Agents should post outputs to GitHub, not create local files (except code agents)
- **Health Checks**: Monitor the `/health` endpoint after changes
- **Git Operations**: All git operations happen in isolated managed checkouts under `/workspace/<project>/`
- **Timeouts**: Keep agent timeouts reasonable (most: 300s, builds: 1800s)
- **Quality Assurance**: Follow maker-checker pattern for quality assurance