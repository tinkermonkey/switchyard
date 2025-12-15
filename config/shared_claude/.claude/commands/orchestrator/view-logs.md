---
description: View orchestrator and agent logs
allowed-tools: Bash(docker:*), Bash(docker-compose:*), Bash(tail:*), Bash(grep:*), Bash(curl:*)
argument-hint: [service] [lines]
---

# View Orchestrator Logs

## Available Log Sources

!`docker-compose ps --services 2>/dev/null | grep -E "(orchestrator|redis|elasticsearch)" || docker ps --format "{{.Names}}" | grep orchestrator`

## Recent Orchestrator Logs

!`docker-compose logs --tail=50 orchestrator 2>/dev/null || docker logs orchestrator --tail=50 2>/dev/null`

## Claude Code Logs (Recent Agents)

!`curl -s http://localhost:5001/claude-logs-history | jq '.[0:3] | .[] | {agent: .agent, time: .timestamp, preview: .content[0:100]}'`

## Task

View logs for: $ARGUMENTS

**Log viewing options:**

1. **Orchestrator service logs**:
   ```bash
   docker-compose logs -f orchestrator --tail=100
   ```

2. **Specific agent logs**:
   ```bash
   # Via observability API
   curl -s http://localhost:5001/claude-logs-history | jq '.[] | select(.agent == "AGENT_NAME")'
   ```

3. **Service logs (Redis, Elasticsearch)**:
   ```bash
   docker-compose logs -f redis --tail=50
   docker-compose logs -f elasticsearch --tail=50
   ```

4. **Agent container logs**:
   ```bash
   docker logs <agent-container-name> --tail=100 -f
   ```

**Filter logs by:**
- Service: `orchestrator`, `redis`, `elasticsearch`, `agent`
- Level: `ERROR`, `WARNING`, `INFO`, `DEBUG`
- Time range: Last N minutes/hours
- Agent name: Specific agent type
- Project: Specific project name

**Arguments:**
- `$1`: Service name or agent name
- `$2`: Number of lines (default: 100)

**Instructions:**
1. If service specified, show logs for that service
2. If agent name specified, show Claude Code logs for that agent
3. Highlight errors and warnings
4. Show timestamps
5. Follow mode if requested (streaming logs)

**Output format:**
- Structured log entries with timestamp
- Color coding for levels (if terminal supports)
- Key information extracted (agent, task, issue)

Example: `/view-logs orchestrator 200` → Last 200 orchestrator logs
Example: `/view-logs senior_software_engineer` → All logs for that agent type
