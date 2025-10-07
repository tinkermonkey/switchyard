"""
WebSocket server for streaming agent observability events to web UI
"""

import asyncio
import json
import logging
import redis
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import threading
from elasticsearch import Elasticsearch
from services.logging_config import setup_service_logging

# Setup logging with reduced verbosity
logger = setup_service_logging('observability_server')

app = Flask(__name__)
CORS(app)  # Enable CORS for web UI
socketio = SocketIO(app, cors_allowed_origins="*")

# Initialize Elasticsearch client
es_client = Elasticsearch(['http://elasticsearch:9200'])

# Track connected clients
connected_clients = set()

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

@app.route('/health')
def health():
    """Health check endpoint - returns orchestrator health status"""
    import json

    # Get last health check result from Redis (cross-process shared state)
    try:
        redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
        health_json = redis_client.get('orchestrator:health')

        if health_json is None:
            # No health check has run yet
            return jsonify({
                'status': 'starting',
                'message': 'Orchestrator is starting, no health check completed yet',
                'connected_clients': len(connected_clients)
            }), 503

        health_data = json.loads(health_json)

        # Return the full health check data
        # Determine status: healthy, degraded, or unhealthy
        if health_data.get('healthy'):
            status = 'degraded' if health_data.get('degraded') else 'healthy'
            status_code = 200
        else:
            status = 'unhealthy'
            status_code = 503

        response = {
            'status': status,
            'connected_clients': len(connected_clients),
            'orchestrator': health_data
        }

        return jsonify(response), status_code

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'Failed to retrieve health status: {str(e)}',
            'connected_clients': len(connected_clients)
        }), 503

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
            if any(agent in ['idea_researcher', 'business_analyst', 'requirements_reviewer'] for agent in recent_agents):
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

# =======================================================================================
# REVIEW FILTER API ENDPOINTS
# =======================================================================================

@app.route('/api/review-filters', methods=['GET'])
def get_review_filters():
    """Get all review filters with optional filtering"""
    try:
        from services.review_filter_manager import get_review_filter_manager

        filter_manager = get_review_filter_manager()

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
            'requirements_reviewer',
            'design_reviewer',
            'code_reviewer',
            'test_reviewer',
            'qa_reviewer'
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
    """Get circuit breaker status from pattern ingestion service"""
    try:
        # Connect to Redis
        redis_client = redis.Redis(
            host='redis',
            port=6379,
            decode_responses=True
        )

        # Get stats from Redis
        stats_json = redis_client.get('orchestrator:pattern_ingestion_stats')

        if not stats_json:
            return jsonify({
                'success': False,
                'error': 'Pattern ingestion service stats not available',
                'circuit_breakers': []
            }), 503

        stats = json.loads(stats_json)

        # Extract circuit breaker states
        circuit_breakers = []

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

        # Calculate summary
        open_count = sum(1 for cb in circuit_breakers if cb['state'] == 'open')
        half_open_count = sum(1 for cb in circuit_breakers if cb['state'] == 'half_open')

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

def redis_subscriber_thread():
    """Background thread that listens to Redis pub/sub and broadcasts to WebSocket clients"""
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

        for message in pubsub.listen():
            if message['type'] == 'message':
                try:
                    # Parse event
                    event_data = json.loads(message['data'])

                    # Route to appropriate websocket event based on channel
                    if message['channel'] == 'orchestrator:agent_events':
                        # Regular agent events
                        socketio.emit('agent_event', event_data)
                        logger.debug(f"Broadcasted agent event: {event_data.get('event_type')}")
                    elif message['channel'] == 'orchestrator:claude_stream':
                        # Claude stream events
                        socketio.emit('claude_stream_event', event_data)
                        logger.debug(f"Broadcasted Claude stream event from {event_data.get('agent')}")

                except Exception as e:
                    logger.error(f"Error processing Redis message: {e}")

    except Exception as e:
        logger.error(f"Redis subscriber error: {e}")

def start_observability_server(host='0.0.0.0', port=5001):
    """Start the observability WebSocket server"""
    # Start Redis subscriber in background thread
    subscriber = threading.Thread(target=redis_subscriber_thread, daemon=True)
    subscriber.start()

    logger.info(f"Starting observability server on {host}:{port}")
    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)

if __name__ == '__main__':
    start_observability_server()