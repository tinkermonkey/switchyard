---
invoked_by: services/pipeline_run_analysis.py — _build_prompt() via default_loader.workflow_template("analysis/pipeline_run")
  Called as: loader.workflow_template("analysis/pipeline_run").format(
    run_id=run_id, skill_content=skill_content,
    summary_start=_SUMMARY_START, analysis_json_start=_ANALYSIS_JSON_START, analysis_json_end=_ANALYSIS_JSON_END)
variables:
  run_id: Pipeline run ID to investigate
  skill_content: Full text of the pipeline-investigate skill file
  summary_start: The ---SUMMARY_START--- sentinel string
  analysis_json_start: The ---ANALYSIS_JSON_START--- sentinel string
  analysis_json_end: The ---ANALYSIS_JSON_END--- sentinel string
notes: >
  The JSON schema block uses {{ }} for literal braces (escaped for str.format()).
  The sentinel variables are kept as Python constants in the service so that _parse_response()
  and the content file stay in sync without hard-coding the delimiter strings in two places.
---

You are performing an automated post-completion analysis of a pipeline run.

Pipeline Run ID to investigate: {run_id}

{skill_content}

## Your Task

Follow the investigation steps above (Steps 1–7) for pipeline run `{run_id}`.

Note: This analysis runs after pipeline completion. Agent containers are already removed (--rm),
so Step 2 (docker-compose exec timeline script) and Step 5 (docker logs) will not be available.
Focus on the Elasticsearch queries in Steps 1, 3, 4, and 6, then synthesize in Step 7.

After completing the investigation, output your findings in the following exact format.
Do not include any text before {summary_start} — that delimiter must be the very first thing you output in your final answer.

{summary_start}
[Human-readable markdown summary — include a timeline table and root cause analysis]

{analysis_json_start}
{{
  "success": true,
  "successExplanation": "one-sentence explanation of the overall outcome",
  "orchestratorRecommendations": [
    {{
      "priority": "high|medium|low",
      "category": "bug|improvement|performance|configuration",
      "description": "...",
      "filePath": "optional/path.py"
    }}
  ],
  "projectRecommendations": [
    {{
      "priority": "high|medium|low",
      "category": "bug|improvement|performance|configuration",
      "description": "..."
    }}
  ]
}}
{analysis_json_end}

Set "success" to false if the pipeline run failed or produced no useful output.
Omit "filePath" from projectRecommendations entries (it is optional on orchestrator entries only).
If there is nothing to recommend, use empty arrays.
