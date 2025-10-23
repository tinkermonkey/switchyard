# Component Interfaces - Information Exchange Patterns

This document provides a deep dive into the interfaces between components, focusing on **information exchange** rather than implementation details.

## Interface Categories

The system uses several interface patterns:
1. **Function Call Interfaces**: Direct function invocations with parameter passing
2. **Context Dictionaries**: Shared mutable state passed through execution chains
3. **Event Streaming**: Publish/subscribe patterns via Redis
4. **Configuration Interfaces**: YAML-based configuration consumption
5. **State Persistence Interfaces**: File and Redis-based state exchange
6. **External API Interfaces**: GitHub, Claude, Docker APIs

---

## 1. Core Orchestration Interfaces

### 1.1 Main Orchestrator → Task Queue
**Interface Type**: Function Call + Redis

**Information Flow**: Main Loop → Task Queue

**Input**:
```python
# From queue
task = task_queue.dequeue()  # TaskQueue.dequeue() -> Task | None
```

**Output (Task Structure)**:
```python
@dataclass
class Task:
    id: str                    # Unique task identifier
    agent: str                 # Agent name to execute
    project: str               # Project name
    priority: TaskPriority     # LOW, MEDIUM, HIGH, CRITICAL
    context: Dict[str, Any]    # Task-specific context (see below)
    created_at: str            # ISO 8601 timestamp
```

**Task Context Structure** (key-value pairs):
```python
{
    'issue': {
        'number': int,
        'title': str,
        'body': str,
        'labels': List[str],
        'state': str
    },
    'issue_number': int,
    'board': str,               # Board/pipeline name
    'column': str,              # Current column name
    'repository': str,          # Repo name
    'project': str,             # Project name
    'workspace_type': str,      # 'issues' | 'discussions' | 'hybrid'
    'discussion_id': str,       # Optional: if workspace_type='discussions'
    'trigger': str,             # 'card_movement' | 'feedback_loop' | 'review_cycle_revision'
    'use_docker': bool,         # Whether to use Docker
    'previous_stage_output': str,  # Optional: output from previous stage
    'feedback': Dict[str, Any], # Optional: feedback data
    'revision': Dict[str, Any], # Optional: revision data
    'review_cycle': Dict[str, Any],  # Optional: review cycle data
    'thread_history': List[Dict],    # Optional: conversation history
    'conversation_mode': str,   # Optional: 'threaded'
    'pipeline_run_id': str,     # Optional: for traceability
}
```

### 1.2 Main Orchestrator → process_task_integrated()
**Interface Type**: Function Call

**Function Signature**:
```python
async def process_task_integrated(
    task: Task,
    state_manager: StateManager,
    logger: OrchestratorLogger
) -> Dict[str, Any]
```

**Information Flow**:
- **Input**: Task object (from queue), StateManager, Logger
- **Output**: Result dictionary with agent output

**Output Structure**:
```python
{
    'markdown_analysis': str,      # Agent output (markdown)
    'raw_analysis_result': str,    # Same as markdown_analysis
    'completed_work': List[str],   # List of completed work items
    'context': Dict[str, Any],     # Updated context
    'claude_session_id': str,      # Optional: for session continuity
    '{agent}_analysis': Dict[str, Any],  # Agent-specific output
}
```

---

## 2. Agent Execution Interfaces

### 2.1 process_task_integrated() → AgentExecutor
**Interface Type**: Function Call

**Function Signature**:
```python
async def execute_agent(
    agent_name: str,
    project_name: str,
    task_context: Dict[str, Any],
    task_id_prefix: str = "task"
) -> Dict[str, Any]
```

**Information Exchange**:

**Input** (task_context):
```python
{
    # Core identification
    'issue_number': int,
    'project': str,
    'board': str,
    'column': str,

    # Issue/Discussion data
    'issue': Dict[str, Any],  # Full issue object
    'discussion_id': str,     # If using discussions

    # Workspace configuration
    'workspace_type': str,    # 'issues' | 'discussions' | 'hybrid'
    'branch_name': str,       # Optional: existing branch

    # Execution mode indicators
    'trigger': str,           # Determines execution mode
    'conversation_mode': str, # 'threaded' for Q&A
    'thread_history': List[Dict],  # Conversation context

    # Previous work
    'previous_stage_output': str,  # From previous stage
    'previous_output': str,        # For revisions

    # Feedback/Revision data
    'feedback': {
        'formatted_text': str,
        'author': str,
        'timestamp': str
    },
    'revision': {
        'previous_output': str,
        'feedback': str
    },

    # Review cycle data
    'review_cycle': {
        'iteration': int,
        'max_iterations': int,
        'reviewer_agent': str
    },

    # Docker/execution control
    'use_docker': bool,
    'skip_workspace_prep': bool,  # For repair cycles

    # Tracing
    'pipeline_run_id': str,
    'agent_name': str  # Added by AgentExecutor
}
```

**Output**:
```python
{
    'markdown_analysis': str,       # Primary agent output
    'raw_analysis_result': str,     # Same content
    '{agent}_analysis': Dict,       # Structured output
    'completed_work': List[str],    # Work items completed
    'context': Dict[str, Any],      # Updated context
    'claude_session_id': str        # Session ID for continuity
}
```

### 2.2 AgentExecutor → WorkspaceContext
**Interface Type**: Function Call

**Function Signature**:
```python
# Factory creation
workspace_context = WorkspaceContextFactory.create(
    workspace_type: str,
    project: str,
    issue_number: int,
    task_context: Dict[str, Any],
    github_integration: GitHubIntegration
) -> WorkspaceContext

# Lifecycle methods
prep_result = await workspace_context.prepare_execution() -> Dict[str, Any]
finalize_result = await workspace_context.finalize_execution(
    result: Dict[str, Any],
    commit_message: str
) -> Dict[str, Any]
```

**Information Exchange - prepare_execution()**:

**Input**: task_context (from AgentExecutor)

**Output** (IssuesWorkspaceContext):
```python
{
    'branch_name': str,           # Feature branch name
    'parent_issue': int,          # Optional: parent issue number
    'is_standalone': bool,        # True if no parent
    'was_reused': bool,           # True if existing branch
    'branch_confidence': float,   # Confidence if reused
    'commits_behind_main': int,   # Staleness check
    'has_conflicts': bool,        # Merge conflict flag
    'conflicting_files': List[str]  # If has_conflicts=True
}
```

**Output** (DiscussionsWorkspaceContext):
```python
{
    'discussion_id': str,         # Discussion ID
    'discussion_url': str,        # GitHub URL
    'category_id': str            # Discussion category
}
```

**Information Exchange - finalize_execution()**:

**Input**:
```python
{
    'result': {
        'markdown_analysis': str,
        # ... agent output
    },
    'commit_message': str
}
```

**Output** (IssuesWorkspaceContext):
```python
{
    'success': bool,
    'branch': str,
    'commit_sha': str,       # Optional: if committed
    'pushed': bool,          # Whether pushed to remote
    'error': str             # Optional: if failure
}
```

**Output** (DiscussionsWorkspaceContext):
```python
{
    'success': bool,
    'discussion_id': str
}
```

### 2.3 AgentExecutor → PipelineStage (Agent)
**Interface Type**: Function Call

**Function Signature**:
```python
result = await agent_stage.execute(context: Dict[str, Any]) -> Dict[str, Any]
```

