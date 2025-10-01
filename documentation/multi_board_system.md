# Multi-Board Pipeline System

The Claude Code Orchestrator now supports multiple Kanban boards per project, each corresponding to different development pipelines. This allows for granular workflow management based on the complexity and type of work.

## Overview

Each project gets **3 specialized boards**:

### 1. Idea Development Pipeline (`idea_development_pipeline`)
- **Purpose**: Early-stage research and requirements validation
- **Stages**: Research → Analysis → Review → Done
- **Agents**: `idea_researcher` → `business_analyst` → `requirements_reviewer`
- **Use Cases**: New feature concepts, research tasks, requirement validation

### 2. Development Pipeline (`dev_pipeline`)
- **Purpose**: Streamlined feature development
- **Stages**: Requirements → Design → Implementation → Code Review → Done
- **Agents**: `business_analyst` → `software_architect` → `senior_software_engineer` → `code_reviewer`
- **Use Cases**: Well-defined features, bug fixes, enhancements

### 3. Full SDLC Pipeline (`full_sdlc_pipeline`)
- **Purpose**: Complete development lifecycle with maker-checker reviews
- **Stages**: Research → Requirements → Requirements Review → Design → Design Review → Test Planning → Test Plan Review → Implementation → Code Review → QA Testing → Documentation → Documentation Review → Done
- **Agents**: All 13 agents with full maker-checker patterns
- **Use Cases**: Complex features, major initiatives, enterprise projects

## Setup

### 1. Create Multi-Board Setup
```bash
# Dry run to see what will be created (can be run from any directory)
python scripts/setup_multi_boards.py your-project --dry-run

# Actually create the boards and labels
export GITHUB_ORG=your-org
python scripts/setup_multi_boards.py your-project
```

**Note**: All scripts automatically handle Python path injection and working directory changes, so no `PYTHONPATH=.` prefix is needed.

This creates:
- 3 project boards in GitHub with proper column configurations
- Pipeline labels (`pipeline:idea-dev`, `pipeline:dev`, `pipeline:full-sdlc`)
- Stage labels (`stage:research`, `stage:design`, etc.)

### 2. Configure Webhooks
The webhook system automatically monitors all boards and routes issues based on labels.

## Usage Workflow

### 1. Issue Creation and Routing

**For Idea Development:**
```bash
gh issue create --title "Research market demand for AI features" \
  --body "Need to understand user requirements for AI integration" \
  --label "pipeline:idea-dev,stage:research"
```

**For Development:**
```bash
gh issue create --title "Implement user authentication" \
  --body "Add OAuth integration for user login" \
  --label "pipeline:dev,stage:requirements"
```

**For Full SDLC:**
```bash
gh issue create --title "Build enterprise dashboard" \
  --body "Complex multi-tenant dashboard with analytics" \
  --label "pipeline:full-sdlc,stage:research"
```

### 2. Automatic Agent Routing

The system automatically routes work based on labels:

- **Issue created** → Webhook detects pipeline label → Routes to first agent
- **Label changed** → Webhook detects stage change → Routes to appropriate agent
- **Board movement** → Agents update status and trigger next stage

### 3. Pipeline Progression

#### Idea Development Flow:
1. `idea_researcher` performs initial research
2. `business_analyst` analyzes requirements
3. `requirements_reviewer` validates and approves
4. Output ready for Development pipeline

#### Development Flow:
1. `business_analyst` refines requirements
2. `software_architect` creates design
3. `senior_software_engineer` implements
4. `code_reviewer` reviews and approves

#### Full SDLC Flow:
All 13 agents with full maker-checker patterns and quality gates.

## Label System

### Pipeline Labels (mutually exclusive):
- `pipeline:idea-dev` - Routes to Idea Development board
- `pipeline:dev` - Routes to Development board
- `pipeline:full-sdlc` - Routes to Full SDLC board

### Stage Labels (pipeline-specific):
- `stage:research`, `stage:analysis`, `stage:review` (idea-dev)
- `stage:requirements`, `stage:design`, `stage:implementation`, `stage:code-review` (dev)
- All stages available for full-sdlc pipeline

## Board Management

### View Boards
```bash
# List all project boards
gh project list --owner your-org

# View specific board
gh project view PROJECT_NUMBER
```

### Move Issues Between Stages
```bash
# Move issue to next stage (triggers agent)
gh issue edit ISSUE_NUMBER --add-label "stage:design" --remove-label "stage:requirements"
```

### Monitor Progress
```bash
# Check webhook and task status
curl http://localhost:3000/queue-status
```

## Architecture

### Components

1. **GitHub Project Manager** (`services/github_project_manager.py`)
   - Creates boards with GraphQL API
   - Configures status fields and columns
   - Manages labels for routing

2. **Webhook System** (`services/webhook_server.py`)
   - Monitors issue events and label changes
   - Routes work based on pipeline/stage labels
   - Creates tasks for appropriate agents

3. **Pipeline Factory** (`pipeline/factory.py`)
   - Creates agent pipelines based on configuration
   - Supports maker-checker workflow patterns

4. **Kanban Templates** (`config/kanban_templates.yaml`)
   - Defines board structures for each pipeline
   - Maps stages to agents
   - Configures labels and colors

### Integration Points

- **Issue Labels** → **Pipeline Routing** → **Agent Assignment**
- **Board Movements** → **Webhook Events** → **Task Creation**
- **Agent Completion** → **Status Updates** → **Next Stage**

## Monitoring and Debugging

### Webhook Status
```bash
curl http://localhost:3000/health
curl http://localhost:3000/queue-status
```

### Task Queue Status
```bash
# View Redis task queues
redis-cli LRANGE tasks:high 0 10
redis-cli LRANGE tasks:medium 0 10
```

### Error Logs
```bash
# View webhook errors
redis-cli LRANGE webhook_errors 0 10
```

## Example Workflows

### Scenario 1: New Feature Idea
1. Create issue with `pipeline:idea-dev,stage:research`
2. `idea_researcher` analyzes market fit
3. `business_analyst` defines requirements
4. `requirements_reviewer` validates
5. Graduate to `pipeline:dev` for implementation

### Scenario 2: Bug Fix
1. Create issue with `pipeline:dev,stage:requirements`
2. `business_analyst` analyzes impact
3. `software_architect` designs fix
4. `senior_software_engineer` implements
5. `code_reviewer` approves

### Scenario 3: Enterprise Feature
1. Create issue with `pipeline:full-sdlc,stage:research`
2. Full 13-agent workflow with reviews
3. Complete documentation and testing
4. Production-ready output

## Best Practices

1. **Label Consistently**: Always include both pipeline and stage labels
2. **Use Appropriate Pipeline**: Match complexity to pipeline choice
3. **Monitor Progress**: Use board views to track status
4. **Review Quality Gates**: Check agent outputs meet quality thresholds
5. **Graduate Issues**: Move from idea-dev → dev → full-sdlc as needed

This multi-board system provides the flexibility to handle different types of work with appropriate levels of process and review, while maintaining full automation and agent orchestration.