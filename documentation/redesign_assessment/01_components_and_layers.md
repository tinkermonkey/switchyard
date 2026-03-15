# Components and Layers - Complete Inventory

This document provides a comprehensive inventory of all functional components and layers in the Claude Code Agent Orchestrator system.

## System Architecture Overview

The orchestrator is a **multi-layered autonomous AI development system** that manages GitHub-integrated software development workflows. It coordinates specialized AI agents through GitHub Projects v2 Kanban boards, executing complete SDLC pipelines from requirements analysis to deployment.

## Layer 1: Core Orchestration Layer

### 1.1 Main Orchestrator (`main.py`)
**Location**: `/switchyard/main.py`

**Responsibilities**:
- System initialization and startup
- Zombie process reaper (SIGCHLD signal handling)
- Component lifecycle management
- Main event loop coordination
- Health monitoring
- Task queue processing
- Cleanup operations on startup

**Key Operations**:
- Initializes all subsystems (Redis, Elasticsearch, GitHub, Docker)
- Recovers or cleans up orphaned containers on restart
- Reconciles GitHub project boards
- Queues dev environment setup tasks
- Processes tasks from queue via `process_task_integrated()`
- Periodic health checks with exponential backoff

### 1.2 Agent Orchestrator Integration (`agents/orchestrator_integration.py`)
**Location**: `/switchyard/agents/orchestrator_integration.py`

**Responsibilities**:
- Bridge between task queue and agent execution
- Task validation (dev container requirements)
- Pipeline creation and management
- Auto-advancement logic

**Key Functions**:
- `process_task_integrated()` - Main entry point for task execution
- `validate_task_can_run()` - Validates agent requirements
- `queue_dev_environment_setup()` - Auto-queues setup tasks
- `create_stage_from_config()` - Factory for pipeline stages
- `create_agent_pipeline()` - Pipeline construction

---

## Layer 2: Agent System

### 2.1 Base Agent Classes

#### 2.1.1 PipelineStage (`pipeline/base.py`)
**Abstract base class** for all pipeline stages.

**Properties**:
- `name`: Stage identifier
- `circuit_breaker`: Fault tolerance mechanism
- `agent_config`: Agent configuration reference

**Methods**:
- `execute(context)`: Abstract method for stage execution
- `run_with_circuit_breaker(context)`: Fault-tolerant execution wrapper

#### 2.1.2 MakerAgent (`agents/base_maker_agent.py`)
**Base class for all maker agents** (agents that create/produce output).

**Execution Modes**:
1. **Initial Mode**: First-time creation from requirements
2. **Question Mode**: Conversational Q&A about previous output (threaded)
3. **Revision Mode**: Update based on reviewer/human feedback

**Abstract Properties** (must be implemented):
- `agent_display_name`: Human-readable name
- `agent_role_description`: Role and expertise description
- `output_sections`: List of expected output sections

**Optional Overrides**:
- `get_initial_guidelines()`: Mode-specific instructions
- `get_quality_standards()`: Quality criteria

**Key Methods**:
- `_determine_execution_mode(task_context)`: Automatic mode detection
- `_build_initial_prompt()`: Initial analysis prompt construction
- `_build_question_prompt()`: Conversational prompt construction
- `_build_revision_prompt()`: Revision prompt construction
- `_get_output_instructions()`: Conditional instructions based on agent capabilities
- `execute(context)`: Main execution entry point

#### 2.1.3 AgentStage (`agents/orchestrator_integration.py`)
**Generic wrapper** that adapts any agent to PipelineStage interface.

**Purpose**: Bridges agent classes to pipeline system.

### 2.2 Specialized Agents

All agents inherit from `MakerAgent` and implement specific domain expertise:

#### Planning & Analysis Agents
1. **IdeaResearcherAgent** (`agents/idea_researcher_agent.py`)
   - Research and validate feature ideas
   - Market analysis and competitive research
   - Feasibility assessment

2. **BusinessAnalystAgent** (`agents/business_analyst_agent.py`)
   - Requirements gathering and analysis
   - User story creation
   - Acceptance criteria definition

3. **WorkBreakdownAgent** (`agents/work_breakdown_agent.py`)
   - Epic decomposition into sub-issues
   - Task sequencing and dependency analysis
   - GitHub issue creation

#### Architecture & Design Agents
4. **SoftwareArchitectAgent** (`agents/software_architect_agent.py`)
   - System architecture design
   - Component interaction patterns
   - Technology stack recommendations

#### Implementation Agents
5. **SeniorSoftwareEngineerAgent** (`agents/senior_software_engineer_agent.py`)
   - Code implementation
   - File modifications
   - Feature development

