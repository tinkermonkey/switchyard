/**
 * Event processing pipeline.
 * Transforms raw pipeline run events into a structured PipelineGraph model.
 * Contains no layout information — pure data modeling.
 *
 * processEvents(events, workflowConfig) → PipelineGraph:
 * {
 *   prelude:  { events: [] },   // events strictly before first cycle's startTime
 *   cycles:   [...],            // review_cycle and repair_cycle entries
 *   postlude: { events: [] },   // events after last cycle's endTime
 *   agentExecutions: Map,       // agent → [execution]
 * }
 */

/**
 * Build map of agent → [execution instances] from lifecycle events.
 * Executions are stored in the order they started.
 */
function buildAgentExecutionMap(sortedEvents) {
  const map = new Map()

  sortedEvents.forEach(event => {
    if (event.event_category !== 'agent_lifecycle') return
    const { agent, task_id: taskId } = event

    if (event.event_type === 'agent_initialized') {
      if (!map.has(agent)) map.set(agent, [])
      map.get(agent).push({
        taskId,
        startTime: event.timestamp,
        startEvent: event,
        endTime: null,
        endEvent: null,
        status: 'running',
      })
    } else if (['agent_completed', 'agent_failed'].includes(event.event_type)) {
      const executions = map.get(agent) || []
      const execution = executions.find(e => e.taskId === taskId)
      if (execution) {
        execution.endTime = event.timestamp
        execution.endEvent = event
        execution.status = event.event_type === 'agent_completed' ? 'completed' : 'failed'
      }
    }
  })

  return map
}

/**
 * Find review cycle boundaries from decision events.
 * Returns [{startEvent, endEvent}] — one entry per review_cycle_started/completed pair.
 * An in-progress cycle (no matching completed event) will have endEvent = null.
 */
function findReviewCycles(sortedEvents) {
  const starts = sortedEvents.filter(
    e => e.event_category === 'decision' && e.event_type === 'review_cycle_started'
  )
  const ends = sortedEvents.filter(
    e => e.event_category === 'decision' && e.event_type === 'review_cycle_completed'
  )

  return starts.map((start, idx) => ({
    startEvent: start,
    endEvent: ends[idx] || null,
  }))
}

/**
 * Find repair cycle boundary.
 * Repair cycles are bounded by agent_initialized / agent_completed events for agent='repair_cycle'.
 * Returns {startEvent, endEvent} or null if no repair cycle found.
 */
function findRepairCycle(sortedEvents) {
  const startEvent = sortedEvents.find(
    e =>
      e.event_category === 'agent_lifecycle' &&
      e.event_type === 'agent_initialized' &&
      e.agent === 'repair_cycle'
  )
  if (!startEvent) return null

  const startMs = new Date(startEvent.timestamp).getTime()
  const endEvent = sortedEvents.find(
    e =>
      e.event_category === 'agent_lifecycle' &&
      e.event_type === 'agent_completed' &&
      e.agent === 'repair_cycle' &&
      new Date(e.timestamp).getTime() > startMs
  )

  return { startEvent, endEvent }
}

/**
 * Group events within a review cycle into iterations.
 * Each iteration starts at a review_cycle_iteration event and ends just before
 * the next review_cycle_iteration (or at review_cycle_completed).
 *
 * @param {Array} cycleEvents  - All events within the cycle's time range (sorted)
 * @param {Object} boundary    - { startEvent, endEvent }
 * @param {Map} agentExecutions
 * @returns {Array} iterations
 */
function groupReviewCycleIterations(cycleEvents, boundary, agentExecutions) {
  const { endEvent } = boundary

  const iterationMarkers = cycleEvents.filter(e => e.event_type === 'review_cycle_iteration')

  return iterationMarkers.map((iterEvent, idx) => {
    const iterStartMs = new Date(iterEvent.timestamp).getTime()
    const nextMarker = iterationMarkers[idx + 1]
    const iterEndMs = nextMarker
      ? new Date(nextMarker.timestamp).getTime()
      : endEvent
      ? new Date(endEvent.timestamp).getTime()
      : Infinity

    // Events between iterStart (exclusive of the marker itself) and iterEnd (exclusive)
    const events = cycleEvents.filter(e => {
      const t = new Date(e.timestamp).getTime()
      return t > iterStartMs && t < iterEndMs
    })

    // Agent executions that started within this iteration's window
    const iterAgentExecs = []
    agentExecutions.forEach((executions, agent) => {
      executions.forEach((exec, executionIndex) => {
        const execMs = new Date(exec.startTime).getTime()
        if (execMs >= iterStartMs && execMs < iterEndMs) {
          iterAgentExecs.push({ agent, execution: exec, executionIndex })
        }
      })
    })

    return {
      number: idx + 1,
      startEvent: iterEvent,
      events,
      agentExecutions: iterAgentExecs,
    }
  })
}