**Information Exchange - Input (Execution Context)**:
```python
{
    # Pipeline identification
    'pipeline_id': str,           # Unique pipeline run ID
    'task_id': str,               # Task identifier
    'agent': str,                 # Agent name
    'project': str,               # Project name

    # Work directory
    'work_dir': str,              # Absolute path to project dir

    # Nested task context
    'context': {
        # All task_context fields (issue, board, etc.)
        # See section 2.1 for full structure
    },

    # Execution tracking
    'completed_work': List[str],  # Work items from previous stages
    'decisions': List[Dict],      # Decision history
    'metrics': Dict[str, Any],    # Performance metrics
    'validation': Dict[str, Any], # Validation results

    # Infrastructure
    'state_manager': StateManager,  # State persistence
    'observability': ObservabilityManager,  # Event emission
    'stream_callback': Callable,   # Live log streaming

    # Configuration
    'claude_model': str,          # Claude model to use
    'use_docker': bool,           # Docker execution flag

    # Agent-specific config
    'agent_config': {
        'model': str,
        'timeout': int,
        'retries': int,
        'requires_docker': bool,
        'requires_dev_container': bool,
        'makes_code_changes': bool,
        'filesystem_write_allowed': bool,
        'mcp_servers': List[Dict]
    }
}
```

**Information Exchange - Output**:
```python
{
    # Primary output
    'markdown_analysis': str,      # Markdown formatted output
    'raw_analysis_result': str,    # Same as markdown_analysis

    # Structured output
    '{agent}_analysis': {
        'full_markdown': str,
        # Agent-specific structured fields
    },

    # Updated context
    'context': Dict[str, Any],     # Updated with new data
    'completed_work': List[str],   # Appended work items

    # Session continuity
    'claude_session_id': str       # For multi-turn conversations
}
```

### 2.4 PipelineStage (MakerAgent) → run_claude_code()
**Interface Type**: Function Call

**Function Signature**:
```python
result = await run_claude_code(
    prompt: str,
    context: Dict[str, Any]
) -> str  # OR Dict[str, Any] with 'result' and 'session_id'
```

**Information Exchange - Input**:

**Prompt**: String containing Claude Code instructions (see section 4.1 for prompt structure)

**Context**: Same as AgentExecutor → PipelineStage context (section 2.3)

**Information Exchange - Output**:

**String Format** (simple):
```
Markdown formatted agent output text
```

**Dict Format** (with session continuity):
```python
{
    'result': str,         # Markdown formatted output
    'session_id': str      # Claude Code session ID
}
```

### 2.5 run_claude_code() → DockerAgentRunner
**Interface Type**: Function Call

**Function Signature**:
```python
result = await docker_runner.run_agent_in_container(
    prompt: str,
    context: Dict[str, Any],
    project_dir: Path,
    mcp_servers: List[Dict[str, Any]],
    stream_callback: Callable
) -> str
```

**Information Exchange - Input**:

**prompt**: Claude Code instructions (string)

**context**: Execution context (from section 2.3)

**project_dir**: Path object to project directory

**mcp_servers** (MCP Server Configuration):
```python
[
    {
        'name': str,           # Server identifier
        'type': 'http' | 'stdio',

        # For HTTP servers
        'url': str,            # HTTP endpoint

        # For stdio servers
        'command': str,        # Executable path
        'args': List[str],     # Command arguments
        'env': Dict[str, str]  # Environment variables
    },
    # ... more servers
]
```

**stream_callback**: Function for real-time log streaming

**Information Exchange - Output**:
```
Markdown formatted agent output text (string)
```

---

## 3. Observability Interfaces

### 3.1 Any Component → ObservabilityManager
**Interface Type**: Function Call + Redis Pub/Sub

**Event Emission**:
```python
obs.emit(
    event_type: EventType,
    agent: str,
    task_id: str,
    project: str,
    data: Dict[str, Any],
    pipeline_run_id: Optional[str] = None
)
```

**Information Exchange - Input (Event Data)**:

**Structure varies by event type, but common pattern**:
```python
{
    # Decision events
    'decision_category': str,     # 'routing' | 'feedback' | 'progression' | ...
    'issue_number': int,
    'board': str,
    'workspace_type': str,
    'discussion_id': str,

    # Decision structure
    'inputs': Dict[str, Any],     # Inputs to decision
    'decision': Dict[str, Any],   # The decision made
    'reason': str,                # Human-readable explanation
    'reasoning_data': Dict,       # Structured reasoning

    # Agent lifecycle events
    'model': str,
    'timeout': int,
    'branch_name': str,
    'container_name': str,
    'duration_ms': float,
    'success': bool,
    'error': str,
    'output': str,
    'agent_execution_id': str,

    # Context
    'pipeline_run_id': str
}
```

