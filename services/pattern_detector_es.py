"""
Pattern Detection Service (Elasticsearch-only)

Detects anti-patterns in agent behavior by analyzing Elasticsearch logs
against configurable pattern rules. Stores all data in Elasticsearch.
"""

import asyncio
import logging
import time
import json
import yaml
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass

from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk

from services.pattern_alerting import PatternAlerter, create_alerter_from_config
from services.circuit_breaker import CircuitBreaker, CircuitBreakerOpen

logger = logging.getLogger(__name__)


@dataclass
class PatternRule:
    """Represents a pattern detection rule"""
    name: str
    description: str
    severity: str
    category: str
    detection: Dict[str, Any]
    proposed_fix: Dict[str, Any]


class PatternRuleLoader:
    """Loads pattern rules from YAML files"""

    def __init__(self, patterns_dir: str = "config/patterns"):
        self.patterns_dir = Path(patterns_dir)

    def load_all_rules(self) -> List[PatternRule]:
        """Load all pattern rules from YAML files"""
        rules = []

        if not self.patterns_dir.exists():
            logger.warning(f"Patterns directory not found: {self.patterns_dir}")
            return rules

        for yaml_file in self.patterns_dir.glob("*.yaml"):
            try:
                rules.extend(self._load_file(yaml_file))
            except Exception as e:
                logger.error(f"Error loading pattern file {yaml_file}: {e}")

        logger.info(f"Loaded {len(rules)} pattern rules")
        return rules

    def _load_file(self, file_path: Path) -> List[PatternRule]:
        """Load patterns from a single YAML file"""
        with open(file_path, 'r') as f:
            data = yaml.safe_load(f)

        rules = []
        for pattern_data in data.get('patterns', []):
            rule = PatternRule(
                name=pattern_data['name'],
                description=pattern_data['description'],
                severity=pattern_data['severity'],
                category=pattern_data['category'],
                detection=pattern_data['detection'],
                proposed_fix=pattern_data.get('proposed_fix', {})
            )
            rules.append(rule)

        return rules


class ElasticsearchQueryBuilder:
    """Builds Elasticsearch queries from pattern detection rules"""

    @staticmethod
    def build_query(rule: PatternRule, time_range_minutes: int = 5) -> Dict[str, Any]:
        """
        Build Elasticsearch query from pattern rule

        Args:
            rule: Pattern rule to build query for
            time_range_minutes: Look back this many minutes

        Returns:
            Elasticsearch query DSL
        """
        # Time range for recent events
        time_threshold = datetime.utcnow() - timedelta(minutes=time_range_minutes)

        # Build must clauses from detection rule
        must_clauses = []

        event_sequence = rule.detection.get('event_sequence', [])
        if event_sequence:
            # For now, we'll match the first event in the sequence
            # More complex multi-event matching can be added later
            event_spec = event_sequence[0]

            # Event category match
            if 'event_category' in event_spec:
                must_clauses.append({
                    "term": {"event_category": event_spec['event_category']}
                })

            # Tool name match
            if 'tool_name' in event_spec:
                must_clauses.append({
                    "term": {"tool_name": event_spec['tool_name']}
                })

            # Tool params text contains (wildcard match)
            if 'tool_params_text_contains' in event_spec:
                must_clauses.append({
                    "wildcard": {
                        "tool_params_text": f"*{event_spec['tool_params_text_contains']}*"
                    }
                })

            # Error message contains
            error_msg = event_spec.get('error_message_contains')
            if error_msg:
                # Support both single string and list of strings
                if isinstance(error_msg, list):
                    should_clauses = []
                    for msg in error_msg:
                        should_clauses.append({
                            "wildcard": {"error_message": f"*{msg}*"}
                        })
                    must_clauses.append({
                        "bool": {"should": should_clauses, "minimum_should_match": 1}
                    })
                else:
                    must_clauses.append({
                        "wildcard": {"error_message": f"*{error_msg}*"}
                    })

        # Build full query
        query = {
            "bool": {
                "must": must_clauses,
                "filter": [
                    {
                        "range": {
                            "timestamp": {
                                "gte": time_threshold.isoformat()
                            }
                        }
                    }
                ]
            }
        }

        return {
            "query": query,
            "sort": [{"timestamp": "desc"}],
            "size": 100  # Limit results
        }


