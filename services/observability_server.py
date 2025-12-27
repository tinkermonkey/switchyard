"""
WebSocket server for streaming agent observability events to web UI
"""

# Monkey patch for eventlet MUST be first
import eventlet
eventlet.monkey_patch()

import asyncio
import json
import logging
import redis
from typing import List
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import threading
from elasticsearch import Elasticsearch
from services.logging_config import setup_service_logging
import subprocess
from pathlib import Path
from datetime import datetime
import time

# Setup logging with reduced verbosity
logger = setup_service_logging('observability_server')

app = Flask(__name__)
CORS(app)  # Enable CORS for web UI
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='eventlet',
    ping_timeout=10,
    ping_interval=5
)

# Initialize Elasticsearch client
es_client = Elasticsearch(['http://elasticsearch:9200'])

# Initialize Redis client for investigation queue
import os
redis_host = os.getenv('REDIS_HOST', 'redis')
redis_port = int(os.getenv('REDIS_PORT', 6379))
redis_client_raw = redis.Redis(host=redis_host, port=redis_port, decode_responses=False)

# Import Medic components (lazy initialization)
_medic_queue = None
_medic_report_manager = None

def check_container_running(container_name):
    """
    Check if a Docker container is actually running.

    Args:
        container_name: Name of the container to check

    Returns:
        True if container is running, False otherwise
    """
    if not container_name:
        return False

    try:
        result = subprocess.run(
            ['docker', 'ps', '-q', '-f', f'name=^{container_name}$'],
            capture_output=True,
            text=True,
            timeout=5
        )
        return bool(result.stdout.strip())
    except Exception as e:
        logger.error(f"Failed to check container {container_name}: {e}")
        return False

def get_investigation_queue():
    """Lazy initialization of Docker investigation queue"""
    global _medic_queue
    if _medic_queue is None:
        from services.medic.docker import DockerInvestigationQueue
        _medic_queue = DockerInvestigationQueue(redis_client_raw)
    return _medic_queue

def get_report_manager():
    """Lazy initialization of Docker report manager"""
    global _medic_report_manager
    if _medic_report_manager is None:
        from services.medic.docker import DockerReportManager
        _medic_report_manager = DockerReportManager()
    return _medic_report_manager

# Track connected clients
connected_clients = set()

# Track Redis subscriber health
subscriber_health = {
    'is_running': False,
    'last_message_time': None,
    'messages_processed': 0,
    'last_error': None,
    'started_at': None
}

# Git branch data cache
git_branch_cache = {}
git_branch_cache_lock = threading.Lock()

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    logger.info(f"Client connected: {request.sid if 'request' in dir() else 'unknown'}")
    connected_clients.add(request.sid if 'request' in dir() else 'unknown')
    emit('connection_established', {'status': 'connected'})

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    sid = request.sid if 'request' in dir() else 'unknown'
    logger.info(f"Client disconnected: {sid}")
    if sid in connected_clients:
        connected_clients.remove(sid)

@app.route('/agents/active')
def get_active_agents():
    """Get list of currently running agent containers"""
    try:
        # Get active agent containers from Redis
        redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)

        # Find all keys matching agent container pattern
        agent_keys = redis_client.keys('agent:container:*')

        active_agents = []
        for key in agent_keys:
            container_info = redis_client.hgetall(key)
            if container_info:
                active_agents.append({
                    'container_name': container_info.get('container_name'),
                    'container_id': container_info.get('container_id'),
                    'agent': container_info.get('agent'),
                    'project': container_info.get('project'),
                    'task_id': container_info.get('task_id'),
                    'started_at': container_info.get('started_at'),
                    'issue_number': container_info.get('issue_number')
                })

        return jsonify({'active_agents': active_agents}), 200

    except Exception as e:
        logger.error(f"Failed to get active agents: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/debug/test-pubsub', methods=['POST'])
def test_pubsub():
    """Test endpoint to publish a test message to Redis pub/sub"""
    try:
        redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
        
        test_event = {
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'event_id': 'test-' + str(time.time()),
            'event_type': 'agent_completed',
            'agent': 'test_agent',
            'task_id': 'test_task',
            'project': 'test_project',
            'data': {'test': True, 'duration_ms': 1000}
        }
        
        # Publish to agent events channel
        result = redis_client.publish('orchestrator:agent_events', json.dumps(test_event))
        
        logger.info(f"Published test event to Redis pub/sub, {result} subscribers received it")
        
        return jsonify({
            'success': True,
            'message': f'Test event published to {result} subscribers',
            'event': test_event,
            'subscriber_health': subscriber_health
        }), 200
        
    except Exception as e:
        logger.error(f"Failed to publish test event: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/agents/kill/<container_name>', methods=['POST'])
def kill_agent(container_name):
    """Emergency kill switch - immediately stop an agent container"""
    try:
        logger.warning(f"KILL SWITCH ACTIVATED for container: {container_name}")

        # Stop the container immediately
        result = subprocess.run(
            ['docker', 'rm', '-f', container_name],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            logger.info(f"Successfully killed container: {container_name}")

            # Remove from Redis tracking
            redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
            redis_client.delete(f'agent:container:{container_name}')

            return jsonify({
                'success': True,
                'message': f'Container {container_name} stopped',
                'container_name': container_name
            }), 200
        else:
            logger.error(f"Failed to kill container {container_name}: {result.stderr}")
            return jsonify({
                'success': False,
                'error': result.stderr,
                'container_name': container_name
            }), 500

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout killing container: {container_name}")
        return jsonify({
            'success': False,
            'error': 'Timeout - container may be unresponsive',
            'container_name': container_name
        }), 500
    except Exception as e:
        logger.error(f"Error killing container {container_name}: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'container_name': container_name
        }), 500

def get_claude_token_usage():
    """
    Query Elasticsearch for Claude Code token usage metrics
    Returns token usage for the last 4 hours and 7 days
    """
    try:
        from datetime import datetime, timedelta

        # Calculate time ranges
        now = datetime.utcnow()
        four_hours_ago = now - timedelta(hours=4)
        seven_days_ago = now - timedelta(days=7)

        # Query for last 4 hours
        query_4h = {
            "query": {
                "bool": {
                    "must": [
                        {"exists": {"field": "context_tokens"}},
                        {"range": {"timestamp": {"gte": four_hours_ago.isoformat()}}}
                    ]
                }
            },
            "aggs": {
                "total_tokens": {
                    "sum": {"field": "context_tokens"}
                }
            },
            "size": 0
        }

        result_4h = es_client.search(index="agent-events-*", body=query_4h)
        tokens_4h = int(result_4h.get('aggregations', {}).get('total_tokens', {}).get('value', 0))

        # Query for last 7 days
        query_7d = {
            "query": {
                "bool": {
                    "must": [
                        {"exists": {"field": "context_tokens"}},
                        {"range": {"timestamp": {"gte": seven_days_ago.isoformat()}}}
                    ]
                }
            },
            "aggs": {
                "total_tokens": {
                    "sum": {"field": "context_tokens"}
                }
            },
            "size": 0
        }

        result_7d = es_client.search(index="agent-events-*", body=query_7d)
        tokens_7d = int(result_7d.get('aggregations', {}).get('total_tokens', {}).get('value', 0))

        return {
            'tokens_4h': tokens_4h,
            'tokens_7d': tokens_7d,
            'collected_at': now.isoformat() + 'Z'
        }

    except Exception as e:
        logger.warning(f"Failed to fetch Claude token usage metrics: {e}")
        return {
            'tokens_4h': 0,
            'tokens_7d': 0,
            'error': str(e),
            'collected_at': datetime.utcnow().isoformat() + 'Z'
        }

@app.errorhandler(500)
def handle_500(e):
    """Handle 500 errors gracefully"""
    logger.error(f"500 Error: {e}", exc_info=True)
    return jsonify({
        'status': 'error',
        'message': 'Internal server error'
    }), 500

@app.errorhandler(Exception)
def handle_exception(e):
    """Handle all uncaught exceptions"""
    logger.error(f"Unhandled exception: {e}", exc_info=True)
    return jsonify({
        'status': 'error',
        'message': str(e)
    }), 500

@app.route('/health')
def health():
    """Health check endpoint - returns orchestrator health status"""
    import json

    # Get last health check result from Redis (cross-process shared state)
    redis_client = None
    try:
        redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
        health_json = redis_client.get('orchestrator:health')

        if health_json is None:
            # No health check has run yet
            response_data = {
                'status': 'starting',
                'message': 'Orchestrator is starting, no health check completed yet',
                'connected_clients': len(connected_clients),
                'subscriber_health': subscriber_health
            }
            return jsonify(response_data), 503

        try:
            health_data = json.loads(health_json)
        except json.JSONDecodeError:
            # Invalid JSON in Redis
            response_data = {
                'status': 'error',
                'message': 'Invalid health data in cache',
                'connected_clients': len(connected_clients)
            }
            return jsonify(response_data), 503
        
        # IMPORTANT: Always fetch fresh rate limit data instead of using cached values
        # The rate limit changes frequently (every API call), so we should not rely on
        # cached values that may be stale. The background rate limit checker updates
        # the client object every 5 minutes, so always get the latest status.
        try:
            from services.github_api_client import get_github_client
            github_client = get_github_client()
            client_status = github_client.get_status()
            
            fresh_rate_limit = {
                'remaining': client_status['rate_limit']['remaining'],
                'limit': client_status['rate_limit']['limit'],
                'percentage_used': client_status['rate_limit']['percentage_used'],
                'reset_time': client_status['rate_limit']['reset_time'],
            }
            
            fresh_circuit_breaker = {
                'state': client_status['breaker']['state'],
                'is_open': client_status['breaker']['is_open'],
                'opened_at': client_status['breaker']['opened_at'],
                'reset_time': client_status['breaker']['reset_time'],
            }

            # Update the cached health data with fresh rate limit and circuit breaker info
            if 'checks' in health_data and 'github' in health_data['checks']:
                health_data['checks']['github']['api_rate_limit'] = fresh_rate_limit
                health_data['checks']['github']['circuit_breaker'] = fresh_circuit_breaker
                # Add dedicated api_usage section for UI consumption
                health_data['checks']['github']['api_usage'] = {
                    'remaining': fresh_rate_limit['remaining'],
                    'limit': fresh_rate_limit['limit'],
                    'percentage_used': fresh_rate_limit['percentage_used'],
                    'reset_time': fresh_rate_limit['reset_time']
                }
        except Exception as e:
            logger.debug(f"Failed to fetch fresh rate limit data: {e}")
            # Continue with cached data if fresh fetch fails

        # IMPORTANT: Also fetch fresh Claude Code circuit breaker status and token usage
        try:
            from monitoring.claude_code_breaker import get_breaker
            claude_breaker = get_breaker()
            if claude_breaker:
                claude_status = claude_breaker.get_status()
                fresh_claude_breaker = {
                    'state': claude_status['state'],
                    'is_open': claude_status['is_open'],
                    'opened_at': claude_status['opened_at'],
                    'reset_time': claude_status['reset_time'],
                    'time_until_reset': claude_status['time_until_reset'],
                }

                # Get token usage metrics
                token_usage = get_claude_token_usage()

                # Add Claude Code breaker and token usage to checks
                if 'checks' not in health_data:
                    health_data['checks'] = {}
                health_data['checks']['claude'] = {
                    'healthy': not claude_status['is_open'],
                    'circuit_breaker': fresh_claude_breaker,
                    'token_usage': token_usage
                }
        except Exception as e:
            logger.debug(f"Failed to fetch Claude Code breaker status: {e}")
            # Continue without Claude Code breaker data if fetch fails
        
        # Add subscriber health to response
        health_data['subscriber_health'] = subscriber_health

        # Return the full health check data
        # Determine status: healthy, degraded, or unhealthy
        if health_data.get('healthy'):
            status = 'degraded' if health_data.get('degraded') else 'healthy'
            status_code = 200
        else:
            status = 'unhealthy'
            status_code = 503

        response_data = {
            'status': status,
            'connected_clients': len(connected_clients),
            'orchestrator': health_data
        }

        return jsonify(response_data), status_code

    except redis.ConnectionError:
        response_data = {
            'status': 'error',
            'message': 'Cannot connect to Redis',
            'connected_clients': len(connected_clients)
        }
        return jsonify(response_data), 503
    except Exception as e:
        logger.error(f"Error in /health endpoint: {e}", exc_info=True)
        response_data = {
            'status': 'error',
            'message': f'Failed to retrieve health status: {str(e)}',
            'connected_clients': len(connected_clients)
        }
        return jsonify(response_data), 503

@app.route('/history')
def get_history():
    """Get recent event history from Redis Stream or Elasticsearch"""
    try:
        # Connect to Redis
        redis_client = redis.Redis(
            host='redis',
            port=6379,
            decode_responses=True
        )

        # Get last N events from the stream (newest first)
        count = int(request.args.get('count', 100))
        count = min(count, 500)  # Cap at 500 events

        stream_key = "orchestrator:event_stream"

        # Get total count in stream
        total_count = redis_client.xlen(stream_key)

        # Try Redis first for real-time events
        events = redis_client.xrevrange(stream_key, '+', '-', count=count)

        # Parse events from Redis
        history = []
        for event_id, event_data in events:
            try:
                event_json = event_data.get('event')
                if event_json:
                    history.append(json.loads(event_json))
            except Exception as e:
                logger.error(f"Error parsing event: {e}")

        # If Redis stream is empty, fall back to Elasticsearch
        if len(history) == 0:
            try:
                # Query agent-events-* index for lifecycle events
                query = {
                    "size": count,
                    "sort": [{"timestamp": {"order": "desc"}}],
                    "query": {"match_all": {}}
                }

                result = es_client.search(index="agent-events-*", body=query)
                hits = result['hits']['hits']

                # Convert ES logs to event format (reconstruct from raw_event)
                for hit in hits:
                    source = hit['_source']
                    # Use raw_event which has the original lifecycle event structure
                    if 'raw_event' in source and source['raw_event']:
                        history.append(source['raw_event'])

                # Reverse to get chronological order (oldest first)
                history.reverse()
                total_count = result['hits']['total']['value']

            except Exception as es_error:
                logger.error(f"Error fetching from Elasticsearch: {es_error}")
        else:
            # Reverse so oldest is first (chronological order)
            history.reverse()

        return jsonify({
            'success': True,
            'count': len(history),
            'total': total_count,
            'events': history
        })

    except Exception as e:
        logger.error(f"Error fetching history: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'events': []
        }), 500

@app.route('/claude-logs-history')
def get_claude_logs_history():
    """Get recent Claude log history from Redis Stream or Elasticsearch"""
    try:
        # Connect to Redis
        redis_client = redis.Redis(
            host='redis',
            port=6379,
            decode_responses=True
        )

        # Get filter parameters
        count = int(request.args.get('count', 100))
        count = min(count, 500)  # Cap at 500 logs
        agent_filter = request.args.get('agent')
        start_time = request.args.get('start_time')
        end_time = request.args.get('end_time')

        stream_key = "orchestrator:claude_logs_stream"

        # Get total count in stream
        total_count = redis_client.xlen(stream_key)

        # Try Redis first for real-time logs
        logs = redis_client.xrevrange(stream_key, '+', '-', count=count)

        # Parse logs from Redis
        history = []
        for log_id, log_data in logs:
            try:
                log_json = log_data.get('log')
                if log_json:
                    log_entry = json.loads(log_json)

                    # Apply filters if provided
                    if agent_filter and log_entry.get('agent') != agent_filter:
                        continue

                    if start_time and log_entry.get('timestamp'):
                        if log_entry['timestamp'] < float(start_time):
                            continue

                    if end_time and log_entry.get('timestamp'):
                        if log_entry['timestamp'] > float(end_time):
                            continue

                    history.append(log_entry)
            except Exception as e:
                logger.error(f"Error parsing log: {e}")

        # If Redis stream is empty, fall back to Elasticsearch
        if len(history) == 0:
            try:
                # Query claude-streams-* index for streaming logs
                query = {
                    "size": count,
                    "sort": [{"timestamp": {"order": "desc"}}],
                    "query": {"match_all": {}}
                }

                # Add agent filter if provided
                if agent_filter:
                    query["query"] = {
                        "term": {"agent_name.keyword": agent_filter}
                    }

                result = es_client.search(index="claude-streams-*", body=query)
                hits = result['hits']['hits']

                # Extract raw_event from each hit (this is the original Claude streaming log)
                for hit in hits:
                    source = hit['_source']
                    if 'raw_event' in source and source['raw_event']:
                        raw = source['raw_event']
                        # Verify it has the event structure (Claude streaming log)
                        if 'event' in raw and isinstance(raw['event'], dict):
                            history.append(raw)

                # Reverse to get chronological order (oldest first)
                history.reverse()
                total_count = result['hits']['total']['value']

            except Exception as es_error:
                logger.error(f"Error fetching from Elasticsearch: {es_error}")
        else:
            # Reverse so oldest is first (chronological order)
            history.reverse()

        return jsonify({
            'success': True,
            'count': len(history),
            'total': total_count,
            'logs': history
        })

    except Exception as e:
        logger.error(f"Error fetching Claude log history: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'logs': []
        }), 500

