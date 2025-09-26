# Phase 0 - Week 1: Foundation Integration

## Objective
Connect existing orchestrator components to create a working foundation with one functional agent.

## Critical Issues Identified

### Import Path Problems
- `pipeline/orchestrator.py`: Relative import `from ..pipeline.base` won't work
- `handoff/protocol.py`: Wrong path `from ..state.manager` should be `state_management.manager`
- `pipeline/resilient_pipeline.py`: Relative imports for resilience modules
- Several other relative import issues throughout codebase

### Missing Functions
- `run_claude_code()` referenced in `agents/agent_stages.py` but not implemented
- `start_webhook_server()` referenced in `main.py` but not implemented
- Disconnect between `main.py` orchestration loop and actual pipeline execution

### Configuration Gaps
- `config/pipelines.yaml` is empty
- No agent-specific configuration files in `agents/`
- Missing environment configuration for Claude Code SDK

---

## Day 1-2: Fix Import Dependencies

### Task 1.1: Fix Pipeline Import Issues
**Priority**: Critical
**Files**: `pipeline/orchestrator.py`

```python
# CHANGE FROM:
from ..pipeline.base import PipelineStage

# CHANGE TO:
from pipeline.base import PipelineStage
```

### Task 1.2: Fix Handoff Import Issues
**Priority**: Critical
**Files**: `handoff/protocol.py`

```python
# CHANGE FROM:
from ..state.manager import StateManager

# CHANGE TO:
from state_management.manager import StateManager
```

### Task 1.3: Fix Resilient Pipeline Imports
**Priority**: Critical
**Files**: `pipeline/resilient_pipeline.py`

```python
# CHANGE FROM:
from ..resilience.circuit_breaker import CircuitBreaker
from ..resilience.retry_manager import RetryManager

# CHANGE TO:
from resilience.circuit_breaker import CircuitBreaker
from resilience.retry_manager import RetryManager
```

### Task 1.4: Verify All Import Paths
**Action**: Run basic import test to ensure all modules load correctly

```bash
cd /Users/austinsand/workspace/orchestrator/clauditoreum
python -c "
from pipeline.orchestrator import SequentialPipeline
from handoff.protocol import HandoffManager
from state_management.manager import StateManager
print(' All imports working')
"
```

---

## Day 3-4: Implement Missing Functions

### Task 2.1: Implement `run_claude_code()` Function
**Priority**: Critical
**Files**: `agents/agent_stages.py` or new `claude/claude_integration.py`

```python
import subprocess
from typing import Dict, Any
from pathlib import Path

async def run_claude_code(prompt: str, context: Dict[str, Any]) -> str:
    """Execute Claude Code with given prompt and context"""

    # Prepare working directory
    work_dir = Path(context.get('work_dir', '.'))

    # Prepare context file if needed
    context_info = f"""
    Project: {context.get('project', 'unknown')}
    Task: {context.get('task_description', '')}
    Files: {context.get('files', [])}
    """

    # Execute Claude Code
    cmd = [
        'claude',
        '-p', prompt,
        '--output-format', 'json',
        '--no-interactive'
    ]

    try:
        result = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

        if result.returncode == 0:
            return result.stdout.strip()
        else:
            raise Exception(f"Claude Code failed: {result.stderr}")

    except subprocess.TimeoutExpired:
        raise Exception("Claude Code execution timed out")
```

### Task 2.2: Implement `start_webhook_server()` Function
**Priority**: High
**Files**: `services/webhook_server.py` or new file

```python
def start_webhook_server(port: int):
    """Start Flask webhook server in current thread"""
    from flask import Flask, request, jsonify
    from task_queue.task_manager import TaskQueue

    app = Flask(__name__)
    task_queue = TaskQueue()

    @app.route('/webhook', methods=['POST'])
    def handle_webhook():
        payload = request.json

        # Create task from webhook
        if payload.get('action') == 'moved':
            task = Task(
                id=f"webhook_{payload['project']['id']}_{datetime.now().timestamp()}",
                agent='business_analyst',
                project=payload['project']['name'],
                priority=TaskPriority.MEDIUM,
                context={
                    'issue': payload.get('issue', {}),
                    'webhook_payload': payload
                },
                created_at=datetime.now().isoformat()
            )
            task_queue.enqueue(task)
            return jsonify({'status': 'queued'})

        return jsonify({'status': 'ignored'})

    app.run(host='0.0.0.0', port=port, debug=False)
```

