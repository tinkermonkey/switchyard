# Agent Team Maintainer System

## Overview

The Agent Team Maintainer is a system that automatically generates and maintains project-specific agents and skills for managed projects in the orchestrator. It analyzes project codebases to understand their unique architecture, tech stack, and patterns, then creates tailored AI agents that understand each project's specifics.

**Status**: 🔄 In Development

- ✅ Sprint 1 (Foundation) - Complete
- 📋 Sprint 2 (Analysis) - Planned
- 📋 Sprint 3 (Generation) - Planned
- 📋 Sprint 4 (Validation & Lifecycle) - Planned
- 📋 Sprint 5 (Integration) - Planned

## Problem Statement

The orchestrator currently has 11 registered agents for orchestration tasks (dev environment setup, code review, architecture design, etc.). However, **managed projects lack project-specific agents and skills** that understand their unique:

- Architecture patterns and design decisions
- Tech stack and key dependencies
- Testing, deployment, and debugging procedures
- Project-specific conventions and patterns

## Solution

Build an **Agent Team Maintainer** system that:

1. **Analyzes** each project's codebase to understand architecture, tech stack, and patterns
2. **Generates** project-specific agents with deep codebase knowledge
3. **Generates** project-specific skills (quick-reference guides for common tasks)
4. **Maintains** clear ownership boundaries (generated vs manual artifacts)
5. **Updates/deletes** outdated artifacts as codebases evolve
6. **Coordinates** with Docker image rebuilds when needed

This is analogous to `dev_environment_setup` (maintains project Docker images) but for **agent/skill definitions** instead of dependencies.

## Architecture

### Component Structure

```
┌──────────────────────────────────────────────────────────┐
│  scripts/maintain_agent_team.py                          │
│  (Main coordinator - runs on host, outside Docker)       │
│                                                           │
│  - Project discovery via config_manager                  │
│  - Change detection via hash calculation                 │
│  - Orchestrate analysis → generation → validation        │
│  - Manage lifecycle (update, cleanup, archiving)         │
│  - Coordinate with Docker image rebuilds                 │
└────────────┬─────────────────────────────────────────────┘
             │
             ├─── Calls Claude Code agents (via subprocess)
             │
             ▼
┌──────────────────────────────────────────────────────────┐
│  Specialized Analysis Agents (Future)                    │
│  (Run via Claude Code, like dev_environment_setup)       │
│                                                           │
│  1. Codebase Analyzer - Analyze structure & patterns     │
│  2. Strategy Generator - Determine what to create        │
│  3. Agent/Skill Generators - Populate templates          │
│  4. Validation Agent - Validate syntax & completeness    │
└──────────────────────────────────────────────────────────┘
             │
             ▼
┌──────────────────────────────────────────────────────────┐
│  Storage: .claude/ and state/projects/                   │
│                                                           │
│  .claude/generated/manifest.yaml         [AUTO]          │
│  .claude/agents/<project>-*.md           [GENERATED]     │
│  .claude/skills/<project>-*/             [GENERATED]     │
│  state/projects/<project>/               [AUTO]          │
│    └── agent_generation_state.yaml                       │
└──────────────────────────────────────────────────────────┘
```

### Key Design Decisions

- **Centralized storage** in orchestrator's `.claude/` for simpler management
- **Project prefix naming** (`<project>-<capability>`) for clear ownership
- **Generated flag** in YAML frontmatter to distinguish from manual artifacts
- **Manifest tracking** for lifecycle management
- **State files** to track generation history and detect changes

## Sprint 1: Foundation (✅ Complete)

### Implemented Components

#### 1. Main Coordinator Script (`scripts/maintain_agent_team.py`)

**Features**:
- Project discovery using `config_manager.list_visible_projects()`
- Change detection via hash calculation of critical files
- Manifest and state file management
- Command-line interface with argparse
- Dry-run mode for safe previewing
- Follows `rebuild_project_images.py` pattern

