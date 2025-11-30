"""
Claude Advisor Orchestrator

Orchestrates the periodic execution of the Claude Code Advisor.
Analyzes project failures and generates improvement recommendations.
"""

import logging
import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime
from elasticsearch import Elasticsearch

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent.parent))

from services.medic.claude_failure_signature_store import ClaudeFailureSignatureStore
from services.medic.claude_advisor_agent_runner import ClaudeAdvisorAgentRunner
from config.manager import config_manager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ClaudeAdvisorOrchestrator")


class ClaudeAdvisorOrchestrator:
    """
    Orchestrates Claude Advisor execution.
    """

    def __init__(self):
        self.es_hosts = os.getenv("ELASTICSEARCH_HOSTS", "http://elasticsearch:9200").split(",")
        self.es_client = Elasticsearch(self.es_hosts)
        self.failure_store = ClaudeFailureSignatureStore(self.es_client)
        self.runner = ClaudeAdvisorAgentRunner()
        
        # Use relative path to ensure it works in container (/app/orchestrator_data) and local
        root_dir = Path(__file__).parent.parent.parent
        self.reports_dir = root_dir / "orchestrator_data/medic/advisor_reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    async def run_for_all_projects(self):
        """Run advisor for all visible projects"""
        projects = config_manager.list_visible_projects()
        logger.info(f"Starting Advisor run for {len(projects)} projects")
        
        for project in projects:
            await self.run_for_project(project)
            
        logger.info("Advisor run completed for all projects")

    async def run_for_project(self, project: str):
        """Run advisor for a specific project"""
        logger.info(f"Running Advisor for project: {project}")
        
        try:
            # 1. Fetch failures
            failures = self.failure_store.get_signatures_by_project(project)
            
            if not failures:
                logger.info(f"No failure signatures found for {project}, skipping advisor")
                return
                
            # Filter for relevant failures (e.g., recurring or trending, or high impact)
            # For now, we take top 20 by impact score
            failures.sort(key=lambda x: x.get('impact_score', 0), reverse=True)
            top_failures = failures[:20]
            
            logger.info(f"Found {len(failures)} failures, analyzing top {len(top_failures)}")
            
            # 2. Prepare output path
            timestamp = datetime.utcnow().strftime("%Y%m%d")
            report_path = self.reports_dir / project / f"advisor_report_{timestamp}.md"
            
            # 3. Run Advisor
            result = await self.runner.run_advisor(
                project=project,
                failures=top_failures,
                output_path=str(report_path)
            )
            
            if result:
                logger.info(f"Advisor completed for {project}. Report: {report_path}")
            else:
                logger.error(f"Advisor failed for {project}")
                
        except Exception as e:
            logger.error(f"Error running advisor for {project}: {e}", exc_info=True)


if __name__ == "__main__":
    orchestrator = ClaudeAdvisorOrchestrator()
    
    if len(sys.argv) > 1:
        # Run for specific project
        project_name = sys.argv[1]
        asyncio.run(orchestrator.run_for_project(project_name))
    else:
        # Run for all
        asyncio.run(orchestrator.run_for_all_projects())
