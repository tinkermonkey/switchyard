"""
Token Metrics Service

Pre-computes token usage statistics from Claude streaming events and writes
aggregated results to Elasticsearch for the token usage report pages.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional
from elasticsearch import Elasticsearch, NotFoundError

logger = logging.getLogger(__name__)

# Index name patterns
AGENTS_INDEX_PREFIX = 'token-metrics-agents'
CYCLES_INDEX_PREFIX = 'token-metrics-cycles'

# Cycle type prefixes to detect from event_type
CYCLE_PREFIXES = ['review_cycle_', 'repair_cycle_', 'pr_review_']

# Map event_type prefix → canonical cycle_type label
CYCLE_TYPE_MAP = {
    'review_cycle_': 'review_cycle',
    'repair_cycle_': 'repair_cycle',
    'pr_review_': 'pr_review_stage',
}


def _index_name(prefix: str, dt: datetime) -> str:
    return f"{prefix}-{dt.strftime('%Y.%m')}"


class TokenMetricsService:
    """
    Computes and stores token usage metrics aggregated by agent and cycle type.
    """

    def __init__(self, es_hosts: List[str] = None):
        if es_hosts is None:
            es_hosts = ['http://elasticsearch:9200']
        self.es = Elasticsearch(es_hosts)
        self._ensure_index_templates()

    def _ensure_index_templates(self):
        """Create index templates for token metrics indices if they don't exist."""
        for prefix, id_field in [
            (AGENTS_INDEX_PREFIX, 'agent_name'),
            (CYCLES_INDEX_PREFIX, 'cycle_type'),
        ]:
            template_name = f"{prefix}-template"
            try:
                self.es.indices.get_index_template(name=template_name)
                logger.debug(f"Index template {template_name} already exists")
            except NotFoundError:
                body = {
                    "index_patterns": [f"{prefix}-*"],
                    "template": {
                        "settings": {
                            "number_of_shards": 1,
                            "number_of_replicas": 0
                        },
                        "mappings": {
                            "properties": {
                                id_field: {"type": "keyword"},
                                "computed_at": {"type": "date"},
                                "window_start": {"type": "date"},
                                "window_end": {"type": "date"},
                                "sample_count": {"type": "integer"},
                                "avg_initial_input": {"type": "float"},
                                "avg_total_input": {"type": "float"},
                                "min_total_input": {"type": "integer"},
                                "max_total_input": {"type": "integer"},
                                "avg_total_output": {"type": "float"},
                                "min_total_output": {"type": "integer"},
                                "max_total_output": {"type": "integer"},
                                "avg_total_all": {"type": "float"},
                                "min_total_all": {"type": "integer"},
                                "max_total_all": {"type": "integer"},
                            }
                        }
                    }
                }
                self.es.indices.put_index_template(name=template_name, body=body)
                logger.info(f"Created index template: {template_name}")
            except Exception as e:
                logger.warning(f"Could not ensure index template {template_name}: {e}")

    async def run_metrics_job(self):
        """
        Run the full token metrics computation job.

        Reads TOKEN_METRICS_INTERVAL_HOURS env var (default 3) to determine
        the time window, then runs agent and cycle metric computations.
        """
        import asyncio

        hours = int(os.environ.get('TOKEN_METRICS_INTERVAL_HOURS', '3'))
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(hours=hours)

        logger.info(
            f"Starting token metrics job: window={window_start.isoformat()} to {now.isoformat()}"
        )

        loop = asyncio.get_running_loop()

        try:
            agent_results = await loop.run_in_executor(
                None, self._compute_agent_metrics, window_start, now
            )
            logger.info(f"Computed agent metrics for {len(agent_results)} agents")
        except Exception as e:
            logger.error(f"Error computing agent metrics: {e}", exc_info=True)
            agent_results = []

        try:
            cycle_results = await loop.run_in_executor(
                None, self._compute_cycle_metrics, window_start, now
            )
            logger.info(f"Computed cycle metrics for {len(cycle_results)} cycle types")
        except Exception as e:
            logger.error(f"Error computing cycle metrics: {e}", exc_info=True)
            cycle_results = []

        # Write results to ES
        computed_at = now.isoformat()
        agents_index = _index_name(AGENTS_INDEX_PREFIX, now)
        cycles_index = _index_name(CYCLES_INDEX_PREFIX, now)

        for doc in agent_results:
            doc['computed_at'] = computed_at
            doc['window_start'] = window_start.isoformat()
            doc['window_end'] = now.isoformat()
            try:
                self.es.index(index=agents_index, body=doc)
            except Exception as e:
                logger.error(f"Error writing agent metrics doc: {e}")

        for doc in cycle_results:
            doc['computed_at'] = computed_at
            doc['window_start'] = window_start.isoformat()
            doc['window_end'] = now.isoformat()
            try:
                self.es.index(index=cycles_index, body=doc)
            except Exception as e:
                logger.error(f"Error writing cycle metrics doc: {e}")

        logger.info(
            f"Token metrics job complete: {len(agent_results)} agent docs, "
            f"{len(cycle_results)} cycle docs written"
        )

    def _compute_agent_metrics(
        self, window_start: datetime, window_end: datetime
    ) -> List[Dict[str, Any]]:
        """
        Compute per-agent token usage statistics for the given window.

        1. Query agent-events-* for agent_initialized events → get task_ids per agent
        2. For each agent, fetch claude-streams-* docs for those task_ids
        3. Walk docs chronologically, extract usage data, aggregate stats
        """
        # Step 1: Find all agent initializations in the window
        try:
            init_result = self.es.search(
                index='agent-events-*',
                body={
                    'size': 10000,
                    'query': {
                        'bool': {
                            'must': [
                                {'term': {'event_type': 'agent_initialized'}},
                                {'range': {'timestamp': {
                                    'gte': window_start.isoformat(),
                                    'lte': window_end.isoformat()
                                }}}
                            ]
                        }
                    },
                    '_source': ['agent', 'task_id', 'agent_execution_id']
                }
            )
        except Exception as e:
            logger.error(f"Error querying agent initializations: {e}")
            return []

        # Warn if results were truncated
        init_hits = init_result['hits']['hits']
        init_total = init_result['hits'].get('total', {})
        if isinstance(init_total, dict):
            init_total = init_total.get('value', len(init_hits))
        if init_total > len(init_hits):
            logger.warning(
                f"agent-events query returned {len(init_hits)} of {init_total} "
                f"agent_initialized events - metrics may be incomplete"
            )

        # Group task_ids by agent
        agent_tasks: Dict[str, List[str]] = {}
        for hit in init_hits:
            src = hit['_source']
            agent = src.get('agent') or src.get('agent_name')
            task_id = src.get('task_id')
            if agent and task_id:
                agent_tasks.setdefault(agent, []).append(task_id)

        if not agent_tasks:
            logger.info("No agent initializations found in window")
            return []

        results = []
        for agent_name, task_ids in agent_tasks.items():
            try:
                stats = self._compute_stats_for_tasks(task_ids)
                if stats:
                    stats['agent_name'] = agent_name
                    results.append(stats)
            except Exception as e:
                logger.error(f"Error computing stats for agent {agent_name}: {e}")

        return results

    def _compute_cycle_metrics(
        self, window_start: datetime, window_end: datetime
    ) -> List[Dict[str, Any]]:
        """
        Compute per-cycle-type token usage statistics for the given window.

        1. Query decision-events-* for cycle-related event_types → pipeline_run_id per cycle type
        2. Query agent-events-* for agent_initialized in those pipeline runs → task_ids per cycle type
        3. Compute stats per cycle type
        """
        # Step 1: Find cycle decision events in window
        should_clauses = []
        for prefix in CYCLE_PREFIXES:
            should_clauses.append({'prefix': {'event_type': prefix}})

        try:
            decision_result = self.es.search(
                index='decision-events-*',
                body={
                    'size': 10000,
                    'query': {
                        'bool': {
                            'must': [
                                {'range': {'timestamp': {
                                    'gte': window_start.isoformat(),
                                    'lte': window_end.isoformat()
                                }}}
                            ],
                            'should': should_clauses,
                            'minimum_should_match': 1
                        }
                    },
                    '_source': ['event_type', 'pipeline_run_id']
                }
            )
        except Exception as e:
            logger.error(f"Error querying decision events: {e}")
            return []

        # Warn if results were truncated
        dec_hits = decision_result['hits']['hits']
        dec_total = decision_result['hits'].get('total', {})
        if isinstance(dec_total, dict):
            dec_total = dec_total.get('value', len(dec_hits))
        if dec_total > len(dec_hits):
            logger.warning(
                f"decision-events query returned {len(dec_hits)} of {dec_total} "
                f"cycle events - metrics may be incomplete"
            )

        # Group pipeline_run_ids by cycle type
        cycle_pipeline_runs: Dict[str, set] = {}
        for hit in dec_hits:
            src = hit['_source']
            event_type = src.get('event_type', '')
            pipeline_run_id = src.get('pipeline_run_id')
            if not pipeline_run_id:
                continue
            for prefix, cycle_type in CYCLE_TYPE_MAP.items():
                if event_type.startswith(prefix):
                    cycle_pipeline_runs.setdefault(cycle_type, set()).add(pipeline_run_id)
                    break

        if not cycle_pipeline_runs:
            logger.info("No cycle events found in window")
            return []

        results = []
        for cycle_type, pipeline_run_ids in cycle_pipeline_runs.items():
            try:
                # Step 2: Get task_ids for these pipeline runs
                task_ids = self._get_task_ids_for_pipeline_runs(
                    list(pipeline_run_ids), window_start, window_end
                )

                if not task_ids:
                    continue

                # Step 3: Compute stats
                stats = self._compute_stats_for_tasks(task_ids)
                if stats:
                    stats['cycle_type'] = cycle_type

                    # Also compute per-agent breakdown
                    agent_breakdown = self._compute_agent_breakdown_for_tasks(task_ids)
                    stats['agent_breakdown'] = agent_breakdown

                    results.append(stats)
            except Exception as e:
                logger.error(f"Error computing stats for cycle {cycle_type}: {e}")

        return results

    def _get_task_ids_for_pipeline_runs(
        self,
        pipeline_run_ids: List[str],
        window_start: datetime,
        window_end: datetime
    ) -> List[str]:
        """Query agent-events-* for agent_initialized events in given pipeline runs."""
        try:
            result = self.es.search(
                index='agent-events-*',
                body={
                    'size': 10000,
                    'query': {
                        'bool': {
                            'must': [
                                {'term': {'event_type': 'agent_initialized'}},
                                {'terms': {'pipeline_run_id': pipeline_run_ids}},
                                {'range': {'timestamp': {
                                    'gte': window_start.isoformat(),
                                    'lte': window_end.isoformat()
                                }}}
                            ]
                        }
                    },
                    '_source': ['task_id']
                }
            )
            hits = result['hits']['hits']
            total = result['hits'].get('total', {})
            if isinstance(total, dict):
                total = total.get('value', len(hits))
            if total > len(hits):
                logger.warning(
                    f"pipeline run task_id query returned {len(hits)} of {total} hits - "
                    f"metrics may be incomplete"
                )
            return [hit['_source']['task_id'] for hit in hits if hit['_source'].get('task_id')]
        except Exception as e:
            logger.error(f"Error fetching task_ids for pipeline runs: {e}")
            return []

    def _compute_stats_for_tasks(self, task_ids: List[str]) -> Optional[Dict[str, Any]]:
        """
        Fetch claude-streams-* docs for the given task_ids and compute token statistics.

        Returns aggregated stats dict or None if no data.
        """
        if not task_ids:
            return None

        # Fetch streaming docs for these task_ids
        try:
            stream_result = self.es.search(
                index='claude-streams-*',
                body={
                    'size': 10000,
                    'sort': [
                        {'task_id': {'order': 'asc'}},
                        {'timestamp': {'order': 'asc'}}
                    ],
                    'query': {
                        'terms': {'task_id': task_ids}
                    },
                    '_source': ['task_id', 'raw_event', 'timestamp']
                }
            )
        except Exception as e:
            logger.error(f"Error querying claude-streams for tasks {task_ids[:3]}...: {e}")
            return None

        hits = stream_result['hits']['hits']
        if not hits:
            return None

        total_available = stream_result['hits'].get('total', {})
        if isinstance(total_available, dict):
            total_available = total_available.get('value', len(hits))
        if total_available > len(hits):
            logger.warning(
                f"claude-streams query returned {len(hits)} of {total_available} total hits "
                f"for task_ids {task_ids[:3]}... - metrics may be incomplete"
            )

        # Group by task_id and process each task's stream chronologically
        task_streams: Dict[str, List[Dict]] = {}
        for hit in hits:
            src = hit['_source']
            task_id = src.get('task_id')
            if task_id:
                task_streams.setdefault(task_id, []).append(src)

        # Per-task stats
        per_task_initial_input = []
        per_task_total_input = []
        per_task_total_output = []
        per_task_total_all = []
        combined_tool_counts: Dict[str, int] = {}
        model_counts: Dict[str, int] = {}

        for task_id, docs in task_streams.items():
            first_input = None
            last_input = 0
            last_output = 0

            for doc in docs:
                raw_event = doc.get('raw_event')
                if not raw_event:
                    continue

                # raw_event may be a dict already or a JSON string
                if isinstance(raw_event, str):
                    try:
                        raw_event = json.loads(raw_event)
                    except Exception:
                        continue

                event = raw_event.get('event') if isinstance(raw_event, dict) else None
                if not event:
                    continue

                if event.get('type') != 'assistant':
                    continue

                message = event.get('message', {})
                usage = message.get('usage', {})
                model = message.get('model')

                if not usage:
                    continue

                input_tokens = usage.get('input_tokens') or 0
                output_tokens = usage.get('output_tokens') or 0

                if model:
                    model_counts[model] = model_counts.get(model, 0) + 1

                if first_input is None:
                    first_input = input_tokens

                last_input = input_tokens
                last_output = output_tokens

                # Count tool calls and approximate token delta per tool
                contents = message.get('content') or []
                if not isinstance(contents, list):
                    contents = []

                tool_uses = [c for c in contents if c.get('type') == 'tool_use']
                for tool_use in tool_uses:
                    name = tool_use.get('name')
                    if name:
                        combined_tool_counts[name] = combined_tool_counts.get(name, 0) + 1

            if first_input is not None:
                per_task_initial_input.append(first_input)
                per_task_total_input.append(last_input)
                per_task_total_output.append(last_output)
                per_task_total_all.append(last_input + last_output)

        if not per_task_total_all:
            return None

        def avg(lst):
            return sum(lst) / len(lst) if lst else 0

        return {
            'sample_count': len(per_task_total_all),
            'avg_initial_input': avg(per_task_initial_input),
            'avg_total_input': avg(per_task_total_input),
            'min_total_input': min(per_task_total_input) if per_task_total_input else 0,
            'max_total_input': max(per_task_total_input) if per_task_total_input else 0,
            'avg_total_output': avg(per_task_total_output),
            'min_total_output': min(per_task_total_output) if per_task_total_output else 0,
            'max_total_output': max(per_task_total_output) if per_task_total_output else 0,
            'avg_total_all': avg(per_task_total_all),
            'min_total_all': min(per_task_total_all) if per_task_total_all else 0,
            'max_total_all': max(per_task_total_all) if per_task_total_all else 0,
            'tool_call_counts': combined_tool_counts,
            'model_breakdown': model_counts,
        }

    def _compute_agent_breakdown_for_tasks(
        self, task_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Compute per-agent stats for a set of task_ids."""
        try:
            result = self.es.search(
                index='agent-events-*',
                body={
                    'size': 10000,
                    'query': {
                        'bool': {
                            'must': [
                                {'term': {'event_type': 'agent_initialized'}},
                                {'terms': {'task_id': task_ids}}
                            ]
                        }
                    },
                    '_source': ['agent', 'task_id']
                }
            )
        except Exception as e:
            logger.error(f"Error querying agent breakdown: {e}")
            return {}

        agent_task_map: Dict[str, List[str]] = {}
        for hit in result['hits']['hits']:
            src = hit['_source']
            agent = src.get('agent') or src.get('agent_name')
            task_id = src.get('task_id')
            if agent and task_id:
                agent_task_map.setdefault(agent, []).append(task_id)

        breakdown = {}
        for agent, atask_ids in agent_task_map.items():
            stats = self._compute_stats_for_tasks(atask_ids)
            if stats:
                breakdown[agent] = {
                    'sample_count': stats['sample_count'],
                    'avg_total_all': stats['avg_total_all'],
                    'avg_total_input': stats['avg_total_input'],
                    'avg_total_output': stats['avg_total_output'],
                }

        return breakdown


# Singleton
_token_metrics_service: Optional[TokenMetricsService] = None


def get_token_metrics_service() -> TokenMetricsService:
    global _token_metrics_service
    if _token_metrics_service is None:
        _token_metrics_service = TokenMetricsService()
    return _token_metrics_service
