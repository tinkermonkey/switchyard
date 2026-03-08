# Cycle Dashboard Inputs: Design Guide

This document is the design reference for collapsed-state mini-dashboard views for all 13 cycle container node types in the pipeline flowchart UI.

## How to Use This Guide

Each section below documents one cycle type with:
1. **Available Data** — fields already extracted by `cycleSummaries.js`
2. **Claude Live Log Data** — additional data extractable from `tool_call` / `text_output` / `tool_result` events within the cycle
3. **User Questions & Answers** — what the user wants to know at a glance
4. **Collapsed Dashboard Design Direction** — guidance for the collapsed summary card

All renderer functions live in `cycleCollapsedSummaries.jsx`. All summary extractors live in `cycleSummaries.js`.

## Nesting Hierarchy

```
Level 1                       Level 2                       Level 3
─────────────────────         ─────────────────────         ─────────────────────
review_cycle              →   review_iteration
repair_cycle              →   repair_test_cycle         →   test_execution
                                                         →   fix_cycle
                                                         →   warning_review
                                                         →   systemic_analysis
                                                         →   systemic_fix
pr_review_cycle           →   pr_review_phase
conversational_loop
status_progression
```

---

## Level 1 Cycles

---

## review_cycle

**Level:** 1 | **Parent:** none

### Available Data

| Field | Source event | Description | Example |
|---|---|---|---|
| `status` | `review_cycle_completed` `.status` | approved / rejected / escalated / running | `"approved"` |
| `makerAgent` | `review_cycle_started` `.inputs.maker_agent` | Agent that produces work | `"senior_software_engineer"` |
| `reviewerAgent` | `review_cycle_started` `.inputs.reviewer_agent` | Agent that reviews work | `"code_reviewer"` |
| `totalIterations` | `review_cycle_completed` `.total_iterations` | Number of iterations completed | `2` |
| `maxIterations` | `review_cycle_started` `.max_iterations` | Maximum allowed iterations | `5` |
| `durationSeconds` | Computed from start/end timestamps | Total wall-clock duration | `167` |
| `iterations[].number` | Per-iteration index | Iteration sequence number | `1` |
| `iterations[].durationSeconds` | Computed per-iteration duration | Wall time for each iteration | `78` |
| `completionReason` | `review_cycle_completed` `.reason` | Human-readable completion message | `"Review cycle completed successfully: approved after 2 iteration(s)"` |

**Extractor note:** `extractReviewCycleSummary()` looks for `endEvent.outcome ?? endEvent.decision?.outcome` to determine status, but the real event provides `status` at the top level. The extractor falls through to `else status = 'approved'` for any non-null end event, which works for approved cycles but would fail to detect rejected/escalated. This is a known bug — the extractor should read `endEvent.status` directly.

### Claude Live Log Data

Within a review cycle, agents write live logs captured via the Claude streaming API. These are keyed to the pipeline run and agent container in Elasticsearch (`claude-live-logs-*` index):

| Data | Log event type | How to extract |
|---|---|---|
| Token usage per turn | `token_usage` events (skipped from flowchart, present in logs) | Sum `input_tokens` + `output_tokens` per agent turn |
| Files read by maker | `tool_call` events with `tool_name: "Read"` | Extract `file_path` param |
| Files edited by maker | `tool_call` events with `tool_name: "Edit"` or `"Write"` | Extract `file_path` param |
| Reviewer feedback text | `text_output` events from reviewer agent turn | Full text of reviewer critique |
| Number of tool uses | Count of `tool_call` events per agent turn | Group by `agent_name` |
| Maker reasoning | `text_output` events from maker agent turn | Summary of what changes were made |

### User Questions & Answers

- Was the review approved or rejected? → `status`
- How many iterations did it take before approval? → `totalIterations` / `maxIterations`
- Who was the maker and who was the reviewer? → `makerAgent`, `reviewerAgent`
- How long did the whole review take? → `durationSeconds`
- Did later iterations run faster or slower? → per-iteration `durationSeconds` array (sparkline)
- Why did it escalate? → `completionReason`
- Which files did the maker change each iteration? → Claude live log `tool_call` Edit/Write params

### Collapsed Dashboard Design Direction

**Currently rendered:** status indicator + status label + duration, maker/reviewer agent names, iteration timeline bars.

**Primary metric:** `status` (approved / rejected / escalated / running)

**Secondary info:**
- Maker → Reviewer agent pair
- Iteration count: `N of M iterations` where M is `maxIterations`
- Iteration timeline: horizontal mini bar-chart, one bar per iteration, proportional to duration

**Enhancement opportunities:**
- Add `completionReason` as a tooltip or small secondary line when escalated
- Add total file change count from Claude live logs (e.g., "3 files changed") as a tertiary line
- Show whether maker changed files on final iteration (no-op vs active fix)

**Suggested layout:**
```
● APPROVED                              2m 22s
Maker     Senior Software Engineer
Reviewer  Code Reviewer
─────────────────────────────────────
Iteration timeline
1 ████████████████         1m 18s
2 █████                      44s
```

---

## repair_cycle

**Level:** 1 | **Parent:** none

### Available Data

