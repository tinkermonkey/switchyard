# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Architecture

This repository is a Claude Code Agent Orchestrator designed to facilitate automated code development and testing.

## Workspace Isolation and File System Safety

**CRITICAL**: The orchestrator MUST be isolated to its own workspace directory tree to prevent accidental modification of user's personal repositories.

### Workspace Structure

The orchestrator operates within a dedicated workspace directory that contains:
- `clauditoreum/` - The orchestrator codebase itself (this repository)
- `<project-name>/` - Managed project checkouts (e.g., `context-studio/`)

Example directory structure on the host machine:
```
~/workspace/orchestrator/          # Orchestrator workspace root
├── clauditoreum/                  # This codebase
│   ├── config/
│   ├── agents/
│   ├── pipeline/
│   └── ...
└── context-studio/                # Managed project checkout
    ├── src/
    ├── tests/
    └── ...
```

### File System Boundaries

**The orchestrator MUST NEVER access files outside its workspace directory.**

User's personal repositories exist as siblings to the orchestrator workspace:
```
~/workspace/                       # User's workspace (OFF LIMITS to orchestrator)
├── orchestrator/                  # Orchestrator's isolated workspace (SAFE)
│   ├── clauditoreum/
│   └── context-studio/           # Orchestrator-managed copy
├── context-studio/                # User's personal copy (OFF LIMITS)
├── other-project/                 # User's personal copy (OFF LIMITS)
└── ...
```

### Container Volume Mounts

In Docker, the isolation is enforced by mounting only the orchestrator workspace:
```yaml
volumes:
  - ./:/app                        # Mount clauditoreum/ as /app
  - ..:/workspace                  # Mount orchestrator/ as /workspace
```

Inside the container:
- `/workspace/` = Host's `~/workspace/orchestrator/`
- `/app/` = Host's `~/workspace/orchestrator/clauditoreum/`
- `/workspace/context-studio/` = Host's `~/workspace/orchestrator/context-studio/`

The orchestrator **cannot** see `/workspace/orchestrator/` from the host level - it only sees its own isolated workspace.

### Project Checkout and Management

When a project is configured in `config/projects/`, the orchestrator:
1. Checks if project directory exists in workspace (e.g., `/workspace/context-studio/`)
2. If not found, clones the repository to the workspace directory
3. Manages git operations (branches, commits, pushes) within this checkout
4. Launches agents in Docker containers with this project directory mounted

This ensures:
- User's personal repositories remain untouched
- All agent work happens in isolated, managed checkouts
- File system operations are contained within the orchestrator workspace

## Best Practices

- Don't use emojis in comments, code, or documentation.
- Never use absolute paths that could escape the workspace boundary
- Always use workspace-relative paths for project operations

## File structure

```
├── pipeline/                         # Pipeline definition
│   ├── base.py
│   ├── orchestrator.py
│   └── factory.py                    # Pipeline factory with new config system
├── state_management/                 # Legacy state management
│   ├── manager.py
│   └── git_state.py
├── handoff/                          # Handoff management
│   ├── protocol.py
│   └── quality_gate.py
├── agents/                           # Sub-agent implementations
│   ├── business_analyst.py
│   ├── product_manager.py
│   ├── software_engineer.py
│   └── ...
├── services/                         # Service layer
│   ├── github_project_manager.py    # GitHub reconciliation manager
│   └── project_monitor.py           # GitHub project monitoring
├── config/                           # Configuration system
│   ├── foundations/                  # Foundational configurations
│   │   ├── agents.yaml              # Agent definitions and capabilities
│   │   ├── pipelines.yaml           # Pipeline templates
│   │   └── workflows.yaml           # Kanban workflow templates
│   ├── projects/                     # Project-specific configurations
│   │   └── context-studio.yaml      # Example project configuration
│   ├── manager.py                    # Configuration loading and management
│   ├── state_manager.py             # GitHub state management
│   └── git_workflow.yaml            # Git automation settings
├── state/                            # Runtime state (not version controlled)
│   ├── projects/                     # Project-specific state
│   │   └── context-studio/
│   │       └── github_state.yaml    # GitHub project IDs, sync status
│   └── orchestrator/                 # Global orchestrator state
├── .claude/
│   └── CLAUDE.md                     # This file, the Orchestrator's own Claude instructions
└── requirements.txt                  # Python requirements
```

