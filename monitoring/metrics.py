import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging
from monitoring.timestamp_utils import utc_now, utc_isoformat
from monitoring.observability import es_index_with_retry

logger = logging.getLogger(__name__)

class MetricsCollector:
    def __init__(self, port: int = 8000, elasticsearch_hosts: Optional[list] = None):
        # Ensure metrics directory exists
        self.metrics_dir = Path("orchestrator_data/metrics")
        self.metrics_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize Elasticsearch client
        self.es = None
        self.es_enabled = False
        
        try:
            from elasticsearch import Elasticsearch
            
            if elasticsearch_hosts is None:
                es_hosts_str = os.getenv("ELASTICSEARCH_HOSTS", "http://elasticsearch:9200")
                elasticsearch_hosts = es_hosts_str.split(",")
            
            self.es = Elasticsearch(elasticsearch_hosts)
            
            # Test connection
            if self.es.ping():
                self.es_enabled = True
                logger.info(f"MetricsCollector initialized with Elasticsearch: {elasticsearch_hosts}")
                
                # Create index templates for better management
                self._create_index_templates()
            else:
                logger.warning("Elasticsearch ping failed - falling back to JSON logging only")
        except ImportError:
            logger.warning("elasticsearch package not installed - using JSON logging only")
        except Exception as e:
            logger.warning(f"Failed to connect to Elasticsearch: {e} - using JSON logging only")
        
        if not self.es_enabled:
            logger.info("MetricsCollector initialized (JSON logging only)")
    
    def _create_index_templates(self):
        """Create index templates for metrics indices with ILM policies"""
        try:
            # First, create the ILM policy for metrics (7-day retention)
            ilm_policy = {
                "policy": {
                    "phases": {
                        "hot": {
                            "min_age": "0ms",
                            "actions": {
                                "rollover": {
                                    "max_age": "1d",
                                    "max_size": "5gb"
                                },
                                "set_priority": {
                                    "priority": 100
                                }
                            }
                        },
                        "warm": {
                            "min_age": "3d",
                            "actions": {
                                "set_priority": {
                                    "priority": 50
                                }
                            }
                        },
                        "delete": {
                            "min_age": "7d",
                            "actions": {
                                "delete": {}
                            }
                        }
                    }
                }
            }
            
            # Create or update ILM policy
            self.es.ilm.put_lifecycle(
                name="orchestrator-metrics-policy",
                body=ilm_policy
            )
            logger.info("Created ILM policy: orchestrator-metrics-policy (7-day retention)")
            
            # Task metrics template with ILM policy
            task_template = {
                "index_patterns": ["orchestrator-task-metrics-*"],
                "template": {
                    "settings": {
                        "number_of_shards": 1,
                        "number_of_replicas": 0,
                        "index.lifecycle.name": "orchestrator-metrics-policy",
                        "index.lifecycle.rollover_alias": "orchestrator-task-metrics"
                    },
                    "mappings": {
                        "properties": {
                            "@timestamp": {"type": "date"},
                            "timestamp": {"type": "date"},
                            "agent": {"type": "keyword"},
                            "duration": {"type": "float"},
                            "success": {"type": "boolean"}
                        }
                    }
                }
            }
            
            # Quality metrics template with ILM policy
            quality_template = {
                "index_patterns": ["orchestrator-quality-metrics-*"],
                "template": {
                    "settings": {
                        "number_of_shards": 1,
                        "number_of_replicas": 0,
                        "index.lifecycle.name": "orchestrator-metrics-policy",
                        "index.lifecycle.rollover_alias": "orchestrator-quality-metrics"
                    },
                    "mappings": {
                        "properties": {
                            "@timestamp": {"type": "date"},
                            "timestamp": {"type": "date"},
                            "agent": {"type": "keyword"},
                            "metric_name": {"type": "keyword"},
                            "score": {"type": "float"}
                        }
                    }
                }
            }
            
            # Use the v2 API for Elasticsearch 9.x
            self.es.indices.put_index_template(
                name="orchestrator-task-metrics",
                body=task_template
            )
            logger.info("Created index template: orchestrator-task-metrics (with ILM policy)")
            
            self.es.indices.put_index_template(
                name="orchestrator-quality-metrics",
                body=quality_template
            )
            logger.info("Created index template: orchestrator-quality-metrics (with ILM policy)")
            
            # Create initial write indices with aliases if they don't exist
            self._create_initial_indices()
            
        except Exception as e:
            logger.warning(f"Failed to create index templates or ILM policy: {e}")
    
    def _create_initial_indices(self):
        """Create initial write indices with aliases for ILM rollover"""
        try:
            # Use numeric suffixes for ILM rollover compatibility
            # ILM requires index names to end with a number (e.g., -000001)
            task_index = "orchestrator-task-metrics-000001"
            task_alias = "orchestrator-task-metrics"

            # Only create initial index if alias doesn't exist yet
            # (if alias exists, ILM rollover has already created newer indices)
            if not self.es.indices.exists_alias(name=task_alias):
                if not self.es.indices.exists(index=task_index):
                    self.es.indices.create(
                        index=task_index,
                        body={
                            "aliases": {
                                task_alias: {
                                    "is_write_index": True
                                }
                            }
                        }
                    )
                    logger.info(f"Created initial write index: {task_index} with alias {task_alias}")
                else:
                    # Index exists but alias doesn't - add the alias
                    self.es.indices.put_alias(
                        index=task_index,
                        name=task_alias,
                        body={"is_write_index": True}
                    )
                    logger.info(f"Added write alias {task_alias} to existing index {task_index}")
            else:
                logger.debug(f"Alias {task_alias} already exists, skipping initial index creation")

            # Create quality metrics initial index with alias
            quality_index = "orchestrator-quality-metrics-000001"
            quality_alias = "orchestrator-quality-metrics"

            # Only create initial index if alias doesn't exist yet
            if not self.es.indices.exists_alias(name=quality_alias):
                if not self.es.indices.exists(index=quality_index):
                    self.es.indices.create(
                        index=quality_index,
                        body={
                            "aliases": {
                                quality_alias: {
                                    "is_write_index": True
                                }
                            }
                        }
                    )
                    logger.info(f"Created initial write index: {quality_index} with alias {quality_alias}")
                else:
                    # Index exists but alias doesn't - add the alias
                    self.es.indices.put_alias(
                        index=quality_index,
                        name=quality_alias,
                        body={"is_write_index": True}
                    )
                    logger.info(f"Added write alias {quality_alias} to existing index {quality_index}")
            else:
                logger.debug(f"Alias {quality_alias} already exists, skipping initial index creation")
                
        except Exception as e:
            logger.warning(f"Failed to create initial indices with aliases: {e}")
        
    def record_task_start(self, agent: str):
        """Record task start (no-op, kept for compatibility)"""
        pass
        
    def record_task_complete(self, agent: str, duration: float, success: bool,
                             input_tokens: int = 0, output_tokens: int = 0,
                             cache_read_tokens: int = 0, cache_creation_tokens: int = 0):
        """Record task completion to Elasticsearch and JSON log"""
        now = utc_now()
        timestamp_str = utc_isoformat()
        metrics_data = {
            "timestamp": timestamp_str,
            "agent": agent,
            "duration": duration,
            "success": success,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens
        }

        # Write to Elasticsearch if enabled
        if self.es_enabled:
            try:
                # Write to the alias, not the dated index - ILM will handle rollover
                es_index_with_retry(self.es, "orchestrator-task-metrics", {
                    "@timestamp": timestamp_str,
                    "agent": agent,
                    "duration": duration,
                    "success": success,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cache_read_tokens,
                    "cache_creation_tokens": cache_creation_tokens
                })
            except Exception as e:
                logger.error(f"Failed to write task metrics to Elasticsearch: {e}")

        # Always write to JSON file as backup
        task_file = self.metrics_dir / f"task_metrics_{now.date()}.jsonl"
        with open(task_file, 'a') as f:
            f.write(json.dumps(metrics_data) + '\n')
    
    def update_pipeline_health(self, score: float):
        """Update overall pipeline health (no-op, kept for compatibility)"""
        pass

    def record_quality_metric(self, agent: str, metric_name: str, score: float):
        """Record quality metrics to Elasticsearch and JSON log"""
        now = utc_now()
        timestamp_str = utc_isoformat()
        metrics_data = {
            "timestamp": timestamp_str,
            "agent": agent,
            "metric_name": metric_name,
            "score": score
        }
        
        # Write to Elasticsearch if enabled
        if self.es_enabled:
            try:
                # Write to the alias, not the dated index - ILM will handle rollover
                es_index_with_retry(self.es, "orchestrator-quality-metrics", {
                    "@timestamp": timestamp_str,
                    "agent": agent,
                    "metric_name": metric_name,
                    "score": score
                })
            except Exception as e:
                logger.error(f"Failed to write quality metrics to Elasticsearch: {e}")
        
        # Always write to JSON file as backup
        quality_file = self.metrics_dir / f"quality_metrics_{now.date()}.jsonl"
        with open(quality_file, 'a') as f:
            f.write(json.dumps(metrics_data) + '\n')