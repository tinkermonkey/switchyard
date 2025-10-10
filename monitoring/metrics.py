import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

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
        """Create index templates for metrics indices"""
        try:
            # Task metrics template
            task_template = {
                "index_patterns": ["orchestrator-task-metrics-*"],
                "template": {
                    "settings": {
                        "number_of_shards": 1,
                        "number_of_replicas": 0
                    },
                    "mappings": {
                        "properties": {
                            "timestamp": {"type": "date"},
                            "agent": {"type": "keyword"},
                            "duration": {"type": "float"},
                            "success": {"type": "boolean"}
                        }
                    }
                }
            }
            
            # Quality metrics template
            quality_template = {
                "index_patterns": ["orchestrator-quality-metrics-*"],
                "template": {
                    "settings": {
                        "number_of_shards": 1,
                        "number_of_replicas": 0
                    },
                    "mappings": {
                        "properties": {
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
            
            self.es.indices.put_index_template(
                name="orchestrator-quality-metrics",
                body=quality_template
            )
            
            logger.info("Created Elasticsearch index templates for metrics")
        except Exception as e:
            logger.warning(f"Failed to create index templates: {e}")
        
    def record_task_start(self, agent: str):
        """Record task start (no-op, kept for compatibility)"""
        pass
        
    def record_task_complete(self, agent: str, duration: float, success: bool):
        """Record task completion to Elasticsearch and JSON log"""
        now = datetime.now()
        metrics_data = {
            "timestamp": now.isoformat(),
            "agent": agent,
            "duration": duration,
            "success": success
        }
        
        # Write to Elasticsearch if enabled
        if self.es_enabled:
            try:
                index_name = f"orchestrator-task-metrics-{now.strftime('%Y.%m.%d')}"
                self.es.index(
                    index=index_name,
                    document={
                        "@timestamp": now.isoformat(),
                        "agent": agent,
                        "duration": duration,
                        "success": success
                    }
                )
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
        now = datetime.now()
        metrics_data = {
            "timestamp": now.isoformat(),
            "agent": agent,
            "metric_name": metric_name,
            "score": score
        }
        
        # Write to Elasticsearch if enabled
        if self.es_enabled:
            try:
                index_name = f"orchestrator-quality-metrics-{now.strftime('%Y.%m.%d')}"
                self.es.index(
                    index=index_name,
                    document={
                        "@timestamp": now.isoformat(),
                        "agent": agent,
                        "metric_name": metric_name,
                        "score": score
                    }
                )
            except Exception as e:
                logger.error(f"Failed to write quality metrics to Elasticsearch: {e}")
        
        # Always write to JSON file as backup
        quality_file = self.metrics_dir / f"quality_metrics_{now.date()}.jsonl"
        with open(quality_file, 'a') as f:
            f.write(json.dumps(metrics_data) + '\n')