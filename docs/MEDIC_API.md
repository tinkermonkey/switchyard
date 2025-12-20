# Medic API Documentation (v2.0)

This document describes the Medic service REST API endpoints for failure signature management and investigation.

## Overview

The Medic service tracks failures from two sources:
- **Docker**: Container log failures (errors/warnings from orchestrator containers)
- **Claude**: Tool execution failures (Claude Code tool call failures)

## Base URL

```
http://localhost:5001/api/medic
```

## Endpoint Structure

### v2.0 Endpoint Organization

```
/api/medic/
├── failure-signatures              # Docker signatures (backward compatible)
├── docker/
│   └── failure-signatures          # Docker signatures (explicit)
├── claude/
│   └── failure-signatures          # Claude signatures
└── failure-signatures/all          # Unified view (both Docker + Claude)
```

---

## Docker Failure Signature Endpoints

### 1. List Docker Failure Signatures

**Backward Compatible** (returns Docker data from new index):
```http
GET /api/medic/failure-signatures
```

**Explicit v2.0 endpoint** (recommended for new code):
```http
GET /api/medic/docker/failure-signatures
```

**Description**: Get Docker container failure signatures with filtering and pagination.

**Query Parameters:**
- `status` (string, optional): Filter by status
  - Values: `new`, `recurring`, `trending`, `resolved`, `ignored`
- `severity` (string, optional): Filter by severity
  - Values: `ERROR`, `WARNING`, `CRITICAL`
- `investigation_status` (string, optional): Filter by investigation status
  - Values: `not_started`, `queued`, `in_progress`, `completed`, `failed`, `ignored`
- `from_date` (ISO 8601, optional): Filter by first_seen >= date
- `to_date` (ISO 8601, optional): Filter by first_seen <= date
- `limit` (integer, optional): Max results (default: 50)
- `offset` (integer, optional): Pagination offset (default: 0)

**Response:**
```json
{
  "signatures": [
    {
      "type": "docker",
      "project": "orchestrator",
      "fingerprint_id": "abc123...",
      "signature": {
        "error_type": "ConnectionError",
        "error_pattern": "Failed to connect to Redis",
        "container_pattern": "orchestrator"
      },
      "occurrence_count": 42,
      "total_failures": 42,
      "status": "trending",
      "severity": "ERROR",
      "tags": ["redis", "connection"],
      "first_seen": "2025-01-01T00:00:00Z",
      "last_seen": "2025-01-15T12:30:45Z",
      "investigation_status": "not_started",
      "sample_entries": [
        {
          "timestamp": "2025-01-15T12:30:45Z",
          "level": "ERROR",
          "message": "Failed to connect to Redis",
          "container_name": "orchestrator"
        }
      ]
    }
  ],
  "total": 100,
  "limit": 50,
  "offset": 0
}
```

**Data Model:**
- `type`: Always `"docker"` for Docker failures
- `project`: Always `"orchestrator"` (Docker system is project-scoped to orchestrator)
- `occurrence_count`: Number of times this signature was recorded
- `total_failures`: Same as occurrence_count (for consistency with Claude)
- `sample_entries`: Array of sample log entries (renamed from `sample_log_entries`)

---

### 2. Get Docker Failure Signature Details

**Backward Compatible:**
```http
GET /api/medic/failure-signatures/{fingerprint_id}
```

**Explicit v2.0:**
```http
GET /api/medic/docker/failure-signatures/{fingerprint_id}
```

**Description**: Get detailed information about a specific Docker failure signature.

**Response:**
```json
{
  "fingerprint_id": "abc123...",
  "type": "docker",
  "project": "orchestrator",
  "signature": {
    "error_type": "ConnectionError",
    "error_pattern": "Failed to connect to Redis",
    "container_pattern": "orchestrator"
  },
  "occurrence_count": 42,
  "total_failures": 42,
  "status": "trending",
  "severity": "ERROR",
  "tags": ["redis", "connection"],
  "first_seen": "2025-01-01T00:00:00Z",
  "last_seen": "2025-01-15T12:30:45Z",
  "investigation_status": "completed",
  "sample_entries": [...]
}
```