| Field | Source event | Description | Example |
|---|---|---|---|
| `status` | `repair_cycle_completed` `.overall_success` or `agent_failed` | success / failed / running | `"success"` |
| `durationSeconds` | `repair_cycle_completed` `.duration_seconds` | Total repair duration | `312` |
| `totalAgentCalls` | `repair_cycle_completed` `.total_agent_calls` | Number of agent invocations | `7` |
| `envRebuildTriggered` | presence of `repair_cycle_env_rebuild_started` event | Whether env was rebuilt | `true` |
| `testCycleRows[].testType` | `repair_cycle_test_cycle_completed` `.test_type` | Test suite name | `"unit"` |
| `testCycleRows[].passed` | `repair_cycle_test_cycle_completed` `.passed` | Whether test type passed | `true` |
| `testCycleRows[].filesFixed` | `repair_cycle_test_cycle_completed` `.files_fixed` | Files repaired for this test type | `3` |
| `testCycleRows[].iterations` | `repair_cycle_test_cycle_completed` `.test_cycle_iterations` | Fix iterations for this test type | `2` |
| `testCycleRows[].durationSeconds` | `repair_cycle_test_cycle_completed` `.duration_seconds` | Duration for this test type | `98` |

### Claude Live Log Data

| Data | Log event type | How to extract |
|---|---|---|
| Specific file paths edited | `tool_call` with `tool_name: "Edit"` or `"Write"` | Extract `file_path` param |
| Test command output | `tool_result` for `tool_name: "Bash"` containing test runner output | Parse stdout for pass/fail lines |
| Specific failing test names | `tool_result` for Bash test commands | Regex match `FAILED test_name` or similar |
| Warnings encountered | `tool_result` for test runner with warning-level output | Parse warning lines from test output |
| Number of fix attempts per file | Count Edit tool_calls per unique `file_path` | Group by file_path across tool_calls |
| Agent reasoning about fixes | `text_output` events from fix agent turns | What approach was taken |

### User Questions & Answers

- Did all tests pass in the end? → `status`
- Which test types passed and which failed? → `testCycleRows[].passed`
- How many files were fixed in total? → sum of `testCycleRows[].filesFixed`
- Did the environment need to be rebuilt? → `envRebuildTriggered`
- How many agent calls were needed overall? → `totalAgentCalls`
- How long did the whole repair take? → `durationSeconds`
- Which specific files were edited? → Claude live log Edit/Write `tool_call` params
- What were the actual test failure messages? → Claude live log `tool_result` for Bash/test commands

### Collapsed Dashboard Design Direction

**Currently rendered:** status indicator + status label + duration, per-test-type row (name + pass/fail mark + files fixed + iterations), env rebuild warning.

**Primary metric:** `status` + total duration

**Secondary info:**
- Test type table: one row per `testCycleRows` entry showing type name, ✓/✗, and files fixed count
- Environment rebuild warning when `envRebuildTriggered`

**Enhancement opportunities:**
- Add `totalAgentCalls` as a small data point (e.g., "7 agent calls")
- Add total files fixed across all test types as a summary number
- Show per-type iteration counts more prominently

**Suggested layout:**
```
● SUCCESS                               5m 12s
─────────────────────────────────────
unit          ✓   3 fixed   2 iters
integration   ✓   1 fixed   1 iter
lint          ✓   0 fixed   1 iter
⚠ Env rebuild triggered
```

---

## pr_review_cycle

**Level:** 1 | **Parent:** none

### Available Data

| Field | Source event | Description | Example |
|---|---|---|---|
| `status` | `pr_review_stage_completed` `.status` or `agent_failed` | completed / failed / running | `"completed"` |
| `phaseCount` | `cycle.phases.length` | Number of review phases | `4` |
| `finalStatus` | `pr_review_stage_completed` `.status` | Terminal status string | `"completed"` |

**Per-phase data** (available on child `pr_review_phase` nodes, see §pr_review_phase):

| Field | Source event | Description | Example |
|---|---|---|---|
| `phase` | `pr_review_phase_started` `.phase` | Phase number (integer) | `2` |
| `phase_name` | `pr_review_phase_started` `.phase_name` | Human-readable phase name | `"PR Code Review"` |
| `agent` | `pr_review_phase_started` `.agent` | Agent that runs this phase | `"requirements_verifier"` |
| `duration_seconds` | `pr_review_phase_completed` `.duration_seconds` | Phase duration (all phases) | `1163.73` |
| `success` | `pr_review_phase_completed` `.success` | Whether phase succeeded (all phases) | `true` |
| `failures_found` | `pr_review_phase_completed` `.failures_found` | CI failures (CI phase only) | `0` |
| `issues_found` | `pr_review_phase_completed` `.issues_found` | Compiled issues (Consolidation phase only) | `7` |

### Claude Live Log Data

| Data | Log event type | How to extract |
|---|---|---|
| Specific issues identified | `text_output` from reviewer agent | Structured list of review comments |
| Files reviewed per phase | `tool_call` with `tool_name: "Read"` | Extract `file_path` param |
| Comments drafted | `tool_call` with `tool_name: "Write"` | PR comment content |
| Code patterns flagged | `text_output` with analysis reasoning | Why specific code was flagged |

### User Questions & Answers

- What was the overall verdict? → `finalStatus`
- How many review phases were there? → `phaseCount`
- How many issues were found total? → `issues_found` on the Consolidation phase (phase 4)
- Were there CI failures? → `failures_found` on the CI Status Check phase
- How long did the full review take? → sum of per-phase `duration_seconds`
- What specific issues were flagged? → Claude live log `text_output` from reviewer turns

### Collapsed Dashboard Design Direction

