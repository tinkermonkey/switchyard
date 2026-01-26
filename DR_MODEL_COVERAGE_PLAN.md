# Documentation Robotics Model Coverage Plan
## Comprehensive Source Code Representation Analysis

**Created:** 2026-01-26
**Current Coverage:** 214 elements modeled / 313+ source files
**Estimated Coverage:** ~35-40%

---

## Executive Summary

This plan outlines a systematic approach to achieve **complete source code representation** in the DR model. Currently, only ~35-40% of the codebase is represented. The analysis identifies **150+ missing elements** across all 12 layers, with the largest gaps in:

1. **Application Layer** (73 missing services/components)
2. **UX Layer** (48 missing components)
3. **Testing Layer** (107 missing test files)
4. **API Layer** (missing internal API operations)
5. **Data Model Layer** (missing Python class schemas)

---

## Current State Analysis

### What's Well-Modeled (✓)

| Layer | Elements | Coverage | Notes |
|-------|----------|----------|-------|
| **Motivation** | 26 | ~80% | Goals, principles, requirements well-defined |
| **Business** | 14 | ~70% | Core capabilities and services captured |
| **Security** | 17 | ~60% | Authentication, authorization, data protection covered |
| **Technology** | 23 | ~85% | Languages, frameworks, databases well-represented |
| **Navigation** | 12 | ~95% | All web UI routes modeled |
| **APM** | 7 | ~70% | Monitoring, metrics, observability covered |

### Critical Gaps (✗)

| Layer | Modeled | Actual | Gap | Priority |
|-------|---------|--------|-----|----------|
| **Application** | 33 | 106+ | **73** | CRITICAL |
| **UX** | 19 | 64+ | **45** | HIGH |
| **Testing** | 14 | 111+ | **97** | MEDIUM |
| **API** | 14 | 30+ | **16** | HIGH |
| **Data Model** | 28 | 50+ | **22** | HIGH |
| **Datastore** | 4 | 6+ | **2** | LOW |

---

## Layer-by-Layer Gap Analysis

### 1. UX Layer (React Web UI)

**Current State:** 19/64 components modeled (30% coverage)

**What Exists in Codebase:**
- 64 JSX component files
- 20 JS utility/service files
- Multiple contexts, hooks, routes

**What's Modeled:**
- ✓ 16 main UI components (Header, ActiveAgents, LiveLogs, etc.)
- ✓ 3 views (Dashboard, Medic Dashboard, Review Learning)

**GAPS - Missing Components (45):**

**Priority 1 - Core UI (15 components):**
```
- web_ui/src/components/claude-medic/ClaudeMedicDashboard.jsx
- web_ui/src/components/claude-medic/ClaudeFailureSignatureList.jsx
- web_ui/src/components/claude-medic/ClaudeSignatureDetail.jsx
- web_ui/src/components/claude-medic/ClaudeDiagnosis.jsx
- web_ui/src/components/claude-medic/ClaudeRecommendations.jsx
- web_ui/src/components/claude-medic/ClaudeAdvisorPanel.jsx
- web_ui/src/components/claude-medic/ClaudeClusterView.jsx
- web_ui/src/components/claude-medic/ProjectFilter.jsx
- web_ui/src/components/FailureSignatureList.jsx
- web_ui/src/components/FailureSignatureDetail.jsx
- web_ui/src/components/InvestigationReport.jsx
- web_ui/src/components/ActiveInvestigations.jsx
- web_ui/src/components/ActiveFixes.jsx
- web_ui/src/components/RepairCycleContainers.jsx
- web_ui/src/components/RepairCycleStatus.jsx
```

**Priority 2 - Supporting UI (13 components):**
```
- web_ui/src/components/StatsCards.jsx
- web_ui/src/components/AgentState.jsx
- web_ui/src/components/ConfirmationModal.jsx
- web_ui/src/components/Toast.jsx
- web_ui/src/components/HeaderBox.jsx
- web_ui/src/components/HeaderStatsCard.jsx
- web_ui/src/components/HeaderClaudeUsage.jsx
- web_ui/src/components/CycleBoundingNode.jsx
- web_ui/src/components/Dashboard.jsx (full view)
- web_ui/src/components/Medic.jsx
- web_ui/src/components/ClaudeMedic.jsx
- web_ui/src/components/ReviewLearning.jsx (full component)
- web_ui/src/components/Projects.jsx (full component)
```

