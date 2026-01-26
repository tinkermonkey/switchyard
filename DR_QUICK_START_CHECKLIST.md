# DR Model Coverage - Quick Start Checklist

**Goal:** Achieve 100% source code representation in DR model
**Current:** 214 elements (35% coverage)
**Target:** 397+ elements (100% coverage)

---

## Phase 1: Critical Infrastructure (START HERE)

### Week 1-2: Core Services & Medic Subsystem

**Priority 1: Core Infrastructure Services (12 elements)**
- [ ] `services/agent_executor.py` → `application.service.agent-executor`
- [ ] `services/agent_container_recovery.py` → `application.service.agent-container-recovery`
- [ ] `services/github_api_client.py` → `application.service.github-api-client`
- [ ] `services/github_app_auth.py` → `application.service.github-app-auth`
- [ ] `services/github_app.py` → `application.service.github-app`
- [ ] `services/github_capabilities.py` → `application.service.github-capabilities`
- [ ] `services/github_discussions.py` → `application.service.github-discussions`
- [ ] `services/github_integration.py` → `application.service.github-integration`
- [ ] `services/human_feedback_loop.py` → `application.service.human-feedback-loop`
- [ ] `services/log_collector.py` → `application.service.log-collector`
- [ ] `services/logging_config.py` → `application.service.logging-config`
- [ ] `services/claude_code_failure_handler.py` → `application.service.claude-code-failure-handler`

**Commands:**
```bash
cd documentation-robotics
dr changeset create "extract-core-infrastructure"

# Example for first service:
dr add application service agent-executor \
  --name "Agent Executor" \
  --description "Executes agent tasks in Docker containers with timeout and error handling" \
  --source-file "services/agent_executor.py" \
  --source-symbol "AgentExecutor" \
  --source-provenance "extracted"

# Validate after every 5 services
dr validate --layer application

# When done:
dr changeset diff
dr changeset apply
```

---

**Priority 2: Medic Data Models (8 elements)**
- [ ] Failure Signature → `data_model.schema.failure-signature`
- [ ] Investigation Report → `data_model.schema.investigation-report`
- [ ] Claude Cluster → `data_model.schema.claude-cluster`
- [ ] Claude Fingerprint → `data_model.schema.claude-fingerprint`
- [ ] Claude Diagnosis → `data_model.schema.claude-diagnosis`
- [ ] Medic Sample → `data_model.schema.medic-sample`
- [ ] Docker Failure → `data_model.schema.docker-failure`
- [ ] Investigation State → `data_model.schema.investigation-state`

**Commands:**
```bash
dr changeset create "extract-medic-schemas"

dr add data_model object-schema failure-signature \
  --name "Failure Signature Schema" \
  --description "Docker container failure fingerprint with stack trace and error patterns" \
  --source-file "services/medic/docker/docker_signature_store.py" \
  --source-symbol "FailureSignature" \
  --source-provenance "extracted"

# Continue for other 7 schemas...
dr validate --layer data_model
dr changeset apply
```

---

**Priority 3: Medic API Operations (8 elements)**
- [ ] GET `/api/medic/signatures` → `api.operation.list-medic-signatures`
- [ ] GET `/api/medic/signature/:id` → `api.operation.get-medic-signature`
- [ ] POST `/api/medic/investigate` → `api.operation.trigger-investigation`
- [ ] GET `/api/medic/investigations` → `api.operation.list-investigations`
- [ ] GET `/api/claude-medic/clusters` → `api.operation.list-claude-clusters`
- [ ] GET `/api/claude-medic/fingerprints` → `api.operation.list-claude-fingerprints`
- [ ] GET `/api/claude-medic/recommendations` → `api.operation.get-claude-recommendations`
- [ ] POST `/api/claude-medic/analyze` → `api.operation.trigger-claude-analysis`