---

## Claude Failure Signature Endpoints

### 3. List Claude Failure Signatures

```http
GET /api/medic/claude/failure-signatures
```

**Description**: Get Claude Code tool execution failure signatures.

**Query Parameters:**
- `status` (string, optional): Filter by status
- `severity` (string, optional): Filter by severity
- `project` (string, optional): Filter by project name
- `limit` (integer, optional): Max results (default: 50)
- `offset` (integer, optional): Pagination offset (default: 0)

**Response:**
```json
{
  "signatures": [
    {
      "type": "claude",
      "project": "context-studio",
      "fingerprint_id": "xyz789...",
      "signature": {
        "tool_name": "Edit",
        "error_type": "PermissionError",
        "error_pattern": "Permission denied: /workspace/file.py",
        "context_signature": "hash123..."
      },
      "cluster_count": 5,
      "total_failures": 15,
      "status": "recurring",
      "severity": "ERROR",
      "tags": ["permissions", "filesystem"],
      "first_seen": "2025-01-10T00:00:00Z",
      "last_seen": "2025-01-15T12:00:00Z",
      "investigation_status": "completed",
      "sample_entries": [
        {
          "cluster_id": "cluster-1",
          "timestamp": "2025-01-15T12:00:00Z",
          "session_id": "sess-123",
          "failure_count": 3,
          "tools_attempted": ["Edit", "Read"],
          "primary_error": "Permission denied..."
        }
      ]
    }
  ],
  "total": 50,
  "limit": 50,
  "offset": 0
}
```

**Data Model Differences:**
- `type`: Always `"claude"` for Claude failures
- `project`: Project name where failures occurred (e.g., "context-studio")
- `cluster_count`: Number of failure clusters (groups of related failures)
- `total_failures`: Total number of individual failures across all clusters
- `sample_entries`: Array of sample clusters (not individual log entries)

---

### 4. Get Claude Failure Signature Details

```http
GET /api/medic/claude/failure-signatures/{fingerprint_id}
```

**Description**: Get detailed information about a specific Claude failure signature.

**Response:**
```json
{
  "fingerprint_id": "xyz789...",
  "type": "claude",
  "project": "context-studio",
  "signature": {...},
  "cluster_count": 5,
  "total_failures": 15,
  "status": "recurring",
  "sample_entries": [...]
}
```

---

## Unified Endpoints (New in v2.0)

### 5. Get All Failure Signatures (Unified View)

```http
GET /api/medic/failure-signatures/all
```

**Description**: Get failure signatures from both Docker and Claude systems in a single unified view.

**Query Parameters:**
- `type` (string, optional): Filter by system type
  - Values: `docker`, `claude`, or omit for all
  - Default: `all` (returns both)
- `status` (string, optional): Filter by status
- `severity` (string, optional): Filter by severity
- `project` (string, optional): Filter by project (primarily for Claude failures)
- `limit` (integer, optional): Max results (default: 50)
- `offset` (integer, optional): Pagination offset (default: 0)

**Examples:**

Get all failures (Docker + Claude):
```http
GET /api/medic/failure-signatures/all
```

Get only Docker failures:
```http
GET /api/medic/failure-signatures/all?type=docker
```

Get only Claude failures for a specific project:
```http
GET /api/medic/failure-signatures/all?type=claude&project=context-studio
```

Get all ERROR severity failures:
```http
GET /api/medic/failure-signatures/all?severity=ERROR
```

**Response:**
```json
{
  "signatures": [
    {
      "type": "docker",
      "project": "orchestrator",
      "fingerprint_id": "abc123...",
      ...
    },
    {
      "type": "claude",
      "project": "context-studio",
      "fingerprint_id": "xyz789...",
      ...
    }
  ],
  "total": 150,
  "limit": 50,
  "offset": 0,
  "type_filter": "all"
}
```

**Use Cases:**
- Dashboard showing all system failures
- Cross-system failure analysis
- Unified search across both Docker and Claude failures

---

## Investigation Endpoints

### 6. Start Investigation

