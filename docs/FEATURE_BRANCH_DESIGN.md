# Feature Branch Management - Parent/Sub-Issue Design

## Overview

Implement hierarchical branch management where:
- **Parent issues** get a shared feature branch
- **Sub-issues** all contribute to the parent's branch
- **One PR** accumulates all changes until all sub-issues complete
- **Git pulls** keep the branch current

## GitHub Issue Hierarchy

GitHub provides native parent/child relationships via Project tracking:

```graphql
issue {
  trackedIssues {        # Sub-issues this issue tracks (I'm the parent)
    nodes { number }
  }
  trackedInIssues {      # Parent issues tracking this (I'm the child)
    nodes { number }
  }
}
```

**Example Structure:**
```
Issue #50: User Authentication Feature (Parent)
├── Issue #51: Login form UI (Sub-issue)
├── Issue #52: Password validation (Sub-issue)
└── Issue #53: Session management (Sub-issue)
```

## Branch Strategy

### Branch Naming
```
feature/issue-{parent_number}-{sanitized-title}

Examples:
- feature/issue-50-user-authentication
- feature/issue-100-payment-integration
```

### Branch Lifecycle

1. **Creation**: When first sub-issue starts work
2. **Accumulation**: All sub-issues commit to same branch
3. **Completion**: When all sub-issues done, PR marked ready
4. **Cleanup**: After merge, branch auto-deleted

## State Management

### Feature Branch State
```yaml
# state/projects/{project}/feature_branches.yaml
feature_branches:
  - parent_issue: 50
    branch_name: "feature/issue-50-user-authentication"
    created_at: "2025-10-05T10:00:00Z"
    sub_issues:
      - number: 51
        status: "completed"
        completed_at: "2025-10-05T11:00:00Z"
      - number: 52
        status: "in_progress"
        started_at: "2025-10-05T11:30:00Z"
      - number: 53
        status: "pending"
    pr_number: 123
    pr_status: "draft"
    last_pull_at: "2025-10-05T11:30:00Z"
    commits_behind_main: 5
    last_updated: "2025-10-05T11:30:00Z"
```

### Fallback for Standalone Issues
If issue has no parent:
```
feature/issue-{issue_number}-{sanitized-title}
```

## Core Workflows

### 1. Before Agent Starts Work

```python
async def prepare_feature_branch(project: str, issue_number: int) -> str:
    """
    Get or create feature branch, checkout, and pull latest

    Returns: branch_name
    Raises: MergeConflictError if conflict detected
    """

    # Step 1: Determine parent issue
    parent_issue = await get_parent_issue(issue_number)

    if not parent_issue:
        # Standalone issue - create individual branch
        logger.info(f"Issue #{issue_number} has no parent - creating standalone branch")
        return await create_standalone_branch(project, issue_number)

    # Step 2: Get or create feature branch for parent
    feature_branch = get_feature_branch_state(project, parent_issue)

    if not feature_branch:
        # First sub-issue - create feature branch
        branch_name = create_feature_branch_name(parent_issue)
        await create_branch_from_main(project_dir, branch_name)

        feature_branch = create_feature_branch_state(
            project=project,
            parent_issue=parent_issue,
            branch_name=branch_name,
            sub_issues=[issue_number]
        )
        logger.info(f"Created feature branch {branch_name} for parent #{parent_issue}")
    else:
        # Add this sub-issue to tracking if not already
        add_sub_issue_to_branch(feature_branch, issue_number)

    # Step 3: Checkout branch
    await git_checkout(project_dir, feature_branch.branch_name)

    # Step 4: Pull latest changes (critical for multi-sub-issue coordination)
    try:
        await git_pull_rebase(project_dir)
        feature_branch.last_pull_at = datetime.now().isoformat()
        save_feature_branch_state(feature_branch)

        logger.info(f"Pulled latest changes for {feature_branch.branch_name}")

    except MergeConflictError as e:
        # Don't auto-resolve - escalate to human
        logger.error(f"Merge conflict in {feature_branch.branch_name}: {e.files}")
        await escalate_merge_conflict(issue_number, feature_branch.branch_name, e.files)
        raise

    # Step 5: Check if branch is stale
    commits_behind = await get_commits_behind_main(project_dir, feature_branch.branch_name)
    feature_branch.commits_behind_main = commits_behind

    if commits_behind > 50:
        await escalate_stale_branch(parent_issue, commits_behind)
    elif commits_behind > 20:
        logger.warning(f"Branch {feature_branch.branch_name} is {commits_behind} commits behind main")

    return feature_branch.branch_name
```

