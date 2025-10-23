# Quick Reference - Critical Containerization Details

This is a condensed reference for the most critical containerization configuration details that must be correct for the system to function.

---

## Essential Volume Mounts

### Orchestrator Container → Host

```yaml
volumes:
  # Application code
  - ./:/app

  # CRITICAL: Workspace isolation boundary
  - ..:/workspace

  # CRITICAL: Docker-in-Docker
  - /var/run/docker.sock:/var/run/docker.sock

  # CRITICAL: SSH keys (read-only, 600 permissions)
  - ~/.ssh/id_ed25519:/home/orchestrator/.ssh/id_ed25519:ro
  - ~/.ssh/id_ed25519.pub:/home/orchestrator/.ssh/id_ed25519.pub:ro

  # CRITICAL: Git config (for commit author)
  - ~/.gitconfig:/home/orchestrator/.gitconfig:ro

  # GitHub App private keys
  - ~/.orchestrator:/home/orchestrator/.orchestrator
```

### Agent Container → Orchestrator Workspace

```python
volumes = {
    # Project directory (read-write)
    '/workspace/project-name': {
        'bind': '/workspace',
        'mode': 'rw'
    },

    # SSH keys (read-only)
    '/home/orchestrator/.ssh/id_ed25519': {
        'bind': '/home/orchestrator/.ssh/id_ed25519',
        'mode': 'ro'
    },

    # Git config (read-only)
    '/home/orchestrator/.gitconfig': {
        'bind': '/home/orchestrator/.gitconfig',
        'mode': 'ro'
    },

    # MCP config (read-only, if needed)
    '/workspace/project-name/.mcp.json': {
        'bind': '/workspace/.mcp.json',
        'mode': 'ro'
    }
}
```

---

## Essential Environment Variables

### Orchestrator Container

```yaml
environment:
  # CRITICAL: Claude authentication (one of these)
  CLAUDE_CODE_OAUTH_TOKEN: ${CLAUDE_CODE_OAUTH_TOKEN}
  ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}

  # GitHub authentication
  GITHUB_TOKEN: ${GITHUB_TOKEN}

  # Infrastructure
  REDIS_HOST: redis
  REDIS_PORT: 6379
  ELASTICSEARCH_HOST: elasticsearch
  ELASTICSEARCH_PORT: 9200

  # CRITICAL: User home directory
  HOME: /home/orchestrator
```

### Agent Container

```python
environment = {
    # CRITICAL: Claude authentication
    'CLAUDE_CODE_OAUTH_TOKEN': os.getenv('CLAUDE_CODE_OAUTH_TOKEN'),
    # OR
    'ANTHROPIC_API_KEY': os.getenv('ANTHROPIC_API_KEY'),

    # CRITICAL: Home directory for SSH key discovery
    'HOME': '/home/orchestrator',

    # Optional: MCP servers
    'CONTEXT7_API_KEY': os.getenv('CONTEXT7_API_KEY'),
}
```

---

## User ID Configuration

**CRITICAL**: All containers must use UID 1000

### Dockerfile (Orchestrator + Agent)
```dockerfile
RUN useradd -m -u 1000 -s /bin/bash orchestrator
USER orchestrator
```

### docker-compose.yml
```yaml
user: "1000:1000"
```

### Docker Run Command
```bash
docker run --user 1000:1000 ...
```

**Why**: File permissions must match between host, orchestrator, and agent containers.

---

## SSH Key Requirements

### On Host
```bash
# Private key must be 600
chmod 600 ~/.ssh/id_ed25519

# Public key can be 644
chmod 644 ~/.ssh/id_ed25519.pub
```

### In Containers
```bash
# Verify permissions
ls -la /home/orchestrator/.ssh/
-r-------- 1 orchestrator orchestrator 411 id_ed25519
-r--r--r-- 1 orchestrator orchestrator  99 id_ed25519.pub

# Verify ownership
stat -c "%u:%g" /home/orchestrator/.ssh/id_ed25519
# Should output: 1000:1000
```

---

## Git Configuration

### Required .gitconfig
```ini
[user]
    name = Your Name
    email = your.email@example.com
```

### Verify in Container
```bash
docker exec orchestrator git config --get user.name
docker exec orchestrator git config --get user.email
```

---

## Docker Socket Access

### On Host
```bash
# Add user to docker group
sudo usermod -aG docker $USER
newgrp docker

# Verify
ls -la /var/run/docker.sock
srw-rw---- 1 root docker 0 /var/run/docker.sock
```

### In Orchestrator Container
```bash
# Verify Docker access
docker ps
# Should work without errors
```

---

## Network Configuration

### All Containers in Same Network
```yaml
networks:
  orchestrator_default:
    driver: bridge
```

### Service Resolution
```bash
# From orchestrator container
curl http://redis:6379  # ✓
curl http://elasticsearch:9200  # ✓

# From agent container
curl http://redis:6379  # ✓ (same network)
```

---

## Container Naming

### Sanitization Rules
```python
# Docker container names must match: [a-zA-Z0-9][a-zA-Z0-9_.-]*

# Valid
claude-agent-context-studio-task_1729680000

# Invalid (will cause errors)
claude-agent-context/studio-task_1729680000  # Has slash
claude-agent-context studio-task_1729680000  # Has space
```

### Implementation
```python
def _sanitize_container_name(raw_name: str) -> str:
    sanitized = re.sub(r'[^a-zA-Z0-9_.-]', '-', raw_name)
    sanitized = re.sub(r'^[^a-zA-Z0-9]+', '', sanitized)
    sanitized = re.sub(r'-+', '-', sanitized)
    return sanitized
```

---

## Path Mappings

### Host
```
/home/user/workspace/orchestrator/
├── clauditoreum/          # Orchestrator code
└── project-name/          # Project checkout
```

