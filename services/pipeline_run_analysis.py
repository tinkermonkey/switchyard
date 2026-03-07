"""
Pipeline Run Analysis Service

Automatically investigates completed pipeline runs by invoking Claude CLI
with bash tool access to query ES, run the timeline script, and check Docker logs.
Results are stored back on the pipeline-run ES document.
"""

import json
import logging
import os
import subprocess
import threading
from typing import Optional

from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)

_ANALYSIS_JSON_START = "---ANALYSIS_JSON_START---"
_ANALYSIS_JSON_END = "---ANALYSIS_JSON_END---"

_SKILL_PATH = "/app/.claude/skills/pipeline-investigate/SKILL.md"


class PipelineRunAnalysisService:
    """Runs post-completion analysis on pipeline runs and stores results in ES."""

    def __init__(self):
        self.es = Elasticsearch(["http://elasticsearch:9200"])
        self.es_index_pattern = "pipeline-runs"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def trigger_analysis_async(self, run_id: str, started_at: str) -> None:
        """Fire-and-forget: spawn daemon thread to analyse the run."""
        t = threading.Thread(
            target=self._run_analysis_safe,
            args=(run_id, started_at),
            daemon=True,
            name=f"pipeline-analysis-{run_id[:8]}",
        )
        t.start()
        logger.info(f"pipeline_run_analysis: triggered async for run {run_id}")

    def run_analysis_for_run(self, run_id: str, started_at: str) -> None:
        """Synchronously analyse a single run (used by catch-up scan)."""
        self._run_analysis_safe(run_id, started_at)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_analysis_safe(self, run_id: str, started_at: str) -> None:
        try:
            self._run_analysis(run_id, started_at)
        except Exception as e:
            logger.error(
                f"pipeline_run_analysis: unhandled error for run {run_id}: {e}",
                exc_info=True,
            )

    def _run_analysis(self, run_id: str, started_at: str) -> None:
        if self._already_analyzed(run_id, started_at):
            logger.info(f"pipeline_run_analysis: run {run_id} already has summary — skipping")
            return

        logger.info(f"pipeline_run_analysis: starting analysis for run {run_id}")

        prompt = self._build_prompt(run_id)
        raw_text = self._run_claude(prompt)

        if not raw_text:
            logger.warning(f"pipeline_run_analysis: empty output from Claude for run {run_id}")
            return

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

    def _already_analyzed(self, run_id: str, started_at: str) -> bool:
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

    def _run_claude(self, prompt: str) -> str:
        """Invoke Claude CLI and return concatenated assistant text."""
        cmd = [
            "claude",
            "--print",
            "--output-format", "stream-json",
            "--permission-mode", "bypassPermissions",
        ]

        env = os.environ.copy()

        try:
            process = subprocess.Popen(
                cmd,
                cwd="/app",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                text=True,
                env=env,
            )

            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except Exception as e:
                process.kill()
                raise RuntimeError(f"Failed to write prompt to stdin: {e}")

            result_parts = []
            stderr_lines = []

            def read_stderr():
                try:
                    for line in iter(process.stderr.readline, ""):
                        if line.strip():
                            stderr_lines.append(line.strip())
                            logger.debug(f"pipeline_run_analysis claude stderr: {line.strip()}")
                except Exception:
                    pass

            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stderr_thread.start()

            for line in iter(process.stdout.readline, ""):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if event.get("type") == "assistant":
                        message = event.get("message", {})
                        for item in message.get("content", []):
                            if isinstance(item, dict) and item.get("type") == "text":
                                text = item.get("text", "")
                                if text:
                                    result_parts.append(text)
                except json.JSONDecodeError:
                    pass

            process.wait(timeout=300)
            stderr_thread.join(timeout=5)

            if process.returncode != 0:
                logger.warning(
                    f"pipeline_run_analysis: claude exited with code {process.returncode}. "
                    f"stderr: {' | '.join(stderr_lines[-5:])}"
                )

            return "".join(result_parts)

        except subprocess.TimeoutExpired:
            process.kill()
            logger.error("pipeline_run_analysis: claude CLI timed out after 300s")
            return ""
        except Exception as e:
            logger.error(f"pipeline_run_analysis: error running claude CLI: {e}", exc_info=True)
            return ""

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
