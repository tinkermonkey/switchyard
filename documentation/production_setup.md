# Production Setup Guide

## Prerequisites

- Docker and Docker Compose installed
- GitHub CLI (`gh`) authenticated with your personal account or organization
- Claude Code CLI installed and authenticated
- ngrok account for webhook testing (development) or production webhook endpoint
- Python 3.9+ with PyYAML (for setup scripts)
- GitHub Personal Access Token with repo, project, and webhook permissions

## Environment Configuration

### 1. Copy Environment Template

```bash
cp .env.example .env
```

### 2. Configure Required Variables

Edit `.env` file with your specific values:

```bash
# GitHub Integration (Required)
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx       # GitHub Personal Access Token
GITHUB_ORG=your-username                     # Your GitHub username (for personal accounts) or organization name
GITHUB_DEFAULT_BRANCH=main                   # Default branch for repositories
GITHUB_WEBHOOK_SECRET=your_webhook_secret_here  # Webhook validation secret (generate random string)

# Claude/Anthropic Configuration (Required)
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxx  # Claude API key from Anthropic Console
CLAUDE_MODEL=claude-3-5-sonnet-20241022        # Claude model to use (optional, defaults to configured)
MAX_TOKENS=100000                               # Maximum tokens per request
TEMPERATURE=0.3                                 # Model temperature (0.0-1.0)

# MCP Server Configuration (Auto-configured)
CONTEXT7_MCP_URL=https://mcp.context7.com/mcp  # Context7 SaaS MCP endpoint (for library documentation)
CONTEXT7_API_KEY=your_context7_api_key_here    # Context7 API key (optional, for rate limits)
SERENA_MCP_URL=http://serena-mcp:3001          # Serena MCP server URL (for local codebase analysis)
PUPPETEER_MCP_URL=http://puppeteer-mcp:3002    # Puppeteer MCP server URL (for web automation)

# Webhook Configuration
WEBHOOK_PORT=3000                               # Port for webhook server
WEBHOOK_HOST=0.0.0.0                           # Webhook server host (0.0.0.0 for Docker)

# ngrok Configuration (for development)
NGROK_AUTHTOKEN=your_ngrok_token_here          # ngrok auth token for webhook tunneling

# Redis Configuration (Auto-configured for Docker)
REDIS_URL=redis://redis:6379                   # Redis connection URL (redis:6379 for Docker, localhost:6379 for local)
# REDIS_PASSWORD=your_redis_password           # Redis password (if authentication enabled)

# Monitoring and Logging
METRICS_PORT=8000                              # Port for metrics endpoint
LOG_LEVEL=INFO                                 # Logging level (DEBUG, INFO, WARNING, ERROR)

# Optional Production Settings
# ENVIRONMENT=production                       # Environment identifier
# SENTRY_DSN=https://xxx@sentry.io/xxx        # Sentry error tracking
```

### 3. Project Configuration (Automated Setup)

**Option A: Fully Automated Setup (Recommended)**

Run the setup script to auto-discover and configure all your GitHub projects:

```bash
python3 scripts/setup_projects.py
```

This script will:
- Auto-discover all repositories in your GitHub organization/account
- Create `config/projects.yaml` with minimal configuration
- Auto-discover or create GitHub project boards with standard columns
- Configure agent mappings for Kanban workflows

**Option B: Manual Configuration**

Create `config/projects.yaml` manually (projects are auto-cloned as siblings to the orchestrator):

```yaml
projects:
  your-project:
    repo_url: git@github.com:yourusername/your-project.git
    # Everything else is auto-derived or auto-configured:
    # - local_path: projects/your-project (from repo URL)
    # - branch: main (default)
    # - kanban_board_id: auto-discovered or auto-created
    # - kanban_columns: auto-configured with agent mappings

    # Optional overrides:
    branch: main  # Only if different from 'main'
    tech_stacks:
      frontend: react
      backend: python

    # Auto-configured by setup script (example):
    kanban_board_id: 123
    kanban_columns:
      "Backlog": null
      "Requirements Analysis": "business_analyst"
      "Design": "software_architect"
      "Ready for Development": null
      "In Development": "senior_software_engineer"
      "Code Review": "code_reviewer"
      "Testing": "senior_qa_engineer"
      "Done": null
```

**Key Changes from Previous Versions:**
- **No more `local_path`**: Auto-derived as `../{repo-name}/` (sibling to orchestrator)
- **Auto-cloning**: Projects are cloned automatically when accessed
- **Kanban auto-setup**: Project boards created with standard agent workflow columns

