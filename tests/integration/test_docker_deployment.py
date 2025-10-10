import requests
import time
import subprocess
import json
import sys
from pathlib import Path

import logging

logger = logging.getLogger(__name__)
def test_docker_deployment():
    """Test complete system running in Docker"""

    logger.info(" Testing Docker deployment...")

    # 1. Verify all services are up
    result = subprocess.run(['docker-compose', 'ps'], capture_output=True, text=True)
    if result.returncode != 0:
        logger.info(" Failed to get docker-compose status")
        logger.info(f"Error: {result.stderr}")
        return False

    # Check that services are running (look for "Up" status)
    if 'Up' not in result.stdout:
        logger.info(" Some services are not running")
        logger.info("Docker services status:")
        logger.info(result.stdout)
        return False

    logger.info(" All Docker services running")

    # 2. Test webhook health
    max_attempts = 30  # Wait up to 60 seconds for services to be ready
    attempt = 0

    while attempt < max_attempts:
        try:
            health_response = requests.get('http://localhost:3000/health', timeout=5)
            if health_response.status_code == 200:
                logger.info(" Webhook server healthy")
                break
        except requests.exceptions.RequestException:
            pass

        attempt += 1
        time.sleep(2)

    if attempt >= max_attempts:
        logger.info(" Webhook server not responding after 60 seconds")
        return False

    # 3. Test ngrok tunnel (if available)
    try:
        tunnel_response = requests.get('http://localhost:4040/api/tunnels', timeout=5)
        if tunnel_response.status_code == 200:
            tunnels = tunnel_response.json().get('tunnels', [])
            if tunnels:
                public_url = tunnels[0]['public_url']
                logger.info(f" ngrok tunnel active: {public_url}")
            else:
                logger.info(" ngrok running but no tunnels active")
        else:
            logger.info(" ngrok web interface not accessible")
    except requests.exceptions.RequestException:
        logger.info(" ngrok not running (this is okay for local testing)")

    # 4. Send test webhook
    test_payload = {
        "action": "opened",
        "issue": {"number": 999, "title": "Docker test", "body": "Testing Docker deployment"},
        "repository": {"name": "docker-test"}
    }

    try:
        webhook_response = requests.post(
            'http://localhost:3000/github-webhook',
            json=test_payload,
            headers={'X-GitHub-Event': 'issues'},
            timeout=10
        )
        if webhook_response.status_code == 200:
            logger.info(" Webhook processed in Docker")
        else:
            logger.info(f" Webhook failed with status {webhook_response.status_code}")
            return False
    except requests.exceptions.RequestException as e:
        logger.info(f" Failed to send webhook: {e}")
        return False

    # 5. Check orchestrator logs for task processing
    logger.info("⏳ Waiting for orchestrator to process task...")
    time.sleep(10)  # Wait for processing

    logs_result = subprocess.run(
        ['docker-compose', 'logs', '--tail=50', 'orchestrator'],
        capture_output=True, text=True
    )

    if logs_result.returncode == 0:
        log_content = logs_result.stdout
        if 'business_analyst' in log_content or 'pipeline execution' in log_content:
            logger.info(" Orchestrator processing tasks in Docker")
        else:
            logger.info(" Orchestrator logs don't show task processing")
            logger.info("Recent orchestrator logs:")
            logger.info(log_content[-500:])  # Last 500 chars
    else:
        logger.info(f" Failed to get orchestrator logs: {logs_result.stderr}")
        return False

    # 6. Check queue status
    try:
        status_response = requests.get('http://localhost:3000/queue-status', timeout=5)
        if status_response.status_code == 200:
            status_data = status_response.json()
            logger.info(f" Task queue status: {status_data.get('pending_tasks', 0)} pending tasks")
        else:
            logger.info(" Queue status endpoint not responding properly")
    except requests.exceptions.RequestException as e:
        logger.info(f" Could not check queue status: {e}")

    return True

def check_service_logs():
    """Check logs from all services for errors"""
    logger.info("\n Checking service logs for errors...")

    services = ['redis', 'webhook', 'orchestrator', 'ngrok']

    for service in services:
        logger.info(f"\n {service.upper()} logs:")
        result = subprocess.run(
            ['docker-compose', 'logs', '--tail=10', service],
            capture_output=True, text=True
        )

        if result.returncode == 0:
            logs = result.stdout
            if logs.strip():
                # Look for common error indicators
                if any(word in logs.lower() for word in ['error', 'failed', 'exception', 'traceback']):
                    logger.info(" Errors found in logs:")
                    logger.info(logs)
                else:
                    logger.info(" No obvious errors in recent logs")
                    # Show last few lines anyway
                    lines = logs.strip().split('\n')
                    for line in lines[-3:]:
                        logger.info(f"   {line}")
            else:
                logger.info(" No recent logs")
        else:
            logger.info(f" Could not get logs: {result.stderr}")

def test_container_networking():
    """Test that containers can communicate with each other"""
    logger.info("\n🌐 Testing container networking...")

    # Test that orchestrator can reach Redis
    result = subprocess.run([
        'docker-compose', 'exec', '-T', 'orchestrator',
        'python', '-c', 'import redis; r=redis.Redis(host="redis", port=6379); logger.info("Redis ping:", r.ping())'
    ], capture_output=True, text=True)

    if result.returncode == 0 and 'True' in result.stdout:
        logger.info(" Orchestrator can reach Redis")
    else:
        logger.info(f" Orchestrator cannot reach Redis: {result.stderr}")
        return False

    # Test that webhook can reach Redis
    result = subprocess.run([
        'docker-compose', 'exec', '-T', 'webhook',
        'python', '-c', 'import redis; r=redis.Redis(host="redis", port=6379); logger.info("Redis ping:", r.ping())'
    ], capture_output=True, text=True)

    if result.returncode == 0 and 'True' in result.stdout:
        logger.info(" Webhook server can reach Redis")
    else:
        logger.info(f" Webhook server cannot reach Redis: {result.stderr}")
        return False

    return True

if __name__ == "__main__":
    logger.info(" Starting Docker deployment tests...\n")

    # Test networking first
    network_ok = test_container_networking()
    if not network_ok:
        logger.info("\n Container networking test FAILED!")
        sys.exit(1)

    # Test main deployment
    deployment_ok = test_docker_deployment()

    # Check logs for issues
    check_service_logs()

    if deployment_ok:
        logger.info("\n Docker deployment test PASSED!")
        logger.info("\n Next steps:")
        logger.info("   1. Configure your .env file with real tokens")
        logger.info("   2. Update config/projects.yaml with your repositories")
        logger.info("   3. Set up GitHub webhooks to point to your ngrok URL")
        logger.info("   4. Create GitHub project boards with proper column names")
        sys.exit(0)
    else:
        logger.info("\n Docker deployment test FAILED!")
        logger.info("\n Troubleshooting tips:")
        logger.info("   - Check that all required environment variables are set")
        logger.info("   - Verify Docker services are running with: docker-compose ps")
        logger.info("   - Check individual service logs with: docker-compose logs <service>")
        sys.exit(1)