**Functions**:
```python
def discover_projects_for_generation(project_filter: str = None) -> List[str]
    # Discover projects needing agent generation
    # Exclude clauditoreum (orchestrator itself)

def detect_codebase_changes(project: str) -> Dict[str, Any]
    # Calculate hash of critical files
    # Compare with previous hash from state
    # Return change report

def calculate_codebase_hash(project: str) -> str
    # Hash: requirements.txt, package.json, CLAUDE.md, etc.
    # Include directory structure (top 2 levels)

def run_generation_workflow(project: str, args) -> Dict[str, Any]
    # Main workflow: analyze → generate → validate → deploy
    # (Implementation pending Sprints 2-4)

def load_manifest() -> Dict[str, Any]
    # Load generation manifest from .claude/generated/manifest.yaml

def save_manifest(manifest: Dict[str, Any])
    # Save manifest with timestamp update

def load_project_state(project: str) -> Dict[str, Any]
    # Load state from state/projects/<project>/agent_generation_state.yaml

def save_project_state(project: str, state: Dict[str, Any])
    # Save state with timestamp update
```

#### 2. Directory Structure

```
.claude/
├── generated/
│   ├── manifest.yaml          # Central inventory (✅ Created)
│   └── README.md              # Documentation (✅ Created)
├── agents/                    # Agent definitions (existing + future generated)
└── skills/                    # Skill definitions (existing + future generated)

state/projects/<project>/
└── agent_generation_state.yaml    # Per-project state (auto-created)
```

#### 3. Manifest File (`.claude/generated/manifest.yaml`)

Structure:
```yaml
version: '1.0'
last_updated: "2026-02-12T14:30:00Z"

projects:
  context-studio:
    last_generation: "2026-02-12T14:30:00Z"
    generation_hash: "abc123..."
    agents:
      - name: context-studio-tester
        file: agents/context-studio-tester.md
        purpose: "Test execution"
    skills:
      - name: context-studio-test
        directory: skills/context-studio-test/
        purpose: "Run tests"
```

#### 4. State Tracking (`state/projects/<project>/agent_generation_state.yaml`)

Structure:
```yaml
version: '1.0'
project: context-studio
last_updated: "2026-02-12T14:30:00Z"

codebase:
  analysis_hash: "abc123..."          # Hash of critical files
  analysis_timestamp: "2026-02-12T14:00:00Z"
  tech_stack:
    backend: "python, fastapi"
    frontend: "react, typescript"

generations:
  - id: "gen-20260212-143000"
    timestamp: "2026-02-12T14:30:00Z"
    trigger: "manual"
    mode: "incremental"
    artifacts_created: 4
    artifacts_updated: 2
    success: true

artifacts:
  agents: [...]
  skills: [...]

maintenance:
  next_analysis: "2026-02-19T14:00:00Z"
  auto_regenerate: false
```

#### 5. Unit Tests (`tests/unit/test_maintain_agent_team.py`)

**Test Coverage** (17 tests, all passing):
- ✅ Project discovery (all, specific, nonexistent)
- ✅ Hash calculation (basic, change detection, missing)
- ✅ Change detection (initial, no changes, modified)
- ✅ Manifest management (load, save, timestamp update)
- ✅ State tracking (load, save, timestamp update, directory creation)
- ✅ Directory setup

### Command-Line Interface

```bash
# Basic usage: analyze and generate for all projects
python scripts/maintain_agent_team.py

# Specific project
python scripts/maintain_agent_team.py --project context-studio

# Dry run (preview without making changes)
python scripts/maintain_agent_team.py --dry-run

# Auto-approve strategy (no interactive prompts)
python scripts/maintain_agent_team.py --auto-approve

# Cleanup outdated artifacts
python scripts/maintain_agent_team.py --cleanup

# Full workflow: analyze → generate → cleanup → rebuild Docker images
python scripts/maintain_agent_team.py --auto-approve --cleanup --rebuild-images
```

### Verification

