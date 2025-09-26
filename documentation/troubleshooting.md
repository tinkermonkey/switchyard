# Troubleshooting Guide

## Quick Diagnostics

### 1. Configuration Validation
```bash
python tests/triage_scripts/validate_configuration.py
```

### 2. System Health Check
```bash
python tests/triage_scripts/test_production_readiness.py
```

### 3. Service Status Check
```bash
# Docker deployment
docker-compose ps

# Manual deployment
ps aux | grep -E "(python|redis|ngrok)"
```

## Common Issues

### Orchestrator Won't Start

#### Issue: `ModuleNotFoundError`
**Symptoms:**
```
ImportError: No module named 'config.environment'
```

**Solutions:**
1. Check Python path:
   ```bash
   export PYTHONPATH="$PWD:$PYTHONPATH"
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Check virtual environment:
   ```bash
   which python
   source .venv/bin/activate
   ```

#### Issue: Environment Configuration Error
**Symptoms:**
```
pydantic.error_wrappers.ValidationError: 1 validation error for Environment
```

**Solutions:**
1. Check `.env` file exists and has required variables:
   ```bash
   ls -la .env
   cat .env | grep -E "(ANTHROPIC_API_KEY|GITHUB_TOKEN|WEBHOOK_SECRET)"
   ```

2. Validate environment configuration:
   ```bash
   python -c "from config.environment import Environment; print('Config valid')"
   ```

3. Check for typos in environment variable names

### Redis Connection Issues

#### Issue: Redis Connection Refused
**Symptoms:**
```
redis.exceptions.ConnectionError: Error 61 connecting to localhost:6379. Connection refused.
```

**Solutions:**
1. Check Redis is running:
   ```bash
   redis-cli ping
   # Should return: PONG
   ```

2. Start Redis:
   ```bash
   # Docker
   docker-compose up redis -d

   # Manual
   redis-server
   ```

3. Check Redis configuration in `.env`:
   ```bash
   grep REDIS_URL .env
   ```

#### Issue: Redis Authentication Failed
**Symptoms:**
```
redis.exceptions.AuthenticationError: Authentication required.
```

**Solutions:**
1. Add Redis password to `.env`:
   ```bash
   echo "REDIS_PASSWORD=your_password" >> .env
   ```

2. Update Redis URL:
   ```bash
   REDIS_URL=redis://:password@localhost:6379
   ```

### GitHub Integration Issues

#### Issue: Webhooks Not Received
**Symptoms:**
- GitHub shows webhook delivery failures
- No logs in orchestrator for new issues

**Diagnosis:**
```bash
# Check webhook server status
curl http://localhost:3000/health

# Check ngrok tunnel
curl http://localhost:4040/api/tunnels
```

**Solutions:**
1. Verify webhook URL in GitHub:
   ```bash
   gh api repos/owner/repo/hooks
   ```

2. Check webhook secret matches:
   ```bash
   grep WEBHOOK_SECRET .env
   ```

3. Test webhook endpoint locally:
   ```bash
   curl -X POST http://localhost:3000/github-webhook \
     -H "Content-Type: application/json" \
     -d '{"action":"test"}'
   ```

#### Issue: GitHub API Rate Limiting
**Symptoms:**
```
github.GithubException.RateLimitExceededException: 403 {'message': 'API rate limit exceeded'}
```

**Solutions:**
1. Check rate limit status:
   ```bash
   gh api rate_limit
   ```

2. Use authenticated requests (check token):
   ```bash
   gh auth status
   ```

3. Implement request caching or reduce API calls

### Agent Execution Issues

#### Issue: Agent Timeout
**Symptoms:**
```
asyncio.exceptions.TimeoutError: Agent execution timed out
```

**Solutions:**
1. Increase timeout in pipeline configuration:
   ```yaml
   agents:
     - name: business_analyst
       timeout: 600  # Increase from 300
   ```

2. Check Claude API connectivity:
   ```bash
   curl -H "Authorization: Bearer $ANTHROPIC_API_KEY" \
     https://api.anthropic.com/v1/messages
   ```

3. Monitor agent resource usage:
   ```bash
   python tests/integration/test_performance.py
   ```

#### Issue: Circuit Breaker Activated
**Symptoms:**
```
Exception: Circuit breaker is OPEN (failures: 3)
```

**Solutions:**
1. Check recent failure logs:
   ```bash
   grep -A5 -B5 "Circuit breaker" orchestrator.log
   ```

2. Wait for recovery timeout or manually reset:
   ```python
   from resilience.circuit_breaker import CircuitBreaker
   cb = CircuitBreaker()
   cb._state = "closed"  # Manual reset
   ```

3. Adjust circuit breaker settings:
   ```yaml
   circuit_breaker:
     failure_threshold: 5  # Increase threshold
     recovery_timeout: 300  # Increase timeout
   ```

### Performance Issues

#### Issue: High Memory Usage
**Symptoms:**
- System becomes slow
- Out of memory errors

**Diagnosis:**
```bash
# Check memory usage
python tests/integration/test_performance.py

