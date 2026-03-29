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

import { CYCLE_TERMINAL_EVENTS } from '../cycleDefinitions.js'

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

  // Mark zombie executions as 'interrupted'.
  // A zombie is a 'running' execution where a later execution for the same agent has
  // already completed or failed. This happens when an orchestrator restart kills an agent
  // container without emitting an end event.
  map.forEach(executions => {
    executions.forEach(exec => {
      if (exec.status !== 'running') return
      const hasLaterSettled = executions.some(
        other => other.startTime > exec.startTime && other.status !== 'running'
      )
      if (hasLaterSettled) exec.status = 'interrupted'
    })
  })

  return map
}

/**
 * Test whether an event matches the given event_type(s) and definition filters
 * (eventCategory, matchFields, excludeFields).
 */
function matchEventForDef(event, types, def) {
  const eventCategory = def.eventCategory ?? 'decision'
  if (event.event_category !== eventCategory) return false
  if (!types.includes(event.event_type)) return false
  if (def.matchFields) {
    for (const [k, v] of Object.entries(def.matchFields)) {
      if (event[k] !== v) return false
    }
  }
  if (def.excludeFields) {
    for (const [k, v] of Object.entries(def.excludeFields)) {
      if (event[k] === v) return false
    }
  }
  return true
}

/**
 * Match start events to end events using sequential-window pairing.
 * For each start S_i, the end must come after S_i AND before the next start S_{i+1}.
 * An end that only arrives after S_{i+1} has begun belongs to a later cycle, not this one.
 *
 * This correctly handles abandoned cycles: if a cycle is restarted without a close event,
 * the existing end event is left for the later start that actually owns it.
 * Returns [{startEvent, endEvent}] — endEvent is null for abandoned/open starts.
 */
function pairCycleEvents(starts, ends) {
  const usedEnds = new Set()
  return starts.map((start, idx) => {
    const startMs = new Date(start.timestamp).getTime()
    const nextStart = starts[idx + 1]
    const nextStartMs = nextStart ? new Date(nextStart.timestamp).getTime() : Infinity
    const matchedEnd = ends.find(
      e => !usedEnds.has(e) &&
           new Date(e.timestamp).getTime() > startMs &&
           new Date(e.timestamp).getTime() <= nextStartMs
    )
    if (matchedEnd) usedEnds.add(matchedEnd)
    return { startEvent: start, endEvent: matchedEnd ?? null }
  })
}

/**
 * Match start events to end events by a shared field value (e.g. task_id).
 * Used for cycle types where multiple instances can run concurrently, so
 * sequential-window pairing would give incorrect results.
 * Returns [{startEvent, endEvent}] — endEvent is null for open starts.
 */
function pairCycleEventsByField(starts, ends, field) {
  const usedEnds = new Set()
  return starts.map(start => {
    const fieldValue = start[field]
    const startMs = new Date(start.timestamp).getTime()
    const matchedEnd = ends.find(
      e => !usedEnds.has(e) &&
           e[field] === fieldValue &&
           new Date(e.timestamp).getTime() > startMs
    )
    if (matchedEnd) usedEnds.add(matchedEnd)
    return { startEvent: start, endEvent: matchedEnd ?? null }
  })
}

/**
 * Find cycle boundaries for any cycle type defined in CYCLE_TERMINAL_EVENTS.
 * Supports both single startEvent (string) and multiple startEvents (array),
 * eventCategory filtering, matchFields/excludeFields, and pairByField for
 * concurrent cycle types.
 * Returns [{startEvent, endEvent}] — endEvent is null for abandoned/open starts.
 */
