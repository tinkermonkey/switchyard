# PR Review Agent - Design Document

## Overview

The PR Review Agent is an automated code review agent that performs intelligent PR reviews when all child issues of a parent issue reach the "Staged" column. It uses Claude Code's `/review` command to analyze pull requests, filters out scope-creep suggestions, and creates new sub-issues for actionable recommendations.

## Core Concept

When a parent issue has multiple sub-issues (created by the work breakdown agent), each sub-issue contributes code changes to a shared feature branch. The workflow operates as follows:

**PR Creation** (when first child enters Code Review):
- When the first child issue moves from Development → Code Review, a draft PR is automatically created for the parent's feature branch
- Subsequent child issues reuse this same PR

**PR Review** (when all children reach Staged):
- As sub-issues complete and move to "Staged," the PR Review Agent monitors for completion of ALL child issues
- Once the last child issue reaches "Staged," the agent automatically:
  1. Reviews the accumulated PR changes
  2. Filters review feedback for scope appropriateness
  3. Creates new sub-issues for each valid recommendation
  4. Repeats automatically when those new sub-issues complete (max 3 iterations)

## Architecture

### Agent Classification

- **Type**: `MakerAgent` (produces GitHub issues as output)
- **Name**: `pr_review_agent`
- **Model**: `claude-opus-4-20250514` (for high-quality review analysis)
- **Capabilities**: `code_review`, `pr_analysis`, `issue_creation`
- **Docker Requirements**: `requires_docker: true`, `requires_dev_container: false`
- **File Operations**: `makes_code_changes: false`, `filesystem_write_allowed: false`

### Trigger Mechanism

The agent is triggered when:
1. An issue moves to "Staged" column
2. That issue has a parent issue (detected via patterns in issue body)
3. All child issues of that parent are now in "Staged" or "Done"
4. The parent issue has an open PR
5. PR review count for this parent < 3 (safety limit)

**Detection Logic** (in `services/project_monitor.py`):

```python
async def _check_for_pr_review_trigger(
    self,
    project_name: str,
    issue_number: int,
    column_name: str
) -> Optional[Dict[str, Any]]:
    """
    Check if moving this issue to Staged triggers PR review.

    Returns task context if review should be triggered, None otherwise.
    """
    if column_name != "Staged":
        return None

    # Get parent issue
    github_integration = self._get_github_integration(project_name)
    parent_issue_number = await self.feature_branch_manager.get_parent_issue(
        github_integration, issue_number, project=project_name
    )

    if not parent_issue_number:
        return None  # Standalone issue, no PR review needed

    # Check if all siblings are in Staged/Done
    all_children = await self._get_child_issues(
        github_integration, parent_issue_number
    )

    all_staged = all(
        child['column'] in ['Staged', 'Done']
        for child in all_children
    )

    if not all_staged:
        return None  # Wait for all siblings

    # Check PR review iteration count
    review_count = await self._get_pr_review_count(
        project_name, parent_issue_number
    )

    if review_count >= 3:
        logger.info(
            f"PR review limit reached for parent #{parent_issue_number} "
            f"({review_count}/3 reviews)"
        )
        return None

    # Get PR number
    pr_number = await self._get_pr_for_parent(
        github_integration, parent_issue_number
    )

    if not pr_number:
        logger.warning(
            f"No open PR found for parent #{parent_issue_number}, "
            f"skipping PR review"
        )
        return None

    # Trigger PR review
    return {
        'project': project_name,
        'parent_issue': parent_issue_number,
        'pr_number': pr_number,
        'review_iteration': review_count + 1,
        'child_issues': all_children
    }
```

### Review Execution

The agent executes Claude Code's `/review` command through the orchestrator's Claude integration:

```python
async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
    """Execute PR review"""
    task_context = context.get('context', {})
    project = task_context['project']
    parent_issue = task_context['parent_issue']
    pr_number = task_context['pr_number']
    review_iteration = task_context['review_iteration']

    # Build prompt for Claude Code
    prompt = f"""
You are a PR Review Agent performing automated code review.

## Task
Review the pull request for parent issue #{parent_issue} (PR #{pr_number})
using the `/review` command.

## Context
- Review iteration: {review_iteration}/3
- Parent issue: #{parent_issue}
- Child issues completed: {len(task_context['child_issues'])}

## Instructions
1. Execute: `/review` to analyze the PR
2. Extract all recommendations from the review output
3. For each recommendation, output:
   - **Category**: bug_fix, performance, security, code_quality, refactoring,
                   infrastructure, abstraction
   - **Title**: Brief description (max 60 chars)
   - **Description**: Full explanation with code references
   - **Priority**: high, medium, low

## Output Format
Provide your analysis as markdown with this structure:

## PR Review Summary
[High-level summary of the PR quality and major findings]

## Recommendations

### [Recommendation Title]
- **Category**: [category]
- **Priority**: [priority]
- **Description**: [detailed description with file:line references]

[Continue for each recommendation...]

## Review Metrics
- Total recommendations: [count]
- High priority: [count]
- Medium priority: [count]
- Low priority: [count]
"""

    # Execute with Claude Code
    result = await run_claude_code(prompt, context)

    # Parse review output
    recommendations = self._parse_recommendations(result)

    # Filter recommendations
    filtered = self._filter_recommendations(recommendations)

    # Create sub-issues for each recommendation
    await self._create_review_issues(
        project, parent_issue, filtered, review_iteration
    )

    # Increment PR review count
    await self._increment_review_count(project, parent_issue)

    # Store results in context
    context['pr_review_result'] = {
        'total_recommendations': len(recommendations),
        'filtered_recommendations': len(filtered),
        'issues_created': len(filtered),
        'review_iteration': review_iteration
    }

    return context
```

### Recommendation Filtering

The filtering mechanism removes scope-increasing suggestions:

```python
def _filter_recommendations(
    self,
    recommendations: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Filter out recommendations that increase scope.

    Removes:
    - Infrastructure additions (observability, monitoring, logging frameworks)
    - Abstraction layers (transaction boundaries, new patterns)
    - Architectural changes (new modules, services, databases)

    Keeps:
    - Bug fixes
    - Performance improvements (within existing architecture)
    - Security issues
    - Code quality improvements (within existing patterns)
    - Refactoring (that doesn't add new abstractions)
    """

    # Keywords that indicate scope increase
    scope_increase_keywords = {
        'infrastructure': [
            'observability', 'monitoring', 'tracing', 'metrics',
            'alerting', 'logging framework', 'telemetry',
            'introduce.*infrastructure', 'add.*observability'
        ],
        'abstraction': [
            'transaction', 'transactional boundary', 'abstraction layer',
            'design pattern', 'factory', 'repository pattern',
            'service layer', 'introduce.*pattern', 'add.*abstraction'
        ],
        'architecture': [
            'new module', 'new service', 'new database', 'new dependency',
            'microservice', 'introduce.*architecture', 'split.*service'
        ]
    }

    filtered = []
    for rec in recommendations:
        category = rec.get('category', '').lower()
        title = rec.get('title', '').lower()
        description = rec.get('description', '').lower()

        # Always keep bug fixes and security issues
        if category in ['bug_fix', 'security']:
            filtered.append(rec)
            continue

        # Check for scope-increasing patterns
        is_scope_increase = False
        for reason, keywords in scope_increase_keywords.items():
            for keyword_pattern in keywords:
                if (re.search(keyword_pattern, title, re.IGNORECASE) or
                    re.search(keyword_pattern, description, re.IGNORECASE)):
                    logger.info(
                        f"Filtered recommendation '{rec['title']}' "
                        f"(reason: {reason}, pattern: {keyword_pattern})"
                    )
                    is_scope_increase = True
                    break
            if is_scope_increase:
                break

        if not is_scope_increase:
            filtered.append(rec)

    return filtered
```

### Issue Creation

For each filtered recommendation, create a sub-issue linked to the parent:

```python
async def _create_review_issues(
    self,
    project: str,
    parent_issue: int,
    recommendations: List[Dict[str, Any]],
    review_iteration: int
) -> List[int]:
    """
    Create GitHub sub-issues for PR review recommendations.

    Returns list of created issue numbers.
    """
    github_client = get_github_client()
    project_config = self.config_manager.get_project_config(project)
    org = project_config.github.org
    repo = project_config.github.repo

    created_issues = []

    for i, rec in enumerate(recommendations, 1):
        title = f"[PR Review R{review_iteration}] {rec['title']}"

        body = f"""## Parent Issue
Part of #{parent_issue}

## PR Review Recommendation
**Iteration**: {review_iteration}/3
**Category**: {rec['category']}
**Priority**: {rec['priority']}

## Description
{rec['description']}

## Context
This issue was automatically created by the PR Review Agent after reviewing
the accumulated changes for parent issue #{parent_issue}.

---
*Generated by PR Review Agent*
"""

        # Create issue
        issue_number = await github_client.create_issue(
            org=org,
            repo=repo,
            title=title,
            body=body,
            labels=[
                'pipeline:sdlc-execution',
                f'priority:{rec["priority"]}',
                f'category:{rec["category"]}',
                f'pr-review-iteration:{review_iteration}'
            ]
        )

        created_issues.append(issue_number)

        # Add to Development column
        await self._add_issue_to_column(
            project, issue_number, column_name='Development'
        )

        logger.info(
            f"Created PR review issue #{issue_number} for parent #{parent_issue}: "
            f"{rec['title']}"
        )

    return created_issues
```

## State Management

### PR Review Iteration Tracking

Track review iterations to enforce 3-iteration limit:

**State File**: `state/projects/{project}/pr_review_state.yaml`

```yaml
# PR review iteration tracking
pr_reviews:
  # parent_issue_number: review_count
  53:
    review_count: 2
    last_review_at: "2025-11-04T10:30:00Z"
    issues_created:
      - iteration: 1
        issues: [101, 102, 103]
      - iteration: 2
        issues: [110, 111]
```

**State Manager** (`state_management/pr_review_state_manager.py`):

```python
class PRReviewStateManager:
    """Manages PR review iteration state"""

    def __init__(self, state_dir: str = "state"):
        self.state_dir = Path(state_dir)

    def get_review_count(self, project: str, parent_issue: int) -> int:
        """Get current review iteration count"""
        state = self._load_state(project)
        return state.get('pr_reviews', {}).get(parent_issue, {}).get('review_count', 0)

    def increment_review_count(
        self,
        project: str,
        parent_issue: int,
        created_issues: List[int]
    ):
        """Increment review count and record created issues"""
        state = self._load_state(project)

        if 'pr_reviews' not in state:
            state['pr_reviews'] = {}

        if parent_issue not in state['pr_reviews']:
            state['pr_reviews'][parent_issue] = {
                'review_count': 0,
                'issues_created': []
            }

        parent_state = state['pr_reviews'][parent_issue]
        parent_state['review_count'] += 1
        parent_state['last_review_at'] = datetime.now().isoformat()
        parent_state['issues_created'].append({
            'iteration': parent_state['review_count'],
            'issues': created_issues
        })

        self._save_state(project, state)
```

## Configuration

### Agent Definition (`config/foundations/agents.yaml`)

```yaml
pr_review_agent:
  description: "Automated PR review with scope-filtered recommendations"
  model: "claude-opus-4-20250514"
  timeout: 600  # 10 minutes for review analysis
  retries: 2
  makes_code_changes: false
  filesystem_write_allowed: false
  requires_dev_container: false
  requires_docker: true
  capabilities:
    - code_review
    - pr_analysis
    - issue_creation
    - recommendation_filtering
  tools_enabled:
    - file_operations
    - git_integration
  mcp_servers:
    - context7
```

### Workflow Integration

The PR Review Agent operates outside the standard pipeline workflow. It's triggered by the project monitor when specific conditions are met (all child issues staged).

**No workflow column mapping** - The agent creates issues directly into the "Development" column of the SDLC workflow.

## Agent Implementation

### Class Structure (`agents/pr_review_agent.py`)