**Information Exchange - Output (to Redis)**:

**ObservabilityEvent Structure**:
```python
{
    'timestamp': str,          # ISO 8601 UTC
    'event_id': str,           # UUID
    'event_type': str,         # EventType enum value
    'agent': str,              # Agent name
    'task_id': str,            # Task identifier
    'project': str,            # Project name
    'data': Dict[str, Any]     # Event-specific data
}
```

**Redis Destinations**:
1. **Pub/Sub**: Channel `orchestrator:agent_events` (real-time)
2. **Stream**: Key `orchestrator:event_stream` (history, 1000 events, 2hr TTL)
3. **Elasticsearch**: Daily indices for decision/agent events

### 3.2 Any Component → DecisionEventEmitter
**Interface Type**: Convenience Wrapper

**Example Method**:
```python
decision_events.emit_agent_routing_decision(
    issue_number: int,
    project: str,
    board: str,
    current_status: str,
    selected_agent: str,
    reason: str,
    alternatives: Optional[List[str]] = None,
    workspace_type: str = "issues",
    discussion_id: Optional[str] = None,
    pipeline_run_id: Optional[str] = None
)
```

**Information Flow**: DecisionEventEmitter → ObservabilityManager.emit()

**Transformation**: High-level parameters → Structured event data dictionary

### 3.3 run_claude_code() → stream_callback
**Interface Type**: Function Call (Callback)

**Callback Signature**:
```python
def stream_callback(event: Dict[str, Any]) -> None
```

**Information Exchange - Input (Claude Code Stream Event)**:
```python
{
    'type': str,  # Event type from Claude Code

    # Common event types and their structures:

    # 'assistant' - Agent response
    'message': {
        'role': 'assistant',
        'content': [
            {
                'type': 'text',
                'text': str
            }
        ]
    },
    'usage': {
        'input_tokens': int,
        'output_tokens': int
    },

    # 'result' - Final result
    'result': {
        'type': 'text',
        'text': str
    },

    # 'error' - Error occurred
    'error': str,
    'message': str,

    # 'progress' - Progress update
    'progress': {
        'percentage': float,
        'message': str
    },

    # 'tool_use' - Tool execution
    'tool': {
        'name': str,
        'input': Dict[str, Any]
    },

    # 'tool_result' - Tool result
    'tool': {
        'name': str,
        'result': str
    },

    # Session continuity
    'session_id': str  # Present in various event types
}
```

**Stream Callback Processing**:
```python
# In AgentExecutor._create_stream_callback()
event_data = {
    'agent': agent_name,
    'task_id': task_id,
    'project': project_name,
    'pipeline_run_id': pipeline_run_id,
    'timestamp': event.get('timestamp') or time.time(),
    'event': event  # Full Claude event
}

# Publish to Redis pub/sub
obs.redis.publish('orchestrator:claude_stream', json.dumps(event_data))

# Add to Redis Stream for history
obs.redis.xadd('orchestrator:claude_logs_stream', {'log': json.dumps(event_data)})
```

---

## 4. Configuration Interfaces

### 4.1 Any Component → ConfigManager
**Interface Type**: Function Call

**Key Methods and Data Structures**:

#### 4.1.1 get_project_config()
```python
project_config = config_manager.get_project_config(project_name: str) -> ProjectConfig
```

