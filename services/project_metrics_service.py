"""
Project Metrics Service

Computes daily per-project rollup metrics covering:
- Token utilization (from agent-execution-summaries)
- Context peaks and initial context
- Tool call invocations
- Review cycles
- Repair cycles
- PR review cycles
- Pipeline outcomes

Writes one document per project per day to project-metrics-YYYY.MM indices.
Upserts are idempotent, keyed on project + day_bucket.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional, Tuple

from elasticsearch import Elasticsearch, NotFoundError

logger = logging.getLogger(__name__)

PROJECT_METRICS_INDEX_PREFIX = 'project-metrics'
PROJECT_METRICS_ILM_POLICY = 'project-metrics-ilm-policy'
PROJECT_METRICS_TEMPLATE = 'project-metrics-template'


def _index_name(dt: datetime) -> str:
    return f"{PROJECT_METRICS_INDEX_PREFIX}-{dt.strftime('%Y.%m')}"


class ProjectMetricsService:
    """
    Computes and stores daily per-project rollup metrics.

    One document per project per day, upserted with doc ID {project}_{day_bucket}.
    Re-running for the same day is idempotent.

    Data sources:
      agent-execution-summaries-*  → token, context, tool call metrics
      decision-events-*            → review cycles, repair cycles, PR review cycles
      pipeline-runs-*              → pipeline outcomes
    """

    def __init__(self, es_hosts: List[str] = None):
        if es_hosts is None:
            es_hosts = ['http://elasticsearch:9200']
        self.es = Elasticsearch(es_hosts)
        self._ensure_index_template()

    def _ensure_index_template(self):
        """Create ILM policy and index template for project-metrics if they don't exist."""
        # ILM policy: 30-day retention
        try:
            self.es.ilm.get_lifecycle(name=PROJECT_METRICS_ILM_POLICY)
        except NotFoundError:
            try:
                self.es.ilm.put_lifecycle(
                    name=PROJECT_METRICS_ILM_POLICY,
                    body={
                        "policy": {
                            "phases": {
                                "hot": {
                                    "min_age": "0ms",
                                    "actions": {"set_priority": {"priority": 100}},
                                },
                                "warm": {
                                    "min_age": "15d",
                                    "actions": {"set_priority": {"priority": 50}},
                                },
                                "delete": {
                                    "min_age": "30d",
                                    "actions": {"delete": {}},
                                },
                            }
                        }
                    },
                )
                logger.info(f"Created ILM policy: {PROJECT_METRICS_ILM_POLICY}")
            except Exception as e:
                logger.warning(f"Could not create ILM policy {PROJECT_METRICS_ILM_POLICY}: {e}")
        except Exception as e:
            logger.warning(f"Could not check ILM policy {PROJECT_METRICS_ILM_POLICY}: {e}")

        # Index template
        properties = {
            "project":            {"type": "keyword"},
            "day_bucket":         {"type": "date"},
            "pipeline_run_count": {"type": "integer"},
            "computed_at":        {"type": "date"},
            "tokens":             {"type": "object", "enabled": True},
            "context":            {"type": "object", "enabled": True},
            "tool_calls":         {"type": "object", "enabled": True},
            "review_cycles":      {"type": "object", "enabled": True},
            "repair_cycles":      {"type": "object", "enabled": True},
            "pr_review_cycles":   {"type": "object", "enabled": True},
            "pipeline_outcomes":  {"type": "object", "enabled": True},
            "agent_breakdown":    {"type": "object", "enabled": False},
            "tool_breakdown":     {"type": "object", "enabled": False},
        }
        try:
            self.es.indices.get_index_template(name=PROJECT_METRICS_TEMPLATE)
        except NotFoundError:
            try:
                self.es.indices.put_index_template(
                    name=PROJECT_METRICS_TEMPLATE,
                    body={
                        "index_patterns": [f"{PROJECT_METRICS_INDEX_PREFIX}-*"],
                        "priority": 100,
                        "template": {
                            "settings": {
                                "number_of_shards": 1,
                                "number_of_replicas": 0,
                                "lifecycle": {"name": PROJECT_METRICS_ILM_POLICY},
                            },
                            "mappings": {"properties": properties},
                        },
                    },
                )
                logger.info(f"Created index template: {PROJECT_METRICS_TEMPLATE}")
            except Exception as e:
                logger.warning(f"Could not create index template {PROJECT_METRICS_TEMPLATE}: {e}")
        except Exception as e:
            logger.warning(f"Could not check index template {PROJECT_METRICS_TEMPLATE}: {e}")

    async def run_metrics_job(self, lookback_days: int = 1):
        """
        Compute project metrics for each day in the lookback window.

        Each day bucket is upserted independently so re-runs are idempotent.
        Skips upsert if no pipeline runs were found for that project+day.
        """
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        days: List[Tuple[datetime, datetime]] = []
        for i in range(lookback_days, 0, -1):
            day_start = today_start - timedelta(days=i)
            days.append((day_start, day_start + timedelta(days=1)))
        days.append((today_start, now))

        logger.info(f"Starting project metrics job: lookback={lookback_days}d, {len(days)} buckets")
        loop = asyncio.get_running_loop()
        total_docs = 0

        for day_start, day_end in days:
            day_bucket = day_start.strftime('%Y-%m-%d')
            try:
                projects = await loop.run_in_executor(
                    None, self._get_projects_in_window, day_start, day_end
                )
                for project in projects:
                    try:
                        doc = await loop.run_in_executor(
                            None, self._build_project_doc, project, day_start, day_end, day_bucket
                        )
                        if doc and doc.get('pipeline_run_count', 0) > 0:
                            self._upsert_project_metrics(doc)
                            total_docs += 1
                    except Exception as e:
                        logger.error(
                            f"Error computing metrics for {project}/{day_bucket}: {e}",
                            exc_info=True,
                        )
            except Exception as e:
                logger.error(f"Error getting projects for {day_bucket}: {e}", exc_info=True)

        logger.info(f"Project metrics job complete: {total_docs} docs written/updated")

    # -------------------------------------------------------------------------
    # Project discovery
    # -------------------------------------------------------------------------

    def _get_projects_in_window(self, start: datetime, end: datetime) -> List[str]:
        """Discover distinct projects active in the given window via agent-execution-summaries."""
        try:
            result = self.es.search(
                index='agent-execution-summaries-*',
                body={
                    'size': 0,
                    'query': {
                        'bool': {
                            'must': [
                                {'range': {'ended_at': {
                                    'gte': start.isoformat(),
                                    'lt': end.isoformat(),
                                }}}
                            ],
                            'must_not': [{'term': {'project': ''}}],
                        }
                    },
                    'aggs': {
                        'projects': {'terms': {'field': 'project', 'size': 500}}
                    },
                },
            )
            buckets = result.get('aggregations', {}).get('projects', {}).get('buckets', [])
            return [b['key'] for b in buckets if b.get('key')]
        except Exception as e:
            logger.error(f"Error discovering projects in window {start.date()}: {e}")
            return []

    # -------------------------------------------------------------------------
    # Document builder
    # -------------------------------------------------------------------------

    def _build_project_doc(
        self, project: str, start: datetime, end: datetime, day_bucket: str
    ) -> Optional[Dict[str, Any]]:
        """Build a complete project metrics document for one project+day."""
        from monitoring.timestamp_utils import utc_isoformat

        token_ctx = self._compute_token_and_context_metrics(project, start, end)
        review = self._compute_review_cycle_metrics(project, start, end)
        repair = self._compute_repair_cycle_metrics(project, start, end)
        pr_review = self._compute_pr_review_metrics(project, start, end)
        outcomes = self._compute_pipeline_outcomes(project, start, end)

        agent_breakdown = token_ctx.get('agent_breakdown', {})
        tool_breakdown = self._compute_tool_breakdown(project, start, end)

        return {
            'project': project,
            'day_bucket': day_bucket,
            'pipeline_run_count': token_ctx.get('pipeline_run_count', 0),
            'computed_at': utc_isoformat(),
            'tokens': token_ctx.get('tokens', {}),
            'context': token_ctx.get('context', {}),
            'tool_calls': token_ctx.get('tool_calls', {}),
            'review_cycles': review,
            'repair_cycles': repair,
            'pr_review_cycles': pr_review,
            'pipeline_outcomes': outcomes,
            'agent_breakdown': agent_breakdown,
            'tool_breakdown': tool_breakdown,
        }

    # -------------------------------------------------------------------------
    # Token, context, and tool call metrics
    # -------------------------------------------------------------------------

    def _compute_token_and_context_metrics(
        self, project: str, start: datetime, end: datetime
    ) -> Dict[str, Any]:
        """
        Aggregate token, context, and tool call metrics from agent-execution-summaries.

        Groups by pipeline_run_id for per-run averages.
        Uses tool_call_count (indexed integer) for tool invocations — this field
        is the pre-summed count of all tool invocations per task.
        """
        empty = {'pipeline_run_count': 0, 'tokens': {}, 'context': {}, 'tool_calls': {}, 'agent_breakdown': {}}
        try:
            result = self.es.search(
                index='agent-execution-summaries-*',
                body={
                    'size': 10000,
                    'query': {
                        'bool': {
                            'must': [
                                {'term': {'project': project}},
                                {'range': {'ended_at': {
                                    'gte': start.isoformat(),
                                    'lt': end.isoformat(),
                                }}},
                            ]
                        }
                    },
                    '_source': [
                        'pipeline_run_id', 'agent_name', 'total_direct_input', 'total_cache_read',
                        'total_cache_creation', 'total_output', 'initial_context',
                        'peak_context', 'tool_call_count',
                    ],
                },
            )
        except Exception as e:
            logger.error(f"Error querying execution summaries for {project}: {e}")
            return empty

        hits = result['hits']['hits']
        total = result['hits'].get('total', {})
        if isinstance(total, dict):
            total = total.get('value', len(hits))
        if total > len(hits):
            logger.warning(
                f"agent-execution-summaries query returned {len(hits)} of {total} docs "
                f"for {project} in window {start.date()} — metrics may be incomplete"
            )
        if not hits:
            return empty

        # Accumulate per pipeline_run_id so we can compute per-run averages
        runs: Dict[str, Dict[str, Any]] = {}
        agent_breakdown: Dict[str, Dict[str, Any]] = {}
        for hit in hits:
            src = hit['_source']
            run_id = src.get('pipeline_run_id') or '__no_run__'
            if run_id not in runs:
                runs[run_id] = {
                    'sum_direct_input': 0,
                    'sum_cache_read': 0,
                    'sum_cache_creation': 0,
                    'sum_output': 0,
                    'sum_initial_input': 0,
                    'sum_tool_calls': 0,
                    'peak_context': 0,
                }
            r = runs[run_id]
            r['sum_direct_input'] += src.get('total_direct_input') or 0
            r['sum_cache_read'] += src.get('total_cache_read') or 0
            r['sum_cache_creation'] += src.get('total_cache_creation') or 0
            r['sum_output'] += src.get('total_output') or 0
            r['sum_initial_input'] += src.get('initial_context') or 0
            r['sum_tool_calls'] += src.get('tool_call_count') or 0
            peak = src.get('peak_context') or 0
            if peak > r['peak_context']:
                r['peak_context'] = peak

            agent = src.get('agent_name') or 'unknown'
            if agent not in agent_breakdown:
                agent_breakdown[agent] = {
                    'task_count': 0,
                    'sum_direct_input': 0,
                    'sum_cache_read': 0,
                    'sum_cache_creation': 0,
                    'sum_output': 0,
                }
            ab = agent_breakdown[agent]
            ab['task_count'] += 1
            ab['sum_direct_input'] += src.get('total_direct_input') or 0
            ab['sum_cache_read'] += src.get('total_cache_read') or 0
            ab['sum_cache_creation'] += src.get('total_cache_creation') or 0
            ab['sum_output'] += src.get('total_output') or 0

        # Count distinct pipeline runs (exclude the sentinel no-run bucket)
        pipeline_run_count = len([rid for rid in runs if rid != '__no_run__'])
        if pipeline_run_count == 0:
            pipeline_run_count = 1  # Some tasks ran, just without a run ID

        n = len(runs)
        total_direct_input = sum(r['sum_direct_input'] for r in runs.values())
        total_cache_read = sum(r['sum_cache_read'] for r in runs.values())
        total_cache_creation = sum(r['sum_cache_creation'] for r in runs.values())
        total_output = sum(r['sum_output'] for r in runs.values())
        total_initial_input = sum(r['sum_initial_input'] for r in runs.values())
        total_tool_calls = sum(r['sum_tool_calls'] for r in runs.values())
        sum_max_context = sum(r['peak_context'] for r in runs.values())
        peak_max_context = max(r['peak_context'] for r in runs.values())

        return {
            'pipeline_run_count': pipeline_run_count,
            'agent_breakdown': agent_breakdown,
            'tokens': {
                'sum_direct_input': total_direct_input,
                'sum_cache_read': total_cache_read,
                'sum_cache_creation': total_cache_creation,
                'sum_output': total_output,
                'avg_direct_input_per_run': round(total_direct_input / n, 2) if n else 0.0,
                'avg_cache_read_per_run': round(total_cache_read / n, 2) if n else 0.0,
                'avg_cache_creation_per_run': round(total_cache_creation / n, 2) if n else 0.0,
                'avg_output_per_run': round(total_output / n, 2) if n else 0.0,
            },
            'context': {
                'sum_max_context': sum_max_context,
                'peak_max_context': peak_max_context,
                'avg_max_context_per_run': round(sum_max_context / n, 2) if n else 0.0,
                'sum_initial_input': total_initial_input,
                'avg_initial_input_per_run': round(total_initial_input / n, 2) if n else 0.0,
            },
            'tool_calls': {
                'total_invocations': total_tool_calls,
                'avg_invocations_per_run': round(total_tool_calls / n, 2) if n else 0.0,
            },
        }

    # -------------------------------------------------------------------------
    # Tool breakdown
    # -------------------------------------------------------------------------

    def _compute_tool_breakdown(
        self, project: str, start: datetime, end: datetime
    ) -> Dict[str, Any]:
        """
        Compute per-tool breakdown for a project by aggregating tool_breakdown fields
        from agent-execution-summaries-*, which are now written per execution and are
        scoped exactly to the project.
        """
        try:
            result = self.es.search(
                index='agent-execution-summaries-*',
                body={
                    'size': 10000,
                    'query': {
                        'bool': {
                            'must': [
                                {'term': {'project': project}},
                                {'range': {'ended_at': {
                                    'gte': start.isoformat(),
                                    'lt': end.isoformat(),
                                }}},
                            ]
                        }
                    },
                    '_source': ['tool_breakdown'],
                },
            )
        except Exception as e:
            if 'index_not_found' not in str(e).lower():
                logger.warning(f"Error fetching tool breakdown for {project}: {e}")
            return {}

        hits = result['hits']['hits']
        total = result['hits'].get('total', {})
        if isinstance(total, dict):
            total = total.get('value', len(hits))
        if total > len(hits):
            logger.warning(
                f"tool_breakdown query returned {len(hits)} of {total} docs "
                f"for {project} in window {start.date()} — tool breakdown may be incomplete"
            )

        tool_breakdown: Dict[str, Any] = {}
        for hit in hits:
            for tool_name, tb in (hit['_source'].get('tool_breakdown') or {}).items():
                if not isinstance(tb, dict):
                    continue
                if tool_name not in tool_breakdown:
                    tool_breakdown[tool_name] = {'task_count': 0, 'sum_output': 0, 'sum_context_growth': 0}
                entry = tool_breakdown[tool_name]
                entry['task_count'] += tb.get('task_count', 0) or 0
                entry['sum_output'] += tb.get('sum_output', 0) or 0
                entry['sum_context_growth'] += tb.get('sum_context_growth', 0) or 0

        return tool_breakdown

    # -------------------------------------------------------------------------
    # Review cycle metrics
    # -------------------------------------------------------------------------

    def _compute_review_cycle_metrics(
        self, project: str, start: datetime, end: datetime
    ) -> Dict[str, Any]:
        """
        Aggregate review cycle metrics from decision-events.

        Counts REVIEW_CYCLE_ITERATION events per pipeline_run_id as iterations.
        Counts REVIEW_CYCLE_ESCALATED events as escalations.
        """
        empty = {
            'total_count': 0,
            'total_iterations': 0,
            'avg_iterations': 0.0,
            'max_iterations': 0,
            'escalation_count': 0,
        }
        try:
            result = self.es.search(
                index='decision-events-*',
                body={
                    'size': 10000,
                    'query': {
                        'bool': {
                            'must': [
                                {'term': {'project': project}},
                                {'range': {'timestamp': {
                                    'gte': start.isoformat(),
                                    'lt': end.isoformat(),
                                }}},
                                {'bool': {
                                    'should': [{'prefix': {'event_type': 'review_cycle_'}}],
                                    'minimum_should_match': 1,
                                }},
                            ]
                        }
                    },
                    '_source': ['event_type', 'pipeline_run_id'],
                },
            )
        except Exception as e:
            logger.error(f"Error querying review cycle events for {project}: {e}")
            return empty

        hits = result['hits']['hits']
        total = result['hits'].get('total', {})
        if isinstance(total, dict):
            total = total.get('value', len(hits))
        if total > len(hits):
            logger.warning(
                f"decision-events (review_cycle) query returned {len(hits)} of {total} docs "
                f"for {project} in window {start.date()} — metrics may be incomplete"
            )

        all_run_ids: set = set()
        run_iterations: Dict[str, int] = {}
        escalation_count = 0

        for hit in hits:
            src = hit['_source']
            event_type = src.get('event_type', '')
            run_id = src.get('pipeline_run_id')
            if run_id:
                all_run_ids.add(run_id)
            if event_type == 'review_cycle_iteration':
                key = run_id or '__no_run__'
                run_iterations[key] = run_iterations.get(key, 0) + 1
            elif event_type == 'review_cycle_escalated':
                escalation_count += 1

        total_count = len(all_run_ids)
        if not run_iterations:
            return {**empty, 'total_count': total_count, 'escalation_count': escalation_count}

        iterations_list = list(run_iterations.values())
        total_iterations = sum(iterations_list)
        return {
            'total_count': total_count,
            'total_iterations': total_iterations,
            'avg_iterations': round(total_iterations / len(iterations_list), 2),
            'max_iterations': max(iterations_list),
            'escalation_count': escalation_count,
        }

    # -------------------------------------------------------------------------
    # Repair cycle metrics
    # -------------------------------------------------------------------------

    def _compute_repair_cycle_metrics(
        self, project: str, start: datetime, end: datetime
    ) -> Dict[str, Any]:
        """
        Aggregate repair cycle metrics from decision-events.

        - REPAIR_CYCLE_COMPLETED / FAILED: total count, duration
        - REPAIR_CYCLE_TEST_CYCLE_STARTED: count per pipeline_run_id → test cycles
        - REPAIR_CYCLE_FIX_CYCLE_STARTED: count per pipeline_run_id → fix cycles
        - REPAIR_CYCLE_SYSTEMIC_ANALYSIS_STARTED: systemic analysis count
        """
        empty = {
            'total_count': 0,
            'avg_test_cycles': 0.0,
            'max_test_cycles': 0,
            'avg_fix_cycles': 0.0,
            'max_fix_cycles': 0,
            'avg_test_duration_ms': 0.0,
            'max_test_duration_ms': 0,
            'systemic_analysis_count': 0,
        }
        try:
            result = self.es.search(
                index='decision-events-*',
                body={
                    'size': 10000,
                    'query': {
                        'bool': {
                            'must': [
                                {'term': {'project': project}},
                                {'range': {'timestamp': {
                                    'gte': start.isoformat(),
                                    'lt': end.isoformat(),
                                }}},
                                {'bool': {
                                    'should': [{'prefix': {'event_type': 'repair_cycle_'}}],
                                    'minimum_should_match': 1,
                                }},
                            ]
                        }
                    },
                    '_source': ['event_type', 'pipeline_run_id', 'data'],
                },
            )
        except Exception as e:
            logger.error(f"Error querying repair cycle events for {project}: {e}")
            return empty

        hits = result['hits']['hits']
        total = result['hits'].get('total', {})
        if isinstance(total, dict):
            total = total.get('value', len(hits))
        if total > len(hits):
            logger.warning(
                f"decision-events (repair_cycle) query returned {len(hits)} of {total} docs "
                f"for {project} in window {start.date()} — metrics may be incomplete"
            )

        test_cycle_counts: Dict[str, int] = {}
        fix_cycle_counts: Dict[str, int] = {}
        durations_ms: List[float] = []
        systemic_analysis_count = 0
        total_count = 0

        for hit in hits:
            src = hit['_source']
            event_type = src.get('event_type', '')
            run_id = src.get('pipeline_run_id') or '__no_run__'
            data = src.get('data') or {}

            if event_type == 'repair_cycle_completed':
                total_count += 1
                duration_s = data.get('duration_seconds')
                if duration_s is not None:
                    durations_ms.append(float(duration_s) * 1000)
            elif event_type == 'repair_cycle_failed':
                total_count += 1
            elif event_type == 'repair_cycle_test_cycle_started':
                test_cycle_counts[run_id] = test_cycle_counts.get(run_id, 0) + 1
            elif event_type == 'repair_cycle_fix_cycle_started':
                fix_cycle_counts[run_id] = fix_cycle_counts.get(run_id, 0) + 1
            elif event_type == 'repair_cycle_systemic_analysis_started':
                systemic_analysis_count += 1

        test_vals = list(test_cycle_counts.values())
        fix_vals = list(fix_cycle_counts.values())
        return {
            'total_count': total_count,
            'avg_test_cycles': round(sum(test_vals) / len(test_vals), 2) if test_vals else 0.0,
            'max_test_cycles': max(test_vals) if test_vals else 0,
            'avg_fix_cycles': round(sum(fix_vals) / len(fix_vals), 2) if fix_vals else 0.0,
            'max_fix_cycles': max(fix_vals) if fix_vals else 0,
            'avg_test_duration_ms': round(sum(durations_ms) / len(durations_ms), 2) if durations_ms else 0.0,
            'max_test_duration_ms': int(max(durations_ms)) if durations_ms else 0,
            'systemic_analysis_count': systemic_analysis_count,
        }

    # -------------------------------------------------------------------------
    # PR review cycle metrics
    # -------------------------------------------------------------------------

    def _compute_pr_review_metrics(
        self, project: str, start: datetime, end: datetime
    ) -> Dict[str, Any]:
        """
        Aggregate PR review cycle metrics from decision-events.

        Counts PR_REVIEW_STAGE_COMPLETED events for total_count.
        Counts PR_REVIEW_PHASE_COMPLETED events per pipeline_run_id as iterations.
        """
        empty = {
            'total_count': 0,
            'total_iterations': 0,
            'avg_iterations': 0.0,
            'max_iterations': 0,
        }
        try:
            result = self.es.search(
                index='decision-events-*',
                body={
                    'size': 10000,
                    'query': {
                        'bool': {
                            'must': [
                                {'term': {'project': project}},
                                {'range': {'timestamp': {
                                    'gte': start.isoformat(),
                                    'lt': end.isoformat(),
                                }}},
                                {'bool': {
                                    'should': [{'prefix': {'event_type': 'pr_review_'}}],
                                    'minimum_should_match': 1,
                                }},
                            ]
                        }
                    },
                    '_source': ['event_type', 'pipeline_run_id'],
                },
            )
        except Exception as e:
            logger.error(f"Error querying PR review events for {project}: {e}")
            return empty

        hits = result['hits']['hits']
        total = result['hits'].get('total', {})
        if isinstance(total, dict):
            total = total.get('value', len(hits))
        if total > len(hits):
            logger.warning(
                f"decision-events (pr_review) query returned {len(hits)} of {total} docs "
                f"for {project} in window {start.date()} — metrics may be incomplete"
            )

        stage_run_ids: set = set()
        phase_counts: Dict[str, int] = {}

        for hit in hits:
            src = hit['_source']
            event_type = src.get('event_type', '')
            run_id = src.get('pipeline_run_id')
            if event_type == 'pr_review_stage_completed' and run_id:
                stage_run_ids.add(run_id)
            elif event_type == 'pr_review_phase_completed':
                key = run_id or '__no_run__'
                phase_counts[key] = phase_counts.get(key, 0) + 1

        total_count = len(stage_run_ids)
        if not phase_counts:
            return {**empty, 'total_count': total_count}

        # Prefer phase counts keyed to stage run IDs; fall back to all counts
        phase_vals = [phase_counts[rid] for rid in stage_run_ids if rid in phase_counts]
        if not phase_vals:
            phase_vals = list(phase_counts.values())

        total_iterations = sum(phase_vals)
        return {
            'total_count': total_count,
            'total_iterations': total_iterations,
            'avg_iterations': round(total_iterations / len(phase_vals), 2) if phase_vals else 0.0,
            'max_iterations': max(phase_vals) if phase_vals else 0,
        }

    # -------------------------------------------------------------------------
    # Pipeline outcome metrics
    # -------------------------------------------------------------------------

    def _compute_pipeline_outcomes(
        self, project: str, start: datetime, end: datetime
    ) -> Dict[str, Any]:
        """Count pipeline run outcomes from pipeline-runs-* indices."""
        empty = {'success_count': 0, 'failed_count': 0, 'success_rate': 0.0}
        try:
            result = self.es.search(
                index='pipeline-runs-*',
                body={
                    'size': 10000,
                    'query': {
                        'bool': {
                            'must': [
                                {'term': {'project': project}},
                                {'range': {'ended_at': {
                                    'gte': start.isoformat(),
                                    'lt': end.isoformat(),
                                }}},
                                {'exists': {'field': 'ended_at'}},
                            ]
                        }
                    },
                    '_source': ['outcome'],
                },
            )
        except Exception as e:
            logger.error(f"Error querying pipeline outcomes for {project}: {e}")
            return empty

        hits = result['hits']['hits']
        total = result['hits'].get('total', {})
        if isinstance(total, dict):
            total = total.get('value', len(hits))
        if total > len(hits):
            logger.warning(
                f"pipeline-runs query returned {len(hits)} of {total} docs "
                f"for {project} in window {start.date()} — outcome metrics may be incomplete"
            )

        success_count = 0
        failed_count = 0
        for hit in hits:
            outcome = hit['_source'].get('outcome')
            if outcome == 'success':
                success_count += 1
            elif outcome in ('failed', 'failure'):
                failed_count += 1

        total = success_count + failed_count
        return {
            'success_count': success_count,
            'failed_count': failed_count,
            'success_rate': round(success_count / total, 4) if total > 0 else 0.0,
        }

    # -------------------------------------------------------------------------
    # Upsert
    # -------------------------------------------------------------------------

    def _upsert_project_metrics(self, doc: Dict[str, Any]):
        """Upsert a project metrics document by project + day_bucket composite key."""
        project = doc['project']
        day_bucket = doc['day_bucket']
        doc_id = f"{project}_{day_bucket}"
        index = _index_name(
            datetime.strptime(day_bucket, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        )
        try:
            self.es.update(
                index=index,
                id=doc_id,
                body={'doc': doc, 'doc_as_upsert': True},
                retry_on_conflict=3,
            )
            logger.debug(f"Upserted project metrics: {doc_id}")
        except Exception as e:
            logger.error(f"Error upserting project metrics {doc_id}: {e}")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_service: Optional[ProjectMetricsService] = None


def get_project_metrics_service() -> ProjectMetricsService:
    """Get or create the global ProjectMetricsService instance."""
    global _service
    if _service is None:
        es_host = os.environ.get('ELASTICSEARCH_HOST', 'elasticsearch:9200')
        _service = ProjectMetricsService(es_hosts=[f'http://{es_host}'])
    return _service
