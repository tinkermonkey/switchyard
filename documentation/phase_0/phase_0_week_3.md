# Phase 0 - Week 3: GitHub Integration

## Objective
Complete GitHub webhook integration and get the orchestrator running in Docker with working Kanban automation.

## Prerequisites from Week 2
- [x] End-to-end orchestration working locally
- [x] Task queue integration functional
- [x] State persistence and handoff working
- [x] Monitoring and logging integrated

## Current GitHub Integration State

###  **Already Implemented**
- **Comprehensive Webhook Server**: `services/webhook_server.py` with full event handling
- **Docker Webhook Service**: `Dockerfile.webhook` ready for deployment
- **Docker Compose**: Full orchestration setup with Redis, ngrok, and multiple services
- **Event Processing**: Handles issues, project cards, PRs, reviews, and pushes
- **Signature Verification**: GitHub webhook security implemented
- **Auto-discovery**: ngrok URL discovery and webhook auto-registration
- **Task Creation**: Events properly create agent tasks in Redis queue

### =' **Missing Components**
- **Main Orchestrator Dockerfile**: Docker-compose expects `build: .` but no Dockerfile exists
- **Integration Gap**: Webhook server creates tasks but orchestrator needs to consume them
- **Project Configuration**: Need real project setup instead of examples
- **GitHub CLI Authentication**: Need proper token setup in containers

---

## Day 1-2: Complete Docker Integration

### Task 1.1: Create Main Orchestrator Dockerfile
**Priority**: Critical
**Files**: `Dockerfile` (new file in root)

```dockerfile
# Main orchestrator Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y git curl redis-tools && \
    rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | \
    tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && \
    apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI (placeholder - update with actual method)
# RUN curl -sSL https://claude.ai/install.sh | bash

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create state directories
RUN mkdir -p orchestrator_data/state orchestrator_data/handoffs agents

# Set up Git (will be configured via environment)
RUN git config --global user.name "Orchestrator Bot" && \
    git config --global user.email "orchestrator@example.com"

# Default command
CMD ["python", "main.py"]
```

### Task 1.2: Create Requirements File
**Priority**: Critical
**Files**: `requirements.txt` (new file)

```txt
# Core dependencies
asyncio-throttle>=1.0.2
aiofiles>=23.2.1
redis>=5.0.1
pydantic>=2.4.2
python-dotenv>=1.0.0

# Monitoring
prometheus-client>=0.17.1
python-json-logger>=2.0.7
psutil>=5.9.6

# Web/API
flask>=3.0.0
requests>=2.31.0

# YAML processing
pyyaml>=6.0.1

# Date/time handling
python-dateutil>=2.8.2
```

### Task 1.3: Fix Docker Compose Integration
**Priority**: High
**Files**: `docker-compose.yml`

```yaml
# MODIFY existing orchestrator service:
orchestrator:
  build: .
  volumes:
    # Mount code for development
    - ./:/app

    # Mount projects (configure these for your actual projects)
    - ~/workspace/project1:/projects/project1
    - ~/workspace/project2:/projects/project2

    # Mount git config and SSH keys
    - ~/.ssh:/root/.ssh:ro
    - ~/.gitconfig:/root/.gitconfig:ro

    # Mount Docker socket if needed
    - /var/run/docker.sock:/var/run/docker.sock

  environment:
    - GITHUB_TOKEN=${GITHUB_TOKEN}
    - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
    - REDIS_URL=redis://redis:6379
    - WEBHOOK_SECRET=${GITHUB_WEBHOOK_SECRET}

  depends_on:
    - redis
    - webhook

  networks:
    - orchestrator-net

  working_dir: /app
  command: ["python", "main.py"]

# ADD missing network definition:
networks:
  orchestrator-net:
    driver: bridge
```

---

## Day 3-4: Connect Webhook to Orchestrator

### Task 2.1: Fix Task Queue Integration
**Priority**: Critical
**Files**: `services/webhook_server.py`

**Problem**: Webhook creates tasks but uses different task format than orchestrator expects