### 2. After Agent Completes Work

```python
async def finalize_feature_branch_work(
    project: str,
    issue_number: int,
    commit_message: str
) -> Dict[str, Any]:
    """
    Commit changes, push, update state, check completion

    Returns: dict with pr_url, all_complete, etc.
    """

    feature_branch = get_feature_branch_for_issue(project, issue_number)

    if not feature_branch:
        logger.error(f"No feature branch found for issue #{issue_number}")
        return {'success': False}

    # Step 1: Commit changes
    await git_add_all(project_dir)
    await git_commit(project_dir, commit_message)

    # Step 2: Push to remote
    await git_push(project_dir, feature_branch.branch_name)

    logger.info(f"Pushed changes for issue #{issue_number} to {feature_branch.branch_name}")

    # Step 3: Update sub-issue status
    mark_sub_issue_complete(feature_branch, issue_number)
    save_feature_branch_state(feature_branch)

    # Step 4: Create or update PR
    pr_result = await create_or_update_feature_pr(
        project=project,
        feature_branch=feature_branch
    )

    # Step 5: Check if all sub-issues complete
    all_complete = check_all_sub_issues_complete(feature_branch)

    if all_complete:
        logger.info(f"All sub-issues complete for parent #{feature_branch.parent_issue}")

        # Mark PR as ready for review
        await mark_pr_ready(project, feature_branch.pr_number)
        feature_branch.pr_status = "ready"
        save_feature_branch_state(feature_branch)

        # Post completion comment to parent issue
        await post_feature_completion_comment(feature_branch.parent_issue)

    return {
        'success': True,
        'branch_name': feature_branch.branch_name,
        'pr_url': pr_result.get('pr_url'),
        'all_complete': all_complete
    }
```

### 3. PR Management

```python
async def create_or_update_feature_pr(
    project: str,
    feature_branch: FeatureBranch
) -> Dict[str, Any]:
    """Create or update PR with current sub-issue status"""

    parent_issue = await get_issue_details(feature_branch.parent_issue)

    # Build PR body with sub-issue checklist
    pr_body = build_feature_pr_body(parent_issue, feature_branch)

    if not feature_branch.pr_number:
        # Create new PR as draft
        result = await create_pr(
            branch=feature_branch.branch_name,
            title=f"[Feature] {parent_issue.title}",
            body=pr_body,
            draft=True
        )

        feature_branch.pr_number = result['pr_number']
        feature_branch.pr_status = 'draft'
        save_feature_branch_state(feature_branch)

        logger.info(f"Created PR #{result['pr_number']} for parent #{feature_branch.parent_issue}")

        return result
    else:
        # Update existing PR description
        await update_pr_body(feature_branch.pr_number, pr_body)

        logger.info(f"Updated PR #{feature_branch.pr_number} with latest sub-issue status")

        return {'pr_number': feature_branch.pr_number}


def build_feature_pr_body(parent_issue, feature_branch) -> str:
    """Build PR description with sub-issue checklist"""

    lines = []
    lines.append(f"# Feature: {parent_issue.title}")
    lines.append("")
    lines.append(f"**Parent Issue:** #{feature_branch.parent_issue}")
    lines.append("")
    lines.append("## Sub-Issues Progress")

    for sub_issue in feature_branch.sub_issues:
        checkbox = "x" if sub_issue.status == "completed" else " "
        sub_details = get_issue_details(sub_issue.number)
        lines.append(f"- [{checkbox}] #{sub_issue.number} - {sub_details.title}")

    lines.append("")
    lines.append("## Changes")

    # Get commit list for this branch
    commits = get_branch_commits(feature_branch.branch_name)
    for commit in commits:
        lines.append(f"- {commit.message} ({commit.sha[:7]})")

    lines.append("")
    lines.append("---")
    lines.append("🤖 Generated by Claude Code Orchestrator")

    return "\n".join(lines)
```