**Currently rendered:** status indicator + status label, phase count.

**Primary metric:** `finalStatus` + phase count

**Secondary info:**
- Total issues found from Consolidation phase `issues_found` — currently not shown, high value addition
- CI status from CI Status Check phase `failures_found` — currently not shown

**Enhancement opportunities (high priority):**
- Aggregate `issues_found` from the Consolidation phase (phase with `phase_name: "Consolidation"`) — this is the canonical issue total
- Show CI failure count from the CI Status Check phase
- Add total duration (sum of per-phase `duration_seconds`)

**Suggested layout:**
```
● COMPLETED
4 review phases  ·  7 issues found  ·  0 CI failures
```

---

## conversational_loop

**Level:** 1 | **Parent:** none

### Available Data

| Field | Source event | Description | Example |
|---|---|---|---|
| `status` | presence of non-inferred `endEvent` | paused / running | `"paused"` |
| `exchangeCount` | count of `agent_initialized` events in cycle | Number of Q&A exchanges | `3` |
| `durationSeconds` | Computed from start/end timestamps | Total loop duration | `210` |

**Sub-events available:**

| Event type | Key fields | Description |
|---|---|---|
| `conversational_question_routed` | `agent_name`, `question_type` | Which agent was assigned to answer |
| `conversational_loop_paused` | `reason`, `pause_type` | Why the loop paused |
| `conversational_loop_resumed` | `resume_trigger` | What resumed the loop |

### Claude Live Log Data

| Data | Log event type | How to extract |
|---|---|---|
| Question text asked by user | `user_message` events in Claude stream | Full question content |
| Agent answers | `text_output` events from answering agent | Full answer text |
| Tools used to research answers | `tool_call` events during answer turns | Read/Grep/WebFetch tool calls |
| Follow-up question detection | Second `user_message` in same session | Whether user asked follow-ups |

### User Questions & Answers

- Is it currently waiting for more input? → `status === 'paused'`
- How many questions have been answered? → `exchangeCount`
- Which agent is handling questions? → `conversational_question_routed` `.agent_name`
- How long has the loop been active? → `durationSeconds`
- What questions were asked? → Claude live log `user_message` events
- Why did it pause? → `conversational_loop_paused` `.reason`

### Collapsed Dashboard Design Direction

**Currently rendered:** status indicator + status label + duration, exchange count.

**Primary metric:** `status` (paused / running) + exchange count

**Secondary info:**
- Duration
- Assigned agent name from `conversational_question_routed` — currently not shown

**Enhancement opportunities:**
- Show which agent is handling questions (from `conversational_question_routed`)
- Show pause reason when paused (from `conversational_loop_paused` `.reason`)
- Show most recent question text as a truncated preview

**Suggested layout:**
```
● PAUSED                                3m 30s
3 exchanges
Agent: Senior Software Engineer
Paused: awaiting user input
```

---

## status_progression

**Level:** 1 | **Parent:** none

### Available Data

| Field | Source event | Description | Example |
|---|---|---|---|
| `fromStatus` | `status_progression_started` `.inputs.from_status` | Source column/status | `"Development"` |
| `toStatus` | `status_progression_started` `.decision.to_status` | Target column/status | `"Code Review"` |
| `trigger` | `status_progression_started` `.inputs.trigger` | What triggered the move | `"agent_auto_advance"` |
| `status` | `status_progression_completed/failed` `.success` | completed / failed / progressing | `"completed"` |
| `durationSeconds` | Computed from start/end timestamps | Duration of the column move | `0.5` |

### User Questions & Answers

- Where did the issue move? → `fromStatus` → `toStatus`
- Did the move succeed? → `status`
- What triggered the status change? → `trigger`
- How long did it take to move? → `durationSeconds`

### Collapsed Dashboard Design Direction

**Currently rendered:** From/To status pill graphic with arrow, status indicator + label, trigger text, duration.

**Primary metric:** `fromStatus → toStatus` transition (the core visual)

**Secondary info:**
- Status indicator (MOVED / FAILED / MOVING...)
- Trigger string
- Duration

**Enhancement opportunities:**
- This renderer is already well-designed and complete
- Could add context about what caused the trigger (e.g., the review cycle result) as hover tooltip

**Suggested layout (already implemented):**
```
[Development] → [Code Review]        0.5s
● MOVED  via agent_auto_advance
```

---

## Level 2 Cycles

---

## review_iteration

**Level:** 2 | **Parent:** `review_cycle`

### Available Data

| Field | Source | Description | Example |
|---|---|---|---|
| `makerAgent` | Inherited from parent `review_cycle_started` `.inputs.maker_agent` | Agent producing work this iteration | `"senior_software_engineer"` |
| `reviewerAgent` | Inherited from parent `review_cycle_started` `.inputs.reviewer_agent` | Agent reviewing work this iteration | `"code_reviewer"` |
| `eventCount` | `iteration.events.length` | Number of events in this iteration | `12` |

Note: `makerAgent` and `reviewerAgent` are inherited from the parent cycle start event, not per-iteration events.

### Claude Live Log Data

| Data | Log event type | How to extract |
|---|---|---|
| Files written by maker | `tool_call` with `tool_name: "Edit"` or `"Write"` from maker agent turn | File paths changed this iteration |
| Review comments by reviewer | `text_output` events from reviewer agent turn | Specific feedback points |
| Maker's summary of changes | `text_output` from maker agent final turn | What was changed and why |
| Number of tool uses by maker | Count of `tool_call` events for maker agent | Work intensity indicator |
| Files read (research) by maker | `tool_call` with `tool_name: "Read"` or `"Grep"` | Context gathering before writing |

