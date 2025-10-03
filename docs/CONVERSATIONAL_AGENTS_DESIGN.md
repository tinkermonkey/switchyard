# Conversational Agents Design

## Problem Statement

**Current Behavior**: When users reply to agent comments in threads, agents generate full new reports regardless of the question asked.

**Example**:
```
Agent: [5000 word comprehensive analysis]
User: "What about caching strategies?"
Agent: [Another 5000 word report with caching added]
```

**Desired Behavior**: Conversational, context-aware responses like a chatbot.

```
Agent: [Initial comprehensive analysis]
User: "What about caching strategies?"
Agent: "Great question! For caching, I'd recommend Redis for this use case because...
       [targeted 200-word response]"
User: "How does that compare to Memcached?"
Agent: "Memcached would be simpler but less feature-rich. The key differences are...
       [targeted comparison]"
```

## Solution Architecture

### 1. Thread History Extraction

**New Function**: `get_full_thread_history()`

Extracts the complete conversation thread when feedback is in a reply:

```python
def get_full_thread_history(discussion_id: str, parent_comment_id: str) -> List[Dict]:
    """
    Get complete thread history for conversational context

    Returns: [
        {
            'role': 'agent',  # or 'user'
            'author': 'idea_researcher' # or username
            'body': '...',
            'timestamp': '...'
        },
        ...
    ]
    """
```

**Thread Structure**:
```
Parent Comment (Agent's Initial Output)
├─ Reply 1 (User question)
│   └─ Reply 2 (Agent answer)
│       └─ Reply 3 (User follow-up)
│           └─ Reply 4 (Agent answer) ← Current feedback trigger
```

### 2. Conversational Context Detection

Determine if this is:
- **Initial work**: No thread history, generate full report
- **Threaded feedback**: Has thread history, be conversational
- **Top-level feedback**: Has previous output but not threaded, generate updated report

```python
task_context = {
    'trigger': 'feedback_loop',
    'conversation_mode': 'threaded',  # NEW
    'thread_history': [...]  # NEW - full conversation
    'previous_output': '...',  # Agent's last output
    'feedback': {
        'formatted_text': '...'  # User's latest message
    }
}
```

### 3. Agent Prompt Adaptation

**Two Prompt Modes**:

#### Mode 1: Initial/Report Mode (Current)
```python
prompt = """
As an Idea Researcher, analyze the following concept...

Conduct comprehensive technical research covering:
1. Problem Abstraction
2. Solution Landscape
[... full analysis template]
"""
```

#### Mode 2: Conversational Mode (NEW)
```python
prompt = """
You are continuing a conversation about {topic}.

## Conversation History:
{format_thread_history(thread_history)}

## Latest Question:
{user_question}

Respond conversationally and directly to their question. Keep your response:
- Focused on answering their specific question
- Conversational in tone (use "I", "you", etc.)
- Concise (aim for 200-500 words unless they ask for more detail)
- Building on the conversation history
- Technical but approachable

If they're asking for expansion on a previous point, build on what you already said.
If it's a new topic, provide a focused answer without rehashing the entire analysis.
"""
```

### 4. Prompt Construction Logic

```python
async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
    task_context = context.get('context', {})

    # Detect conversation mode
    conversation_mode = task_context.get('conversation_mode')
    thread_history = task_context.get('thread_history', [])

    if conversation_mode == 'threaded' and thread_history:
        # Conversational mode
        prompt = self._build_conversational_prompt(
            task_context=task_context,
            thread_history=thread_history
        )
    elif task_context.get('trigger') == 'feedback_loop':
        # Top-level feedback - regenerate full report with updates
        prompt = self._build_update_prompt(task_context)
    else:
        # Initial work - full analysis
        prompt = self._build_initial_prompt(task_context)

    result = await run_claude_code(prompt, context)
    return result
```

### 5. Thread History Formatting

```python
def _format_thread_history(thread_history: List[Dict]) -> str:
    """Format thread history for prompt"""
    formatted = []

    for message in thread_history:
        role = message['role']
        author = message['author']
        body = message['body']

        if role == 'agent':
            formatted.append(f"**You** ({author} agent):\n{body}\n")
        else:
            formatted.append(f"**{author}**:\n{body}\n")

    return "\n".join(formatted)
```

### 6. Output Format Adaptation

**For Conversational Responses**:
- No need for full markdown report structure
- Can use casual markdown (bold, lists, code blocks as needed)
- Shorter, more direct
- Sign with simpler signature

