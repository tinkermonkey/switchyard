# Diagnostic Scripts

Three diagnostic scripts for debugging pipeline runs and agent execution issues.

## Overview

These scripts provide deep visibility into the orchestrator's runtime state:

1. **inspect_pipeline_timeline.py** - Visualize pipeline execution timeline
2. **inspect_task_health.py** - Monitor task queue health
3. **inspect_checkpoint.py** - Inspect checkpoint recovery state

All scripts are designed to run inside the orchestrator Docker container where dependencies are available.

---

## inspect_pipeline_timeline.py

Visualizes pipeline execution as a chronological timeline showing stage transitions, agent assignments, decision points, and bottlenecks.

### Usage

```bash
# Basic usage - show timeline for a pipeline run
docker-compose exec orchestrator python scripts/inspect_pipeline_timeline.py <pipeline_run_id>

# Verbose output with full event details
docker-compose exec orchestrator python scripts/inspect_pipeline_timeline.py <pipeline_run_id> --verbose

# JSON output for programmatic processing
docker-compose exec orchestrator python scripts/inspect_pipeline_timeline.py <pipeline_run_id> --json
```

### Finding Pipeline Run IDs

```bash
# Query Elasticsearch for recent pipeline runs
docker-compose exec orchestrator bash -c "curl -s 'http://localhost:9200/pipeline-runs-*/_search?size=5&sort=started_at:desc' | jq -r '.hits.hits[]._source | \"\(.id) - Issue #\(.issue_number): \(.issue_title)\"'"
```

### Example Output

```
Pipeline Run Timeline: a1b2c3d4-e5f6-7890-abcd-ef1234567890
Issue: #42 - "Implement user authentication"
Project: context-studio | Board: Development Pipeline
Started: 2025-01-25 10:30:15 | Ended: 2025-01-25 11:45:32 | Duration: 1h 15m
Status: completed

Timeline:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[10:30:15] ▶ PIPELINE RUN STARTED
           Project: context-studio, Board: Development Pipeline

[10:30:18] ⚙ AGENT ROUTING DECISION
           Selected: business_analyst
           Reason: Initial requirements gathering

[10:30:20] ▶ AGENT INITIALIZED: business_analyst
           Task: task_001
           Container: ba-context-studio-12345

[10:45:33] ✓ AGENT COMPLETED: business_analyst
           Duration: 15m 13s
           Success: true

[10:45:35] ⚙ STATUS PROGRESSION STARTED
           From: Backlog → To: Analysis

...

Summary:
  Total Agents: 2 (business_analyst x2, tech_lead_reviewer x1)
  Review Cycles: 1 (2 iterations)
  Status Changes: 1 (Backlog → Analysis)
  Errors: 0
```

### What It Shows

- **Event Timeline**: Chronological sequence of all pipeline events
- **Agent Execution**: Start/completion times and durations
- **Decision Points**: Agent routing, status progression, review cycles
- **Review Cycles**: Iterations and feedback loops
- **Errors**: Failed agents or decision events
- **Summary**: Statistics on agents used, review cycles, status changes

### Use Cases

- **Debug Bottlenecks**: Identify which agents take longest
- **Understand Review Cycles**: See how many iterations were needed
- **Trace Failures**: Find exactly when and why a pipeline failed
- **Audit Pipeline**: Review complete execution history

---

## inspect_task_health.py

Monitors task queue health by detecting stuck tasks, analyzing queue depth trends, and identifying retry patterns.

### Usage

```bash
# Check current task queue health
docker-compose exec orchestrator python scripts/inspect_task_health.py

# Show all tasks (not just stuck ones)
docker-compose exec orchestrator python scripts/inspect_task_health.py --show-all

# Filter by project
docker-compose exec orchestrator python scripts/inspect_task_health.py --project context-studio

# JSON output for monitoring systems
docker-compose exec orchestrator python scripts/inspect_task_health.py --json

# Custom age thresholds (in seconds)
docker-compose exec orchestrator python scripts/inspect_task_health.py \
  --high-threshold 3600 \
  --medium-threshold 7200 \
  --low-threshold 14400
```

### Example Output

```
Task Queue Health Report
Generated: 2025-01-25 14:30:00
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Queue Depth Summary:
  High Priority:   3 tasks
  Medium Priority: 12 tasks
  Low Priority:    5 tasks
  Total:           20 tasks

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚠️  STUCK TASKS DETECTED: 2

[HIGH] Task: task_12345
  Agent: senior_software_engineer
  Project: context-studio
  Issue: #42
  Age: 45 minutes (threshold: 30 minutes)
  Created: 2025-01-25 13:45:00
  Status: pending
  ⚠️  STUCK - Exceeds age threshold by 15 minutes

...

Distribution by Project:
  context-studio:           10 tasks (50%)
  documentation_robotics:   8 tasks (40%)

Distribution by Agent:
  senior_software_engineer: 8 tasks (40%)
  code_reviewer:            5 tasks (25%)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Health Status: ⚠️  WARNING - 2 stuck tasks detected

Recommendations:
  - Investigate stuck HIGH priority task (task_12345)
  - Check if orchestrator is processing tasks
  - Verify agent containers are running: docker ps
  - Check circuit breakers: python scripts/inspect_circuit_breakers.py
```

