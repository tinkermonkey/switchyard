# Backlog Requirements Refinement - SDLC Execution Pipeline

## Overview

Issues in the SDLC Execution Backlog can be refined through on-demand interaction with the Business Analyst agent using @mentions, enabling requirements clarification while maintaining strict safety controls.

## Configuration

### Workflow Column Settings

```yaml
# config/foundations/workflows.yaml
sdlc_execution_workflow:
  columns:
    - name: "Backlog"
      stage_mapping: null
      agent: null  # No automatic agent assignment
      description: "Phase-specific implementation tasks"
      automation_rules:
        - trigger: "item_created"
          action: "assign_label"
          parameters:
            labels: ["pipeline:sdlc-execution"]
```

**Key Design Decision**: The Backlog column does NOT have an assigned agent. This prevents automatic execution when issues are created or moved to the backlog. Instead, the Business Analyst is invoked on-demand via @mentions.

### Agent Safety Configuration

The business_analyst agent has built-in safety measures:

```yaml
# config/foundations/agents.yaml
business_analyst:
  description: "Requirements analysis and user story development"
  model: "claude-sonnet-4-5-20250929"
  timeout: 300
  retries: 3
  makes_code_changes: false
  filesystem_write_allowed: false
  requires_dev_container: false
  requires_docker: true
```

## Safety Guarantees

### 1. Read-Only Filesystem

The agent runs with a read-only workspace mount enforced at the Docker level:

```python
# claude/docker_runner.py (lines 175-177)
workspace_mount_mode = 'rw' if filesystem_write_allowed else 'ro'
if not filesystem_write_allowed:
    logger.warning(f"Agent {agent} has filesystem_write_allowed=false, mounting workspace as READ-ONLY")
```

Docker volume mount: `/workspace:ro`

### 2. No Code Changes

The `makes_code_changes: false` flag indicates the agent is not designed to modify code. Combined with the read-only filesystem, this provides defense in depth:

- **Configuration level**: Agent marked as non-code-modifying
- **Filesystem level**: Docker enforces read-only mount
- **Prompt level**: Agent instructions emphasize posting to GitHub, not creating files

### 3. Docker Isolation

The agent runs in an isolated Docker container with:
- Network isolation (orchestrator network only)
- No dev container access (doesn't need project dependencies)
- Limited volume mounts (workspace, SSH keys, git config - all read-only except orchestrator-specific paths)

## Usage

### 1. Create or Move Issue to Backlog

When an issue is created or moved to the SDLC Execution Backlog column:
- Automatically labeled with `pipeline:sdlc-execution`
- Issue sits in backlog **waiting for manual action**
- **No automatic agent execution** occurs

### 2. Invoke Business Analyst On-Demand

To refine requirements for a specific issue, add a comment mentioning the orchestrator:

```
@orchestrator-bot Please analyze the requirements for this issue and clarify the authentication workflow.
```

or

```
@orchestrator-bot Can you break down the acceptance criteria for this feature?
```

The mention can include:
- Specific questions about requirements
- Requests for clarification
- Asks for acceptance criteria refinement
- Questions about edge cases

### 3. Agent Response

When mentioned, the system will:
1. Detect the @orchestrator-bot mention in the issue comment
2. Route to the Business Analyst agent (based on issue labels/column)
3. Agent reads the issue context (title, body, comments, codebase)
4. Agent posts analysis and clarifications as GitHub comments
5. Conversation can continue with additional @mentions

The business_analyst agent will:
- Read the issue title, body, and all comments
- Review relevant codebase files (read-only access only)
- Post analysis and clarifications as GitHub comment replies
- Maintain conversation thread context across multiple interactions

### 4. Continue Refinement (Optional)

Continue the conversation as needed:

```
@orchestrator-bot Thanks! Can you also clarify how this integrates with the existing auth system?
```

The agent maintains context from previous messages in the thread.

### 5. Move to Development

Once requirements are refined and clear, manually move the issue to the Development column to begin automated implementation.

## What the Agent Can Do

- Read all files in the workspace
- Search codebase for context
- Access GitHub API (read issues, post comments, update descriptions)
- Perform web searches for research
- Read git history and status

## What the Agent Cannot Do

- Create files in the workspace
- Modify existing files
- Delete files
- Execute code or tests
- Build artifacts
- Push git commits

## Interaction Model

**On-Demand Only**: The Business Analyst agent is only invoked when explicitly mentioned via `@orchestrator-bot` in issue comments. There is no automatic execution, polling, or timeout.

**Stateless Conversations**: Each @mention creates a new agent invocation. The agent reads the full issue thread for context but doesn't maintain persistent state between invocations.

**No Automatic Triggers**: Moving issues to/from the Backlog column does NOT trigger agent execution.

## Error Handling

If the agent attempts to create a file, the operation will fail:

```
Error: Read-only file system
```

The agent's prompt instructs it to post findings to GitHub instead:

```
IMPORTANT: Output your analysis as text directly in your response.
DO NOT create any files. This analysis will be posted to GitHub as a comment.
```

## Integration with Rest of Pipeline

After backlog refinement in the conversational column:

1. **Backlog** (conversational) - Requirements refinement with Business Analyst
2. **Development** - Senior Software Engineer implements code
3. **Code Review** - Code Reviewer validates implementation
4. **Testing** - QA Engineer creates tests
5. **QA Review** - QA Reviewer validates tests
6. **Staged** (conversational) - Final human review before production
7. **Done** - Completed

## Benefits

1. **Early Clarification**: Catch ambiguous requirements before implementation
2. **Reduced Rework**: Clear requirements reduce implementation errors
3. **Safe Interaction**: Read-only mode prevents accidental code changes
4. **Contextual Analysis**: Agent can read codebase to provide informed clarifications
5. **GitHub-Native**: All conversations visible in issue comments

## Related Documentation

- [Filesystem Write Protection](./filesystem_write_protection.md)
- [Agent Configuration](../config/foundations/agents.yaml)
- [Workflow Configuration](../config/foundations/workflows.yaml)
- [Conversational Loop Architecture](./unified_conversational_loop_status.md)
