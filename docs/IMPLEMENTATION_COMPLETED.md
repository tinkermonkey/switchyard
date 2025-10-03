# Issue→Discussion Auto-Creation Implementation Complete

## Summary

Successfully implemented the automatic creation of GitHub Discussions from Issues when items are added to project boards configured for discussion or hybrid workspaces.

## Implementation Details

### 1. ProjectMonitor Updates (`services/project_monitor.py`)

**Added Methods:**
- `_check_and_create_discussion()` - Checks if an issue needs a discussion and creates it if configured
- `_create_discussion_from_issue()` - Creates a discussion from issue data
- `_format_discussion_from_issue()` - Formats the discussion body with issue details

**Integration Points:**
- `process_board_changes()` now calls `_check_and_create_discussion()` when `item_added` event detected
- Links issue to discussion in state management
- Adds comment to issue with link to discussion

**Flow:**
1. User creates Issue and adds to project board
2. ProjectMonitor detects `item_added` event
3. Checks if pipeline uses `discussions` or `hybrid` workspace
4. Checks if discussion already exists (prevents duplicates)
5. Creates discussion with formatted body
6. Stores bidirectional link in state
7. Comments on issue with link to discussion
8. Proceeds with normal agent triggering

### 2. State Management Updates (`config/state_manager.py`)

**Schema Changes:**
- Added `issue_discussion_links: Dict[int, str]` to `GitHubProjectState`
- Added `discussion_issue_links: Dict[str, int]` to `GitHubProjectState`

**New Methods:**
- `link_issue_to_discussion()` - Creates bidirectional link
- `get_discussion_for_issue()` - Gets discussion ID for an issue
- `get_issue_for_discussion()` - Gets issue number for a discussion
- `unlink_issue_discussion()` - Removes link

**Persistence:**
- Links stored in `state/projects/{project}/github_state.yaml`
- Serialized as string keys for YAML compatibility
- Loaded and converted to proper types on read

### 3. GitHubDiscussions API Updates (`services/github_discussions.py`)

**New Methods:**
- `get_repository_id()` - Gets repository node ID for GraphQL operations
- `get_discussion()` - Gets discussion details by node ID

**Enhanced Methods:**
- `create_discussion()` now accepts optional `repository_id` parameter to avoid redundant API calls

### 4. Configuration Support

**Pipeline Configuration:**
The implementation respects these configuration options (with defaults):

```yaml
pipelines:
  - name: "idea-development"
    workspace: "discussions"  # or "hybrid"
    discussion_category: "Ideas"
    auto_create_from_issues: true  # Default: true
    discussion_title_prefix: "Requirements: "  # Default: "Requirements: "
```

## Discussion Body Format

Auto-created discussions use this template:

```markdown
# Requirements Analysis

Auto-created from Issue #{issue_number}

## User Request

{issue_body}

---

**Labels**: {labels}
**Requested by**: @{author}

---

The orchestrator will analyze this request and develop detailed requirements.
When complete, Issue #{issue_number} will be updated with final requirements.
```

## Issue Comment Format

When a discussion is created, the issue receives this comment:

```markdown
📋 Requirements analysis moved to Discussion #{discussion_number}

This issue will be updated with final requirements when ready for implementation.

_Link: {discussion_url}_
```

## What's Next

The implementation is ready for testing! Remaining work includes:

1. **Discussion→Issue Finalization** - Extract final requirements from discussion and update issue body
2. **Discussion Polling** - Monitor discussions for @orchestrator-bot mentions and activity
3. **Workspace-Aware Context Retrieval** - Read previous stage outputs from discussions
4. **Agent Integration** - Update agents to post to discussions when workspace is discussions
5. **End-to-End Testing** - Test full hybrid workflow

## Testing Checklist

- [ ] Create issue with `pipeline:idea-dev` label
- [ ] Add issue to idea-development board
- [ ] Verify discussion is auto-created
- [ ] Verify issue receives comment with discussion link
- [ ] Verify link is stored in state file
- [ ] Verify no duplicate discussions created on re-add
- [ ] Verify agents can post to discussion
- [ ] Test hybrid workflow transition point

## Files Modified

1. `services/project_monitor.py` - Auto-creation logic (+150 lines)
2. `config/state_manager.py` - Issue/discussion link management (+60 lines)
3. `services/github_discussions.py` - API enhancements (+35 lines)

## Dependencies

- Requires GitHub App with "Discussions: Read & Write" permission
- Requires discussions enabled on repository
- Requires valid discussion category configured or available

## Backward Compatibility

✅ Fully backward compatible:
- Defaults to `workspace: "issues"` if not configured
- Existing pipelines continue to work unchanged
- Auto-creation only triggers for discussion workspaces
- State file handles missing link data gracefully