### Exit Codes

- **0**: Healthy (no stuck tasks)
- **1**: Warning (some stuck tasks detected)
- **2**: Critical (many stuck tasks or Redis connection error)

### Default Age Thresholds

- **HIGH priority**: 30 minutes (1800 seconds)
- **MEDIUM priority**: 1 hour (3600 seconds)
- **LOW priority**: 4 hours (14400 seconds)

### What It Shows

- **Queue Depth**: Number of tasks in each priority queue
- **Stuck Tasks**: Tasks exceeding age thresholds
- **Distribution**: Breakdown by project and agent
- **Health Status**: Overall queue health assessment
- **Recommendations**: Actions to resolve issues

### Use Cases

- **Proactive Monitoring**: Detect queue backups before they cause problems
- **Identify Bottlenecks**: See which agents have the most queued work
- **Troubleshoot Stuck Tasks**: Find tasks that aren't being processed
- **Capacity Planning**: Understand workload distribution

### Integration with Monitoring

The script returns appropriate exit codes and supports JSON output, making it suitable for integration with monitoring systems like Nagios, Prometheus, or custom dashboards:

```bash
# Use in monitoring script
if ! docker-compose exec -T orchestrator python scripts/inspect_task_health.py --json > /tmp/queue_health.json; then
    echo "Queue health check failed!"
    # Send alert
fi
```

---

## inspect_checkpoint.py

Shows checkpoint files for a pipeline, verifies recovery state, and visualizes stage completion status.

### Usage

```bash
# List recent checkpoints across all pipelines
docker-compose exec orchestrator python scripts/inspect_checkpoint.py

# Inspect specific pipeline
docker-compose exec orchestrator python scripts/inspect_checkpoint.py <pipeline_run_id>

# Show full context JSON
docker-compose exec orchestrator python scripts/inspect_checkpoint.py <pipeline_run_id> --show-context

# Verify recovery readiness
docker-compose exec orchestrator python scripts/inspect_checkpoint.py <pipeline_run_id> --verify-recovery

# Custom checkpoint directory
docker-compose exec orchestrator python scripts/inspect_checkpoint.py <pipeline_run_id> \
  --checkpoints-dir /custom/path/checkpoints
```

### Finding Checkpoint Pipeline IDs

```bash
# List checkpoint files
docker-compose exec orchestrator ls orchestrator_data/state/checkpoints/ | head -5

# Extract pipeline ID from checkpoint filename
docker-compose exec orchestrator bash -c "ls orchestrator_data/state/checkpoints/ | head -1 | cut -d'_' -f1-4"
```

### Example Output

```
Checkpoint Inspection: a1b2c3d4-e5f6-7890-abcd-ef1234567890
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Pipeline Run Information:
  Issue: #42 - "Implement user authentication"
  Project: context-studio
  Board: Development Pipeline
  Started: 2025-01-25 10:30:15
  Status: completed

Checkpoint Files Found: 3
  ✓ Stage 0: orchestrator_data/state/checkpoints/a1b2c3d4_stage_0.json
  ✓ Stage 1: orchestrator_data/state/checkpoints/a1b2c3d4_stage_1.json
  ✓ Stage 2: orchestrator_data/state/checkpoints/a1b2c3d4_stage_2.json

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Latest Checkpoint (Stage 2):
  Created: 2025-01-25 10:45:30
  Age: 1 hour 15 minutes

Context Summary:
  Project: context-studio
  Issue: #42
  Board: Development Pipeline
  Previous Stage Output: 2,450 characters
  Conversation History: 5 turns
  Metrics: {success: true, duration_ms: 912345}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Stage Progression:
  [✓] Stage 0 - Completed at 2025-01-25 10:35:20 (checkpoint saved)
  [✓] Stage 1 - Completed at 2025-01-25 10:40:15 (checkpoint saved)
  [✓] Stage 2 - Completed at 2025-01-25 10:45:30 (checkpoint saved)
  [ ] Stage 3 - Not reached

Recovery Readiness: ✓ READY
  If pipeline crashes, will resume from Stage 2
  Context is serializable and complete

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Recommendations:
  ✓ Checkpoints are present and valid
  ✓ Latest checkpoint is recent
  ✓ Recovery context is complete
```

### What It Shows