#### Quality Assurance Agents
6. **CodeReviewerAgent** (`agents/code_reviewer_agent.py`)
   - Code quality review
   - Standards compliance checking
   - Feedback generation

7. **RequirementsReviewerAgent** (`agents/requirements_reviewer_agent.py`)
   - Requirements completeness validation
   - Acceptance criteria review
   - Feedback for business analyst

#### Environment & Documentation Agents
8. **DevEnvironmentSetupAgent** (`agents/dev_environment_setup_agent.py`)
   - Dockerfile.agent generation
   - Dependency management
   - Build configuration

9. **DevEnvironmentVerifierAgent** (`agents/dev_environment_verifier_agent.py`)
   - Docker image verification
   - Container testing
   - Environment validation

10. **TechnicalWriterAgent** (`agents/technical_writer_agent.py`)
    - Technical documentation creation
    - API documentation
    - User guides

11. **DocumentationEditorAgent** (`agents/documentation_editor_agent.py`)
    - Documentation review and editing
    - Consistency checking
    - Quality improvement

### 2.3 Agent Registry (`agents/__init__.py`)
**Purpose**: Central registry for agent discovery and instantiation.

**Structure**:
```python
AGENT_REGISTRY = {
    "agent_name": AgentClass,
    # ... all agents
}
```

**Functions**:
- `get_agent_class(agent_name)`: Retrieves agent class by name
- `list_agents()`: Returns list of available agents

---

## Layer 3: Execution Layer

### 3.1 Agent Executor (`services/agent_executor.py`)
**CRITICAL**: Centralized service for ALL agent executions.

**Responsibilities**:
- Guaranteed observability event emission
- Claude log streaming to Redis
- Workspace preparation and finalization
- Branch management integration
- GitHub output posting
- Dev container status management

**Key Methods**:
- `execute_agent()`: Main execution entry point
- `_create_stream_callback()`: Live Claude Code output streaming
- `_build_execution_context()`: Standardized context construction
- `_post_agent_output_to_github()`: Centralized GitHub posting
- `_extract_markdown_output()`: Output extraction from various formats
- `_queue_environment_verifier()`: Auto-queuing verifier tasks

**Execution Flow**:
1. Generate unique task_id
2. Emit task_received event
3. Create stream callback for live logs
4. Build execution context
5. Prepare workspace (branch/discussion)
6. Emit agent_initialized event (with execution_id)
7. Execute agent via PipelineStage.execute()
8. Post output to GitHub
9. Finalize workspace (commit/push)
10. Record execution outcome
11. Emit agent_completed/agent_failed event

### 3.2 Claude Integration (`claude/claude_integration.py`)
**Responsibilities**:
- Claude Code CLI execution
- Docker vs local execution routing
- MCP server configuration
- Stream output parsing
- Session continuity management

**Key Function**:
- `run_claude_code(prompt, context)`: Executes Claude with prompt

**Execution Paths**:
1. **Docker Path** (default for most agents):
   - Uses `docker_runner.run_agent_in_container()`
   - Isolated project environment
   - Full dependency access

2. **Local Path** (dev_environment_setup only):
   - Direct `claude` CLI execution
   - Access to host Docker socket
   - Used for building project images

**Stream Processing**:
- Parses JSON stream events from Claude Code
- Forwards events to websocket via callback
- Collects assistant text for final result
- Tracks token usage and session_id
- Handles error events

### 3.3 Docker Agent Runner (`claude/docker_runner.py`)
**Responsibilities**:
- Docker container lifecycle management
- Agent container execution
- Volume mounting (project, SSH keys, git config)
- Container recovery and cleanup
- Redis-based container tracking

**Key Methods**:
- `run_agent_in_container()`: Main container execution
- `_build_docker_command()`: Container command construction
- `_stream_container_logs()`: Real-time log streaming
- `cleanup_orphaned_redis_keys()`: Startup cleanup
- `_sanitize_container_name()`: Name normalization

**Container Configuration**:
- Image: `{project}-agent:latest`
- Working directory: `/workspace`
- Volume mounts:
  - Project directory → `/workspace`
  - SSH keys → `/home/orchestrator/.ssh` (read-only)
  - Git config → `/home/orchestrator/.gitconfig` (read-only)
  - MCP config → `/workspace/.mcp.json`
- Environment: CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY
- Network: `orchestrator_default`

---

## Layer 4: Pipeline System

