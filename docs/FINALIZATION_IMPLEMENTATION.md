# Discussion→Issue Finalization Implementation

## Summary

Implemented automatic extraction of final requirements from GitHub Discussions and updating of Issues when requirements are approved by the requirements_reviewer agent.

## Overview

When the requirements_reviewer approves requirements (no "NEEDS REVISION" or critical issues), the system now:

1. Detects the approval
2. Extracts structured requirements from discussion comments
3. Formats a clean, comprehensive issue body
4. Updates the original issue with final requirements
5. Adds "ready-for-implementation" label
6. Posts completion comment to discussion

## Implementation Details

### 1. ProjectMonitor Methods (`services/project_monitor.py`)

**Main Finalization Method:**
```python
def finalize_requirements_to_issue(
    project_name: str,
    board_name: str,
    issue_number: int,
    repository: str,
    discussion_id: Optional[str] = None
)
```

Orchestrates the full finalization process:
- Retrieves discussion from state if not provided
- Extracts requirements from agent comments
- Formats new issue body
- Updates issue via `gh issue edit`
- Adds "ready-for-implementation" label
- Posts completion comment to discussion

**Requirements Extraction:**
```python
def _extract_requirements_from_discussion(
    discussion_id: str,
    project_config,
    repository: str
) -> Dict[str, Any]
```

Parses discussion comments to extract:
- Executive summary
- Functional requirements (bullet list)
- Non-functional requirements (bullet list)
- User stories (INVEST format)
- Architecture notes
- Acceptance criteria

**Text Parsing Utilities:**
- `_extract_section()` - Extracts text between markdown section headers
- `_extract_list_items()` - Extracts bullet points from sections
- `_extract_user_stories()` - Parses "As a... I want... So that..." patterns

**Issue Body Formatting:**
```python
def _format_finalized_requirements(
    discussion_number: int,
    discussion_url: str,
    requirements: Dict[str, Any]
) -> str
```

Creates structured markdown with:
- Executive summary (if present)
- Background section with discussion link
- Functional requirements
- Non-functional requirements
- User stories
- Architecture notes
- Acceptance criteria
- Footer with discussion reference

### 2. Requirements Reviewer Updates (`agents/requirements_reviewer_agent.py`)

**Auto-Finalization Trigger:**

Added to the approval path (line 134-135):
```python
else:
    logger.info("Review approved - no feedback loop needed")
    await self._check_and_finalize_to_issue(context)
```

**Finalization Check Method:**
```python
async def _check_and_finalize_to_issue(context)
```

Validates conditions before triggering finalization:
- Checks workspace_type is "discussions"
- Verifies `update_issue_on_completion` is enabled (default: true)
- Retrieves discussion ID from state
- Creates ProjectMonitor instance
- Calls `finalize_requirements_to_issue()`

### 3. Configuration Schema (`config/manager.py`)

**New ProjectPipeline Fields:**

```python
@dataclass
class ProjectPipeline:
    # ... existing fields ...
    auto_create_from_issues: bool = True
    update_issue_on_completion: bool = True
    discussion_title_prefix: str = "Requirements: "
    transition_stage: Optional[str] = None
```

**Field Descriptions:**
- `auto_create_from_issues` - Auto-create discussion when issue added to board
- `update_issue_on_completion` - Update issue when requirements approved
- `discussion_title_prefix` - Prefix for discussion titles
- `transition_stage` - Stage name for hybrid workflow transitions

## Finalization Flow

```
┌─────────────────────────────────────────────────┐
│ Requirements Reviewer executes                   │
│ - Reviews business analyst output                │
│ - Posts review to discussion                     │
└─────────────┬───────────────────────────────────┘
              │
              ▼
         ┌─────────┐
         │ Approved?│
         └────┬────┘
              │
        ┌─────┴─────┐
        │           │
       Yes         No
        │           │
        │           └──→ Trigger feedback loop to BA
        │
        ▼
┌───────────────────────────────────────┐
│ _check_and_finalize_to_issue()        │
│ - Check workspace type                │
│ - Check configuration flags           │
│ - Get discussion ID from state        │
└───────────┬───────────────────────────┘
            │
            ▼
┌───────────────────────────────────────┐
│ finalize_requirements_to_issue()      │
│ - Get discussion details              │
│ - Extract requirements                │
│ - Format issue body                   │
└───────────┬───────────────────────────┘
            │
            ▼
┌───────────────────────────────────────┐
│ _extract_requirements_from_discussion()│
│ - Query GraphQL for comments          │
│ - Find latest BA/architect outputs    │
│ - Parse sections and lists            │
└───────────┬───────────────────────────┘
            │
            ▼
┌───────────────────────────────────────┐
│ _format_finalized_requirements()      │
│ - Build structured markdown           │
│ - Include all requirement sections    │
│ - Add discussion link                 │
└───────────┬───────────────────────────┘
            │
            ▼
┌───────────────────────────────────────┐
│ Update Issue                          │
│ - gh issue edit --body                │
│ - gh issue edit --add-label           │
│ - Add discussion comment              │
└───────────────────────────────────────┘
```

