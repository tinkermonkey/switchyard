# Scheduled Tasks - Feature Branch Maintenance

This document explains how periodic maintenance tasks run for the feature branch workflow.

## Overview

The orchestrator runs periodic maintenance tasks for feature branches:

1. **Orphaned Branch Cleanup** - Daily at 2 AM
   - Deletes branches for closed parent issues (7+ day grace period)
   - Posts notification to parent issue

2. **Stale Branch Warnings** - Daily at 9 AM
   - Checks how far behind main each branch is
   - Posts warnings for branches 50+ commits behind
   - Recommends rebase actions

## Architecture: APScheduler Service

The orchestrator uses **APScheduler** (Python scheduler) rather than cron for several reasons:

✅ **Integrated logging** - Uses existing orchestrator logging
✅ **Observability** - Can emit metrics and events
✅ **Simpler deployment** - No need to manage cron in Docker
✅ **Dynamic configuration** - Can adjust schedules without rebuilding
✅ **Testable** - Can trigger tasks manually

## Automatic Startup

The scheduler starts automatically when the orchestrator starts:

```python
# main.py
scheduler = get_scheduled_tasks_service()
scheduler.start()
logger.info("Scheduled tasks service started")
```

Logs on startup:
```
Scheduled tasks service started
- Orphaned branch cleanup: Daily at 2 AM
- Stale branch checks: Daily at 9 AM
```

## Manual Triggers

### Via Makefile

Run cleanup for all projects:
```bash
make cleanup-branches
```

Run cleanup for specific project:
```bash
make cleanup-project PROJECT=context-studio
```

### Via Script Directly

All projects:
```bash
python scripts/cleanup_orphaned_branches.py
```

Specific project:
```bash
python scripts/cleanup_orphaned_branches.py --project context-studio
```

### Via Python API

```python
from services.scheduled_tasks import get_scheduled_tasks_service

scheduler = get_scheduled_tasks_service()

# Trigger cleanup now
scheduler.run_cleanup_now()

# Trigger stale check now
scheduler.run_stale_check_now()
```

## Customizing Schedules

Edit `services/scheduled_tasks.py`:

```python
# Change cleanup to 3 AM
self.scheduler.add_job(
    self._cleanup_orphaned_branches,
    trigger=CronTrigger(hour=3, minute=0),  # Changed from 2 to 3
    id='cleanup_orphaned_branches',
    name='Cleanup orphaned feature branches',
    replace_existing=True
)

# Run stale checks twice daily
self.scheduler.add_job(
    self._check_stale_branches,
    trigger=CronTrigger(hour='9,15', minute=0),  # 9 AM and 3 PM
    id='check_stale_branches',
    name='Check for stale feature branches',
    replace_existing=True
)
```

## Docker Deployment

The scheduler runs inside the main orchestrator container - no special Docker configuration needed.

If you want to run cleanup as a **separate cron container** (alternative approach):

### docker-compose.yml
```yaml
services:
  orchestrator:
    # ... existing config ...

  # Optional: Separate cron container
  cleanup-cron:
    build: .
    command: |
      sh -c 'echo "0 2 * * * cd /app && python scripts/cleanup_orphaned_branches.py >> /var/log/cleanup.log 2>&1" | crontab - && crond -f'
    volumes:
      - ./:/app
      - ..:/workspace
    environment:
      - GITHUB_TOKEN=${GITHUB_TOKEN}
```

**Note:** The integrated APScheduler approach (default) is recommended over this.

## Monitoring

Scheduled tasks emit logs you can monitor:

```bash
# Watch logs for scheduled task activity
docker-compose logs -f orchestrator | grep "scheduled\|cleanup\|stale"
```

Example output:
```
2025-01-05 02:00:00 - Cleaning up orphaned branches for project: context-studio
2025-01-05 02:00:05 - Deleted orphaned branch feature/issue-100-old-feature (parent closed 8 days ago)
2025-01-05 02:00:10 - Orphaned branch cleanup complete: 3 projects processed, 0 errors

2025-01-05 09:00:00 - Starting scheduled stale branch check
2025-01-05 09:00:03 - Escalated stale branch feature/issue-50-auth: 65 commits behind
2025-01-05 09:00:10 - Stale branch check complete: 1 warnings posted, 0 errors
```

## Kubernetes Deployment (Alternative)

If running in Kubernetes, you could use **CronJob** resources instead:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: cleanup-orphaned-branches
spec:
  schedule: "0 2 * * *"  # 2 AM daily
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: cleanup
            image: orchestrator:latest
            command: ["python", "scripts/cleanup_orphaned_branches.py"]
            env:
              - name: GITHUB_TOKEN
                valueFrom:
                  secretKeyRef:
                    name: github-secrets
                    key: token
          restartPolicy: OnFailure
```

**Note:** The integrated APScheduler approach works in both Docker Compose and Kubernetes.

## Health Checks

The scheduler service integrates with the orchestrator's health monitoring:

```python
# Future enhancement: Add to health_monitor.py
def check_scheduler_health(self):
    scheduler = get_scheduled_tasks_service()
    return {
        'running': scheduler.running,
        'next_cleanup': scheduler.scheduler.get_job('cleanup_orphaned_branches').next_run_time,
        'next_stale_check': scheduler.scheduler.get_job('check_stale_branches').next_run_time
    }
```

## Troubleshooting

### Tasks not running

1. Check if scheduler started:
   ```bash
   docker-compose logs orchestrator | grep "Scheduled tasks service started"
   ```

2. Manually trigger to test:
   ```bash
   docker-compose exec orchestrator make cleanup-branches
   ```

### APScheduler dependency missing

```bash
pip install apscheduler>=3.10.4
```

### Timezone issues

APScheduler uses system timezone by default. To set explicitly:

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

self.scheduler = AsyncIOScheduler(timezone=pytz.timezone('America/Los_Angeles'))
```

## Summary

| Approach | Pros | Cons | Recommended |
|----------|------|------|-------------|
| **APScheduler (default)** | Integrated, observable, testable | Runs in main process | ✅ Yes |
| Docker cron | Separate process | Complex, poor logging | ❌ No |
| Kubernetes CronJob | Native K8s, separate pods | Only for K8s | ⚠️ If on K8s |
| GitHub Actions | Serverless, no infrastructure | Needs repo access | ⚠️ For CI/CD |

The **APScheduler** approach is recommended for most deployments.
