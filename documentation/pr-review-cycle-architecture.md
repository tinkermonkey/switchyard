# PR review cycle architecture

## Overview

The PR review cycle is the quality gate that sits between the SDLC execution pipeline and the documentation stage. It runs after all sub-issues for a parent epic have been completed and a feature branch PR has been opened. Its purpose is to verify that the code in the PR is correct, complete against the original requirements, and free of CI failures before the parent issue advances to Documentation.

The cycle is automated and repeating: if the review finds problems, it creates new sub-issues, returns the parent to development, and runs again once those sub-issues are resolved. The cycle repeats up to `MAX_REVIEW_CYCLES` times (currently `3`, defined in `pipeline/pr_review_stage.py`).

---

## Where the PR review cycle fits in the pipeline

The PR review stage is defined in `config/foundations/pipelines.yaml` under the `planning_design` template as the final stage before the pipeline exits:

```yaml
- stage: "pr_review"
  name: "PR Review & Requirements Verification"
  stage_type: "pr_review"
  default_agent: "pr_code_reviewer"
  inputs_from: ["work_breakdown"]
  retries: 1
  review_required: false
```

The `stage_type: "pr_review"` field causes `create_stage_from_config()` in `agents/orchestrator_integration.py` to instantiate `PRReviewStage` instead of a standard `AgentStage`.

### Trigger sequence

1. A sub-issue in the SDLC execution pipeline completes (reaches a terminal column such as Staged or Done).
2. `ProjectMonitor._check_pr_ready_on_issue_exit()` evaluates whether all sub-issues for the parent are complete.
3. When all sub-issues are complete, `_advance_parent_for_pr_review()` is called. If the parent is in "In Development", it moves the parent to "In Review" on the Planning board. The normal polling loop then detects the column change and calls `_start_pr_review_for_issue()`.
4. If the parent is already in "In Review" (edge case or race condition), the PR review agent is directly enqueued.

`PRReviewStage.execute()` begins by calling `_find_pr_url()`, which uses `gh pr list` to find an open PR whose branch name starts with `feature/issue-{parent_issue_number}-`. If no PR is found, execution raises `NonRetryableAgentError`.

---

## What PRReviewStage does

`PRReviewStage` (in `pipeline/pr_review_stage.py`) runs in the orchestrator process, not in Docker. It orchestrates up to four sequential phases by launching Docker containers via `AgentExecutor`.

### Phase 1: PR code review

`PRReviewStage` builds a prompt via `_build_pr_review_prompt()` and invokes `PRCodeReviewerAgent` (`pr_code_reviewer`) inside a Docker container with the project code mounted. The prompt instructs the agent to run `/pr-review-toolkit:review-pr all`, which coordinates specialized sub-agents (code-reviewer, test-analyzer, silent-failure-hunter, comment-analyzer, type-design-analyzer). The required output format is a structured markdown document with sections: "Critical Issues", "High Priority Issues", "Medium Priority Issues", "Low Priority / Nice-to-Have".

On cycle 2 and later, `_build_prior_cycle_context()` is called to fetch titles and states of issues created in prior review cycles from GitHub and inject them into the prompt. The reviewer is instructed to tag each finding as NEW or REGRESSION and to not re-report already-fixed issues.

The phase 1 output is collected into `phase_outputs` for consolidation. Issues are not created directly from this output.

### Phase 2: Context verification (up to three sub-phases)

`PRReviewStage` loads three context sources and runs `RequirementsVerifierAgent` (`requirements_verifier`) against each in sequence:

| Sub-phase | Source | Authority key |
|---|---|---|
| 2.1 | Parent issue body (`gh issue view`) | `parent_issue` |
| 2.2 | Business analyst output from GitHub Discussion | `business_analyst` |
| 2.3 | Software architect output from GitHub Discussion | `software_architect` |

Discussion content is retrieved via GraphQL in `_load_discussion_outputs()`, which identifies agent outputs by the signature string `_Processed by the {key} agent_` in comment bodies.