## Example Issue Body (After Finalization)

```markdown
# Schema.org Vector Search Implementation

A comprehensive search capability using vector embeddings for semantic similarity.

## Background
Full requirements analysis available in [Discussion #456](https://github.com/org/repo/discussions/456)

## Functional Requirements
- Ingest schema.org vocabulary into vector database
- Generate embeddings for all schema types and properties
- Provide semantic search API endpoint
- Return ranked results with similarity scores

## Non-Functional Requirements
- Search latency < 200ms for p95
- Support 10,000 concurrent users
- 99.9% uptime SLA
- WCAG 2.1 AA accessibility compliance

## User Stories
- **As a developer** I want to search schema.org types by description So that I can find relevant types without knowing exact names
- **As a content author** I want suggestions for related types So that I can enrich my structured data

## Architecture Notes
Use PostgreSQL with pgvector extension for vector storage.
Implement caching layer with Redis for frequent queries.
Deploy as separate microservice with API gateway integration.

## Acceptance Criteria
- Vector search returns results in under 200ms
- Search accuracy validated against test queries
- API documented in OpenAPI spec
- Unit test coverage > 80%

---
📋 Requirements finalized from [Discussion #456](https://github.com/org/repo/discussions/456)
Ready for implementation.
---
```

## Example Discussion Comment (After Finalization)

```markdown
✅ Requirements finalized and posted to Issue #123

The issue has been updated with the final requirements from this discussion.
Moving to implementation phase.

[View Issue →](https://github.com/org/repo/issues/123)
```

## Configuration Example

```yaml
pipelines:
  enabled:
    - template: "idea_development"
      name: "idea-development"
      board_name: "idea-development"
      workspace: "discussions"
      discussion_category: "Ideas"

      # Finalization settings
      auto_create_from_issues: true
      update_issue_on_completion: true
      discussion_title_prefix: "Requirements: "
```

## Benefits

1. **Clean Separation** - Verbose analysis stays in discussions, clean requirements in issues
2. **Full Traceability** - Issue always links back to discussion for historical context
3. **Automatic** - No manual intervention needed when requirements approved
4. **Structured** - Consistent format across all finalized requirements
5. **Discoverable** - Issues become the source of truth for implementation
6. **Searchable** - Final requirements indexed in GitHub's issue search

## Error Handling

- Graceful failures don't block reviewer approval
- Missing discussion ID logs warning but continues
- Extraction failures return empty requirements (warning logged)
- Parsing errors in individual sections don't fail entire extraction
- GitHub API errors logged with full traceback

## Backward Compatibility

✅ Fully backward compatible:
- Only triggers for `workspace: "discussions"`
- Requires `update_issue_on_completion: true` (default)
- Issues-only pipelines unaffected
- Existing agents continue to work unchanged

## Testing Checklist

- [ ] Requirements approved in discussion
- [ ] Issue body updated with extracted requirements
- [ ] "ready-for-implementation" label added
- [ ] Discussion receives completion comment
- [ ] Issue links to discussion
- [ ] Discussion links to issue
- [ ] All requirement sections extracted correctly
- [ ] User stories parsed properly
- [ ] Empty sections handled gracefully
- [ ] Finalization disabled when `update_issue_on_completion: false`

## Files Modified

1. `services/project_monitor.py` - Finalization logic (+290 lines)
2. `agents/requirements_reviewer_agent.py` - Auto-trigger on approval (+70 lines)
3. `config/manager.py` - Configuration schema (+4 fields)

## Next Steps

With finalization complete, the Issue→Discussion→Issue loop is fully functional! Remaining work:

1. **Discussion Polling** - Monitor discussions for activity and mentions
2. **Workspace-Aware Context Retrieval** - Read previous stage outputs from discussions
3. **Agent Integration** - Update agents to post to discussions
4. **End-to-End Testing** - Test complete hybrid workflow
