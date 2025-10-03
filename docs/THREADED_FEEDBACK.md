# Threaded Feedback in GitHub Discussions

## Overview

The orchestrator now supports **threaded feedback** in GitHub Discussions, allowing users to reply directly to agent outputs and have refined responses posted in the same thread. This creates a natural, conversation-like workflow that keeps related feedback and refinements together.

## Key Features

### 1. Threaded Replies Detection
- The orchestrator monitors both top-level comments and reply threads for `@orchestrator-bot` mentions
- When a user replies to an agent's comment, the system automatically detects which agent produced that output
- Feedback is routed to the correct agent based on the parent comment's signature

### 2. Automatic Agent Routing
- **Direct Threading**: If you reply to an agent's comment, that agent is automatically selected
- **Chronological Fallback**: If the parent isn't an agent comment, the system uses the most recent agent comment before the feedback
- **Reviewer Routing**: Feedback on reviewer comments is routed to the original maker agent

### 3. Nested Conversation Flow
- Agents post refined outputs as replies in the same thread
- Multiple rounds of refinement can happen in a single thread
- Each thread maintains its own conversation history
- Top-level comments remain clean, showing only initial agent outputs

## Usage

### Basic Threaded Feedback

1. **Find an agent's comment** in a discussion (look for the signature: `_Processed by the {agent_name} agent_`)

2. **Reply to that comment** with your feedback and mention `@orchestrator-bot`:
   ```
   @orchestrator-bot The analysis looks good, but can you expand on the
   performance considerations section? Specifically, how would this scale
   to 10K concurrent users?
   ```

3. **The agent will respond** in the same thread with the refined output

### Multiple Refinements

You can continue the conversation with multiple rounds:

```
User: @orchestrator-bot Can you add more detail about caching strategies?
Agent: [Posts refined analysis with caching details as a reply]
User: @orchestrator-bot Great! Now can you compare Redis vs Memcached?
Agent: [Posts comparison as another reply in the same thread]
```

### Top-Level vs. Threaded Feedback

**Top-Level Comment** (traditional):
```
@orchestrator-bot Please add security considerations
```
- Routes to the most recent agent that commented
- Agent posts response as a new top-level comment

**Threaded Reply** (new):
```
(Reply to agent comment) @orchestrator-bot Please add security considerations
```
- Routes to the agent whose comment you replied to
- Agent posts response as a reply in the same thread
- Keeps all related refinements together

## Architecture

### Feedback Detection Flow

```
1. Monitor Discussion
   └─> Fetch comments with replies (GraphQL)
       └─> Check top-level comments for @orchestrator-bot
       └─> Check reply threads for @orchestrator-bot
           └─> Identify parent comment
               └─> Extract agent signature from parent
                   └─> Route to correct agent

2. Create Feedback Task
   └─> Include reply_to_comment_id in task context
       └─> Agent processes feedback
           └─> Agent posts with reply_to_id
               └─> Response appears in thread
```

### Key Components

**`project_monitor.py::check_for_feedback_in_discussion()`**
- GraphQL query includes `replies(first: 50)` to fetch threaded replies
- Loops through both comments and replies to find `@orchestrator-bot` mentions
- Stores `parent_comment_id` and `is_reply` flags in feedback data

**`project_monitor.py::create_feedback_task_for_discussion()`**
- Accepts `reply_to_comment_id` parameter
- Adds it to task context for agent to use

**All Agent Implementations**
- Extract `reply_to_comment_id` from task context
- Pass it to `github.post_agent_output(context, comment, reply_to_id=reply_to_id)`

**`github_integration.py::post_agent_output()`**
- Routes to `_post_discussion_comment()` with `reply_to_id`
- Uses GitHub Discussions API's `replyToId` parameter

**`github_discussions.py::add_discussion_comment()`**
- GraphQL mutation with optional `replyToId`
- Creates nested reply when `reply_to_id` provided

### Agent Signature Detection

The system identifies which agent created a comment by looking for:
```
_Processed by the {agent_name} agent_
```

This signature is checked in:
1. `has_agent_processed_discussion()` - Prevents duplicate runs
2. `check_for_feedback_in_discussion()` - Routes feedback to correct agent
3. Both top-level comments and reply threads

## Benefits

### For Users
- **Contextual Refinement**: Reply exactly where you want changes
- **Organized Discussions**: Each topic has its own thread
- **Clear History**: Easy to see what was refined and why
- **Multiple Iterations**: Continue refining in the same conversation

### For the System
- **Precise Routing**: No ambiguity about which output needs refinement
- **Scalability**: Parallel refinements on different aspects
- **Maintainability**: Clear parent-child relationships in data
- **Extensibility**: Foundation for more complex threading patterns

## Example Workflow

### Scenario: Requirements Refinement

**Initial Discussion Post** (auto-created from issue):
```markdown
# Requirements Analysis

Auto-created from Issue #93

## User Request
[Original request text...]
```

**Business Analyst Comment**:
```markdown
## Business Requirements Analysis

### Functional Requirements
- Vector search for titles and definitions
- Generic node-link data model
- No schema migrations needed

[... full analysis ...]

_Processed by the business_analyst agent_
```

**User Reply #1** (threaded to BA comment):
```markdown
@orchestrator-bot Can you add more details about the data import workflow?
Specifically, how do we handle partial failures during import?
```

**Business Analyst Reply** (in same thread):
```markdown
## Import Workflow Analysis

### Error Handling Strategy
- Transaction-based imports with rollback on failure
- Partial import recovery with checkpoint/resume
[... detailed refinement ...]

_Processed by the business_analyst agent_
```

**User Reply #2** (continuing same thread):
```markdown
@orchestrator-bot Perfect! Now can you add performance benchmarks for
1000 nodes vs 10000 nodes?
```

**Business Analyst Reply** (still in thread):
```markdown
## Performance Benchmarks

[... benchmark details ...]

_Processed by the business_analyst agent_
```

Meanwhile, in a **separate thread** on the Software Architect's comment:
```markdown
User: @orchestrator-bot The architecture looks good, but can you add
a diagram showing the vector index flow?

Architect: [Posts diagram and explanation as a reply]
```

**Result**: Clean, organized discussion with parallel refinement threads

## Testing

To test threaded feedback:

1. Find a discussion with an agent comment
2. Reply to that comment with `@orchestrator-bot [your feedback]`
3. Check orchestrator logs for: `"Reply is threaded to {agent_name} agent comment"`
4. Verify the refined output appears as a reply in the same thread (not a new top-level comment)

## Migration Notes

- **Backward Compatible**: Top-level `@orchestrator-bot` mentions still work
- **No Config Changes**: Threaded feedback is automatically enabled for all discussions
- **Existing Discussions**: Work immediately with threaded replies
- **Issues Workspace**: Not affected (issues don't support threaded replies)

## Future Enhancements

Potential improvements:
- **Thread Summarization**: Automatically summarize long threads
- **Multi-Agent Threads**: Allow different agents to contribute to same thread
- **Thread Branching**: Fork threads for exploring alternatives
- **Thread Resolution**: Mark threads as resolved when refinement is complete
- **Thread Analytics**: Track refinement patterns and iteration counts
