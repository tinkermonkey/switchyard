# Claude Medic Component - Comprehensive Implementation Plan

## Executive Summary

The Claude Medic component extends the existing Medic system to monitor Claude Code execution failures within the Clauditoreum orchestrator. Unlike the Docker log monitor which tracks container-level errors, Claude Medic analyzes tool execution failures from the `claude-streams-*` Elasticsearch indices to identify patterns where Claude Code agents struggle with specific tasks, enabling targeted improvements to agent configurations, CLAUDE.md instructions, and Claude Code skills.

**Key Differentiators:**
- **Source**: Elasticsearch `claude-streams-*` indices (not Docker logs)
- **Scope**: Project-specific failure signatures (each codebase has unique solutions)
- **Clustering**: Contiguous failure grouping - any success breaks the cluster
- **Recommendations**: Agent refinement (sub-agents, CLAUDE.md updates, Claude Code skills)
- **Storage**: `/medic/claude/{fingerprint_id}/` (separate from Docker log investigations)

**Clustering Example:**
```
Session: abc-123, Project: my_project
10:00 - Bash fails (npm install)         ┐
10:01 - Bash fails (npm install)         ├─ Cluster 1 (3 failures)
10:02 - Bash fails (npm run build)       ┘
10:03 - Read succeeds (package.json)     ← SUCCESS BREAKS CLUSTER
10:04 - Bash fails (npm test)            ┐
10:05 - Bash fails (npm test)            ├─ Cluster 2 (2 failures)
10:06 - Edit succeeds (test.js)          ← SUCCESS BREAKS CLUSTER
10:07 - Bash succeeds (npm test)         ← No cluster (isolated success)

Result: 2 clusters fingerprinted, not 5 individual failures
```

---

## Architecture Overview

### Data Flow

```
Elasticsearch (claude-streams-*)
    ↓
Claude Failure Monitor (periodic queries)
    ↓
Failure Clustering Engine (groups sequential failures)
    ↓
Fingerprint Engine (project-scoped signatures)
    ↓
Failure Signature Store (ES: medic-claude-failures-*)
    ↓
Auto-Trigger Logic (threshold-based)
    ↓
Investigation Queue → Claude Code Investigator → Reports (/medic/claude/{fingerprint_id}/)
    ↓
REST API → Web UI (Claude Medic Tab)
```

### Components

**Phase 1: Detection & Visibility**
- Claude Failure Monitor: Queries Elasticsearch for failed tool executions
- Failure Clustering Engine: Groups consecutive failures within same session
- Fingerprint Engine: Creates project-scoped failure signatures
- Failure Signature Store: Elasticsearch storage with project filtering
- State Tracker: Redis-based tracking of analyzed time ranges

**Phase 2: Investigation**
- Investigation Queue Manager: Prioritizes investigations (shared with Docker Medic)
- Investigation Agent Runner: Launches Claude Code with Elasticsearch access
- Report Manager: Writes to `/medic/claude/{fingerprint_id}/`
- Recovery Logic: Resumes stalled investigations

**Phase 3: UX Integration**
- Claude Medic Tab: Separate view for tool execution failures
- Project Filtering: Filter signatures by project
- Cluster Visualization: View individual failures within clusters

---

## Phase 1: Detection & Visibility

### Goal

Monitor `claude-streams-*` indices for tool execution failures, cluster sequential failures, create project-scoped fingerprints, and expose via REST API.

### Failure Detection Strategy

**Two-Phase Approach:**

**Phase 1: Identify Sessions with Failures**
```python
# Every 5 minutes, find sessions with tool failures since last checkpoint
query = {
    "query": {
        "bool": {
            "must": [
                {"term": {"event_category": "tool_result"}},
                {"term": {"success": false}},
                {"range": {"timestamp": {"gt": last_processed_timestamp}}}
            ]
        }
    },
    "aggs": {
        "by_session": {
            "composite": {
                "sources": [
                    {"project": {"terms": {"field": "project"}}},
                    {"session_id": {"terms": {"field": "raw_event.event.session_id"}}}
                ]
            }
        }
    },
    "size": 0  # We only want aggregations
}
```

**Phase 2: Analyze Each Session for Contiguous Clusters**
```python
# For each (project, session_id) with failures:
# 1. Query ALL tool events for that session (successes + failures)
# 2. Build chronological sequence
# 3. Identify contiguous failure clusters
# 4. Fingerprint each cluster

# See FailureClusteringEngine.cluster_failures() above
```

**State Tracking:**
```python
# Redis keys for tracking processed time ranges
claude_medic:last_processed_timestamp -> "2025-11-28T19:30:00Z"
claude_medic:project:{project}:last_fingerprinted -> "2025-11-28T19:30:00Z"
claude_medic:checkpoint:{project}:{timestamp} -> "processed"  # 7-day TTL
```

### Failure Clustering Algorithm

**Problem:** 5 consecutive tool failures in one session = 5 attempts to accomplish the same goal → Should create ONE failure signature, not 5.

**Critical Requirement:** Clusters must be **contiguous failures only**. Any successful tool execution between failures breaks the cluster.

**Example:**
```
10:00 - Bash fails (npm install)
10:01 - Bash fails (npm install)
10:02 - Bash fails (npm run build)
10:03 - Read succeeds (package.json)  ← BREAKS CLUSTER
10:04 - Bash fails (npm test)
10:05 - Bash fails (npm test)

Result: 2 clusters
  Cluster 1: [10:00, 10:01, 10:02] - 3 failures
  Cluster 2: [10:04, 10:05] - 2 failures
```

**Solution:** Query Elasticsearch for ALL tool events (successes + failures) in chronological order, then identify contiguous failure sequences.

