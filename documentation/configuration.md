# Configuration reference

Switchyard uses a three-layer configuration system. The layers are loaded in order and each builds on the previous: the foundations layer defines what is possible, the projects layer selects and configures what each project uses, and the state layer records runtime facts that the orchestrator discovers during operation.

**Configuration root**: `config/` inside the switchyard repository.

```
config/
├── foundations/          # Shared definitions — agents, pipelines, workflows, MCP
│   ├── agents.yaml
│   ├── pipelines.yaml
│   ├── workflows.yaml
│   └── mcp.yaml
├── projects/             # One file per managed project
│   └── <project-name>.yaml
├── manager.py            # ConfigManager — loads and merges all layers
├── state_manager.py      # GitHubStateManager — runtime state persistence
└── environment.py        # Environment variable schema (pydantic-settings)

state/                    # Runtime state — do not edit manually
├── projects/<project>/
│   ├── github_state.yaml
│   ├── pr_review_state.yaml
│   └── github_state_backup_<timestamp>.yaml
├── dev_containers/<project>.yaml
├── execution_history/<project>_issue_<number>.yaml
├── pipeline_locks/<project>_<board>.yaml
├── pipeline_queues/<project>_<board>.yaml
└── conversational_sessions/
```

---

## Layer 1: Foundations

The foundations layer is shared across all projects. It defines the complete catalog of agents, the reusable pipeline templates, the workflow (Kanban board) templates, and the MCP server registry. These files change only when the platform itself changes — not when a new project is onboarded.

The `ConfigManager` loads all four foundations files at startup and caches them in memory. The caches are cleared only when `ConfigManager.reload_config()` is called.

---

## `config/foundations/agents.yaml`

Defines every agent that can be used in a pipeline. The top-level key is `agents`, where each entry key is the agent identifier used throughout the rest of the config system (for example, `senior_software_engineer`). A `default_config` block at the bottom of the file provides values that are merged into every agent definition.

### Per-agent fields

All fields are at the same indentation level under the agent key.

| Field | Type | Required | Description |
|---|---|---|---|
| `description` | string | yes | Human-readable summary of the agent's role. Appears in logs and the web UI. |
| `model` | string | yes | The Anthropic model ID to use. Valid values in use: `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5`. |
| `timeout` | integer | yes | Maximum seconds the agent container may run before the orchestrator kills it. Common values: `3600` (1 hour), `10800` (3 hours). |
| `retries` | integer | yes | Maximum number of times the orchestrator will retry a failed agent run for this stage before the task fails permanently. |
| `makes_code_changes` | boolean | no (default `false`) | Whether this agent writes files to the project workspace. Controls whether the orchestrator expects a commit from the agent and whether git operations are triggered afterward. |
| `filesystem_write_allowed` | boolean | no (default `true`) | Whether the agent container is permitted to write to `/workspace`. Set to `false` for review-only agents to enforce that no files are modified. |
| `requires_dev_container` | boolean | no (default `false`) | If `true`, the agent must run inside the project's dev container image (built by `dev_environment_setup`). This ensures project dependencies are available. |
| `requires_docker` | boolean | no (default `true`) | If `true`, the agent runs in an isolated Docker container via `docker_runner.py`. The only agents with this set to `false` are those that must manipulate Docker themselves (`dev_environment_setup`, `dev_environment_verifier`, `pipeline_analysis`). |
| `capabilities` | list of strings | yes | Capabilities this agent provides. Used by pipeline stages to match agents to requirements via `required_capabilities`. These are symbolic identifiers — there is no enforcement beyond matching. |
| `tools_enabled` | list of strings | yes | Claude Code tool categories enabled for this agent. Common values: `file_operations`, `git_integration`, `docker_operations`, `web_search`. |
| `mcp_servers` | list of strings | no | Names of MCP servers to enable. Each name is resolved against `config/foundations/mcp.yaml`. An empty list (`[]`) disables all MCP servers for the agent. |
| `circuit_breaker` | object | no | Overrides the default circuit breaker settings for this agent. Contains a single sub-field: `failure_threshold` (integer), which sets the number of consecutive failures that trip the breaker. Only `senior_software_engineer` sets this in the current config (`failure_threshold: 6`). |

### Registered agents

| Agent key | Model | Timeout | Docker | Dev container | Writes files |
|---|---|---|---|---|---|
| `dev_environment_setup` | opus-4-6 | 3600 | no | no | yes |
| `dev_environment_verifier` | sonnet-4-6 | 3600 | no | no | no |
| `idea_researcher` | opus-4-6 | 3600 | yes | no | no |
| `business_analyst` | sonnet-4-6 | 3600 | yes | no | no |
| `software_architect` | opus-4-6 | 3600 | yes | no | no |
| `work_breakdown_agent` | sonnet-4-6 | 3600 | yes | no | no |
| `senior_software_engineer` | haiku-4-5 | 10800 | yes | yes | yes |
| `code_reviewer` | opus-4-6 | 3600 | yes | no | no |
| `technical_writer` | sonnet-4-6 | 3600 | yes | no | yes |
| `documentation_editor` | sonnet-4-6 | 3600 | yes | no | no |
| `pr_code_reviewer` | opus-4-6 | 3600 | yes | yes | no |
| `requirements_verifier` | sonnet-4-6 | 3600 | yes | yes | no |
| `test_agent` | haiku-4-5 | 3600 | yes | no | yes |
| `claude_advisor` | sonnet-4-6 | 3600 | yes | yes | no |
| `pipeline_analysis` | sonnet-4-6 | 300 | no | no | no |

