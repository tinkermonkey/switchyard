# Containerization Architecture - Docker-in-Docker Deep Dive

This document provides comprehensive detail on the containerization architecture, which is one of the most complex aspects of the orchestrator system. Getting containerization right is critical for security, isolation, and proper functionality.

---

## Overview of Containerization Strategy

The orchestrator uses a **three-tier containerization model**:

1. **Orchestrator Container** - The main orchestrator runs in Docker
2. **Agent Containers** - Each agent execution runs in a project-specific container (Docker-in-Docker)
3. **Repair Cycle Containers** - Long-running containers for test-fix cycles (Docker-in-Docker)

```
┌─────────────────────────────────────────────────────────────────┐
│ HOST SYSTEM                                                      │
│                                                                  │
│  ┌────────────────────────────────────────────────────────┐    │
│  │ ORCHESTRATOR CONTAINER                                  │    │
│  │ (switchyard)                                          │    │
│  │                                                          │    │
│  │  ┌──────────────────────────────────────────────────┐  │    │
│  │  │ AGENT CONTAINER                                   │  │    │
│  │  │ (project-name-agent)                              │  │    │
│  │  │                                                    │  │    │
│  │  │  - Claude CLI                                     │  │    │
│  │  │  - Project dependencies                           │  │    │
│  │  │  - Git                                            │  │    │
│  │  │  - MCP servers                                    │  │    │
│  │  └──────────────────────────────────────────────────┘  │    │
│  │                                                          │    │
│  │  ┌──────────────────────────────────────────────────┐  │    │
│  │  │ REPAIR CYCLE CONTAINER                            │  │    │
│  │  │ (repair-project-issue-123)                        │  │    │
│  │  │                                                    │  │    │
│  │  │  - Long-running (hours)                           │  │    │
│  │  │  - Test execution                                 │  │    │
│  │  │  - Iterative fixing                               │  │    │
│  │  │  - Checkpoint persistence                         │  │    │
│  │  └──────────────────────────────────────────────────┘  │    │
│  │                                                          │    │
│  └────────────────────────────────────────────────────────┘    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Layer 1: Orchestrator Container

### Dockerfile Configuration

**Location**: `/switchyard/Dockerfile`

```dockerfile
FROM python:3.12-slim

