# Review Cycle Context and File Creation Fixes

## Issues Discovered

### Issue 1: Agents Creating Unexpected Files
**Problem:** Business Analyst agent was creating `business_analysis.md` file on filesystem instead of posting to GitHub discussions, making requirements invisible to users.

**Root Cause:** Despite prompt instruction "DO NOT create any files", Claude was ignoring it and using Write tool.

**Solutions Implemented:**
1. **Strengthened prompts** (already in place, line 73 of business_analyst_agent.py)
2. **Future consideration:** Read-only filesystem Docker variant for non-code agents

### Issue 2: Review Cycle Not Passing Full Discussion Context
**Problem:** Reviewer agent only received the maker's latest output, missing:
- User comments/feedback in the discussion
- Original context and requirements
- Full conversation thread

**Root Cause:**
- `_create_review_task_context()` only passed `latest_maker_output`
- Didn't fetch or include full discussion thread
- No refresh of context during iterations

**Solution:** Added fresh context fetching before each review iteration

### Issue 3: Maker Not Receiving Reviewer's Full Feedback
**Problem:** Maker agent wasn't getting the complete review feedback context during revision.

**Root Cause:**
- `_create_maker_revision_task_context()` only passed `review_feedback` (reviewer's comment)
- Missing full discussion context including all previous work and user feedback

**Solution:** Added fresh context fetching before maker revision

## Changes Made

### File: `services/review_cycle.py`

#### 1. Added Full Discussion Context Fetching (lines 401-479)

New method `_get_fresh_discussion_context()`:
```python
async def _get_fresh_discussion_context(
    cycle_state: ReviewCycleState,
    org: str,
    iteration: int
) -> str:
    """
    Fetch the latest discussion context including all comments and replies.
    This is used to ensure agents see the complete conversation.
    """
```

**Features:**
- Queries GraphQL for full discussion thread
- Retrieves ALL comments AND threaded replies
- Builds chronological context with author attribution
- Formats as markdown for agent consumption
- Returns complete discussion history

#### 2. Updated Review Task Context Creation (lines 301-335)

**Before:**
```python
def _create_review_task_context(...):
    latest_maker_output = cycle_state.maker_outputs[-1]['output']
    return {
        ...
        'previous_stage_output': latest_maker_output,  # Just maker output
    }
```

**After:**
```python
def _create_review_task_context(..., full_discussion_context: str = ""):
    latest_maker_output = cycle_state.maker_outputs[-1]['output']
    context_for_review = full_discussion_context if full_discussion_context else latest_maker_output
    return {
        ...
        'previous_stage_output': context_for_review,  # Full discussion context
    }
```

#### 3. Updated Maker Revision Task Context Creation (lines 337-399)

**Before:**
```python
def _create_maker_revision_task_context(...):
    original_output = cycle_state.maker_outputs[0]['output']
    return {
        ...
        'previous_output': original_output,  # Just original output
    }
```

**After:**
```python
def _create_maker_revision_task_context(..., full_discussion_context: str = ""):
    original_output = cycle_state.maker_outputs[0]['output']
    previous_stage_context = full_discussion_context if full_discussion_context else f"{original_output}\n\n{review_feedback}"
    return {
        ...
        'previous_stage_output': previous_stage_context,  # Full discussion context
        'previous_output': original_output,
    }
```

#### 4. Updated Review Loop to Fetch Fresh Context (lines 181-199, 276-291)

**Before Reviewer Execution:**
```python
# Fetch fresh discussion context before each iteration
fresh_context = ""
if cycle_state.workspace_type == 'discussions' and cycle_state.discussion_id:
    fresh_context = await self._get_fresh_discussion_context(
        cycle_state, org, iteration
    )
    logger.debug(f"Fresh discussion context length: {len(fresh_context)}")

review_task_context = self._create_review_task_context(
    cycle_state, column, issue_data, iteration,
    full_discussion_context=fresh_context  # Pass full context
)
```

**Before Maker Execution:**
```python
# Fetch fresh context again (now includes reviewer's comment)
if cycle_state.workspace_type == 'discussions' and cycle_state.discussion_id:
    fresh_context = await self._get_fresh_discussion_context(
        cycle_state, org, iteration
    )
    logger.debug(f"Fresh context for maker (with review): {len(fresh_context)}")

maker_task_context = self._create_maker_revision_task_context(
    cycle_state, column, issue_data, review_comment, iteration,
    full_discussion_context=fresh_context  # Pass full context
)
```

## How It Works Now

### Review Cycle Flow with Full Context

**Iteration 1:**
1. **Fetch fresh context** - Get ALL discussion comments and replies
2. **Create reviewer task context** - Include complete discussion thread
3. **Execute reviewer** - Reviews with full context of original requirements + maker output + user feedback
4. **Reviewer posts feedback** - Posted to discussion
5. **Fetch fresh context again** - Now includes reviewer's feedback
6. **Create maker task context** - Include complete discussion thread + review feedback
7. **Execute maker** - Revises with full context of everything
8. **Maker posts revision** - Posted to discussion

**Iteration 2-N:**
- Repeat with fresh context each time
- Each agent sees the COMPLETE conversation history
- Includes: Original requirements, all agent outputs, all user comments, all reviews

### Context Format

The fresh discussion context is formatted as:

```markdown
## Complete Discussion Thread for Issue #93

**@user1** (2025-10-02T10:00:00Z):
Original requirement: We need feature X...

**@business_analyst[bot]** (2025-10-02T10:05:00Z):
## Business Analysis
...

  → **@user1** (2025-10-02T10:10:00Z):
  Great analysis, but I think you missed...

**@requirements_reviewer[bot]** (2025-10-02T10:15:00Z):
## Requirements Review
STATUS: CHANGES_REQUESTED
...

**@business_analyst[bot]** (2025-10-02T10:20:00Z):
## Updated Business Analysis
Addressing feedback...
```

## Benefits

1. **Complete Context:** Agents see the full conversation, not just isolated snippets
2. **User Feedback Included:** User comments are part of the context
3. **Chronological Order:** Context maintains timeline of discussion
4. **Author Attribution:** Clear who said what
5. **Thread Awareness:** Threaded replies preserved
6. **Fresh Every Iteration:** Context refreshed before each agent execution

## File Creation Issue - Recommendations

### Option A: Enhanced Prompting (Current)
- ✅ Already implemented: "IMPORTANT: Output your analysis as text directly in your response. DO NOT create any files."
- ❌ Still being ignored by Claude sometimes

### Option B: Read-Only Filesystem Docker Variant
**Pros:**
- Prevents all file creation
- Enforces desired behavior technically
- No prompt workarounds

**Cons:**
- Breaks legitimate use cases (code generation agents need write)
- Need multiple Docker image variants
- Complexity in configuration

### Option C: Agent Configuration Flag
Add to `config/foundations/agents.yaml`:
```yaml
agents:
  business_analyst:
    filesystem_write_allowed: false  # <-- New flag

  senior_software_engineer:
    filesystem_write_allowed: true   # Needs to write code
```

Then in docker runner, mount filesystem as read-only if flag is false.

**Recommended:** Implement Option C
- Granular control per agent
- Simple configuration
- No image variants needed
- Docker mount flag: `--read-only` or `:ro` volume suffix

## Testing

### Verify Full Context Passing
1. Create issue and move to review column
2. Add user comment in discussion
3. Watch reviewer agent - should see user comment in context
4. Watch maker revision - should see ALL previous context

### Verify No File Creation
1. Run business_analyst agent
2. Check workspace directory for unexpected files
3. Should only see output in GitHub discussion

### Debug Logging
Enable debug logs to see context sizes:
```
Fresh discussion context length: 5432
Fresh context for maker (with review): 7654
```

## Future Improvements

1. **Context Summarization:** For very long discussions (>50K chars), summarize older context
2. **Selective Context:** Only include relevant parts based on agent role
3. **Context Caching:** Cache discussion context to reduce GitHub API calls
4. **File Creation Prevention:** Implement Option C (filesystem_write_allowed flag)