> **Note:** `agents/__init__.py` registers 12 agents in `AGENT_REGISTRY`. Not all agents defined in `agents.yaml` are necessarily registered there (`claude_advisor`, `test_agent`, and `pipeline_analysis` are examples of agents defined in YAML but not in the registry). Consult `agents/__init__.py` if adding a new agent.

### `default_config` block

Applied to every agent via merge before the per-agent values are read. The per-agent value always wins over the default if both exist.

| Field | Default value | Description |
|---|---|---|
| `working_directory` | `/workspace/{project_name}` | Working directory inside the agent container. The placeholder `{project_name}` is replaced at runtime by `ConfigManager.get_project_agent_config()`. |
| `output_format` | `structured_json` | Output format hint passed to the agent prompt. |
| `quality_gates.minimum_score` | `0.7` | Minimum quality score gate (applied by reviewer logic). |
| `quality_gates.validation_required` | `true` | Whether quality validation is required before advancing. |

---

## `config/foundations/pipelines.yaml`

Defines reusable pipeline templates. A pipeline template is a sequence of stages that an issue passes through. Projects select templates by name and instantiate them as board-specific pipelines.

The top-level key is `pipeline_templates`. Each entry key is the template identifier (for example, `sdlc_execution`).

### Pipeline template fields

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Human-readable name. |
| `description` | string | yes | Purpose of this pipeline. |
| `workflow_type` | string | yes | Currently always `maker_checker`, which denotes a pattern where agents produce output that can be reviewed before advancing. |
| `workspace` | string | no (default `issues`) | Where work items live. `issues` — GitHub Issues. `discussions` — GitHub Discussions. `hybrid` — starts in discussions and transitions to issues. |
| `discussion_category` | string | no | GitHub Discussions category name. Required when `workspace` is `discussions` or `hybrid`. |
| `stages` | list | yes | Ordered list of stage definitions. Stages run sequentially. |

### Stage definition fields

Each entry in `stages` has the following fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `stage` | string | yes | Stage identifier. Referenced by workflow column `stage_mapping` fields. |
| `name` | string | yes | Human-readable stage name. |
| `required_capabilities` | list of strings | yes | Capability tags the assigned agent must provide. Validated against the agent's `capabilities` list in `agents.yaml`. |
| `default_agent` | string | yes | Agent key to use when no override is specified. Must exist in `agents.yaml`. |
| `retries` | integer | yes | Maximum retry attempts for the maker agent. |
| `review_required` | boolean | yes | If `true`, this stage invokes a review cycle after the maker agent completes. |
| `reviewer_agent` | string | no | Agent key for the checker role. Required when `review_required` is `true`. |
| `reviewer_retries` | integer | no | Maximum retry attempts for the reviewer agent. |
| `escalation` | object | no | Escalation policy for blocked reviews. Sub-fields: `blocking_threshold` (integer — number of blocking reviews before escalation is triggered) and `github_pr_required` (boolean — whether a GitHub PR must exist before the stage is considered complete). |
| `inputs_from` | list of strings | no | Agent keys whose previous output is injected into the current stage's prompt context. |
| `stage_type` | string | no | Special stage behavior marker. `pr_review` — signals that a `PRReviewStage` should be instantiated rather than a standard stage. `repair_cycle` — signals a test-fix loop stage. Omitting this field (or `null`) means standard stage behavior. |
| `max_total_agent_calls` | integer | no | Circuit breaker for repair cycle stages. Caps the total number of agent invocations across all iterations of the cycle. |
| `checkpoint_interval` | integer | no | For repair cycle stages: persist a checkpoint every N iterations. |

### Defined pipeline templates

**`planning_design`**
Four sequential stages: `research` → `requirements` → `design` → `work_breakdown` → `pr_review`.
- `workspace: discussions`, `discussion_category: Ideas`
- No stage in this pipeline has `review_required: true`; the planning workflow uses conversational columns instead.
- `pr_review` stage has `stage_type: pr_review` and runs once the work breakdown is complete.

**`environment_support`**
Two sequential stages: `environment_setup` → `environment_verification`.
- `workspace: issues`
- Neither stage requires review.

**`sdlc_execution`**
Three sequential stages: `implementation` → `testing` → `staging`.
- `workspace: issues`
- `implementation` has `review_required: true`, `reviewer_agent: code_reviewer`, `reviewer_retries: 5`, and an escalation policy (`blocking_threshold: 1`, `github_pr_required: true`).
- `testing` has `stage_type: repair_cycle`, `max_total_agent_calls: 100`, `checkpoint_interval: 5`, and `review_required: false`.
- `staging` has `review_required: false` and `retries: 1`.

---

## `config/foundations/workflows.yaml`

Defines Kanban board structures and the automation rules that connect board columns to pipeline stages. The `GitHubProjectManager` uses these definitions to create and reconcile GitHub Projects v2 boards.

