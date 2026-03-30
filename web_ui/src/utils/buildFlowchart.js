import { MarkerType } from '@xyflow/react'
import { processEvents } from './eventProcessing/index.js'
import { getNodeType, EXCLUDED_EVENT_TYPES } from '../components/nodes/EVENT_TYPE_MAP.js'
import { extractClaudeLogSummaries } from './extractClaudeLogSummaries.js'
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
  extractAgentExecutionSummary,
} from './cycleSummaries.js'

// Alias for the shared exclusion set (see EVENT_TYPE_MAP.js for full documentation).
// Any event type NOT in this set and NOT handled by processEventToNode will trigger
// a console.warn to surface new/unhandled types.
const SKIP_EVENT_TYPES = EXCLUDED_EVENT_TYPES

/**
 * Node types visible in the default ("Simplify") view of PipelineFlowGraph.
 * Any node type NOT in this set gets defaultHidden: true and is hidden when showAllNodes
 * is false. Nodes in this set are always shown.
 *
 * Add new event node types here when they should appear in the simplified view.
 * The fallback 'pipelineEvent' type is included so unknown/unmapped event types are
 * always visible rather than silently hidden.
 */
const DEFAULT_SHOWN_NODE_TYPES = new Set([
  // Fallback — always show unknown event types
  'pipelineEvent',

  // Agent execution
  'agentExecution',
  'agentCompleted',
  'agentFailed',

  // Review cycle — signal events only
  'reviewCycleEscalated',

  // Repair cycle — signal events only
  'repairCycleEnvRebuildStarted',
  'repairCycleEnvRebuildCompleted',
  'repairCycleFileFixStarted',
  'repairCycleFileFixCompleted',
  'repairCycleFileFixFailed',
  'repairCycleContainerCheckpointUpdated',
  'repairCycleContainerRecovered',

  // Agent routing & selection
  'agentRoutingDecision',
  'agentSelected',
  'workspaceRoutingDecision',

  // Pipeline & stage progression
  'pipelineStageTransition',
  'pipelineRunStarted',
  'pipelineRunCompleted',
  'pipelineRunFailed',

  // Feedback monitoring
  'feedbackDetected',
  'feedbackListeningStarted',
  'feedbackListeningStopped',
  'feedbackIgnored',

  // Conversational loop — signal events only
  'conversationalQuestionRouted',
  'conversationalLoopPaused',
  'conversationalLoopResumed',

  // Error handling & circuit breakers
  'errorEncountered',
  'errorRecovered',
  'circuitBreakerOpened',
  'circuitBreakerClosed',
  'retryAttempted',

  // Task queue management
  'taskQueued',
  'taskDequeued',
  'taskPriorityChanged',
  'taskCancelled',

  // Branch management — signal events only
  'branchCreated',
  'branchConflictDetected',
  'branchStaleDetected',
  'branchSelectionEscalated',

  // Issue management
  'subIssueCreated',
  'subIssueCreationFailed',

  // System operations
  'executionStateReconciled',
  'statusValidationFailure',
  'resultPersistenceFailed',
  'fallbackStorageUsed',
  'outputValidationFailed',
  'emptyOutputDetected',
  'containerResultRecovered',
])

