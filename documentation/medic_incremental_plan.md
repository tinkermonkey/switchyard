● User approved Claude's plan
  ⎿  Plan saved to: ~/.claude/plans/recursive-coalescing-sunset.md · /plan to edit
     Medic Component - Incremental Implementation Plan

     Executive Summary

     This plan redesigns the Medic component for incremental delivery, focusing first on visibility and detection (Phase 
     1), then investigation capabilities (Phase 2), and finally UX integration (Phase 3). This approach allows each phase
      to deliver independent value while building toward a comprehensive self-healing monitoring system.

     ---
     Overview

     The Medic component will monitor all WARNING and ERROR logs from Clauditoreum Docker containers, create unique error
      fingerprints, track failure patterns in Elasticsearch, and eventually investigate root causes using Claude Code.

     Key Design Principles

     1. Incremental Delivery: Each phase ships independently with tangible value
     2. No Auto-Fixing Initially: Phase 1 focuses purely on detection and visibility
     3. Host-Based Investigation: Phase 2 investigator runs outside Docker to access cluster logs
     4. Elasticsearch-First: Primary datastore for failure signatures and metrics
     5. Redis for Process Tracking: Lightweight state tracking for investigations
     6. File System Reports: Investigation outputs as markdown in /medic/ directories

     Implementation Approach (User Confirmed)

     1. Phase Sequencing: Complete Phase 1 fully before starting Phase 2
     2. Investigation Triggering: Auto-trigger investigations when signatures hit configurable thresholds
     3. Agent Write Access: Write access to /medic/ directory only (reports), read-only for orchestrator code
     4. UX Integration: Phase 3 included in this implementation effort

     ---
     Phase 1: Visibility & Detection (No Auto-Fixing)

     Goal

     Monitor all WARNING and ERROR logs from Clauditoreum Docker containers, create error fingerprints for deduplication,
      and track failure signatures in Elasticsearch with full API exposure.

     Architecture

     Components:
     - Docker Log Monitor: Continuously streams logs from all orchestrator containers
     - Fingerprint Engine: Generates unique signatures for errors using normalization
     - Failure Signature Store: Elasticsearch-backed storage with 30-day retention
     - REST API Layer: Exposes signatures, occurrences, and statistics

     Data Flow:
     Docker Logs → Log Monitor → Fingerprint Engine → Deduplication Check → Elasticsearch
                                                                                   ↓
                                                              REST API ← Observability Server

     Error Fingerprinting Algorithm

     Fingerprint Components:
     1. Container name (normalized to pattern)
     2. Log level (ERROR, WARNING)
     3. Error type (exception class)
     4. Error message (normalized)
     5. Stack trace signature (function names)

     Normalization Strategy:
     - Strip timestamps, UUIDs, container IDs, temp paths
     - Normalize file paths: /workspace/project-123/ → /workspace/{project}/
     - Normalize identifiers: issue #123 → issue #{issue}
     - Extract exception types and core messages
     - Group stack traces by function call chains

     Example:
     Original: "KeyError: 'issue_number' at 2025-11-28 12:45:23 in container orchestrator-abc123"
     Normalized: "KeyError: '{key}' in task context"
     Fingerprint: sha256:a1b2c3... (based on container pattern + error type + normalized message + stack)

     Elasticsearch Schema

     Index Pattern: medic-failure-signatures-* (daily indices)

     Document Structure:
     {
       "fingerprint_id": "sha256:...",
       "created_at": "2025-11-28T12:00:00Z",
       "updated_at": "2025-11-28T13:15:00Z",
       "first_seen": "2025-11-28T12:00:00Z",
       "last_seen": "2025-11-28T13:15:00Z",

       "signature": {
         "container_pattern": "orchestrator",
         "error_type": "KeyError",
         "error_pattern": "KeyError: '{key}' in task context",
         "stack_signature": ["agent_executor.execute_agent:242"],
         "normalized_message": "Missing required key in task context"
       },

       "occurrence_count": 47,
       "occurrences_last_hour": 3,
       "occurrences_last_day": 47,

       "severity": "ERROR",
       "impact_score": 8.5,

       "status": "new",
       "investigation_status": "not_started",

       "sample_log_entries": [
         {
           "timestamp": "2025-11-28T13:15:00Z",
           "container_id": "abc123",
           "container_name": "orchestrator-1",
           "raw_message": "...",
           "context": {
             "agent": "senior_software_engineer",
             "project": "what_am_i_watching",
             "issue_number": 99
           }
         }
       ],

       "tags": ["agent_execution", "missing_context"]
     }

     ILM Policy:
     - Hot: 0-7 days
     - Warm: 7-30 days
     - Delete: 30 days

     Status State Machine (Phase 1)

     - new - First occurrence detected
     - recurring - Seen 2+ times
     - trending - Increasing occurrence rate
     - resolved - No occurrences for 7 days
     - ignored - Manually marked to ignore

     REST API Endpoints

     Add to services/observability_server.py:

     1. GET /api/medic/failure-signatures
       - List all failure signatures with filtering
       - Query params: status, severity, investigation_status, from_date, to_date, limit, offset
       - Returns: paginated list of signatures
     2. GET /api/medic/failure-signatures/{fingerprint_id}
       - Get detailed information about a specific signature
       - Returns: full signature document
     3. GET /api/medic/failure-signatures/{fingerprint_id}/occurrences
       - Get occurrence history for a signature
       - Query params: from_date, to_date, limit
       - Returns: list of sample log entries
     4. PUT /api/medic/failure-signatures/{fingerprint_id}/status
       - Update signature status (e.g., mark as ignored)
       - Request body: {"status": "ignored", "reason": "..."}
       - Returns: updated signature
     5. GET /api/medic/stats
       - Overall Medic statistics
       - Returns: total signatures, breakdown by status/severity, occurrence counts

     Observability Events

     Add to monitoring/observability.py EventType enum:

     # Medic Events
     MEDIC_SIGNATURE_CREATED = "medic_signature_created"
     MEDIC_SIGNATURE_UPDATED = "medic_signature_updated"
     MEDIC_SIGNATURE_TRENDING = "medic_signature_trending"
     MEDIC_SIGNATURE_RESOLVED = "medic_signature_resolved"

     Docker Compose Integration

     Add service to docker-compose.yml:

     medic-monitor:
       build: .
       volumes:
         - ./:/app
         - /var/run/docker.sock:/var/run/docker.sock  # Docker log access
       environment:
         - REDIS_HOST=redis
         - REDIS_PORT=6379
         - ELASTICSEARCH_HOSTS=http://elasticsearch:9200
         - LOG_LEVEL=INFO
       depends_on:
         - redis
         - elasticsearch
       networks:
         - orchestrator-net
       command: ["python", "-m", "services.medic.docker_log_monitor"]
       restart: unless-stopped

     Phase 1 Files to Create

     services/medic/
     ├── __init__.py
     ├── docker_log_monitor.py       # Main service - streams Docker logs
     ├── fingerprint_engine.py       # Error fingerprinting algorithm
     ├── failure_signature_store.py  # Elasticsearch operations
     └── normalizers.py              # Message normalization helpers

     Phase 1 Files to Modify

     monitoring/observability.py       # Add Medic event types
     services/observability_server.py  # Add REST API endpoints
     docker-compose.yml                # Add medic-monitor service

     ---
     Phase 2: Investigation Agent

     Goal

     Launch investigator agent OUTSIDE Docker to access Clauditoreum cluster logs, diagnose root causes, create fix 
     plans, and persist reports as markdown files.

     Architecture

     Components:
     - Investigation Queue Manager: Manages queue of investigations with Redis-based locking
     - Investigation Agent Runner: Launches Claude Code as host process (not in Docker)
     - Process Lifecycle Manager: Tracks running investigations via Redis with heartbeats
     - Report Manager: Persists markdown reports to file system
     - Investigation Recovery: Startup check to recover/restart stalled investigations

     Data Flow:
     Failure Signature → Investigation Queue → Agent Runner (HOST) → Claude Code
                                ↓                      ↓                    ↓
                         Redis Tracking          Docker Logs         Report Files
                                                                           ↓
                                                                      REST API

     Investigation Process Lifecycle

     Redis Keys:
     medic:investigation:{fingerprint_id}:pid              # Process ID
     medic:investigation:{fingerprint_id}:status           # Status (queued, in_progress, completed, failed)
     medic:investigation:{fingerprint_id}:lock             # Lock with 4-hour TTL
     medic:investigation:{fingerprint_id}:started_at       # ISO timestamp
     medic:investigation:{fingerprint_id}:last_heartbeat   # ISO timestamp
     medic:investigation:{fingerprint_id}:agent_output_lines  # Progress counter
     medic:investigation:{fingerprint_id}:result           # success, failed, ignored
     medic:investigation:{fingerprint_id}:completed_at     # ISO timestamp
     medic:investigation:queue                             # List (queue)

     Status States:
     - queued - Waiting to be picked up
     - starting - Process launching
     - in_progress - Active investigation
     - stalled - No output for >10 minutes
     - completed - Successfully finished
     - failed - Investigation failed
     - ignored - Marked to ignore

     Investigation Agent Runner

     Key Features:
     - Runs Claude Code as host process (not in Docker container)
     - Accesses Docker logs via Docker CLI: docker logs clauditoreum-orchestrator-1 --since 24h
     - Tracks process with PID in Redis
     - Monitors output file for progress (heartbeat)
     - Detects stalls (no output for 10 minutes)
     - 4-hour timeout with auto-kill

     Process Launch:
     claude \
       --project /workspace/clauditoreum \
       --instructions services/medic/investigator_instructions.md \
       --context /medic/{fingerprint_id}/context.json \
       --output /medic/{fingerprint_id}/investigation_log.txt

     Investigation Report Structure

     Directory Layout:
     /medic/{fingerprint_id}/
     ├── context.json              # Input: signature data, sample logs
     ├── investigation_log.txt     # Claude Code execution log
     ├── diagnosis.md              # ROOT CAUSE ANALYSIS
     ├── fix_plan.md               # PROPOSED FIX PLAN
     ├── ignored.md                # Optional: reason for ignoring
     └── attachments/              # Optional: code snippets, logs

     diagnosis.md Template:
     # Root Cause Diagnosis

     **Failure Signature:** `{fingerprint_id}`
     **Investigation Date:** 2025-11-28

     ## Error Summary
     {Brief description}

     ## Root Cause Analysis
     {Detailed explanation}

     ## Evidence
     ### Log Analysis
     ### Code Analysis
     ### System State

     ## Impact Assessment
     - Severity: High/Medium/Low
     - Frequency: {N per day}
     - Affected Components: {list}

     fix_plan.md Template:
     # Fix Plan

     **Failure Signature:** `{fingerprint_id}`

     ## Proposed Solution
     {High-level description}

     ## Implementation Steps
     1. Step 1
     2. Step 2

     ## Code Changes Required
     ### File: {path}
     [before/after code]

     ## Testing Strategy
     ## Risks and Considerations
     ## Deployment Plan

     ignored.md Template:
     # Investigation Outcome: Ignored

     **Failure Signature:** `{fingerprint_id}`

     ## Reason for Ignoring
     {Explanation}

     Investigator Agent Instructions

     File: services/medic/investigator_instructions.md

     # Medic Investigator Agent Instructions

     You are investigating a failure signature in the Clauditoreum orchestrator.

     ## Your Task
     1. Analyze the error pattern and sample log entries
     2. Access Docker container logs for additional context
     3. Examine the orchestrator codebase to identify root cause
     4. Create diagnosis report (diagnosis.md)
     5. Create fix plan (fix_plan.md)
     6. OR create ignored.md if not actionable

     ## Your Workspace
     - Workspace Root: /workspace/clauditoreum
     - You have read access to all orchestrator code
     - You can use `docker logs` to access container logs (you are on the host)

     ## Available Tools
     - Read orchestrator codebase files
     - Execute bash: `docker logs <container>`, grep, find
     - Access Elasticsearch for historical data

     ## Output Requirements
     You MUST create either:
     - diagnosis.md + fix_plan.md (for actionable issues)
     - ignored.md (for non-actionable issues)

     Startup Recovery Logic

     On orchestrator startup:
     1. Find all investigations with status in_progress, starting, or stalled
     2. Check if process still exists (via PID)
     3. If process exists → recover and continue monitoring
     4. If process missing but reports exist → mark as completed
     5. If process missing, no reports, <30min elapsed → wait
     6. If process missing, no reports, >4hr elapsed → mark as failed/timeout
     7. If process missing, no reports, 30min-4hr → re-launch investigation

     Protections:
     - Prevent duplicate investigations via Redis lock
     - Max 3 concurrent investigations
     - 4-hour timeout per investigation
     - Lock prevents re-launch until released

     REST API Endpoints (Phase 2)

     Add to services/observability_server.py:

     1. POST /api/medic/investigations/{fingerprint_id}
       - Start an investigation for a signature
       - Request body: {"priority": "normal" | "high" | "low"}
       - Returns: {"fingerprint_id": "...", "status": "queued"} (202)
     2. GET /api/medic/investigations/{fingerprint_id}/status
       - Get current investigation status
       - Returns: status, pid, timestamps, progress
     3. GET /api/medic/investigations/{fingerprint_id}/diagnosis
       - Get diagnosis report markdown
       - Returns: {"fingerprint_id": "...", "content": "...", "created_at": "..."}
     4. GET /api/medic/investigations/{fingerprint_id}/fix-plan
       - Get fix plan markdown
       - Returns: {"fingerprint_id": "...", "content": "...", "created_at": "..."}
     5. GET /api/medic/investigations
       - List all investigations
       - Returns: list with has_diagnosis, has_fix_plan, has_ignored flags

     Observability Events (Phase 2)

     Add to EventType enum:

     MEDIC_INVESTIGATION_QUEUED = "medic_investigation_queued"
     MEDIC_INVESTIGATION_STARTED = "medic_investigation_started"
     MEDIC_INVESTIGATION_COMPLETED = "medic_investigation_completed"
     MEDIC_INVESTIGATION_FAILED = "medic_investigation_failed"

     Docker Compose Integration (Phase 2)

     Add service:

     medic-investigator:
       build: .
       volumes:
         - ./:/app
         - ..:/workspace  # Access to orchestrator workspace
         - /var/run/docker.sock:/var/run/docker.sock  # Docker CLI access
       environment:
         - REDIS_HOST=redis
         - REDIS_PORT=6379
         - ELASTICSEARCH_HOSTS=http://elasticsearch:9200
       depends_on:
         - redis
         - medic-monitor
       networks:
         - orchestrator-net
       command: ["python", "-m", "services.medic.investigation_orchestrator"]
       restart: unless-stopped

     Phase 2 Files to Create

     services/medic/
     ├── investigation_queue.py          # Queue management with Redis locking
     ├── investigation_agent_runner.py   # Host process launcher
     ├── investigation_orchestrator.py   # Main service (queue processor)
     ├── investigation_recovery.py       # Startup recovery logic
     ├── report_manager.py               # Markdown file persistence
     └── investigator_instructions.md    # Claude Code agent prompt

     Phase 2 Files to Modify

     services/observability_server.py    # Add investigation API endpoints
     monitoring/observability.py         # Add investigation event types
     docker-compose.yml                  # Add medic-investigator service

     Auto-Trigger Logic (Phase 2)

     Trigger Check (runs every 5 minutes):

     # In failure_signature_store.py
     async def check_auto_trigger_conditions(self):
         """Check if any signatures should auto-trigger investigation"""

         # Query Elasticsearch for signatures meeting threshold
         query = {
             "query": {
                 "bool": {
                     "must": [
                         {"term": {"investigation_status": "not_started"}},
                         {"bool": {
                             "should": [
                                 # ERROR: 10 total or 5 in last hour
                                 {"bool": {
                                     "must": [
                                         {"term": {"severity": "ERROR"}},
                                         {"range": {"occurrence_count": {"gte": 10}}}
                                     ]
                                 }},
                                 {"bool": {
                                     "must": [
                                         {"term": {"severity": "ERROR"}},
                                         {"range": {"occurrences_last_hour": {"gte": 5}}}
                                     ]
                                 }},
                                 # WARNING: 50 total or 20 in last hour
                                 {"bool": {
                                     "must": [
                                         {"term": {"severity": "WARNING"}},
                                         {"range": {"occurrence_count": {"gte": 50}}}
                                     ]
                                 }},
                                 {"bool": {
                                     "must": [
                                         {"term": {"severity": "WARNING"}},
                                         {"range": {"occurrences_last_hour": {"gte": 20}}}
                                     ]
                                 }},
                                 # CRITICAL: 3 occurrences
                                 {"bool": {
                                     "must": [
                                         {"term": {"severity": "CRITICAL"}},
                                         {"range": {"occurrence_count": {"gte": 3}}}
                                     ]
                                 }}
                             ]
                         }}
                     ]
                 }
             }
         }

         results = await self.es.search(index="medic-failure-signatures-*", body=query)

         for hit in results['hits']['hits']:
             fingerprint_id = hit['_source']['fingerprint_id']
             # Queue investigation
             await investigation_queue.enqueue_investigation(fingerprint_id, priority="auto")

     Configuration File: config/medic.yaml

     medic:
       auto_trigger:
         enabled: true
         check_interval_seconds: 300  # Check every 5 minutes
         thresholds:
           CRITICAL:
             total_occurrences: 3
             occurrences_per_hour: null  # Any CRITICAL triggers immediately
           ERROR:
             total_occurrences: 10
             occurrences_per_hour: 5
           WARNING:
             total_occurrences: 50
             occurrences_per_hour: 20

     ---
     Phase 3: UX Integration

     Goal

     Add a new tab to the web UI for viewing failure signatures, triggering investigations, viewing reports, and 
     displaying occurrence metrics/graphs.

     Note: Phase 3 is included in this implementation effort and will be built after Phase 1 and 2 are complete.

     Features

     - Dashboard with overall statistics
     - Filterable/sortable failure signature list
     - Detail view with occurrence timeline graph
     - Investigation report viewer (markdown rendering)
     - Trigger investigations from UI
     - Real-time updates via WebSocket

     Web UI Components

     New Route: web_ui/src/routes/medic.jsx

     New Components:
     web_ui/src/components/
     ├── MedicDashboard.jsx            # Overview with stats
     ├── FailureSignatureList.jsx      # Sortable/filterable list
     ├── FailureSignatureDetail.jsx    # Detail view with occurrences
     └── InvestigationReport.jsx       # Markdown report viewer

     WebSocket Integration

     Subscribe to Medic events in SocketContext:
     - medic_signature_created
     - medic_signature_trending
     - medic_investigation_started
     - medic_investigation_completed

     API Usage

     // Get all signatures
     const response = await fetch('/api/medic/failure-signatures?status=recurring&limit=50')

     // Get signature details
     const signature = await fetch('/api/medic/failure-signatures/${fingerprint_id}')

     // Trigger investigation
     await fetch(`/api/medic/investigations/${fingerprint_id}`, {
       method: 'POST',
       body: JSON.stringify({ priority: 'high' })
     })

     // Get diagnosis
     const diagnosis = await fetch(`/api/medic/investigations/${fingerprint_id}/diagnosis`)

     ---
     Safety & Constraints

     Rate Limiting

     Log Monitor:
     - Max 1000 log entries/second per container
     - Batch processing every 5 seconds
     - Circuit breaker on Elasticsearch write failures (3 failures → 60s cooldown)

     Investigation Queue:
     - Max 3 concurrent investigations
     - Max 10 investigations per hour
     - Cooldown: 4 hours per signature before re-investigation

     Investigation Protections

     Resource Limits:
     - Max runtime: 4 hours
     - Max output file size: 10 MB
     - Stall detection: no output for 10 minutes

     Auto-Trigger Thresholds:
     - ERROR severity: Auto-trigger after 10 occurrences OR 5 occurrences in 1 hour
     - WARNING severity: Auto-trigger after 50 occurrences OR 20 occurrences in 1 hour
     - CRITICAL severity: Auto-trigger after 3 occurrences
     - Configurable per severity in config/medic.yaml

     Error Handling (Meta!):
     - Circuit breaker on Docker API failures (5 failures → 30s cooldown)
     - Fallback fingerprint if normalization fails
     - Buffer in memory (max 1000) if Elasticsearch unavailable
     - JSON file backup if ES down >5 minutes
     - Never crash on parse errors

     ---
     Testing Strategy

     Phase 1 Tests

     Unit Tests:
     - test_fingerprint_engine.py - Normalization and fingerprint generation
     - test_failure_signature_store.py - Elasticsearch CRUD
     - test_normalizers.py - Each normalizer independently

     Integration Tests:
     - test_docker_log_monitor.py - Log streaming and parsing
     - test_end_to_end_detection.py - Inject errors, verify signatures created

     Phase 2 Tests

     Unit Tests:
     - test_investigation_queue.py - Queue operations and locking
     - test_report_manager.py - File operations
     - test_investigation_recovery.py - Recovery logic

     Integration Tests:
     - test_investigation_orchestrator.py - Full investigation lifecycle
     - test_agent_runner.py - Process launching and monitoring

     ---
     Deployment Strategy

     Phase 1 Deployment

     Steps:
     1. Create medic service files
     2. Update docker-compose.yml
     3. Deploy Elasticsearch ILM policy and index template
     4. Start medic-monitor service
     5. Verify log streaming and signature creation
     6. Test API endpoints

     Rollback: Stop medic-monitor service (data persists in Elasticsearch)

     Phase 2 Deployment

     Steps:
     1. Create investigation service files
     2. Update docker-compose.yml
     3. Create /medic directory
     4. Start medic-investigator service
     5. Test investigation launch via API
     6. Verify report generation

     Rollback: Stop medic-investigator (in-progress investigations recover on restart)

     ---
     Critical Files Summary

     Phase 1 Implementation

     1. services/medic/docker_log_monitor.py - Core log monitoring
     2. services/medic/fingerprint_engine.py - Error fingerprinting
     3. services/medic/failure_signature_store.py - Elasticsearch operations
     4. services/observability_server.py - REST API endpoints
     5. monitoring/observability.py - Event types

     Phase 2 Implementation

     1. services/medic/investigation_agent_runner.py - Host process management
     2. services/medic/investigation_orchestrator.py - Main service
     3. services/medic/report_manager.py - Markdown persistence
     4. services/medic/investigator_instructions.md - Agent prompt
     5. docker-compose.yml - Service configuration

     ---
     Success Metrics

     Phase 1

     - Detection coverage: 100% of targeted containers monitored
     - Fingerprint accuracy: >90% similar errors grouped correctly
     - False positive rate: <10%
     - API latency: p95 <200ms

     Phase 2

     - Investigation success rate: >70% produce actionable reports
     - Time to diagnosis: median <15 minutes
     - Investigation recovery rate: >95% of stalled investigations recovered
     - Fix applicability: >60% of fix plans are actionable