## Claude Code SDK architecture for autonomous agents

The Claude Code SDK employs a **single-threaded master loop architecture** that fundamentally shapes how agents operate for hours-long autonomous sessions. At its core, the SDK uses the nO (Master Loop Engine) for orchestration, combined with the h2A real-time steering queue for interruptions and guidance. This architecture supports automatic context compaction, session resumption, and memory persistence - essential capabilities for long-running development tasks.

The SDK's production-ready features include automatic prompt caching, fine-grained permission controls, and native IDE integration. **Sub-agents operate in isolated 200K token context windows**, preventing context pollution while enabling parallel processing. This isolation pattern is crucial for managing complex, multi-hour development sessions where different aspects of the codebase require focused attention.

Memory persistence follows a hierarchical file structure: `~/.claude/CLAUDE.md` for global memory, `./CLAUDE.md` for project-shared knowledge, and `./.claude/agents/` for specialized sub-agents. This system automatically discovers and loads relevant context as agents navigate project directories, maintaining continuity across sessions while preventing information overload.

## Sub-agent patterns and context management

Sub-agents represent a paradigm shift in managing complex development tasks. Each sub-agent operates as a specialized assistant with an **isolated context window**, enabling sophisticated task decomposition without context pollution. The implementation leverages markdown configuration files that define the agent's model, tools, and specific expertise.

Context window optimization employs multiple strategies. **Automatic context summarization** triggers when approaching token limits, preserving essential information while discarding verbose details. The recursive memory loading system gathers context from the project hierarchy on-demand, loading CLAUDE.md files only when accessing specific directories. This intelligent loading prevents context bloat while ensuring agents have necessary information.

For production deployments, the SDK supports centralized memory management through MDM or Group Policy, enabling team-shared knowledge in version-controlled memory files while maintaining individual developer preferences in local memory. The checkpoint pattern preserves state before major operations, enabling robust recovery from interruptions or failures.

## GitHub integration and Kanban automation

GitHub CLI integration forms the backbone of automated workflow management. Agents execute commands like `gh issue create`, `gh pr merge`, and `gh project item-create` programmatically, enabling full lifecycle automation from issue creation through deployment. The integration supports both GraphQL queries for complex project management and REST APIs for standard operations.

**Webhook listeners monitor Kanban board changes** in real-time, triggering agent workflows when cards move between columns. The implementation uses Node.js with signature verification for security, processing events like card movements to automatically initiate appropriate agent actions. For the two-board system, label-based status tracking provides additional granularity, with status labels (`incoming`, `scheduled`, `in-progress`) and project labels (`pre-sdlc`, `sdlc`) controlling workflow progression.

Docker containerization ensures consistent development environments across agent sessions. Each agent container includes git, GitHub CLI, and necessary development tools, with volume mounts providing access to the codebase and SSH keys for authentication. This isolation prevents environmental conflicts while maintaining security boundaries.

## Agent prompt engineering best practices

Effective prompt engineering balances conciseness with clarity. **Token reduction strategies** include selective elimination of redundant words, strategic use of abbreviations, and keyword prioritization. The LLMLingua approach achieves up to 20x compression using small language models to identify unimportant tokens, dramatically reducing costs while maintaining effectiveness.

Essential prompt elements that must always be retained include role definition, core objectives, output format specifications, critical constraints, and success criteria. Nice-to-have elements like verbose examples or philosophical explanations should be compressed or removed. The STAR framework (Situation, Task, Action, Result) provides structure for technical tasks while maintaining brevity.

