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
from datetime import datetime, timezone
from typing import Optional

from elasticsearch import Elasticsearch
from prompts.loader import default_loader

logger = logging.getLogger(__name__)

_SUMMARY_START = "---SUMMARY_START---"
_ANALYSIS_JSON_START = "---ANALYSIS_JSON_START---"
_ANALYSIS_JSON_END = "---ANALYSIS_JSON_END---"

_SKILL_PATH = "/app/.claude/skills/pipeline-investigate/SKILL.md"
_AGENT_NAME = "pipeline_analysis"

# The analysis session always runs in the orchestrator's own checkout (/app,
# read-only — filesystem_write_allowed=false in agents.yaml), because all it
# needs is the scripts and skill files there plus curl access to ES. That is a
# statement about the WORKING DIRECTORY, not about which project is being
# analysed, and conflating the two is what #152 item B fixes: this constant
# used to be passed as the run's `project`, so every project's post-run
# analysis was attributed to switchyard in observability/metrics AND contended
# for switchyard's dev_container_build lock (that lock is no longer taken at
# all for this agent — see claude/claude_integration.py's agent-identity gate).
# The real project now comes off the pipeline-run document; this is only the
# fallback for a run whose document can't be read.
_WORK_DIR = "/app"
_FALLBACK_PROJECT_NAME = "switchyard"


