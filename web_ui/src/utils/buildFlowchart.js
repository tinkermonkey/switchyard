import { MarkerType } from '@xyflow/react'
import { processEvents } from './eventProcessing/index.js'
import { getNodeType, HIDDEN_BY_DEFAULT_TYPES } from '../components/nodes/EVENT_TYPE_MAP.js'
import { extractClaudeLogSummaries } from './extractClaudeLogSummaries.js'

// Infrastructure and telemetry event types that are intentionally not rendered as graph nodes.
// See EVENT_TYPE_MAP.js for full documentation. Any event type NOT in this list and NOT
// handled by processEventToNode will trigger a console.warn to surface new/unhandled types.
const SKIP_EVENT_TYPES = new Set([
  'task_received', 'agent_started', 'agent_completed', 'agent_failed',
  'prompt_constructed',
  'claude_api_call_started', 'claude_api_call_completed', 'claude_api_call_failed',
  'container_launch_started', 'container_launch_succeeded', 'container_launch_failed',
  'container_execution_started', 'container_execution_completed', 'container_execution_failed',
  'response_chunk_received',
  'response_processing_started', 'response_processing_completed',
  'tool_execution_started', 'tool_execution_completed',
  'performance_metric', 'token_usage',
])
import {
  extractRepairCycleSummary,
  extractTestCycleSummary,
  extractSubCycleSummary,
  extractReviewCycleSummary,
  extractReviewIterationSummary,
  extractPRReviewCycleSummary,
  extractPRReviewPhaseSummary,
  extractConversationalLoopSummary,
  extractStatusProgressionSummary,
} from './cycleSummaries.js'

/**
 * Builds React Flow nodes and edges from pipeline run events.
 *
 * Node hierarchy produced:
 *   Root level (no parentId):
 *     - pipelineStarted / pipelineCompleted: static pipeline boundary nodes
 *     - <event-specific type>: prelude / postlude standalone decision/agent events
 *     - reviewCycleContainer: one per review cycle
 *     - repairCycleContainer: one per repair cycle
 *     - prReviewCycleContainer: one per PR review stage
 *     - conversationalLoopContainer: one per conversational loop
 *
 *   Level 2 (parentId = cycle container):
 *     - reviewCycleStarted / reviewCycleCompleted: direct event children of review cycle
 *     - iterationContainer: one per review iteration (review cycles)
 *     - iterationContainer: one per test cycle (repair cycles)
 *     - iterationContainer: one per phase (PR review cycles)
 *     - <event-specific type>: direct event children of repair cycle (residuals outside test cycles)
 *     - <event-specific type>: direct event children of conversational loop
 *
 *   Level 3 (parentId = iterationContainer):
 *     - <event-specific type>: events within review iterations
 *     - subCycleContainer: one per fix/warning/systemic/container sub-cycle (repair cycles only)
 *
 *   Level 4 (parentId = subCycleContainer, repair cycles only):
 *     - <event-specific type>: events within each sub-cycle
 *
 *   Node types are resolved via getNodeType() from nodes/EVENT_TYPE_MAP.js.
 *   Each type maps to a dedicated leaf component in nodes/. Unknown types fall
 *   back to 'pipelineEvent' (the base PipelineEventNode component).
 *
 * IMPORTANT: parent nodes must appear before their children in the returned array
 * (React Flow requirement for correct rendering).
 *
 * @param {Object} params
 * @param {Array}  params.events              - Pipeline run events (filtered, no claude_log)
 * @param {Array}  params.allEvents           - Full unfiltered event array (incl. claude_log) for token/tool enrichment
 * @param {Map}    params.existingCycles       - Existing cycles map (preserves collapse state)
 * @param {Object} params.workflowConfig       - Workflow configuration (passed to processEvents)
 * @param {Object} params.selectedPipelineRun  - Pipeline run metadata
 * @param {Set}    params.activeTaskIds        - Currently-active task IDs
 * @returns {{ nodes, edges, agentExecutions, updatedCycles }}
 */