### 4.1 Pipeline Orchestrator (`pipeline/orchestrator.py`)
**Responsibilities**:
- Sequential stage execution
- State management and checkpointing
- Error handling and recovery
- Stage coordination

**Key Class**: `SequentialPipeline`

**Methods**:
- `execute(context)`: Execute all stages sequentially
- `_execute_stage()`: Execute single stage with error handling
- `_save_checkpoint()`: Persist pipeline state
- `_load_checkpoint()`: Recover from saved state

### 4.2 Pipeline Factory (`pipeline/factory.py`)
**Responsibilities**:
- Pipeline construction from configuration
- Agent instantiation
- Stage configuration

**Key Class**: `PipelineFactory`

**Methods**:
- `create_pipeline()`: Build pipeline from config
- `create_agent()`: Instantiate configured agent
- `create_stage()`: Create pipeline stage

### 4.3 Repair Cycle System (`pipeline/repair_cycle.py`)
**Responsibilities**:
- Test-driven repair cycles
- Iterative fixing until tests pass
- Warning review capabilities
- Per-file iteration tracking
- Circuit breaker for infinite loops

**Key Classes**:
- `RepairCycleStage`: Pipeline stage for repair cycles
- `RepairTestRunConfig`: Test execution configuration
- `test_type`: String defining test type (e.g., "unit", "integration", "e2e", "pre-commit", "regression")

**Execution Flow**:
1. Run tests
2. If failures detected:
   - Parse error output
   - Identify failing files
   - For each file:
     - Agent fixes issue
     - Re-run tests
     - Repeat up to max_file_iterations
3. If warnings and review_warnings=True:
   - Agent reviews warnings
   - Assesses criticality
4. Checkpoint after each iteration
5. Circuit breaker at max_total_agent_calls

### 4.4 Repair Cycle Runner (`pipeline/repair_cycle_runner.py`)
**Responsibilities**:
- Containerized repair cycle execution
- Checkpoint persistence in Redis
- Container recovery on restart
- Test execution within container

**Key Functions**:
- `run_repair_cycle_in_container()`: Main containerized execution
- `recover_or_create_repair_cycle_container()`: Container recovery
- `parse_repair_cycle_checkpoint()`: Checkpoint deserialization

---

## Layer 5: Configuration System

### 5.1 Configuration Manager (`config/manager.py`)
**Responsibilities**:
- Configuration loading and validation
- Project configuration access
- Agent configuration lookup
- Pipeline template management
- Workflow template management

**Key Methods**:
- `get_project_config(project_name)`: Project configuration
- `get_project_agent_config(project, agent)`: Agent-specific config
- `get_pipeline_template(template_name)`: Pipeline templates
- `get_workflow_template(workflow_name)`: Workflow templates
- `list_visible_projects()`: Non-hidden projects

### 5.2 State Manager (`config/state_manager.py`)
**Responsibilities**:
- GitHub state persistence
- Board/column ID tracking
- Synchronization status management
- State backup and recovery

**Key Methods**:
- `needs_reconciliation(project)`: Check if config changed
- `save_github_state(project, state)`: Persist state
- `load_github_state(project)`: Load state
- `mark_synchronized(project)`: Update sync status

### 5.3 Environment Configuration (`config/environment.py`)
**Responsibilities**:
- Environment variable management
- Secrets loading
- Feature flags

### 5.4 Configuration Layers

#### Foundation Layer (`config/foundations/`)
**files**:
- `agents.yaml`: Agent definitions (17 agents)
  - Model, timeout, retries
  - Docker/dev container requirements
  - File write permissions
  - Makes code changes flag
- `pipelines.yaml`: Pipeline templates
  - Stage definitions
  - Maker-checker patterns
  - Review requirements
- `workflows.yaml`: Kanban board templates
  - Column definitions
  - Agent mappings
  - Progression rules
- `mcp.yaml`: MCP server configurations
  - HTTP and stdio servers
  - Per-agent assignments

#### Project Layer (`config/projects/`)
**Structure**: `{project}.yaml`

**Contains**:
- GitHub repo information
- Tech stack definitions
- Enabled pipelines
- Testing configuration
- Branch naming conventions

#### State Layer (`state/projects/{project}/`)
**Auto-managed files**:
- `github_state.yaml`: Board IDs, column IDs, sync timestamps
- `dev_container_state.yaml`: Docker image verification status

---

## Layer 6: GitHub Integration Layer

### 6.1 GitHub Project Manager (`services/github_project_manager.py`)
**Responsibilities**:
- Project board reconciliation
- Column creation and updates
- Label management
- Board discovery