### Orchestrator Container
```
/app/                      # Orchestrator code (./clauditoreum)
/workspace/                # Parent directory (..)
├── clauditoreum/
└── project-name/
```

### Agent Container
```
/workspace/                # Single project mount
├── src/
├── tests/
└── ...
```

---

## Redis Keys for Tracking

### Agent Containers
```
Key: agent_container:{container_name}
Value: {
    'project': str,
    'agent': str,
    'task_id': str,
    'started_at': str,
    'status': 'running'
}
TTL: 7200 seconds (2 hours)
```

### Repair Cycle Containers
```
Key: repair_cycle:{project}:{issue_number}
Value: {
    'container_name': str,
    'iteration': int,
    'test_type': str,
    'agent_call_count': int,
    'files_fixed': List[str],
    'timestamp': str
}
TTL: 7200 seconds (2 hours)
```

---

## Common Error Messages & Fixes

### "Permission denied (publickey)"
```
Cause: SSH keys not mounted or HOME not set
Fix:
  - Verify mounts: ~/.ssh/id_ed25519
  - Set HOME=/home/orchestrator
  - Check key permissions: chmod 600
```

### "Author identity unknown"
```
Cause: .gitconfig not mounted
Fix:
  - Mount ~/.gitconfig:/home/orchestrator/.gitconfig:ro
  - Or set GIT_AUTHOR_NAME/EMAIL env vars
```

### "bind source path does not exist"
```
Cause: Project directory not initialized
Fix:
  - Run workspace_manager.initialize_all_projects()
  - Or clone project first
```

### "container name already in use"
```
Cause: Previous container not cleaned up
Fix:
  - docker rm -f {container_name}
  - Or use --rm flag (already used)
```

### "permission denied while connecting to Docker socket"
```
Cause: User not in docker group
Fix:
  - sudo usermod -aG docker $USER
  - newgrp docker
```

### "PermissionError: [Errno 13]"
```
Cause: File created with wrong UID
Fix:
  - Ensure all containers use UID 1000
  - Check host user: id -u (should be 1000)
```

---

## Container Lifecycle Checklist

### Before Starting Agent Container
- [ ] Project directory exists at `/workspace/{project}/`
- [ ] Docker image built: `{project}-agent:latest`
- [ ] SSH keys mounted with correct permissions
- [ ] Git config mounted
- [ ] Environment variables set (CLAUDE_CODE_OAUTH_TOKEN, HOME)
- [ ] Network configured: orchestrator_default
- [ ] Container name sanitized
- [ ] Redis tracking key created

### During Agent Execution
- [ ] Stream callback forwarding events to Redis
- [ ] Collecting assistant text for result
- [ ] Tracking token usage
- [ ] Capturing session_id

### After Agent Execution
- [ ] Container removed (--rm flag)
- [ ] Redis tracking key deleted
- [ ] Output posted to GitHub
- [ ] Workspace finalized (commit/push if needed)
- [ ] Observability events emitted

---

## Dockerfile.agent Template

```dockerfile
FROM python:3.11-slim

# System dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Claude CLI
RUN curl -fsSL https://claude.ai/download/linux | sh

# GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
    https://cli.github.com/packages stable main" | \
    tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && \
    apt-get install -y gh

# CRITICAL: User with UID 1000
RUN useradd -m -u 1000 -s /bin/bash orchestrator

WORKDIR /workspace

# Project dependencies (optimize layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

USER orchestrator

CMD ["/bin/bash"]
```

---

## Diagnostic Commands

```bash
# Check orchestrator container
docker ps | grep orchestrator
docker logs orchestrator --tail 100
docker exec orchestrator ls -la /workspace/

# Check agent container (while running)
docker ps | grep claude-agent
docker logs {container_name} --tail 50
docker exec {container_name} ls -la /workspace/

# Check SSH keys in container
docker exec {container_name} ls -la /home/orchestrator/.ssh/
docker exec {container_name} stat -c "%u:%g %a" /home/orchestrator/.ssh/id_ed25519

# Check git config
docker exec {container_name} git config --get user.name
docker exec {container_name} git config --get user.email

# Check Docker socket access
docker exec orchestrator docker ps

# Check Redis tracking
docker exec orchestrator-redis redis-cli KEYS "agent_container:*"
docker exec orchestrator-redis redis-cli GET "agent_container:{name}"
```

---

## Security Checklist

- [ ] Docker socket access limited to orchestrator container only
- [ ] SSH keys mounted read-only (`:ro`)
- [ ] Private keys have 600 permissions
- [ ] Secrets passed via environment variables (not embedded in images)
- [ ] Agent containers cannot access orchestrator Redis/Elasticsearch
- [ ] Agent containers isolated to single project directory
- [ ] No privileged containers (no `--privileged` flag)
- [ ] Containers run as non-root user (UID 1000)
- [ ] Container removal guaranteed (`--rm` flag)
- [ ] Network isolation via Docker networks

---

## Performance Optimization

### Image Layer Caching
```dockerfile
# Order layers from least to most frequently changing
RUN apt-get update && apt-get install ...  # Rarely changes
RUN curl ... | sh  # Rarely changes
COPY requirements.txt .  # Changes occasionally
RUN pip install ...  # Changes occasionally
COPY . .  # Changes frequently
```

### Container Reuse
- **Agent containers**: No reuse (short-lived, different prompts)
- **Repair cycle containers**: Yes, reuse (long-lived, same issue)

### Volume Mount Performance
- Use absolute paths
- Avoid nested mounts where possible
- On macOS/Windows: Consider named volumes for performance

---

This quick reference captures the 80% of containerization details that cause 95% of issues. For complete details, see [04_containerization_architecture.md](./04_containerization_architecture.md).