export function buildFlowchart({
  events,
  allEvents = null,
  existingCycles = new Map(),
  workflowConfig = null,
  selectedPipelineRun,
  activeTaskIds = new Set(),
}) {
  if (!events.length || !selectedPipelineRun) {
    return { nodes: [], edges: [], agentExecutions: new Map(), updatedCycles: new Map() }
  }

  const _bfT0 = performance.now()

  // ── 0. Extract claude_log summaries for agent enrichment ─────────────────
  const claudeSummaries = extractClaudeLogSummaries(allEvents)

  // ── 1. Process events into structured model ──────────────────────────────
  const model = processEvents(events, workflowConfig)
  const { prelude, cycles, postlude, agentExecutions } = model
  const _bfT1 = performance.now()

  // Merge collapse state from existing cycles (preserve user's open/closed choices)
  const updatedCycles = new Map()
  cycles.forEach(cycle => {
    const existing = existingCycles.get(cycle.id)
    updatedCycles.set(cycle.id, {
      ...cycle,
      // Use processEvents default (cycle.isCollapsed) when no user-persisted state exists.
      // This lets individual cycle types set their own defaults (e.g. status_progression: true).
      isCollapsed: existing?.isCollapsed ?? cycle.isCollapsed,
    })
  })

  // Which container IDs are on the path to currently-active agents.
  // Used to annotate container nodes with containsActiveAgent so PipelineFlowGraph
  // can auto-expand them without callers needing to implement the logic themselves.
  const activeContainerIds = activeTaskIds.size > 0
    ? findActiveContainerPath(model, activeTaskIds)
    : new Set()

  // ── Helpers ──────────────────────────────────────────────────────────────

  /** The ordered leaf-node chain: we build this list, then connect sequentially. */
  const leafChain = [] // [{ id, timestamp }]

  const newNodes = []
  const newEdges = []

  /**
   * Build and push a decision event node.
   * Returns the created node (or null if the event should be skipped).
   */
  function makeDecisionNode(event, id, parentId = null) {
    const reason = event.reason || ''
    const metadataParts = []
    if (event.decision_category) metadataParts.push(`[${event.decision_category}]`)
    if (event.decision) {
      if (event.decision.selected_agent) metadataParts.push(`→ ${event.decision.selected_agent}`)
      if (event.decision.to_status) metadataParts.push(`→ ${event.decision.to_status}`)
      if (event.decision.action) metadataParts.push(event.decision.action)
    }
    if (reason) {
      const maxLen = 50
      metadataParts.push(reason.length > maxLen ? reason.substring(0, maxLen) + '…' : reason)
    }

    const nodeType = getNodeType(event.event_type)
    const node = {
      id,
      type: nodeType,
      position: { x: 0, y: 0 },
      data: {
        label: event.event_type
          ? event.event_type.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())
          : 'Unknown Event',
        type: 'decision_event',
        metadata: metadataParts.join(' • '),
        decision_category: event.decision_category,
        timestamp: event.timestamp,
        ...(HIDDEN_BY_DEFAULT_TYPES.has(nodeType) && { defaultHidden: true }),
      },
      draggable: false,
    }
    if (parentId) node.parentId = parentId
    return node
  }

  /**
   * Build and push an agent execution node.
   * Returns the created node (or null if execution not found).
   */
  function makeAgentNode(event, id, parentId = null) {
    const { agent, task_id: taskId } = event
    const executions = agentExecutions.get(agent) || []
    const executionIndex = executions.findIndex(e => e.taskId === taskId)
    if (executionIndex < 0) return null

    const execution = executions[executionIndex]
    const isActive = activeTaskIds.has(event.task_id)

    const durationMs = (execution.startTime && execution.endTime)
      ? new Date(execution.endTime) - new Date(execution.startTime)
      : null

    const claudeData = claudeSummaries.get(taskId) ?? null

    const node = {
      id,
      type: 'agentExecution',
      position: { x: 0, y: 0 },
      data: {
        label: agent.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()),
        type: 'agent_execution',
        status: execution.status,
        metadata: isActive ? 'Running' : execution.status,
        isActive,
        startTime: event.timestamp,
        durationMs,
        inputTokens: claudeData?.inputTokens ?? null,
        outputTokens: claudeData?.outputTokens ?? null,
        tools: claudeData?.tools ?? null,
      },
      draggable: false,
    }
    if (parentId) node.parentId = parentId
    return node
  }

  /**
   * Process a raw event into a node, push to newNodes, and add to leafChain.
   * Skips agent_completed / agent_failed events (status is reflected on the agent node).
   * Returns the node id if a node was created, or null.
   */
  function processEventToNode(event, idPrefix, parentId = null) {
    // Inferred close events are synthetic bounds only — never render as nodes
    if (event._inferred) return null

    let node = null

    if (event.event_category === 'decision' && !SKIP_EVENT_TYPES.has(event.event_type)) {
      const id = `${idPrefix}-dec-${event.timestamp}`
      node = makeDecisionNode(event, id, parentId)
    } else if (
      event.event_category === 'agent_lifecycle' &&
      event.event_type === 'agent_initialized'
    ) {
      const id = `${idPrefix}-agent-${event.agent}-${event.task_id}`
      node = makeAgentNode(event, id, parentId)
    } else if (!SKIP_EVENT_TYPES.has(event.event_type)) {
      // Not a known infrastructure/telemetry type and not a handled category.
      // This indicates a new backend event type with no frontend handler — add it to
      // EVENT_TYPE_MAP.js (for rendering) or SKIP_EVENT_TYPES above (to silence).
      console.warn('[buildFlowchart] unrendered event (missing from EVENT_TYPE_MAP or SKIP_EVENT_TYPES):',
        event.event_category, event.event_type, event.timestamp)
    }
    // Known infrastructure/telemetry events in SKIP_EVENT_TYPES are intentionally not rendered.

    if (node) {
      newNodes.push(node)
      leafChain.push({ id: node.id, timestamp: event.timestamp })
      return node.id
    }
    return null
  }

  // ── 2. Pipeline started node ──────────────────────────────────────────────
  newNodes.push({
    id: 'created',
    type: 'pipelineStarted',
    position: { x: 0, y: 0 },
    data: {
      label: 'Pipeline Started',
      type: 'pipeline_created',
      metadata: new Date(selectedPipelineRun.started_at).toLocaleString(),
    },
    draggable: false,
  })
  leafChain.push({ id: 'created', timestamp: selectedPipelineRun.started_at })

  // ── 3. Prelude events ─────────────────────────────────────────────────────
  prelude.events.forEach(event => {
    processEventToNode(event, 'pre', null)
  })

  // ── 4. Cycles ─────────────────────────────────────────────────────────────
  cycles.forEach(cycle => {
    const cycleState = updatedCycles.get(cycle.id)
    const isCollapsed = cycleState?.isCollapsed ?? false

    if (cycle.type === 'review_cycle') {
      // ── Review cycle container ──────────────────────────────────────────
      const rcNode = {
        id: cycle.id,
        type: 'reviewCycleContainer',
        position: { x: 0, y: 0 },
        data: {
          cycleId: cycle.id,
          cycleType: 'review_cycle',
          label: 'Review Cycle',
          iterationCount: cycle.iterations.length,
          isCollapsed,
          containsActiveAgent: activeContainerIds.has(cycle.id),
          onToggleCollapse: null, // injected by caller
          startTime: cycle.startEvent?.timestamp,
          endTime: cycle.endEvent?.timestamp,
          summary: extractReviewCycleSummary(cycle),
        },
        // Collapsed: no style/measured — RF auto-sizes from content via ResizeObserver.
        // Expanded: style:0x0 + measured:1x1 so nodesInitialized isn't blocked while
        // children are still being measured.
        style: isCollapsed ? {} : { width: 0, height: 0 },
        ...(isCollapsed ? {} : { measured: { width: 1, height: 1 } }),
        draggable: false,
      }
      newNodes.push(rcNode)

      if (isCollapsed) {
        // Collapsed: the container is the only leaf in the chain
        leafChain.push({ id: cycle.id, timestamp: cycle.startEvent?.timestamp })
      } else {
        // Expanded: collect all leaf-chain entries, then sort chronologically so residual
        // events (in the cycle window but outside any iteration) are interleaved correctly.
        const allReviewEntries = []

        // review_cycle_started → direct child (pre-created node, goes straight to leafChain)
        if (cycle.startEvent) {
          const startId = `${cycle.id}-start`
          newNodes.push(makeDecisionNode(cycle.startEvent, startId, cycle.id))
          allReviewEntries.push({ leafId: startId, timestamp: cycle.startEvent.timestamp })
        }

        // Residual events: in the cycle window but not claimed by any iteration
        ;(cycle.events ?? []).forEach(event => {
          allReviewEntries.push({ event, parentId: cycle.id, timestamp: event.timestamp })
        })

        // Iteration containers
        cycle.iterations.forEach(iteration => {
          const iterId = `${cycle.id}-iter-${iteration.number}`
          const iterState = existingCycles.get(iterId)
          const iterCollapsed = iterState?.isCollapsed ?? true  // default: collapsed
          updatedCycles.set(iterId, { isCollapsed: iterCollapsed })

          newNodes.push({
            id: iterId,
            type: 'iterationContainer',
            parentId: cycle.id,
            position: { x: 0, y: 0 },
            zIndex: 1,
            data: {
              cycleId: iterId,
              cycleType: 'review_iteration',
              iterationNumber: iteration.number,
              label: `Iteration ${iteration.number}`,
              iterationCount: iteration.events.length + 1,
              isCollapsed: iterCollapsed,
              containsActiveAgent: activeContainerIds.has(iterId),
              onToggleCollapse: null, // injected by caller
              startTime: iteration.startEvent?.timestamp,  // used by cycleLayout for ordering
              summary: extractReviewIterationSummary(iteration, cycle.startEvent),
            },
            style: iterCollapsed ? {} : { width: 0, height: 0 },
            ...(iterCollapsed ? {} : { measured: { width: 1, height: 1 } }),
            draggable: false,
          })

          if (iterCollapsed) {
            allReviewEntries.push({ containerId: iterId, timestamp: iteration.startEvent?.timestamp })
          } else {
            const allIterEvents = [iteration.startEvent, ...iteration.events]
              .filter(Boolean)
              .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))
            allIterEvents.forEach(event => {
              allReviewEntries.push({ event, parentId: iterId, timestamp: event.timestamp })
            })
          }
        })

        // review_cycle_completed → direct child (skip if inferred)
        if (cycle.endEvent && !cycle.endEvent._inferred) {
          const endId = `${cycle.id}-end`
          newNodes.push(makeDecisionNode(cycle.endEvent, endId, cycle.id))
          allReviewEntries.push({ leafId: endId, timestamp: cycle.endEvent.timestamp })
        }

        // Process all entries in chronological order for correct leafChain sequencing
        allReviewEntries
          .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))
          .forEach(entry => {
            if (entry.leafId) {
              leafChain.push({ id: entry.leafId, timestamp: entry.timestamp })
            } else if (entry.containerId) {
              leafChain.push({ id: entry.containerId, timestamp: entry.timestamp })
            } else {
              processEventToNode(entry.event, entry.parentId, entry.parentId)
            }
          })
      }
    } else if (cycle.type === 'repair_cycle') {
      // ── Repair cycle container ──────────────────────────────────────────
      const rpcNode = {
        id: cycle.id,
        type: 'repairCycleContainer',
        position: { x: 0, y: 0 },
        data: {
          cycleId: cycle.id,
          cycleType: 'repair_cycle',
          label: 'Repair Cycle',
          iterationCount: cycle.testCycles?.length ?? 0,
          isCollapsed,
          containsActiveAgent: activeContainerIds.has(cycle.id),
          onToggleCollapse: null, // injected by caller
          startTime: cycle.startEvent?.timestamp,
          endTime: cycle.endEvent?.timestamp,
          summary: extractRepairCycleSummary(cycle),
        },
        style: isCollapsed ? {} : { width: 0, height: 0 },
        ...(isCollapsed ? {} : { measured: { width: 1, height: 1 } }),
        draggable: false,
      }
      newNodes.push(rpcNode)

      if (isCollapsed) {
        leafChain.push({ id: cycle.id, timestamp: cycle.startEvent?.timestamp })
      } else if (cycle.testCycles) {
        // Collect ALL entries (repair-level residuals + tc-level events) for one
        // chronological sort so leafChain sequencing is correct across the whole cycle.
        // Entries are either { event, parentId, timestamp } or { containerId, timestamp }
        // for collapsed containers that go directly into the leafChain.
        const allRepairEntries = []

        // Repair-level residual events → direct children of the repair cycle container.
        // Exclude startEvent (repair_cycle agent_initialized) — it is the container
        // boundary marker and is already represented by the repairCycleContainer node
        // itself. Rendering it as a child produces a misleading "Repair Cycle / failed"
        // node (the agent execution's final status) in the first slot of the container.
        ;(cycle.events ?? [])
          .filter(e => e !== cycle.startEvent)
          .forEach(event => {
            allRepairEntries.push({ event, parentId: cycle.id, timestamp: event.timestamp })
          })

        // Test cycle containers + their sub-cycles + events
        cycle.testCycles.forEach(tc => {
          const tcId = `${cycle.id}-tc-${tc.number}`
          const tcState = existingCycles.get(tcId)
          const tcCollapsed = tcState?.isCollapsed ?? true  // default: collapsed
          updatedCycles.set(tcId, { isCollapsed: tcCollapsed })

          const tcNode = {
            id: tcId,
            type: 'iterationContainer',
            parentId: cycle.id,
            position: { x: 0, y: 0 },
            zIndex: 1,
            data: {
              cycleId: tcId,
              cycleType: 'repair_test_cycle',
              iterationNumber: tc.number,
              label: tc.testType,
              iterationCount: tc.subCycles.length,
              isCollapsed: tcCollapsed,
              containsActiveAgent: activeContainerIds.has(tcId),
              onToggleCollapse: null, // injected by caller
              summary: extractTestCycleSummary(tc),
            },
            style: tcCollapsed ? {} : { width: 0, height: 0 },
            ...(tcCollapsed ? {} : { measured: { width: 1, height: 1 } }),
            draggable: false,
          }
          newNodes.push(tcNode)

          if (tcCollapsed) {
            allRepairEntries.push({ containerId: tcId, timestamp: tc.startEvent?.timestamp ?? 0 })
          } else {
            // Recursively create sub-cycle containers and collect their events/nested containers.
            // Called with the outer tc container as the initial parentId; nested sub-cycles
            // use their parent SC's id as parentId so React Flow renders them correctly.
            const addSubCycleEntries = (sc, parentId) => {
              const scId = `${parentId}-${sc.cycleType}-${sc.number}`
              const scState = existingCycles.get(scId)
              const scCollapsed = scState?.isCollapsed ?? true  // default: collapsed
              updatedCycles.set(scId, { isCollapsed: scCollapsed })

              newNodes.push({
                id: scId,
                type: 'subCycleContainer',
                parentId,
                position: { x: 0, y: 0 },
                zIndex: 1,
                data: {
                  cycleId: scId,
                  label: sc.label,
                  cycleType: sc.cycleType,
                  iterationNumber: sc.number,
                  iterationCount: sc.events.length + (sc.subCycles?.length ?? 0),
                  startEvent: sc.startEvent,
                  endEvent: sc.endEvent,
                  isCollapsed: scCollapsed,
                  containsActiveAgent: activeContainerIds.has(scId),
                  onToggleCollapse: null, // injected by caller
                  summary: extractSubCycleSummary(sc),
                },
                style: scCollapsed ? {} : { width: 0, height: 0 },
                ...(scCollapsed ? {} : { measured: { width: 1, height: 1 } }),
                draggable: false,
              })

              if (scCollapsed) {
                allRepairEntries.push({ containerId: scId, timestamp: sc.startEvent?.timestamp ?? 0 })
              } else {
                // Recurse into nested sub-cycles first (parent node already pushed above)
                sc.subCycles?.forEach(nsc => addSubCycleEntries(nsc, scId))
                // Then add residual events (outside any nested sub-cycle windows)
                ;[sc.startEvent, ...sc.events, sc.endEvent].filter(Boolean).forEach(event => {
                  allRepairEntries.push({ event, parentId: scId, timestamp: event.timestamp })
                })
              }
            }

            tc.subCycles.forEach(sc => addSubCycleEntries(sc, tcId))

            // Residual events within the test cycle go directly under the tc container
            ;[tc.startEvent, ...tc.events, tc.endEvent].filter(Boolean).forEach(event => {
              allRepairEntries.push({ event, parentId: tcId, timestamp: event.timestamp })
            })
          }
        })

        // Process all entries in chronological order for correct leafChain sequencing
        allRepairEntries
          .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))
          .forEach(entry => {
            if (entry.containerId) {
              leafChain.push({ id: entry.containerId, timestamp: entry.timestamp })
            } else {
              processEventToNode(entry.event, entry.parentId, entry.parentId)
            }
          })
      }
    } else if (cycle.type === 'pr_review_cycle') {
      // ── PR review cycle container ───────────────────────────────────────
      const prNode = {
        id: cycle.id,
        type: 'prReviewCycleContainer',
        position: { x: 0, y: 0 },
        data: {
          cycleId: cycle.id,
          cycleType: 'pr_review_cycle',
          label: 'PR Review',
          iterationCount: cycle.phases?.length ?? 0,
          isCollapsed,
          containsActiveAgent: activeContainerIds.has(cycle.id),
          onToggleCollapse: null, // injected by caller
          startTime: cycle.startEvent?.timestamp,
          endTime: cycle.endEvent?.timestamp,
          summary: extractPRReviewCycleSummary(cycle),
        },
        style: isCollapsed ? {} : { width: 0, height: 0 },
        ...(isCollapsed ? {} : { measured: { width: 1, height: 1 } }),
        draggable: false,
      }
      newNodes.push(prNode)

      if (isCollapsed) {
        leafChain.push({ id: cycle.id, timestamp: cycle.startEvent?.timestamp })
      } else {
        // Expanded: collect all leaf-chain entries, then sort chronologically so residual
        // events (between stage_started and first phase, or between phases) are interleaved.
        const allPREntries = []

        // pr_review_stage_started → direct child (pre-created node)
        if (cycle.startEvent) {
          const startId = `${cycle.id}-start`
          newNodes.push(makeDecisionNode(cycle.startEvent, startId, cycle.id))
          allPREntries.push({ leafId: startId, timestamp: cycle.startEvent.timestamp })
        }

        // Residual events: in the cycle window but not claimed by any phase
        ;(cycle.events ?? []).forEach(event => {
          allPREntries.push({ event, parentId: cycle.id, timestamp: event.timestamp })
        })

        // Phase containers
        ;(cycle.phases ?? []).forEach(phase => {
          const phaseId = `${cycle.id}-phase-${phase.number}`
          const phaseState = existingCycles.get(phaseId)
          const phaseCollapsed = phaseState?.isCollapsed ?? true  // default: collapsed
          updatedCycles.set(phaseId, { isCollapsed: phaseCollapsed })

          newNodes.push({
            id: phaseId,
            type: 'iterationContainer',
            parentId: cycle.id,
            position: { x: 0, y: 0 },
            zIndex: 1,
            data: {
              cycleId: phaseId,
              cycleType: 'pr_review_phase',
              iterationNumber: phase.number,
              label: `Phase ${phase.number}`,
              iterationCount: phase.events.length + 1,
              isCollapsed: phaseCollapsed,
              containsActiveAgent: activeContainerIds.has(phaseId),
              onToggleCollapse: null, // injected by caller
              startTime: phase.startEvent?.timestamp,  // used by cycleLayout for ordering
              summary: extractPRReviewPhaseSummary(phase),
            },
            style: phaseCollapsed ? {} : { width: 0, height: 0 },
            ...(phaseCollapsed ? {} : { measured: { width: 1, height: 1 } }),
            draggable: false,
          })

          if (phaseCollapsed) {
            allPREntries.push({ containerId: phaseId, timestamp: phase.startEvent?.timestamp })
          } else {
            const allPhaseEvents = [phase.startEvent, ...phase.events]
              .filter(Boolean)
              .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))
            allPhaseEvents.forEach(event => {
              allPREntries.push({ event, parentId: phaseId, timestamp: event.timestamp })
            })
          }
        })

        // pr_review_stage_completed → direct child (skip if inferred)
        if (cycle.endEvent && !cycle.endEvent._inferred) {
          const endId = `${cycle.id}-end`
          newNodes.push(makeDecisionNode(cycle.endEvent, endId, cycle.id))
          allPREntries.push({ leafId: endId, timestamp: cycle.endEvent.timestamp })
        }

        // Process all entries in chronological order for correct leafChain sequencing
        allPREntries
          .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))
          .forEach(entry => {
            if (entry.leafId) {
              leafChain.push({ id: entry.leafId, timestamp: entry.timestamp })
            } else if (entry.containerId) {
              leafChain.push({ id: entry.containerId, timestamp: entry.timestamp })
            } else {
              processEventToNode(entry.event, entry.parentId, entry.parentId)
            }
          })
      }
    } else if (cycle.type === 'conversational_loop') {
      // ── Conversational loop container ───────────────────────────────────
      const clNode = {
        id: cycle.id,
        type: 'conversationalLoopContainer',
        position: { x: 0, y: 0 },
        data: {
          cycleId: cycle.id,
          cycleType: 'conversational_loop',
          label: 'Conversation',
          iterationCount: cycle.events?.length ?? 0,
          isCollapsed,
          containsActiveAgent: activeContainerIds.has(cycle.id),
          onToggleCollapse: null, // injected by caller
          startTime: cycle.startEvent?.timestamp,
          endTime: cycle.endEvent?.timestamp,
          summary: extractConversationalLoopSummary(cycle),
        },
        style: isCollapsed ? {} : { width: 0, height: 0 },
        ...(isCollapsed ? {} : { measured: { width: 1, height: 1 } }),
        draggable: false,
      }
      newNodes.push(clNode)

      if (isCollapsed) {
        leafChain.push({ id: cycle.id, timestamp: cycle.startEvent?.timestamp })
      } else {
        // Loop open event → direct child, leftmost
        if (cycle.startEvent) {
          const startId = `${cycle.id}-start`
          const startNode = makeDecisionNode(cycle.startEvent, startId, cycle.id)
          newNodes.push(startNode)
          leafChain.push({ id: startId, timestamp: cycle.startEvent.timestamp })
        }

        // Child events (flat — no iteration containers)
        ;(cycle.events ?? [])
          .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))
          .forEach(event => processEventToNode(event, cycle.id, cycle.id))

        // Loop close event → direct child, rightmost
        if (cycle.endEvent && !cycle.endEvent._inferred) {
          const endId = `${cycle.id}-end`
          const endNode = makeDecisionNode(cycle.endEvent, endId, cycle.id)
          newNodes.push(endNode)
          leafChain.push({ id: endId, timestamp: cycle.endEvent.timestamp })
        }
      }
    } else if (cycle.type === 'status_progression') {
      // ── Status progression container ─────────────────────────────────────
      const spNode = {
        id: cycle.id,
        type: 'statusProgressionContainer',
        position: { x: 0, y: 0 },
        data: {
          cycleId: cycle.id,
          cycleType: 'status_progression',
          label: 'Status Move',
          iterationCount: cycle.events?.length ?? 0,
          isCollapsed,
          containsActiveAgent: activeContainerIds.has(cycle.id),
          onToggleCollapse: null, // injected by caller
          startTime: cycle.startEvent?.timestamp,
          endTime: cycle.endEvent?.timestamp,
          summary: extractStatusProgressionSummary(cycle),
        },
        style: isCollapsed ? {} : { width: 0, height: 0 },
        ...(isCollapsed ? {} : { measured: { width: 1, height: 1 } }),
        draggable: false,
      }
      newNodes.push(spNode)

      if (isCollapsed) {
        leafChain.push({ id: cycle.id, timestamp: cycle.startEvent?.timestamp })
      } else {
        // Start event → direct child, leftmost
        if (cycle.startEvent) {
          const startId = `${cycle.id}-start`
          newNodes.push(makeDecisionNode(cycle.startEvent, startId, cycle.id))
          leafChain.push({ id: startId, timestamp: cycle.startEvent.timestamp })
        }

        // Child events (flat)
        ;(cycle.events ?? [])
          .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))
          .forEach(event => processEventToNode(event, cycle.id, cycle.id))

        // End event → direct child, rightmost (skip if inferred)
        if (cycle.endEvent && !cycle.endEvent._inferred) {
          const endId = `${cycle.id}-end`
          newNodes.push(makeDecisionNode(cycle.endEvent, endId, cycle.id))
          leafChain.push({ id: endId, timestamp: cycle.endEvent.timestamp })
        }
      }
    } else if (cycle.type === 'review_iteration') {
      // ── Standalone review iteration (orphaned — no parent review cycle) ──
      // Rendered as a top-level iterationContainer so it gets expand/collapse
      // and active-agent highlighting without requiring a parent cycle container.
      const iterState = updatedCycles.get(cycle.id)
      const iterCollapsed = iterState?.isCollapsed ?? false  // default: expanded at top level
      updatedCycles.set(cycle.id, { isCollapsed: iterCollapsed })

      newNodes.push({
        id: cycle.id,
        type: 'iterationContainer',
        position: { x: 0, y: 0 },
        data: {
          cycleId: cycle.id,
          cycleType: 'review_iteration',
          iterationNumber: cycle.iterationNumber,
          label: `Iteration ${cycle.iterationNumber}`,
          iterationCount: cycle.events.length + 1,
          isCollapsed: iterCollapsed,
          containsActiveAgent: activeContainerIds.has(cycle.id),
          onToggleCollapse: null, // injected by caller
          startTime: cycle.startEvent?.timestamp,
          summary: extractReviewIterationSummary(cycle, null),
        },
        style: iterCollapsed ? {} : { width: 0, height: 0 },
        ...(iterCollapsed ? {} : { measured: { width: 1, height: 1 } }),
        draggable: false,
      })

      if (iterCollapsed) {
        leafChain.push({ id: cycle.id, timestamp: cycle.startEvent?.timestamp })
      } else {
        const allIterEvents = [cycle.startEvent, ...cycle.events]
          .filter(Boolean)
          .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))
        allIterEvents.forEach(event => {
          processEventToNode(event, cycle.id, cycle.id)
        })
      }
    }
  })

  // ── 5. Postlude events ────────────────────────────────────────────────────
  postlude.events.forEach(event => {
    processEventToNode(event, 'post', null)
  })

  // ── 6. Pipeline completed node ────────────────────────────────────────────
  if (selectedPipelineRun.status === 'completed') {
    newNodes.push({
      id: 'completed',
      type: 'pipelineCompleted',
      position: { x: 0, y: 0 },
      data: {
        label: 'Pipeline Completed',
        type: 'pipeline_completed',
        metadata: selectedPipelineRun.ended_at
          ? new Date(selectedPipelineRun.ended_at).toLocaleString()
          : '',
      },
      draggable: false,
    })
    leafChain.push({ id: 'completed', timestamp: selectedPipelineRun.ended_at })
  }

  // ── 7. Build sequential edges through leaf chain ──────────────────────────
  for (let i = 1; i < leafChain.length; i++) {
    const source = leafChain[i - 1].id
    const target = leafChain[i].id
    if (source && target && source !== target) {
      newEdges.push({
        id: `edge-${source}-${target}`,
        source,
        target,
        type: 'smart',
        markerEnd: { type: MarkerType.ArrowClosed, color: '#6e7681' },
        style: { stroke: '#6e7681' },
      })
    }
  }

  // ── 8. Ensure correct React Flow node ordering (parents before children) ──
  // newNodes is already built parents-first (containers added before their children above).

  const _bfTEnd = performance.now()
  console.log(
    `[PerfGraph] buildFlowchart: ${(_bfTEnd - _bfT0).toFixed(1)}ms` +
    ` | nodes:${newNodes.length} edges:${newEdges.length} leafChain:${leafChain.length}` +
    ` | processEvents:${(_bfT1 - _bfT0).toFixed(1)}ms` +
    ` | nodeBuild:${(_bfTEnd - _bfT1).toFixed(1)}ms`
  )

  return { nodes: newNodes, edges: newEdges, agentExecutions, updatedCycles, model }
}