**Priority 3 - Context Providers (7 components):**
```
- web_ui/src/contexts/AppStateProvider.jsx
- web_ui/src/contexts/AgentStateContext.jsx
- web_ui/src/contexts/ProjectStateContext.jsx
- web_ui/src/contexts/SocketContext.jsx
- web_ui/src/contexts/SystemStateContext.jsx
- web_ui/src/contexts/ThemeContext.jsx
- web_ui/src/contexts/index.js
```

**Priority 4 - Hooks (10 components):**
```
- web_ui/src/hooks/useActiveAgents.js
- web_ui/src/hooks/useActivePipelineAgents.js
- web_ui/src/hooks/useAgentActions.js
- web_ui/src/hooks/useCircuitBreakers.js
- web_ui/src/hooks/useProjects.js
- web_ui/src/hooks/useRepairCycles.js
- web_ui/src/hooks/useSystemHealth.js
- web_ui/src/hooks/index.js
- web_ui/src/utils/cycleLayout.js
- web_ui/src/utils/eventMerging.js
- web_ui/src/utils/polling.js
- web_ui/src/utils/stateHelpers.js
```

**Actionable Steps:**
1. Create changeset: `dr changeset create "extract-ux-layer"`
2. Extract Priority 1 components (Claude Medic suite)
3. Extract Priority 2 components (supporting UI)
4. Extract Priority 3 (React contexts)
5. Extract Priority 4 (custom hooks and utilities)
6. Validate: `dr validate --layer ux`
7. Link to API operations consumed by each component
8. Apply changeset

---

### 2. API Layer (REST/GraphQL Operations)

**Current State:** 14 operations modeled (46% coverage)

**What Exists in Codebase:**
- 30+ REST endpoints in `monitoring/observability_server.py`
- 14 WebSocket events (Socket.IO)
- GitHub GraphQL operations
- Internal service API calls

**What's Modeled:**
- ✓ 14 observability REST endpoints (health, agents, pipelines, etc.)

**GAPS - Missing Operations (16):**

**Priority 1 - Medic API (8 operations):**
```
- GET /api/medic/signatures
- GET /api/medic/signature/:id
- POST /api/medic/investigate
- GET /api/medic/investigations
- GET /api/claude-medic/clusters
- GET /api/claude-medic/fingerprints
- GET /api/claude-medic/recommendations
- POST /api/claude-medic/analyze
```

**Priority 2 - Repair Cycle API (4 operations):**
```
- GET /api/repair-cycles
- GET /api/repair-cycle/:id
- POST /api/repair-cycle/start
- POST /api/repair-cycle/stop
```

**Priority 3 - WebSocket Events (4 operations):**
```
- socket.event.agent-started
- socket.event.agent-completed
- socket.event.pipeline-updated
- socket.event.claude-stream
```

**Actionable Steps:**
1. Create changeset: `dr changeset create "extract-api-layer"`
2. Extract all REST endpoints from `observability_server.py`
3. Extract WebSocket events from Socket.IO implementation
4. Add request/response schema links to Data Model layer
5. Link to consuming UX components
6. Validate: `dr validate --layer api --validate-links`
7. Apply changeset

---

### 3. Application Layer (Services & Components)

**Current State:** 33 elements modeled (31% coverage)

**What Exists in Codebase:**
- 14 agent classes
- 93 service files
- 6 pipeline components
- 9 monitoring components
- 3 Claude integration components
- 2 state management components
- 1 task queue component

**What's Modeled:**
- ✓ 13 agent components (all core agents)
- ✓ 20 services (core services)
- ✗ Missing: 73 services, 6 pipeline components, medic subsystem

**GAPS - Missing Services (73):**

**Priority 1 - Core Infrastructure (12 services):**
```
services/agent_executor.py - AgentExecutor
services/agent_container_recovery.py - AgentContainerRecovery
services/github_api_client.py - GitHubAPIClient
services/github_app_auth.py - GitHubAppAuth
services/github_app.py - GitHubApp
services/github_capabilities.py - GitHubCapabilities
services/github_discussions.py - GitHubDiscussions
services/github_integration.py - GitHubIntegration
services/human_feedback_loop.py - HumanFeedbackLoop
services/log_collector.py - LogCollector
services/logging_config.py - LoggingConfig
services/claude_code_failure_handler.py - ClaudeCodeFailureHandler
```

