"""
Elasticsearch schema definitions for pattern detection system
"""

from config.retention import MONTHLY_INDEX_PERIOD_DAYS, build_ilm_policy

# Elasticsearch index mappings for agent logs
AGENT_LOGS_MAPPING = {
    "mappings": {
        "properties": {
            # Core identifiers
            "timestamp": {
                "type": "date"
            },
            "session_id": {
                "type": "keyword"
            },
            "agent_name": {
                "type": "keyword"
            },
            "project": {
                "type": "keyword"
            },
            "task_id": {
                "type": "keyword"
            },

            # Event classification
            "event_type": {
                "type": "keyword"
            },
            "event_category": {
                "type": "keyword"  # tool_call, tool_result, user_message, agent_lifecycle
            },

            # Tool call information
            "tool_name": {
                "type": "keyword"
            },
            "tool_params": {
                "type": "object",
                "enabled": False  # Store but don't index (can be large)
            },
            "tool_params_text": {
                "type": "text",
                "analyzer": "standard"
            },

            # Tool result information
            "result": {
                "type": "object",
                "enabled": False
            },
            "result_summary": {
                "type": "text",
                "analyzer": "standard"
            },
            "success": {
                "type": "boolean"
            },
            "error_message": {
                "type": "text",
                "analyzer": "standard",
                "fields": {
                    "keyword": {
                        "type": "keyword",
                        "ignore_above": 512
                    }
                }
            },

            # Performance metrics
            "duration_ms": {
                "type": "float"
            },
            "context_tokens": {
                "type": "integer"
            },
            "retry_count": {
                "type": "integer"
            },

            # Pattern detection metadata
            "user_correction": {
                "type": "boolean"
            },
            "is_retry": {
                "type": "boolean"
            },
            "previous_event_id": {
                "type": "keyword"  # Link to previous event if retry
            },

            # Enriched metadata
            "issue_number": {
                "type": "integer"
            },
            "discussion_id": {
                "type": "keyword"
            },
            "board": {
                "type": "keyword"
            },
            "pipeline_type": {
                "type": "keyword"
            },
            "pipeline_run_id": {
                "type": "keyword"  # Link to pipeline run
            },

            # Full event data for reference
            "raw_event": {
                "type": "object",
                "enabled": False
            }
        }
    },
    "settings": {
        "index": {
            "number_of_shards": 1,
            "number_of_replicas": 0,  # Single node, no replicas needed
            "refresh_interval": "5s",
            "lifecycle": {
                "name": "agent-logs-ilm-policy"
            }
        }
    }
}


# Index template for time-series pattern
AGENT_LOGS_TEMPLATE = {
    "index_patterns": ["agent-logs-*"],
    "template": AGENT_LOGS_MAPPING,
    "priority": 100
}


# Lifecycle policy for agent-logs-*, agent-events-* and claude-streams-*.
# Daily date-based index names, so no rollover action and no index period.
# Retention comes from config/retention.py's single RETENTION_DAYS value (30 days
# by default), so Elasticsearch and the filesystem sweep in
# services/data_retention.py cannot disagree -- and a change takes effect on data
# that already exists, because ILM re-reads a policy rather than stamping it onto
# an index at creation. See config/retention.py for what this replaced.
AGENT_LOGS_ILM_POLICY = build_ilm_policy()


# Separate mapping for agent lifecycle events
AGENT_EVENTS_MAPPING = {
    "mappings": {
        "properties": {
            "timestamp": {"type": "date"},
            "agent_name": {"type": "keyword"},
            "project": {"type": "keyword"},
            "task_id": {"type": "keyword"},
            "event_type": {"type": "keyword"},
            "event_category": {"type": "keyword"},
            "duration_ms": {"type": "float"},
            "context_tokens": {"type": "integer"},
            "success": {"type": "boolean"},
            "error_message": {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 512}}},
            "issue_number": {"type": "integer"},
            "discussion_id": {"type": "keyword"},
            "board": {"type": "keyword"},
            "pipeline_type": {"type": "keyword"},
            "pipeline_run_id": {"type": "keyword"},  # Link to pipeline run
            "raw_event": {"type": "object", "enabled": True}  # Enable indexing for lifecycle events
        }
    },
    "settings": {
        "index": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "refresh_interval": "5s",
            "lifecycle": {
                "name": "agent-logs-ilm-policy"
            }
        }
    }
}