**Key Methods**:
- `reconcile_project()`: Sync config with GitHub
- `create_project_board()`: Create new board
- `update_columns()`: Sync column structure
- `create_or_update_labels()`: Label management

### 6.2 Project Monitor (`services/project_monitor.py`)
**Responsibilities**:
- Continuous board polling
- Card movement detection
- Task queue population
- Change detection

**Key Methods**:
- `monitor_projects()`: Main monitoring loop (runs in thread)
- `_poll_project_board()`: Poll single board
- `_detect_card_movement()`: Movement detection
- `_create_task_from_card()`: Task creation

**Polling Interval**: 30 seconds

### 6.3 GitHub Integration (`services/github_integration.py`)
**Responsibilities**:
- Issue/discussion operations
- Comment posting
- Label management
- Workspace-aware operations (issues vs discussions)

**Key Classes**:
- `GitHubIntegration`: Main integration class
- `AgentCommentFormatter`: Comment formatting utilities

**Key Methods**:
- `post_agent_output()`: Workspace-aware output posting
- `create_comment()`: Issue comment creation
- `add_discussion_comment()`: Discussion comment creation
- `get_issue()`: Issue retrieval
- `update_issue_labels()`: Label manipulation

### 6.4 GitHub API Client (`services/github_api_client.py`)
**Responsibilities**:
- Low-level GitHub API calls
- GraphQL query execution
- REST API interactions
- Rate limit handling

### 6.5 GitHub Discussions (`services/github_discussions.py`)
**Responsibilities**:
- Discussion CRUD operations
- Category management
- Discussion-specific GraphQL queries

### 6.6 GitHub App Authentication (`services/github_app_auth.py`, `services/github_app.py`)
**Responsibilities**:
- GitHub App JWT generation
- Installation token management
- Authentication vs PAT

**Auth Modes**:
1. **GitHub App** (recommended):
   - Bot appearance with `[bot]` badge
   - Better rate limits
   - Requires private key
2. **Personal Access Token** (simple):
   - Appears as user account
   - Standard rate limits

### 6.7 GitHub Capabilities (`services/github_capabilities.py`)
**Responsibilities**:
- Feature detection (discussions, projects v2, etc.)
- Capability checking before operations

---

## Layer 7: Workspace Management Layer

### 7.1 Workspace Abstraction System (`services/workspace/`)

#### 7.1.1 WorkspaceContext (`services/workspace/context.py`)
**Abstract base class** for workspace operations.

**Methods**:
- `prepare_execution()`: Prepare workspace before agent runs
- `finalize_execution()`: Cleanup after agent completes
- `get_context_data()`: Extract workspace-specific context

#### 7.1.2 IssuesWorkspaceContext (`services/workspace/issues_context.py`)
**Git-based workspace** for issue workflows.

**Responsibilities**:
- Feature branch management
- Git operations (checkout, commit, push, pull --rebase)
- Merge conflict detection
- Stale branch detection
- Parent/sub-issue hierarchical branches

**Key Methods**:
- `prepare_execution()`: Create/checkout feature branch
- `finalize_execution()`: Commit and push changes
- `_ensure_branch()`: Branch creation/reuse logic
- `_detect_parent_issue()`: Parent issue detection
- `_handle_merge_conflicts()`: Conflict handling

#### 7.1.3 DiscussionsWorkspaceContext (`services/workspace/discussions_context.py`)
**Discussion-based workspace** for non-git workflows.

**Responsibilities**:
- Discussion lifecycle management
- Discussion ID tracking
- No git operations

**Key Methods**:
- `prepare_execution()`: Ensure discussion exists
- `finalize_execution()`: No-op (discussions self-commit via comments)
- `get_context_data()`: Return discussion_id

#### 7.1.4 HybridWorkspaceContext (`services/workspace/hybrid_context.py`)
**Hybrid workspace** supporting both git and discussion operations.

**Responsibilities**:
- Dynamic routing to git or discussion based on agent
- Conditional branch management
- Flexible context construction

**Agent Routing**:
- Code agents (senior_software_engineer): Use git branches
- Analysis agents (business_analyst, idea_researcher): Discussion only
- Reviewer agents: Discussion only

#### 7.1.5 WorkspaceContextFactory (`services/workspace/__init__.py`)
**Factory** for workspace creation.

**Method**:
- `create(workspace_type, project, issue_number, task_context, github_integration)`: Factory method

**Workspace Types**:
- `"issues"`: IssuesWorkspaceContext
- `"discussions"`: DiscussionsWorkspaceContext
- `"hybrid"`: HybridWorkspaceContext