**Commands:**
```bash
dr changeset create "extract-medic-api"

dr add api operation list-medic-signatures \
  --name "List Medic Signatures" \
  --description "Returns all failure signatures with filtering and pagination" \
  --source-file "monitoring/observability.py" \
  --source-symbol "list_medic_signatures" \
  --source-provenance "extracted" \
  --property method="GET" \
  --property path="/api/medic/signatures"

# Continue for other 7 operations...
dr validate --layer api --validate-links
dr changeset apply
```

---

## Phase 2: Application Services (Week 3-4)

### Medic Subsystem (28 elements)

**Base Components (6)**
- [ ] `services/medic/base/base_agent_runner.py`
- [ ] `services/medic/base/base_investigation_orchestrator.py`
- [ ] `services/medic/base/base_investigation_queue.py`
- [ ] `services/medic/base/base_report_manager.py`
- [ ] `services/medic/base/base_signature_store.py`
- [ ] `services/medic/base/investigation_state_machine.py`

**Claude Medic (11)**
- [ ] `services/medic/claude/claude_advisor_agent_runner.py`
- [ ] `services/medic/claude/claude_advisor_orchestrator.py`
- [ ] `services/medic/claude/claude_agent_runner.py`
- [ ] `services/medic/claude/claude_clustering_engine.py`
- [ ] `services/medic/claude/claude_failure_monitor.py`
- [ ] `services/medic/claude/claude_fingerprint_engine.py`
- [ ] `services/medic/claude/claude_investigation_queue.py`
- [ ] `services/medic/claude/claude_orchestrator.py`
- [ ] `services/medic/claude/claude_report_manager.py`
- [ ] `services/medic/claude/claude_signature_curator.py`
- [ ] `services/medic/claude/claude_signature_store.py`

**Docker Medic (6)**
- [ ] `services/medic/docker/docker_agent_runner.py`
- [ ] `services/medic/docker/docker_investigation_queue.py`
- [ ] `services/medic/docker/docker_log_monitor.py`
- [ ] `services/medic/docker/docker_orchestrator.py`
- [ ] `services/medic/docker/docker_report_manager.py`
- [ ] `services/medic/docker/fingerprint_engine.py`

**Shared Utilities (5)**
- [ ] `services/medic/shared/elasticsearch_utils.py`
- [ ] `services/medic/shared/redis_utils.py`
- [ ] `services/medic/shared/sample_manager.py`
- [ ] `services/medic/shared/status_calculator.py`
- [ ] `services/medic/shared/tag_extractor.py`

**Commands:**
```bash
dr changeset create "extract-medic-subsystem"

# For base classes, use component type:
dr add application component base-investigation-orchestrator \
  --name "Base Investigation Orchestrator" \
  --description "Abstract base class for failure investigation orchestration" \
  --source-file "services/medic/base/base_investigation_orchestrator.py" \
  --source-symbol "BaseInvestigationOrchestrator" \
  --source-provenance "extracted"

# For concrete services:
dr add application service claude-orchestrator \
  --name "Claude Medic Orchestrator" \
  --description "Orchestrates AI-powered failure analysis using Claude API" \
  --source-file "services/medic/claude/claude_orchestrator.py" \
  --source-symbol "ClaudeOrchestrator" \
  --source-provenance "extracted"

# Validate frequently
dr validate --layer application
dr changeset apply
```

---

### Pattern Analysis (12 elements)
- [ ] `services/elasticsearch_pattern_indices.py`
- [ ] `services/pattern_alerting.py`
- [ ] `services/pattern_daily_aggregator_es.py`
- [ ] `services/pattern_detection_schema.py`
- [ ] `services/pattern_detector_es.py`
- [ ] `services/pattern_github_integration_es.py`
- [ ] `services/pattern_github_processor_es.py`
- [ ] `services/pattern_ingestion_service.py`
- [ ] `services/pattern_llm_analyzer.py`
- [ ] `services/pattern_similarity_analyzer.py`
- [ ] `services/medic/claude_normalizer.py`
- [ ] `services/medic/normalizers.py`