class PipelineRunAnalysisService:
    """Runs post-completion analysis on pipeline runs and stores results in ES."""

    def __init__(self):
        self.es = Elasticsearch(["http://elasticsearch:9200"])
        self.es_index_pattern = "pipeline-runs"
        # Track runs currently being analysed to prevent duplicate concurrent invocations.
        # Both the inline trigger and the catch-up scan share this singleton, so a run
        # added here by one caller is visible to the other.
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()

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
        with self._in_flight_lock:
            if run_id in self._in_flight:
                logger.info(f"pipeline_run_analysis: run {run_id} already in-flight — skipping duplicate")
                return
            self._in_flight.add(run_id)

        try:
            await self._run_analysis_inner(run_id, started_at)
        finally:
            with self._in_flight_lock:
                self._in_flight.discard(run_id)

    async def _run_analysis_inner(self, run_id: str, started_at: str) -> None:
        # One lookup for both questions this method has about the run document:
        # has it already been analysed, and which project does it belong to.
        source = self._fetch_run_source(run_id, ["summary", "project"])

        if self._has_summary(source):
            logger.info(f"pipeline_run_analysis: run {run_id} already has summary — skipping")
            return

        logger.info(f"pipeline_run_analysis: starting analysis for run {run_id}")

        # Clear the PREVIOUS attempt's failure marker now that a new attempt is
        # genuinely under way, so `analysis_error` always describes the attempt in
        # flight rather than a dead one. Without this, a re-analysis triggered
        # after a failure had the old error still on the document: the UI's poll
        # reads the run every 5s and stops on the first non-null `analysis`
        # payload, so it rendered the stale "Analysis failed — ...", cleared its
        # spinner and cancelled the poll seconds after the operator asked for the
        # retry -- then never learned that the retry succeeded minutes later
        # (#152 review). _update_es_document() clearing it on SUCCESS is not
        # enough; the window that matters is the one before that.
        self._merge_into_run_document(
            run_id,
            {
                "analysis_error": None,
                "analysis_attempted_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        prompt = self._build_prompt(run_id)
        context = self._build_context(run_id, (source or {}).get("project"))

        try:
            from claude.claude_integration import run_claude_code
            raw_text = await run_claude_code(prompt, context)
        except Exception as e:
            # A resource-lock timeout means the guarded operation never ran at
            # all, so it is not an analysis failure and must not be recorded as
            # one (#148/WI-3). No lock is taken on this path any more (#152
            # item B removed the dev_container_build acquisition for
            # pipeline_analysis), but recognising it keeps a future one from
            # silently becoming "analysis failed" again.
            from services.resource_lock_errors import describe_lock_timeout, is_lock_timeout_error
            if is_lock_timeout_error(e):
                logger.error(
                    f"pipeline_run_analysis: run {run_id} could not start -- "
                    f"{describe_lock_timeout(e)}. Nothing ran; re-trigger the analysis "
                    f"once the contention clears."
                )
                self._record_analysis_failure(run_id, f"lock contention: {describe_lock_timeout(e)}")
                return

            logger.error(f"pipeline_run_analysis: claude execution failed for run {run_id}: {e}", exc_info=True)
            self._record_analysis_failure(run_id, f"{type(e).__name__}: {e}")
            return

        if not raw_text:
            logger.warning(f"pipeline_run_analysis: empty output from Claude for run {run_id}")
            self._record_analysis_failure(run_id, "Claude returned no output")
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

    def _build_context(self, run_id: str, project: Optional[str] = None) -> dict:
        """Build the context dict expected by run_claude_code.

        Uses requires_docker=false so execution runs directly in the orchestrator
        container (non-Docker path in claude_integration.py). This avoids the
        Docker runner's file-based prompt delivery which requires a writeable
        workspace — analysis only needs to run curl/ES queries.

        Args:
            project: the project whose pipeline run is being analysed, read off
                that run's own ES document. NOT the working directory (that is
                always _WORK_DIR, the orchestrator's own checkout) — see the
                _FALLBACK_PROJECT_NAME comment for why the two used to be the
                same constant and what that broke.
        """
        from config.manager import config_manager
        agent_config = config_manager.get_agent(_AGENT_NAME)
        task_id = f"analysis-{run_id[:12]}-{uuid.uuid4().hex[:6]}"
        if not project:
            logger.warning(
                f"pipeline_run_analysis: no project on the pipeline-run document for "
                f"{run_id} — attributing this analysis to {_FALLBACK_PROJECT_NAME!r}"
            )
            project = _FALLBACK_PROJECT_NAME
        return {
            'agent': _AGENT_NAME,
            'task_id': task_id,
            'project': project,
            'agent_config': agent_config,
            'work_dir': _WORK_DIR,
            'mcp_servers': [],
        }

    def _fetch_run_source(self, run_id: str, fields: list) -> Optional[dict]:
        """Return the requested `_source` fields of a run's ES document, or None.

        One lookup for both the already-analysed check and the project the run
        belongs to: they are read at the same moment off the same document, and
        two searches for that would be two chances to disagree.
        """
        try:
            result = self.es.search(
                index=f"{self.es_index_pattern}-*",
                body={
                    "query": {"term": {"id": run_id}},
                    "_source": fields,
                    "size": 1,
                },
            )
            hits = result.get("hits", {}).get("hits", [])
            if hits:
                return hits[0].get("_source", {}) or {}
        except Exception as e:
            logger.warning(f"pipeline_run_analysis: could not read the run document for {run_id}: {e}")
        return None

    @staticmethod
    def _has_summary(source: Optional[dict]) -> bool:
        """Whether a run document already carries a non-empty analysis summary.

        This is what marks a run as analysed, which is why _record_analysis_failure()
        deliberately never writes `summary`.
        """
        summary = (source or {}).get("summary", "")
        return bool(summary and str(summary).strip())

    def _already_analyzed(self, run_id: str) -> bool:
        """Return True if the ES document already has a non-empty summary."""
        return self._has_summary(self._fetch_run_source(run_id, ["summary"]))

    def _build_prompt(self, run_id: str) -> str:
        skill_content = ""
        try:
            with open(_SKILL_PATH, "r") as f:
                skill_content = f.read()
        except Exception as e:
            logger.warning(f"pipeline_run_analysis: could not read skill file: {e}")

        return default_loader.workflow_template("analysis/pipeline_run").format(
            run_id=run_id,
            skill_content=skill_content,
            summary_start=_SUMMARY_START,
            analysis_json_start=_ANALYSIS_JSON_START,
            analysis_json_end=_ANALYSIS_JSON_END,
        )

    def _parse_response(self, text: str):
        """
        Parse the structured output from Claude.

        The expected format is:
            ---SUMMARY_START---
            [markdown]
            ---ANALYSIS_JSON_START---
            {json}
            ---ANALYSIS_JSON_END---

        Everything before ---SUMMARY_START--- is agent preamble (tool call narration)
        and is discarded. Falls back to the legacy behaviour (no SUMMARY_START) for
        responses that predate this delimiter.

        Returns:
            (summary, success, orch_recs, proj_recs)
        """
        json_start_idx = text.find(_ANALYSIS_JSON_START)
        json_end_idx = text.find(_ANALYSIS_JSON_END)

        if json_start_idx == -1 or json_end_idx == -1:
            logger.warning("pipeline_run_analysis: no JSON delimiters found in response")
            # Best-effort: strip preamble if SUMMARY_START is present, otherwise return raw text
            summary_start_idx = text.find(_SUMMARY_START)
            if summary_start_idx != -1:
                return text[summary_start_idx + len(_SUMMARY_START):].strip(), None, [], []
            return text.strip(), None, [], []

        # Extract summary — prefer the region after SUMMARY_START, fall back to everything
        # before the JSON block (legacy behaviour for runs analysed before this change).
        summary_start_idx = text.find(_SUMMARY_START)
        if summary_start_idx != -1:
            summary = text[summary_start_idx + len(_SUMMARY_START):json_start_idx].strip()
        else:
            logger.debug("pipeline_run_analysis: SUMMARY_START delimiter absent, using legacy extraction")
            summary = text[:json_start_idx].strip()

        json_str = text[json_start_idx + len(_ANALYSIS_JSON_START):json_end_idx].strip()

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
            # Clears any error left by a previous attempt that this one has now
            # superseded, so the field always describes the LATEST attempt.
            "analysis_error": None,
        }
        if success is not None:
            doc["outcome"] = "success" if success else "failed"

        self._merge_into_run_document(run_id, doc)

    def _record_analysis_failure(self, run_id: str, error: str) -> None:
        """
        Record on the run's own document that an analysis attempt was made and
        produced nothing.

        Every early return in _run_analysis_inner() used to leave only a server
        log line, so an operator who triggered an analysis from the UI saw the
        request accepted and then nothing at all, indefinitely (#152 item B's
        "silent drop"). Deliberately writes only `analysis_error` and never
        `summary`: _already_analyzed() keys off `summary`, so a failure recorded
        here still leaves the run re-analysable rather than permanently marked
        as done.

        Writing it is only half the fix, and the half that closes the operator's
        loop is the reader: GET /api/pipeline-run/<id>/analysis projects both
        fields and returns them as an `analysis` payload carrying `error` when
        there is no summary (services/observability_server.py), which
        web_ui/src/components/PipelineReports.jsx and
        web_ui/src/routes/pipeline-run.jsx render. Without that the endpoint
        collapsed an empty summary to `analysis: null` and the UI showed "No
        analysis available for this run." — bit for bit the symptom this exists
        to remove (#152 review). Anything added here needs a reader added there.
        """
        self._merge_into_run_document(
            run_id,
            {"analysis_error": error[:1000], "analysis_attempted_at": datetime.now(timezone.utc).isoformat()},
        )

    def _merge_into_run_document(self, run_id: str, doc: dict) -> None:
        """Merge `doc` into the pipeline-run ES document for `run_id`."""
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
