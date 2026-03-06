/**
 * Event processing pipeline.
 * Transforms raw pipeline run events into a structured PipelineGraph model.
 * Contains no layout information — pure data modeling.
 *
 * processEvents(events, workflowConfig) → PipelineGraph:
 * {
 *   prelude:  { events: [] },   // events strictly before first cycle's startTime
 *   cycles:   [...],            // review_cycle, repair_cycle, pr_review_cycle, conversational_loop entries
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
 * Find PR review cycle boundaries from decision events.
 * Returns [{startEvent, endEvent}] — one entry per pr_review_stage_started/completed pair.
 */
function findPRReviewCycles(sortedEvents) {
  const starts = sortedEvents.filter(
    e => e.event_category === 'decision' && e.event_type === 'pr_review_stage_started'
  )
  const ends = sortedEvents.filter(
    e => e.event_category === 'decision' && e.event_type === 'pr_review_stage_completed'
  )
  return starts.map((start, idx) => ({
    startEvent: start,
    endEvent: ends[idx] || null,
  }))
}

/**
 * Group events within a PR review cycle into phases.
 * Each phase starts at a pr_review_phase_started event and ends just before the next
 * phase start (or at the cycle's end event).
 */
function groupPRReviewPhases(cycleEvents, boundary) {
  const { endEvent } = boundary
  const phaseMarkers = cycleEvents.filter(e => e.event_type === 'pr_review_phase_started')

  return phaseMarkers.map((phaseEvent, idx) => {
    const phaseStartMs = new Date(phaseEvent.timestamp).getTime()
    const nextMarker = phaseMarkers[idx + 1]
    const phaseEndMs = nextMarker
      ? new Date(nextMarker.timestamp).getTime()
      : endEvent
      ? new Date(endEvent.timestamp).getTime()
      : Infinity

    const events = cycleEvents.filter(e => {
      const t = new Date(e.timestamp).getTime()
      return t > phaseStartMs && t < phaseEndMs
    })

    return {
      number: idx + 1,
      startEvent: phaseEvent,
      events,
    }
  })
}

/**
 * Find conversational loop boundaries.
 * Each loop is opened by conversational_loop_started or conversational_loop_resumed
 * and closed by the next conversational_loop_paused.
 * Returns [{startEvent, endEvent}] — endEvent is null if in-progress.
 */
function findConversationalLoops(sortedEvents) {
  const opens = sortedEvents.filter(
    e => e.event_category === 'decision' &&
         (e.event_type === 'conversational_loop_started' || e.event_type === 'conversational_loop_resumed')
  )
  const closes = sortedEvents.filter(
    e => e.event_category === 'decision' && e.event_type === 'conversational_loop_paused'
  )
  return opens.map((openEvent, idx) => ({
    startEvent: openEvent,
    endEvent: closes[idx] || null,
  }))
}

/**
 * Find all repair cycle boundaries.
 * Repair cycles are bounded by agent_initialized / agent_completed (or agent_failed) events for agent='repair_cycle'.
 * Returns [{startEvent, endEvent}] — one entry per repair cycle. endEvent is null if in-progress.
 */