Each verification prompt (`_build_verification_prompt()`) carries an authority framing that governs how strictly the verifier should flag gaps:

- `parent_issue`: acceptance criteria — every explicit requirement must be implemented.
- `business_analyst`: functional requirements — flag explicitly required items, skip nice-to-haves.
- `software_architect`: committed technical specifications — flag all gaps and deviations.

Sub-phases that have no content (e.g., no linked discussion) are skipped. Each verifier result is appended to `phase_outputs`.

Context is truncated to 15,000 characters before being passed to the verifier agent.

### Phase 3: CI status check

This phase runs locally in the orchestrator process using `gh pr checks`. It does not launch a Docker container. `_check_ci_status()` parses the JSON output and categorizes checks into `failures` (bucket `fail`) and `pending` (bucket `pending`). If failures exist, a GitHub issue is created immediately (not deferred to consolidation) and counted toward `review_found_issues`.

### Phase 4: Consolidation

All outputs collected in `phase_outputs` (phases 1 and 2) are passed to `_run_consolidation_phase()`, which invokes `PRCodeReviewerAgent` a second time with a consolidation prompt. The consolidation agent is instructed to deduplicate findings across phases, filter out non-actionable findings (style preferences, future enhancements, aspirational suggestions), and produce a JSON document with this structure:

```json
{
  "groups": [
    {
      "name": "Functional Area Name",
      "severity": "critical|high|medium|low",
      "findings": "- **Finding Title**: description with file:line ref<br>..."
    }
  ],
  "filtered_out": ["one-line note about each removed finding"]
}
```

`_parse_consolidated_findings()` converts each group into one GitHub issue spec (one issue per functional area, not one per finding).

---

## Detecting actionable findings

Before creating an issue, `_is_actionable_section()` applies two checks to each section's text:

1. `_has_actionable_findings()`: looks for structured bullet patterns matching `- **Title**: desc` or `1. **Title**: desc`.
2. `_is_none_found()`: matches phrases such as "None found", "N/A", "No issues found", "All requirements verified". If this matches and no structured findings are present, the section is treated as a clean pass.

This two-test approach prevents false positives when agents write explanatory text after "None found" (for example, "None found - issues were already resolved").

---

## How review issues are created and routed

When actionable findings exist, `_create_review_issues()` runs for each issue spec:

1. Creates the GitHub issue via `gh issue create`.
2. Adds the issue to the SDLC Execution board via `gh project item-add`.
3. Sets the issue status to Backlog via a GraphQL `updateProjectV2ItemFieldValue` mutation.
4. Links the issue as a sub-issue of the parent via a GraphQL `addSubIssue` mutation.

After creation, all issues are moved from Backlog to the Development column via `_move_issues_to_development()`.

If more than one issue is created in a single review cycle, `_create_review_issues()` performs a second pass to add a "Concurrent Fix Issues (Same Review Cycle)" section to each issue body. This section lists sibling issues and warns the implementing agent to check those issues for file-level conflicts before making changes.

Issue titles follow the convention `[PR Feedback] {description}` so they can be recognized as review-cycle artifacts.

---

## Post-review decision

After all phases complete, `PRReviewStage` makes one of three decisions:

| Outcome | Condition | Action |
|---|---|---|
| Clean pass | All phases produced no actionable findings | Calls `_advance_parent_to_documentation()`: moves parent to Documentation column via `PipelineProgression.move_issue_to_column()` with trigger `pr_review_clean_pass` |
| Issues found | At least one actionable finding was created as an issue | Calls `_return_parent_to_development()`: moves parent back to "In Development" with trigger `pr_review_issues_found` |
| Inconclusive | All phases failed, or some phases failed and no issues were found | Records the cycle but takes no board action; parent remains in current column |

In all three cases, `pr_review_state_manager.increment_review_count()` is called to record the completed cycle.

