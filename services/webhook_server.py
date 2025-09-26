import requests
import time
import json
import os
import subprocess
import hmac
import hashlib
import redis
import yaml
from flask import Flask, request, jsonify
from datetime import datetime
from task_queue.task_manager import task_queue as task_manager_queue

app = Flask(__name__)
task_queue = task_manager_queue()
redis_client = redis.Redis(host="redis", port=6379, decode_responses=True)


class WebhookProcessor:
    """Processes webhook events and creates tasks for agents"""

    def __init__(self):
        self.redis_client = redis_client
        self.project_configs = self.load_project_configs()

    def load_project_configs(self):
        """Load project configurations"""
        # Load from config/projects.yaml
        config_path = os.path.join(os.path.dirname(__file__), "..", "config", "projects.yaml")
        with open(config_path, "r") as f:
            project_configs = yaml.safe_load(f)
        return project_configs

    def process_event(self, event_data):
        """Main entry point for processing webhook events"""
        event_type = event_data["event_type"]
        payload = event_data["payload"]

        print(f"🔄 Processing {event_type} event")

        # Route to appropriate handler
        handlers = {
            "issues": self.handle_issue_event,
            "project_card": self.handle_project_card_event,
            "pull_request": self.handle_pull_request_event,
            "pull_request_review": self.handle_pr_review_event,
            "push": self.handle_push_event,
        }

        handler = handlers.get(event_type)
        if handler:
            try:
                handler(payload)
            except Exception as e:
                print(f"❌ Error processing {event_type}: {e}")
                self.log_error(event_type, payload, str(e))
        else:
            print(f"⚠️ No handler for event type: {event_type}")

    def handle_issue_event(self, payload):
        """Handle issue events (created, edited, closed, etc.)"""
        action = payload["action"]
        issue = payload["issue"]
        repo = payload["repository"]["name"]

        print(f"📋 Issue #{issue['number']} {action}: {issue['title']}")

        if action == "opened":
            # Create initial task for requirements analysis
            self.create_agent_task(
                agent="business_analyst",
                project=repo,
                priority="medium",
                context={
                    "issue_number": issue["number"],
                    "issue_title": issue["title"],
                    "issue_body": issue["body"],
                    "issue_url": issue["html_url"],
                    "action": "analyze_requirements",
                },
            )

        elif action == "labeled":
            # Check if specific labels trigger actions
            label = payload["label"]["name"]
            if label == "needs-design":
                self.create_agent_task(
                    agent="software_architect",
                    project=repo,
                    priority="medium",
                    context={"issue_number": issue["number"], "issue_title": issue["title"], "action": "create_design"},
                )

    def handle_project_card_event(self, payload):
        """Handle project card movements (Kanban board)"""
        action = payload["action"]

        if action != "moved":
            return

        card = payload["project_card"]
        column_id = card.get("column_id")

        # Get column name (you'll need to fetch this from GitHub API)
        column_name = self.get_column_name(column_id)

        # Extract issue from card
        issue_number = self.extract_issue_from_card(card)
        if not issue_number:
            print("⚠️ Could not extract issue from card")
            return

        repo = payload["repository"]["name"]

        print(f"📍 Card moved to column: {column_name} (Issue #{issue_number})")

        # Map column to agent
        project_config = self.project_configs.get(repo, {})
        agent = project_config.get("kanban_columns", {}).get(column_name)

        if agent:
            # Special handling for development column - create branch
            if column_name in ["In Development", "Ready for Development"]:
                self.create_feature_branch(repo, issue_number)

            # Create task for appropriate agent
            self.create_agent_task(
                agent=agent,
                project=repo,
                priority="high" if column_name == "Code Review" else "medium",
                context={
                    "issue_number": issue_number,
                    "column": column_name,
                    "action": "process_card_move",
                    "previous_column": payload.get("changes", {}).get("column_id", {}).get("from"),
                },
            )

    def handle_pull_request_event(self, payload):
        """Handle pull request events"""
        action = payload["action"]
        pr = payload["pull_request"]
        repo = payload["repository"]["name"]

        print(f"🔀 PR #{pr['number']} {action}: {pr['title']}")

        if action == "opened":
            # Trigger code review
            self.create_agent_task(
                agent="code_reviewer",
                project=repo,
                priority="high",
                context={
                    "pr_number": pr["number"],
                    "pr_title": pr["title"],
                    "pr_url": pr["html_url"],
                    "branch": pr["head"]["ref"],
                    "base_branch": pr["base"]["ref"],
                    "action": "review_pr",
                },
            )

        elif action == "synchronize":
            # Code was pushed to PR
            print(f"📝 PR #{pr['number']} updated with new commits")
            # Could retrigger review or tests here

    def handle_pr_review_event(self, payload):
        """Handle pull request review events"""
        action = payload["action"]
        review = payload["review"]
        pr = payload["pull_request"]
        repo = payload["repository"]["name"]

        if action == "submitted":
            state = review["state"]  # approved, changes_requested, commented

            print(f"👁️ PR #{pr['number']} review: {state}")

            if state == "changes_requested":
                # Notify developer agent to make changes
                self.create_agent_task(
                    agent="senior_software_engineer",
                    project=repo,
                    priority="high",
                    context={
                        "pr_number": pr["number"],
                        "review_comments": review["body"],
                        "action": "address_review_feedback",
                    },
                )

            elif state == "approved":
                # Could trigger merge or deployment
                print(f"✅ PR #{pr['number']} approved and ready to merge")

    def handle_push_event(self, payload):
        """Handle push events"""
        ref = payload["ref"]  # refs/heads/branch-name
        repo = payload["repository"]["name"]
        commits = payload.get("commits", [])

        branch = ref.replace("refs/heads/", "")
        print(f"🚀 Push to {branch}: {len(commits)} commits")

        # Could trigger CI/CD or other automation here

    def create_agent_task(self, agent, project, priority, context):
        """Create a task compatible with orchestrator's TaskQueue"""
        from task_queue.task_manager import Task, TaskPriority

        # Map string priority to TaskPriority enum
        priority_mapping = {
            'high': TaskPriority.HIGH,
            'medium': TaskPriority.MEDIUM,
            'low': TaskPriority.LOW
        }

        task = Task(
            id=f"{agent}_{project}_{datetime.now().timestamp()}",
            agent=agent,
            project=project,
            priority=priority_mapping.get(priority, TaskPriority.MEDIUM),
            context=context,
            created_at=datetime.now().isoformat(),
            status="pending"
        )

        # Use orchestrator's TaskQueue format
        from task_queue.task_manager import TaskQueue
        task_queue = TaskQueue()  # This uses Redis
        task_queue.enqueue(task)

        print(f"✅ Created task {task.id} for {agent} via orchestrator TaskQueue")

    def create_feature_branch(self, repo, issue_number):
        """Create a feature branch for development"""
        project_path = self.project_configs[repo]["path"]

        # This would be better done by the agent, but showing the concept
        branch_name = f"feature/#{issue_number}"

        commands = [
            ["git", "checkout", "main"],
            ["git", "pull", "origin", "main"],
            ["git", "checkout", "-b", branch_name],
        ]

        for cmd in commands:
            subprocess.run(cmd, cwd=project_path)

        print(f"🌿 Created branch: {branch_name}")

    def get_column_name(self, column_id):
        """Fetch column name from GitHub API"""
        # In production, use GitHub API to get column details
        # For now, return a placeholder
        return "In Development"

    def extract_issue_from_card(self, card):
        """Extract issue number from project card"""
        # Cards can have different content types
        if "content_url" in card:
            # Extract issue number from URL
            # Format: https://api.github.com/repos/OWNER/REPO/issues/NUMBER
            parts = card["content_url"].split("/")
            if "issues" in parts:
                return int(parts[-1])

        # Try to parse from note
        if "note" in card:
            import re

            match = re.search(r"#(\d+)", card["note"])
            if match:
                return int(match.group(1))

        return None

    def log_error(self, event_type, payload, error):
        """Log errors for debugging"""
        error_log = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "error": error,
            "payload_sample": {
                "action": payload.get("action"),
                "repository": payload.get("repository", {}).get("name"),
            },
        }

        # Store in Redis for debugging
        self.redis_client.lpush("webhook_errors", json.dumps(error_log))
        self.redis_client.ltrim("webhook_errors", 0, 99)