### User Questions & Answers

- Who was the maker and reviewer for this iteration? → `makerAgent`, `reviewerAgent`
- What did the maker produce in this iteration? → Claude live log `text_output` from maker turn
- What specific feedback did the reviewer give? → Claude live log `text_output` from reviewer turn
- Which files were changed this iteration? → Claude live log Edit/Write `tool_call` params
- How many events happened in this iteration? → `eventCount`

### Collapsed Dashboard Design Direction

**Currently rendered:** Maker and reviewer agent names only.

**Primary metric:** Maker → Reviewer agent pair

**Secondary info:**
- Event count
- Files changed count (from Claude live logs) — not yet implemented

**Enhancement opportunities (high priority for L2):**
- Show files changed count per iteration (requires Claude live log data)
- Show reviewer verdict for this iteration (approved/needs-revision) if extractable from events
- Duration for this specific iteration (computed from timestamps)

**Suggested layout:**
```
Maker     Senior Software Engineer
Reviewer  Pr Review
12 events
```

---

## repair_test_cycle

**Level:** 2 | **Parent:** `repair_cycle`

### Available Data

| Field | Source event | Description | Example |
|---|---|---|---|
| `testType` | `repair_cycle_test_cycle_completed` `.test_type` | Test suite type | `"compilation"` |
| `passed` | `repair_cycle_test_cycle_completed` `.passed` | Whether this test type passed — integer `1`/`0`, not boolean | `1` |
| `filesFixed` | `repair_cycle_test_cycle_completed` `.files_fixed` | Files fixed for this test type | `0` |
| `iterationsUsed` | `repair_cycle_test_cycle_completed` `.test_cycle_iterations` | Iterations to pass | `3` |
| `durationSeconds` | `repair_cycle_test_cycle_completed` `.duration_seconds` | Duration for this test type | `87` |
| `warningsReviewed` | `repair_cycle_test_cycle_completed` `.warnings_reviewed` | Warnings reviewed count | `2` |
| `testResultRow.passedCount` | last `repair_cycle_test_execution_completed` `.passed` or `.passed_count` | Tests that passed in final run | `18` |
| `testResultRow.failedCount` | last `repair_cycle_test_execution_completed` `.failed` or `.failed_count` | Tests that failed in final run | `0` |
| `testResultRow.warningsCount` | last `repair_cycle_test_execution_completed` `.warnings` or `.warning_count` | Warning count in final run | `1` |
| `hadSystemicFix` | presence of `systemic_fix` sub-cycle in `tc.subCycles` | Whether systemic fix was applied | `false` |

### Claude Live Log Data

| Data | Log event type | How to extract |
|---|---|---|
| Exact failing test names | `tool_result` for Bash test runner (within test_execution sub-cycle) | Parse test names from output |
| File paths fixed | `tool_call` with `tool_name: "Edit"` within fix_cycle sub-cycles | Enumerate unique paths |
| Warning messages | `tool_result` for Bash or linter within warning_review | Warning text |
| Fix approach per file | `text_output` during fix_cycle turns | Strategy explanation |

### User Questions & Answers

- Did this test type ultimately pass? → `passed`
- How many tests passed / failed in the final run? → `testResultRow.passedCount` / `.failedCount`
- Were there warnings? → `warningsReviewed` / `testResultRow.warningsCount`
- Was a systemic fix required? → `hadSystemicFix`
- How many files were fixed for this test type? → `filesFixed`
- How many fix iterations did it take to pass? → `iterationsUsed`
- How long did this test cycle take? → `durationSeconds`
- Which specific files were edited? → Claude live log Edit/Write events within this cycle

### Collapsed Dashboard Design Direction

**Currently rendered:** Pass/fail label, files fixed, duration, test pass/fail/warn counts, warnings reviewed count, systemic fix flag.

**Primary metric:** ✓ Passed / ✗ Failed + test type name

**Secondary info:**
- Test counts: `N pass / M fail / K warn`
- Files fixed count
- Systemic fix flag
- Warnings reviewed count

**Enhancement opportunities:**
- Show `iterationsUsed` more prominently
- Add duration
- Show `durationSeconds` in the header row

**Suggested layout (largely already implemented):**
```
✓ Passed          unit          1m 27s
Tests: 18 pass / 0 fail / 1 warn
2 files fixed  ·  3 iters
🔍 Systemic fix applied
```

---

## pr_review_phase

**Level:** 2 | **Parent:** `pr_review_cycle`

### Available Data

**On `pr_review_phase_started`** (present on all phases):

| Field | Event field | Description | Example |
|---|---|---|---|
| `phaseNumber` | `.phase` (integer) | Phase number | `1` |
| `phase_name` | `.phase_name` | Human-readable phase name | `"PR Code Review"` |
| `agent` | `.agent` | Which agent handles this phase | `"pr_code_reviewer"` |
| `sub_phase` | `.sub_phase` (optional) | Sub-phase index for multi-part phases | `1` |

**On `pr_review_phase_completed`** (field availability varies by phase type):