`context['manual_progression_made'] = True` is set for clean pass and issues-found outcomes, preventing the `SequentialPipeline` from applying its own auto-advancement logic.

---

## Review cycle state tracking

`PRReviewStateManager` (in `state_management/pr_review_state_manager.py`) persists review state to `state/projects/{project_name}/pr_review_state.yaml`. A global singleton instance `pr_review_state_manager` is imported throughout the codebase.

The YAML file has this structure per parent issue number:

```yaml
pr_reviews:
  42:
    review_count: 2
    last_review_at: "2025-10-10T12:00:00Z"
    iterations:
      - iteration: 1
        issues_created: [101, 102]
        timestamp: "2025-10-10T11:00:00Z"
      - iteration: 2
        issues_created: []
        timestamp: "2025-10-10T12:00:00Z"
    cycle_limit_notified: false
```

Key methods:

| Method | Purpose |
|---|---|
| `get_review_count(project, issue)` | Returns current cycle count; used by `PRReviewStage` to gate execution |
| `increment_review_count(project, issue, created_issues)` | Records a completed cycle and the issue numbers created |
| `reset_review_count(project, issue)` | Resets count to 0; preserves iteration history for auditing |
| `get_review_history(project, issue)` | Returns full iteration list; used by `_build_prior_cycle_context()` |
| `get_last_review_timestamp(project, issue)` | Returns ISO timestamp of last review; used by `_advance_parent_for_pr_review()` for the 180-second cooldown guard |
| `is_cycle_limit_notified(project, issue)` | Prevents duplicate cycle-limit notifications |
| `mark_cycle_limit_notified(project, issue)` | Records that the limit notification has been posted |

State is never auto-deleted. It is reset only explicitly: `reset_review_count()` is called when a human moves the parent issue to Backlog (detected in `ProjectMonitor`), which is the supported mechanism for restarting the review cycle after the limit is reached.

---

## Cycle limits

`MAX_REVIEW_CYCLES = 3` is defined as a module-level constant in `pipeline/pr_review_stage.py`. It is imported directly into `project_monitor.py` wherever the limit must be checked outside the stage itself.

### Enforcement points

**Inside `PRReviewStage.execute()`**: At the start of each execution, `get_review_count()` is compared to `MAX_REVIEW_CYCLES`. If `review_count >= MAX_REVIEW_CYCLES`, a `NonRetryableAgentError` is raised immediately, preventing any agent containers from launching.

**Inside `_advance_parent_for_pr_review()`**: Before moving the parent to "In Review", the monitor checks `review_count >= MAX_REVIEW_CYCLES`. If true:
- If `is_cycle_limit_notified()` returns `False`, a comment is posted on the parent issue via `GitHubIntegration.post_comment()`, explaining that the limit has been reached and further review must be manual.
- `mark_cycle_limit_notified()` is called so the comment is only posted once.
- The parent is not moved to "In Review".

**At cycle boundary (issues found on final cycle)**: When `review_found_issues` is true and `current_cycle >= MAX_REVIEW_CYCLES`, `_build_cycle_limit_comment()` constructs a summary comment listing the issues that were created in the final cycle, and `_post_comment_on_issue()` posts it to the parent issue. This comment states that no further automated reviews will run and instructs the developer to manually move the parent to "In Review" to reset the count.

### Resetting the limit

Moving the parent issue to the Backlog column causes `ProjectMonitor` to call `pr_review_state_manager.reset_review_count()`. The `review_count` field is set to 0; the `iterations` history is preserved. The `cycle_limit_notified` and `cycle_limit_notified_at` fields are also removed so the notification can be sent again if the limit is reached in the new run.

---

## The approval path

There is no explicit "approval" verdict produced by an agent. Approval is defined structurally: the review cycle produces a clean pass when all of the following are true:

