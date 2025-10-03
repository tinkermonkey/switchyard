# Conversational Mode: Which Agents Should Use It?

## Executive Summary

**Recommendation**: Enable conversational mode for **all "maker" agents**, but **NOT for reviewer agents**.

**Rationale**: Conversational mode enhances collaboration on creative/analytical work, but reviewers need to maintain objective, comprehensive assessment frameworks that shouldn't be fragmented into Q&A.

## Agent Classification

### Category 1: Analytical & Creative Makers ✅ **Enable Conversational**

These agents produce analysis, designs, or strategies that benefit from iterative refinement through dialogue.

| Agent | Enable? | Rationale |
|-------|---------|-----------|
| **idea_researcher** | ✅ YES (implemented) | Research naturally involves exploring questions, comparisons, deep-dives |
| **business_analyst** | ✅ YES | Requirements evolve through clarifying questions; users often need specific sections explained |
| **software_architect** | ✅ YES | Architecture discussions are inherently conversational; users ask "why X over Y?", "how does this scale?" |
| **product_manager** | ✅ YES | Prioritization decisions benefit from stakeholder questions; "why is X higher than Y?" |
| **test_planner** | ✅ YES | Test strategies can be refined with "what about edge case X?", "how do we test Y?" |
| **technical_writer** | ✅ YES | Documentation improvements often come from "can you clarify X?", "add example for Y" |

**Why?**
- Their outputs are **exploratory and analytical**
- Users naturally have **follow-up questions**
- Iteration improves quality through focused refinements
- Conversational mode saves tokens on targeted updates

### Category 2: Implementation Makers ✅ **Enable Conversational** (with caveats)

These agents write code. Conversational mode works but requires careful consideration.

| Agent | Enable? | Rationale & Caveats |
|-------|---------|---------------------|
| **senior_software_engineer** | ✅ YES | Questions like "add error handling for X", "optimize Y" work well conversationally. **CAVEAT**: Code changes should still be complete/tested, not fragments |
| **senior_qa_engineer** | ✅ YES | Test additions can be conversational: "add test for edge case X". **CAVEAT**: Tests must still be complete and runnable |
| **dev_environment_setup** | ⚠️ MAYBE | Setup is often one-shot, but "fix Docker build issue X" works conversationally. **LOW PRIORITY** |

**Why?**
- Debugging conversations: "The test is failing because...", "Try adding X"
- Incremental improvements: "Add logging here", "Extract this to a function"
- **Risk**: Fragmenting code changes across thread could be confusing

**Mitigation**:
- Conversational responses should still produce **complete, working code**
- Agent should reference full file context, not just snippet
- Final thread result should be coherent when read top-to-bottom

### Category 3: Reviewer Agents ❌ **DO NOT Enable Conversational**

These agents provide objective assessments and should maintain comprehensive review frameworks.

| Agent | Enable? | Rationale |
|-------|---------|-----------|
| **requirements_reviewer** | ❌ NO | Reviews must be comprehensive; answering "what about X?" conversationally could skip other issues |
| **design_reviewer** | ❌ NO | Security/scalability reviews require systematic assessment, not Q&A |
| **code_reviewer** | ❌ NO | Code reviews need complete checklists; conversational mode risks incomplete coverage |
| **test_reviewer** | ❌ NO | Coverage analysis must be thorough, not fragmented |
| **documentation_editor** | ❌ NO | Editorial reviews need comprehensive pass, not piecemeal feedback |

**Why NOT?**
1. **Completeness Risk**: Conversational mode might answer specific questions but miss other issues
2. **Objectivity**: Reviewers should provide fresh, complete assessment each time
3. **Systematic Coverage**: Reviews follow checklists/frameworks that shouldn't be fragmented
4. **Approval Authority**: Reviews gate progression; can't be "partially approved" through conversation

**Example Problem**:
```
User: "@orchestrator-bot Is the error handling sufficient?"
Code Reviewer (conversational): "Yes, the error handling looks good with try/catch blocks..."

[Meanwhile, reviewer missed: security issues, performance problems, test gaps]
```

**Better**: Code reviewer always provides complete review covering all dimensions, even for feedback.

## Defocus Risk Analysis

### Does Conversational Mode Defocus Agents?

**Short Answer**: No, IF implemented correctly with these safeguards:

### Safeguard 1: Maintain Core Competency in All Modes

The conversational prompt must still invoke the agent's expertise:

```python
# BAD - Too generic
prompt = "Answer the user's question"

# GOOD - Maintains expertise
prompt = """
You are the Software Architect continuing a conversation.
Use your expertise in system design, scalability, and security to answer.
"""
```

### Safeguard 2: Initial Work is Still Comprehensive

Conversational mode is ONLY for **refinement**, not initial work:

```
Flow:
1. Initial: Full comprehensive report (5000 words)
2. Thread: "What about X?" -> Focused 300-word answer
3. Thread: "Compare X to Y?" -> Focused 400-word comparison

NOT:
1. Initial: 300-word partial analysis (WRONG!)
2. User forced to ask questions to get full picture (WRONG!)
```

### Safeguard 3: Context Preservation

Thread history ensures agents don't lose sight of the big picture:

```python
prompt = f"""
## Original Context
{issue_title and description}

## Your Previous Complete Analysis
{initial_5000_word_analysis}

## Conversation So Far
{thread_history}

## Current Question
{user_question}
"""
```