### 7.2 Project Workspace Manager (`services/project_workspace.py`)
**Responsibilities**:
- Project directory management
- Workspace initialization
- Checkout management
- Path resolution

**Key Methods**:
- `initialize_all_projects()`: Initialize all configured projects
- `get_project_dir()`: Get project directory path
- `clone_or_update_project()`: Git clone/pull operations

**Workspace Structure**:
```
/workspace/
├── switchyard/       # Orchestrator code
└── {project}/          # Managed project checkouts
```

### 7.3 Git Workflow Manager (`services/git_workflow_manager.py`)
**Responsibilities**:
- Git command execution
- Branch operations
- Commit operations
- PR management

**Key Methods**:
- `create_branch()`: Branch creation
- `checkout_branch()`: Branch switching
- `commit_changes()`: Auto-commit
- `push_changes()`: Push to remote
- `create_pull_request()`: PR creation via gh CLI

### 7.4 Feature Branch Manager (`services/feature_branch_manager.py`)
**Responsibilities**:
- Feature branch lifecycle
- Parent/sub-issue tracking
- Branch naming conventions
- Branch reuse detection

**Key Methods**:
- `get_or_create_branch()`: Intelligent branch selection
- `_find_parent_issue()`: Parent detection from GitHub API
- `_detect_existing_branch()`: Branch reuse logic
- `_create_branch_name()`: Name generation

**Branch Naming**:
- Standalone: `feature/issue-{number}`
- Sub-issue: `feature/issue-{parent}/sub-{sub_number}`

---

## Layer 8: State Management Layer

### 8.1 State Manager (`state_management/manager.py`)
**Responsibilities**:
- Pipeline state persistence
- Checkpoint management
- Recovery from failures
- State file I/O

**Key Methods**:
- `save_checkpoint()`: Save pipeline state
- `load_checkpoint()`: Restore pipeline state
- `clear_checkpoint()`: Remove completed checkpoints

**State Storage**: `orchestrator_data/state/`

### 8.2 Work Execution State (`services/work_execution_state.py`)
**Responsibilities**:
- Track in_progress executions
- Prevent duplicate work
- Cleanup stuck states
- Redis-based state tracking

**Key Methods**:
- `mark_in_progress()`: Mark work started
- `mark_completed()`: Mark work done
- `cleanup_stuck_in_progress_states()`: Startup cleanup
- `is_in_progress()`: Check if work is active

**Redis Keys**:
- `execution_state:{project}:{issue_number}:{column}:{agent}`
- TTL: 2 hours

### 8.3 Conversational Session State (`services/conversational_session_state.py`)
**Responsibilities**:
- Track conversational threads
- Session persistence
- Conversation history
- Column exit detection

**Key Methods**:
- `start_session()`: Begin conversation
- `update_session()`: Add conversation turn
- `end_session()`: Close conversation
- `get_active_session()`: Retrieve session state

**Session Data**:
- Thread history (agent + human messages)
- Last interaction timestamps
- Session status (active/paused/completed)
- Column tracking for exit detection

---

## Layer 9: Task Queue Layer

### 9.1 Task Manager (`task_queue/task_manager.py`)
**Responsibilities**:
- Task queue management
- Priority-based queuing
- Redis-backed persistence
- In-memory fallback

**Key Classes**:
- `Task`: Task dataclass (id, agent, project, priority, context, created_at)
- `TaskPriority`: Enum (LOW=1, MEDIUM=2, HIGH=3, CRITICAL=4)
- `TaskQueue`: Queue implementation

**Queue Methods**:
- `enqueue(task)`: Add task to queue
- `dequeue()`: Get highest priority task
- `peek()`: View next task without removing
- `size()`: Queue length

**Redis Implementation**:
- Sorted set: `orchestrator:tasks:queue`
- Score: `-(priority * 1000 + timestamp)` (higher priority + older = lower score = first)
- Backup: JSON file at `orchestrator_data/queue_backup.json`

---

## Layer 10: Observability & Monitoring Layer

### 10.1 Observability Manager (`monitoring/observability.py`)
**Responsibilities**:
- Event streaming to Redis
- Elasticsearch indexing
- Event history management
- Real-time pub/sub

**Event Categories**:
1. **Agent Lifecycle**: task_received, agent_initialized, agent_started, agent_completed, agent_failed
2. **Decision Events**: 60+ decision event types (routing, feedback, progression, etc.)
3. **Claude Events**: prompt_constructed, claude_api_call_started, claude_api_call_completed
4. **Tool Events**: tool_execution_started, tool_execution_completed