**Commands:**
```bash
dr changeset create "extract-pattern-analysis"
# Follow same pattern as above
```

---

### Review & Workflow (9 elements)
- [ ] `services/review_cycle.py`
- [ ] `services/review_learning_schema.py`
- [ ] `services/review_outcome_correlator.py`
- [ ] `services/review_parser.py`
- [ ] `services/review_pattern_detector.py`
- [ ] `services/work_execution_state.py`
- [ ] `services/workspace_router.py`
- [ ] `services/pipeline_progression.py`
- [ ] `services/observability_server.py`

---

## Phase 3: User Interface (Week 5-6)

### Claude Medic UI (8 elements)
- [ ] `web_ui/src/components/claude-medic/ClaudeMedicDashboard.jsx`
- [ ] `web_ui/src/components/claude-medic/ClaudeFailureSignatureList.jsx`
- [ ] `web_ui/src/components/claude-medic/ClaudeSignatureDetail.jsx`
- [ ] `web_ui/src/components/claude-medic/ClaudeDiagnosis.jsx`
- [ ] `web_ui/src/components/claude-medic/ClaudeRecommendations.jsx`
- [ ] `web_ui/src/components/claude-medic/ClaudeAdvisorPanel.jsx`
- [ ] `web_ui/src/components/claude-medic/ClaudeClusterView.jsx`
- [ ] `web_ui/src/components/claude-medic/ProjectFilter.jsx`

**Commands:**
```bash
dr changeset create "extract-ux-claude-medic"

dr add ux component claude-medic-dashboard \
  --name "Claude Medic Dashboard" \
  --description "AI-powered failure analysis dashboard showing clusters, fingerprints, and recommendations" \
  --source-file "web_ui/src/components/claude-medic/ClaudeMedicDashboard.jsx" \
  --source-symbol "ClaudeMedicDashboard" \
  --source-provenance "extracted"

# Link to API operations consumed:
dr update ux.component.claude-medic-dashboard \
  --property crossLayerRelationships.consumes='["api.operation.list-claude-clusters", "api.operation.list-claude-fingerprints"]'

dr validate --layer ux --validate-links
dr changeset apply
```

---

### Medic UI (7 elements)
- [ ] `web_ui/src/components/FailureSignatureList.jsx`
- [ ] `web_ui/src/components/FailureSignatureDetail.jsx`
- [ ] `web_ui/src/components/InvestigationReport.jsx`
- [ ] `web_ui/src/components/ActiveInvestigations.jsx`
- [ ] `web_ui/src/components/ActiveFixes.jsx`
- [ ] `web_ui/src/components/RepairCycleContainers.jsx`
- [ ] `web_ui/src/components/RepairCycleStatus.jsx`

---

### Supporting UI (13 elements)
- [ ] `web_ui/src/components/StatsCards.jsx`
- [ ] `web_ui/src/components/AgentState.jsx`
- [ ] `web_ui/src/components/ConfirmationModal.jsx`
- [ ] `web_ui/src/components/Toast.jsx`
- [ ] `web_ui/src/components/HeaderBox.jsx`
- [ ] `web_ui/src/components/HeaderStatsCard.jsx`
- [ ] `web_ui/src/components/HeaderClaudeUsage.jsx`
- [ ] `web_ui/src/components/CycleBoundingNode.jsx`
- [ ] Full views (Dashboard, Medic, ClaudeMedic, ReviewLearning, Projects)

---

### React Contexts (7 elements)
- [ ] `web_ui/src/contexts/AppStateProvider.jsx`
- [ ] `web_ui/src/contexts/AgentStateContext.jsx`
- [ ] `web_ui/src/contexts/ProjectStateContext.jsx`
- [ ] `web_ui/src/contexts/SocketContext.jsx`
- [ ] `web_ui/src/contexts/SystemStateContext.jsx`
- [ ] `web_ui/src/contexts/ThemeContext.jsx`

