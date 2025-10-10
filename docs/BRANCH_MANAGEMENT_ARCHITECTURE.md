# Branch Management Architecture

## Overview

Branch management in the orchestrator is **fully centralized** through a single code path. All git branch operations MUST go through the `FeatureBranchManager` via the workspace abstraction layer.

## Architecture Diagram

```
User/Webhook Event
      ↓
Pattern Processor
      ↓
AgentExecutor.execute_agent()
      ↓
WorkspaceContextFactory.create()
      ↓
┌─────────────────────┴──────────────────────┐
↓                                             ↓
IssuesWorkspaceContext              HybridWorkspaceContext
↓                                             ↓
prepare_execution()                  prepare_execution()
↓                                             ↓
└────────────────┬────────────────────────────┘
                 ↓
    FeatureBranchManager.prepare_feature_branch()
                 ↓
           Git Operations
```

## The Single Entry Point

**File**: `services/feature_branch_manager.py`

**Method**: `prepare_feature_branch(project, issue_number, github_integration, issue_title)`

This is the **ONLY** method that creates or checks out feature branches. All other code paths must use this.

## How It Works

### 1. Workspace Abstraction Layer

Location: `services/workspace/`

Three workspace types:
- **IssuesWorkspaceContext** - Full git operations for development work
- **DiscussionsWorkspaceContext** - No git operations, discussion-only
- **HybridWorkspaceContext** - Dynamically switches between discussions and issues

### 2. Agent Executor Integration

Location: `services/agent_executor.py:83-120`

```python
# Automatic workspace preparation
workspace_context = WorkspaceContextFactory.create(
    workspace_type=task_context.get('workspace_type', 'issues'),
    project=project_name,
    issue_number=task_context['issue_number'],
    task_context=task_context,
    github_integration=gh_integration
)

# This calls prepare_feature_branch internally
prep_result = await workspace_context.prepare_execution()
task_context.update(prep_result)  # Adds branch_name to context
```

### 3. Feature Branch Manager Logic

Location: `services/feature_branch_manager.py:421-529`

The prepare_feature_branch method:

1. **Validates issue** (lines 436-452)
   - Checks issue_number > 0
   - Verifies issue exists via GitHub API
   - Fetches actual issue title

2. **Determines parent issue** (line 455)
   - Checks if issue is a sub-issue
   - Returns parent issue number if found

3. **Handles standalone issues** (lines 506-529)
   - Detects conflicting branches (reuses existing)
   - Creates new branch if needed
   - Returns branch name

4. **Handles parent/sub-issue workflows** (lines 531+)
   - Gets or creates shared feature branch
   - Adds sub-issue to tracking
   - Pulls latest changes
   - Returns shared branch name

## Validation & Safety Features

### Issue Validation (Lines 261-263, 436-452)

```python
# Rejects invalid issue numbers
if issue_number <= 0:
    raise ValueError(f"Invalid issue number: {issue_number}")

# Verifies issue exists
issue_data = await github_integration.get_issue(issue_number)
if not issue_data:
    raise ValueError(f"Issue #{issue_number} does not exist")
```

### Branch Conflict Detection (Lines 366-381, 510-520)

```python
# Finds existing branches for the same issue
conflicting_branches = await self.find_conflicting_branches(project_dir, issue_number)
if conflicting_branches:
    # Reuse existing branch instead of creating duplicate
    branch_name = conflicting_branches[0]
    await self.git_checkout(project_dir, branch_name)
    return branch_name
```

### Branch Name Sanitization (Lines 265-274)

```python
# Removes special characters
sanitized_title = "".join(c for c in title if c.isalnum() or c == "-")
# Strips trailing/leading dashes
sanitized_title = sanitized_title.strip("-")
# Collapses multiple dashes
while "--" in sanitized_title:
    sanitized_title = sanitized_title.replace("--", "-")
```

## Usage Patterns

### For Most Agents (Development Work)