# Global processor instance
webhook_processor = WebhookProcessor()


def process_webhook_event(event_data):
    """Process a webhook event from the queue"""
    try:
        webhook_processor.process_event(event_data)
    except Exception as e:
        print(f"❌ Failed to process webhook: {e}")
        # Could add retry logic here


def webhook_worker():
    """Worker thread that processes queued webhook events"""
    print("👷 Webhook worker started")

    while True:
        try:
            # Get event from queue (blocks until available)
            event = task_queue.get(timeout=1)

            if event:
                print(f"Processing event from queue: {event.get('event_type')}")
                process_webhook_event(event)
                task_queue.task_done()

        except Exception as e:
            if "Empty" not in str(e):  # Ignore empty queue timeouts
                print(f"Worker error: {e}")
            continue


class WebhookServer:
    def __init__(self):
        self.secret = os.environ["WEBHOOK_SECRET"]
        self.ngrok_url = None
        self.discover_ngrok_url()

    def verify_signature(self, payload, signature_header):
        """Verify GitHub webhook signature"""
        if not signature_header:
            return False

        hash_algorithm, github_signature = signature_header.split("=")
        expected_signature = hmac.new(self.secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

        return hmac.compare_digest(github_signature, expected_signature)

    def discover_ngrok_url(self):
        """Automatically discover ngrok URL from the ngrok API"""
        max_attempts = 10
        attempt = 0

        while attempt < max_attempts:
            try:
                # Query ngrok's local API
                response = requests.get("http://ngrok:4040/api/tunnels")
                tunnels = response.json()["tunnels"]

                for tunnel in tunnels:
                    if tunnel["proto"] == "https":
                        self.ngrok_url = tunnel["public_url"]
                        print(f"✅ Discovered ngrok URL: {self.ngrok_url}")
                        self.update_github_webhooks()
                        return

            except Exception as e:
                print(f"Waiting for ngrok to start... (attempt {attempt + 1}/{max_attempts})")
                time.sleep(2)
                attempt += 1

        print("⚠️ Could not discover ngrok URL. Please check manually at http://localhost:4040")

    def update_github_webhooks(self):
        """Automatically update GitHub webhooks with new ngrok URL"""
        if not self.ngrok_url:
            return

        webhook_url = f"{self.ngrok_url}/github-webhook"

        # Update webhooks for all configured projects
        projects = ["project-a", "project-b"]  # Load from config

        for project in projects:
            print(f"Updating webhook for {project} to {webhook_url}")

            # First, list existing webhooks
            result = subprocess.run(["gh", "api", f"repos/YOU/{project}/hooks"], capture_output=True, text=True)

            hooks = json.loads(result.stdout) if result.returncode == 0 else []

            # Find orchestrator webhook (if exists)
            orchestrator_hook = None
            for hook in hooks:
                if "ngrok" in hook.get("config", {}).get("url", ""):
                    orchestrator_hook = hook
                    break

            if orchestrator_hook:
                # Update existing webhook
                subprocess.run(
                    [
                        "gh",
                        "api",
                        f"repos/YOU/{project}/hooks/{orchestrator_hook['id']}",
                        "--method",
                        "PATCH",
                        "--field",
                        f"config[url]={webhook_url}",
                    ]
                )
            else:
                # Create new webhook
                subprocess.run(
                    [
                        "gh",
                        "api",
                        f"repos/YOU/{project}/hooks",
                        "--method",
                        "POST",
                        "--field",
                        "name=web",
                        "--field",
                        "active=true",
                        "--field",
                        f"config[url]={webhook_url}",
                        "--field",
                        "config[content_type]=json",
                        "--field",
                        f"config[secret]={self.secret}",
                        "--field",
                        "events[]=issues",
                        "--field",
                        "events[]=project_card",
                        "--field",
                        "events[]=pull_request",
                    ]
                )

# Initialize server
webhook_server = WebhookServer()

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'healthy',
        'queue_size': task_queue.qsize(),
        'timestamp': datetime.now().isoformat()
    })

