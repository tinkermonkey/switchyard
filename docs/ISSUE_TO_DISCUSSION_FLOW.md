# Issue → Discussion → Issue Flow

## Overview

Users create Issues with rough ideas. Orchestrator auto-creates Discussions for pre-SDLC work, then updates the original Issue with final requirements when ready for implementation.

## Detailed Flow

### Phase 1: Issue Creation & Discussion Setup

```
User creates Issue #123
Title: "Add vector search to schema.org database"
Body: [Rough description of need]
Labels: pipeline:idea-dev (triggers idea_development pipeline)

↓ ProjectMonitor detects new issue ↓

Orchestrator:
1. Detects workspace="discussions" for idea_development pipeline
2. Creates Discussion #456 in "Ideas" category:
   - Title: "Requirements: Add vector search to schema.org database"
   - Body: Copy of issue description + "Auto-created from Issue #123"
3. Adds comment to Issue #123:
   "Requirements analysis moved to Discussion #456. This issue will be updated with final requirements."
4. Adds context to task:
   discussion_id: "D_abc123..."
   issue_number: 123
   workspace_type: "discussions"
   linked_issue: 123
```

### Phase 2: Pre-SDLC Work (in Discussion)

```
Discussion #456: "Requirements: Add vector search..."

├─ [Original Post] User's rough idea
│
├─ orchestrator-bot[bot] commented
│  🔬 Idea Research Complete
│  [Research output...]
│
├─ orchestrator-bot[bot] commented
│  📊 Business Analysis Complete
│  [Requirements analysis...]
│
│  └─ orchestrator-bot[bot] replied
│     ✅ Requirements Review Complete
│     Status: Needs Revision
│     [Review findings...]
│
│     └─ @tinkermonkey replied
│        Clarification: no fallback needed...
│
└─ orchestrator-bot[bot] commented
   📊 Business Analysis Updated
   [Final requirements incorporating feedback...]

   Requirements approved ✓
   Moving to Implementation → Issue #123
```

### Phase 3: Issue Update for Implementation

```
Orchestrator:
1. Detects requirements approved (or reaches "design" stage completion)
2. Extracts final requirements from discussion
3. Updates Issue #123 body:

---
# Schema.org Vector Search Implementation

[Concise, final requirements extracted from discussion]

## Background
See Discussion #456 for full requirements analysis and design rationale.

## Functional Requirements
[Clean list from BA analysis]

## Non-Functional Requirements
[Clean list from BA analysis]

## Acceptance Criteria
[Clean list from BA analysis]

---
Generated from Discussion #456 by orchestrator-bot
---

4. Adds label: "ready-for-implementation"
5. Moves project card to "Implementation" column
6. Posts to Discussion: "Requirements finalized → Implementation in Issue #123"
7. (Optionally) Closes/locks Discussion as resolved
```

### Phase 4: SDLC Work (in Issue)

```
Issue #123: "Add vector search to schema.org database"
[Now has clean, final requirements as body]

├─ orchestrator-bot[bot] commented
│  🏗️ Architecture Design Complete
│  [Design decisions...]
│
├─ orchestrator-bot[bot] commented
│  💻 Implementation Complete
│  [Commit references, PR links...]
│
└─ orchestrator-bot[bot] commented
   ✅ Testing Complete
   [Test results...]

Issue closed as completed
```

## Configuration

### Project Config (`config/projects/context-studio.yaml`)

```yaml
pipelines:
  enabled:
    - template: "idea_development"
      name: "idea-development"
      board_name: "idea-development"
      workspace: "discussions"
      discussion_category: "Ideas"

      # Key settings for auto-creation
      auto_create_from_issues: true  # Create discussion when issue detected
      update_issue_on_completion: true  # Update issue body with final requirements
      discussion_title_prefix: "Requirements: "  # Prefix for discussion titles

    - template: "full_sdlc"
      name: "full-sdlc"
      workspace: "hybrid"

      # Hybrid: Pre-SDLC in discussions, SDLC in issues
      discussion_stages: ["research", "requirements", "design"]
      issue_stages: ["implementation", "testing", "qa", "documentation"]

      auto_create_from_issues: true
      update_issue_on_completion: true  # Update at transition point
      transition_stage: "implementation"  # When to update issue
```

## Implementation Requirements

### 1. ProjectMonitor Changes

```python
def monitor_issues(self):
    """Monitor issues for new activity"""
    for issue in new_or_updated_issues:
        # Existing logic...

        # NEW: Check if issue needs discussion
        pipeline_config = self._get_pipeline_for_issue(issue)
        if pipeline_config and pipeline_config.workspace == "discussions":
            if not self._has_linked_discussion(issue):
                self._create_discussion_from_issue(issue, pipeline_config)
```