The top-level key is `workflow_templates`. Each entry key is a workflow identifier (for example, `sdlc_execution_workflow`).

### Workflow template fields

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Human-readable name. |
| `description` | string | yes | Purpose of this workflow. |
| `pipeline_mapping` | string | yes | Pipeline template key this workflow corresponds to. |
| `pipeline_trigger_columns` | list of strings | no | Column names that acquire the exclusive pipeline lock when an item enters them. Only one issue per board can hold this lock at a time. |
| `pipeline_exit_columns` | list of strings | no | Column names that release the pipeline lock when an item enters them. |
| `columns` | list | yes | Ordered column definitions for the Kanban board. |

### Column definition fields

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Column name as it appears on the GitHub project board. |
| `stage_mapping` | string or null | yes | Pipeline stage key this column is bound to. `null` for holding columns (Backlog, Done, etc.) that do not trigger agent work. |
| `agent` | string or null | yes | Agent key assigned to handle work items in this column. `null` for columns with no automated action. |
| `description` | string | yes | Human-readable description of the column's purpose. |
| `type` | string | no | Column behavior type. `conversational` — starts a multi-turn feedback loop (planning_design_workflow). `review` — starts a maker/checker review cycle. Omitting means standard pipeline stage. |
| `maker_agent` | string | no | For `type: review` columns — the agent that receives reviewer feedback and produces revisions. |
| `max_iterations` | integer | no (default `3`) | For `type: review` columns — maximum number of maker/checker revision cycles. |
| `auto_advance_on_approval` | boolean | no (default `false`) | If `true`, the orchestrator automatically moves the item to the next column when the reviewer approves. Must be set explicitly per column. |
| `escalate_on_blocked` | boolean | no (default `true`) | For review columns — whether blocking issues trigger escalation. |
| `feedback_timeout_seconds` | integer | no | For `type: conversational` columns — seconds to wait for human feedback before timing out. |
| `automation_rules` | list | yes | Ordered list of automation rule definitions triggered by board events. |

### Automation rule definition fields

Each entry in `automation_rules` has:

| Field | Type | Description |
|---|---|---|
| `trigger` | string | The board event that fires this rule. See trigger values below. |
| `action` | string | The action to perform. See action values below. |
| `parameters` | object | Action-specific parameters. |

**Trigger values**

| Trigger | When it fires |
|---|---|
| `item_created` | A new item is added to the board. |
| `item_moved_to_column` | An item is moved into this column. |
| `all_subtasks_completed` | All sub-issues linked to this item are in a completed state. |

**Action values**

| Action | Parameters | Effect |
|---|---|---|
| `assign_label` | `labels: [...]` | Applies the listed labels to the issue. |
| `start_pipeline_stage` | `stage: <stage-key>` | Enqueues the stage for execution and acquires the pipeline lock. |
| `start_conversational_loop` | `stage: <stage-key>` | Starts a multi-turn conversational agent session. |
| `start_review_cycle` | `stage: <stage-key>` | Starts a maker/checker review cycle. |
| `move_to_column` | `target_column: <name>` | Moves the item to the named column. |

### Defined workflow templates

**`planning_design_workflow`**
Columns: Backlog, Research, Requirements, Design, Work Breakdown, In Development, In Review, Done.
- `pipeline_trigger_columns: [Research]`
- `pipeline_exit_columns: [In Development, Done]`
- Research, Requirements, and Design columns are `type: conversational`.
- Work Breakdown and In Review columns use `start_pipeline_stage`.

**`sdlc_execution_workflow`**
Columns: Backlog, Development, Code Review, Testing, Staged, Done.
- `pipeline_trigger_columns: [Development]`
- `pipeline_exit_columns: [Staged, Done]`
- Code Review is `type: review` with `maker_agent: senior_software_engineer`, `max_iterations: 5`, `auto_advance_on_approval: true`, `escalate_on_blocked: true`.

**`environment_support_workflow`**
Columns: Backlog, In Progress, Verification, Done.
- `pipeline_trigger_columns: [In Progress]`
- `pipeline_exit_columns: [Done]`

### Labels

`workflows.yaml` also defines three sections of GitHub label definitions that `GitHubProjectManager` creates in each repository.

**`pipeline_labels`**: Routing labels applied when an item is created on a board. One label per pipeline: `pipeline:planning-design` (color `5319e7`), `pipeline:sdlc-execution` (color `0e8a16`), `pipeline:environment-support` (color `d73a4a`).

**`stage_labels`**: Progress labels applied when an item enters a column. All use color `cfd3d7`. Labels include `stage:research`, `stage:requirements`, `stage:design`, `stage:work-breakdown`, `stage:implementation`, `stage:code-review`, `stage:testing`, `stage:staging`, `stage:environment-setup`, `stage:environment-verification`, `stage:pr-review`, `status:in-development`, `status:staged`, `status:completed`.

**`github_project_settings`**:
- `visibility: private`
- `description_template: "Automated project board managed by Claude Code Orchestrator"`

---

## `config/foundations/mcp.yaml`

Registry of MCP servers that agents can connect to. The top-level key is `mcp_servers`. Each entry key is the server name used in agent `mcp_servers` lists in `agents.yaml`.

