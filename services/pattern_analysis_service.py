"""
Pattern Analysis Service (Consolidated)

Single service that runs all pattern analysis tasks:
- Daily aggregation (statistical analysis)
- Weekly LLM meta-analysis (Claude API)
- Daily similarity analysis (pattern clustering)

Uses a single AsyncIOScheduler to manage all three jobs efficiently.
"""

import logging
import asyncio
import os
from typing import Dict, Any
from elasticsearch import Elasticsearch
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Import the three analyzers
from services.pattern_daily_aggregator_es import PatternDailyAggregator
from services.pattern_llm_analyzer import PatternLLMAnalyzer
from services.pattern_similarity_analyzer import PatternSimilarityAnalyzer

logger = logging.getLogger(__name__)


class PatternAnalysisService:
    """Consolidated pattern analysis service managing all three analyzers"""

    def __init__(
        self,
        elasticsearch_hosts: list[str],
        anthropic_api_key: str = None,
        # Daily aggregator config
        aggregation_interval_hours: int = 24,
        lookback_days: int = 7,
        # LLM analyzer config
        llm_analysis_interval_hours: int = 168,
        min_occurrences_for_llm: int = 20,
        max_patterns_per_llm_run: int = 5,
        # Similarity analyzer config
        similarity_analysis_interval_hours: int = 24,
        similarity_threshold: float = 0.75,
        min_occurrences_for_similarity: int = 5
    ):
        """
        Initialize consolidated pattern analysis service

        Args:
            elasticsearch_hosts: List of Elasticsearch hosts
            anthropic_api_key: Anthropic API key for Claude (required for LLM analysis)
            aggregation_interval_hours: Hours between daily aggregations
            lookback_days: Days of history to analyze
            llm_analysis_interval_hours: Hours between LLM analysis runs
            min_occurrences_for_llm: Minimum occurrences for LLM analysis
            max_patterns_per_llm_run: Maximum patterns per LLM run
            similarity_analysis_interval_hours: Hours between similarity runs
            similarity_threshold: Minimum similarity score for flagging
            min_occurrences_for_similarity: Minimum occurrences for similarity
        """
        # Initialize Elasticsearch client (shared by all analyzers)
        self.es = Elasticsearch(elasticsearch_hosts)

        # Initialize daily aggregator
        self.daily_aggregator = PatternDailyAggregator(
            elasticsearch_hosts=elasticsearch_hosts,
            aggregation_interval_hours=aggregation_interval_hours,
            lookback_days=lookback_days
        )

        # Initialize LLM analyzer (if API key provided)
        self.llm_analyzer = None
        if anthropic_api_key:
            self.llm_analyzer = PatternLLMAnalyzer(
                elasticsearch_hosts=elasticsearch_hosts,
                anthropic_api_key=anthropic_api_key,
                analysis_interval_hours=llm_analysis_interval_hours,
                min_occurrences_for_analysis=min_occurrences_for_llm,
                max_patterns_per_run=max_patterns_per_llm_run
            )
        else:
            logger.warning("No ANTHROPIC_API_KEY provided - LLM analysis disabled")

        # Initialize similarity analyzer
        self.similarity_analyzer = PatternSimilarityAnalyzer(
            elasticsearch_hosts=elasticsearch_hosts,
            analysis_interval_hours=similarity_analysis_interval_hours,
            similarity_threshold=similarity_threshold,
            min_occurrences_threshold=min_occurrences_for_similarity
        )

        # Single scheduler for all jobs
        self.scheduler = AsyncIOScheduler()

        logger.info("PatternAnalysisService initialized with all three analyzers")

    async def run(self):
        """Start the consolidated service with all three schedulers"""
        import random

        logger.info("Starting consolidated Pattern Analysis Service...")

        # Register daily aggregator job (3 AM daily with 5-min jitter)
        self.scheduler.add_job(
            self.daily_aggregator._run_daily_aggregations,
            trigger=CronTrigger(hour=3, minute=0, jitter=300),
            id='daily_pattern_aggregation',
            name='Daily pattern aggregation (3 AM)',
            replace_existing=True
        )
        logger.info("Registered daily aggregation job (3 AM with 5-min jitter)")

        # Register LLM analyzer job (Sundays 4 AM with 10-min jitter) if enabled
        if self.llm_analyzer:
            self.scheduler.add_job(
                self.llm_analyzer._run_llm_analysis,
                trigger=CronTrigger(day_of_week='sun', hour=4, minute=0, jitter=600),
                id='llm_pattern_analysis',
                name='LLM pattern analysis (Sunday 4 AM)',
                replace_existing=True
            )
            logger.info("Registered LLM analysis job (Sunday 4 AM with 10-min jitter)")

        # Register similarity analyzer job (5 AM daily with 5-min jitter)
        self.scheduler.add_job(
            self.similarity_analyzer._run_similarity_analysis,
            trigger=CronTrigger(hour=5, minute=0, jitter=300),
            id='pattern_similarity_analysis',
            name='Pattern similarity analysis (5 AM)',
            replace_existing=True
        )
        logger.info("Registered similarity analysis job (5 AM with 5-min jitter)")

        # Start the single scheduler
        self.scheduler.start()
        logger.info("Scheduler started - all pattern analysis jobs registered")

        # Run startup tasks with staggered jitter
        # Daily aggregator runs on startup (with jitter)
        startup_delay = random.randint(30, 120)
        logger.info(f"Daily aggregator: waiting {startup_delay}s before initial run")
        await asyncio.sleep(startup_delay)
        await self.daily_aggregator._run_daily_aggregations()

        # LLM analyzer does NOT run on startup (avoid API costs)
        if self.llm_analyzer:
            logger.info("LLM analysis will run on schedule (not at startup to avoid API costs)")

        # Similarity analyzer runs on startup (with different jitter)
        startup_delay = random.randint(45, 150)
        logger.info(f"Similarity analyzer: waiting {startup_delay}s before initial run")
        await asyncio.sleep(startup_delay)
        await self.similarity_analyzer._run_similarity_analysis()

        logger.info("All analyzers initialized - service running")

        # Keep service running (scheduler handles all jobs)
        try:
            while True:
                await asyncio.sleep(3600)  # Sleep 1 hour, scheduler handles tasks
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutting down...")
            self.scheduler.shutdown()

    def run_daily_aggregation_now(self):
        """Manual trigger for daily aggregation"""
        logger.info("Manually triggering daily aggregation")
        asyncio.create_task(self.daily_aggregator._run_daily_aggregations())

    def run_llm_analysis_now(self):
        """Manual trigger for LLM analysis"""
        if not self.llm_analyzer:
            logger.warning("Cannot run LLM analysis - no API key configured")
            return
        logger.info("Manually triggering LLM analysis")
        asyncio.create_task(self.llm_analyzer._run_llm_analysis())

    def run_similarity_analysis_now(self):
        """Manual trigger for similarity analysis"""
        logger.info("Manually triggering similarity analysis")
        asyncio.create_task(self.similarity_analyzer._run_similarity_analysis())

    def get_stats(self) -> Dict[str, Any]:
        """Get aggregated statistics from all analyzers"""
        stats = {
            "service": "pattern_analysis_consolidated",
            "daily_aggregator": self.daily_aggregator.get_stats(),
            "similarity_analyzer": self.similarity_analyzer.get_stats()
        }

        if self.llm_analyzer:
            stats["llm_analyzer"] = self.llm_analyzer.get_stats()
        else:
            stats["llm_analyzer"] = {"status": "disabled", "reason": "no_api_key"}

        return stats


