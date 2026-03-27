---
name: pipeline-recommendations
description: Analyze recommendations from pipeline run analyses — filter by priority, target (orchestrator/project), pipeline run ID, or project
user_invocable: true
args: "[pipeline_run_id] [--project <name>] [--priority <high|medium|low>] [--target <orchestrator|project>] [--recent [time_window]]"
---

# Pipeline Recommendations Analysis

You are analyzing AI-generated recommendations from completed pipeline run post-mortems. The user's argument is: `$ARGUMENTS`.

Recommendations come in two flavors stored in each completed pipeline run document:
- **orchestrator**: Fixes to the switchyard orchestrator codebase (always includes `filePath`)
- **project**: Improvements to the managed project being developed

---

## Argument Parsing

Determine mode from `$ARGUMENTS`:

| Pattern | Mode |
|---|---|
| UUID (e.g. `3d1d2a9b-f9e6-...`) | **Run mode** — single pipeline run |
| `--project <name>` | **Project mode** — all runs for that project |
| `--recent [time_window]` | **Recent mode** — time window (default: `7d`) |
| `--priority high\|medium\|low` | Priority filter — applies to any mode |
| `--target orchestrator\|project` | Target filter — applies to any mode |
| No args | **Summary mode** — cross-project, last 30 days |

Flags are combinable: `--project context-studio --priority high --target orchestrator` is valid.

---

## Index Reference

**Index**: `pipeline-runs-*` (date-partitioned, 7-day ILM retention — use wildcard)

| Field | Type | Notes |
|---|---|---|
| `_id` | keyword | Pipeline run ID (also stored as `id` in `_source`) |
| `project` | keyword | Project name — exact match in term queries |
| `status` | keyword | Always filter `"completed"` |
| `outcome` | keyword | `"success"` or `"failed"` |
| `ended_at` | date | ISO 8601 — use for time-window queries |
| `issue_number` | integer | GitHub issue number |
| `issue_title` | text | Issue title |
| `orchestratorRecommendations` | array | Recommendations targeting switchyard itself |
| `orchestratorRecommendations[].priority` | keyword | `"high"` / `"medium"` / `"low"` |
| `orchestratorRecommendations[].category` | keyword | `"bug"` / `"improvement"` / `"performance"` / `"configuration"` |
| `orchestratorRecommendations[].description` | text | What should be fixed |
| `orchestratorRecommendations[].filePath` | keyword | Path in switchyard codebase (optional) |
| `projectRecommendations` | array | Recommendations for the managed project |
| `projectRecommendations[].priority` | keyword | `"high"` / `"medium"` / `"low"` |
| `projectRecommendations[].category` | keyword | `"bug"` / `"improvement"` / `"performance"` / `"configuration"` |
| `projectRecommendations[].description` | text | What should be fixed |

> **Priority filtering is client-side** — recommendations are nested array objects without a dedicated ES nested mapping, so fetch broadly and filter in Python/jq.

---

## Step 1: Fetch Recommendations

Use the appropriate query for the detected mode.

### Mode A: Specific Pipeline Run

```bash
curl -s "http://localhost:9200/pipeline-runs-*/_search" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": {"term": {"_id": "<PIPELINE_RUN_ID>"}},
    "size": 1,
    "_source": ["id", "project", "outcome", "ended_at", "issue_number", "issue_title", "issue_url",
                "orchestratorRecommendations", "projectRecommendations", "summary"]
  }' | jq '.hits.hits[0]._source'
```

Extract `orchestratorRecommendations` and `projectRecommendations` arrays from the result. Also note the `summary` field — it contains Claude's free-text analysis of the run and is useful context for understanding the recommendations.

### Mode B: Project-Scoped

Use the observability REST API (handles `status=completed` filter automatically):

```bash
curl -s "http://localhost:5001/api/pipeline-recommendations?project=<PROJECT>&rec_type=all" | \
  jq '.recommendations | length, .[0:3]'
```

If the REST API is unavailable, fall back to direct ES:

```bash
curl -s "http://localhost:9200/pipeline-runs-*/_search" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": {
      "bool": {
        "filter": [
          {"term": {"status": "completed"}},
          {"term": {"project": "<PROJECT>"}},
          {"bool": {"should": [
            {"exists": {"field": "orchestratorRecommendations"}},
            {"exists": {"field": "projectRecommendations"}}
          ], "minimum_should_match": 1}}
        ]
      }
    },
    "sort": [{"ended_at": "desc"}],
    "size": 200,
    "_source": ["id", "project", "outcome", "ended_at", "issue_number", "issue_title", "issue_url",
                "orchestratorRecommendations", "projectRecommendations"]
  }' | jq '.hits.hits[]._source'
```

### Mode C: Recent / Time-Windowed

Replace `<TIME_WINDOW>` with arg value (e.g. `1h`, `24h`, `7d`) or default `7d`:

```bash
curl -s "http://localhost:9200/pipeline-runs-*/_search" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": {
      "bool": {
        "filter": [
          {"term": {"status": "completed"}},
          {"range": {"ended_at": {"gte": "now-<TIME_WINDOW>"}}},
          {"bool": {"should": [
            {"exists": {"field": "orchestratorRecommendations"}},
            {"exists": {"field": "projectRecommendations"}}
          ], "minimum_should_match": 1}}
        ]
      }
    },
    "sort": [{"ended_at": "desc"}],
    "size": 200,
    "_source": ["id", "project", "outcome", "ended_at", "issue_number", "issue_title", "issue_url",
                "orchestratorRecommendations", "projectRecommendations"]
  }' | jq '[.hits.hits[]._source]'
```

### Mode D: Summary (No Args)

Same as Mode C with `30d` window, but also get a count breakdown first:

```bash
# Count runs with recommendations
curl -s "http://localhost:9200/pipeline-runs-*/_search" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": {
      "bool": {
        "filter": [
          {"term": {"status": "completed"}},
          {"range": {"ended_at": {"gte": "now-30d"}}},
          {"bool": {"should": [
            {"exists": {"field": "orchestratorRecommendations"}},
            {"exists": {"field": "projectRecommendations"}}
          ], "minimum_should_match": 1}}
        ]
      }
    },
    "size": 0,
    "aggs": {
      "by_project": {"terms": {"field": "project", "size": 20}},
      "by_outcome": {"terms": {"field": "outcome", "size": 5}}
    }
  }' | jq '{total: .hits.total.value, by_project: .aggregations.by_project.buckets, by_outcome: .aggregations.by_outcome.buckets}'
```

Then fetch the docs with the Mode C query using `30d`.

---

## Step 2: Filter and Organize

After fetching, apply filters from args client-side and flatten all recommendations into a unified list.

Use Python to process (pipe through `python3 -c "..."`):

```python
import json, sys

docs = json.load(sys.stdin)  # array of _source docs from jq output
priority_filter = None   # replace with arg value if --priority was given
target_filter = None     # replace with "orchestrator" or "project" if --target was given

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2, "": 3}
recs = []

for doc in docs:
    run_ctx = {
        "id": doc.get("id", ""),
        "project": doc.get("project", ""),
        "outcome": doc.get("outcome", ""),
        "issue_number": doc.get("issue_number"),
        "issue_title": doc.get("issue_title", ""),
        "issue_url": doc.get("issue_url", ""),
        "ended_at": (doc.get("ended_at") or "")[:10],
    }
    if target_filter in (None, "orchestrator"):
        for r in doc.get("orchestratorRecommendations") or []:
            if priority_filter is None or r.get("priority") == priority_filter:
                recs.append({**r, "rec_type": "orchestrator", **run_ctx})
    if target_filter in (None, "project"):
        for r in doc.get("projectRecommendations") or []:
            if priority_filter is None or r.get("priority") == priority_filter:
                recs.append({**r, "rec_type": "project", **run_ctx})

recs.sort(key=lambda r: (r["rec_type"] != "orchestrator", PRIORITY_ORDER.get(r.get("priority", ""), 3)))
print(json.dumps(recs))
```

---

## Step 3: Synthesize and Present

### Recommendation Tables

Group by target (orchestrator first), then sort high → medium → low within each group.

**Orchestrator Recommendations** (target the switchyard codebase):

| Priority | Category | File | Description | Project | Run |
|---|---|---|---|---|---|
| 🔴 high | bug | `services/review_cycle.py` | Add loop detection for review cycles > 8 iterations | codetoreum | #42 |
| 🟡 medium | performance | `pipeline/pr_review_stage.py` | Add timeout warnings for long verification phases | documentation_robotics_viewer | #17 |

**Project Recommendations** (target the managed project codebase):

| Priority | Category | Description | Project | Run |
|---|---|---|---|---|
| 🟢 low | improvement | Add CI/CD pipeline configuration for automated testing | context-library | #31 |

Priority emoji: 🔴 high · 🟡 medium · 🟢 low

For **Mode A (single run)**, also show the `summary` narrative before the tables to give context.

### Summary Statistics

After the tables, provide:

```
## Summary

Total: N recommendations (X orchestrator, Y project)
  🔴 High:   N
  🟡 Medium: N
  🟢 Low:    N

Top categories:
  improvement    N
  bug            N
  performance    N
  configuration  N
```

For **cross-project modes**, also include a per-project breakdown:

```
By project:
  context-studio        N recommendations (N orch, N proj)
  context-library       N recommendations (N orch, N proj)
  ...
```

### High-Priority Orchestrator Callouts

If any `high` priority orchestrator recommendations exist, highlight them as an action list with file paths:

```
## Action Required — High Priority Orchestrator Fixes

1. **`services/review_cycle.py`** — Implement automatic detection and recovery for review cycle loops exceeding 8 iterations without convergence
   → Seen in: codetoreum #42, context-studio #38
```

If the same issue appears across multiple pipeline runs, group them and note the recurrence count as a signal of urgency.

---

## Common Request Patterns

| User asks | Arguments to use |
|---|---|
| "Summarize high priority fixes for the orchestrator" | `--priority high --target orchestrator` |
| "Most impactful recommendations for context-studio" | `--project context-studio --priority high` |
| "What did this pipeline run recommend?" | `<pipeline_run_id>` |
| "All bugs found in the last week" | `--recent 7d --priority high` (then filter category=bug) |
| "What should we fix in context-library?" | `--project context-library` |
| "Anything high priority across all projects?" | `--priority high` |