#### Test Results
```bash
$ python -m pytest tests/unit/test_maintain_agent_team.py -v
============================== test session starts ==============================
collected 17 items

tests/unit/test_maintain_agent_team.py::TestProjectDiscovery::test_discover_all_projects PASSED
tests/unit/test_maintain_agent_team.py::TestProjectDiscovery::test_discover_specific_project PASSED
tests/unit/test_maintain_agent_team.py::TestProjectDiscovery::test_discover_nonexistent_project PASSED
tests/unit/test_maintain_agent_team.py::TestChangeDetection::test_calculate_hash_basic PASSED
tests/unit/test_maintain_agent_team.py::TestChangeDetection::test_calculate_hash_detects_change PASSED
tests/unit/test_maintain_agent_team.py::TestChangeDetection::test_calculate_hash_missing_project PASSED
tests/unit/test_maintain_agent_team.py::TestChangeDetection::test_detect_initial_generation PASSED
tests/unit/test_maintain_agent_team.py::TestChangeDetection::test_detect_no_changes PASSED
tests/unit/test_maintain_agent_team.py::TestChangeDetection::test_detect_codebase_modified PASSED
tests/unit/test_maintain_agent_team.py::TestManifestManagement::test_load_manifest_empty PASSED
tests/unit/test_maintain_agent_team.py::TestManifestManagement::test_save_and_load_manifest PASSED
tests/unit/test_maintain_agent_team.py::TestManifestManagement::test_save_manifest_updates_timestamp PASSED
tests/unit/test_maintain_agent_team.py::TestStateTracking::test_load_state_empty PASSED
tests/unit/test_maintain_agent_team.py::TestStateTracking::test_save_and_load_state PASSED
tests/unit/test_maintain_agent_team.py::TestStateTracking::test_save_state_updates_timestamp PASSED
tests/unit/test_maintain_agent_team.py::TestStateTracking::test_save_state_creates_directory PASSED
tests/unit/test_maintain_agent_team.py::TestDirectorySetup::test_ensure_directories PASSED

============================== 17 passed in 0.07s ==============================
```

#### Dry-Run Test
```bash
$ python scripts/maintain_agent_team.py --dry-run
2026-02-12 08:20:03,432 - __main__ - INFO - Discovering projects for agent generation...
2026-02-12 08:20:03,465 - __main__ - INFO - Found 7 project(s) for agent generation:
2026-02-12 08:20:03,465 - __main__ - INFO -   - codetoreum
2026-02-12 08:20:03,465 - __main__ - INFO -   - context-studio
2026-02-12 08:20:03,465 - __main__ - INFO -   - documentation_robotics
2026-02-12 08:20:03,465 - __main__ - INFO -   - documentation_robotics_viewer
2026-02-12 08:20:03,465 - __main__ - INFO -   - rounds
2026-02-12 08:20:03,465 - __main__ - INFO -   - utterance_emitter
2026-02-12 08:20:03,465 - __main__ - INFO -   - what_am_i_watching
```

## Authentication Requirements

The Agent Team Maintainer uses Claude Code CLI via the orchestrator's infrastructure. **No separate Anthropic API subscription required**.

### Required Credentials

You need ONE of the following:

1. **Claude Code Subscription** (recommended):
   ```bash
   export CLAUDE_CODE_OAUTH_TOKEN="your-token-here"
   ```
   - Get token from Claude Code CLI: `claude auth login`
   - Subscription billing (cheaper for high usage)

2. **Anthropic API Key** (pay-per-use):
   ```bash
   export ANTHROPIC_API_KEY="sk-ant-..."
   ```
   - Get from: https://console.anthropic.com/
   - API billing (more expensive for high usage)

### How It Works

- Strategy generation uses `run_claude_code()` (local mode)
- No Docker container needed for analysis phase
- Reuses orchestrator's Claude Code CLI infrastructure
- Automatic token tracking and observability events

## Next Steps: Sprint 2 (Analysis)

### Planned Features