CLAUDE_STREAMS_MAPPING = {
    "mappings": {
        "properties": {
            "timestamp":             {"type": "date"},
            "agent_name":            {"type": "keyword"},
            "project":               {"type": "keyword"},
            "task_id":               {"type": "keyword"},
            "pipeline_run_id":       {"type": "keyword"},
            "event_type":            {"type": "keyword"},
            "event_category":        {"type": "keyword"},
            "tool_name":             {"type": "keyword"},
            "tool_params":           {"type": "object", "enabled": False},
            "tool_params_text":      {"type": "text", "analyzer": "standard"},
            "output_text":           {"type": "text", "analyzer": "standard"},
            "result_text":           {"type": "text", "analyzer": "standard"},
            "parent_tool_use_id":    {"type": "keyword"},
            "success":               {"type": "boolean"},
            "error_message":         {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 512}}},
            "raw_event":             {"type": "object", "enabled": False},
            "token_input":           {"type": "integer"},
            "token_cache_read":      {"type": "integer"},
            "token_cache_creation":  {"type": "integer"},
            "token_effective_input": {"type": "integer"},
            "token_output":          {"type": "integer"},
            "token_total":           {"type": "integer"},
            "token_model":           {"type": "keyword"},
        }
    },
    "settings": {
        "index": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "refresh_interval": "10s",
            "lifecycle": {"name": "agent-logs-ilm-policy"}
        }
    }
}

CLAUDE_STREAMS_TEMPLATE = {
    "index_patterns": ["claude-streams-*"],
    "template": CLAUDE_STREAMS_MAPPING,
    "priority": 100
}

# Mapping for pipeline runs
PIPELINE_RUNS_MAPPING = {
    "mappings": {
        "properties": {
            "id": {"type": "keyword"},
            "issue_number": {"type": "integer"},
            "issue_title": {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 512}}},
            "issue_url": {"type": "keyword"},
            "project": {"type": "keyword"},
            "board": {"type": "keyword"},
            "started_at": {"type": "date"},
            "ended_at": {"type": "date"},
            "status": {"type": "keyword"},  # active, completed
            "duration_ms": {"type": "long"}  # Calculated field
        }
    },
    "settings": {
        "index": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "refresh_interval": "5s",
            "lifecycle": {
                "name": "agent-logs-ilm-policy"
            }
        }
    }
}

# Index templates
AGENT_EVENTS_TEMPLATE = {
    "index_patterns": ["agent-events-*"],
    "template": AGENT_EVENTS_MAPPING,
    "priority": 100
}

# ILM policy for OTEL data streams (logs-claude.otel-* and metrics-claude.otel-*)
#
# The rollover action is NOT optional here, unlike everywhere else it appears.
# ILM refuses to delete the write index of a data stream, and these streams
# have no other rollover trigger -- so without it the single backing index
# stays the write index forever and the delete phase is unreachable. That is
# how .ds-logs-claude.otel-default-2026.07.07-000001 accumulated two months of
# data under a policy that claimed to delete at 14 days.
#
# Retention comes from config/retention.py's single RETENTION_DAYS value (30 days
# by default), so Elasticsearch and the filesystem sweep in
# services/data_retention.py cannot disagree -- and a change takes effect on data
# that already exists, because ILM re-reads a policy rather than stamping it onto
# an index at creation. See config/retention.py for what this replaced.
CLAUDE_OTEL_ILM_POLICY = build_ilm_policy(
    hot_actions={"rollover": {"max_age": "1d", "max_size": "5gb"}}
)

# Priority-300 override templates for OTEL data streams.
# These win over the built-in logs-otel@template / metrics-otel@template (priority 120)
# and attach claude-otel-ilm-policy while composing the same OTEL component chain.
CLAUDE_OTEL_LOGS_TEMPLATE = {
    "index_patterns": ["logs-claude.otel-*"],
    "composed_of": [
        "logs@mappings",
        "logs@settings",
        "otel@mappings",
        "logs-otel@mappings",
        "semconv-resource-to-ecs@mappings",
        "ecs@mappings",
    ],
    "priority": 300,
    "data_stream": {},
    "template": {
        "settings": {
            "index": {
                "lifecycle": {
                    "name": "claude-otel-ilm-policy"
                }
            }
        }
    }
}