```python
"""
PR Review Agent

Performs automated code review when all child issues of a parent reach Staged.
Uses Claude Code's /review command and filters recommendations for scope appropriateness.
"""

from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class PRReviewAgent(MakerAgent):
    """
    Automated PR Review Agent

    Triggered when all child issues of a parent issue reach "Staged".
    Executes Claude Code /review, filters scope-increasing suggestions,
    and creates sub-issues for actionable recommendations.
    """

    @property
    def agent_display_name(self) -> str:
        return "PR Review Agent"

    @property
    def agent_role_description(self) -> str:
        return """
Expert code reviewer specializing in pull request analysis and quality assurance.
Reviews accumulated changes from completed sub-issues, identifies issues requiring
attention, and filters out scope-creep suggestions.
"""

    @property
    def output_sections(self) -> List[str]:
        return [
            "PR Review Summary",
            "Recommendations",
            "Review Metrics"
        ]

    def get_initial_guidelines(self) -> str:
        return """
## Review Focus Areas

Prioritize:
1. **Bugs**: Logic errors, edge cases, null pointer issues
2. **Security**: Input validation, authentication, authorization, injection risks
3. **Performance**: Inefficient algorithms, memory leaks, N+1 queries
4. **Code Quality**: Readability, maintainability, test coverage

Avoid suggesting:
- New infrastructure (observability, monitoring, logging frameworks)
- New abstraction layers (transaction boundaries, design patterns)
- Architectural changes (new modules, services, databases)

## Recommendation Categories

Use these categories for classification:
- `bug_fix`: Logic errors, incorrect behavior
- `security`: Security vulnerabilities or risks
- `performance`: Performance optimizations
- `code_quality`: Code clarity, maintainability, testing
- `refactoring`: Simplification within existing patterns
- `infrastructure`: New monitoring, observability (FILTERED)
- `abstraction`: New patterns, boundaries (FILTERED)
"""

    def get_quality_standards(self) -> str:
        return """
## Quality Standards

1. **Specificity**: Include file paths and line numbers
2. **Actionability**: Clear steps to address the issue
3. **Impact**: Explain consequences of not addressing
4. **Prioritization**: Distinguish critical from nice-to-have
5. **Scope Awareness**: Avoid expanding project scope
"""

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute PR review (override base implementation for special logic)"""
        # Implementation as shown in "Review Execution" section above
        # ...
```

### Parser Utility

Parse Claude Code's review output into structured recommendations:

```python
def _parse_recommendations(self, review_output: str) -> List[Dict[str, Any]]:
    """
    Parse review output into structured recommendations.

    Expected format:
    ### [Title]
    - **Category**: [category]
    - **Priority**: [priority]
    - **Description**: [description]
    """
    import re

    recommendations = []

    # Pattern: ### [Title]
    # - **Category**: [category]
    # - **Priority**: [priority]
    # - **Description**: [description]

    sections = re.split(r'###\s+', review_output)

    for section in sections[1:]:  # Skip first empty split
        lines = section.strip().split('\n')
        if not lines:
            continue

        title = lines[0].strip()

        # Extract fields
        category = None
        priority = None
        description = []

        for line in lines[1:]:
            if match := re.match(r'\s*-\s*\*\*Category\*\*:\s*(.+)', line):
                category = match.group(1).strip()
            elif match := re.match(r'\s*-\s*\*\*Priority\*\*:\s*(.+)', line):
                priority = match.group(1).strip()
            elif match := re.match(r'\s*-\s*\*\*Description\*\*:\s*(.+)', line):
                description.append(match.group(1).strip())
            elif description:  # Continuation of description
                description.append(line.strip())

        if title and category and priority:
            recommendations.append({
                'title': title,
                'category': category,
                'priority': priority,
                'description': '\n'.join(description)
            })

    return recommendations
```

## Integration Points

### 1. Project Monitor Enhancement

Add PR review trigger detection to `services/project_monitor.py`:

```python
async def _handle_card_move(self, project_name: str, card_data: Dict[str, Any]):
    """Enhanced to detect PR review triggers"""

    # ... existing logic ...

    # Check for PR review trigger
    if column_name == "Staged":
        pr_review_task = await self._check_for_pr_review_trigger(
            project_name, issue_number, column_name
        )

        if pr_review_task:
            await self._enqueue_pr_review_task(pr_review_task)
```