@app.route('/github-webhook', methods=['POST'])
def github_webhook():
    # Verify signature
    signature = request.headers.get('X-Hub-Signature-256')
    if not webhook_server.verify_signature(request.data, signature):
        return jsonify({'error': 'Invalid signature'}), 401
    
    # Queue the event
    event_type = request.headers.get('X-GitHub-Event')
    payload = request.json
    
    task_queue.put({
        'event_type': event_type,
        'payload': payload,
        'timestamp': datetime.now().isoformat()
    })
    
    return jsonify({'status': 'queued'}), 200

@app.route('/queue-status', methods=['GET'])
def queue_status():
    """Check queue and task status"""
    
    # Get pending tasks from Redis
    pending_tasks = []
    for priority in ['high', 'medium', 'low']:
        queue_name = f"tasks:{priority}"
        task_ids = redis_client.lrange(queue_name, 0, 10)
        for task_id in task_ids:
            task_data = redis_client.hgetall(f"task:{task_id}")
            if task_data:
                pending_tasks.append(task_data)
    
    return jsonify({
        'webhook_queue_size': task_queue.qsize(),
        'pending_tasks': len(pending_tasks),
        'tasks': pending_tasks[:10]  # First 10 tasks
    })

if __name__ == '__main__':
    print("🚀 Starting webhook server...")
    app.run(host='0.0.0.0', port=3000, debug=False)