### MCP server fields

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | string | yes | Connection type. `http` — HTTP endpoint. `stdio` — launched as a subprocess via stdin/stdout. |
| `url` | string | required when `type: http` | Endpoint URL. Supports environment variable substitution using `${VAR_NAME}` syntax. |
| `command` | string | required when `type: stdio` | Executable to launch. |
| `args` | list of strings | no | Arguments passed to the command. Supports `{work_dir}` placeholder for the agent's working directory. |
| `capabilities` | list of strings | yes | Symbolic capability names advertised by this server. |
| `description` | string | yes | Human-readable description. |

### Defined MCP servers

**`context7`**
- `type: http`, `url: ${CONTEXT7_MCP_URL}`
- Capabilities: `library_documentation`, `api_references`, `package_search`
- Requires `CONTEXT7_MCP_URL` environment variable. If `CONTEXT7_API_KEY` is also set, it is injected into the agent container environment.

**`playwright`**
- `type: stdio`, `command: npx`, args include `@playwright/mcp@latest` and `--cdp-endpoint ws://browserless:3000`
- Capabilities: `browser_automation`, `ui_testing`, `screenshot_capture`, `accessibility_testing`, `network_interception`, `form_interaction`
- Requires the `browserless` service to be running (defined in `docker-compose.yml`).

**`serena`**
- `type: stdio`, `command: uvx`, fetches from `git+https://github.com/oraios/serena`
- Args include `--project {work_dir}` where `{work_dir}` is replaced with the agent's working directory at runtime.
- Capabilities: `codebase_analysis`, `semantic_search`, `code_understanding`, `symbol_lookup`

---

## Layer 2: Projects (`config/projects/<project-name>.yaml`)

One YAML file per managed project. The filename stem (without `.yaml`) is the project name used in all API calls, state paths, and log messages. The `ConfigManager` reads project configs directly from disk on every call to `get_project_config()` — there is no caching at this layer, so changes take effect on the next poll cycle without a restart.

The file has two top-level sections: `project` and `orchestrator`.

### `project` section

#### `project.name`
String. Must match the filename stem. Used as the identifier throughout the system.

#### `project.description`
String. Free-text description. Appears in the web UI.

#### `project.hidden`
Boolean, default `false`. When `true`, the project is excluded from the web UI, monitoring loops, and `list_visible_projects()`. The state files remain on disk and the project is still returned by `list_projects()`.

#### `project.github`

| Field | Type | Required | Description |
|---|---|---|---|
| `org` | string | yes | GitHub organization or user name. |
| `repo` | string | yes | Repository name within the organization. |
| `repo_url` | string | yes | SSH clone URL. Used by `GitWorkflowManager` for all git operations. Format: `git@github.com:<org>/<repo>.git`. |
| `branch` | string | yes | Default branch name. Typically `main`. |

#### `project.tech_stacks`

| Field | Type | Required | Description |
|---|---|---|---|
| `backend` | string | yes | Comma-separated list of backend technologies. Injected into agent prompts as context. Can be empty string if not applicable. |
| `frontend` | string | yes | Comma-separated list of frontend technologies. Injected into agent prompts as context. Can be empty string if not applicable. |

#### `project.testing`

Configures the repair cycle stages (`stage_type: repair_cycle`) for this project. Optional. If omitted, the repair cycle falls back to built-in defaults.

| Field | Type | Description |
|---|---|---|
| `types` | list | List of test type configurations (see below). |
| `max_file_iterations` | integer | Global per-file fix attempt limit. Applied when a type-specific value is not set. |
| `failure_escalation_threshold` | integer | Number of consecutive cycle failures before the orchestrator escalates to a human. |

**Test type configuration** (entries in `testing.types`):

| Field | Type | Description |
|---|---|---|
| `type` | string | Test category. One of: `compilation`, `unit`, `integration`, `ci`. |
| `max_iterations` | integer | Maximum iterations of the test-fix-validate loop for this type. |
| `review_warnings` | boolean | If `true`, the agent attempts to fix compiler/test warnings in addition to failures. |
| `max_file_iterations` | integer | Maximum times the agent may attempt to fix a single file within one iteration. Overrides the global `max_file_iterations` for this type. |

#### `project.pipelines`

| Field | Type | Required | Description |
|---|---|---|---|
| `enabled` | list | yes | List of pipeline instance definitions. |

**Pipeline instance fields** (entries in `pipelines.enabled`):

| Field | Type | Required | Description |
|---|---|---|---|
| `template` | string | yes | Pipeline template key from `foundations/pipelines.yaml`. |
| `name` | string | yes | Instance identifier. Used in `pipeline_routing.label_routing` and in log messages. Convention: `planning-design`, `sdlc-execution`, `environment-support`. |
| `board_name` | string | yes | The exact name of the GitHub Projects v2 board to create or reconcile for this pipeline. |
| `description` | string | yes | Human-readable description of this board's purpose. |
| `workflow` | string | yes | Workflow template key from `foundations/workflows.yaml`. |
| `active` | boolean | yes | If `false`, this pipeline is ignored by `get_project_pipelines()` and no board is created or monitored. |

When `workspace`, `discussion_category`, `discussion_stages`, or `issue_stages` are not set on the pipeline instance, the values are inherited from the pipeline template definition.

#### `project.pipeline_routing`

Controls how incoming issues are routed to the correct pipeline.