**Clustering Logic:**
```python
class FailureClusteringEngine:
    """
    Groups CONTIGUOUS tool failures into clusters for fingerprinting.

    A cluster represents multiple consecutive failed attempts to accomplish the same goal.
    Any successful tool execution breaks the cluster.
    """

    CLUSTER_TIMEOUT_SECONDS = 300  # 5 minutes between events = new cluster

    async def cluster_failures(
        self,
        es_client,
        project: str,
        session_id: str,
        start_time: str,
        end_time: str
    ) -> List[FailureCluster]:
        """
        Build clusters by analyzing ALL tool events (successes + failures) in sequence.

        Process:
        1. Query Elasticsearch for all tool_call and tool_result events
        2. Sort chronologically
        3. Identify contiguous failure sequences
        4. Group each sequence into a cluster
        """
        # Query for ALL tool events (not just failures)
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"project": project}},
                        {"term": {"event.session_id": session_id}},
                        {"terms": {"event_category": ["tool_call", "tool_result"]}},
                        {"range": {"timestamp": {"gte": start_time, "lte": end_time}}}
                    ]
                }
            },
            "sort": [{"timestamp": {"order": "asc"}}],
            "size": 10000  # Reasonable limit for single session
        }

        result = await es_client.search(
            index="claude-streams-*",
            body=query
        )

        # Extract events
        events = [hit["_source"] for hit in result["hits"]["hits"]]

        # Build event sequence with success/failure tracking
        event_sequence = self._build_event_sequence(events)

        # Extract contiguous failure clusters
        clusters = self._extract_contiguous_clusters(event_sequence, project, session_id)

        return clusters

    def _build_event_sequence(self, events: List[Dict]) -> List[Dict]:
        """
        Build chronological sequence of tool executions with success status.

        Pairs tool_call with subsequent tool_result to determine success/failure.
        """
        sequence = []
        pending_calls = {}  # tool_use_id -> tool_call event

        for event in events:
            event_category = event.get("event_category")

            if event_category == "tool_call":
                # Extract tool_use_id from raw_event
                tool_use_id = self._extract_tool_use_id(event)
                if tool_use_id:
                    pending_calls[tool_use_id] = event

            elif event_category == "tool_result":
                # Match with pending call
                tool_use_id = self._extract_tool_result_id(event)
                success = event.get("success", True)

                if tool_use_id in pending_calls:
                    call_event = pending_calls.pop(tool_use_id)

                    sequence.append({
                        "timestamp": event.get("timestamp"),
                        "tool_name": call_event.get("tool_name"),
                        "tool_use_id": tool_use_id,
                        "success": success,
                        "call_event": call_event,
                        "result_event": event
                    })

        return sequence

    def _extract_contiguous_clusters(
        self,
        sequence: List[Dict],
        project: str,
        session_id: str
    ) -> List[FailureCluster]:
        """
        Extract contiguous failure sequences from event sequence.

        A cluster is broken by:
        1. Any successful tool execution
        2. Time gap > 5 minutes between events
        3. End of sequence
        """
        clusters = []
        current_failures = []
        last_timestamp = None

        for event in sequence:
            timestamp = event["timestamp"]
            success = event["success"]

            # Check for time gap
            if last_timestamp:
                time_gap = self._calculate_time_gap(last_timestamp, timestamp)
                if time_gap > self.CLUSTER_TIMEOUT_SECONDS:
                    # Time gap breaks cluster
                    if current_failures:
                        clusters.append(self._create_cluster(
                            current_failures, project, session_id
                        ))
                        current_failures = []

            if success:
                # SUCCESS BREAKS CLUSTER
                if current_failures:
                    clusters.append(self._create_cluster(
                        current_failures, project, session_id
                    ))
                    current_failures = []
            else:
                # Failure - add to current cluster
                current_failures.append(event)

            last_timestamp = timestamp

        # Final cluster
        if current_failures:
            clusters.append(self._create_cluster(
                current_failures, project, session_id
            ))

        return clusters

    def _create_cluster(
        self,
        failures: List[Dict],
        project: str,
        session_id: str
    ) -> FailureCluster:
        """Create FailureCluster from contiguous failures"""
        return FailureCluster(
            project=project,
            session_id=session_id,
            failures=failures,
            first_failure=failures[0],
            last_failure=failures[-1]
        )

    def _extract_tool_use_id(self, event: Dict) -> Optional[str]:
        """Extract tool_use_id from tool_call event"""
        try:
            # New format: event.message.content[].id
            content = event["raw_event"]["event"]["message"]["content"]
            for item in content:
                if item.get("type") == "tool_use":
                    return item.get("id")
        except (KeyError, TypeError):
            pass
        return None

    def _extract_tool_result_id(self, event: Dict) -> Optional[str]:
        """Extract tool_use_id from tool_result event"""
        try:
            # New format: event.message.content[].tool_use_id
            content = event["raw_event"]["event"]["message"]["content"]
            for item in content:
                if item.get("type") == "tool_result":
                    return item.get("tool_use_id")
        except (KeyError, TypeError):
            pass
        return None

    def _calculate_time_gap(self, time1: str, time2: str) -> float:
        """Calculate time gap in seconds between ISO timestamps"""
        from datetime import datetime
        t1 = datetime.fromisoformat(time1.replace('Z', '+00:00'))
        t2 = datetime.fromisoformat(time2.replace('Z', '+00:00'))
        return abs((t2 - t1).total_seconds())


class FailureCluster:
    """Represents a cluster of contiguous related failures"""

    def __init__(
        self,
        project: str,
        session_id: str,
        failures: List[Dict],
        first_failure: Dict,
        last_failure: Dict
    ):
        self.project = project
        self.session_id = session_id
        self.failures = failures
        self.first_failure = first_failure
        self.last_failure = last_failure
        self.cluster_id = self._generate_cluster_id()

    def _generate_cluster_id(self) -> str:
        """Generate unique cluster ID"""
        return f"cluster_{self.project}_{self.session_id}_{self.first_failure['timestamp']}"

    def get_primary_failure(self) -> Dict:
        """
        Select the most representative failure from the cluster.

        Prioritize:
        1. Last failure (final attempt before giving up)
        2. Longest error message (most context)
        3. First failure (initial error)
        """
        if len(self.failures) == 1:
            return self.failures[0]

        # Use last failure as primary
        return self.last_failure

    def get_fingerprint_context(self) -> Dict:
        """
        Generate context for fingerprinting.

        Includes:
        - Primary failure details
        - Cluster metadata (count, duration, tools)
        - All error messages (for pattern detection)
        """
        return {
            "primary_failure": self.get_primary_failure(),
            "cluster_metadata": {
                "failure_count": len(self.failures),
                "duration_seconds": (
                    self.last_failure['timestamp'] - self.first_failure['timestamp']
                ).total_seconds(),
                "tools_attempted": list(set(f.get('tool_name') for f in self.failures)),
                "session_id": self.session_id
            },
            "all_error_messages": [
                self._extract_error_message(f) for f in self.failures
            ]
        }

    def _extract_error_message(self, failure: Dict) -> str:
        """Extract error message from failure"""
        # From raw_event.event.message.content[0].content
        try:
            content = failure['raw_event']['event']['message']['content'][0]['content']
            return content[:1000]  # First 1000 chars
        except (KeyError, IndexError):
            return failure.get('error_message', 'Unknown error')
```

