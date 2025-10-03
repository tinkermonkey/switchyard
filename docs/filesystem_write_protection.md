# Filesystem Write Protection for Agents

## Overview

The orchestrator implements **read-only filesystem protection** for agents that should only post to GitHub and not create local files. This prevents agents from creating unexpected files that disappear from user visibility.

## Problem

Analysis and review agents (business analyst, requirements reviewer, etc.) were creating local markdown files instead of posting their output to GitHub discussions/issues. This caused:

1. **Invisible outputs**: Users couldn't see the analysis because it was in a local file
2. **Lost context**: Subsequent agents referenced files that weren't in GitHub
3. **Inconsistent workflow**: Some outputs in GitHub, some in files

## Solution

### Configuration Flag: `filesystem_write_allowed`

Each agent now has a `filesystem_write_allowed` configuration flag:

```yaml
# config/foundations/agents.yaml
agents:
  business_analyst:
    filesystem_write_allowed: false  # Read-only workspace

  senior_software_engineer:
    filesystem_write_allowed: true   # Can write code
```

**Default:** `true` (backward compatible)

### Docker Mount Enforcement

When `filesystem_write_allowed: false`, the Docker runner mounts the workspace as **read-only** (`:ro`):

```python
# claude/docker_runner.py
workspace_mount_mode = 'rw' if filesystem_write_allowed else 'ro'
cmd.extend(['-v', f'{host_project_path}:/workspace:{workspace_mount_mode}'])
```

## Agent Categories

### Analysis/Review Agents (Read-Only)

These agents post to GitHub and should NOT create files:

- **business_analyst**: Posts analysis to discussions
- **requirements_reviewer**: Posts review feedback to discussions
- **design_reviewer**: Posts architecture reviews to discussions
- **code_reviewer**: Posts code reviews to issues/PRs
- **test_reviewer**: Posts test plan reviews to discussions
- **documentation_editor**: Posts editorial feedback to PRs

**Configuration:**
```yaml
filesystem_write_allowed: false
makes_code_changes: false
```

### Implementation Agents (Read-Write)

These agents create actual deliverables and need write access:

- **senior_software_engineer**: Writes code files
- **senior_qa_engineer**: Writes test files
- **software_architect**: Creates architecture diagrams/docs (committed to repo)
- **technical_writer**: Creates documentation files (committed to repo)
- **dev_environment_setup**: Creates Dockerfiles, requirements.txt

**Configuration:**
```yaml
filesystem_write_allowed: true
makes_code_changes: true
```

## How It Works

### 1. Agent Configuration (config/manager.py)

```python
@dataclass
class AgentConfig:
    filesystem_write_allowed: bool = True  # Default to True
```

### 2. Docker Runner Checks Config (claude/docker_runner.py)

```python
def _build_docker_command(self, ...):
    agent_config = config_manager.get_project_agent_config(project, agent)
    filesystem_write_allowed = getattr(agent_config, 'filesystem_write_allowed', True)

    workspace_mount_mode = 'rw' if filesystem_write_allowed else 'ro'

    if not filesystem_write_allowed:
        logger.warning(f"Agent {agent} has filesystem_write_allowed=false, mounting workspace as READ-ONLY")

    cmd.extend(['-v', f'{host_project_path}:/workspace:{workspace_mount_mode}'])
```

### 3. Enforcement at Runtime

When an agent tries to create a file in a read-only workspace:

**Without protection:**
```
Agent creates business_analysis.md
User never sees it (file is local)
```

**With protection:**
```
Agent tries: Write("business_analysis.md", content)
Docker: Permission denied - Read-only file system
Agent: Cannot create file, posting to GitHub instead
```

## Logging

The Docker runner logs filesystem mode:

```
INFO - Mounting project: container=/workspace/context-studio, host=/Users/user/workspace/orchestrator/context-studio
WARNING - Agent business_analyst has filesystem_write_allowed=false, mounting workspace as READ-ONLY
INFO - Using Docker image: clauditoreum-orchestrator:latest for agent business_analyst
```

## Testing

### Manual Test

Run the test suite:

```bash
python tests/test_readonly_filesystem.py
```

**Test 1:** Business analyst cannot create files
**Test 2:** Software engineer can create files

### Expected Output

```
================================================================================
Read-Only Filesystem Enforcement Test Suite
================================================================================

Agent: business_analyst
filesystem_write_allowed: False
✅ Agent correctly configured with filesystem_write_allowed=false

Running agent with file creation prompt...
Agent output: I cannot create files in this workspace as it is read-only...
✅ PASS: File was NOT created (expected behavior)
✅ PASS: Agent received filesystem permission error

Agent: senior_software_engineer
filesystem_write_allowed: True
✅ Agent correctly configured with filesystem_write_allowed=true
✅ PASS: Code agent successfully created file

Test Summary
Test 1 (Read-Only Enforcement): ✅ PASS
Test 2 (Read-Write for Code Agents): ✅ PASS

🎉 All tests passed!
```