function findCycleBoundaries(def, sortedEvents) {
  const startTypes = def.startEvents ?? [def.startEvent]
  const terminalTypes = Object.keys(def.terminalEvents)
  const starts = sortedEvents.filter(e => matchEventForDef(e, startTypes, def))
  const ends = sortedEvents.filter(e => matchEventForDef(e, terminalTypes, def))
  if (def.pairByField) {
    return pairCycleEventsByField(starts, ends, def.pairByField)
  }
  return pairCycleEvents(starts, ends)
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
 * Recursively detects nested sub-cycles one level deep (e.g. a test_execution
 * that runs inside a systemic_fix is captured as a nested sub-cycle of that fix,
 * accessible via sc.subCycles). Events that fall inside a nested sub-cycle's
 * window are excluded from the outer sub-cycle's own events list so they are
 * not double-counted.
 */
function groupSubCycles(events, registryEntry, agentExecutions, depth = 0) {
  const MAX_NESTING_DEPTH = 1
  const { startEventType, endEventType, cycleType, label } = registryEntry
  const starts = events.filter(e => e.event_type === startEventType)
  const ends   = events.filter(e => e.event_type === endEventType)

  return starts.map((startEvent, idx) => {
    const endEvent  = ends[idx] || null
    const startMs   = new Date(startEvent.timestamp).getTime()
    const endMs     = endEvent ? new Date(endEvent.timestamp).getTime() : Infinity
    const windowEvents = events.filter(e => {
      const t = new Date(e.timestamp).getTime()
      return t > startMs && t < endMs
    })

    // Detect nested sub-cycles one level deep, then exclude their events from
    // this sub-cycle's own event list so events belong to exactly one container.
    let subCycles = []
    let residualEvents = windowEvents
    if (depth < MAX_NESTING_DEPTH) {
      // Exclude the current entry from recursive detection to prevent a sub-cycle type
      // from being nested inside another instance of itself (e.g. systemic_fix inside
      // systemic_fix). Only cross-type nesting is meaningful (e.g. test_execution inside
      // systemic_fix).
      subCycles = REPAIR_SUB_CYCLE_REGISTRY
        .filter(e => e !== registryEntry)
        .flatMap(entry => groupSubCycles(windowEvents, entry, agentExecutions, depth + 1))
      if (subCycles.length > 0) {
        const nestedRanges = subCycles.map(nsc => ({
          startMs: new Date(nsc.startEvent.timestamp).getTime(),
          endMs:   nsc.endEvent ? new Date(nsc.endEvent.timestamp).getTime() : Infinity,
        }))
        residualEvents = windowEvents.filter(e => {
          const t = new Date(e.timestamp).getTime()
          return !nestedRanges.some(r => t >= r.startMs && t <= r.endMs)
        })
      }
    }

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
      events: residualEvents,
      subCycles,
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
 * Phase 1 handles all cycle types defined in CYCLE_TERMINAL_EVENTS (including
 * repair_cycle and agent_execution). Phases 2-3b handle repair-cycle-specific
 * internal sub-structure (test cycles → sub-cycles → nested sub-cycles).
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

  // ── Phase 1: All cycle types defined in CYCLE_TERMINAL_EVENTS ────────────
  // Generic: each definition drives its own open/close pairing via matchEventForDef.
  // Handles decision events, agent_lifecycle events, matchFields, excludeFields, and
  // pairByField for concurrent cycle types (e.g. agent_execution).
  for (const def of Object.values(CYCLE_TERMINAL_EVENTS)) {
    const startTypes    = def.startEvents ?? [def.startEvent]
    const terminalTypes = Object.keys(def.terminalEvents)
    const opens  = sortedEvents.filter(e => matchEventForDef(e, startTypes, def))
    const closes = sortedEvents.filter(e => matchEventForDef(e, terminalTypes, def))
    const usedCloses = new Set()
    opens.forEach((open, idx) => {
      const openMs   = new Date(open.timestamp).getTime()
      const nextOpen = opens[idx + 1]
      const nextOpenMs = nextOpen ? new Date(nextOpen.timestamp).getTime() : Infinity

      let matched
      if (def.pairByField) {
        const fieldVal = open[def.pairByField]
        matched = closes.find(
          e => !usedCloses.has(e) &&
               e[def.pairByField] === fieldVal &&
               new Date(e.timestamp).getTime() > openMs
        )
      } else {
        matched = closes.find(
          e => !usedCloses.has(e) &&
               new Date(e.timestamp).getTime() > openMs &&
               new Date(e.timestamp).getTime() <= nextOpenMs
        )
      }
      if (matched) { usedCloses.add(matched); return }
      synthetic.push(makeSyntheticClose(
        open,
        def.syntheticCloseType,
        closestUpperBound(openMs, nextOpen ? nextOpenMs : null),
        def.syntheticCloseOverrides ?? {}
      ))
    })
  }

  // Pre-compute repair cycle starts (needed for Phases 2-4 sub-cycle inference).
  const repairStarts = sortedEvents.filter(
    e => matchEventForDef(e, ['agent_initialized'], CYCLE_TERMINAL_EVENTS.repair_cycle)
  )

  // ── Phase 2: Test cycles within repair cycles ─────────────────────────────
  // (uses Phase 1 synthetic repair-cycle ends; snapshot includes any Phase 1 additions)
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

  // ── Phase 3: Sub-cycles within test cycles ────────────────────────────────
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

  // ── Phase 3b: Nested sub-cycles within outer sub-cycles ─────────────────
  // Phase 3 inferred close events for outer sub-cycles. Now use those (via snapshot)
  // to determine outer-SC windows and infer closes for any unclosed nested sub-cycles
  // (e.g. a test_execution running inside a systemic_fix with no completed event).
  {
    const stream4b = snapshot()
    repairStarts.forEach(repairStart => {
      const repairStartMs = new Date(repairStart.timestamp).getTime()
      const repairEnd = stream4b.find(
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
      const allTcEnds = stream4b.filter(e => {
        const t = new Date(e.timestamp).getTime()
        return t >= repairStartMs && t <= repairEndMs && e.event_type === 'repair_cycle_test_cycle_completed'
      })

      allTcStarts.forEach((tcStart, idx) => {
        const tcStartMs = new Date(tcStart.timestamp).getTime()
        const tcEnd = allTcEnds[idx] || null
        const tcEndMs = tcEnd ? new Date(tcEnd.timestamp).getTime() : repairEndMs

        REPAIR_SUB_CYCLE_REGISTRY.forEach(({ startEventType: outerStartType, endEventType: outerEndType }) => {
          const outerStarts = sortedEvents.filter(e => {
            const t = new Date(e.timestamp).getTime()
            return t > tcStartMs && t < tcEndMs && e.event_type === outerStartType
          })
          const outerEnds = stream4b.filter(e => {
            const t = new Date(e.timestamp).getTime()
            return t > tcStartMs && t < tcEndMs && e.event_type === outerEndType
          })

          outerStarts.forEach((outerStart, outerIdx) => {
            const outerStartMs = new Date(outerStart.timestamp).getTime()
            const outerEnd = outerEnds[outerIdx] || null
            const outerEndMs = outerEnd ? new Date(outerEnd.timestamp).getTime() : tcEndMs

            REPAIR_SUB_CYCLE_REGISTRY.forEach(({ startEventType: innerStartType, endEventType: innerEndType }) => {
              // Skip same-type pairings: a sub-cycle type cannot be nested inside itself.
              // Allowing it would produce spurious closes when same-type boundaries overlap.
              if (innerStartType === outerStartType) return

              const innerStarts = sortedEvents.filter(e => {
                const t = new Date(e.timestamp).getTime()
                return t > outerStartMs && t < outerEndMs && e.event_type === innerStartType
              })
              const innerEnds = sortedEvents.filter(e => {
                const t = new Date(e.timestamp).getTime()
                return t > outerStartMs && t < outerEndMs && e.event_type === innerEndType
              })

              innerStarts.forEach((innerStart, innerIdx) => {
                if (innerEnds[innerIdx]) return
                const innerStartMs = new Date(innerStart.timestamp).getTime()
                const nextInnerStart = innerStarts[innerIdx + 1]
                synthetic.push(makeSyntheticClose(
                  innerStart,
                  innerEndType,
                  closestUpperBound(
                    innerStartMs,
                    nextInnerStart ? new Date(nextInnerStart.timestamp).getTime() : null,
                    outerEndMs
                  )
                ))
              })
            })
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
 * Group events by agent execution boundaries.
 *
 * Given a flat list of events at any level of the model, finds agent_initialized
 * events (excluding repair_cycle agents), looks up their execution windows from the
 * global agentExecutionMap, claims child events within each window, and returns the
 * agent execution containers alongside the residual (unclaimed) events.
 *
 * This is the same pattern used for iterations within review cycles, sub-cycles within
 * test cycles, etc. — start event, end event, child events within the time window.
 *
 * @param {Array}    events             - Events to group
 * @param {Map}      agentExecutionMap  - Global agent → [execution] map
 * @param {Function} getMs             - Timestamp → ms converter (cached)
 * @returns {{ agentExecutions: Array, events: Array }}
 */
function groupAgentExecutions(events, agentExecutionMap, getMs) {
  const initEvents = events.filter(e =>
    e.event_category === 'agent_lifecycle' &&
    e.event_type === 'agent_initialized' &&
    e.agent !== 'repair_cycle'
  )

  if (initEvents.length === 0) return { agentExecutions: [], events }

  // Build execution windows from the global map for each agent_initialized in this list
  const windows = []
  initEvents.forEach(initEvent => {
    const execList = agentExecutionMap.get(initEvent.agent) || []
    const exec = execList.find(e => e.taskId === initEvent.task_id)
    if (!exec) return
    windows.push({
      agent: initEvent.agent,
      taskId: initEvent.task_id,
      startEvent: initEvent,
      endEvent: exec.endEvent,
      startMs: getMs(exec.startTime),
      endMs: exec.endTime ? getMs(exec.endTime) : Infinity,
      status: exec.status,
    })
  })

  if (windows.length === 0) return { agentExecutions: [], events }

  // Claim events within each execution window
  const claimed = new Set()
  const agentExecs = windows.map(w => {
    const children = []
    events.forEach(e => {
      if (claimed.has(e)) return
      if (e === w.startEvent) { claimed.add(e); return }

      // agent_lifecycle events for the same task belong to this container
      if (e.event_category === 'agent_lifecycle' && e.task_id === w.taskId) {
        claimed.add(e)
        // agent_completed/agent_failed are boundary markers — claim but don't add as children
        if (e.event_type !== 'agent_completed' && e.event_type !== 'agent_failed') {
          children.push(e)
        }
        return
      }

      // Decision events within the time window belong to this container
      const t = getMs(e.timestamp)
      if (t > w.startMs && t <= w.endMs) {
        claimed.add(e)
        children.push(e)
      }
    })

    return {
      type: 'agent_execution',
      agent: w.agent,
      taskId: w.taskId,
      startEvent: w.startEvent,
      endEvent: w.endEvent,
      events: children,
      status: w.status,
      isCollapsed: true,
    }
  })

  const residual = events.filter(e => !claimed.has(e))
  return { agentExecutions: agentExecs, events: residual }
}

/**
 * Walk the entire PipelineGraph model and nest agent executions at every level.
 * At each node that has an `events` array, groups events by agent execution
 * boundaries and adds an `agentExecutions` array alongside the residual events.
 */
function nestAgentExecutions(model, getMs) {
  const aeMap = model.agentExecutions

  function apply(node) {
    if (!node || !node.events || node.events.length === 0) return
    const result = groupAgentExecutions(node.events, aeMap, getMs)
    node.events = result.events
    node.agentExecutions = result.agentExecutions
  }

  // Prelude / postlude
  apply(model.prelude)
  apply(model.postlude)

  // Walk cycles and their sub-structures
  model.cycles.forEach(cycle => {
    apply(cycle)

    // Review cycle → iterations
    cycle.iterations?.forEach(iter => apply(iter))

    // Repair cycle → test cycles → sub-cycles (recursive)
    function walkSubCycles(subCycles) {
      if (!subCycles) return
      subCycles.forEach(sc => { apply(sc); walkSubCycles(sc.subCycles) })
    }
    cycle.testCycles?.forEach(tc => { apply(tc); walkSubCycles(tc.subCycles) })

    // PR review cycle → phases
    cycle.phases?.forEach(phase => apply(phase))
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
  const reviewCycleBoundaries        = findCycleBoundaries(CYCLE_TERMINAL_EVENTS.review_cycle, sorted)
  const repairCycleBoundaries        = findCycleBoundaries(CYCLE_TERMINAL_EVENTS.repair_cycle, sorted)
  const prReviewCycleBoundaries      = findCycleBoundaries(CYCLE_TERMINAL_EVENTS.pr_review_cycle, sorted)
  const conversationalLoopBoundaries = findCycleBoundaries(CYCLE_TERMINAL_EVENTS.conversational_loop, sorted)
  const statusProgressionBoundaries  = findCycleBoundaries(CYCLE_TERMINAL_EVENTS.status_progression, sorted)
  // agent_execution boundaries are NOT detected here — they nest within other cycles
  // (and prelude/postlude) via nestAgentExecutions() post-processing, using the
  // agentExecutionMap built above. The definition in cycleDefinitions.js is still
  // used by inferMissingCloseEvents for synthetic close inference.
  const _t3 = performance.now()

  // Compute the earliest/latest cycle timestamps for prelude/postlude splitting
  let firstCycleStartMs = Infinity
  let lastCycleEndMs = -Infinity

  // Agent execution boundaries are NOT used for prelude/postlude splitting — they
  // nest within other cycles (or prelude/postlude) rather than defining top-level structure.
  const structuralBoundaries = [
    ...reviewCycleBoundaries,
    ...repairCycleBoundaries,
    ...prReviewCycleBoundaries,
    ...conversationalLoopBoundaries,
    ...statusProgressionBoundaries,
  ]

  structuralBoundaries.forEach(({ startEvent, endEvent }) => {
    const sMs = getMs(startEvent.timestamp)
    const eMs = endEvent ? getMs(endEvent.timestamp) : Date.now()
    firstCycleStartMs = Math.min(firstCycleStartMs, sMs)
    lastCycleEndMs = Math.max(lastCycleEndMs, eMs)
  })

  // Pre-scan for orphaned review_cycle_iteration events to include their time bounds
  // in firstCycleStartMs / lastCycleEndMs before prelude/postlude are computed.
  // (Full orphaned-iteration processing happens after cycles are built below.)
  const reviewCycleWindowsForOrphans = reviewCycleBoundaries.map(({ startEvent, endEvent }) => ({
    startMs: getMs(startEvent.timestamp),
    endMs: endEvent ? getMs(endEvent.timestamp) : Date.now(),
  }))
  sorted.forEach(e => {
    if (e.event_type !== 'review_cycle_iteration') return
    const t = getMs(e.timestamp)
    if (reviewCycleWindowsForOrphans.some(w => t >= w.startMs && t <= w.endMs)) return
    firstCycleStartMs = Math.min(firstCycleStartMs, t)
    // Open-ended: orphaned iterations extend to now, so all subsequent events
    // are claimed by the iteration and excluded from the postlude.
    lastCycleEndMs = Date.now()
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

    // Residual events: in cycle window but not claimed by any iteration, and not the boundary events.
    // Without this, events that occur between cycle_started and the first iteration marker
    // (or after the last iteration) are silently lost — they appear in neither prelude, nor
    // any iteration, nor postlude (they fall inside the cycle's time window).
    const iterationClaimed = new Set()
    iterations.forEach(iter => {
      if (iter.startEvent) iterationClaimed.add(iter.startEvent)
      iter.events.forEach(e => iterationClaimed.add(e))
    })
    const reviewResiduals = cycleEvents.filter(e =>
      !iterationClaimed.has(e) && e !== startEvent && e !== endEvent
    )

    cycles.push({
      id: `review_cycle_${idx + 1}`,
      type: 'review_cycle',
      startEvent,
      endEvent,
      iterations,
      events: reviewResiduals,
      isCollapsed: true,
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
      isCollapsed: true,
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

    // Residual events: in cycle window but not claimed by any phase, and not the boundary events.
    const phasesClaimed = new Set()
    phases.forEach(phase => {
      if (phase.startEvent) phasesClaimed.add(phase.startEvent)
      phase.events.forEach(e => phasesClaimed.add(e))
    })
    const prResiduals = cycleEvents.filter(e =>
      !phasesClaimed.has(e) && e !== startEvent && e !== endEvent
    )

    cycles.push({
      id: `pr_review_cycle_${idx + 1}`,
      type: 'pr_review_cycle',
      startEvent,
      endEvent,
      phases,
      events: prResiduals,
      isCollapsed: true,
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
      isCollapsed: true,
    })
  })

  statusProgressionBoundaries.forEach((boundary, idx) => {
    const { startEvent, endEvent } = boundary
    const cycleStartMs = getMs(startEvent.timestamp)
    const cycleEndMs = endEvent ? getMs(endEvent.timestamp) : Date.now()

    const cycleEvents = sorted.filter(e => {
      const t = getMs(e.timestamp)
      return t >= cycleStartMs && t <= cycleEndMs
    })

    // Child events: everything inside the cycle window except the open/close markers
    const childEvents = cycleEvents.filter(
      e => e !== startEvent && e !== endEvent
    )

    cycles.push({
      id: `status_progression_${idx + 1}`,
      type: 'status_progression',
      startEvent,
      endEvent,
      events: childEvents,
      isCollapsed: true,  // default collapsed — quick operation, primary info is the summary
    })
  })

  // ── Orphaned review iterations ────────────────────────────────────────────
  // review_cycle_iteration events that fall outside every known review cycle window are
  // "orphaned" — they still need containers. This happens when an orchestrator restart
  // causes an iteration to fire without a preceding review_cycle_started event.
  // Each orphaned iteration becomes a standalone top-level cycle entry so it gets its
  // own iteration container in the graph without requiring a parent review cycle.
  {
    const isInsideReviewCycle = (t) =>
      reviewCycleWindowsForOrphans.some(w => t >= w.startMs && t <= w.endMs)

    const orphanedIterMarkers = sorted.filter(e => {
      if (e.event_type !== 'review_cycle_iteration') return false
      return !isInsideReviewCycle(getMs(e.timestamp))
    })

    orphanedIterMarkers.forEach((iterEvent, idx) => {
      const iterStartMs = getMs(iterEvent.timestamp)
      const nextMarker = orphanedIterMarkers[idx + 1]
      const iterEndMs = nextMarker ? getMs(nextMarker.timestamp) : Infinity

      const events = sorted.filter(e => {
        const t = getMs(e.timestamp)
        return t > iterStartMs && t < iterEndMs && !isInsideReviewCycle(t)
      })

      const iterAgentExecs = []
      agentExecutions.forEach((executions, agent) => {
        executions.forEach((exec, executionIndex) => {
          const execMs = getMs(exec.startTime)
          if (execMs >= iterStartMs && execMs < iterEndMs) {
            iterAgentExecs.push({ agent, execution: exec, executionIndex })
          }
        })
      })

      cycles.push({
        id: `review_iteration_orphan_${idx + 1}`,
        type: 'review_iteration',
        startEvent: iterEvent,
        endEvent: null,
        events,
        agentExecutions: iterAgentExecs,
        iterationNumber: idx + 1,
        isCollapsed: true,
      })
    })
  }

  // Sort all cycles chronologically (review and repair cycles may interleave)
  cycles.sort((a, b) =>
    getMs(a.startEvent.timestamp) - getMs(b.startEvent.timestamp)
  )

  const result = {
    prelude: { events: preludeEvents },
    cycles,
    postlude: { events: postludeEvents },
    agentExecutions,
  }

  // Nest agent executions at every level of the model — same pattern as iterations
  // within review cycles or sub-cycles within test cycles.
  nestAgentExecutions(result, getMs)

  const _tEnd = performance.now()
  console.log(
    `[PerfGraph] processEvents: ${(_tEnd - _t0).toFixed(1)}ms` +
    ` | in:${events.length} sorted:${sorted.length} cycles:${cycles.length}` +
    ` | sort+infer:${(_t1 - _t0).toFixed(1)}ms` +
    ` | agentMap:${(_t2 - _t1).toFixed(1)}ms` +
    ` | boundaries:${(_t3 - _t2).toFixed(1)}ms` +
    ` | cycleBuild:${(_tEnd - _t3).toFixed(1)}ms`
  )

  return result
}