/**
 * Finds all container IDs (cycle, iteration, sub-cycle) that contain events
 * from currently-active agents, returning the full hierarchy path to expand.
 *
 * @param {Object} model          - Processed event model (from processEvents)
 * @param {Set}    activeTaskIds  - Set of task_id strings for currently-running agent executions
 * @returns {Set<string>} Container IDs that should be auto-expanded
 */
export function findActiveContainerPath(model, activeTaskIds) {
  if (!activeTaskIds.size) return new Set()
  const result = new Set()

  const hasActive = (events) =>
    events.some(e => e.event_type === 'agent_initialized' && activeTaskIds.has(e.task_id))

  model.cycles.forEach(cycle => {
    if (cycle.type === 'review_cycle') {
      // Check residual cycle-level events (not inside any iteration)
      const cycleResiduals = [cycle.startEvent, ...(cycle.events ?? []), cycle.endEvent].filter(Boolean)
      if (hasActive(cycleResiduals)) result.add(cycle.id)
      cycle.iterations?.forEach(iter => {
        const iterId = `${cycle.id}-iter-${iter.number}`
        const iterEvents = [iter.startEvent, ...iter.events].filter(Boolean)
        if (hasActive(iterEvents)) {
          result.add(cycle.id)
          result.add(iterId)
        }
      })
    } else if (cycle.type === 'repair_cycle') {
      cycle.testCycles?.forEach(tc => {
        const tcId = `${cycle.id}-tc-${tc.number}`

        // Recursively check sub-cycles and their nested sub-cycles.
        // ancestorIds: ordered list of container IDs that must be expanded if a
        // descendant contains an active agent.
        function checkSubCycle(sc, parentId, ancestorIds) {
          const scId = `${parentId}-${sc.cycleType}-${sc.number}`
          const scEvents = [sc.startEvent, ...sc.events, sc.endEvent].filter(Boolean)
          if (hasActive(scEvents)) {
            ancestorIds.forEach(id => result.add(id))
            result.add(scId)
          }
          sc.subCycles?.forEach(nsc =>
            checkSubCycle(nsc, scId, [...ancestorIds, scId])
          )
        }

        tc.subCycles?.forEach(sc => checkSubCycle(sc, tcId, [cycle.id, tcId]))

        const tcEvents = [tc.startEvent, ...(tc.events ?? []), tc.endEvent].filter(Boolean)
        if (hasActive(tcEvents)) {
          result.add(cycle.id)
          result.add(tcId)
        }
      })
      const cycleEvents = [cycle.startEvent, ...(cycle.events ?? []), cycle.endEvent].filter(Boolean)
      if (hasActive(cycleEvents)) {
        result.add(cycle.id)
      }
    } else if (cycle.type === 'pr_review_cycle') {
      // Check residual cycle-level events (not inside any phase)
      const prResiduals = [cycle.startEvent, ...(cycle.events ?? []), cycle.endEvent].filter(Boolean)
      if (hasActive(prResiduals)) result.add(cycle.id)
      cycle.phases?.forEach(phase => {
        const phaseId = `${cycle.id}-phase-${phase.number}`
        const phaseEvents = [phase.startEvent, ...(phase.events ?? [])].filter(Boolean)
        if (hasActive(phaseEvents)) {
          result.add(cycle.id)
          result.add(phaseId)
        }
      })
    } else if (cycle.type === 'conversational_loop') {
      const loopEvents = [cycle.startEvent, ...(cycle.events ?? []), cycle.endEvent].filter(Boolean)
      if (hasActive(loopEvents)) {
        result.add(cycle.id)
      }
    } else if (cycle.type === 'status_progression') {
      const spEvents = [cycle.startEvent, ...(cycle.events ?? []), cycle.endEvent].filter(Boolean)
      if (hasActive(spEvents)) {
        result.add(cycle.id)
      }
    } else if (cycle.type === 'review_iteration') {
      // Standalone orphaned iteration — no parent container, just check the iteration itself
      const iterEvents = [cycle.startEvent, ...(cycle.events ?? [])].filter(Boolean)
      if (hasActive(iterEvents)) {
        result.add(cycle.id)
      }
    }
  })

  return result
}