| Field | Type | Required | Description |
|---|---|---|---|
| `default_pipeline` | string | yes | Pipeline instance `name` to use when no routing label is present. |
| `label_routing` | map | yes | Maps GitHub label strings to pipeline instance names. Each key is a label name (for example, `"pipeline:sdlc-execution"`); the value is the pipeline `name` (for example, `"sdlc-execution"`). Every label defined in `pipeline_labels` in `workflows.yaml` should have a corresponding entry here. |

Validation in `ConfigManager.validate_project_config()` checks that every value in `label_routing` corresponds to a pipeline instance name in `pipelines.enabled`.

#### `project.agent_customizations`

Optional map. Keys are agent names from `agents.yaml`. Values are override maps applied on top of the agent's base config when `get_project_agent_config()` is called. Only `timeout` and `retries` are applied from customizations (see `config/manager.py` line 422).

Example:

```yaml
agent_customizations:
  senior_software_engineer:
    timeout: 7200
    retries: 5
```

### `orchestrator` section

Sits at the top level of the file (sibling to `project`, not nested under it).

| Field | Type | Description |
|---|---|---|
| `polling_interval` | integer | Seconds between GitHub board poll cycles for this project. Overrides the global default in `ProjectMonitor`. |
| `priority_mapping.high_priority_columns` | list of strings | Column names mapped to `HIGH` task priority. |
| `priority_mapping.medium_priority_columns` | list of strings | Column names mapped to `MEDIUM` task priority. |
| `priority_mapping.low_priority_columns` | list of strings | Column names mapped to `LOW` task priority. |

### Full annotated example: `config/projects/context-studio.yaml`

```yaml
project:
  name: "context-studio"            # Must match filename stem
  description: "Context Studio project configuration"

  github:
    org: "tinkermonkey"
    repo: "context-studio"
    repo_url: "git@github.com:tinkermonkey/context-studio.git"
    branch: "main"

  tech_stacks:
    backend: "python, fastapi, sqlite, sqlalchemy, langchain"
    frontend: "react, flowbite-react"

  testing:
    types:
      - type: "compilation"
        max_iterations: 5
        review_warnings: false
        max_file_iterations: 3
      - type: "unit"
        max_iterations: 5
        review_warnings: false     # Do not fix warnings, only failures
        max_file_iterations: 3
      - type: "integration"
        max_iterations: 5
        review_warnings: false
        max_file_iterations: 3
      - type: "ci"
        max_iterations: 5
        review_warnings: false
        max_file_iterations: 3
    max_file_iterations: 3          # Global per-file fallback
    failure_escalation_threshold: 2 # Escalate after 2 consecutive cycle failures

  pipelines:
    enabled:
      - template: "planning_design"
        name: "planning-design"
        board_name: "Planning & Design"
        description: "Planning & Design"
        workflow: "planning_design_workflow"
        active: true
      - template: "sdlc_execution"
        name: "sdlc-execution"
        board_name: "SDLC Execution"
        description: "SDLC Execution"
        workflow: "sdlc_execution_workflow"
        active: true
      - template: "environment_support"
        name: "environment-support"
        board_name: "Environment Support"
        description: "Development Environment Support"
        workflow: "environment_support_workflow"
        active: true

  pipeline_routing:
    default_pipeline: "planning-design"
    label_routing:
      "pipeline:planning-design": "planning-design"
      "pipeline:sdlc-execution": "sdlc-execution"
      "pipeline:environment-support": "environment-support"

orchestrator:                        # Top-level section, not under project:
  polling_interval: 15               # Seconds between board polls

  priority_mapping:
    high_priority_columns:
      - "Code Review"
      - "QA Testing"
    medium_priority_columns:
      - "Implementation"
      - "Design"
    low_priority_columns:
      - "Research"
      - "Analysis"
```

---

## Layer 3: State

The state layer records runtime facts: GitHub API identifiers, board synchronization status, PR review cycle history, dev container verification results, and pipeline execution locks. The orchestrator writes all state files; operators must not edit them manually.

State files live under `state/` relative to the switchyard repository root. Inside the orchestrator container this is `/app/state/`. On the host it is `switchyard/state/`.

### `state/projects/<project>/github_state.yaml`

Written by `GitHubStateManager.save_project_state()`. Created on first reconciliation with GitHub, then updated after each board sync.

```yaml
github_state:
  org: tinkermonkey
  repo: context-studio
  boards:
    SDLC Execution:               # Board name from project config board_name
      project_number: 12          # GitHub Projects v2 project number
      project_id: PVT_kwHOABgBzM4BEr-W   # GraphQL global ID
      node_id: PVT_kwHOABgBzM4BEr-W      # Same as project_id for v2 projects
      status_field_id: PVTSSF_...  # GraphQL ID of the Status single-select field
      url: https://github.com/orgs/tinkermonkey/projects/12  # Optional
      columns:
        - name: Backlog
          id: 7c5ff805            # GraphQL option ID for this status value
          node_id: 7c5ff805
        - name: Development
          id: 339f4df4
          node_id: 339f4df4
        # ... one entry per column
  labels_created:
    - pipeline:sdlc-execution
    - stage:implementation
    # ... all labels that have been created in the repo
  last_sync: '2025-10-07T10:00:05.156437'  # UTC ISO 8601
  sync_hash: f177383428545914     # SHA-256 prefix of serialized pipeline/routing config
  issue_discussion_links:
    "42": DIC_kwDO...             # issue number (string) -> discussion GraphQL ID
  discussion_issue_links:
    DIC_kwDO...: 42               # discussion GraphQL ID -> issue number (integer)
```