| Field | Event field | Present on | Description | Example |
|---|---|---|---|---|
| `duration_seconds` | `.duration_seconds` | All phases | Phase wall-clock duration | `1163.73` |
| `success` | `.success` | All phases | Whether the phase succeeded | `true` |
| `text_collected` | `.text_collected` | Review/verification phases | Whether review text was gathered | `true` |
| `sub_phase` | `.sub_phase` | Multi-part phases (phase 2) | Sub-phase index | `1` |
| `failures_found` | `.failures_found` | CI Status Check phase only | Number of CI failures | `0` |
| `pending_count` | `.pending_count` | CI Status Check phase only | CI checks still pending | `0` |
| `issues_found` | `.issues_found` | Consolidation phase only | Total issues compiled | `7` |

**Currently extracted by `extractPRReviewPhaseSummary()`:**
- `phaseNumber` (from `phase.number`, which is the array index + 1)
- `eventCount` (from `phase.events.length`)

All other fields above are available but not yet extracted.

**Real phase names observed in data:**
1. `"PR Code Review"` — agent: `pr_code_reviewer`
2. `"Context Verification: Parent Issue Requirements"` — agent: `requirements_verifier`, has `sub_phase`
3. `"Context Verification: Software Architect Output"` — agent: `requirements_verifier`, has `sub_phase`
4. `"CI Status Check"` — agent: `local_gh_cli`, has `failures_found` + `pending_count`
5. `"Consolidation"` — agent: `pr_code_reviewer`, has `issues_found`

### Claude Live Log Data

| Data | Log event type | How to extract |
|---|---|---|
| Review comments drafted | `tool_call` with `tool_name: "Write"` | Comment content |
| Files examined this phase | `tool_call` with `tool_name: "Read"` | File paths reviewed |
| Issues described | `text_output` during reviewer turn | Specific issue descriptions |

### User Questions & Answers

- What was reviewed in this phase? → `phase_name` (from start event)
- Which agent ran this phase? → `agent` (from start event, e.g., `pr_code_reviewer`, `requirements_verifier`, `local_gh_cli`)
- Did this phase succeed? → `success` (on completed event)
- How long did this phase take? → `duration_seconds`
- Were there CI failures? → `failures_found` (CI Status Check phase only)
- How many total issues were found? → `issues_found` (Consolidation phase only)
- Was review text collected? → `text_collected`

### Collapsed Dashboard Design Direction

**Currently rendered:** Event count only (very sparse).

**Primary metric:** `phase_name` — currently NOT shown despite being on the start event.

**Enhancement opportunities (high priority — current implementation shows only event count):**
- Add `phase_name` as primary label
- Add `success` indicator (✓/✗)
- Add `duration_seconds`
- For CI Status Check phase: show `failures_found` and `pending_count`
- For Consolidation phase: show `issues_found`
- Update `extractPRReviewPhaseSummary()` to read from both `phase.startEvent` and `phase.events` (find the `pr_review_phase_completed` event)

**Suggested layout:**
```
PR Code Review                       19m 23s
✓ text collected  ·  8 events

── or for CI phase ──

CI Status Check                          0s
✓ 0 failures  ·  0 pending

── or for Consolidation phase ──

Consolidation                           34s
7 issues found
```

---

## Level 3 Cycles

---

## test_execution

**Level:** 3 | **Parent:** `repair_test_cycle`

### Available Data

| Field | Source event | Description | Example |
|---|---|---|---|
| `testPassedCount` | `repair_cycle_test_execution_completed` `.passed` | Tests that passed (integer count) | `13` |
| `testFailedCount` | `repair_cycle_test_execution_completed` `.failed` | Tests that failed (integer count) | `2` |
| `failures[]` | `repair_cycle_test_execution_completed` `.failures` | Structured failure objects (not yet surfaced in UI) | see below |
| `has_failures` | `repair_cycle_test_execution_completed` `.has_failures` | Boolean shorthand for failed > 0 | `true` |
| `test_type` | `repair_cycle_test_execution_completed` `.test_type` | Which test suite this run belongs to | `"pre-commit"` |

Note: Both counts come from `extractSubCycleSummary()` via `endEvent.passed` and `endEvent.failed`.

**`failures[]` object shape** (available directly on the event, no live log parsing needed):
```json
{
  "file": "cli/src/core/virtual-projection.ts",
  "message": "error TS2322: Type '{}' is not assignable to type 'string' at line 197",
  "test": "TypeScript Linting"
}
```
This structured data is not yet surfaced in any renderer and is the highest-value addition for the `test_execution` dashboard.

### Claude Live Log Data

| Data | Log event type | How to extract |
|---|---|---|
| Full test output | `tool_result` for `tool_name: "Bash"` (test runner command) | Complete stdout of test run |
| Test runner command used | `tool_call` with `tool_name: "Bash"` `.command` param | e.g., `pytest tests/unit/` |
| Additional context per failure | `text_output` from fix agent analyzing results | Why specific tests failed |

Note: The `failures[]` array on the event already provides structured file/message/test data — Claude live logs are only needed for the raw stdout or fix agent reasoning.

### User Questions & Answers

- How many tests passed? → `testPassedCount`
- How many tests failed? → `testFailedCount`
- What is the pass rate? → `testPassedCount / (testPassedCount + testFailedCount)`
- Which specific tests failed, and in which file? → `failures[].test` + `failures[].file`
- What were the error messages? → `failures[].message`

### Collapsed Dashboard Design Direction

**Currently rendered:** ✓ N pass / ✗ M fail counts.