**Output (ProjectConfig)**:
```python
{
    'name': str,
    'github': {
        'org': str,
        'repo': str,
        'repo_url': str  # git@github.com:org/repo.git
    },
    'tech_stacks': {
        'backend': str,   # 'python, fastapi'
        'frontend': str,  # 'react, typescript'
        'database': str,  # 'postgresql'
        # ... more
    },
    'pipelines': List[{
        'template': str,      # 'planning_design' | 'sdlc_execution'
        'name': str,          # 'planning' | 'dev'
        'board_name': str,    # 'Planning' | 'Development'
        'workflow': str,      # 'planning_workflow' | 'dev_workflow'
        'workspace': str      # 'issues' | 'discussions' | 'hybrid'
    }],
    'testing': {
        'types': List[{
            'type': str,            # 'unit' | 'integration' | 'e2e' | 'lint'
            'command': str,         # Test execution command
            'timeout': int,         # Timeout in seconds
            'max_iterations': int,  # Max repair iterations
            'review_warnings': bool # Review warnings?
        }]
    },
    'branch_naming': {
        'feature_prefix': str,    # 'feature/'
        'sub_issue_format': str   # 'feature/issue-{parent}/sub-{sub_number}'
    }
}
```

#### 4.1.2 get_project_agent_config()
```python
agent_config = config_manager.get_project_agent_config(
    project_name: str,
    agent_name: str
) -> AgentConfig
```

**Output (AgentConfig)**:
```python
{
    'name': str,                      # Agent identifier
    'model': str,                     # 'claude-sonnet-4-5-20250929'
    'timeout': int,                   # Seconds
    'retries': int,                   # Retry count
    'requires_docker': bool,          # Must run in Docker?
    'requires_dev_container': bool,   # Needs project dependencies?
    'makes_code_changes': bool,       # Modifies files?
    'filesystem_write_allowed': bool, # Can write files?
    'mcp_servers': List[str],         # MCP server names
    'tools_enabled': bool             # Enable Claude tools?
}
```

#### 4.1.3 get_pipeline_template()
```python
pipeline_template = config_manager.get_pipeline_template(
    template_name: str
) -> PipelineTemplate
```

**Output (PipelineTemplate)**:
```python
{
    'name': str,              # Template name
    'description': str,       # Template description
    'stages': List[{
        'stage': str,           # Stage identifier
        'name': str,            # Human-readable name
        'default_agent': str,   # Agent for this stage
        'review_required': bool, # Requires review?
        'reviewer_agent': str,  # Optional: reviewer
        'max_iterations': int,  # Max review iterations
        'stage_type': str,      # 'agent' | 'repair_cycle'

        # For repair_cycle stages
        'max_total_agent_calls': int,
        'checkpoint_interval': int,

        # Dependencies
        'depends_on': List[str] # Previous stage names
    }]
}
```

#### 4.1.4 get_workflow_template()
```python
workflow_template = config_manager.get_workflow_template(
    workflow_name: str
) -> WorkflowTemplate
```

**Output (WorkflowTemplate)**:
```python
{
    'name': str,
    'description': str,
    'columns': List[{
        'name': str,                     # Column name
        'position': int,                 # Sort order
        'agent': str,                    # Agent for this column
        'auto_advance_on_approval': bool, # Auto-move after approval?
        'discussion_category': str       # Optional: for discussions workspace
    }]
}
```

### 4.2 Any Component → StateManager (GitHub State)
**Interface Type**: Function Call

**Key Methods**:

#### 4.2.1 save_github_state()
```python
state_manager.save_github_state(
    project_name: str,
    state: Dict[str, Any]
)
```

**Input (GitHub State)**:
```python
{
    'project_id': str,           # GitHub GraphQL project ID
    'project_number': int,       # GitHub project number
    'last_config_hash': str,     # Config file hash
    'last_synchronized': str,    # ISO 8601 timestamp
    'columns': Dict[str, str],   # column_name -> column_id
    'boards': {
        'board_name': {
            'project_id': str,
            'project_number': int,
            'columns': Dict[str, str]
        }
    }
}
```

#### 4.2.2 load_github_state()
```python
state = state_manager.load_github_state(project_name: str) -> Dict[str, Any]
```