class PatternDetector:
    """Main pattern detection service (Elasticsearch-only)"""

    def __init__(
        self,
        elasticsearch_hosts: List[str],
        patterns_dir: str = "config/patterns",
        detection_interval: int = 60,  # seconds
        lookback_minutes: int = 5
    ):
        """
        Initialize pattern detector

        Args:
            elasticsearch_hosts: List of Elasticsearch hosts
            patterns_dir: Directory containing pattern YAML files
            detection_interval: How often to run detection (seconds)
            lookback_minutes: How far back to look for patterns
        """
        self.es = Elasticsearch(elasticsearch_hosts)
        self.detection_interval = detection_interval
        self.lookback_minutes = lookback_minutes

        # Load pattern rules
        self.rule_loader = PatternRuleLoader(patterns_dir)
        self.rules: List[PatternRule] = []

        # Query builder
        self.query_builder = ElasticsearchQueryBuilder()

        # Alerting system
        self.alerter: Optional[PatternAlerter] = None

        # Statistics
        self.detections = 0
        self.queries_executed = 0

        # Circuit breaker for Elasticsearch queries
        self.elasticsearch_breaker = CircuitBreaker(
            name="pattern_detection_queries",
            failure_threshold=3,
            recovery_timeout=60,
            expected_exception=Exception
        )

        logger.info(f"PatternDetector initialized with {len(elasticsearch_hosts)} ES hosts")

    async def initialize(self):
        """Initialize the detector: load rules and wait for services"""
        logger.info("Initializing pattern detector...")

        # Wait for Elasticsearch
        await self._wait_for_elasticsearch()

        # Load rules from YAML
        self.rules = self.rule_loader.load_all_rules()

        # Initialize alerting system
        self._setup_alerting()

        logger.info(f"Pattern detector initialized with {len(self.rules)} rules")

    def _setup_alerting(self):
        """Setup alerting system"""
        # For now, use a simple log-based alerter
        # In production, this would load from config
        alert_config = {
            "min_severity": "high",
            "channels": [
                {"type": "log", "enabled": True}
            ]
        }

        self.alerter = create_alerter_from_config(alert_config)
        logger.info("Alerting system initialized")

    async def _wait_for_elasticsearch(self):
        """Wait for Elasticsearch to be ready"""
        logger.info("Waiting for Elasticsearch to be ready...")

        max_retries = 30
        for i in range(max_retries):
            try:
                self.es.ping()
                logger.info("Elasticsearch is ready")
                return
            except:
                if i == max_retries - 1:
                    raise Exception("Elasticsearch not available after 30 retries")
                await asyncio.sleep(1)

    async def run(self):
        """Main detection loop"""
        logger.info("Starting pattern detection service...")

        while True:
            try:
                await self.detect_patterns()
                await asyncio.sleep(self.detection_interval)
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                break
            except Exception as e:
                logger.error(f"Error in detection loop: {e}", exc_info=True)
                await asyncio.sleep(10)  # Back off on errors

    async def detect_patterns(self):
        """Run pattern detection for all rules"""
        start_time = time.time()
        detections_this_run = 0

        for rule in self.rules:
            try:
                # Wrap each pattern detection in circuit breaker
                count = await self.elasticsearch_breaker.call(self._detect_pattern, rule)
                detections_this_run += count
            except CircuitBreakerOpen as e:
                logger.debug(f"Pattern detection circuit open, skipping rule {rule.name}: {e}")
            except Exception as e:
                logger.error(f"Error detecting pattern {rule.name}: {e}", exc_info=True)

        duration = time.time() - start_time
        # Only log if patterns were detected (reduce noise)
        if detections_this_run > 0:
            logger.info(
                f"Detection run complete: {detections_this_run} patterns detected "
                f"across {len(self.rules)} rules in {duration:.2f}s"
            )
        else:
            logger.debug(
                f"Detection run complete: no patterns detected in {duration:.2f}s"
            )

    async def _detect_pattern(self, rule: PatternRule) -> int:
        """
        Detect a specific pattern

        Returns:
            Number of new occurrences detected
        """
        # Build query
        query = self.query_builder.build_query(rule, self.lookback_minutes)
        self.queries_executed += 1

        # Execute search against agent logs
        try:
            response = self.es.search(
                index="agent-logs-*",
                body=query
            )
        except Exception as e:
            logger.error(f"Elasticsearch query failed for pattern {rule.name}: {e}")
            return 0

        hits = response['hits']['hits']
        if not hits:
            return 0

        logger.debug(f"Pattern '{rule.name}' matched {len(hits)} events")

        # Store occurrences in Elasticsearch
        return await self._store_occurrences(rule, hits, query)

    async def _store_occurrences(
        self,
        rule: PatternRule,
        hits: List[Dict],
        query: Dict
    ) -> int:
        """Store pattern occurrences in Elasticsearch pattern-occurrences index"""
        actions = []
        new_occurrences = 0

        for hit in hits:
            source = hit['_source']
            event_id = hit['_id']

            # Check if we've already recorded this event for this pattern
            # Query pattern-occurrences for this event_id + pattern_name combination
            existing_query = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"pattern_name": rule.name}},
                            {"term": {"event_ids": event_id}}
                        ]
                    }
                },
                "size": 1
            }

            try:
                existing = self.es.search(
                    index="pattern-occurrences",
                    body=existing_query
                )

                if existing['hits']['total']['value'] > 0:
                    continue  # Already recorded
            except Exception as e:
                logger.error(f"Error checking for existing occurrence: {e}")
                continue

            # Create new occurrence document
            occurrence = {
                "pattern_name": rule.name,
                "pattern_category": rule.category,
                "severity": rule.severity,

                # Context
                "session_id": source.get('session_id'),
                "agent_name": source.get('agent_name'),
                "project": source.get('project'),
                "task_id": source.get('task_id'),

                # Event reference
                "event_ids": [event_id],
                "event_timestamp": source.get('timestamp'),

                # Impact
                "duration_ms": source.get('duration_ms'),
                "error_message": source.get('error_message'),

                # Resolution
                "resolved": False,

                # Detection metadata
                "detected_at": datetime.utcnow().isoformat() + 'Z',
                "detection_rule": rule.detection,
                "elasticsearch_query": query
            }

            # Prepare for bulk insert
            actions.append({
                "_index": "pattern-occurrences",
                "_source": occurrence
            })

            new_occurrences += 1

        # Bulk insert new occurrences
        if actions:
            try:
                success, failed = bulk(self.es, actions, refresh=True)
                self.detections += success

                logger.info(
                    f"Stored {success} new occurrences of pattern '{rule.name}'"
                )

                # Send alerts for new occurrences
                if self.alerter and success > 0:
                    await self._send_alerts(rule, actions)

            except Exception as e:
                logger.error(f"Error bulk indexing occurrences: {e}", exc_info=True)

        return new_occurrences

    async def _send_alerts(self, rule: PatternRule, occurrence_docs: List[Dict]):
        """Send alerts for detected pattern occurrences"""
        for doc in occurrence_docs:
            source = doc['_source']

            pattern_dict = {
                "pattern_name": rule.name,
                "description": rule.description,
                "severity": rule.severity,
                "pattern_category": rule.category
            }

            occurrence_dict = {
                "agent_name": source.get('agent_name'),
                "project": source.get('project'),
                "error_message": source.get('error_message'),
                "event_timestamp": source.get('event_timestamp')
            }

            # Note: alerter now doesn't need DB connection
            self.alerter.send_alert(pattern_dict, occurrence_dict)

    def get_stats(self) -> Dict[str, Any]:
        """Get detector statistics including circuit breaker state"""
        return {
            "rules_loaded": len(self.rules),
            "total_detections": self.detections,
            "queries_executed": self.queries_executed,
            "detection_interval_seconds": self.detection_interval,
            "lookback_minutes": self.lookback_minutes,
            "circuit_breaker": self.elasticsearch_breaker.get_state()
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
    es_hosts = os.getenv("ELASTICSEARCH_HOSTS", "http://elasticsearch:9200").split(",")

    # Create and run detector
    detector = PatternDetector(
        elasticsearch_hosts=es_hosts,
        detection_interval=int(os.getenv("DETECTION_INTERVAL", "60")),
        lookback_minutes=int(os.getenv("LOOKBACK_MINUTES", "5"))
    )

    await detector.initialize()
    await detector.run()


if __name__ == "__main__":
    asyncio.run(main())
