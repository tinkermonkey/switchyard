"""
Integration tests for timestamp consistency across Elasticsearch writes

Verifies that all data sources writing to Elasticsearch use consistent
UTC timestamps with 'Z' suffix.
"""
import pytest
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from monitoring.metrics import MetricsCollector
from monitoring.observability import ObservabilityManager, EventType
from services.pattern_detection_schema import enrich_event, enrich_claude_log


class TestMetricsTimestamps:
    """Test timestamp consistency in metrics collection"""
    
    def test_task_metrics_use_utc_with_z_suffix(self, tmp_path):
        """Verify task metrics use UTC timestamps with 'Z' suffix"""
        # Create metrics collector with mocked Elasticsearch
        # Patch where it's imported (inside __init__), not at module level
        with patch('elasticsearch.Elasticsearch') as mock_es_class:
            mock_es = MagicMock()
            mock_es_class.return_value = mock_es
            mock_es.ping.return_value = True
            
            collector = MetricsCollector(elasticsearch_hosts=['http://localhost:9200'])
            collector.metrics_dir = tmp_path
            
            # Record task completion
            collector.record_task_complete(
                agent="test_agent",
                duration=123.45,
                success=True
            )
            
            # Verify Elasticsearch write called with UTC timestamp
            assert mock_es.index.called
            call_args = mock_es.index.call_args
            document = call_args.kwargs['document']
            
            # Check @timestamp field
            timestamp = document['@timestamp']
            assert timestamp.endswith('Z'), f"Timestamp should end with 'Z': {timestamp}"
            
            # Verify parseable as UTC
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            assert dt.tzinfo == timezone.utc
    
    def test_quality_metrics_use_utc_with_z_suffix(self, tmp_path):
        """Verify quality metrics use UTC timestamps with 'Z' suffix"""
        with patch('elasticsearch.Elasticsearch') as mock_es_class:
            mock_es = MagicMock()
            mock_es_class.return_value = mock_es
            mock_es.ping.return_value = True
            
            collector = MetricsCollector(elasticsearch_hosts=['http://localhost:9200'])
            collector.metrics_dir = tmp_path
            
            # Record quality metric
            collector.record_quality_metric(
                agent="test_agent",
                metric_name="code_quality",
                score=0.95
            )
            
            # Verify Elasticsearch write
            assert mock_es.index.called
            call_args = mock_es.index.call_args
            document = call_args.kwargs['document']
            
            timestamp = document['@timestamp']
            assert timestamp.endswith('Z')
            
            # Verify UTC
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            assert dt.tzinfo == timezone.utc
    
    def test_json_backup_timestamps_consistent(self, tmp_path):
        """Verify JSON backup files also use UTC timestamps"""
        with patch('elasticsearch.Elasticsearch') as mock_es_class:
            mock_es = MagicMock()
            mock_es_class.return_value = mock_es
            mock_es.ping.return_value = True
            
            collector = MetricsCollector(elasticsearch_hosts=['http://localhost:9200'])
            collector.metrics_dir = tmp_path
            
            # Record task
            collector.record_task_complete(
                agent="test_agent",
                duration=123.45,
                success=True
            )
            
            # Read JSON backup
            json_files = list(tmp_path.glob("task_metrics_*.jsonl"))
            assert len(json_files) > 0
            
            with open(json_files[0], 'r') as f:
                line = f.readline()
                data = json.loads(line)
                
                timestamp = data['timestamp']
                assert timestamp.endswith('Z')


class TestObservabilityTimestamps:
    """Test timestamp consistency in observability events"""
    
    def test_observability_events_use_utc_with_z_suffix(self):
        """Verify observability events use UTC timestamps with 'Z' suffix"""
        # Create mock Redis and Elasticsearch clients
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        
        mock_es = MagicMock()
        
        # Pass clients directly (ObservabilityManager expects clients, not connection params)
        obs = ObservabilityManager(
            redis_client=mock_redis,
            elasticsearch_client=mock_es
        )
        
        # Emit event
        obs.emit(
            event_type=EventType.AGENT_STARTED,
            agent="test_agent",
            task_id="test_task",
            project="test_project",
            data={"test": "data"}
        )
        
        # Check Redis publish call
        assert mock_redis.publish.called
        event_json = mock_redis.publish.call_args[0][1]
        event_data = json.loads(event_json)
        
        timestamp = event_data['timestamp']
        assert timestamp.endswith('Z'), f"Event timestamp should end with 'Z': {timestamp}"
        
        # Verify UTC
        dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        assert dt.tzinfo == timezone.utc


