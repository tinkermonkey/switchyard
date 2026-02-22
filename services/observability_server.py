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

        # Try to get project/issue from Redis before killing, so we can set cancellation signal
        project = None
        issue_number = None
        try:
            redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
            data = redis_client.hgetall(f'agent:container:{container_name}')
            if data:
                project = data.get('project')
                issue_str = data.get('issue_number', '')
                if issue_str and issue_str != 'unknown':
                    issue_number = int(issue_str)
        except Exception as e:
            logger.warning(f"Could not read container metadata from Redis: {e}")

        # If we have project/issue, use full cancellation flow
        if project and issue_number:
            from services.cancellation import cancel_issue_work
            cancel_issue_work(project, issue_number, f"Agent killed via Web UI (container: {container_name})")

            return jsonify({
                'success': True,
                'message': f'Container {container_name} stopped and work cancelled for {project}/#{issue_number}',
                'container_name': container_name,
                'project': project,
                'issue_number': issue_number
            }), 200

        # Fallback: kill just this container (no project/issue context)
        result = subprocess.run(
            ['docker', 'rm', '-f', container_name],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            logger.info(f"Successfully killed container: {container_name}")

            # Remove from Redis tracking
            try:
                redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
                redis_client.delete(f'agent:container:{container_name}')
            except Exception as e:
                logger.warning(f"Failed to clean up Redis tracking key for killed container {container_name}: {e}")

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

        # 1. Set cancellation signal FIRST to block any re-dispatch during lock release
        from services.cancellation import cancel_issue_work, get_cancellation_signal
        get_cancellation_signal().cancel(project, issue_number, "Pipeline run killed via Web UI")

        # 2. End the pipeline run (releases lock — safe because signal is already set)
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

        # 3. Full cleanup (containers, review cycles, execution state)
        cancel_issue_work(project, issue_number, "Pipeline run killed via Web UI")

        return jsonify({
            'success': True,
            'message': f'Pipeline run {pipeline_run_id} killed and work cancelled for {project}/#{issue_number}'
        })
        
    except Exception as e:
        logger.error(f"Error killing pipeline run: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/active-pipeline-runs')
def get_active_pipeline_runs():
    """Get all currently active pipeline runs with lock status"""
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

        # Get pipeline lock manager for lock status
        from services.pipeline_lock_manager import get_pipeline_lock_manager
        lock_manager = get_pipeline_lock_manager()

        runs = []
        for hit in result['hits']['hits']:
            run_data = hit['_source']

            # Add lock status information
            project = run_data.get('project')
            board = run_data.get('board')
            issue_number = run_data.get('issue_number')

            if project and board and issue_number:
                lock_status = lock_manager.get_lock_status_for_issue(
                    project, board, issue_number
                )
                lock_holder = lock_manager.get_lock_holder(project, board)

                run_data['lock_status'] = lock_status
                run_data['lock_holder_issue'] = lock_holder

                # Add additional context based on lock status
                if lock_status == 'waiting_for_lock' and lock_holder:
                    run_data['blocked_by_issue'] = lock_holder
            else:
                # Missing data - mark as unknown
                run_data['lock_status'] = 'unknown'
                run_data['lock_holder_issue'] = None

            runs.append(run_data)

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

@app.route('/api/feedback-loops/active')
def get_active_feedback_loops():
    """
    Get all active feedback loops with health status.

    Returns information about feedback loops that are currently monitoring
    for human feedback, including their health status based on heartbeat tracking.
    """
    try:
        from services.human_feedback_loop import human_feedback_loop_executor
        from datetime import datetime, timedelta
        import redis

        redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)

        active_loops = []

        # Get all active loops from in-memory state
        for issue_number, loop_state in human_feedback_loop_executor.active_loops.items():
            # Get heartbeat timestamp from Redis
            heartbeat_key = f"orchestrator:feedback_loop:heartbeat:{loop_state.project_name}:{issue_number}"
            last_heartbeat_str = redis_client.get(heartbeat_key)

            # Calculate health status based on heartbeat age
            health = "unknown"
            last_heartbeat = None
            heartbeat_age_seconds = None

            if last_heartbeat_str:
                try:
                    last_heartbeat = datetime.fromisoformat(last_heartbeat_str)
                    heartbeat_age = datetime.utcnow() - last_heartbeat
                    heartbeat_age_seconds = heartbeat_age.total_seconds()

                    # Health thresholds (poll interval is 30s)
                    if heartbeat_age_seconds < 120:  # < 2 minutes (4x poll interval)
                        health = "healthy"
                    elif heartbeat_age_seconds < 600:  # < 10 minutes (20x poll interval)
                        health = "stale"
                    else:  # >= 10 minutes
                        health = "stuck"
                except ValueError:
                    health = "unknown"
            else:
                # No heartbeat found (may be new loop that hasn't polled yet)
                health = "unknown"

            active_loops.append({
                "issue_number": issue_number,
                "project": loop_state.project_name,
                "agent": loop_state.agent,
                "workspace_type": loop_state.workspace_type,
                "discussion_id": loop_state.discussion_id,
                "pipeline_run_id": loop_state.pipeline_run_id,
                "started_at": loop_state.created_at,
                "last_heartbeat": last_heartbeat.isoformat() if last_heartbeat else None,
                "heartbeat_age_seconds": heartbeat_age_seconds,
                "current_iteration": loop_state.current_iteration,
                "health": health
            })

        # Sort by health (stuck first, then stale, then healthy)
        health_priority = {"stuck": 0, "stale": 1, "unknown": 2, "healthy": 3}
        active_loops.sort(key=lambda x: (health_priority.get(x["health"], 99), x["project"], x["issue_number"]))

        return jsonify({
            'success': True,
            'active_loops': active_loops,
            'count': len(active_loops)
        })

    except Exception as e:
        logger.error(f"Error fetching active feedback loops: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({
            'success': False,
            'error': str(e),
            'active_loops': []
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

        # Add trigger_source from execution state if available
        trigger_source = None
        pipeline_run_id = execution.get('pipeline_run_id')
        if pipeline_run_id:
            try:
                # Get issue_number from pipeline run
                from services.pipeline_run import get_pipeline_run_manager
                pipeline_run_manager = get_pipeline_run_manager()
                pipeline_run = pipeline_run_manager.get_pipeline_run_by_id(pipeline_run_id)

                if pipeline_run and hasattr(pipeline_run, 'issue_number'):
                    issue_number = pipeline_run.issue_number

                    # Query execution history for this issue
                    from services.work_execution_state import work_execution_tracker
                    history = work_execution_tracker.get_execution_history(
                        project_name=execution['project'],
                        issue_number=issue_number
                    )

                    # Find matching execution by task_id (most reliable)
                    task_id = execution['task_id']
                    agent_name = execution['agent']
                    started_at = execution['started_at']

                    for exec_record in history:
                        # First try to match by task_id (most reliable)
                        if exec_record.get('task_id') == task_id:
                            trigger_source = exec_record.get('trigger_source')
                            break
                        # Fallback: match by agent name and approximate timestamp
                        if exec_record.get('agent') == agent_name:
                            exec_ts = exec_record.get('timestamp')
                            if exec_ts:
                                try:
                                    exec_time = datetime.fromisoformat(exec_ts.replace('Z', '+00:00'))
                                    start_time = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                                    # Match if within 5 seconds
                                    if abs((exec_time - start_time).total_seconds()) < 5:
                                        trigger_source = exec_record.get('trigger_source')
                                        break
                                except Exception:
                                    pass
            except Exception as e:
                logger.warning(f"Failed to fetch trigger_source for execution {execution_id}: {e}")

        execution['trigger_source'] = trigger_source
        
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

            # Deduplicate logs at the API level to prevent duplicate entries
            # Create unique key from timestamp + event content
            seen_keys = set()
            for hit in logs_result['hits']['hits']:
                log_data = hit['_source']

                # Create unique key similar to frontend deduplication
                timestamp = log_data.get('timestamp', '')
                agent = log_data.get('agent', '')
                task_id = log_data.get('task_id', '')
                event_type = log_data.get('event_type', '')

                # Extract event content for uniqueness
                event = log_data.get('event') or log_data.get('raw_event', {}).get('event', {})
                msg_type = event.get('type', '') if isinstance(event, dict) else ''
                msg_id = ''
                msg_model = ''
                if isinstance(event, dict) and isinstance(event.get('message'), dict):
                    msg_id = event['message'].get('id', '')
                    msg_model = event['message'].get('model', '')

                content_hash = f"{msg_type}-{msg_id}-{msg_model}"
                key = f"{timestamp}-{agent}-{task_id}-{event_type}-{content_hash}"

                if key in seen_keys:
                    logger.debug(f"[API] Filtering duplicate log: {key[:100]}")
                    continue  # Skip duplicate

                seen_keys.add(key)
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

@app.route('/api/metrics/agents', methods=['GET'])
def get_agent_metrics():
    """
    Get per-agent token usage metrics aggregated over the requested time window.

    Fetches hourly bucket documents from token-metrics-agents-hourly-* and
    aggregates them with weighted math so averages, totals, and min/max are
    correct across any number of hours.
    """
    try:
        try:
            days = int(request.args.get('days', 7))
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Invalid days parameter', 'metrics': []}), 400
        days = max(1, min(days, 365))
        agent_filter = request.args.get('agent')

        must_clauses = [{'range': {'hour_bucket': {'gte': f'now-{days}d'}}}]
        if agent_filter:
            must_clauses.append({'term': {'agent_name': agent_filter}})

        try:
            requested_size = days * 24 * 20  # hours × max agents
            query_size = min(requested_size, 10000)  # ES max_result_window default
            if requested_size > query_size:
                logger.warning(
                    f"Agent metrics query for {days} days would need {requested_size} docs; "
                    f"capped at {query_size} — older hours may be excluded"
                )
            result = es_client.search(
                index='token-metrics-agents-hourly-*',
                body={
                    'size': query_size,
                    'query': {'bool': {'must': must_clauses}},
                    'sort': [{'hour_bucket': {'order': 'asc'}}],
                }
            )
            hourly_docs = [hit['_source'] for hit in result['hits']['hits']]
        except Exception as e:
            if 'index_not_found' in str(e).lower() or 'no such index' in str(e).lower():
                return jsonify({'success': True, 'metrics': []})
            raise

        by_agent: dict = {}
        for doc in hourly_docs:
            an = doc.get('agent_name')
            if an:
                by_agent.setdefault(an, []).append(doc)

        metrics = []
        for agent_name, docs in by_agent.items():
            aggregated = _aggregate_hourly_agent_docs(agent_name, docs)
            if aggregated:
                metrics.append(aggregated)

        # Compact per-agent hourly series for trend charts (no extra ES query)
        hourly_series = {}
        for doc in hourly_docs:
            an = doc.get('agent_name')
            tc = doc.get('task_count', 0)
            if an and tc:
                hourly_series.setdefault(an, []).append({
                    'h': doc.get('hour_bucket'),
                    'sum_out': int(doc.get('sum_output') or 0),
                    'tc': tc,
                })

        return jsonify({'success': True, 'metrics': metrics, 'hourly_series': hourly_series})

    except Exception as e:
        logger.error(f"Error fetching agent metrics: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e), 'metrics': []}), 500


@app.route('/api/metrics/cycles', methods=['GET'])
def get_cycle_metrics():
    """
    Get cycle token metrics aggregated over the requested time window.

    Fetches hourly bucket documents from token-metrics-cycles-hourly-* and
    aggregates them with weighted math so averages, totals, and min/max are
    correct across any number of hours.
    """
    try:
        try:
            days = int(request.args.get('days', 7))
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Invalid days parameter', 'metrics': []}), 400
        days = max(1, min(days, 7))  # raw data retained for 7 days
        cycle_type_filter = request.args.get('cycle_type')

        must_clauses = [{'range': {'hour_bucket': {'gte': f'now-{days}d'}}}]
        if cycle_type_filter:
            must_clauses.append({'term': {'cycle_type': cycle_type_filter}})

        try:
            result = es_client.search(
                index='token-metrics-cycles-hourly-*',
                body={
                    'size': days * 24 * 10,  # hours × max cycle types
                    'query': {'bool': {'must': must_clauses}},
                    'sort': [{'hour_bucket': {'order': 'asc'}}],
                }
            )
            hourly_docs = [hit['_source'] for hit in result['hits']['hits']]
        except Exception as e:
            if 'index_not_found' in str(e).lower() or 'no such index' in str(e).lower():
                return jsonify({'success': True, 'metrics': []})
            raise

        # Group hourly docs by cycle_type and aggregate
        by_type: dict = {}
        for doc in hourly_docs:
            ct = doc.get('cycle_type')
            if ct:
                by_type.setdefault(ct, []).append(doc)

        metrics = []
        for cycle_type, docs in by_type.items():
            aggregated = _aggregate_hourly_cycle_docs(cycle_type, docs)
            if aggregated:
                metrics.append(aggregated)

        # Compact per-cycle hourly series for trend charts (no extra ES query)
        hourly_series = {}
        for doc in hourly_docs:
            ct = doc.get('cycle_type')
            tc = doc.get('task_count', 0)
            if ct and tc:
                hourly_series.setdefault(ct, []).append({
                    'h': doc.get('hour_bucket'),
                    'sum_out': int(doc.get('sum_output') or 0),
                    'tc': tc,
                })

        return jsonify({'success': True, 'metrics': metrics, 'hourly_series': hourly_series})

    except Exception as e:
        logger.error(f"Error fetching cycle metrics: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e), 'metrics': []}), 500


@app.route('/api/metrics/run-now', methods=['POST'])
def trigger_token_metrics():
    """Trigger an immediate token metrics computation job."""
    import threading
    import asyncio

    def _run():
        try:
            from services.token_metrics_service import get_token_metrics_service
            asyncio.run(get_token_metrics_service().run_metrics_job())
        except Exception as e:
            logger.error(f"Error in background token metrics job: {e}", exc_info=True)

    threading.Thread(target=_run, daemon=True, name='token-metrics-now').start()
    return jsonify({'success': True, 'message': 'Token metrics job triggered'})


@app.route('/api/metrics/run-full-history', methods=['POST'])
def trigger_token_metrics_full_history():
    """Backfill token metrics across all available event history.

    Determines the oldest agent_initialized event in Elasticsearch and runs the
    metrics job with a lookback that covers the entire available window.  The
    scheduled cron job cadence is not affected.
    """
    import threading
    import asyncio

    def _run():
        try:
            from services.token_metrics_service import get_token_metrics_service
            svc = get_token_metrics_service()
            lookback_hours = svc.find_oldest_event_hours_ago()
            logger.info(f"Full-history token metrics backfill: oldest event ~{lookback_hours}h ago")
            asyncio.run(svc.run_metrics_job(lookback_hours=lookback_hours))
        except Exception as e:
            logger.error(f"Error in background full-history token metrics job: {e}", exc_info=True)

    threading.Thread(target=_run, daemon=True, name='token-metrics-full-history').start()
    return jsonify({'success': True, 'message': 'Full-history token metrics backfill triggered'})


def _merge_canonical_breakdown(target: dict, source: dict) -> None:
    """
    Merge a source canonical breakdown object into a target accumulator in-place.

    Canonical fields (task_count, sum_*, min_*, max_*) are summed or min/max-ed
    appropriately. The nested tool_breakdown and model_breakdown inside agent entries
    are merged recursively.
    """
    sum_fields = [
        'task_count', 'sum_direct_input', 'sum_cache_read', 'sum_cache_creation',
        'sum_output', 'sum_initial_input', 'sum_max_context', 'sum_context_growth',
    ]
    minmax_fields_min = ['min_max_context', 'min_output']
    minmax_fields_max = ['max_max_context', 'max_output']

    for f in sum_fields:
        target[f] = target.get(f, 0) + source.get(f, 0)
    for f in minmax_fields_min:
        sv = source.get(f, 0)
        tv = target.get(f, 0)
        # 0 is used as sentinel meaning "no data recorded" (not a legitimate min value),
        # so skip zero values when merging minimums.
        target[f] = sv if tv == 0 else (tv if sv == 0 else min(tv, sv))
    for f in minmax_fields_max:
        target[f] = max(target.get(f, 0), source.get(f, 0))

    # Recursively merge nested tool_breakdown and model_breakdown
    for nested_key in ('tool_breakdown', 'model_breakdown'):
        if nested_key not in source:
            continue
        if nested_key not in target:
            target[nested_key] = {}
        for entry_name, entry_data in (source[nested_key] or {}).items():
            if entry_name not in target[nested_key]:
                target[nested_key][entry_name] = {}
            _merge_canonical_breakdown(target[nested_key][entry_name], entry_data)


def _build_display_fields(stats: dict, task_count: int) -> dict:
    """
    Compute derived display averages from raw sum fields.
    Returns a dict of avg_* fields ready for the API response.
    """
    def avg(field):
        return stats.get(field, 0) / task_count if task_count > 0 else 0

    return {
        'avg_direct_input': avg('sum_direct_input'),
        'avg_cache_read': avg('sum_cache_read'),
        'avg_cache_creation': avg('sum_cache_creation'),
        'avg_output': avg('sum_output'),
        'avg_initial_input': avg('sum_initial_input'),
        'avg_max_context': avg('sum_max_context'),
    }


def _aggregate_hourly_agent_docs(agent_name: str, docs: list) -> dict:
    """
    Aggregate a list of hourly sum-based agent documents into a display-ready dict.

    Averages are computed as weighted means (sum / total_tasks) so combining
    hours with different task counts produces the correct overall average.
    """
    total_tasks = sum(d.get('task_count', 0) for d in docs)
    if total_tasks == 0:
        return {}

    def wsum(field):
        return sum(d.get(field, 0) for d in docs)

    def safe_min(field):
        # 0 used as sentinel ("no data"), so filter it out before taking the minimum
        vals = [d[field] for d in docs if d.get(field, 0) > 0]
        return min(vals) if vals else 0

    def safe_max(field):
        return max((d.get(field, 0) for d in docs), default=0)

    # Merge nested tool_breakdown and model_breakdown from each hourly doc
    tool_bd_agg: dict = {}
    for d in docs:
        for tool, tb in (d.get('tool_breakdown') or {}).items():
            if tool not in tool_bd_agg:
                tool_bd_agg[tool] = {}
            _merge_canonical_breakdown(tool_bd_agg[tool], tb)

    model_bd_agg: dict = {}
    for d in docs:
        for model, mb in (d.get('model_breakdown') or {}).items():
            if model not in model_bd_agg:
                model_bd_agg[model] = {}
            _merge_canonical_breakdown(model_bd_agg[model], mb)

    sum_direct = wsum('sum_direct_input')
    sum_cr = wsum('sum_cache_read')
    sum_cc = wsum('sum_cache_creation')
    sum_out = wsum('sum_output')
    sum_ii = wsum('sum_initial_input')
    sum_mc = wsum('sum_max_context')

    return {
        'agent_name': agent_name,
        'task_count': total_tasks,
        # Raw sums
        'sum_direct_input': sum_direct,
        'sum_cache_read': sum_cr,
        'sum_cache_creation': sum_cc,
        'sum_output': sum_out,
        'sum_initial_input': sum_ii,
        'sum_max_context': sum_mc,
        'min_max_context': safe_min('min_max_context'),
        'max_max_context': safe_max('max_max_context'),
        'min_output': safe_min('min_output'),
        'max_output': safe_max('max_output'),
        # Derived averages
        'avg_direct_input': sum_direct / total_tasks,
        'avg_cache_read': sum_cr / total_tasks,
        'avg_cache_creation': sum_cc / total_tasks,
        'avg_output': sum_out / total_tasks,
        'avg_initial_input': sum_ii / total_tasks,
        'avg_max_context': sum_mc / total_tasks,
        # Nested breakdowns
        'tool_breakdown': tool_bd_agg,
        'model_breakdown': model_bd_agg,
    }


def _aggregate_hourly_cycle_docs(cycle_type: str, docs: list) -> dict:
    """
    Aggregate a list of hourly sum-based cycle documents into a display-ready dict.

    Averages are computed as weighted means (sum / total_tasks) so combining
    hours with different task counts produces the correct overall average.
    Min/max are the true min/max across all individual tasks in any of the hours.
    """
    total_tasks = sum(d.get('task_count', 0) for d in docs)
    if total_tasks == 0:
        return {}

    def wsum(field):
        return sum(d.get(field, 0) for d in docs)

    def safe_min(field):
        vals = [d[field] for d in docs if field in d and d[field] > 0]
        return min(vals) if vals else 0

    def safe_max(field):
        vals = [d[field] for d in docs if field in d]
        return max(vals) if vals else 0

    # Merge tool_breakdown from each hourly doc
    tool_bd_agg: dict = {}
    for d in docs:
        for tool, tb in (d.get('tool_breakdown') or {}).items():
            if tool not in tool_bd_agg:
                tool_bd_agg[tool] = {}
            _merge_canonical_breakdown(tool_bd_agg[tool], tb)

    # Merge model_breakdown from each hourly doc
    model_bd_agg: dict = {}
    for d in docs:
        for model, mb in (d.get('model_breakdown') or {}).items():
            if model not in model_bd_agg:
                model_bd_agg[model] = {}
            _merge_canonical_breakdown(model_bd_agg[model], mb)

    # Merge agent_breakdown — each agent entry has the full canonical shape
    # including its own tool_breakdown and model_breakdown
    agent_bd_agg: dict = {}
    for d in docs:
        for agent, ab in (d.get('agent_breakdown') or {}).items():
            if agent not in agent_bd_agg:
                agent_bd_agg[agent] = {}
            _merge_canonical_breakdown(agent_bd_agg[agent], ab)

    # Build display agent_breakdown with avg fields
    agent_breakdown = {}
    for agent, ab in agent_bd_agg.items():
        tc = ab.get('task_count', 0)
        agent_breakdown[agent] = {
            'task_count': tc,
            'sum_direct_input': ab.get('sum_direct_input', 0),
            'sum_cache_read': ab.get('sum_cache_read', 0),
            'sum_cache_creation': ab.get('sum_cache_creation', 0),
            'sum_output': ab.get('sum_output', 0),
            'sum_initial_input': ab.get('sum_initial_input', 0),
            'sum_max_context': ab.get('sum_max_context', 0),
            'min_max_context': ab.get('min_max_context', 0),
            'max_max_context': ab.get('max_max_context', 0),
            'min_output': ab.get('min_output', 0),
            'max_output': ab.get('max_output', 0),
            'tool_breakdown': ab.get('tool_breakdown', {}),
            'model_breakdown': ab.get('model_breakdown', {}),
        }
        agent_breakdown[agent].update(_build_display_fields(ab, tc))

    total_sum_direct = wsum('sum_direct_input')
    total_sum_cache_read = wsum('sum_cache_read')
    total_sum_cache_creation = wsum('sum_cache_creation')
    total_sum_output = wsum('sum_output')
    total_sum_initial_input = wsum('sum_initial_input')
    total_sum_max_context = wsum('sum_max_context')

    return {
        'cycle_type': cycle_type,
        'task_count': total_tasks,
        # Raw sums (for programmatic use)
        'sum_direct_input': total_sum_direct,
        'sum_cache_read': total_sum_cache_read,
        'sum_cache_creation': total_sum_cache_creation,
        'sum_output': total_sum_output,
        'sum_initial_input': total_sum_initial_input,
        'sum_max_context': total_sum_max_context,
        # Averages (for display)
        'avg_direct_input': total_sum_direct / total_tasks,
        'avg_cache_read': total_sum_cache_read / total_tasks,
        'avg_cache_creation': total_sum_cache_creation / total_tasks,
        'avg_output': total_sum_output / total_tasks,
        'avg_initial_input': total_sum_initial_input / total_tasks,
        'avg_max_context': total_sum_max_context / total_tasks,
        # Range fields
        'min_max_context': safe_min('min_max_context'),
        'max_max_context': safe_max('max_max_context'),
        'min_output': safe_min('min_output'),
        'max_output': safe_max('max_output'),
        # Nested breakdowns
        'tool_breakdown': tool_bd_agg,
        'model_breakdown': model_bd_agg,
        'agent_breakdown': agent_breakdown,
    }


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

@app.route('/api/reconcile-state', methods=['POST'])
def reconcile_state():
    """
    Force reconciliation of system state.

    This endpoint triggers immediate reconciliation of:
    - Docker container state with tracking systems
    - Pipeline queues with GitHub board state
    - Execution states and pipeline locks

    Request body (optional):
        {
            "docker": true,     # Reconcile Docker containers (default: true)
            "queues": true,     # Force sync queues with GitHub (default: true)
            "project": "name"   # Optional: only reconcile specific project
        }

    Returns:
        JSON with reconciliation results
    """
    try:
        from services.scheduled_tasks import get_scheduled_tasks_service

        scheduler = get_scheduled_tasks_service()

        # Get request options
        request_data = request.get_json() if request.is_json else {}
        reconcile_docker = request_data.get('docker', True)
        reconcile_queues = request_data.get('queues', True)
        specific_project = request_data.get('project')

        results = {
            'docker_reconciliation': None,
            'queue_reconciliation': None
        }

        # Trigger Docker state reconciliation
        if reconcile_docker:
            logger.info("Manual trigger: Docker state reconciliation")
            scheduler.run_docker_reconciliation_now()
            results['docker_reconciliation'] = 'triggered'

        # Trigger queue state reconciliation
        if reconcile_queues:
            logger.info("Manual trigger: Queue state reconciliation")
            scheduler.run_queue_reconciliation_now()
            results['queue_reconciliation'] = 'triggered'

        return jsonify({
            'success': True,
            'message': 'State reconciliation triggered',
            'results': results,
            'note': 'Reconciliation runs asynchronously - check logs for results'
        }), 200

    except Exception as e:
        logger.error(f"Error triggering state reconciliation: {e}")
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


@app.route('/api/blocked-issues', methods=['GET'])
def get_blocked_issues():
    """
    Get all issues that are blocking pipelines across all projects.

    An issue is "blocked" if:
    - It has status='active' (holds the lock)
    - Its last execution outcome is 'failure'
    - No container is currently running for it

    This indicates the agent failed and the pipeline is stuck waiting
    for manual intervention (move to Backlog, close, or move to non-trigger column).

    Returns:
        JSON response with:
        {
            'blocked_issues': [
                {
                    'project': 'context-studio',
                    'board': 'SDLC Execution',
                    'issue_number': 159,
                    'position': 0,
                    'failed_agent': 'senior_software_engineer',
                    'error': 'Agent execution interrupted...',
                    'failed_at': '2025-01-24T10:30:00Z',
                    'column': 'Development'
                },
                ...
            ],
            'count': 1
        }
    """
    try:
        from services.pipeline_queue_manager import get_pipeline_queue_manager
        from config.manager import config_manager

        all_blocked = []

        # Iterate through all projects and their pipelines
        for project_name in config_manager.get_project_names():
            project_config = config_manager.get_project_config(project_name)

            for pipeline in project_config.pipelines:
                try:
                    queue_mgr = get_pipeline_queue_manager(
                        project_name, pipeline.board_name
                    )

                    blocked = queue_mgr.get_blocked_issues()

                    # Add project and board context to each blocked issue
                    for issue in blocked:
                        issue['project'] = project_name
                        issue['board'] = pipeline.board_name
                        all_blocked.append(issue)

                except Exception as e:
                    logger.error(
                        f"Failed to get blocked issues for {project_name}/{pipeline.board_name}: {e}",
                        exc_info=True
                    )
                    # Continue with other pipelines

        return jsonify({
            'blocked_issues': all_blocked,
            'count': len(all_blocked),
            'timestamp': datetime.utcnow().isoformat() + 'Z'
        }), 200

    except Exception as e:
        logger.error(f"Failed to get blocked issues: {e}", exc_info=True)
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

                        if event_type in decision_event_types:
                            # Decision event - route to decision_event handler
                            socketio.emit('decision_event', event_data)
                            logger.debug(f"Broadcasted decision event: {event_type}")
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