**Primary metric:** Pass/fail counts (green/red)

**Secondary info:**
- Pass rate percentage — not yet implemented
- Failing test list from `failures[]` — not yet implemented, but fully structured data

**Enhancement opportunities:**
- Add pass rate as a percentage (computed from counts)
- Add compact failure list from `failures[]` — show `test` name + `file` (no live log parsing required)
- Add a mini progress bar proportional to pass/fail counts

**Suggested layout:**
```
✓ 13 pass   ✗ 2 fail   92%
████████████████░░
─────────────────────────────────────
TypeScript Linting
· virtual-projection.ts:197
· virtual-projection.ts:281
```

---

## fix_cycle

**Level:** 3 | **Parent:** `repair_test_cycle`

### Available Data

| Field | Source event | Description | Example |
|---|---|---|---|
| `filesFixed` | `repair_cycle_fix_cycle_completed` `.files_fixed` | Number of files fixed this cycle | `2` |

Note: `repair_cycle_fix_cycle_completed` events are not present in available sample data files, so `files_fixed` is documented from code inspection of the extractor (`extractSubCycleSummary`) rather than verified against a real event payload. This field is expected to exist based on the naming convention consistent with other cycle completion events (`repair_cycle_test_cycle_completed.files_fixed` is confirmed).

### Claude Live Log Data

| Data | Log event type | How to extract |
|---|---|---|
| Exact file paths modified | `tool_call` with `tool_name: "Edit"` or `"Write"` | `.params.file_path` |
| What was changed per file | `text_output` from fix agent | Reasoning and approach |
| Functions/classes changed | `tool_call` Edit `.params.old_string` / `.new_string` | The actual diff |
| Bash commands run | `tool_call` with `tool_name: "Bash"` | Verify-fix commands |

### User Questions & Answers

- How many files were fixed this cycle? → `filesFixed`
- Which specific files were changed? → Claude live log Edit/Write `tool_call` params
- What exactly was changed in each file? → Claude live log Edit `tool_call` `old_string`/`new_string`
- What was the reasoning for the fix? → Claude live log `text_output`

### Collapsed Dashboard Design Direction

**Currently rendered:** Files fixed count (amber).

**Primary metric:** `filesFixed` count

**Secondary info:**
- File names list (from Claude live logs) — not yet implemented
- Fix reasoning summary — not yet implemented

**Enhancement opportunities:**
- Show file names (truncated) from Claude live log Edit tool calls
- Shows as compact list: e.g., `tests/test_auth.py`, `src/auth.py`

**Suggested layout:**
```
2 files fixed
· tests/test_auth.py
· src/auth.py
```

---

## warning_review

**Level:** 3 | **Parent:** `repair_test_cycle`

### Available Data

| Field | Source event | Description | Example |
|---|---|---|---|
| `warningCount` | `repair_cycle_warning_review_completed` `.warnings_reviewed` or `.warning_count` | Warnings reviewed | `4` |

### Claude Live Log Data

| Data | Log event type | How to extract |
|---|---|---|
| Warning messages text | `tool_result` for linter/test tool | Full warning text |
| Files with warnings | `tool_call` with `tool_name: "Read"` | Files examined for warnings |
| Categorization decisions | `text_output` from warning review agent | Which warnings are actionable |
| Warnings dismissed as noise | `text_output` reasoning | Which warnings are acceptable |

### User Questions & Answers

- How many warnings were found? → `warningCount`
- What types of warnings were there? → Claude live log `text_output` from reviewer
- Were the warnings actionable? → Claude live log agent reasoning text
- Which files had warnings? → Claude live log Read `tool_call` params

### Collapsed Dashboard Design Direction

**Currently rendered:** Warning count.

**Primary metric:** Warning count

**Secondary info:**
- Actionable vs noise breakdown — not yet implemented (requires Claude live log parsing)

**Enhancement opportunities:**
- Show "N actionable / M dismissed" breakdown if available from agent text output

**Suggested layout:**
```
⚠ 4 warnings reviewed
```

---

## systemic_analysis

**Level:** 3 | **Parent:** `repair_test_cycle`

### Available Data

| Field | Source event | Description | Example |
|---|---|---|---|
| `patternCategory` | `repair_cycle_systemic_analysis_completed` `.pattern_category` | Root cause category label | `null` (not present in real events — see note) |
| `affectedFiles` | `repair_cycle_systemic_analysis_completed` `.affected_files_count` | Number of files affected | `2` |
| `has_systemic_code_issues` | `repair_cycle_systemic_analysis_completed` `.has_systemic_code_issues` | Whether a systemic code pattern was found | `true` |
| `has_env_issues` | `repair_cycle_systemic_analysis_completed` `.has_env_issues` | Whether env-level issues were found | `false` |
| `test_type` | `repair_cycle_systemic_analysis_completed` `.test_type` | Which test suite triggered the analysis | `"pre-commit"` |

**Important:** `pattern_category` does not appear in real event data. `extractSubCycleSummary()` attempts to read it (`endEvent?.pattern_category ?? null`) but it is always null. The actual classification fields are `has_systemic_code_issues` and `has_env_issues` (booleans). These two fields are not yet extracted by the summary function and are the highest-value additions for this cycle type.

### Claude Live Log Data

