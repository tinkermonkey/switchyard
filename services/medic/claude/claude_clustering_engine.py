"""
Failure Clustering Engine for Claude Code Tool Execution Failures

Groups CONTIGUOUS tool failures into clusters for fingerprinting.
Any successful tool execution breaks the cluster.
"""

import logging
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class FailureCluster:
    """Represents a cluster of contiguous related failures"""

    project: str
    session_id: str
    failures: List[Dict]
    first_failure: Dict
    last_failure: Dict

    @property
    def cluster_id(self) -> str:
        """Generate unique cluster ID"""
        timestamp = self.first_failure.get('timestamp', '')
        return f"cluster_{self.project}_{self.session_id}_{timestamp}"

    @property
    def failure_count(self) -> int:
        """Number of failures in cluster"""
        return len(self.failures)

    @property
    def duration_seconds(self) -> float:
        """Duration of cluster in seconds"""
        first_ts = self._parse_timestamp(self.first_failure.get('timestamp'))
        last_ts = self._parse_timestamp(self.last_failure.get('timestamp'))
        if first_ts and last_ts:
            return (last_ts - first_ts).total_seconds()
        return 0.0

    @property
    def tools_attempted(self) -> List[str]:
        """List of unique tools attempted in cluster"""
        return list(set(f.get('tool_name', 'unknown') for f in self.failures))

    def get_primary_failure(self) -> Dict:
        """
        Select the most representative failure from the cluster.
        Uses last failure (final attempt before giving up).
        """
        return self.last_failure

    def get_fingerprint_context(self) -> Dict:
        """
        Generate context for fingerprinting.

        Returns:
            Dictionary with primary failure, cluster metadata, and all error messages
        """
        return {
            "primary_failure": self.get_primary_failure(),
            "cluster_metadata": {
                "failure_count": self.failure_count,
                "duration_seconds": self.duration_seconds,
                "tools_attempted": self.tools_attempted,
                "session_id": self.session_id
            },
            "all_error_messages": [
                self._extract_error_message(f) for f in self.failures
            ]
        }

    def _extract_error_message(self, failure: Dict) -> str:
        """Extract error message from failure event"""
        # Try to get from result_event first
        if 'result_event' in failure:
            try:
                content = failure['result_event']['raw_event']['event']['message']['content']
                if isinstance(content, list) and len(content) > 0:
                    error_content = content[0].get('content', '')
                    return str(error_content)[:1000]  # First 1000 chars
            except (KeyError, IndexError, TypeError):
                pass

        # Fallback to error_message field
        return failure.get('error_message', 'Unknown error')[:1000]

    def _parse_timestamp(self, timestamp_str: str) -> Optional[datetime]:
        """Parse ISO timestamp string"""
        if not timestamp_str:
            return None
        try:
            return datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return None


