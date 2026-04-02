"""
Review Pattern Detector

Identifies patterns in review outcomes with high ignore rates,
leveraging existing pattern detection infrastructure.
"""

import logging
import json
from typing import Dict, Any, List, Optional
from datetime import datetime
from elasticsearch import Elasticsearch

from services.pattern_llm_analyzer import PatternLLMAnalyzer
from services.review_learning_schema import AGG_LOW_VALUE_PATTERNS
from prompts.loader import default_loader

logger = logging.getLogger(__name__)


class ReviewPatternDetector:
    """
    Detects patterns in review outcomes using Elasticsearch aggregations
    and LLM-based semantic analysis.
    """

    def __init__(
        self,
        elasticsearch_hosts: List[str] = None,
        min_sample_size: int = 10,
        ignore_rate_threshold: float = 0.6,
        confidence_threshold: float = 0.8
    ):
        if elasticsearch_hosts is None:
            elasticsearch_hosts = ["http://elasticsearch:9200"]

        self.es = Elasticsearch(elasticsearch_hosts)
        self.llm_analyzer = PatternLLMAnalyzer()
        self.min_sample_size = min_sample_size
        self.ignore_rate_threshold = ignore_rate_threshold
        self.confidence_threshold = confidence_threshold

    async def detect_low_value_patterns(
        self,
        lookback_days: int = 30
    ) -> List[Dict[str, Any]]:
        """
        Identify review feedback patterns with high ignore/reject rates.

        Args:
            lookback_days: How far back to analyze (default 30 days)

        Returns:
            List of detected low-value patterns with metadata
        """
        logger.info(f"Detecting low-value review patterns (lookback: {lookback_days}d)")

        # Build aggregation query
        query = self._build_aggregation_query(lookback_days)

        try:
            # Execute aggregation
            result = await self._execute_aggregation(query)

            # Extract patterns from aggregation results
            patterns = await self._extract_patterns_from_aggregation(result)

            logger.info(f"Detected {len(patterns)} low-value patterns")
            return patterns

        except Exception as e:
            logger.error(f"Error detecting patterns: {e}", exc_info=True)
            return []

    def _build_aggregation_query(self, lookback_days: int) -> Dict[str, Any]:
        """Build Elasticsearch aggregation query for pattern detection"""
        # Clone base query
        query = json.loads(json.dumps(AGG_LOW_VALUE_PATTERNS))

        # Update time range
        query['query']['bool']['must'][1]['range']['timestamp']['gte'] = f"now-{lookback_days}d"

        return query

    async def _execute_aggregation(self, query: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Elasticsearch aggregation query"""
        try:
            result = self.es.search(
                index="review-outcomes-*",
                body=query,
                request_timeout=30
            )
            return result
        except Exception as e:
            logger.error(f"Elasticsearch aggregation failed: {e}")
            raise

    async def _extract_patterns_from_aggregation(
        self,
        agg_result: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Extract patterns from Elasticsearch aggregation results"""
        patterns = []

        buckets = agg_result.get('aggregations', {}).get(
            'by_agent_category_severity', {}
        ).get('buckets', [])

        logger.info(f"Processing {len(buckets)} aggregation buckets")

        for bucket in buckets:
            agent = bucket['key']['agent']
            category = bucket['key']['category']
            severity = bucket['key']['severity']

            # Calculate action breakdown
            action_buckets = bucket.get('action_breakdown', {}).get('buckets', [])
            action_counts = {a['key']: a['doc_count'] for a in action_buckets}

            total = sum(action_counts.values())

            # Skip if insufficient samples
            if total < self.min_sample_size:
                logger.debug(
                    f"Skipping {agent}/{category}/{severity}: "
                    f"insufficient samples ({total} < {self.min_sample_size})"
                )
                continue

            # Calculate rates
            ignored_count = action_counts.get('ignored', 0)
            accepted_count = action_counts.get('accepted', 0) + action_counts.get('modified', 0)

            ignore_rate = ignored_count / total if total > 0 else 0
            acceptance_rate = accepted_count / total if total > 0 else 0

            # Skip if ignore rate too low
            if ignore_rate < self.ignore_rate_threshold:
                logger.debug(
                    f"Skipping {agent}/{category}/{severity}: "
                    f"ignore rate too low ({ignore_rate:.1%} < {self.ignore_rate_threshold:.1%})"
                )
                continue

            # Extract sample findings
            sample_hits = bucket.get('sample_findings', {}).get('hits', {}).get('hits', [])
            sample_findings = [
                hit['_source']['finding_message']
                for hit in sample_hits
            ]

            if not sample_findings:
                logger.warning(f"No sample findings for {agent}/{category}/{severity}")
                continue

            # Use LLM to extract semantic pattern
            logger.info(
                f"Extracting semantic pattern for {agent}/{category}/{severity} "
                f"(ignore_rate: {ignore_rate:.1%}, samples: {total})"
            )

            semantic_pattern = await self._extract_semantic_pattern(
                agent=agent,
                category=category,
                severity=severity,
                ignore_rate=ignore_rate,
                sample_findings=sample_findings
            )

            if not semantic_pattern:
                logger.warning(f"Failed to extract semantic pattern for {agent}/{category}")
                continue

            # Build pattern object
            pattern = {
                'agent': agent,
                'category': category,
                'severity': severity,
                'ignore_rate': ignore_rate,
                'acceptance_rate': acceptance_rate,
                'sample_size': total,
                'pattern_description': semantic_pattern.get('pattern_description', ''),
                'reason_ignored': semantic_pattern.get('reason_ignored', ''),
                'suggested_action': semantic_pattern.get('suggested_action', 'suppress'),
                'confidence': ignore_rate,  # Use ignore rate as confidence
                'sample_findings': sample_findings[:5],  # Keep first 5 for reference
                'detected_at': datetime.now().isoformat()
            }

            patterns.append(pattern)

        return patterns

    async def _extract_semantic_pattern(
        self,
        agent: str,
        category: str,
        severity: str,
        ignore_rate: float,
        sample_findings: List[str]
    ) -> Optional[Dict[str, Any]]:
        """
        Use LLM to extract semantic pattern from sample findings.

        Args:
            agent: Review agent name
            category: Finding category
            severity: Finding severity
            ignore_rate: Percentage of findings ignored
            sample_findings: Sample finding messages

        Returns:
            Dict with pattern_description, reason_ignored, suggested_action
        """
        examples_text = "\n".join(
            f"{i+1}. {msg}" for i, msg in enumerate(sample_findings[:10])
        )
        prompt = default_loader.workflow_template("analysis/ignored_review_pattern").format(
            agent=agent,
            category=category,
            severity=severity,
            ignore_rate=f"{ignore_rate:.1%}",
            sample_size=len(sample_findings),
            examples_text=examples_text,
        )

        try:
            result = await self.llm_analyzer.analyze_pattern(prompt)

            # Parse JSON response
            if isinstance(result, str):
                parsed = json.loads(result)
            else:
                parsed = result

            # Validate required fields
            required_fields = ['pattern_description', 'reason_ignored', 'suggested_action']
            if not all(field in parsed for field in required_fields):
                logger.warning(f"LLM response missing required fields: {parsed}")
                return None

            return parsed

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response as JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"Error extracting semantic pattern: {e}")
            return None

    async def detect_effective_patterns(
        self,
        lookback_days: int = 30,
        min_acceptance_rate: float = 0.8
    ) -> List[Dict[str, Any]]:
        """
        Identify review feedback patterns with high acceptance rates.

        These patterns represent effective review practices that should be amplified.

        Args:
            lookback_days: How far back to analyze
            min_acceptance_rate: Minimum acceptance rate to consider effective

        Returns:
            List of effective patterns
        """
        logger.info(f"Detecting effective review patterns (lookback: {lookback_days}d)")

        query = self._build_aggregation_query(lookback_days)

        try:
            result = await self._execute_aggregation(query)
            buckets = result.get('aggregations', {}).get(
                'by_agent_category_severity', {}
            ).get('buckets', [])

            effective_patterns = []

            for bucket in buckets:
                agent = bucket['key']['agent']
                category = bucket['key']['category']
                severity = bucket['key']['severity']

                action_buckets = bucket.get('action_breakdown', {}).get('buckets', [])
                action_counts = {a['key']: a['doc_count'] for a in action_buckets}

                total = sum(action_counts.values())

                if total < self.min_sample_size:
                    continue

                accepted_count = action_counts.get('accepted', 0) + action_counts.get('modified', 0)
                acceptance_rate = accepted_count / total if total > 0 else 0

                # Look for high acceptance patterns
                if acceptance_rate >= min_acceptance_rate:
                    effective_patterns.append({
                        'agent': agent,
                        'category': category,
                        'severity': severity,
                        'acceptance_rate': acceptance_rate,
                        'sample_size': total,
                        'action': 'amplify'  # These should be encouraged
                    })

            logger.info(f"Detected {len(effective_patterns)} effective patterns")
            return effective_patterns

        except Exception as e:
            logger.error(f"Error detecting effective patterns: {e}")
            return []


# Global singleton
_pattern_detector: Optional[ReviewPatternDetector] = None


def get_review_pattern_detector() -> ReviewPatternDetector:
    """Get global ReviewPatternDetector instance"""
    global _pattern_detector
    if _pattern_detector is None:
        _pattern_detector = ReviewPatternDetector()
    return _pattern_detector
