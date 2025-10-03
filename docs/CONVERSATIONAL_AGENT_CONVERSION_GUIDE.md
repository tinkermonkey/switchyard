# Conversational Agent Conversion Guide

## Agents Already Converted ✅

1. **idea_researcher_agent.py** - Complete
2. **business_analyst_agent.py** - Complete
3. **software_architect_agent.py** - Complete

## Agents To Convert

### Phase 2 (Strategic/Planning):
- product_manager_agent.py
- test_planner_agent.py
- technical_writer_agent.py

### Phase 3 (Implementation):
- senior_software_engineer_agent.py
- senior_qa_engineer_agent.py

## Conversion Steps

### Step 1: Add Import

```python
# At top of file, add:
from agents.conversational_mixin import ConversationalAgentMixin
```

### Step 2: Update Class Declaration

```python
# Change from:
class MyAgent(PipelineStage):

# To:
class MyAgent(PipelineStage, ConversationalAgentMixin):
```

### Step 3: Add Agent Metadata in `__init__`

```python
def __init__(self, agent_config: Dict[str, Any] = None):
    super().__init__("agent_name", agent_config=agent_config)

    # ADD THESE TWO LINES:
    self.agent_display_name = "Display Name"  # Human-readable name
    self.agent_role_description = "Brief description of role and expertise"
```

### Step 4: Update `execute()` Method

Add mode detection logic at the start of execute():

```python
async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
    # Extract task context
    task_context = context.get('context', {})

    # Log conversation context
    self._log_conversation_context(task_context)

    # [Keep existing context extraction code]

    # Build prompt based on mode
    if self._should_use_conversational_mode(task_context):
        # Conversational mode - threaded reply
        prompt = self._build_conversational_prompt(
            agent_name=self.agent_display_name,
            agent_role_description=self.agent_role_description,
            task_context=task_context
        )
    elif task_context.get('trigger') == 'feedback_loop':
        # Feedback mode - update full report
        prompt = self._build_feedback_prompt(
            agent_name=self.agent_display_name,
            agent_role_description=self.agent_role_description,
            task_context=task_context,
            original_prompt_sections=[
                "Section 1",
                "Section 2",
                # List all major sections from your analysis
            ]
        )
    else:
        # Initial mode - keep existing prompt as-is
        prompt = f"""[Your existing full prompt]"""

    # [Rest of execute() stays the same]
```

## Agent-Specific Details

### product_manager_agent.py

**Display Name**: `"Product Manager"`

**Role Description**: `"I provide strategic product planning using the RICE framework (Reach, Impact, Confidence, Effort) to prioritize features and align product strategy with market needs."`

**Sections**:
```python
original_prompt_sections=[
    "RICE Prioritization Analysis",
    "Market Alignment Assessment",
    "Stakeholder Value Analysis",
    "Product Strategy Recommendations"
]
```

### test_planner_agent.py

**Display Name**: `"Test Planner"`

**Role Description**: `"I develop comprehensive test strategies covering unit, integration, system, and acceptance testing, using equivalence partitioning and boundary analysis."`

**Sections**:
```python
original_prompt_sections=[
    "Test Strategy Overview",
    "Test Coverage Plan",
    "Test Case Design",
    "Automation Strategy",
    "Performance Testing Approach"
]
```

### technical_writer_agent.py

**Display Name**: `"Technical Writer"`

**Role Description**: `"I create clear, accurate technical documentation including API docs, user guides, tutorials, and knowledge base content following documentation best practices."`

**Sections**:
```python
original_prompt_sections=[
    "Documentation Overview",
    "API Documentation",
    "User Guides",
    "Tutorial Content",
    "Code Examples"
]
```

### senior_software_engineer_agent.py

**Display Name**: `"Senior Software Engineer"`

**Role Description**: `"I implement clean code following SOLID principles, DRY, KISS, and YAGNI, with comprehensive test coverage and proper error handling."`

**Sections**:
```python
original_prompt_sections=[
    "Implementation Summary",
    "Code Structure",
    "Test Coverage",
    "Error Handling",
    "Performance Considerations"
]
```

**Special Note**: For code agents, ensure conversational responses still produce complete, working code snippets, not fragments.

### senior_qa_engineer_agent.py

**Display Name**: `"Senior QA Engineer"`

**Role Description**: `"I execute comprehensive quality assurance including integration testing, performance testing, and production readiness validation."`

**Sections**:
```python
original_prompt_sections=[
    "QA Test Results",
    "Integration Test Coverage",
    "Performance Test Results",
    "Production Readiness Assessment",
    "Quality Metrics"
]
```

## Quick Conversion Checklist

For each agent:

- [ ] Import ConversationalAgentMixin
- [ ] Add mixin to class declaration
- [ ] Add agent_display_name in __init__
- [ ] Add agent_role_description in __init__
- [ ] Add task_context extraction in execute()
- [ ] Add _log_conversation_context() call
- [ ] Add mode detection (conversational/feedback/initial)
- [ ] Define original_prompt_sections for feedback mode
- [ ] Wrap existing prompt in initial mode else block
- [ ] Test with threaded feedback

## Testing Procedure

After converting each agent:

1. Start orchestrator: `docker-compose restart orchestrator`
2. Create/find a discussion with the agent's output
3. Reply to agent comment: `@orchestrator-bot Can you elaborate on X?`
4. Verify:
   - Logs show "Using conversational mode"
   - Response is focused (200-500 words)
   - Response is posted as threaded reply
   - Agent maintains expertise
5. Try top-level feedback: `@orchestrator-bot Add section about Y`
6. Verify full updated report is generated

## Common Pitfalls

1. **Forgetting task_context extraction**: Must use `task_context = context.get('context', {})` not just `context`
2. **Wrong parameter passing**: Must pass `task_context` not `context` to mixin methods
3. **Missing sections list**: feedback_prompt requires `original_prompt_sections` parameter
4. **Not testing both modes**: Test both threaded replies AND top-level feedback

## Roll-Out Order

1. ✅ Phase 1: business_analyst, software_architect (DONE)
2. ⏳ Phase 2: product_manager, test_planner, technical_writer (DO NEXT)
3. ⏳ Phase 3: senior_software_engineer, senior_qa_engineer (DO LAST - more complex)

## Success Metrics

After full roll-out, monitor:
- Token reduction on refinement rounds (target: 60-70%)
- User engagement with threaded feedback
- Completeness of initial reports (should stay comprehensive)
- Quality of conversational responses
- Next-stage agent satisfaction with context

## Rollback Plan

If issues arise:
1. Comment out mode detection logic
2. Revert to just using initial mode prompt
3. Agent still works, just without conversational mode
4. Investigate and fix
5. Re-enable

Each agent is independent - can rollback one without affecting others.
