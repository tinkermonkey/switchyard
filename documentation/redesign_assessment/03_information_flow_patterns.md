# Information Flow Patterns - End-to-End Data Movement

This document traces how configuration, environment information, project information, and task information flows through the system from initialization to completion.

---

## Flow 1: System Initialization Flow

**Trigger**: `main.py` startup

### Stage 1: Environment & Configuration Loading

```
┌─────────────────────────────────────────────────────────────┐
│ STARTUP SEQUENCE                                             │
└─────────────────────────────────────────────────────────────┘

1. main.py::main()
   │
   ├─> Setup zombie process reaper (SIGCHLD handler)
   │   └─> Prevents subprocess zombies throughout system lifetime
   │
   ├─> Environment() initialization
   │   ├─> Loads .env file
   │   ├─> Reads environment variables:
   │   │   ├─ REDIS_HOST, REDIS_PORT
   │   │   ├─ ELASTICSEARCH_HOST, ELASTICSEARCH_PORT
   │   │   ├─ GITHUB_TOKEN or GITHUB_APP_* credentials
   │   │   ├─ CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY
   │   │   └─ CONTEXT7_API_KEY (optional)
   │   └─> env_config [object]
   │
   ├─> ConfigManager() initialization
   │   ├─> Loads config/foundations/agents.yaml
   │   │   └─> agent_definitions [Dict]: 17 agents with properties
   │   ├─> Loads config/foundations/pipelines.yaml
   │   │   └─> pipeline_templates [Dict]: 3 templates
   │   ├─> Loads config/foundations/workflows.yaml
   │   │   └─> workflow_templates [Dict]: 3 workflows
   │   ├─> Loads config/foundations/mcp.yaml
   │   │   └─> mcp_server_definitions [Dict]: MCP servers
   │   ├─> Scans config/projects/*.yaml
   │   │   └─> project_configs [Dict[str, ProjectConfig]]
   │   └─> config_manager [global singleton]
   │
   └─> StateManager() initialization
       ├─> Scans state/projects/{project}/github_state.yaml
       │   └─> github_states [Dict]: Project board IDs, column IDs
       ├─> Scans state/dev_containers/{project}.yaml
       │   └─> dev_container_states [Dict]: Image verification status
       └─> state_manager [global singleton]
```

### Stage 2: Infrastructure Initialization

```
┌─────────────────────────────────────────────────────────────┐
│ INFRASTRUCTURE SETUP                                         │
└─────────────────────────────────────────────────────────────┘

2. Subsystem Initialization
   │
   ├─> OrchestratorLogger("orchestrator")
   │   ├─> Configures console handler
   │   ├─> Configures file handler (orchestrator_data/logs/)
   │   └─> logger [singleton]
   │
   ├─> MetricsCollector()
   │   ├─> Connects to Elasticsearch (orchestrator-*-metrics indices)
   │   └─> metrics [object]
   │
   ├─> StateManager(Path("orchestrator_data/state"))
   │   └─> checkpoint_manager [object]
   │
   ├─> TaskQueue(use_redis=True)
   │   ├─> Connects to Redis (orchestrator:tasks:queue)
   │   ├─> Loads backup from orchestrator_data/queue_backup.json
   │   └─> task_queue [object]
   │
   ├─> HealthMonitor()
   │   └─> health_monitor [object]
   │
   ├─> GitHubProjectManager(config_manager, state_manager)
   │   ├─> Authenticates with GitHub (PAT or App)
   │   └─> github_project_manager [object]
   │
   └─> workspace_manager.initialize_all_projects()
       ├─> For each project in config_manager.list_visible_projects():
       │   ├─> Determines project_dir: /workspace/{project}/
       │   ├─> Checks if directory exists
       │   ├─> If not exists:
       │   │   ├─> Clones from project_config.github.repo_url
       │   │   └─> Returns {project: True} (needs_dev_setup)
       │   └─> If exists:
       │       ├─> Validates .git directory
       │       └─> Returns {project: False}
       └─> projects_needing_setup [Dict[str, bool]]
```

### Stage 3: Startup Cleanup & Recovery

```
┌─────────────────────────────────────────────────────────────┐
│ STARTUP CLEANUP                                              │
└─────────────────────────────────────────────────────────────┘

3. Recovery Operations
   │
   ├─> wait_for_elasticsearch()
   │   └─> elasticsearch_ready [bool]
   │
   ├─> agent_container_recovery.recover_or_cleanup_containers()
   │   ├─> Lists all Docker containers (docker ps --all)
   │   ├─> For each container matching "claude-agent-*":
   │   │   ├─> Checks Redis key: agent_container:{name}
   │   │   ├─> If key exists + container running:
   │   │   │   └─> RECOVER: Leave running (agent will resume)
   │   │   ├─> If no key + container running:
   │   │   │   └─> KILL: Orphaned container
   │   │   └─> If container stopped:
   │   │       └─> REMOVE: Clean up
   │   └─> (recovered, killed, errors) [Tuple[int, int, int]]
   │
   ├─> agent_container_recovery.recover_or_cleanup_repair_cycle_containers()
   │   └─> Same logic for repair cycle containers
   │
   ├─> DockerAgentRunner.cleanup_orphaned_redis_keys()
   │   ├─> Scans Redis keys: agent_container:*
   │   ├─> For each key:
   │   │   ├─> Checks if Docker container exists
   │   │   └─> If not: Delete Redis key
   │   └─> orphaned_keys_removed [int]
   │
   ├─> work_execution_tracker.cleanup_stuck_in_progress_states()
   │   ├─> Scans Redis keys: execution_state:*
   │   ├─> For each key:
   │   │   ├─> Gets value: {status, started_at, ...}
   │   │   ├─> If status == 'in_progress' AND age > 2 hours:
   │   │   │   └─> Delete key
   │   └─> stuck_states_removed [int]
   │
   ├─> pipeline_run_manager.cleanup_stale_active_runs_on_startup()
   │   │   (only if Elasticsearch available)
   │   ├─> Queries ES index: pipeline-runs-*/active: true
   │   ├─> For each run:
   │   │   ├─> If start_time > 2 hours ago:
   │   │   │   └─> Updates ES: active=false, status='interrupted'
   │   └─> stale_runs_cleaned [int]
   │
   └─> observability.cleanup_stale_agent_events_on_startup()
       │   (only if Elasticsearch available)
       ├─> Reads Redis stream: orchestrator:event_stream
       ├─> Tracks agent states:
       │   ├─> agent_initialized events → {task_id: {agent, timestamp, completed: False}}
       │   └─> agent_completed/failed events → mark completed=True
       ├─> For each uncompleted task older than 2 hours:
       │   └─> Emits synthetic agent_failed event
       └─> stale_events_cleaned [int]
```