// Registry of sub-cycle types recognised within a test cycle.
// Each entry drives automatic boundary detection via _started/_completed event pairs.
const REPAIR_SUB_CYCLE_REGISTRY = [
  {
    startEventType: 'repair_cycle_test_execution_started',
    endEventType:   'repair_cycle_test_execution_completed',
    cycleType:      'test_execution',
    label:          'Test Execution',
  },
  {
    startEventType: 'repair_cycle_fix_cycle_started',
    endEventType:   'repair_cycle_fix_cycle_completed',
    cycleType:      'fix_cycle',
    label:          'Fix Cycle',
  },
]

/**
 * Group events within a time window into sub-cycles using a registry entry.
 */
function groupSubCycles(events, registryEntry, agentExecutions) {
  const { startEventType, endEventType, cycleType, label } = registryEntry
  const starts = events.filter(e => e.event_type === startEventType)
  const ends   = events.filter(e => e.event_type === endEventType)

  return starts.map((startEvent, idx) => {
    const endEvent  = ends[idx] || null
    const startMs   = new Date(startEvent.timestamp).getTime()
    const endMs     = endEvent ? new Date(endEvent.timestamp).getTime() : Infinity
    const subEvents = events.filter(e => {
      const t = new Date(e.timestamp).getTime()
      return t > startMs && t < endMs
    })
    const subAgentExecs = []
    agentExecutions.forEach((executions, agent) => {
      executions.forEach((exec, executionIndex) => {
        const execMs = new Date(exec.startTime).getTime()
        if (execMs >= startMs && execMs <= endMs)
          subAgentExecs.push({ agent, execution: exec, executionIndex })
      })
    })
    return {
      number: idx + 1,
      cycleType,
      label: `${label} ${idx + 1}`,
      startEvent,
      endEvent,
      events: subEvents,
      agentExecutions: subAgentExecs,
    }
  })
}

const TEST_TYPE_LABELS = {
  unit: 'Unit Tests',
  integration: 'Integration Tests',
  e2e: 'E2E Tests',
  ci: 'CI Tests',
  compilation: 'Compilation',
  'pre-commit': 'Pre-commit',
  storybook: 'Storybook',
}

function formatTestType(testType) {
  return TEST_TYPE_LABELS[testType] ||
    testType.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())
}

/**
 * Group events within a repair cycle into test cycles.
 * Each test cycle is bounded by repair_cycle_test_cycle_started / repair_cycle_test_cycle_completed.
 * Each test cycle is further split into sub-cycles using REPAIR_SUB_CYCLE_REGISTRY.
 *
 * @param {Array} cycleEvents  - All events within the repair cycle's time range (sorted)
 * @param {Object} boundary    - { startEvent, endEvent }
 * @param {Map} agentExecutions
 * @returns {Array} testCycles
 */
function groupRepairTestCycles(cycleEvents, boundary, agentExecutions) {
  const tcStarts = cycleEvents.filter(e => e.event_type === 'repair_cycle_test_cycle_started')
  const tcEnds   = cycleEvents.filter(e => e.event_type === 'repair_cycle_test_cycle_completed')

  return tcStarts.map((tcStart, idx) => {
    const tcEnd     = tcEnds[idx] || null
    const tcStartMs = new Date(tcStart.timestamp).getTime()
    const tcEndMs   = tcEnd ? new Date(tcEnd.timestamp).getTime() : Infinity

    // All events strictly between the test cycle markers
    const events = cycleEvents.filter(e => {
      const t = new Date(e.timestamp).getTime()
      return t > tcStartMs && t < tcEndMs
    })

    // Detect sub-cycles generically from registry
    const subCycles = REPAIR_SUB_CYCLE_REGISTRY.flatMap(entry =>
      groupSubCycles(events, entry, agentExecutions)
    )

    // Residual events: not inside any sub-cycle boundary
    const subCycleTimeRanges = subCycles.map(sc => ({
      startMs: new Date(sc.startEvent.timestamp).getTime(),
      endMs:   sc.endEvent ? new Date(sc.endEvent.timestamp).getTime() : Infinity,
    }))
    const residualEvents = events.filter(e => {
      const t = new Date(e.timestamp).getTime()
      return !subCycleTimeRanges.some(r => t >= r.startMs && t <= r.endMs)
    })

    // Agent executions within this test cycle's window
    const tcAgentExecs = []
    agentExecutions.forEach((executions, agent) => {
      executions.forEach((exec, executionIndex) => {
        const execMs = new Date(exec.startTime).getTime()
        if (execMs >= tcStartMs && execMs <= tcEndMs) {
          tcAgentExecs.push({ agent, execution: exec, executionIndex })
        }
      })
    })

    const rawTestType = tcStart.inputs?.test_type ?? tcStart.test_type
    return {
      number: idx + 1,
      testType: rawTestType ? formatTestType(rawTestType) : `Test Cycle ${idx + 1}`,
      startEvent: tcStart,
      endEvent:   tcEnd,
      subCycles,
      events: residualEvents,
      agentExecutions: tcAgentExecs,
    }
  })
}