### Fingerprinting Algorithm

**Key Difference from Docker Medic:** Project-scoped fingerprints. A "missing package.json" error in project A is different from the same error in project B.

```python
class ClaudeFailureFingerprintEngine:
    """
    Generates fingerprints for Claude Code tool execution failures.

    Fingerprints are PROJECT-SCOPED and include cluster metadata.
    """

    def generate_fingerprint(self, cluster: FailureCluster) -> Dict:
        """
        Generate failure signature from cluster.

        Fingerprint Components:
        1. Project name (NOT normalized - signatures are project-specific)
        2. Tool name (normalized - e.g., "Read", "Bash", "Edit")
        3. Error pattern (normalized message)
        4. Error type (extracted from error message)
        5. Context signature (file paths, commands, etc.)
        """
        primary = cluster.get_primary_failure()
        context = cluster.get_fingerprint_context()

        # Extract error details
        error_message = self._extract_error_message(primary)
        normalized_message = self._normalize_error_message(error_message)
        error_type = self._extract_error_type(error_message)
        tool_name = primary.get('tool_name', 'unknown')

        # Extract context signature (commands, file paths, etc.)
        context_sig = self._extract_context_signature(primary, cluster)

        # Generate fingerprint hash
        fingerprint_string = (
            f"{cluster.project}||"  # Project is KEY part of fingerprint
            f"{tool_name}||"
            f"{error_type}||"
            f"{normalized_message}||"
            f"{context_sig}"
        )

        fingerprint_id = hashlib.sha256(fingerprint_string.encode()).hexdigest()

        return {
            "fingerprint_id": f"sha256:{fingerprint_id}",
            "project": cluster.project,  # NOT normalized
            "tool_name": tool_name,
            "error_type": error_type,
            "error_pattern": normalized_message,
            "context_signature": context_sig,
            "cluster_metadata": context["cluster_metadata"]
        }

    def _normalize_error_message(self, message: str) -> str:
        """
        Normalize error message using existing normalizers.

        IMPORTANT: Project paths are partially normalized:
        - /workspace/{project}/src/file.js -> /workspace/{project}/src/file.js
        - Project name is PRESERVED for fingerprinting
        - Only file paths within project are normalized
        """
        from services.medic.normalizers import get_default_normalizers

        normalized = message
        for normalizer in get_default_normalizers():
            normalized = normalizer.normalize(normalized)

        return normalized[:500]  # First 500 chars

    def _extract_error_type(self, message: str) -> str:
        """
        Extract error type from message.

        Examples:
        - "Exit code 1" -> "exit_code_error"
        - "ENOENT: no such file" -> "file_not_found"
        - "npm error" -> "npm_error"
        - "Error: Could not resolve" -> "resolution_error"
        """
        message_lower = message.lower()

        # Exit codes
        if "exit code" in message_lower:
            return "exit_code_error"

        # File system errors
        if "enoent" in message_lower or "no such file" in message_lower:
            return "file_not_found"
        if "eacces" in message_lower or "permission denied" in message_lower:
            return "permission_denied"

        # Package manager errors
        if "npm error" in message_lower:
            return "npm_error"
        if "yarn error" in message_lower:
            return "yarn_error"

        # Build errors
        if "could not resolve" in message_lower:
            return "resolution_error"
        if "syntax error" in message_lower:
            return "syntax_error"

        # Generic
        if "error:" in message_lower:
            return "generic_error"

        return "unknown_error"

    def _extract_context_signature(self, failure: Dict, cluster: FailureCluster) -> str:
        """
        Extract context signature from tool parameters and errors.

        For Bash: Normalize command patterns
        For Read/Edit/Write: Normalize file paths
        For other tools: Extract key parameters
        """
        tool_name = failure.get('tool_name', '')
        tool_params = failure.get('tool_params', {})

        if tool_name == 'Bash':
            # Extract command pattern
            command = tool_params.get('command', '')
            return self._normalize_bash_command(command)

        elif tool_name in ['Read', 'Edit', 'Write']:
            # Extract file path pattern
            file_path = tool_params.get('file_path', '')
            return self._normalize_file_path(file_path, cluster.project)

        elif tool_name == 'Grep':
            # Pattern + path
            pattern = tool_params.get('pattern', '')
            return f"grep:{pattern}"

        else:
            # Generic: first 100 chars of params
            import json
            return json.dumps(tool_params)[:100]

    def _normalize_bash_command(self, command: str) -> str:
        """
        Normalize bash commands for pattern matching.

        Examples:
        - "npm install" -> "npm:install"
        - "npm run build" -> "npm:run:build"
        - "docker build -f Dockerfile.agent" -> "docker:build"
        - "pytest tests/test_foo.py" -> "pytest:tests"
        """
        command_lower = command.lower().strip()

        # Extract command name and primary action
        parts = command_lower.split()
        if not parts:
            return "bash:empty"

        cmd = parts[0]

        # NPM/Yarn
        if cmd in ['npm', 'yarn']:
            action = parts[1] if len(parts) > 1 else ''
            subaction = parts[2] if len(parts) > 2 else ''
            return f"{cmd}:{action}:{subaction}".rstrip(':')

        # Docker
        if cmd == 'docker':
            action = parts[1] if len(parts) > 1 else ''
            return f"docker:{action}"

        # Python/Pytest
        if cmd in ['python', 'python3', 'pytest']:
            return f"{cmd}:script"

        # Generic
        return f"bash:{cmd}"

    def _normalize_file_path(self, path: str, project: str) -> str:
        """
        Normalize file paths within project.

        Examples:
        - "/workspace/my_project/src/foo.ts" -> "src/foo.ts"
        - "/workspace/my_project/package.json" -> "package.json"

        IMPORTANT: Keep project name in signature context, but normalize
        paths relative to project root.
        """
        # Remove project-specific prefix
        project_prefix = f"/workspace/{project}/"
        if path.startswith(project_prefix):
            return path[len(project_prefix):]

        return path
```

