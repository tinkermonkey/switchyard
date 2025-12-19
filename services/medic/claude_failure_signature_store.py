"""
Claude Failure Signature Store

Elasticsearch storage for Claude Code tool execution failure signatures.
Handles creation, updates, and queries for project-scoped failure signatures.
"""

import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from elasticsearch import Elasticsearch, helpers

from .claude_fingerprint_engine import ClaudeFailureFingerprint
from .claude_clustering_engine import FailureCluster

logger = logging.getLogger(__name__)


class ClaudeFailureSignatureStore:
    """
    Manages Claude failure signatures in Elasticsearch.

    Index pattern: medic-claude-failures-*
    """

    INDEX_PREFIX = "medic-claude-failures"

    def __init__(self, es_client: Elasticsearch):
        self.es = es_client
        self.logger = logger

    def create_or_update_signature(
        self,
        fingerprint: ClaudeFailureFingerprint,
        cluster: FailureCluster
    ) -> Dict:
        """
        Create or update a failure signature based on fingerprint.

        If signature exists, increments counters and adds cluster sample.
        If new, creates signature with initial data.

        Args:
            fingerprint: ClaudeFailureFingerprint object
            cluster: FailureCluster that triggered this

        Returns:
            Updated signature document
        """
        index_name = self._get_index_name()
        fingerprint_id = fingerprint.fingerprint_id

        try:
            # Try to get existing signature
            existing = self.get_signature(fingerprint_id)

            if existing:
                return self._update_existing_signature(existing, cluster)
            else:
                return self._create_new_signature(fingerprint, cluster, index_name)

        except Exception as e:
            self.logger.error(f"Failed to create/update signature {fingerprint_id}: {e}")
            raise

    def _create_new_signature(
        self,
        fingerprint: ClaudeFailureFingerprint,
        cluster: FailureCluster,
        index_name: str
    ) -> Dict:
        """Create new signature document"""
        now = datetime.utcnow().isoformat() + 'Z'

        signature_doc = {
            "fingerprint_id": fingerprint.fingerprint_id,
            "type": "claude_failure",  # Distinguishes from docker_failure
            "created_at": now,
            "updated_at": now,
            "first_seen": cluster.first_failure.get('timestamp'),
            "last_seen": cluster.last_failure.get('timestamp'),

            "project": fingerprint.project,

            "signature": {
                "tool_name": fingerprint.tool_name,
                "error_type": fingerprint.error_type,
                "error_pattern": fingerprint.error_pattern,
                "context_signature": fingerprint.context_signature,
                "cluster_size_avg": cluster.failure_count
            },

            "cluster_count": 1,
            "total_failures": cluster.failure_count,
            "clusters_last_hour": 1,
            "clusters_last_day": 1,

            "severity": "ERROR",  # All tool failures are ERROR
            "impact_score": self._calculate_impact_score(cluster.failure_count, 1),

            "status": "new",
            "investigation_status": "not_started",

            "sample_clusters": [self._cluster_to_sample(cluster)],

            "tags": self._generate_tags(fingerprint, cluster)
        }

        # Index the document
        self.es.index(
            index=index_name,
            id=fingerprint.fingerprint_id,
            body=signature_doc
        )

        self.logger.info(f"Created new Claude failure signature: {fingerprint.fingerprint_id} for project {fingerprint.project}")

        return signature_doc

    def _update_existing_signature(self, existing: Dict, cluster: FailureCluster) -> Dict:
        """Update existing signature with new cluster"""
        now = datetime.utcnow().isoformat() + 'Z'
        fingerprint_id = existing['fingerprint_id']

        # Calculate time-based counters
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        one_day_ago = datetime.utcnow() - timedelta(days=1)

        # Count recent clusters
        sample_clusters = existing.get('sample_clusters', [])
        clusters_last_hour = sum(
            1 for c in sample_clusters
            if self._parse_timestamp(c.get('timestamp')) > one_hour_ago
        ) + 1  # +1 for new cluster

        clusters_last_day = sum(
            1 for c in sample_clusters
            if self._parse_timestamp(c.get('timestamp')) > one_day_ago
        ) + 1

        # Update averages
        total_clusters = existing['cluster_count'] + 1
        total_failures = existing['total_failures'] + cluster.failure_count
        cluster_size_avg = total_failures / total_clusters

        # Add new sample (keep last 10)
        new_samples = [self._cluster_to_sample(cluster)] + sample_clusters
        new_samples = new_samples[:10]

        # Determine new status
        new_status = self._calculate_status(existing['status'], total_clusters, clusters_last_hour)

        # Update document
        update_body = {
            "doc": {
                "updated_at": now,
                "last_seen": cluster.last_failure.get('timestamp'),

                "cluster_count": total_clusters,
                "total_failures": total_failures,
                "clusters_last_hour": clusters_last_hour,
                "clusters_last_day": clusters_last_day,

                "signature.cluster_size_avg": cluster_size_avg,

                "impact_score": self._calculate_impact_score(total_failures, total_clusters),

                "status": new_status,

                "sample_clusters": new_samples
            }
        }

        index_name = self._get_index_name()
        self.es.update(
            index=index_name,
            id=fingerprint_id,
            body=update_body
        )

        self.logger.info(f"Updated Claude failure signature: {fingerprint_id} (clusters: {total_clusters}, failures: {total_failures})")

        # Return updated doc
        updated = existing.copy()
        updated.update(update_body["doc"])
        return updated

    def get_signature(self, fingerprint_id: str) -> Optional[Dict]:
        """Get signature by fingerprint ID"""
        try:
            result = self.es.search(
                index=f"{self.INDEX_PREFIX}-*",
                body={
                    "query": {"term": {"fingerprint_id": fingerprint_id}},
                    "size": 1
                }
            )

            hits = result.get('hits', {}).get('hits', [])
            if hits:
                return hits[0]['_source']
            return None

        except Exception as e:
            self.logger.error(f"Failed to get signature {fingerprint_id}: {e}")
            return None

    def get_unresolved_signatures(self) -> List[Dict]:
        """Get all unresolved failure signatures."""
        try:
            result = self.es.search(
                index=f"{self.INDEX_PREFIX}-*",
                body={
                    "query": {
                        "bool": {
                            "must_not": [
                                {"term": {"status": "resolved"}},
                                {"term": {"status": "ignored"}}
                            ]
                        }
                    },
                    "size": 1000  # Limit to 1000 for now
                }
            )
            return [hit['_source'] for hit in result.get('hits', {}).get('hits', [])]
        except Exception as e:
            self.logger.error(f"Failed to get unresolved signatures: {e}")
            return []

    def delete_signature(self, fingerprint_id: str):
        """Delete a signature by ID."""
        try:
            self.es.delete_by_query(
                index=f"{self.INDEX_PREFIX}-*",
                body={"query": {"term": {"fingerprint_id": fingerprint_id}}}
            )
            self.logger.info(f"Deleted signature {fingerprint_id}")
        except Exception as e:
            self.logger.error(f"Failed to delete signature {fingerprint_id}: {e}")

    def merge_signatures(self, primary_id: str, secondary_ids: List[str]):
        """Merge secondary signatures into primary signature."""
        primary = self.get_signature(primary_id)
        if not primary:
            self.logger.error(f"Primary signature {primary_id} not found")
            return

        total_failures = primary['total_failures']
        cluster_count = primary['cluster_count']
        sample_clusters = primary.get('sample_clusters', [])
        
        # Parse timestamps for min/max calculation
        first_seen = self._parse_timestamp(primary['first_seen'])
        last_seen = self._parse_timestamp(primary['last_seen'])

        for sec_id in secondary_ids:
            secondary = self.get_signature(sec_id)
            if not secondary:
                continue
            
            total_failures += secondary['total_failures']
            cluster_count += secondary['cluster_count']
            sample_clusters.extend(secondary.get('sample_clusters', []))
            
            sec_first = self._parse_timestamp(secondary['first_seen'])
            sec_last = self._parse_timestamp(secondary['last_seen'])
            
            if sec_first < first_seen:
                first_seen = sec_first
            if sec_last > last_seen:
                last_seen = sec_last
            
            # Delete secondary
            self.delete_signature(sec_id)

        # Sort samples and keep top 20
        sample_clusters.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        sample_clusters = sample_clusters[:20]
        
        # Update primary
        now = datetime.utcnow().isoformat() + 'Z'
        update_body = {
            "doc": {
                "updated_at": now,
                "total_failures": total_failures,
                "cluster_count": cluster_count,
                "sample_clusters": sample_clusters,
                "first_seen": first_seen.isoformat() + 'Z',
                "last_seen": last_seen.isoformat() + 'Z',
                "signature.cluster_size_avg": total_failures / cluster_count if cluster_count > 0 else 0
            }
        }
        
        try:
            self.es.update(
                index=f"{self.INDEX_PREFIX}-*",
                id=primary_id,
                body=update_body
            )
            self.logger.info(f"Merged {len(secondary_ids)} signatures into {primary_id}")
        except Exception as e:
            self.logger.error(f"Failed to update primary signature {primary_id}: {e}")

    def get_signatures_by_project(self, project: str, status: Optional[str] = None) -> List[Dict]:
        """
        Get all failure signatures for a project.
        
        Args:
            project: Project name
            status: Optional status filter (e.g., 'new', 'recurring')
            
        Returns:
            List of signature documents
        """
        try:
            query = {
                "bool": {
                    "must": [
                        {"term": {"project": project}}
                    ]
                }
            }
            
            if status:
                query["bool"]["must"].append({"term": {"status": status}})
                
            result = self.es.search(
                index=f"{self.INDEX_PREFIX}-*",
                body={
                    "query": query,
                    "size": 100,
                    "sort": [{"total_failures": "desc"}]
                }
            )
            
            return [hit['_source'] for hit in result.get('hits', {}).get('hits', [])]
            
        except Exception as e:
            self.logger.error(f"Failed to get signatures for project {project}: {e}")
            return []

    def _cluster_to_sample(self, cluster: FailureCluster) -> Dict:
        """Convert cluster to sample entry"""
        return {
            "cluster_id": cluster.cluster_id,
            "timestamp": cluster.last_failure.get('timestamp'),
            "session_id": cluster.session_id,
            "task_id": cluster.last_failure.get('call_event', {}).get('task_id', 'unknown'),
            "failure_count": cluster.failure_count,
            "duration_seconds": cluster.duration_seconds,
            "tools_attempted": cluster.tools_attempted,
            "primary_error": cluster.get_primary_failure().get('error_message', '')[:200]
        }

    def _calculate_status(self, current_status: str, total_clusters: int, clusters_last_hour: int) -> str:
        """Calculate new status based on occurrence patterns"""
        if total_clusters >= 2:
            if clusters_last_hour >= 3:
                return "trending"
            return "recurring"
        return current_status

    def _calculate_impact_score(self, total_failures: int, total_clusters: int) -> float:
        """Calculate impact score (0-10 scale)"""
        # Base score on failure frequency
        frequency_score = min(total_failures / 10, 5.0)  # Max 5 points for frequency
        cluster_score = min(total_clusters / 5, 5.0)  # Max 5 points for clusters

        return round(frequency_score + cluster_score, 1)

    def _generate_tags(self, fingerprint: ClaudeFailureFingerprint, cluster: FailureCluster) -> List[str]:
        """Generate tags for signature"""
        tags = ["tool_execution", "claude_code"]

        # Add tool name
        tags.append(f"tool:{fingerprint.tool_name}")

        # Add error type
        if fingerprint.error_type != "unknown_error":
            tags.append(fingerprint.error_type)

        # Add context-specific tags
        if "npm" in fingerprint.context_signature:
            tags.append("npm")
        if "docker" in fingerprint.context_signature:
            tags.append("docker")
        if "git" in fingerprint.context_signature:
            tags.append("git")

        return tags

    def _get_index_name(self, date: Optional[datetime] = None) -> str:
        """Get index name for date (defaults to today)"""
        if date is None:
            date = datetime.utcnow()
        return f"{self.INDEX_PREFIX}-{date.strftime('%Y-%m-%d')}"

    def _parse_timestamp(self, timestamp_str: str) -> datetime:
        """Parse ISO timestamp"""
        try:
            return datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        except:
            return datetime.min


    def setup_index_template(self):
        """Setup Elasticsearch index template for Claude failure signatures"""
        template = {
            "index_patterns": [f"{self.INDEX_PREFIX}-*"],
            "template": {
                "settings": {
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                    "refresh_interval": "5s",
                    "lifecycle": {
                        "name": "agent-logs-ilm-policy"  # Reuse existing policy
                    }
                },
                "mappings": {
                    "properties": {
                        "fingerprint_id": {"type": "keyword"},
                        "type": {"type": "keyword"},
                        "created_at": {"type": "date"},
                        "updated_at": {"type": "date"},
                        "first_seen": {"type": "date"},
                        "last_seen": {"type": "date"},

                        "project": {"type": "keyword"},

                        "signature": {
                            "properties": {
                                "tool_name": {"type": "keyword"},
                                "error_type": {"type": "keyword"},
                                "error_pattern": {"type": "text"},
                                "context_signature": {"type": "keyword"},
                                "cluster_size_avg": {"type": "float"}
                            }
                        },

                        "cluster_count": {"type": "integer"},
                        "total_failures": {"type": "integer"},
                        "clusters_last_hour": {"type": "integer"},
                        "clusters_last_day": {"type": "integer"},

                        "severity": {"type": "keyword"},
                        "impact_score": {"type": "float"},

                        "status": {"type": "keyword"},
                        "investigation_status": {"type": "keyword"},

                        "sample_clusters": {"type": "object", "enabled": False},
                        "tags": {"type": "keyword"}
                    }
                }
            },
            "priority": 100
        }

        try:
            self.es.indices.put_index_template(
                name=f"{self.INDEX_PREFIX}-template",
                body=template
            )
            self.logger.info(f"Created index template: {self.INDEX_PREFIX}-template")
        except Exception as e:
            self.logger.error(f"Failed to create index template: {e}")
            raise

    def update_investigation_status(self, fingerprint_id: str, investigation_status: str):
        """
        Update investigation status for a Claude failure signature.

        Args:
            fingerprint_id: Failure signature ID
            investigation_status: One of: not_started, queued, in_progress, completed, failed, ignored
        """
        now = datetime.utcnow().isoformat() + 'Z'

        # Update signature with new investigation status
        index_name = self._get_index_name()

        try:
            self.es.update(
                index=f"{self.INDEX_PREFIX}-*",
                id=fingerprint_id,
                body={
                    "script": {
                        "source": "ctx._source.investigation_status = params.investigation_status; ctx._source.updated_at = params.updated_at;",
                        "params": {
                            "investigation_status": investigation_status,
                            "updated_at": now,
                        },
                    }
                }
            )
            self.logger.info(f"Updated investigation status for {fingerprint_id}: {investigation_status}")
        except Exception as e:
            self.logger.error(f"Failed to update investigation status for {fingerprint_id}: {e}")

    def cleanup_stale_signatures(self, days: int = 7) -> tuple:
        """
        Delete signatures that haven't been seen in the specified number of days.

        Args:
            days: Number of days of inactivity before deletion (default: 7)

        Returns:
            Tuple of (number of signatures deleted, list of deleted fingerprint IDs)
        """
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        cutoff_iso = cutoff_date.isoformat() + 'Z'

        try:
            # First, get the list of signatures to be deleted (for cleanup coordination)
            query = {
                "query": {
                    "range": {
                        "last_seen": {
                            "lt": cutoff_iso
                        }
                    }
                },
                "_source": ["fingerprint_id"],
                "size": 10000  # Max signatures to clean in one pass
            }

            search_result = self.es.search(
                index=f"{self.INDEX_PREFIX}-*",
                body=query
            )

            fingerprint_ids = [hit['_source']['fingerprint_id'] for hit in search_result['hits']['hits']]

            if not fingerprint_ids:
                self.logger.info("No stale Claude signatures to clean up")
                return 0, []

            # Use delete_by_query for efficient bulk deletion
            delete_result = self.es.delete_by_query(
                index=f"{self.INDEX_PREFIX}-*",
                body=query,
                refresh=True
            )

            deleted_count = delete_result.get('deleted', 0)

            self.logger.info(f"Cleaned up {deleted_count} stale Claude signatures older than {days} days (cutoff: {cutoff_iso})")
            return deleted_count, fingerprint_ids

        except Exception as e:
            self.logger.error(f"Failed to cleanup stale signatures: {e}", exc_info=True)
            return 0, []