**Storage**:
- **Redis Pub/Sub**: Channel `orchestrator:agent_events` (real-time delivery)
- **Redis Stream**: Key `orchestrator:event_stream` (1000 events, 2hr TTL)
- **Elasticsearch**: Daily indices `decision-events-YYYY-MM-DD`, `agent-events-YYYY-MM-DD`

**Key Methods**:
- `emit()`: Generic event emission
- `emit_task_received()`: Task event
- `emit_agent_initialized()`: Agent start (returns execution_id)
- `emit_agent_completed()`: Agent finish
- `cleanup_stale_agent_events_on_startup()`: Startup cleanup

### 10.2 Decision Event Emitter (`monitoring/decision_events.py`)
**Responsibilities**:
- Convenience wrapper for decision events
- Consistent decision event structure
- High-level decision abstractions

**Decision Categories**:
- **routing**: Agent selection, workspace routing
- **feedback**: Feedback detection, listening, ignoring
- **progression**: Status changes, stage transitions
- **review_cycle**: Maker-checker cycle tracking
- **conversational_loop**: Q&A sessions
- **error_handling**: Errors, retries, circuit breakers
- **task_management**: Queue operations
- **branch_management**: Git branch decisions

**Key Methods** (60+ methods):
- `emit_agent_routing_decision()`: Agent selection
- `emit_feedback_detected()`: Feedback events
- `emit_status_progression()`: Column movements
- `emit_review_cycle_decision()`: Review cycle steps
- `emit_conversational_loop_started()`: Q&A start
- `emit_error_decision()`: Error handling
- `emit_branch_created()`: Branch creation
- ... many more

### 10.3 Decision Analytics (`monitoring/decision_analytics.py`)
**Responsibilities**:
- Elasticsearch query abstractions
- Decision pattern analysis
- Metrics aggregation
- Historical analysis

### 10.4 Observability Server (`services/observability_server.py`)
**Responsibilities**:
- REST API for web UI
- Agent status tracking
- Event history retrieval
- Claude log streaming
- Pipeline run management
- Review filter management

**Endpoints**:
- `GET /health`: System health
- `GET /agents/active`: Active agents
- `GET /history`: Agent execution history
- `GET /claude-logs-history`: Claude logs
- `GET /current-pipeline`: Current pipeline state
- `GET /pipeline-run-events`: Pipeline run events
- `GET /active-pipeline-runs`: Active runs
- `POST /agents/kill/<container>`: Kill agent
- `GET /api/review-filters`: Review filters
- `GET /api/circuit-breakers`: Circuit breaker states
- `GET /api/projects`: Project list

**Port**: 5001

### 10.5 Metrics Collector (`monitoring/metrics.py`)
**Responsibilities**:
- Task execution metrics
- Quality metrics
- Elasticsearch indexing
- JSON backup

**Metrics**:
- Task: agent, duration, success/failure
- Quality: agent, metric_name, score

**Indices**:
- `orchestrator-task-metrics-YYYY.MM.DD`
- `orchestrator-quality-metrics-YYYY.MM.DD`

**Backup**: `orchestrator_data/metrics/`

### 10.6 Health Monitor (`monitoring/health_monitor.py`)
**Responsibilities**:
- System health checks
- Component health tracking
- Redis health status persistence

**Health Checks**:
- Redis connectivity
- GitHub API access
- Docker socket access
- Elasticsearch connectivity

**Redis Key**: `orchestrator:health:status` (TTL: 10 minutes)

### 10.7 Logging (`monitoring/logging.py`)
**Responsibilities**:
- Structured JSON logging
- File logging
- Console logging
- Log rotation

**Log Levels**: DEBUG, INFO, WARNING, ERROR, CRITICAL

**Log Destinations**:
- Console (stdout)
- File: `orchestrator_data/logs/orchestrator.log`

---

## Layer 11: Review & Feedback Layer

### 11.1 Review Cycle Service (`services/review_cycle.py`)
**Responsibilities**:
- Maker-checker workflow orchestration
- Iteration tracking
- Escalation logic
- Review outcome tracking

**Key Methods**:
- `start_review_cycle()`: Initiate cycle
- `execute_iteration()`: Run one maker-checker iteration
- `should_escalate()`: Check if escalation needed
- `complete_cycle()`: Finalize cycle

**Review Flow**:
1. Maker creates output
2. Reviewer reviews output
3. If issues found:
   - Queue revision task for maker
   - Increment iteration
   - Repeat (max 3 iterations)
4. If max iterations reached: Escalate to human
5. If approved: Mark complete, auto-advance

