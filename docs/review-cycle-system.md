# Review Cycle System

## Overview

The Review Cycle System implements an automated maker-checker pattern for agent workflows. When a GitHub issue card moves to a review column, the system automatically executes an iterative loop between a maker agent (who created the work) and a reviewer agent (who validates it).

## How It Works

### 1. Column Configuration

Review columns are defined in `config/foundations/workflows.yaml` with the following attributes:

```yaml
- name: "Review"
  type: "review"                      # Marks this as a review column
  agent: "requirements_reviewer"      # The reviewer agent
  maker_agent: "business_analyst"     # The maker agent to send feedback to
  max_iterations: 3                   # Maximum revision cycles
  auto_advance_on_approval: true      # Auto-move card when approved
  escalate_on_blocked: true           # Escalate blocking issues immediately
```

### 2. Automatic Cycle Initiation

When a card moves to a review column, the `ProjectMonitor` detects the column type and starts a review cycle instead of a single agent task:

1. Posts an initial "Starting Review Cycle" comment to GitHub
2. Initiates the `ReviewCycleExecutor` with the column configuration
3. The cycle runs synchronously, blocking the card until complete

### 3. The Iteration Loop

Each iteration consists of:

#### Step 1: Reviewer Evaluates Work
- Reviewer agent receives maker's output
- Includes iteration context in prompt (iteration number, max iterations)
- Reviews and provides structured feedback
- Posts review as GitHub comment
- Returns status: `APPROVED`, `CHANGES_REQUESTED`, or `BLOCKED`

#### Step 2: Parse Review Status
- `ReviewParser` analyzes reviewer's comment
- Extracts status, findings, and severity levels
- Counts blocking and high-severity issues

#### Step 3: Decision Logic

**If APPROVED:**
- Post cycle summary comment
- If `auto_advance_on_approval`, move to next column
- Exit cycle successfully

**If BLOCKED:**
- If `escalate_on_blocked`, escalate immediately:
  - Add `needs-human-review` label
  - Post escalation comment with blocking issues
  - Exit cycle, stay in review column
- Otherwise, treat as `CHANGES_REQUESTED`

**If CHANGES_REQUESTED:**
- Check if max iterations reached:
  - If yes: Escalate with max iterations comment
  - If no: Continue to Step 4

#### Step 4: Maker Revises Work
- Create task for maker agent with feedback context
- Include review cycle metadata:
  - `iteration`: Current iteration number
  - `max_iterations`: Maximum allowed
  - `reviewer_agent`: Who provided feedback
  - `is_revision`: True
  - `review_feedback`: Full reviewer comment
- Maker agent receives special prompt indicating revision mode
- Maker posts updated analysis to GitHub
- Loop back to Step 1 (re-review)

### 4. Escalation Scenarios

#### Blocking Issues Found
```
Iteration: 1/3
Status: BLOCKED
Action: Immediate escalation
Label: needs-human-review
Card: Stays in review column
```

#### Max Iterations Exceeded
```
Iteration: 3/3
Status: CHANGES_REQUESTED (still has issues)
Action: Escalation with max iterations warning
Label: needs-human-review
Card: Stays in review column
```

## User Experience

### GitHub Comment Thread

```
Issue #93: Schema.org local implementation improvements

┌─ Business Analyst Agent (Original Work)
│  ## Requirements Analysis
│  [Full analysis...]
│
└─ 🔄 Starting Review Cycle
   Reviewer: Requirements Reviewer
   Maker: Business Analyst
   Max Iterations: 3

   ┌─ Requirements Reviewer Agent (Iteration 1)
   │  ## Review Results
   │  ### Critical Issues
   │  - Missing cost estimates
   │  **Status**: CHANGES_REQUESTED
   │
   ├─ Business Analyst Agent (Revision 1)
   │  ## Requirements Analysis (Revision 1)
   │  ### Changes Made in This Revision
   │  - Added cost estimates
   │  [Updated analysis...]
   │
   ├─ Requirements Reviewer Agent (Iteration 2)
   │  ## Review Results
   │  All issues addressed.
   │  **Status**: APPROVED
   │
   └─ 🔄 Review Cycle Complete
      Status: APPROVED
      Iterations: 2/3

      Review approved after 2 iteration(s)

[Card automatically moves to next column]
```

### Escalation Example

```
   ├─ Requirements Reviewer Agent (Iteration 3)
   │  ## Review Results
   │  ### Critical Issues
   │  - Security concerns still not addressed
   │  **Status**: CHANGES_REQUESTED
   │
   └─ ⚠️ Max Review Iterations Reached

      The automated review cycle has reached the maximum
      iterations (3) without approval.

      Remaining Issues: 2
      High Severity: 1

      Human review required.
```