**Why not to edit this file**: The `sync_hash` is a 16-character SHA-256 digest of the project's pipeline and routing configuration. If the hash does not match the current config, `needs_reconciliation()` returns `true` and the reconciliation loop recreates board state. Manually editing the hash or board IDs will corrupt board operations.

**Backup files**: Before each save, `GitHubStateManager.backup_state()` creates a copy named `github_state_backup_<YYYYMMDD_HHMMSS>.yaml` in the same directory. These are safe to delete.

### `state/projects/<project>/pr_review_state.yaml`

Written by `PRReviewStateManager`. Tracks PR review cycle counts and iteration history per issue. Used to enforce cycle limits and prevent redundant re-reviews.

```yaml
pr_reviews:
  224:                             # Issue number (integer key)
    review_count: 3                # Total review iterations completed
    last_review_at: '2026-03-06T23:09:56.692890Z'
    cycle_limit_notified: true     # Set when the cycle limit has been reached and notification sent
    cycle_limit_notified_at: '2026-03-07T00:32:54.655311Z'
    iterations:
      - iteration: 1
        timestamp: '2026-03-04T18:44:18.078114Z'
        issues_created:            # Sub-issue numbers created by this review iteration
          - 250
          - 251
```

### `state/dev_containers/<project>.yaml`

Written by `DevContainerStateManager`. Records whether the project's Docker agent image has been verified.

```yaml
image_name: context-studio-agent:latest
status: verified                   # "verified" or "unverified" or "failed"
updated_at: '2026-03-09T01:37:58.560025'
```

The orchestrator checks this file at startup and before launching any agent with `requires_dev_container: true`. If `status` is not `verified`, it triggers `dev_environment_setup` and `dev_environment_verifier` before proceeding.

### `state/execution_history/<project>_issue_<number>.yaml`

Written by `WorkExecutionStateManager`. Records the full execution history of an issue: every agent invocation, its outcome, and all column transitions.

```yaml
issue_number: 122
project_name: context-studio
current_status: In Progress
last_updated: '2025-10-07T16:25:33.445377+00:00'
execution_history:
  - column: In Progress
    agent: dev_environment_setup
    timestamp: '2025-10-07T11:42:40.887853+00:00'
    outcome: failure               # "success" or "failure"
    trigger_source: unknown
    error: 'Dev Environment Setup Specialist execution failed: ...'
  - column: In Progress
    agent: dev_environment_setup
    timestamp: '2025-10-07T14:40:54.803709+00:00'
    outcome: success
    trigger_source: unknown
status_changes:
  - from_status: Backlog
    to_status: In Progress
    timestamp: '2025-10-07T10:05:35.492485+00:00'
    trigger: manual                # "manual" or "automated"
```

### `state/pipeline_locks/<project>_<board>.yaml`

Written by the pipeline lock manager. Enforces that only one issue per board can be in the active pipeline at a time. Files with a `.lock` extension are filesystem-level advisory locks used during write operations.

```yaml
project: codetoreum
board: SDLC Execution
locked_by_issue: 385
lock_acquired_at: '2026-03-15T20:48:02.725078+00:00'
lock_status: locked                # "locked" or "unlocked"
```

A lock file persists until the issue exits a `pipeline_exit_columns` column. If a lock file exists for a board when the orchestrator starts (indicating a crash mid-pipeline), the lock is evaluated against the issue's current board column to determine whether to resume or release.

### `state/pipeline_queues/<project>_<board>.yaml`

Written by the pipeline queue manager. Records issues waiting to enter the pipeline while the lock is held by another issue.

```yaml
project: codetoreum
board: Environment Support
queue: []                          # List of issue numbers waiting for the lock
last_updated: '2026-03-15T22:30:01.207047+00:00'
```

### `state/conversational_sessions/`

Contains per-issue YAML files recording the message history for conversational pipeline stages (those with `type: conversational` in the workflow template). Used by `ConversationalSessionStateManager` to resume multi-turn sessions after an agent container exits. Do not edit these files; the session manager owns them entirely.

---

## Environment variables

Switchyard reads environment from a `.env` file at the repository root (or from the container environment). The `Environment` class in `config/environment.py` uses `pydantic-settings` and provides all defaults.

Variables are listed by their `.env` name (uppercase with underscores). The `Environment` class maps each to a lowercase attribute name with the same spelling.

### Authentication

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | one of these two is required | — | Anthropic API key. Used when `CLAUDE_CODE_OAUTH_TOKEN` is not set. Injected into agent containers. |
| `CLAUDE_CODE_OAUTH_TOKEN` | one of these two is required | — | Claude subscription OAuth token. Takes precedence over `ANTHROPIC_API_KEY` when both are set. |
| `GITHUB_TOKEN` | yes | — | GitHub Personal Access Token with `repo` and `project` scopes. Used for all GitHub API calls. |
| `OPENAI_API_KEY` | no | — | OpenAI API key. Not used by any current agents but present in the schema. |
| `CONTEXT7_API_KEY` | no | — | API key for the Context7 MCP server. Injected into agent containers when set. |