## Configuration Examples

### Analysis Agent (Read-Only)

```yaml
business_analyst:
  description: "Requirements analysis and user story development"
  model: "claude-sonnet-4-5-20250929"
  timeout: 300
  retries: 3
  makes_code_changes: false
  requires_dev_container: false
  filesystem_write_allowed: false  # ← Read-only workspace
  capabilities:
    - requirements_analysis
    - user_story_creation
  tools_enabled:
    - git_integration
    - web_search
  # Note: file_operations tool can still READ files
```

### Code Agent (Read-Write)

```yaml
senior_software_engineer:
  description: "Software development and implementation"
  model: "claude-sonnet-4-5-20250929"
  timeout: 900
  retries: 3
  makes_code_changes: true
  requires_dev_container: true
  filesystem_write_allowed: true  # ← Can write files
  capabilities:
    - software_development
    - code_implementation
  tools_enabled:
    - file_operations
    - git_integration
```

## Agent Prompt Guidelines

For agents with `filesystem_write_allowed: false`, update prompts to emphasize posting to GitHub:

```python
# agents/business_analyst_agent.py
prompt = f"""
Analyze the following issue/requirement for project {project}:

Title: {issue.get('title')}
Description: {issue.get('body')}

IMPORTANT: Output your analysis as text directly in your response.
DO NOT create any files. This analysis will be posted to GitHub as a comment.

Provide a comprehensive business analysis following the format specified in your configuration.
"""
```

## Troubleshooting

### Agent Creates Files Despite Read-Only Mount

**Symptom:** Files appear in workspace even with `filesystem_write_allowed: false`

**Possible Causes:**
1. Agent configuration not loaded correctly
2. Docker mount mode not applied
3. Agent running outside Docker (local mode)

**Debug Steps:**
```bash
# Check agent config
python -c "from config.manager import config_manager; print(config_manager.get_project_agent_config('context-studio', 'business_analyst').filesystem_write_allowed)"

# Check Docker container mounts
docker inspect <container-name> | grep -A5 Mounts

# Check logs for mount mode
grep "mounting workspace" orchestrator_logs.txt
```

### Agent Fails When It Should Write

**Symptom:** Code agent cannot create files

**Cause:** `filesystem_write_allowed` incorrectly set to `false`

**Fix:** Update `config/foundations/agents.yaml`:
```yaml
senior_software_engineer:
  filesystem_write_allowed: true  # Must be true for code agents
```

### Read-Only Still Allows Reads

**Note:** Read-only mode (`:ro`) only prevents **writes**. Agents can still:
- ✅ Read existing files
- ✅ List directories
- ✅ Search file contents
- ❌ Create new files
- ❌ Modify existing files
- ❌ Delete files

This is the desired behavior - analysis agents need to read code to analyze it.

## Future Enhancements

### Granular Permissions

Instead of binary read/write, implement granular permissions:

```yaml
filesystem_permissions:
  read: true
  write: false
  create: false
  delete: false
  directories:
    - "docs/**": write  # Can write to docs/ only
    - "src/**": read    # Read-only for source code
```

### Temporary Scratch Space

Allow read-only agents to write to a temporary directory:

```yaml
filesystem_write_allowed: false
scratch_directory: "/tmp/agent_scratch"  # Writable, not persisted
```

### Audit Logging

Log all file operations attempted by agents:

```json
{
  "agent": "business_analyst",
  "operation": "write",
  "path": "/workspace/analysis.md",
  "allowed": false,
  "blocked": true,
  "timestamp": "2025-10-02T12:00:00Z"
}
```

## Related Documentation

- [Agent Configuration](../config/foundations/agents.yaml)
- [Docker Runner](../claude/docker_runner.py)
- [Review Cycle Context Fixes](./review_cycle_fixes.md)
- [Configuration Management](./configuration_architecture.md)

## Summary

Read-only filesystem protection ensures:

1. ✅ **Visibility**: All agent outputs visible in GitHub
2. ✅ **Consistency**: Predictable agent behavior
3. ✅ **Control**: Fine-grained permissions per agent type
4. ✅ **Safety**: Prevents unexpected file creation
5. ✅ **Flexibility**: Code agents still have write access when needed

The system now enforces the principle: **Analysis agents post to GitHub, implementation agents write to files**.
