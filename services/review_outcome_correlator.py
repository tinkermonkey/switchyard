"""
Review Outcome Correlator

Analyzes completed review cycles to determine which findings were
addressed vs ignored, providing data for the learning system.
"""

import logging
import json
import re
from typing import Dict, Any, List, Optional
from datetime import datetime
from dataclasses import dataclass
import redis
from elasticsearch import Elasticsearch
from monitoring.observability import es_index_with_retry

from services.review_parser import ReviewFinding
from services.review_learning_schema import get_review_outcome_index_name

logger = logging.getLogger(__name__)


@dataclass
class ReviewOutcome:
    """Represents the outcome of a single review finding"""
    agent: str
    maker_agent: str
    finding: ReviewFinding
    action: str  # accepted, modified, ignored, unclear
    context: Dict[str, Any]
    timestamp: str
    review_cycle_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'agent': self.agent,
            'maker_agent': self.maker_agent,
            'finding_category': self.finding.category,
            'finding_severity': self.finding.severity,
            'finding_message': self.finding.message,
            'finding_suggestion': self.finding.suggestion,
            'action': self.action,
            'project': self.context.get('project'),
            'issue_number': self.context.get('issue_number'),
            'iteration': self.context.get('iteration'),
            'code_changed': self.context.get('code_changed', False),
            'mentioned': self.context.get('mentioned', False),
            'recurs': self.context.get('recurs', False),
            'context_json': json.dumps(self.context),
            'timestamp': self.timestamp,
            'review_cycle_id': self.review_cycle_id
        }


