"""
Pattern Similarity Analyzer Service

Analyzes patterns for similarity to identify consolidation opportunities.
Helps prevent CLAUDE.md bloat by merging similar patterns.
"""

import logging
import asyncio
import random
from typing import Dict, Any, List, Tuple
from datetime import datetime
from elasticsearch import Elasticsearch
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


class PatternSimilarityAnalyzer:
    """Analyzes pattern similarity for consolidation opportunities"""

    def __init__(
        self,
        elasticsearch_hosts: List[str],
        analysis_interval_hours: int = 24,  # Daily
        similarity_threshold: float = 0.75,  # 75% similarity
        min_occurrences_threshold: int = 5
    ):
        """
        Initialize similarity analyzer

        Args:
            elasticsearch_hosts: List of Elasticsearch hosts
            analysis_interval_hours: Hours between analysis runs
            similarity_threshold: Minimum similarity score (0-1) to flag for consolidation
            min_occurrences_threshold: Minimum occurrences to consider
        """
        self.es = Elasticsearch(elasticsearch_hosts)
        self.analysis_interval_hours = analysis_interval_hours
        self.similarity_threshold = similarity_threshold
        self.min_occurrences_threshold = min_occurrences_threshold

        # Scheduler
        self.scheduler = AsyncIOScheduler()

        # Statistics
        self.total_runs = 0
        self.total_comparisons = 0
        self.total_similarities_found = 0

        logger.info(
            f"PatternSimilarityAnalyzer initialized "
            f"(interval={analysis_interval_hours}h, threshold={similarity_threshold})"
        )

    async def run(self):
        """Start the scheduler"""
        logger.info("Starting Pattern Similarity Analyzer service...")

        # Schedule similarity analysis using APScheduler with cron trigger
        # Run daily at 5 AM (staggered from aggregator at 3 AM, LLM at 4 AM on Sundays)
        self.scheduler.add_job(
            self._run_similarity_analysis,
            trigger=CronTrigger(hour=5, minute=0, jitter=300),  # 5-minute jitter
            id='pattern_similarity_analysis',
            name='Pattern similarity analysis (daily 5 AM)',
            replace_existing=True
        )

        self.scheduler.start()
        logger.info("Scheduled similarity analysis for daily at 5 AM (with 5-minute jitter)")

        # Startup delay with jitter to avoid thundering herd
        startup_delay = random.randint(45, 150)  # 45-150 seconds
        logger.info(f"Waiting {startup_delay}s before initial similarity analysis")
        await asyncio.sleep(startup_delay)

        # Run initial analysis on startup
        await self._run_similarity_analysis()

        # Keep service running
        try:
            while True:
                await asyncio.sleep(3600)  # Sleep 1 hour, scheduler handles tasks
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutting down...")
            self.scheduler.shutdown()

    async def _run_similarity_analysis(self):
        """Run similarity analysis on all patterns"""
        logger.info("Starting pattern similarity analysis run...")
        start_time = datetime.now()

        similarities_found = 0

        try:
            # Get all patterns
            patterns = self._get_all_patterns()
            logger.info(f"Analyzing similarity for {len(patterns)} patterns")

            # Compare all pairs
            for i, pattern_a in enumerate(patterns):
                for pattern_b in patterns[i+1:]:
                    try:
                        similarity = self._calculate_similarity(pattern_a, pattern_b)
                        self.total_comparisons += 1

                        if similarity >= self.similarity_threshold:
                            self._store_similarity(pattern_a, pattern_b, similarity)
                            similarities_found += 1
                            logger.info(
                                f"Found similar patterns: '{pattern_a['pattern_name']}' "
                                f"and '{pattern_b['pattern_name']}' "
                                f"(similarity={similarity:.2f})"
                            )

                    except Exception as e:
                        logger.error(f"Error comparing patterns: {e}")

            # Update statistics
            self.total_runs += 1
            self.total_similarities_found += similarities_found

            duration = (datetime.now() - start_time).total_seconds()
            logger.info(
                f"Similarity analysis complete in {duration:.2f}s: "
                f"{self.total_comparisons} comparisons, "
                f"{similarities_found} similar pairs found"
            )

        except Exception as e:
            logger.error(f"Error running similarity analysis: {e}", exc_info=True)

    def _get_all_patterns(self) -> List[Dict[str, Any]]:
        """Get all patterns for comparison"""
        agg_query = {
            "size": 0,
            "aggs": {
                "by_pattern": {
                    "terms": {
                        "field": "pattern_name",
                        "size": 100,
                        "min_doc_count": self.min_occurrences_threshold
                    },
                    "aggs": {
                        "projects": {"terms": {"field": "project", "size": 20}},
                        "agents": {"terms": {"field": "agent_name", "size": 20}},
                        "category": {"terms": {"field": "pattern_category", "size": 1}},
                        "error_samples": {
                            "top_hits": {
                                "size": 3,
                                "_source": ["error_message"]
                            }
                        }
                    }
                }
            }
        }

        try:
            response = self.es.search(index="pattern-occurrences", body=agg_query)
        except Exception as e:
            logger.error(f"Error querying patterns: {e}")
            return []

        patterns = []
        for bucket in response['aggregations']['by_pattern']['buckets']:
            # Get error messages for similarity comparison
            error_messages = [
                hit['_source'].get('error_message', '')
                for hit in bucket['error_samples']['hits']['hits']
            ]

            patterns.append({
                "pattern_name": bucket['key'],
                "occurrence_count": bucket['doc_count'],
                "category": bucket['category']['buckets'][0]['key'] if bucket['category']['buckets'] else 'general',
                "projects": [b['key'] for b in bucket['projects']['buckets']],
                "agents": [b['key'] for b in bucket['agents']['buckets']],
                "error_messages": error_messages
            })

        return patterns

    def _calculate_similarity(
        self,
        pattern_a: Dict[str, Any],
        pattern_b: Dict[str, Any]
    ) -> float:
        """
        Calculate similarity score between two patterns

        Uses multiple signals:
        - Pattern name similarity (text similarity)
        - Category match
        - Common projects
        - Common agents
        - Error message similarity

        Returns:
            Similarity score from 0 to 1
        """
        scores = []

        # 1. Pattern name similarity (40% weight)
        name_sim = SequenceMatcher(
            None,
            pattern_a['pattern_name'].lower(),
            pattern_b['pattern_name'].lower()
        ).ratio()
        scores.append(('name', name_sim, 0.40))

        # 2. Category match (20% weight)
        category_match = 1.0 if pattern_a.get('category') == pattern_b.get('category') else 0.0
        scores.append(('category', category_match, 0.20))

        # 3. Common projects (15% weight)
        projects_a = set(pattern_a.get('projects', []))
        projects_b = set(pattern_b.get('projects', []))
        if projects_a and projects_b:
            project_overlap = len(projects_a & projects_b) / len(projects_a | projects_b)
        else:
            project_overlap = 0.0
        scores.append(('projects', project_overlap, 0.15))

        # 4. Common agents (15% weight)
        agents_a = set(pattern_a.get('agents', []))
        agents_b = set(pattern_b.get('agents', []))
        if agents_a and agents_b:
            agent_overlap = len(agents_a & agents_b) / len(agents_a | agents_b)
        else:
            agent_overlap = 0.0
        scores.append(('agents', agent_overlap, 0.15))

        # 5. Error message similarity (10% weight)
        error_sim = self._calculate_error_similarity(
            pattern_a.get('error_messages', []),
            pattern_b.get('error_messages', [])
        )
        scores.append(('errors', error_sim, 0.10))

        # Weighted average
        total_score = sum(score * weight for _, score, weight in scores)

        logger.debug(
            f"Similarity between '{pattern_a['pattern_name']}' and '{pattern_b['pattern_name']}': "
            f"{total_score:.2f} (name={name_sim:.2f}, cat={category_match:.2f}, "
            f"proj={project_overlap:.2f}, agents={agent_overlap:.2f}, err={error_sim:.2f})"
        )

        return total_score

    def _calculate_error_similarity(
        self,
        errors_a: List[str],
        errors_b: List[str]
    ) -> float:
        """Calculate similarity between error message lists"""
        if not errors_a or not errors_b:
            return 0.0

        # Compare each error message from A with each from B
        similarities = []
        for err_a in errors_a:
            for err_b in errors_b:
                sim = SequenceMatcher(None, err_a.lower(), err_b.lower()).ratio()
                similarities.append(sim)

        return max(similarities) if similarities else 0.0

    def _store_similarity(
        self,
        pattern_a: Dict[str, Any],
        pattern_b: Dict[str, Any],
        similarity_score: float
    ):
        """Store similarity finding in Elasticsearch"""
        try:
            # Find common projects and agents
            common_projects = list(set(pattern_a.get('projects', [])) & set(pattern_b.get('projects', [])))
            common_agents = list(set(pattern_a.get('agents', [])) & set(pattern_b.get('agents', [])))

            # Determine if should consolidate
            should_consolidate = similarity_score >= 0.85  # Very high similarity

            # Calculate consolidation priority (higher = more important)
            priority = int(similarity_score * 100)

            doc = {
                "pattern_a_name": pattern_a['pattern_name'],
                "pattern_b_name": pattern_b['pattern_name'],

                # Similarity metrics
                "similarity_score": similarity_score,
                "similarity_method": "multi_signal",

                # Analysis
                "common_projects": common_projects,
                "common_agents": common_agents,
                "should_consolidate": should_consolidate,
                "consolidation_priority": priority,

                # Metadata
                "computed_at": datetime.utcnow().isoformat() + 'Z'
            }

            # Use pattern names as doc ID for idempotency
            names_sorted = sorted([pattern_a['pattern_name'], pattern_b['pattern_name']])
            doc_id = f"{names_sorted[0]}___{names_sorted[1]}"

            self.es.index(
                index="pattern-similarity",
                id=doc_id,
                body=doc,
                refresh=True
            )

            logger.info(
                f"Stored similarity: {pattern_a['pattern_name']} <-> {pattern_b['pattern_name']} "
                f"(score={similarity_score:.2f}, consolidate={should_consolidate})"
            )

        except Exception as e:
            logger.error(f"Error storing similarity: {e}", exc_info=True)

    def run_now(self):
        """Manual trigger for similarity analysis (for testing/debugging)"""
        logger.info("Manually triggering similarity analysis")
        asyncio.create_task(self._run_similarity_analysis())

    def get_stats(self) -> Dict[str, Any]:
        """Get analyzer statistics"""
        return {
            "total_runs": self.total_runs,
            "total_comparisons": self.total_comparisons,
            "total_similarities_found": self.total_similarities_found,
            "analysis_interval_hours": self.analysis_interval_hours,
            "similarity_threshold": self.similarity_threshold
        }


async def main():
    """Main entry point"""
    import os

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Get configuration
    elasticsearch_hosts = [
        os.getenv("ELASTICSEARCH_HOSTS", "http://elasticsearch:9200")
    ]

    # Create and run analyzer
    analyzer = PatternSimilarityAnalyzer(
        elasticsearch_hosts=elasticsearch_hosts,
        analysis_interval_hours=int(os.getenv("SIMILARITY_ANALYSIS_INTERVAL_HOURS", "24")),
        similarity_threshold=float(os.getenv("SIMILARITY_THRESHOLD", "0.75")),
        min_occurrences_threshold=int(os.getenv("MIN_OCCURRENCES_FOR_SIMILARITY", "5"))
    )

    await analyzer.run()


if __name__ == "__main__":
    asyncio.run(main())