**Priority 2 - Medic Subsystem (28 services):**
```
Base Components (6):
- services/medic/base/base_agent_runner.py
- services/medic/base/base_investigation_orchestrator.py
- services/medic/base/base_investigation_queue.py
- services/medic/base/base_report_manager.py
- services/medic/base/base_signature_store.py
- services/medic/base/investigation_state_machine.py

Claude Medic (11):
- services/medic/claude/claude_advisor_agent_runner.py
- services/medic/claude/claude_advisor_orchestrator.py
- services/medic/claude/claude_agent_runner.py
- services/medic/claude/claude_clustering_engine.py
- services/medic/claude/claude_failure_monitor.py
- services/medic/claude/claude_fingerprint_engine.py
- services/medic/claude/claude_investigation_queue.py
- services/medic/claude/claude_orchestrator.py
- services/medic/claude/claude_report_manager.py
- services/medic/claude/claude_signature_curator.py
- services/medic/claude/claude_signature_store.py

Docker Medic (6):
- services/medic/docker/docker_agent_runner.py
- services/medic/docker/docker_investigation_queue.py
- services/medic/docker/docker_log_monitor.py
- services/medic/docker/docker_orchestrator.py
- services/medic/docker/docker_report_manager.py
- services/medic/docker/fingerprint_engine.py

Shared Utilities (5):
- services/medic/shared/elasticsearch_utils.py
- services/medic/shared/redis_utils.py
- services/medic/shared/sample_manager.py
- services/medic/shared/status_calculator.py
- services/medic/shared/tag_extractor.py
```

**Priority 3 - Pattern Analysis (12 services):**
```
- services/elasticsearch_pattern_indices.py
- services/pattern_alerting.py
- services/pattern_daily_aggregator_es.py
- services/pattern_detection_schema.py
- services/pattern_detector_es.py
- services/pattern_github_integration_es.py
- services/pattern_github_processor_es.py
- services/pattern_ingestion_service.py
- services/pattern_llm_analyzer.py
- services/pattern_similarity_analyzer.py
- services/medic/claude_normalizer.py
- services/medic/normalizers.py
```

**Priority 4 - Review & Workflow (9 services):**
```
- services/review_cycle.py
- services/review_learning_schema.py
- services/review_outcome_correlator.py
- services/review_parser.py
- services/review_pattern_detector.py
- services/work_execution_state.py
- services/workspace_router.py
- services/pipeline_progression.py
- services/observability_server.py (as service, not just API)
```

**Priority 5 - Fix Orchestrator (5 services):**
```
- services/fix_orchestrator/claude_fix_agent_runner.py
- services/fix_orchestrator/claude_fix_orchestrator.py
- services/fix_orchestrator/fix_execution_queue.py
- services/fix_orchestrator/fix_state_manager.py
- services/fix_orchestrator/main.py
```

**Priority 6 - Workspace Context (4 services):**
```
- services/workspace/context.py
- services/workspace/discussions_context.py
- services/workspace/hybrid_context.py
- services/workspace/issues_context.py
```

**Priority 7 - Pipeline Components (6 components):**
```
- pipeline/base.py - PipelineStage (base class)
- pipeline/factory.py - PipelineFactory
- pipeline/orchestrator.py - PipelineOrchestrator (enhanced version)
- pipeline/repair_cycle_checkpoint.py - RepairCycleCheckpoint
- pipeline/repair_cycle.py - RepairCycle
- pipeline/repair_cycle_runner.py - RepairCycleRunner
```

**Priority 8 - State & Queue (3 components):**
```
- state_management/manager.py - StateManager
- task_queue/task_manager.py - TaskQueueManager
- claude/session_manager.py - ClaudeSessionManager
```

**Actionable Steps:**
1. Create changeset: `dr changeset create "extract-application-layer"`
2. Extract Priority 1 (core infrastructure) - 12 services
3. Extract Priority 2 (Medic subsystem) - 28 services
4. Extract Priority 3 (pattern analysis) - 12 services
5. Extract Priority 4 (review & workflow) - 9 services
6. Extract Priority 5 (fix orchestrator) - 5 services
7. Extract Priority 6 (workspace context) - 4 services
8. Extract Priority 7 (pipeline components) - 6 components
9. Extract Priority 8 (state & queue) - 3 components
10. Validate after each priority: `dr validate --layer application`
11. Link to technology dependencies
12. Link to data models used
13. Link to API operations exposed
14. Apply changeset

---

### 4. Technology Layer