### 11.2 Review Parser (`services/review_parser.py`)
**Responsibilities**:
- Parse reviewer output
- Extract issues and approval status
- Structured feedback extraction

**Key Methods**:
- `parse_review_output()`: Extract review structure
- `is_approved()`: Check for approval markers
- `extract_issues()`: Get list of issues

**Review Output Format**:
```markdown
## Issues
1. [Issue title]: Description
2. [Issue title]: Description

## Approval
[APPROVED] or [CHANGES REQUESTED]
```

### 11.3 Review Filter Manager (`services/review_filter_manager.py`)
**Responsibilities**:
- Manage review issue filters
- Pattern-based issue suppression
- Learning from human feedback

**Filter Types**:
- Regex patterns
- Agent-specific filters
- Global filters

**Storage**: Elasticsearch index `review-filters`

### 11.4 Review Pattern Detector (`services/review_pattern_detector.py`)
**Responsibilities**:
- Detect recurring review issues
- Suggest filter creation
- Pattern analysis

### 11.5 Feedback Manager (`services/feedback_manager.py`)
**Responsibilities**:
- Track feedback on agent outputs
- Detect user comments/reactions
- Route feedback to agents
- Prevent duplicate feedback processing

**Key Methods**:
- `detect_feedback()`: Check for new feedback
- `route_feedback()`: Queue revision tasks
- `set_last_agent_comment_time()`: Track agent comments
- `has_unprocessed_feedback()`: Check for pending feedback

**Feedback Detection**:
- New comments after agent output
- Label changes
- Reaction emojis
- Status changes

### 11.6 Human Feedback Loop (`services/human_feedback_loop.py`)
**Responsibilities**:
- Human-in-the-loop interactions
- Question/answer routing
- Conversation threading
- Escalation handling

---

## Layer 12: Services Layer (Additional)

### 12.1 Pipeline Progression (`services/pipeline_progression.py`)
**Responsibilities**:
- Issue movement between columns
- Workflow rule enforcement
- Auto-advancement logic

**Key Methods**:
- `move_issue_to_column()`: Move card
- `get_next_column()`: Determine next stage
- `can_auto_advance()`: Check advancement rules

### 12.2 Pipeline Run Manager (`services/pipeline_run.py`)
**Responsibilities**:
- Track pipeline run lifecycle
- Pipeline run ID generation
- Active run tracking
- Run history

**Key Methods**:
- `start_pipeline_run()`: Begin run
- `complete_pipeline_run()`: End run
- `get_active_runs()`: List active
- `cleanup_stale_active_runs_on_startup()`: Startup cleanup

**Elasticsearch Index**: `pipeline-runs-YYYY-MM-DD`

### 12.3 Auto Commit Service (`services/auto_commit.py`)
**Responsibilities**:
- Automatic git commits after agent work
- Commit message generation
- Change detection

### 12.4 Dev Container State (`services/dev_container_state.py`)
**Responsibilities**:
- Track Docker image verification status
- Image existence checking
- Project build state

**States**:
- `UNVERIFIED`: Not yet verified
- `IN_PROGRESS`: Setup running
- `VERIFIED`: Image exists and works
- `BLOCKED`: Manual intervention needed

**Storage**: `state/dev_containers/{project}.yaml`

### 12.5 Agent Container Recovery (`services/agent_container_recovery.py`)
**Responsibilities**:
- Recover or cleanup containers on restart
- Container state assessment
- Orphaned container cleanup

**Recovery Logic**:
- If container running + Redis tracking key exists: Recover
- If container running + no Redis key: Kill (orphaned)
- If container stopped: Remove

### 12.6 Workspace Router (`services/workspace_router.py`)
**Responsibilities**:
- Route work to issues vs discussions
- Determine workspace type per stage
- Category selection for discussions

### 12.7 Scheduled Tasks Service (`services/scheduled_tasks.py`)
**Responsibilities**:
- Periodic maintenance tasks
- Scheduled cleanup
- Background jobs

**Tasks**:
- Cleanup orphaned branches
- Prune old events
- Health checks

---

## Layer 13: Utility & Support Layers

### 13.1 Circuit Breakers (`services/circuit_breaker.py`)
**Responsibilities**:
- Fault tolerance
- Automatic recovery
- Failure tracking

**States**: CLOSED, OPEN, HALF_OPEN

**Parameters**:
- `failure_threshold`: Failures before opening
- `timeout_seconds`: Time before half-open retry
- `half_open_requests`: Requests to test in half-open

### 13.2 Timestamp Utils (`monitoring/timestamp_utils.py`)
**Responsibilities**:
- Consistent UTC timestamp handling
- ISO 8601 formatting
- Timezone normalization

