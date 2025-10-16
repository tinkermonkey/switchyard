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
import subprocess
from pathlib import Path
from datetime import datetime
import time

# Setup logging with reduced verbosity
logger = setup_service_logging('observability_server')

app = Flask(__name__)
CORS(app)  # Enable CORS for web UI
socketio = SocketIO(app, cors_allowed_origins="*")

# Initialize Elasticsearch client
es_client = Elasticsearch(['http://elasticsearch:9200'])

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
                'connected_clients': len(connected_clients),
                'subscriber_health': subscriber_health
            }), 503

        health_data = json.loads(health_json)
        
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
                        "pipeline_run_id.keyword": pipeline_run_id
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
                        "pipeline_run_id": pipeline_run_id
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
                        "pipeline_run_id.keyword": pipeline_run_id
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
            index="pipeline-runs",
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
        logger.error(f"Error fetching active pipeline runs: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'runs': []
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
            index="pipeline-runs",
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
            'code_reviewer',
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

def collect_git_branch_data(project_name, workspace_path):
    """Collect git branch information for a project"""
    try:
        if not workspace_path.exists() or not (workspace_path / '.git').exists():
            return None

        branch_data = {
            'current_branch': None,
            'branches': [],
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

            # Check configured projects in workspace
            project_configs = config_manager.list_projects()

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

                # Get pipeline info
                pipelines = []
                if hasattr(project_config, 'pipelines') and project_config.pipelines:
                    # pipelines is a list of ProjectPipeline objects
                    pipelines = [p.name for p in project_config.pipelines if hasattr(p, 'name')]

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
                'columns': columns
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

    logger.info(f"Starting observability server on {host}:{port}")
    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)

if __name__ == '__main__':
    start_observability_server()