## Negative Cases & Mitigations

### 1. Merge Conflicts Between Sub-Issues

**Scenario:** Sub-issue #51 and #52 both modify `auth.py`

**Detection:**
```python
try:
    await git_pull_rebase(project_dir)
except MergeConflictError as e:
    # e.files = ['auth.py', 'config.js']
    ...
```

**Mitigation:**
- **Don't auto-resolve** - conflicts in code are dangerous
- **Escalate to human** immediately
- **Block sub-issue** until resolved
- **Post conflict details** to issue/discussion

```python
async def escalate_merge_conflict(
    issue_number: int,
    branch_name: str,
    conflict_files: List[str]
):
    """Escalate merge conflict to human intervention"""

    message = f"""## ⚠️ Merge Conflict Detected

Work on this sub-issue cannot proceed due to merge conflicts.

**Branch:** `{branch_name}`
**Conflicting files:**
{chr(10).join(f'- `{f}`' for f in conflict_files)}

**Next Steps:**
1. Manually resolve conflicts in the branch
2. Commit the resolution
3. Push to remote
4. Move this issue back to the appropriate column to retry

**Resolution Command:**
```bash
git checkout {branch_name}
git pull --rebase
# Resolve conflicts manually
git add .
git commit -m "Resolve merge conflicts"
git push
```
"""

    await post_to_issue(issue_number, message)

    # Move to "Blocked" status
    await update_issue_status(issue_number, "Blocked - Merge Conflict")
```

**Prevention Strategy:**
- Git pull before each agent starts (always fresh)
- Trust git's merge for non-conflicting changes
- Conflicts are rare if sub-issues work on different files

### 2. Stale Branch (Far Behind Main)

**Scenario:** Feature branch created 2 weeks ago, main has 50+ commits

**Detection:**
```python
commits_behind = await get_commits_behind_main(project_dir, branch_name)
```

**Mitigation Levels:**

**Low Risk (< 20 commits behind):**
- Log warning, continue normally
- Git pull brings in changes automatically

**Medium Risk (20-50 commits behind):**
- Log warning with details
- Continue but notify in PR

**High Risk (> 50 commits behind):**
- **Escalate to human** for rebase approval
- Don't auto-rebase (could introduce bugs)
- Post guidance to parent issue

```python
async def escalate_stale_branch(parent_issue: int, commits_behind: int):
    """Notify about stale branch requiring rebase"""

    message = f"""## 📅 Branch Maintenance Required

This feature branch is significantly behind the main branch.

**Commits behind:** {commits_behind}
**Risk:** High - may have integration issues

**Recommended Action:**
Rebase the feature branch on latest main to incorporate recent changes.

**Rebase Command:**
```bash
git checkout {branch_name}
git fetch origin
git rebase origin/main
# Resolve any conflicts
git push --force-with-lease
```

**Note:** This is a potentially risky operation. Review changes carefully.
"""

    await post_to_issue(parent_issue, message)
```

