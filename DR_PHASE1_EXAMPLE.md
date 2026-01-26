# DR Model Phase 1: Hands-On Example
**Extracting Core Infrastructure Services**

This document provides **concrete, copy-paste commands** to complete Phase 1.

---

## Setup

```bash
# Navigate to DR model directory
cd /home/austinsand/workspace/orchestrator/clauditoreum/documentation-robotics

# Verify DR CLI is working
dr --version

# Check current state
dr validate --strict
```

---

## Step 1: Create Changeset

```bash
# Create changeset for core infrastructure extraction
dr changeset create "extract-core-infrastructure-$(date +%Y%m%d)"

# Verify changeset created
dr changeset list

# Output should show: extract-core-infrastructure-20260126 (active)
```

---

## Step 2: Extract Core Services (12 services)

### Service 1: Agent Executor

```bash
dr add application service agent-executor \
  --name "Agent Executor" \
  --description "Executes agent tasks in isolated Docker containers with timeout handling, error recovery, and output collection for Claude Code agents" \
  --source-file "services/agent_executor.py" \
  --source-symbol "AgentExecutor" \
  --source-provenance "extracted"
```

### Service 2: Agent Container Recovery

```bash
dr add application service agent-container-recovery \
  --name "Agent Container Recovery" \
  --description "Handles recovery of failed agent containers with automatic restart, state restoration, and failure logging" \
  --source-file "services/agent_container_recovery.py" \
  --source-symbol "AgentContainerRecovery" \
  --source-provenance "extracted"
```

### Service 3: GitHub API Client

```bash
dr add application service github-api-client \
  --name "GitHub API Client" \
  --description "Low-level GitHub REST and GraphQL API client with rate limiting, caching, and retry logic for repository and project operations" \
  --source-file "services/github_api_client.py" \
  --source-symbol "GitHubAPIClient" \
  --source-provenance "extracted"
```

### Service 4: GitHub App Authentication

```bash
dr add application service github-app-auth \
  --name "GitHub App Authentication Service" \
  --description "Manages GitHub App installation token generation with JWT signing and private key handling for bot authentication" \
  --source-file "services/github_app_auth.py" \
  --source-symbol "GitHubAppAuth" \
  --source-provenance "extracted"
```

### Service 5: GitHub App Integration

```bash
dr add application service github-app \
  --name "GitHub App Integration" \
  --description "High-level GitHub App integration coordinating authentication, webhook handling, and API operations for orchestrator bot identity" \
  --source-file "services/github_app.py" \
  --source-symbol "GitHubApp" \
  --source-provenance "extracted"
```

### Service 6: GitHub Capabilities

```bash
dr add application service github-capabilities \
  --name "GitHub Capabilities Service" \
  --description "Abstracts GitHub platform capabilities with feature detection, permissions checking, and API version compatibility" \
  --source-file "services/github_capabilities.py" \
  --source-symbol "GitHubCapabilities" \
  --source-provenance "extracted"
```

### Service 7: GitHub Discussions

```bash
dr add application service github-discussions \
  --name "GitHub Discussions Service" \
  --description "Manages GitHub Discussions integration for agent outputs, comments, and threaded conversations on project boards" \
  --source-file "services/github_discussions.py" \
  --source-symbol "GitHubDiscussions" \
  --source-provenance "extracted"
```

### Service 8: GitHub Integration

```bash
dr add application service github-integration \
  --name "GitHub Integration Coordinator" \
  --description "Coordinates all GitHub integrations including projects, issues, PRs, and discussions with unified error handling" \
  --source-file "services/github_integration.py" \
  --source-symbol "GitHubIntegration" \
  --source-provenance "extracted"
```

### Service 9: Human Feedback Loop

```bash
dr add application service human-feedback-loop \
  --name "Human Feedback Loop Service" \
  --description "Manages human-in-the-loop interactions via GitHub comments with conversational state tracking and feedback routing" \
  --source-file "services/human_feedback_loop.py" \
  --source-symbol "HumanFeedbackLoop" \
  --source-provenance "extracted"
```

### Service 10: Log Collector

```bash
dr add application service log-collector \
  --name "Log Collector" \
  --description "Collects and aggregates logs from agents, services, and containers for centralized monitoring and debugging" \
  --source-file "services/log_collector.py" \
  --source-symbol "LogCollector" \
  --source-provenance "extracted"
```

### Service 11: Logging Configuration

```bash
dr add application service logging-config \
  --name "Logging Configuration Service" \
  --description "Centralized logging configuration with structured JSON formatting, log levels, and output destinations for observability" \
  --source-file "services/logging_config.py" \
  --source-symbol "LoggingConfig" \
  --source-provenance "extracted"
```

### Service 12: Claude Code Failure Handler

```bash
dr add application service claude-code-failure-handler \
  --name "Claude Code Failure Handler" \
  --description "Handles Claude Code CLI failures with error pattern detection, retry strategies, and escalation to Medic for investigation" \
  --source-file "services/claude_code_failure_handler.py" \
  --source-symbol "ClaudeCodeFailureHandler" \
  --source-provenance "extracted"
```

---

## Step 3: Validate

```bash
# Validate application layer
dr validate --layer application

# Expected output: ✓ All validations passed

# If errors, read carefully and fix
# Common issues:
# - Typo in element ID
# - Missing required properties
# - Invalid source file path
```

---

