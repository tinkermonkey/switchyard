import { MarkerType } from '@xyflow/react'
import { processEvents } from './eventProcessing/index.js'
import { getNodeType, HIDDEN_BY_DEFAULT_TYPES } from '../components/nodes/EVENT_TYPE_MAP.js'

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

  const _bfT0 = performance.now()

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
          const iterState = existingCycles.get(iterId)
          const iterCollapsed = iterState?.isCollapsed ?? true  // default: collapsed
          updatedCycles.set(iterId, { isCollapsed: iterCollapsed })

          const iterNode = {
            id: iterId,
            type: 'iterationContainer',
            parentId: cycle.id,
            position: { x: 0, y: 0 },
            zIndex: 1,
            data: {
              cycleId: iterId,
              cycleType: 'review',
              iterationNumber: iteration.number,
              label: `Iteration ${iteration.number}`,
              iterationCount: iteration.events.length + 1,
              isCollapsed: iterCollapsed,
              onToggleCollapse: null, // injected by caller
            },
            style: iterCollapsed ? {} : { width: 0, height: 0 },
            ...(iterCollapsed ? {} : { measured: { width: 1, height: 1 } }),
            draggable: false,
          }
          newNodes.push(iterNode)

          if (iterCollapsed) {
            leafChain.push({ id: iterId, timestamp: iteration.startEvent?.timestamp })
          } else {
            // Grandchildren: startEvent then the other events, chronologically
            const allIterEvents = [iteration.startEvent, ...iteration.events]
              .filter(Boolean)
              .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))

            allIterEvents.forEach(event => {
              processEventToNode(event, iterId, iterId)
            })
          }
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
              cycleType: 'repair',
              iterationNumber: tc.number,
              label: tc.testType,
              iterationCount: tc.subCycles.length,
              isCollapsed: tcCollapsed,
              onToggleCollapse: null, // injected by caller
            },
            style: tcCollapsed ? {} : { width: 0, height: 0 },
            ...(tcCollapsed ? {} : { measured: { width: 1, height: 1 } }),
            draggable: false,
          }
          newNodes.push(tcNode)

          if (tcCollapsed) {
            allRepairEntries.push({ containerId: tcId, timestamp: tc.startEvent?.timestamp ?? 0 })
          } else {
            // Create sub-cycle containers and collect their events
            tc.subCycles.forEach(sc => {
              const scId = `${tcId}-${sc.cycleType}-${sc.number}`
              const scState = existingCycles.get(scId)
              const scCollapsed = scState?.isCollapsed ?? true  // default: collapsed
              updatedCycles.set(scId, { isCollapsed: scCollapsed })

              newNodes.push({
                id: scId,
                type: 'subCycleContainer',
                parentId: tcId,
                position: { x: 0, y: 0 },
                zIndex: 1,
                data: {
                  cycleId: scId,
                  label: sc.label,
                  cycleType: sc.cycleType,
                  iterationNumber: sc.number,
                  iterationCount: sc.events.length,
                  startEvent: sc.startEvent,
                  endEvent: sc.endEvent,
                  isCollapsed: scCollapsed,
                  onToggleCollapse: null, // injected by caller
                },
                style: scCollapsed ? {} : { width: 0, height: 0 },
                ...(scCollapsed ? {} : { measured: { width: 1, height: 1 } }),
                draggable: false,
              })

              if (scCollapsed) {
                allRepairEntries.push({ containerId: scId, timestamp: sc.startEvent?.timestamp ?? 0 })
              } else {
                ;[sc.startEvent, ...sc.events, sc.endEvent].filter(Boolean).forEach(event => {
                  allRepairEntries.push({ event, parentId: scId, timestamp: event.timestamp })
                })
              }
            })

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
          label: 'PR Review',
          iterationCount: cycle.phases?.length ?? 0,
          isCollapsed,
          onToggleCollapse: null, // injected by caller
          startTime: cycle.startEvent?.timestamp,
          endTime: cycle.endEvent?.timestamp,
        },
        style: isCollapsed ? {} : { width: 0, height: 0 },
        ...(isCollapsed ? {} : { measured: { width: 1, height: 1 } }),
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
          const phaseState = existingCycles.get(phaseId)
          const phaseCollapsed = phaseState?.isCollapsed ?? true  // default: collapsed
          updatedCycles.set(phaseId, { isCollapsed: phaseCollapsed })

          const phaseNode = {
            id: phaseId,
            type: 'iterationContainer',
            parentId: cycle.id,
            position: { x: 0, y: 0 },
            zIndex: 1,
            data: {
              cycleId: phaseId,
              cycleType: 'pr_review',
              iterationNumber: phase.number,
              label: `Phase ${phase.number}`,
              iterationCount: phase.events.length + 1,
              isCollapsed: phaseCollapsed,
              onToggleCollapse: null, // injected by caller
            },
            style: phaseCollapsed ? {} : { width: 0, height: 0 },
            ...(phaseCollapsed ? {} : { measured: { width: 1, height: 1 } }),
            draggable: false,
          }
          newNodes.push(phaseNode)

          if (phaseCollapsed) {
            leafChain.push({ id: phaseId, timestamp: phase.startEvent?.timestamp })
          } else {
            const allPhaseEvents = [phase.startEvent, ...phase.events]
              .filter(Boolean)
              .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))

            allPhaseEvents.forEach(event => {
              processEventToNode(event, phaseId, phaseId)
            })
          }
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
 * @param {Object} model            - Processed event model (from processEvents)
 * @param {Set}    activeAgentNames - Set of agent name strings currently running
 * @returns {Set<string>} Container IDs that should be auto-expanded
 */
export function findActiveContainerPath(model, activeAgentNames) {
  if (!activeAgentNames.size) return new Set()
  const result = new Set()

  const hasActive = (events) =>
    events.some(e => e.event_type === 'agent_initialized' && activeAgentNames.has(e.agent))

  model.cycles.forEach(cycle => {
    if (cycle.type === 'review_cycle') {
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
        tc.subCycles?.forEach(sc => {
          const scId = `${tcId}-${sc.cycleType}-${sc.number}`
          const scEvents = [sc.startEvent, ...sc.events, sc.endEvent].filter(Boolean)
          if (hasActive(scEvents)) {
            result.add(cycle.id)
            result.add(tcId)
            result.add(scId)
          }
        })
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
    }
  })

  return result
}