---

### Custom Hooks (10 elements)
- [ ] `web_ui/src/hooks/useActiveAgents.js`
- [ ] `web_ui/src/hooks/useActivePipelineAgents.js`
- [ ] `web_ui/src/hooks/useAgentActions.js`
- [ ] `web_ui/src/hooks/useCircuitBreakers.js`
- [ ] `web_ui/src/hooks/useProjects.js`
- [ ] `web_ui/src/hooks/useRepairCycles.js`
- [ ] `web_ui/src/hooks/useSystemHealth.js`
- [ ] Utilities (cycleLayout, eventMerging, polling, stateHelpers)

---

## Phase 4: Data Models & API Completion (Week 7)

### Remaining Data Models (14 elements)
- [ ] Repair Cycle schemas (3)
- [ ] Pattern Analysis schemas (5)
- [ ] Review Learning schemas (3)
- [ ] Workspace Context schemas (3)

### Remaining API Operations (8 elements)
- [ ] Repair Cycle API (4)
- [ ] WebSocket Events (4)

---

## Phase 5: Final Services & Testing (Week 8)

### Remaining Services (18 elements)
- [ ] Fix Orchestrator (5)
- [ ] Workspace Context (4)
- [ ] Pipeline Components (6)
- [ ] State & Queue (3)

### Test Suites (10 elements)
- [ ] Group tests into logical suites
- [ ] Document coverage targets

---

## Validation Checklist

After each phase:
- [ ] `dr validate --strict` passes
- [ ] `dr validate --validate-links --strict-links` passes
- [ ] All elements have source tracking
- [ ] Cross-layer relationships documented
- [ ] No broken references
- [ ] Changeset diff reviewed
- [ ] Changeset applied

---

## Critical Success Factors

**DO:**
- ✅ Always use `dr add` CLI
- ✅ Always include source tracking (`--source-file`, `--source-symbol`, `--source-provenance`)
- ✅ Create changesets for extraction work
- ✅ Validate incrementally (every 5-10 elements)
- ✅ Document cross-layer relationships
- ✅ Link API operations to UX components that consume them
- ✅ Link services to data models they manage

**DON'T:**
- ❌ Manually create/edit YAML files (60%+ error rate)
- ❌ Skip validation until the end
- ❌ Forget source references
- ❌ Ignore broken relationship warnings

---

## Progress Tracking

**Current Status:**
- Total Elements: 214
- Target: 397+
- Remaining: 183

**By Phase:**
- Phase 1 (Critical): 28 elements → 242 total (11% increase)
- Phase 2 (Services): 49 elements → 291 total (23% increase)
- Phase 3 (UX): 45 elements → 336 total (34% increase)
- Phase 4 (Data/API): 22 elements → 358 total (40% increase)
- Phase 5 (Final): 28 elements → 386 total (45% increase)

**Estimated Time:**
- Phase 1: 8 hours
- Phase 2: 14 hours
- Phase 3: 12 hours
- Phase 4: 6 hours
- Phase 5: 10 hours
- **Total: 50 hours (8 weeks @ 6-7 hours/week)**

---

## Quick Commands Reference

```bash
# Create changeset
dr changeset create "extract-{name}"

# Add element with source tracking
dr add {layer} {type} {id} \
  --name "Name" \
  --description "Description" \
  --source-file "path/to/file.py" \
  --source-symbol "ClassName" \
  --source-provenance "extracted"

# Validate
dr validate --layer {layer}
dr validate --validate-links

# Review and apply
dr changeset diff
dr changeset apply

# Check progress
dr list {layer} {type} | wc -l
```

---

## Notes

- Start with Phase 1 for immediate value (Medic subsystem)
- Testing layer (Phase 5) can be lower priority
- Navigation layer already 100% complete
- Focus on services and UX for maximum impact
- Maintain source tracking for all elements
- Validate relationships after linking layers