```http
POST /api/medic/investigations/{fingerprint_id}/start
```

**Description**: Queue an investigation for a failure signature.

**Request Body:**
```json
{
  "priority": "normal"
}
```

**Values:**
- `priority`: `low`, `normal`, or `high`

**Response:**
```json
{
  "fingerprint_id": "abc123...",
  "status": "queued",
  "priority": "normal"
}
```

---

### 7. Get Investigation Status

```http
GET /api/medic/investigations/{fingerprint_id}
```

**Description**: Get current investigation status for a failure signature.

**Response:**
```json
{
  "fingerprint_id": "abc123...",
  "status": "in_progress",
  "started_at": "2025-01-15T12:00:00Z",
  "pid": 12345
}
```

---

### 8. Cancel Investigation

```http
POST /api/medic/investigations/{fingerprint_id}/cancel
```

**Description**: Cancel an in-progress investigation.

**Response:**
```json
{
  "fingerprint_id": "abc123...",
  "status": "cancelled"
}
```

---

## Statistics Endpoints

### 9. Get Medic Statistics

```http
GET /api/medic/stats
```

**Description**: Get aggregate statistics for Docker failure signatures.

**Response:**
```json
{
  "total_signatures": 100,
  "by_status": {
    "new": 20,
    "recurring": 50,
    "trending": 15,
    "resolved": 10,
    "ignored": 5
  },
  "by_severity": {
    "ERROR": 80,
    "WARNING": 15,
    "CRITICAL": 5
  },
  "total_occurrences": 1000,
  "occurrences_last_hour": 50,
  "occurrences_last_day": 500
}
```

---

## Migration Notes

### Backward Compatibility

**Old Endpoints (still supported):**
- `/api/medic/failure-signatures` → Returns Docker failures from new `medic-docker-failures-*` indices
- `/api/medic/failure-signatures/{id}` → Returns Docker failure details

**Recommended for New Code:**
- Use explicit endpoints: `/api/medic/docker/failure-signatures`
- Use unified endpoint for cross-system views: `/api/medic/failure-signatures/all`

### Breaking Changes

**None.** All existing endpoints continue to work with identical responses. The only change is the underlying Elasticsearch index name.

### Schema Changes

**Old Schema (medic-failure-signatures-*):**
```json
{
  "fingerprint_id": "...",
  "occurrence_count": 42,
  "sample_log_entries": [...]
}
```

**New Schema (medic-docker-failures-*):**
```json
{
  "type": "docker",
  "project": "orchestrator",
  "fingerprint_id": "...",
  "occurrence_count": 42,
  "total_failures": 42,
  "sample_entries": [...]
}
```

**Migration:**
- Run `scripts/migrate_docker_failures.py` to migrate existing data
- Old indices preserved for 30 days as backup
- See `scripts/MIGRATION_GUIDE.md` for details

---

## Error Handling

All endpoints follow consistent error handling:

**Success (200):**
```json
{
  "signatures": [...],
  "total": 100
}
```

**Empty Result (200):**
```json
{
  "signatures": [],
  "total": 0
}
```

**Not Found (404):**
```json
{
  "error": "Signature not found"
}
```

**Server Error (500):**
```json
{
  "error": "Failed to query Elasticsearch: ..."
}
```

---

## Rate Limiting

No rate limiting currently implemented.

---

## Changelog

### v2.0.0 (2025-01-19)

**Added:**
- Unified endpoint: `/api/medic/failure-signatures/all`
- Explicit Docker endpoints: `/api/medic/docker/failure-signatures`
- `type` field in all responses (`docker` or `claude`)
- `project` field in all responses
- `total_failures` field (consistent naming)

**Changed:**
- Renamed `sample_log_entries` → `sample_entries` (backward compatible via migration)
- Index pattern: `medic-failure-signatures-*` → `medic-docker-failures-*`

**Deprecated:**
- None (old endpoints maintained for backward compatibility)

### v1.0.0 (2024-12-01)

- Initial release with Docker failure tracking
- Basic CRUD endpoints for failure signatures
- Investigation management endpoints
