"""
Elasticsearch Index Mappings for Pattern Detection System

Defines all indices needed for pattern detection without PostgreSQL.
"""

# Pattern occurrences - lightweight references to detected patterns
PATTERN_OCCURRENCES_MAPPING = {
    "mappings": {
        "properties": {
            "pattern_name": {"type": "keyword"},
            "pattern_category": {"type": "keyword"},
            "severity": {"type": "keyword"},

            # Context
            "session_id": {"type": "keyword"},
            "agent_name": {"type": "keyword"},
            "project": {"type": "keyword"},
            "task_id": {"type": "keyword"},

            # References to original events
            "event_ids": {"type": "keyword"},  # Array of agent-logs document IDs
            "event_timestamp": {"type": "date"},

            # Impact
            "duration_ms": {"type": "float"},
            "error_message": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},

            # Resolution
            "resolved": {"type": "boolean"},
            "resolution_note": {"type": "text"},
            "resolved_at": {"type": "date"},

            # Detection metadata
            "detected_at": {"type": "date"},
            "detection_rule": {"type": "object", "enabled": False},  # Store but don't index
            "elasticsearch_query": {"type": "object", "enabled": False}
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
        "refresh_interval": "5s"
    }
}

# GitHub tracking - discussions and issues for patterns
PATTERN_GITHUB_TRACKING_MAPPING = {
    "mappings": {
        "properties": {
            "pattern_name": {"type": "keyword"},

            # GitHub details
            "github_type": {"type": "keyword"},  # 'discussion' or 'issue'
            "github_number": {"type": "integer"},
            "github_url": {"type": "keyword"},
            "github_state": {"type": "keyword"},  # 'open' or 'closed'
            "github_id": {"type": "keyword"},  # GitHub GraphQL ID

            # Context at creation
            "occurrence_count": {"type": "integer"},
            "first_occurrence": {"type": "date"},
            "last_occurrence": {"type": "date"},
            "affected_projects": {"type": "keyword"},
            "affected_agents": {"type": "keyword"},

            # Workflow
            "created_at": {"type": "date"},
            "closed_at": {"type": "date"},
            "resolution": {"type": "keyword"},  # 'accepted', 'rejected', 'duplicate', 'wont_fix'

            # Approval tracking (for discussions)
            "approval_count": {"type": "integer"},
            "rejection_count": {"type": "integer"},
            "last_checked_at": {"type": "date"}
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1
    }
}

# LLM analysis results
PATTERN_LLM_ANALYSIS_MAPPING = {
    "mappings": {
        "properties": {
            "pattern_name": {"type": "keyword"},

            # Analysis context
            "analysis_date": {"type": "date"},
            "occurrence_count": {"type": "integer"},
            "affected_sessions": {"type": "integer"},
            "affected_projects": {"type": "keyword"},

            # Impact metrics
            "total_time_wasted_seconds": {"type": "float"},
            "avg_impact_seconds": {"type": "float"},
            "impact_score": {"type": "float"},

            # LLM input
            "llm_prompt": {"type": "text"},
            "pattern_examples": {"type": "object", "enabled": False},
            "current_claude_md_section": {"type": "text"},

            # LLM output
            "llm_response": {"type": "text"},
            "proposed_change_diff": {"type": "text"},
            "expected_impact": {"type": "text"},
            "reasoning": {"type": "text"},

            # Quality
            "proposal_quality_score": {"type": "float"},
            "requires_human_review": {"type": "boolean"},

            # Status
            "status": {"type": "keyword"},
            "reviewed_by": {"type": "keyword"},
            "reviewed_at": {"type": "date"},

            # Metadata
            "created_at": {"type": "date"},
            "llm_model": {"type": "keyword"},
            "llm_tokens_used": {"type": "integer"},
            "llm_cost_usd": {"type": "float"}
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1
    }
}

# Daily aggregation insights
PATTERN_INSIGHTS_MAPPING = {
    "mappings": {
        "properties": {
            # Analysis metadata
            "analysis_date": {"type": "date"},
            "analysis_type": {"type": "keyword"},

            # Aggregated findings (stored as nested objects)
            "insight_data": {"type": "object", "enabled": False},
            "pattern_candidates": {"type": "nested"},

            # Statistics
            "total_events_analyzed": {"type": "integer"},
            "unique_sessions": {"type": "integer"},
            "unique_agents": {"type": "integer"},
            "unique_projects": {"type": "integer"},

            # Metadata
            "created_at": {"type": "date"}
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1
    }
}

# CLAUDE.md changes and impact tracking
CLAUDE_MD_CHANGES_MAPPING = {
    "mappings": {
        "properties": {
            "pattern_name": {"type": "keyword"},

            # Change details
            "file_path": {"type": "keyword"},
            "section_name": {"type": "keyword"},
            "change_type": {"type": "keyword"},
            "change_diff": {"type": "text"},

            # GitHub PR
            "pr_number": {"type": "integer"},
            "pr_url": {"type": "keyword"},
            "pr_state": {"type": "keyword"},

            # Impact tracking
            "deployed_at": {"type": "date"},
            "measurement_start": {"type": "date"},
            "measurement_end": {"type": "date"},

            # Before metrics
            "baseline_occurrence_count": {"type": "integer"},
            "baseline_avg_duration_ms": {"type": "float"},
            "baseline_success_rate": {"type": "float"},

            # After metrics
            "post_occurrence_count": {"type": "integer"},
            "post_avg_duration_ms": {"type": "float"},
            "post_success_rate": {"type": "float"},

            # Calculated impact
            "occurrence_reduction_pct": {"type": "float"},
            "duration_improvement_pct": {"type": "float"},
            "success_rate_delta": {"type": "float"},
            "effective": {"type": "boolean"},

            # Metadata
            "created_at": {"type": "date"},
            "created_by": {"type": "keyword"},
            "notes": {"type": "text"}
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1
    }
}

# Pattern similarity for clustering
PATTERN_SIMILARITY_MAPPING = {
    "mappings": {
        "properties": {
            "pattern_a_name": {"type": "keyword"},
            "pattern_b_name": {"type": "keyword"},

            # Similarity metrics
            "similarity_score": {"type": "float"},
            "similarity_method": {"type": "keyword"},

            # Analysis
            "common_projects": {"type": "keyword"},
            "common_agents": {"type": "keyword"},
            "should_consolidate": {"type": "boolean"},
            "consolidation_priority": {"type": "integer"},

            # Metadata
            "computed_at": {"type": "date"}
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1
    }
}


def create_all_indices(es_client):
    """
    Create all pattern detection indices in Elasticsearch

    Args:
        es_client: Elasticsearch client instance
    """
    indices = {
        "pattern-occurrences": PATTERN_OCCURRENCES_MAPPING,
        "pattern-github-tracking": PATTERN_GITHUB_TRACKING_MAPPING,
        "pattern-llm-analysis": PATTERN_LLM_ANALYSIS_MAPPING,
        "pattern-insights": PATTERN_INSIGHTS_MAPPING,
        "pattern-claude-md-changes": CLAUDE_MD_CHANGES_MAPPING,
        "pattern-similarity": PATTERN_SIMILARITY_MAPPING
    }

    for index_name, mapping in indices.items():
        if not es_client.indices.exists(index=index_name):
            es_client.indices.create(index=index_name, body=mapping)
            print(f"Created index: {index_name}")
        else:
            print(f"Index already exists: {index_name}")


if __name__ == "__main__":
    from elasticsearch import Elasticsearch
    import os

    es_host = os.getenv("ELASTICSEARCH_HOSTS", "http://elasticsearch:9200")
    es = Elasticsearch([es_host])

    create_all_indices(es)