### GitHub configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `GITHUB_ORG` | yes | — | GitHub organization name. Used as a fallback by `github_integration.py` when not derivable from project config. |
| `GITHUB_DEFAULT_BRANCH` | no | `main` | Default branch name for git operations. |
| `GITHUB_APP_ID` | no | — | GitHub App ID. Enables App-based authentication (bot comments, better rate limits). |
| `GITHUB_APP_INSTALLATION_ID` | no | — | GitHub App installation ID for the target organization. |
| `GITHUB_APP_PRIVATE_KEY_PATH` | no | — | Path to the GitHub App private key PEM file. Convention: `~/.orchestrator/<app-name>.pem`. |
| `GITHUB_APP_PRIVATE_KEY` | no | — | GitHub App private key contents as a string (alternative to path). |

### Redis

| Variable | Required | Default | Description |
|---|---|---|---|
| `REDIS_URL` | no | `redis://redis:6379` | Full Redis connection URL. |
| `REDIS_HOST` | no | `redis` | Redis hostname. Read directly in several modules that do not use `Environment`. |
| `REDIS_PORT` | no | `6379` | Redis port. |
| `REDIS_PASSWORD` | no | — | Redis authentication password. |

### Elasticsearch

| Variable | Required | Default | Description |
|---|---|---|---|
| `ELASTICSEARCH_HOST` | no | `elasticsearch` | Elasticsearch hostname. |
| `ELASTICSEARCH_PORT` | no | `9200` | Elasticsearch port. |
| `ELASTICSEARCH_HOSTS` | no | `http://elasticsearch:9200` | Full Elasticsearch URL(s), comma-separated. Takes precedence in modules that read it directly. |

### MCP servers

| Variable | Required | Default | Description |
|---|---|---|---|
| `CONTEXT7_MCP_URL` | required if context7 MCP is used | — | HTTP endpoint for the Context7 MCP server. Substituted into `mcp.yaml` at load time. |
| `SERENA_MCP_URL` | no | — | URL for a remote Serena MCP server. Present in environment schema but Serena is currently configured as stdio. |
| `PUPPETEER_MCP_URL` | no | — | URL for a Puppeteer MCP server. Present in schema, not referenced in current agent configs. |

### Claude and token quotas

| Variable | Required | Default | Description |
|---|---|---|---|
| `CLAUDE_MODEL` | no | `claude-sonnet-4-5-20250929` | Fallback model name used by the `Environment` class. Individual agents override this via `agents.yaml`. |
| `MAX_TOKENS` | no | `100000` | Maximum tokens per Claude API call for the orchestrator's own Claude calls. |
| `TEMPERATURE` | no | `0.3` | Temperature for Claude API calls made by the orchestrator (not agent containers). |
| `CLAUDE_CODE_WEEKLY_TOKEN_QUOTA` | no | `630000000` | Weekly token budget (630M tokens). Used by `HealthMonitor` to warn when approaching the quota. |
| `CLAUDE_CODE_SESSION_TOKEN_QUOTA` | no | `50000000` | Per-session token budget (50M tokens per 5-hour block). |

### Docker and host configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `HOST_HOME` | yes when running in Docker | — | Absolute path to the host user's home directory. Required for correct SSH key mounting into agent containers (`docker_runner.py` uses this to construct the `-v` flag). Set to `/home/<your-user>`. |
| `HOST_WORKSPACE_PATH` | no | `/workspace` | Fallback path used when the orchestrator cannot determine the workspace root. |
| `HOST_UID` | no | `1000` | Host user UID. Used by `docker-compose.yml` for file permission alignment. |
| `HOST_GID` | no | `1000` | Host user GID. Used by `docker-compose.yml` for file permission alignment. |
| `DOCKER_GID` | no | `0` | Docker socket GID. Set to match the host's docker group GID to allow agent containers to reach the Docker socket. |

### Observability and pattern analysis

| Variable | Required | Default | Description |
|---|---|---|---|
| `LOG_LEVEL` | no | `INFO` | Python logging level. Valid values: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `LOG_BATCH_SIZE` | no | `50` | Number of log entries batched before flushing to Elasticsearch. |
| `LOG_BATCH_TIMEOUT` | no | `5.0` | Seconds to wait before flushing an incomplete log batch. |
| `DETECTION_INTERVAL` | no | `60` | Seconds between pattern detection passes. |
| `LOOKBACK_MINUTES` | no | `5` | Minutes of history scanned per pattern detection pass. |
| `AGGREGATION_INTERVAL_HOURS` | no | `24` | Hours between pattern aggregation runs. |
| `LOOKBACK_DAYS` | no | `7` | Days of history used for aggregation. |
| `SIMILARITY_ANALYSIS_INTERVAL_HOURS` | no | `24` | Hours between similarity analysis runs. |
| `SIMILARITY_THRESHOLD` | no | `0.75` | Cosine similarity threshold for grouping patterns. |
| `MIN_OCCURRENCES_FOR_SIMILARITY` | no | `5` | Minimum occurrences before a pattern is eligible for similarity analysis. |
| `LLM_ANALYSIS_INTERVAL_HOURS` | no | `168` | Hours between LLM-based pattern analysis runs (default: weekly). |
| `MIN_OCCURRENCES_FOR_LLM` | no | `20` | Minimum occurrences before a pattern is eligible for LLM analysis. |
| `MAX_PATTERNS_PER_LLM_RUN` | no | `5` | Maximum patterns analyzed per LLM analysis run. |
| `GITHUB_PROCESSING_INTERVAL` | no | `300` | Seconds between GitHub pattern processor runs. |
| `GITHUB_DISCUSSION_CATEGORY` | no | `Ideas` | GitHub Discussions category used by the pattern processor. |
| `MIN_OCCURRENCES_FOR_DISCUSSION` | no | `5` | Minimum pattern occurrences before a GitHub Discussion is created. |