**Output**: Same structure as save_github_state input

#### 4.2.3 needs_reconciliation()
```python
needs_reconcile = state_manager.needs_reconciliation(project_name: str) -> bool
```

**Logic**: Compares current config hash with last_config_hash

---

## 5. GitHub Integration Interfaces

### 5.1 Any Service → GitHubIntegration
**Interface Type**: Function Call + GitHub API

**Key Methods**:

#### 5.1.1 post_agent_output()
```python
result = await github.post_agent_output(
    task_context: Dict[str, Any],
    comment: str,
    reply_to_id: Optional[str] = None
) -> Dict[str, Any]
```

**Input**:
- **task_context**: See section 2.1 for structure
- **comment**: Markdown formatted output
- **reply_to_id**: Optional comment/discussion ID for threading

**Output**:
```python
{
    'success': bool,
    'comment_id': str,       # GitHub comment ID
    'url': str,              # Comment URL
    'error': str             # Optional: if failed
}
```

**Routing Logic** (workspace-aware):
- If `workspace_type == 'discussions'`: Post to discussion
- Else: Post to issue comment

#### 5.1.2 create_comment()
```python
comment_id = github.create_comment(
    issue_number: int,
    body: str,
    reply_to_id: Optional[str] = None
) -> str
```

#### 5.1.3 add_discussion_comment()
```python
comment_id = github.add_discussion_comment(
    discussion_id: str,
    body: str,
    reply_to_id: Optional[str] = None
) -> str
```

#### 5.1.4 get_issue()
```python
issue = github.get_issue(issue_number: int) -> Dict[str, Any]
```

**Output**:
```python
{
    'number': int,
    'title': str,
    'body': str,
    'state': 'open' | 'closed',
    'labels': List[{
        'name': str,
        'color': str,
        'description': str
    }],
    'assignees': List[str],
    'created_at': str,
    'updated_at': str,
    'comments': List[{
        'id': str,
        'author': str,
        'body': str,
        'created_at': str
    }]
}
```

### 5.2 GitHubProjectManager → GitHub GraphQL API
**Interface Type**: GraphQL Queries

**Key Queries**:

#### 5.2.1 Create Project Board
```graphql
mutation CreateProjectV2 {
  createProjectV2(input: {
    ownerId: $ownerId
    title: $title
  }) {
    projectV2 {
      id
      number
    }
  }
}
```

#### 5.2.2 List Project Columns
```graphql
query GetProjectColumns {
  node(id: $projectId) {
    ... on ProjectV2 {
      fields(first: 20) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id
            name
            options {
              id
              name
            }
          }
        }
      }
    }
  }
}
```

#### 5.2.3 Move Card
```graphql
mutation UpdateProjectV2ItemFieldValue {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId
    itemId: $itemId
    fieldId: $fieldId
    value: {
      singleSelectOptionId: $optionId
    }
  }) {
    projectV2Item {
      id
    }
  }
}
```

---

## 6. State Persistence Interfaces

### 6.1 StateManager (Pipeline State)
**Interface Type**: File I/O

**File Format**: YAML

**Location**: `orchestrator_data/state/`

#### 6.1.1 save_checkpoint()
```python
state_manager.save_checkpoint(
    pipeline_id: str,
    checkpoint_data: Dict[str, Any]
)
```

**Checkpoint Data Structure**:
```python
{
    'pipeline_id': str,
    'current_stage': int,           # Stage index
    'stage_outputs': Dict[str, Any], # Stage results
    'context': Dict[str, Any],      # Execution context
    'timestamp': str,               # ISO 8601
    'completed_stages': List[str]   # Completed stage names
}
```

#### 6.1.2 load_checkpoint()
```python
checkpoint = state_manager.load_checkpoint(pipeline_id: str) -> Dict[str, Any]
```

**Output**: Same structure as save_checkpoint input

### 6.2 Redis State Interfaces