**Current State:** 23 technologies modeled (85% coverage - GOOD)

**What Exists:**
- Languages: Python 3.11 ✓
- Frameworks: FastAPI ✗, Flask ✓, React ✓, Asyncio ✓
- Databases: Redis ✓, Elasticsearch ✓, PostgreSQL ✗
- Platforms: Docker ✓
- VCS: Git ✓
- API Clients: GitHub API ✓, Claude API ✓

**GAPS - Missing Technologies (5):**

**Priority 1 - Missing Core (5):**
```
- technology.framework.fastapi (not modeled, if used)
- technology.database.postgresql (if used for persistence)
- technology.library.pydantic (data validation)
- technology.library.aiohttp (async HTTP)
- technology.library.websockets (WebSocket support)
```

**Actionable Steps:**
1. Verify if FastAPI, PostgreSQL are actually used
2. Add missing libraries if used extensively
3. Link to components that use them
4. Validate: `dr validate --layer technology`

---

### 5. Data Model Layer (Schemas)

**Current State:** 28 object-schemas modeled (56% coverage)

**What Exists in Codebase:**
- 50+ Python classes with data structures
- Pydantic models
- Dataclasses
- Type definitions

**What's Modeled:**
- ✓ 28 core schemas (task-info, pipeline-run, github-state, etc.)

**GAPS - Missing Schemas (22):**

**Priority 1 - Medic Domain (8 schemas):**
```
- data_model.schema.failure-signature
- data_model.schema.investigation-report
- data_model.schema.claude-cluster
- data_model.schema.claude-fingerprint
- data_model.schema.claude-diagnosis
- data_model.schema.medic-sample
- data_model.schema.docker-failure
- data_model.schema.investigation-state
```

**Priority 2 - Repair Cycle (3 schemas):**
```
- data_model.schema.repair-cycle
- data_model.schema.repair-cycle-checkpoint
- data_model.schema.fix-execution-state
```

**Priority 3 - Pattern Analysis (5 schemas):**
```
- data_model.schema.pattern-detection
- data_model.schema.pattern-alert
- data_model.schema.pattern-aggregation
- data_model.schema.pattern-similarity
- data_model.schema.elasticsearch-index-mapping
```

**Priority 4 - Review Learning (3 schemas):**
```
- data_model.schema.review-outcome
- data_model.schema.review-correlation
- data_model.schema.review-pattern
```

**Priority 5 - Workspace (3 schemas):**
```
- data_model.schema.workspace-context
- data_model.schema.discussion-context
- data_model.schema.issue-context
```

**Actionable Steps:**
1. Create changeset: `dr changeset create "extract-data-model-layer"`
2. Extract Python classes from each service
3. Document schema structure (fields, types, validation rules)
4. Link to services that use each schema
5. Link to API operations that accept/return schema
6. Link to datastores that persist schema
7. Validate: `dr validate --layer data_model --validate-links`
8. Apply changeset

---

### 6. Datastore Layer

**Current State:** 4 datastores modeled (67% coverage)

**What Exists:**
- Redis ✓
- Elasticsearch ✓
- YAML config store ✓
- JSON metrics backup ✓
- PostgreSQL ✗ (if used)
- File system state ✗

**GAPS - Missing Datastores (2):**

**Priority 1 - State Storage (2):**
```
- datastore.datastore.file-system-state (for state_management/)
- datastore.datastore.checkpoint-storage (for pipeline checkpoints)
```

**Actionable Steps:**
1. Verify file-based state storage mechanisms
2. Model file system as datastore for checkpoints, locks, etc.
3. Link to schemas persisted in each datastore
4. Validate: `dr validate --layer datastore`

---

### 7. Testing Layer

**Current State:** 14 test elements modeled (12% coverage - CRITICAL GAP)

**What Exists in Codebase:**
- 111 test files across unit and integration tests
- Multiple test suites (agents, services, pipeline, medic)
- Test fixtures and utilities

**What's Modeled:**
- ✓ 4 test suites (basic coverage)
- ✓ 4 fixtures
- ✓ 4 strategies
- ✓ 2 coverage requirements

**GAPS - Missing Test Files (97):**

**Priority 1 - Core Test Suites (10):**
```
- testing.test-suite.agent-unit-tests
- testing.test-suite.service-unit-tests
- testing.test-suite.pipeline-unit-tests
- testing.test-suite.medic-unit-tests
- testing.test-suite.agent-integration-tests
- testing.test-suite.service-integration-tests
- testing.test-suite.github-integration-tests
- testing.test-suite.claude-integration-tests
- testing.test-suite.monitoring-tests
- testing.test-suite.workflow-tests
```

