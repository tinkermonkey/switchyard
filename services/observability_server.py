"""
WebSocket server for streaming agent observability events to web UI
"""

import asyncio
import json
import logging
import redis
from flask import Flask, request
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import threading

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # Enable CORS for web UI
socketio = SocketIO(app, cors_allowed_origins="*")

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
    """Health check endpoint"""
    return {
        'status': 'healthy',
        'connected_clients': len(connected_clients)
    }

@app.route('/history')
def get_history():
    """Get recent event history from Redis Stream"""
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

        # Read from stream (oldest to newest)
        events = redis_client.xrevrange(stream_key, '+', '-', count=count)

        # Parse events
        history = []
        for event_id, event_data in events:
            try:
                event_json = event_data.get('event')
                if event_json:
                    history.append(json.loads(event_json))
            except Exception as e:
                logger.error(f"Error parsing event: {e}")

        # Reverse so oldest is first (chronological order)
        history.reverse()

        return {
            'success': True,
            'count': len(history),
            'total': total_count,
            'events': history
        }

    except Exception as e:
        logger.error(f"Error fetching history: {e}")
        return {
            'success': False,
            'error': str(e),
            'events': []
        }, 500

@app.route('/claude-logs-history')
def get_claude_logs_history():
    """Get recent Claude log history from Redis Stream"""
    try:
        # Connect to Redis
        redis_client = redis.Redis(
            host='redis',
            port=6379,
            decode_responses=True
        )

        # Get last N logs from the stream
        count = int(request.args.get('count', 100))
        count = min(count, 500)  # Cap at 500 logs

        stream_key = "orchestrator:claude_logs_stream"

        # Get total count in stream
        total_count = redis_client.xlen(stream_key)

        # Read from stream (newest to oldest, then reverse)
        logs = redis_client.xrevrange(stream_key, '+', '-', count=count)

        # Parse logs
        history = []
        for log_id, log_data in logs:
            try:
                log_json = log_data.get('log')
                if log_json:
                    history.append(json.loads(log_json))
            except Exception as e:
                logger.error(f"Error parsing log: {e}")

        # Reverse so oldest is first (chronological order)
        history.reverse()

        return {
            'success': True,
            'count': len(history),
            'total': total_count,
            'logs': history
        }

    except Exception as e:
        logger.error(f"Error fetching Claude log history: {e}")
        return {
            'success': False,
            'error': str(e),
            'logs': []
        }, 500

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