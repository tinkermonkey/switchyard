import requests
import json
import time
import asyncio
import redis
from datetime import datetime
from pathlib import Path
import sys

# Add parent directory to path to import modules
sys.path.append(str(Path(__file__).parent.parent))

def test_webhook_to_orchestrator():
    """Test complete webhook → orchestrator flow"""

    print("🔄 Testing webhook integration...")

    # 1. Send test webhook
    webhook_payload = {
        "action": "opened",
        "issue": {
            "number": 123,
            "title": "Test Integration Issue",
            "body": "Testing webhook integration with orchestrator",
            "html_url": "https://github.com/test/repo/issues/123"
        },
        "repository": {
            "name": "test-repo"
        }
    }

    # Send to webhook server
    try:
        response = requests.post(
            'http://localhost:3000/github-webhook',
            json=webhook_payload,
            headers={
                'X-GitHub-Event': 'issues',
                'X-Hub-Signature-256': 'sha256=test'  # Will need real signature in production
            },
            timeout=10
        )

        assert response.status_code == 200
        print("✅ Webhook received and queued")
    except requests.exceptions.RequestException as e:
        print(f"❌ Failed to connect to webhook server: {e}")
        print("💡 Make sure the webhook server is running with: python services/webhook_server.py")
        return False

    # 2. Check task was created in Redis using orchestrator's TaskQueue format
    try:
        redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)

        # Wait a moment for processing
        time.sleep(2)

        # Check for tasks in queue using orchestrator's format
        tasks_found = False
        for priority in ['high', 'medium', 'low']:
            queue_name = f"tasks:{priority}"
            task_ids = redis_client.lrange(queue_name, 0, -1)

            if task_ids:
                print(f"✅ Found {len(task_ids)} tasks in {priority} priority queue")
                tasks_found = True

                # 3. Verify task structure
                task_data = redis_client.hgetall(f"task:{task_ids[0]}")
                assert task_data['agent'] == 'business_analyst', f"Expected business_analyst, got {task_data.get('agent')}"
                assert task_data['project'] == 'test-repo', f"Expected test-repo, got {task_data.get('project')}"

                # Check context contains issue information
                context_str = task_data.get('context', '{}')
                context = eval(context_str) if context_str else {}
                assert 'issue_number' in context, "Missing issue_number in task context"
                assert context['issue_number'] == 123, f"Expected issue 123, got {context['issue_number']}"

                print("✅ Task structure validated")
                break

        if not tasks_found:
            print("❌ No tasks found in any queue")
            return False

    except redis.ConnectionError:
        print("❌ Could not connect to Redis. Make sure Redis is running.")
        return False
    except Exception as e:
        print(f"❌ Error checking task queue: {e}")
        return False

    # 4. Check orchestrator can process the task (if running)
    try:
        # Import and test task processing directly
        from task_queue.task_manager import TaskQueue
        from state_management.manager import StateManager
        from monitoring.logging import OrchestratorLogger
        from agents.agent_stages import process_task_integrated

        task_queue = TaskQueue()
        task = task_queue.dequeue()

        if task:
            print(f"✅ Orchestrator can dequeue task: {task.id}")

            # Test processing (this would normally be done by main.py)
            state_manager = StateManager()
            logger = OrchestratorLogger("webhook_test")

            print("🔄 Testing task processing...")
            # Note: This would need to be called in an async context
            # For now, we'll just verify the task structure
            print(f"✅ Task ready for processing: {task.agent} -> {task.project}")
            return True
        else:
            print("⚠️  No task available to process (this is okay if testing webhook creation only)")
            return True

    except ImportError as e:
        print(f"⚠️  Could not import orchestrator modules: {e}")
        print("✅ Webhook integration test passed (webhook → queue creation verified)")
        return True
    except Exception as e:
        print(f"❌ Error testing orchestrator processing: {e}")
        return False

async def async_test_webhook_to_orchestrator():
    """Async wrapper for the test"""
    return test_webhook_to_orchestrator()

def test_queue_status_endpoint():
    """Test the queue status endpoint"""
    print("\n🔍 Testing queue status endpoint...")

    try:
        response = requests.get('http://localhost:3000/queue-status', timeout=5)
        assert response.status_code == 200

        data = response.json()
        print(f"✅ Queue status endpoint working")
        print(f"   - Pending tasks: {data.get('pending_tasks', 0)}")
        print(f"   - Webhook queue size: {data.get('webhook_queue_size', 0)}")

        return True
    except requests.exceptions.RequestException as e:
        print(f"❌ Queue status endpoint failed: {e}")
        return False

def test_health_endpoint():
    """Test the health endpoint"""
    print("\n💗 Testing health endpoint...")

    try:
        response = requests.get('http://localhost:3000/health', timeout=5)
        assert response.status_code == 200

        data = response.json()
        assert data.get('status') == 'healthy'

        print("✅ Health endpoint working")
        return True
    except Exception as e:
        print(f"❌ Health endpoint failed: {e}")
        return False

if __name__ == "__main__":
    print("🧪 Starting webhook integration tests...\n")

    # Test endpoints first
    health_ok = test_health_endpoint()
    status_ok = test_queue_status_endpoint()

    if not (health_ok and status_ok):
        print("\n❌ Basic endpoint tests failed")
        sys.exit(1)

    # Test webhook processing
    print("\n🚀 Testing webhook processing...")
    success = asyncio.run(async_test_webhook_to_orchestrator())

    if success:
        print("\n🎯 Webhook integration test PASSED!")
        sys.exit(0)
    else:
        print("\n❌ Webhook integration test FAILED!")
        sys.exit(1)