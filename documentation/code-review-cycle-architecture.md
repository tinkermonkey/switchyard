# Code review cycle architecture

## Overview

The orchestrator implements two distinct code review mechanisms that operate at different points in the SDLC pipeline:

1. **In-pipeline review cycle** — driven by `CodeReviewerAgent`, embedded in the `sdlc_execution` pipeline's `implementation` stage. This is a synchronous, iterative maker-checker loop that runs before a PR is finalized.
2. **PR-level review** — driven by `PRCodeReviewerAgent`, embedded in the `planning_design` pipeline's `pr_review` stage. This runs after all sub-issues are complete and the epic moves to "In Review" on the board.

These two mechanisms are independent. They run in different pipelines, on different board columns, using different agent classes, and serve different quality gates.

---

## The two reviewer agents

### CodeReviewerAgent

**Location**: `agents/code_reviewer_agent.py`

`CodeReviewerAgent` extends `PipelineStage` directly (not `MakerAgent` or `AnalysisAgent`). It operates inside the `sdlc_execution` review cycle and reviews code committed by `senior_software_engineer` during the `implementation` stage.

This agent's review scope covers:

- Clean code practices (DRY, KISS, YAGNI)
- Naming conventions — flags names containing "Phase X", "Enhanced", "Improved", "Step Y"
- Error handling completeness
- Dead or commented-out code
- Consistency with existing project code style
- Correct reuse of existing libraries and modules
- Unnecessary new capabilities not requested in the requirements

When re-reviewing a revision (iteration > 1), the agent changes its output structure. It must open with a "Previous Issues Status" section that explicitly marks each prior finding as RESOLVED, PARTIALLY RESOLVED, or NOT RESOLVED before listing any new issues. This re-review format is controlled by the `is_rereviewing` flag, which is `True` when `review_cycle.iteration > 1`.

The agent also consults project-specific sub-agents defined in `/workspace/CLAUDE.md` (e.g., a `guardian` agent for boundary violations) before completing its review.

Before building its prompt, `CodeReviewerAgent` calls `ReviewFilterManager.get_agent_filters()` to inject learned filter instructions derived from historical review outcomes. Filters require 75% minimum confidence before injection.

The agent stores its output in `context['markdown_review']`, which the `ReviewCycleExecutor` reads directly from the returned context dict rather than fetching from GitHub to avoid API timing issues.

### PRCodeReviewerAgent

**Location**: `agents/pr_code_reviewer_agent.py`

`PRCodeReviewerAgent` extends `AnalysisAgent` (which extends `MakerAgent`). It runs in the `planning_design` pipeline's `pr_review` stage, triggered when an epic moves to the "In Review" column on the planning board.

This agent delegates its analysis to the `/pr-review-toolkit:review-pr` skill, which coordinates multiple specialized sub-agents via the Task tool. Each sub-agent runs sequentially and blocks until complete before the next is launched.

The output format uses four severity tiers: Critical Issues, High Priority Issues, Medium Priority Issues, Low Priority / Nice-to-Have. Unlike `CodeReviewerAgent`, `PRCodeReviewerAgent` does not drive a revision cycle. Its output is informational — a quality snapshot of the merged PR rather than a gate that blocks pipeline progression.

---

## Review status values

`ReviewParser` (in `services/review_parser.py`) parses reviewer output into a `ReviewResult` containing a `ReviewStatus` enum value:

| Value | Meaning |
|---|---|
| `APPROVED` | No Critical or High Priority items. Advisory items alone do not block. |
| `CHANGES_REQUESTED` | One or more Critical or High Priority items that the maker must address. |
| `BLOCKED` | Issues that the maker cannot resolve alone — security escalation, fundamental requirement conflict, or need for human decision. |
| `UNKNOWN` | The parser could not determine a status from the reviewer output. Treated the same as `BLOCKED` — triggers escalation via `_escalate_blocked()`. |
| `PENDING` | Defined in the enum but not produced by the parser in normal operation. |

The parser first looks for explicit `### Status` declarations. If none are found, it infers status from content patterns. If still unresolved, it infers from the count of blocking and high-severity findings.

A safety net overrides `APPROVED` to `CHANGES_REQUESTED` if the parsed result contains `blocking_count > 0` or `high_severity_count > 0`. This prevents the reviewer's stated status from silently dropping findings.

---

## How the in-pipeline review cycle works