**Priority 2 - Test Coverage Areas (15):**
```
Individual test files can be grouped into test suites:
- Unit tests: agents/ (14 test files)
- Unit tests: services/ (30+ test files)
- Unit tests: pipeline/ (5 test files)
- Unit tests: medic/ (20+ test files)
- Integration tests: medic/ (10+ test files)
- Integration tests: workflow/ (5+ test files)
- Fixtures and mocks (15+ files)
```

**Actionable Steps:**
1. Create changeset: `dr changeset create "extract-testing-layer"`
2. Group test files into logical test-suite elements
3. Model test suites for each major subsystem
4. Document test coverage targets
5. Link test suites to components under test
6. Link fixtures to test suites that use them
7. Validate: `dr validate --layer testing`
8. Apply changeset

**Note:** Testing layer can be lower priority since tests are implementation details, not architecture

---

### 8. Navigation Layer

**Current State:** 12 routes modeled (100% coverage - COMPLETE)

**Status:** ✓ All web UI routes are modeled
**No action needed** - this layer is complete

---

### 9. Security Layer

**Current State:** 17 elements modeled (60% coverage)

**What Exists:**
- GitHub authentication (App + PAT)
- Claude API key management
- Docker socket access control
- SSH key mounting
- Secret management

**What's Modeled:**
- ✓ 3 authentication policies
- ✓ 3 authorization policies
- ✓ 2 data protection policies
- ✓ 3 isolation policies
- ✓ 3 protection mechanisms
- ✓ 3 secret management policies

**GAPS - Missing Security Elements (5):**

**Priority 1 - Additional Security (5):**
```
- security.authentication-policy.claude-api-key-auth
- security.authorization-policy.agent-workspace-access
- security.data-protection.api-key-rotation
- security.secret-management.environment-variable-store
- security.protection-mechanism.rate-limiting
```

**Actionable Steps:**
1. Review security policies in code
2. Add missing authentication/authorization patterns
3. Document secret storage mechanisms (.env, ~/.orchestrator/)
4. Link to services that enforce security
5. Validate: `dr validate --layer security`

---

### 10. Business Layer

**Current State:** 14 elements modeled (70% coverage)

**What Exists:**
- 10 business capabilities ✓
- 4 business services ✓

**Status:** Reasonably complete - business layer represents higher-level capabilities

**GAPS - Missing Elements (3):**

**Priority 2 - Additional Capabilities:**
```
- business.capability.failure-investigation
- business.capability.pattern-learning
- business.capability.repair-automation
```

**Actionable Steps:**
1. Add missing business capabilities for Medic subsystem
2. Link to application services that realize them
3. Validate: `dr validate --layer business`

---

### 11. APM Layer (Observability)

**Current State:** 7 elements modeled (70% coverage)

**What Exists:**
- Metrics, monitors, collectors, event streams ✓
- Elasticsearch-based analytics ✓

**GAPS - Missing Elements (2):**

**Priority 2 - Additional Monitoring:**
```
- apm.metric.docker-container-health
- apm.metric.pattern-detection-effectiveness
```

**Actionable Steps:**
1. Add missing metrics for Medic and repair cycles
2. Link to services that emit metrics
3. Validate: `dr validate --layer apm`

---

### 12. Motivation Layer

**Current State:** 26 elements modeled (80% coverage)

**What Exists:**
- 6 goals ✓
- 8 principles ✓
- 7 requirements ✓
- 5 stakeholders ✓

**Status:** Well-modeled - motivation layer is strategic, not 1:1 with code

**GAPS - Minimal (2-3):**

**Priority 3 - Additional Goals:**
```
- motivation.goal.reduce-review-noise (review learning)
- motivation.goal.automate-failure-resolution (medic)
```

**Actionable Steps:**
1. Add goals related to Medic and review learning
2. Link to capabilities and services
3. Validate: `dr validate --layer motivation`

---

## Prioritized Implementation Roadmap

### Phase 1: Critical Infrastructure (Week 1-2)
**Goal:** Model all core services and infrastructure

**Tasks:**
1. **Application Layer - Priority 1** (12 core services)
   - Agent executor, GitHub clients, logging, failure handling
   - Estimated: 4-6 hours