@app.route('/current-pipeline')
def get_current_pipeline():
    """Get information about the currently running or most recent pipeline"""
    try:
        import yaml
        from pathlib import Path

        # Load pipeline definitions
        pipeline_config_path = Path('/app/config/foundations/pipelines.yaml')
        with open(pipeline_config_path, 'r') as f:
            pipeline_config = yaml.safe_load(f)

        pipeline_templates = pipeline_config.get('pipeline_templates', {})

        # Connect to Redis
        redis_client = redis.Redis(
            host='redis',
            port=6379,
            decode_responses=True
        )

        # Get recent events to determine current pipeline
        stream_key = "orchestrator:event_stream"
        events = redis_client.xrevrange(stream_key, '+', '-', count=100)

        # Parse events to find pipeline information
        pipeline_info = {
            'active': False,
            'pipeline_name': None,
            'pipeline_type': None,
            'stages': [],
            'current_stage': None,
            'progress': 0,
            'project': None,
            'issue_number': None,
            'board': None
        }

        # Track agents by issue number (pipeline = all agents for an issue)
        recent_agents = []
        active_issue = None
        active_project = None
        active_board = None
        currently_running = set()  # Track which agents are currently running

        for event_id, event_data in events:
            try:
                event_json = event_data.get('event')
                if not event_json:
                    continue

                event = json.loads(event_json)
                event_type = event.get('event_type')
                agent = event.get('agent')

                # Extract issue number from task_id or event data
                task_data = event.get('data', {})
                issue_num = task_data.get('issue_number')

                # Also try discussion_id if no issue_number
                discussion_id = task_data.get('discussion_id')

                # Set active issue from first task_received event
                if event_type == 'task_received' and not active_issue:
                    active_issue = issue_num or discussion_id
                    active_project = event.get('project')
                    active_board = task_data.get('board')
                    pipeline_info['issue_number'] = active_issue
                    pipeline_info['project'] = active_project
                    pipeline_info['board'] = active_board
                    pipeline_info['active'] = True

                # Track all agents for the active issue/discussion
                if active_issue and (issue_num == active_issue or discussion_id == active_issue):
                    # Track agent in sequence
                    if agent and agent not in recent_agents:
                        recent_agents.insert(0, agent)  # Insert at beginning for chronological order

                    # Track currently running agents
                    if event_type == 'agent_initialized':
                        currently_running.add(agent)
                    elif event_type in ['agent_completed', 'agent_failed']:
                        currently_running.discard(agent)

            except Exception as e:
                logger.error(f"Error parsing event for pipeline info: {e}")

        # Reverse to get chronological order (oldest first)
        recent_agents.reverse()

        # Set current stage as the most recent running agent
        current_agent = None
        if currently_running:
            current_agent = list(currently_running)[-1]

        # Determine pipeline type based on board or agents
        pipeline_type = None
        if active_board == 'idea-development':
            pipeline_type = 'idea_development'
        elif active_board == 'dev':
            pipeline_type = 'dev_pipeline'
        else:
            # Infer from agents if board not found
            if any(agent in ['idea_researcher', 'business_analyst'] for agent in recent_agents):
                if 'senior_software_engineer' in recent_agents:
                    pipeline_type = 'dev_pipeline'
                elif 'software_architect' in recent_agents:
                    pipeline_type = 'full_sdlc'
                else:
                    pipeline_type = 'idea_development'

        # Load pipeline definition
        if pipeline_type and pipeline_type in pipeline_templates:
            template = pipeline_templates[pipeline_type]
            pipeline_info['pipeline_type'] = pipeline_type
            pipeline_info['pipeline_name'] = template.get('name', pipeline_type)

            # Build stages from template
            stages = []
            current_stage_index = -1

            for idx, stage_def in enumerate(template.get('stages', [])):
                stage_name = stage_def.get('name', stage_def.get('stage', 'Unknown'))
                agent_name = stage_def.get('default_agent', '')
                reviewer_agent = stage_def.get('reviewer_agent')

                stage_info = {
                    'name': stage_name,
                    'agent': agent_name,
                    'status': 'pending'
                }

                # Add reviewer information if present
                if reviewer_agent:
                    stage_info['reviewer_agent'] = reviewer_agent
                    stage_info['review_required'] = True

                # Determine stage status based on executed agents
                if agent_name in recent_agents:
                    if agent_name == current_agent:
                        stage_info['status'] = 'running'
                        current_stage_index = idx
                        pipeline_info['current_stage'] = stage_name
                    else:
                        stage_info['status'] = 'completed'

                stages.append(stage_info)

            pipeline_info['stages'] = stages

            # Calculate progress
            if stages:
                completed = sum(1 for s in stages if s['status'] == 'completed')
                running = sum(1 for s in stages if s['status'] == 'running')
                total = len(stages)

                if running > 0:
                    # Include partial credit for running stage
                    pipeline_info['progress'] = int(((completed + 0.5) / total) * 100)
                else:
                    pipeline_info['progress'] = int((completed / total) * 100)
        else:
            # Fallback: just show executed agents
            pipeline_info['stages'] = [{'name': a, 'agent': a, 'status': 'completed'} for a in recent_agents]
            pipeline_info['pipeline_name'] = 'Custom Pipeline'
            pipeline_info['progress'] = 100

        return jsonify({
            'success': True,
            'pipeline': pipeline_info
        })

    except Exception as e:
        logger.error(f"Error fetching current pipeline: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'pipeline': None
        }), 500

@app.route('/pipeline-run-events')
def get_pipeline_run_events():
    """
    Get all events for a specific pipeline run in chronological order
    Combines decision events, agent lifecycle events, and execution logs
    """
    try:
        pipeline_run_id = request.args.get('pipeline_run_id')
        
        if not pipeline_run_id:
            return jsonify({
                'success': False,
                'error': 'pipeline_run_id parameter is required'
            }), 400
        
        # Get pipeline run details first
        from services.pipeline_run import get_pipeline_run_manager
        pipeline_run_manager = get_pipeline_run_manager()
        pipeline_run = pipeline_run_manager.get_pipeline_run_by_id(pipeline_run_id)
        
        if not pipeline_run:
            return jsonify({
                'success': False,
                'error': f'Pipeline run {pipeline_run_id} not found'
            }), 404
        
        # Query all event types from Elasticsearch
        all_events = []
        
        # 1. Get decision events
        try:
            decision_events_query = {
                "query": {
                    "term": {
                        "pipeline_run_id": pipeline_run_id
                    }
                },
                "sort": [{"timestamp": "asc"}],
                "size": 1000
            }

            decision_result = es_client.search(
                index="decision-events-*",
                body=decision_events_query
            )

            for hit in decision_result['hits']['hits']:
                event_data = hit['_source']
                event_data['event_category'] = 'decision'
                event_data['event_index'] = hit['_index']
                all_events.append(event_data)
        except Exception as e:
            logger.warning(f"Error fetching decision events: {e}")
        
        # 2. Get agent lifecycle events (agent_initialized, agent_completed, etc.)
        try:
            agent_events_query = {
                "query": {
                    "term": {
                        "pipeline_run_id": pipeline_run_id  # Already keyword type, no .keyword needed
                    }
                },
                "sort": [{"timestamp": "asc"}],
                "size": 1000
            }
            
            agent_result = es_client.search(
                index="agent-events-*",
                body=agent_events_query
            )

            for hit in agent_result['hits']['hits']:
                event_data = hit['_source']
                event_data['event_category'] = 'agent_lifecycle'
                event_data['event_index'] = hit['_index']
                all_events.append(event_data)
        except Exception as e:
            logger.warning(f"Error fetching agent events: {e}")
        
        # 3. Get Claude stream logs for detailed execution
        try:
            claude_logs_query = {
                "query": {
                    "term": {
                        "pipeline_run_id": pipeline_run_id  # Already keyword type, no .keyword needed
                    }
                },
                "sort": [{"timestamp": "asc"}],
                "size": 10000  # May be many logs
            }
            
            claude_result = es_client.search(
                index="claude-streams-*",
                body=claude_logs_query
            )
            
            for hit in claude_result['hits']['hits']:
                event_data = hit['_source']
                event_data['event_category'] = 'claude_log'
                event_data['event_index'] = hit['_index']
                all_events.append(event_data)
        except Exception as e:
            logger.warning(f"Error fetching Claude logs: {e}")
        
        # Sort all events by timestamp
        all_events.sort(key=lambda x: x.get('timestamp', ''))
        
        return jsonify({
            'success': True,
            'pipeline_run': pipeline_run.to_dict(),
            'events': all_events,
            'event_count': len(all_events)
        })
        
    except Exception as e:
        logger.error(f"Error fetching pipeline run events: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/pipeline-runs/<pipeline_run_id>/kill', methods=['POST'])
def kill_pipeline_run(pipeline_run_id):
    """
    Kill/Cancel an active pipeline run.
    
    This endpoint:
    1. Ends the pipeline run in Redis/Elasticsearch
    2. Marks any in-progress execution state as failed
    3. Allows the system to start a fresh run for the issue
    """
    try:
        from services.pipeline_run import get_pipeline_run_manager
        from services.work_execution_state import work_execution_tracker
        
        logger.info(f"Received request to kill pipeline run: {pipeline_run_id}")
        
        pipeline_run_manager = get_pipeline_run_manager()
        
        # Get the run details first
        pipeline_run = pipeline_run_manager.get_pipeline_run_by_id(pipeline_run_id)
        
        if not pipeline_run:
            return jsonify({
                'success': False,
                'error': f'Pipeline run {pipeline_run_id} not found'
            }), 404
            
        project = pipeline_run.project
        issue_number = pipeline_run.issue_number
        
        # 1. End the pipeline run
        success = pipeline_run_manager.end_pipeline_run(
            project=project,
            issue_number=issue_number,
            reason="Killed by user via Web UI"
        )
        
        if not success:
            # It might have been already ended, but we should still clean up execution state
            logger.warning(f"Pipeline run {pipeline_run_id} was not active in Redis, forcing update in Elasticsearch")
            
            # Force update in Elasticsearch using the run details we fetched earlier
            # This handles "zombie" runs that exist in ES but not in Redis
            try:
                pipeline_run_manager._end_run_in_elasticsearch(
                    pipeline_run.to_dict(),
                    "Killed by user via Web UI (forced update)"
                )
                success = True # Mark as success since we updated ES
            except Exception as e:
                logger.error(f"Failed to force update pipeline run in ES: {e}")
            
        # 2. Force fail any in-progress execution state to release locks
        # We need to find if there's an in-progress state
        execution_history = work_execution_tracker.get_execution_history(project, issue_number)
        
        cleaned_up_execution = False
        for execution in reversed(execution_history):
            if execution.get('outcome') == 'in_progress':
                agent = execution.get('agent')
                column = execution.get('column')
                
                logger.info(f"Force-failing in-progress execution for {project}/#{issue_number} ({agent})")
                
                work_execution_tracker.record_execution_outcome(
                    issue_number=issue_number,
                    column=column,
                    agent=agent,
                    outcome='failure',
                    project_name=project,
                    error='Pipeline run killed by user'
                )
                cleaned_up_execution = True
                
        # 3. Attempt to kill any running containers associated with this run
        # This is a best-effort cleanup
        try:
            import subprocess
            # Find containers for this project/issue
            # We look for agent containers and repair cycle containers
            
            # Agent containers: claude-agent-{project}-{task_id}
            # We can't easily match task_id to run_id without more queries, 
            # but we can check active agents endpoint logic
            
            # For now, let's just rely on the state cleanup. 
            # The zombie reaper or next health check might clean up orphaned containers,
            # or the user can use the specific "Kill Agent" button if they see a stuck container.
            pass
        except Exception as e:
            logger.warning(f"Error cleaning up containers for killed run: {e}")

        return jsonify({
            'success': True,
            'message': f'Pipeline run {pipeline_run_id} killed',
            'cleaned_execution_state': cleaned_up_execution
        })
        
    except Exception as e:
        logger.error(f"Error killing pipeline run: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/active-pipeline-runs')
def get_active_pipeline_runs():
    """Get all currently active pipeline runs"""
    try:
        # Query Elasticsearch for active pipeline runs
        query = {
            "query": {
                "term": {
                    "status": "active"
                }
            },
            "sort": [{"started_at": "desc"}],
            "size": 100
        }
        
        result = es_client.search(
            index="pipeline-runs-*",
            body=query
        )
        
        runs = []
        for hit in result['hits']['hits']:
            runs.append(hit['_source'])
        
        return jsonify({
            'success': True,
            'runs': runs,
            'count': len(runs)
        })
        
    except Exception as e:
        # Handle index not found gracefully (returns empty list at debug level)
        if 'index_not_found_exception' in str(e) or 'no such index' in str(e):
            logger.debug(f"Pipeline runs index not found (expected on first run): {e}")
            return jsonify({
                'success': True,
                'runs': [],
                'count': 0
            })
        # Other errors log as error
        logger.error(f"Error fetching active pipeline runs: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'runs': []
        }), 500

@app.route('/api/active-agents')
def get_active_agents_from_pipelines():
    """
    Get all currently active agents across all pipeline runs
    Returns agents that have been initialized but not completed/failed
    This is the source of truth for active agent tracking in the UI
    """
    try:
        # Get all active pipeline runs AND recently completed ones (within last 2 hours)
        # This catches edge cases where agents start after pipeline is marked complete
        from datetime import datetime, timedelta
        two_hours_ago = (datetime.utcnow() - timedelta(hours=2)).isoformat()

        pipeline_runs_query = {
            "query": {
                "bool": {
                    "should": [
                        {"term": {"status": "active"}},
                        {
                            "bool": {
                                "must": [
                                    {"term": {"status": "completed"}},
                                    {"range": {"ended_at": {"gte": two_hours_ago}}}
                                ]
                            }
                        }
                    ],
                    "minimum_should_match": 1
                }
            },
            "size": 100
        }

        try:
            pipeline_runs = es_client.search(
                index="pipeline-runs",
                body=pipeline_runs_query
            )
        except Exception as e:
            # Index doesn't exist yet or other error - no pipeline runs available
            logger.debug(f"No pipeline runs found (index may not exist yet): {e}")
            pipeline_runs = {'hits': {'hits': []}}  # Empty result, will fall through to Redis check

        active_agents = []

        for pipeline_hit in pipeline_runs['hits']['hits']:
            pipeline_run = pipeline_hit['_source']
            pipeline_run_id = pipeline_run['id']

            # Get agent lifecycle events for this pipeline
            agent_events_query = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"pipeline_run_id.keyword": pipeline_run_id}},
                            {"terms": {"event_type": ["agent_initialized", "agent_completed", "agent_failed"]}}
                        ]
                    }
                },
                "sort": [{"timestamp": "asc"}],
                "size": 1000
            }

            try:
                events_result = es_client.search(
                    index="agent-events-*",
                    body=agent_events_query
                )
            except Exception as e:
                logger.warning(f"Error fetching agent events for pipeline {pipeline_run_id}: {e}")
                continue

            # Track agent states
            agent_states = {}
            for hit in events_result['hits']['hits']:
                event = hit['_source']
                agent_key = f"{event['agent']}_{event.get('task_id', '')}"

                if event['event_type'] == 'agent_initialized':
                    agent_states[agent_key] = {
                        'agent': event['agent'],
                        'status': 'running',
                        'container_name': event.get('container_name'),
                        'branch_name': event.get('branch_name'),
                        'started_at': event['timestamp'],
                        'project': pipeline_run.get('project', 'unknown'),
                        'issue_number': pipeline_run.get('issue_number', 'unknown'),
                        'issue_title': pipeline_run.get('issue_title', ''),
                        'board': pipeline_run.get('board', 'unknown'),
                        'pipeline_run_id': pipeline_run_id,
                        'is_containerized': bool(event.get('container_name')),
                        'task_id': event.get('task_id', ''),
                    }
                elif event['event_type'] in ['agent_completed', 'agent_failed']:
                    if agent_key in agent_states:
                        agent_states[agent_key]['status'] = 'completed' if event['event_type'] == 'agent_completed' else 'failed'

            # Add running agents to result
            for agent_data in agent_states.values():
                if agent_data['status'] == 'running':
                    active_agents.append(agent_data)

        # Also check Redis for standalone agents (not associated with pipeline runs)
        # This catches repair cycles and other ad-hoc tasks
        try:
            redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
            agent_keys = redis_client.keys('agent:container:*')

            for key in agent_keys:
                container_info = redis_client.hgetall(key)
                if container_info:
                    # Check if this agent is already in our list (by container_name)
                    container_name = container_info.get('container_name')
                    if container_name and not any(a.get('container_name') == container_name for a in active_agents):
                        # Add Redis agent to our list
                        pipeline_run_id = container_info.get('pipeline_run_id', '')
                        active_agents.append({
                            'agent': container_info.get('agent'),
                            'project': container_info.get('project', 'unknown'),
                            'issue_number': container_info.get('issue_number', 'unknown'),
                            'issue_title': '',  # Not available in Redis
                            'branch_name': None,  # Not available in Redis
                            'container_name': container_name,
                            'started_at': container_info.get('started_at'),
                            'is_containerized': True,
                            'pipeline_run_id': pipeline_run_id if pipeline_run_id else None,
                            'board': 'unknown',
                            'task_id': container_info.get('task_id', ''),
                            'source': 'redis'  # Mark as from Redis for debugging
                        })
        except Exception as e:
            logger.warning(f"Error fetching Redis agents: {e}")

        logger.debug(f"Active agents endpoint returning {len(active_agents)} agents ({len([a for a in active_agents if a.get('source') != 'redis'])} from pipelines, {len([a for a in active_agents if a.get('source') == 'redis'])} from Redis)")

        return jsonify({
            'success': True,
            'agents': active_agents,
            'count': len(active_agents)
        })

    except Exception as e:
        logger.error(f"Error fetching active agents: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'agents': []
        }), 500

@app.route('/completed-pipeline-runs')
def get_completed_pipeline_runs():
    """Get completed pipeline runs with pagination"""
    try:
        # Get pagination parameters
        limit = int(request.args.get('limit', 10))
        offset = int(request.args.get('offset', 0))
        
        # Cap limit to prevent excessive queries
        limit = min(limit, 100)
        
        # Query Elasticsearch for completed pipeline runs
        query = {
            "query": {
                "term": {
                    "status": "completed"
                }
            },
            "sort": [{"ended_at": "desc"}],
            "from": offset,
            "size": limit
        }
        
        result = es_client.search(
            index="pipeline-runs-*",
            body=query
        )
        
        runs = []
        for hit in result['hits']['hits']:
            run_data = hit['_source']
            
            # Calculate duration if both timestamps exist
            if run_data.get('started_at') and run_data.get('ended_at'):
                try:
                    from datetime import datetime
                    start = datetime.fromisoformat(run_data['started_at'].replace('Z', '+00:00'))
                    end = datetime.fromisoformat(run_data['ended_at'].replace('Z', '+00:00'))
                    duration = (end - start).total_seconds()
                    run_data['duration'] = duration
                except Exception as e:
                    logger.warning(f"Error calculating duration: {e}")
            
            runs.append(run_data)
        
        return jsonify({
            'success': True,
            'runs': runs,
            'count': len(runs),
            'offset': offset,
            'limit': limit
        })
        
    except Exception as e:
        logger.error(f"Error fetching completed pipeline runs: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'runs': []
        }), 500

