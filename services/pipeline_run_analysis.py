"""
Pipeline Run Analysis Service

Automatically investigates completed pipeline runs by invoking Claude via the
standard Docker execution path (run_claude_code / docker_runner). Claude gets
bash tool access to query ES and investigate events dynamically.
Results are stored back on the pipeline-run ES document.
"""

import asyncio
import json
import logging
import threading
import uuid
from typing import Optional

from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)

_ANALYSIS_JSON_START = "---ANALYSIS_JSON_START---"
_ANALYSIS_JSON_END = "---ANALYSIS_JSON_END---"

_SKILL_PATH = "/app/.claude/skills/pipeline-investigate/SKILL.md"
_AGENT_NAME = "pipeline_analysis"
# Mount the orchestrator codebase read-only — gives Claude access to scripts and skill files.
# filesystem_write_allowed=false in agents.yaml ensures Docker runner mounts this as :ro.
_PROJECT_NAME = "clauditoreum"


class PipelineRunAnalysisService:
    """Runs post-completion analysis on pipeline runs and stores results in ES."""

    def __init__(self):
        self.es = Elasticsearch(["http://elasticsearch:9200"])
        self.es_index_pattern = "pipeline-runs"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def trigger_analysis_async(self, run_id: str, started_at: str) -> None:
        """Fire-and-forget: spawn daemon thread running a fresh event loop for the analysis."""
        t = threading.Thread(
            target=self._run_in_new_loop,
            args=(run_id, started_at),
            daemon=True,
            name=f"pipeline-analysis-{run_id[:8]}",
        )
        t.start()
        logger.info(f"pipeline_run_analysis: triggered async for run {run_id}")

    def run_analysis_for_run(self, run_id: str, started_at: str) -> None:
        """Synchronously analyse a single run (used by catch-up scan via thread pool)."""
        self._run_in_new_loop(run_id, started_at)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_in_new_loop(self, run_id: str, started_at: str) -> None:
        """Run the async analysis in a fresh event loop (called from daemon thread)."""
        try:
            asyncio.run(self._run_analysis(run_id, started_at))
        except Exception as e:
            logger.error(
                f"pipeline_run_analysis: unhandled error for run {run_id}: {e}",
                exc_info=True,
            )

    async def _run_analysis(self, run_id: str, started_at: str) -> None:
        if self._already_analyzed(run_id):
            logger.info(f"pipeline_run_analysis: run {run_id} already has summary — skipping")
            return

        logger.info(f"pipeline_run_analysis: starting analysis for run {run_id}")

        prompt = self._build_prompt(run_id)
        context = self._build_context(run_id)

        try:
            from claude.claude_integration import run_claude_code
            raw_text = await run_claude_code(prompt, context)
        except Exception as e:
            logger.error(f"pipeline_run_analysis: claude execution failed for run {run_id}: {e}", exc_info=True)
            return

        if not raw_text:
            logger.warning(f"pipeline_run_analysis: empty output from Claude for run {run_id}")
            return

        # run_claude_code may return a dict (docker path) or a string (non-docker path)
        if isinstance(raw_text, dict):
            raw_text = raw_text.get('result', '')

        summary, success, orch_recs, proj_recs = self._parse_response(raw_text)

        self._update_es_document(
            run_id=run_id,
            started_at=started_at,
            summary=summary,
            success=success,
            orch_recs=orch_recs,
            proj_recs=proj_recs,
        )
        logger.info(
            f"pipeline_run_analysis: completed for run {run_id} "
            f"(success={success}, orch_recs={len(orch_recs)}, proj_recs={len(proj_recs)})"
        )

    def _build_context(self, run_id: str) -> dict:
        """Build the context dict expected by run_claude_code / docker_runner."""
        from config.manager import config_manager
        agent_config = config_manager.get_agent(_AGENT_NAME)
        task_id = f"analysis-{run_id[:12]}-{uuid.uuid4().hex[:6]}"
        return {
            'agent': _AGENT_NAME,
            'task_id': task_id,
            'project': _PROJECT_NAME,
            'agent_config': agent_config,
            'use_docker': True,
            'mcp_servers': [],
        }

    def _already_analyzed(self, run_id: str) -> bool:
        """Return True if the ES document already has a non-empty summary."""
        try:
            result = self.es.search(
                index=f"{self.es_index_pattern}-*",
                body={
                    "query": {"term": {"id": run_id}},
                    "_source": ["summary"],
                    "size": 1,
                },
            )
            hits = result.get("hits", {}).get("hits", [])
            if hits:
                summary = hits[0].get("_source", {}).get("summary", "")
                return bool(summary and summary.strip())
        except Exception as e:
            logger.warning(f"pipeline_run_analysis: could not check existing summary for {run_id}: {e}")
        return False

    def _build_prompt(self, run_id: str) -> str:
        skill_content = ""
        try:
            with open(_SKILL_PATH, "r") as f:
                skill_content = f.read()
        except Exception as e:
            logger.warning(f"pipeline_run_analysis: could not read skill file: {e}")

        return f"""You are performing an automated post-completion analysis of a pipeline run.

Pipeline Run ID to investigate: {run_id}

{skill_content}

## Your Task

Follow the investigation steps above (Steps 1–7) for pipeline run `{run_id}`.

Note: This analysis runs after pipeline completion. Agent containers are already removed (--rm),
so Step 2 (docker-compose exec timeline script) and Step 5 (docker logs) will not be available.
Focus on the Elasticsearch queries in Steps 1, 3, 4, and 6, then synthesize in Step 7.

After completing the investigation, output your findings in the following exact format:

[Human-readable markdown summary — include a timeline table and root cause analysis]

{_ANALYSIS_JSON_START}
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
{_ANALYSIS_JSON_END}

Set "success" to false if the pipeline run failed or produced no useful output.
Omit "filePath" from projectRecommendations entries (it is optional on orchestrator entries only).
If there is nothing to recommend, use empty arrays.
"""

    def _parse_response(self, text: str):
        """
        Parse the structured output from Claude.

        Returns:
            (summary, success, orch_recs, proj_recs)
        """
        start_idx = text.find(_ANALYSIS_JSON_START)
        end_idx = text.find(_ANALYSIS_JSON_END)

        if start_idx == -1 or end_idx == -1:
            logger.warning("pipeline_run_analysis: no JSON delimiters found in response")
            return text.strip(), None, [], []

        summary = text[:start_idx].strip()
        json_str = text[start_idx + len(_ANALYSIS_JSON_START):end_idx].strip()

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(f"pipeline_run_analysis: failed to parse JSON block: {e}")
            return summary, None, [], []

        success = data.get("success")
        orch_recs = data.get("orchestratorRecommendations", [])
        proj_recs = data.get("projectRecommendations", [])

        return summary, success, orch_recs, proj_recs

    def _update_es_document(
        self,
        run_id: str,
        started_at: str,
        summary: str,
        success: Optional[bool],
        orch_recs: list,
        proj_recs: list,
    ) -> None:
        """Update the pipeline-run ES document with analysis results."""
        doc = {
            "summary": summary,
            "orchestratorRecommendations": orch_recs,
            "projectRecommendations": proj_recs,
        }
        if success is not None:
            doc["outcome"] = "success" if success else "failed"

        try:
            # Find the actual document ID (may differ from run_id)
            search_result = self.es.search(
                index=f"{self.es_index_pattern}-*",
                body={"query": {"term": {"id": run_id}}, "size": 1},
            )
            hits = search_result.get("hits", {}).get("hits", [])
            if not hits:
                logger.warning(f"pipeline_run_analysis: no ES doc found for run {run_id}")
                return

            es_id = hits[0]["_id"]
            es_index = hits[0]["_index"]

            self.es.update(index=es_index, id=es_id, body={"doc": doc})
            logger.info(f"pipeline_run_analysis: updated ES doc {es_id} in {es_index}")

        except Exception as e:
            logger.error(
                f"pipeline_run_analysis: failed to update ES document for run {run_id}: {e}",
                exc_info=True,
            )


# Module-level singleton
_pipeline_run_analysis_service: Optional[PipelineRunAnalysisService] = None


def get_pipeline_run_analysis_service() -> PipelineRunAnalysisService:
    """Get or create the global PipelineRunAnalysisService instance."""
    global _pipeline_run_analysis_service
    if _pipeline_run_analysis_service is None:
        _pipeline_run_analysis_service = PipelineRunAnalysisService()
    return _pipeline_run_analysis_service