2. **Data Model Layer - Priority 1** (8 Medic schemas)
   - Failure signatures, investigation reports, clustering
   - Estimated: 2-3 hours

3. **API Layer - Priority 1** (8 Medic API operations)
   - Medic REST endpoints
   - Estimated: 1-2 hours

**Deliverables:**
- 28 new elements
- Full Medic subsystem represented
- Validation passing

---

### Phase 2: Application Services (Week 3-4)
**Goal:** Complete application layer service coverage

**Tasks:**
1. **Application Layer - Priority 2** (28 Medic services)
   - Base components, Claude Medic, Docker Medic, shared utilities
   - Estimated: 8-10 hours

2. **Application Layer - Priority 3** (12 pattern analysis services)
   - Pattern detection, alerting, LLM analysis
   - Estimated: 3-4 hours

3. **Application Layer - Priority 4** (9 review & workflow services)
   - Review cycles, outcome correlation, workspace routing
   - Estimated: 2-3 hours

**Deliverables:**
- 49 new service elements
- Pattern analysis subsystem complete
- Review learning subsystem complete

---

### Phase 3: User Interface (Week 5-6)
**Goal:** Complete UX layer coverage

**Tasks:**
1. **UX Layer - Priority 1** (15 Claude Medic + Medic components)
   - Dashboard, signature lists, investigation reports
   - Estimated: 4-5 hours

2. **UX Layer - Priority 2** (13 supporting UI components)
   - Stats cards, modals, status displays
   - Estimated: 3-4 hours

3. **UX Layer - Priority 3** (7 React contexts)
   - State management contexts
   - Estimated: 2 hours

4. **UX Layer - Priority 4** (10 hooks and utilities)
   - Custom hooks, layout utilities
   - Estimated: 2-3 hours

**Deliverables:**
- 45 new UX elements
- Complete UI component coverage
- All React patterns documented

---

### Phase 4: Data Models & API (Week 7)
**Goal:** Complete data model and API coverage

**Tasks:**
1. **Data Model Layer - Priorities 2-5** (14 remaining schemas)
   - Repair cycles, pattern analysis, review learning, workspace
   - Estimated: 3-4 hours

2. **API Layer - Priority 2-3** (8 remaining operations)
   - Repair cycle API, WebSocket events
   - Estimated: 2 hours

**Deliverables:**
- 22 new data model elements
- 8 new API operations
- Complete API surface documented

---

### Phase 5: Completion & Polish (Week 8)
**Goal:** Fill remaining gaps and validate

**Tasks:**
1. **Application Layer - Priorities 5-8** (18 remaining services)
   - Fix orchestrator, workspace context, pipeline components, state/queue
   - Estimated: 4-5 hours

2. **Testing Layer** (10 test suites)
   - Group tests into logical suites
   - Estimated: 2-3 hours

3. **Cross-Layer Validation**
   - Validate all relationships
   - Fix broken links
   - Estimated: 2-3 hours

**Deliverables:**
- 28 final elements
- Complete model coverage
- All validation passing

---

## Execution Strategy

### Recommended Workflow

For each layer/priority:

1. **Create Changeset (MANDATORY for extraction)**
   ```bash
   dr changeset create "extract-{layer}-priority-{n}"
   ```

2. **Extract Elements with Source Tracking**
   ```bash
   dr add {layer} {type} {id} \
     --name "Name" \
     --description "..." \
     --source-file "path/to/file.py" \
     --source-symbol "ClassName" \
     --source-provenance "extracted"
   ```

3. **Validate Incrementally**
   ```bash
   dr validate --layer {layer}
   dr validate --validate-links
   ```

4. **Link Cross-Layer Relationships**
   - Add `uses`, `exposes`, `manages`, `consumes` relationships
   - Use dot-notation or x-extensions

5. **Review Changeset**
   ```bash
   dr changeset diff
   ```

6. **Apply Changeset**
   ```bash
   dr changeset apply
   ```

---

### Automation Opportunities

**Bulk Extraction Scripts:**

Create helper scripts for repetitive patterns:

```bash
# extract_services.sh
for service in services/**/*.py; do
  name=$(basename $service .py | tr '_' '-')
  dr add application service "$name" \
    --source-file "$service" \
    --source-provenance "extracted" \
    --name "$(extract_class_name $service)"
done
```

**Pattern Detection:**
- Use `grep` to find all classes, functions
- Parse Python AST to extract class names
- Generate `dr add` commands programmatically