async def main():
    """Main entry point"""
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Get configuration from environment
    elasticsearch_hosts = [
        os.getenv("ELASTICSEARCH_HOSTS", "http://elasticsearch:9200")
    ]

    # Create and run consolidated service
    service = PatternAnalysisService(
        elasticsearch_hosts=elasticsearch_hosts,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        # Daily aggregator
        aggregation_interval_hours=int(os.getenv("AGGREGATION_INTERVAL_HOURS", "24")),
        lookback_days=int(os.getenv("LOOKBACK_DAYS", "7")),
        # LLM analyzer
        llm_analysis_interval_hours=int(os.getenv("LLM_ANALYSIS_INTERVAL_HOURS", "168")),
        min_occurrences_for_llm=int(os.getenv("MIN_OCCURRENCES_FOR_LLM", "20")),
        max_patterns_per_llm_run=int(os.getenv("MAX_PATTERNS_PER_LLM_RUN", "5")),
        # Similarity analyzer
        similarity_analysis_interval_hours=int(os.getenv("SIMILARITY_ANALYSIS_INTERVAL_HOURS", "24")),
        similarity_threshold=float(os.getenv("SIMILARITY_THRESHOLD", "0.75")),
        min_occurrences_for_similarity=int(os.getenv("MIN_OCCURRENCES_FOR_SIMILARITY", "5"))
    )

    await service.run()


if __name__ == "__main__":
    asyncio.run(main())