### 2. Discussion Creation

```python
def _create_discussion_from_issue(self, issue, pipeline_config):
    """Auto-create discussion from new issue"""
    # Create discussion with issue content
    discussion_id = discussions.create_discussion(
        owner=org,
        repo=repo,
        category_id=self._get_category_id(pipeline_config),
        title=f"{pipeline_config.discussion_title_prefix}{issue['title']}",
        body=self._format_discussion_from_issue(issue)
    )

    # Comment on issue linking to discussion
    gh_comment(issue_number,
        f"📋 Requirements analysis moved to Discussion #{discussion_number}\n\n"
        f"This issue will be updated with final requirements when ready for implementation."
    )

    # Store link in state
    self.state_manager.link_issue_to_discussion(issue_number, discussion_id)
```

### 3. Discussion Body Template

```python
def _format_discussion_from_issue(self, issue):
    return f"""# Requirements Analysis

Auto-created from Issue #{issue['number']}

## User Request

{issue['body']}

---

**Labels**: {', '.join(issue['labels'])}
**Requested by**: @{issue['author']['login']}
**Link**: {issue['html_url']}

---

The orchestrator will analyze this request and develop detailed requirements.
When complete, Issue #{issue['number']} will be updated with final requirements.
"""
```

### 4. Issue Update on Completion

```python
def _finalize_requirements_to_issue(self, discussion_id, issue_number):
    """Extract final requirements from discussion and update issue"""
    # Get discussion details
    discussion = discussions.get_discussion_by_number(owner, repo, number)

    # Extract agent outputs (BA, Architect, etc)
    requirements = self._extract_requirements_from_discussion(discussion)

    # Format clean issue body
    new_body = f"""# {discussion['title']}

{requirements['executive_summary']}

## Background
Full requirements analysis: Discussion #{discussion['number']}

## Functional Requirements
{requirements['functional']}

## Non-Functional Requirements
{requirements['non_functional']}

## User Stories
{requirements['user_stories']}

## Architecture Notes
{requirements['architecture']}

---
📋 Requirements finalized from Discussion #{discussion['number']}
Ready for implementation.
---
"""

    # Update issue
    gh_issue_edit(issue_number, --body new_body)

    # Add label
    gh_issue_label(issue_number, "ready-for-implementation")

    # Comment on discussion
    discussions.add_comment(discussion_id,
        f"✅ Requirements finalized and posted to Issue #{issue_number}\n"
        f"Moving to implementation phase."
    )
```

### 5. State Management

```python
# state/projects/context-studio/discussion_links.yaml
issue_discussion_links:
  93: "D_abc123..."  # Issue 93 → Discussion ID
  94: "D_def456..."

discussion_issue_links:
  "D_abc123...": 93  # Discussion → Issue 93
```

## Transition Detection

The orchestrator needs to detect when to transition from discussion back to issue:

### Option 1: Manual Trigger
User comments: `@orchestrator-bot finalize requirements`

### Option 2: Automatic (Recommended)
After successful requirements review approval:
- Requirements_reviewer posts "Approved" review
- No critical issues found
- Orchestrator automatically updates issue and transitions

### Option 3: Stage-Based
When pipeline reaches configured `transition_stage`:
```yaml
transition_stage: "implementation"  # Auto-transition when starting implementation
```

## Benefits of This Approach

1. ✅ **User friction minimized**: Users just create issues (existing workflow)
2. ✅ **Clean separation**: Verbose analysis in discussions, clean requirements in issues
3. ✅ **Full traceability**: Issue always links to discussion
4. ✅ **Natural archival**: Discussions can be closed after finalization
5. ✅ **Better discovery**: Issues show final state, discussions show evolution
6. ✅ **Reduced noise**: Issue comments focused on implementation, not requirements debate
7. ✅ **Reusable issues**: Issue body can be updated multiple times as requirements evolve

## Example Timeline

```
T+0:    User creates Issue #123
T+1:    Bot creates Discussion #456 and links them
T+2-8:  Pre-SDLC work happens in Discussion (research, analysis, reviews, feedback)
T+9:    Requirements approved
T+10:   Bot updates Issue #123 body with final requirements
T+11:   Bot moves card to "Implementation"
T+12+:  SDLC work happens in Issue (design, code, test, deploy)
T+end:  Issue closed, Discussion archived as historical record
```

## Discussion Lifecycle

```
Created → Active (agents working) → Under Review (reviewer + user feedback)
  → Approved → Finalized (issue updated) → Archived (closed/locked)
```

Each state is trackable via:
- Discussion labels
- Project board column position
- State in Redis/YAML
