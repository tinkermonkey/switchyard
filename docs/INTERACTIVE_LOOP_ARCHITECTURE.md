# Interactive Loop Architecture

## Overview

The orchestrator has two distinct services for managing interactive agent workflows:

1. **ReviewCycleManager** (`services/review_cycle.py`) - Automated maker-checker review loops
2. **HumanFeedbackLoopExecutor** (`services/human_feedback_loop.py`) - Human feedback loops

These are **separate and not interchangeable**. Do not confuse them.

## Threading Architecture

**CRITICAL**: Both systems use a non-blocking architecture to prevent blocking the main orchestrator thread.

- **All long-running operations run in background daemon threads**
- **Project monitor polls periodically** (default: 30 seconds)
- **No timeouts on feedback loops** - they run indefinitely until:
  - The card moves to a different column (manual progression)
  - The orchestrator process terminates (daemon threads exit)
- **Multi-project support** - Monitor can handle multiple projects and boards simultaneously

## Review Cycle Manager (Maker-Checker Pattern)

**File**: `services/review_cycle.py`
**Purpose**: Automated review loops where a "maker" agent creates work and a "reviewer" agent provides feedback.

### Features

- **Maker-Checker Loop**: Two agents work together (maker creates, reviewer reviews)
- **State Persistence**: Saves cycle state to disk for crash recovery
- **Resumption**: Can resume interrupted cycles after restart
- **Iteration Limits**: Prevents infinite loops (typically 3 iterations max)
- **Escalation**: Escalates to humans when:
  - Review is BLOCKED (critical issues)
  - Max iterations reached without approval
- **Review Parsing**: Parses reviewer comments for APPROVED/REQUEST_CHANGES/BLOCKED status

### When To Use

Use ReviewCycleManager for workflow columns with `type: review`:

```yaml
columns:
  - name: "Requirements Review"
    type: review
    maker_agent: business_analyst
    reviewer_agent: requirements_reviewer
    max_iterations: 3
```

### How It Works

1. Maker agent produces initial output
2. Reviewer agent reviews the output
3. If APPROVED → Cycle completes, advance to next column
4. If REQUEST_CHANGES → Maker revises based on feedback, goto step 2
5. If BLOCKED or max iterations → Escalate to human

### Key Methods

- `start_review_cycle()` - Start new review cycle
- `resume_review_cycle()` - Resume existing cycle
- `resume_active_cycles()` - Resume all active cycles after restart
- `_execute_review_loop()` - Core loop logic

## Human Feedback Loop Executor

**File**: `services/human_feedback_loop.py`
**Purpose**: Conversational loops where agents respond to human feedback in discussion threads.

### Features

- **Human-Driven**: Agent waits for human comments/feedback
- **Indefinite Monitoring**: Polls indefinitely for human feedback (no timeout)
- **Iteration Tracking**: Tracks how many feedback rounds occurred
- **No Escalation**: No concept of "blocked" - just waits for human input
- **Discussion-Based**: Works in GitHub Discussions for threaded conversations
- **Background Thread**: Runs in daemon thread to avoid blocking orchestrator

### When To Use

Use HumanFeedbackLoopExecutor for workflow columns with `type: conversational`:

```yaml
columns:
  - name: "Idea Research"
    type: conversational
    agent: idea_researcher
    feedback_timeout: 86400  # 24 hours
    auto_advance_on_complete: true
```

### How It Works

1. Agent produces initial output (posted to discussion) in background thread
2. Monitor discussion for human comments indefinitely
3. When human comments → Agent responds to feedback
4. Repeat until card moves to different column or process terminates
5. Runs in background daemon thread (non-blocking)

### Key Methods

- `start_loop()` - Start new human feedback loop
- `_conversational_loop()` - Core loop logic
- `_get_human_feedback_since_last_agent()` - Poll for new comments

## Comparison

| Feature | ReviewCycleManager | HumanFeedbackLoopExecutor |
|---------|-------------------|---------------------------|
| **Feedback Source** | Automated reviewer agent | Human in discussion thread |
| **Iteration Control** | Max iterations enforced | No max, runs indefinitely |
| **State Persistence** | Yes (survives restarts) | No (in-memory only) |
| **Resumption** | Yes | No |
| **Escalation** | Yes (BLOCKED or max iterations) | No escalation needed |
| **Review Parsing** | Yes (APPROVED/REQUEST_CHANGES/BLOCKED) | No (just detects human comments) |
| **Workspace** | Issues or Discussions | Discussions only |
| **Column Type** | `review` | `conversational` |
| **Threading** | Background daemon thread | Background daemon thread |
| **Timeout** | Escalated cycles poll indefinitely | No timeout, runs until card moves |

## Why Two Services?

These services were intentionally kept separate because:

1. **Different Patterns**: Maker-checker is fundamentally different from human feedback
2. **State Requirements**: Review cycles need crash recovery, human loops don't
3. **Complexity**: Combining them creates confusing conditional logic
4. **Clear Boundaries**: Separation makes the code easier to understand and maintain

## History Note

An earlier attempt was made to unify these into a single `ConversationalLoop` that handled both patterns. This was abandoned because:

- The `_review_loop()` method was never implemented (raised `NotImplementedError`)
- The unified approach created confusing state management
- The two patterns have different enough requirements to warrant separation

The code has been refactored to make this distinction clear.

## Usage in Project Monitor

```python
# For review columns
from services.review_cycle import review_cycle_executor
await review_cycle_executor.resume_review_cycle(...)

# For conversational columns
from services.human_feedback_loop import human_feedback_loop_executor
await human_feedback_loop_executor.start_loop(...)
```

## Configuration Examples

### Review Column (Maker-Checker)

```yaml
- name: "Design Review"
  type: review
  maker_agent: software_architect
  reviewer_agent: design_reviewer
  max_iterations: 3
  workspace: discussions
  auto_advance_on_complete: true
```

### Conversational Column (Human Feedback)

```yaml
- name: "Idea Development"
  type: conversational
  agent: idea_researcher
  workspace: discussions
  feedback_timeout: 86400  # 24 hours
  auto_advance_on_complete: true
```

## Best Practices

1. **Use the right service** - Don't try to force one pattern into the other's service
2. **Configure timeouts** - Human feedback loops need realistic timeouts
3. **Set iteration limits** - Review cycles should have max_iterations to prevent infinite loops
4. **Choose workspace carefully** - Discussions provide better threading than issues
5. **Handle escalations** - Have a plan for when review cycles escalate to humans
