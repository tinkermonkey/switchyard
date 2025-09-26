from prometheus_client import Counter, Histogram, Gauge, start_http_server
import json
from datetime import datetime
from pathlib import Path

# Define metrics
task_counter = Counter('tasks_total', 'Total tasks processed', ['agent', 'status'])
task_duration = Histogram('task_duration_seconds', 'Task duration', ['agent'])
active_tasks = Gauge('active_tasks', 'Currently active tasks', ['agent'])
pipeline_health = Gauge('pipeline_health', 'Pipeline health score')

class MetricsCollector:
    def __init__(self, port: int = 8000):
        # Ensure metrics directory exists
        self.metrics_dir = Path("orchestrator_data/metrics")
        self.metrics_dir.mkdir(parents=True, exist_ok=True)

        # Start Prometheus metrics server
        start_http_server(port)
        
    def record_task_start(self, agent: str):
        active_tasks.labels(agent=agent).inc()
        
    def record_task_complete(self, agent: str, duration: float, success: bool):
        active_tasks.labels(agent=agent).dec()
        task_duration.labels(agent=agent).observe(duration)
        status = 'success' if success else 'failure'
        task_counter.labels(agent=agent, status=status).inc()
    
    def update_pipeline_health(self, score: float):
        """Update overall pipeline health (0-100)"""
        pipeline_health.set(score)

    def record_quality_metric(self, agent: str, metric_name: str, score: float):
        """Record quality metrics for an agent (for main.py compatibility)"""
        # Store quality metrics in JSON format for historical tracking
        metrics_data = {
            "timestamp": datetime.now().isoformat(),
            "agent": agent,
            "metric_name": metric_name,
            "score": score
        }

        quality_file = self.metrics_dir / f"quality_metrics_{datetime.now().date()}.jsonl"
        with open(quality_file, 'a') as f:
            f.write(json.dumps(metrics_data) + '\n')