1. **Enhanced Codebase Analysis** (deterministic + LLM insights)
   - Deterministic analysis: Structure, dependencies, tech stacks (fast, ~2 min)
   - LLM-enhanced insights: Semantic understanding via Claude Code CLI (5-10 min)
   - Combined approach minimizes token usage and cost
   - Avoids the need for exhaustive code reading (6-phase, 60-70 minute approach)
   - Discovery: Read project config, list structure, identify tech stacks
   - Dependency Analysis: Parse requirements/package.json, find usage patterns
   - Architecture Detection: Identify layers, sample key files
   - Quality Patterns: Understand test structure, linting
   - LLM Strategy: Generate optimal agent/skill strategy from analysis

2. **Smart Sampling Algorithm** (don't read every file)
   - Priority 100: Base classes, interfaces
   - Priority 80: Entry points (main.py, app.py, routes.py)
   - Priority 70: Type definitions
   - Priority 60: Configuration files
   - Priority by size: Largest files likely contain core logic

3. **Analysis Output** (`codebase_analysis.json`)
   ```json
   {
     "tech_stacks": {...},
     "frameworks": ["fastapi", "react"],
     "dependencies": {"critical": [...]},
     "architecture": {
       "style": "multi-tier",
       "layers": [...],
       "design_patterns": [...]
     },
     "critical_workflows": [...],
     "quality_practices": {...}
   }
   ```

## Future Sprints

### Sprint 3: Generation
- Agent/skill templates with YAML frontmatter
- Template population from analysis
- Generation agents (Agent Generator, Skill Generator)
- Always generate: `<project>-architect`, `<project>-guardian`, `<project>-doc-maintainer`
- Tech-specific agents: `<project>-tester`, `<project>-deployer`

### Sprint 4: Validation & Lifecycle
- Validation stages: syntax, metadata, references, content, integration
- Update detection and incremental regeneration
- Cleanup strategy with archiving
- Safe deletion with confirmations

### Sprint 5: Integration
- Enhanced `rebuild_project_images.py` with `--regenerate-agents` flag
- Docker image coordination
- Wrapper scripts for convenience
- Documentation and production rollout

## Safety Mechanisms

1. **Generated Flag**: Only artifacts with `generated: true` in YAML frontmatter can be auto-deleted
2. **Project Prefix**: Generated artifacts use `<project>-*` naming for clear ownership
3. **Manifest Cross-Reference**: Deletion requires manifest entry
4. **Archiving**: All deletions move to `.claude/archives/<project>/<timestamp>/` (not hard delete)
5. **Dry-Run Mode**: Preview changes before applying
6. **User Confirmation**: Interactive prompts for risky operations (unless `--force`)
7. **State Tracking**: All generations tracked in state files for audit trail

## Design Rationale

### Why Centralized Storage?

**Decision**: Store all generated artifacts in orchestrator's `.claude/` directory (not per-project)

**Rationale**:
- Simpler management: One location to track all generated artifacts
- Easier cleanup: Don't need to traverse multiple project directories
- Clearer ownership: Generated artifacts live with orchestrator, not in projects
- Follows existing pattern: Manual agents/skills already in orchestrator's `.claude/`

### Why Project Prefix Naming?

**Decision**: Use `<project>-<capability>` naming (e.g., `context-studio-tester`)

**Rationale**:
- Clear ownership: Immediately obvious which project an artifact belongs to
- Prevents conflicts: No naming collisions between projects
- Easy filtering: Can glob `context-studio-*` to find all artifacts
- Human-readable: Self-documenting artifact purpose

### Why Hash-Based Change Detection?

**Decision**: Calculate hash of critical files to detect codebase changes

**Rationale**:
- Efficient: Fast hash calculation without deep analysis
- Reliable: Detects any change to critical files
- Simple: No complex diffing or change tracking needed
- Incremental: Only regenerate when changes detected

## References

- Original Implementation Plan: See conversation history for complete plan
- Main Script: `scripts/maintain_agent_team.py`
- Unit Tests: `tests/unit/test_maintain_agent_team.py`
- Documentation: `.claude/generated/README.md`
- Manifest: `.claude/generated/manifest.yaml`
- State Files: `state/projects/<project>/agent_generation_state.yaml`