### Elasticsearch Schema

**Index Pattern:** `medic-claude-failures-*` (daily indices)

**Document Structure:**
```json
{
  "fingerprint_id": "sha256:abc123...",
  "type": "claude_failure",  // NEW: Distinguishes from docker_failure
  "created_at": "2025-11-28T12:00:00Z",
  "updated_at": "2025-11-28T13:15:00Z",
  "first_seen": "2025-11-28T12:00:00Z",
  "last_seen": "2025-11-28T13:15:00Z",

  "project": "what_am_i_watching",  // NOT normalized - key discriminator

  "signature": {
    "tool_name": "Bash",
    "error_type": "npm_error",
    "error_pattern": "npm error code ENOENT... Could not read package.json",
    "context_signature": "npm:install",
    "cluster_size_avg": 3.5  // Average number of failures per cluster
  },

  "cluster_count": 12,  // Number of clusters (not individual failures)
  "total_failures": 47,  // Total individual failures across all clusters
  "clusters_last_hour": 2,
  "clusters_last_day": 12,

  "severity": "ERROR",  // All tool failures are ERROR level
  "impact_score": 8.5,

  "status": "new",
  "investigation_status": "not_started",

  "sample_clusters": [
    {
      "cluster_id": "cluster_what_am_i_watching_session123_1732800000",
      "timestamp": "2025-11-28T13:15:00Z",
      "session_id": "session123",
      "task_id": "senior_software_engineer_what_am_i_watching_...",
      "failure_count": 5,  // 5 failures in this cluster
      "duration_seconds": 45,
      "tools_attempted": ["Bash", "Read"],
      "primary_error": "npm error code ENOENT...",
      "raw_failures": [/* array of ES doc IDs */]
    }
  ],

  "tags": ["tool_execution", "npm", "package_manager"]
}
```

**ILM Policy:** Same as Docker Medic (7-day hot, 30-day retention)

### State Management

**Redis Keys:**
```
claude_medic:last_processed_timestamp -> "2025-11-28T19:30:00Z"
claude_medic:project:{project}:last_checkpoint -> "2025-11-28T19:30:00Z"
claude_medic:processing_lock -> "1"  // TTL 60s
```

**State Flow:**
1. Monitor wakes up every 5 minutes
2. Acquires processing lock (60s TTL)
3. Reads `last_processed_timestamp` from Redis
4. Queries Elasticsearch for failures since timestamp
5. Clusters failures by project/session/time
6. Generates fingerprints for each cluster
7. Updates or creates signatures in Elasticsearch
8. Updates `last_processed_timestamp` to max(failure.timestamp)
9. Releases lock

### REST API Endpoints

**Add to `services/observability_server.py`:**

```python
# Claude Medic Endpoints (parallel to Docker Medic)

@app.route('/api/medic/claude/failure-signatures', methods=['GET'])
def get_claude_failure_signatures():
    """
    List Claude Code failure signatures.

    Query params:
    - project: Filter by project (IMPORTANT: project-scoped)
    - status: new, recurring, trending, resolved, ignored
    - investigation_status: not_started, queued, in_progress, completed, failed
    - from_date, to_date: Date range
    - limit, offset: Pagination
    """
    pass

@app.route('/api/medic/claude/failure-signatures/<fingerprint_id>', methods=['GET'])
def get_claude_signature_detail(fingerprint_id):
    """Get detailed signature with cluster samples"""
    pass

@app.route('/api/medic/claude/failure-signatures/<fingerprint_id>/clusters', methods=['GET'])
def get_signature_clusters(fingerprint_id):
    """
    Get all clusters for a signature.

    Returns individual failure clusters with their constituent failures.
    """
    pass

@app.route('/api/medic/claude/failure-signatures/<fingerprint_id>/failures', methods=['GET'])
def get_signature_failures(fingerprint_id):
    """
    Get all individual failures for a signature.

    Links back to original claude-streams-* documents.
    """
    pass

@app.route('/api/medic/claude/stats', methods=['GET'])
def get_claude_medic_stats():
    """
    Overall statistics:
    - Total signatures by project
    - Breakdown by status/tool
    - Cluster counts vs failure counts
    """
    pass

# Project-specific endpoints
@app.route('/api/medic/claude/projects', methods=['GET'])
def get_claude_medic_projects():
    """List projects with Claude failure signatures"""
    pass

@app.route('/api/medic/claude/projects/<project>/stats', methods=['GET'])
def get_project_claude_stats(project):
    """Statistics for a specific project"""
    pass
```

### Auto-Trigger Thresholds

**Different from Docker Medic:** Lower thresholds because tool failures directly impact agent effectiveness.

```yaml
# config/medic.yaml (extend existing)
medic:
  claude_failures:
    auto_trigger:
      enabled: true
      check_interval_seconds: 300  # 5 minutes
      thresholds:
        # Cluster-based thresholds (not individual failures)
        cluster_count:
          total: 5          # 5 clusters = auto-investigate
          per_hour: 3       # 3 clusters in 1 hour = auto-investigate
        # Individual failure thresholds (across all clusters)
        total_failures:
          total: 15         # 15 total failures = auto-investigate
          per_hour: 10      # 10 failures in 1 hour = auto-investigate
```