# Monitor system resources
top
htop
```

**Solutions:**
1. Implement garbage collection:
   ```python
   import gc
   gc.collect()
   ```

2. Reduce task queue size:
   ```bash
   redis-cli FLUSHDB  # Clear Redis queues
   ```

3. Restart orchestrator periodically:
   ```bash
   docker-compose restart orchestrator
   ```

#### Issue: Slow Task Processing
**Symptoms:**
- Tasks sitting in queue for long time
- Low task throughput

**Diagnosis:**
```bash
# Check task queue status
redis-cli LLEN tasks:high
redis-cli LLEN tasks:medium
redis-cli LLEN tasks:low

# Run performance tests
python tests/integration/test_performance.py
```

**Solutions:**
1. Scale horizontally (add more orchestrator instances)
2. Optimize agent code
3. Increase concurrent processing:
   ```yaml
   orchestrator:
     environment:
       MAX_CONCURRENT_TASKS: 5
   ```

### Docker Issues

#### Issue: Container Won't Start
**Symptoms:**
```
docker-compose ps shows "Exit 1" status
```

**Diagnosis:**
```bash
# Check container logs
docker-compose logs orchestrator

# Check Docker resources
docker system df
```

**Solutions:**
1. Rebuild containers:
   ```bash
   docker-compose down
   docker-compose build --no-cache
   docker-compose up -d
   ```

2. Fix file permissions:
   ```bash
   sudo chown -R $(id -u):$(id -g) orchestrator_data/
   ```

3. Clean up Docker resources:
   ```bash
   docker system prune -f
   docker volume prune -f
   ```

#### Issue: Volume Mount Problems
**Symptoms:**
- File not found errors
- Permission denied errors

**Solutions:**
1. Check volume mounts in `docker-compose.yml`:
   ```yaml
   volumes:
     - ./orchestrator_data:/app/orchestrator_data
     - ./projects:/app/projects
   ```

2. Create directories:
   ```bash
   mkdir -p orchestrator_data/state/checkpoints
   mkdir -p projects
   ```

3. Fix permissions:
   ```bash
   chmod 755 orchestrator_data/
   ```

## Debugging Tools

### Log Analysis

#### View Recent Errors
```bash
# Last 100 lines with errors
tail -100 orchestrator.log | grep -E "(ERROR|Exception)"

# Real-time error monitoring
tail -f orchestrator.log | grep --color -E "(ERROR|WARN|Exception)"
```

#### Agent-specific Logs
```bash
# Filter by agent
grep "business_analyst" orchestrator.log

# Filter by task ID
grep "task_123" orchestrator.log
```

### Health Monitoring

#### Check All Services
```bash
#!/bin/bash
echo "=== Service Health Check ==="

# Orchestrator
curl -s http://localhost:8000/metrics || echo "❌ Orchestrator metrics not available"

# Webhook server
curl -s http://localhost:3000/health || echo "❌ Webhook server not available"

# Redis
redis-cli ping || echo "❌ Redis not available"

# ngrok
curl -s http://localhost:4040/api/tunnels || echo "❌ ngrok not available"

echo "=== Docker Services ==="
docker-compose ps
```

#### Resource Usage Monitoring
```bash
# System resources
echo "=== System Resources ==="
free -h
df -h

# Docker resources
echo "=== Docker Resources ==="
docker stats --no-stream

# Process monitoring
echo "=== Process Usage ==="
ps aux | grep -E "(python|redis)" | grep -v grep
```

### Performance Profiling

#### Memory Profiling
```python
# Add to agent code for profiling
import tracemalloc
tracemalloc.start()

# ... agent code ...

current, peak = tracemalloc.get_traced_memory()
print(f"Current memory usage: {current / 1024 / 1024:.1f} MB")
print(f"Peak memory usage: {peak / 1024 / 1024:.1f} MB")
tracemalloc.stop()
```

#### Timing Analysis
```python
import time
from contextlib import contextmanager

@contextmanager
def timer(description):
    start = time.time()
    yield
    elapsed = time.time() - start
    print(f"{description}: {elapsed:.2f}s")

# Usage
with timer("Agent execution"):
    result = await agent_function()