### Stage 4: Project Reconciliation & Setup

```
┌─────────────────────────────────────────────────────────────┐
│ PROJECT INITIALIZATION                                       │
└─────────────────────────────────────────────────────────────┘

4. Project Setup Loop
   │
   ├─> dev_container_state.verify_and_update_status(project)
   │   ├─> For each project in projects_needing_setup:
   │   │   ├─> Checks Docker image: docker image inspect {project}-agent:latest
   │   │   ├─> If image not found:
   │   │   │   └─> Sets projects_needing_setup[project] = True
   │   │   └─> Else:
   │   │       └─> Updates state: VERIFIED
   │   └─> projects_needing_setup [Dict] (updated)
   │
   ├─> Queue dev_environment_setup tasks
   │   └─> For each project where needs_setup == True:
   │       ├─> Creates Task:
   │       │   ├─ id: "dev_env_setup_{project}_{timestamp}"
   │       │   ├─ agent: "dev_environment_setup"
   │       │   ├─ project: project_name
   │       │   ├─ priority: HIGH
   │       │   ├─ context: {
   │       │   │     'issue': {'title': 'Dev env setup', 'body': '...', 'number': 0},
   │       │   │     'board': 'system',
   │       │   │     'automated_setup': True,
   │       │   │     'use_docker': False  # Runs locally
   │       │   │  }
   │       │   └─ created_at: ISO timestamp
   │       └─> task_queue.enqueue(task)
   │
   └─> GitHub Board Reconciliation
       └─> For each project in config_manager.list_visible_projects():
           ├─> state_manager.needs_reconciliation(project)
           │   ├─> Computes config hash from project.yaml
           │   ├─> Compares with last_config_hash in github_state.yaml
           │   └─> needs_reconcile [bool]
           │
           └─> github_project_manager.reconcile_project(project)
               ├─> Loads project_config from ConfigManager
               ├─> Loads github_state from StateManager
               ├─> For each pipeline in project_config.pipelines:
               │   ├─> Checks if board exists in GitHub
               │   │   └─> GraphQL: query { organization { projectsV2 } }
               │   ├─> If not exists:
               │   │   ├─> Creates board via GraphQL mutation
               │   │   └─> Stores project_id, project_number
               │   ├─> Gets column field ID via GraphQL
               │   ├─> For each column in workflow_template.columns:
               │   │   ├─> Checks if option exists
               │   │   └─> If not: Creates option via GraphQL
               │   └─> Saves column_id mapping to github_state
               ├─> Creates/updates repository labels
               │   └─> For each pipeline:
               │       └─> Ensures label exists: "pipeline:{board_name}"
               ├─> Saves updated github_state to state/projects/{project}/
               └─> success [bool]
```

### Stage 5: Background Services Start

```
┌─────────────────────────────────────────────────────────────┐
│ BACKGROUND SERVICES                                          │
└─────────────────────────────────────────────────────────────┘

5. Start Monitoring Services
   │
   ├─> ProjectMonitor(task_queue, config_manager)
   │   └─> Starts in background thread:
   │       └─> monitor_thread.start()
   │           └─> monitor_projects() loop (every 30 seconds)
   │
   └─> ScheduledTasksService()
       └─> scheduler.start()
           └─> Periodic tasks:
               ├─> Cleanup orphaned branches (daily)
               ├─> Prune old events (hourly)
               └─> Health checks (every 5 minutes)
```

**Information at End of Initialization**:

```python
# Global Singletons Available
- config_manager: Full project/agent/pipeline configurations
- state_manager: GitHub board state, dev container state
- task_queue: Redis-backed queue (may have dev setup tasks)
- observability: Event streaming infrastructure
- metrics: Metrics collection to ES
- logger: Structured logging
- github_project_manager: GitHub API wrapper
- workspace_manager: Project directory management

# System State
- All project directories initialized in /workspace/
- GitHub boards reconciled with configuration
- Dev setup tasks queued for unverified projects
- Background monitoring active
- Recovery complete for any interrupted work
```

---

## Flow 2: GitHub Board Monitoring → Task Creation Flow

**Trigger**: ProjectMonitor detects card movement

### Stage 1: Board Polling

```
┌─────────────────────────────────────────────────────────────┐
│ CONTINUOUS BOARD MONITORING                                  │
└─────────────────────────────────────────────────────────────┘

ProjectMonitor.monitor_projects() [background thread]
   │
   └─> Every 30 seconds:
       └─> For each visible project:
           └─> For each pipeline in project.pipelines:
               │
               ├─> Loads github_state for project
               │   ├─> project_id: GitHub GraphQL project ID
               │   └─> columns: {column_name: column_id}
               │
               ├─> GitHub GraphQL Query:
               │   query GetProjectItems {
               │     node(id: $projectId) {
               │       ... on ProjectV2 {
               │         items(first: 100) {
               │           nodes {
               │             id
               │             fieldValues {
               │               ... on ProjectV2ItemFieldSingleSelectValue {
               │                 field { name }
               │                 name  # Column name
               │               }
               │             }
               │             content {
               │               ... on Issue {
               │                 number
               │                 title
               │                 body
               │                 labels { nodes { name } }
               │                 state
               │               }
               │               ... on DraftIssue {
               │                 title
               │                 body
               │               }
               │             }
               │           }
               │         }
               │       }
               │     }
               │   }
               │   └─> items_data [List[Dict]]
               │
               ├─> For each item in items_data:
               │   ├─> Extracts:
               │   │   ├─ issue_number: item.content.number
               │   │   ├─ column_name: item.fieldValues[0].name
               │   │   ├─ issue_title: item.content.title
               │   │   ├─ issue_body: item.content.body
               │   │   ├─ labels: [label.name for label in item.content.labels]
               │   │   └─ state: item.content.state
               │   │
               │   ├─> Checks Redis key: last_column:{project}:{issue_number}
               │   │   └─> last_column [Optional[str]]
               │   │
               │   └─> If column_name != last_column:
               │       └─> CARD MOVEMENT DETECTED
               │           └─> Continue to Stage 2
               │
               └─> Updates Redis: last_column:{project}:{issue_number} = column_name
```

### Stage 2: Task Creation Decision