### 2. Task Queue Priority

PR review tasks should have HIGH priority (execute before new sub-issue work):

```python
await task_queue.enqueue_task(
    agent_name="pr_review_agent",
    context=pr_review_task,
    priority=TaskPriority.HIGH
)
```

### 3. Agent Registry

Register in `agents/__init__.py`:

```python
from .pr_review_agent import PRReviewAgent

AGENT_REGISTRY = {
    # ... existing agents ...
    "pr_review_agent": PRReviewAgent,
}
```

## Execution Flow

### Happy Path

1. **First child enters Code Review**: Issue #101 (child of #53) moves from Development → Code Review
   - Draft PR is automatically created for parent #53's feature branch (`feature/issue-53-...`)
   - PR remains as draft throughout development
2. **Subsequent children**: Issues #102, #103 complete Development → Code Review
   - They reuse the existing PR (same feature branch)
   - No new PRs created
3. **All children staged**: Issue #103 (last child of #53) moves to "Staged"
   - Project monitor detects all children of #53 are in Staged/Done
4. **PR review triggered**: PR review task queued with HIGH priority
5. **Review execution**:
   - Agent executes `/review` on PR for parent #53
   - Claude analyzes accumulated changes from all children
   - Agent parses and filters recommendations
6. **Issue creation**: 3 filtered recommendations become issues #111, #112, #113
   - Created as sub-issues of parent #53
   - Placed in Development column
7. **State update**: PR review count incremented (1/3)
8. **Natural loop**: When #111-113 complete and reach Staged, trigger PR review again (2/3)

### Edge Cases

**No PR found**:
- Log warning, skip review
- May indicate PR was merged prematurely

**All recommendations filtered**:
- Log info, create no issues
- Post summary comment on parent issue
- Still increment review count

**Iteration limit reached**:
- Log info, skip review
- Post comment: "PR review limit reached (3/3)"
- Rely on human review for remaining issues

**Review execution failure**:
- Retry up to 2 times
- If still failing, log error and skip
- Don't increment review count

**Concurrent reviews** (race condition):
- Use state locking or atomic counter
- Check review count again before creating issues

## Metrics and Observability

Track these metrics for PR review agent:

```python
# Elasticsearch indices
pr_review_metrics = {
    'timestamp': datetime.now().isoformat(),
    'project': project_name,
    'parent_issue': parent_issue,
    'pr_number': pr_number,
    'review_iteration': review_iteration,
    'total_recommendations': len(recommendations),
    'filtered_recommendations': len(filtered),
    'issues_created': len(created_issues),
    'filter_reasons': {
        'infrastructure': count_infrastructure,
        'abstraction': count_abstraction,
        'architecture': count_architecture
    },
    'categories': {
        'bug_fix': count_bug_fix,
        'security': count_security,
        'performance': count_performance,
        'code_quality': count_code_quality
    },
    'execution_time_ms': execution_time
}
```

**Dashboard Queries**:
- Average recommendations per review
- Filter effectiveness (% filtered)
- Review iteration distribution (1st vs 2nd vs 3rd)
- Time to complete all review cycles

## Testing Strategy

### Unit Tests

```python
# tests/unit/test_pr_review_agent.py

def test_filter_removes_infrastructure():
    """Test that infrastructure suggestions are filtered"""
    recommendations = [
        {
            'title': 'Add OpenTelemetry tracing',
            'category': 'infrastructure',
            'description': 'Introduce observability infrastructure...'
        },
        {
            'title': 'Fix null pointer bug',
            'category': 'bug_fix',
            'description': 'Handle null case in validation'
        }
    ]

    filtered = agent._filter_recommendations(recommendations)

    assert len(filtered) == 1
    assert filtered[0]['category'] == 'bug_fix'

def test_filter_removes_abstraction_layers():
    """Test that abstraction layer suggestions are filtered"""
    # ...

def test_recommendation_parsing():
    """Test parsing Claude Code review output"""
    # ...

def test_review_count_increment():
    """Test state management for review iterations"""
    # ...
```

### Integration Tests