function findRepairCycles(sortedEvents) {
  const startEvents = sortedEvents.filter(
    e =>
      e.event_category === 'agent_lifecycle' &&
      e.event_type === 'agent_initialized' &&
      e.agent === 'repair_cycle'
  )
  return startEvents.map(startEvent => {
    const startMs = new Date(startEvent.timestamp).getTime()
    const endEvent = sortedEvents.find(
      e =>
        e.event_category === 'agent_lifecycle' &&
        (e.event_type === 'agent_completed' || e.event_type === 'agent_failed') &&
        e.agent === 'repair_cycle' &&
        new Date(e.timestamp).getTime() > startMs
    )
    return { startEvent, endEvent }
  })
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
  {
    startEventType: 'repair_cycle_warning_review_started',
    endEventType:   'repair_cycle_warning_review_completed',
    cycleType:      'warning_review',
    label:          'Warning Review',
  },
  {
    startEventType: 'repair_cycle_systemic_analysis_started',
    endEventType:   'repair_cycle_systemic_analysis_completed',
    cycleType:      'systemic_analysis',
    label:          'Systemic Analysis',
  },
  {
    startEventType: 'repair_cycle_systemic_fix_started',
    endEventType:   'repair_cycle_systemic_fix_completed',
    cycleType:      'systemic_fix',
    label:          'Systemic Fix',
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
    const allSubCycles = REPAIR_SUB_CYCLE_REGISTRY.flatMap(entry =>
      groupSubCycles(events, entry, agentExecutions)
    )

    // Filter out test_execution sub-cycles that start inside another sub-cycle's window.
    // Those executions are internal runs triggered by that sub-cycle (e.g. systemic_fix),
    // not independent top-level iterations of this test cycle.
    const nonTestExecRanges = allSubCycles
      .filter(sc => sc.cycleType !== 'test_execution')
      .map(sc => ({
        startMs: new Date(sc.startEvent.timestamp).getTime(),
        endMs: sc.endEvent ? new Date(sc.endEvent.timestamp).getTime() : Infinity,
      }))

    const subCycles = allSubCycles.filter(sc => {
      if (sc.cycleType !== 'test_execution') return true
      const startMs = new Date(sc.startEvent.timestamp).getTime()
      return !nonTestExecRanges.some(r => startMs > r.startMs && startMs < r.endMs)
    })

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
 * Infer synthetic close events for any open cycles, working deepest-first.
 *
 * Nesting order (deepest → outermost):
 *   Sub-cycles (test_execution, fix_cycle) → Test cycles → Repair cycles → Review cycles
 *
 * For each unclosed cycle a synthetic close event is created with:
 *   - timestamp = earliest valid upper bound (next sibling's start, parent's end, last event + 1 ms)
 *   - _inferred: true  (flag for downstream renderers)
 *   - all other fields copied from the matching open event
 *
 * Returns the augmented sorted array; the original is not mutated.
 */
function inferMissingCloseEvents(sortedEvents) {
  if (!sortedEvents.length) return sortedEvents

  const synthetic = []
  const lastMs = new Date(sortedEvents[sortedEvents.length - 1].timestamp).getTime()

  function makeSyntheticClose(template, eventType, timestampMs, overrides = {}) {
    return { ...template, event_type: eventType, timestamp: new Date(timestampMs).toISOString(), _inferred: true, ...overrides }
  }

  // Earliest ms strictly greater than startMs from the candidate list.
  // Falls back to lastMs + 1 when no valid candidate exists.
  function closestUpperBound(startMs, ...candidates) {
    const valid = candidates.filter(ms => ms != null && ms > startMs)
    return valid.length > 0 ? Math.min(...valid) : lastMs + 1
  }

  // Snapshot of real + synthetic events sorted chronologically (rebuilt each phase).
  function snapshot() {
    return [...sortedEvents, ...synthetic].sort(
      (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    )
  }

  // ── Phase 1: Review cycles ────────────────────────────────────────────────
  {
    const reviewStarts = sortedEvents.filter(
      e => e.event_category === 'decision' && e.event_type === 'review_cycle_started'
    )
    const reviewEnds = sortedEvents.filter(
      e => e.event_category === 'decision' && e.event_type === 'review_cycle_completed'
    )
    reviewStarts.forEach((start, idx) => {
      if (reviewEnds[idx]) return
      const startMs = new Date(start.timestamp).getTime()
      const nextStart = reviewStarts[idx + 1]
      synthetic.push(makeSyntheticClose(
        start,
        'review_cycle_completed',
        closestUpperBound(startMs, nextStart ? new Date(nextStart.timestamp).getTime() : null)
      ))
    })
  }

  // ── Phase 1b: PR review stages ───────────────────────────────────────────
  {
    const prStarts = sortedEvents.filter(
      e => e.event_category === 'decision' && e.event_type === 'pr_review_stage_started'
    )
    const prEnds = sortedEvents.filter(
      e => e.event_category === 'decision' && e.event_type === 'pr_review_stage_completed'
    )
    prStarts.forEach((start, idx) => {
      if (prEnds[idx]) return
      const startMs = new Date(start.timestamp).getTime()
      const nextStart = prStarts[idx + 1]
      synthetic.push(makeSyntheticClose(
        start,
        'pr_review_stage_completed',
        closestUpperBound(startMs, nextStart ? new Date(nextStart.timestamp).getTime() : null)
      ))
    })
  }

  // ── Phase 1c: Conversational loops ───────────────────────────────────────
  {
    const loopOpens = sortedEvents.filter(
      e => e.event_category === 'decision' &&
           (e.event_type === 'conversational_loop_started' || e.event_type === 'conversational_loop_resumed')
    )
    const loopCloses = sortedEvents.filter(
      e => e.event_category === 'decision' && e.event_type === 'conversational_loop_paused'
    )
    loopOpens.forEach((open, idx) => {
      if (loopCloses[idx]) return
      const openMs = new Date(open.timestamp).getTime()
      const nextOpen = loopOpens[idx + 1]
      synthetic.push(makeSyntheticClose(
        open,
        'conversational_loop_paused',
        closestUpperBound(openMs, nextOpen ? new Date(nextOpen.timestamp).getTime() : null)
      ))
    })
  }

  // ── Phase 2: Repair cycles ────────────────────────────────────────────────
  const repairStarts = sortedEvents.filter(
    e => e.event_category === 'agent_lifecycle' &&
         e.event_type === 'agent_initialized' &&
         e.agent === 'repair_cycle'
  )
  {
    repairStarts.forEach((startEvent, idx) => {
      const startMs = new Date(startEvent.timestamp).getTime()
      const hasEnd = sortedEvents.some(
        e => e.event_category === 'agent_lifecycle' &&
             (e.event_type === 'agent_completed' || e.event_type === 'agent_failed') &&
             e.agent === 'repair_cycle' &&
             new Date(e.timestamp).getTime() > startMs
      )
      if (hasEnd) return
      const nextRepairStart = repairStarts[idx + 1]
      synthetic.push(makeSyntheticClose(
        startEvent,
        'agent_completed',
        closestUpperBound(startMs, nextRepairStart ? new Date(nextRepairStart.timestamp).getTime() : null),
        { event_category: 'agent_lifecycle', agent: 'repair_cycle', success: null, error: null, duration_ms: null }
      ))
    })
  }

  // ── Phase 3: Test cycles within repair cycles ─────────────────────────────
  // (uses Phase 2 synthetic repair-cycle ends; snapshot includes any Phase 2 additions)
  {
    const stream = synthetic.length > 0 ? snapshot() : sortedEvents
    repairStarts.forEach(repairStart => {
      const repairStartMs = new Date(repairStart.timestamp).getTime()
      const repairEnd = stream.find(
        e => e.event_category === 'agent_lifecycle' &&
             (e.event_type === 'agent_completed' || e.event_type === 'agent_failed') &&
             e.agent === 'repair_cycle' &&
             new Date(e.timestamp).getTime() > repairStartMs
      )
      const repairEndMs = repairEnd ? new Date(repairEnd.timestamp).getTime() : lastMs + 1

      const tcStarts = sortedEvents.filter(e => {
        const t = new Date(e.timestamp).getTime()
        return t >= repairStartMs && t <= repairEndMs && e.event_type === 'repair_cycle_test_cycle_started'
      })
      const tcEnds = sortedEvents.filter(e => {
        const t = new Date(e.timestamp).getTime()
        return t >= repairStartMs && t <= repairEndMs && e.event_type === 'repair_cycle_test_cycle_completed'
      })

      tcStarts.forEach((tcStart, idx) => {
        if (tcEnds[idx]) return
        const tcStartMs = new Date(tcStart.timestamp).getTime()
        const nextTcStart = tcStarts[idx + 1]
        synthetic.push(makeSyntheticClose(
          tcStart,
          'repair_cycle_test_cycle_completed',
          closestUpperBound(tcStartMs, nextTcStart ? new Date(nextTcStart.timestamp).getTime() : null, repairEndMs)
        ))
      })
    })
  }

  // ── Phase 4: Sub-cycles within test cycles ────────────────────────────────
  // (uses Phase 2 + 3 synthetic ends via snapshot)
  {
    const stream = snapshot()
    repairStarts.forEach(repairStart => {
      const repairStartMs = new Date(repairStart.timestamp).getTime()
      const repairEnd = stream.find(
        e => e.event_category === 'agent_lifecycle' &&
             (e.event_type === 'agent_completed' || e.event_type === 'agent_failed') &&
             e.agent === 'repair_cycle' &&
             new Date(e.timestamp).getTime() > repairStartMs
      )
      const repairEndMs = repairEnd ? new Date(repairEnd.timestamp).getTime() : lastMs + 1

      const allTcStarts = sortedEvents.filter(e => {
        const t = new Date(e.timestamp).getTime()
        return t >= repairStartMs && t <= repairEndMs && e.event_type === 'repair_cycle_test_cycle_started'
      })
      const allTcEnds = stream.filter(e => {
        const t = new Date(e.timestamp).getTime()
        return t >= repairStartMs && t <= repairEndMs && e.event_type === 'repair_cycle_test_cycle_completed'
      })

      allTcStarts.forEach((tcStart, idx) => {
        const tcStartMs = new Date(tcStart.timestamp).getTime()
        const tcEnd = allTcEnds[idx] || null
        const tcEndMs = tcEnd ? new Date(tcEnd.timestamp).getTime() : repairEndMs

        REPAIR_SUB_CYCLE_REGISTRY.forEach(({ startEventType, endEventType }) => {
          const scStarts = sortedEvents.filter(e => {
            const t = new Date(e.timestamp).getTime()
            return t > tcStartMs && t < tcEndMs && e.event_type === startEventType
          })
          const scEnds = sortedEvents.filter(e => {
            const t = new Date(e.timestamp).getTime()
            return t > tcStartMs && t < tcEndMs && e.event_type === endEventType
          })

          scStarts.forEach((scStart, scIdx) => {
            if (scEnds[scIdx]) return
            const scStartMs = new Date(scStart.timestamp).getTime()
            const nextScStart = scStarts[scIdx + 1]
            synthetic.push(makeSyntheticClose(
              scStart,
              endEventType,
              closestUpperBound(scStartMs, nextScStart ? new Date(nextScStart.timestamp).getTime() : null, tcEndMs)
            ))
          })
        })
      })
    })
  }

  if (!synthetic.length) return sortedEvents

  return [...sortedEvents, ...synthetic].sort(
    (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
  )
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

  const _t0 = performance.now()

  // Pre-cache timestamp ms values to avoid repeated Date parsing across filter/sort loops
  const tsMs = new Map()
  const getMs = (ts) => {
    if (!tsMs.has(ts)) tsMs.set(ts, new Date(ts).getTime())
    return tsMs.get(ts)
  }

  // Sort events chronologically, then infer synthetic close events for any open cycles
  const sorted = inferMissingCloseEvents(
    [...events].sort((a, b) => getMs(a.timestamp) - getMs(b.timestamp))
  )
  const _t1 = performance.now()

  // Build agent execution map (needed for iteration grouping)
  const agentExecutions = buildAgentExecutionMap(sorted)
  const _t2 = performance.now()

  // Detect cycle boundaries
  const reviewCycleBoundaries = findReviewCycles(sorted)
  const repairCycleBoundaries = findRepairCycles(sorted)
  const prReviewCycleBoundaries = findPRReviewCycles(sorted)
  const conversationalLoopBoundaries = findConversationalLoops(sorted)
  const _t3 = performance.now()

  // Compute the earliest/latest cycle timestamps for prelude/postlude splitting
  let firstCycleStartMs = Infinity
  let lastCycleEndMs = -Infinity

  reviewCycleBoundaries.forEach(({ startEvent, endEvent }) => {
    const sMs = getMs(startEvent.timestamp)
    const eMs = endEvent ? getMs(endEvent.timestamp) : Date.now()
    firstCycleStartMs = Math.min(firstCycleStartMs, sMs)
    lastCycleEndMs = Math.max(lastCycleEndMs, eMs)
  })

  repairCycleBoundaries.forEach(({ startEvent, endEvent }) => {
    const sMs = getMs(startEvent.timestamp)
    const eMs = endEvent ? getMs(endEvent.timestamp) : Date.now()
    firstCycleStartMs = Math.min(firstCycleStartMs, sMs)
    lastCycleEndMs = Math.max(lastCycleEndMs, eMs)
  })

  prReviewCycleBoundaries.forEach(({ startEvent, endEvent }) => {
    const sMs = getMs(startEvent.timestamp)
    const eMs = endEvent ? getMs(endEvent.timestamp) : Date.now()
    firstCycleStartMs = Math.min(firstCycleStartMs, sMs)
    lastCycleEndMs = Math.max(lastCycleEndMs, eMs)
  })

  conversationalLoopBoundaries.forEach(({ startEvent, endEvent }) => {
    const sMs = getMs(startEvent.timestamp)
    const eMs = endEvent ? getMs(endEvent.timestamp) : Date.now()
    firstCycleStartMs = Math.min(firstCycleStartMs, sMs)
    lastCycleEndMs = Math.max(lastCycleEndMs, eMs)
  })

  const noCycles = firstCycleStartMs === Infinity

  // Prelude: events strictly before the first cycle's startTime
  const preludeEvents = noCycles
    ? sorted
    : sorted.filter(e => getMs(e.timestamp) < firstCycleStartMs)

  // Postlude: events strictly after the last cycle's endTime
  const postludeEvents = noCycles
    ? []
    : sorted.filter(e => getMs(e.timestamp) > lastCycleEndMs)

  // Build cycles
  const cycles = []

  reviewCycleBoundaries.forEach((boundary, idx) => {
    const { startEvent, endEvent } = boundary
    const cycleStartMs = getMs(startEvent.timestamp)
    const cycleEndMs = endEvent ? getMs(endEvent.timestamp) : Date.now()

    const cycleEvents = sorted.filter(e => {
      const t = getMs(e.timestamp)
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

  repairCycleBoundaries.forEach((boundary, idx) => {
    const { startEvent, endEvent } = boundary
    const cycleStartMs = getMs(startEvent.timestamp)
    const cycleEndMs = endEvent ? getMs(endEvent.timestamp) : Date.now()

    const cycleEvents = sorted.filter(e => {
      const t = getMs(e.timestamp)
      return t >= cycleStartMs && t <= cycleEndMs
    })

    const testCycles = groupRepairTestCycles(cycleEvents, boundary, agentExecutions)

    // Events in the repair cycle window that fall outside any test-cycle boundary
    const allTcWindows = testCycles.map(tc => ({
      startMs: getMs(tc.startEvent.timestamp),
      endMs:   tc.endEvent ? getMs(tc.endEvent.timestamp) : Infinity,
    }))
    const residualEvents = cycleEvents.filter(e => {
      const t = getMs(e.timestamp)
      return !allTcWindows.some(w => t >= w.startMs && t <= w.endMs)
    })

    cycles.push({
      id: `repair_cycle_${idx + 1}`,
      type: 'repair_cycle',
      startEvent,
      endEvent,
      testCycles,
      events: residualEvents,
      isCollapsed: false,
    })
  })

  prReviewCycleBoundaries.forEach((boundary, idx) => {
    const { startEvent, endEvent } = boundary
    const cycleStartMs = getMs(startEvent.timestamp)
    const cycleEndMs = endEvent ? getMs(endEvent.timestamp) : Date.now()

    const cycleEvents = sorted.filter(e => {
      const t = getMs(e.timestamp)
      return t >= cycleStartMs && t <= cycleEndMs
    })

    const phases = groupPRReviewPhases(cycleEvents, boundary)

    cycles.push({
      id: `pr_review_cycle_${idx + 1}`,
      type: 'pr_review_cycle',
      startEvent,
      endEvent,
      phases,
      isCollapsed: false,
    })
  })

  conversationalLoopBoundaries.forEach((boundary, idx) => {
    const { startEvent, endEvent } = boundary
    const cycleStartMs = getMs(startEvent.timestamp)
    const cycleEndMs = endEvent ? getMs(endEvent.timestamp) : Date.now()

    const cycleEvents = sorted.filter(e => {
      const t = getMs(e.timestamp)
      return t >= cycleStartMs && t <= cycleEndMs
    })

    // Child events: everything inside the loop window except the open/close markers themselves
    const childEvents = cycleEvents.filter(
      e => e.event_type !== 'conversational_loop_started' &&
           e.event_type !== 'conversational_loop_resumed' &&
           e.event_type !== 'conversational_loop_paused'
    )

    cycles.push({
      id: `conversational_loop_${idx + 1}`,
      type: 'conversational_loop',
      startEvent,
      endEvent,
      events: childEvents,
      isCollapsed: false,
    })
  })

  // Sort all cycles chronologically (review and repair cycles may interleave)
  cycles.sort((a, b) =>
    new Date(a.startEvent.timestamp).getTime() - new Date(b.startEvent.timestamp).getTime()
  )

  const _tEnd = performance.now()
  console.log(
    `[PerfGraph] processEvents: ${(_tEnd - _t0).toFixed(1)}ms` +
    ` | in:${events.length} sorted:${sorted.length} cycles:${cycles.length}` +
    ` | sort+infer:${(_t1 - _t0).toFixed(1)}ms` +
    ` | agentMap:${(_t2 - _t1).toFixed(1)}ms` +
    ` | boundaries:${(_t3 - _t2).toFixed(1)}ms` +
    ` | cycleBuild:${(_tEnd - _t3).toFixed(1)}ms`
  )

  return {
    prelude: { events: preludeEvents },
    cycles,
    postlude: { events: postludeEvents },
    agentExecutions,
  }
}