```
┌─────────────────────────────────────────────────────────────┐
│ TASK CREATION LOGIC                                          │
└─────────────────────────────────────────────────────────────┘

Card moved to new column
   │
   ├─> Determines workspace type:
   │   ├─> Loads workflow_template for pipeline
   │   ├─> Finds column configuration
   │   ├─> Gets workspace type from pipeline config
   │   │   ├─ 'issues': Git-based workflow
   │   │   ├─ 'discussions': Discussion-based workflow
   │   │   └─ 'hybrid': Dynamic based on agent
   │   └─> workspace_type [str]
   │
   ├─> Determines agent for column:
   │   ├─> Looks up workflow_template.columns
   │   ├─> Finds column by name
   │   └─> agent_name = column.agent
   │
   ├─> Determines discussion category (if discussions):
   │   └─> category_id = column.discussion_category (optional)
   │
   ├─> Checks if work already in progress:
   │   ├─> Redis key: execution_state:{project}:{issue_number}:{column}:{agent}
   │   └─> If exists with status='in_progress':
   │       └─> SKIP (prevent duplicate work)
   │
   ├─> Emits decision event:
   │   └─> decision_events.emit_agent_routing_decision(
   │           issue_number, project, board, column, agent_name, reason,
   │           workspace_type, discussion_id, pipeline_run_id
   │       )
   │
   └─> Creates Task:
       ├─ id: "card_moved_{project}_{issue_number}_{timestamp}"
       ├─ agent: agent_name
       ├─ project: project_name
       ├─ priority: MEDIUM
       ├─ context: {
       │     'issue': {
       │         'number': issue_number,
       │         'title': issue_title,
       │         'body': issue_body,
       │         'labels': labels,
       │         'state': state
       │     },
       │     'issue_number': issue_number,
       │     'board': board_name,
       │     'column': column_name,
       │     'repository': repo_name,
       │     'project': project_name,
       │     'workspace_type': workspace_type,
       │     'discussion_id': discussion_id,  # If discussions
       │     'trigger': 'card_movement',
       │     'use_docker': True,  # Default for most agents
       │     'pipeline_run_id': f"pipeline_{project}_{issue_number}_{timestamp}"
       │  }
       └─ created_at: ISO timestamp
```

### Stage 3: Task Queuing

```
┌─────────────────────────────────────────────────────────────┐
│ TASK QUEUE INGESTION                                         │
└─────────────────────────────────────────────────────────────┘

task_queue.enqueue(task)
   │
   ├─> Serializes Task to JSON
   ├─> Calculates score: -(priority.value * 1000 + timestamp)
   ├─> Redis ZADD: orchestrator:tasks:queue {task_json: score}
   ├─> Backup to file: orchestrator_data/queue_backup.json
   │
   └─> Emits decision event:
       └─> decision_events.emit_task_queued(
               agent, project, issue_number, board, priority, reason, pipeline_run_id
           )
```

**Information at End of Flow**:

```python
# Task in queue with full context:
{
    'id': 'card_moved_context-studio_123_1234567890',
    'agent': 'business_analyst',
    'project': 'context-studio',
    'priority': TaskPriority.MEDIUM,
    'context': {
        'issue': {full issue data},
        'issue_number': 123,
        'board': 'Planning',
        'column': 'Requirements Analysis',
        'workspace_type': 'discussions',
        'discussion_id': 'D_kwDOABCDEF01',
        'trigger': 'card_movement',
        'pipeline_run_id': 'pipeline_context-studio_123_1234567890'
    },
    'created_at': '2025-10-23T10:30:00Z'
}
```

---

## Flow 3: Task Execution → Agent Completion Flow

**Trigger**: Main orchestrator loop dequeues task

### Stage 1: Task Dequeue & Validation

```
┌─────────────────────────────────────────────────────────────┐
│ TASK PROCESSING START                                        │
└─────────────────────────────────────────────────────────────┘

main.py main loop
   │
   ├─> task = task_queue.dequeue()
   │   ├─> Redis ZRANGE: orchestrator:tasks:queue 0 0
   │   ├─> Deserializes Task from JSON
   │   └─> Returns Task object
   │
   └─> process_task_integrated(task, state_manager, logger)
       │
       ├─> Extracts task context:
       │   ├─ task_context = task.context
       │   ├─ board_name = task_context['board']
       │   ├─ issue_number = task_context['issue_number']
       │   └─ workspace_type = task_context.get('workspace_type', 'issues')
       │
       ├─> Emits event:
       │   └─> decision_events.emit_task_dequeued(
       │           agent, project, issue_number, board, workspace_type
       │       )
       │
       ├─> Validates task can run:
       │   └─> validate_task_can_run(task, logger)
       │       ├─> Gets agent_config from ConfigManager
       │       ├─> Checks agent_config.requires_dev_container
       │       ├─> If requires_dev_container:
       │       │   ├─> Checks dev_container_state.get_status(project)
       │       │   ├─> If status != VERIFIED:
       │       │   │   ├─> Emits decision event: Error + needs_dev_setup
       │       │   │   └─> Queue dev_environment_setup task
       │       │   └─> Returns {can_run: False, needs_dev_setup: True}
       │       └─> Returns {can_run: True, reason: "..."}
       │
       └─> If can_run:
           └─> Continue to Stage 2
```

### Stage 2: Agent Executor Initialization

```
┌─────────────────────────────────────────────────────────────┐
│ AGENT EXECUTOR SETUP                                         │
└─────────────────────────────────────────────────────────────┘

executor = get_agent_executor()
executor.execute_agent(agent_name, project_name, task_context, task_id_prefix)
   │
   ├─> Generates unique task_id:
   │   └─> task_id = f"{task_id_prefix}_{agent_name}_{timestamp}"
   │
   ├─> Emits event:
   │   └─> obs.emit_task_received(agent, task_id, project, task_context)
   │
   ├─> Extracts pipeline_run_id from task_context
   │   └─> pipeline_run_id = task_context.get('pipeline_run_id')
   │
   ├─> Creates stream callback:
   │   └─> stream_callback = _create_stream_callback(agent, task_id, project, pipeline_run_id)
   │       └─> Returns function that:
   │           ├─> Receives Claude Code stream events
   │           ├─> Publishes to Redis: orchestrator:claude_stream
   │           └─> Adds to Redis Stream: orchestrator:claude_logs_stream
   │
   └─> Builds execution context:
       └─> _build_execution_context(agent, project, task_id, task_context, stream_callback)
           │
           ├─> Creates StateManager instance
           ├─> Gets project_dir from workspace_manager
           ├─> Gets agent_config from ConfigManager
           │
           └─> Returns execution_context:
               {
                   'pipeline_id': f"pipeline_{task_id}_{timestamp}",
                   'task_id': task_id,
                   'agent': agent_name,
                   'project': project_name,
                   'context': task_context,  # Nested
                   'work_dir': str(project_dir),
                   'completed_work': [],
                   'decisions': [],
                   'metrics': {},
                   'validation': {},
                   'state_manager': state_manager,
                   'observability': obs,
                   'stream_callback': stream_callback,
                   'use_docker': task_context.get('use_docker', True),
                   'claude_model': agent_config.model,
                   'agent_config': agent_config.__dict__
               }
```

