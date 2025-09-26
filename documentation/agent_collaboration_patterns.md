# Agent Collaboration Patterns for Claude Code Orchestrator

## Core Design Principles

### 1. **Dual Communication Channels**
- **GitHub Comments**: Human-readable updates for oversight and audit trail
- **Structured Handoffs**: Efficient JSON/YAML data transfer between agents
- **State Management**: Persistent context across agent sessions

### 2. **Maker-Checker Pattern**
```
Issue Created → Business Analyst (Maker) → Requirements Reviewer (Checker)
            → Software Architect (Maker) → Architecture Reviewer (Checker)
            → Senior Engineer (Maker) → Code Reviewer (Checker)
```

### 3. **Human-in-the-Loop Visibility**
All agent activities are visible through GitHub with escalation paths for human intervention.

## Communication Architecture

### Sequential Pipeline with Structured Handoffs

```python
# Example handoff structure
handoff_object = {
    "handoff_id": "req_analysis_001",
    "source_agent": "business_analyst",
    "target_agent": "requirements_reviewer",
    "context": {
        "issue_number": 123,
        "project": "my-app",
        "priority": "high"
    },
    "artifacts": {
        "requirements_document": {
            "functional_requirements": [...],
            "user_stories": [...],
            "acceptance_criteria": [...]
        },
        "quality_metrics": {
            "completeness_score": 0.85,
            "clarity_score": 0.90
        }
    },
    "decisions_made": [
        "Chose microservices architecture",
        "Selected React for frontend"
    ],
    "questions_for_next_agent": [
        "Should we use GraphQL or REST?",
        "What's the preferred state management approach?"
    ],
    "github_references": {
        "issue_url": "https://github.com/org/repo/issues/123",
        "comment_id": 456789
    }
}
```

### Parallel Review Pattern

```yaml
# config/review_workflows.yaml
review_patterns:
  requirements_review:
    primary_agent: "business_analyst"
    reviewers:
      - agent: "requirements_reviewer"
        focus: ["completeness", "clarity", "testability"]
        blocking: true
      - agent: "product_manager"
        focus: ["business_value", "priorities"]
        blocking: false
    escalation:
      threshold: 2  # Number of "must fix" issues
      human_review_required: true
```

## Agent Collaboration Examples

### 1. Requirements Analysis → Review Loop

**Business Analyst Actions:**
- Creates GitHub issue comment with requirements analysis
- Generates structured handoff with requirements document
- @mentions Requirements Reviewer in GitHub

**Requirements Reviewer Actions:**
- Receives structured handoff with full context
- Posts review findings as GitHub comment
- Returns structured feedback with categorized issues

```python
# In requirements_reviewer_agent.py
async def execute(self, context):
    # Receive structured handoff
    requirements = context['artifacts']['requirements_document']

    # Perform review analysis
    review_result = await self.analyze_requirements(requirements)

    # Post human-readable comment to GitHub
    await self.post_github_comment(
        issue_number=context['issue_number'],
        comment=self.format_review_comment(review_result)
    )

    # Return structured feedback for orchestrator
    return {
        'review_passed': review_result['blocking_issues'] == 0,
        'feedback': review_result,
        'next_actions': self.determine_next_steps(review_result)
    }
```

### 2. Code Review with Multi-Agent Feedback

```python
# Code review orchestration
class CodeReviewOrchestrator:
    async def execute_review(self, pr_context):
        # Parallel review by multiple agents
        review_tasks = [
            self.security_reviewer.review(pr_context),
            self.performance_reviewer.review(pr_context),
            self.code_quality_reviewer.review(pr_context)
        ]

        reviews = await asyncio.gather(*review_tasks)

        # Aggregate feedback
        consolidated_review = self.consolidate_reviews(reviews)

        # Post to GitHub PR
        await self.post_pr_review(pr_context, consolidated_review)

        return consolidated_review
```

### 3. Cross-Agent Context Sharing