CLAUDE_OTEL_METRICS_TEMPLATE = {
    "index_patterns": ["metrics-claude.otel-*"],
    "composed_of": [
        "metrics@tsdb-settings",
        "otel@mappings",
        "metrics-otel@mappings",
        "semconv-resource-to-ecs@mappings",
        "ecs-tsdb@mappings",
    ],
    "priority": 300,
    "data_stream": {},
    "template": {
        "settings": {
            "index": {
                "lifecycle": {
                    "name": "claude-otel-ilm-policy"
                }
            }
        }
    }
}


def get_index_name(date=None, event_category=None):
    """
    Generate index name for a given date and event category

    Args:
        date: datetime object (defaults to now)
        event_category: Category of event (determines index prefix)

    Returns:
        Index name like 'agent-events-2025-10-05' or 'decision-events-2025-10-05'
    """
    from datetime import datetime

    if date is None:
        date = datetime.now()

    # Route to correct index based on category
    if event_category in ['agent_lifecycle', 'claude_api']:
        prefix = 'agent-events'
    elif event_category in ['claude_stream', 'tool_call', 'tool_result', 'agent_output', 'agent_thinking']:
        prefix = 'claude-streams'
    else:
        # 'other', 'decision', and anything unrecognised → decision-events
        prefix = 'decision-events'

    return f"{prefix}-{date.strftime('%Y-%m-%d')}"


def enrich_event(event_data: dict) -> dict:
    """
    Enrich raw Redis event with additional metadata for Elasticsearch

    Args:
        event_data: Raw event from Redis

    Returns:
        Enriched event ready for Elasticsearch indexing
    """
    from monitoring.timestamp_utils import utc_isoformat

    enriched = {
        "timestamp": event_data.get("timestamp") or utc_isoformat(),
        "raw_event": event_data
    }

    # Extract core fields
    enriched["agent_name"] = event_data.get("agent")
    enriched["project"] = event_data.get("project")
    enriched["task_id"] = event_data.get("task_id")
    enriched["event_type"] = event_data.get("event_type")

    # Categorize event
    event_type = enriched["event_type"] or ""
    if "tool" in event_type.lower():
        enriched["event_category"] = "tool_execution"
    elif event_type in ["agent_initialized", "agent_completed", "agent_failed", "task_received"]:
        enriched["event_category"] = "agent_lifecycle"
    elif event_type in [
        "container_launch_started", "container_launch_succeeded",
        "container_launch_failed", "container_execution_started",
        "container_execution_completed", "container_execution_failed",
        "prompt_constructed",
    ]:
        enriched["event_category"] = "agent_lifecycle"
    elif "claude" in event_type.lower():
        enriched["event_category"] = "claude_api"
    else:
        enriched["event_category"] = "other"

    # Extract nested data fields
    data = event_data.get("data", {})

    # Tool information
    if "tool_name" in data:
        enriched["tool_name"] = data["tool_name"]

    # Performance metrics
    if "duration_ms" in data:
        enriched["duration_ms"] = data["duration_ms"]

    # Token usage
    if "input_tokens" in data and "output_tokens" in data:
        enriched["context_tokens"] = data["input_tokens"] + data["output_tokens"]
    elif "total_tokens" in data:
        enriched["context_tokens"] = data["total_tokens"]

    # Error information
    if "error" in data:
        enriched["error_message"] = str(data["error"])
        enriched["success"] = False
    elif "success" in data:
        enriched["success"] = data["success"]
    else:
        # Infer from event type
        enriched["success"] = event_type not in ["agent_failed", "tool_execution_failed"]

    # Issue/Discussion metadata
    if "issue_number" in data:
        enriched["issue_number"] = data["issue_number"]
    if "discussion_id" in data:
        enriched["discussion_id"] = data["discussion_id"]
    if "board" in data:
        enriched["board"] = data["board"]

    # Initialize pattern detection fields
    enriched["retry_count"] = 0
    enriched["is_retry"] = False
    enriched["user_correction"] = False

    return enriched