```python
# MODIFY the create_agent_task method in WebhookProcessor:

def create_agent_task(self, agent, project, priority, context):
    """Create a task compatible with orchestrator's TaskQueue"""
    from task_queue.task_manager import Task, TaskPriority
    from datetime import datetime

    # Map string priority to TaskPriority enum
    priority_mapping = {
        'high': TaskPriority.HIGH,
        'medium': TaskPriority.MEDIUM,
        'low': TaskPriority.LOW
    }

    task = Task(
        id=f"{agent}_{project}_{datetime.now().timestamp()}",
        agent=agent,
        project=project,
        priority=priority_mapping.get(priority, TaskPriority.MEDIUM),
        context=context,
        created_at=datetime.now().isoformat(),
        status="pending"
    )

    # Use orchestrator's TaskQueue format
    from task_queue.task_manager import TaskQueue
    task_queue = TaskQueue()  # This uses Redis
    task_queue.enqueue(task)

    print(f" Created task {task.id} for {agent} via orchestrator TaskQueue")
```

### Task 2.2: Test Webhook � Orchestrator Flow
**Priority**: Critical
**Action**: Create integration test

```python
# Create: scripts/test_webhook_integration.py
import requests
import json
import time
import asyncio
from datetime import datetime

def test_webhook_to_orchestrator():
    """Test complete webhook � orchestrator flow"""

    print(">� Testing webhook integration...")

    # 1. Send test webhook
    webhook_payload = {
        "action": "opened",
        "issue": {
            "number": 123,
            "title": "Test Integration Issue",
            "body": "Testing webhook integration with orchestrator",
            "html_url": "https://github.com/test/repo/issues/123"
        },
        "repository": {
            "name": "test-repo"
        }
    }

    # Send to webhook server
    response = requests.post(
        'http://localhost:3000/github-webhook',
        json=webhook_payload,
        headers={
            'X-GitHub-Event': 'issues',
            'X-Hub-Signature-256': 'sha256=test'  # Will need real signature
        }
    )

    assert response.status_code == 200
    print(" Webhook received and queued")

    # 2. Check task was created in Redis
    import redis
    redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)

    # Wait a moment for processing
    time.sleep(2)

    # Check for tasks in queue
    task_ids = redis_client.lrange("tasks:medium", 0, -1)
    assert len(task_ids) > 0, "No tasks found in queue"

    print(f" Found {len(task_ids)} tasks in queue")

    # 3. Verify task structure
    task_data = redis_client.hgetall(f"task:{task_ids[0]}")
    assert task_data['agent'] == 'business_analyst'
    assert task_data['project'] == 'test-repo'
    assert 'issue_number' in task_data['context']

    print(" Task structure validated")

    # 4. Check orchestrator can process it
    # (This would require orchestrator running)

    print("<� Webhook integration test PASSED!")

if __name__ == "__main__":
    test_webhook_to_orchestrator()
```

### Task 2.3: Setup Project Configuration
**Priority**: High
**Files**: `config/projects.yaml`

```yaml
# Replace example with real project configuration
projects:
  clauditoreum:
    repo_url: git@github.com:example_user/clauditoreum.git
    local_path: /projects/clauditoreum
    branch: main
    kanban_board_id: YOUR_PROJECT_BOARD_ID
    kanban_columns:
      "Backlog": null
      "Requirements Analysis": "business_analyst"
      "Design": "software_architect"
      "Ready for Development": null
      "In Development": "senior_software_engineer"
      "Code Review": "code_reviewer"
      "Testing": "senior_qa_engineer"
      "Done": null
    tech_stacks:
      backend: python
      framework: flask

  # Add your actual projects here
  project-example:
    repo_url: git@github.com:yourusername/project.git
    local_path: /projects/project-example
    branch: main
    kanban_board_id: 123
    kanban_columns:
      "To Do": null
      "In Progress": "senior_software_engineer"
      "Review": "code_reviewer"
      "Done": null
```

---

## Day 5-6: GitHub Kanban Integration

### Task 3.1: Setup Test GitHub Project
**Priority**: High
**Action**: Create GitHub project with Kanban board