The cycle is orchestrated by `ReviewCycleExecutor` in `services/review_cycle.py`. The entry point is `start_review_cycle()`, called by the project monitor when an issue moves into the "Code Review" column of the `sdlc_execution_workflow` board.

### Configuration

In `config/foundations/workflows.yaml`, the "Code Review" column in `sdlc_execution_workflow` is declared as:

```yaml
- name: "Code Review"
  type: "review"
  stage_mapping: "implementation_review"
  agent: "code_reviewer"
  maker_agent: "senior_software_engineer"
  max_iterations: 5
  auto_advance_on_approval: true
  escalate_on_blocked: true
```

In `config/foundations/pipelines.yaml`, the `implementation` stage in `sdlc_execution` references:

```yaml
review_required: true
reviewer_agent: "code_reviewer"
reviewer_retries: 5
escalation:
  blocking_threshold: 1
  github_pr_required: true
```

### Cycle state

`ReviewCycleExecutor` creates a `ReviewCycleState` object for each active issue. This object tracks:

- `current_iteration` — increments at the top of each loop pass
- `max_iterations` — sourced from the column's `max_iterations` (5 for the "Code Review" column)
- `maker_outputs` and `review_outputs` — full output history keyed by iteration number
- `status` — one of `initialized`, `maker_working`, `reviewer_working`, `awaiting_human_feedback`, `completed`
- `pre_maker_commit` — git HEAD snapshot taken just before each maker run
- `last_approved_commit` — commit hash recorded when the cycle reaches APPROVED

State is persisted to `state/projects/<project>/review_cycles/active_cycles.yaml` after every status transition. On orchestrator restart, `resume_active_cycles()` reloads this state and resumes in-progress cycles.

### Iteration loop

`_execute_review_loop()` runs the following sequence on each pass:

1. Increment `current_iteration`.
2. Fetch fresh context (discussion content for the `discussions` workspace, or a change manifest for the `issues` workspace).
3. Build `review_task_context` via `_create_review_task_context()` and execute `CodeReviewerAgent` directly via `_execute_agent_directly()`.
4. Read `markdown_review` from the returned context dict. Fall back to fetching the latest GitHub comment only if the field is absent.
5. Store the review output in `cycle_state.review_outputs`.
6. Parse the review using `ReviewParser.parse_review()`.
7. Branch on the parsed status:
   - `APPROVED`: post a cycle summary, record `last_approved_commit`, mark state `completed`, and return the next column name.
   - `CHANGES_REQUESTED`: if `current_iteration >= max_iterations`, escalate. Otherwise, proceed to step 8.
   - `BLOCKED`: if `escalate_on_blocked` is true and `iteration > 1`, escalate and pause the cycle. On the first iteration with blocking issues, the maker is given a chance to fix them before escalation occurs.
8. Snapshot `pre_maker_commit` (the current git HEAD) into cycle state.
9. Build `maker_task_context` via `_create_maker_revision_task_context()` with `trigger = 'review_cycle_revision'` and execute the maker agent.
10. Auto-commit any file changes made by the maker (if `makes_code_changes` is true in the agent config).
11. Store the maker's revised output in `cycle_state.maker_outputs`.
12. Return to step 1.

---

## How revision mode differs from initial mode

Mode detection happens in `MakerAgent._determine_execution_mode()`. The method checks `task_context.get('trigger')`:

- `trigger == 'review_cycle_revision'` → revision mode
- `trigger == 'feedback_loop'` with `conversation_mode == 'threaded'` and non-empty `thread_history` → question mode
- anything else → initial mode

In revision mode, `_build_revision_prompt()` constructs a prompt that includes:

- The agent's previous output (sourced from `revision.previous_output`, which contains the maker's last iteration output, not the original)
- The original output from iteration 0 (for comparison)
- The reviewer's feedback text (from `revision.feedback`)
- A review cycle header: "Review Cycle - Revision N of M"

The prompt instructs the maker to open its response with a `## Revision Notes` checklist that maps each piece of feedback to a specific change made. This structure gives the reviewer explicit confirmation that each issue was addressed.

In initial mode, the maker receives only the issue title, body, labels, and any previous stage output (e.g., architecture from `software_architect`). There is no revision checklist and no feedback to address.

The key behavioral difference: a revision must not start from scratch, must not skip any feedback point, and must not modify sections that were not cited in the review. Initial mode has no such constraints.

---

## Cycle limit and escalation

The `max_iterations` for the "Code Review" column is 5 (configured in `sdlc_execution_workflow`). The counter increments at the top of `_execute_review_loop()` before the reviewer runs, so the cycle runs at most `max_iterations` reviewer passes.

