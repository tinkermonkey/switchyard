"""
Token Metrics Service

Pre-computes token usage statistics from Claude streaming events and writes
aggregated results to Elasticsearch for the token usage report pages.

Both agent and cycle metrics are stored as hourly bucket documents containing
raw sums and counts. The API layer aggregates these on read so that any time
window (1d, 7d, etc.) produces accurate totals and weighted averages.

Canonical token field meanings:
  sum_direct_input    : Σ input_tokens across all turns (uncached, 100% cost)
  sum_cache_read      : Σ cache_read_input_tokens across all turns (10% cost per read)
  sum_cache_creation  : Σ cache_creation_input_tokens across all turns (25% cost)
  sum_output          : Σ output_tokens across all turns
  sum_initial_input   : Σ first-turn effective_input per task (context startup size)
  sum_max_context     : Σ per-task peak effective_input (largest context window reached)
  task_count          : number of tasks in this bucket
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
AGENTS_HOURLY_INDEX_PREFIX = 'token-metrics-agents-hourly'
CYCLES_HOURLY_INDEX_PREFIX = 'token-metrics-cycles-hourly'

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


def _empty_tool_entry() -> Dict[str, Any]:
    return {
        'invocation_count': 0,
        'sum_direct_input': 0.0,
        'sum_cache_read': 0.0,
        'sum_cache_creation': 0.0,
        'sum_output': 0.0,
        'sum_context_growth': 0.0,
    }



class TokenMetricsService:
    """
    Computes and stores token usage metrics aggregated by agent and cycle type.

    Both agent and cycle metrics are written as hourly bucket documents.
    Each document covers exactly one hour and stores raw sums/counts so the
    API can compute correct weighted averages across any number of hours.

    Index layout:
      token-metrics-agents-hourly-YYYY.MM  — one doc per agent per hour
      token-metrics-cycles-hourly-YYYY.MM  — one doc per cycle_type per hour
      token-metrics-agents-YYYY.MM         — deprecated, no longer written to
    """

    def __init__(self, es_hosts: List[str] = None):
        if es_hosts is None:
            es_hosts = ['http://elasticsearch:9200']
        self.es = Elasticsearch(es_hosts)
        self._ensure_index_templates()

    def _ensure_index_templates(self):
        """Create index templates for token metrics indices if they don't exist."""

        # Canonical hourly field set (used for both agents-hourly and cycles-hourly)
        hourly_properties = {
            "hour_bucket":        {"type": "date"},
            "task_count":         {"type": "integer"},
            "sum_direct_input":   {"type": "long"},
            "sum_cache_read":     {"type": "long"},
            "sum_cache_creation": {"type": "long"},
            "sum_output":         {"type": "long"},
            "sum_initial_input":  {"type": "long"},
            "sum_max_context":    {"type": "long"},
            "min_max_context":    {"type": "long"},
            "max_max_context":    {"type": "long"},
            "min_output":         {"type": "long"},
            "max_output":         {"type": "long"},
            "tool_breakdown":     {"type": "object", "enabled": False},
            "model_breakdown":    {"type": "object", "enabled": False},
        }

        templates = [
            # Deprecated agents rolling-window index — keep template to avoid mapping conflicts
            (
                AGENTS_INDEX_PREFIX,
                f"{AGENTS_INDEX_PREFIX}-template",
                0,  # priority
                {
                    "agent_name": {"type": "keyword"},
                    "computed_at": {"type": "date"},
                    "window_start": {"type": "date"},
                    "window_end": {"type": "date"},
                    "sample_count": {"type": "integer"},
                    "avg_initial_input": {"type": "float"},
                    "min_initial_input": {"type": "integer"},
                    "max_initial_input": {"type": "integer"},
                    "avg_total_input": {"type": "float"},
                    "min_total_input": {"type": "integer"},
                    "max_total_input": {"type": "integer"},
                    "avg_total_output": {"type": "float"},
                    "min_total_output": {"type": "integer"},
                    "max_total_output": {"type": "integer"},
                    "avg_total_all": {"type": "float"},
                    "min_total_all": {"type": "integer"},
                    "max_total_all": {"type": "integer"},
                    "avg_cache_read": {"type": "float"},
                    "avg_cache_creation": {"type": "float"},
                    "avg_direct_input": {"type": "float"},
                    "tool_call_counts":       {"type": "object", "enabled": False},
                    "model_breakdown":        {"type": "object", "enabled": False},
                    "tool_token_attribution": {"type": "object", "enabled": False},
                    "agent_breakdown":        {"type": "object", "enabled": False},
                }
            ),
            # New hourly agents index — higher priority than the general agents prefix
            (
                AGENTS_HOURLY_INDEX_PREFIX,
                f"{AGENTS_HOURLY_INDEX_PREFIX}-template",
                100,  # priority — wins over token-metrics-agents-* template
                {
                    "agent_name": {"type": "keyword"},
                    **hourly_properties,
                }
            ),
            # Existing hourly cycles index
            (
                CYCLES_HOURLY_INDEX_PREFIX,
                f"{CYCLES_HOURLY_INDEX_PREFIX}-template",
                100,  # priority
                {
                    "cycle_type": {"type": "keyword"},
                    **hourly_properties,
                    "agent_breakdown": {"type": "object", "enabled": False},
                }
            ),
        ]

        for prefix, template_name, priority, properties in templates:
            try:
                self.es.indices.get_index_template(name=template_name)
                logger.debug(f"Index template {template_name} already exists")
            except NotFoundError:
                body = {
                    "index_patterns": [f"{prefix}-*"],
                    "priority": priority,
                    "template": {
                        "settings": {
                            "number_of_shards": 1,
                            "number_of_replicas": 0,
                        },
                        "mappings": {"properties": properties},
                    },
                }
                try:
                    self.es.indices.put_index_template(name=template_name, body=body)
                    logger.info(f"Created index template: {template_name}")
                except Exception as e:
                    logger.warning(f"Could not create index template {template_name}: {e}")
            except Exception as e:
                logger.warning(f"Could not ensure index template {template_name}: {e}")

    async def run_metrics_job(self):
        """
        Run the full token metrics computation job.

        Both agent and cycle metrics are computed as hourly buckets covering the
        last (interval_hours * 2) complete hours plus the current partial hour.
        Each hourly document is upserted so re-running is idempotent.
        """
        import asyncio

        interval_hours = int(os.environ.get('TOKEN_METRICS_INTERVAL_HOURS', '3'))
        lookback_hours = interval_hours * 2
        now = datetime.now(timezone.utc)
        current_hour_start = now.replace(minute=0, second=0, microsecond=0)

        logger.info(
            f"Starting token metrics job: now={now.isoformat()}, "
            f"lookback={lookback_hours}h"
        )

        loop = asyncio.get_running_loop()

        # Build list of hours: lookback complete hours + current partial hour
        hours_to_compute = []
        for i in range(lookback_hours, 0, -1):
            h_start = current_hour_start - timedelta(hours=i)
            hours_to_compute.append((h_start, h_start + timedelta(hours=1)))
        hours_to_compute.append((current_hour_start, now))

        total_agent_docs = 0
        total_cycle_docs = 0

        for hour_start, hour_end in hours_to_compute:
            # --- Agent hourly metrics ---
            try:
                agent_hourly = await loop.run_in_executor(
                    None, self._compute_hourly_agent_metrics, hour_start, hour_end
                )
                for doc in agent_hourly:
                    doc_id = f"{doc['agent_name']}_{int(hour_start.timestamp())}"
                    hourly_index = _index_name(AGENTS_HOURLY_INDEX_PREFIX, hour_start)
                    try:
                        self.es.index(index=hourly_index, id=doc_id, body=doc)
                        total_agent_docs += 1
                    except Exception as e:
                        logger.error(f"Error writing hourly agent doc {doc_id}: {e}")
            except Exception as e:
                logger.error(
                    f"Error computing agent metrics for hour {hour_start.isoformat()}: {e}",
                    exc_info=True
                )

            # --- Cycle hourly metrics ---
            try:
                cycle_hourly = await loop.run_in_executor(
                    None, self._compute_hourly_cycle_metrics, hour_start, hour_end
                )
                for doc in cycle_hourly:
                    doc_id = f"{doc['cycle_type']}_{int(hour_start.timestamp())}"
                    hourly_index = _index_name(CYCLES_HOURLY_INDEX_PREFIX, hour_start)
                    try:
                        self.es.index(index=hourly_index, id=doc_id, body=doc)
                        total_cycle_docs += 1
                    except Exception as e:
                        logger.error(f"Error writing hourly cycle doc {doc_id}: {e}")
            except Exception as e:
                logger.error(
                    f"Error computing cycle metrics for hour {hour_start.isoformat()}: {e}",
                    exc_info=True
                )

        logger.info(
            f"Token metrics job complete: {total_agent_docs} agent hourly docs, "
            f"{total_cycle_docs} cycle hourly docs written/updated"
        )

    # -------------------------------------------------------------------------
    # Agent metrics: hourly buckets
    # -------------------------------------------------------------------------

    def _compute_hourly_agent_metrics(
        self, hour_start: datetime, hour_end: datetime
    ) -> List[Dict[str, Any]]:
        """
        Compute sum-based agent metrics for a single hour bucket.

        1. Query agent-events-* for agent_initialized events in [hour_start, hour_end]
        2. Group task_ids by agent_name
        3. For each agent: call _compute_sum_stats_for_tasks() → full canonical fields
        4. Return list of docs ready for upsert into token-metrics-agents-hourly-*
        """
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
                                    'gte': hour_start.isoformat(),
                                    'lte': hour_end.isoformat()
                                }}}
                            ]
                        }
                    },
                    '_source': ['agent', 'agent_name', 'task_id']
                }
            )
        except Exception as e:
            logger.error(f"Error querying agent initializations for hour {hour_start}: {e}")
            return []

        init_hits = init_result['hits']['hits']
        init_total = init_result['hits'].get('total', {})
        if isinstance(init_total, dict):
            init_total = init_total.get('value', len(init_hits))
        if init_total > len(init_hits):
            logger.warning(
                f"agent-events query returned {len(init_hits)} of {init_total} "
                f"agent_initialized events for hour {hour_start} - metrics may be incomplete"
            )

        agent_tasks: Dict[str, List[str]] = {}
        for hit in init_hits:
            src = hit['_source']
            agent = src.get('agent') or src.get('agent_name')
            task_id = src.get('task_id')
            if agent and task_id:
                agent_tasks.setdefault(agent, []).append(task_id)

        if not agent_tasks:
            return []

        results = []
        for agent_name, task_ids in agent_tasks.items():
            try:
                stats = self._compute_sum_stats_for_tasks(task_ids)
                if stats:
                    stats['agent_name'] = agent_name
                    stats['hour_bucket'] = hour_start.isoformat()
                    results.append(stats)
            except Exception as e:
                logger.error(f"Error computing hourly agent stats for {agent_name}: {e}")

        return results

    # -------------------------------------------------------------------------
    # Cycle metrics: hourly computation
    # -------------------------------------------------------------------------

    def _compute_hourly_cycle_metrics(
        self, hour_start: datetime, hour_end: datetime
    ) -> List[Dict[str, Any]]:
        """
        Compute sum-based cycle metrics for a single hour bucket.

        1. Find cycle decision events in [hour_start, hour_end]
        2. Resolve pipeline_run_ids → task_ids
        3. Compute raw sums/counts per cycle_type
        """
        should_clauses = [{'prefix': {'event_type': p}} for p in CYCLE_PREFIXES]
        try:
            decision_result = self.es.search(
                index='decision-events-*',
                body={
                    'size': 10000,
                    'query': {
                        'bool': {
                            'must': [
                                {'range': {'timestamp': {
                                    'gte': hour_start.isoformat(),
                                    'lte': hour_end.isoformat()
                                }}}
                            ],
                            'should': should_clauses,
                            'minimum_should_match': 1,
                        }
                    },
                    '_source': ['event_type', 'pipeline_run_id']
                }
            )
        except Exception as e:
            logger.error(f"Error querying decision events for hour {hour_start}: {e}")
            return []

        dec_hits = decision_result['hits']['hits']
        dec_total = decision_result['hits'].get('total', {})
        if isinstance(dec_total, dict):
            dec_total = dec_total.get('value', len(dec_hits))
        if dec_total > len(dec_hits):
            logger.warning(
                f"decision-events query returned {len(dec_hits)} of {dec_total} "
                f"cycle events for hour {hour_start} - metrics may be incomplete"
            )

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
            return []

        results = []
        for cycle_type, pipeline_run_ids in cycle_pipeline_runs.items():
            try:
                task_ids = self._get_task_ids_for_pipeline_runs(list(pipeline_run_ids))
                if not task_ids:
                    continue

                stats = self._compute_sum_stats_for_tasks(task_ids)
                if stats:
                    stats['cycle_type'] = cycle_type
                    stats['hour_bucket'] = hour_start.isoformat()
                    stats['agent_breakdown'] = self._compute_agent_breakdown_sums_for_tasks(task_ids)
                    results.append(stats)
            except Exception as e:
                logger.error(f"Error computing hourly stats for cycle {cycle_type}: {e}")

        return results

    # -------------------------------------------------------------------------
    # Shared stream processing
    # -------------------------------------------------------------------------

    def _get_task_ids_for_pipeline_runs(
        self,
        pipeline_run_ids: List[str],
        window_start: Optional[datetime] = None,
        window_end: Optional[datetime] = None,
    ) -> List[str]:
        """
        Query agent-events-* for agent_initialized events in the given pipeline runs.
        Time window is optional; omit it when calling from hourly computation to avoid
        missing agent events that land slightly outside the cycle-event window.
        """
        must_clauses = [
            {'term': {'event_type': 'agent_initialized'}},
            {'terms': {'pipeline_run_id': pipeline_run_ids}},
        ]
        if window_start and window_end:
            must_clauses.append({'range': {'timestamp': {
                'gte': window_start.isoformat(),
                'lte': window_end.isoformat()
            }}})

        try:
            result = self.es.search(
                index='agent-events-*',
                body={
                    'size': 10000,
                    'query': {'bool': {'must': must_clauses}},
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

    def _process_task_streams(self, task_ids: List[str]) -> Optional[Dict[str, Any]]:
        """
        Fetch claude-streams docs for the given task_ids and produce per-task
        token sums plus per-tool and per-model breakdowns.

        All four token types are summed across ALL turns (not snapshot of last turn),
        which gives the correct billable quantity:
          - cache_read is charged on every API call that reads from cache
          - cache_creation is charged when the cache is written
          - direct input is the uncached input on every turn
          - output is charged per token produced

        Tool attribution:
          - sum_output: output tokens in turns that invoke each tool (proportional split)
          - sum_context_growth: delta effective_input in the NEXT turn after the tool call

        Returns a dict of raw per-task lists and aggregated breakdowns, or None.
        """
        if not task_ids:
            return None

        try:
            stream_result = self.es.search(
                index='claude-streams-*',
                body={
                    'size': 10000,
                    'sort': [
                        {'task_id': {'order': 'asc'}},
                        {'timestamp': {'order': 'asc'}}
                    ],
                    'query': {'terms': {'task_id': task_ids}},
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

        task_streams: Dict[str, List[Dict]] = {}
        for hit in hits:
            src = hit['_source']
            task_id = src.get('task_id')
            if task_id:
                task_streams.setdefault(task_id, []).append(src)

        # Per-task accumulators (appended to lists after processing each task)
        per_task_sum_direct: List[int] = []
        per_task_sum_cache_read: List[int] = []
        per_task_sum_cache_creation: List[int] = []
        per_task_sum_output: List[int] = []
        per_task_initial_input: List[int] = []
        per_task_max_context: List[int] = []

        # Tool breakdown: name → raw accumulator dict
        tool_breakdown_raw: Dict[str, Dict] = {}

        # Model breakdown: model → list of per-task dicts
        model_per_task: Dict[str, List[Dict]] = {}

        for task_id, docs in task_streams.items():
            sum_direct = 0
            sum_cache_read = 0
            sum_cache_creation = 0
            sum_output = 0
            first_input: Optional[int] = None
            peak_input = 0
            task_model: Optional[str] = None
            prev_effective_input: Optional[int] = None
            prev_tool_uses: List[str] = []

            for doc in docs:
                raw_event = doc.get('raw_event')
                if not raw_event:
                    continue

                if isinstance(raw_event, str):
                    try:
                        raw_event = json.loads(raw_event)
                    except Exception:
                        continue

                event = raw_event.get('event') if isinstance(raw_event, dict) else None
                if not event or event.get('type') != 'assistant':
                    continue

                message = event.get('message', {})
                usage = message.get('usage', {})
                model = message.get('model')

                if not usage:
                    continue

                input_direct = usage.get('input_tokens') or 0
                cache_read = usage.get('cache_read_input_tokens') or 0
                cache_creation = usage.get('cache_creation_input_tokens') or 0
                output_tokens = usage.get('output_tokens') or 0
                effective_input = input_direct + cache_read + cache_creation

                # Sum all four types across all turns
                sum_direct += input_direct
                sum_cache_read += cache_read
                sum_cache_creation += cache_creation
                sum_output += output_tokens

                if first_input is None:
                    first_input = effective_input
                if effective_input > peak_input:
                    peak_input = effective_input
                if model:
                    task_model = model

                # Parse content blocks to find tool_use in this turn
                contents = message.get('content') or []
                if not isinstance(contents, list):
                    contents = []

                current_tool_uses: List[str] = []
                for c in contents:
                    if c.get('type') == 'tool_use':
                        name = c.get('name')
                        if name:
                            current_tool_uses.append(name)

                # Attribute this turn's token costs to tools invoked in this turn
                if current_tool_uses:
                    k = len(current_tool_uses)
                    for tool_name in current_tool_uses:
                        td = tool_breakdown_raw.setdefault(tool_name, _empty_tool_entry())
                        td['invocation_count'] += 1
                        td['sum_direct_input'] += input_direct / k
                        td['sum_cache_read'] += cache_read / k
                        td['sum_cache_creation'] += cache_creation / k
                        td['sum_output'] += output_tokens / k

                # Context growth attribution: delta effective_input attributed to PREV turn's tools
                if prev_effective_input is not None and prev_tool_uses:
                    delta = max(0, effective_input - prev_effective_input)
                    k = len(prev_tool_uses)
                    per_tool_delta = delta / k if k > 0 else 0
                    for tool_name in prev_tool_uses:
                        td = tool_breakdown_raw.setdefault(tool_name, _empty_tool_entry())
                        td['sum_context_growth'] += per_tool_delta

                prev_effective_input = effective_input
                prev_tool_uses = current_tool_uses

            if first_input is not None:
                per_task_sum_direct.append(sum_direct)
                per_task_sum_cache_read.append(sum_cache_read)
                per_task_sum_cache_creation.append(sum_cache_creation)
                per_task_sum_output.append(sum_output)
                per_task_initial_input.append(first_input)
                per_task_max_context.append(peak_input)

                if task_model:
                    model_per_task.setdefault(task_model, []).append({
                        'sum_direct': sum_direct,
                        'sum_cache_read': sum_cache_read,
                        'sum_cache_creation': sum_cache_creation,
                        'sum_output': sum_output,
                        'initial_input': first_input,
                        'max_context': peak_input,
                    })

        if not per_task_sum_output:
            return None

        return {
            'per_task_sum_direct': per_task_sum_direct,
            'per_task_sum_cache_read': per_task_sum_cache_read,
            'per_task_sum_cache_creation': per_task_sum_cache_creation,
            'per_task_sum_output': per_task_sum_output,
            'per_task_initial_input': per_task_initial_input,
            'per_task_max_context': per_task_max_context,
            'tool_breakdown_raw': tool_breakdown_raw,
            'model_per_task': model_per_task,
        }

    def _compute_sum_stats_for_tasks(self, task_ids: List[str]) -> Optional[Dict[str, Any]]:
        """
        Compute the canonical hourly-bucket stats for a set of tasks.

        Returns a dict with all canonical fields including nested tool_breakdown
        and model_breakdown with the full field set. Stores raw sums and counts
        so the API can compute weighted averages across any time window.
        """
        raw = self._process_task_streams(task_ids)
        if raw is None:
            return None

        psd = raw['per_task_sum_direct']
        pscr = raw['per_task_sum_cache_read']
        pscc = raw['per_task_sum_cache_creation']
        pso = raw['per_task_sum_output']
        pii = raw['per_task_initial_input']
        pmc = raw['per_task_max_context']
        n = len(psd)

        # Build tool_breakdown with canonical field names
        tool_breakdown: Dict[str, Dict] = {}
        for tool_name, td in raw['tool_breakdown_raw'].items():
            tool_breakdown[tool_name] = {
                'task_count': td['invocation_count'],  # number of turns that invoked this tool
                'sum_direct_input': int(td['sum_direct_input']),
                'sum_cache_read': int(td['sum_cache_read']),
                'sum_cache_creation': int(td['sum_cache_creation']),
                'sum_output': int(td['sum_output']),
                'sum_initial_input': 0,   # not applicable at tool level
                'sum_max_context': 0,      # not applicable at tool level
                'min_max_context': 0,
                'max_max_context': 0,
                'min_output': 0,
                'max_output': 0,
                'sum_context_growth': int(td['sum_context_growth']),
            }

        # Build model_breakdown with canonical field names
        model_breakdown: Dict[str, Dict] = {}
        for model_name, tasks in raw['model_per_task'].items():
            tc = len(tasks)
            max_contexts = [t['max_context'] for t in tasks]
            sum_outputs = [t['sum_output'] for t in tasks]
            model_breakdown[model_name] = {
                'task_count': tc,
                'sum_direct_input': sum(t['sum_direct'] for t in tasks),
                'sum_cache_read': sum(t['sum_cache_read'] for t in tasks),
                'sum_cache_creation': sum(t['sum_cache_creation'] for t in tasks),
                'sum_output': sum(sum_outputs),
                'sum_initial_input': sum(t['initial_input'] for t in tasks),
                'sum_max_context': sum(max_contexts),
                'min_max_context': min(max_contexts),
                'max_max_context': max(max_contexts),
                'min_output': min(sum_outputs),
                'max_output': max(sum_outputs),
            }

        return {
            'task_count': n,
            'sum_direct_input': sum(psd),
            'sum_cache_read': sum(pscr),
            'sum_cache_creation': sum(pscc),
            'sum_output': sum(pso),
            'sum_initial_input': sum(pii),
            'sum_max_context': sum(pmc),
            'min_max_context': min(pmc) if pmc else 0,
            'max_max_context': max(pmc) if pmc else 0,
            'min_output': min(pso) if pso else 0,
            'max_output': max(pso) if pso else 0,
            'tool_breakdown': tool_breakdown,
            'model_breakdown': model_breakdown,
        }

    def _compute_agent_breakdown_sums_for_tasks(
        self, task_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """
        Compute per-agent canonical stats for the hourly cycle bucket.

        Returns a dict keyed by agent_name where each value is the full canonical
        field set (same shape as a top-level agent hourly doc) including nested
        tool_breakdown and model_breakdown.
        """
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
                    '_source': ['agent', 'agent_name', 'task_id']
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
        for agent, agent_task_ids in agent_task_map.items():
            stats = self._compute_sum_stats_for_tasks(agent_task_ids)
            if stats:
                breakdown[agent] = stats

        return breakdown


# Singleton
_token_metrics_service: Optional[TokenMetricsService] = None


def get_token_metrics_service() -> TokenMetricsService:
    global _token_metrics_service
    if _token_metrics_service is None:
        _token_metrics_service = TokenMetricsService()
    return _token_metrics_service
