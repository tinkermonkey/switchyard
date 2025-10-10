"""
Pattern GitHub Integration (Elasticsearch-only)

Creates GitHub Discussions and Issues for detected patterns,
enabling human-in-the-loop approval workflow.
Uses Elasticsearch instead of PostgreSQL.
"""

import logging
import json
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from elasticsearch import Elasticsearch
from elasticsearch.helpers import scan

from services.github_discussions import GitHubDiscussions
from services.github_app import github_app

logger = logging.getLogger(__name__)


class PatternGitHubIntegration:
    """Manages GitHub integration for pattern detection workflow (ES-only)"""

    def __init__(
        self,
        elasticsearch_hosts: List[str],
        owner: str,
        repo: str,
        discussion_category: str = "Ideas",
        min_occurrences_for_discussion: int = 5,
        min_occurrences_for_issue: int = 20
    ):
        """
        Initialize GitHub integration for patterns

        Args:
            elasticsearch_hosts: List of Elasticsearch hosts
            owner: GitHub repository owner/org
            repo: GitHub repository name
            discussion_category: Category for pattern discussions
            min_occurrences_for_discussion: Threshold for creating discussion
            min_occurrences_for_issue: Threshold for creating issue
        """
        self.es = Elasticsearch(elasticsearch_hosts)
        self.owner = owner
        self.repo = repo
        self.discussion_category = discussion_category
        self.min_occurrences_for_discussion = min_occurrences_for_discussion
        self.min_occurrences_for_issue = min_occurrences_for_issue

        # GitHub clients
        self.discussions = GitHubDiscussions()
        self.app = github_app

        # Cache category ID
        self.category_id: Optional[str] = None

        logger.info(
            f"PatternGitHubIntegration initialized for {owner}/{repo} "
            f"(discussion_threshold={min_occurrences_for_discussion}, "
            f"issue_threshold={min_occurrences_for_issue})"
        )

    def ensure_category(self) -> Optional[str]:
        """Ensure discussion category exists and get its ID"""
        if self.category_id:
            return self.category_id

        # Find category by name
        category_id = self.discussions.find_category_by_name(
            self.owner,
            self.repo,
            self.discussion_category
        )

        if category_id:
            self.category_id = category_id
            logger.info(f"Found discussion category '{self.discussion_category}': {category_id}")
        else:
            logger.warning(
                f"Discussion category '{self.discussion_category}' not found. "
                f"Available categories: {self.discussions.get_discussion_categories(self.owner, self.repo)}"
            )

        return category_id

    def check_patterns_for_thresholds(self) -> List[Dict[str, Any]]:
        """
        Check which patterns have exceeded thresholds

        Returns:
            List of patterns that need GitHub discussions
        """
        # Aggregate pattern occurrences from Elasticsearch
        agg_query = {
            "size": 0,
            "query": {
                "match_all": {}
            },
            "aggs": {
                "by_pattern": {
                    "terms": {
                        "field": "pattern_name",
                        "size": 100
                    },
                    "aggs": {
                        "first_seen": {"min": {"field": "event_timestamp"}},
                        "last_seen": {"max": {"field": "event_timestamp"}},
                        "projects": {"terms": {"field": "project", "size": 50}},
                        "agents": {"terms": {"field": "agent_name", "size": 50}},
                        "severity": {"terms": {"field": "severity", "size": 1}},
                        "category": {"terms": {"field": "pattern_category", "size": 1}}
                    }
                }
            }
        }

        try:
            response = self.es.search(index="pattern-occurrences", body=agg_query)
        except Exception as e:
            logger.error(f"Error querying pattern occurrences: {e}")
            return []

        patterns = []
        for bucket in response['aggregations']['by_pattern']['buckets']:
            pattern_name = bucket['key']
            occurrence_count = bucket['doc_count']

            # Skip if below threshold
            if occurrence_count < self.min_occurrences_for_discussion:
                continue

            # Check if already has a GitHub discussion/issue
            existing_query = {
                "query": {
                    "term": {"pattern_name": pattern_name}
                },
                "size": 1
            }

            try:
                existing = self.es.search(
                    index="pattern-github-tracking",
                    body=existing_query
                )

                if existing['hits']['total']['value'] > 0:
                    continue  # Already has GitHub integration
            except Exception as e:
                logger.error(f"Error checking existing GitHub tracking: {e}")
                continue

            # Get pattern metadata (from first occurrence - they should all match)
            # We need to get severity, category, description, proposed_fix
            # These are stored in pattern occurrences
            sample_query = {
                "query": {
                    "term": {"pattern_name": pattern_name}
                },
                "size": 1
            }

            try:
                sample_response = self.es.search(
                    index="pattern-occurrences",
                    body=sample_query
                )

                if sample_response['hits']['total']['value'] == 0:
                    continue

                sample_doc = sample_response['hits']['hits'][0]['_source']

                patterns.append({
                    "pattern_name": pattern_name,
                    "description": f"Pattern: {pattern_name}",  # We don't have description in ES
                    "severity": bucket['severity']['buckets'][0]['key'] if bucket['severity']['buckets'] else 'medium',
                    "pattern_category": bucket['category']['buckets'][0]['key'] if bucket['category']['buckets'] else 'general',
                    "proposed_fix": sample_doc.get('detection_rule', {}).get('proposed_fix', {}),
                    "occurrence_count": occurrence_count,
                    "first_seen": bucket['first_seen']['value_as_string'],
                    "last_seen": bucket['last_seen']['value_as_string'],
                    "affected_projects": [b['key'] for b in bucket['projects']['buckets']],
                    "affected_agents": [b['key'] for b in bucket['agents']['buckets']]
                })

            except Exception as e:
                logger.error(f"Error getting pattern sample: {e}")
                continue

        logger.info(f"Found {len(patterns)} patterns exceeding discussion threshold")
        return patterns

    def create_pattern_discussion(self, pattern: Dict[str, Any]) -> Optional[str]:
        """
        Create GitHub Discussion for a pattern

        Args:
            pattern: Pattern info dict

        Returns:
            Discussion ID if successful, None otherwise
        """
        # Ensure category exists
        category_id = self.ensure_category()
        if not category_id:
            logger.error("Cannot create discussion without valid category")
            return None

        # Build discussion title
        title = f"Pattern Detected: {pattern['pattern_name']}"

        # Build discussion body
        body = self._build_discussion_body(pattern)

        # Create discussion
        discussion_id = self.discussions.create_discussion(
            self.owner,
            self.repo,
            category_id,
            title,
            body
        )

        if discussion_id:
            # Get discussion number
            discussion_data = self.discussions.get_discussion(discussion_id)
            if discussion_data:
                discussion_number = discussion_data.get('number')
                discussion_url = discussion_data.get('url')

                # Record in Elasticsearch
                self._record_github_discussion(
                    pattern['pattern_name'],
                    discussion_id,
                    discussion_number,
                    discussion_url,
                    pattern['occurrence_count'],
                    pattern['first_seen'],
                    pattern['last_seen'],
                    pattern['affected_projects'],
                    pattern['affected_agents']
                )

                logger.info(
                    f"Created discussion #{discussion_number} for pattern '{pattern['pattern_name']}'"
                )

        return discussion_id

    def _build_discussion_body(self, pattern: Dict[str, Any]) -> str:
        """Build formatted discussion body with pattern details"""
        # Calculate impact score
        impact_score = pattern['occurrence_count'] * self._severity_multiplier(pattern['severity'])

        # Format affected projects/agents
        projects_str = ", ".join(pattern['affected_projects'][:5])
        if len(pattern['affected_projects']) > 5:
            projects_str += f" (+{len(pattern['affected_projects']) - 5} more)"

        agents_str = ", ".join(pattern['affected_agents'][:5])
        if len(pattern['affected_agents']) > 5:
            agents_str += f" (+{len(pattern['affected_agents']) - 5} more)"

        # Build proposed fix section
        proposed_fix = pattern.get('proposed_fix', {})
        fix_section = proposed_fix.get('section', 'General')
        fix_content = proposed_fix.get('content', 'No fix proposed yet')

        body = f"""## Pattern Detected: {pattern['pattern_name']}

**Frequency:** {pattern['occurrence_count']} occurrences
**Severity:** {pattern['severity'].upper()}
**Category:** {pattern['pattern_category']}
**Impact Score:** {impact_score}

### Description

{pattern['description']}

### Occurrence Details

- **First Seen:** {pattern['first_seen']}
- **Last Seen:** {pattern['last_seen']}
- **Affected Projects:** {projects_str}
- **Affected Agents:** {agents_str}

### Proposed CLAUDE.md Fix

**Section:** `{fix_section}`

```markdown
{fix_content}
```

### Community Input Needed

This pattern has been automatically detected from agent behavior. We need your feedback:

1. **Is this a real inefficiency or expected behavior?**
2. **Would the proposed fix help prevent this pattern?**
3. **Any additional context or alternative approaches?**

### How to Approve

If you agree this fix should be applied:
- Comment with ✅ `APPROVE` or 👍 to approve
- Comment with ❌ `REJECT` if this is not an issue
- Comment with 💬 feedback for improvements

Once this pattern receives **3 approvals**, it will be converted to an Issue for implementation.

---

_🤖 This discussion was automatically created by the Pattern Detection System. [View detection logs](https://github.com/{self.owner}/{self.repo}/discussions) or [Learn more about pattern detection](https://docs.example.com/pattern-detection)._
"""

        return body

    def _severity_multiplier(self, severity: str) -> int:
        """Get multiplier for severity level"""
        multipliers = {
            "critical": 10,
            "high": 5,
            "medium": 2,
            "low": 1
        }
        return multipliers.get(severity, 1)

    def _record_github_discussion(
        self,
        pattern_name: str,
        discussion_id: str,
        discussion_number: int,
        discussion_url: str,
        occurrence_count: int,
        first_occurrence: str,
        last_occurrence: str,
        affected_projects: List[str],
        affected_agents: List[str]
    ):
        """Record GitHub discussion in Elasticsearch"""
        doc = {
            "pattern_name": pattern_name,
            "github_type": "discussion",
            "github_id": discussion_id,
            "github_number": discussion_number,
            "github_url": discussion_url,
            "github_state": "open",
            "occurrence_count": occurrence_count,
            "first_occurrence": first_occurrence,
            "last_occurrence": last_occurrence,
            "affected_projects": affected_projects,
            "affected_agents": affected_agents,
            "created_at": datetime.utcnow().isoformat() + 'Z',
            "approval_count": 0,
            "rejection_count": 0
        }

        try:
            self.es.index(
                index="pattern-github-tracking",
                body=doc,
                refresh=True
            )
        except Exception as e:
            logger.error(f"Error recording GitHub discussion: {e}")

    def check_discussions_for_approval(self) -> List[Dict[str, Any]]:
        """
        Check open discussions for approval votes

        Returns:
            List of discussions that have been approved
        """
        # Get open discussions from Elasticsearch
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"github_type": "discussion"}},
                        {"term": {"github_state": "open"}},
                        {"bool": {"must_not": [{"exists": {"field": "resolution"}}]}}
                    ]
                }
            },
            "size": 100
        }

        try:
            response = self.es.search(
                index="pattern-github-tracking",
                body=query
            )
        except Exception as e:
            logger.error(f"Error querying GitHub tracking: {e}")
            return []

        approved_discussions = []

        for hit in response['hits']['hits']:
            doc_id = hit['_id']
            source = hit['_source']

            pattern_name = source['pattern_name']
            discussion_number = source['github_number']

            # Check GitHub for approval
            approval_status = self._check_discussion_approval(discussion_number)

            # Update approval counts in Elasticsearch
            self.es.update(
                index="pattern-github-tracking",
                id=doc_id,
                body={
                    "doc": {
                        "approval_count": approval_status['approve_count'],
                        "rejection_count": approval_status['reject_count'],
                        "last_checked_at": datetime.utcnow().isoformat() + 'Z'
                    }
                },
                refresh=True
            )

            if approval_status['approved']:
                logger.info(
                    f"Discussion #{discussion_number} approved with "
                    f"{approval_status['approve_count']} votes"
                )

                approved_discussions.append({
                    "doc_id": doc_id,
                    "pattern_name": pattern_name,
                    "discussion_number": discussion_number,
                    "approval_count": approval_status['approve_count'],
                    "reject_count": approval_status['reject_count']
                })

        return approved_discussions

    def _check_discussion_approval(self, discussion_number: int) -> Dict[str, Any]:
        """
        Check if discussion has been approved via comments

        Returns:
            Dict with approval status and counts
        """
        # Get discussion with comments
        discussion = self.discussions.get_discussion_by_number(
            self.owner,
            self.repo,
            discussion_number
        )

        if not discussion:
            return {"approved": False, "approve_count": 0, "reject_count": 0}

        approve_count = 0
        reject_count = 0

        # Check comments for approval keywords
        approval_keywords = ['approve', 'approved', '✅', '👍', 'lgtm', 'yes']
        reject_keywords = ['reject', 'rejected', '❌', '👎', 'no']

        for comment in discussion.get('comments', {}).get('nodes', []):
            body_lower = comment.get('body', '').lower()

            # Check for approval
            if any(keyword in body_lower for keyword in approval_keywords):
                approve_count += 1

            # Check for rejection
            if any(keyword in body_lower for keyword in reject_keywords):
                reject_count += 1

        # Approved if >= 3 approvals and approvals > rejections
        approved = approve_count >= 3 and approve_count > reject_count

        return {
            "approved": approved,
            "approve_count": approve_count,
            "reject_count": reject_count
        }

    def create_issue_from_discussion(
        self,
        discussion_number: int,
        pattern_name: str
    ) -> Optional[int]:
        """
        Create GitHub Issue from approved discussion

        Args:
            discussion_number: Discussion number
            pattern_name: Pattern name

        Returns:
            Issue number if successful, None otherwise
        """
        # Get discussion details
        discussion = self.discussions.get_discussion_by_number(
            self.owner,
            self.repo,
            discussion_number
        )

        if not discussion:
            logger.error(f"Discussion #{discussion_number} not found")
            return None

        # Create issue title
        title = f"[Pattern Fix] {pattern_name}"

        # Create issue body referencing the discussion
        body = f"""## Pattern Fix Implementation

This issue tracks the implementation of the approved pattern fix from Discussion #{discussion_number}.

**Pattern:** {pattern_name}
**Discussion:** {discussion.get('url')}

### Implementation Checklist

- [ ] Update CLAUDE.md with proposed fix
- [ ] Test fix doesn't break existing workflows
- [ ] Monitor pattern occurrence reduction
- [ ] Document changes in changelog

### Success Criteria

- Pattern occurrences reduced by 50%+ within 7 days
- No regression in agent success rates
- CLAUDE.md remains under size limits

---

_🤖 This issue was automatically created from an approved pattern detection discussion._
"""

        # Create issue via GitHub API
        try:
            issue_data = self.app.rest_request(
                'POST',
                f'/repos/{self.owner}/{self.repo}/issues',
                {
                    'title': title,
                    'body': body,
                    'labels': ['pattern-detection', 'automation', 'approved']
                }
            )

            if issue_data:
                issue_number = issue_data['number']
                issue_url = issue_data['html_url']

                # Record issue creation in Elasticsearch
                self._record_issue_creation(
                    pattern_name,
                    discussion_number,
                    issue_number,
                    issue_url
                )

                # Add comment to discussion linking to issue
                discussion_id = discussion.get('id')
                if discussion_id:
                    self.discussions.add_discussion_comment(
                        discussion_id,
                        f"✅ This pattern fix has been approved and tracked in Issue #{issue_number}\n\n{issue_url}"
                    )

                logger.info(f"Created issue #{issue_number} from discussion #{discussion_number}")
                return issue_number

        except Exception as e:
            logger.error(f"Failed to create issue: {e}")

        return None

    def _record_issue_creation(
        self,
        pattern_name: str,
        discussion_number: int,
        issue_number: int,
        issue_url: str
    ):
        """Record issue creation and update discussion status in Elasticsearch"""
        # Find the discussion document
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"pattern_name": pattern_name}},
                        {"term": {"github_number": discussion_number}},
                        {"term": {"github_type": "discussion"}}
                    ]
                }
            },
            "size": 1
        }

        try:
            response = self.es.search(
                index="pattern-github-tracking",
                body=query
            )

            if response['hits']['total']['value'] == 0:
                logger.error(f"Discussion tracking not found for pattern {pattern_name}")
                return

            discussion_doc = response['hits']['hits'][0]
            discussion_source = discussion_doc['_source']

            # Create issue tracking document
            issue_doc = {
                "pattern_name": pattern_name,
                "github_type": "issue",
                "github_number": issue_number,
                "github_url": issue_url,
                "github_state": "open",
                "occurrence_count": discussion_source.get('occurrence_count'),
                "first_occurrence": discussion_source.get('first_occurrence'),
                "last_occurrence": discussion_source.get('last_occurrence'),
                "affected_projects": discussion_source.get('affected_projects'),
                "affected_agents": discussion_source.get('affected_agents'),
                "created_at": datetime.utcnow().isoformat() + 'Z'
            }

            self.es.index(
                index="pattern-github-tracking",
                body=issue_doc,
                refresh=True
            )

            # Mark discussion as resolved
            self.es.update(
                index="pattern-github-tracking",
                id=discussion_doc['_id'],
                body={
                    "doc": {
                        "resolution": "accepted",
                        "closed_at": datetime.utcnow().isoformat() + 'Z'
                    }
                },
                refresh=True
            )

        except Exception as e:
            logger.error(f"Error recording issue creation: {e}")

    def process_patterns(self) -> Dict[str, int]:
        """
        Main processing loop: check thresholds, create discussions, check approvals

        Returns:
            Stats dict with counts
        """
        stats = {
            "patterns_checked": 0,
            "discussions_created": 0,
            "discussions_approved": 0,
            "issues_created": 0
        }

        # Step 1: Check for patterns exceeding discussion threshold
        patterns_for_discussion = self.check_patterns_for_thresholds()
        stats["patterns_checked"] = len(patterns_for_discussion)

        for pattern in patterns_for_discussion:
            discussion_id = self.create_pattern_discussion(pattern)
            if discussion_id:
                stats["discussions_created"] += 1

        # Step 2: Check existing discussions for approval
        approved_discussions = self.check_discussions_for_approval()
        stats["discussions_approved"] = len(approved_discussions)

        for discussion in approved_discussions:
            issue_number = self.create_issue_from_discussion(
                discussion['discussion_number'],
                discussion['pattern_name']
            )
            if issue_number:
                stats["issues_created"] += 1

        return stats