**Functions**:
- `utc_now()`: Current UTC datetime
- `utc_isoformat()`: ISO 8601 string
- `parse_iso_timestamp()`: Parse string to datetime

### 13.3 Claude Token Scheduler (`monitoring/claude_token_scheduler.py`)
**Responsibilities**:
- Rate limiting Claude API calls
- Token bucket algorithm
- Request scheduling

### 13.4 Claude Code Failure Handler (`services/claude_code_failure_handler.py`)
**Responsibilities**:
- Detect Claude Code crashes
- Parse error logs
- Recovery strategies

### 13.5 Claude Code Breaker (`monitoring/claude_code_breaker.py`)
**Responsibilities**:
- Monitor for infinite loops
- Claude Code hang detection
- Automatic termination

---

## Layer 14: Pattern Detection & Analysis Layer

### 14.1 Pattern Detection Service (`services/pattern_detector_es.py`)
**Responsibilities**:
- Detect patterns in logs/events
- Elasticsearch-based pattern matching
- Anomaly detection

### 14.2 Pattern Ingestion Service (`services/pattern_ingestion_service.py`)
**Responsibilities**:
- Ingest logs and events for analysis
- Pre-processing and normalization
- Index management

### 14.3 Pattern Analysis Service (`services/pattern_analysis_service.py`)
**Responsibilities**:
- Analyze detected patterns
- Trend analysis
- Root cause analysis

### 14.4 Pattern GitHub Integration (`services/pattern_github_integration_es.py`)
**Responsibilities**:
- Link patterns to GitHub issues
- Automatic issue creation for patterns
- Pattern annotation on PRs

### 14.5 Pattern Alerting (`services/pattern_alerting.py`)
**Responsibilities**:
- Alert on critical patterns
- Notification routing
- Alert throttling

---

## Layer 15: External Dependencies

### 15.1 Redis
**Purpose**:
- Task queue persistence
- Event streaming (pub/sub)
- Event history (streams)
- Health status caching
- Container tracking
- Work execution state

**Keys/Channels**:
- `orchestrator:tasks:queue` (sorted set)
- `orchestrator:agent_events` (pub/sub channel)
- `orchestrator:event_stream` (stream)
- `orchestrator:claude_stream` (pub/sub channel)
- `orchestrator:claude_logs_stream` (stream)
- `orchestrator:health:status` (string)
- `execution_state:*` (strings with TTL)

### 15.2 Elasticsearch
**Purpose**:
- Decision event indexing
- Agent lifecycle indexing
- Pattern detection
- Metrics storage
- Historical analysis

**Indices**:
- `decision-events-YYYY-MM-DD`
- `agent-events-YYYY-MM-DD`
- `orchestrator-task-metrics-YYYY.MM.DD`
- `orchestrator-quality-metrics-YYYY.MM.DD`
- `pipeline-runs-YYYY-MM-DD`
- `review-filters`

### 15.3 Docker
**Purpose**:
- Agent container execution
- Environment isolation
- Dependency management

**Images**:
- Orchestrator: `switchyard`
- Agent: `{project}-agent:latest` (per project)

### 15.4 GitHub
**Purpose**:
- Project board management
- Issue/discussion management
- Code repository hosting
- PR management

**APIs Used**:
- REST API v3
- GraphQL API v4
- GitHub CLI (`gh`)

### 15.5 Claude API
**Purpose**:
- AI agent intelligence
- Code generation
- Analysis and reasoning

**Access Methods**:
- Claude Code CLI (`claude`)
- Subscription (CLAUDE_CODE_OAUTH_TOKEN)
- Pay-per-use (ANTHROPIC_API_KEY)

---

## Summary Statistics

**Total Components**: 150+

**Layers**: 15

**Python Modules**: 120+

**Agents**: 15 specialized agents

**Event Types**: 70+

**Configuration Files**: 20+

**External Services**: 5 (Redis, Elasticsearch, Docker, GitHub, Claude)

**Lines of Code**: ~25,000+

---

## Component Interaction Density

**High Interaction Components**:
1. AgentExecutor - Coordinates 10+ subsystems
2. ObservabilityManager - Receives events from all layers
3. ConfigManager - Used by all components
4. GitHubIntegration - Used by 15+ services
5. WorkspaceContext - Central to execution flow

**Independent Components**:
- Pattern detection services (can operate standalone)
- Scheduled tasks (background operations)
- Health monitor (isolated health checks)

This completes the comprehensive inventory of all functional components and layers in the system.