### Observability Events

**Add to `monitoring/observability.py`:**
```python
# Claude Medic Events
MEDIC_CLAUDE_SIGNATURE_CREATED = "medic_claude_signature_created"
MEDIC_CLAUDE_SIGNATURE_UPDATED = "medic_claude_signature_updated"
MEDIC_CLAUDE_SIGNATURE_TRENDING = "medic_claude_signature_trending"
MEDIC_CLAUDE_CLUSTER_DETECTED = "medic_claude_cluster_detected"
```

### Phase 1 Files to Create

```
services/medic/
├── claude_failure_monitor.py         # Main service - queries Elasticsearch
├── claude_clustering_engine.py       # Clusters sequential failures
├── claude_fingerprint_engine.py      # Project-scoped fingerprinting
├── claude_failure_signature_store.py # Elasticsearch operations
└── claude_normalizers.py             # Claude-specific normalizers (extends base)
```

### Phase 1 Files to Modify

```
services/observability_server.py       # Add Claude Medic API endpoints
monitoring/observability.py            # Add Claude Medic event types
config/medic.yaml                      # Add Claude Medic configuration
```

---

## Phase 2: Investigation Agent

### Goal

Launch investigator agent to analyze Claude Code failures in Elasticsearch, diagnose root causes, and create recommendations for improving agent configurations.

### Investigation Context

**Key Difference:** Investigations focus on Elasticsearch queries, not Docker logs.

**Investigation Agent Instructions:** `services/medic/claude_investigator_instructions.md`

```markdown
# Claude Medic Investigator Agent Instructions

You are investigating a Claude Code tool execution failure signature in the Clauditoreum orchestrator.

## Your Task

1. Analyze the failure signature and cluster samples from Elasticsearch
2. Query `claude-streams-*` indices to understand the full context
3. Examine the orchestrator's agent configuration and instructions
4. Identify patterns in when/why the tool execution fails
5. Create diagnosis report (diagnosis.md)
6. Create recommendations (recommendations.md)

## Your Workspace

- Workspace Root: /workspace/clauditoreum
- Project Root: /workspace/{project}
- You have read access to all orchestrator and project code
- You can query Elasticsearch for Claude execution logs

## Investigation Context

**Signature:**
- Fingerprint ID: {fingerprint_id}
- Project: {project}
- Tool: {tool_name}
- Error Type: {error_type}
- Error Pattern: {error_pattern}

**Cluster Metadata:**
- Total Clusters: {cluster_count}
- Total Failures: {total_failures}
- Average Cluster Size: {cluster_size_avg}

## Available Tools

### Elasticsearch Queries

You can query `claude-streams-*` indices:

```bash
curl -s "http://localhost:9200/claude-streams-*/_search" -H 'Content-Type: application/json' -d '{
  "query": {
    "bool": {
      "must": [
        {"term": {"project": "my_project"}},
        {"term": {"task_id": "task_123"}}
      ]
    }
  },
  "sort": [{"timestamp": "asc"}],
  "size": 100
}'
```

### Codebase Analysis

- Read agent configuration: `config/foundations/agents.yaml`
- Read project CLAUDE.md: `/workspace/{project}/.claude/CLAUDE.md`
- Read agent instructions: `agents/{agent}/.claude/instructions.md`
- Read project code to understand context

## Output Requirements

You MUST create:

1. **diagnosis.md** - Root cause analysis
2. **recommendations.md** - Specific, actionable recommendations

### recommendations.md Template

```markdown
# Recommendations

**Failure Signature:** `{fingerprint_id}`
**Project:** {project}

## Root Cause Summary
{Brief explanation of why failures occur}

## Recommended Actions

### 1. Agent Configuration Changes

**File:** `config/foundations/agents.yaml` or `config/projects/{project}.yaml`

```yaml
# Suggested changes
agents:
  {agent_name}:
    timeout: 600  # Increase from 300
    # OR
    # Add new specialized sub-agent
```

**Rationale:** {Why this helps}

### 2. CLAUDE.md Updates

**File:** `/workspace/{project}/.claude/CLAUDE.md`

Add the following section:

```markdown
## {Topic}

{Guidance for Claude Code agents}
```

**Rationale:** {Why this helps}

### 3. Claude Code Skill Creation

**Skill Name:** `{skill-name}`
**Purpose:** {What it does}

Create skill at: `/workspace/{project}/.claude/skills/{skill-name}.md`

```markdown
# {Skill Name}

{Skill instructions}
```

**Rationale:** {Why this helps}

### 4. Sub-Agent Specialization

**New Agent:** `{agent_name}`
**Purpose:** {Specialized task}

Add to `config/foundations/agents.yaml`:

```yaml
agents:
  {agent_name}:
    description: "{Description}"
    model: "claude-sonnet-4-5-20250929"
    timeout: 300
    makes_code_changes: false
    requires_docker: true
```

Create instructions at: `agents/{agent_name}/.claude/instructions.md`

**Rationale:** {Why specialization helps}

## Implementation Priority

1. {Highest priority action}
2. {Medium priority action}
3. {Lower priority action}

## Expected Impact

- Reduce failure rate by {X}%
- Improve agent success on {specific task type}
- Enable handling of {new capability}
```

## Investigation Approach

1. **Understand the Failure Pattern**
   - Query Elasticsearch for all failures in signature
   - Identify commonalities (same files, commands, error messages)
   - Check if failures are concentrated in specific agents or tasks

2. **Analyze Agent Context**
   - Read the agent's instructions
   - Check if agent has appropriate guidance for this task
   - Look for missing context or unclear instructions

3. **Examine Project Configuration**
   - Read project CLAUDE.md
   - Check if project-specific guidance exists
   - Identify missing documentation

4. **Identify Solutions**
   - Should this be a specialized sub-agent?
   - Should CLAUDE.md provide more guidance?
   - Should a Claude Code skill be created?
   - Is the agent timeout too short?
   - Are there missing dependencies or environment issues?

5. **Create Actionable Recommendations**
   - Be specific (exact file paths, exact changes)
   - Prioritize by impact and ease of implementation
   - Explain rationale for each recommendation
```