### Stage 3: Workspace Preparation

```
┌─────────────────────────────────────────────────────────────┐
│ WORKSPACE CONTEXT PREPARATION                                │
└─────────────────────────────────────────────────────────────┘

Workspace preparation (if issue_number present and not skip_workspace_prep)
   │
   ├─> Gets project_config from ConfigManager
   ├─> Creates GitHubIntegration(repo_owner, repo_name)
   ├─> Adds 'agent_name' to task_context (for hybrid routing)
   │
   ├─> workspace_context = WorkspaceContextFactory.create(
   │       workspace_type, project, issue_number, task_context, github_integration
   │   )
   │
   └─> prep_result = await workspace_context.prepare_execution()

       === If IssuesWorkspaceContext ===
       │
       ├─> feature_branch_manager.get_or_create_branch(issue_number, task_context)
       │   │
       │   ├─> Detects parent issue:
       │   │   ├─> GitHub API: GET /repos/{owner}/{repo}/issues/{issue_number}
       │   │   ├─> Searches body for "Parent Issue: #{parent_num}"
       │   │   └─> parent_issue [Optional[int]]
       │   │
       │   ├─> Searches for existing branch:
       │   │   ├─> git branch -r
       │   │   ├─> Matches patterns:
       │   │   │   ├─ feature/issue-{number}
       │   │   │   └─ feature/issue-{parent}/sub-{number}
       │   │   ├─> Calculates confidence based on:
       │   │   │   ├─ Exact issue number match: 1.0
       │   │   │   ├─ Parent/sub match: 0.9
       │   │   │   └─ Partial match: 0.5-0.7
       │   │   └─> existing_branch [Optional[Dict]]
       │   │
       │   ├─> If existing_branch and confidence >= 0.8:
       │   │   ├─> Emits decision event: emit_branch_reused()
       │   │   └─> Returns {branch_name, was_reused: True, confidence}
       │   │
       │   └─> Else:
       │       ├─> Generates branch name:
       │       │   ├─ If parent_issue:
       │       │   │   └─ f"feature/issue-{parent_issue}/sub-{issue_number}"
       │       │   └─ Else:
       │       │       └─ f"feature/issue-{issue_number}"
       │       ├─> git checkout -b {branch_name}
       │       ├─> Emits decision event: emit_branch_created()
       │       └─> Returns {branch_name, was_reused: False, is_standalone}
       │
       ├─> git_workflow_manager.checkout_branch(branch_name)
       │
       ├─> Sync with main:
       │   ├─> git fetch origin main
       │   ├─> commits_behind = git rev-list --count HEAD..origin/main
       │   ├─> If commits_behind > 0:
       │   │   ├─> git pull --rebase origin main
       │   │   ├─> If conflicts:
       │   │   │   ├─> git rebase --abort
       │   │   │   ├─> Emits: emit_branch_conflict_detected()
       │   │   │   └─> Returns {has_conflicts: True, conflicting_files}
       │   │   └─> Emits: emit_branch_stale_detected()
       │   │       └─> Returns {commits_behind_main}
       │
       └─> Returns prep_result:
           {
               'branch_name': str,
               'parent_issue': Optional[int],
               'is_standalone': bool,
               'was_reused': bool,
               'branch_confidence': float,
               'commits_behind_main': int,
               'has_conflicts': bool,
               'conflicting_files': List[str]
           }

       === If DiscussionsWorkspaceContext ===
       │
       ├─> discussion_id = task_context.get('discussion_id')
       ├─> If not discussion_id:
       │   ├─> Creates discussion via GitHub API:
       │   │   └─> GraphQL mutation CreateDiscussion
       │   └─> discussion_id = created_discussion.id
       │
       └─> Returns prep_result:
           {
               'discussion_id': str,
               'discussion_url': str,
               'category_id': str
           }

task_context.update(prep_result)  # Merge preparation results
```

### Stage 4: Agent Instance Creation & Initialization

```
┌─────────────────────────────────────────────────────────────┐
│ AGENT PIPELINE STAGE CREATION                                │
└─────────────────────────────────────────────────────────────┘

agent_stage = pipeline_factory.create_agent(agent_name, project_name)
   │
   ├─> Gets agent_config from ConfigManager
   ├─> Gets agent_class from AGENT_REGISTRY
   ├─> Instantiates: agent_instance = agent_class(agent_config)
   └─> Wraps in AgentStage: AgentStage(agent_name, agent_config)

Emit agent_initialized event
   │
   ├─> Generates container_name (if use_docker):
   │   ├─ raw = f"claude-agent-{project}-{task_id}"
   │   └─ sanitized = DockerAgentRunner._sanitize_container_name(raw)
   │
   └─> agent_execution_id = obs.emit_agent_initialized(
           agent, task_id, project, agent_config,
           branch_name, container_name, pipeline_run_id
       )
       └─> Returns UUID for tracking this execution
```

### Stage 5: Agent Execution

```
┌─────────────────────────────────────────────────────────────┐
│ AGENT EXECUTION (MakerAgent)                                 │
└─────────────────────────────────────────────────────────────┘

result = await agent_stage.execute(execution_context)
   │
   └─> MakerAgent.execute(context)
       │
       ├─> Extracts task_context from execution_context['context']
       │
       ├─> Determines execution mode:
       │   └─> _determine_execution_mode(task_context)
       │       ├─> Checks trigger, conversation_mode, thread_history
       │       ├─> If trigger=='feedback_loop' AND conversation_mode=='threaded':
       │       │   └─> mode = 'question'
       │       ├─> Elif trigger in ['review_cycle_revision', 'feedback_loop']:
       │       │   └─> mode = 'revision'
       │       └─> Else:
       │           └─> mode = 'initial'
       │
       ├─> Builds prompt based on mode:
       │   ├─ mode='initial': _build_initial_prompt(task_context)
       │   ├─ mode='question': _build_question_prompt(task_context)
       │   └─ mode='revision': _build_revision_prompt(task_context)
       │
       ├─> Enhances context with agent_config:
       │   └─> enhanced_context = {
       │           **execution_context,
       │           'agent_config': agent_config.__dict__
       │       }
       │
       └─> result = await run_claude_code(prompt, enhanced_context)
           │
           └─> Continue to Stage 6 (Claude Execution)
```

### Stage 6: Claude Code Execution

