# CLAUDE.md

This file provides guidance to coding agents when working with this codebase.

**IMPORTANT**: Do not create markdown files or any documentation during your tasks unless creating documentation is explicitly part of the task requirements.

## Project Overview

This is the Claude Code Agent Orchestrator - an autonomous AI development system that manages GitHub-integrated software development workflows. The orchestrator coordinates specialized AI agents through GitHub Projects v2 Kanban boards, executing complete SDLC pipelines from requirements analysis to deployment.

## Core Architecture

### Technology Stack
- **Language**: Python 3.11+
- **Framework**: Async/await with asyncio
- **Queue**: Redis (with in-memory fallback)
- **Search/Analytics**: Elasticsearch 9.0
- **GitHub**: GraphQL API, REST API, GitHub CLI
- **Claude**: Anthropic Claude API (Sonnet 4.5, Opus 4)
- **Docker**: Container orchestration for agent isolation

### Key Components

**Pipeline System** (`pipeline/`)
- Sequential pipeline orchestration with checkpoints
- Circuit breaker pattern for fault tolerance
- State persistence and recovery

**Agent System** (`agents/`)
- Specialized agents for different SDLC stages
- Base classes: `MakerAgent` (creates output), `PipelineStage` (reviews/validates)
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

### Workspace Isolation

**Host File System**:
```
./                                # Orchestrator isolated workspace
│   ├── switchyard/             # This codebase
│   └── <project-name>/           # Managed project checkouts (e.g., context-studio/)
```

**Inside Orchestrator Container**:
The orchestrator always runs in Docker. The container can only see the orchestrator workspace:
```
/app/                             # Orchestrator code (switchyard/)
/workspace/                       # Orchestrator workspace root
├── switchyard/                 # Same as /app (this codebase)
└── <project-name>/               # Managed checkouts ONLY
```

**Container Volume Mounts** (from docker-compose.yml):
```yaml
volumes:
  - ./:/app                        # Host: switchyard/ → Container: /app
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

**agents.yaml**: Defines all 17 agents with capabilities, timeouts, Docker requirements
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
- `dev_container_state.yaml` - Docker image verification status

## Agent Execution Modes

All maker agents support three modes:

1. **Initial Mode**: First-time creation from requirements
2. **Revision Mode**: Update based on reviewer feedback
3. **Question Mode**: Conversational Q&A about previous output

Mode detection is automatic based on task context:
- `trigger: 'feedback_loop'` + `conversation_mode: 'threaded'` → Question mode
- `trigger: 'review_cycle_revision'` or `revision` in context → Revision mode
- Otherwise → Initial mode

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

## Common Commands

### Running the Orchestrator

```bash
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
source .venv/bin/activate && pytest tests/unit/

# Run integration tests
source .venv/bin/activate && pytest tests/integration/
```

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

**Timestamp Standardization:**
- **All timestamps use UTC with 'Z' suffix** (ISO8601 format)
- **Never use `datetime.now()`** for Elasticsearch writes
- **Always use `monitoring.timestamp_utils.utc_now()` or `utc_isoformat()`**
- Format: `2025-10-10T12:34:56.789012Z` (always UTC, always 'Z' suffix)
- This ensures correct time-range queries and proper ILM policy execution

## Security Considerations

- API keys stored in `.env` (NEVER commit)
- SSH keys mounted read-only into containers
- Docker socket access controlled via group membership
- GitHub App private keys at `~/.orchestrator/` (mounted into container)
- Agent containers run as non-root user (UID 1000)
- Docker is run in rootfull mode because rootless docker-in-docker is not yet stable

## File Structure Reference

```
switchyard/
├── agents/                      # 17 specialized AI agents
│   ├── base_maker_agent.py     # Base class for maker agents
│   ├── business_analyst_agent.py
│   ├── senior_software_engineer_agent.py
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
├── state_management/           # Checkpointing and recovery
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