/**
 * Process all pipeline run events into a structured PipelineGraph model.
 *
 * @param {Array} events          - All pipeline run events (unsorted)
 * @param {Object} workflowConfig - Workflow configuration (currently unused, reserved)
 * @returns {Object} PipelineGraph
 */
export function processEvents(events, workflowConfig = null) {
  if (!events || events.length === 0) {
    return {
      prelude: { events: [] },
      cycles: [],
      postlude: { events: [] },
      agentExecutions: new Map(),
    }
  }

  // Sort events chronologically
  const sorted = [...events].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))

  // Build agent execution map (needed for iteration grouping)
  const agentExecutions = buildAgentExecutionMap(sorted)

  // Detect cycle boundaries
  const reviewCycleBoundaries = findReviewCycles(sorted)
  const repairCycleBoundary = findRepairCycle(sorted)

  // Compute the earliest/latest cycle timestamps for prelude/postlude splitting
  let firstCycleStartMs = Infinity
  let lastCycleEndMs = -Infinity

  reviewCycleBoundaries.forEach(({ startEvent, endEvent }) => {
    const sMs = new Date(startEvent.timestamp).getTime()
    const eMs = endEvent ? new Date(endEvent.timestamp).getTime() : sMs
    firstCycleStartMs = Math.min(firstCycleStartMs, sMs)
    lastCycleEndMs = Math.max(lastCycleEndMs, eMs)
  })

  if (repairCycleBoundary) {
    const sMs = new Date(repairCycleBoundary.startEvent.timestamp).getTime()
    const eMs = repairCycleBoundary.endEvent
      ? new Date(repairCycleBoundary.endEvent.timestamp).getTime()
      : sMs
    firstCycleStartMs = Math.min(firstCycleStartMs, sMs)
    lastCycleEndMs = Math.max(lastCycleEndMs, eMs)
  }

  const noCycles = firstCycleStartMs === Infinity

  // Prelude: events strictly before the first cycle's startTime
  const preludeEvents = noCycles
    ? sorted
    : sorted.filter(e => new Date(e.timestamp).getTime() < firstCycleStartMs)

  // Postlude: events strictly after the last cycle's endTime
  const postludeEvents = noCycles
    ? []
    : sorted.filter(e => new Date(e.timestamp).getTime() > lastCycleEndMs)

  // Build cycles
  const cycles = []

  reviewCycleBoundaries.forEach((boundary, idx) => {
    const { startEvent, endEvent } = boundary
    const cycleStartMs = new Date(startEvent.timestamp).getTime()
    const cycleEndMs = endEvent ? new Date(endEvent.timestamp).getTime() : Date.now()

    const cycleEvents = sorted.filter(e => {
      const t = new Date(e.timestamp).getTime()
      return t >= cycleStartMs && t <= cycleEndMs
    })

    const iterations = groupReviewCycleIterations(cycleEvents, boundary, agentExecutions)

    cycles.push({
      id: `review_cycle_${idx + 1}`,
      type: 'review_cycle',
      startEvent,
      endEvent,
      iterations,
      isCollapsed: false,
    })
  })

  if (repairCycleBoundary) {
    const { startEvent, endEvent } = repairCycleBoundary
    const cycleStartMs = new Date(startEvent.timestamp).getTime()
    const cycleEndMs = endEvent ? new Date(endEvent.timestamp).getTime() : Date.now()

    const cycleEvents = sorted.filter(e => {
      const t = new Date(e.timestamp).getTime()
      return t >= cycleStartMs && t <= cycleEndMs
    })

    const testCycles = groupRepairTestCycles(cycleEvents, repairCycleBoundary, agentExecutions)

    cycles.push({
      id: 'repair_cycle_1',
      type: 'repair_cycle',
      startEvent,
      endEvent,
      testCycles,
      isCollapsed: false,
    })
  }

  return {
    prelude: { events: preludeEvents },
    cycles,
    postlude: { events: postludeEvents },
    agentExecutions,
  }
}