### 4. Pipeline Configuration

The `config/pipelines.yaml` file defines your agent workflows with MCP server integration:

```yaml
pipelines:
  business_analyst_only:
    name: "Business Analyst Pipeline"
    description: "Single agent pipeline for requirements analysis"
    agents:
      - name: business_analyst
        timeout: 300
        retries: 3

  default: business_analyst_only

agent_configs:
  business_analyst:
    claude_model: "claude-3-5-sonnet-20241022"
    working_directory: "/workspace/{project_name}"
    output_format: "structured_json"
    tools_enabled:
      - file_operations
      - git_integration
      - web_search
    mcp_servers:
      - name: context7
        url: "${CONTEXT7_MCP_URL}"
        capabilities: ["library_documentation", "api_references"]
      - name: serena
        url: "${SERENA_MCP_URL}"
        capabilities: ["codebase_analysis", "semantic_search", "code_understanding"]
      - name: puppeteer
        url: "${PUPPETEER_MCP_URL}"
        capabilities: ["browser_automation", "web_scraping"]
```

### 5. Advanced Configuration Files

**Agent Collaboration Workflows** (`config/collaboration_workflows.yaml`):
Defines maker-checker patterns, review processes, and GitHub integration settings.

**Kanban Templates** (`config/kanban_templates.yaml`):
Defines standard project board column structures and agent mappings for different project types (Full SDLC, Simple, Research-focused).

## Deployment Options

### Option 1: Complete Automated Setup (Recommended)

**One-Command Setup:**
```bash
# Run the master setup script
./scripts/setup.sh
```

This will:
1. Validate prerequisites (Docker, GitHub CLI, etc.)
2. Guide you through `.env` configuration
3. Auto-discover your GitHub repositories
4. Create GitHub project boards with standard columns
5. Set up webhook integration
6. Start all Docker services

**Manual Steps After Setup:**
```bash
# Start the orchestrator
docker-compose up orchestrator

# Monitor logs
docker-compose logs -f orchestrator
```

### Option 2: Step-by-Step Docker Deployment

#### 1. Initial Setup
```bash
# Copy and edit environment file
cp .env.example .env
# Edit .env with your tokens and configuration

# Auto-configure projects and Kanban boards
python3 scripts/setup_projects.py

# Set up complete workspace
./scripts/setup_workspace.sh
```

#### 2. Start Services
```bash
# Start all services in background
docker-compose up -d

# Check service status
docker-compose ps

# View logs
docker-compose logs -f orchestrator
docker-compose logs -f webhook
```

#### Service Components

The Docker deployment includes:
- **Orchestrator**: Main agent orchestration service with MCP integration
- **Serena MCP**: Local codebase analysis server
- **Puppeteer MCP**: Web automation server
- **Webhook Server**: GitHub webhook listener
- **Redis**: Task queue and state storage
- **ngrok**: Tunnel for webhook development

#### Environment Variables in Docker

All services use environment variables from `.env` file automatically, including MCP server URLs and API keys.

### Option 3: Local Development Setup

#### Install Dependencies

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install requirements
pip install -r requirements.txt
```

#### Start Services Manually

```bash
# Terminal 1: Start Redis
redis-server

# Terminal 2: Start Serena MCP server
uvx --from git+https://github.com/oraios/serena serena start-mcp-server --context ide-assistant --project /workspace

# Terminal 3: Start webhook server
python services/simple_webhook_server.py

# Terminal 4: Start ngrok (for webhook testing)
ngrok http 3000

# Terminal 5: Start orchestrator
python main.py
```

**Note:** For local development, you'll need to manually configure MCP server URLs in your `.env` to point to `localhost` instead of Docker service names.

## Health Checks and Monitoring

### Service Health Endpoints

```bash
# Webhook server health
curl http://localhost:3000/health

# Orchestrator metrics
curl http://localhost:8000/metrics

# MCP server health checks
curl http://localhost:3001/health  # Serena MCP (if running locally)
curl http://localhost:3002/health  # Puppeteer MCP (if running locally)

# ngrok tunnel status
curl http://localhost:4040/api/tunnels

# Redis health
redis-cli ping

# Docker service health
docker-compose ps
docker-compose exec orchestrator python -c "print('Orchestrator container healthy')"
```

### Monitoring Dashboard

Access metrics and logs:
- Orchestrator logs: `docker-compose logs -f orchestrator`
- MCP server logs: `docker-compose logs -f serena-mcp` and `docker-compose logs -f puppeteer-mcp`
- Webhook logs: `docker-compose logs -f webhook`
- All services: `docker-compose logs -f`
- Redis monitoring: `redis-cli monitor`

### System Resource Monitoring

```bash
# Check system resources
htop
# or
docker stats

