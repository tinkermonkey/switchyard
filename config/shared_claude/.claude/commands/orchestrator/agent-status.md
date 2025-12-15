---
description: View status of running and recent agents
allowed-tools: Bash(curl:*), Bash(docker:*), Bash(jq:*)
argument-hint: [agent-name]
---

# Agent Status

## Active Agents

!`curl -s http://localhost:5001/agents/active | jq '.agents // []'`

## Agent Execution History

!`curl -s http://localhost:5001/history | jq '.[0:10]'`

## Active Pipeline Runs

!`curl -s http://localhost:5001/active-pipeline-runs | jq '.'`

## Docker Containers (Agent Processes)

!`docker ps --filter "label=orchestrator.agent" --format "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}"`

## Task

Show detailed status for agent: $ARGUMENTS

**Instructions:**

1. **Active Agents**:
   - Agent name and type
   - Project working on
   - Task ID and issue number
   - Duration running
   - Current status

2. **Recent Completions**:
   - Last 10 completed agents
   - Success/failure status
   - Duration
   - Output summary

3. **Running Pipelines**:
   - Pipeline ID and name
   - Current stage
   - Progress (e.g., "3/5 stages complete")
   - Estimated completion

4. **Performance Metrics**:
   - Average execution time by agent type
   - Success rate
   - Circuit breaker status

**If specific agent name provided:**
- Show detailed history for that agent
- Recent executions
- Success rate
- Common failure modes
- Circuit breaker status

**Output format:**
```
ACTIVE AGENTS (2)
─────────────────────────────────────────
• business_analyst | context-studio | ISSUE-123
  Started: 2m ago | Status: Running

RECENT COMPLETIONS (10)
─────────────────────────────────────────
✓ senior_software_engineer | 5m ago | Success
✗ code_reviewer | 12m ago | Failed: Timeout
```
