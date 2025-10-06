"""
Elasticsearch Schema for Review Learning System

Defines indices for review outcomes and learned filters.
"""

from datetime import datetime


def get_review_outcome_index_name() -> str:
    """Get index name for review outcomes (monthly rotation)"""
    return f"review-outcomes-{datetime.now().strftime('%Y.%m')}"


REVIEW_OUTCOMES_INDEX_TEMPLATE = {
    "index_patterns": ["review-outcomes-*"],
    "template": {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "index.lifecycle.name": "review-outcomes-lifecycle",
            "index.lifecycle.rollover_alias": "review-outcomes"
        },
        "mappings": {
            "properties": {
                "type": {"type": "keyword"},
                "timestamp": {"type": "date"},

                # Agent info
                "agent": {"type": "keyword"},
                "maker_agent": {"type": "keyword"},

                # Finding details
                "finding_category": {"type": "keyword"},
                "finding_severity": {"type": "keyword"},
                "finding_message": {
                    "type": "text",
                    "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}
                },
                "finding_suggestion": {"type": "text"},

                # Outcome
                "action": {"type": "keyword"},  # accepted, modified, ignored, unclear

                # Context
                "project": {"type": "keyword"},
                "issue_number": {"type": "integer"},
                "iteration": {"type": "integer"},
                "code_changed": {"type": "boolean"},
                "mentioned": {"type": "boolean"},
                "recurs": {"type": "boolean"},
                "context_json": {"type": "text"},

                # Metadata
                "review_cycle_id": {"type": "keyword"},
                "commit_before": {"type": "keyword"},
                "commit_after": {"type": "keyword"}
            }
        }
    }
}


REVIEW_FILTERS_INDEX = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0
    },
    "mappings": {
        "properties": {
            "filter_id": {"type": "keyword"},
            "created_at": {"type": "date"},
            "last_updated": {"type": "date"},

            # Agent and category
            "agent": {"type": "keyword"},
            "category": {"type": "keyword"},
            "severity": {"type": "keyword"},

            # Pattern description
            "pattern_description": {"type": "text"},
            "reason_ignored": {"type": "text"},
            "sample_findings": {"type": "text"},

            # Filter action
            "action": {"type": "keyword"},  # suppress, adjust_severity, cluster
            "from_severity": {"type": "keyword"},
            "to_severity": {"type": "keyword"},

            # Statistics
            "confidence": {"type": "float"},
            "sample_size": {"type": "integer"},
            "ignore_rate": {"type": "float"},
            "acceptance_rate": {"type": "float"},

            # Status
            "active": {"type": "boolean"},
            "manual_override": {"type": "boolean"},

            # Effectiveness tracking
            "applications_count": {"type": "integer"},
            "correct_suppressions": {"type": "integer"},
            "incorrect_suppressions": {"type": "integer"}
        }
    }
}


AGENT_PERFORMANCE_INDEX = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0
    },
    "mappings": {
        "properties": {
            "agent": {"type": "keyword"},
            "period_start": {"type": "date"},
            "period_end": {"type": "date"},
            "time_window": {"type": "keyword"},  # 7d, 30d, 90d

            # Overall metrics
            "total_reviews": {"type": "integer"},
            "total_findings": {"type": "integer"},
            "overall_acceptance_rate": {"type": "float"},
            "overall_ignore_rate": {"type": "float"},
            "avg_time_to_action_hours": {"type": "float"},
            "avg_review_cycles": {"type": "float"},

            # Category breakdown
            "category_metrics": {"type": "object", "enabled": False},

            # Severity breakdown
            "severity_metrics": {"type": "object", "enabled": False},

            # Filter info
            "active_filters_count": {"type": "integer"},
            "total_suppressions": {"type": "integer"},

            # Trend
            "trend_direction": {"type": "keyword"},  # improving, stable, declining
            "trend_confidence": {"type": "float"}
        }
    }
}


# Aggregation queries for pattern detection
AGG_LOW_VALUE_PATTERNS = {
    "size": 0,
    "query": {
        "bool": {
            "must": [
                {"term": {"type": "review_outcome"}},
                {"range": {"timestamp": {"gte": "now-30d"}}}
            ]
        }
    },
    "aggs": {
        "by_agent_category_severity": {
            "composite": {
                "size": 100,
                "sources": [
                    {"agent": {"terms": {"field": "agent"}}},
                    {"category": {"terms": {"field": "finding_category"}}},
                    {"severity": {"terms": {"field": "finding_severity"}}}
                ]
            },
            "aggs": {
                "action_breakdown": {
                    "terms": {"field": "action"},
                    "aggs": {
                        "count": {"value_count": {"field": "action"}}
                    }
                },
                "sample_findings": {
                    "top_hits": {
                        "size": 10,
                        "_source": ["finding_message", "context_json", "timestamp"]
                    }
                },
                "ignore_rate": {
                    "bucket_script": {
                        "buckets_path": {
                            "ignored": "action_breakdown['ignored']>count",
                            "total": "_count"
                        },
                        "script": "params.ignored != null ? params.ignored / params.total : 0"
                    }
                }
            }
        }
    }
}


AGG_AGENT_PERFORMANCE = {
    "size": 0,
    "query": {
        "bool": {
            "must": [
                {"term": {"type": "review_outcome"}},
                {"range": {"timestamp": {"gte": "now-30d"}}}
            ]
        }
    },
    "aggs": {
        "by_agent": {
            "terms": {"field": "agent", "size": 50},
            "aggs": {
                "acceptance_rate": {
                    "bucket_script": {
                        "buckets_path": {
                            "accepted": "actions['accepted']>_count",
                            "modified": "actions['modified']>_count",
                            "total": "_count"
                        },
                        "script": "(params.accepted + params.modified) / params.total"
                    }
                },
                "actions": {
                    "terms": {"field": "action"}
                },
                "by_category": {
                    "terms": {"field": "finding_category", "size": 20},
                    "aggs": {
                        "acceptance_rate": {
                            "bucket_script": {
                                "buckets_path": {
                                    "accepted": "actions['accepted']>_count",
                                    "modified": "actions['modified']>_count",
                                    "total": "_count"
                                },
                                "script": "(params.accepted + params.modified) / params.total"
                            }
                        },
                        "actions": {
                            "terms": {"field": "action"}
                        }
                    }
                }
            }
        }
    }
}


def setup_review_learning_indices(es_client):
    """
    Setup all Elasticsearch indices for review learning system.

    Args:
        es_client: Elasticsearch client instance
    """
    # Create index template for review outcomes (monthly rotation)
    es_client.indices.put_index_template(
        name="review-outcomes-template",
        body=REVIEW_OUTCOMES_INDEX_TEMPLATE
    )

    # Create current month's index
    current_index = get_review_outcome_index_name()
    if not es_client.indices.exists(index=current_index):
        es_client.indices.create(index=current_index)

    # Create review filters index
    if not es_client.indices.exists(index="review-filters"):
        es_client.indices.create(
            index="review-filters",
            body=REVIEW_FILTERS_INDEX
        )

    # Create agent performance index
    if not es_client.indices.exists(index="agent-performance"):
        es_client.indices.create(
            index="agent-performance",
            body=AGENT_PERFORMANCE_INDEX
        )