# Check disk usage (important for projects directory)
df -h
docker system df
du -sh ../*  # Check sibling project directories

# Check MCP server resource usage
docker stats serena-mcp puppeteer-mcp
```

## GitHub Integration Setup

### 1. Create GitHub Personal Access Token

1. Go to GitHub Settings → Developer settings → Personal access tokens
2. Generate new token with permissions:
   - `repo` (full repository access)
   - `project` (if using GitHub Projects)
   - `admin:repo_hook` (for webhook management)

### 2. Configure Webhooks

#### Automatic Setup (Recommended)
```bash
# Run the automated setup script (handles all repos)
./scripts/setup_webhooks.sh

# Or set up individual repository webhooks
gh api repos/YOUR-USERNAME/YOUR-REPO/hooks \
    --method POST \
    --field name='web' \
    --field active=true \
    --field events='["issues", "project_card", "pull_request", "pull_request_review"]' \
    --field config[url]="https://your-ngrok-url.ngrok.io/github-webhook" \
    --field config[content_type]='json' \
    --field config[secret]="$GITHUB_WEBHOOK_SECRET"
```

#### Manual Setup
1. Go to repository Settings → Webhooks
2. Add webhook with:
   - URL: `https://your-ngrok-url.ngrok.io/github-webhook` (or your production URL)
   - Content type: `application/json`
   - Secret: Your webhook secret from `.env`
   - Events: `Issues`, `Pull requests`, `Project cards`, `Pull request reviews`

### 3. Test Integration

```bash
# Test complete workflow
gh issue create --title "Test Orchestrator" --body "Testing agent collaboration"

# Move issue through Kanban columns to trigger different agents
gh project item-edit --id ITEM_ID --field-name Status --single-select-option-name "Requirements Analysis"
```

## Production Configuration

### Security Considerations

1. **Environment Variables**: Never commit `.env` to version control
2. **Webhook Secrets**: Use strong, randomly generated secrets (use `openssl rand -hex 32`)
3. **API Keys**: Rotate GitHub tokens, Anthropic keys, and Context7 keys regularly
4. **MCP Server Security**:
   - Serena and Puppeteer MCP servers run in isolated containers
   - Projects mounted read-only to MCP servers
   - Context7 uses SaaS endpoint with API key authentication
5. **Network**: Restrict access to internal MCP server ports (3001, 3002)
6. **SSL**: Use HTTPS for webhook endpoints in production
7. **Project Access**: Projects are auto-cloned with read-only access for analysis

### Scaling Configuration

#### Redis Configuration for Production
```yaml
# docker-compose.override.yml for production
version: '3.8'
services:
  redis:
    command: redis-server --appendonly yes --requirepass ${REDIS_PASSWORD}
    volumes:
      - redis_data:/data
volumes:
  redis_data:
```

#### Orchestrator and MCP Server Scaling
```yaml
# docker-compose.override.yml for production scaling
version: '3.8'
services:
  orchestrator:
    deploy:
      replicas: 2
      resources:
        limits:
          memory: 2G
          cpus: '1.0'

  serena-mcp:
    deploy:
      resources:
        limits:
          memory: 1G
          cpus: '0.5'

  puppeteer-mcp:
    deploy:
      resources:
        limits:
          memory: 2G  # Puppeteer needs more memory for browser instances
          cpus: '0.5'
```

### Backup and Recovery

#### Backup State Data
```bash
# Backup orchestrator data
tar -czf orchestrator-backup-$(date +%Y%m%d).tar.gz orchestrator_data/

# Backup configuration files
tar -czf config-backup-$(date +%Y%m%d).tar.gz config/ .env

# Backup projects (if needed - these are auto-cloned from Git)
tar -czf projects-backup-$(date +%Y%m%d).tar.gz ../

# Backup Redis data
docker exec $(docker-compose ps -q redis) redis-cli BGSAVE
docker cp $(docker-compose ps -q redis):/data/dump.rdb ./backup/redis-$(date +%Y%m%d).rdb
```

#### Recovery Process
```bash
# Restore orchestrator data
tar -xzf orchestrator-backup-YYYYMMDD.tar.gz

# Restore Redis data
docker cp ./backup/redis-YYYYMMDD.rdb redis-container:/data/dump.rdb
docker restart redis-container
```

## Testing Production Setup

### Automated Setup Validation
```bash
# Run complete setup validation
python tests/triage_scripts/validate_configuration.py

# Test production readiness
python tests/triage_scripts/test_production_readiness.py

# Validate specific week 3 features (collaboration patterns)
python tests/triage_scripts/validate_week3_setup.py
```

### Component Testing
```bash
# Test MCP server integration
docker-compose exec orchestrator python -c "
from mcp.integration import MCPIntegration, create_mcp_integration
config = {'mcp_servers': [{'name': 'serena', 'url': 'http://serena-mcp:3001'}]}
mcp = create_mcp_integration(config)
print('MCP integration test passed' if mcp else 'MCP integration failed')
"

# Test project management
python -c "
from services.project_manager import ProjectManager
pm = ProjectManager()
print('Project manager initialized successfully')
"

# Test GitHub integration
gh auth status
gh api user
```

### End-to-End Integration Test
```bash
# Create test issue with full workflow
gh issue create --title "E2E Test: User Authentication Feature" --body "Test complete agent collaboration workflow

**User Story:**
As a user, I want to securely log into the application so that I can access my personal dashboard.

**Acceptance Criteria:**
- User can enter username and password
- System validates credentials
- User is redirected to dashboard upon success
- Error message shown for invalid credentials"

# Watch orchestrator process the issue
docker-compose logs -f orchestrator

# Monitor agent collaboration in GitHub comments
gh issue view --web
```

## Troubleshooting

### Common Issues

#### Port Conflicts
```bash
# Check port usage (now including MCP servers)
lsof -i :3000  # Webhook server
lsof -i :3001  # Serena MCP
lsof -i :3002  # Puppeteer MCP
lsof -i :8000  # Metrics

# Kill conflicting processes
kill -9 $(lsof -t -i:3000)
```

#### MCP Server Issues
```bash
# Check MCP server status
docker-compose ps serena-mcp puppeteer-mcp
docker-compose logs serena-mcp
docker-compose logs puppeteer-mcp

# Test MCP server connectivity
curl http://localhost:3001/health  # Serena (if exposed)
curl http://localhost:3002/health  # Puppeteer (if exposed)

# Restart MCP servers
docker-compose restart serena-mcp puppeteer-mcp
```

#### Project Auto-cloning Issues
```bash
# Check SSH key access to GitHub
ssh -T git@github.com

# Check projects directory permissions
ls -la ../  # Check sibling directories
docker-compose exec orchestrator ls -la /workspace/

# Manually test project cloning
python -c "
from services.project_manager import ProjectManager
pm = ProjectManager()
pm.ensure_project_cloned('your-project-name')
"
```

#### Redis Connection Issues
```bash
# Test Redis connection
redis-cli ping
docker-compose exec redis redis-cli ping

# Check Redis logs
docker-compose logs redis
```

#### Docker Issues
```bash
# Clean up Docker resources
docker-compose down
docker system prune -f
docker-compose build --no-cache
docker-compose up -d
```

#### Webhook Delivery Issues
```bash
# Check ngrok status
curl http://localhost:4040/api/tunnels

# Test webhook endpoint
curl -X POST http://localhost:3000/health

# Test webhook secret validation
curl -X POST http://localhost:3000/github-webhook \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=test" \
  -d '{"test": "webhook"}'
```

#### Agent Collaboration Issues
```bash
# Check handoff directory
ls -la orchestrator_data/handoffs/

# Check agent configuration
docker-compose exec orchestrator python -c "
import yaml
with open('config/pipelines.yaml') as f:
    config = yaml.safe_load(f)
print('Agent configs loaded:', list(config.get('agent_configs', {}).keys()))
"

# Check GitHub integration
gh auth status
gh api rate_limit
```

### Log Analysis

#### Key Log Locations
- Orchestrator: `docker-compose logs orchestrator`
- MCP servers: `docker-compose logs serena-mcp` and `docker-compose logs puppeteer-mcp`
- Webhook server: `docker-compose logs webhook`
- Redis: `docker-compose logs redis`
- All services: `docker-compose logs`

#### Important Log Patterns
```bash
# Search for errors across all services
docker-compose logs | grep -E "(ERROR|FATAL|Exception)"

# Search for successful agent completions
docker-compose logs orchestrator | grep -E "(completed|success|handoff)"

# Search for MCP server activity
docker-compose logs serena-mcp puppeteer-mcp | grep -E "(request|response|error)"

# Monitor collaboration patterns
docker-compose logs orchestrator | grep -E "(maker|checker|review|GitHub)"

# Monitor real-time with filtering
docker-compose logs -f | grep -E "(ERROR|WARNING|completed|handoff|review)"

# Check project auto-cloning
docker-compose logs orchestrator | grep -E "(cloning|project|repository)"
```

## Maintenance

### Regular Tasks

#### Daily
- Check service health endpoints (including MCP servers)
- Monitor disk space usage (especially sibling project directories)
- Review error logs across all services
- Verify GitHub webhook delivery status

#### Weekly
- Update dependencies: `docker-compose pull` and `docker-compose build`
- Clean up old checkpoint files: `find orchestrator_data/handoffs -mtime +7 -delete`
- Backup state data and configuration
- Review agent collaboration metrics
- Check project auto-cloning status

#### Monthly
- Rotate API keys (GitHub, Anthropic, Context7)
- Update MCP server containers
- Performance review using test suites
- Review and update Kanban board configurations
- Clean up unused project directories

### Performance Optimization

#### Monitor Key Metrics
- Task processing rate and agent handoff speed
- Memory usage trends (especially MCP servers)
- Circuit breaker activation frequency
- GitHub API rate limiting
- MCP server response times
- Project cloning and disk usage
- Agent collaboration success rates

#### Optimization Strategies
- Adjust Redis memory settings for handoff storage
- Scale orchestrator instances for parallel processing
- Optimize Docker resource limits for MCP servers
- Configure project cleanup policies
- Implement MCP server connection pooling
- Monitor Context7 API rate limits and upgrade if needed

## Support

For issues and support:
- **First Steps**: Run `./scripts/setup.sh` to validate your setup
- **Configuration Issues**: Run `python tests/triage_scripts/validate_configuration.py`
- **Production Readiness**: Run `python tests/triage_scripts/test_production_readiness.py`
- **Agent Collaboration**: Run `python tests/triage_scripts/validate_week3_setup.py`
- **Project Issues**: Run `python scripts/setup_projects.py` to reconfigure
- **Webhook Issues**: Run `./scripts/setup_webhooks.sh` to reconfigure webhooks
- **Check Logs**: Use troubleshooting guide log analysis commands
- **MCP Server Issues**: Check MCP server logs and connectivity
- **GitHub Integration**: Verify GitHub webhook delivery logs and API rate limits

## Quick Setup Commands

### Complete First-Time Setup
```bash
./scripts/setup.sh
```

### Reconfigure Projects and Kanban Boards
```bash
python3 scripts/setup_projects.py
```

### Fix Webhook Issues
```bash
./scripts/setup_webhooks.sh
```

### Complete Workspace Reset
```bash
./scripts/setup_workspace.sh
```

## Version Management

### Upgrading
1. **Backup Everything**:
   ```bash
   # Backup configuration and data
   tar -czf backup-$(date +%Y%m%d).tar.gz orchestrator_data/ config/ .env projects/
   ```

2. **Update Codebase**:
   ```bash
   git pull
   ```

3. **Update Dependencies**:
   ```bash
   # Update Docker images
   docker-compose pull
   docker-compose build --no-cache

   # Update setup script dependencies
   pip install -r requirements.txt --upgrade
   ```

4. **Validate Configuration**:
   ```bash
   python tests/triage_scripts/validate_configuration.py
   ./scripts/setup.sh --validate-only  # If flag exists
   ```

5. **Restart Services**:
   ```bash
   docker-compose down
   docker-compose up -d
   ```

6. **Test Integration**:
   ```bash
   python tests/triage_scripts/test_production_readiness.py
   gh issue create --title "Upgrade Test" --body "Testing post-upgrade functionality"
   ```

### Rolling Back
1. **Stop Services**:
   ```bash
   docker-compose down
   ```

2. **Revert Code**:
   ```bash
   git checkout <previous-commit>
   ```

3. **Restore Configuration**:
   ```bash
   tar -xzf backup-YYYYMMDD.tar.gz
   ```

4. **Rebuild and Start**:
   ```bash
   docker-compose build
   docker-compose up -d
   ```

### New Features in Latest Version
- **MCP Integration**: Context7, Serena, and Puppeteer MCP servers
- **Auto-Discovery**: Automated project and Kanban board setup
- **Agent Collaboration**: Enhanced maker-checker patterns with GitHub integration
- **Simplified Configuration**: Auto-derived project paths and settings
- **Setup Scripts**: Complete automation with `./scripts/setup.sh`