| Data | Log event type | How to extract |
|---|---|---|
| Root cause analysis text | `text_output` from analysis agent | Full reasoning about the pattern |
| Files examined for patterns | `tool_call` with `tool_name: "Read"` or `"Grep"` | Context gathering scope |
| Pattern search queries | `tool_call` with `tool_name: "Grep"` `.params.pattern` | What patterns were searched |
| Identified anti-patterns | `text_output` structured findings | Specific code patterns found |

### User Questions & Answers

- Was a systemic code issue identified? → `has_systemic_code_issues`
- Was it an environment issue? → `has_env_issues`
- How many files are affected? → `affectedFiles`
- Which test suite triggered this analysis? → `test_type` (on the event, not yet in summary)
- What specifically was wrong? → Claude live log `text_output` from analysis agent
- What code patterns were searched? → Claude live log Grep `tool_call` params

### Collapsed Dashboard Design Direction

**Currently rendered:** Pattern category label (always null in practice) + affected files count.

**Primary metric:** Issue type classification + `affectedFiles` count

**Secondary info:**
- Issue classification: "Systemic Code Issue" or "Environment Issue" (derived from `has_systemic_code_issues` / `has_env_issues`)
- Test type that triggered the analysis
- Analysis conclusion from Claude live log (truncated)

**Enhancement opportunities (high priority — current renderer shows null `patternCategory`):**
- Replace `patternCategory` with `has_systemic_code_issues` / `has_env_issues` derived label
- Add `test_type` to show what was being tested when the systemic issue was found
- Add `affectedFiles` count prominently
- Extract `has_systemic_code_issues` and `has_env_issues` in `extractSubCycleSummary()`

**Suggested layout:**
```
⚙ Systemic Code Issue
pre-commit  ·  2 files affected
```

---

## systemic_fix

**Level:** 3 | **Parent:** `repair_test_cycle`

### Available Data

| Field | Source event | Description | Example |
|---|---|---|---|
| `filesFixed` | `repair_cycle_systemic_fix_completed` `.files_fixed` | Files changed (not present in real events — see note) | `null` |
| `attempts` | `repair_cycle_systemic_fix_completed` `.attempts` | Number of fix attempts made | `1` |
| `tests_pass` | `repair_cycle_systemic_fix_completed` `.tests_pass` | Whether tests passed after the systemic fix | `true` |
| `test_type` | `repair_cycle_systemic_fix_completed` `.test_type` | Test suite that required this fix | `"pre-commit"` |

**Important:** `files_fixed` does not appear in real `repair_cycle_systemic_fix_completed` events. `extractSubCycleSummary()` attempts to read it but always returns null. The actual outcome fields are `tests_pass` (did the fix work?) and `attempts` (how many tries). These are not yet extracted and are the most useful additions.

### Claude Live Log Data

| Data | Log event type | How to extract |
|---|---|---|
| Files modified | `tool_call` with `tool_name: "Edit"` or `"Write"` | File paths in systemic change |
| Scope of change | Count of unique `file_path` values from Edit tool calls | Breadth of systemic fix |
| Change applied per file | Edit `tool_call` `old_string`/`new_string` | The actual transformation |
| Agent reasoning | `text_output` from systemic fix agent | What broad change was applied |

### User Questions & Answers

- Did the systemic fix make tests pass? → `tests_pass`
- How many attempts did it take? → `attempts`
- Which test suite is this for? → `test_type`
- Which specific files were changed? → Claude live log Edit `tool_call` params
- What was the broad transformation applied? → Claude live log `text_output` from fix agent
- Is this fix related to the pattern found in systemic_analysis? → linked via parent `repair_test_cycle`

### Collapsed Dashboard Design Direction

**Currently rendered:** "N files fixed (systemic)" — but `filesFixed` is always null in real data, so this never renders.

**Primary metric:** Fix outcome (`tests_pass`) + test type

**Secondary info:**
- Number of attempts
- File names from Claude live logs

**Enhancement opportunities (high priority — current renderer never shows anything due to missing data):**
- Extract `tests_pass` and `attempts` in `extractSubCycleSummary()`
- Show ✓/✗ based on `tests_pass` as the primary status
- Show file names from Claude live log Edit tool calls

**Suggested layout:**
```
✓ Tests passing (systemic)
pre-commit  ·  1 attempt
· src/core/virtual-projection.ts
```

---

## Summary Data Structures Reference

All `extract*Summary()` return shapes, as defined in `cycleSummaries.js`:

### extractReviewCycleSummary()
```js
{
  // NOTE: status derivation is buggy — extractor reads endEvent.outcome which is absent
  // in real events (real field is endEvent.status). Currently works for 'approved' only
  // via the else-fallthrough. Fix: read endEvent.status directly.
  status: 'approved' | 'rejected' | 'escalated' | 'running',
  makerAgent: string | null,        // from review_cycle_started.inputs.maker_agent
  reviewerAgent: string | null,     // from review_cycle_started.inputs.reviewer_agent
  totalIterations: number,          // from cycle.iterations.length (not from event's total_iterations)
  maxIterations: number | null,     // from review_cycle_started.max_iterations
  durationSeconds: number | null,   // computed from timestamps
  iterations: Array<{ number: number, durationSeconds: number | null }>,
  completionReason: string | null,  // from review_cycle_completed.reason (full string, not a code)
}
```

### extractRepairCycleSummary()
```js
{
  status: 'success' | 'failed' | 'running',
  durationSeconds: number | null,
  totalAgentCalls: number | null,
  envRebuildTriggered: boolean,
  testCycleRows: Array<{
    testType: string,
    passed: boolean | null,
    filesFixed: number | null,
    iterations: number | null,
    durationSeconds: number | null,
  }>,
}
```