### Investigation Report Structure

**Directory Layout:**
```
/medic/claude/{fingerprint_id}/
├── context.json              # Input: signature, clusters, project
├── investigation_log.txt     # Claude Code execution log
├── diagnosis.md              # ROOT CAUSE ANALYSIS
├── recommendations.md        # ACTIONABLE RECOMMENDATIONS (replaces fix_plan.md)
├── ignored.md                # Optional: reason for ignoring
└── attachments/              # Optional: query results, code snippets
    ├── sample_failures.json
    ├── agent_config.yaml
    └── claude_md_excerpt.md
```

### REST API Endpoints (Phase 2)

**Add to `services/observability_server.py`:**

```python
# Claude Medic Investigation Endpoints

@app.route('/api/medic/claude/investigations/<fingerprint_id>', methods=['POST'])
def start_claude_investigation(fingerprint_id):
    """
    Start investigation for Claude failure signature.

    Request: {"priority": "normal" | "high" | "low"}
    Response: {"fingerprint_id": "...", "status": "queued"}
    """
    pass

@app.route('/api/medic/claude/investigations/<fingerprint_id>/status', methods=['GET'])
def get_claude_investigation_status(fingerprint_id):
    """Get investigation status"""
    pass

@app.route('/api/medic/claude/investigations/<fingerprint_id>/diagnosis', methods=['GET'])
def get_claude_diagnosis(fingerprint_id):
    """
    Get diagnosis report.

    Returns: {"fingerprint_id": "...", "content": "markdown", "created_at": "..."}
    """
    pass

@app.route('/api/medic/claude/investigations/<fingerprint_id>/recommendations', methods=['GET'])
def get_claude_recommendations(fingerprint_id):
    """
    Get recommendations report.

    Returns: {"fingerprint_id": "...", "content": "markdown", "created_at": "..."}
    """
    pass

@app.route('/api/medic/claude/investigations', methods=['GET'])
def list_claude_investigations():
    """
    List all Claude investigations.

    Query params: project, status
    """
    pass
```

### Investigation Agent Runner

**Shared with Docker Medic:** Same `investigation_agent_runner.py` and `investigation_orchestrator.py`, but different instructions and context.

**Key Differences:**
- Instructions file: `claude_investigator_instructions.md`
- Context includes: signature, clusters, project, Elasticsearch query helpers
- Output location: `/medic/claude/{fingerprint_id}/`

### Phase 2 Files to Create

```
services/medic/
├── claude_investigator_instructions.md  # Investigation agent prompt
└── claude_investigation_context.py      # Build context for investigations
```

### Phase 2 Files to Modify

```
services/medic/investigation_orchestrator.py  # Add Claude failure support
services/medic/report_manager.py              # Handle /medic/claude/ paths
services/observability_server.py              # Add investigation endpoints
```

---

## Phase 3: UX Integration

### Goal

Add "Claude Medic" tab to web UI for viewing tool execution failures, filtering by project, and viewing recommendations.

### Web UI Components

**New Route:** `web_ui/src/routes/claude-medic.jsx`

**New Components:**
```
web_ui/src/components/claude-medic/
├── ClaudeMedicDashboard.jsx         # Overview with project breakdown
├── ClaudeFailureSignatureList.jsx   # Filterable list (by project, tool, status)
├── ClaudeSignatureDetail.jsx        # Detail view with clusters
├── ClaudeClusterView.jsx            # Individual cluster with failures
├── ClaudeRecommendations.jsx        # Markdown viewer for recommendations
└── ProjectFilter.jsx                # Project selection dropdown
```

### Dashboard Layout

```
┌─────────────────────────────────────────────────────────────┐
│ Claude Medic                                   [Project: All▼]│
├─────────────────────────────────────────────────────────────┤
│ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐            │
│ │  Active     │ │ Under       │ │ Resolved    │            │
│ │  Failures   │ │ Investigation│ │ This Week   │            │
│ │     23      │ │      5      │ │     12      │            │
│ └─────────────┘ └─────────────┘ └─────────────┘            │
├─────────────────────────────────────────────────────────────┤
│ Top Failing Projects                                        │
│ ┌───────────────────────────────────────┐                  │
│ │ what_am_i_watching        [8 failures]│                  │
│ │ utterance_emitter         [5 failures]│                  │
│ │ context_studio            [3 failures]│                  │
│ └───────────────────────────────────────┘                  │
├─────────────────────────────────────────────────────────────┤
│ Recent Failure Signatures                    [Filters ▼]   │
│ ┌─────────────────────────────────────────────────────────┐│
│ │ □ npm_error: Could not read package.json               ││
│ │   Project: utterance_emitter | Tool: Bash             ││
│ │   5 clusters, 18 failures | Last: 2h ago              ││
│ │   [Investigate] [View Clusters]                        ││
│ ├─────────────────────────────────────────────────────────┤│
│ │ □ file_not_found: rollup.config.js                    ││
│ │   Project: utterance_emitter | Tool: Bash             ││
│ │   3 clusters, 12 failures | Investigation: Complete    ││
│ │   [View Recommendations]                               ││
│ └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

### Signature Detail View

```
┌─────────────────────────────────────────────────────────────┐
│ ← Back to Claude Medic                                      │
├─────────────────────────────────────────────────────────────┤
│ Failure Signature: sha256:abc123...                         │
│ Project: utterance_emitter                                  │
│ Tool: Bash | Error Type: npm_error                         │
├─────────────────────────────────────────────────────────────┤
│ Error Pattern:                                              │
│ npm error code ENOENT... Could not read package.json       │
│                                                             │
│ Context: npm:install                                        │
├─────────────────────────────────────────────────────────────┤
│ Statistics                                                  │
│ • Total Clusters: 5                                         │
│ • Total Failures: 18                                        │
│ • Avg Cluster Size: 3.6 failures                           │
│ • First Seen: 2025-11-28 10:00                             │
│ • Last Seen: 2025-11-28 19:25                              │
├─────────────────────────────────────────────────────────────┤
│ Investigation Status: Completed                             │
│ [View Diagnosis] [View Recommendations]                    │
├─────────────────────────────────────────────────────────────┤
│ Clusters (5)                                     [Expand All]│
│ ┌─────────────────────────────────────────────────────────┐│
│ │ ▼ Cluster 1 - 2025-11-28 19:25 (5 failures, 45s)      ││
│ │   Session: 6adb0605-f89a-412d-919d-7b09969e5a22        ││
│ │   Task: dev_environment_verifier_utterance_emitter...  ││
│ │   ┌───────────────────────────────────────────────────┐││
│ │   │ 19:25:43 - Bash failed: npm install               │││
│ │   │ 19:25:55 - Bash failed: npm install               │││
│ │   │ 19:26:10 - Bash failed: npm run build             │││
│ │   │ 19:26:16 - Bash failed: npm run build             │││
│ │   │ 19:26:22 - Read failed: rollup.config.js          │││
│ │   └───────────────────────────────────────────────────┘││
│ │   [View Full Context in Claude Logs]                   ││
│ ├─────────────────────────────────────────────────────────┤│
│ │ ▶ Cluster 2 - 2025-11-28 15:10 (3 failures, 30s)      ││
│ └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