def enrich_claude_log(log_data: dict) -> dict:
    """
    Enrich Claude Code log events (tool calls, results, etc.)

    Args:
        log_data: Raw Claude log from Redis

    Returns:
        Enriched log ready for Elasticsearch
    """
    from monitoring.timestamp_utils import timestamp_to_utc_isoformat, utc_now
    import copy

    # Convert timestamp to ISO format for raw_event to avoid parsing issues
    raw_event = copy.deepcopy(log_data)
    if "timestamp" in raw_event and isinstance(raw_event["timestamp"], (int, float)):
        raw_event["timestamp"] = timestamp_to_utc_isoformat(raw_event["timestamp"])

    # Get timestamp, defaulting to current UTC time if not present
    timestamp = log_data.get("timestamp")
    if isinstance(timestamp, (int, float)):
        timestamp_str = timestamp_to_utc_isoformat(timestamp)
    else:
        timestamp_str = timestamp_to_utc_isoformat(utc_now().timestamp())

    enriched = {
        "timestamp": timestamp_str,
        "agent_name": log_data.get("agent"),
        "project": log_data.get("project"),
        "task_id": log_data.get("task_id"),
        "pipeline_run_id": log_data.get("pipeline_run_id"),  # Extract pipeline_run_id
        "raw_event": raw_event
    }

    # Extract from nested event structure
    event = log_data.get("event", {})
    event_type = event.get("type")

    # parent_tool_use_id links sub-agent events back to the Agent tool call that spawned them
    parent_tool_use_id = event.get("parent_tool_use_id")
    if parent_tool_use_id:
        enriched["parent_tool_use_id"] = parent_tool_use_id

    # Determine event type from Claude event structure
    # New format: event.type = "assistant"/"user" with event.message.content[]
    if event_type == "assistant":
        enriched["event_category"] = "claude_stream"
        enriched["event_type"] = "assistant_message"

        # Check content for tool_use
        message = event.get("message", {})
        content = message.get("content", [])
        if content and isinstance(content, list):
            for item in content:
                if item.get("type") == "tool_use":
                    enriched["event_category"] = "tool_call"
                    enriched["event_type"] = "tool_call"
                    raw_name = item.get("name", "")
                    inp = item.get("input") or {}
                    if raw_name == "Skill" and inp.get("skill"):
                        enriched["tool_name"] = inp["skill"]
                    elif raw_name == "Task" and inp.get("subagent_type"):
                        enriched["tool_name"] = inp["subagent_type"]
                    else:
                        enriched["tool_name"] = raw_name
                    enriched["tool_params"] = inp
                    break
                elif item.get("type") == "text":
                    enriched["event_type"] = "text_output"
                    enriched["output_text"] = item.get("text", "")
                    break

        # Extract token usage as indexed top-level fields
        usage = message.get("usage")
        if usage:
            token_input = usage.get("input_tokens") or 0
            token_cache_read = usage.get("cache_read_input_tokens") or 0
            token_cache_creation = usage.get("cache_creation_input_tokens") or 0
            token_output = usage.get("output_tokens") or 0
            enriched["token_input"] = token_input
            enriched["token_cache_read"] = token_cache_read
            enriched["token_cache_creation"] = token_cache_creation
            enriched["token_effective_input"] = token_input + token_cache_read + token_cache_creation
            enriched["token_output"] = token_output
            enriched["token_total"] = token_input + token_cache_read + token_cache_creation + token_output
            model = message.get("model")
            if model:
                enriched["token_model"] = model

    elif event_type == "user":
        enriched["event_category"] = "claude_stream"
        enriched["event_type"] = "user_message"

        # Check content for tool_result
        message = event.get("message", {})
        content = message.get("content", [])
        if content and isinstance(content, list):
            for item in content:
                if item.get("type") == "tool_result":
                    enriched["event_category"] = "tool_result"
                    enriched["event_type"] = "tool_result"
                    enriched["tool_name"] = item.get("tool_use_id")
                    enriched["success"] = not item.get("is_error", False)
                    result_content = item.get("content")
                    if isinstance(result_content, str) and result_content:
                        enriched["result_text"] = result_content
                    elif isinstance(result_content, list):
                        # content can be a list of content blocks
                        texts = [c.get("text", "") for c in result_content if c.get("type") == "text"]
                        combined = "\n".join(texts)
                        if combined:
                            enriched["result_text"] = combined
                    break

    # Old format: direct keys in event object (fallback for compatibility)
    elif "tool_use" in event:
        enriched["event_category"] = "tool_call"
        enriched["event_type"] = "tool_call"

        tool_use = event["tool_use"]
        enriched["tool_name"] = tool_use.get("name")
        enriched["tool_params"] = tool_use.get("input", {})

        # Create searchable text version of params
        if enriched["tool_params"]:
            import json
            enriched["tool_params_text"] = json.dumps(enriched["tool_params"])

    elif "tool_result" in event:
        enriched["event_category"] = "tool_result"
        enriched["event_type"] = "tool_result"

        tool_result = event["tool_result"]
        enriched["tool_name"] = tool_result.get("tool_name")
        enriched["result"] = tool_result.get("content")
        enriched["success"] = not tool_result.get("is_error", False)

        if tool_result.get("is_error"):
            enriched["error_message"] = str(tool_result.get("content", "Unknown error"))

        # Create summary of result for search
        result_content = tool_result.get("content", "")
        if isinstance(result_content, str):
            enriched["result_summary"] = result_content[:500]  # First 500 chars

    elif "text" in event:
        # Text output from Claude
        enriched["event_category"] = "agent_output"
        enriched["event_type"] = "text_output"

    elif "thinking" in event:
        # Claude's thinking process
        enriched["event_category"] = "agent_thinking"
        enriched["event_type"] = "thinking"

    else:
        # Unrecognised event structure (e.g. Claude Code's 'system' init event,
        # 'result' summary event). Still a claude streaming event — route to
        # claude-streams-* so it stays with the rest of the conversation.
        enriched["event_category"] = "claude_stream"
        enriched["event_type"] = event.get("type", "unknown")

    return enriched