```markdown
Great question about caching! Redis would be my recommendation here because...

The key trade-offs are:
- **Performance**: Redis is faster for...
- **Features**: Redis supports...

Would you like me to dive deeper into any of these aspects?

---
_Idea Researcher Agent_
```

vs. current format:

```markdown
## Caching Strategy Analysis

### Overview
[200 words]

### Implementation Approaches
1. Redis Implementation
   - Architecture
   - Performance characteristics
   ...

[Full structured report]

---
_Generated by Orchestrator Bot 🤖_
_Processed by the idea_researcher agent_
```

## Implementation Plan

### Phase 1: Thread History Extraction

**File**: `services/project_monitor.py`

```python
def get_full_thread_history(self, discussion_id: str, parent_comment_id: str) -> List[Dict]:
    """Extract complete thread history for conversational context"""
    # GraphQL query to get comment with all replies
    # Parse and format as conversation history
    # Return chronologically ordered list
```

**When to call**: In `check_for_feedback_in_discussion()` when creating feedback task:

```python
# If this is a threaded reply
if feedback_comment['is_reply'] and feedback_comment['parent_comment_id']:
    # Extract full thread history
    thread_history = self.get_full_thread_history(
        discussion_id,
        feedback_comment['parent_comment_id']
    )

    task_context['conversation_mode'] = 'threaded'
    task_context['thread_history'] = thread_history
```

### Phase 2: Agent Prompt Refactoring

**For ALL agents**, refactor from:
```python
async def execute(self, context):
    prompt = f"""[Static prompt]"""
    result = await run_claude_code(prompt, context)
```

To:
```python
async def execute(self, context):
    task_context = context.get('context', {})

    # Build appropriate prompt based on mode
    if task_context.get('conversation_mode') == 'threaded':
        prompt = self._build_conversational_prompt(task_context)
    elif task_context.get('trigger') == 'feedback_loop':
        prompt = self._build_feedback_prompt(task_context)
    else:
        prompt = self._build_initial_prompt(task_context)

    result = await run_claude_code(prompt, context)
```

### Phase 3: Base Class Utilities

**File**: `agents/base_agent.py` (new)

```python
class ConversationalAgentMixin:
    """Mixin providing conversational capabilities to agents"""

    def _build_conversational_prompt(self, task_context: Dict) -> str:
        """Build prompt for threaded conversation"""
        thread_history = task_context.get('thread_history', [])
        current_question = task_context['feedback']['formatted_text']
        issue_context = task_context.get('issue', {})

        return f"""
You are the {self.agent_name} agent continuing a technical conversation.

## Original Context:
Title: {issue_context.get('title')}

## Conversation So Far:
{self._format_thread_history(thread_history)}

## Latest Message:
{current_question}

Respond naturally and conversationally to their question. Guidelines:
- Be direct and concise (200-500 words unless they need more)
- Reference previous points in the conversation when relevant
- Use conversational tone ("I recommend...", "That's a great point...")
- Stay technical and accurate
- If they're asking for clarification, explain more clearly
- If they're asking for expansion, add focused new details
- If it's a new topic, provide a complete but focused answer

Your response will be posted as a threaded reply.
"""

    def _format_thread_history(self, history: List[Dict]) -> str:
        """Format thread history for prompt inclusion"""
        formatted = []
        for msg in history:
            if msg['role'] == 'agent':
                formatted.append(f"**You** ({msg['author']}):\n{msg['body']}\n")
            else:
                formatted.append(f"**@{msg['author']}**:\n{msg['body']}\n")
        return "\n".join(formatted)
```

### Phase 4: Update Stage Context Passing

**File**: `services/project_monitor.py::_get_discussion_context()`

Currently passes only final agent output. Update to include thread history:

```python
def _get_discussion_context(self, discussion_id: str, current_column: str, workflow_template) -> str:
    # ... existing logic to find previous agent ...

    # NEW: Extract full thread if final output was in a thread
    thread_context = []
    if previous_agent_comment_id:  # If we know which comment
        thread_context = self._extract_thread_context(
            all_comments,
            previous_agent_comment_id
        )

    # Format context
    context_parts = []
    context_parts.append(f"## Output from {previous_agent}")
    context_parts.append(previous_agent_output)

    # NEW: Include thread history if it exists
    if thread_context:
        context_parts.append("\n## Conversation Thread")
        for msg in thread_context:
            author = msg['author']
            body = msg['body']
            role = "Agent" if msg['is_agent'] else "User"
            context_parts.append(f"**{role} ({author})**: {body}")

    # User feedback since final output
    if user_feedback:
        context_parts.append("\n## Recent Feedback")
        ...
```

