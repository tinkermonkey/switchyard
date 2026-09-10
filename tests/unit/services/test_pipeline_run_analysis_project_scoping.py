"""
Unit tests for PipelineRunAnalysisService's project attribution and its
handling of a run whose analysis produced nothing (issue #152 item B, from
#140 item 20).

Two separate defects lived in the same constant:

  1. `project` was hardcoded to "switchyard" for EVERY project's post-run
     analysis. That constant is really the working DIRECTORY (/app, the
     orchestrator's own read-only checkout, which is genuinely where the
     session runs) -- conflating it with the analysed run's project mislabelled
     every other project's analysis in observability/metrics, and made it
     contend for switchyard's dev_container_build lock. Fixing only the lock
     gate would have left every project's analysis still labelled switchyard's.

  2. Every failure path returned after a log line and nothing else, so a run
     whose analysis died left no trace on its own document -- an operator who
     triggered it from the UI saw the request accepted and then nothing,
     indefinitely.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch

from services.pipeline_run_analysis import (
    PipelineRunAnalysisService,
    _FALLBACK_PROJECT_NAME,
    _WORK_DIR,
)


def _service():
    with patch('services.pipeline_run_analysis.Elasticsearch'):
        service = PipelineRunAnalysisService()
    service.es = MagicMock()
    return service


def _es_hit(source):
    return {"hits": {"hits": [{"_id": "doc-1", "_index": "pipeline-runs-2026.01.01", "_source": source}]}}


class TestProjectAttribution:

    def test_context_uses_the_runs_own_project(self):
        """THE regression: not the orchestrator's own name."""
        service = _service()
        context = service._build_context("run-1", "context-studio")

        assert context['project'] == "context-studio"
        # The working directory is unchanged -- it is a separate concern, and the
        # analysis genuinely does run in the orchestrator's checkout.
        assert context['work_dir'] == _WORK_DIR

    def test_falls_back_only_when_the_document_has_no_project(self):
        service = _service()
        assert service._build_context("run-1", None)['project'] == _FALLBACK_PROJECT_NAME
        assert service._build_context("run-1", "")['project'] == _FALLBACK_PROJECT_NAME

    @pytest.mark.asyncio
    async def test_the_project_read_off_es_reaches_run_claude_code(self):
        service = _service()
        service.es.search.return_value = _es_hit({"summary": "", "project": "context-studio"})

        with patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "no delimiters here"
            await service._run_analysis_inner("run-1", "2026-01-01T00:00:00")

        assert mock_run.await_args.args[1]['project'] == "context-studio"


class TestFailuresAreRecordedNotDropped:

    @pytest.mark.asyncio
    async def test_a_claude_failure_is_recorded_on_the_run_document(self):
        service = _service()
        service.es.search.return_value = _es_hit({"summary": "", "project": "context-studio"})

        with patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = RuntimeError("claude exploded")
            await service._run_analysis_inner("run-1", "2026-01-01T00:00:00")

        doc = service.es.update.call_args.kwargs['body']['doc']
        assert "claude exploded" in doc['analysis_error']
        # Never `summary`: _already_analyzed() keys off that, so writing it would
        # permanently mark the run as analysed and block any retry.
        assert 'summary' not in doc

    @pytest.mark.asyncio
    async def test_empty_output_is_recorded_on_the_run_document(self):
        service = _service()
        service.es.search.return_value = _es_hit({"summary": "", "project": "p"})

        with patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ""
            await service._run_analysis_inner("run-1", "2026-01-01T00:00:00")

        doc = service.es.update.call_args.kwargs['body']['doc']
        assert doc['analysis_error'] == "Claude returned no output"
        assert 'summary' not in doc

    @pytest.mark.asyncio
    async def test_a_lock_timeout_is_reported_as_contention_not_a_failure(self):
        """#148/WI-3: a lock timeout means the guarded operation never ran, so
        it must not be recorded as an analysis failure of the run itself. No
        lock is taken on this path any more, but the recognition stays so a
        future one can't silently become 'analysis failed' again."""
        from services.dev_container_build_lock import DevContainerBuildLockTimeoutError

        service = _service()
        service.es.search.return_value = _es_hit({"summary": "", "project": "p"})

        with patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = DevContainerBuildLockTimeoutError("busy")
            await service._run_analysis_inner("run-1", "2026-01-01T00:00:00")

        doc = service.es.update.call_args.kwargs['body']['doc']
        assert doc['analysis_error'].startswith("lock contention:")
        assert 'summary' not in doc

    @pytest.mark.asyncio
    async def test_a_successful_analysis_clears_a_previous_error(self):
        service = _service()
        service.es.search.return_value = _es_hit(
            {"summary": "", "project": "p", "analysis_error": "an earlier attempt failed"}
        )

        raw = (
            "preamble\n---SUMMARY_START---\nAll good\n"
            "---ANALYSIS_JSON_START---\n"
            '{"success": true, "orchestratorRecommendations": [], "projectRecommendations": []}\n'
            "---ANALYSIS_JSON_END---\n"
        )
        with patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock) as mock_run:
            mock_run.return_value = raw
            await service._run_analysis_inner("run-1", "2026-01-01T00:00:00")

        doc = service.es.update.call_args.kwargs['body']['doc']
        assert doc['summary'] == "All good"
        assert doc['analysis_error'] is None
        assert doc['outcome'] == "success"

    @pytest.mark.asyncio
    async def test_an_already_summarised_run_is_still_skipped(self):
        """The single ES lookup that now also carries `project` must not have
        changed the already-analysed guard."""
        service = _service()
        service.es.search.return_value = _es_hit({"summary": "done already", "project": "p"})

        with patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock) as mock_run:
            await service._run_analysis_inner("run-1", "2026-01-01T00:00:00")

        mock_run.assert_not_awaited()
        service.es.update.assert_not_called()