# Mapping for project-level daily rollup metrics
PROJECT_METRICS_MAPPING = {
    "mappings": {
        "properties": {
            "project":            {"type": "keyword"},
            "day_bucket":         {"type": "date"},
            "pipeline_run_count": {"type": "integer"},
            "computed_at":        {"type": "date"},
            # Nested metric groups — stored and queryable
            "tokens":             {"type": "object", "enabled": True},
            "context":            {"type": "object", "enabled": True},
            "tool_calls":         {"type": "object", "enabled": True},
            "review_cycles":      {"type": "object", "enabled": True},
            "repair_cycles":      {"type": "object", "enabled": True},
            "pr_review_cycles":   {"type": "object", "enabled": True},
            "pipeline_outcomes":  {"type": "object", "enabled": True},
        }
    },
    "settings": {
        "index": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "refresh_interval": "30s",
            "lifecycle": {
                "name": "project-metrics-ilm-policy"
            }
        }
    }
}

PROJECT_METRICS_TEMPLATE = {
    "index_patterns": ["project-metrics-*"],
    "template": PROJECT_METRICS_MAPPING,
    "priority": 100
}

# ILM policy for project-metrics-%Y.%m. Monthly indices, so the delete age is
# the window plus one index period -- otherwise March's index would be deleted
# on 31 March, taking that morning's writes with it. See
# config/retention.py:delete_phase_days().
# Retention comes from config/retention.py's single RETENTION_DAYS value (30 days
# by default), so Elasticsearch and the filesystem sweep in
# services/data_retention.py cannot disagree -- and a change takes effect on data
# that already exists, because ILM re-reads a policy rather than stamping it onto
# an index at creation. See config/retention.py for what this replaced.
PROJECT_METRICS_ILM_POLICY = build_ilm_policy(
    index_period_days=MONTHLY_INDEX_PERIOD_DAYS
)


# ─── Test Cycle Analytics ────────────────────────────────────────────────────