---

## Success Metrics

### Coverage Targets

| Layer | Current | Target | Increase |
|-------|---------|--------|----------|
| Application | 33 | 106 | +73 |
| UX | 19 | 64 | +45 |
| Data Model | 28 | 50 | +22 |
| API | 14 | 30 | +16 |
| Testing | 14 | 24 | +10 |
| Security | 17 | 22 | +5 |
| Business | 14 | 17 | +3 |
| APM | 7 | 9 | +2 |
| Technology | 23 | 28 | +5 |
| Datastore | 4 | 6 | +2 |
| **TOTAL** | **214** | **397** | **+183** |

### Quality Gates

After each phase:
- [ ] `dr validate --strict` passes
- [ ] `dr validate --validate-links --strict-links` passes
- [ ] All elements have source tracking
- [ ] Cross-layer relationships documented
- [ ] No broken references

---

## Risk Mitigation

### Common Pitfalls

1. **Manual YAML Generation**
   - ⚠️ Risk: 60%+ error rate
   - ✅ Solution: Always use `dr add` CLI

2. **Missing Source Tracking**
   - ⚠️ Risk: Loses traceability
   - ✅ Solution: Always include `--source-file`, `--source-symbol`, `--source-provenance`

3. **Batch Without Validation**
   - ⚠️ Risk: Accumulating errors
   - ✅ Solution: Validate after every 5-10 elements

4. **Broken Relationships**
   - ⚠️ Risk: Invalid cross-layer links
   - ✅ Solution: Use `--validate-links` frequently

---

## Maintenance Plan

### Keeping Model Current

**After code changes:**
1. Update source references if files move
2. Add new elements for new components
3. Archive deprecated elements
4. Re-validate relationships

**Quarterly Reviews:**
- Full coverage audit
- Relationship validation
- Schema evolution check
- Documentation updates

---

## Appendix: Detailed File Listings

### Services Not Yet Modeled (73 files)

```
Core Infrastructure (12):
- services/agent_executor.py
- services/agent_container_recovery.py
- services/github_api_client.py
- services/github_app_auth.py
- services/github_app.py
- services/github_capabilities.py
- services/github_discussions.py
- services/github_integration.py
- services/human_feedback_loop.py
- services/log_collector.py
- services/logging_config.py
- services/claude_code_failure_handler.py

Medic Base (6):
- services/medic/base/base_agent_runner.py
- services/medic/base/base_investigation_orchestrator.py
- services/medic/base/base_investigation_queue.py
- services/medic/base/base_report_manager.py
- services/medic/base/base_signature_store.py
- services/medic/base/investigation_state_machine.py

Medic Claude (11):
- services/medic/claude/claude_advisor_agent_runner.py
- services/medic/claude/claude_advisor_orchestrator.py
- services/medic/claude/claude_agent_runner.py
- services/medic/claude/claude_clustering_engine.py
- services/medic/claude/claude_failure_monitor.py
- services/medic/claude/claude_fingerprint_engine.py
- services/medic/claude/claude_investigation_queue.py
- services/medic/claude/claude_orchestrator.py
- services/medic/claude/claude_report_manager.py
- services/medic/claude/claude_signature_curator.py
- services/medic/claude/claude_signature_store.py

Medic Docker (6):
- services/medic/docker/docker_agent_runner.py
- services/medic/docker/docker_investigation_queue.py
- services/medic/docker/docker_log_monitor.py
- services/medic/docker/docker_orchestrator.py
- services/medic/docker/docker_report_manager.py
- services/medic/docker/fingerprint_engine.py

Medic Shared (5):
- services/medic/shared/elasticsearch_utils.py
- services/medic/shared/redis_utils.py
- services/medic/shared/sample_manager.py
- services/medic/shared/status_calculator.py
- services/medic/shared/tag_extractor.py

Pattern Analysis (12):
- services/elasticsearch_pattern_indices.py
- services/pattern_alerting.py
- services/pattern_daily_aggregator_es.py
- services/pattern_detection_schema.py
- services/pattern_detector_es.py
- services/pattern_github_integration_es.py
- services/pattern_github_processor_es.py
- services/pattern_ingestion_service.py
- services/pattern_llm_analyzer.py
- services/pattern_similarity_analyzer.py
- services/medic/claude_normalizer.py
- services/medic/normalizers.py

Review & Workflow (9):
- services/review_cycle.py
- services/review_learning_schema.py
- services/review_outcome_correlator.py
- services/review_parser.py
- services/review_pattern_detector.py
- services/work_execution_state.py
- services/workspace_router.py
- services/pipeline_progression.py
- services/observability_server.py

Fix Orchestrator (5):
- services/fix_orchestrator/claude_fix_agent_runner.py
- services/fix_orchestrator/claude_fix_orchestrator.py
- services/fix_orchestrator/fix_execution_queue.py
- services/fix_orchestrator/fix_state_manager.py
- services/fix_orchestrator/main.py

Workspace Context (4):
- services/workspace/context.py
- services/workspace/discussions_context.py
- services/workspace/hybrid_context.py
- services/workspace/issues_context.py

Utilities (3):
- services/medic/elasticsearch_setup.py
- services/medic/main.py
- services/medic/simplified_investigation_orchestrator.py
```