### Recommendations View

```
┌─────────────────────────────────────────────────────────────┐
│ Recommendations for sha256:abc123...                        │
│ Project: utterance_emitter                                  │
├─────────────────────────────────────────────────────────────┤
│ [Markdown rendering of recommendations.md]                  │
│                                                             │
│ ## Root Cause Summary                                      │
│ Agent is running `npm install` from wrong directory...     │
│                                                             │
│ ## Recommended Actions                                     │
│ ### 1. Update CLAUDE.md                                    │
│ ...                                                         │
├─────────────────────────────────────────────────────────────┤
│ [Apply Recommendations] [Mark as Resolved] [Ignore]        │
└─────────────────────────────────────────────────────────────┘
```

### API Integration

```javascript
// Fetch signatures by project
const response = await fetch('/api/medic/claude/failure-signatures?project=utterance_emitter&limit=50')

// Fetch signature detail
const signature = await fetch('/api/medic/claude/failure-signatures/sha256:abc123')

// Fetch clusters for signature
const clusters = await fetch('/api/medic/claude/failure-signatures/sha256:abc123/clusters')

// Trigger investigation
await fetch('/api/medic/claude/investigations/sha256:abc123', {
  method: 'POST',
  body: JSON.stringify({ priority: 'high' })
})

// Get recommendations
const recs = await fetch('/api/medic/claude/investigations/sha256:abc123/recommendations')
```

### WebSocket Events

**Subscribe in `SocketContext`:**
- `medic_claude_signature_created`
- `medic_claude_signature_trending`
- `medic_claude_cluster_detected`
- `medic_claude_investigation_completed`

### Navigation

**Add to main navigation:**
```jsx
<NavLink to="/claude-medic">
  Claude Medic
  {activeClaudeFailures > 0 && <Badge>{activeClaudeFailures}</Badge>}
</NavLink>
```

---

## Safety & Constraints

### Rate Limiting

**Claude Failure Monitor:**
- Max 1000 failures/minute processed
- Batch clustering every 5 minutes
- Circuit breaker on Elasticsearch query failures (3 failures → 60s cooldown)

**Investigation Queue:**
- Shared with Docker Medic (max 3 concurrent)
- Max 10 investigations per hour (combined)
- Cooldown: 4 hours per signature before re-investigation

### Resource Limits

**Investigation:**
- Same as Docker Medic (4-hour timeout, 10MB output)

**Elasticsearch Queries:**
- Max 1000 documents per query
- Pagination for large result sets
- Timeout: 30 seconds per query

### Auto-Trigger Thresholds

**Cluster-Based:**
- 5 total clusters → auto-investigate
- 3 clusters in 1 hour → auto-investigate

**Failure-Based:**
- 15 total failures → auto-investigate
- 10 failures in 1 hour → auto-investigate

**Configuration:** `config/medic.yaml`

---

## Testing Strategy

### Phase 1 Tests

**Unit Tests:**
```python
tests/unit/medic/claude/
├── test_clustering_engine.py        # Cluster logic
├── test_fingerprint_engine.py       # Project-scoped fingerprinting
├── test_claude_normalizers.py       # Bash command normalization
└── test_claude_signature_store.py   # Elasticsearch operations
```

**Integration Tests:**
```python
tests/integration/medic/claude/
├── test_claude_failure_monitor.py   # End-to-end detection
├── test_elasticsearch_queries.py    # ES query patterns
└── test_project_scoping.py          # Verify project isolation
```

**Test Data:**
- Inject synthetic tool failures into `claude-streams-*`
- Create known clusters (5 failures in 45 seconds)
- Verify fingerprints are project-scoped
- Verify clustering groups sequential failures

### Phase 2 Tests

**Unit Tests:**
```python
tests/unit/medic/claude/
└── test_investigation_context.py    # Context building
```

**Integration Tests:**
```python
tests/integration/medic/claude/
├── test_claude_investigation.py     # Full investigation lifecycle
└── test_recommendations_format.py   # Verify recommendations.md structure
```

---

## Deployment Strategy

### Phase 1 Deployment

**Prerequisites:**
- Elasticsearch indices: `claude-streams-*` (already exist)
- Redis (already running)

**Steps:**
1. Create Claude Medic service files
2. Deploy Elasticsearch index template for `medic-claude-failures-*`
3. Update docker-compose.yml (add claude-failure-monitor service)
4. Start claude-failure-monitor service
5. Verify clustering and fingerprinting
6. Test API endpoints

