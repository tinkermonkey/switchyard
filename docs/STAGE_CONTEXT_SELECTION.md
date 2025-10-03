# Stage Context Selection Criteria

## Overview

When a ticket moves through the workflow (e.g., Research → Analysis → Design → Development), each stage needs context from the previous stage. This document explains what gets passed forward and why.

## Current Implementation (After Threading Enhancement)

### Selection Algorithm

**Step 1: Find Previous Agent's Final Output**
- Search through all comments AND replies (chronologically reversed)
- Find the **most recent** output with the agent signature: `_Processed by the {agent_name} agent_`
- This ensures we get the **final refined version** after any feedback loops

**Step 2: Collect User Feedback After Final Output**
- Gather all user comments (top-level) after the agent's final output
- Gather all user replies (in threads) after the agent's final output
- Exclude bot comments/replies
- Sort chronologically

**Step 3: Format Combined Context**
```markdown
## Output from {Previous Agent}
[Agent's final output - could be from a threaded reply if refined]

## User Feedback Since Then
**@user1**: [comment or reply feedback]
**@user2** (reply): [threaded feedback]
```

### What Gets Included

✅ **Most Recent Agent Output**
- Top-level comment from agent, OR
- Most recent threaded reply from agent (if they refined their work)
- Automatically gets the "final" version after multiple refinement rounds

✅ **All User Feedback After Final Output**
- Top-level comments from users
- Threaded replies from users
- Both are included and marked with "(reply)" indicator
- Chronologically sorted

✅ **Time-Based Filtering**
- Only includes feedback **after** the agent's final output timestamp
- Prevents duplicate context from earlier iterations

### What Gets Excluded

❌ **Earlier Iterations**
- Only the **final** output is passed, not intermediate versions
- Assumption: refinements supersede earlier versions

❌ **Other Agents' Comments**
- Only the previous stage's agent output is included
- Note: This could be enhanced for reviewer workflows

❌ **Bot Comments**
- Filters out any bot activity
- Prevents circular references

❌ **Feedback Already Addressed**
- If agent refined output based on feedback, only the final output is passed
- The feedback that triggered the refinement is NOT included (since it's already incorporated)

## Example Scenario

### Discussion Timeline

```
1. BA posts initial analysis (2025-01-01 10:00)
2. User replies: "Add performance section" (2025-01-01 10:15)
3. BA replies in thread: [refined with performance] (2025-01-01 10:30) ← FINAL VERSION
4. User top-level: "Looks good, also consider scaling" (2025-01-01 10:45)
5. → Card moves to "Design" stage
```

### What Architect Receives

```markdown
## Output from Business Analyst
[Content from step 3 - the refined version with performance section]

## User Feedback Since Then
**@user** (reply): Looks good, also consider scaling
```

**Note**: The user's initial request "Add performance section" is NOT included because:
1. It was posted before the final output (10:15 < 10:30)
2. The BA already incorporated it into the final output
3. Including it would be redundant

## Issues vs. Discussions Differences

### Issues Workspace
- Only top-level comments (issues don't support replies)
- Same selection logic otherwise

### Discussions Workspace
- Top-level comments AND threaded replies
- Marks replies with "(reply)" indicator
- More complete context preservation

## Critical Design Decisions

### 1. "Most Recent Wins" Strategy

**Rationale**: The most recent output from an agent represents their final, refined work. Intermediate versions are superseded.

**Pros**:
- Clean, concise context for next stage
- Avoids overwhelming next agent with iteration history
- Automatically handles feedback loops

**Cons**:
- Loses refinement history (could be valuable for understanding evolution)
- Assumes final version is best (might have introduced new issues)

**Alternative**: Include full thread with all iterations marked

### 2. Time-Based Cutoff

**Rationale**: Only feedback **after** the final output matters to the next stage.

**Pros**:
- Prevents duplicate context
- Clear separation of "already addressed" vs "new" feedback

**Cons**:
- If a refinement happened but user's original feedback is still relevant, it's lost
- Relies on accurate timestamps

**Alternative**: Include all feedback in the thread regardless of timing, with indicators

### 3. Excluding Other Agents

**Rationale**: Linear workflow assumes only immediate predecessor matters.

**Pros**:
- Simple, predictable context
- Reduces token usage

**Cons**:
- For reviewer workflows (BA → BA_Reviewer → Architect), Architect doesn't see BA_Reviewer's output
- Cross-cutting concerns might be mentioned by other agents

**Alternative**: Include outputs from all previous stages, or specifically include reviewer agents

## Ideal Selection Criteria (Future Enhancements)

### 1. Context Modes

**Compact Mode** (current):
- Final output from previous agent only
- User feedback after final output
- Minimal tokens, clean context

**Full Mode**:
- Complete refinement thread with all iterations
- All feedback throughout the process
- Full history for complex decisions

**Smart Mode**:
- Final output + summary of refinement thread
- "Evolved through 3 rounds: performance → scaling → caching"
- Balance between context and brevity

### 2. Multi-Agent Context

For workflows with reviewers:
```
BA → BA_Reviewer → Architect
```

**Architect should receive**:
- BA's final output (after addressing reviewer feedback)
- BA_Reviewer's assessment and concerns
- Any user feedback on both

### 3. Semantic Filtering

Instead of pure chronological:
- Detect if feedback was "incorporated" vs "needs attention"
- Include only "needs attention" feedback
- Use LLM to classify feedback relevance

### 4. Thread Summarization

For long refinement threads:
```
## Output from Business Analyst

[Final version]

### Refinement History Summary
- Round 1: Added performance considerations (user request)
- Round 2: Expanded scaling section (user request)
- Round 3: Added caching strategies (user request)
```

## Configuration Options (Proposed)

```yaml
context_selection:
  mode: "compact"  # compact | full | smart
  include_reviewers: true
  max_feedback_items: 10
  thread_summarization: true
  semantic_filtering: false
```

## Testing Recommendations

### Test Cases

1. **Simple Pass-Through**
   - Single output, no refinement
   - Verify correct output passed

2. **Single Refinement**
   - Initial output + 1 threaded refinement
   - Verify final version passed, not initial

3. **Multiple Refinements**
   - Initial + 3 rounds of refinement
   - Verify most recent passed

4. **Mixed Feedback**
   - Top-level comments + threaded replies
   - Verify both types included

5. **Reviewer Workflow**
   - BA → BA_Reviewer → Architect
   - Verify Architect gets appropriate context

6. **No Previous Output**
   - First stage in workflow
   - Verify empty context returned

## Current Limitations

1. **Thread Explosion**: Many refinement threads could create large context
2. **Lost Nuance**: Intermediate iterations might have valuable context
3. **Reviewer Blind Spots**: Next stage doesn't see reviewer assessments
4. **No Cross-Stage Context**: Can't reference earlier stages (e.g., Architect can't see original user request from Research)

## Recommendations

### Short-Term
✅ **Current implementation is good for**:
- Linear workflows without reviewers
- Moderate refinement activity
- Token budget concerns

### Medium-Term
- Add "full mode" configuration option
- Include reviewer agent outputs explicitly
- Implement thread summarization

### Long-Term
- Semantic feedback classification
- Cross-stage context stitching
- Intelligent context compression
- ML-based relevance scoring