- Phase 1 (PR code review): no actionable findings survive the `_is_actionable_section()` check.
- Phase 2 (context verification): no actionable gaps or deviations found across all three sub-phases.
- Phase 3 (CI status): `_check_ci_status()` returns no failures. Pending checks alone do not block a clean pass — they log a warning but `review_found_issues` is only set to `True` for failures.
- Phase 4 (consolidation): `_parse_consolidated_findings()` returns an empty `groups` list.

On a clean pass, `PRReviewStage` calls `_advance_parent_to_documentation()`, which uses `PipelineProgression.move_issue_to_column()` to move the parent issue from "In Review" to "Documentation" on the Planning board with trigger `pr_review_clean_pass`. No PR merge or GitHub PR approval is performed by the stage; it only advances the board state.

---

## Relationship to GitHub PR comments

The PR review agents operate on the PR diff, not on PR review comments. `PRCodeReviewerAgent` and `RequirementsVerifierAgent` both receive the PR URL, check out the branch via `gh pr checkout {pr_number}` (in the Phase 1 prompt), and read the code directly.

Review findings are not posted as PR comments. They are posted as GitHub issues linked as sub-issues to the parent. Each issue body includes the source phase, specific file and line references where available, and a "Created by PR Review Stage" footer.

The PR itself receives comments only in two circumstances:
- `_post_comment_on_issue()` is called on the parent issue (not the PR) when the final cycle limit is reached, posting a summary via `gh issue comment`.
- Cycle-limit notifications are posted to the parent issue via `GitHubIntegration.post_comment()`.

The `PRCodeReviewerAgent` (`pr_code_reviewer`) uses the `pr-review-toolkit` skill, which in turn launches specialized review sub-agents. Those sub-agents analyze the PR diff and return findings to the coordinator; they do not post PR review comments themselves. All feedback surfaces through GitHub issues.

---

## Maker-checker pattern

The PR review cycle applies the maker-checker pattern at the epic level, not the sub-issue level. The sub-issue pipeline has its own maker-checker cycle (in `config/foundations/pipelines.yaml`, the `implementation` stage has `review_required: true` with `reviewer_agent: "code_reviewer"`). That cycle uses `CodeReviewerAgent` and operates on individual sub-issues during active development.

The PR review cycle is a second, coarser checker that runs after all sub-issues have been completed:

| Role | Agent | Scope |
|---|---|---|
| Maker | `SeniorSoftwareEngineerAgent` (`senior_software_engineer`) | Implements sub-issues; makes code changes |
| Checker (sub-issue level) | `CodeReviewerAgent` (`code_reviewer`) | Reviews individual sub-issue implementation; produces APPROVED / CHANGES NEEDED / BLOCKED verdict |
| Checker (PR level, Phase 1) | `PRCodeReviewerAgent` (`pr_code_reviewer`) | Reviews the full PR for code quality issues |
| Checker (PR level, Phase 2) | `RequirementsVerifierAgent` (`requirements_verifier`) | Verifies implementation completeness against original requirements and design |

`PRCodeReviewerAgent` and `RequirementsVerifierAgent` extend `AnalysisAgent` (via `base_analysis_agent.py`), which extends `MakerAgent`. Both are analysis-only agents: `makes_code_changes: false` and `filesystem_write_allowed: false` in `config/foundations/agents.yaml`. They do not write to the repository.

`PRReviewStage` itself is the orchestrator for the checker phase. It runs in the orchestrator process (not Docker) and is analogous to `RepairCycleStage` in architecture: it coordinates multiple agent invocations without being a Docker-based agent itself. The `max_agent_calls` circuit breaker (default 20) prevents runaway cost in degenerate cases.

---

## Circuit breaker

`PRReviewStage` maintains `self._agent_call_count` and `self.max_agent_calls = 20`. Before launching each Docker container, the count is checked against the maximum. If the circuit breaker triggers, execution returns immediately with a failure message in `context['markdown_analysis']`. The count is reset to 0 at the start of each call to `PRReviewStage.execute()`, so the limit applies per review cycle, not per parent issue lifetime.
