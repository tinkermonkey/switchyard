# GitHub Discussions + Issues Hybrid Implementation Status

## Overview

Implementing hybrid workspace support where pre-SDLC work (research, requirements, design) happens in GitHub Discussions, and SDLC work (implementation, testing) happens in Issues.

## ✅ Completed Components

### 1. GitHub App Authentication (`services/github_app.py`)
- JWT generation for GitHub App authentication
- Installation token management with auto-refresh
- Timezone-aware token expiration handling
- GraphQL and REST API wrappers
- **Tested**: ✅ Working - successfully authenticates and generates tokens

### 2. Discussions API Client (`services/github_discussions.py`)
- Create discussions with category support
- Add comments (top-level and nested replies)
- Get discussion details with full comment tree
- List and search discussions
- Find discussions with @orchestrator-bot mentions
- Category discovery and management
- **Tested**: ✅ Working - successfully queries discussions and categories

### 3. Configuration Schema Updates (`config/manager.py`)
- Added `workspace` field to `ProjectPipeline` (values: "issues", "discussions", "hybrid")
- Added `discussion_category` for category preference
- Added `discussion_stages` and `issue_stages` for hybrid workflows
- Backward compatible (defaults to "issues")
- **Status**: ✅ Schema ready, needs YAML examples

### 4. Workspace Router (`services/workspace_router.py`)
- Determines workspace (issues/discussions) based on project config and stage
- Handles hybrid workflow routing (pre-SDLC → discussions, SDLC → issues)
- Category resolution and fallback logic
- Workspace creation/retrieval
- **Status**: ✅ Implemented, needs integration testing

### 5. Workspace-Aware Posting (`services/github_integration.py`)
- New `post_agent_output()` method routes to correct workspace
- `_post_discussion_comment()` for posting to discussions (with threading support)
- `_post_issue_comment()` for posting to issues
- Unified interface for agents
- **Status**: ✅ Implemented, needs agent integration

## 🚧 Remaining Work

### 6. ProjectMonitor Discussions Polling
**File**: `services/project_monitor.py`

**What's Needed**:
```python
def monitor_discussions(self):
    """Poll Discussions API for new activity"""
    # Get discussions with recent updates
    # Check for @orchestrator-bot mentions
    # Trigger appropriate pipelines based on category/labels
    # Similar to monitor_issues() but for discussions

def check_for_feedback_in_discussions(self, discussion_id):
    """Check discussion comments for user feedback"""
    # Similar to check_for_feedback() but for discussions
    # Find agent comments by signature
    # Find user replies mentioning @orchestrator-bot
```

**Integration Points**:
- Main monitoring loop needs to call both `monitor_issues()` and `monitor_discussions()`
- Feedback detection needs to work across both workspaces

### 7. Workspace-Aware Context Retrieval
**File**: `services/project_monitor.py`

**What's Needed**:
```python
def get_previous_stage_context(self, workspace_type, workspace_id, ...):
    """Unified context retrieval for issues and discussions"""
    if workspace_type == "discussions":
        return self._get_discussion_context(workspace_id, ...)
    else:
        return self._get_issue_context(workspace_id, ...)  # Existing code
```

**Challenge**: Discussion comments have different structure (nested replies vs flat comments)

### 8. Agent Integration
**Files**: All agents in `agents/` directory

**What's Needed**:
- Update `update_github_status()` methods to use workspace-aware posting
- Extract workspace info from context
- Call `github.post_agent_output(context, comment)` instead of direct `gh issue comment`

**Example Change**:
```python
# OLD:
subprocess.run(['gh', 'issue', 'comment', str(issue_number), '--body', comment, ...])

# NEW:
github = GitHubIntegration()
await github.post_agent_output(context, comment)
```

### 9. Discussion Category Management
**New File**: `services/discussion_categories.py`

**What's Needed**:
- Ensure required categories exist on repo initialization
- Create custom categories: "Research & Requirements", "Architecture & Design"
- Category creation via GraphQL mutation
- Part of repo setup/reconciliation

### 10. Hybrid Workflow Transitions
**File**: `services/workspace_router.py`

**What's Needed**:
```python
def transition_to_issues(self, discussion_id, project, board):
    """Create linked issue from discussion for hybrid workflows"""
    # Get discussion details
    # Create issue with link to discussion
    # Add comment to discussion linking to issue
    # Update project board
    # Return issue context for next stages
```

### 11. Configuration Examples
**File**: `config/projects/context-studio.yaml`

**What's Needed**:
```yaml
pipelines:
  enabled:
    - template: "idea_development"
      workspace: "discussions"
      discussion_category: "Ideas"

    - template: "full_sdlc"
      workspace: "hybrid"
      discussion_stages: ["research", "requirements", "design"]
      issue_stages: ["implementation", "testing", "documentation"]
```

## Testing Plan

### Phase 1: Unit Tests
- [x] GitHub App authentication
- [x] Discussions API client
- [ ] Workspace router logic
- [ ] Context retrieval from discussions

### Phase 2: Integration Tests
- [ ] Create test discussion programmatically
- [ ] Post agent comment to discussion
- [ ] Post nested reply to comment
- [ ] Detect @orchestrator-bot mentions in discussions
- [ ] Route feedback from discussion comments

### Phase 3: End-to-End Tests
- [ ] Run idea_development pipeline entirely in discussions
- [ ] Run hybrid pipeline (discussion → issue transition)
- [ ] Verify feedback loops work in discussions
- [ ] Verify maker-checker pattern in discussions

## Migration Strategy

### Option 1: Parallel Operation (Recommended)
1. Keep existing issues-based pipelines working
2. Add discussions support as opt-in via config
3. Test with pilot discussions
4. Gradually migrate pipelines

### Option 2: Project-by-Project
1. Configure per project which workspace to use
2. context-studio uses hybrid (discussions for pre-SDLC)
3. Other projects continue with issues until ready

## Estimated Remaining Work

- **ProjectMonitor Updates**: ~200 lines
- **Context Retrieval**: ~150 lines
- **Agent Integration**: ~20 lines per agent × 10 agents = ~200 lines
- **Category Management**: ~100 lines
- **Hybrid Transitions**: ~150 lines
- **Testing**: ~300 lines
- **Documentation**: ~200 lines

**Total**: ~1300 lines across 8-10 files

## Next Steps

1. **Immediate**: Update ProjectMonitor to poll discussions
2. **Then**: Implement workspace-aware context retrieval
3. **Then**: Update one agent (business_analyst) as proof of concept
4. **Then**: Roll out to all agents
5. **Finally**: End-to-end testing

## Design Decisions (Resolved)

1. **Discussion Creation**: ✅ Auto-create from issues (user creates issue, bot creates discussion)
2. **Discussion naming**: ✅ "Requirements: [Issue Title]"
3. **Flow**: ✅ Issue → Discussion (pre-SDLC) → Issue Updated (final requirements) → Issue (SDLC)
4. **Transition timing**: ✅ Auto-transition when requirements approved OR at "implementation" stage
5. **Comment threading**: ✅ Top-level for stages, nested replies for reviews
6. **Issue body**: ✅ Overwritten with final requirements, discussion link preserved
7. **Archival**: ✅ Close/lock discussion after issue updated

## Key Insight

**Issues = User Interface, Discussions = Orchestrator Workspace**

Users work in familiar issues. Bot does verbose analysis in discussions. Final requirements copied back to issue for implementation. Discussion preserved as historical context.
