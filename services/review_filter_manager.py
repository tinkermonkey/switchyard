"""
Review Filter Manager

Manages learned review filters and provides them to agents for prompt injection.
"""

import logging
import hashlib
from typing import Dict, Any, List, Optional
from datetime import datetime
from elasticsearch import Elasticsearch
import redis

logger = logging.getLogger(__name__)


class ReviewFilterManager:
    """
    Manages learned review filters stored in Elasticsearch.

    Provides CRUD operations, filter retrieval, and effectiveness tracking.
    """

    def __init__(
        self,
        elasticsearch_hosts: List[str] = None,
        redis_host: str = "redis",
        redis_port: int = 6379
    ):
        if elasticsearch_hosts is None:
            elasticsearch_hosts = ["http://elasticsearch:9200"]

        self.es = Elasticsearch(elasticsearch_hosts)
        self.redis = redis.Redis(
            host=redis_host,
            port=redis_port,
            decode_responses=True
        )

        self.filters_index = "review-filters"

    def _ensure_index_exists(self) -> bool:
        """
        Ensure the review-filters index exists, creating it if necessary.

        Returns:
            True if index exists or was created, False if creation failed
        """
        try:
            if not self.es.indices.exists(index=self.filters_index):
                logger.info(f"Creating {self.filters_index} index...")
                from services.review_learning_schema import REVIEW_FILTERS_INDEX

                self.es.indices.create(
                    index=self.filters_index,
                    body=REVIEW_FILTERS_INDEX
                )
                logger.info(f"{self.filters_index} index created successfully")
            return True
        except Exception as e:
            logger.warning(f"Could not ensure {self.filters_index} index exists: {e}")
            return False

    async def create_filter(self, filter_data: Dict[str, Any]) -> str:
        """
        Create a new review filter.

        Args:
            filter_data: Filter configuration

        Returns:
            Filter ID
        """
        # Generate filter ID based on agent, category, and pattern
        filter_id = self._generate_filter_id(
            agent=filter_data['agent'],
            category=filter_data['category'],
            pattern=filter_data['pattern_description']
        )

        # Add metadata
        filter_doc = {
            'filter_id': filter_id,
            'created_at': datetime.now().isoformat(),
            'last_updated': datetime.now().isoformat(),
            'applications_count': 0,
            'correct_suppressions': 0,
            'incorrect_suppressions': 0,
            **filter_data
        }

        try:
            # Index to Elasticsearch
            self.es.index(
                index=self.filters_index,
                id=filter_id,
                document=filter_doc
            )

            # Invalidate cache
            self._invalidate_cache(filter_data['agent'])

            logger.info(f"Created filter: {filter_id} for {filter_data['agent']}/{filter_data['category']}")
            return filter_id

        except Exception as e:
            logger.error(f"Error creating filter: {e}")
            raise

    async def update_filter_stats(
        self,
        filter_id: str,
        new_stats: Dict[str, Any]
    ) -> bool:
        """
        Update filter statistics.

        Args:
            filter_id: Filter to update
            new_stats: Statistics to update

        Returns:
            True if successful
        """
        try:
            # Update document
            update_body = {
                'doc': {
                    'last_updated': datetime.now().isoformat(),
                    **new_stats
                }
            }

            self.es.update(
                index=self.filters_index,
                id=filter_id,
                body=update_body
            )

            logger.info(f"Updated filter stats: {filter_id}")
            return True

        except Exception as e:
            logger.error(f"Error updating filter stats: {e}")
            return False

    async def get_agent_filters(
        self,
        agent_name: str,
        min_confidence: float = 0.8,
        active_only: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Get active filters for a specific agent.

        Args:
            agent_name: Agent to get filters for
            min_confidence: Minimum confidence threshold
            active_only: Only return active filters

        Returns:
            List of filter configurations (empty if index doesn't exist)
        """
        # Check cache first
        cache_key = f"filters:{agent_name}:{min_confidence}:{active_only}"
        cached = self.redis.get(cache_key)

        if cached:
            import json
            return json.loads(cached)

        # Ensure index exists (lazy initialization)
        if not self._ensure_index_exists():
            # Index doesn't exist and couldn't be created - return empty
            logger.debug(f"Review filters index not available, returning empty filters for {agent_name}")
            return []

        try:
            # Build query
            must_clauses = [
                {"term": {"agent.keyword": agent_name}},
                {"range": {"confidence": {"gte": min_confidence}}}
            ]

            if active_only:
                must_clauses.append({"term": {"active": True}})

            query = {
                "query": {
                    "bool": {
                        "must": must_clauses
                    }
                },
                "sort": [
                    {"confidence": "desc"},
                    {"sample_size": "desc"}
                ],
                "size": 50
            }

            # Execute query
            result = self.es.search(
                index=self.filters_index,
                body=query
            )

            filters = [hit['_source'] for hit in result['hits']['hits']]

            # Cache results for 5 minutes
            import json
            self.redis.setex(cache_key, 300, json.dumps(filters))

            if filters:
                logger.info(f"Retrieved {len(filters)} filters for {agent_name}")
            else:
                logger.debug(f"No filters found for {agent_name}")

            return filters

        except Exception as e:
            logger.warning(f"Error querying agent filters for {agent_name}: {e}")
            return []

    async def get_filter_by_pattern(
        self,
        agent: str,
        category: str,
        pattern_sig: str
    ) -> Optional[Dict[str, Any]]:
        """
        Check if a filter exists for a specific pattern.

        Args:
            agent: Agent name
            category: Finding category
            pattern_sig: Pattern signature/description

        Returns:
            Filter if exists, None otherwise
        """
        # Ensure index exists (lazy initialization)
        if not self._ensure_index_exists():
            logger.debug("Review filters index not available")
            return None

        filter_id = self._generate_filter_id(agent, category, pattern_sig)

        try:
            result = self.es.get(
                index=self.filters_index,
                id=filter_id
            )
            return result['_source']

        except Exception:
            # Filter doesn't exist
            return None

    async def deactivate_filter(self, filter_id: str) -> bool:
        """
        Deactivate a filter without deleting it.

        Args:
            filter_id: Filter to deactivate

        Returns:
            True if successful
        """
        try:
            self.es.update(
                index=self.filters_index,
                id=filter_id,
                body={
                    'doc': {
                        'active': False,
                        'last_updated': datetime.now().isoformat()
                    }
                }
            )

            logger.info(f"Deactivated filter: {filter_id}")
            return True

        except Exception as e:
            logger.error(f"Error deactivating filter: {e}")
            return False

    async def prune_stale_filters(
        self,
        max_age_days: int = 90,
        min_effectiveness: float = 0.5
    ) -> int:
        """
        Deactivate filters that are stale or ineffective.

        Args:
            max_age_days: Maximum age for filters
            min_effectiveness: Minimum effectiveness ratio

        Returns:
            Number of filters pruned
        """
        logger.info(f"Pruning stale filters (age > {max_age_days}d, effectiveness < {min_effectiveness})")

        # Ensure index exists (lazy initialization)
        if not self._ensure_index_exists():
            logger.debug("Review filters index not available")
            return 0

        try:
            # Query for stale filters
            query = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"active": True}}
                        ],
                        "should": [
                            # Old filters
                            {
                                "range": {
                                    "created_at": {
                                        "lt": f"now-{max_age_days}d"
                                    }
                                }
                            },
                            # Ineffective filters (more incorrect than correct suppressions)
                            {
                                "script": {
                                    "script": {
                                        "source": """
                                            def total = doc['correct_suppressions'].value + doc['incorrect_suppressions'].value;
                                            if (total > 10) {
                                                def effectiveness = doc['correct_suppressions'].value / total;
                                                return effectiveness < params.threshold;
                                            }
                                            return false;
                                        """,
                                        "params": {
                                            "threshold": min_effectiveness
                                        }
                                    }
                                }
                            }
                        ],
                        "minimum_should_match": 1
                    }
                }
            }

            result = self.es.search(
                index=self.filters_index,
                body=query,
                size=100
            )

            # Deactivate stale filters
            pruned_count = 0
            for hit in result['hits']['hits']:
                filter_id = hit['_id']
                await self.deactivate_filter(filter_id)
                pruned_count += 1

            logger.info(f"Pruned {pruned_count} stale filters")
            return pruned_count

        except Exception as e:
            logger.error(f"Error pruning filters: {e}")
            return 0

    async def get_filter_metrics(self) -> Dict[str, Any]:
        """
        Get overall filter system metrics.

        Returns:
            Dict with metrics
        """
        # Ensure index exists (lazy initialization)
        if not self._ensure_index_exists():
            logger.debug("Review filters index not available")
            return {}

        try:
            # Aggregate metrics
            query = {
                "size": 0,
                "aggs": {
                    "total_filters": {"value_count": {"field": "filter_id.keyword"}},
                    "active_filters": {
                        "filter": {"term": {"active": True}},
                        "aggs": {
                            "count": {"value_count": {"field": "filter_id.keyword"}}
                        }
                    },
                    "by_agent": {
                        "terms": {"field": "agent.keyword", "size": 20},
                        "aggs": {
                            "avg_confidence": {"avg": {"field": "confidence"}},
                            "total_applications": {"sum": {"field": "applications_count"}}
                        }
                    },
                    "by_action": {
                        "terms": {"field": "action.keyword"}
                    },
                    "avg_confidence": {"avg": {"field": "confidence"}},
                    "total_applications": {"sum": {"field": "applications_count"}},
                    "total_correct": {"sum": {"field": "correct_suppressions"}},
                    "total_incorrect": {"sum": {"field": "incorrect_suppressions"}}
                }
            }

            result = self.es.search(
                index=self.filters_index,
                body=query
            )

            aggs = result['aggregations']

            total_correct = aggs['total_correct']['value']
            total_incorrect = aggs['total_incorrect']['value']
            total_decisions = total_correct + total_incorrect

            precision = total_correct / total_decisions if total_decisions > 0 else 0

            metrics = {
                'total_filters': aggs['total_filters']['value'],
                'active_filters': aggs['active_filters']['count']['value'],
                'avg_confidence': aggs['avg_confidence']['value'],
                'total_applications': aggs['total_applications']['value'],
                'precision': precision,
                'by_agent': {
                    bucket['key']: {
                        'count': bucket['doc_count'],
                        'avg_confidence': bucket['avg_confidence']['value'],
                        'total_applications': bucket['total_applications']['value']
                    }
                    for bucket in aggs['by_agent']['buckets']
                },
                'by_action': {
                    bucket['key']: bucket['doc_count']
                    for bucket in aggs['by_action']['buckets']
                }
            }

            return metrics

        except Exception as e:
            logger.error(f"Error getting filter metrics: {e}")
            return {}

    def _generate_filter_id(self, agent: str, category: str, pattern: str) -> str:
        """Generate deterministic filter ID"""
        composite_key = f"{agent}:{category}:{pattern}"
        hash_digest = hashlib.md5(composite_key.encode()).hexdigest()
        return f"filter_{hash_digest[:12]}"

    def _invalidate_cache(self, agent_name: str):
        """Invalidate cached filters for an agent"""
        pattern = f"filters:{agent_name}:*"
        keys = self.redis.keys(pattern)
        if keys:
            self.redis.delete(*keys)

    def build_filter_instructions(self, filters: List[Dict[str, Any]]) -> str:
        """
        Convert learned filters into prompt instructions for agents.

        Args:
            filters: List of filter configurations

        Returns:
            Formatted prompt instructions
        """
        if not filters:
            return ""

        instructions = """
## Review Focus Areas (Learned from Historical Feedback)

Based on analysis of past review cycles, focus your review on high-value findings
and avoid the following low-value patterns that developers typically ignore:

"""

        # Group filters by action
        suppress_filters = [f for f in filters if f.get('action') == 'suppress']
        adjust_filters = [f for f in filters if f.get('action') == 'adjust_severity']
        highlight_filters = [f for f in filters if f.get('action') == 'highlight']

        # Add highlight rules (CHECK THESE FIRST)
        if highlight_filters:
            instructions += "### High-Priority Checks (IMPORTANT)\n\n"
            instructions += "**Pay special attention to these areas** based on historical issues:\n\n"
            for f in highlight_filters[:5]:  # Limit to top 5
                instructions += f"""
**{f['category']} - {f['severity']}**
- Pattern: {f['pattern_description']}
- Why important: {f.get('reason_ignored', 'High acceptance rate indicates this catches real issues')}
- Confidence: {f['confidence']:.0%} (based on {f['sample_size']} samples)
- Example: {f.get('sample_findings', ['See pattern description'])[0] if f.get('sample_findings') else 'N/A'}

"""

        # Add suppression rules
        if suppress_filters:
            instructions += "### Patterns to Suppress\n\n"
            for f in suppress_filters[:10]:  # Limit to top 10
                instructions += f"""
**{f['category']} - {f['severity']}**
- Pattern: {f['pattern_description']}
- Reason: {f['reason_ignored']}
- Confidence: {f['confidence']:.0%} (based on {f['sample_size']} samples)

"""

        # Add severity adjustment rules
        if adjust_filters:
            instructions += "\n### Severity Adjustments\n\n"
            for f in adjust_filters[:5]:  # Limit to top 5
                instructions += f"""
**{f['category']}**: Adjust severity from {f.get('from_severity', 'N/A')} to {f.get('to_severity', 'N/A')}
- Reason: {f['reason_ignored']}

"""

        instructions += """
Focus your review effort on categories with high historical acceptance rates:
security, logic errors, and architectural concerns.
"""

        return instructions


# Global singleton
_filter_manager: Optional[ReviewFilterManager] = None


def get_review_filter_manager() -> ReviewFilterManager:
    """Get global ReviewFilterManager instance"""
    global _filter_manager
    if _filter_manager is None:
        _filter_manager = ReviewFilterManager()
    return _filter_manager