**Automatic Rebase (Optional Future Enhancement):**
- Daily job to rebase branches < 30 commits behind
- Only if no conflicts
- Skip if recent activity (don't disrupt active work)

### 3. Concurrent Sub-Issue Work

**Scenario:** Agent working on #51 while agent working on #52

**Strategy:** **Embrace concurrency, trust git**

**Why it works:**
- Git pull before start gets latest from both
- Git merge handles non-conflicting changes automatically
- If same file modified → conflict detection kicks in
- Both agents work in Docker containers (isolated)

**Protection:**
```python
# Agent 1 (sub-issue #51)
git pull --rebase     # Gets any changes from #52
# Work on files A, B
git commit && git push

# Agent 2 (sub-issue #52) - starts during Agent 1's work
git pull --rebase     # Gets Agent 1's partial work
# Work on files C, D (or A, B → conflict detected)
git commit && git push
```

**No file-level locking needed** - adds complexity without much benefit.

### 4. Orphaned Branches

**Scenario:** Parent issue closed/cancelled but branch remains

**Detection:**
```python
async def cleanup_orphaned_branches():
    """Periodic cleanup job"""

    for feature_branch in get_all_feature_branches():
        parent_issue = await get_issue(feature_branch.parent_issue)

        if parent_issue.state == 'closed':
            days_closed = (now() - parent_issue.closed_at).days

            # Grace period before deletion
            if days_closed > 7:
                logger.info(f"Deleting orphaned branch {feature_branch.branch_name}")

                await delete_branch(feature_branch.branch_name)
                delete_feature_branch_state(feature_branch.parent_issue)

                # Post notification
                await post_to_issue(
                    feature_branch.parent_issue,
                    f"🧹 Deleted orphaned branch `{feature_branch.branch_name}` (parent closed 7+ days ago)"
                )
```

**Run schedule:** Daily at 2 AM

### 5. No Parent Issue (Standalone)

**Scenario:** Issue has no parent tracking relationship

**Strategy:** **Graceful fallback to individual branch**

```python
parent = await get_parent_issue(issue_number)

if not parent:
    # No parent - create standalone branch
    branch_name = f"feature/issue-{issue_number}-{sanitize_title(issue_title)}"
    logger.info(f"Issue #{issue_number} has no parent - creating standalone branch")

    return await create_standalone_branch(issue_number, branch_name)
```

**No failure** - system adapts to both workflows.

### 6. Branch Deleted Externally

**Scenario:** Someone manually deletes the feature branch

**Detection:**
```python
if not await branch_exists(project_dir, branch_name):
    logger.error(f"Branch {branch_name} not found - was it deleted externally?")
    ...
```

**Mitigation:**
```python
# Recreate from main + warn
await create_branch_from_main(project_dir, branch_name)
await post_to_issue(
    parent_issue,
    f"⚠️ Branch `{branch_name}` was deleted externally and has been recreated. Previous work may be lost."
)
```

**Better:** Check branch exists before operations, fail gracefully

### 7. Partial Completion (Abandoned Sub-Issues)

**Scenario:** Parent has 5 sub-issues, 3 done, 2 abandoned for 30+ days

**Strategy:** **Human decides completion**

```python
def should_mark_pr_ready(feature_branch) -> bool:
    """Determine if PR should be marked ready"""

    sub_issues = feature_branch.sub_issues
    completed = [s for s in sub_issues if s.status == 'completed']
    cancelled = [s for s in sub_issues if s.status == 'cancelled']
    pending = [s for s in sub_issues if s.status == 'pending']

    # All complete or cancelled
    if len(completed) + len(cancelled) == len(sub_issues):
        return True

    # Stale - ask human
    if pending and days_since_last_activity(feature_branch) > 30:
        escalate_partial_completion(feature_branch, pending)
        return False

    return False


async def escalate_partial_completion(feature_branch, pending_issues):
    """Ask human about partial completion"""

    message = f"""## 🤔 Partial Feature Completion

This feature has been partially completed with some sub-issues pending.

**Completed:** {len([s for s in feature_branch.sub_issues if s.status == 'completed'])}
**Pending:** {len(pending_issues)}

**Pending sub-issues:**
{chr(10).join(f'- #{i.number}' for i in pending_issues)}

**Options:**
1. Cancel pending issues and mark PR ready (partial feature)
2. Continue work on pending issues
3. Close parent issue (abandon feature)

Reply with your choice to proceed.
"""

    await post_to_issue(feature_branch.parent_issue, message)
```

## Git Pull Strategy

### When to Pull

**✅ Before Agent Starts (Always):**
```python
# REQUIRED - get latest changes from other sub-issues
await git_pull_rebase(project_dir)
```

**❌ During Agent Work (No):**
- Agent work is quick (minutes)
- Pulling during work adds complexity
- Risk of conflicts mid-execution

**❌ After Agent Completes (No):**
- Changes already committed
- Just push - next agent will pull

**✅ Periodic Staleness Check (Yes):**
```python
# Check commits behind main (not pull)
commits_behind = await get_commits_behind_main(branch, 'main')
# Warn if stale, escalate if very stale
```

### Pull Strategy

**Use `git pull --rebase`:**
- Cleaner history (linear)
- Easier conflict detection
- Avoids merge commits

```bash
git pull --rebase origin feature-branch
```

**On conflict:**
- Abort the rebase
- Escalate to human
- Don't attempt auto-resolution

## Implementation Plan

### Phase 1: Core Infrastructure (Week 1)
- [ ] Create `FeatureBranchManager` service
- [ ] Implement state management
- [ ] Add parent issue detection (GraphQL)
- [ ] Basic branch creation/checkout

### Phase 2: Integration (Week 1)
- [ ] Integrate with agent_executor (before/after hooks)
- [ ] Update git_workflow_manager for feature PRs
- [ ] Add conflict detection

### Phase 3: PR Management (Week 2)
- [ ] Feature PR creation with checklist
- [ ] Auto-update PR on sub-issue completion
- [ ] Mark PR ready when all complete

### Phase 4: Safety & Cleanup (Week 2)
- [ ] Staleness detection
- [ ] Orphaned branch cleanup job
- [ ] Escalation notifications

### Phase 5: Testing (Week 3)
- [ ] Test concurrent sub-issues
- [ ] Test conflict scenarios
- [ ] Test partial completion

## Integration Points

### 1. Agent Executor (Before Execution)
```python
# services/agent_executor.py - Line 82
async def execute_agent(...):
    # NEW: Prepare feature branch
    if 'issue_number' in task_context:
        from services.feature_branch_manager import feature_branch_manager

        branch_name = await feature_branch_manager.prepare_feature_branch(
            project_name=project_name,
            issue_number=task_context['issue_number']
        )

        task_context['branch_name'] = branch_name

    # Execute agent...
```

### 2. Agent Executor (After Execution)
```python
# services/agent_executor.py - Line 103
async def execute_agent(...):
    # ... agent execution ...

    # NEW: Finalize feature branch
    if 'issue_number' in task_context and 'branch_name' in task_context:
        await feature_branch_manager.finalize_feature_branch_work(
            project_name=project_name,
            issue_number=task_context['issue_number'],
            commit_message=f"Complete work for issue #{task_context['issue_number']}"
        )
```

### 3. Cleanup Job
```python
# New: scripts/cleanup_orphaned_branches.py
async def main():
    from services.feature_branch_manager import feature_branch_manager
    await feature_branch_manager.cleanup_orphaned_branches()

# Run daily via cron
```

## Summary

**Feasible:** ✅ Yes, very feasible with GitHub's native issue tracking

**Key Success Factors:**
1. **Git pull before each agent** - Keeps branch current
2. **Conflict escalation** - Don't auto-resolve, ask human
3. **Staleness monitoring** - Warn when branch falls behind
4. **Graceful fallbacks** - Handle standalone issues, missing parents
5. **Trust git** - Let it handle non-conflicting concurrent changes

**Risk Mitigation:**
- Detect conflicts early (pull before start)
- Escalate dangerous situations (stale, conflicts, partial completion)
- Don't over-engineer (no file locking, no periodic pulls during work)
- Graceful degradation (fallback to standalone branches)

**Complexity Trade-off:**
- Added: State management, parent detection, PR checklist
- Removed: Individual branches per sub-issue, manual PR coordination
- Net: Cleaner workflow, better collaboration, manageable complexity
