"""
Claude Signature Curator

Periodically reviews unresolved failure signatures, groups them,
merges duplicates, and triggers investigations.
"""

import logging
import asyncio
import json
import redis
from elasticsearch import Elasticsearch
from typing import List, Dict, Any
import os
import time
import subprocess
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from services.medic.claude_failure_signature_store import ClaudeFailureSignatureStore
from services.medic.claude_investigation_queue import ClaudeInvestigationQueue
from services.medic.claude_report_manager import ClaudeReportManager
from monitoring.observability import get_observability_manager
from monitoring.claude_code_breaker import ClaudeCodeBreaker

logger = logging.getLogger(__name__)

class ClaudeSignatureCurator:
    def __init__(self):
        self.redis_client = redis.Redis(
            host=os.environ.get('REDIS_HOST', 'localhost'),
            port=int(os.environ.get('REDIS_PORT', 6379)),
            decode_responses=True
        )
        self.es_client = Elasticsearch(
            hosts=[os.environ.get('ELASTICSEARCH_URL', 'http://localhost:9200')]
        )
        self.store = ClaudeFailureSignatureStore(self.es_client)
        self.queue = ClaudeInvestigationQueue(self.redis_client)
        self.report_manager = ClaudeReportManager()
        self.observability = get_observability_manager()
        self.breaker = ClaudeCodeBreaker()
        self.scheduler = AsyncIOScheduler()

    async def call_claude(self, prompt: str) -> str:
        """Call Claude Code CLI"""
        try:
            # Use claude-3-5-sonnet as it's good for analysis
            cmd = [
                'claude',
                '--print',
                '--permission-mode', 'bypassPermissions',
                prompt
            ]
            
            logger.info(f"Calling Claude CLI...")
            
            # Run subprocess
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy()
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                error_msg = stderr.decode()
                stdout_msg = stdout.decode()
                logger.error(f"Claude CLI failed: stderr={error_msg}, stdout={stdout_msg}")
                
                # Check if this failure is a rate limit and trip breaker if so
                is_limit, reset_time = self.breaker.detect_session_limit(stdout_msg + error_msg)
                if is_limit:
                    self.breaker.trip(reset_time)
                    logger.warning(f"Tripped circuit breaker due to rate limit. Reset at {reset_time}")
                
                raise Exception(f"Claude CLI failed with code {process.returncode}: {error_msg}")
                
            return stdout.decode()
            
        except Exception as e:
            logger.error(f"Failed to call Claude: {e}")
            raise

    async def run_curation_cycle(self):
        logger.info("Starting signature curation cycle")

        if self.breaker.is_open():
            logger.warning(f"Circuit breaker is OPEN (resets at {self.breaker.reset_time}). Skipping curation cycle.")
            return

        # 0. Cleanup stale signatures (signatures not seen in retention period)
        try:
            retention_days = int(os.getenv('MEDIC_SIGNATURE_RETENTION_DAYS', '7'))
            logger.info(f"Cleaning up signatures not seen in {retention_days} days...")

            # Delete from Elasticsearch (returns count and list of deleted IDs)
            deleted_count, deleted_fp_ids = self.store.cleanup_stale_signatures(retention_days)

            # Cleanup associated resources
            if deleted_fp_ids:
                # Delete investigation report directories
                reports_deleted = self.report_manager.cleanup_investigation_reports(deleted_fp_ids)

                # Delete Redis keys
                redis_cleaned = self.queue.cleanup_orphaned_keys(deleted_fp_ids)

                logger.info(f"Cleanup complete: {deleted_count} signatures removed, "
                           f"{reports_deleted} report directories deleted, "
                           f"{redis_cleaned} Redis keys cleaned")
            else:
                logger.info("No stale signatures to clean up")

        except Exception as e:
            logger.error(f"Stale signature cleanup failed: {e}", exc_info=True)
            # Continue with curation even if cleanup fails

        # 1. Get unresolved signatures
        signatures = self.store.get_unresolved_signatures()
        if not signatures:
            logger.info("No unresolved signatures to curate")
            return

        logger.info(f"Found {len(signatures)} unresolved signatures")
        
        # If too many, take top 50 by failure count to avoid context limits
        if len(signatures) > 50:
            signatures.sort(key=lambda x: x.get('total_failures', 0), reverse=True)
            signatures = signatures[:50]
            logger.info("Processing top 50 signatures")

        # 2. Prepare data for Claude
        simplified_sigs = []
        for sig in signatures:
            simplified_sigs.append({
                "id": sig['fingerprint_id'],
                "tool": sig['signature']['tool_name'],
                "error": sig['signature']['error_pattern'],
                "context": sig['signature']['context_signature'],
                "project": sig['project'],
                "failures": sig['total_failures']
            })

        # 3. Ask Claude to group them
        prompt = f"""
You are a Failure Analysis Agent. Your task is to review a list of failure signatures and identify duplicates that should be merged.

Here are the unresolved failure signatures:
{json.dumps(simplified_sigs, indent=2)}

Please analyze these signatures and group them by similarity.
Signatures should be grouped if they represent the same underlying issue, even if the error message varies slightly or they occurred in different projects (if the root cause is likely the same).

Return a JSON object with the following structure:
{{
  "groups": [
    {{
      "primary_id": "ID of the signature to keep (preferably the one with the most generic/representative error pattern or highest failure count)",
      "merged_ids": ["List of IDs to merge into the primary_id"],
      "reason": "Explanation of why these are grouped"
    }}
  ]
}}

Only include groups where there are at least 2 signatures (1 primary + at least 1 merged).
Do not include singletons.
Ensure the JSON is valid.
"""
        
        try:
            content = await self.call_claude(prompt)
            
            # Parse response
            json_str = content
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0]
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0]
                
            result = json.loads(json_str)
            
            # 4. Process groups
            groups = result.get('groups', [])
            logger.info(f"Claude identified {len(groups)} groups to merge")
            
            for group in groups:
                primary_id = group['primary_id']
                merged_ids = group['merged_ids']
                
                if not merged_ids:
                    continue
                    
                logger.info(f"Merging {len(merged_ids)} signatures into {primary_id}: {group['reason']}")
                
                # Merge
                self.store.merge_signatures(primary_id, merged_ids)
                
                # Queue investigation for the primary if needed
                primary_sig = self.store.get_signature(primary_id)
                if primary_sig and primary_sig.get('investigation_status') == 'not_started':
                    self.queue.enqueue(primary_id)
                    logger.info(f"Queued investigation for {primary_id}")

        except Exception as e:
            logger.error(f"Error during curation cycle: {e}", exc_info=True)

    async def start(self):
        """Start the curator service with scheduled tasks"""
        logger.info("Claude Signature Curator Service Starting...")
        
        # Schedule daily curation at 1 AM UTC
        self.scheduler.add_job(
            self.run_curation_cycle,
            trigger=CronTrigger(hour=1, minute=0, jitter=300),
            id='claude_signature_curation',
            name='Daily Claude signature curation (1 AM)',
            replace_existing=True
        )
        logger.info("Registered daily curation job (1 AM UTC)")
        
        self.scheduler.start()
        
        # Run once on startup for immediate effect
        logger.info("Running initial curation cycle...")
        try:
            await self.run_curation_cycle()
        except Exception as e:
            logger.error(f"Initial curation cycle failed: {e}")
            
        # Keep the service running
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("Service stopping...")
            self.scheduler.shutdown()

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    curator = ClaudeSignatureCurator()
    asyncio.run(curator.start())