# Per-iteration records written after each repair cycle completes.
# Monthly rotation (YYYY.MM); retention from config/retention.py.
TEST_CYCLE_RECORDS_MAPPING = {
    "mappings": {
        "properties": {
            # Identity
            "pipeline_run_id":           {"type": "keyword"},
            "project":                   {"type": "keyword"},
            "test_type":                 {"type": "keyword"},
            "test_type_index":           {"type": "integer"},
            "iteration_number":          {"type": "integer"},
            "sub_run_index":             {"type": "integer"},
            # Test execution result
            "test_execution_passed":     {"type": "boolean"},
            "test_failure_count":        {"type": "integer"},
            "test_warning_count":        {"type": "integer"},
            "test_execution_duration_s": {"type": "float"},
            # Fix cycle result (null if no fix cycle on this sub-run)
            "had_fix_cycle":             {"type": "boolean"},
            "fix_cycle_files_fixed":     {"type": "integer"},
            "fix_cycle_duration_s":      {"type": "float"},
            # Metadata
            "@timestamp":                {"type": "date"},
            "recorded_at":               {"type": "date"},
        }
    },
    "settings": {
        "index": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "refresh_interval": "30s",
            "lifecycle": {"name": "test-cycle-records-ilm-policy"},
        }
    },
}

TEST_CYCLE_RECORDS_TEMPLATE = {
    "index_patterns": ["orchestrator-test-cycle-records-*"],
    "priority": 100,
    "template": TEST_CYCLE_RECORDS_MAPPING,
}

# ILM policy for orchestrator-test-cycle-records-%Y.%m. Monthly indices, hence
# the index period -- see config/retention.py:delete_phase_days(). This family
# went from a hand-written 180 days to the shared window; the weekly rollup
# that reads it (scripts/calculate_test_cycle_stats.py) derives its lookback
# from the same value so it cannot ask for a history that no longer exists.
# Retention comes from config/retention.py's single RETENTION_DAYS value (30 days
# by default), so Elasticsearch and the filesystem sweep in
# services/data_retention.py cannot disagree -- and a change takes effect on data
# that already exists, because ILM re-reads a policy rather than stamping it onto
# an index at creation. See config/retention.py for what this replaced.
TEST_CYCLE_RECORDS_ILM_POLICY = build_ilm_policy(
    index_period_days=MONTHLY_INDEX_PERIOD_DAYS
)

# Rolled-up per-project/test-type stats; single index, updated weekly.
TEST_CYCLE_STATS_MAPPING = {
    "mappings": {
        "properties": {
            "project":                          {"type": "keyword"},
            "test_type":                        {"type": "keyword"},
            "calculated_at":                    {"type": "date"},
            # Coverage
            "total_iterations":                 {"type": "integer"},
            "total_pipeline_runs":              {"type": "integer"},
            "data_from":                        {"type": "date"},
            "data_to":                          {"type": "date"},
            # All test executions
            "all_exec_sample_count":            {"type": "integer"},
            "all_exec_duration_min_s":          {"type": "float"},
            "all_exec_duration_max_s":          {"type": "float"},
            "all_exec_duration_avg_s":          {"type": "float"},
            "all_exec_duration_median_s":       {"type": "float"},
            "all_exec_duration_p90_s":          {"type": "float"},
            "all_exec_pass_rate":               {"type": "float"},
            # Clean baseline: iteration_number=1, sub_run_index=1, test_execution_passed=True
            "clean_pass_sample_count":          {"type": "integer"},
            "clean_pass_duration_min_s":        {"type": "float"},
            "clean_pass_duration_max_s":        {"type": "float"},
            "clean_pass_duration_avg_s":        {"type": "float"},
            "clean_pass_duration_median_s":     {"type": "float"},
            "clean_pass_duration_p90_s":        {"type": "float"},
            # Failure / fix profile
            "avg_failure_count_when_failing":   {"type": "float"},
            "avg_fix_cycle_duration_s":         {"type": "float"},
            "avg_iterations_per_repair_cycle":  {"type": "float"},
        }
    },
    "settings": {
        "index": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "refresh_interval": "60s",
        }
    },
}