# Create orchestrator user with specific UID
RUN useradd -m -u 1000 -s /bin/bash orchestrator

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    docker.io \  # Docker CLI for Docker-in-Docker
    && rm -rf /var/lib/apt/lists/*

# Install Claude CLI
RUN curl -fsSL https://claude.ai/download/linux | sh

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
    https://cli.github.com/packages stable main" | \
    tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && \
    apt-get install -y gh

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create workspace directory
RUN mkdir -p /workspace && chown orchestrator:orchestrator /workspace

# Set working directory
WORKDIR /app

# Copy application code
COPY . .

# Switch to orchestrator user
USER orchestrator

# Entry point
CMD ["python", "main.py"]
```

### docker-compose.yml Configuration

**Location**: `/switchyard/docker-compose.yml`

**Critical Volume Mounts**:

```yaml
services:
  orchestrator:
    build: .
    container_name: orchestrator
    user: "1000:1000"  # Run as UID 1000 (orchestrator user)

    volumes:
      # Application code (bind mount for development)
      - ./:/app

      # CRITICAL: Workspace isolation
      # Maps parent directory on host to /workspace in container
      # This creates the isolated workspace boundary
      - ..:/workspace

      # CRITICAL: Docker socket for Docker-in-Docker
      # Allows orchestrator to spawn agent containers
      # MUST use rootfull Docker (rootless not stable for DinD)
      - /var/run/docker.sock:/var/run/docker.sock

      # CRITICAL: SSH keys (read-only)
      # Needed for git operations, must preserve permissions
      - ~/.ssh/id_ed25519:/home/orchestrator/.ssh/id_ed25519:ro
      - ~/.ssh/id_ed25519.pub:/home/orchestrator/.ssh/id_ed25519.pub:ro

      # CRITICAL: Git config (read-only)
      # Needed for git commit author info
      - ~/.gitconfig:/home/orchestrator/.gitconfig:ro

      # CRITICAL: GitHub App private keys
      # Mounted from host ~/.orchestrator/ directory
      - ~/.orchestrator:/home/orchestrator/.orchestrator

      # Data persistence
      - ./orchestrator_data:/app/orchestrator_data

    environment:
      # Authentication
      CLAUDE_CODE_OAUTH_TOKEN: ${CLAUDE_CODE_OAUTH_TOKEN}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      CONTEXT7_API_KEY: ${CONTEXT7_API_KEY}

      # GitHub authentication
      GITHUB_TOKEN: ${GITHUB_TOKEN}
      GITHUB_APP_ID: ${GITHUB_APP_ID}
      GITHUB_APP_INSTALLATION_ID: ${GITHUB_APP_INSTALLATION_ID}
      GITHUB_APP_PRIVATE_KEY_PATH: ${GITHUB_APP_PRIVATE_KEY_PATH}

      # Infrastructure
      REDIS_HOST: redis
      REDIS_PORT: 6379
      ELASTICSEARCH_HOST: elasticsearch
      ELASTICSEARCH_PORT: 9200

      # User context
      HOME: /home/orchestrator

    networks:
      - orchestrator_default

    depends_on:
      - redis
      - elasticsearch

  redis:
    image: redis:7-alpine
    container_name: orchestrator-redis
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    networks:
      - orchestrator_default

  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.11.0
    container_name: orchestrator-elasticsearch
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
      - "ES_JAVA_OPTS=-Xms512m -Xmx512m"
    ports:
      - "9200:9200"
    volumes:
      - es_data:/usr/share/elasticsearch/data
    networks:
      - orchestrator_default

networks:
  orchestrator_default:
    driver: bridge

volumes:
  redis_data:
  es_data:
```

### Critical Path Mappings

**On Host**:
```
/home/user/workspace/orchestrator/
├── switchyard/          # Orchestrator code
└── project-name/          # Managed project checkouts
```

**In Orchestrator Container**:
```
/app/                      # Orchestrator code (./switchyard mapped here)
/workspace/                # Parent directory from host
├── switchyard/          # Same as /app
└── project-name/          # Project checkouts
```

**Critical Insight**: The orchestrator can ONLY see `/workspace/`, which maps to a specific location on the host defined in docker-compose.yml. This creates the workspace isolation boundary.

### User ID Management

**Why UID 1000?**
- Most Linux systems use UID 1000 for the first non-root user
- Files created by orchestrator must be writable by host user
- SSH keys must have correct ownership (600 permissions)
- Git commits must have correct author

**Permission Issues**:
```bash
# In orchestrator container as UID 1000
touch /workspace/project/test.txt
# Creates file owned by 1000:1000

# On host (if host user is UID 1000)
ls -la project/test.txt
# -rw-r--r-- 1 hostuser hostuser 0 Oct 23 10:00 test.txt
# ✓ Host user can read/write

# If host user is different UID (e.g., 1001)
ls -la project/test.txt
# -rw-r--r-- 1 1000 1000 0 Oct 23 10:00 test.txt
# ✗ Permission denied for host user
```

**Solution**: Ensure host user is UID 1000, or adjust Dockerfile USER directive.

### Docker Socket Access

**Critical Configuration**:
```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
```

**What This Enables**:
- Orchestrator container can run `docker` commands
- Can create/start/stop/remove agent containers
- Can inspect Docker images
- Can access Docker networks

**Security Implications**:
- Orchestrator has **full Docker access** (equivalent to root on host)
- Can spawn any container on host
- Can access all host volumes
- **MUST** run trusted code only

**Why Rootfull Docker?**
- Rootless Docker-in-Docker is not stable/mature
- Volume mount permissions are complex in rootless mode
- Docker socket sharing doesn't work reliably rootless
- Production orchestrators typically use rootfull Docker with proper isolation

### SSH Key Mounting

**Critical for Git Operations**:
```yaml
volumes:
  - ~/.ssh/id_ed25519:/home/orchestrator/.ssh/id_ed25519:ro
  - ~/.ssh/id_ed25519.pub:/home/orchestrator/.ssh/id_ed25519.pub:ro
```

**Requirements**:
1. **Read-only mount** (`:ro`) - Prevents accidental modification
2. **Correct permissions** - Private key must be 600
3. **Correct ownership** - Must be owned by UID 1000
4. **SSH agent forwarding** - Not used (keys copied instead)

**Permission Verification**:
```bash
# Inside orchestrator container
ls -la /home/orchestrator/.ssh/
# -r-------- 1 orchestrator orchestrator 411 Oct 20 10:00 id_ed25519
# -r--r--r-- 1 orchestrator orchestrator  99 Oct 20 10:00 id_ed25519.pub
```

**Git Operations**:
```bash
# Git uses SSH keys automatically
git clone git@github.com:org/repo.git
# Uses /home/orchestrator/.ssh/id_ed25519

git push origin feature/issue-123
# Authenticates with SSH key
```

### Git Config Mounting

**Critical for Commits**:
```yaml
volumes:
  - ~/.gitconfig:/home/orchestrator/.gitconfig:ro
```

**What's in .gitconfig**:
```ini
[user]
    name = Your Name
    email = your.email@example.com

[core]
    editor = vim

[init]
    defaultBranch = main
```

**Why It Matters**:
- Every git commit needs author name/email
- Without .gitconfig, commits fail
- Must match GitHub account for attribution

**Auto-Commit Example**:
```python
# In auto_commit.py
subprocess.run([
    'git', 'commit', '-m',
    f"{message}\n\n🤖 Generated with Claude Code\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
])
# Uses name/email from mounted .gitconfig
```

---

## Layer 2: Agent Container (Docker-in-Docker)

### Agent Container Lifecycle

**Location**: `claude/docker_runner.py`

#### Phase 1: Image Building

**Triggered by**: `dev_environment_setup` agent

**Dockerfile.agent Location**: `/workspace/{project}/Dockerfile.agent`

**Generated by Setup Agent**:
```dockerfile
# Example Dockerfile.agent for a Python/FastAPI project
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Claude CLI
RUN curl -fsSL https://claude.ai/download/linux | sh

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
    https://cli.github.com/packages stable main" | \
    tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && \
    apt-get install -y gh

# Create orchestrator user with UID 1000
RUN useradd -m -u 1000 -s /bin/bash orchestrator

# Set working directory
WORKDIR /workspace

# Copy project files for dependency installation
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project
COPY . .

# Switch to orchestrator user
USER orchestrator

# Default command (overridden at runtime)
CMD ["/bin/bash"]
```

**Build Command**:
```bash
docker build \
    -f /workspace/project-name/Dockerfile.agent \
    -t project-name-agent:latest \
    /workspace/project-name/
```

**Critical Details**:
1. **Build Context**: `/workspace/project-name/` - All files in project directory
2. **UID 1000**: Must match orchestrator container for file permissions
3. **Claude CLI**: Must be installed for agent execution
4. **Git + GH CLI**: Required for repository operations
5. **Project Dependencies**: Installed during build (cached layer)

#### Phase 2: Container Name Generation

**Logic in docker_runner.py**:
```python
def _sanitize_container_name(raw_name: str) -> str:
    """
    Sanitize container name to meet Docker requirements.

    Docker container names must match: [a-zA-Z0-9][a-zA-Z0-9_.-]*
    - Start with alphanumeric
    - Contain only: alphanumeric, underscore, period, hyphen
    - No spaces, special chars, or slashes
    """
    # Remove invalid characters
    sanitized = re.sub(r'[^a-zA-Z0-9_.-]', '-', raw_name)

    # Ensure starts with alphanumeric
    sanitized = re.sub(r'^[^a-zA-Z0-9]+', '', sanitized)

    # Collapse multiple hyphens
    sanitized = re.sub(r'-+', '-', sanitized)

    return sanitized

# Usage
raw_name = f"claude-agent-{project_name}-{task_id}"
container_name = _sanitize_container_name(raw_name)
# Example: "claude-agent-context-studio-task_business_analyst_1729680000"
```

**Why Sanitization Matters**:
- Task IDs contain underscores and timestamps
- Project names may have special characters
- Docker rejects invalid names with cryptic errors

#### Phase 3: Volume Mount Configuration

**Critical Mounts**:

```python
volumes = {
    # CRITICAL: Project directory (read-write)
    str(project_dir): {
        'bind': '/workspace',
        'mode': 'rw'
    },

    # CRITICAL: SSH keys (read-only)
    str(Path.home() / '.ssh/id_ed25519'): {
        'bind': '/home/orchestrator/.ssh/id_ed25519',
        'mode': 'ro'
    },
    str(Path.home() / '.ssh/id_ed25519.pub'): {
        'bind': '/home/orchestrator/.ssh/id_ed25519.pub',
        'mode': 'ro'
    },

    # CRITICAL: Git config (read-only)
    str(Path.home() / '.gitconfig'): {
        'bind': '/home/orchestrator/.gitconfig',
        'mode': 'ro'
    }
}

# If MCP servers configured: Add .mcp.json
if mcp_servers:
    mcp_config_path = project_dir / '.mcp.json'
    volumes[str(mcp_config_path)] = {
        'bind': '/workspace/.mcp.json',
        'mode': 'ro'
    }
```

**Path Resolution**:
```python
# Inside orchestrator container
project_dir = Path('/workspace/project-name')  # Absolute path in container

# When mounting to agent container, Docker sees:
# Host path (from orchestrator's perspective): /workspace/project-name
# But this is actually mapped to host: /home/user/workspace/orchestrator/project-name

# Docker resolves this correctly because:
# 1. Orchestrator has access to /var/run/docker.sock
# 2. Docker daemon sees actual host paths
# 3. Volume mounts are resolved by Docker daemon, not orchestrator
```

**Nested Mount Visualization**:
```
HOST:
/home/user/workspace/orchestrator/project-name/
    ↓ (mounted to orchestrator)
ORCHESTRATOR CONTAINER:
/workspace/project-name/
    ↓ (mounted to agent)
AGENT CONTAINER:
/workspace/
```

#### Phase 4: Environment Variables

**Environment Configuration**:
```python
environment = {
    # CRITICAL: Authentication for Claude Code
    'CLAUDE_CODE_OAUTH_TOKEN': os.getenv('CLAUDE_CODE_OAUTH_TOKEN'),
    # OR
    'ANTHROPIC_API_KEY': os.getenv('ANTHROPIC_API_KEY'),

    # CRITICAL: Home directory for SSH keys
    'HOME': '/home/orchestrator',

    # Optional: Context7 MCP server
    'CONTEXT7_API_KEY': os.getenv('CONTEXT7_API_KEY'),

    # Git configuration
    'GIT_AUTHOR_NAME': git_config.get('user.name'),
    'GIT_AUTHOR_EMAIL': git_config.get('user.email'),
    'GIT_COMMITTER_NAME': git_config.get('user.name'),
    'GIT_COMMITTER_EMAIL': git_config.get('user.email'),
}

# Remove None values
environment = {k: v for k, v in environment.items() if v is not None}
```

**Why Each Variable Matters**:

1. **CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY**:
   - Required for Claude CLI authentication
   - Without this, `claude` command fails
   - Subscription vs pay-per-use billing

2. **HOME=/home/orchestrator**:
   - SSH looks for keys at `$HOME/.ssh/id_ed25519`
   - Without this, SSH can't find mounted keys
   - Git uses `$HOME/.gitconfig`

3. **CONTEXT7_API_KEY**:
   - Optional MCP server authentication
   - Only needed if Context7 MCP server configured

4. **GIT_AUTHOR_* / GIT_COMMITTER_***:
   - Fallback if .gitconfig mount fails
   - Ensures commits have correct attribution

#### Phase 5: Network Configuration

**Network Setup**:
```python
# Agent containers join orchestrator's network
network_mode = 'orchestrator_default'

# This allows:
# - Agent can reach Redis at 'redis:6379'
# - Agent can reach Elasticsearch at 'elasticsearch:9200'
# - Agent can reach any other services in orchestrator network
```

**Why This Matters**:
- Agents may need to query Elasticsearch for context
- Some MCP servers run as containers in same network
- Future: Agent-to-agent communication

#### Phase 6: Docker Run Command

**Full Command Construction**:
```python
docker_command = [
    'docker', 'run',
    '--rm',  # Auto-remove on exit
    '--name', container_name,
    '--user', '1000:1000',  # UID:GID
    '--workdir', '/workspace',
    '--network', 'orchestrator_default',
]

# Add volume mounts
for host_path, mount_config in volumes.items():
    docker_command.extend([
        '-v', f"{host_path}:{mount_config['bind']}:{mount_config['mode']}"
    ])

# Add environment variables
for key, value in environment.items():
    docker_command.extend(['-e', f"{key}={value}"])

# Image and command
docker_command.extend([
    f"{project_name}-agent:latest",
    'claude',
    '--print',
    '--verbose',
    '--output-format', 'stream-json',
    '--model', claude_model,
    '--permission-mode', 'bypassPermissions',
    prompt
])
```

**Actual Command Example**:
```bash
docker run \
    --rm \
    --name claude-agent-context-studio-task_1729680000 \
    --user 1000:1000 \
    --workdir /workspace \
    --network orchestrator_default \
    -v /workspace/context-studio:/workspace:rw \
    -v /home/orchestrator/.ssh/id_ed25519:/home/orchestrator/.ssh/id_ed25519:ro \
    -v /home/orchestrator/.ssh/id_ed25519.pub:/home/orchestrator/.ssh/id_ed25519.pub:ro \
    -v /home/orchestrator/.gitconfig:/home/orchestrator/.gitconfig:ro \
    -v /workspace/context-studio/.mcp.json:/workspace/.mcp.json:ro \
    -e CLAUDE_CODE_OAUTH_TOKEN=tok_abc123... \
    -e HOME=/home/orchestrator \
    -e CONTEXT7_API_KEY=ctx7_xyz789... \
    context-studio-agent:latest \
    claude \
        --print \
        --verbose \
        --output-format stream-json \
        --model claude-sonnet-4-5-20250929 \
        --permission-mode bypassPermissions \
        "You are a Business Analyst. Analyze the following requirement..."
```

#### Phase 7: Container Tracking in Redis

**Before Starting Container**:
```python
# Store container metadata in Redis
redis_key = f"agent_container:{container_name}"
redis_value = json.dumps({
    'project': project_name,
    'agent': agent_name,
    'task_id': task_id,
    'started_at': utc_isoformat(),
    'status': 'running',
    'container_name': container_name
})

redis.set(redis_key, redis_value, ex=7200)  # 2 hour TTL
```

**Why This Matters**:
- On orchestrator restart, can detect running containers
- Distinguish between legitimate containers and orphans
- Recovery logic uses this to decide: recover or kill

**After Container Exits**:
```python
# Clean up tracking key
redis.delete(f"agent_container:{container_name}")
```

#### Phase 8: Log Streaming

**Stream Processing**:
```python
# Start container in background
process = subprocess.Popen(docker_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

# Stream stdout (Claude Code JSON events)
for line in iter(process.stdout.readline, ''):
    if not line:
        break

    try:
        event = json.loads(line)

        # Forward to stream_callback for websocket
        if stream_callback:
            stream_callback(event)

        # Extract assistant text for result
        if event.get('type') == 'assistant':
            content = event.get('message', {}).get('content', [])
            for item in content:
                if item.get('type') == 'text':
                    result_parts.append(item.get('text', ''))

        # Track token usage
        if 'usage' in event:
            input_tokens = event['usage'].get('input_tokens', 0)
            output_tokens = event['usage'].get('output_tokens', 0)

        # Capture session_id for continuity
        if 'session_id' in event:
            session_id = event['session_id']

    except json.JSONDecodeError:
        logger.warning(f"Non-JSON output: {line}")

# Wait for container to exit
exit_code = process.wait()
```

**Stream Callback Flow**:
```
Agent Container stdout
    ↓
Docker logs
    ↓
Orchestrator subprocess.Popen
    ↓
Parse JSON events
    ↓
stream_callback(event)
    ↓
Redis pub/sub: orchestrator:claude_stream
    ↓
Web UI websocket
```

#### Phase 9: Container Cleanup

**Normal Exit**:
```python
# Container already removed (--rm flag)
# Just clean up Redis tracking
redis.delete(f"agent_container:{container_name}")
```

**Abnormal Exit (Error/Timeout)**:
```python
try:
    # Try graceful stop
    subprocess.run(['docker', 'stop', container_name], timeout=10)
except:
    # Force kill
    subprocess.run(['docker', 'kill', container_name])

# Remove container
subprocess.run(['docker', 'rm', '-f', container_name])

# Clean up Redis
redis.delete(f"agent_container:{container_name}")
```

### Common Issues & Solutions

#### Issue 1: Permission Denied on Files

**Symptom**:
```
PermissionError: [Errno 13] Permission denied: '/workspace/file.txt'
```

**Root Cause**:
- File created by host user with different UID
- Or file created by agent with UID 1000, but host user is different UID

**Solution**:
```bash
# Check host user UID
id -u
# If not 1000, adjust Dockerfile:
# RUN useradd -m -u $(id -u) -s /bin/bash orchestrator
```

#### Issue 2: SSH Key Not Found

**Symptom**:
```
Permission denied (publickey).
fatal: Could not read from remote repository.
```

**Root Cause**:
- SSH keys not mounted
- HOME environment variable not set
- Key permissions wrong (not 600)

**Debug**:
```python
# Inside agent container
import os
print(os.environ.get('HOME'))  # Must be /home/orchestrator
print(os.path.exists('/home/orchestrator/.ssh/id_ed25519'))  # Must be True
os.system('ls -la /home/orchestrator/.ssh/')  # Check permissions
```

**Solution**:
```python
# Ensure mounts in docker_runner.py
volumes = {
    str(Path.home() / '.ssh/id_ed25519'): {
        'bind': '/home/orchestrator/.ssh/id_ed25519',
        'mode': 'ro'
    },
}
environment = {
    'HOME': '/home/orchestrator'
}
```

#### Issue 3: Claude CLI Not Found

**Symptom**:
```
/bin/sh: claude: not found
```

**Root Cause**:
- Claude CLI not installed in Dockerfile.agent
- Or installed in wrong location

**Solution**:
```dockerfile
# In Dockerfile.agent
RUN curl -fsSL https://claude.ai/download/linux | sh

# Verify installation
RUN which claude  # Should output /usr/local/bin/claude
```

#### Issue 4: Git Commit Fails (Author Unknown)

**Symptom**:
```
Author identity unknown

*** Please tell me who you are.
```

**Root Cause**:
- .gitconfig not mounted
- Or GIT_AUTHOR_* env vars not set

**Solution**:
```python
# Mount .gitconfig
volumes[str(Path.home() / '.gitconfig')] = {
    'bind': '/home/orchestrator/.gitconfig',
    'mode': 'ro'
}

# Or set env vars
environment.update({
    'GIT_AUTHOR_NAME': 'Your Name',
    'GIT_AUTHOR_EMAIL': 'your@email.com',
    'GIT_COMMITTER_NAME': 'Your Name',
    'GIT_COMMITTER_EMAIL': 'your@email.com',
})
```

#### Issue 5: Container Name Conflicts

**Symptom**:
```
docker: Error response from daemon: Conflict. The container name "/claude-agent-..."
is already in use by container "abc123...".
```

**Root Cause**:
- Previous container with same name still exists (not cleaned up)
- Or container crashed and wasn't removed

**Solution**:
```python
# Before starting new container, ensure old one is gone
try:
    subprocess.run(['docker', 'rm', '-f', container_name],
                   capture_output=True, timeout=10)
except:
    pass  # Ignore if doesn't exist

# Then start new container
```

#### Issue 6: Volume Mount Path Not Found

**Symptom**:
```
Error response from daemon: invalid mount config for type "bind":
bind source path does not exist: /workspace/project-name
```

**Root Cause**:
- Project directory doesn't exist yet
- Or path resolution incorrect from orchestrator perspective

**Debug**:
```python
# In orchestrator container
project_dir = workspace_manager.get_project_dir(project_name)
print(f"Project dir: {project_dir}")
print(f"Exists: {project_dir.exists()}")
print(f"Absolute: {project_dir.absolute()}")

# Should output:
# Project dir: /workspace/project-name
# Exists: True
# Absolute: /workspace/project-name
```

**Solution**:
```python
# Ensure project initialized before agent execution
workspace_manager.initialize_all_projects()

# Or clone on-demand
if not project_dir.exists():
    workspace_manager.clone_or_update_project(project_name)
```

---

## Layer 3: Repair Cycle Container (Long-Running DinD)

### Repair Cycle Unique Requirements

Repair cycle containers are fundamentally different from agent containers:

1. **Long-Running**: Hours instead of minutes
2. **Stateful**: Must preserve state across orchestrator restarts
3. **Interactive**: Multiple Claude CLI invocations within same container
4. **Test Execution**: Must run test commands (pytest, npm test, etc.)
5. **Checkpoint Persistence**: State saved to Redis after each iteration

### Container Lifecycle

**Location**: `pipeline/repair_cycle_runner.py`

#### Phase 1: Container Creation or Recovery

```python
def recover_or_create_repair_cycle_container(
    project: str,
    issue_number: int,
    task_context: Dict[str, Any]
) -> Tuple[str, Optional[Dict]]:
    """
    Check if repair cycle container already exists (from previous run).
    If exists and valid checkpoint: RECOVER
    If exists but stale: KILL and CREATE
    If not exists: CREATE
    """

    # Check Redis for existing container
    redis_key = f"repair_cycle:{project}:{issue_number}"
    checkpoint_json = redis.get(redis_key)

    if checkpoint_json:
        checkpoint = json.loads(checkpoint_json)
        container_name = checkpoint['container_name']

        # Verify container still running
        result = subprocess.run(
            ['docker', 'inspect', '--format', '{{.State.Running}}', container_name],
            capture_output=True, text=True
        )

        if result.returncode == 0 and result.stdout.strip() == 'true':
            # Container running, checkpoint valid
            logger.info(f"Recovering repair cycle container: {container_name}")

            # Calculate checkpoint age
            checkpoint_time = datetime.fromisoformat(checkpoint['timestamp'])
            checkpoint_age = (utc_now() - checkpoint_time).total_seconds()

            if checkpoint_age < 7200:  # Less than 2 hours old
                # RECOVER: Resume from checkpoint
                return container_name, checkpoint
            else:
                # Stale checkpoint, kill and recreate
                logger.warning(f"Checkpoint stale ({checkpoint_age}s), recreating container")
                subprocess.run(['docker', 'kill', container_name])
                subprocess.run(['docker', 'rm', container_name])
                redis.delete(redis_key)
                # Fall through to CREATE

    # CREATE: No valid container/checkpoint found
    container_name = f"repair-{project}-{issue_number}"

    # Start long-running container
    subprocess.run([
        'docker', 'run',
        '-d',  # Detached mode (background)
        '--name', container_name,
        '--user', '1000:1000',
        '--workdir', '/workspace',
        '--network', 'orchestrator_default',
        '-v', f'/workspace/{project}:/workspace:rw',
        '-v', f'{Path.home()}/.ssh/id_ed25519:/home/orchestrator/.ssh/id_ed25519:ro',
        '-v', f'{Path.home()}/.gitconfig:/home/orchestrator/.gitconfig:ro',
        '-e', f'CLAUDE_CODE_OAUTH_TOKEN={os.getenv("CLAUDE_CODE_OAUTH_TOKEN")}',
        '-e', 'HOME=/home/orchestrator',
        f'{project}-agent:latest',
        'tail', '-f', '/dev/null'  # Keep alive indefinitely
    ])

    # Initialize checkpoint in Redis
    initial_checkpoint = {
        'container_name': container_name,
        'run_id': f"repair_{project}_{issue_number}_{int(utc_now().timestamp())}",
        'started_at': utc_isoformat(),
        'iteration': 0,
        'test_type': None,
        'agent_call_count': 0,
        'files_fixed': [],
        'timestamp': utc_isoformat()
    }

    redis.set(redis_key, json.dumps(initial_checkpoint), ex=7200)

    return container_name, None  # No checkpoint to recover
```

#### Phase 2: Checkpoint Persistence

**After Every Iteration**:
```python
def save_checkpoint(
    project: str,
    issue_number: int,
    container_name: str,
    iteration: int,
    test_type: str,
    agent_call_count: int,
    files_fixed: List[str],
    test_failures: List[Dict],
    warnings: List[Dict]
):
    """
    Save repair cycle state to Redis.

    Critical for recovery after orchestrator restart.
    """
    checkpoint = {
        'container_name': container_name,
        'iteration': iteration,
        'test_type': test_type,
        'agent_call_count': agent_call_count,
        'files_fixed': files_fixed,
        'test_failures': test_failures,
        'warnings': warnings,
        'timestamp': utc_isoformat()
    }

    redis_key = f"repair_cycle:{project}:{issue_number}"
    redis.set(redis_key, json.dumps(checkpoint), ex=7200)

    # Emit observability event
    obs.emit_repair_cycle_container_checkpoint_updated(
        project, issue_number, container_name, checkpoint
    )
```

**Checkpoint Interval**:
- Default: Every 5 agent calls
- Configurable via `checkpoint_interval` in pipeline config
- Balance between recovery granularity and Redis write overhead

#### Phase 3: Test Execution in Container

**Running Tests**:
```python
def run_tests_in_container(
    container_name: str,
    test_command: str,
    timeout: int
) -> Dict[str, Any]:
    """
    Execute test command inside running repair cycle container.
    """

    # Execute command in running container
    cmd = [
        'docker', 'exec',
        container_name,
        '/bin/bash', '-c',
        test_command
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    # Stream output
    output_lines = []
    for line in iter(process.stdout.readline, ''):
        if not line:
            break
        output_lines.append(line)

        # Stream to websocket for live viewing
        if stream_callback:
            stream_callback({
                'type': 'test_output',
                'line': line
            })

    # Wait for completion with timeout
    try:
        exit_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill process on timeout
        subprocess.run(['docker', 'exec', container_name, 'pkill', '-9', '-f', test_command])
        exit_code = -1
        output_lines.append("\n[TIMEOUT] Test execution exceeded timeout\n")

    # Parse test output for failures
    test_output = ''.join(output_lines)
    failures = parse_test_failures(test_output)
    warnings = parse_test_warnings(test_output)

    return {
        'success': exit_code == 0,
        'exit_code': exit_code,
        'output': test_output,
        'failures': failures,
        'warnings': warnings,
        'duration': time.time() - start_time
    }
```

**Test Command Examples**:
```bash
# Python/pytest
docker exec repair-context-studio-123 pytest tests/unit/ -v

# JavaScript/Jest
docker exec repair-myapp-456 npm test

# Go
docker exec repair-goapi-789 go test ./...

# Linting
docker exec repair-context-studio-123 flake8 src/
```

#### Phase 4: Agent Execution in Container

**Multiple Invocations**:
```python
def run_agent_in_repair_container(
    container_name: str,
    prompt: str,
    model: str
) -> str:
    """
    Run Claude CLI inside existing repair cycle container.

    This is called multiple times (once per file fix) within same container.
    """

    # Build Claude CLI command
    claude_cmd = [
        'claude',
        '--print',
        '--verbose',
        '--output-format', 'stream-json',
        '--model', model,
        '--permission-mode', 'bypassPermissions',
        prompt
    ]

    # Execute in container
    cmd = ['docker', 'exec', container_name] + claude_cmd

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)

    # Stream and collect output (same as agent container)
    result_parts = []
    for line in iter(process.stdout.readline, ''):
        event = json.loads(line)
        if stream_callback:
            stream_callback(event)
        if event.get('type') == 'assistant':
            # Extract text...
            result_parts.append(text)

    return ''.join(result_parts)
```

**Key Difference from Agent Container**:
- Agent containers: 1 container = 1 Claude invocation
- Repair cycle: 1 container = N Claude invocations (N ≈ 5-50)

**Why This Matters**:
- Container startup overhead eliminated
- File changes persist across invocations
- Test feedback loop faster

#### Phase 5: Container Cleanup

**On Successful Completion**:
```python
# All tests pass
subprocess.run(['docker', 'stop', container_name], timeout=30)
subprocess.run(['docker', 'rm', container_name])
redis.delete(f"repair_cycle:{project}:{issue_number}")

obs.emit_repair_cycle_container_completed(
    project, issue_number, container_name,
    success=True, agent_call_count, duration
)
```

**On Failure/Timeout**:
```python
# Max iterations reached or circuit breaker triggered
subprocess.run(['docker', 'stop', container_name], timeout=30)
subprocess.run(['docker', 'rm', container_name])
redis.delete(f"repair_cycle:{project}:{issue_number}")

obs.emit_repair_cycle_container_completed(
    project, issue_number, container_name,
    success=False, agent_call_count, duration
)
```

**On Orchestrator Restart (Container Left Running)**:
- Container continues running
- Checkpoint preserved in Redis
- Next repair cycle execution recovers and resumes

### Repair Cycle Recovery Scenario

**Scenario**: Orchestrator crashes during repair cycle

**State Before Crash**:
```
Container: repair-context-studio-123 [RUNNING]
Redis: repair_cycle:context-studio:123 = {
    iteration: 3,
    test_type: 'unit',
    agent_call_count: 12,
    files_fixed: ['file1.py', 'file2.py'],
    timestamp: '2025-10-23T10:30:00Z'
}
```

**On Orchestrator Restart**:
```python
# main.py startup
agent_container_recovery.recover_or_cleanup_repair_cycle_containers()
    ↓
# For each Redis key: repair_cycle:*
checkpoint = redis.get('repair_cycle:context-studio:123')
container_name = checkpoint['container_name']
    ↓
# Check if container still running
docker inspect repair-context-studio-123 → Running: true
    ↓
# Checkpoint less than 2 hours old → RECOVER
logger.info("Recovering repair cycle container: repair-context-studio-123")
obs.emit_repair_cycle_container_recovered(...)
    ↓
# Mark container as recovered (don't kill)
recovered_count += 1
```

**On Next Repair Cycle Task**:
```python
container_name, checkpoint = recover_or_create_repair_cycle_container(project, issue)
    ↓
# Checkpoint found, container running
if checkpoint:
    iteration = checkpoint['iteration']  # Resume at iteration 3
    agent_call_count = checkpoint['agent_call_count']  # Start at 12
    files_fixed = checkpoint['files_fixed']  # Already fixed file1.py, file2.py
    ↓
# Continue from where left off
for file in remaining_files:
    # Skip files_fixed
    if file in checkpoint['files_fixed']:
        continue
    # Fix remaining files...
```

---

## Critical Configuration Details

### File Permissions

**SSH Private Key**:
```bash
# On host
chmod 600 ~/.ssh/id_ed25519
chmod 644 ~/.ssh/id_ed25519.pub

# In orchestrator container
ls -la /home/orchestrator/.ssh/
-r-------- 1 orchestrator orchestrator 411 id_ed25519
-r--r--r-- 1 orchestrator orchestrator  99 id_ed25519.pub

# In agent container
ls -la /home/orchestrator/.ssh/
-r-------- 1 orchestrator orchestrator 411 id_ed25519  # Same inode (bind mount)
```

**Project Files**:
```bash
# Files created by agent (UID 1000)
touch /workspace/newfile.txt
ls -la /workspace/newfile.txt
-rw-r--r-- 1 orchestrator orchestrator 0 newfile.txt

# Same file on host (if host user is UID 1000)
ls -la project-name/newfile.txt
-rw-r--r-- 1 hostuser hostuser 0 newfile.txt
```

### Docker Socket Permissions

**On Host**:
```bash
ls -la /var/run/docker.sock
srw-rw---- 1 root docker 0 Oct 23 10:00 /var/run/docker.sock
```

**Requirements**:
- Host user must be in `docker` group
- Or socket must be world-writable (less secure)

**Add User to Docker Group**:
```bash
sudo usermod -aG docker $USER
newgrp docker  # Or logout/login
```

**Verify Access**:
```bash
# Inside orchestrator container
docker ps
# Should work without sudo
```

### Environment Variable Precedence

**Claude Authentication**:
```python
# Preference order:
1. CLAUDE_CODE_OAUTH_TOKEN (subscription)
2. ANTHROPIC_API_KEY (pay-per-use)
3. Neither → Error: "No authentication token"
```

**Git Author**:
```python
# Preference order:
1. Mounted .gitconfig (best)
2. GIT_AUTHOR_NAME + GIT_AUTHOR_EMAIL env vars (fallback)
3. Neither → Error: "Author identity unknown"
```

### Network Connectivity

**From Agent Container**:
```bash
# Can reach orchestrator services
curl http://redis:6379  # ✓
curl http://elasticsearch:9200  # ✓

# Can reach internet
curl https://api.anthropic.com  # ✓ (for Claude API)
curl https://api.github.com  # ✓ (for GitHub API)

# Cannot reach host
curl http://host.docker.internal:8080  # ✗ (isolated)
```

**DNS Resolution**:
- Containers use Docker's internal DNS
- Service names (redis, elasticsearch) resolve via Docker DNS
- External domains resolve via host DNS

---

## Observability Integration

### Container Lifecycle Events

**Event Emissions Throughout Lifecycle**:

```python
# Agent Container
obs.emit_agent_initialized(agent, task_id, project, config, branch_name, container_name)
    ↓ [container starting]
obs.emit_claude_call_started(agent, task_id, project, model)
    ↓ [streaming output]
stream_callback(event)  # Multiple times
    ↓ [container exiting]
obs.emit_claude_call_completed(agent, task_id, project, duration, tokens)
obs.emit_agent_completed(agent, task_id, project, duration, success, output)

# Repair Cycle Container
obs.emit_repair_cycle_container_started(project, issue, container_name, run_id)
    ↓ [multiple iterations]
obs.emit_repair_cycle_container_checkpoint_updated(project, issue, container, checkpoint)
    ↓ [recovery scenario]
obs.emit_repair_cycle_container_recovered(project, issue, container, checkpoint)
    ↓ [completion]
obs.emit_repair_cycle_container_completed(project, issue, container, success, calls, duration)
```

### Container Metadata in Events

**Data Structure**:
```python
{
    'timestamp': '2025-10-23T10:30:00Z',
    'event_type': 'agent_initialized',
    'agent': 'business_analyst',
    'task_id': 'task_business_analyst_1729680000',
    'project': 'context-studio',
    'data': {
        'branch_name': 'feature/issue-123',
        'container_name': 'claude-agent-context-studio-task_1729680000',
        'model': 'claude-sonnet-4-5-20250929',
        'timeout': 300,
        'requires_docker': True
    }
}
```

**Querying Container State**:
```bash
# Elasticsearch query
GET /agent-events-2025-10-23/_search
{
  "query": {
    "term": { "data.container_name": "claude-agent-context-studio-task_1729680000" }
  }
}
# Returns all events for this container
```

---

## Security Considerations

### Attack Surface

**Docker Socket Access = Root Access**:
- Orchestrator can spawn privileged containers
- Can mount any host directory
- Can access other containers
- Can modify Docker network/volumes

**Mitigation**:
- Run only trusted code in orchestrator
- Validate all inputs that construct Docker commands
- Use read-only mounts where possible
- Limit network exposure

### Secrets Management

**What's Exposed to Agent Containers**:
```python
EXPOSED:
✓ CLAUDE_CODE_OAUTH_TOKEN (necessary)
✓ ANTHROPIC_API_KEY (necessary)
✓ CONTEXT7_API_KEY (necessary for MCP)
✓ SSH private key (necessary for git)
✓ GitHub token (in .gitconfig or env)

NOT EXPOSED:
✗ REDIS_HOST/PORT (agent shouldn't access orchestrator Redis)
✗ ELASTICSEARCH_HOST/PORT (agent shouldn't access orchestrator ES)
✗ GITHUB_APP_PRIVATE_KEY (not needed in agent)
```

**Secrets Isolation**:
- Secrets only passed to containers that need them
- Read-only mounts prevent modification
- Container removal deletes secrets from memory

### Container Isolation

**What Agents CAN Do**:
- Modify files in `/workspace` (project directory)
- Clone/push to GitHub (via SSH keys)
- Call Claude API
- Call MCP servers

**What Agents CANNOT Do**:
- Access other projects' files
- Access orchestrator code
- Access host filesystem outside workspace
- Spawn additional containers
- Modify Docker configuration
- Access Redis/Elasticsearch directly

---

## Performance Optimization

### Image Layer Caching

**Dockerfile.agent Structure for Optimal Caching**:
```dockerfile
FROM python:3.11-slim

# Layer 1: System dependencies (changes rarely)
RUN apt-get update && apt-get install -y git curl && rm -rf /var/lib/apt/lists/*

# Layer 2: Claude CLI (changes rarely)
RUN curl -fsSL https://claude.ai/download/linux | sh

# Layer 3: User setup (changes rarely)
RUN useradd -m -u 1000 orchestrator

# Layer 4: Python dependencies (changes when requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Layer 5: Application code (changes frequently)
COPY . .

USER orchestrator
```

**Cache Hit Rate**:
- Layers 1-3: ~99% hit rate (almost never rebuild)
- Layer 4: ~80% hit rate (rebuild when dependencies change)
- Layer 5: ~0% hit rate (rebuild every time code changes)

### Container Reuse

**Agent Containers**: No reuse (--rm flag)
- Short-lived (minutes)
- Different prompts each time
- Reuse would require complex state management

**Repair Cycle Containers**: Yes, reuse across iterations
- Long-lived (hours)
- Same project, same issue
- State preserved via checkpoints
- Significant performance benefit (no startup overhead)

### Volume Mount Performance

**Bind Mounts vs Named Volumes**:
- Using bind mounts for all project directories
- Performance is excellent on Linux
- macOS Docker Desktop has slower I/O (known issue)
- Windows Docker Desktop requires WSL2 for good performance

**Optimization**:
```yaml
# Don't do this (slow on macOS/Windows):
- ./project:/workspace

# Do this (consistent everywhere):
- /absolute/path/to/project:/workspace
```

---

## Troubleshooting Guide

### Diagnostic Commands

**Check Orchestrator Container**:
```bash
docker ps | grep orchestrator
docker logs orchestrator --tail 100
docker exec orchestrator ls -la /workspace/
```

**Check Agent Container** (while running):
```bash
docker ps | grep claude-agent
docker logs claude-agent-context-studio-task_1729680000 --tail 50
docker exec claude-agent-context-studio-task_1729680000 ls -la /workspace/
```

**Check Repair Cycle Container**:
```bash
docker ps | grep repair-
docker logs repair-context-studio-123 --tail 100
docker exec repair-context-studio-123 pytest tests/unit/ -v
```

**Check Redis Tracking**:
```bash
docker exec orchestrator-redis redis-cli
> KEYS agent_container:*
> GET agent_container:claude-agent-context-studio-task_1729680000
> KEYS repair_cycle:*
> GET repair_cycle:context-studio:123
```

### Common Error Patterns

**Error**: `bind source path does not exist`
```bash
# Check path from orchestrator perspective
docker exec orchestrator ls -la /workspace/project-name/
```

**Error**: `permission denied while trying to connect to Docker daemon socket`
```bash
# Check socket permissions
ls -la /var/run/docker.sock
# Add user to docker group
sudo usermod -aG docker $USER
```

**Error**: `container name already in use`
```bash
# Remove existing container
docker rm -f claude-agent-context-studio-task_1729680000
```

**Error**: `no such file or directory: /home/orchestrator/.ssh/id_ed25519`
```bash
# Check mount in docker-compose.yml
volumes:
  - ~/.ssh/id_ed25519:/home/orchestrator/.ssh/id_ed25519:ro
# Verify file exists on host
ls -la ~/.ssh/id_ed25519
```

---

## Summary: Critical Containerization Details

### Must-Have Configuration

1. **User ID**: UID 1000 in all containers for file permission compatibility
2. **Docker Socket**: Mounted to orchestrator for Docker-in-Docker
3. **SSH Keys**: Mounted read-only with 600 permissions
4. **Git Config**: Mounted for commit author information
5. **Environment Variables**: CLAUDE_CODE_OAUTH_TOKEN, HOME=/home/orchestrator
6. **Network**: All containers in orchestrator_default network
7. **Volume Mounts**: Project dir, SSH keys, git config, MCP config

### Common Pitfalls

1. **Wrong UID**: Files created with wrong ownership (permission denied)
2. **Missing HOME**: SSH can't find keys (git authentication fails)
3. **No Docker Group**: Socket access denied (can't spawn containers)
4. **Missing .gitconfig**: Commits fail (author unknown)
5. **Invalid Container Name**: Special characters not sanitized (Docker rejects)
6. **Stale Containers**: Previous containers not cleaned up (name conflicts)
7. **Volume Path**: Relative vs absolute paths cause mount failures

### Recovery Capabilities

**Agent Containers**:
- Not recovered (short-lived, --rm flag)
- Tracked in Redis while running
- Orphans killed on restart

**Repair Cycle Containers**:
- Fully recoverable (checkpoint + running container)
- Resume from last iteration
- 2-hour staleness threshold

This document captures the comprehensive containerization architecture that makes the orchestrator's Docker-in-Docker execution secure, isolated, and reliable.