```python
# tests/integration/test_pr_review_workflow.py

async def test_complete_pr_review_cycle():
    """Test full PR review cycle from trigger to issue creation"""
    # 1. Setup: Parent issue #53 with 3 child issues
    # 2. Move all children to Staged
    # 3. Verify PR review triggered
    # 4. Mock Claude Code review output
    # 5. Verify filtered issues created
    # 6. Verify review count incremented
    # ...

async def test_iteration_limit_enforcement():
    """Test that PR review stops after 3 iterations"""
    # ...

async def test_no_pr_found_handling():
    """Test graceful handling when PR doesn't exist"""
    # ...
```

## Future Enhancements

### Phase 2 (Future)

1. **Learning from Human Overrides**
   - Track when humans close PR review issues without fixing
   - Adjust filtering patterns based on rejection rate
   - Fine-tune category detection

2. **Severity Scoring**
   - Assign numeric severity scores (0-10)
   - Auto-prioritize high-severity issues
   - Adjust review iteration limit based on severity

3. **Cross-PR Learning**
   - Identify recurring issues across PRs
   - Suggest project-wide improvements
   - Build pattern library of common issues

4. **Review Summaries**
   - Post review summary on parent issue
   - Include metrics (issues found, filtered, created)
   - Link to created sub-issues

5. **Human Override Commands**
   - `/skip-pr-review` to bypass auto-review
   - `/force-pr-review` to trigger out-of-band
   - `/reset-pr-review-count` to allow additional iterations

## Implementation Notes

### PR Creation Bug Fix (2025-11-04)

During the design of this agent, we discovered that PR creation was broken. The code was attempting to create PRs when issues moved to Code Review (in `services/project_monitor.py:2238-2266`), but it was failing with "No branch tracked" errors.

**Root Cause**: The `feature_branch_manager.get_feature_branch_for_issue()` method didn't handle parent/child relationships - it only looked for branches matching the exact issue number, not the parent's branch.

**Fix Applied**: Updated `get_feature_branch_for_issue()` to:
1. Be async and accept `github_integration` parameter
2. Check if issue is a sub-issue using `get_parent_issue()`
3. Return the parent's feature branch if it's a sub-issue

**Files Modified**:
- `services/feature_branch_manager.py:212-243` - Updated method signature and implementation
- `services/git_workflow_manager.py:95-101` - Pass github_integration, await the call
- `services/review_cycle.py:1780` - Pass github_integration, await the call
- `services/feature_branch_manager.py:1168` - Pass github_integration, await the call

This fix ensures PRs are correctly created when the first child enters Code Review, which is a prerequisite for the PR Review Agent to function.

## Open Questions

1. **Review Timing**: Should we review on each sub-issue completion, or only when all complete?
   - **Decision**: Only when all complete (reduces noise)

2. **Filtering Aggressiveness**: Should we err on side of more or fewer filtered suggestions?
   - **Decision**: More aggressive filtering (can always add more categories)

3. **Review Scope**: Review entire PR or only new changes since last review?
   - **Decision**: Entire PR (avoid missing interactions between changes)

4. **Issue Column**: Where should PR review issues be placed?
   - **Decision**: Development column (ready for immediate work)

5. **Parent Issue Updates**: Should we update parent issue with review progress?
   - **Decision**: Yes, post comment with summary after each review

6. **Conflicting Recommendations**: How to handle contradictory suggestions across iterations?
   - **Decision**: Each iteration is independent; humans resolve conflicts

## Success Criteria

The PR Review Agent will be considered successful if:

1. **Quality**: Recommendations are actionable and relevant (>80% kept by humans)
2. **Efficiency**: Reduces human review time by catching obvious issues
3. **Scope Discipline**: <10% of filtered suggestions reinstated by humans
4. **Iteration Effectiveness**: Most PRs require ≤2 review iterations
5. **No False Negatives**: Critical issues are not filtered out

## References

- [Claude Code /review command](https://docs.claude.com/en/docs/claude-code/slash-commands.md)
- [MakerAgent base class](../agents/base_maker_agent.py)
- [Feature branch manager](../services/feature_branch_manager.py)
- [Project monitor](../services/project_monitor.py)
- [Agent registry](../agents/__init__.py)