### Task 2.3: Connect Process Task to Pipeline
**Priority**: Critical
**Files**: `main.py`

```python
# MODIFY the existing process_task call in main.py:
async def process_task_wrapper(task):
    """Process task using the sequential pipeline"""
    from agents.agent_stages import create_sdlc_pipeline

    pipeline = create_sdlc_pipeline(state_manager)

    try:
        result = await pipeline.execute({
            'task_id': task.id,
            'agent': task.agent,
            'project': task.project,
            'context': task.context,
            'work_dir': f"./projects/{task.project}"
        })
        return result
    except Exception as e:
        logger.log_error(f"Pipeline execution failed: {e}")
        raise

# Replace the process_task call in the main loop
result = await process_task_wrapper(task)
```

---

## Day 5-6: Configuration & First Agent

### Task 3.1: Create Basic Pipeline Configuration
**Priority**: High
**Files**: `config/pipelines.yaml`

```yaml
# Basic single-agent pipeline for testing
pipelines:
  business_analyst_only:
    name: "Business Analyst Pipeline"
    description: "Single agent pipeline for requirements analysis"
    agents:
      - name: business_analyst
        timeout: 300
        retries: 3
        circuit_breaker:
          failure_threshold: 3
          recovery_timeout: 180

  default: business_analyst_only

agent_configs:
  business_analyst:
    claude_model: "claude-3-5-sonnet-20241022"
    working_directory: "./projects/{project_name}"
    output_format: "structured_json"
    tools_enabled:
      - file_operations
      - git_integration
      - web_search
```

### Task 3.2: Create Agent Configuration Directory
**Priority**: High
**Files**: `claude/agents/business_analyst.md`

```markdown
# Business Analyst Agent

You are a Business Analyst Agent specializing in requirements gathering and user story creation.

## Core Expertise
- CBAP certification-level requirements analysis
- INVEST principles for user story creation
- Given-When-Then format for acceptance criteria
- Stakeholder communication and process documentation

## Task Focus
Analyze requirements from issues/tickets and create structured outputs:
- Business Requirements Documents
- User stories with acceptance criteria
- Process flow descriptions
- Stakeholder impact assessments

## Output Format
Always return structured JSON with:
```json
{
  "requirements_analysis": {
    "summary": "Brief summary of requirements",
    "functional_requirements": ["req1", "req2"],
    "non_functional_requirements": ["nfr1", "nfr2"],
    "user_stories": [
      {
        "title": "Story title",
        "description": "As a [user] I want [goal] so that [benefit]",
        "acceptance_criteria": ["Given...", "When...", "Then..."],
        "priority": "High|Medium|Low"
      }
    ],
    "risks": ["risk1", "risk2"],
    "assumptions": ["assumption1", "assumption2"]
  },
  "quality_metrics": {
    "completeness_score": 0.85,
    "clarity_score": 0.90,
    "testability_score": 0.80
  }
}
```

## Constraints
- Analysis timeout: 5 minutes maximum
- Focus on clarity and completeness over speed
- Always validate requirements against SMART criteria

### Task 3.3: Implement Real Business Analyst Agent
**Priority**: High
**Files**: `agents/01_business_analyst.py`

```python
from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
import json