```python
# Enhanced context management
class AgentContext:
    def __init__(self):
        self.conversation_history = []
        self.decisions_made = []
        self.open_questions = []
        self.artifacts = {}
        self.github_references = {}

    def add_decision(self, agent, decision, rationale):
        self.decisions_made.append({
            'agent': agent,
            'decision': decision,
            'rationale': rationale,
            'timestamp': datetime.now(),
            'github_comment_url': self.current_comment_url
        })

    def get_context_for_agent(self, agent_name):
        """Filter context relevant to specific agent"""
        return {
            'relevant_decisions': self.filter_decisions_for(agent_name),
            'pending_questions': self.get_questions_for(agent_name),
            'available_artifacts': self.artifacts,
            'conversation_summary': self.summarize_for(agent_name)
        }
```

## GitHub Integration Patterns

### 1. Issue-Driven Workflow
```
GitHub Issue → Business Analysis → Requirements Review → Design → Implementation → Testing → Done
     ↓              ↓                    ↓              ↓           ↓               ↓
  Comment        Comment             Comment        PR Created   PR Review    Issue Closed
```

### 2. Pull Request Review Workflow
```
PR Created → Automated Reviews (Security, Performance, Quality)
          → Consolidated Review Posted
          → Human Review (if needed)
          → Merge or Request Changes
```

### 3. Comment-Based Communication
```python
# GitHub comment formatting for agent communication
class GitHubCommentFormatter:
    def format_agent_update(self, agent_name, status, details):
        return f"""
## 🤖 {agent_name} Update

**Status:** {status}

### Summary
{details['summary']}

### Key Findings
{self.format_findings(details['findings'])}

### Next Steps
{self.format_next_steps(details['next_steps'])}

### Artifacts Generated
{self.format_artifacts(details['artifacts'])}

---
*Generated by Claude Code Orchestrator at {datetime.now()}*
        """
```

## Best Practices

### 1. **Structured Data + Human Readability**
- Always generate both machine-readable handoffs AND human-readable GitHub comments
- Use consistent formatting for GitHub visibility
- Include links between related artifacts

### 2. **Context Preservation**
```python
# Example: Preserving decisions across agents
class DecisionTracker:
    def record_decision(self, agent, decision_type, details):
        decision = {
            'id': uuid4(),
            'agent': agent,
            'type': decision_type,
            'details': details,
            'timestamp': datetime.now(),
            'github_issue': self.current_issue,
            'rationale': details.get('rationale'),
            'alternatives_considered': details.get('alternatives', [])
        }
        self.decisions.append(decision)
        return decision['id']
```

### 3. **Error Handling and Escalation**
```python
class QualityGate:
    def evaluate_handoff(self, handoff):
        issues = []

        # Check completeness
        if handoff.completeness_score < self.thresholds['completeness']:
            issues.append({
                'severity': 'blocking',
                'message': 'Requirements analysis incomplete',
                'suggested_action': 'Return to Business Analyst for completion'
            })

        # Escalate to human if too many blocking issues
        if self.count_blocking_issues(issues) >= self.escalation_threshold:
            self.escalate_to_human(handoff, issues)

        return issues
```

### 4. **Agent Memory and Learning**
```python
# Agents can reference previous similar work
class AgentMemory:
    async def find_similar_work(self, current_task):
        """Find similar previous tasks for context"""
        similar_tasks = await self.search_previous_handoffs(
            task_type=current_task.type,
            project=current_task.project
        )

        return {
            'patterns_used': self.extract_patterns(similar_tasks),
            'decisions_made': self.extract_decisions(similar_tasks),
            'lessons_learned': self.extract_lessons(similar_tasks)
        }
```

## Implementation Recommendations

### 1. **Start with Sequential Pipeline**
Begin with simple sequential handoffs, then add parallel review patterns as needed.

### 2. **GitHub as Source of Truth**
All major decisions and status updates should be visible in GitHub issues/PRs.

### 3. **Structured Handoff Objects**
Use consistent JSON schemas for agent-to-agent communication.

### 4. **Quality Gates at Each Stage**
Validate handoffs before proceeding to prevent error propagation.

### 5. **Human Escalation Paths**
Always provide clear escalation when agents get stuck or disagree.

This architecture gives you both efficient agent collaboration AND full human visibility through GitHub.