● Perfect! The plan has been approved and finalized. Here's a summary of what we've designed:

  Plan Summary

  I've created a comprehensive incremental implementation plan for the Medic component with three phases:

  Phase 1: Visibility & Detection (Complete First)

  - Docker log monitor streaming ERROR/WARNING logs from all Clauditoreum containers
  - Fingerprint engine with smart normalization (strips timestamps, UUIDs, normalizes paths)
  - Elasticsearch-backed failure signature storage with 30-day retention
  - Full REST API for querying signatures, occurrences, and statistics
  - Status state machine: new → recurring → trending → resolved → ignored

  Phase 2: Investigation Agent (After Phase 1)

  - Investigation agent runs on host (outside Docker) to access cluster logs
  - Redis-based process tracking with heartbeats and stall detection
  - Auto-trigger investigations based on configurable thresholds:
    - CRITICAL: 3 occurrences
    - ERROR: 10 total OR 5 in 1 hour
    - WARNING: 50 total OR 20 in 1 hour
  - Markdown reports (diagnosis.md, fix_plan.md, or ignored.md) in /medic/{fingerprint_id}/
  - Startup recovery for stalled investigations
  - Write access to /medic/ only, read-only for orchestrator code

  Phase 3: UX Integration (Included in Implementation)

  - New "Medic" tab in web UI
  - Dashboard with statistics and graphs
  - Filterable/sortable signature list
  - Investigation report viewer with markdown rendering
  - Real-time WebSocket updates