**Rollback:** Stop claude-failure-monitor (data persists in Elasticsearch)

### Phase 2 Deployment

**Prerequisites:**
- Phase 1 deployed and stable
- Investigation infrastructure (from Docker Medic Phase 2)

**Steps:**
1. Create Claude investigation instructions
2. Update investigation orchestrator to support Claude failures
3. Create `/medic/claude/` directory
4. Test investigation launch via API
5. Verify recommendations.md generation

**Rollback:** Stop investigations (in-progress recover on restart)

### Phase 3 Deployment

**Prerequisites:**
- Phase 1 and 2 deployed
- Web UI infrastructure

**Steps:**
1. Create React components
2. Add Claude Medic route
3. Update navigation
4. Deploy to web UI
5. Test filtering and project views

**Rollback:** Remove route from navigation

---

## Critical Files Summary

### Phase 1: Detection

**New Files:**
```
services/medic/claude_failure_monitor.py         # Main monitoring loop
services/medic/claude_clustering_engine.py       # Failure clustering
services/medic/claude_fingerprint_engine.py      # Project-scoped fingerprints
services/medic/claude_failure_signature_store.py # Elasticsearch ops
services/medic/claude_normalizers.py             # Bash command normalization
```

**Modified Files:**
```
services/observability_server.py                 # Add Claude Medic API
monitoring/observability.py                      # Add event types
config/medic.yaml                                # Add configuration
docker-compose.yml                               # Add service
```

### Phase 2: Investigation

**New Files:**
```
services/medic/claude_investigator_instructions.md  # Investigation prompt
services/medic/claude_investigation_context.py      # Context builder
```

**Modified Files:**
```
services/medic/investigation_orchestrator.py     # Support Claude failures
services/medic/report_manager.py                 # Handle /medic/claude/ paths
services/observability_server.py                 # Add investigation endpoints
```

### Phase 3: UX

**New Files:**
```
web_ui/src/routes/claude-medic.jsx
web_ui/src/components/claude-medic/ClaudeMedicDashboard.jsx
web_ui/src/components/claude-medic/ClaudeFailureSignatureList.jsx
web_ui/src/components/claude-medic/ClaudeSignatureDetail.jsx
web_ui/src/components/claude-medic/ClaudeClusterView.jsx
web_ui/src/components/claude-medic/ClaudeRecommendations.jsx
web_ui/src/components/claude-medic/ProjectFilter.jsx
```

---

## Success Metrics

### Phase 1: Detection

- **Coverage:** 100% of tool_result failures detected
- **Clustering Accuracy:** >95% of sequential failures grouped correctly
- **Fingerprint Accuracy:** >90% of similar failures grouped per project
- **Project Isolation:** 0% cross-project fingerprint collisions
- **API Latency:** p95 <200ms

### Phase 2: Investigation

- **Investigation Success:** >70% produce actionable recommendations
- **Time to Diagnosis:** median <20 minutes
- **Recommendation Quality:** >60% of recommendations are actionable
- **Context Accuracy:** >90% of investigations query correct ES data

### Phase 3: UX

- **Page Load Time:** <2s for signature list
- **Project Filtering:** <500ms to filter by project
- **Real-time Updates:** <5s latency for WebSocket events
- **User Adoption:** >50% of medic tab views are Claude Medic

---

## Appendix: Example Scenarios

### Scenario 1: npm install failures

**Failure Pattern:**
- Project: `utterance_emitter`
- Tool: `Bash`
- Command: `npm install`
- Error: `ENOENT: no such file or directory, open '/workspace/package.json'`

**Clustering:**
- 5 sequential failures in 45 seconds
- Same session, same agent (dev_environment_verifier)
- Grouped into 1 cluster

**Fingerprint:**
```json
{
  "fingerprint_id": "sha256:a1b2c3...",
  "project": "utterance_emitter",
  "tool_name": "Bash",
  "error_type": "file_not_found",
  "error_pattern": "ENOENT... Could not read package.json",
  "context_signature": "npm:install"
}
```

**Investigation Recommendations:**
```markdown
## Recommended Actions

### 1. Update dev_environment_verifier Instructions

**File:** `agents/dev_environment_verifier/.claude/instructions.md`

Add:
```markdown
## Running Build Commands

IMPORTANT: When running package manager commands (npm, yarn), ensure you are in the
correct project directory:

```bash
cd /workspace/utterance_emitter
npm install
```

DO NOT run from /workspace root - each project has its own package.json.
```

**Rationale:** Agent is running npm commands from wrong directory.
```

### Scenario 2: Repeated Read failures

**Failure Pattern:**
- Project: `what_am_i_watching`
- Tool: `Read`
- File: `/workspace/what_am_i_watching/src/types/generated.ts`
- Error: `File not found`

**Clustering:**
- 3 failures, 20 seconds apart
- Agent tried Read, then Glob, then Read again
- Grouped into 1 cluster

**Investigation Recommendations:**
```markdown
## Recommended Actions

### 1. Create Claude Code Skill: "generate-types"

**File:** `/workspace/what_am_i_watching/.claude/skills/generate-types.md`

```markdown
# Generate TypeScript Types

Before reading generated type files, you must run the type generation script:

```bash
cd /workspace/what_am_i_watching
npm run generate:types
```

Generated files are in `src/types/generated.ts` and are NOT checked into git.
```

**Rationale:** File is generated, not source-controlled. Agent needs to generate it first.
```

---

## Implementation Timeline

**Phase 1:** 1 week
- Days 1-3: Clustering engine and fingerprinting
- Days 4-5: Elasticsearch integration and API
- Days 6-7: Testing and refinement

**Phase 2:** 1 week
- Days 1-3: Investigation instructions and context building
- Days 4-5: Integration with investigation orchestrator
- Days 6-7: Testing and refinement

**Phase 3:** 1 week
- Days 1-4: React components and views
- Days 5-6: Integration and testing
- Day 7: Documentation and launch

**Total:** 3 weeks for full implementation