## Benefits

### For Users
1. **Natural interaction**: Ask follow-up questions conversationally
2. **Faster responses**: Get targeted 200-word answers instead of 5000-word reports
3. **Better clarity**: Can drill down with "explain more" without regenerating everything
4. **Context preservation**: Agent remembers the conversation

### For Next Stage Agents
1. **Richer context**: See the full dialogue, not just final output
2. **Understanding evolution**: See how ideas developed through questions
3. **User intent clarity**: See what users cared about (asked questions about)
4. **Better continuity**: Build on conversational insights

## Example Workflow

### Initial Request
```
Issue #93: Add vector search to schema.org database
```

**Idea Researcher (full report)**:
```markdown
## Schema.org Local Implementation Analysis

### 1. Problem Abstraction
The core abstract problem is...
[5000 word comprehensive analysis]
```

### User Thread 1: Performance Questions
```
User replies to idea_researcher comment:
"@orchestrator-bot What about performance with 10K nodes?"

Idea Researcher (conversational):
"Great performance question! With 10K nodes, you'd see:
- Initial index build: ~2-3 seconds
- Query latency: <50ms for vector similarity
- Memory usage: ~100MB for embeddings

The key is using sqlite-vec's HNSW index which scales well.
Want me to elaborate on the indexing strategy?"

User replies:
"Yes, explain the indexing strategy"

Idea Researcher:
"Sure! The HNSW (Hierarchical Navigable Small World) index works by...
[Focused 300-word explanation]"
```

### User Thread 2: Security Questions (parallel)
```
User replies to same idea_researcher comment:
"@orchestrator-bot What are the security implications?"

Idea Researcher (conversational):
"Good security thinking! The main considerations are:
- Read-only data: Lower attack surface
- Input validation: Must sanitize search queries
- Injection: Use parameterized queries
[Focused security discussion]"
```

### Next Stage: Business Analyst
**Receives full context**:
```markdown
## Output from Idea Researcher

[Original 5000-word analysis]

## Conversation Thread on Performance
User: What about performance with 10K nodes?
Agent: [performance answer]
User: Explain the indexing strategy
Agent: [indexing explanation]

## Conversation Thread on Security
User: What are the security implications?
Agent: [security discussion]

## Recent Feedback
[Any new top-level comments]
```

**BA has full picture of**:
- Original comprehensive analysis
- Performance concerns and answers
- Security concerns and answers
- What user cared enough to ask about

## Token Efficiency

**Current approach** (3 feedback rounds):
- Initial: 5000 tokens
- Feedback 1: 5000 tokens (full regeneration)
- Feedback 2: 5000 tokens (full regeneration)
- Feedback 3: 5000 tokens (full regeneration)
- **Total: 20,000 tokens**

**Conversational approach** (3 feedback rounds):
- Initial: 5000 tokens
- Feedback 1: 300 tokens (focused answer)
- Feedback 2: 400 tokens (focused answer)
- Feedback 3: 300 tokens (focused answer)
- **Total: 6,000 tokens (70% reduction)**

## Migration Strategy

### Phase 1: Infrastructure (Week 1)
- [ ] Implement `get_full_thread_history()`
- [ ] Update `check_for_feedback_in_discussion()` to extract threads
- [ ] Add `conversation_mode` and `thread_history` to task context

### Phase 2: Base Agent Class (Week 1)
- [ ] Create `ConversationalAgentMixin`
- [ ] Implement `_build_conversational_prompt()`
- [ ] Implement `_format_thread_history()`

### Phase 3: Pilot Agent (Week 2)
- [ ] Update `idea_researcher_agent.py` with conversational support
- [ ] Test with real discussions
- [ ] Iterate based on response quality

### Phase 4: Rollout (Week 2-3)
- [ ] Update remaining agents
- [ ] Update stage context passing
- [ ] Documentation

### Phase 5: Optimization (Week 4)
- [ ] Fine-tune prompt templates per agent type
- [ ] Add conversation summarization for long threads
- [ ] Metrics and monitoring

## Open Questions

1. **Thread length limits**: At what point do we summarize history instead of including full thread?
2. **Cross-thread awareness**: Should agent know about other parallel threads on same comment?
3. **Tone calibration**: How formal/casual should conversational responses be?
4. **Error handling**: What if thread history extraction fails?
5. **Backwards compatibility**: Should old-style full reports still be available via flag?