## Agent Prompt Enhancements

### Maker Agents (Revision Mode)

When a maker receives feedback in a review cycle, they see:

```
## Review Cycle Context

This is Revision 2 of 3 in an automated review cycle.

- Reviewer: Requirements Reviewer
- Iteration: 2/3
- Mode: Automated maker-checker revision

The reviewer has provided specific feedback that must be addressed.
If you address all feedback points, the reviewer will approve and
the work will proceed. If blocking issues remain after 3 iterations,
the work will be escalated for human review.

## Your Previous Analysis
[...]

## Feedback to Address
[Reviewer's specific feedback]

## Task
Update your analysis to incorporate the feedback above.

Include a "Changes Made in This Revision" section at the top.
```

### Reviewer Agents (Re-review Mode)

When a reviewer sees revised work:

```
## Review Cycle Context

This is Review Iteration 2 of 3 in an automated maker-checker cycle.

- Maker Agent: Business Analyst
- Iteration: 2/3
- Mode: Re-reviewing revised work

The maker has addressed your previous feedback. Your task is to:
1. Verify that all issues have been adequately addressed
2. Identify any new issues or improvements needed
3. Decide whether to APPROVE or request further CHANGES

Status Guidelines:
- APPROVED: All critical and high-priority issues resolved
- CHANGES_REQUESTED: Non-blocking issues remain
- BLOCKED: Critical issues persist or new critical issues found
```

## Review Status Parsing

The `ReviewParser` extracts structured feedback from reviewer comments using:

### Status Detection Patterns
- `Status: APPROVED` or `✅ Approved`
- `Status: BLOCKED` or `🚫 Blocked` or `Blocking issues found`
- `Status: CHANGES_REQUESTED` or `🔄 Changes requested`

### Severity Markers
- **Blocking**: `blocking`, `critical`, `must fix`, `blocker`
- **High**: `high`, `major`, `important`, `should fix`
- **Medium**: `medium`, `moderate`, `consider`
- **Low**: `low`, `minor`, `nice to have`, `nitpick`

### Findings Extraction
- Looks for "Issues", "Findings", "Problems" sections
- Parses bullet points with category and message
- Extracts suggestions marked with 💡 or "Suggestion:"

## Configuration Reference

### Workflow Column Schema

```yaml
columns:
  - name: string                      # Column name
    type: "review" | "maker" | null   # Column type
    agent: string                     # Primary agent for this column
    maker_agent: string               # (review only) Maker to send feedback to
    max_iterations: number            # (review only) Max revision cycles (default: 3)
    auto_advance_on_approval: boolean # (review only) Auto-move when approved (default: true)
    escalate_on_blocked: boolean      # (review only) Escalate blocking issues (default: true)
```

### Review Cycle State

The system tracks active review cycles with:

```python
class ReviewCycleState:
    issue_number: int
    repository: str
    maker_agent: str
    reviewer_agent: str
    max_iterations: int
    current_iteration: int
    maker_outputs: List[Dict]         # History of maker revisions
    review_outputs: List[Dict]        # History of review feedback
```

## Implementation Files

- **`config/foundations/workflows.yaml`**: Column type definitions
- **`config/manager.py`**: WorkflowColumn dataclass with review fields
- **`services/review_parser.py`**: Status and findings extraction
- **`services/review_cycle.py`**: Main iteration loop executor
- **`services/project_monitor.py`**: Integration point, detects review columns
- **`agents/conversational_mixin.py`**: Maker revision prompts
- **`agents/*_reviewer_agent.py`**: Reviewer agents with iteration context

## Benefits

1. **Automated Quality Assurance**: Reviewer agents ensure quality before progression
2. **Iterative Refinement**: Maker agents improve work based on specific feedback
3. **Clear Escalation Path**: Human intervention only when needed
4. **Transparent Process**: All iterations visible in GitHub thread
5. **Bounded Execution**: Max iterations prevent infinite loops
6. **Consistent Pattern**: Same maker-checker logic across all review stages

## Extensibility

The review cycle system can be extended with:

- **Custom Parsers**: Different review formats for different agent types
- **Multi-Reviewer Patterns**: Parallel reviews from multiple agents
- **Quality Metrics**: Track approval rates, iteration counts, common issues
- **Learning Signals**: Use review patterns to improve agent prompts
- **Conditional Logic**: Different max iterations based on change scope