Two escalation paths exist:

**`_escalate_max_iterations()`** is called when `CHANGES_REQUESTED` is returned and `current_iteration >= max_iterations`. It posts a GitHub comment describing all unresolved findings and the current iteration count, sets `cycle_state.status = 'awaiting_human_feedback'`, and returns `CHANGES_REQUESTED, column.name` — leaving the issue in the "Code Review" column.

**`_escalate_blocked()`** is called when the reviewer returns `BLOCKED` and `iteration > 1` (with `escalate_on_blocked = true` on the column). On the first iteration with blocking findings, the maker is given one attempt to resolve them. If blocking issues persist on the second or later iteration, this path is taken. It posts a GitHub comment identifying the blocking findings, pauses the cycle in `awaiting_human_feedback` status, and returns `BLOCKED, column.name`.

In both cases, the cycle is not removed from `active_cycles`. `ProjectMonitor.check_for_feedback_in_discussion()` polls for human comments on issues in `awaiting_human_feedback` state within the monitoring loop. When feedback is detected, `resume_review_cycle_with_feedback()` re-invokes the reviewer with `post_human_feedback = True` in the task context. The reviewer's prompt then includes instructions to read the human's feedback, update its assessment, and post a revised review.

If the reviewer returns `BLOCKED` again after human feedback, the cycle is marked `completed` and removed from active state — manual intervention is required.

---

## Change manifest and scoped diffs

For the `issues` workspace (used by `sdlc_execution`), the reviewer does not receive the maker's prose output as context. Instead, it receives a change manifest built by `_get_change_manifest()`:

```
git diff --stat <base_commit> HEAD
git log --oneline <base_commit>..HEAD
```

The base commit is `cycle_state.pre_maker_commit` — the HEAD snapshot taken immediately before the maker ran. If that snapshot is absent, `HEAD~1` is used as a fallback. If neither produces a diff, `_execute_review_loop()` raises a `RuntimeError` and aborts the review cycle.

The manifest tells the reviewer which files changed and provides the exact git commands to fetch per-file diffs. This scopes the review to only the changes made in the current revision, preventing the reviewer from re-raising issues from previously approved iterations.

---

## Pipeline stage sequence

The `sdlc_execution` pipeline runs these stages in order:

1. `implementation` (stage) — `senior_software_engineer` writes code
2. `testing` (stage, `stage_type: repair_cycle`) — `senior_software_engineer` runs tests and fixes failures
3. `staging` (stage) — `senior_software_engineer` prepares for staging

The code review cycle is not a named stage in `pipelines.yaml`. It is embedded within the `implementation` stage via `review_required: true` and `reviewer_agent: code_reviewer`. The board column "Code Review" in `sdlc_execution_workflow` maps to `stage_mapping: implementation_review`, which is the column that holds the issue during the review cycle. The issue moves to "Testing" only after `APPROVED` is returned and `auto_advance_on_approval` triggers column progression.

The review cycle approval does not update PR status. PR status is managed by `FeatureBranchManager.finalize_workspace()`, which checks whether all sub-issues under an epic are complete before marking the PR ready for merge.

---

## Relationship between in-pipeline review and PR-level review

The two review mechanisms address different quality concerns at different granularities:

| Dimension | In-pipeline review cycle | PR-level review |
|---|---|---|
| Agent | `CodeReviewerAgent` | `PRCodeReviewerAgent` |
| Pipeline | `sdlc_execution` | `planning_design` |
| Board column | "Code Review" | "In Review" |
| Trigger | Issue moved to "Code Review" | All sub-issues complete; epic moves to "In Review" |
| Scope | Code changes in a single sub-issue | Entire PR across all sub-issues |
| Drives revision | Yes — revision cycle with `senior_software_engineer` | No — informational output only |
| Output sink | `context['markdown_review']` posted as issue comment | `context['markdown_analysis']` posted as discussion/issue comment |
| Stage type | Embedded in `implementation` stage | Standalone `pr_review` stage with `stage_type: pr_review` |

An individual sub-issue must pass the in-pipeline `CodeReviewerAgent` cycle before its code is considered accepted. The `PRCodeReviewerAgent` then performs a holistic review of the aggregated PR after all sub-issues have individually passed their review cycles. The PR-level review uses the `/pr-review-toolkit:review-pr` skill, which coordinates multiple specialized review agents rather than running a single comprehensive analysis.