class BusinessAnalystAgent(PipelineStage):
    def __init__(self):
        super().__init__("business_analyst")

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute business analysis on the given issue/requirements"""

        issue = context.get('context', {}).get('issue', {})
        project = context.get('project', 'unknown')

        prompt = f"""
        Analyze the following issue/requirement for project {project}:

        Title: {issue.get('title', 'No title')}
        Description: {issue.get('body', 'No description')}
        Labels: {issue.get('labels', [])}

        Provide a comprehensive business analysis following the format specified in your configuration.
        Focus on extracting clear functional requirements and creating actionable user stories.
        """

        try:
            result = await run_claude_code(prompt, context)

            # Parse Claude's response
            analysis = json.loads(result)

            # Add to context for next stage
            context['requirements_analysis'] = analysis.get('requirements_analysis', {})
            context['quality_metrics'] = analysis.get('quality_metrics', {})
            context['completed_work'] = context.get('completed_work', []) + [
                "Business requirements analysis completed",
                f"Generated {len(analysis.get('requirements_analysis', {}).get('user_stories', []))} user stories"
            ]

            return context

        except Exception as e:
            raise Exception(f"Business analysis failed: {str(e)}")
```

---

## Day 7: Integration Testing

### Task 4.1: End-to-End Test Setup
**Priority**: Critical

Create test script `tests/integration/test_basic_orchestration.py`:

```python
import asyncio
from task_queue.task_manager import TaskQueue, Task, TaskPriority
from datetime import datetime

async def test_basic_orchestration():
    """Test basic orchestration with Business Analyst"""

    task_queue = TaskQueue()

    # Create test task
    test_task = Task(
        id="test_ba_001",
        agent="business_analyst",
        project="test_project",
        priority=TaskPriority.HIGH,
        context={
            "issue": {
                "title": "User Login Feature",
                "body": "As a user, I need to be able to log into the system",
                "labels": ["feature", "authentication"]
            }
        },
        created_at=datetime.now().isoformat()
    )

    # Enqueue task
    task_queue.enqueue(test_task)
    print(" Task queued successfully")

    # Test dequeue
    retrieved_task = task_queue.dequeue()
    assert retrieved_task.id == test_task.id
    print(" Task dequeue working")

    print("Integration test passed!")

if __name__ == "__main__":
    asyncio.run(test_basic_orchestration())
```

### Task 4.2: Smoke Test Components
**Priority**: High

```bash
# Test imports
python -c "from pipeline.orchestrator import SequentialPipeline; print(' Pipeline imports')"

# Test configuration loading
python -c "
import yaml
with open('config/pipelines.yaml') as f:
    config = yaml.safe_load(f)
print(' Pipeline config loaded')
"

# Test agent creation
python -c "
from agents.agent_01_business_analyst import BusinessAnalystAgent
agent = BusinessAnalystAgent()
print(' Agent instantiation working')
"
```

### Task 4.3: Manual End-to-End Test
**Priority**: Critical

1. Start Redis: `redis-server`
2. Start orchestrator: `python main.py`
3. Send test webhook or manually enqueue task
4. Verify task executes through pipeline
5. Check state files in `orchestrator_data/state/`
6. Verify handoff package creation

---

## Success Criteria

### Must Complete
- [ ] All import errors resolved - no Python import failures
- [ ] `run_claude_code()` function implemented and working
- [ ] Basic pipeline configuration created
- [ ] Business Analyst agent implemented with real Claude Code integration
- [ ] Task can be enqueued, dequeued, and processed through pipeline
- [ ] State management working (checkpoint creation/recovery)

### Should Complete
- [ ] Webhook server functional (basic)
- [ ] Agent configuration file created
- [ ] Integration test passes
- [ ] Basic error handling working

### Nice to Have
- [ ] Comprehensive error handling
- [ ] Detailed logging throughout process
- [ ] Health monitoring functional

## Dependencies & Prerequisites

### Environment Setup
```bash
# Ensure Claude Code CLI is installed and authenticated
claude --version

# Redis server running
redis-server --daemonize yes

# Python dependencies installed
pip install -r requirements.txt
```

### File Structure After Week 1
```
   claude/
      agents/
         business_analyst.md
      state/                    # State files created
   config/
      pipelines.yaml           # Populated
   agents/
      01_business_analyst.py   # Real implementation
      agent_stages.py          # Updated with run_claude_code
   claude/
      claude_integration.py    # New file with run_claude_code
   tests/
       integration/
         test_basic_orchestration.py # Test script
```

This plan addresses the critical foundation issues while building toward a working single-agent orchestrator. Each task is specific and actionable with clear acceptance criteria.