```bash
# Manual setup steps:

# 1. Create a test repository or use existing one
gh repo create orchestrator-test --public

# 2. Create project board
gh project create --title "SDLC Orchestrator Test" --body "Testing orchestrator integration"

# 3. Add columns to the board
gh project field-create PROJECT_ID --name "Status" --single-select-options "Backlog,Requirements Analysis,Design,Ready for Development,In Development,Code Review,Testing,Done"

# 4. Create test issue
gh issue create --title "Test Orchestrator Integration" --body "This issue tests the orchestrator GitHub integration" --repo example_user/orchestrator-test

# 5. Add issue to project board
gh project item-add PROJECT_ID --url https://github.com/example_user/orchestrator-test/issues/1
```

### Task 3.2: Test Card Movement Triggers
**Priority**: High
**Action**: Verify card movements trigger orchestrator tasks

```python
# Create: scripts/test_kanban_automation.py
import requests
import subprocess
import time

def test_kanban_automation():
    """Test that moving cards triggers orchestrator tasks"""

    print("<� Testing Kanban automation...")

    # 1. Move card to "Requirements Analysis" column
    # (This would normally be done through GitHub UI)
    # For testing, we'll simulate the webhook

    card_moved_payload = {
        "action": "moved",
        "project_card": {
            "id": 123,
            "content_url": "https://api.github.com/repos/example_user/orchestrator-test/issues/1",
            "column_id": 456
        },
        "repository": {
            "name": "orchestrator-test"
        },
        "changes": {
            "column_id": {
                "from": 789  # Previous column
            }
        }
    }

    # Send webhook
    response = requests.post(
        'http://localhost:3000/github-webhook',
        json=card_moved_payload,
        headers={
            'X-GitHub-Event': 'project_card',
            'X-Hub-Signature-256': 'sha256=test'
        }
    )

    assert response.status_code == 200
    print(" Card movement webhook received")

    # 2. Verify Business Analyst task created
    # Check queue status endpoint
    status_response = requests.get('http://localhost:3000/queue-status')
    status_data = status_response.json()

    assert status_data['pending_tasks'] > 0
    print(f" {status_data['pending_tasks']} tasks created from card movement")

    # 3. Verify task has correct context
    tasks = status_data['tasks']
    card_task = next(t for t in tasks if t['context'].get('action') == 'process_card_move')
    assert card_task['agent'] == 'business_analyst'

    print("<� Kanban automation test PASSED!")

if __name__ == "__main__":
    test_kanban_automation()
```

### Task 3.3: Status Updates Back to GitHub
**Priority**: Medium
**Action**: Make orchestrator update GitHub when tasks complete

```python
# Add to agents/01_business_analyst.py after task completion:

async def update_github_status(self, context):
    """Update GitHub issue/project when task completes"""

    if 'issue_number' in context.get('context', {}):
        issue_number = context['context']['issue_number']
        project = context.get('project', '')

        # Add comment to issue
        comment = f"""
> **Business Analysis Complete**

Requirements analysis has been completed by the orchestrator.

**Summary:**
- {len(context.get('requirements_analysis', {}).get('user_stories', []))} user stories generated
- Quality score: {context.get('quality_metrics', {}).get('completeness_score', 0):.1%}

**Next Steps:**
Moving to design phase...
        """

        import subprocess
        subprocess.run([
            'gh', 'issue', 'comment', str(issue_number),
            '--body', comment,
            '--repo', f"example_user/{project}"
        ])

        print(f" Updated GitHub issue #{issue_number}")
```

---

## Day 7: Docker Deployment Testing

### Task 4.1: Environment Configuration
**Priority**: Critical
**Files**: `.env.example` and documentation

```bash
# Create .env.example for team setup:

# GitHub Integration
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
GITHUB_WEBHOOK_SECRET=your_webhook_secret_here

# Claude/Anthropic
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxx

# ngrok (for webhook testing)
NGROK_AUTHTOKEN=your_ngrok_token_here

# Redis (usually default is fine)
REDIS_URL=redis://redis:6379

# Logging levels
LOG_LEVEL=INFO
```

### Task 4.2: Complete Docker Deployment
**Priority**: Critical
**Action**: Test full system in Docker

```bash
# Test complete Docker deployment:

# 1. Build and start all services
docker-compose up --build

# Expected services:
# - redis: Task queue storage
# - webhook: GitHub webhook receiver
# - orchestrator: Main orchestration loop
# - ngrok: External tunnel for webhooks

# 2. Check all services are healthy
docker-compose ps

# 3. Check webhook server is accessible
curl http://localhost:3000/health

# 4. Check orchestrator logs
docker-compose logs orchestrator

# 5. Verify ngrok tunnel is working
curl http://localhost:4040/api/tunnels
```

