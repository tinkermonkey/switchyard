"""
Elasticsearch ILM policy and index template setup for Medic
Run this once to configure Elasticsearch for Medic failure signatures
"""

import logging
from elasticsearch import Elasticsearch
import os

logger = logging.getLogger(__name__)

def setup_medic_elasticsearch():
    """
    Create ILM policy and index template for Medic failure signatures
    """
    es_hosts = os.getenv('ELASTICSEARCH_HOSTS', 'http://elasticsearch:9200')
    es = Elasticsearch([es_hosts])

    # ILM Policy as per plan: Hot (0-7 days), Warm (7-30 days), Delete (30 days)
    ilm_policy = {
        "phases": {
            "hot": {
                "min_age": "0ms",
                "actions": {
                    "set_priority": {
                        "priority": 100
                    }
                }
            },
            "warm": {
                "min_age": "7d",
                "actions": {
                    "set_priority": {
                        "priority": 50
                    }
                }
            },
            "delete": {
                "min_age": "30d",
                "actions": {
                    "delete": {
                        "delete_searchable_snapshot": True
                    }
                }
            }
        },
        "_meta": {
            "description": "ILM policy for Medic failure signatures with 30-day retention"
        }
    }

    # Create ILM policy
    try:
        es.ilm.put_lifecycle(name="medic-failure-signatures-policy", policy=ilm_policy)
        logger.info("Created ILM policy: medic-failure-signatures-policy")
    except Exception as e:
        logger.warning(f"ILM policy may already exist: {e}")

    # Index template with mappings
    index_template = {
        "index_patterns": ["medic-failure-signatures-*"],
        "template": {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "index.lifecycle.name": "medic-failure-signatures-policy",
                "index.lifecycle.rollover_alias": "medic-failure-signatures"
            },
            "mappings": {
                "properties": {
                    "@timestamp": {"type": "date"},  # Standard ES timestamp for sorting/queries
                    "fingerprint_id": {"type": "keyword"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                    "first_seen": {"type": "date"},
                    "last_seen": {"type": "date"},

                    "signature": {
                        "properties": {
                            "container_pattern": {"type": "keyword"},
                            "error_type": {"type": "keyword"},
                            "error_pattern": {"type": "text"},
                            "stack_signature": {"type": "keyword"},
                            "normalized_message": {"type": "text"}
                        }
                    },

                    "occurrence_count": {"type": "integer"},
                    "occurrences_last_hour": {"type": "integer"},
                    "occurrences_last_day": {"type": "integer"},

                    "severity": {"type": "keyword"},
                    "impact_score": {"type": "float"},

                    "status": {"type": "keyword"},
                    "investigation_status": {"type": "keyword"},

                    "sample_log_entries": {
                        "type": "nested",
                        "properties": {
                            "timestamp": {"type": "date"},
                            "container_id": {"type": "keyword"},
                            "container_name": {"type": "keyword"},
                            "raw_message": {"type": "text"},
                            "context": {"type": "object", "enabled": False}
                        }
                    },

                    "tags": {"type": "keyword"}
                }
            }
        }
    }

    # Create index template
    try:
        es.indices.put_index_template(
            name="medic-failure-signatures-template",
            body=index_template
        )
        logger.info("Created index template: medic-failure-signatures-template")
    except Exception as e:
        logger.warning(f"Index template may already exist: {e}")

    logger.info("Medic Elasticsearch setup complete")
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    setup_medic_elasticsearch()