### Orchestrator runtime

| Variable | Required | Default | Description |
|---|---|---|---|
| `ORCHESTRATOR_ROOT` | no | `/app` (in container) | Path to the switchyard repository root. Used by scripts and state managers to locate `state/` and `config/`. |
| `ORCHESTRATOR_WORKERS` | no | `1` | Number of worker threads for parallel task execution. The default of `1` is single-threaded. |
| `RECONCILIATION_FRESHNESS_HOURS` | no | `1` | Skip board reconciliation if `github_state.yaml` was written within this many hours. |
| `PROGRAMMATIC_CHANGE_WINDOW_SECONDS` | no | `60` | Seconds during which a board event is attributed to an orchestrator action (suppresses duplicate triggers). |
| `WATCHDOG_MAX_RETRIES` | no | `3` | Maximum retries the watchdog applies before giving up on a stuck task. |
| `TOKEN_METRICS_INTERVAL_HOURS` | no | `3` | Hours between token metrics collection runs. |
| `PATTERNS_DIR` | no | `config/patterns` | Directory containing pattern definition files for the pattern ingestion service. |
| `WEBHOOK_SECRET` | no | — | Webhook HMAC secret. Also readable as `GITHUB_WEBHOOK_SECRET`. |
| `WEBHOOK_PORT` | no | `3000` | Port for the webhook server. |
| `WEBHOOK_HOST` | no | `0.0.0.0` | Bind address for the webhook server. |
| `NGROK_AUTHTOKEN` | no | — | ngrok authentication token for exposing the webhook server. |

---

## How `ConfigManager` loads and merges configuration at runtime

`ConfigManager` is instantiated as a module-level singleton (`config_manager`) in `config/manager.py`. The same instance is imported by `GitHubStateManager`, `ProjectMonitor`, `PipelineOrchestrator`, and other services.

**Loading sequence on first access**:

1. When any getter is first called (`get_agents()`, `get_pipeline_templates()`, `get_workflow_templates()`), the manager loads the corresponding foundations file from `config/foundations/`.
2. The loaded data is stored in an in-memory cache (`_agents`, `_pipeline_templates`, `_workflow_templates`). Subsequent calls return the cached value without re-reading disk.
3. MCP server definitions are loaded lazily when the first agent config containing an `mcp_servers` list is resolved.
4. `get_project_config()` always reads from disk. There is no cache for project configs. This means changes to `config/projects/<project>.yaml` take effect on the next poll cycle without a restart.

**Agent config resolution** (`get_project_agent_config(project, agent)`):

1. Load the base `AgentConfig` from the foundations cache.
2. Load the `ProjectConfig` from disk.
3. Look up any `agent_customizations` for this agent in the project config.
4. Build a new `AgentConfig` with `timeout` and `retries` replaced by project values if present; all other fields come from the foundations definition.
5. Replace `{project_name}` in `working_directory` with the actual project name.
6. MCP server names in the base config have already been resolved to full server config dicts at step 1.

**Reload**: `ConfigManager.reload_config()` clears all in-memory caches and sets `_agents`, `_pipeline_templates`, and `_workflow_templates` back to `None`. The next access to any getter re-reads from disk. This method is not called automatically — it must be invoked explicitly (for example, by a SIGHUP handler or a future hot-reload endpoint).

**Validation**: `ConfigManager.validate_project_config(project)` returns a list of error strings. It checks:
- All `template` values in `pipelines.enabled` exist in `pipeline_templates`.
- All `workflow` values in `pipelines.enabled` exist in `workflow_templates`.
- All keys in `agent_customizations` name agents that exist in `agents.yaml`.
- All values in `pipeline_routing.label_routing` name pipelines that appear in `pipelines.enabled`.

Validation is not called automatically at startup. Run it manually or integrate it into a pre-flight check when adding a new project.

---

## Adding a new project

1. Create `config/projects/<project-name>.yaml` following the annotated example above.
2. Set `project.name` to match the filename stem exactly.
3. Define at least one entry in `pipelines.enabled` referencing a template and workflow from the foundations layer.
4. Ensure every pipeline name referenced in `pipeline_routing.label_routing` appears in `pipelines.enabled`.
5. Run `ConfigManager().validate_project_config("<project-name>")` to catch reference errors before starting the orchestrator.
6. On next startup (or poll cycle), the reconciliation loop will create the GitHub boards, columns, and labels defined by the referenced workflow template.