**Reviewer agents implement the maker-checker loop pattern**, where maker agents create initial outputs and checker agents validate quality. This multi-layer review architecture progresses through syntax validation, logic review, context validation, and security assessment before final human review. Each layer catches different issue categories, ensuring comprehensive quality assurance.

## Configuration Management Architecture

The orchestrator uses a three-layer configuration architecture that separates foundational capabilities, project-specific choices, and runtime state.

### Foundational Layer (`config/foundations/`)

**Agent Definitions (`agents.yaml`)**: Defines what agents exist and their capabilities including models, timeouts, tools, and MCP server connections. This is the authoritative source for all available agents in the system.

**Pipeline Templates (`pipelines.yaml`)**: Defines reusable pipeline stage sequences with quality gates, timeouts, and maker-checker patterns. Templates can be instantiated by projects with customizations.

**Workflow Templates (`workflows.yaml`)**: Defines kanban board structures and automation rules including column definitions, agent assignments, and trigger conditions.

### Project Layer (`config/projects/`)

Project-specific configurations that reference foundational templates and apply customizations. Each project defines which pipelines to enable, GitHub repository settings, and agent customizations.

### State Layer (`state/projects/`)

Runtime GitHub state including project IDs, board IDs, column IDs, and sync status. This layer is managed automatically by the orchestrator and enables configuration reconciliation.

### Configuration Reconciliation

The orchestrator implements a reconciliation loop that ensures GitHub project boards match the desired configuration. On startup and when configuration changes, the system:

1. Compares current GitHub state with desired configuration
2. Creates missing project boards and columns
3. Updates existing boards to match configuration
4. Creates repository labels for pipeline routing
5. Marks state as synchronized

This eliminates the need for manual setup scripts and makes the orchestrator the authoritative source for project structure.

## Sequential pipeline architecture

The sequential orchestration pattern ensures agents process outputs from previous agents in a predefined order, with each stage building through progressive refinement. This deterministic flow control with defined handoff points enables quality-focused processing while maintaining clear dependencies between stages.

**State management employs distributed patterns** with coordinated checkpointing across all agents. The StateManager class persists agent state to durable storage with versioning, creating checkpoints at major phase completions. Recovery mechanisms support both backward recovery to stable checkpoints and forward recovery through error correction, minimizing lost work during failures.

The two-board Kanban architecture separates pre-SDLC activities (requirements, design, planning) from SDLC proper (development, testing, deployment). Pre-SDLC columns progress from Backlog through Requirements Analysis and Design to Ready for Development. SDLC columns flow from Development through Code Review, Testing, and Deployment to Done. Each transition triggers specific agent handoffs with context preservation.

## Mono-repo organization with CLAUDE.md

The hierarchical configuration structure places CLAUDE.md files strategically throughout the mono-repo. The root-level file contains global agent configuration and project overview. Technology-specific configurations reside in `ux/CLAUDE.md` for frontend agents and `backend/CLAUDE.md` for backend specialists. This structure enables tech-stack agnostic agents to read appropriate configurations based on working directory.

Each CLAUDE.md file follows a consistent pattern: architecture overview, workflow commands, agent guidelines, handoff protocols, error recovery procedures, and state management configuration. **Checkpoint frequency defaults to every major phase completion**, with backward recovery to the last stable state on failure. The configuration includes specific tool commands, formatting requirements, and validation criteria relevant to each component.

## Role-specific agent implementations

### Business Analyst Agent
Focuses on requirements gathering and user story creation, operating with CBAP certification-level expertise. Uses INVEST principles for user stories and Given-When-Then format for acceptance criteria. Outputs include Business Requirements Documents, user stories, and process flow diagrams.

### Product Manager Agent  
Employs RICE framework for feature prioritization, balancing Reach, Impact, Confidence, and Effort. Creates product roadmaps, conducts market analysis, and aligns stakeholders around OKRs. Outputs prioritization matrices and stakeholder communication summaries.