@app.route('/api/agent-execution/<execution_id>')
def get_agent_execution(execution_id):
    """Get details for a specific agent execution"""
    try:
        # Query Elasticsearch for agent execution in agent-events index
        # Look for agent_initialized event with this execution_id
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"event_type": "agent_initialized"}},
                        {"term": {"agent_execution_id.keyword": execution_id}}
                    ]
                }
            },
            "size": 1
        }
        
        result = es_client.search(
            index="agent-events-*",
            body=query
        )
        
        if result['hits']['total']['value'] == 0:
            return jsonify({
                'success': False,
                'error': 'Execution not found'
            }), 404
        
        # Get the agent_initialized event
        init_event = result['hits']['hits'][0]['_source']
        
        # Look for corresponding completion/failure event
        end_query = {
            "query": {
                "bool": {
                    "must": [
                        {"terms": {"event_type": ["agent_completed", "agent_failed"]}},
                        {"term": {"agent": init_event['agent']}},
                        {"term": {"task_id": init_event['task_id']}}
                    ],
                    "filter": [
                        {"range": {"timestamp": {"gte": init_event['timestamp']}}}
                    ]
                }
            },
            "sort": [{"timestamp": "asc"}],
            "size": 1
        }
        
        end_result = es_client.search(
            index="agent-events-*",
            body=end_query
        )
        
        # Build execution object
        execution = {
            'id': execution_id,
            'agent': init_event['agent'],
            'task_id': init_event['task_id'],
            'project': init_event['project'],
            'pipeline_run_id': init_event.get('pipeline_run_id'),
            'branch_name': init_event.get('branch_name'),
            'started_at': init_event['timestamp'],
            'ended_at': None,
            'status': 'running',
            'duration': None
        }
        
        # Add end information if found
        if end_result['hits']['total']['value'] > 0:
            end_event = end_result['hits']['hits'][0]['_source']
            execution['ended_at'] = end_event['timestamp']
            execution['status'] = 'completed' if end_event['event_type'] == 'agent_completed' else 'failed'
            
            # Calculate duration
            try:
                from datetime import datetime
                start = datetime.fromisoformat(execution['started_at'].replace('Z', '+00:00'))
                end = datetime.fromisoformat(execution['ended_at'].replace('Z', '+00:00'))
                execution['duration'] = (end - start).total_seconds()
            except Exception as e:
                logger.warning(f"Error calculating execution duration: {e}")
        
        # Fetch Claude logs for this execution
        logs = []
        try:
            # Query by task_id only - it's unique to this execution
            logs_query = {
                "query": {
                    "term": {"task_id": execution['task_id']}
                },
                "sort": [{"timestamp": "asc"}],
                "size": 10000
            }
            
            logs_result = es_client.search(
                index="claude-streams-*",
                body=logs_query
            )
            
            for hit in logs_result['hits']['hits']:
                log_data = hit['_source']
                logs.append(log_data)
        except Exception as e:
            logger.warning(f"Error fetching logs for execution: {e}")
        
        # Fetch prompt_constructed event for this execution
        prompt_event = None
        try:
            prompt_query = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"event_type": "prompt_constructed"}},
                            {"term": {"task_id": execution['task_id']}}
                        ]
                    }
                },
                "sort": [{"timestamp": "asc"}],
                "size": 1
            }

            prompt_result = es_client.search(
                index="claude-streams-*",
                body=prompt_query
            )

            if prompt_result['hits']['total']['value'] > 0:
                prompt_event = prompt_result['hits']['hits'][0]['_source']
        except Exception as e:
            logger.warning(f"Error fetching prompt event for execution: {e}")
        
        return jsonify({
            'success': True,
            'execution': execution,
            'logs': logs,
            'prompt_event': prompt_event
        })
        
    except Exception as e:
        logger.error(f"Error fetching agent execution: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# =======================================================================================
# REVIEW FILTER API ENDPOINTS
# =======================================================================================

@app.route('/api/review-filters', methods=['GET'])
def get_review_filters():
    """Get all review filters with optional filtering"""
    try:
        from services.review_filter_manager import get_review_filter_manager

        filter_manager = get_review_filter_manager()

        # Check if index exists, create if needed
        if not filter_manager._ensure_index_exists():
            return jsonify({
                'success': True,
                'filters': [],
                'count': 0,
                'message': 'Review filters index not initialized'
            })

        # Get query parameters
        agent = request.args.get('agent')
        active_only = request.args.get('active_only', 'true').lower() == 'true'
        min_confidence = float(request.args.get('min_confidence', 0.0))

        # Query Elasticsearch
        must_clauses = []

        if agent:
            must_clauses.append({"term": {"agent.keyword": agent}})

        if active_only:
            must_clauses.append({"term": {"active": True}})

        if min_confidence > 0:
            must_clauses.append({"range": {"confidence": {"gte": min_confidence}}})

        query = {
            "query": {
                "bool": {
                    "must": must_clauses if must_clauses else [{"match_all": {}}]
                }
            },
            "sort": [
                {"created_at": "desc"}
            ],
            "size": 100
        }

        result = filter_manager.es.search(
            index=filter_manager.filters_index,
            body=query
        )

        filters = [hit['_source'] for hit in result['hits']['hits']]

        return jsonify({
            'success': True,
            'filters': filters,
            'count': len(filters)
        })

    except Exception as e:
        logger.error(f"Error fetching review filters: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'filters': []
        }), 500