Agent still sees full context; focused response doesn't mean focused thinking.

### Safeguard 4: Fall Back to Full Reports When Needed

If conversation gets too fragmented or confusing:

```python
User: "@orchestrator-bot I'm lost, can you give me the full updated analysis?"
Agent: [Generates complete updated report]
```

Or agent proactively suggests:
```
Agent: "This is getting complex. Would you like me to provide an updated
       comprehensive analysis incorporating all these points?"
```

## Implementation Priority

### Phase 1: High-Value Analytical Agents (Week 1)
✅ `idea_researcher` - **DONE**
- `business_analyst`
- `software_architect`

**Why first?**: Pre-SDLC discussion stages benefit most; lots of back-and-forth

### Phase 2: Planning & Strategy Agents (Week 2)
- `product_manager`
- `test_planner`
- `technical_writer`

**Why second?**: High value, but fewer refinement rounds typically

### Phase 3: Implementation Agents (Week 3)
- `senior_software_engineer`
- `senior_qa_engineer`

**Why later?**: More complex to implement well; need careful testing to avoid code fragmentation

### Phase 4: Specialized Agents (If needed)
- `dev_environment_setup`

**Why last?**: Lower frequency of conversational feedback; one-shot nature

### Never Implement
- ❌ All reviewer agents
- ❌ Quality gate agents

## Configuration Approach

Make conversational mode **opt-in per agent** via config:

```yaml
# agents.yaml
agents:
  idea_researcher:
    conversational_mode: true
    conversational_token_budget: 500  # Max words per conversational reply

  business_analyst:
    conversational_mode: true
    conversational_token_budget: 500

  code_reviewer:
    conversational_mode: false  # Explicitly disabled
```

## Monitoring & Quality Gates

### Metrics to Track

1. **Token Efficiency**
   - Tokens per refinement round (conversational vs full report)
   - Total tokens per ticket through workflow

2. **Conversation Quality**
   - Average conversation thread length
   - User satisfaction (implicit: do they ask follow-ups?)
   - Coherence of final thread when read top-to-bottom

3. **Completeness**
   - Are users getting answers they need?
   - Do threads end naturally or peter out?
   - Are next-stage agents getting sufficient context?

4. **Defocus Detection**
   - Are conversational answers missing critical points?
   - Are users having to ask basic questions that should be in initial report?

### Warning Signs

**Agent IS defocused if**:
- Initial reports become shorter/less comprehensive
- Users consistently ask about basic topics agent should cover initially
- Conversational answers miss obvious related considerations
- Next-stage agents complain about incomplete context

**Agent is WELL-focused if**:
- Initial reports remain comprehensive
- Conversational answers are targeted but complete
- Users ask clarifying/deepening questions, not basic coverage questions
- Next-stage agents receive rich context

## Example Implementations

### Business Analyst (Good Fit)

```python
class BusinessAnalystAgent(PipelineStage, ConversationalAgentMixin):
    agent_display_name = "Business Analyst"
    agent_role_description = """
    I analyze business requirements, create user stories, and model processes.
    I apply CBAP best practices and ensure requirements are clear, complete,
    and testable.
    """

    # Conversational works well for:
    # - "Can you elaborate on user story X?"
    # - "What acceptance criteria did you consider for Y?"
    # - "How does this handle edge case Z?"
```

### Code Reviewer (Poor Fit)

```python
class CodeReviewerAgent(PipelineStage):
    # NO ConversationalAgentMixin!

    async def execute(self, context):
        # ALWAYS provide complete review, even for feedback
        if context.get('trigger') == 'feedback_loop':
            prompt = self._build_complete_review_prompt(context)
        else:
            prompt = self._build_initial_review_prompt(context)

        # Never conversational - always systematic and complete
```

## Recommendations Summary

### ✅ DO Enable For:
1. All pre-SDLC analytical agents (research, analysis, design, planning)
2. Technical writing and documentation
3. Product strategy and prioritization
4. Implementation agents (with careful guidelines)

### ❌ DON'T Enable For:
1. Any reviewer agents
2. Quality gate agents
3. Approval/validation agents

### 🎯 Implementation Principles:
1. **Conversational = Refinement, Not Initial Work**
2. **Maintain Full Context** in all modes
3. **Enable Fallback** to full reports
4. **Monitor Quality** to detect defocus
5. **Make It Configurable** per agent
6. **Preserve Expertise** in conversational prompts

### 📊 Success Criteria:
- Initial reports stay comprehensive (>3000 words for complex topics)
- Conversational replies are focused (200-500 words)
- Token reduction of 50-70% for multi-round refinement
- No complaints from next-stage agents about context quality
- Users engage naturally with follow-up questions
- Threads reach natural conclusions

## Conclusion

Conversational mode is a **powerful enhancement** for collaborative analytical work. It makes agents more natural to interact with while significantly reducing token costs for refinements.

The key is **selective application**: Enable for agents whose work naturally involves iterative exploration and refinement, but NOT for agents whose role requires systematic, comprehensive assessment.

With proper safeguards (maintaining full context, comprehensive initial work, expert prompts), conversational mode enhances rather than defocuses agent capabilities.

**Next Step**: Roll out to business_analyst and software_architect as Phase 1 expansion.
