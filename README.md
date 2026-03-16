![Switchyard](assets/repo_header_medium.png)

# Switchyard

Switchyard is a Claude Code agent orchestrator that automates software development workflows through GitHub Projects v2 Kanban boards. It monitors boards for issue movement, routes work to specialized AI agents, executes agents in isolated Docker containers, and progresses issues through configurable SDLC pipelines — from requirements analysis through implementation, code review, and testing.

## How it works

1. A GitHub issue is labeled and added to a project board (e.g., `pipeline:sdlc-execution`).
2. `ProjectMonitor` polls the board every 15-30 seconds and detects the issue's column.
3. The pipeline orchestrator determines which agent handles that column and enqueues a task.
4. The agent runs inside a project-specific Docker container (`<project>-agent`) via Docker-in-Docker, with the project workspace mounted.
5. Agent output (comments, code, PR updates) is posted back to GitHub.
6. `PipelineProgression` moves the issue to the next column when the agent completes.

```mermaid
flowchart TD
    GH["GitHub<br>(issues & project boards)"]
    PM["ProjectMonitor<br>(polls boards every 15-30s)"]
    TQ["TaskQueue<br>(Redis priority queue)"]
    PO["Pipeline Orchestrator<br>(SequentialPipeline stages)"]
    AR["Agent Registry<br>(12 specialized agents)"]
    DR["DockerAgentRunner<br>(container lifecycle)"]
    AC["Agent Container<br>(Claude Code + project workspace)"]
    PP["PipelineProgression<br>(column advancement)"]
    ES["Elasticsearch<br>(events, logs, metrics)"]
    OB["Observability Server<br>(REST API, port 5001)"]
    UI["Web UI<br>(dashboard, port 3000)"]

    GH -->|"card movement detected"| PM
    PM -->|"enqueue task"| TQ
    TQ -->|"dequeue task"| PO
    PO -->|"select agent"| AR
    AR -->|"dispatch to"| DR
    DR -->|"launch"| AC
    AC -->|"post comments / PRs"| GH
    AC -->|"stream logs"| ES
    PO -->|"stage complete"| PP
    PP -->|"move issue"| GH
    ES -->|"query"| OB
    OB -->|"serve data"| UI
```

## Architecture overview

This project was not architected so much as it evolved organically from a simple proof of concept into a more robust system. The baseline was a simple Claude Code agent orchestrator with containerization and defined workflow support driven by GitHub Projects v2. The idea is to be able to (fairly well) investigate / spec / design / build software without being in an editor, to allow the human in the loop to stay at the context level. It is really intended to be a reference implementation and playground for experimentation, not a production-grade system. That said, it works reasonably well.

