# Claude Code Orchestrator Setup Guide

The orchestrator now uses a modern configuration management system that eliminates manual setup scripts. The orchestrator automatically manages GitHub project boards based on declarative configuration files.

## 🔧 Prerequisites

### Required Software
- **Python 3.9+** with pip
- **Node.js 18+** (for MCP servers)
- **Git** with GitHub CLI (`gh`)
- **Redis** (optional - system falls back to in-memory queues)
- **Docker & Docker Compose** (for production deployment)

### Required Accounts & Tokens
- **GitHub Account** with Personal Access Token
- **Anthropic Account** with API key
- **MCP Server Access** (Context7, Serena, Puppeteer)

## 📦 Installation

### 1. Clone and Setup Python Environment

```bash
git clone <your-repo-url>
cd clauditoreum
python -m venv venv
source venv/bin/activate  # On Windows: venv\\Scripts\\activate
pip install -r requirements.txt
```

### 2. Environment Configuration

```bash
# Copy the template
cp .env.example .env

# Edit the .env file with your actual values
nano .env
```

**Required Environment Variables:**
```bash
# GitHub (Required)
GITHUB_ORG=your-org-name
GITHUB_TOKEN=ghp_xxxxxxxxxxxxx

# Anthropic (Required)
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxx

# Host Configuration (Auto-detected on Linux, defaults work on macOS)
HOST_UID=1000                    # Your user ID (run: id -u)
HOST_GID=1000                    # Your group ID (run: id -g)
DOCKER_GID=984                   # Docker group ID (run: getent group docker | cut -d: -f3)
                                 # On macOS, use DOCKER_GID=0

# MCP Servers (Optional but recommended)
CONTEXT7_MCP_URL=http://localhost:3001
CONTEXT7_API_KEY=your-context7-key
SERENA_MCP_URL=http://localhost:3002
PUPPETEER_MCP_URL=http://localhost:3003
```

**Note on HOST_UID/HOST_GID/DOCKER_GID**: These ensure the container can read mounted files like the GitHub App private key. On Linux, set them to your actual IDs. On macOS Docker Desktop, the defaults (1000/1000/0) usually work.

### 3. GitHub Authentication Setup

You can authenticate with GitHub using either a Personal Access Token (simple) or a GitHub App (recommended for bot functionality).

#### Option A: Personal Access Token (Simple)

**🔑 Create a GitHub Personal Access Token:**

