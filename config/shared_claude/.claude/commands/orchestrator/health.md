---
description: Check orchestrator system health and status
allowed-tools: Bash(curl:*), Bash(docker:*), Bash(docker-compose:*)
---

# Orchestrator Health Check

## System Health API

!`curl -s http://localhost:5001/health 2>/dev/null | jq '.' || echo "Observability server not responding"`

## Active Agents

!`curl -s http://localhost:5001/agents/active 2>/dev/null | jq '.' || echo "Cannot fetch active agents"`

## Circuit Breaker Status

!`curl -s http://localhost:5001/api/circuit-breakers 2>/dev/null | jq '.' || echo "Cannot fetch circuit breakers"`

## Docker Services

!`docker-compose ps 2>/dev/null || docker ps --filter "name=orchestrator"`

## Recent Agent History

!`curl -s http://localhost:5001/history 2>/dev/null | jq '.[0:5]' || echo "Cannot fetch history"`

## Task

Analyze the orchestrator system health and report:

1. **Overall Status**: Healthy, degraded, or down
2. **Component Health**:
   - Orchestrator service
   - Redis (task queue)
   - Elasticsearch (metrics)
   - GitHub API connection
3. **Active Agents**: Count and status
4. **Circuit Breakers**: Any open/half-open breakers?
5. **Recent Activity**: Last 5 agent executions
6. **Issues**: Any errors or warnings

**Provide recommendations:**
- Services that need restart
- Circuit breakers to reset
- Configuration issues
- Performance concerns

Format output as a health dashboard with clear status indicators (✓, ⚠, ✗).