/**
 * Builds React Flow nodes and edges from pipeline run events.
 *
 * Node hierarchy produced:
 *   Root level (no parentId):
 *     - pipelineStarted / pipelineCompleted: static pipeline boundary nodes
 *     - <event-specific type>: prelude / postlude standalone decision/agent events
 *     - subCycleContainer: one per cycle (review, repair, PR review, conversational, status, agent execution)
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
 * @returns {{ nodes, edges, updatedCycles }}
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
    return { nodes: [], edges: [], updatedCycles: new Map() }
  }

  const _bfT0 = performance.now()

  // ── 0. Extract claude_log summaries for agent enrichment ─────────────────
  const claudeSummaries = extractClaudeLogSummaries(allEvents)

  // ── 1. Process events into structured model ──────────────────────────────
  const model = processEvents(events, workflowConfig)
  const { prelude, cycles, postlude, agentExecutionBoundaries } = model
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
        event,
        ...(!DEFAULT_SHOWN_NODE_TYPES.has(nodeType) && { defaultHidden: true }),
      },
      draggable: false,
    }
    if (parentId) node.parentId = parentId
    return node
  }

  /**
   * Create subCycleContainer nodes for agent execution boundaries found within
   * a given set of events. Matches agentExecutionBoundaries whose startEvent
   * appears in the provided events array.
   *
   * Returns a Set of events claimed by agent execution containers, so the caller
   * can exclude them from standalone rendering (preventing double-rendering).
   */
  function createAgentExecutionContainers(events, idPrefix, parentId) {
    const claimed = new Set()
    if (!agentExecutionBoundaries.length || !events.length) return claimed

    const eventSet = new Set(events)
    const matching = agentExecutionBoundaries.filter(b => eventSet.has(b.startEvent))

    matching.forEach(boundary => {
      const { startEvent, endEvent } = boundary
      const taskId = startEvent.task_id
      if (!taskId) return  // skip malformed boundaries
      const agent = startEvent.agent ?? 'agent'
      const containerId = `${idPrefix}-agent_execution-${taskId}`

      const existingState = existingCycles.get(containerId) ?? updatedCycles.get(containerId)
      const isCollapsed = existingState?.isCollapsed ?? true
      updatedCycles.set(containerId, { isCollapsed })

      const isActive = activeTaskIds.has(taskId)
      const startMs = new Date(startEvent.timestamp).getTime()
      const endMs = endEvent ? new Date(endEvent.timestamp).getTime() : Date.now()

      // Child events: everything between start and end in the provided events
      const childEvents = [startEvent, ...events.filter(e => {
        if (e === startEvent || e === endEvent) return false
        const t = new Date(e.timestamp).getTime()
        return t > startMs && t < endMs
      }), endEvent].filter(Boolean)

      // Mark all child events as claimed so the caller skips them
      childEvents.forEach(e => claimed.add(e))

      const childCount = childEvents.filter(e => !SKIP_EVENT_TYPES.has(e.event_type)).length

      const containerNode = {
        id: containerId,
        type: 'subCycleContainer',
        position: { x: 0, y: 0 },
        data: {
          cycleId: containerId,
          cycleType: 'agent_execution',
          label: agent.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()),
          iterationCount: childCount,
          isCollapsed,
          containsActiveAgent: activeContainerIds.has(containerId) || isActive,
          isActive,
          onToggleCollapse: null,
          startTime: startEvent.timestamp,
          endTime: endEvent?.timestamp ?? null,
          summary: extractAgentExecutionSummary(boundary, claudeSummaries),
          event: startEvent,
        },
        style: isCollapsed ? {} : { width: 0, height: 0 },
        ...(isCollapsed ? {} : { measured: { width: 1, height: 1 } }),
        draggable: false,
      }
      if (parentId) containerNode.parentId = parentId
      newNodes.push(containerNode)

      if (!isCollapsed) {
        childEvents
          .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))
          .forEach(event => processEventToNode(event, containerId, containerId))
      }
    })

    return claimed
  }

  /**
   * Process a section (prelude or postlude) — renders events and agent execution
   * containers, sorted chronologically.
   */
  function processSection(section, idPrefix) {
    const claimed = createAgentExecutionContainers(section.events, idPrefix, null)
    section.events.forEach(event => {
      if (!claimed.has(event)) processEventToNode(event, idPrefix, null)
    })
  }

  /**
   * Process a raw event into a node and push to newNodes.
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
      const { agent, task_id: taskId } = event
      const boundary = agentExecutionBoundaries.find(b => b.startEvent === event)
      const isActive = activeTaskIds.has(taskId)
      const claudeData = claudeSummaries.get(taskId) ?? null

      let status = 'running'
      let durationMs = null
      if (boundary?.endEvent && !boundary.endEvent._inferred) {
        status = boundary.endEvent.event_type === 'agent_completed' ? 'completed' : 'failed'
        durationMs = new Date(boundary.endEvent.timestamp) - new Date(event.timestamp)
        if (isNaN(durationMs) || durationMs < 0) durationMs = null
      }

      const id = `${idPrefix}-agentlc-${event.event_type}-${event.timestamp}`
      const nodeType = getNodeType(event.event_type)
      node = {
        id,
        type: nodeType,
        position: { x: 0, y: 0 },
        data: {
          label: (agent ?? 'agent').replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()),
          type: 'agent_execution',
          status,
          metadata: isActive ? 'Running' : status,
          isActive,
          timestamp: event.timestamp,
          startTime: event.timestamp,
          durationMs,
          inputTokens: claudeData?.inputTokens ?? null,
          outputTokens: claudeData?.outputTokens ?? null,
          tools: claudeData?.tools ?? null,
          event,
          ...(!DEFAULT_SHOWN_NODE_TYPES.has(nodeType) && { defaultHidden: true }),
        },
        draggable: false,
      }
      if (parentId) node.parentId = parentId
    } else if (
      event.event_category === 'agent_lifecycle' &&
      (event.event_type === 'agent_completed' || event.event_type === 'agent_failed')
    ) {
      const id = `${idPrefix}-agentlc-${event.event_type}-${event.timestamp}`
      const nodeType = getNodeType(event.event_type)
      const metadataParts = []
      if (event.agent) metadataParts.push(event.agent.replace(/_/g, ' '))
      const dMs = event.data?.duration_ms
      if (dMs != null) {
        const dSec = Math.round(dMs / 1000)
        metadataParts.push(dSec < 60 ? `${dSec}s` : `${Math.floor(dSec / 60)}m ${dSec % 60}s`)
      }
      if (event.data?.error) {
        const err = String(event.data.error)
        metadataParts.push(err.length > 50 ? err.substring(0, 50) + '…' : err)
      }
      node = {
        id,
        type: nodeType,
        position: { x: 0, y: 0 },
        data: {
          label: event.event_type.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()),
          type: event.event_type,
          metadata: metadataParts.join(' • '),
          timestamp: event.timestamp,
          event,
          ...(!DEFAULT_SHOWN_NODE_TYPES.has(nodeType) && { defaultHidden: true }),
        },
        draggable: false,
      }
      if (parentId) node.parentId = parentId
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
      timestamp: selectedPipelineRun.started_at,
    },
    draggable: false,
  })

  // ── 3. Prelude events ─────────────────────────────────────────────────────
  processSection(prelude, 'pre')

  // ── 4. Cycles ─────────────────────────────────────────────────────────────
  cycles.forEach(cycle => {
    const cycleState = updatedCycles.get(cycle.id)
    const isCollapsed = cycleState?.isCollapsed ?? true

    if (cycle.type === 'review_cycle') {
      // ── Review cycle container ──────────────────────────────────────────
      const rcSummary = extractReviewCycleSummary(cycle)
      const rcNode = {
        id: cycle.id,
        type: 'subCycleContainer',
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
          summary: rcSummary,
        },
        // Collapsed: no style/measured — RF auto-sizes from content via ResizeObserver.
        // Expanded: style:0x0 + measured:1x1 so nodesInitialized isn't blocked while
        // children are still being measured.
        style: isCollapsed ? {} : { width: 0, height: 0 },
        ...(isCollapsed ? {} : { measured: { width: 1, height: 1 } }),
        draggable: false,
      }
      newNodes.push(rcNode)

      if (!isCollapsed) {
        // review_cycle_started → direct child
        if (cycle.startEvent) {
          newNodes.push(makeDecisionNode(cycle.startEvent, `${cycle.id}-start`, cycle.id))
        }

        // Residual events at cycle level (skip events claimed by agent exec containers)
        const rcClaimed = createAgentExecutionContainers(cycle.events ?? [], cycle.id, cycle.id)
        ;(cycle.events ?? []).forEach(event => {
          if (!rcClaimed.has(event)) processEventToNode(event, cycle.id, cycle.id)
        })

        // Iteration containers
        const lastIterIdx = cycle.iterations.length - 1
        cycle.iterations.forEach((iteration, iterIdx) => {
          const iterId = `${cycle.id}-iter-${iteration.number}`
          const iterState = existingCycles.get(iterId)
          const iterCollapsed = iterState?.isCollapsed ?? true
          updatedCycles.set(iterId, { isCollapsed: iterCollapsed })

          const isLastFailed = rcSummary.isFailure && iterIdx === lastIterIdx

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
              onToggleCollapse: null,
              startTime: iteration.startEvent?.timestamp,
              summary: extractReviewIterationSummary(iteration, cycle.startEvent, isLastFailed),
              isFailure: isLastFailed,
            },
            style: iterCollapsed ? {} : { width: 0, height: 0 },
            ...(iterCollapsed ? {} : { measured: { width: 1, height: 1 } }),
            draggable: false,
          })

          if (!iterCollapsed) {
            const iterEvents = [iteration.startEvent, ...iteration.events].filter(Boolean)
            const iterClaimed = createAgentExecutionContainers(iterEvents, iterId, iterId)
            iterEvents.forEach(event => {
              if (!iterClaimed.has(event)) processEventToNode(event, iterId, iterId)
            })
          }
        })

        // review_cycle_completed → direct child (skip if inferred)
        if (cycle.endEvent && !cycle.endEvent._inferred) {
          newNodes.push(makeDecisionNode(cycle.endEvent, `${cycle.id}-end`, cycle.id))
        }
      }
    } else if (cycle.type === 'repair_cycle') {
      // ── Repair cycle container ──────────────────────────────────────────
      const rpcNode = {
        id: cycle.id,
        type: 'subCycleContainer',
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

      if (!isCollapsed && cycle.testCycles) {
        // Repair-level residual events (exclude boundary markers)
        const REPAIR_BOUNDARY_TYPES = new Set([
          'repair_cycle_started', 'repair_cycle_completed', 'repair_cycle_container_completed',
        ])
        const repairResiduals = (cycle.events ?? [])
          .filter(e => e !== cycle.startEvent && e !== cycle.endEvent && !REPAIR_BOUNDARY_TYPES.has(e.event_type))
        const repairClaimed = createAgentExecutionContainers(repairResiduals, cycle.id, cycle.id)
        repairResiduals.forEach(event => {
          if (!repairClaimed.has(event)) processEventToNode(event, cycle.id, cycle.id)
        })

        // Test cycle containers + their sub-cycles + events
        cycle.testCycles.forEach(tc => {
          const tcId = `${cycle.id}-tc-${tc.number}`
          const tcState = existingCycles.get(tcId)
          const tcCollapsed = tcState?.isCollapsed ?? true
          updatedCycles.set(tcId, { isCollapsed: tcCollapsed })

          newNodes.push({
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
              onToggleCollapse: null,
              startTime: tc.startEvent?.timestamp,
              summary: extractTestCycleSummary(tc),
            },
            style: tcCollapsed ? {} : { width: 0, height: 0 },
            ...(tcCollapsed ? {} : { measured: { width: 1, height: 1 } }),
            draggable: false,
          })

          if (!tcCollapsed) {
            // Recursively create sub-cycle containers
            const createSubCycles = (sc, parentId) => {
              const scId = `${parentId}-${sc.cycleType}-${sc.number}`
              const scState = existingCycles.get(scId)
              const scCollapsed = scState?.isCollapsed ?? true
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
                  startTime: sc.startEvent?.timestamp,
                  isCollapsed: scCollapsed,
                  containsActiveAgent: activeContainerIds.has(scId),
                  onToggleCollapse: null,
                  summary: extractSubCycleSummary(sc),
                },
                style: scCollapsed ? {} : { width: 0, height: 0 },
                ...(scCollapsed ? {} : { measured: { width: 1, height: 1 } }),
                draggable: false,
              })

              if (!scCollapsed) {
                sc.subCycles?.forEach(nsc => createSubCycles(nsc, scId))
                const scEvents = [sc.startEvent, ...sc.events, sc.endEvent].filter(Boolean)
                const scClaimed = createAgentExecutionContainers(scEvents, scId, scId)
                scEvents.forEach(event => {
                  if (!scClaimed.has(event)) processEventToNode(event, scId, scId)
                })
              }
            }

            tc.subCycles.forEach(sc => createSubCycles(sc, tcId))
            const tcEvents = [tc.startEvent, ...(tc.events ?? []), tc.endEvent].filter(Boolean)
            const tcClaimed = createAgentExecutionContainers(tcEvents, tcId, tcId)
            tcEvents.forEach(event => {
              if (!tcClaimed.has(event)) processEventToNode(event, tcId, tcId)
            })
          }
        })
      }
    } else if (cycle.type === 'pr_review_cycle') {
      // ── PR review cycle container ───────────────────────────────────────
      const prNode = {
        id: cycle.id,
        type: 'subCycleContainer',
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

      if (!isCollapsed) {
        if (cycle.startEvent) {
          newNodes.push(makeDecisionNode(cycle.startEvent, `${cycle.id}-start`, cycle.id))
        }

        const prClaimed = createAgentExecutionContainers(cycle.events ?? [], cycle.id, cycle.id)
        ;(cycle.events ?? []).forEach(event => {
          if (!prClaimed.has(event)) processEventToNode(event, cycle.id, cycle.id)
        })

        ;(cycle.phases ?? []).forEach(phase => {
          const phaseId = `${cycle.id}-phase-${phase.number}`
          const phaseState = existingCycles.get(phaseId)
          const phaseCollapsed = phaseState?.isCollapsed ?? true
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
              onToggleCollapse: null,
              startTime: phase.startEvent?.timestamp,
              summary: extractPRReviewPhaseSummary(phase),
            },
            style: phaseCollapsed ? {} : { width: 0, height: 0 },
            ...(phaseCollapsed ? {} : { measured: { width: 1, height: 1 } }),
            draggable: false,
          })

          if (!phaseCollapsed) {
            const phaseEvents = [phase.startEvent, ...phase.events].filter(Boolean)
            const phaseClaimed = createAgentExecutionContainers(phaseEvents, phaseId, phaseId)
            phaseEvents.forEach(event => {
              if (!phaseClaimed.has(event)) processEventToNode(event, phaseId, phaseId)
            })
          }
        })

        if (cycle.endEvent && !cycle.endEvent._inferred) {
          newNodes.push(makeDecisionNode(cycle.endEvent, `${cycle.id}-end`, cycle.id))
        }
      }
    } else if (cycle.type === 'conversational_loop') {
      // ── Conversational loop container ───────────────────────────────────
      const clNode = {
        id: cycle.id,
        type: 'subCycleContainer',
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

      if (!isCollapsed) {
        if (cycle.startEvent) {
          newNodes.push(makeDecisionNode(cycle.startEvent, `${cycle.id}-start`, cycle.id))
        }
        const clClaimed = createAgentExecutionContainers(cycle.events ?? [], cycle.id, cycle.id)
        ;(cycle.events ?? []).forEach(event => {
          if (!clClaimed.has(event)) processEventToNode(event, cycle.id, cycle.id)
        })
        if (cycle.endEvent && !cycle.endEvent._inferred) {
          newNodes.push(makeDecisionNode(cycle.endEvent, `${cycle.id}-end`, cycle.id))
        }
      }
    } else if (cycle.type === 'status_progression') {
      // ── Status progression container ─────────────────────────────────────
      const spNode = {
        id: cycle.id,
        type: 'subCycleContainer',
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

      if (!isCollapsed) {
        if (cycle.startEvent) {
          newNodes.push(makeDecisionNode(cycle.startEvent, `${cycle.id}-start`, cycle.id))
        }
        const spClaimed = createAgentExecutionContainers(cycle.events ?? [], cycle.id, cycle.id)
        ;(cycle.events ?? []).forEach(event => {
          if (!spClaimed.has(event)) processEventToNode(event, cycle.id, cycle.id)
        })
        if (cycle.endEvent && !cycle.endEvent._inferred) {
          newNodes.push(makeDecisionNode(cycle.endEvent, `${cycle.id}-end`, cycle.id))
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

      if (!iterCollapsed) {
        const orphanEvents = [cycle.startEvent, ...cycle.events].filter(Boolean)
        const orphanClaimed = createAgentExecutionContainers(orphanEvents, cycle.id, cycle.id)
        orphanEvents.forEach(event => {
          if (!orphanClaimed.has(event)) processEventToNode(event, cycle.id, cycle.id)
        })
      }

    } else if (cycle.type === 'agent_execution') {
      // ── Root-level agent execution container (outside any other cycle) ───
      const boundary = agentExecutionBoundaries.find(b => b.startEvent === cycle.startEvent)
      if (!boundary) return

      const agent = cycle.startEvent?.agent ?? 'agent'
      const taskId = cycle.startEvent?.task_id
      const containerId = cycle.id
      const isActive = activeTaskIds.has(taskId)

      const childEvents = [cycle.startEvent, ...(cycle.events ?? []), cycle.endEvent].filter(Boolean)
      const childCount = childEvents.filter(e => !SKIP_EVENT_TYPES.has(e.event_type)).length

      const containerNode = {
        id: containerId,
        type: 'subCycleContainer',
        position: { x: 0, y: 0 },
        data: {
          cycleId: containerId,
          cycleType: 'agent_execution',
          label: agent.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()),
          iterationCount: childCount,
          isCollapsed,
          containsActiveAgent: activeContainerIds.has(containerId) || isActive,
          isActive,
          onToggleCollapse: null,
          startTime: cycle.startEvent?.timestamp,
          endTime: cycle.endEvent?.timestamp ?? null,
          summary: extractAgentExecutionSummary(boundary, claudeSummaries),
          event: cycle.startEvent,
        },
        style: isCollapsed ? {} : { width: 0, height: 0 },
        ...(isCollapsed ? {} : { measured: { width: 1, height: 1 } }),
        draggable: false,
      }
      newNodes.push(containerNode)

      if (!isCollapsed) {
        childEvents
          .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))
          .forEach(event => processEventToNode(event, containerId, containerId))
      }
    }
  })

  // ── 5. Postlude events ────────────────────────────────────────────────────
  processSection(postlude, 'post')

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
        timestamp: selectedPipelineRun.ended_at,
      },
      draggable: false,
    })
  }

  // ── 7. Build edges: sequence leaves + collapsed containers chronologically ──
  const CONTAINER_TYPES = new Set([
    'iterationContainer', 'subCycleContainer',
  ])

  // Robust timestamp extraction — nodes use different field names
  const getTs = n => n.data?.timestamp ?? n.data?.startTime ?? n.data?.event?.timestamp ?? 0

  // The edge sequence includes:
  //   - All leaf (non-container) nodes
  //   - Collapsed containers (they stand in for their hidden children)
  // Expanded containers are excluded — their children participate directly.
  const sequenceNodes = newNodes
    .filter(n => {
      if (!CONTAINER_TYPES.has(n.type)) return true          // leaf node
      if (n.data?.isCollapsed) return true                   // collapsed container
      return false                                           // expanded container (children are in sequence)
    })
    .sort((a, b) => new Date(getTs(a)) - new Date(getTs(b)))

  // Build sequential edges, deduplicating (collapsed containers may appear
  // adjacent to each other or to their own parent chain — skip self-edges).
  const seenEdges = new Set()
  for (let i = 1; i < sequenceNodes.length; i++) {
    const source = sequenceNodes[i - 1].id
    const target = sequenceNodes[i].id
    if (source === target) continue
    const key = `${source}->${target}`
    if (seenEdges.has(key)) continue
    seenEdges.add(key)
    newEdges.push({
      id: `edge-${source}-${target}`,
      source,
      sourceHandle: 'bottom',
      target,
      targetHandle: 'top',
      type: 'smart',
      markerEnd: { type: MarkerType.ArrowClosed, color: '#6e7681' },
      style: { stroke: '#6e7681' },
    })
  }

  const _bfTEnd = performance.now()
  console.log(
    `[PerfGraph] buildFlowchart: ${(_bfTEnd - _bfT0).toFixed(1)}ms` +
    ` | nodes:${newNodes.length} edges:${newEdges.length} sequence:${sequenceNodes.length}` +
    ` | processEvents:${(_bfT1 - _bfT0).toFixed(1)}ms` +
    ` | nodeBuild:${(_bfTEnd - _bfT1).toFixed(1)}ms`
  )

  return { nodes: newNodes, edges: newEdges, updatedCycles, model }
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
      const iterEvents = [cycle.startEvent, ...(cycle.events ?? [])].filter(Boolean)
      if (hasActive(iterEvents)) {
        result.add(cycle.id)
      }
    } else if (cycle.type === 'agent_execution') {
      const aeEvents = [cycle.startEvent, ...(cycle.events ?? []), cycle.endEvent].filter(Boolean)
      if (hasActive(aeEvents)) {
        result.add(cycle.id)
      }
    }
  })

  return result
}