```

## Recovery Procedures

### Emergency Recovery

#### Complete System Reset
```bash
#!/bin/bash
echo "🚨 Emergency system reset..."

# Stop all services
docker-compose down

# Clean Docker resources
docker system prune -f
docker volume prune -f

# Reset Redis data
rm -rf orchestrator_data/redis/

# Clear log files
> orchestrator.log

# Restart services
docker-compose up -d

echo "✅ System reset complete"
```

#### Checkpoint Recovery
```bash
# Restore from specific checkpoint
python -c "
from state_management.manager import StateManager
import asyncio

async def recover():
    sm = StateManager()
    checkpoint = await sm.get_latest_checkpoint('pipeline_123')
    print(f'Latest checkpoint: {checkpoint}')

asyncio.run(recover())
"
```

### Data Recovery

#### Backup Critical Data
```bash
#!/bin/bash
BACKUP_DIR="backups/$(date +%Y%m%d_%H%M%S)"
mkdir -p $BACKUP_DIR

# Backup orchestrator data
cp -r orchestrator_data/ $BACKUP_DIR/

# Backup configuration
cp -r config/ $BACKUP_DIR/
cp .env $BACKUP_DIR/env.backup

# Backup Redis data
docker exec redis redis-cli BGSAVE
docker cp redis:/data/dump.rdb $BACKUP_DIR/

echo "✅ Backup created: $BACKUP_DIR"
```

#### Restore from Backup
```bash
#!/bin/bash
BACKUP_DIR=$1

if [ -z "$BACKUP_DIR" ]; then
    echo "Usage: $0 <backup_directory>"
    exit 1
fi

echo "🔄 Restoring from $BACKUP_DIR..."

# Stop services
docker-compose down

# Restore data
cp -r $BACKUP_DIR/orchestrator_data/ ./
cp -r $BACKUP_DIR/config/ ./
cp $BACKUP_DIR/env.backup .env

# Restore Redis
docker-compose up redis -d
sleep 5
docker cp $BACKUP_DIR/dump.rdb redis:/data/
docker-compose restart redis

# Start all services
docker-compose up -d

echo "✅ Restore complete"
```

## Monitoring and Alerting

### Health Check Script
```bash
#!/bin/bash
# health_check.sh

ALERT_EMAIL="admin@example.com"
LOG_FILE="health_check.log"

check_service() {
    local service=$1
    local url=$2
    local name=$3

    if curl -s -f "$url" > /dev/null; then
        echo "✅ $name: OK" | tee -a $LOG_FILE
        return 0
    else
        echo "❌ $name: FAILED" | tee -a $LOG_FILE
        return 1
    fi
}

# Run checks
failed_checks=0

check_service "orchestrator" "http://localhost:8000/metrics" "Orchestrator" || ((failed_checks++))
check_service "webhook" "http://localhost:3000/health" "Webhook Server" || ((failed_checks++))

# Check Redis
if redis-cli ping > /dev/null 2>&1; then
    echo "✅ Redis: OK" | tee -a $LOG_FILE
else
    echo "❌ Redis: FAILED" | tee -a $LOG_FILE
    ((failed_checks++))
fi

# Alert if failures
if [ $failed_checks -gt 0 ]; then
    echo "🚨 $failed_checks services failed" | mail -s "Orchestrator Health Alert" $ALERT_EMAIL
fi
```

## FAQ

### Q: How do I restart a stuck task?
**A:** Find the task in Redis and remove it, then recreate:
```bash
redis-cli LREM tasks:high 1 "stuck_task_id"
# Then recreate the issue in GitHub or manually enqueue
```

### Q: How do I clear all queued tasks?
**A:**
```bash
redis-cli FLUSHDB  # Clears all Redis data
# Or selectively:
redis-cli DEL tasks:high tasks:medium tasks:low
```

### Q: How do I check circuit breaker status?
**A:** Add logging to your agent or check in Python:
```python
from pipeline.resilient_pipeline import ResilientPipeline
# Check pipeline health report
pipeline.get_health_report()
```

### Q: How do I update agent configuration without restart?
**A:** Currently requires restart. Edit `config/pipelines.yaml` then:
```bash
docker-compose restart orchestrator
```

### Q: How do I scale to handle more load?
**A:**
1. Increase Docker resource limits
2. Run multiple orchestrator instances
3. Use Redis cluster for scaling
4. Optimize agent processing time

## Support

For additional help:
1. Check logs: `orchestrator.log`
2. Run diagnostics: `python tests/triage_scripts/validate_configuration.py`
3. Run health tests: `python tests/triage_scripts/test_production_readiness.py`
4. Monitor resources with provided scripts
5. Review configuration files for inconsistencies