```
┌─────────────────────────────────────────────────────────────┐
│ CLAUDE CODE INTEGRATION                                      │
└─────────────────────────────────────────────────────────────┘

run_claude_code(prompt, context)
   │
   ├─> Extracts from context:
   │   ├─ obs = context['observability']
   │   ├─ task_id = context['task_id']
   │   ├─ agent = context['agent']
   │   ├─ project = context['project']
   │   ├─ mcp_servers = context.get('mcp_servers', [])
   │   ├─ agent_config = context.get('agent_config')
   │   └─ use_docker = context.get('use_docker', True)
   │
   ├─> Emits: obs.emit_prompt_constructed(agent, task_id, project, prompt)
   │
   ├─> Determines execution path:
   │   ├─ If use_docker:
   │   │   └─> Docker path (Stage 6A)
   │   └─ Else:
   │       └─> Local path (Stage 6B)

       === STAGE 6A: Docker Execution Path ===
       │
       ├─> project_dir = workspace_manager.get_project_dir(project)
       │
       └─> docker_runner.run_agent_in_container(
               prompt, context, project_dir, mcp_servers, stream_callback
           )
           │
           ├─> Checks dev container status:
           │   ├─> dev_container_state.get_status(project)
           │   └─> If not VERIFIED: Raises exception
           │
           ├─> Builds Docker command:
           │   │
           │   ├─> Container configuration:
           │   │   ├─ image: f"{project}-agent:latest"
           │   │   ├─ name: f"claude-agent-{project}-{task_id}"
           │   │   ├─ working_dir: "/workspace"
           │   │   ├─ volumes:
           │   │   │   ├─ {project_dir}:/workspace
           │   │   │   ├─ ~/.ssh/id_ed25519:/home/orchestrator/.ssh/id_ed25519:ro
           │   │   │   ├─ ~/.gitconfig:/home/orchestrator/.gitconfig:ro
           │   │   │   └─ {project_dir}/.mcp.json:/workspace/.mcp.json
           │   │   ├─ env:
           │   │   │   ├─ CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY
           │   │   │   ├─ CONTEXT7_API_KEY (if present)
           │   │   │   └─ HOME=/home/orchestrator
           │   │   ├─ network: orchestrator_default
           │   │   └─ user: orchestrator (UID 1000)
           │   │
           │   └─> Claude CLI command inside container:
           │       [
           │           'claude',
           │           '--print',
           │           '--verbose',
           │           '--output-format', 'stream-json',
           │           '--model', claude_model,
           │           '--permission-mode', 'bypassPermissions',
           │           '--resume', session_id,  # If continuing session
           │           prompt
           │       ]
           │
           ├─> Creates .mcp.json in project_dir:
           │   └─> { "mcpServers": { ... } }
           │
           ├─> Tracks container in Redis:
           │   └─> redis.set(f"agent_container:{container_name}", json, ex=7200)
           │
           ├─> Emits: obs.emit_claude_call_started()
           │
           ├─> Starts container:
           │   └─> docker run <config> {image} {command}
           │
           ├─> Streams container output:
           │   └─> For each log line from docker:
           │       ├─> Parses JSON event
           │       ├─> Calls stream_callback(event)
           │       │   └─> Publishes to Redis: orchestrator:claude_stream
           │       ├─> Collects assistant text for result
           │       ├─> Tracks token usage
           │       └─> Captures session_id
           │
           ├─> Waits for container exit
           │
           ├─> Cleans up:
           │   ├─> docker rm {container_name}
           │   └─> redis.delete(f"agent_container:{container_name}")
           │
           ├─> Emits: obs.emit_claude_call_completed()
           │
           └─> Returns result_text (string)

       === STAGE 6B: Local Execution Path ===
       │   (dev_environment_setup agent only)
       │
       ├─> work_dir = Path(context['work_dir'])
       ├─> Creates .mcp.json in work_dir
       ├─> Builds Claude CLI command (same as Docker)
       ├─> Emits: obs.emit_claude_call_started()
       ├─> Executes: subprocess.Popen(['claude', ...])
       ├─> Streams stdout line-by-line:
       │   └─> For each line:
       │       ├─> Parses JSON event
       │       ├─> Calls stream_callback(event)
       │       ├─> Collects assistant text
       │       └─> Tracks session_id, tokens
       ├─> Waits for process completion
       ├─> Emits: obs.emit_claude_call_completed()
       └─> Returns result_text (string)
```

### Stage 7: Result Processing & GitHub Posting

```
┌─────────────────────────────────────────────────────────────┐
│ RESULT PROCESSING                                            │
└─────────────────────────────────────────────────────────────┘

Back in MakerAgent.execute():
   │
   ├─> Processes Claude result:
   │   ├─ If result is dict:
   │   │   ├─> analysis_text = result['result']
   │   │   ├─> session_id = result['session_id']
   │   │   └─> context['claude_session_id'] = session_id
   │   └─ Else:
   │       └─> analysis_text = str(result)
   │
   ├─> Stores in context:
   │   ├─ context['markdown_analysis'] = analysis_text
   │   ├─ context['raw_analysis_result'] = analysis_text
   │   └─ context[f'{agent_name}_analysis'] = {'full_markdown': analysis_text}
   │
   └─> Returns context (with result embedded)

Back in AgentExecutor.execute_agent():
   │
   ├─> Extracts output:
   │   └─> output_text = result.get('markdown_analysis') or result.get('raw_analysis_result')
   │
   ├─> Emits: obs.emit_agent_completed(agent, task_id, project, duration_ms,
   │           success=True, output=output_text, agent_execution_id)
   │
   ├─> Posts to GitHub:
   │   └─> _post_agent_output_to_github(agent_name, task_context, result)
   │       │
   │       ├─> Extracts markdown_output from result
   │       ├─> Formats comment:
   │       │   └─> AgentCommentFormatter.format_agent_completion(agent, output)
   │       │       └─> Returns: "<details><summary>Agent Output</summary>\n\n{output}</details>"
   │       │
   │       ├─> Gets reply_to_id from task_context (for threading)
   │       │
   │       ├─> github.post_agent_output(task_context, comment, reply_to_id)
   │       │   │
   │       │   ├─ If workspace_type == 'discussions':
   │       │   │   └─> github.add_discussion_comment(discussion_id, comment, reply_to_id)
   │       │   └─ Else:
   │       │       └─> github.create_comment(issue_number, comment, reply_to_id)
   │       │
   │       └─> Tracks comment timestamp:
   │           └─> feedback_manager.set_last_agent_comment_time(issue_number, agent, timestamp)
```

### Stage 8: Workspace Finalization