1. Visit [GitHub Settings > Developer settings > Personal access tokens](https://github.com/settings/tokens)
2. Click "Generate new token" → "Generate new token (classic)"
3. **Required scopes for Projects v2:**
   - ✅ `repo` (Full control of private repositories)
   - ✅ `project` (Full control of projects) or `read:project` (Read access to projects)
   - ✅ `read:org` (Read org and team membership)

**⚠️ Important**: Projects v2 requires the `project` scope. Without this, you'll get "Resource not accessible by personal access token" errors.

**Note**: With PAT authentication, orchestrator comments appear as your user account, not as a bot.

#### Option B: GitHub App (Recommended)

**🤖 GitHub App Benefits:**
- Comments appear as `orchestrator-bot[bot]` with bot badge
- Proper `isBot` flag set to `true`
- Better rate limits (5000 req/hour per installation)
- Granular permissions model
- Professional appearance
- Enables Discussions API and advanced GraphQL features

**Quick Setup:**

1. **Create GitHub App**: See [documentation/github_app_setup.md](documentation/github_app_setup.md) for complete guide

2. **Install Private Key**:
   - **REQUIRED**: Place your GitHub App private key (`.pem` file) at `~/.orchestrator/orchestrator-bot.pem`
   - The private key file is generated when you create the GitHub App
   - This file is **required** for GitHub App authentication to work
   - The orchestrator mounts `~/.orchestrator/` into the container automatically

   ```bash
   # Create directory and place your .pem file there
   mkdir -p ~/.orchestrator
   # Copy your .pem file to ~/.orchestrator/
   chmod 644 ~/.orchestrator/orchestrator-bot.*.pem
   ```

   **Important**: The file needs `644` permissions (owner read/write, world read) so the container can read it, especially when using rootless Docker.

3. **Configure environment** in `.env`:
   ```bash
   GITHUB_APP_ID=123456
   GITHUB_APP_INSTALLATION_ID=12345678
   GITHUB_APP_PRIVATE_KEY_PATH=/home/orchestrator/.orchestrator/orchestrator-bot.2025-09-30.private-key.pem
   ```

4. **Test authentication**:
   ```bash
   PYTHONPATH=. python scripts/test_github_app.py
   ```

The orchestrator automatically uses GitHub App auth if configured, falling back to PAT if not available.

**⚠️ Important Notes:**
- GitHub App installations are **organization/repository-specific**, not server-specific
- You can reuse the same GitHub App and private key across multiple servers
- Without the `.pem` file, GitHub App authentication will fail and the system runs in degraded mode
- Check system status at http://localhost:5001/health to verify authentication configuration

### 4. GitHub CLI Setup

```bash
# Authenticate with GitHub using your token
gh auth login

# Verify authentication
gh auth status
```

### 5. Redis Setup

Redis is provided automatically via Docker Compose - no manual setup needed.

## 🔧 Configuration Architecture

The new configuration system uses a three-layer architecture:

### 1. Foundational Layer (`config/foundations/`)
- `agents.yaml`: Defines all available agents and capabilities
- `pipelines.yaml`: Reusable pipeline templates
- `workflows.yaml`: Kanban board workflow templates

### 2. Project Layer (`config/projects/`)
Create a configuration file for your project: `config/projects/your-project.yaml`

```yaml
project:
  name: "your-project"
  description: "Your project description"

  github:
    org: "your-org"
    repo: "your-repo"
    repo_url: "git@github.com:your-org/your-repo.git"
    branch: "main"

  tech_stacks:
    backend: "python, fastapi, postgresql"
    frontend: "react, typescript"

  pipelines:
    enabled:
      - template: "dev_pipeline"
        name: "development"
        board_name: "development"
        description: "Main development workflow"
        workflow: "dev_workflow"
        active: true

  pipeline_routing:
    default_pipeline: "development"
    label_routing:
      "pipeline:dev": "development"

orchestrator:
  polling_interval: 30
```

### 3. State Layer (`state/projects/`)
Runtime GitHub state is managed automatically - no manual configuration needed!

## 🚀 Quick Start

1. **Configure Environment**
   ```bash
   cp .env.example .env
   # Edit .env with your GitHub settings
   ```

2. **Configure Your Project**
   ```bash
   cp config/projects/context-studio.yaml config/projects/your-project.yaml
   # Edit with your project settings
   ```

3. **Start the Orchestrator**
   ```bash
   python main.py
   ```

The orchestrator will automatically:
- ✅ Create GitHub project boards based on your configuration
- ✅ Set up columns and labels
- ✅ Start monitoring for changes
- ✅ Handle configuration reconciliation

## 🆕 What's New: No More Setup Scripts

The old setup scripts have been **completely eliminated**. The orchestrator now handles all GitHub project management automatically through **configuration reconciliation**.

### Automatic Management
- GitHub project board creation
- Column configuration
- Label creation for pipeline routing
- State tracking and synchronization
- Configuration change detection

### Available Pipeline Templates
Choose from these pre-built pipeline templates in your project configuration:

- **`idea_development`** - Research and requirements validation
- **`dev_pipeline`** - Requirements through code development
- **`full_sdlc`** - Complete SDLC with maker-checker patterns

### 3. Agent Configuration

Individual agents can be configured with:
- Model selection (`claude-3-5-sonnet-20241022`)
- Working directories
- MCP server integrations
- Tool permissions

## 🚀 Starting the Orchestrator

```bash
# Start with Docker Compose (includes Redis)
docker-compose up -d
```

**Expected Output:**
```bash
# Check status
docker-compose ps

# View logs
docker-compose logs orchestrator
```

You should see output like:
```
Orchestrator started
📊 Reconciling project configuration: context-studio
✅ Successfully reconciled project: context-studio
🔍 Starting GitHub Projects v2 monitor...
```

**FATAL ERRORS - Orchestrator Will Stop:**
If you see logs like:
```
FATAL: Failed to reconcile project 'context-studio' - GitHub project management is not working
STOPPING ORCHESTRATOR: Cannot function without GitHub project management
```

The orchestrator will **immediately stop** when GitHub project management fails. This is intentional - the orchestrator's purpose is to automate GitHub project management, so it cannot function without it.

**Common causes:**
- Missing GitHub CLI authentication: `gh auth status`
- Missing 'project' token scope: https://github.com/settings/tokens
- No organization access or insufficient permissions
- GitHub API connectivity issues

## 📝 Usage

### 1. Manual Task Creation

```python
from task_queue.task_manager import TaskQueue, Task, TaskPriority
from datetime import datetime

# Create a task
task = Task(
    id="task-001",
    agent="business_analyst",
    project="my-project",
    priority=TaskPriority.HIGH,
    context={
        "issue": {
            "title": "Add user authentication",
            "body": "Implement secure login system",
            "labels": ["feature", "security"]
        }
    },
    created_at=datetime.now().isoformat()
)

# Submit to queue
queue = TaskQueue()
queue.enqueue(task)
```

### 2. GitHub Webhook Integration

**Setup GitHub Webhook:**
1. Go to your repository → Settings → Webhooks
2. Add webhook with URL: `https://your-domain.com:8080/webhook`
3. Secret: Use your `WEBHOOK_SECRET` from `.env`
4. Events: Issues, Pull Requests, Project Cards

**The orchestrator will automatically:**
- Process new issues through the SDLC pipeline
- Move Kanban cards and trigger appropriate agents
- Post updates back to GitHub issues

### 3. Pipeline Execution

The orchestrator runs different pipelines based on configuration:

**Business Analyst Only:**
Issue → Requirements Analysis → Done

**Development Pipeline:**
Issue → Requirements → Architecture → Development → Code Review

**Full SDLC Pipeline:**
Issue → Research → Requirements → Product Review → Architecture → Design Review → Test Planning → Development → Code Review → QA → Documentation → Documentation Review

## 🔍 Monitoring & Debugging

### Logs
```bash
# View logs in real-time
tail -f logs/orchestrator.log

# Debug mode
LOG_LEVEL=DEBUG python main.py
```

### Health Checks
```bash
# Check metrics are being recorded
cat orchestrator_data/metrics/task_metrics_$(date +%Y-%m-%d).jsonl

# Check web UI
curl http://localhost:3000
```

### Common Issues

**❌ Import Errors:**
- Ensure virtual environment is activated
- Run `pip install -r requirements.txt`

**❌ GitHub Project Creation Failures (CRITICAL):**
These are blocking errors that prevent the orchestrator's core function:

```bash
# Check GitHub CLI authentication
gh auth status

# Verify you can create projects manually
gh project create --owner YOUR_ORG --title "test-board"

# Check token scopes at https://github.com/settings/tokens
# Required: 'project' or 'read:project' scope

# Refresh authentication if needed
gh auth refresh
```

**❌ GitHub Authentication:**
```bash
gh auth status
gh auth refresh
```

**❌ Claude Code Not Found:**
- Install Claude Code: `npm install -g @anthropic/claude-code`
- Or run in simulation mode (development only)

**❌ Redis Connection:**
- Redis runs automatically in Docker Compose
- Check with: `docker-compose ps redis`

## 🔒 Security

### API Keys
- Store API keys in `.env` file only
- Never commit `.env` to version control
- Use environment-specific keys for production

### GitHub Webhook Security
- Use webhook secrets for request validation
- Limit webhook to specific events
- Use HTTPS in production

### Network Security
- Run MCP servers on private network
- Use firewall rules to limit access
- Consider VPN for production deployments

## 📈 Production Deployment

### Docker Deployment

```bash
# Start the full stack
docker-compose up -d

# With scaling (if needed)
docker-compose up --scale orchestrator=3 -d
```

### Environment Configuration

Configure via `.env` file:
```bash
LOG_LEVEL=INFO
GITHUB_TOKEN=your_token
ANTHROPIC_API_KEY=your_key
```

### Monitoring Setup

- **Metrics**: JSON logs in `orchestrator_data/metrics/`
- **Logs**: Structured JSON logging to stdout
- **Health Checks**: HTTP endpoint at `/health`
- **Alerts**: Configure based on task failure rates

## 🧪 Testing

```bash
# Run unit tests
python -m pytest tests/unit/

# Run integration tests
python -m pytest tests/integration/

# Test specific components
python -m pytest tests/integration/test_basic_orchestration.py

# Validate production readiness
python tests/triage_scripts/test_production_readiness.py
```

## 📚 Agent Development

### Creating New Agents

1. **Create Agent Class:**
```python
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code

class CustomAgent(PipelineStage):
    def __init__(self, agent_config=None):
        super().__init__("custom_agent", agent_config=agent_config)

    async def execute(self, context):
        # Agent implementation
        pass
```

2. **Register in Agent Registry:**
```python
# In agents/__init__.py
from .custom_agent import CustomAgent

AGENT_REGISTRY = {
    # ... existing agents
    "custom_agent": CustomAgent,
}
```

3. **Configure Pipeline:**
```yaml
# In config/pipelines.yaml
custom_pipeline:
  agents:
    - name: custom_agent
      timeout: 300
      retries: 2
```

### Agent Best Practices

- **Follow established patterns** from existing agents
- **Implement proper error handling** with circuit breakers
- **Use collaborative handoffs** for agent-to-agent communication
- **Post GitHub updates** for transparency
- **Include quality metrics** in outputs
- **Support MCP integration** for enhanced capabilities

## 🆘 Support

- **Documentation**: See `documentation/vision.md` for architecture details
- **Issues**: Report bugs and feature requests in GitHub Issues
- **Discussions**: Use GitHub Discussions for questions
- **Contributing**: See `CONTRIBUTING.md` for development guidelines

---

*Generated by Claude Code Orchestrator Setup Assistant* 🤖