## Step 4: Add Cross-Layer Relationships

### Link to Technology Dependencies

```bash
# Agent Executor uses Docker and Python
dr update application.service.agent-executor \
  --set uses_technologies='["technology.platform.docker", "technology.language.python-3-11", "technology.framework.asyncio"]'

# GitHub API Client uses GitHub API and Python
dr update application.service.github-api-client \
  --set uses_technologies='["technology.api-client.github-api", "technology.language.python-3-11", "technology.library.aiohttp"]'

# GitHub App Auth uses GitHub API
dr update application.service.github-app-auth \
  --set uses_technologies='["technology.api-client.github-api", "technology.language.python-3-11"]'

# Log Collector uses Elasticsearch
dr update application.service.log-collector \
  --set uses_technologies='["technology.database.elasticsearch", "technology.language.python-3-11"]'

# Logging Config uses Python
dr update application.service.logging-config \
  --set uses_technologies='["technology.language.python-3-11"]'
```

### Link to Datastores

```bash
# Log Collector writes to Elasticsearch
dr update application.service.log-collector \
  --set crossLayerRelationships.uses='["datastore.datastore.elasticsearch"]'
```

### Link to Business Capabilities

```bash
# Agent Executor realizes task execution capability
dr update application.service.agent-executor \
  --set crossLayerRelationships.realizes='["business.capability.task-execution"]'

# GitHub Integration realizes project management capability
dr update application.service.github-integration \
  --set crossLayerRelationships.realizes='["business.capability.project-board-management"]'
```

---

## Step 5: Validate Relationships

```bash
# Validate with link checking
dr validate --validate-links

# Expected output: ✓ All relationship targets exist

# If broken links, check:
# - Element ID is correct (use: dr list technology library)
# - Target element exists (use: dr find technology.library.aiohttp)
```

---

## Step 6: Review Changeset

```bash
# Show what changed
dr changeset diff

# Output should show:
# - 12 new application.service elements
# - Property updates for cross-layer relationships
# - No deletions (unless intended)

# Review carefully:
# - Are descriptions accurate?
# - Are source references correct?
# - Are relationships logical?
```

---

## Step 7: Apply Changeset

```bash
# Apply changes to main model
dr changeset apply

# Confirm when prompted

# Verify applied
dr list application service | grep -E "(agent-executor|github-api-client|log-collector)"

# Output should show new services
```

---

## Step 8: Final Validation

```bash
# Run full validation
dr validate --strict --validate-links

# Check manifest updated
cat ../model/manifest.yaml | grep "service:"

# Should show: service: 32 (was 20, now +12)
```

---

## Next Steps

After completing Phase 1 services, continue with **Medic Data Models**:

```bash
# Create new changeset
dr changeset create "extract-medic-schemas-$(date +%Y%m%d)"

# Extract 8 Medic schemas (see next section)
```

---

## Troubleshooting

### Error: "Element already exists"

```bash
# Check if element is already modeled
dr find application.service.agent-executor

# If exists, use update instead of add:
dr update application.service.agent-executor \
  --source-file "services/agent_executor.py" \
  --source-symbol "AgentExecutor" \
  --source-provenance "extracted"
```

### Error: "Invalid source file path"

```bash
# Paths should be relative to repository root
# ✓ Correct: services/agent_executor.py
# ✗ Wrong: /home/user/.../services/agent_executor.py
# ✗ Wrong: ./services/agent_executor.py
```

### Error: "Validation failed: Missing required property"

```bash
# Check what's required
dr add application service --help

# Add missing property
dr update application.service.agent-executor \
  --set property_name="value"
```

### Error: "Broken relationship: Target not found"

```bash
# Check if target exists
dr find technology.library.aiohttp

# If not found, create it first:
dr add technology library aiohttp \
  --name "aiohttp" \
  --description "Async HTTP client library"

# Then retry the relationship update
```

---

## Verification Commands

```bash
# Count services in application layer
dr list application service | wc -l
# Expected: 32 (was 20, added 12)

# Show all services with source tracking
dr list application service --format json | jq '.[] | select(.properties.source) | .id'

# Verify relationships
dr validate --validate-links --output json | jq '.relationship_validation'
```

---

## Time Estimate

- **Services extraction (12)**: 30-45 minutes
- **Validation**: 5 minutes
- **Relationship linking**: 15-20 minutes
- **Review and apply**: 10 minutes
- **Total**: ~60-80 minutes

---

## Success Criteria

After Phase 1 completion:
- [x] 12 new application.service elements added
- [x] All elements have source tracking
- [x] Technology dependencies linked
- [x] Datastore relationships documented
- [x] Business capability links added
- [x] Validation passes (strict + links)
- [x] Changeset applied successfully
- [x] Manifest shows updated count

**Result:** Application layer coverage improves from 33 → 45 elements (36% → 42%)

---

## Next Phase Preview

**Phase 1B: Medic Data Models** (8 schemas)

```bash
dr add data_model object-schema failure-signature \
  --name "Failure Signature Schema" \
  --description "Docker container failure fingerprint with stack trace, error patterns, exit code, and failure timestamp for deduplication" \
  --source-file "services/medic/docker/docker_signature_store.py" \
  --source-symbol "FailureSignature" \
  --source-provenance "extracted"

# ... 7 more schemas
```

See **DR_QUICK_START_CHECKLIST.md** for full Phase 1 details.