@app.route('/api/review-filters', methods=['POST'])
def create_review_filter():
    """Create a new review filter"""
    try:
        from services.review_filter_manager import get_review_filter_manager
        import asyncio

        filter_manager = get_review_filter_manager()

        # Get filter data from request
        data = request.get_json()

        # Validate required fields
        required_fields = ['agent', 'category', 'severity', 'pattern_description', 'action']
        for field in required_fields:
            if field not in data:
                return jsonify({
                    'success': False,
                    'error': f'Missing required field: {field}'
                }), 400

        # Set defaults
        filter_data = {
            'agent': data['agent'],
            'category': data['category'],
            'severity': data['severity'],
            'pattern_description': data['pattern_description'],
            'reason_ignored': data.get('reason_ignored', 'Manually created filter'),
            'sample_findings': data.get('sample_findings', []),
            'action': data['action'],
            'confidence': data.get('confidence', 0.90),
            'sample_size': data.get('sample_size', 1),
            'active': data.get('active', True),
            'manual_override': True
        }

        # Add severity adjustment fields if applicable
        if data['action'] == 'adjust_severity':
            filter_data['from_severity'] = data.get('from_severity')
            filter_data['to_severity'] = data.get('to_severity')

        # Create filter (run async function)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        filter_id = loop.run_until_complete(filter_manager.create_filter(filter_data))
        loop.close()

        return jsonify({
            'success': True,
            'filter_id': filter_id,
            'message': 'Filter created successfully'
        })

    except Exception as e:
        logger.error(f"Error creating review filter: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/review-filters/<filter_id>', methods=['PUT'])
def update_review_filter(filter_id):
    """Update an existing review filter"""
    try:
        from services.review_filter_manager import get_review_filter_manager
        import asyncio
        from datetime import datetime

        filter_manager = get_review_filter_manager()

        # Check if index exists
        if not filter_manager._ensure_index_exists():
            return jsonify({
                'success': False,
                'error': 'Review filters index not available'
            }), 503

        # Get update data from request
        data = request.get_json()

        # Build update document
        update_doc = {
            'last_updated': datetime.now().isoformat()
        }

        # Add updatable fields
        updatable_fields = [
            'pattern_description', 'reason_ignored', 'sample_findings',
            'action', 'confidence', 'active', 'from_severity', 'to_severity'
        ]

        for field in updatable_fields:
            if field in data:
                update_doc[field] = data[field]

        # Update in Elasticsearch
        filter_manager.es.update(
            index=filter_manager.filters_index,
            id=filter_id,
            body={'doc': update_doc}
        )

        # Invalidate cache
        filter_manager._invalidate_cache(data.get('agent', '*'))

        return jsonify({
            'success': True,
            'filter_id': filter_id,
            'message': 'Filter updated successfully'
        })

    except Exception as e:
        logger.error(f"Error updating review filter: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/review-filters/<filter_id>', methods=['DELETE'])
def delete_review_filter(filter_id):
    """Delete a review filter"""
    try:
        from services.review_filter_manager import get_review_filter_manager

        filter_manager = get_review_filter_manager()

        # Check if index exists
        if not filter_manager._ensure_index_exists():
            return jsonify({
                'success': False,
                'error': 'Review filters index not available'
            }), 503

        # Delete from Elasticsearch
        filter_manager.es.delete(
            index=filter_manager.filters_index,
            id=filter_id
        )

        # Invalidate cache
        filter_manager._invalidate_cache('*')

        return jsonify({
            'success': True,
            'filter_id': filter_id,
            'message': 'Filter deleted successfully'
        })

    except Exception as e:
        logger.error(f"Error deleting review filter: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/review-filters/<filter_id>/toggle', methods=['POST'])
def toggle_review_filter(filter_id):
    """Toggle a review filter active/inactive"""
    try:
        from services.review_filter_manager import get_review_filter_manager
        from datetime import datetime

        filter_manager = get_review_filter_manager()

        # Check if index exists
        if not filter_manager._ensure_index_exists():
            return jsonify({
                'success': False,
                'error': 'Review filters index not available'
            }), 503

        # Get current state
        result = filter_manager.es.get(
            index=filter_manager.filters_index,
            id=filter_id
        )

        current_active = result['_source'].get('active', True)
        new_active = not current_active

        # Update
        filter_manager.es.update(
            index=filter_manager.filters_index,
            id=filter_id,
            body={
                'doc': {
                    'active': new_active,
                    'last_updated': datetime.now().isoformat()
                }
            }
        )

        # Invalidate cache
        agent = result['_source'].get('agent')
        filter_manager._invalidate_cache(agent)

        return jsonify({
            'success': True,
            'filter_id': filter_id,
            'active': new_active,
            'message': f'Filter {"activated" if new_active else "deactivated"} successfully'
        })

    except Exception as e:
        logger.error(f"Error toggling review filter: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/review-filters/agents', methods=['GET'])
def get_available_agents():
    """Get list of available review agents"""
    return jsonify({
        'success': True,
        'agents': [
            'code_reviewer',
            'documentation_editor'
        ]
    })

@app.route('/api/review-outcomes', methods=['GET'])
def get_review_outcomes():
    """Get review outcomes for analysis"""
    try:
        from services.review_filter_manager import get_review_filter_manager
        from services.review_learning_schema import get_review_outcome_index_name

        filter_manager = get_review_filter_manager()

        # Get query parameters
        agent = request.args.get('agent')
        days = int(request.args.get('days', 30))
        limit = int(request.args.get('limit', 100))

        # Build query
        must_clauses = [
            {"term": {"type": "review_outcome"}},
            {"range": {"timestamp": {"gte": f"now-{days}d"}}}
        ]

        if agent:
            must_clauses.append({"term": {"agent.keyword": agent}})

        query = {
            "query": {
                "bool": {
                    "must": must_clauses
                }
            },
            "sort": [
                {"timestamp": "desc"}
            ],
            "size": limit
        }

        # Search across review outcome indices
        index_pattern = "review-outcomes-*"
        result = filter_manager.es.search(
            index=index_pattern,
            body=query
        )

        outcomes = [hit['_source'] for hit in result['hits']['hits']]

        return jsonify({
            'success': True,
            'outcomes': outcomes,
            'count': len(outcomes)
        })

    except Exception as e:
        logger.error(f"Error fetching review outcomes: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'outcomes': []
        }), 500

@app.route('/api/circuit-breakers', methods=['GET'])
def get_circuit_breakers():
    """Get circuit breaker status from pattern ingestion service and Claude Code"""
    try:
        # Connect to Redis
        redis_client = redis.Redis(
            host='redis',
            port=6379,
            decode_responses=True
        )

        # Extract circuit breaker states
        circuit_breakers = []

        # Get stats from Redis (optional - pattern services may be disabled)
        stats_json = redis_client.get('orchestrator:pattern_ingestion_stats')

        if stats_json:
            try:
                stats = json.loads(stats_json)

                # Redis circuit breaker
                redis_cb = stats['log_collector']['circuit_breakers']['redis']
                circuit_breakers.append({
                    'name': 'Redis Streams',
                    'service': 'log_collector',
                    'state': redis_cb['state'],
                    'failure_count': redis_cb['failure_count'],
                    'total_failures': redis_cb['total_failures'],
                    'total_successes': redis_cb['total_successes'],
                    'total_rejected': redis_cb['total_rejected'],
                    'time_in_state': redis_cb['time_in_state']
                })

                # Elasticsearch indexing circuit breaker
                es_indexing_cb = stats['log_collector']['circuit_breakers']['elasticsearch']
                circuit_breakers.append({
                    'name': 'Elasticsearch Indexing',
                    'service': 'log_collector',
                    'state': es_indexing_cb['state'],
                    'failure_count': es_indexing_cb['failure_count'],
                    'total_failures': es_indexing_cb['total_failures'],
                    'total_successes': es_indexing_cb['total_successes'],
                    'total_rejected': es_indexing_cb['total_rejected'],
                    'time_in_state': es_indexing_cb['time_in_state']
                })

                # Pattern detection circuit breaker
                pattern_cb = stats['pattern_detector']['circuit_breaker']
                circuit_breakers.append({
                    'name': 'Pattern Detection Queries',
                    'service': 'pattern_detector',
                    'state': pattern_cb['state'],
                    'failure_count': pattern_cb['failure_count'],
                    'total_failures': pattern_cb['total_failures'],
                    'total_successes': pattern_cb['total_successes'],
                    'total_rejected': pattern_cb['total_rejected'],
                    'time_in_state': pattern_cb['time_in_state']
                })
            except Exception as e:
                logger.warning(f"Could not parse pattern ingestion stats: {e}")
        else:
            logger.debug("Pattern ingestion service stats not available (services may be disabled)")

        
        # Claude Code circuit breaker
        try:
            from monitoring.claude_code_breaker import get_breaker
            breaker = get_breaker()
            if breaker:
                status = breaker.get_status()
                circuit_breakers.append({
                    'name': 'Claude Code Token Limit',
                    'service': 'claude_code',
                    'state': status['state'],
                    'is_open': status['is_open'],
                    'opened_at': status['opened_at'],
                    'reset_time': status['reset_time'],
                    'time_until_reset': status['time_until_reset'],
                    'failure_count': status['failure_count'],
                })
            else:
                logger.warning("Claude Code breaker is None!")
        except Exception as e:
            logger.error(f"Could not get Claude Code breaker status: {e}", exc_info=True)

        # GitHub API circuit breaker (breaker state only, usage metrics moved to /health)
        try:
            from services.github_api_client import get_github_client
            github_client = get_github_client()
            github_status = github_client.get_status()

            circuit_breakers.append({
                'name': 'GitHub API Rate Limit',
                'service': 'github_api',
                'state': github_status['breaker']['state'],
                'is_open': github_status['breaker']['is_open'],
                'opened_at': github_status['breaker']['opened_at'],
                'reset_time': github_status['breaker']['reset_time']
            })
        except Exception as e:
            logger.error(f"Could not get GitHub API breaker status: {e}", exc_info=True)

        # Agent-specific circuit breakers from Redis
        logger.debug("=== Starting agent circuit breaker query ===")
        try:
            # Find all agent circuit breaker keys (reuse redis_client from top of function)
            agent_breaker_keys = redis_client.keys('circuit_breaker:*:state')
            logger.debug(f"Found {len(agent_breaker_keys)} agent circuit breaker keys: {agent_breaker_keys}")
            
            for key in agent_breaker_keys:
                try:
                    # Extract agent name from key (format: circuit_breaker:agent_name:state)
                    agent_name = key.replace('circuit_breaker:', '').replace(':state', '')
                    logger.debug(f"Processing agent breaker: {agent_name}")
                    
                    # Get breaker state
                    state_json = redis_client.get(key)
                    if state_json:
                        import json
                        from datetime import datetime
                        state = json.loads(state_json)
                        logger.debug(f"Agent {agent_name} breaker state: {state}")
                        
                        # Calculate time until retry
                        time_until_retry = None
                        if state.get('state') == 'open' and state.get('last_failure_time'):
                            from services.circuit_breaker import CircuitBreaker
                            # Default recovery timeout is 30s
                            recovery_timeout = 30
                            last_failure = datetime.fromisoformat(state['last_failure_time'])
                            elapsed = (datetime.now() - last_failure).total_seconds()
                            time_until_retry = max(0, recovery_timeout - elapsed)
                        
                        circuit_breakers.append({
                            'name': f'{agent_name} (Agent)',
                            'service': 'agent_execution',
                            'agent': agent_name,
                            'state': state.get('state', 'unknown'),
                            'is_open': state.get('state') == 'open',
                            'failure_count': state.get('failure_count', 0),
                            'total_failures': state.get('total_failures', 0),
                            'total_successes': state.get('total_successes', 0),
                            'last_failure_time': state.get('last_failure_time'),
                            'time_until_retry': time_until_retry
                        })
                        logger.info(f"Added agent breaker: {agent_name}, state={state.get('state')}")
                except Exception as e:
                    logger.warning(f"Could not parse agent breaker {key}: {e}")
                    
        except Exception as e:
            logger.error(f"Could not get agent circuit breakers: {e}", exc_info=True)

        # Calculate summary
        open_count = sum(1 for cb in circuit_breakers 
                        if cb.get('state') == 'open' or cb.get('is_open') == True)
        half_open_count = sum(1 for cb in circuit_breakers 
                             if cb.get('state') == 'half_open')

        return jsonify({
            'success': True,
            'circuit_breakers': circuit_breakers,
            'summary': {
                'total': len(circuit_breakers),
                'open': open_count,
                'half_open': half_open_count,
                'healthy': len(circuit_breakers) - open_count - half_open_count
            }
        })

    except Exception as e:
        logger.error(f"Error fetching circuit breaker status: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'circuit_breakers': []
        }), 500

@app.route('/api/circuit-breakers/claude-code/reset', methods=['POST'])
def reset_claude_code_breaker():
    """Manually reset the Claude Code circuit breaker"""
    try:
        from monitoring.claude_code_breaker import get_breaker

        breaker = get_breaker()
        if not breaker:
            return jsonify({
                'success': False,
                'error': 'Claude Code breaker not available'
            }), 500

        # Close the breaker
        breaker.close()

        logger.info("🟢 Claude Code circuit breaker manually reset via API")

        return jsonify({
            'success': True,
            'message': 'Claude Code circuit breaker has been reset',
            'status': breaker.get_status()
        })

    except Exception as e:
        logger.error(f"Error resetting Claude Code breaker: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/circuit-breakers/agent/<agent_name>/reset', methods=['POST'])
def reset_agent_breaker(agent_name):
    """Manually reset an agent-specific circuit breaker"""
    try:
        import redis
        redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
        
        # Delete the breaker state from Redis
        key = f'circuit_breaker:{agent_name}:state'
        deleted = redis_client.delete(key)
        
        if deleted:
            logger.info(f"🟢 Agent circuit breaker '{agent_name}' manually reset via API")
            return jsonify({
                'success': True,
                'message': f'Agent circuit breaker \'{agent_name}\' has been reset'
            })
        else:
            return jsonify({
                'success': False,
                'error': f'Circuit breaker for agent \'{agent_name}\' not found'
            }), 404

    except Exception as e:
        logger.error(f"Error resetting agent breaker {agent_name}: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/circuit-breakers/github-api/reset', methods=['POST'])
def reset_github_api_breaker():
    """Manually reset the GitHub API circuit breaker"""
    try:
        from services.github_api_client import get_github_client

        client = get_github_client()
        if not client:
            return jsonify({
                'success': False,
                'error': 'GitHub API client not available'
            }), 500

        # Close the breaker
        client.breaker.close()

        logger.info("🟢 GitHub API circuit breaker manually reset via API")

        return jsonify({
            'success': True,
            'message': 'GitHub API circuit breaker has been reset',
            'status': {
                'state': client.breaker.state,
                'is_open': client.breaker.is_open(),
                'opened_at': client.breaker.opened_at.isoformat() if client.breaker.opened_at else None,
                'reset_time': client.breaker.reset_time.isoformat() if client.breaker.reset_time else None
            }
        })

    except Exception as e:
        logger.error(f"Error resetting GitHub API breaker: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/github-api-status', methods=['GET'])
def get_github_api_status():
    """Get GitHub API client status including rate limit and circuit breaker."""
    try:
        from services.github_api_client import get_github_client
        client = get_github_client()
        status = client.get_status()
        
        # Call alarm check to log warnings if needed
        client.alarm_if_needed()
        
        return jsonify({
            'success': True,
            'status': status
        })
    except Exception as e:
        logger.error(f"Error fetching GitHub API status: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

def collect_git_branch_data(project_name, workspace_path):
    """Collect git branch information for a project"""
    try:
        if not workspace_path.exists() or not (workspace_path / '.git').exists():
            return None

        branch_data = {
            'current_branch': None,
            'branches': [],
            'stashes': [],
            'collected_at': datetime.now().isoformat()
        }

        # Set git config to trust the directory (handles ownership mismatches)
        subprocess.run(
            ['git', 'config', '--global', '--add', 'safe.directory', str(workspace_path)],
            capture_output=True,
            timeout=5
        )

        # Get current branch
        result = subprocess.run(
            ['git', '-C', str(workspace_path), 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            branch_data['current_branch'] = result.stdout.strip()

        # Get stashes
        stash_result = subprocess.run(
            ['git', '-C', str(workspace_path), 'stash', 'list'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if stash_result.returncode == 0:
            for line in stash_result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                # Format: stash@{0}: On branch-name: message
                parts = line.split(':', 2)
                if len(parts) >= 3:
                    stash_id = parts[0].strip()
                    branch_info = parts[1].strip()
                    message = parts[2].strip()
                    branch_data['stashes'].append({
                        'id': stash_id,
                        'branch': branch_info,
                        'message': message,
                        'raw': line.strip()
                    })
                else:
                    branch_data['stashes'].append({
                        'id': line.split(':')[0] if ':' in line else 'unknown',
                        'branch': 'unknown',
                        'message': line,
                        'raw': line.strip()
                    })

        # Get all local branches with their tracking info
        result = subprocess.run(
            ['git', '-C', str(workspace_path), 'branch', '-vv'],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue

                # Parse branch line (format: "* branch_name commit_hash [remote/branch] commit message")
                is_current = line.startswith('*')
                parts = line.strip().lstrip('* ').split(None, 1)

                if not parts:
                    continue

                branch_name = parts[0]

                # Extract tracking branch if present
                tracking_branch = None
                if '[' in line and ']' in line:
                    tracking_start = line.index('[') + 1
                    tracking_end = line.index(']')
                    tracking_info = line[tracking_start:tracking_end]
                    # Remove ahead/behind info if present
                    tracking_branch = tracking_info.split(':')[0].strip()

                # Get file changes for this branch
                file_changes = []

                # For the current branch, check working directory changes first
                if is_current:
                    # Get unstaged changes
                    status_result = subprocess.run(
                        ['git', '-C', str(workspace_path), 'status', '--porcelain'],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )

                    if status_result.returncode == 0:
                        for status_line in status_result.stdout.strip().split('\n'):
                            if not status_line.strip():
                                continue
                            # Format: "XY filename" where X is staged, Y is unstaged
                            # Positions 0-1 are status, position 2 is space, filename starts at 3
                            if len(status_line) < 4:
                                continue
                            status_chars = status_line[:2]
                            file_name = status_line[3:]

                            change_type = 'modified'
                            if 'A' in status_chars:
                                change_type = 'added'
                            elif 'D' in status_chars:
                                change_type = 'deleted'
                            elif '?' in status_chars:
                                change_type = 'untracked'

                            file_changes.append({
                                'file': file_name,
                                'insertions': 0,
                                'deletions': 0,
                                'change_type': change_type,
                                'staged': status_chars[0] != ' ' and status_chars[0] != '?',
                                'unstaged': status_chars[1] != ' '
                            })

                # Compare to remote tracking branch or main/master
                compare_target = tracking_branch if tracking_branch else 'origin/main'

                # Get diff stat
                diff_result = subprocess.run(
                    ['git', '-C', str(workspace_path), 'diff', '--stat', compare_target, branch_name],
                    capture_output=True,
                    text=True,
                    timeout=5
                )

                if diff_result.returncode == 0 and diff_result.stdout.strip():
                    for diff_line in diff_result.stdout.strip().split('\n'):
                        if '|' in diff_line and not diff_line.strip().endswith('changed'):
                            # Parse git diff stat line (format: " file.txt | 10 +++++-----")
                            file_part = diff_line.split('|')[0].strip()
                            stat_part = diff_line.split('|')[1].strip() if '|' in diff_line else ''

                            # Extract insertions/deletions from stat
                            insertions = stat_part.count('+')
                            deletions = stat_part.count('-')

                            file_changes.append({
                                'file': file_part,
                                'insertions': insertions,
                                'deletions': deletions,
                                'change_type': 'modified'
                            })

                # Get untracked/new files
                status_result = subprocess.run(
                    ['git', '-C', str(workspace_path), 'diff', '--name-status', compare_target, branch_name],
                    capture_output=True,
                    text=True,
                    timeout=5
                )

                if status_result.returncode == 0:
                    for status_line in status_result.stdout.strip().split('\n'):
                        if not status_line.strip():
                            continue
                        parts = status_line.split(None, 1)
                        if len(parts) >= 2:
                            status_char = parts[0]
                            file_name = parts[1]

                            # Check if we already have this file from diff stat
                            if not any(fc['file'] == file_name for fc in file_changes):
                                change_type = 'modified'
                                if status_char == 'A':
                                    change_type = 'added'
                                elif status_char == 'D':
                                    change_type = 'deleted'
                                elif status_char == 'M':
                                    change_type = 'modified'

                                file_changes.append({
                                    'file': file_name,
                                    'insertions': 0,
                                    'deletions': 0,
                                    'change_type': change_type
                                })

                branch_info = {
                    'name': branch_name,
                    'is_current': is_current,
                    'tracking_branch': tracking_branch,
                    'file_changes': file_changes,
                    'total_files_changed': len(file_changes)
                }

                branch_data['branches'].append(branch_info)

        return branch_data

    except subprocess.TimeoutExpired:
        logger.error(f"Git command timeout for project {project_name}")
        return None
    except Exception as e:
        logger.error(f"Error collecting git branch data for {project_name}: {e}")
        return None

def git_branch_collector_thread():
    """Background thread that periodically collects git branch data"""
    from config.manager import config_manager

    while True:
        try:
            # Also check /app (clauditoreum itself) for git data
            app_path = Path("/app")
            if (app_path / '.git').exists():
                branch_data = collect_git_branch_data('clauditoreum', app_path)
                if branch_data:
                    with git_branch_cache_lock:
                        git_branch_cache['clauditoreum'] = branch_data
                    logger.debug("Updated git branch cache for clauditoreum")

            # Check configured visible (non-hidden) projects in workspace
            project_configs = config_manager.list_visible_projects()

            for project_name in project_configs:
                workspace_path = Path(f"/workspace/{project_name}")

                if workspace_path.exists():
                    branch_data = collect_git_branch_data(project_name, workspace_path)

                    if branch_data:
                        with git_branch_cache_lock:
                            git_branch_cache[project_name] = branch_data
                        logger.debug(f"Updated git branch cache for {project_name}")

            # Sleep for 30 seconds
            time.sleep(30)

        except Exception as e:
            logger.error(f"Error in git branch collector: {e}")
            time.sleep(30)

@app.route('/api/projects', methods=['GET'])
def get_projects():
    """Get all configured projects with their dev container status and other metadata"""
    try:
        from config.manager import config_manager
        from services.dev_container_state import dev_container_state
        from pathlib import Path
        import os

        projects = []

        # Add clauditoreum itself as a special project for git monitoring
        app_path = Path("/app")
        if (app_path / '.git').exists():
            git_branches = None
            with git_branch_cache_lock:
                git_branches = git_branch_cache.get('clauditoreum')

            projects.append({
                'name': 'clauditoreum',
                'github': {
                    'org': 'tinkermonkey',
                    'repo': 'clauditoreum',
                    'url': 'https://github.com/tinkermonkey/clauditoreum'
                },
                'pipelines': [],
                'workspace': {
                    'path': '/app',
                    'exists': True,
                    'git_branches': git_branches
                },
                'dev_container': {
                    'status': 'n/a',
                    'image_name': None,
                    'updated_at': None,
                    'error_message': None
                }
            })

        # Get all configured projects from config manager (excluding hidden projects)
        project_configs = config_manager.list_visible_projects()

        for project_name in project_configs:
            try:
                # Get project config
                project_config = config_manager.get_project_config(project_name)

                if not project_config:
                    continue

                # Get dev container status
                container_status = dev_container_state.get_status(project_name)
                image_name = dev_container_state.get_image_name(project_name)

                # Read state file for more details
                state_file = dev_container_state.get_state_file(project_name)
                state_details = {}
                if state_file.exists():
                    import yaml
                    with open(state_file, 'r') as f:
                        state_details = yaml.safe_load(f) or {}

                # Check if project directory exists
                workspace_path = Path(f"/workspace/{project_name}")
                project_exists = workspace_path.exists()

                # Get GitHub info
                github_info = {}
                if hasattr(project_config, 'github'):
                    github_info = {
                        'org': project_config.github.get('org'),
                        'repo': project_config.github.get('repo'),
                        'url': f"https://github.com/{project_config.github.get('org')}/{project_config.github.get('repo')}"
                    }

                # Get pipeline info with lock status
                pipelines = []
                if hasattr(project_config, 'pipelines') and project_config.pipelines:
                    from services.pipeline_lock_manager import get_pipeline_lock_manager
                    lock_manager = get_pipeline_lock_manager()

                    # pipelines is a list of ProjectPipeline objects
                    for p in project_config.pipelines:
                        if hasattr(p, 'name') and hasattr(p, 'board_name'):
                            # Get lock status for this pipeline
                            lock = lock_manager.get_lock(project_name, p.board_name)
                            pipeline_info = {
                                'name': p.name,
                                'board': p.board_name,
                                'lock': None
                            }

                            if lock and lock.lock_status == 'locked':
                                pipeline_info['lock'] = {
                                    'locked_by_issue': lock.locked_by_issue,
                                    'locked_at': lock.lock_acquired_at,
                                    'is_locked': True
                                }

                            pipelines.append(pipeline_info)

                # Get cached git branch data
                git_branches = None
                with git_branch_cache_lock:
                    git_branches = git_branch_cache.get(project_name)

                projects.append({
                    'name': project_name,
                    'github': github_info,
                    'pipelines': pipelines,
                    'workspace': {
                        'path': str(workspace_path),
                        'exists': project_exists,
                        'git_branches': git_branches
                    },
                    'dev_container': {
                        'status': container_status.value,
                        'image_name': image_name,
                        'updated_at': state_details.get('updated_at'),
                        'error_message': state_details.get('error_message')
                    }
                })

            except Exception as e:
                logger.error(f"Error getting status for project {project_name}: {e}")
                projects.append({
                    'name': project_name,
                    'error': str(e),
                    'dev_container': {
                        'status': 'error'
                    }
                })

        return jsonify({
            'success': True,
            'projects': projects,
            'count': len(projects)
        }), 200

    except Exception as e:
        logger.error(f"Error fetching project status: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({
            'success': False,
            'error': str(e),
            'projects': []
        }), 500

@app.route('/api/projects/<project>/pipelines/<board>/release-lock', methods=['POST'])
def release_pipeline_lock(project, board):
    """
    Manually release a pipeline lock for a project/board.

    This is useful when a lock is stuck due to agent crashes or other issues.

    Args:
        project: Project name
        board: Board name (e.g., "SDLC Execution")

    Request body (optional):
        {
            "issue_number": 123  # If provided, only releases lock if held by this issue
        }

    Returns:
        JSON with success status and details
    """
    try:
        from services.pipeline_lock_manager import get_pipeline_lock_manager
        from services.pipeline_queue_manager import get_pipeline_queue_manager

        lock_manager = get_pipeline_lock_manager()
        queue_manager = get_pipeline_queue_manager(project, board)

        # Get optional issue_number from request body
        request_data = request.get_json() if request.is_json else {}
        specified_issue = request_data.get('issue_number')

        # Check current lock status
        lock = lock_manager.get_lock(project, board)

        if not lock or lock.lock_status != 'locked':
            return jsonify({
                'success': False,
                'message': f'No active lock found for {project}/{board}',
                'lock_status': None
            }), 404

        # If issue_number specified, verify it matches
        if specified_issue and lock.locked_by_issue != specified_issue:
            return jsonify({
                'success': False,
                'message': f'Lock is held by issue #{lock.locked_by_issue}, not #{specified_issue}',
                'lock_status': {
                    'locked_by_issue': lock.locked_by_issue,
                    'locked_at': lock.lock_acquired_at
                }
            }), 400

        locked_by_issue = lock.locked_by_issue

        # Release the lock
        released = lock_manager.release_lock(project, board, locked_by_issue)

        if released:
            # Also reset the issue in the queue from active to waiting
            queue_manager.reset_issue_to_waiting(locked_by_issue)

            logger.info(
                f"Manually released pipeline lock for {project}/{board} "
                f"(issue #{locked_by_issue})"
            )

            return jsonify({
                'success': True,
                'message': f'Successfully released lock for issue #{locked_by_issue}',
                'released_issue': locked_by_issue
            }), 200
        else:
            return jsonify({
                'success': False,
                'message': 'Failed to release lock (may have already been released)',
                'lock_status': None
            }), 500

    except Exception as e:
        logger.error(f"Error releasing pipeline lock: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/workflow-config/<project>/<board>', methods=['GET'])
def get_workflow_config(project, board):
    """
    Get the workflow configuration for a specific project and board.
    
    Returns the workflow template including columns with their types (maker/review)
    and maker-reviewer relationships.
    
    Args:
        project: Project name
        board: Board name
        
    Returns:
        JSON with workflow configuration including:
        - columns: List of workflow columns with type, agent, maker_agent info
        - name: Workflow template name
    """
    try:
        from config.manager import config_manager
        
        # Get project config
        project_config = config_manager.get_project_config(project)
        if not project_config:
            return jsonify({
                'success': False,
                'error': f'Project {project} not found'
            }), 404
        
        # Find pipeline config for this board
        pipeline_config = next(
            (p for p in project_config.pipelines if p.board_name == board),
            None
        )
        
        if not pipeline_config:
            return jsonify({
                'success': False,
                'error': f'No pipeline configured for board {board}'
            }), 404
        
        # Get workflow template
        workflow_template = config_manager.get_workflow_template(pipeline_config.workflow)
        
        if not workflow_template:
            return jsonify({
                'success': False,
                'error': f'Workflow template {pipeline_config.workflow} not found'
            }), 404
        
        # Build response with column information
        columns = []
        for col in workflow_template.columns:
            columns.append({
                'name': col.name,
                'type': col.type,  # 'maker' or 'review'
                'agent': col.agent,
                'maker_agent': col.maker_agent,  # For review columns: which agent is being reviewed
                'max_iterations': col.max_iterations,
                'auto_advance_on_approval': col.auto_advance_on_approval,
                'escalate_on_blocked': col.escalate_on_blocked
            })
        
        return jsonify({
            'success': True,
            'workflow': {
                'name': workflow_template.name,
                'columns': columns,
                'pipeline_trigger_columns': workflow_template.pipeline_trigger_columns,
                'pipeline_exit_columns': workflow_template.pipeline_exit_columns
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error fetching workflow config for {project}/{board}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# ============================================================================
# REPAIR CYCLE CONTAINER MONITORING ENDPOINTS
# ============================================================================

@app.route('/api/repair-cycle-containers', methods=['GET'])
def get_repair_cycle_containers():
    """Get all running repair cycle containers with status and progress"""
    try:
        from services.agent_container_recovery import get_agent_container_recovery
        from services import project_workspace
        import subprocess
        
        recovery = get_agent_container_recovery()
        
        # Get running containers
        containers = recovery.get_running_repair_cycle_containers()
        
        result = []
        for container in containers:
            # Parse container name
            info = recovery.parse_repair_cycle_container_name(container['name'])
            if not info:
                continue
                
            project = info['project']
            issue_number = info['issue_number']
            run_id = info['run_id']

            # Get checkpoint
            checkpoint = recovery.check_repair_cycle_checkpoint(project, int(issue_number))

            # Get result
            result_data = recovery.check_repair_cycle_result(project, int(issue_number), run_id)
            
            # Get container age
            container_age_seconds = None
            try:
                import dateutil.parser
                created_str = container.get('created_at', '')
                if created_str:
                    created_time = dateutil.parser.parse(created_str)
                    container_age_seconds = (datetime.utcnow() - created_time.replace(tzinfo=None)).total_seconds()
            except:
                pass
            
            result.append({
                'container_name': container['name'],
                'container_id': container['id'],
                'project': project,
                'issue_number': issue_number,
                'run_id': run_id,
                'status': container.get('status', 'running'),
                'created_at': container.get('created_at'),
                'container_age_seconds': container_age_seconds,
                'checkpoint': checkpoint,
                'result': result_data,
                'is_finished': result_data is not None
            })
        
        return jsonify({
            'success': True,
            'containers': result
        }), 200
        
    except Exception as e:
        logger.error(f"Failed to get repair cycle containers: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/repair-cycle-containers/<project>/<int:issue>/checkpoint', methods=['GET'])
def get_repair_cycle_checkpoint(project, issue):
    """Get current checkpoint state for a specific repair cycle"""
    try:
        from services.agent_container_recovery import get_agent_container_recovery
        
        recovery = get_agent_container_recovery()
        checkpoint = recovery.check_repair_cycle_checkpoint(project)
        
        if checkpoint:
            return jsonify({
                'success': True,
                'checkpoint': checkpoint
            }), 200
        else:
            return jsonify({
                'success': False,
                'error': 'No checkpoint found'
            }), 404
            
    except Exception as e:
        logger.error(f"Failed to get checkpoint for {project}/{issue}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/repair-cycle-containers/<project>/<int:issue>/logs', methods=['GET'])
def get_repair_cycle_logs(project, issue):
    """Get container logs for a specific repair cycle"""
    try:
        import subprocess
        
        # Find container by project and issue
        from services.agent_container_recovery import get_agent_container_recovery
        recovery = get_agent_container_recovery()
        
        containers = recovery.get_running_repair_cycle_containers()
        target_container = None
        
        for container in containers:
            info = recovery.parse_repair_cycle_container_name(container['name'])
            if info and info['project'] == project and info['issue_number'] == str(issue):
                target_container = container
                break
        
        if not target_container:
            return jsonify({
                'success': False,
                'error': f'No running container found for {project} issue #{issue}'
            }), 404
        
        # Get logs (last 500 lines)
        result = subprocess.run(
            ['docker', 'logs', '--tail', '500', target_container['name']],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            return jsonify({
                'success': True,
                'logs': result.stdout + result.stderr,
                'container_name': target_container['name']
            }), 200
        else:
            return jsonify({
                'success': False,
                'error': f'Failed to get logs: {result.stderr}'
            }), 500
            
    except Exception as e:
        logger.error(f"Failed to get logs for {project}/{issue}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/repair-cycle-containers/<project>/<int:issue>/kill', methods=['POST'])
def kill_repair_cycle_container(project, issue):
    """Kill a repair cycle container"""
    try:
        import subprocess
        
        # Find container by project and issue
        from services.agent_container_recovery import get_agent_container_recovery
        recovery = get_agent_container_recovery()
        
        containers = recovery.get_running_repair_cycle_containers()
        target_container = None
        
        for container in containers:
            info = recovery.parse_repair_cycle_container_name(container['name'])
            if info and info['project'] == project and info['issue_number'] == str(issue):
                target_container = container
                break
        
        if not target_container:
            return jsonify({
                'success': False,
                'error': f'No running container found for {project} issue #{issue}'
            }), 404
        
        logger.warning(f"KILL SWITCH ACTIVATED for repair cycle container: {target_container['name']}")
        
        # Stop the container immediately
        result = subprocess.run(
            ['docker', 'rm', '-f', target_container['name']],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            logger.info(f"Successfully killed repair cycle container: {target_container['name']}")
            
            # Remove from Redis tracking
            redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
            redis_client.delete(f'repair_cycle:container:{project}:{issue}')
            
            return jsonify({
                'success': True,
                'message': f'Container {target_container["name"]} stopped',
                'container_name': target_container['name']
            }), 200
        else:
            logger.error(f"Failed to kill container {target_container['name']}: {result.stderr}")
            return jsonify({
                'success': False,
                'error': f'Failed to kill container: {result.stderr}'
            }), 500
            
    except Exception as e:
        logger.error(f"Failed to kill container for {project}/{issue}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# END REPAIR CYCLE CONTAINER MONITORING ENDPOINTS
# ============================================================================


# ============================================================================
# PIPELINE QUEUE AND LOCK STATUS ENDPOINTS
# ============================================================================

@app.route('/api/pipeline-queue/<project>/<board>', methods=['GET'])
def get_pipeline_queue_status(project, board):
    """
    Get current pipeline queue status with fresh GitHub ordering.

    Returns queue state, lock status, and waiting issues ordered by
    their position on the GitHub board.
    """
    try:
        from services.pipeline_lock_manager import get_pipeline_lock_manager
        from services.pipeline_queue_manager import get_pipeline_queue_manager
        from config.manager import config_manager

        lock_manager = get_pipeline_lock_manager()
        pipeline_queue = get_pipeline_queue_manager(project, board)

        # Get current lock
        lock = lock_manager.get_lock(project, board)

        # Get queue summary
        queue_summary = pipeline_queue.get_queue_summary()

        # Build response
        response = {
            'project': project,
            'board': board,
            'locked': lock is not None and lock.lock_status == 'locked',
            'locked_by': None,
            'waiting': [],
            'total_in_queue': queue_summary['total_issues'],
            'last_updated': datetime.utcnow().isoformat() + 'Z'
        }

        # Add lock holder info
        if lock and lock.lock_status == 'locked':
            active_issue = queue_summary.get('active_issue')
            if active_issue:
                response['locked_by'] = {
                    'issue_number': lock.locked_by_issue,
                    'locked_at': lock.lock_acquired_at,
                    'current_column': None,  # Would need to query GitHub for this
                    'queued_at': active_issue.get('queued_at')
                }

        # Add waiting issues with fresh order
        waiting_issues = queue_summary.get('waiting_issues', [])

        # Sort by position
        waiting_issues.sort(key=lambda x: x.get('position_in_column', 999))

        response['waiting'] = [
            {
                'issue_number': issue['issue_number'],
                'position': issue.get('position_in_column', 0),
                'queued_at': issue['queued_at'],
                'initial_column': issue['initial_column'],
                'wait_time_seconds': int(
                    (datetime.fromisoformat(response['last_updated'].replace('Z', ''))
                     - datetime.fromisoformat(issue['queued_at'].replace('Z', ''))).total_seconds()
                )
            }
            for issue in waiting_issues
        ]

        return jsonify(response), 200

    except Exception as e:
        logger.error(f"Failed to get pipeline queue status for {project}/{board}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/api/pipeline-locks', methods=['GET'])
def get_all_pipeline_locks():
    """
    Get all active pipeline locks across all projects.

    Useful for monitoring which pipelines are currently executing.
    """
    try:
        from services.pipeline_lock_manager import get_pipeline_lock_manager
        from config.manager import config_manager

        lock_manager = get_pipeline_lock_manager()

        all_locks = []

        # Iterate through all visible projects
        for project_name in config_manager.list_visible_projects():
            try:
                project_config = config_manager.get_project_config(project_name)

                for pipeline in project_config.pipelines:
                    if not pipeline.active:
                        continue

                    lock = lock_manager.get_lock(project_name, pipeline.board_name)

                    if lock and lock.lock_status == 'locked':
                        all_locks.append({
                            'project': lock.project,
                            'board': lock.board,
                            'locked_by_issue': lock.locked_by_issue,
                            'lock_acquired_at': lock.lock_acquired_at,
                            'lock_status': lock.lock_status
                        })
            except Exception as e:
                logger.error(f"Error checking locks for project {project_name}: {e}")
                continue

        return jsonify({
            'locks': all_locks,
            'total_locks': len(all_locks),
            'timestamp': datetime.utcnow().isoformat() + 'Z'
        }), 200

    except Exception as e:
        logger.error(f"Failed to get all pipeline locks: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/pipeline-queue/<project>/<board>/refresh', methods=['POST'])
def refresh_pipeline_queue_order(project, board):
    """
    Manually refresh queue order from GitHub board.

    Fetches current board order and updates queue positions.
    Useful for debugging or forcing a sync after manual reordering.
    """
    try:
        from services.pipeline_queue_manager import get_pipeline_queue_manager

        pipeline_queue = get_pipeline_queue_manager(project, board)

        # Get next waiting issue (this fetches fresh order from GitHub)
        next_issue = pipeline_queue.get_next_waiting_issue()

        # Get updated queue summary
        queue_summary = pipeline_queue.get_queue_summary()

        return jsonify({
            'success': True,
            'message': 'Queue order refreshed from GitHub',
            'next_issue': next_issue['issue_number'] if next_issue else None,
            'total_waiting': queue_summary['waiting_count'],
            'timestamp': datetime.utcnow().isoformat() + 'Z'
        }), 200

    except Exception as e:
        logger.error(f"Failed to refresh queue order for {project}/{board}: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# END PIPELINE QUEUE AND LOCK STATUS ENDPOINTS
# ============================================================================

def redis_subscriber_thread():
    """Background thread that listens to Redis pub/sub and broadcasts to WebSocket clients"""
    global subscriber_health
    
    subscriber_health['is_running'] = True
    subscriber_health['started_at'] = datetime.utcnow().isoformat() + 'Z'
    
    try:
        # Connect to Redis
        redis_client = redis.Redis(
            host='redis',
            port=6379,
            decode_responses=True
        )

        # Subscribe to both agent events and Claude stream channels
        pubsub = redis_client.pubsub()
        pubsub.subscribe('orchestrator:agent_events', 'orchestrator:claude_stream')

        logger.info("Redis subscriber started, listening for agent events and Claude streams...")

        # Add heartbeat counter for health monitoring
        message_count = 0
        
        for message in pubsub.listen():
            if message['type'] == 'message':
                message_count += 1
                subscriber_health['messages_processed'] = message_count
                subscriber_health['last_message_time'] = datetime.utcnow().isoformat() + 'Z'
                
                try:
                    # Parse event
                    event_data = json.loads(message['data'])
                    
                    # Log every 10th message for health monitoring
                    if message_count % 10 == 0:
                        logger.debug(f"Redis subscriber health check: processed {message_count} messages")

                    # Route to appropriate websocket event based on channel
                    if message['channel'] == 'orchestrator:agent_events':
                        # Check if this is a decision event
                        event_type = event_data.get('event_type', '')
                        decision_event_types = [
                            'agent_routing_decision',
                            'workspace_routing_decision',
                            'status_progression_decision',
                            'review_cycle_started',
                            'review_iteration_started',
                            'reviewer_selected',
                            'maker_selected',
                            'review_escalated',
                            'review_cycle_completed',
                            'error_handling_decision',
                            'feedback_detected',
                            'task_queued'
                        ]

                        medic_event_types = [
                            'medic_signature_created',
                            'medic_signature_updated',
                            'medic_signature_trending',
                            'medic_signature_resolved',
                            'medic_investigation_queued',
                            'medic_investigation_started',
                            'medic_investigation_completed',
                            'medic_investigation_failed'
                        ]

                        if event_type in decision_event_types:
                            # Decision event - route to decision_event handler
                            socketio.emit('decision_event', event_data)
                            logger.debug(f"Broadcasted decision event: {event_type}")
                        elif event_type in medic_event_types:
                            # Medic event - broadcast to specific medic channel
                            socketio.emit(event_type, event_data)
                            logger.info(f"Broadcasted medic event: {event_type} (fingerprint_id={event_data.get('data', {}).get('fingerprint_id', 'N/A')})")
                        else:
                            # Regular agent events (including lifecycle events)
                            socketio.emit('agent_event', event_data)

                            # Log agent lifecycle events at INFO level for visibility
                            if event_type in ['agent_initialized', 'agent_started', 'agent_completed', 'agent_failed']:
                                logger.info(f"Broadcasted agent lifecycle event: {event_type} (agent={event_data.get('agent')}, task_id={event_data.get('task_id')})")
                            else:
                                logger.debug(f"Broadcasted agent event: {event_type}")
                    elif message['channel'] == 'orchestrator:claude_stream':
                        # Claude stream events
                        socketio.emit('claude_stream_event', event_data)
                        logger.debug(f"Broadcasted Claude stream event from {event_data.get('agent')}")

                except Exception as e:
                    error_msg = f"Error processing Redis message: {e}"
                    logger.error(error_msg, exc_info=True)
                    subscriber_health['last_error'] = error_msg
            elif message['type'] == 'subscribe':
                logger.info(f"Subscribed to Redis channel: {message['channel']}")

    except Exception as e:
        error_msg = f"Redis subscriber thread crashed: {e}"
        logger.error(error_msg, exc_info=True)
        subscriber_health['is_running'] = False
        subscriber_health['last_error'] = error_msg
        
        # Try to restart the subscriber after a delay
        import time
        time.sleep(5)
        logger.warning("Attempting to restart Redis subscriber thread...")
        redis_subscriber_thread()


# ========== MEDIC HELPER FUNCTIONS ==========

def _enrich_signatures_with_redis_status(signatures: List[dict], system_type: str = "docker") -> List[dict]:
    """
    Enrich Elasticsearch signature results with real-time Redis status.

    This ensures the UI always shows the authoritative investigation status from Redis,
    overriding potentially stale Elasticsearch data.

    Args:
        signatures: List of signature dicts from Elasticsearch
        system_type: "docker" or "claude" to determine Redis key prefix

    Returns:
        Enriched signature list with updated investigation_status field
    """
    if not signatures:
        return signatures

    try:
        # Initialize Redis client with string responses
        redis_client = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)

        # Determine Redis key prefix
        if system_type == "docker":
            prefix = "medic:docker_investigation"
        elif system_type == "claude":
            prefix = "medic:claude_investigation"
        else:
            logger.warning(f"Unknown system_type '{system_type}', skipping enrichment")
            return signatures

        # Get current state from Redis (batch operations for efficiency)
        active_set = redis_client.smembers(f"{prefix}:active")
        queued_list = redis_client.lrange(f"{prefix}:queue", 0, -1)
        queued_set = set(queued_list)

        # Enrich each signature
        for sig in signatures:
            fp_id = sig.get('fingerprint_id')
            if not fp_id:
                continue

            try:
                # Query Redis for authoritative status
                redis_status_key = f"{prefix}:{fp_id}:status"
                redis_status = redis_client.get(redis_status_key)
                in_active = fp_id in active_set
                in_queue = fp_id in queued_set

                # Determine authoritative status from Redis
                authoritative_status = None
                status_source = "elasticsearch"  # Default

                if in_queue:
                    authoritative_status = "queued"
                    status_source = "redis"
                elif in_active:
                    # In active set - check status key for precision
                    if redis_status == "in_progress":
                        authoritative_status = "in_progress"
                    elif redis_status == "starting":
                        authoritative_status = "starting"
                    else:
                        authoritative_status = "in_progress"  # Default for active
                    status_source = "redis"
                elif redis_status:
                    # Not in queue or active, but has Redis status
                    # Map Redis status to ES status
                    status_map = {
                        "completed": "completed",
                        "failed": "failed",
                        "ignored": "ignored",
                        "timeout": "timeout",
                        "stalled": "failed",
                    }
                    authoritative_status = status_map.get(redis_status, redis_status)
                    status_source = "redis"

                # Override ES status if Redis has authoritative data
                if authoritative_status:
                    es_status = sig.get('investigation_status')
                    if es_status != authoritative_status:
                        logger.debug(
                            f"Enrichment override for {fp_id[:16]}...: "
                            f"ES:{es_status} -> Redis:{authoritative_status}"
                        )
                        sig['investigation_status'] = authoritative_status
                        sig['_status_source'] = status_source
                        sig['_status_enriched'] = True
                    else:
                        sig['_status_source'] = status_source
                        sig['_status_enriched'] = False
                else:
                    # No Redis data - keep ES status
                    sig['_status_source'] = "elasticsearch"
                    sig['_status_enriched'] = False

            except Exception as e:
                logger.error(f"Error enriching signature {fp_id}: {e}")
                # On error, keep original ES data
                sig['_status_source'] = "elasticsearch"
                sig['_status_enriched'] = False

        return signatures

    except Exception as e:
        logger.error(f"Error in signature enrichment: {e}", exc_info=True)
        # On error, return original signatures unchanged
        return signatures


# ========== MEDIC API ENDPOINTS ==========

@app.route('/api/medic/failure-signatures', methods=['GET'])
def get_failure_signatures():
    """
    Get Docker failure signatures with filtering and pagination.

    BACKWARD COMPATIBILITY: This endpoint is maintained for backward compatibility.
    - Returns Docker container failure signatures from medic-docker-failures-* indices
    - For new code, use /api/medic/docker/failure-signatures (explicit)
    - For unified view (Docker + Claude), use /api/medic/failure-signatures/all

    Query params:
    - status: Filter by status (new, recurring, trending, resolved, ignored)
    - severity: Filter by severity (ERROR, WARNING, CRITICAL)
    - investigation_status: Filter by investigation status
    - from_date: Filter by first_seen >= date (ISO format)
    - to_date: Filter by first_seen <= date (ISO format)
    - limit: Max results (default 50)
    - offset: Pagination offset (default 0)
    """
    try:
        # Build Elasticsearch query
        must_clauses = []

        # Status filter
        if request.args.get('status'):
            must_clauses.append({"term": {"status": request.args['status']}})

        # Severity filter
        if request.args.get('severity'):
            must_clauses.append({"term": {"severity": request.args['severity']}})

        # Investigation status filter
        if request.args.get('investigation_status'):
            must_clauses.append({"term": {"investigation_status": request.args['investigation_status']}})

        # Date range filter
        date_filter = {}
        if request.args.get('from_date'):
            date_filter['gte'] = request.args['from_date']
        if request.args.get('to_date'):
            date_filter['lte'] = request.args['to_date']
        if date_filter:
            must_clauses.append({"range": {"first_seen": date_filter}})

        # Build query
        if must_clauses:
            query = {
                "query": {
                    "bool": {"must": must_clauses}
                },
                "size": int(request.args.get('limit', 50)),
                "from": int(request.args.get('offset', 0)),
                "sort": [{"last_seen": {"order": "desc"}}]
            }
        else:
            query = {
                "query": {
                    "match_all": {}
                },
                "size": int(request.args.get('limit', 50)),
                "from": int(request.args.get('offset', 0)),
                "sort": [{"last_seen": {"order": "desc"}}]
            }

        # Execute search (use new index pattern)
        result = es_client.search(index="medic-docker-failures-*", body=query)

        signatures = [hit['_source'] for hit in result['hits']['hits']]

        # Enrich with real-time Redis status (source of truth)
        signatures = _enrich_signatures_with_redis_status(signatures, system_type="docker")

        return jsonify({
            'signatures': signatures,
            'total': result['hits']['total']['value'],
            'limit': int(request.args.get('limit', 50)),
            'offset': int(request.args.get('offset', 0))
        }), 200

    except Exception as e:
        logger.error(f"Failed to get failure signatures: {e}", exc_info=True)
        # Return empty result instead of error for better UX when index doesn't exist
        return jsonify({
            'signatures': [],
            'total': 0,
            'limit': int(request.args.get('limit', 50)),
            'offset': int(request.args.get('offset', 0))
        }), 200


@app.route('/api/medic/failure-signatures/<fingerprint_id>', methods=['GET'])
def get_failure_signature(fingerprint_id):
    """Get detailed information about a specific failure signature"""
    try:
        result = es_client.search(
            index="medic-docker-failures-*",
            body={
                "query": {
                    "term": {"fingerprint_id": fingerprint_id}
                }
            }
        )

        if result['hits']['total']['value'] == 0:
            return jsonify({'error': 'Signature not found'}), 404

        signature = result['hits']['hits'][0]['_source']

        # Fetch agent_execution_id from Redis if investigation is active
        try:
            import redis
            redis_client = redis.Redis(
                host=os.getenv('REDIS_HOST', 'redis'),
                port=int(os.getenv('REDIS_PORT', 6379)),
                decode_responses=True
            )
            agent_execution_id = redis_client.get(f"medic:docker_investigation:{fingerprint_id}:agent_execution_id")
            if agent_execution_id:
                signature['agent_execution_id'] = agent_execution_id
            redis_client.close()
        except Exception as e:
            logger.warning(f"Failed to fetch agent_execution_id from Redis: {e}")

        return jsonify(signature), 200

    except Exception as e:
        logger.error(f"Failed to get signature {fingerprint_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/medic/failure-signatures/<fingerprint_id>', methods=['DELETE'])
def delete_failure_signature(fingerprint_id):
    """
    Delete a failure signature and its associated investigation assets.
    """
    try:
        # 1. Delete from Elasticsearch
        result = es_client.delete_by_query(
            index="medic-docker-failures-*",
            body={
                "query": {
                    "term": {"fingerprint_id": fingerprint_id}
                }
            }
        )
        
        deleted_count = result.get('deleted', 0)
        
        # 2. Delete investigation assets
        report_manager = get_report_manager()
        assets_deleted = report_manager.cleanup_investigation(fingerprint_id)
        
        if deleted_count == 0 and not assets_deleted:
             return jsonify({'error': 'Signature not found'}), 404
             
        return jsonify({
            'fingerprint_id': fingerprint_id,
            'deleted_from_es': deleted_count > 0,
            'assets_deleted': assets_deleted,
            'message': f'Successfully deleted signature {fingerprint_id}'
        }), 200

    except Exception as e:
        logger.error(f"Failed to delete signature {fingerprint_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/medic/failure-signatures/<fingerprint_id>/occurrences', methods=['GET'])
def get_signature_occurrences(fingerprint_id):
    """
    Get detailed occurrence history for a signature.

    Query params:
    - from_date: Filter by timestamp >= date
    - to_date: Filter by timestamp <= date
    - limit: Max results (default 100)
    """
    try:
        # Get signature with sample entries
        result = es_client.search(
            index="medic-docker-failures-*",
            body={
                "query": {
                    "term": {"fingerprint_id": fingerprint_id}
                },
                "_source": ["sample_entries", "fingerprint_id"]
            }
        )

        if result['hits']['total']['value'] == 0:
            return jsonify({'error': 'Signature not found'}), 404

        occurrences = result['hits']['hits'][0]['_source'].get('sample_entries', [])

        # Apply date filters
        from_date = request.args.get('from_date')
        to_date = request.args.get('to_date')
        limit = int(request.args.get('limit', 100))

        if from_date or to_date:
            filtered = []
            for entry in occurrences:
                entry_time = entry['timestamp']
                if from_date and entry_time < from_date:
                    continue
                if to_date and entry_time > to_date:
                    continue
                filtered.append(entry)
            occurrences = filtered

        # Limit results
        occurrences = occurrences[:limit]

        return jsonify({
            'fingerprint_id': fingerprint_id,
            'occurrences': occurrences,
            'total': len(occurrences)
        }), 200

    except Exception as e:
        logger.error(f"Failed to get occurrences for {fingerprint_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/medic/failure-signatures/<fingerprint_id>/status', methods=['PUT'])
def update_signature_status(fingerprint_id):
    """
    Update the status of a failure signature.

    Request body:
    {
        "status": "ignored" | "new" | "recurring" | "trending" | "resolved",
        "reason": "Optional reason for status change"
    }
    """
    try:
        data = request.get_json()
        new_status = data.get('status')

        valid_statuses = ['new', 'recurring', 'trending', 'resolved', 'ignored']
        if new_status not in valid_statuses:
            return jsonify({'error': f'Invalid status. Must be one of: {valid_statuses}'}), 400

        # Update in Elasticsearch
        from monitoring.timestamp_utils import utc_isoformat

        result = es_client.update_by_query(
            index="medic-docker-failures-*",
            body={
                "script": {
                    "source": "ctx._source.status = params.status; ctx._source.updated_at = params.updated_at;",
                    "params": {
                        "status": new_status,
                        "updated_at": utc_isoformat()
                    }
                },
                "query": {
                    "term": {"fingerprint_id": fingerprint_id}
                }
            }
        )

        if result['updated'] == 0:
            return jsonify({'error': 'Signature not found'}), 404

        return jsonify({
            'fingerprint_id': fingerprint_id,
            'status': new_status,
            'updated': True
        }), 200

    except Exception as e:
        logger.error(f"Failed to update status for {fingerprint_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/medic/stats', methods=['GET'])
def get_medic_stats():
    """Get overall Medic statistics"""
    try:
        result = es_client.search(
            index="medic-docker-failures-*",
            body={
                "size": 0,
                "aggs": {
                    "by_status": {
                        "terms": {"field": "status"}
                    },
                    "by_severity": {
                        "terms": {"field": "severity"}
                    },
                    "total_occurrences": {
                        "sum": {"field": "occurrence_count"}
                    },
                    "occurrences_last_hour": {
                        "sum": {"field": "occurrences_last_hour"}
                    },
                    "occurrences_last_day": {
                        "sum": {"field": "occurrences_last_day"}
                    }
                }
            }
        )

        aggs = result.get('aggregations', {})

        return jsonify({
            'total_signatures': result['hits']['total']['value'],
            'by_status': {bucket['key']: bucket['doc_count'] for bucket in aggs.get('by_status', {}).get('buckets', [])},
            'by_severity': {bucket['key']: bucket['doc_count'] for bucket in aggs.get('by_severity', {}).get('buckets', [])},
            'total_occurrences': int(aggs.get('total_occurrences', {}).get('value', 0)),
            'occurrences_last_hour': int(aggs.get('occurrences_last_hour', {}).get('value', 0)),
            'occurrences_last_day': int(aggs.get('occurrences_last_day', {}).get('value', 0))
        }), 200

    except Exception as e:
        logger.error(f"Failed to get Medic stats: {e}", exc_info=True)
        # Return empty stats instead of error for better UX
        return jsonify({
            'total_signatures': 0,
            'by_status': {},
            'by_severity': {},
            'total_occurrences': 0,
            'occurrences_last_hour': 0,
            'occurrences_last_day': 0
        }), 200


# ========================================
# New Unified Medic API Endpoints (v2.0)
# ========================================

@app.route('/api/medic/docker/failure-signatures', methods=['GET'])
def get_docker_failure_signatures():
    """
    Get Docker failure signatures (explicit endpoint for new architecture).

    This is an alias for /api/medic/failure-signatures but makes it clear
    that it returns Docker container failure data.

    Query params: Same as /api/medic/failure-signatures
    """
    # Reuse the existing implementation
    return get_failure_signatures()


@app.route('/api/medic/docker/failure-signatures/<fingerprint_id>', methods=['GET'])
def get_docker_failure_signature(fingerprint_id):
    """Get detailed Docker failure signature (explicit endpoint)."""
    return get_failure_signature(fingerprint_id)


@app.route('/api/medic/failure-signatures/all', methods=['GET'])
def get_all_failure_signatures():
    """
    Get failure signatures from both Docker and Claude systems (unified view).

    Query params:
    - type: Filter by type (docker, claude, or omit for all)
    - status: Filter by status
    - severity: Filter by severity
    - project: Filter by project (for Claude failures)
    - limit: Max results (default 50)
    - offset: Pagination offset (default 0)
    """
    try:
        type_filter = request.args.get('type', 'all')
        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))

        # Build base query filters
        must_clauses = []

        # Status filter
        if request.args.get('status'):
            must_clauses.append({"term": {"status": request.args['status']}})

        # Severity filter
        if request.args.get('severity'):
            must_clauses.append({"term": {"severity": request.args['severity']}})

        # Project filter (for Claude failures)
        if request.args.get('project'):
            must_clauses.append({"term": {"project": request.args['project']}})

        # Determine which indices to query
        if type_filter == 'docker':
            indices = "medic-docker-failures-*"
        elif type_filter == 'claude':
            indices = "medic-claude-failures-*"
        else:
            # Query both
            indices = "medic-docker-failures-*,medic-claude-failures-*"

        # Build query
        if must_clauses:
            query = {
                "query": {"bool": {"must": must_clauses}},
                "size": limit,
                "from": offset,
                "sort": [{"last_seen": {"order": "desc"}}]
            }
        else:
            query = {
                "query": {"match_all": {}},
                "size": limit,
                "from": offset,
                "sort": [{"last_seen": {"order": "desc"}}]
            }

        # Execute search
        result = es_client.search(index=indices, body=query)

        signatures = [hit['_source'] for hit in result['hits']['hits']]

        return jsonify({
            'signatures': signatures,
            'total': result['hits']['total']['value'],
            'limit': limit,
            'offset': offset,
            'type_filter': type_filter
        }), 200

    except Exception as e:
        logger.error(f"Failed to get all failure signatures: {e}", exc_info=True)
        return jsonify({
            'signatures': [],
            'total': 0,
            'limit': limit,
            'offset': offset,
            'error': str(e)
        }), 200


# ========================================
# Medic Investigation API Endpoints (Phase 2)
# ========================================

@app.route('/api/medic/investigations', methods=['GET'])
def get_investigations():
    """Get list of all investigations with status"""
    try:
        queue = get_investigation_queue()
        report_manager = get_report_manager()

        # Get all investigations from reports
        investigations = report_manager.get_all_investigations_summary()

        # Enhance with Redis status
        for inv in investigations:
            fingerprint_id = inv["fingerprint_id"]
            info = queue.get_investigation_info(fingerprint_id)
            inv.update(info)

        return jsonify({"investigations": investigations, "total": len(investigations)}), 200

    except Exception as e:
        logger.error(f"Failed to get investigations: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/medic/investigations/<fingerprint_id>', methods=['POST'])
def start_investigation(fingerprint_id):
    """Start/queue an investigation for a signature"""
    try:
        queue = get_investigation_queue()

        # Get priority from request
        data = request.get_json() or {}
        priority = data.get("priority", "normal")

        if priority not in ["low", "normal", "high"]:
            return jsonify({"error": "Invalid priority. Must be: low, normal, high"}), 400

        # Enqueue investigation
        enqueued = queue.enqueue(fingerprint_id, priority=priority)

        if enqueued:
            # Update ES investigation status (synchronous with retry logic)
            from services.medic.docker import DockerFailureSignatureStore
            store = DockerFailureSignatureStore(es_client)
            store.update_investigation_status(fingerprint_id, "queued")

            return jsonify({
                "fingerprint_id": fingerprint_id,
                "status": "queued",
                "priority": priority
            }), 202
        else:
            return jsonify({
                "fingerprint_id": fingerprint_id,
                "status": queue.get_status(fingerprint_id),
                "message": "Investigation already queued or in progress"
            }), 200

    except Exception as e:
        logger.error(f"Failed to start investigation: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/medic/investigations/<fingerprint_id>/status', methods=['GET'])
def get_investigation_status(fingerprint_id):
    """Get current status of an investigation"""
    try:
        queue = get_investigation_queue()
        report_manager = get_report_manager()

        # Get info from Redis
        info = queue.get_investigation_info(fingerprint_id)

        # Get report status
        report_status = report_manager.get_report_status(fingerprint_id)
        info.update(report_status)

        return jsonify(info), 200

    except Exception as e:
        logger.error(f"Failed to get investigation status: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/medic/investigations/<fingerprint_id>/diagnosis', methods=['GET'])
def get_diagnosis(fingerprint_id):
    """Get diagnosis report markdown"""
    try:
        report_manager = get_report_manager()

        diagnosis = report_manager.read_diagnosis(fingerprint_id)
        if diagnosis is None:
            return jsonify({"error": "Diagnosis not found"}), 404

        report_status = report_manager.get_report_status(fingerprint_id)

        return jsonify({
            "fingerprint_id": fingerprint_id,
            "content": diagnosis,
            "created_at": report_status.get("has_diagnosis_modified")
        }), 200

    except Exception as e:
        logger.error(f"Failed to get diagnosis: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/medic/investigations/<fingerprint_id>/fix-plan', methods=['GET'])
def get_fix_plan(fingerprint_id):
    """Get fix plan markdown"""
    try:
        report_manager = get_report_manager()

        fix_plan = report_manager.read_fix_plan(fingerprint_id)
        if fix_plan is None:
            return jsonify({"error": "Fix plan not found"}), 404

        report_status = report_manager.get_report_status(fingerprint_id)

        return jsonify({
            "fingerprint_id": fingerprint_id,
            "content": fix_plan,
            "created_at": report_status.get("has_fix_plan_modified")
        }), 200

    except Exception as e:
        logger.error(f"Failed to get fix plan: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/medic/investigations/<fingerprint_id>/ignored', methods=['GET'])
def get_ignored_report(fingerprint_id):
    """Get ignored report markdown"""
    try:
        report_manager = get_report_manager()

        ignored = report_manager.read_ignored(fingerprint_id)
        if ignored is None:
            return jsonify({"error": "Ignored report not found"}), 404

        report_status = report_manager.get_report_status(fingerprint_id)

        return jsonify({
            "fingerprint_id": fingerprint_id,
            "content": ignored,
            "created_at": report_status.get("has_ignored_modified")
        }), 200

    except Exception as e:
        logger.error(f"Failed to get ignored report: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/medic/investigations/<fingerprint_id>/log', methods=['GET'])
def get_investigation_log(fingerprint_id):
    """Get investigation log (agent output)"""
    try:
        report_manager = get_report_manager()

        log_file = report_manager.get_report_dir(fingerprint_id) / "investigation_log.txt"
        if not log_file.exists():
            return jsonify({"error": "Investigation log not found"}), 404

        # Read log file
        with open(log_file, 'r') as f:
            content = f.read()

        # Get line count
        line_count = report_manager.count_log_lines(fingerprint_id)

        return jsonify({
            "fingerprint_id": fingerprint_id,
            "content": content,
            "line_count": line_count
        }), 200

    except Exception as e:
        logger.error(f"Failed to get investigation log: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ============================================================================
# Claude Medic API Endpoints (Tool Execution Failure Monitoring)
# ============================================================================

@app.route('/api/medic/claude/failure-signatures', methods=['GET'])
def get_claude_failure_signatures():
    """
    Get all Claude tool execution failure signatures with filtering and pagination.

    Query params:
    - project: Filter by project name
    - tool_name: Filter by tool name (Read, Bash, Edit, etc.)
    - error_type: Filter by error type
    - status: Filter by status (new, recurring, trending, resolved)
    - investigation_status: Filter by investigation status
    - from_date: Filter by first_seen >= date (ISO format)
    - to_date: Filter by first_seen <= date (ISO format)
    - limit: Max results (default 50)
    - offset: Pagination offset (default 0)
    """
    try:
        # Build Elasticsearch query
        must_clauses = [{"term": {"type": "claude_failure"}}]  # Only Claude failures

        # Project filter
        if request.args.get('project'):
            must_clauses.append({"term": {"project": request.args['project']}})

        # Tool name filter
        if request.args.get('tool_name'):
            must_clauses.append({"term": {"signature.tool_name": request.args['tool_name']}})

        # Error type filter
        if request.args.get('error_type'):
            must_clauses.append({"term": {"signature.error_type": request.args['error_type']}})

        # Status filter
        if request.args.get('status'):
            must_clauses.append({"term": {"status": request.args['status']}})

        # Investigation status filter
        if request.args.get('investigation_status'):
            must_clauses.append({"term": {"investigation_status": request.args['investigation_status']}})

        # Date range filter
        date_filter = {}
        if request.args.get('from_date'):
            date_filter['gte'] = request.args['from_date']
        if request.args.get('to_date'):
            date_filter['lte'] = request.args['to_date']
        if date_filter:
            must_clauses.append({"range": {"first_seen": date_filter}})

        # Build query
        query = {
            "query": {
                "bool": {"must": must_clauses}
            },
            "size": int(request.args.get('limit', 50)),
            "from": int(request.args.get('offset', 0)),
            "sort": [{"last_seen": {"order": "desc"}}]
        }

        # Execute search
        result = es_client.search(index="medic-claude-failures-*", body=query)

        signatures = [hit['_source'] for hit in result['hits']['hits']]

        # Enrich with real-time Redis status (source of truth)
        signatures = _enrich_signatures_with_redis_status(signatures, system_type="claude")

        return jsonify({
            'signatures': signatures,
            'total': result['hits']['total']['value'],
            'limit': int(request.args.get('limit', 50)),
            'offset': int(request.args.get('offset', 0))
        }), 200

    except Exception as e:
        logger.error(f"Failed to get Claude failure signatures: {e}", exc_info=True)
        # Return empty result instead of error for better UX when index doesn't exist
        return jsonify({
            'signatures': [],
            'total': 0,
            'limit': int(request.args.get('limit', 50)),
            'offset': int(request.args.get('offset', 0))
        }), 200


@app.route('/api/medic/claude/failure-signatures/<fingerprint_id>', methods=['GET'])
def get_claude_failure_signature(fingerprint_id):
    """Get details of a specific Claude failure signature"""
    try:
        result = es_client.search(
            index="medic-claude-failures-*",
            body={
                "query": {"term": {"fingerprint_id": fingerprint_id}},
                "size": 1
            }
        )

        if not result['hits']['hits']:
            return jsonify({"error": "Signature not found"}), 404

        signature = result['hits']['hits'][0]['_source']
        return jsonify(signature), 200

    except Exception as e:
        logger.error(f"Failed to get Claude failure signature: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/medic/claude/failure-signatures/<fingerprint_id>/clusters', methods=['GET'])
def get_claude_failure_clusters(fingerprint_id):
    """Get sample clusters for a Claude failure signature"""
    try:
        result = es_client.search(
            index="medic-claude-failures-*",
            body={
                "query": {"term": {"fingerprint_id": fingerprint_id}},
                "size": 1
            }
        )

        if not result['hits']['hits']:
            return jsonify({"error": "Signature not found"}), 404

        signature = result['hits']['hits'][0]['_source']
        sample_clusters = signature.get('sample_clusters', [])

        return jsonify({
            'fingerprint_id': fingerprint_id,
            'clusters': sample_clusters,
            'total_clusters': signature.get('cluster_count', 0),
            'sample_count': len(sample_clusters)
        }), 200

    except Exception as e:
        logger.error(f"Failed to get Claude failure clusters: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/medic/claude/stats', methods=['GET'])
def get_claude_medic_stats():
    """Get Claude Medic statistics"""
    try:
        # Get signature counts by status
        status_agg = es_client.search(
            index="medic-claude-failures-*",
            body={
                "query": {"term": {"type": "claude_failure"}},
                "size": 0,
                "aggs": {
                    "by_status": {
                        "terms": {"field": "status"}
                    },
                    "by_tool": {
                        "terms": {"field": "signature.tool_name", "size": 20}
                    },
                    "by_error_type": {
                        "terms": {"field": "signature.error_type", "size": 20}
                    },
                    "by_project": {
                        "terms": {"field": "project", "size": 50}
                    }
                }
            }
        )

        # Extract aggregations
        status_counts = {
            bucket['key']: bucket['doc_count']
            for bucket in status_agg.get('aggregations', {}).get('by_status', {}).get('buckets', [])
        }

        tool_counts = {
            bucket['key']: bucket['doc_count']
            for bucket in status_agg.get('aggregations', {}).get('by_tool', {}).get('buckets', [])
        }

        error_type_counts = {
            bucket['key']: bucket['doc_count']
            for bucket in status_agg.get('aggregations', {}).get('by_error_type', {}).get('buckets', [])
        }

        project_counts = {
            bucket['key']: bucket['doc_count']
            for bucket in status_agg.get('aggregations', {}).get('by_project', {}).get('buckets', [])
        }

        # Get total failures (sum of total_failures across all signatures)
        total_result = es_client.search(
            index="medic-claude-failures-*",
            body={
                "query": {"term": {"type": "claude_failure"}},
                "size": 0,
                "aggs": {
                    "total_failures": {"sum": {"field": "total_failures"}},
                    "total_clusters": {"sum": {"field": "cluster_count"}}
                }
            }
        )

        total_failures = int(total_result.get('aggregations', {}).get('total_failures', {}).get('value', 0))
        total_clusters = int(total_result.get('aggregations', {}).get('total_clusters', {}).get('value', 0))

        # Get under investigation count from Redis active sets (both Docker and Claude)
        redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
        docker_active = redis_client.scard("medic:docker_investigation:active") or 0
        claude_active = redis_client.scard("medic:claude_investigation:active") or 0
        under_investigation = docker_active + claude_active

        return jsonify({
            'total_signatures': sum(status_counts.values()),
            'total_failures': total_failures,
            'total_clusters': total_clusters,
            'under_investigation': under_investigation,
            'docker_investigations_active': docker_active,
            'claude_investigations_active': claude_active,
            'by_status': status_counts,
            'by_tool': tool_counts,
            'by_error_type': error_type_counts,
            'by_project': project_counts
        }), 200

    except Exception as e:
        logger.error(f"Failed to get Claude medic stats: {e}", exc_info=True)
        return jsonify({
            'total_signatures': 0,
            'total_failures': 0,
            'total_clusters': 0,
            'by_status': {},
            'by_tool': {},
            'by_error_type': {},
            'by_project': {}
        }), 200


@app.route('/api/medic/claude/projects', methods=['GET'])
def get_claude_medic_projects():
    """Get list of projects with Claude failure data"""
    try:
        result = es_client.search(
            index="medic-claude-failures-*",
            body={
                "query": {"term": {"type": "claude_failure"}},
                "size": 0,
                "aggs": {
                    "projects": {
                        "terms": {"field": "project", "size": 100},
                        "aggs": {
                            "signature_count": {"value_count": {"field": "fingerprint_id"}},
                            "total_failures": {"sum": {"field": "total_failures"}},
                            "total_clusters": {"sum": {"field": "cluster_count"}},
                            "last_seen": {"max": {"field": "last_seen"}}
                        }
                    }
                }
            }
        )

        projects = []
        for bucket in result.get('aggregations', {}).get('projects', {}).get('buckets', []):
            projects.append({
                'project': bucket['key'],
                'signature_count': bucket['doc_count'],
                'total_failures': int(bucket['total_failures']['value']),
                'total_clusters': int(bucket['total_clusters']['value']),
                'last_seen': bucket['last_seen']['value_as_string']
            })

        return jsonify({'projects': projects}), 200

    except Exception as e:
        logger.error(f"Failed to get Claude medic projects: {e}", exc_info=True)
        return jsonify({'projects': []}), 200


@app.route('/api/medic/claude/projects/<project>/stats', methods=['GET'])
def get_claude_project_stats(project):
    """Get Claude Medic statistics for a specific project"""
    try:
        # Get signature counts by status for this project
        result = es_client.search(
            index="medic-claude-failures-*",
            body={
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"type": "claude_failure"}},
                            {"term": {"project": project}}
                        ]
                    }
                },
                "size": 0,
                "aggs": {
                    "by_status": {"terms": {"field": "status"}},
                    "by_tool": {"terms": {"field": "signature.tool_name", "size": 20}},
                    "by_error_type": {"terms": {"field": "signature.error_type", "size": 20}},
                    "total_failures": {"sum": {"field": "total_failures"}},
                    "total_clusters": {"sum": {"field": "cluster_count"}}
                }
            }
        )

        aggs = result.get('aggregations', {})

        status_counts = {
            bucket['key']: bucket['doc_count']
            for bucket in aggs.get('by_status', {}).get('buckets', [])
        }

        tool_counts = {
            bucket['key']: bucket['doc_count']
            for bucket in aggs.get('by_tool', {}).get('buckets', [])
        }

        error_type_counts = {
            bucket['key']: bucket['doc_count']
            for bucket in aggs.get('by_error_type', {}).get('buckets', [])
        }

        return jsonify({
            'project': project,
            'total_signatures': sum(status_counts.values()),
            'total_failures': int(aggs.get('total_failures', {}).get('value', 0)),
            'total_clusters': int(aggs.get('total_clusters', {}).get('value', 0)),
            'by_status': status_counts,
            'by_tool': tool_counts,
            'by_error_type': error_type_counts
        }), 200

    except Exception as e:
        logger.error(f"Failed to get Claude project stats: {e}", exc_info=True)
        return jsonify({
            'project': project,
            'total_signatures': 0,
            'total_failures': 0,
            'total_clusters': 0,
            'by_status': {},
            'by_tool': {},
            'by_error_type': {}
        }), 200


# ============================================================================
# Claude Investigation API Endpoints
# ============================================================================

@app.route('/api/medic/claude/investigations', methods=['GET'])
def get_claude_investigations():
    """
    Get all Claude investigations with status and progress.

    Query params:
    - status: Filter by status (queued, in_progress, completed, failed, ignored)
    - project: Filter by project
    - limit: Max results (default 50)
    - offset: Pagination offset (default 0)
    """
    try:
        from services.medic.claude import ClaudeReportManager

        report_manager = ClaudeReportManager()
        all_investigations = report_manager.list_all_investigations()

        # Apply filters
        status_filter = request.args.get('status')
        project_filter = request.args.get('project')
        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))

        filtered_investigations = []
        for fingerprint_id in all_investigations:
            summary = report_manager.get_investigation_summary(fingerprint_id)

            # Apply status filter
            if status_filter and summary.get('status') != status_filter:
                continue

            # Apply project filter
            if project_filter and summary.get('project') != project_filter:
                continue

            filtered_investigations.append(summary)

        # Sort by created_at desc
        filtered_investigations.sort(
            key=lambda x: x.get('created_at', ''),
            reverse=True
        )

        # Apply pagination
        paginated = filtered_investigations[offset:offset + limit]

        return jsonify({
            'investigations': paginated,
            'total': len(filtered_investigations),
            'limit': limit,
            'offset': offset
        }), 200

    except Exception as e:
        logger.error(f"Failed to get Claude investigations: {e}", exc_info=True)
        return jsonify({
            'investigations': [],
            'total': 0,
            'limit': 50,
            'offset': 0
        }), 200


@app.route('/api/medic/claude/investigations/<fingerprint_id>', methods=['POST'])
def trigger_claude_investigation(fingerprint_id):
    """Manually trigger a Claude investigation for a signature"""
    try:
        priority = request.json.get('priority', 'normal') if request.json else 'normal'

        # Enqueue investigation via Redis
        import redis
        redis_client = redis.Redis(
            host=os.getenv('REDIS_HOST', 'redis'),
            port=int(os.getenv('REDIS_PORT', 6379)),
            decode_responses=True
        )

        from services.medic.claude import ClaudeInvestigationQueue
        from services.medic.claude import ClaudeFailureSignatureStore

        queue = ClaudeInvestigationQueue(redis_client)

        enqueued = queue.enqueue(fingerprint_id, priority=priority)

        if enqueued:
            # Update Elasticsearch signature document
            signature_store = ClaudeFailureSignatureStore(es_client)
            signature_store.update_investigation_status(fingerprint_id, 'queued')

            return jsonify({
                'message': f'Investigation triggered for {fingerprint_id}',
                'fingerprint_id': fingerprint_id,
                'priority': priority
            }), 200
        else:
            return jsonify({
                'error': 'Investigation already queued or in progress',
                'fingerprint_id': fingerprint_id
            }), 409

    except Exception as e:
        logger.error(f"Failed to trigger Claude investigation: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/medic/claude/investigations/<fingerprint_id>/status', methods=['GET'])
def get_claude_investigation_status(fingerprint_id):
    """Get status and progress of a Claude investigation"""
    try:
        import redis
        redis_client = redis.Redis(
            host=os.getenv('REDIS_HOST', 'redis'),
            port=int(os.getenv('REDIS_PORT', 6379)),
            decode_responses=True
        )

        from services.medic.claude import ClaudeInvestigationQueue
        from services.medic.claude import ClaudeReportManager

        queue = ClaudeInvestigationQueue(redis_client)
        report_manager = ClaudeReportManager()

        # Get queue info
        queue_info = queue.get_investigation_info(fingerprint_id)

        # Get report info
        report_summary = report_manager.get_investigation_summary(fingerprint_id)

        # Merge information
        status_info = {
            'fingerprint_id': fingerprint_id,
            'queue_status': queue_info.get('status'),
            'report_status': report_summary.get('status'),
            'started_at': queue_info.get('started_at'),
            'last_heartbeat': queue_info.get('last_heartbeat'),
            'completed_at': queue_info.get('completed_at'),
            'result': queue_info.get('result'),
            'progress_lines': queue_info.get('progress_lines', 0),
            'duration_seconds': queue_info.get('duration_seconds'),
            'has_diagnosis': report_summary.get('has_diagnosis', False),
            'has_fix_plan': report_summary.get('has_fix_plan', False),
            'has_ignored': report_summary.get('has_ignored', False),
            'project': report_summary.get('project'),
        }

        return jsonify(status_info), 200

    except Exception as e:
        logger.error(f"Failed to get Claude investigation status: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/medic/claude/investigations/<fingerprint_id>/diagnosis', methods=['GET'])
def get_claude_diagnosis(fingerprint_id):
    """Get diagnosis.md content"""
    try:
        from services.medic.claude import ClaudeReportManager

        report_manager = ClaudeReportManager()
        diagnosis = report_manager.read_diagnosis(fingerprint_id)

        if not diagnosis:
            return jsonify({'error': 'Diagnosis not found'}), 404

        return jsonify({
            'fingerprint_id': fingerprint_id,
            'content': diagnosis
        }), 200

    except Exception as e:
        logger.error(f"Failed to get Claude diagnosis: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/medic/claude/investigations/<fingerprint_id>/fix-plan', methods=['GET'])
def get_claude_fix_plan(fingerprint_id):
    """Get fix_plan.md content"""
    try:
        from services.medic.claude import ClaudeReportManager

        report_manager = ClaudeReportManager()
        fix_plan = report_manager.read_fix_plan(fingerprint_id)

        if not fix_plan:
            return jsonify({'error': 'Fix plan not found'}), 404

        return jsonify({
            'fingerprint_id': fingerprint_id,
            'content': fix_plan
        }), 200

    except Exception as e:
        logger.error(f"Failed to get Claude fix plan: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/medic/claude/investigations/<fingerprint_id>/recommendations', methods=['GET'])
def get_claude_recommendations(fingerprint_id):
    """Get recommendations (alias for fix_plan.md)"""
    try:
        from services.medic.claude import ClaudeReportManager

        report_manager = ClaudeReportManager()
        recommendations = report_manager.read_fix_plan(fingerprint_id)

        if not recommendations:
            return jsonify({'error': 'Recommendations not found'}), 404

        # Get file modification timestamp
        import os
        from pathlib import Path
        fix_plan_file = report_manager.get_report_dir(fingerprint_id) / "fix_plan.md"
        created_at = None
        if fix_plan_file.exists():
            from datetime import datetime, timezone
            created_at = datetime.fromtimestamp(
                fix_plan_file.stat().st_mtime, tz=timezone.utc
            ).isoformat()

        return jsonify({
            'fingerprint_id': fingerprint_id,
            'content': recommendations,
            'created_at': created_at
        }), 200

    except Exception as e:
        logger.error(f"Failed to get Claude recommendations: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/medic/claude/investigations/<fingerprint_id>/ignored', methods=['GET'])
def get_claude_ignored(fingerprint_id):
    """Get ignored.md content"""
    try:
        from services.medic.claude import ClaudeReportManager

        report_manager = ClaudeReportManager()
        ignored = report_manager.read_ignored(fingerprint_id)

        if not ignored:
            return jsonify({'error': 'Ignored report not found'}), 404

        return jsonify({
            'fingerprint_id': fingerprint_id,
            'content': ignored
        }), 200

    except Exception as e:
        logger.error(f"Failed to get Claude ignored report: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/medic/claude/investigations/<fingerprint_id>/log', methods=['GET'])
def get_claude_investigation_log(fingerprint_id):
    """Get investigation log (agent output)"""
    try:
        from services.medic.claude import ClaudeReportManager

        report_manager = ClaudeReportManager()

        log_file = report_manager.get_report_dir(fingerprint_id) / "investigation_log.txt"
        if not log_file.exists():
            return jsonify({'error': 'Investigation log not found'}), 404

        # Read log file
        with open(log_file, 'r') as f:
            content = f.read()

        # Get line count
        line_count = report_manager.count_log_lines(fingerprint_id)

        return jsonify({
            'fingerprint_id': fingerprint_id,
            'content': content,
            'line_count': line_count
        }), 200

    except Exception as e:
        logger.error(f"Failed to get Claude investigation log: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/medic/claude/clusters/<cluster_id>/failures', methods=['GET'])
def get_cluster_failure_details(cluster_id):
    """Get detailed failure events for a specific cluster"""
    try:
        # Parse cluster_id to extract session_id and timestamp
        # Format: cluster_{project}_{session_id}_{timestamp}
        # Session ID is a UUID format (8-4-4-4-12 hex digits separated by dashes)
        # Timestamp is ISO format with 'T'

        # Find the session ID pattern (UUID: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)
        import re
        uuid_pattern = r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'
        match = re.search(uuid_pattern, cluster_id)

        if not match:
            return jsonify({"error": "Could not extract session_id from cluster_id"}), 400

        session_id = match.group(1)

        # Query Elasticsearch for tool_result events with failures in this session
        result = es_client.search(
            index="claude-streams-*",
            body={
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"raw_event.event.session_id.keyword": session_id}},
                            {"term": {"event_category": "tool_result"}},
                            {"term": {"success": False}}
                        ]
                    }
                },
                "sort": [{"timestamp": {"order": "asc"}}],
                "size": 100
            }
        )

        failures = []
        for hit in result['hits']['hits']:
            source = hit['_source']
            failures.append({
                "timestamp": source.get("timestamp"),
                "tool_name": source.get("tool_name"),
                "tool_params": source.get("tool_params", {}),
                "success": source.get("success"),
                "error_message": source.get("error_message", ""),
                "raw_event": source.get("raw_event", {})
            })

        return jsonify({
            "cluster_id": cluster_id,
            "session_id": session_id,
            "failures": failures,
            "count": len(failures)
        }), 200

    except Exception as e:
        logger.error(f"Failed to get cluster failure details: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ============================================================================
# Claude Fix Execution API Endpoints
# ============================================================================

@app.route('/api/medic/claude/fixes/<fingerprint_id>', methods=['POST'])
def trigger_claude_fix(fingerprint_id):
    """
    Trigger a fix execution for a failure signature.
    Requires a fix_plan.md to exist.
    """
    try:
        from services.fix_orchestrator.fix_execution_queue import ClaudeFixExecutionQueue
        from services.medic.claude import ClaudeReportManager
        from services.medic.docker import DockerReportManager
        import redis

        # Check if fix plan exists in Claude reports
        claude_report_manager = ClaudeReportManager()
        fix_plan = claude_report_manager.read_fix_plan(fingerprint_id)

        report_manager = claude_report_manager
        is_claude_report = True

        if not fix_plan:
            # Check Docker reports
            std_report_manager = DockerReportManager()
            fix_plan = std_report_manager.read_fix_plan(fingerprint_id)
            if fix_plan:
                report_manager = std_report_manager
                is_claude_report = False
        
        if not fix_plan:
            return jsonify({
                'error': 'Fix plan not found. Please run an investigation first.',
                'fingerprint_id': fingerprint_id
            }), 400
            
        # Get project from report summary
        if is_claude_report:
            summary = report_manager.get_investigation_summary(fingerprint_id)
            project = summary.get('project', 'unknown')
        else:
            # For standard reports, we need to get project from context
            context = report_manager.read_context(fingerprint_id)
            # Try to extract project from signature or sample logs if not explicit
            project = 'unknown'
            if context:
                # Try to find project in sample logs
                sample_logs = context.get('sample_logs', [])
                if sample_logs and len(sample_logs) > 0:
                    # Check first log for project context
                    first_log = sample_logs[0]
                    if isinstance(first_log, dict):
                        context_data = first_log.get('context', {})
                        if isinstance(context_data, dict):
                            project = context_data.get('project', 'unknown')
        
        # Enqueue fix
        redis_client = redis.Redis(
            host=os.getenv('REDIS_HOST', 'redis'),
            port=int(os.getenv('REDIS_PORT', 6379)),
            decode_responses=True
        )
        
        queue = ClaudeFixExecutionQueue(redis_client)
        fix_plan_path = str(report_manager.get_report_dir(fingerprint_id) / "fix_plan.md")
        
        enqueued = queue.enqueue(
            fingerprint_id=fingerprint_id,
            project=project,
            fix_plan_path=fix_plan_path
        )
        
        if enqueued:
            return jsonify({
                'message': f'Fix execution triggered for {fingerprint_id}',
                'fingerprint_id': fingerprint_id,
                'status': 'queued'
            }), 200
        else:
            return jsonify({
                'error': 'Fix execution already queued or in progress',
                'fingerprint_id': fingerprint_id,
                'status': queue.get_status(fingerprint_id)
            }), 409
            
    except Exception as e:
        logger.error(f"Failed to trigger Claude fix: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/medic/claude/fixes/<fingerprint_id>/status', methods=['GET'])
def get_claude_fix_status(fingerprint_id):
    """Get status of a fix execution"""
    try:
        from services.fix_orchestrator.fix_execution_queue import ClaudeFixExecutionQueue
        import redis

        redis_client = redis.Redis(
            host=os.getenv('REDIS_HOST', 'redis'),
            port=int(os.getenv('REDIS_PORT', 6379)),
            decode_responses=True
        )

        queue = ClaudeFixExecutionQueue(redis_client)
        status_info = queue.get_fix_info(fingerprint_id)

        # Try to get agent_execution_id from active fix info in Redis
        try:
            active_fix = redis_client.hgetall(f"fix:active:{fingerprint_id}")
            if active_fix and 'agent_execution_id' in active_fix:
                status_info['agent_execution_id'] = active_fix['agent_execution_id']
        except Exception as e:
            logger.warning(f"Failed to fetch agent_execution_id from Redis: {e}")

        redis_client.close()
        return jsonify(status_info), 200

    except Exception as e:
        logger.error(f"Failed to get Claude fix status: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/medic/claude/fixes/<fingerprint_id>/log', methods=['GET'])
def get_claude_fix_log(fingerprint_id):
    """Get fix execution log"""
    try:
        from services.fix_orchestrator.fix_execution_queue import ClaudeFixExecutionQueue
        import redis
        import json
        
        redis_client = redis.Redis(
            host=os.getenv('REDIS_HOST', 'redis'),
            port=int(os.getenv('REDIS_PORT', 6379)),
            decode_responses=True
        )
        
        queue = ClaudeFixExecutionQueue(redis_client)
        info = queue.get_fix_info(fingerprint_id)
        
        log_file_path = info.get('log_file')
        
        if not log_file_path or not os.path.exists(log_file_path):
            return jsonify({'error': 'Log file not found'}), 404
            
        # Read log file (JSONL format)
        logs = []
        with open(log_file_path, 'r') as f:
            for line in f:
                try:
                    if line.strip():
                        logs.append(json.loads(line))
                except:
                    pass
                    
        return jsonify({
            'fingerprint_id': fingerprint_id,
            'logs': logs,
            'count': len(logs)
        }), 200
        
    except Exception as e:
        logger.error(f"Failed to get Claude fix log: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


# ============================================================================
# Fix Orchestrator Control API Endpoints
# ============================================================================

@app.route('/fix-orchestrator/active', methods=['GET'])
def get_active_fixes():
    """Get all currently active and queued fixes."""
    try:
        import redis
        redis_client = redis.Redis(
            host=os.getenv('REDIS_HOST', 'redis'),
            port=int(os.getenv('REDIS_PORT', 6379)),
            decode_responses=True
        )

        all_fixes = []

        # 1. Get queued fixes from the queue
        queued_fingerprints = redis_client.lrange("medic:claude_fix:queue", 0, -1)
        for fingerprint_id in queued_fingerprints:
            status = redis_client.get(f"medic:claude_fix:{fingerprint_id}:status")
            project = redis_client.get(f"medic:claude_fix:{fingerprint_id}:project")
            # Try multiple timestamp fields
            queued_at = (redis_client.get(f"medic:claude_fix:{fingerprint_id}:queued_at") or
                        redis_client.get(f"medic:claude_fix:{fingerprint_id}:started_at"))

            all_fixes.append({
                "fingerprint_id": fingerprint_id,
                "status": status or "queued",
                "started_at": queued_at,
                "log_file": None,
                "container_name": None,
                "project": project
            })

        # 2. Get active/running fixes from fix:active:* keys
        keys = redis_client.keys("fix:active:*")
        for key in keys:
            fix_data = redis_client.hgetall(key)
            all_fixes.append({
                "fingerprint_id": fix_data.get("fingerprint_id"),
                "status": fix_data.get("status"),
                "started_at": fix_data.get("started_at"),
                "log_file": fix_data.get("log_file"),
                "container_name": fix_data.get("container_name"),
                "project": fix_data.get("project")
            })

        redis_client.close()
        return jsonify(all_fixes), 200
    except Exception as e:
        logger.error(f"Failed to get active fixes: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/fix-orchestrator/status/<fingerprint_id>', methods=['GET'])
def get_fix_status(fingerprint_id):
    """Get detailed status for a specific fix."""
    try:
        import redis
        redis_client = redis.Redis(
            host=os.getenv('REDIS_HOST', 'redis'),
            port=int(os.getenv('REDIS_PORT', 6379)),
            decode_responses=True
        )

        # Check active state
        active_data = redis_client.hgetall(f"fix:active:{fingerprint_id}")

        # Check final status
        status_data = redis_client.hgetall(f"medic:fix:execution:status:{fingerprint_id}")

        redis_client.close()

        return jsonify({
            "fingerprint_id": fingerprint_id,
            "active": bool(active_data),
            "active_data": active_data or None,
            "status_data": status_data or None
        }), 200
    except Exception as e:
        logger.error(f"Failed to get fix status: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/medic/investigations/active', methods=['GET'])
def get_active_investigations():
    """Get all queued and in-progress investigations with container health status."""
    try:
        import redis
        redis_client = redis.Redis(
            host=os.getenv('REDIS_HOST', 'redis'),
            port=int(os.getenv('REDIS_PORT', 6379)),
            decode_responses=True
        )

        investigations = []

        # Process both Docker and Claude investigations
        for prefix in ["medic:docker_investigation", "medic:claude_investigation"]:
            investigation_type = "docker" if "docker" in prefix else "claude"

            # Get queued investigations
            queued = redis_client.lrange(f"{prefix}:queue", 0, -1)

            # Get active investigations
            active_fps = redis_client.smembers(f"{prefix}:active")

            # Add queued
            for fp_id in queued:
                investigations.append({
                    "fingerprint_id": fp_id,
                    "type": investigation_type,
                    "status": "queued",
                    "container_name": None,
                    "container_running": False,
                    "health": "queued",
                    "started_at": None,
                    "last_heartbeat": None
                })

            # Add active
            for fp_id in active_fps:
                status = redis_client.get(f"{prefix}:{fp_id}:status") or "in_progress"
                container_name = redis_client.get(f"{prefix}:{fp_id}:container_name")
                started_at = redis_client.get(f"{prefix}:{fp_id}:started_at")
                last_heartbeat = redis_client.get(f"{prefix}:{fp_id}:last_heartbeat")
                agent_execution_id = redis_client.get(f"{prefix}:{fp_id}:agent_execution_id")

                # Verify container is actually running
                container_running = check_container_running(container_name) if container_name else False

                # Determine health status
                if status == "in_progress" and container_running:
                    health = "healthy"
                elif status == "in_progress" and not container_running:
                    health = "stale"
                else:
                    health = status

                investigations.append({
                    "fingerprint_id": fp_id,
                    "type": investigation_type,
                    "status": status,
                    "container_name": container_name,
                    "container_running": container_running,
                    "health": health,
                    "started_at": started_at,
                    "last_heartbeat": last_heartbeat,
                    "agent_execution_id": agent_execution_id
                })

        redis_client.close()

        # Calculate summary stats
        total = len(investigations)
        healthy = sum(1 for inv in investigations if inv['health'] == 'healthy')
        stale = sum(1 for inv in investigations if inv['health'] == 'stale')
        queued = sum(1 for inv in investigations if inv['status'] == 'queued')

        return jsonify({
            "investigations": investigations,
            "summary": {
                "total": total,
                "healthy": healthy,
                "stale": stale,
                "queued": queued
            }
        }), 200
    except Exception as e:
        logger.error(f"Failed to get active investigations: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/fix-orchestrator/kill/<fingerprint_id>', methods=['POST'])
def kill_fix(fingerprint_id):
    """Force-kill a running fix execution."""
    try:
        import redis
        redis_client = redis.Redis(
            host=os.getenv('REDIS_HOST', 'redis'),
            port=int(os.getenv('REDIS_PORT', 6379)),
            decode_responses=True
        )

        # Signal fix orchestrator to kill this fix
        redis_client.setex(f"fix:kill:{fingerprint_id}", 60, "kill_requested")

        redis_client.close()

        return jsonify({
            "success": True,
            "message": f"Kill signal sent for {fingerprint_id}"
        }), 200
    except Exception as e:
        logger.error(f"Failed to send kill signal: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/fix-orchestrator/health', methods=['GET'])
def fix_orchestrator_health():
    """Check fix orchestrator service health."""
    try:
        import redis
        redis_client = redis.Redis(
            host=os.getenv('REDIS_HOST', 'redis'),
            port=int(os.getenv('REDIS_PORT', 6379)),
            decode_responses=True
        )

        keys = redis_client.keys("fix:active:*")
        queue_length = redis_client.llen("medic:fix:execution:queue")

        redis_client.close()

        return jsonify({
            "healthy": True,
            "service": "fix-orchestrator",
            "active_fixes": len(keys),
            "queue_length": queue_length
        }), 200
    except Exception as e:
        logger.error(f"Fix orchestrator health check failed: {e}", exc_info=True)
        return jsonify({"healthy": False, "error": str(e)}), 500


# ============================================================================
# Claude Advisor API Endpoints
# ============================================================================

# Track active advisor runs in memory
# Format: {project_name: {started_at: iso_timestamp, status: 'running'}}
active_advisor_runs = {}

@app.route('/api/medic/claude/advisor/active', methods=['GET'])
def get_active_advisor_runs():
    """Get list of currently running advisor sessions"""
    return jsonify({
        'runs': [
            {'project': k, **v} 
            for k, v in active_advisor_runs.items()
        ]
    }), 200

@app.route('/api/medic/claude/advisor/reports', methods=['GET'])
def get_all_advisor_reports():
    """List recent advisor reports across all projects"""
    try:
        base_dir = Path("/workspace/orchestrator_data/medic/advisor_reports")
        if not base_dir.exists():
            return jsonify({'reports': []}), 200
            
        reports = []
        # Walk through all project directories
        for project_dir in base_dir.iterdir():
            if project_dir.is_dir():
                project_name = project_dir.name
                for f in project_dir.glob("advisor_report_*.md"):
                    stats = f.stat()
                    reports.append({
                        'project': project_name,
                        'filename': f.name,
                        'path': str(f),
                        'created_at': datetime.fromtimestamp(stats.st_mtime).isoformat(),
                        'size': stats.st_size
                    })
        
        # Sort by date desc
        reports.sort(key=lambda x: x['created_at'], reverse=True)
        
        # Limit to recent 50
        return jsonify({'reports': reports[:50]}), 200
        
    except Exception as e:
        logger.error(f"Failed to list all advisor reports: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/api/medic/claude/advisor/projects/<project>/run', methods=['POST'])
def run_claude_advisor(project):
    """Trigger Claude Advisor for a project"""
    try:
        if project in active_advisor_runs:
             return jsonify({
                'message': f'Advisor already running for {project}',
                'status': 'running'
            }), 409

        # Run in background thread to not block
        def run_advisor_bg():
            try:
                import asyncio
                from services.medic.claude import ClaudeAdvisorOrchestrator
                
                orchestrator = ClaudeAdvisorOrchestrator()
                asyncio.run(orchestrator.run_for_project(project))
            except Exception as e:
                logger.error(f"Advisor run failed for {project}: {e}", exc_info=True)
            finally:
                # Remove from active runs when done
                if project in active_advisor_runs:
                    del active_advisor_runs[project]
            
        # Mark as active
        active_advisor_runs[project] = {
            'started_at': datetime.utcnow().isoformat() + 'Z',
            'status': 'running'
        }

        thread = threading.Thread(target=run_advisor_bg)
        thread.start()
        
        return jsonify({
            'message': f'Claude Advisor triggered for project {project}',
            'status': 'started'
        }), 200
        
    except Exception as e:
        logger.error(f"Failed to trigger Claude Advisor: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/api/medic/claude/advisor/projects/<project>/reports', methods=['GET'])
def get_claude_advisor_reports(project):
    """List advisor reports for a project"""
    try:
        reports_dir = Path("/workspace/orchestrator_data/medic/advisor_reports") / project
        
        if not reports_dir.exists():
            return jsonify({'reports': []}), 200
            
        reports = []
        for f in reports_dir.glob("advisor_report_*.md"):
            stats = f.stat()
            reports.append({
                'filename': f.name,
                'path': str(f),
                'created_at': datetime.fromtimestamp(stats.st_mtime).isoformat(),
                'size': stats.st_size
            })
            
        # Sort by date desc
        reports.sort(key=lambda x: x['created_at'], reverse=True)
        
        return jsonify({'reports': reports}), 200
        
    except Exception as e:
        logger.error(f"Failed to list advisor reports: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/api/medic/claude/advisor/projects/<project>/reports/<filename>', methods=['GET'])
def get_claude_advisor_report_content(project, filename):
    """Get content of a specific report"""
    try:
        reports_dir = Path("/workspace/orchestrator_data/medic/advisor_reports") / project
        file_path = reports_dir / filename
        
        if not file_path.exists():
            return jsonify({'error': 'Report not found'}), 404
            
        with open(file_path, 'r') as f:
            content = f.read()
            
        return jsonify({
            'content': content,
            'filename': filename
        }), 200
        
    except Exception as e:
        logger.error(f"Failed to get advisor report content: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


def start_observability_server(host='0.0.0.0', port=5001):
    """Start the observability WebSocket server"""
    # Start Redis subscriber in background thread
    subscriber = threading.Thread(target=redis_subscriber_thread, daemon=True)
    subscriber.start()

    # Start git branch collector in background thread
    git_collector = threading.Thread(target=git_branch_collector_thread, daemon=True)
    git_collector.start()

    logger.info(f"Starting observability server on {host}:{port} with eventlet backend")
    socketio.run(app, host=host, port=port, debug=False)

if __name__ == '__main__':
    start_observability_server()