### Requirements Reviewer Agent
Validates requirements against the 5Cs: Clear, Concise, Complete, Consistent, and Correct. Identifies ambiguities, gaps, and conflicts while ensuring testability. Produces review reports with severity-categorized issues and specific improvement recommendations.

### Software Architect Agent
Designs systems considering scalability, maintainability, performance, and security. Creates Architecture Decision Records (ADRs) with trade-off analyses. Generates C4 model diagrams, API specifications, and performance plans.

### Architecture Reviewer Agent
Validates designs against patterns, security standards, and scalability requirements. Performs vulnerability assessments and performance analysis. Outputs include compliance checklists and prioritized improvement recommendations.

### Senior Software Engineer Agent
Implements clean code following SOLID principles, DRY, KISS, and YAGNI. Maintains >80% test coverage with comprehensive error handling. Produces well-structured source code with documentation and performance benchmarks.

### Code Reviewer Agent
Categorizes issues as Must Fix, Should Fix, Consider, or Nitpick. Integrates static analysis tools and security scanners. Provides line-specific feedback with severity levels and fix suggestions.

### Senior QA Engineer Agent
Develops comprehensive test strategies across unit, integration, system, and acceptance levels. Designs test cases using equivalence partitioning and boundary analysis. Creates automated test suites with performance baselines.

### Technical Writer Agent
Generates API documentation from OpenAPI specifications, creates user guides and tutorials, and maintains knowledge bases. Follows documentation standards for clarity, accuracy, and completeness.

## Handoff mechanisms between agents

The context transfer protocol structures handoffs with clear specifications. Each handoff includes the originating agent, target agent, task context with completed work and decisions made, deliverables with quality metrics, and next steps with required actions and constraints. This JSON-based protocol ensures no information is lost during transitions.

**Quality gates enforce standards** before progression, with each agent validating input from the previous stage. The QualityGate class evaluates outputs against thresholds, requesting revisions when quality falls below acceptable levels. This approach prioritizes quality over speed, ensuring compound improvements over time.

## Long-running session management

Session persistence leverages the Claude Code SDK's built-in capabilities for conversation history preservation and tool usage logging. The `claude --resume session-abc123` command continues specific conversations, while `claude --continue` resumes the most recent session. Headless mode (`claude -p "task" --output-format stream-json`) enables full automation.

**Memory management strategies** include automatic context summarization when approaching limits, recursive loading based on directory access, and intelligent tool usage tracking. The system preserves context during tool calls while implementing cost-aware optimization, balancing cheaper models for routine tasks with premium models for complex reasoning.

Error recovery employs multiple layers: model-level reasoning for logical errors, tool-level retries with exponential backoff, system-level handling for resource exhaustion, and application-level custom strategies. State preservation during recovery ensures minimal work loss, with detailed logging enabling debugging and continuous improvement.

## Docker containerization for development isolation

The Docker-based workflow encapsulates each agent's environment, mounting the workspace, Docker socket, git configuration, and SSH keys as volumes. The container includes all necessary tools: git, GitHub CLI, development frameworks, and the Claude Code SDK itself.

Docker Compose orchestrates multiple services, including the main Claude agent container and webhook listeners for GitHub integration. Environment variables manage authentication tokens and configuration, while working directories ensure agents operate in the correct context. This containerization enables consistent, reproducible agent behavior across different host systems.

## Making agents tech-stack agnostic

Configuration-driven architecture enables agents to adapt to different technology stacks dynamically. The project configuration schema specifies language, framework, architecture pattern, testing framework, and deployment target. Agents load appropriate tools based on this configuration: Python projects load Pylint and pytest, while Java projects load Checkstyle and JUnit.

The Agent Protocol specification provides standardized communication across frameworks. POST endpoints create tasks and execute steps, while GET endpoints list and monitor progress. This protocol ensures agents remain portable across different technology stacks while maintaining consistent interfaces.