class TestPatternDetectionSchemaTimestamps:
    """Test timestamp consistency in pattern detection schema enrichment"""
    
    def test_enrich_event_preserves_utc_timestamps(self):
        """Verify enrich_event preserves UTC timestamps"""
        event_data = {
            'timestamp': '2025-10-10T12:00:00Z',
            'agent': 'test_agent',
            'project': 'test_project',
            'task_id': 'test_task',
            'event_type': 'agent_started',
            'data': {}
        }
        
        enriched = enrich_event(event_data)
        
        assert enriched['timestamp'] == '2025-10-10T12:00:00Z'
        assert enriched['timestamp'].endswith('Z')
    
    def test_enrich_event_defaults_to_utc_when_missing(self):
        """Verify enrich_event uses UTC when timestamp missing"""
        event_data = {
            'agent': 'test_agent',
            'project': 'test_project',
            'task_id': 'test_task',
            'event_type': 'agent_started',
            'data': {}
        }
        
        enriched = enrich_event(event_data)
        
        # Should have timestamp with 'Z' suffix
        assert 'timestamp' in enriched
        assert enriched['timestamp'].endswith('Z')
        
        # Verify parseable as UTC
        dt = datetime.fromisoformat(enriched['timestamp'].replace('Z', '+00:00'))
        assert dt.tzinfo == timezone.utc
    
    def test_enrich_claude_log_converts_unix_to_utc(self):
        """Verify enrich_claude_log converts Unix timestamps to UTC"""
        log_data = {
            'timestamp': 1757401200.0,  # Unix timestamp
            'agent': 'test_agent',
            'project': 'test_project',
            'task_id': 'test_task',
            'event': {
                'type': 'assistant',
                'message': {
                    'content': []
                }
            }
        }
        
        enriched = enrich_claude_log(log_data)
        
        # Should have ISO8601 timestamp with 'Z' suffix
        assert 'timestamp' in enriched
        timestamp = enriched['timestamp']
        assert timestamp.endswith('Z')
        
        # Verify UTC
        dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        assert dt.tzinfo == timezone.utc
    
    def test_enrich_claude_log_handles_missing_timestamp(self):
        """Verify enrich_claude_log defaults to current UTC when timestamp missing"""
        log_data = {
            'agent': 'test_agent',
            'project': 'test_project',
            'task_id': 'test_task',
            'event': {
                'type': 'assistant',
                'message': {
                    'content': []
                }
            }
        }
        
        enriched = enrich_claude_log(log_data)
        
        # Should have timestamp with 'Z' suffix
        assert 'timestamp' in enriched
        assert enriched['timestamp'].endswith('Z')
        
        # Verify recent and UTC
        dt = datetime.fromisoformat(enriched['timestamp'].replace('Z', '+00:00'))
        assert dt.tzinfo == timezone.utc
        
        # Should be within last second
        now = datetime.now(timezone.utc)
        assert (now - dt).total_seconds() < 2


class TestCrossFunctionalConsistency:
    """Test timestamp consistency across all Elasticsearch writers"""
    
    def test_all_elasticsearch_writes_use_z_suffix(self, tmp_path):
        """Verify all Elasticsearch writes across the system use 'Z' suffix"""
        # Setup mocks - patch at the source package level
        with patch('elasticsearch.Elasticsearch') as mock_es_class:
            mock_metrics_es = MagicMock()
            mock_obs_es = MagicMock()
            
            # Return different mock instances for different calls
            mock_es_class.side_effect = [mock_metrics_es, mock_obs_es]
            mock_metrics_es.ping.return_value = True
            
            mock_redis = MagicMock()
            mock_redis.ping.return_value = True
            
            # Create services
            metrics = MetricsCollector(elasticsearch_hosts=['http://localhost:9200'])
            metrics.metrics_dir = tmp_path
            
            obs = ObservabilityManager(
                redis_client=mock_redis,
                elasticsearch_client=mock_obs_es
            )
            
            # Generate events from different sources
            timestamps = []
            
            # 1. Metrics collector
            metrics.record_task_complete("agent1", 100.0, True)
            if mock_metrics_es.index.called:
                doc = mock_metrics_es.index.call_args.kwargs['document']
                timestamps.append(('metrics_task', doc['@timestamp']))
            
            metrics.record_quality_metric("agent1", "quality", 0.9)
            if mock_metrics_es.index.called:
                doc = mock_metrics_es.index.call_args.kwargs['document']
                timestamps.append(('metrics_quality', doc['@timestamp']))
            
            # 2. Observability events
            obs.emit(EventType.AGENT_STARTED, "agent1", "task1", "project1", {})
            if mock_redis.publish.called:
                event_json = mock_redis.publish.call_args[0][1]
                event = json.loads(event_json)
                timestamps.append(('observability', event['timestamp']))
            
            # 3. Schema enrichment
            enriched_event = enrich_event({
                'agent': 'agent1',
                'project': 'project1',
                'task_id': 'task1',
                'event_type': 'test',
                'data': {}
            })
            timestamps.append(('schema_event', enriched_event['timestamp']))
            
            enriched_log = enrich_claude_log({
                'timestamp': 1757401200.0,
                'agent': 'agent1',
                'project': 'project1',
                'task_id': 'task1',
                'event': {'type': 'assistant', 'message': {'content': []}}
            })
            timestamps.append(('schema_log', enriched_log['timestamp']))
            
            # Verify ALL timestamps end with 'Z'
            for source, timestamp in timestamps:
                assert timestamp.endswith('Z'), f"{source} timestamp should end with 'Z': {timestamp}"
                
                # Verify all are valid UTC timestamps
                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                assert dt.tzinfo == timezone.utc, f"{source} timestamp should be UTC"


class TestTimestampParsing:
    """Test that generated timestamps can be parsed by common libraries"""
    
    def test_timestamps_parseable_by_python_datetime(self):
        """Verify timestamps can be parsed by Python datetime"""
        from monitoring.timestamp_utils import utc_isoformat
        
        timestamp = utc_isoformat()
        
        # Should be parseable
        dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        assert dt.tzinfo == timezone.utc
    
    def test_timestamps_parseable_by_elasticsearch(self):
        """Verify timestamps match Elasticsearch date format expectations"""
        from monitoring.timestamp_utils import utc_isoformat
        
        timestamp = utc_isoformat()
        
        # Elasticsearch expects ISO8601 with 'Z' or timezone offset
        # Format: yyyy-MM-dd'T'HH:mm:ss.SSSZ
        assert 'T' in timestamp
        assert timestamp.endswith('Z')
        
        # Should not have +00:00
        assert '+00:00' not in timestamp
