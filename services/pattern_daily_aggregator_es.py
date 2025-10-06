"""
Pattern Daily Aggregator Service (Elasticsearch-only)

Runs daily Elasticsearch aggregations to discover statistical patterns:
- Common error sequences
- Tools with high retry rates
- Context usage patterns
- Time and project correlations

Stores all results in Elasticsearch pattern-insights index.
"""

import logging
import asyncio
import json
from typing import Dict, Any, List
from datetime import datetime, timedelta, date
from elasticsearch import Elasticsearch
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import random

logger = logging.getLogger(__name__)


class PatternDailyAggregator:
    """Performs daily statistical analysis on Elasticsearch logs (ES-only)"""

    def __init__(
        self,
        elasticsearch_hosts: List[str],
        aggregation_interval_hours: int = 24,
        lookback_days: int = 7
    ):
        """
        Initialize daily aggregator

        Args:
            elasticsearch_hosts: List of Elasticsearch hosts
            aggregation_interval_hours: Hours between aggregation runs
            lookback_days: Days of history to analyze
        """
        self.es = Elasticsearch(elasticsearch_hosts)
        self.aggregation_interval_hours = aggregation_interval_hours
        self.lookback_days = lookback_days

        # Scheduler
        self.scheduler = AsyncIOScheduler()

        # Statistics
        self.total_runs = 0
        self.total_insights_created = 0
        self.total_pattern_candidates = 0

        logger.info(
            f"PatternDailyAggregator initialized "
            f"(interval={aggregation_interval_hours}h, lookback={lookback_days}d)"
        )

    async def run(self):
        """Start the scheduler"""
        logger.info("Starting Pattern Daily Aggregator service...")

        # Schedule aggregation task using APScheduler with cron trigger
        # Run daily at 3 AM (stagger from other services)
        self.scheduler.add_job(
            self._run_daily_aggregations,
            trigger=CronTrigger(hour=3, minute=0, jitter=300),  # 5-minute jitter
            id='daily_pattern_aggregation',
            name='Daily pattern aggregation (3 AM)',
            replace_existing=True
        )

        self.scheduler.start()
        logger.info("Scheduled daily aggregations at 3 AM (with 5-minute jitter)")

        # Run after short delay with jitter on startup (avoid all services starting at once)
        startup_delay = random.randint(30, 120)  # 30-120 seconds
        logger.info(f"Running initial aggregation in {startup_delay} seconds...")
        await asyncio.sleep(startup_delay)
        await self._run_daily_aggregations()

        # Keep service running
        try:
            while True:
                await asyncio.sleep(3600)  # Sleep 1 hour, scheduler handles tasks
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutting down...")
            self.scheduler.shutdown()


    async def _run_daily_aggregations(self):
        """Run all daily aggregation analyses"""
        logger.info("Starting daily pattern aggregation run...")
        start_time = datetime.now()

        insights_created = 0

        try:
            # Analysis 1: Common error sequences
            error_insights = await self._analyze_error_sequences()
            if error_insights:
                self._store_insight('error_sequences', error_insights)
                insights_created += 1

            # Analysis 2: Tool retry patterns
            retry_insights = await self._analyze_tool_retries()
            if retry_insights:
                self._store_insight('retry_analysis', retry_insights)
                insights_created += 1

            # Analysis 3: Tool performance
            tool_insights = await self._analyze_tool_performance()
            if tool_insights:
                self._store_insight('tool_performance', tool_insights)
                insights_created += 1

            # Analysis 4: Context usage patterns
            context_insights = await self._analyze_context_usage()
            if context_insights:
                self._store_insight('context_usage', context_insights)
                insights_created += 1

            # Analysis 5: Time/project correlations
            correlation_insights = await self._analyze_correlations()
            if correlation_insights:
                self._store_insight('correlations', correlation_insights)
                insights_created += 1

            # Update statistics
            self.total_runs += 1
            self.total_insights_created += insights_created

            duration = (datetime.now() - start_time).total_seconds()
            logger.info(
                f"Daily aggregation complete in {duration:.2f}s: "
                f"{insights_created} insights created"
            )

        except Exception as e:
            logger.error(f"Error running daily aggregations: {e}", exc_info=True)

    async def _analyze_error_sequences(self) -> Dict[str, Any]:
        """Find most common error sequences"""
        logger.info("Analyzing error sequences...")

        query = {
            "size": 0,
            "query": {
                "bool": {
                    "must": [
                        {"range": {"timestamp": {"gte": f"now-{self.lookback_days}d"}}},
                        {"term": {"success": False}}
                    ]
                }
            },
            "aggs": {
                "error_messages": {
                    "terms": {
                        "field": "error_message.keyword",
                        "size": 50,
                        "min_doc_count": 5
                    },
                    "aggs": {
                        "by_agent": {
                            "terms": {"field": "agent_name.keyword", "size": 10}
                        },
                        "by_project": {
                            "terms": {"field": "project.keyword", "size": 10}
                        },
                        "by_tool": {
                            "terms": {"field": "tool_name.keyword", "size": 10}
                        },
                        "avg_duration": {
                            "avg": {"field": "duration_ms"}
                        },
                        "sample_events": {
                            "top_hits": {
                                "size": 3,
                                "_source": ["timestamp", "agent_name", "tool_name", "tool_params"]
                            }
                        }
                    }
                }
            }
        }

        try:
            response = self.es.search(index="agent-logs-*", body=query)
            buckets = response['aggregations']['error_messages']['buckets']

            if not buckets:
                return None

            # Extract pattern candidates
            pattern_candidates = []
            for bucket in buckets:
                # Check if this error pattern isn't already detected
                if bucket['doc_count'] >= 10:  # Significant occurrence
                    pattern_candidates.append({
                        "type": "error_sequence",
                        "error_message": bucket['key'],
                        "frequency": bucket['doc_count'],
                        "agents": [b['key'] for b in bucket['by_agent']['buckets']],
                        "projects": [b['key'] for b in bucket['by_project']['buckets']],
                        "tools": [b['key'] for b in bucket['by_tool']['buckets']],
                        "avg_duration_ms": bucket['avg_duration']['value'],
                        "sample_events": [
                            {
                                "timestamp": hit['_source'].get('timestamp'),
                                "agent": hit['_source'].get('agent_name'),
                                "tool": hit['_source'].get('tool_name')
                            }
                            for hit in bucket['sample_events']['hits']['hits']
                        ]
                    })

            return {
                "total_errors": response['hits']['total']['value'],
                "unique_error_types": len(buckets),
                "top_errors": buckets[:10],
                "pattern_candidates": pattern_candidates
            }

        except Exception as e:
            logger.error(f"Error analyzing error sequences: {e}", exc_info=True)
            return None

    async def _analyze_tool_retries(self) -> Dict[str, Any]:
        """Find tools that frequently require retries"""
        logger.info("Analyzing tool retry patterns...")

        query = {
            "size": 0,
            "query": {
                "bool": {
                    "must": [
                        {"range": {"timestamp": {"gte": f"now-{self.lookback_days}d"}}},
                        {"range": {"retry_count": {"gt": 0}}}
                    ]
                }
            },
            "aggs": {
                "tools_needing_retry": {
                    "terms": {
                        "field": "tool_name.keyword",
                        "size": 20
                    },
                    "aggs": {
                        "avg_retries": {
                            "avg": {"field": "retry_count"}
                        },
                        "contexts": {
                            "terms": {
                                "field": "tool_params.command.keyword",
                                "size": 10
                            }
                        },
                        "error_types": {
                            "terms": {
                                "field": "error_message.keyword",
                                "size": 5
                            }
                        }
                    }
                }
            }
        }

        try:
            response = self.es.search(index="agent-logs-*", body=query)
            buckets = response['aggregations']['tools_needing_retry']['buckets']

            if not buckets:
                return None

            # Find pattern candidates
            pattern_candidates = []
            for bucket in buckets:
                if bucket['avg_retries']['value'] > 1.5:  # High retry rate
                    pattern_candidates.append({
                        "type": "retry_pattern",
                        "tool_name": bucket['key'],
                        "retry_count": bucket['doc_count'],
                        "avg_retries": bucket['avg_retries']['value'],
                        "common_commands": [b['key'] for b in bucket['contexts']['buckets']],
                        "common_errors": [b['key'] for b in bucket['error_types']['buckets']]
                    })

            return {
                "total_retries": response['hits']['total']['value'],
                "tools_with_retries": len(buckets),
                "top_retry_tools": buckets[:10],
                "pattern_candidates": pattern_candidates
            }

        except Exception as e:
            logger.error(f"Error analyzing tool retries: {e}", exc_info=True)
            return None

    async def _analyze_tool_performance(self) -> Dict[str, Any]:
        """Analyze tool performance and success rates"""
        logger.info("Analyzing tool performance...")

        query = {
            "size": 0,
            "query": {
                "range": {"timestamp": {"gte": f"now-{self.lookback_days}d"}}
            },
            "aggs": {
                "by_tool": {
                    "terms": {
                        "field": "tool_name.keyword",
                        "size": 30
                    },
                    "aggs": {
                        "success_rate": {
                            "terms": {"field": "success"}
                        },
                        "avg_duration": {
                            "avg": {"field": "duration_ms"}
                        },
                        "percentiles_duration": {
                            "percentiles": {"field": "duration_ms", "percents": [50, 90, 99]}
                        }
                    }
                }
            }
        }

        try:
            response = self.es.search(index="agent-logs-*", body=query)
            buckets = response['aggregations']['by_tool']['buckets']

            if not buckets:
                return None

            # Calculate success rates
            tool_performance = []
            for bucket in buckets:
                success_buckets = {
                    b['key_as_string']: b['doc_count']
                    for b in bucket['success_rate']['buckets']
                }
                total = bucket['doc_count']
                success_count = success_buckets.get('true', 0)
                success_rate = success_count / total if total > 0 else 0

                tool_performance.append({
                    "tool": bucket['key'],
                    "total_calls": total,
                    "success_rate": success_rate,
                    "avg_duration_ms": bucket['avg_duration']['value'],
                    "p50_duration_ms": bucket['percentiles_duration']['values']['50.0'],
                    "p90_duration_ms": bucket['percentiles_duration']['values']['90.0'],
                    "p99_duration_ms": bucket['percentiles_duration']['values']['99.0']
                })

            return {
                "tools_analyzed": len(buckets),
                "tool_performance": tool_performance
            }

        except Exception as e:
            logger.error(f"Error analyzing tool performance: {e}", exc_info=True)
            return None

    async def _analyze_context_usage(self) -> Dict[str, Any]:
        """Analyze context token usage patterns"""
        logger.info("Analyzing context usage patterns...")

        query = {
            "size": 0,
            "query": {
                "range": {"timestamp": {"gte": f"now-{self.lookback_days}d"}}
            },
            "aggs": {
                "by_agent": {
                    "terms": {
                        "field": "agent_name.keyword",
                        "size": 20
                    },
                    "aggs": {
                        "avg_context_tokens": {
                            "avg": {"field": "context_tokens"}
                        },
                        "max_context_tokens": {
                            "max": {"field": "context_tokens"}
                        },
                        "high_context_events": {
                            "filter": {
                                "range": {"context_tokens": {"gte": 180000}}
                            }
                        }
                    }
                }
            }
        }

        try:
            response = self.es.search(index="agent-logs-*", body=query)
            buckets = response['aggregations']['by_agent']['buckets']

            if not buckets:
                return None

            # Find agents with high context usage
            pattern_candidates = []
            for bucket in buckets:
                high_context_count = bucket['high_context_events']['doc_count']
                if high_context_count > 10:
                    pattern_candidates.append({
                        "type": "high_context_usage",
                        "agent_name": bucket['key'],
                        "avg_context_tokens": bucket['avg_context_tokens']['value'],
                        "max_context_tokens": bucket['max_context_tokens']['value'],
                        "high_context_events": high_context_count
                    })

            return {
                "agents_analyzed": len(buckets),
                "context_usage": buckets,
                "pattern_candidates": pattern_candidates
            }

        except Exception as e:
            logger.error(f"Error analyzing context usage: {e}", exc_info=True)
            return None

    async def _analyze_correlations(self) -> Dict[str, Any]:
        """Analyze time-of-day and project correlations"""
        logger.info("Analyzing correlations...")

        query = {
            "size": 0,
            "query": {
                "bool": {
                    "must": [
                        {"range": {"timestamp": {"gte": f"now-{self.lookback_days}d"}}},
                        {"term": {"success": False}}
                    ]
                }
            },
            "aggs": {
                "by_hour": {
                    "date_histogram": {
                        "field": "timestamp",
                        "calendar_interval": "hour"
                    },
                    "aggs": {
                        "error_rate": {
                            "terms": {"field": "success"}
                        }
                    }
                },
                "by_project": {
                    "terms": {
                        "field": "project.keyword",
                        "size": 20
                    },
                    "aggs": {
                        "error_types": {
                            "terms": {
                                "field": "error_message.keyword",
                                "size": 5
                            }
                        }
                    }
                }
            }
        }

        try:
            response = self.es.search(index="agent-logs-*", body=query)

            return {
                "hourly_distribution": response['aggregations']['by_hour']['buckets'],
                "project_errors": response['aggregations']['by_project']['buckets']
            }

        except Exception as e:
            logger.error(f"Error analyzing correlations: {e}", exc_info=True)
            return None

    def _store_insight(self, analysis_type: str, insight_data: Dict[str, Any]):
        """Store aggregated insight in Elasticsearch pattern-insights index"""
        try:
            # Extract metadata
            total_events = insight_data.get('total_errors', 0) or \
                          insight_data.get('total_retries', 0) or \
                          insight_data.get('tools_analyzed', 0) or 0

            pattern_candidates = insight_data.get('pattern_candidates', [])

            # Build insight document
            insight_doc = {
                "analysis_date": date.today().isoformat(),
                "analysis_type": analysis_type,
                "insight_data": insight_data,
                "pattern_candidates": pattern_candidates,
                "total_events_analyzed": total_events,
                "unique_sessions": insight_data.get('unique_sessions', 0),
                "unique_agents": insight_data.get('unique_agents', 0),
                "unique_projects": insight_data.get('unique_projects', 0),
                "created_at": datetime.utcnow().isoformat()
            }

            # Store in Elasticsearch (use date + type as document ID for idempotency)
            doc_id = f"{date.today().isoformat()}_{analysis_type}"

            self.es.index(
                index="pattern-insights",
                id=doc_id,
                body=insight_doc,
                refresh=True
            )

            if pattern_candidates:
                self.total_pattern_candidates += len(pattern_candidates)
                logger.info(
                    f"Stored {analysis_type} insight with "
                    f"{len(pattern_candidates)} pattern candidates"
                )

        except Exception as e:
            logger.error(f"Error storing insight: {e}", exc_info=True)

    def run_now(self):
        """Run aggregation immediately (for testing/manual trigger)"""
        logger.info("Manually triggering daily aggregation")
        asyncio.create_task(self._run_daily_aggregations())

    def get_stats(self) -> Dict[str, Any]:
        """Get aggregator statistics"""
        return {
            "total_runs": self.total_runs,
            "total_insights_created": self.total_insights_created,
            "total_pattern_candidates": self.total_pattern_candidates,
            "aggregation_interval_hours": self.aggregation_interval_hours
        }


async def main():
    """Main entry point"""
    import os

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Get configuration from environment
    elasticsearch_hosts = [
        os.getenv("ELASTICSEARCH_HOSTS", "http://elasticsearch:9200")
    ]

    # Create and run aggregator
    aggregator = PatternDailyAggregator(
        elasticsearch_hosts=elasticsearch_hosts,
        aggregation_interval_hours=int(os.getenv("AGGREGATION_INTERVAL_HOURS", "24")),
        lookback_days=int(os.getenv("LOOKBACK_DAYS", "7"))
    )

    await aggregator.run()


if __name__ == "__main__":
    asyncio.run(main())
