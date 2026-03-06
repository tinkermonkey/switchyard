"""
Pattern LLM Analyzer Service

Uses Claude to analyze detected patterns and generate CLAUDE.md improvement proposals.
Runs weekly to analyze aggregated patterns and propose specific fixes.
"""

import logging
import asyncio
import json
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from elasticsearch import Elasticsearch
from monitoring.observability import es_index_with_retry
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import anthropic
import os
import random

logger = logging.getLogger(__name__)


class PatternLLMAnalyzer:
    """Uses LLM to analyze patterns and propose improvements"""

    def __init__(
        self,
        elasticsearch_hosts: List[str],
        anthropic_api_key: str,
        analysis_interval_hours: int = 168,  # Weekly
        min_occurrences_for_analysis: int = 20,
        max_patterns_per_run: int = 5
    ):
        """
        Initialize LLM analyzer

        Args:
            elasticsearch_hosts: List of Elasticsearch hosts
            anthropic_api_key: Anthropic API key for Claude
            analysis_interval_hours: Hours between analysis runs
            min_occurrences_for_analysis: Minimum occurrences to analyze
            max_patterns_per_run: Maximum patterns to analyze per run
        """
        self.es = Elasticsearch(elasticsearch_hosts)
        self.client = anthropic.Anthropic(api_key=anthropic_api_key)
        self.analysis_interval_hours = analysis_interval_hours
        self.min_occurrences_for_analysis = min_occurrences_for_analysis
        self.max_patterns_per_run = max_patterns_per_run

        # Scheduler
        self.scheduler = AsyncIOScheduler()

        # Statistics
        self.total_runs = 0
        self.total_analyses = 0
        self.total_proposals = 0
        self.total_tokens_used = 0
        self.total_cost_usd = 0.0

        logger.info(
            f"PatternLLMAnalyzer initialized "
            f"(interval={analysis_interval_hours}h, min_occurrences={min_occurrences_for_analysis})"
        )

    async def run(self):
        """Start the scheduler"""
        logger.info("Starting Pattern LLM Analyzer service...")

        # Schedule LLM analysis using APScheduler with cron trigger
        # Run weekly on Sundays at 4 AM (different from daily tasks)
        self.scheduler.add_job(
            self._run_llm_analysis,
            trigger=CronTrigger(day_of_week='sun', hour=4, minute=0, jitter=600),  # 10-minute jitter
            id='llm_pattern_analysis',
            name='LLM pattern analysis (Sunday 4 AM)',
            replace_existing=True
        )

        self.scheduler.start()
        logger.info("Scheduled LLM analysis for Sundays at 4 AM (with 10-minute jitter)")

        # Don't run on startup (expensive) - wait for scheduled time
        logger.info("LLM analysis will run on schedule (not at startup to avoid API costs)")

        # Keep service running
        try:
            while True:
                await asyncio.sleep(3600)  # Sleep 1 hour, scheduler handles tasks
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutting down...")
            self.scheduler.shutdown()

    async def _run_llm_analysis(self):
        """Run LLM analysis on patterns"""
        logger.info("Starting LLM pattern analysis run...")
        start_time = datetime.now()

        analyses_created = 0

        try:
            # Get patterns that need analysis
            patterns_to_analyze = self._get_patterns_for_analysis()

            logger.info(f"Found {len(patterns_to_analyze)} patterns for LLM analysis")

            for pattern in patterns_to_analyze[:self.max_patterns_per_run]:
                try:
                    analysis = await self._analyze_pattern(pattern)
                    if analysis:
                        self._store_analysis(pattern, analysis)
                        analyses_created += 1
                except Exception as e:
                    logger.error(f"Error analyzing pattern {pattern.get('pattern_name')}: {e}", exc_info=True)

            # Update statistics
            self.total_runs += 1
            self.total_analyses += analyses_created

            duration = (datetime.now() - start_time).total_seconds()
            logger.info(
                f"LLM analysis complete in {duration:.2f}s: "
                f"{analyses_created} analyses created, "
                f"tokens={self.total_tokens_used}, "
                f"cost=${self.total_cost_usd:.4f}"
            )

        except Exception as e:
            logger.error(f"Error running LLM analysis: {e}", exc_info=True)

    def _get_patterns_for_analysis(self) -> List[Dict[str, Any]]:
        """
        Get patterns that need LLM analysis

        Returns:
            List of pattern summaries
        """
        # Aggregate patterns from pattern-occurrences
        agg_query = {
            "size": 0,
            "aggs": {
                "by_pattern": {
                    "terms": {
                        "field": "pattern_name",
                        "size": 50,
                        "min_doc_count": self.min_occurrences_for_analysis
                    },
                    "aggs": {
                        "first_seen": {"min": {"field": "event_timestamp"}},
                        "last_seen": {"max": {"field": "event_timestamp"}},
                        "projects": {"terms": {"field": "project", "size": 20}},
                        "agents": {"terms": {"field": "agent_name", "size": 20}},
                        "severity": {"terms": {"field": "severity", "size": 1}},
                        "category": {"terms": {"field": "pattern_category", "size": 1}},
                        "sample_docs": {
                            "top_hits": {
                                "size": 5,
                                "_source": ["error_message", "session_id", "event_timestamp"]
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
            pattern_name = bucket['key']
            occurrence_count = bucket['doc_count']

            # Check if already analyzed
            existing_query = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"pattern_name": pattern_name}},
                            {"range": {
                                "analysis_date": {
                                    "gte": (datetime.now() - timedelta(days=30)).isoformat()
                                }
                            }}
                        ]
                    }
                },
                "size": 1
            }

            try:
                existing = self.es.search(index="pattern-llm-analysis", body=existing_query)
                if existing['hits']['total']['value'] > 0:
                    continue  # Already analyzed recently
            except Exception as e:
                logger.error(f"Error checking existing analysis: {e}")
                continue

            patterns.append({
                "pattern_name": pattern_name,
                "occurrence_count": occurrence_count,
                "first_seen": bucket['first_seen']['value_as_string'],
                "last_seen": bucket['last_seen']['value_as_string'],
                "affected_projects": [b['key'] for b in bucket['projects']['buckets']],
                "affected_agents": [b['key'] for b in bucket['agents']['buckets']],
                "severity": bucket['severity']['buckets'][0]['key'] if bucket['severity']['buckets'] else 'medium',
                "category": bucket['category']['buckets'][0]['key'] if bucket['category']['buckets'] else 'general',
                "examples": [
                    {
                        "error_message": hit['_source'].get('error_message'),
                        "session_id": hit['_source'].get('session_id'),
                        "timestamp": hit['_source'].get('event_timestamp')
                    }
                    for hit in bucket['sample_docs']['hits']['hits']
                ]
            })

        # Sort by occurrence count descending
        patterns.sort(key=lambda x: x['occurrence_count'], reverse=True)

        return patterns

    async def _analyze_pattern(self, pattern: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Analyze a pattern with Claude and generate improvement proposal

        Args:
            pattern: Pattern summary

        Returns:
            Analysis results including proposed change
        """
        logger.info(f"Analyzing pattern '{pattern['pattern_name']}' with Claude...")

        # Build prompt
        prompt = self._build_analysis_prompt(pattern)

        # Call Claude API
        try:
            message = self.client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=4096,
                temperature=0.3,  # Lower temperature for consistent, focused output
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            )

            response_text = message.content[0].text
            tokens_used = message.usage.input_tokens + message.usage.output_tokens

            # Estimate cost (Claude 3.5 Sonnet: $3/MTok input, $15/MTok output)
            cost = (message.usage.input_tokens / 1_000_000 * 3.0) + \
                   (message.usage.output_tokens / 1_000_000 * 15.0)

            self.total_tokens_used += tokens_used
            self.total_cost_usd += cost

            # Parse response
            analysis_result = self._parse_llm_response(response_text, pattern)
            analysis_result['llm_tokens_used'] = tokens_used
            analysis_result['llm_cost_usd'] = cost
            analysis_result['llm_model'] = "claude-3-5-sonnet-20241022"

            logger.info(
                f"LLM analysis complete for '{pattern['pattern_name']}' "
                f"(tokens={tokens_used}, cost=${cost:.4f})"
            )

            return analysis_result

        except Exception as e:
            logger.error(f"Error calling Claude API: {e}", exc_info=True)
            return None

    def _build_analysis_prompt(self, pattern: Dict[str, Any]) -> str:
        """Build Claude prompt for pattern analysis"""
        # Calculate impact
        total_time_wasted = pattern['occurrence_count'] * 30  # Assume 30s per occurrence
        avg_impact = 30

        # Format examples
        examples_text = "\n".join([
            f"- Session {ex['session_id'][:8]}: {ex['error_message'][:100]}"
            for ex in pattern['examples'][:3]
        ])

        prompt = f"""You are analyzing agent behavior logs to improve CLAUDE.md instructions that guide AI agents.

## Pattern Summary
**Type:** {pattern['pattern_name']}
**Frequency:** {pattern['occurrence_count']} occurrences across {len(pattern.get('affected_projects', []))} projects
**Projects affected:** {', '.join(pattern.get('affected_projects', [])[:5])}
**Agents affected:** {', '.join(pattern.get('affected_agents', [])[:3])}
**Severity:** {pattern.get('severity', 'medium')}
**Category:** {pattern.get('category', 'general')}
**Average impact:** ~{avg_impact} seconds per occurrence
**Total time wasted:** ~{total_time_wasted} seconds

## Example Instances
{examples_text}

## Task
Propose a specific, concise addition or modification to CLAUDE.md that would prevent this pattern. Follow these constraints:

1. **Be specific and actionable** - Not philosophical or general advice
2. **Use concrete examples** - Show exact commands or patterns to use/avoid
3. **Keep it concise** - Under 150 words
4. **Format as a git diff** - Show exactly what to add/change
5. **Specify the section** - Which part of CLAUDE.md (e.g., "Git Operations", "File System Safety", "Best Practices")

## Output Format

Return your response in this exact format:

### SECTION
<section_name>

### PROPOSED_CHANGE
```diff
<git diff format showing addition or change>
```

### EXPECTED_IMPACT
<1-2 sentences on how this prevents the pattern>

### REASONING
<2-3 sentences explaining why this pattern occurs and why your fix helps>

Be direct and technical. Focus on preventing the specific error pattern."""

        return prompt

    def _parse_llm_response(self, response_text: str, pattern: Dict[str, Any]) -> Dict[str, Any]:
        """Parse LLM response into structured format"""
        result = {
            "proposed_change_diff": "",
            "expected_impact": "",
            "reasoning": "",
            "section_name": "General"
        }

        # Simple parsing - look for markers
        lines = response_text.split('\n')

        current_section = None
        in_code_block = False
        code_lines = []

        for line in lines:
            # Section markers
            if line.strip().startswith('### SECTION'):
                current_section = 'section'
                continue
            elif line.strip().startswith('### PROPOSED_CHANGE'):
                current_section = 'change'
                continue
            elif line.strip().startswith('### EXPECTED_IMPACT'):
                current_section = 'impact'
                continue
            elif line.strip().startswith('### REASONING'):
                current_section = 'reasoning'
                continue

            # Handle code blocks
            if line.strip().startswith('```'):
                if in_code_block:
                    # End of code block
                    result['proposed_change_diff'] = '\n'.join(code_lines)
                    code_lines = []
                in_code_block = not in_code_block
                continue

            # Collect content
            if current_section == 'section' and line.strip():
                result['section_name'] = line.strip()
            elif current_section == 'change' and in_code_block:
                code_lines.append(line)
            elif current_section == 'impact' and line.strip():
                result['expected_impact'] += line.strip() + " "
            elif current_section == 'reasoning' and line.strip():
                result['reasoning'] += line.strip() + " "

        # Clean up
        result['expected_impact'] = result['expected_impact'].strip()
        result['reasoning'] = result['reasoning'].strip()

        # Quality score (simple heuristic)
        quality_score = 0.5
        if result['proposed_change_diff']:
            quality_score += 0.2
        if result['expected_impact']:
            quality_score += 0.15
        if result['reasoning']:
            quality_score += 0.15

        result['proposal_quality_score'] = min(quality_score, 1.0)
        result['requires_human_review'] = quality_score < 0.7

        return result

    def _store_analysis(self, pattern: Dict[str, Any], analysis: Dict[str, Any]):
        """Store LLM analysis in Elasticsearch"""
        try:
            # Calculate impact score
            severity_multipliers = {"critical": 10, "high": 5, "medium": 2, "low": 1}
            multiplier = severity_multipliers.get(pattern.get('severity', 'medium'), 2)
            impact_score = pattern['occurrence_count'] * 30 * multiplier  # 30s per occurrence

            doc = {
                "pattern_name": pattern['pattern_name'],
                "analysis_date": datetime.utcnow().isoformat() + 'Z',
                "occurrence_count": pattern['occurrence_count'],
                "affected_sessions": pattern['occurrence_count'],  # Approximation
                "affected_projects": pattern.get('affected_projects', []),

                # Impact metrics
                "total_time_wasted_seconds": pattern['occurrence_count'] * 30,
                "avg_impact_seconds": 30.0,
                "impact_score": impact_score,

                # LLM input
                "llm_prompt": self._build_analysis_prompt(pattern),
                "pattern_examples": pattern.get('examples', []),
                "current_claude_md_section": analysis.get('section_name'),

                # LLM output
                "llm_response": json.dumps(analysis),
                "proposed_change_diff": analysis.get('proposed_change_diff'),
                "expected_impact": analysis.get('expected_impact'),
                "reasoning": analysis.get('reasoning'),

                # Quality
                "proposal_quality_score": analysis.get('proposal_quality_score', 0.5),
                "requires_human_review": analysis.get('requires_human_review', True),

                # Status
                "status": "pending",

                # Metadata
                "created_at": datetime.utcnow().isoformat() + 'Z',
                "llm_model": analysis.get('llm_model'),
                "llm_tokens_used": analysis.get('llm_tokens_used'),
                "llm_cost_usd": analysis.get('llm_cost_usd')
            }

            es_index_with_retry(self.es, "pattern-llm-analysis", doc, refresh=True)

            self.total_proposals += 1
            logger.info(f"Stored LLM analysis for pattern '{pattern['pattern_name']}'")

        except Exception as e:
            logger.error(f"Error storing LLM analysis: {e}", exc_info=True)

    def run_now(self):
        """Manual trigger for LLM analysis (for testing/debugging)"""
        logger.info("Manually triggering LLM pattern analysis")
        asyncio.create_task(self._run_llm_analysis())

    def get_stats(self) -> Dict[str, Any]:
        """Get analyzer statistics"""
        return {
            "total_runs": self.total_runs,
            "total_analyses": self.total_analyses,
            "total_proposals": self.total_proposals,
            "total_tokens_used": self.total_tokens_used,
            "total_cost_usd": self.total_cost_usd,
            "analysis_interval_hours": self.analysis_interval_hours
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
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")

    if not anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY environment variable not set")
        return

    # Create and run analyzer
    analyzer = PatternLLMAnalyzer(
        elasticsearch_hosts=elasticsearch_hosts,
        anthropic_api_key=anthropic_api_key,
        analysis_interval_hours=int(os.getenv("LLM_ANALYSIS_INTERVAL_HOURS", "168")),  # Weekly
        min_occurrences_for_analysis=int(os.getenv("MIN_OCCURRENCES_FOR_LLM", "20")),
        max_patterns_per_run=int(os.getenv("MAX_PATTERNS_PER_LLM_RUN", "5"))
    )

    await analyzer.run()


if __name__ == "__main__":
    asyncio.run(main())