### extractTestCycleSummary()  _(used for repair_test_cycle)_
```js
{
  testType: string,
  passed: 1 | 0 | null,   // integer from event, not boolean — truthy checks work fine
  filesFixed: number | null,
  iterationsUsed: number | null,
  durationSeconds: number | null,
  warningsReviewed: number | null,
  testResultRow: { passedCount: number | null, failedCount: number | null, warningsCount: number | null } | null,
  hadSystemicFix: boolean,
}
```

### extractSubCycleSummary()  _(used for test_execution, fix_cycle, warning_review, systemic_analysis, systemic_fix)_
```js
{
  cycleType: string,
  outcome: 'complete' | 'running',
  filesFixed: number | null,          // fix_cycle: from .files_fixed ✓ | systemic_fix: field absent in real events, always null
  warningCount: number | null,        // warning_review: from .warnings_reviewed or .warning_count
  patternCategory: string | null,     // systemic_analysis: .pattern_category absent in real events, always null
  affectedFiles: number | null,       // systemic_analysis: from .affected_files_count ✓
  testPassedCount: number | null,     // test_execution: from .passed (integer count) ✓
  testFailedCount: number | null,     // test_execution: from .failed (integer count) ✓
  // NOT YET EXTRACTED but available on endEvent:
  // failures: Array<{file, message, test}>  — test_execution: structured failure list (highest value addition)
  // has_systemic_code_issues: boolean        — systemic_analysis: actual classification field
  // has_env_issues: boolean                  — systemic_analysis: whether env-level issue
  // tests_pass: boolean                      — systemic_fix: whether fix succeeded
  // attempts: number                         — systemic_fix: how many fix attempts were made
}
```

### extractReviewIterationSummary()
```js
{
  makerAgent: string | null,
  reviewerAgent: string | null,
  eventCount: number,
}
```

### extractPRReviewCycleSummary()
```js
{
  status: 'completed' | 'failed' | 'running',
  phaseCount: number,
  finalStatus: string | null,
}
```

### extractPRReviewPhaseSummary()
```js
{
  phaseNumber: number,     // from array index (phase.number = idx + 1), NOT from event's .phase field
  eventCount: number,
  // Fields available on events but NOT YET EXTRACTED (high-priority):
  // phase_name: string        — from phase.startEvent.phase_name
  // agent: string             — from phase.startEvent.agent (which agent ran this phase)
  // duration_seconds: number  — from pr_review_phase_completed endEvent
  // success: boolean          — from pr_review_phase_completed .success (all phases)
  // failures_found: number    — from pr_review_phase_completed (CI Status Check phase only)
  // pending_count: number     — from pr_review_phase_completed (CI Status Check phase only)
  // issues_found: number      — from pr_review_phase_completed (Consolidation phase only)
  // text_collected: boolean   — from pr_review_phase_completed (review/verification phases)
  // sub_phase: number         — from pr_review_phase_completed (multi-part phases only)
}
```

### extractConversationalLoopSummary()
```js
{
  status: 'paused' | 'running',
  exchangeCount: number,
  durationSeconds: number | null,
}
```

### extractStatusProgressionSummary()
```js
{
  fromStatus: string | null,
  toStatus: string | null,
  trigger: string | null,
  status: 'completed' | 'failed' | 'progressing',
  durationSeconds: number | null,
}
```

---

## Enhancement Priority Summary

| Cycle type | Current implementation | Highest-value additions | Data source |
|---|---|---|---|
| `review_cycle` | Good — status, agents, iteration timeline | Fix extractor to use `.status` not `.outcome`; add `completionReason` display | Event fields |
| `repair_cycle` | Good — status, test type table, env rebuild | Add `totalAgentCalls`, total files fixed sum | Event fields |
| `pr_review_cycle` | Sparse — status + phase count only | Pull `issues_found` from Consolidation phase; CI failure count from CI phase | Child phase events |
| `conversational_loop` | Minimal — status + exchange count | Show assigned agent from `conversational_question_routed`; pause reason | Sub-events |
| `status_progression` | Excellent — fully implemented | No changes needed | — |
| `review_iteration` | Minimal — agent names only | Files changed per iteration (requires live logs); reviewer verdict | Claude live logs |
| `repair_test_cycle` | Good — pass/fail, test counts, systemic flag | Add duration to header row | Event fields |
| `pr_review_phase` | Broken — only shows event count; phase_name not extracted | Extract `phase_name`, `duration_seconds`, `success`; phase-specific fields | Event fields (start + end) |
| `test_execution` | Good — pass/fail counts | Add `failures[]` list (file + message); pass rate % | Event field (already present) |
| `fix_cycle` | Minimal — files fixed count | File name list | Claude live logs |
| `warning_review` | Minimal — warning count only | Actionable vs noise breakdown | Claude live logs |
| `systemic_analysis` | Broken — `patternCategory` always null | Extract `has_systemic_code_issues`/`has_env_issues` instead; show `test_type` | Event fields (not yet extracted) |
| `systemic_fix` | Broken — `filesFixed` always null; never renders | Extract `tests_pass` and `attempts`; show fix outcome | Event fields (not yet extracted) |

**Legend:** "Broken" means the current renderer never displays data because the fields it reads are absent in real events.
