---
name: pipeline-flow-audit
description: Compare actual pipeline execution against expected flow from config
user_invocable: true
args: "<pipeline_run_id>"
---

# Pipeline Flow Audit

You are auditing a pipeline run to compare its actual execution against the expected flow defined in config. The user's argument is: `$ARGUMENTS`.

If no run ID was provided, find recent pipeline runs first:
```bash
curl -s "http://localhost:9200/pipeline-runs-*/_search" -H 'Content-Type: application/json' -d '{"query":{"match_all":{}},"sort":[{"started_at":"desc"}],"size":10}' | jq '.hits.hits[]._source | {id, project, board, status, issue_number, issue_title}'
```
Then ask the user which run to audit.

## Step 1: Get Pipeline Run Metadata

```bash
curl -s "http://localhost:9200/pipeline-runs-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {"term": {"id": "<RUN_ID>"}},
  "size": 1
}' | jq '.hits.hits[]._source'
```

Determine the pipeline type from the `board` field:
- Board contains "Planning" or "Design" → `planning_design`
- Board contains "SDLC" or "Execution" → `sdlc_execution`
- Board contains "Environment" → `environment_support`

## Step 2: Load Expected Flow

Read the pipeline config to understand expected stages:
```bash
cat config/foundations/pipelines.yaml
```
```bash
cat config/foundations/workflows.yaml
```

### Expected Flows Reference

**sdlc_execution**:
1. `implementation` → agent: `senior_software_engineer`, timeout: 1800s, retries: 5
2. `implementation` review cycle → reviewer: `code_reviewer`, max 5 iterations, escalate at 1 blocking finding
3. `testing` → agent: `senior_software_engineer`, repair cycle, max 100 agent calls, checkpoint every 5
4. `staging` → agent: `senior_software_engineer`, timeout: 7200s

Board columns: Backlog → Development → Code Review → Testing → Staged → Done

**planning_design**:
1. `research` → agent: `idea_researcher` (conversational, feedback timeout: 3600s)
2. `requirements` → agent: `business_analyst` (conversational)
3. `design` → agent: `software_architect` (conversational)
4. `work_breakdown` → agent: `work_breakdown_agent` (conversational)
5. *In Development* (tracking only, no agent)
6. `pr_review` → agent: `pr_review_agent`
7. `documentation` → agent: `technical_writer`
8. `documentation` review → reviewer: `documentation_editor`, max 3 iterations

Board columns: Backlog → Research → Requirements → Design → Work Breakdown → In Development → In Review → Documentation → Documentation Review → Done

**environment_support**:
1. `environment_setup` → agent: `dev_environment_setup`, timeout: 1800s, retries: 3
2. `environment_verification` → agent: `dev_environment_verifier`

Board columns: Backlog → In Progress → Verification → Done

## Step 3: Query All Events for the Run

Decision events:
```bash
curl -s "http://localhost:9200/decision-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {"term": {"pipeline_run_id": "<RUN_ID>"}},
  "sort": [{"timestamp": "asc"}],
  "size": 500
}' | jq '.hits.hits[]._source | {timestamp, event_type, agent, data}'
```

Agent events:
```bash
curl -s "http://localhost:9200/agent-events-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {"term": {"pipeline_run_id": "<RUN_ID>"}},
  "sort": [{"timestamp": "asc"}],
  "size": 100
}' | jq '.hits.hits[]._source | {timestamp, agent_name, event_type, success, duration_ms}'
```

## Step 4: Build Actual Stage Sequence

From events, reconstruct:
- Which agents ran and in what order
- Which board columns were visited (from `status_progression_*` events)
- Review cycle iterations (`review_cycle_iteration` events, count per stage)
- Repair cycle iterations (`repair_cycle_iteration` events)
- Any escalations or early terminations

## Step 5: Side-by-Side Comparison

Build a comparison table:

| Stage | Expected Agent | Actual Agent | Expected Behavior | Actual Behavior | Status |
|-------|---------------|-------------|-------------------|-----------------|--------|
| implementation | senior_software_engineer | ... | Initial execution | ... | ... |
| code_review | code_reviewer (max 5 iter) | ... | Review cycle | ... | ... |
| testing | senior_software_engineer (max 100 calls) | ... | Repair cycle | ... | ... |
| staging | senior_software_engineer | ... | Final stage | ... | ... |

Status values: `matched`, `deviated`, `skipped`, `extra`, `failed`

## Step 6: Identify and Explain Deviations

For each deviation, explain:
- **Skipped stage**: Was it intentionally skipped (e.g., no tests configured)? Or was there an error?
- **Wrong agent**: Was fallback routing used? Check `agent_routing_decision` events.
- **Excessive iterations**: What was the reviewer/test feedback pattern? Were the same issues recurring?
- **Early termination**: Circuit breaker? Escalation? Manual kill?
- **Extra stages**: Were there retry cycles or recovery attempts?

## Step 7: Fetch GitHub Issue Context

```bash
gh issue view <ISSUE_NUMBER> --repo <ORG>/<REPO> --json title,labels,state,body | jq '{title, labels: [.labels[].name], state}'
```

Verify:
- Issue labels match the expected pipeline (e.g., `pipeline:sdlc-execution`)
- Issue state is consistent with pipeline outcome

## Output

Present the audit as:

```
## Pipeline Flow Audit: <RUN_ID>

### Pipeline: <type> | Issue: #<number> <title>
### Status: <completed|failed|active> | Duration: <time>

### Flow Comparison
<side-by-side table from step 5>

### Deviations
<numbered list of deviations with explanations>

### Assessment
<overall assessment: clean run, minor deviations, significant issues>
```
