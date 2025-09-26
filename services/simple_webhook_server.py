def start_webhook_server(port: int):
    """Start Flask webhook server in current thread"""
    from flask import Flask, request, jsonify
    from task_queue.task_manager import TaskQueue, Task, TaskPriority
    from datetime import datetime

    app = Flask(__name__)
    task_queue = TaskQueue()

    @app.route('/webhook', methods=['POST'])
    def handle_webhook():
        payload = request.json

        if payload.get('action') == 'moved':
            # Card moved on Kanban board
            task = Task(
                id=f"webhook_{payload.get('project', {}).get('id', 'unknown')}_{datetime.now().timestamp()}",
                agent='business_analyst',
                project=payload.get('project', {}).get('name', 'unknown'),
                priority=TaskPriority.MEDIUM,
                context={
                    'issue': payload.get('issue', {}),
                    'webhook_payload': payload
                },
                created_at=datetime.now().isoformat()
            )
            task_queue.enqueue(task)
            return jsonify({'status': 'queued'})

        return jsonify({'status': 'ignored'})

    @app.route('/health', methods=['GET'])
    def health():
        return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})

    print(f"🚀 Starting webhook server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)