### UX Components Not Yet Modeled (45 files)

```
Claude Medic UI (8):
- web_ui/src/components/claude-medic/ClaudeMedicDashboard.jsx
- web_ui/src/components/claude-medic/ClaudeFailureSignatureList.jsx
- web_ui/src/components/claude-medic/ClaudeSignatureDetail.jsx
- web_ui/src/components/claude-medic/ClaudeDiagnosis.jsx
- web_ui/src/components/claude-medic/ClaudeRecommendations.jsx
- web_ui/src/components/claude-medic/ClaudeAdvisorPanel.jsx
- web_ui/src/components/claude-medic/ClaudeClusterView.jsx
- web_ui/src/components/claude-medic/ProjectFilter.jsx

Medic UI (7):
- web_ui/src/components/FailureSignatureList.jsx
- web_ui/src/components/FailureSignatureDetail.jsx
- web_ui/src/components/InvestigationReport.jsx
- web_ui/src/components/ActiveInvestigations.jsx
- web_ui/src/components/ActiveFixes.jsx
- web_ui/src/components/RepairCycleContainers.jsx
- web_ui/src/components/RepairCycleStatus.jsx

Supporting Components (13):
- web_ui/src/components/StatsCards.jsx
- web_ui/src/components/AgentState.jsx
- web_ui/src/components/ConfirmationModal.jsx
- web_ui/src/components/Toast.jsx
- web_ui/src/components/HeaderBox.jsx
- web_ui/src/components/HeaderStatsCard.jsx
- web_ui/src/components/HeaderClaudeUsage.jsx
- web_ui/src/components/CycleBoundingNode.jsx
- web_ui/src/components/Dashboard.jsx
- web_ui/src/components/Medic.jsx
- web_ui/src/components/ClaudeMedic.jsx
- web_ui/src/components/ReviewLearning.jsx
- web_ui/src/components/Projects.jsx

React Contexts (7):
- web_ui/src/contexts/AppStateProvider.jsx
- web_ui/src/contexts/AgentStateContext.jsx
- web_ui/src/contexts/ProjectStateContext.jsx
- web_ui/src/contexts/SocketContext.jsx
- web_ui/src/contexts/SystemStateContext.jsx
- web_ui/src/contexts/ThemeContext.jsx
- web_ui/src/contexts/index.js

Hooks & Utilities (10):
- web_ui/src/hooks/useActiveAgents.js
- web_ui/src/hooks/useActivePipelineAgents.js
- web_ui/src/hooks/useAgentActions.js
- web_ui/src/hooks/useCircuitBreakers.js
- web_ui/src/hooks/useProjects.js
- web_ui/src/hooks/useRepairCycles.js
- web_ui/src/hooks/useSystemHealth.js
- web_ui/src/utils/cycleLayout.js
- web_ui/src/utils/eventMerging.js
- web_ui/src/utils/polling.js
- web_ui/src/utils/stateHelpers.js
```

---

## Conclusion

This plan provides a **systematic, validated approach** to achieving 100% source code representation in the DR model. By following the phased implementation roadmap and adhering to CLI-first development practices, the model will evolve from 214 elements (35% coverage) to 397+ elements (100% coverage) over an 8-week period.

**Key Success Factors:**
1. Always use `dr add` CLI (never manual YAML)
2. Always include source tracking
3. Validate incrementally
4. Use changesets for all extraction work
5. Document cross-layer relationships

**Expected Outcome:**
- Complete architectural visibility
- Bidirectional code-to-model traceability
- Foundation for automated documentation
- Basis for impact analysis and change management