- **Pipeline Context**: Issue number, project, board, status
- **Checkpoint Files**: All checkpoint files by stage
- **Latest Checkpoint**: Most recent checkpoint details
- **Context Summary**: Key context fields (project, issue, outputs, metrics)
- **Stage Progression**: Timeline of completed stages
- **Recovery Verification**: Whether pipeline can resume from checkpoint
- **Recommendations**: Health status and potential issues

### Use Cases

- **Verify Recovery**: Ensure pipelines can resume after crashes
- **Debug Resume Failures**: Identify why a pipeline won't recover
- **Audit Checkpoints**: Confirm checkpoints are being saved correctly
- **Investigate Stale State**: Find old checkpoints that should be cleaned up

### Recovery Verification

The `--verify-recovery` flag performs several checks:

- **Required Fields**: Ensures all mandatory fields are present
- **Context Completeness**: Verifies context has necessary data
- **JSON Serializability**: Tests that checkpoint can be serialized/deserialized
- **Staleness**: Warns if checkpoint is > 24 hours old

---

## Testing

A test script is provided to verify all diagnostic scripts work correctly:

```bash
docker-compose exec orchestrator python scripts/test_diagnostic_scripts.py
```

This runs unit tests for:
- Checkpoint file reading and verification
- Task age calculation and stuck task detection
- Event timeline formatting and duration calculation

---

## Common Workflows

### Debugging a Failed Pipeline Run

1. Find the pipeline run ID from Elasticsearch or logs
2. Visualize the timeline to see what happened:
   ```bash
   docker-compose exec orchestrator python scripts/inspect_pipeline_timeline.py <run_id>
   ```
3. Check if there are checkpoints (for recovery):
   ```bash
   docker-compose exec orchestrator python scripts/inspect_checkpoint.py <run_id>
   ```
4. Verify task queue isn't backed up:
   ```bash
   docker-compose exec orchestrator python scripts/inspect_task_health.py
   ```

### Investigating Queue Backlog

1. Check queue health:
   ```bash
   docker-compose exec orchestrator python scripts/inspect_task_health.py
   ```
2. If stuck tasks are found, check circuit breakers:
   ```bash
   docker-compose exec orchestrator python scripts/inspect_circuit_breakers.py
   ```
3. Verify agent containers are running:
   ```bash
   docker ps | grep agent
   ```
4. Check orchestrator logs:
   ```bash
   docker-compose logs -f orchestrator
   ```

### Monitoring Pipeline Health

Set up a cron job or monitoring script:

```bash
#!/bin/bash
# Queue health monitoring (run every 5 minutes)

HEALTH_FILE="/tmp/queue_health.json"

docker-compose exec -T orchestrator python scripts/inspect_task_health.py --json > "$HEALTH_FILE"

EXIT_CODE=$?

if [ $EXIT_CODE -eq 2 ]; then
    echo "CRITICAL: Queue health check failed"
    # Send alert
elif [ $EXIT_CODE -eq 1 ]; then
    echo "WARNING: Stuck tasks detected"
    # Send warning
else
    echo "OK: Queue healthy"
fi
```

---

## Troubleshooting

### Connection Errors

If you see "Connection refused" errors:

1. **Verify services are running**:
   ```bash
   docker-compose ps
   ```

2. **Check Redis**:
   ```bash
   docker-compose exec orchestrator redis-cli -h redis ping
   ```

3. **Check Elasticsearch**:
   ```bash
   docker-compose exec orchestrator curl -s http://elasticsearch:9200
   ```

### No Data Found

If scripts report no data:

1. **Verify orchestrator is processing tasks**:
   ```bash
   docker-compose logs orchestrator | tail -50
   ```

2. **Check if issues are being moved on GitHub Projects** (triggers pipeline runs)

3. **Verify Elasticsearch indices exist**:
   ```bash
   docker-compose exec orchestrator curl -s http://elasticsearch:9200/_cat/indices
   ```

### Import Errors

Scripts must run inside the Docker container where dependencies are installed:

```bash
# Wrong (missing dependencies)
python scripts/inspect_task_health.py

# Correct (inside container)
docker-compose exec orchestrator python scripts/inspect_task_health.py
```

---

## Related Scripts

- **inspect_run_details.py**: Queries pipeline run from Redis and Elasticsearch
- **inspect_queue.py**: Simple queue inspection (predecessor to inspect_task_health.py)
- **inspect_circuit_breakers.py**: Shows circuit breaker states
- **cleanup_orphaned_branches.py**: Cleans up stale feature branches

---

## Contributing

When modifying these scripts:

1. Maintain backward compatibility with existing flags
2. Add tests to `test_diagnostic_scripts.py`
3. Update this documentation
4. Ensure scripts work both inside and outside containers (when dependencies available)
5. Follow existing error handling patterns
6. Keep exit codes consistent (0=success, 1=warning, 2=error)