### Task 4.3: End-to-End Docker Test
**Priority**: Critical
**Action**: Complete flow test in Docker environment

```python
# Create: scripts/test_docker_deployment.py
import requests
import time
import subprocess

def test_docker_deployment():
    """Test complete system running in Docker"""

    print("=3 Testing Docker deployment...")

    # 1. Verify all services are up
    result = subprocess.run(['docker-compose', 'ps'], capture_output=True, text=True)
    assert 'Up' in result.stdout, "Some services are not running"
    print(" All Docker services running")

    # 2. Test webhook health
    health_response = requests.get('http://localhost:3000/health')
    assert health_response.status_code == 200
    print(" Webhook server healthy")

    # 3. Test ngrok tunnel
    tunnel_response = requests.get('http://localhost:4040/api/tunnels')
    tunnels = tunnel_response.json()['tunnels']
    assert len(tunnels) > 0, "No ngrok tunnels active"
    print(f" ngrok tunnel active: {tunnels[0]['public_url']}")

    # 4. Send test webhook
    test_payload = {
        "action": "opened",
        "issue": {"number": 999, "title": "Docker test", "body": "Testing"},
        "repository": {"name": "docker-test"}
    }

    webhook_response = requests.post(
        'http://localhost:3000/github-webhook',
        json=test_payload,
        headers={'X-GitHub-Event': 'issues'}
    )
    assert webhook_response.status_code == 200
    print(" Webhook processed in Docker")

    # 5. Check orchestrator logs for task processing
    time.sleep(5)  # Wait for processing

    logs_result = subprocess.run(
        ['docker-compose', 'logs', '--tail=20', 'orchestrator'],
        capture_output=True, text=True
    )
    assert 'business_analyst' in logs_result.stdout, "Orchestrator not processing tasks"
    print(" Orchestrator processing tasks in Docker")

    print("<� Docker deployment test PASSED!")

if __name__ == "__main__":
    test_docker_deployment()
```

---

## Success Criteria

### Must Complete
- [ ] Main Dockerfile created and orchestrator runs in Docker
- [ ] Webhook server integrates with orchestrator TaskQueue
- [ ] GitHub project board setup with proper column mapping
- [ ] Card movement triggers create tasks in orchestrator
- [ ] Complete Docker deployment working (all services up)
- [ ] End-to-end test: GitHub webhook � task creation � orchestrator processing

### Should Complete
- [ ] GitHub status updates when tasks complete
- [ ] ngrok tunnel working for webhook testing
- [ ] Proper error handling in webhook server
- [ ] Project configuration for real repositories

### Nice to Have
- [ ] Automated GitHub project setup scripts
- [ ] Webhook signature verification working
- [ ] Advanced GitHub API integration (branch creation, PR management)

## File Changes Required

### New Files
- `Dockerfile` - Main orchestrator container
- `requirements.txt` - Python dependencies
- `.env.example` - Environment template
- `scripts/test_webhook_integration.py`
- `scripts/test_kanban_automation.py`
- `scripts/test_docker_deployment.py`

### Modified Files
- `services/webhook_server.py` - Fix TaskQueue integration
- `config/projects.yaml` - Real project configuration
- `docker-compose.yml` - Add missing network configuration
- `agents/01_business_analyst.py` - Add GitHub status updates

## Environment Setup

```bash
# 1. Copy environment template
cp .env.example .env

# 2. Fill in your actual tokens:
# - GitHub personal access token
# - Anthropic API key
# - ngrok auth token
# - Webhook secret

# 3. Configure projects.yaml with your repositories

# 4. Setup GitHub project boards with proper columns

# 5. Install Claude Code CLI in Docker container
# (Update Dockerfile once installation method is confirmed)
```

## Week 3 Success Target

**Complete GitHub Integration**: GitHub webhook events automatically trigger orchestrator tasks which execute through the Business Analyst agent and update GitHub status - all running in Docker containers with proper service orchestration.

This establishes the external integration needed to make the orchestrator truly autonomous and reactive to real development workflows.