```
┌─────────────────────────────────────────────────────────────┐
│ WORKSPACE FINALIZATION                                       │
└─────────────────────────────────────────────────────────────┘

workspace_context.finalize_execution(result, commit_message)

   === If IssuesWorkspaceContext ===
   │
   ├─> Checks agent config:
   │   └─> If agent_config.makes_code_changes:
   │       │
   │       ├─> auto_commit.commit_changes(branch_name, commit_message)
   │       │   ├─> git add .
   │       │   ├─> git commit -m "{commit_message}\n\n🤖 Generated with Claude Code\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
   │       │   └─> commit_sha = git rev-parse HEAD
   │       │
   │       ├─> git_workflow_manager.push_changes(branch_name)
   │       │   └─> git push -u origin {branch_name}
   │       │
   │       └─> Returns {
   │               'success': True,
   │               'branch': branch_name,
   │               'commit_sha': commit_sha,
   │               'pushed': True
   │           }
   │
   └─> Else:
       └─> Returns {'success': True, 'branch': branch_name}

   === If DiscussionsWorkspaceContext ===
   │
   └─> Returns {'success': True, 'discussion_id': discussion_id}
       (No git operations needed, output already posted)
```

### Stage 9: Execution State Recording

```
┌─────────────────────────────────────────────────────────────┐
│ STATE RECORDING                                              │
└─────────────────────────────────────────────────────────────┘

work_execution_tracker.record_execution_outcome(
    issue_number, column, agent, outcome='success', project_name
)
   │
   ├─> Redis key: execution_state:{project}:{issue_number}:{column}:{agent}
   ├─> Value: {'status': 'completed', 'completed_at': timestamp}
   └─> TTL: 2 hours
```

**Information at End of Flow**:

```python
# Agent execution complete with:
{
    # Output posted to GitHub
    'comment_id': 'IC_kwDOABCDEF01',
    'comment_url': 'https://github.com/org/repo/issues/123#issuecomment-...',

    # Git state (if issues workspace)
    'branch': 'feature/issue-123',
    'commit_sha': 'abc123...',
    'pushed': True,

    # Or discussion state (if discussions workspace)
    'discussion_id': 'D_kwDOABCDEF01',

    # Observability events emitted:
    # - task_received
    # - agent_initialized
    # - prompt_constructed
    # - claude_call_started
    # - claude_call_completed
    # - agent_completed
    # - branch_selected/created
    # Plus 100+ Claude stream events via stream_callback

    # Execution state recorded in Redis
    'execution_state': 'completed',

    # Session continuity preserved
    'claude_session_id': 'session_abc123'
}
```

---

## Flow 4: Review Cycle Flow

**Trigger**: Review-required stage completes

### Review Cycle Information Flow

```
┌─────────────────────────────────────────────────────────────┐
│ MAKER-CHECKER REVIEW CYCLE                                   │
└─────────────────────────────────────────────────────────────┘

Stage configuration: {review_required: True, reviewer_agent: "code_reviewer"}

Iteration 1:
   │
   ├─> Maker agent completes (Flow 3)
   │   └─> Output posted to GitHub
   │
   ├─> Pipeline checks stage.review_required == True
   │
   ├─> Emits: decision_events.emit_review_cycle_decision(
   │       issue_number, project, board, 1, 'reviewer_selected', maker, reviewer, reason
   │   )
   │
   ├─> Queues reviewer task:
   │   └─> Task(
   │           agent='code_reviewer',
   │           context={
   │               'issue': {issue data},
   │               'previous_stage_output': maker_output,
   │               'trigger': 'review',
   │               'review_cycle': {
   │                   'iteration': 1,
   │                   'max_iterations': 3,
   │                   'maker_agent': 'senior_software_engineer'
   │               }
   │           }
   │       )
   │
   ├─> Reviewer executes (Flow 3 with review context)
   │   └─> Reviewer output format:
   │       ```
   │       ## Issues
   │       1. [Missing error handling]: Add try/catch blocks
   │       2. [Inconsistent naming]: Rename variables to camelCase
   │
   │       ## Approval
   │       [CHANGES REQUESTED]
   │       ```
   │
   ├─> review_parser.parse_review_output(reviewer_output)
   │   └─> Returns:
   │       {
   │           'approved': False,
   │           'issues': [
   │               {'title': 'Missing error handling', 'description': '...'},
   │               {'title': 'Inconsistent naming', 'description': '...'}
   │           ]
   │       }
   │
   └─> If not approved:
       │
       ├─> Increments iteration: iteration = 2
       │
       ├─> Emits: decision_events.emit_review_cycle_decision(
       │       issue_number, project, board, 2, 'maker_selected', maker, reviewer, reason
       │   )
       │
       └─> Queues revision task:
           └─> Task(
                   agent='senior_software_engineer',
                   context={
                       'issue': {issue data},
                       'trigger': 'review_cycle_revision',
                       'revision': {
                           'previous_output': maker_output,
                           'feedback': reviewer_output
                       },
                       'review_cycle': {
                           'iteration': 2,
                           'max_iterations': 3,
                           'reviewer_agent': 'code_reviewer'
                       }
                   }
               )

Iteration 2:
   │
   ├─> Maker agent executes in REVISION mode
   │   └─> Prompt includes:
   │       - Previous output
   │       - Feedback from reviewer
   │       - Instructions to address each issue
   │
   ├─> Maker produces revised output
   │   └─> Must start with:
   │       ```
   │       ## Revision Notes
   │       - ✅ [Missing error handling]: Added try/catch blocks in functions X, Y
   │       - ✅ [Inconsistent naming]: Renamed all variables to camelCase
   │       ```
   │
   ├─> Reviewer executes again
   │   └─> Reviews revised output
   │
   └─> If approved:
       │
       ├─> Emits: decision_events.emit_review_cycle_decision(
       │       issue_number, project, board, 2, 'complete', maker, reviewer, 'Approved'
       │   )
       │
       └─> Checks column.auto_advance_on_approval:
           └─> If True:
               ├─> Determines next column
               ├─> Moves card via GitHub API
               └─> Emits: decision_events.emit_status_progression()

If iteration == max_iterations AND not approved:
   │
   ├─> Emits: decision_events.emit_review_cycle_decision(
   │       issue_number, project, board, 3, 'escalate', maker, reviewer, 'Max iterations'
   │   )
   │
   ├─> Adds label to issue: "needs-human-review"
   │
   └─> Posts escalation comment to GitHub:
       "⚠️ Review cycle reached maximum iterations. Human review required."
```

---

## Flow 5: Conversational Loop Flow

**Trigger**: Human posts question on agent output

### Conversational Information Flow

