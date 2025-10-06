"""
Pattern GitHub Processor Service (Elasticsearch-only)

Background service that periodically processes patterns for GitHub integration:
- Creates discussions for patterns exceeding thresholds
- Checks discussions for approval
- Creates issues for approved patterns

Uses Elasticsearch instead of PostgreSQL.
"""

import asyncio
import logging
import time
from typing import Dict, Any

from services.pattern_github_integration_es import PatternGitHubIntegration

logger = logging.getLogger(__name__)


class PatternGitHubProcessor:
    """Background processor for pattern GitHub integration (ES-only)"""

    def __init__(
        self,
        elasticsearch_hosts: list,
        github_owner: str,
        github_repo: str,
        processing_interval: int = 300,  # 5 minutes
        discussion_category: str = "Ideas",
        min_occurrences_for_discussion: int = 5,
        min_occurrences_for_issue: int = 20
    ):
        """
        Initialize pattern GitHub processor

        Args:
            elasticsearch_hosts: List of Elasticsearch hosts
            github_owner: GitHub repository owner/org
            github_repo: GitHub repository name
            processing_interval: Seconds between processing runs
            discussion_category: GitHub discussion category
            min_occurrences_for_discussion: Threshold for creating discussion
            min_occurrences_for_issue: Threshold for creating issue (unused currently)
        """
        self.processing_interval = processing_interval

        # Initialize GitHub integration
        self.github_integration = PatternGitHubIntegration(
            elasticsearch_hosts=elasticsearch_hosts,
            owner=github_owner,
            repo=github_repo,
            discussion_category=discussion_category,
            min_occurrences_for_discussion=min_occurrences_for_discussion,
            min_occurrences_for_issue=min_occurrences_for_issue
        )

        # Statistics
        self.total_runs = 0
        self.total_discussions_created = 0
        self.total_issues_created = 0

        logger.info(
            f"PatternGitHubProcessor initialized (interval={processing_interval}s, "
            f"repo={github_owner}/{github_repo})"
        )

    async def run(self):
        """Main processing loop"""
        logger.info("Starting Pattern GitHub Processor service...")

        # Initial delay to let other services start
        await asyncio.sleep(30)

        while True:
            try:
                await self._process_patterns()
                await asyncio.sleep(self.processing_interval)
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                break
            except Exception as e:
                logger.error(f"Error in processing loop: {e}", exc_info=True)
                await asyncio.sleep(60)  # Back off on errors

    async def _process_patterns(self):
        """Process patterns for GitHub integration"""
        start_time = time.time()
        logger.info("Starting pattern GitHub processing run...")

        try:
            stats = self.github_integration.process_patterns()

            # Update totals
            self.total_runs += 1
            self.total_discussions_created += stats['discussions_created']
            self.total_issues_created += stats['issues_created']

            duration = time.time() - start_time

            logger.info(
                f"Pattern GitHub processing complete in {duration:.2f}s: "
                f"{stats['patterns_checked']} patterns checked, "
                f"{stats['discussions_created']} discussions created, "
                f"{stats['discussions_approved']} approved, "
                f"{stats['issues_created']} issues created"
            )

        except Exception as e:
            logger.error(f"Error processing patterns: {e}", exc_info=True)

    def get_stats(self) -> Dict[str, Any]:
        """Get processor statistics"""
        return {
            "total_runs": self.total_runs,
            "total_discussions_created": self.total_discussions_created,
            "total_issues_created": self.total_issues_created,
            "processing_interval_seconds": self.processing_interval
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
    github_owner = os.getenv("GITHUB_ORG", "your-org")
    github_repo = os.getenv("GITHUB_REPO", "orchestrator")

    # Create and run processor
    processor = PatternGitHubProcessor(
        elasticsearch_hosts=es_hosts,
        github_owner=github_owner,
        github_repo=github_repo,
        processing_interval=int(os.getenv("GITHUB_PROCESSING_INTERVAL", "300")),
        discussion_category=os.getenv("GITHUB_DISCUSSION_CATEGORY", "Ideas"),
        min_occurrences_for_discussion=int(os.getenv("MIN_OCCURRENCES_FOR_DISCUSSION", "5"))
    )

    await processor.run()


if __name__ == "__main__":
    asyncio.run(main())