class FailureClusteringEngine:
    """
    Groups CONTIGUOUS tool failures into clusters for fingerprinting.

    A cluster represents multiple consecutive failed attempts to accomplish the same goal.
    Any successful tool execution breaks the cluster.
    """

    CLUSTER_TIMEOUT_SECONDS = 300  # 5 minutes between events = new cluster

    def __init__(self):
        self.logger = logger

    def cluster_failures_for_session(
        self,
        es_client,
        project: str,
        session_id: str,
        start_time: str,
        end_time: str
    ) -> List[FailureCluster]:
        """
        Build clusters by analyzing ALL tool events (successes + failures) in sequence.

        Process:
        1. Query Elasticsearch for all tool_call and tool_result events
        2. Sort chronologically
        3. Identify contiguous failure sequences
        4. Group each sequence into a cluster

        Args:
            es_client: Elasticsearch client
            project: Project name
            session_id: Claude Code session ID
            start_time: Start timestamp (ISO format)
            end_time: End timestamp (ISO format)

        Returns:
            List of FailureCluster objects
        """
        # Query for ALL tool events (not just failures)
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"project": project}},
                        {"range": {"timestamp": {"gte": start_time, "lte": end_time}}}
                    ],
                    "should": [
                        # Try to match session_id in various possible locations
                        {"term": {"raw_event.event.session_id.keyword": session_id}},
                    ],
                    "minimum_should_match": 1,
                    "filter": [
                        {"terms": {"event_category": ["tool_call", "tool_result"]}}
                    ]
                }
            },
            "sort": [{"timestamp": {"order": "asc"}}],
            "size": 10000  # Reasonable limit for single session
        }

        try:
            result = es_client.search(
                index="claude-streams-*",
                body=query
            )
        except Exception as e:
            self.logger.error(f"Failed to query Elasticsearch for session {session_id}: {e}")
            return []

        # Extract events
        events = [hit["_source"] for hit in result.get("hits", {}).get("hits", [])]

        if not events:
            self.logger.debug(f"No tool events found for session {session_id}")
            return []

        self.logger.info(f"Found {len(events)} tool events for session {session_id}")
        # Debug: Log event categories
        event_categories = [e.get('event_category') for e in events]
        self.logger.info(f"Event categories: {event_categories}")

        # Build event sequence with success/failure tracking
        event_sequence = self._build_event_sequence(events)

        self.logger.info(f"Built sequence of {len(event_sequence)} tool executions for session {session_id}")
        # Debug: Log sequence details
        if event_sequence:
            sequence_debug = [{"tool": e.get('tool_name'), "success": e.get('success')} for e in event_sequence]
            self.logger.info(f"Event sequence: {sequence_debug}")

        # Extract contiguous failure clusters
        clusters = self._extract_contiguous_clusters(event_sequence, project, session_id)

        self.logger.info(f"Extracted {len(clusters)} failure clusters for session {session_id}")

        return clusters

    def _build_event_sequence(self, events: List[Dict]) -> List[Dict]:
        """
        Build chronological sequence of tool executions with success status.

        Pairs tool_call with subsequent tool_result to determine success/failure.

        Args:
            events: Raw events from Elasticsearch

        Returns:
            List of tool execution events with success status
        """
        sequence = []
        pending_calls = {}  # tool_use_id -> tool_call event

        for event in events:
            event_category = event.get("event_category")

            if event_category == "tool_call":
                # Extract tool_use_id from raw_event
                tool_use_id = self._extract_tool_use_id(event)
                if tool_use_id:
                    pending_calls[tool_use_id] = event
                else:
                    self.logger.warning(f"Failed to extract tool_use_id from tool_call event")

            elif event_category == "tool_result":
                # Match with pending call
                tool_use_id = self._extract_tool_result_id(event)
                success = event.get("success", True)

                if tool_use_id in pending_calls:
                    call_event = pending_calls.pop(tool_use_id)

                    sequence.append({
                        "timestamp": event.get("timestamp"),
                        "tool_name": call_event.get("tool_name"),
                        "tool_use_id": tool_use_id,
                        "success": success,
                        "call_event": call_event,
                        "result_event": event,
                        "error_message": event.get("error_message", "")
                    })
                else:
                    # tool_result without matching call - log warning
                    self.logger.debug(f"Found tool_result without matching call: {tool_use_id}")

        return sequence

    def _extract_contiguous_clusters(
        self,
        sequence: List[Dict],
        project: str,
        session_id: str
    ) -> List[FailureCluster]:
        """
        Extract contiguous failure sequences from event sequence.

        A cluster is broken by:
        1. Any successful tool execution (PRIMARY)
        2. Time gap > 5 minutes between events
        3. End of sequence

        Args:
            sequence: Chronological sequence of tool executions
            project: Project name
            session_id: Session ID

        Returns:
            List of FailureCluster objects
        """
        clusters = []
        current_failures = []
        last_timestamp = None

        for event in sequence:
            timestamp = event["timestamp"]
            success = event["success"]

            # Check for time gap
            if last_timestamp:
                time_gap = self._calculate_time_gap(last_timestamp, timestamp)
                if time_gap > self.CLUSTER_TIMEOUT_SECONDS:
                    # Time gap breaks cluster
                    if current_failures:
                        clusters.append(self._create_cluster(
                            current_failures, project, session_id
                        ))
                        current_failures = []

            if success:
                # SUCCESS BREAKS CLUSTER
                if current_failures:
                    clusters.append(self._create_cluster(
                        current_failures, project, session_id
                    ))
                    current_failures = []
            else:
                # Failure - add to current cluster
                current_failures.append(event)

            last_timestamp = timestamp

        # Final cluster
        if current_failures:
            clusters.append(self._create_cluster(
                current_failures, project, session_id
            ))

        return clusters

    def _create_cluster(
        self,
        failures: List[Dict],
        project: str,
        session_id: str
    ) -> FailureCluster:
        """Create FailureCluster from contiguous failures"""
        return FailureCluster(
            project=project,
            session_id=session_id,
            failures=failures,
            first_failure=failures[0],
            last_failure=failures[-1]
        )

    def _extract_tool_use_id(self, event: Dict) -> Optional[str]:
        """Extract tool_use_id from tool_call event"""
        try:
            # New format: event.message.content[].id
            content = event["raw_event"]["event"]["message"]["content"]
            if isinstance(content, list):
                for item in content:
                    if item.get("type") == "tool_use":
                        return item.get("id")
        except (KeyError, TypeError):
            pass
        return None

    def _extract_tool_result_id(self, event: Dict) -> Optional[str]:
        """Extract tool_use_id from tool_result event"""
        try:
            # New format: event.message.content[].tool_use_id
            content = event["raw_event"]["event"]["message"]["content"]
            if isinstance(content, list):
                for item in content:
                    if item.get("type") == "tool_result":
                        return item.get("tool_use_id")
        except (KeyError, TypeError):
            pass
        return None

    def _calculate_time_gap(self, time1: str, time2: str) -> float:
        """Calculate time gap in seconds between ISO timestamps"""
        try:
            t1 = datetime.fromisoformat(time1.replace('Z', '+00:00'))
            t2 = datetime.fromisoformat(time2.replace('Z', '+00:00'))
            return abs((t2 - t1).total_seconds())
        except (ValueError, AttributeError):
            return 0.0