```
┌─────────────────────────────────────────────────────────────┐
│ THREADED Q&A CONVERSATION                                    │
└─────────────────────────────────────────────────────────────┘

Setup:
   │
   ├─> Agent completes work (Flow 3)
   │   └─> Output posted to GitHub (comment_id = "IC_abc123")
   │
   └─> conversational_session_state.start_session(
           project, issue_number, agent, board, column
       )
       └─> Redis: conversation_session:{project}:{issue_number}
           Value: {
               'agent': agent_name,
               'board': board_name,
               'column': column_name,
               'started_at': timestamp,
               'status': 'active',
               'thread_history': [],
               'last_agent_comment_id': 'IC_abc123',
               'initial_column': column_name
           }

Human Interaction:
   │
   ├─> Human posts threaded reply: "Can you explain section 2 in more detail?"
   │   └─> comment_id = "IC_def456", in_reply_to_id = "IC_abc123"
   │
   ├─> ProjectMonitor.detect_feedback()
   │   │
   │   ├─> Checks conversation_session active
   │   ├─> Detects new comment after last_agent_comment_id
   │   ├─> Identifies as feedback (is_question=True)
   │   │
   │   └─> Emits: decision_events.emit_feedback_detected(
   │           issue_number, project, board, 'comment', question_text,
   │           agent, 'queue_agent_task'
   │       )
   │
   ├─> feedback_manager.route_feedback()
   │   │
   │   ├─> Gets conversation_session from Redis
   │   ├─> Extracts thread_history
   │   ├─> Builds context:
   │   │   {
   │   │       'trigger': 'feedback_loop',
   │   │       'conversation_mode': 'threaded',
   │   │       'thread_history': [
   │   │           {
   │   │               'role': 'agent',
   │   │               'author': agent_name,
   │   │               'body': previous_agent_output,
   │   │               'timestamp': '...'
   │   │           },
   │   │           {
   │   │               'role': 'human',
   │   │               'author': 'username',
   │   │               'body': 'Can you explain section 2...',
   │   │               'timestamp': '...'
   │   │           }
   │   │       ],
   │   │       'feedback': {
   │   │           'formatted_text': 'Can you explain section 2...',
   │   │           'author': 'username',
   │   │           'timestamp': '...'
   │   │       },
   │   │       'reply_to_comment_id': 'IC_def456'
   │   │   }
   │   │
   │   └─> Queues task:
   │       └─> Task(
   │               agent=agent_name,
   │               context={
   │                   'issue': {issue data},
   │                   'trigger': 'feedback_loop',
   │                   'conversation_mode': 'threaded',
   │                   'thread_history': [...],
   │                   'feedback': {...},
   │                   'reply_to_comment_id': 'IC_def456'
   │               }
   │           )
   │
   └─> conversational_session_state.update_session(
           project, issue_number, {'status': 'active', 'last_interaction': timestamp}
       )

Agent Response:
   │
   ├─> Agent executes in QUESTION mode (Flow 3)
   │   │
   │   ├─> MakerAgent._determine_execution_mode() detects:
   │   │   ├─ trigger == 'feedback_loop'
   │   │   ├─ conversation_mode == 'threaded'
   │   │   └─ thread_history exists
   │   │   └─> Returns 'question'
   │   │
   │   ├─> _build_question_prompt() constructs conversational prompt:
   │   │   └─> Includes full thread history
   │   │       └─> Formatted as natural conversation
   │   │
   │   └─> Claude produces conversational answer (200-500 words)
   │
   ├─> Output posted as threaded reply:
   │   └─> reply_to_id = 'IC_def456' (human's question)
   │       └─> Creates comment_id = "IC_ghi789"
   │
   ├─> conversational_session_state.update_session(
   │       project, issue_number, {
   │           'thread_history': [
   │               ... previous messages ...,
   │               {
   │                   'role': 'agent',
   │                   'author': agent_name,
   │                   'body': agent_answer,
   │                   'timestamp': '...'
   │               }
   │           ],
   │           'last_agent_comment_id': 'IC_ghi789',
   │           'turn_count': 2
   │       }
   │   )
   │
   └─> Emits: decision_events.emit_conversational_question_routed(
           issue_number, project, board, question, agent, reason
       )

Conversation Continuation:
   │
   └─> Human can ask follow-up questions
       └─> Process repeats with updated thread_history
       └─> Each iteration adds to thread_history

Column Exit Detection:
   │
   ├─> ProjectMonitor detects card moved to different column
   │
   ├─> conversational_session_state.get_active_session(project, issue_number)
   │   └─> Checks current_column != initial_column
   │
   ├─> If column changed:
   │   │
   │   ├─> Emits: decision_events.emit_conversational_loop_paused(
   │   │       issue_number, project, board, 'Column changed'
   │   │   )
   │   │
   │   └─> conversational_session_state.end_session(
   │           project, issue_number, reason='column_exit'
   │       )
   │       └─> Redis key deleted
   │
   └─> New agent assigned for new column (Flow 3)
```

---

## Flow 6: Repair Cycle Flow

**Trigger**: Repair cycle stage execution

### Repair Cycle Information Flow