class ReviewOutcomeCorrelator:
    """
    Correlates review findings with maker responses to determine acceptance/rejection.

    Leverages existing Redis stream data from review cycles to extract learning signals.
    """

    def __init__(
        self,
        redis_host: str = "redis",
        redis_port: int = 6379,
        elasticsearch_hosts: List[str] = None
    ):
        self.redis = redis.Redis(
            host=redis_host,
            port=redis_port,
            decode_responses=True
        )

        if elasticsearch_hosts is None:
            elasticsearch_hosts = ["http://elasticsearch:9200"]

        self.es = Elasticsearch(elasticsearch_hosts)

    async def analyze_review_cycle_outcome(
        self,
        cycle_state: 'ReviewCycleState'
    ) -> List[ReviewOutcome]:
        """
        Analyze completed review cycle to extract learning data.

        This runs AFTER a review cycle completes (approved or escalated).

        Args:
            cycle_state: The completed review cycle state

        Returns:
            List of ReviewOutcome objects with correlation data
        """
        logger.info(
            f"Analyzing review cycle outcomes for issue #{cycle_state.issue_number} "
            f"({len(cycle_state.review_outputs)} review iterations)"
        )

        outcomes = []

        # Need at least one review iteration
        if not cycle_state.review_outputs:
            logger.warning("No review outputs found, skipping outcome analysis")
            return outcomes

        # Iterate through review iterations
        for i, review_output in enumerate(cycle_state.review_outputs):
            # Check if there's a maker response to this review
            if i + 1 < len(cycle_state.maker_outputs):
                maker_response = cycle_state.maker_outputs[i + 1]

                # Analyze each finding in this review
                findings = review_output.get('findings', [])
                logger.info(f"Iteration {i}: Analyzing {len(findings)} findings")

                for finding_dict in findings:
                    # Convert dict to ReviewFinding object if needed
                    if isinstance(finding_dict, dict):
                        finding = ReviewFinding(
                            category=finding_dict.get('category', 'general'),
                            severity=finding_dict.get('severity', 'medium'),
                            message=finding_dict.get('message', ''),
                            suggestion=finding_dict.get('suggestion')
                        )
                    else:
                        finding = finding_dict

                    outcome = await self._correlate_finding_with_response(
                        finding=finding,
                        maker_response=maker_response,
                        cycle_state=cycle_state,
                        iteration_index=i
                    )
                    outcomes.append(outcome)

            else:
                # This is the final review (no maker response after it)
                # If status is approved, all findings were addressed
                # If escalated, findings remain unaddressed
                findings = review_output.get('findings', [])
                status = review_output.get('status', 'unknown')

                for finding_dict in findings:
                    if isinstance(finding_dict, dict):
                        finding = ReviewFinding(
                            category=finding_dict.get('category', 'general'),
                            severity=finding_dict.get('severity', 'medium'),
                            message=finding_dict.get('message', ''),
                            suggestion=finding_dict.get('suggestion')
                        )
                    else:
                        finding = finding_dict

                    # Final iteration: infer action from status
                    if status == 'approved':
                        action = 'accepted'  # All findings resolved
                    elif status == 'escalated' or cycle_state.status == 'awaiting_human_feedback':
                        action = 'ignored'  # Escalated with unresolved findings
                    else:
                        action = 'unclear'

                    outcome = ReviewOutcome(
                        agent=cycle_state.reviewer_agent,
                        maker_agent=cycle_state.maker_agent,
                        finding=finding,
                        action=action,
                        context={
                            'project': cycle_state.project_name,
                            'issue_number': cycle_state.issue_number,
                            'iteration': i,
                            'final_iteration': True,
                            'final_status': status
                        },
                        timestamp=review_output.get('timestamp', datetime.now().isoformat()),
                        review_cycle_id=f"{cycle_state.project_name}_{cycle_state.issue_number}"
                    )
                    outcomes.append(outcome)

        # Publish all outcomes
        logger.info(f"Extracted {len(outcomes)} review outcomes")
        for outcome in outcomes:
            await self._publish_outcome(outcome)

        return outcomes

    async def _correlate_finding_with_response(
        self,
        finding: ReviewFinding,
        maker_response: Dict[str, Any],
        cycle_state: 'ReviewCycleState',
        iteration_index: int
    ) -> ReviewOutcome:
        """
        Determine if a specific finding was addressed by the maker.

        Uses multiple signals:
        1. Maker's response text (did they mention this finding?)
        2. Whether finding recurs in next review
        3. Git diff analysis (future enhancement)
        """

        # Signal 1: Check if maker response mentions the finding
        mentioned = self._check_response_mentions_finding(
            finding=finding,
            response_text=maker_response.get('result', {}).get('raw_output', '')
        )

        # Signal 2: Check if finding recurs in subsequent review
        recurs = False
        next_review_idx = iteration_index + 1
        if next_review_idx < len(cycle_state.review_outputs):
            next_review = cycle_state.review_outputs[next_review_idx]
            recurs = self._finding_recurs(finding, next_review.get('findings', []))

        # Signal 3: Git diff analysis (placeholder for future implementation)
        code_changed = False  # TODO: Implement git diff analysis

        # Determine action taken
        if mentioned and not recurs:
            action = 'accepted'  # Mentioned and resolved
        elif not mentioned and not recurs:
            action = 'modified'  # Resolved differently than suggested
        elif recurs:
            action = 'ignored'  # Finding still present
        else:
            action = 'unclear'

        return ReviewOutcome(
            agent=cycle_state.reviewer_agent,
            maker_agent=cycle_state.maker_agent,
            finding=finding,
            action=action,
            context={
                'project': cycle_state.project_name,
                'issue_number': cycle_state.issue_number,
                'iteration': iteration_index,
                'code_changed': code_changed,
                'mentioned': mentioned,
                'recurs': recurs
            },
            timestamp=maker_response.get('timestamp', datetime.now().isoformat()),
            review_cycle_id=f"{cycle_state.project_name}_{cycle_state.issue_number}"
        )

    def _check_response_mentions_finding(
        self,
        finding: ReviewFinding,
        response_text: str
    ) -> bool:
        """
        Check if maker's response mentions this specific finding.

        Uses keyword matching and semantic similarity.
        """
        if not response_text:
            return False

        # Extract key terms from finding message
        finding_terms = self._extract_key_terms(finding.message)

        # Check if response mentions the category or key terms
        response_lower = response_text.lower()

        # Check category match
        if finding.category.lower() in response_lower:
            return True

        # Check key term matches (need at least 2 matches for confidence)
        matches = sum(1 for term in finding_terms if term in response_lower)
        if matches >= 2:
            return True

        return False

    def _extract_key_terms(self, text: str) -> List[str]:
        """Extract key terms from text for matching"""
        # Remove common words
        stop_words = {
            'the', 'a', 'an', 'and', 'or', 'but', 'is', 'are', 'was', 'were',
            'be', 'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did',
            'will', 'would', 'should', 'could', 'may', 'might', 'must',
            'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from'
        }

        # Extract words (lowercase, alphanumeric only)
        words = re.findall(r'\b[a-z][a-z0-9_]*\b', text.lower())

        # Filter out stop words and short words
        key_terms = [w for w in words if w not in stop_words and len(w) > 3]

        return key_terms[:10]  # Return top 10 terms

    def _finding_recurs(
        self,
        finding: ReviewFinding,
        next_review_findings: List[Dict[str, Any]]
    ) -> bool:
        """
        Check if a finding recurs in the next review iteration.

        Uses category and message similarity matching.
        """
        for next_finding_dict in next_review_findings:
            # Check category match
            if next_finding_dict.get('category') != finding.category:
                continue

            # Check message similarity
            next_message = next_finding_dict.get('message', '')
            similarity = self._calculate_message_similarity(
                finding.message,
                next_message
            )

            # If messages are >70% similar, consider it a recurrence
            if similarity > 0.7:
                return True

        return False

    def _calculate_message_similarity(self, msg1: str, msg2: str) -> float:
        """
        Calculate similarity between two messages using simple term overlap.

        Returns value between 0 and 1.
        """
        if not msg1 or not msg2:
            return 0.0

        terms1 = set(self._extract_key_terms(msg1))
        terms2 = set(self._extract_key_terms(msg2))

        if not terms1 or not terms2:
            return 0.0

        # Jaccard similarity
        intersection = len(terms1 & terms2)
        union = len(terms1 | terms2)

        return intersection / union if union > 0 else 0.0

    async def _publish_outcome(self, outcome: ReviewOutcome):
        """Publish outcome to Redis stream and Elasticsearch"""
        outcome_data = {
            'type': 'review_outcome',
            **outcome.to_dict()
        }

        try:
            # Publish to Redis stream for real-time processing
            self.redis.xadd(
                'orchestrator:event_stream',
                {'event': json.dumps(outcome_data)}
            )

            # Index directly to Elasticsearch for pattern detection
            es_index_with_retry(self.es, get_review_outcome_index_name(), outcome_data)

            logger.debug(
                f"Published outcome: {outcome.agent} - {outcome.finding.category} "
                f"({outcome.finding.severity}) - {outcome.action}"
            )

        except Exception as e:
            logger.error(f"Error publishing review outcome: {e}")
            # Don't fail the analysis if publishing fails


# Global singleton
_correlator: Optional[ReviewOutcomeCorrelator] = None


def get_review_outcome_correlator() -> ReviewOutcomeCorrelator:
    """Get global ReviewOutcomeCorrelator instance"""
    global _correlator
    if _correlator is None:
        _correlator = ReviewOutcomeCorrelator()
    return _correlator