Agents don't need to do anything! The `AgentExecutor` automatically:
1. Prepares the workspace
2. Checks out the correct branch
3. Adds branch_name to context
4. Commits and pushes changes after execution

### For Agents That Need Current Branch

If an agent needs to know what branch it's on:

```python
from services.feature_branch_manager import feature_branch_manager

current_branch = await feature_branch_manager.get_current_branch(project_dir)
```

### For Manual Branch Operations (Testing/Scripts)

```python
from services.feature_branch_manager import feature_branch_manager
from services.github_integration import GitHubIntegration

github = GitHubIntegration(repo_owner="owner", repo_name="repo")

# Prepare feature branch (creates or checks out)
branch_name = await feature_branch_manager.prepare_feature_branch(
    project="context-studio",
    issue_number=125,
    github_integration=github,
    issue_title="Phase 1: Database schema"
)

# Work happens here...

# Finalize (commit, push, create PR)
result = await feature_branch_manager.finalize_feature_branch_work(
    project="context-studio",
    issue_number=125,
    commit_message="Complete work for issue #125",
    github_integration=github
)
```

## Maintenance Operations

### Detect and Clean Invalid Branches

```python
from services.feature_branch_manager import feature_branch_manager
from services.github_integration import GitHubIntegration

github = GitHubIntegration(repo_owner="owner", repo_name="repo")

# Finds branches with invalid issue numbers (≤0) or non-existent issues
result = await feature_branch_manager.detect_and_clean_invalid_branches(
    project="context-studio",
    project_dir="/workspace/context-studio",
    github_integration=github
)

print(f"Cleaned: {result['cleaned']}")
print(f"Errors: {result['errors']}")
```

### Cleanup Orphaned Branches (Periodic Task)

```python
# Run this periodically (daily cron job)
await feature_branch_manager.cleanup_orphaned_branches(
    project="context-studio",
    github_integration=github
)
```

## Common Issues & Solutions

### Issue: Duplicate branches for same issue

**Cause**: Multiple agents working on same issue simultaneously
**Solution**: Branch conflict detection (lines 510-520) now reuses existing branches

### Issue: Branches with invalid issue numbers (e.g., issue-0)

**Cause**: Invalid input passed to prepare_feature_branch
**Solution**: Issue validation (lines 436-452) now rejects issue_number ≤ 0

### Issue: Branch names with weird characters

**Cause**: Issue titles with special characters
**Solution**: Branch name sanitization (lines 265-274)

### Issue: Agent doesn't know which branch it's on

**Cause**: Agent needs current branch without creating new one
**Solution**: Use `get_current_branch()` method (lines 334-345)

## Testing

Location: `tests/integration/test_feature_branch_workflow.py`

Run tests:
```bash
pytest tests/integration/test_feature_branch_workflow.py -v
```

## DO NOT Do These Things

❌ **Never** create branches directly with `git branch` or `git checkout -b`
❌ **Never** bypass the workspace abstraction layer
❌ **Never** create branch logic in agent code
❌ **Never** assume issue_number is valid without checking

✅ **Always** use `FeatureBranchManager.prepare_feature_branch()`
✅ **Always** go through the workspace abstraction
✅ **Always** let `AgentExecutor` handle branch setup
✅ **Always** validate issue numbers

## File Reference

Core files:
- `services/feature_branch_manager.py` - Branch management logic
- `services/agent_executor.py` - Agent execution with workspace setup
- `services/workspace/issues_context.py` - Issues workspace
- `services/workspace/hybrid_context.py` - Hybrid workspace
- `services/workspace/discussions_context.py` - Discussions workspace
- `services/git_workflow_manager.py` - Low-level git operations

## Summary

Branch management is **centralized and safe**. All operations go through:

1. WorkspaceContext (abstraction)
2. FeatureBranchManager (validation + logic)
3. GitWorkflowManager (git commands)

This three-layer architecture ensures:
- No duplicate branches
- No invalid branches
- Consistent branch naming
- Proper parent/sub-issue coordination
- Safe concurrent agent execution