#### 6.2.1 Work Execution State
**Redis Key Pattern**: `execution_state:{project}:{issue_number}:{column}:{agent}`

**Value**:
```python
{
    'status': 'in_progress' | 'completed' | 'failed',
    'started_at': str,          # ISO 8601
    'completed_at': str,        # ISO 8601
    'error': str                # Optional
}
```

**TTL**: 2 hours

#### 6.2.2 Container Tracking
**Redis Key Pattern**: `agent_container:{container_name}`

**Value**:
```python
{
    'project': str,
    'agent': str,
    'task_id': str,
    'started_at': str,
    'status': 'running'
}
```

**TTL**: 2 hours

#### 6.2.3 Conversational Session State
**Redis Key Pattern**: `conversation_session:{project}:{issue_number}`

**Value**:
```python
{
    'agent': str,
    'board': str,
    'column': str,
    'started_at': str,
    'last_interaction_at': str,
    'last_agent_comment_id': str,
    'status': 'active' | 'paused' | 'completed',
    'thread_history': List[{
        'role': 'agent' | 'human',
        'author': str,
        'body': str,
        'timestamp': str
    }],
    'turn_count': int,
    'initial_column': str  # For column exit detection
}
```

**TTL**: 24 hours

---

## 7. Prompt Construction Interfaces

### 7.1 MakerAgent → Claude Code Prompt
**Interface Type**: String Construction

**Prompt Structure** varies by execution mode:

#### 7.1.1 Initial Mode Prompt
```markdown
You are a {agent_display_name}.

{agent_role_description}

## Task: Initial Analysis

Analyze the following requirement for project {project}:

**Title**: {issue.title}
**Description**: {issue.body}
**Labels**: {issue.labels}

{previous_stage_output}  # If present

## Quality Standards
{quality_standards}  # If defined

## Output Format

Provide a comprehensive analysis with the following sections:
- {output_section_1}
- {output_section_2}
- ...

{guidelines}  # Agent-specific guidelines

{output_instructions}  # Based on makes_code_changes flag
```

#### 7.1.2 Question Mode Prompt
```markdown
You are the {agent_display_name} continuing a conversation.

{agent_role_description}

## Original Context
**Title**: {issue.title}
**Description**: {issue.body}

{guidelines}

## Conversation History
{formatted_thread_history}

## Latest Question
{current_question}

## Response Guidelines

You are in **conversational mode**:

1. **Take Action When Requested**: If the user is asking you to proceed, DO IT
2. **Be Direct & Concise**: 200-500 words unless needed
3. **Reference Prior Discussion**: Build on what's been said
4. **Natural Tone**: Professional but approachable
5. **Stay Focused**: Answer the specific question
...

{output_instructions}

Your response will be posted as a threaded reply.
```

#### 7.1.3 Revision Mode Prompt
```markdown
You are the {agent_display_name} revising your work based on feedback.

{agent_role_description}

{cycle_context}  # Review cycle or feedback context

## Original Context
**Title**: {issue.title}
**Description**: {issue.body}

## Your Previous Output (to be revised)
{previous_output}

## Feedback to Address
{feedback}

## Revision Guidelines

**CRITICAL - How to Revise**:
1. **Read feedback systematically**: List each distinct issue raised
2. **Address EVERY feedback point**: Don't leave any issues unresolved
3. **Make TARGETED changes**: Modify only what was criticized
4. **Keep working content**: Don't rewrite sections that weren't criticized
5. **Stay focused**: Don't add new content unless requested

**Required Output Structure**:

**MUST START WITH**:
```
## Revision Notes
- ✅ [Issue 1 Title]: [Brief description of change]
- ✅ [Issue 2 Title]: [Brief description of change]
...
```

**Then provide your COMPLETE, REVISED document**:
- All sections: {output_sections}
- Full content (not just changes)
...
```

### 7.2 Formatted Thread History
**Structure**:
```markdown
**You** (agent_name):
{agent_previous_message}

**@human_user**:
{human_question}

**You** (agent_name):
{agent_response}

**@human_user**:
{human_followup}
```