```
┌─────────────────────────────────────────────────────────────┐
│ TEST-DRIVEN REPAIR CYCLE                                     │
└─────────────────────────────────────────────────────────────┘

Configuration:
   pipeline_template.stages[X]:
       stage_type: 'repair_cycle'
       max_total_agent_calls: 100
       checkpoint_interval: 5

   project_config.testing:
       types:
           - type: 'unit'
             command: 'pytest tests/unit/'
             timeout: 300
             max_iterations: 5
             review_warnings: true

Initialization:
   │
   ├─> RepairCycleStage.execute(context)
   │   │
   │   ├─> Extracts issue_number, project from context
   │   ├─> Generates run_id: f"repair_{project}_{issue_number}_{timestamp}"
   │   │
   │   ├─> Emits: decision_events.emit_repair_cycle_decision(
   │   │       issue_number, project, board, 0, 'start', agent, 'Starting repair cycle'
   │   │   )
   │   │
   │   └─> repair_cycle_runner.run_repair_cycle_in_container(
   │           project, issue_number, test_configs, agent_name, context
   │       )

Container Setup:
   │
   ├─> Checks for existing repair cycle container:
   │   └─> redis.get(f"repair_cycle:{project}:{issue_number}")
   │
   ├─> If exists:
   │   │
   │   ├─> Loads checkpoint:
   │   │   └─> checkpoint = json.loads(redis_value)
   │   │       {
   │   │           'iteration': 2,
   │   │           'test_type': 'unit',
   │   │           'agent_call_count': 8,
   │   │           'files_fixed': ['file1.py', 'file2.py'],
   │   │           'test_failures': [...],
   │   │           'timestamp': '...'
   │   │       }
   │   │
   │   ├─> Emits: obs.emit_repair_cycle_container_recovered(
   │   │       project, issue_number, container_name, checkpoint
   │   │   )
   │   │
   │   └─> Resumes from checkpoint
   │
   └─> Else:
       │
       ├─> Creates container:
       │   └─> docker run -d \
       │           -v {project_dir}:/workspace \
       │           --name repair-{project}-{issue_number} \
       │           {project}-agent:latest \
       │           tail -f /dev/null
       │
       ├─> Stores in Redis:
       │   └─> redis.set(
       │           f"repair_cycle:{project}:{issue_number}",
       │           json.dumps({
       │               'container_name': container_name,
       │               'run_id': run_id,
       │               'started_at': timestamp,
       │               'iteration': 0,
       │               'agent_call_count': 0
       │           }),
       │           ex=7200
       │       )
       │
       └─> Emits: obs.emit_repair_cycle_container_started(
               project, issue_number, container_name, run_id
           )

Test-Fix Iteration Loop:
   │
   └─> For each test_type in test_configs:
       │
       ├─> iteration = 1
       │
       └─> While True:
           │
           ├─> Emits: obs.emit_repair_cycle_iteration(
           │       project, issue_number, iteration, test_type, agent_call_count
           │   )
           │
           ├─> Execute tests in container:
           │   └─> docker exec {container} {test_command}
           │       │
           │       ├─> Streams output via websocket
           │       │
           │       └─> result = {
           │               'success': bool,
           │               'failures': List[{
           │                   'file': str,
           │                   'function': str,
           │                   'error': str,
           │                   'traceback': str
           │               }],
           │               'warnings': List[{...}],
           │               'duration': float
           │           }
           │
           ├─> Emits: obs.emit_repair_cycle_test_execution_completed(
           │       project, issue_number, test_type, success, failures_count
           │   )
           │
           ├─> If result.success:
           │   └─> BREAK (tests pass, move to next test type)
           │
           ├─> If iteration >= max_iterations:
           │   └─> BREAK (max iterations reached)
           │
           ├─> If agent_call_count >= max_total_agent_calls:
           │   └─> BREAK (circuit breaker)
           │
           ├─> Groups failures by file:
           │   └─> files_to_fix = {
           │           'file1.py': [failure1, failure2],
           │           'file2.py': [failure3]
           │       }
           │
           └─> For each file in files_to_fix:
               │
               ├─> file_iteration = 1
               │
               └─> While file_iteration <= max_file_iterations:
                   │
                   ├─> Emits: obs.emit_repair_cycle_file_fix_started(
                   │       project, issue_number, file, failures
                   │   )
                   │
                   ├─> Builds repair prompt:
                   │   ```
                   │   Fix the following test failures in {file}:
                   │
                   │   Failure 1:
                   │   {failure.error}
                   │   {failure.traceback}
                   │
                   │   Failure 2:
                   │   ...
                   │
                   │   Previous fix attempts: {file_iteration - 1}
                   │   ```
                   │
                   ├─> Executes agent in container:
                   │   └─> docker exec {container} claude --project /workspace "{prompt}"
                   │       └─> Agent modifies file to fix failures
                   │
                   ├─> Increments agent_call_count
                   │
                   ├─> Re-runs tests for this file:
                   │   └─> docker exec {container} pytest {file}
                   │       └─> file_result = {success, failures}
                   │
                   ├─> If file_result.success:
                   │   │
                   │   ├─> Emits: obs.emit_repair_cycle_file_fix_completed(
                   │   │       project, issue_number, file, file_iteration
                   │   │   )
                   │   │
                   │   └─> BREAK (file fixed)
                   │
                   ├─> Else:
                   │   └─> Increments file_iteration
                   │
                   └─> Checkpoint every N iterations:
                       └─> If agent_call_count % checkpoint_interval == 0:
                           │
                           ├─> Updates checkpoint:
                           │   └─> redis.set(
                           │           f"repair_cycle:{project}:{issue_number}",
                           │           json.dumps({
                           │               'iteration': iteration,
                           │               'test_type': test_type.value,
                           │               'agent_call_count': agent_call_count,
                           │               'files_fixed': files_fixed,
                           │               'timestamp': timestamp
                           │           })
                           │       )
                           │
                           └─> Emits: obs.emit_repair_cycle_container_checkpoint_updated(
                                   project, issue_number, container_name, checkpoint
                               )

Warning Review (if enabled):
   │
   └─> If test_config.review_warnings AND result.warnings:
       │
       ├─> Emits: obs.emit_repair_cycle_warning_review_started(
       │       project, issue_number, warnings_count
       │   )
       │
       ├─> Builds review prompt:
       │   ```
       │   Review the following test warnings and assess criticality:
       │
       │   Warning 1: {warning.message}
       │   Warning 2: ...
       │
       │   For each warning, determine:
       │   - Severity: LOW | MEDIUM | HIGH | CRITICAL
       │   - Should fix: YES | NO
       │   - Reason: ...
       │   ```
       │
       ├─> Executes agent in container
       │   └─> Agent assesses warnings
       │
       ├─> Parses assessment
       │
       └─> For each warning where should_fix==YES:
           └─> Adds to files_to_fix for next iteration

Completion:
   │
   ├─> All test types pass or max iterations reached
   │
   ├─> Cleans up container:
   │   ├─> docker stop {container}
   │   ├─> docker rm {container}
   │   └─> redis.delete(f"repair_cycle:{project}:{issue_number}")
   │
   ├─> Emits: obs.emit_repair_cycle_container_completed(
   │       project, issue_number, container_name, success, agent_call_count, duration
   │   )
   │
   ├─> Emits: obs.emit_repair_cycle_completed(
   │       project, issue_number, success, total_iterations, total_agent_calls
   │   )
   │
   └─> Returns result:
       {
           'success': bool,
           'iterations': int,
           'agent_calls': int,
           'test_results': Dict[str, TestResult],
           'files_modified': List[str]
       }
```

---

## Summary: Critical Information Flows

### 1. Configuration Flow
```
YAML files → ConfigManager → Agent/Pipeline/Workflow configs → All components
```

### 2. Task Flow
```
GitHub board change → ProjectMonitor → TaskQueue → process_task_integrated → AgentExecutor → Agent
```

### 3. Execution Context Flow
```
Task.context → execution_context → agent prompt → Claude → result → GitHub comment
```

### 4. Observability Flow
```
Every component → ObservabilityManager → Redis (pub/sub + stream) → Elasticsearch → Web UI
```

### 5. Workspace Flow
```
Task → WorkspaceContext.prepare → Git/Discussion ops → Agent execution → WorkspaceContext.finalize
```

### 6. State Persistence Flow
```
Runtime state → Redis (TTL 2hr) → Survives restarts via recovery logic
Configuration state → YAML files → StateManager → Reconciliation
Checkpoints → StateManager → File system → Recovery on failure
```

This completes the comprehensive information flow documentation for the Claude Code Agent Orchestrator system.
