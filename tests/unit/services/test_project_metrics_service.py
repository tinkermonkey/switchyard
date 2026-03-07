"""
Unit tests for ProjectMetricsService.

Mocks the Elasticsearch client throughout; no live ES connection required.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_hit(source: dict) -> dict:
    return {'_source': source}


def _es_result(hits: list, total: int = None) -> dict:
    return {
        'hits': {
            'hits': hits,
            'total': {'value': total if total is not None else len(hits)},
        }
    }


def _agg_result(buckets: list) -> dict:
    return {
        'hits': {'hits': [], 'total': {'value': 0}},
        'aggregations': {
            'projects': {'buckets': buckets}
        }
    }


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def service():
    """Return a ProjectMetricsService with a fully mocked ES client."""
    with patch('services.project_metrics_service.Elasticsearch') as MockES:
        mock_es = MagicMock()
        MockES.return_value = mock_es

        # Suppress template/ILM setup errors
        mock_es.ilm.get_lifecycle.side_effect = None
        mock_es.indices.get_index_template.side_effect = None

        from services.project_metrics_service import ProjectMetricsService
        svc = ProjectMetricsService(es_hosts=['http://localhost:9200'])
        svc.es = mock_es
        return svc


# ---------------------------------------------------------------------------
# _get_projects_in_window
# ---------------------------------------------------------------------------

class TestGetProjectsInWindow:
    def test_returns_project_names_from_buckets(self, service):
        service.es.search.return_value = _agg_result([
            {'key': 'proj-a'},
            {'key': 'proj-b'},
        ])
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 2, tzinfo=timezone.utc)

        result = service._get_projects_in_window(start, end)

        assert result == ['proj-a', 'proj-b']

    def test_skips_empty_keys(self, service):
        service.es.search.return_value = _agg_result([
            {'key': 'proj-a'},
            {'key': ''},
        ])
        result = service._get_projects_in_window(
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        assert result == ['proj-a']

    def test_returns_empty_on_es_error(self, service):
        service.es.search.side_effect = Exception("ES down")
        result = service._get_projects_in_window(
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        assert result == []


# ---------------------------------------------------------------------------
# _compute_token_and_context_metrics
# ---------------------------------------------------------------------------

class TestComputeTokenAndContextMetrics:
    def test_sums_token_fields_across_tasks(self, service):
        service.es.search.return_value = _es_result([
            _make_hit({
                'pipeline_run_id': 'run-1',
                'total_direct_input': 100,
                'total_cache_read': 50,
                'total_cache_creation': 25,
                'total_output': 200,
                'initial_context': 10,
                'peak_context': 300,
                'tool_call_count': 5,
            }),
            _make_hit({
                'pipeline_run_id': 'run-1',
                'total_direct_input': 100,
                'total_cache_read': 50,
                'total_cache_creation': 25,
                'total_output': 200,
                'initial_context': 10,
                'peak_context': 400,
                'tool_call_count': 3,
            }),
        ])

        result = service._compute_token_and_context_metrics(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        assert result['pipeline_run_count'] == 1
        tokens = result['tokens']
        assert tokens['sum_direct_input'] == 200
        assert tokens['sum_cache_read'] == 100
        assert tokens['sum_output'] == 400
        ctx = result['context']
        assert ctx['peak_max_context'] == 400
        assert result['tool_calls']['total_invocations'] == 8

    def test_multiple_runs(self, service):
        service.es.search.return_value = _es_result([
            _make_hit({
                'pipeline_run_id': 'run-1',
                'total_direct_input': 1000,
                'total_cache_read': 0,
                'total_cache_creation': 0,
                'total_output': 500,
                'initial_context': 100,
                'peak_context': 1000,
                'tool_call_count': 10,
            }),
            _make_hit({
                'pipeline_run_id': 'run-2',
                'total_direct_input': 2000,
                'total_cache_read': 0,
                'total_cache_creation': 0,
                'total_output': 1000,
                'initial_context': 200,
                'peak_context': 2000,
                'tool_call_count': 20,
            }),
        ])

        result = service._compute_token_and_context_metrics(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        assert result['pipeline_run_count'] == 2
        assert result['tokens']['sum_direct_input'] == 3000
        assert result['tokens']['avg_direct_input_per_run'] == 1500.0
        assert result['context']['peak_max_context'] == 2000

    def test_empty_result(self, service):
        service.es.search.return_value = _es_result([])
        result = service._compute_token_and_context_metrics(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        assert result['pipeline_run_count'] == 0
        assert result['tokens'] == {}

    def test_es_error_returns_empty(self, service):
        service.es.search.side_effect = Exception("ES error")
        result = service._compute_token_and_context_metrics(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        assert result['pipeline_run_count'] == 0

    def test_none_fields_treated_as_zero(self, service):
        service.es.search.return_value = _es_result([
            _make_hit({
                'pipeline_run_id': 'run-1',
                'total_direct_input': None,
                'total_cache_read': None,
                'total_cache_creation': None,
                'total_output': None,
                'initial_context': None,
                'peak_context': None,
                'tool_call_count': None,
            }),
        ])
        result = service._compute_token_and_context_metrics(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        assert result['tokens']['sum_direct_input'] == 0
        assert result['tool_calls']['total_invocations'] == 0


# ---------------------------------------------------------------------------
# _compute_review_cycle_metrics
# ---------------------------------------------------------------------------

class TestComputeReviewCycleMetrics:
    def test_counts_iterations_per_run(self, service):
        service.es.search.return_value = _es_result([
            _make_hit({'event_type': 'review_cycle_started',   'pipeline_run_id': 'run-1'}),
            _make_hit({'event_type': 'review_cycle_started',   'pipeline_run_id': 'run-2'}),
            _make_hit({'event_type': 'review_cycle_iteration', 'pipeline_run_id': 'run-1'}),
            _make_hit({'event_type': 'review_cycle_iteration', 'pipeline_run_id': 'run-1'}),
            _make_hit({'event_type': 'review_cycle_iteration', 'pipeline_run_id': 'run-2'}),
            _make_hit({'event_type': 'review_cycle_escalated',  'pipeline_run_id': 'run-1'}),
        ])

        result = service._compute_review_cycle_metrics(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        assert result['total_count'] == 2       # 2 review_cycle_started events
        assert result['total_iterations'] == 3
        assert result['avg_iterations'] == 1.5  # (2+1)/2
        assert result['max_iterations'] == 2
        assert result['escalation_count'] == 1

    def test_empty_result(self, service):
        service.es.search.return_value = _es_result([])
        result = service._compute_review_cycle_metrics(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        assert result['total_count'] == 0
        assert result['escalation_count'] == 0

    def test_es_error_returns_empty(self, service):
        service.es.search.side_effect = Exception("ES error")
        result = service._compute_review_cycle_metrics(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        assert result['total_count'] == 0


# ---------------------------------------------------------------------------
# _compute_repair_cycle_metrics
# ---------------------------------------------------------------------------

class TestComputeRepairCycleMetrics:
    def test_counts_test_and_fix_cycles(self, service):
        service.es.search.return_value = _es_result([
            _make_hit({'event_type': 'repair_cycle_completed',          'pipeline_run_id': 'run-1', 'data': {}}),
            _make_hit({'event_type': 'repair_cycle_test_cycle_started', 'pipeline_run_id': 'run-1', 'data': {}}),
            _make_hit({'event_type': 'repair_cycle_test_cycle_started', 'pipeline_run_id': 'run-1', 'data': {}}),
            _make_hit({'event_type': 'repair_cycle_test_cycle_completed', 'pipeline_run_id': 'run-1', 'data': {'test_type': 'unit', 'duration_seconds': 60, 'test_cycle_iterations': 3}}),
            _make_hit({'event_type': 'repair_cycle_test_cycle_completed', 'pipeline_run_id': 'run-1', 'data': {'test_type': 'pre-commit', 'duration_seconds': 30, 'test_cycle_iterations': 1}}),
            _make_hit({'event_type': 'repair_cycle_fix_cycle_started',  'pipeline_run_id': 'run-1', 'data': {}}),
            _make_hit({'event_type': 'repair_cycle_systemic_analysis_started', 'pipeline_run_id': 'run-1', 'data': {}}),
        ])

        result = service._compute_repair_cycle_metrics(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        assert result['total_count'] == 1
        assert result['avg_test_cycles'] == 2.0
        assert result['max_test_cycles'] == 2
        assert result['avg_fix_cycles'] == 1.0
        assert result['max_fix_cycles'] == 1
        assert result['systemic_analysis_count'] == 1
        assert 'by_test_type' in result
        assert 'unit' in result['by_test_type']
        assert result['by_test_type']['unit']['count'] == 1
        assert result['by_test_type']['unit']['avg_duration_ms'] == 60000.0
        assert result['by_test_type']['unit']['avg_iterations'] == 3.0
        assert 'pre-commit' in result['by_test_type']
        assert result['by_test_type']['pre-commit']['avg_duration_ms'] == 30000.0

    def test_failed_cycles_counted(self, service):
        service.es.search.return_value = _es_result([
            _make_hit({'event_type': 'repair_cycle_completed', 'pipeline_run_id': 'run-1', 'data': {'duration_seconds': 60}}),
            _make_hit({'event_type': 'repair_cycle_failed',    'pipeline_run_id': 'run-2', 'data': {}}),
        ])

        result = service._compute_repair_cycle_metrics(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        assert result['total_count'] == 2

    def test_empty_result(self, service):
        service.es.search.return_value = _es_result([])
        result = service._compute_repair_cycle_metrics(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        assert result['total_count'] == 0
        assert result['systemic_analysis_count'] == 0


# ---------------------------------------------------------------------------
# _compute_pr_review_metrics
# ---------------------------------------------------------------------------

class TestComputePrReviewMetrics:
    def test_counts_phases_per_stage(self, service):
        service.es.search.return_value = _es_result([
            _make_hit({'event_type': 'pr_review_stage_completed',  'pipeline_run_id': 'run-1'}),
            _make_hit({'event_type': 'pr_review_phase_completed',  'pipeline_run_id': 'run-1'}),
            _make_hit({'event_type': 'pr_review_phase_completed',  'pipeline_run_id': 'run-1'}),
            _make_hit({'event_type': 'pr_review_phase_completed',  'pipeline_run_id': 'run-1'}),
            _make_hit({'event_type': 'pr_review_stage_completed',  'pipeline_run_id': 'run-2'}),
            _make_hit({'event_type': 'pr_review_phase_completed',  'pipeline_run_id': 'run-2'}),
        ])

        result = service._compute_pr_review_metrics(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        assert result['total_count'] == 2
        assert result['total_iterations'] == 4   # 3 + 1
        assert result['avg_iterations'] == 2.0   # (3+1)/2
        assert result['max_iterations'] == 3

    def test_empty_result(self, service):
        service.es.search.return_value = _es_result([])
        result = service._compute_pr_review_metrics(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        assert result['total_count'] == 0
        assert result['total_iterations'] == 0


# ---------------------------------------------------------------------------
# _compute_pipeline_outcomes
# ---------------------------------------------------------------------------

class TestComputePipelineOutcomes:
    def test_counts_success_and_failed(self, service):
        # 30 runs, 3 failed → 27 success
        hits = [_make_hit({'outcome': 'failed'})] * 3 + [_make_hit({'outcome': None})] * 27
        service.es.search.return_value = _es_result(hits)

        result = service._compute_pipeline_outcomes(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        assert result['total_count'] == 30
        assert result['failed_count'] == 3
        assert result['success_count'] == 27
        assert result['success_rate'] == pytest.approx(0.9, abs=0.001)

    def test_explicit_outcome_field_irrelevant_for_success(self, service):
        # Only failures are counted; all other completed runs are successes
        service.es.search.return_value = _es_result([
            _make_hit({'outcome': 'success'}),
            _make_hit({'outcome': 'success'}),
            _make_hit({'outcome': 'failed'}),
        ])

        result = service._compute_pipeline_outcomes(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        assert result['total_count'] == 3
        assert result['success_count'] == 2
        assert result['failed_count'] == 1

    def test_no_failures(self, service):
        service.es.search.return_value = _es_result([
            _make_hit({'outcome': 'success'}),
            _make_hit({'outcome': 'success'}),
        ])

        result = service._compute_pipeline_outcomes(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        assert result['success_rate'] == 1.0
        assert result['failed_count'] == 0
        assert result['success_count'] == 2

    def test_empty_result(self, service):
        service.es.search.return_value = _es_result([])
        result = service._compute_pipeline_outcomes(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        assert result['success_count'] == 0
        assert result['success_rate'] == 0.0

    def test_es_error_returns_empty(self, service):
        service.es.search.side_effect = Exception("ES error")
        result = service._compute_pipeline_outcomes(
            'proj', datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        assert result['success_count'] == 0


# ---------------------------------------------------------------------------
# _upsert_project_metrics
# ---------------------------------------------------------------------------

class TestUpsertProjectMetrics:
    def test_upserts_with_correct_doc_id(self, service):
        doc = {
            'project': 'my-project',
            'day_bucket': '2026-01-15',
            'pipeline_run_count': 3,
        }
        service._upsert_project_metrics(doc)

        service.es.update.assert_called_once()
        call_kwargs = service.es.update.call_args
        assert call_kwargs.kwargs['id'] == 'my-project_2026-01-15'
        assert call_kwargs.kwargs['index'] == 'project-metrics-2026.01'
        body = call_kwargs.kwargs['body']
        assert body['doc_as_upsert'] is True
        assert body['doc'] is doc

    def test_upsert_error_does_not_raise(self, service):
        service.es.update.side_effect = Exception("write error")
        doc = {'project': 'proj', 'day_bucket': '2026-01-15', 'pipeline_run_count': 1}
        # Should not raise
        service._upsert_project_metrics(doc)


# ---------------------------------------------------------------------------
# run_metrics_job (integration-level, mocking internal methods)
# ---------------------------------------------------------------------------

class TestRunMetricsJob:
    @pytest.mark.asyncio
    async def test_skips_upsert_when_no_runs(self, service):
        service._get_projects_in_window = MagicMock(return_value=['proj-a'])
        service._build_project_doc = MagicMock(return_value={
            'project': 'proj-a',
            'day_bucket': '2026-01-01',
            'pipeline_run_count': 0,
        })
        service._upsert_project_metrics = MagicMock()

        await service.run_metrics_job(lookback_days=1)

        service._upsert_project_metrics.assert_not_called()

    @pytest.mark.asyncio
    async def test_upserts_when_runs_found(self, service):
        service._get_projects_in_window = MagicMock(return_value=['proj-a'])
        service._build_project_doc = MagicMock(return_value={
            'project': 'proj-a',
            'day_bucket': '2026-01-01',
            'pipeline_run_count': 5,
        })
        service._upsert_project_metrics = MagicMock()

        await service.run_metrics_job(lookback_days=1)

        assert service._upsert_project_metrics.call_count >= 1

    @pytest.mark.asyncio
    async def test_handles_empty_project_list(self, service):
        service._get_projects_in_window = MagicMock(return_value=[])
        service._build_project_doc = MagicMock()
        service._upsert_project_metrics = MagicMock()

        await service.run_metrics_job(lookback_days=1)

        service._build_project_doc.assert_not_called()
        service._upsert_project_metrics.assert_not_called()