---

## 8. Task Queue Interfaces

### 8.1 Any Component → TaskQueue
**Interface Type**: Function Call + Redis

#### 8.1.1 enqueue()
```python
task_queue.enqueue(task: Task)
```

**Redis Operation**:
```python
# Sorted set with priority scoring
score = -(priority.value * 1000 + timestamp)
redis.zadd('orchestrator:tasks:queue', {task_json: score})
```

#### 8.1.2 dequeue()
```python
task = task_queue.dequeue() -> Optional[Task]
```

**Redis Operation**:
```python
# Get lowest score (highest priority, oldest)
results = redis.zrange('orchestrator:tasks:queue', 0, 0)
redis.zrem('orchestrator:tasks:queue', results[0])
return Task.from_json(results[0])
```

---

## 9. Review Cycle Interfaces

### 9.1 ReviewCycleService → Agent Execution
**Interface Type**: Iterative Function Calls

**Flow**:
```
1. Queue maker agent → Execute → Get output
2. Queue reviewer agent → Execute → Get review
3. Parse review → Check approval
4. If approved: Complete
5. If changes requested:
   - Queue maker agent with revision context
   - Increment iteration
   - Repeat from step 2
6. If max iterations: Escalate
```

**Revision Context Structure**:
```python
{
    'trigger': 'review_cycle_revision',
    'revision': {
        'previous_output': str,
        'feedback': str
    },
    'review_cycle': {
        'iteration': int,
        'max_iterations': int,
        'reviewer_agent': str
    }
}
```

### 9.2 ReviewParser → Structured Review
**Interface Type**: String Parsing

**Input**: Markdown review output from reviewer agent

**Output**:
```python
{
    'approved': bool,
    'issues': List[{
        'title': str,
        'description': str,
        'severity': str  # Inferred from language
    }],
    'summary': str,
    'sections_reviewed': List[str]
}
```

**Parsing Logic**:
- Search for `[APPROVED]` or `[CHANGES REQUESTED]`
- Extract issues from "## Issues" section
- Parse numbered/bulleted lists

---

## 10. Workspace Router Interfaces

### 10.1 WorkspaceRouter → Workspace Type Selection
**Interface Type**: Decision Logic

**Input**:
```python
{
    'pipeline': str,        # Pipeline name
    'stage': str,          # Stage name
    'agent': str,          # Agent name
    'issue_labels': List[str]
}
```

**Output**:
```python
{
    'workspace_type': 'issues' | 'discussions' | 'hybrid',
    'category_id': str,     # If discussions
    'reason': str           # Decision explanation
}
```

**Decision Logic**:
1. Check pipeline configuration (`workspace` field)
2. Check stage requirements (code changes need git)
3. Check agent capabilities
4. Default to 'issues' for code agents, 'discussions' for analysis agents

---

## Summary of Interface Patterns

### High-Frequency Interfaces
1. **Context Dictionary Pattern**: Used everywhere, mutable state through execution chain
2. **Event Emission**: Constant observability event flow to Redis/Elasticsearch
3. **Configuration Queries**: Every component queries ConfigManager
4. **GitHub API Calls**: Continuous interaction with GitHub

### Complex Interfaces
1. **Workspace Context Lifecycle**: prepare → execute → finalize with rich data exchange
2. **Review Cycle State Machine**: Multi-iteration maker-checker with revision context
3. **Stream Processing**: Real-time Claude Code event streaming with callback chains
4. **Pipeline Execution Context**: Deep nested context with 20+ fields

### Critical Data Structures
1. **Task Context**: The most critical data structure, passed through entire execution chain
2. **Execution Context**: Enriched task context with infrastructure references
3. **ObservabilityEvent**: Standard event structure for all system events
4. **AgentConfig**: Controls agent behavior across all executions

This interface documentation provides the foundation for understanding how information flows through the system and where integration points exist for redesign efforts.