The obvious limitaions are that it's hard-wired into github and claude code. The "fix" for that I've been working on is to use this foothold and fold the learnings from this into a more modular and extensible framework: [codetoreum](https://github.com/tinkermonkey/codetoreum/). Because this system is inherently complicated, I'm focussing that project on a simulation-first hexagonal architecture approach, which will allow for much more rigorous testing and iteration on the core abstractions without needing to run the full stack, while also making it easier to swap out components (e.g., add support for other LLM providers, or other VCS / project management platforms).

## Key capabilities

- **Claude Code agent orchestration** with a registry of specialized agents for different SDLC stages and tasks
- **GitHub Projects v2 integration** for workflow management and issue tracking
- **Docker-in-Docker containerization** for isolated agent execution with project-specific environments
- **Configurable pipelines and workflows** defined in YAML for flexible process automation
- **Task queue with Redis** for reliable asynchronous processing and retry logic
- **Observability stack** with Elasticsearch for logs and metrics, and a REST API for monitoring and control
- **Web UI dashboard** for real-time visibility into pipeline runs, agent activity, and project status
- **Automated code review and PR management** through agent-driven interactions with GitHub
- **Automated dev environment setup** with dynamic Docker image building based on project tech stack
- **Self-reflection for each pipeline run** claude code generates a report at the end of each run analyzing what went well, what went poorly, and how to improve in the future, broken down for the project being worked on and the orchestrator itself

## Roadmap and future enhancements

Well, the roadmap is really to fold the learnings from this project into the more modular and extensible framework in [codetoreum](https://github.com/tinkermonkey/codetoreum/). It's a balance of continuing to iterate and improve this codebase vs. building the next one, but the changes going into this codebase are very tactical to make it good enough for the moment.

### Key components

| Component | Location | Responsibility |
|---|---|---|
| Pipeline orchestrator | `pipeline/orchestrator.py` | Sequential stage execution, review/repair cycles |
| Project monitor | `services/project_monitor.py` | Board polling, task dispatch |
| Agent registry | `agents/__init__.py` | 12 registered specialized agents |
| Claude integration | `claude/claude_integration.py` | Prompt assembly, Claude API calls |
| Docker runner | `claude/docker_runner.py` | Agent container lifecycle |
| Config manager | `config/manager.py` | Three-layer config (foundations, projects, state) |
| Task queue | `task_queue/task_manager.py` | Redis-backed priority queue |
| Observability server | `services/observability_server.py` | REST API for the web UI, port 5001 |

### Services in `docker-compose.yml`

| Service | Port | Role |
|---|---|---|
| `orchestrator` | — | Main pipeline executor |
| `observability-server` | 5001 | REST API for monitoring and agent control |
| `web-ui` | 3000 | Dashboard |
| `redis` | 6379 | Task queue and state |
| `elasticsearch` | 9200 | Metrics, logs, event storage |
| `log-collector` | — | Batches logs from Redis into Elasticsearch |
| `browserless` | 3080 | Headless Chromium for agents that need web access |

## Prerequisites

- Docker and Docker Compose (rootful Docker; rootless is not supported)
- A GitHub account with a Personal Access Token or GitHub App
  - PAT requires `repo` and `project` scopes
  - GitHub App is recommended for bot attribution and better rate limits (see `documentation/github_app_setup.md`)
- An Anthropic API key or Claude Code OAuth token
- An SSH key at `~/.ssh/id_ed25519` for git operations inside agent containers
- The orchestrator expects to run from a directory whose parent contains both `switchyard/` and the managed project checkouts. The parent directory is mounted as `/workspace` in the container.

```
~/workspace/orchestrator/      # Parent directory
├── switchyard/                # This repository
└── <your-project>/            # Managed project checkouts (cloned automatically)
```

## Getting started

### 1. Clone the repository

```bash
git clone git@github.com:<your-org>/switchyard.git
cd switchyard
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```bash
# GitHub authentication (one of the following)
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
GITHUB_ORG=your-org-or-username

# Claude authentication (one of the following)
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxx
# or
CLAUDE_CODE_OAUTH_TOKEN=your-oauth-token

# Host home directory — required for Docker-in-Docker SSH mounts
HOST_HOME=/home/your-username

# Docker group ID — run: getent group docker | cut -d: -f3
DOCKER_GID=984
```

For GitHub App authentication, also set:

```bash
GITHUB_APP_ID=
GITHUB_APP_INSTALLATION_ID=
GITHUB_APP_PRIVATE_KEY_PATH=/home/your-username/.orchestrator/your-app.pem
```

> **Note:** `HOST_HOME` must be set explicitly. Path inference is unreliable under Snap Docker and will break SSH inside agent containers.

### 3. Add a project configuration

Create `config/projects/<your-project>.yaml`. The minimal structure:

```yaml
project:
  name: "your-project"
  github:
    org: "your-org"
    repo: "your-repo"
    repo_url: "git@github.com:your-org/your-repo.git"
    branch: "main"
  tech_stacks:
    backend: "python, fastapi"
    frontend: "react, typescript"
  pipelines:
    enabled:
      - template: "sdlc_execution"
        name: "sdlc-execution"
        board_name: "SDLC Execution"
        workflow: "sdlc_execution_workflow"
        active: true
```

Available pipeline templates are defined in `config/foundations/pipelines.yaml`. Available workflow templates are in `config/foundations/workflows.yaml`.

### 4. Start the services

```bash
docker-compose up -d
```

On first startup, the orchestrator will:
- Clone the configured project repository into the parent workspace directory
- Reconcile GitHub Projects v2 boards (create boards and columns if they don't exist)
- Create GitHub labels for pipeline routing
- Queue a `dev_environment_setup` task to build the project's agent Docker image

### 5. Verify the system is running

```bash
curl http://localhost:5001/health
```

Expected response when all services are healthy:

```json
{
  "healthy": true,
  "checks": {
    "redis": {"healthy": true, "message": "Connected"},
    "github": {"healthy": true, "message": "Authenticated"},
    "docker": {"healthy": true, "message": "Socket accessible"}
  }
}
```

View orchestrator logs:

```bash
docker-compose logs -f orchestrator
```

Open the web UI at `http://localhost:3000` to see active pipeline runs and agent execution history.

## Triggering work

Move a GitHub issue to a trigger column on the project board, or create an issue with a routing label:

```bash
gh issue create \
  --repo your-org/your-repo \
  --title "Implement feature X" \
  --body "Requirements..." \
  --label "pipeline:sdlc-execution"
```

The orchestrator detects the issue within one polling interval and begins the pipeline.

## Key configuration files

| File | Purpose |
|---|---|
| `config/foundations/agents.yaml` | Agent definitions: model, timeout, Docker requirements |
| `config/foundations/pipelines.yaml` | Pipeline templates: stage sequences and review rules |
| `config/foundations/workflows.yaml` | Kanban board templates: column names and agent assignments |
| `config/projects/<project>.yaml` | Per-project settings: repo, tech stack, active pipelines |
| `.claude/agents/<agent-name>.md` | Agent prompt and instructions |

The `state/` directory is managed automatically — do not edit it manually.

## Running tests

```bash
# Unit tests only (no Docker or external services required)
make test

# All tests
make test-all

# With coverage report
make test-coverage
```

> **Note:** `agents/__init__.py` cannot be imported outside of a Docker container environment. Unit tests that touch the agent registry skip automatically.

## Deeper documentation

| Topic | Location |
|---|---|
| System architecture | `documentation/system-architecture.md` |
| Pipeline architecture | `documentation/pipeline-architecture.md` |
| Adding a project | `documentation/adding-a-project.md` |
| Configuration reference | `documentation/configuration.md` |
| GitHub authentication | `documentation/github-authentication.md` |
| Containerization and Docker | `documentation/containerization.md` |
| Code review cycle | `documentation/code-review-cycle-architecture.md` |
| PR review cycle | `documentation/pr-review-cycle-architecture.md` |
| Repair cycle | `documentation/repair-cycle-architecture.md` |
| Debugging | `documentation/debugging.md` |
| Developer reference | `CLAUDE.md` |
