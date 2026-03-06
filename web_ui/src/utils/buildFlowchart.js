import { MarkerType } from '@xyflow/react'
import { processEvents } from './eventProcessing/index.js'
import { getNodeType } from '../components/nodes/EVENT_TYPE_MAP.js'

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
 * @param {Array}  params.events              - Pipeline run events
 * @param {Map}    params.existingCycles       - Existing cycles map (preserves collapse state)
 * @param {Object} params.workflowConfig       - Workflow configuration (passed to processEvents)
 * @param {Object} params.selectedPipelineRun  - Pipeline run metadata
 * @param {Set}    params.activeAgentNames     - Currently-active agent names
 * @returns {{ nodes, edges, agentExecutions, updatedCycles }}
 */
export function buildFlowchart({
  events,
  existingCycles = new Map(),
  workflowConfig = null,
  selectedPipelineRun,
  activeAgentNames = new Set(),
}) {
  if (!events.length || !selectedPipelineRun) {
    return { nodes: [], edges: [], agentExecutions: new Map(), updatedCycles: new Map() }
  }

  // ── 1. Process events into structured model ──────────────────────────────
  const model = processEvents(events, workflowConfig)
  const { prelude, cycles, postlude, agentExecutions } = model

  // Merge collapse state from existing cycles (preserve user's open/closed choices)
  const updatedCycles = new Map()
  cycles.forEach(cycle => {
    const existing = existingCycles.get(cycle.id)
    updatedCycles.set(cycle.id, {
      ...cycle,
      isCollapsed: existing?.isCollapsed ?? false,
    })
  })

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

    const node = {
      id,
      type: getNodeType(event.event_type),
      position: { x: 0, y: 0 },
      data: {
        label: event.event_type
          ? event.event_type.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())
          : 'Unknown Event',
        type: 'decision_event',
        metadata: metadataParts.join(' • '),
        decision_category: event.decision_category,
        timestamp: event.timestamp,
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
    const isActive = activeAgentNames.has(agent)

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

    if (event.event_category === 'decision') {
      const id = `${idPrefix}-dec-${event.timestamp}`
      node = makeDecisionNode(event, id, parentId)
    } else if (
      event.event_category === 'agent_lifecycle' &&
      event.event_type === 'agent_initialized'
    ) {
      const id = `${idPrefix}-agent-${event.agent}-${event.task_id}`
      node = makeAgentNode(event, id, parentId)
    }
    // agent_completed / agent_failed — skip (status captured on agent node)

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
          label: 'Review Cycle',
          iterationCount: cycle.iterations.length,
          isCollapsed,
          onToggleCollapse: null, // injected by caller
          startTime: cycle.startEvent?.timestamp,
          endTime: cycle.endEvent?.timestamp,
        },
        style: { width: 0, height: 0 }, // sized by applyCycleLayout
        // Non-zero measured prevents RF's updateNodeInternals (which requires width>0) from
        // blocking nodesInitialized. Phase 2 overwrites with correct style dimensions.
        measured: { width: 1, height: 1 },
        draggable: false,
      }
      newNodes.push(rcNode)

      if (isCollapsed) {
        // Collapsed: the container is the only leaf in the chain
        leafChain.push({ id: cycle.id, timestamp: cycle.startEvent?.timestamp })
      } else {
        // Expanded: add direct children + iterations + grandchildren

        // review_cycle_started → direct child, leftmost
        if (cycle.startEvent) {
          const startId = `${cycle.id}-start`
          const startNode = makeDecisionNode(cycle.startEvent, startId, cycle.id)
          newNodes.push(startNode)
          leafChain.push({ id: startId, timestamp: cycle.startEvent.timestamp })
        }

        // Iterations
        cycle.iterations.forEach(iteration => {
          const iterId = `${cycle.id}-iter-${iteration.number}`

          const iterNode = {
            id: iterId,
            type: 'iterationContainer',
            parentId: cycle.id,
            position: { x: 0, y: 0 },
            zIndex: 1,
            data: {
              iterationNumber: iteration.number,
              label: `Iteration ${iteration.number}`,
              eventCount: iteration.events.length + 1,
            },
            style: { width: 0, height: 0 }, // sized by applyCycleLayout
            measured: { width: 1, height: 1 },
            draggable: false,
          }
          newNodes.push(iterNode)

          // Grandchildren: startEvent then the other events, chronologically
          const allIterEvents = [iteration.startEvent, ...iteration.events]
            .filter(Boolean)
            .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))

          allIterEvents.forEach(event => {
            processEventToNode(event, iterId, iterId)
          })
        })

        // review_cycle_completed → direct child, rightmost (after iterations)
        // Skip if inferred — synthetic bounds are not shown as nodes
        if (cycle.endEvent && !cycle.endEvent._inferred) {
          const endId = `${cycle.id}-end`
          const endNode = makeDecisionNode(cycle.endEvent, endId, cycle.id)
          newNodes.push(endNode)
          leafChain.push({ id: endId, timestamp: cycle.endEvent.timestamp })
        }
      }
    } else if (cycle.type === 'repair_cycle') {
      // ── Repair cycle container ──────────────────────────────────────────
      const rpcNode = {
        id: cycle.id,
        type: 'repairCycleContainer',
        position: { x: 0, y: 0 },
        data: {
          cycleId: cycle.id,
          label: 'Repair Cycle',
          iterationCount: cycle.testCycles?.length ?? 0,
          isCollapsed,
          onToggleCollapse: null, // injected by caller
          startTime: cycle.startEvent?.timestamp,
          endTime: cycle.endEvent?.timestamp,
        },
        style: { width: 0, height: 0 }, // sized by applyCycleLayout
        measured: { width: 1, height: 1 },
        draggable: false,
      }
      newNodes.push(rpcNode)

      if (isCollapsed) {
        leafChain.push({ id: cycle.id, timestamp: cycle.startEvent?.timestamp })
      } else if (cycle.testCycles) {
        // Collect ALL entries (repair-level residuals + tc-level events) for one
        // chronological sort so leafChain sequencing is correct across the whole cycle.
        const allRepairEntries = []

        // Repair-level residual events → direct children of the repair cycle container
        ;(cycle.events ?? []).forEach(event => {
          allRepairEntries.push({ event, parentId: cycle.id })
        })

        // Test cycle containers + their sub-cycles + events
        cycle.testCycles.forEach(tc => {
          const tcId = `${cycle.id}-tc-${tc.number}`

          const tcNode = {
            id: tcId,
            type: 'iterationContainer',
            parentId: cycle.id,
            position: { x: 0, y: 0 },
            zIndex: 1,
            data: {
              iterationNumber: tc.number,
              label: tc.testType,
            },
            style: { width: 0, height: 0 }, // sized by applyCycleLayout
            measured: { width: 1, height: 1 },
            draggable: false,
          }
          newNodes.push(tcNode)

          // Create sub-cycle containers and collect their events
          tc.subCycles.forEach(sc => {
            const scId = `${tcId}-${sc.cycleType}-${sc.number}`
            newNodes.push({
              id: scId,
              type: 'subCycleContainer',
              parentId: tcId,
              position: { x: 0, y: 0 },
              zIndex: 1,
              data: {
                label: sc.label,
                cycleType: sc.cycleType,
                iterationNumber: sc.number,
                startEvent: sc.startEvent,
                endEvent: sc.endEvent,
              },
              style: { width: 0, height: 0 },
              measured: { width: 1, height: 1 },
              draggable: false,
            })
            ;[sc.startEvent, ...sc.events, sc.endEvent].filter(Boolean).forEach(event => {
              allRepairEntries.push({ event, parentId: scId })
            })
          })

          // Residual events within the test cycle go directly under the tc container
          ;[tc.startEvent, ...tc.events, tc.endEvent].filter(Boolean).forEach(event => {
            allRepairEntries.push({ event, parentId: tcId })
          })
        })

        // Process all events in chronological order for correct leafChain sequencing
        allRepairEntries
          .sort((a, b) => new Date(a.event.timestamp) - new Date(b.event.timestamp))
          .forEach(({ event, parentId }) => processEventToNode(event, parentId, parentId))
      }
    } else if (cycle.type === 'pr_review_cycle') {
      // ── PR review cycle container ───────────────────────────────────────
      const prNode = {
        id: cycle.id,
        type: 'prReviewCycleContainer',
        position: { x: 0, y: 0 },
        data: {
          cycleId: cycle.id,
          label: 'PR Review',
          iterationCount: cycle.phases?.length ?? 0,
          isCollapsed,
          onToggleCollapse: null, // injected by caller
          startTime: cycle.startEvent?.timestamp,
          endTime: cycle.endEvent?.timestamp,
        },
        style: { width: 0, height: 0 }, // sized by applyCycleLayout
        measured: { width: 1, height: 1 },
        draggable: false,
      }
      newNodes.push(prNode)

      if (isCollapsed) {
        leafChain.push({ id: cycle.id, timestamp: cycle.startEvent?.timestamp })
      } else {
        // pr_review_stage_started → direct child, leftmost
        if (cycle.startEvent) {
          const startId = `${cycle.id}-start`
          const startNode = makeDecisionNode(cycle.startEvent, startId, cycle.id)
          newNodes.push(startNode)
          leafChain.push({ id: startId, timestamp: cycle.startEvent.timestamp })
        }

        // Phase containers
        ;(cycle.phases ?? []).forEach(phase => {
          const phaseId = `${cycle.id}-phase-${phase.number}`

          const phaseNode = {
            id: phaseId,
            type: 'iterationContainer',
            parentId: cycle.id,
            position: { x: 0, y: 0 },
            zIndex: 1,
            data: {
              iterationNumber: phase.number,
              label: `Phase ${phase.number}`,
              eventCount: phase.events.length + 1,
            },
            style: { width: 0, height: 0 }, // sized by applyCycleLayout
            measured: { width: 1, height: 1 },
            draggable: false,
          }
          newNodes.push(phaseNode)

          const allPhaseEvents = [phase.startEvent, ...phase.events]
            .filter(Boolean)
            .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))

          allPhaseEvents.forEach(event => {
            processEventToNode(event, phaseId, phaseId)
          })
        })

        // pr_review_stage_completed → direct child, rightmost
        if (cycle.endEvent && !cycle.endEvent._inferred) {
          const endId = `${cycle.id}-end`
          const endNode = makeDecisionNode(cycle.endEvent, endId, cycle.id)
          newNodes.push(endNode)
          leafChain.push({ id: endId, timestamp: cycle.endEvent.timestamp })
        }
      }
    } else if (cycle.type === 'conversational_loop') {
      // ── Conversational loop container ───────────────────────────────────
      const clNode = {
        id: cycle.id,
        type: 'conversationalLoopContainer',
        position: { x: 0, y: 0 },
        data: {
          cycleId: cycle.id,
          label: 'Conversation',
          iterationCount: cycle.events?.length ?? 0,
          isCollapsed,
          onToggleCollapse: null, // injected by caller
          startTime: cycle.startEvent?.timestamp,
          endTime: cycle.endEvent?.timestamp,
        },
        style: { width: 0, height: 0 }, // sized by applyCycleLayout
        measured: { width: 1, height: 1 },
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

  return { nodes: newNodes, edges: newEdges, agentExecutions, updatedCycles, model }
}
